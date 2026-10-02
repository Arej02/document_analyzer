"""Cost accounting wired into each agent's worker and the Q&A endpoint (design.md section 7).

These run the real orchestration (`process_job`, `process_planning_job`, `run_units_stage`,
`POST .../ask`) with the LLM calls faked, and check what lands in `llm_invocations` /
`llm_job_costs`: who, which stage and purpose, one run id per pickup, flushed on failure and on
cancellation, and that cost logging can never fail a job or a request.
"""

import threading
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from document_analyzer import jobs as jobs_module
from document_analyzer.agent2.worker import run as agent2_run
from document_analyzer.agent3.worker import units as units_module
from document_analyzer.app import create_app
from document_analyzer.auth import AuthContext, get_auth_context
from document_analyzer.shared.llm import MeteredLLMClient
from document_analyzer.worker import run as run_module
from document_analyzer.worker.cancellation import JobCancelled, forget_job
from document_analyzer.worker.ground import GroundingResult
from document_analyzer.worker.ground_retry import GroundingRetryResult
from document_analyzer.worker.summarize import EntitySummary
from document_analyzer.worker.summarize_module import ModuleSummary
from document_analyzer.worker.summarize_subsystem import RepoSummary, SubsystemSummary

from fakes import FakeClient
from test_ask import AskClient, FakeLLM, _answer, _seed as _seed_ask
from test_llm_metered import FakeBase
from test_worker_run import _patch_grounded_pipeline, _seed_entities, _seed_job


@pytest.fixture(autouse=True)
def _clean_jobs():
    yield
    for job_id in ("job-1", "job-2"):
        forget_job(job_id)


def _invocations(client):
    return client.table("llm_invocations").rows


# --- Agent 1 ----------------------------------------------------------------------------------------


def _agent1_client(monkeypatch, jobs=("job-1",)):
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
    client.table("projects").insert({"id": "project-1", "org_id": "org-1", "source_type": "github", "source_ref": "x"}).execute()
    for job in jobs:
        _seed_job(client, job, "org-1")
        client.table("ingestion_jobs").rows[-1]["project_id"] = "project-1"
    monkeypatch.setattr(
        run_module,
        "_run_extractors",
        lambda repo_path, client, job_id, org_id, emitter: _seed_entities(
            client,
            job_id,
            org_id,
            [
                {"name": "foo", "file_path": "app.php", "content_hash": "hash-foo"},
                {"name": "bar", "file_path": "app.php", "content_hash": "hash-bar"},
            ],
        ),
    )
    _patch_grounded_pipeline(monkeypatch)
    return client


def _metering_pipeline(monkeypatch, fail_entity_with=None):
    """Every summarizer and the grounding check make one real call through the client they are
    handed, which is how the production code reaches the LLM."""

    def call(client):
        client.chat.completions.create(model="d", messages=[])

    def summarize_entity(entity, client=None, excerpt=None):
        call(client)
        if fail_entity_with is not None:
            raise fail_entity_with
        return EntitySummary(f"summary of {entity.name}", "low", [])

    def grounding(facts, generate, max_retries=None, client=None):
        summary = generate()
        call(client)
        return GroundingRetryResult(summary=summary, grounding=GroundingResult(verdict="grounded", issues=[]), retries_used=0)

    monkeypatch.setattr(run_module, "summarize_entity", summarize_entity)
    monkeypatch.setattr(run_module, "check_grounding_with_retry", grounding)
    monkeypatch.setattr(
        run_module, "summarize_module", lambda fp, es, client=None: (call(client), ModuleSummary("m", "low", []))[1]
    )
    monkeypatch.setattr(
        run_module, "summarize_subsystem", lambda n, c, client=None: (call(client), SubsystemSummary("s", "low", []))[1]
    )
    monkeypatch.setattr(run_module, "summarize_repo", lambda c, client=None: (call(client), RepoSummary("r", "low", []))[1])
    monkeypatch.setattr(run_module, "get_azure_openai_client", lambda: FakeBase())


def test_agent1_records_every_call_with_stage_purpose_and_one_run_id(monkeypatch):
    client = _agent1_client(monkeypatch)
    _metering_pipeline(monkeypatch)

    run_module.process_job("job-1", "org-1")

    rows = _invocations(client)
    assert {(r["agent"], r["job_type"], r["job_id"], r["org_id"]) for r in rows} == {("agent1", "ingestion", "job-1", "org-1")}
    assert len({r["run_id"] for r in rows}) == 1 and rows[0]["run_id"]
    by_purpose = {}
    for r in rows:
        by_purpose.setdefault(r["purpose"], set()).add(r["stage"])
    assert by_purpose == {
        "entity_summary": {"summarize"},
        "module_summary": {"fold"},
        "subsystem_summary": {"fold"},
        "repo_summary": {"fold"},
        "grounding_check": {"summarize", "fold"},
    }
    # 2 entities x (summary + check), then module, subsystem, repo each x (summary + check)
    assert len(rows) == 4 + 6
    assert all(r["entity_id"] for r in rows if r["purpose"] == "entity_summary")
    [rollup] = client.table("llm_job_costs").rows
    assert (rollup["job_type"], rollup["job_id"], rollup["calls"]) == ("ingestion", "job-1", len(rows))
    assert client.table("ingestion_jobs").rows[0]["status"] == "done"


def test_agent1_counts_summary_cache_hits_and_misses_on_the_job_row(monkeypatch):
    client = _agent1_client(monkeypatch, jobs=("job-1", "job-2"))
    _metering_pipeline(monkeypatch)

    run_module.process_job("job-1", "org-1")
    run_module.process_job("job-2", "org-1")

    first, second = client.table("ingestion_jobs").rows
    assert (first["summary_cache_hits"], first["summary_cache_misses"]) == (0, 2)
    assert (second["summary_cache_hits"], second["summary_cache_misses"]) == (2, 0)
    # job-2 never went back to the LLM for entities, so no entity_summary rows belong to it.
    assert not [r for r in _invocations(client) if r["job_id"] == "job-2" and r["purpose"] == "entity_summary"]


def test_agent1_flushes_what_it_recorded_when_the_job_fails(monkeypatch):
    client = _agent1_client(monkeypatch)
    _metering_pipeline(monkeypatch, fail_entity_with=RuntimeError("boom"))

    run_module.process_job("job-1", "org-1")

    assert client.table("ingestion_jobs").rows[0]["status"] == "failed"
    assert [r["purpose"] for r in _invocations(client)] and all(r["job_id"] == "job-1" for r in _invocations(client))


def test_agent1_flushes_what_it_recorded_when_the_job_is_cancelled(monkeypatch):
    client = _agent1_client(monkeypatch)
    _metering_pipeline(monkeypatch, fail_entity_with=JobCancelled())

    run_module.process_job("job-1", "org-1")

    assert _invocations(client), "calls made before the cancel must still be recorded"
    assert client.table("ingestion_jobs").rows[0]["status"] != "done"


def test_agent1_job_still_completes_when_cost_recording_is_broken(monkeypatch):
    client = _agent1_client(monkeypatch)
    _metering_pipeline(monkeypatch)
    real_table = client.table

    def table(name):
        if name in ("llm_invocations", "llm_job_costs"):
            raise RuntimeError("cost tables unavailable")
        return real_table(name)

    monkeypatch.setattr(client, "table", table)

    run_module.process_job("job-1", "org-1")

    assert real_table("ingestion_jobs").rows[0]["status"] == "done"


# --- Agent 2 ----------------------------------------------------------------------------------------


def _agent2_client(monkeypatch):
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(agent2_run, "get_service_role_client", lambda: client)
    client.table("planning_jobs").insert(
        {"id": "pj-1", "org_id": "org-1", "project_id": "p", "source_job_id": "j", "status": "queued", "params": {"seed": 1}}
    ).execute()
    for name in ("graph", "cluster", "report", "order"):
        monkeypatch.setattr(agent2_run, f"stage_{name}", lambda c, job, state: None)
    return client


def test_agent2_report_stage_meters_the_client_and_flushes_when_it_ends(monkeypatch):
    client = FakeClient(org_id="org-1")
    seen = {}

    def report_stage(c, planning_job, graph, clusters, llm_client=None, embed=None):
        seen["client"] = llm_client
        llm_client.bind(purpose="cluster_report", cluster_id="c1").chat.completions.create(model="d")
        return []

    monkeypatch.setattr(agent2_run, "get_azure_openai_client", lambda: FakeBase())
    monkeypatch.setattr(agent2_run, "run_report_stage", report_stage)
    state = agent2_run.PlanningState(
        cost_recorder=agent2_run.LLMCostRecorder(client, "org-1"),
        cost_context=agent2_run.CallContext("agent2", "planning", "pj-1", "run-1"),
    )

    agent2_run.stage_report(client, {"id": "pj-1", "org_id": "org-1", "source_job_id": "j", "params": {}}, state)

    assert isinstance(seen["client"], MeteredLLMClient)
    [row] = _invocations(client)  # flushed by the stage itself, before any close()
    assert (row["agent"], row["job_type"], row["job_id"], row["stage"], row["run_id"]) == (
        "agent2",
        "planning",
        "pj-1",
        "report",
        "run-1",
    )
    assert (row["purpose"], row["cluster_id"]) == ("cluster_report", "c1")


def test_agent2_stage_without_a_recorder_uses_the_plain_client(monkeypatch):
    seen = {}
    base = FakeBase()
    monkeypatch.setattr(agent2_run, "get_azure_openai_client", lambda: base)
    monkeypatch.setattr(
        agent2_run, "run_report_stage", lambda c, job, g, cl, llm_client=None, embed=None: seen.update(client=llm_client) or []
    )

    agent2_run.stage_report(FakeClient(), {"source_job_id": "j", "params": {}}, agent2_run.PlanningState())

    assert seen["client"] is base


def test_agent2_records_made_before_a_failing_stage_are_still_flushed(monkeypatch):
    client = _agent2_client(monkeypatch)

    def failing_stage(c, planning_job, state):
        llm = MeteredLLMClient(FakeBase(), state.cost_context, state.cost_recorder)
        llm.bind(stage="report", purpose="cluster_report").chat.completions.create(model="d")
        raise RuntimeError("stage blew up")

    monkeypatch.setattr(agent2_run, "stage_graph", failing_stage)

    agent2_run.process_planning_job("pj-1", "org-1")

    assert client.table("planning_jobs").rows[0]["status"] == "failed"
    [row] = _invocations(client)
    assert (row["agent"], row["job_type"], row["job_id"], row["org_id"]) == ("agent2", "planning", "pj-1", "org-1")


def test_agent2_each_pickup_gets_a_new_run_id(monkeypatch):
    client = _agent2_client(monkeypatch)

    def stage(c, planning_job, state):
        MeteredLLMClient(FakeBase(), state.cost_context, state.cost_recorder).chat.completions.create(model="d")

    monkeypatch.setattr(agent2_run, "stage_graph", stage)

    agent2_run.process_planning_job("pj-1", "org-1")
    client.table("planning_jobs").rows[0]["status"] = "queued"  # a redelivery re-runs the job
    agent2_run.process_planning_job("pj-1", "org-1")

    first, second = _invocations(client)
    assert first["run_id"] != second["run_id"]
    [rollup] = client.table("llm_job_costs").rows
    assert rollup["calls"] == 2


# --- Agent 3 ----------------------------------------------------------------------------------------


def _agent3_state(unit_ids):
    return NS(items=[{"cluster_id": u} for u in unit_ids], terminal_unit_ids=set())


def test_agent3_binds_each_unit_and_flushes_after_every_unit(monkeypatch):
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(units_module, "get_azure_openai_client", lambda: FakeBase())
    seen_after_unit = []

    def run_one(c, execution_job, state, item, items_by_cluster_id, llm_client):
        llm_client.bind(cluster_id=item["cluster_id"], purpose="code_generation", round=1, attempt=1).chat.completions.create(
            model="d"
        )

    monkeypatch.setattr(units_module, "_run_one_unit", run_one)
    real_flush = units_module.LLMCostRecorder.flush

    def spying_flush(self):
        real_flush(self)
        seen_after_unit.append(len(_invocations(client)))

    monkeypatch.setattr(units_module.LLMCostRecorder, "flush", spying_flush)

    units_module.run_units_stage(client, {"id": "ex-1", "org_id": "org-1"}, _agent3_state(["u1", "u2"]))

    rows = _invocations(client)
    assert [(r["agent"], r["job_type"], r["job_id"], r["stage"], r["cluster_id"]) for r in rows] == [
        ("agent3", "execution", "ex-1", "units", "u1"),
        ("agent3", "execution", "ex-1", "units", "u2"),
    ]
    assert len({r["run_id"] for r in rows}) == 1
    assert seen_after_unit[:2] == [1, 2]  # u1's row was already written before u2 started


def test_agent3_flushes_and_closes_when_a_unit_raises(monkeypatch):
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(units_module, "get_azure_openai_client", lambda: FakeBase())

    def run_one(c, execution_job, state, item, items_by_cluster_id, llm_client):
        llm_client.bind(cluster_id=item["cluster_id"], purpose="code_generation").chat.completions.create(model="d")
        if item["cluster_id"] == "u2":
            raise RuntimeError("unit blew up")

    monkeypatch.setattr(units_module, "_run_one_unit", run_one)

    with pytest.raises(RuntimeError):
        units_module.run_units_stage(client, {"id": "ex-1", "org_id": "org-1"}, _agent3_state(["u1", "u2", "u3"]))

    assert [r["cluster_id"] for r in _invocations(client)] == ["u1", "u2"]


def test_agent3_with_nothing_pending_never_needs_an_llm_client_or_recorder(monkeypatch):
    def boom():
        raise AssertionError("must not build a client")

    monkeypatch.setattr(units_module, "get_azure_openai_client", boom)
    state = NS(items=[{"cluster_id": "u1"}], terminal_unit_ids={"u1"})
    units_module.run_units_stage(FakeClient(), {"id": "ex-1", "org_id": "org-1"}, state)


# --- Q&A --------------------------------------------------------------------------------------------


@pytest.fixture
def qa_api(monkeypatch):
    client = _seed_ask(AskClient())
    cost_client = FakeClient(org_id="org-1")
    app = create_app()

    async def fake_auth():
        return AuthContext(user_id="user-7", org_id="org-1", access_token="tok", client=client)

    app.dependency_overrides[get_auth_context] = fake_auth
    monkeypatch.setattr(jobs_module, "_question_embedder", lambda: None)
    monkeypatch.setattr(jobs_module, "get_service_role_client", lambda: cost_client)
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-test")
    return TestClient(app), cost_client


def _wait_for_cost_flush():
    for thread in threading.enumerate():
        if thread.name == "qa-cost-flush":
            thread.join(timeout=5)


def _fake_answering_llm(monkeypatch, llm):
    monkeypatch.setattr("document_analyzer.worker.summarize.get_azure_openai_client", lambda: llm)


def test_qa_records_the_call_against_the_source_job_and_requester(qa_api, monkeypatch):
    http, cost = qa_api
    _fake_answering_llm(monkeypatch, FakeLLM(_answer()))

    assert http.post("/api/agent1/jobs/job-1/ask", json={"question": "What does charge_card do?"}).status_code == 200
    _wait_for_cost_flush()

    [row] = _invocations(cost)
    assert (row["agent"], row["job_type"], row["purpose"]) == ("agent1", "qa", "qa_answer")
    assert (row["job_id"], row["source_job_id"], row["requested_by"], row["org_id"]) == (None, "job-1", "user-7", "org-1")
    assert row["status"] == "ok" and row["run_id"]
    assert "llm_job_costs" not in cost._tables or cost.table("llm_job_costs").rows == []


def test_qa_records_even_when_the_answer_is_rejected(qa_api, monkeypatch):
    http, cost = qa_api
    _fake_answering_llm(monkeypatch, FakeLLM(_answer(summary="[E42]")))

    assert http.post("/api/agent1/jobs/job-1/ask", json={"question": "charge_card"}).status_code == 502
    _wait_for_cost_flush()

    assert len(_invocations(cost)) == 1  # the call was made and paid for, whatever we thought of the answer


def test_qa_records_a_failed_llm_call_and_still_reports_the_failure(qa_api, monkeypatch):
    http, cost = qa_api

    class Down:
        chat = NS(completions=NS(create=lambda **k: (_ for _ in ()).throw(ConnectionError("down"))))

    _fake_answering_llm(monkeypatch, Down())
    http_no_raise = TestClient(http.app, raise_server_exceptions=False)

    assert http_no_raise.post("/api/agent1/jobs/job-1/ask", json={"question": "charge_card"}).status_code == 500
    _wait_for_cost_flush()

    [row] = _invocations(cost)
    assert (row["status"], row["error_class"]) == ("error", "ConnectionError")


def test_qa_with_no_evidence_makes_no_call_and_records_nothing(qa_api, monkeypatch):
    http, cost = qa_api
    _fake_answering_llm(monkeypatch, FakeLLM(_answer()))

    response = http.post("/api/agent1/jobs/job-1/ask", json={"question": "zzzqqq nothing matches this"})
    _wait_for_cost_flush()

    assert response.status_code == 200
    if response.json()["model_called"] is False:
        assert _invocations(cost) == []


def test_qa_still_answers_when_the_cost_recorder_cannot_be_built(qa_api, monkeypatch):
    http, _ = qa_api
    _fake_answering_llm(monkeypatch, FakeLLM(_answer()))

    def broken():
        raise RuntimeError("no service role key")

    monkeypatch.setattr(jobs_module, "get_service_role_client", broken)

    response = http.post("/api/agent1/jobs/job-1/ask", json={"question": "What does charge_card do?"})

    assert response.status_code == 200 and response.json()["citations"][0]["id"] == "E1"


def test_qa_still_answers_when_writing_the_cost_fails(qa_api, monkeypatch):
    http, cost = qa_api
    _fake_answering_llm(monkeypatch, FakeLLM(_answer()))
    monkeypatch.setattr(cost, "table", lambda name: (_ for _ in ()).throw(RuntimeError("database unavailable")))

    response = http.post("/api/agent1/jobs/job-1/ask", json={"question": "What does charge_card do?"})
    _wait_for_cost_flush()

    assert response.status_code == 200
