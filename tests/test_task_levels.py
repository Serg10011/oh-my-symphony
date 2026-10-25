"""Task levels: `levels` block, `level:` frontmatter, per-ticket model/effort/limits."""

from __future__ import annotations

import asyncio
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from symphony.errors import ConfigValidationError
from symphony.issue import Issue
from symphony.orchestrator import _config_for_issue_agent
from symphony.orchestrator.core import Orchestrator
from symphony.trackers.file import FileBoardTracker, issue_from_file
from symphony.workflow import ServiceConfig, build_service_config, parse_workflow_text
from symphony.workflow.levels import (
    LevelError,
    inject_claude_flags,
    turn_fields,
)
from tests.test_orchestrator_dispatch import _issue, _make_config, _orch

ACTIVE = ("Todo", "In Progress", "Verify", "Document")

LEVELS = """
levels:
  L1:
    claude: {model: haiku, effort: medium}
  S1:
    claude: {model: sonnet, effort: high}
    codex: {model: gpt-x, effort: low}
  LOCAL:
    max_turns: 40
    max_state_turns: 9
agent:
  default_level: L1
"""


def _cfg(tmp_path: Path, extra: str = LEVELS, active: tuple[str, ...] = ACTIVE) -> ServiceConfig:
    head = (
        "tracker:\n  kind: file\n  board_root: ./board\n"
        f"  active_states: [{', '.join(active)}]\n  terminal_states: [Done, Blocked]\n"
    )
    text = "---\n" + head + textwrap.dedent(extra).lstrip() + "---\nbody\n"
    return build_service_config(parse_workflow_text(text, tmp_path / "WORKFLOW.md"))


def _leveled(**agent_overrides) -> ServiceConfig:
    """A claude-kind config with levels, built without a workflow file."""
    from symphony.workflow.levels import BackendLevel, LevelConfig

    cfg = _make_config(active_states=ACTIVE)
    levels = {
        "L1": LevelConfig(claude=BackendLevel("haiku", "medium")),
        "S1": LevelConfig(
            claude=BackendLevel("sonnet", "high"), codex=BackendLevel("gpt-x", "low")
        ),
        "LOCAL": LevelConfig(max_turns=40, max_state_turns=9),
    }
    agent = replace(cfg.agent, kind="claude", **agent_overrides)
    return replace(cfg, agent=agent, levels=levels)


def _with_level(identifier: str, level: str | None, state: str = "Todo") -> Issue:
    return replace(_issue(identifier, state=state), level=level)


# ---------------------------------------------------------------- parsing


def test_levels_parse(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert set(cfg.levels) == {"L1", "S1", "LOCAL"}
    assert cfg.levels["L1"].claude.model == "haiku"
    assert cfg.levels["L1"].claude.effort == "medium"
    assert cfg.levels["S1"].codex.model == "gpt-x"
    assert cfg.levels["LOCAL"].max_turns == 40
    assert cfg.levels["LOCAL"].max_state_turns == 9
    assert cfg.agent.default_level == "L1"


def test_no_levels_block_changes_nothing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, "agent: {kind: claude}\n")
    assert cfg.levels == {} and cfg.agent.default_level == ""
    issue = _with_level("T-1", None)
    assert _config_for_issue_agent(cfg, issue) is cfg


@pytest.mark.parametrize(
    "block, fragment",
    [
        ("levels: [L1]\n", "levels must be a map"),
        ("levels:\n  'bad name': {}\n", "level name"),
        ("levels:\n  'L.1': {}\n", "level name"),
        ("levels:\n  L1: 5\n", "must be a map"),
        ("levels:\n  L1: {color: red}\n", "unknown key"),
        ("levels:\n  L1: {gemini: {model: x}}\n", "claude and codex"),
        ("levels:\n  L1: {claude: {modle: x}}\n", "unknown key"),
        ("levels:\n  L1: {claude: {model: ''}}\n", "non-empty string"),
        ("levels:\n  L1: {claude: {model: 5}}\n", "non-empty string"),
        ("levels:\n  L1: {claude: {effort: ultra}}\n", "effort must be one of"),
        ("levels:\n  L1: {claude: {effort: ''}}\n", "non-empty string"),
        ("levels:\n  L1: {claude: null}\n", "must be a map"),
        ("levels:\n  L1: {max_turns: 0}\n", "integer >= 1"),
        ("levels:\n  L1: {max_turns: true}\n", "integer >= 1"),
        ("levels:\n  L1: {max_turns: 3}\n", "cannot cover 4 active states"),
        ("levels:\n  L1: {max_state_turns: 0}\n", "integer >= 1"),
        ("levels:\n  L1: {max_state_turns: '2'}\n", "integer >= 1"),
        ("levels:\n  L1: {}\nagent: {default_level: ''}\n", "non-empty string"),
        ("levels:\n  L1: {}\nagent: {default_level: nope}\n", "not defined in levels"),
        ("agent: {default_level: L1}\n", "not defined in levels"),
    ],
)
def test_levels_reject_bad_config(tmp_path: Path, block: str, fragment: str) -> None:
    with pytest.raises(ConfigValidationError, match=fragment):
        _cfg(tmp_path, block)


def test_claude_effort_accepts_every_cli_value(tmp_path: Path) -> None:
    for effort in ("low", "medium", "high", "xhigh", "max"):
        cfg = _cfg(tmp_path, f"levels:\n  X: {{claude: {{effort: {effort}}}}}\n")
        assert cfg.levels["X"].claude.effort == effort


def test_level_max_turns_equal_to_active_states_is_allowed(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, "levels:\n  X: {max_turns: 4}\n")
    assert cfg.levels["X"].max_turns == 4


# ----------------------------------------------------------- flag injection


def test_inject_after_program_and_before_other_flags() -> None:
    out = inject_claude_flags("claude -p --verbose", "sonnet", "high")
    assert out == "claude --model sonnet --effort high -p --verbose"


def test_inject_replaces_existing_flags_without_duplicates() -> None:
    out = inject_claude_flags(
        "claude --model opus -p --effort=max --verbose", "haiku", "medium"
    )
    assert out.split() == "claude --model haiku --effort medium -p --verbose".split()
    assert out.count("--model") == 1 and out.count("--effort") == 1


def test_inject_only_touches_flags_that_are_given() -> None:
    out = inject_claude_flags("claude --effort low -p", "haiku", None)
    assert out == "claude --model haiku --effort low -p"


def test_inject_replaces_quoted_value_and_quotes_new_one() -> None:
    out = inject_claude_flags("claude --model 'a b' -p", "x y", None)
    assert out == "claude --model 'x y' -p"


def test_inject_keeps_leading_whitespace_and_pipelines() -> None:
    out = inject_claude_flags("  claude -p | tee out.log", "haiku", None)
    assert out == "  claude --model haiku -p | tee out.log"


def test_inject_after_an_absolute_program_path() -> None:
    # The kit writes the absolute path of claude into claude.command.
    out = inject_claude_flags("/opt/bin/claude -p --verbose", "haiku", "low")
    assert out == "/opt/bin/claude --model haiku --effort low -p --verbose"
    quoted = inject_claude_flags("'/my tools/claude' -p", "haiku", None)
    assert quoted == "'/my tools/claude' --model haiku -p"


def test_inject_refuses_wrapper_commands() -> None:
    assert inject_claude_flags("./wrap.sh -p", "haiku", None) is None
    assert inject_claude_flags("claudette -p", "haiku", None) is None


def test_inject_noop_without_values() -> None:
    assert inject_claude_flags("claude -p", None, None) == "claude -p"


# --------------------------------------------------- per-ticket config


def test_card_level_sets_claude_model_and_effort() -> None:
    cfg = _config_for_issue_agent(_leveled(), _with_level("T-1", "S1"))
    assert cfg.claude.command.startswith("claude --model sonnet --effort high ")


def test_default_level_applies_without_card_level() -> None:
    cfg = _config_for_issue_agent(_leveled(default_level="L1"), _with_level("T-1", None))
    assert cfg.claude.command.startswith("claude --model haiku --effort medium ")


def test_card_level_beats_default_level() -> None:
    cfg = _config_for_issue_agent(_leveled(default_level="L1"), _with_level("T-1", "S1"))
    assert "--model sonnet" in cfg.claude.command and "haiku" not in cfg.claude.command


def test_no_level_and_no_default_keeps_command() -> None:
    base = _leveled()
    cfg = _config_for_issue_agent(base, _with_level("T-1", None))
    assert cfg.claude.command == base.claude.command


def test_codex_level_sets_model_and_reasoning_effort() -> None:
    base = replace(_leveled(), agent=replace(_leveled().agent, kind="codex"))
    cfg = _config_for_issue_agent(base, _with_level("T-1", "S1"))
    assert cfg.codex.model == "gpt-x" and cfg.codex.reasoning_effort == "low"
    assert cfg.claude.command.startswith("claude --model sonnet")


def test_level_without_backend_settings_leaves_commands_alone() -> None:
    base = _leveled()
    cfg = _config_for_issue_agent(base, _with_level("T-1", "LOCAL"))
    assert cfg.claude.command == base.claude.command
    assert cfg.codex == base.codex


def test_unknown_level_is_an_error_naming_the_known_ones() -> None:
    with pytest.raises(LevelError, match=r"level 'S9'.*\['L1', 'LOCAL', 'S1'\]"):
        _config_for_issue_agent(_leveled(), _with_level("T-1", "S9"))


def test_unknown_level_without_any_levels_block_is_an_error() -> None:
    base = replace(_leveled(), levels={})
    with pytest.raises(LevelError):
        _config_for_issue_agent(base, _with_level("T-1", "L1"))


def test_wrapper_command_with_claude_level_is_an_error() -> None:
    base = _leveled()
    base = replace(base, claude=replace(base.claude, command="./wrap.sh -p"))
    with pytest.raises(LevelError, match="wrapper"):
        _config_for_issue_agent(base, _with_level("T-1", "S1"))


def test_wrapper_command_is_fine_for_a_codex_ticket() -> None:
    base = _leveled()
    base = replace(
        base,
        agent=replace(base.agent, kind="codex"),
        claude=replace(base.claude, command="./wrap.sh -p"),
    )
    cfg = _config_for_issue_agent(base, _with_level("T-1", "S1"))
    assert cfg.claude.command == "./wrap.sh -p"
    assert cfg.codex.model == "gpt-x"


def test_service_model_env_is_only_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", "haiku")
    on_level = turn_fields(
        _config_for_issue_agent(_leveled(), _with_level("T-1", "S1")),
        _with_level("T-1", "S1"),
    )
    assert on_level["model"] == "sonnet"
    no_level = turn_fields(_leveled(), _with_level("T-2", None))
    assert no_level["model"] == "haiku" and no_level["task_level"] is None


# ------------------------------------------------------------ level limits


def test_level_limits_replace_global_limits() -> None:
    base = _leveled(max_turns=8, max_state_turns=2)
    plain = _config_for_issue_agent(base, _with_level("T-1", "S1"))
    local = _config_for_issue_agent(base, _with_level("T-2", "LOCAL"))
    assert (plain.agent.max_turns, plain.agent.max_state_turns) == (8, 2)
    assert (local.agent.max_turns, local.agent.max_state_turns) == (40, 9)
    orch = _orch()
    assert orch._max_state_turns_for_state(local, "In Progress") == 9
    assert orch._max_state_turns_for_state(plain, "In Progress") == 2


def test_per_state_limit_still_beats_level_limit() -> None:
    base = _leveled(max_state_turns_by_state={"verify": 1})
    local = _config_for_issue_agent(base, _with_level("T-1", "LOCAL"))
    orch = _orch()
    assert orch._max_state_turns_for_state(local, "Verify") == 1
    assert orch._max_state_turns_for_state(local, "In Progress") == 9


def test_shared_limits_are_not_touched_by_a_level() -> None:
    base = _leveled(max_total_turns=20, max_attempts=2)
    local = _config_for_issue_agent(base, _with_level("T-1", "LOCAL"))
    assert (local.agent.max_total_turns, local.agent.max_attempts) == (20, 2)


# ------------------------------------------------------------ turn journal


def test_turn_fields_for_claude_codex_and_no_level() -> None:
    claude_issue = _with_level("T-1", "S1")
    cfg = _config_for_issue_agent(_leveled(), claude_issue)
    assert turn_fields(cfg, claude_issue) == {
        "task_level": "S1",
        "model": "sonnet",
        "effort": "high",
    }
    codex_cfg = replace(_leveled(), agent=replace(_leveled().agent, kind="codex"))
    codex_cfg = _config_for_issue_agent(codex_cfg, claude_issue)
    assert turn_fields(codex_cfg, claude_issue) == {
        "task_level": "S1",
        "model": "gpt-x",
        "effort": "low",
    }


def test_turn_started_run_event_keeps_level_model_and_effort() -> None:
    from symphony.orchestrator.diagnostics import normalize_event_payload

    payload = normalize_event_payload(
        "turn_started",
        {"turn": 2, "state": "Verify", "continuation": False,
         "task_level": "S1", "model": "sonnet", "effort": "high", "extra": "dropped"},
    )
    assert payload == {
        "turn": 2, "state": "Verify", "continuation": False,
        "task_level": "S1", "model": "sonnet", "effort": "high",
    }
    # A card without a level (and without default_level) records no level key.
    bare = normalize_event_payload("turn_started", {"turn": 1, "task_level": None})
    assert bare == {"turn": 1}


# --------------------------------------------------- tracker (frontmatter)


def _write_card(path: Path, level_line: str) -> None:
    path.write_text(
        "---\nid: T-1\ntitle: Card\nstate: Todo\n" + level_line + "---\n\nbody\n",
        encoding="utf-8",
    )


def test_level_is_read_from_frontmatter(tmp_path: Path) -> None:
    card = tmp_path / "T-1.md"
    _write_card(card, "level: S1\n")
    assert issue_from_file(card).level == "S1"
    _write_card(card, "level:\n")
    assert issue_from_file(card).level is None
    _write_card(card, "")
    assert issue_from_file(card).level is None


def test_level_survives_state_refresh_and_card_rewrite(tmp_path: Path) -> None:
    board = tmp_path / "board"
    board.mkdir()
    _write_card(board / "T-1.md", "level: S1\n")
    cfg = _board_cfg(tmp_path)
    tracker = FileBoardTracker(replace(cfg.tracker, board_root=board))
    [refreshed] = tracker.fetch_issue_states_by_ids(["T-1"])
    assert refreshed.level == "S1"


def test_level_change_between_attempts_applies_to_the_next_attempt(tmp_path: Path) -> None:
    card = tmp_path / "T-1.md"
    base = _leveled()
    _write_card(card, "level: L1\n")
    first = _config_for_issue_agent(base, issue_from_file(card))
    _write_card(card, "level: S1\n")
    second = _config_for_issue_agent(base, issue_from_file(card))
    assert "--model haiku --effort medium" in first.claude.command
    assert "--model sonnet --effort high" in second.claude.command


# ------------------------------------------------- unknown level -> Blocked


def _tick(monkeypatch, tmp_path: Path, issue: Issue, cfg: ServiceConfig, *, fail_move=False):
    orch = _orch()
    monkeypatch.setattr(orch._workflow_state, "reload", lambda: (cfg, None))
    dispatched: list[str] = []
    notes: list[tuple[str, str, str]] = []
    moved: list[tuple[str, str]] = []

    async def _fetch(_cfg):
        return [issue]

    async def _archive(_cfg):
        return None

    def _move(_cfg, _issue, target):
        if fail_move:
            raise RuntimeError("tracker down")
        moved.append((_issue.identifier, target))

    monkeypatch.setattr(orch, "_fetch_candidates", _fetch)
    monkeypatch.setattr(orch, "_archive_sweep", _archive)
    monkeypatch.setattr(
        orch, "_dispatch", lambda i, c, *, attempt, attempt_kind=None: dispatched.append(i.identifier)
    )
    monkeypatch.setattr(
        Orchestrator,
        "_tracker_call_append_note",
        staticmethod(lambda c, i, heading, body: notes.append((i.identifier, heading, body))),
    )
    monkeypatch.setattr(Orchestrator, "_tracker_call_update_state", staticmethod(_move))
    asyncio.run(orch._on_tick())
    return dispatched, notes, moved


def _board_cfg(tmp_path: Path) -> ServiceConfig:
    cfg = _leveled(default_level="L1")
    return replace(
        cfg,
        tracker=replace(
            cfg.tracker,
            kind="file",
            board_root=tmp_path / "board",
            active_states=ACTIVE,
            terminal_states=("Done", "Blocked"),
        ),
        workflow_path=tmp_path / "WORKFLOW.md",
        workspace_root=tmp_path / "ws",
    )


def test_unknown_level_blocks_the_ticket_before_any_turn(monkeypatch, tmp_path: Path) -> None:
    dispatched, notes, moved = _tick(
        monkeypatch, tmp_path, _with_level("T-1", "S9"), _board_cfg(tmp_path)
    )
    assert dispatched == []
    assert moved == [("T-1", "Blocked")]
    [(identifier, heading, body)] = notes
    assert identifier == "T-1" and heading == "Level"
    assert "level 'S9'" in body and "S1" in body


def test_known_level_is_dispatched(monkeypatch, tmp_path: Path) -> None:
    dispatched, notes, moved = _tick(
        monkeypatch, tmp_path, _with_level("T-1", "S1"), _board_cfg(tmp_path)
    )
    assert dispatched == ["T-1"] and notes == [] and moved == []


def test_ticket_without_level_uses_default_and_is_dispatched(monkeypatch, tmp_path: Path) -> None:
    dispatched, notes, moved = _tick(
        monkeypatch, tmp_path, _with_level("T-1", None), _board_cfg(tmp_path)
    )
    assert dispatched == ["T-1"] and moved == []


def test_unknown_level_is_blocked_on_the_fifo_dispatch_path_too(monkeypatch, tmp_path: Path) -> None:
    orch = _orch()
    cfg = _board_cfg(tmp_path)
    notes: list[tuple[str, str]] = []
    moved: list[tuple[str, str]] = []
    dispatched: list[str] = []
    monkeypatch.setattr(
        orch, "_dispatch", lambda i, c, *, attempt, attempt_kind=None: dispatched.append(i.identifier)
    )
    monkeypatch.setattr(
        Orchestrator,
        "_tracker_call_append_note",
        staticmethod(lambda c, i, heading, body: notes.append((i.identifier, heading))),
    )
    monkeypatch.setattr(
        Orchestrator,
        "_tracker_call_update_state",
        staticmethod(lambda c, i, target: moved.append((i.identifier, target))),
    )
    candidates = [_with_level("T-1", "S9"), _with_level("T-2", "S1")]
    asyncio.run(orch._dispatch_fifo_without_schedule_projection(candidates, cfg))
    assert dispatched == ["T-2"]
    assert moved == [("T-1", "Blocked")] and notes == [("T-1", "Level")]


def test_failed_block_still_keeps_the_ticket_from_dispatching(monkeypatch, tmp_path: Path) -> None:
    dispatched, notes, moved = _tick(
        monkeypatch,
        tmp_path,
        _with_level("T-1", "S9"),
        _board_cfg(tmp_path),
        fail_move=True,
    )
    assert dispatched == [] and notes == [] and moved == []
