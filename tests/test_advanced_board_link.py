"""Run the shipped advanced board guard on actual links, not path-string mocks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from symphony._shell import resolve_bash


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ("WORKFLOW.md", "examples/advanced/WORKFLOW.file.example.md")
CASES = ("absolute", "relative", "wrong", "missing", "directory")
if os.name == "nt":
    CASES += ("junction", "wrong_junction")
SPELLINGS = ("native", "shell") if os.name == "nt" else ("native",)


def _bash(script: str, cwd: Path, *args: str, env=None):
    env = dict(os.environ if env is None else env)
    if os.name == "nt":
        # The default MSYS mode copies directories instead of linking them.
        env["MSYS"] = "winsymlinks:sys"
    return subprocess.run(
        [resolve_bash(), "-c", script, "board-guard-test", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _shell_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    result = _bash('cygpath -u "$1"', path.parent, str(path))
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("host_spelling", SPELLINGS)
def test_advanced_board_guard(tmp_path, workflow, case, host_spelling):
    host = tmp_path / "host project"
    board = host / "kanban"
    board.mkdir(parents=True)
    other = tmp_path / "other board"
    other.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "kanban"
    destination = other if case in {"wrong", "wrong_junction"} else board

    if case in {"junction", "wrong_junction"}:
        linked = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(target), str(destination)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert linked.returncode == 0, linked.stderr
        assert target.is_junction()
    elif case == "directory":
        target.mkdir()
    elif case != "missing":
        link_to = (
            os.path.relpath(destination, workspace).replace("\\", "/")
            if case == "relative"
            else _shell_path(destination)
        )
        # Git Bash creates its real MSYS symlink without requiring Win32
        # symlink privilege; native junctions are exercised separately above.
        linked = _bash('ln -s -- "$1" kanban', workspace, link_to)
        assert linked.returncode == 0, linked.stderr
        assert _bash('[ -L kanban ]', workspace).returncode == 0

    frontmatter = (ROOT / workflow).read_text(encoding="utf-8").split("---", 2)[1]
    hook = yaml.safe_load(frontmatter)["hooks"]["before_run"]
    env = os.environ.copy()
    env["SYMPHONY_WORKFLOW_DIR"] = (
        _shell_path(host) if host_spelling == "shell" else str(host)
    )
    env["SYMPHONY_BOARD_ROOT_NAME"] = "kanban"
    if os.name != "nt":
        env.pop("OS", None)
    result = _bash(hook, workspace, env=env)
    expected = 0 if case in {"absolute", "relative", "junction"} else 42
    assert result.returncode == expected, result.stderr
    if expected == 42:
        assert "FAIL: workspace kanban" in result.stderr
