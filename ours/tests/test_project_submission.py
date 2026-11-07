"""The project-chat capability submits exact cards but cannot grant spending authority."""
import json
import asyncio
import time
import io
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
import pytest

import test_project_connect as fixtures
from test_api_session import KEYS

import api_board as board_api
import api_board_permission as permission_api
import project_connect as connect
from symphony import api_team
from symphony.cli import board as board_cli
from symphony.workflow import WorkflowState

host, connector, simulated_service = fixtures.host, fixtures.connector, fixtures.simulated_service
authorized = fixtures.authorized


def world(host, monkeypatch):
    root, raw = host
    grant = authorized(root, raw)
    connect.connect(raw, root, grant)
    folder = root / raw["project"]
    workflow = folder / "WORKFLOW.md"
    config, error = WorkflowState(workflow).reload()
    assert config and not error
    context = api_team.board_context(config, "api-team", "")
    permission = permission_api.Permission(folder / "api-settings.json", context)
    monkeypatch.setattr(connect, "verify_instance", lambda *args: None)
    monkeypatch.setattr(board_api, "keychain_credentials", lambda _: pytest.fail("No actual keys"))
    return root, raw, grant, folder, permission


def new_card(folder, identifier="SAMPLE-1"):
    assert board_cli.main(["new", "--workflow", str(folder / "WORKFLOW.md"), identifier, "Полезная задача"]) == 0


def test_client_reads_signed_task_result_larger_than_request_limit(host, connector, monkeypatch):
    root, raw = host
    authorized(root, raw)
    credential = root / ".connections/sample.token"
    token = json.loads(connect.read_private(credential))["token"]
    result = {"status": "result", "task": "Описание выполненной работы. " * 2000}
    body = json.dumps(result, ensure_ascii=False).encode()
    assert connect.MAX_REQUEST < len(body) < connect.MAX_REPLY
    def reply(request, **kwargs):
        stream = io.BytesIO(body)
        stream.status = 200
        challenge = request.get_header("X-symphony-challenge")
        stream.headers = {"X-Symphony-Proof": connect.signature(token, "reply:task", challenge, body)}
        return stream
    monkeypatch.setattr(connector.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=reply))
    data = json.dumps({"project": "sample", "identifier": "SAMPLE-1"}).encode()
    assert connector.client(8792, credential, data, "task") == result
    monkeypatch.setattr(connect, "MAX_REPLY", 64)
    with pytest.raises(connect.ConnectError, match="unverified_connector_reply"):
        connector.client(8792, credential, data, "task")


def test_project_read_is_not_permission_and_old_cards_cannot_be_submitted(host, simulated_service, monkeypatch):
    root, raw, grant, folder, permission = world(host, monkeypatch)
    new_card(folder)
    task = {"project": raw["project"], "identifier": "SAMPLE-1"}
    view = connect.task(task, root, grant)
    assert view["status"] == "unassigned" and view["execution_permission"]["status"] == "not_configured"
    offer = permission.prepare()
    permission.authorize(offer["confirmation_token"], lambda _: KEYS)
    with pytest.raises(connect.ConnectError, match="not_eligible"):
        connect.task(task | {"authorization_id": permission.show()["id"], "card_digest": "0" * 64}, root, grant, submit=True)
    assert not (folder / ".ours-state/api-team/SAMPLE-1/session.json").exists()


@pytest.mark.asyncio
async def test_signed_project_submission_and_replay_without_browser_confirmation(host, connector, simulated_service, monkeypatch):
    root, raw, grant, folder, permission = world(host, monkeypatch)
    permission.authorize(permission.prepare()["confirmation_token"], lambda _: KEYS)
    await asyncio.to_thread(new_card, folder)
    token = json.loads(connect.read_private(root / ".connections/sample.token"))["token"]
    challenge = str(int(time.time())) + "-" + "a" * 32
    def headers(action, body):
        return {"Authorization": "Symphony " + connect.signature(token, "request:" + action, challenge, body),
                "X-Symphony-Challenge": challenge, "Content-Type": "application/json"}
    task = {"project": raw["project"], "identifier": "SAMPLE-1"}
    async with TestClient(TestServer(connector.application(root))) as client:
        data = json.dumps(task).encode()
        response = await client.post("/v1/task", data=data, headers=headers("task", data))
        body = await response.read()
        assert response.status == 200 and response.headers["X-Symphony-Proof"] == connect.signature(token, "reply:task", challenge, body)
        view = json.loads(body)
        assert view["status"] == "unassigned"
        submission = json.dumps(task | view["submission"]).encode()
        wrong_route = await client.post("/v1/submit", data=submission, headers=headers("task", submission))
        assert wrong_route.status == 403
        wrong_origin = await client.post("/v1/submit", data=submission, headers=headers("submit", submission) | {"Origin":"https://other.invalid"})
        assert wrong_origin.status == 403
        for _ in range(2):
            response = await client.post("/v1/submit", data=submission, headers=headers("submit", submission))
            body = await response.read()
            assert response.status == 200 and json.loads(body)["status"] == "queued"
            assert response.headers["X-Symphony-Proof"] == connect.signature(token, "reply:submit", challenge, body)
            assert token.encode() not in body
        assert (await client.post("/v1/authorize-execution", json=task)).status == 404
    assert not (folder / ".ours-state/api-team/SAMPLE-1/session.json").exists()


def test_read_then_changed_card_is_not_submitted(host, simulated_service, monkeypatch):
    root, raw, grant, folder, permission = world(host, monkeypatch)
    permission.authorize(permission.prepare()["confirmation_token"], lambda _: KEYS)
    new_card(folder)
    task = {"project": raw["project"], "identifier": "SAMPLE-1"}
    view = connect.task(task, root, grant)
    path = folder / "kanban/SAMPLE-1.md"
    path.write_text(path.read_text() + "\nChanged requirements.\n")
    with pytest.raises(connect.ConnectError, match="task_changed"):
        connect.task(task | view["submission"], root, grant, submit=True)
    assert not (folder / ".ours-state/api-team/SAMPLE-1/assignment.json").exists()


def test_newly_approved_team_uses_same_project_capability(host, simulated_service, monkeypatch):
    root, raw, grant, folder, permission = world(host, monkeypatch)
    connect.models.Teams(folder / "api-settings.json", permission.context).save({"lead_profile":"sol-lead", "worker_profile":None})
    permission.authorize(permission.prepare()["confirmation_token"], lambda _: KEYS)
    new_card(folder)
    task = {"project": raw["project"], "identifier": "SAMPLE-1"}
    view = connect.task(task, root, grant)
    result = connect.task(task | view["submission"], root, grant, submit=True)
    assert result["status"] == "queued" and result["profiles"]["lead"]["name"] == "sol-lead"


@pytest.mark.parametrize("change", [{"project":"other"}, {"identifier":"OTHER-1"}, {"command":"anything"}])
def test_project_handoff_refuses_foreign_identity_and_arbitrary_payload(host, simulated_service, monkeypatch, change):
    root, raw, grant, folder, permission = world(host, monkeypatch)
    with pytest.raises(connect.ConnectError):
        connect.task({"project": raw["project"], "identifier":"SAMPLE-1"} | change, root, grant)
