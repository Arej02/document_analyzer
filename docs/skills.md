# skills.md — Tech Stack & Tooling Inventory (Agent 1)

This is a reference inventory of every technology/tool Agent 1 depends on, why it was chosen, and what it's responsible for. Use this when onboarding, when picking a library for a new task, or when Claude Code needs to know what's already in play before reaching for something new.

## Orchestration

| Tool | Role |
|---|---|
| Python | Primary language for Agent 1. `pyproject.toml` currently pins `requires-python = ">=3.14"` with no dependencies declared yet — verify FastAPI/LangGraph/Supabase-py/pgvector-client have compatible releases before locking this in (Phase 0 task); don't assume bleeding-edge Python is safe by default |
| LangGraph | Agent 1 is implemented as its own subgraph, isolated from Agents 2 and 3 |
| pgmq (Postgres-native queue, see `design.md` §Job Queue) | Decided job queue tech. Everything long-running (ingestion, extraction, summarization) runs off the request/response cycle. No second infra dependency beyond the existing Supabase Postgres instance |

## Parsing / extraction

| Tool | Role |
|---|---|
| `nikic/php-parser` | AST-level structural extraction for plain PHP, invoked as a subprocess (not a Python-native parser — keep the subprocess boundary) |
| Custom WordPress extractor | Extracts hooks, CPTs (custom post types), and meta registrations — WordPress-specific, sits alongside the PHP parser behind a shared `Extractor` protocol |
| Extractor protocol | Pluggable interface so a new language/framework extractor can be added later without touching the rest of the pipeline |

**Rule:** structural extraction is pure parsing — no LLM calls happen at this stage.

## Storage

| Tool | Role |
|---|---|
| Supabase Postgres | Primary store: `extracted_entities`, `entity_edges`, `knowledge_summaries`, `summary_cache` |
| pgvector | Embedding column + index on `knowledge_summaries` for Agent 2's later retrieval |
| Supabase Auth + RLS | Every table scoped by `org_id` from day one — this is not optional or deferred |

## LLM / summarization

| Tool | Role |
|---|---|
| LLM summarization (bottom-up) — Azure OpenAI | entity → module → subsystem → repo, each call scoped to only that level's own facts. Unlike the embedding model below, facts are sent to Azure per call (design.md §2 "Summarization LLM (decided)") |
| Grounding checker | Validates each generated summary against underlying facts before persisting; retries up to a cap, then flags as `unverified` rather than blocking |
| Embedding model (pinned) | Single fixed model for the whole knowledge base; version pinned in a config table, not in code |

## Sandbox / security

| Tool | Role |
|---|---|
| ~~gVisor / Firecracker-backed container~~ | **Removed from MVP scope.** Agent 1 never executes ingested code in any form — structural extraction is static AST parsing only (see `design.md` §1 note, `specs.md` NFR2). No execution sandbox exists or is planned for v1; this was a scope cut, not a deferral. |
| Secret scanning | Runs before persisting extracted strings to Supabase. On a match: redact the value before persisting, log the finding for audit — never blocks the job. |

Since there is no code execution anywhere in Agent 1, the untrusted-input risk surface is limited to parsing arbitrary (potentially malformed/adversarial) source text and bounding ingestion input size — see `specs.md` NFR2 for the resource-bound requirements.

## Frontend

| Tool | Role |
|---|---|
| React (existing app in `frontend/`) | Upload UI, job status/progress UI, knowledge base browser |
| API client layer | Talks to the FastAPI backend; see `design.md` §API Surface for the contract it needs to implement |

## Not yet decided / open

- How `entity_edges` gets visualized in the frontend, if at all, for Agent 1's scope

## Decided (previously open)

- Job queue: pgmq (see `design.md` §Job Queue)
- Summarization LLM: Azure OpenAI (see `design.md` §2 "Summarization LLM (decided)")
- Concurrency cap for parallel entity-summary fan-out: 10, env-configurable (starting default)
- Grounding checker retry cap: 2 retries before flagging `unverified` (starting default)