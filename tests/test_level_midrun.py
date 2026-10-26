"""A card's `level` broken between turns must not kill the watchdog or the worker."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import symphony.orchestrator.retry_reconcile as retry_reconcile_module
from symphony.orchestrator.agent_phase import _AgentPhaseState
from symphony.orchestrator.core import Orchestrator
from symphony.workflow import ServiceConfig

from tests.test_orchestrator_dispatch import (
    _install_running_entry,
    _orch,
)
from tests.test_task_levels import _board_cfg, _tick, _with_level


class _Log:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def __getattr__(self, level: str) -> Any:
        def _rec(message: str, **fields: Any) -> None:
            self.records.append((level, message, fields))

        return _rec

    def count(self, message: str) -> int:
        return sum(1 for _, m, _f in self.records if m == message)


def _cfg(tmp_path: Path) -> ServiceConfig:
    return _board_cfg(tmp_path)


def test_stall_timeout_survives_a_broken_level_and_logs_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log = _Log()
    monkeypatch.setattr(retry_reconcile_module, "log", log)
    orch = _orch()
    cfg = _cfg(tmp_path)
    entry = _install_running_entry(orch, _with_level("T-1", "S9", "In Progress"))

    expected = cfg.claude.stall_timeout_ms
    first = orch._stall_timeout_ms_for_entry(cfg, entry)
    for _ in range(3):
        assert orch._stall_timeout_ms_for_entry(cfg, entry) == first
    assert first == cfg.agent.stall_timeout_ms_for_state("In Progress", expected)
    assert log.count("level_unusable_for_stall") == 1


def test_stall_timeout_without_level_problem_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log = _Log()
    monkeypatch.setattr(retry_reconcile_module, "log", log)
    orch = _orch()
    entry = _install_running_entry(orch, _with_level("T-1", "S1", "In Progress"))
    orch._stall_timeout_ms_for_entry(_cfg(tmp_path), entry)
    assert log.count("level_unusable_for_stall") == 0


async def test_stall_watchdog_still_fires_with_a_broken_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    orch = _orch()
    cfg = _cfg(tmp_path)
    cfg = replace(cfg, claude=replace(cfg.claude, stall_timeout_ms=1000))
    issue = _with_level("T-1", "S9", "In Progress")
    task = asyncio.create_task(asyncio.sleep(60))
    entry = _install_running_entry(orch, issue)
    entry.worker_task = task
    entry.agent_kind = "claude"
    entry.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(orch, "_tracker_call_states_by_ids", lambda _c, _ids: [issue])

    await orch._reconcile_running(cfg)
    try:
        assert entry.cancelled_at is not None
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
    finally:
        task.cancel()


async def test_phase_transition_with_a_broken_level_blocks_the_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    orch = _orch()
    cfg = _cfg(tmp_path)
    issue = _with_level("T-1", "S9", "In Progress")
    entry = _install_running_entry(orch, issue)
    notes: list[tuple[str, str, str]] = []
    moved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        Orchestrator,
        "_tracker_call_append_note",
        staticmethod(lambda c, i, heading, body: notes.append((i.identifier, heading, body))),
    )
    monkeypatch.setattr(
        Orchestrator,
        "_tracker_call_update_state",
        staticmethod(lambda c, i, target: moved.append((i.identifier, target))),
    )
    state = _AgentPhaseState(
        issue=issue,
        cfg=cfg,
        client=object(),  # type: ignore[arg-type]
        first_prompt="",
        current_state="in progress",
        known_app_release=False,
    )

    result = await orch._transition_agent_phase(
        phase_state=state,
        running_issue_id=issue.id,
        base_cfg=cfg,
        workspace_path=tmp_path,
        attempt=None,
        doc_language="en",
        producing_state="todo",
        producing_state_raw="Todo",
        turn_number=1,
    )

    assert result is None
    assert moved == [("T-1", "Blocked")]
    [(identifier, heading, body)] = notes
    assert identifier == "T-1" and heading == "Level" and "level 'S9'" in body
    assert entry.issue.state == "Blocked"


def test_start_with_a_broken_level_still_blocks_before_any_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dispatched, notes, moved = _tick(
        monkeypatch, tmp_path, _with_level("T-2", "S9"), _cfg(tmp_path)
    )
    assert dispatched == []
    assert moved == [("T-2", "Blocked")]
    assert [(i, h) for i, h, _ in notes] == [("T-2", "Level")]
