"""Subprocess helper used by every shell-out module (Gradle, git, gh).

Wrapping subprocess behind a tiny ``Runner`` callable lets the build coordinator,
bootstrap, and delivery steps be tested with a fake runner — no real Gradle or
git required in unit tests.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# A Runner takes (argv, cwd, timeout_seconds) and returns a CommandResult.
Runner = Callable[[list[str], Path, float], CommandResult]


def run_command(argv: list[str], cwd: Path, timeout: float = 1800.0) -> CommandResult:
    """Execute a command, capturing output. Never raises on non-zero exit."""
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)
    except FileNotFoundError as exc:
        return CommandResult(127, "", f"command not found: {argv[0]} ({exc})")
    except subprocess.TimeoutExpired as exc:
        return CommandResult(124, exc.stdout or "", f"timed out after {timeout}s")
