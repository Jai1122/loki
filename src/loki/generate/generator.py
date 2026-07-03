"""The generation / repair / extension LLM calls (DESIGN.md §4.3, §4.5).

Each function performs one logical model interaction and returns a validated
:class:`GenerationResult`. Generation gets a single reformat retry when the model
violates the output contract; that retry is a formatting nudge, not a new turn of
reasoning.
"""

from __future__ import annotations

from loki.errors import ParseError
from loki.llm import prompts
from loki.llm.client import LLMClient
from loki.llm.parse import parse_generation_response
from loki.state.model import ContextPack, GenerationResult, Task, VerificationResult

_REFORMAT_NUDGE = (
    "\n\nIMPORTANT: your previous reply did not follow the required format. "
    "Reply with `PLAN:` bullets then exactly one ```java fenced block containing "
    "one complete test class, and nothing else."
)


def _complete_and_parse(client: LLMClient, system: str, user: str) -> GenerationResult:
    raw = client.complete(system, user)
    try:
        return parse_generation_response(raw)
    except ParseError:
        # One reformat retry before surfacing the failure to the caller.
        raw = client.complete(system, user + _REFORMAT_NUDGE)
        return parse_generation_response(raw)


def generate(client: LLMClient, task: Task, pack: ContextPack) -> GenerationResult:
    """Generate the initial test class for a target."""
    return _complete_and_parse(client, prompts.SYSTEM_PROMPT, prompts.build_user_prompt(task, pack))


def generate_repair(
    client: LLMClient, task: Task, test_source: str, result: VerificationResult
) -> GenerationResult:
    """Repair a candidate that failed to compile or had failing tests."""
    user = prompts.build_repair_prompt(task, test_source, result)
    return _complete_and_parse(client, prompts.SYSTEM_PROMPT, user)


def generate_coverage_extension(
    client: LLMClient,
    task: Task,
    test_source: str,
    uncovered_hints: list[str],
    missing_categories: list[str],
) -> GenerationResult:
    """Extend a passing candidate toward uncovered branches / edge cases."""
    user = prompts.build_coverage_prompt(task, test_source, uncovered_hints, missing_categories)
    return _complete_and_parse(client, prompts.SYSTEM_PROMPT, user)
