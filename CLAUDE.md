# CLAUDE.md — Legacy Migration Agent (Agent 1: Understand, Agent 2: Plan, Agent 3: Execute)

This file orients Claude / Claude Code whenever it works in this repo. Read it first. Then, for the agent you're working on:

- **Agent 1:** `docs/specs.md`, `docs/design.md`, `docs/tasks.md`
- **Agent 2:** `docs/specs_agent2.md`, `docs/design_agent2.md`, `docs/tasks_agent2.md`
- **Agent 3:** `docs/specs_agent3.md`, `docs/design_agent3.md`, `docs/tasks_agent3.md`
- **Frontend redesign (Agent 1 run view, Agent 2 plan document):** `docs/tasks_frontend.md`, reference prototype in `playground/`

`docs/skills.md` covers the tech stack for all three.

## What this project is

An agentic system that migrates legacy codebases (starting with PHP / WordPress) to a modern stack (FastAPI). The pipeline is three independent agents connected only through a shared Supabase store. No agent calls another agent directly:

1. **Agent 1 (Understand)**: codebase → queryable knowledge base. *Built (v0); remaining work is in `docs/tasks.md`.*
2. **Agent 2 (Plan)**: knowledge base → reviewable, ordered migration plan (Leiden-clustered migration units and phases, cycle handling, per-phase human approval, per-unit test contracts). *Current scope. Specified, not yet built.*
3. **Agent 3 (Execute)**: approved plan → generated code + test verdicts. *Specified (2026-09-25), not yet built. Blocked first on the Agent 1 source-snapshot tasks in `docs/tasks.md`.*

## Repo layout

```
/
├── CLAUDE.md                # this file
├── docs/
│   ├── skills.md            # tech stack / tooling inventory (Agents 1, 2 + 3)
│   ├── architecture.md      # cross-agent architecture diagrams (Mermaid)
│   ├── specs.md             # Agent 1 functional + technical spec
│   ├── design.md            # Agent 1 architecture, data model, API
│   ├── tasks.md             # Agent 1 phased tasks (backend paired with frontend)
│   ├── specs_agent2.md      # Agent 2 spec
│   ├── design_agent2.md     # Agent 2 architecture, data model, algorithms, API
│   ├── tasks_agent2.md      # Agent 2 phased tasks (backend paired with frontend)
│   ├── specs_agent3.md      # Agent 3 spec
│   ├── design_agent3.md     # Agent 3 architecture, data model, sandbox, target architecture, API
│   ├── tasks_agent3.md      # Agent 3 phased tasks (backend paired with frontend)
│   └── tasks_frontend.md    # Frontend redesign tasks (Agent 1 run view, Agent 2 plan document)
├── backend/document_analyzer/          # Python / FastAPI
│   ├── (Agent 1 API modules), worker/  # Agent 1 worker: `uv run document-analyzer-worker`
│   │   └── worker/graphify/            # Graphify extractor host side (container runner, validation, mapping)
│   ├── graphify_worker/                # stdlib-only code that runs *inside* the Graphify image
│   ├── ask/                            # cited Q&A over a job (POST /api/agent1/jobs/{id}/ask)
│   ├── agent2/                         # Agent 2 API router + worker: `uv run document-analyzer-planner`
│   └── agent3/                         # Agent 3 API router + worker: `uv run document-analyzer-executor` (planned)
├── backend/graphify_runner/ # Graphify image: `uv run document-analyzer-build-graphify-image`
├── supabase/migrations/     # every table/queue/RLS change goes here
└── frontend/                # existing React app
```

## Core architectural rules — do not violate these

- **No direct agent-to-agent calls.** Agents communicate only through Supabase tables. Agent 1 never enqueues work for Agent 2, and Agent 2 is triggered by the user (`POST /api/agent2/projects/{id}/plans`). Importing a *pure* helper function (no DB access, no pipeline state, e.g. `risk_rollup`, `check_grounding_with_retry`, `embed_text`) is allowed and isn't an agent call. If a helper needs agent-specific changes, move it into `document_analyzer/shared/` rather than forking it.
- **Each agent has its own queue and worker.** `agent1_ingestion_jobs` / `agent2_planning_jobs` are never shared.
- **Agent 2 is read-only on Agent 1 tables.** Gaps in Agent 1 data are worked around in Agent 2 and fixed in Agent 1's own tasks (`design_agent2.md` §13).
- **Only approved plans flow downstream.** Agent 3 reads only `migration_plans.status = approved` (and not stale). Agent 3 is read-only on Agent 1 and Agent 2 tables (`design_agent3.md` §13).
- **Plans are immutable snapshots.** A plan is pinned to one Agent 1 `job_id`. Reviewer edits create a new plan version, and re-ingestion marks older plans `stale`. Nothing is mutated in place.
- **Clustering is deterministic.** Leiden is seeded, and the seed and all params are persisted on the planning job.
- **Async by default.** Ingestion and planning run for minutes to hours. Nothing long-running is called synchronously from a FastAPI request. Everything goes through the job queues (`design.md` §3, `design_agent2.md` §1).
- **Untrusted input, no code execution.** Ingested repos are arbitrary user-submitted code. Neither Agent 1 nor Agent 2 executes any of it (static parsing only). Graphify's parsers run in a disposable, network-less, hardened container because they read hostile input, not because anything from the repo runs (`design.md` §5a); keep Graphify out of the API/worker environment. Repository text that reaches an LLM (source excerpts, Q&A evidence) is untrusted data: fence it or mark it untrusted in the system prompt, never let it act as instructions. Agent 2's test contracts are derived from facts. Agent 3 runs only the Python it generated, never the legacy code, and only inside its sandbox (`design_agent3.md` §7): no network, read-only root, no capabilities, resource limits, a timeout, and gVisor in production. The worker never imports generated code in-process.
- **Multi-tenancy from day one.** Every Supabase table is scoped by `org_id` with RLS. Don't add a table without RLS policies. Worker writes use the service-role connection, stamp `org_id` from the job row (never from repo content), and filter updates by both `id` and `org_id`.
- **Embedding model is pinned**, not hardcoded. See `design.md` §2 Embeddings. Agent 2's cluster report embeddings use the same pin. A change is an explicit migration, never a silent update.
- **Grounding failures never block.** A bad summary or cluster report gets flagged `unverified`, and the rest of the job keeps processing.

## Tech stack (short version — full detail in `docs/skills.md`)

- Orchestration: Python 3.14, FastAPI, pgmq-driven long-lived worker processes (LangGraph is optional and not currently used)
- PHP parsing: `nikic/php-parser` via subprocess; WordPress: custom extractor (hooks/CPTs/meta); Laravel: routes
- Every other code language: Graphify 0.9.68 (tree-sitter) in the `document-analyzer-graphify` image (`backend/graphify_runner/`), `AGENT1_GRAPHIFY=auto|required|off`
- Source evidence: secret-redacted excerpts in `source_chunks` (pgvector), fed to summaries/grounding and to cited Q&A (`ask/`)
- Storage: Supabase Postgres (+ pgvector), Supabase Auth, Vault
- LLM: Azure OpenAI (summaries, cluster reports); embeddings: local pinned `all-MiniLM-L6-v2`
- Agent 2 graph work: `python-igraph` + `leidenalg` (clustering), `networkx` (SCC / topological sort)
- Containers: **`CONTAINER_ENGINE=colima|docker`, Colima by default (docker by default on Windows).** Every container call goes through `shared/container_engine.py`. With `colima` it pins the `COLIMA_PROFILE` socket (the ambient docker context is ignored), refuses an engine that isn't that Colima VM, and stages anything mounted under `CONTAINER_WORKDIR` (Colima shares only `$HOME` and `/tmp/colima`). With `docker` it uses the ambient Docker engine (e.g. Docker Desktop). It never falls back from one engine to the other. Don't shell out to `docker` directly. `uv run document-analyzer-doctor` checks the setup
- Agent 3 sandbox: `docker` CLI client against the configured engine (`agent3/worker/sandbox.py`), gVisor `runsc` in production, runner image `galvanise-agent3-runner` (`backend/agent3_runner/`, `uv run document-analyzer-build-agent3-image`)
- Frontend: React (existing, in `frontend/`), React Flow for Agent 2's plan graph

## Working conventions

- Every new backend endpoint must have a matching frontend task in that agent's tasks file (`docs/tasks.md`, `docs/tasks_agent2.md` or `docs/tasks_agent3.md`). Check there before considering a backend feature "done."
- Keep the tasks-file checkboxes up to date as you complete work. They're the single source of truth for what's left.
- Don't invent new tables or endpoints without first updating the relevant design doc (`docs/design.md`, `docs/design_agent2.md` or `docs/design_agent3.md`). Design changes should be visible in the docs, not just in code.
