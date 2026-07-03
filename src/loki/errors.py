"""Typed exceptions raised across LOKI.

Keeping a small, explicit exception hierarchy lets callers (and the CLI) fail
with clear, actionable messages instead of leaking stack traces from deep in a
subprocess or parser.
"""

from __future__ import annotations


class LokiError(Exception):
    """Base class for every error LOKI raises deliberately."""


class ConfigError(LokiError):
    """Raised when configuration is missing, malformed, or invalid."""


class ScanError(LokiError):
    """Raised when the source scanner cannot analyse a repository."""


class LLMError(LokiError):
    """Raised when the vLLM endpoint call fails or returns nothing usable."""


class ParseError(LokiError):
    """Raised when an LLM response cannot be parsed into a test class."""


class GradleError(LokiError):
    """Raised when a Gradle invocation fails in a way LOKI cannot recover from."""


class StateError(LokiError):
    """Raised on inconsistent or corrupt run state."""
