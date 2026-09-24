-- Agent 2 overrides → new plan version — design_agent2.md §2, §9.2.
-- Links each plan_overrides row to the version it produced. A version is rebuilt
-- by replaying its override chain from version 1, and two overrides made against
-- the same version n (two branches) must not bleed into each other's chain, so
-- plan_id (the version it was made against) alone isn't enough.
-- The API inserts the new version first (service role), then the override with
-- result_plan_id set, through the caller's RLS-scoped client (existing insert
-- policy), so no update policy is needed.

alter table plan_overrides
    add column result_plan_id uuid references migration_plans (id);

-- One override produces exactly one version.
create unique index plan_overrides_result_plan_id_key on plan_overrides (result_plan_id);
