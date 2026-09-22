"""pgmq enqueue/dequeue — design.md §3.

pgmq's functions live in the `pgmq` schema and aren't exposed over PostgREST,
so every operation here goes over a direct Postgres connection (SUPABASE_DB_URL),
not the Supabase client used for RLS-scoped table access. FastAPI only ever
enqueues (`enqueue_ingestion_job`) — `read_next_job`/`delete_job_message` are
the worker main loop's (worker/run.py) read side of the same queue.
"""

import json
import os

import asyncpg

QUEUE_NAME = "agent1_ingestion_jobs"

# How long a read message stays invisible to other readers before pgmq makes it
# visible again for retry — must comfortably cover one job's full fetch ->
# extract -> summarize -> ground pipeline, since a still-running job whose
# message becomes visible again would be picked up a second time concurrently.
DEFAULT_VISIBILITY_TIMEOUT_SECONDS = int(os.environ.get("AGENT1_QUEUE_VISIBILITY_TIMEOUT_SECONDS", 1800))


async def enqueue_ingestion_job(job_id: str, org_id: str) -> None:
    dsn = os.environ["SUPABASE_DB_URL"]
    payload = json.dumps({"job_id": job_id, "org_id": org_id})
    conn = await asyncpg.connect(dsn)
    try:
        await conn.fetchval("select pgmq.send($1, $2::jsonb)", QUEUE_NAME, payload)
    finally:
        await conn.close()


async def read_next_job(vt_seconds: int | None = None) -> dict | None:
    """Reads (without deleting) the oldest visible message, or None if the queue is
    currently empty. Returns `{"msg_id", "job_id", "org_id"}` — `msg_id` is needed by
    `delete_job_message` once the job has actually been processed (success or a
    handled failure); a message that's never deleted becomes visible again after
    `vt_seconds` for another read (crash recovery, not exactly-once)."""
    dsn = os.environ["SUPABASE_DB_URL"]
    vt = vt_seconds if vt_seconds is not None else DEFAULT_VISIBILITY_TIMEOUT_SECONDS
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow("select msg_id, message from pgmq.read($1, $2, 1)", QUEUE_NAME, vt)
        if row is None:
            return None
        message = json.loads(row["message"])
        return {"msg_id": row["msg_id"], "job_id": message["job_id"], "org_id": message["org_id"]}
    finally:
        await conn.close()


async def delete_job_message(msg_id: int) -> None:
    """Removes a message from the queue for good, once its job has been handled."""
    dsn = os.environ["SUPABASE_DB_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        # $2::bigint disambiguates pgmq.delete's two overloads (msg_id bigint vs.
        # msg_ids bigint[]) — asyncpg sends an untyped param, which Postgres can't
        # resolve against both on its own (AmbiguousFunctionError).
        await conn.fetchval("select pgmq.delete($1, $2::bigint)", QUEUE_NAME, msg_id)
    finally:
        await conn.close()
