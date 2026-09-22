import io

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from document_analyzer import storage as storage_module
from document_analyzer.storage import store_upload

from fakes import FakeClient


def _upload_file(contents: bytes, filename: str = "repo.zip", declared_size: int | None = None) -> UploadFile:
    return UploadFile(io.BytesIO(contents), size=declared_size if declared_size is not None else len(contents), filename=filename)


async def _store(client, org_id, job_id, upload):
    return await store_upload(client, org_id, job_id, upload)


async def test_store_upload_within_limit_succeeds():
    client = FakeClient()
    upload = _upload_file(b"small archive")

    path = await _store(client, "org-1", "job-1", upload)

    assert path == "org-1/job-1/repo.zip"
    assert client.storage.from_("agent1-uploads")._objects[path] == b"small archive"


async def test_store_upload_rejects_when_declared_size_exceeds_limit(monkeypatch):
    monkeypatch.setattr(storage_module, "MAX_UPLOAD_SIZE_BYTES", 10)
    client = FakeClient()
    upload = _upload_file(b"0123456789ABCDEF", declared_size=16)

    with pytest.raises(HTTPException) as exc_info:
        await _store(client, "org-1", "job-1", upload)

    assert exc_info.value.status_code == 413
    assert client.storage.from_("agent1-uploads")._objects == {}


async def test_store_upload_rejects_when_actual_size_exceeds_limit_and_size_unknown(monkeypatch):
    monkeypatch.setattr(storage_module, "MAX_UPLOAD_SIZE_BYTES", 10)
    client = FakeClient()
    upload = _upload_file(b"0123456789ABCDEF")
    upload.size = None  # simulate Starlette not having tracked a size

    with pytest.raises(HTTPException) as exc_info:
        await _store(client, "org-1", "job-1", upload)

    assert exc_info.value.status_code == 413


async def test_store_upload_at_exact_limit_is_allowed(monkeypatch):
    monkeypatch.setattr(storage_module, "MAX_UPLOAD_SIZE_BYTES", 10)
    client = FakeClient()
    upload = _upload_file(b"0123456789")  # exactly 10 bytes

    path = await _store(client, "org-1", "job-1", upload)

    assert path == "org-1/job-1/repo.zip"
