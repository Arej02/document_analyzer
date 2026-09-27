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
| `counts` | jsonb, nullable | `{total, passed, failed_verification, error, built_on_failed_dependency}` |
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
5. Resume: any `unit_executions` row already terminal (a redelivered message) keeps its result and symbol map, and is skipped by stage `units`.

## 4. Stage `units`: per-unit loop

For each non-terminal unit, in order:

1. **Context** (`context.py`, specs FR3):
   - Source slices per member: `file_path` lines `line..end_line` from the snapshot, with the class header included for methods.
   - `raw_facts` and the test contract.
   - The cluster report text (or the constituent reports for a merged item).
   - For each `outgoing_dependencies.to_unit_id`, that unit's symbol map plus the public signatures from its generated code.
   - The §8 rules.

   Summaries of `unverified_member_ids` are dropped, and the unit is flagged `unverified_inputs`. If the total goes over `max_context_chars`, the unit ends with `error`.
2. **Generate** (`generate.py`, specs FR4): an Azure OpenAI call (client from `worker/summarize.py::get_azure_openai_client`) with structured output `{files: [{path, content}], symbol_map: {...}}`. Then validate:
   - every path is under `app/units/{unit_slug}/`;
   - every file passes `ast.parse`;
   - `symbol_map` covers every `public_interface.entity_id`.

   A validation failure counts as a failed attempt.
3. **Tests** (`tests_gen.py`, specs FR5): contract tests from §6, plus one LLM call for behaviour tests (given the legacy slices and the generated code), written under `tests/units/{unit_slug}/`.
4. **Run** (`sandbox.py`, §7): build a workspace containing:
   - the template;
   - the last attempt of every unit this one depends on (directly or transitively);
   - this unit's code and tests.

   Run pytest in the sandbox with JUnit XML output, then parse the results.
5. **Retry**: on failure, add the validation errors or the failing test names plus truncated assertion output to the next generation call. Stop at `max_attempts`.
6. **Persist**: upload the attempt's files, log and junit to storage (every attempt, not just the last), then update `unit_executions` (`status`, `attempts`, `symbol_map`, `test_summary`, `last_error`, `flags`, `storage_prefix`), filtered by `id` and `org_id`.

**Failed dependencies (specs FR7):** if a dependency ended `error` with no symbol map, stage `units` generates a stub module for it from the contract's `public_interface` (with the correct names and params, and bodies that `raise NotImplementedError`). The dependent is flagged `built_on_failed_dependency`, and the stubbed unit is flagged `stubbed`.

## 5. Stages `assemble` and `final_test`

- `assemble.py` builds the project tree from `template/` plus every unit's last attempt (stubs for `error` units).
  - It writes `app/units/__init__.py`, which imports every unit so hook registrations run at startup.
  - It writes `README.md` with a table of units, phases, verdicts and flags.
- `final_test` runs the whole `tests/` suite once in the sandbox (with a longer timeout, `sandbox_timeout_seconds × 5`) and stores `final_test_summary`. Then it zips the tree to `{org_id}/{execution_job_id}/project.zip` and sets `output_zip_path`, `counts` and `status = done`.
- A failing final suite doesn't fail the job. It's reported. The job only fails on infrastructure errors (storage, DB, sandbox unavailable).

## 6. Test contract → pytest mapping (deterministic)

Built from `migration_plan_items.test_contract` (`design_agent2.md` §10) and the unit's symbol map. Every generated file is sorted and stable.

| Contract field | Generated contract test |
|---|---|
| `public_interface[]` | For each entry, the symbol from `symbol_map` is importable and callable. Parameter count and names match `params` (snake_cased, defaults allowed). For `kind = method`, it's defined on the mapped class. |
| `incoming_call_sites[]` | For each calling unit, the call site's target symbol is exported at the mapped path. This checks that downstream units can still import what they call. |
| `outgoing_dependencies[]` | The unit imports each dependency only through that unit's symbol-mapped module, never by re-implementing it. Checked by an AST scan of the unit's code. |
| `external_calls[]` | Each legacy `wp_*` / PHP builtin call routes through `wp_compat` (AST scan). Calling the public functions with `wp_compat`'s in-memory fake records the expected external calls, as a smoke test only. |
| `hooks_bound[]` | Importing the unit registers a handler for each hook name on the event dispatcher (`app.events.handlers_for(name)` is non-empty). |
| `side_effect_signals[]` | These members get a behaviour test that must assert on `wp_compat` or DB state, not only on return values. This is a hint passed to the behaviour-test prompt, not a generated test. |
| `unverified_member_ids[]` | No test. The unit is flagged `unverified_inputs` and the prompt leaves those summaries out. |
| `merged_cluster_ids[]` | No test. It only widens the member set in §3. |

Behaviour tests (LLM) are marked `@pytest.mark.behaviour`, and contract tests `@pytest.mark.contract`, so `test_summary` can split them.

## 7. Sandbox

**Runner image** `galvanise-agent3-runner`: `python:3.14-slim` with pinned `fastapi`, `pydantic`, `sqlalchemy`, `httpx`, `pytest`, `pytest-timeout`. It's built from `backend/agent3_runner/Dockerfile`, and the tag is frozen in `params.runner_image`. There's no pip install at run time, because there's no network.

**Where the image comes from.**
- **Development:** built locally with `docker build -t galvanise-agent3-runner:0.1 backend/agent3_runner`. When the image is missing, the executor refuses to start and prints that build command (§10).
- **Docker Desktop name lookup (2026-09-25):** on Docker Desktop 29.6.1 with the containerd image store, `docker image inspect galvanise-agent3-runner:0.1` intermittently answers "No such image" for an image that exists. `docker image ls` lists it, `docker run` resolves it, and the fully qualified `docker.io/library/galvanise-agent3-runner:0.1` always inspects. An earlier "image deleted" diagnosis that day was this bug. `check_sandbox_available` therefore retries a missed short name under its qualified name before reporting the image missing (`sandbox.qualified_image_name`).
- **Production:** push the image to a private registry and set `AGENT3_RUNNER_IMAGE` to its digest (`<registry>/galvanise-agent3-runner@sha256:…`), not a tag.
  - A digest can't be re-pointed, so the image frozen in `params.runner_image` is exactly the one that ran. That keeps runs reproducible and auditable (specs NFR5).
  - A lost image can be pulled back unchanged.
  - `sandbox.py` runs with `--pull never`, so the deploy step pulls the image before the executor starts, and the startup check confirms it's present.
  - Every change to `requirements.txt` or the Dockerfile means a new tag and a new digest, never an overwrite.

**Invocation** (`sandbox.py`, via the Docker CLI, the same way Agent 1 shells out to `git` and `php`):

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
  --mount type=bind,src={workspace},dst=/work,readonly -w /work
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
- `SandboxUnavailableError` covers infrastructure errors: no Docker binary, daemon down, image missing, gVisor required but absent, or `docker run` exiting 125/126/127 without the sentinel. The worker fails the *job* on it and never turns it into a unit verdict.
- **gVisor:** `--runtime runsc` is added when `AGENT3_SANDBOX_RUNTIME=runsc`. The worker refuses to start if `AGENT3_REQUIRE_GVISOR=true` and `runsc` isn't registered with Docker (production setting). On a developer machine (for example Docker Desktop on Windows) it runs with the default runtime, keeping every other flag, and logs a warning.
- The worker never imports, execs or `eval`s generated code in its own process. Parsing with `ast` is the only thing it does with it.

**Known limitation: the sandbox protects the host, not the verdict.** Generated code runs in the same process as pytest, so it can fake its own results. It can monkeypatch pytest, rewrite `/tmp/junit.xml`, or read the sentinel from `/proc/1/cmdline` and print a forged report. Verdicts are therefore advisory. The backstops are the reviewer UI (the code is shown next to its tests and log) and the final assembled run (§5), where one unit can't easily fake another unit's tests. None of this lets code out of the container.

**Docker Desktop vs gVisor (2026-09-25).** Docker Desktop on Windows (WSL 2 backend) doesn't ship `runsc`, and registering it inside Docker Desktop's VM isn't supported. Developer machines therefore run with the default `runc` runtime, keeping every other flag above. gVisor is only verified on a Linux production host (`test_live_gvisor_runtime` skips elsewhere). Verified live on Docker Desktop 29.6.1 (engine runtimes: `runc`, `io.containerd.runc.v2`, `nvidia`; no `runsc`) with `tests/test_agent3_sandbox.py`: 8 of 8 smoke tests pass, and the gVisor test is skipped. Observed: allocating 512 MB under a 256 MB limit is OOM-killed in about 2 s (exit 137 → `killed`, no JUnit), a `sleep(600)` test is `timed_out` after the 5 s limit and its container is gone afterwards, and 1,000 threads hit the 256 pids limit (`RuntimeError`).

**Sandbox smoke tests (blocking before any generation work)**, in `tests/test_agent3_sandbox.py`, run live against the image:
1. A test that opens a socket or resolves DNS fails.
2. A test that allocates beyond the memory limit is killed, and a thread bomb hits the pids limit.
3. A test that sleeps past the timeout is killed, and its container is removed.
4. A write outside `/tmp` (to `/work` or `/etc`) fails, while `/tmp` is writable.

Plus: a positive control (one pass, one fail reported correctly), the uid is 65534, a host env canary isn't visible inside the container, and a gVisor run (skipped without `runsc`).

## 8. Target architecture (WordPress → FastAPI)

The rules every generation prompt includes. This is the contract between units, so changing it is a design change.

```
app/
  main.py              # FastAPI app; includes routers; imports app.units
  events.py            # hook dispatcher: on(name, priority)(fn), fire(name, *args), apply_filters(name, value, *args)
  wp_compat/           # fixed template: options, post/user meta, transients, http (wp_remote_*), escaping, i18n no-ops
  db.py                # SQLAlchemy engine/session dependency ($wpdb replacement)
  models/              # SQLAlchemy models for CPTs / post meta (generated by the unit that owns the schema entities)
  units/{unit_slug}/   # one package per plan item; only files the unit generated
tests/
  conftest.py          # wp_compat in-memory fake, SQLite session, event dispatcher reset
  units/{unit_slug}/test_contract.py, test_behaviour.py
```

| Legacy construct | Generated form |
|---|---|
| Plain function | A module-level function in the unit package, snake_case name, type hints from `raw_facts.params` where known. |
| Class and methods | A Python class with the same methods. Static methods stay static. |
| `add_action` / `add_filter` | `@events.on("hook_name", priority=n)` at module level. `do_action` → `events.fire`, `apply_filters` → `events.apply_filters`. |
| `register_post_type`, post meta | SQLAlchemy model in `app/models/{name}.py`, written by the unit owning the `schema` entity. |
| `$wpdb` queries | SQLAlchemy via `app.db` session, parameterized. No raw string SQL built from input. |
| `add_shortcode`, REST routes, `admin-ajax` handlers | An `APIRouter` in the unit package, which `app.main` picks up by convention (`router` attribute). |
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

## 10. Worker wiring

`agent3/worker/run.py::process_execution_job`:
1. Terminal-status guard.
2. `mark_execution_running`.
3. Run `prepare → units → assemble → final_test` with `mark_execution_stage` before each stage.
4. `mark_execution_done`.

Any exception leads to `mark_execution_failed`. The message is deleted in `finally`. `load_dotenv()` runs before imports (`design.md` §3 bug #1). At startup, `run_forever` checks that Docker is reachable and that the runner image exists, plus the gVisor check (§7). It doesn't need the embedding config check, because Agent 3 doesn't embed.

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
