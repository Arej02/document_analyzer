"""Git history inside the zip — design_agent3.md §15.5 (the former tasks_aamf.md D4).

After `final_test` has run the suite (so the sandbox never sees a `.git`) and before the
zip is built, this writes a real `.git` into the assembled tree: the commit plan of
`commit_plan.py` (baseline, scaffold, one commit per unit in plan order, report) on a single
branch `main`, so the downloaded zip is already a repository with reviewable history.

How: the plan is streamed to ONE `git fast-import` process, which builds the trees and
commits itself, then every commit and tree SHA git reports is compared with the SHA we
computed in pure Python (`git_objects.py`). That makes real git an independent check of
our object builder; any disagreement discards the `.git` rather than shipping a history that
differs from what GitHub delivery will push. (The design text named hash-object / mktree /
commit-tree; fast-import is the same plumbing family but one process instead of thousands.)

Safety and reproducibility:
- git only ever receives bytes we hand it on stdin. It never reads, executes or interprets the
  project's files, and `read-tree` touches only the index.
- `--template=<empty dir>` (no sample hooks), `core.hooksPath` pointed at an empty dir, no
  system or global config, a minimal environment, `protocol.allow=never`, no network.
- `.git/config` is rewritten with fixed text (git's own init writes host-specific keys such as
  `core.symlinks` and `core.ignorecase`), reflogs are off, and the index is built with
  `read-tree` so it carries no stat data. Two runs over the same tree on the same host give
  byte-identical `.git` contents.

Never fails the job: a missing `git`, a git failure or any mismatch is logged and the zip is
built without a `.git` (§15.5). It never blocks GitHub delivery, which rebuilds the same objects
from the manifest and does not read this directory.
"""

import logging
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from ..commit_plan import (
    AUTHOR_EMAIL,
    AUTHOR_NAME,
    CommitPlan,
    CommitPlanError,
    build_commit_plan,
)
from ..git_objects import GitObjectError

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 120
_BRANCH = "main"

# Fixed, host-independent. `filemode = true` is what a Linux checkout of this zip expects.
_GIT_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n\tlogallrefupdates = false\n"


class GitHistoryError(Exception):
    """git could not produce the history, or produced a different one than we computed."""


def _environment(home: Path) -> dict[str, str]:
    # Only what git needs to run; nothing from the worker's own environment (secrets) is forwarded.
    keep = ("PATH", "SYSTEMROOT", "SystemDrive", "COMSPEC", "PATHEXT", "TEMP", "TMP")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return env


def _run(args: list[str], *, cwd: Path, env: dict[str, str], hooks_dir: Path, stdin: bytes | None = None) -> bytes:
    command = [
        "git",
        "-c", "core.autocrlf=false",
        "-c", "core.safecrlf=false",
        "-c", "core.fsmonitor=false",
        "-c", "core.logAllRefUpdates=false",
        "-c", f"core.hooksPath={hooks_dir}",
        "-c", "protocol.allow=never",
        *args,
    ]  # fmt: skip
    try:
        result = subprocess.run(command, cwd=cwd, input=stdin, capture_output=True, env=env, timeout=_GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise GitHistoryError(f"git {args[0]} timed out") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[:500]
        raise GitHistoryError(f"git {args[0]} failed ({result.returncode}): {detail}")
    return result.stdout


def _fast_import_stream(plan: CommitPlan) -> bytes:
    """The plan as a `git fast-import` stream on `refs/heads/main`. The first commit has no
    `from`, so it is a root; each later commit parents the branch tip. No `author` line is
    written, so git copies the committer, which is exactly what the plan's commits use."""
    out = bytearray()
    for commit in plan.commits:
        message = commit.message.encode("utf-8")
        out += f"commit refs/heads/{_BRANCH}\n".encode("ascii")
        out += f"committer {AUTHOR_NAME} <{AUTHOR_EMAIL}> {commit.timestamp} +0000\n".encode("utf-8")
        out += f"data {len(message)}\n".encode("ascii") + message + b"\n"
        for path, data in sorted(commit.changes.items()):
            if path.startswith('"') or any(ord(ch) < 0x20 for ch in path):
                # fast-import would need C-style quoting for these; project_layout never allows them.
                raise GitHistoryError(f"path {path!r} cannot be written to a fast-import stream")
            out += f"M 100644 inline {path}\n".encode("utf-8")
            out += f"data {len(data)}\n".encode("ascii") + data + b"\n"
        out += b"\n"
    return bytes(out)


def write_git_history(project_dir: Path, plan: CommitPlan) -> None:
    """Writes `project_dir/.git` from `plan` and verifies it. Raises `GitHistoryError`;
    the caller removes a partial `.git`."""
    git_dir = project_dir / ".git"
    if git_dir.exists():
        raise GitHistoryError(f"{git_dir} already exists")

    scratch = Path(tempfile.mkdtemp(prefix="agent3-git-"))
    try:
        template, hooks, home = scratch / "template", scratch / "hooks", scratch / "home"
        for directory in (template, hooks, home):
            directory.mkdir()
        env = _environment(home)

        def git(*args: str, stdin: bytes | None = None) -> bytes:
            return _run(list(args), cwd=project_dir, env=env, hooks_dir=hooks, stdin=stdin)

        git("init", "--quiet", f"--template={template}", f"--initial-branch={_BRANCH}")
        (git_dir / "config").write_bytes(_GIT_CONFIG.encode("ascii"))

        git("fast-import", "--quiet", stdin=_fast_import_stream(plan))

        listing = git("log", "--reverse", "--format=%H %T", _BRANCH).decode("ascii").split("\n")
        actual = [tuple(line.split(" ")) for line in listing if line]
        expected = [(commit.commit_sha, commit.tree_sha) for commit in plan.commits]
        if actual != expected:
            first_bad = next((i for i, pair in enumerate(zip(actual, expected)) if pair[0] != pair[1]), min(len(actual), len(expected)))
            raise GitHistoryError(
                f"git's history differs from the computed plan at commit {first_bad} "
                f"({len(actual)} commits from git, {len(expected)} planned): "
                f"git {actual[first_bad] if first_bad < len(actual) else None}, "
                f"expected {expected[first_bad] if first_bad < len(expected) else None}"
            )

        # Index from the branch tip: no stat data, so it is reproducible, and `git status`
        # in the extracted zip is clean (it re-hashes files on first use).
        git("--work-tree", str(project_dir), "read-tree", _BRANCH)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _read_project_files(project_dir: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in project_dir.rglob("*"):
        if path.is_file() and not path.is_symlink():
            files[path.relative_to(project_dir).as_posix()] = path.read_bytes()
    return files


def _remove_git_dir(project_dir: Path) -> None:
    def make_writable_and_retry(function, path, _excinfo):  # pack files are read-only on Windows
        os.chmod(path, stat.S_IWRITE)
        function(path)

    git_dir = project_dir / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir, onerror=make_writable_and_retry)


def try_add_git_history(project_dir: Path, manifest: dict) -> bool:
    """The `final_test` entry point: builds the plan from `manifest` and the tree on disk, writes
    and verifies `.git`. Returns whether a `.git` was written. Never raises (§15.5)."""
    if shutil.which("git") is None:
        logger.warning("git is not installed; the zip is built without a .git directory")
        return False
    try:
        plan = build_commit_plan(manifest, _read_project_files(project_dir))
        write_git_history(project_dir, plan)
        return True
    except (CommitPlanError, GitHistoryError, GitObjectError, OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not add git history to the zip (%s); building it without a .git", exc)
        _remove_git_dir(project_dir)
        return False
