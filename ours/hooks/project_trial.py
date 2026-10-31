"""Shared setup/test execution and serialized measure history for project operators."""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import project_config as pc

SCRIPT_LIMIT_S = 3600
TAIL_BYTES = 3000
POLL_S = 0.2
RESTRICTED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
PASSED_ENV = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR")


def script_env(python: str, board: Path) -> dict[str, str]:
    """Environment of setup.sh / test.sh in the trial: short PATH, no terminal leftovers."""
    env = {"PATH": RESTRICTED_PATH}
    env.update({key: os.environ[key] for key in PASSED_ENV if key in os.environ})
    env.update(SYMPHONY_PYTHON=python, SYMPHONY_WORKFLOW_DIR=str(board))
    return env


def run_script(script: Path, cwd: Path, env: dict[str, str], limit_s: float):
    """(exit code or None on timeout, seconds, tail, summary) of one script."""
    with tempfile.TemporaryFile() as out:
        started = time.monotonic()
        proc = subprocess.Popen(["/bin/bash", str(script)], cwd=str(cwd), env=env,
                                stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while proc.poll() is None and time.monotonic() - started < limit_s:
                time.sleep(POLL_S)
        finally:
            # Decide before the kill: afterwards the leader is dead and a
            # timeout would look like "exit code -9".
            timed_out = proc.poll() is None
            # Same as check.py: the whole group dies, on timeout and after a
            # finished run (stragglers).
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
        elapsed = time.monotonic() - started
        size = out.seek(0, os.SEEK_END)
        out.seek(max(0, size - TAIL_BYTES))
        tail = out.read().decode("utf-8", errors="replace")
        out.seek(0)
        summary = pc.find_summary(out)
    return (None if timed_out else proc.returncode), elapsed, tail, summary


@contextlib.contextmanager
def history_lock(board: Path):
    """Serialize doctor and delivery writers; the persistent lock is never removed."""
    pc.ensure_state_dir(board)
    fd = os.open(board / pc.STATE_DIR / "baselines.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("история замеров занята другим процессом") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def record_measure(board: Path, measure: dict) -> None:
    """Append a checked candidate measure without replacing the base's baseline.json."""
    with history_lock(board):
        records = pc.read_baselines(board)
        if records is None:
            old = pc.read_baseline(board)
            records = [old] if pc.is_real_baseline(old) else []
        data = pc.baselines_text(pc.add_baseline(records, measure)).encode("utf-8")
        path = pc.baselines_path(board)
        fd, name = tempfile.mkstemp(prefix=".measure-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
