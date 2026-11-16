"""Run an approved plan in order and retain each locally verified version."""

from __future__ import annotations

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


def _parent(board, value, *, recheck):
    if (not value or not board.tracker.find_path(board.identifier) or not value.get("planning_team")
            or value.get("status") != "result" or value.get("plan_batch", {}).get("status") != "ready"):
        raise PlanError("api_plan_not_ready")
    front, body = board.card()
    api_plan._parent(board, value, front, body)
    if front["state"] != "Subtasks":
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


def prepare(board, value):
    run = _load(board)
    if run and run["status"] != "prepared":
        raise PlanError("api_subtasks_already_used")
    terms = _terms(board, value)
    token = secrets.token_urlsafe(32)
    _save(board, {"version": 1, "parent": board.identifier, "status": "prepared", "terms": terms,
                  "fingerprint": _digest(terms), "token": _digest(token),
                  "expires_at": time.time() + 600, "checked": []})
    return token


def confirm(board, value, token):
    run = _load(board)
    if (not run or not isinstance(token, str) or len(token) > 256
            or not hmac.compare_digest(run["token"], _digest(token))):
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
    return {"status": run["status"], "checked": count, "total": len(parts), "profiles": session._profiles(spec),
            "budget": spec.get("budget"), "limits": spec["limits"], "reason": run.get("reason"),
            "next": parts[count]["identifier"] if count < len(parts) else None}


def cancel(board):
    run = _load(board)
    if run and run["status"] in {"prepared", "active"}:
        run.update(status="stopped", reason="operator_stopped", finished_at=session._now())
        _save(board, run)


def _child_binding(parent, run, part):
    spec = run["terms"]["spec"]["project"]
    previous = run["checked"][-1]["commit"] if run["checked"] else spec["commit"]
    return {"parent": parent.identifier, "fingerprint": run["fingerprint"], "check": part["check"],
            "repository": str(parent.root / "subtasks.git"), "base_commit": previous,
            "timeout": run["terms"]["timeout"]}


def live(board, assignment, *, recheck=False):
    """The parent grant can disappear after a child has entered the queue."""
    link = assignment["subtask"]
    parent = _board(board, link["parent"])
    run = _load(parent)
    if run is None or run["status"] != "active":
        raise PlanError("api_subtasks_not_active")
    _parent(parent, parent._load(), recheck=recheck)
    parts, count = run["terms"]["parts"], len(run["checked"])
    if (count >= len(parts) or parts[count]["identifier"] != board.identifier
            or parts[count]["card_digest"] != assignment["card_digest"]
            or _child_binding(parent, run, parts[count]) != link):
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
        spec["budget"] = _remaining(spec["budget"], [row["usage"] for row in run["checked"]])
    if spec["lead_profile"] == "deepseek-lead" and terms["context_paths"]:
        spec["project_context"] = project_context.capture(snapshot, parent.teams.binding, terms["context_paths"])
    return {"version": 1, "identifier": child.identifier, "status": "queued", "spec": spec,
            "profiles": session._profiles(spec), "rules": terms["rules"], "subtask": link,
            "card_digest": part["card_digest"], "board_stages": [child.context["active_states"][0]],
            "permission_revision": terms["permission_revision"], "authorization": terms["authorization"],
            "created_at": session._now(), "confirmed_at": run["confirmed_at"]}


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
    previous = _board(parent, record["identifier"])
    assignment = previous._load()
    if not previous.tracker.find_path(previous.identifier) or _verified(previous, assignment) != record:
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
            if assignment and assignment["status"] == "running":
                assignment = child._recover_running(assignment)
                if assignment["status"] == "running":
                    return
            if assignment and assignment["status"] in {"result", "stopped"}:
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
            if not assignment:
                _parent(parent, value, recheck=True)
                _verify_predecessor(parent, run)
            with session._lock(child.path):
                if assignment is None:
                    if child._load() is not None or child.stop_path.exists():
                        raise PlanError("api_plan_subtask_changed")
                    front, body = child.card()
                    if front["state"] != "Subtasks" or child.card_digest(front, body) != part["card_digest"]:
                        raise PlanError("api_plan_subtask_changed")
                    assignment = _queue(parent, child, run, part, link)
                    child._save(assignment)
                todo = child.context["active_states"][0]
                _move(child, assignment, todo, {"Subtasks", todo})
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
