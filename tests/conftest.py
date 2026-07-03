"""Shared fixtures: a synthetic single-module Spring Boot repo and helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from loki.config import (
    ConcurrencyConfig,
    DeliveryConfig,
    LLMConfig,
    LokiConfig,
    QualityConfig,
    VerificationConfig,
)

CALCULATOR_SERVICE = """\
package com.acme;

import org.springframework.stereotype.Service;

@Service
public class CalculatorService {
    public int classify(int n) {
        if (n < 0) {
            return -1;
        }
        return 1;
    }
}
"""

EXISTING_TEST = """\
package com.acme;

import org.junit.jupiter.api.Test;
import static org.assertj.core.api.Assertions.assertThat;

class SampleTest {
    @Test
    void demonstratesStyle() {
        assertThat(1 + 1).isEqualTo(2);
    }
}
"""

GOOD_RESPONSE = """\
PLAN:
- negative input returns -1
- non-negative input returns 1

```java
package com.acme;

import org.junit.jupiter.api.Test;
import static org.assertj.core.api.Assertions.assertThat;

class CalculatorServiceTest {
    @Test
    void negativeReturnsMinusOne() {
        assertThat(new CalculatorService().classify(-5)).isEqualTo(-1);
    }

    @Test
    void nonNegativeReturnsOne() {
        assertThat(new CalculatorService().classify(5)).isEqualTo(1);
    }
}
```
"""

TAUTOLOGY_RESPONSE = """\
PLAN:
- placeholder

```java
package com.acme;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CalculatorServiceTest {
    @Test
    void t() {
        assertTrue(true);
    }
}
```
"""


class SingleResponseClient:
    """An LLM client stand-in that always returns the same response."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    def complete(self, system: str, user: str, temperature=None) -> str:
        self.calls += 1
        return self.response


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    main = tmp_path / "app" / "src" / "main" / "java" / "com" / "acme"
    test = tmp_path / "app" / "src" / "test" / "java" / "com" / "acme"
    main.mkdir(parents=True)
    test.mkdir(parents=True)
    (main / "CalculatorService.java").write_text(CALCULATOR_SERVICE, encoding="utf-8")
    (test / "SampleTest.java").write_text(EXISTING_TEST, encoding="utf-8")
    return tmp_path


@pytest.fixture
def config() -> LokiConfig:
    return LokiConfig(
        llm=LLMConfig(base_url="https://vllm/v1", model="minimax", api_key_env="TOK"),
        concurrency=ConcurrencyConfig(worker_pool_size=2),
        verification=VerificationConfig(candidates_per_batch_k=4, max_llm_turns_per_class=5),
        quality=QualityConfig(pit_enabled=False),
        delivery=DeliveryConfig(),
    )
