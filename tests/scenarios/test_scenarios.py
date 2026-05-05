"""Pytest-обёртка для LLM-as-judge сценариев.

По умолчанию пропускается (нужен реальный ANTHROPIC_API_KEY и стоит токенов).
Запуск:
    RUN_LLM_SCENARIOS=1 .venv/bin/python -m pytest tests/scenarios/ -v -s
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from .judge import judge
from .runner import run_scenario


SCENARIOS_FILE = Path(__file__).parent / "scenarios.yaml"


def _load_scenarios() -> list[dict]:
    if not SCENARIOS_FILE.exists():
        return []
    data = yaml.safe_load(SCENARIOS_FILE.read_text(encoding="utf-8"))
    return data or []


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LLM_SCENARIOS") != "1",
    reason="LLM scenarios skipped (set RUN_LLM_SCENARIOS=1 to run; стоит токенов)",
)


@pytest.fixture
def scenario_vault(tmp_path: Path) -> Path:
    """Минимальный vault для каждого сценария — обычные подпапки + Patterns."""
    for folder in ("Daily", "Inbox", "People", "Projects", "Weekly", "System", "Patterns"):
        (tmp_path / folder).mkdir()
    return tmp_path


@pytest.mark.parametrize(
    "scenario",
    _load_scenarios(),
    ids=lambda s: s.get("name", "?"),
)
async def test_scenario(scenario: dict, scenario_vault: Path, capsys) -> None:
    transcript = await run_scenario(scenario, vault_path=scenario_vault)
    result = await judge(scenario, transcript)

    # Печатаем подробный отчёт в stdout — видно при -s.
    print(f"\n\n=== SCENARIO: {scenario.get('name')} ===")
    print(transcript.render())
    print(result.render())

    assert result.passed, (
        f"Scenario '{scenario.get('name')}' failed:\n"
        + result.render()
        + "\n\nTranscript:\n"
        + transcript.render()
    )
