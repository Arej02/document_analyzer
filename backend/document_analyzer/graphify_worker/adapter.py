"""Graphify adapter: headless extraction into output protocol v1.

Runs only inside the worker container. Uses Graphify's library API
(``graphify.extract.extract(paths, cache_root, root=...)``) on an explicit file list chosen
by the trusted host inventory; repository-controlled ignore files therefore cannot silently
change coverage. The raw per-edge list returned by ``extract`` is the authoritative dataset:
Graphify's graph builder (``build.py``) can collapse parallel relations between the same
endpoints, so it is not used.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import protocol as p
from .capabilities import EXTRACTORS, graphify_version

try:
    import resource
except ImportError:  # Windows: this adapter only actually runs inside the Linux worker container
    resource = None

NODE_CANONICAL_FIELDS = frozenset({"id", "label", "file_type", "source_file", "source_location"})
EDGE_CANONICAL_FIELDS = frozenset(
    {
        "source",
        "target",
        "relation",
        "confidence",
        "confidence_score",
        "source_file",
        "source_location",
        "context",
    }
)
_LINE_RE = re.compile(r"^L(\d+)(?:\s*[-:]\s*L?(\d+))?$")
_PROGRESS_RE = re.compile(r"AST extraction: (\d+)/(\d+)")


class RequestError(Exception):
    pass


class OutputLimitError(Exception):
    pass


class NoAnalysableFiles(Exception):
    pass


@dataclass
class Request:
    job_id: str
    attempt_id: str
    snapshot_id: str
    source_digest: str
    options_hash: str
    capability_digest: str
    files: list[str]
    max_output_bytes: int

    @classmethod
    def load(cls, path: Path) -> Request:
        try:
            data = p.strict_loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise RequestError(f"unreadable request: {exc}") from exc
        if not isinstance(data, dict) or data.get("schema_version") != p.REQUEST_SCHEMA_VERSION:
            raise RequestError("unsupported request schema")
        try:
            req = cls(
                job_id=str(data["job_id"]),
                attempt_id=str(data["attempt_id"]),
                snapshot_id=str(data["snapshot_id"]),
                source_digest=str(data["source_digest"]),
                options_hash=str(data["options_hash"]),
                capability_digest=str(data.get("capability_digest", "")),
                files=list(data["files"]),
                max_output_bytes=int(data["max_output_bytes"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RequestError(f"invalid request field: {exc}") from exc
        if not all(isinstance(f, str) for f in req.files):
            raise RequestError("file list must contain strings")
        return req


def safe_relative(rel: str) -> PurePosixPath:
    """Validate a host-supplied relative path; never trust it to stay inside the root."""
    if not rel or len(rel) > p.MAX_PATH_CHARS or "\x00" in rel or "\\" in rel:
        raise RequestError(f"invalid relative path {rel!r}")
    # Check raw segments: PurePosixPath would silently collapse "a//b" and "a/./b".
    if rel.startswith("/") or any(part in ("", ".", "..") for part in rel.split("/")):
        raise RequestError(f"invalid relative path {rel!r}")
    return PurePosixPath(rel)


class BoundedOutput:
    """Tracks bytes written across all output files and enforces the per-job budget."""

    def __init__(self, root: Path, limit: int) -> None:
        self.root = root
        self.limit = limit
        self.used = 0

    def write_text(self, name: str, lines: list[str] | str) -> dict[str, Any]:
        import hashlib

        target = self.root / name
        tmp = self.root / f".{name}.tmp"
        digest = hashlib.sha256()
        size = 0
        records = 0
        chunks = [lines] if isinstance(lines, str) else lines
        with open(tmp, "wb") as fh:
            for chunk in chunks:
                data = (chunk if isinstance(lines, str) else chunk + "\n").encode("utf-8")
                self.used += len(data)
                if self.used > self.limit:
                    fh.close()
                    tmp.unlink(missing_ok=True)
                    raise OutputLimitError(
                        f"output exceeds {self.limit} bytes while writing {name}"
                    )
                fh.write(data)
                digest.update(data)
                size += len(data)
                records += 0 if isinstance(lines, str) else 1
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        return {"sha256": digest.hexdigest(), "bytes": size, "records": records}


class Progress:
    def __init__(self, scratch: Path | None) -> None:
        self.path = scratch / p.PROGRESS_FILE if scratch else None
        self.started = time.monotonic()

    def update(self, stage: str, done: int | None = None, total: int | None = None) -> None:
        if self.path is None:
            return
        body = {
            "stage": stage,
            "done": done,
            "total": total,
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body), encoding="utf-8")
        os.replace(tmp, self.path)


@dataclass
class FileObservation:
    nodes: int = 0
    error: str | None = None
    skipped: bool = False
    parse_errors: dict[str, Any] | None = None


@dataclass
class Collected:
    observations: dict[str, FileObservation] = field(default_factory=dict)
    calls: int = 0


def _parse_line(location: Any) -> tuple[int | None, int | None]:
    if not isinstance(location, str):
        return None, None
    m = _LINE_RE.match(location.strip())
    if not m:
        return None, None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else None
    return start, end


def _language_for(extractor: Any) -> str:
    if extractor is None:
        return "unsupported"
    info = EXTRACTORS.get(getattr(extractor, "__name__", ""))
    return info.language if info else "unmapped"


def _redact_paths(text: str, source_root: Path) -> str:
    text = text.replace(str(source_root) + "/", "").replace(str(source_root), ".")
    return text.replace(p.SCRATCH_MOUNT, "<scratch>")


def classify_kind(
    node: dict[str, Any], method_targets: set[str], contained: set[str]
) -> tuple[str, str]:
    """Derive a kind only from upstream markers or structural relations, never a label alone.

    Returns (kind, basis). The file rule compares the label with the node's own source path,
    which Graphify sets for the file-level node it creates per extracted file.
    """
    nid = node["id"]
    file_type = node.get("file_type")
    if nid in method_targets:
        return "method", "target of upstream 'method' relation"
    if node.get("_callable_class") is True:
        return "class", "upstream _callable_class marker"
    if node.get("_callable") is True:
        return "function", "upstream _callable marker"
    sf = node.get("source_file")
    if (
        file_type == "code"
        and isinstance(sf, str)
        and node.get("label") == PurePosixPath(sf).name
        and nid not in contained
    ):
        return "file", "file-level node for its source_file"
    if file_type in ("rationale", "document", "concept", "doc_ref"):
        return str(file_type), "upstream file_type"
    return "unknown", "no upstream kind evidence"


def canonicalise(
    result: dict[str, Any], source_root: Path, languages_by_path: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Convert raw Graphify output into canonical protocol records plus diagnostics."""
    raw_nodes = [n for n in result.get("nodes", []) if isinstance(n, dict)]
    raw_edges = [e for e in result.get("edges", []) if isinstance(e, dict)]
    stats: dict[str, Any] = {
        "raw_nodes": len(raw_nodes),
        "raw_edges": len(raw_edges),
        "duplicate_nodes_exact": 0,
        "duplicate_nodes_conflicting": 0,
        "duplicate_edges_exact": 0,
        "unresolved_edges": 0,
        "unresolved_samples": [],
        "invalid_records": 0,
        "oversized_metadata": 0,
        "hyperedges": 0,
    }

    by_id: dict[str, dict[str, Any]] = {}
    for node in raw_nodes:
        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            stats["invalid_records"] += 1
            continue
        if nid in by_id:
            if p.canonical_dumps(by_id[nid]) == p.canonical_dumps(node):
                stats["duplicate_nodes_exact"] += 1
            else:
                stats["duplicate_nodes_conflicting"] += 1
            continue
        by_id[nid] = node

    # Hyperedges (not emitted by the AST path of 0.9.68, handled if present): represented
    # as an entity of kind "hyperedge" plus directed has_member edges to each member.
    for he in result.get("hyperedges", []) or []:
        if not isinstance(he, dict) or not isinstance(he.get("id"), str):
            stats["invalid_records"] += 1
            continue
        stats["hyperedges"] += 1
        members = he.get("members") or he.get("nodes") or he.get("node_ids") or []
        hid = "hyperedge:" + he["id"]
        by_id.setdefault(
            hid,
            {
                "id": hid,
                "label": he.get("label") or he["id"],
                "file_type": "hyperedge",
                "source_file": he.get("source_file"),
                "_hyperedge": {k: v for k, v in he.items() if k not in ("members",)},
            },
        )
        for member in members:
            if isinstance(member, str):
                raw_edges.append(
                    {
                        "source": hid,
                        "target": member,
                        "relation": "has_member",
                        "confidence": he.get("confidence"),
                        "source_file": he.get("source_file"),
                    }
                )

    method_targets = {
        e["target"]
        for e in raw_edges
        if e.get("relation") == "method" and isinstance(e.get("target"), str)
    }
    contained = {
        e["target"]
        for e in raw_edges
        if e.get("relation") in ("contains", "method") and isinstance(e.get("target"), str)
    }

    nodes: list[dict[str, Any]] = []
    for nid, node in by_id.items():
        kind, basis = classify_kind(node, method_targets, contained)
        sf = node.get("source_file")
        rel = sf if isinstance(sf, str) and sf and not sf.startswith("/") else None
        line_start, line_end = _parse_line(node.get("source_location"))
        extra = {k: v for k, v in node.items() if k not in NODE_CANONICAL_FIELDS}
        raw_json, truncated = p.bounded_json(extra)
        stats["oversized_metadata"] += int(truncated)
        label = node.get("label")
        nodes.append(
            {
                "entity_id": p.entity_id(nid),
                "upstream_id": nid,
                "kind": kind,
                "kind_basis": basis,
                "name": (str(label) if label is not None else nid)[: p.MAX_NAME_CHARS],
                "language": languages_by_path.get(rel, None) if rel else None,
                "relative_path": rel,
                "source_location": node.get("source_location")
                if isinstance(node.get("source_location"), str)
                else None,
                "line_start": line_start,
                "line_end": line_end,
                "file_type": node.get("file_type")
                if isinstance(node.get("file_type"), str)
                else None,
                "raw_metadata_json": raw_json,
            }
        )

    edges_by_id: dict[str, dict[str, Any]] = {}
    for edge in raw_edges:
        src, tgt, rel_name = edge.get("source"), edge.get("target"), edge.get("relation")
        if not (isinstance(src, str) and isinstance(tgt, str) and isinstance(rel_name, str)):
            stats["invalid_records"] += 1
            continue
        if src not in by_id or tgt not in by_id:
            stats["unresolved_edges"] += 1
            if len(stats["unresolved_samples"]) < p.MAX_UNRESOLVED_SAMPLES:
                stats["unresolved_samples"].append(
                    {
                        "source": src,
                        "target": tgt,
                        "relation": rel_name[: p.MAX_RELATION_CHARS],
                        "missing": [x for x in (src, tgt) if x not in by_id],
                        "source_file": edge.get("source_file"),
                    }
                )
            continue
        confidence = edge.get("confidence") if isinstance(edge.get("confidence"), str) else None
        provenance = {
            "confidence": confidence,
            "confidence_score": p.finite_or_none(edge.get("confidence_score")),
            "source_file": edge.get("source_file")
            if isinstance(edge.get("source_file"), str)
            else None,
            "source_location": edge.get("source_location")
            if isinstance(edge.get("source_location"), str)
            else None,
            "context": edge.get("context") if isinstance(edge.get("context"), str) else None,
        }
        extra = {k: v for k, v in edge.items() if k not in EDGE_CANONICAL_FIELDS}
        raw_json, truncated = p.bounded_json(extra)
        eid = p.edge_id(src, rel_name, tgt, {**provenance, "raw": raw_json})
        if eid in edges_by_id:
            stats["duplicate_edges_exact"] += 1
            continue
        stats["oversized_metadata"] += int(truncated)
        line, _ = _parse_line(provenance["source_location"])
        edges_by_id[eid] = {
            "edge_id": eid,
            "source_id": p.entity_id(src),
            "target_id": p.entity_id(tgt),
            "source_upstream_id": src,
            "target_upstream_id": tgt,
            "relation": rel_name[: p.MAX_RELATION_CHARS],
            "confidence": confidence,
            "confidence_score": provenance["confidence_score"],
            "relative_path": provenance["source_file"],
            "source_location": provenance["source_location"],
            "line": line,
            "context": provenance["context"],
            "raw_metadata_json": raw_json,
        }

    nodes.sort(key=lambda n: n["entity_id"])
    edges = sorted(edges_by_id.values(), key=lambda e: e["edge_id"])
    return nodes, edges, stats


def run_graphify(
    files: list[Path], source_root: Path, cache_root: Path, progress: Progress
) -> tuple[dict[str, Any], Collected, str]:
    """Call Graphify's library API, observing each per-file result for honest coverage."""
    from graphify import extract as gx

    collected = Collected()
    original = gx._safe_extract_with_xaml_root
    total = len(files)

    def observe(extractor: Callable[..., Any], path: Path, root: Path) -> dict[str, Any]:
        result = original(extractor, path, root)
        rel = os.path.relpath(str(path), str(source_root)).replace(os.sep, "/")
        collected.observations[rel] = FileObservation(
            nodes=len(result.get("nodes") or []),
            error=str(result["error"])[:500] if result.get("error") else None,
            skipped=bool(result.get("skipped")),
            parse_errors=result.get("parse_errors")
            if isinstance(result.get("parse_errors"), dict)
            else None,
        )
        collected.calls += 1
        progress.update("parsing", collected.calls, total)
        return result

    captured = io.StringIO()
    gx._safe_extract_with_xaml_root = observe
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            # parallel=False: the per-file observation hook and real per-file progress rely on
            # the in-process sequential path; the container CPU budget is small anyway.
            result = gx.extract(files, cache_root=cache_root, root=source_root, parallel=False)
    finally:
        gx._safe_extract_with_xaml_root = original
    return result, collected, captured.getvalue()


def file_statuses(
    rel_files: list[str],
    source_root: Path,
    result: dict[str, Any],
    collected: Collected,
) -> tuple[list[dict[str, Any]], dict[str, str], bool]:
    from graphify import extract as gx

    per_file_detail = collected.calls > 0 or not rel_files
    failed = {
        os.path.relpath(f, str(source_root)).replace(os.sep, "/")
        for f in result.get("failed_sources", []) or []
    }
    statuses: list[dict[str, Any]] = []
    languages: dict[str, str] = {}
    for rel in rel_files:
        path = source_root / rel
        extractor = gx._get_extractor(path)
        language = _language_for(extractor)
        languages[rel] = language
        entry: dict[str, Any] = {"path": rel, "language": language}
        obs = collected.observations.get(rel)
        if extractor is None:
            entry["status"] = "unsupported"
            entry["reason"] = (
                "no Graphify extractor accepts this file (e.g. .m without Objective-C)"
            )
        elif obs is not None:
            if obs.error:
                marker = obs.error
                if gx._DEP_MISSING_MARKER in marker or gx._DEP_LOAD_FAILED_MARKER in marker:
                    entry["status"] = "parser_unavailable"
                else:
                    entry["status"] = "parse_failed"
                entry["reason"] = _redact_paths(marker, source_root)
            elif obs.skipped:
                entry["status"] = "declined"
                entry["reason"] = "extractor declined this file by design (e.g. data JSON)"
            elif obs.nodes == 0:
                entry["status"] = "empty"
                entry["reason"] = "parser produced no nodes"
            elif obs.parse_errors:
                entry["status"] = "partial"
                line = obs.parse_errors.get("first_error_line")
                entry["reason"] = (
                    f"syntax errors (first at line {line})" if line else "syntax errors"
                )
            else:
                entry["status"] = "extracted"
            entry["nodes"] = obs.nodes
        else:
            # Observation hook not invoked (upstream internals changed): degrade explicitly.
            entry["status"] = "parse_failed" if rel in failed else "extracted_unobserved"
        statuses.append(entry)
    return statuses, languages, per_file_detail


def analyse(
    source_root: Path,
    output_root: Path,
    scratch_root: Path | None,
    request: Request,
) -> dict[str, Any]:
    started_wall = time.time()
    progress = Progress(scratch_root)
    progress.update("validating_request")
    rel_files = sorted({str(safe_relative(f)) for f in request.files})
    paths: list[Path] = []
    for rel in rel_files:
        full = source_root / rel
        if full.is_symlink() or not full.is_file():
            raise RequestError(f"requested file is not a regular file: {rel}")
        paths.append(full)
    if not paths:
        raise NoAnalysableFiles("no analysable files were supplied")

    cache_root = (scratch_root or Path("/tmp")) / "graphify-cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    progress.update("parsing", 0, len(paths))
    t0 = time.monotonic()
    result, collected, captured = run_graphify(paths, source_root, cache_root, progress)
    parse_seconds = time.monotonic() - t0

    progress.update("canonicalising")
    statuses, languages, per_file_detail = file_statuses(rel_files, source_root, result, collected)
    nodes, edges, stats = canonicalise(result, source_root, languages)
    counts_by_status: dict[str, int] = {}
    by_language: dict[str, dict[str, int]] = {}
    for s in statuses:
        counts_by_status[s["status"]] = counts_by_status.get(s["status"], 0) + 1
        lang = by_language.setdefault(s["language"], {})
        lang[s["status"]] = lang.get(s["status"], 0) + 1
    analysed = sum(
        counts_by_status.get(k, 0) for k in ("extracted", "partial", "extracted_unobserved")
    )
    if analysed == 0:
        raise NoAnalysableFiles(
            "no requested file could be analysed: "
            + ", ".join(f"{k}={v}" for k, v in sorted(counts_by_status.items()))
        )

    warnings = [
        _redact_paths(line.strip(), source_root)[:500]
        for line in captured.splitlines()
        if "warning" in line.lower()
    ][: p.MAX_WARNINGS]

    progress.update("writing_output")
    out = BoundedOutput(output_root, request.max_output_bytes)
    node_lines = [p.canonical_dumps(n) for n in nodes]
    edge_lines = [p.canonical_dumps(e) for e in edges]
    graph_digest = p.sha256_hex("\n".join(node_lines) + "\n--\n" + "\n".join(edge_lines))
    coverage = {
        "schema_version": p.OUTPUT_SCHEMA_VERSION,
        "per_file_detail": per_file_detail,
        "files": statuses,
        "counts_by_status": counts_by_status,
        "by_language": by_language,
        "extraction_stats": stats,
        "graphify_warnings": warnings,
        "failed_sources_upstream": len(result.get("failed_sources", []) or []),
    }
    file_meta = {
        p.NODES_FILE: out.write_text(p.NODES_FILE, node_lines),
        p.EDGES_FILE: out.write_text(p.EDGES_FILE, edge_lines),
        p.COVERAGE_FILE: out.write_text(p.COVERAGE_FILE, p.canonical_dumps(coverage)),
    }
    peak_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource is not None else 0
    manifest = {
        "schema_version": p.OUTPUT_SCHEMA_VERSION,
        "job_id": request.job_id,
        "attempt_id": request.attempt_id,
        "snapshot_id": request.snapshot_id,
        "source_digest": request.source_digest,
        "options_hash": request.options_hash,
        "capability_digest": request.capability_digest,
        "graphify_version": graphify_version(),
        "graph_digest": graph_digest,
        "counts": {
            "files_requested": len(rel_files),
            "files_analysed": analysed,
            "nodes": len(nodes),
            "edges": len(edges),
            **{f"files_{k}": v for k, v in counts_by_status.items()},
        },
        "files": file_meta,
        "measurements": {
            "parse_seconds": round(parse_seconds, 3),
            "total_seconds": round(time.time() - started_wall, 3),
            "peak_rss_kib": peak_rss_kib,
        },
        "started_at": started_wall,
        "finished_at": time.time(),
    }
    # Manifest last, by atomic rename: its presence marks complete output.
    out.write_text(p.MANIFEST_FILE, p.canonical_dumps(manifest))
    progress.update("complete", len(paths), len(paths))
    return manifest


def write_error(output_root: Path, code: str, message: str) -> None:
    try:
        body = p.canonical_dumps({"code": code, "message": message[:2000]})
        tmp = output_root / f".{p.ERROR_FILE}.tmp"
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, output_root / p.ERROR_FILE)
    except OSError:
        pass


def main_analyse() -> int:
    source_root = Path(p.SOURCE_MOUNT)
    output_root = Path(p.OUTPUT_MOUNT)
    scratch = Path(p.SCRATCH_MOUNT)
    scratch_root = scratch if scratch.is_dir() else None
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    try:
        request = Request.load(Path(p.CONTROL_MOUNT) / p.REQUEST_FILE)
        manifest = analyse(source_root, output_root, scratch_root, request)
    except RequestError as exc:
        write_error(output_root, "INVALID_REQUEST", str(exc))
        return p.EXIT_INVALID_REQUEST
    except NoAnalysableFiles as exc:
        write_error(output_root, "NO_SUPPORTED_FILES", str(exc))
        return p.EXIT_NO_ANALYSABLE_FILES
    except OutputLimitError as exc:
        for name in p.DATA_FILES:
            (output_root / name).unlink(missing_ok=True)
        write_error(output_root, "OUTPUT_LIMIT", str(exc))
        return p.EXIT_OUTPUT_LIMIT
    except Exception as exc:  # noqa: BLE001 - report any failure as a redacted summary
        write_error(
            output_root,
            "EXTRACTION_FAILED",
            _redact_paths(f"{type(exc).__name__}: {exc}", source_root),
        )
        return p.EXIT_FAILED
    print(
        json.dumps(
            {
                "event": "complete",
                "nodes": manifest["counts"]["nodes"],
                "edges": manifest["counts"]["edges"],
            }
        )
    )
    return p.EXIT_OK
