-- Agent 1 job cancellation — design.md §4 `POST /api/agent1/jobs/{job_id}/cancel`.
-- `job_status` is shared by ingestion_jobs, planning_jobs and execution_jobs; adding a
-- value is purely additive (Agent 2 and Agent 3 never write it). Nothing here uses the
-- new value, which is why it can share a migration transaction with the rest.
alter type job_status add value if not exists 'cancelled';
