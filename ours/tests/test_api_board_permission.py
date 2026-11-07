"""Board permission uses existing execution and never turns drafts into paid work."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event

import httpx
import pytest
import yaml

from test_api_board import board as board_fixture, approve
from test_api_session import KEYS, delegate, exchange, frame
from test_api_project import git

import api_board as board_api
import api_session as session

board = board_fixture


def authorize(board):
    proposal = board.permission.prepare()
    return board.permission.authorize(proposal["confirmation_token"], lambda _: KEYS)


def new_card(board, identifier="TEST-2"):
    front, body = board.card()
    front.update(id=identifier, identifier=identifier, state="Todo",
                 api_defaults=board.teams.directory([])["creation_defaults"])
    (board.board_root / (identifier + ".md")).write_text("---\n" + yaml.safe_dump(front) + "---\n" + body)
    return board_api.Board(board.settings, {**board.context, "identifier": identifier})


def submitted(board):
    authorize(board)
    card = new_card(board)
    payload = card.show()["submission"]
    assert card.submit(payload)["status"] == "queued"
    return card, payload


def test_permission_preview_is_free_and_key_failure_does_not_authorize(board):
    proposal = board.permission.prepare()
    assert proposal["terms"]["budget_scope"] == "per_task"
    assert proposal["terms"]["budget"] is None
    assert board.permission.show()["status"] == "not_configured"
    with pytest.raises(session.SessionError, match="missing_api_key"):
        board.permission.authorize(proposal["confirmation_token"], lambda _: {})
    assert board.permission.show()["status"] == "not_configured"
    assert not board.path.exists() and not board.session_path.exists()


def test_owner_permission_survives_restart_and_is_not_a_card_submission(board):
    active = authorize(board)
    assert active["status"] == "active" and active["credentials_checked_at"]
    assert "confirmation_token" not in active and "token_sha256" not in json.dumps(active)
    assert all(secret not in json.dumps(active) for secret in KEYS.values())
    old = board.show()
    assert "submission" not in old
    assert old["submission_refusal"] == "api_permission_card_not_eligible"
    card = new_card(board)
    assert card.show()["status"] == "unassigned" and not card.path.exists()
    restarted = board_api.Board(card.settings, card.context)
    assert restarted.permission.show()["id"] == active["id"]
    assert restarted.show()["submission"] == card.show()["submission"]
    assert not restarted.session_path.exists()


def test_submission_is_exact_and_idempotent_and_manual_confirmation_cannot_rearm_it(board):
    card, payload = submitted(board)
    first = card._load()
    assert first["authorization"]["source"] == "board_permission"
    assert card.submit(payload)["status"] == "queued" and card._load() == first
    with pytest.raises(board_api.BoardError, match="invalid_api_confirmation"):
        card.confirm("invented")
    card.stop()
    assert card.submit(payload)["status"] == "stopped"
    assert not card.session_path.exists()


@pytest.mark.parametrize("change", ["task", "team", "mode", "paths", "workflow", "repo", "authorization"])
def test_changed_scope_is_refused_without_key_access_or_provider_call(board, change):
    authorize(board)
    card = new_card(board)
    payload = card.show()["submission"]
    if change == "task":
        card.tracker.update_fields(card.identifier, title="Changed task")
    elif change == "team":
        card.teams.save({"lead_profile": "sol-lead", "worker_profile": None})
    elif change in {"mode", "paths"}:
        raw = json.loads(card.settings.read_text())
        raw[change] = "productivity" if change == "mode" else ["README.md"]
        card.settings.write_text(json.dumps(raw))
    elif change == "workflow":
        card.workflow.write_text("changed")
    elif change == "repo":
        original = card.repo.with_name("original")
        card.repo.rename(original)
        card.repo.mkdir()
    else:
        payload["authorization_id"] = "0" * 32
    with pytest.raises((session.SessionError, session.project.ProjectError)):
        card.submit(payload)
    assert not card.path.exists() and not card.session_path.exists()


def test_owner_confirmation_is_invalidated_while_keychain_prompt_is_open(board):
    pending = board.permission.prepare()
    def credentials(_):
        board.permission.revoke()
        return KEYS
    with pytest.raises(session.SessionError, match="changed"):
        board.permission.authorize(pending["confirmation_token"], credentials)
    assert board.permission.show()["status"] == "revoked"


def test_stale_confirmation_cannot_undo_revocation(board):
    proposal = board.permission.prepare()
    active = board.permission.authorize(proposal["confirmation_token"], lambda _: KEYS)
    board.permission.revoke()
    replay = board.permission.authorize(proposal["confirmation_token"], lambda _: pytest.fail("No reread"))
    assert replay["status"] == "revoked" and replay["id"] == active["id"]


def test_reconfirming_unchanged_board_preserves_queued_cards_and_existing_grant(board):
    card, _ = submitted(board)
    before = card.permission.show()
    token = card.permission.prepare()["confirmation_token"]
    current = card.permission.authorize(token, lambda _: pytest.fail("Already authorized"))
    assert current["id"] == before["id"] and current["history"] == before["history"]
    assert card.permission.allows(card._load())


def test_revision_boundary_does_not_depend_on_moving_project_head(board):
    active = authorize(board)
    (board.repo / "README.md").write_text("Next accepted version\n")
    git(board.repo, "add", "README.md")
    git(board.repo, "commit", "-qm", "next version")
    git(board.repo, "branch", "-f", "our-main", "HEAD")
    assert board.permission.show()["id"] == active["id"]
    card = new_card(board)
    assert card.submit(card.show()["submission"])["status"] == "queued"


def test_queued_permission_is_revalidated_after_restart_before_key_access(board):
    card, payload = submitted(board)
    card.permission.revoke()
    restarted = board_api.Board(card.settings, card.context)
    result = restarted.run("a" * 32)
    assert result["status"] == "stopped" and result["reason"] == "api_permission_revoked"
    assert not card.session_path.exists()
    assert restarted.submit(payload)["status"] == "stopped"


def test_reauthorization_does_not_revive_earlier_cards(board):
    card, _ = submitted(board)
    previous = card.permission.show()["id"]
    card.permission.revoke()
    assert authorize(card)["id"] != previous
    assert card.run("a" * 32)["reason"] == "api_permission_revoked"
    assert card.show()["submission_refusal"] == "api_permission_card_not_eligible"


def test_revocation_during_lead_response_preserves_usage_and_blocks_worker(board):
    card, _ = submitted(board)
    requests = []
    def answer(request):
        requests.append(request)
        card.permission.revoke()
        return httpx.Response(200, json=frame(None, [delegate()]))
    result = card.run("a" * 32, credentials=lambda _: KEYS, transport=httpx.MockTransport(answer))
    assert len(requests) == 1 and result["reason"] == "api_permission_revoked"
    assert result["session"]["usage"]["deepseek-lead"]["input_tokens"] == 12
    assert result["session"]["usage"]["luna-worker"]["calls"] == 0
    assert card.permission.show()["history"][-1]["action"] == "revoked"


def test_revocation_between_request_preparation_and_admission_sends_nothing(board, monkeypatch):
    card, _ = submitted(board)
    original = session.api.prepare
    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        card.permission.revoke()
        return result
    monkeypatch.setattr(session.api, "prepare", prepare)
    transport = httpx.MockTransport(lambda _: pytest.fail("Permission was revoked before admission"))
    result = card.run("a" * 32, credentials=lambda _: KEYS, transport=transport)
    assert result["reason"] == "api_permission_revoked" and result["session"]["calls"] == 0


def test_revocation_during_budget_preflight_prevents_generation_and_keeps_measurement(board, monkeypatch):
    raw = json.loads(board.settings.read_text())
    raw["session"]["budget"] = {"max_usd": "100", "input_tokens_total": 3000000}
    board.settings.write_text(json.dumps(raw))
    monkeypatch.setattr(session.budget, "today", lambda: date(2026, 11, 3))
    card, _ = submitted(board)
    requests = []
    def answer(request):
        requests.append(request)
        assert request.method == "GET"
        card.permission.revoke()
        return httpx.Response(200, json={"object": "list", "data": [{"id": "deepseek-flash", "object": "model",
            "context_window": 1048576, "max_output_tokens": 393216}]})
    result = card.run("a" * 32, credentials=lambda _: KEYS, transport=httpx.MockTransport(answer))
    assert len(requests) == 1 and result["reason"] == "api_permission_revoked"
    assert result["session"]["calls"] == 0
    assert session._load(card.session_path)["preflights"][0]["status"] == "measured"


def test_interrupted_request_stays_unknown_and_submission_never_replays_it(board):
    card, payload = submitted(board)
    def answer(request):
        raise httpx.ReadTimeout("simulated lost response")
    result = card.run("a" * 32, credentials=lambda _: KEYS, transport=httpx.MockTransport(answer))
    assert result["status"] == "stopped" and result["session"]["calls"] == 1
    assert session.spend_report(session._load(card.session_path))["totals"]["unknown_usage_calls"] == 1
    assert card.submit(payload)["status"] == "stopped"
    with pytest.raises(board_api.BoardError, match="not_queued"):
        card.run("b" * 32)


def test_existing_explicit_card_approval_remains_supported_but_obeys_board_stop(board):
    approve(board)
    board.permission.revoke()
    assert board.run("a" * 32)["reason"] == "api_permission_revoked"
    assert not board.session_path.exists()


def test_success_uses_existing_candidate_checks_and_history(board, monkeypatch):
    context = {**board.context, "active_states": ["Todo", "In Progress", "Verify"],
               "terminal_states": ["Human Review", "needs_human", "Done", "Archive"]}
    board = board_api.Board(board.settings, context)
    card, _ = submitted(board)
    transport, sent = exchange([frame("Работа выполнена")])
    monkeypatch.setattr(board_api.candidate, "prepare", lambda *a, **kw: {"status": "no_changes"})
    result = card.run("a" * 32, credentials=lambda _: KEYS, transport=transport)
    assert len(sent) == 1 and result["authorization"]["source"] == "board_permission"
    assert result["session"]["calls"] == 1 and not result["accepted"]


def test_invalid_permission_is_visible_and_cannot_authorize_a_card(board):
    authorize(board)
    board.permission.path.write_text('{}')
    assert board.permission.show()["status"] == "invalid"
    assert board.show()["execution_permission"]["status"] == "invalid"


def test_corrupt_permission_stops_queued_card_before_loading_keys(board):
    card, _ = submitted(board)
    card.permission.path.write_text('{}')
    result = card.run("a" * 32)
    assert result["status"] == "stopped" and result["reason"] == "api_permission_revoked"
    assert card.card()[0]["state"] == "needs_human"
    assert not card.session_path.exists()


def test_permission_refuses_symlinked_private_state(board, tmp_path):
    path = board.permission.path
    target = tmp_path / "foreign.json"
    target.write_text('{}')
    path.symlink_to(target)
    with pytest.raises(session.SessionError):
        board.permission.revoke()


def test_request_guard_releases_before_network_and_serializes_revocation(board):
    card, _ = submitted(board)
    value = card._load()
    entered = Event()
    def revoke():
        entered.set()
        return card.permission.revoke()
    with ThreadPoolExecutor(1) as pool:
        with card.permission.admit(value):
            stopped = pool.submit(revoke)
            assert entered.wait(3) and not stopped.done()
        assert stopped.result(timeout=3)["status"] == "revoked"
    with pytest.raises(session.SessionError, match="api_permission_revoked"):
        with card.permission.admit(value):
            pytest.fail("Cannot admit a request after revocation")


def test_crash_before_permission_publication_cannot_activate_on_replay(board, monkeypatch):
    pending = board.permission.prepare()
    original = session._save
    def fail(path, value):
        if path == board.permission.path:
            raise OSError("simulated storage failure")
        original(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(session, "_save", fail)
        with pytest.raises(session.SessionError, match="state_io_error"):
            board.permission.authorize(pending["confirmation_token"], lambda _: KEYS)
    assert board.permission.show()["status"] == "not_configured"
    with pytest.raises(session.SessionError, match="incomplete"):
        board.permission.authorize(pending["confirmation_token"], lambda _: pytest.fail("No reread"))


def test_lost_submission_reply_acknowledges_one_durable_queue_entry(board, monkeypatch):
    authorize(board)
    card = new_card(board)
    payload = card.show()["submission"]
    def lost_reply(identifier, mutate):
        mutate(*card.card())
        raise OSError("simulated lost reply before ticket write")
    with monkeypatch.context() as patch:
        patch.setattr(card.tracker, "_mutate_ticket", lost_reply)
        with pytest.raises(session.SessionError, match="state_io_error"):
            card.submit(payload)
    stored = card._load()
    assert card.submit(payload)["status"] == "queued" and card._load() == stored
    assert not card.session_path.exists()
