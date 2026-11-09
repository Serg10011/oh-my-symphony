"""Card shows the granted policy and actual effort without a paid request."""

import os

import pytest

from symphony import api_team
from tests.test_api_team import client, workflow, select_api  # noqa: F401
from tests.test_webapi import board_dir  # noqa: F401

pytestmark = [pytest.mark.browser_e2e, pytest.mark.usefixtures("client"),
              pytest.mark.skipif(os.environ.get("SYMPHONY_BROWSER_E2E") != "1", reason="requires local Chromium")]


@pytest.mark.asyncio
async def test_card_explains_escalation_and_updates_current_effort(request, monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    http = request.getfixturevalue("client")
    await select_api(http)
    data = {"status": "prepared", "lead_escalation": "high-to-max", "task": "Проверить функцию",
            "profiles": {"lead": {"model": "gpt-6.1-sol", "effort": "high"}, "worker": None}}
    calls = []

    async def invoke(cfg, kind, identifier, action, **kwargs):
        calls.append(action)
        return dict(data)

    monkeypatch.setattr(api_team, "invoke", invoke)
    async with pw.async_playwright() as service:
        browser = await service.chromium.launch()
        try:
            page = await browser.new_page()
            await page.add_init_script("localStorage.setItem('symphony.lang', 'ru')")
            await page.goto(str(http.make_url("/#/board")), wait_until="networkidle")
            if not await page.locator(".card", has_text="SEED-1").count():
                await page.get_by_role("button", name="Все", exact=True).click()
            await page.locator(".card", has_text="SEED-1").click()
            panel = page.get_by_role("region", name="API-команда")
            await pw.expect(panel.get_by_text("Ведущий может повысить high до max с записью причины.", exact=True)).to_be_visible()
            await pw.expect(panel.get_by_text("gpt-6.1-sol · усилие high", exact=True)).to_be_visible()
            data.update(status="running", session={"lead_effort": {"initial": "high", "current": "max"}})
            await panel.get_by_role("button", name="Обновить", exact=True).click()
            await pw.expect(panel.get_by_text("gpt-6.1-sol · усилие max", exact=True)).to_be_visible()
            assert set(calls) == {"show"}
        finally:
            await browser.close()
