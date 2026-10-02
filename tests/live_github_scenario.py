"""The GitHub delivery scenario — Phase 7 task 8, design_agent3.md §15.4, §15.13, §15.14, §15.15.

One function, `run_scenario`, exercises everything the design could only MODEL about GitHub, and records what it
actually observed. It runs two ways:

- `test_agent3_github_live.py` runs it against real `api.github.com`, opt-in, on a throwaway empty repository.
  That is the only run that settles the open questions.
- `test_agent3_live_scenario_offline.py` runs the same code against `fakes_github.FakeGitHub`, so the script's own
  plumbing is debugged before a real repository is spent on it. That run proves the script, never GitHub.

It returns `Observations`: `facts` (what was seen, including things the design does NOT depend on, like whether the
initial commit came back signed) and `failures` (design claims that did not hold). Nothing here raises on a failed
claim, so one run reports everything; the caller prints the report, then asserts there are no failures.

What it writes to the repository: an initial commit, three delivery branches and pull requests, one merge of the first
pull request, and (if the token may change settings) a temporary flip of "Automatically delete head branches", restored
afterwards. It never deletes the repository and never touches a repository that is not empty.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from document_analyzer.agent3.commit_plan import baseline_commit, tree_sha_of_files
from document_analyzer.agent3.delivery.deliver import run_delivery
from document_analyzer.agent3.delivery.github_api import GitHubClient, GitHubError
from document_analyzer.agent3.project_layout import CI_WORKFLOW_PATH
from fastapi import HTTPException

from delivery_world import ORG, TOKEN_REF, build_world, delivery_row
from test_agent3_commit_plan import _project

REAL_CI_WORKFLOW = Path(__file__).resolve().parent.parent / "backend" / "document_analyzer" / "agent3" / "template" / ".github" / "workflows" / "ci.yml"


@dataclass
class Observations:
    facts: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)

    def note(self, key: str, value: Any) -> None:
        self.facts[key] = value

    def expect(self, condition: bool, claim: str) -> bool:
        if not condition:
            self.failures.append(claim)
        return condition


def format_report(obs: Observations, token: str | None = None) -> str:
    lines = ["GitHub delivery scenario report", "=" * 31, "", "Observed:"]
    lines += [f"  {key}: {value}" for key, value in obs.facts.items()]
    lines += ["", "Steps run: " + (", ".join(obs.steps) or "none")]
    lines += ["", "Design claims that did NOT hold:" if obs.failures else "Every design claim held."]
    lines += [f"  - {failure}" for failure in obs.failures]
    text = "\n".join(lines)
    return text.replace(token, "[redacted]") if token else text


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {}


def run_scenario(
    *,
    http: httpx.Client,
    github_factory: Callable[[str], GitHubClient],
    token: str,
    repo: str,
    validate_connection: Callable[[str, str], Any],
    check_ready: Callable[[str, str], Any],
    ci_wait_seconds: float = 0,
    settle_seconds: float = 0,
    toggle_setting: bool = True,
    cleanup: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> Observations:
    obs = Observations()

    # ---- preconditions: the repository must be empty and have auto-delete on -------------------------------
    info = _json(http.get(f"/repos/{repo}"))
    default = info.get("default_branch", "main")
    obs.note("repo.default_branch", default)
    obs.note("repo.delete_branch_on_merge", info.get("delete_branch_on_merge"))
    obs.note("repo.allow_merge_commit", info.get("allow_merge_commit"))
    obs.note("repo.permissions", info.get("permissions"))
    branches = http.get(f"/repos/{repo}/branches")
    obs.note("repo.branches_before", _json(branches) if branches.status_code == 200 else f"HTTP {branches.status_code}")
    if not obs.expect(info.get("delete_branch_on_merge") is True, "precondition: the throwaway repository must have “Automatically delete head branches” ON"):
        return obs
    if not obs.expect(branches.status_code in (404, 409) or _json(branches) == [], "precondition: the repository must be completely empty (no branches)"):
        return obs

    if toggle_setting:
        _setting_pass(obs, http, repo, token, validate_connection, check_ready)

    # ---- O1: are Git Data writes refused in a repository with no commits? ------------------------------------
    with github_factory(token) as client:
        try:
            sha = client.create_blob(repo, b"probe")
            obs.note("empty_repo.git_data_blob", f"ACCEPTED ({sha[:10]})")
        except GitHubError as exc:
            obs.note("empty_repo.git_data_blob", f"REFUSED (HTTP {exc.status}, kind {exc.kind})")
    obs.steps.append("git-data-probe")

    # ---- the delivery itself -----------------------------------------------------------------------------------
    manifest, files = _project()
    files = {**files, CI_WORKFLOW_PATH: REAL_CI_WORKFLOW.read_bytes()}  # the real, pinned workflow, so CI really runs
    world = build_world(repo_name=repo, manifest=manifest, files=files)
    run_delivery(
        world.client,
        world.delivery_id,
        ORG,
        github_factory=github_factory,
        resolve_token=lambda ref: token if ref == TOKEN_REF else None,
    )
    row = delivery_row(world.client, world.delivery_id)
    obs.steps.append("delivery")
    obs.note("delivery.status", row["status"])
    obs.note("delivery.root_matches_baseline", row.get("root_matches_baseline"))
    if not obs.expect(row["status"] == "done", f"the delivery did not finish: {row.get('error')}"):
        return _finish(obs, http, repo, cleanup)
    prs = row["prs"]
    obs.note("delivery.prs", [(p["kind"], p["branch"], p["pr_number"]) for p in prs])
    obs.note("per_call_verification", "every blob, tree and commit SHA GitHub returned equalled the computed one (the delivery completed)")

    # ---- the initial commit: equal to the baseline? signed? ----------------------------------------------------
    init_sha = _json(http.get(f"/repos/{repo}/git/ref/heads/{default}")).get("object", {}).get("sha")
    obs.note("initial_commit.sha", init_sha)
    obs.note("initial_commit.equals_baseline_B0", init_sha == baseline_commit().commit_sha)
    verification = _json(http.get(f"/repos/{repo}/commits/{init_sha}")).get("commit", {}).get("verification", {})
    obs.note("initial_commit.verification", {"verified": verification.get("verified"), "reason": verification.get("reason")})
    obs.expect(
        row.get("root_matches_baseline") == (init_sha == baseline_commit().commit_sha),
        "the delivery's root_matches_baseline disagrees with the real comparison against B0",
    )

    # ---- the stack: bases and the final tree --------------------------------------------------------------------
    pulls = [_json(http.get(f"/repos/{repo}/pulls/{p['pr_number']}")) for p in prs]
    bases = [pull.get("base", {}).get("ref") for pull in pulls]
    heads = [pull.get("head", {}).get("ref") for pull in pulls]
    obs.note("stack.bases", bases)
    obs.expect(len(prs) == 3, f"expected a scaffold PR and two phase PRs, got {len(prs)}")
    if len(prs) == 3:
        obs.expect(bases == [default, heads[0], heads[1]], f"PR bases are not stacked as designed: {bases} on heads {heads}")
    with github_factory(token) as client:
        tip = client.get_ref_sha(repo, f"heads/{prs[-1]['branch']}")
        final_tree = client.get_commit(repo, tip).tree_sha
    obs.note("stack.final_tree_equals_zip_tree", final_tree == tree_sha_of_files(files))
    obs.expect(final_tree == tree_sha_of_files(files), "the last branch's tree SHA differs from the zip's (expected identical for an empty repository)")

    # ---- the customer's CI actually starts ------------------------------------------------------------------------
    deadline = time.monotonic() + ci_wait_seconds
    runs: dict = {}
    while True:
        runs = _json(http.get(f"/repos/{repo}/actions/runs", params={"branch": prs[0]["branch"], "event": "pull_request"}))
        if runs.get("total_count", 0) >= 1 or time.monotonic() >= deadline:
            break
        sleep(5)
    workflow_runs = runs.get("workflow_runs") or []
    obs.note("ci.runs_for_scaffold_pr", runs.get("total_count", 0))
    if workflow_runs:
        obs.note("ci.latest_run", {"status": workflow_runs[0].get("status"), "conclusion": workflow_runs[0].get("conclusion"), "url": workflow_runs[0].get("html_url")})
    obs.expect(bool(workflow_runs), "no CI run started for the scaffold pull request (is GitHub Actions enabled for the repository?)")
    obs.steps.append("ci")

    # ---- merge PR 0: is PR 1 retargeted at the default branch? -----------------------------------------------------
    merge = http.put(f"/repos/{repo}/pulls/{prs[0]['pr_number']}/merge", json={"merge_method": "merge"})
    obs.note("merge.scaffold_pr", f"HTTP {merge.status_code}: {str(_json(merge).get('message'))[:80]}")
    if obs.expect(merge.status_code == 200, "could not merge the scaffold pull request through the API"):
        retargeted = False
        for _ in range(int(settle_seconds // 3) + 1):
            base_now = _json(http.get(f"/repos/{repo}/pulls/{prs[1]['pr_number']}")).get("base", {}).get("ref")
            if base_now == default:
                retargeted = True
                break
            sleep(3)
        obs.note("merge.next_pr_base_after_merge", base_now)
        obs.note("merge.next_pr_retargeted_at_default", retargeted)
        obs.expect(retargeted, "after merging the scaffold PR, the next PR was NOT retargeted at the default branch (§15.13 assumption false)")
        gone = http.get(f"/repos/{repo}/git/ref/heads/{prs[0]['branch']}").status_code == 404
        obs.note("merge.scaffold_branch_deleted", gone)
        obs.expect(gone, "the merged scaffold branch was not deleted")
    obs.steps.append("merge")

    return _finish(obs, http, repo, cleanup)


def _setting_pass(obs: Observations, http: httpx.Client, repo: str, token: str, validate_connection, check_ready) -> None:
    """With "Automatically delete head branches" OFF, connect-time validation and the deliver pre-check must both block
    (design §15.13). Runs FIRST, while the repository is still empty: after a delivery the repository has several
    branches, and validation would (rightly) refuse it for that reason before ever reaching this setting."""
    off = http.patch(f"/repos/{repo}", json={"delete_branch_on_merge": False})
    obs.note("setting_pass.turn_off", f"HTTP {off.status_code}")
    if off.status_code == 200:
        try:
            try:
                validate_connection(repo, token)
                obs.expect(False, "connect-time validation ACCEPTED a repository with auto-delete off")
            except HTTPException as exc:
                obs.expect(exc.status_code == 422 and "Automatically delete head branches" in str(exc.detail), f"connect-time validation refused for the wrong reason: {exc.status_code} {str(exc.detail)[:80]}")
                obs.note("setting_pass.connect_blocked", f"HTTP {exc.status_code}")
            try:
                check_ready(token, repo)
                obs.expect(False, "the deliver pre-check ACCEPTED a repository with auto-delete off")
            except HTTPException as exc:
                obs.expect(exc.status_code == 409 and "Automatically delete head branches" in str(exc.detail), f"deliver pre-check refused for the wrong reason: {exc.status_code} {str(exc.detail)[:80]}")
                obs.note("setting_pass.deliver_blocked", f"HTTP {exc.status_code}")
        finally:
            back = http.patch(f"/repos/{repo}", json={"delete_branch_on_merge": True})
            obs.note("setting_pass.restored", f"HTTP {back.status_code}")
            obs.expect(back.status_code == 200, "COULD NOT RESTORE “Automatically delete head branches” on the repository: turn it back on by hand")
    else:
        obs.note("setting_pass.skipped", "the token cannot change repository settings (needs Administration: write)")
    obs.steps.append("setting-pass")


def _finish(obs: Observations, http: httpx.Client, repo: str, cleanup: bool) -> Observations:
    """Closes the pull requests this run opened and deletes its `migrate/` branches. The repository itself
    (now holding an initial commit and a merged scaffold) is left for the user to delete."""
    if not cleanup:
        return obs
    for ref in _json(http.get(f"/repos/{repo}/git/matching-refs/heads/migrate/")) or []:
        branch = ref["ref"].removeprefix("refs/heads/")
        for pull in _json(http.get(f"/repos/{repo}/pulls", params={"head": f"{repo.split('/')[0]}:{branch}", "state": "open"})) or []:
            http.patch(f"/repos/{repo}/pulls/{pull['number']}", json={"state": "closed"})
        http.delete(f"/repos/{repo}/git/refs/{ref['ref'].removeprefix('refs/')}")
    obs.steps.append("cleanup")
    return obs
