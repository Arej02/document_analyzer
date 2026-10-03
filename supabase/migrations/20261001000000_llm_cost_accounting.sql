-- LLM cost accounting — design.md §7 (all agents), tasks.md "LLM cost accounting".
--
-- Two internal-only tables plus read-only views. Cost is NOT readable by org
-- members: both tables have RLS enabled and deliberately NO anon/authenticated
-- policy (deny by default), because they hold margin data that tenants must never
-- see, not even for their own org. Every read and write goes through the
-- service-role connection (workers, and the operator-only cost endpoints, which
-- filter by org_id AND job id themselves since the service role bypasses RLS).
-- Prompt and response text is never stored here: metadata only.

-- llm_invocations: one row per Azure OpenAI API call, retries included.
-- Append-only (update/delete are revoked from service_role below).
create table llm_invocations (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    created_at timestamptz not null default now(),
    agent text not null check (agent in ('agent1', 'agent2', 'agent3')),
    job_type text not null check (job_type in ('ingestion', 'planning', 'execution', 'qa')),
    -- No FK: job_id points at ingestion_jobs / planning_jobs / execution_jobs
    -- depending on job_type. Null only for Q&A (a request, not a job).
    job_id uuid,
    -- Q&A only: the ingestion job whose knowledge base was queried.
    source_job_id uuid,
    -- Q&A only: the caller. Never set from repository content.
    requested_by uuid,
    -- One per worker pickup, so a pgmq redelivery that runs a job twice stays separable.
    run_id uuid,
    stage text,
    purpose text not null,
    entity_id uuid,
    -- The unit, for Agent 3.
    cluster_id uuid,
    -- Inner parse/validation attempt.
    attempt integer,
    -- Agent 3's outer sandbox-feedback round.
    round integer,
    retry_reason text check (retry_reason in ('parse', 'grounding', 'sandbox_feedback')),
    deployment text,
    response_model text,
    -- Token counts are null when the API did not report usage.
    prompt_tokens integer,
    completion_tokens integer,
    cached_prompt_tokens integer,
    reasoning_tokens integer,
    total_tokens integer,
    -- List-price estimate, not the invoice (design.md §7). Null for an unknown model.
    cost_usd numeric(12, 6),
    pricing_version text,
    latency_ms integer,
    status text not null check (status in ('ok', 'error')),
    -- Exception class name only, never the message.
    error_class text,
    constraint llm_invocations_agent_job_type_check check (
        (agent = 'agent1' and job_type in ('ingestion', 'qa'))
        or (agent = 'agent2' and job_type = 'planning')
        or (agent = 'agent3' and job_type = 'execution')
    ),
    constraint llm_invocations_job_id_check check ((job_type = 'qa') = (job_id is null))
);

create index llm_invocations_job_idx on llm_invocations (job_type, job_id);
create index llm_invocations_org_created_idx on llm_invocations (org_id, created_at);
create index llm_invocations_purpose_idx on llm_invocations (purpose);
create index llm_invocations_org_job_idx on llm_invocations (org_id, job_type, job_id);

alter table llm_invocations enable row level security;
-- Intentionally no policies: see the header comment.

-- llm_job_costs: running per-job totals, upserted by each agent for its own
-- job_type only, from in-memory totals at flush time. Q&A has no job, so no rollup row.
create table llm_job_costs (
    org_id uuid not null,
    agent text not null check (agent in ('agent1', 'agent2', 'agent3')),
    job_type text not null check (job_type in ('ingestion', 'planning', 'execution')),
    job_id uuid not null,
    tokens bigint not null default 0,
    cost_usd numeric(12, 6) not null default 0,
    calls integer not null default 0,
    error_calls integer not null default 0,
    updated_at timestamptz not null default now(),
    unique (job_type, job_id),
    constraint llm_job_costs_agent_job_type_check check (
        (agent = 'agent1' and job_type = 'ingestion')
        or (agent = 'agent2' and job_type = 'planning')
        or (agent = 'agent3' and job_type = 'execution')
    )
);

create index llm_job_costs_org_idx on llm_job_costs (org_id, job_type, job_id);

alter table llm_job_costs enable row level security;
-- Intentionally no policies: see the header comment.

-- Read-only views. security_invoker = true so they run with the caller's rights
-- (and the caller's RLS), never the view owner's. The repo had no views before.
-- cost_usd sums ignore null rows; unpriced_calls counts successful calls with an
-- unknown price (error calls have no price by nature), so a consumer can show "unknown" instead of a silently low total.

create view llm_cost_by_job with (security_invoker = true) as
select
    org_id, agent, job_type, job_id,
    count(*) as calls,
    count(*) filter (where status = 'error') as error_calls,
    count(*) filter (where status = 'ok' and cost_usd is null) as unpriced_calls,
    coalesce(sum(total_tokens), 0) as tokens,
    coalesce(sum(cost_usd), 0) as cost_usd
from llm_invocations
group by org_id, agent, job_type, job_id;

create view llm_cost_by_job_stage with (security_invoker = true) as
select
    org_id, agent, job_type, job_id, stage,
    count(*) as calls,
    count(*) filter (where status = 'error') as error_calls,
    count(*) filter (where status = 'ok' and cost_usd is null) as unpriced_calls,
    coalesce(sum(total_tokens), 0) as tokens,
    coalesce(sum(cost_usd), 0) as cost_usd
from llm_invocations
group by org_id, agent, job_type, job_id, stage;

create view llm_cost_by_job_purpose with (security_invoker = true) as
select
    org_id, agent, job_type, job_id, purpose,
    count(*) as calls,
    count(*) filter (where status = 'error') as error_calls,
    count(*) filter (where status = 'ok' and cost_usd is null) as unpriced_calls,
    coalesce(sum(total_tokens), 0) as tokens,
    coalesce(sum(cost_usd), 0) as cost_usd
from llm_invocations
group by org_id, agent, job_type, job_id, purpose;

-- Agent 3 only: one row per unit (cluster_id) of an execution job.
create view llm_cost_by_execution_unit with (security_invoker = true) as
select
    org_id, job_id, cluster_id,
    count(*) as calls,
    count(distinct round) as rounds,
    coalesce(max(attempt), 0) as max_attempt,
    count(*) filter (where status = 'error') as error_calls,
    count(*) filter (where status = 'ok' and cost_usd is null) as unpriced_calls,
    coalesce(sum(total_tokens), 0) as tokens,
    coalesce(sum(cost_usd), 0) as cost_usd
from llm_invocations
where job_type = 'execution'
group by org_id, job_id, cluster_id;

-- Internal only: no tenant role may touch these, and the invocation log is append-only.
revoke all on llm_invocations, llm_job_costs from anon, authenticated;
revoke all on llm_cost_by_job, llm_cost_by_job_stage, llm_cost_by_job_purpose,
    llm_cost_by_execution_unit from anon, authenticated;
revoke update, delete on llm_invocations from service_role;

-- Summary-cache effectiveness of an ingestion run. These are counts, not money,
-- so they may live on the org-readable job row; they explain why a re-ingestion
-- was cheap.
alter table ingestion_jobs
    add column summary_cache_hits integer not null default 0,
    add column summary_cache_misses integer not null default 0;
