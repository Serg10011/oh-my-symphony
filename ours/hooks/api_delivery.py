"""Human release of a sealed API result. Polling can rebuild a local check target;
only an explicit action for the displayed version can publish or open a PR.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
import re

import api_candidate as candidate
import api_cloud_check as cloud
import api_completion as completion
import api_project as project
import api_publication as publication
import api_session as session
import check
import deliver
import project_config as pc
from api_publication import BASE, ReleaseError


def pairing(board, value):
    if (not value or value.get("status") != "result" or value.get("cloud_profile") != cloud.PROFILE
            or not board.review_lane or not value.get("check_lane")
            or value.get("candidate", {}).get("status") != "awaiting_full_checks"):
        return False
    try:
        config = pc.load_project(board.workflow.parent)
    except pc.ProjectError:
        raise ReleaseError("release_destination_invalid") from None
    configured = config["delivery"]
    if not configured:
        return False
    if (configured != {"repo": cloud.REPO, "base": BASE} or board.context["base_branch"] != BASE
            or config["project"]["base_branch"] != BASE or config["project"]["repo"] != str(board.repo)
            or not board.identifier.startswith(config["project"]["id_prefix"] + "-")):
        raise ReleaseError("release_destination_invalid")
    try:
        deliver.deliver_branch(board.identifier, BASE)
    except deliver.Failed:
        raise ReleaseError("release_destination_invalid") from None
    return True


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def file_delta(board, base, target):
    """Check the actual two Git trees, including existing tests on the NEW base."""
    bare = board.root / "candidate.git"
    before, after = candidate._tree(bare, base), candidate._tree(bare, target)
    paths = sorted(k.decode() for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    saved = session._decode(session._read_state(board.root / "candidate.json"))
    allowed = {f["path"] for f in saved["files"]}
    cfg = check.load_config(board.workflow.parent)
    if any(candidate.KEY_LIKE.search(p.encode()) or check.has_api_key(p) for p in allowed | set(paths)):
        raise ReleaseError("release_possible_secret")
    if not paths:
        raise ReleaseError("release_already_in_base")
    if (not set(paths) <= allowed or check.forbidden_violations(set(paths), cfg)
            or any(p.startswith(".github/") for p in paths)):
        raise ReleaseError("release_paths_changed")
    is_test, can_edit = check._matcher(cfg.test_globs), check._matcher(cfg.allow_modified_tests)
    result = []
    for p in paths:
        old, new = before.get(p.encode()), after.get(p.encode())
        if old and (is_test(p) and not can_edit(p) or old.split()[0] not in (b"100644", b"100755")):
            raise ReleaseError("release_paths_changed")
        if old and int(candidate._git(bare, "cat-file", "-s", old.split()[2].decode())) > project.MAX_FILE_BYTES:
            raise ReleaseError("release_paths_changed")
        if new:
            mode, kind, blob = new.split()
            if mode not in (b"100644", b"100755") or kind != b"blob":
                raise ReleaseError("release_paths_changed")
            size = int(candidate._git(bare, "cat-file", "-s", blob.decode()))
            if size > project.MAX_FILE_BYTES:
                raise ReleaseError("release_paths_changed")
            data = candidate._git(bare, "cat-file", "blob", blob.decode())
            if candidate.KEY_LIKE.search(data) or check.has_api_key(data.decode("utf-8", errors="replace")):
                raise ReleaseError("release_possible_secret")
        result.append((p, "added" if old is None else "deleted" if new is None else "modified"))
    return result


def target_commit(board, value, base):
    bare = board.root / "candidate.git"
    source = value["candidate"]
    if base == source["base_commit"]:
        return None
    try:
        candidate._git(bare, "merge-base", "--is-ancestor", source["base_commit"], base)
    except candidate.CandidateError:
        raise ReleaseError("release_base_diverged") from None
    try:
        # Same two-parent merge-tree rule as S4-28, inside the private bare
        # repo with all external Git configuration, hooks and network disabled.
        tree = publication.oid(bare, "merge-tree", "--write-tree", "--no-messages", source["commit"], base)
    except candidate.CandidateError:
        raise ReleaseError("release_merge_conflict") from None
    sha = publication.commit(bare, tree, [source["commit"], base], f"{board.identifier}: check updated base")
    file_delta(board, base, sha)
    candidate._git(bare, "update-ref", "refs/api-release/" + sha, sha)
    return {"commit": sha, "tree": tree, "base_commit": base, "source_commit": source["commit"]}


def validate_target(board, target, source):
    bare = board.root / "candidate.git"
    if (not isinstance(target, dict) or set(target) != {"commit", "tree", "base_commit", "source_commit"}
            or any(not isinstance(v, str) or not cloud.OID.fullmatch(v) for v in target.values())
            or target["source_commit"] != source):
        raise ReleaseError("release_target_changed")
    if (session._decode(session._read_state(board.root / ("check-target-" + target["commit"] + ".json"))) != target
            or publication.oid(bare, "rev-parse", "--verify", "refs/api-release/" + target["commit"]) != target["commit"]):
        raise ReleaseError("release_target_changed")
    raw = candidate._git(bare, "cat-file", "commit", target["commit"]).split(b"\n\n", 1)[0].splitlines()
    trees = [s[5:].decode() for s in raw if s.startswith(b"tree ")]
    parents = [s[7:].decode() for s in raw if s.startswith(b"parent ")]
    if trees != [target["tree"]] or parents != [source, target["base_commit"]]:
        raise ReleaseError("release_target_changed")


def finish_lane(board, value):
    """Replay the file-board projection if the process died between the two writes."""
    if lane := value.get("release_lane_pending"):
        board._release_lane(value, lane)
        del value["release_lane_pending"]
        board._save(value)


def save_lane(board, value, lane):
    value["release_lane_pending"] = lane
    board._save(value)
    finish_lane(board, value)


def recover_delivery(board, value):
    if value and value.get("release_accept") and not value.get("release_done"):
        path = deliver.delivered_path(board.workflow.parent, board.identifier)
        if path.exists() or path.is_symlink():
            record = session._decode(session._read_state(path))
            if record.get("pr"):
                # The verified PR receipt precedes the final assignment write.
                # Recover a crash at that boundary, also after the base moves.
                value = {**value, "release_done": {"commit": record["commit"], "pr": record["pr"]}}
                accepted_delivery(board, value)
    return value


def refresh(board, value):
    """Reconcile against the local project tip without publishing anything."""
    value = recover_delivery(board, value)
    if value and value.get("release_done"):
        return value
    try:
        configured = pairing(board, value)
    except ReleaseError:
        # A pilot can check an already public commit even when its delivery
        # base differs. Only publication and acceptance require this pairing.
        return value
    if not configured:
        return value
    base = board._base_commit()
    if ((value.get("check_target") or value["candidate"])["base_commit"] == base
            and not value.get("release_block") and not value.get("release_lane_pending")):
        return value
    with session._lock(board.path):
        value = board._load()
        board._cloud_subject(value, allow_moved=True)
        finish_lane(board, value)
        base = board._base_commit()
        prior = value.get("check_target") or value["candidate"]
        if value.get("release_done") or value.get("release_block", {}).get("base") == base:
            return value
        if prior["base_commit"] == base and not value.get("release_block"):
            return value
        if prior["base_commit"] == base and value.get("release_accept"):
            value.pop("release_block", None)
            save_lane(board, value, board.review_lane)
            return value
        try:
            if value.get("release_accept"):
                raise ReleaseError("release_base_after_acceptance")
            target = target_commit(board, value, base)
        except ReleaseError as exc:
            value["release_block"] = {"base": base, "reason": str(exc)}
            save_lane(board, value, "needs_human")
            return value
        for key in ("cloud_seen", "cloud_attempt", "release_check", "release_block", "check_target"):
            value.pop(key, None)
        if target:
            value["check_target"] = target
            session._save(board.root / ("check-target-" + target["commit"] + ".json"), target)
        save_lane(board, value, value["check_lane"])
    return value


def active_receipt(board, value, subject):
    receipt = board._cloud_receipt(value, subject)
    seen = value.get("cloud_seen")
    if receipt and seen and (seen["phase"] != "verified" or
            (seen["run"], seen["attempt"]) != (receipt["run"], receipt["attempt"])):
        return None
    return receipt


def fresh_receipt(value, subject, receipt):
    if cloud.verify_subject(receipt["run"], subject) != receipt:
        raise ReleaseError("release_receipt_changed")
    oldest = datetime.fromisoformat(value["created_at"]).timestamp() - 3600
    hint = cloud.discover({subject["commit"]}, oldest, {}).get(subject["commit"])
    if (not hint or hint["phase"] != "finished"
            or (hint["run"], hint["attempt"]) != (receipt["run"], receipt["attempt"])):
        raise ReleaseError("release_receipt_changed")


def accepted_delivery(board, value):
    done = value["release_done"]
    accept = value["release_accept"]
    sha, pr = done["commit"], done["pr"]
    if (not isinstance(sha, str) or not cloud.OID.fullmatch(sha)
            or not re.fullmatch("[0-9a-f]{64}", str(accept["offer"]))):
        raise ReleaseError("release_receipt_changed")
    approved = session._decode(session._read_state(board.root / ("release_accept-" + accept["offer"] + ".json")))
    rec = session._decode(session._read_state(deliver.delivered_path(board.workflow.parent, board.identifier)))
    subject = approved["subject"]
    proof = approved["receipt"]
    if any(type(proof.get(key)) is not int or proof[key] < 1 for key in ("run", "attempt")):
        raise ReleaseError("release_receipt_changed")
    archived = session._decode(session._read_state(board.root / f"cloud-{proof['run']}-{proof['attempt']}.json"))
    front, _ = board.card()
    if (str(front.get("created_at")) != approved["card_created_at"]
            or rec["id"] != board.identifier or rec["commit"] != sha or rec["source_commit"] != value["candidate"]["commit"]
            or rec["source_tree"] != value["candidate"]["tree"]
            or rec["repo"] != cloud.REPO or rec["base"] != BASE or rec["our_main"] != subject["base_commit"]
            or rec["branch"] != f"deliver/{board.identifier}" or approved["branch"] != rec["branch"]
            or rec["api_binding"] != value["binding"] or rec["api_acceptance"] != approved["offer"]
            or approved["offer"] != accept["offer"] or approved["sha"] != sha or pr != rec["pr"]
            or subject["binding"] != value["binding"] or subject["identifier"] != board.identifier
            or subject["commit"] != sha or proof != rec["api_proof"]
            or archived != {"version": 1, "subject": subject, "evidence": proof}
            or proof["status"] != "passed" or proof["commit"] != sha
            or proof["tree"] != subject["tree"] or proof["base_commit"] != subject["base_commit"]
            or any(not isinstance(subject[k], str) or not cloud.OID.fullmatch(subject[k]) for k in ("tree", "base_commit"))
            or type(pr["number"]) is not int or pr["number"] < 1
            or pr["url"] != f"https://github.com/{cloud.REPO}/pull/{pr['number']}"):
        raise ReleaseError("release_receipt_changed")
    return rec, subject


def completed(board, value):
    record, _ = accepted_delivery(board, value)
    return {"stage": "delivered", "commit": record["commit"], "pr": record["pr"], "accepted": True,
            "proof": record["api_proof"], "completion": completion.view(board, value, record)}


def reconcile_completion(board):
    with session._lock(board.path):
        value = recover_delivery(board, board._load())
        if not value or not value.get("release_done"):
            return False
        if pairing(board, value) and value["spec"]["project"]["root"] == str(board.repo):
            record, _ = accepted_delivery(board, value)
            completion.reconcile(board, value, record)
        return True


def offer(board, value):
    if value and value.get("release_done"):
        return completed(board, value)
    if not pairing(board, value):
        return None
    if value.get("release_block"):
        return {"stage": "blocked", "reason": value["release_block"]["reason"],
                **({"accepted": True} if value.get("release_accept") else {})}
    subject = board._cloud_subject(value)
    receipt = active_receipt(board, value, subject)
    current = board.card()[0].get("state")
    if receipt and receipt["status"] == "passed" and current == board.review_lane:
        operation = "accept"
    elif current == value["check_lane"] and not receipt:
        if value.get("release_check", {}).get("published") or value.get("cloud_seen"):
            return {"stage": "checking", "commit": subject["commit"]}
        operation = "publish-check"
    else:
        return {"stage": "waiting", "commit": subject["commit"]}
    branch = cloud.CHECK_PREFIX + subject["commit"] if operation == "publish-check" else deliver.deliver_branch(board.identifier, BASE)
    manifest = file_delta(board, subject["base_commit"], subject["commit"])
    original = None
    if "source_commit" in subject:
        # Publishing a two-parent merge also publishes its source parent.
        # Show and bind both deltas, even when the old base was superseded
        # before the original candidate had been made public.
        saved = session._decode(session._read_state(board.root / "candidate.json"))
        files = [f["path"] for f in saved["files"]]
        original = {"commit": saved["commit"], "base_commit": saved["base_commit"], "tree": saved["tree"],
                    "file_count": len(files), "files": files[:100]}
    binding = {**subject, "operation": operation, "repo": cloud.REPO, "base": BASE, "branch": branch,
               "files": manifest, "original": original, "receipt": receipt, "recipe": cloud.CHECK_RECIPE}
    key = fingerprint(binding)
    return {"stage": operation, "offer": key, "repo": cloud.REPO, "base": BASE, "branch": branch,
            "commit": subject["commit"], "tree": subject["tree"], "base_commit": subject["base_commit"],
            "file_count": len(manifest), "files": [p for p, _ in manifest[:100]],
            "rebuilt": original is not None, "original": original, "accepted": value.get("release_accept", {}).get("offer") == key}


def review(board, value, offset):
    if type(offset) is not int or offset < 0:
        raise ReleaseError("invalid_workspace_query")
    value = recover_delivery(board, value)
    if value.get("release_done"):
        _, subject = accepted_delivery(board, value)
        if publication.oid(board.root / "candidate.git", "rev-parse", "--verify",
                           subject["commit"] + "^{tree}") != subject["tree"]:
            raise ReleaseError("release_target_changed")
    else:
        subject = board._cloud_subject(value)
    base, sha = subject["base_commit"], subject["commit"]
    manifest = file_delta(board, base, sha)
    if offset > len(manifest):
        raise ReleaseError("invalid_workspace_query")
    rows = []
    for path, change in manifest[offset:offset + 4]:
        bare = board.root / "candidate.git"
        args = ("--no-ext-diff", "--no-textconv", "--no-renames", base, sha, "--", path)
        binary = candidate._git(bare, "diff", "--numstat", *args).startswith(b"-\t-\t")
        raw = b"" if binary else candidate._git(bare, "diff", *args)
        text = raw[:256 * 1024].decode("utf-8", errors="replace")
        if len(raw) > 256 * 1024:
            text += "\n… Полное изменение доступно по проверенному коммиту."
        rows.append({"path": path, "change": change, "diff": text, "binary": binary})
    end = offset + len(rows)
    return {"source": "delivery_target", "commit": sha, "base_commit": base, "files": rows,
            "total": len(manifest), "next_offset": end if end < len(manifest) else None}


def act(board, operation, expected, *, publisher=None):
    if (operation not in ("accept", "publish-check") or not isinstance(expected, str)
            or not re.fullmatch("[0-9a-f]{64}", expected)):
        raise ReleaseError("release_offer_changed")
    # The cloud lookup takes these locks in the same order. The ticket lock
    # also fences edits from the UI during approval and the external writes.
    with session._lock(board.root / "cloud-lookup"), session._lock(board.path):
        def apply(front, body):
            value = recover_delivery(board, board._load())
            if not value:
                raise ReleaseError("release_offer_changed")
            current = offer(board, value)
            if operation == "publish-check" and (prior := value.get("release_check")) and prior["published"]:
                if hmac.compare_digest(prior["offer"], expected):
                    return None
            if current and current["stage"] == "delivered" and operation == "accept":
                if hmac.compare_digest(value["release_accept"]["offer"], expected):
                    return None
            if (not current or current["stage"] != operation
                    or not hmac.compare_digest(current["offer"], expected)):
                raise ReleaseError("release_offer_changed")
            remote = publisher or publication.Publisher(board)
            remote.check_destination()
            subject = board._cloud_subject(value)
            def guard():
                if board._base_commit() != current["base_commit"] or remote.tip(BASE) != current["base_commit"]:
                    raise ReleaseError("release_base_not_synced")
                if board._cloud_subject(value) != subject:
                    raise ReleaseError("release_offer_changed")
            guard()
            key = "release_check" if operation == "publish-check" else "release_accept"
            intent = value.get(key)
            branch = current["branch"]
            tip = remote.tip(branch)
            if not intent and tip is not None:
                raise ReleaseError("release_branch_occupied")
            sha = subject["commit"]
            receipt = active_receipt(board, value, subject)
            if operation == "accept":
                path = deliver.delivered_path(board.workflow.parent, board.identifier)
                if path.exists() or path.is_symlink():
                    stored = session._decode(session._read_state(path))
                    if not intent or stored.get("api_acceptance") != expected or stored.get("api_binding") != value["binding"]:
                        raise ReleaseError("release_branch_occupied")
                fresh_receipt(value, subject, receipt)
            else:
                sha = publication.marker(board.root / "candidate.git", sha)
            if intent and (intent["sha"] != sha or intent["branch"] != branch):
                raise ReleaseError("release_offer_changed")
            if not intent or intent["offer"] != expected:
                # A later successful attempt on the same commit needs a new
                # human approval. Keep the prior grant and any completed push.
                granted = board.root / (key + "-" + expected + ".json")
                published = bool(intent and intent["published"])
                intent = {"offer": expected, "sha": sha, "subject": subject, "branch": branch,
                          "receipt": receipt, "at": session._now(),
                          "card_created_at": str(front.get("created_at")), "published": published}
                session._save(granted, intent)
                value[key] = intent
                board._save(value)  # durable authorization precedes every possible remote side effect
            guard()
            if tip is None:
                if intent["published"]:
                    raise ReleaseError("release_branch_changed")
                remote.create_branch(branch, sha)
            elif tip != sha:
                raise ReleaseError("release_branch_changed")
            intent["published"] = True
            board._save(value)
            if operation == "publish-check":
                return None
            guard()
            if remote.tip(branch) != sha:
                raise ReleaseError("release_branch_changed")
            proof = intent["receipt"]
            source = value["candidate"]
            rec = {"id": board.identifier, "kind": "card", "source": "api-team/" + board.identifier,
                   "source_commit": source["commit"], "source_tree": source["tree"],
                   "commit": sha, "our_main": subject["base_commit"], "repo": cloud.REPO, "base": BASE,
                   "branch": branch, "at": intent["at"], "pr": None, "recheck": "source_commit" in subject,
                   "api_binding": value["binding"], "api_proof": proof, "api_acceptance": expected}
            path = deliver.delivered_path(board.workflow.parent, board.identifier)
            if path.exists() or path.is_symlink():
                stored = session._decode(session._read_state(path))
                if {k: v for k, v in stored.items() if k != "pr"} != {k: v for k, v in rec.items() if k != "pr"}:
                    raise ReleaseError("release_branch_occupied")
                rec = stored
            if not rec["pr"]:
                # Expose only the card ID and public verification evidence.
                remote.open_pr(board.workflow.parent, rec, f"{board.identifier}: результат API-команды",
                    f"Проверенная версия `{sha}` для {board.identifier}.\n\n"
                    f"Полная проверка дерева `{subject['tree']}` прошла в "
                    f"[GitHub Actions](https://github.com/{cloud.REPO}/actions/runs/{proof['run']}/attempts/{proof['attempt']}).\n")
            else:
                remote.write_record(board.workflow.parent, rec)
            value["release_done"] = {"commit": sha, "pr": rec["pr"]}
            board._save(value)
            return None
        board.tracker._mutate_ticket(board.identifier, apply)
