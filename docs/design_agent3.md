# Agent 3 (Execute) — Technical Design

Architecture detail supporting `specs_agent3.md` (referenced as *specs FRn*). Like `design.md` and `design_agent2.md`, this is the doc to update whenever a design decision changes: code follows this file, not the other way around. Section references (§N) point into this file unless prefixed.

Status: **specified, not yet built** (2026-09-25).

## 1. High-level flow

```
User clicks "Generate code" on an approved plan
        │
        ▼
 [FastAPI: POST /api/agent3/plans/{plan_id}/executions]
        │  gate: plan approved ∧ not stale ∧ source snapshot exists ∧ no active execution (409 otherwise)
        ├─► execution_jobs row (queued) + one unit_executions row per plan item (pending)
        └─► pgmq.send('agent3_execution_jobs', {execution_job_id, org_id})
        │
        ▼
 [Agent 3 worker: `uv run document-analyzer-executor`]
        │
        ├─ stage prepare    ─► load plan items, contracts, members, snapshot → in-memory ExecutionState
        ├─ stage units      ─► for each item in (phase_order, unit_order):
        │                        context → generate → contract + behaviour tests → sandbox run
        │                        → retry ≤ N → persist files + verdict + symbol map
        ├─ stage assemble   ─► template + every unit's last attempt → project tree
        └─ stage final_test ─► full test suite in sandbox → zip → storage
        │
        ▼
 execution_jobs.status = done | failed
        │
        ▼
 Frontend: verdicts by phase → unit detail (legacy vs generated, tests, log) → download zip
```

**Orchestration:** same pattern as Agents 1 and 2. A single long-lived worker process polls its own pgmq queue and runs stage functions in order, handing in-memory results between stages in an `ExecutionState` (the Agent 2 `PlanningState` pattern, `design_agent2.md` §14). No LangGraph.

**Code location:** `backend/document_analyzer/agent3/`:
- `router.py`: the API.
- `worker/run.py`: the loop and stages.
- `worker/context.py`, `worker/generate.py`, `worker/tests_gen.py`, `worker/sandbox.py`, `worker/assemble.py`: one module per step.
- `template/`: the fixed shared project code.

The script entry is `document-analyzer-executor` under `[project.scripts]`.

## 2. Data model (Supabase Postgres)

Rules shared by every table below (same as `design_agent2.md` §2):
- Columns `org_id uuid not null`, `created_at`, `updated_at`.
- RLS enabled, with a `select` policy `org_id = current_org_id()`. **Writes are service-role only.** No Agent 3 table is user-writable in the MVP.
- The worker writes with `insert_scoped` / `insert_many_scoped` (`supabase_client.py`), stamping `org_id` from the **execution job row**. Every update filters on `id` and `org_id`.
- One migration file per table group: `supabase/migrations/2026MMDD..._agent3_*.sql`.

### `execution_jobs`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | returned to the frontend on submit |
| `project_id` | uuid, FK → `projects` | denormalized for listing and project delete |
| `plan_id` | uuid, FK → `migration_plans` | must be `approved` at creation |
| `source_job_id` | uuid, FK → `ingestion_jobs` | copied from the plan: the snapshot this run reads |
| `status` | `job_status` (`queued`,`running`,`done`,`failed`) | reuses Agent 1's enum |
| `progress_stage` | `execution_stage` (`prepare`,`units`,`assemble`,`final_test`), nullable | |
| `error` | text, nullable | job-level failure only; unit failures live on `unit_executions` |
| `params` | jsonb | `{model_deployment, max_attempts, max_context_chars, sandbox_timeout_seconds, sandbox_memory_mb, runner_image}`, frozen from §11 at creation |
| `counts` | jsonb, nullable | `{total, done, passed, failed_verification, error, built_on_failed_dependency}` — `done` included (matches `status.py::counts_by_job`'s actual shape and what `GET /executions/{id}` already returns; this column previously documented `done` as omitted, corrected 2026-09-28) |
| `final_test_summary` | jsonb, nullable | `{passed, failed, errors, duration_s}` from the assembled run |
| `output_zip_path` | text, nullable | storage path of the assembled zip |

Partial unique index `execution_jobs_one_active_per_plan` on `(plan_id)` where `status in ('queued','running')`.

### `unit_executions`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | |
| `execution_job_id` | uuid, FK → `execution_jobs` on delete cascade | |
| `cluster_id` | uuid, FK → `clusters` | the plan item's key (`migration_plan_items` PK is `(plan_id, cluster_id)`) |
| `phase_cluster_id` | uuid, FK → `clusters` | copied from the plan item |
| `phase_order`, `unit_order` | int | copied from the plan item |
| `status` | `unit_execution_status` (`pending`,`running`,`passed`,`failed_verification`,`error`) | |
| `attempts` | int, default 0 | check `>= 0` |
| `flags` | text[], default `{}` | `unverified_inputs`, `built_on_failed_dependency`, `stubbed`. A check constraint allows only these values. |
| `symbol_map` | jsonb, nullable | `{entity_id: {python_module, python_name, kind}}` for every `public_interface` entry |
| `test_summary` | jsonb, nullable | `{contract: {passed, failed}, behaviour: {passed, failed}, duration_s}` for the last attempt |
| `last_error` | text, nullable | truncated failure reason for the last attempt |
| `storage_prefix` | text, nullable | `{org_id}/{execution_job_id}/units/{cluster_id}/` |

Unique `(execution_job_id, cluster_id)`.

Nothing cascades from Agent 1 or Agent 2 rows (plans are immutable snapshots). Only `unit_executions` cascades with its `execution_jobs` row. Migration: `20260925100000_agent3_execution_tables.sql`.

### Queue
`agent3_execution_jobs`, created with `pgmq.create` in its own migration. Its helpers go in `queue.py` next to Agent 1's and 2's (`enqueue_execution_job`, `read_next_execution_job`, `delete_execution_job_message`), with the explicit `$2::bigint` cast (`design.md` §3 bug #2). It is never shared.

### Storage: bucket `agent3-output`
Private. The object layout:

```
{org_id}/{execution_job_id}/units/{cluster_id}/attempt_{n}/code/...
{org_id}/{execution_job_id}/units/{cluster_id}/attempt_{n}/tests/...
{org_id}/{execution_job_id}/units/{cluster_id}/attempt_{n}/log.txt
{org_id}/{execution_job_id}/units/{cluster_id}/attempt_{n}/junit.xml
{org_id}/{execution_job_id}/project.zip
```

The `select` policy uses `(storage.foldername(name))[1] = current_org_id()::text`, the same pattern as `agent1-uploads`. There's no authenticated insert policy: only the worker writes.

## 3. Stage `prepare`

1. Load the execution job, its plan (re-check `approved`; if the plan went stale after enqueue, fail the job with a readable error), and the plan items in `(phase_order, unit_order)` order.
2. For each item, collect its member entity ids:
   - from `cluster_members` of `cluster_id`;
   - for an override merge, from every id in `test_contract.merged_cluster_ids`;
   - for a `merged_scc` unit, the members are already re-pointed (`design_agent2.md` §7).
3. Load the members' `extracted_entities` rows (pinned to `source_job_id`, `org_id`-scoped) and their entity summaries.
4. Download and unpack the source snapshot into a temp dir. It's read-only input, never executed.
5. Resume (`_load_resume_state`): any `unit_executions` row already terminal (a redelivered message) is skipped by stage `units`, and its **symbol map, last-attempt code/tests and `stubbed` membership** are all reconstructed into the in-memory `ExecutionState`, not just the symbol map. A `passed`/`failed_verification` unit (has a `storage_prefix`) has its last attempt read back from `agent3-output` (`units.py::download_attempt_files`); an `error` unit with no `storage_prefix` (nothing was ever uploaded for it) has its deterministic stub regenerated instead (`generate_stub_unit` is pure, so this reproduces exactly what `_finish_as_error` would have stored). Without this (fixed 2026-09-29, `tasks_agent3.md` Phase 6 "Redelivery"), a dependent of a unit finished in an *earlier* run of the same job would silently get built against a fresh stub instead of that unit's real result, and `assemble` (§5) would raise on *any* resumed job at all, since it requires `state.unit_files` for every plan item.
6. **`unit_slug` (§8)**, added 2026-09-28: `unit_slug.py::derive_unit_slug(item, state)` computes and attaches `item["unit_slug"]` for every item, once, right after `report_source_cluster_ids`. It needs a label, and `clusters.label` isn't on any row `prepare` fetched otherwise, so this step also loads `state.clusters` (`id -> {kind, label}`) for `{item.cluster_id} ∪ report_source_cluster_ids` — the same rows `agent2/router.py::_unit_fields` reads. The label itself mirrors that function's rule exactly (own label when not merged; a `merged_scc` unit's own label or else its originals' sorted-and-joined labels; an override merge always joins), so a plan item gets the same slug whichever agent looks at it. A cluster with no label at all (for example grounding failed before one was produced) falls back to a short id-derived slug, so this never raises. `generate.py` and `tests_gen.py` both just read `item["unit_slug"]`.

## 4. Stage `units`: per-unit loop

For each non-terminal unit, in order:

1. **Context** (`context.py`, specs FR3), pure over the already-loaded `ExecutionState` (no DB or filesystem calls beyond reading the unpacked snapshot):
   - Source slices per member: `file_path` lines `line..end_line` from the snapshot, with the class header included for methods. A member with no `line`/`end_line` in `raw_facts` (a hook, route, schema entity, or a legacy extractor that never recorded one) gets no source slice, just its `raw_facts`. A member with a `line` but no `end_line` is a data error, not a "no span" case, and raises `ContextError` rather than silently reading the rest of the file.
   - Every member's `raw_facts`, plus the unit's own `test_contract` (both passed through as-is, so `generate.py` and `tests_gen.py` see exactly what Agent 2 computed).
   - The cluster report text: the unit's own `cluster_reports` row, or — for a `merged_scc` unit or an override merge (no report of its own, `design_agent2.md` §7/§9.2) — its constituent units' reports, using the same stand-in rule as `agent2/router.py::_unit_sources`.
   - For each `outgoing_dependencies.to_unit_id`, that unit's symbol map (`state.symbol_maps`, populated as each unit finishes, or from resume) joined with the **dependency's own `test_contract.public_interface`** (already known from Agent 2, not from generated code — the contract pins the parameter shape, and the sandbox contract test, §6, is what actually verifies the generated code honors it; nothing here needs to parse or import another unit's output).
   - The exported symbol names (not full signatures) of **every** earlier-finished unit in `state.symbol_maps`, not just this unit's declared `outgoing_dependencies` — the sparse-call-graph mitigation (§13): a missing edge still leaves the name visible to the prompt, and an unresolved import then fails the contract test and triggers a retry.
   - The §8 target-architecture rules. Kept as a fixed block assembled by `generate.py` (it's static, not per-unit data), but `build_unit_context` takes the rules block's reserved length and subtracts it from `max_context_chars` before checking its own payload, so the two pieces together can never exceed the frozen `params.max_context_chars`.

   Summaries of `unverified_member_ids` are dropped, and the unit is flagged `unverified_inputs`. If the total (context payload + the reserved rules length) goes over `max_context_chars`, the unit ends with `error`.

   Snapshot paths are untrusted input read a second time here (`prepare` already unpacked them safely, §3): each member's `file_path` is resolved strictly inside `state.snapshot_dir`, rejecting `..`, absolute paths, and a resolved path that (via a symlink) escapes the snapshot directory.
2. **Generate** (`generate.py`, specs FR4): an Azure OpenAI call (client from `worker/summarize.py::get_azure_openai_client`) with structured output `{files: [{path, content}], symbol_map: {...}}`. Then validate:
   - every path is under `app/units/{unit_slug}/`;
   - every file passes `ast.parse`;
   - `symbol_map` covers every `public_interface.entity_id`.

   A validation failure counts as a failed attempt.
3. **Tests** (`tests_gen.py`, specs FR5): contract tests from §6, plus one LLM call for behaviour tests (given the legacy slices and the generated code), written under `tests/units/{unit_slug}/`.

   **Contract tests, implementation notes** (`tests_gen.py::generate_contract_tests(unit_slug, test_contract, symbol_map, dependencies)`, added 2026-09-28): no LLM. `dependencies` is `UnitContext.dependencies` (step 1 above) — each entry's own `symbol_map`, already known for every earlier-finished unit, so an `outgoing_dependencies[]` test (§6) never needs that unit's generated file text, only its symbol map. Every generated test resolves a module with `importlib.import_module(...)`, matching §8's `unit_slug`-starts-with-a-digit rule — never a literal `import`/`from` statement. The file's own emitted source also never uses an f-string for a dynamic value (plain string concatenation / `%`-formatting instead), so there is no risk of confusing the *generator's* f-string braces with braces meant to appear literally in the *generated* code. A dependency with no symbol map yet (still generating, or `error` with no stub in place) makes its `outgoing_dependencies[]` test `pytest.skip`, not fail — there's nothing to check against yet, and this isn't a verdict on the current unit. Returns one file, `tests/units/{unit_slug}/test_contract.py`; calling it twice with the same inputs gives byte-identical output (§6's determinism requirement, checked directly in `tests/test_agent3_tests_gen.py`, independent of the golden-file test below).

   **Behaviour tests, implementation notes** (`tests_gen.py::generate_behaviour_tests(context, unit_slug, generated, llm_client, model_deployment, max_attempts)`, added 2026-09-28): one Azure OpenAI call (same client/param-sourcing convention as `generate.py` — `model_deployment` is always the job-frozen value, never read from env here), given `context` (legacy source, facts, report, this unit's own `test_contract` — which already carries `side_effect_signals`, so no extra plumbing) plus `generated` (the `GeneratedUnit` `generate.py` just produced: its files and symbol map, so the model writes tests against the real generated names, not guessed ones). Structured output `{"content": "<tests/units/{unit_slug}/test_behaviour.py source>"}`, validated in order: JSON object shape with a non-empty `content` string, `ast.parse` succeeds (never executed — same rule as `generate.py`), and every `def test_*` is decorated `@pytest.mark.behaviour` (a non-`test_*` helper needs no marker). A validation failure retries with feedback, the same conversation-continuation pattern as `generate.py`, up to `max_attempts`.

   **Exhausting every attempt does not fail the unit.** Behaviour tests are explicitly less trustworthy than contract tests (specs Glossary, §6) and CLAUDE.md's "grounding failures never block" applies here too: `generate_behaviour_tests` returns `BehaviourTestResult(files=[], generated=False, attempts=max_attempts)` rather than raising, so the unit's own generated code and its deterministic contract tests are unaffected — there is simply no `test_behaviour.py` for the sandbox to run. Stage `units` (Phase 3, next task) uses `generated` to set `test_summary.behaviour`: the normal `{passed, failed}` shape once the sandbox has produced real results, or `{"passed": 0, "failed": 0, "generated": false}` when generation was abandoned, so a reviewer can see that `side_effect_signals` went unchecked for that unit. This is deliberately **not** a new `unit_executions.flags` value — that column has a fixed DB check constraint (`unverified_inputs`, `built_on_failed_dependency`, `stubbed`), and a fourth value is a migration (§2, "writes are service-role only" / CLAUDE.md multi-tenancy migrations are the user's to run); `test_summary` is free-form jsonb already shown per unit by Phase 5's review UI, so recording it there needs no schema change.

   The prompt also tells the model to use the template's own test fixtures (§8, `tests/conftest.py`) to check side effects rather than only return values: `wp_compat_fake` (the autouse fake backend's `.calls` list) for a `side_effect_signals` member, or `real_wp_compat`/`db_session` for a test that specifically needs persisted state.

   Unlike contract tests, there is no byte-identical-output requirement or golden-file test for behaviour tests: LLM output isn't deterministic (specs NFR6), and re-running an execution job may reasonably produce different (but still valid) behaviour tests.
4. **Run** (`sandbox.py`, §7): build a workspace containing:
   - the template;
   - the last attempt of every unit this one depends on (directly or transitively);
   - this unit's code and tests.

   Run pytest in the sandbox with JUnit XML output, then parse the results.
5. **Retry**: on failure, add the validation errors or the failing test names plus truncated assertion output to the next generation call. Stop at `max_attempts`.
6. **Persist**: upload the attempt's files, log and junit to storage (every attempt, not just the last), then update `unit_executions` (`status`, `attempts`, `symbol_map`, `test_summary`, `last_error`, `flags`, `storage_prefix`), filtered by `id` and `org_id`.

**Failed dependencies (specs FR7):** if a dependency ended `error` with no symbol map, stage `units` generates a stub module for it from the contract's `public_interface` (with the correct names and params, and bodies that `raise NotImplementedError`). The dependent is flagged `built_on_failed_dependency`, and the stubbed unit is flagged `stubbed`. A `public_interface` entry's `name` isn't always a Python identifier — a route-bound entry's `name` is literally `"METHOD /path"` (§6) — so the stub's def/method name is slugified (lowercase, non-alphanumeric runs collapsed to `_`, leading digit guarded) before being written; the `raise NotImplementedError(...)` message still carries the original, unsanitized name, and `symbol_map`'s `python_name` uses the same sanitized identifier that was actually written.

**Orchestration, implementation notes** (`agent3/worker/units.py::run_units_stage`, added 2026-09-28 — the module isn't in §1's per-step file list because it's the glue *between* the steps, not a step of its own):

- **Two retry loops, not one, deliberately kept separate.** `generate.py::generate_unit_code` already retries internally, up to `max_attempts`, for *its own* validation failures (bad JSON, path, syntax, missing symbol) — unchanged from Phase 2. `units.py` adds an **outer** loop, also bounded by `max_attempts`, around generate + test-gen + sandbox-run as a whole: a round whose sandbox run has failing tests feeds the failing test names + truncated assertion output into the *next* outer round via one new, additive parameter, `generate_unit_code(..., extra_feedback=...)` (`None` by default; every Phase 2 test is unaffected). `unit_executions.attempts` is the outer round count — what a reviewer means by "attempt 2 of 3" — not raw LLM calls. Worst case is `max_attempts²` LLM calls; the common case (valid JSON the first time each round) is close to `max_attempts`. A `GenerationError` (the *inner* loop's own budget exhausted) ends the unit `error` immediately, with no outer retry — that already represents a dedicated, generous attempt budget for "can this even become valid Python," so retrying it again outside adds no signal. Contract and behaviour tests are regenerated fresh every outer round (their inputs — the round's own `symbol_map` and generated files — changed).
- **`generate.py::generate_stub_unit(item, unit_slug) -> GeneratedUnit`** (new, deterministic, no LLM): every `public_interface` entry becomes a function or method (grouped by `raw_facts.class`) that raises `NotImplementedError`, with a symbol map in the exact same shape (including the method dotted-name convention, §6) as a real attempt — so downstream context-building, contract-test generation and workspace assembly all treat a stub identically to a real one. The entry's `name` is slugified into a valid identifier first (`generate.py::_safe_ident`, mirrors `tests_gen.py::_safe_ident`'s slug rule) since a route-bound entry's `name` is `"METHOD /path"`, not an identifier; `symbol_map`'s `python_name` always reflects that sanitized identifier, never the raw `name`. Used two ways: permanently, for a unit that itself ends `error` (stored into `state.symbol_maps`/`state.unit_files`, flagged `stubbed` on its own row); ad hoc, for a transitive dependency that hasn't run yet (an unresolved cross-phase `needs_facade` cycle, §13, can put a "dependency" after the current unit in plan order) — not stored back into `state`, just used for that one workspace. Either way, the *dependent* whose workspace needed a stub is flagged `built_on_failed_dependency`.
- **The `__init__.py` re-export guarantee.** `app.main`'s router discovery and this unit's own `hooks_bound`/`routes_bound` contract tests (§6) both import the *package* (`app.units.{unit_slug}`), not a specific submodule, and rely on that import alone running the unit's module-level code. `generate.py`'s prompt now asks the model to make its `__init__.py` import every other file it wrote, but `units.py::ensure_package_init` never trusts that alone: it appends the same guaranteed `importlib.import_module(...)` lines on top of whatever the model (or a stub) produced, for every unit, always. Public since 2026-09-29 (no leading underscore) — `prepare.py`'s resume path calls it too, to wrap a regenerated stub identically to a live `error` verdict.
- **Per-unit workspace**: the template's `app/` tree (not `tests/test_template_smoke.py` — that's whole-project, Phase 4's `assemble`/`final_test`), this unit's own files, every transitive dependency's last-attempt files, this round's `tests/units/{unit_slug}/test_contract.py` (+ `test_behaviour.py` if generated), and a per-unit `app/units/__init__.py` importing only those, dependencies first (§8's note under "One unit-loading mechanism").
- **Verdict**: `sandbox_result.status == "completed" and exit_code == 0 and failed == 0 and len(tests) > 0` counts as a pass — the `len(tests) > 0` guard extends §7's "an empty test list is treated as failing" (there for unparseable JUnit XML) to a clean-looking but empty run (a collection error, or nothing to run at all). `timed_out`/`killed` count as a failed *attempt*, eligible for retry and, if attempts run out, `failed_verification` — never `error`, since real code was produced and just didn't verify cleanly within the limits.
- **`test_summary`**: contract vs. behaviour counts come from `TestCaseResult.classname` containing `test_contract` or `test_behaviour` (§7). If behaviour-test generation was abandoned (`BehaviourTestResult.generated is False`, §4 step 3), `test_summary.behaviour` gets an explicit `"generated": false` alongside `{"passed": 0, "failed": 0}`.
- **Storage** (§2's object layout, exactly): `{prefix}attempt_{n}/code/{file.path}` for generated code (`file.path` already starts with `app/...`, so `code/` is the only segment added) and `{prefix}attempt_{n}/{file.path}` for test files (`file.path` already starts with `tests/...`, so nothing is added — prepending `tests/` again would duplicate it), plus `{prefix}attempt_{n}/log.txt` and `{prefix}attempt_{n}/junit.xml`. `attempt_{n}` is exactly the outer round number stored in `unit_executions.attempts` for that round. `error` units (no successful attempt) upload nothing and keep `storage_prefix = null`.

**Known gap, out of scope for this task (recorded 2026-09-28):**
- **§8's `app/models/{name}.py`** (a unit owning a schema entity writes a shared model file) is not reachable: `generate.py::_validate_path` only ever allows `app/units/{unit_slug}/`. Fixing it means deciding how two units sharing one model file would coexist across separate per-unit sandbox workspaces — a real design question, not a one-line fix, and the fixture repo has no schema entities to force the issue yet.

**Resume and `state.unit_files` (closed 2026-09-29, `tasks_agent3.md` Phase 6 "Redelivery"):** §3 step 5's `_load_resume_state` now repopulates `state.unit_files`/`unit_test_files`/`stubbed_units` for every already-terminal unit, the same way it already did for `state.symbol_maps` — `units.py::download_attempt_files` reads a passed/failed_verification unit's last attempt back from storage, and an `error` unit's stub is regenerated (`generate_stub_unit` is pure). A dependent of a unit that finished before a crash now sees that unit's real result on restart instead of a fresh stub.

## 5. Stages `assemble` and `final_test`

- `assemble.py` builds the project tree from `template/` plus every unit's last attempt (stubs for `error` units).
  - It writes `app/units/__init__.py`, which imports every unit so hook registrations run at startup.
  - It writes `README.md` with a table of units, phases, verdicts and flags.
  - It writes `requirements.txt`, pinning the assembled project's own runtime + test dependencies (`fastapi`, `uvicorn`, `sqlalchemy`, `httpx`, `python-multipart`, `pytest`, `pytest-asyncio`) to the same versions as the root `pyproject.toml`, so the downloaded zip is runnable without manually discovering its deps.
- `final_test` runs the whole `tests/` suite once in the sandbox (with a longer timeout, `sandbox_timeout_seconds × 5`) and stores `final_test_summary`. Then it zips the tree to `{org_id}/{execution_job_id}/project.zip` and sets `output_zip_path` and `counts`.
- A failing final suite doesn't fail the job. It's reported. The job only fails on infrastructure errors (storage, DB, sandbox unavailable).

**Orchestration, implementation notes** (`agent3/worker/assemble.py::run_assemble_stage`/`run_final_test_stage`, added 2026-09-28 — one module for both stages, per §1's file list, since `final_test` has nothing to do without `assemble`'s output and there's no step in between):

- **New `ExecutionState` fields** (`run.py`): `unit_test_files` (cluster_id → the last attempt's contract + behaviour test files, set by `units.py` at the same two sites as `unit_files` — passed and `failed_verification`, never for an `error` unit, which never ran tests) and `project_dir` (the tree `assemble` builds, consumed by `final_test`, removed in `process_execution_job`'s `finally` alongside `snapshot_dir`). Contract tests are deterministic and could in principle be regenerated from `test_contract`/`symbol_map` alone, but behaviour tests are an LLM call — `assemble` must reuse whatever actually ran in the sandbox, not regenerate different tests after the fact.
- **Whole template, not the trimmed per-unit copy.** `units.py::_copy_template` (a per-unit sandbox workspace, §4) copies only `app/` plus `tests/conftest.py`/`pytest.ini`, deliberately leaving out `tests/test_template_smoke.py` — that test only makes sense once, over the whole assembled tree, which is exactly what `final_test` runs.
- **`AssembleError`** (mirrors `PrepareError`): raised if a plan item has no entry in `state.unit_files`. Since resume now repopulates `state.unit_files` for every already-terminal unit (§4, closed 2026-09-29), this should only fire if `units.py` itself failed to reach a terminal status for some item — a bug elsewhere, not an expected resume gap.
- **README**: a fresh read of `unit_executions` for the job (not `state`, so a unit resumed from an earlier run is still reflected correctly), joined against `state.items`/`state.clusters` for ordering and labels, grouped into one table per phase — unit slug, label, status, attempts, flags.
- **`final_test_summary`**: `{passed, failed, errors, duration_s}`, counted directly from `SandboxResult.tests` by `outcome` (`"failed"` and `"error"` kept separate, unlike `SandboxResult.failed`'s combined property).
- **`counts`**: reuses `status.py::counts_by_job` (moved there from `agent3/router.py`, unchanged logic — see §2's `counts` shape correction above) rather than a second implementation; by the time `final_test` runs, every `unit_executions` row for the job is already terminal.
- **Zip determinism**: `_build_zip` sorts file paths and fixes every `ZipInfo.date_time`, so two runs over the same tree produce byte-identical archives. Paths use `Path.as_posix()`, not `str(PurePosixPath(...))` — the latter would keep Windows' backslash separators as literal characters instead of splitting on them, corrupting the archive on a Windows dev machine.
- **`pytest.ini`'s import mode** (`agent3/template/pytest.ini`, discovered by the live Docker test this task added): a per-unit sandbox workspace only ever has one `tests/units/{slug}/test_contract.py`, so pytest's default "prepend" import mode never collides. The *assembled* tree puts every unit's `tests/units/{slug}/test_contract.py` side by side with no intervening `__init__.py` packages, and prepend mode computes each one's module name as the bare `test_contract` — the second file pytest tries to collect fails with "import file mismatch." Fixed by adding `--import-mode=importlib` to the template's `pytest.ini` (a no-op for the per-unit case, since there's never a second file to collide with).

## 6. Test contract → pytest mapping (deterministic)

Built from `migration_plan_items.test_contract` (`design_agent2.md` §10) and the unit's symbol map. Every generated file is sorted and stable.

| Contract field | Generated contract test |
|---|---|
| `public_interface[]` | For each entry, the symbol from `symbol_map` is importable and callable (via `importlib.import_module`, never a literal `import`/`from` naming a `unit_slug` — see §8). Parameter count and names match `params` (snake_cased, defaults allowed). For `kind = method`, it's defined on the mapped class. |
| `incoming_call_sites[]` | For each calling unit, the call site's target symbol is exported at the mapped path. This checks that downstream units can still import what they call. |
| `outgoing_dependencies[]` | The unit imports each dependency only through that unit's symbol-mapped module, never by re-implementing it. Checked by an AST scan of the unit's code. |
| `external_calls[]` | Each legacy `wp_*` / PHP builtin call routes through `wp_compat` (AST scan). `wp_compat`'s in-memory fake backend is active by default in every test (`tests/conftest.py`, §8) — specs FR5 ("tests never touch the network") — so calling the public functions and checking the fake's recorded calls is a smoke test only; nothing here needs a DB session or `set_backend` to run a real one. |
| `hooks_bound[]` | Importing the unit registers a handler for each hook name on the event dispatcher (`app.events.handlers_for(name)` is non-empty). |
| `routes_bound[]` | Importing the unit's package exposes a `router` attribute (§8) whose `router.routes` has an entry matching the bound method and path (e.g. `"GET /appointments"`). Same shape as `hooks_bound`, added 2026-09-28 — this row was missing even though Agent 2's contract (`design_agent2.md` §10) always includes the field. A route-bound `public_interface` entry's `name` is this same `"METHOD /path"` string — not a Python identifier — so `generate_stub_unit` (§4 "Failed dependencies") slugifies it before using it as a def/method name. |
| `side_effect_signals[]` | These members get a behaviour test that must assert on `wp_compat` or DB state, not only on return values. This is a hint passed to the behaviour-test prompt, not a generated test. |
| `unverified_member_ids[]` | No test. The unit is flagged `unverified_inputs` and the prompt leaves those summaries out. |
| `merged_cluster_ids[]` | No test. It only widens the member set in §3. |

Behaviour tests (LLM, `tests_gen.py::generate_behaviour_tests`, §4 step 3) are marked `@pytest.mark.behaviour`, and contract tests `@pytest.mark.contract`, so `test_summary` can split them. Unlike the rows above, `side_effect_signals[]` isn't checked by a generated assertion here — it's a hint the behaviour-test prompt is told to honor (request `wp_compat_fake` or `real_wp_compat`/`db_session` and assert on recorded calls or persisted state, not just a return value), so whether it was actually honored is exactly as trustworthy as any other behaviour test: advisory, not verified by this module. If the LLM call itself never produces valid output within `max_attempts`, there is no `test_behaviour.py` at all for that unit (§4 step 3) — this never fails the unit, but it does mean `side_effect_signals` went completely unchecked, which `test_summary.behaviour` records explicitly (`{"passed": 0, "failed": 0, "generated": false}`) for the review UI (Phase 5).

**`symbol_map`'s dotted name for a method** (added 2026-09-28, pins down §4 step 2's `{python_module, python_name, kind}}` shape, which otherwise leaves `kind = method` unable to say *which* class a method landed on): `python_name` is `"ClassName.method_name"` and `python_module` is the module the class itself lives in. This is the only way a `public_interface[]` contract test can check "is this defined on the mapped class" from a three-field symbol map entry, and it's what `generate.py`'s prompt asks the model for.

## 7. Sandbox

**Runner image** `galvanise-agent3-runner`: `python:3.14-slim` with pinned `fastapi`, `pydantic`, `sqlalchemy`, `httpx`, `pytest`, `pytest-timeout`. It's built from `backend/agent3_runner/Dockerfile`, and the tag is frozen in `params.runner_image`. There's no pip install at run time, because there's no network.

**Where the image comes from.**
- **Development:** built locally on Colima with `uv run document-analyzer-build-agent3-image` (a curated tar context, sent only to the Colima profile's engine). When the image is missing, the executor refuses to start and prints that build command (§10).
- **Docker Desktop name lookup (2026-09-25, history — the system moved to Colima on 2026-09-28):** on Docker Desktop 29.6.1 with the containerd image store, `docker image inspect galvanise-agent3-runner:0.1` intermittently answers "No such image" for an image that exists. `docker image ls` lists it, `docker run` resolves it, and the fully qualified `docker.io/library/galvanise-agent3-runner:0.1` always inspects. An earlier "image deleted" diagnosis that day was this bug. `check_sandbox_available` therefore retries a missed short name under its qualified name before reporting the image missing (`sandbox.qualified_image_name`).
- **Production:** push the image to a private registry and set `AGENT3_RUNNER_IMAGE` to its digest (`<registry>/galvanise-agent3-runner@sha256:…`), not a tag.
  - A digest can't be re-pointed, so the image frozen in `params.runner_image` is exactly the one that ran. That keeps runs reproducible and auditable (specs NFR5).
  - A lost image can be pulled back unchanged.
  - `sandbox.py` runs with `--pull never`, so the deploy step pulls the image before the executor starts, and the startup check confirms it's present.
  - Every change to `requirements.txt` or the Dockerfile means a new tag and a new digest, never an overwrite.

**Engine: Colima by default (2026-09-28).** Every `docker` call here (`run`, `kill`, `rm -f`, `info`, `image inspect`) goes through `shared/container_engine.py`, the same module Agent 1's Graphify runner uses: `DOCKER_HOST` is the socket `colima status $COLIMA_PROFILE --json` reports, `DOCKER_CONTEXT` is dropped, and `check_sandbox_available` refuses an engine whose `docker info` name isn't `colima`/`colima-<profile>`. Docker Desktop is never used, whatever the machine's current context. Colima shares only `$HOME` and `/tmp/colima` with its VM, so `run_pytest` copies the workspace (symlinks copied as links, never followed) into a fresh directory under `CONTAINER_WORKDIR/agent3` (default `~/.cache/document-analyzer/agent3`), mounts that copy, and deletes it afterwards; a workspace that contains the staging dir is refused. Before this, a workspace in the system temp dir (`/var/folders/...`, pytest's `tmp_path`) mounted empty on Colima. Verified live on Colima 0.10.3 (engine 29.5.2): the 8 smoke tests below pass.

**Invocation** (`sandbox.py`, via the `docker` CLI client against Colima, the same way Agent 1 shells out to `git` and `php`):

```
docker run --rm --name agent3-<uuid>
  --pull never --log-driver none
  --network none
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=256m
  --cap-drop ALL --security-opt no-new-privileges
  --user 65534:65534
  --memory {sandbox_memory_mb}m --memory-swap {sandbox_memory_mb}m
  --cpus 1 --pids-limit 256 --ulimit nofile=1024:1024
  -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp
  [--runtime runsc]
  --mount type=bind,src={staged copy of workspace},dst=/work,readonly -w /work
  {runner_image} sh -c 'python -m pytest -q -p no:cacheprovider --junitxml=/tmp/junit.xml >/tmp/out.txt 2>&1; c=$?;
                        tail -c 65536 /tmp/out.txt; echo; echo <sentinel>; head -c 5000000 /tmp/junit.xml; exit $c'
```

- `--mount` rather than `-v`, because Windows paths contain `:`. A workspace path containing `,` is refused (it would inject mount options).
- `--pull never`: a missing image fails fast instead of trying a pull. `--log-driver none`: container output never lands in the daemon's log files.
- The host environment is never forwarded (`docker run` doesn't inherit it), so worker secrets stay out of the container. Only the two `-e` values above are set.
- On Linux, `run_pytest` makes the workspace tree world-readable (not writable) first, because `mkdtemp` dirs are `0700` and the container runs as uid 65534.
- The wall-clock timeout is enforced by the worker (`subprocess` timeout → `docker kill` + `docker rm -f` by container name, because killing the CLI doesn't stop the container). Per-test `pytest-timeout` is configured by the template's pytest settings (§8), not on the command line.
- The JUnit XML is streamed back on stdout after the output tail, split on a random per-run sentinel, so nothing needs a writable mount.
- **Output is capped.** Inside the container: the last 64 KB of pytest output and the first 5 MB of JUnit XML. On the host: the CLI's stdout and stderr are drained on threads that keep the first 10 MB / 64 KB and discard the rest. Generated code can write to the container's stdout directly, bypassing the redirect, so neither host memory nor host disk can be flooded.
- **Untrusted XML.** `parse_junit` refuses anything over 5 MB or containing `<!DOCTYPE` / `<!ENTITY`, then parses with stdlib `ElementTree`. Unparseable XML gives an empty test list, which the verdict logic treats as failing.
- **Result:** `SandboxResult{status, exit_code, tests[{classname, name, outcome, message}], output_tail, junit_xml, duration_s}`. `status` is `completed`, `timed_out` (worker killed it), or `killed` (exit 137: the OOM killer or another SIGKILL). `classname` is the dotted module path, which is how `test_contract.py` and `test_behaviour.py` results are told apart. `output_tail` and `junit_xml` are what §2 stores as `log.txt` and `junit.xml`.
- `SandboxUnavailableError` covers infrastructure errors: no `colima` or `docker` CLI, the Colima profile stopped or not using the docker runtime, an engine that isn't Colima, image missing, gVisor required but absent, or `docker run` exiting 125/126/127 without the sentinel. The worker fails the *job* on it and never turns it into a unit verdict.
- **gVisor:** `--runtime runsc` is added when `AGENT3_SANDBOX_RUNTIME=runsc`. The worker refuses to start if `AGENT3_REQUIRE_GVISOR=true` and `runsc` isn't registered with Docker (production setting). On a developer machine (for example Docker Desktop on Windows) it runs with the default runtime, keeping every other flag, and logs a warning.
- The worker never imports, execs or `eval`s generated code in its own process. Parsing with `ast` is the only thing it does with it.

**Switchable engine (2026-09-28, later that day).** `CONTAINER_ENGINE=colima|docker` picks the engine, defaulting to `docker` on Windows (no Colima there) and `colima` everywhere else. `colima` is everything described here. `docker` uses whatever engine the `docker` CLI already points at (its current context, `DOCKER_CONTEXT` or `DOCKER_HOST`), e.g. Docker Desktop on Windows/WSL 2: `verify_engine` only requires `docker info` to answer, and the shared-path check is skipped because Docker Desktop shares host drives (staging under `CONTAINER_WORKDIR` still happens and is harmless). The choice is explicit: an engine that is down is `unavailable`, never swapped for the other one. Images live per engine, so after switching run both `build-*-image` commands once. `container_engine.image_present` retries a missed short image name under its qualified name (the Docker Desktop containerd lookup bug, `design_agent3.md` §7) for Graphify, Agent 3 and the doctor alike. `uv run document-analyzer-doctor` prints the active engine. Verified on Colima both directly and through the `docker` path (`DOCKER_HOST` at Colima's socket): doctor all OK and the 8 live sandbox smoke tests pass. Docker Desktop's lack of `runsc` is covered under "Docker Desktop vs gVisor" below.

**Known limitation: the sandbox protects the host, not the verdict.** Generated code runs in the same process as pytest, so it can fake its own results. It can monkeypatch pytest, rewrite `/tmp/junit.xml`, or read the sentinel from `/proc/1/cmdline` and print a forged report. Verdicts are therefore advisory. The backstops are the reviewer UI (the code is shown next to its tests and log) and the final assembled run (§5), where one unit can't easily fake another unit's tests. None of this lets code out of the container.

**gVisor on Colima (2026-09-28).** Colima's VM doesn't ship `runsc` either, so developer machines run with the default `runc` runtime (warning logged, every other flag kept) — `document-analyzer-doctor` reports it as a WARN. Production gVisor means registering `runsc` with the Docker engine inside the Colima VM (`colima ssh`) or running the executor on a Linux host; `AGENT3_REQUIRE_GVISOR=true` still refuses to start without it.

**Docker Desktop vs gVisor (2026-09-25, history).** Docker Desktop on Windows (WSL 2 backend) doesn't ship `runsc`, and registering it inside Docker Desktop's VM isn't supported. Developer machines therefore run with the default `runc` runtime, keeping every other flag above. gVisor is only verified on a Linux production host (`test_live_gvisor_runtime` skips elsewhere). Verified live on Docker Desktop 29.6.1 (engine runtimes: `runc`, `io.containerd.runc.v2`, `nvidia`; no `runsc`) with `tests/test_agent3_sandbox.py`: 8 of 8 smoke tests pass, and the gVisor test is skipped. Observed: allocating 512 MB under a 256 MB limit is OOM-killed in about 2 s (exit 137 → `killed`, no JUnit), a `sleep(600)` test is `timed_out` after the 5 s limit and its container is gone afterwards, and 1,000 threads hit the 256 pids limit (`RuntimeError`).

**Sandbox smoke tests (blocking before any generation work)**, in `tests/test_agent3_sandbox.py`, run live against the image:
1. A test that opens a socket or resolves DNS fails.
2. A test that allocates beyond the memory limit is killed, and a thread bomb hits the pids limit.
3. A test that sleeps past the timeout is killed, and its container is removed.
4. A write outside `/tmp` (to `/work` or `/etc`) fails, while `/tmp` is writable.

Plus: a positive control (one pass, one fail reported correctly), the uid is 65534, a host env canary isn't visible inside the container, and a gVisor run (skipped without `runsc`).

## 8. Target architecture (WordPress → FastAPI)

The rules every generation prompt includes. This is the contract between units, so changing it is a design change. The fixed, non-generated files (`main.py`, `events.py`, `wp_compat/`, `db.py`, `pytest.ini`, `tests/conftest.py`) live in `agent3/template/` and are copied verbatim by stage `assemble` (§5); nothing in `generate.py` ever touches them.

```
app/
  main.py              # FastAPI app; imports app.units, then walks its unit packages for a `router` attribute
  events.py            # hook dispatcher: on(name, priority)(fn), fire(name, *args), apply_filters(name, value, *args)
  wp_compat/           # fixed template: options, post/user meta, transients, http (wp_remote_*), escaping, i18n no-ops
  db.py                # SQLAlchemy engine/session dependency ($wpdb replacement)
  models/              # SQLAlchemy models for CPTs / post meta (generated by the unit that owns the schema entities)
  units/__init__.py    # written by stage assemble, in plan order (§5) — imports every unit package, nothing else
  units/{unit_slug}/   # one package per plan item; only files the unit generated
tests/
  conftest.py          # wp_compat fake backend (autouse), SQLite session, event dispatcher reset
  units/{unit_slug}/test_contract.py, test_behaviour.py
pytest.ini             # registers the contract/behaviour markers, --strict-markers, a default per-test timeout
```

**One unit-loading mechanism.** `app/units/__init__.py` is the only place units get imported: stage `assemble` writes it in plan order, and importing each unit package runs its module-level `@events.on(...)` registrations. `app/main.py` imports `app.units` (triggering that), then walks `app.units`'s own submodules **in the same sorted order** and includes each one's `router` attribute if it has one — there is no separate `pkgutil` discovery pass and no second way to reach a unit's code, so a unit that was never imported by `units/__init__.py` is invisible to the running app. A per-unit sandbox workspace (stage `units`, §4) needs its own `app/units/__init__.py` too, importing only that unit and its (transitive) dependencies — see the Phase 3 task note in `tasks_agent3.md`.

**`unit_slug` starts with a digit, so it can't be a literal import target.** `{phase_order:02d}_{unit_order:02d}_{slugified label}` (e.g. `01_01_cart`) is not a valid Python dotted-import expression — `from . import 01_01_cart` is a `SyntaxError`, only `importlib.import_module("...")` (a plain string) works. Every place a unit gets imported therefore uses `importlib`, never a literal `import`/`from` statement: `app/main.py` already does (`pkgutil.iter_modules` + `import_module`); `app/units/__init__.py` (stage `assemble`, and the per-unit version stage `units` builds) must too, e.g.:
```python
import importlib
for _slug in ["01_01_cart", "02_01_orders", ...]:  # plan order
    importlib.import_module(f"{__name__}.{_slug}")
```
Generated tests (`tests_gen.py`, §6) resolve a unit's own or a dependency's module the same way, never with a literal `import`/`from` statement naming a `unit_slug`.

**`wp_compat`'s backend interface.** `wp_compat/backend.py` defines an ABC (`get_option`/`update_option`/`delete_option`, `get_post_meta`/`update_post_meta`, `get_user_meta`/`update_user_meta`, `get_transient`/`set_transient`/`delete_transient`, `wp_remote_get`/`wp_remote_post`). `wp_compat/real.py` implements it against `app.db` (options/meta/transients as tables) and `httpx` (the two `wp_remote_*` calls); `wp_compat/fake.py` implements the same interface entirely in memory, recording every call (`FakeWPCompatBackend.calls`) so a behaviour test can assert on them (§6 `external_calls`). `wp_compat.set_backend(backend)` swaps the active one and returns the previous one; `wp_compat/__init__.py` re-exports flat functions (`wp_compat.get_option(...)`) that delegate to whichever backend is active, plus the stateless `escaping.py`/`i18n.py` helpers (`esc_html`, `esc_attr`, `esc_url`, `sanitize_text_field`, `__`, `_e`), which need no backend since they carry no state. **The fake backend is active by default in every test** (`tests/conftest.py` installs it, autouse) — specs FR5 ("tests never touch the network"); a test that specifically wants the real, DB-backed behavior opts in by calling `wp_compat.set_backend(RealWPCompatBackend())` itself.

**Event ordering.** `events.on(name, priority=10)` matches WordPress: handlers run in ascending priority order, and handlers registered at the same priority run in registration order (stable sort on insertion index, not on name or anything else).

**`pytest.ini`** (copied to the assembled project's root, so it's the config `python -m pytest` reads when the sandbox runs from `/work`, §7): registers the `contract` and `behaviour` markers plus `--strict-markers` (an unmarked or misspelled marker on a generated test is caught as an error, not silently ignored), and a default per-test `timeout = 30` (pytest-timeout) — this is what §7 means by "per-test timeout is configured by the template's pytest settings, not on the command line."

| Legacy construct | Generated form |
|---|---|
| Plain function | A module-level function in the unit package, snake_case name, type hints from `raw_facts.params` where known. |
| Class and methods | A Python class with the same methods. Static methods stay static. |
| `add_action` / `add_filter` | `@events.on("hook_name", priority=n)` at module level. `do_action` → `events.fire`, `apply_filters` → `events.apply_filters`. |
| `register_post_type`, post meta | SQLAlchemy model in `app/models/{name}.py`, written by the unit owning the `schema` entity. |
| `$wpdb` queries | SQLAlchemy via `app.db` session, parameterized. No raw string SQL built from input. |
| `add_shortcode`, REST routes, `admin-ajax` handlers | An `APIRouter` in the unit package, exposed as that package's `router` attribute — picked up by `app.main`'s single unit-loading pass above, not a separate discovery mechanism. |
| `wp_*` / PHP builtins with no Python equivalent | A call through `app.wp_compat`. If `wp_compat` lacks it, the unit adds a private adapter inside its own package and marks it `# TODO(wp_compat)`. |
| Globals | A module-level constant or a settings object. No mutable module globals shared across units. |

`unit_slug` is `{phase_order:02d}_{unit_order:02d}_{slugified label}`, so it's stable within one plan version.

## 9. API surface (`/api/agent3/...`)

The conventions are the same as Agents 1 and 2 (`design.md` §4, `design_agent2.md` §11):
- Supabase JWT auth dependency.
- `org_id` resolved server-side and never accepted as a parameter.
- `404` for missing and other-org resources alike.
- `409` for a valid resource in the wrong state.
- Responses rebuilt field by field.
- Inserts into `execution_jobs` / `unit_executions` go through the service-role client (no authenticated insert policy).

| Method & path | Purpose | Notes |
|---|---|---|
| `POST /plans/{plan_id}/executions` | Create an execution job | `202 {execution_job_id, status}`. `404` for missing or other-org plan. `409` if not `approved`, stale (runs Agent 2's `mark_stale_plans` check first, imported as a pure helper or moved to `shared/`), no source snapshot, or an active job exists (also the unique-index `23505` race). |
| `GET /plans/{plan_id}/executions` | List execution jobs for the plan | `{executions: [{id, status, progress_stage, counts, created_at}]}`, newest first. `counts` has the same live shape as `GET /executions/{id}`, fetched for all the plan's jobs in one batched read. `404` for a missing or other-org plan. |
| `GET /executions/{execution_job_id}` | Poll status | `{execution_job_id, plan_id, project_id, status, progress_stage, error, counts, final_test_summary, has_output}`. `counts` is `{total, done, passed, failed_verification, error, built_on_failed_dependency}`, computed live from this job's `unit_executions` rows on every poll, so the status page can show progress while `units` runs (`execution_jobs.counts` is only written at the end, §5). `done` counts terminal units (`passed` + `failed_verification` + `error`). `has_output` is whether `output_zip_path` is set. `404` for missing and other-org jobs alike. |
| `GET /executions/{execution_job_id}/units` | Verdicts | `{phases: [{phase_cluster_id, phase_order, label, units: [{cluster_id, unit_order, label, status, attempts, flags, test_summary}]}]}`. Labels come from `clusters`, the same stand-in rules as `design_agent2.md` §11 for merged items. |
| `GET /executions/{execution_job_id}/units/{cluster_id}` | Unit detail | `{cluster_id, status, attempts, flags, last_error, test_summary, symbol_map, legacy: [{entity_id, name, file_path, line, end_line, source}], files: [{path, content}], tests: [{path, content}], log}`. Reads the last attempt from storage. `404` for a unit not in this job. |
| `GET /executions/{execution_job_id}/download` | Assembled zip | `200 {url, expires_in}` (a signed URL, 5 min). `409` until `output_zip_path` is set. |

**`GET /executions/{execution_job_id}/units` details** (`router.py::get_execution_units`, added 2026-09-28):
- `404` comes from `_get_execution_job_plan_id` (a lighter existence+`plan_id` check than `get_execution_status`'s full row) rather than a separate `_get_execution_job` — the endpoint needs nothing else off the job row.
- `sources` per unit (the `unit_sources` inputs Agent 2's own detail endpoint uses, §11: an override merge's `merged_cluster_ids`, a `merged_scc` unit's originals via `merged_from`, or just the unit itself) come from `migration_plan_items.test_contract` (`unit_executions` doesn't carry the contract) plus `_merged_from`, reading `planning_jobs.graph_stats.merged_into` — the same query `agent2/router.py::_merged_from` and `agent3/worker/prepare.py::_merged_from` each already run (design doc §3 step 6's note on why that one isn't a shared helper applies here too: reads the DB, and Agent 3 may read Agent 2's tables directly, CLAUDE.md).
- `_unit_label(cluster_id, sources, clusters)` is a third, router-local copy of the same rule `agent2/router.py::_unit_fields` and `agent3/worker/unit_slug.py::_unit_label` each already implement (own label when unmerged; a `merged_scc` unit's own label first, else its sources' sorted-and-joined labels; an override merge always joins) — not imported from either, since the worker's version reads `ExecutionState.clusters`, not a plain dict, and isn't meant as a cross-module API, and duplicating ~6 lines three times is cheaper than a shared abstraction across two agents' routers plus one worker.
- **Phase labels are never merge-derived** — a `phase_cluster_id` is always a genuine, unmerged cluster (§7's Leiden clustering only merges *unit*-level SCCs), so a phase's `label` is read straight off its own `clusters` row.
- Every `cluster_id` any unit or its sources or its phase names is loaded in one batched, chunked (`_IN_CHUNK` = 200) `clusters` read, mirroring `agent3/worker/prepare.py::_load_clusters`'s own pattern.

**`GET /executions/{execution_job_id}/units/{cluster_id}` details** (`router.py::get_execution_unit`, added 2026-09-28):
- `404` in two steps: the execution job (via `_get_execution_job_plan_id`, same as the `/units` list endpoint), then the specific `unit_executions` row for `(execution_job_id, cluster_id, org_id)` — a `cluster_id` that's a real cluster but never a unit of *this* job's plan (or belongs to another org) gets the same `404` either way.
- **`legacy`** is built at request time, not read from anything precomputed: `member_entity_ids` are recomputed via a router-local `_member_entity_ids` (the single-unit case of `prepare.py::_member_entity_ids_by_item`'s query — `migration_plan_items.test_contract.merged_cluster_ids` plus `cluster_members`, since `unit_executions` doesn't carry either), their `extracted_entities` rows are loaded, then the plan's source snapshot is downloaded from `agent1-snapshots` and unpacked into a temp dir (`agent3/worker/prepare.py::unpack_snapshot`, the same untrusted-input guards as stage `prepare`) so `agent3/worker/context.py::_source_slice` — imported directly, since it's pure (no DB, no filesystem access beyond the already-unpacked dir) and CLAUDE.md's pure-helper exception covers importing it into the router — can slice each member's source the identical way stage `units` did when it generated against it. The temp dir is removed in a `finally`, whatever happens. **Nothing is cached**: every request re-downloads and re-unpacks the whole snapshot, same as the worker does once per job — a real cost for a large repo, not addressed by this task (no request volume yet to size a cache against).
- A member whose `raw_facts` can't be sliced (`ContextError`, e.g. an out-of-range span) gets `source: null` for that one entity rather than failing the whole response — a display endpoint degrades per-row, unlike stage `units`, where the same error ends the *unit* `error`.
- **`files`/`tests`/`log`** come from `agent3-output` at `{storage_prefix}attempt_{attempts}/` (`storage_prefix` + `attempts` together name the exact folder the last attempt was uploaded under, §2's object layout) via a router-local `_list_objects` (the same recursive-listing pattern `projects.py::_list_objects` uses for delete, copied rather than shared since that one is delete-cleanup's concern) plus `bucket.download`. `storage_prefix is None` (a unit that's `pending`/`running`, or `error` with no sandbox run at all) returns `files: []`, `tests: []`, `log: null` — there is nothing to read.

**`GET /executions/{execution_job_id}/download` details** (`router.py::get_execution_download`, added 2026-09-28):
- `404` if the execution job doesn't exist (or isn't this org's); `409` (`"no output has been generated for this execution yet"`) if it exists but `output_zip_path` is still null — a job that's `queued`/`running`/`failed`, or `done` with `final_test_summary` set but somehow no zip (shouldn't happen, since `final_test` sets both together, §5), all get the same `409`.
- The signed URL comes from `storage3`'s own `bucket.create_signed_url(path, expires_in)` (`{"signedURL": ..., "signedUrl": ...}` — both keys are the same value; the response uses `signedUrl`), not a hand-rolled signature — Supabase Storage does the actual signing. `expires_in` is always `_DOWNLOAD_EXPIRES_IN = 300` (5 minutes, matching the documented shape), never client-supplied.
- `tests/fakes.py::FakeStorageBucket` gained a matching `create_signed_url(path, expires_in, options=None)` (raises the same "object not found" as `.download()` for a missing path, otherwise returns a fake but structurally identical URL) so this endpoint is testable without a real Supabase project.

## 10. Worker wiring

`agent3/worker/run.py::process_execution_job`:
1. Terminal-status guard.
2. `mark_execution_running`.
3. Run `prepare → units → assemble → final_test` with `mark_execution_stage` before each stage.
4. `mark_execution_done`.

Any exception leads to `mark_execution_failed`. The message is deleted in `finally`. `load_dotenv()` runs before imports (`design.md` §3 bug #1). At startup, `run_forever` checks that the Colima profile's engine is reachable (and is Colima) and that the runner image exists, plus the gVisor check (§7). It doesn't need the embedding config check, because Agent 3 doesn't embed.

## 11. Configuration

The values are frozen into `execution_jobs.params` at creation.

| Env var | Default | Meaning |
|---|---|---|
| `AGENT3_MODEL_DEPLOYMENT` | the Azure deployment Agent 1 uses | Model deployment used for generation |
| `AGENT3_MAX_ATTEMPTS` | 3 | Generation attempts per unit |
| `AGENT3_MAX_CONTEXT_CHARS` | 60000 | Prompt payload cap per unit |
| `AGENT3_SANDBOX_TIMEOUT_SECONDS` | 120 | Per-unit test run wall clock |
| `AGENT3_SANDBOX_MEMORY_MB` | 1024 | Container memory limit |
| `AGENT3_RUNNER_IMAGE` | `galvanise-agent3-runner:<pinned tag>` | Sandbox image |
| `AGENT3_SANDBOX_RUNTIME` | *(unset)* | `runsc` to use gVisor (operational, not frozen) |
| `AGENT3_REQUIRE_GVISOR` | `false` | Refuse to start without gVisor. `true` in production (operational, not frozen) |
| `AGENT3_QUEUE_VISIBILITY_TIMEOUT_SECONDS` | 3600 | Same semantics as Agents 1 and 2. Longer, since jobs run longer. |

**Load run findings (2026-09-29, tasks_agent3.md Phase 6 "A larger run"):** `php` isn't available on every dev machine (none here), which blocks `tests/test_load_large_repo.py`'s generator through the real Agent 1 → Agent 2 pipeline. Instead, a 12-unit synthetic plan was built directly at the DB-row level (`tests/test_agent3_load_run.py`, opt-in via `AGENT3_RUN_LOAD_TEST=1` — it makes real Azure OpenAI calls, real cost) — 8 independent small WordPress-utility-style PHP functions (phase 1) plus 4 each depending on one of them (phase 2), run through the real executor: real Azure OpenAI (deployment `gpt-5.6-luna`) and the real sandbox (Docker Desktop).

Result: **12/12 passed** — 10 on the first attempt, 2 on the second, none needed a third. Per-unit wall time (LLM calls + sandbox run together) ranged 13.1–44.2 s (avg ~19.4 s for a first-attempt unit; the two second-attempt units took 44.2 s and — folded into the average above — the rest). 28 LLM calls total, 47,596 tokens (avg ~1,700/call), 178.7 s of LLM wall time out of 232.2 s total units-stage wall time (serial) — sandbox overhead is a small fraction (~54 s across 14 attempt-rounds, ~3.9 s/round) of the total, LLM latency dominates. `assemble` + `final_test` (the whole 78-test assembled suite) took 11.2 s.

This settles specs_agent3.md §9's two attempts/timeout/parallelism questions:
- **`AGENT3_MAX_ATTEMPTS = 3` and `AGENT3_SANDBOX_TIMEOUT_SECONDS = 120` both stay as-is.** Nothing in this run came close to either limit (max attempts actually used: 2 of 3; the whole final suite, under a 5×-longer timeout, finished in 11.2 s). No evidence either default is too tight for functions of this size and complexity; revisit only if a real WordPress plan's units turn out to be markedly larger or more stateful (DB-backed, multi-step hooks) than this synthetic set.
- **Per-phase parallelism is worth adding.** Wall time is dominated by LLM latency (77% of units-stage time) and per-unit Docker startup (~3.9 s/round), both independently parallelizable — the sandbox's own per-container resource caps (§7: memory, cpus, pids-limit) already bound worst-case load *per container* regardless of how many run concurrently, so a bounded concurrency cap (same pattern as Agent 1's `summarize_fanout`, §7's own cost/time-bounding precedent) would cut a large phase's wall time roughly in proportion to the cap, not just make it faster in the best case. Phase 1's 8 mutually-independent units here took the full 232.2 s serially; a cap of 4, say, would have plausibly cut that phase to roughly a quarter given the workload was LLM/IO-bound, not CPU-bound on the host. Not implemented as part of this task — recorded here as the measured basis for a follow-up task, since `run_units_stage`'s current one-at-a-time loop (`units.py`) would need a bounded `ThreadPoolExecutor` (or similar) per phase, plus care that `state.symbol_maps`/`state.unit_files` writes from concurrent units stay race-free.
- **Dollar cost** depends on the org's actual per-token rate for whatever model `gpt-5.6-luna` resolves to, which isn't derivable from the deployment name alone — 47,596 tokens for 12 units (~3,970 tokens/unit including both the generate and behaviour-test calls) is the number to apply that rate to.

## 12. Frontend

It follows `tasks_agent2.md`'s frontend conventions: no router, pages chosen by query param, `useApiFetch()`, and loading, empty and error states from the start. New params: `?execution=<id>` and `?execution=<id>&unit=<cluster_id>`.
- `PlanPage` (approved): a "Generate code" action and a list of past executions.
- `ExecutionStatusPage`: the `ProgressStages` stepper with `prepare → units → assemble → final_test → done`, plus a live count of units done out of the total.
- `ExecutionResultsPage`: units grouped by phase with verdict pills, a flags badge, and a download button.
- `ExecutionUnitPage`: legacy source next to the generated files, the tests, and a collapsible log.

## 13. Dependencies on Agents 1 & 2, and known gaps

Agent 3 is read-only on Agent 1 and 2 tables. These gaps are fixed in their own task files:

- **No legacy source after ingestion (blocking).** Agent 1 clones with `--depth 1`, records no commit, deletes the clone after parsing, and zip uploads are kept but GitHub sources aren't. **Agent 1 fix (`tasks.md`):**
  - Record `ingestion_jobs.source_commit_sha`.
  - Upload the boilerplate-stripped source as `agent1-snapshots/{org_id}/{job_id}/source.tar.gz`, and add `ingestion_jobs.source_snapshot_path`.
  - Add `end_line` to function, method and class `raw_facts` (`extract.php` `getEndLine()`) and to the WordPress extractor's entities.

  Jobs ingested before this fix get `409` "re-ingest this project to generate code". The `source_snapshot_path` column was added ahead of the rest (2026-09-25, `20260925130000_agent1_source_snapshot_path.sql`), so the gate is live. Until Agent 1 uploads snapshots, every plan gets that `409`.
- **Sparse call graph** (`design_agent2.md` §13). Missing edges mean incomplete `outgoing_dependencies`, so a unit may call code it wasn't given the symbol map for. Mitigation: the prompt also gets a list of every earlier unit's exported symbols (names only), and an unresolved import makes the contract test fail, which triggers a retry.
- **`include` edges have no target.** File-level includes are invisible. Their effect should show up as unresolved-symbol test failures.
- **Project delete** (`projects.py::delete_project`, Agent 1) removes Agent 3's `unit_executions`, `execution_jobs` and `agent3-output` objects for the project, and refuses (`409`) while an execution job is active. It was done 2026-09-27 (`design.md` §4), after Agent 1's snapshot work and Agent 3's Phase 1 landed together.
- **Staleness** is checked with Agent 2's rule (`design_agent2.md` §9.1). An existing execution of a plan that later goes stale is kept, and new executions are refused.

## 14. Later: differential testing

Out of the MVP (specs §7), but kept possible:
- `test_summary` is keyed by suite (`contract`, `behaviour`), so a `differential` suite can be added without a schema change.
- A future PHP sandbox would need its own network-isolated, resource-limited runner (with PHP, a WordPress core stub and a DB) and recorded I/O per `public_interface` entry. It would be a separate design section and separate tasks. It must never share the Python runner.
