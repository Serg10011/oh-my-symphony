"""Antigravity CLI backend (`agy`) — the built-in `agy` plain-CLI profile."""

from __future__ import annotations

from .plain_cli import PlainCliBackend


class AgyBackend(PlainCliBackend):
    """Drive `agy --print "$(cat)"` once per Symphony worker turn."""

    PROFILE = "agy"
