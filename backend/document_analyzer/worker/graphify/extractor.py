"""GraphifyExtractor: every non-PHP language Graphify supports, behind the `Extractor`
protocol (extract.py, design.md §5a).

PHP stays with PhpExtractor (nikic/php-parser: real namespace/use resolution) and the
WordPress/Laravel extractors (hook_binding / route_binding, the framework wiring no
generic parser sees), so `.php` files are never sent to Graphify and no entity is
extracted twice. Everything else Graphify's image advertises as a *code* language
(Python, JS/TS, Go, Java, C#, Ruby, ...) goes through one disposable container
(runner.py) and is mapped onto `extracted_entities` / `entity_edges`:

- nodes of kind function / method / class become `function` / `class` entities. File,
  rationale and document nodes don't: they carry no behaviour to summarise, and file
  structure is already what the module-level summaries fold over.
- `calls` stays `calls`; `inherits` / `extends` / `implements` become `inherits`;
  `imports` / `imports_from` become `include` when both ends are entities. Class ->
  method (`method`) and containment are folded into raw_facts (`methods`, `class`),
  like PhpExtractor does. Every other relation is dropped and *counted* in the
  coverage report — never silently.
- Each edge keeps its provenance (upstream relation, EXTRACTED/INFERRED confidence and
  score, file and line), because Graphify resolves cross-file names heuristically.

AGENT1_GRAPHIFY selects the mode: `auto` (default) runs when Docker and the image are
available and otherwise records `unavailable` in the coverage report; `required` fails
the job instead; `off` never runs. Once the container has run, a failure always fails
the job — only a missing prerequisite is tolerated.
"""

import os
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from document_analyzer.resource_bounds import MAX_FILE_COUNT
from document_analyzer.worker.extract import ExtractedEdge, ExtractedEntity, ExtractionResult
from document_analyzer.worker.graphify.runner import (
    GraphifyParams,
    GraphifyRunError,
    GraphifyUnavailableError,
    NoAnalysableFiles,
    check_available,
    load_capabilities,
    run_extraction,
)
from document_analyzer.worker.graphify.validate import GraphifyOutputError, ValidatedOutput

EXCLUDED_SUFFIXES = (".php",)  # owned by PhpExtractor / WordPressExtractor / LaravelExtractor
ENTITY_KINDS = {"function": "function", "method": "function", "class": "class"}
EDGE_MAP = {
    "calls": "calls",
    "inherits": "inherits",
    "extends": "inherits",
    "implements": "inherits",
    "imports": "include",
    "imports_from": "include",
}
FOLDED_RELATIONS = {"method", "contains"}
MAX_COVERAGE_SAMPLES = 200


class GraphifyExtractionError(Exception):
    """Graphify was required but unavailable, or its container/output failed."""


def normalise_label(name: str | None) -> str:
    """Graphify labels look like ``helper()``, ``.greet()`` or ``Greeter``."""
    value = (name or "").strip().removeprefix(".")
    return value[:-2] if value.endswith("()") else value


def graphify_mode() -> str:
    mode = (os.environ.get("AGENT1_GRAPHIFY") or "auto").strip().lower()
    return mode if mode in ("auto", "required", "off") else "auto"


def _repo_files(repo_path: Path) -> list[str]:
    files = []
    for path in repo_path.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        files.append(path.relative_to(repo_path).as_posix())
    return sorted(files)


def select_files(
    files: list[str], sizes: dict[str, int], manifest: dict[str, Any], max_file_bytes: int
) -> tuple[list[str], dict[str, Any]]:
    """Chooses what Graphify analyses and accounts for everything it doesn't."""
    code_extensions = {
        e["extension"].lower()
        for e in manifest.get("extensions", [])
        if e.get("category") == "code" and e.get("availability") in ("installed", "fallback")
    }
    selected: list[str] = []
    claimed_by_php = 0
    oversized: list[str] = []
    no_extractor: Counter[str] = Counter()
    for rel in files:
        lower = rel.lower()
        if lower.endswith(EXCLUDED_SUFFIXES):
            claimed_by_php += 1
            continue
        suffix = PurePosixPath(lower).suffix
        if suffix not in code_extensions:
            no_extractor[suffix or "(none)"] += 1
            continue
        if sizes.get(rel, 0) > max_file_bytes:
            oversized.append(rel)
            continue
        selected.append(rel)
    report = {
        "files_claimed_by_php_extractors": claimed_by_php,
        "files_without_code_extractor": sum(no_extractor.values()),
        "files_without_code_extractor_by_extension": dict(no_extractor.most_common(20)),
        "files_oversized": len(oversized),
        "files_oversized_sample": oversized[:MAX_COVERAGE_SAMPLES],
    }
    return selected, report


def map_output(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> tuple[list[ExtractedEntity], list[ExtractedEdge], dict[str, Any]]:
    """Pure mapping of validated Graphify records onto the Extractor protocol."""
    by_id = {n["entity_id"]: n for n in nodes}
    entity_nodes = {
        nid: n for nid, n in by_id.items() if n.get("kind") in ENTITY_KINDS and n.get("relative_path")
    }

    methods_of: dict[str, list[str]] = defaultdict(list)
    class_of: dict[str, str] = {}
    calls_of: dict[str, set[str]] = defaultdict(set)
    bases_of: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        src, tgt = e["source_id"], e["target_id"]
        if e["relation"] == "method" and src in entity_nodes and tgt in entity_nodes:
            methods_of[src].append(normalise_label(by_id[tgt]["name"]))
            class_of[tgt] = normalise_label(by_id[src]["name"])
        elif e["relation"] == "calls" and src in entity_nodes and tgt in by_id:
            calls_of[src].add(normalise_label(by_id[tgt]["name"]))
        elif EDGE_MAP.get(e["relation"]) == "inherits" and src in entity_nodes and tgt in by_id:
            bases_of[src].add(normalise_label(by_id[tgt]["name"]))

    entities: list[ExtractedEntity] = []
    for nid in sorted(entity_nodes):
        n = entity_nodes[nid]
        raw_facts: dict[str, Any] = {
            "extractor": "graphify",
            "language": n.get("language"),
            "kind": n["kind"],
            "line": n.get("line_start"),
        }
        if n.get("line_end"):
            raw_facts["end_line"] = n["line_end"]
        if nid in class_of:
            raw_facts["class"] = class_of[nid]
        if n["kind"] == "class":
            raw_facts["methods"] = sorted(methods_of.get(nid, []))
            raw_facts["extends"] = sorted(bases_of.get(nid, []))
        else:
            raw_facts["calls"] = sorted(calls_of.get(nid, []))
        entities.append(
            ExtractedEntity(
                local_id=nid,
                entity_type=ENTITY_KINDS[n["kind"]],
                name=normalise_label(n["name"]) or n["name"],
                file_path=n["relative_path"],
                raw_facts=raw_facts,
            )
        )

    mapped: list[ExtractedEdge] = []
    kept: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    for e in edges:
        relation = e["relation"]
        edge_type = EDGE_MAP.get(relation)
        if relation in FOLDED_RELATIONS:
            kept[f"{relation} (folded into raw_facts)"] += 1
            continue
        if edge_type is None or e["source_id"] not in entity_nodes or e["target_id"] not in entity_nodes:
            dropped[relation if edge_type is None else f"{relation} (file-level endpoint)"] += 1
            continue
        kept[relation] += 1
        mapped.append(
            ExtractedEdge(
                from_local_id=e["source_id"],
                edge_type=edge_type,
                to_local_id=e["target_id"],
                provenance={
                    "extractor": "graphify",
                    "relation": relation,
                    "confidence": e.get("confidence"),
                    "confidence_score": e.get("confidence_score"),
                    "file_path": e.get("relative_path"),
                    "line": e.get("line"),
                },
            )
        )
    stats = {
        "nodes": len(nodes),
        "entities": len(entities),
        "nodes_not_entities_by_kind": dict(Counter(n.get("kind") for nid, n in by_id.items() if nid not in entity_nodes)),
        "edges": len(edges),
        "edges_kept_by_relation": dict(kept),
        "edges_dropped_by_relation": dict(dropped),
    }
    return entities, mapped, stats


def _coverage_from_worker(coverage: dict[str, Any]) -> dict[str, Any]:
    files = coverage.get("files") or []
    not_extracted = [
        {"path": f.get("path"), "status": f.get("status"), "reason": f.get("reason")}
        for f in files
        if f.get("status") not in ("extracted", "extracted_unobserved")
    ]
    return {
        "counts_by_status": coverage.get("counts_by_status") or {},
        "by_language": coverage.get("by_language") or {},
        "not_extracted_sample": not_extracted[:MAX_COVERAGE_SAMPLES],
        "not_extracted_count": len(not_extracted),
        "unresolved_edges": (coverage.get("extraction_stats") or {}).get("unresolved_edges"),
    }


class GraphifyExtractor:
    """Extractor protocol implementation for every non-PHP code language Graphify parses."""

    def __init__(
        self,
        params: GraphifyParams | None = None,
        mode: str | None = None,
        run: Callable[..., ValidatedOutput] = run_extraction,
        check: Callable[[GraphifyParams], None] = check_available,
        capabilities: Callable[[str], dict[str, Any]] = load_capabilities,
        job_id: str = "adhoc",
    ):
        self._params = params or GraphifyParams.from_env()
        self._mode = mode or graphify_mode()
        self._run = run
        self._check = check
        self._capabilities = capabilities
        self.job_id = job_id

    def detect(self, repo_path: Path) -> bool:
        if self._mode == "off":
            return False
        return any(not rel.lower().endswith(EXCLUDED_SUFFIXES) for rel in _repo_files(repo_path))

    def extract(self, repo_path: Path) -> ExtractionResult:
        try:
            self._check(self._params)
            manifest = self._capabilities(self._params.image)
        except GraphifyUnavailableError as exc:
            if self._mode == "required":
                raise GraphifyExtractionError(f"graphify is required but unavailable: {exc}") from exc
            return ExtractionResult(coverage={"graphify": {"status": "unavailable", "reason": str(exc)}})

        files = _repo_files(repo_path)[:MAX_FILE_COUNT]
        sizes = {rel: (repo_path / rel).stat().st_size for rel in files}
        selected, selection = select_files(files, sizes, manifest, self._params.max_file_bytes)
        report: dict[str, Any] = {
            "graphify_version": manifest.get("graphify_version"),
            "files_requested": len(selected),
            **selection,
        }
        if not selected:
            return ExtractionResult(coverage={"graphify": {"status": "no_candidate_files", **report}})

        try:
            output = self._run(repo_path, selected, self._params, self.job_id, manifest.get("digest", ""))
        except NoAnalysableFiles as exc:
            return ExtractionResult(
                coverage={"graphify": {"status": "no_analysable_files", "reason": str(exc)[:500], **report}}
            )
        except GraphifyUnavailableError as exc:
            if self._mode == "required":
                raise GraphifyExtractionError(f"graphify is required but unavailable: {exc}") from exc
            return ExtractionResult(coverage={"graphify": {"status": "unavailable", "reason": str(exc), **report}})
        except (GraphifyRunError, GraphifyOutputError) as exc:
            raise GraphifyExtractionError(f"graphify extraction failed: {exc}") from exc

        entities, edges, stats = map_output(output.nodes, output.edges)
        coverage = {
            "status": "ran",
            **report,
            **_coverage_from_worker(output.coverage),
            "mapping": stats,
            "measurements": output.manifest.measurements,
        }
        return ExtractionResult(entities=entities, edges=edges, coverage={"graphify": coverage})
