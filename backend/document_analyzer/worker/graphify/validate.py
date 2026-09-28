"""Trusted validation of Graphify worker output (protocol v1) before anything is persisted.

Ported from Galvanise's graph/validate.py. The container processed untrusted source, so
its output is untrusted too. Checks: only the expected regular files (no symlinks,
nothing else in the directory), total size, per-file size and checksum against the
manifest, manifest identity against the scheduled request, strict JSON (no NaN, no
duplicate keys), per-record schema, unique IDs, and every edge endpoint present.
"""

import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from document_analyzer.graphify_worker import protocol as p


class GraphifyOutputError(Exception):
    """The container's output is missing, oversized or failed validation."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _check_rel(value: str | None) -> str | None:
    if value is None:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\x00" in value or len(value) > p.MAX_PATH_CHARS:
        raise ValueError("relative_path must be a safe relative path")
    return value


class NodeRecord(_Strict):
    entity_id: str = Field(pattern=r"^e[0-9a-f]{32}$")
    upstream_id: str = Field(min_length=1, max_length=4096)
    kind: str = Field(max_length=64)
    kind_basis: str = Field(max_length=200)
    name: str = Field(max_length=p.MAX_NAME_CHARS)
    language: str | None = Field(default=None, max_length=64)
    relative_path: str | None = None
    source_location: str | None = Field(default=None, max_length=200)
    line_start: int | None = Field(default=None, ge=0)
    line_end: int | None = Field(default=None, ge=0)
    file_type: str | None = Field(default=None, max_length=64)
    raw_metadata_json: str = Field(max_length=p.MAX_RAW_METADATA_BYTES)

    @field_validator("relative_path")
    @classmethod
    def _relative(cls, value: str | None) -> str | None:
        return _check_rel(value)


class EdgeRecord(_Strict):
    edge_id: str = Field(pattern=r"^r[0-9a-f]{32}$")
    source_id: str = Field(pattern=r"^e[0-9a-f]{32}$")
    target_id: str = Field(pattern=r"^e[0-9a-f]{32}$")
    source_upstream_id: str = Field(min_length=1, max_length=4096)
    target_upstream_id: str = Field(min_length=1, max_length=4096)
    relation: str = Field(min_length=1, max_length=p.MAX_RELATION_CHARS)
    confidence: str | None = Field(default=None, max_length=64)
    confidence_score: float | None = None
    relative_path: str | None = None
    source_location: str | None = Field(default=None, max_length=200)
    line: int | None = Field(default=None, ge=0)
    context: str | None = Field(default=None, max_length=200)
    raw_metadata_json: str = Field(max_length=p.MAX_RAW_METADATA_BYTES)

    @field_validator("relative_path")
    @classmethod
    def _relative(cls, value: str | None) -> str | None:
        return _check_rel(value)


class FileMeta(_Strict):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)
    records: int = Field(ge=0)


class OutputManifest(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int
    job_id: str
    attempt_id: str
    snapshot_id: str
    source_digest: str
    options_hash: str
    capability_digest: str
    graphify_version: str
    graph_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    counts: dict[str, int]
    files: dict[str, FileMeta]
    measurements: dict[str, float | int] = {}


@dataclass(frozen=True)
class Expected:
    job_id: str
    attempt_id: str
    snapshot_id: str
    source_digest: str
    options_hash: str
    max_output_bytes: int


@dataclass
class ValidatedOutput:
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    coverage: dict[str, Any]
    manifest: OutputManifest


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with open(path, "rb") as fh:
        for raw in fh:
            record = p.strict_loads(raw)
            if not isinstance(record, dict):
                raise ValueError("JSONL record is not an object")
            yield record


def _check_directory(root: Path, max_bytes: int) -> dict[str, int]:
    st = os.lstat(root)
    if not stat.S_ISDIR(st.st_mode):
        raise GraphifyOutputError("output root is not a directory")
    sizes: dict[str, int] = {}
    total = 0
    for entry in os.scandir(root):
        est = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(est.st_mode):
            raise GraphifyOutputError(f"symlink in output: {entry.name[:100]}")
        if not stat.S_ISREG(est.st_mode):
            raise GraphifyOutputError(f"unexpected non-file in output: {entry.name[:100]}")
        if entry.name not in p.ALLOWED_OUTPUT_FILES:
            raise GraphifyOutputError(f"unexpected file in output: {entry.name[:100]}")
        sizes[entry.name] = est.st_size
        total += est.st_size
    if total > max_bytes:
        raise GraphifyOutputError(f"output is {total} bytes, over the {max_bytes} byte limit")
    return sizes


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_output(root: Path, expected: Expected) -> ValidatedOutput:
    sizes = _check_directory(root, expected.max_output_bytes)
    if p.MANIFEST_FILE not in sizes:
        raise GraphifyOutputError("manifest missing: extraction did not complete")
    if sizes[p.MANIFEST_FILE] > 1 << 20:
        raise GraphifyOutputError("manifest too large")
    try:
        manifest = OutputManifest.model_validate(p.strict_loads((root / p.MANIFEST_FILE).read_bytes()))
    except (ValueError, ValidationError) as exc:
        raise GraphifyOutputError(f"manifest is not valid: {str(exc)[:500]}") from exc
    if manifest.schema_version != p.OUTPUT_SCHEMA_VERSION:
        raise GraphifyOutputError(f"unsupported output schema version {manifest.schema_version}")
    for field_name in ("job_id", "attempt_id", "snapshot_id", "source_digest", "options_hash"):
        if getattr(manifest, field_name) != getattr(expected, field_name):
            raise GraphifyOutputError(f"manifest {field_name} does not match the scheduled request")
    if set(manifest.files) != set(p.DATA_FILES):
        raise GraphifyOutputError(f"manifest lists unexpected data files: {sorted(manifest.files)}")
    for name, meta in manifest.files.items():
        if sizes.get(name) != meta.bytes:
            raise GraphifyOutputError(f"file size mismatch: {name}")
        if _sha256(root / name) != meta.sha256:
            raise GraphifyOutputError(f"checksum mismatch: {name}")

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    try:
        node_ids: set[str] = set()
        for record in _iter_jsonl(root / p.NODES_FILE):
            node = NodeRecord.model_validate(record)
            if node.entity_id in node_ids:
                raise GraphifyOutputError(f"duplicate entity id {node.entity_id}")
            if node.entity_id != p.entity_id(node.upstream_id):
                raise GraphifyOutputError(f"entity id does not match upstream id: {node.entity_id}")
            node_ids.add(node.entity_id)
            nodes.append(node.model_dump())
        edge_ids: set[str] = set()
        for record in _iter_jsonl(root / p.EDGES_FILE):
            edge = EdgeRecord.model_validate(record)
            if edge.edge_id in edge_ids:
                raise GraphifyOutputError(f"duplicate edge id {edge.edge_id}")
            if edge.source_id not in node_ids or edge.target_id not in node_ids:
                raise GraphifyOutputError(f"edge endpoint missing: {edge.edge_id}")
            if edge.confidence_score is not None and not (0.0 <= edge.confidence_score <= 1.0):
                raise GraphifyOutputError(f"confidence score out of range: {edge.edge_id}")
            edge_ids.add(edge.edge_id)
            edges.append(edge.model_dump())
        coverage = p.strict_loads((root / p.COVERAGE_FILE).read_bytes())
    except GraphifyOutputError:
        raise
    except (ValueError, ValidationError, UnicodeDecodeError) as exc:
        raise GraphifyOutputError(f"record failed validation: {str(exc)[:500]}") from exc
    if not isinstance(coverage, dict):
        raise GraphifyOutputError("coverage is not an object")
    if manifest.files[p.NODES_FILE].records != len(nodes) or manifest.files[p.EDGES_FILE].records != len(edges):
        raise GraphifyOutputError("record count mismatch")
    if manifest.counts.get("nodes") != len(nodes) or manifest.counts.get("edges") != len(edges):
        raise GraphifyOutputError("manifest counts do not match records")
    return ValidatedOutput(nodes=nodes, edges=edges, coverage=coverage, manifest=manifest)
