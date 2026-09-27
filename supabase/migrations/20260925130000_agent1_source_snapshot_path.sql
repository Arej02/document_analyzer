-- Agent 1: where a job's source snapshot lives — design.md §2 `ingestion_jobs`,
-- design_agent3.md §13. Added ahead of the snapshot upload itself so Agent 3's
-- POST /executions could refuse plans whose source job has no snapshot.
--
-- Superseded by 20260925040000_agent1_snapshots_bucket.sql, which adds the same
-- column (plus the bucket) and sorts first. `if not exists` makes this a no-op on
-- any database built from these migrations in order, and on the shared project,
-- where both have run.

alter table ingestion_jobs add column if not exists source_snapshot_path text;
