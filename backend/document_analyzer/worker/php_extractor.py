"""Structural extraction: plain PHP — tasks.md Phase 2
"PhpExtractor: subprocess wrapper around nikic/php-parser, normalize output
into ExtractionResult".

Implements the `Extractor` protocol (extract.py, design.md §5) for plain PHP.
The actual AST parsing happens out-of-process, in `backend/php/extract.php`,
which loads `nikic/php-parser` (vendored via Composer under `backend/php/`)
and prints one JSON document describing every top-level function/class (and
each class's methods) found under the repo. This module's only job is to run
that subprocess and normalize its JSON into `ExtractionResult`. Pure parsing,
no LLM calls, no Supabase writes — persistence is a separate later task.

Scoping notes (deliberately conservative, not a placeholder):
- A file that fails to parse is skipped for entity extraction rather than
  aborting the whole repo, matching CLAUDE.md's "grounding checker failures
  never block ingestion" principle applied one stage earlier. Nothing is
  persisted yet at all (this task predates the persistence step), so there is
  no log/queryable record of a skipped file here — that lands with the
  boilerplate-filter task's "log filtered files" requirement, which is a
  natural place to also log parse failures.
- `calls` edges are only emitted when the callee resolves to another entity
  in this same extraction (same repo, this run) — a call to something
  outside the repo (a WordPress/PHP builtin, a Composer dependency) is kept
  in the calling entity's `raw_facts["calls"]` but does not become an edge,
  since `entity_edges` has no field to name an unresolved target.
- A qualified call (`Class::method`) that doesn't match a locally-defined
  method (e.g. an inherited Eloquent static like `Appointment::paginate` —
  `paginate` lives on Laravel's vendor base class, not on `Appointment`
  itself) falls back to a `calls` edge at the *class* level, if exactly one
  locally-defined class named `Class` exists. Two different controllers that
  both touch the same model previously shared no edge at all (the specific
  method call site is the same "doesn't exist locally" case as any other
  unresolved call); this at least records "depends on this model", the one
  piece of real structure a framework-heavy app actually has that plain
  call-graph parsing would otherwise miss entirely. Ambiguous by name (two
  same-named classes in different files) resolves to nothing, same
  conservative rule `method_by_name` already applies to a bare method call.
- `include`/`require` edges are only emitted for an include statement inside
  a function/method body, anchored to that function/method as `from_entity`.
  A top-level (file-scope) include has no entity to anchor to — `raw_facts`
  has no home for it either at file scope, so it is not currently captured.
  Revisit if a `module`/file-level entity type is ever added.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from document_analyzer.worker.extract import ExtractedEdge, ExtractedEntity, ExtractionResult

_PHP_SCRIPT = Path(__file__).resolve().parents[2] / "php" / "extract.php"


def _call_names(calls: list[str]) -> list[str]:
    """Distinct call names, sorted: the order and repeat count of calls in a body carry
    no meaning here, and a stable form keeps `content_hash` (persist.py) stable."""
    return sorted(set(calls))


class PhpExtractionError(Exception):
    """Raised when the PHP subprocess fails to run or returns unparseable output."""


@dataclass(frozen=True)
class _PendingCallSite:
    local_id: str
    calls: list[str]
    includes: list[str]


class PhpExtractor:
    """Extractor protocol implementation for plain PHP (design.md §5)."""

    def __init__(self, php_binary: str = "php"):
        self._php_binary = php_binary

    def detect(self, repo_path: Path) -> bool:
        return next(repo_path.rglob("*.php"), None) is not None

    def extract(self, repo_path: Path) -> ExtractionResult:
        files = self._run_php_script(repo_path)

        entities: list[ExtractedEntity] = []
        pending_call_sites: list[_PendingCallSite] = []
        function_by_name: dict[str, str] = {}
        method_by_qualified_name: dict[str, str] = {}
        method_by_name: dict[str, list[str]] = {}
        class_by_name: dict[str, list[str]] = {}

        for file_result in files:
            if file_result.get("error"):
                continue
            file_path = file_result["file"]

            for fn in file_result.get("functions", []):
                local_id = f"function:{file_path}:{fn['name']}:{fn['line']}"
                entities.append(
                    ExtractedEntity(
                        local_id=local_id,
                        entity_type="function",
                        name=fn["name"],
                        file_path=file_path,
                        raw_facts={"params": fn["params"], "line": fn["line"], "calls": _call_names(fn["calls"])},
                    )
                )
                function_by_name.setdefault(fn["name"], local_id)
                pending_call_sites.append(_PendingCallSite(local_id, fn["calls"], fn["includes"]))

            for cls in file_result.get("classes", []):
                class_local_id = f"class:{file_path}:{cls['name']}:{cls['line']}"
                entities.append(
                    ExtractedEntity(
                        local_id=class_local_id,
                        entity_type="class",
                        name=cls["name"],
                        file_path=file_path,
                        raw_facts={
                            "extends": cls["extends"],
                            "implements": cls["implements"],
                            "line": cls["line"],
                            "methods": [m["name"] for m in cls["methods"]],
                        },
                    )
                )
                class_by_name.setdefault(cls["name"], []).append(class_local_id)

                for method in cls.get("methods", []):
                    method_local_id = f"method:{file_path}:{cls['name']}:{method['name']}:{method['line']}"
                    entities.append(
                        ExtractedEntity(
                            local_id=method_local_id,
                            entity_type="function",
                            name=method["name"],
                            file_path=file_path,
                            raw_facts={
                                "class": cls["name"],
                                "params": method["params"],
                                "visibility": method["visibility"],
                                "static": method["static"],
                                "line": method["line"],
                                "calls": _call_names(method["calls"]),
                            },
                        )
                    )
                    qualified = f"{cls['name']}::{method['name']}"
                    method_by_qualified_name.setdefault(qualified, method_local_id)
                    method_by_name.setdefault(method["name"], []).append(method_local_id)
                    pending_call_sites.append(
                        _PendingCallSite(method_local_id, method["calls"], method["includes"])
                    )

        edges = self._resolve_edges(
            pending_call_sites, function_by_name, method_by_qualified_name, method_by_name, class_by_name
        )

        return ExtractionResult(entities=entities, edges=edges)

    def _run_php_script(self, repo_path: Path) -> list[dict[str, Any]]:
        try:
            result = subprocess.run(
                [self._php_binary, str(_PHP_SCRIPT), str(repo_path)],
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise PhpExtractionError(f"could not run php extractor: {exc}") from exc

        if result.returncode != 0:
            raise PhpExtractionError(f"php extractor failed: {result.stderr.strip()}")

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PhpExtractionError(f"php extractor produced invalid JSON: {exc}") from exc

    @staticmethod
    def _resolve_edges(
        call_sites: list[_PendingCallSite],
        function_by_name: dict[str, str],
        method_by_qualified_name: dict[str, str],
        method_by_name: dict[str, list[str]],
        class_by_name: dict[str, list[str]],
    ) -> list[ExtractedEdge]:
        edges: list[ExtractedEdge] = []

        for site in call_sites:
            for call in site.calls:
                target = None
                if "::" in call:
                    target = method_by_qualified_name.get(call)
                    if target is None:
                        # An inherited/vendor method on a locally-defined class (e.g. an
                        # Eloquent static like `Appointment::paginate`) — fall back to a
                        # class-level edge rather than no edge at all.
                        class_name, _, _method_name = call.partition("::")
                        candidates = class_by_name.get(class_name, [])
                        if len(candidates) == 1:
                            target = candidates[0]
                else:
                    target = function_by_name.get(call)
                    if target is None:
                        candidates = method_by_name.get(call, [])
                        if len(candidates) == 1:
                            target = candidates[0]
                if target is not None:
                    edges.append(ExtractedEdge(from_local_id=site.local_id, edge_type="calls", to_local_id=target))

            for _include in site.includes:
                edges.append(ExtractedEdge(from_local_id=site.local_id, edge_type="include", to_local_id=None))

        return edges
