"""Escalation is visible in the exact board/card terms being authorized."""

import json

import pytest

from test_api_board import board as board_fixture
from test_api_session import KEYS

import api_effort as effort
import api_session as session

board = board_fixture


def test_card_and_board_expose_policy_and_old_confirmation_is_invalidated(board):
    raw = json.loads(board.settings.read_text())
    raw["session"] = {"lead_profile": "sol-lead", "lead_effort": "high", "worker_profile": None}
    board.settings.write_text(json.dumps(raw))
    old = board.prepare()["confirmation_token"]
    permission = board.permission.prepare()["confirmation_token"]
    raw["session"]["lead_escalation"] = effort.POLICY
    board.settings.write_text(json.dumps(raw))
    with pytest.raises(Exception, match="api_assignment_changed"):
        board.confirm(old)
    with pytest.raises(session.SessionError, match="api_permission_changed"):
        board.permission.authorize(permission, lambda _: pytest.fail("No keys for stale authorization"))
    fresh = board.prepare()
    assert fresh["lead_escalation"] == effort.POLICY and fresh["profiles"]["lead"]["effort"] == "high"
    proposal = board.permission.prepare()
    assert proposal["terms"]["lead_escalation"] == effort.POLICY
    allowed = board.permission.authorize(proposal["confirmation_token"], lambda _: KEYS)
    assert allowed["status"] == "active" and allowed["terms"]["lead_escalation"] == effort.POLICY
