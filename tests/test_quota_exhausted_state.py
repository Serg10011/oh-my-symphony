"""`agent.quota_exhausted_state`: an exhausted credit parks the ticket, not the board."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import symphony.orchestrator.core as core_module
import symphony.orchestrator.quota_hold as quota_hold
from symphony.errors import ConfigValidationError
from symphony.orchestrator import Orchestrator, RunningEntry, worker_exit
from symphony.orchestrator.entries import _IssueDebug
from symphony.trackers.file import parse_ticket_file, write_ticket_atomic
from symphony.workflow import (
    ServiceConfig,
    build_service_config,
    parse_workflow_text,
)
from tests.test_orchestrator_dispatch import _issue, _make_config, _orch

CREDIT = "Credit balance is too low"
KEY_HELPER = "Your apiKeyHelper script is failing"
NOTE = "API credit exhausted - switch the board to the subscription."


def _board_config(
    tmp_path: Path, *, state: str = "needs_human", note: str = NOTE, **agent
) -> ServiceConfig:
    cfg = _make_config(
        tracker_kind="file",
        terminal_states=("Done", "needs_human"),
        workflow_path=tmp_path / "WORKFLOW.md",
        workspace_root=tmp_path / "ws",
        auto_triage_actionable_todo=False,
    )
    cfg = replace(cfg, tracker=replace(cfg.tracker, board_root=tmp_path / "board"))
    return replace(
        cfg,
        agent=replace(
            cfg.agent, quota_exhausted_state=state, quota_exhausted_note=note, **agent
        ),
    )


def _write_card(cfg: ServiceConfig, identifier: str, state: str = "In Progress") -> Path:
    board = cfg.tracker.board_root
    assert board is not None
    board.mkdir(parents=True, exist_ok=True)
    path = board / f"{identifier}.md"
    write_ticket_atomic(
        path,
        {
            "id": f"id-{identifier}",
            "identifier": identifier,
            "title": f"{identifier} title",
            "state": state,
            "priority": 2,
            "created_at": "2026-01-01T00:00:00Z",
        },
        "## Request\nDo it.",
    )
    return path


def _card(path: Path) -> tuple[dict, str]:
    front, body = parse_ticket_file(path)
    return front, body


def _fail_turn(
    monkeypatch, cfg: ServiceConfig, error: str, *, state: str = "In Progress"
):
    """Run `_handle_worker_error` for a card that sits on the real file board."""
    path = _write_card(cfg, "MT-Q", state)
    orch = _orch()
    issue = _issue("MT-Q", state=state)
    entry = RunningEntry(
        issue=issue,
        started_at=datetime.now(timezone.utc),
        retry_attempt=None,
        worker_task=None,  # type: ignore[arg-type]
        workspace_path=cfg.workspace_root / "MT-Q",
        agent_kind="codex",
    )
    debug = _IssueDebug()
    monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        worker_exit._handle_worker_error(
            orch, issue.id, entry, debug, "turn_error", error
        )

    asyncio.run(_run())
    return orch, entry, debug, path


@pytest.mark.parametrize("error", [CREDIT, KEY_HELPER])
def test_error_parks_the_card_in_the_configured_state(monkeypatch, tmp_path, error):
    cfg = _board_config(tmp_path)
    orch, entry, debug, path = _fail_turn(monkeypatch, cfg, error)

    front, body = _card(path)
    assert front["state"] == "needs_human"
    assert "## Quota Exhausted" in body
    section = body.split("## Quota Exhausted", 1)[1]
    assert error in section
    assert "was: In Progress" in section
    assert "moved to needs_human by agent.quota_exhausted_state" in section
    assert "no automatic switch" in section
    assert NOTE in section

    assert orch.is_paused(entry.issue.id) is False
    assert entry.issue.id not in orch._paused_issue_ids
    assert entry.issue.id not in orch._pause_reasons
    assert entry.issue.id not in orch._retry, "no retry may be scheduled"
    assert debug.rewind_count == 0
    assert debug.current_retry_attempt == 0
    assert entry.retry_attempt is None
    assert entry.issue.state == "needs_human"


def test_note_is_optional(monkeypatch, tmp_path):
    cfg = _board_config(tmp_path, note="")
    _orch_, _entry, _debug, path = _fail_turn(monkeypatch, cfg, CREDIT)
    _front, body = _card(path)
    assert body.rstrip().endswith("no automatic switch")


def test_parking_logs_the_event(monkeypatch, tmp_path):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        quota_hold.log, "warning", lambda event, **f: events.append((event, f))
    )
    cfg = _board_config(tmp_path)
    _fail_turn(monkeypatch, cfg, CREDIT)
    [(_, fields)] = [e for e in events if e[0] == "worker_quota_exhausted_state"]
    assert fields["issue_identifier"] == "MT-Q"
    assert fields["state"] == "needs_human"
    assert CREDIT in fields["error"]


def test_without_key_the_card_is_paused_as_before(monkeypatch, tmp_path):
    cfg = _board_config(tmp_path, state="")
    orch, entry, debug, path = _fail_turn(monkeypatch, cfg, CREDIT)
    front, body = _card(path)
    assert front["state"] == "In Progress"
    assert "Quota Exhausted" not in body
    assert orch.is_paused(entry.issue.id) is True
    assert "paused for operator inspection" in (debug.last_error or "")
    assert orch._dispatch_hold is None, "no key, no hold"


def test_key_helper_error_without_key_keeps_the_pause(monkeypatch, tmp_path):
    cfg = _board_config(tmp_path, state="")
    orch, entry, debug, _path = _fail_turn(monkeypatch, cfg, KEY_HELPER)
    assert orch.is_paused(entry.issue.id) is True
    assert "paused for operator inspection" in (debug.last_error or "")


def test_fallback_kinds_go_first(monkeypatch, tmp_path):
    cfg = _board_config(tmp_path, fallback_kinds=("codex", "claude"))
    pinned: list[tuple[str, str]] = []

    def _pin(_cfg, identifier, kind):
        pinned.append((identifier, kind))
        return True

    monkeypatch.setattr(Orchestrator, "_pin_fallback_agent_kind", lambda s, *a: _pin(*a))
    orch, entry, _debug, path = _fail_turn(monkeypatch, cfg, CREDIT)
    assert pinned == [("MT-Q", "claude")]
    front, _body = _card(path)
    assert front["state"] == "In Progress"
    assert entry.issue.id in orch._retry
    assert orch._dispatch_hold is None


def test_key_applies_once_no_fallback_is_left(monkeypatch, tmp_path):
    cfg = _board_config(tmp_path, fallback_kinds=("claude",))
    monkeypatch.setattr(
        Orchestrator,
        "_next_fallback_agent_kind",
        lambda self, *_a, **_k: None,
    )
    orch, entry, _debug, path = _fail_turn(monkeypatch, cfg, CREDIT)
    front, _body = _card(path)
    assert front["state"] == "needs_human"
    assert entry.issue.id not in orch._retry


def test_key_helper_error_never_goes_to_fallback_kinds(monkeypatch, tmp_path):
    cfg = _board_config(tmp_path, fallback_kinds=("codex", "claude"))
    pinned: list[str] = []
    monkeypatch.setattr(
        Orchestrator, "_pin_fallback_agent_kind", lambda *_a: pinned.append("x") or True
    )
    _orch_, _entry, _debug, path = _fail_turn(monkeypatch, cfg, KEY_HELPER)
    front, _body = _card(path)
    assert pinned == []
    assert front["state"] == "needs_human"


@pytest.mark.parametrize(
    "error", ["429 too many requests", "429 rate limit exceeded", "overloaded_error"]
)
def test_transient_errors_still_retry(monkeypatch, tmp_path, error):
    cfg = _board_config(tmp_path)
    orch, entry, _debug, path = _fail_turn(monkeypatch, cfg, error)
    front, body = _card(path)
    assert front["state"] == "In Progress"
    assert "Quota Exhausted" not in body
    assert orch.is_paused(entry.issue.id) is False
    assert entry.issue.id in orch._retry
    assert orch._dispatch_hold is None


def test_tracker_failure_falls_back_to_the_pause(monkeypatch, tmp_path):
    cfg = _board_config(tmp_path)

    def _boom(_cfg, _issue, _target):
        raise RuntimeError("tracker down")

    monkeypatch.setattr(Orchestrator, "_tracker_call_update_state", staticmethod(_boom))
    events: list[str] = []
    monkeypatch.setattr(
        quota_hold.log, "warning", lambda event, **f: events.append(event)
    )
    orch, entry, debug, _path = _fail_turn(monkeypatch, cfg, CREDIT)
    assert "worker_quota_exhausted_state_failed" in events
    assert orch.is_paused(entry.issue.id) is True
    assert "paused for operator inspection" in (debug.last_error or "")
    assert orch._dispatch_hold is None, "no hold when the card did not move"


# ---------------------------------------------------------------------------
# board hold: one card parks, the rest wait
# ---------------------------------------------------------------------------


def _tick_orchestrator(monkeypatch, cfg, candidates):
    orch = _orch()
    monkeypatch.setattr(orch._workflow_state, "reload", lambda: (cfg, None))
    monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
    dispatched: list[str] = []

    async def _fetch(_cfg):
        return [i for i in candidates if i.state.lower() == "todo"]

    async def _noop(_cfg):
        return None

    monkeypatch.setattr(core_module, "validate_for_dispatch", lambda _cfg: None)
    monkeypatch.setattr(orch, "_fetch_candidates", _fetch)
    monkeypatch.setattr(orch, "_archive_sweep", _noop)
    monkeypatch.setattr(orch, "_auto_reopen_sources_from_resolved_rcas", _noop)
    monkeypatch.setattr(
        orch,
        "_dispatch",
        lambda issue, _cfg, *, attempt, attempt_kind=None: dispatched.append(
            issue.identifier
        ),
    )
    return orch, dispatched


def _park_first_card(monkeypatch, orch, cfg):
    path = _write_card(cfg, "MT-1", "Todo")
    issue = _issue("MT-1", state="Todo")
    entry = RunningEntry(
        issue=issue,
        started_at=datetime.now(timezone.utc),
        retry_attempt=None,
        worker_task=None,  # type: ignore[arg-type]
        workspace_path=cfg.workspace_root / "MT-1",
        agent_kind="codex",
    )

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        worker_exit._handle_worker_error(
            orch, issue.id, entry, _IssueDebug(), "turn_error", CREDIT
        )

    asyncio.run(_run())
    return path


def test_board_stops_after_parking_and_resumes_when_workflow_changes(
    monkeypatch, tmp_path
):
    cfg = _board_config(tmp_path)
    cfg.workflow_path.write_text("---\nagent:\n  kind: codex\n---\nprompt\n")
    second = _issue("MT-2", state="Todo")
    orch, dispatched = _tick_orchestrator(monkeypatch, cfg, [second])
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        quota_hold.log, "warning", lambda event, **f: events.append((event, f))
    )
    monkeypatch.setattr(
        quota_hold.log, "info", lambda event, **f: events.append((event, f))
    )

    path = _park_first_card(monkeypatch, orch, cfg)
    assert _card(path)[0]["state"] == "needs_human"
    assert orch._dispatch_hold is not None
    assert "MT-1" in orch._dispatch_hold.reason

    asyncio.run(orch._on_tick())
    assert dispatched == [], "the second card must not be taken"
    held = [f for e, f in events if e == "dispatch_held_quota"]
    assert [f["issue_identifier"] for f in held] == ["MT-2"]
    assert CREDIT in held[0]["reason"]

    snapshot = orch.snapshot()
    assert snapshot["dispatch_hold"]["reason"] == orch._dispatch_hold.reason

    # Logged at most once a minute for the whole board.
    asyncio.run(orch._on_tick())
    assert len([e for e, _ in events if e == "dispatch_held_quota"]) == 1
    orch._dispatch_hold_logged_at -= quota_hold.HOLD_LOG_INTERVAL_SEC + 1
    asyncio.run(orch._on_tick())
    assert len([e for e, _ in events if e == "dispatch_held_quota"]) == 2
    assert dispatched == []

    # Same content, new mtime: still held.
    cfg.workflow_path.write_text("---\nagent:\n  kind: codex\n---\nprompt\n")
    asyncio.run(orch._on_tick())
    assert dispatched == []
    assert not [e for e, _ in events if e == "dispatch_hold_released"]

    # New content: released, the second card goes.
    cfg.workflow_path.write_text("---\nagent:\n  kind: claude\n---\nprompt\n")
    asyncio.run(orch._on_tick())
    assert [e for e, _ in events if e == "dispatch_hold_released"] == [
        "dispatch_hold_released"
    ]
    assert orch._dispatch_hold is None
    assert dispatched == ["MT-2"]
    assert orch.snapshot()["dispatch_hold"] is None


def test_without_key_the_second_card_is_taken(monkeypatch, tmp_path):
    cfg = _board_config(tmp_path, state="")
    cfg.workflow_path.write_text("---\nagent:\n  kind: codex\n---\nprompt\n")
    orch, dispatched = _tick_orchestrator(monkeypatch, cfg, [_issue("MT-2", state="Todo")])
    _park_first_card(monkeypatch, orch, cfg)
    assert orch._dispatch_hold is None
    asyncio.run(orch._on_tick())
    assert dispatched == ["MT-2"]


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def _config(tmp_path: Path, *agent_lines: str) -> ServiceConfig:
    agent = "".join(f"  {line}\n" for line in agent_lines)
    text = (
        "---\n"
        "tracker:\n"
        "  kind: file\n"
        "  board_root: ./board\n"
        "  active_states: [Todo, In Progress]\n"
        "  terminal_states: [Done, needs_human, Human Review]\n"
        "workspace:\n"
        "  root: ./ws\n"
        "agent:\n"
        "  auto_merge_on_done: false\n"
        f"{agent}"
        "---\nprompt\n"
    )
    path = tmp_path / "WORKFLOW.md"
    path.write_text(text, encoding="utf-8")
    return build_service_config(parse_workflow_text(text, path))


def test_default_is_empty(tmp_path):
    cfg = _config(tmp_path)
    assert cfg.agent.quota_exhausted_state == ""
    assert cfg.agent.quota_exhausted_note == ""


def test_state_compares_without_case_and_edge_spaces(tmp_path):
    cfg = _config(tmp_path, 'quota_exhausted_state: "  NEEDS_HUMAN "')
    assert cfg.agent.quota_exhausted_state == "needs_human"
    cfg = _config(tmp_path, "quota_exhausted_state: human review")
    assert cfg.agent.quota_exhausted_state == "Human Review"


def test_empty_state_is_the_same_as_no_key(tmp_path):
    assert _config(tmp_path, 'quota_exhausted_state: ""').agent.quota_exhausted_state == ""
    assert _config(tmp_path, 'quota_exhausted_state: "  "').agent.quota_exhausted_state == ""


@pytest.mark.parametrize("name", ["Todo", "Blocked", "nope"])
def test_state_outside_terminal_states_is_rejected(tmp_path, name):
    with pytest.raises(ConfigValidationError) as exc:
        _config(tmp_path, f"quota_exhausted_state: {name}")
    assert "agent.quota_exhausted_state" in str(exc.value)
    assert "needs_human" in str(exc.value), "the allowed names are listed"


def test_state_must_be_a_string(tmp_path):
    with pytest.raises(ConfigValidationError):
        _config(tmp_path, "quota_exhausted_state: [needs_human]")


def test_note_is_kept_multiline_and_trimmed(tmp_path):
    cfg = _config(
        tmp_path,
        "quota_exhausted_note: |",
        "  line one",
        "  line two",
    )
    assert cfg.agent.quota_exhausted_note == "line one\nline two"


def test_note_over_the_limit_is_rejected(tmp_path):
    long_note = "x" * 2001
    with pytest.raises(ConfigValidationError) as exc:
        _config(tmp_path, f"quota_exhausted_note: {long_note}")
    assert "2000" in str(exc.value)
    ok = _config(tmp_path, f"quota_exhausted_note: {'x' * 2000}")
    assert len(ok.agent.quota_exhausted_note) == 2000


def test_note_must_be_a_string(tmp_path):
    with pytest.raises(ConfigValidationError):
        _config(tmp_path, "quota_exhausted_note: [a]")
