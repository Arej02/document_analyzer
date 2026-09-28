"""Apply supabase/migrations/ to the database at SUPABASE_DB_URL.

Needs only the connection string, so a brand-new Supabase project gets its whole
schema (tables, RLS, pgmq queues, Vault, storage buckets, embedding pin) from one
command. Each file runs in its own transaction and is recorded in
`supabase_migrations.schema_migrations` — the Supabase CLI's own tracking table, so
`supabase db push` stays compatible — and re-running applies only what's new.
Re-run after every pull that adds a migration.

Use the Session pooler connection string (Connect -> Session pooler, port 5432).
The transaction pooler (port 6543) breaks asyncpg's prepared statements.

Usage:
    uv run document-analyzer-setup-db                      apply pending migrations
    uv run document-analyzer-setup-db --status             list applied/pending, write nothing
    uv run document-analyzer-setup-db --baseline VERSION   mark every migration up to VERSION
                                                           applied without running it (a DB
                                                           whose schema was pasted in by hand)
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

import asyncpg
from dotenv import load_dotenv

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
TRANSACTION_POOLER_PORT = 6543

_TRACKING_DDL = """
create schema if not exists supabase_migrations;
create table if not exists supabase_migrations.schema_migrations (
    version text primary key,
    statements text[],
    name text
);
"""


class Migration(NamedTuple):
    version: str
    name: str
    path: Path


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """`<version>_<name>.sql`, sorted by version — the Supabase CLI's naming convention."""
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        version, _, name = path.stem.partition("_")
        if not version.isdigit():
            raise ValueError(f"migration {path.name!r} doesn't start with a numeric version")
        migrations.append(Migration(version, name, path))
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise ValueError("two migrations share a version")
    return migrations


def pending(migrations: list[Migration], applied_versions: set[str]) -> list[Migration]:
    return [m for m in migrations if m.version not in applied_versions]


def is_transaction_pooler(dsn: str) -> bool:
    try:
        return urlsplit(dsn).port == TRANSACTION_POOLER_PORT
    except ValueError:
        return False


async def _applied_versions(conn) -> set[str]:
    await conn.execute(_TRACKING_DDL)
    rows = await conn.fetch("select version from supabase_migrations.schema_migrations")
    return {row["version"] for row in rows}


async def _record(conn, migration: Migration, statements: list[str]) -> None:
    await conn.execute(
        "insert into supabase_migrations.schema_migrations (version, name, statements) values ($1, $2, $3)",
        migration.version,
        migration.name,
        statements,
    )


async def _apply(conn, migrations: list[Migration]) -> int:
    todo = pending(migrations, await _applied_versions(conn))
    if not todo:
        print("Database is up to date.")
        return 0
    for migration in todo:
        sql = migration.path.read_text()
        print(f"applying {migration.path.name} ... ", end="", flush=True)
        try:
            # One transaction per file: `ALTER TYPE ... ADD VALUE` migrations rely on
            # being committed before a later file uses the new value.
            async with conn.transaction():
                await conn.execute(sql)
                await _record(conn, migration, [sql])
        except asyncpg.PostgresError as exc:
            print("FAILED")
            print(f"{migration.path.name}: {exc}", file=sys.stderr)
            await _reload_schema_cache(conn)
            return 1
        print("ok")
    await _reload_schema_cache(conn)
    print(f"Applied {len(todo)} migration(s).")
    return 0


async def _reload_schema_cache(conn) -> None:
    # PostgREST caches the schema; without this, new tables/columns 404 over the REST API
    # (PGRST204/PGRST205) until it happens to reload.
    await conn.execute("notify pgrst, 'reload schema'")


async def _status(conn, migrations: list[Migration]) -> int:
    applied = await _applied_versions(conn)
    for migration in migrations:
        mark = "applied" if migration.version in applied else "PENDING"
        print(f"{mark:8} {migration.path.name}")
    print(f"{len(pending(migrations, applied))} pending of {len(migrations)}.")
    return 0


async def _baseline(conn, migrations: list[Migration], through_version: str) -> int:
    if through_version not in {m.version for m in migrations}:
        print(f"No migration has version {through_version}.", file=sys.stderr)
        return 1
    if not await conn.fetchval("select to_regclass('public.ingestion_jobs') is not null"):
        print("This database has no schema yet; run without --baseline to apply migrations.", file=sys.stderr)
        return 1
    todo = [m for m in pending(migrations, await _applied_versions(conn)) if m.version <= through_version]
    for migration in todo:
        await _record(conn, migration, [])
        print(f"marked applied {migration.path.name}")
    print(f"Baselined {len(todo)} migration(s).")
    return 0


async def _run(args: list[str]) -> int:
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        print("SUPABASE_DB_URL is not set (see .env.example).", file=sys.stderr)
        return 1
    if is_transaction_pooler(dsn):
        print(
            "SUPABASE_DB_URL points at the transaction pooler (port 6543). "
            "Use the Session pooler string (port 5432) from Connect -> Session pooler.",
            file=sys.stderr,
        )
        return 1

    migrations = discover_migrations()
    conn = await asyncpg.connect(dsn)
    try:
        if args == ["--status"]:
            return await _status(conn, migrations)
        if len(args) == 2 and args[0] == "--baseline":
            return await _baseline(conn, migrations, args[1])
        return await _apply(conn, migrations)
    finally:
        await conn.close()


def main() -> None:
    load_dotenv()
    args = sys.argv[1:]
    if args in (["-h"], ["--help"]):
        print(__doc__)
        return
    if args and args != ["--status"] and not (len(args) == 2 and args[0] == "--baseline"):
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
