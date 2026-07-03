"""Deterministic repository scanning: AST inventory, dependency graph,
exclusion rules, and prioritization (DESIGN.md §1, §4.2)."""

from loki.scan.ast import ClassInfo, FieldInfo, MethodInfo, discover_modules, parse_java_source
from loki.scan.exclude import is_excluded
from loki.scan.graph import build_index, collaborators_for

# Note: the ``prioritize`` function is intentionally *not* re-exported here so it
# does not shadow the ``loki.scan.prioritize`` submodule. Import it as
# ``from loki.scan.prioritize import prioritize``.

__all__ = [
    "ClassInfo",
    "FieldInfo",
    "MethodInfo",
    "discover_modules",
    "parse_java_source",
    "is_excluded",
    "build_index",
    "collaborators_for",
]
