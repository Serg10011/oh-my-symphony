"""Stdlib-only Antigravity hooks for Symphony's text worker.

The binding is controller-owned and outside the empty CLI workspace. No task
text, tool arguments, credentials or reasoning are written to the hook journal.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import uuid

STOP_CODE = "SYMPHONY_FLASH_GATE_CLOSED"
MAX_BYTES = 256 * 1024


def stop():
    return {"terminationBehavior": "terminate", "injectSteps": [
        {"errorMessage": {"userMessage": STOP_CODE}}]}


def decide(event, value, binding, *, now):
    """Pure policy; unknown/missing metadata never grants access."""
    value = value if isinstance(value, dict) else {}
    binding = binding if isinstance(binding, dict) else {}
    try:
        conversation = str(uuid.UUID(value["conversationId"]))
        matches = (value["workspacePaths"] == [binding["workspace"]]
                   and value["modelName"] == binding["model"]
                   and conversation == value["conversationId"])
        active = (binding["enabled"] is True and type(binding["expires_at"]) in (float, int)
                  and now < binding["expires_at"] <= now + 3600)
    except (KeyError, TypeError, ValueError, AttributeError):
        matches, active, conversation = False, False, None
    name = value.get("toolCall", {}).get("name") if isinstance(value.get("toolCall"), dict) else None
    allowed = matches and (event == "start" or active and event in {"pre", "tool"})
    if event == "tool":
        allowed = allowed and name == "finish"
        result = {"decision": "allow" if allowed else "deny", "reason": STOP_CODE}
    else:
        result = {} if allowed else stop()
    journal = {"event": event, "conversation_id": conversation, "matched": matches,
               "allowed": allowed, "tool": name if name == "finish" else None}
    return result, journal


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    event = args[1] if len(args) == 2 else "invalid"
    result = {"decision": "deny", "reason": STOP_CODE} if event == "tool" else stop()
    try:
        directory = Path(args[0])
        info = directory.stat()
        if (directory.resolve(strict=True) != directory or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ValueError("private directory required")
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversize hook")
        value = json.loads(raw)
        binding = json.loads((directory / "binding.json").read_bytes())
        result, journal = decide(event, value, binding, now=time.time())
        fd = os.open(directory / "hooks.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(json.dumps(journal) + "\n")
    except (OSError, ValueError, TypeError, AttributeError, IndexError, RecursionError):
        result = {"decision": "deny", "reason": STOP_CODE} if event == "tool" else stop()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
