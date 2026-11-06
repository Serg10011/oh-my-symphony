"""The API card shows the team spend read from the detailed report, offline.

Full, partial and zero spending must stay readable without opening the long
report: the lead part, the executor part and the team total. Unknown amounts are
never presented as zero, and a known part is explicitly marked as an incomplete
total.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from tests.api_team import STATIC, node_binary, render_report, spend_summary
    from tests.api_team import reports
except ModuleNotFoundError:  # pytest may put tests/ itself on sys.path
    from api_team import STATIC, node_binary, render_report, spend_summary
    from api_team import reports

needs_node = pytest.mark.skipif(node_binary() is None,
                                reason="Node is needed to run api-team-costs.js")


def report_text(report: dict) -> str:
    try:
        return render_report(report)
    except ModuleNotFoundError as exc:  # the board's renderer needs its own runtime deps
        pytest.skip(f"board spend renderer is unavailable: {exc.name}")


def summary_of(report: dict, *, with_board_profiles: bool = True) -> dict:
    text = report_text(report)
    session = reports.session_for(report)
    return spend_summary(text, session,
                         reports.board_profiles() if with_board_profiles else None)


def test_the_cost_module_is_loaded_with_the_panel_and_has_no_markup_shortcuts():
    costs = STATIC / "api-team-costs.js"
    panel = (STATIC / "api-team.js").read_text()
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "style.css").read_text()
    assert costs.is_file() and costs.read_text().startswith("/* Spend summary")
    assert html.index('/static/api-team-costs.js') < html.index('/static/api-team.js')
    assert html.index('/static/api-team.js') < html.index('/static/app.js')
    # The panel reuses the module; it does not parse amounts a second time.
    assert "SymphonyApiTeamCosts" in panel
    assert "amountPattern" not in panel and "function micros" not in panel
    assert "innerHTML" not in panel and "innerHTML" not in costs.read_text()
    # An incomplete total stays visible, not just in the long report text.
    assert '.api-team-spend-incomplete' in css and '[data-incomplete="true"]' in css


@needs_node
def test_full_spending_shows_lead_executor_and_the_complete_total():
    report = reports.full_report()
    summary = summary_of(report)
    assert summary["lead"]["value"] == "0.100000 USD"
    assert summary["worker"]["value"] == "0.030003 USD"
    assert summary["total"]["value"] == "0.130003 USD"
    assert summary["total"]["incomplete"] is False and summary["complete"] is True
    assert summary["lead"]["incomplete"] is False and summary["worker"]["incomplete"] is False
    assert summary["missing"] == 0 and summary["calls"] == 3
    assert "неполный" not in summary["total"]["value"]
    assert "счёт провайдера не сверен" in summary["note"]


@needs_node
def test_partial_spending_shows_the_known_part_and_marks_the_total_incomplete():
    report = reports.partial_report()
    summary = summary_of(report)
    assert summary["total"]["amount"] == reports.amount_micros("0.130003")
    assert summary["total"]["value"] == "0.130003 USD (неполный)"
    assert summary["total"]["incomplete"] is True and summary["complete"] is False
    assert summary["lead"]["value"] == "0.100000 USD" and summary["lead"]["incomplete"] is False
    assert summary["worker"]["value"] == "0.030003 USD (неполный)"
    assert summary["worker"]["incomplete"] is True
    assert summary["missing"] == 1 and summary["calls"] == 4
    assert "итог неполный" in summary["note"]
    assert "1 из 4 запросов без оценки" in summary["note"]


@needs_node
def test_zero_spending_is_shown_as_zero_and_not_as_missing():
    report = reports.zero_report()
    summary = summary_of(report)
    assert summary["total"]["value"] == "0.000000 USD"
    assert summary["total"]["amount"] == 0 and summary["total"]["incomplete"] is False
    assert summary["lead"]["value"] == "0.000000 USD"
    assert summary["worker"]["value"] == "0.000000 USD"
    assert "н/д" not in summary["total"]["value"] + summary["lead"]["value"] + summary["worker"]["value"]
    assert "запросов ещё не было" in summary["note"]


@needs_node
def test_an_unknown_amount_is_never_shown_as_zero_spending():
    summary = summary_of(reports.unpriced_report())
    # The report knows no priced amount for the model, so the participant is н/д.
    assert summary["worker"]["value"] == "н/д" and summary["worker"]["incomplete"] is True
    assert summary["lead"]["value"] == "0.040000 USD" and summary["lead"]["incomplete"] is False
    assert summary["total"]["value"] == "0.040000 USD (неполный)"
    assert summary["total"]["incomplete"] is True
    assert "1 из 2 запросов без оценки" in summary["note"]


@needs_node
def test_a_shared_model_keeps_the_total_but_marks_the_split_as_incomplete():
    summary = summary_of(reports.shared_model_report())
    assert summary["total"]["value"] == "0.040000 USD" and summary["complete"] is True
    assert summary["breakdownComplete"] is False
    assert summary["lead"]["value"] == "н/д" and summary["worker"]["value"] == "н/д"
    assert summary["unallocated"]["amount"] == reports.amount_micros("0.040000")
    assert "разбивка по участникам неполная" in summary["note"]
    assert "счёт провайдера не сверен" in summary["note"]


@needs_node
def test_a_card_without_a_started_run_shows_a_zero_total_honestly():
    summary = spend_summary("", None, reports.board_profiles())
    assert summary["total"]["value"] == "0.000000 USD" and summary["total"]["incomplete"] is False
    assert summary["lead"]["value"] == "0.000000 USD"
    assert summary["worker"]["value"] == "0.000000 USD"
    assert "Запуск ещё не начинался" in summary["note"]


@needs_node
def test_an_unreadable_report_is_shown_as_unknown_not_hidden():
    session = {"calls": 3, "profiles": {}, "lead_profile": "deepseek-lead",
               "worker_profile": "luna-worker", "usage": {}}
    summary = spend_summary("Ответ без раздела стоимости\n", session, reports.board_profiles())
    assert summary is not None and summary["total"]["value"] == "н/д"
    assert summary["total"]["incomplete"] is True
    assert "не распознан" in summary["note"]
    assert summary["lead"]["value"] == "н/д" and summary["worker"]["value"] == "н/д"


@needs_node
def test_models_are_enough_to_split_the_spend_when_profile_names_are_absent():
    report = reports.full_report()
    summary = spend_summary(report_text(report), None, reports.board_profiles())
    assert summary["lead"]["value"] == "0.100000 USD"
    assert summary["worker"]["value"] == "0.030003 USD"
    assert summary["total"]["value"] == "0.130003 USD"


@needs_node
def test_conflicting_counters_do_not_produce_a_confident_zero():
    session = reports.session_for(reports.full_report())
    session["calls"] = 0  # the profile counters still remember two and one call
    summary = spend_summary("", session, reports.board_profiles())
    assert summary["total"]["value"] == "н/д" and summary["total"]["incomplete"] is True
    assert summary["lead"]["value"] == "н/д" and summary["worker"]["value"] == "н/д"
    assert "0.000000 USD" not in summary["total"]["value"] + summary["lead"]["value"]


@needs_node
def test_counters_that_disagree_with_the_report_mark_the_split_as_incomplete():
    report = reports.full_report()
    session = reports.session_for(report)
    session["usage"]["deepseek-lead"]["calls"] = 3  # the report prices two lead requests
    summary = spend_summary(report_text(report), session, reports.board_profiles())
    assert summary["total"]["value"] == "0.130003 USD" and summary["complete"] is True
    assert summary["breakdownComplete"] is False
    assert summary["lead"]["incomplete"] is True
    assert "разбивка по участникам неполная" in summary["note"]


@needs_node
def test_a_report_without_model_rows_keeps_the_known_total_in_the_breakdown():
    partial = reports.partial_report()
    text = report_text(partial)
    lines = text.split("\n")
    start = next(index for index, line in enumerate(lines) if line.strip() == "Итог по моделям")
    end = next(index for index, line in enumerate(lines)
               if index > start and line.strip() == "Токены и кеш за сессию")
    summary = spend_summary("\n".join(lines[:start + 1] + lines[end:]),
                            reports.session_for(partial), reports.board_profiles())
    assert summary["total"]["amount"] == reports.amount_micros("0.130003")
    assert summary["lead"]["value"] == "н/д" and summary["worker"]["value"] == "н/д"
    # Nothing of the known total disappears from the breakdown.
    assert summary["unallocated"]["amount"] == reports.amount_micros("0.130003")
    assert summary["breakdownComplete"] is False
    assert "Часть расхода не разделена между участниками" in summary["note"]


@needs_node
def test_every_branch_returns_the_same_summary_shape():
    keys = {"lead", "worker", "total", "note", "complete", "breakdownComplete",
            "breakdownIncomplete", "unallocated", "calls", "missing", "estimated"}
    branches = [summary_of(reports.full_report()), summary_of(reports.zero_report()),
                spend_summary("", None, reports.board_profiles()),
                spend_summary("Отчёт без стоимости\n", {"calls": 2}, reports.board_profiles())]
    for summary in branches:
        assert keys <= set(summary)
        assert {"value", "amount", "incomplete"} <= set(summary["total"])
        assert {"amount", "incomplete"} <= set(summary["unallocated"])
        assert summary["breakdownIncomplete"] is (not summary["breakdownComplete"])
