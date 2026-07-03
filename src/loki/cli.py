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
from loki.benchmark import recommend_pool_size, run_benchmark
from loki.config import load_config
from loki.deliver.report import build_report
from loki.errors import LokiError
from loki.llm.client import LLMClient
from loki.pipeline import (
    Pipeline,
    deliver,
    ensure_dependencies,
    make_baseline_provider,
    run_dry,
    run_full,
)
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
    if args.max_turns is not None:
        config.verification.max_llm_turns_per_class = args.max_turns
    state_path = _state_path(args.repo)

    if args.resume:
        store = StateStore.load(state_path)
        requeued = store.requeue_stale_in_progress()
        print(f"Resuming; re-queued {requeued} interrupted task(s).")
    elif args.dry_run:
        store = build_plan(args.repo, config, state_path)  # no build in dry-run
        print(f"Planned {len(store.all_tasks())} target class(es).")
    else:
        # Phase 0 bootstrap: inject build deps (single owner) and baseline coverage.
        changed = ensure_dependencies(args.repo)
        if changed:
            print(f"Bootstrapped build.gradle in module(s): {', '.join(changed)}")
        store = build_plan(args.repo, config, state_path, baseline_provider=make_baseline_provider())
        print(f"Planned {len(store.all_tasks())} target class(es).")

    client = LLMClient(config.llm)
    pipeline = Pipeline(args.repo, config, client, store)

    if args.dry_run:
        run_dry(pipeline)
        print("Dry run complete (candidates written, gates applied; no build, no PRs).")
    else:
        run_full(pipeline)
        report_path = deliver(pipeline, open_prs=not args.no_pr)
        print(f"Run complete. Report: {report_path}")
        if args.no_pr:
            print("(--no-pr: report written, PRs not opened)")

    counts = store.counts()
    print(f"Results: {counts[TaskState.PASSED.value]} passed, "
          f"{counts[TaskState.PARKED.value]} parked.")
    print(f"State saved to {state_path}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    store = StateStore.load(_state_path(args.repo))
    print(build_report(store))
    return 0


def _cmd_benchmark(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    client = LLMClient(config.llm)
    print(f"Benchmarking {config.llm.base_url} (model={config.llm.model})")
    print(f"{args.requests} request(s) per level, up to concurrency {args.max_concurrency}\n")
    results = run_benchmark(client, args.max_concurrency, args.requests)

    print(f"{'concurrency':>11} | {'ok/total':>9} | {'throughput':>12} | {'mean latency':>12}")
    print("-" * 54)
    for r in results:
        print(
            f"{r.concurrency:>11} | {r.successes:>3}/{r.requests:<5} | "
            f"{r.throughput_rps:>8.2f} rps | {r.mean_latency_s:>9.2f} s"
        )

    recommended = recommend_pool_size(results)
    print()
    if recommended == 0:
        print("All requests failed — check llm.base_url, the token env var, and llm.model.")
        return 1
    print(f"Recommended concurrency.worker_pool_size: {recommended}")
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
    p_run.add_argument("--no-pr", action="store_true", help="Write the report but do not open PRs")
    p_run.add_argument("--max-turns", type=int, default=None,
                       help="Override LLM turns per class (config: verification.max_llm_turns_per_class)")
    p_run.set_defaults(func=_cmd_run)

    p_report = sub.add_parser("report", help="Print the report for an existing run")
    p_report.add_argument("repo")
    p_report.set_defaults(func=_cmd_report)

    p_bench = sub.add_parser("benchmark", help="Measure vLLM throughput to size the swarm")
    p_bench.add_argument("--config", required=True)
    p_bench.add_argument("--requests", type=int, default=6, help="Requests per concurrency level")
    p_bench.add_argument("--max-concurrency", type=int, default=8)
    p_bench.set_defaults(func=_cmd_benchmark)

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
