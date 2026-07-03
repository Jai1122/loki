"""Tests for the data models and the durable, resumable work-queue store."""

from __future__ import annotations

import json
import threading

import pytest

from loki.errors import StateError
from loki.state.model import Collaborator, Task, TaskState
from loki.state.store import StateStore


def make_task(task_id: str = "t1", fqcn: str = "com.acme.svc.UserService") -> Task:
    return Task(
        id=task_id,
        fqcn=fqcn,
        module="app",
        source_path=f"src/main/java/{fqcn.replace('.', '/')}.java",
        test_path=f"src/test/java/{fqcn.replace('.', '/')}Test.java",
        collaborators=[Collaborator("com.acme.repo.UserRepository", True, ["User findById(Long)"])],
    )


def test_task_name_helpers() -> None:
    task = make_task()
    assert task.simple_name == "UserService"
    assert task.package == "com.acme.svc"
    assert task.test_class_name == "UserServiceTest"
    assert task.test_fqcn == "com.acme.svc.UserServiceTest"


def test_task_round_trips_through_dict() -> None:
    task = make_task()
    task.state = TaskState.VERIFYING
    task.llm_turns = 2
    task.mutation_score = 0.75
    restored = Task.from_dict(task.to_dict())
    assert restored == task


def test_task_state_terminal_flags() -> None:
    assert TaskState.PASSED.is_terminal
    assert TaskState.PARKED.is_terminal
    assert not TaskState.PENDING.is_terminal
    assert not TaskState.VERIFYING.is_terminal


def test_store_persists_and_reloads(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path, repo="/repo", config_snapshot={"k": 1})
    store.add_tasks([make_task("a"), make_task("b", "com.acme.svc.OrderService")])

    reloaded = StateStore.load(path)
    assert {t.id for t in reloaded.all_tasks()} == {"a", "b"}
    assert reloaded.repo == "/repo"


def test_add_tasks_is_idempotent_on_resume(tmp_path) -> None:
    # Re-adding an id must not clobber progress already recorded for it.
    store = StateStore(tmp_path / "state.json")
    original = make_task("a")
    original.state = TaskState.PASSED
    store._tasks["a"] = original  # seed as if loaded
    store.add_tasks([make_task("a")])  # same id, fresh PENDING
    assert store.get("a").state is TaskState.PASSED


def test_claim_next_pending_is_atomic_and_single_assignment(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    store._tasks = {f"t{i}": make_task(f"t{i}") for i in range(200)}

    claimed: list[str] = []
    claimed_lock = threading.Lock()

    def worker() -> None:
        while True:
            task = store.claim_next_pending()
            if task is None:
                return
            with claimed_lock:
                claimed.append(task.id)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every task claimed exactly once, none left pending.
    assert sorted(claimed) == sorted(f"t{i}" for i in range(200))
    assert len(claimed) == len(set(claimed))
    assert store.tasks_by_state(TaskState.PENDING) == []


def test_requeue_stale_in_progress_only_touches_non_terminal(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    a, b, c, d = make_task("a"), make_task("b"), make_task("c"), make_task("d")
    a.state = TaskState.GENERATING
    b.state = TaskState.VERIFYING
    c.state = TaskState.PASSED
    d.state = TaskState.PARKED
    store._tasks = {t.id: t for t in (a, b, c, d)}

    requeued = store.requeue_stale_in_progress()
    assert requeued == 2
    assert store.get("a").state is TaskState.PENDING
    assert store.get("b").state is TaskState.PENDING
    assert store.get("c").state is TaskState.PASSED
    assert store.get("d").state is TaskState.PARKED


def test_counts_reports_every_state(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    store._tasks = {"a": make_task("a")}
    counts = store.counts()
    assert counts["pending"] == 1
    assert set(counts) == {s.value for s in TaskState}


def test_update_unknown_task_raises(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    with pytest.raises(StateError):
        store.update(make_task("ghost"))


def test_load_missing_or_corrupt_raises(tmp_path) -> None:
    with pytest.raises(StateError):
        StateStore.load(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateError):
        StateStore.load(bad)


def test_persist_writes_valid_json(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.add_tasks([make_task("a")])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tasks"][0]["id"] == "a"
