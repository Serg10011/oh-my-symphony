"""Lifecycle hardening tests for backend subprocess handling."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from tests._backend_windows_stub import mocked_windows_transport  # noqa: F401

import symphony._shell as shell_module
import symphony.backends.codex as codex_module
import symphony.backends.per_turn as per_turn_module
from symphony.backends import (
    EVENT_TURN_COMPLETED,
    MALFORMED_LINE_LIMIT,
    POST_STREAM_REAP_TIMEOUT_S,
    BackendInit,
    TurnResult,
)
from symphony.backends.claude_code import ClaudeCodeBackend
from symphony.backends.codex import CodexAppServerBackend
from symphony.backends.gemini import GeminiBackend
from symphony.backends.pi import PiBackend
from symphony.errors import CodexNotFound, PortExit, ResponseError, TurnFailed
from tests.test_backends import (
    _FakeProcess,
    _FakeStdin,
    _FakeStream,
    _make_cfg,
    _noop_event,
)


class _PipeProcess(_FakeProcess):
    def __init__(
        self,
        *,
        stdout_lines: list[bytes] | None = None,
        stderr_lines: list[bytes] | None = None,
        returncode: int | None = None,
    ) -> None:
        super().__init__()
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(lines=stdout_lines or [])
        self.stderr = None if stderr_lines is None else _FakeStream(lines=stderr_lines)
        self.returncode = returncode


@pytest.mark.asyncio
async def test_per_turn_actual_spawn_holds_launch_guard_through_pid_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []
    process = _PipeProcess(stdout_lines=[], stderr_lines=[])

    async def fake_spawn(*args, **kwargs):
        del args, kwargs
        events.append("spawn")
        return process

    @contextlib.asynccontextmanager
    async def guard():
        events.append("enter")

        async def publish(pid: int) -> None:
            events.append(("publish", pid))

        yield publish
        events.append("exit")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    cfg = _make_cfg("gemini", workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = GeminiBackend(
        BackendInit(
            cfg=cfg, cwd=cwd, workspace_root=tmp_path,
            on_event=_noop_event, process_launch_guard=guard,
        )
    )
    spawned = await backend._spawn("true", pipe_stdin=False)
    assert spawned is process
    assert events == ["enter", "spawn", ("publish", process.pid), "exit"]


@pytest.mark.asyncio
async def test_per_turn_guard_exit_cancellation_retains_preawait_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _PipeProcess(stdout_lines=[], stderr_lines=[])
    identified: list[tuple[int, str | None]] = []

    async def fake_spawn(*_args, **_kwargs):
        return process

    @contextlib.asynccontextmanager
    async def cancelling_guard():
        async def publish(_pid: int) -> None:
            return None

        try:
            yield publish
        finally:
            raise asyncio.CancelledError

    async def cancelled_cleanup(_proc, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(per_turn_module, "process_identity", lambda _pid: "spawn-owner")
    monkeypatch.setattr(
        per_turn_module, "terminate_process_tree", cancelled_cleanup
    )
    cfg = _make_cfg("gemini", workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = GeminiBackend(BackendInit(
        cfg=cfg,
        cwd=cwd,
        workspace_root=tmp_path,
        on_event=_noop_event,
        on_process_identified=lambda pid, identity: identified.append((pid, identity)),
        process_launch_guard=cancelling_guard,
    ))

    with pytest.raises(asyncio.CancelledError):
        await backend._spawn("true", pipe_stdin=False)

    assert identified == [(process.pid, "spawn-owner")]
    assert backend._active_proc is process
    assert backend._active_process_identity == "spawn-owner"
    assert backend._cleanup_unconfirmed


@pytest.mark.asyncio
async def test_codex_publication_failure_retains_original_identity_when_cleanup_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _PipeProcess(stdout_lines=[], stderr_lines=[])
    identified: list[tuple[int, str | None]] = []

    async def fake_spawn(*_args, **_kwargs):
        return process

    @contextlib.asynccontextmanager
    async def rejecting_guard():
        async def publish(_pid: int) -> None:
            raise RuntimeError("revoked")

        yield publish

    async def cancelled_cleanup(_proc, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(codex_module, "process_identity", lambda _pid: "spawn-owner")
    monkeypatch.setattr(codex_module, "terminate_process_tree", cancelled_cleanup)
    cfg = _make_cfg("codex", workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = CodexAppServerBackend(BackendInit(
        cfg=cfg,
        cwd=cwd,
        workspace_root=tmp_path,
        on_event=_noop_event,
        on_process_identified=lambda pid, identity: identified.append((pid, identity)),
        process_launch_guard=rejecting_guard,
    ))

    with pytest.raises(asyncio.CancelledError):
        await backend.start()

    assert identified == [(process.pid, "spawn-owner")]
    assert backend._process is process
    assert backend._process_identity == "spawn-owner"
    assert backend._cleanup_unconfirmed


@pytest.mark.asyncio
async def test_persistent_actual_spawn_reaps_when_guard_publication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _PipeProcess(stdout_lines=[], stderr_lines=[])
    reaped: list[int] = []

    async def fake_spawn(*args, **kwargs):
        del args, kwargs
        return process

    async def fake_reap(proc, **_kwargs):
        reaped.append(proc.pid)
        proc.returncode = -15
        return -15

    @contextlib.asynccontextmanager
    async def guard():
        async def publish(pid: int) -> None:
            del pid
            raise RuntimeError("revoked")
        yield publish

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("symphony.backends.codex.terminate_process_tree", fake_reap)
    cfg = _make_cfg("codex", workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = CodexAppServerBackend(
        BackendInit(
            cfg=cfg, cwd=cwd, workspace_root=tmp_path,
            on_event=_noop_event, process_launch_guard=guard,
        )
    )
    with pytest.raises(RuntimeError, match="revoked"):
        await backend.start()
    assert reaped == [process.pid]
    assert backend.pid is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "backend_cls", "method", "expected_exc"),
    [
        ("codex", CodexAppServerBackend, "start", CodexNotFound),
        ("claude", ClaudeCodeBackend, "run_turn", PortExit),
        ("gemini", GeminiBackend, "run_turn", PortExit),
        ("pi", PiBackend, "run_turn", PortExit),
    ],
)
async def test_backend_spawns_own_process_group_on_posix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    backend_cls: type,
    method: str,
    expected_exc: type[BaseException],
) -> None:
    captured: list[dict[str, Any]] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        del args
        captured.append(kwargs)
        raise FileNotFoundError("missing bash")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    cfg = _make_cfg(kind, workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = backend_cls(
        BackendInit(cfg=cfg, cwd=cwd, workspace_root=tmp_path, on_event=_noop_event)
    )

    with pytest.raises(expected_exc):
        if method == "start":
            await backend.start()
        else:
            await backend.run_turn(prompt="hi", is_continuation=False)

    assert captured
    assert captured[0]["start_new_session"] is (os.name == "posix")


@pytest.mark.asyncio
async def test_terminate_process_tree_escalates_sigterm_to_sigkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProcess()
    waits: list[float | None] = []
    signals: list[tuple[int, int]] = []
    taskkills: list[int] = []

    async def fake_safe_proc_wait(process, *, timeout=None):
        waits.append(timeout)
        return None if len(waits) == 1 else -9

    monkeypatch.setattr(shell_module, "safe_proc_wait", fake_safe_proc_wait)
    monkeypatch.setattr(
        shell_module,
        "_signal_process_group",
        lambda pid, sig: signals.append((pid, sig)) or True,
    )
    if sys.platform == "win32":
        # Keep the unit test from taskkill-ing an unrelated real pid.
        monkeypatch.setattr(
            shell_module,
            "_taskkill_tree",
            lambda pid, *, force=True: taskkills.append(pid) or True,
        )
        rc = await shell_module.terminate_process_tree(proc)
        expected_waits = [2.0, 5.0]
    else:
        group_states = iter((True, True, False))
        monkeypatch.setattr(
            shell_module,
            "process_group_exists",
            lambda _pid: next(group_states),
        )
        rc = await shell_module.terminate_process_tree(
            proc, term_timeout=0, kill_timeout=0
        )
        expected_waits = [0, 0]

    assert rc == -9
    assert waits == expected_waits
    if sys.platform == "win32":
        # taskkill /T /F goes first; because the (faked) taskkill did not
        # reap the process, the terminate -> kill fallback ladder runs.
        assert taskkills == [proc.pid]
        assert signals == []
        assert proc.terminated is True
        assert proc.killed is True
    else:
        assert signals == [(proc.pid, signal.SIGTERM), (proc.pid, signal.SIGKILL)]
        assert proc.terminated is False
        assert proc.killed is False


def test_kill_process_group_uses_platform_native_tree_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[tuple[int, ...]] = []

    if sys.platform == "win32":
        def fake_taskkill(pid: int, *, force: bool = True) -> bool:
            killed.append((pid, force))
            return True

        monkeypatch.setattr(shell_module, "_taskkill_tree", fake_taskkill)
        assert shell_module.kill_process_group(4242) is True
        assert killed == [(4242, True)]
    else:
        monkeypatch.setattr(
            shell_module,
            "_signal_process_group",
            lambda pid, sig: killed.append((pid, sig)) or True,
        )
        assert shell_module.kill_process_group(4242) is True
        assert killed == [(4242, signal.SIGKILL)]


def test_kill_process_group_skips_kill_on_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded pid whose fingerprint changed must never be signalled.

    The OS reuses pids; killing on a stale recorded pid would take down an
    unrelated process (on win32, a whole unrelated tree via taskkill /T).
    The identity gate runs before any platform dispatch, so this test is
    platform-independent.
    """
    signalled: list[int] = []
    if sys.platform == "win32":
        monkeypatch.setattr(
            shell_module,
            "_taskkill_tree",
            lambda pid, *, force=True: signalled.append(pid) or True,
        )
    else:
        monkeypatch.setattr(
            shell_module,
            "_signal_process_group",
            lambda pid, sig: signalled.append(pid) or True,
        )
    monkeypatch.setattr(
        shell_module, "process_identity", lambda pid: "live-fingerprint"
    )

    killed = shell_module.kill_process_group(4242, identity="recorded-fingerprint")

    assert killed is False
    assert signalled == []


def test_kill_process_group_proceeds_on_identity_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching fingerprint proves the recorded incarnation still lives."""
    signalled: list[int] = []
    if sys.platform == "win32":
        monkeypatch.setattr(
            shell_module,
            "_taskkill_tree",
            lambda pid, *, force=True: signalled.append(pid) or True,
        )
    else:
        monkeypatch.setattr(
            shell_module,
            "_signal_process_group",
            lambda pid, sig: signalled.append(pid) or True,
        )
    monkeypatch.setattr(
        shell_module, "process_identity", lambda pid: "matching-fingerprint"
    )

    killed = shell_module.kill_process_group(4242, identity="matching-fingerprint")

    assert killed is True
    assert signalled == [4242]


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX-only")
def test_process_group_exists_treats_contradictory_empty_status_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyStatus:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(shell_module.os, "killpg", lambda _pid, _sig: None)
    monkeypatch.setattr(
        shell_module.subprocess, "run", lambda *_args, **_kwargs: EmptyStatus()
    )

    assert shell_module.process_group_exists(4242) is None


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX-only")
def test_process_group_exists_treats_failed_empty_status_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedInspection:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(shell_module.os, "killpg", lambda _pid, _sig: None)
    monkeypatch.setattr(
        shell_module.subprocess, "run", lambda *_args, **_kwargs: FailedInspection()
    )

    assert shell_module.process_group_exists(4242) is None


async def _wait_for_file(path: Path, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path.name}")


@pytest.mark.asyncio
async def test_terminate_process_tree_kills_real_grandchild(tmp_path: Path) -> None:
    """Regression: the agent CLI is a grandchild of the spawned wrapper.

    Backends spawn ``bash -lc <agent cli>``, so the real workload runs one
    level below the process Symphony holds. Terminating only the direct
    child (the Windows bug this guards against) leaves the grandchild
    running. Uses a file handshake instead of pid-liveness APIs so the
    same assertions hold on Windows and on the POSIX CI runners.
    """
    (tmp_path / "tree_child.py").write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "grandchild = subprocess.Popen(\n"
        "    [sys.executable, str(Path(__file__).with_name('tree_grandchild.py'))]\n"
        ")\n"
        "print(grandchild.pid, flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    (tmp_path / "tree_grandchild.py").write_text(
        "import time\n"
        "from pathlib import Path\n"
        "here = Path(__file__).parent\n"
        "(here / 'grandchild_alive').write_text('x', encoding='utf-8')\n"
        "while not (here / 'grandchild_stop').exists():\n"
        "    time.sleep(0.05)\n"
        "(here / 'grandchild_exited').write_text('x', encoding='utf-8')\n",
        encoding="utf-8",
    )
    spawn_kwargs: dict[str, Any] = {}
    if os.name == "posix":
        # Mirror the backends: the child leads its own process group so a
        # group kill can never reach the test runner.
        spawn_kwargs["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(tmp_path / "tree_child.py"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        **spawn_kwargs,
    )
    grandchild_pid: int | None = None
    try:
        assert proc.stdout is not None  # spawned with stdout=PIPE above
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=15)
        grandchild_pid = int(line.strip())
        # Positive control: the grandchild must actually be running before
        # we terminate the tree, or the assertions below prove nothing.
        await _wait_for_file(tmp_path / "grandchild_alive", timeout_s=15)

        rc = await asyncio.wait_for(
            shell_module.terminate_process_tree(proc), timeout=30
        )
        assert rc is not None or proc.returncode is not None

        # If the grandchild survived the tree kill it notices this stop
        # file within ~0.05s and writes its exit marker — a generous 3s
        # window with no marker proves it was taken down with the tree.
        (tmp_path / "grandchild_stop").write_text("x", encoding="utf-8")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            assert not (tmp_path / "grandchild_exited").exists(), (
                f"grandchild pid {grandchild_pid} survived tree termination"
            )
            await asyncio.sleep(0.1)
    finally:
        # Self-service exit first: a surviving grandchild stops on its own
        # even if the hard kills below somehow miss it.
        with contextlib.suppress(OSError):
            (tmp_path / "grandchild_stop").write_text("x", encoding="utf-8")
        for pid in (proc.pid, grandchild_pid):
            if pid is None:
                continue
            with contextlib.suppress(OSError):
                if sys.platform == "win32":
                    shell_module._taskkill_tree(pid)
                else:
                    os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await shell_module.safe_proc_wait(proc, timeout=1.0)


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
@pytest.mark.asyncio
async def test_terminate_process_tree_retains_unowned_group_after_parent_already_exited(
    tmp_path: Path,
) -> None:
    child = tmp_path / "term_resistant_child.py"
    ready = tmp_path / "term_resistant.ready"
    child.write_text(
        "import signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "exited_parent.py"
    parent.write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}])\n",
        encoding="utf-8",
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(parent),
        start_new_session=True,
    )
    try:
        assert await asyncio.wait_for(proc.wait(), timeout=5) == 0
        await _wait_for_file(ready, timeout_s=5)
        assert shell_module.process_group_exists(proc.pid) is True

        cfg = _make_cfg("gemini", workspace_root=tmp_path)
        backend = GeminiBackend(
            BackendInit(
                cfg=cfg,
                cwd=tmp_path,
                workspace_root=tmp_path,
                on_event=_noop_event,
            )
        )
        backend._active_proc = proc

        with pytest.raises(RuntimeError, match="could not be confirmed"):
            await backend.stop()
        assert backend._active_proc is proc
        assert shell_module.process_group_exists(proc.pid) is True
    finally:
        if shell_module.process_group_exists(proc.pid) is not False:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
@pytest.mark.asyncio
async def test_terminate_process_tree_signals_own_live_group_when_ps_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ps-less host must still stop our own unreaped worker.

    The leader is our child and not yet reaped, so its pid (and pgid) cannot
    have been reused; ``ps`` being unavailable is no reason to send nothing.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)",
        start_new_session=True,
    )
    try:
        empty_path = tmp_path / "no-ps"
        empty_path.mkdir()
        monkeypatch.setenv("PATH", str(empty_path))
        # Precondition: the group is live but ps cannot describe it.
        assert shell_module.process_group_exists(proc.pid) is None

        rc = await asyncio.wait_for(
            shell_module.terminate_process_tree(proc), timeout=30
        )

        # The exact code depends on which reaper wins (safe_proc_wait's
        # waitpid gives -SIGTERM; asyncio's watcher then records 255), so
        # pin the contract: a confirmed exit status equal to the child's.
        assert rc is not None
        assert proc.returncode == rc
        assert shell_module.process_group_exists(proc.pid) is False
    finally:
        if proc.returncode is None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            await shell_module.safe_proc_wait(proc, timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
@pytest.mark.asyncio
async def test_terminate_process_tree_stays_unconfirmed_when_ps_is_missing_and_group_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ps a surviving group member must keep cleanup unconfirmed."""
    ready = tmp_path / "member.ready"
    member = tmp_path / "term_ignoring_member.py"
    member.write_text(
        "import signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    leader = tmp_path / "leader.py"
    leader.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(member)!r}])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(leader), start_new_session=True,
    )
    try:
        await _wait_for_file(ready, timeout_s=10)
        empty_path = tmp_path / "no-ps"
        empty_path.mkdir()
        monkeypatch.setenv("PATH", str(empty_path))
        assert shell_module.process_group_exists(proc.pid) is None

        rc = await asyncio.wait_for(
            shell_module.terminate_process_tree(
                proc, term_timeout=0.5, kill_timeout=0.5
            ),
            timeout=30,
        )

        assert rc is None
        # The leader itself was signalled and reaped.
        assert proc.returncode is not None
        # The TERM-ignoring member is still in the group.
        os.killpg(proc.pid, 0)
    finally:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        if proc.returncode is None:
            await shell_module.safe_proc_wait(proc, timeout=5)


@pytest.mark.asyncio
async def test_terminate_process_tree_skips_already_dead_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProcess()
    proc.returncode = 0
    waits: list[object] = []
    signals: list[object] = []
    monkeypatch.setattr(
        shell_module,
        "safe_proc_wait",
        lambda *args, **kwargs: waits.append(args),
    )
    monkeypatch.setattr(
        shell_module,
        "_signal_process_group",
        lambda *args, **kwargs: signals.append(args),
    )
    monkeypatch.setattr(shell_module, "process_group_exists", lambda _pid: False)

    rc = await shell_module.terminate_process_tree(proc)

    assert rc == 0
    assert waits == []
    assert signals == []


@pytest.mark.asyncio
async def test_codex_stdout_eof_fails_completion_waiter_promptly(tmp_path: Path) -> None:
    cfg = _make_cfg("codex", workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = CodexAppServerBackend(
        BackendInit(cfg=cfg, cwd=cwd, workspace_root=tmp_path, on_event=_noop_event)
    )
    backend._process = _PipeProcess(  # type: ignore[assignment]
        stdout_lines=[b""],
        returncode=1,
    )
    waiter = backend._arm_completion_waiter()

    await backend._stdout_reader()

    with pytest.raises(TurnFailed, match="closed stdout"):
        await waiter


@pytest.mark.asyncio
async def test_codex_stop_rejects_unconfirmed_group_after_parent_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg("codex", workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = CodexAppServerBackend(
        BackendInit(cfg=cfg, cwd=cwd, workspace_root=tmp_path, on_event=_noop_event)
    )
    backend._process = _PipeProcess(returncode=0)  # type: ignore[assignment]

    async def unconfirmed(_process, **_kwargs):
        return None

    monkeypatch.setattr(codex_module, "terminate_process_tree", unconfirmed)

    with pytest.raises(RuntimeError, match="could not be confirmed"):
        await backend.stop()


@pytest.mark.asyncio
async def test_per_turn_normal_completion_confirms_group_before_forgetting_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg("gemini", workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = GeminiBackend(
        BackendInit(cfg=cfg, cwd=cwd, workspace_root=tmp_path, on_event=_noop_event)
    )
    proc = _PipeProcess(returncode=0)

    async def spawn(*_args, **_kwargs):
        backend._active_proc = proc  # type: ignore[assignment]
        backend._active_process_identity = "spawn-owner"
        return proc

    async def drive(_proc):
        return TurnResult("turn_completed", "turn-1")

    reaped: list[object] = []

    async def reap(owned):
        reaped.append(owned)

    monkeypatch.setattr(backend, "_spawn", spawn)
    monkeypatch.setattr(backend, "_drive_turn", drive)
    monkeypatch.setattr(backend, "_reap", reap)

    result = await backend.run_turn(prompt="work", is_continuation=False)

    assert result.status == "turn_completed"
    assert reaped == [proc]
    assert backend._active_proc is None


@pytest.mark.asyncio
async def test_per_turn_unconfirmed_normal_return_blocks_next_turn_until_stop_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg("gemini", workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = GeminiBackend(
        BackendInit(cfg=cfg, cwd=cwd, workspace_root=tmp_path, on_event=_noop_event)
    )
    proc = _PipeProcess(returncode=0)
    spawn_calls = 0

    async def spawn(*_args, **_kwargs):
        nonlocal spawn_calls
        spawn_calls += 1
        backend._active_proc = proc  # type: ignore[assignment]
        backend._active_process_identity = "spawn-owner"
        return proc

    async def drive(_proc):
        return TurnResult("turn_completed", "turn-1")

    async def unconfirmed_reap(_proc):
        raise RuntimeError("backend process cleanup could not be confirmed")

    monkeypatch.setattr(backend, "_spawn", spawn)
    monkeypatch.setattr(backend, "_drive_turn", drive)
    monkeypatch.setattr(backend, "_reap", unconfirmed_reap)

    with pytest.raises(RuntimeError, match="could not be confirmed"):
        await backend.run_turn(prompt="work", is_continuation=False)
    assert backend._active_proc is proc
    with pytest.raises(ResponseError, match="not confirmed"):
        await backend.run_turn(prompt="unsafe next turn", is_continuation=True)
    assert spawn_calls == 1
    assert backend._active_proc is proc

    async def confirmed_cleanup(_proc, **_kwargs):
        return 0

    monkeypatch.setattr(per_turn_module, "terminate_process_tree", confirmed_cleanup)
    await backend.stop()
    assert backend._active_proc is None


@pytest.mark.asyncio
async def test_per_turn_preserves_spawn_identity_across_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This case models POSIX process-group reuse, independently of the host OS.
    monkeypatch.setattr(shell_module.sys, "platform", "linux")
    cfg = _make_cfg("gemini", workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    proc = _PipeProcess(returncode=0)
    identity_reads: list[int] = []
    signals: list[tuple[int, int]] = []

    async def spawn(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        per_turn_module,
        "process_identity",
        lambda pid: identity_reads.append(pid) or "spawn-owner",
    )
    monkeypatch.setattr(shell_module, "process_group_exists", lambda _pid: True)
    monkeypatch.setattr(
        shell_module, "process_identity", lambda _pid: "replacement-owner"
    )
    monkeypatch.setattr(
        shell_module,
        "_signal_process_group",
        lambda pid, sig: signals.append((pid, sig)) or True,
    )
    backend = GeminiBackend(
        BackendInit(cfg=cfg, cwd=cwd, workspace_root=tmp_path, on_event=_noop_event)
    )

    async def drive(_proc):
        return TurnResult("turn_completed", "turn-1")

    monkeypatch.setattr(backend, "_drive_turn", drive)

    with pytest.raises(RuntimeError, match="could not be confirmed"):
        await backend.run_turn(prompt="work", is_continuation=False)

    assert identity_reads == [proc.pid]
    assert backend._active_process_identity == "spawn-owner"
    assert backend._active_proc is proc
    assert signals == []
    with pytest.raises(ResponseError, match="not confirmed"):
        await backend.run_turn(prompt="unsafe", is_continuation=True)


@pytest.mark.asyncio
async def test_per_turn_failed_stop_retains_process_for_retry_and_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg("gemini", workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = GeminiBackend(
        BackendInit(cfg=cfg, cwd=cwd, workspace_root=tmp_path, on_event=_noop_event)
    )
    proc = _PipeProcess(returncode=0)
    backend._active_proc = proc  # type: ignore[assignment]
    outcomes = iter((None, 0))
    calls: list[object] = []

    async def cleanup(owned, **_kwargs):
        calls.append(owned)
        return next(outcomes)

    monkeypatch.setattr(per_turn_module, "terminate_process_tree", cleanup)

    with pytest.raises(RuntimeError, match="could not be confirmed"):
        await backend.stop()
    assert backend._active_proc is proc
    with pytest.raises(ResponseError, match="closed"):
        await backend.run_turn(prompt="unsafe next turn", is_continuation=True)
    assert backend._active_proc is proc
    await backend.stop()
    assert calls == [proc, proc]
    assert backend._active_proc is None


@pytest.mark.asyncio
async def test_codex_stop_never_signals_reused_group_after_parent_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Windows jobs have separate exact-token guards in test_windows_containment.
    monkeypatch.setattr(shell_module.sys, "platform", "linux")
    cfg = _make_cfg("codex", workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = CodexAppServerBackend(
        BackendInit(cfg=cfg, cwd=cwd, workspace_root=tmp_path, on_event=_noop_event)
    )
    proc = _PipeProcess(returncode=0)
    backend._process = proc  # type: ignore[assignment]
    backend._process_identity = "recorded"
    checked: list[int] = []
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(shell_module, "process_group_exists", lambda _pid: True)
    monkeypatch.setattr(
        shell_module,
        "process_identity",
        lambda pid: checked.append(pid) or "replacement",
    )
    monkeypatch.setattr(
        shell_module,
        "_signal_process_group",
        lambda pid, sig: signalled.append((pid, sig)) or True,
    )

    with pytest.raises(RuntimeError, match="could not be confirmed"):
        await backend.stop()

    assert checked == [proc.pid]
    assert signalled == []
    assert backend._process is proc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "backend_cls", "valid_line"),
    [
        ("claude", ClaudeCodeBackend, b'{"type":"assistant","message":{}}\n'),
        ("pi", PiBackend, b'{"type":"message_start"}\n'),
    ],
)
async def test_stream_malformed_streak_sets_corrupt_after_limit(
    tmp_path: Path, kind: str, backend_cls: type, valid_line: bytes
) -> None:
    del valid_line
    cfg = _make_cfg(kind, workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = backend_cls(
        BackendInit(cfg=cfg, cwd=cwd, workspace_root=tmp_path, on_event=_noop_event)
    )
    proc = _PipeProcess(stdout_lines=[b"{bad json\n"] * MALFORMED_LINE_LIMIT)

    await backend._consume_stream(proc)  # type: ignore[attr-defined]

    assert backend._stream_corrupt is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "backend_cls", "valid_line"),
    [
        ("claude", ClaudeCodeBackend, b'{"type":"assistant","message":{}}\n'),
        ("pi", PiBackend, b'{"type":"message_start"}\n'),
    ],
)
async def test_stream_malformed_streak_resets_after_valid_line(
    tmp_path: Path, kind: str, backend_cls: type, valid_line: bytes
) -> None:
    cfg = _make_cfg(kind, workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = backend_cls(
        BackendInit(cfg=cfg, cwd=cwd, workspace_root=tmp_path, on_event=_noop_event)
    )
    proc = _PipeProcess(
        stdout_lines=([b"{bad json\n"] * (MALFORMED_LINE_LIMIT - 1))
        + [valid_line]
        + ([b"{still bad\n"] * (MALFORMED_LINE_LIMIT - 1))
    )

    await backend._consume_stream(proc)  # type: ignore[attr-defined]

    assert backend._stream_corrupt is None


@pytest.mark.asyncio
async def test_claude_bounded_post_stream_reap_terminates_lingering_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[dict] = []

    def on_event(event: dict) -> "asyncio.Future[None]":
        events.append(event)
        fut: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        fut.set_result(None)
        return fut

    proc = _PipeProcess(
        stdout_lines=[
            (
                b'{"type":"result","subtype":"success","result":"ok",'
                b'"session_id":"s1","usage":{}}\n'
            )
        ],
        stderr_lines=[],
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return proc

    waits: list[float | None] = []
    terminated: list[int] = []

    async def fake_safe_proc_wait(process, *, timeout=None):
        waits.append(timeout)
        return None

    async def fake_terminate_process_tree(process, **_kwargs):
        terminated.append(process.pid)
        process.returncode = 0
        return 0

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    # claude streams through per_turn.JsonlStreamBackend, so both the
    # bounded post-stream wait and the reap resolve in per_turn's namespace.
    monkeypatch.setattr(per_turn_module, "safe_proc_wait", fake_safe_proc_wait)
    monkeypatch.setattr(
        per_turn_module,
        "terminate_process_tree",
        fake_terminate_process_tree,
    )

    cfg = _make_cfg("claude", workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = ClaudeCodeBackend(
        BackendInit(cfg=cfg, cwd=cwd, workspace_root=tmp_path, on_event=on_event)
    )

    result = await backend.run_turn(prompt="hi", is_continuation=False)

    assert result.status == EVENT_TURN_COMPLETED
    assert waits == [POST_STREAM_REAP_TIMEOUT_S]
    # The stream path performs its bounded reap, then the shared run-turn
    # finalizer independently confirms ownership cleanup before forgetting it.
    assert terminated == [proc.pid, proc.pid]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["stdin", "stream"])
async def test_per_turn_failure_reaps_live_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    cfg = _make_cfg("claude", workspace_root=tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = ClaudeCodeBackend(
        BackendInit(cfg=cfg, cwd=cwd, workspace_root=tmp_path, on_event=_noop_event)
    )
    proc = _PipeProcess(returncode=None)
    reaped = []

    async def spawn(*args, **kwargs):
        return proc

    async def fail(*args, **kwargs):
        raise PortExit("stdin closed") if failure_stage == "stdin" else TurnFailed("bad event")

    async def reap(process, **_kwargs):
        reaped.append(process.pid)
        process.returncode = -15
        return -15

    monkeypatch.setattr(backend, "_spawn", spawn)
    monkeypatch.setattr(backend, "_write_prompt" if failure_stage == "stdin" else "_drive_turn", fail)
    monkeypatch.setattr(per_turn_module, "terminate_process_tree", reap)

    with pytest.raises((PortExit, TurnFailed)):
        await backend.run_turn(prompt="hello", is_continuation=False)

    assert reaped == [proc.pid]
    assert backend.pid is None
