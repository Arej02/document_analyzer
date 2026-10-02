# Assessment Report — Technical Design

Architecture detail supporting `specs_report.md` (referenced as *specs §N*). Like `design.md`, `design_agent2.md` and `design_agent3.md`, this is the doc to update whenever a design decision changes: code follows this file, not the other way around. Section references (§N) point into this file unless prefixed.

Status: **specified, not yet built** (2026-10-01). Build order is in `tasks_report.md`.

## 1. High-level flow

```
User clicks "Generate X-ray" / "Generate Full Assessment" on a project
        │
        ▼
 [FastAPI: POST /api/projects/{project_id}/reports  {tier}]
        │  gate: Agent 1 job done; for `full`, an eligible plan pinned to the same job (409 otherwise)
        ├─► report_generation_jobs row (queued, params frozen)
        └─► pgmq.send('report_generation_jobs', {report_job_id, org_id})
        │
        ▼
 [Report worker: document-analyzer-reporter]
   collect ──► estimate ──► summarize ──► render_html ──► render_pdf ──► publish
      │           │            │              │               │             │
      │           │            │              │               │             └─ assessment_reports row done
      │           │            │              │               └─ PDF container (network-less)
      │           │            │              └─ Jinja2 → one self-contained HTML (+ leak gate)
      │           │            └─ the only LLM call (executive summary), grounded + numeric check
      │           └─ pure estimator; calibration is the only DB-reading piece
      └─ read-only collectors over Agent 1 / 2 / 3 tables, explicit columns only

 Share flow:  POST /api/reports/{id}/share-links ─► token shown once, sha256 stored
              GET /api/public/reports/{token}    ─► stored HTML only, no auth, same 404 for every failure
```

The report is a read-only consumer, not a fourth agent: it is triggered by the user, never by an agent, and it never calls one.

## 2. Where it sits in the architecture

- **Read-only on Agents 1, 2 and 3** (and the cost tables). It writes only its own tables and bucket (§3). A gap in another agent's data is handled by degrading the section and stating it in §5.8 of the report, then fixed in that agent's tasks file (specs §13).
- **Own queue and worker**: pgmq queue `report_generation_jobs`, worker `uv run document-analyzer-reporter` (`report/worker/run.py::main`). Never shared with another agent. Helpers go in `report/queue.py` next to the others' (`enqueue_report_job`, `read_next_report_job`, `delete_report_job_message`) with the explicit `$2::bigint` cast (`design.md` §3 bug #2). Visibility timeout `REPORT_QUEUE_VISIBILITY_TIMEOUT_SECONDS`, default 900.
- **Module layout**: `backend/document_analyzer/report/` holds `router.py`, `public_router.py`, `config.py`, `calibration.py`, `estimator.py`, `collect.py`, `build.py`, `exec_summary.py`, `leak_gate.py`, `charts.py`, `render.py`, `templates/`, `queue.py` and `worker/`. `backend/report_renderer/` holds the PDF image.
- **Allowed imports**: pure helpers only, with no DB access or pipeline state: `risk_rollup`, `check_grounding_with_retry`, the secret scanner, `shared.plan_staleness.is_stale`, `embed`-free helpers. **Not** `mark_stale_plans`: it writes to `migration_plans`, so the report computes out-of-date on read (§10). Anything needing report-specific changes moves to `shared/` first.
- **Agent 1 / 2 / 3 never import `report/`.** The report router imports `get_cost_operator` from `shared/cost_access.py`.
- **Untrusted input**: repository text (summaries, paths, entity names) is attacker-controlled. It is escaped in rendering (§8), fenced in the LLM prompt (§6.4), and never executed. No ingested code runs anywhere.

## 3. Data model (Supabase Postgres)

All tables have `org_id` and RLS (`current_org_id()`). Worker writes use the service-role client, stamp `org_id` from the job row (never from repo content), and filter updates by `id` **and** `org_id`. One migration creates all four tables, the queue and the bucket (`tasks_report.md` Phase 1), plus the CHECK widening (§3.5).

### `report_generation_jobs`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | returned by `POST .../reports` |
| `org_id` | uuid | |
| `project_id` | uuid, FK → `projects` | |
| `tier` | text, check `xray`/`full` | |
| `source_job_id` | uuid, FK → `ingestion_jobs` | the project's latest `done` Agent 1 job at creation |
| `plan_id` | uuid, FK → `migration_plans`, nullable | `full` only |
| `plan_version` | int, nullable | recorded at creation |
| `status` | `job_status` | reuses Agent 1's enum |
| `progress_stage` | text, nullable, check in the §4 stage keys | |
| `error` | text, nullable | job-level failure only |
| `params` | jsonb | frozen at creation (§12) |
| `created_by` | uuid, FK → `auth.users` | |
| `created_at`, `updated_at` | timestamptz | |

Unique partial index: at most one `queued`/`running` job per `(project_id, tier)`. A second request gets `409`.

### `assessment_reports`
| column | type | notes |
|---|---|---|
| `id` | uuid, PK | |
| `org_id`, `project_id` | uuid | |
| `generation_job_id` | uuid, FK → `report_generation_jobs` | |
| `tier` | text | |
| `source_job_id` | uuid | the pinned Agent 1 job |
| `plan_id`, `plan_version`, `plan_status_at_generation` | nullable | `full` only |
| `report_json` | jsonb | §5. Customer-safe by construction (no internal cost, no source) |
| `schema_version`, `template_version`, `estimator_version` | text/int | pinned versions (also in `report_json.inputs`) |
| `exec_summary_status` | text, check `verified`/`unverified`/`withheld` | |
| `html_path`, `pdf_path` | text, nullable | object paths in the `reports` bucket |
| `pdf_status` | text, check `pending`/`done`/`failed` | a PDF failure leaves the report usable |
| `created_by`, `created_at` | | |

Insert-only for tenants: authenticated `select` by `current_org_id()`, no update or delete policy. Regenerating creates a new row. The only in-place change is the worker's own `pdf_status` / `pdf_path` / `html_path` writes through the service role (the spec's "generation status fields").

### `report_share_links`
`id, org_id, report_id` (FK → `assessment_reports` on delete cascade)`, token_hash` (unique, sha256 hex)`, created_by, created_at, expires_at` (not null)`, revoked_at, revoked_by, access_count, last_accessed_at`. Tenants may `select` their org's rows **without** `token_hash` (a column grant or a view). Every write goes through the API's service role.

### `report_internal_estimates`
`org_id, report_id` (PK pair)`, ai_cost_p50, ai_cost_p90, n, calibration jsonb, price_basis jsonb, created_at`. **RLS on, no tenant policy, `select` revoked from `anon` and `authenticated`**, the same pattern as `llm_invocations`. Read only through `get_cost_operator`. This table is why "the customer report has no internal cost" holds by construction (specs §1.2).

### Storage: bucket `reports`
Private. Objects at `{org_id}/{report_id}/report.html` and `{org_id}/{report_id}/report.pdf` (deterministic, overwritten on redelivery, never appended). Select policy for the owning org only, no tenant insert/update/delete. Pattern: `20260925120000_agent3_output_bucket.sql`.

### 3.5 Constraint widening (migration)
`llm_invocations` (`agent`, `job_type`, and `llm_invocations_agent_job_type_check`) and `llm_job_costs` (same three) gain `report` / `report`. `workflow_events.workflow_kind` gains `report_generation`. Python side: `shared/llm.py::_AGENT_JOB_TYPES` gets `"report": ("report",)`, and `shared/workflow_events.py::VALID_WORKFLOW_KINDS` gets `report_generation`. The aggregate cost views group generically and need no change.

## 4. Queue and stages

`report/worker/run.py::process_report_job`:
1. Terminal-status guard (a redelivered finished job is deleted, not rerun).
2. `mark_report_running`, a fresh `run_id` for this pickup.
3. Run `collect → estimate → summarize → render_html → render_pdf → publish`, with `mark_report_stage` before each and a cancellation check at each stage boundary.
4. `mark_report_done`.

Any exception before `render_html` leads to `mark_report_failed`. The message is deleted in `finally`. `load_dotenv()` runs before imports (`design.md` §3 bug #1). At startup `run_forever` checks the container engine and that the report image exists (only warns if absent: a missing image fails PDF only, never the report).

| Stage | Does | On failure |
|---|---|---|
| `collect` | Runs the collectors (§6), checks the pin and eligibility again, builds the facts and `inputs` | Fails the job |
| `estimate` | Calibration (the only DB read here), `estimator.py`, writes the internal part to `report_internal_estimates` | Fails the job. Missing calibration is not a failure: cold start (§7) |
| `summarize` | Executive summary LLM call, grounding and numeric check | **Never fails the job**: ends `unverified` or `withheld` |
| `render_html` | Content gate, Jinja2 render, rendered gate, upload HTML | Fails the job (a gate hit is "withheld", not a failure, except on the secret scanner: §8.1) |
| `render_pdf` | PDF container, text-extraction gate, upload PDF | Report stays `done`, `pdf_status = failed`, retry endpoint |
| `publish` | Inserts the `assessment_reports` row, sets job `done` | Fails the job |

Each stage emits `workflow_events` with `workflow_kind = report_generation` and the existing event types (`stage.started`, `activity`, `warning`, `output`, ...). **Report events carry no cost** and no repository text beyond names and paths already shown in the report.

**Idempotency on redelivery:** a new `run_id` per pickup. The report row is inserted only in `publish`, keyed so a second pickup upserts on `generation_job_id` (unique). Artifact paths are deterministic and overwritten.

## 5. `report_json` schema

Versioned (`schema_version`, integer, starts at 1). A change that removes or renames a field bumps it. Ordering is stable everywhere (explicit sort keys, never dict/DB order) so equal `inputs` give byte-equal deterministic sections (specs §10).

```jsonc
{
  "schema_version": 1,
  "tier": "xray | full",
  "inputs": {
    "source_job_id": "uuid",
    "plan_id": "uuid | null", "plan_version": "int | null", "plan_status": "draft|in_review|approved | null",
    "estimator_version": "str", "estimator_config_hash": "sha256 hex",
    "pricing_version": "str | null",          // only when a price is shown
    "template_version": "str",
    "calibration_snapshot_id": "str | null"   // aggregate snapshot id (date range + counts), no org data
  },
  "generated_at": "iso8601",                  // not part of the byte-equality contract
  "cover": { "project_name": "str", "snapshot_date": "date", "watermark": "draft_plan | null" },
  "executive_summary": { "text": "str", "status": "verified|unverified|withheld", "note": "str | null" },
  "glance": { "loc": "int|null", "languages": [{"name","files"}], "frameworks": ["str"], "dependencies": "int|null",
              "files_analysed": "int", "files_filtered": [{"reason","count"}], "entities_by_type": {"function": 0},
              "coverage": { /* from extraction_coverage, samples capped at 10 */ } },
  "architecture": { "subsystems": [{"name","summary","summary_grounding","risk_level","modules":[...]}],
                    "hubs": [{"name","file_path","degree"}], "connectivity": [{"from","to","edges"}] },
  "risk": { "heat_map": [{"subsystem","low","medium","high"}],
            "hotspots": [{"subsystem","entities":[{"name","file_path","risk_level","signal","explanation"}],"more": 0}] },
  "security": { "secrets_by_reason": [{"reason","count","locations":[{"file_path","field_path"}]}],
                "risky_patterns": [{"pattern","count"}], "caveat": "str" },
  "plan": null,           // full only: phases, units, cycles needing facades, oversized units, status/version
  "estimate": null,       // full only, §7: customer part only
  "limitations": [{"code","text"}],          // §5.8, generated, never hand-written
  "next_steps": { "heading","body","contact" }
}
```

- X-ray **omits** `plan` and `estimate` (the keys are absent, not null) — the schema above shows `null` only as a placeholder.
- No field holds source text, `raw_facts` values, secret values, `[REDACTED:` text, token counts or internal cost. The leak gate (§8.1) and a schema allow-list test enforce it.
- Free-text fields (summaries, explanations, the executive summary) are the only places a leak can hide, so they are the ones the content gate scans.

## 6. Collectors and builder

`report/collect.py` has one collector per section. Rules:
- **Explicit column lists only.** Never `select("*")`, never `source_chunks.content`, never `raw_facts` values. A test asserts no collector selects a forbidden column.
- Service-role client, every query filtered by `org_id` **and** the job id.
- Each applies the fallbacks in specs §4 and returns `(data, limitations)`.

| Section | Reads | Fallback |
|---|---|---|
| glance | `ingestion_jobs.discovery_stats`; else last `stats.updated` event; `filtered_files` grouped by `reason`; `extracted_entities` counts; `extraction_coverage` | "not recorded for this run" + limitation |
| architecture | `knowledge_summaries` (all levels), `entity_edges` degree, Agent 2 `foundation` cluster if a plan | top-N by degree |
| risk | `knowledge_summaries.risk_level`, `.risk_factors`, `grounding_status`; `risk_rollup` for subsystem level | "signal not recorded" per entity |
| security | `secret_findings` (`reason`, entity `file_path`, `field_path`); rubric signals from `risk_factors` | n/a |
| plan | `migration_plans`, `migration_plan_items`, `clusters`, `cluster_reports`, `cluster_edges` | n/a (full only) |

`report/build.py` assembles `report_json`. For `full` it requires the plan's `source_job_id` to equal the report's job and the plan's status to be `draft`, `in_review` or `approved`; otherwise the job fails with a clear error (the API already returned 409, this is the re-check at run time). It records plan status and version in `inputs`, and a non-approved plan sets `cover.watermark = "draft_plan"` and adds a limitation.

### 6.4 Executive summary (the only LLM-written section)
`report/exec_summary.py`. Facts payload = repo-level summary, top risks (names, levels, signal labels), the deterministic counts, plan stats. Never code. The call goes through `shared/llm.py` with `agent='report'`, `job_type='report'`, `purpose='executive_summary'`. Repository-derived text is fenced and the system prompt says it is untrusted data (CLAUDE.md), as in `ask/`.

- Grounded with `check_grounding_with_retry` on the same facts payload.
- **Numeric check** (new, a separate pure function `check_numbers_in_facts(text, facts)`): every number in the generated text must appear in the payload (digits, with thousands separators normalised). A mismatch counts as ungrounded and triggers the retry. This is a new function rather than a change to the shared helper, because `check_grounding_with_retry` is used by Agents 1 and 2.
- After retries it is shown labelled **Unverified** with a one-line explanation. Generation never fails because of it. Length cap, no markdown headings, no code formatting. Backticks are stripped, and anything the leak gate flags makes the status `withheld` (the section is replaced by a fixed sentence).

## 7. The estimator

Pure module `report/estimator.py` (no DB, no rendering), config in `report/estimator.toml`, loaded and validated by `report/config.py`, which also computes the **config hash** recorded in `inputs`.

### Method (specs §6.1)
1. `bucket_units(units)`: size bucket (config edges on `clusters.size`) × risk (`cluster_reports.risk_level`; a unit with no report uses the config's `unknown_risk` default and adds a limitation).
2. `report/calibration.py` is the **only DB-reading piece**. It reads the pooled cross-org completed-unit set with the service role (decided 2026-10-01) from `llm_cost_by_execution_unit`, `unit_executions`, `clusters`, `cluster_reports`, with duration from `started_at`/`finished_at` (prerequisite), or stage-level `workflow_events` as a flagged lower-confidence fallback. It returns **aggregates only** per bucket (empirical samples of cost, attempts, duration, verdict). No org id, project id, path or unit id leaves it. `build_calibration(rows)` drops `cost_usd = null` rows and counts them. It stores a calibration snapshot id and date range.
3. `simulate(units, calibration, config)`: seeded Monte Carlo (`seed`, `runs` in config). Per unit it draws cost, attempts, duration and verdict from its bucket's empirical distribution and sums. Output is p50 and p90, never one number, plus `n` per bucket and overall.
4. **Cold start**: a bucket with fewer than `min_samples` (default 20) uses the config defaults. The report says how many units used defaults versus history.
5. **Failed units are not free**: the observed `failed_verification`/`error` rate becomes rework hours (config hours per failed unit by risk), added to the runtime/effort figures.
6. **Wall-clock runtime**: unit durations summed in plan order under the config's concurrency assumption (stated in the report).
7. **Unpriced calls**: rows with `cost_usd = null` are excluded and counted. Above a threshold the cost range is flagged low-confidence.
8. `manual_effort(facts, config)`: developer-days from entity counts by type, risk multipliers, cycles needing facades and oversized units. Always labelled **Heuristic**, with the coefficients in a footnote.
9. `quote(estimate, config)`: `None` without `[pricing]`. Otherwise a price range rounded to the configured precision so cost can't be reverse-engineered.

The estimator's output has two top-level parts, `customer` and `internal`. Only `customer` is ever placed in `report_json.estimate`. `internal` goes to `report_internal_estimates` and nowhere else. Tests assert the `customer` part has no cost field.

### Config schema (`report/estimator.toml`)
```toml
version = "1"
[buckets]            # size edges, e.g. size_edges = [3, 8, 20]
[defaults.<size>.<risk>]   # cold-start: cost_usd, attempts, duration_s, failure_rate
[calibration]  min_samples = 20; unpriced_threshold = 0.2
[simulation]   seed = 1; runs = 2000; concurrency = 1
[rework]       hours_per_failed_unit = { low = 2, medium = 4, high = 8 }
[manual_effort] per_entity = {function = 0.1, class = 0.3, route = 0.2, schema = 0.3, hook = 0.2}
                risk_multiplier = { low = 1.0, medium = 1.5, high = 2.5 }
                per_facade_cycle = 2.0; per_oversized_unit = 1.0
[pricing]      # optional: currency, method = "markup" | "per_unit", markup, unit_rate, rounding = 1000, minimum
[sharing]      default_expiry_days = 14; max_expiry_days = 90
[quality]      sparse_calls_per_function = 0.5
```
Values shown are placeholders: the real numbers are an operator's to set, and the open questions in specs §11 keep their stated defaults until confirmed. A missing `[pricing]` hides the price section.

## 8. Rendering, leak gates and the PDF

### 8.1 Leak gates (all fail closed: a report that trips a gate is not published)
What must never appear: raw source, source excerpts, secret values, `[REDACTED:…]` markers with surrounding text, prompt text, internal cost, other orgs' data. Allowed: names, paths, counts, summaries, explanations.

1. **Construction**: explicit-column collectors (§6).
2. **Content gate** (`report/leak_gate.py`) over the final `report_json`: the existing secret scanner, an overlap check of every free-text field against the job's `source_chunks` (any 6 consecutive words, punctuation ignored, at least 3 distinct, so tidying quotes or semicolons does not evade it) (the one place the worker reads chunk content, in memory, never persisted or logged), a code-shape heuristic (long backticked or brace-heavy runs), and `[REDACTED:` markers. An offending summary is replaced by "Summary withheld: contained source text" and listed in the report's limitations. It is not dropped silently.
3. **Rendered gate**: the same scan over the final HTML and over the text extracted from the PDF. A hit here fails the stage (nothing is uploaded) because the artifact itself is the problem.
4. **Schema allow-list**: a test (and a runtime assertion in `build.py`) that the final JSON has no key named like a cost field.

**Known gap:** a bare secret value written into prose matches no scanner pattern, and the stored source is already redacted, so nothing can overlap. The defence is upstream: the model only ever sees the redacted excerpt. `tests/test_report_leak.py` records it as a strict xfail.

### 8.2 One renderer
A deterministic server-side Jinja2 template (`template_version` pinned) renders `report_json` to **one self-contained HTML file** (inline CSS, inline SVG charts from `report/charts.py`, embedded fonts, no external requests). That stored file is what the app shows, what a share link serves, and what the PDF is printed from, so screen, link and PDF can't drift apart.

- Autoescape on for every value. No scripts, no `on*=` attributes, no external URLs (a test fails the build otherwise). Entity names and paths are attacker-controlled and are only ever text nodes or escaped attribute values.
- Served with the strict CSP `default-src 'none'; style-src 'unsafe-inline'; img-src data:` (and `font-src data:` for embedded fonts, added to the spec's value only if fonts are inlined as data URIs). The React shell shows it in `<iframe sandbox="">` (no `allow-scripts`, no `allow-same-origin`).
- The operator panel is React, **outside** the iframe, and is never part of the artifact.
- Charts: risk heat map (subsystem × level), phase-order diagram, estimate range bar (p50→p90 with n). Text labels on every cell, readable in greyscale.

### 8.3 PDF container (`backend/report_renderer/`)
Headless Chromium (Playwright base image, about 400 MB), built by `uv run document-analyzer-build-report-image`. Run through `shared/container_engine.py` (never `docker` directly). Hardening, the same family as the Agent 3 sandbox (`design_agent3.md` §7):
- `--network none`, read-only root filesystem, `--cap-drop ALL`, `--security-opt no-new-privileges`, memory / pids / cpu limits, a wall-clock timeout, non-root user, a tmpfs for Chromium's scratch space.
- **JavaScript disabled**, and Chromium's own request interception blocks every non-`file:` request.
- Input: the single HTML file, mounted read-only. Output: one PDF, written to a separate mounted directory. No other host paths are mounted. With `CONTAINER_ENGINE=colima` the files are staged under `CONTAINER_WORKDIR` as other containers do.
- Print CSS: page breaks, a running header (report id, date), page numbers. Added to `document-analyzer-doctor`.
- Chosen over WeasyPrint because it renders the same CSS and SVG as the browser view and avoids GTK/Pango on Windows dev. gVisor is not required (the input is our own rendered HTML, with JS off and no network), but `REPORT_SANDBOX_RUNTIME=runsc` is honoured for parity.

## 9. API surface

Org-scoped through the user's own Supabase client (RLS), 404 for another org's rows. Frontend pairs are listed in `tasks_report.md`.

| Method and path | Auth | Purpose |
|---|---|---|
| `POST /api/projects/{project_id}/reports` | member | `{tier}` → `202 {report_job_id}`. `409` when `full` has no eligible plan pinned to the latest job, when no `done` Agent 1 job exists, or when the same tier is already queued/running |
| `GET /api/projects/{project_id}/reports` | member | List: id, tier, plan status/version, created_at, `pdf_status`, `exec_summary_status`, `out_of_date` (computed on read) |
| `GET /api/reports/{report_id}` | member | `report_json` + status + `out_of_date` |
| `GET /api/reports/jobs/{job_id}/events` | member | Generation progress events (same shape as the other agents' events endpoints) |
| `GET /api/reports/{report_id}/html` | member | Stored HTML, CSP and `Cache-Control: no-store` |
| `GET /api/reports/{report_id}/pdf` | member | PDF stream |
| `POST /api/reports/{report_id}/pdf/retry` | member | Re-enqueues only the PDF stage (`409` unless `pdf_status = failed`) |
| `POST /api/reports/{report_id}/share-links` | member | `{expires_in_days}` capped by config → the token and its path **once** (§9.3) |
| `GET /api/reports/{report_id}/share-links` | member | id, created, expires, revoked, access count, last access. Never the token |
| `POST /api/share-links/{id}/revoke` | member | Sets `revoked_at`/`revoked_by`. Effective on the next request |
| `GET /api/reports/{report_id}/internal-estimate` | **operator** | AI cost p50/p90, n, calibration breakdown, price basis. `404` for non-operators |
| `GET /api/public/reports/{token}` | none | Stored HTML only |
| `GET /api/public/reports/{token}/pdf` | none | Stored PDF only |

### 9.1 Generation and read endpoints (Phase 4 contract)

All four are in `report/router.py`, use the caller's RLS-scoped client for reads, and write only through the service role (`org_id` from the `AuthContext`, never from the request). A report, job or project of another org is a `404`.

**Where the buttons live (frontend, 2026-10-02).** The same `ReportsSection` component is mounted in three places, each limited to the tier that fits the step the reader just finished: the Agent 1 run view's next-step area (**X-ray** only, once the job is `done`), the Agent 2 plan page after the document (**Full Assessment** only), and the project page (both tiers, the home of the full report list). No new endpoints: all three call the §9.1 `POST` and `GET`, and the report list is always the whole project's.

**Eligibility** (one function, used by the `POST` and by the list's `eligibility` block):
- **X-ray** needs `projects.latest_job_id` to point at an `ingestion_jobs` row with `status = done`. Otherwise not eligible: "no completed ingestion yet".
- **Full** needs the above and a plan with `source_job_id = latest_job_id` and status `draft`, `in_review` or `approved`. With several, the pick is `approved` first, then the highest `version`, then the newest. Otherwise not eligible, with the reason: "no plan for the latest ingestion" or "the plan is stale/rejected".
- Either tier is also blocked while a job of the same tier is `queued`/`running` for the project (the unique index; a race on it is the same `409`).

`POST /api/projects/{project_id}/reports`, body `{"tier": "xray" | "full"}` (anything else is `422`):
- `202 {"report_job_id", "status": "queued"}`. The `report_generation_jobs` row is pinned at creation: `source_job_id`, and for `full` the chosen `plan_id` and `plan_version`, plus `created_by` and frozen `params` (§12: `model_deployment`, `max_grounding_retries`, `pdf_timeout_seconds`, `pdf_memory_mb`, `renderer_image`).
- `409` with a readable `detail` when not eligible or already active. If creating the queue message fails the job is marked `failed` ("could not start") so it can't block the project.

`GET /api/projects/{project_id}/reports` returns `{"reports": [...], "active_jobs": [...], "eligibility": {"xray": {"eligible", "reason"}, "full": {"eligible", "reason"}}}`:
- `reports`: `id, tier, plan_id, plan_version, plan_status_at_generation, plan_status` (the plan's current status), `exec_summary_status, pdf_status, created_at, out_of_date`; newest first. No `report_json` and no storage paths.
- `active_jobs`: this project's `queued`/`running` jobs (`id, tier, status, progress_stage, created_at`), so the page can resume the progress view after a reload.
- `eligibility`: so the frontend can enable the buttons and say why one is disabled.

`GET /api/reports/{report_id}` returns the report row without storage paths or `created_by`: `id, project_id, generation_job_id, tier, source_job_id, plan_id, plan_version, plan_status_at_generation, plan_status, report_json, schema_version, template_version, estimator_version, exec_summary_status, pdf_status, has_html, created_at, out_of_date`.

`GET /api/reports/jobs/{job_id}/events?after=&limit=` has the same event shape and cursor as `GET /api/agent1/jobs/{id}/events`, filtered to `workflow_kind = report_generation`. The envelope adds the job's `tier`, `progress_stage`, `error` and, once published, `report_id`. The route is `/api/reports/jobs/...`, so it can't collide with `/api/reports/{report_id}`.

`out_of_date` is computed as in §10 and never written.

The PDF retry is a new queue message for the same job with `stage = "render_pdf"` (below), so it goes through the same worker and the same hardening.

### 9.2 Artifact endpoints and the worker's artifact stages (Phase 5 contract)

Artifacts live in the private `reports` bucket at `{org_id}/{report_id}/report.html` and `report.pdf`. Reads use the caller's own client (the bucket's select policy is org-scoped, like Agent 3's downloads); only the worker writes.

`GET /api/reports/{report_id}/html` returns the stored HTML bytes as `text/html; charset=utf-8` with `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Robots-Tag: noindex` and the CSP `default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; frame-ancestors 'self'`. A report with no stored HTML (older rows, or a job that ended before `render_html`) is a `404`. The browser app can't put an `Authorization` header on an iframe `src`, so it fetches this with `useApiFetch()` and shows the text in `<iframe sandbox="" srcdoc>`.

`GET /api/reports/{report_id}/pdf` streams the PDF (`application/pdf`, `Content-Disposition: attachment; filename="report-{tier}-{first 8 of id}.pdf"`, the same `no-store` and `nosniff`). `409` with a readable `detail` while `pdf_status` is `pending` ("not ready yet") or `failed` ("retry it"); `404` for another org's report. Streaming through the API rather than a signed URL keeps every download behind the same membership check.

`POST /api/reports/{report_id}/pdf/retry` is `409` unless `pdf_status = failed` and HTML is stored. Otherwise it sets `pdf_status = pending` (service role, filtered by `id` and `org_id`, so a second click is a `409`), enqueues `{report_job_id, org_id, stage: "render_pdf"}` and returns `202 {"status": "pending"}`. If enqueueing fails it puts `failed` back and raises.

**Worker.** `render_html` renders `report_json` with the pinned template, runs the rendered gate (`leak_gate.scan_rendered`, §8.1 gate 3: secrets, markers and source overlap over the HTML's text nodes), and uploads the HTML; a hit fails the job and uploads nothing. The report id is fixed at the start of the stage (the existing row's id on a redelivery, else a new one), so the artifact paths are deterministic and `publish` inserts the row under that id. `render_pdf` renders the stored HTML in the container, extracts the PDF's text with `pypdf`, runs the same gate, and only then uploads; any failure, including a gate hit, leaves `pdf_status = failed` and the job `done`. The retry message re-runs only that part: it downloads the stored HTML (so the PDF always matches the artifact the app shows), renders, gates, uploads and updates `pdf_path` and `pdf_status` on the existing row. It does not touch the job row.

### 9.3 Share-link management endpoints (Phase 6 contract)

In `report/router.py`, same rules as §9.1: reads through the caller's RLS-scoped client, writes through the service role with `org_id` from the `AuthContext`. A report or link of another org is a `404`.

`POST /api/reports/{report_id}/share-links`, body `{"expires_in_days": int}` (optional, default `sharing.default_expiry_days`; `422` below 1; a larger value is **capped** to `sharing.max_expiry_days` from `estimator.toml`, and the response shows the resulting `expires_at`). It generates `secrets.token_urlsafe(32)`, stores only its sha256 hex (`token_hash`), and returns `201 {"id", "token", "share_path": "/?share=<token>", "created_at", "expires_at"}`. **This is the only response that ever contains the token.** The API doesn't know the app's public origin, so it returns the path and the frontend prefixes `window.location.origin` to make the full URL. `404` for a missing or other-org report.

`GET /api/reports/{report_id}/share-links` returns `{"share_links": [{"id", "created_at", "expires_at", "revoked_at", "access_count", "last_accessed_at", "status"}]}`, newest first, where `status` is `active`, `expired` or `revoked` (revoked wins), computed on read. No token, no `token_hash`, no `created_by`, no `org_id`; each row is rebuilt explicitly.

`POST /api/share-links/{id}/revoke` sets `revoked_at` and `revoked_by` (service role, filtered by `id` and `org_id`) and returns `200 {"id", "revoked_at", "status": "revoked"}`. Revoking again is idempotent: the first `revoked_at` is kept. The public routes check revocation on every request, so it is effective on the next one.

### 9.4 Public routes (Phase 6 contract)

In `report/public.py` (its own router, no `get_auth_context`). They use the service-role client, because the visitor has no session, and read only `report_share_links` (by `token_hash`) and the report's own `assessment_reports` row, for `created_at`, `html_path` and `pdf_path`. They never read `projects`, `migration_plans`, `report_json` or any status, so out-of-date state can't leak (§10). The paths come from the link's `org_id` and `report_id` (`artifacts.html_path/pdf_path`), never from the request.

`GET /api/public/reports/{token}` returns the stored HTML bytes as `text/html; charset=utf-8` with the §9.2 artifact headers and CSP (`Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Robots-Tag: noindex`). Two extra headers carry what the public page needs without a second table or a JSON body: `X-Report-Generated-At` (the report's `created_at`, for "Snapshot as of") and `X-Report-Pdf` (`available` or `unavailable`, from whether a PDF is stored). `GET /api/public/reports/{token}/pdf` returns the stored PDF (`application/pdf`, `Content-Disposition: attachment; filename="report-<first 8 of report id>.pdf"`, same headers).

**One failure.** A token that is malformed (not 43 URL-safe base64 characters, rejected before any query), unknown, expired, revoked, or whose report or artifact is gone returns the same `404 {"detail": "not found"}` with the same headers. The PDF route does the same for a missing PDF. Lookup is by `sha256(token)` as an equality filter, then `hmac.compare_digest` on the stored hash. Deleting a report or project cascades the link row (§3), so it is the same 404.

**Rate limiting.** A per-client-IP sliding window in the API process (`REPORT_PUBLIC_RATE_LIMIT` requests per 60 s, default 30; `0` disables), checked before the lookup and counting every request, hits or misses. Over the limit is `429 {"detail": "too many requests"}` with `Retry-After`. It is per process, so behind several API workers the effective limit multiplies; a proxy limit is the production control, and this is the floor. The client IP is `request.client.host` (set the proxy headers handling at the server, not here).

**Access audit.** A successful response increments `access_count` and sets `last_accessed_at` (service role, filtered by `id` and `org_id`; a read-then-write, so two simultaneous opens can count once. It's an audit figure, not a quota). Both routes count. The viewer's IP is never stored.

**Public page.** `?share=<token>` is read before the sign-in gate, so it needs no session and shows no sidebar. It fetches the HTML with a plain `fetch` (no `Authorization` header), shows it in the same sandboxed iframe as `ReportPage`, a "Snapshot as of <date>" header from `X-Report-Generated-At`, and a Download PDF button enabled by `X-Report-Pdf`. Any failure shows one generic "This link is not available" message, never a reason. It shows no out-of-date state.

### 9.5 Internal-estimate endpoint (Phase 7 contract)

`GET /api/reports/{report_id}/internal-estimate` in `report/router.py`, behind `get_cost_operator` (`shared/cost_access.py`, `COST_OPERATOR_USER_IDS`). A caller who isn't an operator gets the dependency's `404 {"detail": "not found"}` before anything is read, so the endpoint's existence isn't revealed.

For an operator the report is first looked up through the caller's own RLS-scoped client, filtered by `org_id` (another org's report is a `404 "report not found"`). The estimate row is then read with the **service-role** client, because `report_internal_estimates` has no tenant privileges (§3), filtered by both `org_id` and `report_id`. A report with no row (an X-ray, or a report whose estimate stage found nothing) is a `404 "no internal estimate for this report"`.

`200 {"report_id", "ai_cost_p50", "ai_cost_p90", "n", "calibration", "price_basis", "created_at"}`, the stored columns as they are, rebuilt explicitly so `org_id` can't reach the body. `ai_cost_p50`/`ai_cost_p90` are `null` when nothing was priced (cold start; never zero), and `n` is the number of calibration units behind them. `calibration` is `{snapshot, buckets, unpriced_excluded, unpriced_share, low_confidence}` (§7) and `price_basis` is `{method, currency, pricing_version}` or `null` without `[pricing]`. A list-price estimate of our own AI spend, not the customer's quote.

**Panel.** `InternalEstimatePanel` on `ReportPage`, **outside** the iframe and never in the artifact or the PDF. It fetches the endpoint once per report, and on a `404` (or any failure) renders nothing at all, no error and no hint, the same as `CostPanel`. For an operator it shows the p50–p90 AI cost (or "no cost history yet, estimated from defaults" when null), `n`, the low-confidence flag with the unpriced share, the price basis, and a per-bucket table (size, risk, units, n, source, median cost).

## 10. Staleness and pinning

- A report is an immutable snapshot pinned to one `ingestion_jobs.id` and, for `full`, one plan id + version. Regenerating makes a new row.
- **Out of date is computed on read**, never written: true when `projects.latest_job_id` ≠ the report's `source_job_id` (`shared/plan_staleness.py::is_stale`), or when the pinned plan's *current* status is `stale` or `rejected`. The list and detail endpoints do this with one extra select on `projects` and one on `migration_plans`. The report **never calls `mark_stale_plans`**, so no Agent 1 or 2 row is written. If another path hasn't yet moved a plan to `stale`, the first condition (job id) already catches it.
- A **Full Assessment** requires the plan's `source_job_id` to equal the report's job, checked in the API (409) and again in `collect`.
- A shared report shows "Snapshot as of <date>" and does **not** reveal whether the repo was re-ingested. The public routes never read `projects` or `migration_plans`.

## 11. Share-link threat model

| Threat | Mitigation |
|---|---|
| Guessing a token | 32 random bytes (URL-safe, 256 bits). Lookup by sha256 with a constant-time compare |
| DB leak exposes links | Only `sha256(token)` is stored. The link is shown once at creation. Losing it means creating a new one |
| Enumerating which links exist | Every failure (unknown, malformed, expired, revoked, deleted report or project) returns the **same 404** with the same body and timing class |
| Brute force | Rate limiting on both public routes (per IP, in front of the lookup) |
| Link scraped into a search engine or proxy cache | `Cache-Control: no-store`, `X-Robots-Tag: noindex`, `Referrer-Policy: no-referrer` |
| XSS from a hostile entity name | Autoescape, no scripts in the artifact, strict CSP on every response, sandboxed iframe in-app |
| Parameter abuse reaching other data | The routes accept the token only and read only `report_share_links` and the stored artifact. No org id, report id or query parameters |
| A viewer learning more than the report | The public response is the stored HTML/PDF only, with no `out_of_date`, no plan state, no operator data |
| Stale access after revoke or delete | Revocation is checked on every request. Report/project deletion cascades the link rows |
| Audit | `access_count` and `last_accessed_at` only: never the viewer's IP beyond ordinary server logs |

`expires_at` is not null: default 14 days, maximum 90 (config, specs §11 defaults).

## 12. Configuration

Operational values (read at call time) and values frozen into `report_generation_jobs.params` at creation.

| Setting | Default | Frozen | Meaning |
|---|---|---|---|
| `REPORT_MODEL_DEPLOYMENT` | the deployment Agent 1 uses | yes | Executive-summary model |
| `REPORT_MAX_GROUNDING_RETRIES` | the shared grounding cap | yes | Retries before `unverified` |
| `REPORT_PDF_TIMEOUT_SECONDS` | 120 | yes | PDF container wall clock |
| `REPORT_PDF_MEMORY_MB` | 1024 | yes | PDF container memory |
| `REPORT_RENDERER_IMAGE` | `galvanise-report-renderer:<pinned tag>` | yes | PDF image |
| `REPORT_SANDBOX_RUNTIME` | unset | no | `runsc` for gVisor, optional |
| `REPORT_QUEUE_VISIBILITY_TIMEOUT_SECONDS` | 900 | no | Same semantics as the other agents |
| `report/estimator.toml`, `report/report.toml` | in repo | hash only | Estimator, pricing, sharing, next-steps block. The estimator config hash goes in `inputs`. `report.toml` is plain text plus one link, never HTML |
| `COST_OPERATOR_USER_IDS` | empty | no | Existing operator list (`design.md` §7.4) |

## 13. Project deletion

`projects.py::delete_project` (all report data goes before the Agent 1 rows, since reports reference `ingestion_jobs` and `migration_plans`):
1. `ProjectBusyError` also counts queued/running `report_generation_jobs`.
2. Delete `report_generation_jobs`' workflow events (`report_generation`), then `report_internal_estimates`, `report_share_links` and `assessment_reports` for the project's reports, then `report_generation_jobs`.
3. After the rows, remove the `reports` bucket objects under `{org_id}/{report_id}/` with the existing `_list_objects` / chunked-remove pattern, logging and continuing on failure like the other buckets.
Tests extend `tests/test_projects_delete.py`.

## 14. Dependencies on other agents, and known gaps

- Landed (2026-10-01): `knowledge_summaries.risk_factors`, `ingestion_jobs.discovery_stats`, `unit_executions.started_at/finished_at`. Rows from before them degrade per specs §4 and add a limitation.
- The Agent 1 run view's use of `discovery_stats` (`tasks.md`) is a separate, still-open frontend task and doesn't block the report.
- Calibration is only as good as completed Agent 3 history. Until then every bucket is cold start, and the report says so.
- `report` as an `llm_invocations` agent depends on the CHECK widening (§3.5).

## 15. Deliberately out of scope

- Editing a report by hand, or a hand-written executive summary.
- Per-viewer share-link accounts, passwords or one-time-use links.
- Branding beyond the project name (no logo), per specs §11's default.
- A bundled "N× cheaper" claim or any pricing comparison.
- Regenerating or refreshing a report in place. A new snapshot is always a new row.
- Re-scanning code for security issues. Security findings come from stored facts only.
- Showing or exporting internal cost anywhere except the operator endpoint.
- Differential or live-repo comparison between two reports.
