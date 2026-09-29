"""Stage `units`: per-unit loop — design_agent3.md §4, specs_agent3.md FR2/FR6/FR7.

Wires the already-built steps together, in `(phase_order, unit_order)` order,
skipping `state.terminal_unit_ids` (resume, §3 step 5):

1. context.py -> `UnitContext` (`ContextError`/`ContextTooLargeError` ends the
   unit `error` immediately, generate.py's own attempts never spent).
2. generate.py -> `GeneratedUnit` (`GenerationError` after its own internal
   attempts ends the unit `error`; this is terminal, no outer retry).
3. tests_gen.py -> contract tests (deterministic) + behaviour tests (best
   effort, never raises).
4. sandbox.py -> a per-unit workspace (the template, this unit's own code, the
   last attempt of every transitive dependency, this round's tests) run
   through `run_pytest`.
5. Retry: a sandbox run with failures feeds the failing test names back into
   the *next* generate.py call (`extra_feedback`) — a different, outer loop
   from generate.py's own internal one (design doc §4 step 5) — up to
   `params.max_attempts` outer rounds.
6. Persist every attempt (not just the last) to `agent3-output`, then update
   `unit_executions`.

Two decisions worth calling out (both design_agent3.md §4, added 2026-09-28):

- **Failed dependencies** (specs FR7): a dependency with no real code — either
  because it ended `error`, or because an unresolved cross-phase
  `needs_facade` cycle (§13) puts it after the current unit in plan order —
  gets a deterministic stub (`generate.py::generate_stub_unit`). The errored
  unit itself is flagged `stubbed`; a dependent built against a stub is
  flagged `built_on_failed_dependency`.
- **`__init__.py` re-export guarantee**: `generate.py`'s prompt now asks the
  model to make its package's `__init__.py` import every other file it wrote,
  but this module never trusts that alone — `_ensure_package_init` adds the
  same guaranteed imports on top of whatever the model (or a stub) produced,
  because both `app.main`'s router discovery and this unit's own
  `hooks_bound`/`routes_bound` contract tests (§6) import the *package*, not
  a specific submodule, and rely on that import alone triggering the unit's
  module-level code.

**Known gaps, out of scope for this task:**
- §8's `app/models/{name}.py` (a unit owning a schema entity writes a shared
  model file) is not reachable: `generate.py::_validate_path` only ever
  allows `app/units/{unit_slug}/`. Recorded here rather than fixed, since
  fixing it means deciding how two units sharing one model file would coexist
  across separate per-unit sandbox workspaces — more than this task needs,
  and the fixture repo has no schema entities to exercise it.
- On a **resumed** job, `state.symbol_maps` is repopulated from
  `unit_executions.symbol_map` for units finished in an earlier run (§3 step
  5), but `state.unit_files` (this module's own in-memory cache of file
  contents, needed to assemble a *dependent's* workspace) is not — it is only
  ever filled in during the current run. A dependent of a unit finished
  before a crash would need this module to fetch that unit's last attempt
  back from storage instead. `tasks_agent3.md` Phase 6 ("Redelivery") owns
  full resumption correctness and should close this gap.
"""

import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from supabase import Client

from document_analyzer.worker.summarize import get_azure_openai_client

from .context import ContextError, build_unit_context
from .generate import (
    TARGET_ARCHITECTURE_RULES,
    GeneratedFile,
    GeneratedUnit,
    GenerationError,
    generate_stub_unit,
    generate_unit_code,
)
from .sandbox import SandboxParams, SandboxResult, run_pytest
from .status import mark_unit_running, update_unit_execution
from .tests_gen import BehaviourTestResult, generate_behaviour_tests, generate_contract_tests

OUTPUT_BUCKET = "agent3-output"

# design_agent3.md §7: "output is capped" applies to the sandbox's own reply; this
# caps how much of a *failing test* we feed back into the next generation attempt,
# so a huge assertion diff can't blow the next round's context budget.
_MAX_FEEDBACK_CHARS = 4000

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "template"


# --------------------------------------------------------------------------- #
# Template + workspace assembly
# --------------------------------------------------------------------------- #


def _copy_template(dest: Path) -> None:
    shutil.copytree(
        _TEMPLATE_DIR / "app",
        dest / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (dest / "tests").mkdir(parents=True, exist_ok=True)
    shutil.copy2(_TEMPLATE_DIR / "tests" / "conftest.py", dest / "tests" / "conftest.py")
    shutil.copy2(_TEMPLATE_DIR / "pytest.ini", dest / "pytest.ini")


def _write_files(dest: Path, files: list[GeneratedFile]) -> None:
    for f in files:
        path = dest / f.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f.content, encoding="utf-8")


def _dotted_module_name(path: str) -> str:
    posix = PurePosixPath(path)
    parts = list(posix.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = posix.stem
    return ".".join(parts)


def _ensure_package_init(files: list[GeneratedFile], unit_slug: str) -> list[GeneratedFile]:
    """Guarantees `app/units/{unit_slug}/__init__.py` imports every other file the
    unit wrote, via `importlib.import_module` (§8's digit-prefix rule — a literal
    `from . import module` is fine here since it doesn't *name* `unit_slug`, but
    `importlib` is used anyway for one uniform mechanism). See the module docstring:
    this is a guarantee, not a hope that the model followed the prompt."""
    init_path = f"app/units/{unit_slug}/__init__.py"
    other_modules = sorted(_dotted_module_name(f.path) for f in files if f.path != init_path)
    if not other_modules:
        return files

    import_lines = "\n".join(f"importlib.import_module({m!r})" for m in other_modules)
    guaranteed = f"import importlib\n\n{import_lines}\n"

    existing = next((f for f in files if f.path == init_path), None)
    others = [f for f in files if f.path != init_path]
    content = f"{existing.content.rstrip(chr(10))}\n\n{guaranteed}" if existing else guaranteed
    return [*others, GeneratedFile(path=init_path, content=content)]


def _write_units_init(dest: Path, unit_slug: str, dependency_slugs: list[str]) -> None:
    lines = [
        '"""Per-unit sandbox workspace (design_agent3.md §4 step 4, §8): imports only '
        'this unit and its (transitive) dependencies, never the whole plan."""',
        "",
        "import importlib",
        "",
    ]
    for slug in [*dependency_slugs, unit_slug]:
        module_name = f"app.units.{slug}"
        lines.append(f"importlib.import_module({module_name!r})")
    (dest / "app" / "units" / "__init__.py").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_workspace(
    unit_slug: str,
    own_files: list[GeneratedFile],
    dependency_slugs: list[str],
    dependency_files_by_slug: dict[str, list[GeneratedFile]],
    test_files: list[GeneratedFile],
) -> Path:
    workspace = Path(tempfile.mkdtemp(prefix="agent3-unit-"))
    _copy_template(workspace)
    _write_files(workspace, own_files)
    for files in dependency_files_by_slug.values():
        _write_files(workspace, files)
    _write_units_init(workspace, unit_slug, dependency_slugs)
    _write_files(workspace, test_files)
    return workspace


# --------------------------------------------------------------------------- #
# Transitive dependency resolution
# --------------------------------------------------------------------------- #


def _direct_dependencies(cluster_id: str, items_by_cluster_id: dict) -> list[str]:
    item = items_by_cluster_id.get(cluster_id) or {}
    deps = (item.get("test_contract") or {}).get("outgoing_dependencies") or []
    return [d["to_unit_id"] for d in deps]


def _sort_key(cluster_id: str, items_by_cluster_id: dict) -> tuple:
    item = items_by_cluster_id.get(cluster_id)
    return (0, item["phase_order"], item["unit_order"]) if item else (1, cluster_id)


def _transitive_dependencies(cluster_id: str, items_by_cluster_id: dict) -> list[str]:
    """BFS over `outgoing_dependencies`, deduplicated. A visited set means an
    unresolved cross-phase `needs_facade` cycle (§13) — where two units depend on
    each other — can never loop forever; it just includes both once each."""
    seen: set[str] = set()
    queue = list(_direct_dependencies(cluster_id, items_by_cluster_id))
    seen.update(queue)
    result = list(queue)
    while queue:
        current = queue.pop(0)
        for dep_id in _direct_dependencies(current, items_by_cluster_id):
            if dep_id not in seen:
                seen.add(dep_id)
                result.append(dep_id)
                queue.append(dep_id)
    return sorted(result, key=lambda cid: _sort_key(cid, items_by_cluster_id))


def _resolve_dependency(dep_id: str, items_by_cluster_id: dict, state) -> tuple[str, list[GeneratedFile], bool]:
    """Returns (unit_slug, files, is_stub) for one transitive dependency. `is_stub`
    drives the `built_on_failed_dependency` flag on the *current* unit."""
    dep_item = items_by_cluster_id.get(dep_id)
    if dep_id in state.unit_files:
        slug = dep_item["unit_slug"] if dep_item else dep_id
        return slug, state.unit_files[dep_id], dep_id in state.stubbed_units
    if dep_item is None:
        # No contract to build even a stub from; the dependent's workspace simply
        # won't have this module (design_agent3.md §13, a sparse/inconsistent graph).
        return dep_id, [], True
    stub = generate_stub_unit(dep_item, dep_item["unit_slug"])
    return dep_item["unit_slug"], _ensure_package_init(stub.files, dep_item["unit_slug"]), True


# --------------------------------------------------------------------------- #
# Test summary + retry feedback
# --------------------------------------------------------------------------- #


def _build_test_summary(sandbox_result: SandboxResult, behaviour_result: BehaviourTestResult) -> dict:
    contract = {"passed": 0, "failed": 0}
    behaviour = {"passed": 0, "failed": 0}
    for t in sandbox_result.tests:
        bucket = behaviour if "test_behaviour" in t.classname else contract if "test_contract" in t.classname else None
        if bucket is None:
            continue
        if t.outcome == "passed":
            bucket["passed"] += 1
        elif t.outcome in ("failed", "error"):
            bucket["failed"] += 1
    if not behaviour_result.generated:
        behaviour["generated"] = False
    return {"contract": contract, "behaviour": behaviour, "duration_s": sandbox_result.duration_s}


def _sandbox_succeeded(result: SandboxResult) -> bool:
    """§7's "empty test list... treated as failing" extends to the verdict here too:
    a clean-looking but empty run (a collection error, or nothing generated) is not
    a pass. `exit_code == 0` also rules out pytest's own "no tests collected" (5)."""
    return result.status == "completed" and result.exit_code == 0 and result.failed == 0 and len(result.tests) > 0


def _failure_feedback(result: SandboxResult) -> str:
    lines = [f"{t.classname}::{t.name}: {t.message or t.outcome}" for t in result.tests if t.outcome in ("failed", "error")]
    if not lines:
        lines = [f"sandbox run ended {result.status} (exit code {result.exit_code})"]
    return "\n".join(lines)[:_MAX_FEEDBACK_CHARS]


def _last_error(result: SandboxResult) -> str:
    if result.status != "completed":
        return f"sandbox run {result.status} (exit code {result.exit_code})"
    failing = result.failed
    return f"{failing} test(s) still failing after every attempt" if failing else "verification failed"


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def _attempt_prefix(org_id: str, execution_job_id: str, cluster_id: str, attempt: int) -> str:
    return f"{org_id}/{execution_job_id}/units/{cluster_id}/attempt_{attempt}/"


def _unit_storage_prefix(org_id: str, execution_job_id: str, cluster_id: str) -> str:
    return f"{org_id}/{execution_job_id}/units/{cluster_id}/"


def _upload_attempt(
    client: Client,
    org_id: str,
    execution_job_id: str,
    cluster_id: str,
    attempt: int,
    code_files: list[GeneratedFile],
    test_files: list[GeneratedFile],
    sandbox_result: SandboxResult,
) -> None:
    """design_agent3.md §2's object layout, exactly: `.../attempt_{n}/code/...` (the
    generated code, its own `app/units/...` path kept under `code/`), `.../tests/...`
    (test files already start with `tests/`, so no extra segment is added),
    `.../log.txt`, `.../junit.xml`."""
    prefix = _attempt_prefix(org_id, execution_job_id, cluster_id, attempt)
    bucket = client.storage.from_(OUTPUT_BUCKET)
    for f in code_files:
        bucket.upload(f"{prefix}code/{f.path}", f.content.encode("utf-8"), {"content-type": "text/x-python"})
    for f in test_files:
        bucket.upload(f"{prefix}{f.path}", f.content.encode("utf-8"), {"content-type": "text/x-python"})
    bucket.upload(f"{prefix}log.txt", sandbox_result.output_tail.encode("utf-8"), {"content-type": "text/plain"})
    bucket.upload(f"{prefix}junit.xml", sandbox_result.junit_xml.encode("utf-8"), {"content-type": "application/xml"})


# --------------------------------------------------------------------------- #
# Per-unit orchestration
# --------------------------------------------------------------------------- #


def _sandbox_params(params: dict) -> SandboxParams:
    return SandboxParams(
        image=params["runner_image"],
        timeout_seconds=params["sandbox_timeout_seconds"],
        memory_mb=params["sandbox_memory_mb"],
        runtime=os.environ.get("AGENT3_SANDBOX_RUNTIME") or None,
    )


def _finish_as_error(client: Client, execution_job: dict, state, item: dict, flags: set[str], attempts: int, error: str) -> None:
    """specs FR7 `error`: no usable code at all (a `ContextError` before generation
    even started, or `generate_unit_code` exhausting its own attempts). Still
    produces a stub (§4 "Failed dependencies") so dependents can build against it —
    stored into `state` and flagged `stubbed` on this unit's own row."""
    org_id, cluster_id, unit_slug = execution_job["org_id"], item["cluster_id"], item["unit_slug"]
    stub = generate_stub_unit(item, unit_slug)
    stub_files = _ensure_package_init(stub.files, unit_slug)
    state.symbol_maps[cluster_id] = stub.symbol_map
    state.unit_files[cluster_id] = stub_files
    state.stubbed_units.add(cluster_id)
    update_unit_execution(
        client,
        execution_job["id"],
        org_id,
        cluster_id,
        status="error",
        attempts=attempts,
        symbol_map=stub.symbol_map,
        test_summary=None,
        last_error=error[:2000],
        flags=sorted(flags | {"stubbed"}),
        storage_prefix=None,
    )


def _run_one_unit(client: Client, execution_job: dict, state, item: dict, items_by_cluster_id: dict, llm_client) -> None:
    org_id, execution_job_id = execution_job["org_id"], execution_job["id"]
    cluster_id, unit_slug = item["cluster_id"], item["unit_slug"]
    params = execution_job["params"]
    test_contract = item.get("test_contract") or {}

    mark_unit_running(client, execution_job_id, org_id, cluster_id)

    flags: set[str] = set()
    if test_contract.get("unverified_member_ids"):
        flags.add("unverified_inputs")

    dependency_slugs: list[str] = []
    dependency_files_by_slug: dict[str, list[GeneratedFile]] = {}
    for dep_id in _transitive_dependencies(cluster_id, items_by_cluster_id):
        slug, files, is_stub = _resolve_dependency(dep_id, items_by_cluster_id, state)
        if not files:
            continue
        dependency_slugs.append(slug)
        dependency_files_by_slug[slug] = files
        if is_stub:
            flags.add("built_on_failed_dependency")

    try:
        context = build_unit_context(
            item, state, max_context_chars=params["max_context_chars"], rules_reserved_chars=len(TARGET_ARCHITECTURE_RULES)
        )
    except ContextError as exc:
        _finish_as_error(client, execution_job, state, item, flags, attempts=0, error=str(exc))
        return

    max_attempts = params["max_attempts"]
    sandbox_params = _sandbox_params(params)
    feedback: str | None = None
    last: dict = {}

    for attempt in range(1, max_attempts + 1):
        try:
            generated = generate_unit_code(
                context, unit_slug, llm_client, params["model_deployment"], max_attempts, extra_feedback=feedback
            )
        except GenerationError as exc:
            _finish_as_error(client, execution_job, state, item, flags, attempts=attempt, error=str(exc))
            return

        own_files = _ensure_package_init(generated.files, unit_slug)
        contract_files = generate_contract_tests(unit_slug, test_contract, generated.symbol_map, context.dependencies)
        behaviour_result = generate_behaviour_tests(
            context, unit_slug, generated, llm_client, params["model_deployment"], max_attempts
        )
        test_files = [*contract_files, *behaviour_result.files]

        workspace = _build_workspace(unit_slug, own_files, dependency_slugs, dependency_files_by_slug, test_files)
        try:
            sandbox_result = run_pytest(workspace, sandbox_params)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

        _upload_attempt(client, org_id, execution_job_id, cluster_id, attempt, own_files, test_files, sandbox_result)
        last = {
            "generated": generated,
            "own_files": own_files,
            "test_files": test_files,
            "sandbox": sandbox_result,
            "behaviour": behaviour_result,
        }

        if _sandbox_succeeded(sandbox_result):
            update_unit_execution(
                client,
                execution_job_id,
                org_id,
                cluster_id,
                status="passed",
                attempts=attempt,
                symbol_map=generated.symbol_map,
                test_summary=_build_test_summary(sandbox_result, behaviour_result),
                last_error=None,
                flags=sorted(flags),
                storage_prefix=_unit_storage_prefix(org_id, execution_job_id, cluster_id),
            )
            state.symbol_maps[cluster_id] = generated.symbol_map
            state.unit_files[cluster_id] = own_files
            state.unit_test_files[cluster_id] = test_files
            return

        if attempt < max_attempts:
            feedback = _failure_feedback(sandbox_result)

    update_unit_execution(
        client,
        execution_job_id,
        org_id,
        cluster_id,
        status="failed_verification",
        attempts=max_attempts,
        symbol_map=last["generated"].symbol_map,
        test_summary=_build_test_summary(last["sandbox"], last["behaviour"]),
        last_error=_last_error(last["sandbox"]),
        flags=sorted(flags),
        storage_prefix=_unit_storage_prefix(org_id, execution_job_id, cluster_id),
    )
    state.symbol_maps[cluster_id] = last["generated"].symbol_map
    state.unit_files[cluster_id] = last["own_files"]
    state.unit_test_files[cluster_id] = last["test_files"]


def run_units_stage(client: Client, execution_job: dict, state) -> None:
    """design_agent3.md §4. Units in `(phase_order, unit_order)` order (already how
    `state.items` is sorted by `prepare.py`, specs FR2), skipping already-terminal
    ones (resume, §3 step 5). The LLM client is built lazily — only once there is at
    least one non-terminal unit — so a job with nothing left to do never requires
    Azure OpenAI credentials at all."""
    pending = [item for item in state.items if item["cluster_id"] not in state.terminal_unit_ids]
    if not pending:
        return
    items_by_cluster_id = {item["cluster_id"]: item for item in state.items}
    llm_client = get_azure_openai_client()
    for item in pending:
        _run_one_unit(client, execution_job, state, item, items_by_cluster_id, llm_client)
