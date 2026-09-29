"""Stage `units` end to end — design_agent3.md §4, specs_agent3.md FR2/FR6/FR7.

Three groups:
1. `FakeClient` + a `FakeLLM` + a monkeypatched `sandbox.run_pytest` (no Docker
   needed): a two-unit synthetic scenario (B depends on A) built directly
   against `ExecutionState`, same style as `test_agent3_context.py`. Covers
   the passed / retry-then-pass / failed_verification / error+stub /
   built_on_failed_dependency / storage-path-shape paths.
2. The `prepare` -> `units` seam, run for real through `run_prepare_stage`
   (same realistic-synthetic-DB-rows fixture pattern as
   `test_agent3_prepare.py`, not the full php-driven Agent 1/2 pipeline —
   `run_prepare_stage` never touches php, so this needs no `php` binary and
   still exercises the real handoff: `unit_slug` derivation, snapshot
   unpacking, `member_entity_ids`/`report_source_cluster_ids`, all consumed
   by the real `run_units_stage`, not a hand-built `ExecutionState`).
3. A `live`-marked test (needs Docker + the runner image, skipped otherwise,
   same convention as `test_agent3_sandbox.py`/`test_agent3_template.py`)
   running one real unit through the actual sandbox.
"""

import json
import shutil

import pytest

from document_analyzer.agent3.worker import units as units_module
from document_analyzer.agent3.worker.prepare import run_prepare_stage
from document_analyzer.agent3.worker.run import ExecutionState
from document_analyzer.agent3.worker.sandbox import SandboxParams, SandboxResult, TestCaseResult
from document_analyzer.supabase_client import insert_scoped

from agent3_fixtures import make_tarball as _make_tarball
from agent3_fixtures import seed_plan as _seed_plan
from fakes import FakeClient

ORG = "org-1"
JOB_ID = "ej-1"

PARAMS = {
    "model_deployment": "gpt-test",
    "max_attempts": 3,
    "max_context_chars": 60_000,
    "sandbox_timeout_seconds": 120,
    "sandbox_memory_mb": 1024,
    "runner_image": "galvanise-agent3-runner:0.1",
}


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class FakeLLM:
    """Routes each call by unit_slug (parsed off the system message, generate.py's
    own "Your unit_slug is: <slug>" line) for `generate_unit_code` calls, and by a
    fixed marker for `generate_behaviour_tests` calls. `generate_responses` and
    `behaviour_responses` are unit_slug -> list of JSON strings, popped one per
    call (the last repeats if exhausted, same convention as test_agent3_generate.py)."""

    def __init__(self, generate_responses: dict[str, list[str]], behaviour_responses: dict[str, list[str]] | None = None):
        self._generate = {k: list(v) for k, v in generate_responses.items()}
        self._behaviour = {k: list(v) for k, v in (behaviour_responses or {}).items()}
        self.calls: list[list[dict]] = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        messages = kwargs["messages"]
        self.calls.append(messages)
        system = messages[0]["content"]
        if "writing pytest behaviour tests" in system:
            unit_slug = system.rsplit("unit_slug is: ", 1)[-1].strip()
            queue = self._behaviour.setdefault(unit_slug, [_default_behaviour_response()])
        else:
            unit_slug = system.rsplit("unit_slug is: ", 1)[-1].strip()
            queue = self._generate.setdefault(unit_slug, ["{}"])
        content = queue.pop(0) if len(queue) > 1 else queue[0]
        return _Resp(content)


def _default_behaviour_response() -> str:
    return json.dumps({"content": "import pytest\n\n\n@pytest.mark.behaviour\ndef test_noop():\n    assert True\n"})


def _generate_response(unit_slug: str, entity_id: str, name: str) -> str:
    module = f"app.units.{unit_slug}.module"
    return json.dumps(
        {
            "files": [{"path": f"{module.replace('.', '/')}.py", "content": f"def {name}(order):\n    return order\n"}],
            "symbol_map": {entity_id: {"python_module": module, "python_name": name, "kind": "function"}},
        }
    )


def _sandbox_result(*, contract=1, contract_failed=0, behaviour=0, behaviour_failed=0, status="completed", exit_code=0):
    tests = (
        [TestCaseResult(classname="tests.units.x.test_contract", name=f"c{i}", outcome="passed") for i in range(contract)]
        + [
            TestCaseResult(classname="tests.units.x.test_contract", name=f"cf{i}", outcome="failed", message="assert 1 == 2")
            for i in range(contract_failed)
        ]
        + [TestCaseResult(classname="tests.units.x.test_behaviour", name=f"b{i}", outcome="passed") for i in range(behaviour)]
        + [
            TestCaseResult(classname="tests.units.x.test_behaviour", name=f"bf{i}", outcome="failed", message="assert False")
            for i in range(behaviour_failed)
        ]
    )
    return SandboxResult(status=status, exit_code=exit_code, tests=tests, output_tail="log", junit_xml="<xml/>", duration_s=0.05)


class _FakeSandbox:
    """`results` maps a workspace's unit_slug (read off `app/units/__init__.py`'s
    last import) to a queue of SandboxResults, popped one per call to that unit."""

    def __init__(self, results_by_slug: dict[str, list[SandboxResult]]):
        self._results = {k: list(v) for k, v in results_by_slug.items()}
        self.calls: list[tuple[str, object]] = []

    def __call__(self, workspace, params):
        init_text = (workspace / "app" / "units" / "__init__.py").read_text()
        # The unit under test is always the *last* importlib.import_module(...) line.
        last_line = [line for line in init_text.splitlines() if "importlib.import_module" in line][-1]
        slug = last_line.split("'")[1].rsplit(".", 1)[-1]
        self.calls.append((slug, workspace))
        queue = self._results[slug]
        return queue.pop(0) if len(queue) > 1 else queue[0]


# --------------------------------------------------------------------------- #
# ExecutionState fixture: two units, B depends on A
# --------------------------------------------------------------------------- #


def _contract(public_name: str, entity_id: str, *, outgoing_to: str | None = None, unverified: bool = False) -> dict:
    return {
        "public_interface": [
            {"entity_id": entity_id, "name": public_name, "kind": "function", "class": None, "params": ["order"], "file_path": "x.php"}
        ],
        "incoming_call_sites": [],
        "outgoing_dependencies": [{"to_unit_id": outgoing_to, "call_count": 1, "hook_binding_count": 0}] if outgoing_to else [],
        "external_calls": [],
        "hooks_bound": [],
        "routes_bound": [],
        "side_effect_signals": [],
        "unverified_member_ids": [entity_id] if unverified else [],
        "merged_cluster_ids": [],
    }


def _item(cluster_id, phase_order, unit_order, unit_slug, member_id, test_contract) -> dict:
    return {
        "cluster_id": cluster_id,
        "phase_cluster_id": "phase-1",
        "phase_order": phase_order,
        "unit_order": unit_order,
        "unit_slug": unit_slug,
        "member_entity_ids": [member_id],
        "report_source_cluster_ids": [cluster_id],
        "test_contract": test_contract,
    }


def _state(tmp_path, items: list[dict], entities: dict) -> ExecutionState:
    state = ExecutionState()
    state.items = items
    state.entities = entities
    state.summaries = {}
    state.cluster_reports = {}
    state.snapshot_dir = tmp_path
    return state


def _entity(name: str, entity_id: str) -> dict:
    return {
        "id": entity_id,
        "entity_type": "function",
        "name": name,
        "file_path": "x.php",
        "raw_facts": {"params": ["order"]},
    }


def _seed_unit_execution(client, cluster_id: str, phase_order: int, unit_order: int) -> None:
    insert_scoped(
        client,
        "unit_executions",
        ORG,
        {
            "execution_job_id": JOB_ID,
            "cluster_id": cluster_id,
            "phase_cluster_id": "phase-1",
            "phase_order": phase_order,
            "unit_order": unit_order,
            "status": "pending",
            "attempts": 0,
            "flags": [],
        },
    )


def _execution_job(params=None) -> dict:
    return {"id": JOB_ID, "org_id": ORG, "project_id": "p1", "plan_id": "plan-1", "source_job_id": "job-1", "params": params or PARAMS}


def _unit_row(client, cluster_id: str) -> dict:
    [row] = [r for r in client.table("unit_executions").rows if r["cluster_id"] == cluster_id]
    return row


@pytest.fixture
def client():
    return FakeClient(org_id=ORG)


@pytest.fixture
def two_unit_state(tmp_path, client):
    """A (no deps) and B (depends on A), both pending in unit_executions."""
    item_a = _item("cluster-a", 1, 1, "01_01_a", "e-calc", _contract("calc_total", "e-calc"))
    item_b = _item("cluster-b", 1, 2, "01_02_b", "e-render", _contract("render", "e-render", outgoing_to="cluster-a"))
    state = _state(tmp_path, [item_a, item_b], {"e-calc": _entity("calc_total", "e-calc"), "e-render": _entity("render", "e-render")})
    _seed_unit_execution(client, "cluster-a", 1, 1)
    _seed_unit_execution(client, "cluster-b", 1, 2)
    return state


def _run(monkeypatch, client, state, llm, sandbox_results_by_slug):
    monkeypatch.setattr(units_module, "get_azure_openai_client", lambda: llm)
    fake_sandbox = _FakeSandbox(sandbox_results_by_slug)
    monkeypatch.setattr(units_module, "run_pytest", fake_sandbox)
    units_module.run_units_stage(client, _execution_job(), state)
    return fake_sandbox


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_both_units_pass_on_first_attempt(monkeypatch, client, two_unit_state):
    llm = FakeLLM(
        generate_responses={
            "01_01_a": [_generate_response("01_01_a", "e-calc", "calc_total")],
            "01_02_b": [_generate_response("01_02_b", "e-render", "render")],
        }
    )
    _run(monkeypatch, client, two_unit_state, llm, {"01_01_a": [_sandbox_result()], "01_02_b": [_sandbox_result()]})

    row_a, row_b = _unit_row(client, "cluster-a"), _unit_row(client, "cluster-b")
    assert (row_a["status"], row_a["attempts"]) == ("passed", 1)
    assert (row_b["status"], row_b["attempts"]) == ("passed", 1)
    assert row_a["flags"] == []
    assert row_b["flags"] == []  # A succeeded for real, so B isn't built on a failed dependency
    assert row_a["test_summary"] == {"contract": {"passed": 1, "failed": 0}, "behaviour": {"passed": 0, "failed": 0}, "duration_s": 0.05}
    assert two_unit_state.symbol_maps["cluster-a"]["e-calc"]["python_name"] == "calc_total"
    assert two_unit_state.unit_files["cluster-a"]


def test_unverified_inputs_flag(monkeypatch, client, tmp_path):
    item = _item("cluster-a", 1, 1, "01_01_a", "e-calc", _contract("calc_total", "e-calc", unverified=True))
    state = _state(tmp_path, [item], {"e-calc": _entity("calc_total", "e-calc")})
    _seed_unit_execution(client, "cluster-a", 1, 1)
    llm = FakeLLM({"01_01_a": [_generate_response("01_01_a", "e-calc", "calc_total")]})

    _run(monkeypatch, client, state, llm, {"01_01_a": [_sandbox_result()]})

    assert _unit_row(client, "cluster-a")["flags"] == ["unverified_inputs"]


def test_retry_with_feedback_then_pass(monkeypatch, client, tmp_path):
    item = _item("cluster-a", 1, 1, "01_01_a", "e-calc", _contract("calc_total", "e-calc"))
    state = _state(tmp_path, [item], {"e-calc": _entity("calc_total", "e-calc")})
    _seed_unit_execution(client, "cluster-a", 1, 1)
    llm = FakeLLM({"01_01_a": [_generate_response("01_01_a", "e-calc", "calc_total")] * 2})

    _run(
        monkeypatch,
        client,
        state,
        llm,
        {"01_01_a": [_sandbox_result(contract=0, contract_failed=1), _sandbox_result()]},
    )

    row = _unit_row(client, "cluster-a")
    assert (row["status"], row["attempts"]) == ("passed", 2)
    # The second generate call's conversation carries the first round's failing test as feedback.
    generate_calls = [c for c in llm.calls if "writing pytest behaviour tests" not in c[0]["content"]]
    assert "cf0" in generate_calls[1][1]["content"] or "assert 1 == 2" in generate_calls[1][1]["content"]


def test_exhausting_attempts_ends_failed_verification(monkeypatch, client, tmp_path):
    item = _item("cluster-a", 1, 1, "01_01_a", "e-calc", _contract("calc_total", "e-calc"))
    state = _state(tmp_path, [item], {"e-calc": _entity("calc_total", "e-calc")})
    _seed_unit_execution(client, "cluster-a", 1, 1)
    llm = FakeLLM({"01_01_a": [_generate_response("01_01_a", "e-calc", "calc_total")] * 3})
    always_failing = [_sandbox_result(contract=0, contract_failed=1)] * 3

    _run(monkeypatch, client, state, llm, {"01_01_a": always_failing})

    row = _unit_row(client, "cluster-a")
    assert (row["status"], row["attempts"]) == ("failed_verification", 3)
    assert "still failing" in row["last_error"]
    assert row["storage_prefix"] == f"{ORG}/{JOB_ID}/units/cluster-a/"


def test_generation_error_ends_error_with_a_stub_and_flags_the_unit_stubbed(monkeypatch, client, tmp_path):
    item = _item("cluster-a", 1, 1, "01_01_a", "e-calc", _contract("calc_total", "e-calc"))
    state = _state(tmp_path, [item], {"e-calc": _entity("calc_total", "e-calc")})
    _seed_unit_execution(client, "cluster-a", 1, 1)
    llm = FakeLLM({"01_01_a": ["not json at all"] * 3})

    _run(monkeypatch, client, state, llm, {})  # sandbox is never reached

    row = _unit_row(client, "cluster-a")
    assert row["status"] == "error"
    assert row["flags"] == ["stubbed"]
    assert row["storage_prefix"] is None
    assert row["test_summary"] is None
    # The stub is stored for dependents even though nothing was uploaded for this unit.
    assert "e-calc" in state.symbol_maps["cluster-a"]
    assert "cluster-a" in state.stubbed_units


def test_dependent_of_an_errored_unit_is_flagged_built_on_failed_dependency(monkeypatch, client, two_unit_state):
    llm = FakeLLM(
        generate_responses={
            "01_01_a": ["still not json"] * 3,  # A can never generate
            "01_02_b": [_generate_response("01_02_b", "e-render", "render")],
        }
    )

    _run(monkeypatch, client, two_unit_state, llm, {"01_02_b": [_sandbox_result()]})

    row_a, row_b = _unit_row(client, "cluster-a"), _unit_row(client, "cluster-b")
    assert row_a["status"] == "error"
    assert row_b["status"] == "passed"
    assert row_b["flags"] == ["built_on_failed_dependency"]
    # B's own workspace built against A's NotImplementedError stub without crashing.
    assert "cluster-a" in two_unit_state.stubbed_units


def test_context_error_ends_error_with_zero_attempts(monkeypatch, client, tmp_path):
    # A line with no end_line is a data error (context.py::ContextError), not "no span".
    bad_entity = {"id": "e-calc", "entity_type": "function", "name": "calc_total", "file_path": "x.php", "raw_facts": {"line": 3}}
    item = _item("cluster-a", 1, 1, "01_01_a", "e-calc", _contract("calc_total", "e-calc"))
    state = _state(tmp_path, [item], {"e-calc": bad_entity})
    _seed_unit_execution(client, "cluster-a", 1, 1)
    llm = FakeLLM({})

    _run(monkeypatch, client, state, llm, {})

    row = _unit_row(client, "cluster-a")
    assert (row["status"], row["attempts"]) == ("error", 0)
    assert llm.calls == []  # never even reached generate.py


def test_behaviour_generation_abandoned_is_recorded_without_failing_the_unit(monkeypatch, client, tmp_path):
    item = _item("cluster-a", 1, 1, "01_01_a", "e-calc", _contract("calc_total", "e-calc"))
    state = _state(tmp_path, [item], {"e-calc": _entity("calc_total", "e-calc")})
    _seed_unit_execution(client, "cluster-a", 1, 1)
    llm = FakeLLM(
        generate_responses={"01_01_a": [_generate_response("01_01_a", "e-calc", "calc_total")]},
        behaviour_responses={"01_01_a": ["not json"] * 3},
    )

    _run(monkeypatch, client, state, llm, {"01_01_a": [_sandbox_result(behaviour=0)]})

    row = _unit_row(client, "cluster-a")
    assert row["status"] == "passed"  # a bad behaviour draft never fails the unit
    assert row["test_summary"]["behaviour"] == {"passed": 0, "failed": 0, "generated": False}


def test_timed_out_sandbox_run_is_a_failed_attempt_not_an_error(monkeypatch, client, tmp_path):
    item = _item("cluster-a", 1, 1, "01_01_a", "e-calc", _contract("calc_total", "e-calc"))
    state = _state(tmp_path, [item], {"e-calc": _entity("calc_total", "e-calc")})
    _seed_unit_execution(client, "cluster-a", 1, 1)
    llm = FakeLLM({"01_01_a": [_generate_response("01_01_a", "e-calc", "calc_total")] * 3})
    timed_out = SandboxResult(status="timed_out", exit_code=None, tests=[], output_tail="", junit_xml="", duration_s=120.0)

    _run(monkeypatch, client, state, llm, {"01_01_a": [timed_out] * 3})

    row = _unit_row(client, "cluster-a")
    assert row["status"] == "failed_verification"  # not "error": real code was produced
    assert "timed_out" in row["last_error"]


def test_storage_paths_match_design_agent3_object_layout_exactly(monkeypatch, client, tmp_path):
    item = _item("cluster-a", 1, 1, "01_01_a", "e-calc", _contract("calc_total", "e-calc"))
    state = _state(tmp_path, [item], {"e-calc": _entity("calc_total", "e-calc")})
    _seed_unit_execution(client, "cluster-a", 1, 1)
    llm = FakeLLM({"01_01_a": [_generate_response("01_01_a", "e-calc", "calc_total")]})

    _run(monkeypatch, client, state, llm, {"01_01_a": [_sandbox_result()]})

    bucket = client.storage.from_(units_module.OUTPUT_BUCKET)
    uploaded_paths = {path for path, _content, _opts in bucket.uploads}
    prefix = f"{ORG}/{JOB_ID}/units/cluster-a/attempt_1/"

    assert f"{prefix}code/app/units/01_01_a/module.py" in uploaded_paths
    assert f"{prefix}code/app/units/01_01_a/__init__.py" in uploaded_paths
    assert f"{prefix}tests/units/01_01_a/test_contract.py" in uploaded_paths
    assert f"{prefix}tests/units/01_01_a/test_behaviour.py" in uploaded_paths
    assert f"{prefix}log.txt" in uploaded_paths
    assert f"{prefix}junit.xml" in uploaded_paths
    # Every uploaded object for this attempt sits under the exact attempt_{n} prefix
    # unit_executions.storage_prefix (+"attempt_N/") points to — nothing stray.
    assert all(p.startswith(prefix) for p in uploaded_paths)
    assert _unit_row(client, "cluster-a")["storage_prefix"] == f"{ORG}/{JOB_ID}/units/cluster-a/"


def test_every_attempt_is_uploaded_not_just_the_last(monkeypatch, client, tmp_path):
    item = _item("cluster-a", 1, 1, "01_01_a", "e-calc", _contract("calc_total", "e-calc"))
    state = _state(tmp_path, [item], {"e-calc": _entity("calc_total", "e-calc")})
    _seed_unit_execution(client, "cluster-a", 1, 1)
    llm = FakeLLM({"01_01_a": [_generate_response("01_01_a", "e-calc", "calc_total")] * 2})

    _run(
        monkeypatch, client, state, llm,
        {"01_01_a": [_sandbox_result(contract=0, contract_failed=1), _sandbox_result()]},
    )

    bucket = client.storage.from_(units_module.OUTPUT_BUCKET)
    uploaded_paths = {path for path, _content, _opts in bucket.uploads}
    assert f"{ORG}/{JOB_ID}/units/cluster-a/attempt_1/log.txt" in uploaded_paths
    assert f"{ORG}/{JOB_ID}/units/cluster-a/attempt_2/log.txt" in uploaded_paths


def test_resume_skips_already_terminal_units(monkeypatch, client, two_unit_state):
    two_unit_state.terminal_unit_ids = {"cluster-a"}
    two_unit_state.symbol_maps["cluster-a"] = {"e-calc": {"python_module": "app.units.01_01_a.module", "python_name": "calc_total", "kind": "function"}}
    two_unit_state.unit_files["cluster-a"] = [units_module.GeneratedFile(path="app/units/01_01_a/module.py", content="def calc_total(order):\n    return order\n")]
    llm = FakeLLM({"01_02_b": [_generate_response("01_02_b", "e-render", "render")]})

    fake_sandbox = _run(monkeypatch, client, two_unit_state, llm, {"01_02_b": [_sandbox_result()]})

    assert [slug for slug, _ws in fake_sandbox.calls] == ["01_02_b"]
    assert _unit_row(client, "cluster-b")["status"] == "passed"


# --------------------------------------------------------------------------- #
# The prepare -> units seam, run for real (no php needed: run_prepare_stage
# never touches it — same realistic-synthetic-rows pattern as
# test_agent3_prepare.py, not the php-driven Agent 1/2 pipeline)
# --------------------------------------------------------------------------- #


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


def test_prepare_then_units_end_to_end(monkeypatch):
    client = FakeClient(org_id=ORG)
    job, plan, _project = _seed_plan(client)
    _seed_plan_unit(
        client, plan, job, cluster_id="unit-a", phase_order=1, unit_order=1, entity_id="e-calc", name="calc_total",
        test_contract=_contract("calc_total", "e-calc"),
    )
    _seed_plan_unit(
        client, plan, job, cluster_id="unit-b", phase_order=1, unit_order=2, entity_id="e-render", name="render",
        test_contract=_contract("render", "e-render", outgoing_to="unit-a"),
    )
    insert_scoped(client, "execution_jobs", ORG, {"id": JOB_ID, "project_id": "p1", "plan_id": plan["id"], "source_job_id": job["id"], "status": "running", "params": PARAMS})
    _seed_unit_execution(client, "unit-a", 1, 1)
    _seed_unit_execution(client, "unit-b", 1, 2)

    state = ExecutionState()
    execution_job = {"id": JOB_ID, "org_id": ORG, "plan_id": plan["id"], "source_job_id": job["id"], "params": PARAMS}
    run_prepare_stage(client, execution_job, state)

    # run_prepare_stage derived real unit_slugs (no `clusters` row seeded, so both fall
    # back to a short-id slug, design_agent3.md §8/unit_slug.py) — read them back rather
    # than hardcoding, since that fallback is itself under test elsewhere.
    slug_a = next(i for i in state.items if i["cluster_id"] == "unit-a")["unit_slug"]
    slug_b = next(i for i in state.items if i["cluster_id"] == "unit-b")["unit_slug"]
    llm = FakeLLM(
        generate_responses={
            slug_a: [_generate_response(slug_a, "e-calc", "calc_total")],
            slug_b: [_generate_response(slug_b, "e-render", "render")],
        }
    )
    fake_sandbox = _FakeSandbox({slug_a: [_sandbox_result()], slug_b: [_sandbox_result()]})
    monkeypatch.setattr(units_module, "get_azure_openai_client", lambda: llm)
    monkeypatch.setattr(units_module, "run_pytest", fake_sandbox)

    units_module.run_units_stage(client, execution_job, state)

    row_a, row_b = _unit_row(client, "unit-a"), _unit_row(client, "unit-b")
    assert (row_a["status"], row_b["status"]) == ("passed", "passed")
    assert row_b["flags"] == []  # a real dependency symbol map, not a stub


# --------------------------------------------------------------------------- #
# Live sandbox test (skipped without Docker + the runner image)
# --------------------------------------------------------------------------- #

_live = pytest.mark.skipif(shutil.which("docker") is None, reason="needs Docker")


@_live
def test_live_one_unit_through_the_real_sandbox(monkeypatch, client, tmp_path):
    from document_analyzer.agent3.worker.sandbox import SandboxUnavailableError, check_sandbox_available

    try:
        check_sandbox_available(SandboxParams())
    except SandboxUnavailableError as exc:
        pytest.skip(f"sandbox not available: {exc}")

    item = _item("cluster-a", 1, 1, "01_01_a", "e-calc", _contract("calc_total", "e-calc"))
    state = _state(tmp_path, [item], {"e-calc": _entity("calc_total", "e-calc")})
    _seed_unit_execution(client, "cluster-a", 1, 1)
    llm = FakeLLM({"01_01_a": [_generate_response("01_01_a", "e-calc", "calc_total")]})
    monkeypatch.setattr(units_module, "get_azure_openai_client", lambda: llm)

    units_module.run_units_stage(client, _execution_job(), state)

    row = _unit_row(client, "cluster-a")
    assert row["status"] == "passed"
    assert row["test_summary"]["contract"]["failed"] == 0
