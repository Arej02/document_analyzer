"""Multi-tenancy smoke test (write path) for the executor — tasks_agent3.md
Phase 6, first task. Agent 3's counterpart of
`tests/test_worker_write_path_multi_tenancy.py` (Agent 1).

The read-path test (test_rls_multi_tenancy.py) proves Postgres RLS blocks a
cross-org *read*. RLS never applies to the executor worker's own connection
though (get_service_role_client bypasses it by construction, per CLAUDE.md) —
the write path's only protection against a cross-org write is the worker's
own code discipline:

1. Every `org_id` the worker writes with comes from the trusted
   `execution_jobs` row (`agent3/worker/run.py::_fetch_execution_job`, itself
   filtered by `.eq("org_id", org_id)` off the queue message), never from
   repo content or LLM output. Generated file paths/content, symbol maps and
   sandbox log/junit text are all attacker- or model-influenced data, but
   `units.py::_upload_attempt`/`_attempt_prefix`/`_unit_storage_prefix` only
   ever take `org_id` as a plain parameter — there's no path from "a generated
   file happens to contain another org's uuid as a string" to that string
   becoming part of the actual storage prefix or an `org_id` column.
2. Every `status.py` write that updates an existing row by key filters on
   both that key (`id`, or the `(execution_job_id, cluster_id)` unique pair)
   *and* `org_id`. If some future bug ever desynced the `org_id` argument
   passed to one of these functions from the job's real org (the realistic
   shape of "tricked into writing under the wrong org", since every one of
   them takes org_id as a parameter rather than looking it up itself), the
   filter fails safe: zero rows match, nothing is written.

Both are checked against a real Postgres connection and the real
`agent3-output` bucket (tests/fakes.py::FakeClient has no real row-security or
storage-path semantics to check, same reasoning as Agent 1's file).

Requires live credentials (SUPABASE_URL/SUPABASE_ANON_KEY/SUPABASE_SERVICE_ROLE_KEY)
against a project with supabase/migrations applied; skips itself otherwise. No
auth users needed (unlike the read-path test) — org_id is a bare column with
no `orgs` table backing it, and this test only exercises the service-role
connection, which never goes through RLS/JWTs at all.
"""

import os
import uuid

import pytest
from dotenv import load_dotenv

# Call directly (same fix as test_worker_write_path_multi_tenancy.py /
# test_rls_multi_tenancy.py) rather than relying on another test module's
# import order to have populated os.environ first.
load_dotenv()

from document_analyzer.agent3.worker.generate import GeneratedFile  # noqa: E402
from document_analyzer.agent3.worker.sandbox import SandboxResult  # noqa: E402
from document_analyzer.agent3.worker.units import OUTPUT_BUCKET, _attempt_prefix, _unit_storage_prefix, _upload_attempt  # noqa: E402
from document_analyzer.agent3.worker.status import (  # noqa: E402
    mark_execution_done,
    mark_execution_failed,
    mark_execution_running,
    mark_execution_stage,
    mark_unit_running,
    set_execution_output,
    update_unit_execution,
)
from document_analyzer.supabase_client import get_service_role_client, insert_scoped  # noqa: E402

_LIVE_ENV_VARS = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"]

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in _LIVE_ENV_VARS),
    reason="requires a live Supabase project (SUPABASE_URL/SUPABASE_ANON_KEY/SUPABASE_SERVICE_ROLE_KEY)",
)


def _seed_execution(service_client, org_id: str) -> dict:
    """One execution_job + one unit_execution under org_id, FK-chained through a
    minimal Agent 1/2 lineage (same shape as test_rls_multi_tenancy.py's
    _seed_agent3_chain, built locally rather than imported — that one lives with
    the read-path RLS test, a different module's concern)."""
    project = insert_scoped(service_client, "projects", org_id, {"source_type": "github", "source_ref": "https://x/y"}).data[0]
    job = insert_scoped(
        service_client,
        "ingestion_jobs",
        org_id,
        {
            "project_id": project["id"],
            "source_type": "github",
            "source_ref": "https://x/y",
            "status": "done",
            "source_snapshot_path": f"{org_id}/job/source.tar.gz",
        },
    ).data[0]
    planning_job = insert_scoped(
        service_client, "planning_jobs", org_id, {"project_id": project["id"], "source_job_id": job["id"], "status": "done", "params": {"seed": 42}}
    ).data[0]
    plan = insert_scoped(
        service_client,
        "migration_plans",
        org_id,
        {"project_id": project["id"], "source_job_id": job["id"], "planning_job_id": planning_job["id"], "status": "approved", "version": 1},
    ).data[0]
    phase = insert_scoped(
        service_client, "clusters", org_id, {"planning_job_id": planning_job["id"], "level": "phase", "kind": "community", "size": 1}
    ).data[0]
    unit = insert_scoped(
        service_client,
        "clusters",
        org_id,
        {"planning_job_id": planning_job["id"], "level": "unit", "parent_cluster_id": phase["id"], "kind": "community", "size": 1},
    ).data[0]
    execution_job = insert_scoped(
        service_client,
        "execution_jobs",
        org_id,
        {"project_id": project["id"], "plan_id": plan["id"], "source_job_id": job["id"], "status": "queued", "params": {"max_attempts": 3}},
    ).data[0]
    unit_execution = insert_scoped(
        service_client,
        "unit_executions",
        org_id,
        {
            "execution_job_id": execution_job["id"],
            "cluster_id": unit["id"],
            "phase_cluster_id": phase["id"],
            "phase_order": 0,
            "unit_order": 0,
            "status": "pending",
        },
    ).data[0]
    return {
        "project_id": project["id"],
        "job_id": job["id"],
        "planning_job_id": planning_job["id"],
        "plan_id": plan["id"],
        "phase_cluster_id": phase["id"],
        "unit_cluster_id": unit["id"],
        "execution_job_id": execution_job["id"],
        "unit_execution_id": unit_execution["id"],
    }


def _delete_org(service_client, org_id: str) -> None:
    for table in ["unit_executions", "execution_jobs", "migration_plans", "clusters", "planning_jobs", "ingestion_jobs", "projects"]:
        try:
            service_client.table(table).delete().eq("org_id", org_id).execute()
        except Exception:
            pass


@pytest.fixture
def two_orgs():
    service_client = get_service_role_client()
    org_a_id = str(uuid.uuid4())
    org_b_id = str(uuid.uuid4())  # never seeded with real rows — used only as attacker-controlled payload data
    seed = _seed_execution(service_client, org_a_id)
    try:
        yield {"service_client": service_client, "org_a_id": org_a_id, "org_b_id": org_b_id, **seed}
    finally:
        bucket = service_client.storage.from_(OUTPUT_BUCKET)
        for prefix in (org_a_id, org_b_id):
            try:
                entries = bucket.list(prefix)
            except Exception:
                entries = []
            if entries:
                _remove_recursively(bucket, prefix)
        _delete_org(service_client, org_a_id)
        _delete_org(service_client, org_b_id)


def _remove_recursively(bucket, prefix: str) -> None:
    entries = bucket.list(prefix)
    for entry in entries:
        path = f"{prefix}/{entry['name']}"
        if entry.get("id") is None:
            _remove_recursively(bucket, path)
        else:
            bucket.remove([path])


def _list_objects(bucket, prefix: str) -> list[str]:
    """Every object path under prefix, recursively (same pattern as
    agent3/router.py::_list_objects, copied locally rather than imported since
    it's a router-module helper, not a shared pure function)."""
    entries = bucket.list(prefix)
    paths: list[str] = []
    for entry in entries:
        path = f"{prefix}/{entry['name']}"
        if entry.get("id") is None:
            paths.extend(_list_objects(bucket, path))
        else:
            paths.append(path)
    return paths


def test_crafted_generated_content_cannot_smuggle_a_different_org_id_into_storage(two_orgs):
    service_client = two_orgs["service_client"]
    org_a_id, org_b_id = two_orgs["org_a_id"], two_orgs["org_b_id"]
    execution_job_id, cluster_id = two_orgs["execution_job_id"], two_orgs["unit_cluster_id"]

    # Every value here is attacker/model-controlled: this is exactly what a
    # hostile repo file or a manipulated LLM response could contain, including
    # org_b_id itself as a plain string trying to look like it belongs
    # somewhere sensitive (a path, a module name, a log line).
    code_files = [GeneratedFile(path="app/units/x/__init__.py", content=f"# planted: org_id={org_b_id}\n")]
    test_files = [GeneratedFile(path="tests/units/x/test_contract.py", content=f"# planted: {org_b_id}\n")]
    sandbox_result = SandboxResult(
        status="completed",
        exit_code=0,
        tests=[],
        output_tail=f"log line mentioning org {org_b_id}",
        junit_xml=f"<testsuite name='{org_b_id}'></testsuite>",
        duration_s=1.0,
    )

    _upload_attempt(service_client, org_a_id, execution_job_id, cluster_id, 1, code_files, test_files, sandbox_result)

    bucket = service_client.storage.from_(OUTPUT_BUCKET)
    attempt_prefix = _attempt_prefix(org_a_id, execution_job_id, cluster_id, 1)
    assert attempt_prefix.startswith(f"{org_a_id}/")

    uploaded = _list_objects(bucket, attempt_prefix.rstrip("/"))
    assert len(uploaded) == 4  # code file, test file, log.txt, junit.xml
    for path in uploaded:
        assert path.startswith(f"{org_a_id}/")

    # The crafted content is persisted verbatim (it's just file bytes)...
    code_bytes = bucket.download(f"{attempt_prefix}code/app/units/x/__init__.py")
    assert org_b_id.encode() in code_bytes
    # ...but never redirects any object under org B's own prefix.
    assert bucket.list(org_b_id) == []

    # A symbol_map/test_summary write with crafted content follows the same rule:
    # the org_id column is set from the trusted parameter, never from the payload.
    update_unit_execution(
        service_client,
        execution_job_id,
        org_a_id,
        cluster_id,
        symbol_map={"e1": {"python_module": "app.units.x", "python_name": org_b_id, "kind": "function"}},
        storage_prefix=_unit_storage_prefix(org_a_id, execution_job_id, cluster_id),
    )
    row = (
        service_client.table("unit_executions")
        .select("*")
        .eq("execution_job_id", execution_job_id)
        .eq("cluster_id", cluster_id)
        .execute()
        .data[0]
    )
    assert row["org_id"] == org_a_id
    assert row["symbol_map"]["e1"]["python_name"] == org_b_id  # stored as data, not acted on
    assert row["storage_prefix"] == f"{org_a_id}/{execution_job_id}/units/{cluster_id}/"


@pytest.mark.parametrize(
    "call,verify",
    [
        (
            lambda c, job_id, unit_id, org_id: mark_execution_running(c, job_id, org_id),
            lambda row, unit_row: row["status"] == "running" and row["progress_stage"] == "prepare",
        ),
        (
            lambda c, job_id, unit_id, org_id: mark_execution_stage(c, job_id, org_id, "assemble"),
            lambda row, unit_row: row["progress_stage"] == "assemble",
        ),
        (
            lambda c, job_id, unit_id, org_id: mark_execution_failed(c, job_id, org_id, "smoke test failure"),
            lambda row, unit_row: row["status"] == "failed" and row["error"] == "smoke test failure",
        ),
        (
            lambda c, job_id, unit_id, org_id: mark_execution_done(c, job_id, org_id),
            lambda row, unit_row: row["status"] == "done" and row["progress_stage"] is None,
        ),
        (
            lambda c, job_id, unit_id, org_id: set_execution_output(
                c, job_id, org_id, output_zip_path="x.zip", counts={"total": 1}, final_test_summary={"passed": 1}
            ),
            lambda row, unit_row: row["output_zip_path"] == "x.zip",
        ),
        (
            lambda c, job_id, unit_id, org_id: mark_unit_running(c, job_id, org_id, unit_id),
            lambda row, unit_row: unit_row["status"] == "running",
        ),
        (
            lambda c, job_id, unit_id, org_id: update_unit_execution(c, job_id, org_id, unit_id, status="error", last_error="smoke"),
            lambda row, unit_row: unit_row["status"] == "error" and unit_row["last_error"] == "smoke",
        ),
    ],
    ids=[
        "mark_execution_running",
        "mark_execution_stage",
        "mark_execution_failed",
        "mark_execution_done",
        "set_execution_output",
        "mark_unit_running",
        "update_unit_execution",
    ],
)
def test_status_write_with_mismatched_org_id_is_a_no_op(two_orgs, call, verify):
    service_client = two_orgs["service_client"]
    org_a_id, org_b_id = two_orgs["org_a_id"], two_orgs["org_b_id"]
    execution_job_id, unit_id = two_orgs["execution_job_id"], two_orgs["unit_cluster_id"]

    def fetch():
        job_row = service_client.table("execution_jobs").select("*").eq("id", execution_job_id).execute().data[0]
        unit_row = service_client.table("unit_executions").select("*").eq("execution_job_id", execution_job_id).eq("cluster_id", unit_id).execute().data[0]
        return job_row, unit_row

    baseline_job, baseline_unit = fetch()

    # The "trick": execution_job_id/unit_id genuinely exist, but tagged with the
    # wrong org_id — the shape a desync bug between "which job" and "which org"
    # would take.
    call(service_client, execution_job_id, unit_id, org_b_id)

    job_row, unit_row = fetch()
    assert job_row == baseline_job
    assert unit_row == baseline_unit
    assert not verify(job_row, unit_row)

    # Positive control — the same call with the *correct* org_id does write
    # through, proving the no-op above is the id+org_id filter working, not a
    # broken query.
    call(service_client, execution_job_id, unit_id, org_a_id)

    job_row, unit_row = fetch()
    assert verify(job_row, unit_row)
