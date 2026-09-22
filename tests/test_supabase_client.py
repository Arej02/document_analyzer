import pytest

from document_analyzer.supabase_client import (
    get_request_scoped_client,
    get_service_role_client,
    insert_scoped,
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
