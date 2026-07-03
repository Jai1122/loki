"""Prioritization by coverage-gap × risk (DESIGN.md §4.2 step 4).

Classes with the most uncovered, most complex business logic are generated
first, so the climb from 30%→90% spends the swarm where it matters. Service and
controller layers are weighted up relative to plain components.
"""

from __future__ import annotations

from dataclasses import dataclass

from loki.scan.ast import ClassInfo

_STEREOTYPE_WEIGHT = {
    "Service": 1.5,
    "RestController": 1.3,
    "Controller": 1.3,
    "RestControllerAdvice": 1.2,
    "ControllerAdvice": 1.2,
    "Component": 1.2,
    "Repository": 1.1,
}


@dataclass
class PrioritizedClass:
    info: ClassInfo
    baseline_coverage: float
    score: float


def priority_score(info: ClassInfo, baseline_coverage: float, target_coverage: float) -> float:
    gap = max(0.0, target_coverage - baseline_coverage)
    risk = 1 + info.complexity + len(info.public_methods)
    weight = _STEREOTYPE_WEIGHT.get(info.stereotype or "", 1.0)
    return round(gap * risk * weight, 6)


def prioritize(
    items: list[tuple[ClassInfo, float]], target_coverage: float
) -> list[PrioritizedClass]:
    """Rank ``(class, baseline_coverage)`` pairs, highest priority first.

    Ties break by higher complexity then class name for a stable, deterministic
    ordering across runs.
    """
    ranked = [
        PrioritizedClass(info, cov, priority_score(info, cov, target_coverage))
        for info, cov in items
    ]
    ranked.sort(key=lambda p: (-p.score, -p.info.complexity, p.info.fqcn))
    return ranked
