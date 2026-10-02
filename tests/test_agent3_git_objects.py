"""`agent3/git_objects.py` against real git — design_agent3.md §15.14.

The builder is the single source of truth for every SHA in GitHub delivery, so it is
checked two ways: a few well-known vectors, and a cross-check of every object kind against
the `git` binary's own plumbing over awkward inputs (a name that sorts differently as a
directory, CRLF bytes, executable and symlink modes, messages with and without trailing
newlines). The cross-check skips when `git` is not installed; the vectors always run.
"""

import os
import shutil
import subprocess

import pytest

from document_analyzer.agent3.git_objects import (
    FileRef,
    GitObjectError,
    Identity,
    blob_sha,
    build_tree,
    commit_sha,
    file_refs,
    normalize_message,
)

EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
HELLO_BLOB = "ce013625030ba8dba906f756967f9e9ca394464a"  # printf 'hello\n' | git hash-object --stdin

IDENTITY = Identity("Galvanise Agent 3", "noreply@galvanise.invalid", 946684801)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="requires the git binary")


def test_known_blob_vectors():
    assert blob_sha(b"") == EMPTY_BLOB
    assert blob_sha(b"hello\n") == HELLO_BLOB


def test_empty_tree_vector():
    assert build_tree({}).root_sha == EMPTY_TREE


def test_message_normalisation():
    assert normalize_message("msg") == "msg\n"
    assert normalize_message("msg\n") == "msg\n"
    assert normalize_message("subject\n\nbody\n\n\n") == "subject\n\nbody\n"
    with pytest.raises(GitObjectError):
        normalize_message("  \n")
    with pytest.raises(GitObjectError):
        normalize_message("a\0b")


@pytest.mark.parametrize("path", ["", "/abs", "a//b", "a/./b", "a/../b", "a\\b", "a/b/"])
def test_unusable_paths_are_refused(path):
    with pytest.raises(GitObjectError):
        build_tree({path: FileRef("100644", EMPTY_BLOB)})


def test_a_file_cannot_also_be_a_directory():
    with pytest.raises(GitObjectError):
        build_tree({"a": FileRef("100644", EMPTY_BLOB), "a/b": FileRef("100644", EMPTY_BLOB)})


@pytest.mark.parametrize("name,email", [("", "a@b"), ("a", ""), ("a<b", "a@b"), ("a", "a>b"), ("a\nb", "a@b")])
def test_bad_identities_are_refused(name, email):
    with pytest.raises(GitObjectError):
        Identity(name, email, 1).render()


# --------------------------------------------------------------------------- #
# Cross-check against the real git binary
# --------------------------------------------------------------------------- #


def _git(repo, *args, stdin: bytes | None = None, env_extra: dict | None = None) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        **(env_extra or {}),
    }
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", *args],
        cwd=repo,
        input=stdin,
        capture_output=True,
        env=env,
        check=True,
    )
    return result.stdout.decode("utf-8").strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    return tmp_path


def _git_tree_sha(repo, entries: dict[str, tuple[str, bytes]]) -> str:
    """`entries` is `{path: (mode, bytes)}`; a symlink's bytes are its target."""
    for path, (mode, data) in entries.items():
        sha = _git(repo, "hash-object", "-w", "--stdin", stdin=data)
        _git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{sha},{path}")
    return _git(repo, "write-tree")


AWKWARD = {
    "a.b": ("100644", b"dot\n"),
    "a-b": ("100644", b"dash\n"),
    "a/x": ("100644", b"inside dir a\n"),
    "a0": ("100644", b"zero\n"),
    "crlf.txt": ("100644", b"line one\r\nline two\r\n"),
    "bin/run.sh": ("100755", b"#!/bin/sh\necho hi\n"),
    "link": ("120000", b"bin/run.sh"),
    "app/units/01_01_cart/x.py": ("100644", b"x = 1\n"),
    "z/deep/er/file": ("100644", b""),
    "ünï/çode.txt": ("100644", "utf8 name\n".encode("utf-8")),
}


@needs_git
def test_tree_sha_matches_git_for_awkward_names_and_modes(repo):
    ours = build_tree({path: FileRef(mode, blob_sha(data)) for path, (mode, data) in AWKWARD.items()}).root_sha

    assert ours == _git_tree_sha(repo, AWKWARD)


@needs_git
def test_blob_sha_matches_git_for_crlf_and_binary_bytes(repo):
    for data in (b"a\r\nb\r\n", b"\x00\x01\xff\xfe", b"", b"no newline"):
        assert blob_sha(data) == _git(repo, "hash-object", "--stdin", stdin=data)


@needs_git
@pytest.mark.parametrize("message", ["one line", "one line\n", "subject\n\nbody line\n\n\n"])
def test_commit_sha_matches_git_commit_tree(repo, message):
    tree = _git_tree_sha(repo, {"README.md": ("100644", b"hi\n")})
    env = {
        "GIT_AUTHOR_NAME": IDENTITY.name,
        "GIT_AUTHOR_EMAIL": IDENTITY.email,
        "GIT_AUTHOR_DATE": f"{IDENTITY.timestamp} +0000",
        "GIT_COMMITTER_NAME": IDENTITY.name,
        "GIT_COMMITTER_EMAIL": IDENTITY.email,
        "GIT_COMMITTER_DATE": f"{IDENTITY.timestamp} +0000",
    }

    root_git = _git(repo, "commit-tree", tree, stdin=normalize_message(message).encode("utf-8"), env_extra=env)
    child_git = _git(
        repo, "commit-tree", tree, "-p", root_git, stdin=normalize_message(message).encode("utf-8"), env_extra=env
    )

    root_ours = commit_sha(tree, [], IDENTITY, IDENTITY, message)
    assert root_ours == root_git
    assert commit_sha(tree, [root_ours], IDENTITY, IDENTITY, message) == child_git


def test_build_tree_is_independent_of_input_order():
    refs = file_refs({"b": b"1", "a/c": b"2", "a.b": b"3"})
    reversed_refs = dict(reversed(list(refs.items())))

    assert build_tree(refs).root_sha == build_tree(reversed_refs).root_sha
