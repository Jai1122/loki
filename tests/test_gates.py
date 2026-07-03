"""Gate self-tests (DESIGN.md §15): meaningless tests must be rejected, and
meaningful tests must pass. This is the executable guarantee behind the
"no assert(true)" requirement."""

from __future__ import annotations

import pytest

from loki.verify import gates


def wrap(*methods: str) -> str:
    body = "\n\n".join(methods)
    return (
        "package com.acme;\n"
        "import org.junit.jupiter.api.Test;\n"
        "import static org.junit.jupiter.api.Assertions.*;\n"
        "import static org.assertj.core.api.Assertions.*;\n"
        "import static org.mockito.Mockito.*;\n"
        "class FooTest {\n" + body + "\n}\n"
    )


# --------------------------------------------------------------------------
# BANNED patterns must be rejected
# --------------------------------------------------------------------------

BAD_CASES = {
    gates.ZERO_ASSERTION: "@Test void t() { int x = compute(); System.out.println(x); }",
    gates.TAUTOLOGY: "@Test void t() { assertTrue(true); }",
    gates.NON_NULL_ONLY: "@Test void t() { var r = service.load(); assertNotNull(r); }",
    gates.DOES_NOT_THROW_ONLY: "@Test void t() { assertDoesNotThrow(() -> service.run()); }",
    gates.STUB_FAIL_ONLY: '@Test void t() { fail("not implemented"); }',
    gates.EMPTY_BODY: "@Test void t() { }",
}


@pytest.mark.parametrize("expected_rule,method", list(BAD_CASES.items()))
def test_banned_patterns_are_rejected(expected_rule: str, method: str) -> None:
    violations = gates.analyze(wrap(method))
    assert violations, f"expected a violation for {expected_rule}"
    assert any(v.rule == expected_rule for v in violations), (
        f"expected rule {expected_rule}, got {[v.rule for v in violations]}"
    )


def test_assert_false_false_is_tautology() -> None:
    assert not gates.passes(wrap("@Test void t() { assertFalse(false); }"))


def test_assert_equals_identical_args_is_tautology() -> None:
    src = wrap("@Test void t() { assertEquals(user.getName(), user.getName()); }")
    assert any(v.rule == gates.TAUTOLOGY for v in gates.analyze(src))


def test_assert_that_is_true_on_literal_is_tautology() -> None:
    src = wrap("@Test void t() { assertThat(true).isTrue(); }")
    assert any(v.rule == gates.TAUTOLOGY for v in gates.analyze(src))


def test_assert_that_is_not_null_only_is_rejected() -> None:
    src = wrap("@Test void t() { assertThat(service.load()).isNotNull(); }")
    assert any(v.rule == gates.NON_NULL_ONLY for v in gates.analyze(src))


def test_verify_with_only_wildcard_matchers_is_change_detector() -> None:
    src = wrap("@Test void t() { service.save(u); verify(repo).persist(any(), anyLong()); }")
    assert any(v.rule == gates.INTERACTION_ONLY for v in gates.analyze(src))


def test_class_without_any_test_methods_is_rejected() -> None:
    src = "package com.acme;\nclass FooTest {\n  void helper() { }\n}\n"
    assert any(v.rule == gates.NO_TESTS for v in gates.analyze(src))


def test_disabled_without_reason_is_rejected() -> None:
    src = wrap("@Test\n@org.junit.jupiter.api.Disabled\nvoid t() { assertThat(x).isEqualTo(1); }")
    assert any(v.rule == gates.DISABLED_WITHOUT_REASON for v in gates.analyze(src))


# --------------------------------------------------------------------------
# MEANINGFUL tests must pass
# --------------------------------------------------------------------------

GOOD_METHODS = [
    "@Test void t() { assertThat(service.add(2, 3)).isEqualTo(5); }",
    "@Test void t() { assertEquals(5, service.add(2, 3)); }",
    "@Test void t() { assertThrows(IllegalArgumentException.class, () -> service.check(-1)); }",
    "@Test void t() { assertThatThrownBy(() -> s.run()).isInstanceOf(IllegalStateException.class); }",
    "@Test void t() { service.save(u); verify(repo).persist(eq(u)); }",
    "@Test void t() { service.save(u); verify(repo).delete(42L); }",
    "@Test void t() { assertThat(service.names()).containsExactly(\"a\", \"b\"); }",
    "@Test void t() { verifyNoInteractions(repo); }",
    "@Test void t() { assertThat(r).isNotNull().extracting(User::getName).isEqualTo(\"jay\"); }",
    "@Test void t() { assertNull(service.find(\"missing\")); }",
]


@pytest.mark.parametrize("method", GOOD_METHODS)
def test_meaningful_tests_pass(method: str) -> None:
    violations = gates.analyze(wrap(method))
    assert violations == [], f"unexpected violations: {[(v.rule, v.detail) for v in violations]}"


def test_meaningful_assertion_alongside_non_null_still_passes() -> None:
    # A non-null check is fine as long as there is also a real assertion.
    src = wrap("@Test void t() { var r = s.load(); assertNotNull(r); assertThat(r.id()).isEqualTo(7); }")
    assert gates.passes(src)


def test_assert_that_string_not_confused_by_masking() -> None:
    # 'assertTrue(true)' appears only inside a string literal; must NOT be flagged.
    src = wrap('@Test void t() { assertThat(msg).isEqualTo("assertTrue(true)"); }')
    assert gates.passes(src)


def test_commented_out_assertion_does_not_count() -> None:
    src = wrap("@Test void t() { // assertThat(x).isEqualTo(1);\n int y = f(); }")
    assert not gates.passes(src)  # only a comment -> zero real assertions


def test_nested_class_test_methods_are_analyzed() -> None:
    src = (
        "package com.acme;\n"
        "import org.junit.jupiter.api.*;\n"
        "class FooTest {\n"
        "  @Nested class WhenAdding {\n"
        "    @Test void t() { assertTrue(true); }\n"  # tautology in a nested class
        "  }\n"
        "}\n"
    )
    assert any(v.rule == gates.TAUTOLOGY for v in gates.analyze(src))


def test_constant_expression_assert_is_tautology() -> None:
    assert any(v.rule == gates.TAUTOLOGY for v in gates.analyze(wrap("@Test void t() { assertTrue(1 == 1); }")))
    assert any(v.rule == gates.TAUTOLOGY for v in gates.analyze(wrap("@Test void t() { assertFalse(2 > 1); }")))


def test_assert_true_on_variable_is_meaningful() -> None:
    assert gates.passes(wrap("@Test void t() { assertTrue(service.isReady()); }"))


def test_delegated_assertion_helper_is_not_rejected() -> None:
    # A test that delegates checks to a custom assert*/verify* helper must pass.
    assert gates.passes(wrap("@Test void t() { verifyResult(service.run()); }"))
    assert gates.passes(wrap("@Test void t() { assertValidOrder(service.create()); }"))


def test_non_deterministic_constructs_are_rejected() -> None:
    bad = [
        "@Test void t() { Thread.sleep(10); assertThat(x).isEqualTo(1); }",
        "@Test void t() { assertThat(svc.at(java.time.Instant.now())).isEqualTo(1); }",
        "@Test void t() { int r = new java.util.Random().nextInt(); assertThat(f(r)).isEqualTo(1); }",
        "@Test void t() { assertThat(java.util.UUID.randomUUID()).isNotNull(); }",
    ]
    for method in bad:
        rules = [v.rule for v in gates.analyze(wrap(method))]
        assert gates.NON_DETERMINISTIC in rules, f"missed non-determinism in: {method}"


def test_injected_clock_now_is_allowed() -> None:
    # LocalDate.now(clock) has an argument -> deterministic -> not flagged.
    src = wrap("@Test void t() { assertThat(svc.today(java.time.LocalDate.now(clock))).isEqualTo(d); }")
    assert not any(v.rule == gates.NON_DETERMINISTIC for v in gates.analyze(src))


def test_seeded_random_is_allowed() -> None:
    src = wrap("@Test void t() { var r = new java.util.Random(42L); assertThat(svc.pick(r)).isEqualTo(3); }")
    assert not any(v.rule == gates.NON_DETERMINISTIC for v in gates.analyze(src))


def test_parameterized_test_with_value_source_braces() -> None:
    # Annotation-array braces must not confuse method-body detection.
    src = (
        "package com.acme;\n"
        "import org.junit.jupiter.params.ParameterizedTest;\n"
        "import org.junit.jupiter.params.provider.ValueSource;\n"
        "import static org.assertj.core.api.Assertions.*;\n"
        "class FooTest {\n"
        "  @ParameterizedTest\n"
        "  @ValueSource(ints = {1, 2, 3})\n"
        "  void t(int x) { assertThat(service.square(x)).isEqualTo(x * x); }\n"
        "}\n"
    )
    assert gates.passes(src)
