-- Assessment Report prerequisite (specs_report.md §12, tasks_agent3.md "Assessment Report
-- prerequisite"): per-unit wall-clock timing for the report's runtime estimator.
-- Nullable: null for units that predate it, or that never started. No RLS change.
alter table unit_executions
    add column started_at timestamptz,
    add column finished_at timestamptz;
