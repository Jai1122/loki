"""Human-facing run report (DESIGN.md §4.6).

Reviewers should read *signal*, not hundreds of test files: coverage delta,
mutation score (soft), pass rate, and the parked classes that need attention.
"""

from __future__ import annotations

from loki.state.model import TaskState
from loki.state.store import StateStore


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def build_report(store: StateStore) -> str:
    """Render a Markdown summary of the run."""
    tasks = store.all_tasks()
    passed = [t for t in tasks if t.state is TaskState.PASSED]
    parked = [t for t in tasks if t.state is TaskState.PARKED]
    counts = store.counts()

    total_terminal = len(passed) + len(parked)
    pass_rate = (len(passed) / total_terminal * 100) if total_terminal else 0.0
    deltas = [t.current_branch_cov - t.baseline_branch_cov for t in passed]
    mutation_scores = [t.mutation_score for t in passed if t.mutation_score is not None]

    lines = [
        "# LOKI run report",
        "",
        f"- Classes targeted: {len(tasks)}",
        f"- Passed (committable): {len(passed)}",
        f"- Parked (needs human attention): {len(parked)}",
        f"- Pass rate: {pass_rate:.0f}%",
        f"- Mean branch-coverage delta (passed): +{_mean(deltas) * 100:.1f} pts",
    ]
    if mutation_scores:
        lines.append(f"- Mean mutation score (soft signal): {_mean(mutation_scores) * 100:.0f}%")
    lines.append(f"- State breakdown: {counts}")
    lines.append("")

    if passed:
        lines.append("## Passed classes")
        lines.append("")
        lines.append("| Class | Baseline | New | Δ | Mutation |")
        lines.append("|---|---|---|---|---|")
        for task in sorted(passed, key=lambda t: t.fqcn):
            mutation = "—" if task.mutation_score is None else f"{task.mutation_score * 100:.0f}%"
            lines.append(
                f"| {task.fqcn} | {task.baseline_branch_cov * 100:.0f}% | "
                f"{task.current_branch_cov * 100:.0f}% | "
                f"+{(task.current_branch_cov - task.baseline_branch_cov) * 100:.0f} | {mutation} |"
            )
        lines.append("")

    if parked:
        lines.append("## Parked classes (not committed)")
        lines.append("")
        for task in sorted(parked, key=lambda t: t.fqcn):
            reason = task.last_error or "did not reach a green, meaningful state"
            lines.append(f"- `{task.fqcn}` — {reason}")
        lines.append("")

    lines.append("> Tests are **characterization tests**: they assert current behaviour.")
    return "\n".join(lines)
