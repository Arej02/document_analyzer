import io
import tarfile
import zipfile

import pytest

from document_analyzer.worker import fetch as fetch_module
from document_analyzer.worker.fetch import SourceFetchError, fetch_source

from fakes import FakeClient


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, contents in entries.items():
            archive.writestr(name, contents)
    return buf.getvalue()


def _tar_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, contents in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
    return buf.getvalue()


def test_fetch_upload_extracts_zip(tmp_path):
    client = FakeClient()
    client.storage.from_("agent1-uploads").upload(
        "org-1/job-1/repo.zip", _zip_bytes({"main.php": b"<?php echo 1;"}), None
    )

    repo_path = fetch_source(client, {"source_type": "upload", "source_ref": "org-1/job-1/repo.zip"}, tmp_path)

    assert (repo_path / "main.php").read_bytes() == b"<?php echo 1;"


def test_fetch_upload_extracts_tar_gz(tmp_path):
    client = FakeClient()
    client.storage.from_("agent1-uploads").upload(
        "org-1/job-1/repo.tar.gz", _tar_bytes({"main.php": b"<?php echo 1;"}), None
    )

    repo_path = fetch_source(
        client, {"source_type": "upload", "source_ref": "org-1/job-1/repo.tar.gz"}, tmp_path
    )

    assert (repo_path / "main.php").read_bytes() == b"<?php echo 1;"


def test_fetch_upload_rejects_zip_slip(tmp_path):
    client = FakeClient()
    client.storage.from_("agent1-uploads").upload(
        "org-1/job-1/evil.zip", _zip_bytes({"../../evil.txt": b"pwned"}), None
    )

    with pytest.raises(SourceFetchError, match="escapes"):
        fetch_source(client, {"source_type": "upload", "source_ref": "org-1/job-1/evil.zip"}, tmp_path)


def test_fetch_upload_rejects_tar_slip(tmp_path):
    client = FakeClient()
    client.storage.from_("agent1-uploads").upload(
        "org-1/job-1/evil.tar.gz", _tar_bytes({"../../evil.txt": b"pwned"}), None
    )

    with pytest.raises(SourceFetchError, match="escapes"):
        fetch_source(client, {"source_type": "upload", "source_ref": "org-1/job-1/evil.tar.gz"}, tmp_path)


def test_fetch_upload_rejects_non_archive(tmp_path):
    client = FakeClient()
    client.storage.from_("agent1-uploads").upload("org-1/job-1/notes.txt", b"just text", None)

    with pytest.raises(SourceFetchError, match="not a valid zip or tar"):
        fetch_source(client, {"source_type": "upload", "source_ref": "org-1/job-1/notes.txt"}, tmp_path)


def test_fetch_upload_missing_object_raises(tmp_path):
    client = FakeClient()

    with pytest.raises(SourceFetchError, match="could not download"):
        fetch_source(client, {"source_type": "upload", "source_ref": "org-1/job-1/missing.zip"}, tmp_path)


def test_fetch_github_public_repo_clones(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, capture_output, text):
        calls.append(cmd)
        (tmp_path / "repo").mkdir()
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(fetch_module.subprocess, "run", fake_run)

    repo_path = fetch_source(
        None, {"source_type": "github", "source_ref": "https://github.com/org/repo"}, tmp_path
    )

    assert repo_path == tmp_path / "repo"
    assert calls[0] == ["git", "clone", "--depth", "1", "https://github.com/org/repo", str(tmp_path / "repo")]


def test_fetch_github_private_repo_uses_header_not_url(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, capture_output, text):
        calls.append(cmd)
        (tmp_path / "repo").mkdir()
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(fetch_module.subprocess, "run", fake_run)

    fetch_source(
        None,
        {"source_type": "github", "source_ref": "https://github.com/org/repo", "access_token": "sekret"},
        tmp_path,
    )

    cmd = calls[0]
    assert "sekret" not in " ".join(cmd)  # never embedded in the URL
    assert any("extraHeader" in part for part in cmd)
    assert "https://github.com/org/repo" in cmd  # URL itself stays clean


def test_fetch_github_clone_failure_redacts_token(tmp_path, monkeypatch):
    def fake_run(cmd, capture_output, text):
        return type("Result", (), {"returncode": 128, "stderr": "fatal: auth failed for token sekret"})()

    monkeypatch.setattr(fetch_module.subprocess, "run", fake_run)

    with pytest.raises(SourceFetchError) as exc_info:
        fetch_source(
            None,
            {"source_type": "github", "source_ref": "https://github.com/org/repo", "access_token": "sekret"},
            tmp_path,
        )

    assert "sekret" not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_fetch_github_access_token_requires_https(tmp_path):
    with pytest.raises(SourceFetchError, match="https://"):
        fetch_source(
            None,
            {"source_type": "github", "source_ref": "git@github.com:org/repo.git", "access_token": "sekret"},
            tmp_path,
        )


def test_fetch_source_rejects_unsupported_type(tmp_path):
    with pytest.raises(SourceFetchError, match="unsupported source_type"):
        fetch_source(None, {"source_type": "ftp", "source_ref": "whatever"}, tmp_path)


def test_fetch_upload_rejects_zip_with_too_many_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_module, "MAX_FILE_COUNT", 2)
    client = FakeClient()
    client.storage.from_("agent1-uploads").upload(
        "org-1/job-1/bomb.zip",
        _zip_bytes({"a.php": b"1", "b.php": b"2", "c.php": b"3"}),
        None,
    )

    with pytest.raises(SourceFetchError, match="exceeding max allowed count"):
        fetch_source(client, {"source_type": "upload", "source_ref": "org-1/job-1/bomb.zip"}, tmp_path)


def test_fetch_upload_rejects_zip_over_limit_without_extracting(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_module, "MAX_FILE_COUNT", 2)
    client = FakeClient()
    client.storage.from_("agent1-uploads").upload(
        "org-1/job-1/bomb.zip",
        _zip_bytes({"a.php": b"1", "b.php": b"2", "c.php": b"3"}),
        None,
    )

    with pytest.raises(SourceFetchError):
        fetch_source(client, {"source_type": "upload", "source_ref": "org-1/job-1/bomb.zip"}, tmp_path)

    assert not (tmp_path / "repo" / "a.php").exists()


def test_fetch_upload_rejects_tar_with_too_many_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_module, "MAX_FILE_COUNT", 2)
    client = FakeClient()
    client.storage.from_("agent1-uploads").upload(
        "org-1/job-1/bomb.tar.gz",
        _tar_bytes({"a.php": b"1", "b.php": b"2", "c.php": b"3"}),
        None,
    )

    with pytest.raises(SourceFetchError, match="exceeding max allowed count"):
        fetch_source(client, {"source_type": "upload", "source_ref": "org-1/job-1/bomb.tar.gz"}, tmp_path)


def test_fetch_upload_within_limit_still_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_module, "MAX_FILE_COUNT", 2)
    client = FakeClient()
    client.storage.from_("agent1-uploads").upload(
        "org-1/job-1/ok.zip", _zip_bytes({"main.php": b"<?php"}), None
    )

    repo_path = fetch_source(client, {"source_type": "upload", "source_ref": "org-1/job-1/ok.zip"}, tmp_path)

    assert (repo_path / "main.php").exists()


def test_fetch_github_rejects_repo_with_too_many_files(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_module, "MAX_FILE_COUNT", 2)

    def fake_run(cmd, capture_output, text):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        for name in ("a.php", "b.php", "c.php"):
            (repo_dir / name).write_text("<?php")
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(fetch_module.subprocess, "run", fake_run)

    with pytest.raises(SourceFetchError, match="exceeding max allowed count"):
        fetch_source(None, {"source_type": "github", "source_ref": "https://github.com/org/repo"}, tmp_path)
