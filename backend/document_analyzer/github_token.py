"""GitHub access-token lifecycle — design.md §2 `github_token_ref`, tasks.md Phase 1.

A private-repo access token is stored in Supabase Vault (pgsodium-encrypted),
scoped to a single ingestion job: created when the job is created, resolved
(decrypted) only by the worker when it needs to clone, and deleted on job
completion (success or failure) — never retrievable afterward. Vault functions
live in the `vault` schema and aren't PostgREST-exposed, so this goes over a
direct Postgres connection (SUPABASE_DB_URL), same as queue.py.
"""

import os

import asyncpg


async def store_github_token(job_id: str, access_token: str) -> str:
    """Stores access_token in Vault, scoped to job_id. Returns the secret id to persist as github_token_ref."""
    dsn = os.environ["SUPABASE_DB_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        secret_id = await conn.fetchval(
            "select vault.create_secret($1, $2, $3)",
            access_token,
            f"agent1_github_token_{job_id}",
            f"GitHub access token for ingestion job {job_id}",
        )
        return str(secret_id)
    finally:
        await conn.close()


async def resolve_github_token(github_token_ref: str) -> str | None:
    """Decrypts and returns the token for github_token_ref, or None if it's already been deleted."""
    dsn = os.environ["SUPABASE_DB_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval(
            "select decrypted_secret from vault.decrypted_secrets where id = $1",
            github_token_ref,
        )
    finally:
        await conn.close()


async def delete_github_token(github_token_ref: str) -> None:
    """Permanently deletes the secret. Call on job completion, success or failure, never leave it lingering."""
    dsn = os.environ["SUPABASE_DB_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("delete from vault.secrets where id = $1", github_token_ref)
    finally:
        await conn.close()
