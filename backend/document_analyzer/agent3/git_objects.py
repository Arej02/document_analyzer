"""Pure git object builder — design_agent3.md §15.14.

Computes the exact SHA-1 of git blobs, trees and commits from plain data, using
nothing but `hashlib`. It is the single source of truth for every object SHA in
the GitHub PR delivery feature: the zip's `.git` (`worker/git_history.py`) is
written from these objects and git's reported SHAs are asserted equal to ours,
and every GitHub `POST /git/trees` / `POST /git/commits` response is verified
against them. No DB, network, filesystem or `git` binary is touched here.

Scope is deliberately narrow: regular files (mode `100644`) for everything this
project generates, plus caller-supplied `FileRef`s with any mode so a non-empty
target repo's existing tree (executables, symlinks, submodules) can be carried
through unchanged. Commits are unsigned, have a fixed `+0000` zone and no
encoding header.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping

REGULAR_FILE_MODE = "100644"
_TREE_MODE = "40000"  # git writes a tree's own mode without a leading zero
_NAME_FORBIDDEN = re.compile(r"[<>\n\r\x00]")


class GitObjectError(ValueError):
    """Bad input to the object builder (an unusable path, identity or message)."""


@dataclass(frozen=True)
class FileRef:
    """A tree entry that is not itself a directory: a mode and the SHA-1 it points at."""

    mode: str
    sha: str


@dataclass(frozen=True)
class Identity:
    name: str
    email: str
    # Seconds since the epoch, always rendered with a +0000 zone.
    timestamp: int

    def render(self) -> str:
        for field_name, value in (("name", self.name), ("email", self.email)):
            if not value or _NAME_FORBIDDEN.search(value):
                raise GitObjectError(f"identity {field_name} {value!r} is empty or contains <, >, a newline or NUL")
        if self.timestamp < 0:
            raise GitObjectError("identity timestamp must be non-negative")
        return f"{self.name} <{self.email}> {self.timestamp} +0000"


@dataclass(frozen=True)
class TreeBuild:
    """The root tree's SHA plus every tree object needed to write it: `{sha: raw content}`.
    Blob content is not stored here (the caller already has it)."""

    root_sha: str
    trees: dict[str, bytes]


def hash_object(kind: str, content: bytes) -> str:
    header = f"{kind} {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def blob_sha(data: bytes) -> str:
    return hash_object("blob", data)


def file_refs(files: Mapping[str, bytes]) -> dict[str, FileRef]:
    """Regular-file refs (mode 100644) for a `{path: bytes}` mapping."""
    return {path: FileRef(REGULAR_FILE_MODE, blob_sha(data)) for path, data in files.items()}


def _validate_path(path: str) -> list[str]:
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", "..") or "\0" in part or "\\" in part:
            raise GitObjectError(f"unusable path {path!r}")
    return parts


def _entry_sort_key(name: str, is_tree: bool) -> bytes:
    # Git orders tree entries by name bytes, comparing a directory as if its name ended in "/".
    return name.encode("utf-8") + (b"/" if is_tree else b"")


def _build(node: dict, trees: dict[str, bytes]) -> str:
    entries: list[tuple[bytes, bytes]] = []
    for name, child in node.items():
        is_tree = isinstance(child, dict)
        if is_tree:
            child_sha = _build(child, trees)
            mode = _TREE_MODE
        else:
            child_sha, mode = child.sha, child.mode
        raw = f"{mode} {name}".encode("utf-8") + b"\0" + bytes.fromhex(child_sha)
        entries.append((_entry_sort_key(name, is_tree), raw))
    entries.sort(key=lambda entry: entry[0])
    content = b"".join(raw for _, raw in entries)
    sha = hash_object("tree", content)
    trees[sha] = content
    return sha


def build_tree(entries: Mapping[str, FileRef]) -> TreeBuild:
    """The tree object for a flat `{path: FileRef}` listing (like `git ls-tree -r`)."""
    root: dict = {}
    for path, ref in entries.items():
        parts = _validate_path(path)
        node = root
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise GitObjectError(f"{path!r}: a parent component is already a file")
            node = child
        if parts[-1] in node:
            raise GitObjectError(f"{path!r} collides with another entry")
        node[parts[-1]] = ref
    trees: dict[str, bytes] = {}
    root_sha = _build(root, trees)
    return TreeBuild(root_sha=root_sha, trees=trees)


def normalize_message(message: str) -> str:
    """Exactly one trailing newline, no trailing blank lines. `git commit-tree` stores a
    message as given, so we normalize it ourselves and send the same bytes everywhere."""
    if "\0" in message:
        raise GitObjectError("commit message contains NUL")
    normalized = message.rstrip("\n")
    if not normalized.strip():
        raise GitObjectError("commit message is empty")
    return normalized + "\n"


def commit_content(
    tree_sha: str, parents: list[str], author: Identity, committer: Identity, message: str
) -> bytes:
    lines = [f"tree {tree_sha}"]
    lines += [f"parent {parent}" for parent in parents]
    lines += [f"author {author.render()}", f"committer {committer.render()}", ""]
    return ("\n".join(lines) + "\n" + normalize_message(message)).encode("utf-8")


def commit_sha(tree_sha: str, parents: list[str], author: Identity, committer: Identity, message: str) -> str:
    return hash_object("commit", commit_content(tree_sha, parents, author, committer, message))
