"""Stage `prepare` — design_agent3.md §3.

FakeClient stands in for the worker's service-role client (same convention as
tests/test_agent3_worker_run.py and tests/test_agent2_pipeline_fixture.py).
"""

import io
import tarfile

import pytest

from document_analyzer.agent3.worker.prepare import PrepareError, run_prepare_stage, unpack_snapshot
from document_analyzer.agent3.worker.run import ExecutionState
from document_analyzer.supabase_client import insert_scoped

from fakes import FakeClient

ORG = "org-123"


def _make_tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _seed_base(fake_client, *, plan_status="approved", latest_is_source=True, snapshot_bytes=b"default"):
    project = insert_scoped(fake_client, "projects", ORG, {"source_type": "github", "source_ref": "https://x/y"}).data[0]
    job = insert_scoped(
        fake_client,
        "ingestion_jobs",
        ORG,
        {"project_id": project["id"], "status": "done", "source_snapshot_path": f"{ORG}/job/source.tar.gz"},
    ).data[0]
    project["latest_job_id"] = job["id"] if latest_is_source else "newer-job"
    planning_job = insert_scoped(fake_client, "planning_jobs", ORG, {"project_id": project["id"]}).data[0]
    plan = insert_scoped(
        fake_client,
        "migration_plans",
        ORG,
        {
            "project_id": project["id"],
            "source_job_id": job["id"],
            "planning_job_id": planning_job["id"],
            "status": plan_status,
            "version": 1,
        },
    ).data[0]

    tar_bytes = _make_tarball({"app.php": snapshot_bytes})
    fake_client.storage.from_("agent1-snapshots").upload(job["source_snapshot_path"], tar_bytes)

    return project, job, plan


def _add_unit(fake_client, plan, job, *, cluster_id, phase_order, unit_order, entity_id, merged_cluster_ids=None):
    insert_scoped(
        fake_client,
        "cluster_members",
        ORG,
        {"cluster_id": cluster_id, "entity_id": entity_id, "canonical_entity_id": entity_id},
    )
    insert_scoped(
        fake_client,
        "extracted_entities",
        ORG,
        {
            "id": entity_id,
            "job_id": job["id"],
            "entity_type": "function",
            "name": entity_id,
            "file_path": "app.php",
            "raw_facts": {"line": 1, "end_line": 5},
        },
    )
    insert_scoped(
        fake_client,
        "knowledge_summaries",
        ORG,
        {
            "job_id": job["id"],
            "level": "entity",
            "entity_id": entity_id,
            "summary_text": "s",
            "risk_level": "low",
            "grounding_status": "verified",
        },
    )
    insert_scoped(
        fake_client,
        "cluster_reports",
        ORG,
        {"cluster_id": cluster_id, "summary_text": f"report for {cluster_id}", "risk_level": "low", "grounding_status": "verified"},
    )
    insert_scoped(
        fake_client,
        "migration_plan_items",
        ORG,
        {
            "plan_id": plan["id"],
            "cluster_id": cluster_id,
            "phase_cluster_id": f"phase-{phase_order}",
            "phase_order": phase_order,
            "unit_order": unit_order,
            "test_contract": {"merged_cluster_ids": merged_cluster_ids or []},
        },
    )


def _execution_job(job, plan, execution_job_id="ej-1"):
    return {"id": execution_job_id, "org_id": ORG, "plan_id": plan["id"], "source_job_id": job["id"]}


def test_happy_path_loads_plan_items_members_and_snapshot():
    fake_client = FakeClient(org_id=ORG)
    project, job, plan = _seed_base(fake_client, snapshot_bytes=b"hello world")
    _add_unit(fake_client, plan, job, cluster_id="unit-b", phase_order=1, unit_order=2, entity_id="e-b")
    _add_unit(fake_client, plan, job, cluster_id="unit-a", phase_order=1, unit_order=1, entity_id="e-a")
    # An override-merged item: its own cluster has no members, but merged_cluster_ids pulls in unit-a's.
    insert_scoped(
        fake_client,
        "migration_plan_items",
        ORG,
        {
            "plan_id": plan["id"],
            "cluster_id": "unit-merged",
            "phase_cluster_id": "phase-2",
            "phase_order": 2,
            "unit_order": 1,
            "test_contract": {"merged_cluster_ids": ["unit-a"]},
        },
    )

    state = ExecutionState()
    run_prepare_stage(fake_client, _execution_job(job, plan), state)

    assert state.plan["id"] == plan["id"]
    # (phase_order, unit_order) ordering, regardless of insertion order.
    assert [item["cluster_id"] for item in state.items] == ["unit-a", "unit-b", "unit-merged"]

    by_id = {item["cluster_id"]: item for item in state.items}
    assert by_id["unit-a"]["member_entity_ids"] == ["e-a"]
    assert by_id["unit-b"]["member_entity_ids"] == ["e-b"]
    # The merged item's own cluster ("unit-merged") has no cluster_members row, so its
    # members come entirely from merged_cluster_ids.
    assert by_id["unit-merged"]["member_entity_ids"] == ["e-a"]

    assert set(state.entities) == {"e-a", "e-b"}
    assert state.entities["e-a"]["file_path"] == "app.php"
    assert set(state.summaries) == {"e-a", "e-b"}

    assert state.snapshot_dir is not None
    assert (state.snapshot_dir / "app.php").read_bytes() == b"hello world"

    # unit-a and unit-b each stand on their own report; the override-merged item
    # (design_agent2.md §9.2) has no report of its own, so it stands on unit-a's.
    assert by_id["unit-a"]["report_source_cluster_ids"] == ["unit-a"]
    assert by_id["unit-b"]["report_source_cluster_ids"] == ["unit-b"]
    assert by_id["unit-merged"]["report_source_cluster_ids"] == ["unit-a"]
    assert set(state.cluster_reports) == {"unit-a", "unit-b"}


def test_plan_not_approved_fails_readably():
    fake_client = FakeClient(org_id=ORG)
    _project, job, plan = _seed_base(fake_client, plan_status="draft")

    with pytest.raises(PrepareError, match="not.*approved|status=draft"):
        run_prepare_stage(fake_client, _execution_job(job, plan), ExecutionState())


def test_plan_stale_after_reingestion_fails_readably():
    fake_client = FakeClient(org_id=ORG)
    _project, job, plan = _seed_base(fake_client, latest_is_source=False)

    with pytest.raises(PrepareError, match="stale"):
        run_prepare_stage(fake_client, _execution_job(job, plan), ExecutionState())

    [row] = [r for r in fake_client.table("migration_plans").rows if r["id"] == plan["id"]]
    assert row["status"] == "stale"


def test_resume_skips_terminal_units_and_carries_symbol_map():
    fake_client = FakeClient(org_id=ORG)
    _project, job, plan = _seed_base(fake_client)
    _add_unit(fake_client, plan, job, cluster_id="unit-a", phase_order=1, unit_order=1, entity_id="e-a")
    _add_unit(fake_client, plan, job, cluster_id="unit-b", phase_order=1, unit_order=2, entity_id="e-b")
    execution_job = _execution_job(job, plan)
    insert_scoped(
        fake_client,
        "unit_executions",
        ORG,
        {
            "execution_job_id": execution_job["id"],
            "cluster_id": "unit-a",
            "status": "passed",
            "symbol_map": {"e-a": {"python_module": "app.units.a", "python_name": "a", "kind": "function"}},
        },
    )
    insert_scoped(
        fake_client,
        "unit_executions",
        ORG,
        {"execution_job_id": execution_job["id"], "cluster_id": "unit-b", "status": "pending", "symbol_map": None},
    )

    state = ExecutionState()
    run_prepare_stage(fake_client, execution_job, state)

    assert state.terminal_unit_ids == {"unit-a"}
    assert state.symbol_maps == {
        "unit-a": {"e-a": {"python_module": "app.units.a", "python_name": "a", "kind": "function"}}
    }


def test_unpack_rejects_path_traversal(tmp_path):
    tar_bytes = _make_tarball({"../../etc/passwd": b"pwned"})

    with pytest.raises(PrepareError):
        unpack_snapshot(tar_bytes, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_unpack_rejects_symlink_escape(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        info = tarfile.TarInfo(name="escape")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)

    with pytest.raises(PrepareError):
        unpack_snapshot(buf.getvalue(), tmp_path)


def test_unpack_rejects_oversized_snapshot(tmp_path, monkeypatch):
    from document_analyzer.agent3.worker import prepare as prepare_module

    monkeypatch.setattr(prepare_module, "MAX_UPLOAD_SIZE_BYTES", 10)
    tar_bytes = _make_tarball({"big.php": b"x" * 1000})

    with pytest.raises(PrepareError, match="exceeding max allowed"):
        unpack_snapshot(tar_bytes, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_process_execution_job_cleans_up_snapshot_dir(monkeypatch):
    from document_analyzer.agent3.worker import run as run_module

    fake_client = FakeClient(org_id=ORG)
    _project, job, plan = _seed_base(fake_client)
    _add_unit(fake_client, plan, job, cluster_id="unit-a", phase_order=1, unit_order=1, entity_id="e-a")
    execution_job_id = insert_scoped(
        fake_client,
        "execution_jobs",
        ORG,
        {
            "project_id": plan["project_id"],
            "plan_id": plan["id"],
            "source_job_id": job["id"],
            "status": "queued",
            "params": {},
        },
    ).data[0]["id"]

    monkeypatch.setattr(run_module, "get_service_role_client", lambda: fake_client)
    captured = {}

    def fake_units(client, execution_job, state):
        captured["snapshot_dir"] = state.snapshot_dir
        raise RuntimeError("stop before units is built (Phase 3)")

    monkeypatch.setattr(run_module, "stage_units", fake_units)

    run_module.process_execution_job(execution_job_id, ORG)

    [row] = fake_client.table("execution_jobs").rows
    assert row["status"] == "failed"
    assert not captured["snapshot_dir"].exists()
