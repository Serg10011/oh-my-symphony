"""Tests for S4-15: consistent limits in project.toml and the needs_human
workspace kept by the core.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the kit is POSIX-only")

OURS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OURS / "hooks"))

import project_config as pc  # noqa: E402
from test_project_kit import PREFIX, Kit  # noqa: E402

BASE = {
    "project": {"name": "x", "repo": "/r", "base_branch": "dev", "id_prefix": "XX", "port": 9000},
    "tools": {"python": "/p", "symphony": "/s", "claude": "/c/claude"},
}


def data(**sections: dict) -> dict:
    return {**BASE, **sections}


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    return Kit(tmp_path)


# ---------------------------------------------------------------------------
# max_turns must cover max_attempts returns
# ---------------------------------------------------------------------------


def test_max_turns_below_the_formula_is_refused_with_an_explanation() -> None:
    with pytest.raises(pc.ProjectError) as caught:
        pc.validate_project(data(limits={"max_turns": 8, "max_attempts": 2, "max_total_turns": 20}))
    message = str(caught.value)
    assert "[limits] max_turns = 8 меньше 4 × (1 + max_attempts) = 12" in message
    assert "не успеет дойти до Human Review" in message
    assert "увеличь max_turns до 12 или уменьши max_attempts" in message


def test_max_turns_on_the_formula_is_accepted() -> None:
    limits = pc.validate_project(
        data(limits={"max_turns": 12, "max_attempts": 2, "max_total_turns": 20})
    )["limits"]
    assert (limits["max_turns"], limits["max_attempts"]) == (12, 2)


def test_unlimited_returns_keep_the_stage_count_rule() -> None:
    # project.toml itself takes max_attempts >= 1 (test_project_doctor), so the
    # `0 = unlimited` branch of the core is checked on the helper directly.
    pc._check_turns_cover_attempts("[limits] max_turns", 4, 0)
    pc._check_turns_cover_attempts("[levels.local] max_turns", 4, 0)
    with pytest.raises(pc.ProjectError, match="max_turns = 3 меньше 4 × \\(1 \\+ max_attempts\\) = 8"):
        pc._check_turns_cover_attempts("[limits] max_turns", 3, 1)


def test_level_max_turns_follows_the_same_formula_and_names_the_level() -> None:
    limits = {"max_turns": 12, "max_attempts": 2, "max_total_turns": 20}
    with pytest.raises(pc.ProjectError) as caught:
        pc.validate_project(data(limits=limits, levels={"local": {"max_turns": 8}}))
    message = str(caught.value)
    assert "[levels.local] max_turns = 8 меньше 4 × (1 + max_attempts) = 12" in message
    out = pc.validate_project(data(limits=limits, levels={"local": {"max_turns": 12}}))
    assert out["levels"]["levels"]["local"]["max_turns"] == 12


def test_max_total_turns_must_still_cover_max_turns() -> None:
    with pytest.raises(pc.ProjectError, match="max_total_turns меньше max_turns"):
        pc.validate_project(data(limits={"max_turns": 12, "max_attempts": 2, "max_total_turns": 11}))


def test_stub_limits_pass_the_loader_and_the_formula(kit: Kit) -> None:
    assert kit.new().returncode == 0
    limits = pc.load_project(kit.board)["limits"]
    assert limits["max_turns"] == pc.ACTIVE_STATE_COUNT * (1 + limits["max_attempts"]) == 12
    assert limits["max_total_turns"] >= limits["max_turns"]
    text = (kit.board / "project.toml").read_text()
    assert "штатного сброса нет" not in text
    assert "reset-budget" in text


def test_stub_levels_describe_task_complexity(kit: Kit) -> None:
    assert kit.new().returncode == 0
    text = (kit.board / "project.toml").read_text()
    assert "Уровень описывает задачу" in text
    for old in ("L1", "L2", "S1", "S2", "дешёвый работник"):
        assert old not in text
    card = (kit.board / "card-template.md").read_text()
    assert all(old not in card for old in ("L1", "L2", "S1", "S2"))


# ---------------------------------------------------------------------------
# needs_human workspaces survive the service restart
# ---------------------------------------------------------------------------


def _core_cfg(board: Path):
    from symphony.workflow import build_service_config, load_workflow

    return build_service_config(load_workflow(board / "WORKFLOW.md"))


def test_rendered_workflow_preserves_needs_human_in_the_core_form(kit: Kit) -> None:
    assert kit.new().returncode == 0
    states = _core_cfg(kit.board).agent.preserve_workspace_states
    # The core compares lower-cased states.
    assert "needs_human" in states and "human review" in states


def test_startup_cleanup_keeps_the_needs_human_workspace(kit: Kit, monkeypatch) -> None:
    from symphony.orchestrator import Orchestrator
    from symphony.workflow import WorkflowState
    from symphony.workspace import WorkspaceManager

    assert kit.new().returncode == 0
    cfg = _core_cfg(kit.board)
    ident = f"{PREFIX}-7"
    card = kit.board / "kanban" / f"{ident}.md"
    card.write_text(
        f"---\nid: {ident}\ntitle: Keep me\nstate: needs_human\n---\nbody\n", encoding="utf-8"
    )
    orch = Orchestrator(WorkflowState(cfg.workflow_path))
    monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
    manager = WorkspaceManager(
        cfg.workspace_root, cfg.hooks, workflow_dir=cfg.workflow_path.parent
    )
    orch._workspace_manager = manager
    workspace = manager.path_for(ident)
    workspace.mkdir(parents=True)

    asyncio.run(orch._startup_terminal_cleanup(cfg))

    assert workspace.is_dir()
