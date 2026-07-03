"""Configuration loading and validation (DESIGN.md §16).

Config is read from a YAML file into typed dataclasses. The LLM bearer token is
never stored in the file: the file names an environment variable
(``api_key_env``) and the token is read from the process environment. Invalid or
missing values raise :class:`ConfigError` with an actionable message.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from loki.errors import ConfigError

DEFAULT_EXCLUSIONS: tuple[str, ...] = (
    "**/config/**",
    "**/dto/**",
    "**/*MapperImpl.java",
    "**/generated/**",
)


@dataclass
class LLMConfig:
    base_url: str
    model: str
    api_key_env: str = "LOKI_LLM_TOKEN"
    max_context_tokens: int = 90_000
    temperature: float = 0.2
    request_timeout_s: float = 120.0

    @property
    def api_key(self) -> str:
        token = os.environ.get(self.api_key_env, "")
        if not token:
            raise ConfigError(
                f"LLM bearer token not found in environment variable '{self.api_key_env}'. "
                f"Export it, e.g. `export {self.api_key_env}=...`."
            )
        return token


@dataclass
class ConcurrencyConfig:
    worker_pool_size: int = 0  # 0 = auto (from benchmark)
    requests_per_second: float = 0.0  # 0 = unthrottled beyond the pool size


@dataclass
class VerificationConfig:
    candidates_per_batch_k: int = 8
    max_llm_turns_per_class: int = 5


@dataclass
class QualityConfig:
    pit_enabled: bool = True
    chase_mutants: bool = False
    target_branch_coverage: float = 0.90
    min_mutation_score_report: float = 0.0


@dataclass
class DeliveryConfig:
    pr_chunking: str = "per-module"  # or "per-package"
    label: str = "characterization-tests"


@dataclass
class LokiConfig:
    llm: LLMConfig
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    exclusions: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUSIONS))

    def snapshot(self) -> dict[str, Any]:
        """A JSON-serializable copy for the run state (token excluded)."""
        return {
            "llm": {
                "base_url": self.llm.base_url,
                "model": self.llm.model,
                "max_context_tokens": self.llm.max_context_tokens,
                "temperature": self.llm.temperature,
            },
            "concurrency": {
                "worker_pool_size": self.concurrency.worker_pool_size,
                "requests_per_second": self.concurrency.requests_per_second,
            },
            "verification": {
                "candidates_per_batch_k": self.verification.candidates_per_batch_k,
                "max_llm_turns_per_class": self.verification.max_llm_turns_per_class,
            },
            "quality": {
                "pit_enabled": self.quality.pit_enabled,
                "chase_mutants": self.quality.chase_mutants,
                "target_branch_coverage": self.quality.target_branch_coverage,
            },
            "delivery": {
                "pr_chunking": self.delivery.pr_chunking,
                "label": self.delivery.label,
            },
            "exclusions": list(self.exclusions),
        }


def _require(mapping: dict[str, Any], key: str, section: str) -> Any:
    if key not in mapping or mapping[key] in (None, ""):
        raise ConfigError(f"Missing required config value: {section}.{key}")
    return mapping[key]


def load_config(path: str | Path) -> LokiConfig:
    """Parse and validate a LOKI config file."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Config root must be a mapping, got {type(raw).__name__}")
    return _build_config(raw)


def _build_config(raw: dict[str, Any]) -> LokiConfig:
    llm_raw = raw.get("llm")
    if not isinstance(llm_raw, dict):
        raise ConfigError("Missing required config section: llm")

    llm = LLMConfig(
        base_url=str(_require(llm_raw, "base_url", "llm")).rstrip("/"),
        model=str(_require(llm_raw, "model", "llm")),
        api_key_env=str(llm_raw.get("api_key_env", "LOKI_LLM_TOKEN")),
        max_context_tokens=int(llm_raw.get("max_context_tokens", 90_000)),
        temperature=float(llm_raw.get("temperature", 0.2)),
        request_timeout_s=float(llm_raw.get("request_timeout_s", 120.0)),
    )

    conc_raw = raw.get("concurrency", {}) or {}
    concurrency = ConcurrencyConfig(
        worker_pool_size=int(conc_raw.get("worker_pool_size", 0)),
        requests_per_second=float(conc_raw.get("requests_per_second", 0.0)),
    )

    ver_raw = raw.get("verification", {}) or {}
    verification = VerificationConfig(
        candidates_per_batch_k=int(ver_raw.get("candidates_per_batch_k", 8)),
        max_llm_turns_per_class=int(ver_raw.get("max_llm_turns_per_class", 5)),
    )

    qual_raw = raw.get("quality", {}) or {}
    quality = QualityConfig(
        pit_enabled=bool(qual_raw.get("pit_enabled", True)),
        chase_mutants=bool(qual_raw.get("chase_mutants", False)),
        target_branch_coverage=float(qual_raw.get("target_branch_coverage", 0.90)),
        min_mutation_score_report=float(qual_raw.get("min_mutation_score_report", 0.0)),
    )

    del_raw = raw.get("delivery", {}) or {}
    delivery = DeliveryConfig(
        pr_chunking=str(del_raw.get("pr_chunking", "per-module")),
        label=str(del_raw.get("label", "characterization-tests")),
    )

    exclusions = raw.get("exclusions")
    if exclusions is None:
        exclusions = list(DEFAULT_EXCLUSIONS)
    elif not isinstance(exclusions, list):
        raise ConfigError("config.exclusions must be a list of glob patterns")
    else:
        exclusions = [str(e) for e in exclusions]

    _validate(concurrency, verification, quality, delivery)

    return LokiConfig(
        llm=llm,
        concurrency=concurrency,
        verification=verification,
        quality=quality,
        delivery=delivery,
        exclusions=exclusions,
    )


def _validate(
    concurrency: ConcurrencyConfig,
    verification: VerificationConfig,
    quality: QualityConfig,
    delivery: DeliveryConfig,
) -> None:
    if concurrency.worker_pool_size < 0:
        raise ConfigError("concurrency.worker_pool_size must be >= 0 (0 = auto)")
    if verification.candidates_per_batch_k < 1:
        raise ConfigError("verification.candidates_per_batch_k must be >= 1")
    if verification.max_llm_turns_per_class < 1:
        raise ConfigError("verification.max_llm_turns_per_class must be >= 1")
    if not 0.0 <= quality.target_branch_coverage <= 1.0:
        raise ConfigError("quality.target_branch_coverage must be between 0 and 1")
    if delivery.pr_chunking not in ("per-module", "per-package"):
        raise ConfigError("delivery.pr_chunking must be 'per-module' or 'per-package'")
