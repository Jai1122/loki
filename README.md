# LOKI 🐍

**L**LM-**O**rchestrated **K**iller-test **I**ntelligence

> A reusable framework that unleashes a *swarm of variants* (LLM agents) to
> generate high-quality JUnit 5 unit tests for Java Spring Boot repos. LOKI
> optimizes for **meaningful assertions that catch regressions** — not raw
> coverage. Good tests **kill mutants**; that's the bar.

- Runs **entirely inside a secured environment** — source code never leaves.
- Talks to a **self-hosted vLLM** endpoint (MiniMax / Qwen), OpenAI-compatible.
- Targets **Gradle + JUnit 5 + Mockito + Java 21 + Spring Boot** repos.
- Delivers results as a **branch + chunked Pull Requests**.

> **Status:** design finalized, implementation not started. See
> [`DESIGN.md`](./DESIGN.md) for the full architecture and contracts. This README
> describes how LOKI is intended to be executed.

---

## Why LOKI (not "just generate tests")

Coverage tools go green when a test *runs* a line — even if it never checks the
result. LLMs happily emit `assertThat(x).isNotNull()` tests that hit 90% coverage
and catch nothing. LOKI defends against that with:

1. A **closed feedback loop** — every test is compiled, run, and measured; only
   green tests that raise coverage are kept.
2. **Deterministic quality gates ("TVA")** — reject `assertTrue(true)`,
   zero-assertion, and non-null-only filler *before* it ever lands.
3. **Mutation testing (PIT)** — a *soft signal* surfaced in the PR that tells
   humans whether the assertions actually catch regressions.

See `DESIGN.md` §8 (edge-case taxonomy) and §9 (meaningful-assertion policy).

---

## Concepts in 30 seconds

| Term | Meaning |
|---|---|
| **Swarm / variants** | Many LLM workers generating tests in parallel, bounded by your vLLM endpoint's throughput. |
| **Characterization tests** | Generated tests pin *current* behavior (a regression net). They will pin existing bugs too — labeled so reviewers know. |
| **Kill a mutant** | PIT changes the source; a good test *fails* (kills the mutant). Surviving mutants = weak assertions. |
| **TVA** | The deterministic quality gates that prune bad test variants. |
| **Parked** | A class LOKI couldn't get to green within the turn budget — reported for humans, not committed. |

---

## Prerequisites

On the machine that will run LOKI (inside the secured env):

- **Python 3.11+**
- **JDK 21** and the target repo's **Gradle wrapper** (`./gradlew`) working
- Network reachability to your **vLLM endpoint** (OpenAI-compatible) + a **bearer token**
- The target Spring Boot repo checked out locally
- (Optional) Docker, only if you want repository/integration slice tests via Testcontainers

---

## Install

```bash
# from the framework directory
pip install -e .
# or, once packaged:
pipx install loki
```

---

## Configure

Copy the example config and edit it:

```bash
cp config/loki.example.yaml loki.yaml
export LOKI_LLM_TOKEN="<your vLLM bearer token>"
```

Minimal `loki.yaml`:

```yaml
llm:
  base_url: https://myvllm.com/v1     # OpenAI-compatible vLLM endpoint
  api_key_env: LOKI_LLM_TOKEN         # token read from this env var
  model: minimax                      # or qwen-*
  max_context_tokens: 90000
concurrency:
  worker_pool_size: 0                 # 0 = auto (from benchmark)
verification:
  candidates_per_batch_K: 8           # verify when K candidates are ready
  max_llm_turns_per_class: 5
quality:
  pit_enabled: true                   # soft signal, never blocks a commit
  target_branch_coverage: 0.90
delivery:
  pr_chunking: per-module
```

Full reference: `DESIGN.md` §16.

---

## Usage (intended CLI)

> One repository at a time.

```bash
# 0. Benchmark your vLLM endpoint to size the swarm (run once per endpoint)
loki benchmark --config loki.yaml

# 1. Scan & plan only — see what LOKI would target (no LLM, no writes)
loki scan /path/to/spring-repo --config loki.yaml

# 2. Dry run — generate + gate candidates, but do NOT open PRs
loki run /path/to/spring-repo --config loki.yaml --dry-run

# 3. Full run — generate, verify, and open chunked PRs
loki run /path/to/spring-repo --config loki.yaml

# Resume an interrupted run (durable work queue picks up where it stopped)
loki run /path/to/spring-repo --config loki.yaml --resume
```

### `run` flags

| Flag | Effect |
|---|---|
| `--dry-run` | Generate + gate, write candidates, **no build, no PRs**. |
| `--resume` | Continue from the durable work queue. |
| `--no-pr` | Full run + write the report, but do not open PRs. |
| `--max-turns <n>` | Override LLM turns per class (config: `verification.max_llm_turns_per_class`). |

A full `loki run` (without `--dry-run`) performs Phase 0 bootstrap (inject test
deps into `build.gradle`, baseline coverage) and Phase 5 delivery (write
`.loki/report.md`, open chunked PRs) automatically. `--dry-run` does neither.

Other tuning lives in `loki.yaml` (see `config/loki.example.yaml`):
`quality.chase_mutants`, `quality.pit_enabled`, `quality.target_branch_coverage`,
`verification.candidates_per_batch_k`, `concurrency.worker_pool_size`, and
`exclusions`.

---

## What a run does (pipeline)

```
0. Bootstrap   inject test+jacoco+pit deps (once), baseline coverage, grab style exemplars
1. Scan/Plan   AST inventory, dependency graph, exclude config/DTO/generated, prioritize → work queue
2. Generate    swarm of LLM workers writes candidate tests in parallel (no Gradle here)
3. Verify      per module, when K ready: compile → auto-fix → test → JaCoCo delta → PIT (soft)
4. Feedback    ≤5 LLM turns/class: repair failures, cover missing edge cases, TVA gates reject filler
5. Deliver     chunked PRs per module/package + coverage/mutation/parked report
```

Full detail: `DESIGN.md` §4.

---

## Outputs

- **Branch + PR(s)** per module/package, containing only `passed` tests.
- **PR body report**: branch-coverage delta, per-class mutation score (soft),
  pass rate, and the list of **parked** classes needing human attention.
- Tests are labeled **characterization tests** (assert current behavior).

---

## Guarantees & guardrails

- ✅ No test with only `assertTrue(true)` / `assertNotNull` / zero assertions survives the TVA gates.
- ✅ Every generated test targets concrete edge cases (null/empty, boundaries, exceptions, branches) — see `DESIGN.md` §8.
- ✅ Tests are deterministic (no real clock/random/network/DB, no reliance on hash ordering).
- ✅ Swarm workers **never** edit `build.gradle`; only the one-time bootstrap step does.
- ✅ Existing hand-written tests are never overwritten.
- ✅ Runs are **resumable** — kill and restart safely.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Throughput collapses / timeouts | Worker pool exceeds vLLM `max_num_seqs`. Re-run `loki benchmark`; set `worker_pool_size` accordingly. |
| Many classes `parked` at "won't compile" | Model struggling; check exemplar quality and collaborator signatures in the context pack. Consider raising `max_llm_turns`. |
| PIT is very slow | It's scoped per touched class and run once; if still slow, set `pit_enabled: false` (TVA static gates still enforce meaningfulness). |
| Coverage rises but PRs look weak | Low mutation scores in the report → triage those classes; enable `quality.chase_mutants` in `loki.yaml` for the hot packages. |
| Gradle daemon lock errors | Don't run multiple LOKI processes on one repo; verification is intentionally serialized per module. |

---

## Project layout

See `DESIGN.md` §12 for the framework's own source layout, §13 for data models,
and §14 for the LLM prompt contracts.

---

## Safety notes

LOKI is designed for a **secured, air-gapped-friendly** setup: it calls only your
configured vLLM endpoint and never sends source code to any external service.
Keep `LOKI_LLM_TOKEN` in your secret store, not in `loki.yaml`.
