"""End-to-end FULL run: generate -> verify (fake Gradle emitting real JUnit/
JaCoCo/PIT XML) -> finalize -> deliver. Proves the whole chain is wired."""

from __future__ import annotations

from pathlib import Path

from loki.pipeline import (
    Pipeline,
    deliver,
    ensure_dependencies,
    run_full,
)
from loki.planner import build_plan
from loki.proc import CommandResult
from loki.state.model import TaskState

from conftest import SingleResponseClient

ORDER_SERVICE = """\
package com.acme;
import org.springframework.stereotype.Service;
@Service
public class OrderService {
    private final PricingClient pricing;
    public OrderService(PricingClient pricing) { this.pricing = pricing; }
    public int total(int qty) {
        if (qty <= 0) { throw new IllegalArgumentException("qty"); }
        return qty * pricing.unitPrice();
    }
}
"""

PRICING_CLIENT = "package com.acme;\npublic interface PricingClient { int unitPrice(); }\n"

ORDER_RESPONSE = """\
PLAN:
- throws on non-positive qty
- multiplies qty by unit price

```java
package com.acme;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class OrderServiceTest {
    @Mock
    PricingClient pricing;

    @Test
    void throwsOnNonPositive() {
        assertThatThrownBy(() -> new OrderService(pricing).total(0))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void multiplies() {
        when(pricing.unitPrice()).thenReturn(10);
        assertThat(new OrderService(pricing).total(3)).isEqualTo(30);
    }
}
```
"""

JUNIT_XML = """<?xml version="1.0"?>
<testsuite name="com.acme.OrderServiceTest" tests="2">
  <testcase classname="com.acme.OrderServiceTest" name="throwsOnNonPositive"/>
  <testcase classname="com.acme.OrderServiceTest" name="multiplies"/>
</testsuite>
"""

JACOCO_XML = (
    '<report><package name="com/acme">'
    '<class name="com/acme/OrderService"><counter type="BRANCH" missed="0" covered="4"/></class>'
    "</package></report>"
)

MUTATIONS_XML = """<?xml version="1.0"?>
<mutations>
  <mutation detected="true" status="KILLED">
    <mutatedClass>com.acme.OrderService</mutatedClass>
    <mutatedMethod>total</mutatedMethod>
    <mutator>ConditionalsBoundaryMutator</mutator>
    <lineNumber>7</lineNumber>
  </mutation>
</mutations>
"""


def _make_repo(tmp_path: Path) -> Path:
    main = tmp_path / "app" / "src" / "main" / "java" / "com" / "acme"
    main.mkdir(parents=True)
    (tmp_path / "app" / "src" / "test" / "java" / "com" / "acme").mkdir(parents=True)
    (main / "OrderService.java").write_text(ORDER_SERVICE, encoding="utf-8")
    (main / "PricingClient.java").write_text(PRICING_CLIENT, encoding="utf-8")
    (tmp_path / "app" / "build.gradle").write_text(
        "plugins { id 'java' }\ndependencies {\n}\n", encoding="utf-8"
    )
    return tmp_path


def _fake_gradle_git_runner(app_root: Path, calls: list):
    """Simulate Gradle by writing the structured outputs the coordinator reads."""

    def runner(argv, cwd, timeout) -> CommandResult:
        calls.append(argv)
        if argv and argv[0] in ("git", "gh"):
            return CommandResult(0, "", "")
        if "compileTestJava" in argv:
            return CommandResult(0, "BUILD SUCCESSFUL", "")
        if "jacocoTestReport" in argv:
            d = app_root / "build" / "reports" / "jacoco" / "test"
            d.mkdir(parents=True, exist_ok=True)
            (d / "jacocoTestReport.xml").write_text(JACOCO_XML, encoding="utf-8")
            return CommandResult(0, "", "")
        if "pitest" in argv:
            d = app_root / "build" / "reports" / "pitest"
            d.mkdir(parents=True, exist_ok=True)
            (d / "mutations.xml").write_text(MUTATIONS_XML, encoding="utf-8")
            return CommandResult(0, "", "")
        if "test" in argv:
            d = app_root / "build" / "test-results" / "test"
            d.mkdir(parents=True, exist_ok=True)
            (d / "OrderServiceTest.xml").write_text(JUNIT_XML, encoding="utf-8")
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    return runner


def test_full_run_generates_verifies_measures_and_delivers(tmp_path: Path, config) -> None:
    repo = _make_repo(tmp_path)
    config.quality.pit_enabled = True
    store = build_plan(repo, config, repo / ".loki" / "state.json")
    calls: list = []
    runner = _fake_gradle_git_runner(repo / "app", calls)
    pipeline = Pipeline(repo, config, SingleResponseClient(ORDER_RESPONSE), store, runner=runner)

    run_full(pipeline)

    task = next(t for t in store.all_tasks() if t.fqcn == "com.acme.OrderService")
    assert task.state is TaskState.PASSED
    assert task.current_branch_cov == 1.0          # from JaCoCo
    assert task.mutation_score == 1.0              # from PIT (soft signal)
    assert (repo / task.test_path).exists()

    # Delivery: report written + branch/commit/PR commands issued.
    report_path = deliver(pipeline, open_prs=True)
    assert report_path.exists()
    assert "OrderService" in report_path.read_text(encoding="utf-8")
    assert any(c[0] == "git" and "commit" in c for c in calls)
    assert any(c[0] == "gh" for c in calls)


def test_full_run_no_pr_writes_report_without_git(tmp_path: Path, config) -> None:
    repo = _make_repo(tmp_path)
    store = build_plan(repo, config, repo / ".loki" / "state.json")
    calls: list = []
    runner = _fake_gradle_git_runner(repo / "app", calls)
    pipeline = Pipeline(repo, config, SingleResponseClient(ORDER_RESPONSE), store, runner=runner)
    run_full(pipeline)

    deliver(pipeline, open_prs=False)
    assert not any(c[0] in ("git", "gh") for c in calls)


def test_ensure_dependencies_injects_and_is_owner(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    changed = ensure_dependencies(repo)
    assert "app" in changed
    text = (repo / "app" / "build.gradle").read_text(encoding="utf-8")
    assert "assertj-core" in text and "jacoco" in text


def test_existing_hand_written_test_is_not_overwritten(tmp_path: Path, config) -> None:
    repo = _make_repo(tmp_path)
    store = build_plan(repo, config, repo / ".loki" / "state.json")
    task = store.all_tasks()[0]
    # Pre-create a hand-written test at the target path.
    existing = repo / task.test_path
    existing.parent.mkdir(parents=True, exist_ok=True)
    original = "// hand-written, do not touch\n"
    existing.write_text(original, encoding="utf-8")

    from loki.pipeline import run_dry

    pipeline = Pipeline(repo, config, SingleResponseClient(ORDER_RESPONSE), store)
    run_dry(pipeline)

    reloaded = store.all_tasks()[0]
    assert reloaded.state is TaskState.PARKED
    assert "not overwritten" in (reloaded.last_error or "")
    assert existing.read_text(encoding="utf-8") == original  # untouched
