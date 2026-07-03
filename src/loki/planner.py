"""Phase 1: scan & plan → the durable work queue (DESIGN.md §4.2).

Deterministic and LLM-free. Walks every module, parses each source file, drops
excluded classes, resolves collaborators, ranks targets by coverage-gap × risk,
and writes one prioritized :class:`Task` per surviving class into the store. The
store's insertion order *is* the priority order, so the swarm claims the
highest-value classes first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from loki.config import LokiConfig
from loki.scan.ast import ClassInfo, Module, discover_modules, parse_java_source
from loki.scan.exclude import is_excluded
from loki.scan.graph import build_index, collaborators_for
from loki.scan.prioritize import prioritize
from loki.state.model import Task
from loki.state.store import StateStore
from loki.verify.edgecheck import strategy_hints

# A baseline provider maps a module to ``{fqcn: baseline_branch_coverage}``.
BaselineProvider = Callable[[Module], dict[str, float]]


def _iter_classes(root: Path, modules: list[Module]) -> list[tuple[Module, ClassInfo]]:
    found: list[tuple[Module, ClassInfo]] = []
    for module in modules:
        if not module.main_src.is_dir():
            continue
        for java_file in sorted(module.main_src.rglob("*.java")):
            try:
                source = java_file.read_text(encoding="utf-8")
            except OSError:
                continue
            rel = java_file.relative_to(root).as_posix()
            for info in parse_java_source(source, rel):
                found.append((module, info))
    return found


def _test_path(root: Path, module: Module, info: ClassInfo) -> str:
    package_dir = info.package.replace(".", "/") if info.package else ""
    test_file = module.test_src / package_dir / f"{info.name}Test.java"
    return test_file.relative_to(root).as_posix()


def build_plan(
    repo_root: str | Path,
    config: LokiConfig,
    state_path: str | Path,
    baseline_provider: BaselineProvider | None = None,
) -> StateStore:
    """Scan the repo and write the prioritized work queue."""
    root = Path(repo_root)
    modules = discover_modules(root)
    classes = _iter_classes(root, modules)
    index = build_index([info for _, info in classes])

    baselines: dict[str, dict[str, float]] = {}
    if baseline_provider is not None:
        for module in modules:
            baselines[module.name] = baseline_provider(module)

    candidates: list[tuple[Module, ClassInfo, float]] = []
    for module, info in classes:
        excluded, _reason = is_excluded(info, info.source_path, config.exclusions)
        if excluded:
            continue
        coverage = baselines.get(module.name, {}).get(info.fqcn, 0.0)
        candidates.append((module, info, coverage))

    ranked = prioritize(
        [(info, cov) for _, info, cov in candidates], config.quality.target_branch_coverage
    )
    module_by_fqcn = {info.fqcn: module for module, info, _ in candidates}
    cov_by_fqcn = {info.fqcn: cov for _, info, cov in candidates}

    store = StateStore(state_path, repo=str(root), config_snapshot=config.snapshot())
    tasks: list[Task] = []
    for prioritized in ranked:
        info = prioritized.info
        module = module_by_fqcn[info.fqcn]
        tasks.append(
            Task(
                id=info.fqcn,
                fqcn=info.fqcn,
                module=module.name,
                source_path=info.source_path,
                test_path=_test_path(root, module, info),
                collaborators=collaborators_for(info, index),
                baseline_branch_cov=cov_by_fqcn[info.fqcn],
                strategy_hints=strategy_hints(info),
            )
        )
    store.add_tasks(tasks)
    return store
