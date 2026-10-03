"""Stages `assemble` and `final_test` — design_agent3.md §5.

`assemble` combines stage `units`'s per-unit results (`state.unit_files`,
`state.unit_test_files` — every unit's last attempt, real code or a
`generate_stub_unit` stand-in for an `error` unit, §4 "Failed dependencies")
into one whole project tree: the fixed template plus every unit's own files,
`app/units/__init__.py` importing every unit in plan order (§8's one
unit-loading mechanism), a `README.md` verdict table, and a `requirements.txt`
pinning the assembled project's own runtime + test dependencies. `final_test` then
runs that whole tree's test suite once for real in the sandbox, zips it, and
records the result — never raising on a failing suite (design doc §5: "the job
only fails on infrastructure errors").

Neither stage runs generated code outside the sandbox (CLAUDE.md "untrusted
input, no code execution"); `assemble` only ever reads/writes file contents on
disk.
"""

import json
import os
import shutil
import zipfile
from io import BytesIO
from pathlib import Path
from tempfile import mkdtemp

from supabase import Client

from ..project_layout import README_PATH, UNITS_INIT_PATH, render_units_init
from .generate import GeneratedFile
from .git_history import try_add_git_history
from .sandbox import SandboxParams, SandboxResult, run_pytest
from .status import counts_by_job, set_execution_output
from .units import _TEMPLATE_DIR

OUTPUT_BUCKET = "agent3-output"
# design_agent3.md §15.3: the delivery manifest sits next to project.zip.
MANIFEST_VERSION = 1

# design_agent3.md §5: the assembled project's own runtime + test deps, pinned to
# match the root pyproject.toml's constraints (the only place all seven already
# appear together) — a static constant rather than parsed from that file at
# assemble time, to avoid coupling this stage to the dev project's own layout.
REQUIREMENTS_TXT = """fastapi>=0.141.1
uvicorn[standard]>=0.53.0
sqlalchemy==2.1.0
httpx>=0.28.1
python-multipart>=0.0.32
pytest>=9.1.1
pytest-asyncio>=1.4.0
"""

# design_agent3.md §5: the whole assembled suite gets a longer timeout than any one
# unit's own sandbox run, since it's every unit's tests running together.
_FINAL_TEST_TIMEOUT_MULTIPLIER = 5


class AssembleError(Exception):
    """A job-level failure raised by stage `assemble` (never a unit verdict) — same
    shape as `prepare.py::PrepareError`. A plan item with no `state.unit_files` entry
    means neither this run's `units.py` nor `prepare.py`'s resume path
    (`_load_resume_state`, §3 step 5) ever produced one for it — since resume now
    repopulates every already-terminal unit (tasks_agent3.md Phase 6 "Redelivery"),
    this should only fire if `units.py` itself failed to reach a terminal status for
    some item, a bug elsewhere rather than an expected gap."""


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
    # design_agent3.md §15.2: the scaffold also carries the fixed CI workflow and .gitignore.
    shutil.copy2(_TEMPLATE_DIR / ".gitignore", dest / ".gitignore")
    (dest / ".github" / "workflows").mkdir(parents=True)
    shutil.copy2(_TEMPLATE_DIR / ".github" / "workflows" / "ci.yml", dest / ".github" / "workflows" / "ci.yml")


def _write_text(path: Path, text: str) -> None:
    """UTF-8 bytes exactly as given. `Path.write_text` would translate "
" to "
" on
    Windows, so the same run would yield different zips (and different git blobs, §15.14)
    depending on the host OS, and CRLF would end up inside generated code."""
    path.write_bytes(text.encode("utf-8"))


def _write_files(dest: Path, files: list[GeneratedFile]) -> None:
    for f in files:
        path = dest / f.path
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text(path, f.content)


def _write_units_init(dest: Path, unit_slugs: list[str]) -> None:
    """§8's one unit-loading mechanism, over every item in plan order (unlike
    `units.py::_write_units_init`, which writes the same shape but only for one
    unit's own transitive dependencies, for a per-unit sandbox workspace). The text
    comes from `project_layout.render_units_init`, shared with the commit plan
    (§15.2) so every intermediate commit imports only the units committed so far."""
    _write_text(dest / "app" / "units" / "__init__.py", render_units_init(unit_slugs))


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
    _write_text(dest / "README.md", _render_readme(state, unit_rows))
    _write_text(dest / "requirements.txt", REQUIREMENTS_TXT)

    state.project_dir = dest


# --------------------------------------------------------------------------- #
# Delivery manifest (design_agent3.md §15.3)
# --------------------------------------------------------------------------- #


def _project_file_paths(project_dir: Path) -> list[str]:
    return sorted(p.relative_to(project_dir).as_posix() for p in project_dir.rglob("*") if p.is_file())


def build_manifest(execution_job: dict, state, unit_rows: dict[str, dict], final_test_summary: dict) -> dict:
    """The only link between `project.zip` and the plan: which files are scaffold, and for
    each phase and unit (in plan order) its verdict and the files it owns. Pure over its
    arguments plus a directory listing of `state.project_dir`; the commit plan
    (`commit_plan.py`) validates all of it again before anything is committed."""
    unit_paths: set[str] = set()
    phases: list[dict] = []
    for item in state.items:
        files = sorted(f.path for f in state.unit_files[item["cluster_id"]]) + sorted(
            f.path for f in state.unit_test_files.get(item["cluster_id"], [])
        )
        unit_paths.update(files)
        if not phases or phases[-1]["phase_order"] != item["phase_order"]:
            phase_cluster = state.clusters.get(item["phase_cluster_id"]) or {}
            phases.append(
                {
                    "phase_order": item["phase_order"],
                    "phase_cluster_id": item["phase_cluster_id"],
                    "label": phase_cluster.get("label"),
                    "units": [],
                }
            )
        row = unit_rows.get(item["cluster_id"], {})
        phases[-1]["units"].append(
            {
                "cluster_id": item["cluster_id"],
                "unit_slug": item["unit_slug"],
                "label": _unit_label(item, state),
                "status": row.get("status", "unknown"),
                "attempts": row.get("attempts", 0),
                "flags": sorted(row.get("flags") or []),
                "files": files,
            }
        )
    scaffold = [
        path
        for path in _project_file_paths(state.project_dir)
        if path not in unit_paths and path not in (README_PATH, UNITS_INIT_PATH)
    ]
    return {
        "version": MANIFEST_VERSION,
        "created_at": execution_job.get("created_at"),
        "scaffold": scaffold,
        "phases": phases,
        "final_test_summary": final_test_summary,
    }


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


def _with_created_at(client: Client, execution_job: dict) -> dict:
    """The manifest's commit dates derive from the execution's `created_at` (design_agent3.md §15.2). The worker's own row
    does not always carry it, so read it from the table rather than ever writing a manifest without one."""
    if execution_job.get("created_at"):
        return execution_job
    rows = (
        client.table("execution_jobs")
        .select("created_at")
        .eq("org_id", execution_job["org_id"])
        .eq("id", execution_job["id"])
        .limit(1)
        .execute()
        .data
    )
    return {**execution_job, "created_at": rows[0].get("created_at") if rows else None}


def run_final_test_stage(client: Client, execution_job: dict, state) -> None:
    execution_job = _with_created_at(client, execution_job)
    org_id, execution_job_id = execution_job["org_id"], execution_job["id"]
    params = execution_job["params"]

    result = run_pytest(state.project_dir, _sandbox_params(params))
    final_test_summary = _final_test_summary(result)

    counts = counts_by_job(client, org_id, [execution_job_id])[execution_job_id]

    # §15.3: built from the tree as it is NOW, so the scaffold list never includes a .git.
    manifest = build_manifest(execution_job, state, _load_unit_rows(client, org_id, execution_job_id), final_test_summary)
    # §15.5: after the suite has run (the sandbox never sees a .git), before the zip is built.
    # Never raises: no git, or a failed check, just means a zip without history.
    try_add_git_history(state.project_dir, manifest)

    zip_bytes = _build_zip(state.project_dir)
    output_zip_path = f"{org_id}/{execution_job_id}/project.zip"
    client.storage.from_(OUTPUT_BUCKET).upload(output_zip_path, zip_bytes, {"content-type": "application/zip"})
    client.storage.from_(OUTPUT_BUCKET).upload(
        f"{org_id}/{execution_job_id}/manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
        {"content-type": "application/json"},
    )

    set_execution_output(
        client,
        execution_job_id,
        org_id,
        output_zip_path=output_zip_path,
        counts=counts,
        final_test_summary=final_test_summary,
    )
