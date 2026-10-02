-- Assessment Report prerequisite (specs_report.md §12, tasks.md "Assessment Report
-- prerequisites"): persist the rubric signals behind each summary's risk_level.
-- Existing rows read '[]' (signal not recorded). No RLS change: the table already has policies.
alter table knowledge_summaries
    add column risk_factors jsonb not null default '[]'::jsonb;
