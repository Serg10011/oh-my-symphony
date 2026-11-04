"""Operator-readable Russian spend report next to the unchanged JSON mode, offline."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
import json

import pytest

from test_api_session_report import api, budget, session
from test_api_session import KEYS, delegate, exchange, frame, state_file
import test_api_budget as guarded


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    for name in [*KEYS, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]:
        monkeypatch.delenv(name, raising=False)


def test_text_report_shows_models_tokens_cache_and_the_cost_basis(tmp_path):
    path = state_file(tmp_path)
    transport, _ = exchange([frame(None, [delegate()]), frame("Итог", model="gpt-6-luna"), frame("Готово")])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    report = session.report(path)
    text = session.render_report(report)
    assert text.startswith("Отчёт о расходе сессии api-session")
    assert "  статус: диалог завершён; приёмка результата отдельно" in text and "  бюджет: не задан" in text
    assert "Итог по моделям" in text and "Токены и кеш за сессию" in text
    assert "Стоимость и полнота оценки" in text and "Основы оценки" in text
    assert "  deepseek-flash: запросов 2" in text and "  gpt-6-luna: запросов 1" in text
    assert "    профили: deepseek-lead" in text and "    профили: luna-worker" in text
    assert "    вход: 24 (в том числе из кеша: 20)" in text
    assert "    выход: 16 (в том числе рассуждения: 12)" in text
    assert "  вход: 36 (в том числе из кеша: 30)" in text
    assert "  выход: 24 (в том числе рассуждения: 18)" in text
    assert "  всего токенов: 60" in text
    assert report["cost"]["estimated_total_usd"] in text
    assert "  известная часть: " + report["cost"]["estimated_known_usd"] + " USD за 3 из 3 запросов" in text
    assert "  запросов без оценки: 0" in text
    assert "  счёт провайдера: не сверен" in text
    assert "  источник: встроенный каталог" in text
    assert "1. сессия без общего бюджета" in text
    assert all(key not in text for key in KEYS.values())


def test_text_report_never_turns_unknown_spend_into_zero(tmp_path):
    path = state_file(tmp_path)
    transport, _ = exchange([frame(usage=None)])
    assert session.run(path, environ=KEYS, transport=transport)["stop_reason"] == "unknown_usage"
    report = session.report(path)
    text = session.render_report(report)
    assert "  вход: н/д" in text and "    вход: н/д" in text
    assert "  оценка за всю сессию: полной суммы нет" in text
    assert "  запросов без оценки: 1 — их списание не считается нулевым" in text
    assert "без подтверждённого расхода: 1 запрос(ей)" in text
    assert "    стоимость: нет оценки — расход или тариф запроса непроверяемы" in text
    assert report["cost"]["estimated_total_usd"] is None and "0.000000 USD" in text


def test_text_report_names_a_model_without_a_tariff_row(tmp_path, monkeypatch):
    monkeypatch.setitem(api.MODEL_OUTPUT_LIMITS, "unlisted-model", 4096)
    monkeypatch.setitem(api.PROFILES, "deepseek-lead", replace(api.PROFILES["deepseek-lead"], model="unlisted-model"))
    path = state_file(tmp_path)
    transport, _ = exchange([frame(model="unlisted-model")])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    text = session.render_report(session.report(path))
    assert "  unlisted-model: запросов 1" in text
    assert "    стоимость: нет оценки — модели нет в каталоге тарифов" in text
    assert "  модели вне каталога: unlisted-model" in text


def test_text_report_shows_the_shared_budget_and_its_tariff(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "today", lambda: date(2026, 11, 3))
    path = guarded.create(tmp_path, model="sol-lead", worker=None)
    transport, _ = guarded.exchange([guarded.frame()])
    assert session.run(path, environ=guarded.KEYS, transport=transport,
                       require_budget=True)["status"] == "dialogue_complete"
    text = session.render_report(session.report(path))
    assert "  бюджет: включён · до 2.000000 USD и 3 000 000 входных токенов" in text
    assert "  сумма исходных резервов: " in text and "верхняя оценка расхода: " in text
    assert "  проверок до запроса: 1" in text
    assert "  нарушение тарифного контракта: нет" in text
    assert "  источник: снимок сессии" in text and "1. тариф зафиксирован в снимке сессии" in text


def test_text_report_is_read_only_offline_and_repeatable(tmp_path, monkeypatch):
    path = state_file(tmp_path)
    transport, _ = exchange([frame(None, [delegate()]), frame("Итог", model="gpt-6-luna"), frame("Готово")])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    before = path.read_bytes()
    monkeypatch.setattr(api.httpx, "Client", lambda **kwargs: pytest.fail("text report used the network"))
    first = session.render_report(session.report(path))
    assert session.render_report(session.report(path)) == first
    assert path.read_bytes() == before


def test_cli_text_mode_is_opt_in_and_json_stays_the_default(tmp_path, capsys):
    path = state_file(tmp_path)
    assert session.cli(["report", "--state", str(path)]) == 0
    default = capsys.readouterr().out
    assert json.loads(default)["kind"] == "api_session_spend"
    assert session.cli(["report", "--state", str(path), "--format", "text"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("Отчёт о расходе сессии api-session")
    assert "  запросов ещё не было" in text
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
