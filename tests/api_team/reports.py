"""Report fixtures that mirror the board's spend-report schema.

The fixtures are plain report dictionaries, so the detailed text is produced by
the board's own renderer (``ours/hooks/api_session.render_report``) instead of a
copy of the format inside the tests. Full, partial and zero spending are the
three cases the card has to show without inventing amounts.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

RATES = {
    "deepseek-flash": {"input": "0.30", "cached": "0.006", "write": "0.30", "output": "1.20"},
    "gpt-6-luna": {"input": "0.10", "cached": "0.01", "write": "0.25", "output": "0.50"},
    "gpt-6.1-sol": {"input": "2", "cached": "0.10", "write": "5", "output": "10"},
}
FACTORS = {"deepseek-flash": "1", "gpt-6-luna": "1.1", "gpt-6.1-sol": "1.1"}
LONG_CONTEXT = {"deepseek-flash": False, "gpt-6-luna": True, "gpt-6.1-sol": True}
TARIFF = {"version": "2026-11-03-standard-peak-v2", "verified_on": "2026-11-03",
          "valid_until": "2026-11-10", "source": "builtin_catalog"}
MICRO = Decimal("0.000001")


def amount_micros(value: str | None) -> int | None:
    """Convert a plain report amount such as ``"0.130003"`` into micro-USD."""
    if not isinstance(value, str):
        return None
    return int((Decimal(value) * 1_000_000).to_integral_value())


def _tokens(input_tokens, cached, output, reasoning):
    total = None if input_tokens is None or output is None else input_tokens + output
    return {"input_tokens": input_tokens, "cached_input_tokens": cached,
            "output_tokens": output, "reasoning_tokens": reasoning, "total_tokens": total}


def _model(model_name: str, *, calls: int, estimated_usd: str | None, known: str | None,
           estimated_requests: int, complete: bool, priced: bool = True, tokens=None) -> dict:
    row: dict[str, Any] = {"model": model_name, "calls": calls, "estimated_usd": estimated_usd,
                           "estimated_known_usd": known, "estimated_requests": estimated_requests,
                           "estimate_complete": complete, "priced": priced}
    row.update(tokens or _tokens(0, 0, 0, 0))
    if priced:
        row["rates_per_million_tokens"] = RATES[model_name]
        row["regional_factor"] = FACTORS[model_name]
        row["long_context_surcharge"] = LONG_CONTEXT[model_name]
    return row


def _request(index: int, profile: str, model: str, estimated_usd: str | None,
             status: str = "estimated") -> dict:
    return {"index": index, "profile": profile, "model": model, "estimated_usd": estimated_usd,
            "estimate_status": status, "usage_known": estimated_usd is not None}


def _report(*, requests, by_model, totals, cost, status="dialogue_complete",
            stop_reason=None) -> dict:
    return {"version": 1, "kind": "api_session_spend", "status": status,
            "task_acceptance": "not_evaluated", "created_at": "2026-11-04T17:07:00+00:00",
            "started_at": "2026-11-04T17:18:18+00:00", "finished_at": "2026-11-04T17:27:19+00:00",
            "stop_reason": stop_reason, "budget_enforced": False, "calls": len(requests),
            "requests": requests, "by_model": by_model, "totals": totals, "cost": cost}


def _cost(*, total: str | None, known: str | None, estimated: int, missing: int,
          unpriced=(), cache_unknown=0) -> dict:
    return {"estimated_total_usd": total, "estimate_complete": total is not None,
            "estimated_known_usd": known, "estimated_requests": estimated,
            "estimate_kind": "conservative_catalog_estimate", "requests_without_estimate": missing,
            "cache_write_unknown_calls": cache_unknown,
            "provider_billed_total_usd": None, "provider_invoice_verified": False,
            "cost_scope": "generation_requests_of_this_session_only; provider_invoice_not_verified",
            "tariff": dict(TARIFF), "unpriced_models": list(unpriced), "assumptions": []}


def full_report() -> dict:
    """Every request has a catalog estimate; lead and worker have their own model."""
    requests = [_request(1, "deepseek-lead", "deepseek-flash", "0.040000"),
                _request(2, "deepseek-lead", "deepseek-flash", "0.060000"),
                _request(3, "luna-worker", "gpt-6-luna", "0.030003")]
    by_model = {"deepseek-flash": _model("deepseek-flash", calls=2, estimated_usd="0.100000",
                                         known="0.100000", estimated_requests=2, complete=True,
                                         tokens=_tokens(30, 20, 10, 4)),
                "gpt-6-luna": _model("gpt-6-luna", calls=1, estimated_usd="0.030003",
                                     known="0.030003", estimated_requests=1, complete=True,
                                     tokens=_tokens(12, 8, 6, 2))}
    totals = {"calls": 3, "unknown_usage_calls": 0, "input_includes_cached": True,
              "output_includes_reasoning": True, **_tokens(42, 28, 16, 6)}
    return _report(requests=requests, by_model=by_model, totals=totals,
                   cost=_cost(total="0.130003", known="0.130003", estimated=3, missing=0))


def partial_report() -> dict:
    """One of the four requests has no confirmed spend: the total is a known part."""
    requests = [_request(1, "deepseek-lead", "deepseek-flash", "0.040000"),
                _request(2, "deepseek-lead", "deepseek-flash", "0.060000"),
                _request(3, "luna-worker", "gpt-6-luna", "0.030003"),
                _request(4, "luna-worker", "gpt-6-luna", None, status="unknown_usage")]
    by_model = {"deepseek-flash": _model("deepseek-flash", calls=2, estimated_usd="0.100000",
                                         known="0.100000", estimated_requests=2, complete=True,
                                         tokens=_tokens(30, 20, 10, 4)),
                "gpt-6-luna": _model("gpt-6-luna", calls=2, estimated_usd=None, known="0.030003",
                                     estimated_requests=1, complete=False,
                                     tokens=_tokens(None, None, None, None))}
    totals = {"calls": 4, "unknown_usage_calls": 1, "input_includes_cached": True,
              "output_includes_reasoning": True, **_tokens(None, None, None, None)}
    return _report(requests=requests, by_model=by_model, totals=totals,
                   cost=_cost(total=None, known="0.130003", estimated=3, missing=1))


def zero_report() -> dict:
    """The session exists but no request has been paid for yet."""
    totals = {"calls": 0, "unknown_usage_calls": 0, "input_includes_cached": True,
              "output_includes_reasoning": True, **_tokens(0, 0, 0, 0)}
    return _report(requests=[], by_model={}, totals=totals,
                   cost=_cost(total="0.000000", known="0.000000", estimated=0, missing=0))


def unpriced_report() -> dict:
    """A model outside the tariff catalog is unknown spend, not zero spend."""
    requests = [_request(1, "deepseek-lead", "deepseek-flash", "0.040000"),
                _request(2, "luna-worker", "unlisted-model", None, status="unpriced_model")]
    by_model = {"deepseek-flash": _model("deepseek-flash", calls=1, estimated_usd="0.040000",
                                         known="0.040000", estimated_requests=1, complete=True,
                                         tokens=_tokens(10, 4, 5, 1)),
                "unlisted-model": _model("unlisted-model", calls=1, estimated_usd=None,
                                         known="0.000000", estimated_requests=0, complete=False,
                                         priced=False, tokens=_tokens(10, 0, 5, 0))}
    totals = {"calls": 2, "unknown_usage_calls": 0, "input_includes_cached": True,
              "output_includes_reasoning": True, **_tokens(20, 4, 10, 1)}
    return _report(requests=requests, by_model=by_model, totals=totals,
                   cost=_cost(total=None, known="0.040000", estimated=1, missing=1,
                              unpriced=["unlisted-model"]))


def shared_model_report() -> dict:
    """Both participants share one model, so the total is known but not split."""
    requests = [_request(1, "sol-lead", "gpt-6.1-sol", "0.020000"),
                _request(2, "sol-worker", "gpt-6.1-sol", "0.020000")]
    by_model = {"gpt-6.1-sol": _model("gpt-6.1-sol", calls=2, estimated_usd="0.040000",
                                      known="0.040000", estimated_requests=2, complete=True,
                                      tokens=_tokens(20, 0, 10, 2))}
    totals = {"calls": 2, "unknown_usage_calls": 0, "input_includes_cached": True,
              "output_includes_reasoning": True, **_tokens(20, 0, 10, 2)}
    return _report(requests=requests, by_model=by_model, totals=totals,
                   cost=_cost(total="0.040000", known="0.040000", estimated=2, missing=0))


def session_for(report: dict, *, lead: str | None = None, worker: str | None = None,
                lead_model: str | None = None, worker_model: str | None = None) -> dict:
    """The session part of the board reply, including per-profile call counters."""
    names = list(dict.fromkeys(item["profile"] for item in report["requests"]))
    lead = lead or (names[0] if names else "deepseek-lead")
    worker = worker or (names[-1] if len(names) > 1 else "luna-worker")
    lead_model = lead_model or next((item["model"] for item in report["requests"]
                                     if item["profile"] == lead), "deepseek-flash")
    worker_model = worker_model or next((item["model"] for item in report["requests"]
                                         if item["profile"] == worker), "gpt-6-luna")
    profiles = {"lead": {"name": lead, "model": lead_model, "effort": "high"},
                "worker": {"name": worker, "model": worker_model, "effort": "max"}}
    usage = {name: {"calls": sum(1 for item in report["requests"] if item["profile"] == name)}
             for name in dict.fromkeys([lead, worker])}
    return {"version": 1, "status": report["status"], "profiles": profiles,
            "lead_profile": lead, "worker_profile": worker,
            "calls": len(report["requests"]), "usage": usage}


def board_profiles(lead: str = "deepseek-flash", worker: str = "gpt-6-luna") -> dict:
    """The assignment part of the board reply; it carries models but no profile names."""
    return {"lead": {"model": lead, "effort": "high"}, "worker": {"model": worker, "effort": "max"}}


def known_micros(report: dict) -> int:
    return amount_micros(report["cost"]["estimated_known_usd"])
