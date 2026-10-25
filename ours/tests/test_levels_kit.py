"""Tests for task levels in the kit (TASK-K14): [levels] and [limits] of
project.toml, the stub of project-new, the rendered WORKFLOW.md.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the kit is POSIX-only")

OURS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OURS / "hooks"))

import project_config as pc  # noqa: E402
from test_project_kit import PREFIX, Kit  # noqa: E402

BASE = {
    "project": {"name": "x", "repo": "/r", "base_branch": "dev", "id_prefix": "XX", "port": 9000},
    "tools": {"python": "/p", "symphony": "/s", "claude": "/c/claude"},
}


def data(**sections: dict) -> dict:
    return {**BASE, **sections}


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    return Kit(tmp_path)


def core_cfg(board: Path):
    from symphony.workflow import build_service_config, load_workflow

    return build_service_config(load_workflow(board / "WORKFLOW.md"))


def rerender(board: Path) -> None:
    (board / "WORKFLOW.md").write_text(pc.render_workflow(board, pc.load_project(board)))


# ---------------------------------------------------------------------------
# the stub of project-new
# ---------------------------------------------------------------------------


def test_stub_has_config_b_levels_and_strict_limits(kit: Kit) -> None:
    assert kit.new().returncode == 0
    project = pc.load_project(kit.board)
    levels = project["levels"]
    assert levels["default_level"] == "L1"
    assert levels["levels"] == {
        "L1": {"claude": {"model": "haiku", "effort": "medium"}},
        "L2": {"claude": {"model": "haiku", "effort": "high"}},
        "S1": {"claude": {"model": "sonnet", "effort": "high"}},
        "S2": {"claude": {"model": "sonnet", "effort": "max"}},
    }
    assert project["limits"] == {
        "max_state_turns": 2, "max_turns": 8, "max_total_turns": 20, "max_attempts": 2,
    }
    assert project["checks"]["max_check_failures"] == 2


def test_stub_levels_have_no_own_limits_of_their_own(kit: Kit) -> None:
    assert kit.new().returncode == 0
    for body in pc.load_project(kit.board)["levels"]["levels"].values():
        assert "max_turns" not in body and "max_state_turns" not in body


def test_stub_workflow_is_accepted_by_the_core(kit: Kit) -> None:
    assert kit.new().returncode == 0
    cfg = core_cfg(kit.board)
    assert set(cfg.levels) == {"L1", "L2", "S1", "S2"}
    assert cfg.levels["S2"].claude.effort == "max"
    assert cfg.agent.default_level == "L1"
    assert (cfg.agent.max_state_turns, cfg.agent.max_turns) == (2, 8)
    assert (cfg.agent.max_total_turns, cfg.agent.max_attempts) == (20, 2)


def test_stub_card_level_reaches_the_claude_command(kit: Kit) -> None:
    from dataclasses import replace

    from symphony.issue import Issue
    from symphony.orchestrator import _config_for_issue_agent

    assert kit.new().returncode == 0
    cfg = core_cfg(kit.board)
    card = Issue(id="K-1", identifier="K-1", title="t", description=None, priority=None,
                 state="Todo")
    program = str(kit.bin / "claude")
    s1 = _config_for_issue_agent(cfg, replace(card, level="S1")).claude.command
    assert s1.startswith(f"{program} --model sonnet --effort high -p ")
    default = _config_for_issue_agent(cfg, card).claude.command
    assert default.startswith(f"{program} --model haiku --effort medium -p ")
    # The rest of the command (permissions, board folders) is untouched.
    assert default.split(" -p ", 1)[1] == s1.split(" -p ", 1)[1]


def test_stub_comments_explain_levels_and_limits(kit: Kit) -> None:
    assert kit.new().returncode == 0
    text = (kit.board / "project.toml").read_text()
    for phrase in ("Ход — один запуск агента", "[levels.LOCAL]", "needs_human", "Blocked",
                   "budget_exhausted", "max_check_failures"):
        assert phrase in text


# ---------------------------------------------------------------------------
# a project without [levels] behaves as before
# ---------------------------------------------------------------------------


def test_project_without_levels_renders_no_levels(kit: Kit) -> None:
    assert kit.new().returncode == 0
    toml = kit.board / "project.toml"
    text = toml.read_text()
    toml.write_text(text[: text.index("[levels]")] + text[text.index("[limits]"):])
    rerender(kit.board)
    workflow = (kit.board / "WORKFLOW.md").read_text()
    assert "levels:" not in workflow and "default_level" not in workflow
    cfg = core_cfg(kit.board)
    assert cfg.levels == {} and cfg.agent.default_level == ""


def test_old_limits_without_state_turns_keep_the_core_default() -> None:
    limits = pc.validate_project(data(limits={"max_turns": 30}))["limits"]
    assert limits["max_state_turns"] == pc.DEFAULT_LIMITS["max_state_turns"] == 30


# ---------------------------------------------------------------------------
# [levels] validation
# ---------------------------------------------------------------------------


def levels(**kwargs) -> dict:
    return data(levels=kwargs)


def test_levels_accept_every_documented_key() -> None:
    out = pc.validate_project(levels(
        default_level="A",
        A={"claude": {"model": "claude-haiku-5-5", "effort": "xhigh"},
           "codex": {"model": "gpt-x", "effort": "low"}, "max_turns": 4, "max_state_turns": 1},
        B={},
    ))["levels"]
    assert out["default_level"] == "A" and set(out["levels"]) == {"A", "B"}


@pytest.mark.parametrize(
    ("sections", "reason"),
    [
        ({"bad name": {}}, "имя уровня"),
        ({"L.1": {}}, "имя уровня"),
        ({"L1": 5}, "должен быть таблицей [levels.L1]"),
        ({"L1": {"color": "red"}}, "неизвестный ключ(и) color"),
        ({"L1": {"gemini": {"model": "x"}}}, "только claude и codex"),
        ({"L1": {"claude": {"modle": "x"}}}, "неизвестный ключ(и) в claude: modle"),
        ({"L1": {"claude": "haiku"}}, "claude должен быть таблицей"),
        ({"L1": {"claude": {"model": ""}}}, "нужна непустая строка"),
        ({"L1": {"claude": {"model": "a b"}}}, "только буквы, цифры"),
        ({"L1": {"claude": {"model": "$(x)"}}}, "только буквы, цифры"),
        ({"L1": {"claude": {"effort": ""}}}, "нужна непустая строка"),
        ({"L1": {"claude": {"effort": "ultra"}}}, "допустимо: low, medium, high, xhigh, max"),
        ({"L1": {"max_turns": 3}}, "max_turns не меньше 4"),
        ({"L1": {"max_turns": 0}}, "нужно целое число больше нуля"),
        ({"L1": {"max_state_turns": 0}}, "нужно целое число больше нуля"),
        ({"L1": {"max_state_turns": True}}, "нужно целое число больше нуля"),
        ({"L1": {"max_state_turns": "2"}}, "нужно целое число больше нуля"),
        ({"default_level": ""}, "нужна непустая строка"),
        ({"default_level": 3}, "нужно имя уровня"),
        ({"L1": {}, "default_level": "L2"}, "такого уровня нет; есть: L1"),
        ({"default_level": "L1"}, "такого уровня нет"),
    ],
)
def test_bad_levels_refused(sections: dict, reason: str) -> None:
    with pytest.raises(pc.ProjectError, match=re.escape(reason)):
        pc.validate_project(levels(**sections))


def test_levels_must_be_a_section() -> None:
    with pytest.raises(pc.ProjectError, match="должен быть разделом"):
        pc.validate_project(data(levels=["L1"]))


def test_codex_effort_is_not_limited_to_claude_values() -> None:
    out = pc.validate_project(levels(L1={"codex": {"effort": "minimal"}}))
    assert out["levels"]["levels"]["L1"]["codex"]["effort"] == "minimal"


def test_unknown_section_still_refused_next_to_levels() -> None:
    with pytest.raises(pc.ProjectError, match="неизвестный раздел"):
        pc.validate_project(data(level={"L1": {}}))


# ---------------------------------------------------------------------------
# [limits] max_state_turns
# ---------------------------------------------------------------------------


def test_max_state_turns_reaches_the_core(kit: Kit) -> None:
    assert kit.new().returncode == 0
    toml = kit.board / "project.toml"
    toml.write_text(toml.read_text().replace("max_state_turns = 2 ", "max_state_turns = 5"))
    rerender(kit.board)
    assert core_cfg(kit.board).agent.max_state_turns == 5


@pytest.mark.parametrize("value", [0, -1, True, "2"])
def test_max_state_turns_must_be_a_positive_integer(value: object) -> None:
    with pytest.raises(pc.ProjectError, match="нужно целое число больше нуля"):
        pc.validate_project(data(limits={"max_state_turns": value}))


def test_max_turns_still_covers_the_active_states() -> None:
    with pytest.raises(pc.ProjectError, match="max_turns не меньше 4"):
        pc.validate_project(data(limits={"max_turns": 3}))


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_levels_block_is_valid_yaml_with_odd_values_quoted() -> None:
    import yaml

    project = pc.validate_project(levels(
        default_level="L-1",
        **{"L-1": {"claude": {"model": "claude-x[1m]", "effort": "low"}, "max_turns": 9},
           "E": {}},
    ))
    block = yaml.safe_load(pc.levels_yaml(project))
    assert block == {"levels": {
        "L-1": {"claude": {"model": "claude-x[1m]", "effort": "low"}, "max_turns": 9},
        "E": {},
    }}


def test_levels_with_own_limits_reach_the_core(kit: Kit) -> None:
    assert kit.new().returncode == 0
    toml = kit.board / "project.toml"
    toml.write_text(toml.read_text().replace(
        "# [levels.LOCAL]\n# max_turns = 40               # не меньше 4 (число активных состояний)\n"
        "# max_state_turns = 6",
        "[levels.LOCAL]\nmax_turns = 40\nmax_state_turns = 6"))
    rerender(kit.board)
    local = core_cfg(kit.board).levels["LOCAL"]
    assert (local.max_turns, local.max_state_turns) == (40, 6)


# ---------------------------------------------------------------------------
# card template
# ---------------------------------------------------------------------------


def test_project_new_writes_a_card_template_with_level_hints(kit: Kit) -> None:
    import yaml

    assert kit.new().returncode == 0
    path = kit.board / "card-template.md"
    text = path.read_text()
    front = yaml.safe_load(text.split("---\n")[1])
    assert front["level"] == "L1" and front["state"] == "Todo" and front["id"] == f"{PREFIX}-N"
    assert "Уровень: L1 — почему такой" in text
    assert f"/bin/bash {kit.board}/test.sh" in text
    # Outside kanban/: the service must never dispatch the sample.
    assert not list((kit.board / "kanban").glob("*.md"))
