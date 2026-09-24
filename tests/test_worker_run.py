"""Orchestration-level tests for the worker main loop (run.py) — control flow only.

Every individual pipeline step (fetch, extract, persist, summarize, ground, ...) is
already unit-tested in its own file; these tests stub every step's boundary at the
name run.py itself calls it under (monkeypatching document_analyzer.worker.run's own
module attributes, not the origin modules) and assert only ordering / status
transitions / cleanup-on-failure — the things only the orchestrator itself can get
wrong.
"""

from pathlib import Path

from document_analyzer.worker import persist_summary as persist_summary_module
from document_analyzer.worker import run as run_module
from document_analyzer.worker.fetch import SourceFetchError
from document_analyzer.worker.ground import GroundingResult
from document_analyzer.worker.ground_retry import GroundingRetryResult
from document_analyzer.worker.summarize import EntitySummary
from document_analyzer.worker.summarize_module import ModuleSummary
from document_analyzer.worker.summarize_subsystem import RepoSummary, SubsystemSummary

from fakes import FakeClient


def _fake_embed(text):
    return [float(len(text)), 0.0]


def _seed_job(client, job_id, org_id, github_token_ref=None):
    client.table("ingestion_jobs").insert(
        {
            "id": job_id,
            "org_id": org_id,
            "source_type": "github",
            "source_ref": "https://github.com/example/repo",
            "github_token_ref": github_token_ref,
            "status": "queued",
        }
    ).execute()


def _seed_entities(client, job_id, org_id, rows):
    client.table("extracted_entities").insert(
        [
            {
                "org_id": org_id,
                "job_id": job_id,
                "entity_type": "function",
                "raw_facts": {},
                **row,
            }
            for row in rows
        ]
    ).execute()


def _patch_grounded_pipeline(monkeypatch):
    """Every summary comes back grounded on the first attempt — no retries."""

    def fake_check_grounding_with_retry(facts, generate, max_retries=None, client=None):
        summary = generate()
        return GroundingRetryResult(summary=summary, grounding=GroundingResult(verdict="grounded", issues=[]), retries_used=0)

    monkeypatch.setattr(run_module, "check_grounding_with_retry", fake_check_grounding_with_retry)
    monkeypatch.setattr(
        run_module, "summarize_entity", lambda entity, client=None: EntitySummary(f"summary of {entity.name}", "low", [])
    )
    monkeypatch.setattr(
        run_module,
        "summarize_module",
        lambda file_path, entity_summaries, client=None: ModuleSummary(f"module {file_path}", "low", []),
    )
    monkeypatch.setattr(
        run_module,
        "summarize_subsystem",
        lambda name, children, client=None: SubsystemSummary(f"subsystem {name}", "low", []),
    )
    monkeypatch.setattr(run_module, "summarize_repo", lambda children, client=None: RepoSummary("repo summary", "low", []))
    monkeypatch.setattr(run_module, "get_azure_openai_client", lambda: object())
    monkeypatch.setattr(persist_summary_module, "embed_text", _fake_embed)
    monkeypatch.setattr(run_module, "fetch_source", lambda client, job, dest_dir: Path(dest_dir) / "repo")
    monkeypatch.setattr(run_module, "_strip_boilerplate", lambda repo_path, client, job_id, org_id: None)


def test_process_job_runs_full_pipeline_and_marks_done(monkeypatch):
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
    client.table("projects").insert({"id": "project-1", "org_id": "org-1", "source_type": "github", "source_ref": "x"}).execute()
    _seed_job(client, "job-1", "org-1")
    client.table("ingestion_jobs").rows[-1]["project_id"] = "project-1"

    def fake_run_extractors(repo_path, client, job_id, org_id):
        _seed_entities(
            client,
            job_id,
            org_id,
            [
                {"name": "foo", "file_path": "app.php", "content_hash": "hash-foo"},
                {"name": "bar", "file_path": "app.php", "content_hash": "hash-bar"},
            ],
        )

    monkeypatch.setattr(run_module, "_run_extractors", fake_run_extractors)
    _patch_grounded_pipeline(monkeypatch)

    run_module.process_job("job-1", "org-1")

    [job_row] = client.table("ingestion_jobs").rows
    assert job_row["status"] == "done"
    assert job_row["progress_stage"] is None

    summaries = client.table("knowledge_summaries").rows
    assert len(summaries) == 5  # 2 entity + 1 module + 1 subsystem + 1 repo
    assert all(row["grounding_status"] == "verified" for row in summaries)
    assert len(client.table("summary_cache").rows) == 2

    [project_row] = client.table("projects").rows
    assert project_row["latest_job_id"] == "job-1"  # tasks.md Phase 5


def test_process_job_skips_a_redelivered_job_that_already_finished(monkeypatch):
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
    _seed_job(client, "job-1", "org-1")
    client.table("ingestion_jobs").rows[-1]["status"] = "done"

    def fail_if_called(*args, **kwargs):
        raise AssertionError("a finished job must not be re-run")

    monkeypatch.setattr(run_module, "fetch_source", fail_if_called)
    monkeypatch.setattr(run_module, "_run_extractors", fail_if_called)

    run_module.process_job("job-1", "org-1")

    [job_row] = client.table("ingestion_jobs").rows
    assert job_row["status"] == "done"
    assert client.table("extracted_entities").rows == []


def test_process_job_folds_modules_across_several_files_and_subsystems(monkeypatch):
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
    _seed_job(client, "job-1", "org-1")

    def fake_run_extractors(repo_path, client, job_id, org_id):
        _seed_entities(
            client,
            job_id,
            org_id,
            [
                {"name": "a", "file_path": "inc/a.php", "content_hash": "hash-a"},
                {"name": "b", "file_path": "inc/b.php", "content_hash": "hash-b"},
                {"name": "c", "file_path": "admin/c.php", "content_hash": "hash-c"},
            ],
        )

    monkeypatch.setattr(run_module, "_run_extractors", fake_run_extractors)
    _patch_grounded_pipeline(monkeypatch)

    run_module.process_job("job-1", "org-1")

    [job_row] = client.table("ingestion_jobs").rows
    assert job_row["status"] == "done"
    levels = sorted(row["level"] for row in client.table("knowledge_summaries").rows)
    assert levels == ["entity"] * 3 + ["module"] * 3 + ["repo"] + ["subsystem"] * 2


def test_process_job_with_zero_entities_still_completes(monkeypatch):
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
    _seed_job(client, "job-1", "org-1")
    monkeypatch.setattr(run_module, "_run_extractors", lambda repo_path, client, job_id, org_id: None)
    _patch_grounded_pipeline(monkeypatch)

    run_module.process_job("job-1", "org-1")

    [job_row] = client.table("ingestion_jobs").rows
    assert job_row["status"] == "done"
    assert client.table("knowledge_summaries").rows == []


def test_process_job_marks_failed_and_cleans_up_github_token_on_pipeline_error(monkeypatch):
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
    _seed_job(client, "job-1", "org-1", github_token_ref="vault-secret-1")

    async def fake_resolve(ref):
        assert ref == "vault-secret-1"
        return "gh-token-abc"

    deleted = []

    async def fake_delete(ref):
        deleted.append(ref)

    monkeypatch.setattr(run_module, "resolve_github_token", fake_resolve)
    monkeypatch.setattr(run_module, "delete_github_token", fake_delete)

    def fake_fetch_source(client, job, dest_dir):
        assert job["access_token"] == "gh-token-abc"
        raise SourceFetchError("could not clone repo")

    monkeypatch.setattr(run_module, "fetch_source", fake_fetch_source)

    run_module.process_job("job-1", "org-1")

    [job_row] = client.table("ingestion_jobs").rows
    assert job_row["status"] == "failed"
    assert "could not clone repo" in job_row["error"]
    assert deleted == ["vault-secret-1"]


def test_process_job_reuses_unchanged_entity_summary_across_a_reingestion(monkeypatch):
    """tasks.md Phase 5 "Confirm summary_cache correctly reuses unchanged entity
    summaries" — an end-to-end, two-job confirmation. Found and fixed a real gap
    while writing this: a cache hit used to return the *first* job's
    knowledge_summaries row as-is, so the second job's own (job_id, entity_id) pair
    had no row of its own for GET .../entities / .../entities/{id} (entities.py) to
    join against — reused-but-unqueryable. `copy_cached_summary_forward`
    (summary_cache.py) fixes this by writing a cheap new row per job on a hit."""
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
    client.table("projects").insert({"id": "project-1", "org_id": "org-1", "source_type": "github", "source_ref": "x"}).execute()
    _seed_job(client, "job-1", "org-1")
    client.table("ingestion_jobs").rows[-1]["project_id"] = "project-1"
    _seed_job(client, "job-2", "org-1")
    client.table("ingestion_jobs").rows[-1]["project_id"] = "project-1"

    def fake_run_extractors(repo_path, client, job_id, org_id):
        _seed_entities(client, job_id, org_id, [{"name": "foo", "file_path": "app.php", "content_hash": "same-hash"}])

    monkeypatch.setattr(run_module, "_run_extractors", fake_run_extractors)
    _patch_grounded_pipeline(monkeypatch)

    calls: list[str] = []

    def counting_summarize_entity(entity, client=None):
        calls.append(entity.name)
        return EntitySummary(f"summary of {entity.name}", "low", [])

    monkeypatch.setattr(run_module, "summarize_entity", counting_summarize_entity)

    run_module.process_job("job-1", "org-1")
    run_module.process_job("job-2", "org-1")

    assert calls == ["foo"]  # job-2's identical entity never went back to the LLM

    entity_rows = client.table("extracted_entities").rows
    [job1_entity] = [r for r in entity_rows if r["job_id"] == "job-1"]
    [job2_entity] = [r for r in entity_rows if r["job_id"] == "job-2"]

    entity_summaries = [r for r in client.table("knowledge_summaries").rows if r["level"] == "entity"]
    [job1_summary] = [r for r in entity_summaries if r["job_id"] == "job-1"]
    [job2_summary] = [r for r in entity_summaries if r["job_id"] == "job-2"]

    # Each job has its own queryable row for its own entity — the gap this test caught.
    assert job1_summary["id"] != job2_summary["id"]
    assert job1_summary["entity_id"] == job1_entity["id"]
    assert job2_summary["entity_id"] == job2_entity["id"]
    assert job2_summary["summary_text"] == job1_summary["summary_text"] == "summary of foo"
    assert job2_summary["grounding_status"] == job1_summary["grounding_status"] == "verified"

    # summary_cache still maps the content_hash to the original (job-1) summary.
    [cache_row] = client.table("summary_cache").rows
    assert cache_row["summary_id"] == job1_summary["id"]

    # diff.py sees the same file as unchanged for job-2, since content_hash matched.
    [job2_row] = [r for r in client.table("ingestion_jobs").rows if r["id"] == "job-2"]
    assert job2_row["unchanged_file_count"] == 1
    assert job2_row["changed_file_count"] == 0


def test_process_job_marks_failed_on_unexpected_error(monkeypatch):
    client = FakeClient(org_id="org-1")
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
    _seed_job(client, "job-1", "org-1")

    def raise_unexpected(client, job, dest_dir):
        raise RuntimeError("something else broke")

    monkeypatch.setattr(run_module, "fetch_source", raise_unexpected)

    run_module.process_job("job-1", "org-1")

    [job_row] = client.table("ingestion_jobs").rows
    assert job_row["status"] == "failed"
    assert "something else broke" in job_row["error"]
