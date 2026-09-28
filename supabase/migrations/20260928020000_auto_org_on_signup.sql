-- design.md §1a: signing up is the only step. Every new auth.users row gets its own
-- org (a fresh org_id in org_members), so a new user never lands in someone else's
-- data. Sharing an org with teammates is still the service-role-only
-- add_org_member_by_email (20260925000000_add_org_member_admin_helpers.sql).

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
    insert into public.org_members (user_id, org_id)
    values (new.id, gen_random_uuid())
    on conflict (user_id) do nothing;
    return new;
end;
$$;

revoke all on function public.handle_new_user() from public;

create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- Accounts created before this migration get their own org too.
insert into public.org_members (user_id, org_id)
select id, gen_random_uuid() from auth.users
on conflict (user_id) do nothing;
