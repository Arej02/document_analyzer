-- Storage for post-boilerplate-strip source tarballs — Agent 3 prerequisite
-- (design_agent3.md §13, design.md §2a). Agent 1 otherwise throws the fetched
-- source away once extraction finishes; Agent 3 needs it to translate.
--
-- Private bucket; objects are `{org_id}/{job_id}/source.tar.gz`, written only
-- by the worker's service-role connection (bypasses RLS) — unlike
-- agent1-uploads, there is no authenticated `insert` policy, since nothing
-- outside the worker ever writes here.

insert into storage.buckets (id, name, public)
values ('agent1-snapshots', 'agent1-snapshots', false)
on conflict (id) do nothing;

drop policy if exists agent1_snapshots_select_own_org on storage.objects;
create policy agent1_snapshots_select_own_org
    on storage.objects for select
    to authenticated
    using (
        bucket_id = 'agent1-snapshots'
        and (storage.foldername(name))[1] = current_org_id()::text
    );

alter table ingestion_jobs
    add column if not exists source_snapshot_path text;
