"""Exercise the issue API handlers without a live board or model calls."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from .test_attention_queue_api import (
    Application,
    Ctx,
    Issue,
    Orchestrator,
    Request,
    ServiceConfig,
    api_team,
    issue_routes,
)


class AgentPickerApiTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = ServiceConfig(workflow_path=Path("fixture/WORKFLOW.md"))
        self.cfg.tracker.kind = "file"
        self.cfg.tracker.active_states = ["Todo"]
        self.cfg.tracker.terminal_states = ["needs_human", "Human Review", "Done"]
        self.cfg.agent.kind = "claude"
        self.cfg.agent.new_issue_kind = ""
        self.cfg.agent.stage_kinds = {}
        self.allowed = {"claude", "codex"}
        self.cfg.board_agent_kinds = lambda: self.allowed
        self.ctx = Ctx(self.cfg)
        self.ctx.stats = lambda: SimpleNamespace(record_transition=Mock())
        self.tracker = Mock()
        self.tracker.fetch_issues_by_states.return_value = []
        self.tracker.create_validated.return_value = ("OMS-1", None)
        self.ctx.file_tracker = lambda: self.tracker
        self.orch = Orchestrator()
        self.orch.request_refresh = Mock()
        self.app = Application()
        issue_routes._register_issue_routes(self.app, self.ctx, self.orch)

    async def board(self):
        with patch.object(issue_routes, "FileBoardTracker", return_value=self.tracker):
            response = await self.app.router.routes["/api/v1/board"](Request())
        return response.json()

    async def test_new_card_uses_api_default_without_changing_permissions(self):
        self.cfg.agent.profiles["api-team"] = SimpleNamespace(
            format=api_team.FORMAT, command="/tools/api_board.py --settings /fixture/settings.json",
        )
        self.allowed.add("api-team")
        self.cfg.agent.picker_kinds = ("api-team",)
        self.cfg.agent.new_issue_kind = "api-team"
        meta = (await self.board())["board"]
        self.assertEqual(meta["agent_picker_kinds"], ["api-team"])
        self.assertEqual(meta["agent_kinds"], ["api-team", "claude", "codex"])
        self.assertEqual(meta["new_issue_kind"], "api-team")
        self.assertEqual(meta["default_agent_kind"], "claude")

        frozen = {"lead_profile": "demo-lead"}
        with (
            patch.object(issue_routes, "_read_json", AsyncMock(return_value={"title": "New card"})),
            patch.object(api_team, "defaults_for_new_card", AsyncMock(return_value=frozen)) as defaults,
        ):
            response = await self.app.router.routes["/api/v1/issues"](Request())
        self.assertEqual(response.status, 201)
        self.assertEqual(response.json(), {"identifier": "OMS-1", "state": "Todo"})
        defaults.assert_awaited_once_with(self.cfg, "api-team", None, None)
        self.assertEqual(self.tracker.create_validated.call_args.kwargs["agent_kind"], "api-team")
        self.assertEqual(self.tracker.create_validated.call_args.kwargs["api_defaults"], frozen)

    async def test_old_pin_stays_visible_and_start_uses_current_permissions(self):
        self.cfg.agent.kind = "codex"
        self.cfg.agent.picker_kinds = ("codex",)
        self.cfg.tracker.manual_start = True
        old = Issue(identifier="OMS-8", state="Todo", agent_kind="claude",
                    updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.tracker.fetch_issues_by_states.return_value = [old]
        self.tracker.fetch_issue_full_by_id.return_value = old
        board = await self.board()
        self.assertEqual(board["issues"][0]["agent_kind"], "claude")
        self.assertEqual(board["board"]["agent_picker_kinds"], ["codex"])
        self.assertIs(
            await issue_routes.startable_issue(self.tracker, self.cfg, old.identifier, old.updated_at.isoformat()), old,
        )
        self.allowed.remove("claude")
        with self.assertRaises(issue_routes.SymphonyError):
            await issue_routes.startable_issue(self.tracker, self.cfg, old.identifier, old.updated_at.isoformat())
        self.assertEqual(old.agent_kind, "claude")

    async def test_board_without_picker_publishes_the_ordinary_catalog(self):
        self.cfg.agent.stage_kinds = {"todo": "codex"}
        meta = (await self.board())["board"]
        self.assertNotIn("agent_picker_kinds", meta)
        self.assertEqual(meta["agent_kinds"], ["claude", "codex"])
        self.assertEqual(meta["new_issue_kind"], "")
        self.assertEqual(meta["stage_agent_kinds"], {"todo": "codex"})
