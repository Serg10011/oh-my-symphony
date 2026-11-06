"""Use the real board, runner and browser; no orchestrator dispatch or real model requests."""
import os
from dataclasses import replace
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace
from typing import cast

from aiohttp.test_utils import TestClient, TestServer
import pytest
import yaml

from symphony.orchestrator import Orchestrator
from symphony.api_team import confirmed_in_queue
from symphony.orchestrator.dispatch import _eligibility_contract_decision
from symphony.orchestrator.blocked_recovery import _auto_normalize_legacy_human_review_done, _legacy_human_review_is_done
from symphony.server import build_app
from symphony.workflow import WorkflowState
from tests.test_webapi import _StubOrchestrator
from test_api_board import board as board_fixture

board = board_fixture


@pytest.mark.browser_e2e
@pytest.mark.skipif(os.environ.get("SYMPHONY_BROWSER_E2E") != "1", reason="requires local Chromium")
@pytest.mark.asyncio
async def test_settings_card_override_and_historical_names_in_chromium(board, tmp_path):
    pw = pytest.importorskip("playwright.async_api")
    cmd = shlex.join([sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "hooks/api_board.py"),
                      "--settings", str(board.settings)])
    active = ["Todo", "In Progress", "Verify", "Document"]
    data = {"tracker": {"kind": "file", "board_root": "./kanban", "active_states": active,
                        "terminal_states": ["Human Review", "needs_human", "Done", "Archive"]},
            "workspace": {"root": str(tmp_path / "workspaces"), "code_repo": str(board.repo)},
            "agent": {"kind": "api-team", "feature_base_branch": "our-main",
                      "profiles": {"api-team": {"format": "api-team-v1", "command": cmd}}}}
    board.workflow.write_text("---\n" + yaml.safe_dump(data) + "---\nUI test.\n")
    card = board.board_root / "TEST-1.md"
    (card.parent / "TEST-2.md").write_text(card.read_text().replace("TEST-1", "TEST-2").replace("Исправь функцию", "Новая задача"))
    state = WorkflowState(board.workflow)
    config, err = state.reload()
    assert not err and config
    board.confirm(board.prepare()["confirmation_token"])
    board.stop()
    stub = _StubOrchestrator(state)
    async with TestClient(TestServer(build_app(cast(Orchestrator, stub)))) as client, pw.async_playwright() as p:
        endpoint = "/api/v1/api-teams/api-team"
        wrong = await client.post(endpoint, json={"team": {"lead_profile": "sol-lead","worker_profile": None}},
                                  headers={"Origin": "https://example.invalid"})
        assert wrong.status == 403
        resp = await client.post("/api/v1/issues", json={"title":"Обычный новый запуск"})
        created = await resp.json()
        assert resp.status == 201 and created["state"] == "Todo", created
        assert not confirmed_in_queue(config, created["identifier"])
        selection = await client.post(f"/api/v1/issues/{created['identifier']}/api-team/prepare", json={})
        assert selection.status == 200 and not confirmed_in_queue(config, created["identifier"])
        pending = board.tracker.fetch_issue_full_by_id(created["identifier"])
        assert _eligibility_contract_decision(cast(Orchestrator, stub), pending, config).code == "api_not_queued"
        handoff = replace(pending, state="Human Review", description="## Human Review\n### Decision Needed\nConfirm Done\n")
        assert config.agent.normalize_legacy_human_review and _legacy_human_review_is_done(handoff)
        writes = []
        normalizer = SimpleNamespace(_tracker_call_terminal_issues=lambda _: [handoff],
                                     _tracker_call_append_note=lambda *args: writes.append("note"),
                                     _tracker_call_update_state=lambda *args: writes.append("move"))
        assert await _auto_normalize_legacy_human_review_done(cast(Orchestrator, normalizer), config) == 0
        assert not writes
        # The stub owns no dispatcher. Confirming in this isolated fixture can only queue the card.
        entry = await selection.json()
        confirmed = await client.post(f"/api/v1/issues/{created['identifier']}/api-team/confirm",
                                      json={"confirmation_token": entry["confirmation_token"]})
        assert confirmed.status == 200 and confirmed_in_queue(config, created["identifier"])
        assert _eligibility_contract_decision(cast(Orchestrator, stub), pending, config) is None
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1260,"height": 920})
            await page.add_init_script("localStorage.setItem('symphony.lang','ru')")
            await page.goto(str(client.make_url("/#/settings")), wait_until="networkidle")
            settings = page.get_by_role("region", name="Команда по умолчанию")
            await settings.get_by_label("Ведущий", exact=True).select_option("sol-lead")
            await settings.get_by_label("Исполнитель", exact=True).select_option("")
            await settings.get_by_role("button", name="Сохранить команду доски").click()
            await pw.expect(settings.get_by_role("status")).to_contain_text("Сохранено")
            await settings.screenshot(path=tmp_path/"models-settings.png")
            await page.goto(str(client.make_url("/#/board")), wait_until="networkidle")
            if not await page.locator(".card", has_text="TEST-1").count():
                await page.get_by_role("button",name="Все",exact=True).click()
            old = page.locator(".card",has_text="TEST-1")
            new = page.locator(".card",has_text="TEST-2")
            await pw.expect(old.locator(".chip-agent")).to_have_text("api-team — DeepSeek Flash 4.1 + Luna")
            await pw.expect(new.locator(".chip-agent")).to_have_text("api-team — Sol 6.1 (по умолчанию)")
            await new.click()
            panel = page.get_by_role("region", name="API-команда")
            await panel.get_by_label("Другая команда для этой карточки").check()
            await panel.get_by_label("Ведущий",exact=True).select_option("deepseek-lead")
            await pw.expect(panel.get_by_label("Исполнитель",exact=True)).to_have_value("luna-worker")
            await panel.get_by_role("button",name="Посмотреть назначение").click()
            await pw.expect(panel.get_by_role("button",name="Подтвердить платный запуск")).to_be_visible()
            await panel.screenshot(path=tmp_path/"models-card.png")
            await panel.get_by_label("Ведущий",exact=True).select_option("sol-lead")
            await pw.expect(panel.get_by_role("button",name="Подтвердить платный запуск")).to_be_hidden()
            assert board.teams.default() == {"lead_profile": "sol-lead","worker_profile": None}
            assert not board.session_path.exists() and not (board.root.parent/"TEST-2/session.json").exists()
        finally:
            await browser.close()
