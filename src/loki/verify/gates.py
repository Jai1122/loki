"""Static meaningful-assertion quality gates (DESIGN.md §9).

These deterministic gates are the guarantee that LOKI never commits trivial
tests. They run *before* the (soft) mutation-testing signal and reject — forcing
a re-prompt — any test that:

- has no assertion at all (``zero_assertion``);
- asserts only a tautology like ``assertTrue(true)`` / ``assertEquals(x, x)``
  (``tautology``);
- asserts only non-nullity (``non_null_only``);
- only checks that no exception was thrown (``does_not_throw_only``);
- only mirrors mock calls with wildcard matchers — a change-detector test
  (``interaction_only``);
- is an empty or ``fail()``-only stub, or ``@Disabled`` without a reason.

A test **passes** the gate as soon as it contains at least one *meaningful*
assertion, so legitimate tests that also happen to include a non-null check are
never penalised.
"""

from __future__ import annotations

import re

from loki.javatext import (
    extract_argument,
    find_test_methods,
    mask,
    split_top_level_args,
)
from loki.state.model import GateViolation

# Rule identifiers (stable; surfaced in PR reports and re-prompts).
NO_TESTS = "no_tests"
EMPTY_BODY = "empty_body"
ZERO_ASSERTION = "zero_assertion"
TAUTOLOGY = "tautology"
NON_NULL_ONLY = "non_null_only"
DOES_NOT_THROW_ONLY = "does_not_throw_only"
INTERACTION_ONLY = "interaction_only"
STUB_FAIL_ONLY = "stub_fail_only"
DISABLED_WITHOUT_REASON = "disabled_without_reason"
NON_DETERMINISTIC = "non_deterministic"

# A boolean argument built only from literals/operators (no variables) is a
# constant — e.g. assertTrue(1 == 1) — which tests nothing.
_CONSTANT_EXPR = re.compile(r"^[\d.\s()+\-*/%<>=!&|^~]+$")
# Custom assertion helpers (assertX/verifyX/checkX/expectX/...) count as a
# delegated assertion, so helper-based tests are not falsely rejected.
_DELEGATE_ASSERTION = re.compile(r"^(assert|verify|check|expect|ensure|should)[A-Za-z0-9_$]")
# Non-deterministic constructs forbidden in generated tests (DESIGN.md §8).
_NON_DETERMINISM = [
    (re.compile(r"\bThread\s*\.\s*sleep\s*\("), "Thread.sleep"),
    (re.compile(r"\bMath\s*\.\s*random\s*\("), "Math.random"),
    (re.compile(r"\bSystem\s*\.\s*(?:currentTimeMillis|nanoTime)\s*\("), "System time"),
    (re.compile(r"\bUUID\s*\.\s*randomUUID\s*\("), "UUID.randomUUID"),
    (re.compile(r"\bnew\s+(?:[\w.]*\.)?Random\s*\(\s*\)"), "new Random()"),
    (re.compile(r"\bnew\s+(?:[\w.]*\.)?Date\s*\(\s*\)"), "new Date()"),
    (
        re.compile(
            r"\b(?:Instant|LocalDate|LocalDateTime|LocalTime|ZonedDateTime|OffsetDateTime"
            r"|Clock|Year|YearMonth|MonthDay)\s*\.\s*now\s*\(\s*\)"
        ),
        "real clock .now()",
    ),
]

# Internal per-assertion signals.
_MEANINGFUL = "meaningful"
_TAUTOLOGY = "tautology"
_NON_NULL = "non_null"
_DOES_NOT_THROW = "does_not_throw"
_INTERACTION_WEAK = "interaction_weak"
_STUB = "stub"

_KNOWN_ASSERTIONS = {
    "assertEquals", "assertNotEquals", "assertTrue", "assertFalse", "assertNull",
    "assertNotNull", "assertSame", "assertNotSame", "assertArrayEquals",
    "assertIterableEquals", "assertLinesMatch", "assertThrows", "assertThrowsExactly",
    "assertDoesNotThrow", "assertTimeout", "assertTimeoutPreemptively", "assertAll",
    "assertThat", "assertThatThrownBy", "assertThatExceptionOfType", "assertThatCode",
    "assertThatObject", "assertThatList", "assertThatCollection", "fail",
    "verify", "verifyNoInteractions", "verifyNoMoreInteractions", "verifyZeroInteractions",
}

_CALL = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
_MEANINGFUL_ASSERTJ_TERMINAL = re.compile(
    r"\.(isEqualTo|isNotEqualTo|isSameAs|isNotSameAs|contains|containsExactly"
    r"|containsExactlyInAnyOrder|containsOnly|containsSequence|containsEntry|containsKey"
    r"|containsValue|containsExactlyElementsOf|hasSize|hasSizeGreaterThan|hasSizeLessThan"
    r"|isTrue|isFalse|isNull|isZero|isNotZero|isPositive|isNegative|isGreaterThan"
    r"|isGreaterThanOrEqualTo|isLessThan|isLessThanOrEqualTo|isBetween|isCloseTo|startsWith"
    r"|endsWith|matches|isEmpty|isNotEmpty|isInstanceOf|isExactlyInstanceOf|hasMessage"
    r"|hasMessageContaining|hasMessageStartingWith|hasCause|hasFieldOrPropertyWithValue"
    r"|extracting|returns|satisfies|usingRecursiveComparison|isEqualToIgnoringCase"
    r"|hasValue|isPresent|isNotPresent|hasToString|isSorted|isNotBlank|isEqualToIgnoringWhitespace)\b"
)
_BARE_MATCHER = re.compile(r"^(any\w*|nullable|isNull|isNotNull|notNull)\s*\(")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _statement_end(masked: str, start: int) -> int:
    depth = 0
    for i in range(start, len(masked)):
        ch = masked[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == ";" and depth == 0:
            return i
    return len(masked)


def _classify_verify(masked_body: str, close_idx: int) -> str:
    """Meaningful unless the invoked method's args are all wildcard matchers."""
    stmt_end = _statement_end(masked_body, close_idx)
    chain = masked_body[close_idx + 1 : stmt_end]
    call = _CALL.search(chain)
    if not call:
        return _MEANINGFUL  # e.g. verify(mock).method — lenient on unusual shapes
    inner, method_close = extract_argument(chain, call.end() - 1)
    if method_close == -1:
        return _MEANINGFUL
    args = [a for a in split_top_level_args(inner) if a]
    if not args:
        return _MEANINGFUL  # verifying a specific no-arg method call is a real assertion
    if all(_BARE_MATCHER.match(a.strip()) for a in args):
        return _INTERACTION_WEAK
    return _MEANINGFUL


def _classify_assert_that(masked_body: str, inner: str, close_idx: int) -> str:
    args = split_top_level_args(inner)
    subject = _norm(args[0]) if args else ""
    chain = masked_body[close_idx + 1 : _statement_end(masked_body, close_idx)]
    if subject in ("true", "false") and re.search(r"\.is(True|False)\s*\(", chain):
        return _TAUTOLOGY
    if _MEANINGFUL_ASSERTJ_TERMINAL.search(chain):
        return _MEANINGFUL
    if re.search(r"\.isNotNull\s*\(", chain):
        return _NON_NULL
    if re.search(r"\.doesNotThrowAnyException\s*\(", chain):
        return _DOES_NOT_THROW
    # Unrecognised terminal: stay lenient to avoid false rejections.
    return _MEANINGFUL


def _classify_call(name: str, masked_body: str, paren_idx: int) -> str:
    inner, close = extract_argument(masked_body, paren_idx)
    if close == -1:
        return _MEANINGFUL
    args = split_top_level_args(inner)
    arg0 = _norm(args[0]) if args else ""

    if name == "assertThat":
        return _classify_assert_that(masked_body, inner, close)
    if name == "verify":
        return _classify_verify(masked_body, close)
    if name in ("verifyNoInteractions", "verifyNoMoreInteractions", "verifyZeroInteractions"):
        return _MEANINGFUL
    if name == "assertTrue":
        if arg0 in ("true", "boolean.true", "!false") or _CONSTANT_EXPR.match(arg0):
            return _TAUTOLOGY
        return _MEANINGFUL
    if name == "assertFalse":
        if arg0 in ("false", "boolean.false", "!true") or _CONSTANT_EXPR.match(arg0):
            return _TAUTOLOGY
        return _MEANINGFUL
    if name == "assertNull":
        return _TAUTOLOGY if arg0 == "null" else _MEANINGFUL
    if name == "assertNotNull":
        return _NON_NULL
    if name in ("assertEquals", "assertSame", "assertArrayEquals", "assertIterableEquals"):
        if len(args) >= 2 and _norm(args[0]) == _norm(args[1]):
            return _TAUTOLOGY
        return _MEANINGFUL
    if name == "assertDoesNotThrow":
        return _DOES_NOT_THROW
    if name == "assertThatCode":
        chain = masked_body[close + 1 : _statement_end(masked_body, close)]
        if re.search(r"\.doesNotThrowAnyException\s*\(", chain):
            return _DOES_NOT_THROW
        return _MEANINGFUL
    if name == "fail":
        return _STUB
    # assertNotEquals, assertNotSame, assertThrows*, assertTimeout*, assertAll,
    # assertLinesMatch, assertThatThrownBy, assertThatExceptionOfType, ...
    return _MEANINGFUL


def _method_violation(name: str, body: str) -> str | None:
    if body.strip() == "":
        return EMPTY_BODY
    masked_body = mask(body)
    signals: set[str] = set()
    has_any = False
    for match in _CALL.finditer(masked_body):
        token = match.group(1)
        if token in _KNOWN_ASSERTIONS:
            has_any = True
            signals.add(_classify_call(token, masked_body, match.end() - 1))
        elif _DELEGATE_ASSERTION.match(token):
            # A custom assertion helper (assertX/verifyX/...) is assumed to assert;
            # this avoids falsely rejecting tests that delegate their checks.
            has_any = True
            signals.add(_MEANINGFUL)

    if _MEANINGFUL in signals:
        return None
    if not has_any:
        return ZERO_ASSERTION
    if _NON_NULL in signals:
        return NON_NULL_ONLY
    if _DOES_NOT_THROW in signals:
        return DOES_NOT_THROW_ONLY
    if _INTERACTION_WEAK in signals:
        return INTERACTION_ONLY
    if _TAUTOLOGY in signals:
        return TAUTOLOGY
    if _STUB in signals:
        return STUB_FAIL_ONLY
    return ZERO_ASSERTION


def _determinism_violations(test_source: str) -> list[GateViolation]:
    masked = mask(test_source)
    hits = [label for pattern, label in _NON_DETERMINISM if pattern.search(masked)]
    if not hits:
        return []
    return [GateViolation(NON_DETERMINISTIC, "non-deterministic construct(s): " + ", ".join(hits))]


def analyze(test_source: str) -> list[GateViolation]:
    """Return every meaningful-assertion violation in a generated test class."""
    methods = find_test_methods(test_source)
    if not methods:
        return [GateViolation(NO_TESTS, "Generated class contains no @Test methods")]

    violations: list[GateViolation] = []
    for method in methods:
        if method.disabled and not method.disabled_has_reason:
            violations.append(
                GateViolation(DISABLED_WITHOUT_REASON, f"{method.name} is @Disabled without a reason")
            )
            continue  # a disabled test contributes nothing; no point checking assertions
        rule = _method_violation(method.name, method.body)
        if rule is not None:
            violations.append(GateViolation(rule, f"{method.name}: {rule.replace('_', ' ')}"))
    # Determinism guardrails apply to the whole class (DESIGN.md §8).
    violations.extend(_determinism_violations(test_source))
    return violations


def passes(test_source: str) -> bool:
    """True when the test class has no meaningful-assertion violations."""
    return not analyze(test_source)
