"""Content leak gate (design_report.md §8.1, gate 2): fail closed over the final `report_json`.

Free-text fields (the executive summary, subsystem summaries, risk explanations) are the
only places source text can hide, so each is checked for:
  - a secret (the Agent 1 scanner, `redact_text`) or a `[REDACTED:` marker,
  - n-gram overlap with the job's `source_chunks` (verbatim source),
  - code shape (fences, long backticked runs, brace/symbol-heavy runs).
An offending field is replaced by a fixed sentence and listed in the report's limitations;
it is never dropped silently. Every other string in the report (names, paths, labels) gets
the secret and marker checks only: a hit there can't be replaced without changing what the
report says, so the gate raises `LeakGateError` and the report is not published.

Source text is only ever held as hashed n-grams, built in memory by `load_source_index`
from explicit-column, org- and job-scoped reads. Nothing here logs or returns a matched
value: results carry field paths and reason names only. `scan_text` is the same check for
the rendered HTML and PDF text in Phase 5.
"""

import copy
import re
from html.parser import HTMLParser
from dataclasses import dataclass
from typing import Any, Iterable

from supabase import Client

from document_analyzer.worker.secret_scan import redact_text

NGRAM = 6  # consecutive words; punctuation is ignored, so tidying quotes or semicolons does not evade the check
MIN_DISTINCT_WORDS = 3  # `0 0 0 0 0 0` or `a a a a a a` is not evidence of copied source
WITHHELD_SUMMARY = "Summary withheld: contained source text"
WITHHELD_EXEC_NOTE = "The executive summary was withheld by a content check."
PAGE_SIZE = 500

_WORD_RE = re.compile(r"\w+")
_BACKTICK_SPAN_RE = re.compile(r"`([^`]+)`")  # one properly paired inline span
BACKTICK_SPAN_MAX = 30
_BRACE_RUN_RE = re.compile(r"\{[^{}]{20,}\}")
_CODE_MARKERS = ("```", "<?php", "?>")
_SYMBOLS = frozenset("{}();$<>=[]")


class LeakGateError(Exception):
    """A secret or redaction marker was found outside a replaceable free-text field."""


@dataclass(frozen=True)
class Hit:
    path: str
    reasons: tuple[str, ...]


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _windows(tokens: list[str]) -> Iterable[tuple[str, ...]]:
    for i in range(len(tokens) - NGRAM + 1):
        window = tuple(tokens[i : i + NGRAM])
        if len(set(window)) >= MIN_DISTINCT_WORDS:
            yield window


class SourceIndex:
    """Hashed n-grams of source text. Holds no source text."""

    def __init__(self) -> None:
        self._grams: set[int] = set()

    def add(self, text: str) -> None:
        self._grams.update(hash(w) for w in _windows(_tokens(text)))

    def overlaps(self, text: str) -> bool:
        return any(hash(w) in self._grams for w in _windows(_tokens(text)))


def build_source_index(texts: Iterable[str]) -> SourceIndex:
    index = SourceIndex()
    for text in texts:
        index.add(text)
    return index


def load_source_index(client: Client, org_id: str, job_id: str) -> SourceIndex:
    """Pages `source_chunks.content` for one job into a `SourceIndex`. The one place the
    report worker reads chunk content: in memory, never persisted or logged."""
    index, start = SourceIndex(), 0
    while True:
        page = (
            client.table("source_chunks")
            .select("id, content")
            .eq("org_id", org_id)
            .eq("job_id", job_id)
            .order("id")
            .range(start, start + PAGE_SIZE - 1)
            .execute()
            .data
            or []
        )
        for row in page:
            index.add(row["content"])
        if len(page) < PAGE_SIZE:
            return index
        start += PAGE_SIZE


def _code_shaped(text: str) -> bool:
    if any(marker in text for marker in _CODE_MARKERS) or any(len(span) >= BACKTICK_SPAN_MAX for span in _BACKTICK_SPAN_RE.findall(text)) or _BRACE_RUN_RE.search(text):
        return True
    return len(text) >= 40 and sum(1 for c in text if c in _SYMBOLS) / len(text) > 0.15


def scan_secrets(text: str) -> list[str]:
    """Reason names for secrets and redaction markers in `text` (never the values)."""
    reasons = sorted(set(redact_text(text)[1]))
    if "[REDACTED:" in text:
        reasons.append("redaction_marker")
    return reasons


def scan_text(text: str, index: SourceIndex | None = None) -> list[str]:
    """All checks on one free-text string; empty means clean. Also used on rendered text."""
    reasons = scan_secrets(text)
    if index is not None and index.overlaps(text):
        reasons.append("source_overlap")
    if _code_shaped(text):
        reasons.append("code_shape")
    return reasons


def _free_text_paths(report: dict) -> list[tuple]:
    paths: list[tuple] = [("executive_summary", "text")]
    for i, subsystem in enumerate(report.get("architecture", {}).get("subsystems", [])):
        paths.append(("architecture", "subsystems", i, "summary"))
    for i, hotspot in enumerate(report.get("risk", {}).get("hotspots", [])):
        for j, _ in enumerate(hotspot.get("entities", [])):
            paths.append(("risk", "hotspots", i, "entities", j, "explanation"))
    return paths


def _get(report: dict, path: tuple) -> Any:
    node: Any = report
    for key in path:
        node = node[key]
    return node


def _set(report: dict, path: tuple, value: Any) -> None:
    _get(report, path[:-1])[path[-1]] = value


def _strings(node: Any, path: tuple = ()) -> Iterable[tuple[tuple, str]]:
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, path + (key,))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _strings(value, path + (i,))


def _dotted(path: tuple) -> str:
    return ".".join(str(p) for p in path)


def apply_content_gate(report: dict, index: SourceIndex | None) -> tuple[dict, list[Hit], list[dict]]:
    """Returns `(report, hits, limitations)`: a copy with offending free-text fields replaced,
    the hits (path + reasons) and the limitation entries to merge into `report["limitations"]`
    (already merged into the returned report). Raises `LeakGateError` for a secret or marker
    in any other string. `index=None` skips the overlap check (tests, or no chunks stored)."""
    free = set(_free_text_paths(report))
    for path, text in _strings(report):
        if path in free:
            continue
        reasons = scan_secrets(text)
        if reasons:
            raise LeakGateError(f"{', '.join(reasons)} found outside a free-text field at {_dotted(path)}")

    out = copy.deepcopy(report)
    hits: list[Hit] = []
    for path in sorted(free, key=_dotted):
        text = _get(out, path)
        if not isinstance(text, str) or not text:
            continue
        reasons = scan_text(text, index)
        if not reasons:
            continue
        hits.append(Hit(_dotted(path), tuple(reasons)))
        if path == ("executive_summary", "text"):
            out["executive_summary"] = {"text": "", "status": "withheld", "note": WITHHELD_EXEC_NOTE}
        else:
            _set(out, path, WITHHELD_SUMMARY)

    limitations: list[dict] = []
    summaries = [h for h in hits if h.path != "executive_summary.text"]
    if summaries:
        limitations.append(
            {
                "code": "summaries_withheld",
                "text": f"{len(summaries)} summary or explanation field(s) were withheld because they contained source text.",
            }
        )
    if len(summaries) != len(hits):
        limitations.append({"code": "executive_summary_withheld", "text": "The executive summary was withheld by a content check."})
    if limitations:
        merged = {(l["code"], l["text"]) for l in out.get("limitations", []) + limitations}
        out["limitations"] = [{"code": c, "text": t} for c, t in sorted(merged)]
    return out, hits, limitations


class _VisibleText(HTMLParser):
    """The text nodes of an HTML document, without `<style>` and `<script>` contents."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        self._skip += tag in ("style", "script")

    def handle_endtag(self, tag):
        self._skip -= tag in ("style", "script") and self._skip > 0

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_text(html: str) -> str:
    parser = _VisibleText()
    parser.feed(html)
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def scan_rendered(text: str, index: SourceIndex | None = None) -> list[str]:
    """Gate 3 (design_report.md §8.1): the same secret, marker and source-overlap checks over the
    text of a rendered artifact (the HTML's text nodes, or the text extracted from the PDF). Not the
    code-shape heuristic: rendered pages legitimately hold symbols the content gate has already
    judged field by field. A non-empty result means the artifact itself is the problem."""
    reasons = scan_secrets(text)
    if index is not None and index.overlaps(text):
        reasons.append("source_overlap")
    return reasons
