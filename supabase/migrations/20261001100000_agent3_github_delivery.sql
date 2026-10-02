-- Agent 3 GitHub PR delivery tables — design_agent3.md §15.6.
-- project_github_connections and deliveries are written by the API's
-- service-role client only (the connection endpoints and the delivery background
-- task), so authenticated users get org-scoped select policies and nothing else,
-- the same as the other Agent 3 tables (20260925100000_agent3_execution_tables.sql).
-- Reuses Agent 1's current_org_id() helper (20260918010000_rls_policies.sql).
--
-- The GitHub token is NEVER a column. token_ref is the id of a Supabase Vault
-- secret (design_agent3.md §15.7); the secret is only readable through the
-- vault schema over a direct DB connection, which PostgREST does not expose.
-- Agent 1 stores its per-job token reference the same way (ingestion_jobs.github_token_ref).

create table project_github_connections (
    -- One connection per project.
    project_id uuid primary key references projects (id) on delete cascade,
    org_id uuid not null,
    -- owner/name of the customer's separate target repo (never the legacy repo).
    repo_full_name text not null,
    -- Read from GitHub when the connection was made.
    default_branch text not null,
    -- Vault secret id (secret name agent3_delivery_token_{project_id}).
    token_ref uuid not null,
    created_by uuid not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint project_github_connections_repo_format check (
        repo_full_name ~ '^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$'
        -- no "." or ".." path segment (the name is later used in API URL paths)
        and repo_full_name !~ '(^|/)[.]{1,2}(/|$)'
    )
);

create table deliveries (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    execution_job_id uuid not null references execution_jobs (id) on delete cascade,
    -- Denormalized for listing and project delete, like execution_jobs.project_id.
    project_id uuid not null references projects (id),
    -- Frozen from the connection when the delivery is created, so a later
    -- reconnect cannot redirect an in-flight delivery.
    repo_full_name text not null,
    default_branch text not null,
    status text not null default 'queued',
    -- Readable and secret-redacted.
    error text,
    -- [{kind: 'scaffold'|'phase', phase_order, label, branch, pr_number, url}],
    -- appended as each PR is opened.
    prs jsonb not null default '[]'::jsonb,
    -- Whether the stack's root commit SHA equalled the zip's baseline commit
    -- (design_agent3.md §15.14); null until the root exists.
    root_matches_baseline boolean,
    -- Touched after every GitHub step; a stale queued/running row is swept (§15.8).
    heartbeat_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint deliveries_status_known check (status in ('queued', 'running', 'done', 'failed')),
    constraint deliveries_prs_is_array check (jsonb_typeof(prs) = 'array'),
    constraint deliveries_repo_format check (
        repo_full_name ~ '^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$'
        -- no "." or ".." path segment (the name is later used in API URL paths)
        and repo_full_name !~ '(^|/)[.]{1,2}(/|$)'
    )
);

-- At most one queued/running delivery per execution (§15.6); same shape as
-- execution_jobs_one_active_per_plan.
create unique index deliveries_one_active_per_execution
    on deliveries (execution_job_id)
    where status in ('queued', 'running');

create index project_github_connections_org_id_idx on project_github_connections (org_id);
create index deliveries_org_id_idx on deliveries (org_id);
create index deliveries_execution_job_id_idx on deliveries (execution_job_id);
create index deliveries_project_id_idx on deliveries (project_id);

-- Read-only for authenticated users; no insert/update/delete policies.
alter table project_github_connections enable row level security;

create policy project_github_connections_select_own_org
    on project_github_connections for select
    to authenticated
    using (org_id = current_org_id());

alter table deliveries enable row level security;

create policy deliveries_select_own_org
    on deliveries for select
    to authenticated
    using (org_id = current_org_id());
