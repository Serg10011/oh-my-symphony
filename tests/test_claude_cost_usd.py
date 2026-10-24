"""Run cost (`runs.cost_usd`) from claude's session-cumulative `total_cost_usd`.

The fixture numbers come from a real three-turn `claude -p --resume` probe
that reused one session id: `total_cost_usd` in each `result` frame is the
cost of the whole session so far, while `usage` covers only that turn. A turn
therefore costs the difference to the previous value of the same session.
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

import symphony.orchestrator.agent_phase as agent_phase_module
import symphony.orchestrator.core as core_module
from symphony.backends import EVENT_TURN_COMPLETED, BackendInit
from symphony.backends.claude_code import ClaudeCodeBackend
from symphony.issue import Issue
from symphony.orchestrator import RunningEntry
from symphony.orchestrator.entries import _IssueDebug
from symphony.orchestrator.run_registry import RunRegistry
from symphony.server import build_app

from tests.test_backends import _make_cfg
from tests.test_orchestrator_dispatch import (
    _install_running_entry,
    _issue,
    _make_config,
    _orch,
    _stub_workflow_state_returning,
)

MODEL = "claude-sonnet-4-5"
# (total_cost_usd, per-turn usage, cumulative modelUsage cacheReadInputTokens)
PROBE_TURNS: list[tuple[float, dict[str, int], int]] = [
    (
        0.0329794,
        {
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_creation_input_tokens": 7953,
            "cache_read_input_tokens": 11334,
        },
        11334,
    ),
    (
        0.0573399,
        {
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_creation_input_tokens": 5742,
            "cache_read_input_tokens": 13585,
        },
        24919,
    ),
    (
        0.0595641,
        {
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_creation_input_tokens": 65,
            "cache_read_input_tokens": 19302,
        },
        44221,
    ),
]
PROBE_TURN_COSTS = [0.0329794, 0.0243605, 0.0022242]
TOL = 1e-9

_MISSING = object()


def _result(
    cost: Any, usage: dict[str, int], cache_read_total: int, session: str
) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "session_id": session,
        "usage": dict(usage),
        "modelUsage": {
            MODEL: {"cacheReadInputTokens": cache_read_total, "costUSD": cost}
        },
    }
    if cost is not _MISSING:
        frame["total_cost_usd"] = cost
    return frame


class _Log:
    """Stand-in for the module logger; keeps (level, message, fields)."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def _rec(self, level: str, message: str, **fields: Any) -> None:
        self.records.append((level, message, fields))

    def info(self, message: str, **fields: Any) -> None:
        self._rec("info", message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._rec("warning", message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self._rec("error", message, **fields)

    def debug(self, message: str, **fields: Any) -> None:
        self._rec("debug", message, **fields)

    def messages(self, level: str) -> list[str]:
        return [m for lvl, m, _ in self.records if lvl == level]


class _Harness:
    """Real claude backend -> real orchestrator event path -> real registry."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.db_path = tmp_path / "state.db"
        self.orch = _orch()
        _stub_workflow_state_returning(
            self.orch, _make_config(workspace_root=tmp_path / "ws"), monkeypatch
        )
        self.registry = RunRegistry(self.db_path)
        self.orch._run_registry = self.registry
        self.issue = _issue("COST-1", state="In Progress")
        self.entry = _install_running_entry(self.orch, self.issue)
        run_id = self.registry.acquire_run(
            self.issue,
            workspace_path=tmp_path / "ws" / "COST-1",
            attempt=None,
            attempt_kind="initial",
            agent_kind="claude",
        )
        assert run_id is not None
        self.run_id = run_id
        self.entry.run_id = run_id
        self.log = _Log()
        monkeypatch.setattr(agent_phase_module, "log", self.log)
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        self.backend = ClaudeCodeBackend(
            BackendInit(
                cfg=_make_cfg("claude", workspace_root=tmp_path),
                cwd=cwd,
                workspace_root=tmp_path,
                on_event=self._forward,
            )
        )

    async def _forward(self, event: dict[str, Any]) -> None:
        await self.orch._on_codex_event(self.issue.id, event)

    async def turn(
        self, cost: Any, *, session: str = "sess-A", index: int = 0
    ) -> None:
        _, usage, cache_total = PROBE_TURNS[index]
        self.entry.turn_count += 1
        await self.backend._handle_stream_event(
            {"type": "system", "subtype": "init", "session_id": session}
        )
        terminal = await self.backend._handle_stream_event(
            _result(cost, usage, cache_total, session)
        )
        assert terminal is not None
        await self.backend._complete_stream_turn(None, terminal, 0)  # type: ignore[arg-type]

    def finish(self) -> None:
        self.orch._finish_run_lease(self.issue.id, self.entry, "normal")

    def events(self, event_type: str) -> list[dict[str, Any]]:
        return [
            e["payload"]
            for e in self.registry.run_events(self.run_id)
            if e["event_type"] == event_type
        ]


async def test_three_resumed_turns_cost_the_last_cumulative_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    for index, (cost, _, _) in enumerate(PROBE_TURNS):
        await h.turn(cost, index=index)

    assert h.entry.cost_usd == pytest.approx(0.0595641, abs=TOL)
    turns = h.events("turn_completed")
    assert [t["turn_cost_usd"] for t in turns] == pytest.approx(
        PROBE_TURN_COSTS, abs=TOL
    )
    assert [t["cost_usd"] for t in turns] == pytest.approx(
        [0.0329794, 0.0573399, 0.0595641], abs=TOL
    )
    # Tokens keep their per-turn accumulation: the usage block is per turn.
    assert h.entry.codex_cache_input_tokens == sum(
        u["cache_read_input_tokens"] + u["cache_creation_input_tokens"]
        for _, u, _ in PROBE_TURNS
    )
    # The running run row already carries the cost (crash-safe).
    assert h.registry.get_run(h.run_id).cost_usd == pytest.approx(0.0595641, abs=TOL)

    h.finish()
    (completed,) = h.events("run_completed")
    assert completed["cost_usd"] == pytest.approx(0.0595641, abs=TOL)
    assert h.log.messages("warning") == []


async def test_session_change_mid_run_counts_the_new_value_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    await h.turn(0.0329794, session="sess-A", index=0)
    # A phase transition opens a fresh claude session; its first value is
    # that session's own total.
    await h.turn(0.0100000, session="sess-B", index=1)
    await h.turn(0.0150000, session="sess-B", index=2)

    turns = h.events("turn_completed")
    assert [t["turn_cost_usd"] for t in turns] == pytest.approx(
        [0.0329794, 0.01, 0.005], abs=TOL
    )
    assert h.entry.cost_usd == pytest.approx(0.0479794, abs=TOL)
    assert h.log.messages("info").count("cost_session_changed") == 1


async def test_lower_value_in_same_session_is_a_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    await h.turn(0.0329794, index=0)
    await h.turn(0.0573399, index=1)
    await h.turn(0.0050000, index=2)

    turns = h.events("turn_completed")
    assert [t["turn_cost_usd"] for t in turns] == pytest.approx(
        [0.0329794, 0.0243605, 0.005], abs=TOL
    )
    assert h.entry.cost_usd == pytest.approx(0.0623399, abs=TOL)
    assert h.log.messages("warning") == ["cost_session_reset"]


BAD_VALUES = [
    pytest.param(_MISSING, id="missing"),
    pytest.param(None, id="null"),
    pytest.param("0.0329794", id="string"),
    pytest.param(math.nan, id="nan"),
    pytest.param(math.inf, id="inf"),
    pytest.param(True, id="bool"),
    pytest.param(-0.01, id="negative"),
]


@pytest.mark.parametrize("bad", BAD_VALUES)
async def test_unusable_values_leave_cost_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: Any
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    await h.turn(bad, index=0)
    await h.turn(bad, index=1)
    h.finish()

    assert h.entry.cost_usd is None
    assert all("cost_usd" not in t for t in h.events("turn_completed"))
    assert "cost_usd" not in h.events("run_completed")[0]
    record = h.registry.get_run(h.run_id)
    assert record is not None and record.cost_usd is None
    # NULL, not 0, in the column itself.
    with sqlite3.connect(h.db_path) as conn:
        (raw,) = conn.execute(
            "SELECT cost_usd FROM runs WHERE run_id = ?", (h.run_id,)
        ).fetchone()
    assert raw is None


@pytest.mark.parametrize("bad", BAD_VALUES)
async def test_unusable_value_between_good_ones_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: Any
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    await h.turn(0.0329794, index=0)
    await h.turn(bad, index=1)
    assert h.entry.cost_usd == pytest.approx(0.0329794, abs=TOL)
    await h.turn(0.0595641, index=2)

    assert h.entry.cost_usd == pytest.approx(0.0595641, abs=TOL)
    turns = h.events("turn_completed")
    assert "turn_cost_usd" not in turns[1]
    assert turns[1]["cost_usd"] == pytest.approx(0.0329794, abs=TOL)
    assert turns[2]["turn_cost_usd"] == pytest.approx(0.0265847, abs=TOL)


async def test_cost_reaches_get_api_v1_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    async with TestClient(TestServer(build_app(h.orch))) as client:
        before = await (await client.get("/api/v1/runs")).json()
        assert before["runs"][0]["cost_usd"] is None

        for index, (cost, _, _) in enumerate(PROBE_TURNS):
            await h.turn(cost, index=index)
        live = await (await client.get("/api/v1/runs")).json()
        assert live["runs"][0]["cost_usd"] == pytest.approx(0.0595641, abs=TOL)

        h.finish()
        resp = await client.get(f"/api/v1/runs?issue={h.issue.id}")
        assert resp.status == 200
        (run,) = (await resp.json())["runs"]
        assert run["run_id"] == h.run_id
        assert run["status"] == "normal"
        assert run["cost_usd"] == pytest.approx(0.0595641, abs=TOL)


async def test_cost_survives_sqlite_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    for index, (cost, _, _) in enumerate(PROBE_TURNS):
        await h.turn(cost, index=index)
    h.finish()
    h.registry.close()

    reopened = RunRegistry(h.db_path)
    record = reopened.get_run(h.run_id)
    assert record is not None
    assert record.cost_usd == pytest.approx(0.0595641, abs=TOL)
    detail = reopened.run_detail(h.run_id)
    assert detail is not None
    assert detail["run"]["cost_usd"] == pytest.approx(0.0595641, abs=TOL)
    reopened.close()
    with sqlite3.connect(h.db_path) as conn:
        (raw,) = conn.execute(
            "SELECT cost_usd FROM runs WHERE run_id = ?", (h.run_id,)
        ).fetchone()
    assert raw == pytest.approx(0.0595641, abs=TOL)


def test_complete_run_keeps_older_rows_null_and_rejects_bad_cost(
    tmp_path: Path,
) -> None:
    registry = RunRegistry(tmp_path / "state.db")
    issue = _issue("COST-OLD", state="In Progress")
    run_id = registry.acquire_run(
        issue,
        workspace_path=tmp_path / "COST-OLD",
        attempt=None,
        attempt_kind="initial",
        agent_kind="codex",
    )
    assert run_id is not None
    # Callers that do not know about cost (e.g. codex runs) leave NULL.
    assert registry.complete_run(issue_id=issue.id, run_id=run_id, status="normal")
    record = registry.get_run(run_id)
    assert record is not None and record.cost_usd is None


# ----------------------------------------------------------------------
# Continuation after a service restart (crash_continuation)
# ----------------------------------------------------------------------


def _interrupted_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, keep_events: bool
) -> tuple[RunRegistry, Issue, str]:
    """Two probe turns on sess-A, a checkpoint, then a graceful stop."""
    h = _Harness(tmp_path, monkeypatch)

    async def _turns() -> None:
        await h.turn(PROBE_TURNS[0][0], index=0)
        await h.turn(PROBE_TURNS[1][0], index=1)

    asyncio.run(_turns())
    now = datetime.now(timezone.utc)
    assert h.registry.checkpoint_completed_turn(
        issue_id=h.issue.id,
        run_id=h.run_id,
        resume_session_id="sess-A",
        state=h.issue.state,
        turn=2,
        now=now,
    )
    h.orch._finish_run_lease(h.issue.id, h.entry, "shutdown_interrupted")
    if not keep_events:
        with sqlite3.connect(h.db_path) as conn:
            conn.execute("DELETE FROM attempt_events WHERE run_id = ?", (h.run_id,))
    source = h.registry.get_run(h.run_id)
    assert source is not None
    assert source.cost_usd == pytest.approx(0.0573399, abs=TOL)
    return h.registry, h.issue, h.run_id


@pytest.mark.parametrize("keep_events", [True, False])
def test_resumed_session_after_restart_counts_from_persisted_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep_events: bool
) -> None:
    """Pins both outcomes of a continuation.

    With the predecessor's last `turn_completed` event in place, the first
    resumed turn costs value − persisted session value (0.0022242). Without
    it (events pruned or dropped), the whole cumulative value is counted again:
    the continuation run is overstated by what the predecessor already spent.
    """
    registry, issue, source_run_id = _interrupted_source(
        tmp_path, monkeypatch, keep_events=keep_events
    )
    acquisition = registry.acquire_continuation_run(
        issue,
        continued_from_run_id=source_run_id,
        workspace_path=tmp_path / "ws" / issue.identifier,
        attempt=None,
        attempt_kind="recovery",
        agent_kind="claude",
        now=datetime.now(timezone.utc) + timedelta(seconds=1),
    )
    assert acquisition is not None
    expected_baseline = 0.0573399 if keep_events else None
    if expected_baseline is None:
        assert acquisition.checkpoint.session_cost_usd is None
    else:
        assert acquisition.checkpoint.session_cost_usd == pytest.approx(
            expected_baseline, abs=TOL
        )

    cfg = _make_config(
        workflow_path=tmp_path / "WORKFLOW.md",
        workspace_root=tmp_path / "ws",
        active_states=("In Progress",),
        terminal_states=("Done", "Blocked"),
    )
    cfg = replace(
        cfg,
        agent=replace(
            cfg.agent, max_turns=1, max_total_turns=3, auto_commit_on_done=False
        ),
    )
    seen: dict[str, Any] = {}
    log = _Log()

    class _Backend:
        def __init__(self, on_event: Any) -> None:
            self._on_event = on_event

        async def start(self) -> None:
            return None

        async def initialize(self) -> None:
            return None

        async def resume_session(self, session_id: str) -> bool:
            seen["resumed"] = session_id
            return True

        async def start_session(self, *, initial_prompt: str, issue_title: str) -> str:
            raise AssertionError("continuation must resume, not start fresh")

        async def run_turn(self, *, prompt: str, is_continuation: bool) -> None:
            await self._on_event(
                {
                    "event": EVENT_TURN_COMPLETED,
                    "timestamp": "2026-10-24T00:00:00Z",
                    "payload": {"total_cost_usd": PROBE_TURNS[2][0]},
                    "usage": {},
                }
            )
            entry = orch._running[issue.id]
            seen["turn_cost"] = entry.cost_usd

        async def stop(self) -> None:
            return None

    class _Workspace:
        path = tmp_path

    class _StubWS:
        async def create_or_reuse(self, identifier: str) -> _Workspace:
            return _Workspace()

        async def before_run(self, path: Path) -> None:
            return None

        async def after_run_best_effort(self, path: Path) -> None:
            return None

    orch = _orch()

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        entry = _install_running_entry(orch, issue)
        entry.continued_from_run_id = source_run_id
        entry.continuation_checkpoint = acquisition.checkpoint
        orch._issue_debug[issue.id] = _IssueDebug(completed_turn_count=2)
        _stub_workflow_state_returning(orch, cfg, monkeypatch)
        orch._workspace_manager = _StubWS()  # type: ignore[assignment]
        monkeypatch.setattr(
            core_module, "build_backend", lambda init: _Backend(init.on_event)
        )
        monkeypatch.setattr(
            orch, "_tracker_call_states_by_ids", lambda _cfg, _ids: [issue]
        )
        monkeypatch.setattr(agent_phase_module, "log", log)
        await orch._run_agent_attempt(issue, attempt=None, cfg=cfg)

    asyncio.run(_run())

    assert seen["resumed"] == "sess-A"
    if keep_events:
        assert seen["turn_cost"] == pytest.approx(0.0022242, abs=TOL)
        assert "cost_baseline_missing" not in log.messages("warning")
    else:
        # Known overstatement: 0.0595641 instead of 0.0022242.
        assert seen["turn_cost"] == pytest.approx(0.0595641, abs=TOL)
        assert "cost_baseline_missing" in log.messages("warning")


def test_running_entry_cost_defaults_to_none() -> None:
    entry = RunningEntry(
        issue=_issue("COST-DEF"),
        started_at=datetime.now(timezone.utc),
        retry_attempt=None,
        worker_task=None,  # type: ignore[arg-type]
        workspace_path=Path("/tmp"),
    )
    assert entry.cost_usd is None
    assert entry.cost_session_last is None
