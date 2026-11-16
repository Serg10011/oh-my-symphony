"""Match an operator's saved test result to the board and Git revision."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import project_config as pc


def script_digests(board: Path) -> dict[str, str]:
    try:
        return {
            name: hashlib.sha256((board / name).read_bytes()).hexdigest()
            for name in ("setup.sh", "test.sh")
        }
    except OSError as exc:
        raise pc.ProjectError(f"не читаются скрипты проверки проекта: {exc}") from None


def revision(repo: Path, ref: str) -> tuple[str, str, list[str]]:
    with_ref = ["git", "--no-replace-objects", "show", "-s", "--format=%H %T %P", ref, "--"]
    result = subprocess.run(with_ref, cwd=repo, capture_output=True, text=True)
    parts = result.stdout.split()
    if result.returncode or len(parts) < 2 or not all(pc.is_sha(part) for part in parts):
        raise pc.ProjectError(f"не удалось определить дерево выпуска в {repo}")
    commit, tree, *parents = parts
    return commit, tree, parents


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise pc.ProjectError(f"отчёт {path} недоступен или повреждён: {exc}") from None
    if not isinstance(data, dict):
        raise pc.ProjectError(f"отчёт {path} должен содержать объект JSON")
    return data


def _counts(value: object) -> bool:
    return (isinstance(value, dict) and set(value) == set(pc.SUMMARY_KEYS)
            and all(type(count) is int and count >= 0 for count in value.values()))


def _check_recheck(path: Path, report: dict) -> None:
    """Accept the recorded OMS format only when every original failure passed."""
    retry = _read_json(path.with_name("targeted-verification.json"))
    try:
        lines = path.with_name("full-run.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise pc.ProjectError(f"журнал первого прогона недоступен: {exc}") from None
    failed_cases = {
        line.split(" ", 1)[1].split(" - ", 1)[0]
        for line in lines if line.startswith(("FAILED ", "ERROR ")) and "::" in line
    }
    count = report["counts"]["failed"]
    cases = retry.get("case_ids")
    if (pc.find_summary(lines) != report["counts"] or len(failed_cases) != count
            or not isinstance(cases, list) or not all(isinstance(case, str) for case in cases)
            or len(cases) != count or set(cases) != failed_cases
            or retry.get("commit") != report["commit"] or retry.get("tree") != report["tree"]
            or retry.get("tree_after") != report["tree"] or retry.get("tracked_files_clean") is not True
            or retry.get("passed") is not True or type(retry.get("test_exit_code")) is not int
            or retry["test_exit_code"] != 0
            or not _counts(retry.get("counts")) or retry["counts"] != {"passed": count, "skipped": 0, "failed": 0}):
        raise pc.ProjectError("точечная перепроверка не подтверждает все ошибки первого прогона")


def read(path: Path) -> dict:
    report = _read_json(path)
    commit, tree, counts = (report.get(key) for key in ("commit", "tree", "counts"))
    if (not pc.is_sha(commit) or not pc.is_sha(tree) or report.get("tree_after") != tree
            or report.get("tracked_files_clean") is not True or not _counts(counts)
            or type(report.get("attempt")) is not int or report["attempt"] != 1
            or not isinstance(report.get("test_script"), str) or type(report.get("test_exit_code")) is not int):
        raise pc.ProjectError("в отчёте отсутствуют точная версия, чистое дерево или итоги полного прогона")
    if counts["failed"]:
        if report.get("passed") is not False or report["test_exit_code"] != 1 or "measurement" in report:
            raise pc.ProjectError("первый прогон не завершён с проверяемым списком ошибок")
        _check_recheck(path, report)
    elif report.get("passed") is not True or report["test_exit_code"] != 0:
        raise pc.ProjectError("в отчёте нет успешного результата проверки")
    if "measurement" in report:
        measure = report["measurement"]
        keys = {*pc.SUMMARY_KEYS, "base_sha", "base_branch", "setup_s", "tests_s", "measured_at"}
        if (not pc._check_baseline_record(measure) or set(measure) != keys or measure["base_sha"] != commit
                or any(measure[key] != counts[key] for key in pc.SUMMARY_KEYS)
                or not isinstance(measure.get("measured_at"), str)
                or any(type(measure[key]) not in (float, int) or not 0 <= measure[key] <= 3601
                       for key in ("setup_s", "tests_s"))
                or not isinstance(report.get("setup_script_sha256"), str)):
            raise pc.ProjectError("замер для выпуска повреждён")
    return report


def verify_board(report: dict, board: Path, base_branch: str) -> None:
    if report["test_script"] != str(board.resolve() / "test.sh"):
        raise pc.ProjectError("отчёт проверки относится к другой доске")
    digests = script_digests(board)
    if (report.get("test_script_sha256") != digests["test.sh"]
            or ("setup_script_sha256" in report and report["setup_script_sha256"] != digests["setup.sh"])):
        raise pc.ProjectError("скрипты проверки изменились после записи отчёта")
    if "measurement" in report and report["measurement"]["base_branch"] != base_branch:
        raise pc.ProjectError("в отчёте указана другая базовая ветка")


def verify_revision(report: dict, repo: Path, ref: str) -> str:
    commit, tree, parents = revision(repo, ref)
    # A normal GitHub merge preserves the tested commit as its second parent.
    direct_merge = len(parents) == 2 and parents[1] == report["commit"]
    if tree != report["tree"] or (commit != report["commit"] and not direct_merge):
        raise pc.ProjectError(f"версия в {repo} не совпадает с проверенным выпуском {report['commit'][:12]}")
    return commit
