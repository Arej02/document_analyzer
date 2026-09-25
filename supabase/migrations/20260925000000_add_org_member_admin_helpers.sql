-- Admin helpers for org membership provisioning — design.md §1a.
--
-- There is no self-serve signup flow yet: a new auth.users row grants no
-- access to any org until a row exists in org_members (current_org_id()
-- returns null otherwise and every RLS policy denies access). These
-- functions are the operational path for provisioning that row, callable
-- only via the service-role client (backend/document_analyzer/admin/add_org_member.py) —
-- never exposed to `authenticated`, since they read auth.users by email and
-- bypass org scoping by design.

create or replace function add_org_member_by_email(p_email text, p_org_id uuid)
returns uuid
language plpgsql
security definer
set search_path = public, auth
as $$
declare
    v_user_id uuid;
begin
    select id into v_user_id from auth.users where email = p_email;
    if v_user_id is null then
        raise exception 'No auth.users row found for email %; they must sign up first', p_email;
    end if;

    insert into org_members (user_id, org_id)
    values (v_user_id, p_org_id)
    on conflict (user_id) do update set org_id = excluded.org_id;

    return v_user_id;
end;
$$;

revoke all on function add_org_member_by_email(text, uuid) from public;
grant execute on function add_org_member_by_email(text, uuid) to service_role;

create or replace function list_org_members()
returns table (user_id uuid, email text, org_id uuid, created_at timestamptz)
language sql
security definer
stable
set search_path = public, auth
as $$
    select m.user_id, u.email, m.org_id, m.created_at
    from org_members m
    join auth.users u on u.id = m.user_id
    order by m.created_at;
$$;

revoke all on function list_org_members() from public;
grant execute on function list_org_members() to service_role;
