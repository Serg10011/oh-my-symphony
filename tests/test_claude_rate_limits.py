"""`rate_limit_event` frames from claude reach `latest_rate_limits` and `/api/v1/state`.

The frame shape below was captured from a real `claude -p --output-format
stream-json --verbose` run: `rate_limit_info` is stored verbatim, nothing is
parsed or rescaled.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from symphony.backends import BackendInit
from symphony.backends.claude_code import ClaudeCodeBackend
from symphony.backends.codex import NOTIF_RATE_LIMITS, CodexAppServerBackend
from symphony.issue import Issue
from symphony.orchestrator import Orchestrator, RunningEntry
from symphony.server import build_app
from symphony.workflow import WorkflowState

from tests.test_backends import _make_cfg

RATE_LIMIT_INFO: dict[str, Any] = {
    "status": "allowed",
    "resetsAt": 1792000000,
    "rateLimitType": "five_hour",
    "overageStatus": "rejected",
    "overageDisabledReason": "org_level_disabled",
    "isUsingOverage": False,
    "unifiedWindows": {
        "five_hour": {"utilization": 0.15, "resetsAt": 1792000000},
        "seven_day": {"utilization": 0.04, "resetsAt": 1792400000},
    },
}


def _frame(info: Any = RATE_LIMIT_INFO) -> dict[str, Any]:
    return {
        "type": "rate_limit_event",
        "rate_limit_info": copy.deepcopy(info),
        "uuid": "u-1",
        "session_id": "s-1",
    }


def _claude(tmp_path: Path, events: list[dict[str, Any]]) -> ClaudeCodeBackend:
    async def on_event(event: dict[str, Any]) -> None:
        events.append(event)

    cwd = tmp_path / "ws"
    cwd.mkdir()
    return ClaudeCodeBackend(
        BackendInit(
            cfg=_make_cfg("claude", workspace_root=tmp_path),
            cwd=cwd,
            workspace_root=tmp_path,
            on_event=on_event,
        )
    )


async def test_rate_limit_event_is_stored_verbatim_and_still_emitted(
    tmp_path: Path,
) -> None:
    events: list[dict[str, Any]] = []
    backend = _claude(tmp_path, events)
    assert backend.latest_rate_limits is None

    assert await backend._handle_stream_event(_frame()) is None

    assert backend.latest_rate_limits == RATE_LIMIT_INFO
    # The frame is still reported as before, and its envelope already
    # carries the value (what the orchestrator reads).
    assert len(events) == 1
    assert events[0]["event"] == "other_message"
    assert events[0]["payload"]["type"] == "rate_limit_event"
    assert events[0]["rate_limits"] == RATE_LIMIT_INFO


async def test_newer_rate_limit_frame_replaces_the_value(tmp_path: Path) -> None:
    backend = _claude(tmp_path, [])
    await backend._handle_stream_event(_frame())
    newer = copy.deepcopy(RATE_LIMIT_INFO)
    newer["unifiedWindows"]["five_hour"]["utilization"] = 0.2
    await backend._handle_stream_event(_frame(newer))
    assert backend.latest_rate_limits == newer


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "rate_limit_event"},
        {"type": "rate_limit_event", "rate_limit_info": None},
        {"type": "rate_limit_event", "rate_limit_info": "allowed"},
        {"type": "rate_limit_event", "rate_limit_info": ["allowed"]},
        {"type": "rate_limit_event", "rate_limit_info": 0.15},
    ],
)
async def test_malformed_rate_limit_frame_keeps_previous_value(
    tmp_path: Path, frame: dict[str, Any]
) -> None:
    events: list[dict[str, Any]] = []
    backend = _claude(tmp_path, events)
    await backend._handle_stream_event(_frame())
    await backend._handle_stream_event(frame)
    assert backend.latest_rate_limits == RATE_LIMIT_INFO
    assert len(events) == 2  # the odd frame is still reported, not dropped


async def test_malformed_rate_limit_frame_on_fresh_backend_stays_empty(
    tmp_path: Path,
) -> None:
    backend = _claude(tmp_path, [])
    await backend._handle_stream_event({"type": "rate_limit_event"})
    assert backend.latest_rate_limits is None


async def test_other_frames_do_not_touch_rate_limits(tmp_path: Path) -> None:
    backend = _claude(tmp_path, [])
    await backend._handle_stream_event(_frame())
    await backend._handle_stream_event({"type": "assistant", "message": {}})
    await backend._handle_stream_event({"type": "stream_event"})
    await backend._handle_stream_event(
        {"type": "result", "subtype": "success", "usage": {"output_tokens": 3}}
    )
    assert backend.latest_rate_limits == RATE_LIMIT_INFO


def _orchestrator_with_running_entry() -> tuple[Orchestrator, Issue]:
    orch = Orchestrator(WorkflowState(Path("/tmp/no.md")))
    issue = Issue(
        id="id-MT-1",
        identifier="MT-1",
        title="MT-1 title",
        description=None,
        priority=2,
        state="In Progress",
        labels=(),
        blocked_by=(),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=None,
    )
    orch._running[issue.id] = RunningEntry(
        issue=issue,
        started_at=datetime.now(timezone.utc),
        retry_attempt=None,
        worker_task=None,  # type: ignore[arg-type]
        workspace_path=Path("/tmp"),
    )
    return orch, issue


async def test_claude_rate_limits_reach_api_v1_state(tmp_path: Path) -> None:
    orch, issue = _orchestrator_with_running_entry()

    async def forward(event: dict[str, Any]) -> None:
        await orch._on_codex_event(issue.id, event)

    cwd = tmp_path / "ws"
    cwd.mkdir()
    backend = ClaudeCodeBackend(
        BackendInit(
            cfg=_make_cfg("claude", workspace_root=tmp_path),
            cwd=cwd,
            workspace_root=tmp_path,
            on_event=forward,
        )
    )

    async with TestClient(TestServer(build_app(orch))) as client:
        before = await (await client.get("/api/v1/state")).json()
        assert before["rate_limits"] is None

        await backend._handle_stream_event(_frame())

        resp = await client.get("/api/v1/state")
        assert resp.status == 200
        assert (await resp.json())["rate_limits"] == RATE_LIMIT_INFO


async def test_codex_rate_limits_are_unaffected(tmp_path: Path) -> None:
    cwd = tmp_path / "ws"
    cwd.mkdir()

    async def noop(_: dict[str, Any]) -> None:
        return None

    codex = CodexAppServerBackend(
        BackendInit(
            cfg=_make_cfg("codex", workspace_root=tmp_path),
            cwd=cwd,
            workspace_root=tmp_path,
            on_event=noop,
        )
    )
    assert codex.latest_rate_limits is None
    codex_limits = {"primary": {"usedPercent": 12}, "secondary": {"usedPercent": 3}}
    await codex._handle_notification(
        {"method": NOTIF_RATE_LIMITS, "params": {"rateLimits": codex_limits}}
    )
    assert codex.latest_rate_limits == codex_limits

    # A claude-style frame is not a codex notification: the stored value stays.
    await codex._handle_notification(_frame())
    assert codex.latest_rate_limits == codex_limits
