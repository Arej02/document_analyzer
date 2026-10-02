"""The delivery run — design_agent3.md §15.4, §15.8, §15.13, §15.14.

`run_delivery` reads one execution's finished output (`project.zip` plus `manifest.json`), turns it into
the stacked branches and pull requests of §15.4, and records progress on its `deliveries` row. It runs
in a background thread of the API process (never in the executor worker or the sandbox), makes outbound
HTTPS calls to GitHub only, and never executes anything from the zip: it reads bytes and sends them as
blobs.

Guarantees, each with a test:
- Nothing is written to GitHub until the repository has been re-checked (push access, not archived, the
  default branch unchanged, `delete_branch_on_merge` still on).
- Every blob, tree and commit GitHub creates is compared with the SHA computed locally (`git_objects.py`).
  A mismatch aborts the delivery before any branch is created or moved.
- A branch is only created after all its commits verified, and only at the planned tip. A branch that
  already exists at any other commit fails the delivery. Nothing is ever force-pushed or overwritten.
- Resumable: all names derive from the execution id, so a retry reuses existing branches (at the expected
  SHA) and pull requests, and a finished delivery re-run changes nothing on GitHub.
- The token is resolved once, used only by the GitHub client, and redacted from every stored error.
"""

import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable

from ..commit_plan import (
    AUTHOR_EMAIL,
    AUTHOR_NAME,
    BASELINE_MESSAGE,
    BASELINE_TIMESTAMP,
    Base,
    CommitPlan,
    CommitPlanError,
    baseline_commit,
    build_commit_plan,
)
from ..git_objects import FileRef, GitObjectError, blob_sha
from ..project_layout import STUB_README, path_is_safe
from .github_api import GitHubClient, GitHubError, redact
from .pr_text import branch_name, neutralize, phase_body, pr_title, scaffold_body

logger = logging.getLogger(__name__)

OUTPUT_BUCKET = "agent3-output"

_MAX_ZIP_FILES = 20_000
_MAX_ZIP_FILE_BYTES = 16 * 1024 * 1024
_MAX_ZIP_TOTAL_BYTES = 128 * 1024 * 1024

SETTINGS_PATH = "Settings → General → Pull Requests → “Automatically delete head branches”"


class DeliveryError(Exception):
    """A failure whose message is written to be read by the user (stored on the delivery row)."""


# --------------------------------------------------------------------------- #
# database helpers (every write filters on id AND org_id)
# --------------------------------------------------------------------------- #


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _update(service, org_id: str, delivery_id: str, **values: Any) -> None:
    (
        service.table("deliveries")
        .update({**values, "updated_at": _now()})
        .eq("id", delivery_id)
        .eq("org_id", org_id)
        .execute()
    )


def _get_delivery(service, org_id: str, delivery_id: str) -> dict | None:
    rows = service.table("deliveries").select("*").eq("org_id", org_id).eq("id", delivery_id).limit(1).execute().data
    return rows[0] if rows else None


# --------------------------------------------------------------------------- #
# reading the finished output
# --------------------------------------------------------------------------- #


def unzip_project(zip_bytes: bytes) -> dict[str, bytes]:
    """The project's files as `{path: bytes}`, without `.git/`. Every name is re-validated and the sizes are
    bounded, even though the archive is our own output (design §15.3: defence in depth)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise DeliveryError("the stored project archive is not a valid zip file") from exc
    infos = [info for info in archive.infolist() if not info.is_dir()]
    if len(infos) > _MAX_ZIP_FILES:
        raise DeliveryError("the project archive has too many files to deliver")
    if sum(info.file_size for info in infos) > _MAX_ZIP_TOTAL_BYTES:
        raise DeliveryError("the project archive is too large to deliver")
    files: dict[str, bytes] = {}
    for info in infos:
        name = info.filename
        if name.startswith(".git/"):
            continue  # the zip's own history; delivery rebuilds the same objects from the manifest (§15.14)
        if not path_is_safe(name):
            raise DeliveryError(f"the project archive contains an unsafe path: {neutralize(name, 60)!r}")
        if info.file_size > _MAX_ZIP_FILE_BYTES:
            raise DeliveryError(f"the file {neutralize(name, 60)!r} is too large to deliver")
        files[name] = archive.read(info)
    return files


def _load_artifacts(service, org_id: str, output_zip_path: str) -> tuple[dict, dict[str, bytes]]:
    bucket = service.storage.from_(OUTPUT_BUCKET)
    manifest_path = output_zip_path.rsplit("/", 1)[0] + "/manifest.json"
    try:
        manifest_bytes = bucket.download(manifest_path)
    except Exception as exc:  # noqa: BLE001 — a missing object and a storage failure read the same to the user
        raise DeliveryError(
            "this execution has no delivery manifest (it finished before GitHub delivery existed); run code generation again"
        ) from exc
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as exc:
        raise DeliveryError("the delivery manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise DeliveryError("the delivery manifest has an unexpected shape")
    try:
        zip_bytes = bucket.download(output_zip_path)
    except Exception as exc:  # noqa: BLE001
        raise DeliveryError("the project archive for this execution could not be read from storage") from exc
    return manifest, unzip_project(zip_bytes)


# --------------------------------------------------------------------------- #
# GitHub steps
# --------------------------------------------------------------------------- #


def _expect(kind: str, got: str, want: str) -> None:
    if got != want:
        raise DeliveryError(
            f"GitHub created a different {kind} than the one computed locally (GitHub {got}, expected {want}). "
            "Something rewrote the content (for example line-ending conversion or commit signing), so the delivery was stopped "
            "before any branch was created."
        )


def _recheck_repo(client: GitHubClient, delivery: dict) -> None:
    info = client.get_repo(delivery["repo_full_name"])
    if info.archived:
        raise DeliveryError("the repository is archived, so nothing can be pushed to it")
    if not info.can_push:
        raise DeliveryError("the stored token can no longer push to the repository")
    if info.default_branch != delivery["default_branch"]:
        raise DeliveryError(
            f"the repository's default branch is now {neutralize(info.default_branch, 60)!r}, not "
            f"{neutralize(delivery['default_branch'], 60)!r}; reconnect the repository"
        )
    if info.delete_branch_on_merge is not True:
        raise DeliveryError(
            "the repository no longer deletes head branches after a pull request is merged, so a stack of pull requests would not "
            f"reach the default branch. Turn on {SETTINGS_PATH} on GitHub and start the delivery again."
        )


def _resolve_root(client: GitHubClient, delivery: dict, manifest: dict, execution_job_id: str) -> str:
    """The commit the scaffold branch sits on. On a first run, the default branch's tip (creating the
    initial commit when the repository has no history, §15.4). On a resume, the parent recorded by the
    scaffold branch that an earlier run already pushed, so SHAs stay consistent."""
    repo, default = delivery["repo_full_name"], delivery["default_branch"]
    scaffold = branch_name(execution_job_id, "scaffold")
    existing = client.get_ref_sha(repo, f"heads/{scaffold}")
    if existing is not None:
        steps = 2 if not manifest.get("phases") else 1  # a plan with no units keeps the report commit on the scaffold branch
        sha = existing
        for _ in range(steps):
            parents = client.get_commit(repo, sha).parents
            if not parents:
                raise DeliveryError(f"branch {scaffold} does not descend from the repository's history")
            sha = parents[0]
        return sha
    if client.find_pull_request(repo, scaffold) is not None:
        raise DeliveryError(
            "an earlier pull request from this delivery was already merged or closed and its branch is gone, so the stack cannot be "
            "continued safely. Open the remaining pull requests by hand, or run code generation again for a fresh delivery."
        )
    tip = client.get_ref_sha(repo, f"heads/{default}")
    if tip is not None:
        return tip
    if client.list_branch_names(repo, limit=2):
        raise DeliveryError(
            f"the default branch {neutralize(default, 60)!r} does not exist but the repository has other branches; "
            "connect a new or empty repository"
        )
    # Truly empty: the one direct write to the default branch (§15.4). Our fixed identity, date and message let the
    # commit equal the zip's baseline (§15.14); whether GitHub stores it exactly as sent is checked, not assumed.
    return client.create_initial_commit(
        repo,
        path="README.md",
        content=STUB_README.encode("utf-8"),
        message=BASELINE_MESSAGE,
        name=AUTHOR_NAME,
        email=AUTHOR_EMAIL,
        timestamp=BASELINE_TIMESTAMP,
    )


def _push_group(client: GitHubClient, repo: str, group, parent_tree: str, blobs_done: set[str], touch: Callable[[], None]) -> None:
    previous_tree = parent_tree
    for commit in group.commits:
        entries: dict[str, FileRef] = {}
        for path, data in commit.changes.items():
            local = blob_sha(data)
            if local not in blobs_done:
                _expect("blob", client.create_blob(repo, data), local)
                blobs_done.add(local)
            entries[path] = FileRef("100644", local)
        tree = client.create_tree(repo, entries, base_tree=previous_tree)
        _expect("tree", tree, commit.tree_sha)
        sha = client.create_commit(
            repo,
            tree=tree,
            parents=[commit.parent_sha],
            message=commit.message,
            name=AUTHOR_NAME,
            email=AUTHOR_EMAIL,
            timestamp=commit.timestamp,
        )
        _expect("commit", sha, commit.commit_sha)
        previous_tree = tree
        touch()


def _units_for_phase(manifest: dict, phase_order: int) -> list[dict]:
    for phase in manifest.get("phases", []):
        if phase.get("phase_order") == phase_order:
            return [u for u in phase.get("units", []) if isinstance(u, dict)]
    return []


def _deliver(service, client: GitHubClient, delivery: dict, execution_job: dict) -> None:
    org_id, delivery_id, repo = delivery["org_id"], delivery["id"], delivery["repo_full_name"]
    default = delivery["default_branch"]
    execution_job_id = execution_job["id"]

    def touch(**values: Any) -> None:
        _update(service, org_id, delivery_id, heartbeat_at=_now(), **values)

    _recheck_repo(client, delivery)
    manifest, files = _load_artifacts(service, org_id, execution_job["output_zip_path"])

    try:
        root_sha = _resolve_root(client, delivery, manifest, execution_job_id)
        root_commit = client.get_commit(repo, root_sha)
        base = Base(commit_sha=root_sha, entries=client.get_tree(repo, root_commit.tree_sha))
        plan: CommitPlan = build_commit_plan(manifest, files, base=base)
    except (CommitPlanError, GitObjectError) as exc:
        raise DeliveryError(f"the generated project could not be turned into commits: {neutralize(exc, 200)}") from exc
    touch(root_matches_baseline=(root_sha == baseline_commit().commit_sha))

    total_phases = len(manifest.get("phases", []))
    summary = manifest.get("final_test_summary")
    prs: list[dict] = []
    blobs_done: set[str] = set()
    previous_branch, previous_tree = default, root_commit.tree_sha

    for group in plan.groups:
        branch = branch_name(execution_job_id, group.key, group.label)
        tip = group.commits[-1].commit_sha
        existing = client.get_ref_sha(repo, f"heads/{branch}")
        if existing is not None and existing != tip:
            raise DeliveryError(
                f"the branch {branch} already exists on GitHub at a different commit than this delivery would create; "
                "it was not changed. Delete or rename that branch, or run code generation again for a fresh delivery."
            )
        if existing is None and client.find_pull_request(repo, branch) is None:
            _push_group(client, repo, group, previous_tree, blobs_done, touch)
            try:
                client.create_ref(repo, f"refs/heads/{branch}", tip)
            except GitHubError as exc:
                if exc.kind != "validation" or client.get_ref_sha(repo, f"heads/{branch}") != tip:
                    raise

        pull = client.find_pull_request(repo, branch)
        if pull is None:
            if previous_branch != default and client.get_ref_sha(repo, f"heads/{previous_branch}") is None:
                raise DeliveryError(
                    f"the branch {previous_branch}, which this pull request is stacked on, no longer exists (its pull request was "
                    "merged or closed). Open the remaining pull requests by hand."
                )
            if group.kind == "scaffold":
                title = pr_title(execution_job_id, "scaffold", total_phases)
                body = scaffold_body(
                    execution_job_id=execution_job_id,
                    repo_full_name=repo,
                    default_branch=default,
                    total_phases=total_phases,
                    final_test_summary=summary,
                )
            else:
                title = pr_title(execution_job_id, group.key, total_phases)
                body = phase_body(
                    execution_job_id=execution_job_id,
                    phase_order=group.phase_order,
                    phase_label=group.label,
                    total_phases=total_phases,
                    base_branch=previous_branch,
                    default_branch=default,
                    units=_units_for_phase(manifest, group.phase_order),
                    final_test_summary=summary,
                )
            pull = client.create_pull_request(repo, head=branch, base=previous_branch, title=title, body=body)

        prs.append(
            {
                "kind": group.kind,
                "phase_order": group.phase_order,
                "label": neutralize(group.label) if group.label else None,
                "branch": branch,
                "pr_number": pull.number,
                "url": pull.url,
            }
        )
        touch(prs=prs)
        previous_branch, previous_tree = branch, group.commits[-1].tree_sha


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def _describe_github_error(exc: GitHubError) -> str:
    if exc.kind == "rate_limit":
        return "GitHub's rate limit was reached. Start the delivery again later; the steps already completed are reused."
    if exc.kind == "auth":
        return "GitHub rejected the stored token. Reconnect the repository with a new token and start the delivery again."
    if exc.kind in ("permission", "not_found"):
        return "GitHub refused access to the repository or one of the objects needed. Check the token's repository access."
    if exc.kind == "redirect":
        return "GitHub redirected the request (the repository may have been renamed). Reconnect it under its current name."
    return f"GitHub error: {exc}"


def run_delivery(
    service,
    delivery_id: str,
    org_id: str,
    *,
    github_factory: Callable[[str], GitHubClient],
    resolve_token: Callable[[str], str | None],
) -> None:
    """Runs one delivery to completion. Never raises: every outcome is written to the row."""
    token: str | None = None
    try:
        delivery = _get_delivery(service, org_id, delivery_id)
        if delivery is None or delivery["status"] in ("done", "failed"):
            return
        _update(service, org_id, delivery_id, status="running", heartbeat_at=_now())

        connection = (
            service.table("project_github_connections")
            .select("repo_full_name, token_ref")
            .eq("org_id", org_id)
            .eq("project_id", delivery["project_id"])
            .limit(1)
            .execute()
            .data
        )
        if not connection:
            raise DeliveryError("the project's GitHub connection was removed; connect the repository again")
        if connection[0]["repo_full_name"] != delivery["repo_full_name"]:
            raise DeliveryError("the project's GitHub connection was changed after this delivery started; start it again")
        token = resolve_token(connection[0]["token_ref"])
        if not token:
            raise DeliveryError("the stored GitHub token could not be read; reconnect the repository")

        jobs = (
            service.table("execution_jobs")
            .select("id, output_zip_path")
            .eq("org_id", org_id)
            .eq("id", delivery["execution_job_id"])
            .limit(1)
            .execute()
            .data
        )
        if not jobs or not jobs[0].get("output_zip_path"):
            raise DeliveryError("this execution has no output to deliver")

        client = github_factory(token)
        try:
            _deliver(service, client, delivery, jobs[0])
        finally:
            client.close()
        _update(service, org_id, delivery_id, status="done", error=None, heartbeat_at=_now())
    except DeliveryError as exc:
        _fail(service, org_id, delivery_id, redact(str(exc), token))
    except GitHubError as exc:
        _fail(service, org_id, delivery_id, redact(_describe_github_error(exc), token))
    except Exception:  # noqa: BLE001 — a background task must not die silently; the row says what to do
        logger.exception("delivery %s failed unexpectedly", delivery_id)
        _fail(service, org_id, delivery_id, "the delivery stopped because of an unexpected error; see the server log, then start it again")


def _fail(service, org_id: str, delivery_id: str, message: str) -> None:
    try:
        _update(service, org_id, delivery_id, status="failed", error=message[:1000])
    except Exception:  # noqa: BLE001
        logger.exception("could not record the failure of delivery %s", delivery_id)
