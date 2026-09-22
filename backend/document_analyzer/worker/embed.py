"""Local embedding generation for `knowledge_summaries.embedding` — tasks.md
Phase 3.

**Pinned embedding model** (design.md §2 "Pinned embedding model (decided)",
CLAUDE.md "Embedding model is pinned, not hardcoded"): `sentence-transformers/
all-MiniLM-L6-v2` at revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`,
dimension 384 — the exact pin already recorded in the `embedding_config` table
(`supabase/migrations/20260918020000_embedding_config.sql`) and mirrored here
in code, not the other way around: the DB row is the source of truth for
*what's pinned*, this module is what actually loads that pin. Runs entirely
on-CPU, locally — unlike the Azure OpenAI summarization calls, text embedded
here never leaves this infra (that's *why* the embedding model, specifically,
was pinned to a local model — see design.md §2). Changing `EMBEDDING_MODEL_*`
below without also migrating `embedding_config` (and every existing
`knowledge_summaries.embedding` row, if the dimension changes) is exactly the
silent-drift CLAUDE.md warns against — don't.

The `sentence-transformers` import is deferred into `get_embedding_model()`
so merely importing this module (e.g. from a unit test that injects a fake
embed function) never pulls in torch.
"""

import functools

EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
EMBEDDING_DIMENSION = 384


@functools.lru_cache(maxsize=1)
def get_embedding_model():
    """Lazily loads and caches the pinned local embedding model (one load per worker
    process, not per call)."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBEDDING_MODEL_NAME, revision=EMBEDDING_MODEL_REVISION)


def embed_text(text: str, model=None) -> list[float]:
    """Embeds one piece of text with the pinned local model. `model` is injectable so
    callers (and tests) can avoid loading the real model."""
    model = model or get_embedding_model()
    return model.encode(text, convert_to_numpy=True).tolist()
