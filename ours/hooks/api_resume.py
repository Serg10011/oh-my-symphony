"""Explicit recovery at verified network or unapplied read-batch boundaries."""

from __future__ import annotations

import hashlib
from copy import deepcopy
import json
import os
from pathlib import Path

import api_session as session


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject():
    raise session.SessionError("resume_history_inconsistent")


def _pending(history):
    """Only a suffix of an ordered tool batch may remain unanswered."""
    seen, pending = set(), []
    for item in history:
        if not isinstance(item, dict):
            _reject()
        kind = item.get("type")
        if kind == "function_call":
            identifier = item.get("call_id")
            if not isinstance(identifier, str) or not identifier or identifier in seen:
                _reject()
            seen.add(identifier)
            pending.append({k: item[k] for k in ("call_id", "name", "arguments")})
        elif kind == "function_call_output":
            if not pending or item.get("call_id") != pending[0]["call_id"]:
                _reject()
            pending.pop(0)
    return pending


def _verify_request(state, call, history):
    if call.get("history_items") != len(history) + 1 or not isinstance(call.get("resources"), dict):
        _reject()
    request = session._build_request(state, history, call["output_cap"], call["resources"],
                                     part_id=call.get("part_id"), request_version=call.get("request_version", 1))
    _, body = session.api.prepare(call["profile"], request)
    if _digest(session._encode(body)) != call.get("request_sha256"):
        raise session.SessionError("resume_request_changed")


def _boundary(state):
    if (state["status"] != "stopped" or state.get("stop_reason") not in {"api_error", "invalid_tool_batch"}
            or not state["calls"] or state.get("result") is not None):
        raise session.SessionError("resume_not_supported")
    last = state["calls"][-1]
    read_batch = state["stop_reason"] == "invalid_tool_batch"
    error = last.get("error", {})
    if not read_batch and (last["status"] != "error" or error.get("error") != "network_error"
            or error.get("charge_possible") is not True or error.get("http_status") is not None):
        raise session.SessionError("resume_not_supported")
    if read_batch:
        calls = last.get("response", {}).get("function_calls", [])
        names = {t["name"] for t in (session.project.LEGACY_TOOLS if last.get("request_version", 1) == 1 else session.project.TOOLS)}
        if ("project" not in state["spec"] or len(calls) <= 32
                or any(c["name"] not in names or not isinstance(c["call_id"], str) or not 1 <= len(c["call_id"]) <= 256 for c in calls)):
            raise session.SessionError("resume_not_supported")
    if state["profiles"] != session._profiles(state["spec"]):
        raise session.SessionError("profile_changed_since_init")
    if not state["lead_history"] or state["lead_history"][0] != {"role": "user", "content": state["spec"]["task"]}:
        _reject()
    resumes = state.get("resumptions", [])
    if not isinstance(resumes, list):
        _reject()
    accounted = set()
    for event in resumes:
        previous = event.get("source_call_index", event["uncertain_call_index"])
        following = event["next_call_index"]
        if (type(previous) is not int or type(following) is not int or previous in accounted
                or not 1 <= previous < following <= len(state["calls"]) or following != previous + 1
                or state["calls"][following - 1].get("resume_of") != previous):
            _reject()
        old = state["calls"][previous - 1]
        kind = event.get("recovery_kind", "network_error")
        if kind == "network_error":
            if old["status"] != "error" or old.get("error", {}).get("error") != "network_error":
                _reject()
        elif kind == "unapplied_read_batch":
            if old["status"] != "received" or len(old.get("response", {}).get("function_calls", [])) <= 32:
                _reject()
        else:
            _reject()
        accounted.add(previous)
    for call in state["calls"] if read_batch else state["calls"][:-1]:
        if call["status"] == "error" and call["index"] in accounted:
            if call.get("error", {}).get("error") != "network_error":
                _reject()
            continue
        response = call.get("response", {})
        usage = response.get("usage", {})
        if (call["status"] != "received" or response.get("status") != "completed"
                or response.get("refusals") or not usage.get("usage_known")
                or type(usage.get("input_tokens")) is not int or usage["input_tokens"] < 0
                or type(usage.get("output_tokens")) is not int or not 0 <= usage["output_tokens"] <= call["output_cap"]):
            _reject()
    if any(c["status"] == "in_flight" for c in state.get("file_calls", [])):
        _reject()
    part_id = last.get("part_id")
    if part_id is not None and part_id not in state["parts"]:
        _reject()
    expected_profile = state["spec"]["worker_profile" if part_id is not None else "lead_profile"]
    if last["profile"] != expected_profile:
        _reject()
    for key, part in state["parts"].items():
        if _pending(part["history"]) and not (read_batch and key == part_id):
            _reject()
    pending = _pending(state["lead_history"])
    history = state["lead_history"] if part_id is None else state["parts"][part_id]["history"]
    if read_batch:
        start = last["history_items"] - 1
        if (start < 1 or history[start:] != last["response"]["output"]
                or _pending(history) != last["response"]["function_calls"]):
            _reject()
        _verify_request(state, last, history[:start])
    else:
        _verify_request(state, last, history)
    if part_id is None:
        if pending and not read_batch:
            _reject()
    else:
        if not pending or pending[0]["name"] != "delegate_luna":
            _reject()
        lead = next(c for c in reversed(state["calls"][:-1]) if c.get("part_id") is None)
        response = lead["response"]
        batch = response["function_calls"]
        if not batch or pending != batch[-len(pending):]:
            _reject()
        start = lead["history_items"] - 1
        _verify_request(state, lead, state["lead_history"][:start])
        end = start + len(response["output"])
        if state["lead_history"][start:end] != response["output"]:
            _reject()
        completed = state["lead_history"][end:]
        if (len(completed) != len(batch) - len(pending)
                or any(item.get("type") != "function_call_output" or item.get("call_id") != call["call_id"]
                       for item, call in zip(completed, batch))):
            _reject()
        part = state["parts"][part_id]
        assignments = [item for item in history if item.get("role") == "user"]
        if len(assignments) != part["attempts"]:
            _reject()
        previous_parts = {key: dict(value) for key, value in state["parts"].items()}
        if part["attempts"] == 1:
            del previous_parts[part_id]
        else:
            previous_parts[part_id]["attempts"] -= 1
        args, failure = session._arguments(pending[0], previous_parts)
        if (failure or args["part_id"] != part_id or not assignments
                or assignments[-1] != {"role": "user", "content": args["message"]}):
            _reject()
    return last, pending


def _plan(path, raw, state):
    try:
        last, pending = _boundary(state)
        spec = state["spec"]
        if "project" in spec:
            _, snapshot = session.project.snapshot(spec["project"])
            if snapshot != state["project_snapshot"]:
                raise session.SessionError("resume_project_changed")
        if "budget" in spec:
            session.budget.fresh(state["budget_catalog"])
            if session.budget.totals(state)["contract_breached"]:
                raise session.SessionError("resume_budget_breached")
        remaining = spec["limits"]["output_tokens_total"] - session._spent(state)
        worker = last.get("part_id") is not None
        if (len(state["calls"]) >= spec["limits"]["max_calls"]
                or remaining < session.MIN_RESPONSE_TOKENS
                or (worker and not session._worker_has_room(state))):
            raise session.SessionError("resume_limits_exhausted")
        cap = min(session._output_limits(state)[last["profile"]], session.api.output_limit(last["profile"]),
                  remaining - (session.MIN_RESPONSE_TOKENS if worker else 0))
        history = state["parts"][last["part_id"]]["history"] if worker else state["lead_history"]
        prepared = []
        read_batch = state["stop_reason"] == "invalid_tool_batch"
        if read_batch:
            history = deepcopy(history)
            for index, call in enumerate(last["response"]["function_calls"]):
                answer = session._project_answer(state, call, deferred=index >= session.PROJECT_CALLS_PER_RESPONSE)
                if answer[1]:
                    raise session.SessionError("resume_project_unavailable")
                prepared.append((call, answer))
                history.append({"type": "function_call_output", "call_id": call["call_id"],
                                "output": json.dumps(answer[0], ensure_ascii=False)})
        resources = session._resources(state, cap, worker=worker)
        request = session._build_request(state, history, cap, resources, part_id=last.get("part_id"))
        _, body = session.api.prepare(last["profile"], request)
        encoded = session._encode(body)
        if len(encoded) > spec["limits"]["request_bytes"]:
            raise session.SessionError("request_size_limit")
        runtime = {name: _digest(Path(__file__).with_name(name).read_bytes()) for name in
                   ("api_resume.py", "api_session.py", "api_executor.py", "api_budget.py", "api_project.py", "api_cache.py")}
        plan = {"version": 1, "status": "resume_ready", "state_path": str(path),
                "state_sha256": _digest(raw), "runtime_sha256": runtime,
                "uncertain_call_index": None if read_batch else last["index"], "next_call_index": last["index"] + 1,
                "source_call_index": last["index"], "recovery_kind": "unapplied_read_batch" if read_batch else "network_error",
                "local_read_replies": len(prepared), "local_replies_sha256": _digest(session._encode(prepared)),
                "profile": last["profile"], "part_id": last.get("part_id"), "profiles": state["profiles"],
                "pending_lead_tool_ids": [c["call_id"] for c in pending],
                "resources": resources, "limits": spec["limits"], "output_limits": session._output_limits(state),
                "next_request_sha256": _digest(encoded), "next_request_bytes": len(encoded),
                "budget_enforced": "budget" in spec, "budget": session.summary(state).get("budget"),
                "project_digest": state.get("project_snapshot", {}).get("digest"),
                "possible_previous_charge": any(c["status"] == "error" and c.get("error", {}).get("charge_possible") for c in state["calls"]),
                "provider_retrieval_available": False, "next_request_version": 2,
                "new_api_requests": 0, "scope": "Continue the same task within its remaining grants; stop again on failure"}
        plan["plan_sha256"] = _digest(session._encode(plan))
        return plan, pending, prepared
    except (KeyError, TypeError, ValueError, IndexError, StopIteration, AttributeError):
        _reject()
    except (session.api.ExecutorError, session.budget.BudgetError, session.project.ProjectError) as exc:
        raise session.SessionError(str(exc)) from None


def _read_locked(path):
    raw = session._read_state(path)
    try:
        return raw, session._load(path, raw=raw)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise session.SessionError("invalid_state") from None


def plan(path):
    """Inspect locally under the session lock; no key access, mutation or network."""
    path = session._path(path)
    with session._lock(path):
        raw, state = _read_locked(path)
        return _plan(path, raw, state)[0]


def _backup(path, raw, checksum):
    backup = path.with_name(path.name + ".before-resume-" + checksum + ".json")
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if session._read_state(backup) != raw:
            raise session.SessionError("resume_backup_conflict") from None
        return backup
    with os.fdopen(fd, "wb") as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return backup


def resume(path, *, expected_plan_sha256, accept_possible_charge=False, environ=None, transport=None,
           require_budget=False):
    """An explicit paid action. A stale plan cannot consume another generation."""
    if accept_possible_charge is not True:
        raise session.SessionError("resume_charge_acknowledgement_required")
    path = session._path(path)
    with session._lock(path):
        raw, state = _read_locked(path)
        planned, pending, prepared = _plan(path, raw, state)
        if expected_plan_sha256 != planned["plan_sha256"]:
            raise session.SessionError("resume_plan_changed")
        if require_budget and "budget" not in state["spec"]:
            raise session.SessionError("budget_required")
        env = dict(os.environ if environ is None else environ)
        session._credentials(state["spec"], env)
        backup = _backup(path, raw, planned["state_sha256"])
        state.setdefault("resumptions", []).append({
            "plan_sha256": planned["plan_sha256"], "source_state_sha256": planned["state_sha256"],
            "backup": str(backup), "uncertain_call_index": planned["uncertain_call_index"],
            "source_call_index": planned["source_call_index"], "recovery_kind": planned["recovery_kind"],
            "next_call_index": planned["next_call_index"], "part_id": planned["part_id"],
            "accepted_at": session._now(), "possible_previous_charge_acknowledged": True,
            "previous_stop_reason": state["stop_reason"], "previous_finished_at": state.get("finished_at"),
        })
        state.update(status="running", stop_reason=None)
        state.pop("finished_at", None)
        session._save(path, state)
        history = state["parts"][planned["part_id"]]["history"] if planned["part_id"] is not None else state["lead_history"]
        for call, answer in prepared:
            session._project_call(path, state, call, history, part_id=planned["part_id"], prepared=answer)
        if planned["part_id"] is not None:
            session._dispatch(path, state, pending, environ=env, transport=transport, resumed_part=planned["part_id"])
        return session._lead_loop(path, state, environ=env, transport=transport)
