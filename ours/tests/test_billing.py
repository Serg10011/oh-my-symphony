"""Tests for the payment mode of a board (TASK-E1b): [billing] of project.toml, the settings
file of `claude` with apiKeyHelper, the key in a diff (check.py, deliver.py) and project-doctor.

Real temporary folders and git repositories. No real API key anywhere: the sample key is built
at run time, so that no file of the repository holds a string a check would take for a key.
`claude` is never started.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from test_check import Env as CheckEnv
from test_check import check
from test_check import assert_passed, assert_refused, git, write
from test_deliver import CARD, REPORT, card_text
from test_deliver import Env as DeliverEnv
from test_project_doctor import Doc

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the kit is POSIX-only")

OURS = Path(__file__).resolve().parents[1]
PROJECT_RENDER = OURS / "bin" / "project-render"
PROJECT_LAUNCHD = OURS / "bin" / "project-launchd"
sys.path.insert(0, str(OURS / "hooks"))

import deliver  # noqa: E402
import project_config as pc  # noqa: E402

KEY = "sk-" + "ant-" + "x" * 30
KEY_LINE = f"API_KEY = {KEY!r}\n"
FOUND = "ключ API Anthropic"


@pytest.fixture(autouse=True)
def _no_key_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in pc.KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# the board: project.toml, key file, folders
# ---------------------------------------------------------------------------


class World:
    """A board folder with project.toml, a key folder and the places the key must stay out of."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp.resolve()
        self.home = self.tmp / "home"
        self.code = self.tmp / "code"
        self.fork = self.tmp / "fork"
        self.board = self.tmp / "projects" / "demo"
        self.keys = self.home / ".symphony-keys"
        self.key = self.keys / "demo.key"
        self.workspaces = self.home / "symphony_workspaces" / "demo"
        for path in (self.home, self.code, self.fork, self.board / "kanban"):
            path.mkdir(parents=True)
        git(self.code, "init", "-q", "-b", "main")  # the core wants a real repository in code_repo
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setattr(pc, "FORK_DIR", self.fork)
        self.keys.mkdir(mode=0o700)
        self.keys.chmod(0o700)
        self.put_key(self.key)
        self.toml('mode = "api"', f'api_key_file = "{self.key}"')

    def put_key(self, path: Path, text: str | None = None, mode: int = 0o600) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"  {KEY}  \n" if text is None else text)
        path.chmod(mode)
        return path

    def toml(self, *billing: str, section: bool = True) -> None:
        lines = [
            "[project]", 'name = "demo"', f'repo = "{self.code}"', 'base_branch = "main"',
            'id_prefix = "BIL"', "port = 9981",
            "[tools]", 'python = "/bin/python"', 'symphony = "/bin/symphony"', 'claude = "/bin/claude"',
        ]
        if section:
            lines += ["[billing]", *billing]
        (self.board / "project.toml").write_text("\n".join(lines) + "\n")

    def project(self) -> dict:
        return pc.load_project(self.board)

    def render(self) -> str:
        return pc.render_workflow(self.board, self.project())

    def render_command(self):
        return subprocess.run(
            [sys.executable, str(PROJECT_RENDER), "demo", "--root", str(self.board.parent)],
            env={**os.environ, "HOME": str(self.home)}, capture_output=True, text=True, timeout=60,
        )

    @property
    def settings(self) -> Path:
        return self.board / "claude-settings.json"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


def legacy_render(world: World) -> str:
    """What the template gave before E1b: the same template without the one new placeholder."""
    legacy = world.tmp / "legacy.tmpl"
    legacy.write_text(pc.TEMPLATE.read_text(encoding="utf-8").replace("{{raw:quota_lines}}", ""))
    return pc.render_workflow(world.board, world.project(), template=legacy)


# ---------------------------------------------------------------------------
# 1. plan: everything as before
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("billing", [(), ('mode = "plan"',), ('mode = "plan"', 'api_key_file = "/keys/x.key"')])
def test_plan_mode_renders_exactly_as_before(world: World, billing: tuple[str, ...]) -> None:
    world.toml(*billing, section=bool(billing))
    text = world.render()
    assert text == legacy_render(world)
    assert "quota_exhausted" not in text and "--settings" not in text and "apiKeyHelper" not in text
    assert world.project()["billing"]["mode"] == "plan"


def test_project_render_in_plan_mode_removes_a_leftover_settings_file(world: World) -> None:
    assert world.render_command().returncode == 0
    assert world.settings.exists()
    world.toml('mode = "plan"')
    result = world.render_command()
    assert result.returncode == 0, result.stderr
    assert not world.settings.exists() and "удалён" in result.stdout
    assert "--settings" not in (world.board / "WORKFLOW.md").read_text()
    assert "без изменений" in world.render_command().stdout


# ---------------------------------------------------------------------------
# 2. api: the settings file, the command, the agent block, the core
# ---------------------------------------------------------------------------


def test_api_mode_writes_settings_command_and_quota_keys(world: World) -> None:
    from symphony.workflow import build_service_config, load_workflow

    result = world.render_command()
    assert result.returncode == 0, result.stderr
    assert KEY not in result.stdout + result.stderr
    assert stat.S_IMODE(world.settings.stat().st_mode) == 0o600
    settings = json.loads(world.settings.read_text())
    assert settings["apiKeyHelper"] == f"/usr/bin/tr -d '[:space:]' < '{world.key}'"
    assert settings["permissions"] == {"deny": [f"Read(/{world.keys}/**)"]}
    assert set(settings) == {"apiKeyHelper", "permissions"}
    assert KEY not in world.settings.read_text()

    text = (world.board / "WORKFLOW.md").read_text()
    cfg = build_service_config(load_workflow(world.board / "WORKFLOW.md"))
    assert cfg.claude.command.endswith(f"--settings {world.settings}")
    assert cfg.claude.command.count("--settings") == 1
    assert cfg.agent.quota_exhausted_state == "needs_human"
    note = cfg.agent.quota_exhausted_note
    assert note == pc.QUOTA_NOTE
    for part in ('[billing] mode = "plan"', "ours/bin/project-render <имя>", "Остальные карточки ждут"):
        assert part in note
    assert text.count("quota_exhausted_state: needs_human") == 1
    assert "  budget_exhausted_state: needs_human\n  # [billing]" in text
    again = world.render_command()
    assert "без изменений" in again.stdout and "записан" not in again.stdout


def test_switching_back_to_plan_restores_the_old_workflow(world: World) -> None:
    assert world.render_command().returncode == 0
    api_text = (world.board / "WORKFLOW.md").read_text()
    world.toml('mode = "plan"', f'api_key_file = "{world.key}"')
    assert world.render_command().returncode == 0
    assert (world.board / "WORKFLOW.md").read_text() == legacy_render(world) != api_text


def test_a_refused_key_leaves_workflow_and_settings_alone(world: World) -> None:
    assert world.render_command().returncode == 0
    before = (world.board / "WORKFLOW.md").read_text(), world.settings.read_text()
    world.key.chmod(0o644)
    result = world.render_command()
    assert result.returncode == 1 and "WORKFLOW.md не пересобран" in result.stderr
    assert ((world.board / "WORKFLOW.md").read_text(), world.settings.read_text()) == before
    assert KEY not in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# 3. refusals of the build and of the loader
# ---------------------------------------------------------------------------


def refuses(world: World, needle: str) -> None:
    with pytest.raises(pc.ProjectError) as caught:
        world.render()
    assert needle in str(caught.value)
    assert KEY not in str(caught.value)


def use_key(world: World, path: Path) -> Path:
    world.put_key(path)
    world.toml('mode = "api"', f'api_key_file = "{path}"')
    return path


def test_refuses_a_missing_file(world: World) -> None:
    world.key.unlink()
    refuses(world, "не найден")


def test_refuses_a_link(world: World) -> None:
    real = world.put_key(world.keys / "real.key")
    world.key.unlink()
    world.key.symlink_to(real)
    refuses(world, "нужен обычный файл")


def test_refuses_a_link_in_the_path(world: World) -> None:
    link = world.tmp / "link"
    link.symlink_to(world.keys)
    world.toml('mode = "api"', f'api_key_file = "{link / "demo.key"}"')
    refuses(world, "символьную ссылку")


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_refuses_loose_rights(world: World, mode: int) -> None:
    world.key.chmod(mode)
    refuses(world, "шире 600")


def test_refuses_a_big_or_empty_file(world: World) -> None:
    world.put_key(world.key, "x" * 2000)
    refuses(world, "больше 1024 байт")
    world.put_key(world.key, "")
    refuses(world, "пуст")


@pytest.mark.parametrize(
    ("place", "label"),
    [
        (lambda w: w.code / "keys" / "demo.key", "репозиторий кода"),
        (lambda w: w.board / "keys" / "demo.key", "папка доски"),
        (lambda w: w.workspaces / "keys" / "demo.key", "корень рабочих копий"),
        (lambda w: w.fork / "keys" / "demo.key", "основная папка форка"),
    ],
)
def test_refuses_a_key_inside_the_work_places(world: World, place, label: str) -> None:
    path = place(world)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    use_key(world, path)
    refuses(world, f"лежит внутри: {label}")


@pytest.mark.parametrize(
    ("place", "label"),
    [
        (lambda w: w.home / "demo.key", "домашняя папка"),
        (lambda w: w.tmp / "demo.key", "содержит её"),  # the folder holds the code repository
        (lambda w: w.board.parent / "demo.key", "папка доски"),
        (lambda w: w.workspaces.parent / "demo.key", "корень рабочих копий"),
        (lambda w: w.fork.parent / "demo.key", "основная папка форка"),
    ],
)
def test_refuses_a_key_folder_shared_with_the_work(world: World, place, label: str) -> None:
    path = place(world)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    use_key(world, path)
    refuses(world, label)


def test_refuses_the_folder_of_a_fork_that_is_a_child_of_the_keys(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = world.tmp / "forks"
    parent.mkdir(mode=0o700)
    (parent / "oms").mkdir()
    monkeypatch.setattr(pc, "FORK_DIR", parent / "oms")
    use_key(world, parent / "demo.key")
    refuses(world, "основная папка форка")


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o701])
def test_refuses_a_loose_key_folder(world: World, mode: int) -> None:
    world.keys.chmod(mode)
    refuses(world, f"права {mode:03o}")


def test_refuses_a_missing_key_folder(world: World) -> None:
    world.key.unlink()
    world.keys.rmdir()
    refuses(world, "папки ключей")


@pytest.mark.parametrize(
    ("lines", "needle"),
    [
        (['mode = "api"'], "api_key_file"),
        (['mode = "api"', 'api_key_file = "keys/demo.key"'], "абсолютный путь"),
        (['mode = "api"', 'api_key_file = "/keys/../demo.key"'], "абсолютный путь"),
        (['mode = "api"', 'api_key_file = "/keys/my key"'], "абсолютный путь"),
        (['mode = "api"', 'api_key_file = "/keys/it\'s"'], "абсолютный путь"),
        (['mode = "api"', 'api_key_file = "/keys/$HOME"'], "абсолютный путь"),
        (['mode = "free"'], "допустимо"),
        (['mode = "api"', 'api_key_file = "/k/x"', 'token = "y"'], "неизвестный ключ"),
        (['mode = 1'], "строка"),
    ],
)
def test_the_loader_refuses_a_bad_billing_section(world: World, lines: list[str], needle: str) -> None:
    world.toml(*lines)
    with pytest.raises(pc.ProjectError) as caught:
        world.project()
    assert needle in str(caught.value)


def test_the_loader_does_not_look_at_the_key_file(world: World) -> None:
    world.key.unlink()
    assert world.project()["billing"]["mode"] == "api"


# ---------------------------------------------------------------------------
# 4. the helper command gives the key and only the key
# ---------------------------------------------------------------------------


def test_the_helper_command_prints_exactly_the_key(world: World) -> None:
    assert world.render_command().returncode == 0
    command = json.loads(world.settings.read_text())["apiKeyHelper"]
    for text in (f"{KEY}\n", f"  {KEY}\r\n\n", KEY):
        world.put_key(world.key, text)
        done = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, timeout=10)
        assert done.returncode == 0 and done.stdout == KEY and done.stderr == ""


# ---------------------------------------------------------------------------
# 5. check.py: a key in an added line
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path) -> CheckEnv:
    return CheckEnv(tmp_path)


def put_key_line(env: CheckEnv, where: str) -> None:
    if where == "committed":
        write(env.ws / "src" / "notes.py", f"# note\n{KEY_LINE}")
        env.commit()
    elif where == "staged":
        write(env.ws / "src" / "notes.py", f"# note\n{KEY_LINE}")
        git(env.ws, "add", "src/notes.py")
    elif where == "unstaged":
        write(env.ws / "src" / "app.py", f"x = 1\n{KEY_LINE}")
    elif where == "untracked":
        write(env.ws / "src" / "notes.py", f"# note\n{KEY_LINE}")
    else:  # a key added to a file the card also changes elsewhere
        write(env.ws / "src" / "app.py", f"x = 2\n{KEY_LINE}")
        env.commit()


WHERE = {"committed": "src/notes.py:2", "staged": "src/notes.py:2", "unstaged": "src/app.py:2",
         "untracked": "src/notes.py:2", "modified": "src/app.py:2"}


@pytest.mark.parametrize("where", sorted(WHERE))
def test_check_refuses_a_key_in_an_added_line(env: CheckEnv, where: str) -> None:
    put_key_line(env, where)
    result = env.run("gate")
    assert_refused(result, FOUND, WHERE[where], "сама строка не выводится")
    assert KEY not in result.stdout + result.stderr
    assert not env.marker.exists()  # before the tests


@pytest.mark.parametrize("where", ["committed", "untracked"])
def test_a_key_never_reaches_the_report_the_card_or_the_log(env: CheckEnv, where: str) -> None:
    put_key_line(env, where)
    result = env.run("hook")
    assert result.returncode == 0
    assert KEY not in result.stdout + result.stderr
    assert env.state() == "In Progress"
    card = env.card.read_text()
    assert "## Check Failure" in card and FOUND in card and KEY not in card
    for path in (env.board / ".ours-state").rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(errors="replace"), path
    assert env.log()[-1]["outcome"].startswith("rollback")


def test_check_names_each_place_and_cuts_a_long_list(env: CheckEnv) -> None:
    write(env.ws / "src" / "many.py", KEY_LINE * 15)
    result = env.run("gate")
    assert_refused(result, "src/many.py:1,", "и ещё 5")
    assert KEY not in result.stdout


def test_a_removed_key_line_is_not_a_refusal(env: CheckEnv) -> None:
    write(env.ws / "src" / "old.py", f"a = 1\n{KEY_LINE}")
    env.commit("a key got in")
    env.record(git(env.ws, "rev-parse", "HEAD").strip())  # the key is in the base now
    write(env.ws / "src" / "old.py", "a = 1\n")
    env.commit("and is gone")
    assert_passed(env.run("gate"))


@pytest.mark.parametrize(
    "line",
    ["see sk-ant-… in the docs\n", "sk-ant-short\n", "token sk-ant-api03-…\n", "sk_ant_" + "x" * 30 + "\n"],
)
def test_text_that_is_not_a_key_passes(env: CheckEnv, line: str) -> None:
    write(env.ws / "src" / "doc.py", f"# {line}")
    env.commit()
    assert_passed(env.run("gate"))


def test_a_binary_file_is_skipped(env: CheckEnv) -> None:
    (env.ws / "src").mkdir(exist_ok=True)
    (env.ws / "src" / "blob.bin").write_bytes(b"\0\1\2" + KEY.encode() + b"\n\0")
    env.commit()
    assert_passed(env.run("gate"))


def test_the_diff_scanner_reads_hunks_without_mistaking_content_for_headers() -> None:
    diff = "\n".join([
        "diff --git a/a.txt b/a.txt", "--- a/a.txt", "+++ b/a.txt",
        "@@ -3,2 +3,3 @@", "-gone", "-gone too", "+++ b/fake.txt", f"+x {KEY}", "+tail",
        "\\ No newline at end of file",
        "@@ -9,0 +11,1 @@", f"+{KEY}",
        "diff --git a/b.txt b/b.txt", "--- /dev/null", "+++ b/b.txt", "@@ -0,0 +1 @@", f"+{KEY}",
    ])
    assert check.diff_key_hits(diff) == [("a.txt", 4), ("a.txt", 11), ("b.txt", 1)]
    assert check.diff_key_hits("") == []


# ---------------------------------------------------------------------------
# 6. deliver push: a key in the diff, the title or the description
# ---------------------------------------------------------------------------


@pytest.fixture
def delivery(tmp_path: Path) -> DeliverEnv:
    return DeliverEnv(tmp_path)


def card_with_key(delivery: DeliverEnv, *, in_diff: bool = False, in_title: bool = False,
                  in_report: bool = False) -> None:
    ws = delivery.worktree(CARD, f"symphony/{CARD}", "main")
    write(ws / "feature.txt", KEY_LINE if in_diff else "feature\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "work")
    text = card_text(CARD, title=f"Add {KEY}" if in_title else "Add feature")
    delivery.card(CARD, text.replace(REPORT, f"report {KEY}") if in_report else text)
    delivery.event(CARD, f"symphony/{CARD}")


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"in_diff": True}, "feature.txt:1"),
        ({"in_title": True}, "заголовке или описании PR"),
        ({"in_report": True}, "заголовке или описании PR"),
    ],
)
def test_deliver_refuses_a_key_and_pushes_nothing(delivery: DeliverEnv, capsys, kwargs: dict, needle: str) -> None:
    card_with_key(delivery, **kwargs)
    before = delivery.heads()
    assert delivery.push() == deliver.EXIT_REFUSED
    out = capsys.readouterr()
    assert "Отказ:" in out.out and FOUND in out.out and needle in out.out
    assert KEY not in out.out + out.err
    assert delivery.pushes() == [] and delivery.creates() == []
    assert delivery.remote_branch(f"deliver/{CARD}") is None and delivery.heads() == before
    log = (delivery.board / pc.STATE_DIR / "check.log").read_text()
    assert KEY not in log and '"outcome": "refused"' in log
    assert not deliver.delivered_path(delivery.board, CARD).exists()


def test_deliver_still_delivers_a_clean_card(delivery: DeliverEnv) -> None:
    card_with_key(delivery)
    assert delivery.push() == deliver.EXIT_OK
    assert len(delivery.creates()) == 1


# ---------------------------------------------------------------------------
# 7. project-doctor and project-launchd
# ---------------------------------------------------------------------------


class DoctorWorld:
    def __init__(self, doc: Doc) -> None:
        self.doc = doc
        doc.ready()
        self.keys = doc.home / ".symphony-keys"
        self.keys.mkdir(mode=0o700)
        self.keys.chmod(0o700)
        self.key = self.keys / "demo.key"
        self.key.write_text(KEY + "\n")
        self.key.chmod(0o600)

    def api(self) -> None:
        with open(self.doc.toml, "a") as fh:
            fh.write(f'\n[billing]\nmode = "api"\napi_key_file = "{self.key}"\n')

    def run(self):
        return self.doc.doctor()


@pytest.fixture
def doctor(tmp_path: Path) -> DoctorWorld:
    doc = Doc(tmp_path.resolve())
    return DoctorWorld(doc)


def test_doctor_in_plan_mode_says_subscription(doctor: DoctorWorld) -> None:
    result = doctor.run()
    assert result.returncode == 0, result.stdout
    assert "оплата: подписка" in result.stdout


def test_doctor_accepts_a_good_key_file(doctor: DoctorWorld) -> None:
    doctor.api()
    result = doctor.run()
    assert result.returncode == 0, result.stdout
    assert "оплата: API-кредит" in result.stdout and "apiKeyHelper выдаёт ключ нужного вида" in result.stdout
    assert KEY not in result.stdout + result.stderr
    assert (doctor.doc.board / "claude-settings.json").exists()


def test_doctor_refuses_loose_rights(doctor: DoctorWorld) -> None:
    doctor.api()
    doctor.key.chmod(0o644)
    result = doctor.run()
    assert result.returncode == 1 and "FAIL" in result.stdout and "шире 600" in result.stdout
    assert KEY not in result.stdout + result.stderr


def test_doctor_refuses_a_key_folder_that_is_the_home(doctor: DoctorWorld) -> None:
    doctor.key = doctor.doc.home / "demo.key"
    doctor.key.write_text(KEY)
    doctor.key.chmod(0o600)
    doctor.doc.home.chmod(0o700)
    doctor.api()
    result = doctor.run()
    assert result.returncode == 1 and "домашняя папка" in result.stdout


def test_doctor_refuses_a_helper_that_prints_something_else(doctor: DoctorWorld) -> None:
    doctor.api()
    secret = "just-a-password-" + "z" * 20
    doctor.key.write_text(secret)
    result = doctor.run()
    assert result.returncode == 1 and "печатает не ключ Anthropic" in result.stdout
    assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["plan", "api"])
@pytest.mark.parametrize("name", pc.KEY_ENV_VARS)
def test_doctor_refuses_key_variables_in_the_environment(doctor: DoctorWorld, mode: str, name: str) -> None:
    if mode == "api":
        doctor.api()
    secret = "value-" + "q" * 24
    doctor.doc.env_extra[name] = secret
    result = doctor.run()
    assert result.returncode == 1
    assert f"FAIL  {name} в окружении, в котором запущен project-doctor" in result.stdout
    assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["plan", "api"])
@pytest.mark.parametrize("name", pc.KEY_ENV_VARS)
def test_doctor_refuses_key_variables_in_the_service_plist(doctor: DoctorWorld, mode: str, name: str) -> None:
    if mode == "api":
        doctor.api()
    secret = "value-" + "w" * 24
    plist = {"Label": "local.symphony.demo", "EnvironmentVariables": {"PATH": "/usr/bin", name: secret}}
    (doctor.doc.board / "launchd.plist").write_bytes(plistlib.dumps(plist))
    result = doctor.run()
    assert result.returncode == 1
    assert f"FAIL  {name} в разделе EnvironmentVariables файла launchd.plist" in result.stdout
    assert secret not in result.stdout + result.stderr


def test_doctor_passes_a_plist_without_key_variables(doctor: DoctorWorld) -> None:
    plist = {"Label": "local.symphony.demo", "EnvironmentVariables": {"PATH": "/usr/bin"}}
    (doctor.doc.board / "launchd.plist").write_bytes(plistlib.dumps(plist))
    result = doctor.run()
    assert result.returncode == 0, result.stdout
    assert "нет ANTHROPIC_API_KEY и ANTHROPIC_AUTH_TOKEN" in result.stdout


def test_project_launchd_never_writes_key_variables(doctor: DoctorWorld) -> None:
    doctor.api()
    secret = "value-" + "e" * 24
    env = {**doctor.doc.env(), "ANTHROPIC_API_KEY": secret, "ANTHROPIC_AUTH_TOKEN": secret}
    result = subprocess.run(
        [sys.executable, str(PROJECT_LAUNCHD), "demo", "--root", str(doctor.doc.root)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    raw = (doctor.doc.board / "launchd.plist").read_bytes()
    assert secret.encode() not in raw
    service_env = plistlib.loads(raw)["EnvironmentVariables"]
    assert not set(pc.KEY_ENV_VARS) & set(service_env)
    assert service_env["SYMPHONY_PYTHON"] == sys.executable


def test_the_stub_of_project_new_shows_the_billing_section(doctor: DoctorWorld) -> None:
    text = doctor.doc.toml.read_text()
    assert "# [billing]" in text and "# mode = \"api\"" in text and "install -d -m 700" in text
    assert "billing" not in tomllib.loads(text)
