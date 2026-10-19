from __future__ import annotations

from pathlib import Path
import json
import subprocess

import pytest

from symphony.pm.identity import host_project_identity

from symphony.pm.models import (
    PmError,
    PmFinding,
    PmRequestContext,
    PmResult,
)


def test_request_context_requires_identity_and_optional_cas():
    ctx = PmRequestContext.from_json(
        {
            "project_id": "project-1",
            "expected_revision": "sha256:old",
            "client_operation_id": "op-1",
        },
        change_id="change-1",
        require_revision=True,
        require_operation=True,
    )
    assert ctx == PmRequestContext("project-1", "change-1", "sha256:old", "op-1")

    with pytest.raises(PmError) as exc:
        PmRequestContext.from_json({"project_id": "project-1"}, change_id="change-1", require_revision=True)
    assert exc.value.code == "revision_conflict"
    assert exc.value.status == 409


def test_result_json_contract_is_stable_and_nested_findings_are_json_safe():
    result = PmResult(
        project_id="project-1",
        change_id="change-1",
        previous_revision="sha256:old",
        revision="sha256:new",
        changed=("tasks.md",),
        findings=(PmFinding("invalid_dependency", "missing T-2", item_id="T-1"),),
        impacted_ids=("REQ-1",),
        approval_invalidated=True,
        operation_id="op-1",
    )

    assert result.as_json() == {
        "project_id": "project-1",
        "change_id": "change-1",
        "previous_revision": "sha256:old",
        "revision": "sha256:new",
        "changed": ["tasks.md"],
        "findings": [
            {
                "code": "invalid_dependency",
                "message": "missing T-2",
                "severity": "error",
                "document_id": None,
                "item_id": "T-1",
                "field": None,
                "path": [],
                "fix": None,
            }
        ],
        "impacted_ids": ["REQ-1"],
        "approval_invalidated": True,
        "operation_id": "op-1",
    }


def test_registered_host_identity_wins_over_duplicate_tracker_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = tmp_path / "projects.json"
    workflow_a = tmp_path / "a" / "WORKFLOW.md"
    workflow_b = tmp_path / "b" / "WORKFLOW.md"
    workflow_a.parent.mkdir()
    workflow_b.parent.mkdir()
    # Separate repositories: inside an enclosing checkout both directories
    # would otherwise resolve to the same Git common dir and the registry
    # rejects them as one repository registered twice.
    for directory in (workflow_a.parent, workflow_b.parent):
        subprocess.run(["git", "init", "-q", str(directory)], check=True)
    projects.write_text(json.dumps({"version": 1, "projects": [
        {"id": "registered-a", "name": "A", "git_repo": str(workflow_a.parent), "workflow": str(workflow_a), "host": "127.0.0.1", "port": 10001},
        {"id": "registered-b", "name": "B", "git_repo": str(workflow_b.parent), "workflow": str(workflow_b), "host": "127.0.0.1", "port": 10002},
    ]}), encoding="utf-8")
    monkeypatch.setenv("SYMPHONY_PROJECTS_FILE", str(projects))
    assert host_project_identity(workflow_a, standalone_id="same-slug") == "registered-a"
    assert host_project_identity(workflow_b, standalone_id="same-slug") == "registered-b"


def test_unreadable_host_registry_does_not_fall_back_to_tracker_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from symphony.pm.models import PmError

    projects = tmp_path / "projects.json"
    monkeypatch.setenv("SYMPHONY_PROJECTS_FILE", str(projects))
    workflow = tmp_path / "WORKFLOW.md"
    assert host_project_identity(workflow, standalone_id="slug") == "slug"
    projects.write_text("{not json", encoding="utf-8")
    with pytest.raises(PmError) as exc:
        host_project_identity(workflow, standalone_id="slug")
    assert exc.value.code == "project_identity_unavailable"
