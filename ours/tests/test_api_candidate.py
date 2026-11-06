"""Real private Git candidates, old policy rules and no false delivery proof."""

import json
from pathlib import Path
import sys

import httpx
import pytest

import test_api_board as board_tests
from test_api_project import git
from test_api_session import KEYS, frame
from test_api_workspace import tool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import api_candidate as candidate  # noqa: E402
import api_session as session  # noqa: E402
import api_workspace as workspace  # noqa: E402
import deliver  # noqa: E402

board = board_tests.board
approve = board_tests.approve


def configure(board, checks=""):
    (board.workflow.parent / "project.toml").write_text(
        '[project]\nname="fixture"\nrepo=' + json.dumps(str(board.repo)) + '\n'
        'base_branch="our-main"\nid_prefix="TEST"\nport=9990\n'
        f'[tools]\npython={json.dumps(sys.executable)}\nsymphony="/bin/true"\nclaude="/bin/true"\n'
        f'[checks]\n{checks}\n')
    (board.workflow.parent / "test.sh").write_text("exit 29 # must never execute during capture\n")


def start(board, files=None, checks=""):
    for name, data in (files or {}).items():
        dest = board.repo / name
        dest.parent.mkdir(exist_ok=True, parents=True)
        dest.write_bytes(data if isinstance(data, bytes) else data.encode())
    git(board.repo, "add", "-A")
    git(board.repo, "commit", "--allow-empty", "-qm", "fixture for candidate")
    git(board.repo, "branch", "-f", "our-main", "HEAD")
    raw = json.loads(board.settings.read_text())
    raw["paths"] = ["."]
    board.settings.write_text(json.dumps(raw))
    configure(board, checks)
    session.create(board.session_path, board.proposal()["spec"])
    state = session._load(board.session_path)
    state["status"] = "dialogue_complete"
    session._save(board.session_path, state)
    return state


def edit(state, name, text=None, *, delete=False):
    spec, source = state["spec"], state["execution_state"]
    files = workspace.manifest(spec, source)
    args = {"path": name, "expected_sha256": files.get(name, {}).get("sha256", "")}
    if not delete:
        args["text"] = text
    workspace.call(spec, source, "delete_project_file" if delete else "write_project_file", args)


def capture(board, **kwargs):
    return candidate.prepare(board.session_path, board.workflow.parent, board.identifier,
                             candidate.rules_snapshot(board.workflow.parent), **kwargs)


def test_real_candidate_keeps_base_and_source_untouched_without_pass_or_delivery(board):
    state = start(board, {"tests/test_old.py": "assert 1\n", "docs/other.md": "base\n"})
    before_refs = git(board.repo, "show-ref")
    before_status = git(board.repo, "status", "--porcelain")
    edit(state, "src/main.py", "answer = 42\n")
    edit(state, "tests/test_new.py", "from main import answer\nassert answer == 42\n")
    report = capture(board)
    assert report["status"] == "awaiting_full_checks"
    assert report["checks"] == {"static": "passed", "full": "not_run"}
    assert report["accepted"] is report["delivered"] is False
    repo = Path(report["repository"])
    assert git(repo, "rev-parse", report["commit"] + "^").decode().strip() == report["base_commit"]
    assert git(repo, "show", report["commit"] + ":src/main.py") == b"answer = 42\n"
    assert git(repo, "show", report["commit"] + ":docs/other.md") == b"base\n"
    assert git(repo, "show", report["commit"] + ":tests/test_old.py") == b"assert 1\n"
    assert git(board.repo, "show-ref") == before_refs
    assert git(board.repo, "status", "--porcelain") == before_status
    assert not (board.root.parent.parent / "check.log").exists()
    assert deliver.green_check_problem([], report["tree"]) is not None
    edit(state, "src/main.py", "changed after capture\n")
    assert git(repo, "show", report["commit"] + ":src/main.py") == b"answer = 42\n"
    assert json.loads((board.root / "candidate.json").read_text()) == report


@pytest.mark.parametrize("name,code", [
    ("ours/hooks/guard.py", "protected_path"), ("ours/bin/new-tool", "protected_path"),
    ("ours/templates/new.tmpl", "protected_path"), ("AGENTS.md", "protected_path"),
    ("docs/AGENTS.md", "protected_path"), ("tests/test_old.py", "existing_test_changed"),
])
@pytest.mark.parametrize("delete", [False, True])
def test_old_guards_block_modifications_and_deletions_even_with_ours_open(board, name, code, delete):
    state = start(board, {name: "original\n"}, checks='allow_paths=["ours/**"]')
    edit(state, name, "replacement\n", delete=delete)
    report = capture(board)
    assert report["status"] == "blocked"
    assert {"code": code, "path": name} in report["issues"]
    assert not (board.root / "candidate.git").exists()


def test_existing_test_exception_comes_only_from_trusted_board_settings(board):
    name = "tests/test_old.py"
    state = start(board, {name: "original\n"}, checks='allow_modified_tests=["tests/test_old.py"]')
    edit(state, name, "explicitly permitted\n")
    assert capture(board)["status"] == "awaiting_full_checks"


@pytest.mark.parametrize("prefix,binary", [("sk-ant-", False), ("sk-proj-", False), ("sk-", True)])
def test_key_like_values_are_blocked_without_persisting_values(board, prefix, binary):
    state = start(board)
    secret = (prefix + "z" * 35).encode()
    location = Path(state["execution_state"]["root"]) / "src/secret.txt"
    location.write_bytes((b"\0" if binary else b"") + secret)
    report = capture(board)
    assert {"code": "possible_api_key", "path": "src/secret.txt"} in report["issues"]
    assert secret not in json.dumps(report).encode()
    assert secret not in candidate.render_report(report).encode()
    assert not (board.root / "candidate.git").exists()


def test_permissions_and_modes_and_new_unicode_file_are_preserved(board):
    state = start(board, {"src/delete.py": "delete me\n"})
    edit(state, "src/delete.py", delete=True)
    name = "src/новый файл,2.sh"
    edit(state, name, "#!/bin/sh\nexit 0\n")
    (Path(state["execution_state"]["root"]) / name).chmod(0o755)
    report = capture(board)
    rows = git(Path(report["repository"]), "ls-tree", "-r", "-z", report["commit"])
    assert b"src/delete.py" not in rows
    assert any(row.startswith(b"100755") and row.endswith(name.encode()) for row in rows.split(b"\0"))


@pytest.mark.parametrize("kind", ["symlink", "large"])
def test_omitted_base_file_cannot_be_replaced_as_a_new_file(board, kind):
    name = "src/omitted"
    if kind == "symlink":
        (board.repo / name).symlink_to("main.py")
    else:
        (board.repo / name).write_bytes(b"z" * (candidate.project.MAX_FILE_BYTES + 1))
    state = start(board)
    assert name not in state["execution_state"]["initial"]
    edit(state, name, "replacement\n")
    report = capture(board)
    assert {"code": "unavailable_base_file", "path": name} in report["issues"]
    assert not (board.root / "candidate.git").exists()


def test_git_filters_and_hooks_and_host_environment_do_not_execute(board, monkeypatch):
    marker = board.root / "unwanted-command"
    state = start(board, {".gitattributes": "*.py filter=evil\n"})
    git(board.repo, "config", "filter.evil.clean", f"touch {marker}")
    hook = board.repo / ".git/hooks/post-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"touch {marker}")
    edit(state, "src/main.py", "print('unchanged by filter')\n")
    report = capture(board)
    assert report["status"] == "awaiting_full_checks" and not marker.exists()
    assert git(Path(report["repository"]), "show", report["commit"] + ":src/main.py") == b"print('unchanged by filter')\n"


def test_capture_requires_complete_sealed_result_and_honours_stop(board):
    state = start(board)
    edit(state, "src/main.py", "new\n")
    report = capture(board, cancelled=lambda: True)
    assert report["issues"] == [{"code": "operator_stopped"}]
    state["execution_state"]["sealed"] = False
    session._save(board.session_path, state)
    report = capture(board)
    assert report["status"] == "blocked" and not (board.root / "candidate.git").exists()


def test_rules_change_invalidates_confirmation_and_completed_capture(board):
    configure(board)
    token = board.prepare()["confirmation_token"]
    (board.workflow.parent / "test.sh").write_text("changed rules\n")
    with pytest.raises(board_tests.board_api.BoardError, match="api_assignment_changed"):
        board.confirm(token)
    start(board)
    rules = candidate.rules_snapshot(board.workflow.parent)
    (board.workflow.parent / "project.toml").write_text("changed rules\n")
    report = candidate.prepare(board.session_path, board.workflow.parent, board.identifier, rules)
    assert report["issues"] == [{"code": "api_check_rules_changed"}]
    assert not (board.root / "candidate.git").exists()


def test_board_exposes_real_candidate_and_stays_unaccepted(board):
    configure(board)
    approve(board)
    replies = [frame(None, [tool("write_project_file", "file", path="src/new.py", text="answer = 42\n", expected_sha256="")]),
               frame("Готово. Добавлен файл.")]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=replies.pop(0)))
    result = board.run("a" * 32, credentials=lambda spec: KEYS, transport=transport)
    assert result["candidate"]["status"] == "awaiting_full_checks"
    assert result["candidate"]["file_count"] == 1 and "files" not in result["candidate"]
    assert result["delivery_status"] == "awaiting_full_checks"
    assert "полный прогон ещё не выполнен" in result["report_text"]
    assert result["accepted"] is result["delivered"] is False
    assert board.card()[0]["state"] == "needs_human"
    assert not replies
    assert git(board.repo, "branch", "--list", "symphony/*") == b""
    assert board.review()["revision"] == result["candidate"]["revision"]
    state = session._load(board.session_path)
    edit(state, "src/new.py", "changed afterwards\n")
    with pytest.raises(board_tests.board_api.BoardError, match="api_candidate_result_changed"):
        board.review()


def test_capture_detects_changed_file_during_read(board, monkeypatch):
    state = start(board)
    edit(state, "src/main.py", "one\n")
    original = workspace.read
    def changed(spec, value, name):
        data, mode = original(spec, value, name)
        return data + b"tampered", mode
    monkeypatch.setattr(workspace, "read", changed)
    report = capture(board)
    assert report["status"] == "blocked" and not (board.root / "candidate.git").exists()


def test_no_changes_does_not_create_empty_commit_or_accept_task(board):
    start(board)
    report = capture(board)
    assert report["status"] == "no_changes" and report["accepted"] is False
    assert not (board.root / "candidate.git").exists()


def test_file_directory_collision_cannot_delete_ungranted_base_file(board):
    state = start(board, {"extra/large": b"z" * (candidate.project.MAX_FILE_BYTES + 1)})
    edit(state, "extra", "not a directory\n")
    report = capture(board)
    assert report["status"] == "blocked" and report["checks"]["full"] == "not_run"
    assert "commit" not in report


def test_stop_during_git_capture_never_creates_delivery_proof(board, monkeypatch):
    state = start(board)
    edit(state, "src/main.py", "changed\n")
    stopped = False
    original = candidate._git
    def stop_after_hash(path, *args, **kwargs):
        nonlocal stopped
        result = original(path, *args, **kwargs)
        if args[0] == "hash-object":
            stopped = True
        return result
    monkeypatch.setattr(candidate, "_git", stop_after_hash)
    report = capture(board, cancelled=lambda: stopped)
    assert report["status"] == "blocked" and report["accepted"] is False
    assert {"code": "operator_stopped"} in report["issues"]
    assert not (board.root.parent.parent / "check.log").exists()


def test_large_candidate_manifest_cannot_overflow_the_polling_response(board):
    configure(board)
    approve(board)
    assignment = board._load()
    assignment["candidate"] = {"version": 1, "status": "blocked", "files": [
        {"path": "p/" + "long-" * 400 + str(i)} for i in range(10000)],
        "issues": [{"code": "protected_path", "path": "p/" + str(i)} for i in range(10000)],
        "checks": {"static": "failed", "full": "not_run"}, "accepted": False, "delivered": False}
    board._save(assignment)
    view = board.show()
    assert len(json.dumps(view).encode()) < 256 * 1024
    assert view["candidate"]["file_count"] == view["candidate"]["issue_count"] == 10000
    assert "Других замечаний: 9980" in view["report_text"]
    assert view["candidate"]["checks"]["full"] == "not_run"
    assert "files" not in view["candidate"]
