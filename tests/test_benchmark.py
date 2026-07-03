"""Tests for the vLLM throughput benchmark."""

from __future__ import annotations

import time

from loki.benchmark import (
    concurrency_levels,
    measure,
    recommend_pool_size,
    run_benchmark,
)
from loki.errors import LLMError


class FakeClient:
    def __init__(self, latency: float = 0.0, fail: bool = False) -> None:
        self.latency = latency
        self.fail = fail
        self.calls = 0

    def complete(self, system, user, temperature=None, max_tokens=None) -> str:
        self.calls += 1
        if self.fail:
            raise LLMError("endpoint down")
        if self.latency:
            time.sleep(self.latency)
        return "OK"


def test_concurrency_levels() -> None:
    assert concurrency_levels(8) == [1, 2, 4, 8]
    assert concurrency_levels(1) == [1]
    assert concurrency_levels(5) == [1, 2, 4, 5]


def test_measure_counts_successes() -> None:
    client = FakeClient(latency=0.005)
    result = measure(client, concurrency=2, requests=6)
    assert result.successes == 6
    assert result.failures == 0
    assert result.throughput_rps > 0
    assert client.calls == 6


def test_measure_counts_failures() -> None:
    result = measure(FakeClient(fail=True), concurrency=2, requests=4)
    assert result.successes == 0
    assert result.failures == 4
    assert result.throughput_rps == 0


def test_run_benchmark_covers_all_levels() -> None:
    results = run_benchmark(FakeClient(), max_concurrency=4, requests_per_level=3)
    assert [r.concurrency for r in results] == [1, 2, 4]


def test_recommend_pool_size_picks_best_throughput() -> None:
    results = run_benchmark(FakeClient(latency=0.002), max_concurrency=4, requests_per_level=4)
    recommended = recommend_pool_size(results)
    assert recommended in (1, 2, 4)


def test_recommend_zero_when_all_fail() -> None:
    results = run_benchmark(FakeClient(fail=True), max_concurrency=4, requests_per_level=2)
    assert recommend_pool_size(results) == 0
