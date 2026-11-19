"""OMS-18/2: real-browser unittests using local, deterministic API fixtures.

The actual app.js, i18n.js, api-team.js and CSS run in Chromium. Only the
unrelated API-model decoration boundary is stubbed (its assets are outside this
subtask). Every URL is fulfilled locally; a live board or network is never used.
Missing Playwright/Chromium is a reported skip, not a passing browser scenario.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

STATIC = Path(__file__).resolve().parents[2] / "src/symphony/web/static"
ORIGIN = "http://oms-attention.test"
CHILD = "OMS-18-2"
PARENT = "OMS-18"
HELP = "OMS-10"
SAVED = "saved fixture"
SAVED_BOARD = f"/api/v1/projects/{SAVED}/board"
FALLBACK = "Причина не записана"

# Minimal app shell, not a replacement DOM implementation. The drawer scaffold
# is deliberately created by the application's existing ensureDrawerScaffold().
HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/style.css">
<script>
window.SymphonyApiModels = {
  decorate: async board => board,
  kindLabel: (board, kind) => kind,
  confirmedLabel: kind => kind
};
</script></head><body>
<div class="app">
<aside class="sidebar">
  <div id="board-name"></div><span id="conn-dot"></span><span id="conn-text"></span>
  <select id="project-selector"></select><button id="manage-projects">Projects</button>
  <div id="project-current-path"></div><div id="project-workflow-path"></div>
  <div id="project-board-path"></div>
  <a class="nav-item" data-route="board" href="#/board">Board</a>
  <a class="nav-item" data-route="runs" href="#/runs">Runs</a>
</aside>
<main class="main"><div id="view" class="view"></div></main>
</div><div id="overlay-root"></div><div id="toast-container"></div>
<script src="/static/i18n.js"></script><script src="/static/api-team.js"></script>
<script src="/static/app.js"></script></body></html>"""


def card(identifier: str, state: str, **fields) -> dict:
    return {
        "id": f"id-{identifier}", "identifier": identifier,
        "title": f"Card {identifier}", "state": state, "labels": [],
        "priority": None, "agent_kind": "codex", "blocked_by": [],
        "request": "", "description": "Local fixture description",
        "created_at": "2026-07-01T12:00:00Z", "updated_at": "2026-07-01T12:00:00Z",
        "artifacts": [], "live": None, "can_start": False, **fields,
    }


def board_fixture() -> dict:
    return {
        "board": {
            "name": "OMS local fixture", "tracker_kind": "file", "read_only": False,
            "path": "/fixture/kanban", "agent_kinds": ["codex"], "api_team_kinds": [],
        },
        "columns": [
            {"name": "Todo", "terminal": False},
            {"name": "needs_human", "terminal": True},
            {"name": "Blocked", "terminal": True},
            {"name": "Done", "terminal": True},
        ],
        "issues": [card(PARENT, "Todo"), card(HELP, "needs_human"),
                   card(CHILD, "Blocked", request=PARENT)],
        "live": {},
        "attention_queue": [
            {"identifier": HELP, "state": "needs_human", "parent_identifier": None,
             "reason": "Нужно уточнить требования"},
            {"identifier": CHILD, "state": "Blocked", "parent_identifier": PARENT,
             "reason": None},
        ],
    }


@unittest.skipIf(sync_playwright is None, "Browser scenarios blocked: Playwright is not installed")
class AttentionQueueBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.addClassCleanup(cls.pw.stop)
        # Do not convert application/driver failures to skips. Only an absent
        # installed browser binary is an environmental skip.
        if not Path(cls.pw.chromium.executable_path).is_file():
            raise unittest.SkipTest("Browser scenarios blocked: Playwright Chromium is not installed")
        cls.browser = cls.pw.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.board = board_fixture()
        self.requests = []
        self.unexpected = []
        self.page_errors = []
        self.console_errors = []
        self.board_unavailable = False
        self.allowed_patches = set()
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(6000)
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        self.page.on("console", self._console)
        self.context.route("**/*", self._route)

    def tearDown(self):
        self.assertEqual(self.page_errors, [], "Uncaught application errors")
        self.assertEqual(self.console_errors, [], "Browser console errors")
        self.assertEqual(self.unexpected, [], "Unexpected network/control requests")
        mutations = [(method, path) for method, path, _ in self.requests if method != "GET"]
        self.assertEqual(mutations, [("PATCH", path) for path in sorted(self.allowed_patches)])

    def _console(self, message):
        if message.type != "error":
            return
        # Expected network failures in the stale-snapshot scenario only.
        if self.board_unavailable and "503" in message.text:
            return
        self.console_errors.append(message.text)

    def _route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        path = unquote(parsed.path)
        body = request.post_data
        self.requests.append((request.method, path, body))

        def reply(data, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(data))

        if parsed.netloc != urlsplit(ORIGIN).netloc:
            self.unexpected.append((request.method, request.url))
            reply({}, 404)
        elif request.method == "GET" and path == "/":
            route.fulfill(content_type="text/html", body=HTML)
        elif request.method == "GET" and path == "/favicon.ico":
            route.fulfill(status=204, body="")
        elif request.method == "GET" and path.startswith("/static/"):
            name = path.removeprefix("/static/")
            if name not in {"app.js", "api-team.js", "i18n.js", "style.css"}:
                self.unexpected.append((request.method, path))
                reply({}, 404)
            else:
                route.fulfill(content_type="text/css" if name.endswith(".css") else "text/javascript",
                              body=(STATIC / name).read_text(encoding="utf-8"))
        elif request.method == "GET" and path == "/api/v1/projects":
            reply({"projects": [{"id": SAVED, "name": "Saved fixture", "saved": True}],
                   "current": None})
        elif request.method == "GET" and path in {"/api/v1/board", SAVED_BOARD}:
            reply({"error": {"message": "Fixture offline"}} if self.board_unavailable else self.board,
                  503 if self.board_unavailable else 200)
        elif request.method == "GET" and path == "/api/v1/runs":
            reply({"runs": [], "next_cursor": None})
        elif request.method == "GET" and path in self.detail_paths():
            identifier = path.rsplit("/", 1)[1]
            reply(next(deepcopy(issue) for issue in self.board["issues"]
                       if issue["identifier"] == identifier))
        elif request.method == "PATCH" and path in self.allowed_patches:
            fields = json.loads(body)
            self.assertEqual(fields, {"state": "Todo"})
            issue = next(issue for issue in self.board["issues"] if issue["identifier"] == CHILD)
            issue.update(fields)
            self.board["attention_queue"] = [item for item in self.board["attention_queue"]
                                              if item["identifier"] != CHILD]
            reply(issue)
        else:
            self.unexpected.append((request.method, path))
            reply({"error": {"message": "Unexpected fixture URL"}}, 404)

    def detail_paths(self):
        return {f"{prefix}/{issue['identifier']}" for prefix in ("/api/v1/issues", SAVED_BOARD)
                for issue in self.board["issues"]}

    def issue_gets(self):
        paths = self.detail_paths()
        return [path for method, path, _ in self.requests if method == "GET" and path in paths]

    def open_board(self, saved=False):
        if saved:
            self.board["board"]["read_only"] = True
        suffix = "&board=saved%20fixture" if saved else ""
        self.page.goto(f"{ORIGIN}/?lang=ru{suffix}#/board")
        expect(self.page.locator(".attention-queue")).to_be_visible()

    def row(self, identifier):
        return self.page.locator(".attention-item-row").filter(
            has=self.page.get_by_role("button", name=f"Открыть карточку {identifier}", exact=True))

    def assert_drawer(self, identifier):
        expect(self.page.locator("#drawer-panel.open .drawer-id")).to_have_text(identifier)
        expect(self.page.locator(".drawer-error")).to_have_count(0)

    def close_drawer(self):
        self.page.keyboard.press("Escape")
        expect(self.page.locator("#drawer-panel")).not_to_have_class("drawer open")

    def test_groups_parent_reason_and_no_per_card_fetches(self):
        self.open_board()
        expect(self.page.get_by_role("region", name="Очередь внимания", exact=True)).to_be_visible()
        help_group = self.page.locator(".attention-group-needs-human")
        blocked_group = self.page.locator(".attention-group-blocked")
        expect(help_group.locator("h3")).to_have_text("Нужна помощь")
        expect(blocked_group.locator("h3")).to_have_text("Заблокирована")
        expect(help_group.locator(".attention-item-id-btn")).to_have_text([HELP])
        expect(blocked_group.locator(".attention-item-id-btn")).to_have_text([CHILD])
        expect(self.row(CHILD).locator(".attention-item-parent")).to_contain_text(PARENT)
        expect(self.row(HELP).locator(".attention-item-parent")).to_have_count(0)
        expect(self.row(HELP).locator(".attention-item-reason")).to_have_text("Нужно уточнить требования")
        self.assertEqual(self.row(CHILD).locator(".attention-item-reason").text_content(), FALLBACK)
        # Reasons are not fetched by opening every card in the background.
        self.assertEqual(self.issue_gets(), [])
        expect(self.page.locator(".attention-queue button")).to_have_count(2)

    def test_mouse_row_and_primary_button_open_the_selected_child_not_parent(self):
        self.open_board()
        self.row(CHILD).locator(".attention-item-reason").click()
        self.assert_drawer(CHILD)
        self.close_drawer()
        self.row(HELP).locator("button").click()
        self.assert_drawer(HELP)
        self.assertEqual(self.issue_gets(), [f"/api/v1/issues/{CHILD}", f"/api/v1/issues/{HELP}"])

    def test_keyboard_tab_enter_and_space(self):
        self.open_board()
        self.row(HELP).locator("button").focus()
        self.page.keyboard.press("Tab")
        expect(self.row(CHILD).locator("button")).to_be_focused()
        for key in ("Enter", "Space"):
            self.row(CHILD).locator("button").focus()
            self.page.keyboard.press(key)
            self.assert_drawer(CHILD)
            self.close_drawer()
        # No duplicate activation from the native button's bubbling click.
        self.assertEqual(self.issue_gets(), [f"/api/v1/issues/{CHILD}"] * 2)

    def test_saved_board_context_is_used_for_the_selected_card(self):
        self.open_board(saved=True)
        self.row(CHILD).locator("button").focus()
        self.page.keyboard.press("Enter")
        self.assert_drawer(CHILD)
        self.assertEqual(self.issue_gets(), [f"{SAVED_BOARD}/{CHILD}"])
        self.assertEqual(parse_qs(urlsplit(self.page.url).query).get("board"), [SAVED])
        self.assertNotIn(("GET", "/api/v1/board", None), self.requests)
        expect(self.page.locator("#drawer-panel button")).to_have_count(1)  # close only

    def test_poll_updates_reasons_and_state_groups_despite_search_and_hidden_columns(self):
        self.open_board()
        self.page.locator("#board-search").fill("does-not-match-any-card")
        expect(self.row(CHILD)).to_be_visible()
        expect(self.row(HELP)).to_be_visible()
        # Both stopped columns are terminal, while the board scope is active.
        expect(self.page.locator(".board-scope-toggle .active")).to_have_text(
            self.page.evaluate("window.i18n.t('common.active')"))
        self.board["attention_queue"] = [
            {"identifier": CHILD, "state": "needs_human", "parent_identifier": PARENT,
             "reason": "Причина обновлена штатным опросом"}]
        self.board["issues"][2]["state"] = "needs_human"
        # No reload, internal hook, or direct render: wait for the real 5s poll.
        expect(self.page.locator(".attention-group-needs-human .attention-item-reason")).to_have_text(
            "Причина обновлена штатным опросом", timeout=12000)
        expect(self.page.locator(".attention-group-blocked .attention-item-row")).to_have_count(0)
        self.assertEqual(self.issue_gets(), [])
        self.assertGreaterEqual(sum(path == "/api/v1/board" for _, path, _ in self.requests), 2)

    def test_existing_drawer_state_change_refreshes_the_summary(self):
        self.open_board()
        self.row(CHILD).locator("button").click()
        self.assert_drawer(CHILD)
        path = f"/api/v1/issues/{CHILD}"
        self.allowed_patches.add(path)
        # The only mutation in this test is an explicit, pre-existing drawer
        # action. The new summary itself still exposes no control actions.
        self.page.locator("#drawer-panel .drawer-fields select").first.select_option("Todo")
        expect(self.row(CHILD)).to_have_count(0)
        expect(self.row(HELP)).to_be_visible()
        self.assertIn(("PATCH", path, '{"state":"Todo"}'), self.requests)

    def test_reopening_board_uses_latest_snapshot(self):
        self.open_board()
        self.page.locator('a[data-route="runs"]').click()
        expect(self.page.locator("#runs-search")).to_be_visible()
        self.board["attention_queue"] = []
        count = sum(path == "/api/v1/board" for _, path, _ in self.requests)
        # Wait for an actual background board response while another route is open.
        with self.page.expect_response(
                lambda response: urlsplit(response.url).path == "/api/v1/board", timeout=12000):
            pass
        self.assertGreater(sum(path == "/api/v1/board" for _, path, _ in self.requests), count)
        self.page.locator('a[data-route="board"]').click()
        expect(self.page.locator(".attention-queue-empty")).to_be_visible()
        expect(self.page.locator(".attention-item-row")).to_have_count(0)
        self.assertEqual(self.issue_gets(), [])

    def test_old_contract_unknown_is_distinct_from_confirmed_empty(self):
        self.board.pop("attention_queue")
        self.open_board()
        expect(self.page.locator(".attention-queue-unknown")).to_contain_text("Неизвестно")
        expect(self.page.locator(".attention-queue-empty")).to_have_count(0)
        self.board["attention_queue"] = []
        expect(self.page.locator(".attention-queue-empty")).to_be_visible(timeout=12000)
        expect(self.page.locator(".attention-queue-unknown")).to_have_count(0)
        expect(self.page.locator(".attention-item-row")).to_have_count(0)
        self.assertEqual(self.issue_gets(), [])

    def test_long_reason_is_safe_text_and_readable_on_narrow_screen(self):
        reason = '<img src="/xss-probe" onerror="window.injected=1">\n' + "длиннаяпричина" * 200
        self.board["attention_queue"][0]["reason"] = reason
        self.page.set_viewport_size({"width": 375, "height": 812})
        self.open_board()
        reason_node = self.row(HELP).locator(".attention-item-reason")
        self.assertEqual(reason_node.text_content(), reason)
        expect(reason_node.locator("img, script")).to_have_count(0)
        self.assertIsNone(self.page.evaluate("window.injected"))
        overflow = self.page.locator(".attention-queue").evaluate("""section => {
          const box = section.getBoundingClientRect();
          return box.left < 0 || box.right > innerWidth + 1 ||
            [...section.querySelectorAll('*')].some(node => node.scrollWidth > node.clientWidth + 1);
        }""")
        self.assertFalse(overflow, "The summary/reason must wrap without horizontal overflow")
        for identifier in (HELP, CHILD):
            self.row(identifier).locator("button").scroll_into_view_if_needed()
            expect(self.row(identifier).locator("button")).to_be_in_viewport()

    def test_labels_follow_existing_language_switch(self):
        self.open_board()
        for language, title, help_label, blocked_label in (
            ("en", "Attention Queue", "Needs human", "Blocked"),
            ("ru", "Очередь внимания", "Нужна помощь", "Заблокирована"),
        ):
            self.page.evaluate("language => window.i18n.setLang(language)", language)
            expect(self.page.locator(".attention-queue-title")).to_have_text(title)
            expect(self.page.locator(".attention-group-needs-human h3")).to_have_text(help_label)
            expect(self.page.locator(".attention-group-blocked h3")).to_have_text(blocked_label)
            # The exact missing-reason contract is independent of UI language.
            self.assertEqual(self.page.locator(".attention-group-blocked .attention-item-reason").text_content(),
                             FALLBACK)

    def test_stale_snapshot_blocks_mouse_and_keyboard_navigation(self):
        self.open_board()
        self.board_unavailable = True
        # Exercise the existing stale clock and failed polls, not a test-only flag.
        self.page.wait_for_function("document.querySelector('.main').classList.contains('board-stale')",
                                    timeout=23000)
        button = self.row(CHILD).locator("button")
        button.focus()
        for key in ("Enter", "Space"):
            self.page.keyboard.press(key)
        # Raw pointer input respects CSS pointer-events:none (locator.click would wait).
        button.scroll_into_view_if_needed()
        box = button.bounding_box()
        self.assertIsNotNone(box)
        self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        # Also exercise the handler guard independently of the CSS pointer block.
        self.row(CHILD).dispatch_event("click")
        self.page.wait_for_timeout(100)
        self.assertEqual(self.issue_gets(), [])
        expect(self.page.locator("#drawer-panel.open")).to_have_count(0)


if __name__ == "__main__":
    unittest.main()
