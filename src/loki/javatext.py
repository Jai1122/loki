"""Lightweight, dependency-free Java source utilities.

The quality gates, auto-fixers, and response parser all need to reason about the
*structure* of generated Java without a full compiler. The one hazard in doing
that with regexes is braces/quotes that live inside string literals or comments.
:func:`mask` neutralizes exactly those regions (replacing their contents with
spaces while preserving length and newlines) so brace/paren matching and pattern
detection operate only on real code.

Everything here is deterministic and offset-preserving: an index into a masked
string maps to the same index in the original.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}

_TEST_ANNOTATION = re.compile(
    r"@(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b"
)
_METHOD_NAME = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
# A type *declaration* is a keyword followed by an identifier. Requiring the
# identifier is what distinguishes ``class Foo`` from the ``.class`` literal in
# expressions like ``MockitoExtension.class``.
_TYPE_DECL_KEYWORD = re.compile(r"\b(?:class|interface|enum|record)\s+[A-Za-z_$]")
_DISABLED = re.compile(r'@(?:[A-Za-z_$][\w$]*\.)*Disabled\b(\s*\(\s*"([^"]*)"\s*\))?')


@dataclass
class TestMethod:
    """A discovered ``@Test``-family method and the facts the gates need."""

    name: str
    body: str
    disabled: bool = False
    disabled_has_reason: bool = False


@dataclass
class TopLevelType:
    """A top-level type declaration split into its structural parts."""

    kind: str  # class | interface | enum | record
    name: str
    annotations: str  # raw text of the preceding annotation block
    header: str  # declaration text up to the opening brace
    body: str  # text between the type's braces


def mask(src: str) -> str:
    """Return a same-length copy of ``src`` with the *contents* of comments,
    string literals, char literals, and text blocks replaced by spaces.

    Newlines are preserved so line numbers are unchanged. Delimiters themselves
    are blanked too, which is harmless for structural analysis.
    """
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        # Line comment
        if c == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        # Block comment
        if c == "/" and nxt == "*":
            out.append(" ")
            out.append(" ")
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            # closing */
            if i < n:
                out.append(" ")
                i += 1
            if i < n:
                out.append(" ")
                i += 1
            continue
        # Text block """ ... """
        if c == '"' and src[i : i + 3] == '"""':
            out.append(" ")
            out.append(" ")
            out.append(" ")
            i += 3
            while i < n and src[i : i + 3] != '"""':
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            for _ in range(3):
                if i < n:
                    out.append(" ")
                    i += 1
            continue
        # String literal
        if c == '"':
            out.append(" ")
            i += 1
            while i < n and src[i] != '"':
                if src[i] == "\\" and i + 1 < n:
                    out.append(" ")
                    out.append(" ")
                    i += 2
                    continue
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            if i < n:  # closing quote
                out.append(" ")
                i += 1
            continue
        # Char literal
        if c == "'":
            out.append(" ")
            i += 1
            while i < n and src[i] != "'":
                if src[i] == "\\" and i + 1 < n:
                    out.append(" ")
                    out.append(" ")
                    i += 2
                    continue
                out.append(" ")
                i += 1
            if i < n:  # closing quote
                out.append(" ")
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def matching_bracket(masked: str, open_idx: int) -> int:
    """Index of the bracket that closes the one at ``open_idx`` (inclusive), or -1.

    ``masked`` must already have literals/comments neutralized.
    """
    if open_idx >= len(masked) or masked[open_idx] not in _OPEN_TO_CLOSE:
        return -1
    open_c = masked[open_idx]
    close_c = _OPEN_TO_CLOSE[open_c]
    depth = 0
    for i in range(open_idx, len(masked)):
        ch = masked[i]
        if ch == open_c:
            depth += 1
        elif ch == close_c:
            depth -= 1
            if depth == 0:
                return i
    return -1


def top_level_type_count(src: str) -> int:
    """Count top-level type declarations (class/interface/enum/record).

    "Top-level" = declared at brace depth 0. Used by the parser to enforce that
    a generated response contains exactly one test class.
    """
    masked = mask(src)
    # Record the brace depth at every index, then count only declarations that
    # sit at depth 0 (true top-level types, not nested classes or `.class`).
    depth = 0
    depth_at: list[int] = []
    for ch in masked:
        depth_at.append(depth)
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
    return sum(1 for m in _TYPE_DECL_KEYWORD.finditer(masked) if depth_at[m.start()] == 0)


def _annotation_block_start(src: str, ann_idx: int) -> int:
    """Start index of the contiguous annotation block preceding ``ann_idx``."""
    line_start = src.rfind("\n", 0, ann_idx) + 1
    while line_start > 0:
        prev_end = line_start - 1
        prev_start = src.rfind("\n", 0, prev_end) + 1
        if src[prev_start:prev_end].strip().startswith("@"):
            line_start = prev_start
        else:
            break
    return line_start


def _last_method_name(masked_header: str) -> str:
    matches = list(_METHOD_NAME.finditer(masked_header))
    return matches[-1].group(1) if matches else "<unknown>"


def find_test_methods(src: str) -> list[TestMethod]:
    """Extract every ``@Test``-family method with its raw body text.

    Handles nested/@Nested classes, annotations whose arguments contain braces
    (e.g. ``@ValueSource(ints = {1, 2})``), and multi-line signatures — because
    the method body is located as the first ``{`` at paren-depth 0 after the
    annotation, and annotation-array braces always sit inside parentheses.
    """
    masked = mask(src)
    methods: list[TestMethod] = []
    for m in _TEST_ANNOTATION.finditer(masked):
        i = m.end()
        depth = 0
        body_open = -1
        while i < len(masked):
            ch = masked[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "{" and depth == 0:
                body_open = i
                break
            elif ch == ";" and depth == 0:
                break  # no body (defensive; @Test methods have bodies)
            i += 1
        if body_open == -1:
            continue
        body_close = matching_bracket(masked, body_open)
        if body_close == -1:
            continue
        header_start = _annotation_block_start(src, m.start())
        header = src[header_start:body_open]
        name = _last_method_name(masked[header_start:body_open])
        body = src[body_open + 1 : body_close]
        disabled_match = _DISABLED.search(header)
        disabled = disabled_match is not None
        disabled_reason = bool(
            disabled_match and disabled_match.group(2) and disabled_match.group(2).strip()
        )
        methods.append(TestMethod(name, body, disabled, disabled_reason))
    return methods


_TYPE_DECL = re.compile(r"\b(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")


def top_level_types(src: str) -> list[TopLevelType]:
    """Split ``src`` into its top-level type declarations (depth 0).

    Returns each declaration's kind, name, preceding annotation block, header
    text, and brace-delimited body. Nested types are left inside their parent's
    body (callers that care recurse themselves).
    """
    masked = mask(src)
    results: list[TopLevelType] = []
    depth = 0
    i = 0
    n = len(masked)
    while i < n:
        ch = masked[i]
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0:
            m = _TYPE_DECL.match(masked, i)
            if m:
                kind, name = m.group(1), m.group(2)
                brace = masked.find("{", m.end())
                if brace == -1:
                    break
                close = matching_bracket(masked, brace)
                if close == -1:
                    break
                ann_start = _annotation_block_start(src, i)
                results.append(
                    TopLevelType(
                        kind=kind,
                        name=name,
                        annotations=src[ann_start:i],
                        header=src[i:brace],
                        body=src[brace + 1 : close],
                    )
                )
                i = close + 1
                depth = 0
                continue
        i += 1
    return results


def control_flow_complexity(src: str) -> int:
    """A cheap cyclomatic-style score: decision points in the code.

    Counts branch keywords and short-circuit / ternary operators on masked
    source (so operators inside strings/comments never count). Used to rank
    classes by risk during prioritization.
    """
    masked = mask(src)
    keywords = len(re.findall(r"\b(?:if|for|while|case|catch)\b", masked))
    operators = masked.count("&&") + masked.count("||") + masked.count("?")
    return keywords + operators


def extract_argument(masked: str, open_paren_idx: int) -> tuple[str, int]:
    """Given the index of a ``(``, return (inner_text, index_of_closing_paren)."""
    close = matching_bracket(masked, open_paren_idx)
    if close == -1:
        return "", -1
    return masked[open_paren_idx + 1 : close], close


def split_top_level_args(arg_text: str) -> list[str]:
    """Split a call's argument list on top-level commas.

    Depth is tracked with ``() [] {}`` only (not ``<>``), so comparison
    operators inside arguments never corrupt the split. Over-splitting on
    generic commas is acceptable: it can only cause the tautology check to *miss*
    a case, never to raise a false positive.
    """
    args: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in arg_text:
        if ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail or args:
        args.append(tail)
    return args
