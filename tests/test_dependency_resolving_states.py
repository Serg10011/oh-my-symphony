"""`agent.dependency_resolving_states`: which blocker states count as done."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from symphony.errors import ConfigValidationError
from symphony.issue import BlockerRef
from symphony.orchestrator.core_support import _blocker_dependency_is_resolved
from symphony.workflow import (
    ServiceConfig,
    build_service_config,
    parse_workflow_text,
)
from tests.test_orchestrator_dispatch import _issue, _make_config, _orch

TERMINAL = "[Subtasks, Human Review, needs_human, Done, Blocked, Archive]"


def _config(tmp_path: Path, *agent_lines: str, terminal: str = TERMINAL) -> ServiceConfig:
    agent = "".join(f"  {line}\n" for line in agent_lines)
    text = (
        "---\n"
        "tracker:\n"
        "  kind: file\n"
        "  board_root: ./board\n"
        "  active_states: [Todo, In Progress]\n"
        f"  terminal_states: {terminal}\n"
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


def _resolved(cfg: ServiceConfig, state: str) -> bool:
    return _blocker_dependency_is_resolved(state, cfg)


def test_without_key_legacy_rule_is_unchanged(tmp_path):
    cfg = _config(tmp_path, terminal="[Human Review, needs_human, Done, Blocked, Archive]")
    assert cfg.agent.dependency_resolving_states == ()
    assert _resolved(cfg, "Done") is True
    for state in ("Human Review", "needs_human", "Blocked", "Archive", "In Progress"):
        assert _resolved(cfg, state) is False


def test_without_key_extra_terminal_state_counts_as_resolved(tmp_path):
    cfg = _config(tmp_path)
    assert _resolved(cfg, "Subtasks") is True
    assert _resolved(cfg, "Done") is True


@pytest.mark.parametrize("key", ["[Done]", "[' DONE ']", "[done]"])
def test_key_limits_resolving_states_case_insensitively(tmp_path, key):
    cfg = _config(tmp_path, f"dependency_resolving_states: {key}")
    assert cfg.agent.dependency_resolving_states == ("done",)
    assert _resolved(cfg, "Done") is True
    assert _resolved(cfg, "DONE") is True
    assert _resolved(cfg, "Subtasks") is False
    assert _resolved(cfg, "Blocked") is False
    assert _resolved(cfg, None) is False


def test_key_may_name_several_states(tmp_path):
    cfg = _config(tmp_path, "dependency_resolving_states: [Done, Subtasks]")
    assert _resolved(cfg, "Done") is True
    assert _resolved(cfg, "Subtasks") is True
    assert _resolved(cfg, "Archive") is False


def test_empty_list_is_the_same_as_no_key(tmp_path):
    cfg = _config(tmp_path, "dependency_resolving_states: []")
    assert cfg.agent.dependency_resolving_states == ()
    assert _resolved(cfg, "Subtasks") is True


def test_dispatcher_and_projection_agree(tmp_path):
    cfg = _make_config(
        terminal_states=("Subtasks", "Done", "Blocked"),
    )
    cfg = replace(cfg, agent=replace(cfg.agent, dependency_resolving_states=("done",)))
    orch = _orch()
    orch._workflow_state._config = cfg

    blocker = BlockerRef(id="id-MT-9", identifier="MT-9", state="Subtasks")
    issue = _issue("MT-1", state="Todo", blocked_by=(blocker,))
    decision = orch._eligibility_contention_decision(issue, cfg)
    assert decision is not None
    assert decision.code == "waiting_dependency"
    assert "MT-9" in decision.reason
    assert orch._should_dispatch(issue, cfg) is False
    assert orch.dependency_state_resolved("Subtasks") is False

    issue = replace(issue, blocked_by=(replace(blocker, state="Done"),))
    assert orch._eligibility_contention_decision(issue, cfg) is None
    assert orch._should_dispatch(issue, cfg) is True
    assert orch.dependency_state_resolved("Done") is True


def test_without_key_dispatcher_treats_extra_terminal_state_as_done():
    cfg = _make_config(terminal_states=("Subtasks", "Done"))
    orch = _orch()
    blocker = BlockerRef(id="id-MT-9", identifier="MT-9", state="Subtasks")
    issue = _issue("MT-1", state="Todo", blocked_by=(blocker,))
    assert orch._should_dispatch(issue, cfg) is True


@pytest.mark.parametrize(
    "value",
    ["[Nope]", "[In Progress]", "[Done, Nope]", "Done", "''", "[Done, '']", "[1]", "{a: b}"],
)
def test_bad_value_is_a_config_error(tmp_path, value):
    with pytest.raises(ConfigValidationError, match="dependency_resolving_states"):
        _config(tmp_path, f"dependency_resolving_states: {value}")


def test_error_lists_allowed_states(tmp_path):
    with pytest.raises(ConfigValidationError, match="allowed:.*done"):
        _config(tmp_path, "dependency_resolving_states: [In Progress]")
