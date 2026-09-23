"""Embedding model pin drift check — design.md §2 "Pinned embedding model (decided)",
CLAUDE.md "Embedding model is pinned, not hardcoded ... If it ever changes, that's an
explicit migration, not a silent update", tasks.md Phase 6.

`embedding_config` is the declared source of truth for which model/revision/dimension
is "active"; `embed.py`'s EMBEDDING_MODEL_NAME/EMBEDDING_MODEL_REVISION/EMBEDDING_DIMENSION
constants are what the worker actually loads and runs. Nothing keeps those two in sync
automatically — if someone flips `embedding_config.active` to a new row (or edits one in
place) without also updating embed.py and, if the dimension changed, migrating
`knowledge_summaries.embedding`'s column type and backfilling every existing row, the
worker would carry on embedding under the *old* pin while the DB claims a different one
is active. That's exactly the silent drift CLAUDE.md warns against.

design.md §2's `embedding_config.dimension` column note is the decision this module
enforces: a dimension change is a schema migration (the `vector(N)` column itself must
change type, so nothing can even be written until that runs) plus a full backfill (every
existing embedding was produced by the old model and is unusable at the new dimension); a
model/version change at the *same* dimension doesn't break the column type, but every
existing row is still an embedding from a now-superseded model — that's a data-only
backfill (re-embed, no schema change) rather than a schema migration.

This module only detects and reports drift — it never re-embeds anything itself. Callers
(assert_embedding_config_in_sync, wired into worker/run.py's startup) are expected to
refuse to proceed rather than silently continue.
"""

from dataclasses import dataclass
from typing import Literal

from supabase import Client

from document_analyzer.worker.embed import EMBEDDING_DIMENSION, EMBEDDING_MODEL_NAME, EMBEDDING_MODEL_REVISION

DriftKind = Literal["in_sync", "schema_migration_required", "data_backfill_required"]


class EmbeddingConfigDriftError(Exception):
    """Raised by assert_embedding_config_in_sync — meant to stop the worker cold, never
    to be caught and suppressed anywhere."""


@dataclass(frozen=True)
class EmbeddingConfigStatus:
    drift: DriftKind
    active_model_name: str
    active_model_version: str
    active_dimension: int


def check_embedding_config(client: Client) -> EmbeddingConfigStatus:
    """Reads the active embedding_config row and classifies it against what embed.py
    actually loads. Dimension mismatch wins over a model/version mismatch when both are
    present — a dimension change already implies the model changed too, and the schema
    migration is the more disruptive, more blocking requirement of the two."""
    result = (
        client.table("embedding_config")
        .select("model_name, model_version, dimension")
        .eq("active", True)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise EmbeddingConfigDriftError(
            "no active embedding_config row found — see design.md §2 'Pinned embedding model'"
        )
    row = result.data[0]

    if row["dimension"] != EMBEDDING_DIMENSION:
        drift: DriftKind = "schema_migration_required"
    elif row["model_name"] != EMBEDDING_MODEL_NAME or row["model_version"] != EMBEDDING_MODEL_REVISION:
        drift = "data_backfill_required"
    else:
        drift = "in_sync"

    return EmbeddingConfigStatus(
        drift=drift,
        active_model_name=row["model_name"],
        active_model_version=row["model_version"],
        active_dimension=row["dimension"],
    )


def assert_embedding_config_in_sync(client: Client) -> None:
    """Call at worker startup, before any job is processed. Raises EmbeddingConfigDriftError
    rather than let the worker silently embed under a config that's drifted from
    embedding_config's declared active row."""
    status = check_embedding_config(client)

    if status.drift == "schema_migration_required":
        raise EmbeddingConfigDriftError(
            f"embedding_config's active dimension ({status.active_dimension}) does not match "
            f"the pinned model's dimension ({EMBEDDING_DIMENSION}) in embed.py — this needs a schema "
            "migration (ALTER knowledge_summaries.embedding's column type) and a full backfill of every "
            "existing row before the worker can run under this config. See design.md §2."
        )
    if status.drift == "data_backfill_required":
        raise EmbeddingConfigDriftError(
            f"embedding_config's active model ({status.active_model_name}@{status.active_model_version}) "
            f"does not match what embed.py actually loads ({EMBEDDING_MODEL_NAME}@{EMBEDDING_MODEL_REVISION}) "
            "at the same dimension — every existing knowledge_summaries.embedding row was produced by a "
            "different model and needs a data-only backfill (re-embed, no schema change) before this is "
            "safe to run. See design.md §2."
        )
