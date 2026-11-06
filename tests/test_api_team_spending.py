"""The card summary and the detailed spend report must describe one amount.

The board returns the detailed report as text; the card only reads amounts from
it. These checks render that text with the board's own renderer for full,
partial and zero spending and require the card figures to coincide with it.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from tests.api_team import COSTS_MODULE, node_binary, render_report, spend_summary
    from tests.api_team import reports
except ModuleNotFoundError:  # pytest may put tests/ itself on sys.path
    from api_team import COSTS_MODULE, node_binary, render_report, spend_summary
    from api_team import reports

needs_node = pytest.mark.skipif(node_binary() is None,
                                reason="Node is needed to run api-team-costs.js")

CASES = [pytest.param(build, id=name) for name, build in
         [("full", reports.full_report), ("partial", reports.partial_report),
          ("zero", reports.zero_report)]]


def card(report: dict) -> tuple[dict, str]:
    try:
        text = render_report(report)
    except ModuleNotFoundError as exc:  # the board's renderer needs its own runtime deps
        pytest.skip(f"board spend renderer is unavailable: {exc.name}")
    return spend_summary(text, reports.session_for(report), reports.board_profiles()), text


def displayed_amount(value: str) -> str:
    """The amount part of a card value such as ``0.130003 USD (неполный)``."""
    match = re.match(r"^([0-9]+\.[0-9]{6}) USD", value)
    assert match is not None, value
    return match.group(1)


def expected_role_amount(report: dict, profile: str) -> int:
    """Report subtotals of the models only this profile used."""
    total = 0
    for model_name, row in report["by_model"].items():
        users = {item["profile"] for item in report["requests"] if item["model"] == model_name}
        if users == {profile}:
            total += reports.amount_micros(row["estimated_known_usd"]) or 0
    return total


@needs_node
@pytest.mark.parametrize("build", CASES)
def test_card_total_matches_the_detailed_report(build):
    report = build()
    summary, text = card(report)
    cost = report["cost"]
    complete = cost["estimate_complete"] is True
    reported = cost["estimated_total_usd"] if complete else cost["estimated_known_usd"]
    # The detailed report states the same amount the card shows.
    if complete:
        assert f"оценка за всю сессию: {cost['estimated_total_usd']} USD" in text
    else:
        assert "  оценка за всю сессию: полной суммы нет" in text
    assert f"известная часть: {reported} USD за" in text
    assert displayed_amount(summary["total"]["value"]) == reported
    assert summary["total"]["amount"] == reports.amount_micros(reported)
    assert summary["total"]["incomplete"] is (not complete)
    assert ("неполный" in summary["total"]["value"]) is (not complete)
    # "запросов без оценки" is the same number in both places.
    assert f"  запросов без оценки: {cost['requests_without_estimate']}" in text
    assert summary["missing"] == cost["requests_without_estimate"]


@needs_node
@pytest.mark.parametrize("build", CASES)
def test_lead_and_executor_parts_match_the_model_subtotals(build):
    report = build()
    summary, text = card(report)
    lead_profile = reports.session_for(report)["lead_profile"]
    worker_profile = reports.session_for(report)["worker_profile"]
    assert summary["lead"]["amount"] == expected_role_amount(report, lead_profile)
    assert summary["worker"]["amount"] == expected_role_amount(report, worker_profile)
    for role in ("lead", "worker"):
        value = summary[role]["value"]
        if value.startswith("н/д"):
            continue
        # Every amount the card shows is already printed in the report.
        assert displayed_amount(value) + " USD" in text
    # No part of the known total is lost between the participants.
    parts = (summary["lead"]["amount"] or 0) + (summary["worker"]["amount"] or 0) \
        + (summary["unallocated"]["amount"] or 0)
    assert parts == reports.known_micros(report)


@needs_node
@pytest.mark.parametrize("build", CASES)
def test_only_the_detailed_report_amounts_reach_the_card(build):
    report = build()
    before, _ = card(report)
    cost = report["cost"]
    # A different report must produce a different card amount, so the card has
    # no stored or hard-coded figure of its own.
    if cost["estimate_complete"]:
        cost["estimated_total_usd"] = "1.234500"
        cost["estimated_known_usd"] = "1.234500"
    else:
        cost["estimated_known_usd"] = "1.234500"
    after, text = card(report)
    assert "1.234500 USD" in text
    assert after["total"]["amount"] == reports.amount_micros("1.234500")
    assert before["total"]["amount"] != after["total"]["amount"]


@needs_node
def test_partial_report_keeps_the_known_total_and_its_incomplete_mark():
    report = reports.partial_report()
    summary, text = card(report)
    assert summary["total"]["value"] == "0.130003 USD (неполный)"
    assert "полной суммы нет" in text
    assert "1 из 4 запросов без оценки" in summary["note"]
    assert displayed_amount(summary["lead"]["value"]) + " USD" in text
    # The participant with the unestimated request is marked, the other is not.
    assert summary["worker"]["value"] == "0.030003 USD (неполный)"
    assert summary["lead"]["value"] == "0.100000 USD"


@needs_node
def test_saved_pilot_report_is_read_with_the_same_amounts():
    # A real saved report from the pilot run: 26 priced requests, two models.
    text = (ROOT / "docs" / "our" / "reports" / "data" / "S4-61-REPORT-TEXT.txt").read_text()
    session = {"calls": 26, "lead_profile": "deepseek-lead", "worker_profile": "luna-worker",
               "profiles": {"lead": {"name": "deepseek-lead", "model": "deepseek-flash"},
                            "worker": {"name": "luna-worker", "model": "gpt-6-luna"}},
               "usage": {"deepseek-lead": {"calls": 16}, "luna-worker": {"calls": 10}}}
    summary = spend_summary(text, session, reports.board_profiles())
    assert displayed_amount(summary["lead"]["value"]) + " USD" in text
    assert displayed_amount(summary["worker"]["value"]) + " USD" in text
    assert summary["lead"]["value"] == "0.100969 USD"
    assert summary["worker"]["value"] == "0.030003 USD"
    assert summary["total"]["value"] == "0.130972 USD"
    assert summary["total"]["incomplete"] is False
    assert summary["lead"]["amount"] + summary["worker"]["amount"] == summary["total"]["amount"]


def test_the_compared_module_is_the_one_the_board_serves():
    assert COSTS_MODULE.is_file()
    assert "SymphonyApiTeamCosts" in COSTS_MODULE.read_text()
