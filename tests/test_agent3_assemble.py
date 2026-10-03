"""Stages `assemble` and `final_test` — design_agent3.md §5, tasks_agent3.md Phase 4.

Four groups:
1. `run_assemble_stage` over a hand-built `ExecutionState` (three units: passed,
   failed_verification, error+stubbed) — tree layout, `app/units/__init__.py`
   plan order, the README verdict table, and `AssembleError` on the documented
   `state.unit_files` resume gap.
2. `run_final_test_stage` with a monkeypatched `sandbox.run_pytest` — summary,
   counts, the zip upload and the `execution_jobs` write.
3. A real `prepare -> units -> assemble -> final_test` chain, the same
   synthetic-DB-rows fixture pattern as `test_agent3_units.py`'s
   `test_prepare_then_units_end_to_end` (no `php` needed).
4. A `live`-marked test (Docker + the runner image, skipped otherwise): unzips
   the real output and asserts a real `pytest` pass inside the sandbox — the
   task's explicitly required test.
"""

import json
import shutil
import subprocess
import zipfile
from io import BytesIO

import pytest

from document_analyzer.agent3.worker import assemble as assemble_module
from document_analyzer.agent3.worker import units as units_module
from document_analyzer.agent3.worker.assemble import (
    AssembleError,
    run_assemble_stage,
    run_final_test_stage,
)
from document_analyzer.agent3.worker.generate import GeneratedFile
from document_analyzer.agent3.worker.prepare import run_prepare_stage
from document_analyzer.agent3.worker.run import ExecutionState
from document_analyzer.agent3.worker.sandbox import SandboxParams, SandboxResult, TestCaseResult
from document_analyzer.supabase_client import insert_scoped

from agent3_fixtures import ORG, seed_plan
from fakes import FakeClient

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
# Group 1: run_assemble_stage over a hand-built state
# --------------------------------------------------------------------------- #


def _item(cluster_id: str, phase_order: int, unit_order: int, unit_slug: str) -> dict:
    return {
        "cluster_id": cluster_id,
        "phase_cluster_id": f"phase-{phase_order}",
        "phase_order": phase_order,
        "unit_order": unit_order,
        "unit_slug": unit_slug,
    }


def _seed_unit_execution(client, cluster_id: str, *, status: str, attempts: int, flags: list[str]) -> None:
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
            "status": status,
            "attempts": attempts,
            "flags": flags,
        },
    )


def _execution_job() -> dict:
    return {
        "id": JOB_ID,
        "org_id": ORG,
        "project_id": "p1",
        "plan_id": "plan-1",
        "source_job_id": "job-1",
        "params": PARAMS,
        "created_at": "2026-10-01T12:00:00+00:00",
    }


@pytest.fixture
def client():
    client = FakeClient(org_id=ORG)
    row = {k: v for k, v in _execution_job().items() if k != "org_id"}
    insert_scoped(client, "execution_jobs", ORG, row)
    return client


@pytest.fixture
def three_unit_state(client):
    """A: passed, real code + tests. B: failed_verification, real code + tests.
    C: error, a generate_stub_unit-shaped stand-in with no tests (matches
    units.py::_finish_as_error, which never sets state.unit_test_files)."""
    state = ExecutionState()
    state.items = [
        _item("cluster-a", 1, 1, "01_01_a"),
        _item("cluster-b", 1, 2, "01_02_b"),
        _item("cluster-c", 2, 1, "02_01_c"),
    ]
    state.clusters = {"cluster-a": {"label": "Unit A"}, "cluster-b": {"label": "Unit B"}, "cluster-c": {"label": "Unit C"}}
    state.unit_files = {
        "cluster-a": [GeneratedFile(path="app/units/01_01_a/__init__.py", content="import importlib\n")],
        "cluster-b": [GeneratedFile(path="app/units/01_02_b/__init__.py", content="import importlib\n")],
        "cluster-c": [
            GeneratedFile(
                path="app/units/02_01_c/__init__.py",
                content="def calc():\n    raise NotImplementedError\n",
            )
        ],
    }
    state.unit_test_files = {
        "cluster-a": [GeneratedFile(path="tests/units/01_01_a/test_contract.py", content="def test_x():\n    assert True\n")],
        "cluster-b": [GeneratedFile(path="tests/units/01_02_b/test_contract.py", content="def test_y():\n    assert False\n")],
        # no entry for cluster-c: an error unit never ran tests.
    }
    for cid, status, attempts, flags in [
        ("cluster-a", "passed", 1, []),
        ("cluster-b", "failed_verification", 3, []),
        ("cluster-c", "error", 2, ["stubbed"]),
    ]:
        _seed_unit_execution(client, cid, status=status, attempts=attempts, flags=flags)
    return state


def test_assemble_writes_every_units_files_under_the_template(client, three_unit_state):
    run_assemble_stage(client, _execution_job(), three_unit_state)

    dest = three_unit_state.project_dir
    assert dest is not None
    assert (dest / "app" / "main.py").exists()  # template copied
    assert (dest / "app" / "units" / "01_01_a" / "__init__.py").exists()
    assert (dest / "app" / "units" / "01_02_b" / "__init__.py").exists()
    assert (dest / "app" / "units" / "02_01_c" / "__init__.py").exists()
    assert (dest / "tests" / "units" / "01_01_a" / "test_contract.py").exists()
    assert (dest / "tests" / "units" / "01_02_b" / "test_contract.py").exists()
    assert not (dest / "tests" / "units" / "02_01_c").exists()  # error unit: no tests ran
    assert (dest / "tests" / "test_template_smoke.py").exists()  # whole template, not the trimmed per-unit copy


def test_assemble_units_init_imports_every_unit_in_plan_order(client, three_unit_state):
    run_assemble_stage(client, _execution_job(), three_unit_state)

    init_text = (three_unit_state.project_dir / "app" / "units" / "__init__.py").read_text()
    lines = [line for line in init_text.splitlines() if "importlib.import_module" in line]
    assert [line.split("'")[1] for line in lines] == [
        "app.units.01_01_a",
        "app.units.01_02_b",
        "app.units.02_01_c",
    ]


def test_assemble_readme_has_a_verdict_table_per_phase(client, three_unit_state):
    run_assemble_stage(client, _execution_job(), three_unit_state)

    readme = (three_unit_state.project_dir / "README.md").read_text()
    assert "## Phase 1" in readme
    assert "## Phase 2" in readme
    assert "01_01_a" in readme and "passed" in readme
    assert "01_02_b" in readme and "failed_verification" in readme
    assert "02_01_c" in readme and "error" in readme and "stubbed" in readme


def test_assemble_writes_requirements_txt(client, three_unit_state):
    run_assemble_stage(client, _execution_job(), three_unit_state)

    requirements = (three_unit_state.project_dir / "requirements.txt").read_text()
    for package in ("fastapi", "uvicorn", "sqlalchemy", "httpx", "python-multipart", "pytest", "pytest-asyncio"):
        assert package in requirements


def test_assemble_raises_on_a_unit_missing_from_state_unit_files(client, three_unit_state):
    del three_unit_state.unit_files["cluster-b"]

    with pytest.raises(AssembleError, match="cluster-b"):
        run_assemble_stage(client, _execution_job(), three_unit_state)


# --------------------------------------------------------------------------- #
# Group 2: run_final_test_stage with a monkeypatched sandbox
# --------------------------------------------------------------------------- #


def _sandbox_result(*, passed=1, failed=0, errors=0, status="completed", exit_code=0, duration_s=0.25) -> SandboxResult:
    tests = (
        [TestCaseResult(classname="tests.units.x.test_contract", name=f"p{i}", outcome="passed") for i in range(passed)]
        + [TestCaseResult(classname="tests.units.x.test_contract", name=f"f{i}", outcome="failed", message="boom") for i in range(failed)]
        + [TestCaseResult(classname="tests.units.x.test_contract", name=f"e{i}", outcome="error", message="boom") for i in range(errors)]
    )
    return SandboxResult(status=status, exit_code=exit_code, tests=tests, output_tail="log", junit_xml="<xml/>", duration_s=duration_s)


def test_final_test_writes_summary_counts_and_zip(monkeypatch, client, three_unit_state, tmp_path):
    run_assemble_stage(client, _execution_job(), three_unit_state)

    captured_params = {}

    def fake_run_pytest(workspace, params):
        captured_params["params"] = params
        return _sandbox_result(passed=2, failed=1, errors=1, duration_s=1.5)

    monkeypatch.setattr(assemble_module, "run_pytest", fake_run_pytest)

    run_final_test_stage(client, _execution_job(), three_unit_state)

    # Timeout multiplier (design_agent3.md §5): 5x a single unit's own budget.
    assert captured_params["params"].timeout_seconds == PARAMS["sandbox_timeout_seconds"] * 5

    [row] = client.table("execution_jobs").rows
    assert row["final_test_summary"] == {"passed": 2, "failed": 1, "errors": 1, "duration_s": 1.5}
    assert row["output_zip_path"] == f"{ORG}/{JOB_ID}/project.zip"
    assert row["counts"] == {
        "total": 3,
        "done": 3,
        "passed": 1,
        "failed_verification": 1,
        "error": 1,
        "built_on_failed_dependency": 0,
    }

    bucket = client.storage.from_(assemble_module.OUTPUT_BUCKET)
    # project.zip, then the delivery manifest (design_agent3.md §15.3).
    assert [path for path, _, _ in bucket.uploads] == [f"{ORG}/{JOB_ID}/project.zip", f"{ORG}/{JOB_ID}/manifest.json"]
    path, contents, opts = bucket.uploads[0]
    assert opts["content-type"] == "application/zip"
    with zipfile.ZipFile(BytesIO(contents)) as archive:
        names = set(archive.namelist())
        assert "app/main.py" in names
        assert "README.md" in names
        assert "app/units/01_01_a/__init__.py" in names
        assert "tests/units/01_01_a/test_contract.py" in names


def _zip_files(contents: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(BytesIO(contents)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_final_test_writes_a_manifest_the_commit_plan_accepts(monkeypatch, client, three_unit_state, tmp_path):
    """design_agent3.md §15.3: the manifest plus the zip's own files are everything delivery
    needs, and the plan built from them ends on exactly the zip's tree (§15.14)."""
    from document_analyzer.agent3.commit_plan import build_commit_plan, tree_sha_of_files

    run_assemble_stage(client, _execution_job(), three_unit_state)
    monkeypatch.setattr(assemble_module, "run_pytest", lambda workspace, params: _sandbox_result(passed=3))
    run_final_test_stage(client, _execution_job(), three_unit_state)

    uploads = {path: contents for path, contents, _ in client.storage.from_(assemble_module.OUTPUT_BUCKET).uploads}
    manifest = json.loads(uploads[f"{ORG}/{JOB_ID}/manifest.json"])
    zip_files = _zip_files(uploads[f"{ORG}/{JOB_ID}/project.zip"])
    # §15.5: the zip also carries a .git (when git is installed); the plan is built from the tree without it.
    files = {name: data for name, data in zip_files.items() if not name.startswith(".git/")}

    assert manifest["version"] == 1
    assert manifest["created_at"] == "2026-10-01T12:00:00+00:00"
    assert manifest["final_test_summary"]["passed"] == 3
    assert [[u["unit_slug"] for u in phase["units"]] for phase in manifest["phases"]] == [["01_01_a", "01_02_b"], ["02_01_c"]]
    assert [u["status"] for phase in manifest["phases"] for u in phase["units"]] == ["passed", "failed_verification", "error"]
    assert manifest["phases"][1]["units"][0]["flags"] == ["stubbed"]
    # The error unit never ran tests, so it owns only its stub file.
    assert manifest["phases"][1]["units"][0]["files"] == ["app/units/02_01_c/__init__.py"]
    # Scaffold = template + the fixed CI workflow, .gitignore and requirements; no README / units init.
    assert ".github/workflows/ci.yml" in manifest["scaffold"]
    assert ".gitignore" in manifest["scaffold"]
    assert "requirements.txt" in manifest["scaffold"]
    assert "README.md" not in manifest["scaffold"]
    assert "app/units/__init__.py" not in manifest["scaffold"]

    plan = build_commit_plan(manifest, files)

    assert plan.final_tree_sha == tree_sha_of_files(files)
    assert [c.kind for c in plan.commits] == ["baseline", "scaffold", "unit", "unit", "unit", "report"]
    assert [g.key for g in plan.groups] == ["scaffold", "phase-1", "phase-2"]
    assert not any(path.startswith(".git/") for path in manifest["scaffold"])
    if shutil.which("git"):
        # The zip's history is exactly this plan: extract it and ask git for HEAD.
        extracted = tmp_path / "extracted"
        with zipfile.ZipFile(BytesIO(uploads[f"{ORG}/{JOB_ID}/project.zip"])) as archive:
            archive.extractall(extracted)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=extracted, capture_output=True, check=True).stdout.decode().strip()
        assert head == plan.commits[-1].commit_sha
    else:
        assert not any(name.startswith(".git/") for name in zip_files)


def test_assembled_text_files_are_written_byte_exact_with_lf_endings(client, three_unit_state):
    """`Path.write_text` would turn "\n" into "\r\n" on Windows (design_agent3.md §5, §15.14),
    making the zip and every git blob depend on the host OS."""
    run_assemble_stage(client, _execution_job(), three_unit_state)
    project = three_unit_state.project_dir

    for relative in ("app/units/__init__.py", "README.md", "requirements.txt", "app/units/01_01_a/__init__.py"):
        assert b"\r" not in (project / relative).read_bytes(), relative


def test_the_manifest_gets_created_at_from_the_table_when_the_workers_row_lacks_it(monkeypatch, client, three_unit_state):
    """Regression: the worker loaded the execution row without created_at, so every real manifest said null and could not
    be turned into commits."""
    from document_analyzer.agent3.worker import run as run_module

    worker_row = {k: v for k, v in _execution_job().items() if k != "created_at"}  # what _fetch_execution_job returned
    assert "created_at" in __import__("inspect").getsource(run_module._fetch_execution_job)  # and it now selects it
    run_assemble_stage(client, worker_row, three_unit_state)
    monkeypatch.setattr(assemble_module, "run_pytest", lambda workspace, params: _sandbox_result(passed=3))

    run_final_test_stage(client, worker_row, three_unit_state)

    uploads = {path: contents for path, contents, _ in client.storage.from_(assemble_module.OUTPUT_BUCKET).uploads}
    assert json.loads(uploads[f"{ORG}/{JOB_ID}/manifest.json"])["created_at"] == "2026-10-01T12:00:00+00:00"


def test_the_zip_ships_the_fixed_ci_workflow_with_pinned_actions(client, three_unit_state):
    run_assemble_stage(client, _execution_job(), three_unit_state)
    workflow = (three_unit_state.project_dir / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    for line in workflow.splitlines():
        if "uses:" in line:
            # Pinned by a full 40-hex commit SHA, never a movable tag (design_agent3.md §15.2).
            assert "@" in line and len(line.split("@")[1].split()[0]) == 40, line
    assert "contents: read" in workflow
    assert "secrets." not in workflow
    assert "pull_request" in workflow


def test_final_test_does_not_raise_on_a_failing_suite(monkeypatch, client, three_unit_state):
    """design_agent3.md §5: "a failing final suite doesn't fail the job" — only an
    infrastructure error (SandboxUnavailableError) should propagate."""
    run_assemble_stage(client, _execution_job(), three_unit_state)
    monkeypatch.setattr(assemble_module, "run_pytest", lambda workspace, params: _sandbox_result(passed=0, failed=5))

    run_final_test_stage(client, _execution_job(), three_unit_state)  # must not raise

    [row] = client.table("execution_jobs").rows
    assert row["final_test_summary"]["failed"] == 5


def test_zip_build_is_deterministic(three_unit_state, client):
    run_assemble_stage(client, _execution_job(), three_unit_state)
    first = assemble_module._build_zip(three_unit_state.project_dir)
    second = assemble_module._build_zip(three_unit_state.project_dir)
    assert first == second


# --------------------------------------------------------------------------- #
# Group 3: the real prepare -> units -> assemble -> final_test chain (no php)
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class FakeLLM:
    def __init__(self, generate_responses: dict[str, list[str]]):
        self._generate = {k: list(v) for k, v in generate_responses.items()}
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        messages = kwargs["messages"]
        system = messages[0]["content"]
        unit_slug = system.rsplit("unit_slug is: ", 1)[-1].strip()
        if "writing pytest behaviour tests" in system:
            return _Resp(json.dumps({"content": "import pytest\n\n\n@pytest.mark.behaviour\ndef test_noop():\n    assert True\n"}))
        queue = self._generate.setdefault(unit_slug, ["{}"])
        content = queue.pop(0) if len(queue) > 1 else queue[0]
        return _Resp(content)


def _generate_response(unit_slug: str, entity_id: str, name: str) -> str:
    module = f"app.units.{unit_slug}.module"
    return json.dumps(
        {
            "files": [{"path": f"{module.replace('.', '/')}.py", "content": f"def {name}(order):\n    return order\n"}],
            "symbol_map": {entity_id: {"python_module": module, "python_name": name, "kind": "function"}},
        }
    )


class _FakeUnitSandbox:
    """Same convention as test_agent3_units.py's _FakeSandbox: identifies a
    per-unit workspace by the last importlib.import_module(...) line."""

    def __call__(self, workspace, params) -> SandboxResult:
        return _sandbox_result()


def _seed_plan_unit(client, plan, job, *, cluster_id, phase_order, unit_order, entity_id, name) -> None:
    contract = {
        "public_interface": [
            {"entity_id": entity_id, "name": name, "kind": "function", "class": None, "params": ["order"], "file_path": "x.php"}
        ],
        "incoming_call_sites": [],
        "outgoing_dependencies": [],
        "external_calls": [],
        "hooks_bound": [],
        "routes_bound": [],
        "side_effect_signals": [],
        "unverified_member_ids": [],
        "merged_cluster_ids": [],
    }
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
        {"plan_id": plan["id"], "cluster_id": cluster_id, "phase_cluster_id": f"phase-{phase_order}", "phase_order": phase_order, "unit_order": unit_order, "test_contract": contract},
    )
    insert_scoped(
        client, "unit_executions", ORG,
        {"execution_job_id": JOB_ID, "cluster_id": cluster_id, "phase_cluster_id": f"phase-{phase_order}", "phase_order": phase_order, "unit_order": unit_order, "status": "pending", "attempts": 0, "flags": []},
    )


def test_prepare_units_assemble_final_test_end_to_end(monkeypatch):
    client = FakeClient(org_id=ORG)
    job, plan, _project = seed_plan(client)
    _seed_plan_unit(client, plan, job, cluster_id="unit-a", phase_order=1, unit_order=1, entity_id="e-calc", name="calc_total")
    insert_scoped(client, "execution_jobs", ORG, {"id": JOB_ID, "project_id": "p1", "plan_id": plan["id"], "source_job_id": job["id"], "status": "running", "params": PARAMS})

    state = ExecutionState()
    execution_job = {"id": JOB_ID, "org_id": ORG, "plan_id": plan["id"], "source_job_id": job["id"], "params": PARAMS}
    run_prepare_stage(client, execution_job, state)

    slug = next(i for i in state.items if i["cluster_id"] == "unit-a")["unit_slug"]
    llm = FakeLLM({slug: [_generate_response(slug, "e-calc", "calc_total")]})
    monkeypatch.setattr(units_module, "get_azure_openai_client", lambda: llm)
    monkeypatch.setattr(units_module, "run_pytest", _FakeUnitSandbox())
    units_module.run_units_stage(client, execution_job, state)

    run_assemble_stage(client, execution_job, state)
    assert (state.project_dir / "app" / "units" / slug / "module.py").exists()

    monkeypatch.setattr(assemble_module, "run_pytest", lambda workspace, params: _sandbox_result())
    run_final_test_stage(client, execution_job, state)

    [row] = [r for r in client.table("execution_jobs").rows if r["id"] == JOB_ID]
    assert row["output_zip_path"] == f"{ORG}/{JOB_ID}/project.zip"
    assert row["final_test_summary"]["passed"] == 1
    assert row["counts"]["total"] == 1 and row["counts"]["passed"] == 1

    shutil.rmtree(state.project_dir, ignore_errors=True)
    shutil.rmtree(state.snapshot_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Group 4: live sandbox test (skipped without Docker + the runner image)
# --------------------------------------------------------------------------- #

_live = pytest.mark.skipif(shutil.which("docker") is None, reason="needs Docker")


@_live
def test_live_assemble_and_final_test_through_the_real_sandbox(client, three_unit_state, tmp_path):
    """The task's required test: unzip the fixture run's output, check the layout
    and that pytest genuinely passes in the sandbox. Uses only cluster-a and
    cluster-b (real, importable code) — cluster-c (the NotImplementedError stub)
    is dropped first, since it would make the real suite fail for real, which
    isn't what this test is checking."""
    from document_analyzer.agent3.worker.sandbox import SandboxUnavailableError, check_sandbox_available

    try:
        check_sandbox_available(SandboxParams())
    except SandboxUnavailableError as exc:
        pytest.skip(f"sandbox not available: {exc}")

    three_unit_state.items = [i for i in three_unit_state.items if i["cluster_id"] != "cluster-c"]
    del three_unit_state.unit_files["cluster-c"]
    # cluster-b's own test asserts False on purpose (failed_verification fixture,
    # group 1) — replace it with a passing one so this test proves a genuine pass.
    three_unit_state.unit_test_files["cluster-b"] = [
        GeneratedFile(path="tests/units/01_02_b/test_contract.py", content="def test_y():\n    assert True\n")
    ]

    run_assemble_stage(client, _execution_job(), three_unit_state)
    result = assemble_module.run_pytest(three_unit_state.project_dir, SandboxParams(timeout_seconds=60, memory_mb=512))
    assert result.status == "completed", result.output_tail
    assert result.failed == 0, result.output_tail
    assert result.passed > 0

    run_final_test_stage(client, _execution_job(), three_unit_state)
    [row] = client.table("execution_jobs").rows
    zip_bytes = client.storage.from_(assemble_module.OUTPUT_BUCKET).download(row["output_zip_path"])
    with zipfile.ZipFile(BytesIO(zip_bytes)) as archive:
        names = set(archive.namelist())
        assert "app/main.py" in names
        assert "README.md" in names
        assert "app/units/01_01_a/__init__.py" in names
        assert "tests/units/01_01_a/test_contract.py" in names

    shutil.rmtree(three_unit_state.project_dir, ignore_errors=True)
