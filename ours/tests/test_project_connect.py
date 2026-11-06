"""The constructor binds an owner-approved request to one project, board and real card defaults."""
from concurrent.futures import ThreadPoolExecutor
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import project_connect as connect  # noqa: E402
from symphony import api_team
from symphony.cli import board as board_cli
from symphony.server import build_app
from symphony.trackers.file import parse_ticket_file
from symphony.workflow import WorkflowState
from tests.test_webapi import _StubOrchestrator


@pytest.fixture
def connector():
    tool = Path(connect.__file__).resolve().parents[1] / "bin/project-connect"
    loader = SourceFileLoader("test_connector_cli", str(tool))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "boards"
    root.mkdir(mode=0o700)
    repo = tmp_path / "source"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("A real project file.\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "base"], check=True)
    raw = {"project": "sample", "repo": str(repo), "base": "main", "paths": ["README.md"], "mode": "economy",
           "team": {"lead_profile": "deepseek-lead", "worker_profile": "luna-worker"}, "verification": "manual"}
    return root, raw


def authorized(root, raw):
    review = connect.review(raw, root)
    auth = connect.authorize(raw, root, review["approval_id"])
    token = json.loads(Path(auth["credential_file"]).read_text())["token"]
    body = json.dumps(raw).encode()
    challenge = str(int(time.time())) + "-" + "a" * 32
    proof = connect.signature(token, "request", challenge, body)
    grant, key = connect.authenticate(raw, root, challenge, proof, body)
    assert key == token and grant["approval_id"] == review["approval_id"]
    return grant


@pytest.fixture
def simulated_service(monkeypatch):
    starts = []
    monkeypatch.setattr(connect, "ensure_live", lambda board, port, can_start: starts.append((board, port, can_start)))
    monkeypatch.setattr(connect, "verify_live", lambda *args: ["Todo", "In Progress", "Verify", "Human Review"])
    return starts


@pytest.mark.parametrize("change", [
    {"authorized": True}, {"project": "../other"}, {"repo": "."}, {"base": "-f"},
    {"mode": "unknown"}, {"team": {"lead_profile": "fake", "worker_profile": None}},
    {"paths": ["."]}, {"paths": [".git/config"]}, {"verification": "untrusted-command"},
    {"command": "anything"}, {"port": 1}, {"budget": {"max_usd": -1}},
])
def test_invalid_request_cannot_start_or_write_a_board(host, change):
    root, raw = host
    with pytest.raises(connect.ConnectError):
        connect.review(raw | change, root)
    assert list(root.iterdir()) == []


def test_review_does_not_authorize_and_changed_approval_is_refused(host):
    root, raw = host
    review = connect.review(raw, root)
    assert not list(root.iterdir())
    with pytest.raises(connect.ConnectError, match="approval_changed"):
        connect.authorize(raw | {"mode": "productivity"}, root, review["approval_id"])
    authorized(root, raw)
    changed = raw | {"mode": "productivity"}
    with pytest.raises(connect.ConnectError, match="differently"):
        connect.authorize(changed, root, connect.review(changed, root)["approval_id"])
    assert not (root / "sample").exists()


def test_sibling_project_and_modified_requests_get_no_authority(host):
    root, raw = host
    grant = authorized(root, raw)
    peer = raw | {"project": "peer"}
    authorized(root, peer)
    key = json.loads(connect.read_private(root / ".connections/sample.token"))["token"]
    body = json.dumps(peer).encode()
    challenge = str(int(time.time())) + "-" + "0" * 32
    signature = connect.signature(key, "request", challenge, body)
    with pytest.raises(connect.ConnectError, match="unauthorized"):
        connect.authenticate(peer, root, challenge, signature, body)
    with pytest.raises(connect.ConnectError, match="unauthorized"):
        connect.authenticate(raw, root, challenge, signature, json.dumps(raw).encode())
    with pytest.raises(connect.ConnectError, match="different_from_approval"):
        connect.connect(raw | {"mode": "productivity"}, root, grant)
    assert sorted(p.name for p in root.iterdir()) == [".connections"]


def test_create_retry_and_concurrent_call_share_one_board(host, simulated_service):
    root, raw = host
    grant = authorized(root, raw)
    with ThreadPoolExecutor(2) as threads:
        first, second = [job.result() for job in [threads.submit(connect.connect, raw, root, grant) for _ in range(2)]]
    assert first == second
    assert first["prefix"] == "SAMPLE" and first["lead_effort"] == "high" and first["payment"] == "explicit_per_card"
    assert [call[2] for call in simulated_service] == [True, False]
    assert sorted(p.name for p in root.iterdir()) == [".connections", "sample"]
    assert not list((root / "sample/kanban").glob("*.md"))


def test_failed_start_is_recoverable_without_recreating_board(host, simulated_service, monkeypatch):
    root, raw = host
    grant = authorized(root, raw)
    with monkeypatch.context() as mock:
        def unavailable(*args):
            raise connect.ConnectError("board_start_failed")
        mock.setattr(connect, "ensure_live", unavailable)
        with pytest.raises(connect.ConnectError, match="board_start_failed"):
            connect.connect(raw, root, grant)
    workflow = root / "sample/WORKFLOW.md"
    inode = workflow.stat().st_ino
    assert not (root / ".connections/sample.ready").exists()
    assert connect.connect(raw, root, grant)["status"] == "ready"
    assert workflow.stat().st_ino == inode


def test_refuses_unowned_or_symlink_board_and_never_replaces_empty_directory(host):
    root, raw = host
    grant = authorized(root, raw)
    board = root / "sample"
    board.mkdir()
    with pytest.raises(connect.ConnectError, match="board_conflict"):
        connect.connect(raw, root, grant)
    stage = root / ".other"
    stage.mkdir()
    with pytest.raises(connect.ConnectError, match="board_conflict"):
        connect.publish_directory(stage, board)
    assert stage.exists() and not list(board.iterdir())
    board.rmdir()
    board.symlink_to(raw["repo"], target_is_directory=True)
    with pytest.raises(connect.ConnectError, match="board_conflict"):
        connect.connect(raw, root, grant)
    assert not (Path(raw["repo"]) / "project.toml").exists()


def test_manual_existing_board_is_never_started_or_rewritten(host, simulated_service):
    root, raw = host
    grant = authorized(root, raw)
    board = root / "sample"
    connect.create_board(root, board, grant)
    (board / ".ours-state/connection.json").unlink()
    before = (board / "WORKFLOW.md").read_bytes()
    assert connect.connect(raw, root, grant)["status"] == "ready"
    assert simulated_service[-1][2] is False
    assert before == (board / "WORKFLOW.md").read_bytes()
    (board / "WORKFLOW.md").write_bytes(before + b"\n")
    with pytest.raises(connect.ConnectError, match="board_configuration_differs"):
        connect.connect(raw, root, grant)


def test_recreated_repository_at_same_path_is_not_the_approved_project(host):
    root, raw = host
    grant = authorized(root, raw)
    repo = Path(raw["repo"])
    repo.rename(repo.with_name("old"))
    subprocess.run(["git", "clone", "-q", str(repo.with_name("old")), str(repo)], check=True)
    with pytest.raises(connect.ConnectError, match="repository_changed"):
        connect.connect(raw, root, grant)
    assert not (root / "sample").exists()


def test_verify_refuses_foreign_workflow_or_process(host, monkeypatch):
    root, _ = host
    workflow = root / "sample/WORKFLOW.md"
    monkeypatch.setattr(connect.service, "load_record", lambda wf: None)
    monkeypatch.setattr(connect.service, "is_symphony_workflow_reachable", lambda *args: False)
    for managed in (True, False):
        with pytest.raises(connect.ConnectError, match="board_identity_changed"):
            connect.verify_instance(workflow, 10020, managed)
    monkeypatch.setattr(connect.service, "load_record", lambda wf: SimpleNamespace(
        host="127.0.0.1", port=10020, orchestrator_pid=10, service_instance_id="recorded"))
    monkeypatch.setattr(connect.service, "probe_service_endpoint_identity", lambda *args: SimpleNamespace(orchestrator_pid=11))
    with pytest.raises(connect.ConnectError, match="board_identity_changed"):
        connect.verify_instance(workflow, 10020, True)


def test_offline_board_with_existing_work_requests_operator_start(host, monkeypatch):
    root, raw = host
    connect.create_board(root, root / "sample", authorized(root, raw))
    card = root / "sample/kanban/SAMPLE-1.md"
    card.write_text("Existing work.")
    monkeypatch.setattr(connect.service, "is_symphony_workflow_reachable", lambda *args: False)
    with pytest.raises(connect.ConnectError, match="nonempty_board_requires_operator_start"):
        connect.ensure_live(root / "sample", 10020, True)
    assert card.read_text() == "Existing work."


@pytest.mark.asyncio
async def test_gateway_signs_success_and_refuses_stale_or_cross_origin_requests(host, connector, simulated_service):
    root, raw = host
    authorized(root, raw)
    token = json.loads(connect.read_private(root / ".connections/sample.token"))["token"]
    data = json.dumps(raw).encode()
    challenge = str(int(time.time())) + "-" + "e" * 32
    def headers(challenge):
        return {"Authorization": "Symphony " + connect.signature(token, "request", challenge, data),
                "X-Symphony-Challenge": challenge, "Content-Type": "application/json"}
    async with TestClient(TestServer(connector.application(root))) as client:
        foreign = await client.post("/v1/connect", data=data, headers=headers(challenge) | {"Origin":"https://other.invalid"})
        assert foreign.status == 403
        stale = await client.post("/v1/connect", data=data, headers=headers("1000000000-" + "e" * 32))
        assert stale.status == 403 and not simulated_service
        reply = await client.post("/v1/connect", data=data, headers=headers(challenge))
        body = await reply.read()
        assert reply.status == 200 and json.loads(body)["status"] == "ready"
        assert reply.headers["X-Symphony-Proof"] == connect.signature(token, "reply", challenge, body)
        assert token.encode() not in body


def test_client_rejects_unsigned_ready_and_does_not_send_credential(host, connector, monkeypatch):
    root, raw = host
    authorized(root, raw)
    credential = root / ".connections/sample.token"
    token = json.loads(connect.read_private(credential))["token"]
    def forged(request, timeout):
        assert token not in str(request.header_items()) and token.encode() not in request.data
        stream = io.BytesIO(b'{"status":"ready"}')
        stream.headers, stream.status = {}, 200
        return stream
    monkeypatch.setattr(connector.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=forged))
    with pytest.raises(connect.ConnectError, match="unverified_connector_reply"):
        connector.client(8792, credential, json.dumps(raw).encode())


def test_keyed_cli_retry_keeps_original_card_defaults(host, simulated_service):
    root, raw = host
    connect.connect(raw, root, authorized(root, raw))
    board = root / "sample"
    args = ["new", "--workflow", str(board / "WORKFLOW.md"), "SAMPLE-1", "Write operator guide",
            "--creation-key", "e" * 64]
    assert board_cli.main(args) == 0
    card = board / "kanban/SAMPLE-1.md"
    before = card.read_bytes()
    teams = connect.models.Teams(board / "api-settings.json", {"workflow": str(board / "WORKFLOW.md"), "agent_kind":"api-team"})
    teams.save({"lead_profile":"sol-lead", "worker_profile":None})
    assert board_cli.main(args) == 0 and card.read_bytes() == before


@pytest.mark.asyncio
async def test_normal_http_creation_pins_team_mode_and_prefix_until_review(host, simulated_service):
    root, raw = host
    raw["mode"] = "productivity"
    grant = authorized(root, raw)
    connect.connect(raw, root, grant)
    board = root / "sample"
    state = WorkflowState(board / "WORKFLOW.md")
    assert state.reload()[0]
    async with TestClient(TestServer(build_app(_StubOrchestrator(state)))) as http:
        first = await http.post("/api/v1/issues", json={"title": "Update project instructions"})
        assert first.status == 201 and await first.json() == {"identifier":"SAMPLE-1", "state":"Todo"}
        wrong = await http.post("/api/v1/issues", json={"title": "Wrong project prefix", "prefix": "TASK"})
        assert wrong.status >= 400 and not (board / "kanban/TASK-1.md").exists()
        other = await http.post("/api/v1/issues", json={"title": "Another backend", "agent_kind": "claude"})
        assert other.status == 400
        original, _ = parse_ticket_file(board / "kanban/SAMPLE-1.md")
        assert original["agent"]["kind"] == "api-team"
        assert original["api_defaults"]["team"] == raw["team"] and original["api_defaults"]["mode"] == "productivity"
        new_team = {"lead_profile": "sol-lead", "worker_profile": None}
        origin = {"Origin": str(http.make_url("/")).rstrip("/")}
        changed = await http.post("/api/v1/api-teams/api-team", json={"team": new_team}, headers=origin)
        assert changed.status == 200
        second = await http.post("/api/v1/issues", json={"title": "Next card"})
        assert second.status == 201
        later, _ = parse_ticket_file(board / "kanban/SAMPLE-2.md")
        assert later["api_defaults"]["team"] == new_team
        directory = await (await http.get("/api/v1/api-teams/api-team?id=SAMPLE-1&id=SAMPLE-2")).json()
        assert directory["assignments"] == {"SAMPLE-1": raw["team"], "SAMPLE-2": new_team}
        assignment = await http.post("/api/v1/issues/SAMPLE-1/api-team/prepare", json={}, headers=origin)
        assert assignment.status == 200
        prepared = await assignment.json()
        assert prepared["mode"] == "productivity" and prepared["profiles"]["lead"]["effort"] == "max"
        assert prepared["profiles"]["lead"]["name"] == "deepseek-lead" and prepared["profiles"]["worker"]["effort"] == "max"
        assert not api_team.confirmed_in_queue(state.current(), "SAMPLE-1")
        assert not (board / ".ours-state/api-team/SAMPLE-1/session.json").exists()
        policy = board / "api-settings.json"
        original = json.loads(policy.read_text())
        connect.api_session._save(policy, original | {"paths": ["src"]})
        changed_scope = await http.post("/api/v1/issues/SAMPLE-1/api-team/prepare", json={}, headers=origin)
        assert changed_scope.status >= 400 and not api_team.confirmed_in_queue(state.current(), "SAMPLE-1")
