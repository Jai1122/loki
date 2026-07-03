"""Edge-case strategy heuristics (DESIGN.md §8).

At planning time these produce the per-class strategy hints that steer the
generator toward the applicable edge categories. During the feedback loop they
describe which categories still look under-covered so re-prompts stay targeted.
"""

from __future__ import annotations

from loki.scan.ast import ClassInfo

_BASE_CATEGORIES = [
    "Null/empty inputs (null args, empty collections, Optional.empty()).",
    "Boundary values (0, ±1, MIN/MAX, empty vs single vs many).",
    "Collaborator returns empty/Optional.empty()/null where reachable.",
    "Collaborator throws -> verify propagation or handling.",
]


def strategy_hints(info: ClassInfo) -> list[str]:
    """Applicable edge-case guidance for one class under test."""
    hints: list[str] = []
    if info.is_controller:
        hints.append(
            "Controller: assert HTTP status, JSON body (JSONAssert), validation "
            "errors, exception->HTTP mapping, authorized vs unauthorized."
        )
    if info.complexity > 0:
        hints.append("Cover every branch (if/else, switch incl. default, ternary, try/catch).")
    if any(m.throws for m in info.methods):
        hints.append("Assert each declared exception's type and message/state.")
    hints.extend(_BASE_CATEGORIES)
    return hints


def missing_categories(info: ClassInfo, existing_test_source: str) -> list[str]:
    """Best-effort list of edge categories not yet evident in a passing test.

    Uses cheap textual signals (a re-prompt aid, not a correctness gate).
    """
    text = existing_test_source.lower()
    missing: list[str] = []
    if "null" not in text:
        missing.append("Null-input handling.")
    if not any(tok in text for tok in ("assertthrows", "assertthatthrownby", "expects")):
        if any(m.throws for m in info.methods):
            missing.append("Exception-path coverage for declared throws.")
    if info.is_controller and "status" not in text:
        missing.append("HTTP status assertions for the controller.")
    return missing
