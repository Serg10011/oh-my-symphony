"""A settings change takes effect on new cards without reusing an old grant."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

import pytest

from test_api_board import board as board_fixture, board_api
from test_api_board_permission import authorize, new_card, submitted
from test_api_cloud_check import RUN, complete_card, public_api, responses
from test_api_session import KEYS, exchange, frame

board = board_fixture
session = board_api.session


def save(permission, mode, paths):
    preview = permission.preview_settings(mode, paths)
    return permission.save_settings(mode, paths, preview["expected"])


def test_new_conditions_survive_restart_and_returning_to_old_values_cannot_reuse_grant(board):
    board = board_api.Board(board.settings, {**board.context, "terminal_states": [
        *board.context["terminal_states"], "Human Review",
    ]})
    original = json.loads(board.settings.read_text())
    board.settings.write_text(json.dumps({**original, "mode": "economy", "paths": ["README.md"]}))
    original = board.settings.read_bytes()
    active = authorize(board)
    old = new_card(board)
    before_card = old.tracker.find_path(old.identifier).read_bytes()
    pending = board.permission.prepare()["confirmation_token"]
    approved = board.permission.path.read_bytes()

    preview = board.permission.preview_settings("productivity", ["src", "README.md"])
    assert preview["current"]["mode"] == "economy"
    assert preview["proposed"]["profiles"]["lead"]["effort"] == "max"
    assert preview["permission_status"] == "changed"
    assert not board.teams.work_file.exists() and board.permission.show()["status"] == "active"
    board.permission.save_settings("productivity", ["README.md", "src"], preview["expected"])
    assert board.settings.read_bytes() == original and board.permission.path.read_bytes() == approved
    assert old.tracker.find_path(old.identifier).read_bytes() == before_card

    reopened = board_api.Board(board.settings, board.context)
    assert reopened.teams.directory([])["work_settings"] == {"mode": "productivity", "paths": ["README.md", "src"]}
    assert reopened.permission.show()["status"] == "changed"
    fresh = new_card(reopened, "TEST-3")
    assert "authorization_id" not in fresh.card()[0]["api_defaults"]
    plan = fresh.prepare()
    assert plan["project"]["paths"] == ["README.md", "src"] and plan["profiles"]["lead"]["effort"] == "max"
    with pytest.raises(board_api.BoardError, match="api_assignment_changed"):
        old.prepare()
    with pytest.raises(session.SessionError, match="api_permission_changed"):
        reopened.permission.authorize(pending, lambda _: pytest.fail("Stale confirmation asked for keys"))

    save(reopened.permission, "economy", ["README.md"])
    assert reopened.permission.show()["status"] == "changed"
    current = authorize(reopened)
    assert active["id"] != current["id"]
    assert "submission" not in old.show()
    assert new_card(reopened, "TEST-4").show()["submission"]["authorization_id"] == current["id"]
    assert not fresh.session_path.exists()


@pytest.mark.parametrize(("paths", "error"), [
    (["README.md", "no-such-file"], "api_board_path_missing"),
    (["/README.md"], "invalid_project_path"),
    (["src/../README.md"], "invalid_project_path"),
    (["README.md", ".env"], "project_scope_denied"),
    ([], "invalid_project_spec"),
])
def test_preview_applies_committed_path_checks(board, paths, error):
    with pytest.raises(session.SessionError, match=error):
        board.permission.preview_settings("economy", paths)
    assert not board.teams.work_file.exists() and not board.permission.path.exists()


def test_queued_and_running_cards_keep_their_request_admission_available(board):
    card, _ = submitted(board)
    permission = card.permission
    preview = permission.preview_settings("economy", ["README.md"])

    def refuse_while_busy():
        # Reject a settings edit without even contending for the lock that
        # an already authorized request may be using.
        with session._lock(permission.path):
            with pytest.raises(session.SessionError, match="api_board_settings_busy"):
                permission.save_settings("economy", ["README.md"], preview["expected"])

    refuse_while_busy()

    def credentials(_):
        assert card._load()["status"] == "running"
        refuse_while_busy()
        return KEYS

    transport, requests = exchange([frame()])
    outcome = card.run("a" * 32, credentials=credentials, transport=transport)
    assert outcome["status"] == "result" and len(requests) == 1
    assert permission.save_settings("economy", ["README.md"], preview["expected"])["status"] == "saved"


def test_settings_write_fences_manual_confirmation_and_other_previews(board, monkeypatch):
    permission = board.permission
    token = board.prepare()["confirmation_token"]
    original = permission.preview_settings("productivity", ["README.md"])
    other = permission.preview_settings("economy", ["src"])
    writing, release = Event(), Event()
    save_file = session._save

    def hold_work_file(path, value):
        if path == board.teams.work_file:
            writing.set()
            assert release.wait(5)
        save_file(path, value)

    monkeypatch.setattr(session, "_save", hold_work_file)
    with ThreadPoolExecutor(max_workers=1) as pool:
        change = pool.submit(permission.save_settings, "productivity", ["README.md"], original["expected"])
        try:
            assert writing.wait(5)
            with pytest.raises(session.SessionError, match="session_busy"):
                board.confirm(token)
        finally:
            release.set()
        assert change.result()["status"] == "saved"
    with pytest.raises(board_api.BoardError, match="api_assignment_changed"):
        board.confirm(token)
    with pytest.raises(session.SessionError, match="api_board_settings_changed"):
        permission.save_settings("economy", ["src"], other["expected"])
    assert board._load()["status"] == "prepared" and not board.session_path.exists()


def test_existing_result_still_accepts_its_check_after_default_scope_changes(board, monkeypatch):
    result = complete_card(board)
    candidate = result["candidate"]
    saved = board.path.read_bytes(), board.session_path.read_bytes()
    save(board.permission, "productivity", ["README.md"])
    assert (board.path.read_bytes(), board.session_path.read_bytes()) == saved
    public_api(monkeypatch, responses(candidate["commit"], candidate["tree"], candidate["base_commit"]))
    checked = board.attach_check(str(RUN))
    assert checked["cloud_check"]["receipt"]["status"] == "passed"
    assert "src" in checked["project"]["paths"] and checked["profiles"]["lead"]["effort"] == "high"
