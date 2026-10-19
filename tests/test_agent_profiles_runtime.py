"""Configured plain-CLI profiles remain selectable at runtime boundaries."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient

from symphony.chat import ChatManager
from symphony.errors import ConfigValidationError, SymphonyError
from symphony.orchestrator import _config_for_issue_agent
from symphony.trackers.file import FileBoardTracker
from symphony.workflow import SUPPORTED_AGENT_KINDS, ServiceConfig, WorkflowState
from tests.test_agent_profiles import _ECHO_PROFILE, _cfg
from tests.test_chat import _FakeBackend, _wait_turn
from tests.test_chat import fake_backends as fake_backends
from tests.test_orchestrator_dispatch import _issue, _orch
from tests.test_webapi import board_dir as board_dir
from tests.test_webapi import client as client


def _profile_cfg(tmp_path: Path, *, default: str = "echo-cli") -> ServiceConfig:
    return _cfg(tmp_path, _ECHO_PROFILE.replace("kind: echo-cli", f"kind: {default}"))


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit"])
async def test_chat_starts_configured_profile(
    tmp_path: Path, fake_backends: list[_FakeBackend], explicit: bool
) -> None:
    cfg = _profile_cfg(tmp_path, default="codex" if explicit else "echo-cli")
    manager = ChatManager(lambda: cfg)
    try:
        snapshot = await manager.start_session(
            "qa", agent_kind="echo-cli" if explicit else None
        )
        assert snapshot["agent_kind"] == "echo-cli"
        assert snapshot["mode_enforced"] is False
        assert fake_backends[-1].init.cfg.agent.kind == "echo-cli"
        assert (
            fake_backends[-1].init.cfg.agent_profile("echo-cli")
            == cfg.agent.profiles["echo-cli"]
        )
        assert manager.list_sessions()["supported_agent_kinds"] == sorted(
            SUPPORTED_AGENT_KINDS | {"echo-cli"}
        )
    finally:
        await manager.close()


async def test_chat_reattach_uses_configured_profile_and_fresh_turn(
    tmp_path: Path, fake_backends: list[_FakeBackend]
) -> None:
    cfg = _profile_cfg(tmp_path, default="codex")
    manager = ChatManager(lambda: cfg)
    try:
        session_id = (await manager.start_session("qa"))["session_id"]
        await manager.send_message("remember this", session_id)
        await _wait_turn(manager, session_id)
    finally:
        await manager.close()

    cfg = replace(cfg, agent=replace(cfg.agent, kind="echo-cli"))
    restarted = ChatManager(lambda: cfg)
    try:
        snapshot = await restarted.reattach(session_id)
        assert snapshot["agent_kind"] == "echo-cli"
        assert snapshot["mode"] == "qa"
        assert any(row["text"] == "remember this" for row in snapshot["transcript_tail"])
        assert fake_backends[-1].init.cfg.agent.kind == "echo-cli"
        await restarted.send_message("second", session_id)
        await _wait_turn(restarted, session_id)
        assert fake_backends[-1].turns[0][1] is False
    finally:
        await restarted.close()


async def test_chat_rejects_unknown_profile_before_backend_launch(
    tmp_path: Path, fake_backends: list[_FakeBackend]
) -> None:
    cfg = _profile_cfg(tmp_path)
    manager = ChatManager(lambda: cfg)
    try:
        with pytest.raises(
            SymphonyError, match="unsupported agent kind 'missing-cli'"
        ) as excinfo:
            await manager.start_session("qa", agent_kind="missing-cli")
        assert "echo-cli" in str(excinfo.value)
        assert "codex" in str(excinfo.value)
        assert fake_backends == []
        assert manager.live_count == 0
    finally:
        await manager.close()


def test_profile_ticket_pin_is_eligible_and_overrides_stage_kind(tmp_path: Path) -> None:
    cfg = _profile_cfg(tmp_path, default="codex")
    cfg = replace(cfg, agent=replace(cfg.agent, stage_kinds={"todo": "claude"}))
    issue = replace(_issue("PROFILE-1"), agent_kind=" Echo-CLI ")
    resolved = _config_for_issue_agent(cfg, issue)
    assert resolved.agent.kind == "echo-cli"
    assert resolved.agent_profile("echo-cli") == cfg.agent.profiles["echo-cli"]
    assert cfg.agent.kind == "codex"
    orch = _orch()
    assert orch._eligible(issue, cfg, owning_retry=False) is True
    assert orch._should_dispatch(issue, cfg) is True


def test_unknown_ticket_pin_is_rejected_with_profiles_configured(tmp_path: Path) -> None:
    cfg = _profile_cfg(tmp_path)
    issue = replace(_issue("UNKNOWN-1"), agent_kind="missing-cli")
    with pytest.raises(
        ConfigValidationError, match="ticket agent.kind must be one of"
    ) as excinfo:
        _config_for_issue_agent(cfg, issue)
    assert "echo-cli" in str(excinfo.value)
    assert "codex" in str(excinfo.value)
    decision = _orch()._eligibility_decision(issue, cfg, owning_retry=False)
    assert decision.disposition.value == "reject"
    assert decision.code == "unsupported_agent"


@pytest.mark.parametrize("explicit", [False, True], ids=["source-pin", "explicit"])
async def test_blocked_recovery_persists_profile_on_fix_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    cfg = _profile_cfg(tmp_path, default="codex")
    cfg = replace(
        cfg,
        tracker=replace(
            cfg.tracker,
            active_states=("Todo", "Doing"),
            terminal_states=("Done", "Blocked"),
        ),
    )
    tracker = FileBoardTracker(cfg.tracker)
    tracker.create(
        identifier="PROFILE-BLOCKED",
        title="profile recovery",
        state="Blocked",
        agent_kind="codex" if explicit else "echo-cli",
    )
    orch = _orch()
    monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)

    changed, _message, details = await orch.recover_blocked_issue(
        "PROFILE-BLOCKED", agent_kind="echo-cli" if explicit else None
    )

    assert changed is True
    assert details["agent_kind"] == "echo-cli"
    fix = tracker.fetch_issue_full_by_id(details["fix_identifier"])
    assert fix is not None and fix.agent_kind == "echo-cli"
    source = tracker.fetch_issue_full_by_id("PROFILE-BLOCKED")
    assert source is not None and source.state == "Blocked"
    assert [blocker.identifier for blocker in source.blocked_by] == [fix.identifier]


@pytest.fixture()
def profile_client(client: TestClient, board_dir: Path) -> TestClient:
    path = board_dir / "WORKFLOW.md"
    text = path.read_text(encoding="utf-8").replace(
        "agent:\n  kind: claude\n",
        "agent:\n  kind: claude\n  profiles:\n    echo-cli: {command: echo-cli}\n",
    )
    path.write_text(text, encoding="utf-8")
    state: WorkflowState = client.stub.workflow_state  # type: ignore[attr-defined]
    cfg, err = state.reload()
    assert err is None and cfg is not None
    assert "echo-cli" in cfg.agent.profiles
    return client


async def test_webapi_lists_and_persists_configured_profile(profile_client: TestClient) -> None:
    board = await (await profile_client.get("/api/v1/board")).json()
    workflow = await (await profile_client.get("/api/v1/workflow")).json()
    assert "echo-cli" in board["board"]["agent_kinds"]
    assert "echo-cli" in workflow["agent_kinds"]
    assert "codex" in board["board"]["agent_kinds"]

    created = await profile_client.post(
        "/api/v1/issues", json={"title": "profile work", "agent_kind": " Echo-CLI "}
    )
    assert created.status == 201
    identifier = (await created.json())["identifier"]
    detail = await (await profile_client.get(f"/api/v1/issues/{identifier}")).json()
    assert detail["agent_kind"] == "echo-cli"

    patched = await profile_client.patch(
        "/api/v1/issues/SEED-1", json={"agent_kind": "echo-cli"}
    )
    assert patched.status == 200
    detail = await (await profile_client.get("/api/v1/issues/SEED-1")).json()
    assert detail["agent_kind"] == "echo-cli"


@pytest.mark.parametrize("route", ["recover", "chat", "ci"])
async def test_webapi_runtime_routes_accept_profile(
    profile_client: TestClient, fake_backends: list[_FakeBackend], route: str
) -> None:
    if route == "recover":
        response = await profile_client.post(
            "/api/v1/issues/SEED-1/recover-blocked", json={"agent_kind": "echo-cli"}
        )
        assert response.status == 200
        assert (await response.json())["agent_kind"] == "echo-cli"
        assert profile_client.stub.recover_calls[-1]["agent_kind"] == "echo-cli"  # type: ignore[attr-defined]
    elif route == "chat":
        response = await profile_client.post(
            "/api/v1/chat/sessions", json={"mode": "qa", "agent_kind": "echo-cli"}
        )
        assert response.status == 201
        assert (await response.json())["agent_kind"] == "echo-cli"
        assert fake_backends[-1].init.cfg.agent.kind == "echo-cli"
        listing = await (await profile_client.get("/api/v1/chat/sessions")).json()
        assert "echo-cli" in listing["supported_agent_kinds"]
    else:
        response = await profile_client.put(
            "/api/v1/workflow/continuous-improvement", json={"agent_kind": "echo-cli"}
        )
        assert response.status == 200
        assert (await response.json())["continuous_improvement"]["agent_kind"] == "echo-cli"
        workflow = await (await profile_client.get("/api/v1/workflow")).json()
        assert workflow["continuous_improvement"]["agent_kind"] == "echo-cli"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/issues", {"title": "bad", "agent_kind": "missing-cli"}),
        ("patch", "/api/v1/issues/SEED-1", {"agent_kind": "missing-cli"}),
        ("post", "/api/v1/issues/SEED-1/recover-blocked", {"agent_kind": "missing-cli"}),
        ("post", "/api/v1/chat/sessions", {"agent_kind": "missing-cli"}),
        ("put", "/api/v1/workflow/continuous-improvement", {"agent_kind": "missing-cli"}),
    ],
)
async def test_webapi_rejects_unknown_kind_with_profile_configured(
    profile_client: TestClient, method: str, path: str, body: dict[str, str]
) -> None:
    response = await getattr(profile_client, method)(path, json=body)
    assert response.status == 400
    message = (await response.json())["error"]["message"]
    assert "unknown agent_kind 'missing-cli'" in message
    assert "echo-cli" in message
    assert "codex" in message
