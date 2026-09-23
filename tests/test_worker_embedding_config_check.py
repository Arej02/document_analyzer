"""tasks.md Phase 6: "Embedding model pin: confirm changing embedding_config
(including a dimension change) triggers the correct migration path — schema
migration if dimension changes, data-only backfill otherwise — not silent drift."

embedding_config_check.py is the mechanism this confirms: it compares the DB's
active embedding_config row against what embed.py actually loads, and classifies
any mismatch as needing a schema migration (dimension changed) or a data-only
backfill (model/version changed at the same dimension) — see that module's
docstring for the full reasoning. Most of this is a pure classification function,
tested here with tests/fakes.py::FakeClient like the rest of the worker suite;
the one live case confirms the *actual* configured project isn't already
drifted from what's pinned in code today.
"""

import os

import pytest
from dotenv import load_dotenv

load_dotenv()

from document_analyzer.supabase_client import get_service_role_client  # noqa: E402
from document_analyzer.worker.embed import EMBEDDING_DIMENSION, EMBEDDING_MODEL_NAME, EMBEDDING_MODEL_REVISION  # noqa: E402
from document_analyzer.worker.embedding_config_check import (  # noqa: E402
    EmbeddingConfigDriftError,
    assert_embedding_config_in_sync,
    check_embedding_config,
)

from fakes import FakeClient  # noqa: E402


@pytest.fixture
def fake_client():
    return FakeClient(org_id="org-123")


def _seed_active_config(fake_client, *, model_name=None, model_version=None, dimension=None):
    fake_client.table("embedding_config").insert(
        {
            "model_name": model_name or EMBEDDING_MODEL_NAME,
            "model_version": model_version or EMBEDDING_MODEL_REVISION,
            "dimension": dimension if dimension is not None else EMBEDDING_DIMENSION,
            "active": True,
        }
    ).execute()


def test_matching_config_is_in_sync(fake_client):
    _seed_active_config(fake_client)

    status = check_embedding_config(fake_client)

    assert status.drift == "in_sync"
    assert_embedding_config_in_sync(fake_client)  # doesn't raise


def test_dimension_change_requires_schema_migration(fake_client):
    _seed_active_config(fake_client, dimension=768)

    status = check_embedding_config(fake_client)
    assert status.drift == "schema_migration_required"

    with pytest.raises(EmbeddingConfigDriftError, match="schema migration"):
        assert_embedding_config_in_sync(fake_client)


def test_model_name_change_at_same_dimension_requires_data_backfill(fake_client):
    _seed_active_config(fake_client, model_name="sentence-transformers/all-mpnet-base-v2")

    status = check_embedding_config(fake_client)
    assert status.drift == "data_backfill_required"

    with pytest.raises(EmbeddingConfigDriftError, match="data-only backfill"):
        assert_embedding_config_in_sync(fake_client)


def test_model_version_change_at_same_dimension_requires_data_backfill(fake_client):
    _seed_active_config(fake_client, model_version="deadbeef")

    status = check_embedding_config(fake_client)

    assert status.drift == "data_backfill_required"


def test_dimension_and_model_both_changed_reports_schema_migration_not_backfill(fake_client):
    """A dimension change already implies the model changed too — the schema migration
    is the more disruptive requirement of the two, so it should win, not be masked by
    reporting the lesser "just a backfill" fix."""
    _seed_active_config(fake_client, model_name="a-different-model", dimension=999)

    status = check_embedding_config(fake_client)

    assert status.drift == "schema_migration_required"


def test_no_active_row_raises(fake_client):
    with pytest.raises(EmbeddingConfigDriftError, match="no active embedding_config row"):
        check_embedding_config(fake_client)


def test_inactive_row_is_not_considered(fake_client):
    fake_client.table("embedding_config").insert(
        {"model_name": EMBEDDING_MODEL_NAME, "model_version": EMBEDDING_MODEL_REVISION, "dimension": EMBEDDING_DIMENSION, "active": False}
    ).execute()

    with pytest.raises(EmbeddingConfigDriftError, match="no active embedding_config row"):
        check_embedding_config(fake_client)


_LIVE_ENV_VARS = ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"]


@pytest.mark.skipif(
    not all(os.environ.get(name) for name in _LIVE_ENV_VARS),
    reason="requires a live Supabase project (SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY)",
)
def test_the_live_projects_active_config_is_not_already_drifted():
    """Confirms today's actual deployed embedding_config row still matches what
    embed.py loads — i.e. nobody has silently drifted it since the pin was set."""
    service_client = get_service_role_client()

    status = check_embedding_config(service_client)

    assert status.drift == "in_sync"
    assert_embedding_config_in_sync(service_client)  # doesn't raise
