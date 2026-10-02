"""An in-memory GitHub for the delivery tests — design_agent3.md §15, Phase 7 tasks 5-6.

`FakeGitHub(...).transport` is an `httpx.MockTransport` that implements just the endpoints the
delivery uses, computing real git SHAs with `agent3/git_objects.py` (so the client, the fake and
the object builder can be checked against each other). It is a MODEL of GitHub's documented
behaviour, not evidence of it: the places where real behaviour is still unverified (a repo with no
commits refusing Git Data calls, whether the Contents API stores our commit byte-exact, PR
retargeting on merge) are marked below and are what the opt-in live test (Phase 7 task 8) settles.

Fault injection: `fail_next(...)` queues canned responses; `commit_sha_override` / `tree_sha_override`
make the fake report a different SHA than it stored (a rewritten or signed commit).
"""

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import httpx

from document_analyzer.agent3.git_objects import FileRef, Identity, blob_sha, build_tree, commit_sha

TOKEN = "ghp_" + "A" * 36  # shaped like a real token on purpose: redaction must catch it


@dataclass
class FakeRepo:
    full_name: str
    default_branch: str = "main"
    private: bool = True
    archived: bool = False
    can_push: bool = True
    # None = the key is omitted from the repo JSON (a token that cannot read the setting).
    delete_branch_on_merge: bool | None = True
    allow_merge_commit: bool | None = True
    refs: dict[str, str] = field(default_factory=dict)  # "refs/heads/x" -> commit sha
    blobs: dict[str, bytes] = field(default_factory=dict)
    trees: dict[str, dict[str, FileRef]] = field(default_factory=dict)  # tree sha -> flat listing
    commits: dict[str, dict] = field(default_factory=dict)
    pulls: list[dict] = field(default_factory=list)
    truncate_trees: bool = False
    # Behaviours the live scenario (tests/live_github_scenario.py) observes. MODELLED from GitHub's documentation, not observed.
    retarget_on_merge: bool = True  # an open PR whose base branch is deleted after a merge is retargeted at the merged PR's base
    can_admin: bool = True  # whether a token may change repository settings
    ci_enabled: bool = True  # whether a pull request whose branch contains .github/workflows/ci.yml gets a workflow run

    @property
    def empty(self) -> bool:
        return not self.refs

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        stack, seen = [descendant], set()
        while stack:
            sha = stack.pop()
            if sha == ancestor:
                return True
            if sha in seen or sha not in self.commits:
                continue
            seen.add(sha)
            stack.extend(self.commits[sha]["parents"])
        return False


def _epoch(iso: str) -> int:
    return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def _json(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body)


class FakeGitHub:
    def __init__(self, *repos: FakeRepo, token: str = TOKEN, strict_empty_git_data: bool = True):
        self.token = token
        self.repos = {repo.full_name: repo for repo in repos}
        # UNVERIFIED model of real GitHub: Git Data writes (blobs, trees, commits, refs) are refused
        # with a 409 in a repo that has no commits. The Contents API is the way in (design §15.4).
        self.strict_empty_git_data = strict_empty_git_data
        self.requests: list[dict] = []
        self.queue: list[httpx.Response] = []
        self.commit_sha_override: Callable[[str], str] | None = None
        self.tree_sha_override: Callable[[str], str] | None = None
        self.transport = httpx.MockTransport(self._handle)

    # -- test controls --------------------------------------------------- #

    def fail_next(self, count: int, status: int, *, headers: dict | None = None, message: str = "error") -> None:
        for _ in range(count):
            self.queue.append(httpx.Response(status, json={"message": message}, headers=headers or {}))

    def calls(self, method: str | None = None, contains: str = "") -> list[dict]:
        return [r for r in self.requests if (method is None or r["method"] == method) and contains in r["path"]]

    # -- dispatch -------------------------------------------------------- #

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            {
                "method": request.method,
                "host": request.url.host,
                "scheme": request.url.scheme,
                "path": request.url.path,
                "query": dict(request.url.params),
                "json": body,
                "auth": request.headers.get("authorization"),
                "url": str(request.url),
            }
        )
        if self.queue:
            return self.queue.pop(0)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return _json(401, {"message": "Bad credentials"})

        match = re.match(r"^/repos/([^/]+)/([^/]+)(/.*)?$", request.url.path)
        if not match:
            return _json(404, {"message": "Not Found"})
        repo = self.repos.get(f"{match.group(1)}/{match.group(2)}")
        if repo is None:
            return _json(404, {"message": "Not Found"})
        rest = match.group(3) or ""
        return self._route(repo, request, rest, body)

    def _route(self, repo: FakeRepo, request: httpx.Request, rest: str, body) -> httpx.Response:
        method = request.method
        if rest == "" and method == "GET":
            return self._repo_json(repo)
        if rest == "/branches" and method == "GET":
            if repo.empty:
                return _json(404, {"message": "Not Found"})
            names = [ref.removeprefix("refs/heads/") for ref in sorted(repo.refs)]
            return _json(200, [{"name": name} for name in names[: int(request.url.params.get("per_page", 30))]])

        git_write = (method in ("POST", "PATCH")) and rest.startswith("/git/")
        if git_write and repo.empty and self.strict_empty_git_data:
            return _json(409, {"message": "Git Repository is empty."})

        if rest.startswith("/git/ref/") and method == "GET":
            if repo.empty:
                return _json(409, {"message": "Git Repository is empty."})
            ref = "refs/" + rest.removeprefix("/git/ref/")
            return _json(200, {"object": {"sha": repo.refs[ref]}}) if ref in repo.refs else _json(404, {"message": "Not Found"})
        if rest.startswith("/git/commits/") and method == "GET":
            sha = rest.removeprefix("/git/commits/")
            commit = repo.commits.get(sha)
            if not commit:
                return _json(404, {"message": "Not Found"})
            return _json(200, {"sha": sha, "tree": {"sha": commit["tree"]}, "parents": [{"sha": p} for p in commit["parents"]]})
        if rest.startswith("/git/trees/") and method == "GET":
            return self._get_tree(repo, rest.removeprefix("/git/trees/"))
        if rest == "/git/blobs" and method == "POST":
            return self._create_blob(repo, body)
        if rest == "/git/trees" and method == "POST":
            return self._create_tree(repo, body)
        if rest == "/git/commits" and method == "POST":
            return self._create_commit(repo, body)
        if rest == "/git/refs" and method == "POST":
            return self._create_ref(repo, body)
        if rest.startswith("/git/refs/") and method == "PATCH":
            return self._update_ref(repo, "refs/" + rest.removeprefix("/git/refs/"), body)
        if rest.startswith("/contents/") and method == "PUT":
            return self._put_contents(repo, rest.removeprefix("/contents/"), body)
        if rest == "/pulls" and method == "GET":
            return self._list_pulls(repo, request.url.params)
        if rest == "/pulls" and method == "POST":
            return self._create_pull(repo, body)
        return self._route_extras(repo, request, rest, body)

    # -- endpoints ------------------------------------------------------- #

    def _repo_json(self, repo: FakeRepo) -> httpx.Response:
        data = {
            "full_name": repo.full_name,
            "default_branch": repo.default_branch,
            "private": repo.private,
            "archived": repo.archived,
            "permissions": {"push": repo.can_push, "pull": True},
        }
        if repo.delete_branch_on_merge is not None:
            data["delete_branch_on_merge"] = repo.delete_branch_on_merge
        if repo.allow_merge_commit is not None:
            data["allow_merge_commit"] = repo.allow_merge_commit
        return _json(200, data)

    def _get_tree(self, repo: FakeRepo, sha: str) -> httpx.Response:
        flat = repo.trees.get(sha)
        if flat is None:
            return _json(404, {"message": "Not Found"})
        entries = [
            {"path": path, "mode": ref.mode, "type": "commit" if ref.mode == "160000" else "blob", "sha": ref.sha}
            for path, ref in sorted(flat.items())
        ]
        return _json(200, {"sha": sha, "tree": entries, "truncated": repo.truncate_trees})

    def _create_blob(self, repo: FakeRepo, body: dict) -> httpx.Response:
        if body.get("encoding") != "base64":
            return _json(422, {"message": "only base64 is modelled"})
        data = base64.b64decode(body["content"])
        sha = blob_sha(data)
        repo.blobs[sha] = data
        return _json(201, {"sha": sha})

    def _create_tree(self, repo: FakeRepo, body: dict) -> httpx.Response:
        flat: dict[str, FileRef] = {}
        if body.get("base_tree"):
            if body["base_tree"] not in repo.trees:
                return _json(422, {"message": "base_tree does not exist"})
            flat.update(repo.trees[body["base_tree"]])
        for entry in body["tree"]:
            if entry["sha"] not in repo.blobs and entry["mode"] != "160000":
                return _json(422, {"message": f"tree.sha {entry['sha']} does not exist"})
            flat[entry["path"]] = FileRef(entry["mode"], entry["sha"])
        sha = build_tree(flat).root_sha
        repo.trees[sha] = flat
        reported = self.tree_sha_override(sha) if self.tree_sha_override else sha
        return _json(201, {"sha": reported})

    def _store_commit(self, repo: FakeRepo, tree: str, parents: list[str], message: str, author: dict, committer: dict) -> str:
        identity_a = Identity(author["name"], author["email"], _epoch(author["date"]))
        identity_c = Identity(committer["name"], committer["email"], _epoch(committer["date"]))
        sha = commit_sha(tree, parents, identity_a, identity_c, message)
        repo.commits[sha] = {"tree": tree, "parents": list(parents), "message": message}
        return sha

    def _create_commit(self, repo: FakeRepo, body: dict) -> httpx.Response:
        if body["tree"] not in repo.trees:
            return _json(422, {"message": "Tree SHA does not exist"})
        if any(parent not in repo.commits for parent in body["parents"]):
            return _json(422, {"message": "Parent SHA does not exist"})
        sha = self._store_commit(repo, body["tree"], body["parents"], body["message"], body["author"], body["committer"])
        reported = self.commit_sha_override(sha) if self.commit_sha_override else sha
        return _json(201, {"sha": reported})

    def _create_ref(self, repo: FakeRepo, body: dict) -> httpx.Response:
        if body["ref"] in repo.refs:
            return _json(422, {"message": "Reference already exists"})
        if body["sha"] not in repo.commits:
            return _json(422, {"message": "Object does not exist"})
        repo.refs[body["ref"]] = body["sha"]
        return _json(201, {"ref": body["ref"], "object": {"sha": body["sha"]}})

    def _update_ref(self, repo: FakeRepo, ref: str, body: dict) -> httpx.Response:
        if ref not in repo.refs:
            return _json(422, {"message": "Reference does not exist"})
        if body["sha"] not in repo.commits:
            return _json(422, {"message": "Object does not exist"})
        if not body.get("force") and not repo.is_ancestor(repo.refs[ref], body["sha"]):
            return _json(422, {"message": "Update is not a fast forward"})
        repo.refs[ref] = body["sha"]
        return _json(200, {"ref": ref, "object": {"sha": body["sha"]}})

    def _put_contents(self, repo: FakeRepo, path: str, body: dict) -> httpx.Response:
        data = base64.b64decode(body["content"])
        branch_ref = f"refs/heads/{body.get('branch') or repo.default_branch}"
        if repo.empty:
            parents, flat = [], {}
        else:
            if branch_ref not in repo.refs:
                return _json(422, {"message": "Branch not found"})
            tip = repo.refs[branch_ref]
            parents, flat = [tip], dict(repo.trees[repo.commits[tip]["tree"]])
            if path in flat:
                return _json(422, {"message": "Invalid request. \"sha\" wasn't supplied."})
        repo.blobs[blob_sha(data)] = data
        flat[path] = FileRef("100644", blob_sha(data))
        tree_sha = build_tree(flat).root_sha
        repo.trees[tree_sha] = flat
        # MODEL: the commit is stored exactly as sent (our author, committer, date and message).
        # Whether real GitHub does so (no auto-signing, no message change) is UNVERIFIED (§15.14).
        sha = self._store_commit(repo, tree_sha, parents, body["message"], body["author"], body["committer"])
        repo.refs[branch_ref] = sha
        reported = self.commit_sha_override(sha) if self.commit_sha_override else sha
        return _json(201, {"commit": {"sha": reported}, "content": {"path": path}})

    def _list_pulls(self, repo: FakeRepo, params) -> httpx.Response:
        head = params.get("head", "")
        branch = head.split(":", 1)[-1]
        matches = [p for p in repo.pulls if p["head"]["ref"] == branch]
        return _json(200, matches[: int(params.get("per_page", 30))])

    def _create_pull(self, repo: FakeRepo, body: dict) -> httpx.Response:
        for side in ("head", "base"):
            if f"refs/heads/{body[side]}" not in repo.refs:
                return _json(422, {"message": f"{side} branch does not exist"})
        if any(p["head"]["ref"] == body["head"] and p["base"]["ref"] == body["base"] and p["state"] == "open" for p in repo.pulls):
            return _json(422, {"message": "A pull request already exists"})
        number = len(repo.pulls) + 1
        pull = {
            "number": number,
            "state": "open",
            "title": body["title"],
            "body": body["body"],
            "html_url": f"https://github.com/{repo.full_name}/pull/{number}",
            "head": {"ref": body["head"]},
            "base": {"ref": body["base"]},
        }
        repo.pulls.append(pull)
        return _json(201, pull)

    # -- endpoints used by the live scenario ------------------------------ #

    def _route_extras(self, repo: FakeRepo, request: httpx.Request, rest: str, body) -> httpx.Response:
        method = request.method
        pull_match = re.fullmatch(r"/pulls/(\d+)", rest)
        merge_match = re.fullmatch(r"/pulls/(\d+)/merge", rest)
        if pull_match and method == "GET":
            pull = self._pull(repo, int(pull_match.group(1)))
            return _json(200, pull) if pull else _json(404, {"message": "Not Found"})
        if pull_match and method == "PATCH":
            pull = self._pull(repo, int(pull_match.group(1)))
            if not pull:
                return _json(404, {"message": "Not Found"})
            if "state" in body:
                pull["state"] = body["state"]
            return _json(200, pull)
        if merge_match and method == "PUT":
            return self._merge_pull(repo, int(merge_match.group(1)), body)
        if rest.startswith("/git/refs/") and method == "DELETE":
            ref = "refs/" + rest.removeprefix("/git/refs/")
            if ref not in repo.refs:
                return _json(422, {"message": "Reference does not exist"})
            del repo.refs[ref]
            return httpx.Response(204)
        if rest.startswith("/git/matching-refs/") and method == "GET":
            prefix = "refs/" + rest.removeprefix("/git/matching-refs/")
            return _json(200, [{"ref": ref, "object": {"sha": sha}} for ref, sha in sorted(repo.refs.items()) if ref.startswith(prefix)])
        if rest == "" and method == "PATCH":
            if not repo.can_admin:
                return _json(403, {"message": "Must have admin rights to Repository."})
            if "delete_branch_on_merge" in body:
                repo.delete_branch_on_merge = bool(body["delete_branch_on_merge"])
            return self._repo_json(repo)
        if rest.startswith("/commits/") and method == "GET":
            sha = rest.removeprefix("/commits/")
            if sha not in repo.commits:
                return _json(404, {"message": "Not Found"})
            return _json(200, {"sha": sha, "commit": {"verification": {"verified": False, "reason": "unsigned"}}})
        if rest == "/actions/runs" and method == "GET":
            return self._list_runs(repo, request.url.params.get("branch", ""))
        return _json(404, {"message": "Not Found"})

    @staticmethod
    def _pull(repo: FakeRepo, number: int) -> dict | None:
        return next((p for p in repo.pulls if p["number"] == number), None)

    def _merge_pull(self, repo: FakeRepo, number: int, body: dict) -> httpx.Response:
        pull = self._pull(repo, number)
        if not pull:
            return _json(404, {"message": "Not Found"})
        if pull["state"] != "open":
            return _json(405, {"message": "Pull Request is not mergeable"})
        head_ref, base_ref = f"refs/heads/{pull['head']['ref']}", f"refs/heads/{pull['base']['ref']}"
        if head_ref not in repo.refs or base_ref not in repo.refs:
            return _json(422, {"message": "branch missing"})
        # MODEL: the merge leaves the base at the head's content (a merge commit with the same tree).
        repo.refs[base_ref] = repo.refs[head_ref]
        pull["state"], pull["merged"] = "closed", True
        if repo.delete_branch_on_merge:
            del repo.refs[head_ref]
            if repo.retarget_on_merge:
                for other in repo.pulls:
                    if other["state"] == "open" and other["base"]["ref"] == pull["head"]["ref"]:
                        other["base"]["ref"] = pull["base"]["ref"]
        return _json(200, {"merged": True, "sha": repo.refs[base_ref], "message": "Pull Request successfully merged"})

    def _list_runs(self, repo: FakeRepo, branch: str) -> httpx.Response:
        ref = f"refs/heads/{branch}"
        runs = []
        if repo.ci_enabled and ref in repo.refs:
            tree = repo.trees.get(repo.commits[repo.refs[ref]]["tree"], {})
            if ".github/workflows/ci.yml" in tree:
                runs.append(
                    {"id": 1, "status": "completed", "conclusion": "success", "head_branch": branch, "event": "pull_request",
                     "html_url": f"https://github.com/{repo.full_name}/actions/runs/1"}
                )
        return _json(200, {"total_count": len(runs), "workflow_runs": runs})
