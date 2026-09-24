# skills.md — Tech Stack & Tooling Inventory (Agents 1 & 2)

This is a reference inventory of every technology/tool Agent 1 and Agent 2 depend on (Agent 2 section at the bottom), why it was chosen, and what it's responsible for. Use this when onboarding, when picking a library for a new task, or when Claude Code needs to know what's already in play before reaching for something new.

## Orchestration

| Tool | Role |
|---|---|
| Python | Primary language for Agent 1. `pyproject.toml` currently pins `requires-python = ">=3.14"` with no dependencies declared yet — verify FastAPI/LangGraph/Supabase-py/pgvector-client have compatible releases before locking this in (Phase 0 task); don't assume bleeding-edge Python is safe by default |
| LangGraph | Installed but **not currently used**: Agent 1 turned out to be a plain pgmq-driven worker loop (`design.md` §3). Each agent stays isolated in its own worker process. Adopting LangGraph later means one node per stage, with no data-model change |
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

---

# Agent 2 (Plan)

Full detail is in `design_agent2.md`. Only what's *new* relative to Agent 1 is listed here. Everything else (FastAPI, Supabase/RLS, pgmq, Azure OpenAI, the pinned embedding model) is reused as-is.

## Graph / clustering

| Tool | Role |
|---|---|
| `python-igraph` | In-memory weighted graph for Leiden. Handles 5–10k+ nodes comfortably in one process |
| `leidenalg` | Leiden community detection (`RBConfigurationVertexPartition`, seeded, run to convergence). Used bottom-up twice: units, then phases (`design_agent2.md` §4) |
| `networkx` | Directed cluster graph: `strongly_connected_components` for cycle detection, `condensation` + `lexicographical_topological_sort` for deterministic ordering (`design_agent2.md` §7–§8) |
| ~~`graspologic` (`hierarchical_leiden`)~~ | **Rejected.** It's top-down (doesn't match the bottom-up nesting design), and it pins older numpy/scipy releases that are risky on the repo's Python ≥3.14. Phase 0 of `tasks_agent2.md` verifies the chosen libraries on 3.14 before anything is built on them |

**Verified on Python 3.14.7 (Windows, prebuilt wheels, no fallback needed):** `python-igraph` 1.0.0, `leidenalg` 0.12.0, `networkx` 3.6.1. Smoke test: `tests/test_agent2_graph_libs.py` (seeded Leiden reproducible; SCC + `lexicographical_topological_sort` on a condensed digraph).

## Queue / worker

| Tool | Role |
|---|---|
| pgmq queue `agent2_planning_jobs` | Agent 2's own queue, never shared with `agent1_ingestion_jobs` |
| `uv run document-analyzer-planner` | Agent 2's own long-lived worker process, separate from Agent 1's worker and from `uvicorn` |

## LLM / embeddings (reused)

| Tool | Role |
|---|---|
| Azure OpenAI | One cluster-report call per unit (fanned out, capped by `AGENT2_REPORT_CONCURRENCY`) and one per phase. The prompt is adapted from GraphRAG's community-report prompt |
| Grounding checker (`worker/ground_retry.py`) | Imported as a pure function. Reports that fail after the retry cap are stored `unverified` and never block |
| `risk_rollup.py::rollup_risk` | Deterministic cluster `risk_level`. The LLM never judges risk |
| Pinned `all-MiniLM-L6-v2` (`embed.py`) | Cluster report embeddings (`vector(384)`). The drift check runs at planner startup |

## Frontend

| Tool | Role |
|---|---|
| React Flow | Plan graph drill-down (phases → units), with `needs_facade` cycle edges highlighted. Chosen over D3 for built-in pan/zoom/node interaction with less custom code |

## Decided (Agent 2)

- Trigger: user-initiated `POST /api/agent2/projects/{id}/plans`. No automatic trigger on Agent 1 completion.
- Cycles: SCC condensation; auto-merge when the merged unit fits under `max_unit_size` inside one phase, otherwise flag `needs_facade`, which blocks plan approval until a human resolves it.
- Validation: Agent 2 emits per-unit **test contracts** from facts only. Agent 3 owns test generation and execution.
- Two levels only (unit, phase). Unverified entity summaries contribute structural edge weight only.
- Starting defaults (tune empirically): α = 0.5, `max_unit_size` = 25 entities, hub percentile = 99, seed = 42.
