"""Kiro CLI backend — the built-in `kiro` plain-CLI profile."""

from __future__ import annotations

from .plain_cli import PlainCliBackend


class KiroBackend(PlainCliBackend):
    """Drive `kiro-cli chat --no-interactive` once per Symphony worker turn."""

    PROFILE = "kiro"
