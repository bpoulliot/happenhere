"""Item 20: the example deployment runs an init as PID 1, which reaps Chromium's orphans."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_example_compose_runs_an_init():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["happenhere"]
    assert service.get("init") is True, "uvicorn as PID 1 never reaps orphaned Chromium processes"
