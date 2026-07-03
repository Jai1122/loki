"""End-to-end dry-run: plan → generate → auto-fix → gate → write, no Gradle."""

from __future__ import annotations

from pathlib import Path

from loki.pipeline import Pipeline, run_dry
from loki.planner import build_plan
from loki.state.model import TaskState

from conftest import GOOD_RESPONSE, TAUTOLOGY_RESPONSE, SingleResponseClient


def test_build_plan_finds_service_and_skips_nothing(repo: Path, config) -> None:
    store = build_plan(repo, config, repo / ".loki" / "state.json")
    tasks = store.all_tasks()
    assert [t.fqcn for t in tasks] == ["com.acme.CalculatorService"]
    assert tasks[0].test_path == "app/src/test/java/com/acme/CalculatorServiceTest.java"
    assert tasks[0].strategy_hints  # branch present -> hints populated


def test_dry_run_writes_passing_test(repo: Path, config) -> None:
    store = build_plan(repo, config, repo / ".loki" / "state.json")
    client = SingleResponseClient(GOOD_RESPONSE)
    pipeline = Pipeline(repo, config, client, store)

    run_dry(pipeline)

    task = store.all_tasks()[0]
    assert task.state is TaskState.PASSED
    written = repo / task.test_path
    assert written.exists()
    assert "CalculatorServiceTest" in written.read_text(encoding="utf-8")


def test_dry_run_parks_meaningless_test(repo: Path, config) -> None:
    store = build_plan(repo, config, repo / ".loki" / "state.json")
    client = SingleResponseClient(TAUTOLOGY_RESPONSE)
    pipeline = Pipeline(repo, config, client, store)

    run_dry(pipeline)

    task = store.all_tasks()[0]
    assert task.state is TaskState.PARKED
    assert "tautology" in (task.last_error or "")


def test_resume_requeues_and_completes(repo: Path, config) -> None:
    state_path = repo / ".loki" / "state.json"
    store = build_plan(repo, config, state_path)
    # Simulate an interruption: mark the task as mid-generation.
    task = store.all_tasks()[0]
    task.state = TaskState.GENERATING
    store.update(task)

    from loki.state.store import StateStore

    resumed = StateStore.load(state_path)
    assert resumed.requeue_stale_in_progress() == 1

    pipeline = Pipeline(repo, config, SingleResponseClient(GOOD_RESPONSE), resumed)
    run_dry(pipeline)
    assert resumed.all_tasks()[0].state is TaskState.PASSED
