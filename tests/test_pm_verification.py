from __future__ import annotations

import subprocess
import shutil
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from symphony.artifacts import ArtifactStore
from symphony.orchestrator.run_registry import RunRegistry
from symphony.issue import Issue
from symphony.pm.models import WbsTask
from symphony.pm.models import ChangeView, SemanticItem
from symphony.pm.coverage import PmCoverageService
from symphony.pm.verification import PmVerificationService
from symphony.workflow.config import ManualVerificationProcedure


def _fixture(tmp_path: Path, *, required: tuple[str, ...] = ("pass", "fail")):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    (repo / "file").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "file"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    registry = RunRegistry(tmp_path / "state.db")
    procedures = {
        "pass": ("/usr/bin/true",), "fail": ("/usr/bin/false",),
        "pass2": ("/bin/echo", "verified"),
        "mutate": (sys.executable, "-c", "from pathlib import Path; Path('file').write_text('changed\\n')"),
    }
    policy = {
        procedure_id: "sha256:" + hashlib.sha256(
            json.dumps(procedures[procedure_id], separators=(",", ":")).encode()
        ).hexdigest()
        for procedure_id in required
    }
    registry.create_pm_handoff_batch(
        handoff_id="h1", operation_id="handoff", project_id="p1", change_id="c1",
        scope_hash="sha256:scope", approval_id="a1", snapshot_id="a1",
        items=({"task_id": "T-ABCDEFGH", "task_revision": "r1",
                "deterministic_ticket_id": "PM-1", "content_hash": "sha256:content",
                "input_hash": "sha256:input", "input_json": "{}",
                "required_procedures_json": json.dumps(policy)},),
    )
    registry.publish_pm_handoff("h1", ticket_identifiers={"T-ABCDEFGH": "PM-1"})
    issue = Issue("PM-1", "PM-1", "Task", "", None, "Done")
    run_id = registry.acquire_run(issue, workspace_path=repo, attempt=None, attempt_kind="initial", agent_kind="codex")
    registry.complete_run(
        issue_id=issue.id, run_id=run_id, status="normal", state="Done",
        commit_sha=sha,
    )
    store = ArtifactStore(tmp_path / "artifacts")
    magic = repo / ".symphony-artifacts"
    magic.mkdir()
    (magic / "report.txt").write_text("verified\n", encoding="utf-8")
    store.collect_from_workspace(repo, identifier="PM-1", run_id=run_id)
    shutil.rmtree(magic)
    service = PmVerificationService(
        registry, procedures,
        artifacts=store, timeout_seconds=2,
    )
    return repo, sha, run_id, registry, store, service


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("normal", "Todo"),
        ("normal", "Cancelled"),
        ("normal", "Blocked"),
        ("turn_limit", "Done"),
        ("cancelled", "Done"),
        ("failed", "Done"),
    ],
)
def test_verification_rejects_non_successful_run_provenance(
    tmp_path: Path, status: str, state: str,
) -> None:
    _repo, _sha, run_id, registry, _store, service = _fixture(tmp_path)
    registry._connect().execute(
        "UPDATE runs SET status = ?, state = ? WHERE run_id = ?",
        (status, state, run_id),
    )
    with pytest.raises(Exception, match="successful terminal"):
        service.run_procedure(
            operation_id="rejected", project_id="p1", ticket_identifier="PM-1",
            task_revision="r1", procedure_id="pass", run_id=run_id,
        )


def test_host_receipt_derives_pass_from_actual_command_result(tmp_path: Path) -> None:
    _repo, _sha, run_id, _, _, service = _fixture(tmp_path)
    passed = service.run_procedure(operation_id="op-pass", project_id="p1", ticket_identifier="PM-1", task_revision="r1", procedure_id="pass", run_id=run_id, artifact_refs=("artifact://PM-1/report.txt",))
    failed = service.run_procedure(operation_id="op-fail", project_id="p1", ticket_identifier="PM-1", task_revision="r1", procedure_id="fail", run_id=run_id)
    assert passed.result == "passed" and passed.return_code == 0
    assert failed.result == "failed" and failed.return_code != 0


def test_artifact_refs_require_host_record_run_and_unchanged_bytes(tmp_path: Path) -> None:
    repo, _sha, run_id, _registry, store, service = _fixture(tmp_path)
    with pytest.raises(Exception, match="does not belong"):
        service.run_procedure(operation_id="wrong-ticket", project_id="p1", ticket_identifier="PM-1", task_revision="r1", procedure_id="pass", run_id=run_id, artifact_refs=("artifact://OTHER/report.txt",))
    with pytest.raises(Exception, match="missing"):
        service.run_procedure(operation_id="missing", project_id="p1", ticket_identifier="PM-1", task_revision="r1", procedure_id="pass", run_id=run_id, artifact_refs=("artifact://PM-1/missing.txt",))
    path = store.resolve_file("PM-1", "report.txt")
    assert path is not None
    path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(Exception, match="no longer match"):
        service.run_procedure(operation_id="tampered", project_id="p1", ticket_identifier="PM-1", task_revision="r1", procedure_id="pass", run_id=run_id, artifact_refs=("artifact://PM-1/report.txt",))


def test_exit_zero_that_mutates_isolated_checkout_is_failed_and_source_unchanged(tmp_path: Path) -> None:
    repo, _sha, run_id, _registry, _store, service = _fixture(tmp_path, required=("mutate",))
    receipt = service.run_procedure(
        operation_id="mutate", project_id="p1", ticket_identifier="PM-1",
        task_revision="r1", procedure_id="mutate", run_id=run_id,
    )
    assert receipt.return_code == 0 and receipt.result == "failed"
    assert (repo / "file").read_text(encoding="utf-8") == "fixture\n"


def test_acceptance_requires_complete_approved_procedure_set_and_human_confirmation(tmp_path: Path) -> None:
    _repo, sha, run_id, _registry, _store, service = _fixture(tmp_path, required=("pass", "fail"))
    passed = service.run_procedure(
        operation_id="pass", project_id="p1", ticket_identifier="PM-1",
        task_revision="r1", procedure_id="pass", run_id=run_id,
    )
    failed = service.run_procedure(
        operation_id="fail", project_id="p1", ticket_identifier="PM-1",
        task_revision="r1", procedure_id="fail", run_id=run_id,
    )
    preview = service.preview_acceptance(
        project_id="p1", ticket_identifier="PM-1", task_revision="r1",
        tested_commit=sha,
    )
    assert not preview.ready and preview.missing_procedure_ids == ("fail",)
    assert passed.receipt_id in preview.passing_receipt_ids
    assert failed.receipt_id not in preview.passing_receipt_ids
    with pytest.raises(Exception, match="incomplete"):
        service.accept(preview=preview, confirmation_token=preview.confirmation_token, reviewer_label="reviewer")

    ready_root = tmp_path / "ready"
    ready_root.mkdir()
    _repo2, sha2, run_id2, registry2, _store2, service2 = _fixture(ready_root, required=("pass",))
    service2.run_procedure(
        operation_id="pass-ready", project_id="p1", ticket_identifier="PM-1",
        task_revision="r1", procedure_id="pass", run_id=run_id2,
    )
    ready = service2.preview_acceptance(
        project_id="p1", ticket_identifier="PM-1", task_revision="r1",
        tested_commit=sha2,
    )
    with pytest.raises(Exception, match="stale"):
        service2.accept(preview=ready, confirmation_token="wrong", reviewer_label="reviewer")
    accepted = service2.accept(
        preview=ready, confirmation_token=ready.confirmation_token,
        reviewer_label="reviewer",
    )
    assert accepted.state == "ACCEPTED"
    assert registry2.get_accepted_pm_commit("p1", "PM-1") == sha2


def test_acceptance_blocks_expired_revoked_owner_until_confirmed_finalization(
    tmp_path: Path,
) -> None:
    repo, sha, run_id, registry, _store, service = _fixture(
        tmp_path, required=("pass",)
    )
    service.run_procedure(
        operation_id="pass-owner", project_id="p1", ticket_identifier="PM-1",
        task_revision="r1", procedure_id="pass", run_id=run_id,
    )
    preview = service.preview_acceptance(
        project_id="p1", ticket_identifier="PM-1", task_revision="r1",
        tested_commit=sha,
    )
    issue = Issue("PM-1", "PM-1", "Task", "", None, "Done")
    owner_run = registry.acquire_run(
        issue,
        workspace_path=repo,
        attempt=1,
        attempt_kind="retry",
        agent_kind="codex",
    )
    assert owner_run is not None
    assert registry.heartbeat(
        issue_id=issue.id,
        run_id=owner_run,
        backend_agent_pid=424242,
    )
    expired = datetime.now(timezone.utc) - timedelta(minutes=5)
    registry._connect().execute(
        "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
        (expired.isoformat(), owner_run),
    )
    registry._connect().execute(
        "UPDATE pm_managed_tickets SET state = 'REVOKED' "
        "WHERE project_id = 'p1' AND ticket_identifier = 'PM-1'"
    )
    assert not registry.has_active_lease("PM-1")
    assert not registry.has_active_pm_dispatch("p1", "PM-1")
    assert registry.has_unresolved_backend_ownership("PM-1")

    with pytest.raises(Exception, match="active native attempt"):
        service.accept(
            preview=preview,
            confirmation_token=preview.confirmation_token,
            reviewer_label="reviewer",
        )

    registry._connect().execute(
        "UPDATE runs SET status = 'reclaiming' WHERE run_id = ?",
        (owner_run,),
    )
    assert registry.finalize_reclaimed_lease(owner_run)
    assert not registry.has_unresolved_backend_ownership("PM-1")
    accepted = service.accept(
        preview=preview,
        confirmation_token=preview.confirmation_token,
        reviewer_label="reviewer",
    )
    assert accepted.state == "ACCEPTED"


def test_accepted_receipt_set_hash_is_independent_of_receipt_order(tmp_path: Path) -> None:
    _repo, sha, run_id, registry, _store, service = _fixture(
        tmp_path, required=("pass", "pass2")
    )
    for procedure_id in ("pass", "pass2"):
        service.run_procedure(
            operation_id=procedure_id, project_id="p1", ticket_identifier="PM-1",
            task_revision="r1", procedure_id=procedure_id, run_id=run_id,
        )
    preview = service.preview_acceptance(
        project_id="p1", ticket_identifier="PM-1", task_revision="r1",
        tested_commit=sha,
    )
    accepted = service.accept(
        preview=preview, confirmation_token=preview.confirmation_token,
        reviewer_label="reviewer",
    )
    registry._connect().execute(
        "UPDATE pm_acceptances SET receipt_ids_json = ? WHERE acceptance_id = ?",
        (json.dumps(list(reversed(accepted.receipt_ids))), accepted.acceptance_id),
    )
    assert registry.get_accepted_pm_commit("p1", "PM-1") == sha


def test_manual_evidence_without_native_run_is_revision_bound_and_requires_acceptance(
    tmp_path: Path,
) -> None:
    repo, sha, _run_id, registry, store, _service = _fixture(tmp_path, required=())
    report = repo / "manual-report.json"
    report.write_text('{"result":"passed"}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "manual-report.json"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "manual evidence"], check=True, capture_output=True)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    definition = {
        "procedure_id": "manual-ux", "kind": "manual",
        "instructions": "Review keyboard behavior", "report": "required",
        "artifacts": "optional",
    }
    definition["definition_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    registry._connect().execute(
        "UPDATE pm_handoff_items SET required_procedures_json=? WHERE deterministic_ticket_id='PM-1'",
        (json.dumps({"manual-ux": definition}),),
    )
    service = PmVerificationService(
        registry, {}, artifacts=store, project_root=repo,
        manual_procedures={"manual-ux": ManualVerificationProcedure("Review keyboard behavior")},
    )
    preview = service.preview_manual_evidence({
        "project_id": "p1", "change_id": "c1", "ticket_identifier": "PM-1",
        "task_revision": "r1", "tested_commit": commit, "procedure_id": "manual-ux",
        "environment_summary": "browser", "started_at": "2026-10-05T00:00:00Z",
        "completed_at": "2026-10-05T00:01:00Z", "result": "passed",
        "report_ref": {"ref_kind": "git_blob", "ref": "manual-report.json"},
        "artifact_refs": [], "submitted_by": "reviewer",
    })
    assert preview.ready
    replay_preview = service.preview_manual_evidence(dict(preview.normalized))
    assert replay_preview.ready and replay_preview.confirmation_token == preview.confirmation_token
    receipt, created = service.confirm_manual_evidence(preview, preview.confirmation_token, "manual-op")
    assert created and receipt.run_id is None and receipt.return_code is None
    assert registry.get_verified_pm_commit("p1", "PM-1") == commit
    assert registry.get_active_pm_acceptance("p1", "PM-1", "r1") is None
    acceptance = service.preview_acceptance(
        project_id="p1", ticket_identifier="PM-1", task_revision="r1", tested_commit=commit
    )
    assert acceptance.ready
    completed: list[str | None] = []
    service.accept(
        preview=acceptance,
        confirmation_token=acceptance.confirmation_token,
        reviewer_label="reviewer",
        complete_ticket=lambda: completed.append(
            registry.get_accepted_pm_commit("p1", "PM-1")
        ),
    )
    assert completed == [commit]
    assert registry.get_accepted_pm_commit("p1", "PM-1") == commit


def test_verified_candidate_requires_complete_mixed_no_run_receipt_set(
    tmp_path: Path,
) -> None:
    repo, _sha, _run_id, registry, store, _service = _fixture(tmp_path, required=())
    report = repo / "manual-report.json"
    report.write_text('{"result":"passed"}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "manual-report.json"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "mixed evidence"],
        check=True, capture_output=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()

    native = {"procedure_id": "pass", "kind": "native", "argv": ["/usr/bin/true"]}
    manual = {
        "procedure_id": "manual-ux", "kind": "manual",
        "instructions": "Review keyboard behavior", "report": "required",
        "artifacts": "optional",
    }
    for definition in (native, manual):
        definition["definition_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    registry._connect().execute(
        "UPDATE pm_handoff_items SET required_procedures_json=? "
        "WHERE deterministic_ticket_id='PM-1'",
        (json.dumps({"pass": native, "manual-ux": manual}),),
    )
    service = PmVerificationService(
        registry, {"pass": ("/usr/bin/true",)}, artifacts=store,
        project_root=repo,
        manual_procedures={
            "manual-ux": ManualVerificationProcedure("Review keyboard behavior")
        },
    )

    common = {
        "project_id": "p1", "change_id": "c1", "ticket_identifier": "PM-1",
        "task_revision": "r1", "tested_commit": commit,
        "environment_summary": "local", "started_at": "2026-10-05T00:00:00Z",
        "completed_at": "2026-10-05T00:01:00Z", "submitted_by": "reviewer",
    }
    native_preview = service.preview_manual_evidence({
        **common, "procedure_id": "pass", "result": "",
        "report_ref": None, "artifact_refs": [],
    })
    service.confirm_manual_evidence(
        native_preview, native_preview.confirmation_token, "native-no-run"
    )
    assert registry.get_verified_pm_commit("p1", "PM-1") is None
    manual_preview = service.preview_manual_evidence({
        **common, "procedure_id": "manual-ux", "result": "passed",
        "report_ref": {"ref_kind": "git_blob", "ref": "manual-report.json"},
        "artifact_refs": [],
    })
    service.confirm_manual_evidence(
        manual_preview, manual_preview.confirmation_token, "manual-no-run"
    )
    assert registry.get_verified_pm_commit("p1", "PM-1") == commit
    acceptance = service.preview_acceptance(
        project_id="p1", ticket_identifier="PM-1",
        task_revision="r1", tested_commit=commit,
    )
    assert acceptance.ready and len(acceptance.passing_receipt_ids) == 2
    assert registry.get_active_pm_acceptance("p1", "PM-1", "r1") is None
    coverage = PmCoverageService(registry).build(
        ChangeView(
            "p1", "c1", "change", "rev", "scope", "Validated", "", "",
            semantic_index=(SemanticItem(
                "SC-MIXED", "scenario", "Mixed", "spec:requirements",
                "specs/requirements/spec.md", 1,
            ),),
        ),
        (WbsTask(
            "T-ABCDEFGH", None, 10, "Feature", "Manual implementation",
            requirement_refs=("SC-MIXED",),
        ),),
    )
    link = coverage.entities[0].task_links[0]
    assert link.implemented and link.implementation_commit == commit
    assert link.tested and link.test_provenance == "mixed"
    assert not link.accepted


def test_acceptance_refuses_while_native_attempt_is_active(tmp_path: Path) -> None:
    _repo, sha, run_id, registry, _store, service = _fixture(tmp_path, required=("pass",))
    service.run_procedure(
        operation_id="pass", project_id="p1", ticket_identifier="PM-1",
        task_revision="r1", procedure_id="pass", run_id=run_id,
    )
    preview = service.preview_acceptance(
        project_id="p1", ticket_identifier="PM-1", task_revision="r1", tested_commit=sha
    )
    issue = Issue("PM-1", "PM-1", "Task", "", None, "In Progress")
    active = registry.acquire_run(
        issue, workspace_path=tmp_path / "repo", attempt=2,
        attempt_kind="retry", agent_kind="codex",
    )
    assert active is not None
    with pytest.raises(Exception, match="active native attempt"):
        service.accept(
            preview=preview, confirmation_token=preview.confirmation_token,
            reviewer_label="reviewer",
        )


def test_manual_evidence_confirm_rechecks_current_policy(tmp_path: Path) -> None:
    repo, _sha, _run_id, registry, store, _service = _fixture(tmp_path, required=())
    report = repo / "manual-report.json"
    report.write_text('{"result":"passed"}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "manual-report.json"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "policy evidence"],
        check=True, capture_output=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    definition = {
        "procedure_id": "manual-ux", "kind": "manual",
        "instructions": "Review keyboard behavior", "report": "required",
        "artifacts": "optional",
    }
    definition["definition_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    registry._connect().execute(
        "UPDATE pm_handoff_items SET required_procedures_json=? "
        "WHERE deterministic_ticket_id='PM-1'",
        (json.dumps({"manual-ux": definition}),),
    )
    service = PmVerificationService(
        registry, {}, artifacts=store, project_root=repo,
        manual_procedures={
            "manual-ux": ManualVerificationProcedure("Review keyboard behavior")
        },
    )
    preview = service.preview_manual_evidence({
        "project_id": "p1", "change_id": "c1", "ticket_identifier": "PM-1",
        "task_revision": "r1", "tested_commit": commit,
        "procedure_id": "manual-ux", "environment_summary": "browser",
        "started_at": "2026-10-05T00:00:00Z",
        "completed_at": "2026-10-05T00:01:00Z", "result": "passed",
        "report_ref": {"ref_kind": "git_blob", "ref": "manual-report.json"},
        "artifact_refs": [], "submitted_by": "reviewer",
    })
    registry._connect().execute(
        "UPDATE pm_handoff_items SET required_procedures_json='{}' "
        "WHERE deterministic_ticket_id='PM-1'"
    )
    with pytest.raises(Exception, match="stale"):
        service.confirm_manual_evidence(
            preview, preview.confirmation_token, "policy-drift"
        )


@pytest.mark.timing
def test_manual_git_timeout_reaps_process_and_repair_resolves_same_immutable_blob(tmp_path, monkeypatch):
    import os
    import time
    import symphony.pm.verification as verification
    from symphony.pm.models import PmError

    _repo, commit, _run_id, registry, _store, service = _fixture(tmp_path)
    service.project_root = _repo
    service.timeout_seconds = .5
    pid_file = tmp_path / "git-child.pid"
    original = subprocess.run
    def slow_git(argv, *args, **kwargs):
        if argv[0] == "git" and "show" in argv:
            child = [sys.executable, "-c", "import os,time; from pathlib import Path; Path(" + repr(str(pid_file)) + ").write_text(str(os.getpid())); time.sleep(60)"]
            return original(child, *args, **kwargs)
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(verification.subprocess, "run", slow_git)
    ref = {"ref_kind": "git_blob", "ref": "file"}
    try:
        started = time.monotonic()
        with pytest.raises(PmError) as failure:
            service._resolve_ref("PM-1", commit, ref, report=True)
        assert failure.value.code == "invalid_evidence_ref" and failure.value.status == 409
        assert failure.value.message == "Git evidence blob is unavailable"
        assert time.monotonic() - started < 2
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        monkeypatch.setattr(verification.subprocess, "run", original)
        recovered = service._resolve_ref("PM-1", commit, ref, report=True)
        assert recovered["sha256"] == "sha256:" + hashlib.sha256(b"fixture\n").hexdigest()
        assert recovered["size_bytes"] == len(b"fixture\n")
    finally:
        registry.close()
