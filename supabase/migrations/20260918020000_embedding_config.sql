-- Pin the embedding model — design.md §2 "Pinned embedding model (decided)".
-- model_version is the exact HF model revision (git commit sha), not "main",
-- so the pin is reproducible.

insert into embedding_config (model_name, model_version, dimension, active)
values (
    'sentence-transformers/all-MiniLM-L6-v2',
    '1110a243fdf4706b3f48f1d95db1a4f5529b4d41',
    384,
    true
);

-- knowledge_summaries.embedding was left as an unconstrained `vector` in the
-- initial schema migration pending this pin (design.md §2 dimension note).
alter table knowledge_summaries
    alter column embedding type vector(384);
