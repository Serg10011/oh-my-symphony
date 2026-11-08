"""Project an accepted, merged and installed API result onto its board card."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import time

import api_candidate as candidate
import api_cloud_check as cloud
import api_publication as publication
import api_session as session
import post_merge


SOURCE_ROOT = Path(__file__).resolve().parents[2]
POLL_SECONDS = 240
MERGED_NOTE = "\n\n## Delivery\n\n- PR влит"

MESSAGES = {
    "waiting_merge": "Версия принята. Ожидает слияния PR.",
    "waiting_installation": "PR влит. Ожидает применения выпуска.",
    "done": "Выпуск применён. Карточка завершена.",
    "held": "Карточка выведена из очереди приёмки.",
    "completion_pr_closed": "PR закрыт без слияния. Проверь решение по карточке.",
    "completion_pr_changed": "Данные PR расходятся с принятой версией. Нужна сверка.",
    "completion_check_unavailable": "GitHub пока не подтвердил состояние PR. Служба проверит снова.",
    "completion_card_changed": "Карточка изменилась после приёмки. Нужна сверка.",
    "completion_installation_changed": "Подтверждение установки расходится с версией выпуска. Нужна сверка.",
}


class CompletionError(session.SessionError):
    pass


def _state(value, record):
    state = value.get("completion")
    if state is None:
        return {"offer": record["api_acceptance"]}
    if (not isinstance(state, dict) or state.get("offer") != record["api_acceptance"]
            or any(not isinstance(state[k], str) or not cloud.OID.fullmatch(state[k])
                   for k in ("merge_commit", "applied_commit") if k in state)
            or "applied_commit" in state and "merge_commit" not in state
            or "reason" in state and state["reason"] not in MESSAGES
            or type(state.get("next_poll", 0)) not in (int, float)):
        raise CompletionError("release_receipt_changed")
    return state.copy()


def _done_lane(board):
    return next((s for s in board.context["terminal_states"] if s.casefold() == "done"), None)


def view(board, value, record):
    state = _state(value, record)
    lane = board.card()[0].get("state")
    if lane == _done_lane(board) and "applied_commit" in state:
        status = "done"
    elif lane != board.review_lane:
        status = "held"
    elif state.get("reason"):
        status = "blocked"
    else:
        status = "waiting_installation" if "merge_commit" in state else "waiting_merge"
    reason = state.get("reason") if status == "blocked" else None
    return {"status": status, "message": MESSAGES[reason or status],
            **({"reason": reason} if reason else {}),
            **{k: state[k] for k in ("merge_commit", "applied_commit") if k in state}}


def _card_matches(board, value, record, front, body):
    kind = front.get("agent_kind") or (front.get("agent") or {}).get("kind")
    if kind != board.context["agent_kind"] or board.stop_path.exists():
        return False
    expected = value.get("result_card_digest")
    if board.card_digest(front, body) == expected:
        return True
    # The original deliver merged command appends this one line to older
    # accepted cards. Check the entire original body and front matter anyway.
    return (bool(record.get("merged_at")) and body.endswith(MERGED_NOTE)
            and board.card_digest(front, body.removesuffix(MERGED_NOTE)) == expected)


def _merged_commit(board, record):
    pr = record["pr"]
    actual = publication.Publisher(board).github(f"repos/{cloud.REPO}/pulls/{pr['number']}")
    try:
        head, base = actual["head"], actual["base"]
        if (type(actual["number"]) is not int or actual["number"] != pr["number"]
                or actual["html_url"] != pr["url"]
                or head["ref"] != record["branch"] or head["sha"] != record["commit"]
                or base["ref"] != record["base"]
                or any(ref["repo"]["id"] != cloud.REPO_ID or ref["repo"]["full_name"] != cloud.REPO
                       for ref in (head, base))):
            raise CompletionError("completion_pr_changed")
        if actual["state"] == "open" and actual["merged"] is False:
            return None
        if actual["state"] == "closed" and actual["merged"] is False:
            raise CompletionError("completion_pr_closed")
        sha = actual["merge_commit_sha"]
        if (actual["state"] != "closed" or actual["merged"] is not True or not actual.get("merged_at")
                or not isinstance(sha, str) or not cloud.OID.fullmatch(sha)):
            raise CompletionError("completion_pr_changed")
        return sha
    except (KeyError, TypeError, AttributeError):
        raise CompletionError("completion_pr_changed") from None


def _applied_commit(board, record, merge):
    path = SOURCE_ROOT / post_merge.STATE_REL
    try:
        if path.parent.resolve() != path.parent:
            raise CompletionError("completion_installation_changed")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
                    or info.st_size > 1024 * 1024):
                raise CompletionError("completion_installation_changed")
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise CompletionError("completion_installation_changed")
        applied = session._decode(raw)["applied"]
        installed = applied.get(str(board.repo))
        if installed is None or applied.get(str(SOURCE_ROOT)) != installed:
            return None
        if not isinstance(installed, str) or not cloud.OID.fullmatch(installed):
            raise CompletionError("completion_installation_changed")
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, session.SessionError):
        raise CompletionError("completion_installation_changed") from None
    if installed != board._base_commit() or installed != candidate._git(
            SOURCE_ROOT, "rev-parse", "--verify", "HEAD^{commit}").decode().strip():
        return None
    try:
        candidate._git(board.repo, "merge-base", "--is-ancestor", merge, installed)
    except candidate.CandidateError:
        return None
    try:
        candidate._git(board.repo, "merge-base", "--is-ancestor", record["commit"], merge)
        tree = candidate._git(board.repo, "rev-parse", "--verify", record["commit"] + "^{tree}").decode().strip()
    except candidate.CandidateError:
        raise CompletionError("completion_installation_changed") from None
    if tree != record["api_proof"]["tree"]:
        raise CompletionError("completion_installation_changed")
    return installed


def reconcile(board, value, record):
    """Called only by the service scan under the assignment lock."""
    state = _state(value, record)
    done = _done_lane(board)
    front, body = board.card()

    def save():
        if state != value.get("completion"):
            value["completion"] = state.copy()
            board._save(value)

    def record_lane():
        if value["board_stages"][-1] != done:
            value["board_stages"].append(done)
            board._save(value)

    if done and front.get("state") == done and state.get("applied_commit"):
        record_lane()  # Replay after a crash between the card and history writes.
        return
    if not done or front.get("state") != board.review_lane:
        return
    if not _card_matches(board, value, record, front, body):
        state["reason"] = "completion_card_changed"
        save()
        return
    now = time.time()
    if "merge_commit" not in state:
        if state.get("next_poll", 0) > now:
            return
        state["next_poll"] = now + POLL_SECONDS
        save()  # Keep the read budget across restarts, including a lost response.
        try:
            merge = _merged_commit(board, record)
        except publication.ReleaseError:
            state["reason"] = "completion_check_unavailable"
            save()
            return
        except CompletionError as exc:
            state["reason"] = str(exc)
            save()
            return
        state.pop("reason", None)
        if merge is None:
            save()
            return
        state["merge_commit"] = merge
        save()
    try:
        installed = _applied_commit(board, record, state["merge_commit"])
    except (CompletionError, candidate.CandidateError) as exc:
        state["reason"] = str(exc) if isinstance(exc, CompletionError) else "completion_installation_changed"
        save()
        return
    state.pop("reason", None)
    if installed is None:
        save()
        return

    def transition(current, current_body):
        if current.get("state") != board.review_lane:
            return None
        if not _card_matches(board, value, record, current, current_body):
            state["reason"] = "completion_card_changed"
            save()
            return None
        if _applied_commit(board, record, state["merge_commit"]) != installed:
            return None
        state["applied_commit"] = installed
        save()  # Evidence precedes the projection onto the card.
        return {**current, "state": done, "updated_at": session._now()}, current_body

    board.tracker._mutate_ticket(board.identifier, transition)
    if board.card()[0].get("state") == done and state.get("applied_commit"):
        record_lane()
