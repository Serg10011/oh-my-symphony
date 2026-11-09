"""Inspect Antigravity's startup contract without sending any model prompt.

This is a readiness diagnostic, not a worker adapter. The 1.5.3 init tool list
is a general catalog; effective permissions require a separate execution gate.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time

MODEL = "gemini-3.8-flash-high"
AGENT = "symphony-controlled-worker"
MAX_BYTES = 256 * 1024
SETTINGS = {
    "modelProvider": "", "useG1Credits": False, "toolPermission": "strict",
    "allowNonWorkspaceAccess": False,
    "permissions": {"allow": [], "ask": [], "deny": [
        "read_file(*)", "write_file(*)", "command(*)", "read_url(*)", "execute_url(*)", "mcp(*)"]},
    "statusLine": {"enabled": False}, "title": {"enabled": False},
}
AGENT_DOCUMENT = """---
name: symphony-controlled-worker
description: Text worker whose requested actions are executed by Symphony
tools: [finish]
mainAgent: true
subagent: false
commandExecutionPolicy: "off"
---
Return structured text only. Symphony executes permitted file operations.
"""


class ProbeError(Exception):
    """Public fixed codes, never raw CLI output or local credentials."""


def check_customizations(home):
    # No prompt is sent, but a startup hook or MCP process could still run.
    # Until discovery can be disabled reliably, refuse active customizations.
    base = Path(home) / ".gemini"
    for folder in (base / "config", base / "antigravity-cli", base):
        for name in ("hooks.json", "mcp_config.json"):
            path = folder / name
            if path.is_symlink():
                raise ProbeError("flash_customizations_active")
            if path.exists():
                try:
                    if path.stat().st_size > MAX_BYTES:
                        raise ProbeError("flash_customizations_active")
                    text = path.read_text().strip()
                    value = json.loads(text) if text else {}
                    if not isinstance(value, dict) or any(value.values()):
                        raise ProbeError("flash_customizations_active")
                except (OSError, ValueError):
                    raise ProbeError("flash_customizations_unreadable") from None
        for name in ("plugins", "agents", "rules", "skills"):
            path, manifest = folder / name, folder / (name + ".json")
            if (path.is_symlink() or manifest.exists() or manifest.is_symlink()
                    or path.exists() and (not path.is_dir() or any(path.iterdir()))):
                raise ProbeError("flash_customizations_active")


def subscription_executable(executable=None):
    executable = Path(executable) if executable is not None else Path.home() / ".local/bin/agy"
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ProbeError("flash_cli_unavailable")
    check_customizations(Path.home())
    return executable


def inspect_init(event, directory):
    value = event.get("init")
    if (event.get("event") != "init" or not isinstance(value, dict)
            or value.get("model") != MODEL or value.get("agent") != AGENT
            or value.get("permission_mode") != "strict"
            or value.get("cwd") != str(directory)
            or not isinstance(value.get("tools"), list)
            or any(not isinstance(tool, str) for tool in value["tools"])):
        raise ProbeError("flash_startup_contract_mismatch")
    extra = sorted(set(value["tools"]) - {"finish"})
    return {"startup_contract_matches": True, "model": MODEL, "effort": "high",
            "native_tools": sorted(set(value["tools"])), "unexpected_tools": extra,
            "tool_catalog_matches_profile": not extra, "effective_permissions_verified": False,
            "reason": "flash_effective_permissions_unverified",
            "ready_for_tasks": False, "model_prompt_sent": False,
            "extra_credits_requested": False,
            "quota": {"five_hour_remaining": None, "weekly_remaining": None,
                      "source": "not_queried", "manual_command": "/usage",
                      "documentation": "https://antigravity.google/docs/cli/commands/usage/"}}


def _environment():
    # Reuse cached Google login through the supported CLI, never export tokens
    # or forward OpenAI/DeepSeek keys, cloud API keys or proxy settings.
    return {key: value for key, value in os.environ.items()
            if key in {"HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME"}}


def _inspect(executable, directory, timeout, cancelled):
    agents = directory / ".agents" / "agents"
    agents.mkdir(parents=True, mode=0o700)
    (agents / (AGENT + ".md")).write_text(AGENT_DOCUMENT)
    argv = [str(executable), "--input-format", "stream-json", "--output-format", "stream-json",
            "--agent", AGENT, "--model", MODEL, "--effort", "high", "--disable-slash-commands",
            "--settings", json.dumps(SETTINGS), "--print="]
    process = None
    selector = selectors.DefaultSelector()
    buffered, received = bytearray(), 0
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(argv, cwd=directory, env=_environment(),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, close_fds=True)
        for stream in (process.stdout, process.stderr):
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            if cancelled is not None and cancelled():
                raise ProbeError("flash_probe_cancelled")
            if time.monotonic() >= deadline:
                raise ProbeError("flash_probe_timeout")
            for key, _ in selector.select(.1):
                block = os.read(key.fd, 65536)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                received += len(block)
                if received > MAX_BYTES:
                    raise ProbeError("flash_probe_output_limit")
                if key.fileobj != process.stdout:
                    continue  # Never persist or expose diagnostic text from the CLI.
                buffered.extend(block)
                while b"\n" in buffered:
                    raw, _, remainder = buffered.partition(b"\n")
                    buffered = bytearray(remainder)
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise ProbeError("flash_probe_invalid_output")
                    return inspect_init(event, directory)
        raise ProbeError("flash_probe_no_init")
    except (OSError, ValueError, RecursionError):
        raise ProbeError("flash_probe_failed") from None
    finally:
        selector.close()
        if process is not None:
            # No wait/poll before group termination: retain the owned PID until
            # cleanup so it cannot be recycled to an unrelated process group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Some managed hosts reject signalling an exited child group.
                # Reap that child; if still alive, terminate the owned process.
                if process.poll() is None:
                    process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise ProbeError("flash_probe_cleanup_unconfirmed") from None
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()


def probe(executable=None, *, timeout=30, cancelled=None, temporary_root=None):
    if type(timeout) not in (int, float) or not 0 < timeout <= 60:
        raise ProbeError("flash_probe_invalid_timeout")
    executable = subscription_executable(executable)
    # This generated control directory contains no project files or task text.
    with tempfile.TemporaryDirectory(prefix="symphony-flash-probe-", dir=temporary_root) as directory:
        return _inspect(executable, Path(directory).resolve(), timeout, cancelled)


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable")
    args = parser.parse_args(argv)
    try:
        result = probe(args.executable)
    except ProbeError as exc:
        result = {"ready_for_tasks": False, "model_prompt_sent": False, "reason": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ready_for_tasks") else 2


if __name__ == "__main__":
    raise SystemExit(cli())
