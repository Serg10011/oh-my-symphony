"""A2/U3 — direct run path: preflight failures and actionable sentences."""

from __future__ import annotations

import asyncio
import importlib
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from symphony.service import ServiceRecord, save_record

cli_main_mod = importlib.import_module("symphony.cli.main")

# The agent command is parsed with POSIX shell rules and later executed
# through bash; a native Windows interpreter path (`C:\...`) loses its
# backslashes there, so tests that need the command to *run* use the
# POSIX-styled rendering of the same executable.
_PYTHON = Path(sys.executable).as_posix()


def _workflow_text(
    *,
    codex_command: str = "codex app-server",
    after_create: str | None = None,
    workspace_root: str = "./tmp_workspaces",
) -> str:
    hook_block = ""
    if after_create is not None:
        hook_block = f"hooks:\n  after_create: '{after_create}'\n\n"
    return (
        "---\n"
        "tracker:\n"
        "  kind: file\n"
        "  board_root: ./kanban\n"
        '  active_states: [Todo, "In Progress"]\n'
        "  terminal_states: [Done]\n"
        "\n"
        "workspace:\n"
        f"  root: {workspace_root}\n"
        "\n"
        f"{hook_block}"
        "agent:\n"
        "  kind: codex\n"
        "\n"
        "codex:\n"
        f"  command: '{codex_command}'\n"
        "---\n"
        "\n"
        "Prompt for {{ issue.identifier }}.\n"
    )


def _board(tmp_path: Path, text: str) -> Path:
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(text, encoding="utf-8")
    (tmp_path / "kanban").mkdir(exist_ok=True)
    return workflow


def test_missing_workflow_prints_actionable_sentence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli_main_mod.main([str(tmp_path / "nope" / "WORKFLOW.md")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "symphony:" in err
    assert "WORKFLOW.md not found" in err
    assert "symphony board init" in err


def test_broken_workflow_prints_actionable_sentence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("---\ntracker: [broken\n---\n", encoding="utf-8")
    rc = cli_main_mod.main([str(workflow)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "symphony:" in err
    assert "could not be loaded" in err
    assert "symphony doctor" in err


def test_preflight_blocks_missing_agent_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _board(
        tmp_path,
        _workflow_text(codex_command="definitely-not-a-real-cli-xyz app-server"),
    )
    rc = cli_main_mod.main([str(workflow)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "symphony:" in err
    assert "definitely-not-a-real-cli-xyz" in err
    assert "$PATH" in err


def test_preflight_blocks_placeholder_after_create(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _board(
        tmp_path,
        _workflow_text(
            codex_command=f"{sys.executable} -m symphony.mock_codex app-server",
            after_create="git clone git@github.com:my-org/my-repo.git .",
        ),
    )
    rc = cli_main_mod.main([str(workflow)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "symphony:" in err
    assert "my-org/my-repo" in err


def test_port_conflict_prints_actionable_sentence_not_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _board(
        tmp_path,
        _workflow_text(
            codex_command=f"{_PYTHON} -m symphony.mock_codex app-server",
            workspace_root=str(tmp_path / "ws"),
        ),
    )
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        rc = cli_main_mod.main([str(workflow), "--port", str(port)])
    finally:
        blocker.close()
    assert rc == 1
    err = capsys.readouterr().err
    assert "symphony:" in err
    assert str(port) in err
    assert "already in use" in err


def test_port_conflict_names_this_workflow_service(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _board(
        tmp_path,
        _workflow_text(
            codex_command=f"{_PYTHON} -m symphony.mock_codex app-server",
            workspace_root=str(tmp_path / "ws"),
        ),
    ).resolve()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    # A live pid that is not this process: the hint must never blame the
    # current process (the orchestrator child sees its own record on startup).
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    save_record(
        ServiceRecord(
            workflow_path=workflow,
            workflow_dir=workflow.parent,
            host="127.0.0.1",
            port=port,
            orchestrator_pid=owner.pid,
            log_path=tmp_path / "log" / "symphony.log",
            started_at="2026-07-03T01:00:00Z",
            orchestrator_command=["symphony", str(workflow)],
        )
    )
    try:
        rc = cli_main_mod.main([str(workflow), "--port", str(port)])
    finally:
        blocker.close()
        owner.kill()
        owner.wait(timeout=5)

    assert rc == 1
    err = capsys.readouterr().err
    assert "owned by this workflow's service" in err
    assert f"pid {owner.pid}" in err
    assert "symphony service status" in err


@pytest.mark.parametrize("requested_port", [52363, 0])
async def test_runtime_port_is_advertised_in_progress_without_rewriting_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requested_port: int,
) -> None:
    monkeypatch.delenv("SYMPHONY_BOARD_URL", raising=False)
    text = _workflow_text().replace("\n---\n", "\nserver:\n  port: 9999\n---\n")
    workflow = _board(tmp_path, text)
    original = workflow.read_bytes()

    class StubOrchestrator:
        def __init__(self, _state):
            self.observers = []

        async def start(self):
            pass

        async def stop(self):
            pass

        def add_observer(self, callback):
            self.observers.append(callback)

        def snapshot(self):
            return {"running": [], "retrying": []}

    class StubRunner:
        async def cleanup(self):
            pass

    monkeypatch.setattr(cli_main_mod, "Orchestrator", StubOrchestrator)
    monkeypatch.setattr(cli_main_mod, "_startup_preflight_failures", lambda _cfg: [])
    monkeypatch.setattr(cli_main_mod, "build_app", lambda orch: orch)
    async def bind_server(orch, _host, port):
        assert port == requested_port
        for callback in orch.observers:
            await callback()
        return StubRunner(), 52363
    monkeypatch.setattr(cli_main_mod, "run_server", bind_server)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda _sig, callback: callback())
    args = cli_main_mod._build_parser().parse_args([
        str(workflow), "--port", str(requested_port), "--no-keep-awake",
    ])

    assert await cli_main_mod._run(args) == 0
    progress = (tmp_path / "WORKFLOW-PROGRESS.md").read_text(encoding="utf-8")
    assert "http://127.0.0.1:52363/" in progress
    assert "http://127.0.0.1:9999/" not in progress
    assert workflow.read_bytes() == original
