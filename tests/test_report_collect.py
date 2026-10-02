"""report/collect.py — design_report.md §6: explicit columns, org + job filters, fallbacks."""

import pytest

from document_analyzer.report import collect
from document_analyzer.report.collect import (
    FORBIDDEN_COLUMNS,
    ReportInputError,
    collect_architecture,
    collect_glance,
    collect_plan,
    collect_risk,
    collect_security,
)
from tests.fakes import FakeClient

ORG, JOB, PLAN, PJOB = "org-1", "job-1", "plan-1", "pjob-1"

# Tables read by an id list that came from an already-scoped query (plan clusters).
_ID_LIST_TABLES = {"clusters", "cluster_reports", "cluster_members"}


class _RecordingQuery:
    def __init__(self, inner, log, table, columns):
        self._inner, self._table, self.columns = inner, table, columns
        self.eqs: list[str] = []
        log.append(self)

    def eq(self, key, value):
        self.eqs.append(key)
        self._inner.eq(key, value)
        return self

    def __getattr__(self, name):
        attr = getattr(self._inner, name)

        def call(*args, **kwargs):
            result = attr(*args, **kwargs)
            return self if result is self._inner else result

        return call


class _RecordingTable:
    def __init__(self, table, log):
        self._table, self._log = table, log
        self.name = table.name

    def select(self, columns="*", **kwargs):
        return _RecordingQuery(self._table.select(columns), self._log, self.name, columns)


class RecordingClient(FakeClient):
    def __init__(self):
        super().__init__(ORG)
        self.queries: list[_RecordingQuery] = []

    def table(self, name):
        return _RecordingTable(super().table(name), self.queries)

    def seed(self, name, rows):
        super().table(name).rows.extend(rows)


def _entity(eid, kind, name, path):
    return {"id": eid, "org_id": ORG, "job_id": JOB, "entity_type": kind, "name": name, "file_path": path, "raw_facts": {"secret": "s3cr3t"}}


def _summary(eid, level="entity", risk="low", factors=(), text="does a thing", grounding="verified"):
    return {
        "id": f"s-{eid}-{level}", "org_id": ORG, "job_id": JOB, "level": level, "entity_id": eid,
        "summary_text": text, "risk_level": risk, "risk_factors": list(factors), "grounding_status": grounding, "embedding": [0.1],
    }


@pytest.fixture
def client():
    c = RecordingClient()
    c.seed("ingestion_jobs", [{
        "id": JOB, "org_id": ORG,
        "discovery_stats": {"loc": 1200, "language.PHP": 5, "language.Python": 2, "framework.WordPress": 1, "dependencies": 7},
        "extraction_coverage": {
            "evidence": {"secret_excerpt": "do not leak"},
            "graphify": {"status": "ok", "counts_by_status": {"extracted": 3, "failed": 1}, "by_language": {"Go": 4},
                         "not_extracted_count": 1, "not_extracted_sample": [{"path": "a.go", "status": "failed", "reason": "syntax"}]},
        },
    }])
    c.seed("filtered_files", [
        {"id": "f1", "org_id": ORG, "job_id": JOB, "file_path": "vendor/a", "reason": "composer_vendor_directory"},
        {"id": "f2", "org_id": ORG, "job_id": JOB, "file_path": "vendor/b", "reason": "composer_vendor_directory"},
        {"id": "f3", "org_id": ORG, "job_id": JOB, "file_path": "x", "reason": "vcs_directory"},
        {"id": "f9", "org_id": "other", "job_id": JOB, "file_path": "y", "reason": "vcs_directory"},
    ])
    c.seed("extracted_entities", [
        _entity("e1", "function", "run_query", "includes/db.php"),
        _entity("e2", "function", "render", "includes/view.php"),
        _entity("e3", "class", "App", "main.php"),
        {**_entity("e9", "function", "foreign", "x.php"), "org_id": "other"},
    ])
    c.seed("knowledge_summaries", [
        _summary("e1", risk="high", factors=["sql_concatenation", "superglobal_get"], text="Builds SQL."),
        _summary("e2", risk="medium", factors=[], text="Writes a file."),
        _summary("e3", risk="low"),
        _summary(None, level="repo", text="The repo."),
    ])
    c.seed("entity_edges", [
        {"id": "g1", "org_id": ORG, "job_id": JOB, "from_entity_id": "e1", "to_entity_id": "e2", "edge_type": "calls"},
        {"id": "g2", "org_id": ORG, "job_id": JOB, "from_entity_id": "e3", "to_entity_id": "e1", "edge_type": "calls"},
        {"id": "g3", "org_id": ORG, "job_id": JOB, "from_entity_id": "e3", "to_entity_id": None, "edge_type": "include"},
    ])
    c.seed("secret_findings", [
        {"id": "x1", "org_id": ORG, "job_id": JOB, "entity_id": "e1", "field_path": "raw_facts.key", "reason": "aws_key"},
        {"id": "x2", "org_id": ORG, "job_id": JOB, "entity_id": "e3", "field_path": "raw_facts.pw", "reason": "aws_key"},
    ])
    return c


def _plan_rows(c, status="approved", source=JOB):
    c.seed("migration_plans", [{"id": PLAN, "org_id": ORG, "planning_job_id": PJOB, "source_job_id": source, "version": 2, "status": status, "open_facade_flags": 0}])
    c.seed("planning_jobs", [{"id": PJOB, "org_id": ORG, "params": {"max_unit_size": 10}}])
    c.seed("clusters", [
        {"id": "ph1", "org_id": ORG, "planning_job_id": PJOB, "kind": "community", "size": 0, "label": "Phase A"},
        {"id": "ph2", "org_id": ORG, "planning_job_id": PJOB, "kind": "community", "size": 0, "label": "Phase B"},
        {"id": "u1", "org_id": ORG, "planning_job_id": PJOB, "kind": "foundation", "size": 3, "label": "core"},
        {"id": "u2", "org_id": ORG, "planning_job_id": PJOB, "kind": "community", "size": 40, "label": "billing"},
        {"id": "u3", "org_id": ORG, "planning_job_id": PJOB, "kind": "community", "size": 5, "label": "mail"},
    ])
    c.seed("migration_plan_items", [
        {"plan_id": PLAN, "org_id": ORG, "cluster_id": "u1", "phase_cluster_id": "ph1", "phase_order": 1, "unit_order": 1},
        {"plan_id": PLAN, "org_id": ORG, "cluster_id": "u2", "phase_cluster_id": "ph2", "phase_order": 2, "unit_order": 1},
        {"plan_id": PLAN, "org_id": ORG, "cluster_id": "u3", "phase_cluster_id": "ph2", "phase_order": 2, "unit_order": 2},
    ])
    c.seed("cluster_reports", [{"cluster_id": "u2", "org_id": ORG, "risk_level": "high", "grounding_status": "verified", "summary_text": "x"}])
    c.seed("cluster_members", [{"cluster_id": "u1", "entity_id": "e1", "org_id": ORG}])
    c.seed("cluster_edges", [
        {"id": "ce1", "org_id": ORG, "planning_job_id": PJOB, "level": "unit", "from_cluster_id": "u2", "to_cluster_id": "u3", "flag": "needs_facade", "scc_id": 1},
        {"id": "ce2", "org_id": ORG, "planning_job_id": PJOB, "level": "phase", "from_cluster_id": "ph2", "to_cluster_id": "ph1", "flag": "none", "scc_id": None},
    ])


def _everything(c):
    return [
        collect_glance(c, ORG, JOB),
        collect_architecture(c, ORG, JOB),
        collect_risk(c, ORG, JOB),
        collect_security(c, ORG, JOB),
        collect_architecture(c, ORG, JOB, plan={"planning_job_id": PJOB}),
        collect_plan(c, ORG, JOB, PLAN),
    ]


# --- the rules ------------------------------------------------------------------------


def test_no_collector_selects_a_forbidden_column_and_every_query_is_scoped(client):
    _plan_rows(client)
    _everything(client)
    assert client.queries
    for query in client.queries:
        columns = {c.strip() for c in query.columns.split(",")}
        assert not columns & FORBIDDEN_COLUMNS, (query._table, query.columns)
        assert "org_id" in query.eqs, (query._table, "no org_id filter")
        scoped = {"job_id", "id", "workflow_id", "plan_id", "planning_job_id"} & set(query.eqs)
        assert scoped or query._table in _ID_LIST_TABLES, (query._table, "no job/plan filter")


def test_forbidden_column_is_refused_at_runtime(client):
    with pytest.raises(ValueError):
        collect._rows(client, "extracted_entities", "id, raw_facts", "id", {"org_id": ORG})
    with pytest.raises(ValueError):
        collect._rows(client, "extracted_entities", "*", "id", {"org_id": ORG})


def test_no_secret_or_raw_value_reaches_any_section(client):
    _plan_rows(client)
    text = repr(_everything(client))
    assert "s3cr3t" not in text and "do not leak" not in text and "[REDACTED" not in text


# --- glance ----------------------------------------------------------------------------


def test_glance_counts_scoped_to_org_and_sorted(client):
    data, limits = collect_glance(client, ORG, JOB)
    assert data["loc"] == 1200 and data["dependencies"] == 7
    assert data["languages"] == [{"name": "PHP", "files": 5}, {"name": "Python", "files": 2}]
    assert data["frameworks"] == ["WordPress"]
    assert data["files_filtered"] == [{"reason": "composer_vendor_directory", "count": 2}, {"reason": "vcs_directory", "count": 1}]
    assert data["entities_by_type"] == {"class": 1, "function": 2}
    assert data["files_analysed"] == 3
    assert data["coverage"]["not_extracted_sample"] == [{"path": "a.go", "status": "failed", "reason": "syntax"}]
    assert "evidence" not in str(data["coverage"])
    assert [l["code"] for l in limits] == ["files_not_parsed"]


def test_glance_falls_back_to_stats_event_then_to_not_recorded(client):
    job = client._tables["ingestion_jobs"].rows[0]
    job["discovery_stats"], job["extraction_coverage"] = None, None
    data, limits = collect_glance(client, ORG, JOB)
    assert data["loc"] is None and data["coverage"] is None
    assert {l["code"] for l in limits} == {"stats_not_recorded", "coverage_not_recorded"}

    client.seed("workflow_events", [
        {"id": "w1", "org_id": ORG, "workflow_kind": "agent1_ingestion", "workflow_id": JOB, "event_type": "stats.updated", "data": {"loc": 10}, "created_at": "2026-01-01"},
        {"id": "w2", "org_id": ORG, "workflow_kind": "agent1_ingestion", "workflow_id": JOB, "event_type": "stats.updated", "data": {"loc": 99}, "created_at": "2026-01-02"},
    ])
    data, limits = collect_glance(client, ORG, JOB)
    assert data["loc"] == 99
    assert "stats_from_events" in {l["code"] for l in limits}


# --- architecture, risk, security ------------------------------------------------------------


def test_architecture_subsystems_hubs_and_connectivity(client):
    data, limits = collect_architecture(client, ORG, JOB)
    assert [s["name"] for s in data["subsystems"]] == ["(root)", "includes"]
    includes = data["subsystems"][1]
    assert includes["risk_level"] == "high" and includes["modules"] == ["includes/db.php", "includes/view.php"]
    assert includes["summary"] is None
    # e1 and e3 both have degree 2; ties break by name.
    assert [(h["name"], h["degree"]) for h in data["hubs"]] == [("App", 2), ("run_query", 2), ("render", 1)]
    assert data["connectivity"] == [{"from": "(root)", "to": "includes", "edges": 1}]
    assert "summaries_not_attributable" in {l["code"] for l in limits}


def test_architecture_uses_foundation_unit_for_hubs_when_plan_exists(client):
    _plan_rows(client)
    data, _ = collect_architecture(client, ORG, JOB, plan={"planning_job_id": PJOB})
    assert data["hubs"] == [{"name": "run_query", "file_path": "includes/db.php", "degree": 2}]


def test_risk_heat_map_hotspots_and_missing_signal(client):
    data, limits = collect_risk(client, ORG, JOB)
    assert data["heat_map"] == [
        {"subsystem": "(root)", "low": 1, "medium": 0, "high": 0},
        {"subsystem": "includes", "low": 0, "medium": 1, "high": 1},
    ]
    assert data["hotspots"][0]["subsystem"] == "includes"
    assert [(e["name"], e["risk_level"], e["signal"]) for e in data["hotspots"][0]["entities"]] == [
        ("run_query", "high", "sql_concatenation, superglobal_get"),
        ("render", "medium", None),
    ]
    assert data["hotspots"][0]["more"] == 0
    assert "signal_not_recorded" in {l["code"] for l in limits}


def test_security_counts_and_locations_without_values(client):
    data, _ = collect_security(client, ORG, JOB)
    assert data["secrets_by_reason"] == [{
        "reason": "aws_key", "count": 2,
        "locations": [
            {"file_path": "includes/db.php", "field_path": "raw_facts.key"},
            {"file_path": "main.php", "field_path": "raw_facts.pw"},
        ],
    }]
    assert {"pattern": "sql_built_by_concatenation", "count": 1} in data["risky_patterns"]
    assert "false negatives" in data["caveat"]


def test_risky_patterns_match_whole_words_only(client):
    client._tables["knowledge_summaries"].rows.append(_summary("e2", risk="high", factors=["retrieval_logic"]))
    data, _ = collect_security(client, ORG, JOB)
    assert not any(p["pattern"] == "dynamic_code_execution" for p in data["risky_patterns"])


# --- plan ------------------------------------------------------------------------------------


def test_plan_phases_cycles_oversized_and_status(client):
    _plan_rows(client, status="draft")
    data, limits = collect_plan(client, ORG, JOB, PLAN)
    assert (data["status"], data["version"], data["units"]) == ("draft", 2, 3)
    assert [p["label"] for p in data["phases"]] == ["Phase A", "Phase B"]
    assert [u["label"] for u in data["phases"][1]["units"]] == ["billing", "mail"]
    assert data["phases"][1]["units"][0]["risk_level"] == "high"
    assert data["cycles_needing_facades"] == [{"units": ["billing", "mail"]}]
    assert data["oversized_units"] == [{"label": "billing", "size": 40}]
    assert data["phase_dependencies"] == [{"from": 2, "to": 1}]
    assert "plan_not_approved" in {l["code"] for l in limits}


@pytest.mark.parametrize("status", ["stale", "rejected"])
def test_plan_ineligible_status_is_refused(client, status):
    _plan_rows(client, status=status)
    with pytest.raises(ReportInputError):
        collect_plan(client, ORG, JOB, PLAN)


def test_plan_pinned_to_another_job_is_refused(client):
    _plan_rows(client, source="job-other")
    with pytest.raises(ReportInputError):
        collect_plan(client, ORG, JOB, PLAN)


def test_output_is_deterministic(client):
    _plan_rows(client)
    assert repr(_everything(client)) == repr(_everything(client))
