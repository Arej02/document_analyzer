import pytest

from document_analyzer import queue as queue_module


class _FakeConn:
    def __init__(self, recorder, fetchrow_result=None):
        self._recorder = recorder
        self._fetchrow_result = fetchrow_result

    async def fetchval(self, sql, *args):
        self._recorder.append((sql, args))
        return None

    async def fetchrow(self, sql, *args):
        self._recorder.append((sql, args))
        return self._fetchrow_result

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def db_url_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://user:pass@localhost:5432/postgres")


async def test_enqueue_ingestion_job_sends_to_pgmq(monkeypatch):
    calls = []

    async def fake_connect(dsn):
        assert dsn == "postgresql://user:pass@localhost:5432/postgres"
        return _FakeConn(calls)

    monkeypatch.setattr(queue_module.asyncpg, "connect", fake_connect)

    await queue_module.enqueue_ingestion_job(job_id="job-1", org_id="org-1")

    assert len(calls) == 1
    sql, args = calls[0]
    assert "pgmq.send" in sql
    assert args[0] == queue_module.QUEUE_NAME
    assert '"job_id": "job-1"' in args[1]
    assert '"org_id": "org-1"' in args[1]


async def test_read_next_job_returns_none_on_empty_queue(monkeypatch):
    async def fake_connect(dsn):
        return _FakeConn([], fetchrow_result=None)

    monkeypatch.setattr(queue_module.asyncpg, "connect", fake_connect)

    assert await queue_module.read_next_job() is None


async def test_read_next_job_parses_the_message_payload(monkeypatch):
    calls = []

    async def fake_connect(dsn):
        return _FakeConn(calls, fetchrow_result={"msg_id": 42, "message": '{"job_id": "job-1", "org_id": "org-1"}'})

    monkeypatch.setattr(queue_module.asyncpg, "connect", fake_connect)

    result = await queue_module.read_next_job(vt_seconds=60)

    assert result == {"msg_id": 42, "job_id": "job-1", "org_id": "org-1"}
    sql, args = calls[0]
    assert "pgmq.read" in sql
    assert args == (queue_module.QUEUE_NAME, 60)


async def test_delete_job_message_sends_msg_id(monkeypatch):
    calls = []

    async def fake_connect(dsn):
        return _FakeConn(calls)

    monkeypatch.setattr(queue_module.asyncpg, "connect", fake_connect)

    await queue_module.delete_job_message(42)

    sql, args = calls[0]
    assert "pgmq.delete" in sql
    assert args == (queue_module.QUEUE_NAME, 42)


async def test_enqueue_planning_job_sends_to_agent2_queue(monkeypatch):
    calls = []

    async def fake_connect(dsn):
        return _FakeConn(calls)

    monkeypatch.setattr(queue_module.asyncpg, "connect", fake_connect)

    await queue_module.enqueue_planning_job(planning_job_id="pj-1", org_id="org-1")

    sql, args = calls[0]
    assert "pgmq.send" in sql
    assert args[0] == "agent2_planning_jobs"
    assert args[0] != queue_module.QUEUE_NAME
    assert '"planning_job_id": "pj-1"' in args[1]
    assert '"org_id": "org-1"' in args[1]


async def test_read_next_planning_job_returns_none_on_empty_queue(monkeypatch):
    async def fake_connect(dsn):
        return _FakeConn([], fetchrow_result=None)

    monkeypatch.setattr(queue_module.asyncpg, "connect", fake_connect)

    assert await queue_module.read_next_planning_job() is None


async def test_read_next_planning_job_parses_the_message_payload(monkeypatch):
    calls = []

    async def fake_connect(dsn):
        return _FakeConn(
            calls, fetchrow_result={"msg_id": 7, "message": '{"planning_job_id": "pj-1", "org_id": "org-1"}'}
        )

    monkeypatch.setattr(queue_module.asyncpg, "connect", fake_connect)

    result = await queue_module.read_next_planning_job(vt_seconds=60)

    assert result == {"msg_id": 7, "planning_job_id": "pj-1", "org_id": "org-1"}
    sql, args = calls[0]
    assert "pgmq.read" in sql
    assert args == (queue_module.PLANNING_QUEUE_NAME, 60)


async def test_delete_planning_job_message_casts_msg_id_to_bigint(monkeypatch):
    calls = []

    async def fake_connect(dsn):
        return _FakeConn(calls)

    monkeypatch.setattr(queue_module.asyncpg, "connect", fake_connect)

    await queue_module.delete_planning_job_message(7)

    sql, args = calls[0]
    assert "pgmq.delete" in sql
    assert "$2::bigint" in sql
    assert args == (queue_module.PLANNING_QUEUE_NAME, 7)


async def test_enqueue_execution_job_sends_to_agent3_queue(monkeypatch):
    calls = []

    async def fake_connect(dsn):
        return _FakeConn(calls)

    monkeypatch.setattr(queue_module.asyncpg, "connect", fake_connect)

    await queue_module.enqueue_execution_job(execution_job_id="ej-1", org_id="org-1")

    sql, args = calls[0]
    assert "pgmq.send" in sql
    assert args[0] == "agent3_execution_jobs"
    assert args[0] not in (queue_module.QUEUE_NAME, queue_module.PLANNING_QUEUE_NAME)
    assert '"execution_job_id": "ej-1"' in args[1]
    assert '"org_id": "org-1"' in args[1]


async def test_read_next_execution_job_returns_none_on_empty_queue(monkeypatch):
    async def fake_connect(dsn):
        return _FakeConn([], fetchrow_result=None)

    monkeypatch.setattr(queue_module.asyncpg, "connect", fake_connect)

    assert await queue_module.read_next_execution_job() is None


async def test_read_next_execution_job_parses_the_message_payload(monkeypatch):
    calls = []

    async def fake_connect(dsn):
        return _FakeConn(
            calls, fetchrow_result={"msg_id": 9, "message": '{"execution_job_id": "ej-1", "org_id": "org-1"}'}
        )

    monkeypatch.setattr(queue_module.asyncpg, "connect", fake_connect)

    result = await queue_module.read_next_execution_job(vt_seconds=60)

    assert result == {"msg_id": 9, "execution_job_id": "ej-1", "org_id": "org-1"}
    sql, args = calls[0]
    assert "pgmq.read" in sql
    assert args == (queue_module.EXECUTION_QUEUE_NAME, 60)


async def test_read_next_execution_job_defaults_to_the_agent3_visibility_timeout(monkeypatch):
    calls = []

    async def fake_connect(dsn):
        return _FakeConn(calls, fetchrow_result=None)

    monkeypatch.setattr(queue_module.asyncpg, "connect", fake_connect)
    monkeypatch.setattr(queue_module, "DEFAULT_EXECUTION_VISIBILITY_TIMEOUT_SECONDS", 3600)

    await queue_module.read_next_execution_job()

    assert calls[0][1] == (queue_module.EXECUTION_QUEUE_NAME, 3600)


async def test_delete_execution_job_message_casts_msg_id_to_bigint(monkeypatch):
    calls = []

    async def fake_connect(dsn):
        return _FakeConn(calls)

    monkeypatch.setattr(queue_module.asyncpg, "connect", fake_connect)

    await queue_module.delete_execution_job_message(9)

    sql, args = calls[0]
    assert "pgmq.delete" in sql
    assert "$2::bigint" in sql
    assert args == (queue_module.EXECUTION_QUEUE_NAME, 9)
