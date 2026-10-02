"""`agent3/delivery/github_api.py` against `tests/fakes_github.py` — design_agent3.md §15.7, §15.8
(Phase 7 task 5).

The fake is a model of GitHub's documented behaviour, so these tests prove the client's own
logic (URL safety, redaction, bounded retries, error mapping, no redirects) and that client, fake
and `git_objects` agree on SHAs. They do NOT prove real GitHub behaves like the fake; that is what
the opt-in live test settles (task 8).
"""

import json

import pytest

from document_analyzer.agent3.commit_plan import (
    AUTHOR_EMAIL,
    AUTHOR_NAME,
    BASELINE_TIMESTAMP,
    Base,
    baseline_commit,
    build_commit_plan,
    tree_sha_of_files,
)
from document_analyzer.agent3.delivery import github_api
from document_analyzer.agent3.delivery.github_api import GitHubClient, GitHubError, redact, validate_repo_full_name
from document_analyzer.agent3.git_objects import FileRef
from document_analyzer.agent3.project_layout import STUB_README

from fakes_github import TOKEN, FakeGitHub, FakeRepo
from test_agent3_commit_plan import _project

REPO = "acme/migrated-shop"


@pytest.fixture
def fake():
    return FakeGitHub(FakeRepo(REPO))


def _client(fake: FakeGitHub, **kwargs) -> GitHubClient:
    return GitHubClient(fake.token, transport=fake.transport, **kwargs)


# --------------------------------------------------------------------------- #
# Names, redaction, request hygiene
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["acme/shop", "a-b_c.d/e.f-g_h", "A1/b2"])
def test_valid_repo_names_pass(name):
    assert validate_repo_full_name(name) == name


@pytest.mark.parametrize("name", ["", "x", "a/b/c", "../..", "a/..", "./b", "a b/c", "a/b c", "a/b?x=1", "a/b#f", "a/b%2f", "x" * 101 + "/y", None, 5])
def test_bad_repo_names_are_refused_before_any_request(fake, name):
    with _client(fake) as client:
        with pytest.raises(GitHubError) as caught:
            client.get_repo(name)

    assert caught.value.kind == "invalid_input"
    assert fake.requests == []


def test_redact_strips_the_token_and_every_token_shape():
    text = f"x {TOKEN} y github_pat_{'A' * 30} z gho_{'b' * 30} {'ghs_' + 'c' * 30}"

    cleaned = redact(text, TOKEN)

    assert TOKEN not in cleaned and "github_pat_" not in cleaned and "gho_" not in cleaned and "ghs_" not in cleaned
    assert redact("a custom-token-value here", "custom-token-value") == "a [redacted] here"


def test_every_request_goes_to_the_fixed_https_host_with_the_token_only_in_the_header(fake):
    with _client(fake) as client:
        client.get_repo(REPO)
        client.list_branch_names(REPO)
        client.create_initial_commit(
            REPO, branch="main", path="README.md", content=b"x", message="m", name="n", email="e@x.invalid", timestamp=1
        )
        client.create_blob(REPO, b"data")

    assert fake.requests
    for request in fake.requests:
        assert (request["scheme"], request["host"]) == ("https", "api.github.com")
        assert request["auth"] == f"Bearer {TOKEN}"
        assert TOKEN not in request["url"]
        assert TOKEN not in json.dumps(request["json"])


def test_a_wrong_token_is_an_auth_error_without_the_token_in_the_message(fake):
    with GitHubClient("ghp_" + "B" * 36, transport=fake.transport) as client:
        with pytest.raises(GitHubError) as caught:
            client.get_repo(REPO)

    assert caught.value.kind == "auth" and caught.value.status == 401
    assert "ghp_" + "B" * 36 not in str(caught.value)


def test_github_echoing_the_token_back_is_redacted(fake):
    fake.fail_next(1, 422, message=f"validation failed for {TOKEN}")
    with _client(fake) as client:
        with pytest.raises(GitHubError) as caught:
            client.create_blob(REPO, b"x")

    assert TOKEN not in str(caught.value)
    assert caught.value.kind == "validation"


def test_a_missing_token_is_refused():
    with pytest.raises(GitHubError):
        GitHubClient("")


# --------------------------------------------------------------------------- #
# Retries, rate limits, redirects, hostile responses
# --------------------------------------------------------------------------- #


def test_retry_after_is_honoured_and_then_the_call_succeeds(fake):
    sleeps: list[float] = []
    fake.fail_next(2, 429, headers={"retry-after": "7"}, message="slow down")
    with _client(fake, sleep=sleeps.append) as client:
        assert client.get_repo(REPO).full_name == REPO

    assert sleeps == [7.0, 7.0]


def test_rate_limit_retries_are_bounded(fake):
    sleeps: list[float] = []
    fake.fail_next(10, 429, headers={"retry-after": "1"})
    with _client(fake, sleep=sleeps.append) as client:
        with pytest.raises(GitHubError) as caught:
            client.get_repo(REPO)

    assert caught.value.kind == "rate_limit"
    assert len(sleeps) == github_api.MAX_RETRIES == 3
    assert len(fake.requests) == 4


def test_a_wait_over_the_cap_fails_without_sleeping(fake):
    sleeps: list[float] = []
    fake.fail_next(1, 429, headers={"retry-after": "120"})
    with _client(fake, sleep=sleeps.append) as client:
        with pytest.raises(GitHubError) as caught:
            client.get_repo(REPO)

    assert caught.value.kind == "rate_limit" and sleeps == []


def test_the_ratelimit_reset_header_sets_the_wait(fake):
    sleeps: list[float] = []
    fake.fail_next(1, 403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1010"}, message="API rate limit exceeded")
    with _client(fake, sleep=sleeps.append, clock=lambda: 1000.0) as client:
        client.get_repo(REPO)

    assert sleeps == [11.0]


def test_a_plain_permission_403_is_not_retried(fake):
    sleeps: list[float] = []
    fake.fail_next(1, 403, message="Resource not accessible by personal access token")
    with _client(fake, sleep=sleeps.append) as client:
        with pytest.raises(GitHubError) as caught:
            client.get_repo(REPO)

    assert caught.value.kind == "permission" and sleeps == [] and len(fake.requests) == 1


def test_a_get_is_retried_on_503_but_a_post_never_is(fake):
    sleeps: list[float] = []
    fake.fail_next(2, 503)
    with _client(fake, sleep=sleeps.append) as client:
        assert client.get_repo(REPO).default_branch == "main"
        assert len(sleeps) == 2

        fake.requests.clear()
        fake.fail_next(1, 500)
        with pytest.raises(GitHubError) as caught:
            client.create_ref(REPO, "refs/heads/x", "0" * 40)

    assert caught.value.kind == "server"
    assert len(fake.calls("POST")) == 1  # not re-sent: it may have been processed


def test_a_redirect_is_refused_and_never_followed(fake):
    from httpx import Response

    fake.queue.append(Response(301, headers={"location": "https://evil.example/steal"}))
    with _client(fake) as client:
        with pytest.raises(GitHubError) as caught:
            client.get_repo(REPO)

    assert caught.value.kind == "redirect"
    assert [r["host"] for r in fake.requests] == ["api.github.com"]


def test_a_non_json_success_body_is_an_error(fake):
    from httpx import Response

    fake.queue.append(Response(200, content=b"<html>proxy page</html>"))
    with _client(fake) as client:
        with pytest.raises(GitHubError) as caught:
            client.get_repo(REPO)

    assert caught.value.kind == "other"


def test_an_oversized_response_is_refused(fake, monkeypatch):
    monkeypatch.setattr(github_api, "MAX_RESPONSE_BYTES", 10)
    with _client(fake) as client:
        with pytest.raises(GitHubError) as caught:
            client.get_repo(REPO)

    assert caught.value.kind == "too_large"


def test_a_transport_failure_message_carries_no_url(fake):
    import httpx

    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with GitHubClient(TOKEN, transport=httpx.MockTransport(boom)) as client:
        with pytest.raises(GitHubError) as caught:
            client.get_repo(REPO)

    assert "api.github.com" not in str(caught.value) and "ConnectError" in str(caught.value)


# --------------------------------------------------------------------------- #
# Repository facts
# --------------------------------------------------------------------------- #


def test_get_repo_parses_the_settings_delivery_depends_on():
    fake = FakeGitHub(FakeRepo(REPO, default_branch="trunk", delete_branch_on_merge=False, allow_merge_commit=False, can_push=False))
    with _client(fake) as client:
        info = client.get_repo(REPO)

    assert (info.default_branch, info.delete_branch_on_merge, info.allow_merge_commit, info.can_push) == ("trunk", False, False, False)


def test_a_setting_the_token_cannot_read_is_none_not_false():
    fake = FakeGitHub(FakeRepo(REPO, delete_branch_on_merge=None, allow_merge_commit=None))
    with _client(fake) as client:
        info = client.get_repo(REPO)

    assert info.delete_branch_on_merge is None and info.allow_merge_commit is None


def test_an_unknown_repo_is_not_found(fake):
    with _client(fake) as client:
        with pytest.raises(GitHubError) as caught:
            client.get_repo("acme/other")

    assert caught.value.kind == "not_found"


def test_an_empty_repo_has_no_branches_and_no_default_ref(fake):
    with _client(fake) as client:
        assert client.list_branch_names(REPO) == []
        assert client.get_ref_sha(REPO, "heads/main") is None


def test_git_data_writes_to_an_empty_repo_are_a_conflict_in_the_model(fake):
    """UNVERIFIED model of real GitHub (see fakes_github.py); the live test settles it."""
    with _client(fake) as client:
        with pytest.raises(GitHubError) as caught:
            client.create_blob(REPO, b"x")

    assert caught.value.kind == "conflict"


# --------------------------------------------------------------------------- #
# Client, fake and git_objects agree on SHAs: a whole plan pushed as stacked branches
# --------------------------------------------------------------------------- #


def _push_plan(client: GitHubClient, plan, root_sha: str) -> None:
    """What task 6 will do, minus its resume and failure handling: every commit through
    blobs -> tree (base_tree) -> commit, each response SHA compared with the computed one."""
    previous_commit, previous_tree = root_sha, client.get_commit(REPO, root_sha).tree_sha
    for group in plan.groups:
        for commit in group.commits:
            refs = {path: FileRef("100644", client.create_blob(REPO, data)) for path, data in commit.changes.items()}
            tree = client.create_tree(REPO, refs, base_tree=previous_tree)
            assert tree == commit.tree_sha, f"tree mismatch at {commit.kind}"
            sha = client.create_commit(
                REPO, tree=tree, parents=[previous_commit], message=commit.message, name=AUTHOR_NAME, email=AUTHOR_EMAIL, timestamp=commit.timestamp
            )
            assert sha == commit.commit_sha, f"commit mismatch at {commit.kind}"
            previous_commit, previous_tree = sha, tree
        client.create_ref(REPO, f"refs/heads/migrate/test/{group.key}", previous_commit)


def test_an_empty_repo_round_trips_the_whole_plan_with_every_sha_matching(fake):
    manifest, files = _project()
    with _client(fake) as client:
        init_sha = client.create_initial_commit(
            REPO,
            branch="main",
            path="README.md",
            content=STUB_README.encode("utf-8"),
            message="Initial commit",
            name=AUTHOR_NAME,
            email=AUTHOR_EMAIL,
            timestamp=BASELINE_TIMESTAMP,
        )
        # In the MODEL the Contents API stores our commit exactly as sent, so it equals B0.
        # Whether real GitHub does is the §15.14 question the live test answers.
        assert init_sha == baseline_commit().commit_sha

        base_tree = client.get_commit(REPO, init_sha).tree_sha
        plan = build_commit_plan(manifest, files, base=Base(init_sha, client.get_tree(REPO, base_tree)))
        _push_plan(client, plan, init_sha)

        head = client.get_ref_sha(REPO, "heads/migrate/test/phase-2")

    assert head == plan.commits[-1].commit_sha
    assert fake.repos[REPO].commits[head]["tree"] == tree_sha_of_files(files)  # tree-identical to the zip
    assert sorted(ref for ref in fake.repos[REPO].refs) == [
        "refs/heads/main",
        "refs/heads/migrate/test/phase-1",
        "refs/heads/migrate/test/phase-2",
        "refs/heads/migrate/test/scaffold",
    ]


def test_a_rewritten_commit_sha_is_visible_to_the_caller(fake):
    """What per-call verification (task 6) relies on: the client returns GitHub's SHA unchanged."""
    manifest, files = _project()
    with _client(fake) as client:
        init_sha = client.create_initial_commit(
            REPO, branch="main", path="README.md", content=STUB_README.encode(), message="Initial commit",
            name=AUTHOR_NAME, email=AUTHOR_EMAIL, timestamp=BASELINE_TIMESTAMP,
        )  # fmt: skip
        plan = build_commit_plan(manifest, files, base=Base(init_sha, client.get_tree(REPO, client.get_commit(REPO, init_sha).tree_sha)))
        fake.commit_sha_override = lambda sha: "f" * 40
        scaffold = plan.commits[0]
        refs = {path: FileRef("100644", client.create_blob(REPO, data)) for path, data in scaffold.changes.items()}
        tree = client.create_tree(REPO, refs, base_tree=client.get_commit(REPO, init_sha).tree_sha)
        sha = client.create_commit(
            REPO, tree=tree, parents=[init_sha], message=scaffold.message, name=AUTHOR_NAME, email=AUTHOR_EMAIL, timestamp=scaffold.timestamp
        )

    assert sha == "f" * 40 != scaffold.commit_sha


def test_a_truncated_base_tree_is_an_error_not_a_partial_base(fake):
    with _client(fake) as client:
        init_sha = client.create_initial_commit(
            REPO, branch="main", path="README.md", content=b"x", message="m", name="n", email="e@x.invalid", timestamp=1
        )
        fake.repos[REPO].truncate_trees = True
        with pytest.raises(GitHubError) as caught:
            client.get_tree(REPO, client.get_commit(REPO, init_sha).tree_sha)

    assert caught.value.kind == "too_large"


def test_update_ref_fast_forwards_but_refuses_a_rewrite(fake):
    with _client(fake) as client:
        first = client.create_initial_commit(
            REPO, branch="main", path="a.txt", content=b"1", message="one", name="n", email="e@x.invalid", timestamp=1
        )
        tree = client.create_tree(REPO, {"b.txt": FileRef("100644", client.create_blob(REPO, b"2"))}, base_tree=client.get_commit(REPO, first).tree_sha)
        second = client.create_commit(REPO, tree=tree, parents=[first], message="two", name="n", email="e@x.invalid", timestamp=2)
        sibling = client.create_commit(REPO, tree=tree, parents=[first], message="other", name="n", email="e@x.invalid", timestamp=3)

        client.update_ref(REPO, "refs/heads/main", second)
        assert client.get_ref_sha(REPO, "heads/main") == second
        with pytest.raises(GitHubError) as caught:
            client.update_ref(REPO, "heads/main", sibling)  # not a fast-forward

    assert caught.value.kind == "validation"
    assert fake.repos[REPO].refs["refs/heads/main"] == second
    assert all(r["json"]["force"] is False for r in fake.calls("PATCH"))


def test_create_ref_refuses_to_overwrite_an_existing_branch(fake):
    with _client(fake) as client:
        sha = client.create_initial_commit(
            REPO, branch="main", path="a.txt", content=b"1", message="one", name="n", email="e@x.invalid", timestamp=1
        )
        client.create_ref(REPO, "refs/heads/x", sha)
        with pytest.raises(GitHubError) as caught:
            client.create_ref(REPO, "refs/heads/x", sha)

    assert caught.value.kind == "validation"


# --------------------------------------------------------------------------- #
# Pull requests
# --------------------------------------------------------------------------- #


def test_pull_requests_are_found_created_and_never_duplicated(fake):
    with _client(fake) as client:
        sha = client.create_initial_commit(
            REPO, branch="main", path="a.txt", content=b"1", message="one", name="n", email="e@x.invalid", timestamp=1
        )
        client.create_ref(REPO, "refs/heads/feature", sha)

        assert client.find_pull_request(REPO, "feature") is None
        created = client.create_pull_request(REPO, head="feature", base="main", title="t", body="b")
        assert (created.number, created.head, created.base) == (1, "feature", "main")
        assert client.find_pull_request(REPO, "feature") == created
        with pytest.raises(GitHubError) as duplicate:
            client.create_pull_request(REPO, head="feature", base="main", title="t", body="b")
        with pytest.raises(GitHubError) as missing:
            client.create_pull_request(REPO, head="nope", base="main", title="t", body="b")

    assert duplicate.value.kind == "validation" and missing.value.kind == "validation"
    assert fake.calls("POST", "/pulls")[0]["json"]["maintainer_can_modify"] is False
