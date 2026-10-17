"""WORKFLOW-PROGRESS.md mirror — render + observer + atomic write."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from symphony.issue import Issue
from symphony.progress_md import (
    ProgressFileWriter,
    _atomic_write_text,
    _Transition,
    render_progress_md,
)
from symphony.workflow import (
    AgentConfig,
    ClaudeConfig,
    CodexConfig,
    GeminiConfig,
    HooksConfig,
    PiConfig,
    PrimeAgentConfig,
    ProgressConfig,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
    WorkflowState,
    build_service_config,
    parse_workflow_text,
)


def _cfg(tmp_path: Path, board_root: Path | None = None) -> ServiceConfig:
    return ServiceConfig(
        workflow_path=tmp_path / "WORKFLOW.md",
        poll_interval_ms=30_000,
        workspace_root=tmp_path / "ws",
        tracker=TrackerConfig(
            kind="file",
            endpoint="",
            api_key="",
            project_slug="",
            active_states=("Todo", "In Progress", "Review", "QA"),
            terminal_states=("Done", "Cancelled"),
            board_root=board_root,
        ),
        hooks=HooksConfig(None, None, None, None, 60_000),
        agent=AgentConfig(
            kind="claude",
            max_concurrent_agents=1,
            max_turns=20,
            max_retry_backoff_ms=300_000,
            max_concurrent_agents_by_state={},
        ),
        codex=CodexConfig(
            command="codex app-server",
            approval_policy=None,
            thread_sandbox=None,
            turn_sandbox_policy=None,
            turn_timeout_ms=3_600_000,
            read_timeout_ms=5_000,
            stall_timeout_ms=300_000,
        ),
        claude=ClaudeConfig(
            command="claude -p",
            turn_timeout_ms=3_600_000,
            read_timeout_ms=5_000,
            stall_timeout_ms=300_000,
            resume_across_turns=True,
        ),
        gemini=GeminiConfig(
            command="gemini -p",
            turn_timeout_ms=3_600_000,
            read_timeout_ms=5_000,
            stall_timeout_ms=300_000,
        ),
        pi=PiConfig(
            command="pi -p",
            turn_timeout_ms=3_600_000,
            read_timeout_ms=5_000,
            stall_timeout_ms=300_000,
            resume_across_turns=True,
        ),
prime_agent=PrimeAgentConfig(
    command='prime-agent -p --mode json',
    turn_timeout_ms=3_600_000,
    read_timeout_ms=5_000,
    stall_timeout_ms=300_000,
    resume_across_turns=True,
),
        server=ServerConfig(port=None),
        progress=ProgressConfig(enabled=True, path=tmp_path / "WORKFLOW-PROGRESS.md"),
        prompt_template="hi",
    )


def _issue(identifier: str, state: str) -> Issue:
    return Issue(
        id=f"id-{identifier}",
        identifier=identifier,
        title=f"{identifier} title",
        description=None,
        priority=None,
        state=state,
    )


def test_render_groups_tickets_by_state(tmp_path):
    cfg = _cfg(tmp_path)
    issues = [
        _issue("MT-1", "Todo"),
        _issue("MT-2", "In Progress"),
        _issue("MT-3", "Done"),
    ]
    text = render_progress_md(
        cfg,
        issues,
        running_by_id={},
        retry_by_id={},
        transitions=(),
        generated_at=datetime(2026, 5, 16, 14, 22, 31, tzinfo=timezone.utc),
    )
    assert "# Symphony Progress" in text
    assert "_Updated: 2026-05-16 14:22:31 UTC_" in text
    # Active state ordering preserved.
    todo_idx = text.index("| Todo |")
    inprog_idx = text.index("| In Progress |")
    done_idx = text.index("| Done |")
    assert todo_idx < inprog_idx < done_idx
    assert "MT-1" in text and "MT-2" in text and "MT-3" in text
    # Empty lanes get an em-dash placeholder.
    assert "| Review | — |" in text
    assert "| Cancelled | — |" in text


def test_render_includes_running_meta(tmp_path):
    cfg = _cfg(tmp_path)
    now = datetime(2026, 5, 16, 14, 22, 31, tzinfo=timezone.utc)
    started = datetime(2026, 5, 16, 14, 14, 19, tzinfo=timezone.utc)
    running_by_id = {
        "MT-2": {
            "issue_identifier": "MT-2",
            "started_at": started.isoformat(),
            "tokens": {"total_tokens": 12_048},
            "attempt_kind": "initial",
            "attempt": 1,
            "paused": False,
        }
    }
    text = render_progress_md(
        cfg,
        [_issue("MT-2", "In Progress")],
        running_by_id=running_by_id,
        retry_by_id={},
        transitions=(),
        generated_at=now,
    )
    # Elapsed shown as 8m12s, tokens condensed.
    assert "MT-2 (8m12s · 12k tok)" in text


def test_render_includes_recent_transitions(tmp_path):
    cfg = _cfg(tmp_path)
    now = datetime(2026, 5, 16, 14, 22, 31, tzinfo=timezone.utc)
    transitions = (
        _Transition(at=now, identifier="MT-1", from_state="Todo", to_state="In Progress"),
        _Transition(
            at=datetime(2026, 5, 16, 14, 25, 0, tzinfo=timezone.utc),
            identifier="MT-2",
            from_state="In Progress",
            to_state="Review",
        ),
    )
    text = render_progress_md(
        cfg,
        [],
        running_by_id={},
        retry_by_id={},
        transitions=transitions,
        generated_at=now,
    )
    assert "## Recent transitions" in text
    # Newest first.
    mt1_idx = text.index("MT-1")
    mt2_idx = text.index("MT-2")
    assert mt2_idx < mt1_idx


def test_render_unknown_state_lands_in_other(tmp_path):
    cfg = _cfg(tmp_path)
    issues = [_issue("MT-9", "Triage")]
    text = render_progress_md(
        cfg,
        issues,
        running_by_id={},
        retry_by_id={},
        transitions=(),
        generated_at=datetime(2026, 5, 16, 14, 22, 31, tzinfo=timezone.utc),
    )
    assert "| _other_ |" in text
    assert "MT-9 [Triage]" in text


def test_render_includes_default_board_url(tmp_path, monkeypatch):
    """SYMPHONY_BOARD_URL unset → admin UI URL on the service port is advertised."""
    monkeypatch.delenv("SYMPHONY_BOARD_URL", raising=False)
    cfg = _cfg(tmp_path)
    text = render_progress_md(
        cfg,
        [],
        running_by_id={},
        retry_by_id={},
        transitions=(),
        generated_at=datetime(2026, 5, 16, 14, 22, 31, tzinfo=timezone.utc),
    )
    assert "_Board:_" in text
    # server.port unset in _cfg → falls back to the service default port 9999.
    assert "[http://127.0.0.1:9999/](http://127.0.0.1:9999/)" in text


def test_render_default_board_url_uses_server_port(tmp_path, monkeypatch):
    """SYMPHONY_BOARD_URL unset + server.port set → that port is advertised."""
    monkeypatch.delenv("SYMPHONY_BOARD_URL", raising=False)
    from dataclasses import replace

    cfg = replace(_cfg(tmp_path), server=ServerConfig(port=8123))
    text = render_progress_md(
        cfg,
        [],
        running_by_id={},
        retry_by_id={},
        transitions=(),
        generated_at=datetime(2026, 5, 16, 14, 22, 31, tzinfo=timezone.utc),
    )
    assert "[http://127.0.0.1:8123/](http://127.0.0.1:8123/)" in text


def test_render_board_url_can_be_disabled(tmp_path, monkeypatch):
    """SYMPHONY_BOARD_URL="" → header line is omitted."""
    monkeypatch.setenv("SYMPHONY_BOARD_URL", "")
    cfg = _cfg(tmp_path)
    text = render_progress_md(
        cfg,
        [],
        running_by_id={},
        retry_by_id={},
        transitions=(),
        generated_at=datetime(2026, 5, 16, 14, 22, 31, tzinfo=timezone.utc),
    )
    assert "_Board:_" not in text


def test_render_explicit_board_url_overrides_env(tmp_path, monkeypatch):
    """Explicit `board_url=` argument wins over env."""
    monkeypatch.setenv("SYMPHONY_BOARD_URL", "http://example.invalid/from-env")
    cfg = _cfg(tmp_path)
    text = render_progress_md(
        cfg,
        [],
        running_by_id={},
        retry_by_id={},
        transitions=(),
        generated_at=datetime(2026, 5, 16, 14, 22, 31, tzinfo=timezone.utc),
        board_url="https://board.example/",
    )
    assert "[https://board.example/](https://board.example/)" in text
    assert "example.invalid" not in text


def test_atomic_write_replaces_existing(tmp_path):
    target = tmp_path / "progress.md"
    target.write_text("OLD")
    _atomic_write_text(target, "NEW\n")
    assert target.read_text() == "NEW\n"
    # No leftover .tmp- siblings.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


def _board_root_with(tmp_path: Path, tickets: list[tuple[str, str]]) -> Path:
    """Materialise a file-tracker board with the given (identifier, state) pairs."""
    root = tmp_path / "board"
    root.mkdir()
    for ident, state in tickets:
        (root / f"{ident}.md").write_text(
            f"---\nidentifier: {ident}\ntitle: {ident}\nstate: {state}\n---\n"
        )
    return root


def test_observer_writes_file_and_detects_transitions(tmp_path):
    board = _board_root_with(tmp_path, [("MT-1", "Todo"), ("MT-2", "In Progress")])
    cfg = _cfg(tmp_path, board_root=board)

    workflow_state = WorkflowState(cfg.workflow_path)
    workflow_state._config = cfg  # bypass disk reload — inject directly.

    class _StubOrch:
        def __init__(self) -> None:
            self.observers: list = []

        def add_observer(self, cb) -> None:
            self.observers.append(cb)

        def snapshot(self) -> dict:
            return {"running": [], "retrying": []}

    orch = _StubOrch()
    target = tmp_path / "PROGRESS.md"
    writer = ProgressFileWriter(orch, workflow_state, target, max_transitions=10)
    writer.register()
    assert orch.observers == [writer.on_change]

    asyncio.run(writer.on_change())
    first = target.read_text()
    assert "MT-1" in first and "MT-2" in first
    # First render establishes baseline only — no transition log entries.
    assert "## Recent transitions" not in first

    # Move MT-1 Todo → In Progress; add MT-3 fresh.
    (board / "MT-1.md").write_text(
        "---\nidentifier: MT-1\ntitle: MT-1\nstate: In Progress\n---\n"
    )
    (board / "MT-3.md").write_text(
        "---\nidentifier: MT-3\ntitle: MT-3\nstate: Todo\n---\n"
    )
    asyncio.run(writer.on_change())
    second = target.read_text()
    assert "## Recent transitions" in second
    assert "**MT-1**  Todo → In Progress" in second
    # Newly-appeared ticket gets logged as "(new) → state".
    assert "**MT-3**  (new) → Todo" in second

    # Remove MT-2; expect a "(removed)" line on the next render.
    (board / "MT-2.md").unlink()
    asyncio.run(writer.on_change())
    third = target.read_text()
    assert "**MT-2**  In Progress → (removed)" in third


def test_workflow_progress_block_parses(tmp_path):
    """`progress:` block in WORKFLOW.md flows through to ServiceConfig."""
    text = (
        "---\n"
        "tracker:\n"
        "  kind: file\n"
        "agent:\n"
        "  kind: claude\n"
        "progress:\n"
        "  enabled: false\n"
        "  path: docs/STATUS.md\n"
        "  max_transitions: 5\n"
        "---\n"
        "prompt body\n"
    )
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(text)
    wf = parse_workflow_text(text, workflow_path)
    cfg = build_service_config(wf)
    assert cfg.progress.enabled is False
    assert cfg.progress.path is not None
    assert cfg.progress.path.name == "STATUS.md"
    assert cfg.progress.max_transitions == 5


def test_workflow_progress_defaults_when_omitted(tmp_path):
    text = (
        "---\n"
        "tracker:\n"
        "  kind: file\n"
        "agent:\n"
        "  kind: claude\n"
        "---\n"
        "prompt body\n"
    )
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(text)
    wf = parse_workflow_text(text, workflow_path)
    cfg = build_service_config(wf)
    assert cfg.progress.enabled is True
    assert cfg.progress.path is not None
    assert cfg.progress.path.name == "WORKFLOW-PROGRESS.md"
    assert cfg.progress.max_transitions == 20


def test_workflow_progress_rejects_non_bool_enabled(tmp_path):
    text = (
        "---\n"
        "tracker:\n"
        "  kind: file\n"
        "agent:\n"
        "  kind: claude\n"
        "progress:\n"
        "  enabled: 1\n"
        "---\n"
        "prompt body\n"
    )
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(text)
    wf = parse_workflow_text(text, workflow_path)
    with pytest.raises(Exception, match="progress.enabled"):
        build_service_config(wf)


@pytest.mark.parametrize("env_url", [None, "", "https://board.example/custom"])
async def test_observer_keeps_runtime_port_across_reload_and_preserves_url_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_url: str | None,
) -> None:
    if env_url is None:
        monkeypatch.delenv("SYMPHONY_BOARD_URL", raising=False)
    else:
        monkeypatch.setenv("SYMPHONY_BOARD_URL", env_url)
    workflow = tmp_path / "WORKFLOW.md"
    text = "---\ntracker:\n  kind: file\n  board_root: ./board\nserver:\n  port: 9999\n---\nPrompt\n"
    workflow.write_text(text, encoding="utf-8")
    (tmp_path / "board").mkdir()
    state = WorkflowState(workflow)
    cfg, error = state.reload()
    assert error is None and cfg is not None

    class StubOrchestrator:
        def snapshot(self):
            return {"running": [], "retrying": []}

    target = tmp_path / "PROGRESS.md"
    writer = ProgressFileWriter(StubOrchestrator(), state, target)
    writer.set_server_port(52363)
    await writer.on_change()
    assert state.current() is cfg and cfg.server.port == 9999
    workflow.write_text(text.replace("9999", "8090"), encoding="utf-8")
    reloaded, error = state.reload()
    assert error is None and reloaded is not None
    await writer.on_change()
    assert state.current() is reloaded and reloaded.server.port == 8090
    output = target.read_text(encoding="utf-8")
    if env_url == "":
        assert "_Board:_" not in output
    else:
        expected = env_url or "http://127.0.0.1:52363/"
        assert f"[{expected}]({expected})" in output
    assert "http://127.0.0.1:8090/" not in output
