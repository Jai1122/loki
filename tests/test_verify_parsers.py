"""Tests for auto-fixers (§5), JaCoCo parsing (§4.4), and PIT parsing (§7)."""

from __future__ import annotations

from loki.verify import autofix, jacoco, pit


# --- autofix --------------------------------------------------------------

def test_autofix_adds_package_when_missing() -> None:
    fixed, changed = autofix.autofix("class FooTest {}\n", "com.acme")
    assert changed
    assert fixed.startswith("package com.acme;")


def test_autofix_corrects_wrong_package() -> None:
    fixed, changed = autofix.autofix("package wrong;\nclass FooTest {}\n", "com.acme")
    assert changed
    assert "package com.acme;" in fixed
    assert "package wrong;" not in fixed


def test_autofix_adds_mockito_extension_when_mock_present() -> None:
    src = "package com.acme;\nclass FooTest {\n  @Mock Repo repo;\n}\n"
    fixed, changed = autofix.autofix(src, "com.acme")
    assert changed
    assert "@ExtendWith(MockitoExtension.class)" in fixed
    assert "import org.mockito.junit.jupiter.MockitoExtension;" in fixed


def test_autofix_adds_missing_static_and_type_imports() -> None:
    src = (
        "package com.acme;\n"
        "class FooTest {\n"
        "  @Test void t() { when(repo.get()).thenReturn(1); assertThat(x).isEqualTo(1); }\n"
        "}\n"
    )
    fixed, _ = autofix.autofix(src, "com.acme")
    assert "import org.junit.jupiter.api.Test;" in fixed
    assert "import static org.assertj.core.api.Assertions.*;" in fixed
    assert "import static org.mockito.Mockito.*;" in fixed


def test_autofix_is_idempotent() -> None:
    src = (
        "package com.acme;\n"
        "import org.junit.jupiter.api.Test;\n"
        "import static org.assertj.core.api.Assertions.*;\n"
        "class FooTest {\n  @Test void t() { assertThat(1).isEqualTo(1); }\n}\n"
    )
    once, changed1 = autofix.autofix(src, "com.acme")
    twice, changed2 = autofix.autofix(once, "com.acme")
    assert not changed2
    assert once == twice


def test_autofix_ignores_symbols_inside_strings() -> None:
    src = 'package com.acme;\nimport org.junit.jupiter.api.Test;\nclass FooTest {\n  @Test void t() { String s = "assertThat(x)"; org.junit.jupiter.api.Assertions.assertEquals(1, f()); }\n}\n'
    fixed, _ = autofix.autofix(src, "com.acme")
    # 'assertThat' only appears in a string -> no AssertJ import added.
    assert "org.assertj.core.api.Assertions.*" not in fixed


# --- jacoco ---------------------------------------------------------------

JACOCO_XML = """<?xml version="1.0"?>
<report name="app">
  <package name="com/acme">
    <class name="com/acme/UserService">
      <counter type="BRANCH" missed="2" covered="6"/>
      <counter type="LINE" missed="1" covered="9"/>
    </class>
    <class name="com/acme/Pojo">
      <counter type="LINE" missed="0" covered="3"/>
    </class>
  </package>
</report>
"""


def test_jacoco_branch_coverage() -> None:
    cov = jacoco.parse_branch_coverage(JACOCO_XML)
    assert cov["com.acme.UserService"] == 0.75  # 6 / (6+2)
    assert cov["com.acme.Pojo"] == 1.0  # no branches -> line fallback fully covered


def test_jacoco_handles_empty_or_bad_xml() -> None:
    assert jacoco.parse_branch_coverage("") == {}
    assert jacoco.parse_branch_coverage("<not xml") == {}


# --- pit ------------------------------------------------------------------

PIT_XML = """<?xml version="1.0"?>
<mutations>
  <mutation detected="true" status="KILLED">
    <mutatedClass>com.acme.UserService</mutatedClass>
    <mutatedMethod>find</mutatedMethod>
    <mutator>org.pitest.mutationtest.engine.gregor.mutators.ConditionalsBoundaryMutator</mutator>
    <lineNumber>12</lineNumber>
  </mutation>
  <mutation detected="false" status="SURVIVED">
    <mutatedClass>com.acme.UserService</mutatedClass>
    <mutatedMethod>count</mutatedMethod>
    <mutator>org.pitest.mutationtest.engine.gregor.mutators.MathMutator</mutator>
    <lineNumber>20</lineNumber>
  </mutation>
</mutations>
"""


def test_pit_aggregates_per_class() -> None:
    reports = pit.parse_mutations(PIT_XML)
    report = reports["com.acme.UserService"]
    assert report.killed == 1
    assert report.survived == 1
    assert report.score == 0.5
    assert any("MathMutator" in d for d in report.surviving_details)


def test_pit_handles_empty() -> None:
    assert pit.parse_mutations("") == {}
