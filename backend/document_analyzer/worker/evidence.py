"""Worker step: persisted source evidence — design.md §2 `source_chunks`, ported from
Galvanise's evidence/chunking.py.

Agent 1 used to keep only structural facts and LLM summaries, so a summary could only be
checked against the facts it was generated from, and search could only find text an
LLM had already paraphrased. This step keeps bounded, secret-redacted excerpts of the
real source, taken from the post-boilerplate repo while it's still on disk:

1. Entity ranges. A range is *verified* when the extractor reported an end line
   (nikic/php-parser always does; Graphify sometimes does) or, for Python, when the
   standard-library parser finds a def/class of that name starting on that line
   (`ast.parse` only parses; nothing runs). Outermost verified ranges up to
   ENTITY_MAX_LINES become `entity_range` chunks.
2. Every remaining line is covered by overlapping `source_window` chunks.
3. Chunks are associated with entities only through file path and line numbers:
   `entity_range` (the chunk is the entity's definition) or `starts_in` (the entity's
   start line falls inside it). A display name never associates anything.

Per entity, `excerpt_for` returns the text summarize.py and ground.py now receive beside
raw_facts, and its hash feeds `content_hash` (persist.py), so a body change that leaves
the call list unchanged still invalidates the summary cache.

Chunks are embedded with the same pinned local model as summaries (embed.py), so code
search no longer depends on summary quality; nothing here goes to Azure OpenAI.
"""

import ast
import hashlib
import os
import stat
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from supabase import Client

from document_analyzer.supabase_client import insert_many_scoped
from document_analyzer.worker.extract import ExtractedEntity
from document_analyzer.worker.secret_scan import Finding, log_findings, redact_text

STRATEGY_VERSION = 1
WINDOW_LINES = 60
WINDOW_OVERLAP = 10
ENTITY_MAX_LINES = 150
MAX_LINE_CHARS = 2000
CHUNK_CHAR_LIMIT = 12_000
EXCERPT_WINDOW_LINES = 60
EXCERPT_MAX_CHARS = 8_000
CHARS_PER_TOKEN = 3.0  # conservative for source code: over-estimates tokens, never under
INSERT_BATCH = 500

MAX_FILE_BYTES = int(os.environ.get("AGENT1_EVIDENCE_MAX_FILE_BYTES", 1024 * 1024))
MAX_CHUNKS = int(os.environ.get("AGENT1_EVIDENCE_MAX_CHUNKS", 50_000))

EntityKey = tuple[int, str]  # (index of the ExtractionResult, entity local_id)


def estimate_tokens(text: str) -> int:
    return max(1, -(-len(text) // int(CHARS_PER_TOKEN)))


@dataclass
class FileLines:
    lines: list[str]
    omissions: list[dict[str, int]] = field(default_factory=list)


def read_source_lines(path: Path, max_bytes: int) -> tuple[FileLines | None, str | None]:
    """Read one file without following symlinks; returns (lines, skip_reason)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        return None, f"unreadable ({type(exc).__name__})"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not a regular file"
        if st.st_size > max_bytes:
            return None, f"larger than the {max_bytes} byte evidence limit"
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(max_bytes + 1)
    finally:
        os.close(fd)
    if not data.strip():
        return None, "empty"
    if b"\x00" in data[:8192]:
        return None, "binary content"
    text = data.decode("utf-8", "replace").lstrip("﻿")
    raw = text.split("\n")
    if raw and raw[-1] == "":
        raw.pop()
    lines, omissions = [], []
    for number, line in enumerate(raw, start=1):
        line = line.removesuffix("\r")
        if len(line) > MAX_LINE_CHARS:
            omitted = len(line) - MAX_LINE_CHARS
            omissions.append({"line": number, "omitted_chars": omitted})
            line = line[:MAX_LINE_CHARS] + f" …[{omitted} characters omitted]"
        lines.append(line)
    return FileLines(lines, omissions), None


def python_ranges(text: str) -> dict[tuple[int, str], int]:
    """(start line, name) -> end line for every def/class, from the stdlib parser.

    Recorded both at the def/class keyword and at the first decorator, since either may
    be what an extractor reports. Parsing never executes the source."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return {}
    out: dict[tuple[int, str], int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            end = getattr(node, "end_lineno", None)
            if end is None:
                continue
            out[(node.lineno, node.name)] = end
            if node.decorator_list:
                out[(min(d.lineno for d in node.decorator_list), node.name)] = end
    return out


@dataclass
class _Range:
    start: int
    end: int
    keys: list[EntityKey]


def _select_ranges(ranges: list[_Range]) -> list[_Range]:
    """Outermost non-overlapping ranges no longer than ENTITY_MAX_LINES."""
    kept: list[_Range] = []
    for r in sorted(ranges, key=lambda r: (r.start, -r.end)):
        if r.end - r.start + 1 > ENTITY_MAX_LINES:
            continue
        if kept and r.start <= kept[-1].end:
            if (r.start, r.end) == (kept[-1].start, kept[-1].end):
                kept[-1].keys.extend(k for k in r.keys if k not in kept[-1].keys)
            continue
        kept.append(_Range(r.start, r.end, list(r.keys)))
    return kept


def _windows(start: int, end: int, size: int, overlap: int) -> list[tuple[int, int]]:
    out, s = [], start
    while s <= end:
        e = min(s + size - 1, end)
        out.append((s, e))
        if e == end:
            break
        s = e - overlap + 1
    return out


def _split_to_fit(lines: list[str], start: int, end: int, limit: int) -> list[tuple[int, int]]:
    if sum(len(line) + 1 for line in lines[start - 1 : end]) <= limit or start == end:
        return [(start, end)]
    mid = (start + end) // 2
    return _split_to_fit(lines, start, mid, limit) + _split_to_fit(lines, mid + 1, end, limit)


def _line(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


@dataclass
class Chunk:
    file_path: str
    line_start: int
    line_end: int
    strategy: str
    content: str  # already redacted
    content_hash: str
    redactions: list[str]
    entities: list[tuple[EntityKey, str]]  # (entity key, association)


@dataclass
class EvidenceBuild:
    chunks: list[Chunk]
    excerpts: dict[EntityKey, dict[str, Any]]
    report: dict[str, Any]

    def excerpt_hash(self, key: EntityKey) -> str | None:
        excerpt = self.excerpts.get(key)
        return excerpt["sha256"] if excerpt else None


def build_evidence(repo_path: Path, entities: Iterable[tuple[EntityKey, ExtractedEntity]]) -> EvidenceBuild:
    """Chunks every file of the languages extraction touched and associates chunks with
    entities. Pure over the files on disk: no database, no LLM."""
    by_file: dict[str, list[tuple[EntityKey, ExtractedEntity]]] = defaultdict(list)
    for key, entity in entities:
        by_file[entity.file_path].append((key, entity))

    suffixes = {PurePosixPath(path).suffix.lower() for path in by_file if PurePosixPath(path).suffix}
    files = set(by_file)
    for path in repo_path.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in suffixes:
            files.add(path.relative_to(repo_path).as_posix())

    chunks: list[Chunk] = []
    excerpts: dict[EntityKey, dict[str, Any]] = {}
    skipped: list[dict[str, str]] = []
    stats: Counter[str] = Counter()
    basis: Counter[str] = Counter()
    truncated = False
    for rel in sorted(files):
        if len(chunks) >= MAX_CHUNKS:
            truncated = True
            break
        rel_path = PurePosixPath(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            skipped.append({"path": rel, "reason": "unsafe path"})
            continue
        file_lines, reason = read_source_lines(repo_path / rel, MAX_FILE_BYTES)
        if file_lines is None:
            if reason != "empty":
                skipped.append({"path": rel, "reason": reason or "unreadable"})
            continue
        stats["files_chunked"] += 1
        lines, n = file_lines.lines, len(file_lines.lines)
        members = by_file.get(rel, [])
        py = python_ranges("\n".join(lines)) if rel.endswith((".py", ".pyw")) and not file_lines.omissions else {}

        ranges: list[_Range] = []
        for key, entity in members:
            start, end = _line(entity.raw_facts.get("line")), _line(entity.raw_facts.get("end_line"))
            if start is None or start > n:
                basis["no_location"] += 1
                continue
            if end and end >= start:
                ranges.append(_Range(start, min(end, n), [key]))
                basis["extractor_range"] += 1
                continue
            verified = py.get((start, entity.name))
            if verified and verified >= start:
                ranges.append(_Range(start, min(verified, n), [key]))
                basis["python_ast"] += 1
            else:
                basis["start_line_only"] += 1
        selected = _select_ranges(ranges)

        pieces: list[tuple[int, int, str]] = []
        covered = [False] * (n + 2)
        for r in selected:
            parts = _split_to_fit(lines, r.start, r.end, CHUNK_CHAR_LIMIT)
            strategy = "entity_range" if len(parts) == 1 else "entity_range_part"
            pieces.extend((s, e, strategy) for s, e in parts)
            for i in range(r.start, r.end + 1):
                covered[i] = True
        gap_start = None
        for i in range(1, n + 2):
            if i <= n and not covered[i]:
                gap_start = gap_start or i
                continue
            if gap_start is not None:
                for ws, we in _windows(gap_start, i - 1, WINDOW_LINES, WINDOW_OVERLAP):
                    if any(line.strip() for line in lines[ws - 1 : we]):
                        pieces.extend((s, e, "source_window") for s, e in _split_to_fit(lines, ws, we, CHUNK_CHAR_LIMIT))
                gap_start = None

        owners = {(r.start, r.end): r.keys for r in selected}
        for s, e, strategy in sorted(set(pieces)):
            content, reasons = redact_text("\n".join(lines[s - 1 : e]))
            assoc: dict[EntityKey, str] = {}
            if strategy == "entity_range":
                for key in owners.get((s, e), []):
                    assoc[key] = "entity_range"
            for key, entity in members:
                start = _line(entity.raw_facts.get("line"))
                if start and s <= start <= e:
                    assoc.setdefault(key, "starts_in")
            chunks.append(
                Chunk(
                    file_path=rel,
                    line_start=s,
                    line_end=e,
                    strategy=strategy,
                    content=content,
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    redactions=reasons,
                    entities=list(assoc.items()),
                )
            )
            stats[f"chunks_{strategy}"] += 1

        range_of = {k: (r.start, r.end) for r in selected for k in r.keys}
        for key, entity in members:
            start = _line(entity.raw_facts.get("line"))
            if start is None or start > n:
                continue
            s, e = range_of.get(key, (start, min(n, start + EXCERPT_WINDOW_LINES - 1)))
            text, _reasons = redact_text("\n".join(lines[s - 1 : e]))
            complete = key in range_of
            if len(text) > EXCERPT_MAX_CHARS:
                text, complete = text[:EXCERPT_MAX_CHARS] + "\n…[excerpt truncated]", False
            excerpts[key] = {
                "file_path": rel,
                "line_start": s,
                "line_end": e,
                "complete_definition": complete,
                "text": text,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }

    report = {
        "strategy_version": STRATEGY_VERSION,
        "files_considered": len(files),
        "files_chunked": stats["files_chunked"],
        "files_skipped": skipped[:200],
        "files_skipped_count": len(skipped),
        "chunks": len(chunks),
        "chunks_by_strategy": {k.removeprefix("chunks_"): v for k, v in stats.items() if k.startswith("chunks_")},
        "entity_range_basis": dict(basis),
        "entities_with_excerpt": len(excerpts),
        "redacted_chunks": sum(1 for c in chunks if c.redactions),
        "truncated_at_max_chunks": truncated,
    }
    return EvidenceBuild(chunks=chunks, excerpts=excerpts, report=report)


def embedding_text(chunk: Chunk, names: list[str]) -> str:
    """The pinned model reads only ~256 word pieces, so the identifying header goes first."""
    header = f"{chunk.file_path} lines {chunk.line_start}-{chunk.line_end}"
    if names:
        header += " | " + ", ".join(names[:10])
    return f"{header}\n{chunk.content}"


def persist_evidence(
    client: Client,
    job_id: str,
    org_id: str,
    build: EvidenceBuild,
    entity_ids: dict[EntityKey, str],
    names: dict[EntityKey, str],
    embed_many: Callable[[list[str]], list[list[float]]] | None = None,
) -> int:
    """Inserts `source_chunks` (with embeddings) and `source_chunk_entities` in batches.
    Returns the number of chunks written. A redaction inside a chunk is logged to
    `secret_findings` against the chunk's first associated entity (the table requires one);
    the chunk row records its own redaction reasons either way."""
    if embed_many is None:
        from document_analyzer.worker.embed import embed_texts as embed_many
    written = 0
    findings: dict[str, list[Finding]] = defaultdict(list)
    for offset in range(0, len(build.chunks), INSERT_BATCH):
        batch = build.chunks[offset : offset + INSERT_BATCH]
        vectors = embed_many(
            [embedding_text(c, [names[k] for k, _ in c.entities if k in names]) for c in batch]
        )
        rows = [
            {
                "job_id": job_id,
                "file_path": c.file_path,
                "line_start": c.line_start,
                "line_end": c.line_end,
                "strategy": c.strategy,
                "content": c.content,
                "content_hash": c.content_hash,
                "token_estimate": estimate_tokens(c.content),
                "redactions": c.redactions,
                "embedding": vector,
            }
            for c, vector in zip(batch, vectors, strict=True)
        ]
        inserted = insert_many_scoped(client, "source_chunks", org_id, rows).data
        links = []
        for c, row in zip(batch, inserted, strict=True):
            for key, association in c.entities:
                entity_id = entity_ids.get(key)
                if entity_id is not None:
                    links.append(
                        {"job_id": job_id, "chunk_id": row["id"], "entity_id": entity_id, "association": association}
                    )
            owner = next((entity_ids[k] for k, _ in c.entities if k in entity_ids), None)
            if c.redactions and owner is not None:
                findings[owner].extend(
                    Finding(field_path=f"source:{c.file_path}:L{c.line_start}-L{c.line_end}", reason=r)
                    for r in sorted(set(c.redactions))
                )
        if links:
            insert_many_scoped(client, "source_chunk_entities", org_id, links)
        written += len(inserted)
    if findings:
        log_findings(client, job_id, org_id, dict(findings))
    return written
