-- Boilerplate filter's audit log — design.md §2 `filtered_files`, specs.md FR3
-- ("Filtered files must be logged ... auditable, not silent"). Same shape/RLS
-- pattern as extracted_entities/entity_edges: worker (service-role) writes,
-- authenticated users read their own org's rows.

create table filtered_files (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    job_id uuid not null references ingestion_jobs (id),
    file_path text not null,
    reason text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index filtered_files_org_id_idx on filtered_files (org_id);
create index filtered_files_job_id_idx on filtered_files (job_id);

alter table filtered_files enable row level security;

create policy filtered_files_select_own_org
    on filtered_files for select
    to authenticated
    using (org_id = current_org_id());
