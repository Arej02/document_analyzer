"""`agent3/delivery/deliver.py` and `pr_text.py` — design_agent3.md §15.4, §15.8, §15.9, §15.13, §15.14
(Phase 7 task 6, the run itself).

Everything runs against `fakes_github.FakeGitHub`, a MODEL of GitHub's documented behaviour (see that file), a
FakeClient database and fake storage holding a real zip. What these tests prove is the delivery's own logic; that
real GitHub behaves like the fake is what the opt-in live test (task 8) settles.
"""

import json

import pytest

from document_analyzer.agent3.commit_plan import tree_sha_of_files
from document_analyzer.agent3.delivery import pr_text
from document_analyzer.agent3.delivery.deliver import run_delivery
from document_analyzer.agent3.delivery.github_api import GitHubClient
from document_analyzer.supabase_client import insert_scoped

from delivery_world import CrashAfter, ORG, REPO, TOKEN, TOKEN_REF, build_world, delivery_row, github_factory, make_zip, new_delivery
from fakes_github import FakeRepo
from test_agent3_commit_plan import HOSTILE_LABEL


def run(world, *, transport=None, delivery_id=None, token=TOKEN):
    run_delivery(
        world.client,
        delivery_id or world.delivery_id,
        ORG,
        github_factory=github_factory(world.github, transport=transport),
        resolve_token=lambda ref: token if ref == TOKEN_REF else None,
    )


def writes(world) -> list[dict]:
    return [r for r in world.github.requests if r["method"] in ("POST", "PUT", "PATCH")]


def exec8(world) -> str:
    return world.execution_job_id.replace("-", "")[:8]


def seed_other_history(world, *, branch="main", path="LICENSE") -> str:
    """Gives the target repo a first commit that is not ours, as a repo created with a license would have."""
    with GitHubClient(TOKEN, transport=world.github.transport, sleep=lambda s: None) as client:
        sha = client.create_initial_commit(
            REPO, branch=branch, path=path, content=b"MIT\n", message="Add license", name="Someone", email="s@example.invalid", timestamp=1700000000
        )
    world.github.requests.clear()
    return sha


def _norm(world, text: str) -> str:
    """Each build_world() has its own execution id, which is embedded in every branch name."""
    return text.replace(exec8(world), "EXEC")


def snapshot(world) -> dict:
    repo = world.github.repos[REPO]
    return {
        "refs": {_norm(world, ref): sha for ref, sha in repo.refs.items()},
        "pulls": sorted((_norm(world, p["head"]["ref"]), _norm(world, p["base"]["ref"]), _norm(world, p["title"])) for p in repo.pulls),
    }


def normalized_prs(world, prs: list[dict]) -> list[dict]:
    return [{**pr, "branch": _norm(world, pr["branch"])} for pr in prs]


# --------------------------------------------------------------------------- #
# happy paths
# --------------------------------------------------------------------------- #


def test_an_empty_repo_gets_a_scaffold_pr_then_one_stacked_pr_per_phase():
    world = build_world()

    run(world)

    row = delivery_row(world.client, world.delivery_id)
    assert row["status"] == "done" and row["error"] is None
    assert [(p["kind"], p["phase_order"]) for p in row["prs"]] == [("scaffold", None), ("phase", 1), ("phase", 2)]
    scaffold, phase1, phase2 = (p["branch"] for p in row["prs"])
    assert scaffold == f"migrate/{exec8(world)}/scaffold"
    assert phase2 == f"migrate/{exec8(world)}/phase-2-checkout"
    assert phase1.startswith(f"migrate/{exec8(world)}/phase-1-") and all(c.isalnum() or c in "/-" for c in phase1)

    repo = world.github.repos[REPO]
    assert [(p["head"]["ref"], p["base"]["ref"]) for p in repo.pulls] == [(scaffold, "main"), (phase1, scaffold), (phase2, phase1)]
    assert set(repo.refs) == {"refs/heads/main", f"refs/heads/{scaffold}", f"refs/heads/{phase1}", f"refs/heads/{phase2}"}
    # Content: the last branch's tree is the zip's tree, file for file.
    tip = repo.refs[f"refs/heads/{phase2}"]
    assert repo.commits[tip]["tree"] == tree_sha_of_files(world.files)
    assert row["root_matches_baseline"] is True  # in the MODEL the initial commit is stored exactly as sent (§15.14, unverified)
    assert [p["url"] for p in row["prs"]] == [f"https://github.com/{REPO}/pull/{n}" for n in (1, 2, 3)]


def test_the_initial_commit_does_not_name_a_branch_that_does_not_exist_yet():
    world = build_world()

    run(world)

    [put] = world.github.calls("PUT")
    assert "branch" not in put["json"]
    assert "refs/heads/main" in world.github.repos[REPO].refs  # GitHub (here: the model) creates the default branch


def test_the_zips_own_git_directory_is_ignored_and_nothing_is_forced():
    world = build_world()

    run(world)

    assert delivery_row(world.client, world.delivery_id)["status"] == "done"
    pushed_paths = {entry["path"] for r in world.github.calls("POST", "/git/trees") for entry in r["json"]["tree"]}
    assert not any(path.startswith(".git/") for path in pushed_paths)
    assert not world.github.calls("PATCH")  # no ref was ever moved, let alone forced
    assert all(r["auth"] == f"Bearer {TOKEN}" for r in world.github.requests)
    assert all(TOKEN not in json.dumps(r["json"]) for r in world.github.requests)


def test_a_repo_that_already_has_history_keeps_its_files_and_the_root_is_theirs():
    world = build_world()
    seed_other_history(world)

    run(world)

    row = delivery_row(world.client, world.delivery_id)
    assert row["status"] == "done" and row["root_matches_baseline"] is False
    repo = world.github.repos[REPO]
    tip = repo.refs[f"refs/heads/{row['prs'][-1]['branch']}"]
    final = repo.trees[repo.commits[tip]["tree"]]
    assert "LICENSE" in final and "app/main.py" in final and final["README.md"].sha != "0" * 40
    assert repo.commits[tip]["tree"] != tree_sha_of_files(world.files)  # their file makes the trees differ, as documented
    assert not world.github.calls("PUT")  # no initial commit was needed


def test_a_plan_with_no_units_delivers_just_the_scaffold_pr():
    from document_analyzer.agent3.project_layout import render_units_init
    from test_agent3_commit_plan import CREATED_AT

    files = {"pytest.ini": b"x", "README.md": b"# r\n", "app/units/__init__.py": render_units_init([]).encode()}
    manifest = {"version": 1, "created_at": CREATED_AT, "scaffold": ["pytest.ini"], "phases": [], "final_test_summary": None}
    world = build_world(manifest=manifest, files=files)

    run(world)

    row = delivery_row(world.client, world.delivery_id)
    assert row["status"] == "done" and [p["kind"] for p in row["prs"]] == ["scaffold"]
    repo = world.github.repos[REPO]
    assert repo.commits[repo.refs[f"refs/heads/{row['prs'][0]['branch']}"]]["tree"] == tree_sha_of_files(files)


def test_re_running_a_finished_delivery_changes_nothing_on_github():
    world = build_world()
    run(world)
    before = snapshot(world)
    second = new_delivery(world.client, world.project_id, world.execution_job_id)
    world.github.requests.clear()

    run(world, delivery_id=second)

    assert delivery_row(world.client, second)["status"] == "done"
    assert writes(world) == []
    assert snapshot(world) == before
    assert delivery_row(world.client, second)["prs"] == delivery_row(world.client, world.delivery_id)["prs"]


def test_a_delivery_that_is_already_done_or_failed_is_not_run_again():
    world = build_world()
    delivery_row(world.client, world.delivery_id)["status"] = "failed"

    run(world)

    assert world.github.requests == []


# --------------------------------------------------------------------------- #
# PR text: fixed titles, labels only inside code spans
# --------------------------------------------------------------------------- #


def _inside_code_span(text: str, needle: str) -> bool:
    for line in text.splitlines():
        index = line.find(needle)
        if index != -1:
            return line[:index].count("`") % 2 == 1
    return False


def test_titles_are_fixed_templates_and_bodies_confine_labels_to_code_spans():
    world = build_world()

    run(world)

    pulls = world.github.repos[REPO].pulls
    ref = exec8(world)
    assert [p["title"] for p in pulls] == [
        f"Add project scaffold (migration {ref})",
        f"Migrate phase 1 of 2 (migration {ref})",
        f"Migrate phase 2 of 2 (migration {ref})",
    ]
    phase1 = pulls[1]["body"]
    assert "```" not in phase1
    for fragment in ("@acme/security-team", "![x](", "evil.example", "<script>", "#1"):
        assert _inside_code_span(phase1, fragment), fragment
    for pull in pulls:
        assert HOSTILE_LABEL not in pull["title"] and "evil.example" not in pull["title"]
        assert "advisory" in pull["body"] and "stack" in pull["body"].lower()


def test_units_that_need_attention_are_called_out_at_the_top():
    world = build_world()

    run(world)

    body = world.github.repos[REPO].pulls[2]["body"]
    assert "need attention" in body and "`02_01_checkout`" in body and "`error`" in body and "`stubbed`" in body
    assert "Final run" not in body and "9 passed, 0 failed, 0 errors" in body


# --------------------------------------------------------------------------- #
# per-call SHA verification (§15.14)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind,attribute", [("commit", "commit_sha_override"), ("tree", "tree_sha_override")])
def test_a_sha_that_differs_from_the_computed_one_aborts_before_any_branch_exists(kind, attribute):
    world = build_world()
    seed_other_history(world)
    setattr(world.github, attribute, lambda sha: "e" * 40)

    run(world)

    row = delivery_row(world.client, world.delivery_id)
    assert row["status"] == "failed"
    assert f"different {kind}" in row["error"] and "e" * 40 in row["error"]
    repo = world.github.repos[REPO]
    assert set(repo.refs) == {"refs/heads/main"} and repo.pulls == []
    assert not world.github.calls("PATCH") and not world.github.calls("POST", "/git/refs")


# --------------------------------------------------------------------------- #
# never overwrite, never write when something is off
# --------------------------------------------------------------------------- #


def test_a_branch_that_exists_at_another_commit_is_never_touched():
    world = build_world()
    init = seed_other_history(world)
    with GitHubClient(TOKEN, transport=world.github.transport, sleep=lambda s: None) as client:
        from document_analyzer.agent3.git_objects import FileRef

        blob = client.create_blob(REPO, b"squatter")
        tree = client.create_tree(REPO, {"squat.txt": FileRef("100644", blob)}, base_tree=client.get_commit(REPO, init).tree_sha)
        child = client.create_commit(REPO, tree=tree, parents=[init], message="squat", name="n", email="e@example.invalid", timestamp=1700000001)
        client.create_ref(REPO, f"refs/heads/migrate/{exec8(world)}/scaffold", child)
    world.github.requests.clear()

    run(world)

    row = delivery_row(world.client, world.delivery_id)
    assert row["status"] == "failed" and "already exists" in row["error"] and "different commit" in row["error"]
    assert world.github.repos[REPO].refs[f"refs/heads/migrate/{exec8(world)}/scaffold"] == child
    assert writes(world) == [] and world.github.repos[REPO].pulls == []


def test_delete_branch_on_merge_turned_off_stops_the_run_before_any_write():
    world = build_world(repo=FakeRepo(REPO, delete_branch_on_merge=False))

    run(world)

    row = delivery_row(world.client, world.delivery_id)
    assert row["status"] == "failed" and "Automatically delete head branches" in row["error"]
    assert writes(world) == []


@pytest.mark.parametrize(
    "repo_kwargs,expected",
    [({"can_push": False}, "can no longer push"), ({"archived": True}, "archived")],
)
def test_a_repo_that_can_no_longer_be_pushed_to_stops_the_run_before_any_write(repo_kwargs, expected):
    world = build_world(repo=FakeRepo(REPO, **repo_kwargs))

    run(world)

    assert expected in delivery_row(world.client, world.delivery_id)["error"]
    assert writes(world) == []


def test_a_changed_default_branch_stops_the_run():
    world = build_world(repo=FakeRepo(REPO, default_branch="trunk"))

    run(world)

    assert "default branch" in delivery_row(world.client, world.delivery_id)["error"]
    assert writes(world) == []


def test_a_repo_with_other_branches_but_no_default_branch_is_refused():
    world = build_world()
    seed_other_history(world, branch="other")

    run(world)

    row = delivery_row(world.client, world.delivery_id)
    assert row["status"] == "failed" and "other branches" in row["error"]
    assert writes(world) == []


def test_the_connection_changing_or_vanishing_fails_the_run():
    world = build_world()
    connection = world.client.table("project_github_connections").rows[0]
    connection["repo_full_name"] = "acme/somewhere-else"

    run(world)

    assert "changed after this delivery started" in delivery_row(world.client, world.delivery_id)["error"]

    world = build_world()
    world.client.table("project_github_connections").rows.clear()
    run(world)
    assert "connection was removed" in delivery_row(world.client, world.delivery_id)["error"]

    world = build_world()
    run(world, token=None)
    assert "could not be read" in delivery_row(world.client, world.delivery_id)["error"]
    assert world.github.requests == []


# --------------------------------------------------------------------------- #
# hostile or broken inputs
# --------------------------------------------------------------------------- #


def test_an_unsafe_path_in_the_zip_is_refused_before_any_write():
    world = build_world(zip_extra={"../evil.py": b"import os"})

    run(world)

    row = delivery_row(world.client, world.delivery_id)
    assert row["status"] == "failed" and "unsafe path" in row["error"]
    assert writes(world) == []


def test_a_manifest_that_would_plant_a_workflow_is_refused_before_any_write():
    world = build_world()
    manifest = json.loads(json.dumps(world.manifest))
    manifest["scaffold"].append(".github/workflows/pwn.yml")
    files = {**world.files, ".github/workflows/pwn.yml": b"on: push"}
    world = build_world(manifest=manifest, files=files)

    run(world)

    row = delivery_row(world.client, world.delivery_id)
    assert row["status"] == "failed" and "could not be turned into commits" in row["error"]
    assert not any(r["json"] and ".github/workflows/pwn.yml" in json.dumps(r["json"]) for r in writes(world))
    assert [r for r in writes(world) if "/git/" in r["path"]] == []


def test_a_missing_manifest_or_garbage_zip_fails_readably():
    world = build_world(with_manifest=False)
    run(world)
    assert "no delivery manifest" in delivery_row(world.client, world.delivery_id)["error"]

    world = build_world()
    world.client.storage.from_("agent3-output").upload(world.zip_path, b"not a zip")
    run(world)
    assert "not a valid zip" in delivery_row(world.client, world.delivery_id)["error"]


def test_github_echoing_the_token_never_reaches_the_stored_error():
    world = build_world()
    world.github.fail_next(10, 422, message=f"token {TOKEN} is bad")

    run(world)

    row = delivery_row(world.client, world.delivery_id)
    assert row["status"] == "failed" and TOKEN not in row["error"] and "ghp_" not in row["error"]


def test_a_rate_limit_fails_readably_and_a_second_run_finishes():
    world = build_world()
    world.github.fail_next(4, 429, headers={"retry-after": "1"})

    run(world)

    assert "rate limit" in delivery_row(world.client, world.delivery_id)["error"].lower()
    second = new_delivery(world.client, world.project_id, world.execution_job_id)
    run(world, delivery_id=second)
    assert delivery_row(world.client, second)["status"] == "done"


# --------------------------------------------------------------------------- #
# resumability: crash at every request, then run again
# --------------------------------------------------------------------------- #


def _clean_run_request_count() -> int:
    world = build_world()
    run(world)
    return len(world.github.requests)


_TOTAL_REQUESTS = _clean_run_request_count()


@pytest.mark.parametrize("lose_response", [False, True], ids=["request-lost", "response-lost"])
@pytest.mark.parametrize("crash_after", range(1, _TOTAL_REQUESTS))
def test_a_run_interrupted_at_any_request_converges_on_the_same_github_state(crash_after, lose_response):
    clean = build_world()
    run(clean)
    expected = snapshot(clean)
    expected_prs = normalized_prs(clean, delivery_row(clean.client, clean.delivery_id)["prs"])

    world = build_world()
    run(world, transport=CrashAfter(world.github, crash_after, lose_response=lose_response))
    first = delivery_row(world.client, world.delivery_id)
    assert first["status"] == "failed" and first["error"]
    assert TOKEN not in first["error"]
    recorded = normalized_prs(world, first.get("prs", []))  # the real column defaults to []; the in-memory fake has no defaults
    assert recorded == expected_prs[: len(recorded)]  # whatever it recorded is a true prefix

    second = new_delivery(world.client, world.project_id, world.execution_job_id)
    run(world, delivery_id=second)

    final = delivery_row(world.client, second)
    assert final["status"] == "done", final["error"]
    assert snapshot(world) == expected  # no duplicate branches or PRs, same bases and titles
    assert normalized_prs(world, final["prs"]) == expected_prs
    assert not world.github.calls("PATCH")
    repo = world.github.repos[REPO]
    assert len(repo.pulls) == 3


# --------------------------------------------------------------------------- #
# pr_text
# --------------------------------------------------------------------------- #


def test_neutralize_strips_everything_that_could_change_meaning():
    dirty = "line1\nline2\t`tick` pipe|x ‮bidi​zero \x00nul " + "y" * 200
    cleaned = pr_text.neutralize(dirty)

    assert "\n" not in cleaned and "`" not in cleaned and "|" not in cleaned
    assert "‮" not in cleaned and "​" not in cleaned and "\x00" not in cleaned
    assert len(cleaned) <= 80
    assert pr_text.code("```") == "`(unnamed)`" and pr_text.code(None) == "`(unnamed)`"


def test_branch_names_come_only_from_a_safe_alphabet():
    for label in (HOSTILE_LABEL, "../../etc/passwd", "ünïcode ☃", "", None, "A" * 300):
        name = pr_text.branch_name("3f9a1c2e-0000-0000-0000-000000000000", "phase-3", label)
        assert name.startswith("migrate/3f9a1c2e/phase-3") and len(name) <= 80
        assert all(c.isascii() and (c.isalnum() or c in "/-") for c in name) and ".." not in name and "//" not in name
    with pytest.raises(ValueError):
        pr_text.branch_name("3f9a1c2e", "phase-../x")
    assert pr_text.branch_name("3F9A1C2E-1", "scaffold") == "migrate/3f9a1c2e/scaffold"


def test_the_app_link_is_added_only_for_a_plain_http_url(monkeypatch):
    kwargs = dict(execution_job_id="abc123", repo_full_name="a/b", default_branch="main", total_phases=1)

    monkeypatch.setenv("APP_BASE_URL", "https://app.example.test/")
    assert "[open in the app](https://app.example.test/?execution=abc123)" in pr_text.scaffold_body(**kwargs)
    for bad in ("javascript:alert(1)", "https://x.test/ )[evil](", "ftp://x.test", "  "):
        monkeypatch.setenv("APP_BASE_URL", bad)
        assert "open in the app" not in pr_text.scaffold_body(**kwargs)
