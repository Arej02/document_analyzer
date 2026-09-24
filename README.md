# Galvanise

Agentic system that migrates legacy codebases (starting with PHP / WordPress) to a
modern stack (FastAPI). The full pipeline is three independent agents connected
only through a shared Supabase store — Agent 1 (**Understand**, this repo),
Agent 2 (Plan), Agent 3 (Execute). **This repo currently implements Agent 1
only.**

See [`CLAUDE.md`](CLAUDE.md) for the architectural rules, [`docs/specs.md`](docs/specs.md)
for the functional spec, [`docs/design.md`](docs/design.md) for the data model and
API contract, and [`docs/tasks.md`](docs/tasks.md) for what's built vs. outstanding.

## Repo layout

```
/
├── backend/          # FastAPI app (Python) — Agent 1's API + worker
├── frontend/          # React app (Vite) — upload UI, job status, knowledge base browser
├── supabase/
│   └── migrations/    # SQL migrations — tables, RLS policies, pgmq queue, Vault
├── tests/             # Backend tests (pytest)
└── docs/               # specs.md, design.md, skills.md, tasks.md
```

## Prerequisites

- Python 3.14+ and [uv](https://docs.astral.sh/uv/)
- Node.js and npm
- A [Supabase](https://supabase.com) project (Postgres + Auth + Storage + pgmq + Vault)
- `git`, and the `git` CLI available on `PATH` (the worker shells out to it to clone repos)

## Setup

1. **Environment variables** — copy `.env.example` to `.env` and fill in your Supabase project's values (Project Settings → API / Database):

   ```
   cp .env.example .env
   ```

   | Variable | Where to find it |
   |---|---|
   | `SUPABASE_URL` | Project Settings → API |
   | `SUPABASE_ANON_KEY` | Project Settings → API |
   | `SUPABASE_SERVICE_ROLE_KEY` | Project Settings → API (keep secret — worker-only, bypasses RLS) |
   | `SUPABASE_JWT_SECRET` | Project Settings → API → JWT Settings |
   | `SUPABASE_DB_URL` | Project Settings → Database (direct connection string — used for pgmq/Vault calls that aren't exposed over PostgREST) |

2. **Apply database migrations** — run everything in `supabase/migrations/` against your project, in order (via the [Supabase CLI](https://supabase.com/docs/guides/cli): `supabase link` then `supabase db push`, or paste each file into the SQL Editor in order). This creates the tables, RLS policies, the `agent1_ingestion_jobs` pgmq queue, the `agent1-uploads` storage bucket, and enables Vault.

3. **Install backend dependencies:**

   ```
   uv sync
   ```

4. **Install frontend dependencies:**

   ```
   cd frontend
   npm install
   ```

## Running it

Run these in separate terminals from the repo root.

**Backend** (FastAPI, on `http://localhost:8000`):

```
uv run uvicorn document_analyzer.app:app --reload
```

API docs are then available at `http://localhost:8000/docs`.

**Worker** (polls `agent1_ingestion_jobs` and runs the ingestion pipeline):

```
uv run document-analyzer-worker
```

This is a separate long-lived process from the FastAPI app — nothing in Agent 1
runs the pipeline synchronously from a request (see `CLAUDE.md` "Async by
default"). It polls the queue every 5 seconds when idle and processes one job
at a time; Ctrl+C to stop.

**Planner** (polls `agent2_planning_jobs` and builds migration plans):

```
uv run document-analyzer-planner
```

Agent 2's own worker, separate from the ingestion worker above. Without it,
"Generate plan" requests stay `queued` forever. Same polling behaviour:
every 5 seconds when idle, one job at a time.

**Frontend** (Vite dev server, on `http://localhost:5173`):

```
cd frontend
npm run dev
```

The dev server proxies `/api/*` requests to `http://localhost:8000` (see
`frontend/vite.config.js`), so the two need to be running together for the app
to actually work end to end.

## Tests

```
uv run pytest
```

## Notes

- Frontend auth (attaching a Supabase session JWT to API requests) isn't wired
  up yet — see the `TODO`s in `frontend/src/main.jsx` and `docs/tasks.md`.
