# Assessment Report — Specification

Functional and non-functional spec for the Assessment Report. The *how* (tables, queue, templates, API shapes) will live in `design_report.md`, and the build order lives in `tasks_report.md`. Section references (§N) point into this file unless they name another file.

This spec replaces the original one-page brief (2026-10-01). It keeps every requirement in that brief and adds what a check against the codebase showed was missing (§13 lists each gap and where it's handled).

## 1. Overview

The Assessment Report is a shareable, polished document generated from one repo's Agent 1 run and, optionally, its Agent 2 plan. It includes a time and price estimate for migrating the repo. It is our main sales tool and will be read by enterprise buyers, their security teams and their engineers. That sets three bars, in this order:

1. **Safe to share.** It never contains source code, source excerpts or secret values, and it never contains our internal cost or margin.
2. **Honest.** Every number traces to a stored fact or a labelled heuristic. Every estimate is a range with its sample size. Every gap in our analysis is stated, not hidden.
3. **Accurate and reproducible.** The same inputs give the same report, apart from the one LLM-written section (§5.1).

Looking good comes after those three.

### 1.1 Two tiers

| Tier | Available when | Contents |
|---|---|---|
| **Codebase X-ray** | The project's Agent 1 job is `done` | §5.1–§5.5, §5.8, §5.9 |
| **Full Assessment** | Also an Agent 2 plan pinned to **the same** ingestion job, status `draft`, `in_review` or `approved` (not `stale`, not `rejected`) | All sections |

Sections that need a plan are **left out** of the X-ray, not shown empty. The Full Assessment shows the plan's status and version on the cover and in §5.6. A non-approved plan carries a "Draft plan, subject to review" watermark on every plan-dependent page.

### 1.2 Two audiences

| Audience | Sees | Never sees |
|---|---|---|
| **Customer** (org members, share-link viewers, the PDF) | Everything in §5, including runtime, manual effort and the quoted price when pricing is configured | Our AI cost, margin, token counts, per-unit cost, calibration data from other orgs |
| **Operator** (`COST_OPERATOR_USER_IDS`, `design.md` §7.4) | The customer report plus an internal estimate panel: AI cost p50/p90, the price-to-cost ratio, the calibration sample | n/a |

Internal figures are stored apart from the report (§6.2). They are never in `report_json`, the HTML or the PDF, so "the customer report has no internal cost" holds by construction, not only by template logic.

## 2. Where it fits in the architecture

The report is a **read-only consumer** of Agent 1 and Agent 2 tables (and, for calibration, of Agent 3 and cost tables). It is not a fourth agent and is never triggered by an agent. Like Agent 2, a user triggers it.

- It never writes to Agent 1, 2 or 3 tables. Gaps in their data are handled by degrading the section and stating the gap (§5.8), and fixed in the owning agent's tasks file.
- It lives in `backend/document_analyzer/report/` with its own router (`/api/reports/...`), its own queue and worker (§7), its own tables and bucket.
- Pure helpers may be imported (`risk_rollup`, `check_grounding_with_retry`, the secret scanner, `is_stale`). Anything that needs report-specific changes moves to `shared/` first.

## 3. Snapshots and staleness

- A report is an **immutable snapshot**, pinned to one `ingestion_jobs.id` and, for a Full Assessment, one `migration_plans.id` + `version`. Regenerating creates a new report row. Nothing is mutated in place except the generation status fields while it is being built.
- **Out of date** is computed on read. It is true when `projects.latest_job_id` ≠ the report's job (`shared/plan_staleness.py::is_stale`), or when the pinned plan has since moved to `stale`/`rejected`. It is shown as a banner in the app. Nothing is written to any table to mark it.
- A shared report shows "Snapshot as of <date>" and does **not** reveal whether the repo has since been re-ingested.
- `report_json` carries an `inputs` block: job id, plan id + version + status at generation, `estimator_version`, estimator config hash, `pricing_version` (when a price is shown), `template_version`, calibration snapshot id. Two reports with equal `inputs` must have byte-equal deterministic sections.

## 4. Data sources

Every section reads named fields. If a field is missing for an older job, the section degrades as stated and adds an entry to §5.8. It never guesses.

| Section | Source | Fallback when missing |
|---|---|---|
| LOC, languages, frameworks, dependencies | `ingestion_jobs.discovery_stats` (Agent 1 prerequisite, §12) | Last `stats.updated` event for the job in `workflow_events`. If neither, "not recorded for this run" |
| Files analysed vs filtered | `filtered_files` grouped by `reason`, entity `file_path` count | n/a |
| Parse coverage | `ingestion_jobs.extraction_coverage` | "Coverage not recorded" |
| Entity counts | `extracted_entities` by `entity_type` | n/a |
| Summaries, risk | `knowledge_summaries` (all levels), `risk_level`, `grounding_status` | n/a |
| Rubric signal per entity | `knowledge_summaries.risk_factors` (Agent 1 prerequisite, §12) | "Signal not recorded for this run" per entity, and one §5.8 entry |
| Hubs / connectivity | `entity_edges` (degree), Agent 2 `foundation` cluster when a plan exists | Top-N by degree only |
| Secrets | `secret_findings` (`reason`, entity `file_path`, `field_path` location only) | n/a |
| Plan | `migration_plans`, `migration_plan_items`, `clusters`, `cluster_reports`, `cluster_edges` | n/a (Full only) |
| Calibration | `llm_cost_by_execution_unit`, `unit_executions`, `clusters`, `workflow_events` for `agent3_execution` | Config defaults (cold start, §5.7) |

## 5. Report sections

### 5.1 Executive summary (the only LLM-written section)
- Input: the repo-level summary, the top risks (names, levels, signal labels), the deterministic counts from §5.2, and plan stats when present. **Never raw code or `source_chunks`.**
- Called through `shared/llm.py` (`agent='report'`, `job_type='report'`, `purpose='executive_summary'`), so it shows up in cost accounting.
- Repository-derived text in the prompt is fenced and marked untrusted in the system prompt (CLAUDE.md), the same as `ask/`.
- Checked with `check_grounding_with_retry` against the exact facts payload it was given. **Also a deterministic numeric check**: every number in the generated text must appear in the facts payload, and a mismatch counts as ungrounded and triggers the retry.
- After the retries are used up it is shown labelled **Unverified**, with a one-line explanation. Generation never fails because of it.
- Length cap, no markdown headings, no code formatting. A post-check strips backticks and rejects anything the leak gate (§8) flags.

### 5.2 Codebase at a glance
Lines of code, languages, frameworks, dependency count, files analysed vs filtered (filter reasons summarised as counts per reason), entity counts by type, parse coverage, and what wasn't parsed and why (status and per-language counts from `extraction_coverage`, sample paths capped at 10).

### 5.3 Architecture map
Subsystems and modules with their summaries (with an Unverified label where it applies), hub functions (the plan's `foundation` unit when there is a plan, otherwise top-N by degree), and the most-connected areas (subsystem-to-subsystem edge counts).

### 5.4 Risk hotspots
High- and medium-risk entities grouped by subsystem. Each one shows its file path, the rubric signal that matched and the plain-English explanation from its summary. Subsystem risk uses the existing `risk_rollup`. Capped per subsystem with "+N more". Includes the **risk heat map** (subsystem × risk level, counts).

### 5.5 Security findings
- Counts of redacted secrets by `reason`, and their locations (file path and `field_path` only). **Never the value, never a surrounding excerpt.**
- High-risk patterns from the rubric: `eval`/dynamic execution, `unserialize` on input, SQL built by concatenation, superglobals feeding any of those. These are counted from the stored risk signals, not by re-scanning code.
- A fixed caveat: this is a static scan with known false negatives, not a penetration test.

### 5.6 Migration plan (Full only)
Phases and units in order, with labels, sizes and risk. Cycles that need facades (`needs_facade` edges, merged SCCs). Oversized units (size above the plan's `max_unit_size` param). The plan's status and version. The **phase order diagram** (phases as columns, with dependency arrows between phases).

### 5.7 Estimate (Full only)
See §6.

### 5.8 Assumptions and limitations
Built automatically, never hand-written:
- Percentage of unverified summaries (by level).
- Parsing gaps (files not parsed, languages without an extractor, Graphify unavailable).
- **Sparse call graph warning** when resolved `calls` edges per function entity are below the configured threshold, plus the share of `INFERRED` Graphify edges.
- Missing data: every fallback from §4 that was used.
- The estimate's assumptions and cold-start status (§6).
- A draft or unapproved plan.
- An unverified executive summary.

### 5.9 Next steps
A short call-to-action block from config (`report/report.toml`: heading, body, contact). Plain text plus one link, no HTML from config.

## 6. The estimate

### 6.1 Method
`report/estimator.py` is a **pure** module: data in, numbers out, no DB, no rendering. Every coefficient lives in `report/estimator.toml`.

- **Buckets**: each plan unit gets a size bucket (config edges on `clusters.size`) × a risk level (`cluster_reports.risk_level`).
- **Calibration set**: completed Agent 3 units **pooled across all orgs** (decided 2026-10-01). From that set: cost per unit (`llm_cost_by_execution_unit`), attempts per unit and verdict (`unit_executions`), unit duration (`unit_executions.started_at/finished_at`, Agent 3 prerequisite §12; stage-level `workflow_events` as fallback). Only aggregates leave the internal tables. No other org's id, name, path or per-unit row ever reaches a report or an operator view.
- **Simulation**: a seeded Monte Carlo (seed and run count in config) draws a cost, attempt count, duration and verdict for each unit from its bucket's empirical distribution and sums them. Output is **p50 and p90**, never a single number, plus **n** (calibration units used) per bucket and overall.
- **Cold start**: a bucket with fewer than `min_samples` calibration units uses the config defaults for that bucket. The report says how many units were estimated from defaults versus history.
- **Failed units are not free**: the observed `failed_verification`/`error` rate becomes human rework hours (config hours per failed unit by risk), added to the runtime/effort figures.
- **Wall-clock runtime**: unit durations summed in plan order, with the configured concurrency assumption stated.
- **Unpriced calls**: calibration rows with `cost_usd = null` are excluded and counted. If they exceed a threshold, the cost range is flagged as low-confidence.

### 6.2 Outputs and who sees them
| Output | Customer report | Operator panel |
|---|---|---|
| Agent 3 runtime p50/p90 + n | yes | yes |
| Manual effort (developer-days) p50/p90 | yes, labelled **Heuristic** | yes |
| Quoted price | only when `pricing` is configured | yes, with the cost it was derived from |
| AI cost p50/p90 + n | **no** | yes |
| Calibration breakdown per bucket | counts only (n, default vs historical) | full |

Operator-only figures are stored in an internal table with RLS on and no tenant policy (same pattern as `llm_invocations`) and served only through `get_cost_operator` (404 for everyone else).

### 6.3 Manual effort heuristic
Developer-days from entity counts by type, risk levels, cycles needing facades and oversized units. Every coefficient is in config, and the report shows the formula's inputs and coefficients in a footnote. It is always called a heuristic and is never presented as a measurement. No "N× cheaper" claim is generated.

### 6.4 Customer price
Optional. When `[pricing]` is present in config (currency, method, markup or per-unit rate, rounding, minimum), the report shows a quoted price range derived from the estimate, rounded to the configured precision so that cost can't be reverse-engineered. When it is absent, the section is hidden, not shown empty.

### 6.5 Assumptions
The estimate lists its assumptions in the report: calibration size and date range, cold-start buckets, concurrency, model deployment and pricing version at calibration time, rework hours per failed unit, and that prices may change.

## 7. Generation and async processing
- **Own queue and worker**: `report_generation_jobs` (pgmq) and `uv run document-analyzer-reporter`. Generation includes an LLM call with retries and a PDF render in a container, so it takes 10–60 s or more and must survive restarts and redeliveries. Trade-off: one more process to run, but it follows the one-queue-per-agent rule and the `queue.py` patterns and gets redelivery for free. FastAPI BackgroundTasks were rejected because they lose work on restart. Reusing another agent's queue was rejected because it breaks the architecture rules.
- **Stages**: `collect → estimate → summarize → render_html → render_pdf → publish`, each emitting `workflow_events` with `workflow_kind = report_generation` and the existing event types. Report events carry no cost.
- **Idempotent on redelivery**: a `run_id` per pickup, and artifacts written to deterministic paths and overwritten, never appended.
- **Failure**: a failure in `render_pdf` leaves the report `done` with HTML and `pdf_status = failed` and a retry button. A failure before `render_html` fails the job. A grounding failure never fails it (§5.1).

## 8. Sharing and safety

### 8.1 Leak model (what must never appear)
Raw source, source excerpts, secret values, `[REDACTED:…]` markers with surrounding text, prompt text, internal cost, and other orgs' data. Allowed: names, paths, counts, summaries and explanations.

Enforcement, all fail closed (a report that trips a gate is not published):
1. **Construction**: collectors select explicit columns. `source_chunks.content` and `raw_facts` values are never selected.
2. **Content gate** over the final `report_json`: the existing secret scanner, plus an n-gram overlap check of every free-text field (summaries, explanations, executive summary) against the job's `source_chunks`, plus a code-shape heuristic (long backticked or brace-heavy runs). An offending summary is replaced by "Summary withheld: contained source text" and listed in §5.8. It is not dropped silently.
3. **Rendered gate**: the same scan over the HTML and the text extracted from the PDF.

### 8.2 Untrusted text in rendering
Entity names, paths and summaries are attacker-controlled. The template autoescapes everything, the HTML has no scripts, and it is served with a strict CSP (`default-src 'none'; style-src 'unsafe-inline'; img-src data:`). The PDF renderer runs with JavaScript off, network off, and only the one local HTML file as input.

### 8.3 Share links
- A token is 32 random bytes (URL-safe). **Only its sha256 is stored** (decided 2026-10-01): the link is shown and copied once, at creation, and losing it means creating a new one.
- Each link gives read-only access to **one** report snapshot, without login, until `expires_at` (default and maximum from config) or revocation.
- `GET /api/public/reports/{token}` serves only the stored HTML, and `/pdf` only the stored PDF. They look the token up by hash with the service role, with no other parameters, and every failure (unknown, expired, revoked, deleted report or project) gives the **same 404**. The response headers are `Cache-Control: no-store`, `X-Robots-Tag: noindex`, `Referrer-Policy: no-referrer` and the CSP above. The routes are rate-limited.
- `access_count` and `last_accessed_at` are recorded for audit, never the viewer's IP beyond what normal logs keep.
- Revoking from the UI takes effect on the next request. Deleting the project deletes its reports, links and artifacts.

### 8.4 Multi-tenancy
Every new table has `org_id` and RLS. Worker writes use the service role, stamp `org_id` from the job row, and filter updates by `id` and `org_id`. The bucket path is `{org_id}/{report_id}/…`, with a select policy only for the owning org. The public route never accepts an org or report id.

## 9. Rendering
- **One renderer.** A deterministic server-side Jinja2 template (`template_version` pinned) renders `report_json` to a self-contained HTML file (inline CSS, inline SVG charts, embedded fonts, no external requests). That stored file is what the app shows (inside a sandboxed iframe in the React shell), what a share link serves, and what the PDF is printed from. Screen, link and PDF can't drift apart.
- **React shell** (`frontend/src/report/`): project-page buttons, the report list, a header with tier, plan status/version, out-of-date banner and generation progress, and the iframe. The operator panel sits outside the iframe and is never part of the artifact.
- **PDF**: headless Chromium (Playwright base image), run through `shared/container_engine.py`. It runs network-less with a read-only root and no capabilities, with resource limits and a timeout, and with JS disabled. Chosen over WeasyPrint because it renders the same CSS and SVG as the browser view, needs no GTK/Pango install (painful on Windows dev), and fits our existing isolated-container pattern. Cost: a ~400 MB image, built with `uv run document-analyzer-build-report-image`.
- **Visuals**: clear typography, the risk heat map by subsystem, the phase-order diagram, the estimate as a range bar (p50 to p90) with n shown, print CSS (page breaks, running header with report id and date, page numbers).

## 10. Acceptance criteria
- Same `inputs` give byte-equal deterministic sections (snapshot test).
- The leak test: a fixture repo with a fake secret and distinctive source lines. The secret, its redaction context and any source excerpt are absent from the JSON, the HTML and the extracted PDF text, and the build fails otherwise.
- No internal cost field or value appears anywhere in the customer artifacts (test).
- Share links: valid, expired, revoked, wrong token, deleted project, and cross-org (org A's report unreachable with any input from org B) all behave as in §8.3.
- Re-ingesting marks old reports out of date on read, with no write to Agent 1 or 2 tables.
- Full Assessment refuses a plan pinned to a different job.
- The estimator's tests cover cold start, the normal case, range ordering (p50 ≤ p90), missing data and seed determinism.
- The full existing test suite passes.

## 11. Open questions (defaults in brackets, confirm before release)
- Currency and rounding for the quoted price [USD, nearest 1,000].
- Customer branding on the cover (logo, company name) [project name only, no logo].
- Default and maximum share-link expiry [14 days / 90 days].
- `min_samples` per bucket for leaving cold start [20 units].
- Sparse call graph threshold [< 0.5 resolved `calls` edges per function].

## 12. Prerequisites in other agents (owned there, not here)
- **Agent 1** (`tasks.md`, "Assessment Report prerequisites"): persist entity `risk_factors` on `knowledge_summaries`, and persist discovery stats on `ingestion_jobs.discovery_stats`.
- **Agent 3** (`tasks_agent3.md`): record `started_at` / `finished_at` on `unit_executions`.

## 13. Gaps found in the original brief
| # | Gap | Handled in |
|---|---|---|
| 1 | Entity `risk_factors` aren't stored anywhere | §4, §12 |
| 2 | Discovery stats exist only as lossy events | §4, §12 |
| 3 | No per-unit timing in Agent 3 | §6.1, §12 |
| 4 | `llm_invocations` and `workflow_events` CHECKs only allow agent1–3 | §5.1, §7, tasks migration step |
| 5 | "AI cost" in the report conflicts with cost being internal-only | §1.2, §6.2 |
| 6 | "HTML in React" and "PDF from the same HTML" conflict | §9 |
| 7 | Leaks via LLM summaries, and XSS/script execution in the renderer | §8.1, §8.2 |
| 8 | A plan could be pinned to a different job than the report | §1.1, §10 |
| 9 | The LLM can invent numbers that still pass grounding | §5.1 |
| 10 | Deleting a project must remove reports, links and PDFs | §8.3 |
| 11 | Quoted price could reveal cost | §6.4 |
| 12 | Estimates can't be reproduced or audited later | §3 `inputs`, §6.1 |
