"""Local per-request spend report without provider access, keys or hidden zeros."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ours/hooks"))
import api_budget as budget  # noqa: E402
import api_executor as api  # noqa: E402
import api_session as session  # noqa: E402
import test_api_budget as guarded  # noqa: E402
from test_api_session import KEYS, delegate, exchange, frame, state_file  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    for name in [*KEYS, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]:
        monkeypatch.delenv(name, raising=False)


def test_report_breaks_spend_down_by_request_and_model_without_double_counting(tmp_path):
    path = state_file(tmp_path)
    transport, _ = exchange([frame(None, [delegate()]), frame("Итог", model="gpt-6-luna"), frame("Готово")])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    report = session.report(path)
    assert (report["version"], report["kind"], report["calls"]) == (1, "api_session_spend", 3)
    assert [(r["index"], r["profile"], r["model"], r["part_id"]) for r in report["requests"]] == [
        (1, "deepseek-lead", "deepseek-flash", None),
        (2, "luna-worker", "gpt-6-luna", "part_1"),
        (3, "deepseek-lead", "deepseek-flash", None)]
    lead = report["by_model"]["deepseek-flash"]
    assert (lead["calls"], lead["input_tokens"], lead["output_tokens"]) == (2, 24, 16)
    assert (lead["cached_input_tokens"], lead["reasoning_tokens"], lead["total_tokens"]) == (20, 12, 40)
    assert lead["input_includes_cached"] is True and lead["output_includes_reasoning"] is True
    assert lead["rates_per_million_tokens"]["input"] == "0.30" and lead["priced"] is True
    luna = report["by_model"]["gpt-6-luna"]
    assert (luna["calls"], luna["total_tokens"], luna["regional_factor"]) == (1, 20, "1.1")
    totals = report["totals"]
    assert (totals["input_tokens"], totals["output_tokens"], totals["total_tokens"]) == (36, 24, 60)
    assert (totals["cached_input_tokens"], totals["reasoning_tokens"]) == (30, 18)
    assert totals["total_tokens"] == totals["input_tokens"] + totals["output_tokens"]
    assert totals["unknown_usage_calls"] == 0
    amounts = [budget.money(record["estimated_usd"]) for record in report["requests"]]
    assert all(amount > 0 for amount in amounts)
    assert sum(amounts[0::2]) == budget.money(lead["estimated_usd"])
    assert amounts[1] == budget.money(luna["estimated_usd"])
    assert sum(amounts) == budget.money(report["cost"]["estimated_total_usd"])
    assert report["cost"]["estimate_complete"] is True
    assert report["cost"]["requests_without_estimate"] == 0
    assert report["cost"]["provider_billed_total_usd"] is None
    assert report["cost"]["provider_invoice_verified"] is False
    assert report["cost"]["tariff"]["source"] == "builtin_catalog"
    assert report["cost"]["tariff"]["version"] == budget.CATALOG["version"]
    assert report["budget_enforced"] is False and "budget" not in report
    assert all(record["provider_billed_usd"] is None for record in report["requests"])
    assert report["cost"]["cache_write_unknown_calls"] == 3
    assert "unknown_cache_writes_priced_at_catalog_write_rate" in report["cost"]["assumptions"]


def test_unknown_usage_stays_null_in_requests_totals_and_cost(tmp_path):
    path = state_file(tmp_path)
    transport, _ = exchange([frame(usage=None)])
    assert session.run(path, environ=KEYS, transport=transport)["stop_reason"] == "unknown_usage"
    report = session.report(path)
    usage = report["requests"][0]["usage"]
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (None, None, None)
    assert usage["usage_known"] is False
    assert report["requests"][0]["estimated_usd"] is None
    assert report["requests"][0]["estimate_status"] == "unknown_usage"
    assert (report["totals"]["input_tokens"], report["totals"]["total_tokens"]) == (None, None)
    assert report["totals"]["unknown_usage_calls"] == 1
    assert report["cost"]["estimated_total_usd"] is None
    assert report["cost"]["estimate_complete"] is False
    assert report["cost"]["requests_without_estimate"] == 1
    assert report["cost"]["provider_billed_total_usd"] is None


def test_request_rejected_locally_is_a_confirmed_zero_not_unknown(tmp_path, monkeypatch):
    path = state_file(tmp_path)

    def reject(*args, **kwargs):
        raise api.ExecutorError("local_stop_before_send", charge_possible=False)

    monkeypatch.setattr(api, "execute", reject)
    assert session.run(path, environ=KEYS)["stop_reason"] == "api_error"
    report = session.report(path)
    usage = report["requests"][0]["usage"]
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (0, 0, 0)
    assert usage["usage_known"] is True
    assert report["requests"][0]["estimated_usd"] == "0.000000"
    assert report["cost"]["estimated_total_usd"] == "0.000000"


def test_model_without_a_tariff_row_has_no_estimate_instead_of_zero(tmp_path, monkeypatch):
    monkeypatch.setitem(api.MODEL_OUTPUT_LIMITS, "unlisted-model", 4096)
    monkeypatch.setitem(api.PROFILES, "deepseek-lead", replace(api.PROFILES["deepseek-lead"], model="unlisted-model"))
    path = state_file(tmp_path)
    transport, _ = exchange([frame(model="unlisted-model")])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    report = session.report(path)
    assert report["requests"][0]["model"] == "unlisted-model"
    assert report["requests"][0]["estimated_usd"] is None
    assert report["requests"][0]["estimate_status"] == "unpriced_model"
    assert report["by_model"]["unlisted-model"]["priced"] is False
    assert report["by_model"]["unlisted-model"]["estimated_usd"] is None
    assert report["cost"]["unpriced_models"] == ["unlisted-model"]
    assert report["cost"]["estimated_total_usd"] is None


def test_report_keeps_pinned_tariff_and_reserved_cost_apart_from_the_invoice(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "today", lambda: date(2026, 11, 3))
    path = guarded.create(tmp_path, model="sol-lead", worker=None)
    transport, _ = guarded.exchange([guarded.frame()])
    assert session.run(path, environ=guarded.KEYS, transport=transport)["status"] == "dialogue_complete"
    report = session.report(path)
    shown = session.show(path)
    assert report["budget_enforced"] is True
    assert report["cost"]["tariff"] == {"version": budget.CATALOG["version"], "verified_on": "2026-11-03",
                                       "valid_until": "2026-11-10", "source": "session_snapshot"}
    assert report["budget"]["enforced"] is True and report["budget"]["preflight_requests"] == 1
    assert report["budget"]["input_tokens_committed"] == 12
    assert report["budget"]["generation_cost_upper_usd"] == shown["budget"]["generation_cost_upper_usd"]
    assert report["budget"]["tariff_version"] == budget.CATALOG["version"]
    assert report["budget"]["scope"] == "generation_under_pinned_tariff; provider_invoice_not_verified"
    assert report["cost"]["cache_write_unknown_calls"] == 0
    reservation = report["requests"][0]["budget_reservation"]
    assert reservation["basis"] == "provider_count" and reservation["settled_input_tokens"] == 12
    assert budget.money(report["budget"]["reserved_cost_usd"]) >= budget.money(report["cost"]["estimated_total_usd"])
    assert report["cost"]["provider_billed_total_usd"] is None


def test_old_session_without_output_limits_or_new_counters_stays_readable(tmp_path):
    path = state_file(tmp_path)
    transport, _ = exchange([frame(None, [delegate()]), frame("Итог", model="gpt-6-luna"), frame("Готово")])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    raw = json.loads(path.read_text())
    raw.pop("output_limits")
    for call in raw["calls"]:
        for key in ("cache_write_input_tokens", "reasoning_tokens", "usage_known"):
            call["response"]["usage"].pop(key)
        call.pop("part_id")
    path.write_text(json.dumps(raw))
    report = session.report(path)
    assert session.show(path)["usage"]["deepseek-lead"]["reasoning_tokens"] is None
    assert [record["part_id"] for record in report["requests"]] == [None, None, None]
    assert report["requests"][0]["usage"]["usage_known"] is True
    assert report["requests"][0]["usage"]["cache_write_input_tokens"] is None
    assert report["requests"][0]["usage"]["reasoning_tokens"] is None
    assert report["totals"]["input_tokens"] == 36 and report["totals"]["total_tokens"] == 60
    assert report["totals"]["reasoning_tokens"] is None
    assert report["totals"]["cache_write_input_tokens"] is None
    assert budget.money(report["cost"]["estimated_total_usd"]) > 0


def test_report_is_read_only_and_never_needs_keys_or_network(tmp_path, monkeypatch):
    path = state_file(tmp_path)
    transport, _ = exchange([frame(None, [delegate()]), frame("Итог", model="gpt-6-luna"), frame("Готово")])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    before = path.read_bytes()
    shown = session.show(path)
    report = session.report(path)
    monkeypatch.setattr(api.httpx, "Client", lambda **kwargs: pytest.fail("Unexpected network client"))
    assert session.report(path) == report and path.read_bytes() == before
    assert session.show(path) == shown
    raw = json.loads(path.read_text())
    assert raw["lead_history"] and raw["parts"]["part_1"]["attempts"] == 1
    assert all(key not in json.dumps(report) for key in KEYS.values())


def test_cli_report_is_local_for_prepared_and_stopped_states(tmp_path, capsys):
    path = tmp_path / "cli.json"
    env = {key: value for key, value in os.environ.items() if key not in KEYS}
    command = [sys.executable, str(ROOT / "ours/bin/api-session")]
    created = subprocess.run([*command, "init", "--state", str(path)], cwd=tmp_path, env=env,
                             input=json.dumps({"task": "Отчёт без сети и ключей"}), text=True,
                             capture_output=True, timeout=10)
    assert created.returncode == 0 and not created.stderr
    reported = subprocess.run([*command, "report", "--state", str(path)], cwd=tmp_path, env=env,
                              text=True, capture_output=True, timeout=10)
    assert reported.returncode == 0 and not reported.stderr
    output = json.loads(reported.stdout)
    assert output["kind"] == "api_session_spend" and output["calls"] == 0
    assert output["cost"]["estimated_total_usd"] == "0.000000"
    raw = json.loads(path.read_text())
    raw.update(status="stopped", stop_reason="call_limit")
    path.write_text(json.dumps(raw))
    assert session.cli(["report", "--state", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "stopped"
