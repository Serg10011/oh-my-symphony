"""Text-only subscription transport for the Symphony worker.

Each conversation owns an empty workspace. A closed-gate startup verifies the
hook before task data is sent. Project operations remain requests for Symphony
to validate and execute; this module never performs them or retries a turn.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import select
import selectors
import shlex
import signal
import subprocess
import sys
import tempfile
import time

import api_executor as api
import api_flash_guard as guard
import api_flash_probe as probe
import api_flash_protocol as protocol


class TransportError(Exception):
    """Safe diagnostic with separately retained usage, never raw CLI output."""

    def __init__(self, code, *, usage=None, quota_usage_possible=False):
        super().__init__(code)
        self.usage = usage
        self.quota_usage_possible = quota_usage_possible


class Conversation:
    def __init__(self, *, tools, instructions, temporary_root, executable=None, cancelled=None):
        self.tools = json.loads(protocol._encode(tools))
        self.schema = protocol.schema(self.tools)
        if len(protocol._encode(self.schema)) > 65536:
            raise TransportError("flash_tool_schema_too_large")
        self.instructions = instructions
        if not isinstance(instructions, str):
            raise TransportError("flash_invalid_instructions")
        try:
            self.executable = probe.subscription_executable(executable)
        except probe.ProbeError as exc:
            raise TransportError(str(exc)) from None
        root = Path(temporary_root).resolve(strict=True)
        if any((parent / ".git").exists() for parent in (root, *root.parents)):
            raise TransportError("flash_control_directory_inside_project")
        self.root, self.cancelled = root, cancelled
        self.process, self.selector, self.temporary = None, None, None
        self.buffered = bytearray()
        self.received = 0
        self.previous_usage, self.conversation_id = None, None
        self.turn_ids = set()
        self.verified, self.sent = False, False

    def __enter__(self):
        usage = None
        try:
            self._start(enabled=False)
            self._init(time.monotonic() + 30)
            self._send(protocol.user_input("SYMPHONY_GATE_CHECK. Reply READY only."))
            result = self._result(time.monotonic() + 30)
            usage = protocol.usage_delta(result.get("usage"))
            rows = self._journal()
            if (result.get("status") != "ERROR" or result.get("error") != guard.STOP_CODE
                    or usage["total_tokens"]
                    or not any(r["event"] == "pre" and r["matched"] and not r["allowed"] for r in rows)):
                raise TransportError("flash_gate_unverified", usage=usage, quota_usage_possible=bool(usage["total_tokens"]))
            self._close_process()
            usage = None
            self.verified = True
            self._start(enabled=True)
            self._init(time.monotonic() + 30)
            self.sent = False
            return self
        except BaseException as exc:
            if not isinstance(exc, Exception):
                self.close()
                raise
            raise self._abort(exc, usage, "flash_startup_failed") from None

    def __exit__(self, *_):
        self.close()

    def _start(self, *, enabled):
        self.sent = False
        self.temporary = tempfile.TemporaryDirectory(prefix="symphony-flash-", dir=self.root)
        self.directory = Path(self.temporary.name).resolve()
        self.workspace = self.directory / "workspace"
        agents = self.workspace / ".agents/agents"
        agents.mkdir(parents=True, mode=0o700)
        (agents / (probe.AGENT + ".md")).write_text(probe.AGENT_DOCUMENT + "\n" + protocol.INSTRUCTIONS)
        self.binding = {"workspace": str(self.workspace), "model": probe.MODEL,
                        "enabled": enabled, "expires_at": time.time() + 30}
        self._write_binding()

        def command(event):
            return {"type": "command", "timeout": 5, "command": shlex.join([
                str(Path(sys.executable).resolve()), "-B", str(Path(guard.__file__).resolve()),
                str(self.directory), event])}

        hooks = {"symphony-gate": {"enabled": True, "SessionStart": [command("start")],
                 "PreInvocation": [command("pre")],
                 "PreToolUse": [{"matcher": "*", "hooks": [command("tool")]}]}}
        (self.workspace / ".agents/hooks.json").write_text(json.dumps(hooks))
        settings = {**probe.SETTINGS, "trustedWorkspaces": [str(self.workspace)]}
        argv = [str(self.executable), "--input-format", "stream-json", "--output-format", "stream-json",
                "--agent", probe.AGENT, "--model", probe.MODEL, "--effort", "high",
                "--disable-slash-commands", "--settings", json.dumps(settings),
                "--json-schema", json.dumps(self.schema), "--print="]
        self.process = subprocess.Popen(argv, cwd=self.workspace, env=probe._environment(),
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        close_fds=True, start_new_session=True)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.selector = selectors.DefaultSelector()
        for stream in (self.process.stdout, self.process.stderr):
            self.selector.register(stream, selectors.EVENT_READ)
        self.buffered, self.received = bytearray(), 0
        self.journal_count = 0

    def _write_binding(self):
        target = self.directory / "binding.json"
        temporary = self.directory / "binding.tmp"
        temporary.write_text(json.dumps(self.binding))
        temporary.replace(target)

    def _journal(self):
        path = self.directory / "hooks.jsonl"
        if not path.exists() or path.stat().st_size > api.MAX_RESPONSE_BYTES:
            raise TransportError("flash_gate_journal_missing", quota_usage_possible=self.sent)
        try:
            rows = [json.loads(line) for line in path.read_text().splitlines()]
        except (ValueError, OSError):
            raise TransportError("flash_gate_journal_invalid", quota_usage_possible=self.sent) from None
        if any(not isinstance(row, dict) or set(row) != {
                "event", "conversation_id", "matched", "allowed", "tool"}
               or row["event"] not in ("start", "pre", "tool")
               or type(row["matched"]) is not bool or type(row["allowed"]) is not bool
               or row["tool"] not in (None, "finish") for row in rows):
            raise TransportError("flash_gate_journal_invalid", quota_usage_possible=self.sent)
        return rows

    def _event(self, deadline):
        while True:
            if self.cancelled and self.cancelled():
                raise TransportError("flash_cancelled", quota_usage_possible=self.sent)
            if time.monotonic() >= deadline:
                raise TransportError("flash_timeout", quota_usage_possible=self.sent)
            if b"\n" in self.buffered:
                raw, _, self.buffered = self.buffered.partition(b"\n")
                try:
                    value = json.loads(raw)
                except (ValueError, RecursionError):
                    raise TransportError("flash_invalid_stream", quota_usage_possible=self.sent) from None
                if not isinstance(value, dict):
                    raise TransportError("flash_invalid_stream", quota_usage_possible=self.sent)
                return value
            if not self.selector.get_map():
                raise TransportError("flash_cli_exited", quota_usage_possible=self.sent)
            for key, _ in self.selector.select(.1):
                block = os.read(key.fd, 65536)
                if not block:
                    self.selector.unregister(key.fileobj)
                    continue
                self.received += len(block)
                if self.received > api.MAX_RESPONSE_BYTES:
                    raise TransportError("flash_output_too_large", quota_usage_possible=self.sent)
                if key.fileobj == self.process.stdout:
                    self.buffered.extend(block)

    def _init(self, deadline):
        try:
            # init.tools is a general catalog in 1.5.3, not the active whitelist.
            probe.inspect_init(self._event(deadline), self.workspace)
        except probe.ProbeError:
            raise TransportError("flash_startup_contract_mismatch") from None

    def _send(self, encoded, deadline=None):
        deadline = time.monotonic() + 30 if deadline is None else deadline
        pending = memoryview(encoded)
        while pending:
            if self.cancelled and self.cancelled():
                raise TransportError("flash_cancelled", quota_usage_possible=self.sent)
            if time.monotonic() >= deadline:
                raise TransportError("flash_timeout", quota_usage_possible=self.sent)
            if select.select([], [self.process.stdin], [], .1)[1]:
                try:
                    written = os.write(self.process.stdin.fileno(), pending)
                except BlockingIOError:
                    continue
                self.sent = self.sent or written > 0
                pending = pending[written:]

    def _result(self, deadline):
        while True:
            event = self._event(deadline)
            if event.get("event") == "result":
                result = event.get("result")
                if not isinstance(result, dict):
                    raise TransportError("flash_invalid_result", quota_usage_possible=self.sent)
                return result
            if event.get("event") not in {"step_update"}:
                raise TransportError("flash_unexpected_event", quota_usage_possible=self.sent)

    def exchange(self, message, *, turn_id, timeout=600):
        """One user turn, no automatic retries; a malformed answer retains usage."""
        usage, self.sent = None, False
        try:
            if (not self.verified or self.process is None or not isinstance(turn_id, str)
                    or not api.NAME_RE.fullmatch(turn_id) or turn_id in self.turn_ids):
                raise TransportError("flash_invalid_turn")
            if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
                raise TransportError("flash_invalid_timeout")
            first = not self.turn_ids
            encoded = protocol.turn_input(message, tools=self.tools if first else None, instructions=self.instructions)
            self.turn_ids.add(turn_id)
            self.binding["expires_at"] = time.time() + timeout
            self._write_binding()
            self.received, self.sent = 0, False
            deadline = time.monotonic() + timeout
            self._send(encoded, deadline)
            result = self._result(deadline)
            identifier = result.get("conversation_id")
            if result.get("status") != "SUCCESS":
                if self.conversation_id is None or identifier == self.conversation_id:
                    try:
                        usage = protocol.usage_delta(result.get("usage"), self.previous_usage)
                    except protocol.ProtocolError:
                        pass
                error = result.get("error")
                if result.get("status") == "ERROR" and isinstance(error, str) and re.search(
                        r"\b(?:quota exhausted|quota limit exceeded|rate limit exceeded|RESOURCE_EXHAUSTED)\b",
                        error[:4096], re.IGNORECASE):
                    raise TransportError("flash_capacity_unavailable")
                raise TransportError("flash_worker_stopped")
            if (not isinstance(identifier, str) or not identifier
                    or self.conversation_id is not None and identifier != self.conversation_id):
                raise TransportError("flash_conversation_changed")
            usage = protocol.usage_delta(result.get("usage"), self.previous_usage)
            self.previous_usage = protocol.cumulative_usage(result["usage"])
            self.conversation_id = identifier
            rows = self._journal()
            fresh = rows[self.journal_count:]
            if (not fresh or any(not row["matched"] or not row["allowed"] for row in rows)
                    or any(row["conversation_id"] != identifier for row in rows)
                    or not any(row["event"] == "pre" for row in fresh)
                    or not any(row["event"] == "tool" and row["tool"] == "finish" for row in fresh)):
                raise TransportError("flash_gate_rejected")
            self.journal_count = len(rows)
            reply = protocol.parse_reply(result.get("structured_output"), self.tools, turn_id=turn_id)
            reply.update(usage=usage, conversation_id=identifier, billing="subscription")
            return reply
        except (TransportError, protocol.ProtocolError, OSError) as exc:
            raise self._abort(exc, usage, "flash_io_error") from None

    def _abort(self, exc, usage, fallback):
        code = str(exc) if isinstance(exc, (TransportError, protocol.ProtocolError, probe.ProbeError)) else fallback
        if isinstance(exc, TransportError) and exc.usage is not None:
            usage = exc.usage
        possible = self.sent if usage is None else bool(usage["total_tokens"])
        try:
            self.close()
        except (TransportError, OSError):
            code = "flash_cleanup_unconfirmed"
        return TransportError(code, usage=usage, quota_usage_possible=possible)

    def _close_process(self):
        if self.selector:
            self.selector.close()
            self.selector = None
        if self.process:
            process, self.process = self.process, None
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
                raise TransportError("flash_cleanup_unconfirmed") from None
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
        if self.temporary:
            self.temporary.cleanup()
            self.temporary = None

    def close(self):
        self.verified = False
        self._close_process()
