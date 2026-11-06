"""An operator chooses people by role; changes may never silently change a paid assignment."""
import json

import pytest

from test_api_board import board as board_fixture, board_api
from test_api_cloud_check import RUN, complete_card, public_api, responses
from test_api_session import KEYS, exchange, frame

DS = {"lead_profile": "deepseek-lead", "worker_profile": "luna-worker"}
SOLO = {"lead_profile": "sol-lead", "worker_profile": None}
SOL_PAIR = {"lead_profile": "sol-lead", "worker_profile": "luna-worker"}

board = board_fixture  # pytest discovers the named fixture in this module.


def test_choices_are_free_and_old_confirmation_cannot_buy_a_different_pair(board):
    assert board.show()["team_picker"]["default"] == DS
    first = board.prepare()["confirmation_token"]
    board.teams.save(SOLO)
    with pytest.raises(board_api.BoardError, match="api_assignment_changed"):
        board.confirm(first)
    second = board.prepare(SOL_PAIR)
    assert second["profiles"]["lead"]["effort"] == "medium"
    assert board.teams.default() == SOLO
    with pytest.raises(board_api.BoardError, match="invalid_api_confirmation"):
        board.confirm(first)
    assert not board.session_path.exists() and board.card()[0]["state"] == "needs_human"
    board.confirm(second["confirmation_token"])
    with pytest.raises(board_api.BoardError, match="already_used"):
        board.prepare(DS)
    assert board.teams.directory([board.identifier])["assignments"][board.identifier] == SOL_PAIR


def test_one_assignment_moves_through_real_stages_and_keeps_approved_team(board, monkeypatch):
    board.context["active_states"] = ["Todo", "In Progress", "Verify", "Document"]
    config = json.loads(board.settings.read_text())
    config["session"]["lead_effort"] = "max"
    board.settings.write_text(json.dumps(config))
    token = board.prepare()["confirmation_token"]
    board.confirm(token)
    assert board.card()[0]["state"] == "Todo"
    board.teams.save(SOLO)
    stages = []
    def keys(spec):
        stages.append(board.card()[0]["state"])
        assert spec["lead_effort"] == "max" and spec["lead_profile"] == DS["lead_profile"]
        return KEYS
    prepare = board_api.candidate.prepare
    def check(*args, **kwargs):
        stages.append(board.card()[0]["state"])
        return prepare(*args, **kwargs)
    monkeypatch.setattr(board_api.candidate, "prepare", check)
    transport, requests = exchange([frame()])
    final = board.run("9" * 32, credentials=keys, transport=transport)
    assert final["status"] == "result" and stages == ["In Progress", "Verify"]
    assert board.card()[0]["state"] == "needs_human" and len(requests) == 1
    assert final["board_stages"] == ["Todo", "In Progress", "Verify", "needs_human"]
    assert board.confirm(token)["status"] == "result"
    assert "team_picker" not in board.show()
    dirs = board.teams.directory([board.identifier])
    assert dirs["default"] == SOLO and dirs["assignments"][board.identifier] == DS
    with pytest.raises(board_api.BoardError, match="not_queued"):
        board.run("8" * 32)


@pytest.mark.parametrize("choice", [
    {"lead_profile": "luna-worker", "worker_profile": "luna-worker"},
    {"lead_profile": "deepseek-lead", "worker_profile": None},
    {**SOLO, "endpoint": "https://example.invalid"},
])
def test_invalid_composition_changes_neither_default_nor_ticket(board, choice):
    for op in (board.teams.save, board.prepare):
        with pytest.raises(board_api.session.SessionError, match="invalid_composition"):
            op(choice)
    assert board.teams.default() == DS and not board.path.exists()


@pytest.mark.parametrize("passed", [True, False])
def test_candidate_waits_for_its_exact_checks_and_moves_without_a_paid_run(board, monkeypatch, passed):
    context = {**board.context, "active_states": ["Todo", "In Progress", "Verify", "Document"],
               "terminal_states": ["Human Review", "needs_human", "Done", "Archive"]}
    board = board_api.Board(board.settings, context)
    board.tracker.update_fields(board.identifier, state="Todo")
    result = complete_card(board)
    candidate = result["candidate"]
    assert candidate["status"] == "awaiting_full_checks" and board.card()[0]["state"] == "Verify"
    assert result["board_stages"] == ["Todo", "In Progress", "Verify"]
    docs = responses(candidate["commit"], candidate["tree"], candidate["base_commit"])
    if not passed:
        jobs = next(v for k,v in docs.items() if k.endswith("/jobs"))
        next(s for s in jobs["jobs"][0]["steps"] if s["name"] == "Run development suite")["conclusion"] = "failure"
    public_api(monkeypatch, docs)
    final = board.attach_check(str(RUN))
    target = "Human Review" if passed else "needs_human"
    assert final["board_stages"][-1] == board.card()[0]["state"] == target
    assert final["cloud_check"]["receipt"]["status"] == ("passed" if passed else "failed")
    assert final["accepted"] is False and final["session"]["calls"] == 2
