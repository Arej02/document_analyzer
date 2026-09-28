"""Worker output protocol v1, shared by the extraction worker and the trusted importer.

Standard library only: this module ships inside the worker image, which must not carry
the web/runner dependency tree.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

OUTPUT_SCHEMA_VERSION = 1
REQUEST_SCHEMA_VERSION = 1
CAPABILITY_SCHEMA_VERSION = 1

NODES_FILE = "nodes.jsonl"
EDGES_FILE = "edges.jsonl"
COVERAGE_FILE = "coverage.json"
MANIFEST_FILE = "manifest.json"
ERROR_FILE = "error.json"
DATA_FILES = (NODES_FILE, EDGES_FILE, COVERAGE_FILE)
ALLOWED_OUTPUT_FILES = frozenset((*DATA_FILES, MANIFEST_FILE, ERROR_FILE))

PROGRESS_FILE = "progress.json"  # written to the scratch mount, never the output mount

# Container mount points. Source is read-only; control is read-only; output and scratch
# are writable, job-scoped bind mounts.
SOURCE_MOUNT = "/source"
CONTROL_MOUNT = "/control"
OUTPUT_MOUNT = "/output"
SCRATCH_MOUNT = "/scratch"
REQUEST_FILE = "request.json"

MAX_RAW_METADATA_BYTES = 8192
MAX_NAME_CHARS = 1024
MAX_PATH_CHARS = 4096
MAX_RELATION_CHARS = 128
MAX_WARNINGS = 200
MAX_UNRESOLVED_SAMPLES = 200

# Worker exit codes (documented contract; anything else is an unexpected failure).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_ANALYSABLE_FILES = 2
EXIT_INVALID_REQUEST = 3
EXIT_OUTPUT_LIMIT = 4


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite number {value!r} is not allowed")


def strict_loads(text: str | bytes) -> Any:
    """Parse JSON rejecting NaN/Infinity and duplicate object keys."""

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=pairs_hook)


def canonical_dumps(value: Any) -> str:
    """Deterministic compact JSON used for hashing and JSONL records."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def id_hash(*parts: str) -> str:
    """Unambiguous hash of an ordered tuple of strings (length-prefixed encoding)."""
    h = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        h.update(len(encoded).to_bytes(8, "big"))
        h.update(encoded)
    return h.hexdigest()


def entity_id(upstream_id: str) -> str:
    """Opaque, URL-safe, deterministic within-snapshot entity ID."""
    return "e" + id_hash("entity", upstream_id)[:32]


def edge_id(
    source_upstream: str,
    relation: str,
    target_upstream: str,
    provenance: dict[str, Any],
) -> str:
    """Direction, relation, endpoints and emitted provenance all contribute to identity."""
    return (
        "r"
        + id_hash("edge", source_upstream, relation, target_upstream, canonical_dumps(provenance))[
            :32
        ]
    )


def finite_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def bounded_json(value: Any, limit: int = MAX_RAW_METADATA_BYTES) -> tuple[str, bool]:
    """Serialise metadata; if it exceeds ``limit`` return an explicit truncation marker."""
    text = canonical_dumps(value)
    if len(text.encode("utf-8")) <= limit:
        return text, False
    marker = {"_truncated": True, "original_bytes": len(text.encode("utf-8"))}
    return canonical_dumps(marker), True
