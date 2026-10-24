"""`agent.normalize_legacy_human_review` switch for the Human Review -> Done sweep."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from symphony.errors import ConfigValidationError
from symphony.orchestrator import Orchestrator
from symphony.workflow import ServiceConfig, WorkflowState
from symphony.workflow import build_service_config, parse_workflow_text

CARD = (
    "---\n"
    "id: MT-1\n"
    "title: Legacy card\n"
    "state: Human Review\n"
    "---\n"
    "## As-Is -> To-Be Report\n"
    "### As-Is\n- old\n### To-Be\n- new\n"
)


def _config(tmp_path: Path, agent_line: str = "") -> ServiceConfig:
    path = tmp_path / "WORKFLOW.md"
    agent = f"agent:\n  {agent_line}\n" if agent_line else ""
    text = (
        "---\n"
        "tracker:\n"
        "  kind: file\n"
        "  board_root: ./board\n"
        "  active_states: [Todo]\n"
        "  terminal_states: [Human Review, Done]\n"
        f"{agent}"
        "---\nprompt\n"
    )
    path.write_text(text, encoding="utf-8")
    return build_service_config(parse_workflow_text(text, path))


def _card(tmp_path: Path) -> Path:
    path = tmp_path / "board" / "MT-1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CARD, encoding="utf-8")
    return path


def _sweep(tmp_path: Path, cfg: ServiceConfig) -> int:
    orch = Orchestrator(WorkflowState(tmp_path / "WORKFLOW.md"))
    return asyncio.run(orch._auto_normalize_legacy_human_review_done(cfg))


@pytest.mark.parametrize("agent_line", ["", "normalize_legacy_human_review: true"])
def test_default_and_true_move_card_to_done(tmp_path, agent_line):
    cfg = _config(tmp_path, agent_line)
    card = _card(tmp_path)

    assert cfg.agent.normalize_legacy_human_review is True
    assert _sweep(tmp_path, cfg) == 1
    assert "state: Done" in card.read_text(encoding="utf-8")


def test_false_leaves_card_untouched(tmp_path):
    cfg = _config(tmp_path, "normalize_legacy_human_review: false")
    card = _card(tmp_path)

    assert _sweep(tmp_path, cfg) == 0

    assert card.read_text(encoding="utf-8") == CARD
    assert "state: Human Review" in card.read_text(encoding="utf-8")


def test_false_is_read_from_workflow_file(tmp_path):
    cfg = _config(tmp_path, "normalize_legacy_human_review: false")

    assert cfg.agent.normalize_legacy_human_review is False


@pytest.mark.parametrize("value", ['"false"', "0", "no_such_bool_text"])
def test_non_boolean_value_is_a_config_error(tmp_path, value):
    with pytest.raises(ConfigValidationError, match="normalize_legacy_human_review"):
        _config(tmp_path, f"normalize_legacy_human_review: {value}")
