"""Cost tables are internal-only (design.md section 7.1): no anon or authenticated user, of any
org, may read `llm_invocations`, `llm_job_costs` or the `llm_cost_by_*` views, and the
invocation log is append-only.

Like test_rls_multi_tenancy.py this needs the real database, because row security and grants are
enforced by Postgres, not by anything a fake can model. It connects with SUPABASE_DB_URL, does
everything inside one transaction (seeding rows, switching to the `anon` / `authenticated` /
`service_role` roles the way PostgREST does), and rolls the whole thing back, so nothing is left
behind. That matters here: `llm_invocations` is append-only, so rows could not be cleaned up.

Skips when SUPABASE_DB_URL is unset, or when the migration
(supabase/migrations/20261001000000_llm_cost_accounting.sql) hasn't been applied to that project.
"""

import json
import os
import uuid

import asyncpg
import pytest
from dotenv import load_dotenv

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.environ.get("SUPABASE_DB_URL"), reason="requires a live database (SUPABASE_DB_URL)"
)

TABLES = ["llm_invocations", "llm_job_costs"]
VIEWS = ["llm_cost_by_job", "llm_cost_by_job_stage", "llm_cost_by_job_purpose", "llm_cost_by_execution_unit"]
RELATIONS = TABLES + VIEWS


_MIGRATION_MISSING = "the llm cost accounting migration has not been applied to this database"
_migration_applied: bool | None = None  # probed once, so a missing migration skips quickly


async def _connect():
    if _migration_applied is False:
        pytest.skip(_MIGRATION_MISSING)
    return await asyncpg.connect(os.environ["SUPABASE_DB_URL"], statement_cache_size=0)


async def _require_migration(conn):
    global _migration_applied
    _migration_applied = await conn.fetchval("select to_regclass('public.llm_invocations')") is not None
    if not _migration_applied:
        await conn.close()
        pytest.skip(_MIGRATION_MISSING)


async def _seed(conn, org_id, job_id):
    await conn.execute(
        "insert into llm_invocations (org_id, agent, job_type, job_id, purpose, status, cost_usd, total_tokens) "
        "values ($1, 'agent1', 'ingestion', $2, 'entity_summary', 'ok', 0.01, 10)",
        org_id,
        job_id,
    )
    await conn.execute(
        "insert into llm_job_costs (org_id, agent, job_type, job_id, tokens, cost_usd, calls) "
        "values ($1, 'agent1', 'ingestion', $2, 10, 0.01, 1)",
        org_id,
        job_id,
    )


async def _count_as(conn, role, relation, claims=None):
    """Rows `relation` shows to `role`, or None when the role is refused outright. Each probe runs
    in its own savepoint so a refusal can't poison the surrounding transaction, and the
    `set local role` is undone with it."""
    savepoint = conn.transaction()
    await savepoint.start()
    try:
        await conn.execute(f"set local role {role}")
        if claims:
            await conn.execute("select set_config('request.jwt.claims', $1, true)", json.dumps(claims))
        return await conn.fetchval(f"select count(*) from {relation}")
    except asyncpg.InsufficientPrivilegeError:
        return None
    finally:
        await savepoint.rollback()


@pytest.mark.parametrize("role", ["anon", "authenticated"])
@pytest.mark.parametrize("relation", RELATIONS)
async def test_tenant_roles_read_nothing_from_any_cost_table_or_view(role, relation):
    conn = await _connect()
    await _require_migration(conn)
    org, job = str(uuid.uuid4()), str(uuid.uuid4())
    tx = conn.transaction()
    await tx.start()
    try:
        await _seed(conn, org, job)
        assert await conn.fetchval(f"select count(*) from {relation}") >= 1 or relation == "llm_cost_by_execution_unit", (
            "positive control: the seeded row is visible to the owner, so a zero below means access was blocked"
        )
        # An authenticated member of the very org that owns the rows still sees nothing.
        claims = {"sub": str(uuid.uuid4()), "role": role, "org_id": org}
        seen = await _count_as(conn, role, relation, claims)
        assert seen in (None, 0), f"{role} could read {seen} row(s) of {relation}"
    finally:
        await tx.rollback()
        await conn.close()


async def test_the_invocation_log_is_append_only_even_for_the_service_role():
    conn = await _connect()
    await _require_migration(conn)
    tx = conn.transaction()
    await tx.start()
    try:
        await _seed(conn, str(uuid.uuid4()), str(uuid.uuid4()))
        for statement in ("update llm_invocations set purpose = 'x'", "delete from llm_invocations"):
            savepoint = conn.transaction()
            await savepoint.start()
            try:
                await conn.execute("set local role service_role")
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute(statement)
            finally:
                await savepoint.rollback()
        # ...while the service role can still append and read, which is all the workers need.
        assert await _count_as(conn, "service_role", "llm_invocations") >= 1
    finally:
        await tx.rollback()
        await conn.close()


async def test_rls_is_enabled_with_no_tenant_policy_and_the_views_run_as_the_caller():
    conn = await _connect()
    await _require_migration(conn)
    try:
        for table in TABLES:
            assert await conn.fetchval("select relrowsecurity from pg_class where oid = $1::regclass", table) is True
            policies = await conn.fetchval("select count(*) from pg_policies where schemaname = 'public' and tablename = $1", table)
            assert policies == 0, f"{table} must have no policies (deny by default), found {policies}"
        for view in VIEWS:
            options = await conn.fetchval("select reloptions from pg_class where oid = $1::regclass", view)
            assert options and "security_invoker=true" in options, f"{view} must be security_invoker"
    finally:
        await conn.close()
