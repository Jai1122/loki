"""Throughput-aware generation swarm (DESIGN.md §3, §4.3).

Generation is I/O-bound (HTTP to vLLM), so a bounded thread pool is the right
tool. The pool size must not exceed the endpoint's measured concurrency ceiling,
and an optional rate limiter caps requests/second. Tasks are pulled one at a time
via the store's atomic ``claim_next_pending`` so each class is handled by exactly
one worker and the run stays resumable.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from loki.state.model import Task
from loki.state.store import StateStore

DEFAULT_POOL_SIZE = 4  # conservative default when config says "auto" (0)


class RateLimiter:
    """Spaces request starts to at most ``rps`` per second (thread-safe)."""

    def __init__(self, rps: float) -> None:
        self._min_interval = 1.0 / rps if rps and rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            start = max(now, self._next_allowed)
            self._next_allowed = start + self._min_interval
        if wait > 0:
            time.sleep(wait)


def resolve_pool_size(configured: int) -> int:
    """0 (auto) → a safe default; otherwise the configured size."""
    return configured if configured and configured > 0 else DEFAULT_POOL_SIZE


def run_swarm(
    store: StateStore,
    handler: Callable[[Task], None],
    pool_size: int,
    rps: float = 0.0,
) -> int:
    """Run ``handler`` over every pending task using ``pool_size`` workers.

    ``handler`` is responsible for advancing the task's state and persisting it
    via the store. Exceptions from a handler are contained so one bad class never
    halts the swarm; the handler itself decides how to record failures. Returns
    the number of tasks processed.
    """
    size = resolve_pool_size(pool_size)
    limiter = RateLimiter(rps)
    count_lock = threading.Lock()
    counter = {"n": 0}

    def worker() -> None:
        while True:
            task = store.claim_next_pending()
            if task is None:
                return
            limiter.acquire()
            try:
                handler(task)
            finally:
                with count_lock:
                    counter["n"] += 1

    with ThreadPoolExecutor(max_workers=size) as pool:
        futures = [pool.submit(worker) for _ in range(size)]
        for future in futures:
            future.result()  # surface unexpected worker-loop errors
    return counter["n"]
