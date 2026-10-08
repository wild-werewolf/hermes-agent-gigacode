"""Shared helpers: the fake CLI scenario writer and the injected test-only DirectDriver."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent.gigacode.driver import DirectDriver

FAKE_CLI = Path(__file__).parent / "fake_gigacode.py"


def success_steps(text: str = "Готово.", usage: dict | None = None) -> list[dict[str, Any]]:
    return [
        {"event": {"type": "system", "subtype": "init", "model": "fixture-model", "session_id": "fake-session"}},
        {"event": {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}},
        {"event": {"type": "result", "subtype": "success", "is_error": False, "result": text,
                   "usage": usage or {"input_tokens": 11, "output_tokens": 3}}},
    ]


@pytest.fixture
def scenario(tmp_path):
    """``scenario(**spec) -> path``: write a fake-CLI scenario file."""
    counter = {"n": 0}

    def write(**spec: Any) -> Path:
        counter["n"] += 1
        path = tmp_path / f"scenario-{counter['n']}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path
    return write


def direct_driver(scenario_path: Path, grace: float = 2.0) -> DirectDriver:
    return DirectDriver([sys.executable, "-I", str(FAKE_CLI)], grace_seconds=grace,
                        extra_env={"FAKE_GIGACODE_SCENARIO": str(scenario_path)})
