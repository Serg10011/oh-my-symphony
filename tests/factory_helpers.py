"""Factory test setup shared by skill-copying assertions."""

from pathlib import Path

from symphony.cli import factory as factory_cli


def install_skills(target: Path, names: set[str], *, force: bool) -> None:
    factory_cli._copy_skills(
        target,
        factory_cli._skill_sources(names),
        force=force,
        recovery="rerun 'symphony factory sync <project> --force'",
    )
