"""The service closes a checked API result only after its release is applied."""
import hashlib
import os
import subprocess
from types import SimpleNamespace

import pytest

import test_api_delivery as delivery_fixture
from test_api_delivery import action, attest, fixtures
from test_api_project import git
import api_board
import api_completion as completion
import api_publication as publication
import api_session as session
import deliver
import post_merge


board = delivery_fixture.board
world = delivery_fixture.world


def local_git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"PATH": os.defpath, "GIT_ALLOW_PROTOCOL": "file", "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_GLOBAL": os.devnull})


@pytest.fixture
def accepted(world, tmp_path, monkeypatch):
    board, hub = world
    action(board, hub, "publish-check")
    attest(board, hub, monkeypatch)
    action(board, hub, "accept")
    source = tmp_path / "service-source"
    local_git(tmp_path, "clone", "--quiet", "--branch", "our-main", str(board.repo), str(source))
    monkeypatch.setattr(completion, "SOURCE_ROOT", source)
    pr = {**hub.pr, "merged": False}
    reads = []

    def lookup(self, path):
        reads.append(pr["number"])
        assert path.endswith(f"/pulls/{pr['number']}")
        return pr

    monkeypatch.setattr(publication.Publisher, "github", lookup)
    return board, hub, source, pr, reads


def merge(accepted):
    board, hub, _, pr, _ = accepted
    local_git(board.repo, "fetch", str(hub.bare), "refs/heads/" + pr["head"]["ref"])
    git(board.repo, "switch", "our-main")
    git(board.repo, "merge", "--no-ff", "--no-edit", "FETCH_HEAD")
    sha = board._base_commit()
    pr.update(state="closed", merged=True, merged_at=session._now(), merge_commit_sha=sha)
    return sha


def apply(accepted):
    board, _, source, _, _ = accepted
    local_git(source, "pull", "--ff-only")
    sha = board._base_commit()
    post_merge.save_state(source / post_merge.STATE_REL, {str(source): sha, str(board.repo): sha})
    return sha


def status(board):
    return board.show()["release"]["completion"]["status"]


@pytest.mark.parametrize("cut", ["before_card", "after_card"])
def test_installed_group_release_finishes_once_after_interruption(accepted, monkeypatch, cut):
    board, hub, _, pr, reads = accepted
    clock = [1000]
    monkeypatch.setattr(completion, "time", SimpleNamespace(time=lambda: clock[0]))
    original_session = hashlib.sha256(board.session_path.read_bytes()).hexdigest()
    assert status(board) == "waiting_merge" and not reads
    fixtures.visit(board)
    assert status(board) == "waiting_merge" and reads == [17]
    merged = merge(accepted)
    clock[0] += completion.POLL_SECONDS + 1
    # An already-installed card can carry the pre-existing delivery note.
    receipt_path = deliver.delivered_path(board.workflow.parent, board.identifier)
    record = session._decode(session._read_state(receipt_path))
    record["merged_at"] = session._now()
    session._save(receipt_path, record)
    board.tracker.update_fields(board.identifier, description=board.card()[1] + completion.MERGED_NOTE)
    fixtures.visit(board)
    assert status(board) == "waiting_installation" and board.card()[0]["state"] == "Human Review"
    # An additional, approved release commit need not have the candidate's tree.
    (board.repo / "release-notes.txt").write_text("The combined release also updates the notes.\n")
    git(board.repo, "add", "release-notes.txt")
    git(board.repo, "commit", "-m", "Group release")
    installed = apply(accepted)
    assert installed != merged

    with monkeypatch.context() as patch:
        if cut == "before_card":
            def fail_write(*args, **kwargs):
                raise OSError("interrupted before card write")
            patch.setattr(board.tracker.__class__, "_write_ticket_with_updated_at_cas", fail_write)
        else:
            original = api_board.Board._save

            def fail_history(self, value):
                if value["board_stages"][-1] == "Done":
                    raise OSError("interrupted before history write")
                original(self, value)
            patch.setattr(api_board.Board, "_save", fail_history)
        fixtures.visit(board)
        assert board._load()["completion"]["applied_commit"] == installed
        assert board.card()[0]["state"] == ("Human Review" if cut == "before_card" else "Done")
        assert board._load()["board_stages"][-1] == "Human Review"

    reopened = api_board.Board(board.settings, board.context)
    fixtures.visit(reopened)
    assert reopened.card()[0]["state"] == "Done" and status(reopened) == "done"
    saved = reopened.path.read_bytes(), reopened.tracker.find_path(board.identifier).read_bytes()
    fixtures.visit(reopened)
    assert (reopened.path.read_bytes(), reopened.tracker.find_path(board.identifier).read_bytes()) == saved
    assert reopened._load()["board_stages"].count("Done") == 1
    assert reads == [17, 17] and hub.created == 1 and len(hub.pushes) == 2
    assert hashlib.sha256(reopened.session_path.read_bytes()).hexdigest() == original_session
    view = reopened.show()
    assert view["cloud_check"]["receipt"]["status"] == "passed"
    patch = reopened.review()
    assert patch["commit"] == pr["head"]["sha"] and patch["files"][0]["path"] == "src/new.py"


@pytest.mark.parametrize("change,reason", [
    ("closed", "completion_pr_closed"),
    ("foreign_head", "completion_pr_changed"),
    ("unrelated_merge", "completion_installation_changed"),
    ("changed_card", "completion_card_changed"),
])
def test_conflicting_evidence_never_projects_done(accepted, change, reason):
    board, _, _, pr, _ = accepted
    merge(accepted)
    apply(accepted)
    if change == "closed":
        pr.update(merged=False, merged_at=None)
    elif change == "foreign_head":
        pr["head"]["sha"] = "0" * 40
    elif change == "unrelated_merge":
        pr["merge_commit_sha"] = pr["base"]["sha"]
    else:
        board.tracker.update_fields(board.identifier, description="Changed after approval")
    fixtures.visit(board)
    assert board.card()[0]["state"] == "Human Review"
    assert board.show()["release"]["completion"]["reason"] == reason
    assert "applied_commit" not in board._load()["completion"]
