"""Real browser verification of the API drawer; all provider replies are fake."""

import asyncio
import os
from pathlib import Path

import pytest

from symphony import api_team
from tests.test_api_team import client, workflow, select_api  # noqa: F401
from tests.test_webapi import board_dir  # noqa: F401

pytestmark = [pytest.mark.browser_e2e, pytest.mark.usefixtures("client"), pytest.mark.skipif(os.environ.get("SYMPHONY_BROWSER_E2E") != "1",
    reason="set SYMPHONY_BROWSER_E2E=1 to run browser E2E")]


@pytest.mark.asyncio
async def test_api_drawer_requires_confirmation_and_keeps_report_scroll(request, monkeypatch, tmp_path):
    playwright = pytest.importorskip("playwright.async_api")
    api_client = request.getfixturevalue("client")
    await select_api(api_client)
    data = {"status": "unassigned"}
    calls = []
    async def invoke(cfg, kind, identifier, action, **kw):
        calls.append(action)
        if action == "prepare":
            data.update(status="prepared", task="Исправь функцию <img src=x onerror=window.injected=true>",
                profiles={"lead": {"model": "deepseek-flash", "effort": "high"},
                          "worker": {"model": "gpt-6-luna", "effort": "max"}},
                project={"paths": ["src", "tests"], "commit": "12345"},
                limits={"max_calls": 500, "output_tokens_per_call": 32768, "output_tokens_total": 2000000},
                process_timeout_seconds=7200, budget=None, execution={"read_roots": ["/runtime"]})
            return {**data, "confirmation_token": "test-capability-only"}
        if action == "confirm":
            assert kw["payload"] == {"confirmation_token": "test-capability-only"}
            data.update(status="running", session={"execution": {}}, report_text="Отчёт расходов\n" * 100)
        if action == "stop":
            data.update(status="stopped", reason="operator_stopped", session={"execution": {}, "result": "Результат команды"},
                        report_text=data["report_text"] + "Неизвестное списание: 1 запрос\n")
        if action == "review":
            return {"total": 1, "next_offset": None, "files": [{"path": "tests/test_new.py", "change": "added",
                "diff": "+assert 1 + 1 == 2\n+<script>window.injected=true</script>", "binary": False}]}
        return dict(data)
    monkeypatch.setattr(api_team, "invoke", invoke)
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 1000})
            await page.add_init_script("localStorage.setItem('symphony.lang', 'ru')")
            errors = []
            page.on("pageerror", lambda err: errors.append(str(err)))
            await page.goto(str(api_client.make_url("/#/board")), wait_until="networkidle")
            # Terminal cards are hidden by the board's normal Active filter.
            if await page.locator('.card', has_text="SEED-1").count() == 0:
                await page.get_by_role("button", name="Все", exact=True).click()
            await page.locator('.card', has_text="SEED-1").click()
            panel = page.get_by_role("region", name="API-команда")
            await panel.get_by_role("button", name="Посмотреть назначение").click()
            await panel.get_by_role("button", name="Подтвердить платный запуск").wait_for()
            assert calls.count("confirm") == 0 and calls.count("prepare") == 1
            assert "deepseek-flash" in await panel.inner_text()
            assert "без отдельного денежного потолка" in await panel.inner_text()
            await panel.get_by_role("button", name="Подтвердить платный запуск").click()
            await panel.get_by_text("Расходы и работа участников", exact=True).click()
            report = panel.locator('details').filter(has=page.locator('summary', has_text="Расходы и работа участников")).locator('pre')
            await report.evaluate("node => { node.scrollTop = 180; }")
            data["report_text"] += "Обновлённые расходы\n"
            await panel.get_by_role("button", name="Обновить", exact=True).click()
            await page.wait_for_function("document.querySelector('.api-team-panel').textContent.includes('Обновлённые расходы')")
            assert await report.evaluate("node => node.scrollTop") == 180
            assert await report.evaluate("node => node.parentElement.open")
            await panel.get_by_role("button", name="Остановить", exact=True).click()
            await panel.get_by_role("button", name="Посмотреть изменения файлов").click()
            await panel.get_by_text("tests/test_new.py — добавлен", exact=True).click()
            assert not await page.evaluate("Boolean(window.injected)")
            assert await panel.locator("script, img").count() == 0
            assert calls.count("confirm") == 1 and "run" not in calls
            assert "Неизвестное списание: 1 запрос" in await panel.inner_text()
            artifact = Path(os.environ.get("SYMPHONY_API_BROWSER_SCREENSHOT", str(tmp_path / "api-team.png")))
            await page.screenshot(path=str(artifact), full_page=True)
            assert not errors
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_card_shows_attempt_and_comparison_without_a_new_run(request, monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    api_client = request.getfixturevalue("client")
    await select_api(api_client)
    check = {"status": "ready", "commit": "c" * 40, "receipt": None}
    actions = []
    async def invoke(cfg, kind, identifier, action, **kw):
        actions.append(action)
        if action == "attach-check":
            assert kw["payload"] == {"run_number": "12345"}
            check["receipt"] = {"run": 12345, "attempt": 2, "status": "passed", "comparison": "failure"}
        return {"status": "result", "cloud_check": check}
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
            field = panel.get_by_role("textbox", name="Номер запуска GitHub Actions")
            await field.fill("12345")
            await panel.get_by_role("button", name="Сверить проверку").click()
            await panel.get_by_text("Контрольная версия завершилась с ошибкой.", exact=False).wait_for()
            link = panel.get_by_role("link", name="Открыть попытку на GitHub")
            assert (await link.get_attribute("href")).endswith("/12345/attempts/2")
            assert "Проверка коммита cccccccccccc прошла" in await panel.inner_text()
            check.clear()
            check["status"] = "stale"
            await panel.get_by_role("button", name="Обновить", exact=True).click()
            await panel.get_by_text("Результат требует новой сверки.", exact=False).wait_for()
            assert not await field.is_visible() and not await link.is_visible()
            assert set(actions) == {"show", "attach-check"} and actions.count("attach-check") == 1
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_closed_drawer_ignores_late_assignment_reply(request, monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    api_client = request.getfixturevalue("client")
    await select_api(api_client)
    started, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args, **kw):
        started.set()
        await release.wait()
        return {"status": "prepared", "confirmation_token": "must-not-appear"}
    monkeypatch.setattr(api_team, "invoke", delayed)
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.add_init_script("localStorage.setItem('symphony.lang', 'ru')")
            await page.goto(str(api_client.make_url("/#/board")), wait_until="networkidle")
            if await page.locator('.card', has_text="SEED-1").count() == 0:
                await page.get_by_role("button", name="Все", exact=True).click()
            await page.locator('.card', has_text="SEED-1").click()
            await asyncio.wait_for(started.wait(), 5)
            await page.locator('.drawer-header button').click()
            release.set()
            await page.wait_for_timeout(100)
            assert not await page.get_by_role("button", name="Подтвердить платный запуск").is_visible()
        finally:
            release.set()
            await browser.close()
