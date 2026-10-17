"""Shipped cleanup must detach links before native Git can traverse them."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from symphony._shell import resolve_bash
from symphony import _worktree_cleanup as cleanup_module


ROOT = Path(__file__).resolve().parents[1]
ADVANCED = ("WORKFLOW.md", "WORKFLOW.example.md", "examples/advanced/WORKFLOW.file.example.md")


def _git(path, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(path), *args], env=env, check=True,
        capture_output=True, text=True,
    )


def _link(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)],
            check=True, capture_output=True,
        )
        assert path.is_junction()
    else:
        path.symlink_to(target, target_is_directory=True)


def _prepare(tmp_path, monkeypatch, boards=("kanban",)):
    env = {
        "GIT_CONFIG_GLOBAL": str(tmp_path / "unused-config"), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Cleanup QA", "GIT_AUTHOR_EMAIL": "cleanup@qa.invalid",
        "GIT_COMMITTER_NAME": "Cleanup QA", "GIT_COMMITTER_EMAIL": "cleanup@qa.invalid",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    host = tmp_path / "host project"
    host.mkdir()
    _git(host, "init", "-b", "main")
    (host / "README.md").write_bytes(b"preserve host bytes\x00\xff")
    for board_name in boards:
        board = host / board_name
        board.mkdir(parents=True)
        (board / "TASK-1.md").write_bytes("state: Done\r\n한글\r\n".encode())
        (board / "TASK-2.md").write_bytes(b"state: Ready\n")
    _git(host, "add", ".")
    _git(host, "commit", "-m", "seed owned fixture")
    root = tmp_path / "workspaces"
    workspace = root / "TASK-1"
    root.mkdir()
    _git(host, "worktree", "add", "-b", "symphony/TASK-1", str(workspace))
    for board_name in boards:
        local = workspace / board_name
        for child in local.iterdir():
            child.unlink()
        local.rmdir()
        _link(local, host / board_name)
        _git(host, "update-index", "--skip-worktree", "--", f"{board_name}/TASK-1.md")
    external = tmp_path / "external data"
    external.mkdir()
    (external / "keep.bin").write_bytes(b"preserve external bytes\x00\xff")
    _link(workspace / "node_modules/cache", external)
    expected = {str(path.relative_to(host)): path.read_bytes() for path in host.rglob("*")
                if path.is_file() and ".git" not in path.relative_to(host).parts}
    monkeypatch.setenv("SYMPHONY_WORKFLOW_DIR", str(host))
    monkeypatch.setenv("SYMPHONY_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("SYMPHONY_PYTHON", sys.executable)
    monkeypatch.setenv("SYMPHONY_BOARD_ROOT_NAME", " ".join(boards))
    monkeypatch.setenv("SYMPHONY_ISSUE_ID", "TASK-1")
    return host, root, workspace, external, expected


def _hook(workflow):
    frontmatter = (ROOT / workflow).read_text(encoding="utf-8").split("---", 2)[1]
    return yaml.safe_load(frontmatter)["hooks"]["before_remove"]


@pytest.mark.parametrize("workflow", ADVANCED)
@pytest.mark.parametrize("boards", [("kanban",), ("custom-board", "boards/release")])
def test_shipped_cleanup_preserves_link_targets(tmp_path, monkeypatch, workflow, boards):
    host, _, workspace, external, expected = _prepare(tmp_path, monkeypatch, boards)
    result = subprocess.run(
        [resolve_bash(), "-c", _hook(workflow)], cwd=workspace,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert expected == {name: (host / name).read_bytes() for name in expected}
    assert (external / "keep.bin").read_bytes() == b"preserve external bytes\x00\xff"
    assert str(workspace).replace("\\", "/") not in _git(host, "worktree", "list", "--porcelain").stdout
    for name in boards:
        assert _git(host, "ls-files", "-v", "--", f"{name}/TASK-1.md").stdout.startswith("H ")


def _watch_mutations(monkeypatch):
    calls = []
    run = subprocess.run

    def watch(args, **kwargs):
        if any(command in args for command in ("remove", "prune", "update-index", "update-ref", "add")):
            calls.append(args)
        return run(args, **kwargs)

    monkeypatch.setattr(cleanup_module.subprocess, "run", watch)
    return calls


@pytest.mark.parametrize("workflow", ADVANCED)
def test_shipped_hook_propagates_wrong_target_refusal(tmp_path, monkeypatch, workflow):
    host, _, workspace, external, expected = _prepare(tmp_path, monkeypatch)
    board = workspace / "kanban"
    if board.is_junction():
        board.rmdir()
    else:
        board.unlink()
    _link(board, external)
    result = subprocess.run(
        [resolve_bash(), "-c", _hook(workflow)], cwd=workspace,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert "does not target the host board" in result.stderr
    assert (workspace / ".git").is_file()
    assert expected == {name: (host / name).read_bytes() for name in expected}
    assert (external / "keep.bin").read_bytes() == b"preserve external bytes\x00\xff"


@pytest.mark.parametrize("unsafe", ["real", "wrong", "dangling", "outside-board"])
def test_all_boards_are_validated_before_any_mutation(tmp_path, monkeypatch, unsafe):
    host, _, workspace, external, expected = _prepare(
        tmp_path, monkeypatch, ("kanban", "custom-board"),
    )
    board = workspace / "custom-board"
    if board.is_junction():
        board.rmdir()
    else:
        board.unlink()
    if unsafe == "real":
        board.mkdir()
    else:
        _link(board, external)
        if unsafe == "dangling":
            (external / "keep.bin").unlink()
            external.rmdir()
        elif unsafe == "outside-board":
            monkeypatch.setenv("SYMPHONY_BOARD_ROOT_NAME", "kanban ../host")
    mutations = _watch_mutations(monkeypatch)
    with pytest.raises(RuntimeError):
        cleanup_module.cleanup(workspace, restore_board_tracking=True)
    assert not mutations
    assert (workspace / "kanban").exists()
    assert (workspace / ".git").is_file()
    assert expected == {name: (host / name).read_bytes() for name in expected}


@pytest.mark.parametrize("unsafe", ["host", "root", "outside", "unregistered", "other-repo", "wrong-common-dir"])
def test_cleanup_refuses_unowned_paths_before_mutation(tmp_path, monkeypatch, unsafe):
    host, root, workspace, _, expected = _prepare(tmp_path, monkeypatch)
    if unsafe == "host":
        candidate = host
    elif unsafe == "root":
        candidate = root
    elif unsafe == "outside":
        candidate = tmp_path / "outside"
        candidate.mkdir()
    elif unsafe == "unregistered":
        candidate = root / "not-a-worktree"
        candidate.mkdir()
    else:
        other = tmp_path / "other repo"
        other.mkdir()
        _git(other, "init", "-b", "main")
        (other / "keep").write_text("other repository")
        _git(other, "add", ".")
        _git(other, "commit", "-m", "other seed")
        candidate = root / "OTHER"
        _git(other, "worktree", "add", "-b", "other", str(candidate))
        if unsafe == "wrong-common-dir":
            # Git marks this owned metadata file hidden on Windows; replace it
            # rather than opening the hidden file with truncation semantics.
            (workspace / ".git").unlink()
            (workspace / ".git").write_bytes((candidate / ".git").read_bytes())
            candidate = workspace
    mutations = _watch_mutations(monkeypatch)
    with pytest.raises(RuntimeError):
        cleanup_module.cleanup(candidate)
    assert not mutations
    assert (workspace / ".git").is_file()
    assert expected == {name: (host / name).read_bytes() for name in expected}


def test_denied_detach_stops_before_git_remove(tmp_path, monkeypatch):
    host, _, workspace, external, expected = _prepare(tmp_path, monkeypatch)
    board = workspace / "kanban"
    method = "rmdir" if board.is_junction() else "unlink"
    original = getattr(Path, method)

    def deny(path, *args, **kwargs):
        if path == board:
            raise PermissionError("owned test denies board detachment")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, deny)
    mutations = _watch_mutations(monkeypatch)
    with pytest.raises(PermissionError, match="denies board"):
        cleanup_module.cleanup(workspace)
    assert not mutations
    assert board.exists()
    assert (workspace / ".git").is_file()
    assert expected == {name: (host / name).read_bytes() for name in expected}
    assert (external / "keep.bin").read_bytes() == b"preserve external bytes\x00\xff"


def test_scan_denial_stops_before_detach_or_git_remove(tmp_path, monkeypatch):
    _, _, workspace, _, _ = _prepare(tmp_path, monkeypatch)

    def deny_scan(*args, **kwargs):
        kwargs["onerror"](PermissionError("owned test denies directory scan"))
        return iter(())

    monkeypatch.setattr(cleanup_module.os, "walk", deny_scan)
    mutations = _watch_mutations(monkeypatch)
    with pytest.raises(PermissionError, match="denies directory scan"):
        cleanup_module.cleanup(workspace)
    assert not mutations
    assert (workspace / "kanban").exists()
    assert (workspace / ".git").is_file()


def test_linked_worktree_root_is_refused_even_when_target_is_missing(tmp_path, monkeypatch):
    _, root, workspace, _, _ = _prepare(tmp_path, monkeypatch)
    candidate = root / "linked-worktree"
    _link(candidate, workspace)
    moved = workspace.with_name("moved-linked-target")
    workspace.rename(moved)
    mutations = _watch_mutations(monkeypatch)
    with pytest.raises(RuntimeError, match="linked worktree root"):
        cleanup_module.cleanup(candidate)
    assert not mutations
    assert (moved / ".git").is_file()


def test_retry_accepts_detached_board_and_then_missing_unregistered_path(tmp_path, monkeypatch):
    host, _, workspace, external, expected = _prepare(tmp_path, monkeypatch)
    board = workspace / "kanban"
    if board.is_junction():
        board.rmdir()
    else:
        board.unlink()
    cleanup_module.cleanup(workspace)
    cleanup_module.cleanup(workspace)
    assert not workspace.exists()
    assert expected == {name: (host / name).read_bytes() for name in expected}
    assert (external / "keep.bin").read_bytes() == b"preserve external bytes\x00\xff"
    # Factory cleanup preserves its old behavior: it does not reset the host index.
    assert _git(host, "ls-files", "-v", "--", "kanban/TASK-1.md").stdout.startswith("S ")


def test_missing_but_registered_worktree_is_refused(tmp_path, monkeypatch):
    host, _, workspace, _, _ = _prepare(tmp_path, monkeypatch)
    moved = workspace.with_name("moved-for-missing-path-test")
    workspace.rename(moved)
    mutations = _watch_mutations(monkeypatch)
    with pytest.raises(RuntimeError, match="missing registered"):
        cleanup_module.cleanup(workspace)
    assert not mutations
    assert str(workspace).replace("\\", "/") in _git(host, "worktree", "list", "--porcelain").stdout


@pytest.mark.parametrize("returncode, registered, nonempty", [(255, False, False), (255, True, False), (255, False, True), (1, False, False)])
def test_git_error_only_accepts_windows_empty_unregistered_255(
    tmp_path, monkeypatch, returncode, registered, nonempty,
):
    host, _, workspace, _, expected = _prepare(tmp_path, monkeypatch)
    run = subprocess.run
    real_registered = cleanup_module._registered
    removed = False

    def simulate(args, **kwargs):
        nonlocal removed
        if "remove" in args:
            result = run(args, **kwargs)
            assert result.returncode == 0
            workspace.mkdir()
            if nonempty:
                (workspace / "unexpected").write_text("keep")
            removed = True
            return subprocess.CompletedProcess(args, returncode)
        return run(args, **kwargs)

    monkeypatch.setattr(cleanup_module.subprocess, "run", simulate)
    monkeypatch.setattr(
        cleanup_module, "_registered",
        lambda h, w: registered if removed else real_registered(h, w),
    )
    accepted = os.name == "nt" and returncode == 255 and not registered and not nonempty
    if accepted:
        cleanup_module.cleanup(workspace)
    else:
        with pytest.raises(subprocess.CalledProcessError):
            cleanup_module.cleanup(workspace)
    assert expected == {name: (host / name).read_bytes() for name in expected}


@pytest.mark.skipif(os.name != "nt", reason="native Git long-path removal")
def test_native_cleanup_removes_deep_venv_leaf_without_global_config(tmp_path, monkeypatch):
    host, _, workspace, external, expected = _prepare(tmp_path, monkeypatch)
    _git(host, "config", "core.longpaths", "false")
    leaf = workspace / ".venv/Lib/site-packages" / ("package" * 15) / ("nested" * 14) / "keep.txt"
    assert len(str(leaf)) > 260
    leaf.parent.mkdir(parents=True)
    leaf.write_bytes(b"owned deep leaf")
    cleanup_module.cleanup(workspace)
    assert not workspace.exists()
    assert not cleanup_module._registered(host, workspace)
    assert _git(host, "config", "core.longpaths").stdout.strip() == "false"
    assert expected == {name: (host / name).read_bytes() for name in expected}
    assert (external / "keep.bin").read_bytes() == b"preserve external bytes\x00\xff"
