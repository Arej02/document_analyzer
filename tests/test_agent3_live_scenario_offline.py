"""The live GitHub scenario, run against the fake GitHub — Phase 7 task 8.

This proves the SCRIPT (`live_github_scenario.run_scenario`): that it drives the product code end to end, records what it
sees, reports a design claim that does not hold instead of crashing, never leaks the token into its report, and refuses a
repository it must not touch. It proves nothing about real GitHub; `test_agent3_github_live.py` is the run that does.
"""

import httpx
import pytest

from document_analyzer.agent3 import connection_router, delivery_router
from document_analyzer.agent3.delivery.github_api import GitHubClient

from fakes_github import TOKEN, FakeGitHub, FakeRepo
from live_github_scenario import format_report, run_scenario

REPO = "acme/live-throwaway"


def scenario(monkeypatch, repo: FakeRepo, **kwargs):
    github = FakeGitHub(repo)

    def factory(token):
        return GitHubClient(token, transport=github.transport, sleep=lambda s: None)

    monkeypatch.setattr(connection_router, "make_github_client", factory)
    monkeypatch.setattr(delivery_router, "make_github_client", factory)
    http = httpx.Client(base_url="https://api.github.com", transport=github.transport, headers={"Authorization": f"Bearer {TOKEN}"})
    obs = run_scenario(
        http=http,
        github_factory=factory,
        token=TOKEN,
        repo=REPO,
        validate_connection=connection_router._validate_with_github,
        check_ready=delivery_router._check_github_still_ready,
        sleep=lambda s: None,
        **kwargs,
    )
    return obs, github


def test_the_whole_scenario_runs_against_the_model_and_every_claim_holds(monkeypatch):
    obs, github = scenario(monkeypatch, FakeRepo(REPO))

    assert obs.failures == [], obs.failures
    assert obs.steps == ["setting-pass", "git-data-probe", "delivery", "ci", "merge", "cleanup"]
    facts = obs.facts
    assert facts["empty_repo.git_data_blob"].startswith("REFUSED")  # the model's assumption, written down as an observation
    assert facts["initial_commit.equals_baseline_B0"] is True and facts["delivery.root_matches_baseline"] is True
    assert facts["initial_commit.verification"] == {"verified": False, "reason": "unsigned"}
    assert facts["stack.final_tree_equals_zip_tree"] is True
    assert facts["ci.runs_for_scaffold_pr"] == 1
    assert facts["merge.next_pr_retargeted_at_default"] is True and facts["merge.scaffold_branch_deleted"] is True
    assert facts["setting_pass.connect_blocked"] == "HTTP 422" and facts["setting_pass.deliver_blocked"] == "HTTP 409"
    assert facts["setting_pass.restored"] == "HTTP 200" and github.repos[REPO].delete_branch_on_merge is True


def test_cleanup_closes_this_runs_pull_requests_and_deletes_its_branches(monkeypatch):
    _, github = scenario(monkeypatch, FakeRepo(REPO))

    repo = github.repos[REPO]
    assert not [ref for ref in repo.refs if ref.startswith("refs/heads/migrate/")]
    assert all(p["state"] == "closed" for p in repo.pulls)
    assert "refs/heads/main" in repo.refs  # the repository itself is left alone


def test_without_cleanup_the_branches_stay(monkeypatch):
    obs, github = scenario(monkeypatch, FakeRepo(REPO), cleanup=False)

    assert "cleanup" not in obs.steps
    assert [ref for ref in github.repos[REPO].refs if ref.startswith("refs/heads/migrate/")]


def test_a_missing_retarget_is_reported_as_a_failed_claim_not_a_crash(monkeypatch):
    obs, _ = scenario(monkeypatch, FakeRepo(REPO, retarget_on_merge=False))

    assert any("NOT retargeted" in failure for failure in obs.failures)
    assert obs.facts["merge.next_pr_retargeted_at_default"] is False
    assert "setting-pass" in obs.steps  # the rest of the run still happened and was reported


def test_ci_that_never_starts_is_reported(monkeypatch):
    obs, _ = scenario(monkeypatch, FakeRepo(REPO, ci_enabled=False))

    assert any("no CI run started" in failure for failure in obs.failures)


def test_a_token_that_cannot_change_settings_skips_the_second_pass_without_failing(monkeypatch):
    obs, github = scenario(monkeypatch, FakeRepo(REPO, can_admin=False))

    assert obs.failures == []
    assert "Administration: write" in obs.facts["setting_pass.skipped"]
    assert github.repos[REPO].delete_branch_on_merge is True  # never touched


def test_the_second_pass_can_be_switched_off(monkeypatch):
    obs, _ = scenario(monkeypatch, FakeRepo(REPO), toggle_setting=False)

    assert obs.failures == [] and "setting-pass" not in obs.steps


def test_a_repository_with_auto_delete_off_is_refused_before_any_write(monkeypatch):
    obs, github = scenario(monkeypatch, FakeRepo(REPO, delete_branch_on_merge=False))

    assert any("must have “Automatically delete head branches” ON" in failure for failure in obs.failures)
    assert obs.steps == [] and not [r for r in github.requests if r["method"] in ("POST", "PUT", "PATCH", "DELETE")]


def test_a_repository_that_is_not_empty_is_refused_before_any_write(monkeypatch):
    repo = FakeRepo(REPO)
    repo.refs["refs/heads/main"] = "a" * 40
    obs, github = scenario(monkeypatch, repo)

    assert any("completely empty" in failure for failure in obs.failures)
    assert obs.steps == [] and not [r for r in github.requests if r["method"] in ("POST", "PUT", "PATCH", "DELETE")]


def test_a_repository_that_cannot_be_read_is_reported_as_that_not_as_a_setting_problem(monkeypatch):
    github = FakeGitHub(FakeRepo("someone/else"))  # REPO is not in the fake: GitHub answers 404 for it

    def factory(token):
        return GitHubClient(token, transport=github.transport, sleep=lambda s: None)

    http = httpx.Client(base_url="https://api.github.com", transport=github.transport, headers={"Authorization": f"Bearer {TOKEN}"})
    obs = run_scenario(http=http, github_factory=factory, token=TOKEN, repo=REPO, validate_connection=None, check_ready=None, sleep=lambda s: None)

    assert obs.steps == [] and obs.facts["repo.get"] == "HTTP 404"
    [failure] = obs.failures
    assert "cannot read" in failure and "not found" in failure and "Repository access" in failure
    assert "Automatically delete head branches" not in failure
    assert not [r for r in github.requests if r["method"] != "GET"]


def test_a_rejected_token_is_reported_as_a_token_problem(monkeypatch):
    github = FakeGitHub(FakeRepo(REPO))
    http = httpx.Client(base_url="https://api.github.com", transport=github.transport, headers={"Authorization": "Bearer ghp_" + "Q" * 36})
    obs = run_scenario(
        http=http, github_factory=lambda t: GitHubClient(t, transport=github.transport), token=TOKEN, repo=REPO,
        validate_connection=None, check_ready=None, sleep=lambda s: None,
    )  # fmt: skip

    assert obs.facts["repo.get"] == "HTTP 401" and "rejected the token" in obs.failures[0]


@pytest.mark.parametrize("bad", ["owner/name/extra", "https://github.com/acme/x", "", "../x"])
def test_a_malformed_repo_setting_is_caught_before_any_request(monkeypatch, bad):
    github = FakeGitHub(FakeRepo(REPO))
    http = httpx.Client(base_url="https://api.github.com", transport=github.transport, headers={"Authorization": f"Bearer {TOKEN}"})
    obs = run_scenario(http=http, github_factory=lambda t: None, token=TOKEN, repo=bad, validate_connection=None, check_ready=None, sleep=lambda s: None)

    assert "must look like owner/name" in obs.failures[0] and github.requests == []


def test_a_setting_that_the_token_cannot_see_is_its_own_finding(monkeypatch):
    obs, github = scenario(monkeypatch, FakeRepo(REPO, delete_branch_on_merge=None))

    assert any("did not report" in failure and "Administration: read" in failure for failure in obs.failures)
    assert obs.steps == [] and not [r for r in github.requests if r["method"] != "GET"]


def test_the_report_lists_observations_and_failures_and_never_contains_the_token(monkeypatch):
    obs, _ = scenario(monkeypatch, FakeRepo(REPO, retarget_on_merge=False))
    obs.note("accidentally", f"leaked {TOKEN}")  # even if a value somehow carried the token

    report = format_report(obs, TOKEN)

    assert "Observed:" in report and "initial_commit.equals_baseline_B0" in report
    assert "Design claims that did NOT hold:" in report and "NOT retargeted" in report
    assert TOKEN not in report and "[redacted]" in report
    assert "Every design claim held." in format_report(scenario(monkeypatch, FakeRepo(REPO))[0])
