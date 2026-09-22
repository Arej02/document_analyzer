-- File-diff counts against a project's previous job — design.md §2 `ingestion_jobs`,
-- specs.md FR6 ("only changed files ... are re-extracted and re-summarized"),
-- tasks.md Phase 5 "Diff against the project's previous job by content_hash".
--
-- Populated by worker/diff.py once per job, right after that job's own entities
-- are persisted. Null for a job that predates this migration, or (transiently)
-- for one still mid-extraction. A job with no previous completed job for its
-- project (first-time ingestion) still gets real counts: every current file is
-- `changed_file_count`, `unchanged_file_count` is 0.

alter table ingestion_jobs
    add column changed_file_count int,
    add column unchanged_file_count int,
    add column removed_file_count int;
