"""Sandbox abuse suite — tasks_agent3.md Phase 6, second task; design_agent3.md §7.

`test_agent3_sandbox.py`'s live smoke tests prove the container's own
restrictions hold (no network, memory/pids limits, timeout, read-only root)
against hand-written probe code that expects to be blocked. This file instead
runs real, uncooperative "generated unit" code — the shape a hostile or
malfunctioning LLM response could actually take — through the *real* worker
path (`units.py::run_units_stage`, a `FakeLLM` standing in for Azure OpenAI,
the real sandbox), for each of the five vectors tasks_agent3.md names:
network access, a thread/fork bomb, a huge `/tmp` write, reading
`/proc/1/environ` or a host path, and `import ctypes` to attempt a privilege
escalation.

Each attack is planted as *module-level* code in the generated unit — the one
thing every one of these units is guaranteed to execute, since
`units.py::_ensure_package_init` always makes `app/units/{slug}/__init__.py`
import every file the unit wrote (design_agent3.md §4/§8), and the generated
contract test always imports that package. None of the five attacks are
wrapped in a try/except: real hostile code has no reason to gracefully handle
its own failure, so the natural outcome (an uncaught exception, or the
container being killed/timed out) is exactly what should drive the unit to a
terminal, non-`passed` status.

For each vector this file checks:
1. The unit ends `failed_verification` or `error` (specs FR7) — never `passed`.
2. `run_units_stage` returns normally (doesn't hang or raise) — the shape of
   "the job completes" at this layer; the full stage-loop already proves a
   failing unit doesn't fail the *job* (`test_agent3_job_nonblocking.py`).
3. Nothing sensitive leaks into the stored log/junit text: a host-env canary
   set for this test never appears in `output_tail`/`junit_xml`, and no
   attack is ever recorded as having actually succeeded (checked by grepping
   the log for an explicit "breach" marker the ctypes attack would emit if the
   sandbox's non-root/no-new-privileges/cap-drop combination ever failed).

Skips without Docker + the runner image, same convention as
`test_agent3_sandbox.py`/`test_agent3_units.py`.
"""

import json
import shutil

import pytest

from document_analyzer.agent3.worker import units as units_module
from document_analyzer.agent3.worker.run import ExecutionState
from document_analyzer.agent3.worker.sandbox import SandboxParams, SandboxUnavailableError, check_sandbox_available
from document_analyzer.supabase_client import insert_scoped

from fakes import FakeClient

ORG = "org-1"
JOB_ID = "ej-1"

PARAMS = {
    "model_deployment": "gpt-test",
    "max_attempts": 1,  # every attempt would generate identical hostile code; one is enough to prove the verdict
    "max_context_chars": 60_000,
    "sandbox_timeout_seconds": 15,
    "sandbox_memory_mb": 512,
    "runner_image": "galvanise-agent3-runner:0.1",
}

_live = pytest.mark.skipif(shutil.which("docker") is None, reason="needs Docker")


# --------------------------------------------------------------------------- #
# Fakes and fixtures (same shapes as test_agent3_units.py, kept local rather
# than imported — a test module, not a shared helper).
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class FakeLLM:
    """Routes `generate_unit_code` calls by unit_slug; `generate_behaviour_tests`
    calls get a trivial no-op behaviour test regardless of unit (this suite only
    cares what the contract test's import does)."""

    def __init__(self, generate_responses: dict[str, str]):
        self._generate = generate_responses
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        system = kwargs["messages"][0]["content"]
        unit_slug = system.rsplit("unit_slug is: ", 1)[-1].strip()
        if "writing pytest behaviour tests" in system:
            return _Resp(json.dumps({"content": "import pytest\n\n\n@pytest.mark.behaviour\ndef test_noop():\n    assert True\n"}))
        return _Resp(self._generate[unit_slug])


def _contract(entity_id: str) -> dict:
    return {
        "public_interface": [{"entity_id": entity_id, "name": "run", "kind": "function", "class": None, "params": ["order"], "file_path": "x.php"}],
        "incoming_call_sites": [],
        "outgoing_dependencies": [],
        "external_calls": [],
        "hooks_bound": [],
        "routes_bound": [],
        "side_effect_signals": [],
        "unverified_member_ids": [],
        "merged_cluster_ids": [],
    }


def _generate_response(unit_slug: str, entity_id: str, malicious_body: str) -> str:
    module = f"app.units.{unit_slug}.module"
    content = f"{malicious_body}\n\n\ndef run(order):\n    return order\n"
    return json.dumps(
        {
            "files": [{"path": f"{module.replace('.', '/')}.py", "content": content}],
            "symbol_map": {entity_id: {"python_module": module, "python_name": "run", "kind": "function"}},
        }
    )


def _item(cluster_id: str, unit_slug: str, entity_id: str) -> dict:
    return {
        "cluster_id": cluster_id,
        "phase_cluster_id": "phase-1",
        "phase_order": 1,
        "unit_order": 1,
        "unit_slug": unit_slug,
        "member_entity_ids": [entity_id],
        "report_source_cluster_ids": [cluster_id],
        "test_contract": _contract(entity_id),
    }


def _state(tmp_path, item: dict, entity_id: str) -> ExecutionState:
    state = ExecutionState()
    state.items = [item]
    state.entities = {entity_id: {"id": entity_id, "entity_type": "function", "name": "run", "file_path": "x.php", "raw_facts": {"params": ["order"]}}}
    state.summaries = {}
    state.cluster_reports = {}
    state.snapshot_dir = tmp_path
    return state


def _seed_unit_execution(client, cluster_id: str) -> None:
    insert_scoped(
        client,
        "unit_executions",
        ORG,
        {
            "execution_job_id": JOB_ID,
            "cluster_id": cluster_id,
            "phase_cluster_id": "phase-1",
            "phase_order": 1,
            "unit_order": 1,
            "status": "pending",
            "attempts": 0,
            "flags": [],
        },
    )


def _execution_job() -> dict:
    return {"id": JOB_ID, "org_id": ORG, "project_id": "p1", "plan_id": "plan-1", "source_job_id": "job-1", "params": PARAMS}


def _unit_row(client, cluster_id: str) -> dict:
    [row] = [r for r in client.table("unit_executions").rows if r["cluster_id"] == cluster_id]
    return row


@pytest.fixture
def client():
    return FakeClient(org_id=ORG)


def _skip_unless_sandbox_ready():
    try:
        check_sandbox_available(SandboxParams())
    except SandboxUnavailableError as exc:
        pytest.skip(f"sandbox not available: {exc}")


# --------------------------------------------------------------------------- #
# The five attack vectors (tasks_agent3.md Phase 6)
# --------------------------------------------------------------------------- #

_NETWORK_ATTACK = """
import socket

socket.create_connection(("1.1.1.1", 53), 2)
"""

_FORK_BOMB_ATTACK = """
import threading

_stop = threading.Event()
_threads = []
for _ in range(4000):
    t = threading.Thread(target=_stop.wait)
    t.start()
    _threads.append(t)
_stop.set()
"""

_HUGE_TMP_WRITE_ATTACK = """
with open("/tmp/bigfile", "wb") as f:
    chunk = b"\\x00" * (8 * 1024 * 1024)
    for _ in range(200):  # 1.6 GB total, far past the sandbox's 256 MB tmpfs
        f.write(chunk)
        f.flush()
"""

_READ_HOST_PATHS_ATTACK = """
import os

_canary = "AGENT3_CANARY_SECRET"
with open("/proc/1/environ", "rb") as f:
    _environ = f.read()
assert _canary.encode() not in _environ, "BREACH: host secret leaked via /proc/1/environ"

with open("/etc/shadow", "rb") as f:
    f.read()
"""

_CTYPES_PRIVILEGE_ESCALATION_ATTACK = """
import ctypes
import os

_libc = ctypes.CDLL(None, use_errno=True)
_rc = _libc.setuid(0)
if _rc == 0 and os.getuid() == 0:
    raise AssertionError("BREACH: privilege escalation via ctypes succeeded")
raise PermissionError(f"setuid(0) via ctypes correctly blocked (rc={_rc}, uid={os.getuid()})")
"""

_ATTACKS = {
    "network": _NETWORK_ATTACK,
    "fork_bomb": _FORK_BOMB_ATTACK,
    "huge_tmp_write": _HUGE_TMP_WRITE_ATTACK,
    "read_host_paths": _READ_HOST_PATHS_ATTACK,
    "ctypes_privilege_escalation": _CTYPES_PRIVILEGE_ESCALATION_ATTACK,
}


@_live
@pytest.mark.parametrize("attack_name", list(_ATTACKS), ids=list(_ATTACKS))
def test_abusive_generated_unit_ends_in_a_terminal_failure(monkeypatch, client, tmp_path, attack_name):
    _skip_unless_sandbox_ready()
    monkeypatch.setenv("AGENT3_CANARY_SECRET", "do-not-leak")

    cluster_id, unit_slug, entity_id = f"cluster-{attack_name}", f"01_01_{attack_name}", f"e-{attack_name}"
    item = _item(cluster_id, unit_slug, entity_id)
    state = _state(tmp_path, item, entity_id)
    _seed_unit_execution(client, cluster_id)
    llm = FakeLLM({unit_slug: _generate_response(unit_slug, entity_id, _ATTACKS[attack_name])})
    monkeypatch.setattr(units_module, "get_azure_openai_client", lambda: llm)

    units_module.run_units_stage(client, _execution_job(), state)  # must return, not hang or raise

    row = _unit_row(client, cluster_id)
    assert row["status"] in ("failed_verification", "error")

    log_text = json.dumps(row.get("test_summary")) + (row.get("last_error") or "")
    assert "do-not-leak" not in log_text
    assert "BREACH" not in log_text.upper().replace("breach", "BREACH")  # no attack ever recorded as having succeeded


@_live
def test_run_units_stage_completes_even_when_every_unit_is_hostile(monkeypatch, client, tmp_path):
    """Multiple hostile units in one job: run_units_stage still processes every one
    (specs FR1's "done means every unit reached a terminal verdict") rather than
    aborting on the first attack."""
    _skip_unless_sandbox_ready()

    items, entities = [], {}
    generate_responses = {}
    for attack_name, body in _ATTACKS.items():
        cluster_id, unit_slug, entity_id = f"cluster-{attack_name}", f"01_01_{attack_name}", f"e-{attack_name}"
        items.append(_item(cluster_id, unit_slug, entity_id))
        entities[entity_id] = {"id": entity_id, "entity_type": "function", "name": "run", "file_path": "x.php", "raw_facts": {"params": ["order"]}}
        generate_responses[unit_slug] = _generate_response(unit_slug, entity_id, body)
        _seed_unit_execution(client, cluster_id)

    state = ExecutionState()
    state.items = items
    state.entities = entities
    state.summaries = {}
    state.cluster_reports = {}
    state.snapshot_dir = tmp_path
    llm = FakeLLM(generate_responses)
    monkeypatch.setattr(units_module, "get_azure_openai_client", lambda: llm)

    units_module.run_units_stage(client, _execution_job(), state)

    rows = client.table("unit_executions").rows
    assert len(rows) == len(_ATTACKS)
    assert all(r["status"] in ("failed_verification", "error") for r in rows)
