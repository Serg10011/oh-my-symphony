"""One composed result, one verified version and a durable handoff."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace
from typing import cast

from aiohttp.test_utils import TestClient, TestServer
import pytest

from symphony import api_team
from symphony.orchestrator import Orchestrator
from symphony.server import build_app
from symphony.workflow import WorkflowState
from test_api_delivery import FakeGitHub, action, attest
from test_api_plan import basic_board as basic_board
from test_api_plan import planning_board as planning_board
from test_api_project import git
from test_api_subtasks import checks as checks
from test_api_subtasks import children, make_plan, memo, spend, tick
from test_api_workspace import tool
from tests.test_webapi import _StubOrchestrator

import api_board
import api_cloud_check as cloud
import api_final
import api_session as session

NATIVE_RUN = session.sandbox.run
PASSING_SCRIPT = """\
set -e
test -s src/memo.md
grep -qF '[Мануал](src/memo.md)' README.md
printf 'PIPELINE_TESTS passed=2 skipped=0 failed=0\\n'
"""


@pytest.fixture()
def final_runs(checks, monkeypatch):
    subtask_runner = session.sandbox.run
    verified = []

    def run(policy, command, **kw):
        if not command.startswith("SYMPHONY_WORKFLOW_DIR="):
            return subtask_runner(policy, command, **kw)
        verified.append(command)
        if sys.platform == "darwin" and os.environ.get("SYMPHONY_TEST_NATIVE_SANDBOX") == "1":
            return NATIVE_RUN(replace(policy, executable=session.sandbox.CODEX), command, **kw)
        args = shlex.split(command)
        script = Path(args[-1])
        assert args[-2] == "/bin/bash" and script.parent == policy.workspace.parent / "scripts"
        # Execute only the trusted fixture's test.sh when the Mac sandbox is unavailable.
        proc = subprocess.run([args[-2], script], cwd=policy.workspace, capture_output=True, text=True,
                              env={"PATH": os.defpath}, timeout=10)
        return {"exit_code": proc.returncode, "output": proc.stdout + proc.stderr, "stop_reason": None}

    monkeypatch.setattr(session.sandbox, "run", run)
    return verified


def finish_parts(board):
    make_plan(board)
    board.start_subtasks(board.prepare_subtasks()["confirmation_token"])
    first, second = children(board)
    spend(first, memo())
    tick(board)
    original = hashlib.sha256((board.repo / "README.md").read_bytes()).hexdigest()
    spend(second, tool("write_project_file", "link", path="README.md", text="[Мануал](src/memo.md)\n",
                      expected_sha256=original))
    pending = tick(board)
    assert board.card()[0]["state"] == "Verify"
    assert board.show()["final"]["status"] == "checking"
    return pending


@pytest.mark.parametrize("failure", ["crash", "busy"])
def test_saved_local_proof_recovers_handoff_and_checks_both_parts_once(planning_board, final_runs, monkeypatch, failure):
    board = planning_board
    (board.workflow.parent / "test.sh").write_text(PASSING_SCRIPT)
    assert finish_parts(board) == {"local_checks": [board.identifier]}
    before = git(board.repo, "show-ref")
    offer = board.show()["final"]
    saved = board.root / "final-local.json"
    with monkeypatch.context() as bindings:
        if failure == "crash":
            def interrupted(*args):
                assert saved.exists()
                raise SystemExit("stop after saving the result")
            bindings.setattr(api_final, "document", interrupted)
            with pytest.raises(SystemExit):
                board.check_local_final(lambda: False)
        else:
            lock = session._lock
            @contextmanager
            def busy(path):
                if path == board.path and final_runs:
                    raise session.SessionError("session_busy")
                with lock(path):
                    yield
            bindings.setattr(session, "_lock", busy)
            with pytest.raises(session.SessionError, match="session_busy"):
                board.check_local_final(lambda: False)
    assert json.loads(saved.read_text())["status"] == "passed"
    assert board.card()[0]["state"] == "Verify" and len(final_runs) == 1
    restarted = api_board.Board(board.settings, board.context)
    ready = restarted.check_local_final(lambda: False)
    assert ready["final"] == {**offer, "status": "ready"}
    assert ready["board_stages"] == ["Todo", "Plan", "Subtasks", "Verify", "Document", "Human Review"]
    assert "Сводка составной задачи" in ready["report_text"] and "2 прошли" in ready["report_text"]
    diff = restarted.review()
    assert diff["commit"] == offer["commit"] and [item["path"] for item in diff["files"]] == ["README.md", "src/memo.md"]
    assert git(board.root / "candidate.git", "rev-parse", diff["commit"] + "^").decode().strip() == board._base_commit()
    assert git(board.repo, "show-ref") == before and git(board.repo, "status", "--porcelain") == b""
    restarted.check_local_final(lambda: False)
    assert tick(restarted) == {} and len(final_runs) == 1


@pytest.mark.parametrize("failure", ["tests", "tracked-files", "moved-base"])
def test_wrong_or_failed_local_result_never_reaches_review(planning_board, final_runs, failure):
    board = planning_board
    script = {"tests": "exit 3\n", "tracked-files": PASSING_SCRIPT + "printf 'changed after check' >> README.md\n",
              "moved-base": PASSING_SCRIPT}[failure]
    (board.workflow.parent / "test.sh").write_text(script)
    finish_parts(board)
    if failure == "moved-base":
        git(board.repo, "commit", "--allow-empty", "-qm", "source moved")
        git(board.repo, "branch", "-f", "our-main", "HEAD")
    outcome = board.check_local_final(lambda: False)
    reason = {"tests": "api_final_tests_failed", "tracked-files": "api_final_test_copy_changed",
              "moved-base": "api_final_result_changed"}[failure]
    assert outcome["final"]["status"] == "failed" and outcome["final"]["reason"] == reason, outcome
    assert board.card()[0]["state"] == "needs_human" and "Human Review" not in outcome["board_stages"]
    assert len(final_runs) == (0 if failure == "moved-base" else 1)
    assert not list(board.root.glob("final-document-*.json"))


def test_composed_documentation_uses_targeted_check_and_skips_full_suite(planning_board, final_runs):
    board = planning_board
    settings = json.loads(board.settings.read_text())
    board.settings.write_text(json.dumps({**settings, "context_paths": ["src/main.py", "README.md"]}))
    with (board.workflow.parent / "project.toml").open("a") as stream:
        stream.write('\n[checks]\ndoc_paths = ["README.md", "src/memo.md"]\n')
    (board.workflow.parent / "test.sh").write_text("exit 19\n")
    finish_parts(board)
    ready = board.check_local_final(lambda: False)
    assert ready["final"]["status"] == "ready" and final_runs == []
    assert json.loads((board.root / "final-local.json").read_text())["tests"] == "targeted"


def test_cloud_board_reuses_normal_publication_offer_and_verified_run(planning_board, final_runs, monkeypatch, tmp_path):
    board = planning_board
    raw = json.loads(board.settings.read_text())
    board.settings.write_text(json.dumps({**raw, "cloud_check": cloud.PROFILE}))
    config = board.workflow.parent / "project.toml"
    with config.open("a") as stream:
        stream.write(f'\n[delivery]\nrepo = "{cloud.REPO}"\nbase = "our-main"\n')
    git(board.repo, "remote", "add", "origin", f"https://github.com/{cloud.REPO}.git")
    with monkeypatch.context() as startup:
        startup.setattr(cloud, "discover", lambda *a: {})
        assert finish_parts(board) == {}
    hub = FakeGitHub(tmp_path, board)
    assert hub.pushes == [] and not final_runs
    initial = board.show()["release"]
    assert initial["stage"] == "publish-check" and initial["files"] == ["README.md", "src/memo.md"]
    action(board, hub, "publish-check")
    attest(board, hub, monkeypatch)
    ready = board.show()
    assert ready["final"]["status"] == "ready" and ready["final"]["commit"] == initial["commit"]
    assert ready["board_stages"][-3:] == ["Verify", "Document", "Human Review"]
    assert len(hub.pushes) == 1 and hub.pr is None and not final_runs


@pytest.mark.asyncio
@pytest.mark.browser_e2e
@pytest.mark.skipif(os.environ.get("SYMPHONY_BROWSER_E2E") != "1", reason="needs Chromium")
async def test_browser_shows_parent_in_review_with_combined_diff(planning_board, final_runs, monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    board = planning_board
    (board.workflow.parent / "test.sh").write_text(PASSING_SCRIPT)
    finish_parts(board)
    workflow = WorkflowState(board.workflow)
    cfg, error = workflow.reload()
    assert cfg and not error
    app = build_app(cast(Orchestrator, _StubOrchestrator(workflow)))
    refreshes = []
    service = SimpleNamespace(_next_cloud_check=0, _cloud_check_task=None,
                              _spawn_supervised=asyncio.create_task, request_refresh=lambda: refreshes.append(True))

    async def invoke(cfg, kind, identifier, operation, **kw):
        if operation == "sync-checks":
            return tick(board)
        selected = api_board.Board(board.settings, api_team.board_context(cfg, kind, identifier))
        if operation == "check-local-final":
            return selected.check_local_final(lambda: False)
        if operation == "review":
            return selected.review(**kw["payload"])
        return selected.show() if operation == "show" else {}

    monkeypatch.setattr(api_team, "invoke", invoke)
    api_team.schedule_cloud_checks(service, cfg)
    await service._cloud_check_task
    await service._api_final_check_task
    assert refreshes == [True] and len(final_runs) == 1
    async with TestClient(TestServer(app)) as client, playwright.async_playwright() as driver:
        browser = await driver.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 1000})
            await page.goto(str(client.make_url("/#/board")))
            lane = page.locator('[data-board-scroll="terminal:Human Review"]')
            await lane.locator(".card", has_text="Исправь функцию").click()
            panel = page.get_by_role("region", name="API-команда")
            await playwright.expect(panel).to_contain_text("Общий результат проверен и ждёт твоей приёмки")
            await panel.get_by_role("button", name="Посмотреть изменения файлов").click()
            await playwright.expect(panel).to_contain_text("src/memo.md — добавлен")
            await playwright.expect(panel).to_contain_text("README.md — изменён")
        finally:
            await browser.close()
