-- Agent 1: where a job's source snapshot lives — design.md §2 `ingestion_jobs`,
-- design_agent3.md §13. Added ahead of the snapshot upload itself (tasks.md
-- "Agent 3 prerequisites") so Agent 3's POST /executions can refuse plans whose
-- source job has no snapshot. Null until the worker starts uploading snapshots.

alter table ingestion_jobs add column source_snapshot_path text;
