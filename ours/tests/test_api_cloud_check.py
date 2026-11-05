"""GitHub evidence for one immutable candidate, with no uploads or paid retries."""

import base64
from functools import partial
import json
from pathlib import Path

import httpx
import pytest

import test_api_candidate as capture_tests
from test_api_board import approve
from test_api_project import git
from test_api_session import KEYS, frame
from test_api_workspace import tool

import api_board as controller
import api_cloud_check as cloud
import api_session as session

board = capture_tests.board


RUN, ATTEMPT = 12345, 2
COMPLETED = "2026-11-05T11:20:30Z"
HEAD, BLOB = "a" * 40, "b" * 40
TRIAL, TREE, PARENT = "c" * 40, "d" * 40, "e" * 40
SOURCE = Path(__file__).resolve().parents[2]


def responses(commit=TRIAL, tree=TREE, parent=PARENT):
    raw = cloud.TRIAL_LINE.sub(rb"\g<1>" + commit.encode(), (SOURCE / cloud.WORKFLOW).read_bytes())
    repo = {"id": cloud.REPO_ID, "full_name": cloud.REPO, "private": False}
    steps = [{"name": name, "status": "completed", "conclusion": "success"} for name in cloud.STEPS]
    steps[3]["conclusion"] = "failure"
    run = {"id": RUN, "workflow_id": cloud.WORKFLOW_ID, "repository": repo, "head_repository": dict(repo),
           "path": cloud.WORKFLOW, "event": "push", "head_branch": cloud.BRANCH, "head_sha": HEAD,
           "run_attempt": ATTEMPT, "status": "completed", "conclusion": "failure"}
    entries = [{"path": name, "mode": mode, "sha": sha} for name, mode, sha in [
        (cloud.WORKFLOW, "100644", BLOB), (".gitattributes", "100644", cloud.ATTRIBUTES),
        ("ours/ci/linux", "040000", cloud.TOOLS_TREE)]]
    job = {"name": "verify", "head_sha": HEAD, "run_id": RUN, "run_attempt": ATTEMPT, "id": 88,
           "status": "completed", "conclusion": "failure", "completed_at": COMPLETED, "steps": steps}
    return {f"actions/runs/{RUN}": run, f"git/trees/{HEAD}?recursive=1": {"tree": entries, "truncated": False},
            f"git/blobs/{BLOB}": {"sha": BLOB, "encoding": "base64", "size": len(raw),
                                  "content": base64.b64encode(raw).decode()},
            f"git/commits/{commit}": {"sha": commit, "tree": {"sha": tree}, "parents": [{"sha": parent}]},
            f"actions/runs/{RUN}/attempts/{ATTEMPT}/jobs": {"total_count": 1, "jobs": [job]}}


def public_api(monkeypatch, documents):
    seen = []
    def route(request):
        assert request.method == "GET" and not request.content and "authorization" not in request.headers
        assert request.url.host == "api.github.com" and request.url.scheme == "https"
        path = request.url.raw_path.decode().removeprefix(f"/repos/{cloud.REPO}/")
        seen.append(path)
        return httpx.Response(200, json=documents[path])
    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(route)))
    return seen


def test_scopes_receipt_to_actual_trial_and_specific_attempt(monkeypatch):
    docs = responses()
    seen = public_api(monkeypatch, docs)
    result = cloud.verify(str(RUN), TRIAL, TREE, PARENT)
    assert result == {"run": RUN, "attempt": ATTEMPT, "job": 88, "completed_at": COMPLETED, "workflow_commit": HEAD,
                      "commit": TRIAL, "tree": TREE, "base_commit": PARENT, "comparison": "failure", "status": "passed"}
    assert len(seen) == 5 and seen[-1].endswith("/attempts/2/jobs")
    docs[seen[-1]]["jobs"][0]["steps"][4]["conclusion"] = "failure"
    assert cloud.verify(str(RUN), TRIAL, TREE, PARENT)["status"] == "failed"


@pytest.mark.parametrize("part", ["commit", "tree", "parent"])
def test_private_candidate_hashes_never_enter_any_request(monkeypatch, part):
    seen = public_api(monkeypatch, responses())
    local = {"commit": TRIAL, "tree": TREE, "parent": PARENT, part: "0" * 40}
    with pytest.raises(cloud.CloudError, match="cloud_other_candidate"):
        cloud.verify(str(RUN), **local)
    assert all("0" * 40 not in path for path in seen)


@pytest.mark.parametrize("change", ["repo", "fork", "private", "workflow", "branch", "event", "head", "pending",
                                    "tools", "attributes", "truncated", "recipe", "other_attempt", "duplicate",
                                    "isolation", "cancelled", "skipped", "cleanup", "conclusion"])
def test_untrusted_or_incomplete_attempt_cannot_pass(monkeypatch, change):
    docs = responses()
    run, tree, blob, _, jobs = docs.values()
    job = jobs["jobs"][0]
    if change in {"repo", "fork", "private"}:
        section = run["repository" if change == "repo" else "head_repository"]
        section.update({"private": True} if change == "private" else {"id": 9})
    elif change in {"workflow", "branch", "event", "head", "pending"}:
        run.update({"workflow": {"workflow_id": 123}, "branch": {"head_branch": "main"}, "event": {"event": "pull_request_target"},
                    "head": {"head_sha": "../another-repo"}, "pending": {"status": "in_progress"}}[change])
    elif change == "tools":
        tree["tree"][-1]["sha"] = "1" * 40
    elif change == "attributes":
        tree["tree"].append({"path": "ours/.gitattributes", "mode": "100644", "sha": BLOB})
    elif change == "truncated":
        tree["truncated"] = True
    elif change == "recipe":
        raw = base64.b64decode(blob["content"]).replace(b"host.sh\" trial", b"host.sh\" false")
        blob.update(content=base64.b64encode(raw).decode(), size=len(raw))
    elif change == "other_attempt":
        job["run_attempt"] = ATTEMPT - 1
    elif change == "duplicate":
        job["steps"].append(job["steps"][4])
    elif change == "conclusion":
        job["conclusion"] = run["conclusion"] = "success"
    else:
        i = {"isolation": 2, "cancelled": 4, "skipped": 4, "cleanup": 5}[change]
        job["steps"][i]["conclusion"] = change if change in ("skipped", "cancelled") else "failure"
    public_api(monkeypatch, docs)
    with pytest.raises(cloud.CloudError, match="cloud_(untrusted|incomplete)"):
        cloud.verify(str(RUN), TRIAL, TREE, PARENT)


@pytest.mark.parametrize("value", [True, 123, "0", "-1", " 12", "1/attempts/1", "https://evil.invalid", "1" * 20])
def test_only_a_run_number_can_be_supplied(value):
    with pytest.raises(cloud.CloudError, match="cloud_invalid_run"):
        cloud.verify(value, TRIAL, TREE, PARENT)


@pytest.mark.parametrize("response,reason", [(httpx.Response(302, headers={"Location": "https://evil.invalid"}), "cloud_unavailable"),
    (httpx.Response(429, text="secret error detail"), "cloud_unavailable"),
    (httpx.Response(200, content=b"x" * 2100000), "cloud_untrusted"), (httpx.Response(200, json=[]), "cloud_untrusted")])
def test_bad_remote_result_is_bounded_and_never_followed(monkeypatch, response, reason):
    calls = []
    def route(request):
        calls.append(request.url.host)
        return response
    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(route)))
    with pytest.raises(cloud.CloudError) as exc:
        cloud.verify(str(RUN), TRIAL, TREE, PARENT)
    assert str(exc.value) == reason and calls == ["api.github.com"]


def complete_card(board, *, enabled=True):
    capture_tests.configure(board)
    git(board.repo, "remote", "add", "origin", f"https://github.com/{cloud.REPO}.git")
    if enabled:
        raw = json.loads(board.settings.read_text())
        board.settings.write_text(json.dumps({**raw, "cloud_check": cloud.PROFILE}))
    approve(board)
    replies = [frame(None, [tool("write_project_file", "file", path="src/new.py", text="answer = 42\n", expected_sha256="")]),
               frame("Готово")]
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=replies.pop(0)))
    return board.run("a" * 32, credentials=lambda _: KEYS, transport=transport)


def test_board_keeps_attempt_and_cost_across_reopen_and_retry(board, monkeypatch):
    result = complete_card(board)
    candidate = result["candidate"]
    assert result["cloud_check"] == {"status": "ready", "commit": candidate["commit"], "receipt": None}
    docs = responses(candidate["commit"], candidate["tree"], candidate["base_commit"])
    seen = public_api(monkeypatch, docs)
    saved = board.attach_check(str(RUN))
    receipt = saved["cloud_check"]["receipt"]
    assert receipt["status"] == "passed" and receipt["comparison"] == "failure"
    assert saved["session"]["calls"] == result["session"]["calls"]
    assert not saved["accepted"] and not saved["delivered"] and board.card()[0]["state"] == "needs_human"
    archived = board.root / f"cloud-{RUN}-{ATTEMPT}.json"
    assert archived.is_file() and not (board.root / "check.log").exists()
    reopened = controller.Board(board.settings, board.context)
    assert reopened.show()["cloud_check"]["receipt"] == receipt and len(seen) == 5
    assert reopened.attach_check(str(RUN))["cloud_check"]["receipt"] == receipt and len(seen) == 10
    archived.unlink()
    assert reopened.show()["cloud_check"] == {"status": "stale"}


@pytest.mark.parametrize("change", ["profile", "card", "repo", "rules", "base", "files", "ref"])
def test_changed_subject_refuses_lookup_without_network(board, monkeypatch, change):
    result = complete_card(board)
    if change == "profile":
        raw = json.loads(board.settings.read_text())
        del raw["cloud_check"]
        board.settings.write_text(json.dumps(raw))
    elif change == "card":
        path = board.tracker.find_path(board.identifier)
        path.write_text(path.read_text().replace("2026-11-05", "2026-11-06", 1))
    elif change == "repo":
        git(board.repo, "remote", "set-url", "origin", "https://github.com/example/private.git")
    elif change == "rules":
        (board.workflow.parent / "test.sh").write_text("exit 0")
    elif change == "base":
        git(board.repo, "commit", "--allow-empty", "-qm", "advance")
        git(board.repo, "branch", "-f", "our-main", "HEAD")
    elif change == "files":
        work = Path(session._load(board.session_path)["execution_state"]["root"])
        (work / "src/new.py").write_text("different")
    else:
        git(board.root / "candidate.git", "update-ref", "refs/heads/candidate", result["candidate"]["base_commit"])
    monkeypatch.setattr(cloud, "verify", lambda *a: pytest.fail("Invalid subject reached network"))
    assert board.show()["cloud_check"] == {"status": "stale"}
    with pytest.raises(controller.BoardError, match="cloud_stale"):
        board.attach_check(str(RUN))


def test_edit_during_lookup_rejects_late_evidence(board, monkeypatch):
    complete_card(board)
    def intervening_edit(*args):
        board.tracker.update_fields(board.identifier, title="Другая задача")
        return {"status": "passed"}
    monkeypatch.setattr(cloud, "verify", intervening_edit)
    with pytest.raises(controller.BoardError, match="cloud_stale"):
        board.attach_check(str(RUN))
    assert not list(board.root.glob("cloud-*.json"))


def test_lookup_serializes_and_preserves_separate_reruns(board, monkeypatch):
    candidate = complete_card(board)["candidate"]
    docs = responses(candidate["commit"], candidate["tree"], candidate["base_commit"])
    seen = public_api(monkeypatch, docs)
    with session._lock(board.root / "cloud-lookup"), pytest.raises(session.SessionError, match="session_busy"):
        board.attach_check(str(RUN))
    assert not seen
    board.attach_check(str(RUN))
    jobs_path = f"actions/runs/{RUN}/attempts/{ATTEMPT}/jobs"
    jobs = docs[jobs_path]
    docs[f"actions/runs/{RUN}"]["run_attempt"] = jobs["jobs"][0]["run_attempt"] = 3
    jobs["jobs"][0]["steps"][4]["conclusion"] = "failure"
    docs[jobs_path.replace("/2/", "/3/")] = jobs
    assert board.attach_check(str(RUN))["cloud_check"]["receipt"]["status"] == "failed"
    assert len(list(board.root.glob("cloud-*.json"))) == 2
    docs[f"actions/runs/{RUN}"]["run_attempt"] = jobs["jobs"][0]["run_attempt"] = 2
    jobs["jobs"][0]["steps"][4]["conclusion"] = "success"
    with pytest.raises(controller.BoardError, match="cloud_conflict"):
        board.attach_check(str(RUN))
    assert board.show()["cloud_check"]["receipt"]["attempt"] == 3


def test_old_assignments_have_no_cloud_access(board, monkeypatch):
    assert "cloud_check" not in complete_card(board, enabled=False)
    raw = json.loads(board.settings.read_text())
    board.settings.write_text(json.dumps({**raw, "cloud_check": cloud.PROFILE}))
    monkeypatch.setattr(cloud, "verify", lambda *a: pytest.fail("Old assignment gained a new grant"))
    with pytest.raises(controller.BoardError, match="cloud_not_enabled"):
        board.attach_check(str(RUN))
