"""Full Assessment pinning — design_report.md §5.8, §9.1, §10.

A Full Assessment is pinned to one ingestion job and one plan. A plan built on a different job is
refused with a 409 at the API (and again in `collect` and the worker). How the watermark renders is tested in test_report_render.py. A plan that isn't approved
yet is allowed, but the report carries the draft watermark and the §5.8 limitation."""

import pytest

from document_analyzer.report.build import build_report_json
from document_analyzer.report.collect import ReportInputError
from tests.test_report_api import (  # noqa: F401  (fixtures)
    JOB,
    NEW_JOB,
    PROJECT,
    _jobs,
    _seed,
    client,
    enqueued,
    fake_client,
)
from tests.test_report_build import COMMON, ESTIMATE
from tests.test_report_collect import ORG, PLAN, _plan_rows
from tests.test_report_collect import client as collect_client  # noqa: F401  (fixture)


def _move_to_new_ingestion(fake_client):
    """The project was re-ingested: its latest done job is NEW_JOB, and the plan is still on JOB."""
    fake_client.table("ingestion_jobs").insert({"id": NEW_JOB, "org_id": "org-123", "status": "done"}).execute()
    fake_client.table("projects").rows[0]["latest_job_id"] = NEW_JOB


# --- the API refuses a plan pinned to another job -------------------------------------------------------


@pytest.mark.parametrize("status", ["draft", "in_review", "approved"])
def test_a_plan_pinned_to_an_older_job_is_a_409_and_nothing_is_queued(client, fake_client, enqueued, status):
    _seed(fake_client, plans=(("plan-1", status, 1),))
    _move_to_new_ingestion(fake_client)

    response = client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "full"})

    assert response.status_code == 409 and "plan" in response.json()["detail"]
    assert _jobs(fake_client) == [] and enqueued == []


def test_the_x_ray_is_still_allowed_when_the_only_plan_is_on_an_older_job(client, fake_client):
    _seed(fake_client)
    _move_to_new_ingestion(fake_client)

    assert client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "xray"}).status_code == 202


def test_the_plan_for_the_latest_job_is_pinned_not_the_one_for_an_older_job(client, fake_client):
    _seed(fake_client, plans=(("plan-old", "approved", 5),))
    _move_to_new_ingestion(fake_client)
    fake_client.table("migration_plans").insert(
        {"id": "plan-new", "org_id": "org-123", "project_id": PROJECT, "source_job_id": NEW_JOB, "status": "draft", "version": 1}
    ).execute()

    assert client.post(f"/api/projects/{PROJECT}/reports", json={"tier": "full"}).status_code == 202

    [job] = _jobs(fake_client)
    assert (job["plan_id"], job["plan_version"], job["source_job_id"]) == ("plan-new", 1, NEW_JOB)


def test_the_eligibility_block_says_why_a_full_report_is_blocked(client, fake_client):
    _seed(fake_client)
    _move_to_new_ingestion(fake_client)

    eligibility = client.get(f"/api/projects/{PROJECT}/reports").json()["eligibility"]

    assert eligibility["xray"]["eligible"] is True
    assert eligibility["full"]["eligible"] is False and "plan" in eligibility["full"]["reason"]


# --- the build refuses it too (the API check is repeated where the data is read) ------------------------


def test_the_build_refuses_a_plan_pinned_to_another_job(collect_client):
    _plan_rows(collect_client, source="job-other")

    with pytest.raises(ReportInputError):
        build_report_json(collect_client, ORG, tier="full", plan_id=PLAN, estimate=ESTIMATE, **COMMON)


# --- a draft plan is allowed, but marked ---------------------------------------------------------------


@pytest.mark.parametrize("status", ["draft", "in_review"])
def test_an_unapproved_plan_carries_the_watermark_and_the_5_8_entry(collect_client, status):
    _plan_rows(collect_client, status=status)

    report = build_report_json(collect_client, ORG, tier="full", plan_id=PLAN, estimate=ESTIMATE, **COMMON)

    assert report["cover"]["watermark"] == "draft_plan"
    assert report["inputs"]["plan_status"] == status
    [entry] = [item for item in report["limitations"] if item["code"] == "plan_not_approved"]
    assert status in entry["text"] and "draft" in entry["text"]


def test_an_approved_plan_has_neither(collect_client):
    _plan_rows(collect_client, status="approved")

    report = build_report_json(collect_client, ORG, tier="full", plan_id=PLAN, estimate=ESTIMATE, **COMMON)

    assert report["cover"]["watermark"] is None
    assert "plan_not_approved" not in {item["code"] for item in report["limitations"]}
