-- RLS policies scoped to org_id — design.md §1a, §2.
-- Service role (used by the async worker) bypasses RLS by construction and needs no policies here.

create table org_members (
    user_id uuid primary key references auth.users (id),
    org_id uuid not null,
    created_at timestamptz not null default now()
);

alter table org_members enable row level security;

create policy org_members_select_own
    on org_members for select
    to authenticated
    using (user_id = auth.uid());

create function current_org_id()
returns uuid
language sql
security definer
stable
set search_path = public
as $$
    select org_id from org_members where user_id = auth.uid()
$$;

grant execute on function current_org_id() to authenticated;

-- projects: authenticated users read their org's projects and create new ones
-- (POST /api/agent1/jobs resolves/creates a project row synchronously in the request).
alter table projects enable row level security;

create policy projects_select_own_org
    on projects for select
    to authenticated
    using (org_id = current_org_id());

create policy projects_insert_own_org
    on projects for insert
    to authenticated
    with check (org_id = current_org_id());

-- ingestion_jobs: same pattern — created synchronously by the authenticated API,
-- updated afterward only by the service-role worker.
alter table ingestion_jobs enable row level security;

create policy ingestion_jobs_select_own_org
    on ingestion_jobs for select
    to authenticated
    using (org_id = current_org_id());

create policy ingestion_jobs_insert_own_org
    on ingestion_jobs for insert
    to authenticated
    with check (org_id = current_org_id());

-- extracted_entities, entity_edges, knowledge_summaries, summary_cache:
-- read-only for authenticated users — all writes come from the service-role worker.
alter table extracted_entities enable row level security;

create policy extracted_entities_select_own_org
    on extracted_entities for select
    to authenticated
    using (org_id = current_org_id());

alter table entity_edges enable row level security;

create policy entity_edges_select_own_org
    on entity_edges for select
    to authenticated
    using (org_id = current_org_id());

alter table knowledge_summaries enable row level security;

create policy knowledge_summaries_select_own_org
    on knowledge_summaries for select
    to authenticated
    using (org_id = current_org_id());

alter table summary_cache enable row level security;

create policy summary_cache_select_own_org
    on summary_cache for select
    to authenticated
    using (org_id = current_org_id());

-- embedding_config: global model config, not org-scoped (design.md §2).
-- Blanket read for authenticated users; writes are service-role/admin only.
alter table embedding_config enable row level security;

create policy embedding_config_select_all
    on embedding_config for select
    to authenticated
    using (true);
