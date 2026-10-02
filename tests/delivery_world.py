"""A finished execution to deliver, for the delivery tests (Phase 7 task 6): rows in a FakeClient, a real
`project.zip` (with a `.git/` entry that must be ignored) and `manifest.json` in fake storage, a GitHub
connection, and a queued delivery. The project itself is `test_agent3_commit_plan._project()`: a scaffold,
phase 1 with two units, phase 2 with one failed-and-stubbed unit."""

import io
import json
import zipfile
from dataclasses import dataclass, field

import httpx

from document_analyzer.agent3.delivery.github_api import GitHubClient
from document_analyzer.supabase_client import insert_scoped

from fakes import FakeClient
from fakes_github import TOKEN, FakeGitHub, FakeRepo
from test_agent3_commit_plan import _project

ORG = "org-123"
REPO = "acme/migrated-shop"
TOKEN_REF = "secret-ref-1"


@dataclass
class World:
    client: FakeClient
    github: FakeGitHub
    manifest: dict
    files: dict
    project_id: str
    plan_id: str
    execution_job_id: str
    delivery_id: str
    zip_path: str
    extra: dict = field(default_factory=dict)


def make_zip(files: dict[str, bytes], extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in {**files, **(extra or {})}.items():
            archive.writestr(path, data)
    return buffer.getvalue()


def build_world(*, repo: FakeRepo | None = None, repo_name: str = REPO, manifest=None, files=None, zip_extra=None, with_manifest=True, with_delivery=True, execution_status="done") -> World:
    client = FakeClient(org_id=ORG)
    github = FakeGitHub(repo or FakeRepo(repo_name))
    base_manifest, base_files = _project()
    manifest = manifest if manifest is not None else base_manifest
    files = files if files is not None else base_files

    project = insert_scoped(client, "projects", ORG, {"source_type": "github", "source_ref": "https://x/y"}).data[0]
    job = insert_scoped(client, "ingestion_jobs", ORG, {"project_id": project["id"], "status": "done"}).data[0]
    project["latest_job_id"] = job["id"]
    plan = insert_scoped(
        client, "migration_plans", ORG, {"project_id": project["id"], "source_job_id": job["id"], "status": "approved", "version": 1}
    ).data[0]
    execution = insert_scoped(
        client,
        "execution_jobs",
        ORG,
        {"project_id": project["id"], "plan_id": plan["id"], "source_job_id": job["id"], "status": execution_status, "params": {}},
    ).data[0]
    zip_path = f"{ORG}/{execution['id']}/project.zip"
    execution["output_zip_path"] = zip_path

    bucket = client.storage.from_("agent3-output")
    bucket.upload(zip_path, make_zip(files, {".git/HEAD": b"ref: refs/heads/main\n", ".git/objects/ab/cdef": b"\x00junk"} | (zip_extra or {})))
    if with_manifest:
        bucket.upload(f"{ORG}/{execution['id']}/manifest.json", json.dumps(manifest).encode("utf-8"))

    insert_scoped(
        client,
        "project_github_connections",
        ORG,
        {"project_id": project["id"], "repo_full_name": repo_name, "default_branch": "main", "token_ref": TOKEN_REF, "created_by": "user-1"},
    )
    delivery_id = new_delivery(client, project["id"], execution["id"], repo=repo_name) if with_delivery else ""
    return World(client, github, manifest, files, project["id"], plan["id"], execution["id"], delivery_id, zip_path)


def new_delivery(client: FakeClient, project_id: str, execution_job_id: str, *, repo: str = REPO, status: str = "queued") -> str:
    return insert_scoped(
        client,
        "deliveries",
        ORG,
        {
            "project_id": project_id,
            "execution_job_id": execution_job_id,
            "repo_full_name": repo,
            "default_branch": "main",
            "status": status,
        },
    ).data[0]["id"]


def github_factory(github: FakeGitHub, *, transport: httpx.BaseTransport | None = None):
    return lambda token: GitHubClient(token, transport=transport or github.transport, sleep=lambda seconds: None)


def delivery_row(client: FakeClient, delivery_id: str) -> dict:
    return next(row for row in client.table("deliveries").rows if row["id"] == delivery_id)


class CrashAfter(httpx.BaseTransport):
    """Delegates to the fake GitHub for the first `limit` requests, then fails every request as a network error.
    With `lose_response`, request number `limit + 1` is still APPLIED by the fake but its response is lost, which is
    how a real connection drop leaves things half-done (a commit created, a pull request opened, nobody told)."""

    def __init__(self, github: FakeGitHub, limit: int, *, lose_response: bool = False):
        self._github, self._limit, self._lose = github, limit, lose_response
        self.count = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.count += 1
        if self.count <= self._limit:
            return self._github.transport.handle_request(request)
        if self._lose and self.count == self._limit + 1:
            self._github.transport.handle_request(request)
        raise httpx.ConnectError("connection dropped")


__all__ = ["ORG", "REPO", "TOKEN", "TOKEN_REF", "World", "build_world", "new_delivery", "github_factory", "delivery_row", "CrashAfter", "make_zip"]
