-- Restores current_org_id() to the version in 20260918010000_rls_policies.sql
-- (design.md §1a). It had been overwritten directly on the live database
-- (untracked, no migration) to gate on app_private.sessions / a custom
-- `app_session_id` JWT claim from a teammate's in-progress backend-managed
-- session experiment. The frontend authenticates via real Supabase Auth
-- only, which never sets that claim, so the swapped-in version made
-- current_org_id() return null for every user — a full auth outage, not
-- just for new signups.

create or replace function current_org_id()
returns uuid
language sql
security definer
stable
set search_path = public
as $$
    select org_id from org_members where user_id = auth.uid()
$$;
