"""Tests for ours/hooks/summarize.py (TASK-K08b).

The samples follow the JUnit XML each runner writes: Vitest
(`--reporter=junit`), pytest (`--junitxml`, xunit2 family) and Jest
(`jest-junit`), plus nested <testsuite> elements. summarize.py always runs
as a subprocess, the way test.sh calls it.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SUMMARIZE = Path(__file__).resolve().parents[1] / "hooks" / "summarize.py"

VITEST = """\
<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="5" failures="1" errors="0" time="0.0123">
    <testsuite name="src/math.test.ts" timestamp="2026-10-24T10:00:00.000Z" hostname="mac" tests="4" failures="1" errors="0" skipped="1" time="0.0050">
        <testcase classname="src/math.test.ts" name="math &gt; adds" time="0.0010">
        </testcase>
        <testcase classname="src/math.test.ts" name="math &gt; subtracts" time="0.0010">
            <failure message="expected 1 to be 2 // Object.is equality" type="AssertionError">
AssertionError: expected 1 to be 2 // Object.is equality
 ❯ src/math.test.ts:9:17
            </failure>
        </testcase>
        <testcase classname="src/math.test.ts" name="math &gt; later" time="0">
            <skipped/>
        </testcase>
        <testcase classname="src/math.test.ts" name="math &gt; logs" time="0.0010">
            <system-out>
hello
            </system-out>
        </testcase>
    </testsuite>
    <testsuite name="src/ui.test.ts" timestamp="2026-10-24T10:00:00.000Z" hostname="mac" tests="1" failures="0" errors="0" skipped="0" time="0.0020">
        <testcase classname="src/ui.test.ts" name="renders" time="0.0020">
        </testcase>
    </testsuite>
</testsuites>
"""

PYTEST = (
    '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
    '<testsuite name="pytest" errors="1" failures="1" skipped="2" tests="6" time="0.051" '
    'timestamp="2026-10-24T10:00:00.000000+00:00" hostname="mac">'
    '<testcase classname="tests.test_app" name="test_ok" time="0.001" />'
    '<testcase classname="tests.test_app" name="test_fail" time="0.001">'
    '<failure message="assert 1 == 2">def test_fail():\n&gt;       assert 1 == 2\n'
    'E       assert 1 == 2\n\ntests/test_app.py:5: AssertionError</failure></testcase>'
    '<testcase classname="tests.test_app" name="test_skip" time="0.000">'
    '<skipped type="pytest.skip" message="not today">tests/test_app.py:9: not today</skipped>'
    '</testcase>'
    '<testcase classname="tests.test_app" name="test_xfail" time="0.000">'
    '<skipped type="pytest.xfail" message="known bug" /></testcase>'
    '<testcase classname="tests.test_app" name="test_setup_error" time="0.000">'
    '<error message="failed on setup with &quot;RuntimeError: boom&quot;">'
    'RuntimeError: boom</error></testcase>'
    '<testcase classname="tests.test_app" name="test_param[1]" time="0.001">'
    '<system-out>captured</system-out></testcase>'
    '</testsuite></testsuites>'
)

JEST = """\
<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="jest tests" tests="3" failures="1" errors="0" time="1.234">
  <testsuite name="Button" errors="0" failures="1" skipped="1" timestamp="2026-10-24T10:00:00" time="0.5" tests="3">
    <properties>
      <property name="file" value="src/Button.test.js"/>
    </properties>
    <testcase classname="Button renders" name="Button renders" time="0.01">
    </testcase>
    <testcase classname="Button clicks" name="Button clicks" time="0.02">
      <failure>Error: expect(received).toBe(expected) // Object.is equality</failure>
    </testcase>
    <testcase classname="Button pending" name="Button pending" time="0">
      <skipped/>
    </testcase>
  </testsuite>
  <testsuite name="empty" errors="0" failures="0" skipped="0" timestamp="2026-10-24T10:00:00" time="0" tests="0">
  </testsuite>
</testsuites>
"""

NESTED = """\
<testsuite name="root" tests="99" failures="0">
  <testsuite name="a">
    <testcase name="1"/>
    <testsuite name="a.b">
      <testcase name="2"><error type="Crash"/></testcase>
      <testcase name="3"/>
    </testsuite>
  </testsuite>
  <testcase name="4"><skipped/></testcase>
  <testcase name="5"><failure/><skipped/></testcase>
</testsuite>
"""


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SUMMARIZE), *args], capture_output=True,
                          text=True, timeout=60)


@pytest.mark.parametrize(
    ("xml", "expected"),
    [
        (VITEST, "passed=3 skipped=1 failed=1"),
        (PYTEST, "passed=2 skipped=2 failed=2"),
        (JEST, "passed=1 skipped=1 failed=1"),
        # Suite counters are ignored; failure wins over skipped in one case.
        (NESTED, "passed=2 skipped=1 failed=2"),
        ("<testsuites/>", "passed=0 skipped=0 failed=0"),
    ],
    ids=["vitest", "pytest", "jest", "nested", "no-tests"],
)
def test_summary_line(tmp_path: Path, xml: str, expected: str) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(xml, encoding="utf-8")
    result = run(str(report))
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"PIPELINE_TESTS {expected}\n"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "cannot read"),
        ("", "is empty"),
        ("  \n\t\n", "is empty"),
        ("not xml at all", "is not XML"),
        ('<testsuites><testcase name="cut"', "is not XML"),
        ("<html><body/></html>", "root <html>"),
    ],
    ids=["missing", "empty", "blank", "garbage", "truncated", "foreign-root"],
)
def test_unreadable_report_prints_no_line(tmp_path: Path, content: str | None, reason: str) -> None:
    report = tmp_path / "junit.xml"
    if content is not None:
        report.write_text(content, encoding="utf-8")
    result = run(str(report))
    assert result.returncode == 1
    assert result.stdout == ""
    assert reason in result.stderr


def test_usage_without_a_file() -> None:
    result = run()
    assert result.returncode == 2 and result.stdout == ""


def test_real_pytest_report(tmp_path: Path) -> None:
    """The same path test.sh takes: pytest --junitxml, then summarize.py."""
    sample = tmp_path / "proj" / "test_sample.py"
    sample.parent.mkdir()
    sample.write_text(
        "import pytest\n\n"
        "def test_ok():\n    pass\n\n"
        "def test_fail():\n    assert 1 == 2\n\n"
        "@pytest.mark.skip(reason='later')\ndef test_skip():\n    pass\n\n"
        "@pytest.mark.parametrize('x', [1, 2])\ndef test_param(x):\n    pass\n\n"
        "@pytest.fixture\ndef broken():\n    raise RuntimeError('boom')\n\n"
        "def test_error(broken):\n    pass\n"
    )
    report = tmp_path / "junit.xml"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         f"--junitxml={report}", str(sample.parent)],
        cwd=sample.parent, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 1, proc.stdout
    result = run(str(report))
    assert result.stdout == "PIPELINE_TESTS passed=3 skipped=1 failed=2\n"


def test_several_reports_are_summed(tmp_path: Path) -> None:
    reports = []
    for name, xml in (("a", VITEST), ("b", PYTEST), ("c", "<testsuites/>")):
        path = tmp_path / f"{name}.xml"
        path.write_text(xml, encoding="utf-8")
        reports.append(str(path))
    result = run(*reports)
    assert result.returncode == 0, result.stderr
    # vitest 3/1/1 + pytest 2/2/2 + empty
    assert result.stdout == "PIPELINE_TESTS passed=5 skipped=3 failed=3\n"


def test_one_report_gives_the_same_line_as_before(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(JEST, encoding="utf-8")
    assert run(str(report)).stdout == "PIPELINE_TESTS passed=1 skipped=1 failed=1\n"


def test_one_bad_report_among_several_prints_no_line(tmp_path: Path) -> None:
    good = tmp_path / "good.xml"
    good.write_text(VITEST, encoding="utf-8")
    bad = tmp_path / "bad.xml"
    bad.write_text("not xml at all", encoding="utf-8")
    missing = tmp_path / "missing.xml"
    for order in ((good, bad), (bad, good), (good, missing)):
        result = run(*map(str, order))
        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr
