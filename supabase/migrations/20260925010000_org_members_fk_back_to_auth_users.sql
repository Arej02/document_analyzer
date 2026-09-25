-- Repoints org_members.user_id back to auth.users(id), matching design.md §1a.
--
-- The live FK had drifted to reference app_private.accounts(id) (a
-- teammate's out-of-band experiment moving accounts/sessions into
-- backend-managed Postgres tables) without ever going through a checked-in
-- migration. This restores the documented, git-tracked shape.

alter table org_members
    drop constraint org_members_user_id_fkey,
    add constraint org_members_user_id_fkey
        foreign key (user_id) references auth.users (id);
