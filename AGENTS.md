# AGENTS.md — working on LOKI

This file orients an AI coding agent (e.g. MiniMax/Qwen) that will **enhance**
this project on another machine. Read it fully before changing code. The
authoritative design is `DESIGN.md`; this file is the practical "how to work
here" guide.

---

## 1. What LOKI is (one paragraph)

LOKI is a Python framework that runs a swarm of LLM agents to generate
**meaningful** JUnit 5 unit tests for Java Spring Boot repos, raising branch
coverage from ~30% to ~90%. The non-negotiable goal is *quality, not coverage*:
generated tests must carry meaningful assertions and never be `assertTrue(true)`
filler. LOKI enforces this with deterministic quality gates (not an LLM reviewer)
plus soft mutation-testing signal. See `DESIGN.md` §1–§3 for the "why".

---

## 2. Golden rules (do not break these)

These are load-bearing invariants. A change that violates one is wrong even if
tests pass:

1. **The meaningful-assertion gates are the product.** `src/loki/verify/gates.py`
   must reject `assertTrue(true)`, tautologies, zero-assertion, non-null-only,
   does-not-throw-only, change-detector, and empty/`fail()`-only tests. If you
   touch it, the self-tests in `tests/test_gates.py` must still pass and you
   should add cases for any new pattern.
2. **Deterministic gates decide quality — never an LLM.** The LLM is used only
   for generation and repair. Do not add an "LLM reviewer".
3. **Parallelize generation, serialize verification.** Generation is a thread
   pool bounded by the vLLM endpoint; Gradle runs one at a time per module. Do
   not launch concurrent `gradle` invocations on one module.
4. **PIT mutation testing is a SOFT signal.** It is reported, never blocks a
   commit. Do not turn it into a hard gate by default.
5. **Only bootstrap edits `build.gradle`.** Swarm workers must never modify build
   files (`src/loki/bootstrap/gradle_deps.py` is the sole owner).
6. **The run is resumable.** All progress lives in the durable work queue
   (`src/loki/state/store.py`). Keep state changes going through the store.
7. **Config drives everything external.** Endpoint, model, and token are
   configurable (see §6). Never hardcode a URL, model name, or secret.
8. **Generated tests are characterization tests** (assert current behaviour) and
   must be deterministic (no real clock/random/network/DB).

---

## 3. Setup on a fresh machine

```bash
cd <repo>
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"       # installs loki + pytest; PyYAML & javalang are runtime deps
```

Python 3.11+ required. Java/Gradle are only needed to run the *full* pipeline
against a real repo, not to develop or test the framework itself.

---

## 4. How to validate your changes (run ALL of these)

```bash
. .venv/bin/activate

# 1. Unit + integration test suite — must stay green.
python -m pytest -q

# 2. Lint — must be clean (no unused imports / dead code).
python -m pyflakes src/loki tests          # pip install pyflakes if missing

# 3. CLI smoke test (no network needed).
loki --version
loki scan <path-to-any-spring-repo> --config config/loki.example.yaml
```

### End-to-end check with a stub endpoint (recommended, catches real bugs)
The unit tests use a fake LLM transport. To exercise the **real HTTP client and
the full `loki run` path**, stand up a stub OpenAI-compatible server that returns
a canned test class and run a dry run against it. This is exactly how the
`MockitoExtension.class` parser bug was found — do it after any change to the
LLM client, parser (`llm/parse.py`), or `javatext.py`.

Minimal stub (serve `POST /v1/chat/completions` returning
`{"choices":[{"message":{"content": "<PLAN + ```java fenced test```>"}}]}`), then:

```bash
export LOKI_LLM_TOKEN=dummy
loki run <demo-repo> --config <yaml pointing base_url at the stub> --dry-run
# Expect: "Results: 1 passed, 0 parked" and a written test file.
```

**Definition of done for any change:** pytest green, pyflakes clean, and if you
touched generation/parsing, the stub e2e still produces a passing test.

---

## 5. Architecture map (where things live)

Pipeline phases (DESIGN.md §4) → modules under `src/loki/`:

| Phase | Module(s) | Nature |
|---|---|---|
| 0 Bootstrap | `bootstrap/gradle_deps.py`, `bootstrap/exemplars.py`, `bootstrap/baseline.py` | deterministic |
| 1 Scan & plan | `scan/ast.py`, `scan/exclude.py`, `scan/graph.py`, `scan/prioritize.py`, `planner.py` | deterministic, no LLM |
| 2 Generate | `generate/contextpack.py`, `generate/generator.py`, `generate/worker_pool.py`, `llm/*` | LLM, parallel |
| 3 Verify | `verify/coordinator.py`, `verify/autofix.py`, `verify/jacoco.py`, `verify/pit.py` | Gradle, serialized |
| 4 Feedback/gates | `verify/gates.py`, `verify/edgecheck.py` | deterministic gates + LLM repair |
| 5 Deliver | `deliver/report.py`, `deliver/pr.py` | deterministic |
| Cross-cutting | `config.py`, `state/model.py`, `state/store.py`, `proc.py`, `errors.py`, `javatext.py`, `pipeline.py`, `cli.py` | |

Each source module has a matching `tests/test_*.py`. Start from the test to
understand a module's contract.

### The one tricky utility: `javatext.py`
It masks the contents of comments/strings/char-literals/text-blocks (preserving
length and offsets) so regex/brace analysis only sees real code. **Any structural
analysis of Java must go through `mask()`** — do not regex raw source, or you
will match tokens inside strings/comments (that class of bug is why this module
exists). Type-declaration counting requires a keyword *followed by an
identifier* so `MockitoExtension.class` is not mistaken for a class declaration.

---

## 6. Configuration (endpoint / model / token) — all configurable

Config lives in a YAML file (see `config/loki.example.yaml`), parsed by
`src/loki/config.py`:

```yaml
llm:
  base_url: https://your-vllm/v1   # <-- endpoint, configurable
  model: minimax                   # <-- model name, configurable (e.g. qwen-*)
  api_key_env: LOKI_LLM_TOKEN      # <-- NAME of the env var holding the token
```

- The **endpoint** is `llm.base_url`.
- The **model name** is `llm.model`.
- The **API key/token** is read from the environment variable named by
  `llm.api_key_env` (default `LOKI_LLM_TOKEN`). It is intentionally **never**
  stored in the config file. Set it with `export LOKI_LLM_TOKEN=...`.

The client is OpenAI-compatible and posts to `{base_url}/chat/completions` with
`Authorization: Bearer <token>`. If you add config fields, add them to
`config.py`, validate them, include them in `LokiConfig.snapshot()` (minus
secrets), and update `config/loki.example.yaml` + `DESIGN.md` §16.

---

## 7. Coding conventions

- **Type hints everywhere**; `from __future__ import annotations` at the top.
- **Dataclasses** for data; explicit `to_dict`/`from_dict` where persisted.
- **Dependency injection for testability**: the LLM client takes a `Transport`,
  and shell-out modules take a `Runner` (`proc.py`). Preserve this — it is why
  the suite runs with no network and no Gradle. New external calls must go
  through an injectable seam.
- **Typed exceptions** from `errors.py`; the CLI converts `LokiError` to a clean
  message. Don't let raw tracebacks escape to users.
- **Keep functions small and named for intent.** Match the surrounding style.
- **Every new deterministic behaviour gets a unit test.** For LLM/Gradle paths,
  test the parsing/decision logic with fixtures + fakes.

---

## 8. Common enhancement recipes

**Add a new meaningful-assertion rule** (`verify/gates.py`):
1. Add a rule constant + detection in `_method_violation` / `_classify_call`.
2. Add both a rejecting and a non-rejecting case to `tests/test_gates.py`.
3. Keep it *lenient on unknowns* — only reject patterns you can identify, to
   avoid false rejections of good tests.

**Add a new edge-case category** (`verify/edgecheck.py` + `llm/prompts.py`):
1. Add the hint in `strategy_hints` / `EDGE_CASE_CHECKLIST`.
2. Optionally detect its absence in `missing_categories` for re-prompts.

**Add a config option**: see §6.

**Support a new build detail**: put parsing in `verify/coordinator.py` /
`verify/jacoco.py` / `verify/pit.py` and unit-test with sample XML/console text.

**Change prompts**: edit `llm/prompts.py`. Re-run the stub e2e — prompt changes
can subtly break the output contract the parser expects.

---

## 9. Do / Don't

**Do**
- Run pytest + pyflakes + the stub e2e before declaring done.
- Update `DESIGN.md` and `README.md` when behaviour changes.
- Keep the work queue as the single source of truth for run state.

**Don't**
- Hardcode endpoints, model names, or secrets.
- Regex raw Java source without `javatext.mask()`.
- Make PIT a hard gate, add an LLM reviewer, or let workers edit `build.gradle`.
- Run parallel Gradle on one module, or bypass the store for state.
- Weaken the gates to make more tests "pass" — the gates are the point.
