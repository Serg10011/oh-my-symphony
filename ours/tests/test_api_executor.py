"""Provider protocol, credential isolation and bounds without paid API calls."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ours/hooks"))
import api_executor as api  # noqa: E402

KEYS = {"OPENAI_API_KEY": "fake-openai-unit-test-key", "DEEPSEEK_API_KEY": "fake-deepseek-unit-test-key"}
TOOL = {"type": "function", "name": "run_tests", "description": "Run permitted tests",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}, "strict": True}


@pytest.fixture(autouse=True)
def clean_keys(monkeypatch):
    for name in [*KEYS, "ANTHROPIC_API_KEY"]:
        monkeypatch.delenv(name, raising=False)


def response(**changes):
    result = {"id": "resp_unit", "model": "gpt-6-luna", "status": "completed",
              "output": [{"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": "готово"}]}],
              "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 10},
                        "output_tokens": 30, "output_tokens_details": {"reasoning_tokens": 20}, "total_tokens": 130}}
    result.update(changes)
    return result


def exchange(body=None, status=200):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=response() if body is None else body)

    return httpx.MockTransport(handler), calls


@pytest.mark.parametrize("name,model,effort,endpoint,key_env", [
    ("deepseek-lead", "deepseek-flash", "high", "https://api.deepseek.com/responses", "DEEPSEEK_API_KEY"),
    ("luna-worker", "gpt-6-luna", "max", "https://api.openai.com/v1/responses", "OPENAI_API_KEY"),
    ("sol-lead", "gpt-6.1-sol", "medium", "https://api.openai.com/v1/responses", "OPENAI_API_KEY"),
])
def test_provider_profile_and_explicit_credentials(name, model, effort, endpoint, key_env, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong.invalid")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://wrong.invalid")
    transport, calls = exchange(response(model=model))
    result = api.execute(name, {"input": "проверка"}, environ=KEYS, transport=transport)
    assert len(calls) == 1 and str(calls[0].url) == endpoint
    assert calls[0].headers["authorization"] == "Bearer " + KEYS[key_env]
    sent = json.loads(calls[0].content)
    assert sent["model"] == model and sent["reasoning"] == {"effort": effort}
    assert sent["max_output_tokens"] == 4096 and sent["stream"] is False
    assert all(value not in calls[0].content.decode() for value in KEYS.values())
    if name == "deepseek-lead":
        assert "store" not in sent and "include" not in sent
    else:
        assert sent["store"] is False and "reasoning.encrypted_content" in sent["include"]
    assert result["model"] == model and result["requested_model"] == model
    assert result["output_text"] == "готово" and result["status"] == "completed"


def test_cached_and_reasoning_tokens_are_subsets():
    transport, _ = exchange()
    result = api.execute("luna-worker", {"input": "hello"}, environ=KEYS, transport=transport)
    assert result["usage"] == {"input_tokens": 100, "output_tokens": 30, "cached_input_tokens": 80,
                               "cache_write_input_tokens": 10, "reasoning_tokens": 20,
                               "total_tokens": 130, "usage_known": True, "cost_usd": None}


def test_missing_usage_remains_unknown():
    transport, _ = exchange(response(usage=None))
    result = api.execute("deepseek-lead", {"input": "hello"}, environ=KEYS, transport=transport)
    assert result["usage"]["input_tokens"] is None
    assert result["usage"]["cached_input_tokens"] is None
    assert result["usage"]["usage_known"] is False and result["usage"]["cost_usd"] is None


@pytest.mark.parametrize("changes", [
    {"input": ""}, {"input": False}, {"input": [False]}, {"effort": []}, {"effort": "ultra"},
    {"max_output_tokens": True}, {"max_output_tokens": 15}, {"max_output_tokens": 128001},
    {"instructions": {}}, {"base_url": "https://wrong.invalid"}, {"model": "other"},
    {"api_key": "fake-key"}, {"stream": True}, {"previous_response_id": "resp_previous"},
    {"tools": [{"type": "web_search"}]}, {"tools": [TOOL, TOOL]},
    {"input": [{"role": [], "content": "hello"}]}, {"input": [{"type": []}]},
    {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "https://wrong.invalid"}]}]},
])
def test_invalid_request_is_rejected_before_network(changes):
    transport, calls = exchange()
    with pytest.raises(api.ExecutorError) as caught:
        api.execute("luna-worker", {"input": "hello", **changes}, environ=KEYS, transport=transport)
    assert caught.value.charge_possible is False and calls == []


def test_luna_effort_is_pinned_and_sol_can_be_assigned_effort():
    with pytest.raises(api.ExecutorError, match="invalid_effort"):
        api.prepare("luna-worker", {"input": "hello", "effort": "medium"})
    _, body = api.prepare("sol-lead", {"input": "hello", "effort": "high"})
    assert body["reasoning"] == {"effort": "high"}


def test_request_and_response_size_limits(monkeypatch):
    transport, calls = exchange()
    with pytest.raises(api.ExecutorError, match="request_too_large"):
        api.execute("luna-worker", {"input": "x" * api.MAX_REQUEST_BYTES}, environ=KEYS, transport=transport)
    assert calls == []
    monkeypatch.setattr(api, "MAX_RESPONSE_BYTES", 64)
    with pytest.raises(api.ExecutorError, match="response_too_large") as caught:
        api.execute("luna-worker", {"input": "hello"}, environ=KEYS, transport=transport)
    assert caught.value.charge_possible and len(calls) == 1


@pytest.mark.parametrize("environment", [{}, {"DEEPSEEK_API_KEY": "other-provider-key"},
                                         {"ANTHROPIC_API_KEY": "other-provider-key"}])
def test_missing_provider_key_never_falls_back(environment):
    transport, calls = exchange()
    with pytest.raises(api.ExecutorError, match="missing_api_key"):
        api.execute("luna-worker", {"input": "hello"}, environ=environment, transport=transport)
    assert calls == []


@pytest.mark.parametrize("key", ["key\nsecret", "key with space", "ключ"])
def test_bad_key_is_not_echoed(key):
    transport, calls = exchange()
    with pytest.raises(api.ExecutorError) as caught:
        api.execute("luna-worker", {"input": "hello"}, environ={"OPENAI_API_KEY": key}, transport=transport)
    assert key not in json.dumps(caught.value.as_dict()) and calls == []


@pytest.mark.parametrize("status", [302, 400, 401, 429, 500, 503])
def test_http_errors_and_redirects_are_not_retried_or_echoed(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://wrong.invalid"},
                              json={"error": {"message": KEYS["OPENAI_API_KEY"]}})

    with pytest.raises(api.ExecutorError) as caught:
        api.execute("luna-worker", {"input": "hello"}, environ=KEYS, transport=httpx.MockTransport(handler))
    assert len(calls) == 1 and caught.value.http_status == status and caught.value.charge_possible
    assert KEYS["OPENAI_API_KEY"] not in str(caught.value.as_dict())


def test_timeout_is_not_retried_and_billing_is_unknown():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(KEYS["OPENAI_API_KEY"])

    with pytest.raises(api.ExecutorError, match="network_error") as caught:
        api.execute("luna-worker", {"input": "hello"}, environ=KEYS, transport=httpx.MockTransport(handler))
    assert len(calls) == 1 and caught.value.charge_possible
    assert KEYS["OPENAI_API_KEY"] not in str(caught.value)


@pytest.mark.parametrize("data", [b"not json", b'{"usage":NaN}', b"\xff"])
def test_malformed_response_is_not_a_success(data):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=data))
    with pytest.raises(api.ExecutorError, match="invalid_response_json") as caught:
        api.execute("luna-worker", {"input": "hello"}, environ=KEYS, transport=transport)
    assert caught.value.charge_possible


@pytest.mark.parametrize("usage", [False, {"input_tokens": True}, {"output_tokens": -1},
                                    {"input_tokens_details": []}, {"input_tokens": 2, "input_tokens_details": {"cached_tokens": 3}},
                                    {"output_tokens": 2, "output_tokens_details": {"reasoning_tokens": 3}},
                                    {"input_tokens": 10, "output_tokens": 5, "total_tokens": 99}])
def test_impossible_usage_is_not_reported_as_valid(usage):
    transport, _ = exchange(response(usage=usage))
    with pytest.raises(api.ExecutorError, match="invalid_usage"):
        api.execute("luna-worker", {"input": "hello"}, environ=KEYS, transport=transport)


@pytest.mark.parametrize("changes", [{"status": []}, {"status": "in_progress"}, {"output": None},
                                      {"output": [None]}, {"output": [{"type": "web_search_call"}]}])
def test_invalid_or_unsupported_response_items_stop(changes):
    transport, _ = exchange(response(**changes))
    with pytest.raises(api.ExecutorError):
        api.execute("luna-worker", {"input": "hello"}, environ=KEYS, transport=transport)


def test_function_calls_are_data_and_history_is_preserved(tmp_path):
    marker = tmp_path / "must-not-exist"
    output = [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque-unit-value"},
              {"type": "function_call", "call_id": "call_1", "name": "run_tests",
               "arguments": json.dumps({"command": f"touch {marker}"})}]
    transport, calls = exchange(response(output=output))
    result = api.execute("luna-worker", {"input": "hello", "tools": [TOOL]}, environ=KEYS, transport=transport)
    assert result["output"] == output and len(result["function_calls"]) == 1
    assert result["output_text"] == "" and not marker.exists() and len(calls) == 1
    history = [{"role": "user", "content": "hello"}, *result["output"],
               {"type": "function_call_output", "call_id": "call_1", "output": "not executed"}]
    _, body = api.prepare("luna-worker", {"input": history, "tools": [TOOL]})
    assert body["input"] == history


def test_undeclared_function_is_rejected():
    output = [{"type": "function_call", "call_id": "call_1", "name": "run_anything", "arguments": "{}"}]
    transport, _ = exchange(response(output=output))
    with pytest.raises(api.ExecutorError, match="invalid_function_call"):
        api.execute("luna-worker", {"input": "hello", "tools": [TOOL]}, environ=KEYS, transport=transport)


def test_doctor_and_prepare_are_local_and_never_echo_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", KEYS["OPENAI_API_KEY"])
    monkeypatch.setattr(api.httpx, "Client", lambda **kwargs: pytest.fail("Unexpected network client"))
    assert api.run(["doctor", "--profile", "luna-worker"]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["remote_access_verified"] is False and checked["network_used"] is False
    path = tmp_path / "request.json"
    path.write_text('{"input":"hello"}')
    assert api.run(["prepare", "--profile", "luna-worker", "--request", str(path)]) == 0
    prepared = capsys.readouterr().out
    assert KEYS["OPENAI_API_KEY"] not in prepared and json.loads(prepared)["network_used"] is False


@pytest.mark.parametrize("status,refusal,expected", [("completed", False, 0), ("incomplete", False, 3),
                                                    ("failed", False, 3), ("completed", True, 3)])
def test_cli_preserves_partial_usage_and_returns_nonzero(status, refusal, expected, monkeypatch, tmp_path, capsys):
    body = response(status=status)
    if refusal:
        body["output"][0]["content"] = [{"type": "refusal", "refusal": "Cannot complete"}]
    transport, calls = exchange(body)
    real_client = httpx.Client

    def client(**kwargs):
        return real_client(**{**kwargs, "transport": transport})

    monkeypatch.setenv("OPENAI_API_KEY", KEYS["OPENAI_API_KEY"])
    monkeypatch.setattr(api.httpx, "Client", client)
    path = tmp_path / "request.json"
    path.write_text('{"input":"hello"}')
    assert api.run(["request", "--profile", "luna-worker", "--request", str(path)]) == expected
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == status and result["usage"]["total_tokens"] == 130 and len(calls) == 1


def test_cli_missing_key_stops_before_network(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(api.httpx, "Client", lambda **kwargs: pytest.fail("Unexpected network client"))
    path = tmp_path / "request.json"
    path.write_text('{"input":"hello"}')
    assert api.run(["request", "--profile", "luna-worker", "--request", str(path)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "missing_api_key" and result["charge_possible"] is False


def test_cli_entry_point_from_another_directory(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "ours/bin/api-executor"), "profiles"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and result.stderr == ""
    assert {p["model"] for p in json.loads(result.stdout)["profiles"]} == {"deepseek-flash", "gpt-6-luna", "gpt-6.1-sol"}


def test_cli_bad_json_and_missing_file_are_clean_errors(tmp_path, capsys):
    path = tmp_path / "request.json"
    path.write_text('{"input":NaN}')
    assert api.run(["prepare", "--profile", "luna-worker", "--request", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_request_json"
    assert api.run(["prepare", "--profile", "luna-worker", "--request", str(tmp_path / "absent")]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "request_read_failed"
