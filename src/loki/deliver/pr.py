"""Chunked pull-request delivery (DESIGN.md §4.6).

Passed tests are grouped per module (or per package) so each PR is reviewable,
committed on a LOKI branch, and opened with ``gh`` when available. Only
``passed`` classes are delivered; ``parked`` classes are reported, not committed.
Git/GitHub calls go through the injectable runner so grouping stays unit-testable.
"""

from __future__ import annotations

from pathlib import Path

from loki.proc import Runner, run_command
from loki.state.model import Task, TaskState
from loki.state.store import StateStore


def group_tasks(tasks: list[Task], chunking: str) -> dict[str, list[Task]]:
    """Group passed tasks into PR-sized chunks by module or package."""
    groups: dict[str, list[Task]] = {}
    for task in tasks:
        if task.state is not TaskState.PASSED:
            continue
        key = task.module if chunking == "per-module" else task.package
        groups.setdefault(key or "root", []).append(task)
    return groups


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in ".-_" else "-" for c in name).strip("-") or "group"


def create_prs(
    repo_root: Path,
    store: StateStore,
    chunking: str,
    label: str,
    body_intro: str,
    runner: Runner = run_command,
) -> list[str]:
    """Create one branch+PR per group. Returns the branch names created.

    Best-effort: if ``gh`` is unavailable the branch and commit are still made so
    a human can push/open the PR manually.
    """
    root = Path(repo_root)
    groups = group_tasks(store.all_tasks(), chunking)
    branches: list[str] = []
    for name, tasks in sorted(groups.items()):
        branch = f"loki/tests-{_sanitize(name)}"
        paths = [task.test_path for task in tasks]
        runner(["git", "checkout", "-B", branch], root, 120.0)
        runner(["git", "add", *paths], root, 120.0)
        message = f"test: add characterization tests for {name} ({len(tasks)} classes)"
        runner(["git", "commit", "-m", message], root, 120.0)
        body = body_intro + "\n\n" + "\n".join(f"- {t.fqcn}" for t in tasks)
        runner(
            ["gh", "pr", "create", "--fill", "--label", label, "--title", message, "--body", body],
            root,
            120.0,
        )
        branches.append(branch)
    return branches
