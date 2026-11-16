"""Run an approved plan in order and retain each locally verified version."""

from __future__ import annotations

from contextlib import nullcontext
import hashlib
import hmac
from pathlib import Path
import secrets
import shutil
import tempfile
import time

import api_budget as budget
import api_candidate as candidate
import api_plan
import api_project as project
import api_project_context as project_context
import api_session as session
import api_workspace as workspace
import check
from symphony.errors import SymphonyError

PlanError = api_plan.PlanError
RETRYABLE_FLASH_STOPS = frozenset({
    "flash_region_unsupported", "flash_capacity_unavailable", "flash_quota_exhausted",
    "flash_connection_unavailable", "flash_worker_failed",
})


def _digest(value):
    return hashlib.sha256(session._encode(value)).hexdigest()


def _path(board):
    return board.root / "subtasks.json"


def _load(board):
    path = _path(board)
    if not path.exists() and not path.is_symlink():
        return None
    run = session._decode(session._read_state(path))
    if (not isinstance(run, dict) or run.get("version") != 1 or run.get("parent") != board.identifier
            or run.get("status") not in {"prepared", "active", "complete", "stopped"}
            or not isinstance(run.get("terms"), dict) or not isinstance(run.get("checked"), list)
            or run.get("fingerprint") != _digest(run.get("terms"))):
        raise PlanError("api_subtasks_state_invalid")
    return run


def _save(board, run):
    session._save(_path(board), run)


def _board(board, identifier):
    return type(board)(board.settings, {**board.context, "identifier": identifier})


def _remaining(policy, uses):
    if policy is None:
        return None
    money = budget.money(policy["max_usd"]) - sum(budget.money(use["generation_cost_upper_usd"]) for use in uses)
    tokens = policy["input_tokens_total"] - sum(use["input_tokens_committed"] for use in uses)
    if money <= 0 or tokens <= 0 or any(use["contract_breached"] for use in uses):
        raise PlanError("api_subtasks_budget_exhausted")
    return {"max_usd": budget.display(money), "input_tokens_total": tokens}


def _parent(board, value, *, recheck, waiting=("Subtasks",)):
    if (not value or not board.tracker.find_path(board.identifier) or not value.get("planning_team")
            or value.get("status") != "result" or value.get("plan_batch", {}).get("status") != "ready"):
        raise PlanError("api_plan_not_ready")
    front, body = board.card()
    api_plan._parent(board, value, front, body)
    if front["state"] not in waiting:
        raise PlanError("api_plan_parent_changed")
    if recheck:
        if not board.permission.allows(value):
            raise PlanError("api_permission_revoked")
        current = board.proposal(value["planning_team"])
        if any(current[key] != value.get(key) for key in ("binding", "subtask_spec", "subtask_context_paths")):
            raise PlanError("api_plan_parent_changed")


def _terms(board, value):
    _parent(board, value, recheck=True)
    if "Verify" not in board.context["active_states"] or "Done" not in board.context["terminal_states"]:
        raise PlanError("api_plan_not_configured")
    spec = session.validate_spec({**value["subtask_spec"], "task": "Validate subtask assignment"})
    spec.pop("task")
    parts = api_plan.frozen_parts(board, value)
    if any(not project._in_scope(path, workspace.writable_paths(spec)) for part in parts for path in part["paths"]):
        raise PlanError("api_plan_subtask_changed")
    if "budget" in spec:
        spent = budget.totals(session._load(board.session_path))
        spec["budget"] = _remaining(spec["budget"], [spent])
    return {"plan_binding": value["binding"], "spec": spec, "parts": parts,
            "context_paths": value["subtask_context_paths"], "rules": value["rules"],
            "permission_revision": value.get("permission_revision"), "authorization": value.get("authorization"),
            "timeout": min(check.load_config(board.workflow.parent).test_timeout_s, session.sandbox.MAX_TIMEOUT_SECONDS)}


def _retry_part(run):
    parts, index = run["terms"]["parts"], len(run["checked"])
    if run["status"] == "stopped" and run.get("reason") in RETRYABLE_FLASH_STOPS and index < len(parts):
        return parts[index]
    return None


def _retry_binding(board, value, run, *, waiting=("Subtasks",)):
    """A fresh approval can reuse a sealed plan from an older installed release."""
    _parent(board, value, recheck=False, waiting=waiting)
    if not board.permission.allows(value):
        raise PlanError("api_permission_revoked")
    base = run["terms"]["spec"]["project"]["commit"]
    current_base = board._base_commit()
    if base != current_base:
        try:
            candidate._git(board.repo, "merge-base", "--is-ancestor", base, current_base)
        except candidate.CandidateError:
            raise PlanError("api_plan_parent_changed") from None
    current = board.proposal(value["planning_team"], base_commit=base)
    if (run["terms"]["plan_binding"] != value["binding"] or
            any(current.get(key) != value.get(key) for key in (
                "card_digest", "spec", "project_digest", "subtask_spec", "subtask_context_paths", "rules", "plan_check_config"))):
        raise PlanError("api_plan_parent_changed")
    return _digest([current["binding"], current_base])


def _spent(run):
    return [row["usage"] for row in [*run["checked"], *run.get("previous_attempts", [])] if row["usage"] is not None]


def _retry_details(board, value, run):
    part = _retry_part(run)
    if part is None:
        raise PlanError("api_subtasks_already_used")
    binding = _retry_binding(board, value, run, waiting=("needs_human",))
    parts = api_plan.frozen_parts(board, value, previous_parts=run["terms"]["parts"])
    index = len(run["checked"])
    for count in range(1, index + 1):
        _verify_predecessor(board, {**run, "checked": run["checked"][:count]})
    for later in parts[index + 1:]:
        identifier = later["identifier"]
        issue = board.tracker.fetch_issue_full_by_id(identifier)
        folder = board.root.parent / identifier
        markers = [folder / name for name in ("assignment.json", "stop-requested")]
        if (issue is None or issue.state != "Subtasks" or folder.is_symlink()
                or any(path.exists() or path.is_symlink() for path in markers)):
            raise PlanError("api_plan_subtask_changed")
    child = _board(board, part["identifier"])
    old = child._load()
    candidate_path = child.root / "candidate.json"
    if (not old or old["status"] != "stopped" or old.get("reason") != run["reason"]
            or old.get("subtask") != _child_binding(board, run, part)
            or old.get("card_digest") != part["card_digest"] or old["spec"]["worker_profile"] != session.api.FLASH_WORKER
            or child.card()[0]["state"] != "needs_human" or child.stop_path.exists()
            or old.get("candidate") or candidate_path.exists() or candidate_path.is_symlink()):
        raise PlanError("api_subtask_retry_unavailable")
    path = child.session_file(old)
    if path.is_symlink() or (not path.is_file() and (old.get("run_id") or run["reason"] != "flash_quota_exhausted")):
        raise PlanError("api_subtask_retry_unavailable")
    saved = session._load(path) if path.exists() else None
    if saved and (saved["status"] != "stopped" or saved["spec"] != old["spec"]
                  or any(call["status"] == "in_flight" for call in saved["calls"])):
        raise PlanError("api_subtask_retry_unavailable")
    if run["reason"] == "flash_worker_failed":
        last = (saved or {}).get("calls", [])[-1:]
        if (not last or last[0]["profile"] != session.api.FLASH_WORKER
                or last[0].get("error") != {"error": "flash_worker_stopped", "quota_usage_possible": False}):
            raise PlanError("api_subtask_retry_unavailable")
    usage = budget.totals(saved) if saved and "budget" in saved["spec"] else None
    history = {"identifier": child.identifier, "reason": run["reason"], "run_id": old.get("run_id"),
               "attempt_id": old.get("attempt_id"),
               "confirmed_at": old.get("confirmed_at"), "finished_at": old.get("finished_at"),
               "board_stages": old.get("board_stages", []),
               "session_file": path.name if saved else None, "session_digest": _digest(saved) if saved else None,
               "usage": usage, "spend": session.spend_report(saved)["by_model"] if saved else {}}
    extra = [usage] if usage and history not in run.get("previous_attempts", []) else []
    remaining = _remaining(run["terms"]["spec"].get("budget"), _spent(run) + extra)
    return {"binding": binding, "old_assignment": _digest(old), "history": history, "budget": remaining}


def prepare(board, value):
    run = _load(board)
    if run and run["status"] == "stopped":
        details = _retry_details(board, value, run)
        token = secrets.token_urlsafe(32)
        run["retry_pending"] = {"token": _digest(token), "expires_at": time.time() + 600, "details": details}
        _save(board, run)
        return token
    if run and run["status"] != "prepared":
        raise PlanError("api_subtasks_already_used")
    terms = _terms(board, value)
    token = secrets.token_urlsafe(32)
    _save(board, {"version": 1, "parent": board.identifier, "status": "prepared", "terms": terms,
                  "fingerprint": _digest(terms), "token": _digest(token),
                  "expires_at": time.time() + 600, "checked": []})
    return token


def confirm(board, value, token):
    if not isinstance(token, str) or len(token) > 256:
        raise PlanError("invalid_api_confirmation")
    run = _load(board)
    pending = (run or {}).get("retry_pending")
    digest = _digest(token)
    if pending and hmac.compare_digest(pending["token"], digest):
        if time.time() > pending["expires_at"] or _retry_details(board, value, run) != pending["details"]:
            raise PlanError("api_assignment_changed")
        details = pending["details"]
        history = run.setdefault("previous_attempts", [])
        if details["history"] not in history:
            history.append(details["history"])
        run["retry"] = {"identifier": details["history"]["identifier"], "attempt_id": digest[:32],
                        "binding": details["binding"], "old_assignment": details["old_assignment"],
                        "session_digest": details["history"]["session_digest"],
                        "restore_pending": True}
        for field in ("reason", "finished_at", "retry_pending"):
            run.pop(field, None)
        run.update(status="active", token=digest, confirmed_at=session._now())
        _save(board, run)
        return
    if not run or not hmac.compare_digest(run["token"], digest):
        raise PlanError("invalid_api_confirmation")
    if run["status"] != "prepared":
        return
    if time.time() > run["expires_at"] or _terms(board, value) != run["terms"]:
        raise PlanError("api_assignment_changed")
    run.update(status="active", confirmed_at=session._now())
    _save(board, run)


def view(board):
    run = _load(board)
    if not run:
        return {"status": "unassigned"}
    spec, parts = run["terms"]["spec"], run["terms"]["parts"]
    count = len(run["checked"])
    result = {"status": run["status"], "checked": count, "total": len(parts), "profiles": session._profiles(spec),
              "budget": spec.get("budget"), "limits": spec["limits"], "reason": run.get("reason"),
              "next": parts[count]["identifier"] if count < len(parts) else None}
    if part := _retry_part(run):
        result["retry"] = {"identifier": part["identifier"], "base_commit": spec["project"]["commit"]}
        if (pending := run.get("retry_pending")) and pending["expires_at"] >= time.time():
            result.update(status="prepared", budget=pending["details"]["budget"])
    return result


def previous_spend(run):
    return {f"{row['identifier']}, остановка {index}": row["spend"]
            for index, row in enumerate(run.get("previous_attempts", []), 1)}


def cancel(board):
    run = _load(board)
    if run and run.pop("retry_pending", None) is not None:
        _save(board, run)
    elif run and run["status"] in {"prepared", "active"}:
        run.update(status="stopped", reason="operator_stopped", finished_at=session._now())
        _save(board, run)


def _child_binding(parent, run, part):
    spec = run["terms"]["spec"]["project"]
    previous = run["checked"][-1]["commit"] if run["checked"] else spec["commit"]
    return {"parent": parent.identifier, "fingerprint": run["fingerprint"], "check": part["check"],
            "repository": str(parent.root / "subtasks.git"), "base_commit": previous,
            "timeout": run["terms"]["timeout"]}


def _recheck(parent, value, run, *, waiting=("Subtasks",)):
    retry = run.get("retry")
    if retry:
        if _retry_binding(parent, value, run, waiting=waiting) != retry["binding"]:
            raise PlanError("api_assignment_changed")
    else:
        _parent(parent, value, recheck=True)


def live(board, assignment, *, recheck=False):
    """The parent grant can disappear after a child has entered the queue."""
    link = assignment["subtask"]
    parent = _board(board, link["parent"])
    run = _load(parent)
    if run is None or run["status"] != "active":
        raise PlanError("api_subtasks_not_active")
    value = parent._load()
    if recheck:
        _recheck(parent, value, run)
    else:
        _parent(parent, value, recheck=False)
    parts, count = run["terms"]["parts"], len(run["checked"])
    retry = run.get("retry") or {}
    if (count >= len(parts) or parts[count]["identifier"] != board.identifier
            or parts[count]["card_digest"] != assignment["card_digest"]
            or _child_binding(parent, run, parts[count]) != link
            or (retry.get("identifier") == board.identifier and retry["attempt_id"] != assignment.get("attempt_id"))):
        raise PlanError("api_plan_subtask_changed")
    if recheck:
        _verify_predecessor(parent, run)


def _repository(parent, source):
    repo = parent.root / "subtasks.git"
    if not repo.exists() and not repo.is_symlink():
        with tempfile.TemporaryDirectory(prefix=".subtasks-", dir=parent.root) as directory:
            staging = Path(directory) / "repo"
            staging.mkdir(mode=0o700)
            candidate.init_repository(staging, source)
            staging.rename(repo)
    if repo.resolve(strict=True) != repo or not repo.is_dir():
        raise PlanError("api_subtasks_state_invalid")
    return repo


def _queue(parent, child, run, part, link):
    terms = run["terms"]
    recipe = terms["spec"]
    repo = _repository(parent, recipe["project"])
    spec = session.validate_spec({**recipe, "task": part["task"],
        "project": {**recipe["project"], "root": str(repo), "commit": link["base_commit"]},
        "execution": {**recipe["execution"], "write_paths": part["paths"]}})
    spec["project"], snapshot = project.snapshot(spec["project"])
    if "budget" in spec:
        spec["budget"] = _remaining(spec["budget"], _spent(run))
    if spec["lead_profile"] == "deepseek-lead" and terms["context_paths"]:
        spec["project_context"] = project_context.capture(snapshot, parent.teams.binding, terms["context_paths"])
    value = {"version": 1, "identifier": child.identifier, "status": "queued", "spec": spec,
             "profiles": session._profiles(spec), "rules": terms["rules"], "subtask": link,
             "card_digest": part["card_digest"], "board_stages": [child.context["active_states"][0]],
             "permission_revision": terms["permission_revision"], "authorization": terms["authorization"],
             "created_at": session._now(), "confirmed_at": run["confirmed_at"]}
    retry = run.get("retry") or {}
    if retry.get("identifier") == child.identifier:
        value["attempt_id"] = retry["attempt_id"]
        path = child.session_file(value)
        if path.exists() or path.is_symlink():
            raise PlanError("api_plan_subtask_changed")
    return value


def _move(board, assignment, target, allowed):
    def mutate(front, body):
        if board.card_digest(front, body) != assignment["card_digest"] or front["state"] not in allowed:
            raise PlanError("api_plan_subtask_changed")
        if front["state"] == target:
            return None
        front.update(state=target, updated_at=session._now())
        return front, body
    board.tracker._mutate_ticket(board.identifier, mutate)


def _verified(child, value):
    report = (value or {}).get("subtask_check", {})
    if report.get("status") != "passed" or value["status"] != "result":
        raise PlanError("api_subtask_check_failed")
    state = session._load(child.session_path)
    candidate.check_identity(child.session_path, value, state, shared=Path(value["subtask"]["repository"]))
    if (report["commit"], report["revision"], report["command"]) != (
            value["candidate"]["commit"], value["candidate"]["revision"], value["subtask"]["check"]):
        raise PlanError("api_subtask_result_changed")
    return {"identifier": child.identifier, "commit": report["commit"], "revision": report["revision"],
            "usage": budget.totals(state) if "budget" in state["spec"] else None}


def _verify_predecessor(parent, run):
    if not run["checked"]:
        return
    record = run["checked"][-1]
    part = run["terms"]["parts"][len(run["checked"]) - 1]
    previous = _board(parent, record["identifier"])
    assignment = previous._load()
    prefix = {**run, "checked": run["checked"][:-1]}
    if (previous.identifier != part["identifier"] or not assignment
            or assignment.get("subtask") != _child_binding(parent, prefix, part)
            or not previous.tracker.find_path(previous.identifier) or _verified(previous, assignment) != record):
        raise PlanError("api_subtask_result_changed")
    front, body = previous.card()
    if front["state"] != "Done" or previous.card_digest(front, body) != assignment["card_digest"]:
        raise PlanError("api_subtask_result_changed")


def advance(parent, value):
    """One free poll. The caller has the parent assignment lock."""
    run = _load(parent)
    if not run or run["status"] != "active":
        return
    try:
        retry = run.get("retry") or {}
        if retry.get("restore_pending"):
            _recheck(parent, value, run, waiting=("needs_human", "Subtasks"))
            _move(parent, value, "Subtasks", {"needs_human", "Subtasks"})
            if value["board_stages"][-1] != "Subtasks":
                value["board_stages"].append("Subtasks")
                parent._save(value)
            retry["restore_pending"] = False
            _save(parent, run)
        _parent(parent, value, recheck=False)
        parts, completed = run["terms"]["parts"], run["checked"]
        for part in parts[len(completed):]:
            child = _board(parent, part["identifier"])
            if not child.tracker.find_path(child.identifier):
                raise PlanError("api_plan_subtask_changed")
            link = _child_binding(parent, run, part)
            assignment = child._load()
            if assignment and assignment.get("subtask") != link:
                raise PlanError("api_plan_subtask_changed")
            retry_part = retry.get("identifier") == child.identifier
            replacing = bool(retry_part and assignment and assignment["status"] == "stopped"
                             and _digest(assignment) == retry["old_assignment"])
            if retry_part and not replacing and (not assignment or assignment.get("attempt_id") != retry["attempt_id"]):
                raise PlanError("api_plan_subtask_changed")
            if assignment and assignment["status"] == "running":
                assignment = child._recover_running(assignment)
                if assignment["status"] == "running":
                    return
            if assignment and not replacing and assignment["status"] in {"result", "stopped"}:
                if assignment["status"] == "stopped":
                    raise PlanError(assignment.get("reason") or "api_subtask_check_failed")
                receipt = _verified(child, assignment)
                with session._lock(child.path):
                    _move(child, assignment, "Done", {"Verify", "Done"})
                    if assignment["board_stages"][-1] != "Done":
                        assignment["board_stages"].append("Done")
                        child._save(assignment)
                completed.append(receipt)
                _save(parent, run)
                continue
            if not assignment or replacing:
                _recheck(parent, value, run)
                _verify_predecessor(parent, run)
            owner = session._lock(child.root / "owner") if replacing else nullcontext()
            with owner, session._lock(child.path):
                if assignment is None or replacing:
                    if child._load() != assignment or child.stop_path.exists():
                        raise PlanError("api_plan_subtask_changed")
                    front, body = child.card()
                    waiting = "needs_human" if replacing else "Subtasks"
                    if front["state"] != waiting or child.card_digest(front, body) != part["card_digest"]:
                        raise PlanError("api_plan_subtask_changed")
                    if replacing:
                        old_path = child.session_file(assignment)
                        candidate_path = child.root / "candidate.json"
                        actual = _digest(session._load(old_path)) if old_path.exists() else None
                        if (actual != retry["session_digest"] or old_path.is_symlink()
                                or candidate_path.exists() or candidate_path.is_symlink()):
                            raise PlanError("api_plan_subtask_changed")
                    assignment = _queue(parent, child, run, part, link)
                    child._save(assignment)
                todo = child.context["active_states"][0]
                _move(child, assignment, todo, {"needs_human", todo} if retry_part else {"Subtasks", todo})
            return
        run.update(status="complete", finished_at=session._now())
        _save(parent, run)
    except (PlanError, candidate.CandidateError, session.SessionError, project.ProjectError, SymphonyError, OSError) as exc:
        if str(exc) == "session_busy":
            return
        reason = exc.reason if isinstance(exc, PlanError) else "api_subtask_result_changed"
        run.update(status="stopped", reason=reason, finished_at=session._now())
        _save(parent, run)
        try:
            _move(parent, value, "needs_human", {"Subtasks"})
            if value["board_stages"][-1] != "needs_human":
                value["board_stages"].append("needs_human")
                parent._save(value)
        except (PlanError, SymphonyError):
            pass


def verify(board, value, cancelled):
    """Check the sealed version in a disposable copy under the existing sandbox."""
    report = {"status": "failed", "command": value["subtask"]["check"], "reason": "api_subtask_check_failed"}
    candidate_report = value["candidate"]
    if candidate_report["status"] != "awaiting_full_checks":
        return report
    state = session._load(board.session_path)
    spec, source = state["spec"], state["execution_state"]
    shared = Path(value["subtask"]["repository"])
    candidate.check_identity(board.session_path, value, state, shared=shared)
    files = workspace.manifest(spec, source)
    if workspace._revision(files) != candidate_report["revision"]:
        raise PlanError("api_subtask_result_changed")
    clone = workspace._copy_files(spec, source, files, ".verify-")
    try:
        copy = {**source, "root": str(clone)}
        checked = session.sandbox.run(workspace.policy(spec, copy), report["command"],
                                     timeout=value["subtask"]["timeout"], cancelled=cancelled)
        report.update(commit=candidate_report["commit"], revision=candidate_report["revision"],
                      exit_code=checked["exit_code"], seconds=checked["seconds"], output=checked["output"][-4000:])
        if checked["exit_code"] == 0 and not checked["stop_reason"] and not cancelled():
            if files != workspace.manifest(spec, copy):
                report["reason"] = "api_subtask_check_changed_files"
            else:
                report.update(status="passed", reason=None)
    finally:
        shutil.rmtree(clone)
    return report
