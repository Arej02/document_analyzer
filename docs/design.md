# Agent 1 (Understand)

Architecture detail supporting `specs.md`. This is the doc to update whenever a design decision changes — code should follow this, not the other way around.

## 1. High-level flow

```
Upload (zip/tar) or GitHub URL
        │
        ▼
  [FastAPI: create job] ──► job row in Supabase (status: queued)
        │
        ▼
  [Async worker picks up job]
        │
        ├─► Ingest/unpack or clone repo
        ├─► Detect language/framework signals
        ├─► AST-extract (Extractor protocol) ──► extracted_entities, entity_edges
        ├─► Boilerplate filter (log filtered files)
        ├─► Bottom-up LLM summarization ──► knowledge_summaries (draft)
        ├─► Grounding checker (retry up to cap, else flag unverified)
        └─► summary_cache updated (content hash → summary)
        │
        ▼
  job row updated (status: done | failed), result queryable
        │
        ▼
  Frontend polls/subscribes ──► shows knowledge base browser
```

No step in this pipeline executes ingested code. Structural extraction (AST-extract) is static parsing only — there is no code-sampling/execution sandbox in Agent 1 (deliberate MVP scope cut, see `specs.md` NFR2).

## 1a. Auth (decided)

- **Frontend**: authenticates via Supabase Auth (email/OAuth, whatever providers are enabled on the Supabase project). The Supabase client SDK holds the session and attaches the Supabase-issued JWT (`access_token`) to every request to the FastAPI backend as a `Bearer` header.
- **Backend**: FastAPI validates the incoming JWT against Supabase's signing key (JWKS, or the project JWT secret depending on Supabase Auth config) on every request — this is a dependency, not per-route boilerplate. `org_id` is never accepted as a request parameter (see §4); it's resolved server-side from the validated token.
- **`org_id` resolution (decided): lookup table, not a custom JWT claim.** A custom claim would need a Supabase Auth Hook configured by hand in the dashboard — an out-of-band manual step outside migrations. Instead, `org_members` (`user_id` PK → `org_id`, one org per user for MVP) maps `auth.uid()` to `org_id`. RLS policies resolve it via a `current_org_id()` SQL function (`security definer`, reads `org_members` for `auth.uid()`) so every policy expression stays a one-liner. Revisit if multi-org-per-user is ever needed.
- The async worker's service-role connection (bypasses RLS, described below) is a separate credential from this request-scoped, JWT-validated client — never conflate the two paths.

## 2. Data model (Supabase Postgres)

All tables include `org_id` (RLS-scoped), `created_at`, `updated_at`.

RLS is enforced on every table below via the authenticated session for reads. Writes during ingestion come from the async worker, which runs on a service-role connection that bypasses RLS by construction — it is the sole write path outside the authenticated API and is solely responsible for stamping every inserted row with the correct `org_id`, taken from the job's own `org_id`, never from ingested repo content. Any bug here is a cross-tenant data leak; treat this path as security-critical.

### `projects`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | stable identity for a repo across multiple ingestion runs |
| `org_id` | uuid | RLS scope |
| `source_type` | enum(`upload`, `github`) | |
| `source_ref` | text | storage path prefix for upload, or GitHub repo URL — used to match re-ingestions to this project |
| `latest_job_id` | uuid, FK → `ingestion_jobs`, nullable | the current snapshot; null until the first job completes |

### `ingestion_jobs`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | job id, returned to frontend on submit |
| `org_id` | uuid | RLS scope |
| `project_id` | uuid, FK → `projects` | resolved (matched or created) before the job row is inserted |
| `source_type` | enum(`upload`, `github`) | |
| `source_ref` | text | storage path for upload, or GitHub URL |
| `github_token_ref` | text, nullable | reference to a short-lived secret scoped to this job's lifetime; deleted on job completion (success or failure), never persisted long-term |
| `status` | enum(`queued`, `running`, `done`, `failed`) | |
| `progress_stage` | text, nullable | e.g. `extracting`, `summarizing`, `grounding` — for UI progress display |
| `error` | text, nullable | populated on `failed` |
| `changed_file_count` | int, nullable | written by `worker/diff.py` once extraction is persisted; null until then. FR6's file diff against the project's previous `done` job — see §3 |
| `unchanged_file_count` | int, nullable | same as above; a file whose entities' `content_hash` all matched the previous job |
| `removed_file_count` | int, nullable | same as above; a file the previous job had entities for that this job has none for |

### `extracted_entities`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | |
| `org_id` | uuid | |
| `job_id` | uuid, FK → `ingestion_jobs` | |
| `entity_type` | enum(`function`, `class`, `route`, `schema`, `hook`, `dependency`) | |
| `name` | text | |
| `file_path` | text | |
| `raw_facts` | jsonb | structural facts from the extractor, pre-LLM |
| `content_hash` | text | drives `summary_cache` lookups |

### `entity_edges`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | |
| `org_id` | uuid | |
| `job_id` | uuid, FK | |
| `from_entity_id` | uuid, FK → `extracted_entities` | |
| `to_entity_id` | uuid, FK → `extracted_entities`, nullable | nullable for edges to external/global targets |
| `edge_type` | enum(`calls`, `hook_binding`, `global`, `include`) | |

### `filtered_files`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | |
| `org_id` | uuid | |
| `job_id` | uuid, FK → `ingestion_jobs` | |
| `file_path` | text | path relative to the repo root that the boilerplate filter excluded from extraction |
| `reason` | text | short machine-readable reason, e.g. `wordpress_core`, `composer_vendor_directory`, `node_modules_directory`, `vcs_directory` |

Queryable record of FR3's boilerplate filter (§ below) — every file it excludes gets a row here rather than silently vanishing, so "why isn't X in the knowledge base" is answerable per job. Written by the worker in the same batch style as `extracted_entities`/`entity_edges`; read-only for authenticated users, same RLS pattern.

### `secret_findings`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | |
| `org_id` | uuid | |
| `job_id` | uuid, FK → `ingestion_jobs` | |
| `entity_id` | uuid, FK → `extracted_entities` | the entity whose `raw_facts` contained the match — the match itself is never stored here, only where/why |
| `field_path` | text | dotted path into that entity's `raw_facts` where the match was found, e.g. `args.password` |
| `reason` | text | which heuristic matched, e.g. `aws_access_key`, `private_key_block`, `hardcoded_credential_field` |

specs.md NFR2's secret/credential scan: every extracted string is scanned before `extracted_entities.raw_facts` is persisted; a match is redacted in place (`[REDACTED:<reason>]`) before persisting, and the finding is logged here for audit — never the raw secret value, and never a reason to fail the entity or the job. Same write/RLS pattern as `filtered_files`.

### `knowledge_summaries`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | |
| `org_id` | uuid | |
| `job_id` | uuid, FK | |
| `level` | enum(`entity`, `module`, `subsystem`, `repo`) | |
| `entity_id` | uuid, FK → `extracted_entities`, nullable | null for module/subsystem/repo level |
| `summary_text` | text | |
| `risk_level` | enum(`low`, `medium`, `high`) | |
| `embedding` | vector | pgvector column, dimension = pinned model's output size |
| `grounding_status` | enum(`verified`, `unverified`) | |

### `summary_cache`
| column | type | notes |
|---|---|---|
| `content_hash` | text, PK (+ org_id) | |
| `org_id` | uuid | |
| `summary_id` | uuid, FK → `knowledge_summaries` | reused on unchanged content |

specs.md FR6's cache: `content_hash` here is `extracted_entities.content_hash` (persist.py §Phase 2 above) — the only content_hash concept the data model defines, so this cache operates at the entity level only; module/subsystem/repo cache invalidation is the separate, not-yet-built "cascading re-summarization" task (Phase 4). `backend/document_analyzer/worker/summary_cache.py`: `lookup_cached_summary`/`write_through_cache` are the raw read/write against this table; `summarize_entity_cached` wires them around `summarize_entity` + `persist_summary` — a cache hit still makes no LLM call and generates no new embedding, but (as of tasks.md Phase 5's "confirm summary_cache correctly reuses" — a real gap found and fixed there, not just a re-verification) it does write one new, cheap `knowledge_summaries` row via `copy_cached_summary_forward`, scoped to the *current* job/entity, copying `summary_text`/`risk_level`/`embedding`/`grounding_status` from the cached row. Without that, a re-ingested job's unchanged entity had no `knowledge_summaries` row of its own for `GET /jobs/{job_id}/entities`/`.../entities/{entity_id}` (both join on `job_id` + `entity_id`, §4) to find — reused, but unqueryable for that job. `worker/run.py::_process_one_entity` does the same thing inline (it can't call `summarize_entity_cached` directly, since it also needs to wrap generation in the grounding retry loop, §3) via the same `copy_cached_summary_forward` helper.

### `embedding_config`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | |
| `model_name` | text | |
| `model_version` | text | |
| `dimension` | int | output size of this model's embeddings; if a new active row's dimension differs from the current `knowledge_summaries.embedding` column, that's a schema migration (new column/table), not just flipping `active` — plan the backfill accordingly |
| `active` | boolean | only one row active at a time; changing this is a migration, not a deploy |

Not org-scoped — it's global model config, not tenant data. RLS is still enabled (every table has it), but the policy is a blanket read for authenticated users with writes restricted to the service role, rather than an `org_id` filter.

**Pinned embedding model (decided):** `sentence-transformers/all-MiniLM-L6-v2`, pinned to model revision (commit sha) `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, dimension 384. Chosen for zero API cost — it runs locally, so summary text (derived from potentially private ingested code) never leaves your own infra, and it's cheap enough on CPU to not need GPU provisioning for an MVP. The active `embedding_config` row and the `knowledge_summaries.embedding` column type (`vector(384)`) are both set from this pin in the same migration — see `supabase/migrations/20260918020000_embedding_config.sql`. Changing this later is an explicit schema migration + backfill (per the `dimension` column note above), never a silent config flip. Loaded/run via `backend/document_analyzer/worker/embed.py::embed_text` (`sentence-transformers` dependency), mirroring the same pin in code.

**Drift enforcement (tasks.md Phase 6):** the "never a silent config flip" rule above is checked, not just documented — `backend/document_analyzer/worker/embedding_config_check.py::check_embedding_config` reads the active `embedding_config` row and classifies it against embed.py's own pinned constants: a `dimension` mismatch is `schema_migration_required` (the `vector(N)` column type itself must change, plus a full backfill — this wins over a same-dimension model mismatch, since it already implies one too), a `model_name`/`model_version` mismatch at the *same* dimension is `data_backfill_required` (existing rows are still valid vectors, just from a superseded model), and anything else is `in_sync`. `assert_embedding_config_in_sync` raises `EmbeddingConfigDriftError` for either drift case and is called once at `worker/run.py::run_forever` startup, before the poll loop — the worker refuses to start at all under a drifted config rather than silently embedding under a pin nobody signed off on.

**Summarization LLM (decided):** Azure OpenAI (deployment configured via `AZURE_OPENAI_ENDPOINT`/`AZURE_OPENAI_API_KEY`/`AZURE_OPENAI_API_VERSION`/`AZURE_OPENAI_DEPLOYMENT`, see `.env.example`) — user-chosen, credentials already provisioned. Unlike the embedding model above, this is a cloud call: entity/module/subsystem/repo facts are sent to Azure OpenAI per summarization call, an accepted tradeoff for LLM-quality summaries over a local model. Implemented in `backend/document_analyzer/worker/summarize.py::summarize_entity` (entity level), `backend/document_analyzer/worker/summarize_module.py::summarize_module` (module level), and `backend/document_analyzer/worker/summarize_subsystem.py::summarize_subsystem`/`summarize_repo` (subsystem and repo levels).

**Module grouping (decided):** a "module" is one file — every `extracted_entities` row sharing the same `file_path` belongs to the same module. No other module concept exists in the data model, so this is the natural (and only) grouping available without inventing a new entity type.

**Entity-summary fan-out concurrency (decided, specs.md NFR5):** capped at `AGENT1_SUMMARY_CONCURRENCY` concurrent in-flight LLM calls (default 10 — starting default, revisit after Phase 6 load testing), implemented in `backend/document_analyzer/worker/summarize_fanout.py::summarize_entities` via a bounded thread pool. Scoped to the entity level only — module/subsystem/repo levels have far fewer, dependent calls (each must wait on its own children), so they don't need the same treatment.

**Subsystem grouping (decided):** a "subsystem" is the top-level directory component of its modules' `file_path` — e.g. `includes/checkout.php` and `includes/cart.php` both belong to subsystem `includes`; a file with no directory component (repo root) belongs to the sentinel subsystem `(root)`. `summarize_subsystem.py::subsystem_for_file` is the single source of truth for this grouping. The repo level folds every subsystem summary (never modules directly — specs.md FR4's "one level up at a time").

**Grounding checker (specs.md FR5, decided):** level-agnostic — `backend/document_analyzer/worker/ground.py::check_grounding` takes the same `facts` JSON payload the summarizer call for that level was scoped to (entity `raw_facts`, or a module/subsystem/repo's children-summaries payload) plus the generated `summary_text`/`risk_level`/`risk_factors`, and makes one LLM call to decide whether every claim in the summary is supported by facts and whether `risk_level` understates a risk signal actually present in facts (an understated `risk_level` counts as an ungrounded claim, not a separate axis). Returns `grounded` or `ungrounded` (+ `issues`); a malformed LLM response raises `GroundingCheckError`, kept distinct from an `ungrounded` verdict so the retry loop can tell "the check itself broke" apart from "the check ran and failed the summary". Pure function, no Supabase access — `grounding_status` persistence, the done-gate, and cascading re-summarization are separate, not-yet-built Phase 4 tasks.

**Grounding retry loop (specs.md FR5, decided: 2 retries):** `backend/document_analyzer/worker/ground_retry.py::check_grounding_with_retry` wraps `check_grounding` around a caller-supplied `generate` callable (a zero-argument closure over whichever level's summarizer, e.g. `lambda: summarize_entity(entity, client=client)`) — this module stays decoupled from any concrete summarizer, same as `check_grounding` itself is decoupled from any concrete facts shape. On an ungrounded verdict, `generate` is called again for a fresh regenerated attempt and re-checked, up to `GROUNDING_RETRY_CAP` (env `AGENT1_GROUNDING_RETRY_CAP`, default 2 — starting default, revisit if the false-`unverified` rate is too high in practice) additional regenerations. Returns the last attempt's summary and verdict either way; a still-ungrounded result after the cap is not an error here — flagging `grounding_status = unverified` is the next task, below.

**Grounding status persistence (specs.md FR5, "continue rather than fail the job"):** `backend/document_analyzer/worker/grounding_status.py::persist_grounding_status` takes a `GroundingRetryResult` (ground_retry.py) for one already-persisted `knowledge_summaries` row and writes its final `grounding_status` column: `verified` when the retry loop ended grounded, `unverified` when the retry cap was exhausted without one (`grounding_status_for` does the mapping). This flips the row from `persist_summary.py`'s default-`unverified` draft state to its resolved state. No failure path is tied to the grounding *outcome* — an exhausted-retries result is written as `unverified` exactly like a grounded result is written as `verified`, never raised as an error, which is what satisfies "continue rather than fail the job" (CLAUDE.md: "Grounding checker failures never block ingestion"). Filtered by both `id` and `org_id` on the update, same defense-in-depth as `status.py`'s `mark_job_running`.

**Cascading re-summarization (specs.md FR6):** `backend/document_analyzer/worker/cascade.py::regenerate_ancestors` orders the regeneration a changed entity summary requires: its module (from module_summary.py, given the module's full current entity-summary set), then that module's subsystem (from summarize_subsystem.py, folding the just-regenerated module alongside already-current sibling modules), then the repo (folding the just-regenerated subsystem alongside already-current sibling subsystems) — never a stale child folded into a parent. Pure function, reusing Phase 3's summarizers as-is; siblings are supplied by the caller rather than queried. **Known gap, not addressed here:** `knowledge_summaries` has no column identifying which module/subsystem a `level = module`/`subsystem` row is for (only `entity_id`, null at those levels) — there is currently no query that can find "the existing module summary row for file X" to replace once this is wired to Supabase. Whoever wires this up needs to either add that identifying column (a schema migration) or resolve it some other way — flagged here rather than worked around silently.

**File diff against the previous job (specs.md FR6, "only changed files ... are re-extracted and re-summarized", tasks.md Phase 5):** `backend/document_analyzer/worker/diff.py::diff_against_previous_job` runs right after a job's own `extracted_entities` are persisted (content_hash is computed there, at insert time — persist.py's `content_hash_for`). It finds the project's most recent *other* `done` job (none, for a first-time ingestion — everything then counts as changed) and matches entities across the two jobs by `(file_path, name, entity_type)`: a match with an identical `content_hash` makes that file `unchanged`; anything else (content differs, or the entity/file is new) makes it `changed`; a file the previous job had entities for that this job has none for is `removed`. A file with even one changed/new entity in it is `changed` as a whole, never split. `persist_diff_counts` writes the three resulting counts onto the *current* job's own `ingestion_jobs` row (`changed_file_count`/`unchanged_file_count`/`removed_file_count`, §2) — the "12 files changed since last run" figure tasks.md's Phase 5 frontend task surfaces once it's built.

This module computes and persists the diff as an auditable record — it does **not** re-implement summary reuse itself. An unchanged entity's summary is already reused with no LLM call by the pre-existing, content_hash-keyed `summary_cache` lookup `worker/run.py::_process_one_entity` does before ever calling `summarize_entity` (Phase 3) — that reuse works for *any* content_hash match, not just ones from this project's own previous job, so it needs no help from this diff to do its job. **Known limitation, flagged rather than silently worked around:** re-*extraction* still runs unconditionally against the whole current source tree — `content_hash` can only be computed after parsing, and no raw per-file hash is persisted anywhere a future job could diff against before parsing starts (the entity-level `content_hash` remains "the only content_hash concept the data model defines" — see the `summary_cache` note above). Skipping the parse itself would need a new raw-file-hash concept (a schema change), and the parse has no LLM cost, so it isn't the resource this task is protecting.

**Job completion gate (specs.md FR5, "never blocks on a single bad one"):** `backend/document_analyzer/worker/job_completion.py::job_ready_for_completion` / `mark_job_done_if_ready`. Since `knowledge_summaries.grounding_status` is a NOT NULL `verified`/`unverified` enum with no third "pending" value, a job is ready for completion once every one of its `extracted_entities` rows has a matching entity-level `knowledge_summaries` row — that row's mere existence already means grounding + retry has resolved it one way or the other (grounding_status.py always writes one of the two). `mark_job_done_if_ready` checks that gate and, if met, transitions `ingestion_jobs.status -> done`, `progress_stage -> None`; it never looks at *which* grounding_status any row carries and never sets `status = failed` because of one, so a job where every summary ended up `unverified` completes exactly like one where every summary `verified`. Module/subsystem/repo-level summaries aren't part of the gate — entity-level completeness is the actual bottleneck (module+ summaries are each derived only after their own children are already done, per FR4's "one level up at a time").

On the transition actually happening (not on a no-op call), `mark_job_done_if_ready` also points `projects.latest_job_id` at this job (tasks.md Phase 5, `POST /projects/{project_id}/reingest`'s own note above about this being not-yet-built) — `_update_project_latest_job_id`, scoped by both `id` and `org_id` on the `projects` update, same defense-in-depth as every other worker write. Reads `project_id` off the just-updated `ingestion_jobs` row rather than a separate query. This is what makes `GET /api/agent1/projects`'s "current state per repo" (§4) actually track the most recent successful run once re-ingestion is used, rather than staying pinned to whichever job happened to create the project.

### Risk level heuristic (specs.md FR4)

`risk_level` must be explainable to an enterprise reviewer, not an opaque per-call LLM judgment. Two different mechanisms achieve this depending on level:

**Entity level** — each summarization call is given this fixed rubric (also embedded verbatim in `summarize.py`'s system prompt — keep both in sync) and must name which signal(s) matched as `risk_factors`:

- **high** — raw_facts show any of: a dynamic code execution call (`eval`, `create_function`, `call_user_func`/`call_user_func_array` with a variable callback), a shell/process call (`exec`, `shell_exec`, `system`, `passthru`, `proc_open`, `popen`), `unserialize()` on external input, a redacted credential marker (a string containing `[REDACTED:` anywhere in `raw_facts` — see `secret_findings`/NFR2), a raw SQL string built by concatenation/interpolation next to `SELECT`/`INSERT`/`UPDATE`/`DELETE`, or direct use of a superglobal (`$_GET`/`$_POST`/`$_REQUEST`/`$_COOKIE`) feeding into one of the above.
- **medium** — raw_facts show file-system writes (`file_put_contents`, `fopen` in a write mode, `unlink`), outbound network calls (`curl_*`, `file_get_contents` on an `http(s)` URL, `wp_remote_*`), a WordPress hook registration/invocation that runs on every request (e.g. `init`, `wp_loaded`, `template_redirect`), or a schema/CPT/meta registration that changes what data the site accepts — none of the high-risk signals above, but the entity still affects state or the outside world.
- **low** — everything else: pure computation, formatting, getters, or a hook binding to a narrow admin-only or display-only action with no state change.

**Module level (and up)** — *not* re-judged by an LLM against a rubric. By the time entities are folded into a module, their own `raw_facts` are no longer in the payload (only their summaries are, per FR4's "small regardless of repo size"), so there's nothing left for a rubric to pattern-match against — re-asking the LLM would be a guess, not a fact-check. Instead every level above entity takes the deterministic maximum of its children's `risk_level` (`low < medium < high`), with `risk_factors` naming which child/children carried that level — shared across levels via `risk_rollup.py::rollup_risk` (used by `summarize_module.py::compute_module_risk` and directly by `summarize_subsystem.py`'s `summarize_subsystem`/`summarize_repo`). This is at least as explainable as the entity rubric (it's traceable straight back to already-fact-checked entity-level risk), just computed in code rather than asked of the LLM. The LLM call at every level above entity is only ever responsible for the prose `summary_text`.

## 2a. Storage (zip/tar uploads)

**Bucket: `agent1-uploads`** (private) — backs the upload path of `POST /api/agent1/jobs`. Objects are stored under `{org_id}/{job_id}/{filename}`, so the same `org_id`-prefix RLS pattern used elsewhere applies directly (`(storage.foldername(name))[1] = current_org_id()::text`) — see `supabase/migrations/20260918040000_agent1_uploads_bucket.sql`. `source_ref` on `projects`/`ingestion_jobs` for an upload-sourced project is this object path. The async worker reads the object via its service-role connection (bypasses RLS, same as table access).

### `org_members`
Backs `org_id` resolution (§1a); not part of the ingestion pipeline itself.

| column | type | notes |
|---|---|---|
| `user_id` | uuid, PK, FK → `auth.users` | one org per user for MVP |
| `org_id` | uuid | |
| `created_at` | timestamptz | |

## 3. Job queue

**Decided: pgmq** (Postgres-native queue on the existing Supabase instance). No second infra dependency to operate/secure for an MVP; revisit only if Phase 6 load testing shows it's an actual throughput bottleneck.

The FastAPI layer only ever *enqueues* — it never runs extraction/summarization inline.

**Queue name: `agent1_ingestion_jobs`** — set up via `supabase/migrations/20260918030000_pgmq_job_queue.sql` (`create extension pgmq` + `pgmq.create('agent1_ingestion_jobs')`). Scoped to Agent 1 only; Agent 2/3 get their own queues when built, never sharing this one (§ core architectural rules — no direct agent-to-agent calls). `POST /api/agent1/jobs` calls `pgmq.send('agent1_ingestion_jobs', ...)` with the new job's `id`/`org_id` after inserting the `ingestion_jobs` row; the worker polls with `pgmq.read`/`pgmq.delete` (or `pgmq.pop`).

### Worker main loop

**Decided: a single long-lived process, run via `uv run document-analyzer-worker`** (`backend/document_analyzer/worker/run.py`), separate from `uvicorn` — the async worker CLAUDE.md's "Async by default" rule assumes exists. `run_forever()` polls `pgmq.read` (5s idle interval, configurable visibility timeout via `AGENT1_QUEUE_VISIBILITY_TIMEOUT_SECONDS`, default 1800s) and processes one job at a time, serially; `pgmq.delete` only runs after the job reaches a terminal state (`done` or `failed`) so a worker crash mid-job leaves the message to become visible again for retry, rather than losing it.

Per job, in order: `mark_job_running` → resolve `github_token_ref` (if any) → `fetch_source` → strip boilerplate (`filter_boilerplate`, then removed from disk before extraction) → run whichever `Extractor`s `.detect()` the repo (`PhpExtractor`, `WordPressExtractor`) → `persist_extraction_result` per extractor that ran → diff the job's now-persisted entities against the project's previous `done` job (`diff.py::diff_against_previous_job`, then `persist_diff_counts` — see "File diff" below) → fan entity-level summarization out (`AGENT1_SUMMARY_CONCURRENCY` concurrent workers), each entity going through `summary_cache` lookup, then (on a miss) `check_grounding_with_retry` around `summarize_entity` → `persist_summary` → `persist_grounding_status` → `write_through_cache` → fold module → subsystem → repo summaries the same grounded way (sequential, not fanned out — NFR5 only requires the fan-out at entity level) → `mark_job_done_if_ready`. `github_token_ref`'s secret is deleted, and the queue message removed, no matter how the job ends (`try/finally`).

`progress_stage` while `status = running`: `extracting` (fetch/detect/boilerplate/parse/persist — no LLM calls) → `summarizing` (the concurrent entity-level fan-out; grounding's regenerate-on-failure retry is itself part of generating an entity's final summary, not a separate phase) → `grounding` (the smaller, sequential module → subsystem → repo fold). A pipeline failure (bad URL, unparseable archive, malformed LLM response after retries are exhausted at the *check* level — not to be confused with a normal `ungrounded`→retry→`unverified` outcome, which is not a failure) sets `status = failed` with `ingestion_jobs.error`, matching the stage the job died at.

Deliberately out of scope for the first version, flagged rather than silently worked around: only one job processed at a time (no worker concurrency yet — revisit only if Phase 6 load testing shows it's a bottleneck, same call as pgmq itself above); a `summary_cache` hit's reconstructed `EntitySummary.risk_factors` is `[]` (the schema has no column for it, so a cached row can't recover the original list — only used for a rollup's explanatory naming, never the `risk_level` value itself).

**Bugs found and fixed running an actual live job through this loop** (tasks.md Phase 4, "verify a real end-to-end run"):
- `worker/run.py` never called `load_dotenv()` — only `app.py` did. The worker is a separate process from `uvicorn` (this section's whole premise), so it needs its own `.env` load; without it, every `os.environ[...]` read (`SUPABASE_DB_URL` first) raised `KeyError` before the loop could even start. Fixed by loading `.env` at the top of `run.py`, same pattern as `app.py`.
- `queue.py::delete_job_message` called `pgmq.delete($1, $2)` with an untyped second param; `pgmq.delete` has two overloads (`msg_id bigint` vs. `msg_ids bigint[]`), and Postgres can't resolve the ambiguity on its own (`AmbiguousFunctionError`) — this crashed `run_forever` itself (uncaught, outside `process_job`'s try/except), killing the whole worker process even though the job it had just processed was already handled. Fixed with an explicit `$2::bigint` cast.
- `_strip_boilerplate`'s `unlink()` raised `PermissionError` on git's own read-only pack/object files under Windows (POSIX only needs write access to the *directory* to unlink; Windows also checks the file's own read-only attribute). Fixed by clearing the flag (`chmod`) and retrying once — harmless on POSIX, since the flag there doesn't block unlink in the first place.

None of these needed a design decision — they're the same ordering/wiring described above, just made to actually run. Confirmed after the fixes: a real job against `https://github.com/php-fig/log` went `queued → running (extracting)` and completed fetch → boilerplate-strip → `PhpExtractor` → `persist_extraction_result` against the live Supabase project, then reached `progress_stage = summarizing` before failing on `AZURE_OPENAI_*` — the configured endpoint (`https://<resource>.services.ai.azure.com/api/projects/<project>`) is an Azure AI Foundry *project* endpoint, a different API surface than the classic `https://<resource>.openai.azure.com` resource endpoint the `openai` SDK's `AzureOpenAI` client (`summarize.py::get_azure_openai_client`) expects to append `/openai/deployments/<deployment>/...` onto — this is a live-credential/config issue in `.env`, not a code defect, and needs the classic resource endpoint (and a matching `api_version`) from the Azure portal before the summarization stage can be verified end-to-end.

## 4. API surface (what the frontend integrates against)

This is the contract `tasks.md` frontend tasks are built around. Adjust route prefixes to match your actual FastAPI router.

### `POST /api/agent1/jobs`
Create an ingestion job.

Request (upload):
```
multipart/form-data
  file: <zip or tar>
```
Request (GitHub):
```json
{
  "source_type": "github",
  "repo_url": "https://github.com/org/repo",
  "access_token": "optional, only for private repos"
}
```
Response:
```json
{ "job_id": "uuid", "status": "queued" }
```

### `GET /api/agent1/jobs/{job_id}`
Poll job status.
```json
{
  "job_id": "uuid",
  "status": "queued | running | done | failed",
  "progress_stage": "extracting | summarizing | grounding | null",
  "error": "string | null",
  "changed_file_count": "int | null",
  "unchanged_file_count": "int | null",
  "removed_file_count": "int | null"
}
```
The three counts are `null` until `worker/diff.py` runs (early in `extracting`, right after this job's own entities are persisted — see §3), and stay populated afterward regardless of how the job ends. tasks.md Phase 5's frontend task ("distinguishing a fresh ingestion from a cached/incremental one") is what actually reads these — `unchanged_file_count > 0` with `previous_job_id` having existed (implied by any file being `unchanged` at all) means at least some of this run reused prior work; a first-time ingestion for a project has `unchanged_file_count == 0` by construction (§3's "File diff" note).

### `GET /api/agent1/jobs/{job_id}/summary`
Only valid once `status == done`. Returns the repo-level summary plus counts, for a dashboard view.
```json
{
  "repo_summary": "text",
  "risk_level": "low | medium | high",
  "entity_counts": { "function": 120, "class": 34, "route": 18, "hook": 40 },
  "unverified_count": 3
}
```

### `GET /api/agent1/jobs/{job_id}/entities`
Paginated list of `extracted_entities` + their entity-level summary, for the knowledge base browser. Supports `?entity_type=` and `?risk_level=` filters — `risk_level` filters on the entity's own summary (`extracted_entities` itself has no `risk_level` column, only `knowledge_summaries` does). `?limit=` (default 50, max 200) and `?offset=` paginate. Unlike `/summary`, this endpoint doesn't gate on job status — the knowledge base is browsable incrementally as entities/summaries land, not just once a job reaches `done`.
```json
{
  "entities": [
    {
      "id": "uuid",
      "entity_type": "function",
      "name": "string",
      "file_path": "string",
      "summary_text": "string | null",
      "risk_level": "low | medium | high | null",
      "grounding_status": "verified | unverified | null"
    }
  ],
  "total_count": 128,
  "limit": 50,
  "offset": 0
}
```
`summary_text`/`risk_level`/`grounding_status` are `null` for an entity whose entity-level summary hasn't been persisted yet (e.g. querying mid-ingestion). Per-entity `raw_facts` and edges are the separate `GET .../entities/{entity_id}` detail endpoint, below.

### `GET /api/agent1/jobs/{job_id}/entities/{entity_id}`
Single entity detail: raw facts, summary, grounding status, and its edges (both directions). Doesn't gate on job status, same reasoning as `.../entities`.
```json
{
  "id": "uuid",
  "entity_type": "function",
  "name": "string",
  "file_path": "string",
  "raw_facts": {},
  "summary_text": "string | null",
  "risk_level": "low | medium | high | null",
  "grounding_status": "verified | unverified | null",
  "edges": {
    "outgoing": [
      { "id": "uuid", "edge_type": "calls", "to_entity": { "id": "uuid", "entity_type": "string", "name": "string", "file_path": "string" } | null }
    ],
    "incoming": [
      { "id": "uuid", "edge_type": "hook_binding", "from_entity": { "id": "uuid", "entity_type": "string", "name": "string", "file_path": "string" } }
    ]
  }
}
```
`edges.outgoing` are `entity_edges` rows where this entity is `from_entity_id` (`to_entity` is `null` for the documented nullable-`to_entity_id` case — an edge to an external/global target, §2); `edges.incoming` are rows where this entity is `to_entity_id` (`from_entity` always exists — `from_entity_id` is NOT NULL). Each side's other entity is a small stub (`id`/`entity_type`/`name`/`file_path`), not its own full detail — following an edge answers "what does this touch", not a second entity list.

### `GET /api/agent1/projects`
List projects for the org — the stable, cross-run view of "repos we've ingested." Each row includes `latest_job_id` and that job's status/summary counts, so the frontend can show "current state per repo" without knowing job IDs up front.

### `GET /api/agent1/projects/{project_id}`
Single project detail: source info, full job history (most recent first), and the latest job's status. Each job in that history also carries `changed_file_count`/`unchanged_file_count`/`removed_file_count`, same nullable-until-diffed shape as `GET /jobs/{job_id}` above — the run history list is exactly where "was this a fresh ingestion or an incremental one" (tasks.md Phase 5) is naturally read job-over-job.

### `POST /api/agent1/projects/{project_id}/reingest`
Re-ingest the same source (upload a new zip, or re-pull the GitHub URL). Creates a new `ingestion_jobs` row against the existing `project_id`, diffs against the project's previous job by `content_hash` (FR6), and updates `projects.latest_job_id` once the new job reaches `done`.

Request shape depends on the project's own `source_type` (not a caller-chosen option — a project's source is fixed at creation):
- `source_type = "github"`: re-pulls `project.source_ref` (the original repo URL) fresh. Body is optional JSON, `{"access_token": "string"}` for a private repo — `github_token_ref` is per-job (§2), so a prior job's token is never reused even if one existed.
- `source_type = "upload"`: there's no stored original content to "re-pull" from, so this requires a new archive in the request — same `multipart/form-data` shape as `POST /api/agent1/jobs`. A `404` on the project also covers "wrong source_type for this request shape" implicitly via the 400 the upload path already returns when no file is attached.

Response (`202`), same shape as `POST /api/agent1/jobs`:
```json
{ "job_id": "uuid", "status": "queued" }
```
`404` for a missing/another-org project, same indistinguishable-404 pattern as every other project/job lookup in this API.

**Scope note:** the endpoint above only creates the job row and enqueues it (tasks.md Phase 5's first task). The `content_hash` diff (`worker/diff.py`), confirmed `summary_cache` reuse, and the `projects.latest_job_id` update (`worker/job_completion.py::mark_job_done_if_ready`, once the new job reaches `done`) are all now built too — see §3's "File diff", "Job completion gate", and `summary_cache`'s own entry above. What's still true: extraction itself still re-parses the whole current source tree unconditionally on every re-ingestion (the diff step's known limitation, §3) — only the *summarization* half of "only re-extract/re-summarize changed files" is actually skipped for unchanged content.

> All endpoints are `org_id`-scoped via the authenticated session — never accept `org_id` as a request parameter. `POST /api/agent1/jobs` internally resolves (matches or creates) a `project` row before creating the job — Agent 2 and the frontend should generally query by `project_id`, not by a bare `job_id`, except when looking at one specific run's detail/history.

## 5. Extractor protocol (interface, not implementation)

```python
class Extractor(Protocol):
    def detect(self, repo_path: str) -> bool:
        """Return True if this extractor applies to the given repo."""

    def extract(self, repo_path: str) -> ExtractionResult:
        """Return entities + edges. Pure parsing, no LLM calls."""
```

Two implementations at launch: `PhpExtractor` (wraps `nikic/php-parser` subprocess) and `WordPressExtractor` (hooks/CPTs/meta). New language support = new class implementing this protocol, nothing else in the pipeline changes.

## 6. Frontend integration points

The existing React app (`frontend/`) needs, at minimum:

1. An upload/GitHub-URL form that calls `POST /api/agent1/jobs`.
2. A job status view that polls `GET /api/agent1/jobs/{job_id}` until `done`/`failed`.
3. A results view calling `GET /api/agent1/jobs/{job_id}/summary` and `.../entities` for the knowledge base browser.
4. A project detail view (`ProjectDetailPage`, `frontend/src/main.jsx`, tasks.md Phase 5) calling `GET /api/agent1/projects/{project_id}` for job history and `POST /api/agent1/projects/{project_id}/reingest` for the re-ingest action — the reachable home for `.../reingest` that `ProjectsList`'s rows link to by `project_id` (design.md §4's own "should generally query by project_id, not a bare job_id" guidance).

See `tasks.md` for the concrete, checkable steps — each backend milestone has a paired frontend milestone so the two stay in sync as you build.