"""Durable, resumable work-queue store (DESIGN.md §10).

The store is the single source of truth for a run. It is persisted to a JSON
file after every mutation using an atomic ``os.replace`` so an interrupted run
never leaves a half-written file, and it is guarded by a re-entrant lock so the
generation swarm can claim tasks concurrently without races.

The critical concurrency primitive is :meth:`claim_next_pending`, which
atomically moves one ``pending`` task to ``generating`` and hands it to exactly
one worker.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from loki.errors import StateError
from loki.state.model import Task, TaskState


class StateStore:
    """A persistent map of ``task_id -> Task`` plus run metadata."""

    def __init__(self, path: str | Path, repo: str = "", config_snapshot: dict[str, Any] | None = None) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self._repo = repo
        self._config_snapshot = config_snapshot or {}
        self._started_at = time.time()
        self._updated_at = self._started_at

    # -- construction -----------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "StateStore":
        """Load an existing run for resumption."""
        p = Path(path)
        if not p.exists():
            raise StateError(f"No run state to resume at {p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"Corrupt run state at {p}: {exc}") from exc
        store = cls(p, repo=data.get("repo", ""), config_snapshot=data.get("config_snapshot", {}))
        store._started_at = data.get("started_at", store._started_at)
        store._tasks = {t["id"]: Task.from_dict(t) for t in data.get("tasks", [])}
        return store

    # -- properties -------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def repo(self) -> str:
        return self._repo

    # -- mutations --------------------------------------------------------

    def add_tasks(self, tasks: list[Task]) -> None:
        """Register new tasks. Existing ids are preserved (idempotent resume)."""
        with self._lock:
            for task in tasks:
                self._tasks.setdefault(task.id, task)
            self._persist_locked()

    def update(self, task: Task) -> None:
        """Replace a task's stored state and persist."""
        with self._lock:
            if task.id not in self._tasks:
                raise StateError(f"Unknown task id: {task.id}")
            self._tasks[task.id] = task
            self._persist_locked()

    def claim_next_pending(self) -> Task | None:
        """Atomically move one ``pending`` task to ``generating`` and return it.

        Returns ``None`` when no pending work remains. This is what makes the
        swarm safe: each pending task is handed to exactly one worker.
        """
        with self._lock:
            for task in self._tasks.values():
                if task.state is TaskState.PENDING:
                    task.state = TaskState.GENERATING
                    self._persist_locked()
                    return task
            return None

    def requeue_stale_in_progress(self) -> int:
        """On resume, move ``generating``/``verifying`` tasks back to ``pending``.

        These represent work interrupted mid-flight; re-doing them is safe and
        avoids losing classes. Terminal tasks are untouched. Returns the count
        requeued.
        """
        with self._lock:
            count = 0
            for task in self._tasks.values():
                if task.state in (TaskState.GENERATING, TaskState.VERIFYING):
                    task.state = TaskState.PENDING
                    count += 1
            if count:
                self._persist_locked()
            return count

    # -- queries ----------------------------------------------------------

    def get(self, task_id: str) -> Task:
        with self._lock:
            if task_id not in self._tasks:
                raise StateError(f"Unknown task id: {task_id}")
            return self._tasks[task_id]

    def all_tasks(self) -> list[Task]:
        with self._lock:
            return list(self._tasks.values())

    def tasks_by_state(self, state: TaskState) -> list[Task]:
        with self._lock:
            return [t for t in self._tasks.values() if t.state is state]

    def counts(self) -> dict[str, int]:
        with self._lock:
            result = {s.value: 0 for s in TaskState}
            for task in self._tasks.values():
                result[task.state.value] += 1
            return result

    # -- persistence ------------------------------------------------------

    def _persist_locked(self) -> None:
        self._updated_at = time.time()
        payload = {
            "repo": self._repo,
            "config_snapshot": self._config_snapshot,
            "started_at": self._started_at,
            "updated_at": self._updated_at,
            "tasks": [t.to_dict() for t in self._tasks.values()],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except BaseException:
            # Never leave a stray temp file behind on failure.
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
