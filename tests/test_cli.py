"""Tests for the CLI entry points (argument wiring and scan/report commands)."""

from __future__ import annotations

from pathlib import Path

from loki.cli import main
from loki.planner import build_plan

CONFIG_YAML = """\
llm:
  base_url: https://vllm/v1
  model: minimax
  api_key_env: TOK
"""


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "loki.yaml"
    path.write_text(CONFIG_YAML, encoding="utf-8")
    return path


def test_scan_command_lists_targets(repo: Path, capsys) -> None:
    cfg = _write_config(repo)
    rc = main(["scan", str(repo), "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "com.acme.CalculatorService" in out
    assert "Planned 1 target class" in out


def test_report_command_reads_state(repo: Path, config, capsys) -> None:
    build_plan(repo, config, repo / ".loki" / "state.json")
    rc = main(["report", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# LOKI run report" in out


def test_invalid_config_returns_error(repo: Path, capsys) -> None:
    rc = main(["scan", str(repo), "--config", str(repo / "missing.yaml")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
