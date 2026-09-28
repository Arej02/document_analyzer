import pytest
from fastapi.testclient import TestClient

from document_analyzer.app import create_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://postgres.abcref123:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-key")
    return TestClient(create_app())


def test_config_serves_derived_url_and_anon_key_without_auth(client):
    response = client.get("/api/config")

    assert response.status_code == 200
    assert response.json() == {"supabase_url": "https://abcref123.supabase.co", "supabase_anon_key": "anon-key"}
    assert "service-role-key" not in response.text


def test_config_is_503_without_anon_key(client, monkeypatch):
    monkeypatch.delenv("SUPABASE_ANON_KEY")

    response = client.get("/api/config")

    assert response.status_code == 503
    assert "SUPABASE_ANON_KEY" in response.json()["detail"]


def test_config_is_503_when_project_url_is_underivable(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://postgres:pw@localhost/postgres")

    response = client.get("/api/config")

    assert response.status_code == 503
