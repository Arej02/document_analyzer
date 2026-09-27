                                                           -- Storage for Agent 3's generated code, tests, logs and the assembled project zip
-- — design_agent3.md §2 "Storage". Private bucket; objects are stored under
-- `{org_id}/{execution_job_id}/...` so RLS can scope reads the same way it scopes
-- every other table (pattern: 20260918040000_agent1_uploads_bucket.sql).
-- Only the service-role executor worker writes here, so authenticated users get a
-- select policy and nothing else: no insert, update or delete policies. Downloads
-- go through short-lived signed URLs (GET /api/agent3/executions/{id}/download).

insert into storage.buckets (id, name, public)
values ('agent3-output', 'agent3-output', false)
on conflict (id) do nothing;

create policy agent3_output_select_own_org
    on storage.objects for select
    to authenticated
    using (
        bucket_id = 'agent3-output'
        and (storage.foldername(name))[1] = current_org_id()::text
    );
