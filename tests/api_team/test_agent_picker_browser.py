"""Agent menus in the actual UI, using only intercepted local responses."""

import mimetypes
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from . import STATIC
from .test_attention_queue_browser import card

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

ORIGIN = "http://agent-picker.test"
LEGACY = "OMS-8"


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class AgentPickerBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pw = sync_playwright().start()
        cls.addClassCleanup(pw.stop)
        if not Path(pw.chromium.executable_path).is_file():
            raise unittest.SkipTest("Playwright Chromium is not installed")
        cls.browser = pw.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.old = card(LEGACY, "Todo", agent_kind="claude", can_start=True)
        self.board = {
            "board": {
                "name": "OMS", "tracker_kind": "file", "read_only": False,
                "id_prefix": "OMS", "default_agent_kind": "claude", "new_issue_kind": "api-team",
                "agent_kinds": ["api-team", "claude", "codex", "gemini"],
                "agent_picker_kinds": ["api-team"],
                "api_team_kinds": ["api-team"], "api_team_model_kinds": ["api-team"],
            },
            "columns": [{"name": state, "terminal": False} for state in ("Todo", "Verify")],
            "issues": [self.old], "live": {}, "attention_queue": [],
        }
        self.mutations = []
        self.errors = []
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(context.close)
        self.page = context.new_page()
        self.page.set_default_timeout(5000)
        self.page.on("pageerror", lambda err: self.errors.append(str(err)))
        context.route("**/*", self.route)

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path
        if url.netloc != urlsplit(ORIGIN).netloc:
            self.errors.append(request.url)
            route.abort()
        elif request.method == "POST" and path == "/api/v1/issues":
            self.mutations.append((path, request.post_data_json))
            route.fulfill(status=201, json={"identifier": "OMS-9", "state": "Todo"})
        elif request.method == "POST" and path == f"/api/v1/issues/{LEGACY}/start":
            self.mutations.append((path, request.post_data_json))
            self.old["can_start"] = False
            route.fulfill(json={"identifier": LEGACY})
        elif request.method != "GET":
            self.errors.append(f"{request.method} {path}")
            route.abort()
        elif path == "/":
            route.fulfill(path=STATIC / "index.html", content_type="text/html")
        elif path == "/favicon.ico":
            route.fulfill(status=204, body="")
        elif path.startswith("/static/"):
            asset = STATIC / path.removeprefix("/static/")
            if asset.is_file() and asset.resolve().is_relative_to(STATIC):
                route.fulfill(path=asset, content_type=mimetypes.guess_type(asset)[0])
            else:
                self.errors.append(path)
                route.abort()
        elif path == "/api/v1/projects":
            route.fulfill(json={"projects": [], "current": None})
        elif path == "/api/v1/board":
            route.fulfill(json=self.board)
        elif path == "/api/v1/api-teams/api-team":
            pair = {"lead_profile": "sol-lead", "worker_profile": "flash-worker"}
            route.fulfill(json={
                "default": pair, "pairs": [pair], "assignments": {},
                "models": {"sol-lead": "gpt-6.2-sol", "flash-worker": "deepseek-flash"},
            })
        elif path == "/api/v1/runs":
            route.fulfill(json={"runs": []})
        elif path == f"/api/v1/issues/{LEGACY}":
            route.fulfill(json=self.old)
        else:
            self.errors.append(path)
            route.abort()

    def open_board(self):
        self.page.goto(f"{ORIGIN}/?lang=ru#/board")
        expect(self.page.locator(".card-id").filter(has_text=LEGACY)).to_be_visible()

    def select(self, scope, label):
        return scope.locator("label.form-group").filter(
            has=self.page.get_by_text(label, exact=True),
        ).locator("select")

    def new_card_form(self):
        self.open_board()
        self.page.get_by_role("button", name="+ New Issue", exact=True).click()
        return self.page.locator(".modal-backdrop form")

    def test_oms_new_card_places_the_configured_team_first(self):
        form = self.new_card_form()
        picker = self.select(form, "Agent")
        expect(picker.locator("option")).to_have_text(["api-team — Sol 6.2 + DeepSeek Flash 4.1 (по умолчанию)"])
        expect(picker).to_have_value("api-team")
        form.get_by_placeholder("Issue title").fill("Fresh OMS card")
        with self.page.expect_response(
            lambda resp: resp.request.method == "POST" and resp.url.endswith("/issues"),
        ):
            form.get_by_role("button", name="Create issue", exact=True).click()
        self.assertEqual(len(self.mutations), 1)
        self.assertEqual(self.mutations[0][1]["agent_kind"], "api-team")

    def test_saved_assignment_not_in_picker_is_selected_and_can_start(self):
        self.open_board()
        self.page.locator(".card-id").filter(has_text=LEGACY).click()
        drawer = self.page.locator("#drawer-panel.open")
        picker = self.select(drawer, "Agent")
        expect(picker).to_have_value("claude")
        expect(picker.locator("option:checked")).to_have_text("claude (сохранённое назначение)")
        expect(picker.locator('option[value="claude"]')).to_have_attribute("disabled", "")
        self.assertEqual(picker.locator("option:not([disabled])").evaluate_all("options => options.map(o => o.value)"),
                         ["api-team"])
        self.assertEqual(self.mutations, [])
        path = f"/api/v1/issues/{LEGACY}/start"
        with self.page.expect_response(lambda resp: resp.request.method == "POST" and resp.url.endswith(path)):
            drawer.get_by_role("button", name="Запустить задачу", exact=True).click()
        self.assertEqual(self.mutations, [(path, {"updated_at": self.old["updated_at"]})])
        self.assertEqual(self.old["agent_kind"], "claude")

    def test_legacy_card_without_explicit_pin_shows_its_inherited_backend(self):
        self.old["agent_kind"] = ""
        self.open_board()
        self.page.locator(".card-id").filter(has_text=LEGACY).click()
        picker = self.select(self.page.locator("#drawer-panel.open"), "Agent")
        expect(picker.locator("option:checked")).to_have_text("claude (сохранённое назначение)")
        expect(picker.locator("option:checked")).to_have_attribute("disabled", "")
        self.assertEqual(picker.locator("option:not([disabled])").evaluate_all("options => options.map(o => o.value)"),
                         ["api-team"])

    def test_ordinary_menu_preserves_choices_and_labels_state_default(self):
        del self.board["board"]["agent_picker_kinds"]
        self.board["board"].update(new_issue_kind="", stage_agent_kinds={"verify": "codex"})
        form = self.new_card_form()
        picker = self.select(form, "Agent")
        expect(picker.locator("option")).to_have_text([
            "claude (по умолчанию)", "api-team — Sol 6.2 + DeepSeek Flash 4.1", "claude", "codex", "gemini",
        ])
        self.select(form, "Состояние").select_option("Verify")
        expect(picker.locator("option:checked")).to_have_text("codex (по умолчанию)")
        expect(picker).to_have_value("")
