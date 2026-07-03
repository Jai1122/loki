"""Tests for the Gradle build coordinator's parsing and control flow."""

from __future__ import annotations

from pathlib import Path

from loki.proc import CommandResult
from loki.verify.coordinator import (
    GradleCoordinator,
    parse_compile_errors,
    parse_junit_results,
)

JAVAC_OUTPUT = """\
> Task :compileTestJava FAILED
/repo/src/test/java/com/acme/FooTest.java:12: error: cannot find symbol
    Foo foo = new Foo();
        ^
1 error
"""

JUNIT_PASS = """<?xml version="1.0"?>
<testsuite name="com.acme.FooTest" tests="1">
  <testcase classname="com.acme.FooTest" name="works"/>
</testsuite>
"""

JUNIT_FAIL = """<?xml version="1.0"?>
<testsuite name="com.acme.BarTest" tests="1">
  <testcase classname="com.acme.BarTest" name="broken">
    <failure message="expected 1 but was 2">stacktrace line 1
line 2</failure>
  </testcase>
</testsuite>
"""


def test_parse_compile_errors() -> None:
    errors = parse_compile_errors(JAVAC_OUTPUT)
    assert len(errors) == 1
    assert "cannot find symbol" in errors[0]


def test_parse_junit_results(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    (results / "FooTest.xml").write_text(JUNIT_PASS, encoding="utf-8")
    (results / "BarTest.xml").write_text(JUNIT_FAIL, encoding="utf-8")
    passed, failures = parse_junit_results(results)
    assert passed == 1
    assert len(failures) == 1
    assert failures[0].name == "com.acme.BarTest.broken"
    assert "expected 1 but was 2" in failures[0].trace


def test_parse_junit_missing_dir(tmp_path: Path) -> None:
    assert parse_junit_results(tmp_path / "nope") == (0, [])


def test_compile_tests_reports_errors_via_fake_runner(tmp_path: Path) -> None:
    def fake_runner(argv, cwd, timeout) -> CommandResult:
        return CommandResult(1, JAVAC_OUTPUT, "")

    coordinator = GradleCoordinator(tmp_path, runner=fake_runner)
    ok, errors = coordinator.compile_tests()
    assert not ok
    assert any("cannot find symbol" in e for e in errors)


def test_compile_tests_success_via_fake_runner(tmp_path: Path) -> None:
    def fake_runner(argv, cwd, timeout) -> CommandResult:
        return CommandResult(0, "BUILD SUCCESSFUL", "")

    coordinator = GradleCoordinator(tmp_path, runner=fake_runner)
    ok, errors = coordinator.compile_tests()
    assert ok
    assert errors == []


def test_coverage_reads_jacoco_report(tmp_path: Path) -> None:
    report_dir = tmp_path / "build" / "reports" / "jacoco" / "test"
    report_dir.mkdir(parents=True)
    (report_dir / "jacocoTestReport.xml").write_text(
        '<report><class name="com/acme/Foo">'
        '<counter type="BRANCH" missed="1" covered="3"/></class></report>',
        encoding="utf-8",
    )
    coordinator = GradleCoordinator(tmp_path, runner=lambda a, c, t: CommandResult(0, "", ""))
    coverage = coordinator.coverage()
    assert coverage["com.acme.Foo"] == 0.75


def test_detect_gradle_prefers_wrapper(tmp_path: Path) -> None:
    (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    coordinator = GradleCoordinator(tmp_path, runner=lambda a, c, t: CommandResult(0, "", ""))
    assert coordinator._gradle[0].endswith("gradlew")
