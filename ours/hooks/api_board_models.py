"""Board defaults and public model names. No key lookup, task dispatch or execution."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

import api_executor as api
import api_session as session
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
    session.validate_spec({"task": "team", **value})
    return dict(value)


def from_spec(spec):
    return pair({"lead_profile": spec.get("lead_profile", "deepseek-lead"),
                 "worker_profile": spec.get("worker_profile", "luna-worker")})


def catalog():
    choices = []
    for lead in api.PROFILES:
        for worker in [*api.PROFILES, None]:
            try:
                choices.append(pair({"lead_profile": lead, "worker_profile": worker}))
            except session.SessionError:
                pass
    return {"pairs": choices, "models": {p.name: p.model for p in api.PROFILES.values()}}


def _checked_dir(path):
    if path.resolve() != path or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise session.SessionError("unsafe_state_file")
    return path


class Teams:
    def __init__(self, settings, context):
        self.settings = Path(settings).resolve(strict=True)
        self.base = Path(context["workflow"]).resolve(strict=True).parent / ".ours-state"
        self.cards = Path(context.get("board_root", self.base.parent / "kanban"))
        self.binding = project_binding(context)
        self.kind = context["agent_kind"]
        if not isinstance(self.kind, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.kind):
            raise session.SessionError("invalid_composition")
        self.file = self.base / "team-defaults" / (self.kind + ".json")

    def default(self):
        if self.file.exists() or self.file.is_symlink():
            _checked_dir(self.base)
            _checked_dir(self.file.parent)
            return pair(session._decode(session._read_state(self.file)))
        raw = session._decode(self.settings.read_bytes())
        return from_spec(raw["session"])

    def save(self, choice):
        choice = pair(choice)
        for p in (self.base, self.file.parent):
            p.mkdir(mode=0o700, exist_ok=True)
            _checked_dir(p)
        if stat.S_IMODE(self.file.parent.stat().st_mode) != 0o700:
            raise session.SessionError("unsafe_state_file")
        with session._lock(self.file):
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
        default = self.default()
        raw = session._decode(self.settings.read_bytes())
        frozen = {"team": default, "mode": raw.get("mode"), "policy": policy_digest(raw)}
        return {"default": default, "creation_defaults": frozen, "project_binding": self.binding,
                "assignments": assigned, **catalog()}
