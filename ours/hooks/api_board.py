"""Explicit, single-use API assignments for the existing file-backed board.

The browser supplies a confirmation token, review cursor or public run number. Project
permissions, provider profiles and execution settings come from trusted local
configuration. No action other than run reads credentials or calls a provider.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import time

import api_executor as api
import api_candidate as candidate
import api_cloud_check as cloud
import api_project as project
import api_session as session
from symphony.errors import SymphonyError
from symphony.trackers.file import FileBoardTracker, parse_ticket_file
from symphony.workflow import TrackerConfig

FORMAT = "api-team-v1"
MAX_MESSAGE = 1024 * 1024
PENDING_SECONDS = 600
KEYCHAIN_SERVICES = {"DEEPSEEK_API_KEY": "symphony.deepseek", "OPENAI_API_KEY": "symphony.openai"}
RESULT_NOTE = ("API-команда завершила запуск. Изменения, расходы и сведения о проверке доступны "
               "в панели карточки. Решение о приёмке остаётся за человеком.")


class BoardError(Exception):
    """Fixed, public error codes; never provider/Keychain exception bodies."""


class Interrupted(BaseException):
    pass


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str, allow_nan=False).encode()).hexdigest()


def _held_body(body, note):
    return body.rstrip() + "\n\n## API Team\n\n" + note + "\n"


def _directory(path: Path) -> Path:
    path.mkdir(mode=0o700, exist_ok=True)
    if (path.resolve() != path or not path.is_dir() or path.stat().st_uid != os.getuid()
            or stat.S_IMODE(path.stat().st_mode) != 0o700):
        raise BoardError("api_state_directory_not_private")
    return path


class Board:
    def __init__(self, settings: Path, context: dict):
        self.settings = settings.resolve(strict=True)
        required = {"workflow", "board_root", "repo", "identifier", "agent_kind", "command",
                    "active_states", "terminal_states", "base_branch", "process_timeout_seconds"}
        if not isinstance(context, dict) or set(context) != required:
            raise BoardError("invalid_board_context")
        self.context = context
        self.identifier = context["identifier"]
        if not isinstance(self.identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.identifier):
            raise BoardError("invalid_board_identifier")
        self.workflow = Path(context["workflow"]).resolve(strict=True)
        self.board_root = Path(context["board_root"]).resolve(strict=True)
        self.repo = Path(context["repo"]).resolve(strict=True)
        if "needs_human" not in context["terminal_states"] or not context["active_states"]:
            raise BoardError("api_board_states_missing")
        cfg = TrackerConfig("file", "", "", "", tuple(context["active_states"]),
                            tuple(context["terminal_states"]), board_root=self.board_root)
        self.tracker = FileBoardTracker(cfg)
        state = self.workflow.parent / ".ours-state"
        state.mkdir(mode=0o700, exist_ok=True)
        if state.is_symlink() or state.resolve() != state:
            raise BoardError("api_state_directory_invalid")
        self.root = _directory(_directory(state / "api-team") / self.identifier)
        self.path = self.root / "assignment.json"
        self.session_path = self.root / "session.json"
        self.stop_path = self.root / "stop-requested"

    def card(self):
        path = self.tracker.find_path(self.identifier)
        if path is None:
            raise BoardError("api_card_missing")
        return parse_ticket_file(path)

    def card_digest(self, front: dict, body: str) -> str:
        # State changes are controlled by the queue. All substantive fields,
        # including created_at, dependencies and identity, bind the approval.
        fields = {k: v for k, v in front.items() if k not in {"state", "updated_at", "last_agent_kind"}}
        return _digest({"front": fields, "body": body})

    def _base_commit(self):
        base = self.context["base_branch"] or "HEAD"
        return project._git(str(self.repo), ["rev-parse", "--verify", "--end-of-options",
                                            base + "^{commit}"], limit=128).decode().strip()

    def proposal(self) -> dict:
        raw = session._decode(self.settings.read_bytes())
        if (not isinstance(raw, dict) or set(raw) - {"cloud_check"} != {"version", "paths", "session", "execution"}
                or ("cloud_check" in raw and raw["cloud_check"] != cloud.PROFILE)
                or raw["version"] != 1 or not isinstance(raw["session"], dict)
                or set(raw["session"]) - {"context", "lead_profile", "worker_profile", "lead_effort", "limits", "budget"}):
            raise BoardError("invalid_api_board_settings")
        front, body = self.card()
        pinned = front.get("agent_kind") or (front.get("agent") or {}).get("kind")
        if pinned != self.context["agent_kind"]:
            raise BoardError("api_card_agent_changed")
        spec = session.validate_spec({**raw["session"], "task": str(front.get("title", "")) + "\n\n" + body,
                                      "project": {"root": str(self.repo), "paths": raw["paths"],
                                                  "commit": self._base_commit()},
                                      "execution": raw["execution"]})
        session._path(self.session_path)
        spec["project"], snapshot = project.snapshot(spec["project"])
        code = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path(__file__).parent.glob("api_*.py"))}
        rules = candidate.rules_snapshot(self.workflow.parent)
        binding = {"card": self.card_digest(front, body), "spec": spec,
                   "snapshot": snapshot["digest"], "profiles": session._profiles(spec),
                   "context": self.context, "settings": raw, "code": code,
                   "workflow_sha256": hashlib.sha256(self.workflow.read_bytes()).hexdigest(), "rules": rules}
        proposal = {"binding": _digest(binding), "card_digest": binding["card"], "spec": spec,
                    "profiles": binding["profiles"], "project_digest": snapshot["digest"], "rules": rules}
        if "cloud_check" in raw:
            proposal.update(cloud_profile=cloud.PROFILE,
                            result_card_digest=self.card_digest(front, _held_body(body, RESULT_NOTE).strip()))
        return proposal

    def _load(self) -> dict | None:
        if not self.path.exists():
            return None
        value = session._decode(session._read_state(self.path))
        if (not isinstance(value, dict) or value.get("version") != 1
                or value.get("identifier") != self.identifier
                or value.get("status") not in {"prepared", "queued", "running", "result", "stopped"}):
            raise BoardError("invalid_api_assignment")
        return value

    def _save(self, value):
        session._save(self.path, value)

    def prepare(self) -> dict:
        with session._lock(self.path):
            previous = self._load()
            if previous and previous["status"] != "prepared":
                raise BoardError("api_assignment_already_used")
            front, _ = self.card()
            if front.get("state") != "needs_human":
                raise BoardError("api_card_must_wait_for_confirmation")
            proposal = self.proposal()
            token = secrets.token_urlsafe(32)
            value = {"version": 1, "identifier": self.identifier, "status": "prepared",
                     "created_at": session._now(), "expires_at": time.time() + PENDING_SECONDS,
                     "token_sha256": _digest(token), **proposal}
            self._save(value)
        return {**self.show(), "confirmation_token": token}

    def confirm(self, token) -> dict:
        if not isinstance(token, str) or len(token) > 256:
            raise BoardError("invalid_api_confirmation")
        with session._lock(self.path):
            value = self._load()
            if value is None or not hmac.compare_digest(value["token_sha256"], _digest(token)):
                raise BoardError("invalid_api_confirmation")
            if value["status"] != "prepared":
                # Lost HTTP reply: the same confirmation is idempotent and does
                # not move a finished/interrupted run back into the queue.
                return self.show()
            if time.time() > value["expires_at"]:
                raise BoardError("api_confirmation_expired")
            if self.proposal()["binding"] != value["binding"]:
                raise BoardError("api_assignment_changed")

            def mutate(front, body):
                if (self.card_digest(front, body) != value["card_digest"]
                        or front.get("state") != "needs_human"):
                    raise BoardError("api_assignment_changed")
                # Consume before publishing an executable state. A lost write
                # can leave an idle assignment; it cannot start a second run.
                value.update(status="queued", confirmed_at=session._now())
                self._save(value)
                front["state"] = self.context["active_states"][0]
                front["updated_at"] = session._now()
                return front, body
            self.tracker._mutate_ticket(self.identifier, mutate)
        return self.show()

    def show(self) -> dict:
        value = self._load()
        if value is None:
            return {"version": 1, "status": "unassigned", "identifier": self.identifier}
        if value["status"] == "running":
            # The lifetime lock survives HTTP disconnects but is released by
            # the OS on process death. A crashed owner can never be retried.
            try:
                with session._lock(self.root / "owner"):
                    with session._lock(self.path):
                        value = self._load()
                        if value["status"] == "running":
                            if self.session_path.exists():
                                session.interrupt(self.session_path, "board_execution_failed")
                            value.update(status="stopped", reason="board_execution_failed",
                                         finished_at=session._now())
                            self._save(value)
                            self._hold(value, "Процесс API-команды прерван. Автоматического платного повтора нет.")
            except session.SessionError as exc:
                if str(exc) != "session_busy":
                    raise
        spec = value["spec"]
        result = {"version": 1, "identifier": self.identifier, "status": value["status"],
                  "profiles": value["profiles"], "task": spec["task"], "limits": spec["limits"],
                  "budget": spec.get("budget"), "project": spec["project"],
                  "execution": spec["execution"],
                  "process_timeout_seconds": self.context["process_timeout_seconds"],
                  "reason": value.get("reason"), "run_id": value.get("run_id"),
                  "stop_requested": self.stop_path.exists(), "accepted": False, "delivered": False,
                  "delivery_status": "not_implemented"}
        if self.session_path.exists():
            state = session._load(self.session_path)
            result["session"] = session.summary(state)
            result["report_text"] = session.render_report(session.spend_report(state))
        if value.get("candidate"):
            result["candidate"] = candidate.public_report(value["candidate"])
            result["delivery_status"] = value["candidate"]["status"]
            receipt = None
            if value.get("cloud_profile") and value["candidate"]["status"] == "awaiting_full_checks":
                try:
                    subject = self._cloud_subject(value)
                    receipt = self._cloud_receipt(value, subject)
                    result["cloud_check"] = {"status": "ready", "commit": subject["commit"], "receipt": receipt}
                    seen = value.get("cloud_seen")
                    if seen and (not receipt or (seen["run"], seen["attempt"]) != (receipt["run"], receipt["attempt"])):
                        receipt = None
                        result["cloud_check"]["receipt"] = None
                        result["cloud_check"]["phase"] = seen["phase"]
                except (BoardError, session.SessionError, ValueError, KeyError, TypeError):
                    result["cloud_check"] = {"status": "stale"}
            result["report_text"] = (result.get("report_text", "") + "\n\n"
                                     + candidate.render_report(value["candidate"], cloud_checked=bool(receipt)))
        return result

    def _cloud_subject(self, value):
        if (not value or value.get("cloud_profile") != cloud.PROFILE or value["status"] != "result"
                or value.get("candidate", {}).get("status") != "awaiting_full_checks"):
            raise BoardError("cloud_not_enabled")
        report = value["candidate"]
        try:
            front, body = self.card()
            current = session._decode(self.settings.read_bytes())
            origin = candidate._git(self.repo, "config", "--local", "--no-includes", "--get-all",
                                    "remote.origin.url").decode().removesuffix("\n")
            kind = front.get("agent_kind") or (front.get("agent") or {}).get("kind")
            if (kind != self.context["agent_kind"] or front.get("state") != "needs_human"
                    or self.card_digest(front, body) != value["result_card_digest"]
                    or current.get("cloud_check") != cloud.PROFILE or self.stop_path.exists()
                    or origin not in (f"https://github.com/{cloud.REPO}.git", f"git@github.com:{cloud.REPO}.git")
                    or str(self.repo) != value["spec"]["project"]["root"] or self._base_commit() != report["base_commit"]
                    or candidate.rules_snapshot(self.workflow.parent) != value["rules"]):
                raise BoardError("cloud_stale")
            candidate.check_identity(self.session_path, value, session._load(self.session_path))
        except (candidate.CandidateError, session.SessionError, session.workspace.WorkspaceError,
                project.ProjectError, SymphonyError, OSError, ValueError, KeyError, TypeError, AttributeError):
            raise BoardError("cloud_stale") from None
        return {"profile": cloud.PROFILE, "identifier": self.identifier, "binding": value["binding"],
                "run_id": value["run_id"], **{k: report[k] for k in ("commit", "tree", "revision", "base_commit")}}

    def _cloud_receipt(self, value, subject):
        ref = value.get("cloud_attempt")
        if ref is None:
            return None
        number, attempt = ref["run"], ref["attempt"]
        if any(type(n) is not int or n <= 0 for n in (number, attempt)):
            raise BoardError("cloud_conflict")
        record = session._decode(session._read_state(self.root / f"cloud-{number}-{attempt}.json"))
        evidence = record["evidence"]
        if (record["version"] != 1 or record["subject"] != subject or evidence["run"] != number
                or evidence["attempt"] != attempt or evidence["status"] not in ("passed", "failed")
                or any(evidence[k] != subject[k] for k in ("commit", "tree", "base_commit"))):
            raise BoardError("cloud_conflict")
        return evidence

    def _record_check(self, value, subject, evidence):
        previous = value.get("cloud_attempt")
        if previous and previous["run"] == evidence["run"] and previous["attempt"] > evidence["attempt"]:
            raise BoardError("cloud_conflict")
        record = {"version": 1, "subject": subject, "evidence": evidence}
        archive = self.root / f"cloud-{evidence['run']}-{evidence['attempt']}.json"
        if archive.exists():
            if session._decode(session._read_state(archive)) != record:
                raise BoardError("cloud_conflict")
        else:
            session._save(archive, record)
        value["cloud_attempt"] = {"run": evidence["run"], "attempt": evidence["attempt"]}

    def attach_check(self, run_number):
        """Compatibility for the diagnostic CLI and already-open older clients."""
        cloud.run_number(run_number)
        with session._lock(self.root / "cloud-lookup"):
            subject = self._cloud_subject(self._load())
            evidence = cloud.verify(run_number, subject["commit"], subject["tree"], subject["base_commit"])
            with session._lock(self.path):
                value = self._load()
                if self._cloud_subject(value) != subject:
                    raise BoardError("cloud_stale")
                self._record_check(value, subject, evidence)
                self._save(value)
        return self.show()

    def follow_check(self, hint, expected, *, allow_verify):
        """Observe a public index entry. Only separately verified evidence can pass."""
        with session._lock(self.root / "cloud-lookup"):
            value = self._load()
            if self._cloud_subject(value) != expected:
                raise BoardError("cloud_stale")
            prev = value.get("cloud_seen")
            if prev and (prev["order"], prev["attempt"]) > (hint["order"], hint["attempt"]):
                return
            same = prev and (prev["run"], prev["attempt"]) == (hint["run"], hint["attempt"])
            if same and prev["phase"] in ("verified", "unverified"):
                return
            receipt = self._cloud_receipt(value, expected)
            phase = hint["phase"]
            if receipt:
                if receipt["run"] == hint["run"]:
                    if receipt["attempt"] > hint["attempt"]:
                        return
                    if receipt["attempt"] == hint["attempt"]:
                        phase = "verified"
                elif not prev and hint["created"] < datetime.fromisoformat(receipt["completed_at"]).timestamp():
                    # Pre-index receipts have no workflow sequence. Retain
                    # their evidence when replaying older public history.
                    return
            evidence = failure = None
            used = phase == "finished"
            if phase == "finished":
                if not allow_verify:
                    return False
                try:
                    evidence = cloud.verify(str(hint["run"]), expected["commit"], expected["tree"], expected["base_commit"])
                    if evidence["attempt"] != hint["attempt"]:
                        return  # A rerun started between listing and verification.
                    phase = "verified"
                except cloud.CloudError as exc:
                    if str(exc) == "cloud_unavailable":
                        phase, failure = "waiting", exc
                    else:
                        phase = "unverified"
            observed = {**hint, "phase": phase}
            if observed != prev:
                with session._lock(self.path):
                    value = self._load()
                    if self._cloud_subject(value) != expected:
                        raise BoardError("cloud_stale")
                    if evidence:
                        self._record_check(value, expected, evidence)
                    value["cloud_seen"] = observed
                    self._save(value)
            if failure:
                raise failure
            return used

    def stop(self) -> dict:
        with session._lock(self.path):
            value = self._load()
            if value is None:
                raise BoardError("api_assignment_missing")
            if value["status"] in {"prepared", "queued", "running"}:
                self.stop_path.touch(mode=0o600, exist_ok=True)
                if value["status"] != "running":
                    value.update(status="stopped", reason="operator_stopped")
                    self._save(value)
                    self._hold(value, "Запуск отменён до обращения к моделям.")
        return self.show()

    def _hold(self, value, text):
        def mutate(front, body):
            # Do not overwrite operator edits or a different/recreated card.
            if (self.card_digest(front, body) != value["card_digest"]
                    or front.get("state") not in {*self.context["active_states"], "needs_human"}):
                return front, body
            front["state"] = "needs_human"
            front["updated_at"] = session._now()
            return front, _held_body(body, text)
        self.tracker._mutate_ticket(self.identifier, mutate)

    def review(self, offset=0):
        value = self._load()
        if not value or value["status"] in {"queued", "running"}:
            raise BoardError("api_result_not_ready")
        result = session.review(self.session_path, offset=offset)
        revision = value.get("candidate", {}).get("revision")
        if revision is not None and result["revision"] != revision:
            raise BoardError("api_candidate_result_changed")
        return result

    def run(self, run_id, *, credentials=None, transport=None, cancelled=None):
        with session._lock(self.root / "owner"):
            return self._run(run_id, credentials=credentials, transport=transport, cancelled=cancelled)

    def _run(self, run_id, *, credentials=None, transport=None, cancelled=None):
        with session._lock(self.path):
            value = self._load()
            if (not value or value["status"] != "queued" or self.stop_path.exists()
                    or not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id)):
                raise BoardError("api_assignment_not_queued")
            try:
                unchanged = self.proposal()["binding"] == value["binding"]
            except (BoardError, candidate.CandidateError, session.SessionError, project.ProjectError, OSError, ValueError):
                unchanged = False
            if not unchanged:
                value.update(status="stopped", reason="api_assignment_changed")
                self._save(value)
                self._hold(value, "Назначение изменилось после подтверждения. Платный запуск не выполнен.")
                return self.show()
            value.update(status="running", run_id=run_id, started_at=session._now())
            self._save(value)
        try:
            session.create(self.session_path, value["spec"])
            def should_stop():
                if self.stop_path.exists() or bool(cancelled and cancelled()):
                    return True
                try:
                    front, body = self.card()
                    return (front.get("state") not in self.context["active_states"]
                            or self.card_digest(front, body) != value["card_digest"])
                except (BoardError, SymphonyError, OSError, ValueError):
                    # Removed/malformed cards withdraw permission; do not send
                    # another request while their identity cannot be checked.
                    return True
            if should_stop():
                raise Interrupted()
            # run invokes the loader only after its local isolation preflight.
            outcome = session.run(self.session_path, transport=transport, cancelled=should_stop,
                                  credential_loader=credentials or keychain_credentials)
            value.update(status="result" if outcome["status"] == "dialogue_complete" else "stopped",
                         reason=outcome["stop_reason"])
            if outcome["status"] == "dialogue_complete":
                value["candidate"] = candidate.public_report(candidate.prepare(self.session_path, self.workflow.parent,
                    self.identifier, value["rules"], cancelled=should_stop))
        except Interrupted:
            if self.session_path.exists():
                session.interrupt(self.session_path, "operator_stopped")
            value.update(status="stopped", reason="operator_stopped")
        except (BoardError, candidate.CandidateError, session.SessionError, session.workspace.WorkspaceError,
                session.sandbox.SandboxError, project.ProjectError, OSError):
            if self.session_path.exists():
                session.interrupt(self.session_path, "board_execution_failed")
            value.update(status="stopped", reason="board_execution_failed")
        with session._lock(self.path):
            value["finished_at"] = session._now()
            self._save(value)
        self._hold(value, RESULT_NOTE)
        return self.show()


def sync_checks(settings: Path, context: dict):
    """One free service tick for this profile, independent of the browser."""
    if context.get("identifier") != "":
        raise BoardError("invalid_board_context")
    folder = Path(context["workflow"]).resolve(strict=True).parent / ".ours-state/api-team"
    if not folder.is_dir() or session._decode(settings.read_bytes()).get("cloud_check") != cloud.PROFILE:
        return {}
    profile = _digest([str(settings.resolve()), context["agent_kind"]])[:24]
    path = session._path(_directory(folder) / f"cloud-index-{profile}.json")
    with session._lock(path):
        scan = session._decode(session._read_state(path)) if path.exists() else {}
        now = time.time()
        if scan.get("next", 0) > now:
            return {}
        targets = []
        for child in sorted(folder.iterdir()):
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", child.name) or child.is_symlink()
                    or not (child / "assignment.json").is_file()):
                continue
            try:
                board = Board(settings, {**context, "identifier": child.name})
                value = board._load()
                subject = board._cloud_subject(value)
                targets.append((board, subject, datetime.fromisoformat(value["created_at"]).timestamp()))
            except (BoardError, session.SessionError, OSError, ValueError, KeyError):
                continue
        if not targets:
            return {}
        # Reserve the next visit before I/O, including across a restart. At
        # most one new receipt per tick; discovery pages are shared by cards.
        scan["next"] = now + 240
        session._save(path, scan)
        budget = True
        try:
            matches = cloud.discover({s["commit"] for _, s, _ in targets}, min(t for _, _, t in targets) - 3600, scan)
            for board, subject, _ in targets:
                hint = matches.get(subject["commit"])
                if hint:
                    try:
                        if board.follow_check(hint, subject, allow_verify=budget):
                            budget = False
                    except (BoardError, session.SessionError):
                        continue
        except cloud.CloudError as exc:
            scan["next"] = max(now + 600, exc.retry_at)
        session._save(path, scan)
    return {}


def keychain_credentials(spec):
    """Read each required service once; never print/store its secret value."""
    result = {}
    profiles = [spec["lead_profile"], spec["worker_profile"]]
    for name in profiles:
        if name is None:
            continue
        key = api.profile_for(name).key_env
        if key in result:
            continue
        if sys.platform != "darwin":
            raise BoardError("api_keychain_platform_unsupported")
        try:
            proc = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICES[key],
                                   "-a", "symphony", "-w"], stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=120, env={"PATH": os.defpath, "HOME": str(Path.home())})
            if proc.returncode != 0:
                raise BoardError("api_keychain_unavailable")
            result[key] = proc.stdout.decode().rstrip("\r\n")
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            raise BoardError("api_keychain_unavailable") from None
    session._credentials(spec, result)
    return result


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--action", required=True, choices=["prepare", "confirm", "show", "stop", "review", "attach-check", "sync-checks", "run"])
    args = parser.parse_args(argv)
    def interrupted(*_):
        raise Interrupted()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        raw = sys.stdin.buffer.read(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE:
            raise BoardError("api_board_message_too_large")
        message = session._decode(raw)
        if not isinstance(message, dict) or set(message) - {"context", "payload", "run_id"}:
            raise BoardError("invalid_board_message")
        payload = message.get("payload", {})
        if not isinstance(payload, dict):
            raise BoardError("invalid_board_payload")
        if args.action == "sync-checks":
            if payload or message.get("run_id") is not None:
                raise BoardError("invalid_board_payload")
            print(json.dumps(sync_checks(Path(args.settings), message["context"])))
            return 0
        board = Board(Path(args.settings), message["context"])
        if args.action == "confirm":
            if set(payload) != {"confirmation_token"}:
                raise BoardError("invalid_board_payload")
            result = board.confirm(payload["confirmation_token"])
        elif args.action == "review":
            if set(payload) - {"offset"}:
                raise BoardError("invalid_board_payload")
            result = board.review(payload.get("offset", 0))
        elif args.action == "attach-check":
            if set(payload) != {"run_number"}:
                raise BoardError("invalid_board_payload")
            result = board.attach_check(payload["run_number"])
        elif args.action == "run":
            if payload:
                raise BoardError("invalid_board_payload")
            parent = os.getppid()
            result = board.run(message.get("run_id"), cancelled=lambda: os.getppid() != parent)
        else:
            if payload:
                raise BoardError("invalid_board_payload")
            result = getattr(board, args.action)()
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (BoardError, cloud.CloudError, candidate.CandidateError, session.SessionError, project.ProjectError, OSError, ValueError, KeyError) as exc:
        # Detailed provider/OS error bodies may contain credentials. This is a
        # fixed bridge error, not a swallowed success or a license to retry.
        code = str(exc) if isinstance(exc, (BoardError, cloud.CloudError, session.SessionError)) else "api_board_action_refused"
        print(json.dumps({"error": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
