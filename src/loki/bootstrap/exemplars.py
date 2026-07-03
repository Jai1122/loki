"""Harvest in-repo style exemplars (DESIGN.md §4.1 step 4).

A single well-written existing test is the highest-leverage context item for the
generator: it teaches the repo's idioms (AssertJ, JSONAssert, naming, profiles).
We rank existing test files by how many quality signals they contain and return
the best few.
"""

from __future__ import annotations

from pathlib import Path

_QUALITY_SIGNALS = ("assertThat", "assertEquals", "assertThrows", "@Test", "verify(", "JSONAssert")


def _score(source: str) -> int:
    return sum(source.count(signal) for signal in _QUALITY_SIGNALS)


def harvest_exemplars(test_src_dir: Path, limit: int = 3, max_chars: int = 6000) -> list[str]:
    """Return up to ``limit`` exemplar test sources, best first."""
    directory = Path(test_src_dir)
    if not directory.is_dir():
        return []
    scored: list[tuple[int, str, str]] = []
    for path in sorted(directory.rglob("*Test.java")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        score = _score(source)
        if score <= 0:
            continue
        scored.append((score, str(path), source[:max_chars]))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [source for _, _, source in scored[:limit]]
