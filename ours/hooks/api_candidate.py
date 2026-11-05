"""Capture sealed API files as a private Git candidate, never as a passed check.

The existing board's path/test rules apply before any candidate is created.
No project code, Git filters, hooks, setup or test scripts execute here. Full
checks and publication remain separate: this module cannot write a source ref,
an old check.log pass, a delivery record or a human acceptance.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess

import api_project as project
import api_session as session
import api_workspace as workspace
import check


class CandidateError(Exception):
    """Fixed public errors, without command output or file contents."""


KEY_LIKE = re.compile(rb"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}")
MAX_RULE_BYTES = 1024 * 1024


def rules_snapshot(board: Path) -> dict:
    """Bind trusted board rules and their implementation to the approval."""
    result = {}
    for name in ("project.toml", "setup.sh", "test.sh"):
        path = board / name
        if not path.exists() and not path.is_symlink():
            result[name] = None
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RULE_BYTES:
            raise CandidateError("api_check_rules_invalid")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("check.py", "project_config.py"):
        result[name] = hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
    return result


def _git(path: Path, *args: str, data: bytes | None = None) -> bytes:
    # A new private bare repository has no source checkout configuration. No
    # inherited keys, external diff/filter, shell helper, templates or network.
    env = {"PATH": os.defpath, "HOME": "/nonexistent", "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "Symphony API", "GIT_COMMITTER_NAME": "Symphony API",
           "GIT_AUTHOR_EMAIL": "symphony@localhost", "GIT_COMMITTER_EMAIL": "symphony@localhost"}
    try:
        done = subprocess.run([project.GIT, "--no-replace-objects", "--literal-pathspecs",
            "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "commit.gpgsign=false", "-C", str(path), *args], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise CandidateError("api_candidate_git_failed") from None
    if done.returncode:
        raise CandidateError("api_candidate_git_failed")
    return done.stdout


def _oid(raw: bytes) -> str:
    value = raw.decode("ascii").strip()
    if not project.OID.fullmatch(value):
        raise CandidateError("api_candidate_git_failed")
    return value


def _capture(state: dict, path: Path, cfg: check.Config) -> tuple[dict, dict, list[dict]]:
    spec, source, initial = state["spec"], state["execution_state"], state["execution_state"]["initial"]
    workspace.validate_state(source, spec, state["project_snapshot"], path)
    if not source["sealed"]:
        raise CandidateError("workspace_result_not_sealed")
    current = workspace.manifest(spec, source)
    names = sorted(p for p in set(current) | set(initial) if current.get(p) != initial.get(p))
    raw = project._git(spec["project"]["root"], ["ls-tree", "-r", "-z", "--full-tree",
                       spec["project"]["commit"]], project.MAX_TREE_BYTES)
    base_paths = {row.split(b"\t", 1)[1].decode("utf-8") for row in raw.split(b"\0") if row}
    problems, contents = [], {}
    is_test, allowed_test = check._matcher(cfg.test_globs), check._matcher(cfg.allow_modified_tests)
    for name in names:
        if check.forbidden_violations({name}, cfg):
            problems.append({"code": "protected_path", "path": name})
        if name in base_paths and name not in initial:
            # The read grant omits symlinks, submodules and oversized files.
            # Such a path must not become an apparent "new" file replacement.
            problems.append({"code": "unavailable_base_file", "path": name})
        if name in base_paths and is_test(name) and not allowed_test(name):
            problems.append({"code": "existing_test_changed", "path": name})
        if name not in current:
            continue
        data, mode = workspace.read(spec, source, name)
        if {"sha256": workspace._sha(data), "bytes": len(data), "mode": mode} != current[name]:
            raise CandidateError("workspace_review_changed")
        # Deliberately scan complete changed blobs, including binary data. A
        # key-looking value is a reason for review, not proof of a valid key.
        if KEY_LIKE.search(data) or check.has_api_key(data.decode("utf-8", errors="replace")):
            problems.append({"code": "possible_api_key", "path": name})
        contents[name] = data
    if workspace.manifest(spec, source) != current:
        raise CandidateError("workspace_review_changed")
    changes = [{"path": p, "before": initial.get(p), "after": current.get(p)} for p in names]
    return {"revision": workspace._revision(current), "files": changes}, contents, problems


def _tree(path: Path, oid: str) -> dict:
    raw = _git(path, "ls-tree", "-r", "-z", "--full-tree", oid)
    if len(raw) > project.MAX_TREE_BYTES:
        raise CandidateError("api_candidate_tree_too_large")
    return {name: meta for row in raw.split(b"\0") if row for meta, name in [row.split(b"\t", 1)]}


def _build(root: Path, state: dict, delta: dict, contents: dict, identifier: str, cancelled) -> dict:
    spec = state["spec"]["project"]
    path = root / "candidate.git"
    path.mkdir(mode=0o700)  # Never reuse a partial or unrelated candidate.
    algorithm = "sha1" if len(spec["commit"]) == 40 else "sha256"
    _git(path, "init", "--bare", "-q", "--template=", "--object-format=" + algorithm)
    common = project._git(spec["root"], ["rev-parse", "--path-format=absolute", "--git-common-dir"], 4096)
    objects = str((Path(common.decode().removesuffix("\n")) / "objects").resolve(strict=True))
    if any(c in objects for c in ("\n", "\r", "\x00")):
        raise CandidateError("api_candidate_source_invalid")
    # Existing immutable base objects are read through an alternate. New blobs,
    # index, tree, commit and the candidate ref exist only in this private repo.
    (path / "objects/info/alternates").write_text(objects + "\n")
    _git(path, "read-tree", spec["commit"])
    expected = _tree(path, spec["commit"])
    remove, add = [], []
    for entry in delta["files"]:
        if cancelled and cancelled():
            raise CandidateError("operator_stopped")
        name, after = entry["path"], entry["after"]
        expected.pop(name.encode(), None)
        remove.append(f"0 {'0' * len(spec['commit'])}\t{name}\0".encode())
        if after is not None:
            oid = _oid(_git(path, "hash-object", "-w", "--no-filters", "--stdin", data=contents[name]))
            mode = "100755" if after["mode"] == 0o755 else "100644"
            add.append(f"{mode} {oid}\t{name}\0".encode())
            expected[name.encode()] = f"{mode} blob {oid}".encode()
    _git(path, "update-index", "-z", "--index-info", data=b"".join(remove + add))
    tree = _oid(_git(path, "write-tree"))
    if _tree(path, tree) != expected:
        # A file/directory collision must not remove any omitted base paths.
        raise CandidateError("api_candidate_tree_changed")
    commit = _oid(_git(path, "commit-tree", tree, "-p", spec["commit"],
                       data=f"API candidate {identifier}\n".encode()))
    _git(path, "update-ref", "refs/heads/candidate", commit, "0" * len(commit))
    return {"repository": str(path), "commit": commit, "tree": tree}


def prepare(path: Path, board: Path, identifier: str, expected_rules: dict, *, cancelled=None) -> dict:
    """Free host-side capture after a completed dialogue; never claims tests passed."""
    report = {"version": 1, "status": "blocked", "identifier": identifier,
              "checks": {"static": "not_run", "full": "not_run"}, "issues": [],
              "accepted": False, "delivered": False}
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identifier):
            raise CandidateError("invalid_board_identifier")
        with session._lock(path):
            if cancelled and cancelled():
                raise CandidateError("operator_stopped")
            if rules_snapshot(board) != expected_rules:
                raise CandidateError("api_check_rules_changed")
            state = session._load(path)
            if state["status"] != "dialogue_complete" or "execution" not in state["spec"]:
                raise CandidateError("api_result_not_complete")
            cfg = check.load_config(board)
            configured = check.pc.load_project(board)["project"]["repo"]
            if Path(configured).resolve() != Path(state["spec"]["project"]["root"]).resolve():
                raise CandidateError("api_check_project_changed")
            delta, contents, problems = _capture(state, path, cfg)
            report.update(base_commit=state["spec"]["project"]["commit"], **delta)
            report["checks"]["static"] = "failed" if problems else "passed"
            report["issues"] = problems
            if not problems:
                if cancelled and cancelled():
                    raise CandidateError("operator_stopped")
                if rules_snapshot(board) != expected_rules:
                    raise CandidateError("api_check_rules_changed")
                if delta["files"]:
                    report.update(_build(path.parent, state, delta, contents, identifier, cancelled))
                if rules_snapshot(board) != expected_rules:
                    raise CandidateError("api_check_rules_changed")
                if cancelled and cancelled():
                    raise CandidateError("operator_stopped")
                report["status"] = "awaiting_full_checks" if delta["files"] else "no_changes"
    except CandidateError as exc:
        report["status"] = "blocked"
        report["issues"].append({"code": str(exc)})
    except (check.CheckError, check.pc.ProjectError, session.SessionError, project.ProjectError,
            workspace.WorkspaceError, OSError, ValueError):
        # Detailed config/Git exceptions may contain secret values. Public
        # diagnostics name the stage only; there is no fallback to host tests.
        report["status"] = "blocked"
        report["issues"].append({"code": "candidate_preparation_failed"})
    report["rules"] = expected_rules
    session._save(path.parent / "candidate.json", report)
    return report


def public_report(value: dict) -> dict:
    """Polling is bounded; the complete file manifest stays in candidate.json."""
    keys = ("version", "status", "identifier", "base_commit", "revision", "commit", "tree",
            "checks", "accepted", "delivered")
    return {**{key: value[key] for key in keys if key in value},
            "file_count": value.get("file_count", len(value.get("files", []))),
            "issue_count": value.get("issue_count", len(value.get("issues", []))),
            "issues": value.get("issues", [])[:20]}


def check_identity(path: Path, value: dict, state: dict) -> None:
    """Match the reported result to the sealed workspace and the actual private ref."""
    report = value["candidate"]
    repo = path.parent / "candidate.git"
    saved = session._decode(session._read_state(path.parent / "candidate.json"))
    if (repo.resolve(strict=True) != repo or saved.get("repository") != str(repo)
            or public_report(saved) != report or saved["rules"] != value["rules"]
            or report["identifier"] != value["identifier"] or report["checks"]["static"] != "passed"
            or state["status"] != "dialogue_complete" or state["spec"] != value["spec"]
            or not state["execution_state"]["sealed"]):
        raise CandidateError("api_candidate_result_changed")
    workspace.validate_state(state["execution_state"], state["spec"], state["project_snapshot"], path)
    commit = _oid(project._git(str(repo), ["rev-parse", "--verify", "refs/heads/candidate^{commit}"], 128))
    header = project._git(str(repo), ["cat-file", "commit", commit], 65536).split(b"\n\n", 1)[0].splitlines()
    parents = [line[7:].decode("ascii") for line in header if line.startswith(b"parent ")]
    trees = [line[5:].decode("ascii") for line in header if line.startswith(b"tree ")]
    base = value["spec"]["project"]["commit"]
    if (commit != report["commit"] or trees != [report["tree"]] or parents != [base]
            or report["base_commit"] != base or workspace._revision(workspace.manifest(
                state["spec"], state["execution_state"])) != report["revision"]):
        raise CandidateError("api_candidate_result_changed")


def render_report(value: dict, *, cloud_checked=False) -> str:
    value = public_report(value)
    labels = {"awaiting_full_checks": "Кандидат собран; полный прогон ещё не выполнен.",
              "no_changes": "Изменений файлов нет; задача автоматически не принята.",
              "blocked": "Подготовка кандидата остановлена."}
    issues = {"protected_path": "Изменён защищённый файл", "existing_test_changed": "Изменён существующий тест",
              "unavailable_base_file": "Замена файла, который не был выдан команде",
              "possible_api_key": "Найдена строка, похожая на API-ключ",
              "operator_stopped": "Остановлено оператором",
              "api_check_rules_changed": "Настройки проверок изменились после подтверждения",
              "api_check_project_changed": "Проект в настройках проверок не совпадает с назначением",
              "api_candidate_git_failed": "Не удалось собрать отдельный Git-кандидат",
              "api_candidate_tree_changed": "Собранный кандидат не совпадает с проверенными файлами",
              "api_candidate_tree_too_large": "Кандидат превышает допустимый размер списка файлов",
              "api_result_not_complete": "Нет завершённого результата команды",
              "workspace_result_not_sealed": "Рабочие файлы ещё не закреплены после выполнения команды",
              "candidate_preparation_failed": "Не удалось подтвердить настройки, целостность или готовность результата"}
    rows = ["Проверка и передача файлов", "Кандидат собран. Итог Linux-проверки показан отдельно."
            if cloud_checked else labels[value["status"]]]
    rows.append(f"Изменённых файлов: {value['file_count']}.")
    for issue in value["issues"]:
        rows.append(issues.get(issue["code"], "Подготовка прервана") + (": " + issue["path"] if "path" in issue else ""))
    if value["issue_count"] > len(value["issues"]):
        rows.append(f"Других замечаний: {value['issue_count'] - len(value['issues'])}.")
    rows.append("Доставка ожидает отдельного решения. Приёмка остаётся за человеком." if cloud_checked
                else "Полная проверка и доставка не выполнены. Приёмка остаётся за человеком.")
    return "\n".join(rows)
