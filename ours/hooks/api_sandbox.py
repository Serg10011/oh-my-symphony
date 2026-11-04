"""Bounded local commands behind a closed, per-invocation macOS sandbox.

The Codex executable supplies OS isolation only; it does not run a model.
The controller directory and permission profile are never model-writable.
There is deliberately no unsandboxed fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid


CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
MAX_COMMAND_BYTES = 32768
MAX_OUTPUT_BYTES = 512 * 1024
MAX_TIMEOUT_SECONDS = 1800


class SandboxError(Exception):
    """Public error codes never contain inherited environment or secret values."""


def _directory(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or not path.is_dir():
        raise SandboxError("sandbox_directory_invalid")
    return path


def _toml(value) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(k) + " = " + _toml(v) for k, v in value.items()) + "}"
    return json.dumps(value, ensure_ascii=True)


@dataclass(frozen=True)
class Policy:
    workspace: Path
    control: Path
    read_roots: tuple[Path, ...]
    executable: str = CODEX

    def validate(self) -> None:
        if sys.platform != "darwin":
            raise SandboxError("sandbox_platform_unsupported")
        workspace, control = _directory(self.workspace), _directory(self.control)
        if (workspace == control or control.is_relative_to(workspace)
                or workspace.is_relative_to(control)):
            raise SandboxError("sandbox_control_must_be_separate")
        if stat.S_IMODE(control.stat().st_mode) != 0o700 or control.stat().st_uid != os.getuid():
            raise SandboxError("sandbox_control_not_private")
        if any(control.iterdir()):
            raise SandboxError("sandbox_control_must_be_empty")
        executable = Path(self.executable)
        if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
            raise SandboxError("sandbox_executable_unavailable")
        for raw in self.read_roots:
            root = _directory(raw)
            # A runtime grant must not reopen the controller, source or whole home.
            if (root == Path("/") or Path.home().is_relative_to(root)
                    or workspace.is_relative_to(root) or control.is_relative_to(root)
                    or root.is_relative_to(workspace) or root.is_relative_to(control)):
                raise SandboxError("sandbox_runtime_root_too_broad")

    def argv(self, command: str) -> list[str]:
        self.validate()
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise SandboxError("sandbox_command_invalid")
        try:
            command_bytes = command.encode("utf-8")
        except UnicodeError:
            raise SandboxError("sandbox_command_invalid") from None
        if len(command_bytes) > MAX_COMMAND_BYTES:
            raise SandboxError("sandbox_command_too_large")
        # Resolve Codex configuration in the empty trusted controller, not in the
        # model's checkout. A unique profile cannot inherit a same-named profile.
        name = "symphony_" + uuid.uuid4().hex
        filesystem = {":root": "deny", ":minimal": "read", ":tmpdir": "write",
                      ":slash_tmp": "deny", ":workspace_roots": "read",
                      str(self.workspace): "write"}
        filesystem.update({str(root): "read" for root in self.read_roots})
        profile = {"filesystem": filesystem, "network": {"enabled": False}}
        # The shell's program text is a separate argument, never interpolated in
        # the host shell or in the permission profile. No startup/login scripts.
        return [self.executable, "sandbox", "-P", name, "-C", str(self.control),
                "-c", f"permissions.{name}={_toml(profile)}", "--",
                "/bin/sh", "-c", 'cd "$1" && exec /bin/sh -c "$2"',
                "symphony-command", str(self.workspace), command]


def _environment(policy: Policy) -> dict[str, str]:
    # Allowlist rather than deletion: provider keys, proxies, injected Python,
    # shell startup, Git and loader settings must not reach project commands.
    return {"PATH": os.defpath, "HOME": str(Path.home()), "LANG": "en_US.UTF-8",
            "TMPDIR": str(policy.workspace / ".tmp"), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}


def run(policy: Policy, command: str, *, timeout: int = 600,
        output_limit: int = MAX_OUTPUT_BYTES, cancelled=None) -> dict:
    """Return bounded output, including nonzero exits; never retry a command."""
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise SandboxError("sandbox_timeout_invalid")
    if type(output_limit) is not int or not 1024 <= output_limit <= MAX_OUTPUT_BYTES:
        raise SandboxError("sandbox_output_limit_invalid")
    argv = policy.argv(command)
    temporary = policy.workspace / ".tmp"
    if temporary.is_symlink():
        raise SandboxError("sandbox_temporary_directory_invalid")
    temporary.mkdir(mode=0o700, exist_ok=True)
    started = time.monotonic()
    deadline = started + timeout
    output = bytearray()
    reason = None
    process = None
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(argv, cwd=policy.control, env=_environment(policy),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True,
                                   close_fds=True)
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            if cancelled is not None and cancelled():
                reason = "operator_stopped"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "timeout"
                break
            for key, _ in selector.select(min(remaining, .2)):
                block = os.read(key.fd, min(65536, output_limit - len(output) + 1))
                if not block:
                    selector.unregister(key.fileobj)
                    break
                output.extend(block)
                if len(output) > output_limit:
                    del output[output_limit:]
                    reason = "output_limit"
                    break
            if reason:
                break
        if reason is None:
            # Observe exit without reaping. The child retains its PID until the
            # group cleanup below, so that PID cannot be recycled into an
            # unrelated process group between wait() and killpg().
            while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                if cancelled is not None and cancelled():
                    reason = "operator_stopped"
                    break
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
                time.sleep(.02)
    except OSError:
        raise SandboxError("sandbox_launch_failed") from None
    finally:
        selector.close()
        if process is not None:
            # Also terminate same-group descendants after a normal shell exit.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if process.poll() is None:
                    process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise SandboxError("sandbox_cleanup_unconfirmed") from None
            if process.stdout is not None:
                process.stdout.close()
    return {"exit_code": process.returncode, "stop_reason": reason,
            "output": output.decode("utf-8", errors="replace"),
            "output_bytes": len(output), "output_sha256": hashlib.sha256(output).hexdigest(),
            "truncated": reason == "output_limit", "seconds": round(time.monotonic() - started, 3)}


def probe(policy: Policy, *, python: str) -> dict:
    """Use harmless canaries; an inconclusive boundary test fails closed."""
    import shlex

    outside = policy.control / ("canary-" + uuid.uuid4().hex)
    # The controller is intentionally readable. The outside canary instead lives
    # beside it and outside every declared runtime/workspace grant.
    outside = outside.parent.parent / outside.name
    inside = policy.workspace / ("probe-" + uuid.uuid4().hex)
    link = policy.workspace / ("link-" + uuid.uuid4().hex)
    try:
        outside.write_text("symphony-non-secret-canary", encoding="utf-8")
        link.symlink_to(outside)
        script = '''import json, os, pathlib, socket, sys
inside, outside, link = map(pathlib.Path, sys.argv[1:])
results = {}
inside.write_text("ok")
results["workspace_write_read"] = inside.read_text() == "ok"
for label, path in (("outside_read_denied", outside), ("symlink_read_denied", link)):
    try: path.read_bytes(); results[label] = False
    except PermissionError: results[label] = True
try: outside.write_text("unexpected"); results["outside_write_denied"] = False
except PermissionError: results["outside_write_denied"] = True
s = socket.socket()
try: s.connect(("127.0.0.1", 9)); results["network_denied"] = False
except PermissionError: results["network_denied"] = True
except OSError: results["network_denied"] = False
finally: s.close()
results["credentials_absent"] = not any(k in os.environ for k in (
    "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
print(json.dumps(results))
'''
        command = shlex.join([python, "-I", "-c", script, str(inside), str(outside), str(link)])
        result = run(policy, command, timeout=30)
        try:
            checks = json.loads(result["output"])
        except (ValueError, TypeError):
            raise SandboxError("sandbox_probe_failed") from None
        required = {"workspace_write_read", "outside_read_denied", "symlink_read_denied",
                    "outside_write_denied", "network_denied", "credentials_absent"}
        if (result["exit_code"] != 0 or result["stop_reason"] is not None
                or not isinstance(checks, dict) or set(checks) != required
                or any(value is not True for value in checks.values())):
            raise SandboxError("sandbox_probe_failed")
        return {"checks": checks, "seconds": result["seconds"]}
    finally:
        for path in (outside, inside, link):
            path.unlink(missing_ok=True)
