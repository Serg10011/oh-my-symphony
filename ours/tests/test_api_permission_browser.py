"""Actual board HTTP and browser controls with a fake credential probe, no dispatcher."""
import asyncio
import os

from aiohttp.test_utils import TestClient, TestServer
import pytest

import test_project_connect as fixtures
from test_api_session import KEYS

import api_board_permission as permission_api
import project_connect as connect
from symphony import api_team
from symphony.server import build_app
from symphony.workflow import WorkflowState
from tests.test_webapi import _StubOrchestrator

host, simulated_service = fixtures.host, fixtures.simulated_service


def application(host, monkeypatch):
    root, raw = host
    connect.connect(raw, root, fixtures.authorized(root, raw))
    board = root / raw["project"]
    state = WorkflowState(board / "WORKFLOW.md")
    assert state.reload()[0]
    original = api_team.invoke
    probes = []
    async def invoke(cfg, kind, identifier, action, **kwargs):
        if action == "permission-authorize":
            permission = permission_api.Permission(board / "api-settings.json", api_team.board_context(cfg, kind, identifier))
            def credentials(spec):
                probes.append(spec)
                return KEYS
            return await asyncio.to_thread(permission.authorize, kwargs["payload"]["confirmation_token"], credentials)
        assert action != "run", "The isolated board has no paid dispatcher"
        return await original(cfg, kind, identifier, action, **kwargs)
    monkeypatch.setattr(api_team, "invoke", invoke)
    return board, state, probes, build_app(_StubOrchestrator(state))


@pytest.mark.asyncio
async def test_permission_routes_reject_foreign_browser_origin_and_extra_fields(host, simulated_service, monkeypatch):
    board, state, probes, app = application(host, monkeypatch)
    route = "/api/v1/api-teams/api-team/permission"
    async with TestClient(TestServer(app)) as http:
        for action in ("prepare", "authorize", "revoke"):
            assert (await http.post(route + "/" + action, json={}, headers={"Origin":"https://foreign.invalid"})).status == 403
            assert (await http.get(route + "/" + action)).status == 405
        assert (await http.post(route + "/authorize", json={"confirmation_token":"bad", "paths":["."]})).status == 400
        for action in ("run", "resume", "shell"):
            assert (await http.post(route + "/" + action, json={})).status == 404
        assert (await (await http.get(route)).json())["status"] == "not_configured"
    assert not probes and not (board / ".ours-state/api-permissions/api-team/permission.json").exists()


@pytest.mark.browser_e2e
@pytest.mark.skipif(os.environ.get("SYMPHONY_BROWSER_E2E") != "1", reason="requires local Chromium")
@pytest.mark.asyncio
async def test_owner_approves_once_architect_submits_and_owner_stops_in_browser(host, simulated_service, monkeypatch, tmp_path):
    pw = pytest.importorskip("playwright.async_api")
    board, state, probes, app = application(host, monkeypatch)
    async with TestClient(TestServer(app)) as http, pw.async_playwright() as service:
        old = await (await http.post("/api/v1/issues", json={"title":"Ранее созданный черновик"})).json()
        browser = await service.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width":1280,"height":1000})
            await page.add_init_script("localStorage.setItem('symphony.lang','ru')")
            await page.goto(str(http.make_url("/#/settings")), wait_until="networkidle")
            panel = page.get_by_role("region", name="Разрешение на работу доски")
            await pw.expect(panel.get_by_role("status")).to_contain_text("отдельного подтверждения")
            await panel.get_by_role("button", name="Проверить условия доски").click()
            await pw.expect(panel).to_contain_text("Без отдельного денежного потолка")
            await pw.expect(panel).to_contain_text("DeepSeek Flash 4.1")
            assert not probes
            await panel.get_by_role("button", name="Разрешить работу новых карточек").click()
            await pw.expect(panel.get_by_role("status")).to_contain_text("разрешена")
            assert len(probes) == 1
            await panel.screenshot(path=tmp_path / "permission-active.png")
            old_view = await (await http.get(f"/api/v1/issues/{old['identifier']}/api-team")).json()
            assert "submission" not in old_view and old_view["status"] == "unassigned"
            created = await (await http.post("/api/v1/issues", json={"title":"Обновить памятку проекта"})).json()
            identifier = created["identifier"]
            assert not api_team.confirmed_in_queue(state.current(), identifier)
            await page.goto(str(http.make_url("/#/board")), wait_until="networkidle")
            await page.locator(".card", has_text=identifier).click()
            card = page.get_by_role("region", name="API-команда")
            await pw.expect(card.get_by_role("button",name="Передать в работу")).to_be_visible()
            await card.get_by_role("button",name="Передать в работу").click()
            await pw.expect(card.get_by_text("Подтверждено, ожидает свободного места", exact=True)).to_be_visible()
            assert api_team.confirmed_in_queue(state.current(), identifier)
            await card.screenshot(path=tmp_path / "card-submitted.png")
            await page.goto(str(http.make_url("/#/settings")), wait_until="networkidle")
            panel = page.get_by_role("region", name="Разрешение на работу доски")
            await panel.get_by_role("button", name="Остановить API-команду доски").click()
            await pw.expect(panel.get_by_role("status")).to_contain_text("остановлена")
            await panel.get_by_text("История разрешений").click()
            await pw.expect(panel).to_contain_text("Разрешение отозвано")
            await panel.screenshot(path=tmp_path / "permission-revoked.png")
            assert len(probes) == 1
            assert not list((board / ".ours-state/api-team").glob("*/session.json"))
        finally:
            await browser.close()
