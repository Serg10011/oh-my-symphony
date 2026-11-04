"""Large session ceilings, exact legacy grants and bounded transport offline."""

from datetime import date
import json

import httpx
import pytest

from test_api_project import repository
from test_api_project_session import state_file, tool
from test_api_session import KEYS, delegate, exchange, frame
from test_api_session_resources import resources, tool_outputs
import test_api_budget as guarded

session, api = guarded.session, guarded.api


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for key in [*KEYS, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(guarded.budget, "today", lambda: date(2026, 11, 4))


def test_long_lead_worker_chain_passes_old_call_and_total_output_limits(tmp_path):
    root = repository(tmp_path)
    path = state_file(tmp_path, root)
    limits = {"max_calls": 1000, "output_tokens_per_call": 393216,
              "output_tokens_total": 2097152, "request_bytes": 16777216}
    assert session.show(path)["limits"] == limits
    usage = {"input_tokens": 12, "output_tokens": 256, "total_tokens": 268}
    frames = [frame(None, [tool(call_id=f"lead_{i}")], usage=usage) for i in range(66)]
    frames += [frame(None, [delegate("assign")], usage=usage)]
    frames += [frame(None, [tool(call_id=f"worker_{i}")], model="gpt-6-luna", usage=usage) for i in range(66)]
    frames += [frame("Worker result", model="gpt-6-luna", usage=usage), frame("Final", usage=usage)]
    transport, sent = exchange(frames)
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["calls"] == len(sent) == 135
    assert result["output_tokens_committed"] == 34560 and not result["budget_enforced"]
    assert result["project"]["file_calls"] == 132 and not result["tool_errors"]
    assert result["parts"] == {"part_1": {"attempts": 1}}
    assert resources(sent[64]["body"])["calls_remaining"] == 936
    assert resources(sent[-1]["body"])["calls_remaining"] == 866
    assert all(r["body"]["max_output_tokens"] == (393216 if r["body"]["model"] == "deepseek-flash" else 128000)
               for r in sent)
    assert tool_outputs(sent[-1]["body"])["assign"]["text"] == "Worker result"
    before = path.read_bytes()
    assert session.show(path) == session.run(path, environ={}, transport=transport) == result
    assert path.read_bytes() == before and len(sent) == 135


def test_explicit_large_ceiling_still_returns_the_reserved_lead_call(tmp_path):
    root = repository(tmp_path)
    path = state_file(tmp_path, root, limits={"max_calls": 70})
    frames = [frame(None, [delegate("assign")])]
    frames += [frame(None, [tool(call_id=f"file_{i}")], model="gpt-6-luna") for i in range(68)]
    frames += [frame("Final after the reserved return")]
    transport, sent = exchange(frames)
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["calls"] == len(sent) == 70
    assert tool_outputs(sent[-1]["body"])["assign"]["error"] == "budget_reserves_lead_return"
    assert resources(sent[-2]["body"])["last_available_call"] is True
    assert resources(sent[-1]["body"])["calls_remaining"] == 1
    assert session.show(path) == result


def test_large_call_limit_and_budget_preflights_are_shared_and_persisted(tmp_path):
    root = repository(tmp_path)
    path = state_file(tmp_path, root, lead_profile="sol-lead", worker_profile=None,
                      limits={"max_calls": 70}, budget={"max_usd": "2", "input_tokens_total": 100000})
    frames = [guarded.frame(calls=[tool(call_id=f"file_{i}")]) for i in range(70)]
    transport, sent = guarded.exchange(frames)
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "call_limit" and result["calls"] == 70
    assert len(guarded.generated(sent)) == result["budget"]["preflight_requests"] == 70
    assert result["budget"]["input_tokens_committed"] == 840
    assert result["budget"]["max_usd"] == "2.000000" and result["project"]["file_calls"] == 70
    before = path.read_bytes()
    assert session.show(path) == session.run(path, environ={}, transport=transport) == result
    assert path.read_bytes() == before and len(sent) == 140


@pytest.mark.parametrize("legacy", [
    {"max_calls": 8, "output_tokens_per_call": 2048, "output_tokens_total": 16384, "request_bytes": 262144},
    {"max_calls": 24, "output_tokens_per_call": 16384, "output_tokens_total": 98304, "request_bytes": 262144},
    {"max_calls": 1000, "output_tokens_per_call": 32768, "output_tokens_total": 2097152, "request_bytes": 1048576},
])
def test_saved_legacy_defaults_are_not_upgraded_on_read_or_run(tmp_path, monkeypatch, legacy):
    root = repository(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(session, "DEFAULT_LIMITS", legacy)
        path = state_file(tmp_path, root)
    old = json.loads(path.read_text())
    del old["output_limits"]
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    assert session.show(path)["limits"] == legacy and path.read_bytes() == before
    transport, sent = exchange([frame("Result within the original grant")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["limits"] == legacy
    assert sent[0]["body"]["max_output_tokens"] == legacy["output_tokens_per_call"]
    assert resources(sent[0]["body"])["calls_remaining"] == legacy["max_calls"]
    assert json.loads(path.read_text())["spec"]["limits"] == legacy


def test_explicit_limits_above_old_ceiling_and_partial_defaults_are_retained(tmp_path):
    path = tmp_path / "large.json"
    session.create(path, {"task": "Explicit allowance", "limits": {"max_calls": 5000, "output_tokens_total": 4000000}})
    assert session.show(path)["limits"] == {"max_calls": 5000, "output_tokens_per_call": 393216,
                                           "output_tokens_total": 4000000, "request_bytes": 16777216}


@pytest.mark.parametrize("limits", [
    {"max_calls": 0}, {"max_calls": -1}, {"max_calls": None}, {"max_calls": 1000.0},
    {"max_calls": 100001}, {"output_tokens_total": 100000001},
])
def test_invalid_large_allowances_do_not_create_a_session(tmp_path, limits):
    path = tmp_path / "invalid.json"
    with pytest.raises(session.SessionError, match="invalid_limits"):
        session.create(path, {"task": "Invalid allowance", "limits": limits})
    assert not path.exists()


def test_larger_call_default_does_not_bypass_an_explicit_money_budget(tmp_path):
    path = guarded.create(tmp_path, dollars="0.03")
    transport, sent = guarded.exchange([])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["limits"]["max_calls"] == 1000 and result["budget"]["max_usd"] == "0.030000"
    assert result["stop_reason"] == "money_budget_exhausted" and not guarded.generated(sent)
    assert session.show(path) == result


def test_new_output_default_is_fully_reserved_on_unknown_usage(tmp_path):
    path = tmp_path / "unknown.json"
    session.create(path, {"task": "Unknown charge"})
    transport, sent = exchange([frame(usage=None)])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "unknown_usage" and result["output_tokens_committed"] == 393216
    assert session.run(path, environ={}, transport=transport) == result and len(sent) == 1


def test_project_context_can_exceed_the_old_request_default(tmp_path):
    root = repository(tmp_path, {"src/main.py": "x" * 4096})
    path = state_file(tmp_path, root, context="c" * 130000)
    transport, sent = exchange([frame(None, [tool(call_id=f"file_{i}") for i in range(32)]), frame("Final")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and len(sent) == 2
    assert 262144 < len(json.dumps(sent[-1]["body"]).encode()) <= 1048576
    assert session.show(path) == result


@pytest.mark.parametrize("profile,model", [("deepseek-lead", "deepseek-flash"), ("luna-worker", "gpt-6-luna")])
def test_generation_has_more_read_time_without_extending_other_timeouts(profile, model):
    observed = []
    def handle(request):
        observed.append(request.extensions["timeout"])
        return httpx.Response(200, json=frame(model=model))
    api.execute(profile, {"input": "Complete the task"}, environ=KEYS, transport=httpx.MockTransport(handle))
    assert observed == [{"connect": 120, "read": 600, "write": 120, "pool": 120}]


@pytest.mark.parametrize("profile", ["sol-lead", "deepseek-lead"])
def test_preflight_keeps_its_original_timeout(profile):
    observed = []
    def handle(request):
        observed.append(request.extensions["timeout"])
        value = ({"object": "list", "data": [{"id": "deepseek-flash", "context_window": 1048576, "max_output_tokens": 32768}]}
                 if request.method == "GET" else {"object": "response.input_tokens", "input_tokens": 12})
        return httpx.Response(200, json=value)
    api.measure_input(profile, {"input": "Count this"}, environ=KEYS, transport=httpx.MockTransport(handle))
    assert observed == [{"connect": 120, "read": 120, "write": 120, "pool": 120}]
