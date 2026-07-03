"""Tests for the Java-source utilities, including the `.class` literal fix."""

from __future__ import annotations

from loki import javatext
from loki.llm.parse import parse_generation_response

MOCKITO_TEST = """\
package com.acme;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.junit.jupiter.MockitoExtension;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

@ExtendWith(MockitoExtension.class)
class OrderServiceTest {
    @Test
    void throwsOnBadInput() {
        assertThatThrownBy(() -> new OrderService(null).total(0))
            .isInstanceOf(IllegalArgumentException.class);
    }
}
"""


def test_dot_class_literals_are_not_counted_as_types() -> None:
    # Two `.class` literals plus one real class declaration -> exactly one type.
    assert javatext.top_level_type_count(MOCKITO_TEST) == 1


def test_parser_accepts_mockito_extension_test() -> None:
    raw = "PLAN:\n- throws on bad input\n\n```java\n" + MOCKITO_TEST + "```\n"
    result = parse_generation_response(raw)
    assert "class OrderServiceTest" in result.test_source


def test_nested_class_counts_as_one_top_level() -> None:
    src = "package com.acme;\nclass Outer {\n  static class Inner {}\n}\n"
    assert javatext.top_level_type_count(src) == 1


def test_two_top_level_classes_counted() -> None:
    src = "class A {}\nclass B {}\n"
    assert javatext.top_level_type_count(src) == 2
