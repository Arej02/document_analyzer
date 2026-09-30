"""Redelivery / resume — tasks_agent3.md Phase 6, third task; design_agent3.md §3/§4.

Simulates "kill the worker mid-`units`, then restart it" by literally doing
that across two separate calls into the real stages, sharing the same
`FakeClient` (its tables and `agent3-output` storage stand in for what would
actually persist across a real crash):

1. **Run 1** ("before the crash"): `run_prepare_stage` loads a two-unit plan
   (A, B — B depends on A) for real, then only unit A is processed
   (`units.py::_run_one_unit` called directly, not through the pending loop —
   modeling the process dying before it got to B).
2. **Run 2** ("after the restart"): a brand-new `ExecutionState` is built from
   scratch and `run_prepare_stage` is run again against the *same* client. It
   must recognize A as already terminal (skip it) and, since `prepare.py`'s
   resume path now repopulates `state.unit_files`/`unit_test_files`/
   `stubbed_units` for every already-terminal unit (not just `symbol_maps`,
   closing the gap `design_agent3.md` §4 used to document), B's dependency
   resolution must see A's *real* result — not silently fall back to a fresh
   stub.

Two dependency outcomes are checked, since they take different resume paths
(`prepare.py::_load_resume_state`): A **passed** (has a `storage_prefix`, so
its last attempt is read back from storage) and A **errored** (no
`storage_prefix`, so its deterministic stub is regenerated instead). A third
test checks that redelivering an already-fully-terminal job is a pure no-op.

No Docker needed: `FakeClient`/`FakeStorageBucket` (`tests/fakes.py`) plus a
monkeypatched sandbox, same pattern as `test_agent3_units.py`'s "prepare ->
units, run for real" seam test.
"""

import json

from document_analyzer.agent3.worker import units as units_module
from document_analyzer.agent3.worker.generate import generate_stub_unit
from document_analyzer.agent3.worker.prepare import run_prepare_stage
from document_analyzer.agent3.worker.run import ExecutionState
from document_analyzer.agent3.worker.sandbox import SandboxResult, TestCaseResult
from document_analyzer.supabase_client import insert_scoped

from agent3_fixtures import seed_plan as _seed_plan
from fakes import FakeClient

ORG = "org-1"
JOB_ID = "ej-1"

PARAMS = {
    "model_deployment": "gpt-test",
    "max_attempts": 1,
    "max_context_chars": 60_000,
    "sandbox_timeout_seconds": 120,
    "sandbox_memory_mb": 1024,
    "runner_image": "galvanise-agent3-runner:0.1",
}


# --------------------------------------------------------------------------- #
# Fakes (same shapes as test_agent3_units.py, kept local)
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class FakeLLM:
    def __init__(self, generate_responses: dict[str, list[str]]):
        self._generate = {k: list(v) for k, v in generate_responses.items()}
        self.calls: list[list[dict]] = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        messages = kwargs["messages"]
        self.calls.append(messages)
        system = messages[0]["content"]
        unit_slug = system.rsplit("unit_slug is: ", 1)[-1].strip()
        if "writing pytest behaviour tests" in system:
            return _Resp(json.dumps({"content": "import pytest\n\n\n@pytest.mark.behaviour\ndef test_noop():\n    assert True\n"}))
        queue = self._generate.setdefault(unit_slug, ["{}"])
        return _Resp(queue.pop(0) if len(queue) > 1 else queue[0])


def _generate_response(unit_slug: str, entity_id: str, name: str) -> str:
    module = f"app.units.{unit_slug}.module"
    return json.dumps(
        {
            "files": [{"path": f"{module.replace('.', '/')}.py", "content": f"def {name}(order):\n    return order\n"}],
            "symbol_map": {entity_id: {"python_module": module, "python_name": name, "kind": "function"}},
        }
    )


_INVALID_RESPONSE = json.dumps({"files": [{"path": "not/allowed/path.py", "content": "x = 1\n"}], "symbol_map": {}})


def _sandbox_result(*, passed=True) -> SandboxResult:
    tests = [TestCaseResult(classname="tests.units.x.test_contract", name="c0", outcome="passed" if passed else "failed", message=None if passed else "assert 1 == 2")]
    return SandboxResult(status="completed", exit_code=0 if passed else 1, tests=tests, output_tail="log", junit_xml="<xml/>", duration_s=0.05)


class _FakeSandbox:
    """Answers per unit_slug (read off the workspace's own `app/units/__init__.py`
    last import line, same trick as test_agent3_units.py's version), and records
    which slugs it was actually called for — used to prove a resumed run never
    re-processes a terminal unit."""

    def __init__(self, results_by_slug: dict[str, SandboxResult]):
        self._results = results_by_slug
        self.calls: list[str] = []

    def __call__(self, workspace, params):
        init_text = (workspace / "app" / "units" / "__init__.py").read_text()
        last_line = [line for line in init_text.splitlines() if "importlib.import_module" in line][-1]
        slug = last_line.split("'")[1].rsplit(".", 1)[-1]
        self.calls.append(slug)
        return self._results[slug]


def _contract(name: str, entity_id: str, *, outgoing_to: str | None = None) -> dict:
    return {
        "public_interface": [{"entity_id": entity_id, "name": name, "kind": "function", "class": None, "params": ["order"], "file_path": "x.php"}],
        "incoming_call_sites": [],
        "outgoing_dependencies": [{"to_unit_id": outgoing_to, "call_count": 1, "hook_binding_count": 0}] if outgoing_to else [],
        "external_calls": [],
        "hooks_bound": [],
        "routes_bound": [],
        "side_effect_signals": [],
        "unverified_member_ids": [],
        "merged_cluster_ids": [],
    }


def _seed_plan_unit(client, plan, job, *, cluster_id, phase_order, unit_order, entity_id, name, test_contract) -> None:
    insert_scoped(client, "cluster_members", ORG, {"cluster_id": cluster_id, "entity_id": entity_id, "canonical_entity_id": entity_id})
    insert_scoped(
        client,
        "extracted_entities",
        ORG,
        {"id": entity_id, "job_id": job["id"], "entity_type": "function", "name": name, "file_path": "x.php", "raw_facts": {"params": ["order"]}},
    )
    insert_scoped(
        client, "knowledge_summaries", ORG,
        {"job_id": job["id"], "level": "entity", "entity_id": entity_id, "summary_text": "s", "risk_level": "low", "grounding_status": "verified"},
    )
    insert_scoped(client, "cluster_reports", ORG, {"cluster_id": cluster_id, "summary_text": f"report {cluster_id}", "risk_level": "low", "grounding_status": "verified"})
    insert_scoped(
        client, "migration_plan_items", ORG,
        {"plan_id": plan["id"], "cluster_id": cluster_id, "phase_cluster_id": f"phase-{phase_order}", "phase_order": phase_order, "unit_order": unit_order, "test_contract": test_contract},
    )


def _seed_unit_execution(client, cluster_id: str, phase_order: int, unit_order: int) -> None:
    insert_scoped(
        client, "unit_executions", ORG,
        {"execution_job_id": JOB_ID, "cluster_id": cluster_id, "phase_cluster_id": f"phase-{phase_order}", "phase_order": phase_order, "unit_order": unit_order, "status": "pending", "attempts": 0, "flags": []},
    )


def _unit_row(client, cluster_id: str) -> dict:
    [row] = [r for r in client.table("unit_executions").rows if r["cluster_id"] == cluster_id]
    return row


def _build_two_unit_plan(client) -> tuple[dict, dict]:
    """unit-a (no deps) and unit-b (depends on unit-a), both pending."""
    job, plan, _project = _seed_plan(client)
    _seed_plan_unit(client, plan, job, cluster_id="unit-a", phase_order=1, unit_order=1, entity_id="e-calc", name="calc_total", test_contract=_contract("calc_total", "e-calc"))
    _seed_plan_unit(client, plan, job, cluster_id="unit-b", phase_order=1, unit_order=2, entity_id="e-render", name="render", test_contract=_contract("render", "e-render", outgoing_to="unit-a"))
    insert_scoped(client, "execution_jobs", ORG, {"id": JOB_ID, "project_id": "p1", "plan_id": plan["id"], "source_job_id": job["id"], "status": "running", "params": PARAMS})
    _seed_unit_execution(client, "unit-a", 1, 1)
    _seed_unit_execution(client, "unit-b", 1, 2)
    return plan, job


def _execution_job(plan: dict, job: dict) -> dict:
    return {"id": JOB_ID, "org_id": ORG, "plan_id": plan["id"], "source_job_id": job["id"], "params": PARAMS}


def _run_prepare(client, plan, job) -> ExecutionState:
    state = ExecutionState()
    run_prepare_stage(client, _execution_job(plan, job), state)
    return state


# --------------------------------------------------------------------------- #
# Scenario 1: the dependency passed before the crash
# --------------------------------------------------------------------------- #


def test_resumed_dependent_uses_the_real_passed_code_not_a_stub(monkeypatch):
    client = FakeClient(org_id=ORG)
    plan, job = _build_two_unit_plan(client)
    execution_job = _execution_job(plan, job)

    # --- run 1: prepare loads both items; only A is actually processed ---
    state1 = _run_prepare(client, plan, job)
    item_a = next(i for i in state1.items if i["cluster_id"] == "unit-a")
    items_by_cluster_id = {i["cluster_id"]: i for i in state1.items}
    llm1 = FakeLLM({item_a["unit_slug"]: [_generate_response(item_a["unit_slug"], "e-calc", "calc_total")]})
    monkeypatch.setattr(units_module, "run_pytest", _FakeSandbox({item_a["unit_slug"]: _sandbox_result(passed=True)}))

    units_module._run_one_unit(client, execution_job, state1, item_a, items_by_cluster_id, llm1)

    assert _unit_row(client, "unit-a")["status"] == "passed"
    assert _unit_row(client, "unit-b")["status"] == "pending"  # never touched — the "crash"

    # --- run 2: a fresh process restarts the job ---
    state2 = _run_prepare(client, plan, job)
    assert state2.terminal_unit_ids == {"unit-a"}
    # The real fix under test: resume repopulated A's files from storage, not empty.
    assert "unit-a" in state2.unit_files
    assert any("def calc_total" in f.content for f in state2.unit_files["unit-a"])
    assert "unit-a" not in state2.stubbed_units

    item_b = next(i for i in state2.items if i["cluster_id"] == "unit-b")
    llm2 = FakeLLM({item_b["unit_slug"]: [_generate_response(item_b["unit_slug"], "e-render", "render")]})
    fake_sandbox = _FakeSandbox({item_b["unit_slug"]: _sandbox_result(passed=True)})
    monkeypatch.setattr(units_module, "get_azure_openai_client", lambda: llm2)
    monkeypatch.setattr(units_module, "run_pytest", fake_sandbox)

    units_module.run_units_stage(client, execution_job, state2)

    assert fake_sandbox.calls == [item_b["unit_slug"]]  # A was never re-run
    row_b = _unit_row(client, "unit-b")
    assert row_b["status"] == "passed"
    assert "built_on_failed_dependency" not in row_b["flags"]  # real dependency, not a stub

    rows = client.table("unit_executions").rows
    assert len(rows) == 2 and {r["cluster_id"] for r in rows} == {"unit-a", "unit-b"}


# --------------------------------------------------------------------------- #
# Scenario 2: the dependency errored (stubbed) before the crash
# --------------------------------------------------------------------------- #


def test_resumed_dependent_still_flagged_when_the_dependency_errored(monkeypatch):
    client = FakeClient(org_id=ORG)
    plan, job = _build_two_unit_plan(client)
    execution_job = _execution_job(plan, job)

    state1 = _run_prepare(client, plan, job)
    item_a = next(i for i in state1.items if i["cluster_id"] == "unit-a")
    items_by_cluster_id = {i["cluster_id"]: i for i in state1.items}
    llm1 = FakeLLM({item_a["unit_slug"]: [_INVALID_RESPONSE]})

    units_module._run_one_unit(client, execution_job, state1, item_a, items_by_cluster_id, llm1)

    row_a = _unit_row(client, "unit-a")
    assert row_a["status"] == "error" and "stubbed" in row_a["flags"] and row_a["storage_prefix"] is None

    # --- restart ---
    state2 = _run_prepare(client, plan, job)
    assert state2.terminal_unit_ids == {"unit-a"}
    assert "unit-a" in state2.stubbed_units
    item_a2 = next(i for i in state2.items if i["cluster_id"] == "unit-a")
    expected_stub = units_module.ensure_package_init(generate_stub_unit(item_a2, item_a2["unit_slug"]).files, item_a2["unit_slug"])
    assert {f.path: f.content for f in state2.unit_files["unit-a"]} == {f.path: f.content for f in expected_stub}

    item_b = next(i for i in state2.items if i["cluster_id"] == "unit-b")
    llm2 = FakeLLM({item_b["unit_slug"]: [_generate_response(item_b["unit_slug"], "e-render", "render")]})
    fake_sandbox = _FakeSandbox({item_b["unit_slug"]: _sandbox_result(passed=True)})
    monkeypatch.setattr(units_module, "get_azure_openai_client", lambda: llm2)
    monkeypatch.setattr(units_module, "run_pytest", fake_sandbox)

    units_module.run_units_stage(client, execution_job, state2)

    assert fake_sandbox.calls == [item_b["unit_slug"]]
    row_b = _unit_row(client, "unit-b")
    assert row_b["status"] == "passed"
    assert "built_on_failed_dependency" in row_b["flags"]  # correctly still flagged

    rows = client.table("unit_executions").rows
    assert len(rows) == 2 and {r["cluster_id"] for r in rows} == {"unit-a", "unit-b"}


# --------------------------------------------------------------------------- #
# Scenario 3: redelivering an already fully-terminal job is a no-op
# --------------------------------------------------------------------------- #


def test_redelivering_a_fully_terminal_job_is_a_no_op(monkeypatch):
    client = FakeClient(org_id=ORG)
    plan, job = _build_two_unit_plan(client)
    execution_job = _execution_job(plan, job)

    state = _run_prepare(client, plan, job)
    item_a = next(i for i in state.items if i["cluster_id"] == "unit-a")
    item_b = next(i for i in state.items if i["cluster_id"] == "unit-b")
    llm = FakeLLM(
        {
            item_a["unit_slug"]: [_generate_response(item_a["unit_slug"], "e-calc", "calc_total")],
            item_b["unit_slug"]: [_generate_response(item_b["unit_slug"], "e-render", "render")],
        }
    )
    fake_sandbox = _FakeSandbox({item_a["unit_slug"]: _sandbox_result(passed=True), item_b["unit_slug"]: _sandbox_result(passed=True)})
    monkeypatch.setattr(units_module, "get_azure_openai_client", lambda: llm)
    monkeypatch.setattr(units_module, "run_pytest", fake_sandbox)

    units_module.run_units_stage(client, execution_job, state)
    rows_after_first_run = {r["cluster_id"]: dict(r) for r in client.table("unit_executions").rows}
    assert {r["status"] for r in rows_after_first_run.values()} == {"passed"}

    # Redelivered two more times: prepare's resume path must see everything as
    # terminal and run_units_stage must have nothing left to do, every time.
    for _ in range(2):
        state_n = _run_prepare(client, plan, job)
        assert state_n.terminal_unit_ids == {"unit-a", "unit-b"}
        fake_sandbox.calls.clear()

        units_module.run_units_stage(client, execution_job, state_n)

        assert fake_sandbox.calls == []  # nothing re-run
        rows_now = {r["cluster_id"]: dict(r) for r in client.table("unit_executions").rows}
        assert rows_now == rows_after_first_run  # byte-for-byte unchanged
        assert len(client.table("unit_executions").rows) == 2  # no duplicates
