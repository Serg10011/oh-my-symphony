"""Validate a lead's read-only plan and stage its API subtask cards."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import re

import api_project
import api_session as session
import check
import plan
import project_config as pc
from symphony.errors import SymphonyError
from symphony.trackers.file import parse_ticket_file
from symphony.trackers.validate import validate_ticket_dependencies

PLAN_LABEL = "role:api-plan"
DRAFT_LABEL = "role:api-subtask"


class PlanError(Exception):
    def __init__(self, reason: str, problems: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.problems = [problem[:240] for problem in (problems or [])[:8]]


def _config(board) -> dict:
    try:
        project = pc.load_project(board.workflow.parent)
    except pc.ProjectError:
        raise PlanError("api_plan_not_configured") from None
    if ("Plan" not in board.context["active_states"] or "Subtasks" not in board.context["terminal_states"]
            or (board.workflow.parent / pc.BOARD_DIR).resolve() != board.board_root
            or Path(project["project"]["repo"]).resolve() != board.repo
            or project["project"]["base_branch"] != board.context["base_branch"]
            or not project["levels"]["levels"]):
        raise PlanError("api_plan_not_configured")
    return project


def plan_context(board, front: dict, paths: list[str]) -> str:
    project = _config(board)
    ceiling, _ = plan.subtask_level_ceiling(project, front.get("level"))
    levels = list(project["levels"]["levels"])
    if ceiling is not None:
        levels = levels[:ceiling + 1]
    checks = _local_checks(board, project)
    return (
        "\n\nPlan format: version = 1, then one [[subtask]] block per child. Each block needs "
        "key (lowercase letters, digits, hyphens), title, level, level_reason, goal, criteria "
        "(one to five strings), check (one targeted command for the isolated task copy), and paths (specific write paths "
        "relative to the repository). Optional blocked_by lists keys from earlier blocks. "
        f"Maximum subtasks: {project['plan']['max_subtasks']}. Levels: {', '.join(levels)}. "
        f"Granted paths: {', '.join(paths)}. Check command prefixes: {', '.join(checks)}."
    )


def _parent(board, assignment: dict, front: dict, body: str) -> None:
    if (board.stop_path.exists() or PLAN_LABEL not in (front.get("labels") or [])
            or front.get("state") not in {"Plan", "Subtasks", "needs_human"}
            or board.card_digest(front, body) != assignment["card_digest"]):
        raise PlanError("api_plan_parent_changed")


def _local_checks(board, project) -> list[str]:
    return [prefix for prefix in plan.allowed_check_commands(board.workflow.parent, project) if not prefix.startswith("/")]


def _validate_plan(board, assignment: dict, text: str, front: dict) -> list[plan.Subtask]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > plan.PLAN_MAX_BYTES:
        raise PlanError("api_plan_invalid")
    fenced = re.fullmatch(r"```(?:toml)?\s*\n(.*?)\n```", text.strip(), flags=re.DOTALL)
    text = fenced[1] if fenced else text
    project = _config(board)
    try:
        cfg = check.load_config(board.workflow.parent)
    except check.CheckError:
        raise PlanError("api_plan_not_configured") from None
    subtasks, problems = plan.check_plan(text, project, cfg, front.get("level"), board.workflow.parent)
    if problems:
        raise PlanError("api_plan_invalid", problems)
    grants = assignment["spec"]["project"]["paths"]
    prefixes = _local_checks(board, project)
    for part in subtasks:
        if problem := plan._command_problem(part.check, prefixes):
            raise PlanError("api_plan_invalid", [f"{part.key}: {problem}"])
        for path in part.paths:
            if any(char in path for char in "*?[") or not api_project._in_scope(path, grants) or api_project._closed(path):
                raise PlanError("api_plan_invalid", [f"{part.key}: путь {path} вне области работы или задан шаблоном"])
    return subtasks


def _verify_draft(board, path, card: dict, part: plan.Subtask, description: str) -> str:
    sid = card["identifier"]
    blockers = card["blocked_by"]
    kind = board.context["agent_kind"]
    front, body = parse_ticket_file(path)
    actual = {key: value for key, value in front.items()
              if key not in {"created_at", "updated_at"} and value is not None}
    expected = {"id": sid, "identifier": sid, "title": part.title, "state": "Subtasks",
                "request": board.identifier, "level": part.level, "agent": {"kind": kind},
                "labels": [DRAFT_LABEL]}
    if blockers:
        expected["blocked_by"] = blockers
    if actual != expected or body.strip() != description.strip():
        raise PlanError("api_plan_subtask_changed")
    return board.card_digest(front, body)


def _stage_card(board, card: dict, part: plan.Subtask, number: int, total: int, fingerprint: str) -> None:
    sid = card["identifier"]
    blockers = card["blocked_by"]
    description = plan.render_body(board.identifier, number, total, part)
    _, path = board.tracker.create_validated(
        identifier=sid, title=part.title, description=description, state="Subtasks", labels=[DRAFT_LABEL],
        request=board.identifier, level=part.level, agent_kind=board.context["agent_kind"], blocked_by=blockers,
        creation_key=plan._digest([fingerprint, sid]),
        validate=lambda issues, identifier: validate_ticket_dependencies(
            issues, identifier=identifier, blocked_by=blockers, new_ticket=True),
    )
    _verify_draft(board, path, card, part, description)


def _plan_cards(board, subtasks):
    ids = {part.key: f"{board.identifier}-{n}" for n, part in enumerate(subtasks, 1)}
    if any(not plan.SUBTASK_ID_RE.fullmatch(sid) for sid in ids.values()):
        raise PlanError("api_plan_invalid", ["из номера задачи нельзя получить номера подзадач"])
    return [{"identifier": ids[part.key], "title": part.title,
             "blocked_by": [ids[key] for key in part.blocked_by]} for part in subtasks]


def frozen_parts(board, assignment: dict, *, previous_parts=None) -> list[dict]:
    front, body = board.card()
    _parent(board, assignment, front, body)
    batch = assignment.get("plan_batch", {})
    waiting = {"Subtasks", "needs_human"} if previous_parts is not None else {"Subtasks"}
    if front["state"] not in waiting or batch.get("status") != "ready":
        raise PlanError("api_plan_not_ready")
    outcome = session.show(board.session_path)
    if outcome["status"] != "dialogue_complete":
        raise PlanError("api_plan_not_ready")
    subtasks = _validate_plan(board, assignment, outcome["result"], front)
    cards = _plan_cards(board, subtasks)
    if (cards != batch["cards"] or batch["fingerprint"] != plan._digest(
            [assignment["binding"], [asdict(part) for part in subtasks]])):
        raise PlanError("api_plan_parent_changed")
    parts = []
    for number, (card, part) in enumerate(zip(cards, subtasks, strict=True), 1):
        path = board.tracker.find_path(card["identifier"])
        if path is None:
            raise PlanError("api_plan_subtask_changed")
        description = plan.render_body(board.identifier, number, len(subtasks), part)
        digest = (_verify_draft(board, path, card, part, description) if previous_parts is None
                  else board.card_digest(*parse_ticket_file(path)))
        parts.append({**card, "card_digest": digest, "paths": list(part.paths), "check": part.check,
                      "task": part.title + "\n\n" + description})
    if previous_parts is not None and parts != previous_parts:
        raise PlanError("api_plan_subtask_changed")
    return parts


def materialize(board, assignment: dict, text: str) -> dict:
    """Called under the assignment lock. Replay only the same planned drafts."""
    front, body = board.card()
    _parent(board, assignment, front, body)
    subtasks = _validate_plan(board, assignment, text, front)
    cards = _plan_cards(board, subtasks)
    fingerprint = plan._digest([assignment["binding"], [asdict(part) for part in subtasks]])
    receipt = {"status": "staging", "fingerprint": fingerprint, "cards": cards}
    previous = assignment.get("plan_batch")
    if previous is None:
        if {card["identifier"] for card in cards}.intersection(board.tracker.list_all_identifiers()):
            raise PlanError("api_plan_subtask_changed")
        assignment["plan_batch"] = receipt
        board._save(assignment)
    elif previous != receipt:
        raise PlanError("api_plan_parent_changed")

    try:
        for number, (card, part) in enumerate(zip(cards, subtasks, strict=True), 1):
            _parent(board, assignment, *board.card())
            _stage_card(board, card, part, number, len(subtasks), fingerprint)
    except (OSError, SymphonyError):
        raise PlanError("api_plan_staging_failed") from None

    def move(front, body):
        _parent(board, assignment, front, body)
        if front["state"] == "Subtasks":
            return None
        front.update(state="Subtasks", updated_at=session._now())
        return front, body

    board.tracker._mutate_ticket(board.identifier, move)
    return {**receipt, "status": "ready"}
