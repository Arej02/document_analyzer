"""Multi-tenancy smoke test (write path) — tasks.md Phase 6, second task.

The read-path test (tests/test_rls_multi_tenancy.py) proves Postgres RLS
blocks a cross-org *read*. RLS never applies to the worker's own connection
though (supabase_client.get_service_role_client bypasses it by construction,
per CLAUDE.md) — the write path's only protection against a cross-org write is
the worker's own code discipline: every write stamps org_id from the trusted
`ingestion_jobs` row, never from ingested repo content, and every update-by-id
call filters by both `id` and `org_id` as defense-in-depth. This test exercises
that discipline against a real Postgres connection (every other worker test
uses tests/fakes.py::FakeClient, which can't tell a leaked cross-org write from
a correct one — it has no real row security or query semantics to check).

Two things are checked:
1. Crafted repo content can't smuggle a different org_id through
   persist_extraction_result — an entity's name/file_path/raw_facts are
   attacker-controlled (they come straight from parsing an untrusted repo),
   but they only ever become *values* in the row dict persist.py builds, never
   new top-level keys, so there's no path from "malicious file content" to a
   literal `org_id` key. insert_scoped/insert_many_scoped's own guard against
   an already-present `org_id` key is the unit-level backstop for that same
   invariant (tests/test_supabase_client.py).
2. If some future bug ever desynced the `org_id` argument passed to an
   update-by-id worker write from the job's *actual* org_id (the realistic
   shape of "tricked into writing under the wrong org" for these functions,
   since they take org_id as a parameter rather than looking it up
   themselves), the `.eq("id", ...).eq("org_id", ...)` filter fails safe:
   zero rows match, nothing is written — checked against real Postgrest
   filtering, not just tests/fakes.py::FakeClient's approximation of it.

Requires live credentials (SUPABASE_URL/SUPABASE_ANON_KEY/SUPABASE_SERVICE_ROLE_KEY)
against a project with supabase/migrations applied; skips itself otherwise. No
auth users needed here (unlike the read-path test) — org_id is a bare column
with no `orgs` table backing it, and this test only exercises the service-role
connection, which never goes through RLS/JWTs at all.
"""

import os
import uuid

import pytest
from dotenv import load_dotenv

# Call directly (same as app.py/worker/run.py) rather than relying on another
# test module's import order to have populated os.environ first — see
# test_rls_multi_tenancy.py for the same fix applied there.
load_dotenv()

from document_analyzer.supabase_client import get_service_role_client, insert_scoped  # noqa: E402
from document_analyzer.worker.diff import JobDiff, persist_diff_counts  # noqa: E402
from document_analyzer.worker.extract import ExtractedEdge, ExtractedEntity, ExtractionResult  # noqa: E402
from document_analyzer.worker.ground import GroundingResult  # noqa: E402
from document_analyzer.worker.ground_retry import GroundingRetryResult  # noqa: E402
from document_analyzer.worker.grounding_status import persist_grounding_status  # noqa: E402
from document_analyzer.worker.job_completion import mark_job_done_if_ready  # noqa: E402
from document_analyzer.worker.persist import persist_extraction_result  # noqa: E402
from document_analyzer.worker.status import mark_job_failed, mark_job_running, mark_progress_stage  # noqa: E402

_LIVE_ENV_VARS = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"]

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in _LIVE_ENV_VARS),
    reason="requires a live Supabase project (SUPABASE_URL/SUPABASE_ANON_KEY/SUPABASE_SERVICE_ROLE_KEY)",
)


def _delete_org(service_client, org_id: str) -> None:
    for table in [
        "secret_findings",
        "summary_cache",
        "knowledge_summaries",
        "entity_edges",
        "extracted_entities",
        "ingestion_jobs",
    ]:
        service_client.table(table).delete().eq("org_id", org_id).execute()


@pytest.fixture
def two_org_jobs():
    service_client = get_service_role_client()
    org_a_id = str(uuid.uuid4())
    org_b_id = str(uuid.uuid4())

    job_a = insert_scoped(
        service_client, "ingestion_jobs", org_a_id, {"source_type": "upload", "source_ref": "write-path-smoke-a"}
    ).data[0]
    job_b = insert_scoped(
        service_client, "ingestion_jobs", org_b_id, {"source_type": "upload", "source_ref": "write-path-smoke-b"}
    ).data[0]

    try:
        yield {
            "service_client": service_client,
            "org_a_id": org_a_id,
            "org_b_id": org_b_id,
            "job_a": job_a,
            "job_b": job_b,
        }
    finally:
        _delete_org(service_client, org_a_id)
        _delete_org(service_client, org_b_id)


def test_crafted_repo_content_cannot_smuggle_a_different_org_id(two_org_jobs):
    service_client = two_org_jobs["service_client"]
    org_a_id, org_b_id = two_org_jobs["org_a_id"], two_org_jobs["org_b_id"]
    job_a = two_org_jobs["job_a"]

    # Every field here is attacker-controlled — this is exactly what a hostile
    # PHP file's function/class name, file path, or literal array contents
    # could contain, including org_b_id itself as a plain string trying to look
    # like it belongs somewhere sensitive.
    result = ExtractionResult(
        entities=[
            ExtractedEntity(
                local_id="attacker",
                entity_type="function",
                name=f"org_id={org_b_id}",
                file_path=f"{org_b_id}/evil.php",
                raw_facts={"org_id": org_b_id, "nested": {"org_id": org_b_id}},
            ),
            ExtractedEntity(
                local_id="target",
                entity_type="function",
                name="target_fn",
                file_path="evil.php",
                raw_facts={},
            ),
        ],
        edges=[ExtractedEdge(from_local_id="attacker", edge_type="calls", to_local_id="target")],
    )

    persist_extraction_result(service_client, job_a["id"], org_a_id, result)

    entity_rows = service_client.table("extracted_entities").select("*").eq("job_id", job_a["id"]).execute().data
    assert len(entity_rows) == 2
    for row in entity_rows:
        assert row["org_id"] == org_a_id  # stamped from the job, never from content — regardless of content

    crafted_row = next(row for row in entity_rows if row["name"] == f"org_id={org_b_id}")
    # The crafted content is persisted verbatim (it's just data)...
    assert crafted_row["file_path"] == f"{org_b_id}/evil.php"
    assert crafted_row["raw_facts"]["org_id"] == org_b_id
    assert crafted_row["raw_facts"]["nested"]["org_id"] == org_b_id
    # ...but never influences the real org_id column.
    assert crafted_row["org_id"] == org_a_id

    edge_rows = service_client.table("entity_edges").select("*").eq("job_id", job_a["id"]).execute().data
    assert len(edge_rows) == 1
    assert edge_rows[0]["org_id"] == org_a_id

    # And org B's job is untouched by any of this — nothing crossed over.
    other_org_entities = service_client.table("extracted_entities").select("*").eq("job_id", two_org_jobs["job_b"]["id"]).execute().data
    assert other_org_entities == []


@pytest.mark.parametrize(
    "call",
    [
        lambda c, job_id, org_id: mark_job_running(c, job_id, org_id),
        lambda c, job_id, org_id: mark_progress_stage(c, job_id, org_id, "summarizing"),
        lambda c, job_id, org_id: mark_job_failed(c, job_id, org_id, "smoke test failure"),
    ],
    ids=["mark_job_running", "mark_progress_stage", "mark_job_failed"],
)
def test_status_write_with_mismatched_org_id_does_not_touch_the_real_job(two_org_jobs, call):
    service_client = two_org_jobs["service_client"]
    job_a_id = two_org_jobs["job_a"]["id"]
    org_b_id = two_org_jobs["org_b_id"]

    # The "trick": job_a_id genuinely exists, but tagged with the wrong org_id —
    # the shape a desync bug between "which job" and "which org" would take.
    call(service_client, job_a_id, org_b_id)

    row = service_client.table("ingestion_jobs").select("*").eq("id", job_a_id).execute().data[0]
    assert row["status"] == "queued"
    assert row["progress_stage"] is None
    assert row["error"] is None

    # Positive control — the same call with the *correct* org_id does write through,
    # proving the no-op above is the id+org_id filter working, not a broken query.
    call(service_client, job_a_id, two_org_jobs["org_a_id"])
    row = service_client.table("ingestion_jobs").select("*").eq("id", job_a_id).execute().data[0]
    assert row["status"] != "queued" or row["progress_stage"] == "summarizing" or row["error"] == "smoke test failure"


def test_mark_job_done_with_mismatched_org_id_does_not_complete_the_real_job(two_org_jobs):
    service_client = two_org_jobs["service_client"]
    org_a_id, org_b_id = two_org_jobs["org_a_id"], two_org_jobs["org_b_id"]
    job_a_id = two_org_jobs["job_a"]["id"]

    # job_a has zero entities, so job_ready_for_completion reads as vacuously
    # ready under *either* org_id — the mismatch has to be caught by the final
    # update's id+org_id filter, not by readiness happening to be false.
    transitioned = mark_job_done_if_ready(service_client, job_a_id, org_b_id)
    assert transitioned is False

    row = service_client.table("ingestion_jobs").select("*").eq("id", job_a_id).execute().data[0]
    assert row["status"] == "queued"

    transitioned = mark_job_done_if_ready(service_client, job_a_id, org_a_id)
    assert transitioned is True

    row = service_client.table("ingestion_jobs").select("*").eq("id", job_a_id).execute().data[0]
    assert row["status"] == "done"


def test_persist_grounding_status_with_mismatched_org_id_does_not_touch_the_real_summary(two_org_jobs):
    service_client = two_org_jobs["service_client"]
    org_a_id, org_b_id = two_org_jobs["org_a_id"], two_org_jobs["org_b_id"]
    job_a = two_org_jobs["job_a"]

    entity = insert_scoped(
        service_client,
        "extracted_entities",
        org_a_id,
        {
            "job_id": job_a["id"],
            "entity_type": "function",
            "name": "smoke_fn",
            "file_path": "smoke.php",
            "raw_facts": {},
            "content_hash": f"smoke-{uuid.uuid4().hex}",
        },
    ).data[0]
    summary = insert_scoped(
        service_client,
        "knowledge_summaries",
        org_a_id,
        {
            "job_id": job_a["id"],
            "level": "entity",
            "entity_id": entity["id"],
            "summary_text": "smoke test summary",
            "risk_level": "low",
        },
    ).data[0]
    grounded_result = GroundingRetryResult(summary=None, grounding=GroundingResult(verdict="grounded", issues=[]), retries_used=0)

    persist_grounding_status(service_client, summary["id"], org_b_id, grounded_result)

    row = service_client.table("knowledge_summaries").select("*").eq("id", summary["id"]).execute().data[0]
    assert row["grounding_status"] == "unverified"  # untouched — still persist_summary.py's default draft state

    persist_grounding_status(service_client, summary["id"], org_a_id, grounded_result)

    row = service_client.table("knowledge_summaries").select("*").eq("id", summary["id"]).execute().data[0]
    assert row["grounding_status"] == "verified"


def test_persist_diff_counts_with_mismatched_org_id_does_not_touch_the_real_job(two_org_jobs):
    service_client = two_org_jobs["service_client"]
    org_a_id, org_b_id = two_org_jobs["org_a_id"], two_org_jobs["org_b_id"]
    job_a_id = two_org_jobs["job_a"]["id"]
    diff = JobDiff(previous_job_id=None, changed_file_paths={"a.php", "b.php"})

    persist_diff_counts(service_client, job_a_id, org_b_id, diff)

    row = service_client.table("ingestion_jobs").select("*").eq("id", job_a_id).execute().data[0]
    assert row["changed_file_count"] is None

    persist_diff_counts(service_client, job_a_id, org_a_id, diff)

    row = service_client.table("ingestion_jobs").select("*").eq("id", job_a_id).execute().data[0]
    assert row["changed_file_count"] == 2
