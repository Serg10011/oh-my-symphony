"""Per-model output grants and large payloads, using only fake providers."""

from dataclasses import replace
from datetime import date
import json

import httpx
import pytest

import test_api_budget as guarded
from test_api_project import repository
from test_api_project_session import state_file as project_state, tool
from test_api_session import KEYS, delegate, exchange, frame, state_file
from test_api_session_resources import resources

api, session = guarded.api, guarded.session


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for key in [*KEYS, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(guarded.budget, "today", lambda: date(2026, 11, 4))


@pytest.mark.parametrize("profile,model,cap", [
    ("deepseek-lead", "deepseek-flash", 393216),
    ("luna-worker", "gpt-6-luna", 128000),
    ("sol-lead", "gpt-6.1-sol", 128000),
])
def test_provider_boundary_is_sent_exactly_and_excess_is_rejected_before_network(profile, model, cap):
    transport, sent = exchange([frame(model=model)])
    api.execute(profile, {"input": "Complete the task", "max_output_tokens": cap}, environ=KEYS, transport=transport)
    assert sent[0]["body"]["max_output_tokens"] == cap
    with pytest.raises(api.ExecutorError, match="invalid_output_limit") as caught:
        api.execute(profile, {"input": "Too much", "max_output_tokens": cap + 1}, environ=KEYS, transport=transport)
    assert not caught.value.charge_possible and len(sent) == 1


def test_an_unknown_model_never_borrows_another_models_allowance(monkeypatch):
    monkeypatch.setitem(api.PROFILES, "sol-lead", replace(api.PROFILES["sol-lead"], model="unverified-model"))
    transport, sent = exchange([])
    with pytest.raises(api.ExecutorError, match="model_output_limit_unverified"):
        api.execute("sol-lead", {"input": "No fallback"}, environ=KEYS, transport=transport)
    assert not sent


@pytest.mark.parametrize("worker", [None, "luna-worker"])
def test_sol_defaults_and_visible_grants_fit_both_supported_compositions(tmp_path, worker):
    path = state_file(tmp_path, lead_profile="sol-lead", worker_profile=worker)
    expected = {"sol-lead": 128000} | ({"luna-worker": 128000} if worker else {})
    before = path.read_bytes()
    shown = session.show(path)
    assert shown["limits"]["output_tokens_per_call"] == 128000 and shown["output_limits"] == expected
    assert path.read_bytes() == before
    frames = ([frame(None, [delegate()], model="gpt-6.1-sol"), frame("Worker", model="gpt-6-luna")]
              if worker else []) + [frame("Final", model="gpt-6.1-sol")]
    transport, sent = exchange(frames)
    result = session.run(path, environ={"OPENAI_API_KEY": KEYS["OPENAI_API_KEY"]}, transport=transport)
    assert result["status"] == "dialogue_complete" and result["output_limits"] == expected
    assert all(r["body"]["max_output_tokens"] == resources(r["body"])["response_output_cap"] == 128000 for r in sent)


@pytest.mark.parametrize("changed_cap,expected", [(256000, 128000), (64000, 64000)])
def test_pinned_worker_grant_cannot_grow_and_still_respects_a_lower_provider_limit(tmp_path, monkeypatch, changed_cap, expected):
    path = state_file(tmp_path)
    before = path.read_bytes()
    monkeypatch.setitem(api.MODEL_OUTPUT_LIMITS, "gpt-6-luna", changed_cap)
    assert session.show(path)["output_limits"] == {"deepseek-lead": 393216, "luna-worker": 128000}
    assert path.read_bytes() == before
    transport, sent = exchange([frame(None, [delegate()]), frame("Worker", model="gpt-6-luna"), frame("Final")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and sent[1]["body"]["max_output_tokens"] == expected
    assert json.loads(path.read_text())["output_limits"]["luna-worker"] == 128000


@pytest.mark.parametrize("pinned", [None, {}, {"deepseek-lead": 393216},
    {"deepseek-lead": 393216, "luna-worker": True},
    {"deepseek-lead": 393216, "luna-worker": 15},
    {"deepseek-lead": 393216, "luna-worker": 393217},
    {"deepseek-lead": 393216, "luna-worker": 128000, "sol-lead": 128000},
])
def test_malformed_pinned_grants_are_rejected_without_touching_state_or_network(tmp_path, monkeypatch, pinned):
    path = state_file(tmp_path)
    raw = json.loads(path.read_text())
    raw["output_limits"] = pinned
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    monkeypatch.setattr(api.httpx, "Client", lambda **kwargs: pytest.fail("Unexpected network"))
    with pytest.raises(session.SessionError, match="invalid_output_limits"):
        session.run(path, environ=KEYS)
    assert path.read_bytes() == before


def test_reported_large_outputs_share_the_total_and_reserve_the_actual_role_cap(tmp_path):
    path = state_file(tmp_path, budget={"max_usd": "2", "input_tokens_total": 3_000_000})
    frames = [guarded.frame("deepseek-flash", output=40000, calls=[delegate()]),
              guarded.frame("gpt-6-luna", output=50000), guarded.frame("deepseek-flash", output=60000)]
    transport, sent = guarded.exchange(frames)
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["output_tokens_committed"] == 150000
    assert [r["body"]["max_output_tokens"] for r in guarded.generated(sent)] == [393216, 128000, 393216]
    calls = json.loads(path.read_text())["calls"]
    assert [c["output_cap"] for c in calls] == [393216, 128000, 393216]
    assert calls[0]["budget_reservation"]["cost_usd"] == "0.786432"
    assert calls[1]["budget_reservation"]["cost_usd"] == "0.070404"
    assert result["budget"]["preflight_requests"] == 3 and session.show(path) == result


def test_unknown_worker_output_commits_its_own_grant_and_never_retries(tmp_path):
    path = state_file(tmp_path)
    transport, sent = exchange([frame(None, [delegate()]), frame("Worker", model="gpt-6-luna", usage=None)])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "unknown_usage" and result["output_tokens_committed"] == 128008
    before = path.read_bytes()
    assert session.run(path, environ={}, transport=transport) == result
    assert path.read_bytes() == before and len(sent) == 2


def test_total_output_and_lead_reserve_can_be_smaller_than_the_model_grant(tmp_path):
    path = state_file(tmp_path, limits={"output_tokens_total": 100000})
    transport, sent = exchange([frame(None, [delegate()]), frame("Worker", model="gpt-6-luna"), frame("Final")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete"
    assert [r["body"]["max_output_tokens"] for r in sent] == [100000, 99976, 99984]
    assert result["output_limits"] == {"deepseek-lead": 393216, "luna-worker": 128000}


def test_large_responses_are_kept_reused_and_read_back_without_old_byte_barriers(tmp_path):
    root = repository(tmp_path)
    path = project_state(tmp_path, root)
    # Exercise byte transport/storage, not a claim about any provider's tokenizer.
    text = "x" * (5 * 1024 * 1024)
    usage = {"input_tokens": 12, "output_tokens": 100000, "total_tokens": 100012}
    frames = [frame(text, [tool(call_id=f"read_{i}")], usage=usage) for i in range(2)] + [frame("Final")]
    assert len(json.dumps(frames[0]).encode()) > 4 * 1024 * 1024
    transport, sent = exchange(frames)
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["calls"] == 3
    assert 1 * 1024 * 1024 < len(json.dumps(sent[-1]["body"]).encode()) < 16 * 1024 * 1024
    assert 16 * 1024 * 1024 < path.stat().st_size < 128 * 1024 * 1024
    assert result["output_tokens_committed"] == 200008 and result["project"]["file_calls"] == 2
    before = path.read_bytes()
    assert session.show(path) == session.run(path, environ={}, transport=transport) == result
    assert path.read_bytes() == before and len(sent) == 3


def test_a_new_response_byte_ceiling_still_stops_without_retry():
    calls = []
    class Oversized(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(257):
                yield b"x" * 65536
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, stream=Oversized())
    with pytest.raises(api.ExecutorError, match="response_too_large") as caught:
        api.execute("deepseek-lead", {"input": "Bounded response"}, environ=KEYS, transport=httpx.MockTransport(handle))
    assert caught.value.charge_possible and len(calls) == 1
