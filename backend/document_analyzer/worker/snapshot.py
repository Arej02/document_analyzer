"""Worker step: archive a job's source snapshot — tasks.md "Agent 3 prerequisites"
(design_agent3.md §13). Agent 1 currently throws the fetched/parsed source away
once extraction finishes; Agent 3 needs the actual source text to translate,
not just the structural facts extraction leaves behind.

Runs on the async worker only, via the service-role client (bypasses RLS) —
same as every other worker/*.py step. Called right after `_strip_boilerplate`
in worker/run.py, so the archived tarball never contains vendor/node_modules/
wp-includes/.git (the same files the boilerplate filter already decided don't
belong to the repo's own code) — it just tars up whatever's already left on
disk under `repo_path` at that point.
"""

import io
import tarfile
from pathlib import Path

from supabase import Client

from document_analyzer.resource_bounds import MAX_UPLOAD_SIZE_BYTES

SNAPSHOT_BUCKET = "agent1-snapshots"


class SnapshotError(Exception):
    """Raised when a job's source snapshot can't be archived (oversized, upload failure)."""


def archive_source_snapshot(client: Client, job_id: str, org_id: str, repo_path: Path) -> str:
    """Tars up whatever's left under `repo_path` (post-boilerplate-strip) and uploads
    it to the org-scoped `agent1-snapshots` bucket. Returns the object path to persist
    onto `ingestion_jobs.source_snapshot_path`.

    Bounded by the same `resource_bounds.py::MAX_UPLOAD_SIZE_BYTES` the upload path
    enforces (design.md §2a) — file count is already bounded by `fetch.py`'s own
    check, since these files are a subset of what was fetched.
    """
    tar_bytes = _build_tarball(repo_path)
    if len(tar_bytes) > MAX_UPLOAD_SIZE_BYTES:
        raise SnapshotError(
            f"source snapshot is {len(tar_bytes)} bytes, exceeding max allowed size of {MAX_UPLOAD_SIZE_BYTES} bytes"
        )

    object_path = f"{org_id}/{job_id}/source.tar.gz"
    client.storage.from_(SNAPSHOT_BUCKET).upload(
        object_path, tar_bytes, {"content-type": "application/gzip"}
    )
    return object_path


def _build_tarball(repo_path: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for file_path in sorted(p for p in repo_path.rglob("*") if p.is_file()):
            archive.add(file_path, arcname=str(file_path.relative_to(repo_path)))
    return buf.getvalue()
