import argparse
import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from symphony._shell import resolve_bash
from symphony.workflow import SUPPORTED_AGENT_KINDS, build_service_config, load_workflow
from symphony.workflow.constants import DEFAULT_AGY_COMMAND
from symphony.cli import board as board_cli
from symphony.cli import factory as factory_cli
from symphony.factory.wayfinder import parse_wayfinder_ticket
from symphony.skills import render_skill_block

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _installed_supergoal_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep factory CLI tests hermetic from developer-installed skills."""
    skill_root = tmp_path / "installed-skills"
    supergoal = skill_root / "supergoal"
    (supergoal / "reference").mkdir(parents=True)
    (supergoal / "agents").mkdir()
    (supergoal / "SKILL.md").write_text("# Supergoal\n", encoding="utf-8")
    (supergoal / "reference/role-loop.md").write_text("loop\n", encoding="utf-8")
    (supergoal / "agents/executor.md").write_text("execute\n", encoding="utf-8")
    monkeypatch.setattr(factory_cli, "_SKILL_SEARCH_ROOTS", (skill_root,))


def test_board_new_accepts_skills(tmp_path: Path) -> None:
    assert board_cli.main(
        ["new", "--root", str(tmp_path), "T-1", "ticket", "--skills", "supergoal,superqa"]
    ) == 0
    text = (tmp_path / "T-1.md").read_text(encoding="utf-8")
    assert "skills:" in text
    assert "supergoal" in text and "superqa" in text


def test_board_new_defaults_to_workflow_first_active_state(tmp_path: Path) -> None:
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(
        "---\ntracker:\n  kind: file\n  board_root: ./kanban\n"
        "  active_states: [Ready, Build, Verify]\n  terminal_states: [Done]\n---\n",
        encoding="utf-8",
    )
    assert board_cli.main(["new", "--workflow", str(workflow), "T-1", "ticket"]) == 0
    assert "state: Ready" in (tmp_path / "kanban/T-1.md").read_text(encoding="utf-8")


def test_factory_init_refuses_overwrite_then_force_updates(tmp_path: Path) -> None:
    target = tmp_path / "project"
    assert factory_cli.main(["init", str(target), "--agent-kind", "opencode"]) == 0
    workflow = target / "WORKFLOW.md"
    assert "kind: opencode" in workflow.read_text(encoding="utf-8")
    workflow.write_text("mine", encoding="utf-8")

    assert factory_cli.main(["init", str(target)]) == 1
    assert workflow.read_text(encoding="utf-8") == "mine"
    assert factory_cli.main(["init", str(target), "--force"]) == 0
    assert "active_states: [Ready, Build, Verify]" in workflow.read_text(encoding="utf-8")
    assert (target / "skills/supergoal/reference/role-loop.md").exists()
    assert not (target / "skills/superpm").exists()
    assert not list(target.rglob("__pycache__"))
    assert "(skill not found under skills/)" not in render_skill_block(
        target, ("supergoal",)
    )
    copied = target / "skills/supergoal"
    build_prompt = (
        target / "docs/symphony-prompts/file/stages/build.md"
    ).read_text(encoding="utf-8")
    assert "Read each attached skill's `SKILL.md` once." in build_prompt
    assert "sequentially in the current worker" in build_prompt
    assert "do not create nested worktrees" in build_prompt
    assert not (copied / "docs/changelog").exists()
    assert not (copied / "docs/experiments").exists()
    assert not (copied / "tests").exists()
    assert not (copied / "tui").exists()
    assert not list(copied.rglob("*.pyc"))
    assert not list((target / "wayfinder/tickets").glob("*.md"))


def test_factory_init_merges_runtime_ignores_without_overwriting_user_rules(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    target.mkdir()
    gitignore = target / ".gitignore"
    gitignore.write_text("# user rules\n.env\n", encoding="utf-8")

    assert factory_cli.main(["init", str(target)]) == 0

    text = gitignore.read_text(encoding="utf-8")
    assert text.startswith("# user rules\n.env\n")
    for rule in (
        "/kanban",
        "/.symphony/",
        "/log/",
        "/WORKFLOW-PROGRESS.md",
        "__pycache__/",
        "*.py[cod]",
    ):
        assert text.count(rule) == 1

    assert factory_cli.main(["init", str(target), "--force"]) == 0
    rerun = gitignore.read_text(encoding="utf-8")
    assert rerun == text


def test_factory_generated_board_cards_are_gitignored(tmp_path: Path) -> None:
    target = tmp_path / "project"
    assert factory_cli.main(["init", str(target)]) == 0
    subprocess.run(["git", "init", "-b", "main"], cwd=target, check=True, capture_output=True)
    card = target / "kanban/TASK-1.md"
    card.write_text("---\nstate: Ready\n---\n", encoding="utf-8")

    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "kanban/TASK-1.md"],
        cwd=target,
        check=False,
    )

    assert ignored.returncode == 0


@pytest.mark.parametrize(
    "agent_kind", sorted(SUPPORTED_AGENT_KINDS)
)
def test_factory_init_emits_only_the_selected_backend_block(
    tmp_path: Path, agent_kind: str
) -> None:
    target = tmp_path / agent_kind

    assert factory_cli.main(["init", str(target), "--agent-kind", agent_kind]) == 0

    text = (target / "WORKFLOW.md").read_text(encoding="utf-8")
    assert f"agent:\n  kind: {agent_kind}\n" in text
    backend_headers = {
        line[:-1]
        for line in text.splitlines()
        if line in {f"{kind}:" for kind in SUPPORTED_AGENT_KINDS}
    }
    assert backend_headers == {agent_kind}
    cfg = build_service_config(load_workflow(target / "WORKFLOW.md"))
    assert cfg.agent.kind == agent_kind
    if agent_kind == "agy":
        assert cfg.agy.command == DEFAULT_AGY_COMMAND


def test_factory_init_supports_agent_alias_and_prints_wayfinder_prompt(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert factory_cli.main(["init", str(tmp_path / "project"), "--agent", "claude"]) == 0
    output = capsys.readouterr().out
    assert factory_cli.WAYFINDER_NEXT_STEP_PROMPT in output
    workflow = (tmp_path / "project/WORKFLOW.md").read_text(encoding="utf-8")
    assert "kind: claude" in workflow
    assert "--output-format stream-json --verbose" in workflow
    assert '--add-dir "$SYMPHONY_WORKFLOW_DIR/kanban"' in workflow
    assert "SuperPM" in output
    assert "customer" in output.lower()
    for required in (
        "id: stable-ticket-id",
        "title: One vertical slice",
        "route: GREENFIELD",
        "blocked_by: []",
        "skills: []",
        "kind: ui",
        "browser: true",
        "## Acceptance criteria",
        "## Proof commands",
        "## Non-goals",
        "GREENFIELD, DEBUG, LEGACY",
        "superdesign, superpm, superqa",
        "customer-research, research, design, product-spec, qa, ui",
    ):
        assert required in output
    schema = factory_cli.WAYFINDER_NEXT_STEP_PROMPT.split(
        "For every ticket, use this exact parser-compatible Markdown schema:\n", 1
    )[1].split("\nSchema rules:", 1)[0]
    ticket_path = tmp_path / "printed-schema.md"
    ticket_path.write_text(schema, encoding="utf-8")
    ticket = parse_wayfinder_ticket(ticket_path)
    assert ticket.key == "stable-ticket-id"
    assert ticket.route == "GREENFIELD"
    assert ticket.skills == ("supergoal",)


def test_standard_skill_source_prefers_bundle_over_incomplete_local_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    skill = tmp_path / "skills/superqa"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# SuperQA\n", encoding="utf-8")
    monkeypatch.setattr(factory_cli, "_SKILL_SEARCH_ROOTS", (tmp_path / "skills",))

    source = factory_cli._skill_sources({"superqa"})["superqa"]

    assert "bundled_skills/superqa" in str(source).replace("\\", "/")
    assert source.joinpath("reference", "agent-qa.md").is_file()


def test_factory_init_does_not_require_optional_overlay_skills(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    skill_root = tmp_path / "installed"
    supergoal = skill_root / "supergoal"
    (supergoal / "reference").mkdir(parents=True)
    (supergoal / "agents").mkdir()
    (supergoal / "SKILL.md").write_text("# Supergoal\n", encoding="utf-8")
    (supergoal / "reference/role-loop.md").write_text("loop\n", encoding="utf-8")
    (supergoal / "agents/executor.md").write_text("executor\n", encoding="utf-8")
    monkeypatch.setattr(factory_cli, "_SKILL_SEARCH_ROOTS", (skill_root,))

    target = tmp_path / "project"
    assert factory_cli.main(["init", str(target)]) == 0
    assert (target / "skills/supergoal/SKILL.md").is_file()
    assert not (target / "skills/superpm").exists()


def test_factory_start_stops_when_doctor_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    (target / ".git").mkdir()
    (target / "WORKFLOW.md").write_text("---\n---\n", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(factory_cli, "run_doctor", lambda _path, _port=None: 1)
    monkeypatch.setattr(factory_cli, "start_service", lambda _argv: calls.append("service") or 0)

    assert factory_cli.main(["start", str(target), "--force"]) == 1
    assert calls == []


def test_factory_start_passes_port_override_to_doctor_and_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "project"
    target.mkdir()
    (target / ".git").mkdir()
    (target / "WORKFLOW.md").write_text("---\n---\n", encoding="utf-8")
    seen: list[tuple[str, object]] = []
    monkeypatch.setattr(
        factory_cli, "run_doctor", lambda path, port=None: seen.append(("doctor", port)) or 0
    )
    monkeypatch.setattr(
        factory_cli, "start_service", lambda argv: seen.append(("service", argv)) or 0
    )

    assert factory_cli.main(["start", str(target), "--port", "12345"]) == 0
    assert seen == [
        ("doctor", 12345),
        ("service", ["start", str(target / "WORKFLOW.md"), "--skip-doctor", "--port", "12345"]),
    ]


@pytest.mark.parametrize("command", ["sync", "start"])
def test_factory_commands_accept_wayfinder_as_positional_path(
    command: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "project"
    wayfinder = target / "wayfinder"
    wayfinder.mkdir(parents=True)
    (target / "WORKFLOW.md").write_text("---\n---\n", encoding="utf-8")
    (target / ".git").mkdir()
    seen: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        factory_cli,
        "_sync",
        lambda project, source, _prefix, **_kwargs: seen.append((project, source)) or 0,
    )
    monkeypatch.setattr(factory_cli, "run_doctor", lambda _path, _port=None: 0)
    monkeypatch.setattr(factory_cli, "start_service", lambda _argv: 0)

    assert factory_cli.main([command, str(wayfinder)]) == 0
    assert seen == [(target.resolve(), wayfinder.resolve())]


def test_factory_sync_preserves_target_and_explicit_wayfinder_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "project"
    explicit = tmp_path / "spec/wayfinder"
    target.mkdir()
    seen: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        factory_cli,
        "_sync",
        lambda project, source, _prefix, **_kwargs: seen.append((project, source)) or 0,
    )

    assert factory_cli.main(["sync", str(target)]) == 0
    assert factory_cli.main(
        ["sync", str(target), "--wayfinder", str(explicit)]
    ) == 0
    assert seen == [
        (target.resolve(), (target / "wayfinder").resolve()),
        (target.resolve(), explicit.resolve()),
    ]


@pytest.mark.parametrize("command", ["sync", "start"])
def test_factory_commands_import_full_graph_by_default_with_frontier_opt_in(
    command: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "project"
    target.mkdir()
    (target / ".git").mkdir()
    (target / "WORKFLOW.md").write_text("---\n---\n", encoding="utf-8")
    (target / "wayfinder").mkdir()
    seen: list[bool] = []
    monkeypatch.setattr(
        factory_cli,
        "_sync",
        lambda _project, _source, _prefix, **kwargs: seen.append(
            kwargs["all_tickets"]
        )
        or 0,
    )
    monkeypatch.setattr(factory_cli, "run_doctor", lambda _path, _port=None: 0)
    monkeypatch.setattr(factory_cli, "start_service", lambda _argv: 0)

    assert factory_cli.main([command, str(target)]) == 0
    assert factory_cli.main([command, str(target), "--frontier-only"]) == 0

    assert seen == [True, False]


def test_factory_setup_hook_is_safe_when_run_twice(tmp_path: Path) -> None:
    host = tmp_path / "project"
    workspace = tmp_path / "workspaces/TASK-1"
    host.mkdir()
    (host / "kanban").mkdir()
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=host, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "factory@example.com"], cwd=host, check=True
    )
    subprocess.run(["git", "config", "user.name", "Factory Test"], cwd=host, check=True)
    (host / "README.md").write_text("factory\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=host, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=host, check=True, capture_output=True)
    script = ROOT / "src/symphony/factory/templates/scripts/factory-setup-worktree.sh"
    env = {**os.environ, "SYMPHONY_WORKFLOW_DIR": str(host)}

    for _ in range(2):
        subprocess.run([resolve_bash(), str(script)], cwd=workspace, env=env, check=True)

    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "symphony/TASK-1"
    assert ((workspace / "kanban").is_symlink() or (workspace / "kanban").is_junction())
    assert (workspace / "kanban").resolve() == (host / "kanban").resolve()


def test_factory_setup_hook_replaces_only_tracked_gitkeep_with_shared_board(
    tmp_path: Path,
) -> None:
    host = tmp_path / "project"
    workspace = tmp_path / "workspaces/TASK-1"
    host.mkdir()
    (host / "kanban").mkdir()
    (host / "kanban/.gitkeep").write_text("\n", encoding="utf-8")
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=host, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "factory@example.com"], cwd=host, check=True
    )
    subprocess.run(["git", "config", "user.name", "Factory Test"], cwd=host, check=True)
    (host / "README.md").write_text("factory\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md", "kanban/.gitkeep"], cwd=host, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=host, check=True, capture_output=True)
    script = ROOT / "src/symphony/factory/templates/scripts/factory-setup-worktree.sh"

    result = subprocess.run(
        [resolve_bash(), str(script)],
        cwd=workspace,
        env={**os.environ, "SYMPHONY_WORKFLOW_DIR": str(host)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert ((workspace / "kanban").is_symlink() or (workspace / "kanban").is_junction())
    assert (workspace / "kanban").resolve() == (host / "kanban").resolve()


def test_factory_setup_hook_rejects_nonempty_real_board_directory(
    tmp_path: Path,
) -> None:
    host = tmp_path / "project"
    workspace = tmp_path / "workspaces/TASK-1"
    host.mkdir()
    (host / "kanban").mkdir()
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=host, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "factory@example.com"], cwd=host, check=True
    )
    subprocess.run(["git", "config", "user.name", "Factory Test"], cwd=host, check=True)
    (host / "README.md").write_text("factory\n", encoding="utf-8")
    (host / "kanban/.gitkeep").write_text("\n", encoding="utf-8")
    (host / "kanban/TASK-1.md").write_text("stale tracked card\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md", "kanban/.gitkeep", "kanban/TASK-1.md"],
        cwd=host,
        check=True,
    )
    subprocess.run(["git", "commit", "-m", "initial"], cwd=host, check=True, capture_output=True)
    script = ROOT / "src/symphony/factory/templates/scripts/factory-setup-worktree.sh"

    result = subprocess.run(
        [resolve_bash(), str(script)],
        cwd=workspace,
        env={**os.environ, "SYMPHONY_WORKFLOW_DIR": str(host)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "nonempty real kanban directory" in result.stderr
    assert not ((workspace / "kanban").is_symlink() or (workspace / "kanban").is_junction())
    assert (workspace / "kanban/.gitkeep").is_file()
    assert (workspace / "kanban/TASK-1.md").is_file()


def test_factory_sync_resolves_all_inferred_skills_before_board_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "project"
    tickets = target / "wayfinder/tickets"
    tickets.mkdir(parents=True)
    tickets.joinpath("design.md").write_text(
        """---
id: design
title: Design the screen
route: GREENFIELD
kind: design
browser: true
---

## Acceptance criteria

- The screen is verified.
""",
        encoding="utf-8",
    )
    skill_root = tmp_path / "installed"
    (skill_root / "supergoal").mkdir(parents=True)
    (skill_root / "supergoal/SKILL.md").write_text("# Supergoal\n", encoding="utf-8")
    monkeypatch.setattr(factory_cli, "_SKILL_SEARCH_ROOTS", (skill_root,))

    assert factory_cli.main(["sync", str(target)]) == 1
    assert not list((target / "kanban").glob("*.md"))


def test_factory_sync_installs_path_safe_custom_skill_from_local_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    skill_root = tmp_path / "installed"
    custom = skill_root / "custom-check"
    custom.mkdir(parents=True)
    (custom / "SKILL.md").write_text("# Custom check\n", encoding="utf-8")
    monkeypatch.setattr(factory_cli, "_SKILL_SEARCH_ROOTS", (skill_root,))
    target = tmp_path / "project"
    assert factory_cli.main(["init", str(target)]) == 0
    ticket = target / "wayfinder/tickets/custom.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text(
        """---
id: custom
title: Run the custom check
route: LEGACY
blocked_by: []
skills: [custom-check]
---

## Acceptance criteria

- The check runs.

## Proof commands

- `pytest`

## Non-goals

- None.
""",
        encoding="utf-8",
    )

    assert factory_cli.main(["sync", str(target)]) == 0

    assert (target / "skills/custom-check/SKILL.md").is_file()
    assert list((target / "kanban").glob("*.md"))


def test_factory_sync_force_recovers_an_incomplete_generated_skill(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    assert factory_cli.main(["init", str(target)]) == 0
    (target / "wayfinder/tickets").mkdir(parents=True)
    (target / "wayfinder/tickets/check.md").write_text(
        """---
id: check
title: Check recovery
route: LEGACY
blocked_by: []
skills: []
---

## Acceptance criteria

- Recovery succeeds.

## Proof commands

- `pytest`

## Non-goals

- None.
""",
        encoding="utf-8",
    )
    broken = target / "skills/supergoal/reference/wayfinder.md"
    broken.unlink()

    assert factory_cli.main(["sync", str(target)]) == 1
    assert factory_cli.main(["sync", str(target), "--force"]) == 0

    assert broken.is_file()


def test_top_level_cli_routes_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    cli_main = importlib.import_module("symphony.cli.main")
    seen: list[str] = []
    monkeypatch.setattr(factory_cli, "main", lambda argv: seen.extend(argv) or 9)
    assert cli_main.main(["factory", "sync", "."]) == 9
    assert seen == ["sync", "."]


def test_init_rejects_unsupported_backend_before_writes(tmp_path: Path) -> None:
    target = tmp_path / "project"
    args = argparse.Namespace(target=str(target), agent_kind="unsupported", force=False)

    assert factory_cli.cmd_init(args) == 1
    assert not target.exists()


@pytest.mark.parametrize("force", [False, True])
def test_init_preserves_directory_conflicts_before_writes(tmp_path: Path, force: bool) -> None:
    target = tmp_path / "project"
    conflict = target / "WORKFLOW.md"
    conflict.mkdir(parents=True)
    sentinel = conflict / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    argv = ["init", str(target)] + (["--force"] if force else [])
    assert factory_cli.main(argv) == 1
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (target / "skills").exists()


@pytest.mark.parametrize("invalid_input", ["dependency", "prefix", "workflow", "acceptance", "proof"])
def test_sync_validates_inputs_before_replacing_skills(
    tmp_path: Path, invalid_input: str,
) -> None:
    target = tmp_path / "project"
    assert factory_cli.main(["init", str(target)]) == 0
    entrypoint = target / "skills/supergoal/SKILL.md"
    entrypoint.write_text("operator version", encoding="utf-8")
    ticket_dir = target / "wayfinder/tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "bad.md").write_text(
        "---\nid: bad\ntitle: Missing dependency\nroute: LEGACY\nblocked_by: [missing]\n---\n"
        "## Acceptance criteria\n- verified\n## Proof commands\n- `pytest`\n## Non-goals\n- none\n",
        encoding="utf-8",
    )

    argv = ["sync", str(target), "--force"]
    if invalid_input != "dependency":
        ticket = ticket_dir / "bad.md"
        ticket.write_text(ticket.read_text(encoding="utf-8").replace("[missing]", "[]"), encoding="utf-8")
    if invalid_input in {"acceptance", "proof"}:
        ticket = ticket_dir / "bad.md"
        content = ticket.read_text(encoding="utf-8")
        content = content.replace("- verified", "") if invalid_input == "acceptance" else content.replace("`pytest`", "manual check")
        ticket.write_text(content, encoding="utf-8")
    if invalid_input == "prefix":
        argv.extend(["--prefix", "../escape"])
    elif invalid_input == "workflow":
        (target / "WORKFLOW.md").write_text("broken workflow", encoding="utf-8")
    assert factory_cli.main(argv) == 1
    assert entrypoint.read_text(encoding="utf-8") == "operator version"
    assert not list((target / "kanban").glob("*.md"))


@pytest.mark.parametrize("directory", ["docs", "skills", "kanban"])
def test_init_rejects_file_in_required_directory_before_writes(
    tmp_path: Path, directory: str,
) -> None:
    target = tmp_path / "project"
    target.mkdir()
    conflict = target / directory
    conflict.write_text("keep", encoding="utf-8")

    assert factory_cli.main(["init", str(target), "--force"]) == 1
    assert conflict.read_text(encoding="utf-8") == "keep"
    assert not (target / "WORKFLOW.md").exists()


def test_factory_cleanup_preserves_shared_host_board_across_restart(tmp_path: Path) -> None:
    host = tmp_path / "project"
    assert factory_cli.main(["init", str(host)]) == 0
    subprocess.run(["git", "init", "-b", "main"], cwd=host, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "factory@example.com"], cwd=host, check=True)
    subprocess.run(["git", "config", "user.name", "Factory Test"], cwd=host, check=True)
    (host / "README.md").write_text("factory\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=host, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=host, check=True, capture_output=True)
    board = host / "kanban"
    expected = {"TASK-1.md": "state: Done\n", "TASK-2.md": "state: Ready\n"}
    for name, content in expected.items():
        (board / name).write_text(content, encoding="utf-8")
    expected_bytes = {path.name: path.read_bytes() for path in board.glob("*.md")}
    unrelated = (host / "README.md").read_bytes()
    root = host / ".symphony/workspaces"
    workspace = root / "TASK-1"
    env = {
        **os.environ, "SYMPHONY_WORKFLOW_DIR": str(host),
        "SYMPHONY_WORKSPACE_ROOT": str(root), "SYMPHONY_PYTHON": sys.executable,
    }
    cfg = build_service_config(load_workflow(host / "WORKFLOW.md"))
    external = tmp_path / "user-data"
    external.mkdir()
    (external / "keep.bin").write_bytes(b"unrelated user data\0\xff")
    external_bytes = (external / "keep.bin").read_bytes()
    for _ in range(2):
        workspace.mkdir(parents=True, exist_ok=True)
        subprocess.run([resolve_bash(), str(host / "scripts/factory-setup-worktree.sh")], cwd=workspace, env=env, check=True)
        assert (workspace / "kanban").resolve() == board.resolve()
        nested = workspace / "node_modules/cache"
        nested.parent.mkdir()
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(nested), str(external)], check=True, capture_output=True)
        else:
            nested.symlink_to(external, target_is_directory=True)
        subprocess.run([resolve_bash(), "-c", cfg.hooks.before_remove], cwd=workspace, env=env, check=True)
        assert not (workspace / ".git").exists()
        assert expected_bytes == {path.name: path.read_bytes() for path in board.glob("*.md")}
        assert (host / "README.md").read_bytes() == unrelated
        assert (external / "keep.bin").read_bytes() == external_bytes


@pytest.mark.parametrize("unsafe_kind", ["real-board", "wrong-link", "outside-root", "workspace-root", "host"])
def test_factory_cleanup_refuses_unsafe_paths_without_removing_data(
    tmp_path: Path, unsafe_kind: str,
) -> None:
    host = tmp_path / "host"
    host.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=host, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "factory@example.com"], cwd=host, check=True)
    subprocess.run(["git", "config", "user.name", "Factory Test"], cwd=host, check=True)
    (host / "README.md").write_text("factory\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=host, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=host, check=True, capture_output=True)
    root = tmp_path / "workspaces"
    workspace = root / "TASK-1"
    subprocess.run(["git", "worktree", "add", "-b", "test", str(workspace)], cwd=host, check=True, capture_output=True)
    board = workspace / "kanban"
    board.mkdir()
    card = board / "keep.md"
    card.write_text("keep", encoding="utf-8")
    if unsafe_kind == "wrong-link":
        external = tmp_path / "external-board"
        board.rename(external)
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(board), str(external)], check=True, capture_output=True)
        else:
            board.symlink_to(external, target_is_directory=True)
    host_board = host / "kanban"
    host_board.mkdir()
    (host_board / "TASK-1.md").write_bytes(b"Done\r\n")
    (host_board / "TASK-2.md").write_bytes(b"Ready\n")
    expected_host = {path.name: path.read_bytes() for path in host_board.iterdir()}
    configured_root = tmp_path / "different-root" if unsafe_kind == "outside-root" else root
    if unsafe_kind == "workspace-root":
        configured_root = workspace
    env = {
        **os.environ, "SYMPHONY_WORKFLOW_DIR": str(workspace if unsafe_kind == "host" else host),
        "SYMPHONY_WORKSPACE_ROOT": str(configured_root),
        "SYMPHONY_PYTHON": sys.executable,
    }
    script = ROOT / "src/symphony/factory/templates/scripts/factory-remove-worktree.sh"
    result = subprocess.run([resolve_bash(), str(script)], cwd=workspace, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert (workspace / ".git").is_file()
    assert card.read_text(encoding="utf-8") == "keep"
    assert expected_host == {path.name: path.read_bytes() for path in host_board.iterdir()}
