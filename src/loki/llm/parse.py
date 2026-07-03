"""Parse an LLM response into a validated test class (DESIGN.md §14).

The contract is a ``PLAN:`` bullet list followed by exactly one fenced ```java```
block containing exactly one top-level test class. Anything else raises
:class:`ParseError`, which the generator turns into a single reformat retry.
"""

from __future__ import annotations

import re

from loki.errors import ParseError
from loki.javatext import top_level_type_count
from loki.state.model import GenerationResult

_FENCE = re.compile(r"```(?:java)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_PLAN_LINE = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")


def _extract_plan(raw: str) -> list[str]:
    plan: list[str] = []
    in_plan = False
    for line in raw.splitlines():
        if line.strip().upper().startswith("PLAN"):
            in_plan = True
            continue
        if line.strip().startswith("```"):
            break
        if in_plan:
            m = _PLAN_LINE.match(line)
            if m:
                plan.append(m.group(1).strip())
    return plan


def parse_generation_response(raw: str) -> GenerationResult:
    """Extract and validate the plan and the single test class from ``raw``."""
    if not raw or not raw.strip():
        raise ParseError("Empty response from model")

    fences = _FENCE.findall(raw)
    if not fences:
        raise ParseError("No ```java code block found in response")
    # Prefer the block that actually declares a type (models sometimes emit an
    # incidental snippet first).
    candidates = [f for f in fences if re.search(r"\b(class|interface|enum|record)\b", f)]
    if not candidates:
        raise ParseError("No type declaration found in any code block")
    if len(candidates) > 1:
        raise ParseError(f"Expected one test class, found {len(candidates)} code blocks with types")

    test_source = candidates[0].strip() + "\n"
    count = top_level_type_count(test_source)
    if count != 1:
        raise ParseError(f"Expected exactly one top-level class, found {count}")

    return GenerationResult(
        plan=_extract_plan(raw),
        test_source=test_source,
        raw_response=raw,
    )
