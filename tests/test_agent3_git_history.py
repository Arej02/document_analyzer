"""`agent3/worker/git_history.py` — design_agent3.md §15.5 (Phase 7 task 3).

Real `git` throughout (the module's whole point is that git independently reproduces the
SHAs we computed), so the file skips when `git` is not installed. No Supabase or Docker.
"""

import os
import shutil
import subprocess
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from document_analyzer.agent3.commit_plan import build_commit_plan
from document_analyzer.agent3.worker import git_history
from document_analyzer.agent3.worker.assemble import _build_zip
from document_analyzer.agent3.worker.git_history import GitHistoryError, try_add_git_history, write_git_history

from test_agent3_commit_plan import _project

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="requires the git binary")

CRLF_FILE = "app/units/01_01_cart/impl.py"


def _materialise(tmp_path: Path, name: str, files: dict[str, bytes]) -> Path:
    root = tmp_path / name
    for path, data in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    result = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, capture_output=True, env=env, check=True)
    return result.stdout.decode("utf-8").strip()


@pytest.fixture
def project():
    manifest, files = _project()
    files[CRLF_FILE] = b"line one\r\nline two\r\n"  # must survive byte-for-byte
    return manifest, files


def _dir_snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_writes_a_repository_whose_history_is_the_plan(tmp_path, project):
    manifest, files = project
    root = _materialise(tmp_path, "p", files)

    assert try_add_git_history(root, manifest) is True

    plan = build_commit_plan(manifest, files)
    log = _git(root, "log", "--reverse", "--format=%H %T %an <%ae> %at %s").split("\n")
    assert len(log) == len(plan.commits) == 6
    for line, commit in zip(log, plan.commits):
        sha, tree, *_ = line.split(" ")
        assert (sha, tree) == (commit.commit_sha, commit.tree_sha)
    assert _git(root, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert "Galvanise Agent 3 <noreply@galvanise.invalid>" in log[1]
    assert log[2].endswith("Migrate unit 01_01_cart")


def test_the_working_tree_is_clean_and_blobs_are_byte_exact(tmp_path, project):
    manifest, files = project
    root = _materialise(tmp_path, "p", files)
    try_add_git_history(root, manifest)

    assert _git(root, "status", "--porcelain") == ""
    blob = subprocess.run(["git", "cat-file", "blob", f"HEAD:{CRLF_FILE}"], cwd=root, capture_output=True, check=True).stdout
    assert blob == files[CRLF_FILE]


def test_the_tree_has_no_hooks_and_a_fixed_config(tmp_path, project):
    manifest, files = project
    root = _materialise(tmp_path, "p", files)
    try_add_git_history(root, manifest)

    assert not (root / ".git" / "hooks").exists()
    config = (root / ".git" / "config").read_text(encoding="ascii")
    assert "filemode = true" in config and "symlinks" not in config and "ignorecase" not in config
    assert not (root / ".git" / "logs").exists()  # no reflog with a wall-clock time in it


def test_two_runs_over_the_same_tree_give_identical_git_dirs_and_zips(tmp_path, project):
    manifest, files = project
    first = _materialise(tmp_path, "a", files)
    second = _materialise(tmp_path, "b", files)

    try_add_git_history(first, manifest)
    try_add_git_history(second, manifest)

    assert _dir_snapshot(first / ".git") == _dir_snapshot(second / ".git")
    assert _build_zip(first) == _build_zip(second)


def test_the_extracted_zip_is_a_clean_repository(tmp_path, project):
    manifest, files = project
    root = _materialise(tmp_path, "p", files)
    try_add_git_history(root, manifest)

    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(BytesIO(_build_zip(root))) as archive:
        archive.extractall(extracted)

    assert (extracted / ".git" / "HEAD").exists()
    assert _git(extracted, "rev-list", "--count", "HEAD") == "6"
    assert _git(extracted, "status", "--porcelain") == ""
    assert _git(extracted, "fsck", "--strict") == ""


def test_a_poisoned_global_git_config_and_environment_change_nothing(tmp_path, monkeypatch, project):
    """Hostile ambient git settings (autocrlf, forced identity, a hooks dir, signing) and
    GIT_* variables must not leak into the history; neither may a worker secret."""
    manifest, files = project
    home = tmp_path / "evil-home"
    (home / "hooks").mkdir(parents=True)
    (home / ".gitconfig").write_text(
        f"[core]\n\tautocrlf = true\n\thooksPath = {(home / 'hooks').as_posix()}\n"
        "[user]\n\tname = Evil\n\temail = evil@example.com\n[commit]\n\tgpgsign = true\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Evil")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2001-01-01T00:00:00")
    monkeypatch.setenv("AGENT3_CANARY_SECRET", "s3cret")
    root = _materialise(tmp_path, "p", files)

    assert try_add_git_history(root, manifest) is True

    plan = build_commit_plan(manifest, files)
    assert _git(root, "rev-parse", "HEAD") == plan.commits[-1].commit_sha
    assert set(_git(root, "log", "--format=%an").split("\n")) == {"Galvanise Agent 3"}
    assert "AGENT3_CANARY_SECRET" not in git_history._environment(home)
    assert not any("secret" in key.lower() for key in git_history._environment(home))
    assert _dir_snapshot(root / ".git")  # exists, nothing escaped to the evil hooks dir
    assert list((home / "hooks").iterdir()) == []


def test_a_history_that_differs_from_the_plan_is_detected_and_discarded(tmp_path, monkeypatch, project):
    """The SHA check must bite: tamper with the stream git receives and nothing is kept."""
    manifest, files = project
    root = _materialise(tmp_path, "p", files)
    real_stream = git_history._fast_import_stream
    monkeypatch.setattr(git_history, "_fast_import_stream", lambda plan: real_stream(plan).replace(b"requirements.txt", b"requirement5.txt", 1))

    plan = build_commit_plan(manifest, files)
    with pytest.raises(GitHistoryError, match="differs from the computed plan"):
        write_git_history(_materialise(tmp_path, "direct", files), plan)

    assert try_add_git_history(root, manifest) is False
    assert not (root / ".git").exists()


def test_without_git_the_zip_is_built_without_a_git_dir(tmp_path, monkeypatch, project):
    manifest, files = project
    root = _materialise(tmp_path, "p", files)
    monkeypatch.setattr(git_history.shutil, "which", lambda name: None)

    assert try_add_git_history(root, manifest) is False
    assert not (root / ".git").exists()
    assert not any(name.startswith(".git/") for name in zipfile.ZipFile(BytesIO(_build_zip(root))).namelist())


def test_a_bad_manifest_never_raises_and_leaves_no_git_dir(tmp_path, project):
    manifest, files = project
    manifest = {**manifest, "created_at": None}
    root = _materialise(tmp_path, "p", files)

    assert try_add_git_history(root, manifest) is False
    assert not (root / ".git").exists()


def test_an_existing_git_dir_is_refused_not_overwritten(tmp_path, project):
    manifest, files = project
    root = _materialise(tmp_path, "p", {**files, ".git/HEAD": b"ref: refs/heads/other\n"})
    plan = build_commit_plan(manifest, files)

    with pytest.raises(GitHistoryError, match="already exists"):
        write_git_history(root, plan)
    assert (root / ".git" / "HEAD").read_bytes() == b"ref: refs/heads/other\n"


def test_git_failure_degrades_to_no_history(tmp_path, monkeypatch, project):
    manifest, files = project
    root = _materialise(tmp_path, "p", files)

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(git_history.subprocess, "run", boom)

    assert try_add_git_history(root, manifest) is False
    assert not (root / ".git").exists()
