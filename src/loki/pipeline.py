"""Orchestration that wires the phases together (DESIGN.md §4).

Generation is parallel (the swarm); verification is serialized per module. Two
entry points:

- :func:`run_dry` — generate, auto-fix, write candidates, and apply the static
  meaningful-assertion gates. No Gradle, no PRs. This is the fast inspection path.
- :func:`run_full` — additionally compile/test/measure per module and drive the
  bounded feedback loop, then finalize each class as passed or parked.

Both share :class:`Pipeline`, which owns generation and the deterministic gates.
"""

from __future__ import annotations

import threading
from pathlib import Path

from loki.config import LokiConfig
from loki.errors import LLMError, ParseError
from loki.generate.contextpack import build_context_pack
from loki.generate.generator import generate, generate_coverage_extension, generate_repair
from loki.generate.worker_pool import run_swarm
from loki.bootstrap.baseline import compute_baseline
from loki.bootstrap.exemplars import harvest_exemplars
from loki.bootstrap.gradle_deps import ensure_test_dependencies
from loki.deliver.pr import create_prs
from loki.deliver.report import build_report
from loki.llm.client import LLMClient
from loki.proc import Runner, run_command
from loki.scan.ast import Module, discover_modules
from loki.state.model import FailedTest, Task, TaskState, VerificationResult
from loki.state.store import StateStore
from loki.verify import gates
from loki.verify.autofix import autofix
from loki.verify.coordinator import GradleCoordinator


class Pipeline:
    def __init__(
        self,
        repo_root: str | Path,
        config: LokiConfig,
        client: LLMClient,
        store: StateStore,
        runner: Runner = run_command,
    ) -> None:
        self.root = Path(repo_root)
        self.config = config
        self.client = client
        self.store = store
        self.runner = runner
        self.modules = {m.name: m for m in discover_modules(self.root)}
        self._exemplars: dict[str, str | None] = {}
        self._exemplar_lock = threading.Lock()

    # -- helpers ----------------------------------------------------------

    def _read_source(self, task: Task) -> str:
        return (self.root / task.source_path).read_text(encoding="utf-8")

    def _exemplar_for(self, module_name: str) -> str | None:
        with self._exemplar_lock:
            if module_name not in self._exemplars:
                module = self.modules.get(module_name)
                found = harvest_exemplars(module.test_src, limit=1) if module else []
                self._exemplars[module_name] = found[0] if found else None
            return self._exemplars[module_name]

    def _write_test(self, task: Task, source: str) -> None:
        path = self.root / task.test_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def _is_preexisting_hand_written(self, task: Task) -> bool:
        """True if a hand-written test already occupies the target path.

        Guarantee (DESIGN.md §15): never overwrite an existing test. A fresh task
        (no prior LOKI output) whose target file already exists is treated as
        hand-written and skipped. Tasks LOKI has already touched (``test_source``
        set or ``llm_turns > 0``, e.g. on --resume) may rewrite their own file.
        """
        if task.test_source is not None or task.llm_turns > 0:
            return False
        return (self.root / task.test_path).exists()

    def _park_preexisting(self, task: Task) -> None:
        task.state = TaskState.PARKED
        task.last_error = "existing hand-written test present; not overwritten"
        self.store.update(task)

    def _generate(self, task: Task) -> str:
        """Generate + auto-fix a candidate. Raises ParseError/LLMError on failure."""
        source_text = self._read_source(task)
        pack = build_context_pack(
            task,
            target_source=source_text,
            exemplar_test=self._exemplar_for(task.module),
            env_facts={},
            edge_checklist=[],
            max_context_tokens=self.config.llm.max_context_tokens,
        )
        result = generate(self.client, task, pack)
        fixed, _ = autofix(result.test_source, task.package)
        return fixed

    # -- dry-run handler --------------------------------------------------

    def dry_handler(self, task: Task) -> None:
        if self._is_preexisting_hand_written(task):
            self._park_preexisting(task)
            return
        task.llm_turns += 1
        try:
            source = self._generate(task)
        except (ParseError, LLMError, OSError) as exc:
            task.state = TaskState.PARKED
            task.last_error = f"generation failed: {exc}"
            self.store.update(task)
            return
        self._write_test(task, source)
        task.test_source = source
        violations = gates.analyze(source)
        if violations:
            task.state = TaskState.PARKED
            task.last_error = "gate: " + ", ".join(sorted({v.rule for v in violations}))
        else:
            task.state = TaskState.PASSED
        self.store.update(task)

    # -- full generation handler (candidate ready for verification) -------

    def generate_handler(self, task: Task) -> None:
        """Generate, auto-fix, gate-repair, write, and mark VERIFYING or PARKED."""
        if self._is_preexisting_hand_written(task):
            self._park_preexisting(task)
            return
        try:
            source = self._generate(task)
            task.llm_turns += 1
            # Static-gate repair loop (cheap, no build) within the turn budget.
            violations = gates.analyze(source)
            while violations and task.llm_turns < self.config.verification.max_llm_turns_per_class:
                fake = VerificationResult(compiled=True, gate_violations=violations)
                repaired = generate_repair(self.client, task, source, fake)
                source, _ = autofix(repaired.test_source, task.package)
                task.llm_turns += 1
                violations = gates.analyze(source)
        except (ParseError, LLMError, OSError) as exc:
            task.state = TaskState.PARKED
            task.last_error = f"generation failed: {exc}"
            self.store.update(task)
            return

        self._write_test(task, source)
        task.test_source = source
        if violations:
            task.state = TaskState.PARKED
            task.last_error = "gate: " + ", ".join(sorted({v.rule for v in violations}))
        else:
            task.state = TaskState.VERIFYING
        self.store.update(task)


def run_dry(pipeline: Pipeline) -> None:
    """Generate + gate all pending tasks; no build, no PRs (DESIGN.md README)."""
    run_swarm(
        pipeline.store,
        pipeline.dry_handler,
        pool_size=pipeline.config.concurrency.worker_pool_size,
        rps=pipeline.config.concurrency.requests_per_second,
    )


def run_full(pipeline: Pipeline) -> None:
    """Full run: parallel generation, then serialized per-module verification."""
    run_swarm(
        pipeline.store,
        pipeline.generate_handler,
        pool_size=pipeline.config.concurrency.worker_pool_size,
        rps=pipeline.config.concurrency.requests_per_second,
    )
    for module_name in sorted(pipeline.modules):
        _verify_module(pipeline, module_name)


def _verify_module(pipeline: Pipeline, module_name: str) -> None:
    """Compile/test/measure a module's candidates, then finalize each class."""
    module = pipeline.modules[module_name]
    candidates = [
        t
        for t in pipeline.store.tasks_by_state(TaskState.VERIFYING)
        if t.module == module_name
    ]
    if not candidates:
        return
    coordinator = GradleCoordinator(module.root, runner=pipeline.runner)

    compiled, compile_errors = coordinator.compile_tests()
    if not compiled:
        _repair_batch(pipeline, coordinator, candidates, compile_errors)

    _passed_count, failures = coordinator.run_tests()
    coverage = coordinator.coverage()

    for task in pipeline.store.tasks_by_state(TaskState.VERIFYING):
        if task.module != module_name:
            continue
        task.current_branch_cov = coverage.get(task.fqcn, task.baseline_branch_cov)
        if _has_failure(task, failures):
            task.state = TaskState.PARKED
            task.last_error = "tests failing after repair budget exhausted"
        else:
            task.state = TaskState.PASSED
        pipeline.store.update(task)

    _run_mutation(pipeline, coordinator)


def _has_failure(task: Task, failures: list[FailedTest]) -> bool:
    return any(task.test_class_name in f.name for f in failures)


def _repair_batch(pipeline: Pipeline, coordinator, candidates, compile_errors) -> None:
    result = VerificationResult(compiled=False, compile_errors=compile_errors)
    for task in candidates:
        if task.llm_turns >= pipeline.config.verification.max_llm_turns_per_class:
            continue
        try:
            repaired = generate_repair(pipeline.client, task, task.test_source or "", result)
            fixed, _ = autofix(repaired.test_source, task.package)
            pipeline._write_test(task, fixed)
            task.test_source = fixed
            task.llm_turns += 1
            pipeline.store.update(task)
        except (ParseError, LLMError):
            continue
    coordinator.compile_tests()


def _run_mutation(pipeline: Pipeline, coordinator) -> None:
    if not pipeline.config.quality.pit_enabled:
        return
    passed = [t for t in pipeline.store.tasks_by_state(TaskState.PASSED) if t.mutation_score is None]
    if not passed:
        return
    reports = coordinator.mutation([t.fqcn for t in passed])
    for task in passed:
        report = reports.get(task.fqcn)
        if report is not None:
            task.mutation_score = round(report.score, 4)
            task.surviving_mutants = report.surviving_details
            pipeline.store.update(task)

    if pipeline.config.quality.chase_mutants:
        _chase_mutants(pipeline, coordinator)


def _chase_mutants(pipeline: "Pipeline", coordinator) -> None:
    """Optional pass: spend remaining turns extending tests to kill survivors.

    Off by default (DESIGN.md §7). Each extension is verified; if it breaks the
    build or tests, the prior green test is restored so quality never regresses.
    """
    budget = pipeline.config.verification.max_llm_turns_per_class
    targets = [
        t
        for t in pipeline.store.tasks_by_state(TaskState.PASSED)
        if t.surviving_mutants and t.test_source and t.llm_turns < budget
    ]
    for task in targets:
        prior = task.test_source
        try:
            extended = generate_coverage_extension(
                pipeline.client, task, prior, uncovered_hints=[], missing_categories=task.surviving_mutants
            )
            fixed, _ = autofix(extended.test_source, task.package)
        except (ParseError, LLMError):
            continue
        pipeline._write_test(task, fixed)
        task.llm_turns += 1
        compiled, _ = coordinator.compile_tests()
        _, failures = (coordinator.run_tests() if compiled else (0, [FailedTest("_", "compile")]))
        if compiled and not _has_failure(task, failures):
            report = coordinator.mutation([task.fqcn]).get(task.fqcn)
            if report is not None:
                task.mutation_score = round(report.score, 4)
                task.surviving_mutants = report.surviving_details
            task.test_source = fixed
        else:
            pipeline._write_test(task, prior)  # restore the last green version
        pipeline.store.update(task)


# --- Phase 0 bootstrap orchestration (DESIGN.md §4.1) --------------------

def ensure_dependencies(repo_root: str | Path, runner: Runner = run_command) -> list[str]:
    """Inject test + measurement deps into each module's ``build.gradle`` (once).

    The sole owner of build-file edits (DESIGN.md §3). Only Groovy ``build.gradle``
    is modified; a Kotlin ``build.gradle.kts`` is left untouched to avoid emitting
    invalid syntax. Returns the names of modules whose build file changed.
    """
    changed: list[str] = []
    for module in discover_modules(repo_root):
        build_file = module.root / "build.gradle"
        if not build_file.exists():
            continue
        text = build_file.read_text(encoding="utf-8")
        updated, was_changed = ensure_test_dependencies(text)
        if was_changed:
            build_file.write_text(updated, encoding="utf-8")
            changed.append(module.name)
    return changed


def make_baseline_provider(runner: Runner = run_command):
    """A baseline provider that runs each module's tests + JaCoCo (DESIGN.md §4.1)."""

    def provider(module: Module) -> dict[str, float]:
        return compute_baseline(module.root, runner=runner)

    return provider


# --- Phase 5 delivery orchestration (DESIGN.md §4.6) ---------------------

def deliver(pipeline: "Pipeline", open_prs: bool) -> Path:
    """Write the run report and (optionally) open chunked PRs. Returns report path."""
    report = build_report(pipeline.store)
    report_path = pipeline.root / ".loki" / "report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    if open_prs:
        intro = f"Characterization tests generated by LOKI ({pipeline.config.delivery.label})."
        create_prs(
            pipeline.root,
            pipeline.store,
            pipeline.config.delivery.pr_chunking,
            pipeline.config.delivery.label,
            intro,
            pipeline.runner,
        )
    return report_path
