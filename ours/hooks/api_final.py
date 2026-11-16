"""Finalize the exact result of an approved API subtask chain."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import shlex
import tempfile

import api_candidate as candidate
import api_cloud_check as cloud
import api_delivery as delivery
import api_project as project
import api_publication as publication
import api_session as session
import api_subtasks as subtasks
import api_workspace as workspace
import check
import project_config as pc

FinalError = session.SessionError
SUBJECT_KEYS = ("commit", "tree", "base_commit")


def _read(path):
    return session._decode(session._read_state(path))


def _ref(repo, commit, tree, base):
    actual = candidate._git(repo, "rev-list", "--parents", "-n", "1", "refs/heads/candidate").decode().split()
    if actual != [commit, base] or publication.oid(repo, "rev-parse", commit + "^{tree}") != tree:
        raise FinalError("api_final_result_changed")


def _chain(board, value, run):
    terms, rows = run["terms"], run["checked"]
    original = value["spec"]["project"]
    if (run["status"] != "complete" or not rows or len(rows) != len(terms["parts"])
            or terms["spec"]["project"] != original or original["root"] != str(board.repo)
            or terms["plan_binding"] != value["binding"] or terms["rules"] != value["rules"]):
        raise FinalError("api_final_result_changed")
    return rows, original["commit"]


def _checked_parts(board, value, run):
    rows, base = _chain(board, value, run)
    repo = board.root / "subtasks.git"
    if repo.resolve(strict=True) != repo:
        raise FinalError("api_final_result_changed")
    allowed = set()
    previous = base
    for part, row in zip(run["terms"]["parts"], rows, strict=True):
        child = subtasks._board(board, part["identifier"])
        assignment = child._load()
        front, body = child.card()
        if (not assignment or front["state"] != "Done" or part["card_digest"] != child.card_digest(front, body)
                or part["card_digest"] != assignment["card_digest"] or assignment.get("subtask") != {
                    "parent": board.identifier, "fingerprint": run["fingerprint"], "check": part["check"],
                    "repository": str(repo), "base_commit": previous, "timeout": run["terms"]["timeout"]}
                or subtasks._verified(child, assignment) != row):
            raise FinalError("api_final_result_changed")
        delta = _read(child.root / "candidate.json")["files"]
        if any(not project._in_scope(file["path"], part["paths"]) for file in delta):
            raise FinalError("api_final_result_changed")
        allowed.update(file["path"] for file in delta)
        previous = row["commit"]
    return repo, base, previous, allowed


def assemble(board, value, run):
    """Publish the local result record before projecting it into Verify."""
    subtasks._parent(board, value, recheck=False)
    if not board.review_lane or "Verify" not in board.context["active_states"]:
        raise FinalError("api_final_review_lane_required")
    settings = board.check_config()
    frozen = value.get("plan_check_config", settings)
    if frozen != settings or candidate.rules_snapshot(board.workflow.parent) != value["rules"]:
        raise FinalError("api_final_settings_changed")
    repo, base, tip, allowed = _checked_parts(board, value, run)
    cfg = check.load_config(board.workflow.parent)
    files = delivery.compare_files(repo, base, tip, allowed, cfg)
    tree = publication.oid(repo, "rev-parse", tip + "^{tree}")
    bare = board.root / "candidate.git"
    title = f"API composed candidate {board.identifier}"
    if not bare.exists() and not bare.is_symlink():
        with tempfile.TemporaryDirectory(prefix=".final-", dir=board.root) as directory:
            staging = Path(directory) / "candidate"
            staging.mkdir(mode=0o700)
            candidate.init_repository(staging, {"root": str(repo), "commit": base})
            commit = publication.commit(staging, tree, [base], title)
            candidate._git(staging, "update-ref", "refs/heads/candidate", commit, "0" * len(commit))
            staging.rename(bare)
    if bare.resolve(strict=True) != bare or not bare.is_dir():
        raise FinalError("api_final_result_changed")
    commit = publication.commit(bare, tree, [base], title)
    _ref(bare, commit, tree, base)
    aggregate = {"chain": run["fingerprint"], "tip": tip,
                 "method": "cloud" if board.cloud_profile() == cloud.PROFILE else "local"}
    report = {"version": 1, "status": "awaiting_full_checks", "identifier": board.identifier,
              "checks": {"static": "passed", "full": "not_run"}, "issues": [], "accepted": False,
              "delivered": False, "repository": str(bare), "base_commit": base, "tree": tree, "commit": commit,
              "revision": delivery.fingerprint([aggregate, files, tree]), "rules": value["rules"],
              "aggregate": aggregate, "files": [{"path": name, "change": change} for name, change in files]}
    path = board.root / "candidate.json"
    if path.exists() or path.is_symlink():
        if _read(path) != report:
            raise FinalError("api_final_result_changed")
    else:
        session._save(path, report)
    value.update(aggregate=aggregate, candidate=candidate.public_report(report), check_config=settings,
                 result_lane="Verify", check_lane="Verify", result_card_digest=value["card_digest"])
    if aggregate["method"] == "cloud":
        value["cloud_profile"] = cloud.PROFILE
    board._save(value)


def check_identity(board, value):
    run = subtasks._load(board)
    if not run:
        raise FinalError("api_final_result_changed")
    rows, base = _chain(board, value, run)
    aggregate, report = value["aggregate"], value["candidate"]
    bare = board.root / "candidate.git"
    saved = _read(board.root / "candidate.json")
    if (bare.resolve(strict=True) != bare or saved["repository"] != str(bare)
            or candidate.public_report(saved) != report or saved["aggregate"] != aggregate
            or saved["rules"] != value["rules"] or report["base_commit"] != base
            or aggregate["chain"] != run["fingerprint"] or aggregate["tip"] != rows[-1]["commit"]
            or publication.oid(bare, "rev-parse", aggregate["tip"] + "^{tree}") != report["tree"]
            or candidate.rules_snapshot(board.workflow.parent) != value["rules"]
            or board.check_config() != value["check_config"]):
        raise FinalError("api_final_result_changed")
    _ref(bare, report["commit"], report["tree"], base)


def can_check_local(board, value):
    return (value.get("aggregate", {}).get("method") == "local" and not value.get("final_error")
            and board.card()[0]["state"] in {"Verify", "Document"})


def local_subject(board, value):
    if not value or not board.tracker.find_path(board.identifier):
        raise FinalError("api_final_result_changed")
    check_identity(board, value)
    front, body = board.card()
    if (board.card_digest(front, body) != value["result_card_digest"] or board.stop_path.exists()
            or board._base_commit() != value["candidate"]["base_commit"]):
        raise FinalError("api_final_result_changed")
    return {key: value["candidate"][key] for key in SUBJECT_KEYS}


def _local_tests(board, value, subject, cancelled):
    cfg = check.load_config(board.workflow.parent)
    run = subtasks._load(board)
    docs = check._docs_only({name for name, _ in delivery.file_delta(board, subject["base_commit"], subject["commit"])}, cfg)
    recipe = [*dict.fromkeys(part["check"] for part in run["terms"]["parts"])] if docs else []
    spec = run["terms"]["spec"]
    with tempfile.TemporaryDirectory(prefix=".final-test-", dir=board.root) as directory:
        root = Path(directory)
        git, source, control, scripts = (root / name for name in ("check.git", "source", "control", "scripts"))
        for path in (git, source, control, scripts):
            path.mkdir(mode=0o700)
        candidate.init_repository(git, {"root": str(board.root / "candidate.git"), "commit": subject["commit"]})
        candidate._git(git, "update-ref", "--no-deref", "HEAD", subject["commit"])
        candidate._git(git, "update-ref", "refs/heads/" + cfg.base_branch, subject["base_commit"])
        candidate._git(git, "read-tree", subject["commit"])
        candidate._git(git, "--work-tree=" + str(source), "checkout-index", "-a", "-u")
        if (source / ".tmp").exists() or (source / ".tmp").is_symlink():
            raise FinalError("api_final_test_copy_changed")
        counts = baseline = None
        policy = workspace.policy(spec, {"root": str(source), "control": str(control)})
        if not docs:
            script = board.workflow.parent / "test.sh"
            if not value["rules"].get("test.sh"):
                raise FinalError("api_final_tests_unavailable")
            script_data = script.read_bytes()
            if hashlib.sha256(script_data).hexdigest() != value["rules"]["test.sh"]:
                raise FinalError("api_final_settings_changed")
            (scripts / "test.sh").write_bytes(script_data)
            policy = replace(policy, read_roots=(*policy.read_roots, scripts))
            recipe = [f"SYMPHONY_WORKFLOW_DIR={shlex.quote(str(scripts))} "
                      f"SYMPHONY_PYTHON={shlex.quote(spec['execution']['python'])} "
                      f"/bin/bash {shlex.quote(str(scripts / 'test.sh'))}"]
            if any((board.workflow.parent / name).exists() for name in ("baseline.json", "baselines.json")):
                measures = check.load_baselines(board.workflow.parent, cfg)
                baseline = check.effective_baseline(git, measures, cfg.base_branch, pc.paths_rule(cfg.baseline_paths))
        output = []
        passed = True
        for command in recipe:
            result = session.sandbox.run(policy, command, timeout=run["terms"]["timeout"], cancelled=cancelled)
            output.append(result["output"][-4000:])
            if result["exit_code"] != 0 or result["stop_reason"] or cancelled():
                passed = False
                break
        if not docs:
            counts = pc.find_summary(result["output"].splitlines())
            problems = check.summary_violations(counts, baseline.record if baseline else None, baseline)
            passed = passed and not problems
            output.extend(problems)
        try:
            candidate._git(git, "--work-tree=" + str(source), "diff", "--quiet", "--no-ext-diff", "--no-textconv", "--")
        except candidate.CandidateError:
            raise FinalError("api_final_test_copy_changed") from None
        return {**subject, "status": "passed" if passed else "failed", "kind": "local",
                "tests": "targeted" if docs else "full", "counts": counts, "output": "\n".join(output)[-8000:],
                "reason": None if passed else "api_final_tests_failed", "checked_at": session._now()}


def local_check(board, cancelled):
    """Run once per precise result. A saved receipt replays only lane changes."""
    path = board.root / "final-local.json"
    with session._lock(board.root / "final-owner"):
        with session._lock(board.path):
            value = board._load()
            if not value or not can_check_local(board, value):
                return
            subject = _local_subject_or_fail(board, value)
            if subject is None:
                return
            stored = _read(path) if path.exists() or path.is_symlink() else None
            if stored:
                if any(stored[key] != subject[key] for key in SUBJECT_KEYS):
                    fail(board, value, "api_final_result_changed")
                    return
                if stored["status"] == "running":
                    stored = {**stored, "status": "failed", "reason": "api_final_test_interrupted"}
                    session._save(path, stored)
                _apply_local(board, value, subject, stored)
                return
            session._save(path, {**subject, "status": "running", "kind": "local"})
        try:
            proof = _local_tests(board, value, subject, cancelled)
        except (FinalError, candidate.CandidateError, check.CheckError, session.sandbox.SandboxError, OSError) as exc:
            reason = str(exc) if isinstance(exc, FinalError) else "api_final_tests_unavailable"
            proof = {**subject, "status": "failed", "kind": "local", "reason": reason, "checked_at": session._now()}
        session._save(path, proof)
        with session._lock(board.path):
            value = board._load()
            if not value or _local_subject_or_fail(board, value) != subject:
                return
            _apply_local(board, value, subject, proof)


def _apply_local(board, value, subject, proof):
    if proof["status"] == "passed":
        document(board, value, subject, proof)
    else:
        fail(board, value, proof["reason"])


def _local_subject_or_fail(board, value):
    try:
        return local_subject(board, value)
    except (FinalError, candidate.CandidateError, project.ProjectError, OSError, ValueError):
        fail(board, value, "api_final_result_changed")
        return None


def reconcile_local_review(board, value):
    if (value.get("aggregate", {}).get("method") == "local" and value.get("final_document")
            and board.card()[0]["state"] == board.review_lane):
        _local_subject_or_fail(board, value)


def fail(board, value, reason):
    value["final_error"] = reason
    value["result_card_digest"] = value["card_digest"]
    board._save(value)
    if board.tracker.find_path(board.identifier) and board.card_digest(*board.card()) == value["result_card_digest"]:
        board._release_lane(value, "needs_human", allowed_states={
            "Subtasks", "Verify", "Document", "needs_human", board.review_lane})


def document(board, value, subject, proof):
    """Keep a check-bound handoff and idempotently show it in Human Review."""
    if proof["status"] != "passed" or any(proof[key] != subject[key] for key in SUBJECT_KEYS):
        raise FinalError("api_final_result_changed")
    evidence = {**{key: subject[key] for key in SUBJECT_KEYS}, "proof": proof}
    token = delivery.fingerprint(evidence)
    path = board.root / f"final-document-{token}.json"
    if not path.exists() and not path.is_symlink():
        run = subtasks._load(board)
        phases = [("Plan", board), *((row["identifier"], subtasks._board(board, row["identifier"])) for row in run["checked"])]
        spend = {label: session.spend_report(session._load(task.session_path))["by_model"] for label, task in phases}
        changes = delivery.file_delta(board, subject["base_commit"], subject["commit"])
        session._save(path, {"version": 1, **evidence, "parts": run["checked"], "files": changes, "spend": spend})
    saved = _read(path)
    if any(saved[key] != evidence[key] for key in evidence):
        raise FinalError("api_final_result_changed")
    value.pop("final_error", None)
    value["final_document"] = {"id": token, **{key: subject[key] for key in SUBJECT_KEYS}}
    board._save(value)
    allowed = {"Verify", "Document", "needs_human", board.review_lane}
    if "Document" in board.context["active_states"] and board.card()[0]["state"] != board.review_lane:
        board._release_lane(value, "Document", allowed_states=allowed)
    board._release_lane(value, board.review_lane, allowed_states=allowed)


def view(board, value):
    current = board.card()[0]["state"]
    record = {key: (value.get("check_target") or value["candidate"])[key] for key in SUBJECT_KEYS}
    record.update(status="checking", reason=value.get("final_error"), method=value["aggregate"]["method"])
    if value.get("final_error") or current == "needs_human":
        record["status"] = "failed"
    elif current == board.review_lane and all(record[k] == value.get("final_document", {}).get(k) for k in SUBJECT_KEYS):
        record["status"] = "ready"
        if record["method"] == "local":
            try:
                local_subject(board, value)
            except (FinalError, candidate.CandidateError, project.ProjectError, OSError, ValueError):
                record.update(status="failed", reason="api_final_result_changed")
    return record


def render_document(board, value, record):
    if record["status"] == "failed" and record["method"] == "local":
        path = board.root / "final-local.json"
        if path.is_file():
            output = _read(path).get("output", "")
            return "Итоговая проверка общей версии\n" + output if output else ""
    if record["status"] != "ready":
        return ""
    token = value["final_document"]["id"]
    if not isinstance(token, str) or not workspace.SHA.fullmatch(token):
        raise FinalError("api_final_result_changed")
    saved = _read(board.root / f"final-document-{token}.json")
    if any(saved[key] != record[key] for key in SUBJECT_KEYS) or saved["proof"]["status"] != "passed":
        raise FinalError("api_final_result_changed")
    rows = ["Сводка составной задачи", f"Общая версия {saved['commit']}",
            f"Проверено частей — {len(saved['parts'])}. Изменено файлов — {len(saved['files'])}."]
    if counts := saved["proof"].get("counts"):
        rows.append(f"Тесты — {counts['passed']} прошли, {counts['skipped']} пропущены, {counts['failed']} ошибок.")
    rows.extend(f"{label} · {model} · запросов {row['calls']} · {session._model_cost_line(row)}"
                for label, models in saved["spend"].items() for model, row in models.items())
    return "\n".join(rows)
