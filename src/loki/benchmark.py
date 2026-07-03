"""vLLM throughput benchmark (DESIGN.md §3, §18).

The swarm's concurrency must not exceed what the self-hosted vLLM endpoint
sustains. This measures successful requests/second at increasing concurrency
levels using tiny prompts, and recommends the ``worker_pool_size`` that yields
the best throughput — the "first implementation task" that turns the endpoint's
real ceiling into a config value.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

from loki.errors import LLMError

_SYSTEM = "You are a throughput benchmark. Reply tersely."
_USER = "Reply with exactly: OK"


class _Completer(Protocol):
    def complete(self, system: str, user: str, temperature: float | None = ..., max_tokens: int | None = ...) -> str:
        ...


@dataclass
class BenchmarkResult:
    concurrency: int
    requests: int
    successes: int
    failures: int
    wall_s: float
    mean_latency_s: float

    @property
    def throughput_rps(self) -> float:
        return self.successes / self.wall_s if self.wall_s > 0 else 0.0


def _timed_call(client: _Completer) -> tuple[bool, float]:
    start = time.monotonic()
    try:
        client.complete(_SYSTEM, _USER, temperature=0.0, max_tokens=16)
        return True, time.monotonic() - start
    except LLMError:
        return False, time.monotonic() - start


def measure(client: _Completer, concurrency: int, requests: int) -> BenchmarkResult:
    """Fire ``requests`` calls through a pool of ``concurrency`` workers."""
    latencies: list[float] = []
    successes = 0
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for ok, latency in pool.map(lambda _: _timed_call(client), range(requests)):
            latencies.append(latency)
            if ok:
                successes += 1
    wall = time.monotonic() - start
    mean_latency = sum(latencies) / len(latencies) if latencies else 0.0
    return BenchmarkResult(concurrency, requests, successes, requests - successes, wall, mean_latency)


def concurrency_levels(max_concurrency: int) -> list[int]:
    """Powers of two up to (and including) ``max_concurrency``."""
    levels: list[int] = []
    level = 1
    while level < max_concurrency:
        levels.append(level)
        level *= 2
    levels.append(max(1, max_concurrency))
    return sorted(set(levels))


def run_benchmark(client: _Completer, max_concurrency: int, requests_per_level: int) -> list[BenchmarkResult]:
    return [measure(client, c, requests_per_level) for c in concurrency_levels(max_concurrency)]


def recommend_pool_size(results: list[BenchmarkResult]) -> int:
    """Concurrency with the highest sustained throughput; 0 if all failed."""
    successful = [r for r in results if r.successes > 0]
    if not successful:
        return 0
    return max(successful, key=lambda r: r.throughput_rps).concurrency
