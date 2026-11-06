"""Operator publication and acceptance through the actual file board and its HTTP routes."""
import asyncio
import os

from aiohttp.test_utils import TestClient, TestServer
import pytest

import test_api_delivery as delivery_fixture
from symphony import api_team
from symphony.server import build_app
from symphony.workflow import WorkflowState
from tests.test_webapi import _StubOrchestrator

board, world = delivery_fixture.board, delivery_fixture.world
pytestmark = [pytest.mark.browser_e2e, pytest.mark.skipif(os.environ.get("SYMPHONY_BROWSER_E2E") != "1",
    reason="set SYMPHONY_BROWSER_E2E=1 to run browser E2E")]


@pytest.mark.asyncio
@pytest.mark.parametrize("moved", [False, True], ids=["same-base", "changed-base"])
async def test_publish_review_and_deliver_in_browser(world, monkeypatch, moved):
    playwright = pytest.importorskip("playwright.async_api")
    board, hub = world
    if moved:
        delivery_fixture.advance(board, hub)
    workflow = WorkflowState(board.workflow)
    assert workflow.reload()[0]
    original = api_team.invoke
    mutations = []
    async def invoke(cfg, kind, identifier, action, **kwargs):
        if action in {"publish-check", "accept"}:
            mutations.append(action)
            await asyncio.to_thread(delivery_fixture.release.act, board, action, kwargs["payload"]["offer"],
                                    publisher=hub.publisher)
            return board.show()
        # GET/show, model directory and review go through the installed
        # subprocess bridge. No real credentials are available to the test.
        return await original(cfg, kind, identifier, action, **kwargs)

    monkeypatch.setattr(api_team, "invoke", invoke)
    async with TestClient(TestServer(build_app(_StubOrchestrator(workflow)))) as client:
        for action in ("publish-check", "accept"):
            route = f"/api/v1/issues/{board.identifier}/api-team/{action}"
            assert (await client.get(route)).status == 405
            assert (await client.post(route, json={"offer": "0" * 64},
                                      headers={"Origin": "https://foreign.invalid"})).status == 403
        assert not mutations
        async with playwright.async_playwright() as service:
            browser = await service.chromium.launch()
            try:
                page = await browser.new_page()
                await page.clock.install()
                await page.add_init_script("localStorage.setItem('symphony.lang', 'ru')")
                await page.goto(str(client.make_url("/#/board")), wait_until="networkidle")
                await page.locator(".card", has_text=board.identifier).click()
                panel = page.get_by_role("region", name="API-команда")
                transfer = panel.get_by_role("group", name="Передача результата")
                await playwright.expect(transfer).to_contain_text(delivery_fixture.cloud.REPO)
                await transfer.get_by_text("Файлы этой версии").click()
                await playwright.expect(transfer).to_contain_text("src/new.py")
                if moved:
                    await transfer.get_by_role("button", name="Изменения исходной версии").click()
                    await playwright.expect(panel).to_contain_text("Исходная версия " + board._load()["candidate"]["commit"][:12])
                await transfer.get_by_role("button", name="Опубликовать и проверить на GitHub").click()
                await playwright.expect(transfer).to_be_hidden()
                await asyncio.to_thread(delivery_fixture.attest, board, hub, monkeypatch)
                await page.clock.fast_forward(25000)
                await transfer.get_by_role("button", name="Принять версию и открыть PR").click()
                link = transfer.get_by_role("link", name="Открыть PR")
                await playwright.expect(link).to_have_attribute("href", "https://github.com/Serg10011/oh-my-symphony/pull/17")
                assert mutations == ["publish-check", "accept"] and len(hub.pushes) == 2
                assert board.show()["delivered"] and hub.created == 1
            finally:
                await browser.close()
