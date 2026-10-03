-- Assessment Report prerequisite (specs_report.md §12, tasks.md "Assessment Report
-- prerequisites"): persist the flat discovery stats (loc, dependencies, language.*,
-- framework.*) that were previously only in best-effort workflow_events.
-- Nullable: null for jobs predating it. No RLS change: the table already has policies.
alter table ingestion_jobs
    add column discovery_stats jsonb;
