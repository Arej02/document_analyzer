-- Repoints Agent 2's three user columns back to auth.users(id), matching
-- 20260923010000_agent2_plan_tables.sql and design_agent2.md §2.
--
-- The same out-of-band app_private.accounts experiment that
-- 20260925010000_org_members_fk_back_to_auth_users.sql reverted for org_members
-- had also repointed these three FKs, and that fix missed them. With them
-- pointing at app_private.accounts, approving a plan, deciding a phase or
-- making an override fails for every Supabase Auth user (23503), because the
-- API writes auth.uid() into these columns.
--
-- All three columns were empty on the live project when this was written
-- (2026-09-25), so re-validating the FKs against auth.users can't fail on
-- existing rows. Nothing else references app_private; the schema itself is
-- left in place.

alter table migration_plans
    drop constraint migration_plans_approved_by_fkey,
    add constraint migration_plans_approved_by_fkey
        foreign key (approved_by) references auth.users (id);

alter table phase_approvals
    drop constraint phase_approvals_reviewer_id_fkey,
    add constraint phase_approvals_reviewer_id_fkey
        foreign key (reviewer_id) references auth.users (id);

alter table plan_overrides
    drop constraint plan_overrides_created_by_fkey,
    add constraint plan_overrides_created_by_fkey
        foreign key (created_by) references auth.users (id);
