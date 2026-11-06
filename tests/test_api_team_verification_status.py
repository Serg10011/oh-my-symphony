"""A finished paid run can wait for a free check without looking stopped."""
from dataclasses import replace
import json

import pytest

from symphony import api_team
from symphony.orchestrator import Orchestrator
from symphony.webapi.payloads import _public_schedule_reason
from tests.test_api_team import board_dir, workflow  # noqa: F401
from tests.test_orchestrator_max_retries import _entry_for_exit


@pytest.fixture
def verify_board(request):
    service = request.getfixturevalue("workflow")
    path = service.current().workflow_path
    path.write_text(path.read_text().replace("/absolute/trusted/runner", "/absolute/api_board.py --settings /local/settings"))
    cfg, error = service.reload()
    assert not error
    orch = Orchestrator(service)
    entry = _entry_for_exit("MT-1")
    entry.issue = replace(entry.issue, agent_kind="api-team", state="Verify")
    entry.agent_kind = "api-team"
    record = path.parent / ".ours-state/api-team/MT-1/assignment.json"
    record.parent.mkdir(parents=True)
    def save(phase=None, status="result"):
        data = {"identifier": "MT-1", "status": status, "check_lane": "Verify",
                "candidate": {"status": "awaiting_full_checks"}}
        if phase:
            data["cloud_seen"] = {"phase": phase}
        record.write_text(json.dumps(data))
        record.chmod(0o600)
    save()
    return orch, cfg, entry, save


@pytest.mark.asyncio
async def test_finished_run_shows_the_external_check_and_never_becomes_dispatchable(verify_board):
    orch, cfg, entry, save = verify_board
    issue = entry.issue
    orch._running[issue.id] = entry
    await orch._on_worker_exit(issue.id, "api_team_finished", None)
    for phase, label in [(None, "Ожидает проверки"), ("queued", "Проверка в очереди"),
                         ("running", "Проверяется"), ("waiting", "Ожидает результата"),
                         ("interrupted", "Ожидает проверки")]:
        save(phase)
        badge = orch.issue_attention(issue)
        assert badge["label"] == label and badge["severity"] == "info"
        decision = orch._eligibility_ownership_decision(issue, cfg, owning_retry=False)
        assert decision.code == badge["kind"] and _public_schedule_reason(decision.code) == badge["message"]
        assert orch.is_paused(issue.id) and not api_team.confirmed_in_queue(cfg, issue.identifier)
    save(status="stopped")
    assert orch.issue_attention(issue)["kind"] == "paused"


@pytest.mark.asyncio
async def test_manual_pause_keeps_its_reason_even_when_the_paid_worker_exits(verify_board):
    orch, cfg, entry, save = verify_board
    issue = entry.issue
    orch._paused_issue_ids.add(issue.id)
    orch._pause_reasons[issue.id] = "operator pause"
    orch._running[issue.id] = entry
    save("running")
    await orch._on_worker_exit(issue.id, "cancelled", None)
    assert orch.issue_attention(issue)["message"] == "operator pause"
    assert orch._eligibility_ownership_decision(issue, cfg, owning_retry=False).code == "paused"
