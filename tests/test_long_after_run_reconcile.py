"""Reconcile vs. a long `after_run` hook on a ticket that went terminal.

The worker runs `after_run` after its last turn, while the board already
says Human Review. Reconcile must not cancel that worker or reap its
workspace mid-hook (the hook may still move the ticket back), but a hook
that outlives `hooks.timeout_ms` + grace falls back to the old cleanup.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import symphony.orchestrator.core as core_mod
from symphony.orchestrator import Orchestrator
from symphony.orchestrator.constants import AFTER_RUN_RECONCILE_GRACE_S
from symphony.trackers.file import FileBoardTracker
from symphony.workflow import (
    ServiceConfig,
    WorkflowState,
    build_service_config,
    parse_workflow_text,
)
from symphony.workspace import WorkspaceManager

IDENT = "MT-1"
HOOK_TIMEOUT_MS = 30_000
LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)
posix_only = pytest.mark.skipif(os.name != "posix", reason="bash hook")


def _config(tmp_path: Path, *agent_lines: str) -> ServiceConfig:
    """Real file board + real workspace hooks; first after_run blocks."""
    started = tmp_path / "hook-started"
    release = tmp_path / "hook-release"
    finished = tmp_path / "hook-finished"
    count = tmp_path / "hook-count"
    hook = tmp_path / "after_run.sh"
    # First run: wait for the test, then roll the ticket back to In
    # Progress like a failing check would. Later runs pass silently.
    hook.write_text(
        "set -e\n"
        f"n=$(cat '{count}' 2>/dev/null || echo 0); n=$((n+1)); echo $n > '{count}'\n"
        'if [ "$n" = 1 ]; then\n'
        f"  touch '{started}'\n"
        f"  for _ in $(seq 1 400); do [ -e '{release}' ] && break; sleep 0.05; done\n"
        f"  '{sys.executable}' -m symphony board mv {IDENT} 'In Progress'"
        f" --root '{tmp_path / 'board'}'\n"
        f"  touch '{finished}'\n"
        "fi\n",
        encoding="utf-8",
    )
    agent = "".join(f"  {line}\n" for line in agent_lines)
    text = (
        "---\n"
        "tracker:\n"
        "  kind: file\n"
        "  board_root: ./board\n"
        "  active_states: [Todo, In Progress]\n"
        "  terminal_states: [Human Review, Done, Blocked]\n"
        "workspace:\n"
        "  root: ./ws\n"
        "hooks:\n"
        f"  after_run: \"bash '{hook}'\"\n"
        f"  timeout_ms: {HOOK_TIMEOUT_MS}\n"
        "agent:\n"
        "  max_turns: 3\n"
        "  auto_merge_on_done: false\n"
        "  auto_commit_on_done: false\n"
        "  normalize_legacy_human_review: false\n"
        "  stage_contracts: off\n"
        f"{agent}"
        "---\nprompt\n"
    )
    path = tmp_path / "WORKFLOW.md"
    path.write_text(text, encoding="utf-8")
    return build_service_config(parse_workflow_text(text, path))


def _state(cfg: ServiceConfig) -> str:
    tracker = FileBoardTracker(cfg.tracker)
    try:
        issue = tracker.fetch_issue_full_by_id(IDENT)
    finally:
        tracker.close()
    assert issue is not None
    return issue.state


class _ToHumanReviewBackend:
    """Each turn records the state it ran in and finishes in Human Review."""

    seen: list[str]

    def __init__(self, cfg: ServiceConfig) -> None:
        self.cfg = cfg

    async def start(self) -> None:
        return None

    async def initialize(self) -> None:
        return None

    async def start_session(self, *, initial_prompt: str, issue_title: str) -> None:
        del initial_prompt, issue_title

    async def run_turn(self, *, prompt: str, is_continuation: bool) -> None:
        del prompt, is_continuation
        tracker = FileBoardTracker(self.cfg.tracker)
        try:
            issue = tracker.fetch_issue_full_by_id(IDENT)
            assert issue is not None
            _ToHumanReviewBackend.seen.append(issue.state)
            tracker.update_state(issue, "Human Review")
        finally:
            tracker.close()

    async def stop(self) -> None:
        return None


def _orchestrator(tmp_path: Path, cfg: ServiceConfig, monkeypatch) -> Orchestrator:
    tracker = FileBoardTracker(cfg.tracker)
    tracker.create(identifier=IDENT, title="Long hook", state="In Progress", priority=1)
    tracker.close()
    orch = Orchestrator(WorkflowState(cfg.workflow_path))
    monkeypatch.setattr(orch._workflow_state, "reload", lambda: (cfg, None))
    monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
    orch._workspace_manager = WorkspaceManager(
        cfg.workspace_root, cfg.hooks, workflow_dir=cfg.workflow_path.parent
    )
    _ToHumanReviewBackend.seen = []
    monkeypatch.setattr(core_mod, "build_backend", lambda init: _ToHumanReviewBackend(cfg))
    return orch


async def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.exists():
        assert asyncio.get_running_loop().time() < deadline, f"{path.name} never appeared"
        await asyncio.sleep(0.02)


async def _reconcile_mid_hook(
    tmp_path: Path, orch: Orchestrator, cfg: ServiceConfig, *, aged: str
) -> dict[str, Any]:
    """Dispatch, then reconcile while the first after_run is still running."""
    await orch._on_tick()
    entry = orch._running[next(iter(orch._running))]
    worker = entry.worker_task
    workspace = entry.workspace_path
    await _wait_for(tmp_path / "hook-started")
    assert _state(cfg) == "Human Review"
    # Pretend the reconcile grace (60 s) or the stall budget is long gone.
    entry.last_progress_timestamp = None
    if aged == "stall":
        entry.started_at = LONG_AGO
    else:
        entry.terminal_seen_at = LONG_AGO
    if aged == "hook_overdue":
        limit = HOOK_TIMEOUT_MS / 1000 + AFTER_RUN_RECONCILE_GRACE_S
        entry.after_run_started_at = datetime.now(timezone.utc) - timedelta(
            seconds=limit + 5
        )
    await orch._reconcile_running(cfg)
    observed = {
        "cancelled": worker.cancelling() > 0 or worker.cancelled(),
        "workspace_after_reconcile": workspace.is_dir(),
    }
    (tmp_path / "hook-release").touch()
    await _wait_for(tmp_path / "hook-finished")
    try:
        await asyncio.wait_for(asyncio.shield(worker), timeout=10)
    except asyncio.CancelledError:
        pass
    for retry in list(orch._retry.values()):
        retry.timer_handle.cancel()
    observed["workspace_at_end"] = workspace.is_dir()
    return observed


@posix_only
@pytest.mark.parametrize("aged", ["terminal", "stall"])
def test_reconcile_waits_for_after_run_and_applies_its_verdict(
    tmp_path, monkeypatch, aged
):
    cfg = _config(tmp_path)
    orch = _orchestrator(tmp_path, cfg, monkeypatch)

    observed = asyncio.run(_reconcile_mid_hook(tmp_path, orch, cfg, aged=aged))

    assert observed["cancelled"] is False
    assert observed["workspace_after_reconcile"] is True
    # The hook's rollback reached the worker: a second turn ran in In
    # Progress and finished back in Human Review with the workspace kept.
    assert _ToHumanReviewBackend.seen == ["In Progress", "In Progress"]
    assert _state(cfg) == "Human Review"
    assert observed["workspace_at_end"] is True
    assert not orch._running


@posix_only
def test_reconcile_reaps_worker_whose_after_run_overran_the_hook_timeout(
    tmp_path, monkeypatch
):
    cfg = _config(tmp_path)
    orch = _orchestrator(tmp_path, cfg, monkeypatch)

    observed = asyncio.run(
        _reconcile_mid_hook(tmp_path, orch, cfg, aged="hook_overdue")
    )

    # Old behavior: worker cancelled, Human Review workspace reaped.
    assert observed["cancelled"] is True
    assert observed["workspace_after_reconcile"] is False
    assert _ToHumanReviewBackend.seen == ["In Progress"]
