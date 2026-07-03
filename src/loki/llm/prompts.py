"""Prompt templates and the required output contract (DESIGN.md §8, §9, §14).

The system prompt fixes the model's role and the hard rules (edge-case taxonomy
and meaningful-assertion policy). The user prompt is assembled per class from the
context pack. The repair and coverage prompts drive the bounded feedback loop.
"""

from __future__ import annotations

from loki.state.model import ContextPack, Task, VerificationResult

# The edge-case taxonomy (DESIGN.md §8), rendered for the prompt.
EDGE_CASE_CHECKLIST = """\
- Happy path: typical valid inputs, assert the exact expected output.
- Null / empty: null arguments, empty collections/strings, Optional.empty().
- Boundary values: 0, ±1, MIN/MAX, off-by-one, empty vs single vs many elements.
- Invalid inputs / validation: constraint violations -> expected exception/error.
- Every branch: each if/else, switch case (incl. default), ternary, try/catch.
- Loops: 0, 1, and N iterations; early break/continue.
- Collaborator returns: normal value, empty/Optional.empty(), null where reachable.
- Collaborator throws: each declared exception -> verify propagation/handling.
- Exception paths: assert exception TYPE and message/state where meaningful.
- Interactions: verify(...) with argument matchers that encode intent (not any()).
- Spring (if applicable): status codes, JSON body (JSONAssert), validation errors,
  exception->HTTP mapping, authorized vs unauthorized."""

# The determinism guardrails (DESIGN.md §8) and assertion policy (DESIGN.md §9).
SYSTEM_PROMPT = """\
You are a senior Java/Spring test engineer. You write JUnit 5 unit tests using \
Mockito and AssertJ for a Spring Boot codebase.

Absolute rules:
1. Output exactly ONE compilable test class. No prose outside the required format.
2. Every @Test method MUST contain at least one MEANINGFUL assertion on a concrete
   outcome: a returned value, a thrown exception (type + message/state), changed
   state, or a behaviour-relevant interaction with intent-encoding matchers.
3. NEVER write meaningless tests: no assertTrue(true)/assertFalse(false), no
   tautologies, no test whose only assertion is assertNotNull/isNotNull, no bare
   assertDoesNotThrow, no empty or fail()-only bodies, no @Disabled.
4. Cover the edge and corner cases that apply to each method (see checklist).
5. Tests MUST be deterministic: no real clock/random/network/filesystem/DB, no
   Thread.sleep, no reliance on HashMap/HashSet ordering. Mock collaborators.
6. Prefer AssertJ (assertThat) for assertions and JSONAssert for JSON payloads.
7. These are characterization tests: assert the code's CURRENT behaviour.

Output format, exactly:
PLAN:
- <one bullet per scenario, naming the edge category it covers>

```java
<the complete test class>
```"""


def _collaborator_block(pack: ContextPack) -> str:
    if not pack.collaborator_signatures:
        return "(none — this class has no project collaborators to mock)"
    return "\n".join(pack.collaborator_signatures)


def _env_block(pack: ContextPack) -> str:
    if not pack.env_facts:
        return "(no special environment facts)"
    return "\n".join(f"- {k}: {v}" for k, v in sorted(pack.env_facts.items()))


def build_user_prompt(task: Task, pack: ContextPack) -> str:
    """Assemble the per-class generation prompt (DESIGN.md §6, §14)."""
    exemplar = pack.exemplar_test or "(no in-repo exemplar available)"
    checklist = "\n".join(pack.edge_checklist) if pack.edge_checklist else EDGE_CASE_CHECKLIST
    hints = "\n".join(f"- {h}" for h in task.strategy_hints) or "- (none)"
    return f"""\
Generate a JUnit 5 test class named {task.test_class_name} in package {task.package}
for the class under test below.

=== CLASS UNDER TEST ({task.fqcn}) ===
{pack.target_source}

=== COLLABORATOR SIGNATURES TO MOCK ===
{_collaborator_block(pack)}

=== STYLE EXEMPLAR FROM THIS REPO (match these idioms) ===
{exemplar}

=== EDGE-CASE CHECKLIST (cover all that apply) ===
{checklist}

=== STRATEGY HINTS ===
{hints}

=== ENVIRONMENT ===
{_env_block(pack)}

Produce the PLAN then the single test class, following the required output format."""


def build_repair_prompt(task: Task, test_source: str, result: VerificationResult) -> str:
    """Prompt to fix a candidate that failed compilation or tests (DESIGN.md §4.5)."""
    if not result.compiled:
        problem = "The test class does not COMPILE. Compiler errors:\n" + "\n".join(
            result.compile_errors
        )
    else:
        failures = "\n".join(f"- {f.name}: {f.trace}" for f in result.failed_tests)
        problem = (
            "The test class compiles but has FAILING tests. Because these are "
            "characterization tests, fix the assertions to match the code's actual "
            "observed behaviour (do not change the class under test). Failures:\n" + failures
        )
    return f"""\
Fix the following JUnit 5 test class. {problem}

=== CURRENT TEST CLASS ===
{test_source}

Return the corrected complete test class in the required output format. Preserve
all scenarios that already pass and keep every assertion meaningful."""


def build_coverage_prompt(
    task: Task, test_source: str, uncovered_hints: list[str], missing_categories: list[str]
) -> str:
    """Prompt to extend a passing candidate toward uncovered branches (DESIGN.md §4.5)."""
    uncovered = "\n".join(f"- {u}" for u in uncovered_hints) or "- (branch details unavailable)"
    categories = "\n".join(f"- {c}" for c in missing_categories) or "- (none identified)"
    return f"""\
The test class below passes but does not yet reach the target branch coverage for
{task.fqcn}. Add focused, meaningful tests for the uncovered branches and the
missing edge-case categories. Do not remove existing tests.

=== UNCOVERED / UNDER-TESTED ===
{uncovered}

=== MISSING EDGE-CASE CATEGORIES ===
{categories}

=== CURRENT TEST CLASS ===
{test_source}

Return the complete extended test class in the required output format."""
