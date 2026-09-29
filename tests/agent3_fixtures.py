"""Shared fixture builders for Agent 3 worker tests: a real seeded plan plus an
uploaded source snapshot, the same synthetic-DB-rows pattern
`test_agent3_prepare.py`/`test_agent3_units.py` use to exercise `run_prepare_stage`
for real without a `php` binary (`run_prepare_stage` never touches php either way).
Reused by `test_agent3_units.py`, `test_agent3_assemble.py` and
`test_agent3_worker_run.py` rather than duplicated in each.
"""

import io
import tarfile

from document_analyzer.supabase_client import insert_scoped

ORG = "org-1"


def make_tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def seed_plan(client, org_id: str = ORG) -> tuple[dict, dict, dict]:
    """Returns (job, plan, project): an approved plan over a "done" ingestion job
    with an uploaded (fake) source snapshot, ready for `run_prepare_stage`."""
    project = insert_scoped(client, "projects", org_id, {"source_type": "github", "source_ref": "https://x/y"}).data[0]
    job = insert_scoped(
        client,
        "ingestion_jobs",
        org_id,
        {"project_id": project["id"], "status": "done", "source_snapshot_path": f"{org_id}/job/source.tar.gz"},
    ).data[0]
    project["latest_job_id"] = job["id"]
    planning_job = insert_scoped(client, "planning_jobs", org_id, {"project_id": project["id"]}).data[0]
    plan = insert_scoped(
        client,
        "migration_plans",
        org_id,
        {
            "project_id": project["id"],
            "source_job_id": job["id"],
            "planning_job_id": planning_job["id"],
            "status": "approved",
            "version": 1,
        },
    ).data[0]
    client.storage.from_("agent1-snapshots").upload(job["source_snapshot_path"], make_tarball({"x.php": b"<?php\n"}))
    return job, plan, project
