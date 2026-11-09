"""Opt-in Sol escalation, pinned to an auditable request boundary."""

from __future__ import annotations

import json

POLICY = "high-to-max"
TOOL = {"type": "function", "name": "raise_lead_effort",
        "description": "Raise your reasoning effort from high to max for this task when difficulty warrants it. Give the concrete reason. This preserves context and does not retry a failed request.",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string"}},
                       "required": ["reason"], "additionalProperties": False}, "strict": True}
INSTRUCTIONS = """You may raise your reasoning effort from high to max using
raise_lead_effort when the task's difficulty warrants it. Give a concrete reason.
It applies to your following requests in this same conversation. It does not
increase budgets or permissions, fix access/quota failures, or retry a request.
The worker's effort is unchanged. A fresh task keeps its own assigned effort.
"""


def enabled(spec):
    return spec.get("lead_escalation") == POLICY


def current(state, call_index=None):
    index = len(state["calls"]) + 1 if call_index is None else call_index
    events = state.get("effort_events", [])
    return "max" if any(e["effective_call_index"] <= index for e in events) else state["spec"]["lead_effort"]


def reason(arguments):
    try:
        args = json.loads(arguments)
        value = args.get("reason") if isinstance(args, dict) else None
        if (not isinstance(args, dict) or set(args) != {"reason"}
                or not isinstance(value, str) or not value.strip()
                or len(value.encode("utf-8")) > 4096):
            return None
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def apply(state, call, now):
    if not enabled(state["spec"]):
        return {"error": "lead_escalation_not_authorized"}
    why = reason(call.get("arguments"))
    if why is None:
        return {"error": "invalid_escalation_reason"}
    if current(state) == "max":
        return {"effort": "max", "changed": False}
    source = next(c for c in reversed(state["calls"]) if c.get("part_id") is None)
    event = {"from": "high", "to": "max", "initiator": "lead", "reason": why,
             "source_call_index": source["index"], "call_id": call["call_id"],
             "effective_call_index": len(state["calls"]) + 1, "created_at": now}
    state["effort_events"].append(event)
    return {"effort": "max", "changed": True, "effective_call_index": event["effective_call_index"]}


def valid(state):
    events = state.get("effort_events")
    if not enabled(state["spec"]):
        return events is None
    if not isinstance(events, list) or len(events) > 1:
        return False
    for event in events:
        if (not isinstance(event, dict) or set(event) != {"from", "to", "initiator", "reason",
                "source_call_index", "call_id", "effective_call_index", "created_at"}
                or state["spec"]["lead_effort"] != "high"
                or event["from"] != "high" or event["to"] != "max" or event["initiator"] != "lead"
                or not isinstance(event["reason"], str) or not event["reason"].strip()
                or not isinstance(event["created_at"], str)
                or type(event["source_call_index"]) is not int
                or type(event["effective_call_index"]) is not int
                or not 1 <= event["source_call_index"] < event["effective_call_index"] <= len(state["calls"]) + 1):
            return False
        source = state["calls"][event["source_call_index"] - 1]
        if source.get("part_id") is not None or source["status"] != "received":
            return False
        functions = source["response"].get("function_calls", [])
        if (not isinstance(functions, list) or any(not isinstance(c, dict) for c in functions)
                or any(not isinstance(c, dict) for c in state["lead_history"])):
            return False
        matching = [c for c in functions
                    if c.get("call_id") == event["call_id"] and c.get("name") == TOOL["name"]]
        if len(matching) != 1 or reason(matching[0].get("arguments")) != event["reason"]:
            return False
        replies = [c for c in state["lead_history"] if c.get("type") == "function_call_output"
                   and c.get("call_id") == event["call_id"]]
        try:
            expected = {"effort": "max", "changed": True, "effective_call_index": event["effective_call_index"]}
            if len(replies) != 1 or json.loads(replies[0]["output"]) != expected:
                return False
        except (ValueError, KeyError, TypeError, RecursionError):
            return False
    return all(c.get("effort") == current(state, c["index"])
               for c in state["calls"] if c.get("part_id") is None)


def summary(state):
    return {"initial": state["spec"]["lead_effort"], "current": current(state),
            "policy": POLICY, "events": state["effort_events"]}
