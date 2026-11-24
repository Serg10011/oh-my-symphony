"""One native Orchestrator turn over a disposable, expressly scoped copy."""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import time

import api_executor as api
import api_flash_probe as probe
import api_project as project
import api_workspace as workspace

TIMEOUT_SECONDS = 7200
MAX_EVENT_BYTES = 2 * 1024 * 1024
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "thinking_tokens", "total_tokens")
COMMANDS = ("pwd", "ls", "ls -a", "ls -l", "ls -la", "ls -al")
PLAN_TOOLS = {"exit_plan_mode", "update_steps", "finish"}


class NeonError(Exception):
    """A reason safe to show on the board."""


def _permitted(payload, policy):
    root = Path(policy["root"])
    if payload["workspacePaths"] != [str(root)]:
        return False
    call = payload["toolCall"]
    name, args = call["name"], call["args"]
    if not isinstance(name, str) or not isinstance(args, dict):
        return False
    if name in PLAN_TOOLS:
        return True
    if name == "run_command":
        return (args.get("CommandLine") in COMMANDS
                and (root / args.get("Cwd", str(root))).resolve() == root)
    if name not in {"read_file", "write_file", "edit_file"}:
        return False
    if args.get("ArtifactMetadata"):
        return False
    value = args["Path"]
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    destination = root / value
    relative = destination.relative_to(root)
    if (not relative.parts or ".." in relative.parts or destination.resolve() != destination
            or project._closed(relative.as_posix()) or relative.parts[0].casefold() in {".agents", ".tmp"}):
        return False
    scopes = policy["read"] if name == "read_file" else policy["write"]
    return project._in_scope(relative.as_posix(), scopes)


def _authorize(policy_path):
    allowed = False
    try:
        raw = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
        if len(raw) <= MAX_EVENT_BYTES:
            allowed = _permitted(json.loads(raw), json.loads(Path(policy_path).read_bytes()))
    except (OSError, TypeError, KeyError, ValueError, AttributeError, RuntimeError):
        pass
    result = {"decision": "allow"} if allowed else {
        "decision": "deny", "reason": "The card only allows files in its project scope and ls/pwd."
    }
    print(json.dumps(result))


def _configure(root, spec):
    policy = {"root": str(root), "read": spec["project"]["paths"], "write": workspace.writable_paths(spec)}
    agents = root / ".agents"
    agents.mkdir(mode=0o700)
    policy_path = agents / "symphony-scope.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    command = shlex.join([sys.executable, "-B", str(Path(__file__).resolve()), str(policy_path)])
    hooks = agents / "hooks.json"
    hooks.write_text(json.dumps({"symphony-neon-scope": {"enabled": True, "PreToolUse": [{
        "matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": 5}],
    }]}}), encoding="utf-8")
    for path in (policy_path, hooks):
        path.chmod(0o400)
    settings = {"modelProvider": "", "useG1Credits": False, "toolPermission": "request-review",
                "agentMode": "accept-edits", "allowNonWorkspaceAccess": False, "enableTerminalSandbox": True,
                "trustedWorkspaces": [str(root)], "statusLine": {"enabled": False}, "title": {"enabled": False},
                "permissions": {"allow": [f"read_file({root})", "command(ls)", "command(pwd)",
                                          *(f"write_file({root / scope})" for scope in policy["write"])],
                                "ask": [],
                                "deny": [f"write_file({agents})", "unsandboxed(*)", "mcp(*)",
                                         "read_url(*)", "execute_url(*)", "download_url(*)"]}}
    return settings, (hooks, policy_path)


def _events(process, cancelled, deadline):
    """Discard nonterminal frames and bound a partial line, even on a stalled CLI."""
    pending = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, "out")
        selector.register(process.stderr, selectors.EVENT_READ, "err")
        while selector.get_map():
            if cancelled and cancelled():
                raise NeonError("operator_stopped")
            if time.monotonic() >= deadline:
                raise NeonError("neon_timeout")
            for key, _ in selector.select(0.2):
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if key.data != "out":
                    continue
                pending.extend(data)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    if len(line) > MAX_EVENT_BYTES:
                        raise NeonError("neon_invalid_response")
                    try:
                        frame = json.loads(line)
                    except (ValueError, RecursionError):
                        raise NeonError("neon_invalid_response") from None
                    if not isinstance(frame, dict):
                        raise NeonError("neon_invalid_response")
                    yield frame
                if len(pending) > MAX_EVENT_BYTES:
                    raise NeonError("neon_invalid_response")


def _invoke(root, settings, prompt, *, cancelled, admit, checkpoint):
    try:
        executable = probe.subscription_executable()
    except probe.ProbeError:
        raise NeonError("neon_startup_failed") from None
    argv = [str(executable), "--model", api.NEON_MODEL, "--mode=accept-edits", "--sandbox",
            "--disable-slash-commands", "--settings", json.dumps(settings),
            "--input-format", "stream-json", "--output-format", "stream-json", "--print="]
    with subprocess.Popen(argv, cwd=root, env=probe._environment(), stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as process:
        try:
            began = time.monotonic()
            frames = _events(process, cancelled, began + 30)
            initialization = next(frames, {}).get("init")
            frames.close()
            if (not isinstance(initialization, dict) or initialization.get("model") != api.NEON_MODEL
                    or initialization.get("permission_mode") != "request-review"
                    or initialization.get("cwd") != str(root)):
                raise NeonError("neon_startup_failed")
            payload = json.dumps({"event": "user", "message": {"content": prompt}}).encode()
            with admit():
                process.stdin.write(payload + b"\n")
                process.stdin.close()
            last_checkpoint = time.monotonic()
            result = None
            for frame in _events(process, cancelled, began + TIMEOUT_SECONDS):
                if frame.get("event") == "result":
                    result = frame.get("result")
                if checkpoint and time.monotonic() - last_checkpoint > 10:
                    checkpoint()
                    last_checkpoint = time.monotonic()
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise NeonError("neon_failed") from None
            if not isinstance(result, dict):
                raise NeonError("neon_failed")
            if returncode and result.get("status") == "SUCCESS":
                result["status"] = "ERROR"
            return result
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)


def _usage(result):
    value = result.get("usage")
    return {key: value[key] for key in USAGE_FIELDS
            if isinstance(value, dict) and type(value.get(key)) is int and value[key] >= 0}


def _failure_reason(result):
    detail = str(result.get("error", "")).lower()
    if any(marker in detail for marker in ("resource_exhausted", "quota exhausted", "quota limit")):
        return "neon_quota_exhausted"
    if "permission" in detail and "denied" in detail:
        return "neon_permission_denied"
    return "neon_failed"


def run(spec, value, *, cancelled, admit, checkpoint):
    """Verify the whole native result before constructing a sealed candidate."""
    before = workspace.manifest(spec, value)
    root = workspace._copy_files(spec, value, before, ".neon-")
    try:
        settings, controls = _configure(root, spec)
        hashes = {file: workspace._sha(file.read_bytes()) for file in controls}
        prompt = (spec["task"] + "\n\n" + spec["context"] + "\n\n"
                  + workspace.scope_instructions(spec) + "\n"
                  "Ознакомься с выданными файлами, внеси правки и кратко опиши результат. "
                  "Для осмотра доступны ls и pwd. Проверки сдачи запускает Symphony.")
        result = _invoke(root, settings, prompt, cancelled=cancelled, admit=admit, checkpoint=checkpoint)
        usage = _usage(result)
        answer = result.get("response")
        if (result.get("status") != "SUCCESS" or not isinstance(answer, str) or not answer.strip()
                or len(answer.encode()) > 256 * 1024):
            return {"usage": usage, "reason": _failure_reason(result)}
        if cancelled and cancelled():
            return {"usage": usage, "reason": "operator_stopped"}
        if any(path.is_symlink() or workspace._sha(path.read_bytes()) != digest for path, digest in hashes.items()):
            return {"usage": usage, "reason": "neon_scope_changed"}
        for path in controls:
            path.unlink()
        root.joinpath(".agents").rmdir()
        if any(path.is_symlink() for path in root.rglob("*")):
            return {"usage": usage, "reason": "neon_scope_changed"}
        scratch = {**value, "root": str(root)}
        try:
            after = workspace.manifest(spec, scratch)
        except workspace.WorkspaceError:
            return {"usage": usage, "reason": "neon_scope_changed"}
        changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
        if not changed:
            return {"usage": usage, "reason": "neon_no_changes"}
        if any(not project._in_scope(key, workspace.writable_paths(spec)) for key in changed):
            return {"usage": usage, "reason": "neon_scope_changed"}
        return {"usage": usage, "answer": answer,
                "root": str(workspace._copy_files(spec, scratch, after, ".workspace-"))}
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    _authorize(sys.argv[1])
