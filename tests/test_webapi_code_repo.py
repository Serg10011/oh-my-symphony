"""`workspace.code_repo`: read-only web git routes inspect an external code repo.

The board dir (WORKFLOW.md + kanban) stays the repo for everything that
mutates git (merge, push, branch delete); the key only redirects branches,
log, task branches, compare and diff.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient

from symphony.utils.auto_merge import AutoMergeResult
from symphony.workflow import WorkflowState
from tests import test_webapi as web

# Reuse the web API fixtures (board dir, stub orchestrator, client, git repo).
_git = web._git
board_dir = web.board_dir
client = web.client
git_repo = web.git_repo


@pytest.fixture()
def code_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Standalone repo: main plus a symphony/SEED-1 branch with one commit."""
    repo = tmp_path_factory.mktemp("code")
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("print('v1')\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "code: init")
    _git(repo, "checkout", "-q", "-b", "symphony/SEED-1")
    (repo / "feature.py").write_text("print('code feature')\n")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-q", "-m", "SEED-1: code feature")
    _git(repo, "checkout", "-q", "main")
    return repo


def _set_code_repo(client: TestClient, board_dir: Path, value: str) -> None:
    workflow = board_dir / "WORKFLOW.md"
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "agent:\n  kind: claude",
            f'workspace:\n  code_repo: "{value}"\n\nagent:\n  kind: claude',
        ),
        encoding="utf-8",
    )
    cfg, err = client.stub.workflow_state.reload()  # type: ignore[attr-defined]
    assert err is None and cfg is not None


async def test_read_routes_show_external_code_repo(
    client: TestClient, board_dir: Path, code_repo: Path
) -> None:
    # The board dir is not a git repo: without the key every route degrades.
    assert (await (await client.get("/api/v1/git/branches")).json()) == {"branches": []}

    _set_code_repo(client, board_dir, str(code_repo))

    branches = (await (await client.get("/api/v1/git/branches")).json())["branches"]
    assert sorted(branches) == ["main", "symphony/SEED-1"]

    log = await (await client.get("/api/v1/git/log?branch=symphony/SEED-1")).json()
    assert [c["subject"] for c in log["commits"]][:2] == [
        "SEED-1: code feature",
        "code: init",
    ]
    assert log["note"] is None

    tasks = await (await client.get("/api/v1/git/task-branches")).json()
    assert tasks["target_branch"] == "main"
    (row,) = tasks["branches"]
    assert row["branch"] == "symphony/SEED-1"
    assert row["ticket"]["identifier"] == "SEED-1"  # tickets still come from the board
    assert row["ahead"] == 1

    cmp_ = await (await client.get("/api/v1/git/compare?branch=symphony/SEED-1")).json()
    assert cmp_["target"] == "main"
    assert cmp_["stat"]["files"][0]["path"] == "feature.py"

    diff = await (await client.get("/api/v1/git/diff?branch=symphony/SEED-1")).json()
    assert "code feature" in str(diff)
    sha = log["commits"][0]["sha"]
    patch = await (await client.get(f"/api/v1/git/diff?commit={sha}")).json()
    assert "code feature" in str(patch)


async def test_code_repo_wins_over_a_git_board_dir(
    git_repo: Path, client: TestClient, board_dir: Path, code_repo: Path
) -> None:
    # Without the key the board dir's own repo is shown (previous behaviour).
    _git(git_repo, "branch", "board-only")
    before = (await (await client.get("/api/v1/git/branches")).json())["branches"]
    assert "board-only" in before

    _set_code_repo(client, board_dir, str(code_repo))
    after = (await (await client.get("/api/v1/git/branches")).json())["branches"]
    assert "board-only" not in after
    assert sorted(after) == ["main", "symphony/SEED-1"]


async def test_mutating_routes_ignore_code_repo(
    client: TestClient,
    board_dir: Path,
    code_repo: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    bare = tmp_path_factory.mktemp("remote")
    _git(bare, "init", "-q", "--bare")
    _git(code_repo, "remote", "add", "origin", str(bare))
    main_ref = code_repo / ".git" / "refs" / "heads" / "main"
    head_before = main_ref.read_text()
    _set_code_repo(client, board_dir, str(code_repo))

    # Merge works on the board dir (not a git repo here), so it refuses.
    branch = {"branch": "symphony/SEED-1"}
    resp = await client.post("/api/v1/git/merge", json=branch)
    assert (resp.status, (await resp.json())["error"]["code"]) == (400, "no_target")
    resp = await client.post("/api/v1/git/merge", json={**branch, "target": "main"})
    assert resp.status == 409
    assert (await resp.json())["error"]["code"].startswith("merge_")

    resp = await client.post("/api/v1/git/push", json=branch)
    assert (resp.status, (await resp.json())["error"]["code"]) == (400, "no_remote")

    resp = await client.post(
        "/api/v1/git/branch/delete", json={**branch, "force": True}
    )
    assert resp.status == 409
    assert (await resp.json())["error"]["code"] == "not_a_git_repo"

    # The code repo is untouched: branch kept, main not moved, remote empty.
    branches = (await (await client.get("/api/v1/git/branches")).json())["branches"]
    assert "symphony/SEED-1" in branches
    assert main_ref.read_text() == head_before
    assert not (code_repo / "feature.py").exists()
    assert not list((bare / "refs" / "heads").iterdir())


async def test_merge_engine_gets_board_dir_not_code_repo(
    client: TestClient,
    board_dir: Path,
    code_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_code_repo(client, board_dir, str(code_repo))
    captured: dict[str, object] = {}

    async def fake_merge(**kwargs: object) -> AutoMergeResult:
        captured.update(kwargs)
        return AutoMergeResult(ok=True, status="merged", detail="")

    monkeypatch.setattr(
        "symphony.webapi.git_routes.auto_merge_on_done_best_effort", fake_merge
    )
    resp = await client.post(
        "/api/v1/git/merge", json={"branch": "symphony/SEED-1", "target": "main"}
    )
    assert resp.status == 200
    assert captured["workflow_dir"] == board_dir


def _reload_error(board_dir: Path, value: object) -> Exception | None:
    workflow = board_dir / "WORKFLOW.md"
    text = workflow.read_text(encoding="utf-8")
    rendered = (
        value if isinstance(value, str) and value.startswith("[") else repr(value)
    )
    workflow.write_text(
        text.replace(
            "agent:\n  kind: claude",
            f"workspace:\n  code_repo: {rendered}\n\nagent:\n  kind: claude",
        ),
        encoding="utf-8",
    )
    _cfg, err = WorkflowState(workflow).reload()
    return err


@pytest.mark.parametrize("raw", [None, ""])
def test_unset_or_empty_code_repo_keeps_previous_behaviour(
    board_dir: Path, raw: str | None
) -> None:
    workflow = board_dir / "WORKFLOW.md"
    if raw is not None:
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace(
                "agent:\n  kind: claude",
                f'workspace:\n  code_repo: "{raw}"\n\nagent:\n  kind: claude',
            ),
            encoding="utf-8",
        )
    cfg, err = WorkflowState(workflow).reload()
    assert err is None and cfg is not None
    assert cfg.code_repo is None


def test_valid_code_repo_is_parsed(board_dir: Path, code_repo: Path) -> None:
    assert _reload_error(board_dir, str(code_repo)) is None
    cfg, _ = WorkflowState(board_dir / "WORKFLOW.md").reload()
    assert cfg is not None and cfg.code_repo == code_repo


def test_invalid_code_repo_is_a_config_error(
    board_dir: Path, code_repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    plain = tmp_path_factory.mktemp("plain")
    bad: list[object] = [
        "relative/repo",
        str(plain),  # exists, not a git repo
        str(code_repo / "sub"),  # missing
        42,
        "[a, b]",
    ]
    (code_repo / "sub").mkdir()
    bad.append(str(code_repo / "sub"))  # inside a repo, not its root
    for value in bad:
        err = _reload_error(board_dir, value)
        assert err is not None, value
        assert "code_repo" in str(err), value
