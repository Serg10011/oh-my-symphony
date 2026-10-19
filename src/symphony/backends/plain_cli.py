"""Plain-text CLI backend driven by an `AgentProfileConfig`.

Every plain-stdout CLI (built-in `agy` / `kiro`, and any `agent.profiles`
entry from WORKFLOW.md) runs through this one class: the prompt goes to
stdin, stdout is the final assistant message, and the profile data decides
the extra flags. Adding such a CLI needs a profile, not a module.
"""

from __future__ import annotations

from ..errors import ConfigValidationError
from ..workflow import AgentProfileConfig
from . import EVENT_TURN_COMPLETED, BackendInit, TurnResult
from .per_turn import PerTurnCliBackend, _has_shell_flag

_PROMPT_ARG_MARKERS = ('"$(cat)"', "$(cat)")


class PlainCliBackend(PerTurnCliBackend):
    """One subprocess per turn; treats stdout as the final assistant message."""

    # Profile to load; None means "the profile named by `agent.kind`".
    PROFILE: str | None = None

    def __init__(self, init: BackendInit) -> None:
        name = self.PROFILE or init.cfg.agent.kind
        profile = init.cfg.agent_profile(name)
        if profile is None:
            raise ConfigValidationError(
                f"no plain-CLI agent profile named {name!r}", value=name
            )
        super().__init__(
            init, agent_name=profile.name, turn_timeout_ms=profile.turn_timeout_ms
        )
        self._profile: AgentProfileConfig = profile

    def _command_for_turn(self, *, prompt: str, is_continuation: bool) -> str:
        del prompt  # travels via stdin
        profile = self._profile
        cmd = profile.command
        missing = [
            flag for flag in profile.unattended_flags if not _has_shell_flag(cmd, flag)
        ]
        flag = profile.continuation_flag
        if (
            flag is not None
            and is_continuation
            and profile.resume_across_turns
            and not _has_shell_flag(cmd, flag, *profile.continuation_flag_aliases)
        ):
            missing.append(flag)
        if not missing:
            return cmd
        if profile.continuation_position == "before_prompt_arg":
            return _insert_before_prompt_arg(cmd, " ".join(missing))
        return f"{cmd} {' '.join(missing)}"

    async def _complete_turn(self, stdout_text: str, rc: int) -> TurnResult:
        await self._emit(
            EVENT_TURN_COMPLETED,
            {
                "message": stdout_text,
                "result": stdout_text,
                "response": stdout_text,
                "session_id": self._session_id,
                "stats": {},
                "exit_code": rc,
            },
        )
        return TurnResult(
            status=EVENT_TURN_COMPLETED,
            turn_id=self._session_id,
            last_message=stdout_text[:400],
        )


def _insert_before_prompt_arg(command: str, flags: str) -> str:
    """Put *flags* before a trailing `"$(cat)"` prompt argument, else append."""
    stripped = command.rstrip()
    for marker in _PROMPT_ARG_MARKERS:
        if stripped.endswith(marker):
            prefix = stripped[: -len(marker)].rstrip()
            return f"{prefix} {flags} {marker}"
    return f"{stripped} {flags}"
