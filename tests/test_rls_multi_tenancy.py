"""Multi-tenancy smoke test (read path) — tasks.md Phase 6, first task.

RLS is enforced by Postgres itself, not by anything this repo's unit tests can
fake — every other test in this suite runs against `tests/fakes.py::FakeClient`,
which has no concept of row security at all. This test is the one place that
actually exercises `current_org_id()` and the `*_select_own_org` policies
(supabase/migrations/20260918010000_rls_policies.sql, the two later
migrations that added `filtered_files`/`secret_findings`, and Agent 2's
`20260923000000_agent2_planning_tables.sql`/`20260923010000_agent2_plan_tables.sql`
— tasks_agent2.md Phase 0, and Agent 3's `20260925100000_agent3_execution_tables.sql`
plus the `agent3-output` bucket policy in `20260925120000_agent3_output_bucket.sql`
— tasks_agent3.md Phase 0 — and the Assessment Report's `report_generation_jobs`,
`assessment_reports`, `report_share_links`, `report_internal_estimates` and `reports`
bucket in `20261001040000_report_tables.sql` — tasks_report.md Phase 1) against a real
Supabase project, the same way earlier phases' "verified live" notes did by
hand — just automated and self-cleaning instead of one-off.

Requires live credentials (SUPABASE_URL/SUPABASE_ANON_KEY/SUPABASE_SERVICE_ROLE_KEY)
against a project with the migrations under supabase/migrations applied; skips
itself otherwise, same as this repo has no local Supabase stack to run against.
"""

import os
import uuid
import warnings

import pytest
from dotenv import load_dotenv
from supabase import create_client

from document_analyzer.supabase_client import get_request_scoped_client, get_service_role_client, insert_scoped

# Other test modules happen to trigger this too (app.py/worker/run.py both call
# load_dotenv() at import time), but this file must not depend on import order
# to see real credentials — call it directly so the skip check below is correct
# whether this file runs alone or as part of the full suite.
load_dotenv()

AGENT3_BUCKET = "agent3-output"
AGENT3_OBJECT_BODY = b"rls smoke agent3 log"
REPORTS_BUCKET = "reports"
REPORTS_OBJECT_BODY = b"<html>rls smoke report</html>"

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
    # Agent 2 (design_agent2.md §2).
    "planning_jobs",
    "clusters",
    "cluster_members",
    "cluster_reports",
    "cluster_edges",
    "migration_plans",
    "migration_plan_items",
    "plan_dependency_targets",
    "phase_approvals",
    "plan_overrides",
    # Agent 3 (design_agent3.md §2).
    "execution_jobs",
    "unit_executions",
    # Assessment Report (design_report.md §3). report_internal_estimates is deliberately
    # absent: not even its own org may read it (see the tests below).
    "report_generation_jobs",
    "assessment_reports",
    "report_share_links",
    # Live workflow run view (design.md §2) — shared by all three agents.
    "workflow_events",
]


# Tables whose tenant read access is column-limited: `select *` is a permission error by
# design (report_share_links hides token_hash), so these are read with explicit columns.
_SELECT_COLUMNS = {
    "report_share_links": "id, org_id, report_id, expires_at, access_count",
}


def _create_org_user(service_client, org_id: str, created_user_ids: list[str]) -> tuple[str, str, str]:
    """Creates a disposable auth user + org_members row, returns (user_id, email, password).
    The user id goes into `created_user_ids` as soon as the user exists, so the
    fixture's cleanup removes it even if the org_members insert (or anything later) fails."""
    email = f"rls-smoke-{uuid.uuid4().hex}@example.com"
    password = f"Sm0ke-{uuid.uuid4().hex}"
    created = service_client.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    )
    user_id = created.user.id
    created_user_ids.append(user_id)
    service_client.table("org_members").insert({"user_id": user_id, "org_id": org_id}).execute()
    return user_id, email, password


def _sign_in(url: str, anon_key: str, email: str, password: str) -> str:
    anon_client = create_client(url, anon_key)
    response = anon_client.auth.sign_in_with_password({"email": email, "password": password})
    return response.session.access_token


def _seed_org_a_chain(service_client, org_id: str, user_id: str) -> dict:
    """Seeds one row per org-scoped table, chained by FK, all under org_id. Returns their ids/keys.
    `user_id` is org A's own user, for the Agent 2 review tables' reviewer/creator columns."""
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
    workflow_event = insert_scoped(
        service_client,
        "workflow_events",
        org_id,
        {"workflow_kind": "agent1_ingestion", "workflow_id": job["id"], "event_type": "activity", "message": "smoke test event"},
    ).data[0]

    agent2 = _seed_agent2_chain(service_client, org_id, user_id, project["id"], job["id"], entity["id"])
    agent3 = _seed_agent3_chain(
        service_client, org_id, project["id"], job["id"], agent2["migration_plans"], agent2["clusters"], agent2["_phase_cluster_id"]
    )

    report = _seed_report_chain(service_client, org_id, user_id, project["id"], job["id"])

    return {
        **agent2,
        **agent3,
        **report,
        "projects": project["id"],
        "ingestion_jobs": job["id"],
        "extracted_entities": entity["id"],
        "entity_edges": edge["id"],
        "knowledge_summaries": summary["id"],
        "summary_cache": cache_entry["content_hash"],
        "filtered_files": filtered_file["id"],
        "secret_findings": secret_finding["id"],
        "workflow_events": workflow_event["id"],
    }


def _seed_agent2_chain(service_client, org_id: str, user_id: str, project_id: str, job_id: str, entity_id: str) -> dict:
    """One row per Agent 2 table under org_id, FK-chained onto the Agent 1 rows above."""
    planning_job = insert_scoped(
        service_client,
        "planning_jobs",
        org_id,
        {"project_id": project_id, "source_job_id": job_id, "status": "done", "params": {"seed": 42}},
    ).data[0]
    phase = insert_scoped(
        service_client,
        "clusters",
        org_id,
        {"planning_job_id": planning_job["id"], "level": "phase", "kind": "community", "size": 1},
    ).data[0]
    unit = insert_scoped(
        service_client,
        "clusters",
        org_id,
        {
            "planning_job_id": planning_job["id"],
            "level": "unit",
            "parent_cluster_id": phase["id"],
            "kind": "community",
            "size": 1,
        },
    ).data[0]
    member = insert_scoped(
        service_client,
        "cluster_members",
        org_id,
        {"cluster_id": unit["id"], "entity_id": entity_id, "canonical_entity_id": entity_id},
    ).data[0]
    report = insert_scoped(
        service_client,
        "cluster_reports",
        org_id,
        {"cluster_id": unit["id"], "summary_text": "smoke test cluster report", "risk_level": "low"},
    ).data[0]
    cluster_edge = insert_scoped(
        service_client,
        "cluster_edges",
        org_id,
        {
            "planning_job_id": planning_job["id"],
            "level": "phase",
            "from_cluster_id": phase["id"],
            "to_cluster_id": phase["id"],
            "weight": 1.0,
        },
    ).data[0]
    plan = insert_scoped(
        service_client,
        "migration_plans",
        org_id,
        {"project_id": project_id, "planning_job_id": planning_job["id"], "source_job_id": job_id, "version": 1},
    ).data[0]
    plan_item = insert_scoped(
        service_client,
        "migration_plan_items",
        org_id,
        {"plan_id": plan["id"], "cluster_id": unit["id"], "phase_cluster_id": phase["id"], "phase_order": 0, "unit_order": 0},
    ).data[0]
    dependency_target = insert_scoped(
        service_client,
        "plan_dependency_targets",
        org_id,
        {"plan_id": plan["id"], "entity_id": entity_id, "target_name": "smoke-test-target"},
    ).data[0]
    approval = insert_scoped(
        service_client,
        "phase_approvals",
        org_id,
        {"plan_id": plan["id"], "phase_cluster_id": phase["id"], "status": "approved", "reviewer_id": user_id},
    ).data[0]
    override = insert_scoped(
        service_client,
        "plan_overrides",
        org_id,
        {"plan_id": plan["id"], "kind": "facade_ack", "payload": {"scc_id": 0}, "created_by": user_id},
    ).data[0]

    return {
        "planning_jobs": planning_job["id"],
        "clusters": unit["id"],
        "cluster_members": member["cluster_id"],
        "cluster_reports": report["cluster_id"],
        "cluster_edges": cluster_edge["id"],
        "migration_plans": plan["id"],
        "migration_plan_items": plan_item["plan_id"],
        "plan_dependency_targets": dependency_target["plan_id"],
        "phase_approvals": approval["id"],
        "plan_overrides": override["id"],
        # Not a table key: Agent 3's unit_executions row needs the phase cluster too.
        "_phase_cluster_id": phase["id"],
    }


def _seed_agent3_chain(
    service_client, org_id: str, project_id: str, job_id: str, plan_id: str, unit_cluster_id: str, phase_cluster_id: str
) -> dict:
    """One row per Agent 3 table under org_id, FK-chained onto the Agent 1/2 rows above."""
    execution_job = insert_scoped(
        service_client,
        "execution_jobs",
        org_id,
        {"project_id": project_id, "plan_id": plan_id, "source_job_id": job_id, "status": "done", "params": {"max_attempts": 3}},
    ).data[0]
    unit_execution = insert_scoped(
        service_client,
        "unit_executions",
        org_id,
        {
            "execution_job_id": execution_job["id"],
            "cluster_id": unit_cluster_id,
            "phase_cluster_id": phase_cluster_id,
            "phase_order": 0,
            "unit_order": 0,
            "status": "passed",
        },
    ).data[0]

    return {"execution_jobs": execution_job["id"], "unit_executions": unit_execution["id"]}


def _seed_report_chain(service_client, org_id: str, user_id: str, project_id: str, job_id: str) -> dict:
    """One row per Assessment Report table under org_id (an X-ray, so no plan), FK-chained
    onto the Agent 1 rows above."""
    report_job = insert_scoped(
        service_client,
        "report_generation_jobs",
        org_id,
        {
            "project_id": project_id,
            "tier": "xray",
            "source_job_id": job_id,
            "status": "done",
            "params": {"pdf_timeout_seconds": 120},
            "created_by": user_id,
        },
    ).data[0]
    report = insert_scoped(
        service_client,
        "assessment_reports",
        org_id,
        {
            "project_id": project_id,
            "generation_job_id": report_job["id"],
            "tier": "xray",
            "source_job_id": job_id,
            "report_json": {"schema_version": 1},
            "schema_version": 1,
            "template_version": "smoke",
            "estimator_version": "smoke",
            "exec_summary_status": "verified",
            "created_by": user_id,
        },
    ).data[0]
    share_link = insert_scoped(
        service_client,
        "report_share_links",
        org_id,
        {
            "report_id": report["id"],
            "token_hash": uuid.uuid4().hex + uuid.uuid4().hex,  # 64 lowercase hex chars
            "created_by": user_id,
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    ).data[0]
    insert_scoped(
        service_client,
        "report_internal_estimates",
        org_id,
        {"report_id": report["id"], "ai_cost_p50": 1.5, "ai_cost_p90": 2.5, "n": 3},
    )

    return {
        "report_generation_jobs": report_job["id"],
        "assessment_reports": report["id"],
        "report_share_links": share_link["id"],
        "report_internal_estimates": report["id"],
    }


def _delete_org(service_client, org_id: str) -> list[str]:
    """Deletes every row seeded under `org_id`. Best-effort: each table is tried even if
    an earlier delete failed (a half-seeded org must still be cleaned up), and the
    failures are returned rather than raised."""
    # Children before parents, respecting the FKs in the create-tables migrations.
    # Agent 3's rows reference the plan and clusters, so they go first.
    # The Agent 2 plan tables don't cascade (plans are immutable snapshots), so
    # they go before clusters/planning_jobs; the cluster tables would cascade from
    # planning_jobs but are listed explicitly anyway.
    failures: list[str] = []
    for table in [
        # The report's rows reference the project and Agent 1 job; its estimates and
        # links cascade from the report but are listed explicitly anyway.
        "report_internal_estimates",
        "report_share_links",
        "assessment_reports",
        "report_generation_jobs",
        "unit_executions",
        "execution_jobs",
        "plan_overrides",
        "phase_approvals",
        "plan_dependency_targets",
        "migration_plan_items",
        "migration_plans",
        "cluster_edges",
        "cluster_reports",
        "cluster_members",
        "clusters",
        "planning_jobs",
        "workflow_events",
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
        try:
            service_client.table(table).delete().eq("org_id", org_id).execute()
        except Exception as exc:  # noqa: BLE001 — keep cleaning the remaining tables
            failures.append(f"{table}: {exc}")
    return failures


@pytest.fixture(scope="module")
def two_orgs():
    """Two throwaway orgs with one user each, org A's rows seeded. Everything is created
    inside the `try`, and each thing is recorded as soon as it exists, so a failure at
    any point of the setup still cleans up what was made. It runs against a live,
    shared project, so leftovers would pile up there otherwise."""
    service_client = get_service_role_client()
    url = os.environ["SUPABASE_URL"]
    anon_key = os.environ["SUPABASE_ANON_KEY"]

    org_a_id = str(uuid.uuid4())
    org_b_id = str(uuid.uuid4())
    created_user_ids: list[str] = []
    object_paths = [f"{org_a_id}/forged.txt", f"{org_b_id}/forged.txt"]
    report_object_paths: list[str] = []

    try:
        user_a_id, email_a, password_a = _create_org_user(service_client, org_a_id, created_user_ids)
        user_b_id, email_b, password_b = _create_org_user(service_client, org_b_id, created_user_ids)

        seeded_ids = _seed_org_a_chain(service_client, org_a_id, user_a_id)
        org_b_project_id = insert_scoped(
            service_client, "projects", org_b_id, {"source_type": "upload", "source_ref": "rls-smoke-test-org-b"}
        ).data[0]["id"]
        # One agent3-output object under org A's folder, written the way the executor writes (service role).
        agent3_object_path = f"{org_a_id}/{seeded_ids['execution_jobs']}/units/smoke/attempt_1/log.txt"
        object_paths.append(agent3_object_path)
        service_client.storage.from_(AGENT3_BUCKET).upload(
            agent3_object_path, AGENT3_OBJECT_BODY, {"content-type": "text/plain"}
        )

        # One reports-bucket object under org A's folder, written the way the reporter writes (service role).
        report_object_path = f"{org_a_id}/{seeded_ids['assessment_reports']}/report.html"
        report_object_paths.append(report_object_path)
        service_client.storage.from_(REPORTS_BUCKET).upload(
            report_object_path, REPORTS_OBJECT_BODY, {"content-type": "text/html"}
        )

        token_a = _sign_in(url, anon_key, email_a, password_a)
        token_b = _sign_in(url, anon_key, email_b, password_b)

        yield {
            "org_a_id": org_a_id,
            "org_b_id": org_b_id,
            "user_a_id": user_a_id,
            "user_b_id": user_b_id,
            "client_a": get_request_scoped_client(token_a),
            "client_b": get_request_scoped_client(token_b),
            "seeded_ids": seeded_ids,
            "org_b_project_id": org_b_project_id,
            "agent3_object_path": agent3_object_path,
            "report_object_path": report_object_path,
        }
    finally:
        failures: list[str] = []
        try:
            service_client.storage.from_(AGENT3_BUCKET).remove(object_paths)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"storage: {exc}")
        try:
            service_client.storage.from_(REPORTS_BUCKET).remove(
                report_object_paths + [f"{org_a_id}/forged.txt", f"{org_b_id}/forged.txt"]
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"reports storage: {exc}")
        failures += _delete_org(service_client, org_a_id)
        failures += _delete_org(service_client, org_b_id)
        for user_id in created_user_ids:
            try:
                service_client.auth.admin.delete_user(user_id)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"auth user {user_id}: {exc}")
        if failures:
            # A warning, not a raise: raising here would hide the setup error that got us here.
            warnings.warn("RLS smoke cleanup left data behind: " + "; ".join(failures), stacklevel=1)


_ROW_ID_COLUMN = {
    "summary_cache": "content_hash",
    "cluster_members": "cluster_id",
    "cluster_reports": "cluster_id",
    "migration_plan_items": "plan_id",
    "plan_dependency_targets": "plan_id",
}


@pytest.mark.parametrize("table", _ORG_SCOPED_TABLES)
def test_other_org_cannot_read_a_seeded_row(two_orgs, table):
    row_id = two_orgs["seeded_ids"][table]
    id_column = _ROW_ID_COLUMN.get(table, "id")

    columns = _SELECT_COLUMNS.get(table, "*")
    result = two_orgs["client_b"].table(table).select(columns).eq(id_column, row_id).execute()

    assert result.data == []


@pytest.mark.parametrize("table", _ORG_SCOPED_TABLES)
def test_owning_org_can_read_its_own_seeded_row(two_orgs, table):
    """Positive control — proves an empty result above means RLS blocked it, not that the query itself is broken."""
    row_id = two_orgs["seeded_ids"][table]
    id_column = _ROW_ID_COLUMN.get(table, "id")

    columns = _SELECT_COLUMNS.get(table, "*")
    result = two_orgs["client_a"].table(table).select(columns).eq(id_column, row_id).execute()

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


# --- Agent 3: writes are service-role only, and the agent3-output bucket -----


@pytest.mark.parametrize("table", ["execution_jobs", "unit_executions"])
def test_authenticated_users_cannot_insert_agent3_rows(two_orgs, table):
    """No insert policy on either table (design_agent3.md §2): only the worker and the
    API's service-role client write them, even into the user's own org."""
    with pytest.raises(Exception, match="row-level security"):
        two_orgs["client_a"].table(table).insert({"org_id": two_orgs["org_a_id"]}).execute()


@pytest.mark.parametrize("who", ["a", "b"])
def test_authenticated_users_cannot_write_plan_dependency_targets(two_orgs, who):
    """Written only by the planner worker (design_agent2.md §2): no insert, update or delete
    for tenants, even in their own org."""
    org_id = two_orgs[f"org_{who}_id"]
    client = two_orgs[f"client_{who}"]
    plan_id = two_orgs["seeded_ids"]["plan_dependency_targets"]

    with pytest.raises(Exception, match="row-level security|permission denied"):
        client.table("plan_dependency_targets").insert({"org_id": org_id, "target_name": "x"}).execute()
    # An update/delete either errors (privilege revoked) or matches no row; the seeded row must survive either way.
    for attempt in (
        lambda: client.table("plan_dependency_targets").update({"target_name": "tampered"}).eq("plan_id", plan_id).execute(),
        lambda: client.table("plan_dependency_targets").delete().eq("plan_id", plan_id).execute(),
    ):
        try:
            attempt()
        except Exception:
            pass
    row = get_service_role_client().table("plan_dependency_targets").select("target_name").eq("plan_id", plan_id).execute().data
    assert [r["target_name"] for r in row] == ["smoke-test-target"]


def test_owning_org_can_read_its_agent3_output_object(two_orgs):
    """Positive control for the bucket checks below."""
    data = two_orgs["client_a"].storage.from_(AGENT3_BUCKET).download(two_orgs["agent3_object_path"])

    assert data == AGENT3_OBJECT_BODY


def test_other_org_cannot_read_or_list_agent3_output(two_orgs):
    path = two_orgs["agent3_object_path"]
    bucket = two_orgs["client_b"].storage.from_(AGENT3_BUCKET)

    with pytest.raises(Exception, match="not found|not_found"):
        bucket.download(path)
    assert bucket.list(path.rsplit("/", 1)[0]) == []


@pytest.mark.parametrize("who", ["a", "b"])
def test_authenticated_users_cannot_upload_to_agent3_output(two_orgs, who):
    """No insert policy on the bucket, even under the user's own org folder."""
    org_id = two_orgs[f"org_{who}_id"]

    with pytest.raises(Exception, match="row-level security|Unauthorized|403"):
        two_orgs[f"client_{who}"].storage.from_(AGENT3_BUCKET).upload(f"{org_id}/forged.txt", b"forged")


# --- Assessment Report: tenants can't write, and the internal tables stay internal ----

_REPORT_TENANT_TABLES = ["report_generation_jobs", "assessment_reports", "report_share_links"]


@pytest.mark.parametrize("table", _REPORT_TENANT_TABLES)
def test_authenticated_users_cannot_insert_report_rows(two_orgs, table):
    """No insert policy (design_report.md §3): the API and worker write through the
    service role, even into the user's own org."""
    with pytest.raises(Exception, match="row-level security|permission denied"):
        two_orgs["client_a"].table(table).insert({"org_id": two_orgs["org_a_id"]}).execute()


@pytest.mark.parametrize("table", _REPORT_TENANT_TABLES)
def test_authenticated_users_cannot_update_or_delete_report_rows(two_orgs, table):
    """No update or delete policy: PostgREST turns that into zero affected rows, and the
    row must still be there (read back with the service role) afterwards."""
    row_id = two_orgs["seeded_ids"][table]
    service = get_service_role_client()
    before = service.table(table).select("*").eq("id", row_id).execute().data

    try:
        two_orgs["client_a"].table(table).update({"org_id": two_orgs["org_b_id"]}).eq("id", row_id).execute()
    except Exception:  # noqa: BLE001 — a permission error is also a refusal
        pass
    try:
        two_orgs["client_a"].table(table).delete().eq("id", row_id).execute()
    except Exception:  # noqa: BLE001
        pass

    assert service.table(table).select("*").eq("id", row_id).execute().data == before


def test_share_link_token_hash_is_unreadable_even_to_the_owning_org(two_orgs):
    """token_hash is excluded from the column grant, so naming it (or `select *`) fails."""
    link_id = two_orgs["seeded_ids"]["report_share_links"]

    with pytest.raises(Exception, match="permission denied"):
        two_orgs["client_a"].table("report_share_links").select("token_hash").eq("id", link_id).execute()
    with pytest.raises(Exception, match="permission denied"):
        two_orgs["client_a"].table("report_share_links").select("*").eq("id", link_id).execute()


@pytest.mark.parametrize("who", ["a", "b"])
def test_report_internal_estimates_are_unreadable_even_to_the_owning_org(two_orgs, who):
    """RLS on with no tenant policy and every privilege revoked: nobody but the service
    role reads it, org A included (it owns the seeded row)."""
    report_id = two_orgs["seeded_ids"]["report_internal_estimates"]

    try:
        result = two_orgs[f"client_{who}"].table("report_internal_estimates").select("*").eq("report_id", report_id).execute()
    except Exception as exc:  # noqa: BLE001
        assert "permission denied" in str(exc)
    else:
        assert result.data == []


def test_authenticated_users_cannot_write_report_internal_estimates(two_orgs):
    with pytest.raises(Exception, match="row-level security|permission denied"):
        two_orgs["client_a"].table("report_internal_estimates").insert(
            {"org_id": two_orgs["org_a_id"], "report_id": two_orgs["seeded_ids"]["assessment_reports"], "n": 0}
        ).execute()


def test_owning_org_can_read_its_report_object(two_orgs):
    """Positive control for the bucket checks below."""
    data = two_orgs["client_a"].storage.from_(REPORTS_BUCKET).download(two_orgs["report_object_path"])

    assert data == REPORTS_OBJECT_BODY


def test_other_org_cannot_read_or_list_report_objects(two_orgs):
    path = two_orgs["report_object_path"]
    bucket = two_orgs["client_b"].storage.from_(REPORTS_BUCKET)

    with pytest.raises(Exception, match="not found|not_found"):
        bucket.download(path)
    assert bucket.list(path.rsplit("/", 1)[0]) == []


@pytest.mark.parametrize("who", ["a", "b"])
def test_authenticated_users_cannot_upload_to_the_reports_bucket(two_orgs, who):
    """No insert policy on the bucket, even under the user's own org folder."""
    org_id = two_orgs[f"org_{who}_id"]

    with pytest.raises(Exception, match="row-level security|Unauthorized|403"):
        two_orgs[f"client_{who}"].storage.from_(REPORTS_BUCKET).upload(f"{org_id}/forged.txt", b"forged")
