"""``Orchestrator.reset_issue_budget`` and ``POST /api/v1/{id}/reset-budget``."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator, cast

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from symphony.orchestrator import Orchestrator
from symphony.orchestrator.entries import _IssueDebug
from symphony.orchestrator.run_registry import RunRegistry
from symphony.server import build_app

from tests.test_orchestrator_dispatch import (
    _install_running_entry,
    _issue,
    _make_config,
    _orch,
)


def _setup(issue_state: str = "Todo"):
    cfg = _make_config()
    orch = _orch()
    orch._workflow_state._config = cfg
    issue = _issue("RB-1", state=issue_state)
    orch._tracker_call_fetch_issue_full_by_id = (  # type: ignore[method-assign]
        lambda _cfg, identifier: issue if identifier == issue.identifier else None
    )
    return orch, cfg, issue


def _dirty(orch: Orchestrator, issue_id: str) -> None:
    orch._issue_debug[issue_id] = _IssueDebug(
        restart_count=3,
        completed_turn_count=7,
        rewind_count=2,
        state_turn_state="In Progress",
        state_turn_count=5,
        last_error="boom",
        recent_events=[{"event": "x"}],
    )
    orch._turn_budget_exhausted.add(issue_id)
    orch._persisted_retry_attempts[issue_id] = 4


async def test_reset_zeroes_counters_and_flags(tmp_path: Path):
    orch, _cfg, issue = _setup()
    registry = RunRegistry(tmp_path / "state.db")
    orch._run_registry = registry
    _dirty(orch, issue.id)
    registry.set_issue_flags(issue.id, budget_exhausted=True, retry_attempt=4)

    result = await orch.reset_issue_budget(issue.identifier)

    debug = orch._issue_debug[issue.id]
    assert (debug.completed_turn_count, debug.rewind_count, debug.state_turn_count) == (0, 0, 0)
    assert debug.state_turn_state == ""
    assert debug.last_error is None
    assert debug.restart_count == 3
    assert debug.recent_events == [{"event": "x"}]
    assert issue.id not in orch._turn_budget_exhausted
    assert issue.id not in orch._persisted_retry_attempts
    flags = registry.get_issue_flags(issue.id)
    assert flags is None or (not flags.budget_exhausted and flags.retry_attempt is None)

    assert result["changed"] is True
    assert result["issue_identifier"] == issue.identifier
    assert result["issue_id"] == issue.id
    assert result["state"] == "Todo"
    assert result["before"] == {
        "completed_turn_count": 7,
        "rewind_count": 2,
        "state_turn_state": "In Progress",
        "state_turn_count": 5,
        "restart_count": 3,
        "budget_exhausted": True,
        "retry_attempt": 4,
        "paused": False,
    }


async def test_second_reset_is_a_noop():
    orch, _cfg, issue = _setup()
    _dirty(orch, issue.id)
    assert (await orch.reset_issue_budget(issue.identifier))["changed"] is True
    again = await orch.reset_issue_budget(issue.identifier)
    assert again["changed"] is False
    assert again["before"]["budget_exhausted"] is False
    assert again["before"]["retry_attempt"] is None


async def test_reset_without_debug_record_is_a_noop():
    orch, _cfg, issue = _setup()
    result = await orch.reset_issue_budget(issue.identifier)
    assert result["changed"] is False
    assert issue.id not in orch._issue_debug  # nothing is created by a reset


async def test_reset_refused_for_running_worker():
    orch, _cfg, issue = _setup()
    _install_running_entry(orch, issue)
    _dirty(orch, issue.id)
    with pytest.raises(ValueError, match="running worker"):
        await orch.reset_issue_budget(issue.identifier)
    assert issue.id in orch._turn_budget_exhausted


async def test_reset_refused_for_active_run_lease(tmp_path: Path):
    orch, _cfg, issue = _setup()
    registry = RunRegistry(tmp_path / "state.db")
    orch._run_registry = registry
    run_id = registry.acquire_run(
        issue, workspace_path=tmp_path, attempt=None,
        attempt_kind="normal", agent_kind="claude",
    )
    assert run_id
    _dirty(orch, issue.id)
    with pytest.raises(ValueError, match="active run lease"):
        await orch.reset_issue_budget(issue.identifier)
    assert issue.id in orch._turn_budget_exhausted


async def test_reset_unknown_issue():
    orch, _cfg, _issue_ = _setup()
    with pytest.raises(FileNotFoundError):
        await orch.reset_issue_budget("NOPE-1")


async def test_reset_without_workflow_is_refused():
    orch = _orch()
    with pytest.raises(ValueError, match="workflow is unavailable"):
        await orch.reset_issue_budget("RB-1")


async def test_reset_keeps_pause():
    orch, _cfg, issue = _setup()
    _dirty(orch, issue.id)
    orch._paused_issue_ids.add(issue.id)
    result = await orch.reset_issue_budget(issue.identifier)
    assert result["before"]["paused"] is True
    assert orch.is_paused(issue.id) is True
    assert result["changed"] is True


async def test_flag_does_not_return_after_service_restart(tmp_path: Path):
    db = tmp_path / "state.db"
    orch, cfg, issue = _setup()
    registry = RunRegistry(db)
    orch._run_registry = registry
    registry.set_issue_flags(issue.id, budget_exhausted=True)
    orch._turn_budget_exhausted.add(issue.id)

    await orch.reset_issue_budget(issue.identifier)
    registry.close()

    # What the service does on start: open the registry, load flags, rehydrate.
    restarted = _orch()
    reopened = RunRegistry(db)
    restarted._rehydrate_issue_flags(reopened.list_issue_flags(), cfg=cfg, registry=reopened)
    assert issue.id not in restarted._turn_budget_exhausted
    flags = reopened.get_issue_flags(issue.id)
    assert flags is None or not flags.budget_exhausted

    # Control: without a reset the flag does come back.
    reopened.set_issue_flags(issue.id, budget_exhausted=True)
    control = _orch()
    control._rehydrate_issue_flags(reopened.list_issue_flags(), cfg=cfg, registry=reopened)
    assert issue.id in control._turn_budget_exhausted
    reopened.close()


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


class _StubOrch:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    def request_refresh(self) -> bool:
        return False

    async def reset_issue_budget(self, identifier: str) -> dict[str, Any]:
        self.calls.append(identifier)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return cast(dict[str, Any], self.outcome)


async def _client(stub: _StubOrch) -> TestClient:
    cli = TestClient(TestServer(build_app(cast(Orchestrator, stub))))
    await cli.start_server()
    return cli


@pytest_asyncio.fixture
async def route_clients() -> AsyncIterator[list[TestClient]]:
    clients: list[TestClient] = []
    yield clients
    for cli in clients:
        await cli.close()


async def test_route_ok(route_clients: list[TestClient]):
    body = {"issue_identifier": "RB-1", "issue_id": "id-RB-1", "state": "Todo",
            "changed": True, "before": {"paused": False}}
    stub = _StubOrch(body)
    cli = await _client(stub)
    route_clients.append(cli)
    resp = await cli.post("/api/v1/RB-1/reset-budget")
    assert resp.status == 200
    assert await resp.json() == body
    assert stub.calls == ["RB-1"]


@pytest.mark.parametrize("exc,status,code", [
    (FileNotFoundError("RB-1"), 404, "issue_not_found"),
    (ValueError("RB-1 has a running worker"), 409, "not_resettable"),
    (RuntimeError("disk"), 500, "reset_budget_failed"),
])
async def test_route_errors(route_clients: list[TestClient], exc, status, code):
    cli = await _client(_StubOrch(exc))
    route_clients.append(cli)
    resp = await cli.post("/api/v1/RB-1/reset-budget")
    assert resp.status == status
    assert (await resp.json())["error"]["code"] == code
