"""Assessment Report prerequisite: unit_executions.started_at / finished_at."""

from tests.fakes import FakeClient
from document_analyzer.agent3.worker.status import mark_unit_running, update_unit_execution


def _client():
    client = FakeClient()
    client.table("unit_executions").rows.append(
        {"id": "u1", "execution_job_id": "j", "cluster_id": "c", "org_id": "o", "status": "pending", "started_at": None}
    )
    return client


def test_started_at_is_set_once_and_kept_across_redelivery():
    client = _client()
    mark_unit_running(client, "j", "o", "c")
    first = client.table("unit_executions").rows[0]["started_at"]
    assert first
    mark_unit_running(client, "j", "o", "c")
    assert client.table("unit_executions").rows[0]["started_at"] == first


def test_finished_at_only_on_a_terminal_verdict():
    client = _client()
    mark_unit_running(client, "j", "o", "c")
    update_unit_execution(client, "j", "o", "c", attempts=1)
    assert not client.table("unit_executions").rows[0].get("finished_at")
    update_unit_execution(client, "j", "o", "c", status="passed")
    assert client.table("unit_executions").rows[0]["finished_at"]
