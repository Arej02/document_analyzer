"""Reporter worker (report/worker/run.py) — design_report.md §4: stages, redelivery, cancellation.

Runs the real stages over the report fixture project (tests/fixtures/report_project) with a
fake LLM, so the report that gets published is the real builder's output."""

import io
import json
from pathlib import Path

import pytest

from pypdf import PdfWriter

from document_analyzer.report import exec_summary
from document_analyzer.report import pdf as pdf_module
from document_analyzer.report.pdf import PdfRenderError, PdfUnavailable
from document_analyzer.report.worker import artifacts
from document_analyzer.report.worker import run as run_module
from document_analyzer.report.worker.status import STAGES, mark_report_done, mark_report_stage
from tests.fakes import FakeClient

FIXTURE = Path(__file__).parent / "fixtures" / "report_project" / "project.json"
ORG, JOB, PLAN, PROJECT, REPORT_JOB = "org-1", "job-1", "plan-1", "project-1", "rj-1"
AWS = "AKIAABCDEFGHIJKLMNOP"

CONFIG = {
    "version": "1",
    "buckets": {"size_edges": [3, 8, 20], "unknown_risk": "medium"},
    "defaults": {"fallback": {"cost_usd": 4.0, "attempts": 2, "duration_s": 120.0, "failure_rate": 0.25}},
    "calibration": {"min_samples": 5, "unpriced_threshold": 0.2},
    "simulation": {"seed": 1, "runs": 100, "concurrency": 2},
    "rework": {"hours_per_failed_unit": {"low": 2, "medium": 4, "high": 8}},
    "manual_effort": {"per_entity": {"function": 0.1, "class": 0.3}, "risk_multiplier": {"low": 1, "medium": 1.5, "high": 2.5}},
    "quality": {"sparse_calls_per_function": 0.5},
    "pricing": {"currency": "USD", "method": "markup", "markup": 2.0, "rounding": 100, "minimum": 500, "version": "p1"},
}


class FakeLLM:
    """Routes by system prompt like test_report_exec_summary: writer vs grounding checker."""

    def __init__(self, summaries=("A small PHP shop.",), verdicts=(True,), fail=False):
        self.summaries, self.verdicts, self.fail = list(summaries), list(verdicts), fail
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        if self.fail:
            raise RuntimeError("model down")
        if kwargs["messages"][0]["content"] == exec_summary.INSTRUCTIONS:
            content = json.dumps({"summary": self.summaries.pop(0)})
        else:
            content = json.dumps({"grounded": self.verdicts.pop(0), "issues": []})
        message = type("M", (), {"content": content})()
        return type("R", (), {"choices": [type("C", (), {"message": message})()], "usage": None, "model": None})()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-test")


def blank_pdf() -> bytes:
    """A real, text-free PDF: valid for pypdf, so the text-extraction gate has something to read."""
    writer, buffer = PdfWriter(), io.BytesIO()
    writer.add_blank_page(width=200, height=200)
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def fake_pdf(monkeypatch):
    """The real container is covered by tests/test_report_pdf.py; here the render is a fake."""
    calls = []

    def render(html, report_id, report_date, params=None):
        calls.append({"html": html, "report_id": report_id, "report_date": report_date, "params": params})
        return blank_pdf()

    monkeypatch.setattr(pdf_module, "render_pdf", render)
    return calls


@pytest.fixture
def client(monkeypatch):
    client = FakeClient(ORG)
    for table, rows in json.loads(FIXTURE.read_text(encoding="utf-8")).items():
        client.table(table).rows.extend(rows)
    [source] = client.table("ingestion_jobs").rows
    source.update(status="done", updated_at="2026-09-30T10:00:00Z")
    client.table("projects").insert({"id": PROJECT, "org_id": ORG, "source_ref": "https://github.com/acme/legacy-shop.git"}).execute()
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
    monkeypatch.setattr(run_module, "load_report_config", lambda: (CONFIG, "hash-1"))
    monkeypatch.setattr(run_module, "get_azure_openai_client", lambda: FakeLLM())
    return client


def _seed_job(client, tier="full", job_id=REPORT_JOB, org_id=ORG, status="queued"):
    client.table("report_generation_jobs").insert(
        {
            "id": job_id,
            "org_id": org_id,
            "project_id": PROJECT,
            "tier": tier,
            "source_job_id": JOB,
            "plan_id": PLAN if tier == "full" else None,
            "plan_version": 3 if tier == "full" else None,
            "status": status,
            "progress_stage": None,
            "params": {},
            "created_by": "user-1",
        }
    ).execute()


def _keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from _keys(value)


def _job(client):
    [row] = client.table("report_generation_jobs").rows
    return row


def _events(client):
    return [(e["event_type"], e["stage"]) for e in client.table("workflow_events").rows]


# --- happy paths ---------------------------------------------------------------------


def test_full_report_end_to_end(client):
    _seed_job(client)
    run_module.process_report_job(REPORT_JOB, ORG)

    job = _job(client)
    assert job["status"] == "done" and job["progress_stage"] is None and not job.get("error")
    [row] = client.table("assessment_reports").rows
    assert row["org_id"] == ORG and row["generation_job_id"] == REPORT_JOB and row["created_by"] == "user-1"
    assert (row["tier"], row["plan_id"], row["plan_version"], row["plan_status_at_generation"]) == ("full", PLAN, 3, "approved")
    assert row["exec_summary_status"] == "verified" and row["pdf_status"] == "done"
    assert row["html_path"] == f"{ORG}/{row['id']}/report.html" and row["pdf_path"] == f"{ORG}/{row['id']}/report.pdf"
    report = row["report_json"]
    assert report["cover"]["project_name"] == "legacy-shop" and report["cover"]["snapshot_date"] == "2026-09-30"
    assert report["executive_summary"]["text"] == "A small PHP shop."
    assert report["inputs"]["estimator_config_hash"] == "hash-1" and report["inputs"]["pricing_version"] == "p1"
    assert report["estimate"]["manual_effort"]["label"] == "Heuristic" and "quote" in report["estimate"]
    assert not [k for k in _keys(report) if "cost" in k.lower() or "token" in k.lower()]  # no internal cost in the report

    [internal] = client.table("report_internal_estimates").rows
    assert internal["org_id"] == ORG and internal["report_id"] == row["id"]
    assert internal["price_basis"]["method"] == "markup" and internal["n"] == 0
    assert internal["ai_cost_p50"] is None  # no history: nothing priced from calibration


def test_stages_emit_events_and_progress_in_order(client):
    _seed_job(client)
    seen = []
    original = run_module.mark_report_stage

    def spy(c, job_id, org_id, stage):
        original(c, job_id, org_id, stage)
        seen.append((stage, _job(c)["status"]))

    run_module.mark_report_stage = spy
    try:
        run_module.process_report_job(REPORT_JOB, ORG)
    finally:
        run_module.mark_report_stage = original
    assert seen == [(s, "running") for s in STAGES]
    events = client.table("workflow_events").rows
    assert {e["workflow_kind"] for e in events} == {"report_generation"}
    assert all(e["org_id"] == ORG and e["workflow_id"] == REPORT_JOB for e in events)
    types = [e["event_type"] for e in events]
    assert types[0] == "workflow.started" and types[-1] == "workflow.completed"
    assert [e["stage"] for e in events if e["event_type"] == "stage.started"] == list(STAGES)
    assert "cost" not in json.dumps(events).lower()  # report events carry no cost


def test_xray_has_no_plan_estimate_or_internal_row(client):
    _seed_job(client, tier="xray")
    run_module.process_report_job(REPORT_JOB, ORG)
    assert _job(client)["status"] == "done"
    [row] = client.table("assessment_reports").rows
    assert row["tier"] == "xray" and row["plan_id"] is None and row["plan_status_at_generation"] is None
    assert "plan" not in row["report_json"] and "estimate" not in row["report_json"]
    assert client.table("report_internal_estimates").rows == []


def test_llm_cost_rows_are_recorded_for_the_report(client):
    _seed_job(client, tier="xray")
    run_module.process_report_job(REPORT_JOB, ORG)
    rows = client.table("llm_invocations").rows
    assert rows and {(r["agent"], r["job_type"], r["job_id"]) for r in rows} == {("report", "report", REPORT_JOB)}
    assert {r["purpose"] for r in rows} == {"executive_summary", "grounding_check"}


# --- summarize never fails the job -------------------------------------------------------


def test_model_failure_leaves_a_withheld_summary_and_a_done_job(client, monkeypatch):
    monkeypatch.setattr(run_module, "get_azure_openai_client", lambda: FakeLLM(fail=True))
    _seed_job(client, tier="xray")
    run_module.process_report_job(REPORT_JOB, ORG)
    assert _job(client)["status"] == "done"
    [row] = client.table("assessment_reports").rows
    assert row["exec_summary_status"] == "withheld"
    assert any(l["code"] == "executive_summary_withheld" for l in row["report_json"]["limitations"])


def test_ungrounded_summary_is_published_unverified(client, monkeypatch):
    llm = FakeLLM(summaries=["One.", "Two.", "Three."], verdicts=[False, False, False])
    monkeypatch.setattr(run_module, "get_azure_openai_client", lambda: llm)
    _seed_job(client, tier="xray")
    run_module.process_report_job(REPORT_JOB, ORG)
    [row] = client.table("assessment_reports").rows
    assert row["exec_summary_status"] == "unverified" and _job(client)["status"] == "done"


# --- render stages ----------------------------------------------------------------------


def _bucket(client):
    return client.storage.from_(artifacts.BUCKET)


@pytest.mark.parametrize("error", [PdfRenderError("chromium crashed"), PdfUnavailable("image missing"), RuntimeError("odd")])
def test_pdf_failure_leaves_the_report_done_with_its_html(client, monkeypatch, error):
    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(pdf_module, "render_pdf", boom)
    _seed_job(client)
    run_module.process_report_job(REPORT_JOB, ORG)
    assert _job(client)["status"] == "done"
    [row] = client.table("assessment_reports").rows
    assert row["pdf_status"] == "failed" and row["pdf_path"] is None
    assert row["html_path"] and _bucket(client)._objects.keys() == {row["html_path"]}  # the HTML is there, no PDF
    assert any(e["event_type"] == "activity" and "PDF could not be generated" in (e["message"] or "") for e in client.table("workflow_events").rows)


def test_html_artifact_is_the_gated_report_rendered_and_uploaded(client):
    _seed_job(client)
    run_module.process_report_job(REPORT_JOB, ORG)
    [row] = client.table("assessment_reports").rows
    stored = _bucket(client)._objects[row["html_path"]].decode("utf-8")
    assert stored == run_module.render_report_html(row["report_json"])  # exactly what the stored JSON renders to
    assert stored.startswith("<!doctype html>") and "<script" not in stored.lower()
    path, data, opts = _bucket(client).uploads[0]
    assert path == row["html_path"] and opts["content-type"] == "text/html; charset=utf-8" and opts["upsert"] == "true"
    assert row["template_version"] == run_module.TEMPLATE_VERSION


def test_pdf_is_rendered_from_the_html_with_the_header_values_and_frozen_params(client, fake_pdf):
    _seed_job(client)
    client.table("report_generation_jobs").rows[0]["params"] = {"renderer_image": "r:9", "pdf_timeout_seconds": 33, "pdf_memory_mb": 512}
    run_module.process_report_job(REPORT_JOB, ORG)
    [row] = client.table("assessment_reports").rows
    [call] = fake_pdf
    assert call["html"] == _bucket(client)._objects[row["html_path"]].decode("utf-8")
    assert call["report_id"] == row["id"] and call["report_date"] == "2026-09-30"
    assert (call["params"].image, call["params"].timeout_seconds, call["params"].memory_mb) == ("r:9", 33, 512)
    assert _bucket(client)._objects[row["pdf_path"]].startswith(b"%PDF-")


def test_a_secret_added_by_the_template_fails_the_job_and_uploads_nothing(client, monkeypatch):
    real = run_module.render_report_html
    monkeypatch.setattr(run_module, "render_report_html", lambda report: real(report).replace("</body>", f"<p>{AWS}</p></body>"))
    _seed_job(client, tier="xray")
    run_module.process_report_job(REPORT_JOB, ORG)
    job = _job(client)
    assert job["status"] == "failed" and "rendered report failed the content gate" in job["error"] and AWS not in job["error"]
    assert client.table("assessment_reports").rows == [] and _bucket(client)._objects == {}


def test_a_pdf_whose_text_fails_the_gate_is_not_uploaded(client, monkeypatch):
    monkeypatch.setattr(artifacts, "pdf_text", lambda pdf: f"Page text with {AWS} in it")
    _seed_job(client, tier="xray")
    run_module.process_report_job(REPORT_JOB, ORG)
    assert _job(client)["status"] == "done"
    [row] = client.table("assessment_reports").rows
    assert row["pdf_status"] == "failed" and row["pdf_path"] is None
    assert list(_bucket(client)._objects) == [row["html_path"]]
    events = json.dumps(client.table("workflow_events").rows)
    assert "failed the content gate" in events and AWS not in events


def test_secret_outside_free_text_fails_the_job_and_publishes_nothing(client):
    client.table("extracted_entities").rows[0]["file_path"] = f"config/{AWS}.php"
    _seed_job(client, tier="xray")
    run_module.process_report_job(REPORT_JOB, ORG)
    job = _job(client)
    assert job["status"] == "failed" and "aws_access_key" in job["error"] and AWS not in job["error"]
    assert client.table("assessment_reports").rows == []
    assert AWS not in json.dumps(client.table("workflow_events").rows)


def test_free_text_hit_is_withheld_not_failed(client, monkeypatch):
    monkeypatch.setattr(run_module, "get_azure_openai_client", lambda: FakeLLM(summaries=["Uses key " + AWS + " here."]))
    _seed_job(client, tier="xray")
    run_module.process_report_job(REPORT_JOB, ORG)
    assert _job(client)["status"] == "done"
    [row] = client.table("assessment_reports").rows
    assert row["exec_summary_status"] == "withheld" and AWS not in json.dumps(row["report_json"])


# --- input checks --------------------------------------------------------------------------


def test_source_job_not_done_fails(client):
    client.table("ingestion_jobs").rows[0]["status"] = "running"
    _seed_job(client, tier="xray")
    run_module.process_report_job(REPORT_JOB, ORG)
    assert _job(client)["status"] == "failed" and "not done" in _job(client)["error"]


def test_plan_pinned_to_another_job_fails(client):
    client.table("migration_plans").rows[0]["source_job_id"] = "job-other"
    _seed_job(client)
    run_module.process_report_job(REPORT_JOB, ORG)
    assert _job(client)["status"] == "failed" and client.table("assessment_reports").rows == []


def test_plan_version_changed_since_request_fails(client):
    client.table("migration_plans").rows[0]["version"] = 4
    _seed_job(client)
    run_module.process_report_job(REPORT_JOB, ORG)
    assert _job(client)["status"] == "failed" and "changed" in _job(client)["error"]


def test_missing_config_fails_with_a_readable_error(client, monkeypatch):
    def missing():
        raise RuntimeError("report/config.py is not available")

    monkeypatch.setattr(run_module, "load_report_config", missing)
    _seed_job(client, tier="xray")
    run_module.process_report_job(REPORT_JOB, ORG)
    assert _job(client)["status"] == "failed" and "config" in _job(client)["error"]


# --- redelivery, cancellation, tenancy ----------------------------------------------------------


def test_redelivery_after_publish_does_not_duplicate_the_report(client):
    _seed_job(client)
    run_module.process_report_job(REPORT_JOB, ORG)
    [first] = client.table("assessment_reports").rows
    first_id = first["id"]

    # A crash after publish but before the job's terminal write: the message comes back.
    _job(client).update(status="running", progress_stage="publish")
    run_module.process_report_job(REPORT_JOB, ORG)

    [again] = client.table("assessment_reports").rows
    assert again["id"] == first_id and _job(client)["status"] == "done"
    [internal] = client.table("report_internal_estimates").rows
    assert internal["report_id"] == first_id


def test_redelivery_overwrites_the_same_artifact_paths(client):
    _seed_job(client, tier="xray")
    run_module.process_report_job(REPORT_JOB, ORG)
    [row] = client.table("assessment_reports").rows
    paths = set(_bucket(client)._objects)
    _job(client).update(status="running", progress_stage="publish")
    run_module.process_report_job(REPORT_JOB, ORG)
    assert set(_bucket(client)._objects) == paths == {row["html_path"], row["pdf_path"]}
    assert len(_bucket(client).uploads) == 4  # each uploaded twice, never to a second path
    assert client.table("assessment_reports").rows[0]["id"] == row["id"]


def test_finished_job_is_not_rerun(client):
    _seed_job(client, tier="xray", status="done")
    run_module.process_report_job(REPORT_JOB, ORG)
    assert client.table("assessment_reports").rows == [] and client.table("workflow_events").rows == []


def test_cancelled_before_start_is_skipped(client):
    _seed_job(client, tier="xray", status="cancelled")
    run_module.process_report_job(REPORT_JOB, ORG)
    assert _job(client)["status"] == "cancelled" and client.table("assessment_reports").rows == []


def test_cancel_during_run_stops_at_the_next_boundary(client, monkeypatch):
    original = run_module.stage_estimate

    def cancelling(c, job, state):
        original(c, job, state)
        _job(c)["status"] = "cancelled"  # the API cancels while this stage runs

    monkeypatch.setattr(run_module, "stage_estimate", cancelling)
    _seed_job(client)
    run_module.process_report_job(REPORT_JOB, ORG)

    assert _job(client)["status"] == "cancelled" and not _job(client).get("error")
    assert client.table("assessment_reports").rows == []
    types = [e["event_type"] for e in client.table("workflow_events").rows]
    assert types[-1] == "workflow.cancelled" and "workflow.completed" not in types and "workflow.failed" not in types
    assert ("stage.started", "summarize") not in _events(client)


def test_other_orgs_job_is_never_touched(client):
    _seed_job(client, tier="xray", org_id="org-2")
    run_module.process_report_job(REPORT_JOB, ORG)  # right id, wrong org
    assert _job(client)["status"] == "queued" and client.table("assessment_reports").rows == []


def test_status_writes_filter_by_org(client):
    _seed_job(client, tier="xray", org_id="org-2")
    mark_report_stage(client, REPORT_JOB, ORG, "collect")
    mark_report_done(client, REPORT_JOB, ORG)
    assert _job(client)["status"] == "queued" and _job(client)["progress_stage"] is None


# --- loop ---------------------------------------------------------------------------------------


def test_run_once_deletes_the_message_even_when_processing_raises(monkeypatch):
    deleted = []

    async def read():
        return {"msg_id": 7, "report_job_id": REPORT_JOB, "org_id": ORG}

    async def delete(msg_id):
        deleted.append(msg_id)

    def explode(*_):
        raise RuntimeError("boom")

    monkeypatch.setattr(run_module, "read_next_report_job", read)
    monkeypatch.setattr(run_module, "delete_report_job_message", delete)
    monkeypatch.setattr(run_module, "process_report_job", explode)
    with pytest.raises(RuntimeError):
        run_module.run_once()
    assert deleted == [7]


def test_run_once_with_empty_queue(monkeypatch):
    async def read():
        return None

    monkeypatch.setattr(run_module, "read_next_report_job", read)
    assert run_module.run_once() is False


# --- the PDF-only retry (design_report.md section 9.2) ----------------------------------------------------------


def _published(client, tier="xray"):
    _seed_job(client, tier=tier)
    run_module.process_report_job(REPORT_JOB, ORG)
    [row] = client.table("assessment_reports").rows
    return row


def test_pdf_retry_renders_the_stored_html_and_updates_only_the_pdf_fields(client, monkeypatch, fake_pdf):
    row = _published(client)
    html = _bucket(client)._objects[row["html_path"]]
    # The first attempt failed; the API set it back to pending and queued the retry.
    row.update(pdf_status="pending", pdf_path=None)
    del _bucket(client)._objects[artifacts.pdf_path(ORG, row["id"])]
    fake_pdf.clear()
    job_before = dict(_job(client))
    events_before = len(client.table("workflow_events").rows)

    run_module.process_pdf_retry(REPORT_JOB, ORG)

    row = client.table("assessment_reports").rows[0]
    assert (row["pdf_status"], row["pdf_path"]) == ("done", f"{ORG}/{row['id']}/report.pdf")
    assert _bucket(client)._objects[row["pdf_path"]].startswith(b"%PDF-")
    [call] = fake_pdf
    assert call["html"].encode("utf-8") == html and call["report_id"] == row["id"] and call["report_date"] == "2026-09-30"
    assert _job(client) == job_before and len(client.table("workflow_events").rows) == events_before  # job untouched


def test_pdf_retry_that_fails_again_ends_failed(client, monkeypatch):
    row = _published(client)
    row.update(pdf_status="pending", pdf_path=None)

    def boom(*args, **kwargs):
        raise PdfRenderError("still broken")

    monkeypatch.setattr(pdf_module, "render_pdf", boom)
    run_module.process_pdf_retry(REPORT_JOB, ORG)
    assert (row["pdf_status"], row["pdf_path"]) == ("failed", None)


def test_pdf_retry_gate_hit_ends_failed_and_uploads_nothing_new(client, monkeypatch):
    row = _published(client)
    row.update(pdf_status="pending", pdf_path=None)
    del _bucket(client)._objects[artifacts.pdf_path(ORG, row["id"])]
    monkeypatch.setattr(artifacts, "pdf_text", lambda pdf: f"leaked {AWS}")
    run_module.process_pdf_retry(REPORT_JOB, ORG)
    assert (row["pdf_status"], row["pdf_path"]) == ("failed", None)
    assert artifacts.pdf_path(ORG, row["id"]) not in _bucket(client)._objects


def test_pdf_retry_without_stored_html_or_report_is_dropped_without_error(client):
    run_module.process_pdf_retry("missing-job", ORG)  # nothing to retry: a no-op
    row = _published(client)
    row["html_path"] = None
    row.update(pdf_status="pending")
    run_module.process_pdf_retry(REPORT_JOB, ORG)
    assert row["pdf_status"] == "pending"  # untouched: the API refuses this case before queueing


def test_pdf_retry_for_another_org_changes_nothing(client):
    row = _published(client)
    row.update(pdf_status="pending", pdf_path=None)
    run_module.process_pdf_retry(REPORT_JOB, "org-999")
    assert row["pdf_status"] == "pending" and row["pdf_path"] is None


def test_a_storage_error_during_retry_marks_the_pdf_failed_not_the_job(client, monkeypatch):
    row = _published(client)
    row.update(pdf_status="pending", pdf_path=None)
    monkeypatch.setattr(artifacts, "download", lambda c, path: (_ for _ in ()).throw(RuntimeError("storage down")))
    run_module.process_pdf_retry(REPORT_JOB, ORG)
    assert row["pdf_status"] == "failed" and _job(client)["status"] == "done"


def test_run_once_routes_a_pdf_retry_message_to_the_pdf_only_path(monkeypatch):
    seen = []

    async def read():
        return {"msg_id": 9, "report_job_id": REPORT_JOB, "org_id": ORG, "stage": "render_pdf"}

    async def delete(msg_id):
        seen.append(("deleted", msg_id))

    monkeypatch.setattr(run_module, "read_next_report_job", read)
    monkeypatch.setattr(run_module, "delete_report_job_message", delete)
    monkeypatch.setattr(run_module, "process_pdf_retry", lambda job, org: seen.append(("pdf", job, org)))
    monkeypatch.setattr(run_module, "process_report_job", lambda job, org: seen.append(("job", job, org)))
    assert run_module.run_once() is True
    assert seen == [("pdf", REPORT_JOB, ORG), ("deleted", 9)]


def test_run_once_without_a_stage_runs_the_whole_job(monkeypatch):
    seen = []

    async def read():
        return {"msg_id": 3, "report_job_id": REPORT_JOB, "org_id": ORG, "stage": None}

    async def delete(msg_id):
        pass

    monkeypatch.setattr(run_module, "read_next_report_job", read)
    monkeypatch.setattr(run_module, "delete_report_job_message", delete)
    monkeypatch.setattr(run_module, "process_pdf_retry", lambda job, org: seen.append("pdf"))
    monkeypatch.setattr(run_module, "process_report_job", lambda job, org: seen.append("job"))
    run_module.run_once()
    assert seen == ["job"]
