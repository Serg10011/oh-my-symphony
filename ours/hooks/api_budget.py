"""Conservative generation-cost reservations under a pinned, expiring tariff."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_CEILING
import re

from api_executor import FLASH_WORKER

CATALOG = {
    "version": "2026-11-03-standard-peak-v2", "verified_on": "2026-11-03", "valid_until": "2026-11-10",
    "models": {
        "deepseek-flash": {"input": "0.30", "cached": "0.006", "write": "0.30", "output": "1.20",
                           "long_context": False, "regional_factor": "1"},
        "deepseek-v4-pro": {"input": "1.32", "cached": "0.044", "write": "1.32", "output": "3.96",
                            "long_context": False, "regional_factor": "1"},
        "gpt-6-luna": {"input": "0.10", "cached": "0.01", "write": "0.25", "output": "0.50",
                       "long_context": True, "regional_factor": "1.1"},
        "gpt-6.1-sol": {"input": "2", "cached": "0.10", "write": "5", "output": "10",
                        "long_context": True, "regional_factor": "1.1"},
    },
}
MONEY = re.compile(r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,6})?\Z")
MICRO = Decimal("0.000001")


class BudgetError(Exception):
    """A fixed local error code with no request or credential content."""


def today() -> date:
    return datetime.now(timezone.utc).date()


def money(value) -> Decimal:
    if not isinstance(value, str) or not MONEY.fullmatch(value):
        raise BudgetError("invalid_budget_amount")
    return Decimal(value)


def display(value: Decimal) -> str:
    return str(value.quantize(MICRO, rounding=ROUND_CEILING))


def validate(raw) -> dict:
    if not isinstance(raw, dict) or set(raw) != {"max_usd", "input_tokens_total"}:
        raise BudgetError("invalid_budget")
    amount = money(raw["max_usd"])
    tokens = raw["input_tokens_total"]
    if amount <= 0 or type(tokens) is not int or not 1 <= tokens <= 100_000_000:
        raise BudgetError("invalid_budget")
    return {"max_usd": display(amount), "input_tokens_total": tokens}


def snapshot() -> dict:
    fresh(CATALOG)
    return deepcopy(CATALOG)


def fresh(catalog: dict) -> None:
    if catalog != CATALOG:
        raise BudgetError("tariff_snapshot_changed")
    if not date.fromisoformat(catalog["verified_on"]) <= today() <= date.fromisoformat(catalog["valid_until"]):
        raise BudgetError("tariff_expired")


def _rates(catalog, model: str, incoming: int):
    if model not in catalog["models"]:
        raise BudgetError("unpriced_model")
    row = catalog["models"][model]
    long = row["long_context"] and incoming > 272_000
    factor = Decimal(row["regional_factor"])
    return {key: Decimal(row[key]) * factor * (Decimal("1.5") if long and key == "output" else 2 if long else 1)
            for key in ["input", "cached", "write", "output"]}


def cost(catalog, model: str, incoming: int, outgoing: int, *, cached=0, written=None) -> str:
    # Reject impossible per-call usage before Decimal arithmetic or rate selection.
    if (any(type(v) is not int or not 0 <= v <= 1_050_000 for v in [incoming, outgoing, cached]) or cached > incoming
            or (written is not None and (type(written) is not int or not 0 <= written <= incoming - cached))):
        raise BudgetError("untrusted_budget_usage")
    rates = _rates(catalog, model, incoming)
    # Unknown cache writes are reserved at the most expensive supported write lifetime.
    written = incoming - cached if written is None else written
    total = ((incoming - cached - written) * rates["input"] + cached * rates["cached"]
             + written * rates["write"] + outgoing * rates["output"]) / 1_000_000
    return display(total)


def totals(state: dict) -> dict:
    incoming, amount, breached = 0, Decimal(0), False
    for call in state["calls"]:
        record = call.get("budget_reservation")
        if record is None:
            continue
        if call["status"] == "error" and not call.get("error", {}).get("charge_possible", True):
            continue
        incoming += record.get("settled_input_tokens", record["input_tokens"])
        amount += money(record.get("settled_cost_usd", record["cost_usd"]))
        breached |= bool(record.get("contract_breached"))
    return {"input_tokens_committed": incoming, "generation_cost_upper_usd": display(amount),
            "contract_breached": breached}


def quote(state: dict, model: str, input_bound: int, output_cap: int, basis: str) -> dict:
    policy, catalog = state["spec"]["budget"], state["budget_catalog"]
    fresh(catalog)
    used = totals(state)
    reservation = cost(catalog, model, input_bound, output_cap)
    if used["input_tokens_committed"] + input_bound > policy["input_tokens_total"]:
        raise BudgetError("input_budget_exhausted")
    if money(used["generation_cost_upper_usd"]) + money(reservation) > money(policy["max_usd"]):
        raise BudgetError("money_budget_exhausted")
    return {"input_tokens": input_bound, "cost_usd": reservation, "basis": basis}


def settle(state: dict, call: dict, response: dict) -> str | None:
    record, usage = call["budget_reservation"], response["usage"]
    if not usage["usage_known"]:
        return "unknown_usage"
    model = response["requested_model"]
    # No release on a changed model or an unverified processing tier.
    if response["model"] != model or (response["provider"] == "openai" and response.get("service_tier") != "default"):
        record["contract_breached"] = True
        return "billing_contract_unverified"
    incoming, outgoing = usage["input_tokens"], usage["output_tokens"]
    try:
        observed = cost(state["budget_catalog"], model, incoming, outgoing,
                        cached=usage["cached_input_tokens"] or 0, written=usage["cache_write_input_tokens"])
    except BudgetError:
        record["contract_breached"] = True
        return "untrusted_budget_usage"
    if incoming > record["input_tokens"] or outgoing > call["output_cap"] or money(observed) > money(record["cost_usd"]):
        record.update(contract_breached=True, settled_input_tokens=max(incoming, record["input_tokens"]),
                      settled_cost_usd=display(max(money(observed), money(record["cost_usd"]))))
        return "billing_contract_exceeded"
    record.update(settled_input_tokens=incoming, settled_cost_usd=observed)
    return None


def validate_state(state: dict) -> None:
    catalog = state.get("budget_catalog")
    if (not isinstance(catalog, dict) or not isinstance(catalog.get("version"), str)
            or not isinstance(catalog.get("models"), dict) or not isinstance(state.get("preflights"), list)):
        raise BudgetError("invalid_budget_state")
    if len(state["preflights"]) > state["spec"]["limits"]["max_calls"]:
        raise BudgetError("invalid_budget_state")
    for call in state["calls"]:
        record = call.get("budget_reservation")
        if call["profile"] == FLASH_WORKER:
            if "budget_reservation" in call:
                raise BudgetError("invalid_budget_state")
            continue
        if (not isinstance(record, dict) or type(record.get("input_tokens")) is not int
                or not 1 <= record["input_tokens"] <= 1_050_000):
            raise BudgetError("invalid_budget_state")
        money(record.get("cost_usd"))
        if "settled_cost_usd" in record:
            money(record["settled_cost_usd"])
        if "settled_input_tokens" in record and (type(record["settled_input_tokens"]) is not int or record["settled_input_tokens"] < 0):
            raise BudgetError("invalid_budget_state")
    totals(state)
