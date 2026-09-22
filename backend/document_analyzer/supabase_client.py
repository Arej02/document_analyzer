import os

from supabase import Client, ClientOptions, create_client


def get_service_role_client() -> Client:
    """Async worker connection. Bypasses RLS by construction — never use from a request handler."""
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key)


def get_request_scoped_client(access_token: str) -> Client:
    """FastAPI request-handler connection. RLS-enforced via the caller's own Supabase JWT.

    The JWT must be passed via ClientOptions at construction time, not through
    `client.postgrest.auth()` afterward — that call mutates a headers dict that
    has already been copied into the underlying httpx session, so it's silently
    ignored on outgoing requests.
    """
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_ANON_KEY"]
    options = ClientOptions(headers={"Authorization": f"Bearer {access_token}"})
    return create_client(url, key, options=options)


def insert_scoped(client: Client, table: str, org_id: str, row: dict):
    """Insert via the service-role client, stamping org_id from the job row — never from ingested content."""
    if "org_id" in row:
        raise ValueError(f"row for {table!r} must not already contain org_id; pass it via the org_id argument")
    return client.table(table).insert({**row, "org_id": org_id}).execute()


def insert_many_scoped(client: Client, table: str, org_id: str, rows: list[dict]):
    """Batch insert via the service-role client, stamping org_id on every row from the job row.

    A single multi-row INSERT (not N round-trips) — needed for extraction output,
    which can run into the thousands of rows per job (tasks.md Phase 6 load test).
    Relies on Postgres preserving input row order in a single INSERT ... RETURNING,
    so callers can zip `rows` against `.data` to recover each row's new id.
    """
    for row in rows:
        if "org_id" in row:
            raise ValueError(f"row for {table!r} must not already contain org_id; pass it via the org_id argument")
    return client.table(table).insert([{**row, "org_id": org_id} for row in rows]).execute()
