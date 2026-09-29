# tasks_aamf.md — Improvements adopted from AAMF

Ideas taken from [AAMF](https://github.com/jafreck/AAMF) (Autonomous Agent Migration Framework), compared against our specs and designs on 2026-09-29. The comparison used AAMF's README, not its source code.

This file is a **backlog of candidates**, not approved design. The CLAUDE.md rules apply to every item:
- Update the owning design doc (`design.md`, `design_agent2.md` or `design_agent3.md`) **before** writing code.
- Then move the item into that agent's tasks file (`tasks.md`, `tasks_agent2.md` or `tasks_agent3.md`) and check it off *there*. This file only tracks the decision.
- Every new or changed endpoint gets a paired frontend task.
- Every new table gets `org_id` + RLS. The worker stamps `org_id` from the job row.
- Nothing here relaxes the sandbox or untrusted-input rules.

Each item has three checkboxes: **designed** (design doc updated), **tasked** (backend + frontend tasks added to the agent's tasks file) and **done** (the tasks there are complete).

### What we already match or do better (no action)
- Deterministic, graph-based task ordering. Ours adds seeded Leiden, SCC merges, a hub foundation unit and atomic classes.
- Human approval: per-phase approval, versioned plans, overrides and staleness. AAMF has no equivalent.
- Hardened sandbox (gVisor in production), multi-tenancy with RLS, and secret-redacted evidence.
- Deterministic contract tests from the test contract (`design_agent3.md` §6).
- Grounding checks on summaries, and unit-level resume on queue redelivery.

### Not adopting
- **Agents with filesystem/CLI access** (AAMF runs Copilot / Claude Code as the agent runtime). This conflicts with our untrusted-input and sandbox rules. Tool access stays read-only and database-scoped (A1).
- **Local JSON checkpoint files.** Supabase job and unit rows are already our checkpoint.
- **"Parity" judged mainly by an LLM.** AAMF can lean on a compiler and the legacy project's test suite. PHP → FastAPI has no such oracle, so the strong parity signal is differential testing (`design_agent3.md` §14, item E1). An LLM verifier is advisory only.

---

## Phase A — Better verdicts (Agent 3)

### A1. Tool-based context retrieval for generation
Today a unit's whole context is packed into one prompt capped at `AGENT3_MAX_CONTEXT_CHARS` (FR3). A unit over the cap ends in `error`, and the sparse call graph (`design_agent3.md` §13) hides symbols a unit needs. AAMF gives its agents paths plus an MCP server over the knowledge base instead.
- [ ] Designed (`design_agent3.md` §4 and §8: tool list, caps, fencing)
- [ ] Tasked (`tasks_agent3.md`)
- [ ] Done
- Scope:
  - Read-only tool calls during generation: `search_source`, `get_entity`, `get_symbol_map(unit)`, `list_exported_symbols`.
  - Reuse the retrieval in `ask/context.py` (semantic + symbol search, two-hop expansion). If it needs Agent 3-specific changes, move it into `shared/` first.
  - Every tool is scoped to the execution job's `org_id` and `source_job_id`. Tool results are fenced as untrusted data.
  - Cap tool calls per attempt and total tool-result characters. Record both in `params`.
  - Keep a small fixed base context (contract, member signatures, §8 rules). The rest is fetched on demand.

### A2. Failure classification after retries run out
AAMF's `parity-failure-resolver` labels an exhausted failure as fixable, a false positive (the test is wrong), a real gap, or inconclusive. For us this matters more because behaviour tests are LLM-written. It also answers the open `passed_contract_only` question (specs §9).
- [ ] Designed (`design_agent3.md` §2 column + §4 step, `specs_agent3.md` FR7)
- [ ] Tasked
- [ ] Done
- Scope:
  - One structured LLM call per `failed_verification` unit, given the legacy slices, the generated code, the tests and the failure output.
  - New column `unit_executions.failure_class` (`fixable`, `test_wrong`, `real_gap`, `inconclusive`) plus a short `failure_reason`.
  - Decide whether "contract passed, only behaviour failed" + `test_wrong` becomes its own verdict.
  - Frontend: show the class and reason on `ExecutionUnitPage` and as a pill on `ExecutionResultsPage`.

### A3. Fix loop after `final_test`
Today a failing assembled suite is only reported (`design_agent3.md` §5). AAMF's final parity sweep sends failures back for targeted fixes.
- [ ] Designed (`design_agent3.md` §5, new params `max_fix_rounds`)
- [ ] Tasked
- [ ] Done
- Scope:
  - Map failing tests in the assembled run back to their `unit_slug` (by the JUnit `classname` path).
  - Re-generate only those units, with the integration failure as feedback. Store the attempts like any other attempt.
  - Re-assemble and re-run, up to `max_fix_rounds` (default 2). Keep the last round's results.
  - Frontend: show the fix rounds on the execution status page (a `fix` stage in `ProgressStages`).

---

## Phase B — Throughput and success rate (Agent 3)

### B1. Waves with a test barrier between phases
Units in the same phase with no dependency between them can run in parallel (open question in specs §9). A barrier run between phases catches cross-unit breakage early instead of only at `final_test`.
- [ ] Designed (`design_agent3.md` §4, `specs_agent3.md` FR2)
- [ ] Tasked
- [ ] Done
- Scope:
  - Within a phase, compute the frontier of units whose dependencies are all terminal. Run them through a bounded pool (`AGENT3_UNIT_CONCURRENCY`, same pattern as `summarize_fanout.py`). Supabase writes stay on the worker thread.
  - After each phase, assemble everything built so far and run the whole suite in the sandbox (the barrier).
  - A barrier failure triggers a targeted fix pass (the A3 mechanism) before the next phase starts, capped at `max_convergence_iterations`.
  - Frontend: per-phase barrier status on `ExecutionResultsPage`.

### B2. Model escalation by difficulty
AAMF uses a normal tier, a heavy tier for complex tasks, and a critical tier on retries, with a capped dollar budget for escalation.
- [ ] Designed (`design_agent3.md` §11 config, §4 generate step)
- [ ] Tasked
- [ ] Done
- Scope:
  - Add `AGENT3_MODEL_DEPLOYMENT_HEAVY`, frozen in `params`.
  - Escalate from attempt ≥ 2, or when the unit's `risk_level` is high, or when it's a recorded oversized unit.
  - Record the deployment used on each attempt (it feeds C1).
  - Cap escalation spend per job (depends on C1).

### B3. Chunked generation for oversized units
AAMF splits oversized cycles into stubs plus sequential chunks. We record `oversized_units` in Agent 2 and then fail them as "unit too large" in Agent 3.
- [ ] Designed (`design_agent3.md` §4)
- [ ] Tasked
- [ ] Done
- Scope:
  - Generate the unit's interface stubs first. Reuse the stub generator for failed dependencies (`design_agent3.md` §4).
  - Fill in the method bodies in chunks against those stubs, each chunk under the context cap.
  - Run the contract tests once all chunks are in. Flag the unit `chunked`, which means adding the value to the flags check constraint.

---

## Phase C — Cost, reliability and observability (all agents)

### C1. LLM call accounting
No agent records token usage or cost today.
- [ ] Designed (new table in `design.md`, referenced from `design_agent2.md` / `design_agent3.md`)
- [ ] Tasked (`tasks.md`, `tasks_agent2.md`, `tasks_agent3.md`)
- [ ] Done
- Scope:
  - Table `llm_invocations`: `org_id`, `agent` (1/2/3), `job_id`, `stage`, `unit_id` (nullable), `deployment`, `prompt_tokens`, `completion_tokens`, `cached_tokens`, `latency_ms`, `attempt`, `outcome`, `prompt_sha256`, `created_at`. RLS `select` on `org_id`; service-role writes only.
  - One wrapper around the Azure OpenAI client, used by Agent 1 summaries, Agent 2 cluster reports, `ask/`, and Agent 3 generation and tests.
  - Cost per model from a config table. Unknown models are logged with a warning, not silently priced at zero.
  - `prompt_sha256` covers NFR5 auditability without storing prompts twice.
  - Frontend: token and cost totals on each job's status page.

### C2. Per-job token budget with pause
- [ ] Designed
- [ ] Tasked
- [ ] Done
- Scope:
  - A per-job budget, frozen in `params`. Warn at 80%. At 100%, stop at the next unit boundary with a `paused` status.
  - Resume reuses the existing redelivery resume: terminal units are kept, the rest re-run.
  - Frontend: a budget meter, a paused state and a "Resume" action (needs a new endpoint, so a design change first).

### C3. Pre-run cost and time estimate
AAMF writes an `impact-assessment.md` before it runs.
- [ ] Designed
- [ ] Tasked
- [ ] Done
- Scope:
  - Estimate an execution job's tokens, cost and wall time from the unit count, context sizes and `max_attempts`, calibrated with C1 history.
  - Frontend: show it next to the "Generate code" action on an approved plan.

### C4. Infrastructure retries separate from generation attempts
Today one `SandboxUnavailableError` fails the whole execution job, even hours into a run. AAMF retries infrastructure errors without using up the agent's attempts.
- [ ] Designed (`design_agent3.md` §7, §10, §11)
- [ ] Tasked
- [ ] Done
- Scope:
  - Retry transient Docker, storage and DB errors with backoff (`AGENT3_MAX_INFRA_RETRIES`, default 3). They don't count toward `max_attempts`.
  - Persistent failures (image missing, engine isn't the configured one, gVisor required but absent) still fail the job immediately.
  - Apply the same rule to the Agent 1 and Agent 2 workers where it fits.

### C5. Graceful shutdown on SIGTERM/SIGINT
- [ ] Check what each worker (`document-analyzer-worker`, `-planner`, `-executor`) does on SIGTERM today
- [ ] Designed
- [ ] Done
- Scope:
  - Finish or abandon the current unit or entity cleanly and persist state.
  - Kill and remove any running sandbox container.
  - Leave the queue message so the job is redelivered and resumes.

### C6. Execution timeline and observability report
AAMF produces a Gantt timeline, concurrency over time, cost per agent and a record of each retry chain.
- [ ] Designed (build on `shared/workflow_events.py` and `20260928030000_workflow_events.sql`)
- [ ] Tasked
- [ ] Done
- Scope:
  - Emit start and end events per stage, unit and attempt.
  - Endpoint returning a job's timeline plus C1 cost rollups per stage and unit.
  - Frontend: a timeline chart, a retry-chain view per unit, and a cost breakdown.

### C7. Versioned prompt catalog
AAMF compiles its prompts once from Markdown templates into a fixed catalog.
- [ ] Designed
- [ ] Done
- Scope:
  - Move every LLM prompt (summaries, grounding, cluster reports, ask, generation, behaviour tests) into versioned template files.
  - Freeze the catalog version (or hash) into each job's `params`, so a run records exactly which prompts it used.

---

## Phase D — Output quality (Agent 3, after Phases A–C)

### D1. Idiomatic refactor pass
Translating through `wp_compat` produces WordPress-shaped Python. AAMF has an optional review → plan → refactor loop.
- [ ] Designed
- [ ] Tasked
- [ ] Done
- Scope: an optional stage after `assemble`. Changes are kept only if the whole suite still passes. Capped at `max_refactor_iterations` and off by default.

### D2. End-to-end tests over the assembled app
- [ ] Designed
- [ ] Tasked
- [ ] Done
- Scope:
  - Generate tests that drive REST routes, shortcodes and admin-ajax handlers through FastAPI's `TestClient`, run in the sandbox.
  - Report them as a separate `e2e` suite in `final_test_summary`.

### D3. Migration guide and docs in the output
- [ ] Designed
- [ ] Done
- Scope: a per-unit migration note (legacy → new mapping, `TODO(wp_compat)` items, flags) plus a project-level guide in the zip. This goes beyond the verdict-table README.

### D4. Git history inside the zip
- [ ] Designed
- [ ] Done
- Scope: the assembled project is a git repo with one commit per unit, in plan order. Pushing to remote repos stays out of scope (specs §7).

### D5. Competing candidates for high-risk units
- [ ] Designed
- [ ] Tasked
- [ ] Done
- Scope: for high-risk units only, generate two candidates and pick one by test results, then a judge call. Expensive, so behind a flag and depends on C1/C2.

---

## Phase E — Long-term parity

### E1. Differential testing against legacy PHP
This is already sketched in `design_agent3.md` §14. It's the only strong parity signal for PHP → FastAPI, and it's the real counterpart of AAMF's parity checks.
- [ ] Designed (separate design section: PHP sandbox with a WordPress core stub + DB, recorded I/O per `public_interface`)
- [ ] Tasked
- [ ] Done
- Scope:
  - A separate, network-isolated, resource-limited PHP runner. It never shares the Python runner.
  - Record legacy inputs and outputs, then replay them against the generated Python as a `differential` suite in `test_summary`.

---

## Suggested order
1. C1 + C4: cheap, and they help immediately.
2. A1, A2, A3: better verdicts.
3. B1, B2, B3: speed and success rate.
4. C2, C3, C5, C6, C7, then Phase D.
5. E1 as the long-term parity goal.
