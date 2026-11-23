"""CLI utility to populate the deterministic consulting demo workspace.

Usage:
    python -m demo_data.run_guided_demo

Executes strictly through the real Backtest, Market Replay, Strategy, Risk Engine,
OMS, Paper Broker, Position, and Reconciliation services (PAPER_ONLY).
"""

from __future__ import annotations

import json

from core.config import get_settings
from core.demo.service import GuidedDemoService
from core.persistence.uow import unit_of_work


def main() -> None:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        result = GuidedDemoService.run_full_demo_showcase(
            session, actor="cli.demo_setup", settings=cfg
        )
    print(
        json.dumps(
            {
                "showcase_executed": result["showcase_executed"],
                "backtest_run_code": result["backtest_run_code"],
                "completed_steps": f"{result['status']['completed_steps']}/{result['status']['total_steps']}",
                "showcase_artifacts": result["status"]["showcase_artifacts"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
