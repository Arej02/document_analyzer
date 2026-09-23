"""Multi-tenancy smoke test (read path) — tasks.md Phase 6, first task.

RLS is enforced by Postgres itself, not by anything this repo's unit tests can
fake — every other test in this suite runs against `tests/fakes.py::FakeClient`,
which has no concept of row security at all. This test is the one place that
actually exercises `current_org_id()` and the `*_select_own_org` policies
(supabase/migrations/20260918010000_rls_policies.sql and the two later
migrations that added `filtered_files`/`secret_findings`) against a real
Supabase project, the same way earlier phases' "verified live" notes did by
hand — just automated and self-cleaning instead of one-off.

Requires live credentials (SUPABASE_URL/SUPABASE_ANON_KEY/SUPABASE_SERVICE_ROLE_KEY)
against a project with the migrations under supabase/migrations applied; skips
itself otherwise, same as this repo has no local Supabase stack to run against.
"""

import os
import uuid

import pytest
from dotenv import load_dotenv
from supabase import create_client

from document_analyzer.supabase_client import get_request_scoped_client, get_service_role_client, insert_scoped

# Other test modules happen to trigger this too (app.py/worker/run.py both call
# load_dotenv() at import time), but this file must not depend on import order
# to see real credentials — call it directly so the skip check below is correct
# whether this file runs alone or as part of the full suite.
load_dotenv()

_LIVE_ENV_VARS = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"]

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in _LIVE_ENV_VARS),
    reason="requires a live Supabase project (SUPABASE_URL/SUPABASE_ANON_KEY/SUPABASE_SERVICE_ROLE_KEY)",
)

# Every table RLS-scopes by org_id (design.md §2) except embedding_config, which
# is deliberately global — not a cross-org leak vector, so it's not checked here.
_ORG_SCOPED_TABLES = [
    "projects",
    "ingestion_jobs",
    "extracted_entities",
    "entity_edges",
    "knowledge_summaries",
    "summary_cache",
    "filtered_files",
    "secret_findings",
]


def _create_org_user(service_client, org_id: str) -> tuple[str, str, str]:
    """Creates a disposable auth user + org_members row, returns (user_id, email, password)."""
    email = f"rls-smoke-{uuid.uuid4().hex}@example.com"
    password = f"Sm0ke-{uuid.uuid4().hex}"
    created = service_client.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    )
    user_id = created.user.id
    service_client.table("org_members").insert({"user_id": user_id, "org_id": org_id}).execute()
    return user_id, email, password


def _sign_in(url: str, anon_key: str, email: str, password: str) -> str:
    anon_client = create_client(url, anon_key)
    response = anon_client.auth.sign_in_with_password({"email": email, "password": password})
    return response.session.access_token


def _seed_org_a_chain(service_client, org_id: str) -> dict:
    """Seeds one row per org-scoped table, chained by FK, all under org_id. Returns their ids/keys."""
    project = insert_scoped(
        service_client, "projects", org_id, {"source_type": "upload", "source_ref": "rls-smoke-test"}
    ).data[0]
    job = insert_scoped(
        service_client,
        "ingestion_jobs",
        org_id,
        {
            "project_id": project["id"],
            "source_type": "upload",
            "source_ref": "rls-smoke-test",
            "status": "done",
        },
    ).data[0]
    entity = insert_scoped(
        service_client,
        "extracted_entities",
        org_id,
        {
            "job_id": job["id"],
            "entity_type": "function",
            "name": "smoke_fn",
            "file_path": "smoke.php",
            "raw_facts": {},
            "content_hash": f"smoke-{uuid.uuid4().hex}",
        },
    ).data[0]
    target_entity = insert_scoped(
        service_client,
        "extracted_entities",
        org_id,
        {
            "job_id": job["id"],
            "entity_type": "function",
            "name": "smoke_fn_target",
            "file_path": "smoke.php",
            "raw_facts": {},
            "content_hash": f"smoke-{uuid.uuid4().hex}",
        },
    ).data[0]
    edge = insert_scoped(
        service_client,
        "entity_edges",
        org_id,
        {"job_id": job["id"], "from_entity_id": entity["id"], "to_entity_id": target_entity["id"], "edge_type": "calls"},
    ).data[0]
    summary = insert_scoped(
        service_client,
        "knowledge_summaries",
        org_id,
        {
            "job_id": job["id"],
            "level": "entity",
            "entity_id": entity["id"],
            "summary_text": "smoke test summary",
            "risk_level": "low",
        },
    ).data[0]
    cache_entry = insert_scoped(
        service_client,
        "summary_cache",
        org_id,
        {"content_hash": entity["content_hash"], "summary_id": summary["id"]},
    ).data[0]
    filtered_file = insert_scoped(
        service_client,
        "filtered_files",
        org_id,
        {"job_id": job["id"], "file_path": "vendor/smoke.php", "reason": "composer_vendor_directory"},
    ).data[0]
    secret_finding = insert_scoped(
        service_client,
        "secret_findings",
        org_id,
        {"job_id": job["id"], "entity_id": entity["id"], "field_path": "raw_facts.smoke", "reason": "smoke_test"},
    ).data[0]

    return {
        "projects": project["id"],
        "ingestion_jobs": job["id"],
        "extracted_entities": entity["id"],
        "entity_edges": edge["id"],
        "knowledge_summaries": summary["id"],
        "summary_cache": cache_entry["content_hash"],
        "filtered_files": filtered_file["id"],
        "secret_findings": secret_finding["id"],
    }


def _delete_org(service_client, org_id: str) -> None:
    # Children before parents, respecting the FKs in the create-tables migration.
    for table in [
        "secret_findings",
        "summary_cache",
        "knowledge_summaries",
        "entity_edges",
        "filtered_files",
        "extracted_entities",
        "ingestion_jobs",
        "projects",
        "org_members",
    ]:
        service_client.table(table).delete().eq("org_id", org_id).execute()


@pytest.fixture(scope="module")
def two_orgs():
    service_client = get_service_role_client()
    url = os.environ["SUPABASE_URL"]
    anon_key = os.environ["SUPABASE_ANON_KEY"]

    org_a_id = str(uuid.uuid4())
    org_b_id = str(uuid.uuid4())
    user_a_id, email_a, password_a = _create_org_user(service_client, org_a_id)
    user_b_id, email_b, password_b = _create_org_user(service_client, org_b_id)

    seeded_ids = _seed_org_a_chain(service_client, org_a_id)
    org_b_project_id = insert_scoped(
        service_client, "projects", org_b_id, {"source_type": "upload", "source_ref": "rls-smoke-test-org-b"}
    ).data[0]["id"]

    token_a = _sign_in(url, anon_key, email_a, password_a)
    token_b = _sign_in(url, anon_key, email_b, password_b)
    client_a = get_request_scoped_client(token_a)
    client_b = get_request_scoped_client(token_b)

    try:
        yield {
            "org_a_id": org_a_id,
            "org_b_id": org_b_id,
            "user_a_id": user_a_id,
            "user_b_id": user_b_id,
            "client_a": client_a,
            "client_b": client_b,
            "seeded_ids": seeded_ids,
            "org_b_project_id": org_b_project_id,
        }
    finally:
        _delete_org(service_client, org_a_id)
        _delete_org(service_client, org_b_id)
        service_client.auth.admin.delete_user(user_a_id)
        service_client.auth.admin.delete_user(user_b_id)


_ROW_ID_COLUMN = {"summary_cache": "content_hash"}


@pytest.mark.parametrize("table", _ORG_SCOPED_TABLES)
def test_other_org_cannot_read_a_seeded_row(two_orgs, table):
    row_id = two_orgs["seeded_ids"][table]
    id_column = _ROW_ID_COLUMN.get(table, "id")

    result = two_orgs["client_b"].table(table).select("*").eq(id_column, row_id).execute()

    assert result.data == []


@pytest.mark.parametrize("table", _ORG_SCOPED_TABLES)
def test_owning_org_can_read_its_own_seeded_row(two_orgs, table):
    """Positive control — proves an empty result above means RLS blocked it, not that the query itself is broken."""
    row_id = two_orgs["seeded_ids"][table]
    id_column = _ROW_ID_COLUMN.get(table, "id")

    result = two_orgs["client_a"].table(table).select("*").eq(id_column, row_id).execute()

    assert len(result.data) == 1


def test_leakage_check_holds_in_the_other_direction_too(two_orgs):
    """Confirms the block isn't one-directional — org A can't read org B's data either."""
    blocked = two_orgs["client_a"].table("projects").select("*").eq("id", two_orgs["org_b_project_id"]).execute()
    allowed = two_orgs["client_b"].table("projects").select("*").eq("id", two_orgs["org_b_project_id"]).execute()

    assert blocked.data == []
    assert len(allowed.data) == 1


def test_org_members_rows_are_not_cross_readable(two_orgs):
    """org_members underlies current_org_id() for every other policy — a leak here breaks everything downstream."""
    result = two_orgs["client_a"].table("org_members").select("*").eq("user_id", two_orgs["user_b_id"]).execute()

    assert result.data == []
