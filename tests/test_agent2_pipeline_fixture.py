"""End-to-end planner run over a PHP fixture with a known circular dependency —
tasks_agent2.md Phase 4, last task.

`tests/fixtures/agent2_cycle_repo/` has three files whose entry functions call
each other in a cycle (billing_total -> tax_rate -> shipping_cost ->
billing_total), plus two WordPress hook callbacks. Agent 1's real extractors
(PHP subprocesses) and persist step fill a fake Supabase client. Then the real
planner worker (`process_planning_job`) runs all four stages, with only the
LLM and the embedder faked.
"""

import json
import shutil
from pathlib import Path

import pytest

from document_analyzer.agent2.worker import run as run_module
from document_analyzer.worker.persist import persist_extraction_result
from document_analyzer.worker.php_extractor import PhpExtractor
from document_analyzer.worker.wordpress_extractor import WordPressExtractor

from fakes import FakeClient

pytestmark = pytest.mark.skipif(shutil.which("php") is None, reason="needs the php binary for Agent 1's extractors")

FIXTURE = Path(__file__).parent / "fixtures" / "agent2_cycle_repo"
PARAMS = {"seed": 42, "alpha": 0.5, "max_unit_size": 25, "hub_percentile": 99, "leiden_resolution": 1.0}
CYCLE = ("billing_total", "tax_rate", "shipping_cost")


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class FakeLLM:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        if "fact-checking" in kwargs["messages"][0]["content"]:
            return _Resp(json.dumps({"grounded": True, "issues": []}))
        return _Resp(json.dumps({"label": "A unit", "summary_text": "Does things."}))


def _client():
    client = FakeClient()
    # The FKs that cascade (20260923000000_agent2_planning_tables.sql). Plan tables never do.
    client.cascades["clusters"] = [
        ("clusters", "parent_cluster_id"),
        ("cluster_members", "cluster_id"),
        ("cluster_reports", "cluster_id"),
        ("cluster_edges", "from_cluster_id"),
    ]
    for extractor in (PhpExtractor(), WordPressExtractor()):
        persist_extraction_result(client, "job-1", "org-1", extractor.extract(FIXTURE))
    client.table("planning_jobs").insert(
        {"id": "pj-1", "org_id": "org-1", "project_id": "project-1", "source_job_id": "job-1", "status": "queued",
         "progress_stage": None, "params": PARAMS}
    ).execute()
    return client


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-test")
    monkeypatch.setattr(run_module, "get_azure_openai_client", FakeLLM)
    monkeypatch.setattr(run_module, "embed_text", lambda text: [0.0] * 384)

    def _run(client):
        monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
        run_module.process_planning_job("pj-1", "org-1")
        return client

    return _run


def _names(client):
    return {e["id"]: e["name"] for e in client.table("extracted_entities").rows}


def _unit_of_name(client):
    names = _names(client)
    return {names[m["canonical_entity_id"]]: m["cluster_id"] for m in client.table("cluster_members").rows}


def _plan_layout(client, planning_job_id):
    """(phase_order, unit_order) -> the unit's member names, for one planning job's plan."""
    names = _names(client)
    members: dict[str, set] = {}
    for m in client.table("cluster_members").rows:
        members.setdefault(m["cluster_id"], set()).add(names[m["canonical_entity_id"]])
    [plan] = [p for p in client.table("migration_plans").rows if p["planning_job_id"] == planning_job_id]
    return {
        (i["phase_order"], i["unit_order"]): frozenset(members.get(i["cluster_id"], ()))
        for i in client.table("migration_plan_items").rows
        if i["plan_id"] == plan["id"]
    }


def test_pipeline_completes_and_emits_a_draft_plan(run):
    client = run(_client())

    [job] = client.table("planning_jobs").rows
    assert (job["status"], job["progress_stage"], job.get("error")) == ("done", None, None)
    [plan] = client.table("migration_plans").rows
    assert (plan["version"], plan["status"], plan["org_id"]) == (1, "draft", "org-1")

    # Every canonical entity lands in exactly one planned unit.
    items = client.table("migration_plan_items").rows
    planned = {i["cluster_id"] for i in items}
    unit_of = _unit_of_name(client)
    assert set(unit_of) >= set(CYCLE) | {"cycle_register", "cycle_render_invoice", "init", "save_post"}
    assert set(unit_of.values()) == planned
    # Orders are 1-based and gap-free within each phase.
    by_phase: dict[int, list[int]] = {}
    for i in items:
        by_phase.setdefault(i["phase_order"], []).append(i["unit_order"])
    assert sorted(by_phase) == list(range(1, len(by_phase) + 1))
    assert all(sorted(v) == list(range(1, len(v) + 1)) for v in by_phase.values())


def test_the_three_file_cycle_is_flagged_needs_facade(run):
    client = run(_client())
    unit_of = _unit_of_name(client)
    cycle_units = {unit_of[name] for name in CYCLE}
    assert len(cycle_units) == 3  # one unit per file

    edges = client.table("cluster_edges").rows
    unit_cycle = [e for e in edges if e["level"] == "unit" and e["flag"] == "needs_facade"]
    assert {(e["from_cluster_id"], e["to_cluster_id"]) for e in unit_cycle} == {
        (unit_of["billing_total"], unit_of["tax_rate"]),
        (unit_of["tax_rate"], unit_of["shipping_cost"]),
        (unit_of["shipping_cost"], unit_of["billing_total"]),
    }
    assert len({e["scc_id"] for e in unit_cycle}) == 1

    # With seed 42 the cycle spans two phases, so it can't auto-merge and the phases form a cycle too.
    phases = {c["id"]: c["parent_cluster_id"] for c in client.table("clusters").rows if c["level"] == "unit"}
    assert len({phases[u] for u in cycle_units}) == 2
    assert [e for e in edges if e["level"] == "phase" and e["flag"] == "needs_facade"]
    assert not [c for c in client.table("clusters").rows if c["kind"] == "merged_scc"]
    [plan] = client.table("migration_plans").rows
    assert plan["open_facade_flags"] == 2
    assert client.table("planning_jobs").rows[0]["graph_stats"]["facade_sccs"] == 2


def test_order_puts_dependencies_first_and_contracts_carry_facts(run):
    client = run(_client())
    unit_of = _unit_of_name(client)
    items = {i["cluster_id"]: i for i in client.table("migration_plan_items").rows}

    def pos(name):
        item = items[unit_of[name]]
        return item["phase_order"], item["unit_order"]

    # cycle_render_invoice calls billing_format, so billing's unit migrates first.
    assert pos("billing_format") < pos("cycle_render_invoice")

    billing = items[unit_of["billing_total"]]["test_contract"]
    public = {p["name"] for p in billing["public_interface"]}
    assert {"billing_total", "billing_format"} <= public  # called from other units
    assert "billing_sum" not in public
    # PHP builtins the billing functions call, from raw_facts.calls, never the resolved in-repo ones.
    assert billing["external_calls"] == ["number_format", "round"]
    assert {d["to_unit_id"] for d in billing["outgoing_dependencies"]} == {unit_of["tax_rate"]}

    render = items[unit_of["cycle_render_invoice"]]["test_contract"]
    assert render["hooks_bound"] == ["save_post"]
    assert "cycle_render_invoice" in {p["name"] for p in render["public_interface"]}  # a bound callback


def test_rerun_of_the_same_job_replaces_its_plan(run):
    client = run(_client())
    client.table("planning_jobs").update({"status": "queued"}).eq("id", "pj-1").execute()

    run(client)

    [plan] = client.table("migration_plans").rows
    clusters = {c["id"] for c in client.table("clusters").rows}
    items = client.table("migration_plan_items").rows
    assert all(i["plan_id"] == plan["id"] for i in items)
    assert all(i["cluster_id"] in clusters and i["phase_cluster_id"] in clusters for i in items)


def test_same_snapshot_and_seed_give_the_same_plan(run):
    # Determinism is per ingestion snapshot: a re-ingestion mints new entity ids,
    # and ids are the last tie-break (§8), so two planning jobs share one source job.
    client = run(_client())
    client.table("planning_jobs").insert(
        {"id": "pj-2", "org_id": "org-1", "project_id": "project-1", "source_job_id": "job-1", "status": "queued",
         "progress_stage": None, "params": PARAMS}
    ).execute()
    run_module.process_planning_job("pj-2", "org-1")

    assert _plan_layout(client, "pj-1") == _plan_layout(client, "pj-2")


def _snapshot(client, planning_job_id):
    """Everything a planning job produced, keyed by member names instead of
    generated ids: unit and phase clusters (member sets), which units sit in which
    phase, cluster edges, and the ordered plan with its contracts."""
    names = _names(client)
    clusters = [c for c in client.table("clusters").rows if c["planning_job_id"] == planning_job_id]
    members: dict[str, set] = {}
    for m in client.table("cluster_members").rows:
        members.setdefault(m["cluster_id"], set()).add(names[m["canonical_entity_id"]])
    unit_key = {c["id"]: frozenset(members.get(c["id"], ())) for c in clusters if c["level"] == "unit"}
    phase_key = {
        c["id"]: frozenset(k for u in clusters if u["level"] == "unit" and u["parent_cluster_id"] == c["id"] for k in [unit_key[u["id"]]])
        for c in clusters
        if c["level"] == "phase"
    }
    key = {**unit_key, **phase_key}
    label = {cid: "+".join(sorted(k)) for cid, k in unit_key.items()}

    def relabel(value):
        # Contracts reference other units by cluster id, which is minted per job.
        if isinstance(value, dict):
            return {k: relabel(v) for k, v in value.items()}
        if isinstance(value, list):
            return [relabel(v) for v in value]
        return label.get(value, value) if isinstance(value, str) else value

    [plan] = [p for p in client.table("migration_plans").rows if p["planning_job_id"] == planning_job_id]
    return {
        "units": {(c["kind"], unit_key[c["id"]]) for c in clusters if c["level"] == "unit"},
        "phases": {(c["kind"], phase_key[c["id"]]) for c in clusters if c["level"] == "phase"},
        "edges": {
            (e["level"], key[e["from_cluster_id"]], key[e["to_cluster_id"]], e["call_count"], e["hook_binding_count"], e["weight"], e["flag"])
            for e in client.table("cluster_edges").rows
            if e["planning_job_id"] == planning_job_id
        },
        "plan": sorted(
            (i["phase_order"], i["unit_order"], unit_key[i["cluster_id"]], phase_key[i["phase_cluster_id"]], json.dumps(relabel(i["test_contract"]), sort_keys=True, default=str))
            for i in client.table("migration_plan_items").rows
            if i["plan_id"] == plan["id"]
        ),
        "open_facade_flags": plan["open_facade_flags"],
    }


def test_two_jobs_with_the_same_params_give_identical_clusters_and_order(run):
    client = run(_client())
    for pj in ("pj-2", "pj-3"):
        client.table("planning_jobs").insert(
            {"id": pj, "org_id": "org-1", "project_id": "project-1", "source_job_id": "job-1", "status": "queued",
             "progress_stage": None, "params": PARAMS}
        ).execute()
        run_module.process_planning_job(pj, "org-1")

    first = _snapshot(client, "pj-1")
    assert first["units"] and first["phases"] and first["plan"]  # the comparison below isn't vacuous
    assert _snapshot(client, "pj-2") == first
    assert _snapshot(client, "pj-3") == first


def test_override_recomputation_reproduces_the_worker_plan(run):
    # design_agent2.md §9.2: every version is rebuilt from version 1 with the stored
    # clusters, edges and contracts. With nothing to change, that rebuild must match
    # what the worker emitted, order, contracts and flag count alike.
    from document_analyzer.agent2.overrides import build_items, load_base, load_chain, replay

    client = run(_client())
    [plan] = client.table("migration_plans").rows

    root, chain = load_chain(client, "org-1", plan)
    base = load_base(client, "org-1", root["id"], "pj-1")
    rows, flags = build_items(base, replay(base, chain))

    def layout(items):
        return {
            i["cluster_id"]: (i["phase_cluster_id"], i["phase_order"], i["unit_order"], i["test_contract"])
            for i in items
        }

    assert layout(rows) == layout(client.table("migration_plan_items").rows)
    assert flags == plan["open_facade_flags"] == 2


def _add_dependency(client):
    client.table("extracted_entities").insert(
        {"id": "dep-1", "org_id": "org-1", "job_id": "job-1", "entity_type": "dependency", "name": "monolog/monolog",
         "file_path": "composer.json",
         "raw_facts": {"ecosystem": "composer", "version_spec": "^3", "manifest_path": "composer.json", "dev": False}}
    ).execute()


class DependencyAwareLLM(FakeLLM):
    def create(self, **kwargs):
        if "PyPI" in kwargs["messages"][0]["content"]:
            return _Resp(json.dumps({"targets": [
                {"package": "monolog/monolog", "ecosystem": "composer", "target_name": "structlog", "rationale": "Logging, ^3."}]}))
        return super().create(**kwargs)


def test_pipeline_proposes_dependency_targets_for_the_v1_plan_and_keeps_packages_out_of_the_plan(run, monkeypatch):
    client = _client()
    _add_dependency(client)
    monkeypatch.setattr(run_module, "get_azure_openai_client", DependencyAwareLLM)

    client = run(client)

    [plan] = client.table("migration_plans").rows
    [target] = client.table("plan_dependency_targets").rows
    assert (target["plan_id"], target["entity_id"], target["target_name"], target["grounding_status"]) == (
        plan["id"], "dep-1", "structlog", "verified")
    # A package is not code: it is in no unit, and the entity count is unchanged for the plan.
    assert "dep-1" not in {m["entity_id"] for m in client.table("cluster_members").rows}


def test_pipeline_still_finishes_when_dependency_targets_fail(run, monkeypatch):
    client = _client()
    _add_dependency(client)

    class Boom(FakeLLM):
        def create(self, **kwargs):
            if "PyPI" in kwargs["messages"][0]["content"]:
                raise RuntimeError("model down")
            return super().create(**kwargs)

    monkeypatch.setattr(run_module, "get_azure_openai_client", Boom)

    client = run(client)

    [job] = client.table("planning_jobs").rows
    assert (job["status"], job.get("error")) == ("done", None)
    assert client.table("migration_plans").rows and client.table("plan_dependency_targets").rows == []
