# CLAUDE.md — Legacy Migration Agent (Agent 1: Understand)

This file orients Claude / Claude Code whenever it works in this repo. Read this first, then `docs/specs.md` and `docs/design.md` for detail, and `docs/tasks.md` for what's left to do.

## What this project is

An agentic system that migrates legacy codebases (starting with PHP / WordPress) to a modern stack (FastAPI). The pipeline is three independent agents connected only through a shared Supabase store — no agent calls another agent directly:

1. **Agent 1 (Understand)** — codebase → queryable knowledge base. *This is the current scope.*
2. **Agent 2 (Plan)** — knowledge base → reviewable migration plan. *Not yet built.*
3. **Agent 3 (Execute)** — approved plan → generated code + test verdicts. *Not yet built.*

This repo currently implements **Agent 1 only**. Agents 2 and 3 are out of scope until Agent 1 is stable.

## Repo layout (expected)

```
/
├── CLAUDE.md              # this file
├── docs/
│   ├── skills.md          # tech stack / tooling inventory & why each was chosen
│   ├── specs.md           # functional + technical spec for Agent 1
│   ├── design.md          # architecture, data model, sequence of operations
│   └── tasks.md           # phased task list, backend tasks paired with frontend tasks
├── backend/                # Python / FastAPI / LangGraph — adjust path if different
└── frontend/                # existing React app — already scaffolded, not created by this pass
```

If your actual folder names differ (e.g. `api/` instead of `backend/`), treat the names above as placeholders and keep the docs/ and root CLAUDE.md structure as requested.

## Core architectural rules — do not violate these

- **No direct agent-to-agent calls.** Agent 1 writes to Supabase; it never invokes Agent 2 or Agent 3 code. This keeps each agent independently testable and re-runnable.
- **Async by default.** Ingestion and (later) execution can run for minutes to hours. Nothing in Agent 1 should be called synchronously from a FastAPI request/response cycle — everything goes through a job queue (see `docs/design.md` §Job Queue).
- **Untrusted input.** Ingested repos are arbitrary user-submitted code. Any execution of that code (e.g. the sampling sandbox) must be network-isolated and resource-limited. Never relax this for convenience.
- **Multi-tenancy from day one.** Every Supabase table is scoped by `org_id` with RLS. Don't add a table without RLS policies.
- **Embedding model is pinned**, not hardcoded — see `docs/design.md` §Embeddings. If it ever changes, that's an explicit migration, not a silent update.
- **Grounding checker failures never block ingestion.** A bad module gets flagged `unverified`, the rest of the repo keeps processing.

## Tech stack (short version — full detail in `docs/skills.md`)

- Orchestration: Python, LangGraph (Agent 1 is its own subgraph)
- PHP parsing: `nikic/php-parser` via subprocess
- WordPress parsing: custom extractor (hooks/CPTs/meta)
- Storage: Supabase Postgres (+ pgvector for embeddings)
- Sandbox: gVisor/Firecracker-backed, no network access
- Frontend: React (existing, in `frontend/`)

## Working conventions

- Every new backend endpoint that Agent 1 exposes must have a matching frontend task in `docs/tasks.md` — check there before considering a backend feature "done."
- Keep `docs/tasks.md` checkboxes up to date as you complete work; it's the single source of truth for what's left.
- Don't invent new tables/endpoints without first updating `docs/design.md` — design changes should be visible in the docs, not just in code.