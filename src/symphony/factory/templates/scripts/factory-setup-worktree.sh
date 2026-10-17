#!/usr/bin/env bash
set -euo pipefail

host="${SYMPHONY_WORKFLOW_DIR:?SYMPHONY_WORKFLOW_DIR not set}"
host="$(cd "$host" && pwd -P)"
issue="$(basename "$PWD")"
branch="symphony/${issue}"
base_sha="$(git -C "$host" rev-parse HEAD)"
base_branch="$(git -C "$host" branch --show-current)"

current_branch="$(git branch --show-current 2>/dev/null || true)"
if [ "$current_branch" != "$branch" ]; then
  if git -C "$host" show-ref --verify --quiet "refs/heads/$branch"; then
    git -C "$host" worktree add --force "$PWD" "$branch"
  else
    git -C "$host" worktree add --force -b "$branch" "$PWD" HEAD
  fi
fi

git -C "$host" config extensions.worktreeConfig true
if ! git config --worktree --get symphony.basesha >/dev/null; then
  git config --worktree symphony.basesha "$base_sha"
fi
if ! git config --worktree --get symphony.basebranch >/dev/null; then
  git config --worktree symphony.basebranch "$base_branch"
fi
git config --worktree --replace-all symphony.autocommitExclude kanban
if [ -L "$PWD/kanban" ] || [ "$PWD/kanban" -ef "$host/kanban" ]; then
  if [ ! "$PWD/kanban" -ef "$host/kanban" ]; then
    echo "factory setup: kanban symlink does not point to the host board" >&2
    exit 1
  fi
elif [ -d "$PWD/kanban" ]; then
  # A checkout of the default template contains only this tracked sentinel.
  # It carries no board state, so it is the sole real entry safe to replace.
  if [ -f "$PWD/kanban/.gitkeep" ] && [ ! -L "$PWD/kanban/.gitkeep" ] \
    && ! find "$PWD/kanban" -mindepth 1 -maxdepth 1 ! -name .gitkeep -print -quit | grep -q .; then
    rm -- "$PWD/kanban/.gitkeep"
  fi
  if find "$PWD/kanban" -mindepth 1 -print -quit | grep -q .; then
    echo "factory setup: nonempty real kanban directory blocks the shared board link" >&2
    echo "untrack kanban/*.md, keep the host cards, then recreate this workspace" >&2
    exit 1
  fi
  rmdir "$PWD/kanban"
elif [ -e "$PWD/kanban" ]; then
  echo "factory setup: kanban exists but is not a directory or symlink" >&2
  exit 1
fi
if [ ! -e "$PWD/kanban" ] && [ ! -L "$PWD/kanban" ]; then
  if [ "${OS:-}" = "Windows_NT" ] && command -v cmd.exe >/dev/null 2>&1; then
    # Git Bash ln -s can silently copy; junctions share the live board without elevation.
    target_win="$(cygpath -w "$PWD/kanban")"
    source_win="$(cygpath -w "$host/kanban")"
    bat="$(mktemp "${TEMP:-/tmp}/symphony-factory-link-XXXXXX.bat")"
    trap 'rm -f "$bat"' EXIT
    printf '@echo off\r\nmklink /J %%1 %%2\r\n' > "$bat"
    cmd.exe //c "$(cygpath -w "$bat")" "$target_win" "$source_win" >/dev/null
    rm -f "$bat"
    trap - EXIT
  else
    ln -s "$host/kanban" "$PWD/kanban"
  fi
fi
if [ ! "$PWD/kanban" -ef "$host/kanban" ]; then
  echo "factory setup: shared board link does not point to the host board" >&2
  exit 1
fi
