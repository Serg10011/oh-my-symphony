"""The operator can distinguish an older viewable draft from a task handoff."""

import os

import pytest

from symphony import api_team
from tests.test_api_team import client, select_api, workflow  # noqa: F401
from tests.test_webapi import board_dir  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.browser_e2e
@pytest.mark.usefixtures("client")
@pytest.mark.skipif(os.environ.get("SYMPHONY_BROWSER_E2E") != "1", reason="opt-in browser")
async def test_card_shows_only_review_for_files_before_the_rejected_command(request, monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    api_client = request.getfixturevalue("client")
    await select_api(api_client)
    actions = []
    async def invoke(cfg, kind, identifier, action, **kw):
        actions.append(action)
        if action == "review":
            return {"source": "before_command", "total": 1, "next_offset": None,
                    "files": [{"path": "src/main.py", "change": "modified", "diff": "+prior = 1"}]}
        return {"status": "stopped", "reason": "workspace_command_outside_scope",
                "session": {"execution": {"review_source": "before_command"}}}
    monkeypatch.setattr(api_team, "invoke", invoke)
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.add_init_script("localStorage.setItem('symphony.lang', 'ru')")
            await page.goto(str(api_client.make_url("/#/board")), wait_until="networkidle")
            card = page.locator(".card", has_text="SEED-1")
            if not await card.count():
                await page.get_by_role("button", name="Все", exact=True).click()
            await card.click()
            panel = page.get_by_role("region", name="API-команда")
            await playwright.expect(panel).to_contain_text("вне разрешённых путей")
            await playwright.expect(panel).to_contain_text("сохранённые до этой команды")
            await panel.get_by_role("button", name="Посмотреть изменения файлов").click()
            await playwright.expect(panel).to_contain_text("задача не сдана")
            await panel.get_by_text("src/main.py — изменён", exact=True).click()
            await playwright.expect(panel.get_by_text("+prior = 1", exact=True)).to_be_visible()
            assert not await panel.get_by_role("button", name="Подтвердить платный запуск").is_visible()
            assert "confirm" not in actions and "run" not in actions
        finally:
            await browser.close()
