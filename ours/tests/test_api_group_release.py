"""One merged release can complete several sealed results after installation."""

import hashlib
from types import SimpleNamespace

import httpx
import pytest

import test_api_delivery as delivery_tests
import api_board
import api_completion
import api_group_release as group
import api_session as session
import post_merge
from test_api_board import approve
from test_api_completion import local_git
from test_api_project import git
from test_api_session import KEYS, frame
from test_api_workspace import tool


board = delivery_tests.board
world = delivery_tests.world
cloud = delivery_tests.cloud
visit = delivery_tests.fixtures.visit


def another_card(first, new_text, *, path="README.md"):
    ident = "TEST-2"
    (first.board_root / (ident + ".md")).write_text(
        "---\nid: TEST-2\nidentifier: TEST-2\ntitle: Another ready task\n"
        "state: Todo\nagent_kind: api-team\ncreated_at: '2026-11-05T01:00:00Z'\n---\n\nUpdate the introduction.\n",
    )
    other = api_board.Board(first.settings, {**first.context, "identifier": ident})
    approve(other)
    digest = hashlib.sha256((first.repo / path).read_bytes()).hexdigest()
    replies = iter([frame(None, [tool("write_project_file", "file", path=path,
                                     text=new_text, expected_sha256=digest)]), frame("Готово")])
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=next(replies)))
    other.run("b" * 32, credentials=lambda _: KEYS, transport=transport)
    return other


def merged_release(board, files, *, with_revert=False):
    base = board._base_commit()
    git(board.repo, "switch", "-c", "codex/combined", base)
    if with_revert:
        source = board._load()["candidate"]["commit"]
        local_git(board.repo, "fetch", str(board.root / "candidate.git"), source)
        git(board.repo, "merge", "--ff-only", "FETCH_HEAD")
        git(board.repo, "revert", "--no-edit", "HEAD")
    for path, text in {"release-notes.txt": "One release.\n", **files}.items():
        (board.repo / path).write_text(text)
    git(board.repo, "add", "-A")
    git(board.repo, "commit", "-qm", "Common release")
    head = git(board.repo, "rev-parse", "HEAD").decode().strip()
    git(board.repo, "switch", "our-main")
    git(board.repo, "merge", "--no-ff", "--no-edit", head)
    merge = board._base_commit()
    identity = {"id": cloud.REPO_ID, "full_name": cloud.REPO}
    pr = {"number": 21, "html_url": f"https://github.com/{cloud.REPO}/pull/21", "state": "closed",
          "merged": True, "merged_at": session._now(), "merge_commit_sha": merge,
          "head": {"sha": head, "repo": identity}, "base": {"sha": base, "ref": "our-main", "repo": identity}}

    def github(path):
        assert path == f"repos/{cloud.REPO}/pulls/21"
        return pr

    return head, merge, SimpleNamespace(github=github), pr


def mark_installed(board, source, merge):
    local_git(source, "pull", "--ff-only")
    assert board._base_commit() == merge
    post_merge.save_state(source / post_merge.STATE_REL, {str(source): merge, str(board.repo): merge})


def test_common_release_closes_both_cards_and_preserves_old_proof(world, tmp_path, monkeypatch):
    first, _ = world
    git(first.repo, "switch", "our-main")
    original_text = "Old introduction.\n\n" + "\n".join(f"Section {n}." for n in range(12)) + "\n"
    (first.repo / "README.md").write_text(original_text)
    git(first.repo, "commit", "-qam", "Prepare release base")
    result_text = original_text.replace("Old introduction.", "Reviewed introduction.")
    second = another_card(first, result_text)
    source = tmp_path / "service-source"
    local_git(tmp_path, "clone", "--quiet", "--branch", "our-main", str(first.repo), str(source))
    monkeypatch.setattr(api_completion, "SOURCE_ROOT", source)
    history = first.root / "cloud-12345-2.json"
    session._save(history, {"evidence": {"status": "failed"}})
    original = history.read_bytes()
    sessions = {b.identifier: hashlib.sha256(b.session_path.read_bytes()).digest() for b in (first, second)}
    first.tracker.update_fields(first.identifier, state="needs_human")
    first.workflow.write_text(first.workflow.read_text() + "\nTechnical settings changed.\n")
    assert first.show()["cloud_check"]["status"] == "stale"
    head, _, remote, _ = merged_release(first, {"src/new.py": "answer = 42\n", "README.md": result_text})

    shown = group.preview([first, second], 21, head, publisher=remote)
    assert not group._ledger_path(first).exists()
    with pytest.raises(group.ReleaseError, match="group_release_offer_changed"):
        group.record([first], 21, head, shown["offer"], publisher=remote)
    group.record([second, first], 21, head, shown["offer"], publisher=remote)
    monkeypatch.setattr(cloud, "discover", lambda *a: pytest.fail("Group requires no per-card cloud check"))
    visit(first)
    assert first.show()["release"]["completion"]["status"] == "waiting_installation"
    assert first.card()[0]["state"] == "needs_human"
    assert second.card()[0]["state"] == "Verify"

    (first.repo / "README.md").write_text(result_text + "\nAn independent update.\n")
    git(first.repo, "commit", "-qam", "Extend installed README")
    installed = first._base_commit()
    mark_installed(first, source, installed)
    visit(first)
    for b in (first, second):
        assert b.card()[0]["state"] == "Done"
        assert b.show()["release"]["completion"]["applied_commit"] == installed
        assert b.show()["release"]["pr"]["number"] == 21
        assert b.review()["commit"] == b._load()["candidate"]["commit"]
        assert hashlib.sha256(b.session_path.read_bytes()).digest() == sessions[b.identifier]
        assert b._load()["board_stages"].count("Done") == 1
    assert history.read_bytes() == original
    assert group.record([first, second], 21, head, shown["offer"], publisher=None) == shown
    visit(api_board.Board(first.settings, first.context))
    assert all(b._load()["board_stages"].count("Done") == 1 for b in (first, second))


@pytest.mark.parametrize("case", ["missing", "reverted", "foreign_pr"])
def test_group_refuses_a_release_that_cannot_prove_inclusion(world, case):
    board, _ = world
    files = {"src/new.py": "answer = 42\n"} if case == "foreign_pr" else {}
    head, _, remote, pr = merged_release(board, files, with_revert=case == "reverted")
    if case == "foreign_pr":
        pr["head"]["repo"] = {"id": 55, "full_name": "another/repository"}
    reason = "group_release_pr_changed" if case == "foreign_pr" else "group_release_missing_changes"
    with pytest.raises(group.ReleaseError, match=reason):
        group.preview([board], 21, head, publisher=remote)
    assert not group._ledger_path(board).exists()


@pytest.mark.parametrize("change", ["card", "installed_file"])
def test_group_completion_requires_the_same_card_and_released_files(world, tmp_path, monkeypatch, change):
    board, _ = world
    source = tmp_path / "service-source"
    local_git(tmp_path, "clone", "--quiet", "--branch", "our-main", str(board.repo), str(source))
    monkeypatch.setattr(api_completion, "SOURCE_ROOT", source)
    head, merge, remote, _ = merged_release(board, {"src/new.py": "answer = 42\n"})
    shown = group.preview([board], 21, head, publisher=remote)
    group.record([board], 21, head, shown["offer"], publisher=remote)
    if change == "card":
        board.tracker.update_fields(board.identifier, description="Changed after approval")
    else:
        (board.repo / "src/new.py").write_text("answer = 0\n")
        git(board.repo, "commit", "-qam", "Changed installed file")
        merge = board._base_commit()
    mark_installed(board, source, merge)
    visit(board)
    assert board.card()[0]["state"] == "Verify"
    reason = "completion_card_changed" if change == "card" else "completion_installation_changed"
    assert board.show()["release"]["completion"]["reason"] == reason
