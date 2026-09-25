# tasks_agent3.md — Agent 3 (Execute)

Rule for this file (same as `tasks.md` and `tasks_agent2.md`): **every backend task that adds or changes an `/api/agent3/...` endpoint has a paired frontend task directly under it.** Don't check off a backend box until its frontend pair can exercise the endpoint end to end. Backend tasks with no endpoint (worker stages, migrations) need no frontend pair, and each says so explicitly.

Contracts live in `design_agent3.md` §9 (API) and §2 (tables). If a task needs a contract change, update the design doc first, then this file, then the code (CLAUDE.md).

### Frontend conventions
The same as `tasks_agent2.md`:
- No router. Pages are chosen by query param, and the params are mutually exclusive. Agent 3 adds `?execution=<id>` (optionally `&unit=<cluster_id>`) with a `navigateToExecution` helper.
- Every call goes through `useApiFetch()`. Errors render through `formatJobErrorMessage`.
- Reuse the `ProgressStages` stepper, the `risk-level-*` pills, `UnverifiedBadge` and the loading/error classes.
- Every view ships with loading, empty and error states.
- Visual check: `vite build` plus a real browser pass.

### Prerequisites (outside this file)
- [ ] **Agent 1 source snapshot** (`tasks.md`, "Agent 3 prerequisites"): commit SHA, snapshot upload, `end_line` in `raw_facts`. Nothing in Phase 2 onwards can run on real data without it.
- [ ] At least one plan reaches `approved` live, end to end, for a **WordPress** repo: ingest → plan → submit → clear every `needs_facade` flag → approve every phase. The repo must be ingested after the Agent 1 source-snapshot task, so the plan's source job has a snapshot. Agent 1 jobs already reach `done` live, so the Azure endpoint note in `tasks.md` Phase 4 is out of date (`design_agent2.md` §15: Laravel `hospital_test`, 2026-09-23). What's missing is a WordPress repo taken through to an approved plan. Until then, Phases 0–4 build against `tests/fakes.py::FakeClient` and `tests/fixtures/agent2_cycle_repo/`, extending `tests/test_agent2_pipeline_fixture.py`.
- [ ] Docker is installed on every machine that runs the executor, and gVisor (`runsc`) on the production host.

---

## Phase 0 — Foundations

> **Live project (2026-09-25).** The "applied live" notes below were first checked on a personal Supabase project. The team then moved to one shared project, and everything was re-applied and re-checked there:
> - It had been missing `20260923030000` (Agent 2) plus the four Agent 3 / Agent 1 migrations below (renamed `20260925100000`–`130000` so they sort after `main`'s `20260925000000`–`020000`). All were applied in the SQL Editor.
> - It also had three Agent 2 user FKs drifted to an out-of-band `app_private.accounts`, fixed by `20260925140000_plan_user_fks_back_to_auth_users.sql` (`design_agent2.md` §2).
> - Re-checked there: every Agent 3 table/column, index, policy and queue; a queue round trip; the bucket check (9/9); and `tests/test_rls_multi_tenancy.py` 46/46.
> - That test's fixture now cleans up after a failed setup too. Earlier failed runs had left throwaway `rls-smoke-*` users and rows, and the ones from this session were deleted.

- [x] **Blocking:** runner image `backend/agent3_runner/Dockerfile` (`design_agent3.md` §7), plus `sandbox.py::run_pytest(workspace, params)` returning parsed JUnit results. Add `tests/test_agent3_sandbox.py` with the four §7 smoke tests: no network, killed on memory/pids, killed on timeout, read-only root. Also add a positive control (a passing test reports passed). Record the Docker Desktop vs gVisor findings in `design_agent3.md` §7. No endpoint, so no frontend pair.
  - *Done 2026-09-25:* image `galvanise-agent3-runner:0.1`. 21 unit tests plus 8 live smoke tests pass on Docker Desktop 29.6.1 (also checked: non-root uid, no host env, DNS blocked). The gVisor test is skipped because Docker Desktop has no `runsc`, so it still needs a run on the Linux production host. Output capping and untrusted-XML handling are in §7, along with the known limitation that verdicts are advisory.
- [x] Migration `<ts>_agent3_execution_tables.sql`: `execution_jobs` (including the partial unique index `execution_jobs_one_active_per_plan`) and `unit_executions` (§2). RLS enabled with `select` policies on `org_id = current_org_id()`. No insert or update policies. Apply to the live project and verify. No endpoint, so no frontend pair.
  - *Done 2026-09-25:* `20260925100000_agent3_execution_tables.sql`. First checked in a throwaway Postgres 17 container, with stubs for the referenced tables and `current_org_id()`. 14 behaviour checks pass there: the one-active-job index (`23505`, then allowed after `done`), the unit uniqueness, the flag/attempts/status constraints, no cascade from plans or clusters, the cascade from the job to its units, and RLS (org A sees its rows, org B sees none, authenticated inserts refused). It was then applied live in the SQL Editor, together with the four `20260923*` Agent 2 migrations, which the live project was missing. Every column of both tables was checked through the REST API, and anonymous inserts are refused by RLS. `tests/test_rls_multi_tenancy.py` now passes (36/36) against the live project.
- [x] Migration `<ts>_agent3_pgmq_queue.sql`: `pgmq.create('agent3_execution_jobs')`. Add the `enqueue_execution_job` / `read_next_execution_job` / `delete_execution_job_message` helpers to `queue.py` with the `$2::bigint` cast, and extend `tests/test_queue.py`. No endpoint, so no frontend pair.
  - *Done 2026-09-25:* `20260925110000_agent3_pgmq_queue.sql` was applied live in the SQL Editor, and `pgmq.list_queues()` shows `agent1_ingestion_jobs`, `agent2_planning_jobs` and `agent3_execution_jobs`. The message is `{execution_job_id, org_id}`, and the visibility timeout defaults to 3600 s (`AGENT3_QUEUE_VISIBILITY_TIMEOUT_SECONDS`, §11). `tests/test_queue.py` has 5 new tests and all 13 pass. A live round trip through the helpers (`enqueue` → `read_next` → `delete`) passes over the Session pooler URL, and the queue was left empty. The direct `db.<ref>.supabase.co` host is IPv6-only and didn't resolve from the dev machine, so `SUPABASE_DB_URL` has to be the pooler string there.
- [x] Migration `<ts>_agent3_output_bucket.sql`: private `agent3-output` bucket, with an org-scoped `select` policy only (pattern: `20260918040000_agent1_uploads_bucket.sql`). No endpoint, so no frontend pair.
  - *Done 2026-09-25:* `20260925120000_agent3_output_bucket.sql` was applied live in the SQL Editor. It was checked live with two throwaway orgs and users (9/9), then everything was removed:
    - the bucket is private, and the service role can write;
    - org A's user can download its own object and make a signed URL for it (the positive control);
    - org B's user gets `404` downloading it and an empty list for org A's folder;
    - neither user can upload (`403`, RLS), and org A's user can't delete.

    The Agent 3 tables and this bucket get a permanent test in the `test_rls_multi_tenancy.py` task below.
- [x] Executor skeleton `backend/document_analyzer/agent3/worker/run.py` plus a `document-analyzer-executor` script in `pyproject.toml`. Copy the `agent2/worker/run.py` shape:
  - `load_dotenv()` first;
  - the terminal-status guard;
  - the stage list with stub stages;
  - `mark_execution_running` / `_stage` / `_failed` / `_done` filtered by `id` + `org_id`;
  - message delete in `finally`;
  - startup checks for Docker, the runner image and gVisor (§10).

  Add `tests/test_agent3_worker_run.py`. No endpoint, so no frontend pair.
  - *Done 2026-09-25:*
    - `agent3/worker/run.py` and `agent3/worker/status.py`, with an `ExecutionState` dataclass.
    - The stub stages **raise** `NotImplementedError`, so a real job ends `failed` ("not implemented yet") and never `done` with nothing generated.
    - `check_sandbox_at_startup` calls `sandbox.check_sandbox_available` with the env params and `AGENT3_REQUIRE_GVISOR`, and logs a warning without `runsc`. There's no embedding check (§10).
    - The README has an "Executor" section.
    - 21 tests in `tests/test_agent3_worker_run.py`.
    - Checked live:
      - with the image missing, `uv run document-analyzer-executor` refuses to start and names the build command;
      - with the image present, it warns about gVisor, polls, dequeues a message for a nonexistent job, drops it as not found, and deletes it (the queue is left empty).
- [x] Extend `tests/test_rls_multi_tenancy.py`: one row per Agent 3 table under org A, invisible to org B, with a positive control. No endpoint, so no frontend pair.
  - *Done 2026-09-25:*
    - `_seed_agent3_chain` seeds one `execution_jobs` and one `unit_executions` row under org A, chained onto the Agent 2 plan and unit/phase clusters.
    - Both tables are in the parametrized "org B can't read" and "org A can" (positive control) checks.
    - New checks:
      - signed-in users can't insert into either table (RLS);
      - the `agent3-output` bucket: org A downloads its object (positive control), org B gets not-found and an empty listing, and neither user can upload.
    - Cleanup deletes the Agent 3 rows first and removes the bucket object.
    - 46/46 pass live, and no rows or objects are left afterwards.
- [x] Update `docs/skills.md` with Docker, gVisor and the runner image's pinned libraries, and why each was chosen. Include the image's distribution: a local build in development, and a private registry pinned by digest in production (`design_agent3.md` §7, "Where the image comes from"). No endpoint, so no frontend pair.
  - *Done 2026-09-25:* `skills.md` has a new "Agent 3 (Execute)" section:
    - Sandbox: Docker CLI, gVisor and the stdlib XML parser, with the reason each was chosen.
    - Runner image: every pinned package, why it's in the image, and the versions read from inside the built image (Python 3.14.7).
    - Where the image comes from: local build in development, registry by digest in production.
    - Queue, worker and storage, plus the Agent 3 decisions.
    - The title, the intro and the Agent 1 sandbox note now cover Agent 3, and `CLAUDE.md` says `skills.md` covers all three agents.

    Open point recorded there: the image's transitive dependencies aren't pinned (only the six top-level packages are).

## Phase 1 — Trigger & execution status

- [x] `POST /api/agent3/plans/{plan_id}/executions` (§9): `agent3/router.py`, mounted in `app.py`. Gates: `approved`, not stale (reuse `mark_stale_plans`), snapshot present, no active job (plus the `23505` race → `409`). Freezes `params` from §11, inserts `execution_jobs` plus a `pending` `unit_executions` row per plan item, and enqueues. Tests: happy path, each `409`, `404`, `401`.
  - *Done 2026-09-25:*
    - `agent3/router.py`, mounted in `app.py`. Gates in order: `404`, stale (`409`, and the plan is marked `stale`), not approved (`409`), no source snapshot (`409` "re-ingest"), no units (`409`), active job (`409`, plus the `23505` race).
    - Params frozen from §11. One `pending` `unit_executions` row per plan item.
    - If creating the units or enqueueing fails, the job is marked `failed`, so it can't block the plan through the one-active-job index.
    - `mark_stale_plans` / `is_stale` moved to `document_analyzer/shared/plan_staleness.py`, and Agent 2's router imports them from there.
    - New Agent 1 column `ingestion_jobs.source_snapshot_path` (`20260925130000_agent1_source_snapshot_path.sql`, `design.md` §2), always null until the snapshot upload task. It **must be applied live** before this endpoint is used there, or the snapshot check fails with a 500.
    - 21 tests in `tests/test_agent3_executions_api.py`.

### Frontend sync — Generate code
- [x] A "Generate code" action on `PlanPage`, shown only when the plan is `approved` and not stale. Otherwise it explains why ("approve every phase first", "plan is stale"). Show the backend's `409` verbatim. On `202`, call `navigateToExecution(execution_job_id)`.
  - *Done 2026-09-25:*
    - `GenerateCodeAction` in the `PlanPage` header. It's disabled with the reason for draft, in-review, rejected and stale plans (`generateCodeBlocker`).
    - `navigateToExecution` with the `execution` page param. `ExecutionStatusPage` is a placeholder until the next task.
    - Checked with `vite build` and in a browser against the real app over a seeded `FakeClient` (dev harness in the session scratchpad):
      - draft, in-review and stale plans show a disabled button with the right hint;
      - approved with no snapshot, and approved with a job already running, each show the backend's `409` verbatim;
      - approved lands on `?execution=<id>` with the job, 3 unit rows and one enqueue recorded.

- [ ] `GET /api/agent3/executions/{execution_job_id}` (§9). Tests: each status, `404`, `401`.

### Frontend sync — Execution status
- [ ] `ExecutionStatusPage` (`?execution=`): polls until terminal. `ProgressStages` with `prepare → units → assemble → final_test → done`, a count of units done out of the total from `counts`, `error` on failure, and a "View results" button on `done`.

- [ ] `GET /api/agent3/plans/{plan_id}/executions` (§9). Tests: ordering, other-org `404`.

### Frontend sync — Execution list
- [ ] A "Code generation runs" list on the approved `PlanPage` (status, counts, date), linking to each execution. Include an empty state.

## Phase 2 — Unit context & generation (worker only)

- [ ] Stage `prepare` (§3): plan re-check, ordered items, member sets (including `merged_cluster_ids`), entities and summaries pinned to `source_job_id`, snapshot download and unpack, resume of terminal units. Tests against `FakeClient`. No endpoint, so no frontend pair.
- [ ] `context.py` (§4 step 1): source slicing by `line..end_line`, exclusion of unverified summaries plus the flag, upstream symbol maps and signatures, and the `max_context_chars` cap leading to `error`. Unit tests with the fixture repo. No endpoint, so no frontend pair.
- [ ] Target-architecture template `agent3/template/` (§8): `main.py`, `events.py`, `wp_compat/` (a real implementation and an in-memory fake), `db.py`, `tests/conftest.py`. The template has its own tests, which run in the sandbox. No endpoint, so no frontend pair.
- [ ] `generate.py` (§4 step 2): structured-output generation, then path, `ast.parse` and `symbol_map`-coverage validation. The retry loop feeds back failures up to `max_attempts`. Tests with a fake LLM client (valid output, a bad path, a syntax error, a missing symbol, recovery on the second attempt). No endpoint, so no frontend pair.

## Phase 3 — Tests & sandboxed verification (worker only)

- [ ] `tests_gen.py` contract tests (§6): every row of the mapping table. The same contract must give byte-identical tests. Golden-file tests over the fixture repo's contracts. No endpoint, so no frontend pair.
- [ ] Behaviour-test generation (LLM, `@pytest.mark.behaviour`) with `side_effect_signals` hints. Test with a fake LLM client. No endpoint, so no frontend pair.
- [ ] Stage `units` end to end (§4): workspace assembly with transitive dependencies, a sandbox run, verdict and `test_summary` split by marker, per-attempt storage upload, and the `unit_executions` update. Also the failed-dependency stubs and the `built_on_failed_dependency` / `stubbed` flags. Test: the fixture pipeline through Agent 3 with a fake LLM that returns canned passing and failing code. No endpoint, so no frontend pair.
- [ ] Non-blocking check: one unit forced to `error` and one to `failed_verification`. The job still reaches `done`, the dependents are generated, and the counts are correct. No endpoint, so no frontend pair.

## Phase 4 — Assembly & final test (worker only)

- [ ] Stages `assemble` and `final_test` (§5): project tree, `app/units/__init__.py`, the README verdict table, a full-suite sandbox run, the zip upload, and `output_zip_path` / `counts` / `final_test_summary`. Test: unzip the fixture run's output and check the layout and that `pytest` passes in the sandbox. No endpoint, so no frontend pair.

## Phase 5 — Review API & UI

- [ ] `GET /api/agent3/executions/{id}/units` (§9): verdicts grouped by phase, with merged-item labels following Agent 2's stand-in rules. Tests.

### Frontend sync — Results
- [ ] `ExecutionResultsPage`: phases, then units with verdict pills (`passed` / `failed_verification` / `error`), flag badges, attempts and contract vs behaviour counts. Include empty and error states.

- [ ] `GET /api/agent3/executions/{id}/units/{cluster_id}` (§9): legacy slices from the snapshot, plus the last attempt's files, tests and log from storage. `404` for a unit not in this job. Tests.

### Frontend sync — Unit detail
- [ ] `ExecutionUnitPage` (`?execution=&unit=`): legacy source next to the generated code (a file picker per side), the tests, a collapsible log, the symbol map and `last_error`.

- [ ] `GET /api/agent3/executions/{id}/download` (§9): a 5-minute signed URL. `409` until the output exists. Tests.

### Frontend sync — Download
- [ ] A "Download project (.zip)" button on `ExecutionResultsPage`, disabled with a reason until the job is `done`.

## Phase 6 — Hardening

- [ ] Write-path multi-tenancy test for the executor (pattern: `tests/test_worker_write_path_multi_tenancy.py`): repo content or LLM output containing another org's id never changes the stamped `org_id` or the storage prefix. An update with the wrong `org_id` is a no-op. Include a positive control. No endpoint, so no frontend pair.
- [ ] Sandbox abuse suite with generated code that tries network access, fork bombs, huge writes to `/tmp`, reading `/proc/1/environ` or host paths, and `import ctypes`. Each ends `failed_verification` / `error`, the job completes, and nothing leaks into the log. No endpoint, so no frontend pair.
- [ ] Redelivery: kill the worker mid-`units`. On restart, the terminal units are kept, the rest run, and there are no duplicate `unit_executions` rows. No endpoint, so no frontend pair.
- [ ] Project delete covers Agent 3 (`tasks.md` item): rows plus `agent3-output` objects are removed, and `409` while an execution is active. Frontend check: `DeleteProjectAction`'s confirm text mentions generated code.
- [ ] Production runner image and host (`design_agent3.md` §7, "Where the image comes from"), deferred on 2026-09-25 until there's a production host:
  - choose a private registry (GHCR, ACR or Docker Hub);
  - push `galvanise-agent3-runner` with a version tag;
  - set `AGENT3_RUNNER_IMAGE` to its `@sha256:` digest;
  - make the deploy step `docker pull` that digest before starting the executor;
  - install gVisor and set `AGENT3_SANDBOX_RUNTIME=runsc` and `AGENT3_REQUIRE_GVISOR=true`;
  - run `tests/test_agent3_sandbox.py` on the host, including `test_live_gvisor_runtime`, which has only ever been skipped on Docker Desktop.

  No endpoint, so no frontend pair.
- [ ] A larger run over a synthetic plan (reuse `tests/test_load_large_repo.py`'s generator through Agent 2). Record per-unit time, tokens and total cost in `design_agent3.md`, and use them to settle specs §9 (attempts, timeout, parallelism). No endpoint, so no frontend pair.
- [ ] Frontend audit across `ExecutionStatusPage`, `ExecutionResultsPage`, `ExecutionUnitPage` and the `PlanPage` additions: loading, empty and error states, keyboard reachability, and readability of long logs.

---

## Cross-cutting: keeping backend and frontend in sync

Whenever you add or change an `/api/agent3/...` endpoint:
1. Update `design_agent3.md` §9 first.
2. Add the backend checkbox here.
3. Add the paired frontend checkbox directly under it.
4. Don't check off the backend box until the frontend can exercise it.
