"""`agent3/commit_plan.py` — design_agent3.md §15.2, §15.3, §15.14 (Phase 7 task 2).

Pure, so no Supabase or Docker: a hand-built two-phase, three-unit project and its
manifest. The real-git replay at the bottom proves the plan's tree and commit SHAs are the
ones git itself produces, which is what lets the zip's `.git` (task 3) and the GitHub push
(task 6) share one history.
"""

import copy
import os
import shutil
import subprocess

import pytest

from document_analyzer.agent3.commit_plan import (
    AUTHOR_EMAIL,
    BASELINE_TIMESTAMP,
    Base,
    CommitPlanError,
    baseline_commit,
    build_commit_plan,
    tree_sha_of_files,
)
from document_analyzer.agent3.git_objects import FileRef, blob_sha, file_refs
from document_analyzer.agent3.project_layout import (
    CI_WORKFLOW_PATH,
    STUB_README,
    UNITS_INIT_PATH,
    render_units_init,
    scaffold_path_allowed,
    unit_path_allowed,
)

CREATED_AT = "2026-10-01T12:00:00+00:00"
CREATED_EPOCH = 1790856000

SLUGS = ["01_01_cart", "01_02_totals", "02_01_checkout"]
HOSTILE_LABEL = "```@acme/security-team #1 ![x](https://evil.example/p.png) <script>"


def _project() -> tuple[dict, dict[str, bytes]]:
    """(manifest, files) for: scaffold, phase 1 = two units, phase 2 = one failed+stubbed unit."""
    files: dict[str, bytes] = {
        "pytest.ini": b"[pytest]\n",
        "requirements.txt": b"pytest\n",
        ".gitignore": b"__pycache__/\n",
        CI_WORKFLOW_PATH: b"name: CI\n",
        "app/__init__.py": b"",
        "app/main.py": b"app = None\n",
        "tests/conftest.py": b"# fixtures\n",
        "README.md": b"# Full report\n\n| unit | status |\n",
        UNITS_INIT_PATH: render_units_init(SLUGS).encode("utf-8"),
    }
    scaffold = sorted(p for p in files if p not in ("README.md", UNITS_INIT_PATH))

    def unit(cluster_id: str, slug: str, status: str, attempts: int, flags: list[str]) -> dict:
        unit_files = [f"app/units/{slug}/__init__.py", f"app/units/{slug}/impl.py", f"tests/units/{slug}/test_contract.py"]
        for path in unit_files:
            files[path] = f"# {slug} {path}\n".encode("utf-8")
        return {
            "cluster_id": cluster_id,
            "unit_slug": slug,
            "label": HOSTILE_LABEL if slug == "01_02_totals" else slug,
            "status": status,
            "attempts": attempts,
            "flags": flags,
            "files": unit_files,
        }

    manifest = {
        "version": 1,
        "created_at": CREATED_AT,
        "scaffold": scaffold,
        "phases": [
            {
                "phase_order": 1,
                "phase_cluster_id": "phase-1-id",
                "label": HOSTILE_LABEL,
                "units": [
                    unit("c1", SLUGS[0], "passed", 1, []),
                    unit("c2", SLUGS[1], "passed", 2, ["unverified_inputs"]),
                ],
            },
            {
                "phase_order": 2,
                "phase_cluster_id": "phase-2-id",
                "label": "Checkout",
                "units": [unit("c3", SLUGS[2], "error", 3, ["built_on_failed_dependency", "stubbed"])],
            },
        ],
        "final_test_summary": {"passed": 9, "failed": 0, "errors": 0, "duration_s": 1.0},
    }
    return manifest, files


@pytest.fixture
def project():
    return _project()


def test_the_last_tree_equals_the_project_files(project):
    manifest, files = project

    plan = build_commit_plan(manifest, files)

    assert plan.final_tree_sha == tree_sha_of_files(files)
    assert plan.commits[-1].tree_sha == tree_sha_of_files(files)


def test_the_plan_is_deterministic(project):
    manifest, files = project

    first = build_commit_plan(manifest, files)
    second = build_commit_plan(copy.deepcopy(manifest), dict(reversed(list(files.items()))))

    assert [c.commit_sha for c in first.commits] == [c.commit_sha for c in second.commits]
    assert [c.tree_sha for c in first.commits] == [c.tree_sha for c in second.commits]


def test_commit_order_is_baseline_scaffold_units_in_plan_order_then_report(project):
    manifest, files = project

    plan = build_commit_plan(manifest, files)

    assert [c.kind for c in plan.commits] == ["baseline", "scaffold", "unit", "unit", "unit", "report"]
    assert [c.cluster_id for c in plan.commits if c.kind == "unit"] == ["c1", "c2", "c3"]
    # A linear chain rooted on the baseline.
    assert plan.commits[0].parent_sha is None
    for parent, child in zip(plan.commits, plan.commits[1:]):
        assert child.parent_sha == parent.commit_sha


def test_identity_and_dates_are_fixed_and_derived_from_created_at(project):
    manifest, files = project

    plan = build_commit_plan(manifest, files)

    assert plan.commits[0].timestamp == BASELINE_TIMESTAMP
    assert [c.timestamp for c in plan.commits[1:]] == [CREATED_EPOCH + i for i in range(1, 6)]
    assert AUTHOR_EMAIL.endswith(".invalid")


def test_every_commit_imports_only_the_units_committed_so_far(project):
    manifest, files = project
    plan = build_commit_plan(manifest, files)

    state: dict[str, bytes] = {}
    committed: list[str] = []
    for commit in plan.commits:
        state.update(commit.changes)
        if commit.kind == "unit":
            committed.append(next(s for s in SLUGS if f"app/units/{s}/impl.py" in commit.changes))
        if commit.kind == "baseline":
            continue
        assert state[UNITS_INIT_PATH] == render_units_init(committed).encode("utf-8")
        for slug in committed:
            assert f"app/units/{slug}/__init__.py" in state


def test_the_scaffold_commit_has_the_stub_readme_and_an_empty_units_init(project):
    manifest, files = project

    scaffold = build_commit_plan(manifest, files).commits[1]

    assert scaffold.changes["README.md"] == STUB_README.encode("utf-8")
    assert scaffold.changes[UNITS_INIT_PATH] == render_units_init([]).encode("utf-8")
    assert CI_WORKFLOW_PATH in scaffold.changes
    assert not any(path.startswith(("app/units/0", "tests/units/")) for path in scaffold.changes)


def test_the_report_commit_replaces_the_stub_readme_with_the_full_one(project):
    manifest, files = project

    report = build_commit_plan(manifest, files).commits[-1]

    assert report.kind == "report"
    assert report.changes == {"README.md": files["README.md"]}


def test_failed_and_stubbed_units_are_committed_and_labelled(project):
    manifest, files = project

    messages = {c.cluster_id: c.message for c in build_commit_plan(manifest, files).commits if c.kind == "unit"}

    assert messages["c1"].splitlines()[0] == "Migrate unit 01_01_cart"
    assert messages["c3"].splitlines()[0] == "Migrate unit 02_01_checkout [error] [stubbed]"
    assert "Flags: built_on_failed_dependency, stubbed." in messages["c3"]
    assert all(m.endswith("\n") and not m.endswith("\n\n") for m in messages.values())


def test_no_label_ever_reaches_a_commit_message(project):
    manifest, files = project

    plan = build_commit_plan(manifest, files)

    for commit in plan.commits:
        for fragment in ("```", "@acme", "evil.example", "<script>", "#1"):
            assert fragment not in commit.message


def test_groups_follow_the_stacked_branch_layout(project):
    manifest, files = project

    plan = build_commit_plan(manifest, files)

    assert [g.key for g in plan.groups] == ["scaffold", "phase-1", "phase-2"]
    assert [c.kind for c in plan.groups[0].commits] == ["scaffold"]
    assert [c.kind for c in plan.groups[1].commits] == ["unit", "unit"]
    assert [c.kind for c in plan.groups[2].commits] == ["unit", "report"]
    # The label rides along for the PR renderer, still raw: it must be neutralised there (§15.9).
    assert plan.groups[1].label == HOSTILE_LABEL


def test_a_plan_with_no_units_puts_the_report_on_the_scaffold_branch():
    files = {"pytest.ini": b"x", "README.md": b"# r\n", UNITS_INIT_PATH: render_units_init([]).encode("utf-8")}
    manifest = {"version": 1, "created_at": CREATED_AT, "scaffold": ["pytest.ini"], "phases": []}

    plan = build_commit_plan(manifest, files)

    assert [g.key for g in plan.groups] == ["scaffold"]
    assert [c.kind for c in plan.groups[0].commits] == ["scaffold", "report"]
    assert plan.final_tree_sha == tree_sha_of_files(files)


# --------------------------------------------------------------------------- #
# Path re-validation (§15.3) and manifest integrity
# --------------------------------------------------------------------------- #


def _with_scaffold_path(project, path: str):
    manifest, files = project
    files = {**files, path: b"x"}
    manifest = {**manifest, "scaffold": manifest["scaffold"] + [path]}
    return manifest, files


@pytest.mark.parametrize(
    "path",
    [
        "../escape.py",
        "/etc/passwd",
        ".git/config",
        "app/../../x",
        ".github/workflows/evil.yml",
        ".github/CODEOWNERS",
        "scripts/run.sh",
        "app/units/other/x.py",
        "tests/units/other/test_x.py",
        "back\\slash.py",
    ],
)
def test_scaffold_paths_outside_the_allow_list_are_refused(project, path):
    manifest, files = _with_scaffold_path(project, path)

    with pytest.raises(CommitPlanError):
        build_commit_plan(manifest, files)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "app/units/02_01_checkout/x.py",  # another unit's directory
        "app/units/__init__.py",
        "tests/units/01_02_totals/../01_01_cart/x.py",
        "app/units/01_01_cart/.git/hooks/pre-commit",
        "app/units/01_01_cart2/x.py",  # prefix of the slug but not its directory
        "app/other/x.py",
    ],
)
def test_unit_files_outside_their_own_directories_are_refused(project, path):
    manifest, files = project
    manifest = copy.deepcopy(manifest)
    manifest["phases"][0]["units"][0]["files"].append(path)
    files = {**files, path: b"x"}

    with pytest.raises(CommitPlanError):
        build_commit_plan(manifest, files)


def test_a_workflow_other_than_the_fixed_one_cannot_be_planted_through_a_unit(project):
    manifest, files = project
    manifest = copy.deepcopy(manifest)
    evil = "app/units/01_01_cart/../../../.github/workflows/pwn.yml"
    manifest["phases"][0]["units"][0]["files"].append(evil)
    files = {**files, evil: b"on: push"}

    with pytest.raises(CommitPlanError):
        build_commit_plan(manifest, files)


def test_a_project_file_the_manifest_does_not_claim_is_refused(project):
    manifest, files = project

    with pytest.raises(CommitPlanError, match="not covered by the manifest"):
        build_commit_plan(manifest, {**files, "app/stowaway.py": b"x"})


def test_a_path_claimed_twice_is_refused(project):
    manifest, files = project
    manifest = copy.deepcopy(manifest)
    manifest["phases"][1]["units"][0]["files"].append(manifest["phases"][0]["units"][0]["files"][0])

    with pytest.raises(CommitPlanError):
        build_commit_plan(manifest, files)


def test_a_manifest_file_missing_from_the_project_is_refused(project):
    manifest, files = project
    files = {k: v for k, v in files.items() if k != "app/main.py"}

    with pytest.raises(CommitPlanError, match="not in the project files"):
        build_commit_plan(manifest, files)


def test_a_units_init_that_does_not_match_the_manifest_is_refused(project):
    manifest, files = project
    files = {**files, UNITS_INIT_PATH: render_units_init(list(reversed(SLUGS))).encode("utf-8")}

    with pytest.raises(CommitPlanError, match="plan order"):
        build_commit_plan(manifest, files)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(version=2),
        lambda m: m.pop("created_at"),
        lambda m: m.update(created_at="yesterday"),
        lambda m: m["phases"][0]["units"][0].update(status="unknown"),
        lambda m: m["phases"][0]["units"][0].update(flags=["rm -rf"]),
        lambda m: m["phases"][0]["units"][0].update(unit_slug="Bad Slug; drop"),
        lambda m: m["phases"][0]["units"][0].update(attempts=-1),
        lambda m: m["phases"][1].update(phase_order=1),
        lambda m: m["phases"][0].update(units=[]),
    ],
)
def test_malformed_manifest_values_are_refused(project, mutate):
    manifest, files = project
    manifest = copy.deepcopy(manifest)
    mutate(manifest)

    with pytest.raises(CommitPlanError):
        build_commit_plan(manifest, files)


def test_allow_list_helpers():
    assert scaffold_path_allowed("app/wp_compat/fake.py")
    assert scaffold_path_allowed("tests/conftest.py")
    assert scaffold_path_allowed(CI_WORKFLOW_PATH)
    assert not scaffold_path_allowed(".github/workflows/other.yml")
    assert not scaffold_path_allowed("app/units/x.py")
    assert unit_path_allowed("app/units/01_01_a/x.py", "01_01_a")
    assert not unit_path_allowed("app/units/01_01_a/x.py", "01_01_b")
    assert not unit_path_allowed("app/units/01_01_a/x.py", "../x")


@pytest.mark.parametrize("path", ["app/x\ny.py", "app/x\ty.py", "app/\x00.py", "app/x\x7f.py"])
def test_paths_with_control_characters_are_refused(path):
    assert not scaffold_path_allowed(path)
    assert not unit_path_allowed(path.replace("app/", "app/units/01_01_a/", 1), "01_01_a")


# --------------------------------------------------------------------------- #
# A real target repo as the base (delivery, not the zip)
# --------------------------------------------------------------------------- #


def test_a_supplied_base_becomes_the_first_parent_and_ours_win_collisions(project):
    manifest, files = project
    existing = {
        "LICENSE": FileRef("100644", blob_sha(b"MIT\n")),
        "README.md": FileRef("100644", blob_sha(b"someone else's readme\n")),
        "bin/tool": FileRef("100755", blob_sha(b"#!/bin/sh\n")),
    }
    base = Base(commit_sha="a" * 40, entries=existing)

    plan = build_commit_plan(manifest, files, base=base)

    assert plan.baseline is None
    assert plan.commits[0].kind == "scaffold"
    assert plan.commits[0].parent_sha == "a" * 40
    expected = {**existing, **file_refs(files)}
    from document_analyzer.agent3.git_objects import build_tree

    assert plan.final_tree_sha == build_tree(expected).root_sha
    # The repo's own files, mode included, are still there; the colliding README became ours.
    assert plan.final_tree_sha != tree_sha_of_files(files)


def test_the_baseline_commit_is_stable_across_runs():
    assert baseline_commit().commit_sha == baseline_commit().commit_sha
    assert baseline_commit().changes == {"README.md": STUB_README.encode("utf-8")}


# --------------------------------------------------------------------------- #
# Replay through real git
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("git") is None, reason="requires the git binary")
def test_replaying_the_plan_through_real_git_reproduces_every_sha(project, tmp_path):
    manifest, files = project
    plan = build_commit_plan(manifest, files)

    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

    def git(*args, stdin=None, extra=None) -> str:
        return (
            subprocess.run(
                ["git", "-c", "core.autocrlf=false", *args],
                cwd=tmp_path,
                input=stdin,
                capture_output=True,
                env={**env, **(extra or {})},
                check=True,
            )
            .stdout.decode("utf-8")
            .strip()
        )

    git("init", "-q")
    state: dict[str, bytes] = {}
    parent = None
    for commit in plan.commits:
        state.update(commit.changes)
        for path, data in state.items():
            sha = git("hash-object", "-w", "--stdin", stdin=data)
            git("update-index", "--add", "--cacheinfo", f"100644,{sha},{path}")
        tree = git("write-tree")
        identity = {
            "GIT_AUTHOR_NAME": "Galvanise Agent 3",
            "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
            "GIT_AUTHOR_DATE": f"{commit.timestamp} +0000",
            "GIT_COMMITTER_NAME": "Galvanise Agent 3",
            "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
            "GIT_COMMITTER_DATE": f"{commit.timestamp} +0000",
        }
        args = ["commit-tree", tree] + (["-p", parent] if parent else [])
        parent = git(*args, stdin=commit.message.encode("utf-8"), extra=identity)

        assert tree == commit.tree_sha, commit.kind
        assert parent == commit.commit_sha, commit.kind
