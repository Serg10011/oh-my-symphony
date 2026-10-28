"""Delivery of the result of a card to GitHub (TASK-S4-27, part A). Logic of ours/bin/deliver.

    deliver push <ID> [--root DIR] [--main DIR] [--gh PATH]
    deliver merged <ID> [--root DIR] [--main DIR]

`push` checks that the card is in Human Review with a green full check of the very
commit that is going to be delivered, that the base branch did not move since, that the
clone is not behind GitHub; then pushes that commit (and nothing else) to the new branch
`deliver/<ID>` of the repository named in `[delivery]` of project.toml, and opens a PR
into the base branch. Local branches and the working copy of the card are never touched.
`merged` runs after the PR was merged: it writes the one-shot "merged" record (S4-13) that
lets the done gate skip the tests again.

Exit codes: 0 done, 1 refusal (nothing changed), 2 error (see the output). Output is in
Russian. Everything external (gh, the address of the remote repository, git) goes through
`Ops`, so tests substitute it. Standard library only, Python >= 3.11, POSIX.

Not in this part (S4-28): a base branch that moved since the green check, and a green
check that went the light way (doc paths) - both are refusals here.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check  # noqa: E402
import integrate  # noqa: E402
import post_merge  # noqa: E402
import project_config as pc  # noqa: E402

EXIT_OK, EXIT_REFUSED, EXIT_ERROR = 0, 1, 2

MODE = "deliver"
NOTE_HEADING = "Delivery"
DELIVERED_SUFFIX = ".delivered.json"
STATE_REVIEW = "Human Review"
SYMPHONY_PREFIX = "symphony/"
DELIVER_PREFIX = check.DELIVER_PREFIX
# The owner of the original project: nothing of ours is ever pushed or proposed there.
FORBIDDEN_OWNERS = ("cskwork",)
# Branches a delivery never pushes to, whatever the configuration says.
PROTECTED_BRANCHES = ("main", "our-main")
LOCK_WAIT_S = 5
GIT_TIMEOUT_S = 120
PUSH_TIMEOUT_S = 600
GH_TIMEOUT_S = 120
TITLE_LIMIT = 120
BODY_LIMIT = 60_000
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
REPORT_HEADING_PATTERN = r"As-Is\s*->\s*To-Be\s+Report"

ID_RE = re.compile(r"^([A-Z][A-Z0-9]{1,9})-([0-9]+)((?:-[0-9]+)*)$")
SAFE_GH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
PR_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/pull/([0-9]+)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class Refused(Exception):
    """Nothing was changed; the message is the reason (in Russian)."""


class Failed(Exception):
    """Something went wrong, possibly after a change; the message says what."""


# ---------------------------------------------------------------------------
# external world
# ---------------------------------------------------------------------------


@dataclass
class Proc:
    rc: int
    out: str = ""
    err: str = ""


def run_process(argv: list[str], cwd: Path | None = None, env: dict[str, str] | None = None,
                timeout: float = GIT_TIMEOUT_S) -> Proc:
    """Run argv (no shell, own process group, closed stdin); the group is killed on timeout."""
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return Proc(127, "", f"не удалось запустить {argv[0]}: {exc.strerror or exc}")
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return Proc(124, out or "", (err or "") + f"\nтаймаут {timeout:g} с, группа процессов убита")
    return Proc(proc.returncode, out or "", err or "")


def default_remote_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def default_tracker_python(project: dict) -> str | None:
    """The python of the board that has `symphony` (check.append_note writes through the tracker)."""
    path = project["tools"]["python"]
    return path if os.access(path, os.X_OK) else None


@dataclass
class Ops:
    proc: Callable[..., Proc] = run_process
    remote_url: Callable[[str], str] = default_remote_url
    tracker_python: Callable[[dict], str | None] = default_tracker_python


@contextlib.contextmanager
def env_var(name: str, value: str | None) -> Iterator[None]:
    """`name` = `value` for the block (unchanged when value is None)."""
    if value is None:
        yield
        return
    old = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


def net_env(gh: str) -> dict[str, str]:
    """Short explicit environment: no SYMPHONY_* variables, no prompts, credentials only from gh."""
    env = {"PATH": f"{Path(gh).parent}:{SAFE_PATH}", "HOME": str(Path.home()),
           "LANG": os.environ.get("LANG", "en_US.UTF-8"), "GIT_TERMINAL_PROMPT": "0"}
    for key in ("SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def credential_args(gh: str) -> list[str]:
    """git options: the only credential helper is `gh auth git-credential` (the token stays in gh)."""
    return ["-c", "credential.helper=", "-c", f"credential.helper=!{gh} auth git-credential"]


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------


def delivered_path(board: Path, card_id: str) -> Path:
    return board / pc.STATE_DIR / f"{card_id}{DELIVERED_SUFFIX}"


def one_line(text: str, limit: int) -> str:
    """One line without control characters, at most `limit` characters."""
    cleaned = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    return cleaned[:limit].rstrip()


def deliver_branch(card_id: str, base: str) -> str:
    """The one branch a delivery pushes to. Checked here, not only by the caller."""
    branch = f"{DELIVER_PREFIX}{card_id}"
    match = ID_RE.match(card_id)
    if (match is None or match.group(3) or branch in PROTECTED_BRANCHES or branch == base
            or not branch.startswith(DELIVER_PREFIX)):
        raise Failed(f"внутренняя ошибка: ветка {branch!r} недопустима для пуша")
    return branch


def section_of(body: str, heading_pattern: str) -> str | None:
    """Text of the `## <heading>` section (up to the next `##`), or None."""
    match = re.search(rf"^##\s+{heading_pattern}\s*$", body, re.IGNORECASE | re.MULTILINE)
    if match is None:
        return None
    after = body[match.end():]
    following = re.search(r"^##\s+\S", after, re.MULTILINE)
    return (after if following is None else after[: following.start()]).strip()


def plan_lines(body: str) -> list[str]:
    """Subtask lines `- <ID> — <title> — <level>` of `## Plan Accepted`."""
    section = section_of(body, re.escape(integrate.NOTE_ACCEPTED))
    ids = set(integrate.plan_accepted_ids(body))
    if section is None:
        return []
    found = []
    for line in section.splitlines():
        match = re.match(r"^-\s+(\S+) — ", line)
        if match and match.group(1) in ids:
            found.append(line.strip())
    return found


def pr_body(card_id: str, kind: str, card_body: str, rec: dict) -> str:
    report = section_of(card_body, REPORT_HEADING_PATTERN) or "отчёта нет"
    parts = [f"## Отчёт As-Is -> To-Be\n\n{report}"]
    if kind == "task":
        lines = plan_lines(card_body)
        parts.append("## Подзадачи\n\n" + ("\n".join(lines) if lines else "список не найден"))
    parts.append(
        "## Проверка\n\n"
        f"- карточка: {card_id} ({'карточка-задача' if kind == 'task' else 'обычная карточка'})\n"
        f"- коммит доставки: {rec['commit']}\n"
        f"- источник: {rec['source']} ({rec['source_commit']})\n"
        f"- {rec['base']} при доставке: {rec['our_main']}\n"
        "- полный прогон тестов прошёл на этом же дереве до доставки; "
        f"{rec['base']} с тех пор не сдвигался, повторной проверки не было"
    )
    text = "\n\n".join(parts) + "\n"
    if len(text) > BODY_LIMIT:
        mark = "\n\n…обрезано (слишком длинное описание)\n"
        text = text[: BODY_LIMIT - len(mark)] + mark
    return text


def parse_check_log(path: Path, card_id: str) -> list[dict]:
    """Events of one card in check.log, oldest first; unreadable lines are skipped."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("id") == card_id:
            events.append(event)
    return events


def is_decisive(event: dict) -> bool:
    mode, outcome = event.get("mode"), str(event.get("outcome", ""))
    return (mode == "hook" and (outcome == "pass" or outcome.startswith("rollback:"))) or (
        mode == "gate" and outcome == "fail")


def green_check_problem(events: list[dict], source_tree: str) -> str | None:
    """Why there is no green check of `source_tree` (in Russian), or None when there is one."""
    decisive = [e for e in events if is_decisive(e)]
    if not decisive:
        return "в check.log нет ни одной проверки этой карточки"
    last = decisive[-1]
    if last.get("outcome") != "pass":
        return (f"после проверки был откат или провал шлюза (последнее решающее событие: "
                f"{last.get('mode')} {last.get('outcome')})")
    tree = last.get("tree")
    if not isinstance(tree, str) or not tree:
        return "нет проверки с деревом (событие до S4-27)"
    if tree != source_tree:
        return (f"дерево ветки не то, что прошло проверку ({source_tree[:12]} вместо {tree[:12]}): "
                "в ветке есть правки после проверки")
    if last.get("tests") != "full":
        return ("проверка шла лёгким путём (tests: " f"{last.get('tests') or 'нет'}): "
                "полный прогон перед доставкой — S4-28")
    return None


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


class Delivery:
    def __init__(self, card_id: str, root: Path, main: Path | None, gh: str, ops: Ops, say) -> None:
        self.id = card_id
        self.root = root
        self.main = main
        self.gh = gh
        self.ops = ops
        self.say = say
        self.board: Path | None = None
        self.ctx: check.Context | None = None
        self.env = net_env(gh)

    # --- running things ------------------------------------------------------

    def git(self, clone: Path, *args: str, net: bool = False, timeout: float = GIT_TIMEOUT_S) -> Proc:
        prefix = credential_args(self.gh) if net else []
        return self.ops.proc(["git", *prefix, "-C", str(clone), *args], cwd=None, env=self.env, timeout=timeout)

    def rev(self, clone: Path, spec: str) -> str | None:
        done = self.git(clone, "rev-parse", "--verify", "--quiet", spec)
        out = done.out.strip()
        return out if done.rc == 0 and SHA_RE.match(out) else None

    def remote_ref(self, clone: Path, url: str, ref: str) -> str | None:
        """Tip of `ref` on the remote repository; None when there is no such ref."""
        done = self.git(clone, "ls-remote", url, ref, net=True)
        if done.rc != 0:
            raise Failed(f"git ls-remote {ref} не прошёл (код {done.rc}): {done.err.strip()[:300]}")
        for line in done.out.splitlines():
            sha, _, name = line.partition("\t")
            if name.strip() == ref and SHA_RE.match(sha):
                return sha
        return None

    def log(self, outcome: str, reason: str, **extra: str) -> None:
        if self.ctx is not None:
            check.log_event(self.ctx, self.id, MODE, outcome, reason, **extra)

    # --- the sequence ------------------------------------------------------------

    def push(self) -> int:
        try:
            return self._push()
        except Refused as exc:
            self.log("refused", str(exc))
            self.say(f"Отказ: {exc}")
            self.say("Ничего не изменено.")
            return EXIT_REFUSED
        except Failed as exc:
            return self.fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - the queue gets a clean answer, the trace goes to the log
            return self.fail(f"внутренняя ошибка: {type(exc).__name__}: {exc}")

    def fail(self, reason: str) -> int:
        self.log("error", reason)
        self.say(f"Ошибка: {reason}")
        return EXIT_ERROR

    def _push(self) -> int:
        project, board = self.load_board()
        repo, base = project["delivery"].get("repo"), project["delivery"].get("base")
        self.check_delivery_section(project, repo, base)
        front, body = self.read_card(board, project)
        kind = "task" if integrate.is_task_card(front, body) else "card"
        clone = Path(project["project"]["repo"])
        lock = check.try_lock(self.ctx, LOCK_WAIT_S)
        if lock is None:
            raise Refused("идёт проверка карточки (занята блокировка .ours-state); попробуй позже")
        try:
            return self._push_locked(project, board, front, body, kind, clone, repo, base)
        finally:
            lock.close()

    def load_board(self) -> tuple[dict, Path]:
        match = ID_RE.match(self.id)
        if match is None:
            raise Refused(f"ID {self.id!r} не вида ПРЕФИКС-N (например OMS-12)")
        if match.group(3):
            raise Refused(f"{self.id} — подзадача: подзадача доставляется в составе задачи "
                          f"{match.group(1)}-{match.group(2)}")
        prefix = match.group(1)
        boards = [b for b, raw in pc.other_projects(self.root) if raw.get("id_prefix") == prefix]
        if not boards:
            raise Refused(f"под {self.root} нет доски с префиксом {prefix}")
        if len(boards) > 1:
            raise Refused(f"под {self.root} несколько досок с префиксом {prefix}: "
                          + ", ".join(sorted(str(b) for b in boards)))
        board = boards[0]
        try:
            project = pc.load_project(board)
        except pc.ProjectError as exc:
            raise Refused(f"project.toml доски не принят: {exc}") from None
        self.board = board
        self.ctx = check.Context(MODE, board, integrate.workspace_root(project) / self.id, self.id)
        return project, board

    def check_delivery_section(self, project: dict, repo: str | None, base: str | None) -> None:
        if not project["delivery"] or repo is None or base is None:
            raise Refused(f"нет раздела [delivery] в {pc.PROJECT_FILE} доски")
        if repo.split("/")[0].lower() in FORBIDDEN_OWNERS:
            raise Refused(f"[delivery] repo {repo!r}: владелец — исходный проект автора, "
                          "пушить и открывать PR туда запрещено всегда")
        if base != project["project"]["base_branch"]:
            raise Refused(f"[delivery] base {base!r} не совпадает с [project] base_branch "
                          f"{project['project']['base_branch']!r}: проверка «ветка не сдвинулась» "
                          "шла бы не по той ветке")

    def read_card(self, board: Path, project: dict) -> tuple[dict, str]:
        card = board / pc.BOARD_DIR / f"{self.id}.md"
        if not card.is_file():
            raise Refused(f"нет карточки {pc.BOARD_DIR}/{card.name}")
        try:
            front = integrate.read_card_front(card, with_title=True)
            body = check.split_card(card.read_text(encoding="utf-8"))[1]
        except (integrate.IntegrateError, OSError, UnicodeDecodeError) as exc:
            raise Refused(f"карточка не читается: {exc}") from None
        if front["state"].lower() != STATE_REVIEW.lower():
            raise Refused(f"карточка в статусе {front['state']!r}, а нужен {STATE_REVIEW!r}")
        if integrate.is_subtask(front, project["integration"]):
            raise Refused(f"{self.id} — подзадача: подзадача доставляется в составе задачи")
        return front, body

    def read_record(self, board: Path) -> dict | None:
        """The delivered record when there is one; Refused when it is final or damaged."""
        path = delivered_path(board, self.id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Refused(f"запись {path.name} повреждена ({exc}): удали её, если доставка не нужна") from None
        if not isinstance(data, dict):
            raise Refused(f"запись {path.name} повреждена: удали её, если доставка не нужна")
        pr = data.get("pr")
        if isinstance(pr, dict) and pr.get("url"):
            raise Refused(f"карточка уже доставлена: PR {pr['url']}. Чтобы доставить заново, "
                          f"удали запись {path} (это делает человек)")
        return data

    def final_record(self, board: Path, branch: str, source_commit: str) -> dict:
        path = integrate.final_path(board, self.id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise Refused(f"нет итоговой проверки задачи ({path.name}): сначала финал задачи") from None
        except (OSError, ValueError) as exc:
            raise Refused(f"запись {path.name} повреждена: {exc}") from None
        if not isinstance(data, dict) or data.get("task") != self.id or data.get("branch") != branch:
            raise Refused(f"запись {path.name} не относится к ветке {branch}")
        if data.get("integration_commit") != source_commit:
            raise Refused(f"в ветке карточки-задачи правки после итоговой проверки "
                          f"({str(data.get('integration_commit'))[:12]} → {source_commit[:12]})")
        if not isinstance(data.get("our_main"), str) or not SHA_RE.match(data["our_main"]):
            raise Refused(f"в записи {path.name} нет корректного our_main")
        return data

    def _push_locked(self, project: dict, board: Path, front: dict, body: str, kind: str,
                     clone: Path, repo: str, base: str) -> int:
        record = self.read_record(board)
        continuing = record is not None
        if kind == "task":
            source = integrate.integration_branch(project, self.id)
        else:
            source = f"{SYMPHONY_PREFIX}{self.id}"
        commit = self.rev(clone, f"refs/heads/{source}^{{commit}}")
        if commit is None:
            raise Refused(f"в клоне {clone} нет ветки {source}")
        tree = self.rev(clone, f"{commit}^{{tree}}")
        if tree is None:
            raise Failed(f"не прочитать дерево коммита {commit[:12]}")
        final = self.final_record(board, source, commit) if kind == "task" else None
        problem = green_check_problem(
            parse_check_log(board / pc.STATE_DIR / check.LOG_NAME, self.id), tree)
        if problem:
            raise Refused(problem)

        url = self.ops.remote_url(repo)
        local_base = self.rev(clone, f"refs/heads/{base}^{{commit}}")
        if local_base is None:
            raise Refused(f"в клоне {clone} нет ветки {base}")
        remote_base = self.remote_ref(clone, url, f"refs/heads/{base}")
        if remote_base != local_base:
            raise Refused(f"клон отстаёт от GitHub: сначала ours/bin/post-merge "
                          f"({base}: в клоне {local_base[:12]}, на GitHub "
                          f"{remote_base[:12] if remote_base else 'ветки нет'})")
        if final is not None:
            moved = final["our_main"] != local_base
            then = final["our_main"]
        else:
            ancestor = self.git(clone, "merge-base", "--is-ancestor", local_base, commit)
            if ancestor.rc not in (0, 1):
                raise Failed(f"git merge-base не прошёл: {ancestor.err.strip()[:300]}")
            moved = ancestor.rc == 1
            then = self.merge_base(clone, local_base, commit) or "?"
        if moved:
            raise Refused(f"{base} ушёл вперёд после зелёной проверки ({then[:12]} → {local_base[:12]}): "
                          "повторная проверка будет в S4-28, пока доставка вручную")

        branch = deliver_branch(self.id, base)
        remote_branch = self.remote_ref(clone, url, f"refs/heads/{branch}")
        if continuing:
            self.check_continuation(record, repo, base, branch, commit, remote_branch)
        elif remote_branch is not None:
            raise Refused(f"ветка {branch} уже есть на GitHub ({remote_branch[:12]}): "
                          "удали её на GitHub или реши сам")

        title = one_line(f"{self.id}: {front.get('title') or ''}", TITLE_LIMIT)
        rec = record if continuing else {
            "id": self.id, "kind": kind, "source": source, "source_commit": commit,
            "source_tree": tree, "commit": commit, "our_main": local_base, "recheck": False,
            "repo": repo, "base": base, "branch": branch, "pr": None, "at": check._now(),
        }
        if not continuing:
            self.git_push(clone, url, commit, branch)
            self.write_record(board, rec)
            self.say(f"Ветка {branch} отправлена на GitHub ({commit[:12]}).")
        else:
            self.say(f"Ветка {branch} уже на GitHub ({commit[:12]}): пуш пропущен, продолжаю с PR.")
        self.open_pr(board, rec, title, pr_body(self.id, kind, body, rec))
        self.finish(project, board, rec, url)
        return EXIT_OK

    def merge_base(self, clone: Path, one: str, two: str) -> str:
        done = self.git(clone, "merge-base", one, two)
        return done.out.strip() if done.rc == 0 else ""

    def check_continuation(self, record: dict, repo: str, base: str, branch: str, commit: str,
                           remote_branch: str | None) -> None:
        if (record.get("repo"), record.get("base"), record.get("branch")) != (repo, base, branch):
            raise Refused("запись доставки сделана для другого репозитория, базы или ветки: "
                          "удали её, если доставка нужна заново")
        if remote_branch is None:
            raise Refused(f"запись есть, а ветки {branch} на GitHub нет: удали запись и доставь заново")
        if remote_branch != record.get("commit"):
            raise Refused(f"ветка {branch} на GitHub указывает на {remote_branch[:12]}, "
                          f"а в записи доставки {str(record.get('commit'))[:12]}")
        if commit != record.get("commit"):
            raise Refused("ветка карточки сдвинулась после пуша: удали запись доставки и доставь заново")

    def git_push(self, clone: Path, url: str, commit: str, branch: str) -> None:
        refspec = f"{commit}:refs/heads/{branch}"
        assert not refspec.startswith("+") and "force" not in refspec
        done = self.git(clone, "push", url, refspec, net=True, timeout=PUSH_TIMEOUT_S)
        if done.rc != 0:
            raise Failed(f"git push не прошёл (код {done.rc}); ничего не записано: "
                         f"{(done.err or done.out).strip()[-500:]}")

    def write_record(self, board: Path, rec: dict) -> None:
        try:
            check.ensure_state_dir(board / pc.STATE_DIR)
            check._write_atomic(delivered_path(board, self.id),
                                (json.dumps(rec, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        except OSError as exc:
            raise Failed(f"не записать {delivered_path(board, self.id).name}: {exc}") from None

    # --- the PR ----------------------------------------------------------------------

    def existing_pr(self, repo: str, base: str, branch: str) -> dict | None:
        done = self.ops.proc(
            [self.gh, "pr", "list", "--repo", repo, "--head", branch, "--state", "open",
             "--json", "number,url,headRefName,baseRefName"],
            cwd=None, env=self.env, timeout=GH_TIMEOUT_S)
        if done.rc != 0:
            raise Failed(f"gh pr list не прошёл (код {done.rc}): {(done.err or done.out).strip()[:300]}. "
                         f"Ветка {branch} уже на GitHub, запись доставки осталась; повтори .deliver")
        try:
            data = json.loads(done.out)
        except ValueError:
            raise Failed("gh pr list вернул не JSON") from None
        if not isinstance(data, list):
            raise Failed("gh pr list вернул не список")
        if not data:
            return None
        if len(data) > 1:
            raise Failed(f"открытых PR из {branch} {len(data)}, нужен не больше одного: закрой лишние")
        pr = data[0]
        if (not isinstance(pr, dict) or pr.get("headRefName") != branch or pr.get("baseRefName") != base
                or not isinstance(pr.get("url"), str) or not isinstance(pr.get("number"), int)
                or isinstance(pr.get("number"), bool)):
            raise Failed(f"PR из {branch} есть, но не в {base} (или ответ gh непонятен): разберись сам")
        return {"number": pr["number"], "url": pr["url"]}

    def open_pr(self, board: Path, rec: dict, title: str, body: str) -> None:
        repo, base, branch = rec["repo"], rec["base"], rec["branch"]
        pr = self.existing_pr(repo, base, branch)
        if pr is not None:
            self.say(f"PR из {branch} уже есть: {pr['url']} (второй не создаю).")
        else:
            fd, name = tempfile.mkstemp(prefix="deliver-body-", suffix=".md")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(body)
                done = self.ops.proc(
                    [self.gh, "pr", "create", "--repo", repo, "--base", base, "--head", branch,
                     "--title", title, "--body-file", name],
                    cwd=None, env=self.env, timeout=GH_TIMEOUT_S)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(name)
            lines = [line.strip() for line in done.out.splitlines() if line.strip()]
            match = PR_URL_RE.match(lines[-1]) if lines else None
            if done.rc != 0 or match is None:
                raise Failed(f"gh pr create не создал PR (код {done.rc}): "
                             f"{(done.err or done.out).strip()[:300]}. Ветка {branch} уже на GitHub, "
                             "запись доставки осталась; повтори .deliver — пуш будет пропущен")
            pr = {"number": int(match.group(1)), "url": lines[-1]}
            self.say(f"PR создан: {pr['url']}")
        rec["pr"] = pr
        self.write_record(board, rec)

    # --- after the PR ----------------------------------------------------------------

    def finish(self, project: dict, board: Path, rec: dict, url: str) -> None:
        assert self.ctx is not None
        note = "\n".join([
            f"- PR: {rec['pr']['url']}",
            f"- коммит доставки: {rec['commit']}",
            f"- источник: {rec['source']} ({rec['source_commit']})",
            f"- {rec['base']} при доставке: {rec['our_main']}",
            "- повторной проверки не было",
        ])
        try:
            with env_var("SYMPHONY_PYTHON", self.ops.tracker_python(project)):
                check.append_note(self.ctx, note, NOTE_HEADING)
        except Exception as exc:  # noqa: BLE001 - the PR exists; a lost note is only reported
            self.say(f"Предупреждение: раздел «## {NOTE_HEADING}» в карточке не записан: {exc}")
        self.log("delivered", f"PR {rec['pr']['url']}", pr=str(rec["pr"]["number"]), commit=rec["commit"],
                 kind=rec["kind"], source=rec["source"], base_tip=rec["our_main"])
        self.fetch_main(url, project)
        self.say(f"Доставлено: PR {rec['pr']['url']}, ветка {rec['branch']}, коммит {rec['commit'][:12]}. "
                 f"Карточка остаётся в {STATE_REVIEW}.")

    def fetch_main(self, url: str, project: dict) -> None:
        """`git fetch origin` in the main folder, only when its origin is the delivery repository."""
        if self.main is None or not (self.main / ".git").exists():
            return
        if post_merge.origin_of(self.main) != post_merge.normalize_url(url):
            return
        done = self.git(self.main, "fetch", "origin", net=True)
        if done.rc != 0:
            self.say(f"Предупреждение: git fetch origin в основной папке не прошёл: {done.err.strip()[:200]}")
        else:
            self.say("В основной папке выполнен git fetch origin.")

    # --- merged ------------------------------------------------------------------------

    def merged(self) -> int:
        try:
            return self._merged()
        except Refused as exc:
            self.log("refused", str(exc))
            self.say(f"Отказ: {exc}")
            return EXIT_REFUSED
        except Failed as exc:
            return self.fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            return self.fail(f"внутренняя ошибка: {type(exc).__name__}: {exc}")

    def _merged(self) -> int:
        project, board = self.load_board()
        assert self.ctx is not None
        lock = check.try_lock(self.ctx, LOCK_WAIT_S)
        if lock is None:
            raise Refused("идёт проверка карточки (занята блокировка .ours-state); попробуй позже")
        try:
            path = delivered_path(board, self.id)
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise Refused(f"нет записи доставки {path.name}: карточка не доставлялась") from None
            except (OSError, ValueError) as exc:
                raise Refused(f"запись {path.name} повреждена: {exc}") from None
            pr = rec.get("pr") if isinstance(rec, dict) else None
            if not isinstance(pr, dict) or not pr.get("url"):
                raise Refused(f"в записи {path.name} нет PR: слияние отмечать не с чем")
            if rec.get("merged_at"):
                raise Refused(f"слияние уже отмечено ({rec['merged_at']}); запись «влито» одноразовая")
            clone = Path(project["project"]["repo"])
            commit = rec.get("commit")
            tree = self.rev(clone, f"{commit}^{{tree}}") if isinstance(commit, str) and SHA_RE.match(commit) else None
            if tree is None:
                raise Failed(f"в клоне {clone} нет коммита доставки {commit}: "
                             "запусти ours/bin/post-merge и повтори deliver merged")
            written = integrate._write_merged(self.ctx, None, rec["base"], f"{DELIVER_PREFIX}{self.id}", tree, commit)
            if written != "written":
                raise Failed("запись «влито» не записана")
            rec["merged_at"] = check._now()
            self.write_record(board, rec)
            try:
                with env_var("SYMPHONY_PYTHON", self.ops.tracker_python(project)):
                    check.append_note(self.ctx, "- PR влит", NOTE_HEADING)
            except Exception as exc:  # noqa: BLE001 - the record is the point; the line is a courtesy
                self.say(f"Предупреждение: строка «PR влит» в карточку не записана: {exc}")
            self.log("merged", f"PR {pr['url']} влит", commit=str(commit), tree=tree)
        finally:
            lock.close()
        self.say(f"Отмечено: PR {pr['url']} влит; запись «влито» (дерево {tree[:12]}) записана, "
                 "шлюз при Done не будет гонять тесты повторно.")
        return EXIT_OK


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deliver", description="Доставка результата карточки на GitHub (S4-27).")
    sub = parser.add_subparsers(dest="command", required=True)
    push = sub.add_parser("push", help="собрать ветку deliver/<ID>, запушить и открыть PR")
    merged = sub.add_parser("merged", help="отметить слияние PR (запись «влито» для шлюза)")
    for p in (push, merged):
        p.add_argument("id", metavar="ID")
        p.add_argument("--root", default=str(pc.DEFAULT_ROOT), help="папка досок")
        p.add_argument("--main", default=None, help="основная папка форка")
    push.add_argument("--gh", default=None, help="путь к gh (по умолчанию — из PATH)")
    return parser


def run(argv: list[str] | None = None, ops: Ops | None = None) -> int:
    args = build_parser().parse_args(argv)

    def say(text: str = "") -> None:
        print(text, flush=True)

    ops = ops or Ops()
    gh = getattr(args, "gh", None) or shutil.which("gh") or ""
    if args.command == "push" and not SAFE_GH_RE.match(gh):
        say(f"Ошибка: нужен абсолютный путь к gh из букв, цифр и . _ / - (получено {gh!r}); укажи --gh")
        return EXIT_ERROR
    main = Path(args.main).expanduser() if args.main else post_merge.default_main()
    root = Path(args.root).expanduser().absolute()
    delivery = Delivery(args.id, root, main.resolve() if main else None, gh, ops, say)
    return delivery.push() if args.command == "push" else delivery.merged()
