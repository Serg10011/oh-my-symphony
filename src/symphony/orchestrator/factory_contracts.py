"""Factory stage contracts, isolated from default and deep preset gates."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from ..factory.wayfinder import factory_acceptance_items, factory_proof_commands

from .contracts import (
    ContractResult,
    _build_result,
    _missing_sections,
    _markdown_table_header,
    _normalize_cell,
    _parse_markdown_table_rows,
    _section_body_text,
)

_FACTORY_BUILD_REQUIRED = (
    "## Implementation",
    "## Full Spec Review",
    "## Edge Case Review",
    "## Adversarial Review",
    "## Test Evidence",
)


_FACTORY_EVIDENCE_PLACEHOLDERS = frozenset(
    {"", "n/a", "na", "none", "not run", "tbd", "-", "--", "—"}
)
_INLINE_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_MARKDOWN_INLINE_LINK_RE = re.compile(r"!?\[([^\]]+)]\([^)]+\)")
_MARKDOWN_REFERENCE_LINK_RE = re.compile(r"!?\[([^\]]+)]\[[^\]]*]")


def evaluate_factory_contract(
    producing_state: str, body: str, advanced: str, *, docs_root: Path | None = None
) -> ContractResult:
    state = producing_state.strip().lower()
    # A genuine unavailable-environment handoff must never require success proof.
    if advanced == "blocked":
        return _build_result(producing_state, _missing_sections(body, ("## Blocker",)))
    if advanced in {"cancelled", "canceled", "human review"}:
        return ContractResult(passed=True)
    if state == "build":
        missing = _missing_sections(body, _FACTORY_BUILD_REQUIRED)
        if advanced and advanced != "verify":
            missing.append(
                "Build must advance to Verify for an independent fresh-context turn"
            )
        return _build_result(producing_state, missing)
    if state == "verify":
        return _evaluate_factory_verify_contract(producing_state, body)
    return ContractResult(passed=True)


def _evaluate_factory_verify_contract(
    producing_state: str, body: str
) -> ContractResult:
    acceptance_heading = "## Acceptance criteria"
    heading = "## Verification"
    missing = _missing_sections(body, (acceptance_heading, heading))
    acceptance_criteria = _factory_acceptance_criteria(body, acceptance_heading)
    if acceptance_heading not in missing and not acceptance_criteria:
        missing.append(
            "## Acceptance criteria must contain at least one bullet or numbered item"
        )
    if heading in missing:
        return _build_result(producing_state, missing)

    required = ("criterion", "command", "result")
    header = _markdown_table_header(body, heading)
    if not all(name in header for name in required):
        missing.append("## Verification table with `criterion | command | result`")
        return _build_result(producing_state, missing)

    rows = _parse_markdown_table_rows(body, heading, escaped_pipes=True)
    if not rows:
        missing.append("## Verification table must contain at least one evidence row")
        return _build_result(producing_state, missing)

    indexes = {name: header.index(name) for name in required}
    verification_criteria: list[str] = []
    declared = factory_proof_commands(body)
    if not declared:
        missing.append(
            "## Proof commands or ## Proof must declare exact commands in inline code"
        )
    verified_commands: set[str] = set()
    for row in rows:
        if len(row.cells) <= max(indexes.values()):
            missing.append(f"## Verification row {row.row} is incomplete")
            continue
        criterion_cell = row.cells[indexes["criterion"]]
        criterion = _normalize_cell(criterion_cell)
        command_cell = row.cells[indexes["command"]].strip()
        commands = _INLINE_CODE_SPAN_RE.findall(command_cell)
        if commands:
            separators = _INLINE_CODE_SPAN_RE.sub("", command_cell)
            if not re.fullmatch(r"\s*(?:(?:<br\s*/?>|[,;])\s*)*", separators):
                missing.append(f"## Verification row {row.row} command cell contains text outside exact proof commands")
        else:
            commands = [command_cell]
        result = row.cells[indexes["result"]].strip().strip("`").strip()
        if criterion in _FACTORY_EVIDENCE_PLACEHOLDERS:
            missing.append(f"## Verification row {row.row} criterion is missing")
        else:
            normalized = _normalize_factory_criterion(criterion_cell)
            if normalized:
                verification_criteria.append(normalized)
        for command in commands:
            if command.casefold() in _FACTORY_EVIDENCE_PLACEHOLDERS:
                missing.append(
                    f"## Verification row {row.row} command must name the exact relevant proof"
                )
            elif command not in declared:
                missing.append(
                    f"## Verification row {row.row} command is not an exact declared proof: `{command}`"
                )
            elif _no_op_proof(command):
                missing.append(
                    f"## Verification row {row.row} command is a no-op proof: `{command}`"
                )
            else:
                verified_commands.add(command)
        if result != "pass":
            missing.append(
                f"## Verification row {row.row} result is not exactly `pass`: "
                f"`{result or 'empty'}`"
            )
    missing.extend(
        _missing_factory_acceptance_rows(acceptance_criteria, verification_criteria)
    )
    missing.extend(
        f"declared proof lacks Verification evidence: `{command}`"
        for command in declared
        if command not in verified_commands
    )
    return _build_result(producing_state, missing)


def _no_op_proof(command: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:true|:|exit\s+0|(?:echo|printf|write-output|write-host)\s+[\"']?(?:pass|ok|success)[\"']?)",
            command,
            re.IGNORECASE,
        )
    )


def factory_verification_text(body: str) -> str:
    return _section_body_text(body, "## Verification").strip()


def factory_browser_required(body: str, skills: tuple[str, ...]) -> bool:
    """Use routed browser QA or an explicit report section, not incidental prose."""
    return "superqa" in skills or bool(
        re.search(
            r"^##[ \t]+Browser Verification[ \t]*:?[ \t]*\r?$",
            body,
            re.IGNORECASE | re.MULTILINE,
        )
    )


def factory_browser_failures(
    body: str, workspace_path: Path, started_at: float
) -> list[str]:
    """Require a durable browser report and explicit regression counts."""
    heading = "## Browser Verification"
    header = _markdown_table_header(body, heading)
    required = ("report", "passed", "failed", "side effects")
    if not all(name in header for name in required):
        return [
            "## Browser Verification requires `report | passed | failed | side effects`"
        ]
    rows = _parse_markdown_table_rows(body, heading, escaped_pipes=True)
    if not rows:
        return ["## Browser Verification must contain a report row"]
    failures: list[str] = []
    for row in rows:
        if len(row.cells) <= max(header.index(name) for name in required):
            failures.append(f"Browser Verification row {row.row} is incomplete")
            continue
        cells = {
            name: row.cells[header.index(name)].strip().strip("`").strip()
            for name in required
        }
        path = (workspace_path / cells["report"]).resolve()
        try:
            path.relative_to(workspace_path.resolve())
            stat = path.stat()
            if not path.is_file() or not stat.st_size:
                raise ValueError("report must be a nonempty file")
            if started_at and stat.st_mtime < started_at:
                raise ValueError("report predates the independent Verify phase")
        except (OSError, ValueError) as exc:
            failures.append(
                f"Browser Verification report is unavailable or stale: {exc}"
            )
        try:
            counts = {name: int(cells[name]) for name in required[1:]}
            if (
                counts["passed"] <= 0
                or counts["failed"] != 0
                or counts["side effects"] < 0
            ):
                raise ValueError("requires passed > 0, failed = 0, side effects >= 0")
        except ValueError as exc:
            failures.append(f"Browser Verification counts are invalid: {exc}")
    return failures


def _factory_acceptance_criteria(body: str, heading: str) -> list[tuple[str, str]]:
    """Normalize the same declared items accepted by the Wayfinder parser."""
    return [(item, _normalize_factory_criterion(item)) for item in factory_acceptance_items(body, heading)]


def _normalize_factory_criterion(value: str) -> str:
    """Normalize presentational Markdown while preserving criterion wording."""
    text = unicodedata.normalize("NFKC", value)
    text = _MARKDOWN_INLINE_LINK_RE.sub(r"\1", text)
    text = _MARKDOWN_REFERENCE_LINK_RE.sub(r"\1", text)
    text = _INLINE_CODE_SPAN_RE.sub(r"\1", text)
    for pattern in (
        r"\*\*(.+?)\*\*",
        r"__(.+?)__",
        r"~~(.+?)~~",
        r"(?<!\w)\*([^*\n]+)\*(?!\w)",
        r"(?<!\w)_([^_\n]+)_(?!\w)",
    ):
        text = re.sub(pattern, r"\1", text)
    text = re.sub(r"\\([\\`*_{}\[\]()#+.!~-])", r"\1", text)
    return " ".join(text.casefold().split())


def _missing_factory_acceptance_rows(
    acceptance_criteria: list[tuple[str, str]], verification_criteria: list[str]
) -> list[str]:
    """Require one distinct Verification row per declared criterion."""
    available: dict[str, int] = {}
    for criterion in verification_criteria:
        available[criterion] = available.get(criterion, 0) + 1

    missing: list[str] = []
    for display, normalized in acceptance_criteria:
        if available.get(normalized, 0) > 0:
            available[normalized] -= 1
            continue
        missing.append(
            f"acceptance criterion lacks a distinct Verification row: `{display}`"
        )
    for normalized, count in available.items():
        for _ in range(count):
            missing.append(f"Verification has undeclared criterion: `{normalized}`")
    return missing
