"""`issue_budget_snapshot`: `state_turns`, `budget_exhausted`, `paused`, level limits."""
from __future__ import annotations

from dataclasses import replace

from symphony.orchestrator.entries import _IssueDebug
from symphony.workflow.levels import LevelConfig
from tests.test_orchestrator_dispatch import _install_running_entry, _issue, _make_config, _orch


def _setup(*, level: str | None = None, state: str = "In Progress"):
    cfg = _make_config()
    cfg = replace(
        cfg,
        agent=replace(cfg.agent, max_turns=30, max_state_turns=12),
        levels={"S1": LevelConfig(max_turns=8, max_state_turns=4)},
    )
    orch = _orch()
    orch._workflow_state._config = cfg
    issue = replace(_issue("SNAP-1", state=state), level=level)
    return orch, cfg, issue


def _rows(result):
    return {row["name"]: row for row in result["items"]}


async def test_state_turns_item_sits_after_attempt_turns():
    orch, _cfg, issue = _setup()
    result = await orch.issue_budget_snapshot(issue)
    names = [row["name"] for row in result["items"]]
    assert names[:2] == ["attempt_turns", "state_turns"]
    assert set(_rows(result)["state_turns"]) == {"name", "limit", "used", "remaining", "status"}


async def test_state_turns_same_state_uses_counter():
    orch, _cfg, issue = _setup()
    orch._issue_debug[issue.id] = _IssueDebug(state_turn_state="in progress", state_turn_count=5)
    row = _rows(await orch.issue_budget_snapshot(issue))["state_turns"]
    assert (row["limit"], row["used"], row["remaining"], row["status"]) == (12, 5, 7, "remaining")


async def test_state_turns_exhausted_when_counter_reaches_limit():
    orch, _cfg, issue = _setup()
    orch._issue_debug[issue.id] = _IssueDebug(state_turn_state="in progress", state_turn_count=12)
    row = _rows(await orch.issue_budget_snapshot(issue))["state_turns"]
    assert (row["remaining"], row["status"]) == (0, "exhausted")


async def test_state_turns_other_state_counts_as_zero():
    orch, _cfg, issue = _setup()
    orch._issue_debug[issue.id] = _IssueDebug(state_turn_state="verify", state_turn_count=9)
    row = _rows(await orch.issue_budget_snapshot(issue))["state_turns"]
    assert (row["limit"], row["used"], row["remaining"]) == (12, 0, 12)


async def test_state_turns_without_record_is_unknown():
    orch, _cfg, issue = _setup()
    row = _rows(await orch.issue_budget_snapshot(issue))["state_turns"]
    assert (row["limit"], row["used"], row["remaining"], row["status"]) == (12, None, None, "unknown")


async def test_state_turns_limit_follows_per_state_config():
    orch, cfg, issue = _setup()
    orch._workflow_state._config = replace(
        cfg, agent=replace(cfg.agent, max_state_turns_by_state={"in progress": 6})
    )
    row = _rows(await orch.issue_budget_snapshot(issue))["state_turns"]
    assert row["limit"] == 6


async def test_budget_exhausted_and_paused_flags():
    orch, _cfg, issue = _setup()
    result = await orch.issue_budget_snapshot(issue)
    assert result["budget_exhausted"] is False and result["paused"] is False
    orch._turn_budget_exhausted.add(issue.id)
    orch._paused_issue_ids.add(issue.id)
    result = await orch.issue_budget_snapshot(issue)
    assert result["budget_exhausted"] is True and result["paused"] is True
    assert result["state"] == "In Progress"


async def test_flags_present_for_running_ticket():
    orch, _cfg, issue = _setup()
    _install_running_entry(orch, issue)
    orch._turn_budget_exhausted.add(issue.id)
    result = await orch.issue_budget_snapshot(issue)
    assert result["budget_exhausted"] is True and result["paused"] is False


async def test_limits_come_from_ticket_level():
    orch, _cfg, issue = _setup(level="S1")
    rows = _rows(await orch.issue_budget_snapshot(issue))
    assert rows["attempt_turns"]["limit"] == 8
    assert rows["state_turns"]["limit"] == 4


async def test_ticket_without_level_uses_general_limits():
    orch, _cfg, issue = _setup(level=None)
    rows = _rows(await orch.issue_budget_snapshot(issue))
    assert (rows["attempt_turns"]["limit"], rows["state_turns"]["limit"]) == (30, 12)


async def test_broken_level_falls_back_to_general_limits(caplog):
    orch, _cfg, issue = _setup(level="NOPE")
    result = await orch.issue_budget_snapshot(issue)  # must not raise
    rows = _rows(result)
    assert (rows["attempt_turns"]["limit"], rows["state_turns"]["limit"]) == (30, 12)
    assert result["available"] is True


async def test_unavailable_workflow_keeps_old_shape():
    orch = _orch()
    result = await orch.issue_budget_snapshot(_issue("SNAP-2"))
    assert result == {"available": False, "items": []}
