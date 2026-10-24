"""`agent.preserve_workspace_states`: terminal workspaces kept for review."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

import symphony.orchestrator.core as core_module
from symphony.errors import ConfigValidationError
from symphony.orchestrator import Orchestrator, RunningEntry
from symphony.trackers.file import FileBoardTracker
from symphony.workflow import (
    ServiceConfig,
    WorkflowState,
    build_service_config,
    parse_workflow_text,
)
from symphony.workspace import COMMIT_OK, CommitOutcome, WorkspaceManager

IDENT = "MT-1"
LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _config(tmp_path: Path, *agent_lines: str) -> ServiceConfig:
    agent = "".join(f"  {line}\n" for line in agent_lines)
    text = (
        "---\n"
        "tracker:\n"
        "  kind: file\n"
        "  board_root: ./board\n"
        "  active_states: [Todo, In Progress]\n"
        "  terminal_states: [Human Review, needs_human, Done, Blocked, Archive]\n"
        "workspace:\n"
        "  root: ./ws\n"
        "agent:\n"
        "  auto_merge_on_done: false\n"
        "  normalize_legacy_human_review: false\n"
        f"{agent}"
        "---\nprompt\n"
    )
    path = tmp_path / "WORKFLOW.md"
    path.write_text(text, encoding="utf-8")
    return build_service_config(parse_workflow_text(text, path))


def _setup(tmp_path: Path, cfg: ServiceConfig, monkeypatch, state: str):
    path = tmp_path / "board" / f"{IDENT}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {IDENT}\ntitle: Keep me\nstate: {state}\n---\nbody\n",
        encoding="utf-8",
    )
    orch = Orchestrator(WorkflowState(cfg.workflow_path))
    monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
    manager = WorkspaceManager(
        cfg.workspace_root, cfg.hooks, workflow_dir=cfg.workflow_path.parent
    )
    orch._workspace_manager = manager
    workspace = manager.path_for(IDENT)
    workspace.mkdir(parents=True)
    snapshots: list[str] = []

    async def _snapshot(path, *, identifier, **_):
        del path
        snapshots.append(identifier)
        return CommitOutcome(COMMIT_OK)

    monkeypatch.setattr(core_module, "commit_workspace_on_done", _snapshot)
    return orch, workspace, snapshots


def _move(cfg: ServiceConfig, state: str) -> None:
    tracker = FileBoardTracker(cfg.tracker)
    try:
        issue = tracker.fetch_issue_full_by_id(IDENT)
        assert issue is not None
        tracker.update_state(issue, state)
    finally:
        tracker.close()


def _reconcile(orch: Orchestrator, cfg: ServiceConfig, workspace: Path) -> bool:
    """Reconcile a terminal ticket whose worker is past the grace; True if cancelled."""
    tracker = FileBoardTracker(cfg.tracker)
    issue = tracker.fetch_issue_full_by_id(IDENT)
    tracker.close()
    assert issue is not None

    async def _run() -> bool:
        orch._loop = asyncio.get_running_loop()
        worker_task = asyncio.create_task(asyncio.sleep(3600))
        entry = RunningEntry(
            issue=issue,
            started_at=datetime.now(timezone.utc),
            retry_attempt=None,
            worker_task=worker_task,
            workspace_path=workspace,
        )
        entry.terminal_seen_at = LONG_AGO
        orch._running[issue.id] = entry
        await orch._reconcile_running(cfg)
        cancelled = worker_task.cancelling() > 0
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        return cancelled

    return asyncio.run(_run())


@pytest.mark.parametrize(
    ("keep", "kept"),
    [((), False), (("needs_human",), True), (("NEEDS_Human",), True)],
)
def test_reconcile_keeps_listed_state_and_still_snapshots(
    tmp_path, monkeypatch, keep, kept
):
    lines = [f"preserve_workspace_states: {list(keep)}"] if keep else []
    cfg = _config(tmp_path, *lines)
    orch, workspace, snapshots = _setup(tmp_path, cfg, monkeypatch, "Needs_Human")

    assert _reconcile(orch, cfg, workspace) is True

    assert workspace.is_dir() is kept
    assert snapshots == [IDENT]


@pytest.mark.parametrize(
    ("keep", "kept"),
    [((), False), (("needs_human",), True), (("Needs_HUMAN",), True)],
)
def test_startup_keeps_listed_state(tmp_path, monkeypatch, keep, kept):
    lines = [f"preserve_workspace_states: {list(keep)}"] if keep else []
    cfg = _config(tmp_path, *lines)
    orch, workspace, _snapshots = _setup(tmp_path, cfg, monkeypatch, "needs_human")

    asyncio.run(orch._startup_terminal_cleanup(cfg))

    assert workspace.is_dir() is kept


def test_startup_hardcoded_states_are_kept_alongside_the_key(tmp_path, monkeypatch):
    cfg = _config(tmp_path, "preserve_workspace_states: [needs_human]")
    for state in ("Blocked", "Human Review"):
        orch, workspace, _snapshots = _setup(tmp_path, cfg, monkeypatch, state)
        asyncio.run(orch._startup_terminal_cleanup(cfg))
        assert workspace.is_dir()
        workspace.rmdir()


def test_leaving_preserved_state_for_archive_reaps_on_startup(tmp_path, monkeypatch):
    cfg = _config(tmp_path, "preserve_workspace_states: [needs_human]")
    orch, workspace, _snapshots = _setup(tmp_path, cfg, monkeypatch, "needs_human")
    asyncio.run(orch._startup_terminal_cleanup(cfg))
    assert workspace.is_dir()

    _move(cfg, "Archive")
    asyncio.run(orch._startup_terminal_cleanup(cfg))

    assert not workspace.exists()


def test_confirm_done_from_preserved_human_review_reaps(tmp_path, monkeypatch):
    cfg = _config(
        tmp_path,
        "preserve_workspace_states: [Human Review]",
        "auto_commit_on_done: false",
    )
    orch, workspace, _snapshots = _setup(tmp_path, cfg, monkeypatch, "Human Review")
    assert _reconcile(orch, cfg, workspace) is True
    assert workspace.is_dir()
    orch._running.clear()

    result = asyncio.run(orch.confirm_done(IDENT))

    assert result["changed"] is True
    assert not workspace.exists()


def test_default_is_empty_and_states_are_normalized(tmp_path):
    assert _config(tmp_path).agent.preserve_workspace_states == ()
    cfg = _config(tmp_path, "preserve_workspace_states: [' Needs_Human ', Blocked]")
    assert cfg.agent.preserve_workspace_states == ("needs_human", "blocked")


@pytest.mark.parametrize(
    "value", ["needs_human", "[needs_human, '']", "[1]", "[needs_human, Done]", "{a: b}"]
)
def test_bad_value_is_a_config_error(tmp_path, value):
    with pytest.raises(ConfigValidationError, match="preserve_workspace_states"):
        _config(tmp_path, f"preserve_workspace_states: {value}")
