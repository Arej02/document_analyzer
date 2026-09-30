-- Live workflow run view — design.md §2 (`workflow_events`) and §4
-- (`GET /jobs/{job_id}/events`). Append-only event stream shared by all three
-- agents, keyed by (workflow_kind, workflow_id). Each agent writes only its
-- own rows (no agent-to-agent calls); Agent 1 is the first producer.

create table workflow_events (
    id bigserial primary key,
    org_id uuid not null,
    workflow_kind text not null check (workflow_kind in ('agent1_ingestion', 'agent2_planning', 'agent3_execution')),
    -- No FK: workflow_id points at whichever agent's job table workflow_kind names
    -- (ingestion_jobs / planning_jobs / execution_jobs), never a single shared table.
    workflow_id uuid not null,
    event_type text not null,
    stage text,
    message text,
    data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index workflow_events_kind_workflow_id_idx on workflow_events (workflow_kind, workflow_id, id);

alter table workflow_events enable row level security;

-- Writes are service-role only (no insert/update/delete policy for authenticated
-- users): each agent's worker stamps org_id from its own job row, never from
-- repo content.
create policy workflow_events_select_own_org
    on workflow_events for select
    to authenticated
    using (org_id = current_org_id());
