"""Bounded delegation, separate histories and durable no-retry behavior offline."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ours/hooks"))
import api_executor as api  # noqa: E402
import api_session as session  # noqa: E402

KEYS = {"OPENAI_API_KEY": "fake-openai-session-key", "DEEPSEEK_API_KEY": "fake-deepseek-session-key"}


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    for name in [*KEYS, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]:
        monkeypatch.delenv(name, raising=False)


def state_file(tmp_path, **spec):
    path = tmp_path / "session.json"
    session.create(path, {"task": "Нужен проверяемый результат.", **spec})
    return path


def frame(text="Результат", calls=(), *, model="deepseek-v4-pro", **changes):
    reasoning = ({"type": "reasoning", "content": [{"type": "reasoning_text", "text": "private-lead-reasoning"}]}
                 if model == "deepseek-v4-pro" else
                 {"type": "reasoning", "id": "rs_local", "summary": [], "encrypted_content": "opaque-worker-reasoning"})
    output = [reasoning, *calls]
    if text:
        output.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
    result = {"id": "resp_local", "model": model, "status": "completed", "output": output,
              "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20,
                        "input_tokens_details": {"cached_tokens": 10}, "output_tokens_details": {"reasoning_tokens": 6}}}
    return result | changes


def delegate(call_id="call_1", part="part_1", kind="start", message="Сделай законченную часть.", **changes):
    return {"type": "function_call", "name": "delegate_luna", "call_id": call_id,
            "arguments": json.dumps({"part_id": part, "message": message, "kind": kind}, ensure_ascii=False)} | changes


def exchange(frames):
    requests = []
    remaining = iter(frames)

    def handle(request):
        requests.append({"url": str(request.url), "body": json.loads(request.content),
                         "authorization": request.headers["authorization"]})
        item = next(remaining)
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)

    return httpx.MockTransport(handle), requests


def test_lead_worker_correction_and_final_keep_contexts_separate(tmp_path):
    path = state_file(tmp_path, context="Контекст только ведущего.")
    transport, sent = exchange([
        frame(None, [delegate()]), frame("Первая версия", model="gpt-6-luna"),
        frame(None, [delegate("call_2", kind="correction", message="Исправь проверку отрицательного числа.")]),
        frame("Исправленная версия", model="gpt-6-luna"), frame("Проверено ведущим с указанными ограничениями.")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["task_acceptance"] == "not_evaluated"
    assert result["calls"] == 5 and result["parts"] == {"part_1": {"attempts": 2}}
    assert [r["body"]["model"] for r in sent] == ["deepseek-v4-pro", "gpt-6-luna", "deepseek-v4-pro", "gpt-6-luna", "deepseek-v4-pro"]
    assert "private-lead-reasoning" in json.dumps(sent[2]["body"])
    assert "opaque-worker-reasoning" in json.dumps(sent[3]["body"])
    for request in sent:
        body = request["body"]
        if body["model"] == "gpt-6-luna":
            assert body["reasoning"] == {"effort": "max"} and body["store"] is False
            assert not body.get("tools") and "Контекст только ведущего" not in body["instructions"]
            assert "private-lead-reasoning" not in json.dumps(body)
            assert request["authorization"] == "Bearer " + KEYS["OPENAI_API_KEY"]
        else:
            assert "store" not in body and "include" not in body
            assert "opaque-worker-reasoning" not in json.dumps(body)
            assert request["authorization"] == "Bearer " + KEYS["DEEPSEEK_API_KEY"]
    outputs = [i for i in sent[-1]["body"]["input"] if i.get("type") == "function_call_output"]
    assert [i["call_id"] for i in outputs] == ["call_1", "call_2"]
    assert json.loads(outputs[-1]["output"])["text"] == "Исправленная версия"
    assert result["usage"]["deepseek-lead"]["total_tokens"] == 60
    assert result["usage"]["luna-worker"]["total_tokens"] == 40
    assert result["usage"]["luna-worker"]["cached_input_tokens"] == 20
    assert result["usage"]["luna-worker"]["reasoning_tokens"] == 12
    assert result["output_tokens_committed"] == 40 and result["cost_usd"] is None
    assert all(key not in path.read_text() for key in KEYS.values())
    assert session.show(path) == result
    assert session.run(path, environ={}) == result
    assert len(sent) == 5


def test_multiple_parts_in_one_response_are_processed_sequentially(tmp_path):
    path = state_file(tmp_path)
    transport, sent = exchange([frame(None, [delegate(), delegate("call_2", "part_2")]),
                                frame("Первое", model="gpt-6-luna"), frame("Второе", model="gpt-6-luna"), frame()])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["calls"] == 4 and result["status"] == "dialogue_complete"
    assert set(result["parts"]) == {"part_1", "part_2"}
    assert all(len(sent[i]["body"]["input"]) == 1 for i in [1, 2])
    outputs = [i["call_id"] for i in sent[3]["body"]["input"] if i.get("type") == "function_call_output"]
    assert outputs == ["call_1", "call_2"]


@pytest.mark.parametrize("worker", [None, "luna-worker"])
def test_sol_compositions_keep_assigned_effort_and_solo_needs_no_worker_key(tmp_path, worker):
    path = state_file(tmp_path, lead_profile="sol-lead", worker_profile=worker, lead_effort="high")
    transport, sent = exchange([frame(model="gpt-6.1-sol")])
    result = session.run(path, environ={"OPENAI_API_KEY": KEYS["OPENAI_API_KEY"]}, transport=transport)
    assert result["status"] == "dialogue_complete"
    assert sent[0]["body"]["reasoning"] == {"effort": "high"}
    assert bool(sent[0]["body"].get("tools")) is bool(worker)
    assert result["profiles"]["lead"]["effort"] == "high"


def test_third_attempt_is_returned_to_lead_without_another_worker_call(tmp_path):
    path = state_file(tmp_path)
    transport, sent = exchange([frame(None, [delegate()]), frame(model="gpt-6-luna"),
                                frame(None, [delegate("call_2", kind="correction")]), frame(model="gpt-6-luna"),
                                frame(None, [delegate("call_3", kind="correction")]), frame("Завершаю сам в пределах возможностей.")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["calls"] == 6 and result["parts"]["part_1"]["attempts"] == 2
    assert result["tool_errors"] == [{"call_id": "call_3", "error": "correction_limit"}]
    assert sum(r["body"]["model"] == "gpt-6-luna" for r in sent) == 2
    assert json.loads(sent[-1]["body"]["input"][-1]["output"])["error"] == "correction_limit"


@pytest.mark.parametrize("arguments", ['not json', '[]', 'NaN', '{"part_id":"x","part_id":"y"}',
                                     '{"part_id":"p","kind":"start","message":"x","model":"other"}',
                                     '{"part_id":"../escape","kind":"start","message":"x"}',
                                     '{"part_id":"p","kind":"start","message":" "}',
                                     '{"part_id":"p","kind":"other","message":"x"}'])
def test_malformed_assignment_is_data_for_lead_not_an_execution(tmp_path, arguments):
    path = state_file(tmp_path)
    transport, sent = exchange([frame(None, [delegate(arguments=arguments)]), frame()])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["calls"] == 2
    assert not result["parts"] and result["tool_errors"][0]["error"] == "invalid_assignment"
    assert all(r["body"]["model"] == "deepseek-v4-pro" for r in sent)


def test_unknown_correction_and_repeated_start_cannot_reset_worker_attempts(tmp_path):
    path = state_file(tmp_path)
    transport, _ = exchange([frame(None, [delegate(kind="correction")]),
                             frame(None, [delegate("call_2")]), frame(model="gpt-6-luna"),
                             frame(None, [delegate("call_3")]), frame()])
    result = session.run(path, environ=KEYS, transport=transport)
    assert [r["error"] for r in result["tool_errors"]] == ["unknown_part", "part_already_exists"]
    assert result["parts"]["part_1"]["attempts"] == 1


@pytest.mark.parametrize("duplicate_batch", [True, False])
def test_duplicate_call_ids_stop_before_dispatch(tmp_path, duplicate_batch):
    path = state_file(tmp_path)
    frames = ([frame(None, [delegate(), delegate(part="p2")])] if duplicate_batch else
              [frame(None, [delegate()]), frame(model="gpt-6-luna"), frame(None, [delegate()])])
    transport, sent = exchange(frames)
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "invalid_tool_batch" and len(sent) == len(frames)


@pytest.mark.parametrize("limits,expected_calls,reason", [
    ({"max_calls": 1}, 1, "call_limit"),
    ({"max_calls": 4}, 4, "call_limit"),
    ({"output_tokens_total": 16}, 1, "output_limit"),
])
def test_shared_limits_bound_repeated_lead_requests(tmp_path, limits, expected_calls, reason):
    path = state_file(tmp_path, limits=limits)
    frames = [frame(None, [delegate(f"call_{i}", kind="correction")]) for i in range(expected_calls)]
    transport, sent = exchange(frames)
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "stopped" and result["stop_reason"] == reason
    assert len(sent) == expected_calls
    assert result["output_tokens_committed"] <= result["limits"]["output_tokens_total"]
    assert session.run(path, environ={}, transport=transport) == result and len(sent) == expected_calls


@pytest.mark.parametrize("limits", [{"max_calls": 2}, {"output_tokens_total": 24}])
def test_delegation_reserves_a_lead_return(tmp_path, limits):
    path = state_file(tmp_path, limits=limits)
    transport, sent = exchange([frame(None, [delegate()]), frame("Недостаточно ресурса для передачи.")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["calls"] == 2
    assert result["tool_errors"][0]["error"] == "budget_reserves_lead_return"
    assert all(r["body"]["model"] == "deepseek-v4-pro" for r in sent)
    if "output_tokens_total" in limits:
        assert sent[-1]["body"]["max_output_tokens"] == 16


@pytest.mark.parametrize("response,reason", [
    (frame(usage=None), "unknown_usage"),
    (frame(status="incomplete"), "incomplete_response"),
    (frame(status="failed"), "incomplete_response"),
    (frame(output=[{"type": "message", "content": [{"type": "refusal", "refusal": "Нет"}]}]), "model_refusal"),
    (frame(text=None), "empty_result"),
])
def test_uncertain_or_partial_result_stops_without_followup(tmp_path, response, reason):
    path = state_file(tmp_path)
    transport, sent = exchange([response])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == reason and len(sent) == 1
    assert session.run(path, environ={}, transport=transport) == result
    if reason == "unknown_usage":
        assert result["usage"]["deepseek-lead"]["input_tokens"] is None
        assert result["output_tokens_committed"] == 2048


def test_provider_output_limit_violation_is_not_hidden(tmp_path):
    path = state_file(tmp_path, limits={"output_tokens_per_call": 16})
    transport, _ = exchange([frame(usage={"input_tokens": 12, "output_tokens": 17, "total_tokens": 29})])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "provider_output_limit" and result["output_tokens_committed"] == 17


@pytest.mark.parametrize("item", [httpx.ReadTimeout("do not expose fake-secret"),
                                  httpx.Response(429, text="do not expose fake-secret"),
                                  RuntimeError("do not expose fake-secret")])
def test_api_error_is_not_retried_and_does_not_leak_details(tmp_path, item):
    path = state_file(tmp_path)
    transport, sent = exchange([item])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "stopped" and result["stop_reason"] == "api_error" and len(sent) == 1
    assert result["output_tokens_committed"] == 2048
    assert result["usage"]["deepseek-lead"]["output_tokens"] is None
    assert "fake-secret" not in path.read_text()
    session.run(path, environ={}, transport=transport)
    assert len(sent) == 1


def test_interrupted_inflight_call_is_never_repeated(tmp_path, monkeypatch):
    path = state_file(tmp_path)
    calls = []

    def interrupt(*args, **kwargs):
        calls.append(args)
        raise KeyboardInterrupt

    monkeypatch.setattr(api, "execute", interrupt)
    with pytest.raises(KeyboardInterrupt):
        session.run(path, environ=KEYS)
    assert session.show(path)["status"] == "running"
    result = session.run(path, environ={})
    assert result["stop_reason"] == "interrupted_no_automatic_retry" and len(calls) == 1
    assert result["output_tokens_committed"] == 2048


def test_failed_result_checkpoint_keeps_inflight_marker(tmp_path, monkeypatch):
    path = state_file(tmp_path)
    original = session._save

    def fail_received(path, state):
        if state["calls"] and state["calls"][-1]["status"] == "received":
            raise session.SessionError("state_write_failed")
        original(path, state)

    transport, sent = exchange([frame()])
    with monkeypatch.context() as patch:
        patch.setattr(session, "_save", fail_received)
        with pytest.raises(session.SessionError, match="state_write_failed"):
            session.run(path, environ=KEYS, transport=transport)
    result = session.run(path, environ={}, transport=transport)
    assert result["stop_reason"] == "interrupted_no_automatic_retry" and len(sent) == 1


def test_worker_cannot_delegate_or_execute_a_tool(tmp_path):
    path = state_file(tmp_path)
    transport, sent = exchange([frame(None, [delegate()]), frame(None, [delegate("worker_call")], model="gpt-6-luna")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "api_error" and len(sent) == 2
    raw = json.loads(path.read_text())
    assert raw["calls"][-1]["error"]["error"] == "invalid_function_call"


def test_context_size_is_checked_before_any_network(tmp_path):
    path = state_file(tmp_path, context="x" * 5000, limits={"request_bytes": 1024})
    transport, sent = exchange([])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "request_size_limit" and not sent and result["calls"] == 0


def test_missing_or_invalid_keys_do_not_consume_session(tmp_path, monkeypatch):
    path = state_file(tmp_path)
    monkeypatch.setattr(api.httpx, "Client", lambda **kwargs: pytest.fail("Unexpected network client"))
    for env, reason in [({}, "missing_api_key"), ({"DEEPSEEK_API_KEY": "fake"}, "missing_api_key"),
                        (KEYS | {"OPENAI_API_KEY": "bad\nkey"}, "invalid_api_key")]:
        with pytest.raises(session.SessionError, match=reason):
            session.run(path, environ=env)
        assert session.show(path)["status"] == "prepared" and session.show(path)["calls"] == 0
        assert "bad" not in path.read_text()


@pytest.mark.parametrize("changes", [{"task": " "}, {"task": ["x"]}, {"extra": True},
                                     {"lead_profile": "luna-worker"}, {"worker_profile": "sol-lead"},
                                     {"worker_profile": None},
                                     {"lead_effort": "medium"}, {"limits": {"unknown": 1}},
                                     {"limits": {"max_calls": True}}, {"limits": {"max_calls": 65}},
                                     {"limits": {"output_tokens_total": 15}}, {"context": 2}])
def test_invalid_spec_is_rejected_without_state(tmp_path, changes):
    path = tmp_path / "session.json"
    with pytest.raises(session.SessionError):
        session.create(path, {"task": "Проверка", **changes})
    assert not path.exists()


def test_state_permissions_existing_files_symlinks_and_git_are_protected(tmp_path):
    path = state_file(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    previous = path.read_bytes()
    with pytest.raises(session.SessionError, match="state_already_exists"):
        session.create(path, {"task": "Другая задача"})
    assert path.read_bytes() == previous
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(session.SessionError):
        session.show(link)
    path.chmod(0o644)
    with pytest.raises(session.SessionError, match="unsafe_state_file"):
        session.run(path, environ=KEYS)
    path.chmod(0o600)
    (tmp_path / ".git").mkdir()
    with pytest.raises(session.SessionError, match="state_must_be_outside_git"):
        session.show(path)


def test_busy_session_cannot_start_another_run_and_show_is_passive(tmp_path, monkeypatch):
    path = state_file(tmp_path)
    monkeypatch.setattr(api.httpx, "Client", lambda **kwargs: pytest.fail("Unexpected network client"))
    with session._lock(path):
        assert session.show(path)["status"] == "prepared"
        with pytest.raises(session.SessionError, match="session_busy"):
            session.run(path, environ=KEYS)


def test_prepared_state_with_prior_calls_cannot_reset_the_budget(tmp_path):
    path = state_file(tmp_path)
    transport, _ = exchange([frame()])
    session.run(path, environ=KEYS, transport=transport)
    state = json.loads(path.read_text())
    state["status"] = "prepared"
    path.write_text(json.dumps(state))
    with pytest.raises(session.SessionError, match="invalid_state"):
        session.run(path, environ=KEYS)


def test_profile_change_after_init_requires_a_new_assignment(tmp_path, monkeypatch):
    path = state_file(tmp_path)
    monkeypatch.setitem(api.PROFILES, "deepseek-lead", replace(api.PROFILES["deepseek-lead"], model="different-model"))
    monkeypatch.setattr(api.httpx, "Client", lambda **kwargs: pytest.fail("Unexpected network client"))
    with pytest.raises(session.SessionError, match="profile_changed_since_init"):
        session.run(path, environ=KEYS)
    assert session.show(path)["profiles"]["lead"]["model"] == "deepseek-v4-pro"


def test_cli_is_local_from_another_directory_and_missing_key_has_no_trace(tmp_path):
    path = tmp_path / "cli.json"
    env = {k: v for k, v in os.environ.items() if k not in KEYS}
    command = [sys.executable, str(ROOT / "ours/bin/api-session")]
    created = subprocess.run([*command, "init", "--state", str(path)], cwd=tmp_path, env=env,
                             input=json.dumps({"task": "Проверка без сети"}), text=True, capture_output=True, timeout=10)
    assert created.returncode == 0 and json.loads(created.stdout)["status"] == "prepared"
    shown = subprocess.run([*command, "show", "--state", str(path)], cwd=tmp_path, env=env,
                           text=True, capture_output=True, timeout=10)
    assert shown.returncode == 0 and json.loads(shown.stdout)["calls"] == 0
    stopped = subprocess.run([*command, "run", "--state", str(path)], cwd=tmp_path, env=env,
                             text=True, capture_output=True, timeout=10)
    assert stopped.returncode == 2 and json.loads(stopped.stdout)["error"] == "missing_api_key"
    assert not stopped.stderr and session.show(path)["calls"] == 0
