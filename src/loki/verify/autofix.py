"""Deterministic auto-fixers (DESIGN.md §5).

These mechanical repairs run *before* any LLM repair turn so the turn budget is
spent on logic, not boilerplate. They fix the package declaration, add the
Mockito extension when ``@Mock`` is present, and add missing imports for the
common JUnit 5 / Mockito / AssertJ symbols the test uses. All fixes are
idempotent: running them on already-correct source changes nothing.
"""

from __future__ import annotations

import re

from loki.javatext import mask

# Simple annotation/type name -> import.
_TYPE_IMPORTS = {
    "Test": "org.junit.jupiter.api.Test",
    "BeforeEach": "org.junit.jupiter.api.BeforeEach",
    "AfterEach": "org.junit.jupiter.api.AfterEach",
    "DisplayName": "org.junit.jupiter.api.DisplayName",
    "Nested": "org.junit.jupiter.api.Nested",
    "Disabled": "org.junit.jupiter.api.Disabled",
    "Mock": "org.mockito.Mock",
    "InjectMocks": "org.mockito.InjectMocks",
    "Spy": "org.mockito.Spy",
    "Captor": "org.mockito.Captor",
    "ArgumentCaptor": "org.mockito.ArgumentCaptor",
    "ExtendWith": "org.junit.jupiter.api.extension.ExtendWith",
    "MockitoExtension": "org.mockito.junit.jupiter.MockitoExtension",
    "ParameterizedTest": "org.junit.jupiter.params.ParameterizedTest",
    "ValueSource": "org.junit.jupiter.params.provider.ValueSource",
    "CsvSource": "org.junit.jupiter.params.provider.CsvSource",
    "MethodSource": "org.junit.jupiter.params.provider.MethodSource",
    "EnumSource": "org.junit.jupiter.params.provider.EnumSource",
}

# Static wildcard imports keyed by a probe token that implies their use, guarded
# by an owner substring so we do not double-import.
_STATIC_IMPORTS = [
    (re.compile(r"\bassertThat\s*\("), "org.assertj.core.api.Assertions", "org.assertj.core.api.Assertions.*"),
    (re.compile(r"\b(assertEquals|assertTrue|assertFalse|assertThrows|assertNull|assertNotNull|assertAll|assertDoesNotThrow|fail)\s*\("),
     "org.junit.jupiter.api.Assertions", "org.junit.jupiter.api.Assertions.*"),
    (re.compile(r"\b(mock|when|verify|verifyNoInteractions|verifyNoMoreInteractions|doReturn|doThrow|doNothing|inOrder|spy)\s*\("),
     "org.mockito.Mockito", "org.mockito.Mockito.*"),
    (re.compile(r"\b(any|anyInt|anyLong|anyString|anyBoolean|anyList|anyMap|anySet|eq|argThat|isNull|nullable)\s*\("),
     "org.mockito.ArgumentMatchers", "org.mockito.ArgumentMatchers.*"),
]


def _ensure_package(source: str, package: str) -> tuple[str, bool]:
    if not package:
        return source, False
    m = re.search(r"^\s*package\s+([\w.]+)\s*;", source, re.MULTILINE)
    if m and m.group(1) == package:
        return source, False
    if m:
        fixed = source[: m.start()] + f"package {package};" + source[m.end():]
        return fixed, True
    return f"package {package};\n\n{source}", True


def _has_import(source: str, fqn: str) -> bool:
    return re.search(rf"^\s*import\s+(static\s+)?{re.escape(fqn)}", source, re.MULTILINE) is not None


def _import_owner_present(source: str, owner: str) -> bool:
    return re.search(rf"^\s*import\s+static\s+{re.escape(owner)}\.", source, re.MULTILINE) is not None


def _insertion_point(source: str) -> int:
    imports = list(re.finditer(r"^\s*import\s+.*;\s*$", source, re.MULTILINE))
    if imports:
        return imports[-1].end()
    pkg = re.search(r"^\s*package\s+[\w.]+\s*;\s*$", source, re.MULTILINE)
    if pkg:
        return pkg.end()
    return 0


def _needed_imports(source: str, masked: str) -> list[str]:
    needed: list[str] = []
    for name, fqn in _TYPE_IMPORTS.items():
        if re.search(rf"@{name}\b|\b{name}\.class\b|\b{name}<", masked) and not _has_import(source, fqn):
            needed.append(f"import {fqn};")
    for probe, owner, wildcard in _STATIC_IMPORTS:
        if probe.search(masked) and not _import_owner_present(source, owner):
            needed.append(f"import static {wildcard};")
    return needed


def _ensure_imports(source: str) -> tuple[str, bool]:
    masked = mask(source)
    needed = _needed_imports(source, masked)
    if not needed:
        return source, False
    at = _insertion_point(source)
    block = "\n" + "\n".join(needed)
    return source[:at] + block + source[at:], True


def _ensure_mockito_extension(source: str) -> tuple[str, bool]:
    masked = mask(source)
    if "@Mock" not in masked and "@InjectMocks" not in masked:
        return source, False
    if re.search(r"@ExtendWith\s*\(\s*MockitoExtension\.class\s*\)", masked):
        return source, False
    m = re.search(r"^(\s*)((?:public\s+|final\s+|abstract\s+)*)(class\s+\w+)", source, re.MULTILINE)
    if not m:
        return source, False
    indent = m.group(1)
    annotation = f"{indent}@ExtendWith(MockitoExtension.class)\n"
    return source[: m.start()] + annotation + source[m.start():], True


def autofix(source: str, package: str) -> tuple[str, bool]:
    """Apply all mechanical fixes; return ``(fixed_source, changed)``."""
    source, c1 = _ensure_package(source, package)
    source, c2 = _ensure_mockito_extension(source)
    source, c3 = _ensure_imports(source)
    return source, (c1 or c2 or c3)
