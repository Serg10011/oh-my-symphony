"""Operator acceptance of incomplete charges, Russian labels and unchanged JSON."""

from copy import deepcopy
import json

import httpx
import pytest

from test_api_session import KEYS, exchange, frame, session, state_file
from test_api_project_session import repository, state_file as project_state, tool


def test_text_identifies_the_unknown_request_and_preserves_the_known_subtotal(tmp_path):
    path = project_state(tmp_path, repository(tmp_path))
    transport, _ = exchange([frame(None, [tool()]), httpx.ReadTimeout("private error")])
    assert session.run(path, environ=KEYS, transport=transport)["stop_reason"] == "api_error"
    data = session.report(path)
    before = deepcopy(data)
    text = session.render_report(data)
    known = data["cost"]["estimated_known_usd"]
    assert data == before
    assert "статус: остановлена" in text
    assert "причина остановки: ошибка при обращении к провайдеру" in text
    assert "известная часть: " + known + " USD за 1 из 2 запросов" in text
    assert "стоимость: неполная, известная часть " + known in text
    assert "№2 · deepseek-flash: расход токенов неизвестен" in text
    assert "оценка за всю сессию: полной суммы нет" in text
    assert "их списание не считается нулевым" in text
    assert "private error" not in text


@pytest.mark.parametrize("reason,expected", [
    ("call_limit", "достигнут аварийный предел числа запросов"),
    ("future_stop_code", "неизвестное значение (код: future_stop_code)"),
])
def test_text_explains_known_stops_and_preserves_unfamiliar_codes(tmp_path, reason, expected):
    path = state_file(tmp_path)
    state = json.loads(path.read_text())
    state.update(status="stopped", stop_reason=reason)
    text = session.render_report(session.spend_report(state))
    assert "причина остановки: " + expected in text
    assert "запросов ещё не было" in text


def test_cli_text_and_explicit_json_preserve_the_default_offline_data(tmp_path, monkeypatch, capsys):
    path = state_file(tmp_path)
    transport, _ = exchange([frame()])
    session.run(path, environ=KEYS, transport=transport)
    before = path.read_bytes()
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("report accessed the network"))
    assert session.cli(["report", "--state", str(path)]) == 0
    default = capsys.readouterr().out
    assert session.cli(["report", "--state", str(path), "--format", "json"]) == 0
    assert capsys.readouterr().out == default
    assert session.cli(["report", "--state", str(path), "--format", "text"]) == 0
    text = capsys.readouterr().out
    assert "приёмка результата отдельно" in text
    assert "рассуждения" in text and "reasoning" not in text
    assert "ниже только факт" not in text
    assert path.read_bytes() == before
    assert json.loads(default) == session.report(path)


def test_text_explains_long_context_rates_and_preserves_unpriced_usage(tmp_path):
    path = state_file(tmp_path, lead_profile="sol-lead", worker_profile=None)
    transport, _ = exchange([frame(model="gpt-6.1-sol")])
    session.run(path, environ=KEYS, transport=transport)
    text = session.render_report(session.report(path))
    assert "тариф в USD за 1 млн токенов" in text
    assert "при входе отдельного запроса свыше 272 000 токенов" in text
    assert "ставки входа, чтения и записи кеша ×2, выхода ×1,5" in text

    state = json.loads(path.read_text())
    state["calls"][0]["response"]["model"] = "unpriced-model"
    text = session.render_report(session.spend_report(state))
    assert "№1 · unpriced-model: для модели нет тарифа" in text
    assert "вход: 12 (в том числе из кеша: 10)" in text
    assert "запросов без оценки: 1" in text
