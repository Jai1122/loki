"""Target exclusion rules (DESIGN.md §4.2 step 3).

Skip anything that is not worth unit-testing: config/bootstrap classes,
interfaces, generated code, and pure data holders (records/enums/DTOs with no
logic). Both configurable glob patterns and structural rules are applied.
"""

from __future__ import annotations

import re
from functools import lru_cache

from loki.scan.ast import ClassInfo

_GENERATED = {"Generated", "lombok"}
_CONFIG_STEREOTYPES = {"Configuration", "SpringBootApplication"}
_DATA_METHOD = re.compile(r"^(get|set|is|with|builder|toBuilder|equals|hashCode|toString|canEqual)")


@lru_cache(maxsize=512)
def _glob_to_regex(glob: str) -> re.Pattern[str]:
    out = ["^"]
    i = 0
    while i < len(glob):
        if glob[i : i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif glob[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def _glob_match(pattern: str, path: str) -> bool:
    return _glob_to_regex(pattern).match(path) is not None


def _is_data_holder(info: ClassInfo) -> bool:
    if info.complexity > 0:
        return False
    publics = info.public_methods
    if not publics:
        return True
    return all(_DATA_METHOD.match(m.name) for m in publics)


def is_excluded(info: ClassInfo, rel_path: str, glob_patterns: list[str]) -> tuple[bool, str]:
    """Return ``(excluded, reason)`` for one class.

    ``rel_path`` is the repo-relative POSIX path of the source file.
    """
    for pattern in glob_patterns:
        if _glob_match(pattern, rel_path):
            return True, f"matches exclusion glob '{pattern}'"

    if any(a.rsplit(".", 1)[-1] in _GENERATED for a in info.annotations):
        return True, "generated code (@Generated)"

    if info.stereotype in _CONFIG_STEREOTYPES:
        return True, f"configuration class (@{info.stereotype})"

    if info.kind == "interface":
        return True, "interface (nothing to unit test directly)"

    if info.is_abstract and info.complexity == 0:
        return True, "abstract class with no concrete logic"

    if info.kind in ("record", "enum") and info.complexity == 0:
        return True, f"{info.kind} with no logic"

    if _is_data_holder(info):
        return True, "data holder / DTO with no logic"

    return False, ""
