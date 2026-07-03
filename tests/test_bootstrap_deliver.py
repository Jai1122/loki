"""Tests for bootstrap (deps, exemplars) and delivery (grouping, report)."""

from __future__ import annotations

from pathlib import Path

from loki.bootstrap.exemplars import harvest_exemplars
from loki.bootstrap.gradle_deps import ensure_test_dependencies
from loki.deliver.pr import group_tasks
from loki.deliver.report import build_report
from loki.state.model import Task, TaskState
from loki.state.store import StateStore

BASE_GRADLE = """\
plugins {
    id 'java'
    id 'org.springframework.boot' version '3.2.0'
}

dependencies {
    implementation 'org.springframework.boot:spring-boot-starter-web'
}
"""


def test_ensure_dependencies_adds_missing() -> None:
    updated, changed = ensure_test_dependencies(BASE_GRADLE)
    assert changed
    assert "org.assertj:assertj-core" in updated
    assert "org.junit-pioneer:junit-pioneer:2.2.0" in updated
    assert "com.tngtech.archunit:archunit-junit5:1.3.0" in updated
    assert "apply plugin: 'jacoco'" in updated
    assert "info.solidsoft.pitest" in updated


def test_ensure_dependencies_is_idempotent() -> None:
    once, _ = ensure_test_dependencies(BASE_GRADLE)
    twice, changed = ensure_test_dependencies(once)
    assert not changed
    assert once == twice


def test_ensure_dependencies_without_block() -> None:
    updated, changed = ensure_test_dependencies("plugins { id 'java' }\n")
    assert changed
    assert "dependencies {" in updated
    assert "spring-boot-starter-test" in updated


def test_harvest_exemplars_prefers_richer_test(tmp_path: Path) -> None:
    (tmp_path / "WeakTest.java").write_text(
        "class WeakTest { @Test void t() { assertThat(1).isEqualTo(1); } }", encoding="utf-8"
    )
    (tmp_path / "RichTest.java").write_text(
        "class RichTest {\n"
        "  @Test void a() { assertThat(x).isEqualTo(1); verify(m).call(); }\n"
        "  @Test void b() { assertThrows(RuntimeException.class, () -> f()); }\n"
        "}",
        encoding="utf-8",
    )
    exemplars = harvest_exemplars(tmp_path, limit=1)
    assert len(exemplars) == 1
    assert "RichTest" in exemplars[0]


def test_harvest_exemplars_empty_dir(tmp_path: Path) -> None:
    assert harvest_exemplars(tmp_path / "missing") == []


def _task(fqcn: str, module: str, state: TaskState) -> Task:
    package = fqcn.rsplit(".", 1)[0]
    return Task(
        id=fqcn, fqcn=fqcn, module=module,
        source_path="s.java", test_path=f"{package.replace('.', '/')}/X.java",
        state=state,
    )


def test_group_tasks_per_module_and_package() -> None:
    tasks = [
        _task("com.a.Foo", "app", TaskState.PASSED),
        _task("com.b.Bar", "app", TaskState.PASSED),
        _task("com.a.Baz", "core", TaskState.PASSED),
        _task("com.a.Skip", "app", TaskState.PARKED),  # not passed -> excluded
    ]
    by_module = group_tasks(tasks, "per-module")
    assert set(by_module) == {"app", "core"}
    assert len(by_module["app"]) == 2

    by_package = group_tasks(tasks, "per-package")
    assert set(by_package) == {"com.a", "com.b"}


def test_build_report_contains_signal(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    passed = _task("com.a.Foo", "app", TaskState.PASSED)
    passed.baseline_branch_cov = 0.30
    passed.current_branch_cov = 0.90
    passed.mutation_score = 0.8
    parked = _task("com.a.Bar", "app", TaskState.PARKED)
    parked.last_error = "gate: tautology"
    store._tasks = {passed.id: passed, parked.id: parked}

    report = build_report(store)
    assert "# LOKI run report" in report
    assert "com.a.Foo" in report
    assert "+60" in report  # coverage delta in points
    assert "characterization tests" in report
    assert "com.a.Bar" in report and "tautology" in report
