"""`agent.done_gate_command`: external gate on every Done acceptance path."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import symphony.orchestrator.core as core_module
from symphony.errors import ConfigValidationError
from symphony.orchestrator import Orchestrator, RunningEntry, _IssueDebug
from symphony.trackers.file import FileBoardTracker
from symphony.workflow import (
    ServiceConfig,
    WorkflowState,
    build_service_config,
    parse_workflow_text,
)
from symphony.workspace import WorkspaceManager

IDENT = "MT-1"
posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")


def _config(tmp_path: Path, *agent_lines: str) -> ServiceConfig:
    path = tmp_path / "WORKFLOW.md"
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
        "agent:\n"
        "  auto_merge_on_done: false\n"
        "  auto_commit_on_done: false\n"
        f"{agent}"
        "---\nprompt\n"
    )
    path.write_text(text, encoding="utf-8")
    return build_service_config(parse_workflow_text(text, path))


def _gate_config(tmp_path: Path, script: str, *extra: str) -> ServiceConfig:
    gate = tmp_path / "gate.sh"
    gate.write_text(f"#!/usr/bin/env bash\n{script}\n", encoding="utf-8")
    gate.chmod(0o755)
    return _config(tmp_path, f"done_gate_command: '{gate}'", *extra)


def _card(tmp_path: Path, state: str) -> Path:
    path = tmp_path / "board" / f"{IDENT}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {IDENT}\ntitle: Gate card\nstate: {state}\n---\nbody\n",
        encoding="utf-8",
    )
    return path


def _state(tmp_path: Path) -> str:
    issue = FileBoardTracker(_config(tmp_path).tracker).fetch_issue_full_by_id(IDENT)
    assert issue is not None
    return issue.state


def _setup(tmp_path: Path, cfg: ServiceConfig, monkeypatch, state: str = "Done"):
    card = _card(tmp_path, state)
    orch = Orchestrator(WorkflowState(cfg.workflow_path))
    monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
    manager = WorkspaceManager(
        cfg.workspace_root, cfg.hooks, workflow_dir=cfg.workflow_path.parent
    )
    orch._workspace_manager = manager
    workspace = manager.path_for(IDENT)
    workspace.mkdir(parents=True)
    issue = FileBoardTracker(cfg.tracker).fetch_issue_full_by_id(IDENT)
    assert issue is not None
    return orch, issue, workspace, card


def _gate(orch, cfg, issue, workspace, debug=None) -> bool:
    return asyncio.run(
        orch._auto_merge_done_gate_or_block(cfg, issue, workspace, debug_target=debug)
    )


# 1. Pass: Done stays, script sees the documented cwd and environment.
def test_passing_gate_keeps_done_and_gets_issue_env(tmp_path, monkeypatch):
    cfg = _gate_config(
        tmp_path,
        'env | grep "^SYMPHONY_" > "$PWD/../env.txt"; pwd > "$PWD/../cwd.txt"; exit 0',
    )
    orch, issue, workspace, card = _setup(tmp_path, cfg, monkeypatch)

    assert _gate(orch, cfg, issue, workspace) is True

    assert _state(tmp_path) == "Done"
    assert "Done Gate Failure" not in card.read_text(encoding="utf-8")
    env = (workspace.parent / "env.txt").read_text(encoding="utf-8")
    assert f"SYMPHONY_ISSUE_ID={IDENT}" in env
    assert f"SYMPHONY_WORKSPACE={workspace}" in env
    assert f"SYMPHONY_BRANCH=symphony/{IDENT}" in env
    assert "SYMPHONY_ISSUE_STATE=Done" in env
    assert f"SYMPHONY_WORKFLOW_DIR={tmp_path}" in env
    cwd = (workspace.parent / "cwd.txt").read_text(encoding="utf-8").strip()
    assert Path(cwd).resolve() == workspace.resolve()


# 2. Non-zero exit: Blocked, note with output tail, workspace kept, last_error.
def test_failing_gate_blocks_with_output_tail(tmp_path, monkeypatch):
    cfg = _gate_config(tmp_path, "echo 'pytest: 2 failed'; echo boom >&2; exit 3")
    orch, issue, workspace, card = _setup(tmp_path, cfg, monkeypatch)
    debug = _IssueDebug()

    assert _gate(orch, cfg, issue, workspace, debug) is False

    text = card.read_text(encoding="utf-8")
    assert _state(tmp_path) == "Blocked"
    assert "## Done Gate Failure" in text
    assert "reason: `exit_code`" in text
    assert "returncode: `3`" in text
    assert "pytest: 2 failed" in text and "boom" in text
    assert workspace.is_dir()
    assert "done gate failed (exit_code)" in (debug.last_error or "")


def test_note_keeps_only_output_tail(tmp_path, monkeypatch):
    cfg = _gate_config(
        tmp_path, "head -c 20000 /dev/zero | tr '\\0' x; echo; echo LAST-LINE; exit 1"
    )
    orch, issue, workspace, card = _setup(tmp_path, cfg, monkeypatch)

    assert _gate(orch, cfg, issue, workspace) is False

    text = card.read_text(encoding="utf-8")
    assert "LAST-LINE" in text
    assert len(text) < 6000


# 3. Timeout: refused quickly, no process of the gate group survives.
@posix_only
def test_timeout_refuses_and_kills_process_group(tmp_path, monkeypatch):
    pids = tmp_path / "pids.txt"
    cfg = _gate_config(
        tmp_path,
        f'sleep 60 & echo $! > "{pids}"; echo $$ >> "{pids}"; wait',
        "done_gate_timeout_ms: 500",
    )
    orch, issue, workspace, card = _setup(tmp_path, cfg, monkeypatch)

    started = time.monotonic()
    assert _gate(orch, cfg, issue, workspace) is False
    assert time.monotonic() - started < 15

    assert _state(tmp_path) == "Blocked"
    assert "reason: `timeout`" in card.read_text(encoding="utf-8")
    recorded = [int(p) for p in pids.read_text(encoding="utf-8").split()]
    assert len(recorded) == 2
    deadline = time.monotonic() + 5
    while any(_alive(pid) for pid in recorded) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(_alive(pid) for pid in recorded)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        # An orphan reaped late by a container init still shows as a zombie.
        return "\nState:\tZ" not in status.read_text(encoding="utf-8")
    return True


# 4. Missing command: bash exits 127, the card does not stay Done.
def test_missing_command_refuses(tmp_path, monkeypatch):
    cfg = _config(tmp_path, f"done_gate_command: '{tmp_path}/no-such-gate'")
    orch, issue, workspace, card = _setup(tmp_path, cfg, monkeypatch)

    assert _gate(orch, cfg, issue, workspace) is False

    assert _state(tmp_path) == "Blocked"
    assert "reason: `not_found`" in card.read_text(encoding="utf-8")


def test_spawn_exception_refuses(tmp_path, monkeypatch):
    cfg = _gate_config(tmp_path, "exit 0")
    orch, issue, workspace, card = _setup(tmp_path, cfg, monkeypatch)

    def _no_bash() -> str:
        raise OSError("bash disappeared")

    monkeypatch.setattr("symphony.workspace.resolve_bash", _no_bash)

    assert _gate(orch, cfg, issue, workspace) is False

    assert _state(tmp_path) == "Blocked"
    text = card.read_text(encoding="utf-8")
    assert "reason: `exception`" in text and "bash disappeared" in text


def test_missing_workspace_refuses(tmp_path, monkeypatch):
    cfg = _gate_config(tmp_path, "exit 0")
    orch, issue, workspace, _card_path = _setup(tmp_path, cfg, monkeypatch)
    workspace.rmdir()

    assert _gate(orch, cfg, issue, workspace) is False
    assert _state(tmp_path) == "Blocked"


# 5. Not configured: no spawn, no state change.
@pytest.mark.parametrize("line", [None, "done_gate_command: ''", "done_gate_command: '   '"])
def test_empty_command_never_runs(tmp_path, monkeypatch, line):
    cfg = _config(tmp_path, *([line] if line else []))
    orch, issue, workspace, card = _setup(tmp_path, cfg, monkeypatch)
    before = card.read_text(encoding="utf-8")

    async def _must_not_run(*_args, **_kwargs):
        raise AssertionError("gate must not run")

    monkeypatch.setattr(WorkspaceManager, "run_done_gate", _must_not_run)

    assert cfg.agent.done_gate_command == ""
    assert _gate(orch, cfg, issue, workspace) is True
    assert card.read_text(encoding="utf-8") == before


# 6. Order: the gate runs with auto-merge off, and before the merge when on.
def test_gate_runs_before_merge_and_skips_it_on_refusal(tmp_path, monkeypatch):
    cfg = _gate_config(tmp_path, "exit 1", "auto_merge_on_done: true")
    orch, issue, workspace, _card_path = _setup(tmp_path, cfg, monkeypatch)
    merges: list[str] = []

    async def _merge(**kwargs):
        merges.append(kwargs["identifier"])

    monkeypatch.setattr(core_module, "auto_merge_on_done_best_effort", _merge)

    assert cfg.agent.auto_merge_on_done is True
    assert _gate(orch, cfg, issue, workspace) is False
    assert merges == []


def test_gate_runs_with_auto_merge_off(tmp_path, monkeypatch):
    cfg = _gate_config(tmp_path, "exit 1")
    orch, issue, workspace, _card_path = _setup(tmp_path, cfg, monkeypatch)

    assert cfg.agent.auto_merge_on_done is False
    assert _gate(orch, cfg, issue, workspace) is False
    assert _state(tmp_path) == "Blocked"


# Acceptance path A: the worker finished its turn with the card in Done.
@pytest.mark.parametrize(("code", "expected"), [(0, "Done"), (4, "Blocked")])
def test_worker_exit_path(tmp_path, monkeypatch, code, expected):
    cfg = _gate_config(tmp_path, f"exit {code}")
    orch, issue, workspace, _card_path = _setup(tmp_path, cfg, monkeypatch)

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        orch._running[issue.id] = RunningEntry(
            issue=issue,
            started_at=datetime.now(timezone.utc),
            retry_attempt=None,
            worker_task=None,  # type: ignore[arg-type]
            workspace_path=workspace,
        )
        try:
            await orch._on_worker_exit(issue.id, reason="normal", error=None)
        finally:
            for retry in list(orch._retry.values()):
                retry.timer_handle.cancel()

    asyncio.run(_run())

    assert _state(tmp_path) == expected
    # Pass -> normal Done cleanup removes the workspace; refusal keeps it.
    assert workspace.is_dir() is (expected == "Blocked")


# Acceptance path B: reconcile saw Done on a running worker.
@pytest.mark.parametrize(("code", "expected"), [(0, "Done"), (5, "Blocked")])
def test_reconcile_path(tmp_path, monkeypatch, code, expected):
    cfg = _gate_config(tmp_path, f"exit {code}")
    orch, issue, workspace, _card_path = _setup(tmp_path, cfg, monkeypatch)

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        worker_task = asyncio.create_task(asyncio.sleep(3600))
        entry = RunningEntry(
            issue=issue,
            started_at=datetime.now(timezone.utc),
            retry_attempt=None,
            worker_task=worker_task,
            workspace_path=workspace,
        )
        entry.last_codex_timestamp = datetime(2000, 1, 1, tzinfo=timezone.utc)
        entry.terminal_seen_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        orch._running[issue.id] = entry
        try:
            await orch._reconcile_running(cfg)
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())

    assert _state(tmp_path) == expected
    assert workspace.is_dir() is (expected == "Blocked")


# Acceptance path C (7.): confirm_done from TUI / web / API.
def test_confirm_done_passes(tmp_path, monkeypatch):
    cfg = _gate_config(tmp_path, "exit 0")
    orch, _issue, workspace, _card_path = _setup(
        tmp_path, cfg, monkeypatch, state="Human Review"
    )

    result = asyncio.run(orch.confirm_done(IDENT))

    assert result["changed"] is True
    assert _state(tmp_path) == "Done"
    assert not workspace.exists()


def test_confirm_done_refusal_never_writes_done(tmp_path, monkeypatch):
    cfg = _gate_config(
        tmp_path,
        'echo "$SYMPHONY_ISSUE_STATE" > "$PWD/../seen-state.txt"; exit 2',
    )
    orch, _issue, workspace, card = _setup(
        tmp_path, cfg, monkeypatch, state="Human Review"
    )
    written: list[str] = []
    original = orch._tracker_call_update_state

    def _record(cfg_, issue_, target):
        written.append(target)
        return original(cfg_, issue_, target)

    monkeypatch.setattr(orch, "_tracker_call_update_state", _record)

    with pytest.raises(ValueError, match=r"done gate failed \(exit_code\)"):
        asyncio.run(orch.confirm_done(IDENT))

    assert written == ["Blocked"]
    assert _state(tmp_path) == "Blocked"
    assert "## Done Gate Failure" in card.read_text(encoding="utf-8")
    assert workspace.is_dir()
    seen = (workspace.parent / "seen-state.txt").read_text(encoding="utf-8")
    assert seen.strip() == "Human Review"


# 8. Persisting Blocked fails: the gate still refuses.
def test_refusal_holds_when_block_cannot_be_persisted(tmp_path, monkeypatch):
    cfg = _gate_config(tmp_path, "exit 1")
    orch, issue, workspace, _card_path = _setup(tmp_path, cfg, monkeypatch)

    def _broken(*_args, **_kwargs):
        raise OSError("board is read-only")

    monkeypatch.setattr(orch, "_tracker_call_update_state", _broken)

    assert _gate(orch, cfg, issue, workspace) is False
    assert workspace.is_dir()


# 9. Strict config parsing.
def test_defaults(tmp_path):
    cfg = _config(tmp_path)
    assert cfg.agent.done_gate_command == ""
    assert cfg.agent.done_gate_timeout_ms == 600_000


def test_command_is_stripped_and_read_from_workflow(tmp_path):
    cfg = _config(tmp_path, "done_gate_command: '  ./check.sh  '", "done_gate_timeout_ms: 1500")
    assert cfg.agent.done_gate_command == "./check.sh"
    assert cfg.agent.done_gate_timeout_ms == 1500


@pytest.mark.parametrize("value", ["5", "[a, b]", "true", "{x: 1}"])
def test_non_string_command_is_a_config_error(tmp_path, value):
    with pytest.raises(ConfigValidationError, match="done_gate_command"):
        _config(tmp_path, f"done_gate_command: {value}")


@pytest.mark.parametrize("value", ["0", "-1", '"300"', "1.5", "true", "abc"])
def test_bad_timeout_is_a_config_error(tmp_path, value):
    with pytest.raises(ConfigValidationError, match="done_gate_timeout_ms"):
        _config(tmp_path, "done_gate_command: ./check.sh", f"done_gate_timeout_ms: {value}")
