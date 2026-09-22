-- Secret/credential scan's audit log — design.md §2 `secret_findings`,
-- specs.md NFR2 ("the finding is logged for audit with the job/entity
-- reference — a match never fails the entity or blocks the job"). Same
-- shape/RLS pattern as filtered_files: worker (service-role) writes,
-- authenticated users read their own org's rows. Never stores the matched
-- secret value itself, only where (entity + field path) and why (reason).

create table secret_findings (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    job_id uuid not null references ingestion_jobs (id),
    entity_id uuid not null references extracted_entities (id),
    field_path text not null,
    reason text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index secret_findings_org_id_idx on secret_findings (org_id);
create index secret_findings_job_id_idx on secret_findings (job_id);
create index secret_findings_entity_id_idx on secret_findings (entity_id);

alter table secret_findings enable row level security;

create policy secret_findings_select_own_org
    on secret_findings for select
    to authenticated
    using (org_id = current_org_id());
