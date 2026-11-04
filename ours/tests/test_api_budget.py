"""Shared reservations and preflight protocol without real provider requests."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ours/hooks"))
import api_budget as budget  # noqa: E402
import api_executor as api  # noqa: E402
import api_session as session  # noqa: E402

KEYS = {"OPENAI_API_KEY": "fake-openai-budget-key", "DEEPSEEK_API_KEY": "fake-deepseek-budget-key"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(budget, "today", lambda: date(2026, 11, 3))
    for name in [*KEYS, "ANTHROPIC_API_KEY"]:
        monkeypatch.delenv(name, raising=False)


def create(tmp_path, *, model="sol-lead", worker=None, dollars="2", inputs=3_000_000, **changes):
    path = tmp_path / "budget.json"
    session.create(path, {"task": "Нужен проверяемый результат", "lead_profile": model, "worker_profile": worker,
                          "budget": {"max_usd": dollars, "input_tokens_total": inputs}, **changes})
    return path


def frame(model="gpt-6.1-sol", *, output=8, incoming=12, calls=(), cached=0, written=0, **changes):
    items = list(calls) or [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Результат"}]}]
    return {"id": "resp_budget", "model": model, "status": "completed", "service_tier": "default", "output": items,
            "usage": {"input_tokens": incoming, "output_tokens": output, "total_tokens": incoming + output,
                      "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written},
                      "output_tokens_details": {"reasoning_tokens": 3}}} | changes


def delegate(call_id="call_1", kind="start"):
    return {"type": "function_call", "name": "delegate_luna", "call_id": call_id,
            "arguments": json.dumps({"part_id": "part", "message": "Сделай законченную часть", "kind": kind})}


def exchange(frames, *, count=12, metadata=None, probe_error=None):
    frames, sent = iter(frames), []
    default_metadata = {"object": "list", "data": [{"id": "deepseek-flash", "object": "model",
                         "context_window": 1048576, "max_output_tokens": 393216}]}

    def handle(request):
        payload = json.loads(request.content) if request.content else None
        sent.append({"url": str(request.url), "method": request.method, "body": payload,
                     "auth": request.headers["authorization"]})
        if request.url.path.endswith("/input_tokens") or request.method == "GET":
            if probe_error is not None:
                if isinstance(probe_error, Exception):
                    raise probe_error
                return probe_error
            return httpx.Response(200, json=(metadata if metadata is not None else default_metadata)
                                  if request.method == "GET" else {"object": "response.input_tokens", "input_tokens": count})
        item = next(frames)
        if isinstance(item, BaseException):
            raise item
        return httpx.Response(200, json=item)

    return httpx.MockTransport(handle), sent


def generated(sent):
    return [r for r in sent if r["method"] == "POST" and r["url"].endswith("/responses")]


def test_entire_chain_uses_one_budget_and_preserves_metadata(tmp_path):
    path = create(tmp_path, model="deepseek-lead", worker="luna-worker")
    transport, sent = exchange([frame("deepseek-flash", calls=[delegate()]), frame("gpt-6-luna"),
                                frame("deepseek-flash", calls=[delegate("call_2", "correction")]),
                                frame("gpt-6-luna"), frame("deepseek-flash")])
    result = session.run(path, environ=KEYS, transport=transport, require_budget=True)
    assert result["status"] == "dialogue_complete" and result["calls"] == 5
    assert result["budget"]["preflight_requests"] == 5 and result["budget"]["input_tokens_committed"] == 60
    assert len(generated(sent)) == 5 and len(sent) == 10
    raw = json.loads(path.read_text())
    for call in raw["calls"]:
        reservation = call["budget_reservation"]
        assert budget.money(reservation["settled_cost_usd"]) <= budget.money(reservation["cost_usd"])
        assert reservation["input_tokens"] == (1048576 if call["profile"] == "deepseek-lead" else 12)
    for request in sent:
        if "deepseek.com" in request["url"]:
            assert request["auth"] == "Bearer " + KEYS["DEEPSEEK_API_KEY"]
        else:
            assert request["auth"] == "Bearer " + KEYS["OPENAI_API_KEY"]
    assert result["cost_usd"] is None and not result["budget"]["contract_breached"]
    assert all(key not in path.read_text() for key in KEYS.values())
    assert session.show(path) == result
    assert session.run(path, environ={}, transport=transport, require_budget=True) == result and len(sent) == 10


def test_input_counter_receives_the_same_context_and_only_supported_fields(tmp_path):
    path = create(tmp_path, context="Контекст проекта", worker="luna-worker")
    transport, sent = exchange([frame()])
    session.run(path, environ=KEYS, transport=transport)
    counter, generation = sent
    assert counter["url"] == "https://api.openai.com/v1/responses/input_tokens"
    assert set(counter["body"]) == {"model", "input", "instructions", "tools", "reasoning"}
    assert all(counter["body"][k] == generation["body"][k] for k in counter["body"])
    assert generation["body"]["service_tier"] == "default"
    assert generation["body"]["store"] is False


@pytest.mark.parametrize("inputs,dollars,reason", [(11, "2", "input_budget_exhausted"),
                                                 (3_000_000, "0.001", "money_budget_exhausted")])
def test_exhaustion_is_detected_before_generation(tmp_path, inputs, dollars, reason):
    path = create(tmp_path, inputs=inputs, dollars=dollars)
    transport, sent = exchange([])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == reason and result["calls"] == 0
    assert not generated(sent) and len(sent) == 1 and result["budget"]["generation_cost_upper_usd"] == "0.000000"


def test_worker_spending_cannot_use_an_independent_budget(tmp_path):
    path = create(tmp_path, worker="luna-worker", dollars="0.023", limits={"output_tokens_per_call": 2048})
    transport, sent = exchange([frame(output=2000, calls=[delegate()])])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "money_budget_exhausted" and result["calls"] == 1
    assert result["budget"]["generation_cost_upper_usd"] == "0.022027"
    assert len(generated(sent)) == 1 and result["budget"]["preflight_requests"] == 2


def test_deepseek_reservation_uses_capacity_not_character_estimation(tmp_path):
    path = create(tmp_path, model="deepseek-lead", worker="luna-worker", dollars="0.10")
    transport, sent = exchange([])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "money_budget_exhausted" and not generated(sent)
    raw = json.loads(path.read_text())
    assert raw["preflights"][0]["result"] == {"input_tokens_upper_bound": 1048576, "basis": "full_model_context"}
    assert sent[0]["method"] == "GET" and sent[0]["url"] == "https://api.deepseek.com/models"


@pytest.mark.parametrize("count", [0, True, -1, 1_050_001, "12", None])
def test_invalid_counter_result_never_falls_back_to_an_estimate(tmp_path, count):
    path = create(tmp_path)
    transport, sent = exchange([], count=count)
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "preflight_error" and not generated(sent)
    assert json.loads(path.read_text())["preflights"][0]["error"]["error"] == "invalid_input_count"


@pytest.mark.parametrize("models", [[], [{"id": "other"}], [{"id": "deepseek-flash"}],
                                  [{"id": "deepseek-flash", "context_window": 2000000, "max_output_tokens": 393216}],
                                  [{"id": "deepseek-flash", "context_window": True, "max_output_tokens": 393216}],
                                  [{"id": "deepseek-flash", "context_window": 1048576, "max_output_tokens": 15}],
                                  [{"id": "deepseek-flash"}, {"id": "deepseek-flash"}]])
def test_unverified_deepseek_capacity_stops_before_generation(tmp_path, models):
    path = create(tmp_path, model="deepseek-lead", worker="luna-worker")
    transport, sent = exchange([], metadata={"object": "list", "data": models})
    assert session.run(path, environ=KEYS, transport=transport)["stop_reason"] == "preflight_error"
    assert not generated(sent)


@pytest.mark.parametrize("error", [httpx.Response(302, headers={"Location": "https://wrong.invalid"}),
                                 httpx.Response(429, text="do not expose fake-secret"),
                                 httpx.ReadTimeout("do not expose fake-secret")])
def test_probe_error_has_no_generation_retry_redirect_or_secret_leak(tmp_path, error):
    path = create(tmp_path)
    transport, sent = exchange([], probe_error=error)
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "preflight_error" and len(sent) == 1
    assert "fake-secret" not in path.read_text()
    session.run(path, environ={}, transport=transport)
    assert len(sent) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("failure"), KeyboardInterrupt()])
def test_unknown_generation_retains_full_reservation_and_never_repeats(tmp_path, failure):
    path = create(tmp_path)
    transport, sent = exchange([failure])
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            session.run(path, environ=KEYS, transport=transport)
    else:
        session.run(path, environ=KEYS, transport=transport)
    result = session.run(path, environ={}, transport=transport)
    assert result["status"] == "stopped" and result["calls"] == 1 and len(sent) == 2
    assert result["budget"]["input_tokens_committed"] == 12
    raw = json.loads(path.read_text())
    assert result["budget"]["generation_cost_upper_usd"] == raw["calls"][0]["budget_reservation"]["cost_usd"]


@pytest.mark.parametrize("changes,reason", [({"incoming": 13}, "billing_contract_exceeded"),
                                           ({"output": 2049}, "billing_contract_exceeded"),
                                           ({"service_tier": "priority"}, "billing_contract_unverified"),
                                           ({"service_tier": None}, "billing_contract_unverified"),
                                           ({"model": "different-model"}, "billing_contract_unverified"),
                                           ({"written": 13}, "untrusted_budget_usage")])
def test_unverified_billing_contract_does_not_release_reservation(tmp_path, changes, reason):
    path = create(tmp_path, limits={"output_tokens_per_call": 2048})
    transport, sent = exchange([frame(**changes)])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == reason and result["budget"]["contract_breached"]
    record = json.loads(path.read_text())["calls"][0]["budget_reservation"]
    assert budget.money(result["budget"]["generation_cost_upper_usd"]) >= budget.money(record["cost_usd"])
    assert len(generated(sent)) == 1


def test_unknown_usage_is_not_free_and_reading_expired_history_is_passive(tmp_path, monkeypatch):
    path = create(tmp_path)
    transport, _ = exchange([frame(usage=None)])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "unknown_usage"
    assert budget.money(result["budget"]["generation_cost_upper_usd"]) > 0
    monkeypatch.setattr(budget, "today", lambda: date(2027, 1, 1))
    assert session.show(path) == result


@pytest.mark.parametrize("incoming,outgoing", [(10**50, 8), (12, 10**50)])
def test_extreme_usage_stops_without_a_decimal_exception(tmp_path, incoming, outgoing):
    path = create(tmp_path)
    transport, sent = exchange([frame(incoming=incoming, output=outgoing)])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "untrusted_budget_usage"
    assert result["budget"]["contract_breached"] and len(generated(sent)) == 1


def test_expired_or_changed_tariff_blocks_run_before_network(tmp_path, monkeypatch):
    path = create(tmp_path)
    monkeypatch.setattr(api.httpx, "Client", lambda **kwargs: pytest.fail("Unexpected network"))
    with monkeypatch.context() as patch:
        patch.setattr(budget, "today", lambda: date(2026, 11, 11))
        with pytest.raises(session.SessionError, match="tariff_expired"):
            session.run(path, environ=KEYS)
    raw = json.loads(path.read_text())
    raw["budget_catalog"]["version"] = "older"
    path.write_text(json.dumps(raw))
    with pytest.raises(session.SessionError, match="tariff_snapshot_changed"):
        session.run(path, environ=KEYS)


def test_cost_ceilings_cover_cache_writes_long_context_and_rounding():
    cat = budget.snapshot()
    # Twelve-hour writes and regional processing are reserved conservatively.
    assert budget.cost(cat, "gpt-6-luna", 1_000_000, 0) == "0.550000"
    assert budget.cost(cat, "gpt-6.1-sol", 1000, 100, cached=800, written=100) == "0.001958"
    assert budget.cost(cat, "deepseek-flash", 1000, 100, cached=800) == "0.000185"
    assert budget.cost(cat, "gpt-6-luna", 1, 0, written=0) == "0.000001"
    assert budget.money(budget.cost(cat, "gpt-6-luna", 272001, 10)) > budget.money(budget.cost(cat, "gpt-6-luna", 272000, 10))


@pytest.mark.parametrize("raw", [{"max_usd": 2, "input_tokens_total": 1}, {"max_usd": "NaN", "input_tokens_total": 1},
                                {"max_usd": "0", "input_tokens_total": 1}, {"max_usd": "1e2", "input_tokens_total": 1},
                                {"max_usd": "0.0000001", "input_tokens_total": 1}, {"max_usd": "1", "input_tokens_total": True},
                                {"max_usd": "1", "input_tokens_total": 0}, {"max_usd": "1", "input_tokens_total": 100_000_001},
                                {"max_usd": "1", "input_tokens_total": 10, "rate": 0}])
def test_invalid_budget_cannot_be_initialized(tmp_path, raw):
    with pytest.raises(session.SessionError):
        create(tmp_path, budget=raw)
    assert not (tmp_path / "budget.json").exists()


def test_cli_explicit_budget_requirement_rejects_missing_budget(tmp_path, monkeypatch, capsys):
    path = tmp_path / "legacy.json"
    session.create(path, {"task": "Legacy", "lead_profile": "sol-lead", "worker_profile": None})
    for name, key in KEYS.items():
        monkeypatch.setenv(name, key)
    monkeypatch.setattr(api.httpx, "Client", lambda **kwargs: pytest.fail("Unexpected network"))
    assert session.cli(["run", "--state", str(path), "--require-budget"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "budget_required"
    assert session.show(path)["status"] == "prepared"


def mock_cli(monkeypatch, transport):
    client = api.httpx.Client

    def local_client(**kwargs):
        return client(**(kwargs | {"transport": transport}))

    monkeypatch.setattr(api.httpx, "Client", local_client)
    for name, key in KEYS.items():
        monkeypatch.setenv(name, key)


def test_cli_without_budget_completes_chain_and_preserves_usage(tmp_path, monkeypatch, capsys):
    path = tmp_path / "trial.json"
    session.create(path, {"task": "Проверить полезный результат на пробном балансе"})
    transport, sent = exchange([frame("deepseek-flash", calls=[delegate()]),
                                frame("gpt-6-luna"), frame("deepseek-flash")])
    mock_cli(monkeypatch, transport)
    monkeypatch.setattr(budget, "today", lambda: date(2027, 1, 1))
    assert session.cli(["run", "--state", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "dialogue_complete" and result["calls"] == 3
    assert result["budget_enforced"] is False and "budget" not in result
    assert len(sent) == len(generated(sent)) == 3
    assert result["usage"]["deepseek-lead"]["input_tokens"] == 24
    assert result["usage"]["luna-worker"]["cache_write_input_tokens"] == 0
    assert result["cost_usd"] is None and result["task_acceptance"] == "not_evaluated"
    raw = json.loads(path.read_text())
    assert "budget_catalog" not in raw and all(c["status"] == "received" for c in raw["calls"])
    assert all(key not in path.read_text() for key in KEYS.values())
    assert session.show(path) == result
    assert session.cli(["run", "--state", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == result and len(sent) == 3


@pytest.mark.parametrize("dollars,code,status", [("0.10", 3, "stopped"), ("2", 0, "dialogue_complete")])
def test_cli_keeps_configured_budget_without_strict_flag(tmp_path, monkeypatch, capsys, dollars, code, status):
    path = create(tmp_path, model="deepseek-lead", worker="luna-worker", dollars=dollars)
    transport, sent = exchange([frame("deepseek-flash")])
    mock_cli(monkeypatch, transport)
    assert session.cli(["run", "--state", str(path)]) == code
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == status and result["budget_enforced"] is True
    assert result["budget"]["max_usd"] == budget.display(budget.money(dollars))
    assert len(sent) == (1 if code == 3 else 2)
    if code == 3:
        assert result["stop_reason"] == "money_budget_exhausted" and not generated(sent)
    else:
        assert len(generated(sent)) == 1


@pytest.mark.parametrize("kind", ["call_limit", "provider_output_limit", "unknown_usage"])
def test_cli_without_budget_still_stops_at_technical_bounds(tmp_path, monkeypatch, capsys, kind):
    path = tmp_path / "bounded.json"
    spec = {"task": "Проверить остановку без денежного потолка", "limits": {"output_tokens_per_call": 2048}}
    if kind == "call_limit":
        spec["limits"]["max_calls"] = 1
        response = frame("deepseek-flash", calls=[delegate()])
    else:
        response = (frame("deepseek-flash", output=2049) if kind == "provider_output_limit"
                    else frame("deepseek-flash", usage=None))
    session.create(path, spec)
    transport, sent = exchange([response])
    mock_cli(monkeypatch, transport)
    assert session.cli(["run", "--state", str(path)]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["budget_enforced"] is False and result["stop_reason"] == kind
    assert result["calls"] == 1 and len(sent) == len(generated(sent)) == 1
    assert session.show(path)["status"] == "stopped"


def test_corrupt_budget_ledger_cannot_be_used(tmp_path):
    path = create(tmp_path)
    raw = json.loads(path.read_text())
    raw["budget_catalog"] = {}
    path.write_text(json.dumps(raw))
    with pytest.raises(session.SessionError, match="invalid_budget_state"):
        session.run(path, environ=KEYS)
