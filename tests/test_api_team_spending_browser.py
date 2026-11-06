"""The actual drawer distinguishes no session, zero spend and unknown spend."""

import os

import pytest

from symphony import api_team
from tests.api_team import render_report, reports
from tests.test_api_team import client, workflow, select_api  # noqa: F401
from tests.test_webapi import board_dir  # noqa: F401

pytestmark = [pytest.mark.browser_e2e, pytest.mark.usefixtures("client"),
    pytest.mark.skipif(os.environ.get("SYMPHONY_BROWSER_E2E") != "1",
                      reason="set SYMPHONY_BROWSER_E2E=1 to run browser E2E")]


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [reports.full_report, reports.partial_report, reports.zero_report],
                         ids=["full", "partial", "zero"])
async def test_drawer_hides_spend_before_session_then_matches_report(request, monkeypatch, build):
    playwright = pytest.importorskip("playwright.async_api")
    api_client = request.getfixturevalue("client")
    await select_api(api_client)
    data = {"status": "prepared", "profiles": reports.board_profiles()}
    actions = []

    async def invoke(cfg, kind, identifier, action, **kwargs):
        actions.append(action)
        return dict(data)

    monkeypatch.setattr(api_team, "invoke", invoke)
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.add_init_script("localStorage.setItem('symphony.lang', 'ru')")
            await page.goto(str(api_client.make_url("/#/board")), wait_until="networkidle")
            if await page.locator(".card", has_text="SEED-1").count() == 0:
                await page.get_by_role("button", name="Все", exact=True).click()
            await page.locator(".card", has_text="SEED-1").click()
            panel = page.get_by_role("region", name="API-команда")
            spend = panel.get_by_role("group", name="Расход команды", include_hidden=True)
            await playwright.expect(panel).to_contain_text("Ожидает твоего подтверждения")
            await playwright.expect(spend).to_be_hidden()
            report = build()
            data.update(status="result", session=reports.session_for(report), report_text=render_report(report))
            await panel.get_by_role("button", name="Обновить", exact=True).click()
            await playwright.expect(spend).to_be_visible()
            cost = report["cost"]
            amount = cost["estimated_total_usd"] if cost["estimate_complete"] else cost["estimated_known_usd"]
            await playwright.expect(spend.locator("dd").nth(2)).to_contain_text(amount + " USD")
            assert await spend.locator("dd").nth(2).get_attribute("data-incomplete") == str(not cost["estimate_complete"]).lower()
            assert "run" not in actions and "confirm" not in actions
        finally:
            await browser.close()
