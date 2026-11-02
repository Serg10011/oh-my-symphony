"""Verify the overview with temporary boards and read-only fake service APIs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOKS))
import project_overview as overview  # noqa: E402


def issue(identifier, state="Todo", request="", title=None):
    return {"identifier": identifier, "state": state, "request": request, "title": title or identifier}


def board(issues):
    return {"board": {"tracker_kind": "file"}, "issues": issues,
            "columns": [{"name": name, "terminal": name in {"Done", "Archive", "Blocked", "Human Review", "Subtasks", "Merging", "needs_human", "Parking"}}
                        for name in ["Todo", "In Progress", "Done", "Archive", "Blocked", "Human Review", "Subtasks", "Merging", "needs_human", "Parking"]]}


def state():
    return {"counts": {"running": 0, "retrying": 0}, "dispatch_hold": None}


def project(root, name="oms", port=9972):
    path = root / name
    path.mkdir()
    (path / "project.toml").write_text(f'[project]\nname="{name}"\nport={port}\n')
    (path / "WORKFLOW.md").write_text("---\ntracker:\n  kind: file\n---\n")
    return path


def rows(data):
    return [row for tree in data["trees"] for row in tree["rows"] if row["kind"] == "issue"]


def test_categories_hierarchy_and_archive():
    data = overview.board_view(board([issue("OMS-1", "Subtasks"), issue("OMS-1-1", "In Progress", "OMS-1"),
                                     issue("OMS-1-2", "Human Review", "OMS-1"), issue("OMS-2", "needs_human"),
                                     issue("OMS-3", "Blocked"), issue("OMS-4", "Done"), issue("OMS-5", "Archive"),
                                     issue("OMS-6", "Parking"), issue("OMS-7", "Merging")]), state())
    assert data["summary"] == dict(working=3, review=1, help=2, completed=2, other=1)
    tree = data["trees"][0]
    assert [r["identifier"] for r in tree["rows"]] == ["OMS-1", "OMS-1-1", "OMS-1-2"]
    assert [r["depth"] for r in tree["rows"]] == [0, 1, 1]
    assert data["trees"][3]["completed"] and len(rows(data)) == 9


@pytest.mark.parametrize("requests", [["OMS-1"], ["OMS-2", "OMS-1"], ["OMS-2", "OMS-3", "OMS-1"]])
def test_cycles_keep_every_card(requests):
    source = [issue(f"OMS-{i}", request=parent) for i, parent in enumerate(requests, 1)]
    data = overview.board_view(board(source), state())
    assert sorted(r["identifier"] for r in rows(data)) == [f"OMS-{i}" for i in range(1, len(source) + 1)]
    assert data["warnings"] and all(row["depth"] == 0 for row in rows(data))


def test_unknown_request_group_and_deep_chain_are_lossless():
    source = [issue("OMS-1", request="external-request"), issue("OMS-2", request="external-request")]
    data = overview.board_view(board(source), state())
    assert len(data["trees"]) == 1 and data["trees"][0]["rows"][0]["kind"] == "group"
    assert data["summary"]["working"] == 2 and len(rows(data)) == 2
    source = [issue(f"OMS-{i}", request=f"OMS-{i-1}" if i > 1 else "") for i in range(1, 1200)]
    data = overview.board_view(board(source), state())
    assert len(data["trees"]) == 1 and len(rows(data)) == 1199
    assert rows(data)[-1]["depth"] == 1198


@pytest.mark.parametrize("source", [[issue("OMS-1"), issue("OMS-1")], [issue("../x")], [dict(identifier="OMS-1", state="Todo")], [None]])
def test_invalid_cards_do_not_become_empty_success(source):
    with pytest.raises(overview.DataError):
        overview.board_view(board(source), state())


@pytest.mark.parametrize("bad", [None, {"counts": {"running": True, "retrying": 0}}, {"counts": {"running": -1, "retrying": 0}}])
def test_unknown_runtime_is_not_zero(bad):
    with pytest.raises(overview.DataError):
        overview.board_view(board([]), bad)


def test_parent_completed_with_active_child_remains_visible():
    data = overview.board_view(board([issue("OMS-1", "Done"), issue("OMS-1-1", "Blocked", "OMS-1")]), state())
    assert not data["trees"][0]["completed"] and data["summary"]["help"] == 1


async def test_mixed_projects_reads_only_expected_gets(tmp_path):
    first = project(tmp_path)
    project(tmp_path, "eve", 9973)
    calls = []
    async def fetch(url):
        calls.append(url)
        if ':9973/' in url:
            raise aiohttp.ClientConnectionError()
        return {'/api/v1/health': {'workflow_path': str((first / 'WORKFLOW.md').resolve())},
                '/api/v1/board': board([issue('OMS-1', 'Human Review')]), '/api/v1/state': state()}[urlsplit(url).path]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob('*') if p.is_file()}
    data = await overview.snapshot(tmp_path, fetch)
    by_id = {p['id']: p for p in data['projects']}
    assert by_id['oms']['summary']['review'] == 1 and by_id['oms']['board_url'] == 'http://127.0.0.1:9972/'
    assert by_id['eve']['service_state'] == 'unavailable' and by_id['eve']['summary'] is None
    assert by_id['eve']['board_url'] is None
    assert all(urlsplit(url).path in {'/api/v1/health', '/api/v1/board', '/api/v1/state'} for url in calls)
    assert sum(':9973/' in url for url in calls) == 1
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob('*') if p.is_file()}


async def test_wrong_workflow_stops_before_board_read(tmp_path):
    project(tmp_path)
    calls = []
    async def fetch(url):
        calls.append(url)
        return {'workflow_path': '/different/WORKFLOW.md'}
    result = (await overview.snapshot(tmp_path, fetch))['projects'][0]
    assert result['service_state'] == 'mismatch' and result['board_url'] is None and result['summary'] is None
    assert len(calls) == 1


async def test_bad_board_does_not_hide_another_project(tmp_path):
    first = project(tmp_path)
    project(tmp_path, 'broken', 9973)
    async def fetch(url):
        name = 'oms' if ':9972/' in url else 'broken'
        path = urlsplit(url).path
        if path.endswith('health'):
            return {'workflow_path': str((tmp_path / name / 'WORKFLOW.md').resolve())}
        if name == 'broken':
            return []
        return board([issue('OMS-1')]) if path.endswith('board') else state()
    data = {p['id']: p for p in (await overview.snapshot(tmp_path, fetch))['projects']}
    assert first.exists() and data['oms']['service_state'] == 'running'
    assert data['broken']['service_state'] == 'error' and data['broken']['summary'] is None


@pytest.mark.parametrize('content', ['[broken', '[project]\nname="x"\nport=true', '[project]\nname="x"\nport=65536'])
async def test_bad_configuration_is_visible(tmp_path, content):
    path = project(tmp_path)
    (path / 'project.toml').write_text(content)
    async def fetch(url): raise AssertionError('invalid configuration must not request a service')
    result = (await overview.snapshot(tmp_path, fetch))['projects'][0]
    assert result['service_state'] == 'error' and result['summary'] is None


async def test_http_surface_rejects_mutation_and_foreign_host(tmp_path):
    client = TestClient(TestServer(overview.build_app(tmp_path)))
    await client.start_server()
    try:
        response = await client.get('/')
        assert response.status == 200 and 'Обзор проектов' in await response.text()
        assert "script-src 'self'" in response.headers['Content-Security-Policy']
        assert (await client.get('/overview.js')).status == 200
        response = await client.get('/api/projects')
        assert response.status == 200 and (await response.json())['read_only']
        for method in ['post','patch','delete','put']:
            assert (await getattr(client,method)('/api/projects',json={})).status == 405
        assert (await client.get('/api/projects',headers={'Host':'evil.example'})).status == 403
    finally:
        await client.close()


@pytest.mark.parametrize('mode', ['redirect', 'bad_json', 'nonfinite', 'too_large'])
async def test_fetch_rejects_invalid_or_redirected_responses(mode, monkeypatch):
    hits = []
    async def route(request):
        hits.append(request.path)
        if mode == 'redirect':
            raise web.HTTPFound('/other')
        if mode == 'bad_json':
            return web.Response(text='<html>error</html>')
        if mode == 'nonfinite':
            return web.Response(text='{"value":NaN}')
        return web.Response(text='x' * 100)
    app = web.Application()
    app.router.add_get('/data', route)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(overview,'MAX_BYTES',32)
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(overview.DataError):
                await overview.fetch_json(session,str(server.make_url('/data')))
        assert hits == ['/data']
    finally:
        await server.close()


def test_cli_empty_snapshot_and_invalid_root(tmp_path):
    command = [sys.executable,str(HOOKS.parent/'bin/project-overview'),'--snapshot','--projects-root',str(tmp_path)]
    result = subprocess.run(command,capture_output=True,text=True,timeout=10)
    assert result.returncode == 0 and json.loads(result.stdout)['projects'] == []
    tmp_path.rmdir()
    result = subprocess.run(command,capture_output=True,text=True,timeout=10)
    assert result.returncode == 1 and json.loads(result.stdout)['error']


async def test_timeout_is_unknown_not_a_zero_snapshot(tmp_path):
    project(tmp_path)
    async def fetch(url): raise asyncio.TimeoutError()
    result = (await overview.snapshot(tmp_path,fetch))['projects'][0]
    assert result['service_state'] == 'unavailable' and result['summary'] is None
