-- Source evidence, edge provenance and extraction coverage — design.md §2
-- (`source_chunks`, `source_chunk_entities`, `entity_edges.provenance`,
-- `ingestion_jobs.extraction_coverage`) and §4 (`POST /jobs/{job_id}/ask`).
-- Merged from Galvanise's chunks / chunk_entities / pgvector embeddings.

-- Where an edge came from. {} for the PHP-family extractors (every edge is a
-- directly parsed fact); GraphifyExtractor records the upstream relation,
-- EXTRACTED/INFERRED confidence and score, file and line.
alter table entity_edges
    add column provenance jsonb not null default '{}'::jsonb;

-- What extraction and evidence actually covered for this job (bounded report:
-- counts plus capped samples, never the full file list).
alter table ingestion_jobs
    add column extraction_coverage jsonb;

-- Bounded, secret-redacted excerpts of the job's source (worker/evidence.py).
-- Written by the service-role worker only, read by authenticated users of the org.
create table source_chunks (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    job_id uuid not null references ingestion_jobs (id),
    file_path text not null,
    line_start int not null check (line_start >= 1),
    line_end int not null check (line_end >= line_start),
    strategy text not null check (strategy in ('entity_range', 'entity_range_part', 'source_window')),
    content text not null,
    content_hash text not null,
    token_estimate int not null,
    redactions jsonb not null default '[]'::jsonb,
    -- same pinned model and dimension as knowledge_summaries.embedding (embedding_config)
    embedding vector(384),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index source_chunks_org_id_idx on source_chunks (org_id);
create index source_chunks_job_id_idx on source_chunks (job_id);
create index source_chunks_job_file_idx on source_chunks (job_id, file_path, line_start);

-- Chunk <-> entity links, by file path and line numbers only (never by name).
create table source_chunk_entities (
    id uuid primary key default gen_random_uuid(),
    org_id uuid not null,
    job_id uuid not null references ingestion_jobs (id),
    chunk_id uuid not null references source_chunks (id) on delete cascade,
    entity_id uuid not null references extracted_entities (id),
    association text not null check (association in ('entity_range', 'starts_in')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index source_chunk_entities_org_id_idx on source_chunk_entities (org_id);
create index source_chunk_entities_job_id_idx on source_chunk_entities (job_id);
create index source_chunk_entities_chunk_id_idx on source_chunk_entities (chunk_id);
create index source_chunk_entities_entity_id_idx on source_chunk_entities (entity_id);

alter table source_chunks enable row level security;

create policy source_chunks_select_own_org
    on source_chunks for select
    to authenticated
    using (org_id = current_org_id());

alter table source_chunk_entities enable row level security;

create policy source_chunk_entities_select_own_org
    on source_chunk_entities for select
    to authenticated
    using (org_id = current_org_id());

-- Exact cosine search over one job's chunks. SECURITY INVOKER, so the caller's
-- RLS policy above still decides which rows exist: a user can only ever match
-- their own org's chunks, whatever job id they pass.
create function match_source_chunks(p_job_id uuid, p_query vector(384), p_match_count int)
returns table (
    id uuid,
    file_path text,
    line_start int,
    line_end int,
    strategy text,
    content text,
    token_estimate int,
    distance double precision
)
language sql
stable
security invoker
set search_path = public, extensions
as $$
    select c.id, c.file_path, c.line_start, c.line_end, c.strategy, c.content, c.token_estimate,
           c.embedding <=> p_query as distance
    from source_chunks c
    where c.job_id = p_job_id and c.embedding is not null
    order by c.embedding <=> p_query
    limit least(greatest(p_match_count, 1), 100)
$$;

grant execute on function match_source_chunks(uuid, vector, int) to authenticated;
