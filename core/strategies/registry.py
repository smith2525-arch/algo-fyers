"""Strategy Registry & Immutable Versioning Service (`Strategy Lab` core engine)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from core.audit.service import AuditService
from core.clock import ensure_ist, now_ist
from core.domain.entities import StrategyDefinitionEntity, StrategyVersionEntity
from core.domain.enums import (
    ActorType,
    AuditCategory,
    AuditSeverity,
    StrategyVersionStatus,
)
from core.exceptions import EntityNotFoundError, StrategyValidationError
from core.persistence.models import (
    InstrumentModel,
    StrategyDefinitionModel,
    StrategyVersionModel,
)
from core.persistence.seed import compute_parameter_hash
from core.strategies.config_schema import (
    EMA_RSI_SHORT_TAG,
    EMA_RSI_STRATEGY_CODE,
    PARAMETER_METADATA,
    EmaRsiStrategyConfig,
    parse_and_validate_ema_rsi_config,
)


def strategy_short_tag(strategy_code: str) -> str:
    code = strategy_code.strip().upper()
    if code == EMA_RSI_STRATEGY_CODE:
        return EMA_RSI_SHORT_TAG
    return code


def format_version_label(instrument_symbol: str, strategy_code: str, version_number: int) -> str:
    """Build canonical trading desk version label, e.g. `NIFTY_EMA_RSI_v1`."""
    return f"{instrument_symbol.strip().upper()}_{strategy_short_tag(strategy_code)}_v{version_number}"


def to_definition_entity(row: StrategyDefinitionModel) -> StrategyDefinitionEntity:
    return StrategyDefinitionEntity(
        id=row.id,
        code=row.code,
        name=row.name,
        category=row.category,
        description=row.description,
        parameter_schema=dict(row.parameter_schema_json or {}),
        default_parameters=dict(row.default_parameters_json or {}),
        supported_symbols=list(row.supported_symbols_json or []),
        is_enabled=bool(row.is_enabled),
        created_at=ensure_ist(row.created_at),
        updated_at=ensure_ist(row.updated_at),
    )


def to_version_entity(
    row: StrategyVersionModel,
    strategy_code: str,
    cloned_from_label: str | None = None,
) -> StrategyVersionEntity:
    resolved_clone_label = cloned_from_label
    if resolved_clone_label is None and row.cloned_from is not None:
        resolved_clone_label = row.cloned_from.version_label
    return StrategyVersionEntity(
        id=row.id,
        strategy_definition_id=row.strategy_definition_id,
        strategy_code=strategy_code,
        version_number=row.version_number,
        version_label=row.version_label,
        instrument_symbol=row.instrument_symbol,
        timeframe=row.timeframe,
        parameters=dict(row.parameters_json or {}),
        parameter_hash=row.parameter_hash,
        notes=row.notes,
        created_by=row.created_by,
        status=StrategyVersionStatus(row.status),
        created_at=ensure_ist(row.created_at),
        cloned_from_version_id=row.cloned_from_version_id,
        cloned_from_label=resolved_clone_label,
    )


class StrategyRegistryService:
    """Manages strategy validation, immutable version creation, cloning, and diffing."""

    @staticmethod
    def validate_config(
        session: Session,
        *,
        strategy_code: str,
        raw_parameters: dict[str, Any],
    ) -> EmaRsiStrategyConfig:
        clean_code = strategy_code.strip().upper()
        if clean_code != EMA_RSI_STRATEGY_CODE:
            raise EntityNotFoundError(f"Unsupported strategy definition code: {clean_code!r}")

        # Initial structural validation
        preliminary = parse_and_validate_ema_rsi_config(raw_parameters)

        # Verify instrument exists and is active in reference table, and check exchange hours
        inst_row = session.scalar(
            select(InstrumentModel).where(InstrumentModel.symbol == preliminary.instrument)
        )
        if inst_row is None:
            raise StrategyValidationError(
                f"Instrument {preliminary.instrument!r} is not registered in configurable reference data."
            )
        if not inst_row.is_active:
            raise StrategyValidationError(
                f"Instrument {preliminary.instrument!r} is currently disabled in Settings."
            )

        return parse_and_validate_ema_rsi_config(
            raw_parameters,
            exchange_open_ist=inst_row.session_open_ist,
            exchange_close_ist=inst_row.session_close_ist,
        )

    @staticmethod
    def list_definitions_with_versions(
        session: Session,
        *,
        instrument: str | None = None,
    ) -> list[tuple[StrategyDefinitionEntity, list[StrategyVersionEntity]]]:
        defs = session.scalars(
            select(StrategyDefinitionModel).order_by(StrategyDefinitionModel.code)
        ).all()
        clean_inst = instrument.strip().upper() if instrument else None

        output: list[tuple[StrategyDefinitionEntity, list[StrategyVersionEntity]]] = []
        for s_def in defs:
            stmt = select(StrategyVersionModel).where(
                StrategyVersionModel.strategy_definition_id == s_def.id
            )
            if clean_inst:
                stmt = stmt.where(StrategyVersionModel.instrument_symbol == clean_inst)
            stmt = stmt.order_by(
                desc(StrategyVersionModel.created_at),
                desc(StrategyVersionModel.version_number),
            )
            ver_rows = session.scalars(stmt).all()
            versions = [to_version_entity(v, s_def.code) for v in ver_rows]
            output.append((to_definition_entity(s_def), versions))
        return output

    @staticmethod
    def get_version(session: Session, version_id: UUID) -> StrategyVersionEntity:
        row = session.get(StrategyVersionModel, version_id)
        if row is None:
            raise EntityNotFoundError(f"StrategyVersion {version_id} not found.")
        return to_version_entity(row, row.definition.code)

    @classmethod
    def create_version(
        cls,
        session: Session,
        *,
        strategy_code: str,
        raw_parameters: dict[str, Any],
        notes: str | None,
        actor: str,
        cloned_from_version_id: UUID | None = None,
    ) -> StrategyVersionEntity:
        """Validate configuration and persist a new immutable StrategyVersion."""
        clean_code = strategy_code.strip().upper()
        s_def = session.scalar(
            select(StrategyDefinitionModel)
            .where(StrategyDefinitionModel.code == clean_code)
            .with_for_update()
        )
        if s_def is None:
            raise EntityNotFoundError(f"Strategy definition {clean_code!r} not found.")

        cloned_label: str | None = None
        if cloned_from_version_id is not None:
            parent_row = session.get(StrategyVersionModel, cloned_from_version_id)
            if parent_row is None:
                raise EntityNotFoundError(
                    f"Source StrategyVersion {cloned_from_version_id} for clone not found."
                )
            cloned_label = parent_row.version_label

        validated_cfg = cls.validate_config(
            session,
            strategy_code=clean_code,
            raw_parameters=raw_parameters,
        )
        canonical_params = validated_cfg.to_canonical_dict()
        instrument_sym = validated_cfg.instrument.upper()

        max_ver = session.scalar(
            select(func.max(StrategyVersionModel.version_number)).where(
                StrategyVersionModel.strategy_definition_id == s_def.id,
                StrategyVersionModel.instrument_symbol == instrument_sym,
            )
        )
        next_ver = int(max_ver or 0) + 1
        version_label = format_version_label(instrument_sym, clean_code, next_ver)
        param_hash = compute_parameter_hash(canonical_params)

        new_row = StrategyVersionModel(
            strategy_definition_id=s_def.id,
            version_number=next_ver,
            version_label=version_label,
            instrument_symbol=instrument_sym,
            timeframe=validated_cfg.timeframe.value,
            cloned_from_version_id=cloned_from_version_id,
            parameters_json=canonical_params,
            parameter_hash=param_hash,
            notes=(notes or "").strip() or None,
            created_by=actor,
            status=StrategyVersionStatus.VALIDATED.value,
            created_at=now_ist(),
        )
        session.add(new_row)
        session.flush()

        audit_action = (
            "STRATEGY_VERSION_CLONED"
            if cloned_from_version_id is not None
            else "STRATEGY_VERSION_CREATED"
        )
        summary_msg = (
            f"Cloned {cloned_label} -> immutable {version_label} (hash={param_hash[:12]})."
            if cloned_label
            else f"Created immutable strategy version {version_label} (hash={param_hash[:12]})."
        )
        AuditService.record(
            session,
            action=audit_action,
            summary=summary_msg,
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.STRATEGY,
            severity=AuditSeverity.INFO,
            entity_type="strategy_versions",
            entity_id=version_label,
            details={
                "version_id": str(new_row.id),
                "version_label": version_label,
                "strategy_code": clean_code,
                "instrument": instrument_sym,
                "timeframe": validated_cfg.timeframe.value,
                "version_number": next_ver,
                "parameter_hash": param_hash,
                "cloned_from_version_id": str(cloned_from_version_id)
                if cloned_from_version_id
                else None,
                "cloned_from_label": cloned_label,
                "parameters": canonical_params,
            },
        )
        return to_version_entity(new_row, clean_code, cloned_from_label=cloned_label)

    @classmethod
    def clone_version(
        cls,
        session: Session,
        *,
        source_version_id: UUID,
        parameter_overrides: dict[str, Any] | None,
        notes: str | None,
        actor: str,
    ) -> StrategyVersionEntity:
        """Clone an existing immutable version with optional parameter overrides into a new version."""
        source = session.get(StrategyVersionModel, source_version_id)
        if source is None:
            raise EntityNotFoundError(f"StrategyVersion {source_version_id} not found.")

        merged_params = dict(source.parameters_json or {})
        if parameter_overrides:
            merged_params.update(parameter_overrides)

        default_note = f"Cloned from {source.version_label}"
        final_notes = (notes or "").strip() or default_note

        return cls.create_version(
            session,
            strategy_code=source.definition.code,
            raw_parameters=merged_params,
            notes=final_notes,
            actor=actor,
            cloned_from_version_id=source.id,
        )

    @classmethod
    def compare_versions(
        cls,
        session: Session,
        *,
        left_version_id: UUID,
        right_version_id: UUID,
    ) -> dict[str, Any]:
        """Compute structured parameter-by-parameter diff between two immutable versions."""
        left = cls.get_version(session, left_version_id)
        right = cls.get_version(session, right_version_id)

        diffs: list[dict[str, Any]] = []
        changed_count = 0

        seen_keys: set[str] = set()
        for key, group, label in PARAMETER_METADATA:
            seen_keys.add(key)
            l_val = left.parameters.get(key)
            r_val = right.parameters.get(key)
            changed = str(l_val) != str(r_val)
            if changed:
                changed_count += 1
            diffs.append(
                {
                    "parameter": key,
                    "group": group,
                    "label": label,
                    "left_value": l_val,
                    "right_value": r_val,
                    "changed": changed,
                }
            )

        # Include any legacy/additional keys if present
        extra_keys = sorted((set(left.parameters.keys()) | set(right.parameters.keys())) - seen_keys)
        for key in extra_keys:
            l_val = left.parameters.get(key)
            r_val = right.parameters.get(key)
            changed = str(l_val) != str(r_val)
            if changed:
                changed_count += 1
            diffs.append(
                {
                    "parameter": key,
                    "group": "OTHER",
                    "label": key,
                    "left_value": l_val,
                    "right_value": r_val,
                    "changed": changed,
                }
            )

        return {
            "left_version": left,
            "right_version": right,
            "identical_hash": left.parameter_hash == right.parameter_hash,
            "changed_count": changed_count,
            "unchanged_count": len(diffs) - changed_count,
            "rows": diffs,
        }
