-- Assessment Report — design_report.md §3, §4; tasks_report.md Phase 1.
-- report_generation_jobs is written by the service-role reporter worker and by the
-- API's POST /api/projects/{id}/reports (through the service-role client) only, so
-- authenticated users get an org-scoped select policy and nothing else.
-- Reuses Agent 1's job_status enum and current_org_id() helper
-- (20260918000000_create_agent1_tables.sql, 20260918010000_rls_policies.sql).
-- The report is a read-only consumer of Agents 1-3: nothing here cascades from, or
-- is referenced by, their rows apart from the pinned source_job_id / plan_id FKs.
-- The remaining Phase 1 tables and the bucket are added to this migration by the
-- following tasks.

create table report_generation_jobs (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    project_id uuid not null references projects (id),
    tier text not null,
    -- The project's latest done Agent 1 job at creation; the report is pinned to it.
    source_job_id uuid not null references ingestion_jobs (id),
    -- Full tier only: the plan (and its version at creation) the report is pinned to.
    plan_id uuid references migration_plans (id),
    plan_version int,
    status job_status not null default 'queued',
    -- collect, estimate, summarize, render_html, render_pdf, publish (design_report.md §4).
    progress_stage text,
    -- Job-level failure only; a PDF failure is assessment_reports.pdf_status.
    error text,
    -- {model_deployment, max_grounding_retries, pdf_timeout_seconds, pdf_memory_mb,
    --  renderer_image}, frozen at creation (design_report.md §12).
    params jsonb not null,
    created_by uuid not null references auth.users (id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint report_generation_jobs_tier_check check (tier in ('xray', 'full')),
    constraint report_generation_jobs_stage_check check (
        progress_stage is null
        or progress_stage in ('collect', 'estimate', 'summarize', 'render_html', 'render_pdf', 'publish')
    ),
    -- A full assessment names its plan and version; an X-ray names neither.
    constraint report_generation_jobs_plan_matches_tier check (
        (tier = 'full' and plan_id is not null and plan_version is not null)
        or (tier = 'xray' and plan_id is null and plan_version is null)
    ),
    constraint report_generation_jobs_plan_version_positive check (plan_version is null or plan_version >= 1)
);

-- At most one queued/running report job per project and tier.
create unique index report_generation_jobs_one_active_per_project_tier
    on report_generation_jobs (project_id, tier)
    where status in ('queued', 'running');

create index report_generation_jobs_org_id_idx on report_generation_jobs (org_id);
create index report_generation_jobs_project_id_idx on report_generation_jobs (project_id);
create index report_generation_jobs_source_job_id_idx on report_generation_jobs (source_job_id);
create index report_generation_jobs_plan_id_idx on report_generation_jobs (plan_id);

-- Read-only for authenticated users; no insert/update/delete policies.
alter table report_generation_jobs enable row level security;

create policy report_generation_jobs_select_own_org
    on report_generation_jobs for select
    to authenticated
    using (org_id = current_org_id());

-- Its own queue, never shared with another agent's (CLAUDE.md: each agent has its own
-- queue and worker). pgmq extension already enabled by 20260918030000_pgmq_job_queue.sql.
select pgmq.create('report_generation_jobs');

-- assessment_reports — design_report.md §3. One immutable snapshot per generation job,
-- pinned to one Agent 1 job and, for a full assessment, one plan id + version.
-- Regenerating inserts a new row. Tenants can only select; the worker's insert in
-- `publish` and its later pdf_status / pdf_path / html_path updates use the
-- service role. report_json is customer-safe by construction (no internal cost,
-- no source); internal figures live in report_internal_estimates.
create table assessment_reports (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    project_id uuid not null references projects (id),
    -- One report per generation job, so a redelivered `publish` upserts instead of duplicating.
    generation_job_id uuid not null unique references report_generation_jobs (id),
    tier text not null,
    source_job_id uuid not null references ingestion_jobs (id),
    plan_id uuid references migration_plans (id),
    plan_version int,
    -- The plan's status when the report was generated (draft, in_review or approved).
    plan_status_at_generation plan_status,
    report_json jsonb not null,
    schema_version int not null,
    template_version text not null,
    estimator_version text not null,
    exec_summary_status text not null,
    -- Object paths in the `reports` bucket: {org_id}/{report_id}/report.html and report.pdf.
    html_path text,
    pdf_path text,
    pdf_status text not null default 'pending',
    created_by uuid not null references auth.users (id),
    created_at timestamptz not null default now(),
    constraint assessment_reports_tier_check check (tier in ('xray', 'full')),
    constraint assessment_reports_plan_matches_tier check (
        (tier = 'full' and plan_id is not null and plan_version is not null and plan_status_at_generation is not null)
        or (tier = 'xray' and plan_id is null and plan_version is null and plan_status_at_generation is null)
    ),
    constraint assessment_reports_plan_version_positive check (plan_version is null or plan_version >= 1),
    constraint assessment_reports_schema_version_positive check (schema_version >= 1),
    constraint assessment_reports_exec_summary_status_check check (
        exec_summary_status in ('verified', 'unverified', 'withheld')
    ),
    constraint assessment_reports_pdf_status_check check (pdf_status in ('pending', 'done', 'failed'))
);

create index assessment_reports_org_id_idx on assessment_reports (org_id);
-- The project's report list, newest first.
create index assessment_reports_project_created_idx on assessment_reports (project_id, created_at desc);
create index assessment_reports_source_job_id_idx on assessment_reports (source_job_id);
create index assessment_reports_plan_id_idx on assessment_reports (plan_id);

-- Read-only for authenticated users; no insert/update/delete policies.
alter table assessment_reports enable row level security;

create policy assessment_reports_select_own_org
    on assessment_reports for select
    to authenticated
    using (org_id = current_org_id());

-- report_share_links — design_report.md §3, §11. A link gives read-only access to one
-- report snapshot without login, until expires_at or revocation. Only sha256(token) is
-- stored (hex), never the token: the full URL is shown once, at creation.
-- Tenants can read their org's links but never token_hash (column-level grant below);
-- every write goes through the API's service role.
create table report_share_links (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    -- Deleting a report deletes its links, so a deleted report's link is a plain 404.
    report_id uuid not null references assessment_reports (id) on delete cascade,
    -- sha256(token) as 64 lowercase hex chars; the public route looks a link up by it.
    token_hash text not null unique,
    created_by uuid not null references auth.users (id),
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    revoked_at timestamptz,
    revoked_by uuid references auth.users (id),
    access_count int not null default 0,
    last_accessed_at timestamptz,
    constraint report_share_links_token_hash_format check (token_hash ~ '^[0-9a-f]{64}$'),
    constraint report_share_links_expires_after_created check (expires_at > created_at),
    constraint report_share_links_access_count_nonnegative check (access_count >= 0),
    -- Revoking records who did it, and only a revocation records one.
    constraint report_share_links_revoker_with_revocation check (
        (revoked_at is null and revoked_by is null) or (revoked_at is not null)
    )
);

create index report_share_links_org_id_idx on report_share_links (org_id);
create index report_share_links_report_id_idx on report_share_links (report_id);

alter table report_share_links enable row level security;

create policy report_share_links_select_own_org
    on report_share_links for select
    to authenticated
    using (org_id = current_org_id());

-- Column-level read access: everything except token_hash. A `select *` or a select that
-- names token_hash from a tenant client fails with a permission error, so the hash can't
-- leak through PostgREST even to the owning org. Callers must list columns.
revoke all on report_share_links from anon, authenticated;
grant select (
    id, org_id, report_id, created_by, created_at, expires_at,
    revoked_at, revoked_by, access_count, last_accessed_at
) on report_share_links to authenticated;

-- report_internal_estimates — design_report.md §3. The operator-only half of the estimate:
-- our AI cost range, the calibration breakdown and the basis of the quoted price. Kept
-- apart from assessment_reports so "the customer report has no internal cost" holds by
-- construction. RLS is on with NO tenant policy and all privileges revoked from anon and
-- authenticated, the same pattern as llm_invocations (20261001000000_llm_cost_accounting.sql):
-- written by the reporter worker and read by the operator endpoint, both service role.
create table report_internal_estimates (
    org_id uuid not null,
    report_id uuid not null references assessment_reports (id) on delete cascade,
    -- USD estimates from list prices (llm_pricing.toml); null when there was no usable
    -- calibration (nothing priced), never zero.
    ai_cost_p50 numeric(14, 4),
    ai_cost_p90 numeric(14, 4),
    -- Calibration units behind the estimate.
    n int not null default 0,
    -- Per-bucket counts, default vs historical, unpriced rows, snapshot id and date range.
    calibration jsonb not null default '{}'::jsonb,
    -- What the quoted price was derived from; null when no [pricing] is configured.
    price_basis jsonb,
    created_at timestamptz not null default now(),
    primary key (org_id, report_id),
    constraint report_internal_estimates_n_nonnegative check (n >= 0),
    constraint report_internal_estimates_cost_nonnegative check (
        (ai_cost_p50 is null or ai_cost_p50 >= 0) and (ai_cost_p90 is null or ai_cost_p90 >= 0)
    ),
    constraint report_internal_estimates_p50_le_p90 check (
        ai_cost_p50 is null or ai_cost_p90 is null or ai_cost_p50 <= ai_cost_p90
    )
);

create index report_internal_estimates_report_id_idx on report_internal_estimates (report_id);

alter table report_internal_estimates enable row level security;
revoke all on report_internal_estimates from anon, authenticated;

-- Storage for the report's rendered artifacts — design_report.md §3 "Storage". Private
-- bucket; objects live at `{org_id}/{report_id}/report.html` and `report.pdf`, so the
-- policy scopes reads by the first path segment the same way every table is scoped by
-- org_id (pattern: 20260925120000_agent3_output_bucket.sql). Only the service-role
-- reporter worker writes here, so authenticated users get a select policy and nothing
-- else: no insert, update or delete. Org members read through the API; the public share
-- route reads with the service role, never through this policy.
insert into storage.buckets (id, name, public)
values ('reports', 'reports', false)
on conflict (id) do nothing;

create policy reports_select_own_org
    on storage.objects for select
    to authenticated
    using (
        bucket_id = 'reports'
        and (storage.foldername(name))[1] = current_org_id()::text
    );

-- Widen the CHECKs that only allowed agent1-3 so the report can use cost accounting and
-- workflow events (design_report.md §3.5). Inline column checks carry Postgres's default
-- names (<table>_<column>_check). The existing rows all satisfy the wider constraints.
-- llm_invocations: agent 'report' writes job_type 'report', with a job id (only Q&A has none).
alter table llm_invocations
    drop constraint llm_invocations_agent_check,
    drop constraint llm_invocations_job_type_check,
    drop constraint llm_invocations_agent_job_type_check,
    add constraint llm_invocations_agent_check
        check (agent in ('agent1', 'agent2', 'agent3', 'report')),
    add constraint llm_invocations_job_type_check
        check (job_type in ('ingestion', 'planning', 'execution', 'qa', 'report')),
    add constraint llm_invocations_agent_job_type_check check (
        (agent = 'agent1' and job_type in ('ingestion', 'qa'))
        or (agent = 'agent2' and job_type = 'planning')
        or (agent = 'agent3' and job_type = 'execution')
        or (agent = 'report' and job_type = 'report')
    );

alter table llm_job_costs
    drop constraint llm_job_costs_agent_check,
    drop constraint llm_job_costs_job_type_check,
    drop constraint llm_job_costs_agent_job_type_check,
    add constraint llm_job_costs_agent_check
        check (agent in ('agent1', 'agent2', 'agent3', 'report')),
    add constraint llm_job_costs_job_type_check
        check (job_type in ('ingestion', 'planning', 'execution', 'report')),
    add constraint llm_job_costs_agent_job_type_check check (
        (agent = 'agent1' and job_type = 'ingestion')
        or (agent = 'agent2' and job_type = 'planning')
        or (agent = 'agent3' and job_type = 'execution')
        or (agent = 'report' and job_type = 'report')
    );

alter table workflow_events
    drop constraint workflow_events_workflow_kind_check,
    add constraint workflow_events_workflow_kind_check
        check (workflow_kind in ('agent1_ingestion', 'agent2_planning', 'agent3_execution', 'report_generation'));
