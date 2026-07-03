"""Tests for config loading, validation, and secret handling."""

from __future__ import annotations

import pytest

from loki.config import load_config
from loki.errors import ConfigError

MINIMAL = """
llm:
  base_url: https://myvllm.com/v1/
  model: minimax
"""


def write(tmp_path, text: str):
    path = tmp_path / "loki.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_config_applies_defaults(tmp_path) -> None:
    cfg = load_config(write(tmp_path, MINIMAL))
    assert cfg.llm.base_url == "https://myvllm.com/v1"  # trailing slash stripped
    assert cfg.llm.model == "minimax"
    assert cfg.verification.candidates_per_batch_k == 8
    assert cfg.quality.pit_enabled is True
    assert cfg.delivery.pr_chunking == "per-module"
    assert "**/dto/**" in cfg.exclusions


def test_api_key_read_from_named_env_var(tmp_path, monkeypatch) -> None:
    cfg = load_config(write(tmp_path, MINIMAL))
    monkeypatch.delenv("LOKI_LLM_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        _ = cfg.llm.api_key
    monkeypatch.setenv("LOKI_LLM_TOKEN", "secret-token")
    assert cfg.llm.api_key == "secret-token"


def test_snapshot_excludes_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOKI_LLM_TOKEN", "secret-token")
    cfg = load_config(write(tmp_path, MINIMAL))
    snap = cfg.snapshot()
    assert "secret-token" not in str(snap)
    assert snap["llm"]["model"] == "minimax"


def test_missing_llm_section_raises(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "verification:\n  candidates_per_batch_k: 4\n"))


def test_missing_base_url_raises(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "llm:\n  model: minimax\n"))


def test_invalid_values_rejected(tmp_path) -> None:
    text = MINIMAL + "verification:\n  candidates_per_batch_k: 0\n"
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))

    text = MINIMAL + "delivery:\n  pr_chunking: per-file\n"
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))

    text = MINIMAL + "quality:\n  target_branch_coverage: 1.5\n"
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_missing_file_raises(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.yaml")


def test_exclusions_must_be_list(tmp_path) -> None:
    text = MINIMAL + "exclusions: nope\n"
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))
