"""Update the fork after a PR merge (TASK-S4-01). Logic of ours/bin/post-merge.

Pulls the main folder and the project clones, decides from the changed files
what each project needs (restart / project-doctor / project-render), does it
without touching cards in work, and tidies up merged dev worktrees.

Everything external (launchctl, the service API, the project scripts) goes
through `Ops`, so tests substitute it. Standard library only.
"""

from __future__ import annotations

import argparse
import fcntl
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import project_config as pc  # noqa: E402

BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
BRANCH = "our-main"
STATE_REL = Path("tmp_workspaces") / "post-merge-state.json"
INBOX_REL = Path("tmp_workspaces") / "inbox"
LABEL_PREFIX = "local.symphony."
PLIST_FILE = "launchd.plist"
API_WAIT_S = 30

# What a change means. The single place to edit the decisions.
#   restart: files of the MAIN folder loaded into the service process;
#   render:  files of the MAIN folder read when WORKFLOW.md is built
#            (the service re-reads WORKFLOW.md and prompts on every tick);
#   doctor:  files of the PROJECT CLONE that change what the tests measure.
RULES = {
    "restart": {"prefixes": ("src/",), "files": ()},
    "render": {
        "prefixes": ("ours/templates/", "ours/prompts/"),
        "files": ("ours/hooks/project_config.py",),
    },
    # One rule with check.py (which baseline to trust): project_config.BASELINE_RULE, the
    # default of a project without [baseline] paths (E2); see ProjectPlan.baseline_rule.
    "doctor": pc.BASELINE_RULE,
}

EXIT_OK, EXIT_FAIL, EXIT_DEFERRED = 0, 1, 2


def matches(kind: str, path: str) -> bool:
    rule = RULES[kind]
    return (
        path.startswith(rule["prefixes"]) or path in rule["files"]
        or any(fnmatch.fnmatch(Path(path).name, p) for p in rule.get("basenames", ()))
    )


# ---------------------------------------------------------------------------
# external world
# ---------------------------------------------------------------------------


def api_state(port: int) -> dict | None:
    """GET /api/v1/state of the service; None when it does not answer."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/state", timeout=3) as resp:
            data = json.load(resp)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def launchctl(args: list[str]) -> tuple[int, str] | None:
    """Run launchctl; None when there is no launchctl on this machine."""
    exe = shutil.which("launchctl")
    if exe is None:
        return None
    done = subprocess.run([exe, *args], capture_output=True, text=True)
    return done.returncode, (done.stdout + done.stderr).strip()


def run_script(name: str, args: list[str]) -> int:
    """Run ours/bin/<name> with the same Python as this script; output is shown."""
    return subprocess.run([sys.executable, str(BIN_DIR / name), *args]).returncode


@dataclass
class Ops:
    api_state: Callable[[int], dict | None] = api_state
    launchctl: Callable[[list[str]], tuple[int, str] | None] = launchctl
    run_script: Callable[[str, list[str]], int] = run_script
    sleep: Callable[[float], None] = time.sleep
    uid: int = field(default_factory=os.getuid)
    wait_s: float = API_WAIT_S

    def wait_api(self, port: int) -> bool:
        deadline = time.monotonic() + self.wait_s
        while True:
            if self.api_state(port) is not None:
                return True
            if time.monotonic() >= deadline:
                return False
            self.sleep(1)


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def git_out(cwd: Path, *args: str) -> str:
    done = git(cwd, *args)
    return done.stdout.strip() if done.returncode == 0 else ""


def normalize_url(url: str) -> str:
    """Comparable form of a git remote URL (scheme, credentials, .git stripped)."""
    u = url.strip()
    if u.startswith(("/", ".", "~")):
        u = os.path.realpath(os.path.expanduser(u))
    elif "://" not in u and (m := re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", u)):
        u = f"{m[1]}/{m[2]}"
    else:
        u = re.sub(r"^[A-Za-z+]+://", "", u)
        u = re.sub(r"^[^@/]*@", "", u)
    u = u.rstrip("/")
    u = u.removesuffix(".git")
    return u.lower()


def origin_of(repo: Path) -> str | None:
    url = git_out(repo, "remote", "get-url", "origin")
    return normalize_url(url) if url else None


def default_main() -> Path | None:
    """Main working copy of the repository this script lives in."""
    out = git_out(Path(__file__).resolve().parent, "worktree", "list", "--porcelain")
    for line in out.splitlines():
        if line.startswith("worktree "):
            return Path(line.removeprefix("worktree "))
    return None


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@dataclass
class Target:
    key: str
    path: Path
    label: str
    before: str = ""
    head: str = ""
    base: str = ""
    files: list[str] = field(default_factory=list)


@dataclass
class ProjectPlan:
    name: str
    board: Path
    port: int
    main_key: str
    clone_key: str
    restart: bool = False
    render: bool = False
    doctor: bool = False
    # E2: the rule of the project ([baseline] paths); None = RULES["doctor"].
    baseline_rule: Callable[[str], bool] | None = None
    # Why the project.toml cannot be used; the project then gets nothing done.
    refusal: str = ""

    @property
    def needed(self) -> bool:
        return self.restart or self.render or self.doctor or bool(self.refusal)

    @property
    def keys(self) -> set[str]:
        keys = set()
        if self.restart or self.render:
            keys.add(self.main_key)
        if self.doctor or self.refusal:
            keys.add(self.clone_key)
        if self.refusal:
            keys.add(self.main_key)
        return keys

    def describe(self) -> list[str]:
        """Human actions after the 'doctor includes render / start is restart' merging."""
        if self.refusal:
            return [f"ОТКАЗ: {self.refusal}"]
        if self.doctor:
            return ["остановить службу → project-doctor → запустить службу"]
        out = []
        if self.render:
            out.append("project-render (служба подхватит сама, без перезапуска)")
        if self.restart:
            out.append("перезапуск службы")
        return out


def classify(plan: ProjectPlan, main: Target, clone: Target) -> None:
    if plan.refusal:
        return
    plan.restart = any(matches("restart", f) for f in main.files)
    plan.render = any(matches("render", f) for f in main.files)
    rule = plan.baseline_rule or (lambda f: matches("doctor", f))
    plan.doctor = any(rule(f) for f in clone.files)


def find_projects(root: Path, main: Path, only: list[str]) -> tuple[list[tuple[Path, dict, Path]], list[str]]:
    """Projects whose repo is a clone of the same repository as `main`."""
    problems: list[str] = []
    main_origin = origin_of(main)
    found = []
    unreadable: list[tuple[Path, str]] = []
    projects = pc.other_projects(root, unreadable)
    for board, why in unreadable:
        # Without project.toml neither the repository nor the rule of the project is known:
        # "measure again: no" would be a guess (E2), so the run is refused.
        if not only or board.name in only:
            problems.append(f"проект {board.name}: {why}. Отказ: без project.toml нельзя решить, "
                            "нужен ли project-doctor; исправь файл и запусти post-merge ещё раз")
    for board, raw in projects:
        name = raw.get("name", board.name)
        if only and name not in only and board.name not in only:
            continue
        repo = raw.get("repo")
        if not isinstance(repo, str):
            print(f"предупреждение: в {board / pc.PROJECT_FILE} нет [project] repo — пропуск", file=sys.stderr)
            continue
        repo_path = Path(repo).expanduser()
        if not repo_path.is_dir():
            print(f"предупреждение: проект {name}: папки {repo_path} нет — пропуск", file=sys.stderr)
            continue
        same = repo_path.resolve() == main.resolve()
        origin = origin_of(repo_path)
        if same or (origin is not None and origin == main_origin):
            found.append((board, raw, repo_path.resolve()))
    names = {raw.get("name", b.name) for b, raw, _ in found} | {b.name for b, _, _ in found}
    for want in only:
        if want not in names:
            problems.append(f"проект {want!r} не найден среди проектов этого репозитория в {root}")
    return found, problems


def precheck(target: Target) -> str | None:
    branch = git_out(target.path, "branch", "--show-current")
    if branch != BRANCH:
        return (f"{target.label} ({target.path}) стоит на ветке {branch or '(нет ветки)'}, "
                f"а нужна {BRANCH}. Переключать ветку должен человек")
    dirty = git_out(target.path, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        return (f"{target.label} ({target.path}): есть незакоммиченные изменения "
                f"отслеживаемых файлов — обновление отменено, stash не делаю:\n{dirty}")
    return None


def load_state(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return dict(data.get("applied", {}))
    except (OSError, ValueError, AttributeError):
        return {}


def save_state(path: Path, applied: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"applied": applied}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# cleanup of merged dev worktrees
# ---------------------------------------------------------------------------


def dev_worktrees(main: Path) -> list[tuple[Path, str]]:
    out = git_out(main, "worktree", "list", "--porcelain")
    found, path = [], None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line.startswith("branch refs/heads/dev/") and path is not None:
            found.append((path, line.removeprefix("branch refs/heads/")))
    return [(p, b) for p, b in found if p.resolve() != main.resolve()]


def has_own_commits(main: Path, branch: str) -> bool:
    """False for a branch that still sits where it was created (merged only in name)."""
    log = git_out(main, "reflog", "show", "--format=%H", f"refs/heads/{branch}").splitlines()
    return not log or log[0] != log[-1]


def cleanup_plan(main: Path, ref: str) -> tuple[list[tuple[Path, str]], list[str]]:
    merged = set(git_out(main, "branch", "--merged", ref, "--format=%(refname:short)").split())
    here = Path.cwd().resolve()
    take, skip = [], []
    for path, branch in dev_worktrees(main):
        if branch not in merged:
            skip.append(f"{branch}: не влита в {BRANCH} — оставлена")
        elif not has_own_commits(main, branch):
            skip.append(f"{branch}: своих коммитов нет — оставлена")
        elif git_out(path, "status", "--porcelain"):
            skip.append(f"{branch}: в {path} есть незакоммиченные или неотслеживаемые файлы — оставлена")
        elif here == path.resolve() or path.resolve() in here.parents:
            skip.append(f"{branch}: вы сейчас в {path} — оставлена")
        else:
            take.append((path, branch))
    return take, skip


def cleanup(main: Path, take: list[tuple[Path, str]]) -> list[str]:
    done = []
    git(main, "worktree", "prune")
    for path, branch in take:
        removed = git(main, "worktree", "remove", str(path))
        if removed.returncode != 0:
            done.append(f"{branch}: не удалось убрать {path}: {removed.stderr.strip()}")
            continue
        deleted = git(main, "branch", "-d", branch)
        note = "" if deleted.returncode == 0 else f" (ветку удалить не вышло: {deleted.stderr.strip()})"
        inbox = main / INBOX_REL / f"{branch.removeprefix('dev/')}.md"
        if inbox.is_file():
            inbox.unlink()
            note += f"; файл {inbox.name} из inbox убран"
        done.append(f"{branch}: рабочая копия {path} убрана{note}")
    return done


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

DONE, DEFERRED, FAILED = "done", "deferred", "failed"


def busy_cards(state: dict) -> int | None:
    """running + retrying, None when the answer cannot be understood."""
    counts = state.get("counts")
    try:
        return int(counts["running"]) + int(counts["retrying"])
    except (TypeError, KeyError, ValueError):
        return None


def start_hint(board: Path) -> str:
    return (f"служба не запущена; запустить: ours/bin/project-launchd {board.name}, "
            f"затем launchctl bootstrap gui/$(id -u) {board / PLIST_FILE}")


def launchd_step(ops: Ops, args: list[str], what: str) -> str | None:
    """None on success, otherwise a human error text."""
    res = ops.launchctl(args)
    if res is None:
        return f"{what}: на этой машине нет launchctl (не macOS?) — сделайте это вручную"
    if res[0] != 0:
        return f"{what}: launchctl вернул {res[0]}: {res[1]}"
    return None


def start_and_wait(ops: Ops, plan: ProjectPlan, say: Callable[[str], None]) -> bool:
    plist = plan.board / PLIST_FILE
    if not plist.is_file():
        say(f"  нет {plist} — запустить нельзя. {start_hint(plan.board)}")
        return False
    err = launchd_step(ops, ["bootstrap", f"gui/{ops.uid}", str(plist)], "запуск службы")
    if err:
        say(f"  {err}")
        return False
    if not ops.wait_api(plan.port):
        say(f"  служба запущена, но порт {plan.port} не отвечает за {int(ops.wait_s)} с — смотрите log/symphony.log")
        return False
    say("  служба запущена и отвечает")
    return True


def apply_project(plan: ProjectPlan, ops: Ops, root: Path, say: Callable[[str], None]) -> str:
    """Do what the project needs. Returns DONE / DEFERRED / FAILED."""
    if plan.refusal:
        say(f"  ОТКАЗ: {plan.refusal}. Исправь project.toml проекта и запусти ours/bin/post-merge ещё раз; "
            "для проекта ничего не сделано.")
        return FAILED
    root_args = ["--root", str(root)]
    state = ops.api_state(plan.port) if (plan.doctor or plan.restart or plan.render) else None
    running = state is not None
    busy = busy_cards(state) if state is not None else 0
    changed = ", ".join(plan.describe())

    if (plan.doctor or plan.restart) and running and busy != 0:
        what = "непонятный ответ" if busy is None else f"в работе карточек: {busy}"
        say(f"  ОТЛОЖЕНО ({what}). Нужно: {changed}. Службу и файлы не трогаю, чтобы не прервать карточки. "
            f"Дождитесь, когда карточки выйдут из работы, и запустите ours/bin/post-merge ещё раз "
            f"(или остановите службу сами).")
        return DEFERRED

    if plan.doctor:
        stopped = False
        if running:
            plist = plan.board / PLIST_FILE
            if not plist.is_file():
                say(f"  нет {plist}: остановить службу не могу. Остановите её сами и запустите post-merge ещё раз.")
                return FAILED
            err = launchd_step(ops, ["bootout", f"gui/{ops.uid}", str(plist)], "остановка службы")
            if err:
                say(f"  {err}")
                return FAILED
            stopped = True
        ok = False
        try:
            say("  запускаю project-doctor (может занять много времени)")
            ok = ops.run_script("project-doctor", [plan.name, *root_args]) == 0
        finally:
            if stopped and not start_and_wait(ops, plan, say):
                ok = False
        if not ok:
            say("  project-doctor или запуск службы не удались — смотрите вывод выше; изменения останутся «непримёнными», "
                "повторный запуск post-merge попробует снова.")
            return FAILED
        if not stopped:
            say(f"  служба не была запущена. {start_hint(plan.board)}")
        return DONE

    if plan.render:
        say("  запускаю project-render")
        if ops.run_script("project-render", [plan.name, *root_args]) != 0:
            say("  project-render отказал — смотрите вывод выше")
            return FAILED
        if running and not plan.restart and busy:
            say("  в работе есть карточки: новый WORKFLOW.md подхватится на следующем такте")

    if plan.restart:
        if not running:
            say(f"  перезапускать нечего: {start_hint(plan.board)}")
            return DONE
        err = launchd_step(ops, ["kickstart", "-k", f"gui/{ops.uid}/{LABEL_PREFIX}{plan.name}"],
                           "перезапуск службы")
        if err:
            say(f"  {err}")
            say(f"  если служба запущена не из launchd, перезапустите её сами; иначе: {start_hint(plan.board)}")
            return FAILED
        if not ops.wait_api(plan.port):
            say(f"  порт {plan.port} не отвечает за {int(ops.wait_s)} с после перезапуска — смотрите log/symphony.log")
            return FAILED
        say("  служба перезапущена и отвечает")
    return DONE


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def run(argv: list[str] | None = None, ops: Ops | None = None) -> int:
    ops = ops or Ops()
    parser = argparse.ArgumentParser(prog="post-merge", description="Обновить форк и проекты после слияния PR.")
    parser.add_argument("--project", action="append", default=[], metavar="ИМЯ")
    parser.add_argument("--dry-run", action="store_true", help="только fetch и план; ничего не менять")
    parser.add_argument("--root", default=str(pc.DEFAULT_ROOT))
    parser.add_argument("--main", default=None, help="основная папка форка")
    parser.add_argument("--state", default=None, help="файл состояния")
    args = parser.parse_args(argv)

    def say(text: str = "") -> None:
        print(text, flush=True)

    main = Path(args.main).expanduser() if args.main else default_main()
    if main is None or not (main / ".git").exists():
        say("post-merge: не нашёл основную папку форка; укажите --main")
        return EXIT_FAIL
    main = main.resolve()
    root = Path(args.root).expanduser().absolute()
    state_path = Path(args.state).expanduser() if args.state else main / STATE_REL
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(state_path.with_name(state_path.name + ".lock"), "w")  # noqa: SIM115 - held until exit
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        say("post-merge: уже запущен другой post-merge (занят файл-замок). Дождитесь его завершения.")
        return EXIT_FAIL
    try:
        return _run_locked(args, ops, main, root, state_path, say)
    finally:
        lock.close()


def _run_locked(args, ops: Ops, main: Path, root: Path, state_path: Path, say) -> int:
    dry = args.dry_run
    projects, problems = find_projects(root, main, args.project)
    if problems:
        for p in problems:
            say(f"Ошибка: {p}")
        return EXIT_FAIL

    targets: dict[str, Target] = {str(main): Target(str(main), main, "основная папка")}
    plans: list[ProjectPlan] = []
    for board, raw, repo in projects:
        key = str(repo)
        targets.setdefault(key, Target(key, repo, f"клон проекта {raw.get('name', board.name)}"))
        plan = ProjectPlan(raw.get("name", board.name), board, int(raw.get("port", 0)), str(main), key)
        try:
            plan.baseline_rule = pc.load_baseline_rule(board)
        except pc.ProjectError as exc:
            plan.refusal = str(exc)
        plans.append(plan)

    say(f"{'ПЛАН (--dry-run, ничего не меняется)' if dry else 'Обновление'}: основная папка {main}")
    say(f"Проекты этого репозитория: {', '.join(p.name for p in plans) or 'нет'}")

    # Step 1: checks before anything is done.
    refusals = [r for t in targets.values() if (r := precheck(t))]
    if refusals:
        say("\nОТКАЗ — ничего не обновлено:")
        for r in refusals:
            say(f"- {r}")
        return EXIT_FAIL

    # Step 2: fetch / pull.
    applied = load_state(state_path)
    for t in targets.values():
        t.before = git_out(t.path, "rev-parse", "HEAD")
        fetched = git(t.path, "fetch")
        if fetched.returncode != 0:
            say(f"\nОШИБКА: git fetch в {t.path}: {fetched.stderr.strip()}")
            return EXIT_FAIL
        if dry:
            t.head = git_out(t.path, "rev-parse", f"origin/{BRANCH}") or t.before
        else:
            pulled = git(t.path, "pull", "--ff-only")
            if pulled.returncode != 0:
                say(f"\nОШИБКА: git pull --ff-only в {t.path}: {(pulled.stderr or pulled.stdout).strip()}\n"
                    "Это не простое «догнать» (ветки разошлись?) — разберитесь вручную. Ничего не применено.")
                return EXIT_FAIL
            t.head = git_out(t.path, "rev-parse", "HEAD")

    # Step 3: what changed since the last handled commit.
    for t in targets.values():
        t.base = applied.get(t.key) or t.before
        diff = git(t.path, "diff", "--name-only", f"{t.base}..{t.head}")
        if diff.returncode != 0:
            say(f"\nОШИБКА: не могу сравнить {t.base[:9]}..{t.head[:9]} в {t.path}: {diff.stderr.strip()}")
            return EXIT_FAIL
        t.files = diff.stdout.split()
        pulled_n = git_out(t.path, "rev-list", "--count", f"{t.before}..{t.head}")
        say(f"- {t.label}: {t.before[:9]} → {t.head[:9]} (подтянуто коммитов: {pulled_n or 0}), "
            f"изменённых файлов с последнего применения: {len(t.files)}")

    # Step 4: decisions.
    for plan in plans:
        classify(plan, targets[plan.main_key], targets[plan.clone_key])
    say("\nЧто потребуется:")
    for plan in plans:
        what = plan.describe() if plan.needed else ["ничего"]
        say(f"- проект {plan.name}: {'; '.join(what)}")
    main_files = targets[str(main)].files
    if any(f.startswith(("ours/hooks/", "ours/bin/")) and not matches("render", f) for f in main_files):
        say("- хуки и скрипты ours/ подхватятся сами при следующем вызове — действий не нужно")

    statuses: dict[str, str] = {}
    if dry:
        for plan in plans:
            if plan.doctor or plan.restart:
                state = ops.api_state(plan.port)
                if state is None:
                    say(f"  {plan.name}: служба сейчас не отвечает на порту {plan.port} (перезапускать нечего)")
                else:
                    n = busy_cards(state)
                    say(f"  {plan.name}: карточек в работе сейчас: {'неизвестно' if n is None else n}"
                        + (" — действие было бы отложено" if n != 0 else ""))
        take, skip = cleanup_plan(main, f"origin/{BRANCH}")
        say("\nБудет убрано: " + ("; ".join(f"{b} ({p})" for p, b in take) or "ничего"))
        for s in skip:
            say(f"  пропущено: {s}")
        say("\nИтог: --dry-run, ничего не изменено (кроме ссылок fetch).")
        return EXIT_OK

    # Steps 5-6: do it.
    for plan in plans:
        if not plan.needed:
            continue
        say(f"\nПроект {plan.name}:")
        statuses[plan.name] = apply_project(plan, ops, root, say)

    # applied_sha only for targets whose actions all succeeded.
    blocked: set[str] = set()
    for plan in plans:
        if statuses.get(plan.name) in (DEFERRED, FAILED):
            blocked |= plan.keys
    for t in targets.values():
        if t.key not in blocked:
            applied[t.key] = t.head
    save_state(state_path, applied)

    # Step 7: tidy up.
    take, skip = cleanup_plan(main, BRANCH)
    cleaned = cleanup(main, take)

    say("\n=== Итог ===")
    for t in targets.values():
        say(f"- {t.label}: на {t.head[:9]}")
    done = [n for n, s in statuses.items() if s == DONE]
    deferred = [n for n, s in statuses.items() if s == DEFERRED]
    failed = [n for n, s in statuses.items() if s == FAILED]
    say("- сделано: " + (", ".join(done) or ("ничего" if statuses else "действий не потребовалось")))
    if deferred:
        say(f"- ОТЛОЖЕНО из-за карточек в работе: {', '.join(deferred)}. "
            "Дождитесь, когда карточки выйдут из работы, и запустите ours/bin/post-merge ещё раз.")
    if failed:
        say(f"- ОШИБКА: {', '.join(failed)} — смотрите вывод выше; повторный запуск попробует снова.")
    say("- убрано: " + ("; ".join(cleaned) or "ничего"))
    for s in skip:
        say(f"  пропущено: {s}")
    if failed:
        return EXIT_FAIL
    if deferred:
        return EXIT_DEFERRED
    return EXIT_OK
