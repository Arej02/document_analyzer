-- Agent 2 (Plan) planning-stage tables — design_agent2.md §2.
-- planning_jobs, clusters, cluster_members, cluster_reports, cluster_edges.
-- All writes come from the service-role planner worker (RLS bypassed by
-- construction), so authenticated users only get org-scoped select policies.
-- Reuses Agent 1's job_status, risk_level and grounding_status enums and its
-- current_org_id() helper (20260918010000_rls_policies.sql).

create type planning_stage as enum ('graph', 'cluster', 'report', 'order');
create type cluster_level as enum ('unit', 'phase');
create type cluster_kind as enum ('community', 'foundation', 'merged_scc', 'isolated_file');
create type cluster_edge_flag as enum ('none', 'merged', 'needs_facade');

create table planning_jobs (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    project_id uuid not null references projects (id),
    source_job_id uuid not null references ingestion_jobs (id),
    status job_status not null default 'queued',
    progress_stage planning_stage,
    error text,
    -- {seed, alpha, max_unit_size, hub_percentile, leiden_resolution}, frozen at creation (§12).
    params jsonb not null,
    graph_stats jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- specs FR1: at most one queued/running planning job per project.
create unique index planning_jobs_one_active_per_project
    on planning_jobs (project_id)
    where status in ('queued', 'running');

create table clusters (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    planning_job_id uuid not null references planning_jobs (id) on delete cascade,
    level cluster_level not null,
    -- Set for units (-> their phase); null for phases.
    parent_cluster_id uuid references clusters (id) on delete cascade,
    kind cluster_kind not null,
    size int not null,
    label text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint clusters_parent_matches_level check (
        (level = 'unit' and parent_cluster_id is not null)
        or (level = 'phase' and parent_cluster_id is null)
    )
);

-- Unit-level clusters only (enforced by the worker; phase membership is
-- derived through clusters.parent_cluster_id, never stored here).
create table cluster_members (
    cluster_id uuid not null references clusters (id) on delete cascade,
    entity_id uuid not null references extracted_entities (id),
    org_id uuid not null,
    canonical_entity_id uuid not null references extracted_entities (id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (cluster_id, entity_id)
);

create table cluster_reports (
    cluster_id uuid primary key references clusters (id) on delete cascade,
    org_id uuid not null,
    summary_text text not null,
    risk_level risk_level not null,
    risk_factors jsonb not null default '[]'::jsonb,
    -- Same pinned model/dimension as knowledge_summaries.embedding (embedding_config).
    embedding vector(384),
    grounding_status grounding_status not null default 'unverified',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- from_cluster_id depends on to_cluster_id (to migrates first).
create table cluster_edges (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    planning_job_id uuid not null references planning_jobs (id) on delete cascade,
    level cluster_level not null,
    from_cluster_id uuid not null references clusters (id) on delete cascade,
    to_cluster_id uuid not null references clusters (id) on delete cascade,
    call_count int not null default 0,
    hook_binding_count int not null default 0,
    weight double precision not null,
    flag cluster_edge_flag not null default 'none',
    scc_id int,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index planning_jobs_org_id_idx on planning_jobs (org_id);
create index planning_jobs_project_id_idx on planning_jobs (project_id);
create index planning_jobs_source_job_id_idx on planning_jobs (source_job_id);
create index clusters_org_id_idx on clusters (org_id);
create index clusters_planning_job_id_idx on clusters (planning_job_id);
create index clusters_parent_cluster_id_idx on clusters (parent_cluster_id);
create index cluster_members_org_id_idx on cluster_members (org_id);
create index cluster_members_entity_id_idx on cluster_members (entity_id);
create index cluster_members_canonical_entity_id_idx on cluster_members (canonical_entity_id);
create index cluster_reports_org_id_idx on cluster_reports (org_id);
create index cluster_edges_org_id_idx on cluster_edges (org_id);
create index cluster_edges_planning_job_id_idx on cluster_edges (planning_job_id);
create index cluster_edges_from_cluster_id_idx on cluster_edges (from_cluster_id);
create index cluster_edges_to_cluster_id_idx on cluster_edges (to_cluster_id);

alter table planning_jobs enable row level security;

create policy planning_jobs_select_own_org
    on planning_jobs for select
    to authenticated
    using (org_id = current_org_id());

alter table clusters enable row level security;

create policy clusters_select_own_org
    on clusters for select
    to authenticated
    using (org_id = current_org_id());

alter table cluster_members enable row level security;

create policy cluster_members_select_own_org
    on cluster_members for select
    to authenticated
    using (org_id = current_org_id());

alter table cluster_reports enable row level security;

create policy cluster_reports_select_own_org
    on cluster_reports for select
    to authenticated
    using (org_id = current_org_id());

alter table cluster_edges enable row level security;

create policy cluster_edges_select_own_org
    on cluster_edges for select
    to authenticated
    using (org_id = current_org_id());
