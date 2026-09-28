import pytest

from document_analyzer.supabase_client import (
    get_request_scoped_client,
    get_service_role_client,
    insert_many_scoped,
    insert_scoped,
    project_url_from_db_url,
    supabase_url,
)


@pytest.fixture(autouse=True)
def supabase_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-key")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")


def test_service_role_and_request_scoped_clients_use_different_keys():
    service_client = get_service_role_client()
    request_client = get_request_scoped_client("user-jwt")

    assert service_client.supabase_key == "service-role-key"
    assert request_client.supabase_key == "anon-key"


def test_request_scoped_client_carries_the_caller_jwt():
    request_client = get_request_scoped_client("user-jwt")

    # Must check the actual httpx session headers (what's sent on the wire), not
    # postgrest.headers — client.postgrest.auth() only updates the latter, which
    # is a documented gotcha this client module works around.
    assert request_client.postgrest.session.headers["authorization"] == "Bearer user-jwt"


class _FakeTable:
    def __init__(self, recorder, name):
        self._recorder = recorder
        self._name = name

    def insert(self, row):
        self._recorder.append((self._name, row))
        return self

    def execute(self):
        return self._recorder[-1]


class _FakeClient:
    def __init__(self):
        self.inserted = []

    def table(self, name):
        return _FakeTable(self.inserted, name)


def test_insert_scoped_stamps_org_id_from_argument():
    client = _FakeClient()

    insert_scoped(client, "ingestion_jobs", "org-123", {"status": "queued"})

    assert client.inserted == [("ingestion_jobs", {"status": "queued", "org_id": "org-123"})]


def test_insert_scoped_rejects_org_id_already_in_row():
    client = _FakeClient()

    with pytest.raises(ValueError):
        insert_scoped(client, "ingestion_jobs", "org-123", {"org_id": "org-from-content"})

    assert client.inserted == []


def test_insert_many_scoped_stamps_org_id_on_every_row():
    client = _FakeClient()

    insert_many_scoped(client, "extracted_entities", "org-123", [{"name": "a"}, {"name": "b"}])

    assert client.inserted == [
        ("extracted_entities", [{"name": "a", "org_id": "org-123"}, {"name": "b", "org_id": "org-123"}])
    ]


def test_insert_many_scoped_rejects_org_id_already_in_any_row():
    """Same guard as insert_scoped, checked across the whole batch — the defense-in-depth
    that stops a future caller from ever letting untrusted content smuggle an org_id key
    in at the top level (CLAUDE.md: "any bug here is a cross-tenant leak")."""
    client = _FakeClient()

    with pytest.raises(ValueError):
        insert_many_scoped(client, "extracted_entities", "org-123", [{"name": "a"}, {"org_id": "org-from-content"}])

    assert client.inserted == []


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres.abcref123:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres",
        "postgresql://postgres.abcref123:pw@aws-0-us-east-1.pooler.supabase.com:6543/postgres",
        "postgresql://postgres:pw@db.abcref123.supabase.co:5432/postgres",
    ],
)
def test_project_url_is_derived_from_supabase_connection_strings(dsn):
    assert project_url_from_db_url(dsn) == "https://abcref123.supabase.co"


def test_project_url_cannot_be_derived_from_a_non_supabase_dsn():
    with pytest.raises(RuntimeError, match="Set SUPABASE_URL"):
        project_url_from_db_url("postgresql://postgres:pw@localhost:5432/postgres")


def test_explicit_supabase_url_wins_over_the_derived_one(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://postgres:pw@db.otherref.supabase.co/postgres")

    assert supabase_url() == "https://example.supabase.co"


def test_supabase_url_falls_back_to_the_connection_string(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://postgres.abcref123:pw@aws-0-eu-west-1.pooler.supabase.com:5432/postgres")

    assert supabase_url() == "https://abcref123.supabase.co"
    assert str(get_service_role_client().supabase_url).rstrip("/") == "https://abcref123.supabase.co"
