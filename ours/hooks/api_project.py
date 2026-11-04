"""Opt-in, read-only access to explicitly granted, immutable Git blobs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import subprocess
import time

MAX_TREE_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 10000
MAX_REPLY_BYTES = 32768
MAX_CHARS = 4096
GIT_TIMEOUT = 10
GIT = shutil.which("git")
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
CLOSED_DIRS = frozenset({".git", ".ssh", ".aws", ".azure", ".kube", ".gnupg", ".codex",
                        ".claude", ".symphony", ".ours-state", ".venv", "node_modules", "__pycache__",
                        "tmp_workspaces", "symphony_workspaces"})
CLOSED_NAMES = frozenset({"credentials", "credentials.json", "secrets", "secrets.json", "secrets.toml",
                         "secrets.yaml", "secrets.yml", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
                         ".netrc", ".git-credentials", ".npmrc", ".pypirc", ".mcp.json"})
CLOSED_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
TOOL_NAMES = frozenset({"list_project_files", "read_project_file", "search_project_files"})
TOOLS = [
    {"type": "function", "name": "list_project_files",
     "description": "List granted committed project files. Use prefix='' and offset=0 initially; follow next_offset.",
     "parameters": {"type": "object", "properties": {
         "prefix": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}},
         "required": ["prefix", "offset"], "additionalProperties": False}, "strict": True},
    {"type": "function", "name": "read_project_file",
     "description": "Read a UTF-8 file from the fixed project commit. Offsets count Unicode characters, not bytes.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
         "max_chars": {"type": "integer", "minimum": 1, "maximum": MAX_CHARS}},
         "required": ["path", "offset", "max_chars"], "additionalProperties": False}, "strict": True},
]
LEGACY_TOOLS = list(TOOLS)
TOOLS.append({"type": "function", "name": "search_project_files",
    "description": "Search literal strings only in granted files of the fixed commit. Results include line numbers and bounded snippets. Start file_offset=0,line_offset=0; continue using next_cursor. No regular expressions or commands.",
    "parameters": {"type": "object", "properties": {
        "queries": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 256}, "minItems": 1, "maxItems": 8},
        "prefix": {"type": "string"}, "case_sensitive": {"type": "boolean"},
        "file_offset": {"type": "integer", "minimum": 0}, "line_offset": {"type": "integer", "minimum": 0}},
        "required": ["queries", "prefix", "case_sensitive", "file_offset", "line_offset"],
        "additionalProperties": False}, "strict": True})


class ProjectError(Exception):
    """A fixed error code; never expose Git diagnostics, host paths or credentials."""


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _relative(value: str, *, root=False) -> str:
    if root and value == ".":
        return value
    if (not isinstance(value, str) or not value or len(value) > 1024 or "\\" in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ProjectError("invalid_project_path")
    path = PurePosixPath(value)
    try:
        valid = len(value.encode("utf-8")) <= 1024
    except UnicodeError:
        valid = False
    if not valid or path.is_absolute() or path.as_posix() != value or any(p in {"", ".", ".."} for p in value.split("/")):
        raise ProjectError("invalid_project_path")
    return value


def _closed(path: str) -> bool:
    parts = [p.lower() for p in path.split("/")]
    return (any(p in CLOSED_DIRS or p in CLOSED_NAMES or p.startswith(".env") for p in parts)
            or parts[-1].endswith(CLOSED_SUFFIXES))


def _in_scope(path: str, scopes: list[str]) -> bool:
    return any(p == "." or path == p or path.startswith(p + "/") for p in scopes)


def validate(raw: dict) -> dict:
    if not isinstance(raw, dict) or set(raw) - {"root", "paths", "commit"} or not {"root", "paths"} <= set(raw):
        raise ProjectError("invalid_project_spec")
    root, paths, commit = raw["root"], raw["paths"], raw.get("commit", "HEAD")
    if (not isinstance(root, str) or len(root) > 4096 or "\x00" in root or not Path(root).is_absolute()
            or ".." in Path(root).parts or not isinstance(paths, list) or not 1 <= len(paths) <= 32
            or not isinstance(commit, str) or (commit != "HEAD" and not OID.fullmatch(commit))):
        raise ProjectError("invalid_project_spec")
    try:
        root.encode("utf-8")
        scopes = sorted({_relative(p, root=True) for p in paths})
    except (UnicodeError, TypeError):
        raise ProjectError("invalid_project_spec") from None
    if any(_closed(p) for p in scopes):
        raise ProjectError("project_scope_denied")
    return {"root": str(Path(root)), "paths": scopes, "commit": commit}


def _git(root: str, args: list[str], limit: int) -> bytes:
    # Only host-constructed read operations reach this function, never model command text.
    if not GIT or not args or args[0] not in {"rev-parse", "ls-tree", "cat-file"}:
        raise ProjectError("project_git_unavailable")
    command = [GIT, "--no-pager", "--no-replace-objects", "--no-optional-locks", "--literal-pathspecs",
               "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
               "-c", "core.attributesFile=/dev/null", "-c", "credential.helper=", "-C", root, *args]
    # Do not inherit API keys, Git overrides, trace destinations or credential helpers.
    # Protocol denial also prevents a lazy fetch on older Git versions that ignore NO_LAZY_FETCH.
    env = {"PATH": os.defpath, "LC_ALL": "C", "HOME": "/nonexistent",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
    process = None
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + GIT_TIMEOUT
    data = bytearray()
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env, start_new_session=True)
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise ProjectError("project_git_timeout")
            block = os.read(process.stdout.fileno(), min(65536, limit - len(data) + 1))
            if not block:
                break
            data.extend(block)
            if len(data) > limit:
                raise ProjectError("project_output_limit")
        if process.wait(timeout=max(.01, deadline - time.monotonic())) != 0:
            raise ProjectError("project_git_failed")
        return bytes(data)
    except (OSError, subprocess.TimeoutExpired):
        raise ProjectError("project_git_failed") from None
    finally:
        selector.close()
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    # macOS can report EPERM for an already exiting process group.
                    # The unreaped child PID is still ours, so direct kill is safe.
                    try:
                        process.kill()
                    except (ProcessLookupError, PermissionError):
                        pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                raise ProjectError("project_git_failed") from None


def _digest(snapshot: dict) -> str:
    return hashlib.sha256(_json({k: v for k, v in snapshot.items() if k != "digest"})).hexdigest()


def snapshot(raw: dict) -> tuple[dict, dict]:
    spec = validate(raw)
    try:
        commit = _git(spec["root"], ["rev-parse", "--verify", spec["commit"] + "^{commit}"], 256).decode("ascii").strip()
    except UnicodeError:
        raise ProjectError("invalid_project_commit") from None
    if not OID.fullmatch(commit):
        raise ProjectError("invalid_project_commit")
    spec["commit"] = commit
    args = ["ls-tree", "-r", "-l", "-z", "--full-tree", commit]
    if "." not in spec["paths"]:
        args.extend(["--", *spec["paths"]])
    entries = _git(spec["root"], args, MAX_TREE_BYTES)
    files = {}
    for entry in entries.split(b"\x00"):
        if not entry:
            continue
        try:
            meta, name = entry.split(b"\t", 1)
            mode, kind, oid, size = meta.split()
            if kind != b"blob" or mode not in {b"100644", b"100755"}:
                continue
            path = _relative(name.decode("utf-8"))
            oid, size = oid.decode("ascii"), int(size)
        except (ValueError, UnicodeError, ProjectError):
            raise ProjectError("invalid_project_tree") from None
        if _closed(path) or not _in_scope(path, spec["paths"]) or size > MAX_FILE_BYTES:
            continue
        if not OID.fullmatch(oid) or size < 0:
            raise ProjectError("invalid_project_tree")
        files[path] = {"oid": oid, "bytes": size}
        if len(files) > MAX_FILES:
            raise ProjectError("project_file_limit")
    if not files:
        raise ProjectError("no_project_files")
    result = {"version": 1, **spec, "files": dict(sorted(files.items()))}
    result["digest"] = _digest(result)
    validate_snapshot(result, spec)
    return spec, result


def validate_snapshot(value: dict, spec: dict) -> None:
    if (not isinstance(value, dict) or set(value) != {"version", "root", "paths", "commit", "files", "digest"}
            or type(value["version"]) is not int or value["version"] != 1
            or any(value[k] != spec[k] for k in ["root", "paths", "commit"])
            or not OID.fullmatch(value["commit"]) or not isinstance(value["files"], dict)
            or not 1 <= len(value["files"]) <= MAX_FILES):
        raise ProjectError("invalid_project_state")
    for path, entry in value["files"].items():
        try:
            safe = _relative(path) == path and not _closed(path) and _in_scope(path, spec["paths"])
        except ProjectError:
            safe = False
        if (not safe or not isinstance(entry, dict) or set(entry) != {"oid", "bytes"}
                or not isinstance(entry["oid"], str) or not OID.fullmatch(entry["oid"])
                or len(entry["oid"]) != len(value["commit"]) or type(entry["bytes"]) is not int
                or not 0 <= entry["bytes"] <= MAX_FILE_BYTES):
            raise ProjectError("invalid_project_state")
    if value["digest"] != _digest(value):
        raise ProjectError("invalid_project_state")


def _number(value, maximum: int) -> bool:
    return type(value) is int and 0 <= value <= maximum


def call(snapshot: dict, name: str, args: dict) -> dict:
    if not isinstance(args, dict):
        raise ProjectError("invalid_project_query")
    if name == "search_project_files":
        return _search(snapshot, args)
    if name == "list_project_files":
        if (set(args) != {"prefix", "offset"} or not _number(args["offset"], MAX_FILES)
                or not isinstance(args["prefix"], str)):
            raise ProjectError("invalid_project_query")
        prefix = args["prefix"]
        if prefix:
            _relative(prefix)
        names = [p for p in snapshot["files"] if not prefix or p == prefix or p.startswith(prefix + "/")]
        offset = args["offset"]
        if offset > len(names):
            raise ProjectError("invalid_project_range")
        output = {"commit": snapshot["commit"], "files": [], "total": len(names), "next_offset": None}
        for path in names[offset:offset + 100]:
            output["files"].append({"path": path, "bytes": snapshot["files"][path]["bytes"]})
            if len(_json(output)) > MAX_REPLY_BYTES - 32:
                output["files"].pop()
                break
        end = offset + len(output["files"])
        output["next_offset"] = end if end < len(names) else None
        return output
    if name != "read_project_file" or set(args) != {"path", "offset", "max_chars"}:
        raise ProjectError("invalid_project_query")
    path = _relative(args["path"])
    if (not _number(args["offset"], MAX_FILE_BYTES) or not _number(args["max_chars"], MAX_CHARS)
            or args["max_chars"] == 0):
        raise ProjectError("invalid_project_query")
    text = _read_text(snapshot, path)
    start = args["offset"]
    if start > len(text):
        raise ProjectError("invalid_project_range")
    end = min(len(text), start + args["max_chars"])
    return {"path": path, "commit": snapshot["commit"], "offset": start, "next_offset": end,
            "eof": end == len(text), "text": text[start:end]}


def _read_text(snapshot, path):
    entry = snapshot["files"].get(path)
    if entry is None:
        raise ProjectError("project_file_unavailable")
    data = _git(snapshot["root"], ["cat-file", "blob", entry["oid"]], entry["bytes"])
    algorithm = hashlib.sha1 if len(entry["oid"]) == 40 else hashlib.sha256
    actual = algorithm(b"blob " + str(len(data)).encode("ascii") + b"\x00" + data).hexdigest()
    if len(data) != entry["bytes"] or actual != entry["oid"]:
        raise ProjectError("project_content_changed")
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise ProjectError("project_file_not_text") from None
    if "\x00" in text:
        raise ProjectError("project_file_not_text")
    return text


def _search(snapshot, args):
    if (set(args) != {"queries", "prefix", "case_sensitive", "file_offset", "line_offset"}
            or not isinstance(args["queries"], list) or not 1 <= len(args["queries"]) <= 8
            or any(not isinstance(q, str) or not 1 <= len(q) <= 256 or "\x00" in q for q in args["queries"])
            or not isinstance(args["prefix"], str) or type(args["case_sensitive"]) is not bool
            or not _number(args["file_offset"], MAX_FILES) or not _number(args["line_offset"], MAX_FILE_BYTES)):
        raise ProjectError("invalid_project_query")
    prefix = args["prefix"]
    if prefix:
        _relative(prefix)
    names = sorted(p for p in snapshot["files"] if not prefix or p == prefix or p.startswith(prefix + "/"))
    start, line_start = args["file_offset"], args["line_offset"]
    if start > len(names) or (start == len(names) and line_start):
        raise ProjectError("invalid_project_range")
    queries = args["queries"] if args["case_sensitive"] else [q.casefold() for q in args["queries"]]
    output = {"commit": snapshot["commit"], "matches": [], "files_total": len(names),
              "files_scanned": 0, "skipped_nontext": 0, "next_cursor": None}
    scanned_bytes = 0
    for index in range(start, len(names)):
        path = names[index]
        if output["files_scanned"] >= 100 or scanned_bytes + snapshot["files"][path]["bytes"] > 4 * MAX_FILE_BYTES:
            output["next_cursor"] = {"file_offset": index, "line_offset": 0}
            return output
        scanned_bytes += snapshot["files"][path]["bytes"]
        output["files_scanned"] += 1
        try:
            text = _read_text(snapshot, path)
        except ProjectError as exc:
            if str(exc) != "project_file_not_text":
                raise
            output["skipped_nontext"] += 1
            line_start = 0
            continue
        lines = text.splitlines()
        if line_start > len(lines):
            raise ProjectError("invalid_project_range")
        for number in range(line_start, len(lines)):
            line = lines[number]
            candidate = line if args["case_sensitive"] else line.casefold()
            if not any(q in candidate for q in queries):
                continue
            found = min(candidate.find(q) for q in queries if q in candidate)
            if not args["case_sensitive"]:
                folded = 0
                for position, character in enumerate(line):
                    folded += len(character.casefold())
                    if folded > found:
                        found = position
                        break
            begin = max(0, found - 80)
            item = {"path": path, "line": number + 1, "text": line[begin:begin + 400],
                    "truncated": begin > 0 or len(line) > begin + 400}
            if len(output["matches"]) >= 50:
                output["next_cursor"] = {"file_offset": index, "line_offset": number}
                return output
            output["matches"].append(item)
            if len(_json(output)) > MAX_REPLY_BYTES - 128:
                output["matches"].pop()
                output["next_cursor"] = {"file_offset": index, "line_offset": number}
                return output
        line_start = 0
    return output
