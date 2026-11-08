"""The real settings page edits the board without granting payment or dispatching."""

import os
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
import pytest

import test_api_permission_browser as fixtures
from test_api_project import git

SETTINGS = "/api/v1/api-teams/api-team/settings"
PERMISSION = "/api/v1/api-teams/api-team/permission"
host, simulated_service = fixtures.host, fixtures.simulated_service


@pytest.mark.asyncio
async def test_settings_actions_require_board_origin_and_exact_fields(host, simulated_service, monkeypatch):
    board, _, probes, app = fixtures.application(host, monkeypatch)
    async with TestClient(TestServer(app)) as http:
        choice = {"mode": "productivity", "paths": ["README.md"]}
        for action in ("preview", "save"):
            assert (await http.post(SETTINGS + "/" + action, json=choice,
                                    headers={"Origin": "https://foreign.invalid"})).status == 403
        assert (await http.post(SETTINGS + "/save", json=choice)).status == 400
        assert (await http.post(SETTINGS + "/preview", json={**choice, "authorize": True})).status == 400
        assert (await http.post(SETTINGS + "/authorize", json=choice)).status == 404
        status = await (await http.get(PERMISSION)).json()
        assert status["status"] == "not_configured" and not probes
    assert not (board / ".ours-state/work-settings").exists()


@pytest.mark.browser_e2e
@pytest.mark.skipif(os.environ.get("SYMPHONY_BROWSER_E2E") != "1", reason="requires local Chromium")
@pytest.mark.asyncio
async def test_owner_previews_and_saves_new_conditions_and_fresh_cards_inherit_them(
        host, simulated_service, monkeypatch, tmp_path):
    pw = pytest.importorskip("playwright.async_api")
    repo = Path(host[1]["repo"])
    (repo / "guide.md").write_text("A committed guide.\n")
    git(repo, "add", "guide.md")
    git(repo, "commit", "-qm", "test guide")
    board, _, probes, app = fixtures.application(host, monkeypatch)
    async with TestClient(TestServer(app)) as http, pw.async_playwright() as service:
        consent = await (await http.post(PERMISSION + "/prepare", json={})).json()
        response = await http.post(PERMISSION + "/authorize", json={"confirmation_token": consent["confirmation_token"]})
        assert (await response.json())["status"] == "active"
        old = await (await http.post("/api/v1/issues", json={"title": "Прежний черновик"})).json()
        original = (board / f"kanban/{old['identifier']}.md").read_bytes()
        browser = await service.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1240, "height": 980})
            await page.goto(str(http.make_url("/#/settings")), wait_until="networkidle")
            await pw.expect(page.get_by_text("API-команда · api-team", exact=True)).to_be_visible()
            editor = page.get_by_role("region", name="Режим и доступ к файлам")
            mode = editor.get_by_label("Режим работы", exact=True)
            paths = editor.get_by_label("Доступные файлы и папки", exact=True)
            await pw.expect(mode).to_have_value("economy")
            await pw.expect(paths).to_have_value("README.md")
            await mode.select_option("productivity")
            await paths.fill("guide.md\nREADME.md")
            await editor.get_by_role("button", name="Проверить изменения").click()
            await pw.expect(editor.get_by_text("Экономия → Продуктивность")).to_be_visible()
            await pw.expect(editor.get_by_text("high → max")).to_be_visible()
            await pw.expect(editor.get_by_text("guide.md", exact=True)).to_be_visible()
            await pw.expect(editor.get_by_text("Для платной передачи потребуется подтвердить новые условия.")).to_be_visible()
            await editor.screenshot(path=tmp_path / "settings-preview.png")
            await editor.get_by_role("button", name="Сохранить настройки").click()
            await pw.expect(editor.get_by_text("Настройки сохранены.", exact=False)).to_be_visible()
            await page.reload(wait_until="networkidle")
            await pw.expect(mode).to_have_value("productivity")
            await pw.expect(paths).to_have_value("README.md\nguide.md")
            fresh = await (await http.post("/api/v1/issues", json={"title": "Новый черновик"})).json()
            result = await http.post(f"/api/v1/issues/{fresh['identifier']}/api-team/prepare", json={})
            view = await result.json()
            assert result.status == 200 and view["mode"] == "productivity", view
            assert view["project"]["paths"] == ["README.md", "guide.md"]
            assert view["execution_permission"]["status"] == "changed" and "submission" not in view
            assert (board / f"kanban/{old['identifier']}.md").read_bytes() == original and len(probes) == 1
        finally:
            await browser.close()
