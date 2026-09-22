"""Upload handling for the `POST /api/agent1/jobs` zip/tar path — design.md §2a.

Max upload size is enforced here (resource_bounds.py); max file count can only
be known once the archive is actually inspected, so that half of the Phase 2
"Enforce ingestion resource bounds" task lives in worker/fetch.py instead.
"""

from fastapi import HTTPException, UploadFile
from supabase import Client

from .resource_bounds import MAX_UPLOAD_SIZE_BYTES

BUCKET = "agent1-uploads"
_ALLOWED_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")


def _validate_filename(filename: str | None) -> str:
    if not filename or not filename.lower().endswith(_ALLOWED_SUFFIXES):
        raise HTTPException(status_code=400, detail="file must be a .zip or .tar(.gz) archive")
    return filename


def _reject_if_oversized(size: int) -> None:
    if size > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"upload exceeds max allowed size of {MAX_UPLOAD_SIZE_BYTES} bytes",
        )


async def store_upload(client: Client, org_id: str, job_id: str, file: UploadFile) -> str:
    """Uploads the archive to the org-scoped bucket path and returns that path as source_ref."""
    filename = _validate_filename(file.filename)

    # request.form() (jobs.py) has already fully received/spooled the upload by
    # this point, so `.size` (when Starlette populated it) is an accurate count
    # of bytes already read — checking it costs nothing extra and lets us skip
    # the read() below entirely for an oversized file.
    if file.size is not None:
        _reject_if_oversized(file.size)

    contents = await file.read()
    if file.size is None:
        _reject_if_oversized(len(contents))

    object_path = f"{org_id}/{job_id}/{filename}"
    client.storage.from_(BUCKET).upload(
        object_path,
        contents,
        {"content-type": file.content_type or "application/octet-stream"},
    )
    return object_path
