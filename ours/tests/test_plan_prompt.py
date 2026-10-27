"""Tests for the planning prompt (TASK S4-20): ours/prompts/stages/plan.md.

The prompt is rendered by the core engine for a manager card in `Plan`
(label `role:manager-plan`). Its example subtask must pass plan.check_plan
without a single reason, exactly as the hook would read it.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import json
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
    subtasks, reasons = plan.check_plan(example, pc.load_project(board), check.load_config(board), "standard",
                                        board)
    assert reasons == []
    assert [s.key for s in subtasks] == ["glossary-terms"]


# ---------------------------------------------------------------------------
# TASK S4-24: the hook holds the rules, limits.md gives the facts
# ---------------------------------------------------------------------------

OMS_CHECKS = '[checks]\nallow_paths = ["ours/prompts/**", "ours/README.md", "ours/tests/**"]\n'


def one_subtask(**fields: object) -> str:
    data: dict = {
        "key": "a", "title": "Define the terms", "level": "simple", "level_reason": "one document",
        "goal": "Write it.", "criteria": ["It exists"], "check": "test -s docs/our/GLOSSARY.md",
        "paths": ["docs/our/GLOSSARY.md"],
    }
    data.update(fields)
    lines = ["version = 1", "", "[[subtask]]"]
    lines += [f"{name} = {json.dumps(value)}" for name, value in data.items()]
    return "\n".join(lines) + "\n"


def reasons_for(board: Path, text: str) -> list[str]:
    return plan.check_plan(text, pc.load_project(board), check.load_config(board), "standard", board)[1]


@pytest.mark.parametrize("command", [
    "test -s docs/our/GLOSSARY.md",
    "test",
    "echo done",
])
def test_an_allowed_check_command_is_accepted(board: Path, command: str) -> None:
    assert reasons_for(board, one_subtask(check=command)) == []


def test_the_exact_test_command_of_the_board_is_accepted(board: Path) -> None:
    command = f"/bin/bash {board.resolve() / 'test.sh'}"
    assert reasons_for(board, one_subtask(check=command)) == []
    assert reasons_for(board, one_subtask(check=command + " tests/test_x.py")) == []


@pytest.mark.parametrize("command", [
    "/bin/bash /somewhere/else/test.sh",  # somebody else's test.sh
    "grep -n x docs/our/GLOSSARY.md",  # not an allowed command
    "testx docs/our/GLOSSARY.md",  # `test` must end at a space
    "test -s a && test -s b",
    "test -s a || test -s b",
    "test -s a ; echo done",
    "test -s a | wc -l",
    "echo $(whoami)",
    "echo x > docs/out.txt",
    "cat < docs/in.txt",
])
def test_a_check_command_the_agent_cannot_run_is_rejected(board: Path, command: str) -> None:
    reasons = reasons_for(board, one_subtask(check=command))
    assert len(reasons) == 1 and "check: команда не разрешена агенту" in reasons[0], reasons
    assert "разрешены:" in reasons[0] and "test" in reasons[0]


def test_a_command_of_the_project_is_allowed_with_its_own_allowed_bash(tmp_path: Path) -> None:
    (tmp_path / "project.toml").write_text(PROJECT + '[agent]\nallowed_bash = ["make lint"]\n', encoding="utf-8")
    assert reasons_for(tmp_path, one_subtask(check="make lint ours")) == []
    assert reasons_for(tmp_path, one_subtask(check="make lintx")) != []


def make_limits(board: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    kanban = board / "kanban"
    kanban.mkdir(exist_ok=True)
    (kanban / "OMS-8.md").write_text(
        "---\nid: OMS-8\nidentifier: OMS-8\ntitle: Big task\nstate: Plan\nlabels: [role:manager-plan]\n"
        "level: standard\n---\n\nbody\n", encoding="utf-8")
    monkeypatch.setattr(plan, "fetch_budgets", lambda port, task: (None, "no service"))
    ctx = check.Context("plan", board, board, "OMS-8")
    return plan.render_limits(ctx, pc.load_project(board))


def section(text: str, heading: str) -> str:
    return re.split(rf"^## {re.escape(heading)}\n", text, flags=re.MULTILINE)[1].split("\n## ")[0]


def bullets(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"^- `([^`]+)`", text, flags=re.MULTILINE)]


def test_limits_name_the_test_command_and_the_allowed_check_commands(board: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = make_limits(board, monkeypatch)
    assert f"`/bin/bash {board.resolve() / 'test.sh'}`" in section(text, "Test command")
    allowed = bullets(section(text, "Allowed check commands"))
    assert allowed == plan.allowed_check_commands(board, pc.load_project(board))
    assert f"/bin/bash {board.resolve() / 'test.sh'}" in allowed and "test" in allowed


def test_limits_list_the_forbidden_paths_of_an_oms_like_board(board: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (board / "project.toml").write_text(PROJECT + OMS_CHECKS, encoding="utf-8")
    forbidden = section(make_limits(board, monkeypatch), "Forbidden paths")
    listed = bullets(forbidden)
    assert "ours/hooks/**" in listed and "ours/bin/**" in listed and "ours/templates/**" in listed
    assert "WORKFLOW.md" in listed and ".env" in listed and "**/*.pem" in listed
    for opened in ("ours/prompts/**", "ours/README.md", "ours/tests/**"):
        assert opened not in listed, opened
        assert f"`{opened}`" in forbidden.split("Открыто внутри")[1]


def test_limits_without_allow_paths_close_ours_entirely(board: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = section(make_limits(board, monkeypatch), "Forbidden paths")
    assert "ours/**" in bullets(forbidden) and "(кроме открытого ниже)" not in forbidden
    assert "ничего не открыто" in forbidden


def test_the_forbidden_list_is_what_the_hook_applies(board: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (board / "project.toml").write_text(PROJECT + OMS_CHECKS + 'forbidden_paths = ["docs/secret/**"]\n', encoding="utf-8")
    cfg = check.load_config(board)
    listed = bullets(section(make_limits(board, monkeypatch), "Forbidden paths"))
    assert "docs/secret/**" in listed
    assert check.forbidden_violations({"docs/secret/x.md"}, cfg)
    assert check.forbidden_violations({"ours/hooks/check.py"}, cfg)
    assert not check.forbidden_violations({"ours/prompts/stages/plan.md"}, cfg)
    assert reasons_for(board, one_subtask(paths=["ours/hooks/check.py"])) != []
    assert reasons_for(board, one_subtask(paths=["ours/prompts/stages/plan.md"])) == []


def test_the_prompt_leaves_the_rules_to_the_hook() -> None:
    text = render_plan()
    assert "## Forbidden paths" in text and "## Allowed check commands" in text and "## Test command" in text
    assert "ours/hooks" not in text and ".pem" not in text, "the list of forbidden paths is not in the prompt"
    assert "<board folder>" not in text and "starting with a verb" in text
    assert "except the plan file" in text


def test_the_prompt_says_that_check_proves_its_own_subtask() -> None:
    text = render_plan()
    assert "6. `check` proves the result of THIS subtask" in text
    assert "never a precondition" in text
