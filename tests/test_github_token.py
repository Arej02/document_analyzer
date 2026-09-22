import pytest

from document_analyzer import github_token as github_token_module


class _FakeConn:
    def __init__(self, recorder, fetchval_result=None):
        self._recorder = recorder
        self._fetchval_result = fetchval_result

    async def fetchval(self, sql, *args):
        self._recorder.append(("fetchval", sql, args))
        return self._fetchval_result

    async def execute(self, sql, *args):
        self._recorder.append(("execute", sql, args))

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def db_url_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://user:pass@localhost:5432/postgres")


async def test_store_github_token_creates_vault_secret_scoped_to_job(monkeypatch):
    calls = []

    async def fake_connect(dsn):
        return _FakeConn(calls, fetchval_result="secret-uuid-123")

    monkeypatch.setattr(github_token_module.asyncpg, "connect", fake_connect)

    ref = await github_token_module.store_github_token("job-1", "ghp_sekret")

    assert ref == "secret-uuid-123"
    assert len(calls) == 1
    kind, sql, args = calls[0]
    assert kind == "fetchval"
    assert "vault.create_secret" in sql
    assert args[0] == "ghp_sekret"
    assert "job-1" in args[1]  # name scoped to the job
    assert "job-1" in args[2]  # description scoped to the job


async def test_resolve_github_token_reads_decrypted_secret(monkeypatch):
    calls = []

    async def fake_connect(dsn):
        return _FakeConn(calls, fetchval_result="ghp_sekret")

    monkeypatch.setattr(github_token_module.asyncpg, "connect", fake_connect)

    token = await github_token_module.resolve_github_token("secret-uuid-123")

    assert token == "ghp_sekret"
    kind, sql, args = calls[0]
    assert "vault.decrypted_secrets" in sql
    assert args == ("secret-uuid-123",)


async def test_resolve_github_token_returns_none_when_deleted(monkeypatch):
    async def fake_connect(dsn):
        return _FakeConn([], fetchval_result=None)

    monkeypatch.setattr(github_token_module.asyncpg, "connect", fake_connect)

    token = await github_token_module.resolve_github_token("secret-uuid-123")

    assert token is None


async def test_delete_github_token_removes_from_vault(monkeypatch):
    calls = []

    async def fake_connect(dsn):
        return _FakeConn(calls)

    monkeypatch.setattr(github_token_module.asyncpg, "connect", fake_connect)

    await github_token_module.delete_github_token("secret-uuid-123")

    kind, sql, args = calls[0]
    assert kind == "execute"
    assert "delete from vault.secrets" in sql
    assert args == ("secret-uuid-123",)
