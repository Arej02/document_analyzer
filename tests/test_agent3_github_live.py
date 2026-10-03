"""Opt-in LIVE check of GitHub delivery against real api.github.com — Phase 7 task 8, design_agent3.md §15.15.

This is the only test that can settle what the design could only model: whether a repository with no commits refuses Git Data
writes, whether the Contents API stores our initial commit exactly as sent (so it equals the baseline B0) and whether it comes
back signed, whether the customer's CI really starts, and whether merging a pull request retargets the next one.

It WRITES to a real repository, so it does nothing unless every one of these is set (in your shell, never in chat):

    AGENT3_RUN_GITHUB_LIVE=1
    AGENT3_LIVE_GITHUB_REPO=<owner>/<name>            a brand-new EMPTY repository you created for this and will delete after
    AGENT3_LIVE_GITHUB_CONFIRM=<owner>/<name>         the same value again: proof you mean this repository
    AGENT3_LIVE_GITHUB_TOKEN=<token>                  fine-grained, limited to that one repository: Contents, Pull requests and Workflows
                                                      read/write, plus Actions read; add Administration write to also run the
                                                      "auto-delete off" pass (without it that pass is skipped and reported)
    AGENT3_LIVE_REPORT=<path>                         optional: also write the report to this file

Preconditions the test checks itself and refuses to proceed past: the repository is completely empty, and "Automatically delete head
branches" is ON. It creates an initial commit, three branches and pull requests, merges the first, briefly turns the auto-delete
setting off (and back on), then closes its pull requests and deletes its `migrate/` branches. It never deletes the repository: the
repository now has history, so use a fresh one for every run and delete it yourself afterwards.

Run:  uv run pytest tests/test_agent3_github_live.py -s -q
"""

import os
from pathlib import Path

import httpx
import pytest

from document_analyzer.agent3 import connection_router, delivery_router
from document_analyzer.agent3.delivery.github_api import GitHubClient

from live_github_scenario import format_report, run_scenario

_REQUIRED = ["AGENT3_RUN_GITHUB_LIVE", "AGENT3_LIVE_GITHUB_REPO", "AGENT3_LIVE_GITHUB_CONFIRM", "AGENT3_LIVE_GITHUB_TOKEN"]

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT3_RUN_GITHUB_LIVE") != "1" or not all(os.environ.get(name) for name in _REQUIRED),
    reason="opt-in: set AGENT3_RUN_GITHUB_LIVE=1, AGENT3_LIVE_GITHUB_REPO, AGENT3_LIVE_GITHUB_CONFIRM and AGENT3_LIVE_GITHUB_TOKEN (see this file's docstring)",
)


def test_live_github_delivery_scenario():
    repo = os.environ["AGENT3_LIVE_GITHUB_REPO"].strip()
    token = os.environ["AGENT3_LIVE_GITHUB_TOKEN"].strip()
    if os.environ["AGENT3_LIVE_GITHUB_CONFIRM"].strip() != repo:
        pytest.fail("AGENT3_LIVE_GITHUB_CONFIRM must equal AGENT3_LIVE_GITHUB_REPO exactly: this test writes to that repository")

    http = httpx.Client(
        base_url="https://api.github.com",
        timeout=30,
        follow_redirects=False,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    try:
        obs = run_scenario(
            http=http,
            github_factory=lambda t: GitHubClient(t),
            token=token,
            repo=repo,
            validate_connection=connection_router._validate_with_github,
            check_ready=delivery_router._check_github_still_ready,
            ci_wait_seconds=180,
            settle_seconds=45,
        )
    finally:
        http.close()

    report = format_report(obs, token)
    print("\n" + report)
    report_path = os.environ.get("AGENT3_LIVE_REPORT")
    if report_path:
        Path(report_path).write_text(report + "\n", encoding="utf-8")

    assert not obs.failures, "some design claims did not hold against real GitHub:\n- " + "\n- ".join(obs.failures)
