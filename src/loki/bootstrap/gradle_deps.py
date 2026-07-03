"""Ensure test + measurement dependencies in ``build.gradle`` (DESIGN.md §4.1).

This is the ONLY place that edits build files, and it runs once per module during
bootstrap — never from a swarm worker (DESIGN.md §3, §11). The transform is
idempotent: dependencies/plugins already present are left untouched.

Version-managed dependencies (Spring Boot BOM covers junit-jupiter, assertj,
spring-boot-starter-test) are added unversioned; the rest carry explicit
versions.
"""

from __future__ import annotations

import re

# (gradle coordinate, explicit version or None if BOM-managed)
_REQUIRED_TEST_DEPS: list[tuple[str, str | None]] = [
    ("org.springframework.boot:spring-boot-starter-test", None),
    ("org.junit.jupiter:junit-jupiter-api", None),
    ("org.junit.platform:junit-platform-launcher", None),
    ("org.assertj:assertj-core", None),
    ("org.junit-pioneer:junit-pioneer", "2.2.0"),
    ("org.skyscreamer:jsonassert", "1.5.1"),
    ("com.tngtech.archunit:archunit-junit5", "1.3.0"),
]


def _dependency_line(coordinate: str, version: str | None) -> str:
    spec = coordinate if version is None else f"{coordinate}:{version}"
    return f"    testImplementation '{spec}'"


def _has_dependency(text: str, coordinate: str) -> bool:
    return coordinate in text


def _ensure_dependencies_block(text: str, additions: list[str]) -> str:
    if not additions:
        return text
    block = re.search(r"dependencies\s*\{", text)
    if block:
        insert_at = block.end()
        return text[:insert_at] + "\n" + "\n".join(additions) + text[insert_at:]
    # No dependencies block: append a fresh one.
    joined = "\n".join(additions)
    return text.rstrip() + "\n\ndependencies {\n" + joined + "\n}\n"


def _ensure_plugin(text: str, apply_line: str, marker: str) -> tuple[str, bool]:
    if marker in text:
        return text, False
    return text.rstrip() + "\n" + apply_line + "\n", True


def ensure_test_dependencies(build_gradle: str) -> tuple[str, bool]:
    """Return ``(updated_text, changed)`` with required deps/plugins ensured."""
    original = build_gradle
    additions = [
        _dependency_line(coord, ver)
        for coord, ver in _REQUIRED_TEST_DEPS
        if not _has_dependency(build_gradle, coord)
    ]
    build_gradle = _ensure_dependencies_block(build_gradle, additions)

    build_gradle, _ = _ensure_plugin(build_gradle, "apply plugin: 'jacoco'", "jacoco")
    build_gradle, _ = _ensure_plugin(
        build_gradle, "apply plugin: 'info.solidsoft.pitest'", "info.solidsoft.pitest"
    )
    return build_gradle, build_gradle != original
