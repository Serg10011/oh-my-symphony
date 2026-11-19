"""Autonomous unittests for OMS-18 subtask 1: attention_queue on GET /api/v1/board.

Tests contract, backwards compatibility, state filtering, Russian localization,
agent-kind neutrality, order stability, deduplication, conflict resolution,
bounded reads, file immutability, corruption handling, permissions, recreate app lifecycle,
and path traversal guards.
"""

from __future__ import annotations

import asyncio
import builtins
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"


def load_isolated_modules():
    """Load symphony.api_team, webapi.payloads, webapi.issue_routes with local doubles.

    Only a private namespace is temporarily installed; real symphony/aiohttp
    modules and newly imported standard-library dependencies are never replaced.
    """
    namespace = "_oms_attention_queue_test"
    local_modules = {}
    saved = {}
    missing = object()

    def install(name, module):
        saved.setdefault(name, sys.modules.get(name, missing))
        sys.modules[name] = module


    try:
        sym_pkg = types.ModuleType("symphony")
        sym_pkg.__path__ = [str(SRC_DIR / "symphony")]
        local_modules["symphony"] = sym_pkg

        shell_mod = types.ModuleType("symphony._shell")
        shell_mod.process_identity = lambda pid: {}
        shell_mod.terminate_process_tree = lambda pid: None
        local_modules["symphony._shell"] = shell_mod

        err_mod = types.ModuleType("symphony.errors")
        class SymphonyError(Exception): pass
        err_mod.SymphonyError = SymphonyError
        local_modules["symphony.errors"] = err_mod

        log_mod = types.ModuleType("symphony.logging")
        log_mod.get_logger = lambda *a, **k: MagicMock()
        local_modules["symphony.logging"] = log_mod

        iss_mod = types.ModuleType("symphony.issue")
        class Issue:
            def __init__(self, **kw):
                self.identifier = ""
                self.title = ""
                self.state = ""
                self.priority = 1
                self.labels = ()
                self.skills = ()
                self.agent_kind = None
                self.last_agent_kind = None
                self.request = None
                self.blocked_by = ()
                self.created_at = None
                self.updated_at = None
                self.start_requested = False
                self.id = ""
                self.__dict__.update(kw)
        iss_mod.Issue = Issue
        iss_mod.IssueAttention = lambda **kw: None
        iss_mod.normalize_state = lambda s: (s or "").strip().casefold()
        iss_mod.registration_order_key = lambda i: getattr(i, "identifier", "")
        local_modules["symphony.issue"] = iss_mod

        wf_mod = types.ModuleType("symphony.workflow")
        class AgentProfileConfig:
            def __init__(self, **kw):
                self.command = ""
                self.format = "api-team-v1"
                self.turn_timeout_ms = 30000
                self.__dict__.update(kw)
        class ServiceConfig:
            def __init__(self, **kw):
                self.workflow_path = None
                self.code_repo = None
                self.poll_interval_ms = 1000
                self.tracker = types.SimpleNamespace(
                    kind="file",
                    board_root=None,
                    id_prefix="OMS",
                    active_states=["In Progress", "Needs Human", "Blocked", "Нужна помощь", "Заблокирован"],
                    terminal_states=["Done", "Closed"],
                    state_descriptions={},
                    manual_start=False,
                )
                self.agent = types.SimpleNamespace(
                    kind="default",
                    new_issue_kind="default",
                    max_concurrent_agents=2,
                    max_turns=20,
                    max_attempts=3,
                    scheduling_policy="fifo",
                    feature_base_branch="main",
                    auto_merge_target_branch="main",
                    auto_merge_on_done=False,
                    auto_merge_push_target="",
                    stage_contracts={},
                    stage_contracts_enabled=lambda s: False,
                    profiles={},
                    kind_for_state=lambda s, k: k or "default",
                )
                self.continuous_improvement = types.SimpleNamespace(
                    enabled=False,
                    interval_ms=60000,
                    max_turns=1,
                    agent_kind="ci",
                    ticket_prefix="CI",
                    max_tickets_per_run=1,
                    require_idle_board=True,
                    modes=(),
                    resolved_modes=lambda: (),
                    interval_hours_for=lambda m: 24,
                    max_improvement_tickets_per_run=1,
                )
                self.preview = types.SimpleNamespace(
                    enabled=False, cwd="", health_path="", url_path="", release_ticket="", acceptance=()
                )
                self.prompts = types.SimpleNamespace(stage_paths={})
                self.__dict__.update(kw)
            def board_agent_kinds(self):
                return list(self.agent.profiles.keys())
            def agent_profile(self, kind: str):
                return self.agent.profiles.get(kind)
        wf_mod.AgentProfileConfig = AgentProfileConfig
        wf_mod.ServiceConfig = ServiceConfig
        wf_mod.SUPPORTED_CI_MODES = ()
        local_modules["symphony.workflow"] = wf_mod

        wfm_mod = types.ModuleType("symphony.workflow.mutate")
        class WorkflowMutationError(Exception): pass
        wfm_mod.WorkflowMutationError = WorkflowMutationError
        local_modules["symphony.workflow.mutate"] = wfm_mod

        orch_mod = types.ModuleType("symphony.orchestrator")
        class Orchestrator:
            def __init__(self):
                self._attention = {}
                self._running = []
                self._retrying = []
            def issue_attention(self, issue):
                return self._attention.get(issue.identifier)
            def snapshot(self):
                return {"running": self._running, "retrying": self._retrying}
            def schedule_snapshot(self):
                return {"available": True, "entries": []}
        orch_mod.Orchestrator = Orchestrator
        local_modules["symphony.orchestrator"] = orch_mod

        run_reg = types.ModuleType("symphony.orchestrator.run_registry")
        run_reg.clamp_run_history_limit = lambda x: x
        local_modules["symphony.orchestrator.run_registry"] = run_reg

        sched_mod = types.ModuleType("symphony.orchestrator.scheduler")
        class RequestGroupKey:
            def __init__(self, kind="", value=""):
                self.kind = kind
                self.value = value
        sched_mod.RequestGroupKey = RequestGroupKey
        sched_mod.dependency_cycle_nodes = lambda *a: set()
        sched_mod.group_by_request = lambda *a: {}
        sched_mod.MAX_DEPENDENCY_EDGES = 1000
        sched_mod.MAX_DEPENDENCY_NODES = 1000
        local_modules["symphony.orchestrator.scheduler"] = sched_mod

        skills_mod = types.ModuleType("symphony.skills")
        skills_mod.normalize_skill_names = lambda s: list(s or [])
        local_modules["symphony.skills"] = skills_mod

        trk_pkg = types.ModuleType("symphony.trackers")
        trk_pkg.__path__ = []
        local_modules["symphony.trackers"] = trk_pkg

        trk_file = types.ModuleType("symphony.trackers.file")
        class FileBoardTracker:
            def __init__(self, tracker_cfg):
                self.tracker_cfg = tracker_cfg
            def fetch_issues_by_states(self, states):
                return []
            def fetch_issue_full_by_id(self, identifier):
                return None
        trk_file.FileBoardTracker = FileBoardTracker
        trk_file.parse_ticket_file = lambda *a: {}
        local_modules["symphony.trackers.file"] = trk_file

        trk_val = types.ModuleType("symphony.trackers.validate")
        trk_val.validate_ticket_dependencies = lambda *a: None
        local_modules["symphony.trackers.validate"] = trk_val

        aio_mod = types.ModuleType("aiohttp")
        aio_web = types.ModuleType("aiohttp.web")
        class HTTPNotFound(Exception): pass
        aio_web.HTTPNotFound = HTTPNotFound
        class StubResponse:
            def __init__(self, body: bytes, status: int = 200, headers: dict | None = None):
                self.body = body
                self.status = status
                self.headers = headers or {}
            def json(self):
                return json.loads(self.body.decode("utf-8"))
        def json_resp(d, status=200, headers=None):
            return StubResponse(body=json.dumps(d, ensure_ascii=False).encode("utf-8"), status=status, headers=headers)
        aio_web.json_response = json_resp
        class StubRouter:
            def __init__(self):
                self.routes = {}
            def add_get(self, path, handler):
                self.routes[path] = handler
            def add_post(self, path, handler):
                self.routes[path] = handler
            def add_patch(self, path, handler):
                self.routes[path] = handler
            def add_delete(self, path, handler):
                self.routes[path] = handler
        class Application:
            def __init__(self):
                self.router = StubRouter()
        class Request:
            def __init__(self, query=None, match_info=None):
                self.query = query or {}
                self.match_info = match_info or {}
        aio_web.Application = Application
        aio_web.Request = Request
        aio_mod.web = aio_web
        local_modules["aiohttp"] = aio_mod
        local_modules["aiohttp.web"] = aio_web

        web_pkg = types.ModuleType("symphony.webapi")
        web_pkg.__path__ = [str(SRC_DIR / "symphony" / "webapi")]
        local_modules["symphony.webapi"] = web_pkg

        ctx_mod = types.ModuleType("symphony.webapi.context")
        class _Ctx:
            def __init__(self, cfg):
                self._cfg = cfg
            def config(self):
                return self._cfg
            def workflow_dir(self):
                return Path(self._cfg.workflow_path).parent if self._cfg.workflow_path else Path.cwd()
        ctx_mod._Ctx = _Ctx
        local_modules["symphony.webapi.context"] = ctx_mod

        sec_mod = types.ModuleType("symphony.webapi.security")
        import re
        sec_mod._RUN_ID_RE = re.compile(r"^[a-f0-9]{32}$")
        sec_mod._json_error = lambda status, code, detail: json_resp({"error": code, "message": detail}, status=status)
        sec_mod._read_json = lambda req: {}
        sec_mod._request_is_loopback = lambda req: True
        sec_mod._wrap = lambda h: h
        local_modules["symphony.webapi.security"] = sec_mod

        val_mod = types.ModuleType("symphony.webapi.validation")
        val_mod._INLINE_ARTIFACT_TYPES = ()
        val_mod._artifact_json = lambda *a: {}
        val_mod._ascii_filename = lambda *a: ""
        val_mod._check_agent_kind = lambda *a: None
        val_mod._check_blocked_by = lambda *a: None
        val_mod._check_description = lambda *a: None
        val_mod._check_identifier = lambda *a: None
        val_mod._check_labels = lambda *a: None
        val_mod._check_level = lambda *a: None
        val_mod._check_priority = lambda *a: None
        val_mod._check_request = lambda *a: None
        val_mod._check_request_schedule_key = lambda *a: None
        val_mod._check_state = lambda *a: None
        val_mod._check_title = lambda *a: None
        val_mod._valid_states = lambda cfg: {
            state: state for state in [*cfg.tracker.active_states, *cfg.tracker.terminal_states]
        }
        local_modules["symphony.webapi.validation"] = val_mod

        for name, module in local_modules.items():
            if not name.startswith("symphony"):
                continue
            private_name = namespace + name[len("symphony"):]
            module.__name__ = private_name
            module.__package__ = (private_name if hasattr(module, "__path__")
                                  else private_name.rpartition(".")[0])
            install(private_name, module)
        for name in saved:
            parent, _, child = name.rpartition(".")
            if parent in sys.modules:
                setattr(sys.modules[parent], child, sys.modules[name])

        real_import = builtins.__import__
        def isolated_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "aiohttp" and level == 0:
                return aio_mod
            return real_import(name, globals, locals, fromlist, level)
        module_builtins = {**vars(builtins), "__import__": isolated_import}

        spec_api = importlib.util.spec_from_file_location(namespace + ".api_team", str(SRC_DIR / "symphony" / "api_team.py"))
        api_team_m = importlib.util.module_from_spec(spec_api)
        api_team_m.__builtins__ = module_builtins
        install(namespace + ".api_team", api_team_m)
        sys.modules[namespace].api_team = api_team_m
        spec_api.loader.exec_module(api_team_m)

        spec_pay = importlib.util.spec_from_file_location(namespace + ".webapi.payloads", str(SRC_DIR / "symphony" / "webapi" / "payloads.py"))
        payloads_m = importlib.util.module_from_spec(spec_pay)
        payloads_m.__builtins__ = module_builtins
        install(namespace + ".webapi.payloads", payloads_m)
        spec_pay.loader.exec_module(payloads_m)

        spec_iss = importlib.util.spec_from_file_location(namespace + ".webapi.issue_routes", str(SRC_DIR / "symphony" / "webapi" / "issue_routes.py"))
        issue_routes_m = importlib.util.module_from_spec(spec_iss)
        issue_routes_m.__builtins__ = module_builtins
        install(namespace + ".webapi.issue_routes", issue_routes_m)
        spec_iss.loader.exec_module(issue_routes_m)

        doubles_dict = {
            "Issue": Issue,
            "ServiceConfig": ServiceConfig,
            "Orchestrator": Orchestrator,
            "Ctx": _Ctx,
            "StubResponse": StubResponse,
            "Request": aio_web.Request,
            "Application": aio_web.Application,
            "FileBoardTracker": FileBoardTracker,
        }
        return api_team_m, payloads_m, issue_routes_m, doubles_dict

    finally:
        for name, previous in saved.items():
            if previous is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


api_team, payloads, issue_routes, doubles = load_isolated_modules()
Issue = doubles["Issue"]
ServiceConfig = doubles["ServiceConfig"]
Orchestrator = doubles["Orchestrator"]
Ctx = doubles["Ctx"]
StubResponse = doubles["StubResponse"]
Request = doubles["Request"]
Application = doubles["Application"]
FileBoardTracker = doubles["FileBoardTracker"]


class BaseAttentionQueueTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name).resolve()
        self.workflow_path = self.root_path / "WORKFLOW.yaml"
        self.workflow_path.write_text("tracker: {kind: file}", encoding="utf-8")
        self.board_root = self.root_path / "cards"
        self.board_root.mkdir(parents=True, exist_ok=True)
        self.state_root = self.root_path / ".ours-state" / "api-team"
        self.state_root.mkdir(parents=True, exist_ok=True)

        self.cfg = ServiceConfig(
            workflow_path=self.workflow_path,
        )
        self.cfg.tracker.board_root = self.board_root
        self.cfg.tracker.active_states = ["In Progress", "Needs Human", "Blocked", "Нужна помощь", "Заблокирован"]
        self.cfg.tracker.terminal_states = ["Done", "Closed"]

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_card(self, identifier: str, frontmatter: dict | None = None, body: str = ""):
        lines = ["---"]
        frontmatter = {"identifier": identifier, "state": "Needs Human", **(frontmatter or {})}
        for k, v in frontmatter.items():
            lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append(body)
        content = "\n".join(lines)
        card_file = self.board_root / f"{identifier}.md"
        card_file.write_text(content, encoding="utf-8")
        return card_file

    def create_state_file(self, identifier: str, filename: str, data: dict, mode: int = 0o600):
        target_dir = self.state_root / identifier
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / filename
        target_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.chmod(target_file, mode)
        return target_file


class TestAttentionQueueContractAndCompatibility(BaseAttentionQueueTestCase):
    """Verify GET /api/v1/board backwards compatibility and exact value preservation."""

    def test_board_payload_structure_and_exact_values(self):
        issue1 = Issue(identifier="OMS-1", state="In Progress", title="Task 1")
        issue2 = Issue(identifier="OMS-2", state="Needs Human", title="Task 2")
        self.create_card("OMS-1")
        self.create_card("OMS-2")
        self.create_state_file("OMS-2", "assignment.json", {
            "identifier": "OMS-2",
            "status": "stopped",
            "stop_reason": "Команда ожидает вмешательства оператора",
        })

        app = Application()
        ctx = Ctx(self.cfg)
        orchestrator = Orchestrator()

        def fetch_issues(states):
            return [issue1, issue2]

        with patch.object(FileBoardTracker, "fetch_issues_by_states", side_effect=fetch_issues):
            issue_routes._register_issue_routes(app, ctx, orchestrator)
            routes = app.router.routes
            self.assertIn("/api/v1/board", routes)
            handle_board = routes["/api/v1/board"]

            loop = asyncio.new_event_loop()
            try:
                resp = loop.run_until_complete(handle_board(Request()))
            finally:
                loop.close()

            # Decoder check on resp.body (not stub-only resp.json())
            self.assertIsInstance(resp.body, bytes)
            data = json.loads(resp.body.decode("utf-8"))

            # Deep equality verification of board, issues, live
            self.assertEqual(data["board"], {
                "name": self.root_path.name,
                "tracker_kind": "file",
                "read_only": False,
                "default_agent_kind": "default",
                "new_issue_kind": "default",
                "id_prefix": "OMS",
                "agent_kinds": [],
                "api_team_kinds": [],
                "api_team_model_kinds": [],
            })
            self.assertEqual(len(data["issues"]), 2)
            self.assertEqual(data["issues"][0]["identifier"], "OMS-1")
            self.assertEqual(data["issues"][1]["identifier"], "OMS-2")
            self.assertEqual(data["live"], {})

            # Backwards-compatible attention_queue field
            self.assertIn("attention_queue", data)
            self.assertEqual(len(data["attention_queue"]), 1)
            item = data["attention_queue"][0]
            self.assertEqual(item["identifier"], "OMS-2")
            self.assertEqual(item["state"], "Needs Human")
            self.assertEqual(item["parent_identifier"], None)
            self.assertEqual(item["reason"], "Команда ожидает вмешательства оператора")

    def test_read_only_tracker_returns_empty_queue(self):
        self.cfg.tracker.kind = "github"
        app = Application()
        ctx = Ctx(self.cfg)
        orchestrator = Orchestrator()
        issue_routes._register_issue_routes(app, ctx, orchestrator)
        handle_board = app.router.routes["/api/v1/board"]

        loop = asyncio.new_event_loop()
        try:
            resp = loop.run_until_complete(handle_board(Request()))
        finally:
            loop.close()

        data = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(data["board"]["read_only"])
        self.assertEqual(data["attention_queue"], [])


class TestAttentionStatesAndLocalization(BaseAttentionQueueTestCase):
    def test_attention_states_recognition(self):
        states = [
            "needs_human", "Needs Human", "needs human",
            "blocked", "Blocked", "BLOCKED",
            "Нужна помощь", "нужна помощь", "нужна_помощь",
            "Заблокирован", "заблокирована", "заблокировано",
        ]
        issues = [Issue(identifier=f"OMS-{idx}", state=st) for idx, st in enumerate(states)]
        for iss in issues:
            self.create_card(iss.identifier)

        queue = payloads._attention_queue_payload(issues, self.cfg)
        self.assertEqual(len(queue), len(states))
        matched_ids = [item["identifier"] for item in queue]
        self.assertEqual(matched_ids, [iss.identifier for iss in issues])

    def test_non_attention_states_excluded(self):
        issues = [
            Issue(identifier="OMS-ACTIVE", state="In Progress"),
            Issue(identifier="OMS-DONE", state="Done"),
            Issue(identifier="OMS-TODO", state="Todo"),
            Issue(identifier="OMS-REV", state="Human Review"),
        ]
        for iss in issues:
            self.create_card(iss.identifier)

        queue = payloads._attention_queue_payload(issues, self.cfg)
        self.assertEqual(queue, [])


class TestAgentKindNeutrality(BaseAttentionQueueTestCase):
    def test_any_agent_kind_included(self):
        issues = [
            Issue(identifier="OMS-FLASH", state="Needs Human", agent_kind="flash"),
            Issue(identifier="OMS-PRO", state="Blocked", agent_kind="pro"),
            Issue(identifier="OMS-CODER", state="Заблокирован", agent_kind="coder"),
            Issue(identifier="OMS-NONE", state="Needs Human", agent_kind=None),
        ]
        for iss in issues:
            self.create_card(iss.identifier)

        queue = payloads._attention_queue_payload(issues, self.cfg)
        self.assertEqual(len(queue), 4)


class TestOrderAndDeduplication(BaseAttentionQueueTestCase):
    def test_stable_order_and_deduplication(self):
        issues = [
            Issue(identifier="OMS-B", state="Needs Human"),
            Issue(identifier="OMS-A", state="Blocked"),
            Issue(identifier="OMS-B", state="Needs Human"),
            Issue(identifier="OMS-C", state="Нужна помощь"),
        ]
        for iss in issues:
            self.create_card(iss.identifier)

        queue = payloads._attention_queue_payload(issues, self.cfg)
        self.assertEqual([item["identifier"] for item in queue], ["OMS-B", "OMS-A", "OMS-C"])


class TestConflictResolutionAndFreshness(BaseAttentionQueueTestCase):
    """Verify real conflict cases and freshness priorities required by AC3/4/5."""

    def test_conflict_old_assignment_error_vs_stopped_session(self):
        # A current, explicitly linked stopped session beats an old error field.
        self.create_card("OMS-10")
        self.create_state_file("OMS-10", "assignment.json", {
            "identifier": "OMS-10",
            "status": "stopped",
            "error": "release_merge_conflict",
            "session_file": "session.json",
            "run_id": "current-run",
        })
        self.create_state_file("OMS-10", "session.json", {
            "identifier": "OMS-10",
            "status": "stopped",
            "run_id": "current-run",
            "reason": "flash_quota_exhausted",
        })

        issue = Issue(identifier="OMS-10", state="Needs Human")
        queue = payloads._attention_queue_payload([issue], self.cfg)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["reason"], "По последнему снимку квота Gemini исчерпана.")

    def test_conflict_running_current_assignment_vs_old_session(self):
        # Assignment is actively running; session has stopped reason. Session must be ignored!
        self.create_card("OMS-20")
        self.create_state_file("OMS-20", "assignment.json", {
            "identifier": "OMS-20",
            "status": "running",
        })
        self.create_state_file("OMS-20", "session.json", {
            "identifier": "OMS-20",
            "status": "stopped",
            "reason": "flash_quota_exhausted",
        })

        issue = Issue(identifier="OMS-20", state="Blocked")
        queue = payloads._attention_queue_payload([issue], self.cfg)
        self.assertEqual(len(queue), 1)
        # Running assignment means old session is stale -> fallback
        self.assertEqual(queue[0]["reason"], api_team.FALLBACK_REASON)

    def test_conflict_heading_only_markdown_falls_back(self):
        # Card has '## Check Failure' heading but no content underneath -> fallback
        self.create_card("OMS-30", body="## Check Failure\n\n## Next Section\nSome text")
        issue = Issue(identifier="OMS-30", state="Needs Human")
        queue = payloads._attention_queue_payload([issue], self.cfg)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["reason"], api_team.FALLBACK_REASON)

    def test_conflict_historical_markdown_without_current_stop_falls_back(self):
        # Historical check failure section without explicit current stop section or frontmatter -> fallback
        self.create_card("OMS-31", body="## Previous Attempts\nOld error happened yesterday")
        issue = Issue(identifier="OMS-31", state="Blocked")
        queue = payloads._attention_queue_payload([issue], self.cfg)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["reason"], api_team.FALLBACK_REASON)

    def test_explicit_current_stop_in_frontmatter_wins(self):
        self.create_card("OMS-32", frontmatter={"current_stop_reason": "Превышен лимит API ходов"})
        issue = Issue(identifier="OMS-32", state="Needs Human")
        queue = payloads._attention_queue_payload([issue], self.cfg)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["reason"], "Превышен лимит API ходов")

    def test_explicit_current_stop_section_in_markdown(self):
        body = "## Current Stop\nТест интеграции упал с таймаутом\n\n## Notes\nOther details"
        self.create_card("OMS-33", body=body)
        issue = Issue(identifier="OMS-33", state="Needs Human")
        queue = payloads._attention_queue_payload([issue], self.cfg)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["reason"], "Тест интеграции упал с таймаутом")

    def test_corrupted_nested_shapes_isolated_among_healthy_cards(self):
        # Healthy card
        self.create_card("OMS-HEALTHY")
        self.create_state_file("OMS-HEALTHY", "assignment.json", {
            "identifier": "OMS-HEALTHY",
            "status": "stopped",
            "reason": "operator_stopped",
        })

        # Card with bad plan types (plan=string, launch=bad)
        self.create_card("OMS-CORRUPT-1")
        self.create_state_file("OMS-CORRUPT-1", "assignment.json", {
            "identifier": "OMS-CORRUPT-1",
            "status": "stopped",
            "plan": "not_a_dict",
            "candidate": 12345,
            "launch": ["not", "dict"],
        })

        # Card with launch.reason as list/dict
        self.create_card("OMS-CORRUPT-2")
        self.create_state_file("OMS-CORRUPT-2", "assignment.json", {
            "identifier": "OMS-CORRUPT-2",
            "status": "stopped",
            "plan": {"launch": {"status": "stopped", "reason": ["a", "list"]}},
        })

        issues = [
            Issue(identifier="OMS-HEALTHY", state="Needs Human"),
            Issue(identifier="OMS-CORRUPT-1", state="Blocked"),
            Issue(identifier="OMS-CORRUPT-2", state="needs_human"),
        ]

        queue = payloads._attention_queue_payload(issues, self.cfg)
        self.assertEqual(len(queue), 3)
        self.assertEqual(queue[0]["identifier"], "OMS-HEALTHY")
        self.assertEqual(queue[0]["reason"], "Остановлено оператором или после потери связи с доской.")
        self.assertEqual(queue[1]["identifier"], "OMS-CORRUPT-1")
        self.assertEqual(queue[1]["reason"], api_team.FALLBACK_REASON)
        self.assertEqual(queue[2]["identifier"], "OMS-CORRUPT-2")
        self.assertEqual(queue[2]["reason"], api_team.FALLBACK_REASON)


class TestFailFastMocks(BaseAttentionQueueTestCase):
    """Assert no invoke/subprocess/sync/recovery commands run during board resolution."""

    def test_assert_no_invoke_or_subprocess_executed(self):
        self.create_card("OMS-SAFE")
        self.create_state_file("OMS-SAFE", "assignment.json", {
            "identifier": "OMS-SAFE",
            "status": "stopped",
            "reason": "operator_stopped",
        })

        def forbidden_invoke(*args, **kwargs):
            raise AssertionError("FORBIDDEN: invoke() was called!")

        def forbidden_subprocess(*args, **kwargs):
            raise AssertionError("FORBIDDEN: subprocess was executed!")

        issue = Issue(identifier="OMS-SAFE", state="Needs Human")

        with patch.object(api_team, "invoke", side_effect=forbidden_invoke), \
             patch("subprocess.Popen", side_effect=forbidden_subprocess), \
             patch("subprocess.run", side_effect=forbidden_subprocess), \
             patch("asyncio.create_subprocess_exec", side_effect=forbidden_subprocess):
            queue = payloads._attention_queue_payload([issue], self.cfg)
            self.assertEqual(len(queue), 1)
            self.assertEqual(queue[0]["identifier"], "OMS-SAFE")


class TestAppRecreationAndFileImmutability(BaseAttentionQueueTestCase):
    """App recreation and byte-for-byte immutability in a single unified scenario."""

    def test_recreate_app_and_verify_immutability(self):
        self.create_card("OMS-A", frontmatter={"title": "Card A"})
        self.create_card("OMS-B", frontmatter={"parent": "OMS-A"})
        self.create_state_file("OMS-B", "assignment.json", {
            "identifier": "OMS-B",
            "status": "stopped",
            "reason": "operator_stopped",
        })

        file_hashes_before = {}
        for p in self.root_path.glob("**/*"):
            if p.is_file():
                file_hashes_before[p] = hashlib.sha256(p.read_bytes()).hexdigest()

        issues = [
            Issue(identifier="OMS-A", state="In Progress"),
            Issue(identifier="OMS-B", state="Needs Human"),
        ]

        def fetch_issues(states):
            return issues

        # App 1
        app1 = Application()
        ctx1 = Ctx(self.cfg)
        orch1 = Orchestrator()
        with patch.object(FileBoardTracker, "fetch_issues_by_states", side_effect=fetch_issues):
            issue_routes._register_issue_routes(app1, ctx1, orch1)
            h1 = app1.router.routes["/api/v1/board"]
            loop = asyncio.new_event_loop()
            try:
                resp1 = loop.run_until_complete(h1(Request()))
            finally:
                loop.close()

        # App 2 (recreated from scratch)
        app2 = Application()
        ctx2 = Ctx(self.cfg)
        orch2 = Orchestrator()
        with patch.object(FileBoardTracker, "fetch_issues_by_states", side_effect=fetch_issues):
            issue_routes._register_issue_routes(app2, ctx2, orch2)
            h2 = app2.router.routes["/api/v1/board"]
            loop = asyncio.new_event_loop()
            try:
                resp2 = loop.run_until_complete(h2(Request()))
            finally:
                loop.close()

        # Verify exact identical JSON responses
        data1 = json.loads(resp1.body.decode("utf-8"))
        data2 = json.loads(resp2.body.decode("utf-8"))
        self.assertEqual(data1, data2)
        self.assertEqual(data1["attention_queue"], [{
            "identifier": "OMS-B",
            "state": "Needs Human",
            "parent_identifier": "OMS-A",
            "reason": "Остановлено оператором или после потери связи с доской.",
        }])

        # Verify byte-for-byte immutability of all files
        file_hashes_after = {}
        for p in self.root_path.glob("**/*"):
            if p.is_file():
                file_hashes_after[p] = hashlib.sha256(p.read_bytes()).hexdigest()

        self.assertEqual(file_hashes_before, file_hashes_after)


class TestParentIdentifierRules(BaseAttentionQueueTestCase):
    def test_explicit_request_reference_to_card_is_parent(self):
        self.create_card("OMS-PARENT")
        self.create_card("OMS-CHILD")
        issue_parent = Issue(identifier="OMS-PARENT", state="In Progress")
        issue_child = Issue(identifier="OMS-CHILD", state="Needs Human", request="OMS-PARENT")

        queue = payloads._attention_queue_payload([issue_parent, issue_child], self.cfg)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["parent_identifier"], "OMS-PARENT")

    def test_self_reference_in_request_is_not_parent(self):
        self.create_card("OMS-SELF")
        issue_self = Issue(identifier="OMS-SELF", state="Needs Human", request="OMS-SELF")
        queue = payloads._attention_queue_payload([issue_self], self.cfg)
        self.assertEqual(len(queue), 1)
        self.assertIsNone(queue[0]["parent_identifier"])

    def test_arbitrary_group_in_request_is_not_parent(self):
        self.create_card("OMS-CHILD")
        issue = Issue(identifier="OMS-CHILD", state="Needs Human", request="GROUP-FRONTEND-TASK")
        queue = payloads._attention_queue_payload([issue], self.cfg)
        self.assertEqual(len(queue), 1)
        self.assertIsNone(queue[0]["parent_identifier"])

    def test_blocked_by_is_never_parent(self):
        self.create_card("OMS-BLOCKER")
        self.create_card("OMS-BLOCKED")
        issue_blocker = Issue(identifier="OMS-BLOCKER", state="In Progress")
        issue_blocked = Issue(
            identifier="OMS-BLOCKED",
            state="Blocked",
            blocked_by=[types.SimpleNamespace(identifier="OMS-BLOCKER", state="In Progress")],
        )
        queue = payloads._attention_queue_payload([issue_blocker, issue_blocked], self.cfg)
        self.assertEqual(len(queue), 1)
        self.assertIsNone(queue[0]["parent_identifier"])

    def test_number_suffix_is_never_parent(self):
        self.create_card("OMS-18")
        self.create_card("OMS-18-1")
        issue18 = Issue(identifier="OMS-18", state="In Progress")
        issue18_1 = Issue(identifier="OMS-18-1", state="Needs Human")
        queue = payloads._attention_queue_payload([issue18, issue18_1], self.cfg)
        self.assertEqual(len(queue), 1)
        self.assertIsNone(queue[0]["parent_identifier"])

    def test_deterministic_parent_indexing_and_ambiguity_exclusion(self):
        # Parent 1 claims Child
        self.create_card("OMS-P1")
        self.create_state_file("OMS-P1", "assignment.json", {
            "identifier": "OMS-P1",
            "plan": {"cards": [{"identifier": "OMS-C1"}]},
        })

        # Child card
        self.create_card("OMS-C1")
        c1 = Issue(identifier="OMS-C1", state="Needs Human")
        p1 = Issue(identifier="OMS-P1", state="In Progress")

        queue = payloads._attention_queue_payload([p1, c1], self.cfg)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["parent_identifier"], "OMS-P1")

        # Ambiguous claim: Parent 2 ALSO claims Child 1
        self.create_card("OMS-P2")
        self.create_state_file("OMS-P2", "assignment.json", {
            "identifier": "OMS-P2",
            "plan": {"cards": [{"identifier": "OMS-C1"}]},
        })
        p2 = Issue(identifier="OMS-P2", state="In Progress")

        # Must omit ambiguous parent (never pick randomly!)
        queue_ambiguous = payloads._attention_queue_payload([p1, p2, c1], self.cfg)
        self.assertEqual(len(queue_ambiguous), 1)
        self.assertIsNone(queue_ambiguous[0]["parent_identifier"])


class TestSecurityAndPathSafety(BaseAttentionQueueTestCase):
    def test_symlink_state_file_rejected(self):
        self.create_card("OMS-LINK")
        secret = self.root_path / "secret.json"
        secret.write_text(json.dumps({"identifier": "OMS-LINK", "reason": "leak"}), encoding="utf-8")
        card_dir = self.state_root / "OMS-LINK"
        card_dir.mkdir(parents=True, exist_ok=True)
        link = card_dir / "assignment.json"
        os.symlink(secret, link)

        issue = Issue(identifier="OMS-LINK", state="Needs Human")
        queue = payloads._attention_queue_payload([issue], self.cfg)
        self.assertEqual(queue[0]["reason"], api_team.FALLBACK_REASON)

    def test_symlink_card_file_rejected(self):
        secret = self.root_path / "secret.md"
        secret.write_text("## Current Stop\nSecret stop reason", encoding="utf-8")
        card_link = self.board_root / "OMS-SYMCARD.md"
        os.symlink(secret, card_link)

        issue = Issue(identifier="OMS-SYMCARD", state="Needs Human")
        queue = payloads._attention_queue_payload([issue], self.cfg)
        self.assertEqual(queue[0]["reason"], api_team.FALLBACK_REASON)

    def test_state_file_mode_not_600_rejected(self):
        self.create_card("OMS-MODE")
        self.create_state_file("OMS-MODE", "assignment.json", {
            "identifier": "OMS-MODE",
            "status": "stopped",
            "reason": "operator_stopped",
        }, mode=0o644)
        issue = Issue(identifier="OMS-MODE", state="Needs Human")
        queue = payloads._attention_queue_payload([issue], self.cfg)
        self.assertEqual(queue[0]["reason"], api_team.FALLBACK_REASON)

    def test_path_traversal_identifier_rejected(self):
        self.assertIsNone(api_team._read_assignment(self.cfg, "../OMS-TRAVERSAL"))
        self.assertIsNone(api_team._read_session(self.cfg, "../../etc/passwd"))

    def test_bounded_read_actual_bytes_limit(self):
        self.create_card("OMS-BIG")
        big_data = {"identifier": "OMS-BIG", "padding": "x" * (1024 * 1024 + 10)}
        self.create_state_file("OMS-BIG", "assignment.json", big_data)
        issue = Issue(identifier="OMS-BIG", state="Needs Human")
        queue = payloads._attention_queue_payload([issue], self.cfg)
        self.assertEqual(queue[0]["reason"], api_team.FALLBACK_REASON)

    def test_corrupted_json_isolated(self):
        self.create_card("OMS-CORRUPT-JSON")
        card_dir = self.state_root / "OMS-CORRUPT-JSON"
        card_dir.mkdir(parents=True, exist_ok=True)
        bad_file = card_dir / "assignment.json"
        bad_file.write_text("{malformed: json", encoding="utf-8")
        os.chmod(bad_file, 0o600)

        issue = Issue(identifier="OMS-CORRUPT-JSON", state="Needs Human")
        queue = payloads._attention_queue_payload([issue], self.cfg)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["reason"], api_team.FALLBACK_REASON)


class TestStrictCurrentRecords(BaseAttentionQueueTestCase):
    def queue(self, identifier="OMS-CURRENT", state="needs_human", **kwargs):
        issue = Issue(identifier=identifier, state=state, **kwargs)
        return payloads._attention_queue_payload([issue], self.cfg)[0]

    def test_active_or_unknown_assignment_invalidates_all_nested_history(self):
        for status in ("prepared", "queued", "running", "unassigned", "unknown", None, [], {}):
            with self.subTest(status=status):
                self.create_state_file("OMS-CURRENT", "assignment.json", {
                    "identifier": "OMS-CURRENT", "status": status,
                    "reason": "old assignment reason", "error": "old error",
                    "final": {"status": "failed", "reason": "old final"},
                    "plan": {"launch": {"status": "stopped", "reason": "old launch"}},
                    "session_file": "session.json",
                })
                self.create_state_file("OMS-CURRENT", "session.json", {
                    "identifier": "OMS-CURRENT", "status": "stopped", "reason": "old session",
                })
                self.assertEqual(self.queue()["reason"], api_team.FALLBACK_REASON)

    def test_current_assignment_beats_history_session_and_card(self):
        self.create_card("OMS-CURRENT", {"current_stop_reason": "card stop"},
                         "## Check Failure\nold card failure")
        self.create_state_file("OMS-CURRENT", "assignment.json", {
            "identifier": "OMS-CURRENT", "status": "stopped", "reason": "current assignment stop",
            "error": "old error", "attempts": [{"reason": "old attempt"}],
            "final": {"status": "failed", "reason": "old final"},
            "session_file": "session.json",
        })
        self.create_state_file("OMS-CURRENT", "session.json", {
            "identifier": "OMS-CURRENT", "status": "stopped", "reason": "session stop",
        })
        self.assertEqual(self.queue()["reason"], "current assignment stop")

    def test_result_error_candidate_status_and_unbound_session_are_not_causes(self):
        self.create_state_file("OMS-CURRENT", "assignment.json", {
            "identifier": "OMS-CURRENT", "status": "result", "reason": "old reason",
            "error": "old error", "candidate": {"status": "blocked"},
        })
        self.create_state_file("OMS-CURRENT", "session.json", {
            "identifier": "OMS-CURRENT", "status": "stopped", "reason": "unbound previous session",
        })
        self.assertEqual(self.queue()["reason"], api_team.FALLBACK_REASON)

    def test_final_precedes_launch_and_later_stage_without_reason_blocks_history(self):
        assignment = {
            "identifier": "OMS-CURRENT", "status": "result", "session_file": "session.json",
            "plan": {"launch": {"status": "stopped", "reason": "previous launch stop"}},
        }
        self.create_state_file("OMS-CURRENT", "session.json", {
            "identifier": "OMS-CURRENT", "status": "stopped", "reason": "previous planning stop",
        })
        for final in ({"status": "failed", "reason": "current final failure"},
                      {"status": "failed"}, {"status": "ready"}, {"status": "checking"}):
            with self.subTest(final=final):
                self.create_state_file("OMS-CURRENT", "assignment.json", {**assignment, "final": final})
                self.assertEqual(self.queue()["reason"], final.get("reason", api_team.FALLBACK_REASON))

    def test_stopped_launch_is_a_cause_but_missing_launch_reason_does_not_use_planning_session(self):
        for launch in ({"status": "stopped", "reason": "current child failed"},
                       {"status": "stopped"}, {"status": "active"}, {"status": "complete"}):
            with self.subTest(launch=launch):
                self.create_state_file("OMS-CURRENT", "assignment.json", {
                    "identifier": "OMS-CURRENT", "status": "result", "session_file": "session.json",
                    "plan": {"launch": launch},
                })
                self.create_state_file("OMS-CURRENT", "session.json", {
                    "identifier": "OMS-CURRENT", "status": "stopped", "reason": "old planning failure",
                })
                self.assertEqual(self.queue()["reason"], launch.get("reason", api_team.FALLBACK_REASON))

    def test_unstarted_later_stage_does_not_mask_current_earlier_stop(self):
        self.create_state_file("OMS-CURRENT", "assignment.json", {
            "identifier": "OMS-CURRENT", "status": "result", "final": {"status": "unassigned"},
            "plan": {"launch": {"status": "stopped", "reason": "current child failed"}},
        })
        self.assertEqual(self.queue()["reason"], "current child failed")
        self.create_state_file("OMS-CURRENT", "assignment.json", {
            "identifier": "OMS-CURRENT", "status": "stopped", "session_file": "session.json",
            "final": {"status": "unassigned"}, "plan": {"launch": {"status": "unassigned"}},
        })
        self.create_state_file("OMS-CURRENT", "session.json", {
            "status": "stopped", "stop_reason": "current planning stop",
        })
        self.assertEqual(self.queue()["reason"], "current planning stop")

    def test_session_attempt_and_card_identity_are_checked(self):
        self.create_state_file("OMS-CURRENT", "assignment.json", {
            "identifier": "OMS-CURRENT", "status": "stopped", "run_id": "current",
            "session_file": "session.json",
        })
        for identity, run_id in (("OTHER-1", "current"), ("OMS-CURRENT", "previous")):
            with self.subTest(identity=identity, run_id=run_id):
                self.create_state_file("OMS-CURRENT", "session.json", {
                    "identifier": identity, "run_id": run_id, "status": "stopped", "reason": "not current",
                })
                self.assertEqual(self.queue()["reason"], api_team.FALLBACK_REASON)
        for front in ({"identifier": "OTHER-1", "current_stop_reason": "wrong card"},
                      {"state": "Todo", "current_stop_reason": "wrong state"}):
            with self.subTest(front=front):
                self.create_card("OMS-CURRENT", front)
                self.assertEqual(self.queue()["reason"], api_team.FALLBACK_REASON)

    def test_default_session_needs_matching_attempt_and_stopped_status(self):
        self.create_state_file("OMS-CURRENT", "assignment.json", {
            "identifier": "OMS-CURRENT", "status": "stopped", "run_id": "current",
        })
        for status in ("dialogue_complete", "running", None, "stopped"):
            with self.subTest(status=status):
                self.create_state_file("OMS-CURRENT", "session.json", {
                    "identifier": "OMS-CURRENT", "run_id": "current", "status": status,
                    "stop_reason": "session current reason", "error": "old error",
                })
                expected = "session current reason" if status == "stopped" else api_team.FALLBACK_REASON
                self.assertEqual(self.queue()["reason"], expected)

    def test_last_explicit_current_section_wins_not_fenced_or_prefix_history(self):
        body = ("## Check Failure\nold failure\n\n"
                "## Current Stop\nold explicit slot\n\n"
                "## Current Stop\ncurrent cause\n\n"
                "## Current Stop archived\nnot current\n\n"
                "```markdown\n## Current Stop\nnot a record\n```\n")
        self.create_card("OMS-CURRENT", body=body)
        self.assertEqual(self.queue()["reason"], "current cause")

    def test_legacy_dated_stop_must_match_current_saved_revision(self):
        current = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
        self.create_card("OMS-CURRENT", body=(
            "## Check Failure (2026-11-07T10:00:00Z)\nprevious failure\n\n"
            "## Integration (2026-11-08T10:00:00Z)\ncurrent merge conflict\n"))
        self.assertEqual(self.queue(updated_at=current)["reason"], "current merge conflict")
        later = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(self.queue(updated_at=later)["reason"], api_team.FALLBACK_REASON)

    def test_quoted_and_multiline_card_scalars_not_yaml_objects(self):
        card = self.board_root / "OMS-CURRENT.md"
        card.write_text("---\nidentifier: OMS-CURRENT\nstate: needs_human\n"
                        "current_stop_reason: |-\n  Current cause:\n  need credentials\n---\n", encoding="utf-8")
        self.assertEqual(self.queue()["reason"], "Current cause:\nneed credentials")
        for scalar in ('"Current: quote # literal"', "'Operator''s stop'", "[old, error]", "null", "|"):
            with self.subTest(scalar=scalar):
                card.write_text("---\nidentifier: OMS-CURRENT\nstate: needs_human\n"
                                f"current_stop_reason: {scalar}\n---\n", encoding="utf-8")
                expected = {'"Current: quote # literal"': "Current: quote # literal",
                            "'Operator''s stop'": "Operator's stop"}.get(scalar, api_team.FALLBACK_REASON)
                self.assertEqual(self.queue()["reason"], expected)

    def test_saved_records_are_fresh_each_request_and_assignment_read_once_per_card(self):
        self.create_state_file("OMS-CURRENT", "assignment.json", {
            "identifier": "OMS-CURRENT", "status": "stopped", "reason": "first cause",
        })
        with patch.object(api_team, "_read_assignment", wraps=api_team._read_assignment) as reader:
            self.assertEqual(self.queue()["reason"], "first cause")
            self.assertEqual(reader.call_count, 1)
        self.create_state_file("OMS-CURRENT", "assignment.json", {
            "identifier": "OMS-CURRENT", "status": "stopped", "reason": "second cause",
        })
        self.assertEqual(self.queue()["reason"], "second cause")

    def test_subtask_parent_and_manifest_use_only_explicit_membership(self):
        child = Issue(identifier="OMS-CHILD", state="Blocked")
        parent = Issue(identifier="OMS-PARENT", state="Subtasks")
        self.create_state_file("OMS-CHILD", "assignment.json", {
            "identifier": "OMS-CHILD", "subtask": {"parent": "OMS-PARENT"},
        })
        self.assertEqual(payloads._attention_queue_payload([parent, child], self.cfg)[0]["parent_identifier"],
                         "OMS-PARENT")
        self.create_state_file("OMS-CHILD", "assignment.json", {"identifier": "OMS-CHILD"})
        self.create_state_file("OMS-PARENT", "subtasks.json", {"subtasks": [{"identifier": "OMS-CHILD"}]})
        self.assertEqual(payloads._attention_queue_payload([parent, child], self.cfg)[0]["parent_identifier"],
                         "OMS-PARENT")


class TestAdditionalReadSafety(BaseAttentionQueueTestCase):
    queue = TestStrictCurrentRecords.queue

    def test_intermediate_state_directory_symlinks_are_rejected(self):
        for index, component in enumerate((".ours-state", "api-team", "OMS-CURRENT")):
            with self.subTest(component=component):
                root = self.root_path / f"case-{index}"
                root.mkdir()
                cfg = ServiceConfig(workflow_path=root / "WORKFLOW.md")
                outside = self.root_path / f"outside-{index}"
                outside.mkdir()
                parts = [".ours-state", "api-team", "OMS-CURRENT"]
                split = parts.index(component)
                link = root.joinpath(*parts[:split + 1])
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(outside, target_is_directory=True)
                target = outside.joinpath(*parts[split + 1:], "assignment.json")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps({"identifier": "OMS-CURRENT", "status": "stopped",
                                              "reason": "must not read outside state"}), encoding="utf-8")
                target.chmod(0o600)
                self.assertIsNone(api_team._read_assignment(cfg, "OMS-CURRENT"))

    def test_unsafe_session_reference_never_falls_back_to_old_default(self):
        secret = self.root_path / "secret.json"
        secret.write_text(json.dumps({"identifier": "OMS-CURRENT", "status": "stopped",
                                      "reason": "outside secret"}), encoding="utf-8")
        secret.chmod(0o600)
        self.create_state_file("OMS-CURRENT", "session.json", {
            "identifier": "OMS-CURRENT", "run_id": "same", "status": "stopped", "reason": "old default",
        })
        for path in (str(secret), "../secret.json", "../OTHER/session.json", "a/../../secret.json",
                     "a\\session.json", "./session.json", "", [], {"path": "session.json"}):
            with self.subTest(path=path):
                self.create_state_file("OMS-CURRENT", "assignment.json", {
                    "identifier": "OMS-CURRENT", "status": "stopped", "run_id": "same", "session_path": path,
                })
                self.assertEqual(self.queue()["reason"], api_team.FALLBACK_REASON)

    def test_checked_relative_and_absolute_session_paths_stay_inside_card(self):
        target = self.create_state_file("OMS-CURRENT", "session.json", {
            "status": "stopped", "stop_reason": "selected current session",
        })
        for path in ("session.json", str(target)):
            with self.subTest(path=path):
                self.create_state_file("OMS-CURRENT", "assignment.json", {
                    "identifier": "OMS-CURRENT", "status": "stopped", "session_path": path,
                })
                self.assertEqual(self.queue()["reason"], "selected current session")
        nested = target.parent / "attempt-current"
        nested.mkdir()
        target.rename(nested / "session.json")
        self.create_state_file("OMS-CURRENT", "assignment.json", {
            "identifier": "OMS-CURRENT", "status": "stopped", "session_path": "attempt-current/session.json",
        })
        self.assertEqual(self.queue()["reason"], "selected current session")
        (target.parent / "linked").symlink_to(nested, target_is_directory=True)
        self.create_state_file("OMS-CURRENT", "assignment.json", {
            "identifier": "OMS-CURRENT", "status": "stopped", "session_path": "linked/session.json",
        })
        self.assertEqual(self.queue()["reason"], api_team.FALLBACK_REASON)

    def test_nonregular_wrong_owner_invalid_utf8_and_deep_json_are_isolated(self):
        self.create_state_file("OMS-HEALTHY", "assignment.json", {
            "identifier": "OMS-HEALTHY", "status": "stopped", "reason": "healthy current reason",
        })
        target = self.create_state_file("OMS-CURRENT", "assignment.json", {"identifier": "OMS-CURRENT"})
        for raw in (b"\xff", b"[]", b'{"identifier":"OMS-CURRENT","plan":' + b"[" * 1500 + b"]" * 1500 + b"}"):
            with self.subTest(kind=raw[:20]):
                target.write_bytes(raw)
                queue = payloads._attention_queue_payload([
                    Issue(identifier="OMS-CURRENT", state="Blocked"),
                    Issue(identifier="OMS-HEALTHY", state="needs_human"),
                ], self.cfg)
                self.assertEqual([row["reason"] for row in queue],
                                 [api_team.FALLBACK_REASON, "healthy current reason"])
        target.unlink()
        os.mkfifo(target, 0o600)
        self.assertIsNone(api_team._read_assignment(self.cfg, "OMS-CURRENT"))
        target.unlink()
        self.create_state_file("OMS-CURRENT", "assignment.json", {"identifier": "OMS-CURRENT"})
        original = os.fstat
        def wrong_owner(fd):
            info = original(fd)
            return types.SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size, st_uid=os.getuid() + 1)
        with patch.object(os, "fstat", side_effect=wrong_owner):
            self.assertIsNone(api_team._read_assignment(self.cfg, "OMS-CURRENT"))

    def test_actual_read_limit_even_if_file_grows_after_stat(self):
        self.create_state_file("OMS-CURRENT", "assignment.json", {
            "identifier": "OMS-CURRENT", "padding": "x" * 100,
        })
        original = os.fstat
        def old_size(fd):
            info = original(fd)
            return types.SimpleNamespace(st_mode=info.st_mode, st_size=0, st_uid=info.st_uid)
        with patch.object(os, "fstat", side_effect=old_size):
            self.assertIsNone(api_team._read_safe_state_file(self.cfg, "OMS-CURRENT", "assignment.json", max_bytes=32))

    def test_real_module_names_are_not_replaced_by_test_loader(self):
        before = {key: value for key, value in sys.modules.items()
                  if key == "aiohttp" or key.startswith(("symphony", "aiohttp."))}
        load_isolated_modules()
        after = {key: value for key, value in sys.modules.items()
                 if key == "aiohttp" or key.startswith(("symphony", "aiohttp."))}
        self.assertEqual(before, after)
        self.assertFalse(any(key.startswith("_oms_attention_queue_test") for key in sys.modules))


class TestHandlerReadOnlyGuarantees(BaseAttentionQueueTestCase):
    def test_handler_reads_unconfigured_attention_states_without_actions(self):
        self.cfg.tracker.active_states = ["In Progress"]
        self.cfg.tracker.terminal_states = ["Done"]
        issues = [Issue(identifier="OMS-B", state="Blocked", request="OMS-P"),
                  Issue(identifier="OMS-P", state="In Progress"),
                  Issue(identifier="OMS-A", state="needs_human"),
                  Issue(identifier="OMS-D", state="Done")]
        self.create_card("OMS-A", {"current_stop_reason": "ordinary card needs input"})
        self.create_card("OMS-B", {"state": "Blocked"})
        self.create_state_file("OMS-B", "assignment.json", {
            "identifier": "OMS-B", "status": "stopped", "reason": "current subtask stop",
        })
        def snapshot():
            return {str(path.relative_to(self.root_path)):
                    (path.stat().st_mode, path.stat().st_mtime_ns,
                     hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
                    for path in self.root_path.rglob("*")}
        before = snapshot()
        forbidden = MagicMock(side_effect=AssertionError("board GET must not execute actions"))
        orch = Orchestrator()
        orch._running = [{"issue_identifier": "OMS-P", "last_error": "historical live error"}]
        orch._attention = {"OMS-A": {"code": "inactive", "reason": "scheduler decision"}}
        orch.request_refresh = orch.recover_blocked = orch.resume = forbidden
        def fetch(states):
            self.assertIn("needs_human", states)
            self.assertIn("Blocked", states)
            return [issue for issue in issues if issue.state in states]
        responses = []
        with patch.object(FileBoardTracker, "fetch_issues_by_states", side_effect=fetch), \
             patch.object(api_team, "invoke", forbidden), \
             patch.object(api_team, "schedule_cloud_checks", forbidden), \
             patch("subprocess.Popen", forbidden), patch("subprocess.run", forbidden), \
             patch("asyncio.create_subprocess_exec", forbidden), patch("asyncio.create_subprocess_shell", forbidden):
            for _ in range(2):
                app = Application()
                issue_routes._register_issue_routes(app, Ctx(self.cfg), orch)
                response = asyncio.run(app.router.routes["/api/v1/board"](Request()))
                responses.append(json.loads(response.body))
        forbidden.assert_not_called()
        self.assertEqual(responses[0], responses[1])
        self.assertEqual(before, snapshot())
        data = responses[0]
        self.assertEqual(data["attention_queue"], [
            {"identifier": "OMS-A", "state": "needs_human", "parent_identifier": None,
             "reason": "ordinary card needs input"},
            {"identifier": "OMS-B", "state": "Blocked", "parent_identifier": "OMS-P",
             "reason": "current subtask stop"},
        ])
        self.assertEqual(data["live"], {"OMS-P": {"status": "running", **orch._running[0]}})
        for actual, issue in zip(data["issues"], sorted(issues, key=lambda i: i.identifier), strict=True):
            self.assertEqual(actual, {
                "identifier": issue.identifier, "title": "", "state": issue.state, "priority": 1,
                "labels": [], "skills": [], "agent_kind": "", "last_agent_kind": "",
                "request": issue.request or "", "blocked_by": [],
                "attention": orch._attention.get(issue.identifier), "created_at": None, "updated_at": None,
            })
        self.assertEqual(data["columns"], [
            {"name": "In Progress", "terminal": False, "description": "", "has_prompt": False},
            {"name": "Done", "terminal": True, "description": "", "has_prompt": False},
        ])


class TestRealApplicationIfAvailable(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_board_survives_application_recreation_without_writes(self):
        # Never substitute an HTTP/tracker double in this test. The task copy
        # may lack the rest of Symphony and aiohttp; report that as a real skip.
        try:
            from aiohttp import web
            from aiohttp.test_utils import TestClient, TestServer
        except ModuleNotFoundError as exc:
            self.skipTest(f"full HTTP runtime unavailable: {exc.name}")
        with patch.object(sys, "path", [str(SRC_DIR), *sys.path]):
            try:
                from symphony.workflow import WorkflowState
                from symphony.webapi.issue_routes import _register_issue_routes
            except ModuleNotFoundError as exc:
                self.skipTest(f"full Symphony runtime unavailable: {exc.name}")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            workflow = root / "WORKFLOW.md"
            workflow.write_text("---\ntracker:\n  kind: file\n  board_root: ./cards\n"
                                "  active_states: [Todo, Blocked]\n  terminal_states: [Done, needs_human]\n"
                                "agent:\n  kind: claude\n---\nTask.\n", encoding="utf-8")
            cards = root / "cards"
            cards.mkdir()
            (cards / "OMS-1.md").write_text("---\nid: OMS-1\nidentifier: OMS-1\ntitle: Stopped\n"
                                            "state: needs_human\ncurrent_stop_reason: Need operator\n---\n", encoding="utf-8")
            def snapshot():
                return {str(path.relative_to(root)): path.read_bytes()
                        for path in root.rglob("*") if path.is_file()}
            before = snapshot()
            replies = []
            for _ in range(2):
                state = WorkflowState(workflow)
                cfg, error = state.reload()
                self.assertIsNone(error)
                self.assertIsNotNone(cfg)
                app = web.Application()
                ctx = types.SimpleNamespace(config=lambda: cfg, workflow_dir=lambda: root)
                orch = types.SimpleNamespace(issue_attention=lambda issue: None,
                                             snapshot=lambda: {"running": [], "retrying": []})
                _register_issue_routes(app, ctx, orch)
                async with TestClient(TestServer(app)) as client:
                    response = await client.get("/api/v1/board")
                    self.assertEqual(response.status, 200)
                    replies.append(await response.json())
            self.assertEqual(replies[0], replies[1])
            self.assertEqual(replies[0]["attention_queue"], [{
                "identifier": "OMS-1", "state": "needs_human", "parent_identifier": None,
                "reason": "Need operator",
            }])
            self.assertEqual(before, snapshot())


if __name__ == "__main__":
    unittest.main()
