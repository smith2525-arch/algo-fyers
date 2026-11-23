"""Guided Consulting Demo & Deterministic Showcase API endpoints (`/api/v1/demo`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.config import get_settings
from core.demo.service import GuidedDemoService
from core.persistence.uow import unit_of_work

router = APIRouter(prefix="/api/v1/demo", tags=["Guided Consulting Demo"])


class DemoActionRequest(BaseModel):
    actor: str = Field(default="consulting.presenter", min_length=2, max_length=64)


@router.get("/status")
def get_guided_demo_status() -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return GuidedDemoService.get_demo_status(session, settings=cfg)


@router.post("/run-showcase")
def run_deterministic_demo_showcase(
    payload: DemoActionRequest | None = None,
) -> dict[str, Any]:
    cfg = get_settings()
    eff_actor = payload.actor if payload else "consulting.presenter"
    with unit_of_work(settings=cfg) as session:
        return GuidedDemoService.run_full_demo_showcase(
            session, actor=eff_actor, settings=cfg
        )


@router.post("/drill/{drill_name}")
def trigger_guided_demo_drill(
    drill_name: str,
    payload: DemoActionRequest | None = None,
) -> dict[str, Any]:
    cfg = get_settings()
    eff_actor = payload.actor if payload else "consulting.presenter"
    with unit_of_work(settings=cfg) as session:
        return GuidedDemoService.trigger_demo_drill(
            session, drill_name, actor=eff_actor, settings=cfg
        )
