"""Per-module Gradle build coordinator (DESIGN.md §4.4).

Verification is serialized per module: one ``compileTestJava``, then ``test``,
then JaCoCo, then (optionally) scoped PIT. The coordinator shells out through an
injectable :data:`loki.proc.Runner` and parses Gradle's structured outputs
(JUnit result XML, JaCoCo XML, PIT XML) rather than scraping console text, so
results are reliable and the parsing is unit-testable.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from loki.proc import Runner, run_command
from loki.state.model import FailedTest, MutationReport
from loki.verify import jacoco, pit

_COMPILE_ERROR_PREFIX = ".java:"


def detect_gradle(module_root: Path) -> list[str]:
    """Prefer the repo's Gradle wrapper; fall back to a system ``gradle``."""
    wrapper = module_root / "gradlew"
    # The wrapper at the repo root also serves submodules; callers pass the repo
    # root as module_root for single-module repos.
    if wrapper.exists():
        return [str(wrapper)]
    return ["gradle"]


def parse_compile_errors(output: str) -> list[str]:
    """Extract javac-style ``File.java:line: error: ...`` lines from output."""
    errors: list[str] = []
    for line in output.splitlines():
        if _COMPILE_ERROR_PREFIX in line and "error:" in line:
            errors.append(line.strip())
    return errors


def parse_junit_results(results_dir: Path) -> tuple[int, list[FailedTest]]:
    """Parse ``build/test-results/test/*.xml`` into (passed_count, failures)."""
    if not results_dir.is_dir():
        return 0, []
    passed = 0
    failures: list[FailedTest] = []
    for xml_file in sorted(results_dir.glob("*.xml")):
        try:
            root = ET.fromstring(xml_file.read_text(encoding="utf-8"))
        except (ET.ParseError, OSError):
            continue
        for case in root.iter("testcase"):
            name = f"{case.get('classname', '')}.{case.get('name', '')}"
            problem = case.find("failure")
            if problem is None:
                problem = case.find("error")
            if problem is None:
                passed += 1
            else:
                message = (problem.get("message") or problem.text or "test failed").strip()
                failures.append(FailedTest(name=name, trace=message.splitlines()[0][:500]))
    return passed, failures


class GradleCoordinator:
    def __init__(self, module_root: Path, runner: Runner = run_command, timeout: float = 1800.0) -> None:
        self.module_root = Path(module_root)
        self._runner = runner
        self._timeout = timeout
        self._gradle = detect_gradle(self.module_root)

    def _run(self, tasks: list[str]) -> tuple[int, str]:
        result = self._runner(self._gradle + tasks, self.module_root, self._timeout)
        return result.returncode, result.stdout + "\n" + result.stderr

    def compile_tests(self) -> tuple[bool, list[str]]:
        code, output = self._run(["compileTestJava", "-q", "--console=plain"])
        if code == 0:
            return True, []
        return False, parse_compile_errors(output) or [output.strip()[:1000]]

    def run_tests(self) -> tuple[int, list[FailedTest]]:
        self._run(["test", "--console=plain"])  # non-zero on failures is expected
        return parse_junit_results(self.module_root / "build" / "test-results" / "test")

    def coverage(self) -> dict[str, float]:
        self._run(["jacocoTestReport", "-q", "--console=plain"])
        report = self.module_root / "build" / "reports" / "jacoco" / "test" / "jacocoTestReport.xml"
        if not report.exists():
            return {}
        return jacoco.parse_branch_coverage(report.read_text(encoding="utf-8"))

    def mutation(self, target_classes: list[str]) -> dict[str, MutationReport]:
        """Run PIT scoped to the given classes and parse the result (soft signal)."""
        if not target_classes:
            return {}
        self._run(["pitest", f"-Dpit.targetClasses={','.join(target_classes)}", "--console=plain"])
        report = self.module_root / "build" / "reports" / "pitest" / "mutations.xml"
        if not report.exists():
            return {}
        return pit.parse_mutations(report.read_text(encoding="utf-8"))
