"""Tests for S4-18: the states `Plan` (active, the manager slices a task) and
`Subtasks` (terminal for the core, the task card waits for its subtasks).

The board is rendered by project-new, loaded by the core, the prompts are
rendered by the core engine.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the kit is POSIX-only")

OURS = Path(__file__).resolve().parents[1]
PROMPTS = OURS / "prompts"
sys.path.insert(0, str(OURS / "hooks"))

import project_config as pc  # noqa: E402
from test_project_kit import PREFIX, Kit  # noqa: E402

OLD_ACTIVE = ("Todo", "In Progress", "Verify", "Document")
NEW_ACTIVE = ("Todo", "Plan", "In Progress", "Verify", "Document")

BASE = {
    "project": {"name": "x", "repo": "/r", "base_branch": "dev", "id_prefix": "XX", "port": 9000},
    "tools": {"python": "/p", "symphony": "/s", "claude": "/c/claude"},
}


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    kit = Kit(tmp_path)
    assert kit.new().returncode == 0
    return kit


def core_cfg(board: Path):
    from symphony.workflow import build_service_config, load_workflow

    return build_service_config(load_workflow(board / "WORKFLOW.md"))


def issue(state: str, ident: str = f"{PREFIX}-2", **fields):
    from symphony.issue import Issue

    return Issue(id=ident, identifier=ident, title="T", description="d", priority=None,
                 state=state, **fields)


# ---------------------------------------------------------------------------
# the rendered WORKFLOW.md as the core reads it
# ---------------------------------------------------------------------------


def test_the_core_accepts_the_rendered_workflow_with_the_new_states(kit: Kit) -> None:
    cfg = core_cfg(kit.board)
    assert cfg.tracker.active_states == NEW_ACTIVE
    assert cfg.tracker.terminal_states == (
        "Human Review", "needs_human", "Subtasks", "Done", "Blocked", "Archive",
    )
    assert cfg.prompts.stage_paths["plan"] == PROMPTS / "stages" / "plan.md"
    assert cfg.prompts.stage_paths["todo"] == PROMPTS / "stages" / "todo.md"
    assert cfg.agent.preserve_workspace_states == ("human review", "needs_human", "subtasks")


def test_the_board_is_created_in_todo_and_moves_to_the_new_states(kit: Kit) -> None:
    from symphony.cli.board import main

    workflow = str(kit.board / "WORKFLOW.md")
    card = kit.board / "kanban" / f"{PREFIX}-5.md"
    assert main(["new", "--workflow", workflow, f"{PREFIX}-5", "Fresh"]) == 0
    assert "state: Todo" in card.read_text(encoding="utf-8")
    for state in ("Plan", "Subtasks", "Plan"):
        assert main(["mv", "--workflow", workflow, f"{PREFIX}-5", state]) == 0
        assert f"state: {state}" in card.read_text(encoding="utf-8")
    # The main path of a manager card: created straight in Plan.
    assert main(["new", "--workflow", workflow, f"{PREFIX}-6", "Task", "--state", "Plan",
                 "--label", "role:manager-plan"]) == 0
    assert "state: Plan" in (kit.board / "kanban" / f"{PREFIX}-6.md").read_text(encoding="utf-8")


def test_the_lower_bound_of_max_turns_is_the_number_of_active_states(kit: Kit) -> None:
    cfg = core_cfg(kit.board)
    assert pc.ACTIVE_STATE_COUNT == len(cfg.tracker.active_states) == 5
    assert pc.STAGES_ON_LONGEST_PATH == len(OLD_ACTIVE)


# ---------------------------------------------------------------------------
# stage contracts
# ---------------------------------------------------------------------------


def test_contracts_are_on_and_plan_has_none(kit: Kit) -> None:
    from symphony.orchestrator.contracts import evaluate_contract
    from symphony.workflow.presets import board_uses_shipped_contracts

    agent = core_cfg(kit.board).agent
    assert agent.stage_contracts == "on"
    assert agent.stage_contracts_enabled(NEW_ACTIVE)
    # Why not `auto`: Plan is not a stock lane, auto would switch contracts off.
    assert not board_uses_shipped_contracts(NEW_ACTIVE)
    assert board_uses_shipped_contracts(OLD_ACTIVE)
    result = evaluate_contract("Plan", "", f"{PREFIX}-2", preset=None)
    assert result.passed and not result.missing


def test_the_other_stages_keep_the_contract_set_they_had() -> None:
    from symphony.orchestrator.contracts import contract_producing_states, evaluate_contract
    from symphony.workflow.presets import guess_lane_preset

    old, new = guess_lane_preset(OLD_ACTIVE), guess_lane_preset(NEW_ACTIVE)
    # The new set matches no preset (the core then uses the default contracts).
    assert (old, new) == ("default", None)
    assert contract_producing_states(new) == contract_producing_states(old)
    assert "plan" not in contract_producing_states(new)
    for stage in ("In Progress", "Verify", "Document", "Done", "Todo"):
        before = evaluate_contract(stage, "", f"{PREFIX}-2", preset=old)
        after = evaluate_contract(stage, "", f"{PREFIX}-2", preset=new)
        assert (after.passed, after.missing) == (before.passed, before.missing), stage
    # The enforced ones still demand something from an empty card.
    assert not evaluate_contract("Verify", "", f"{PREFIX}-2", preset=new).passed


# ---------------------------------------------------------------------------
# dependencies and target states
# ---------------------------------------------------------------------------


def test_only_done_resolves_a_dependency(kit: Kit) -> None:
    from symphony.orchestrator.core_support import _blocker_dependency_is_resolved as resolved

    cfg = core_cfg(kit.board)
    assert cfg.agent.dependency_resolving_states == ("done",)
    assert resolved("Done", cfg)
    for state in ("Subtasks", "Plan", "Human Review", "needs_human", "Blocked", "Archive"):
        assert not resolved(state, cfg), state


def test_without_the_key_subtasks_would_count_as_done(kit: Kit) -> None:
    from symphony.orchestrator.core_support import _blocker_dependency_is_resolved as resolved

    cfg = core_cfg(kit.board)
    legacy = dataclasses.replace(
        cfg, agent=dataclasses.replace(cfg.agent, dependency_resolving_states=())
    )
    # The old statuses behave the same with and without the key ...
    assert resolved("Done", legacy)
    for state in ("Plan", "Human Review", "needs_human", "Blocked", "Archive"):
        assert not resolved(state, legacy), state
    # ... but Subtasks is "done" for the legacy rule: that is what the key prevents.
    assert resolved("Subtasks", legacy)
    assert not resolved("Subtasks", cfg)


def test_the_hand_off_and_rewind_states_do_not_change(kit: Kit) -> None:
    from symphony.orchestrator.helpers import _human_review_target_state, _rewind_budget_target_state

    cfg = core_cfg(kit.board)
    assert _human_review_target_state(cfg) == "Human Review"
    assert _rewind_budget_target_state(cfg) == "Blocked"


# ---------------------------------------------------------------------------
# the dispatcher
# ---------------------------------------------------------------------------


def make_orchestrator(cfg, monkeypatch):
    from symphony.orchestrator import Orchestrator
    from symphony.workflow import WorkflowState

    orch = Orchestrator(WorkflowState(cfg.workflow_path))
    monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
    return orch


def test_a_card_in_plan_is_taken_and_a_card_in_subtasks_is_not(kit: Kit, monkeypatch) -> None:
    from symphony.orchestrator.core_support import _EligibilityDisposition as Disposition

    cfg = core_cfg(kit.board)
    orch = make_orchestrator(cfg, monkeypatch)
    assert orch._eligibility_contract_decision(issue("Plan"), cfg) is None
    refused = orch._eligibility_contract_decision(issue("Subtasks"), cfg)
    assert refused is not None
    assert (refused.disposition, refused.code) == (Disposition.REJECT, "inactive")
    ready = orch._eligibility_decision(issue("Plan"), cfg, owning_retry=False)
    assert ready.disposition == Disposition.READY


def test_a_card_waits_for_a_blocker_in_subtasks_until_it_is_done(kit: Kit, monkeypatch) -> None:
    from symphony.issue import BlockerRef
    from symphony.orchestrator.core_support import _EligibilityDisposition as Disposition

    cfg = core_cfg(kit.board)
    orch = make_orchestrator(cfg, monkeypatch)

    def decision(blocker_state: str):
        blocker = BlockerRef(id=f"{PREFIX}-1", identifier=f"{PREFIX}-1", state=blocker_state)
        return orch._eligibility_decision(
            issue("In Progress", blocked_by=(blocker,)), cfg, owning_retry=False
        )

    for state in ("Plan", "Subtasks", "Human Review"):
        waiting = decision(state)
        assert (waiting.disposition, waiting.code) == (
            Disposition.WAIT_NON_SLOT, "waiting_dependency"), state
    assert decision("Done").disposition == Disposition.READY


# ---------------------------------------------------------------------------
# the workspace of a Subtasks card survives a service restart
# ---------------------------------------------------------------------------


def test_startup_cleanup_keeps_the_workspace_of_a_subtasks_card(kit: Kit, monkeypatch) -> None:
    from symphony.workspace import WorkspaceManager

    cfg = core_cfg(kit.board)
    ident = f"{PREFIX}-7"
    (kit.board / "kanban" / f"{ident}.md").write_text(
        f"---\nid: {ident}\ntitle: Wait for subtasks\nstate: Subtasks\n---\nbody\n",
        encoding="utf-8",
    )
    orch = make_orchestrator(cfg, monkeypatch)
    manager = WorkspaceManager(cfg.workspace_root, cfg.hooks, workflow_dir=cfg.workflow_path.parent)
    orch._workspace_manager = manager
    workspace = manager.path_for(ident)
    workspace.mkdir(parents=True)

    asyncio.run(orch._startup_terminal_cleanup(cfg))

    assert workspace.is_dir()


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


def render_prompt(name: str, *, state: str = "Todo", labels: tuple[str, ...] = (),
                  request: str | None = None) -> str:
    from symphony.prompt import build_prompt_env, render

    card = issue(state, labels=labels, request=request)
    values = build_prompt_env(card, None, auto_merge_on_done=False, max_attempts=2)
    return render((PROMPTS / name).read_text(encoding="utf-8"), values)


def test_todo_routes_a_manager_card_to_plan_and_only_that_one() -> None:
    manager = render_prompt("stages/todo.md", labels=("role:manager-plan",))
    assert "this replaces step 3" in manager
    assert "set state to `Plan`" in manager
    for labels in ((), ("role:worker",), ("bug",)):
        plain = render_prompt("stages/todo.md", labels=labels)
        assert "this replaces step 3" not in plain
        assert "`Plan`" not in plain
        assert "routing to In Progress" in plain  # step 3 stays for everyone


def test_base_shows_the_request_and_the_manager_path() -> None:
    with_request = render_prompt("base.md", labels=("role:worker",), request="REQ-9")
    assert "Request: REQ-9" in with_request
    assert "Request:" not in render_prompt("base.md", labels=("role:worker",))
    assert "manager (role:manager-plan): Plan -> Subtasks -> Verify -> Document -> Human Review" in with_request


def test_the_plan_placeholder_renders_and_hands_the_card_to_a_human() -> None:
    text = render_prompt("stages/plan.md", state="Plan", labels=("role:manager-plan",))
    assert "needs_human" in text
    assert "Plan stage is not configured yet (S4-20)" in text


# ---------------------------------------------------------------------------
# project_config: two different numbers instead of one
# ---------------------------------------------------------------------------


def data(**sections: dict) -> dict:
    return {**BASE, **sections}


def test_the_numbers_of_the_oms_board_still_pass() -> None:
    out = pc.validate_project(data(limits={"max_turns": 12, "max_attempts": 2, "max_total_turns": 20}))
    assert (out["limits"]["max_turns"], out["limits"]["max_attempts"]) == (12, 2)


def test_max_turns_below_the_number_of_active_states_is_refused_with_that_number() -> None:
    # Before S4-18 this passed the loader (4 was the bound) and the core refused it.
    with pytest.raises(pc.ProjectError) as caught:
        pc.validate_project(data(limits={"max_turns": 4, "max_attempts": 2, "max_total_turns": 20}))
    message = str(caught.value)
    assert "[limits] max_turns не меньше числа активных статусов доски (5)" in message
    assert "иначе ядро не примет WORKFLOW.md" in message
    pc.validate_project(data(limits={"max_turns": 12, "max_attempts": 2, "max_total_turns": 20}))


def test_a_level_with_max_turns_below_the_number_of_active_states_is_refused() -> None:
    limits = {"max_turns": 12, "max_attempts": 2, "max_total_turns": 20}
    with pytest.raises(pc.ProjectError) as caught:
        pc.validate_project(data(limits=limits, levels={"local": {"max_turns": 4}}))
    message = str(caught.value)
    assert "[levels.local]" in message and "(5)" in message
    out = pc.validate_project(data(limits=limits, levels={"local": {"max_turns": 12}}))
    assert out["levels"]["levels"]["local"]["max_turns"] == 12


def test_the_attempts_rule_names_the_stages_on_the_longest_path() -> None:
    with pytest.raises(pc.ProjectError) as caught:
        pc.validate_project(data(limits={"max_turns": 8, "max_attempts": 2, "max_total_turns": 20}))
    message = str(caught.value)
    assert "4 × (1 + max_attempts) = 12" in message
    assert "этапов на самом длинном пути" in message
