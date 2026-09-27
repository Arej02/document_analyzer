# skills.md — Tech Stack & Tooling Inventory (Agents 1, 2 & 3)

This is a reference inventory of every technology/tool Agents 1, 2 and 3 depend on (the Agent 2 and Agent 3 sections are at the bottom), why it was chosen, and what it's responsible for. Use this when onboarding, when picking a library for a new task, or when Claude Code needs to know what's already in play before reaching for something new.

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

Agent 3 is the only place anything runs, and only the Python it generated, never ingested code (see the Agent 3 section below). Since there is no code execution anywhere in Agent 1, the untrusted-input risk surface is limited to parsing arbitrary (potentially malformed/adversarial) source text and bounding ingestion input size — see `specs.md` NFR2 for the resource-bound requirements.

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

---

# Agent 3 (Execute)

Full detail is in `design_agent3.md`. Only what's *new* relative to Agents 1 and 2 is listed here. Everything else (FastAPI, Supabase/RLS/Storage, pgmq, Azure OpenAI) is reused as-is.

## Sandbox

Agent 3 is the first agent that runs code. It runs only the Python it generated, never the legacy PHP, and only inside this sandbox (`design_agent3.md` §7, specs NFR2).

| Tool | Role | Why |
|---|---|---|
| Docker (CLI, via `subprocess`) | Runs each unit's pytest suite in a throwaway container: `--network none`, `--read-only` with a `noexec` tmpfs `/tmp`, `--cap-drop ALL`, `no-new-privileges`, uid 65534, memory/CPU/pids/nofile limits, and a wall-clock timeout enforced with `docker kill` (`agent3/worker/sandbox.py`) | Every restriction the spec requires is a standard `docker run` flag, and every flag is checked by a live smoke test (`tests/test_agent3_sandbox.py`). It's driven through the CLI the same way Agent 1 shells out to `git` and `php`, so there's no Docker SDK dependency. The worker never imports generated code in-process |
| gVisor (`runsc`) | Container runtime that puts a user-space kernel between the generated code and the host kernel. Selected with `AGENT3_SANDBOX_RUNTIME=runsc`. `AGENT3_REQUIRE_GVISOR=true` makes the executor refuse to start without it | Namespaces alone share the host kernel, so a kernel bug is a container escape. gVisor closes that gap without the operational weight of a microVM (Firecracker). It's **required in production and unavailable on Docker Desktop** (Windows/WSL 2), so developer machines run `runc` with every other flag and log a warning. It has not been verified on a real host yet (`tasks_agent3.md` Phase 6) |
| Stdlib `xml.etree.ElementTree` | Parses pytest's JUnit XML | No new dependency. The XML comes out of an untrusted container, so it's size-capped and refused if it declares a `DOCTYPE` or entities *before* parsing, which removes the entity-expansion attacks `defusedxml` exists for |

**Known limitation:** the sandbox protects the host, not the verdict. Generated code runs in the same process as pytest and could fake its own results, so verdicts are advisory (`design_agent3.md` §7).

## Runner image `galvanise-agent3-runner:0.1`

Built from `backend/agent3_runner/Dockerfile` on `python:3.14-slim` (Python 3.14.7, about 65 MB). It holds everything generated code and its tests can import. There's no pip install at run time, because the sandbox has no network.

| Package (pinned in `requirements.txt`) | Why it's in the image |
|---|---|
| `fastapi==0.141.1` | The target framework (`design_agent3.md` §8). Same version as `uv.lock` |
| `pydantic==2.13.5` | FastAPI's models. Same version as `uv.lock` |
| `httpx==0.28.1` | FastAPI's `TestClient` needs it, and `wp_compat`'s HTTP adapter uses it (`wp_remote_*`). Same version as `uv.lock` |
| `sqlalchemy==2.1.0` | Replaces `$wpdb` and backs the generated CPT/post-meta models (§8). Tests use in-memory SQLite, so no DB driver is needed |
| `pytest==9.1.1` | Runs contract and behaviour tests. Same version as the repo's dev group |
| `pytest-timeout==2.4.0` | Per-test timeouts, set by the template's pytest config (§8), so one hanging test fails by itself instead of using up the whole run's wall clock |

The image's transitive dependencies aren't pinned yet (for example `starlette 1.7.0` and `pydantic_core 2.46.5` in the current build). A rebuild can pick up newer ones. The production digest pin (below) makes a given run reproducible, but locking them at build time too (a `pip freeze` constraints file) is open.

**Where the image comes from** (`design_agent3.md` §7):
- **Development:** a local build, `docker build -t galvanise-agent3-runner:0.1 backend/agent3_runner`. A missing image stops the executor at startup, and the error prints that command.
- **Production:** a private registry (GHCR, ACR or Docker Hub; not chosen yet), with `AGENT3_RUNNER_IMAGE` set to the image's `@sha256:` digest rather than a tag. The deploy step pulls it, because the sandbox uses `--pull never`. Every image change gets a new version tag and digest. This is a deferred task in `tasks_agent3.md` Phase 6.

## Queue / worker / storage

| Tool | Role |
|---|---|
| pgmq queue `agent3_execution_jobs` | Agent 3's own queue, never shared. The message is `{execution_job_id, org_id}`, and the visibility timeout is 3600 s (`AGENT3_QUEUE_VISIBILITY_TIMEOUT_SECONDS`), longer than Agents 1 and 2 because jobs run longer |
| `uv run document-analyzer-executor` | Agent 3's own long-lived worker. At startup it checks for Docker, the runner image and (when required) gVisor. There's no embedding check, because Agent 3 doesn't embed |
| Supabase Storage bucket `agent3-output` | Private. Holds generated code, tests, logs and JUnit XML for every attempt, plus the assembled project zip. Users can only read their own org's folder, only the worker writes, and downloads go through 5-minute signed URLs |
| `SUPABASE_DB_URL` = Session pooler | pgmq is reached over a direct Postgres connection. The pooler string works on IPv4-only networks, while the direct `db.<ref>.supabase.co` host is IPv6-only |

## Decided (Agent 3)

- Trigger: user-initiated `POST /api/agent3/plans/{plan_id}/executions`, on approved, non-stale plans only.
- Verification: deterministic contract tests plus LLM behaviour tests, run only on generated Python and only in the sandbox. There's no legacy PHP execution in the MVP.
- Isolation: Docker with the flags above everywhere, plus gVisor in production. Not Firecracker: gVisor gives kernel isolation with plain `docker run`.
- Output: the `agent3-output` bucket and a zip download. No GitHub PRs.
- Starting defaults (tune on the first real WordPress plan): 3 attempts per unit, a 120 s sandbox timeout, 1024 MB of memory, 1 CPU, 256 pids.

