"""`needs_human` is a failed terminal state for `blocked_by`; confirm_done refusal texts."""

from __future__ import annotations

import asyncio

import pytest

import symphony.orchestrator.core as core_module
from symphony.issue import BlockerRef
from symphony.orchestrator.core_support import _blocker_dependency_is_resolved
from symphony.utils.auto_merge import AutoMergeResult
from tests.test_done_gate import IDENT, _config, _gate_config, _setup, _state
from tests.test_orchestrator_dispatch import _issue, _make_config, _orch

_TERMINALS = ("Human Review", "needs_human", "Done", "Blocked", "Archive")


@pytest.mark.parametrize(
    ("blocker_state", "dispatchable"),
    [
        ("needs_human", False),
        ("Needs_Human", False),
        ("Blocked", False),
        ("Human Review", False),
        ("Done", True),
    ],
)
def test_todo_card_waits_unless_blocker_is_done(blocker_state, dispatchable):
    cfg = _make_config(terminal_states=_TERMINALS)
    blocker = BlockerRef(id="z", identifier="MT-9", state=blocker_state)
    issue = _issue("MT-1", state="Todo", blocked_by=(blocker,))

    assert _orch()._should_dispatch(issue, cfg) is dispatchable
    assert _blocker_dependency_is_resolved(blocker_state, cfg) is dispatchable


# confirm_done refusal text: the done gate reports its own reason, the merge gate keeps its text.
def test_confirm_done_refusal_by_done_gate_reports_its_reason(tmp_path, monkeypatch):
    cfg = _gate_config(tmp_path, "exit 2")
    orch, *_ = _setup(tmp_path, cfg, monkeypatch, state="Human Review")

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(orch.confirm_done(IDENT))

    message = str(excinfo.value)
    assert message.startswith("done gate failed (exit_code)")
    assert "merge gate" not in message
    assert _state(tmp_path) == "Blocked"


def test_confirm_done_refusal_by_merge_gate_keeps_old_text(tmp_path, monkeypatch):
    cfg = _config(tmp_path, "auto_merge_on_done: true")
    orch, *_ = _setup(tmp_path, cfg, monkeypatch, state="Human Review")

    async def _merge(**_kwargs):
        return AutoMergeResult(ok=False, status="conflict", detail="x")

    monkeypatch.setattr(core_module, "auto_merge_on_done_best_effort", _merge)

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(orch.confirm_done(IDENT))

    assert str(excinfo.value) == "merge gate failed; workspace preserved; inspect the ticket"
    assert _state(tmp_path) == "Blocked"


def test_confirm_done_ignores_stale_last_error_on_merge_gate_refusal(tmp_path, monkeypatch):
    cfg = _config(tmp_path, "auto_merge_on_done: true")
    orch, issue, *_ = _setup(tmp_path, cfg, monkeypatch, state="Human Review")
    orch._issue_debug.setdefault(issue.id, core_module._IssueDebug()).last_error = "old failure"

    async def _merge(**_kwargs):
        return AutoMergeResult(ok=False, status="conflict", detail="x")

    monkeypatch.setattr(core_module, "auto_merge_on_done_best_effort", _merge)

    with pytest.raises(ValueError, match="merge gate failed"):
        asyncio.run(orch.confirm_done(IDENT))
