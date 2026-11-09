"""A text worker cannot bypass Symphony's tool registry or invent usage."""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ours/hooks"))
import api_flash_protocol as protocol  # noqa: E402
import api_project as project  # noqa: E402
from test_api_project import repository, snapshot  # noqa: E402

TOOLS = [{"type": "function", "name": "read_project_file", "parameters": {"type": "object"}}]


def test_request_is_data_and_preserves_its_arguments_for_the_controller():
    reply = protocol.parse_reply({"text": "", "requests": [{"name": "read_project_file",
        "arguments": {"path": "../outside"}}]}, TOOLS, turn_id="part_1_turn_2")
    # The existing workspace controller owns path validation and execution.
    assert reply["function_calls"][0] == {"type": "function_call", "name": "read_project_file",
        "call_id": "flash_part_1_turn_2_0", "arguments": '{"path": "../outside"}'}
    assert reply["output_text"] == "" and reply["status"] == "completed"


def test_native_commands_and_other_lead_tools_cannot_be_requested():
    for name in ("run_command", "raise_lead_effort", "delegate_part", "write_project_file"):
        with pytest.raises(protocol.ProtocolError, match="flash_invalid_request"):
            protocol.parse_reply({"text": "", "requests": [{"name": name, "arguments": {}}]},
                                 TOOLS, turn_id="turn1")


def test_translated_requests_use_the_existing_project_scope_check(tmp_path):
    root = repository(tmp_path, {"allowed.txt": "visible", "private.txt": "not-shared"})
    _, fixed = snapshot(root, ["allowed.txt"])
    for index, path in enumerate(("allowed.txt", "private.txt", "../outside")):
        reply = protocol.parse_reply({"text": "", "requests": [{"name": "read_project_file",
            "arguments": {"path": path, "offset": 0, "max_chars": 100}}]}, project.TOOLS,
            turn_id=f"t{index}")
        call = reply["function_calls"][0]
        if path == "allowed.txt":
            assert project.call(fixed, call["name"], json.loads(call["arguments"]))["text"] == "visible"
        else:
            with pytest.raises(project.ProjectError):
                project.call(fixed, call["name"], json.loads(call["arguments"]))


def test_text_result_and_unique_host_assigned_call_ids():
    value = {"text": "Готово", "requests": []}
    reply = protocol.parse_reply(value, TOOLS, turn_id="turn1")
    assert reply["output"][0]["content"] == [{"type": "output_text", "text": "Готово"}]
    call = {"text": "", "requests": [{"name": "read_project_file", "arguments": {}}]}
    first = protocol.parse_reply(call, TOOLS, turn_id="turn1")["function_calls"][0]
    second = protocol.parse_reply(call, TOOLS, turn_id="turn2")["function_calls"][0]
    assert first["call_id"] != second["call_id"]


@pytest.mark.parametrize("value", [None, {}, {"text": "", "requests": []},
    {"text": "ready", "requests": [], "execute": "shell"},
    {"text": [], "requests": []}, {"text": "", "requests": [{}]},
    {"text": "", "requests": [{"name": "read_project_file", "arguments": "[]"}]},
    {"text": "", "requests": [{"name": "read_project_file", "arguments": '{"x":NaN}'}]},
    {"text": "", "requests": [{"name": "read_project_file", "arguments": {}, "call_id": "forged"}]}])
def test_malformed_or_injected_reply_fails_closed(value):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_reply(value, TOOLS, turn_id="turn1")


def test_empty_tool_registry_disables_requests():
    assert protocol.schema([])["properties"]["requests"]["maxItems"] == 0
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_reply({"text": "", "requests": [{"name": "read_project_file", "arguments": {}}]},
                             [], turn_id="turn1")


def test_cumulative_counters_do_not_charge_history_twice_or_double_count_reasoning():
    first = {"input_tokens": 2247, "output_tokens": 32693, "thinking_tokens": 32619,
             "cache_read_tokens": 0, "total_tokens": 34940}
    second = {"input_tokens": 2525, "output_tokens": 32697, "thinking_tokens": 32619,
              "cache_read_tokens": 30214, "total_tokens": 35222}
    usage = protocol.usage_delta(second, first)
    assert usage["input_tokens"] == 30492 and usage["cached_input_tokens"] == 30214
    assert usage["output_tokens"] == 4 and usage["reasoning_tokens"] == 0
    assert usage["total_tokens"] == 30496 and usage["cost_usd"] is None
    assert usage["billing"] == "subscription" and usage["usage_known"]


@pytest.mark.parametrize("changes", [{"input_tokens": None}, {"output_tokens": True},
    {"thinking_tokens": -1}, {"thinking_tokens": 30}, {"cache_read_tokens": 1.5},
    {"total_tokens": 14}, {"input_tokens": 10**16}])
def test_unknown_or_inconsistent_usage_is_not_reported_as_zero(changes):
    value = {"input_tokens": 10, "output_tokens": 5, "thinking_tokens": 2,
             "cache_read_tokens": 0, "total_tokens": 15} | changes
    with pytest.raises(protocol.ProtocolError):
        protocol.usage_delta(value)


def test_counter_reset_is_not_mistaken_for_a_free_turn():
    before = {"input_tokens": 10, "output_tokens": 5, "thinking_tokens": 2,
              "cache_read_tokens": 0, "total_tokens": 15}
    with pytest.raises(protocol.ProtocolError, match="flash_usage_counter_reset"):
        protocol.usage_delta(dict.fromkeys(protocol.COUNTERS, 0), before)
