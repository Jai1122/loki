"""Dependency graph → collaborators to mock (DESIGN.md §4.2 step 2).

A class's collaborators are the project types it receives through constructor
injection (preferred) or injected fields. Third-party/JDK types (``String``,
``List``, ``Clock`` …) are not treated as collaborators to mock; only types that
exist in the scanned repository are, and for those we surface their public method
signatures so the generator knows what to stub.
"""

from __future__ import annotations

from loki.scan.ast import ClassInfo
from loki.state.model import Collaborator


def build_index(classes: list[ClassInfo]) -> dict[str, ClassInfo]:
    """Index classes by simple name for fast collaborator resolution.

    On simple-name collisions the first wins; constructor-injected types are
    resolved by simple name because source rarely uses fully-qualified names.
    """
    index: dict[str, ClassInfo] = {}
    for info in classes:
        index.setdefault(info.name, info)
    return index


def _signatures(info: ClassInfo) -> list[str]:
    return [
        f"{m.return_type} {m.name}({', '.join(m.parameter_types)})"
        for m in info.public_methods
    ]


def collaborators_for(info: ClassInfo, index: dict[str, ClassInfo]) -> list[Collaborator]:
    """Resolve the project-internal collaborators of ``info``."""
    simple_names: list[str] = list(info.constructor_param_types)
    for f in info.fields:
        if f.is_injected:
            simple_names.append(f.type)

    seen: set[str] = set()
    collaborators: list[Collaborator] = []
    for simple in simple_names:
        simple = simple.split("<")[0].strip()
        if simple in seen or simple == info.name:
            continue
        seen.add(simple)
        target = index.get(simple)
        if target is None:
            continue  # not a project type -> not a mock target
        collaborators.append(
            Collaborator(fqcn=target.fqcn, mockable=True, signatures=_signatures(target))
        )
    return collaborators
