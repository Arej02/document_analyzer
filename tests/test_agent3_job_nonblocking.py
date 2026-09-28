"""Phase 3's last checkbox: a unit forced to `error` and another to
`failed_verification` never block the job — design_agent3.md §4 "Failed
dependencies", specs_agent3.md FR1/FR7.

Run at the `process_execution_job` level (design_agent3.md §10), following
`test_agent3_worker_run.py`'s `_patch_stages` pattern: `stage_assemble` and
`stage_final_test` are monkeypatched to trivial no-ops — Phase 4 stand-ins,
not real implementations, since assembling a project and running its full
suite is out of this task's scope — while `stage_prepare` and `stage_units`
run completely unmodified, against the real `agent2_cycle_repo` fixture
pipeline (Agent 1's real PHP extractors + Agent 2's real worker, same as
`test_agent2_pipeline_fixture.py`), a fake Agent 3 LLM client, and a
monkeypatched `sandbox.run_pytest`.

PHP-skip-guarded like that file: skipped on this machine (no `php` binary).
"""

import json
import shutil
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from document_analyzer.agent2.worker import run as agent2_run
from document_analyzer.agent3.router import _counts_by_job
from document_analyzer.agent3.worker import run as agent3_run
from document_analyzer.agent3.worker import units as units_module
from document_analyzer.agent3.worker.sandbox import SandboxResult, TestCaseResult
from document_analyzer.agent3.worker.status import TERMINAL_UNIT_STATUSES
from document_analyzer.worker.persist import persist_extraction_result
from document_analyzer.worker.php_extractor import PhpExtractor
from document_analyzer.worker.wordpress_extractor import WordPressExtractor

from fakes import FakeClient

pytestmark = pytest.mark.skipif(shutil.which("php") is None, reason="needs the php binary for Agent 1's extractors")

FIXTURE = Path(__file__).parent / "fixtures" / "agent2_cycle_repo"
ORG = "org-1"
PLANNING_PARAMS = {"seed": 42, "alpha": 0.5, "max_unit_size": 25, "hub_percentile": 99, "leiden_resolution": 1.0}
EXECUTION_PARAMS = {
    "model_deployment": "gpt-test",
    "max_attempts": 3,
    "max_context_chars": 60_000,
    "sandbox_timeout_seconds": 120,
    "sandbox_memory_mb": 1024,
    "runner_image": "galvanise-agent3-runner:0.1",
}

# The unit whose own public_interface includes this name is forced to `error` (the LLM
# always answers invalid JSON); the one including this name is forced to
# `failed_verification` (the sandbox always reports a failing test) — regardless of
# unit_slug, which depends on Leiden clustering + the fake report LLM's label and isn't
# hardcoded here.
ERROR_MARKER = "tax_rate"
FAILING_MARKER = "cycle_render_invoice"


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class Agent2FakeLLM:
    """Same shape as test_agent2_pipeline_fixture.py's: grounded fact-checking,
    a fixed label/summary for every cluster report."""

    def __init__(self):
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        if "fact-checking" in kwargs["messages"][0]["content"]:
            return _Resp(json.dumps({"grounded": True, "issues": []}))
        return _Resp(json.dumps({"label": "A unit", "summary_text": "Does things."}))


class Agent3FakeLLM:
    """Routes by this unit's own `public_interface` names (read back out of
    `context.text`, embedded verbatim as the first JSON value in the user
    message — `json.JSONDecoder().raw_decode` parses it whether or not a later
    outer-retry round appended feedback text after it) rather than by
    `unit_slug`, which this test can't predict ahead of time."""

    def __init__(self):
        self.chat = type("Chat", (), {"completions": self})()
        self.calls: list[str] = []

    def create(self, **kwargs):
        messages = kwargs["messages"]
        system, user = messages[0]["content"], messages[1]["content"]
        if "writing pytest behaviour tests" in system:
            return _Resp(json.dumps({"content": "import pytest\n\n\n@pytest.mark.behaviour\ndef test_noop():\n    assert True\n"}))

        unit_slug = system.rsplit("unit_slug is: ", 1)[-1].strip()
        self.calls.append(unit_slug)
        context_data, _ = json.JSONDecoder().raw_decode(user)
        public_interface = (context_data.get("test_contract") or {}).get("public_interface") or []
        names = {e["name"] for e in public_interface}
        if ERROR_MARKER in names:
            return _Resp("not valid json")
        return _Resp(_generic_generate_response(unit_slug, context_data))


def _generic_generate_response(unit_slug: str, context_data: dict) -> str:
    """A plain function per `public_interface` entry, referencing every
    dependency's own module (real or a stub — `context.dependencies` carries
    whichever it is, design_agent3.md §4 step 1) so the `outgoing_dependencies`
    contract test (§6) can find it. Never registers hooks/routes, so a unit
    with `hooks_bound`/`routes_bound` naturally fails its own contract test —
    not relied on by this test (the failing unit is forced at the sandbox
    level instead), but a realistic, undramatic reason some *other* untargeted
    unit might also end up `failed_verification`, which is fine: this test
    only asserts the two units it targets end up where expected, not that
    every other unit passes."""
    test_contract = context_data.get("test_contract") or {}
    dependencies = context_data.get("dependencies") or []
    module = f"app.units.{unit_slug}.module"

    dep_modules = sorted(
        {sig["python_module"] for dep in dependencies for sig in (dep.get("signatures") or []) if sig.get("python_module")}
    )
    dep_body = "\n".join(f"    importlib.import_module({m!r})" for m in dep_modules) or "    pass"

    functions, symbol_map = [], {}
    for entry in test_contract.get("public_interface") or []:
        functions.append(f"def {entry['name']}(*args, **kwargs):\n{dep_body}\n    return None")
        symbol_map[entry["entity_id"]] = {"python_module": module, "python_name": entry["name"], "kind": "function"}
    if not functions:
        functions = [f"def _noop():\n{dep_body}\n    return None"]

    content = "import importlib\n\n\n" + "\n\n\n".join(functions) + "\n"
    return json.dumps({"files": [{"path": f"app/units/{unit_slug}/module.py", "content": content}], "symbol_map": symbol_map})


def _sandbox_result(*, failed: bool) -> SandboxResult:
    tests = (
        [TestCaseResult(classname="tests.units.x.test_contract", name="c0", outcome="passed")]
        if not failed
        else [TestCaseResult(classname="tests.units.x.test_contract", name="c0", outcome="failed", message="forced failure")]
    )
    return SandboxResult(status="completed", exit_code=0 if not failed else 1, tests=tests, output_tail="log", junit_xml="<xml/>", duration_s=0.05)


class FakeSandbox:
    """Identifies a workspace by its *own* unit's generated source (the last
    `importlib.import_module(...)` line in the per-unit `app/units/__init__.py`
    names this unit's slug), and fails only the one whose own code defines the
    forced-failing marker function."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, workspace, params) -> SandboxResult:
        init_text = (workspace / "app" / "units" / "__init__.py").read_text()
        own_slug = [line for line in init_text.splitlines() if "importlib.import_module" in line][-1].split("'")[1].rsplit(".", 1)[-1]
        own_module = workspace / "app" / "units" / own_slug / "module.py"
        own_code = own_module.read_text() if own_module.exists() else ""
        self.calls.append(own_slug)
        return _sandbox_result(failed=f"def {FAILING_MARKER}(" in own_code)


def _tar_fixture_repo(root: Path) -> bytes:
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for file_path in sorted(p for p in root.rglob("*") if p.is_file()):
            # .as_posix(), not str(): raw_facts.file_path is always POSIX-style
            # (design_agent3.md §4 step 1), and this runs on Windows too, where
            # str(Path.relative_to(...)) would give backslash-separated names.
            archive.add(file_path, arcname=file_path.relative_to(root).as_posix())
    return buf.getvalue()


def _seed_and_run_agent2(monkeypatch) -> FakeClient:
    client = FakeClient()
    client.cascades["clusters"] = [
        ("clusters", "parent_cluster_id"),
        ("cluster_members", "cluster_id"),
        ("cluster_reports", "cluster_id"),
        ("cluster_edges", "from_cluster_id"),
    ]
    for extractor in (PhpExtractor(), WordPressExtractor()):
        persist_extraction_result(client, "job-1", ORG, extractor.extract(FIXTURE))

    client.table("projects").insert(
        {"id": "project-1", "org_id": ORG, "source_type": "github", "source_ref": "https://x/y", "latest_job_id": "job-1"}
    ).execute()
    client.table("ingestion_jobs").insert(
        {"id": "job-1", "org_id": ORG, "project_id": "project-1", "status": "done", "source_snapshot_path": f"{ORG}/job-1/source.tar.gz"}
    ).execute()
    client.storage.from_("agent1-snapshots").upload(f"{ORG}/job-1/source.tar.gz", _tar_fixture_repo(FIXTURE))
    client.table("planning_jobs").insert(
        {
            "id": "pj-1",
            "org_id": ORG,
            "project_id": "project-1",
            "source_job_id": "job-1",
            "status": "queued",
            "progress_stage": None,
            "params": PLANNING_PARAMS,
        }
    ).execute()

    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-test")
    monkeypatch.setattr(agent2_run, "get_azure_openai_client", Agent2FakeLLM)
    monkeypatch.setattr(agent2_run, "embed_text", lambda text: [0.0] * 384)
    monkeypatch.setattr(agent2_run, "get_service_role_client", lambda: client)
    agent2_run.process_planning_job("pj-1", ORG)
    return client


def _approve_plan_and_seed_execution(client: FakeClient) -> dict:
    [plan] = [p for p in client.table("migration_plans").rows if p["planning_job_id"] == "pj-1"]
    client.table("migration_plans").update({"status": "approved"}).eq("id", plan["id"]).execute()

    items = [i for i in client.table("migration_plan_items").rows if i["plan_id"] == plan["id"]]
    client.table("execution_jobs").insert(
        {
            "id": "ej-1",
            "org_id": ORG,
            "project_id": "project-1",
            "plan_id": plan["id"],
            "source_job_id": "job-1",
            "status": "queued",
            "progress_stage": None,
            "params": EXECUTION_PARAMS,
        }
    ).execute()
    for item in items:
        client.table("unit_executions").insert(
            {
                "id": f"ue-{item['cluster_id']}",
                "org_id": ORG,
                "execution_job_id": "ej-1",
                "cluster_id": item["cluster_id"],
                "phase_cluster_id": item["phase_cluster_id"],
                "phase_order": item["phase_order"],
                "unit_order": item["unit_order"],
                "status": "pending",
                "attempts": 0,
                "flags": [],
            }
        ).execute()
    return plan


def _unit_of_name(client: FakeClient) -> dict[str, str]:
    names = {e["id"]: e["name"] for e in client.table("extracted_entities").rows}
    return {names[m["canonical_entity_id"]]: m["cluster_id"] for m in client.table("cluster_members").rows}


def test_error_and_failed_verification_units_never_block_the_job(monkeypatch):
    client = _seed_and_run_agent2(monkeypatch)
    _approve_plan_and_seed_execution(client)
    unit_of = _unit_of_name(client)
    error_unit_id, failing_unit_id = unit_of[ERROR_MARKER], unit_of[FAILING_MARKER]

    # billing_total's unit calls tax_rate (test_agent2_pipeline_fixture.py's own
    # assertion), so it's a real dependent of the forced-`error` unit.
    dependent_id = unit_of["billing_total"]
    assert dependent_id != error_unit_id

    agent3_llm = Agent3FakeLLM()
    fake_sandbox = FakeSandbox()
    monkeypatch.setattr(agent3_run, "get_service_role_client", lambda: client)
    monkeypatch.setattr(units_module, "get_azure_openai_client", lambda: agent3_llm)
    monkeypatch.setattr(units_module, "run_pytest", fake_sandbox)
    # Phase 4 stand-ins, not real implementations: assembling a project and running its
    # full suite is out of scope for this check (tasks_agent3.md Phase 4).
    monkeypatch.setattr(agent3_run, "stage_assemble", lambda c, execution_job, state: None)
    monkeypatch.setattr(agent3_run, "stage_final_test", lambda c, execution_job, state: None)

    agent3_run.process_execution_job("ej-1", ORG)

    [job_row] = [r for r in client.table("execution_jobs").rows if r["id"] == "ej-1"]
    assert job_row["status"] == "done"

    rows = {r["cluster_id"]: r for r in client.table("unit_executions").rows if r["execution_job_id"] == "ej-1"}

    # Nothing left pending/running: every unit reached a terminal verdict (specs FR1
    # "done means every unit reached a terminal verdict, not that every unit passed").
    assert all(r["status"] in TERMINAL_UNIT_STATUSES for r in rows.values())

    error_row = rows[error_unit_id]
    assert error_row["status"] == "error"
    # Membership, not exact equality: this cycle is flagged needs_facade (spans two
    # phases, design_agent2.md §7 / test_agent2_pipeline_fixture.py's own assertion) —
    # tax_rate's own unresolved dependency (shipping_cost) could plausibly still be
    # unprocessed when tax_rate runs, which would *also* flag tax_rate's own row
    # built_on_failed_dependency (an ad hoc stub, units.py::_resolve_dependency) on top
    # of stubbed. Either way it never blocks anything, which is what this test checks.
    assert "stubbed" in error_row["flags"]

    failing_row = rows[failing_unit_id]
    assert failing_row["status"] == "failed_verification"

    # The dependent of the errored unit wasn't skipped or blocked: it still ran to a
    # terminal verdict, built against the NotImplementedError stub, flagged accordingly.
    dependent_row = rows[dependent_id]
    assert dependent_row["status"] in ("passed", "failed_verification")
    assert "built_on_failed_dependency" in dependent_row["flags"]

    # unit_executions counts match router.py::_counts_by_job's own live computation —
    # the same shape/values the status and results endpoints would show a reviewer.
    [counts] = _counts_by_job(client, ORG, ["ej-1"]).values()
    assert counts["total"] == len(rows)
    assert counts["done"] == len(rows)  # every unit is terminal
    assert counts["done"] == counts["passed"] + counts["failed_verification"] + counts["error"]
    assert counts["error"] >= 1
    assert counts["failed_verification"] >= 1
    assert counts["built_on_failed_dependency"] >= 1
    assert counts == {
        "total": len(rows),
        "done": sum(1 for r in rows.values() if r["status"] in TERMINAL_UNIT_STATUSES),
        "passed": sum(1 for r in rows.values() if r["status"] == "passed"),
        "failed_verification": sum(1 for r in rows.values() if r["status"] == "failed_verification"),
        "error": sum(1 for r in rows.values() if r["status"] == "error"),
        "built_on_failed_dependency": sum(1 for r in rows.values() if "built_on_failed_dependency" in (r.get("flags") or [])),
    }

    # The error unit's LLM was actually invoked (proving it was really attempted, not
    # just skipped). Every non-error unit reaches the sandbox at least once (a
    # `failed_verification` one, like the forced-failing unit here, reaches it up to
    # `max_attempts` times on retries, hence >=, not ==) — including the dependent,
    # whose workspace had to build successfully against the errored unit's stub
    # (an unresolved import there would raise inside the sandbox call itself, which
    # would have surfaced as an unhandled exception failing the whole job, not `done`).
    assert agent3_llm.calls  # at least one unit was generated
    non_error_units = sum(1 for r in rows.values() if r["status"] != "error")
    assert len(fake_sandbox.calls) >= non_error_units
