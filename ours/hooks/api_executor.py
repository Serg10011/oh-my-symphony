"""Isolated, single-request Responses API transport; never executes tools."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping

import httpx

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_OUTPUT_TOKENS = 4096
MAX_OUTPUT_TOKENS = 32768
TIMEOUT_SECONDS = 120
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Profile:
    name: str
    provider: str
    model: str
    effort: str
    endpoint: str
    key_env: str


PROFILES = {
    "deepseek-lead": Profile("deepseek-lead", "deepseek", "deepseek-v4-pro", "high",
                             "https://api.deepseek.com/responses", "DEEPSEEK_API_KEY"),
    "luna-worker": Profile("luna-worker", "openai", "gpt-6-luna", "max",
                           "https://api.openai.com/v1/responses", "OPENAI_API_KEY"),
    "sol-lead": Profile("sol-lead", "openai", "gpt-6.1-sol", "medium",
                        "https://api.openai.com/v1/responses", "OPENAI_API_KEY"),
}


class ExecutorError(Exception):
    """Public errors contain no request, credentials or provider error body."""

    def __init__(self, code: str, *, charge_possible: bool = False,
                 http_status: int | None = None):
        super().__init__(code)
        self.code = code
        self.charge_possible = charge_possible
        self.http_status = http_status

    def as_dict(self) -> dict:
        return {"version": 1, "error": self.code, "charge_possible": self.charge_possible,
                "http_status": self.http_status}


def profile_for(name: str) -> Profile:
    try:
        return PROFILES[name]
    except (KeyError, TypeError):
        raise ExecutorError("unknown_profile") from None


def _encode(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ExecutorError("invalid_json_value") from None


def _validate_input(value: Any) -> None:
    if isinstance(value, str):
        if not value.strip():
            raise ExecutorError("empty_input")
        return
    if not isinstance(value, list) or not value:
        raise ExecutorError("invalid_input")
    for item in value:
        if not isinstance(item, dict):
            raise ExecutorError("invalid_input_item")
        kind = item.get("type", "message")
        if not isinstance(kind, str):
            raise ExecutorError("invalid_input_item")
        if kind == "message":
            if not isinstance(item.get("role"), str) or item["role"] not in {"system", "developer", "user", "assistant"}:
                raise ExecutorError("invalid_message_role")
            content = item.get("content")
            if isinstance(content, str):
                continue
            if not isinstance(content, list) or not content:
                raise ExecutorError("invalid_message_content")
            for part in content:
                if (not isinstance(part, dict) or not isinstance(part.get("type"), str)
                        or part["type"] not in {"input_text", "output_text", "refusal"}
                        or not isinstance(part.get("refusal" if part.get("type") == "refusal" else "text"), str)):
                    raise ExecutorError("text_input_only")
        elif kind not in {"reasoning", "function_call", "function_call_output"}:
            raise ExecutorError("unsupported_input_item")


def prepare(name: str, request: dict) -> tuple[Profile, dict]:
    """Build a bounded request without reading a key or accessing the network."""
    profile = profile_for(name)
    allowed = {"input", "instructions", "tools", "max_output_tokens", "effort"}
    if not isinstance(request, dict) or set(request) - allowed:
        raise ExecutorError("unknown_request_field")
    _validate_input(request.get("input"))
    effort = request.get("effort", profile.effort)
    choices = {"high", "max", "low"} if profile.provider == "deepseek" else {"low", "medium", "high", "xhigh", "max"}
    if not isinstance(effort, str) or effort not in choices or (name == "luna-worker" and effort != "max"):
        raise ExecutorError("invalid_effort")
    limit = request.get("max_output_tokens", DEFAULT_OUTPUT_TOKENS)
    if type(limit) is not int or not 16 <= limit <= MAX_OUTPUT_TOKENS:
        raise ExecutorError("invalid_output_limit")
    body = {"model": profile.model, "input": request["input"],
            "reasoning": {"effort": effort}, "max_output_tokens": limit, "stream": False}
    if "instructions" in request:
        if not isinstance(request["instructions"], str):
            raise ExecutorError("invalid_instructions")
        body["instructions"] = request["instructions"]
    tools = request.get("tools", [])
    if not isinstance(tools, list):
        raise ExecutorError("invalid_tools")
    names = set()
    for tool in tools:
        if (not isinstance(tool, dict) or tool.get("type") != "function"
                or not isinstance(tool.get("name"), str) or not NAME_RE.fullmatch(tool["name"])
                or tool["name"] in names or not isinstance(tool.get("parameters"), dict)
                or set(tool) - {"type", "name", "description", "parameters", "strict"}):
            raise ExecutorError("function_tools_only")
        names.add(tool["name"])
    if tools:
        body["tools"] = tools
    if profile.provider == "openai":
        body.update(store=False, include=["reasoning.encrypted_content"], service_tier="default")
    if len(_encode(body)) > MAX_REQUEST_BYTES:
        raise ExecutorError("request_too_large")
    return profile, body


def _count(source: dict, key: str) -> int | None:
    value = source.get(key)
    if value is not None and (type(value) is not int or value < 0):
        raise ExecutorError("invalid_usage", charge_possible=True)
    return value


def usage_fields(raw: Any) -> dict:
    """Cache/reasoning counts are subsets, not additional billable tokens."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ExecutorError("invalid_usage", charge_possible=True)
    incoming = raw.get("input_tokens_details", {})
    outgoing = raw.get("output_tokens_details", {})
    incoming = {} if incoming is None else incoming
    outgoing = {} if outgoing is None else outgoing
    if not isinstance(incoming, dict) or not isinstance(outgoing, dict):
        raise ExecutorError("invalid_usage", charge_possible=True)
    inp, out = _count(raw, "input_tokens"), _count(raw, "output_tokens")
    cached, reasoning = _count(incoming, "cached_tokens"), _count(outgoing, "reasoning_tokens")
    total = _count(raw, "total_tokens")
    if ((inp is not None and cached is not None and cached > inp)
            or (out is not None and reasoning is not None and reasoning > out)
            or (inp is not None and out is not None and total is not None and total != inp + out)):
        raise ExecutorError("invalid_usage", charge_possible=True)
    return {"input_tokens": inp, "output_tokens": out, "cached_input_tokens": cached,
            "cache_write_input_tokens": _count(incoming, "cache_write_tokens"),
            "reasoning_tokens": reasoning, "total_tokens": total,
            "usage_known": inp is not None and out is not None, "cost_usd": None}


def normalize(response: Any, profile: Profile, body: dict) -> dict:
    if (not isinstance(response, dict) or not isinstance(response.get("status"), str)
            or response["status"] not in {"completed", "incomplete", "failed"}
            or not isinstance(response.get("id"), str) or not response["id"]
            or not isinstance(response.get("model"), str) or not isinstance(response.get("output"), list)):
        raise ExecutorError("invalid_response", charge_possible=True)
    texts, calls, refusals = [], [], []
    declared = {tool["name"] for tool in body.get("tools", [])}
    for item in response["output"]:
        if not isinstance(item, dict):
            raise ExecutorError("invalid_response", charge_possible=True)
        kind = item.get("type")
        if kind == "message":
            if not isinstance(item.get("content"), list):
                raise ExecutorError("invalid_response", charge_possible=True)
            for part in item["content"]:
                if not isinstance(part, dict):
                    raise ExecutorError("invalid_response", charge_possible=True)
                kind_part = part.get("type")
                key = {"output_text": "text", "refusal": "refusal"}.get(kind_part) if isinstance(kind_part, str) else None
                if key is None or not isinstance(part.get(key), str):
                    raise ExecutorError("invalid_response", charge_possible=True)
                (texts if key == "text" else refusals).append(part[key])
        elif kind == "function_call":
            if (not isinstance(item.get("name"), str) or item["name"] not in declared or not isinstance(item.get("call_id"), str)
                    or not item["call_id"] or not isinstance(item.get("arguments"), str)):
                raise ExecutorError("invalid_function_call", charge_possible=True)
            calls.append({key: item[key] for key in ("call_id", "name", "arguments")})
        elif kind != "reasoning":
            raise ExecutorError("unsupported_response_item", charge_possible=True)
    return {"version": 1, "profile": profile.name, "provider": profile.provider,
            "requested_model": profile.model, "model": response["model"],
            "effort": body["reasoning"]["effort"], "id": response["id"],
            "status": response["status"], "output_text": "".join(texts), "function_calls": calls,
            "service_tier": response.get("service_tier"),
            "refusals": refusals, "output": response["output"],
            "usage": usage_fields(response.get("usage")), "charge_possible": True}


def _exchange(profile: Profile, endpoint: str, body: dict | None, *, environ, transport):
    """One bounded exchange with fixed provider credentials; no retries or redirects."""
    env = os.environ if environ is None else environ
    key = env.get(profile.key_env, "")
    if not key:
        raise ExecutorError("missing_api_key")
    if not all(33 <= ord(char) <= 126 for char in key):
        raise ExecutorError("invalid_api_key")
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False, follow_redirects=False,
                          transport=transport) as client:
            with client.stream("POST" if body is not None else "GET", endpoint,
                               content=_encode(body) if body is not None else None,
                               headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}) as response:
                if response.status_code != 200:
                    raise ExecutorError("http_error", http_status=response.status_code, charge_possible=True)
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise ExecutorError("response_too_large", charge_possible=True)
    except httpx.HTTPError:
        raise ExecutorError("network_error", charge_possible=True) from None
    try:
        return json.loads(data, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ExecutorError("invalid_response_json", charge_possible=True) from None


def measure_input(name: str, request: dict, *, environ=None, transport=None) -> dict:
    """Count OpenAI input or reserve DeepSeek's entire published model capacity."""
    profile, body = prepare(name, request)
    if profile.provider == "openai":
        payload = {k: v for k, v in body.items() if k in {"model", "input", "instructions", "tools", "reasoning"}}
        raw = _exchange(profile, profile.endpoint + "/input_tokens", payload, environ=environ, transport=transport)
        if (not isinstance(raw, dict) or raw.get("object") != "response.input_tokens"
                or type(raw.get("input_tokens")) is not int or not 1 <= raw["input_tokens"] <= 1_050_000):
            raise ExecutorError("invalid_input_count")
        return {"input_tokens_upper_bound": raw["input_tokens"], "basis": "provider_count"}
    raw = _exchange(profile, "https://api.deepseek.com/models", None, environ=environ, transport=transport)
    if not isinstance(raw, dict) or raw.get("object") != "list" or not isinstance(raw.get("data"), list):
        raise ExecutorError("invalid_model_metadata")
    models = [m for m in raw["data"] if isinstance(m, dict) and m.get("id") == profile.model]
    if len(models) != 1:
        raise ExecutorError("invalid_model_metadata")
    model = models[0]
    if (type(model.get("context_window")) is not int or not 1 <= model["context_window"] <= 1_048_576
            or type(model.get("max_output_tokens")) is not int or model["max_output_tokens"] < body["max_output_tokens"]):
        raise ExecutorError("model_limits_unverified")
    return {"input_tokens_upper_bound": model["context_window"], "basis": "full_model_context"}


def execute(name: str, request: dict, *, environ: Mapping[str, str] | None = None,
            transport: httpx.BaseTransport | None = None) -> dict:
    """Send exactly once; the injectable transport is for offline tests."""
    profile, body = prepare(name, request)
    started = time.monotonic()
    raw = _exchange(profile, profile.endpoint, body, environ=environ, transport=transport)
    result = normalize(raw, profile, body)
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def doctor(name: str | None = None) -> dict:
    profiles = [profile_for(name)] if name else list(PROFILES.values())
    return {"version": 1, "network_used": False, "remote_access_verified": False,
            "profiles": [{**asdict(p), "key_present": bool(os.environ.get(p.key_env))} for p in profiles]}


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _read_request(path: str) -> dict:
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        else:
            with Path(path).open("rb") as source:
                raw = source.read(MAX_REQUEST_BYTES + 1)
    except OSError:
        raise ExecutorError("request_read_failed") from None
    if len(raw) > MAX_REQUEST_BYTES:
        raise ExecutorError("request_too_large")
    try:
        return json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ExecutorError("invalid_request_json") from None


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("profiles", help="list profiles without accessing credentials or network")
    check = commands.add_parser("doctor", help="check key presence locally; no network")
    check.add_argument("--profile", choices=PROFILES)
    for name in ["prepare", "request"]:
        command = commands.add_parser(name, help="prepare locally" if name == "prepare" else "send one billable API request")
        command.add_argument("--profile", required=True, choices=PROFILES)
        command.add_argument("--request", default="-", help="request JSON file, default stdin")
    args = parser.parse_args(argv)
    try:
        if args.command == "profiles":
            result = {"version": 1, "profiles": [asdict(p) for p in PROFILES.values()]}
        elif args.command == "doctor":
            result = doctor(args.profile)
        elif args.command == "prepare":
            profile, body = prepare(args.profile, _read_request(args.request))
            result = {"version": 1, "profile": asdict(profile), "body": body, "network_used": False}
        else:
            result = execute(args.profile, _read_request(args.request))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        if args.command == "doctor":
            return 0 if all(p["key_present"] for p in result["profiles"]) else 1
        if args.command == "request" and (result["status"] != "completed" or result["refusals"]):
            return 3
        return 0
    except ExecutorError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(run())
