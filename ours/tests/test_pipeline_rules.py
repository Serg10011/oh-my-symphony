"""Tests for the rule files of a board (TASK-E4b): the files named by [agent] project_rules /
manager_rules are forbidden for cards, nested CLAUDE.md / AGENTS.md too, and docs/agents/pipeline.md
holds the rules of a pipeline agent without the rules of Claude Code tasks.

Real temporary folders and git repositories; `claude` is never started.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from test_check import Env as CheckEnv
from test_check import assert_passed, assert_refused, write

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the kit is POSIX-only")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ours" / "hooks"))

import check as check_py  # noqa: E402
import plan  # noqa: E402

PROJECT_RULES = "docs/agents/pipeline.md"
MANAGER_RULES = "docs/agents/pipeline-map.md"


@pytest.fixture
def env(tmp_path: Path) -> CheckEnv:
    return CheckEnv(tmp_path)


def board(env: CheckEnv, agent: str = "", checks: str = "") -> None:
    """The board of `env` with an [agent] section and extra [checks] lines."""
    env.config()
    path = env.board / "project.toml"
    text = path.read_text()
    if checks:
        text = text.replace("[checks]\n", "[checks]\n" + checks, 1) if "[checks]" in text else text + "[checks]\n" + checks
    path.write_text(text + ("[agent]\n" + agent if agent else ""))


def limits_list(env: CheckEnv) -> str:
    ctx = check_py.Context("plan", env.board, env.ws, "T-1")
    return "\n".join(plan._forbidden_lines(ctx))


# ---------------------------------------------------------------------------
# 1. the files of [agent] project_rules / manager_rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [PROJECT_RULES, MANAGER_RULES])
def test_a_card_cannot_change_a_rules_file(env: CheckEnv, path: str) -> None:
    board(env, f'project_rules = "{PROJECT_RULES}"\nmanager_rules = "{MANAGER_RULES}"\n')
    write(env.ws / path, "new rules\n")
    env.commit()
    assert_refused(env.run("gate"), f"forbidden path changed: {path}")


@pytest.mark.parametrize("path", [PROJECT_RULES, MANAGER_RULES])
def test_without_the_keys_the_path_is_not_forbidden(env: CheckEnv, path: str) -> None:
    board(env)
    write(env.ws / path, "notes\n")
    env.commit()
    assert_passed(env.run("gate"))


def test_allow_paths_does_not_free_a_rules_file(env: CheckEnv) -> None:
    board(env, f'project_rules = "{PROJECT_RULES}"\n', checks='allow_paths = ["ours/**"]\n')
    write(env.ws / PROJECT_RULES, "new rules\n")
    env.commit()
    assert_refused(env.run("gate"), f"forbidden path changed: {PROJECT_RULES}")


def test_the_rules_files_are_in_the_forbidden_list_of_the_brief(env: CheckEnv) -> None:
    board(env, f'project_rules = "{PROJECT_RULES}"\nmanager_rules = "{MANAGER_RULES}"\n')
    text = limits_list(env)
    assert f"- `{PROJECT_RULES}`" in text and f"- `{MANAGER_RULES}`" in text
    board(env)
    assert PROJECT_RULES not in limits_list(env)


# ---------------------------------------------------------------------------
# 2. a double ban
# ---------------------------------------------------------------------------


def test_a_double_ban_gives_one_clear_refusal(env: CheckEnv) -> None:
    board(env, f'project_rules = "{PROJECT_RULES}"\n', checks='forbidden_paths = ["docs/**"]\n')
    cfg = check_py.load_config(env.board)
    assert check_py.forbidden_violations({PROJECT_RULES}, cfg) == [
        f"forbidden path changed: {PROJECT_RULES}"
    ]
    write(env.ws / PROJECT_RULES, "new rules\n")
    env.commit()
    assert_refused(env.run("gate"), f"forbidden path changed: {PROJECT_RULES}")


def test_a_change_elsewhere_passes_with_a_double_ban(env: CheckEnv) -> None:
    board(env, f'project_rules = "{PROJECT_RULES}"\n', checks='forbidden_paths = ["docs/**"]\n')
    write(env.ws / "src" / "newmod.py", "x = 1\n")
    env.commit()
    assert_passed(env.run("gate"))


# ---------------------------------------------------------------------------
# 3. nested CLAUDE.md / AGENTS.md
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["docs/CLAUDE.md", "lib/x/AGENTS.md", "CLAUDE.md", "AGENTS.md"])
def test_nested_and_root_instruction_files_are_forbidden(env: CheckEnv, path: str) -> None:
    board(env)
    write(env.ws / path, "rules\n")
    env.commit()
    assert_refused(env.run("gate"), f"forbidden path changed: {path}")


def test_other_files_with_similar_names_stay_open() -> None:
    cfg = check_py.Config(base_branch="main", symphony_bin="/bin/true")
    assert check_py.forbidden_violations({"docs/MY-CLAUDE.md", "docs/claude.md.txt"}, cfg) == []


# ---------------------------------------------------------------------------
# 4. docs/agents/pipeline.md and CLAUDE.md
# ---------------------------------------------------------------------------


def test_pipeline_rules_are_short_and_free_of_task_rules() -> None:
    data = (REPO / PROJECT_RULES).read_bytes()
    assert 0 < len(data) <= 3 * 1024
    text = data.decode("utf-8")
    for name in ("DECISIONS.md", "ARCHITECTURE.md", "ROADMAP.md", "AGENTS.md"):
        assert name not in text, name


def test_claude_md_points_to_the_pipeline_rules() -> None:
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert "## Если ты агент конвейера" not in text
    assert "Ты агент конвейера, если" not in text
    assert "Агент конвейера: твои правила — `docs/agents/pipeline.md`" in text
