-- Agent 2: proposed target-stack equivalents for a repo's declared packages
-- (design_agent2.md §2 `plan_dependency_targets`, §6.4, §16).
--
-- Written only by the planner worker (service role). Tenants can select their own
-- org's rows and nothing else. Derived data: it cascades from migration_plans, so a
-- re-run's delete_unfinished_plan and a project delete remove it with the plan.

create table plan_dependency_targets (
    plan_id uuid not null references migration_plans (id) on delete cascade,
    entity_id uuid not null references extracted_entities (id),
    org_id uuid not null,
    target_name text not null,
    rationale text,
    grounding_status grounding_status not null default 'unverified',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (plan_id, entity_id)
);

create index plan_dependency_targets_org_id_idx on plan_dependency_targets (org_id);
create index plan_dependency_targets_entity_id_idx on plan_dependency_targets (entity_id);

alter table plan_dependency_targets enable row level security;

create policy plan_dependency_targets_select_own_org
    on plan_dependency_targets for select
    to authenticated
    using (org_id = current_org_id());

revoke insert, update, delete on plan_dependency_targets from anon, authenticated;
