-- Agent 3 (Execute) tables — design_agent3.md §2.
-- execution_jobs and unit_executions are written by the service-role executor
-- worker and by the API's POST /plans/{plan_id}/executions (through the
-- service-role client) only, so authenticated users get org-scoped select
-- policies and nothing else.
-- Reuses Agent 1's job_status enum and current_org_id() helper
-- (20260918000000_create_agent1_tables.sql, 20260918010000_rls_policies.sql).
-- Plans are immutable snapshots (CLAUDE.md), so nothing cascades from Agent 2
-- or Agent 1 rows; only an execution job's own unit rows cascade with it.

create type execution_stage as enum ('prepare', 'units', 'assemble', 'final_test');
create type unit_execution_status as enum ('pending', 'running', 'passed', 'failed_verification', 'error');

create table execution_jobs (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    project_id uuid not null references projects (id),
    plan_id uuid not null references migration_plans (id),
    -- Copied from the plan: the Agent 1 job whose source snapshot this run reads.
    source_job_id uuid not null references ingestion_jobs (id),
    status job_status not null default 'queued',
    progress_stage execution_stage,
    -- Job-level failure only; unit failures live on unit_executions.last_error.
    error text,
    -- {model_deployment, max_attempts, max_context_chars, sandbox_timeout_seconds,
    --  sandbox_memory_mb, runner_image}, frozen at creation (§11).
    params jsonb not null,
    -- {total, passed, failed_verification, error, built_on_failed_dependency}
    counts jsonb,
    -- {passed, failed, errors, duration_s} from the assembled run (§5).
    final_test_summary jsonb,
    output_zip_path text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- specs FR1: at most one queued/running execution job per plan.
create unique index execution_jobs_one_active_per_plan
    on execution_jobs (plan_id)
    where status in ('queued', 'running');

create table unit_executions (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    execution_job_id uuid not null references execution_jobs (id) on delete cascade,
    -- The plan item's key (migration_plan_items PK is (plan_id, cluster_id)).
    cluster_id uuid not null references clusters (id),
    -- Copied from the plan item.
    phase_cluster_id uuid not null references clusters (id),
    phase_order int not null,
    unit_order int not null,
    status unit_execution_status not null default 'pending',
    attempts int not null default 0,
    flags text[] not null default '{}',
    -- {entity_id: {python_module, python_name, kind}} for every public_interface entry.
    symbol_map jsonb,
    -- {contract: {passed, failed}, behaviour: {passed, failed}, duration_s} for the last attempt.
    test_summary jsonb,
    last_error text,
    -- {org_id}/{execution_job_id}/units/{cluster_id}/
    storage_prefix text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint unit_executions_job_cluster_unique unique (execution_job_id, cluster_id),
    constraint unit_executions_attempts_nonnegative check (attempts >= 0),
    constraint unit_executions_flags_known check (
        flags <@ array['unverified_inputs', 'built_on_failed_dependency', 'stubbed']::text[]
    )
);

create index execution_jobs_org_id_idx on execution_jobs (org_id);
create index execution_jobs_project_id_idx on execution_jobs (project_id);
create index execution_jobs_plan_id_idx on execution_jobs (plan_id);
create index execution_jobs_source_job_id_idx on execution_jobs (source_job_id);
create index unit_executions_org_id_idx on unit_executions (org_id);
create index unit_executions_cluster_id_idx on unit_executions (cluster_id);
create index unit_executions_phase_cluster_id_idx on unit_executions (phase_cluster_id);

-- Read-only for authenticated users; no insert/update/delete policies.
alter table execution_jobs enable row level security;

create policy execution_jobs_select_own_org
    on execution_jobs for select
    to authenticated
    using (org_id = current_org_id());

alter table unit_executions enable row level security;

create policy unit_executions_select_own_org
    on unit_executions for select
    to authenticated
    using (org_id = current_org_id());
