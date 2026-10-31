"""Tests for the delivery of a card (TASK-S4-27, part A): ours/hooks/deliver.py, the `[delivery]`
section of project.toml and the merged record that lets the done gate skip the tests.

Real git in temporary repositories: a clone of the project with the branches `main`, `symphony/<ID>`,
`integration/<TASK>`, and a local bare repository that plays GitHub (the address goes through `Ops`).
`gh` is a fake script that writes down its calls. Real check.log, cards and locks. Nothing here
reaches the network or the real `gh`.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_integrate import git, make_repo, put_file, write

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hooks are POSIX-only")

OURS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OURS / "hooks"))

import check  # noqa: E402
import deliver  # noqa: E402
import integrate  # noqa: E402
import project_config as pc  # noqa: E402

REPO = "Serg10011/oh-my-symphony"
CARD = "OMS-12"
TASK = "OMS-7"
SUBTASK = "OMS-7-1"
REPORT = "AS-IS: nothing worked. TO-BE: the feature works."
PLAN = ("OMS-7-1", "OMS-7-2")

GH = """#!__PYTHON__
import json, sys
from pathlib import Path

here = Path(__file__).resolve().parent
args = sys.argv[1:]
path = here / "gh-behavior.json"
behavior = json.loads(path.read_text()) if path.exists() else {}
record = {"argv": args}
if "--body-file" in args:
    record["body"] = Path(args[args.index("--body-file") + 1]).read_text()
with open(here / "gh-calls.jsonl", "a") as fh:
    fh.write(json.dumps(record) + "\\n")
if args[:2] == ["pr", "list"]:
    if behavior.get("list_fail"):
        print("boom", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(behavior.get("list", [])))
elif args[:2] == ["pr", "create"]:
    if behavior.get("create_fail"):
        print("HTTP 502", file=sys.stderr)
        sys.exit(1)
    print("https://github.com/Serg10011/oh-my-symphony/pull/" + str(behavior.get("number", 99)))
else:
    sys.exit(3)
"""


def card_text(card_id: str, *, state: str = "Human Review", title: str = "Add feature",
              labels: str = "", request: str | None = None, task: bool = False) -> str:
    lines = ["---", f"id: {card_id}", f"identifier: {card_id}", f"title: {title}", f"state: {state}"]
    if labels:
        lines.append(f"labels: {labels}")
    if request:
        lines.append(f"request: {request}")
    text = "\n".join([*lines, "---", "", "Goal of the card.", "", "## Wiki Updates", "", "- none", "",
                      "## As-Is -> To-Be Report", "", REPORT, ""])
    if task:
        text += "\n".join(["## Plan Accepted", "", "- at: 2026-10-28T00:00:00Z", "",
                           *(f"- {ident} — Subtask {ident} — simple" for ident in PLAN), "",
                           "## Subtask Progress", "", "- both merged", ""])
    return text


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.code = make_repo(tmp / "code")
        self.github = tmp / "github.git"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(self.github))
        git(self.code, "push", "-q", str(self.github), "main")
        self.main = tmp / "oms-main"
        git(tmp, "clone", "-q", str(self.github), str(self.main))
        self.root = tmp / "boards"
        self.board = self.root / "demo"
        (self.board / "kanban").mkdir(parents=True)
        pc.ensure_state_dir(self.board)
        self.ws_root = tmp / "workspaces"
        self.ws_root.mkdir()
        (tmp / "bin").mkdir()
        self.gh = write(tmp / "bin" / "gh", GH.replace("__PYTHON__", sys.executable))
        self.gh.chmod(0o755)
        self.procs: list[list[str]] = []
        self.toml()

    # --- setup ------------------------------------------------------------------

    def toml(self, delivery: str | None = None, base_branch: str = "main") -> None:
        if delivery is None:
            delivery = f'[delivery]\nrepo = "{REPO}"\nbase = "main"\n'
        write(self.board / "project.toml", "\n".join([
            "[project]", 'name = "demo"', f"repo = {json.dumps(str(self.code))}",
            f'base_branch = "{base_branch}"', 'id_prefix = "OMS"', "port = 9971",
            "[tools]", f"python = {json.dumps(sys.executable)}", 'symphony = "/bin/symphony"',
            'claude = "/bin/claude"', "[integration]", "enabled = true", delivery,
        ]))

    def behave(self, **behavior: object) -> None:
        write(self.tmp / "bin" / "gh-behavior.json", json.dumps(behavior))

    def worktree(self, card_id: str, branch: str, start: str) -> Path:
        ws = self.ws_root / card_id
        git(self.code, "worktree", "add", "-q", "-b", branch, str(ws), start)
        return ws

    def card(self, card_id: str, text: str) -> None:
        write(self.board / "kanban" / f"{card_id}.md", text)

    def event(self, card_id: str, branch: str, *, tree: bool = True, **over: str) -> None:
        """What check.py hook writes after a green check of the end of `branch`."""
        extra = {"tests": "full", **over}
        if tree:
            extra.update(tree=git(self.code, "rev-parse", f"{branch}^{{tree}}"),
                         head=git(self.code, "rev-parse", branch))
        check.log_event(self.board / pc.STATE_DIR, card_id, "hook", "pass", "", **extra)

    def ordinary(self, state: str = "Human Review", **event: str) -> str:
        ws = self.worktree(CARD, f"symphony/{CARD}", "main")
        write(ws / "feature.txt", "feature\n")
        git(ws, "add", "-A")
        git(ws, "commit", "-q", "-m", "work")
        self.card(CARD, card_text(CARD, state=state))
        self.event(CARD, f"symphony/{CARD}", **event)
        return git(self.code, "rev-parse", f"symphony/{CARD}")

    def task(self, **event: str) -> str:
        branch = f"integration/{TASK}"
        integrate.ensure_integration_branch(self.code, branch, "main")
        put_file(self.code, branch, "feature.txt", "feature\n")
        tip = git(self.code, "rev-parse", branch)
        self.card(TASK, card_text(TASK, labels="[role:manager-plan]", task=True))
        self.worktree(TASK, f"symphony/{TASK}", tip)
        integrate._write_state_json(integrate.final_path(self.board, TASK), {
            "task": TASK, "branch": branch, "integration_commit": tip,
            "our_main": git(self.code, "rev-parse", "main"), "merged": False, "at": "t"})
        self.event(TASK, branch, **event)
        return tip

    # --- running ------------------------------------------------------------------

    def proc(self, argv, cwd=None, env=None, timeout=120):
        self.procs.append(list(argv))
        return deliver.run_process(argv, cwd=cwd, env=env, timeout=timeout)

    def ops(self) -> deliver.Ops:
        return deliver.Ops(proc=self.proc, remote_url=lambda repo: str(self.github),
                           tracker_python=lambda project: None)

    def push(self, card_id: str = CARD) -> int:
        return deliver.run(["push", card_id, "--root", str(self.root), "--main", str(self.main),
                            "--gh", str(self.gh)], self.ops())

    def merged(self, card_id: str = CARD) -> int:
        return deliver.run(["merged", card_id, "--root", str(self.root), "--main", str(self.main)], self.ops())

    # --- observations ---------------------------------------------------------------

    def calls(self) -> list[dict]:
        path = self.tmp / "bin" / "gh-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def creates(self) -> list[dict]:
        return [c for c in self.calls() if c["argv"][:2] == ["pr", "create"]]

    def pushes(self) -> list[list[str]]:
        return [p for p in self.procs if "push" in p]

    def heads(self) -> str:
        return git(self.github, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")

    def remote_branch(self, branch: str) -> str | None:
        done = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                              cwd=self.github, capture_output=True, text=True)
        return done.stdout.strip() or None

    def snapshot(self) -> dict:
        wss = sorted(p for p in self.ws_root.iterdir() if (p / ".git").exists())
        return {
            "refs": git(self.code, "for-each-ref"), "head": git(self.code, "rev-parse", "HEAD"),
            "status": git(self.code, "status", "--porcelain"),
            "worktrees": [(p.name, git(p, "rev-parse", "HEAD"), git(p, "symbolic-ref", "HEAD"),
                           git(p, "status", "--porcelain")) for p in wss],
        }

    def events(self, mode: str = deliver.MODE) -> list[dict]:
        path = self.board / pc.STATE_DIR / "check.log"
        if not path.exists():
            return []
        records = [json.loads(line) for line in path.read_text().splitlines()]
        return [r for r in records if r["mode"] == mode]

    def record(self, card_id: str = CARD) -> dict:
        return json.loads(deliver.delivered_path(self.board, card_id).read_text())

    def card_text(self, card_id: str = CARD) -> str:
        return (self.board / "kanban" / f"{card_id}.md").read_text()

    def state(self, card_id: str = CARD) -> str:
        return check.read_card_state(self.board / "kanban" / f"{card_id}.md")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    for key in [k for k in os.environ if k.startswith("SYMPHONY_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("SYMPHONY_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    return Env(tmp_path)


def refuses(env: Env, capsys: pytest.CaptureFixture[str], *needles: str, card_id: str = CARD) -> None:
    """The delivery was refused: reason in Russian, nothing pushed, no PR, no ref of the clone moved."""
    before, remote = env.snapshot(), env.heads()
    assert env.push(card_id) == deliver.EXIT_REFUSED
    out = capsys.readouterr().out
    assert out.startswith("Отказ: ") and "Ничего не отправлено." in out, out
    for needle in needles:
        assert needle in out, out
    assert env.pushes() == [] and env.creates() == [] and env.heads() == remote
    assert env.snapshot() == before
    assert not deliver.delivered_path(env.board, card_id).exists()


# ---------------------------------------------------------------------------
# 1. an ordinary card
# ---------------------------------------------------------------------------


def test_an_ordinary_card_is_pushed_and_gets_a_pr(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    commit = env.ordinary()
    before = env.snapshot()
    assert env.push() == deliver.EXIT_OK, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "Доставлено: PR https://github.com/Serg10011/oh-my-symphony/pull/99" in out
    # GitHub got the end of the card branch, under the one name deliver/<ID>
    assert env.remote_branch(f"deliver/{CARD}") == commit
    assert env.heads().splitlines() == [f"refs/heads/deliver/{CARD} {commit}",
                                        f"refs/heads/main {git(env.code, 'rev-parse', 'main')}"]
    # the push: an explicit address, one refspec, no force, the only credential helper is gh
    assert env.pushes() == [[
        "git", "-c", "credential.helper=", "-c", f"credential.helper=!{env.gh} auth git-credential",
        "-C", str(env.code), "push", str(env.github), f"{commit}:refs/heads/deliver/{CARD}"]]
    assert not any(a.startswith(("+", "--force")) or "force" in a for a in env.pushes()[0])
    # the PR: looked for first, then created with exact arguments
    assert env.calls()[0]["argv"] == ["pr", "list", "--repo", REPO, "--head", f"deliver/{CARD}",
                                      "--state", "open", "--json", "number,url,headRefName,baseRefName"]
    (create,) = env.creates()
    assert create["argv"][:-1] == ["pr", "create", "--repo", REPO, "--base", "main", "--head",
                                   f"deliver/{CARD}", "--title", f"{CARD}: Add feature", "--body-file"]
    assert REPORT in create["body"] and f"- коммит доставки: {commit}" in create["body"]
    assert "повторной проверки не было" in create["body"] and "## Подзадачи" not in create["body"]
    # the card: a section, the state stays; the record; the event
    assert env.state() == "Human Review"
    text = env.card_text()
    assert "## Delivery" in text and "- PR: https://github.com/Serg10011/oh-my-symphony/pull/99" in text
    assert f"- коммит доставки: {commit}" in text and f"- источник: symphony/{CARD} ({commit})" in text
    rec = env.record()
    assert {k: rec[k] for k in ("id", "kind", "source", "source_commit", "commit", "our_main", "recheck",
                                "repo", "base", "branch", "pr")} == {
        "id": CARD, "kind": "card", "source": f"symphony/{CARD}", "source_commit": commit, "commit": commit,
        "our_main": git(env.code, "rev-parse", "main"), "recheck": False, "repo": REPO, "base": "main",
        "branch": f"deliver/{CARD}",
        "pr": {"number": 99, "url": "https://github.com/Serg10011/oh-my-symphony/pull/99"}}
    assert rec["source_tree"] == git(env.code, "rev-parse", f"{commit}^{{tree}}") and rec["at"]
    (event,) = env.events()
    assert event["outcome"] == "delivered" and event["id"] == CARD and event["commit"] == commit
    # nothing local moved: references of the clone, its HEAD, the working copy of the card
    assert env.snapshot() == before
    # the main folder follows the delivery repository, so it was fetched
    assert git(env.main, "rev-parse", f"refs/remotes/origin/deliver/{CARD}") == commit


def test_a_main_folder_of_another_repository_is_not_fetched(env: Env) -> None:
    other = make_repo(env.tmp / "other")
    git(other, "remote", "add", "origin", str(env.tmp / "elsewhere.git"))
    env.ordinary()
    assert deliver.run(["push", CARD, "--root", str(env.root), "--main", str(other), "--gh", str(env.gh)],
                       env.ops()) == deliver.EXIT_OK
    assert not any("fetch" in p for p in env.procs)


def test_a_long_title_is_one_line_of_limited_length(env: Env) -> None:
    env.ordinary()
    env.card(CARD, card_text(CARD, title="Add\tfeature " + "x" * 300))
    assert env.push() == deliver.EXIT_OK
    title = env.creates()[0]["argv"][env.creates()[0]["argv"].index("--title") + 1]
    assert title.startswith(f"{CARD}: Add feature xxx") and len(title) <= deliver.TITLE_LIMIT
    assert "\t" not in title and "\n" not in title


def test_a_card_without_a_report_says_so(env: Env) -> None:
    env.ordinary()
    env.card(CARD, card_text(CARD).replace("## As-Is -> To-Be Report", "## Other").replace(REPORT, "x"))
    assert env.push() == deliver.EXIT_OK
    assert "отчёта нет" in env.creates()[0]["body"]


def test_the_description_is_cut_at_the_limit(env: Env) -> None:
    env.ordinary()
    env.card(CARD, card_text(CARD).replace(REPORT, "long " * 20000))
    assert env.push() == deliver.EXIT_OK
    body = env.creates()[0]["body"]
    assert len(body) <= deliver.BODY_LIMIT and body.rstrip().endswith("…обрезано (слишком длинное описание)")


# ---------------------------------------------------------------------------
# 2. a task card
# ---------------------------------------------------------------------------


def test_a_task_card_is_delivered_from_the_integration_branch(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    tip = env.task()
    before = env.snapshot()
    assert env.push(TASK) == deliver.EXIT_OK, capsys.readouterr().out
    assert env.remote_branch(f"deliver/{TASK}") == tip == git(env.code, "rev-parse", f"integration/{TASK}")
    (create,) = env.creates()
    assert create["argv"][create["argv"].index("--head") + 1] == f"deliver/{TASK}"
    assert "## Подзадачи" in create["body"] and all(f"- {ident} — Subtask {ident} — simple" in create["body"]
                                                    for ident in PLAN)
    assert "- at:" not in create["body"] and REPORT in create["body"]
    rec = env.record(TASK)
    assert rec["kind"] == "task" and rec["source"] == f"integration/{TASK}" and rec["commit"] == tip
    assert rec["our_main"] == json.loads(integrate.final_path(env.board, TASK).read_text())["our_main"]
    assert env.state(TASK) == "Human Review" and env.snapshot() == before


def test_a_task_card_needs_its_final_record(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.task()
    integrate.final_path(env.board, TASK).unlink()
    refuses(env, capsys, "нет итоговой проверки", card_id=TASK)


def test_a_task_card_refuses_edits_after_the_final_check(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.task()
    put_file(env.code, f"integration/{TASK}", "later.txt", "later\n")
    refuses(env, capsys, "правки после итоговой проверки", card_id=TASK)


def test_a_task_card_refuses_a_final_record_of_another_branch(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.task()
    path = integrate.final_path(env.board, TASK)
    path.write_text(path.read_text().replace(f"integration/{TASK}", "integration/other"))
    refuses(env, capsys, "не относится к ветке", card_id=TASK)


def test_rechecking_a_task_on_a_moved_base_needs_configured_scripts(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.task()
    put_file(env.code, "main", "base.txt", "base\n")
    git(env.code, "push", "-q", str(env.github), "main")  # the clone and GitHub agree: only the final record is old
    refuses(env, capsys, "повторной проверки", "setup.sh", card_id=TASK)


# ---------------------------------------------------------------------------
# 3. refusals: nothing is pushed, no PR, no reference of the clone moves
# ---------------------------------------------------------------------------


def test_refuses_without_any_check_event(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    (env.board / pc.STATE_DIR / "check.log").unlink()
    refuses(env, capsys, "нет ни одной проверки")


def test_refuses_a_pass_event_from_before_the_tree_was_recorded(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary(tree=False)
    refuses(env, capsys, "нет проверки с деревом (событие до S4-27)")


def test_refuses_a_branch_that_changed_after_the_check(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    put_file(env.code, f"symphony/{CARD}", "late.txt", "late\n")
    refuses(env, capsys, "дерево ветки не то, что прошло проверку")


def test_rechecking_the_light_path_needs_configured_scripts(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary(tests="skipped-docs")
    refuses(env, capsys, "повторной проверки", "setup.sh")


def test_refuses_a_pass_without_the_tests_field(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    path = env.board / pc.STATE_DIR / "check.log"
    event = json.loads(path.read_text().splitlines()[-1])
    del event["tests"]
    path.write_text(json.dumps(event) + "\n")
    refuses(env, capsys, "лёгким путём")


@pytest.mark.parametrize(("mode", "outcome"), [("hook", "rollback:In Progress"), ("hook", "rollback:needs_human"),
                                              ("gate", "fail")])
def test_refuses_after_a_rollback_or_a_failed_gate(env: Env, capsys: pytest.CaptureFixture[str], mode: str,
                                                  outcome: str) -> None:
    env.ordinary()
    check.log_event(env.board / pc.STATE_DIR, CARD, mode, outcome, "tests failed")
    refuses(env, capsys, "откат или провал шлюза")


def test_a_later_pass_wins_over_an_earlier_rollback(env: Env) -> None:
    env.ordinary()
    check.log_event(env.board / pc.STATE_DIR, CARD, "hook", "rollback:In Progress", "x")
    env.event(CARD, f"symphony/{CARD}")
    assert env.push() == deliver.EXIT_OK


def test_other_events_and_other_cards_do_not_decide(env: Env) -> None:
    env.ordinary()
    state = env.board / pc.STATE_DIR
    check.log_event(state, CARD, "gate", "pass", "ok", tests="skipped")
    check.log_event(state, CARD, "integrate", "done", "x")
    check.log_event(state, "OMS-99", "hook", "rollback:In Progress", "another card")
    with open(state / "check.log", "a") as fh:
        fh.write("not json at all\n")
    assert env.push() == deliver.EXIT_OK


def test_rechecking_an_ordinary_card_on_a_moved_base_needs_configured_scripts(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    put_file(env.code, "main", "base.txt", "base\n")
    git(env.code, "push", "-q", str(env.github), "main")
    refuses(env, capsys, "повторной проверки", "setup.sh")


def test_a_base_that_is_an_ancestor_of_the_card_is_fine(env: Env) -> None:
    env.ordinary()
    # the card was merged with the newer base: the base tip is its ancestor
    ws = env.ws_root / CARD
    put_file(env.code, "main", "base.txt", "base\n")
    git(env.code, "push", "-q", str(env.github), "main")
    git(ws, "merge", "-q", "--no-edit", "main")
    env.event(CARD, f"symphony/{CARD}")
    assert env.push() == deliver.EXIT_OK


def test_refuses_a_clone_that_is_behind_github(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    other = env.tmp / "someone"
    git(env.tmp, "clone", "-q", str(env.github), str(other))
    git(other, "config", "user.email", "t@example.invalid")
    git(other, "config", "user.name", "t")
    write(other / "new.txt", "n\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "elsewhere")
    git(other, "push", "-q", "origin", "main")
    refuses(env, capsys, "клон отстаёт от GitHub: сначала ours/bin/post-merge")


def test_refuses_when_github_has_no_base_branch(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    git(env.github, "update-ref", "-d", "refs/heads/main")
    refuses(env, capsys, "клон отстаёт от GitHub", "ветки нет")


def test_refuses_when_the_branch_is_already_on_github(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    git(env.code, "push", "-q", str(env.github), f"main:refs/heads/deliver/{CARD}")
    refuses(env, capsys, f"ветка deliver/{CARD} уже есть на GitHub")


def test_refuses_a_card_that_is_already_delivered(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    assert env.push() == deliver.EXIT_OK
    capsys.readouterr()
    before, remote = env.snapshot(), env.heads()
    assert env.push() == deliver.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "карточка уже доставлена: PR https://github.com/Serg10011/oh-my-symphony/pull/99" in out
    assert str(deliver.delivered_path(env.board, CARD)) in out and "удали запись" in out
    assert len(env.pushes()) == 1 and len(env.creates()) == 1 and env.heads() == remote
    assert env.snapshot() == before
    # a person deletes the record: the branch is still on GitHub, so the delivery stops there
    deliver.delivered_path(env.board, CARD).unlink()
    refuses_text = env.push()
    assert refuses_text == deliver.EXIT_REFUSED and "уже есть на GitHub" in capsys.readouterr().out


@pytest.mark.parametrize("state", ["In Progress", "Verify", "Done", "needs_human"])
def test_refuses_a_card_outside_human_review(env: Env, capsys: pytest.CaptureFixture[str], state: str) -> None:
    env.ordinary(state=state)
    refuses(env, capsys, "Human Review")


def test_refuses_a_subtask_by_its_id(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    refuses(env, capsys, "подзадача доставляется в составе задачи OMS-7", card_id=SUBTASK)


def test_refuses_a_subtask_by_its_card(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    env.card(CARD, card_text(CARD, labels="[role:worker]", request=TASK))
    refuses(env, capsys, "подзадача доставляется в составе задачи")


@pytest.mark.parametrize("bad", ["12", "oms-12", "OMS12", "OMS-", "OMS-12x", "../OMS-12", "OMS-12/../x"])
def test_refuses_a_bad_id(env: Env, capsys: pytest.CaptureFixture[str], bad: str) -> None:
    env.ordinary()
    assert env.push(bad) == deliver.EXIT_REFUSED
    assert capsys.readouterr().out.startswith("Отказ: ") and env.pushes() == []


def test_refuses_an_unknown_prefix_and_a_missing_card(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.push("ZZZ-1") == deliver.EXIT_REFUSED
    assert "нет доски с префиксом ZZZ" in capsys.readouterr().out
    assert env.push("OMS-5") == deliver.EXIT_REFUSED
    assert "нет карточки" in capsys.readouterr().out


def test_refuses_two_boards_with_one_prefix(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    second = env.root / "second"
    second.mkdir()
    (second / "project.toml").write_text((env.board / "project.toml").read_text())
    refuses(env, capsys, "несколько досок с префиксом OMS")


def test_refuses_without_a_delivery_section(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    env.toml(delivery="")
    refuses(env, capsys, "нет раздела [delivery]")


@pytest.mark.parametrize("owner", ["cskwork", "CSKWork", "cskWORK"])
def test_refuses_the_owner_of_the_original_project(env: Env, capsys: pytest.CaptureFixture[str], owner: str) -> None:
    env.ordinary()
    env.toml(delivery=f'[delivery]\nrepo = "{owner}/oh-my-symphony"\nbase = "main"\n')
    refuses(env, capsys, "владелец — исходный проект автора")


def test_refuses_a_base_that_is_not_the_base_branch(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    env.toml(delivery=f'[delivery]\nrepo = "{REPO}"\nbase = "our-main"\n')
    refuses(env, capsys, "не совпадает с [project] base_branch")


def test_refuses_a_broken_project_file(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    env.toml(delivery='[delivery]\nrepo = "no-slash"\nbase = "main"\n')
    refuses(env, capsys, "project.toml доски не принят")


def test_refuses_while_the_card_is_being_checked(env: Env, capsys: pytest.CaptureFixture[str],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    env.ordinary()
    monkeypatch.setattr(deliver, "LOCK_WAIT_S", 0.2)
    lock = check.try_lock(check.Context("hook", env.board, env.ws_root / CARD, CARD), 0)
    assert lock is not None
    try:
        refuses(env, capsys, "идёт проверка карточки")
    finally:
        lock.close()


def test_refuses_a_card_without_its_branch(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    env.card("OMS-13", card_text("OMS-13"))
    refuses(env, capsys, "нет ветки symphony/OMS-13", card_id="OMS-13")


def test_refusals_and_errors_are_logged_as_events(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary(tests="skipped-docs")
    assert env.push() == deliver.EXIT_REFUSED
    (event,) = env.events()
    assert event["outcome"] == "refused" and "setup.sh" in event["reason"]
    env.event(CARD, f"symphony/{CARD}")  # a green full check comes later
    env.behave(list_fail=True)
    assert env.push() == deliver.EXIT_ERROR
    assert [e["outcome"] for e in env.events()] == ["refused", "error"]
    assert "gh pr list не прошёл" in capsys.readouterr().out


def test_a_bad_gh_path_is_an_error_before_anything(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    code = deliver.run(["push", CARD, "--root", str(env.root), "--gh", "gh"], env.ops())
    assert code == deliver.EXIT_ERROR and "абсолютный путь к gh" in capsys.readouterr().out
    assert env.pushes() == []


# ---------------------------------------------------------------------------
# the branch that may be pushed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("card_id", "base"), [("OMS-12", "deliver/OMS-12"), ("bad", "main"), ("OMS-12-1", "main")])
def test_the_push_branch_is_checked_in_code(card_id: str, base: str) -> None:
    with pytest.raises(deliver.Failed, match="недопустима для пуша"):
        deliver.deliver_branch(card_id, base)


def test_the_push_branch_is_deliver_slash_id() -> None:
    assert deliver.deliver_branch("OMS-12", "our-main") == "deliver/OMS-12"


# ---------------------------------------------------------------------------
# 4. continuing after a failed `gh pr create`; an existing PR
# ---------------------------------------------------------------------------


def test_a_failed_pr_creation_keeps_the_push_and_the_repeat_goes_on(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    commit = env.ordinary()
    env.behave(create_fail=True)
    assert env.push() == deliver.EXIT_ERROR
    out = capsys.readouterr().out
    assert "gh pr create не создал PR" in out and "повтори .deliver" in out
    assert env.remote_branch(f"deliver/{CARD}") == commit
    assert env.record()["pr"] is None and "## Delivery" not in env.card_text()
    assert len(env.pushes()) == 1 and len(env.creates()) == 1
    env.behave()
    assert env.push() == deliver.EXIT_OK
    assert len(env.pushes()) == 1  # the push was skipped the second time
    assert len(env.creates()) == 2
    assert env.record()["pr"]["number"] == 99 and "## Delivery" in env.card_text()
    assert "пуш пропущен" in capsys.readouterr().out
    assert [e["outcome"] for e in env.events()] == ["error", "delivered"]


def test_a_branch_on_another_commit_stops_the_continuation(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    env.behave(create_fail=True)
    assert env.push() == deliver.EXIT_ERROR
    env.behave()
    git(env.code, "push", "-q", "--force", str(env.github), f"main:refs/heads/deliver/{CARD}")
    capsys.readouterr()
    pushes, creates = len(env.pushes()), len(env.creates())
    assert env.push() == deliver.EXIT_REFUSED
    assert "указывает на" in capsys.readouterr().out
    assert len(env.pushes()) == pushes and len(env.creates()) == creates
    assert env.record()["pr"] is None


def test_a_missing_branch_stops_the_continuation(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    env.behave(create_fail=True)
    assert env.push() == deliver.EXIT_ERROR
    git(env.github, "update-ref", "-d", f"refs/heads/deliver/{CARD}")
    env.behave()
    assert env.push() == deliver.EXIT_REFUSED
    assert "ветки" in capsys.readouterr().out and len(env.pushes()) == 1


def test_a_card_that_moved_after_the_push_stops_the_continuation(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    env.behave(create_fail=True)
    assert env.push() == deliver.EXIT_ERROR
    put_file(env.code, f"symphony/{CARD}", "late.txt", "late\n")
    env.event(CARD, f"symphony/{CARD}")  # a new green check of the new end
    env.behave()
    capsys.readouterr()
    assert env.push() == deliver.EXIT_REFUSED
    assert "ветка карточки сдвинулась после пуша" in capsys.readouterr().out
    assert len(env.pushes()) == 1 and len(env.creates()) == 1


EXISTING_PR = [{"number": 7, "url": "https://github.com/Serg10011/oh-my-symphony/pull/7",
                "headRefName": f"deliver/{CARD}", "baseRefName": "main"}]


def test_an_existing_pr_is_recorded_and_not_created_again(env: Env) -> None:
    env.ordinary()
    env.behave(list=EXISTING_PR)
    assert env.push() == deliver.EXIT_OK
    assert env.creates() == []
    assert env.record()["pr"] == {"number": 7, "url": "https://github.com/Serg10011/oh-my-symphony/pull/7"}
    assert "pull/7" in env.card_text()


def test_an_existing_pr_is_found_when_the_answer_to_create_was_lost(env: Env) -> None:
    env.ordinary()
    env.behave(create_fail=True)
    assert env.push() == deliver.EXIT_ERROR
    env.behave(list=EXISTING_PR)  # the PR was created after all
    assert env.push() == deliver.EXIT_OK
    assert len(env.creates()) == 1 and len(env.pushes()) == 1
    assert env.record()["pr"]["number"] == 7


@pytest.mark.parametrize("listed", [
    EXISTING_PR + [{**EXISTING_PR[0], "number": 8}],
    [{**EXISTING_PR[0], "baseRefName": "other"}],
    [{**EXISTING_PR[0], "headRefName": "deliver/OMS-1"}],
    [{"number": 7}],
])
def test_an_unclear_list_of_prs_is_an_error(env: Env, capsys: pytest.CaptureFixture[str], listed: list) -> None:
    env.ordinary()
    env.behave(list=listed)
    assert env.push() == deliver.EXIT_ERROR
    assert env.creates() == [] and env.record()["pr"] is None
    assert "Ошибка:" in capsys.readouterr().out


def test_a_failed_push_writes_nothing(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    hook = env.github / "hooks" / "pre-receive"
    write(hook, "#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    assert env.push() == deliver.EXIT_ERROR
    assert "git push не прошёл" in capsys.readouterr().out
    assert not deliver.delivered_path(env.board, CARD).exists() and env.creates() == []


# ---------------------------------------------------------------------------
# 5. deliver merged: the record that lets the gate skip the tests
# ---------------------------------------------------------------------------


def gate_files(env: Env, card_id: str) -> Path:
    """What check.py needs to run a gate in the working copy of the card; returns the tests marker."""
    base = git(env.code, "rev-parse", "main")
    write(env.board / pc.STATE_DIR / "baseline.json", json.dumps({
        "base_branch": "main", "base_sha": base, "passed": 1, "skipped": 0, "failed": 0,
        "setup_s": 0.1, "tests_s": 0.1, "measured_at": "2026-10-28T00:00:00Z"}))
    write(pc.base_record_path(env.board, card_id), json.dumps(
        {"base_sha": base, "base_branch": "main", "created_at": "2026-10-28T00:00:00Z"}))
    marker = env.tmp / "tests-ran"
    write(env.board / "test.sh", f"touch {marker}\necho 'PIPELINE_TESTS passed=1 skipped=0 failed=0'\n")
    return marker


def gate(env: Env, card_id: str) -> check.Result:
    return check.run_checks(check.Context("gate", env.board, env.ws_root / card_id, card_id))


def merged_file(env: Env, card_id: str) -> Path:
    return check.merged_path(check.Context("gate", env.board, env.ws_root / card_id, card_id))


def test_merged_writes_the_record_and_the_gate_skips_the_tests_of_an_ordinary_card(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    commit = env.ordinary()
    marker = gate_files(env, CARD)
    assert env.push() == deliver.EXIT_OK
    capsys.readouterr()
    assert env.merged() == deliver.EXIT_OK
    assert "запись «влито»" in capsys.readouterr().out
    record = json.loads(merged_file(env, CARD).read_text())
    assert {k: record[k] for k in ("tree", "commit", "task", "branch", "source")} == {
        "tree": git(env.code, "rev-parse", f"{commit}^{{tree}}"), "commit": commit, "task": None,
        "branch": "main", "source": f"deliver/{CARD}"} and record["at"]
    text = env.card_text()
    assert text.count("## Delivery") == 1 and "- PR влит" in text  # one section, a line added
    assert env.record()["merged_at"] and env.record()["pr"]["number"] == 99
    assert [e["outcome"] for e in env.events()] == ["delivered", "merged"]
    result = gate(env, CARD)  # Sergei sets Done
    assert result.ok, result.reasons
    assert result.tests == "skipped" and result.tests_skipped and not marker.exists()
    assert not merged_file(env, CARD).exists()  # one shot


def test_merged_writes_the_record_and_the_gate_skips_the_tests_of_a_task_card(env: Env) -> None:
    tip = env.task()
    marker = gate_files(env, TASK)
    assert env.push(TASK) == deliver.EXIT_OK
    assert env.merged(TASK) == deliver.EXIT_OK
    record = json.loads(merged_file(env, TASK).read_text())
    assert record["source"] == f"deliver/{TASK}" and record["commit"] == tip and record["task"] is None
    result = gate(env, TASK)
    assert result.ok, result.reasons
    assert result.tests == "skipped" and not marker.exists()


def test_the_gate_of_a_task_card_does_not_take_the_record_of_a_subtask(env: Env) -> None:
    env.task()
    marker = gate_files(env, TASK)
    ws = env.ws_root / TASK
    write(merged_file(env, TASK), json.dumps({
        "tree": check.worktree_tree(ws), "commit": "c" * 40, "task": TASK, "branch": f"integration/{TASK}",
        "source": f"symphony/{SUBTASK}@{'a' * 40}", "at": "t"}))
    result = gate(env, TASK)
    assert result.ok, result.reasons
    assert result.tests == "full" and not result.tests_skipped and marker.exists()
    assert not merged_file(env, TASK).exists()  # used up all the same


def test_the_gate_of_a_task_card_does_not_take_the_delivery_record_of_another_card(env: Env) -> None:
    env.task()
    marker = gate_files(env, TASK)
    write(merged_file(env, TASK), json.dumps({
        "tree": check.worktree_tree(env.ws_root / TASK), "commit": None, "task": None, "branch": "main",
        "source": f"deliver/{CARD}", "at": "t"}))
    result = gate(env, TASK)
    assert result.tests == "full" and marker.exists()


def test_a_delivery_record_of_a_changed_tree_does_not_skip_the_tests(env: Env) -> None:
    env.task()
    marker = gate_files(env, TASK)
    assert env.push(TASK) == deliver.EXIT_OK
    assert env.merged(TASK) == deliver.EXIT_OK
    write(env.ws_root / TASK / "edited.txt", "edited after the merge\n")
    result = gate(env, TASK)
    assert result.tests == "full" and marker.exists()


def test_merged_refuses_without_a_delivery(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    assert env.merged() == deliver.EXIT_REFUSED
    assert "карточка не доставлялась" in capsys.readouterr().out
    assert not merged_file(env, CARD).exists()


def test_merged_refuses_a_delivery_without_a_pr(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    env.behave(create_fail=True)
    assert env.push() == deliver.EXIT_ERROR
    capsys.readouterr()
    assert env.merged() == deliver.EXIT_REFUSED
    assert "нет PR" in capsys.readouterr().out and not merged_file(env, CARD).exists()


def test_merged_is_marked_once(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    assert env.push() == deliver.EXIT_OK
    assert env.merged() == deliver.EXIT_OK
    merged_file(env, CARD).unlink()  # a gate used the record
    capsys.readouterr()
    assert env.merged() == deliver.EXIT_REFUSED
    assert "уже отмечено" in capsys.readouterr().out and not merged_file(env, CARD).exists()


def test_merged_without_the_commit_in_the_clone_is_an_error(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.ordinary()
    assert env.push() == deliver.EXIT_OK
    path = deliver.delivered_path(env.board, CARD)
    path.write_text(path.read_text().replace(env.record()["commit"], "e" * 40))
    capsys.readouterr()
    assert env.merged() == deliver.EXIT_ERROR
    assert "нет коммита доставки" in capsys.readouterr().out and not merged_file(env, CARD).exists()


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


def test_the_script_runs_as_a_program(env: Env) -> None:
    done = subprocess.run([sys.executable, str(OURS / "bin" / "deliver"), "push", "OMS-12", "--root",
                           str(env.root), "--gh", str(env.gh)], capture_output=True, text=True)
    assert done.returncode == deliver.EXIT_REFUSED and "Отказ: нет карточки" in done.stdout
    done = subprocess.run([sys.executable, str(OURS / "bin" / "deliver")], capture_output=True, text=True)
    assert done.returncode == 2 and "usage" in done.stderr


# ---------------------------------------------------------------------------
# 8. [delivery] in project.toml
# ---------------------------------------------------------------------------


def project_data(**delivery: object) -> dict:
    data: dict = {
        "project": {"name": "demo", "repo": "/repo", "base_branch": "our-main", "id_prefix": "OMS", "port": 9971},
        "tools": {"python": "/usr/bin/python3", "symphony": "/bin/symphony", "claude": "/bin/claude"},
    }
    if delivery:
        data["delivery"] = dict(delivery)
    return data


def test_a_board_without_the_section_loads_as_before() -> None:
    assert pc.validate_project(project_data())["delivery"] == {}


def test_the_section_is_read() -> None:
    project = pc.validate_project(project_data(repo="Serg10011/oh-my-symphony", base="our-main"))
    assert project["delivery"] == {"repo": "Serg10011/oh-my-symphony", "base": "our-main"}


@pytest.mark.parametrize("repo", ["no-slash", "a/b/c", "/b", "a/", "a b/c", "a/b c", "a/b;rm", "./x", "a/..", "../b",
                                  "https://github.com/a/b", "a/b\n"])
def test_a_bad_repo_form_is_refused(repo: str) -> None:
    with pytest.raises(pc.ProjectError, match="repo"):
        pc.validate_project(project_data(repo=repo, base="our-main"))


@pytest.mark.parametrize("base", ["-x", "a..b", "a b", "a/", "x.lock", "/a", "a//b"])
def test_a_bad_base_is_refused(base: str) -> None:
    with pytest.raises(pc.ProjectError, match="base"):
        pc.validate_project(project_data(repo="a/b", base=base))


def test_an_unknown_key_and_a_missing_key_are_refused() -> None:
    with pytest.raises(pc.ProjectError, match="неизвестный ключ.*push"):
        pc.validate_project(project_data(repo="a/b", base="our-main", push=True))
    with pytest.raises(pc.ProjectError, match="нет ключа base"):
        pc.validate_project(project_data(repo="a/b"))
    with pytest.raises(pc.ProjectError, match="нет ключа repo"):
        pc.validate_project(project_data(base="our-main"))
    data = project_data()
    data["delivery"] = {}
    with pytest.raises(pc.ProjectError, match="нет ключа repo"):
        pc.validate_project(data)


def test_the_owner_is_checked_by_deliver_not_by_the_loader() -> None:
    # the loader accepts any owner/name; deliver refuses cskwork always (see the tests above)
    assert pc.validate_project(project_data(repo="cskwork/oh-my-symphony", base="our-main"))["delivery"]


def test_project_new_has_the_section_as_a_commented_example() -> None:
    text = (OURS / "bin" / "project-new").read_text(encoding="utf-8")
    assert "# [delivery]" in text and "# repo = " in text and "# base = " in text
    assert "\n[delivery]" not in text


def test_an_unexpected_exception_is_an_error_with_a_clean_answer(env: Env, capsys: pytest.CaptureFixture[str],
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    env.ordinary()

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(deliver, "parse_check_log", broken)
    assert env.push() == deliver.EXIT_ERROR
    assert "Ошибка: внутренняя ошибка: RuntimeError: boom" in capsys.readouterr().out
    assert [e["outcome"] for e in env.events()] == ["error"] and env.pushes() == []
