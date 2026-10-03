"""A thin GitHub REST client for PR delivery — design_agent3.md §15.7, §15.8.

Deliberately small and defensive:
- The base URL is fixed (`https://api.github.com`, GitHub Enterprise is out of scope) and the
  repo name is validated before it is ever put in a URL path, so a hostile name cannot
  traverse to another endpoint or host.
- Redirects are never followed (a renamed repo answers 301 to another path; we refuse rather than
  re-send an Authorization header anywhere we did not choose).
- The token only ever travels in the `Authorization` header. Every error message is passed
  through `redact()`, which strips the token and any GitHub token shape.
- Rate limits (`Retry-After`, `x-ratelimit-*`, secondary-limit messages) are retried a bounded
  number of times with a capped sleep; a longer wait fails the call (the delivery resumes later,
  §15.8). Only GETs are retried on a 5xx: a POST that may have been processed is never re-sent.
- Blobs are created with `POST /git/blobs` (base64, byte-exact), not inline tree `content`,
  because an empty file's inline content is unverified and base64 cannot be mangled.

Synchronous on purpose: the delivery runs in a FastAPI background task thread. No DB access.
Whether GitHub behaves exactly as this client assumes is verified by the live test (Phase 7
task 8); until then `tests/fakes_github.py` is a model of the documented behaviour, not proof.
"""

import base64
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping
from urllib.parse import quote

import httpx

from ..git_objects import FileRef

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"

MAX_RETRIES = 3
MAX_WAIT_SECONDS = 60.0
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
_REQUEST_TIMEOUT = httpx.Timeout(30.0)

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
# Classic (ghp_), fine-grained (github_pat_), OAuth (gho_), user-to-server (ghu_), server (ghs_), refresh (ghr_).
_TOKEN_SHAPE = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")


class GitHubError(Exception):
    """A failed GitHub call. `kind` is one of: auth, permission, not_found, conflict,
    validation, rate_limit, server, redirect, too_large, invalid_input, other."""

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass(frozen=True)
class RepoInfo:
    full_name: str
    default_branch: str
    private: bool
    archived: bool
    can_push: bool
    # None when the token cannot read the setting (design §15.13 treats that as "unverified").
    delete_branch_on_merge: bool | None
    allow_merge_commit: bool | None


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    tree_sha: str
    parents: tuple[str, ...]


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    head: str
    base: str


def validate_repo_full_name(full_name: str) -> str:
    """`owner/name` in GitHub's character set, with no `.` / `..` segment. Raises GitHubError
    (kind invalid_input) so callers can turn it into a 422 before anything is stored."""
    if not isinstance(full_name, str) or not _REPO_PATTERN.match(full_name):
        raise GitHubError("invalid_input", "repository must look like owner/name")
    if any(part in (".", "..") for part in full_name.split("/")):
        raise GitHubError("invalid_input", "repository must look like owner/name")
    return full_name


def redact(text: str, token: str | None = None) -> str:
    if token:
        text = text.replace(token, "[redacted]")
    return _TOKEN_SHAPE.sub("[redacted]", text)


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        base_url: str = API_BASE,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        if not token:
            raise GitHubError("auth", "no GitHub token")
        self._token = token
        self._sleep = sleep
        self._clock = clock
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=_REQUEST_TIMEOUT,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "galvanise-agent3-delivery",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Transport: retries, error mapping, redaction
    # ------------------------------------------------------------------ #

    def _message(self, response: httpx.Response) -> str:
        try:
            body = response.json()
            text = body.get("message") if isinstance(body, dict) else None
        except ValueError:
            text = None
        return redact(str(text or response.reason_phrase or "error")[:300], self._token)

    def _rate_limit_wait(self, response: httpx.Response) -> float | None:
        if response.status_code not in (403, 429):
            return None
        headers = response.headers
        if headers.get("retry-after", "").isdigit():
            return float(headers["retry-after"])
        if headers.get("x-ratelimit-remaining") == "0" and headers.get("x-ratelimit-reset", "").isdigit():
            return max(float(headers["x-ratelimit-reset"]) - self._clock(), 0.0) + 1.0
        lowered = self._message(response).lower()
        if "rate limit" in lowered or "abuse" in lowered or "secondary" in lowered:
            return 30.0
        return None

    def _request(self, method: str, path: str, *, json: object | None = None, params: dict | None = None) -> httpx.Response:
        retries_left = MAX_RETRIES
        while True:
            try:
                response = self._http.request(method, path, json=json, params=params)
            except httpx.HTTPError as exc:
                # Never include str(exc): it can carry the request URL.
                raise GitHubError("other", f"GitHub request failed ({type(exc).__name__})") from None
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise GitHubError("too_large", "GitHub response is too large", response.status_code)

            wait = self._rate_limit_wait(response)
            if wait is not None:
                if wait > MAX_WAIT_SECONDS or retries_left == 0:
                    raise GitHubError("rate_limit", f"GitHub rate limit reached (retry in about {int(wait)}s)", response.status_code)
                retries_left -= 1
                self._sleep(wait)
                continue
            if response.status_code in (500, 502, 503, 504) and method == "GET" and retries_left > 0:
                retries_left -= 1
                self._sleep(min(2.0 ** (MAX_RETRIES - retries_left), MAX_WAIT_SECONDS))
                continue
            return response

    def _json(self, response: httpx.Response, *, ok: tuple[int, ...] = (200, 201)) -> object:
        status = response.status_code
        if status in ok:
            try:
                return response.json()
            except ValueError:
                raise GitHubError("other", "GitHub returned a response that is not JSON", status) from None
        message = self._message(response)
        if 300 <= status < 400:
            raise GitHubError("redirect", "GitHub redirected the request (the repository may have been renamed or moved); not followed", status)
        kind = {401: "auth", 403: "permission", 404: "not_found", 409: "conflict", 422: "validation"}.get(status)
        if kind is None:
            kind = "server" if status >= 500 else "other"
        raise GitHubError(kind, f"GitHub {status}: {message}", status)

    @staticmethod
    def _repo_path(full_name: str) -> str:
        validate_repo_full_name(full_name)
        owner, name = full_name.split("/")
        return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"

    # ------------------------------------------------------------------ #
    # Repository facts (connect-time validation, §15.7, §15.13)
    # ------------------------------------------------------------------ #

    def get_repo(self, full_name: str) -> RepoInfo:
        data = self._json(self._request("GET", self._repo_path(full_name)), ok=(200,))
        permissions = data.get("permissions") or {}

        def optional_bool(key: str) -> bool | None:
            value = data.get(key)
            return value if isinstance(value, bool) else None

        return RepoInfo(
            full_name=str(data.get("full_name") or full_name),
            default_branch=str(data.get("default_branch") or "main"),
            private=bool(data.get("private")),
            archived=bool(data.get("archived")),
            can_push=bool(permissions.get("push")),
            delete_branch_on_merge=optional_bool("delete_branch_on_merge"),
            allow_merge_commit=optional_bool("allow_merge_commit"),
        )

    def check_pull_request_access(self, full_name: str) -> None:
        """Reads at most one pull request, which needs the Pull requests permission. Raises GitHubError (kind `permission` or
        `not_found`) when the token has not been given it. Opening a pull request needs WRITE access, which cannot be probed
        without opening one, so a missing write permission still shows up at the first pull request."""
        self._json(self._request("GET", f"{self._repo_path(full_name)}/pulls", params={"state": "all", "per_page": 1}), ok=(200,))

    def list_branch_names(self, full_name: str, *, limit: int = 3) -> list[str]:
        """At most `limit` branch names; an empty repo (GitHub answers 404 or 409 for it) is []."""
        response = self._request("GET", f"{self._repo_path(full_name)}/branches", params={"per_page": limit})
        if response.status_code in (404, 409):
            return []
        data = self._json(response, ok=(200,))
        return [str(item["name"]) for item in data][:limit]

    # ------------------------------------------------------------------ #
    # Git data
    # ------------------------------------------------------------------ #

    def get_ref_sha(self, full_name: str, ref: str) -> str | None:
        """`ref` like `heads/main`. None when it does not exist (404, or 409 for an empty repo)."""
        response = self._request("GET", f"{self._repo_path(full_name)}/git/ref/{quote(ref, safe='/')}")
        if response.status_code in (404, 409):
            return None
        return str(self._json(response, ok=(200,))["object"]["sha"])

    def get_commit(self, full_name: str, sha: str) -> CommitInfo:
        data = self._json(self._request("GET", f"{self._repo_path(full_name)}/git/commits/{quote(sha, safe='')}"), ok=(200,))
        return CommitInfo(
            sha=str(data["sha"]),
            tree_sha=str(data["tree"]["sha"]),
            parents=tuple(str(parent["sha"]) for parent in data.get("parents", [])),
        )

    def get_tree(self, full_name: str, tree_sha: str) -> dict[str, FileRef]:
        """The tree's files as `{path: FileRef}` (like `git ls-tree -r`). A truncated listing is an
        error: building on a partial base tree would silently drop the repo's own files."""
        data = self._json(
            self._request("GET", f"{self._repo_path(full_name)}/git/trees/{quote(tree_sha, safe='')}", params={"recursive": "1"}),
            ok=(200,),
        )
        if data.get("truncated"):
            raise GitHubError("too_large", "the target repository's tree is too large to read in one call")
        return {str(item["path"]): FileRef(str(item["mode"]), str(item["sha"])) for item in data.get("tree", []) if item.get("type") != "tree"}

    def create_blob(self, full_name: str, data: bytes) -> str:
        body = {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"}
        return str(self._json(self._request("POST", f"{self._repo_path(full_name)}/git/blobs", json=body))["sha"])

    def create_tree(self, full_name: str, entries: Mapping[str, FileRef], *, base_tree: str | None) -> str:
        """`entries` are the files this tree adds or replaces (blobs already created); with
        `base_tree` the rest of the base is carried over by GitHub."""
        body: dict = {
            "tree": [{"path": path, "mode": ref.mode, "type": "commit" if ref.mode == "160000" else "blob", "sha": ref.sha} for path, ref in sorted(entries.items())]
        }
        if base_tree:
            body["base_tree"] = base_tree
        return str(self._json(self._request("POST", f"{self._repo_path(full_name)}/git/trees", json=body))["sha"])

    def create_commit(
        self, full_name: str, *, tree: str, parents: list[str], message: str, name: str, email: str, timestamp: int
    ) -> str:
        identity = {"name": name, "email": email, "date": _iso(timestamp)}
        body = {"message": message, "tree": tree, "parents": parents, "author": identity, "committer": identity}
        return str(self._json(self._request("POST", f"{self._repo_path(full_name)}/git/commits", json=body))["sha"])

    def create_ref(self, full_name: str, ref: str, sha: str) -> None:
        """`ref` like `refs/heads/migrate/…`."""
        self._json(self._request("POST", f"{self._repo_path(full_name)}/git/refs", json={"ref": ref, "sha": sha}))

    def update_ref(self, full_name: str, ref: str, sha: str, *, force: bool = False) -> None:
        """Fast-forward by default. The delivery never passes `force=True` (design §15.8)."""
        # PATCH /git/refs/{ref} takes `heads/name`, not `refs/heads/name`.
        path = f"{self._repo_path(full_name)}/git/refs/{quote(ref.removeprefix('refs/'), safe='/')}"
        self._json(self._request("PATCH", path, json={"sha": sha, "force": force}), ok=(200,))

    def create_initial_commit(
        self, full_name: str, *, path: str, content: bytes, message: str, name: str, email: str, timestamp: int, branch: str | None = None
    ) -> str:
        """The first commit of a repo with no history, via the Contents API (design §15.4): author,
        committer and date are ours. Returns the new commit's SHA.

        `branch` is normally omitted: GitHub documents the repository's default branch as the default, and in a
        repository with no commits the branch named here does not exist yet, which GitHub may refuse. The branch that
        results is the default branch, which the delivery has already checked against the frozen connection."""
        identity = {"name": name, "email": email, "date": _iso(timestamp)}
        body = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "author": identity,
            "committer": identity,
        }
        if branch:
            body["branch"] = branch
        response = self._request("PUT", f"{self._repo_path(full_name)}/contents/{quote(path, safe='/')}", json=body)
        return str(self._json(response)["commit"]["sha"])

    # ------------------------------------------------------------------ #
    # Pull requests
    # ------------------------------------------------------------------ #

    @staticmethod
    def _pull(data: dict) -> PullRequest:
        return PullRequest(
            number=int(data["number"]),
            url=str(data["html_url"]),
            head=str(data["head"]["ref"]),
            base=str(data["base"]["ref"]),
        )

    def find_pull_request(self, full_name: str, head_branch: str) -> PullRequest | None:
        owner = full_name.split("/")[0]
        data = self._json(
            self._request(
                "GET",
                f"{self._repo_path(full_name)}/pulls",
                params={"head": f"{owner}:{head_branch}", "state": "all", "per_page": 1},
            ),
            ok=(200,),
        )
        return self._pull(data[0]) if data else None

    def create_pull_request(self, full_name: str, *, head: str, base: str, title: str, body: str) -> PullRequest:
        data = self._json(
            self._request(
                "POST",
                f"{self._repo_path(full_name)}/pulls",
                json={"title": title, "head": head, "base": base, "body": body, "maintainer_can_modify": False},
            )
        )
        return self._pull(data)
