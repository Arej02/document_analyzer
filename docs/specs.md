## Agent 1 (Understand)

## Purpose

Turn a legacy codebase into a queryable knowledge base that Agent 2 (Plan) can later retrieve from. Agent 1 does not plan or generate code — it only understands and summarizes what exists.

## Pipeline position

Agent 1 is one of three independent LangGraph subgraphs, connected only through the shared Supabase store. No agent calls another agent directly. This means:

- Agent 1 can be re-run against a repo without touching Agents 2/3.
- Agent 2 can be re-run against existing Agent 1 output without re-ingesting the repo.
- Each agent is independently testable.

## Input / Output

| | |
|---|---|
| **Input** | Zip/tar upload, or a GitHub URL (public repo, or private + access token). PHP or WordPress codebase. |
| **Output** | `extracted_entities` (functions, classes, routes, schema, hooks, deps), `entity_edges` (calls, hook bindings, globals, includes), `knowledge_summaries` (entity → module → subsystem → repo, each with `risk_level` + embedding) |

## Functional requirements

### FR1 — Ingestion
- Accept a zip/tar upload OR a GitHub URL.
- Support public repos and private repos (via access token).
- Detect language/framework signals: plain PHP vs. WordPress plugin/theme structure.
- Every ingestion resolves to a `project` (matched by `org_id` + source, or newly created if no match) before a job is created. A job is one run against a project — this is what makes incremental re-ingestion (FR6) and Agent 2 querying "the current knowledge base for this repo" possible, instead of every run being an orphan silo.
- A private-repo access token is used only for the clone operation and is not retained past job completion (see `design.md` §2 `github_token_ref`).

### FR2 — Structural extraction
- AST-extract structural facts using the pluggable per-language `Extractor` protocol.
  - PHP: `nikic/php-parser`, run as a subprocess.
  - WordPress: custom extractor for hooks, CPTs, and meta.
- Pure parsing only — no LLM involvement at this stage.
- Same pass emits `entity_edges`: call graph, hook registration/invocation, globals, includes.

### FR3 — Boilerplate filtering
- Cheap heuristics filter out boilerplate/vendor code (e.g. `wp-includes/`, `vendor/`) before anything reaches the LLM.
- Filtered files must be logged, since heuristics can false-positive on a customized vendor fork — false positives need to be auditable, not silent.

### FR4 — Bottom-up summarization
- Entity summaries generated from that entity's own facts only.
- Module summaries generated from entity summaries.
- Subsystem/repo summaries generated the same way, one level up at a time.
- Every individual LLM call stays small regardless of total repo size.
- `risk_level` (low/medium/high) is assigned at every summary level. The criteria/heuristic used to assign it must be documented (in `design.md` or the summarization prompt spec) so the value is explainable to an enterprise reviewer, not an opaque per-call LLM judgment.

### FR5 — Grounding validation
- A grounding checker validates each generated summary against its underlying facts before persisting.
- On failure: regenerate, up to a retry cap.
- After the retry cap is exhausted: flag the summary `unverified` rather than blocking the rest of ingestion. One bad module must never stall the whole repo.

### FR6 — Caching / incremental re-ingestion
- `summary_cache` keyed by content hash skips re-summarizing unchanged code on repeat ingestions.
- On re-submission of an edited repo (same `project`), only changed files (by content hash) are re-extracted and re-summarized — not the whole tree.
- Cascading re-summarization: when an entity's summary changes (or is newly generated), every ancestor summary built on top of it (module → subsystem → repo) is invalidated and regenerated. A changed leaf must never leave a stale parent summary in place — the knowledge base must stay internally consistent after every incremental run.

## Non-functional requirements

### NFR1 — Async execution
Ingestion can run for minutes to hours. All of Agent 1 sits behind an async job queue rather than being called synchronously from the FastAPI layer. The frontend must poll or subscribe for status rather than expect a synchronous response.

### NFR2 — Security / untrusted input
- Ingested repos are arbitrary user-submitted code. Agent 1 never executes any part of it — structural extraction is static AST parsing only (FR2), full stop. There is no code-sampling/execution sandbox in MVP scope; this is a deliberate scope cut, not a deferred TODO, and removing it is a net security win for a system that accepts arbitrary customer-submitted code.
- Extracted strings are scanned for secrets/credentials before being persisted to Supabase. On a match, the value is redacted (replaced with a placeholder) before it's persisted, and the finding is logged for audit with the job/entity reference — a match never fails the entity or blocks the job.
- Ingestion input itself is bounded before extraction starts: a max upload size and a max file/entity count are enforced, and an oversized submission is rejected with a clear error rather than being partially processed.

### NFR3 — Multi-tenancy
- Supabase Auth + RLS keyed on `org_id`, applied to every table from day one. No table ships without this.
- The async worker writes via a service-role connection that bypasses RLS by construction — it is the only path into these tables besides the authenticated request/response API, and it is solely responsible for stamping the correct `org_id` on every row it inserts, sourced from the job's own `org_id`, never from ingested content. This is the actual multi-tenancy trust boundary and must be treated as security-critical, not incidental plumbing.

### NFR4 — Embedding consistency
A single fixed embedding model is used for the entire knowledge base. If it ever changes, every existing embedding must be regenerated. The model/version is pinned in a config table, not in code, so a change is an explicit migration. If the new model's output dimension differs from the current one, this is not just a data backfill — pgvector's `vector(N)` column type is fixed-dimension at creation time, so a dimension change requires an actual schema migration (new column or table), which must be planned for, not discovered mid-migration.

### NFR5 — Cost control at scale
Bottom-up summarization bounds the size of each individual LLM call, but total call count still scales with entity count. Large repos need parallel fan-out at the entity-summary level, with a concurrency cap — not sequential processing.

## Out of scope for Agent 1

- Generating or modifying any code (that's Agent 3).
- Producing a migration plan or recommendation (that's Agent 2).
- Any direct call from Agent 1 into Agent 2 or Agent 3's code paths.
- Executing or sampling ingested code in any form (no sandbox in MVP — see NFR2). Revisit only behind an explicit future spec if dynamic behavior verification is ever justified.

## Decided (previously open questions)

- Job queue: pgmq (Postgres-native), see `design.md` §3. No new infra dependency beyond Supabase.
- Concurrency cap for entity-summary fan-out: 10 concurrent, configurable via env — starting default, revisit after Phase 6 load testing.
- Grounding checker retry cap: 2 retries before flagging `unverified` — starting default, revisit if false-unverified rate is too high in practice.