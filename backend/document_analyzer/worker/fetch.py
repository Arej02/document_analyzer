"""Worker step: materialize a job's source onto local disk — tasks.md Phase 1
"Worker: unpack zip/tar, or clone GitHub repo (handle private repo + access token)".

Runs on the async worker only, against the service-role Supabase client (bypasses
RLS) — see supabase_client.get_service_role_client. Ingested archives/repos are
untrusted input (CLAUDE.md): extraction is guarded against zip-slip/tar-slip, and
no static parsing/execution of the fetched content happens here.

Also enforces the "max file count" half of Phase 2's "Enforce ingestion resource
bounds" task (specs.md NFR2) — max upload *size* is checked earlier, at request
time, in storage.py, but file count can only be known once an archive is actually
inspected or a repo actually cloned, which happens here. A zip/tar's entry count
is checked before extractall() ever runs, so a zip bomb never gets decompressed to
disk; a GitHub clone can't be size-checked before the fact, so it's checked
immediately after cloning, before the caller can run any Extractor over it.
"""

import base64
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from supabase import Client

from document_analyzer.resource_bounds import MAX_FILE_COUNT

UPLOAD_BUCKET = "agent1-uploads"


class SourceFetchError(Exception):
    """Raised when a job's source can't be materialized (bad archive, clone failure, ...)."""


@dataclass(frozen=True)
class FetchResult:
    repo_path: Path
    source_commit_sha: str | None  # design.md §2 `ingestion_jobs.source_commit_sha` — null for uploads


def fetch_source(client: Client, job: dict, dest_dir: Path) -> FetchResult:
    """Materializes an ingestion_jobs row's source under dest_dir.

    `job` needs `source_type` and `source_ref`; for a private GitHub repo, an
    `access_token` key (resolved from `github_token_ref` — see the next tasks.md
    item, not yet wired) enables authenticated cloning.
    """
    source_type = job["source_type"]
    if source_type == "upload":
        return _fetch_upload(client, job["source_ref"], dest_dir)
    if source_type == "github":
        return _fetch_github(job["source_ref"], dest_dir, access_token=job.get("access_token"))
    raise SourceFetchError(f"unsupported source_type: {source_type!r}")


def _fetch_upload(client: Client, object_path: str, dest_dir: Path) -> FetchResult:
    try:
        archive_bytes = client.storage.from_(UPLOAD_BUCKET).download(object_path)
    except Exception as exc:  # supabase-py raises its own storage exception type
        raise SourceFetchError(f"could not download {object_path!r}: {exc}") from exc

    filename = object_path.rsplit("/", 1)[-1]
    archive_path = dest_dir / filename
    archive_path.write_bytes(archive_bytes)

    extract_dir = dest_dir / "repo"
    extract_dir.mkdir(parents=True, exist_ok=True)
    _extract_archive_safely(archive_path, extract_dir)
    return FetchResult(repo_path=extract_dir, source_commit_sha=None)


def _extract_archive_safely(archive_path: Path, extract_dir: Path) -> None:
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            _assert_entry_count_within_bounds(len(members))
            for member in members:
                _assert_within(extract_dir, member.filename)
            archive.extractall(extract_dir)
        return

    try:
        with tarfile.open(archive_path) as archive:
            members = archive.getmembers()
            _assert_entry_count_within_bounds(len(members))
            for member in members:
                _assert_within(extract_dir, member.name)
            archive.extractall(extract_dir, filter="data")
        return
    except tarfile.ReadError as exc:
        raise SourceFetchError(f"{archive_path.name} is not a valid zip or tar archive") from exc


def _assert_within(base_dir: Path, member_name: str) -> None:
    resolved_base = base_dir.resolve()
    resolved_member = (base_dir / member_name).resolve()
    if resolved_member != resolved_base and resolved_base not in resolved_member.parents:
        raise SourceFetchError(f"archive member escapes extraction directory: {member_name!r}")


def _assert_entry_count_within_bounds(entry_count: int) -> None:
    """Checked before extractall() so a zip/tar bomb is rejected without ever being decompressed to disk.

    Counts every archive entry (files and directories) rather than just files —
    simpler, and errs conservative for a resource-exhaustion guard.
    """
    if entry_count > MAX_FILE_COUNT:
        raise SourceFetchError(f"archive contains {entry_count} entries, exceeding max allowed count of {MAX_FILE_COUNT}")


def _fetch_github(repo_url: str, dest_dir: Path, access_token: str | None = None) -> FetchResult:
    repo_dir = dest_dir / "repo"
    cmd = ["git"]
    if access_token:
        if not repo_url.startswith("https://"):
            raise SourceFetchError("access_token is only supported for https:// GitHub URLs")
        # Passed as a transient `-c` override, not embedded in the URL, so the
        # token never ends up persisted in the clone's .git/config.
        credentials = base64.b64encode(f"x-access-token:{access_token}".encode()).decode()
        cmd += ["-c", f"http.extraHeader=Authorization: Basic {credentials}"]
    cmd += ["clone", "--depth", "1", repo_url, str(repo_dir)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SourceFetchError(_redact(result.stderr, access_token))

    # Unlike an upload's archive entries, a GitHub repo's size can't be known
    # before cloning it — checked immediately after, before any Extractor runs.
    file_count = sum(1 for path in repo_dir.rglob("*") if path.is_file())
    _assert_entry_count_within_bounds(file_count)

    source_commit_sha = _rev_parse_head(repo_dir)

    return FetchResult(repo_path=repo_dir, source_commit_sha=source_commit_sha)


def _rev_parse_head(repo_dir: Path) -> str:
    """Records exactly which commit a `--depth 1` clone landed on (design.md §2
    `source_commit_sha`) — Agent 3 needs this to re-fetch the same source it's
    translating (design_agent3.md §13). A shallow clone's HEAD is still a real,
    resolvable commit, so this is just `git rev-parse HEAD` in the clone's own dir."""
    result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=repo_dir)
    if result.returncode != 0:
        raise SourceFetchError(f"could not resolve cloned commit: {result.stderr}")
    return result.stdout.strip()


def _redact(message: str, access_token: str | None) -> str:
    if access_token and access_token in message:
        return message.replace(access_token, "***")
    return message
