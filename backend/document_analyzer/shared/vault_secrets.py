"""Generic Supabase Vault secret helpers — design_agent3.md §15.7.

Same storage technology as Agent 1's `github_token.py` (`vault.create_secret`,
`vault.decrypted_secrets`, `delete from vault.secrets`, over a direct Postgres connection because
the `vault` schema is not exposed through PostgREST), but with a different lifecycle: Agent 1's token
is per ingestion job and deleted when the job ends; the delivery token belongs to a project and lives
until the user disconnects or rotates it. Agent 1's module is deliberately neither imported nor changed
(its docstring promises delete-on-job-end, and its `agent1_github_token_*` secrets must never be
reachable from here), so callers pass the whole secret name and only ever delete a ref they stored.

A secret's `name` is unique in Vault, so a caller that must create a replacement before deleting the old
secret (rotation) needs a distinct name for each.
"""

import os

import asyncpg


async def store_secret(name: str, description: str, value: str) -> str:
    """Stores `value` under a unique `name`; returns the new secret's id (a uuid string)."""
    conn = await asyncpg.connect(os.environ["SUPABASE_DB_URL"])
    try:
        secret_id = await conn.fetchval("select vault.create_secret($1, $2, $3)", value, name, description)
        return str(secret_id)
    finally:
        await conn.close()


async def resolve_secret(secret_id: str) -> str | None:
    """The decrypted value, or None if the secret no longer exists."""
    conn = await asyncpg.connect(os.environ["SUPABASE_DB_URL"])
    try:
        return await conn.fetchval("select decrypted_secret from vault.decrypted_secrets where id = $1", secret_id)
    finally:
        await conn.close()


async def delete_secret(secret_id: str) -> None:
    conn = await asyncpg.connect(os.environ["SUPABASE_DB_URL"])
    try:
        await conn.execute("delete from vault.secrets where id = $1", secret_id)
    finally:
        await conn.close()
