# LOKI — Design Document

**LOKI** = **L**LM-**O**rchestrated **K**iller-test **I**ntelligence

A reusable framework that unleashes a *swarm of variants* (LLM agents) to generate
high-quality, meaningful JUnit 5 unit tests for Java Spring Boot repositories —
tests whose job is to **kill mutants** (survive mutation testing), not pad
coverage. (Theme: Loki spawns infinite variants → our agent swarm; good tests
"kill mutants" → PIT mutation testing; the quality gate is the **TVA**, which
prunes bad variants.)

- Version: 1.1 (finalized design, pre-implementation)
- Date: 2026-07-03
- Status: Approved architecture. Implementation has **not** started.

> This document is written to be self-contained context for a code-generating
> LLM (e.g. MiniMax / Qwen with a ~90k-token window). It carries not just the
> *what* but the *why*, plus concrete contracts (data models, prompt formats,
> edge-case taxonomy, assertion rules) so a model can generate, enhance, or
> improve the framework's code without additional context.

---

## 1. Purpose

LOKI automatically generates JUnit 5 unit tests for Spring Boot repos to raise
branch-test coverage from ~30% to ~90%. The emphasis is **quality, not coverage
alone**: tests must contain **meaningful assertions that catch regressions**, and
must exercise **corner and edge cases** — never trivial `assertTrue(true)` /
`assertFalse(false)` filler.

Generated tests are **characterization tests**: they pin the code's *current*
behavior and form a regression safety net. Consequence: if the code currently
has a bug, the generated test asserts the buggy behavior as "expected." All
generated tests are labeled as characterization tests so reviewers interpret
them correctly.

The two quality forces in the system:
- **Deterministic gates** (compiler, test runner, JaCoCo, static assertion
  checks, PIT) decide whether a test is trustworthy — never an LLM "reviewer."
- **Mutation score** (soft signal) is the closest machine-checkable proxy for
  "the assertions are meaningful."

---

## 2. Environment & Constraints (hard requirements)

| Constraint | Detail |
|---|---|
| **Execution locus** | Runs **entirely inside a secured environment**. Source code never leaves. No public/external LLM API calls. |
| **LLM backend** | Self-hosted **vLLM** server hosting **MiniMax** and **Qwen**, exposed as an **OpenAI-compatible HTTP endpoint** (e.g. `https://myvllm.com/v1`) with a **bearer token**. URL, token, model are config. |
| **Model context window** | ~**90,000 tokens** effective. Large but finite — pack context deliberately; chunk large classes. |
| **Framework language** | **Python.** Shells out to Gradle for all build/test/coverage. |
| **Target repos** | Uniform: **Gradle + JUnit 5 + Mockito + Java 21 + Spring Boot**. May assume these conventions. |
| **Repo concurrency** | **One repository at a time.** |
| **Delivery** | **Branch + Pull Request(s)** per repo, chunked per module/package. |

### 2.1 Available test toolbox (generated tests may use)

- `spring-boot-starter-test` — `@WebMvcTest`, `@DataJpaTest`, `@SpringBootTest`, MockMvc
- `assertj-core` — **preferred** fluent assertions
- `junit-jupiter-api` / JUnit 5 — test framework
- `junit-platform-launcher` — programmatic launching
- `junit-pioneer` — JUnit 5 extension pack
- `jsonassert` (JSONAssert) — JSON payload comparison
- `archunit-junit5` — architecture/convention rules
- Mockito — mocking collaborators

JaCoCo (coverage) and PIT/pitest (mutation) are added by LOKI for measurement.

---

## 3. Guiding Principles

1. **Parallelize generation, serialize verification.** LLM generation fans out
   cheaply; Gradle compile/test is per-**module** and stateful. Many concurrent
   `gradle test` runs against one module cause daemon lock contention, redundant
   recompiles, and wasted wall-clock. Generation scales with the vLLM endpoint;
   verification is batched and serialized per module.

2. **Deterministic gates are the reviewer — not an LLM.** Compiler + test runner
   + JaCoCo + PIT + static checks are ground truth. "LLM reviews LLM" is
   low-signal and token-costly; it is intentionally excluded. LLM is used for
   *generation* and *targeted repair* only.

3. **Concurrency is bounded by the vLLM endpoint.** A single self-hosted vLLM
   instance has a finite ceiling (`max_num_seqs`, KV-cache, tokens/sec).
   "Max swarms" means "as many workers as the endpoint sustains," discovered by
   benchmark — not a fixed vanity number.

4. **Mutation score measures meaning; coverage measures execution.** JaCoCo goes
   green when a test *runs* code, even if it never checks the result. PIT mutates
   the source and reruns tests; a surviving mutant proves a test doesn't really
   assert behavior. (See §7.)

5. **Everything is resumable** via a durable work queue with per-class state
   (§10).

6. **Only one owner touches build files** — concurrent `build.gradle` edits by
   workers guarantee conflicts (§4.1).

---

## 4. Pipeline Overview

Six phases. LLM calls occur only in Phase 2 (generation) and Phase 4 (targeted
repair). Everything else is deterministic.

```
 Phase 0  Bootstrap (once/repo, deterministic, single owner)
    │        detect modules, inject test/measurement deps, baseline coverage, grab exemplars
    ▼
 Phase 1  Scan & Plan (deterministic, no LLM)
    │        AST inventory, dependency graph, exclusions, prioritization, DURABLE work queue
    ▼
 Phase 2  Parallel Generation (LLM, concurrency ≤ vLLM ceiling)
    │        per-class context pack → plan + test in one call → write candidate
    ▼
 Phase 3  Batched Verification (serialized per module, fires when K candidates ready)
    │        compileTestJava → auto-fix → test → JaCoCo delta → PIT (soft)
    ▼
 Phase 4  Feedback Loop (≤5 LLM turns/class; auto-fixers free)
    │        repair compile/test failures, target uncovered branches/edge cases, static quality gates
    ▼
 Phase 5  Deliver (chunked PRs + coverage/mutation/parked report)
```

Phases 3 and 4 interleave per class; shown separately for clarity.

### 4.1 Phase 0 — Bootstrap (once/repo, deterministic, single owner)
1. **Detect structure**: Gradle modules (multi-module aware), Java 21 toolchain, `src/main/java` + `src/test/java` per module.
2. **Ensure dependencies** in each `build.gradle`: assertj-core, junit-pioneer, jsonassert, archunit-junit5, junit-platform-launcher, junit-jupiter-api, spring-boot-starter-test, JaCoCo plugin, PIT plugin. **Only this step edits build files.**
3. **Baseline coverage**: `gradle test jacocoTestReport` on the untouched repo → record **per-class branch coverage** (the 30% baseline / source of truth).
4. **Harvest exemplars**: 1–3 existing well-written test classes for few-shot style (AssertJ, JSONAssert, naming, profiles).
5. **Record env facts**: Docker/Testcontainers availability, active Spring profiles, DB strategy.

### 4.2 Phase 1 — Scan & Plan (deterministic, no LLM)
1. **AST inventory** of `src/main/java`: package, annotations, public methods, constructor deps, field injections.
2. **Dependency graph** from constructor params + `@Autowired` fields → prioritization + what to mock.
3. **Exclusions** (rule-based, configurable): existing tests, `@Configuration`, DTOs/POJOs/records with no logic, generated code (MapStruct/Lombok-only/OpenAPI/protobuf), `main` bootstrap classes.
4. **Prioritization** by **coverage gap × risk** (uncovered service/business logic first).
5. **Write the durable work queue** — one Task per target class (schema in §13).

### 4.3 Phase 2 — Parallel Generation (LLM-bound, max concurrency)
- **Throughput-aware worker pool** pulls `pending` tasks; pool size = **measured** vLLM ceiling; bounded queue absorbs bursts; rate-limited to sustainable tokens/sec.
- Each worker builds a **context pack** (§6) that fits the ~90k window.
- **One LLM call** returns a short *plan* + the full test class (analysis folded in — no separate Analyzer call).
- Candidate written to the module's `src/test/java`; task → `verifying`. **No Gradle here.**

### 4.4 Phase 3 — Batched Verification (serialized per module)
Trigger: **verify when K candidates are ready** for a module (K configurable).
A single build coordinator per module, per batch:
1. `gradle compileTestJava` (one invocation for the batch).
2. **Deterministic auto-fixers** on compile errors (§5); recompile; only unresolved errors escalate to an LLM repair turn.
3. `gradle test` for the batch; parse per-test-class results + stack traces.
4. **JaCoCo** → per-class branch-coverage delta vs baseline.
5. **PIT** — soft signal, scoped to touched classes, once per class at loop end (§7).

Serialized per module (one Gradle process at a time) to avoid contention.

### 4.5 Phase 4 — Feedback Loop (bounded, LLM only when needed)
Budget: **≤5 LLM turns per class.** Auto-fixer passes are free and don't count.
- **Compile failure** → auto-fix; else one LLM repair turn with the exact compiler error.
- **Test failure** → default **pin current behavior** (fix assertion to observed output, feed trace back). If a failure heuristically reveals a real code bug, **flag for human** rather than silently rewrite.
- **Passes but coverage/edge-cases missing** → **targeted** re-prompt for the specific uncovered branches/edge cases (from JaCoCo + the §8 taxonomy) — not "write more tests." Mutant-chasing is **off by default** (PIT is soft); optional `--chase-mutants` for high-risk packages.
- **Static quality gates** (deterministic; reject + re-prompt) enforce the §9 assertion policy.

Exit states: `passed` (compiles, green, coverage rose, passes §9 gates) or `parked` (hit cap; logged for humans, **not committed**).

### 4.6 Phase 5 — Deliver
- **Chunk PRs per module/package** (a single PR of hundreds of test files is unreviewable).
- **PR body = signal**: branch-coverage delta, per-class mutation score (soft), pass rate, parked list.
- **Label tests as characterization tests.**
- Commit only `passed` classes.

---

## 5. Deterministic Auto-Fixers

Keep the LLM turn budget for real reasoning by mechanically resolving common,
boring failures that open models produce frequently:
- **Missing imports** — resolve from classpath / known symbols.
- **Missing mocks** — add `@Mock` fields + `@InjectMocks` for the class under test from the dependency graph.
- **Package declaration** — align with directory.
- **Constructor wiring** — match discovered constructor.
- **Boilerplate** — `@ExtendWith(MockitoExtension.class)`, AssertJ imports, static assertion imports.

Anything not mechanically resolvable escalates to one LLM repair turn with the
precise error attached.

---

## 6. Context Strategy (MiniMax / Qwen, ~90k window)

Output quality on self-hosted open models is dominated by context quality. Pack:
1. **Full source of the target class.**
2. **Signatures (not bodies) of collaborators** to mock (methods, params, returns, thrown exceptions).
3. **One style exemplar** — a real, well-written test from the same repo (highest-leverage item; teaches idioms).
4. **Edge-case checklist** (§8) and **strategy hints** from Phase 1.
5. **Env facts** (Docker/Testcontainers, Spring profiles).

**Chunking**: for very large classes, split generation by method-group and merge
resulting test methods into one test class during verification.

**Prompt shape**: request a brief bulleted **plan** (scenarios incl. edge cases)
then the **complete compilable test class**. The plan improves scenario coverage
without a separate round-trip.

---

## 7. Mutation Testing (PIT) — Why and How

**Why.** Coverage measures *execution*, not *verification*. A test that calls a
method and asserts only non-null yields full coverage but catches no regression —
the exact failure mode to avoid. PIT mutates source (flip `>`→`>=`, alter return
values, remove calls) and reruns tests: a test that **fails** on a mutation
"killed" it (genuinely asserts behavior); a **surviving** mutant means the code
ran but the result was unchecked. Mutation score ≈ "meaningful assertions."

**How (SOFT signal).**
- **Scoped to the touched class** (`targetClasses` = class under test), not the whole module — keeps it affordable.
- **Run once** at the end of a class's loop (fast inner loop = compile + JaCoCo).
- **Reported, never blocking** — attached to the PR as metadata; a class commits on the deterministic gates alone.
- **No mutant-chasing by default** (protects throughput); optional `--chase-mutants` for high-risk packages.

**Cheaper fallback** if PIT is ever too slow: rely on the §9 static gates (catch
crude gaming) — but PIT also catches "asserts the wrong thing," so keep it if
feasible.

---

## 8. Edge & Corner Case Coverage (generation requirement)

For every target method, the generator MUST reason through this taxonomy and
produce tests for each applicable category. The plan step must enumerate which
categories apply and why; verification re-prompts on missing branches.

**Input-space categories**
- **Happy path** — typical valid inputs, expected output asserted precisely.
- **Null / empty** — null arguments, empty collections/strings/maps, `Optional.empty()`.
- **Boundary values** — 0, ±1, `MIN`/`MAX`, off-by-one, empty vs single vs many elements, first/last index.
- **Invalid inputs / validation** — constraint violations (`@Valid`, `@NotNull`, `@Size`), malformed data → expected exception or error response.

**Control-flow categories**
- **Every branch** — each `if/else`, `switch` case (incl. default), ternary, `try/catch/finally`.
- **Loops** — 0, 1, and N iterations; early `break`/`continue`; empty iterable.
- **Short-circuit conditions** — each operand of `&&`/`||` decisive.

**Collaborator / interaction categories**
- **Mock returns** — normal value, empty/`Optional.empty()`, null (if reachable).
- **Mock throws** — each declared/possible exception from a collaborator → verify propagation or handling.
- **Interaction assertions** — `verify(...)` with argument matchers that encode intent (not line-for-line mirroring of the implementation).

**Exception categories**
- **Each thrown/declared exception path** — assert exception **type** and, where it carries meaning, **message/cause/state**.

**Spring-specific categories (where applicable)**
- **Controllers** — status codes, body (JSONAssert), validation errors, exception→HTTP mapping, auth (authorized vs unauthorized/forbidden).
- **Services** — business rules, transactional side-effects (via mocks/slices).
- **Serialization** — request/response JSON round-trips.
- **State transitions** — assert state before vs after a mutating call.
- **Idempotency** — repeated calls produce the expected (same or accumulated) result.

**Determinism guardrails (MANDATORY — reject if violated)**
- No real wall-clock (inject/fix `Clock`, use fixed instants). No `Thread.sleep`.
- No real randomness (seed or mock the source).
- No real network, filesystem, DB, or external process in a unit test (mock; or use a Spring slice + Testcontainers only if env allows and the class truly needs it).
- No reliance on `HashMap`/`HashSet` iteration order.
- Tests must be independent and order-independent.

---

## 9. Meaningful Assertion Policy (enforced by static quality gates)

Static gates run before PIT and **reject + re-prompt** on violations. This is how
LOKI guarantees "no `assert(true)` filler."

**BANNED patterns (auto-reject):**
- Tautologies: `assertTrue(true)`, `assertFalse(false)`, `assert true`, `assertEquals(x, x)`, any assertion whose truth is independent of the code under test.
- **Zero-assertion tests** — a test method with no assertion and no meaningful `verify(...)`.
- **Non-null-only** — a test whose *sole* assertion is `assertNotNull` / `isNotNull`, UNLESS the method's entire contract is non-nullity.
- **Bare "does not throw"** — `assertDoesNotThrow(...)` with no further assertion, UNLESS the contract is specifically "must not throw."
- **Change-detector tests** — assertions that only mirror the implementation's mock calls line-for-line, adding no behavioral guarantee.
- **Commented-out / empty bodies**, `@Disabled` without reason, `fail()`-only stubs.

**REQUIRED for every test:**
- Asserts a **concrete, meaningful outcome**: returned value, thrown exception (type + message/state where meaningful), changed state, or a behavior-relevant interaction with intent-encoding argument matchers.
- **AssertJ** fluent assertions; **JSONAssert** for JSON; assert collection **content** (not just size) where feasible.
- **Arrange-Act-Assert** structure.
- **Descriptive name** — `method_condition_expectedOutcome` and/or `@DisplayName`.
- At least one assertion tied to the specific scenario (edge case) the test claims to cover.

**Verification of the policy:**
- **Static gates** (regex/AST checks) catch the crude violations above pre-PIT.
- **PIT mutation score** (soft) catches the subtle "executes but doesn't check" and "asserts the wrong thing" cases and is surfaced in the PR for human triage.

---

## 10. State & Resumability

- The **durable work queue** (Phase 1) is the single source of truth. Persist to disk (e.g. SQLite or JSON-lines) so the run survives interruption.
- Per-Task state machine: `pending → generating → verifying → passed | failed | parked`.
- On restart: resume `pending`/`generating`/`verifying`; `passed`/`parked` are terminal. No lost or duplicated work.
- Run-level config (K, iteration cap, model, concurrency) recorded for reproducibility.

---

## 11. Agent / Component Roles (mapped to phases)

The original hierarchical swarm sketch is preserved but re-cast so most roles are
**deterministic components**, not LLM agents.

| Role | Nature | Phase |
|---|---|---|
| Orchestrator / Scanner | Deterministic (AST + graph + JaCoCo baseline) | 0–1 |
| Analyzer | Folded into the generation prompt (no separate LLM call) | 2 |
| Test Generator (swarm) | **LLM**, parallel, throughput-bounded | 2 |
| Build Coordinator / Executor | Deterministic, serialized per module | 3 |
| Auto-Fixer | Deterministic (mechanical repairs) | 3–4 |
| Repair Agent | **LLM**, targeted, non-mechanical failures only | 4 |
| Quality Gates ("TVA") | Deterministic (static §9 checks + JaCoCo + PIT-soft) — prunes bad variants | 4 |
| Integrator / Delivery | Deterministic (PR chunking + report) | 5 |
| Dependency Owner | Deterministic, single-owner `build.gradle` edits | 0 |

The original "LLM Reviewer" role is intentionally removed. The original mention
of `pom.xml` was Maven; this framework targets **Gradle** (`build.gradle`).

---

## 12. Framework Repository Layout (proposed)

```
loki/
  pyproject.toml
  README.md
  DESIGN.md
  config/
    loki.example.yaml
  src/loki/
    cli.py                # entrypoint: loki run <repo> ...
    config.py             # load/validate config, secrets via env
    llm/
      client.py           # OpenAI-compatible vLLM client (httpx), retries, rate limit
      prompts.py          # system/user templates (§14)
      parse.py            # extract java code fence + plan, validate single class
    scan/
      ast.py              # class inventory (javalang / tree-sitter / JVM helper)
      graph.py            # dependency graph
      exclude.py          # exclusion rules
      prioritize.py       # coverage-gap × risk ranking
    bootstrap/
      gradle_deps.py      # ensure test + jacoco + pit deps (single owner)
      baseline.py         # gradle test jacocoTestReport, parse per-class coverage
      exemplars.py        # harvest style exemplars
    generate/
      contextpack.py      # build the §6 context pack (+ chunking)
      worker_pool.py      # throughput-aware async pool
      generator.py        # one-call plan+test
    verify/
      coordinator.py      # batched, per-module gradle orchestration
      autofix.py          # §5 deterministic fixers
      jacoco.py           # parse coverage delta
      pit.py              # scoped mutation run + parse (soft)
      gates.py            # §9 static assertion checks (AST/regex)
      edgecheck.py        # §8 coverage-of-categories heuristics
    state/
      model.py            # Task, RunState, reports (§13)
      store.py            # durable persistence (sqlite/jsonl)
    deliver/
      pr.py               # branch + chunked PR creation
      report.py           # coverage/mutation/parked summary
  helpers/java/           # optional small JVM helper for robust AST/JaCoCo/PIT XML parsing
  tests/                  # LOKI's own unit tests + golden-repo fixtures
```

---

## 13. Core Data Models (contracts for code generation)

Indicative fields; a code-generating model should implement these as typed
dataclasses/pydantic models.

- **Task**: `id`, `fqcn` (target class), `module`, `source_path`, `test_path`,
  `collaborators: [ {fqcn, mockable, signatures} ]`, `baseline_branch_cov: float`,
  `current_branch_cov: float`, `strategy_hints: [str]`, `edge_categories: [str]`,
  `state: pending|generating|verifying|passed|failed|parked`, `llm_turns: int`,
  `last_error: str?`, `mutation_score: float?`, `surviving_mutants: [str]`.
- **ContextPack**: `target_source`, `collaborator_signatures`, `exemplar_test`,
  `edge_checklist`, `env_facts`, `token_estimate`.
- **GenerationResult**: `plan: [str]`, `test_source: str`, `raw_response: str`.
- **VerificationResult**: `compiled: bool`, `compile_errors: [str]`,
  `passed_tests: int`, `failed_tests: [ {name, trace} ]`,
  `branch_cov_delta: float`, `gate_violations: [ {rule, detail} ]`.
- **MutationReport**: `class_fqcn`, `killed: int`, `survived: int`,
  `score: float`, `surviving_details: [ {mutator, line} ]`.
- **RunState**: `repo`, `config_snapshot`, `tasks: [Task]`, `started_at`,
  `updated_at`, counts by state.

---

## 14. LLM Prompt Contracts

**System prompt (fixed):** role = "senior Java/Spring test engineer"; MUST emit
JUnit 5 + Mockito + AssertJ; MUST follow the §8 edge-case taxonomy and §9
assertion policy; MUST output exactly one test class; determinism guardrails
apply.

**User prompt (per class):** ordered sections —
1. Target class source (fenced).
2. Collaborator signatures to mock.
3. One style exemplar from this repo.
4. Edge-case checklist (§8) + strategy hints.
5. Env facts (Docker/profiles).
6. Output instructions.

**Required output format (parseable):**
```
PLAN:
- <scenario 1, incl. edge category>
- <scenario 2 ...>

```java
// exactly one complete, compilable test class
```
```
**Parsing rules (`llm/parse.py`):** extract the single ```java fenced block;
verify it declares one top-level test class matching `<Target>Test`; if malformed
or multiple/zero classes → one reformat retry, then mark for repair.

**Repair prompt (Phase 4):** original class + the failing test + the exact
compiler/test error + instruction to fix minimally while preserving covered
scenarios and obeying §9. For coverage/edge re-prompts: include the specific
uncovered branches/lines and the missing §8 categories.

---

## 15. Framework Self-Quality & "Double-Checking"

How LOKI keeps *its own* code correct (the framework must ship with these):
- **LOKI unit tests** for every deterministic component (scanner, graph,
  exclusion, autofix, gates, parsers, coverage/PIT XML parsing).
- **Golden-repo fixtures**: small Spring Boot sample modules with known classes;
  assert LOKI produces compiling, passing, meaningful tests and correct
  coverage deltas end-to-end.
- **Gate self-tests**: feed known-bad tests (`assertTrue(true)`, non-null-only,
  zero-assertion) and assert the §9 gates reject them; feed known-good tests and
  assert they pass. This is the guard that "no meaningless test slips through."
- **Dry-run mode** (`--dry-run`): scan + plan + generate, write candidates, run
  gates, but do not open PRs — for inspection.
- **Idempotency / no-clobber**: never overwrite an existing hand-written test;
  never let a worker edit `build.gradle`; assert both in tests.
- **Determinism check**: run a generated test twice; flag if results differ
  (catches accidental non-determinism before it reaches a PR).

---

## 16. Configuration (indicative)

```yaml
llm:
  base_url: https://myvllm.com/v1     # OpenAI-compatible
  api_key_env: LOKI_LLM_TOKEN       # bearer token via env / secret store
  model: minimax                      # or qwen-*
  max_context_tokens: 90000
concurrency:
  worker_pool_size: 0                 # 0 = auto from benchmark
  requests_per_second: 0              # 0 = auto from benchmark
verification:
  candidates_per_batch_K: 8           # verify when K ready
  max_llm_turns_per_class: 5
quality:
  pit_enabled: true                   # soft signal
  chase_mutants: false                # optional per high-risk package
  target_branch_coverage: 0.90
  min_mutation_score_report: 0.0      # reported, not enforced
exclusions:
  - "**/config/**"
  - "**/dto/**"
  - "**/*MapperImpl.java"
  - "**/generated/**"
delivery:
  pr_chunking: per-module             # or per-package
  label: characterization-tests
```

---

## 17. Design Self-Consistency Checklist

(Reviewed for internal contradictions; a code-gen model should keep these true.)
- [x] Build tool is **Gradle** everywhere (no `pom.xml`/Maven references).
- [x] LLM is used **only** in generation + repair; all gates are deterministic.
- [x] Generation is parallel; verification is serialized per module.
- [x] Concurrency is endpoint-bounded (auto from benchmark), not a fixed number.
- [x] PIT is **soft** — reported, never blocks a commit; no default mutant-chasing.
- [x] Tests are **characterization** tests and labeled as such.
- [x] Iteration cap counts **LLM turns only**; auto-fixers are free.
- [x] Workers never edit `build.gradle`; only Phase-0 owner does.
- [x] Every test must satisfy §8 (edge coverage) and §9 (meaningful assertions).
- [x] The run is resumable via the durable work queue.

---

## 18. Open / Tunable Items

- **vLLM throughput benchmark** — sets `worker_pool_size` / `requests_per_second`. First implementation task.
- **K** (candidates per verification batch) — feedback latency vs. Gradle overhead.
- **Iteration cap** — currently 5 LLM turns/class; revisit after observing how many turns MiniMax/Qwen need to reach *compiles*.
- **PIT scope/threshold** for the soft report.
- **Bug-vs-test heuristic** — confidence that a failure reveals a real code bug (→ human flag) vs. a wrong assertion (→ pin behavior).

---

## 19. First Implementation Slices

1. **vLLM client + config** (OpenAI-compatible; URL/bearer/model) + **throughput benchmark** to set concurrency.
2. **Phase 1 deterministic scanner** (AST inventory + dependency graph + exclusions) writing the durable work queue.
3. **Phase 0 bootstrap** (module detection, dependency injection into `build.gradle`, JaCoCo baseline, exemplar harvest).
4. **§9 quality gates + gate self-tests** (so "no meaningless test" is guaranteed from day one).

(Implementation has not started; this document is the agreed specification.)
