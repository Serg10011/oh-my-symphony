"""Private cleanup shared by the shipped factory and advanced workflow hooks."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


def _common_dir(path: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
        check=True, stdout=subprocess.PIPE, text=True,
    )
    return (path / result.stdout.strip()).resolve()


def _worktrees(host: Path) -> list[dict[str, str]]:
    listing = subprocess.run(
        ["git", "-C", str(host), "worktree", "list", "--porcelain", "-z"],
        check=True, stdout=subprocess.PIPE, text=True,
    ).stdout
    records: list[dict[str, str]] = []
    for block in listing.split("\0\0"):
        record = {}
        for field in block.split("\0"):
            if field:
                key, _, value = field.partition(" ")
                record[key] = value
        if record:
            if "worktree" not in record:
                raise RuntimeError("cleanup refuses an invalid worktree listing")
            records.append(record)
    return records


def _registered(host: Path, workspace: Path) -> bool:
    return any(Path(record["worktree"]).resolve() == workspace for record in _worktrees(host))


def _paths(workspace: Path) -> tuple[Path, Path]:
    host = Path(os.environ["SYMPHONY_WORKFLOW_DIR"]).resolve()
    root = Path(os.environ["SYMPHONY_WORKSPACE_ROOT"]).resolve()
    if workspace.is_symlink() or workspace.is_junction():
        raise RuntimeError("cleanup refuses a linked worktree root")
    workspace = workspace.resolve()
    if workspace in {host, root} or workspace.parent != root:
        raise RuntimeError("cleanup refuses the host, workspace root, or an outside path")
    return host, workspace


def cleanup(workspace: Path, *, restore_board_tracking: bool = False) -> None:
    host, workspace = _paths(workspace)
    registered = _registered(host, workspace)
    if not workspace.exists():
        if registered:
            raise RuntimeError("cleanup refuses a missing registered worktree or dangling link")
        return
    if not registered or _common_dir(workspace) != _common_dir(host):
        raise RuntimeError("cleanup workspace is not a registered worktree of the host repository")
    _cleanup_registered(host, workspace, restore_board_tracking=restore_board_tracking)


def prepare_setup(workspace: Path, expected_branch: str) -> None:
    """Prepare only an exact ticket target before setup's ref CAS and ordinary add."""
    host, workspace = _paths(workspace)
    records = _worktrees(host)
    exact = [record for record in records if Path(record["worktree"]).resolve() == workspace]
    expected_ref = f"refs/heads/{expected_branch}"
    if any(record.get("branch") == expected_ref and record not in exact for record in records):
        raise RuntimeError("setup refuses a feature branch checked out in another worktree")
    if len(exact) > 1:
        raise RuntimeError("setup refuses ambiguous worktree registration")
    # Validate configured board paths even for fresh/empty targets, before add.
    _board_links(host, workspace)
    if not exact:
        if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
            raise RuntimeError("setup refuses a nonempty unregistered workspace")
        return
    record = exact[0]
    if "locked" in record or record.get("branch") != expected_ref:
        raise RuntimeError("setup refuses a locked worktree or unexpected feature branch")
    if (workspace / ".git").is_file():
        if _common_dir(workspace) != _common_dir(host):
            raise RuntimeError("setup workspace belongs to another repository")
        branch = subprocess.run(
            ["git", "-C", str(workspace), "symbolic-ref", "--quiet", "HEAD"],
            check=True, stdout=subprocess.PIPE, text=True,
        ).stdout.strip()
        if branch != expected_ref:
            raise RuntimeError("setup worktree has an unexpected feature branch")
        _cleanup_registered(host, workspace, restore_board_tracking=True)
        return
    if "prunable" not in record or (workspace.exists() and (
        not workspace.is_dir() or any(workspace.iterdir())
    )):
        raise RuntimeError("setup refuses a nonempty or non-prunable stale worktree")
    if workspace.exists():
        # Preserve default prune semantics and the empty directory held by outer Bash.
        subprocess.run(["git", "-C", str(host), "worktree", "prune"], cwd=host, check=True)
    else:
        _remove(host, workspace)
    if _registered(host, workspace):
        raise RuntimeError("setup stale worktree registration remains after preparation")


def _board_links(host: Path, workspace: Path) -> tuple[list[str], list[Path]]:
    names = (os.environ.get("SYMPHONY_BOARD_ROOT_NAME") or "kanban").split()
    boards: list[Path] = []
    for name in names:
        relative = Path(name)
        if relative.is_absolute() or relative.drive or ".." in relative.parts or not relative.parts:
            raise RuntimeError("cleanup refuses an invalid board path")
        board = workspace / relative
        if board == workspace or not board.parent.resolve().is_relative_to(workspace):
            raise RuntimeError("cleanup refuses a board path outside the worktree")
        if board.exists() or board.is_symlink() or board.is_junction():
            if not (board.is_symlink() or board.is_junction()):
                raise RuntimeError("cleanup refuses a real board directory")
            source = host / relative
            if not board.exists() or not source.is_dir() or board.resolve() != source.resolve():
                raise RuntimeError("cleanup board link does not target the host board")
            if board not in boards:
                boards.append(board)

    return names, boards


def _cleanup_registered(host: Path, workspace: Path, *, restore_board_tracking: bool) -> None:
    names, boards = _board_links(host, workspace)
    # Scan without entering junctions, before mutating any of the validated entries.
    junctions: list[Path] = []

    def scan_error(error: OSError) -> None:
        raise error

    for directory, children, _ in os.walk(
        workspace, topdown=True, followlinks=False, onerror=scan_error,
    ):
        for name in list(children):
            child = Path(directory) / name
            if child.is_junction():
                if child not in boards:
                    junctions.append(child)
                children.remove(name)

    if restore_board_tracking:
        issue_id = os.environ.get("SYMPHONY_ISSUE_ID") or workspace.name
        for name in names:
            subprocess.run(
                ["git", "-C", str(host), "update-index", "--no-skip-worktree", "--",
                 f"{name}/{issue_id}.md"],
                check=False, stderr=subprocess.DEVNULL,
            )
    for board in boards:
        if board.is_junction():
            board.rmdir()
        else:
            board.unlink()
    # Git for Windows may traverse directory junctions even when Python would not.
    for junction in junctions:
        junction.rmdir()
    _remove(host, workspace)


def _remove(host: Path, workspace: Path) -> None:
    options = ["-c", "core.longpaths=true"] if os.name == "nt" else []
    result = subprocess.run(
        ["git", *options, "-C", str(host), "worktree", "remove", "--force", str(workspace)],
        cwd=host, check=False,
    )
    if result.returncode:
        registered = _registered(host, workspace)
        empty = not workspace.exists() or not any(workspace.iterdir())
        if os.name == "nt" and result.returncode == 255 and empty and not registered:
            print(
                "cleanup: Git removed the worktree contents and registration; "
                "the hook holds its empty directory until exit, then Symphony removes it",
                file=sys.stderr,
            )
        else:
            raise subprocess.CalledProcessError(result.returncode, result.args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--restore-board-tracking", action="store_true")
    parser.add_argument("--prepare-setup", metavar="EXPECTED_BRANCH")
    args = parser.parse_args()
    if args.prepare_setup is not None:
        prepare_setup(args.workspace, args.prepare_setup)
    else:
        cleanup(args.workspace, restore_board_tracking=args.restore_board_tracking)


if __name__ == "__main__":
    main()
