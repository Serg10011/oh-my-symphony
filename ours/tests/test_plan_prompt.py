"""Tests for the planning prompt (TASK S4-20): ours/prompts/stages/plan.md.

The prompt is rendered by the core engine for a manager card in `Plan`
(label `role:manager-plan`). Its example subtask must pass plan.check_plan
without a single reason, exactly as the hook would read it.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hooks are POSIX-only")

OURS = Path(__file__).resolve().parents[1]
PROMPTS = OURS / "prompts"
sys.path.insert(0, str(OURS / "hooks"))

import check  # noqa: E402
import plan  # noqa: E402
import project_config as pc  # noqa: E402

PROJECT = """\
[project]
name = "demo"
repo = "/tmp/demo-repo"
base_branch = "main"
id_prefix = "OMS"
port = 9971
[tools]
python = "/usr/bin/python3"
symphony = "/bin/symphony"
claude = "/bin/claude"
[levels]
default_level = "simple"
[levels.simple]
[levels.simple.claude]
model = "haiku"
effort = "medium"
[levels.standard]
max_turns = 16
[levels.standard.claude]
model = "haiku"
effort = "high"
[levels.complex]
[levels.complex.claude]
model = "sonnet"
effort = "high"
"""


def render_plan(ident: str = "OMS-8") -> str:
    from symphony.issue import Issue
    from symphony.prompt import build_prompt_env, render

    card = Issue(id=ident, identifier=ident, title="Big task", description="d", priority=None,
                 state="Plan", labels=("role:manager-plan",))
    values = build_prompt_env(card, None, auto_merge_on_done=False, max_attempts=2)
    return render((PROMPTS / "stages" / "plan.md").read_text(encoding="utf-8"), values)


def example_block(text: str) -> str:
    blocks = re.findall(r"```toml\n(.*?)```", text, re.DOTALL)
    assert len(blocks) == 1, "the prompt must give exactly one TOML example"
    return blocks[0]


@pytest.fixture
def board(tmp_path: Path) -> Path:
    (tmp_path / "project.toml").write_text(PROJECT, encoding="utf-8")
    return tmp_path


def test_the_prompt_renders_for_a_manager_card_with_the_card_paths() -> None:
    text = render_plan()
    assert "docs/OMS-8/work/plan.toml" in text
    assert "docs/OMS-8/work/limits.md" in text
    assert "{{" not in text and "{%" not in text


@pytest.mark.parametrize("keyword", ["plan.toml", "Subtasks", "Plan Failure", "limits.md", "## Blocker", "needs_human"])
def test_the_prompt_names_the_files_and_the_states_of_the_stage(keyword: str) -> None:
    assert keyword in render_plan()


@pytest.mark.parametrize("key", ["version", *plan._REQUIRED, *plan._OPTIONAL])
def test_the_prompt_documents_every_key_of_the_plan_file(key: str) -> None:
    assert f"`{key}`" in render_plan()


def test_the_example_subtask_passes_the_hook_without_a_reason(board: Path) -> None:
    example = example_block(render_plan())
    subtasks, reasons = plan.check_plan(example, pc.load_project(board), check.load_config(board), "standard")
    assert reasons == []
    assert [s.key for s in subtasks] == ["glossary-terms"]
