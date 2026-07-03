"""Context-pack assembly for one class (DESIGN.md §6).

Packs the target source, collaborator signatures, one style exemplar, the
edge-case checklist, and environment facts, then trims to fit the model's
context window (dropping the exemplar first, as it is the largest optional item).
"""

from __future__ import annotations

from loki.state.model import ContextPack, Task

_CHARS_PER_TOKEN = 4  # rough heuristic for budgeting only


def _estimate_tokens(pack: ContextPack) -> int:
    total = len(pack.target_source)
    total += sum(len(s) for s in pack.collaborator_signatures)
    total += len(pack.exemplar_test or "")
    total += sum(len(str(k)) + len(str(v)) for k, v in pack.env_facts.items())
    total += sum(len(c) for c in pack.edge_checklist)
    return total // _CHARS_PER_TOKEN


def _collaborator_lines(task: Task) -> list[str]:
    lines: list[str] = []
    for collaborator in task.collaborators:
        lines.append(f"// {collaborator.fqcn}")
        lines.extend(f"  {sig}" for sig in collaborator.signatures)
    return lines


def build_context_pack(
    task: Task,
    target_source: str,
    exemplar_test: str | None,
    env_facts: dict | None,
    edge_checklist: list[str] | None,
    max_context_tokens: int,
) -> ContextPack:
    """Build a context pack, trimming the exemplar if the budget is exceeded.

    A safety margin is reserved for the system prompt and the model's own
    response, so the pack targets roughly half the raw context window.
    """
    pack = ContextPack(
        target_source=target_source,
        collaborator_signatures=_collaborator_lines(task),
        exemplar_test=exemplar_test,
        edge_checklist=list(edge_checklist or []),
        env_facts=dict(env_facts or {}),
    )
    budget = max(1, max_context_tokens // 2)
    pack.token_estimate = _estimate_tokens(pack)
    if pack.token_estimate > budget and pack.exemplar_test:
        pack.exemplar_test = None
        pack.token_estimate = _estimate_tokens(pack)
    return pack
