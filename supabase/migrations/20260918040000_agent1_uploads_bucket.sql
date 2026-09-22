-- Storage for zip/tar uploads backing POST /api/agent1/jobs — design.md §2a.
-- Private bucket; objects are stored under `{org_id}/{job_id}/{filename}` so RLS
-- can scope access the same way it scopes every other table.

insert into storage.buckets (id, name, public)
values ('agent1-uploads', 'agent1-uploads', false)
on conflict (id) do nothing;

create policy agent1_uploads_insert_own_org
    on storage.objects for insert
    to authenticated
    with check (
        bucket_id = 'agent1-uploads'
        and (storage.foldername(name))[1] = current_org_id()::text
    );

create policy agent1_uploads_select_own_org
    on storage.objects for select
    to authenticated
    using (
        bucket_id = 'agent1-uploads'
        and (storage.foldername(name))[1] = current_org_id()::text
    );
