"""Read the quota and choose a mixed team through the normal board settings."""

import asyncio
import os

from aiohttp.test_utils import TestClient, TestServer
import pytest

import test_api_permission_browser as fixtures

import api_board_models as models
import api_gemini_quota as quota
from symphony import api_team

host, simulated_service = fixtures.host, fixtures.simulated_service
TEAM = "/api/v1/api-teams/api-team"
QUOTA = TEAM + "/settings/gemini-quota"


@pytest.fixture()
def flash_board(host, simulated_service, monkeypatch):
    board, _, credentials, app = fixtures.application(host, monkeypatch)
    original = api_team.invoke
    reads = []

    def status_line(**_):
        reads.append(True)
        return quota.normalize({"quota": {"gemini-5h": {"remaining_fraction": 0},
                                          "gemini-weekly": {"remaining_fraction": .9977}}})

    async def invoke(cfg, kind, identifier, action, **kwargs):
        if action == "gemini-quota":
            assert identifier == "" and kwargs["payload"] == {}
            teams = models.Teams(board / "api-settings.json", api_team.board_context(cfg, kind, identifier))
            return await asyncio.to_thread(teams.refresh_gemini_quota)
        return await original(cfg, kind, identifier, action, **kwargs)

    monkeypatch.setattr(quota, "read", status_line)
    monkeypatch.setattr(api_team, "invoke", invoke)
    return board, credentials, reads, app


@pytest.mark.asyncio
async def test_quota_is_cached_and_only_a_local_board_action_can_refresh_it(flash_board):
    _, credentials, reads, app = flash_board
    async with TestClient(TestServer(app)) as http:
        assert (await (await http.get(TEAM)).json())["gemini_quota"] is None
        assert (await http.get(QUOTA)).status == 405
        assert (await http.post(QUOTA, json={}, headers={"Origin": "https://foreign.invalid"})).status == 403
        assert (await http.post(QUOTA, json={"prompt": "unauthorized"})).status == 400
        assert not reads
        response = await http.post(QUOTA, json={})
        assert response.status == 200
        snapshot = (await response.json())["gemini_quota"]
        assert snapshot["buckets"]["five_hour"]["remaining_percent"] == 0
        assert (await (await http.get(TEAM)).json())["gemini_quota"] == snapshot
        assert len(reads) == 1 and not credentials


@pytest.mark.browser_e2e
@pytest.mark.skipif(os.environ.get("SYMPHONY_BROWSER_E2E") != "1", reason="requires local Chromium")
@pytest.mark.asyncio
async def test_flash_selection_quota_and_confirmation_in_browser(flash_board, tmp_path):
    _, credentials, reads, app = flash_board
    pw = pytest.importorskip("playwright.async_api")
    async with TestClient(TestServer(app)) as http, pw.async_playwright() as service:
        browser = await service.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1240, "height": 980})
            await page.goto(str(http.make_url("/#/settings")), wait_until="networkidle")
            quota_panel = page.get_by_role("region", name="Квота Gemini")
            await pw.expect(quota_panel).to_contain_text("н/д")
            await quota_panel.get_by_role("button", name="Обновить квоту Gemini").click()
            await pw.expect(quota_panel.get_by_text("0%", exact=True)).to_be_visible()
            await pw.expect(quota_panel.get_by_text("99,77%", exact=True)).to_be_visible()
            settings = page.get_by_role("region", name="Команда по умолчанию")
            await settings.get_by_label("Ведущий", exact=True).select_option("sol-lead")
            await settings.get_by_label("Исполнитель", exact=True).select_option("flash-worker")
            await settings.get_by_role("button", name="Сохранить команду доски").click()
            await pw.expect(settings.get_by_text("Сохранено для новых назначений.", exact=False)).to_be_visible()
            await quota_panel.screenshot(path=tmp_path / "gemini-quota.png")
            created = await (await http.post("/api/v1/issues", json={"title": "Проверка назначения Flash"})).json()
            await page.goto(str(http.make_url("/#/board")), wait_until="networkidle")
            await page.locator(".card", has_text=created["identifier"]).click()
            panel = page.get_by_role("region", name="API-команда")
            await panel.get_by_role("button", name="Посмотреть назначение").click()
            await pw.expect(panel).to_contain_text("gpt-6.1-sol · усилие high")
            await pw.expect(panel).to_contain_text("Подписка Antigravity")
            await pw.expect(panel).to_contain_text("Ведущий может повысить high до max")
            await panel.screenshot(path=tmp_path / "flash-card-confirmation.png")
            assert len(reads) == 1 and not credentials
        finally:
            await browser.close()
