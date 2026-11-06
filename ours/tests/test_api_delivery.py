"""End-to-end host acceptance against real Git refs, with GitHub API replies offline."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

import test_api_cloud_check as fixtures
from test_api_project import git
import api_board
import api_cloud_check as cloud
import api_delivery as release
import api_publication as publication
import api_session as session
import deliver
from symphony import api_team
from symphony.workflow import WorkflowState

board = fixtures.board


def publish_base(repo, bare):
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo),
                    "push", "-q", str(bare), "refs/heads/our-main:refs/heads/our-main"], check=True,
                   capture_output=True, env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1",
                                            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ALLOW_PROTOCOL": "file"})


class FakeGitHub:
    def __init__(self, root, board):
        self.bare = root / "remote.git"
        git(root, "init", "-q", "--bare", str(self.bare))
        publish_base(board.repo, self.bare)
        self.pr = None
        self.pushes = []
        self.created = 0
        self.fail_create = False
        self.lose_create_reply = False
        self.private = False
        self.before_push = None
        self.pr_wrong_head = False
        ops = deliver.Ops(proc=self.proc, remote_url=lambda _: str(self.bare))
        self.publisher = publication.Publisher(board, gh="/fake/gh", ops=ops)

    def ref(self, branch):
        proc = subprocess.run(["git", "-C", str(self.bare), "show-ref", "--verify", "--hash",
                               "refs/heads/" + branch], capture_output=True, text=True)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def proc(self, argv, cwd=None, env=None, timeout=120):
        if argv[0] == "git":
            if "push" in argv:
                self.pushes.append(argv[-1])
                if self.before_push:
                    callback, self.before_push = self.before_push, None
                    callback()
            return deliver.run_process(argv, cwd, {**env, "GIT_ALLOW_PROTOCOL": "file"}, timeout)
        assert argv[0] == "/fake/gh"
        repo = {"id": cloud.REPO_ID, "full_name": cloud.REPO, "private": self.private}
        if argv[1] == "api":
            path = argv[-1]
            if path == "repos/" + cloud.REPO:
                result = repo
            elif "/pulls?" in path:
                result = [self.pr] if self.pr else []
            else:
                assert path.endswith("/pulls/17") and self.pr
                result = self.pr
                if self.pr_wrong_head:
                    result = {**result, "head": {**result["head"], "sha": "0" * 40}}
            return deliver.Proc(0, json.dumps(result))
        assert argv[1:3] == ["pr", "create"]
        if self.fail_create:
            return deliver.Proc(1)
        branch, base = (argv[argv.index(flag) + 1] for flag in ("--head", "--base"))
        body = Path(argv[argv.index("--body-file") + 1]).read_text()
        assert self.pr is None and self.ref(branch) in body
        assert board_secret not in body
        self.pr = {"number": 17, "html_url": f"https://github.com/{cloud.REPO}/pull/17", "state": "open",
                   "head": {"ref": branch, "sha": self.ref(branch), "repo": repo},
                   "base": {"ref": base, "sha": self.ref(base), "repo": repo}}
        self.created += 1
        return deliver.Proc(1) if self.lose_create_reply else deliver.Proc(0, self.pr["html_url"] + "\n")


board_secret = "Only the operator should see this task text"


@pytest.fixture()
def world(board, tmp_path, monkeypatch):
    command = shlex.join([str(Path(sys.prefix) / "bin/python"), "-B", str(Path(api_board.__file__).resolve()),
                          "--settings", str(board.settings)])
    board.workflow.write_text("---\ntracker: {kind: file, board_root: ./kanban, "
        "active_states: [Todo, In Progress, Verify, Document], terminal_states: [Human Review, needs_human, Done, Archive]}\n"
        f"workspace: {{code_repo: {json.dumps(str(board.repo))}, root: {json.dumps(str(tmp_path / 'workspace'))}}}\n"
        "agent:\n  kind: api-team\n  auto_merge_on_done: false\n  feature_base_branch: our-main\n  profiles:\n"
        f"    api-team:\n      format: api-team-v1\n      command: {json.dumps(command)}\n---\nLocal fixture\n")
    cfg, error = WorkflowState(board.workflow).reload()
    assert cfg and not error
    board = api_board.Board(board.settings, api_team.board_context(cfg, "api-team", board.identifier))
    board.tracker.update_fields(board.identifier, state="Todo", description=board_secret)
    configure = fixtures.capture_tests.configure
    def with_delivery(board, checks=""):
        configure(board, checks)
        with (board.workflow.parent / "project.toml").open("a") as fh:
            fh.write(f'\n[delivery]\nrepo="{cloud.REPO}"\nbase="our-main"\n')
    monkeypatch.setattr(fixtures.capture_tests, "configure", with_delivery)
    fixtures.complete_card(board)
    return board, FakeGitHub(tmp_path, board)


def action(board, hub, op):
    choice = board.show()["release"]
    assert choice["stage"] == op
    release.act(board, op, choice["offer"], publisher=hub.publisher)
    return choice


def attest(board, hub, monkeypatch, *, run=fixtures.RUN, expected_stage="accept"):
    value = board._load()
    sub = board._cloud_subject(value)
    sha, tree, base = sub["commit"], sub["tree"], sub["base_commit"]
    docs = fixtures.responses(sha, tree, base)
    marker = value["release_check"]["sha"]
    branch = cloud.CHECK_PREFIX + sha
    assert hub.ref(branch) == marker
    raw = git(hub.bare, "show", marker + ":" + cloud.WORKFLOW)
    blob = docs[f"git/blobs/{fixtures.BLOB}"]
    blob.update(content=base64.b64encode(raw).decode(), size=len(raw))
    path = f"actions/runs/{fixtures.RUN}"
    record = docs.pop(path)
    record.update(id=run, head_branch=branch, head_sha=marker)
    docs[f"git/trees/{marker}?recursive=1"] = docs.pop(f"git/trees/{fixtures.HEAD}?recursive=1")
    docs[f"actions/runs/{run}"] = record
    jobs = docs.pop(path + f"/attempts/{fixtures.ATTEMPT}/jobs")
    jobs["jobs"][0].update(run_id=run, head_sha=marker)
    docs[f"actions/runs/{run}/attempts/{fixtures.ATTEMPT}/jobs"] = jobs
    if "source_commit" in sub:
        docs[f"git/commits/{sha}"]["parents"].insert(0, {"sha": sub["source_commit"]})
    row = {**record, "run_number": run, "display_title": sha, "created_at": session._now()}
    docs[fixtures.index_path()] = {"workflow_runs": [row]}
    fixtures.public_api(monkeypatch, docs)
    # A new controller after restart discovers the run; no manual run number.
    for path in (board.root.parent).glob("cloud-index-*.json"):
        scan = session._decode(session._read_state(path))
        scan["next"] = 0
        session._save(path, scan)
    fixtures.visit(api_board.Board(board.settings, board.context))
    assert board.card()[0]["state"] == "Human Review"
    assert board.show()["release"]["stage"] == expected_stage
    return docs


def advance(board, hub, name="extra.txt"):
    (board.repo / name).write_text("base changed\n")
    git(board.repo, "add", name)
    git(board.repo, "commit", "-qm", "new public base")
    git(board.repo, "branch", "-f", "our-main", "HEAD")
    publish_base(board.repo, hub.bare)
    return board._base_commit()


def test_checked_version_only_after_approval_and_one_pr_after_restart(world, monkeypatch):
    board, hub = world
    session_hash = hashlib.sha256(board.session_path.read_bytes()).digest()
    refs = git(board.repo, "show-ref")
    initial = board.show()["release"]
    assert initial["stage"] == "publish-check" and initial["files"] == ["src/new.py"]
    with pytest.raises(release.ReleaseError, match="offer_changed"):
        release.act(board, "publish-check", "0" * 64, publisher=hub.publisher)
    assert hub.pushes == [] and not hub.pr
    action(board, hub, "publish-check")
    release.act(board, "publish-check", initial["offer"], publisher=hub.publisher)
    marker = hub.ref(initial["branch"])
    assert git(hub.bare, "rev-parse", marker + "^").decode().strip() == initial["commit"]
    assert git(hub.bare, "rev-parse", marker + ":ours/ci/linux").decode().strip() == cloud.TOOLS_TREE
    assert set(git(hub.bare, "ls-tree", "-r", "--name-only", marker).decode().splitlines()) == {
        ".gitattributes", cloud.WORKFLOW, *("ours/ci/linux/" + x for x in publication.CI_FILES)}
    attest(board, hub, monkeypatch)
    choice = action(board, hub, "accept")
    reopened = api_board.Board(board.settings, board.context)
    release.act(reopened, "accept", choice["offer"], publisher=hub.publisher)
    result = reopened.show()
    assert result["accepted"] and result["delivered"] and result["release"]["pr"]["number"] == 17
    rec = session._decode(session._read_state(deliver.delivered_path(board.workflow.parent, board.identifier)))
    assert rec["api_proof"]["commit"] == initial["commit"] == hub.ref("deliver/TEST-1")
    assert rec["source_tree"] == choice["tree"] and not rec["recheck"]
    assert len(hub.pushes) == 2 and hub.created == 1 and board.card()[0]["state"] == "Human Review"
    assert hashlib.sha256(board.session_path.read_bytes()).digest() == session_hash
    assert git(board.repo, "show-ref") == refs and hub.ref("our-main") == initial["base_commit"]


def test_moved_base_requires_new_public_check_and_new_human_choice(world, monkeypatch):
    board, hub = world
    original = action(board, hub, "publish-check")
    attest(board, hub, monkeypatch)
    old_choice = board.show()["release"]
    old_proof = (board.root / f"cloud-{fixtures.RUN}-{fixtures.ATTEMPT}.json").read_bytes()
    base = advance(board, hub)
    view = board.show()
    target = view["release"]
    assert board.card()[0]["state"] == "Verify" and target["stage"] == "publish-check"
    assert target["commit"] != original["commit"] and target["rebuilt"] and target["base_commit"] == base
    assert view["cloud_check"]["receipt"] is None and not view["accepted"] and not view["delivered"]
    with pytest.raises(release.ReleaseError, match="offer_changed"):
        release.act(board, "accept", old_choice["offer"], publisher=hub.publisher)
    parents = git(board.root / "candidate.git", "show", "-s", "--format=%P", target["commit"]).decode().split()
    assert parents == [original["commit"], base]
    assert board.review()["commit"] == target["commit"] and board.review()["files"][0]["path"] == "src/new.py"
    action(board, hub, "publish-check")
    attest(board, hub, monkeypatch, run=fixtures.RUN + 1)
    action(board, hub, "accept")
    assert board.show()["delivered"] and hub.ref("deliver/TEST-1") == target["commit"]
    assert (board.root / f"cloud-{fixtures.RUN}-{fixtures.ATTEMPT}.json").read_bytes() == old_proof
    assert board._load()["board_stages"][-3:] == ["Human Review", "Verify", "Human Review"]


@pytest.mark.parametrize("mode", ["after_push", "lost_pr_response", "wrong_pr_head"])
def test_recover_delivery_without_rewriting_branch_or_opening_another_pr(world, monkeypatch, mode):
    board, hub = world
    action(board, hub, "publish-check")
    attest(board, hub, monkeypatch)
    choice = board.show()["release"]
    hub.fail_create = mode == "after_push"
    hub.lose_create_reply = mode == "lost_pr_response"
    hub.pr_wrong_head = mode == "wrong_pr_head"
    with pytest.raises((deliver.Failed, release.ReleaseError)):
        release.act(board, "accept", choice["offer"], publisher=hub.publisher)
    assert board.show()["accepted"] and not board.show()["delivered"]
    assert hub.ref("deliver/TEST-1") == choice["commit"] and len(hub.pushes) == 2
    hub.fail_create = hub.lose_create_reply = hub.pr_wrong_head = False
    reopened = api_board.Board(board.settings, board.context)
    release.act(reopened, "accept", choice["offer"], publisher=hub.publisher)
    assert reopened.show()["delivered"] and hub.created == 1 and len(hub.pushes) == 2


@pytest.mark.parametrize("change", ["private", "remote_base", "occupied", "race"])
def test_refusal_does_not_publish_or_replace_an_existing_ref(world, change):
    board, hub = world
    choice = board.show()["release"]
    base = choice["base_commit"]
    branch = "refs/heads/" + choice["branch"]
    if change == "private":
        hub.private = True
    elif change == "remote_base":
        git(hub.bare, "update-ref", "-d", "refs/heads/our-main")
    elif change == "occupied":
        git(hub.bare, "update-ref", branch, base)
    else:
        hub.before_push = lambda: git(hub.bare, "update-ref", branch, base)
    with pytest.raises(release.ReleaseError):
        release.act(board, "publish-check", choice["offer"], publisher=hub.publisher)
    assert hub.ref(choice["branch"]) in (None, base)
    assert not board.show()["accepted"] and not hub.pr


def test_merge_conflict_routes_to_human_and_keeps_original(world):
    board, hub = world
    original = board.show()["release"]
    advance(board, hub, "src/new.py")
    result = board.show()
    assert result["release"] == {"stage": "blocked", "reason": "release_merge_conflict",
        "message": api_board.PUBLIC_ERRORS["release_merge_conflict"]}
    assert board.card()[0]["state"] == "needs_human" and not hub.pushes
    assert git(board.root / "candidate.git", "rev-parse", "candidate").decode().strip() == original["commit"]


def test_branch_move_after_approval_and_newer_check_are_refused(world, monkeypatch):
    board, hub = world
    action(board, hub, "publish-check")
    docs = attest(board, hub, monkeypatch)
    choice = board.show()["release"]
    docs[f"actions/runs/{fixtures.RUN}"]["status"] = "in_progress"
    with pytest.raises(cloud.CloudError, match="cloud_incomplete"):
        release.act(board, "accept", choice["offer"], publisher=hub.publisher)
    assert not hub.pr and not board.show()["accepted"] and hub.ref("deliver/TEST-1") is None


def test_release_marker_refuses_changed_recipe(world, tmp_path):
    board, _ = world
    root = tmp_path / "fork"
    recipe = root / cloud.WORKFLOW
    recipe.parent.mkdir(parents=True)
    recipe.write_bytes((publication.FORK / cloud.WORKFLOW).read_bytes().replace(b"permissions: {}", b"permissions: write-all"))
    with pytest.raises(release.ReleaseError, match="recipe_changed"):
        publication.marker(board.root / "candidate.git", board.show()["release"]["commit"], fork=root)


def test_merged_offer_shows_both_versions_before_the_first_publication(world):
    board, hub = world
    original = board.show()["release"]
    advance(board, hub)
    merged = board.show()["release"]
    assert not hub.pushes and merged["original"]["commit"] == original["commit"]
    assert merged["original"]["files"] == original["files"]
    assert board.review(version="original")["commit"] == original["commit"]
    assert board.review()["commit"] == merged["commit"]
    with pytest.raises(release.ReleaseError, match="offer_changed"):
        release.act(board, "publish-check", original["offer"], publisher=hub.publisher)
    action(board, hub, "publish-check")
    assert hub.ref(merged["branch"]) is not None


def test_a_new_independent_run_supersedes_the_pass_presented_for_acceptance(world, monkeypatch):
    board, hub = world
    action(board, hub, "publish-check")
    docs = attest(board, hub, monkeypatch)
    choice = board.show()["release"]
    previous = docs[fixtures.index_path()]["workflow_runs"][0]
    docs[fixtures.index_path()]["workflow_runs"].insert(0, {**previous, "id": fixtures.RUN + 1,
        "run_number": fixtures.RUN + 1, "status": "in_progress", "conclusion": None})
    with pytest.raises(release.ReleaseError, match="receipt_changed"):
        release.act(board, "accept", choice["offer"], publisher=hub.publisher)
    assert not board.show()["accepted"] and hub.ref("deliver/TEST-1") is None


def test_lost_branch_response_recovers_only_the_authorized_ref(world, monkeypatch):
    board, hub = world
    action(board, hub, "publish-check")
    attest(board, hub, monkeypatch)
    choice = board.show()["release"]
    create = hub.publisher.create_branch
    def lost(branch, sha):
        create(branch, sha)
        raise release.ReleaseError("release_branch_unconfirmed")
    monkeypatch.setattr(hub.publisher, "create_branch", lost)
    with pytest.raises(release.ReleaseError, match="branch_unconfirmed"):
        release.act(board, "accept", choice["offer"], publisher=hub.publisher)
    assert not board._load()["release_accept"]["published"] and hub.ref(choice["branch"]) == choice["commit"]
    reopened = api_board.Board(board.settings, board.context)
    action(reopened, hub, "accept")
    assert reopened.show()["delivered"] and hub.created == 1 and len(hub.pushes) == 2
    # A missing receipt must not be reported as a successful delivery.
    deliver.delivered_path(board.workflow.parent, board.identifier).unlink()
    assert not reopened.show()["delivered"]


def test_changed_branch_during_delivery_never_gets_a_pr(world, monkeypatch):
    board, hub = world
    action(board, hub, "publish-check")
    attest(board, hub, monkeypatch)
    choice = board.show()["release"]
    hub.before_push = lambda: advance(board, hub)
    with pytest.raises(release.ReleaseError, match="base_not_synced"):
        release.act(board, "accept", choice["offer"], publisher=hub.publisher)
    assert hub.ref(choice["branch"]) == choice["commit"] and not hub.pr
    result = board.show()
    assert result["release"]["reason"] == "release_base_after_acceptance"
    assert board.card()[0]["state"] == "needs_human" and not result["delivered"]


def test_pending_lane_is_repaired_after_a_restart_without_payment(world, monkeypatch):
    board, hub = world
    action(board, hub, "publish-check")
    attest(board, hub, monkeypatch)
    advance(board, hub)
    def lost_write(*args):
        raise OSError("lost state write")
    monkeypatch.setattr(board, "_release_lane", lost_write)
    assert board.show()["release"]["stage"] == "blocked"
    assert board._load()["release_lane_pending"] == "Verify"
    recovered = api_board.Board(board.settings, board.context)
    assert recovered.show()["release"]["stage"] == "publish-check"
    assert recovered.card()[0]["state"] == "Verify" and "release_lane_pending" not in recovered._load()


def test_unsupported_delivery_does_not_break_existing_public_check(world, monkeypatch):
    board, hub = world
    # The PILOT can monitor a previously public candidate without being
    # eligible to publish or deliver. Preserve its existing receipt path.
    action(board, hub, "publish-check")
    def unavailable(*args):
        raise release.ReleaseError("release_destination_invalid")
    monkeypatch.setattr(release, "pairing", unavailable)
    attest(board, hub, monkeypatch, expected_stage="blocked")
    assert board.card()[0]["state"] == "Human Review"
    assert board.show()["cloud_check"]["receipt"]["status"] == "passed"
    assert board.show()["release"]["reason"] == "release_destination_invalid"


def test_crash_after_pr_record_recovers_after_base_has_moved(world, monkeypatch):
    board, hub = world
    action(board, hub, "publish-check")
    attest(board, hub, monkeypatch)
    choice = action(board, hub, "accept")
    state = board._load()
    state.pop("release_done")
    board._save(state)
    advance(board, hub)
    restarted = api_board.Board(board.settings, board.context)
    assert restarted.show()["delivered"] and restarted.card()[0]["state"] == "Human Review"
    release.act(restarted, "accept", choice["offer"], publisher=hub.publisher)
    assert len(hub.pushes) == 2 and hub.created == 1


def test_changed_settings_or_private_ref_never_accept_old_offer(world, monkeypatch):
    board, hub = world
    advance(board, hub)
    action(board, hub, "publish-check")
    attest(board, hub, monkeypatch)
    view = board.show()["release"]
    git(board.root / "candidate.git", "update-ref", "refs/api-release/" + view["commit"], view["base_commit"])
    with pytest.raises(api_board.BoardError, match="cloud_stale"):
        release.act(board, "accept", view["offer"], publisher=hub.publisher)
    assert not hub.pr


def test_new_attempt_requires_fresh_approval_and_preserves_prior_acceptance(world, monkeypatch):
    board, hub = world
    action(board, hub, "publish-check")
    attest(board, hub, monkeypatch)
    first = board.show()["release"]
    hub.fail_create = True
    with pytest.raises(deliver.Failed):
        release.act(board, "accept", first["offer"], publisher=hub.publisher)
    attest(board, hub, monkeypatch, run=fixtures.RUN + 1)
    new = board.show()["release"]
    assert new["offer"] != first["offer"] and not new["accepted"]
    with pytest.raises(release.ReleaseError, match="offer_changed"):
        release.act(board, "accept", first["offer"], publisher=hub.publisher)
    hub.fail_create = False
    action(board, hub, "accept")
    assert board.show()["delivered"] and len(list(board.root.glob("release_accept-*.json"))) == 2
    assert len(hub.pushes) == 2 and hub.created == 1


def test_legacy_delivery_respects_current_executor_after_an_api_attempt(world, monkeypatch):
    board, hub = world
    action(board, hub, "publish-check")
    attest(board, hub, monkeypatch)
    messages = []
    old = deliver.Delivery(board.identifier, board.workflow.parent.parent, None, "/fake/gh",
                           hub.publisher.ops, messages.append)
    assert old.push() == deliver.EXIT_REFUSED
    assert any("панель приёмки" in m for m in messages) and hub.ref("deliver/TEST-1") is None
    board.tracker.update_fields(board.identifier, agent_kind="claude")
    messages.clear()
    assert old.push() == deliver.EXIT_REFUSED
    assert any("нет ветки symphony/TEST-1" in m for m in messages)
