-- Records the cloned commit for a GitHub-sourced job — design.md §2 `ingestion_jobs`,
-- Agent 3 prerequisite (design_agent3.md §13, tasks.md "Agent 3 prerequisites").
--
-- Populated by worker/fetch.py (`git rev-parse HEAD` right after the `--depth 1`
-- clone) and persisted by the worker main loop. Null for archive uploads (no
-- commit to record) and for any job that predates this column.

alter table ingestion_jobs
    add column source_commit_sha text;
