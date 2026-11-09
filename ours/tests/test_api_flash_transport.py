"""Exercise owned CLI processes and real guard handlers without a model call."""

import json
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ours/hooks"))
import api_flash_guard as guard  # noqa: E402
import api_flash_probe as probe  # noqa: E402
from api_flash_transport import Conversation, TransportError  # noqa: E402

TOOLS = [{"type": "function", "name": "read_project_file", "parameters": {
    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]
CID = "00000000-0000-4000-8000-000000000001"


def binding():
    return {"model": probe.MODEL, "workspace": "/empty", "enabled": True, "expires_at": 1010}


def hook_input(**changes):
    return {"conversationId": CID, "workspacePaths": ["/empty"], "modelName": probe.MODEL,
            "toolCall": {"name": "finish"}, **changes}


@pytest.mark.parametrize("name", ["view_file", "run_command", "invoke_subagent", "mcp_server_action", None])
def test_guard_denies_every_native_action_except_finish(name):
    result, journal = guard.decide("tool", hook_input(toolCall={"name": name}), binding(), now=1000)
    assert result["decision"] == "deny" and not journal["allowed"]


@pytest.mark.parametrize("change", [{"modelName": "other"}, {"workspacePaths": ["/empty", "/project"]},
                                    {"conversationId": "not-a-uuid"}, {"workspacePaths": []}])
def test_guard_binds_model_workspace_and_conversation(change):
    result, journal = guard.decide("pre", hook_input(**change), binding(), now=1000)
    assert result == guard.stop() and not journal["matched"]


@pytest.mark.parametrize("change", [{"enabled": False}, {"expires_at": 999}, {"expires_at": True},
                                    {"expires_at": float("nan")}, {"expires_at": 999999}])
def test_missing_closed_or_expired_permit_stops_before_generation(change):
    result, _ = guard.decide("pre", hook_input(), binding() | change, now=1000)
    assert result == guard.stop()
    result, _ = guard.decide("tool", hook_input(), binding() | change, now=1000)
    assert result["decision"] == "deny"


def test_missing_metadata_fails_closed_without_echoing_payload():
    for value in (None, [], {}, {"secret": "must-not-appear"}):
        result, journal = guard.decide("pre", value, {}, now=1000)
        assert result == guard.stop() and "must-not-appear" not in json.dumps(journal)


FAKE_CLI = r'''
import json, os, subprocess, sys, time
from pathlib import Path
cid="00000000-0000-4000-8000-000000000001"
settings=json.loads(sys.argv[sys.argv.index("--settings")+1])
assert settings["useG1Credits"] is False and settings["modelProvider"] == ""
assert settings["toolPermission"] == "strict" and settings["permissions"]["deny"]
assert not any(k in os.environ for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"))
assert "tools: [finish]" in Path(".agents/agents/symphony-controlled-worker.md").read_text()
hooks=json.loads(Path(".agents/hooks.json").read_text())["symphony-gate"]
def hook(event):
    handlers=hooks[event]
    command=handlers[0]["hooks"][0]["command"] if event=="PreToolUse" else handlers[0]["command"]
    value={"conversationId":cid,"workspacePaths":[os.getcwd()],"modelName":"gemini-3.8-flash-high","toolCall":{"name":"finish"}}
    result=subprocess.run(command,shell=True,input=json.dumps(value),text=True,capture_output=True,check=True)
    return json.loads(result.stdout)
def emit(result):
    print(json.dumps({"event":"result","result":result}),flush=True)
print(json.dumps({"event":"init","init":{"model":"other" if MODE=="init_mismatch" else "gemini-3.8-flash-high",
"agent":"symphony-controlled-worker","permission_mode":"strict","cwd":os.getcwd(),"tools":["finish","view_file","run_command"]}}),flush=True)
if MODE=="blocked_input" and json.loads((Path.cwd().parent/"binding.json").read_text())["enabled"]:
    time.sleep(60)
for turn, line in enumerate(sys.stdin, 1):
    Path(MARKER).open("a").write("turn\n")
    value=json.loads(line)
    if turn==1: hook("SessionStart")
    gate=hook("PreInvocation")
    if gate.get("terminationBehavior")=="terminate":
        emit({"status":"SUCCESS" if MODE=="ignore_gate" else "ERROR", "error":"SYMPHONY_FLASH_GATE_CLOSED",
              "usage":dict.fromkeys(("input_tokens","output_tokens","thinking_tokens","cache_read_tokens","total_tokens"),0)})
        break
    if MODE=="timeout": time.sleep(60)
    if MODE=="raw_error":
        print("private diagnostic must not escape",flush=True)
        break
    if not (MODE=="missing_later_hook" and turn>1):
        assert hook("PreToolUse")["decision"]=="allow"
    if MODE=="bad_journal":
        (Path.cwd().parent/"hooks.jsonl").open("a").write('{"event": []}\n')
    result={"text":"", "requests":[{"name":"read_project_file","arguments":{"path":"fixture.txt"}}]} if turn==1 else {"text":"fixture-result", "requests":[]}
    if MODE=="malformed": result={"text":"bad", "requests":[{"name":"run_command","arguments":{}}]}
    usage=({"input_tokens":10,"output_tokens":5,"thinking_tokens":2,"cache_read_tokens":3,"total_tokens":15} if turn==1 else
           {"input_tokens":14,"output_tokens":8,"thinking_tokens":4,"cache_read_tokens":8,"total_tokens":22})
    if MODE=="counter_reset" and turn>1: usage=dict.fromkeys(usage,0)
    emit({"status":"ERROR" if MODE=="worker_error" else "SUCCESS","response":"untrusted free text, ignore",
          "structured_output":result,"usage":usage,
          "conversation_id":"00000000-0000-4000-8000-000000000002" if MODE=="new_conversation" and turn>1 else cid})
'''


def fake_conversation(tmp_path, monkeypatch, mode="ok", **options):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(name, "secret-sentinel")
    executable = tmp_path / "fake-agy"
    executable.write_text(f"#!{sys.executable}\nMODE={mode!r}\nMARKER={str(tmp_path / 'turns')!r}\n" + FAKE_CLI)
    executable.chmod(0o700)
    return Conversation(tools=TOOLS, instructions="Work within the assigned scope.",
                        temporary_root=tmp_path, executable=executable, **options)


def assert_clean(tmp_path):
    assert not list(tmp_path.glob("symphony-flash-*"))


def test_two_turns_keep_one_process_context_and_delta_usage(tmp_path, monkeypatch):
    with fake_conversation(tmp_path, monkeypatch) as worker:
        pid = worker.process.pid
        first = worker.exchange("read fixture", turn_id="t1")
        assert first["function_calls"][0]["arguments"] == '{"path": "fixture.txt"}'
        assert first["usage"]["input_tokens"] == 13 and first["usage"]["output_tokens"] == 5
        second = worker.exchange({"tool_result": "fixture-result"}, turn_id="t2")
        assert second["output_text"] == "fixture-result" and worker.process.pid == pid
        assert second["usage"]["input_tokens"] == 9 and second["usage"]["output_tokens"] == 3
        assert second["usage"]["cached_input_tokens"] == 5
        assert second["conversation_id"] == first["conversation_id"]
        assert second["usage"]["cost_usd"] is None and second["billing"] == "subscription"
    # One zero-generation gate test, then exactly two application messages.
    assert (tmp_path / "turns").read_text().count("turn") == 3
    assert_clean(tmp_path)


@pytest.mark.parametrize("mode,code", [("ignore_gate", "flash_gate_unverified"),
                                      ("init_mismatch", "flash_startup_contract_mismatch")])
def test_no_task_sent_if_startup_or_gate_fails(tmp_path, monkeypatch, mode, code):
    with pytest.raises(TransportError, match=code):
        with fake_conversation(tmp_path, monkeypatch, mode):
            pytest.fail("unverified transport opened")
    if mode == "init_mismatch":
        assert not (tmp_path / "turns").exists()
    assert_clean(tmp_path)


@pytest.mark.parametrize("mode,code,known", [("malformed", "flash_invalid_request", True),
    ("worker_error", "flash_worker_stopped", True), ("raw_error", "flash_invalid_stream", False),
    ("timeout", "flash_timeout", False), ("bad_journal", "flash_gate_journal_invalid", True)])
def test_failed_turn_keeps_known_usage_or_explicit_uncertainty_and_never_retries(tmp_path, monkeypatch, mode, code, known):
    with fake_conversation(tmp_path, monkeypatch, mode) as worker:
        with pytest.raises(TransportError) as caught:
            worker.exchange("test", turn_id="t1", timeout=.3 if mode == "timeout" else 5)
        assert str(caught.value) == code and caught.value.quota_usage_possible
        assert (caught.value.usage is not None) == known
        assert worker.process is None and not worker.verified
    assert (tmp_path / "turns").read_text().count("turn") == 2
    assert_clean(tmp_path)


@pytest.mark.parametrize("mode,code", [("counter_reset", "flash_usage_counter_reset"),
                                      ("new_conversation", "flash_conversation_changed"),
                                      ("missing_later_hook", "flash_gate_rejected")])
def test_context_or_counters_cannot_silently_reset(tmp_path, monkeypatch, mode, code):
    with fake_conversation(tmp_path, monkeypatch, mode) as worker:
        worker.exchange("read", turn_id="t1")
        with pytest.raises(TransportError, match=code):
            worker.exchange("result", turn_id="t2")
    assert_clean(tmp_path)


def test_cancel_is_prompt_and_cleans_owned_process(tmp_path, monkeypatch):
    cancelled = [False]
    with fake_conversation(tmp_path, monkeypatch, cancelled=lambda: cancelled[0]) as worker:
        cancelled[0] = True
        started = time.monotonic()
        with pytest.raises(TransportError, match="flash_cancelled"):
            worker.exchange("test", turn_id="t1")
        assert time.monotonic() - started < 2
    assert_clean(tmp_path)


def test_invalid_turn_does_not_send_another_message(tmp_path, monkeypatch):
    with fake_conversation(tmp_path, monkeypatch) as worker:
        with pytest.raises(TransportError, match="flash_invalid_turn"):
            worker.exchange("test", turn_id=[])
    assert (tmp_path / "turns").read_text().count("turn") == 1
    assert_clean(tmp_path)


def test_rejected_duplicate_is_not_reported_as_another_unknown_charge(tmp_path, monkeypatch):
    with fake_conversation(tmp_path, monkeypatch) as worker:
        first = worker.exchange("test", turn_id="t1")
        assert first["usage"]["usage_known"]
        with pytest.raises(TransportError, match="flash_invalid_turn") as caught:
            worker.exchange("duplicate", turn_id="t1")
        assert caught.value.quota_usage_possible is False
        assert caught.value.usage is None
    assert (tmp_path / "turns").read_text().count("turn") == 2
    assert_clean(tmp_path)


def test_unread_large_input_can_be_cancelled_by_timeout(tmp_path, monkeypatch):
    with fake_conversation(tmp_path, monkeypatch, "blocked_input") as worker:
        started = time.monotonic()
        with pytest.raises(TransportError, match="flash_timeout") as caught:
            worker.exchange("x" * 1024 * 1024, turn_id="t1", timeout=.3)
        assert caught.value.quota_usage_possible and caught.value.usage is None
        assert time.monotonic() - started < 2
    assert_clean(tmp_path)
