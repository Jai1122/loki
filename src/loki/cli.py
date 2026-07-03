"""LOKI command-line interface (DESIGN.md README "Usage").

Commands:
  loki scan  <repo>   Scan & plan only; print the prioritized target list.
  loki run   <repo>   Generate tests. ``--dry-run`` gates without building/PRs.
  loki report <repo>  Print the report for an existing run state.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loki import __version__
from loki.config import load_config
from loki.deliver.report import build_report
from loki.errors import LokiError
from loki.llm.client import LLMClient
from loki.pipeline import Pipeline, run_dry, run_full
from loki.planner import build_plan
from loki.state.model import TaskState
from loki.state.store import StateStore

DEFAULT_STATE_FILE = ".loki/state.json"


def _state_path(repo: str) -> Path:
    return Path(repo) / DEFAULT_STATE_FILE


def _cmd_scan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = build_plan(args.repo, config, _state_path(args.repo))
    tasks = store.all_tasks()
    print(f"Planned {len(tasks)} target class(es) (highest priority first):")
    for task in tasks[: args.limit]:
        print(f"  {task.fqcn}  [module={task.module}, baseline={task.baseline_branch_cov:.0%}]")
    if len(tasks) > args.limit:
        print(f"  ... and {len(tasks) - args.limit} more")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state_path = _state_path(args.repo)

    if args.resume:
        store = StateStore.load(state_path)
        requeued = store.requeue_stale_in_progress()
        print(f"Resuming; re-queued {requeued} interrupted task(s).")
    else:
        store = build_plan(args.repo, config, state_path)
        print(f"Planned {len(store.all_tasks())} target class(es).")

    client = LLMClient(config.llm)
    pipeline = Pipeline(args.repo, config, client, store)

    if args.dry_run:
        run_dry(pipeline)
        print("Dry run complete (candidates written, gates applied; no build, no PRs).")
    else:
        run_full(pipeline)
        print("Run complete.")

    counts = store.counts()
    print(f"Results: {counts[TaskState.PASSED.value]} passed, "
          f"{counts[TaskState.PARKED.value]} parked.")
    print(f"State saved to {state_path}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    store = StateStore.load(_state_path(args.repo))
    print(build_report(store))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loki", description="LOKI test-generation swarm")
    parser.add_argument("--version", action="version", version=f"loki {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Scan & plan only (no LLM, no writes)")
    p_scan.add_argument("repo")
    p_scan.add_argument("--config", required=True)
    p_scan.add_argument("--limit", type=int, default=50)
    p_scan.set_defaults(func=_cmd_scan)

    p_run = sub.add_parser("run", help="Generate tests")
    p_run.add_argument("repo")
    p_run.add_argument("--config", required=True)
    p_run.add_argument("--dry-run", action="store_true", help="Generate + gate; no build, no PRs")
    p_run.add_argument("--resume", action="store_true", help="Resume from saved state")
    p_run.set_defaults(func=_cmd_run)

    p_report = sub.add_parser("report", help="Print the report for an existing run")
    p_report.add_argument("repo")
    p_report.set_defaults(func=_cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except LokiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
