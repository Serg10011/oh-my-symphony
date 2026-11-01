"""Exercise the inventory on real disposable Git repositories and worktrees."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOKS))
import workspace_report as wr  # noqa: E402

CLI = HOOKS.parent / "bin/workspace-report"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def repository(path):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Workspace Test")
    git(path, "config", "user.email", "workspace@example.invalid")
    (path / "tracked.txt").write_text("initial\n")
    (path / ".gitignore").write_text("ignored.bin\n.cache/\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "initial")
    return path


@pytest.fixture
def layout(tmp_path):
    workspaces, boards = tmp_path / "workspaces", tmp_path / "boards"
    workspaces.mkdir()
    boards.mkdir()
    return workspaces, boards


def board(boards, name, repo, *, folder=None):
    path = boards / (folder or name)
    (path / "kanban").mkdir(parents=True)
    (path / "project.toml").write_text(
        f'[project]\nname = {json.dumps(name)}\nrepo = {json.dumps(str(repo))}\n',
        encoding="utf-8",
    )
    return path


def command(layout, *args):
    root, boards = layout
    return subprocess.run([sys.executable, str(CLI), "--workspace-root", str(root),
                           "--projects-root", str(boards), *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=20)


def snapshot(root):
    result = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            value = ("link", os.readlink(path))
        elif path.is_file():
            value = ("file", hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        else:
            value = ("directory", path.stat().st_mtime_ns)
        result[str(path.relative_to(root))] = value
    return result


def first_row(report):
    return report["projects"][0]["entries"][0]


def test_known_worktree_changes_ignored_card_and_no_writes(tmp_path, layout):
    root, boards = layout
    repo = repository(tmp_path / "repo")
    b = board(boards, "oms", repo)
    (b / "kanban/OMS-1.md").write_text("---\nid: OMS-1\nstate: Done\n---\n")
    (root / "oms").mkdir()
    place = root / "oms/OMS-1"
    git(repo, "worktree", "add", "-qb", "symphony/OMS-1", str(place))
    (place / "tracked.txt").write_text("changed\n")
    (place / "untracked.txt").write_text("keep\n")
    (place / "ignored.bin").write_bytes(b"private ignored data")
    before = snapshot(tmp_path)
    result = command(layout, "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    row = first_row(report)
    assert report["complete"] and report["read_only"] and not report["deletion_assessed"]
    assert row["kind"] == "worktree"
    assert row["repository_matches_board"] is True and row["card_state"] == "Done"
    assert row["git"]["branch"] == "symphony/OMS-1"
    assert row["git"]["head"] == git(repo, "rev-parse", "HEAD")
    assert row["git"]["tracked_changes"] == row["git"]["untracked_entries"] == 1
    assert row["git"]["ignored_entries"] == 1
    assert "private ignored data" not in result.stdout
    assert snapshot(tmp_path) == before
    text = command(layout)
    assert text.returncode == 0 and "только чтение" in text.stdout
    assert "не разрешает удаление" in text.stdout and "Done" in text.stdout
    assert snapshot(tmp_path) == before


def test_unknown_repository_folder_and_service_metadata(tmp_path, layout):
    root, _ = layout
    group = root / "old-demo"
    repository(group / "saved")
    (group / "unknown").mkdir()
    (group / ".symphony-workspace-owners").mkdir()
    (group / ".symphony-workspace-hook-output").mkdir()
    result = command(layout, "--json")
    assert result.returncode == 0
    report = json.loads(result.stdout)
    rows = {Path(row["path"]).name: row for row in report["projects"][0]["entries"]}
    assert rows["saved"]["kind"] == "repository"
    assert rows["saved"]["repository_matches_board"] is None
    assert rows["saved"]["card_state"] is None
    assert "собственная история Git" in " ".join(rows["saved"]["notes"])
    assert rows["unknown"]["kind"] == "directory"
    assert rows[".symphony-workspace-owners"]["kind"] == "service_metadata"
    assert rows[".symphony-workspace-hook-output"]["kind"] == "service_metadata"


@pytest.mark.parametrize("where", ["project", "workspace", "git"])
def test_links_are_not_followed(tmp_path, layout, where):
    root, _ = layout
    outside = repository(tmp_path / "outside")
    (root / "demo").mkdir()
    if where == "project":
        (root / "demo").rmdir()
        (root / "demo").symlink_to(outside, target_is_directory=True)
    elif where == "workspace":
        (root / "demo/saved").symlink_to(outside, target_is_directory=True)
    else:
        (root / "demo/saved").mkdir()
        (root / "demo/saved/.git").symlink_to(outside / ".git", target_is_directory=True)
    before = snapshot(tmp_path)
    result = command(layout, "--json")
    assert result.returncode == 0
    row = first_row(json.loads(result.stdout))
    assert row["kind"] == "linked_path" and row["git"] is None
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("what", ["missing_workspace_root", "linked_workspace_root", "missing_boards"])
def test_invalid_roots_return_failure_json(tmp_path, layout, what):
    root, boards = layout
    if what == "missing_workspace_root":
        root.rmdir()
    elif what == "linked_workspace_root":
        root.rmdir()
        root.symlink_to(tmp_path, target_is_directory=True)
    else:
        boards.rmdir()
    result = command(layout, "--json")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert not report["complete"] and report["errors"]
    assert not report["deletion_assessed"] and "Traceback" not in result.stderr


@pytest.mark.parametrize("content", ['[broken', '[project]\nname = 7\nrepo = "/tmp"',
                                  '[project]\nname = "../outside"\nrepo = "/tmp"',
                                  '[project]\nname = "oms"\nrepo = "relative"'])
def test_bad_board_configuration_is_visible(layout, content):
    root, boards = layout
    (root / "old").mkdir()
    (boards / "bad").mkdir()
    (boards / "bad/project.toml").write_text(content)
    result = command(layout, "--json")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["errors"] and not report["complete"]
    assert len(report["projects"]) == 1


def test_duplicate_board_names_do_not_assign_ownership(tmp_path, layout):
    root, boards = layout
    repo = repository(tmp_path / "repo")
    board(boards, "oms", repo, folder="a")
    board(boards, "oms", repo, folder="b")
    board(boards, "oms", repo, folder="c")
    (root / "oms").mkdir()
    result = command(layout, "--json")
    assert result.returncode == 1
    assert json.loads(result.stdout)["projects"][0]["board"] is None


@pytest.mark.parametrize("content", ["no header", "---\nstate: [\n---\n",
                                   "---\nid: OTHER-1\nstate: Done\n---\n",
                                   "---\nid: OTHER-1\nidentifier: OMS-1\nstate: Done\n---\n"])
def test_bad_card_is_not_treated_as_completed(tmp_path, layout, content):
    root, boards = layout
    repo = repository(tmp_path / "repo")
    b = board(boards, "oms", repo)
    (b / "kanban/OMS-1.md").write_text(content)
    (root / "oms").mkdir()
    git(repo, "worktree", "add", "-qb", "symphony/OMS-1", str(root / "oms/OMS-1"))
    result = command(layout, "--json")
    assert result.returncode == 1
    row = first_row(json.loads(result.stdout))
    assert row["card_state"] is None and row["kind"] == "unreadable"


def test_foreign_repo_does_not_inherit_card_state(tmp_path, layout):
    root, boards = layout
    repo = repository(tmp_path / "repo")
    b = board(boards, "oms", repo)
    (b / "kanban/OMS-1.md").write_text("---\nid: OMS-1\nstate: Done\n---\n")
    repository(root / "oms/OMS-1")
    result = command(layout, "--json")
    assert result.returncode == 0
    row = first_row(json.loads(result.stdout))
    assert row["repository_matches_board"] is False and row["card_state"] is None


@pytest.mark.parametrize("state", ["unborn", "detached"])
def test_unborn_or_detached_repository_is_reported(tmp_path, layout, state):
    root, _ = layout
    place = root / "demo/saved"
    if state == "unborn":
        place.mkdir(parents=True)
        git(place, "init", "-q", "-b", "main")
    else:
        repository(place)
        git(place, "checkout", "--detach", "-q")
    result = command(layout, "--json")
    assert result.returncode == 0, result.stdout
    info = first_row(json.loads(result.stdout))["git"]
    if state == "unborn":
        assert info["head"] is None and info["branch"] == "main"
    else:
        assert info["head"] and info["branch"] is None


def test_rename_with_whitespace_and_newline_path(tmp_path, layout):
    root, _ = layout
    place = repository(root / "demo spaces/saved\nкопия\n")
    git(place, "mv", "tracked.txt", "renamed file.txt")
    result = command(layout, "--json")
    assert result.returncode == 0
    row = first_row(json.loads(result.stdout))
    assert row["path"] == str(place)
    assert row["git"]["tracked_changes"] == 1 and row["git"]["untracked_entries"] == 0


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired(["git"], 10), FileNotFoundError("git")])
def test_failed_git_and_timeout_are_partial_errors(tmp_path, layout, monkeypatch, failure):
    root, boards = layout
    repo = repository(tmp_path / "repo")
    board(boards, "oms", repo)
    repository(root / "oms/saved")
    def fail(*args, **kwargs):
        assert kwargs["timeout"] == wr.GIT_TIMEOUT_S
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
        assert "--no-optional-locks" in args[0]
        raise failure

    monkeypatch.setattr(wr.subprocess, "run", fail)
    report = wr.inventory(root, boards)
    assert not report["complete"] and report["errors"]
    assert first_row(report)["kind"] == "unreadable"


def test_corrupt_git_does_not_hide_other_entries(layout):
    root, _ = layout
    (root / "demo/broken").mkdir(parents=True)
    (root / "demo/broken/.git").write_text("gitdir: missing\n")
    (root / "demo/unknown").mkdir()
    result = command(layout, "--json")
    assert result.returncode == 1
    rows = json.loads(result.stdout)["projects"][0]["entries"]
    assert [row["kind"] for row in rows] == ["unreadable", "directory"]


def test_empty_roots_are_complete_without_creating_metadata(tmp_path, layout):
    before = snapshot(tmp_path)
    result = command(layout, "--json")
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["projects"] == [] and report["complete"]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("where", ["board", "config", "card", "kanban"])
def test_linked_board_data_is_not_read(tmp_path, layout, where):
    root, boards = layout
    repo = repository(tmp_path / "repo")
    b = board(boards, "oms", repo)
    (root / "oms").mkdir()
    git(repo, "worktree", "add", "-qb", "symphony/OMS-1", str(root / "oms/OMS-1"))
    external = tmp_path / "external"
    if where == "board":
        b.rename(external)
        b.symlink_to(external, target_is_directory=True)
    elif where == "config":
        (b / "project.toml").rename(external)
        (b / "project.toml").symlink_to(external)
    elif where == "card":
        external.write_text("---\nid: OMS-1\nstate: Done\n---\n")
        (b / "kanban/OMS-1.md").symlink_to(external)
    else:
        (b / "kanban").rename(external)
        (b / "kanban").symlink_to(external, target_is_directory=True)
    before = snapshot(tmp_path)
    result = command(layout, "--json")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["errors"] and first_row(report)["card_state"] is None
    assert snapshot(tmp_path) == before


def test_only_bounded_card_header_is_read(tmp_path, layout):
    root, boards = layout
    repo = repository(tmp_path / "repo")
    b = board(boards, "oms", repo)
    (root / "oms").mkdir()
    git(repo, "worktree", "add", "-qb", "symphony/OMS-1", str(root / "oms/OMS-1"))
    card = b / "kanban/OMS-1.md"
    card.write_bytes(b'---\nid: OMS-1\ntitle: "note --- here"\nstate: Merging\n---\n\xff')
    result = command(layout, "--json")
    assert result.returncode == 0
    assert first_row(json.loads(result.stdout))["card_state"] == "Merging"
    card.write_bytes(b"---\n" + b"x" * (wr.CARD_HEADER_LIMIT + 1))
    result = command(layout, "--json")
    assert result.returncode == 1 and not json.loads(result.stdout)["complete"]


@pytest.mark.parametrize("filter_kind", ["clean", "process"])
def test_configured_filters_are_not_executed(tmp_path, layout, filter_kind):
    root, _ = layout
    place = repository(root / "demo/saved")
    marker = tmp_path / "filter-ran"
    script = tmp_path / "filter.sh"
    script.write_text("#!/bin/sh\nprintf x >> " + shlex.quote(str(marker)) + "\ncat\n")
    script.chmod(0o755)
    git(place, "config", f"filter.inventory.{filter_kind}", str(script))
    (place / ".gitattributes").write_text("tracked.txt filter=inventory\n")
    (place / "tracked.txt").write_text("changed\n")
    before = snapshot(tmp_path)
    result = command(layout, "--json")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    info = first_row(report)["git"]
    assert info["status_checked"] is False and info["external_filters"] is True
    assert info["tracked_changes"] is info["untracked_entries"] is None
    assert not marker.exists() and snapshot(tmp_path) == before
    assert not report["complete"] and "Git-фильтры" in " ".join(report["errors"])


def test_inherited_git_overrides_and_trace_are_ignored(tmp_path, layout, monkeypatch):
    root, _ = layout
    place = repository(root / "demo/saved")
    foreign = repository(tmp_path / "foreign")
    trace = tmp_path / "trace.log"
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    monkeypatch.setenv("GIT_TRACE", str(trace))
    result = command(layout, "--json")
    assert result.returncode == 0
    info = first_row(json.loads(result.stdout))["git"]
    assert info["common_dir"] == str(place / ".git")
    assert not trace.exists()
