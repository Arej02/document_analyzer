"""Stages `assemble` and `final_test` — design_agent3.md §5.

`assemble` combines stage `units`'s per-unit results (`state.unit_files`,
`state.unit_test_files` — every unit's last attempt, real code or a
`generate_stub_unit` stand-in for an `error` unit, §4 "Failed dependencies")
into one whole project tree: the fixed template plus every unit's own files,
`app/units/__init__.py` importing every unit in plan order (§8's one
unit-loading mechanism), and a `README.md` verdict table. `final_test` then
runs that whole tree's test suite once for real in the sandbox, zips it, and
records the result — never raising on a failing suite (design doc §5: "the job
only fails on infrastructure errors").

Neither stage runs generated code outside the sandbox (CLAUDE.md "untrusted
input, no code execution"); `assemble` only ever reads/writes file contents on
disk.
"""

import os
import shutil
import zipfile
from io import BytesIO
from pathlib import Path
from tempfile import mkdtemp

from supabase import Client

from .generate import GeneratedFile
from .sandbox import SandboxParams, SandboxResult, run_pytest
from .status import counts_by_job, set_execution_output
from .units import _TEMPLATE_DIR

OUTPUT_BUCKET = "agent3-output"

# design_agent3.md §5: the whole assembled suite gets a longer timeout than any one
# unit's own sandbox run, since it's every unit's tests running together.
_FINAL_TEST_TIMEOUT_MULTIPLIER = 5


class AssembleError(Exception):
    """A job-level failure raised by stage `assemble` (never a unit verdict) — same
    shape as `prepare.py::PrepareError`. Currently only raised for the documented
    `state.unit_files` resume gap (design_agent3.md §4 "Known gaps"): a plan item
    with no entry means this run never (re)generated it and nothing was carried
    forward from a previous run either, so there is nothing correct to assemble."""


# --------------------------------------------------------------------------- #
# Template + tree assembly
# --------------------------------------------------------------------------- #


def _copy_whole_template(dest: Path) -> None:
    """Unlike `units.py::_copy_template` (which copies only `app/` plus
    `tests/conftest.py` and `pytest.ini`, for a minimal per-unit sandbox
    workspace), the assembled project needs the template's own tests too
    (`tests/test_template_smoke.py`) so `final_test`'s whole-suite run actually
    exercises the fixed, non-generated code as well."""
    shutil.copytree(
        _TEMPLATE_DIR / "app",
        dest / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        _TEMPLATE_DIR / "tests",
        dest / "tests",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(_TEMPLATE_DIR / "pytest.ini", dest / "pytest.ini")


def _write_files(dest: Path, files: list[GeneratedFile]) -> None:
    for f in files:
        path = dest / f.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f.content, encoding="utf-8")


def _write_units_init(dest: Path, unit_slugs: list[str]) -> None:
    """§8's one unit-loading mechanism, over every item in plan order (unlike
    `units.py::_write_units_init`, which writes the same shape but only for one
    unit's own transitive dependencies, for a per-unit sandbox workspace)."""
    lines = [
        '"""Written by stage `assemble` (design_agent3.md §5, §8): imports every '
        'unit, in plan order, so hook registrations run at startup."""',
        "",
        "import importlib",
        "",
    ]
    for slug in unit_slugs:
        module_name = f"app.units.{slug}"
        lines.append(f"importlib.import_module({module_name!r})")
    (dest / "app" / "units" / "__init__.py").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# README verdict table
# --------------------------------------------------------------------------- #


def _load_unit_rows(client: Client, org_id: str, execution_job_id: str) -> dict[str, dict]:
    """A fresh read (not `state`, which only carries the current run's in-memory
    results) so the README always reflects what's actually persisted, including a
    unit resumed from an earlier run."""
    rows: list[dict] = []
    start = 0
    page_size = 1000
    while True:
        page = (
            client.table("unit_executions")
            .select("cluster_id, status, attempts, flags")
            .eq("org_id", org_id)
            .eq("execution_job_id", execution_job_id)
            .range(start, start + page_size - 1)
            .execute()
        ).data
        rows.extend(page)
        if len(page) < page_size:
            break
        start += page_size
    return {row["cluster_id"]: row for row in rows}


def _unit_label(item: dict, state) -> str:
    cluster = state.clusters.get(item["cluster_id"])
    label = cluster.get("label") if cluster else None
    return label or item["unit_slug"]


def _render_readme(state, unit_rows: dict[str, dict]) -> str:
    lines = ["# Generated project", "", "Assembled by Agent 3 (Execute) — see each unit's verdict below.", ""]
    current_phase = None
    for item in state.items:
        if item["phase_order"] != current_phase:
            current_phase = item["phase_order"]
            lines += ["", f"## Phase {current_phase}", "", "| Unit | Status | Attempts | Flags |", "|---|---|---|---|"]
        row = unit_rows.get(item["cluster_id"], {})
        status = row.get("status", "unknown")
        attempts = row.get("attempts", 0)
        flags = ", ".join(row.get("flags") or []) or "—"
        lines.append(f"| `{item['unit_slug']}` ({_unit_label(item, state)}) | {status} | {attempts} | {flags} |")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Stage `assemble`
# --------------------------------------------------------------------------- #


def run_assemble_stage(client: Client, execution_job: dict, state) -> None:
    org_id, execution_job_id = execution_job["org_id"], execution_job["id"]

    dest = Path(mkdtemp(prefix="agent3-project-"))
    _copy_whole_template(dest)

    unit_slugs: list[str] = []
    for item in state.items:
        cluster_id, unit_slug = item["cluster_id"], item["unit_slug"]
        if cluster_id not in state.unit_files:
            raise AssembleError(
                f"unit {cluster_id!r} ({unit_slug}) has no generated files in this run "
                "(a resumed job whose earlier-finished units aren't yet re-fetched from "
                "storage — design_agent3.md §4 'Known gaps', tasks_agent3.md Phase 6)"
            )
        _write_files(dest, state.unit_files[cluster_id])
        _write_files(dest, state.unit_test_files.get(cluster_id, []))
        unit_slugs.append(unit_slug)

    _write_units_init(dest, unit_slugs)

    unit_rows = _load_unit_rows(client, org_id, execution_job_id)
    (dest / "README.md").write_text(_render_readme(state, unit_rows), encoding="utf-8")

    state.project_dir = dest


# --------------------------------------------------------------------------- #
# Stage `final_test`
# --------------------------------------------------------------------------- #


def _sandbox_params(params: dict) -> SandboxParams:
    return SandboxParams(
        image=params["runner_image"],
        timeout_seconds=params["sandbox_timeout_seconds"] * _FINAL_TEST_TIMEOUT_MULTIPLIER,
        memory_mb=params["sandbox_memory_mb"],
        runtime=os.environ.get("AGENT3_SANDBOX_RUNTIME") or None,
    )


def _final_test_summary(result: SandboxResult) -> dict:
    passed = sum(1 for t in result.tests if t.outcome == "passed")
    failed = sum(1 for t in result.tests if t.outcome == "failed")
    errors = sum(1 for t in result.tests if t.outcome == "error")
    return {"passed": passed, "failed": failed, "errors": errors, "duration_s": result.duration_s}


def _build_zip(project_dir: Path) -> bytes:
    """Deterministic contents: sorted paths, a fixed `ZipInfo.date_time` — two runs
    over the same tree produce byte-identical zips."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in project_dir.rglob("*") if p.is_file()):
            # .as_posix(), not PurePosixPath(str(...)): a plain str() round-trip
            # would keep Windows' backslash separators as literal characters instead
            # of splitting on them, corrupting the archive's path structure.
            arcname = path.relative_to(project_dir).as_posix()
            info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return buf.getvalue()


def run_final_test_stage(client: Client, execution_job: dict, state) -> None:
    org_id, execution_job_id = execution_job["org_id"], execution_job["id"]
    params = execution_job["params"]

    result = run_pytest(state.project_dir, _sandbox_params(params))
    final_test_summary = _final_test_summary(result)

    counts = counts_by_job(client, org_id, [execution_job_id])[execution_job_id]

    zip_bytes = _build_zip(state.project_dir)
    output_zip_path = f"{org_id}/{execution_job_id}/project.zip"
    client.storage.from_(OUTPUT_BUCKET).upload(output_zip_path, zip_bytes, {"content-type": "application/zip"})

    set_execution_output(
        client,
        execution_job_id,
        org_id,
        output_zip_path=output_zip_path,
        counts=counts,
        final_test_summary=final_test_summary,
    )
