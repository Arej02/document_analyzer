import tarfile
import io

import pytest

from document_analyzer.worker import snapshot as snapshot_module
from document_analyzer.worker.snapshot import SnapshotError, archive_source_snapshot

from fakes import FakeClient


def test_archive_source_snapshot_uploads_tarball_of_kept_files(tmp_path):
    (tmp_path / "app.php").write_text("<?php echo 1;")
    (tmp_path / "inc").mkdir()
    (tmp_path / "inc" / "helper.php").write_text("<?php echo 2;")

    client = FakeClient()

    object_path = archive_source_snapshot(client, "job-1", "org-1", tmp_path)

    assert object_path == "org-1/job-1/source.tar.gz"
    [(uploaded_path, contents, opts)] = client.storage.from_("agent1-snapshots").uploads
    assert uploaded_path == object_path
    assert opts == {"content-type": "application/gzip"}

    with tarfile.open(fileobj=io.BytesIO(contents), mode="r:gz") as archive:
        names = sorted(archive.getnames())
        assert names == ["app.php", "inc/helper.php"]
        assert archive.extractfile("app.php").read() == b"<?php echo 1;"


def test_archive_source_snapshot_excludes_already_stripped_boilerplate(tmp_path):
    """Boilerplate is unlinked from disk by `_strip_boilerplate` before this runs,
    so there's nothing left for this step to filter itself — confirms the tarball
    only ever reflects whatever repo_path still has on it."""
    (tmp_path / "app.php").write_text("<?php")
    (tmp_path / "vendor").mkdir()
    # Simulate _strip_boilerplate having already removed vendor's contents.

    client = FakeClient()

    object_path = archive_source_snapshot(client, "job-1", "org-1", tmp_path)

    contents = client.storage.from_("agent1-snapshots")._objects[object_path]
    with tarfile.open(fileobj=io.BytesIO(contents), mode="r:gz") as archive:
        assert archive.getnames() == ["app.php"]


def test_archive_source_snapshot_rejects_oversized_tarball(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_module, "MAX_UPLOAD_SIZE_BYTES", 10)
    (tmp_path / "app.php").write_text("<?php echo 'this is definitely more than ten bytes';")

    client = FakeClient()

    with pytest.raises(SnapshotError, match="exceeding max allowed size"):
        archive_source_snapshot(client, "job-1", "org-1", tmp_path)

    assert client.storage.from_("agent1-snapshots").uploads == []


def test_archive_source_snapshot_empty_repo_still_uploads(tmp_path):
    client = FakeClient()

    object_path = archive_source_snapshot(client, "job-1", "org-1", tmp_path)

    assert object_path in client.storage.from_("agent1-snapshots")._objects
