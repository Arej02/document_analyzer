-- Agent 1 (Understand) core schema — design.md §2
-- Tables only. RLS policies are applied in a separate migration (Phase 0 next task).

create extension if not exists pgcrypto;
create extension if not exists vector;

create type source_type as enum ('upload', 'github');
create type job_status as enum ('queued', 'running', 'done', 'failed');
create type entity_type as enum ('function', 'class', 'route', 'schema', 'hook', 'dependency');
create type edge_type as enum ('calls', 'hook_binding', 'global', 'include');
create type summary_level as enum ('entity', 'module', 'subsystem', 'repo');
create type risk_level as enum ('low', 'medium', 'high');
create type grounding_status as enum ('verified', 'unverified');

create table ingestion_jobs (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    project_id uuid, -- FK to projects added below (projects.latest_job_id references this table)
    source_type source_type not null,
    source_ref text not null,
    github_token_ref text,
    status job_status not null default 'queued',
    progress_stage text,
    error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table projects (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    source_type source_type not null,
    source_ref text not null,
    latest_job_id uuid references ingestion_jobs (id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table ingestion_jobs
    add constraint ingestion_jobs_project_id_fkey
    foreign key (project_id) references projects (id);

create table extracted_entities (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    job_id uuid not null references ingestion_jobs (id),
    entity_type entity_type not null,
    name text not null,
    file_path text not null,
    raw_facts jsonb not null default '{}'::jsonb,
    content_hash text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table entity_edges (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    job_id uuid not null references ingestion_jobs (id),
    from_entity_id uuid not null references extracted_entities (id),
    to_entity_id uuid references extracted_entities (id),
    edge_type edge_type not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table knowledge_summaries (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    job_id uuid not null references ingestion_jobs (id),
    level summary_level not null,
    entity_id uuid references extracted_entities (id),
    summary_text text not null,
    risk_level risk_level not null,
    -- dimension is pinned per embedding_config; fixed via a follow-up migration
    -- once the active embedding_config row is inserted (design.md §2 note).
    embedding vector,
    grounding_status grounding_status not null default 'unverified',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table summary_cache (
    content_hash text not null,
    org_id uuid not null,
    summary_id uuid not null references knowledge_summaries (id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (content_hash, org_id)
);

create table embedding_config (
    id uuid primary key default gen_random_uuid(),
    model_name text not null,
    model_version text not null,
    dimension int not null,
    active boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Only one active embedding_config row at a time (design.md §2).
create unique index embedding_config_single_active
    on embedding_config (active)
    where active;

create index ingestion_jobs_org_id_idx on ingestion_jobs (org_id);
create index projects_org_id_idx on projects (org_id);
create index extracted_entities_org_id_idx on extracted_entities (org_id);
create index extracted_entities_job_id_idx on extracted_entities (job_id);
create index entity_edges_org_id_idx on entity_edges (org_id);
create index entity_edges_job_id_idx on entity_edges (job_id);
create index knowledge_summaries_org_id_idx on knowledge_summaries (org_id);
create index knowledge_summaries_job_id_idx on knowledge_summaries (job_id);
create index summary_cache_org_id_idx on summary_cache (org_id);
