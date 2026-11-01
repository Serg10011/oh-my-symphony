"""Executable protocol fixture: local schemas and strict initialization only."""
from __future__ import annotations

import sys
from pathlib import Path


def schemas():
    thread = {"properties": {
        "model": {"type": "string"}, "allowProviderModelFallback": {"type": "boolean"},
        "ephemeral": {"type": "boolean"}, "approvalPolicy": {"enum": ["never"]},
        "sandbox": {"enum": ["read-only"]}, "developerInstructions": {"type": "string"},
        "dynamicTools": {"type": "array"}, "environments": {"type": "array"},
    }}
    turn = {"properties": {
        "model": {"type": "string"}, "effort": {"enum": ["medium"]},
        "approvalPolicy": {"enum": ["never"]}, "environments": {"type": "array"},
        "disabledPluginIds": {"type": "array"}, "outputSchema": {},
        "additionalContext": {"type": "object", "additionalProperties": {
            "type": "object", "properties": {"kind": {"enum": ["untrusted"]}, "value": {"type": "string"}},
        }},
        "sandboxPolicy": {"type": "object", "properties": {
            "type": {"enum": ["readOnly"]}, "networkAccess": {"type": "boolean"},
        }},
    }}
    return {"ThreadStartParams.json": thread, "TurnStartParams.json": turn}


def fake_codex(directory: Path, version="0.162.1", *, missing=None, mode="ok", output=None, returncode=0, mutate=None,
               ready_file: Path | None = None):
    directory.mkdir(exist_ok=True)
    executable = directory / "codex"
    data = schemas()
    if mutate:
        mutate(data)
    if missing:
        filename, key = missing
        del data[filename]["properties"][key]
    executable.write_text(f'''#!{sys.executable}
import json, os, sys, time
from pathlib import Path
with Path(__file__).with_suffix(".calls").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if "--version" in sys.argv:
    print({output if output is not None else 'codex-cli ' + version!r})
    raise SystemExit({returncode})
if {mode!r} == "timeout":
    if {str(ready_file) if ready_file is not None else None!r} is not None:
        ready = Path({str(ready_file) if ready_file is not None else None!r})
        pending = ready.with_suffix(".tmp")
        pending.write_text(str(os.getpid()))
        pending.replace(ready)
    time.sleep(30)
if "generate-json-schema" in sys.argv:
    root = Path(sys.argv[sys.argv.index("--out") + 1]) / "v2"
    root.mkdir(parents=True)
    for name, schema in {data!r}.items():
        (root / name).write_text(json.dumps(schema))
    raise SystemExit(0)
assert "--strict-config" in sys.argv
assert "skip_host_skill_discovery" in sys.argv
assert 'shell_environment_policy.inherit="none"' in sys.argv
assert "mcp_servers={{}}" in sys.argv
assert not any(os.environ.get(key) for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"))
if {mode!r} == "unsupported_flags":
    raise SystemExit(2)
if {mode!r} == "auth_read":
    assert not (Path(os.environ["CODEX_HOME"]) / "auth.json").exists()
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    assert message["method"] == "initialize", "readiness must never start a thread/turn"
    if {mode!r} == "server_request":
        print(json.dumps({{"id": 999, "method": "item/commandExecution/requestApproval", "params": {{}}}}), flush=True)
    print(json.dumps({{"id": message["id"], "result": {{}}}}), flush=True)
''', encoding="utf-8")
    executable.chmod(0o755)
    return executable
