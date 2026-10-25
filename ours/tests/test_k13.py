"""Tests for TASK-K13: [agent] allowed_bash, the stall watchdog formula,
[service] model and ours/bin/project-launchd.

The board folder here is a bare project.toml: render and launchd only read
that file, so no git repository is needed. The scripts run as subprocesses.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the kit is POSIX-only")

OURS = Path(__file__).resolve().parents[1]
PROJECT_RENDER = OURS / "bin" / "project-render"
PROJECT_LAUNCHD = OURS / "bin" / "project-launchd"
PROJECT_NEW = OURS / "bin" / "project-new"
sys.path.insert(0, str(OURS / "hooks"))

import project_config as pc  # noqa: E402

TOML = """\
[project]
name = "demo"
repo = "{repo}"
base_branch = "dev"
id_prefix = "KIT"
port = 9971

[tools]
python = "/opt/py/bin/python"
symphony = "/opt/py/bin/symphony"
claude = "/opt/bin/claude"
"""


def make_board(tmp: Path, extra: str = "") -> tuple[Path, Path]:
    """Board folder with a bare project.toml; [project] repo is a real git repository
    (the core refuses workspace.code_repo otherwise)."""
    repo = tmp / "code"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    root = tmp / "projects"
    board = root / "demo"
    board.mkdir(parents=True)
    (board / "project.toml").write_text(TOML.format(repo=repo) + extra, encoding="utf-8")
    return root, board


def run_script(script: Path, *args: str, home: Path | None = None):
    env = {**os.environ}
    if home:
        env["HOME"] = str(home)
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True,
                          timeout=120, env=env)


def claude_args(board: Path) -> list[str]:
    project = pc.load_project(board)
    return shlex.split(pc.template_values(board, project)["claude_command"])


# ---------------------------------------------------------------------------
# [agent] allowed_bash
# ---------------------------------------------------------------------------


def test_default_command_carries_the_base_allow_list(tmp_path: Path) -> None:
    _, board = make_board(tmp_path)
    args = claude_args(board)
    start = args.index("--allowedTools")
    rules = args[start + 1 : args.index("--add-dir")]
    assert rules == [
        f"Bash(/bin/bash {board.resolve()}/test.sh:*)",
        "Bash(git merge-tree:*)", "Bash(test:*)", "Bash(echo:*)", "Bash(cat:*)",
        "Bash(wc:*)", "Bash(head:*)", "Bash(tail:*)",
    ]
    # The allow list sits before the options the core appends and injects.
    assert args[:7] == ["/opt/bin/claude", "-p", "--output-format", "stream-json", "--verbose",
                        "--permission-mode", "acceptEdits"]


def test_project_entries_follow_the_base_and_are_deduplicated(tmp_path: Path) -> None:
    _, board = make_board(tmp_path, '\n[agent]\nallowed_bash = ['
                          '".venv/bin/python -m ruff", "git merge-tree", ".venv/bin/python -m ruff"]\n')
    assert pc.load_project(board)["agent"]["allowed_bash"] == [
        ".venv/bin/python -m ruff", "git merge-tree"]
    args = claude_args(board)
    rules = args[args.index("--allowedTools") + 1 : args.index("--add-dir")]
    assert rules[-1] == "Bash(.venv/bin/python -m ruff:*)"
    assert rules.count("Bash(git merge-tree:*)") == 1 and len(rules) == 9


def test_path_with_spaces_stays_one_argument(tmp_path: Path) -> None:
    root, board = make_board(tmp_path / "my projects")
    args = claude_args(board)
    assert f"Bash(/bin/bash {board.resolve()}/test.sh:*)" in args


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ('""', "нужен список непустых строк"),
        ('"   "', "нужен список непустых строк"),
        ('"*"', "разрешает любую команду"),
        ('"Bash(*)"', "без обёртки Bash"),
        ('"Bash(git log)"', "без обёртки Bash"),
        ('"bash"', "разрешает любую команду"),
        ('"SH"', "разрешает любую команду"),
        ('"env"', "разрешает любую команду"),
        ('"git log:*"', "суффикс :*"),
        ('"--dangerously-skip-permissions"', "отключение проверок разрешений"),
        ('"claude --permission-mode bypassPermissions"', "отключение проверок разрешений"),
        ('"claude --allowedTools Bash"', "отключение проверок разрешений"),
        ('"x\\ny"', "управляющие символы"),
    ],
)
def test_bad_allowed_bash_is_refused(tmp_path: Path, entry: str, reason: str) -> None:
    _, board = make_board(tmp_path, f"\n[agent]\nallowed_bash = [{entry}]\n")
    with pytest.raises(pc.ProjectError) as exc:
        pc.load_project(board)
    assert reason in str(exc.value) and "allowed_bash" in str(exc.value)


def test_allowed_bash_must_be_a_list_and_the_key_is_checked(tmp_path: Path) -> None:
    for extra, text in (
        ('\n[agent]\nallowed_bash = ".venv/bin/python"\n', "нужен список"),
        ("\n[agent]\nallowed = []\n", "неизвестный ключ"),
    ):
        _, board = make_board(tmp_path / str(abs(hash(extra))), extra)
        with pytest.raises(pc.ProjectError, match=text):
            pc.load_project(board)


def test_rendered_workflow_with_allow_list_is_accepted_by_the_core(tmp_path: Path) -> None:
    from symphony.workflow import build_service_config, load_workflow

    root, board = make_board(tmp_path, '\n[agent]\nallowed_bash = [".venv/bin/python -m ruff"]\n')
    done = run_script(PROJECT_RENDER, "demo", "--root", str(root))
    assert done.returncode == 0, done.stderr
    cfg = build_service_config(load_workflow(board / "WORKFLOW.md"))
    command = shlex.split(cfg.claude.command)
    assert "Bash(.venv/bin/python -m ruff:*)" in command
    assert "--allowedTools" in command and "--add-dir" in command
    for banned in ("--dangerously-skip-permissions", "--permission-mode=bypassPermissions"):
        assert banned not in command
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"


def test_project_new_example_is_commented_out_and_loads(tmp_path: Path) -> None:
    text = (PROJECT_NEW).read_text(encoding="utf-8")
    assert '# allowed_bash = [".venv/bin/python -m ruff"]' in text
    assert '# model = "haiku"' in text
    # Uncommented, the example is a valid project.toml.
    _, board = make_board(tmp_path, '\n[agent]\nallowed_bash = [".venv/bin/python -m ruff"]\n'
                                    '\n[service]\nmodel = "haiku"\n')
    project = pc.load_project(board)
    assert project["service"] == {"model": "haiku"}


# ---------------------------------------------------------------------------
# stall watchdog
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tests_s", "expected"),
    [(1, 300000), (60, 300000), (180, 300000), (181, 301000), (300, 420000),
     (420, 540000), (900, 1020000)],
)
def test_stall_timeout_formula(tests_s: int, expected: int) -> None:
    assert pc.stall_timeout_ms(tests_s) == expected


def test_rendered_stall_follows_tests_s(tmp_path: Path) -> None:
    from symphony.workflow import build_service_config, load_workflow

    for tests_s, expected in ((None, 1020000), (60, 300000), (420, 540000)):
        extra = f"\n[timeouts]\ntests_s = {tests_s}\n" if tests_s else ""
        root, board = make_board(tmp_path / f"t{tests_s}", extra)
        assert run_script(PROJECT_RENDER, "demo", "--root", str(root)).returncode == 0
        cfg = build_service_config(load_workflow(board / "WORKFLOW.md"))
        assert cfg.claude.stall_timeout_ms == expected
        assert f"stall_timeout_ms: {expected}\n" in (board / "WORKFLOW.md").read_text()


# ---------------------------------------------------------------------------
# [service] model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["", "ha iku", "-haiku", "haiku;rm", "a\\nb"])
def test_bad_model_is_refused(tmp_path: Path, model: str) -> None:
    _, board = make_board(tmp_path, f'\n[service]\nmodel = "{model}"\n')
    with pytest.raises(pc.ProjectError, match="model"):
        pc.load_project(board)


# ---------------------------------------------------------------------------
# project-launchd
# ---------------------------------------------------------------------------


def launchd(tmp: Path, extra: str = "") -> tuple[subprocess.CompletedProcess, Path, dict]:
    root, board = make_board(tmp, extra)
    home = tmp / "home"
    home.mkdir()
    result = run_script(PROJECT_LAUNCHD, "demo", "--root", str(root), home=home)
    plist = board / "launchd.plist"
    data = plistlib.loads(plist.read_bytes()) if plist.exists() else {}
    return result, board, data


def test_plist_content(tmp_path: Path) -> None:
    result, board, data = launchd(tmp_path)
    assert result.returncode == 0, result.stderr
    board = board.resolve()
    assert data == {
        "Label": "local.symphony.demo",
        "ProgramArguments": ["/opt/py/bin/python", "-m", "symphony.cli", str(board / "WORKFLOW.md"),
                             "--host", "127.0.0.1", "--port", "9971"],
        "WorkingDirectory": str(board),
        "EnvironmentVariables": {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(tmp_path / "home"),
            "PYTHONUNBUFFERED": "1",
            "SYMPHONY_PYTHON": "/opt/py/bin/python",
        },
        "StandardOutPath": str(board / "log" / "symphony.log"),
        "StandardErrorPath": str(board / "log" / "symphony.log"),
        "RunAtLoad": True,
        "KeepAlive": False,
    }
    assert "ANTHROPIC_MODEL" not in data["EnvironmentVariables"]
    assert (board / "log").is_dir()


def test_plist_command_matches_the_core(tmp_path: Path) -> None:
    from symphony.service import build_orchestrator_command

    _, board, data = launchd(tmp_path)
    core = build_orchestrator_command(board / "WORKFLOW.md", host="127.0.0.1", port=9971)
    assert data["ProgramArguments"][1:] == core[1:]


def test_plist_model_sets_anthropic_model(tmp_path: Path) -> None:
    result, _, data = launchd(tmp_path, '\n[service]\nmodel = "haiku"\n')
    assert result.returncode == 0, result.stderr
    assert data["EnvironmentVariables"]["ANTHROPIC_MODEL"] == "haiku"


def test_launchctl_commands_and_reminder_are_printed(tmp_path: Path) -> None:
    result, board, _ = launchd(tmp_path)
    plist = board / "launchd.plist"
    out = result.stdout
    assert f"launchctl bootstrap gui/$(id -u) {plist}" in out
    assert f"launchctl bootout gui/$(id -u) {plist}" in out
    assert "ps eww -p" in out
    assert "одно вместо другого" in out and "не обе сразу" in out
    assert "ВНИМАНИЕ: нет WORKFLOW.md" in out  # board without a rendered file


def test_rewrite_shows_the_diff_and_second_run_changes_nothing(tmp_path: Path) -> None:
    root, board = make_board(tmp_path)
    first = run_script(PROJECT_LAUNCHD, "demo", "--root", str(root))
    again = run_script(PROJECT_LAUNCHD, "demo", "--root", str(root))
    assert "launchd.plist: записан" in first.stdout
    assert "без изменений" in again.stdout and "ВНИМАНИЕ" in again.stdout
    with (board / "project.toml").open("a", encoding="utf-8") as handle:
        handle.write('\n[service]\nmodel = "haiku"\n')
    third = run_script(PROJECT_LAUNCHD, "demo", "--root", str(root))
    assert '+\t\t<key>ANTHROPIC_MODEL</key>' in third.stdout
    assert "launchd.plist: записан" in third.stdout


@pytest.mark.parametrize(
    ("args", "reason"),
    [(["Demo"], "некорректно"), (["nope"], "нет файла"), (["other"], "нет файла")],
)
def test_launchd_refusals_write_nothing(tmp_path: Path, args: list[str], reason: str) -> None:
    root, board = make_board(tmp_path)
    result = run_script(PROJECT_LAUNCHD, *args, "--root", str(root))
    assert result.returncode == 1 and reason in result.stderr
    assert not (board / "launchd.plist").exists() and not (board / "log").exists()


def test_launchd_refuses_a_name_that_differs_from_project_toml(tmp_path: Path) -> None:
    root, board = make_board(tmp_path)
    (root / "alias").symlink_to(board)
    result = run_script(PROJECT_LAUNCHD, "alias", "--root", str(root))
    assert result.returncode == 1 and "а не 'alias'" in result.stderr
    assert not (board / "launchd.plist").exists()


def test_launchd_refuses_a_bad_project_toml(tmp_path: Path) -> None:
    root, board = make_board(tmp_path, '\n[service]\nmodel = "a b"\n')
    result = run_script(PROJECT_LAUNCHD, "demo", "--root", str(root))
    assert result.returncode == 1 and "model" in result.stderr
    assert not (board / "launchd.plist").exists()
