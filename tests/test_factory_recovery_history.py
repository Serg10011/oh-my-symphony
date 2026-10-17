"""Resolved factory diagnostics follow host delivery, preserving raw card bytes."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import symphony.orchestrator.core as core
from symphony.orchestrator import Orchestrator
from symphony.trackers.file import FileBoardTracker
from symphony.utils.auto_merge import AutoMergeResult
from symphony.workflow import WorkflowState
from tests.test_orchestrator_dispatch import _make_config

GIT_NOTE = """Git history snapshot failed; workspace preserved; operator action required.

The host could not record this workspace in Git. Delivery and cleanup were stopped, and the workspace and its product files were preserved.

- status: `commit_failed`
- classification: `unknown`
- workspace: `/owned/workspace/TASK-1`
- detail: index.lock exists

Fix the reported Git/index problem without deleting the workspace. Then return this ticket to its verification lane and resume it through the operator resume control. Do not treat this ticket as delivered."""
TOKEN_NOTE = """max_total_tokens reached (664618/600000 in verify); worker cancelled before delivery; workspace preserved; operator action required.

The worker wrote Done, but token-budget cancellation stopped delivery. Delivery and cleanup were stopped, and the workspace and its product files were preserved. Do not treat this ticket as delivered.

- workspace: `/owned/workspace/TASK-1`

Fix the token-budget cause without deleting the workspace. Check `agent.max_total_tokens` and `agent.max_total_tokens_by_state`. Then return this ticket to Verify and resume it through the operator resume control so verification can run again before delivery."""
RESOLVED = (
    "## Resolved Blocker (history)\n\n"
    "Resolved after durable target delivery. The original diagnostic below is historical."
)


def _setup(tmp_path, note=GIT_NOTE, newline="\n", state="Done", profile="factory"):
    cfg = _make_config(tracker_kind="file", workflow_path=tmp_path / "WORKFLOW.md")
    cfg = replace(
        cfg,
        tracker=replace(cfg.tracker, board_root=tmp_path / "kanban"),
        agent=replace(cfg.agent, auto_merge_on_done=True, auto_merge_push_target=False),
        raw={"contract_profile": profile},
    )
    card = tmp_path / "kanban/TASK-1.md"
    card.parent.mkdir()
    text = (
        f"---\n# Keep this YAML comment and ordering\ntitle: 'Original title'\n"
        f"state: {state}\nid: TASK-1\nlabels: [factory]\n---\n\n"
        "## Operator notes\n\nKeep  two spaces.\n\n"
        "```markdown\n## Blocker\nExample only.\n```\n\n"
        f"## Blocker\n\n{note}\n\n"
        "## Blocker\n\nOperator's unrelated warning.\n\n"
        "## Verification\n\nProof retained.\n\n\n"
    )
    raw = text.replace("\n", newline).encode()
    card.write_bytes(raw)
    tracker = FileBoardTracker(cfg.tracker)
    issue = tracker.fetch_issue_full_by_id("TASK-1")
    assert issue is not None
    orch = Orchestrator(WorkflowState(cfg.workflow_path))
    return orch, cfg, issue, card, raw


@pytest.mark.parametrize("note", [GIT_NOTE, TOKEN_NOTE])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("status", ["merged", "nothing_to_apply"])
async def test_host_delivery_resolves_only_owned_blocker_bytes(
    tmp_path, monkeypatch, note, newline, status
):
    orch, cfg, issue, card, raw = _setup(tmp_path, note, newline)
    monkeypatch.setattr(core, "auto_merge_on_done_best_effort", AsyncMock(return_value=AutoMergeResult(True, status)))
    assert await orch._auto_merge_done_gate_or_block(cfg, issue, tmp_path, debug_target=None)
    old = ("## Blocker\n\n" + note).replace("\n", newline).encode()
    new = (RESOLVED + "\n\n" + note).replace("\n", newline).encode()
    assert card.read_bytes() == raw.replace(old, new, 1)
    once = card.read_bytes()
    assert await orch._auto_merge_done_gate_or_block(cfg, issue, tmp_path, debug_target=None)
    assert card.read_bytes() == once


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("detail_heading", [
    "# Hook output", "## Hook output", "```markdown\n## Blocker\n```",
], ids=["h1", "h2", "fenced-blocker"])
async def test_host_delivery_resolves_owned_multiline_diagnostics_without_changing_other_bytes(
    tmp_path, monkeypatch, newline, detail_heading
):
    note = GIT_NOTE.replace("index.lock exists", f"pre-commit failed\n{detail_heading}\nvalidation failed")
    orch, cfg, issue, card, raw = _setup(tmp_path, note, newline)
    old = ("## Blocker\n\n" + note).replace("\n", newline).encode()
    verification = "## Verification".encode()
    raw = raw.replace(verification, old + newline.encode() * 2 + verification)
    card.write_bytes(raw)
    monkeypatch.setattr(core, "auto_merge_on_done_best_effort", AsyncMock(return_value=AutoMergeResult(True, "merged")))
    assert await orch._auto_merge_done_gate_or_block(cfg, issue, tmp_path, debug_target=None)
    new = (RESOLVED + "\n\n" + note).replace("\n", newline).encode()
    assert card.read_bytes() == raw.replace(old, new)
    once = card.read_bytes()
    assert await orch._auto_merge_done_gate_or_block(cfg, issue, tmp_path, debug_target=None)
    assert card.read_bytes() == once


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("first_note", [
    GIT_NOTE,
    GIT_NOTE.replace("index.lock exists", "pre-commit failed\n## Hook output\nvalidation failed"),
    GIT_NOTE.replace("index.lock exists", "pre-commit failed\n```markdown\n## Hook output\n```\nvalidation failed"),
    TOKEN_NOTE,
], ids=["git", "git-heading", "git-fence", "token"])
@pytest.mark.parametrize("wrapper", [
    "{}\n\nOperator additional warning.",
    "Operator additional warning.\n\n{}",
    "````markdown\n## Blocker\n\n{}\n````",
], ids=["suffix", "prefix", "fenced"])
@pytest.mark.parametrize("owned_note", [GIT_NOTE, TOKEN_NOTE], ids=["git", "token"])
async def test_delivery_preserves_modified_diagnostic_before_later_owned_note(
    tmp_path, monkeypatch, newline, first_note, wrapper, owned_note
):
    orch, cfg, issue, card, raw = _setup(tmp_path, note=wrapper.format(first_note), newline=newline)
    later = "## Operator decision\n\nKeep this active.\n\n## Blocker\n\n" + owned_note + "\n\n"
    raw = raw.replace(b"## Verification", later.replace("\n", newline).encode() + b"## Verification")
    card.write_bytes(raw)
    monkeypatch.setattr(core, "auto_merge_on_done_best_effort", AsyncMock(return_value=AutoMergeResult(True, "merged")))
    assert await orch._auto_merge_done_gate_or_block(cfg, issue, tmp_path, debug_target=None)
    old = ("## Blocker\n\n" + owned_note + "\n\n").replace("\n", newline).encode()
    new = (RESOLVED + "\n\n" + owned_note + "\n\n").replace("\n", newline).encode()
    expected = new.join(raw.rsplit(old, 1))
    assert card.read_bytes() == expected
    assert await orch._auto_merge_done_gate_or_block(cfg, issue, tmp_path, debug_target=None)
    assert card.read_bytes() == expected


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("detail", [
    "hook quoted a recovery footer:\n\n" + GIT_NOTE.rsplit("\n\n", 1)[1] + "\n## Hook output\nvalidation failed",
    "pre-commit failed\n```markdown\n## Hook output\nvalidation failed",
    "pre-commit failed\n## Blocker\nvalidation failed",
], ids=["embedded-footer", "unclosed-fence", "blocker-in-detail"])
async def test_ambiguous_diagnostic_boundaries_preserve_the_card(tmp_path, monkeypatch, newline, detail):
    orch, cfg, issue, card, raw = _setup(tmp_path, note=GIT_NOTE.replace("index.lock exists", detail), newline=newline)
    monkeypatch.setattr(core, "auto_merge_on_done_best_effort", AsyncMock(return_value=AutoMergeResult(True, "merged")))
    assert await orch._auto_merge_done_gate_or_block(cfg, issue, tmp_path, debug_target=None)
    assert card.read_bytes() == raw


@pytest.mark.parametrize("result", [None, AutoMergeResult(False, "push_failed"), AutoMergeResult(False, "dirty_overlap")])
async def test_absent_or_failed_delivery_keeps_active_diagnostic(tmp_path, monkeypatch, result):
    orch, cfg, issue, card, raw = _setup(tmp_path)
    monkeypatch.setattr(core, "auto_merge_on_done_best_effort", AsyncMock(return_value=result))
    monkeypatch.setattr(orch, "_block_done_ticket_for_merge_gate", AsyncMock())
    await orch._auto_merge_done_gate_or_block(cfg, issue, tmp_path, debug_target=None)
    assert card.read_bytes() == raw


@pytest.mark.parametrize("profile,state,note", [("advanced", "Done", GIT_NOTE), ("factory", "Verify", GIT_NOTE), ("factory", "Done", "Operator action required.\n\n" + GIT_NOTE)])
async def test_other_profiles_states_and_user_blocker_untouched(tmp_path, monkeypatch, profile, state, note):
    orch, cfg, issue, card, raw = _setup(tmp_path, note=note, state=state, profile=profile)
    monkeypatch.setattr(core, "auto_merge_on_done_best_effort", AsyncMock(return_value=AutoMergeResult(True, "merged")))
    await orch._auto_merge_done_gate_or_block(cfg, issue, tmp_path, debug_target=None)
    assert card.read_bytes() == raw


@pytest.mark.parametrize("wrapper", ["Operator's warning.\n\n{}", "{}\n\nOperator's warning."])
async def test_multiline_diagnostic_with_operator_text_stays_untouched(tmp_path, monkeypatch, wrapper):
    note = GIT_NOTE.replace("index.lock exists", "pre-commit failed\n## Hook output\nvalidation failed")
    orch, cfg, issue, card, raw = _setup(tmp_path, note=wrapper.format(note))
    monkeypatch.setattr(core, "auto_merge_on_done_best_effort", AsyncMock(return_value=AutoMergeResult(True, "merged")))
    assert await orch._auto_merge_done_gate_or_block(cfg, issue, tmp_path, debug_target=None)
    assert card.read_bytes() == raw


async def test_disabled_merge_and_fenced_diagnostic_stay_untouched(tmp_path, monkeypatch):
    note = "```markdown\n## Blocker\n\n" + GIT_NOTE + "\n```"
    orch, cfg, issue, card, raw = _setup(tmp_path, note=note)
    merge = AsyncMock(return_value=AutoMergeResult(True, "merged"))
    monkeypatch.setattr(core, "auto_merge_on_done_best_effort", merge)
    disabled = replace(cfg, agent=replace(cfg.agent, auto_merge_on_done=False))
    assert await orch._auto_merge_done_gate_or_block(disabled, issue, tmp_path, debug_target=None)
    merge.assert_not_awaited()
    assert card.read_bytes() == raw
    assert await orch._auto_merge_done_gate_or_block(cfg, issue, tmp_path, debug_target=None)
    assert card.read_bytes() == raw


@pytest.mark.parametrize("clean,push_target", [(True, False), (False, False), (True, True)])
async def test_startup_already_merged_requires_clean_workspace(tmp_path, monkeypatch, clean, push_target):
    orch, cfg, issue, card, raw = _setup(tmp_path)
    cfg = replace(cfg, agent=replace(cfg.agent, auto_merge_push_target=push_target))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SimpleNamespace(path_for=lambda _: workspace, remove=AsyncMock())
    orch._workspace_manager = manager  # type: ignore[assignment]
    monkeypatch.setattr(orch, "_tracker_call_terminal_issues", lambda _: [issue])
    monkeypatch.setattr(orch, "_startup_release_terminal_guard", AsyncMock(return_value=(issue, False, False)))
    monkeypatch.setattr(core, "_branch_already_merged_into_target", AsyncMock(return_value=True))
    monkeypatch.setattr(core, "_worktree_status_snapshot", lambda _: [] if clean else [" M product.py"])
    monkeypatch.setattr(orch, "_preserve_history_failure", AsyncMock())
    await orch._startup_terminal_cleanup(cfg)
    if clean and not push_target:
        assert b"## Resolved Blocker (history)" in card.read_bytes()
        manager.remove.assert_awaited_once()
    else:
        assert card.read_bytes() == raw
        if clean:
            manager.remove.assert_awaited_once()
        else:
            manager.remove.assert_not_awaited()
