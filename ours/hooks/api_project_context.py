"""Approved project files at the start of a DeepSeek lead's prompt."""

from __future__ import annotations

import hashlib
import re

import api_project as project

MAX_CONTEXT_BYTES = 128 * 1024
MAX_CONTEXT_PATHS = 32
BOARD_ID = re.compile(r"[0-9a-f]{64}")


def paths(value):
    if not isinstance(value, list) or len(value) > MAX_CONTEXT_PATHS:
        raise project.ProjectError("invalid_project_context")
    names = sorted({project._relative(path) for path in value})
    if any(project._closed(path) for path in names):
        raise project.ProjectError("project_scope_denied")
    return names


def validate(value, allowed_paths):
    if (not isinstance(value, dict) or set(value) != {"version", "board", "files"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["board"], str) or not BOARD_ID.fullmatch(value["board"])
            or not isinstance(value["files"], dict) or not value["files"]):
        raise project.ProjectError("invalid_project_context")
    names = paths(list(value["files"]))
    if any(not project._in_scope(path, allowed_paths) for path in names):
        raise project.ProjectError("project_scope_denied")
    files = {path: value["files"][path] for path in names}
    if any(not isinstance(text, str) or "\x00" in text for text in files.values()):
        raise project.ProjectError("invalid_project_context")
    result = {"version": 1, "board": value["board"], "files": files}
    try:
        size = len(project._json(result))
    except UnicodeError:
        raise project.ProjectError("invalid_project_context") from None
    if size > MAX_CONTEXT_BYTES:
        raise project.ProjectError("project_context_too_large")
    return result


def capture(snapshot, board, selection):
    names = paths(selection)
    if any(path not in snapshot["files"] for path in names):
        raise project.ProjectError("project_context_file_unavailable")
    if sum(snapshot["files"][path]["bytes"] for path in names) > MAX_CONTEXT_BYTES:
        raise project.ProjectError("project_context_too_large")
    try:
        files = {path: project._read_text(snapshot, path) for path in names}
    except project.ProjectError as exc:
        if str(exc) == "project_file_not_text":
            raise project.ProjectError("project_context_file_unavailable") from None
        raise
    return validate({"version": 1, "board": board, "files": files}, snapshot["paths"])


def validate_snapshot(value, snapshot):
    for path, text in value["files"].items():
        entry = snapshot["files"].get(path)
        if entry is None or not project._matches_blob(entry, text.encode("utf-8")):
            raise project.ProjectError("invalid_project_context")


def prefix(value):
    return (f"Project board reference v1 ({value['board']}).\n"
            "Use the following committed files as project background. Follow the task's "
            "instructions, file permissions and tool limits below.\n"
            + project._json(value["files"]).decode("utf-8") + "\n\n")


def metadata(value):
    return {"paths": sorted(value["files"]), "sha256": hashlib.sha256(project._json(value)).hexdigest()}
