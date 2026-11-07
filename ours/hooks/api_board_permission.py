"""Host-owned board spending permission; drafts and old assignments never grant it."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

import api_board_config as config
import api_board_models as models
import api_candidate as candidate
import api_project as project
import api_session as session


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def private_dir(path):
    path.mkdir(mode=0o700, exist_ok=True)
    if (path.resolve() != path or path.stat().st_uid != os.getuid()
            or stat.S_IMODE(path.stat().st_mode) != 0o700):
        raise session.SessionError("api_permission_storage_unsafe")
    return path


class Permission:
    def __init__(self, settings, context):
        self.settings = Path(settings).resolve(strict=True)
        self.context = {k: v for k, v in context.items() if k != "identifier"}
        self.workflow = Path(context["workflow"]).resolve(strict=True)
        self.kind = context["agent_kind"]
        if not isinstance(self.kind, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.kind):
            raise session.SessionError("invalid_composition")
        base = self.workflow.parent / ".ours-state"
        base.mkdir(mode=0o700, exist_ok=True)
        models._checked_dir(base)
        self.root = private_dir(private_dir(base / "api-permissions") / self.kind)
        self.path = self.root / "permission.json"
        self.pending = self.root / "pending.json"

    def _load(self):
        if not self.path.exists() and not self.path.is_symlink():
            return None
        value = session._decode(session._read_state(self.path))
        if (not isinstance(value, dict) or value.get("version") != 1
                or value.get("status") not in {"active", "revoked"}
                or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("revision", "")))
                or not isinstance(value.get("history"), list)):
            raise session.SessionError("api_permission_invalid")
        if value["status"] == "active" and (not re.fullmatch(r"[0-9a-f]{32}", str(value.get("id", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("fingerprint", "")))
                or not isinstance(value.get("terms"), dict)):
            raise session.SessionError("api_permission_invalid")
        return value

    def revision(self):
        value = self._load()
        return value["revision"] if value else None

    def proposal(self):
        raw = config.load_settings(self.settings)
        team = models.Teams(self.settings, self.context).default()
        spec = session.validate_spec({**config.session_options(raw, team), "task": "Board permission",
            "project": {"root": self.context["repo"], "paths": raw["paths"], "commit": "0" * 40},
            "execution": raw["execution"]})
        repo = Path(spec["project"]["root"])
        if repo.resolve(strict=True) != repo:
            raise session.SessionError("api_permission_changed")
        project._git(str(repo), ["rev-parse", "--verify", "--end-of-options",
                                (self.context["base_branch"] or "HEAD") + "^{commit}"], limit=128)
        info = repo.stat()
        terms = {"repo": str(repo), "base_branch": self.context["base_branch"],
                 "team": team, "mode": raw.get("mode"), "profiles": session._profiles(spec),
                 "paths": spec["project"]["paths"], "budget": spec.get("budget"), "limits": spec["limits"],
                 "budget_scope": "per_task", "process_timeout_seconds": self.context["process_timeout_seconds"]}
        binding = {"terms": terms, "context": self.context, "settings_path": str(self.settings),
                   "settings": raw, "repository_identity": [info.st_dev, info.st_ino],
                   "workflow": hashlib.sha256(self.workflow.read_bytes()).hexdigest(),
                   "rules": candidate.rules_snapshot(self.workflow.parent),
                   "code": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in sorted(Path(__file__).parent.glob("api_*.py"))}}
        return {"fingerprint": digest(binding), "terms": terms}, spec

    def show(self):
        try:
            value = self._load()
        except (session.SessionError, OSError, ValueError, KeyError):
            return {"status": "invalid", "reason": "api_permission_invalid", "history": []}
        if not value:
            return {"status": "not_configured", "history": []}
        result = {k: value[k] for k in ("status", "revision", "history")}
        for key in ("id", "terms", "approved_at", "revoked_at", "credentials_checked_at"):
            if key in value:
                result[key] = value[key]
        if value["status"] == "active":
            try:
                if self.proposal()[0]["fingerprint"] != value["fingerprint"]:
                    result.update(status="changed", reason="api_permission_changed")
            except (session.SessionError, project.ProjectError, candidate.CandidateError, OSError, ValueError, KeyError):
                result.update(status="changed", reason="api_permission_changed")
        return result

    def creation_id(self):
        value = self.show()
        return value.get("id") if value["status"] == "active" else None

    def prepare(self):
        with session._lock(self.path):
            proposal, _ = self.proposal()
            token = secrets.token_urlsafe(32)
            pending = {**proposal, "token_sha256": digest(token), "revision": self.revision(),
                       "expires_at": time.time() + 600}
            session._save(self.pending, pending)
        return {"status": "needs_confirmation", **proposal, "confirmation_token": token}

    def _pending(self, token):
        if not isinstance(token, str) or len(token) > 256 or not self.pending.exists():
            raise session.SessionError("api_permission_confirmation_invalid")
        value = session._decode(session._read_state(self.pending))
        if not hmac.compare_digest(value.get("token_sha256", ""), digest(token)):
            raise session.SessionError("api_permission_confirmation_invalid")
        return value

    def _validate_pending(self, pending):
        proposal, spec = self.proposal()
        if (time.time() > pending["expires_at"] or pending["revision"] != self.revision()
                or pending["fingerprint"] != proposal["fingerprint"]):
            raise session.SessionError("api_permission_changed")
        return spec

    def authorize(self, token, credentials):
        # Keychain may ask the owner for access. Never hold the revocation lock
        # during that prompt, and revalidate everything after it returns.
        with session._lock(self.path):
            pending = self._pending(token)
            if pending.get("consumed"):
                if (self._load() or {}).get("id") != pending["consumed"]:
                    raise session.SessionError("api_permission_incomplete")
                return self.show()
            spec = self._validate_pending(pending)
            current = self._load()
            if current and current["status"] == "active" and current["fingerprint"] == pending["fingerprint"]:
                pending["consumed"] = current["id"]
                session._save(self.pending, pending)
                return self.show()
        session._credentials(spec, credentials(spec))
        with session._lock(self.path):
            pending = self._pending(token)
            if pending.get("consumed"):
                if (self._load() or {}).get("id") != pending["consumed"]:
                    raise session.SessionError("api_permission_incomplete")
                return self.show()
            self._validate_pending(pending)
            previous = self._load()
            identifier = secrets.token_hex(16)
            now = session._now()
            event = {"action": "authorized", "at": now, "id": identifier,
                     "fingerprint": pending["fingerprint"], "terms": pending["terms"]}
            value = {"version": 1, "status": "active", "id": identifier, "revision": identifier,
                     "fingerprint": pending["fingerprint"], "terms": pending["terms"],
                     "approved_at": now, "credentials_checked_at": now,
                     "history": [*(previous["history"] if previous else []), event]}
            # Permission is published last. A crash before this save cannot
            # activate a permission and a replay cannot activate it twice.
            pending["consumed"] = identifier
            session._save(self.pending, pending)
            session._save(self.path, value)
        return self.show()

    def revoke(self):
        # Admission holds this lock only while recording a request, never
        # during network I/O. The owner's stop waits for that short boundary
        # instead of failing with "busy" and requiring another click.
        with self._revocation_lock():
            value = self._load() or {"version": 1, "history": []}
            if value.get("status") != "revoked":
                now = session._now()
                value.update(status="revoked", revision=secrets.token_hex(16), revoked_at=now)
                value["history"].append({"action": "revoked", "at": now, "id": value.get("id")})
                session._save(self.path, value)
        return self.show()

    @contextmanager
    def _revocation_lock(self):
        fd = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            session._safe_file(fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def validate_submission(self, board, expected):
        value = self._load()
        if not value or value["status"] != "active":
            raise session.SessionError("api_permission_required")
        if self.proposal()[0]["fingerprint"] != value["fingerprint"]:
            raise session.SessionError("api_permission_changed")
        front, body = board.card()
        frozen = front.get("api_defaults")
        if (not isinstance(expected, dict) or set(expected) != {"authorization_id", "card_digest"}
                or expected["authorization_id"] != value["id"]
                or not isinstance(frozen, dict) or frozen.get("authorization_id") != value["id"]):
            raise session.SessionError("api_permission_card_not_eligible")
        if (board.card_digest(front, body) != expected["card_digest"]
                or front.get("state") != board.context["active_states"][0]
                or frozen.get("team") != value["terms"]["team"]
                or frozen.get("mode") != value["terms"]["mode"]):
            raise session.SessionError("api_permission_task_changed")
        return {"id": value["id"], "revision": value["revision"], "fingerprint": value["fingerprint"],
                "terms": value["terms"], "source": "board_permission", "submitted_at": session._now()}

    def allows(self, assignment):
        try:
            return self._allows(assignment)
        except (session.SessionError, project.ProjectError, candidate.CandidateError, OSError, ValueError, KeyError):
            return False

    def _allows(self, assignment):
        value = self._load()
        if not value:
            return not assignment.get("permission_revision") and not assignment.get("authorization")
        if value["status"] != "active" or assignment.get("permission_revision") != value["revision"]:
            return False
        auth = assignment.get("authorization")
        if auth:
            return (auth.get("id") == value["id"] and auth.get("fingerprint") == value["fingerprint"]
                    and self.proposal()[0]["fingerprint"] == value["fingerprint"])
        return True  # An explicit per-card approval has its own immutable binding.

    @contextmanager
    def admit(self, assignment):
        # Serialize revocation against the durable admission of ONE request.
        # The network call is outside this short lock; already admitted calls
        # may finish and are accounted for even after permission is withdrawn.
        with session._lock(self.path):
            if not self.allows(assignment):
                raise session.SessionError("api_permission_revoked")
            yield
