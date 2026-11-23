"""Strategy Lab API endpoints: catalog, validation, immutable versioning, cloning, and diffing."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, status

from backend.schemas.api import (
    CloneStrategyVersionRequest,
    CreateStrategyVersionRequest,
    StrategyDefinitionOut,
    StrategyDiffRowOut,
    StrategyVersionComparisonOut,
    StrategyVersionOut,
    ValidateStrategyConfigOut,
    ValidateStrategyConfigRequest,
)
from core.config import get_settings
from core.persistence.seed import compute_parameter_hash
from core.persistence.uow import unit_of_work
from core.strategies.config_schema import default_ema_rsi_parameters
from core.strategies.registry import StrategyRegistryService

router = APIRouter(prefix="/api/v1/strategies", tags=["Strategies"])


@router.get("", response_model=list[StrategyDefinitionOut])
def list_strategies(
    instrument: str | None = Query(default=None, description="Optional symbol filter (e.g. NIFTY)"),
) -> list[StrategyDefinitionOut]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        pairs = StrategyRegistryService.list_definitions_with_versions(
            session, instrument=instrument
        )
        output: list[StrategyDefinitionOut] = []
        for s_def, versions in pairs:
            ver_dtos = [StrategyVersionOut(**v.__dict__) for v in versions]
            output.append(StrategyDefinitionOut(**s_def.__dict__, versions=ver_dtos))
        return output


@router.get("/defaults/{instrument}", response_model=dict)
def get_instrument_strategy_defaults(instrument: str) -> dict[str, object]:
    """Return neutral textbook baseline parameters for a given instrument."""
    return default_ema_rsi_parameters(instrument)


@router.get("/compare", response_model=StrategyVersionComparisonOut)
def compare_strategy_versions(
    left_id: UUID = Query(..., description="Left / baseline StrategyVersion UUID"),
    right_id: UUID = Query(..., description="Right / candidate StrategyVersion UUID"),
) -> StrategyVersionComparisonOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        diff = StrategyRegistryService.compare_versions(
            session, left_version_id=left_id, right_version_id=right_id
        )
        return StrategyVersionComparisonOut(
            left_version=StrategyVersionOut(**diff["left_version"].__dict__),
            right_version=StrategyVersionOut(**diff["right_version"].__dict__),
            identical_hash=diff["identical_hash"],
            changed_count=diff["changed_count"],
            unchanged_count=diff["unchanged_count"],
            rows=[StrategyDiffRowOut(**row) for row in diff["rows"]],
        )


@router.get("/versions/{version_id}", response_model=StrategyVersionOut)
def get_strategy_version(version_id: UUID) -> StrategyVersionOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        ver = StrategyRegistryService.get_version(session, version_id)
        return StrategyVersionOut(**ver.__dict__)


@router.post("/{strategy_code}/validate", response_model=ValidateStrategyConfigOut)
def validate_strategy_configuration(
    strategy_code: str, payload: ValidateStrategyConfigRequest
) -> ValidateStrategyConfigOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        validated = StrategyRegistryService.validate_config(
            session,
            strategy_code=strategy_code,
            raw_parameters=payload.parameters,
        )
        canonical = validated.to_canonical_dict()
        return ValidateStrategyConfigOut(
            valid=True,
            canonical_parameters=canonical,
            parameter_hash=compute_parameter_hash(canonical),
        )


@router.post(
    "/{strategy_code}/versions",
    response_model=StrategyVersionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_strategy_version(
    strategy_code: str, payload: CreateStrategyVersionRequest
) -> StrategyVersionOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        created = StrategyRegistryService.create_version(
            session,
            strategy_code=strategy_code,
            raw_parameters=payload.parameters,
            notes=payload.notes,
            actor=payload.actor,
        )
        return StrategyVersionOut(**created.__dict__)


@router.post(
    "/versions/{version_id}/clone",
    response_model=StrategyVersionOut,
    status_code=status.HTTP_201_CREATED,
)
def clone_strategy_version(
    version_id: UUID, payload: CloneStrategyVersionRequest
) -> StrategyVersionOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        cloned = StrategyRegistryService.clone_version(
            session,
            source_version_id=version_id,
            parameter_overrides=payload.parameter_overrides,
            notes=payload.notes,
            actor=payload.actor,
        )
        return StrategyVersionOut(**cloned.__dict__)
