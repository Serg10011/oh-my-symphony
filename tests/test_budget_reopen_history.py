"""`issue_budget_snapshot` reads the real run registry from the service thread."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from symphony.orchestrator.run_registry import RunRegistry
from tests.test_orchestrator_dispatch import _install_running_entry, _issue, _make_config, _orch


def _setup(tmp_path: Path):
    cfg = _make_config()
    cfg = replace(cfg, agent=replace(cfg.agent, max_reopens=2))
    orch = _orch()
    orch._workflow_state._config = cfg
    # Created in the test (event-loop) thread, like the service creates it.
    orch._run_registry = RunRegistry(tmp_path / "state.db")
    return orch, _issue("REOPEN-1", state="Done")


def _complete_run(registry: RunRegistry, issue, *, state: str = "Done") -> None:
    run_id = registry.acquire_run(
        issue,
        workspace_path=Path("/tmp/ws-fake"),
        attempt=None,
        attempt_kind="first",
        agent_kind="codex",
    )
    assert run_id is not None
    assert registry.complete_run(
        issue_id=issue.id, run_id=run_id, status="normal", state=state
    )


def _reopens(result):
    return next(row for row in result["items"] if row["name"] == "reopens")


async def test_reopens_is_known_and_registry_error_counter_stays_zero(tmp_path):
    orch, issue = _setup(tmp_path)
    errors_before = orch._registry_error_count

    row = _reopens(await orch.issue_budget_snapshot(issue))

    assert row["used"] is not None
    assert orch._registry_error_count == errors_before
    assert orch._last_registry_error is None


async def test_reopens_counts_prior_done_runs_as_before(tmp_path):
    orch, issue = _setup(tmp_path)
    assert _reopens(await orch.issue_budget_snapshot(issue))["used"] == 0

    _complete_run(orch._run_registry, issue)
    # One completed delivery is the initial completion, not a reopen.
    assert _reopens(await orch.issue_budget_snapshot(issue))["used"] == 0

    _complete_run(orch._run_registry, issue)
    _complete_run(orch._run_registry, issue, state="In Progress")
    row = _reopens(await orch.issue_budget_snapshot(issue))
    assert (row["used"], row["remaining"], row["status"]) == (1, 1, "remaining")

    # A live run on top of earlier deliveries counts as one more reopen.
    _install_running_entry(orch, issue)
    assert _reopens(await orch.issue_budget_snapshot(issue))["used"] == 2
    assert orch._registry_error_count == 0
    assert orch._last_registry_error is None
