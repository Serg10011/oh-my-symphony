"""Flash assignments must not rewrite or restart historical Pro sessions."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
import json

import pytest

import test_api_budget as guarded

api, budget, session = guarded.api, guarded.budget, guarded.session


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(budget, "today", lambda: date(2026, 11, 3))
    for key in guarded.KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("with_budget", [False, True])
@pytest.mark.parametrize("completed", [False, True])
def test_pro_history_survives_flash_migration_without_reassignment(tmp_path, monkeypatch, with_budget, completed):
    path = tmp_path / "legacy-pro.json"
    spec = {"task": "Сохранить результат прежней модели", "limits": {"output_tokens_per_call": 32768}}
    if with_budget:
        spec["budget"] = {"max_usd": "2", "input_tokens_total": 3_000_000}
    legacy_catalog = deepcopy(budget.CATALOG)
    legacy_catalog["version"] = "2026-11-03-standard-peak-v1"
    del legacy_catalog["models"]["deepseek-flash"]
    with monkeypatch.context() as old_runtime:
        old_runtime.setitem(api.PROFILES, "deepseek-lead", replace(api.PROFILES["deepseek-lead"], model="deepseek-v4-pro"))
        old_runtime.setattr(budget, "CATALOG", legacy_catalog)
        session.create(path, spec)
        if completed:
            metadata = {"object": "list", "data": [{"id": "deepseek-v4-pro", "context_window": 1048576,
                                                     "max_output_tokens": 393216}]}
            transport, _ = guarded.exchange([guarded.frame("deepseek-v4-pro")], metadata=metadata)
            session.run(path, environ=guarded.KEYS, transport=transport)
        previous = session.show(path)
    original_bytes = path.read_bytes()

    monkeypatch.setattr(api.httpx, "Client", lambda **kwargs: pytest.fail("Unexpected network client"))
    assert session.show(path) == previous
    assert previous["profiles"]["lead"]["model"] == "deepseek-v4-pro"
    if completed:
        assert previous["status"] == "dialogue_complete"
        assert session.run(path, environ={}) == previous
    else:
        with pytest.raises(session.SessionError, match="profile_changed_since_init"):
            session.run(path, environ={})
    assert path.read_bytes() == original_bytes
    if with_budget:
        assert json.loads(original_bytes)["budget_catalog"] == legacy_catalog

    new_path = tmp_path / "new-flash.json"
    session.create(new_path, spec)
    current = json.loads(new_path.read_text())
    assert current["profiles"]["lead"]["model"] == "deepseek-flash"
    assert current["profiles"]["lead"]["effort"] == "high"
    assert current["profiles"]["worker"]["model"] == "gpt-6-luna"
    assert current["profiles"]["worker"]["effort"] == "max"
    if with_budget:
        assert current["budget_catalog"] == budget.CATALOG
        assert current["budget_catalog"]["version"] == "2026-11-03-standard-peak-v2"


@pytest.mark.parametrize("incoming,outgoing,cached,expected", [
    (1_000_000, 0, 0, "0.300000"),
    (1_000_000, 0, 1_000_000, "0.006000"),
    (0, 1_000_000, 0, "1.200000"),
])
def test_flash_reservations_use_verified_peak_rates(incoming, outgoing, cached, expected):
    assert budget.cost(budget.snapshot(), "deepseek-flash", incoming, outgoing, cached=cached) == expected


def test_pro_tariff_remains_available_for_historical_calculations():
    assert budget.cost(budget.snapshot(), "deepseek-v4-pro", 1000, 100, cached=800) == "0.000696"


def test_flash_does_not_fall_back_to_pro_metadata(tmp_path):
    path = guarded.create(tmp_path, model="deepseek-lead", worker="luna-worker")
    metadata = {"object": "list", "data": [{"id": "deepseek-v4-pro", "context_window": 1048576,
                                             "max_output_tokens": 393216}]}
    transport, sent = guarded.exchange([], metadata=metadata)
    result = session.run(path, environ=guarded.KEYS, transport=transport)
    assert result["stop_reason"] == "preflight_error" and result["calls"] == 0
    assert len(sent) == 1 and not guarded.generated(sent)
