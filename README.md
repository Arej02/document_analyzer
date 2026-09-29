# Galvanise

Migrates legacy codebases (starting with PHP / WordPress) to FastAPI. Three independent agents, connected only through Supabase:

- **Agent 1 — Understand:** codebase → queryable knowledge base
- **Agent 2 — Plan:** knowledge base → reviewable migration plan
- **Agent 3 — Execute:** approved plan → generated code + test verdicts (in progress)

Architecture rules are in [`CLAUDE.md`](CLAUDE.md). Specs, design and task lists are in [`docs/`](docs/).

## Repo layout

```
backend/            FastAPI app, workers, container images
frontend/           React app (Vite)
supabase/migrations SQL migrations
tests/              pytest suite
docs/               specs, design, tasks
```

## Prerequisites

- Python 3.14+ and [uv](https://docs.astral.sh/uv/)
- Node.js and npm
- A [Supabase](https://supabase.com) project
- `git`
- A container engine, one of:
  - **macOS/Linux (default):** [Colima](https://github.com/abiosoft/colima) and the `docker` CLI: `brew install colima docker`
  - **Windows, or anyone who prefers Docker:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) (WSL 2 backend). On native Windows this is picked automatically; elsewhere, including inside WSL, set `CONTAINER_ENGINE=docker` in `.env`
- PHP + Composer, then `composer install` in `backend/php/`

## Setup

1. **Environment:** `cp .env.example .env` and fill in the required values:

   | Variable | Where to find it |
   |---|---|
   | `SUPABASE_DB_URL` | Connect → **Session pooler** (port 5432) |
   | `SUPABASE_ANON_KEY` | Project Settings → API Keys → Legacy |
   | `SUPABASE_SERVICE_ROLE_KEY` | Project Settings → API Keys → Legacy (keep secret) |
   | `AZURE_OPENAI_*` | Your Azure OpenAI resource |

   The frontend needs no `.env`; it gets its Supabase settings from the backend.

2. **Database:** apply the migrations. Re-run this after every pull that adds one.

   ```
   uv run document-analyzer-setup-db
   uv run document-analyzer-setup-db --status
   ```

3. **Dependencies:**

   ```
   uv sync
   cd frontend && npm install
   ```

4. **Containers:**

   Start your engine (`colima start --runtime docker --cpus 4 --memory 8`, or open Docker Desktop), then:

   ```
   uv run document-analyzer-build-graphify-image
   uv run document-analyzer-build-agent3-image
   uv run document-analyzer-doctor
   ```

   The doctor prints which engine is active. To switch, set `CONTAINER_ENGINE=colima` or `CONTAINER_ENGINE=docker` in `.env` and rebuild both images once (images live per engine). Native Windows is untested beyond the containers; running the backend inside WSL 2 is the safer path.

5. **Auth:** in Supabase → Authentication → Sign In / Providers → Email, turn off **Confirm email**. Then just sign up in the app; each account gets its own org automatically. To share an org with a teammate: `uv run document-analyzer-add-org-member <their-email> <your-org-id>` (`--list` shows org ids).

## Running

Each in its own terminal, from the repo root:

| Process | Command |
|---|---|
| API (`localhost:8000`) | `uv run python -m uvicorn document_analyzer.app:app --reload` |
| Agent 1 worker | `uv run document-analyzer-worker` |
| Agent 2 planner | `uv run document-analyzer-planner` |
| Agent 3 executor | `uv run document-analyzer-executor` |
| Frontend (`localhost:5173`) | `cd frontend && npm run dev` |

A job stays `queued` until its agent's worker is running.

## Tests

```
uv run pytest
```
