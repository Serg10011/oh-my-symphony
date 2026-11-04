"""Stable explicit OpenAI cache boundaries, separate from transient resources."""

from copy import deepcopy


def input_with_breakpoints(history, calls, part_id):
    """Keep recent prior write boundaries available for exact prefix lookup."""
    items = deepcopy(history)
    eligible = {}
    for index, item in enumerate(items):
        if item.get("role") in {"user", "developer"} and isinstance(item.get("content"), str):
            item["content"] = [{"type": "input_text", "text": item["content"]}]
        if item.get("type") == "function_call_output" and isinstance(item.get("output"), str):
            item["output"] = [{"type": "input_text", "text": item["output"]}]
        content = item.get("output") if item.get("type") == "function_call_output" else item.get("content")
        if (item.get("role") in {"user", "developer"} or item.get("type") == "function_call_output") and isinstance(content, list):
            if content and content[-1].get("type") == "input_text":
                eligible[index] = content[-1]
    if not eligible:
        return items
    boundaries = {max(eligible)}
    for call in calls:
        index = call.get("history_items", 0) - 2
        if (call.get("request_version", 1) == 2 and call.get("part_id") == part_id
                and index in eligible):
            boundaries.add(index)
    # Explicit-only mode writes no transient resource footer. Retain previous
    # selected boundaries as lookup points even after a large group of tool outputs.
    for index in sorted(boundaries)[-4:]:
        eligible[index]["prompt_cache_breakpoint"] = {"mode": "explicit"}
    return items
