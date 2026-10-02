"""Multi-tenancy smoke test (write path) for the planner worker — tasks_agent2.md
Phase 6, first task. Agent 2's counterpart of
`tests/test_worker_write_path_multi_tenancy.py` (Agent 1) and
`tests/test_agent3_write_path_multi_tenancy.py` (Agent 3).

The service-role connection the planner uses bypasses RLS by construction, so
the write path's only protection against a cross-org write is code discipline:

1. Every `org_id` the worker writes with comes from the trusted `planning_jobs`
   row (`run.py::_fetch_planning_job`, filtered by the queue message's org_id),
   never from repo content. Entity names, file paths and `raw_facts` are
   attacker-controlled, but they only ever reach the stages as data.
2. Every update/delete helper filters on both the key and `org_id`, so a desync
   between "which job" and "which org" is a no-op.

Both are checked against a real Postgres connection (FakeClient has no real
constraint semantics). The LLM and embedder are faked; everything else in
`process_planning_job` is real.

Requires live credentials (SUPABASE_URL or SUPABASE_DB_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY)
against a project with supabase/migrations applied; skips itself otherwise.
org_id is a bare column with no `orgs` table backing it, so no auth users needed.
"""

import json
import os
import uuid

import pytest
from dotenv import load_dotenv

load_dotenv()

from document_analyzer.agent2.worker import run as run_module  # noqa: E402
from document_analyzer.agent2.worker.status import (  # noqa: E402
    mark_planning_done,
    mark_planning_failed,
    mark_planning_running,
    mark_planning_stage,
)
from document_analyzer.supabase_client import get_service_role_client, insert_many_scoped, insert_scoped  # noqa: E402

# SUPABASE_URL is optional: supabase_client derives it from SUPABASE_DB_URL.
_LIVE_ENV_VARS = ["SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"]

pytestmark = pytest.mark.skipif(
    not (all(os.environ.get(name) for name in _LIVE_ENV_VARS) and (os.environ.get("SUPABASE_URL") or os.environ.get("SUPABASE_DB_URL"))),
    reason="requires a live Supabase project (SUPABASE_URL or SUPABASE_DB_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY)",
)

PARAMS = {"seed": 42, "alpha": 0.5, "max_unit_size": 25, "hub_percentile": 99, "leiden_resolution": 1.0}

# Every table the planner writes (design_agent2.md §2), plus the cost tables
# its LLM calls feed. All are checked for a stray org_id.
_AGENT2_TABLES = ["cluster_members", "cluster_reports", "cluster_edges", "migration_plan_items", "migration_plans", "clusters"]
_ORG_SCOPED_TABLES = [*_AGENT2_TABLES, "planning_jobs", "llm_invocations", "llm_job_costs"]


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class _FakeLLM:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        if "fact-checking" in kwargs["messages"][0]["content"]:
            return _Resp(json.dumps({"grounded": True, "issues": []}))
        return _Resp(json.dumps({"label": "A unit", "summary_text": "Does things."}))


def _seed_hostile_repo(service_client, org_a_id: str, org_b_id: str) -> dict:
    """An Agent 1 job under org A whose entities carry org B's id everywhere
    repo content can put it: names, file paths, raw_facts (including an
    `org_id` key and a call to a function named after it)."""
    project = insert_scoped(service_client, "projects", org_a_id, {"source_type": "github", "source_ref": "https://x/y"}).data[0]
    job = insert_scoped(
        service_client,
        "ingestion_jobs",
        org_a_id,
        {"project_id": project["id"], "source_type": "github", "source_ref": "https://x/y", "status": "done"},
    ).data[0]
    entities = []
    for prefix in ("a", "b"):
        for i in range(3):
            entities.append(
                {
                    "job_id": job["id"],
                    "entity_type": "function",
                    "name": f"{prefix}{i}_{org_b_id}",
                    "file_path": f"{org_b_id}/{prefix}.php",
                    "raw_facts": {"org_id": org_b_id, "params": [], "calls": [f"fn_{org_b_id}"], "note": f"owned by {org_b_id}"},
                    "content_hash": f"{prefix}{i}",
                }
            )
    rows = insert_many_scoped(service_client, "extracted_entities", org_a_id, entities).data
    ids = [r["id"] for r in rows]
    edges = [(ids[0], ids[1]), (ids[0], ids[2]), (ids[1], ids[2]), (ids[3], ids[4]), (ids[3], ids[5]), (ids[4], ids[5]), (ids[0], ids[3])]
    insert_many_scoped(
        service_client,
        "entity_edges",
        org_a_id,
        [{"job_id": job["id"], "from_entity_id": a, "to_entity_id": b, "edge_type": "calls"} for a, b in edges],
    )
    planning_job = insert_scoped(
        service_client,
        "planning_jobs",
        org_a_id,
        {"project_id": project["id"], "source_job_id": job["id"], "status": "queued", "params": PARAMS},
    ).data[0]
    return {"project_id": project["id"], "job_id": job["id"], "planning_job_id": planning_job["id"], "entity_ids": ids}


def _delete_org(service_client, org_id: str) -> None:
    # Children first; plan tables never cascade from clusters.
    for table in [
        "llm_job_costs",
        "llm_invocations",
        "migration_plan_items",
        "migration_plans",
        "cluster_edges",
        "cluster_reports",
        "cluster_members",
        "clusters",
        "planning_jobs",
        "entity_edges",
        "knowledge_summaries",
        "extracted_entities",
        "ingestion_jobs",
        "projects",
    ]:
        try:
            service_client.table(table).delete().eq("org_id", org_id).execute()
        except Exception:
            pass


@pytest.fixture
def two_orgs():
    service_client = get_service_role_client()
    org_a_id = str(uuid.uuid4())
    org_b_id = str(uuid.uuid4())  # never seeded with real rows — only attacker-controlled payload data
    seed = _seed_hostile_repo(service_client, org_a_id, org_b_id)
    try:
        yield {"service_client": service_client, "org_a_id": org_a_id, "org_b_id": org_b_id, **seed}
    finally:
        _delete_org(service_client, org_a_id)
        _delete_org(service_client, org_b_id)


def test_hostile_entity_content_cannot_change_the_stamped_org_id(two_orgs, monkeypatch):
    service_client = two_orgs["service_client"]
    org_a_id, org_b_id = two_orgs["org_a_id"], two_orgs["org_b_id"]
    planning_job_id = two_orgs["planning_job_id"]

    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-test")
    monkeypatch.setattr(run_module, "get_azure_openai_client", _FakeLLM)
    monkeypatch.setattr(run_module, "embed_text", lambda text: [0.0] * 384)
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: service_client)

    run_module.process_planning_job(planning_job_id, org_a_id)

    job = service_client.table("planning_jobs").select("*").eq("id", planning_job_id).execute().data[0]
    assert job["status"] == "done", job.get("error")

    # The run really wrote to every Agent 2 table (otherwise the checks below prove nothing)...
    for table in _AGENT2_TABLES:
        rows = service_client.table(table).select("org_id").eq("org_id", org_a_id).execute().data
        assert rows, f"{table} got no rows from the run"
    # ...and nothing landed under org B or any other org.
    for table in _ORG_SCOPED_TABLES:
        assert service_client.table(table).select("org_id").eq("org_id", org_b_id).execute().data == [], table
    # Rows reachable from org A's job all carry org A, not just rows filtered by it.
    clusters = service_client.table("clusters").select("id, org_id").eq("planning_job_id", planning_job_id).execute().data
    assert clusters and {c["org_id"] for c in clusters} == {org_a_id}
    plan = service_client.table("migration_plans").select("org_id").eq("planning_job_id", planning_job_id).execute().data
    assert {p["org_id"] for p in plan} == {org_a_id}

    # The crafted content survives only as data; it never became a label/id.
    members = service_client.table("cluster_members").select("org_id, entity_id").eq("org_id", org_a_id).execute().data
    assert {m["entity_id"] for m in members} <= set(two_orgs["entity_ids"])


@pytest.mark.parametrize(
    "call,verify",
    [
        (lambda c, job_id, org_id: mark_planning_running(c, job_id, org_id), lambda row: row["status"] == "running" and row["progress_stage"] == "graph"),
        (lambda c, job_id, org_id: mark_planning_stage(c, job_id, org_id, "cluster"), lambda row: row["progress_stage"] == "cluster"),
        (lambda c, job_id, org_id: mark_planning_failed(c, job_id, org_id, "smoke test failure"), lambda row: row["status"] == "failed" and row["error"] == "smoke test failure"),
        (lambda c, job_id, org_id: mark_planning_done(c, job_id, org_id), lambda row: row["status"] == "done"),
    ],
    ids=["mark_planning_running", "mark_planning_stage", "mark_planning_failed", "mark_planning_done"],
)
def test_status_write_with_mismatched_org_id_is_a_no_op(two_orgs, call, verify):
    service_client = two_orgs["service_client"]
    org_a_id, org_b_id = two_orgs["org_a_id"], two_orgs["org_b_id"]
    planning_job_id = two_orgs["planning_job_id"]

    def fetch():
        return service_client.table("planning_jobs").select("*").eq("id", planning_job_id).execute().data[0]

    baseline = fetch()

    # planning_job_id genuinely exists, but is tagged with the wrong org_id.
    call(service_client, planning_job_id, org_b_id)
    row = fetch()
    assert row == baseline
    assert not verify(row)

    # Positive control: the correct org_id does write through, so the no-op
    # above is the id+org_id filter working, not a broken query.
    call(service_client, planning_job_id, org_a_id)
    assert verify(fetch())


def test_stage_writes_with_mismatched_org_id_are_no_ops(two_orgs, monkeypatch):
    """Stage-level helpers that update or delete existing rows (graph_stats,
    cluster labels, cluster/plan deletes) are filtered by org_id too."""
    from document_analyzer.agent2.worker.graph import write_graph_stats
    from document_analyzer.agent2.worker.ordering import delete_unfinished_plan

    service_client = two_orgs["service_client"]
    org_a_id, org_b_id = two_orgs["org_a_id"], two_orgs["org_b_id"]
    planning_job_id = two_orgs["planning_job_id"]

    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-test")
    monkeypatch.setattr(run_module, "get_azure_openai_client", _FakeLLM)
    monkeypatch.setattr(run_module, "embed_text", lambda text: [0.0] * 384)
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: service_client)
    run_module.process_planning_job(planning_job_id, org_a_id)

    def plan_count():
        return len(service_client.table("migration_plans").select("id").eq("planning_job_id", planning_job_id).execute().data)

    def graph_stats():
        return service_client.table("planning_jobs").select("graph_stats").eq("id", planning_job_id).execute().data[0]["graph_stats"]

    baseline_stats = graph_stats()
    assert plan_count() == 1

    write_graph_stats(service_client, planning_job_id, org_b_id, {"planted": True})
    assert graph_stats() == baseline_stats

    delete_unfinished_plan(service_client, {"id": planning_job_id, "org_id": org_b_id})
    assert plan_count() == 1

    # Positive controls.
    write_graph_stats(service_client, planning_job_id, org_a_id, {"planted": True})
    assert graph_stats() == {"planted": True}
    delete_unfinished_plan(service_client, {"id": planning_job_id, "org_id": org_a_id})
    assert plan_count() == 0
