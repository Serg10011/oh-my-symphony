"""Operator acceptance cases for incomplete totals and historical model pricing."""

from datetime import date
import json

import httpx
import pytest

from test_api_session_report import api, budget, session
from test_api_session import KEYS, exchange, frame, state_file
from test_api_project_session import repository, state_file as project_state, tool


def test_known_subtotal_is_preserved_alongside_an_unknown_network_charge(tmp_path):
    root = repository(tmp_path)
    path = project_state(tmp_path, root)
    transport, _ = exchange([frame(None, [tool()]), httpx.ReadTimeout("private error")])
    assert session.run(path, environ=KEYS, transport=transport)["stop_reason"] == "api_error"
    report = session.report(path)
    cost = report["cost"]
    assert cost["estimated_total_usd"] is None and not cost["estimate_complete"]
    assert cost["estimated_known_usd"] == report["requests"][0]["estimated_usd"]
    assert budget.money(cost["estimated_known_usd"]) > 0
    assert cost["estimated_requests"] == cost["requests_without_estimate"] == 1
    assert report["requests"][1]["estimated_usd"] is None
    model = report["by_model"]["deepseek-flash"]
    assert model["estimated_usd"] is None and model["estimated_known_usd"] == cost["estimated_known_usd"]
    assert model["estimated_requests"] == 1
    assert cost["estimate_kind"] == "conservative_catalog_estimate"


def test_returned_model_is_not_silently_priced_as_the_requested_model(tmp_path):
    path = state_file(tmp_path)
    transport, _ = exchange([frame(model="different-unpriced-model")])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    report = session.report(path)
    call = report["requests"][0]
    assert call["requested_model"] == "deepseek-flash"
    assert call["model"] == "different-unpriced-model"
    assert call["estimated_usd"] is None and call["estimate_status"] == "unpriced_model"
    assert report["cost"]["estimated_requests"] == 0


@pytest.mark.parametrize("bad_row", [{}, {"input": "NaN"}, "not a tariff"])
def test_malformed_tariff_preserves_usage_without_crashing_or_guessing_cost(tmp_path, monkeypatch, bad_row):
    path = state_file(tmp_path)
    transport, _ = exchange([frame()])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    monkeypatch.setitem(budget.CATALOG["models"], "deepseek-flash", bad_row)
    report = session.report(path)
    assert report["totals"]["input_tokens"] == 12
    assert report["requests"][0]["estimated_usd"] is None
    assert report["requests"][0]["estimate_status"] == "invalid_tariff"
    assert report["cost"]["estimated_total_usd"] is None


def test_expired_tariff_and_retired_profile_stay_readable_offline(tmp_path, monkeypatch):
    path = state_file(tmp_path)
    transport, _ = exchange([frame()])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    old = json.loads(path.read_text())
    old["profiles"]["lead"]["model"] = "deepseek-v4-pro"
    old["calls"][0]["response"]["model"] = "deepseek-v4-pro"
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    monkeypatch.setattr(budget, "today", lambda: date(2030, 1, 1))
    monkeypatch.setattr(api.httpx, "Client", lambda **kwargs: pytest.fail("report accessed the network"))
    report = session.report(path)
    assert set(report["by_model"]) == {"deepseek-v4-pro"}
    assert budget.money(report["cost"]["estimated_total_usd"]) > 0
    assert report["cost"]["tariff"]["valid_until"] == "2026-11-10"
    assert path.read_bytes() == before
