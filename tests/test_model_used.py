"""`model_used` in the `turn_completed` event: the model that really answered."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from symphony.orchestrator.agent_phase import _model_used
from symphony.orchestrator.diagnostics import _ALLOWED_FIELDS

from tests.test_claude_cost_usd import PROBE_TURNS, _Harness


def test_one_model() -> None:
    assert _model_used({"modelUsage": {"claude-haiku-5-5": {"costUSD": 0.1}}}) == (
        "claude-haiku-5-5"
    )


def test_two_models_pick_the_larger_cost() -> None:
    usage = {"a": {"costUSD": 0.01}, "b": {"costUSD": 0.5}, "c": {"costUSD": 0.2}}
    assert _model_used({"modelUsage": usage}) == "b"


def test_tie_and_missing_cost_pick_the_first() -> None:
    assert _model_used({"modelUsage": {"a": {"costUSD": 1}, "b": {"costUSD": 1}}}) == "a"
    assert _model_used({"modelUsage": {"a": {}, "b": {"costUSD": 0.3}}}) == "b"
    assert _model_used({"modelUsage": {"a": {}, "b": {}}}) == "a"


def test_last_result_usage_beats_cumulative() -> None:
    payload = {
        "modelUsage": {"old": {"costUSD": 9.0}},
        "model_usage_since_last_result": {"new": {"costUSD": 0.1}},
    }
    assert _model_used(payload) == "new"


def test_empty_last_result_usage_falls_back_to_cumulative() -> None:
    payload = {
        "modelUsage": {"old": {"costUSD": 9.0}},
        "model_usage_since_last_result": {},
    }
    assert _model_used(payload) == "old"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"modelUsage": {}},
        {"modelUsage": None},
        {"modelUsage": ["claude-haiku-5-5"]},
        {"modelUsage": "claude-haiku-5-5"},
        {"modelUsage": {"": {"costUSD": 1}}},
        {"modelUsage": {"x" * 101: {"costUSD": 1}}},
    ],
)
def test_unusable_data_gives_none(payload: dict[str, Any]) -> None:
    assert _model_used(payload) is None


def test_non_numeric_cost_is_ignored() -> None:
    usage = {"a": {"costUSD": "9"}, "b": {"costUSD": True}, "c": {"costUSD": 0.1}}
    assert _model_used({"modelUsage": usage}) == "c"


def test_name_of_exactly_100_chars_is_kept() -> None:
    name = "m" * 100
    assert _model_used({"modelUsage": {name: {"costUSD": 1}}}) == name


def test_allowed_in_diagnostics() -> None:
    assert "model_used" in _ALLOWED_FIELDS["turn_completed"]


async def test_turn_completed_event_carries_model_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    await h.turn(PROBE_TURNS[0][0])
    [event] = h.events("turn_completed")
    assert event["model_used"] == "claude-sonnet-4-5"


async def test_turn_completed_event_has_no_key_without_model_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    original = h.backend._handle_stream_event

    async def _strip(event: dict[str, Any]) -> Any:
        event.pop("modelUsage", None)
        return await original(event)

    h.backend._handle_stream_event = _strip  # type: ignore[method-assign]
    await h.turn(PROBE_TURNS[0][0])
    [event] = h.events("turn_completed")
    assert "model_used" not in event
