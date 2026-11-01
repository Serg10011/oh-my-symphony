"""A subtask without Document and the light path, end to end (TASK-S4-16).

A subtask from start to Done on a real git and a real bash (the rendered
after_run of the template, as in test_integrate.py), and the prompt templates
rendered by the core engine. The check.py cases are in test_light_path.py.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import test_check as tc
import test_integrate as ti

check = tc.check
pc = ti.pc

pytestmark = pytest.mark.skipif(os.name != "posix", reason="check.py is POSIX-only")

SKIPPED_LINE = "tests skipped: only doc paths changed"


@pytest.fixture
def henv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ti.Env:
    """test_integrate's board with integration on, and its fake symphony."""
    for key in [k for k in os.environ if k.startswith("SYMPHONY_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("SYMPHONY_WORKFLOW_DIR", str(tmp_path / "projects" / "demo"))
    monkeypatch.setenv("SYMPHONY_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("SYMPHONY_PYTHON", sys.executable)
    board = ti.Env(tmp_path, enabled=True)
    ti.point_to_symphony(board)
    return board


SUBTASK_BODY = """\
## QA Evidence

- Full suite: run by the host hook (check.py hook) after Document

## Merge Status

Merge deferred to a human by pipeline rule; no merge performed.
"""

# ---------------------------------------------------------------------------
# end to end: a subtask on a real git and a real bash
# ---------------------------------------------------------------------------

SUBTASK = "OMS-12"


def test_a_docs_only_subtask_goes_to_done_without_tests_and_the_gate_agrees(
    henv: ti.Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from symphony.workflow import build_service_config, load_workflow

    board, marker = henv.board, henv.tmp / "tests-ran"
    ti.write(board / "test.sh", f"touch {marker}\necho 'PIPELINE_TESTS passed=1 skipped=0 failed=0'\n")
    toml = board / "project.toml"
    toml.write_text(toml.read_text() + '[checks]\ndoc_paths = ["docs/our/**/*.md"]\n')
    ti.write(board / ".ours-state" / "baseline.json", json.dumps({
        "base_branch": "main", "base_sha": "0" * 40, "passed": 1, "skipped": 0, "failed": 0,
        "setup_s": 0.1, "tests_s": 0.1, "measured_at": "2026-10-26T00:00:00Z",
    }))
    henv.card(SUBTASK, ti.card_text())
    assert henv.start(SUBTASK, monkeypatch) == 0
    ws = henv.ws(SUBTASK)
    ti.write(ws / "docs" / "our" / "NOTE.md", "text\n")
    # The agent's last turn: Verify has set Human Review; no Wiki Updates, no Document.
    henv.card(SUBTASK, "\n".join([
        "---", f"id: {SUBTASK}", "title: Add notes", "state: Human Review",
        "labels: [role:worker]", f"request: {ti.TASK}", "---", "", SUBTASK_BODY,
    ]))
    ti.write(board / "WORKFLOW.md", pc.render_workflow(board, pc.load_project(board)))
    after_run = build_service_config(load_workflow(board / "WORKFLOW.md")).hooks.after_run

    run_env = {k: v for k, v in os.environ.items() if not k.startswith("SYMPHONY_")}
    run_env.update(SYMPHONY_PYTHON=sys.executable, SYMPHONY_WORKFLOW_DIR=str(board))
    proc = subprocess.run(["/bin/bash", "-c", after_run], cwd=ws, env=run_env, capture_output=True,
                          text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr

    assert not marker.exists(), "the full test run must not start"
    assert SKIPPED_LINE in proc.stdout
    assert ti.card_state(henv, SUBTASK) == "Done"
    state_dir = board / ".ours-state"
    log = [json.loads(line) for line in (state_dir / "check.log").read_text().splitlines()]
    hook = [e for e in log if e["mode"] == "hook"]
    assert [(e["outcome"], e.get("tests")) for e in hook] == [("pass", "skipped-docs")]
    (merged,) = [e for e in log if e["mode"] == "integrate"]
    assert merged["outcome"] == "done"
    assert not (state_dir / f"{SUBTASK}.pass.json").exists()  # used up by integrate.py
    assert "docs/our/NOTE.md" in ti.git(henv.code, "ls-tree", "-r", "--name-only", ti.BRANCH)

    # The core now runs the done gate: the merged record makes it a skip.
    gate = subprocess.run([sys.executable, str(Path(check.__file__)), "gate"], cwd=ws, env=run_env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert "tests skipped: tree " in gate.stdout
    assert not marker.exists()
    gate_event = [json.loads(line) for line in (state_dir / "check.log").read_text().splitlines()][-1]
    assert gate_event["mode"] == "gate" and gate_event["tests"] == "skipped"


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

PROMPTS = Path(ti.__file__).resolve().parents[1] / "prompts"


def render_prompt(name: str, *, labels: tuple[str, ...], request: str | None) -> str:
    from symphony.issue import Issue
    from symphony.prompt import build_prompt_env, render

    issue = Issue(id="OMS-12", identifier="OMS-12", title="Add notes", description="d",
                  priority=None, state="Verify", labels=labels, request=request)
    values = build_prompt_env(issue, None, auto_merge_on_done=False, max_attempts=2)
    return render((PROMPTS / name).read_text(encoding="utf-8"), values)


@pytest.mark.parametrize(
    ("labels", "request_id"), [(("role:worker",), "OMS-7"), ((), None), (("bug",), None)]
)
def test_the_prompts_render_with_and_without_the_worker_label(
    labels: tuple[str, ...], request_id: str | None
) -> None:
    base = render_prompt("base.md", labels=labels, request=request_id)
    verify = render_prompt("stages/verify.md", labels=labels, request=request_id)
    document = render_prompt("stages/document.md", labels=labels, request=request_id)
    # base.md shows the labels the Verify branch reads, and only when there are some.
    assert ("Labels: role:worker" in base) is bool(labels and labels[0] == "role:worker")
    assert "subtask (integration enabled, role:worker + request): Verify -> Merging -> Done (host, no Document)" in base
    assert "a subtask goes from Verify straight to `Merging`" in base
    assert "(a subtask needs only the first two" in base
    # verify.md: one text for every card, the branch is chosen by the labels and `request:`.
    assert "**Subtask** (the `Labels:` line of the ticket shows `role:worker` AND the ticket has a `request:` field)" in verify
    assert "`## Document Skipped` with the one line `Subtask: documentation is written by the task card`" in verify
    assert "Any other ticket: set state to `Document`." in verify
    # document.md: no self-fixing of small inaccuracies.
    assert "Do not fix them yourself" in document
    assert "fix them yourself when" not in document


def test_the_template_engine_has_no_label_membership_condition() -> None:
    """Why verify.md reads the `Labels:` line instead of a template branch."""
    from symphony.errors import TemplateParseError, TemplateRenderError
    from symphony.prompt import build_prompt_env, render
    from symphony.issue import Issue

    issue = Issue(id="x", identifier="x", title="t", description=None, priority=None,
                  state="Verify", labels=("role:worker",))
    values = build_prompt_env(issue, None)
    with pytest.raises((TemplateParseError, TemplateRenderError)):
        render("{% if issue.labels contains 'role:worker' %}yes{% endif %}", values)
