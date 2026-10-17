"""Private gated Windows supervisor; control frames never reach backend stdio."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from typing import Any, Callable

from ._windows_job import (
    _api, create_own_job, new_job_identity, open_owner, owner_creation, wait_owner,
)

_TIMEOUT = 10.0
_GO = b"SYMPHONY-GO-v1\n"


def _ready(identity: str) -> bytes:
    return b"SYMPHONY-READY-v1 " + identity.encode("ascii") + b"\n"


async def spawn_backend(
    argv: list[str], *, cwd: str, env: dict[str, str], limit: int,
    pipe_stdin: bool, on_identified: Callable[[int, str | None], bool | None] | None,
    on_started: Callable[[int], bool | None] | None, publish_started: Any,
    require_durable: bool,
    track_process: Callable[[asyncio.subprocess.Process, str], None] | None = None,
) -> tuple[asyncio.subprocess.Process, str]:
    identity = new_job_identity()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "symphony._windows_backend", identity,
        str(os.getpid()), owner_creation(), "pipe" if pipe_stdin else "null", *argv,
        cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=limit,
    )
    if track_process is not None:
        track_process(proc, identity)
    try:
        assert proc.stdout is not None and proc.stdin is not None
        expected = _ready(identity)
        received = await asyncio.wait_for(proc.stdout.readexactly(len(expected)), _TIMEOUT)
        if received != expected:
            raise RuntimeError("invalid Windows backend READY frame")
        acknowledged = None
        if on_identified is not None:
            acknowledged = on_identified(proc.pid, identity)
        elif on_started is not None:
            on_started(proc.pid)
        if acknowledged is False or (require_durable and acknowledged is not True):
            raise RuntimeError("Windows backend ownership was not durably recorded")
        await publish_started(proc.pid)
        # Cancellation requested by a host callback must win before GO.
        await asyncio.sleep(0)
        proc.stdin.write(_GO)
        await asyncio.wait_for(proc.stdin.drain(), _TIMEOUT)
        if not pipe_stdin:
            proc.stdin.close()
            proc.stdin = None
        return proc, identity
    except BaseException:
        from ._shell import terminate_process_tree

        await terminate_process_tree(proc, identity=identity)
        raise


def _main() -> None:
    if sys.platform != "win32" or len(sys.argv) < 6:
        raise ValueError("invalid private Windows backend bootstrap")
    identity, owner_pid, creation, stdin_mode = sys.argv[1:5]
    if stdin_mode not in {"pipe", "null"}:
        raise ValueError("invalid private Windows backend stdin mode")
    import msvcrt

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        msvcrt.setmode(stream.fileno(), os.O_BINARY)
    owner = open_owner(int(owner_pid), creation)
    job = create_own_job(identity)
    monitoring = threading.Event()

    def abort() -> None:
        _api().TerminateJobObject(job, 125)
        os._exit(125)

    def monitor() -> None:
        monitoring.set()
        try:
            wait_owner(owner)
        finally:
            abort()

    threading.Thread(target=monitor, daemon=True).start()
    if not monitoring.wait(1):
        abort()
    timer = threading.Timer(_TIMEOUT, abort)
    timer.daemon = True
    timer.start()
    os.write(sys.stdout.fileno(), _ready(identity))
    frame = b""
    while len(frame) < len(_GO):
        chunk = os.read(sys.stdin.fileno(), len(_GO) - len(frame))
        if not chunk:
            abort()
        frame += chunk
    if frame != _GO:
        abort()
    timer.cancel()
    # No buffered stdin read: bytes after GO belong exclusively to the backend.
    child = subprocess.Popen(
        sys.argv[5:], stdin=None if stdin_mode == "pipe" else subprocess.DEVNULL,
        close_fds=True,
    )
    status = child.wait()
    # Closing our self-member job early would kill us and replace this exit code.
    # Process exit closes its sole noninheritable job handle and remaining descendants.
    _api().ExitProcess(status & 0xFFFFFFFF)
    raise RuntimeError("Windows ExitProcess unexpectedly returned")


if __name__ == "__main__":
    try:
        _main()
    except BaseException as exc:
        print(f"Windows backend containment failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        os._exit(125)
