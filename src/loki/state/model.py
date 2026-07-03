"""Core data models (DESIGN.md §13).

Every model is a plain dataclass with explicit ``to_dict`` / ``from_dict`` so the
run state can be persisted as JSON and resumed verbatim. Serialization is kept
explicit (rather than ``dataclasses.asdict``) so nested objects and the
``TaskState`` enum round-trip deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskState(str, Enum):
    """Lifecycle of a single target class (DESIGN.md §10).

    ``pending -> generating -> verifying -> passed | failed | parked``.
    ``passed`` and ``parked`` are terminal.
    """

    PENDING = "pending"
    GENERATING = "generating"
    VERIFYING = "verifying"
    PASSED = "passed"
    FAILED = "failed"
    PARKED = "parked"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskState.PASSED, TaskState.PARKED)


@dataclass
class Collaborator:
    """A dependency of the class under test that should be mocked."""

    fqcn: str
    mockable: bool = True
    signatures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"fqcn": self.fqcn, "mockable": self.mockable, "signatures": list(self.signatures)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Collaborator":
        return cls(
            fqcn=data["fqcn"],
            mockable=bool(data.get("mockable", True)),
            signatures=list(data.get("signatures", [])),
        )


@dataclass
class Task:
    """One target class to generate tests for (DESIGN.md §13)."""

    id: str
    fqcn: str
    module: str
    source_path: str
    test_path: str
    collaborators: list[Collaborator] = field(default_factory=list)
    baseline_branch_cov: float = 0.0
    current_branch_cov: float = 0.0
    strategy_hints: list[str] = field(default_factory=list)
    edge_categories: list[str] = field(default_factory=list)
    state: TaskState = TaskState.PENDING
    llm_turns: int = 0
    last_error: str | None = None
    mutation_score: float | None = None
    surviving_mutants: list[str] = field(default_factory=list)
    test_source: str | None = None

    @property
    def simple_name(self) -> str:
        return self.fqcn.rsplit(".", 1)[-1]

    @property
    def package(self) -> str:
        return self.fqcn.rsplit(".", 1)[0] if "." in self.fqcn else ""

    @property
    def test_class_name(self) -> str:
        return f"{self.simple_name}Test"

    @property
    def test_fqcn(self) -> str:
        return f"{self.package}.{self.test_class_name}" if self.package else self.test_class_name

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "fqcn": self.fqcn,
            "module": self.module,
            "source_path": self.source_path,
            "test_path": self.test_path,
            "collaborators": [c.to_dict() for c in self.collaborators],
            "baseline_branch_cov": self.baseline_branch_cov,
            "current_branch_cov": self.current_branch_cov,
            "strategy_hints": list(self.strategy_hints),
            "edge_categories": list(self.edge_categories),
            "state": self.state.value,
            "llm_turns": self.llm_turns,
            "last_error": self.last_error,
            "mutation_score": self.mutation_score,
            "surviving_mutants": list(self.surviving_mutants),
            "test_source": self.test_source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=data["id"],
            fqcn=data["fqcn"],
            module=data["module"],
            source_path=data["source_path"],
            test_path=data["test_path"],
            collaborators=[Collaborator.from_dict(c) for c in data.get("collaborators", [])],
            baseline_branch_cov=float(data.get("baseline_branch_cov", 0.0)),
            current_branch_cov=float(data.get("current_branch_cov", 0.0)),
            strategy_hints=list(data.get("strategy_hints", [])),
            edge_categories=list(data.get("edge_categories", [])),
            state=TaskState(data.get("state", TaskState.PENDING.value)),
            llm_turns=int(data.get("llm_turns", 0)),
            last_error=data.get("last_error"),
            mutation_score=data.get("mutation_score"),
            surviving_mutants=list(data.get("surviving_mutants", [])),
            test_source=data.get("test_source"),
        )


@dataclass
class ContextPack:
    """The bundle of context handed to the model for one class (DESIGN.md §6)."""

    target_source: str
    collaborator_signatures: list[str] = field(default_factory=list)
    exemplar_test: str | None = None
    edge_checklist: list[str] = field(default_factory=list)
    env_facts: dict[str, Any] = field(default_factory=dict)
    token_estimate: int = 0


@dataclass
class GenerationResult:
    """Parsed output of a single generation/repair call (DESIGN.md §14)."""

    plan: list[str]
    test_source: str
    raw_response: str


@dataclass
class GateViolation:
    """A single static quality-gate failure (DESIGN.md §9)."""

    rule: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "detail": self.detail}


@dataclass
class FailedTest:
    """A failing test method and its stack trace."""

    name: str
    trace: str


@dataclass
class VerificationResult:
    """Outcome of compiling + running + measuring a candidate (DESIGN.md §13)."""

    compiled: bool = False
    compile_errors: list[str] = field(default_factory=list)
    passed_tests: int = 0
    failed_tests: list[FailedTest] = field(default_factory=list)
    branch_cov_delta: float = 0.0
    new_branch_cov: float = 0.0
    gate_violations: list[GateViolation] = field(default_factory=list)

    @property
    def is_green(self) -> bool:
        """A candidate is green when it compiles, has no failing tests, and no
        static gate violations. Coverage improvement is evaluated separately."""
        return self.compiled and not self.failed_tests and not self.gate_violations


@dataclass
class MutationReport:
    """PIT mutation-testing result for one class (soft signal, DESIGN.md §7)."""

    class_fqcn: str
    killed: int = 0
    survived: int = 0
    surviving_details: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.killed + self.survived

    @property
    def score(self) -> float:
        return self.killed / self.total if self.total else 0.0
