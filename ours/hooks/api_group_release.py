"""Bind reviewed card results to one approved, merged release."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import tempfile
from contextlib import ExitStack
from pathlib import Path

import api_candidate as candidate
import api_cloud_check as cloud
import api_publication as publication
import api_session as session
from api_publication import BASE, ReleaseError


def lanes(board, value):
    review = {"needs_human", board.review_lane} - {None}
    check = value.get("check_lane")
    if check in board.context["active_states"]:
        review.add(check)
    return review


def _ledger_path(board):
    return session._path(board.root.parent / "group-releases.json")


def _releases(path):
    if not path.exists() and not path.is_symlink():
        return {}
    saved = session._decode(session._read_state(path))
    if not isinstance(saved, dict) or saved.get("version") != 1 or not isinstance(saved.get("releases"), dict):
        raise ReleaseError("group_release_changed")
    return saved["releases"]


def _fingerprint(release):
    data = json.dumps(release, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _identity(value):
    source = value["candidate"]
    return {key: value[key] for key in ("binding", "result_card_digest")} | {
        "source_commit": source["commit"], "source_tree": source["tree"], "source_base": source["base_commit"],
    }


def receipt(board, value):
    if not value or value["status"] != "result":
        return None
    for offer, release in _releases(_ledger_path(board)).items():
        card = release["cards"].get(board.identifier)
        if card is None:
            continue
        if (offer != _fingerprint(release) or release["project"] != str(board.repo)
                or release["workflow"] != str(board.workflow) or release["agent"] != board.context["agent_kind"]
                or board.context["base_branch"] != BASE or value.get("cloud_profile") != cloud.PROFILE
                or value.get("release_accept") or card["identity"] != _identity(value)):
            raise ReleaseError("group_release_changed")
        return {**release["target"], **card, "kind": "group_release", "api_acceptance": offer}
    return None


def _commit_tree(repo, commit):
    return publication.oid(repo, "rev-parse", "--verify", commit + "^{tree}")


def _target(board, number, head, publisher):
    actual = publisher.github(f"repos/{cloud.REPO}/pulls/{number}")
    url = f"https://github.com/{cloud.REPO}/pull/{number}"
    try:
        base, source = actual["base"], actual["head"]
        merge = actual["merge_commit_sha"]
        if (type(actual["number"]) is not int or actual["number"] != number or actual["html_url"] != url
                or actual["state"] != "closed" or actual["merged"] is not True or not actual.get("merged_at")
                or base["ref"] != BASE or source["sha"] != head
                or any(ref["repo"]["id"] != cloud.REPO_ID or ref["repo"]["full_name"] != cloud.REPO
                       for ref in (base, source))
                or any(not isinstance(sha, str) or not cloud.OID.fullmatch(sha) for sha in (base["sha"], merge))):
            raise ReleaseError("group_release_pr_changed")
        parents = candidate._git(board.repo, "rev-list", "--parents", "-n", "1", merge).decode().split()
        tree = _commit_tree(board.repo, head)
        if parents != [merge, base["sha"], head] or _commit_tree(board.repo, merge) != tree:
            raise ReleaseError("group_release_pr_changed")
    except (KeyError, TypeError, AttributeError):
        raise ReleaseError("group_release_pr_changed") from None
    return {"repo": cloud.REPO, "base": BASE, "base_commit": base["sha"], "commit": head,
            "tree": tree, "merge_commit": merge, "pr": {"number": number, "url": url}}


def _objects(repo):
    common = candidate._git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    directory = str((Path(common.decode().removesuffix("\n")) / "objects").resolve(strict=True))
    if any(ch in directory for ch in ("\n", "\r", "\0")):
        raise ReleaseError("group_release_changed")
    return directory


def includes(board, tree, source):
    bare = board.root / "candidate.git"
    with tempfile.TemporaryDirectory(prefix="symphony-release-") as directory:
        scratch = Path(directory)
        algorithm = "sha1" if len(tree) == 40 else "sha256"
        candidate._git(scratch, "init", "--bare", "--quiet", "--template=", "--object-format=" + algorithm)
        (scratch / "objects/info/alternates").write_text(_objects(board.repo) + "\n" + _objects(bare) + "\n")
        try:
            # Compare trees as siblings of the original base. Git must see
            # the card's delta even if its commit was merged and then reverted.
            release_view = publication.commit(
                scratch, tree, [source["source_base"]], "Compare release with card",
            )
            merged = publication.oid(
                scratch, "-c", "merge.renames=false", "merge-tree", "--write-tree", "--no-messages",
                release_view, source["source_commit"],
            )
        except candidate.CandidateError:
            return False
        return merged == tree


def _result_identity(board, value, allowed_states=None):
    # The ordinary delivery also reads group receipts.
    from api_delivery import pairing

    if not pairing(board, value) or value.get("release_accept") or board.stop_path.exists():
        raise ReleaseError("group_release_card_changed")
    front, body = board.card()
    kind = front.get("agent_kind") or (front.get("agent") or {}).get("kind")
    if allowed_states is None:
        allowed_states = lanes(board, value)
    if (front.get("state") not in allowed_states or kind != board.context["agent_kind"]
            or board.card_digest(front, body) != value["result_card_digest"]
            or value["spec"]["project"]["root"] != str(board.repo)):
        raise ReleaseError("group_release_card_changed")
    if value.get("aggregate"):
        final = value.get("final_document", {})
        subject = value.get("check_target") or value["candidate"]
        if (front["state"] != board.review_lane or value.get("final_error")
                or any(final.get(key) != subject[key] for key in ("commit", "tree", "base_commit"))):
            raise ReleaseError("group_release_card_changed")
    board.verify_result(value)
    return _identity(value)


def _check_note(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 500 or not value.isprintable():
        raise ReleaseError("group_review_checks_required")
    return value.strip()


def review_receipt(board, value):
    if not value or "group_review" not in value:
        return None
    record = value["group_review"]
    if (not isinstance(record, dict) or set(record) != {"identity", "checks", "checked_at"}
            or _check_note(record["checks"]) != record["checks"] or not isinstance(record["checked_at"], str)
            or record["identity"] != _result_identity(board, value, {value.get("check_lane"), board.review_lane})):
        raise ReleaseError("group_review_changed")
    return record


def project_review(board, value):
    """Recover the lane projection from the saved target-check receipt."""
    record = review_receipt(board, value)
    if record is None:
        return False
    board._release_lane(value, board.review_lane, allowed_states={value["check_lane"], board.review_lane})
    return True


def record_review(board, expected, checks):
    """Record the operator's checks of one candidate before assembling the release."""
    from api_delivery import file_delta

    checks = _check_note(checks)
    if not isinstance(expected, str) or not cloud.OID.fullmatch(expected):
        raise ReleaseError("group_review_changed")
    with session._lock(board.root / "cloud-lookup"), session._lock(board.path):
        value = board._load()
        if not value or receipt(board, value):
            raise ReleaseError("group_release_card_changed")
        if any(key in value for key in ("check_target", "cloud_attempt", "cloud_seen", "release_check", "release_accept")):
            raise ReleaseError("group_review_individual_check_started")
        record = review_receipt(board, value)
        if record is None:
            identity = _result_identity(board, value, {value.get("check_lane")})
            if identity["source_commit"] != expected:
                raise ReleaseError("group_review_changed")
            file_delta(board, identity["source_base"], identity["source_commit"])
            record = {"identity": identity, "checks": checks, "checked_at": session._now()}
            value["group_review"] = record
            board._save(value)
        elif record["identity"]["source_commit"] != expected or record["checks"] != checks:
            raise ReleaseError("group_review_changed")
        board._release_lane(value, board.review_lane, allowed_states={value["check_lane"], board.review_lane})
        return {"status": "ready", "identifier": board.identifier, "commit": expected,
                "checks": record["checks"], "checked_at": record["checked_at"]}


def _member(board, target, release_tree):
    from api_delivery import file_delta

    identity = _result_identity(board, board._load())
    files = file_delta(board, identity["source_base"], identity["source_commit"])
    if not includes(board, target["tree"], identity):
        raise ReleaseError("group_release_missing_changes")
    return {"identity": identity, "files": {
        path: release_tree.get(path.encode(), b"").decode("ascii") for path, _ in files
    }}


def _selection(boards, number, head):
    if (not boards or type(number) is not int or number < 1
            or not isinstance(head, str) or not cloud.OID.fullmatch(head)):
        raise ReleaseError("group_release_bad_selection")
    first = boards[0]
    ids = {board.identifier for board in boards}
    if len(ids) != len(boards) or any(
            board.workflow != first.workflow or board.repo != first.repo or board.settings != first.settings
            or board.context["agent_kind"] != first.context["agent_kind"] for board in boards):
        raise ReleaseError("group_release_bad_selection")
    return first, ids


def preview(boards, number, head, *, publisher=None):
    first, _ = _selection(boards, number, head)
    target = _target(first, number, head, publisher or publication.Publisher(first))
    tree = candidate._tree(first.repo, target["tree"])
    release = {"target": target, "project": str(first.repo), "workflow": str(first.workflow),
               "agent": first.context["agent_kind"],
               "cards": {board.identifier: _member(board, target, tree) for board in boards}}
    return {"offer": _fingerprint(release), "release": release}


def record(boards, number, head, expected, *, publisher=None):
    first, ids = _selection(boards, number, head)
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ReleaseError("group_release_offer_changed")
    path = _ledger_path(first)
    with session._lock(path):
        releases = _releases(path)
        if approved := releases.get(expected):
            target = approved["target"]
            if (_fingerprint(approved) != expected or target["commit"] != head
                    or target["pr"]["number"] != number or set(approved["cards"]) != ids):
                raise ReleaseError("group_release_offer_changed")
            return {"offer": expected, "release": approved}
        if any(ids & item["cards"].keys() for item in releases.values()):
            raise ReleaseError("group_release_card_already_recorded")
        # Fence the ordinary per-card acceptance while writing the group
        # receipt. A concurrent card edit is checked again at projection.
        with ExitStack() as locks:
            for board in sorted(boards, key=lambda item: item.identifier):
                locks.enter_context(session._lock(board.path))
            shown = preview(boards, number, head, publisher=publisher)
            if not hmac.compare_digest(shown["offer"], expected):
                raise ReleaseError("group_release_offer_changed")
            releases[expected] = shown["release"]
            session._save(path, {"version": 1, "releases": releases})
            return shown
