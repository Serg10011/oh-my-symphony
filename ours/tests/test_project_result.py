"""Project credentials can read their own saved diff through the local client."""
import asyncio
import json
import time

from aiohttp.test_utils import TestClient, TestServer

import test_project_connect as connector_fixture
import api_board
import project_connect as connect
from symphony.cli import board as board_cli


connector = connector_fixture.connector
host = connector_fixture.host
simulated_service = connector_fixture.simulated_service


async def test_signed_result_read_is_bound_to_project_and_cannot_mutate(host, connector, simulated_service, monkeypatch):
    root, raw = host
    grant = connector_fixture.authorized(root, raw)
    connect.connect(raw, root, grant)
    monkeypatch.setattr(connect, "verify_instance", lambda *args: None)
    board = root / "sample"
    args = ["new", "--workflow", str(board / "WORKFLOW.md"), "SAMPLE-1", "Read the result"]
    assert await asyncio.to_thread(board_cli.main, args) == 0
    card = board / "kanban/SAMPLE-1.md"
    original = card.read_bytes()
    calls = []

    def review(self, offset, version):
        calls.append((self.identifier, offset, version))
        return {"files": [{"path": "README.md", "diff": "+Saved change"}], "next_offset": None}

    monkeypatch.setattr(api_board.Board, "review", review)
    token = json.loads(connect.read_private(root / ".connections/sample.token"))["token"]
    challenge = str(int(time.time())) + "-" + "b" * 32
    query = {"project": "sample", "identifier": "SAMPLE-1", "offset": 4, "version": "checked"}
    async with TestClient(TestServer(connector.application(root))) as http:
        async def send(raw, direction="request:result"):
            data = json.dumps(raw).encode()
            return await http.post("/v1/result", data=data, headers={
                "Content-Type": "application/json", "X-Symphony-Challenge": challenge,
                "Authorization": "Symphony " + connect.signature(token, direction, challenge, data)})

        assert (await send(query, "request:task")).status == 403
        assert (await send({**query, "project": "other"})).status == 403
        assert (await send({**query, "identifier": "OTHER-1"})).status == 409
        assert (await send({**query, "state": "Done"})).status == 409
        response = await send(query)
        body = await response.read()
        assert response.status == 200
        assert response.headers["X-Symphony-Proof"] == connect.signature(token, "reply:result", challenge, body)
        result = json.loads(body)
        assert result["card_state"] == "Todo" and result["review"]["files"][0]["path"] == "README.md"
    assert calls == [("SAMPLE-1", 4, "checked")]
    assert card.read_bytes() == original
