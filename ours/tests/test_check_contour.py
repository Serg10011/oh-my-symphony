"""[checks] allow_paths and the control contour of check.py (TASK-S4-08).

Unit-level: `forbidden_violations` on a Config, `load_config` on a tiny board
folder, and `changed_paths` on a tiny git repository (renames). The helpers
are repeated here in small form; nothing is imported from test_check.py.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
REPO_ROOT = HOOKS.parents[1]
sys.path.insert(0, str(HOOKS))

_spec = importlib.util.spec_from_file_location("ours_check_contour", HOOKS / "check.py")
assert _spec is not None and _spec.loader is not None
check = importlib.util.module_from_spec(_spec)
sys.modules["ours_check_contour"] = check
_spec.loader.exec_module(check)

OMS_ALLOW = ["ours/prompts/**", "ours/README.md", "ours/tests/**"]
ALL_OURS = ["ours/**"]


def cfg_with(allow: list[str] | None = None, forbidden: list[str] | None = None):
    base = check.Config(base_branch="main", symphony_bin="/bin/true")
    return dataclasses.replace(
        base,
        allow_paths=tuple(allow or ()),
        forbidden_paths=check.DEFAULT_FORBIDDEN_PATHS + tuple(forbidden or ()),
    )


def violated(paths: list[str], cfg) -> set[str]:
    prefix = "forbidden path changed: "
    found = check.forbidden_violations(set(paths), cfg)
    return {line.removeprefix(prefix).split(" (")[0] for line in found}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def make_board(tmp: Path, checks: str) -> Path:
    board = tmp / "board"
    board.mkdir()
    write(
        board / "project.toml",
        "[project]\n"
        'name = "t"\nrepo = "/tmp/t"\nbase_branch = "main"\nid_prefix = "TT"\nport = 9990\n'
        f'[tools]\npython = "{sys.executable}"\nsymphony = "/bin/true"\nclaude = "/bin/true"\n'
        f"[checks]\n{checks}\n",
    )
    return board


# -- defaults and the allow list ---------------------------------------------


def test_without_allow_paths_ours_is_forbidden() -> None:
    assert violated(["ours/prompts/x.md"], cfg_with()) == {"ours/prompts/x.md"}


def test_allow_paths_frees_listed_files() -> None:
    paths = ["ours/prompts/x.md", "ours/README.md", "ours/tests/test_new.py"]
    assert violated(paths, cfg_with(OMS_ALLOW)) == set()


def test_ours_outside_allow_list_stays_forbidden() -> None:
    assert violated(["ours/other/x.md"], cfg_with(OMS_ALLOW)) == {"ours/other/x.md"}


@pytest.mark.parametrize(
    "path",
    [
        "ours/hooks/check.py",
        "ours/hooks/worktree.py",
        "ours/hooks/project_config.py",
        "ours/templates/WORKFLOW.md.tmpl",
        "ours/bin/inbox-runner",
        "ours/bin/post-merge",
        "ours/bin/deliver",
        "ours/hooks/integrate_new.py",
    ],
)
def test_contour_is_forbidden_with_oms_allow_list(path: str) -> None:
    assert violated([path], cfg_with(OMS_ALLOW)) == {path}


def test_contour_message_keeps_the_prefix() -> None:
    lines = check.forbidden_violations({"ours/hooks/check.py"}, cfg_with(OMS_ALLOW))
    assert len(lines) == 1
    assert lines[0].startswith("forbidden path changed: ours/hooks/check.py")


def test_wrong_allow_all_still_protects_contour_and_frees_the_rest() -> None:
    cfg = cfg_with(ALL_OURS)
    assert violated(["ours/hooks/check.py"], cfg) == {"ours/hooks/check.py"}
    assert violated(["ours/prompts/x.md"], cfg) == set()


@pytest.mark.parametrize("path", ["ours/prompts/secret.pem", "ours/prompts/.env", "ours/a/b.key"])
def test_allow_paths_frees_only_the_ours_pattern(path: str) -> None:
    assert violated([path], cfg_with(ALL_OURS)) == {path}


def test_project_forbidden_paths_beat_allow_paths() -> None:
    cfg = cfg_with(["ours/prompts/**"], forbidden=["ours/prompts/locked/**"])
    assert violated(["ours/prompts/locked/a.md", "ours/prompts/b.md"], cfg) == {
        "ours/prompts/locked/a.md"
    }


def test_other_defaults_are_not_freed() -> None:
    cfg = cfg_with(ALL_OURS)
    paths = ["WORKFLOW.md", ".github/x.yml", ".git/config", ".env.local"]
    assert violated(paths, cfg) == set(paths)


# -- renames: changed_paths reports both names ---------------------------------


def _repo_with(tmp: Path, files: list[str]) -> tuple[Path, str]:
    repo = tmp / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    for name in files:
        write(repo / name)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD").strip()


def _moved(tmp: Path, old: str, new: str) -> set[str]:
    repo, base = _repo_with(tmp, [old])
    (repo / new).parent.mkdir(parents=True, exist_ok=True)
    git(repo, "mv", old, new)
    git(repo, "commit", "-q", "-m", "move")
    return violated(sorted(check.changed_paths(repo, base, "kanban")), cfg_with(OMS_ALLOW))


def test_rename_from_contour_into_allowed_zone_is_forbidden(tmp_path: Path) -> None:
    result = _moved(tmp_path, "ours/hooks/check.py", "ours/prompts/check.py")
    assert result == {"ours/hooks/check.py"}


def test_rename_from_allowed_zone_into_contour_is_forbidden(tmp_path: Path) -> None:
    result = _moved(tmp_path, "ours/prompts/a.md", "ours/hooks/a.md")
    assert result == {"ours/hooks/a.md"}


# -- config -------------------------------------------------------------------


def test_load_config_reads_allow_paths(tmp_path: Path) -> None:
    board = make_board(tmp_path, f"allow_paths = {json.dumps(OMS_ALLOW)}")
    assert check.load_config(board).allow_paths == tuple(OMS_ALLOW)


def test_load_config_default_is_empty(tmp_path: Path) -> None:
    assert check.load_config(make_board(tmp_path, "")).allow_paths == ()


@pytest.mark.parametrize(
    ("line", "needle"),
    [
        ('allow_paths = ["src/**"]', "must start with 'ours/'"),
        ('allow_paths = ["ours/prompts/**", "tests/**"]', "must start with 'ours/'"),
        ('allow_paths = [""]', "allow_paths"),
        ('allow_paths = "ours/prompts/**"', "allow_paths"),
        ("allow_paths = [1, 2]", "allow_paths"),
    ],
)
def test_load_config_rejects_bad_allow_paths(tmp_path: Path, line: str, needle: str) -> None:
    board = make_board(tmp_path, line)
    with pytest.raises(check.CheckError) as err:
        check.load_config(board)
    assert str(err.value).startswith("config:")
    assert needle in str(err.value)


# -- guard: nothing in the contour can be opened by a list --------------------


def test_every_contour_file_of_the_repository_is_forbidden() -> None:
    listing = git(REPO_ROOT, "ls-files", "ours/hooks", "ours/bin", "ours/templates")
    files = listing.split()
    assert files, "no contour files found"
    assert violated(files, cfg_with(ALL_OURS)) == set(files)
