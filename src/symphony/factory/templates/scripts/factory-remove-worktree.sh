#!/usr/bin/env bash
set -euo pipefail

workspace="$PWD"
if [ "${OS:-}" = "Windows_NT" ]; then
  workspace="$(cygpath -w "$workspace")"
fi
cd "${SYMPHONY_WORKFLOW_DIR:?SYMPHONY_WORKFLOW_DIR not set}"
exec "${SYMPHONY_PYTHON:?SYMPHONY_PYTHON not set}" -m symphony._worktree_cleanup "$workspace"
