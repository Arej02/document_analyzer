# Agent 3 (Execute) — Specification

Functional and non-functional spec for Agent 3. The *how* (tables, stages, sandbox, API shapes) lives in `design_agent3.md`; the build order lives in `tasks_agent3.md`. Section references (§N) in this file point into this file unless prefixed with a file name.

## 1. Overview

Agent 3 is the last stage of the pipeline. It takes one **approved** Agent 2 migration plan and turns it into **generated FastAPI code plus a test verdict per migration unit**.

Agent 3 owns three outputs:

1. **Generated code per unit.** Python modules that replace the unit's legacy PHP members, written in plan order so every unit can build on the units it depends on.
2. **Tests and verdicts per unit.** A pytest suite derived from the unit's test contract (`design_agent2.md` §10), run in an isolated sandbox, with a `passed` / `failed_verification` / `error` verdict.
3. **An assembled project.** One runnable FastAPI project combining every unit, with its whole test suite run once more, downloadable as a zip.

Method: for each unit, give an LLM the unit's legacy source, facts and test contract plus the interfaces of the units it depends on, and ask it to generate code. Generate tests from the contract, deterministically where possible and with the LLM where not. Run the tests in a network-isolated container and feed failures back for a bounded number of retries.

### Glossary

| Term | Meaning |
|---|---|
| **Plan item** | One `migration_plan_items` row of the approved plan (Agent 2). One unit, or a merged group of units, in `(phase_order, unit_order)` order. |
| **Execution job** | One asynchronous run of the Agent 3 pipeline against one approved plan. |
| **Unit execution** | One plan item's generation and verification inside an execution job. |
| **Source snapshot** | The boilerplate-stripped legacy source for one Agent 1 ingestion job, archived by Agent 1 (`design_agent3.md` §13). |
| **Contract tests** | Tests generated deterministically from the test contract: interface shape, dependency wiring, hook registration, external call routing. |
| **Behaviour tests** | Tests the LLM writes from the legacy source to check what the code does. Less trustworthy than contract tests, and reported separately. |
| **Symbol map** | Per unit, legacy entity id → the Python name that replaces it. How downstream units import upstream code. |
| **Sandbox** | The locked-down container that runs generated code and tests (§6 NFR2). |

## 2. Pipeline position & trigger

- Agent 3 is independent of Agents 1 and 2. It communicates **only** through Supabase: it reads their tables and never imports or invokes their pipelines. Importing *pure* helpers is allowed (CLAUDE.md).
- **Trigger (decided): user-initiated.** A user clicks "Generate code" on an approved plan. The API enqueues an execution job on Agent 3's own pgmq queue (`agent3_execution_jobs`). There is no automatic trigger on plan approval.
- **Gate:** the plan must have `status = approved` and must not be stale (`design_agent2.md` §9.1). Agent 3 never reads drafts, in-review, rejected or stale plans as instructions.
- Only one queued or running execution job per plan at a time.

## 3. Inputs (read-only)

| Source | What Agent 3 uses |
|---|---|
| `migration_plans` (Agent 2) | `id`, `project_id`, `source_job_id`, `version`, `status` (must be `approved`). |
| `migration_plan_items` (Agent 2) | `cluster_id`, `phase_cluster_id`, `phase_order`, `unit_order`, `test_contract`. |
| `clusters`, `cluster_members`, `cluster_reports` (Agent 2) | Unit label, member entity ids, report text and `risk_level`, used as narrative context only. |
| `extracted_entities`, `entity_edges` (Agent 1, pinned by `source_job_id`) | Member `raw_facts` (signatures, class, `calls`, line spans), `file_path`, `entity_type`. |
| `knowledge_summaries` (Agent 1) | Entity summaries with `grounding_status`. `unverified` summaries are left out of prompts (FR3). |
| Source snapshot (Agent 1 storage) | Legacy source text of each member, sliced by line span. |

Agent 3 **never writes** Agent 1 or Agent 2 tables. Gaps in their data are worked around here and fixed in their own task files (`design_agent3.md` §13).

## 4. Outputs

Written by Agent 3's worker, all scoped by `org_id` and an `execution_job_id`:

- `execution_jobs`: status, stage, counts, pointer to the assembled zip.
- `unit_executions`: one row per plan item, with status, attempts, test results, symbol map and storage location.
- Files in the `agent3-output` storage bucket: generated code, generated tests and test logs per unit per attempt, plus the assembled project zip.

## 5. Functional requirements

### FR1 — Execution job lifecycle
- `POST` creates an `execution_jobs` row (`queued`) plus one `unit_executions` row per plan item (`pending`), then enqueues. The worker moves the job through `running` with a `progress_stage` of `prepare → units → assemble → final_test`, ending in `done` or `failed` (with `error`).
- `done` means every unit reached a terminal verdict, not that every unit passed. The job's counts say how many passed.
- `409` if the plan isn't `approved`, is stale, or already has an active execution job. `409` if the plan's source job has no source snapshot (ingested before Agent 1 archived snapshots). The message tells the user to re-ingest.
- A redelivered queue message for a terminal job is skipped. For a non-terminal job, finished units are kept and only non-terminal units re-run.

### FR2 — Order
- Units are processed in `(phase_order, unit_order)` order, the plan's callee-first order. When a unit is generated, every unit it depends on (`test_contract.outgoing_dependencies`) already has a symbol map.
- MVP processes units one at a time. Parallelism within a phase, for units that don't depend on each other, is a later optimisation (§9).

### FR3 — Unit context
- Context for one unit: member source slices, member `raw_facts`, the test contract, the unit's cluster report text, the symbol maps and public signatures of the units it depends on, and the target architecture rules (`design_agent3.md` §8).
- Summaries marked `unverified` in `test_contract.unverified_member_ids` are **not** put in the prompt. The legacy source and facts are used instead, and the unit is flagged `unverified_inputs`.
- The payload is bounded (`AGENT3_MAX_CONTEXT_CHARS`). A unit whose source doesn't fit fails with `error` ("unit too large to generate") and doesn't block the job.

### FR4 — Code generation
- The LLM returns structured output: a list of files (path + content) under the unit's own package, plus a symbol map covering every `public_interface` entry.
- Paths outside the unit's package are rejected (no writing into another unit or into shared code). Shared compatibility code (`wp_compat`, the event dispatcher) is generated once by the assemble stage from a fixed template, never by a unit.
- Output that is missing a `public_interface` symbol, or doesn't parse as Python, counts as a failed attempt.

### FR5 — Test generation
- **Contract tests** are generated deterministically from the test contract, without the LLM (mapping in `design_agent3.md` §6). Same contract in, same tests out.
- **Behaviour tests** are written by the LLM from the legacy source and the generated code. They're tagged separately so the review UI can show "contract passed, behaviour failed".
- Tests never make network calls. External calls go to `wp_compat`, which is an in-memory fake under test.

### FR6 — Sandboxed verification
- Tests run only inside the sandbox (NFR2). The generated code and tests are the only things mounted into it. The worker never imports or executes generated code in its own process.
- A unit's tests run against its own code, the code of the units it depends on, and the shared template code.

### FR7 — Retry and verdict
- A failed attempt (generation error, parse error, missing symbol, failing test) feeds the failure back into the next generation call, up to `AGENT3_MAX_ATTEMPTS` (default 3).
- Verdicts:
  - `passed`: all contract and behaviour tests pass.
  - `failed_verification`: the attempts cap was reached with tests still failing. The last attempt's code and logs are kept.
  - `error`: no usable code could be produced at all.
- **Non-blocking (decided):** a unit that fails never stops the job. Its dependents build against its symbol map. For an `error` unit with no code, the dependents build against stubs generated from the contract, which raise `NotImplementedError`, and they're marked `built_on_failed_dependency`.

### FR8 — Assembly
- After every unit is terminal, the assemble stage combines the template (app entry, `wp_compat`, event dispatcher, models package) with every unit's last attempt into one project, adds a `README` listing each unit's verdict, and runs the full test suite once in the sandbox.
- The result is zipped to storage and linked from `execution_jobs`.

### FR9 — Review & download
- Users can list the execution jobs of a plan, poll a job's status, see verdicts per unit grouped by phase, open a unit to see legacy source and generated code side by side with its tests and test log, and download the assembled zip.
- Download uses a short-lived signed URL. Nothing is public.

## 6. Non-functional requirements

- **NFR1 — Async.** All work runs on the `agent3_execution_jobs` queue and a separate worker process (`document-analyzer-executor`). The API only enqueues and reads.
- **NFR2 — Sandbox (non-negotiable).** Generated code comes from untrusted input, so it is treated as untrusted. Every sandbox run has:
  - no network;
  - a read-only root filesystem, with tmpfs scratch space;
  - no Linux capabilities and no privilege escalation;
  - a non-root user;
  - CPU, memory and process-count limits;
  - a wall-clock timeout.

  The gVisor runtime is used wherever it's available and is required in production (`design_agent3.md` §7). Never relax this for convenience.
- **NFR3 — Multi-tenancy.** Every Agent 3 table has `org_id` with RLS (`org_id = current_org_id()`). The worker writes through the service-role connection, stamps `org_id` from the execution job row (never from repo content or LLM output), and filters updates by `id` and `org_id`. Storage paths start with `{org_id}/`.
- **NFR4 — Cost control.** Generation calls are bounded: the payload cap, the attempts cap, and one unit at a time for the MVP. Params are frozen per job.
- **NFR5 — Auditability.** Every attempt's prompt inputs (by reference), generated files, tests and logs are kept. A job records the model deployment and params it ran with.
- **NFR6 — Determinism where possible.** Contract tests and the assembled layout are deterministic. LLM output isn't. Re-running an execution job may give different code, and that is expected and recorded.

## 7. Out of scope (MVP)

- **Executing legacy PHP**, and differential testing (running PHP and Python side by side). This was decided 2026-09-25. The design keeps it possible later (`design_agent3.md` §14), but there's no PHP sandbox in the MVP.
- Opening GitHub pull requests or pushing to any repo. Output is Storage + download only (decided 2026-09-25).
- Database data migration (moving rows from the WordPress DB). Agent 3 generates models, not data transfers.
- Editing generated code in the UI, and re-running a single unit. A per-unit retry endpoint is a later task (§9).
- Writing to any Agent 1 or Agent 2 table.

## 8. Decided

- Trigger: user-initiated `POST`, own queue `agent3_execution_jobs`, approved and non-stale plans only.
- Verification: contract tests (deterministic) plus behaviour tests (LLM), generated Python only, in a sandbox. No legacy execution.
- Output: Supabase Storage bucket `agent3-output` and a zip download.
- Failures never block: `failed_verification` / `error` units are kept and flagged, and dependents continue.
- Order: plan order, one unit at a time.
- Legacy source: Agent 1 archives a source snapshot per ingestion job (new Agent 1 tasks in `tasks.md`).

## 9. Open questions

- ~~Default `AGENT3_MAX_ATTEMPTS` (3) and the sandbox timeout (120 s). Tune on the first real WordPress plan.~~ **Settled 2026-09-29** (`design_agent3.md` §11 "Load run findings", a 12-unit synthetic run — real `php` wasn't available to run a true WordPress plan through Agent 1/2, so this is a lower-confidence stand-in, worth re-checking once a real plan is available): both defaults hold. Max attempts actually used was 2 of 3; the whole assembled suite finished in 11.2 s against a 600 s (5×) timeout. No evidence either needs raising for functions of this size/complexity.
- ~~Whether to run independent units in the same phase in parallel. Revisit after measuring per-unit time.~~ **Settled (recommendation) 2026-09-29**: yes, worth adding. Per-unit time (13–44 s) is dominated by LLM latency (77% of units-stage wall time) and Docker container startup, both independently parallelizable; the sandbox's own per-container resource caps (§7 memory/cpus/pids-limit) already bound worst-case load regardless of concurrency, so a bounded concurrency cap (Agent 1's `summarize_fanout` pattern) would cut a large phase's wall time roughly in proportion to the cap. Not implemented — a follow-up task, since `run_units_stage`'s current one-at-a-time loop would need a bounded `ThreadPoolExecutor` (or similar) per phase, plus race-free `state.symbol_maps`/`state.unit_files` writes from concurrent units.
- How far the fixed `wp_compat` template should go (options, post meta, HTTP, `$wpdb`) before units need to generate their own adapters.
- A per-unit "regenerate" action for reviewers, and whether regenerating should invalidate the units that depend on it.
- Whether behaviour-test failures alone should give a separate verdict (for example `passed_contract_only`) rather than `failed_verification`.
