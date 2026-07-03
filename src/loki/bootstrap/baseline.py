"""Baseline coverage measurement (DESIGN.md §4.1 step 3).

Runs the module's existing tests with JaCoCo and records per-class branch
coverage — the 30% starting point and the source of truth for "did this class
improve?". If the baseline build fails or produces no report, coverage defaults
to empty (treated as 0.0 per class), which is safe: every class then looks like
it needs work.
"""

from __future__ import annotations

from pathlib import Path

from loki.proc import Runner, run_command
from loki.verify.coordinator import GradleCoordinator


def compute_baseline(
    module_root: Path, runner: Runner = run_command, timeout: float = 1800.0
) -> dict[str, float]:
    """Return ``{fqcn: baseline_branch_coverage}`` for a module."""
    coordinator = GradleCoordinator(module_root, runner=runner, timeout=timeout)
    coordinator.run_tests()
    return coordinator.coverage()
