-- Agent 2 (Plan) plan + review tables — design_agent2.md §2, §9.
-- migration_plans and migration_plan_items are written by the service-role
-- planner worker (and by the API's override recomputation, §9.2) only.
-- phase_approvals and plan_overrides are user-writable: authenticated users
-- get org-scoped insert policies, and the acting user column must be their own
-- auth.uid() (design: "= auth.uid(), set server-side").
-- Plans are immutable snapshots (CLAUDE.md), so nothing here cascades on
-- delete: removing clusters or a planning job must never silently take an
-- existing plan version with it.

create type plan_status as enum ('draft', 'in_review', 'approved', 'rejected', 'stale');
create type phase_approval_status as enum ('approved', 'rejected');
create type plan_override_kind as enum ('merge', 'split', 'facade_ack', 'reorder');

create table migration_plans (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    project_id uuid not null references projects (id),
    planning_job_id uuid not null references planning_jobs (id),
    -- Pinned Agent 1 snapshot (specs FR7).
    source_job_id uuid not null references ingestion_jobs (id),
    version int not null,
    parent_plan_id uuid references migration_plans (id),
    status plan_status not null default 'draft',
    -- needs_facade SCCs without a facade_ack; must be 0 to approve.
    open_facade_flags int not null default 0,
    approved_by uuid references auth.users (id),
    approved_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint migration_plans_version_unique unique (planning_job_id, version),
    constraint migration_plans_version_positive check (version >= 1),
    constraint migration_plans_open_facade_flags_nonnegative check (open_facade_flags >= 0)
);

create table migration_plan_items (
    plan_id uuid not null references migration_plans (id),
    -- Unit-level cluster. Override-merged units use the lowest merged id, with
    -- the merged ids in test_contract.merged_cluster_ids (clusters stay immutable).
    cluster_id uuid not null references clusters (id),
    org_id uuid not null,
    -- Phase-level cluster; may differ from clusters.parent_cluster_id after a
    -- reorder/merge override.
    phase_cluster_id uuid not null references clusters (id),
    phase_order int not null,
    unit_order int not null,
    test_contract jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (plan_id, cluster_id)
);

create table phase_approvals (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    plan_id uuid not null references migration_plans (id),
    phase_cluster_id uuid not null references clusters (id),
    status phase_approval_status not null,
    reviewer_id uuid not null references auth.users (id),
    note text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint phase_approvals_plan_phase_unique unique (plan_id, phase_cluster_id)
);

create table plan_overrides (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    -- The plan version the override was made against.
    plan_id uuid not null references migration_plans (id),
    kind plan_override_kind not null,
    -- Cluster ids / edge id / new position, depending on kind (§9.2).
    payload jsonb not null,
    created_by uuid not null references auth.users (id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index migration_plans_org_id_idx on migration_plans (org_id);
create index migration_plans_project_id_idx on migration_plans (project_id);
create index migration_plans_source_job_id_idx on migration_plans (source_job_id);
create index migration_plans_parent_plan_id_idx on migration_plans (parent_plan_id);
create index migration_plan_items_org_id_idx on migration_plan_items (org_id);
create index migration_plan_items_cluster_id_idx on migration_plan_items (cluster_id);
create index migration_plan_items_phase_cluster_id_idx on migration_plan_items (phase_cluster_id);
create index phase_approvals_org_id_idx on phase_approvals (org_id);
create index phase_approvals_phase_cluster_id_idx on phase_approvals (phase_cluster_id);
create index plan_overrides_org_id_idx on plan_overrides (org_id);
create index plan_overrides_plan_id_idx on plan_overrides (plan_id);

-- migration_plans, migration_plan_items: read-only for authenticated users.
alter table migration_plans enable row level security;

create policy migration_plans_select_own_org
    on migration_plans for select
    to authenticated
    using (org_id = current_org_id());

alter table migration_plan_items enable row level security;

create policy migration_plan_items_select_own_org
    on migration_plan_items for select
    to authenticated
    using (org_id = current_org_id());

-- phase_approvals, plan_overrides: user-writable (insert only; no update/delete).
alter table phase_approvals enable row level security;

create policy phase_approvals_select_own_org
    on phase_approvals for select
    to authenticated
    using (org_id = current_org_id());

create policy phase_approvals_insert_own_org
    on phase_approvals for insert
    to authenticated
    with check (org_id = current_org_id() and reviewer_id = auth.uid());

alter table plan_overrides enable row level security;

create policy plan_overrides_select_own_org
    on plan_overrides for select
    to authenticated
    using (org_id = current_org_id());

create policy plan_overrides_insert_own_org
    on plan_overrides for insert
    to authenticated
    with check (org_id = current_org_id() and created_by = auth.uid());
