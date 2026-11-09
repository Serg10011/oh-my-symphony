"""Board defaults and public model names. No key lookup, task dispatch or execution."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

import api_board_config as config
import api_executor as api
import api_gemini_quota as quota
import api_project as project
import api_session as session
from api_flash_probe import ProbeError
from symphony.errors import SymphonyError
from symphony.trackers.file import parse_ticket_file


def project_binding(context):
    fields = ("workflow", "board_root", "repo", "base_branch")
    if any(not isinstance(context.get(key), str) for key in fields):
        return None
    return hashlib.sha256(json.dumps([context[key] for key in fields]).encode()).hexdigest()


def policy_digest(settings):
    # Grants and budgets must still match at preparation. Team and mode are pinned per card.
    options = {k: v for k, v in settings["session"].items() if k not in ("lead_profile", "worker_profile")}
    policy = {k: v for k, v in settings.items() if k != "mode"}
    policy["session"] = options
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pair(value):
    if (not isinstance(value, dict) or set(value) != {"lead_profile", "worker_profile"}
            or not isinstance(value["lead_profile"], str)
            or value["worker_profile"] is not None and not isinstance(value["worker_profile"], str)):
        raise session.SessionError("invalid_composition")
    session.validate_composition(value["lead_profile"], value["worker_profile"])
    return dict(value)


def from_spec(spec):
    return pair({"lead_profile": spec.get("lead_profile", "deepseek-lead"),
                 "worker_profile": spec.get("worker_profile", "luna-worker")})


def catalog():
    choices = []
    for lead in api.PROFILES:
        for worker in [*api.PROFILES, api.FLASH_WORKER, None]:
            try:
                choices.append(pair({"lead_profile": lead, "worker_profile": worker}))
            except session.SessionError:
                pass
    profiles = [*api.PROFILES.values(), api.FLASH_PROFILE]
    return {"pairs": choices, "models": {p.name: p.model for p in profiles}}


def _checked_dir(path):
    if path.resolve() != path or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise session.SessionError("unsafe_state_file")
    return path


class Teams:
    def __init__(self, settings, context):
        self.context = context
        self.settings = Path(settings).resolve(strict=True)
        self.base = Path(context["workflow"]).resolve(strict=True).parent / ".ours-state"
        self.cards = Path(context.get("board_root", self.base.parent / "kanban"))
        self.binding = project_binding(context)
        self.kind = context["agent_kind"]
        if not isinstance(self.kind, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.kind):
            raise session.SessionError("invalid_composition")
        self.file = self.base / "team-defaults" / (self.kind + ".json")
        self.work_file = self.base / "work-settings" / (self.kind + ".json")
        self.quota_file = self.base / "gemini-quota" / "snapshot.json"

    def default(self, raw=None):
        if self.file.exists() or self.file.is_symlink():
            _checked_dir(self.base)
            _checked_dir(self.file.parent)
            return pair(session._decode(session._read_state(self.file)))
        if raw is None:
            raw = config.load_settings(self.settings)
        return from_spec(raw["session"])

    def work_settings(self, raw):
        if not self.work_file.exists() and not self.work_file.is_symlink():
            return raw, None
        _checked_dir(self.base)
        folder = _checked_dir(self.work_file.parent)
        if stat.S_IMODE(folder.stat().st_mode) != 0o700:
            raise session.SessionError("invalid_api_work_settings")
        value = session._decode(session._read_state(self.work_file))
        if (not isinstance(value, dict) or set(value) != {"version", "binding", "revision", "mode", "paths"}
                or type(value["version"]) is not int or value["version"] != 1
                or not self.binding or value["binding"] != self.binding
                or not isinstance(value["revision"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["revision"])):
            raise session.SessionError("invalid_api_work_settings")
        try:
            paths = project.validate({"root": self.context["repo"], "paths": value["paths"]})["paths"]
        except project.ProjectError:
            raise session.SessionError("invalid_api_work_settings") from None
        return config.with_work_settings(raw, value["mode"], paths), value["revision"]

    def defaults(self):
        raw, revision = self.work_settings(config.load_settings(self.settings))
        return raw, self.default(raw), revision

    def saved_gemini_quota(self):
        path = self.quota_file
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > quota.MAX_BYTES:
                return None
            _checked_dir(self.base)
            return quota.saved_snapshot(session._decode(session._read_state(session._path(path))))
        except (OSError, session.SessionError, ProbeError):
            return None

    def refresh_gemini_quota(self):
        self.base.mkdir(mode=0o700, exist_ok=True)
        _checked_dir(self.base)
        folder = self.quota_file.parent
        folder.mkdir(mode=0o700, exist_ok=True)
        _checked_dir(folder)
        path = session._path(self.quota_file)
        with session._lock(path):
            snapshot = self.saved_gemini_quota()
            if not quota.recent(snapshot, 5):
                try:
                    snapshot = quota.saved_snapshot(quota.read(temporary_root=folder))
                except (OSError, ProbeError):
                    raise session.SessionError("gemini_quota_unavailable") from None
                if snapshot["status"] == "unavailable":
                    raise session.SessionError("gemini_quota_unavailable")
                session._save(path, snapshot)
        return {"gemini_quota": snapshot}

    def has_queued_work(self, *, board_permission_only=False):
        assignments = self.base / "api-team"
        if not assignments.exists() and not assignments.is_symlink():
            return False
        for folder in _checked_dir(assignments).iterdir():
            path = folder / "assignment.json"
            if not path.exists() and not path.is_symlink():
                continue
            _checked_dir(folder)
            value = session._decode(session._read_state(path))
            if (not isinstance(value, dict) or value.get("identifier") != folder.name
                    or value.get("status") not in ("prepared", "queued", "running", "result", "stopped")):
                raise session.SessionError("invalid_api_assignment")
            if value["status"] in ("queued", "running") and (not board_permission_only or value.get("authorization")):
                return True
        return False

    def save(self, choice):
        from api_board_permission import Permission

        choice = pair(choice)
        for p in (self.base, self.file.parent):
            p.mkdir(mode=0o700, exist_ok=True)
            _checked_dir(p)
        if stat.S_IMODE(self.file.parent.stat().st_mode) != 0o700:
            raise session.SessionError("unsafe_state_file")
        permission = Permission(self.settings, self.context)
        with session._lock(permission.settings_lock), session._lock(self.file):
            if choice != self.default():
                # Manually confirmed cards already have an independent team.
                if self.has_queued_work(board_permission_only=True):
                    raise session.SessionError("api_board_settings_busy")
            session._save(self.file, choice)
        return {"default": choice, **catalog()}

    def directory(self, identifiers):
        if (not isinstance(identifiers, list) or len(identifiers) > 100 or
                any(not isinstance(i, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", i)
                    for i in identifiers)):
            raise session.SessionError("invalid_board_payload")
        assigned = {}
        for key in dict.fromkeys(identifiers):
            p = self.base / "api-team" / key / "assignment.json"
            try:
                if not p.exists():
                    card = self.cards / (key + ".md")
                    if card.is_file() and not card.is_symlink():
                        frozen = parse_ticket_file(card)[0].get("api_defaults")
                        if frozen is not None:
                            assigned[key] = pair(frozen["team"])
                    continue
                assigned[key] = None  # Never substitute a new default for unreadable history.
                _checked_dir(self.base)
                _checked_dir(p.parent.parent)
                _checked_dir(p.parent)
                if p.is_symlink() or p.stat().st_size > 1024 * 1024:
                    continue
                item = session._decode(session._read_state(p))
                if item["identifier"] == key:
                    assigned[key] = from_spec(item["spec"])
            except (OSError, ValueError, KeyError, TypeError, session.SessionError, SymphonyError):
                continue
        defaults = self.defaults()
        raw, default, _ = defaults
        frozen = {"team": default, "mode": raw.get("mode"), "policy": policy_digest(raw)}
        # Existing boards remain per-card until the owner explicitly approves
        # a board permission. Only cards created under that permission inherit it.
        from api_board_permission import Permission

        permission = Permission(self.settings, self.context).show(defaults=defaults)
        if permission["status"] == "active":
            frozen["authorization_id"] = permission["id"]
        work = {"mode": raw.get("mode"), "paths": raw["paths"]}
        return {"default": default, "creation_defaults": frozen, "work_settings": work, "project_binding": self.binding,
                "gemini_quota": self.saved_gemini_quota(),
                "assignments": assigned, "execution_permission": permission, **catalog()}
