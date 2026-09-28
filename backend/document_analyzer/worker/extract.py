"""Structural extraction interface — tasks.md Phase 2
"Implement `Extractor` protocol (design.md §5)".

This is the shared contract every language/framework extractor implements
(`PhpExtractor`, `WordPressExtractor`, `LaravelExtractor`, and any future
addition) plus the
plain-data shape `extract()` hands back. Pure parsing only — no LLM calls,
no Supabase writes; persisting an `ExtractionResult` into
`extracted_entities`/`entity_edges` (design.md §2) is the next, separate task.

Entities don't carry database ids here since they don't exist until
persisted. Edges reference entities by `local_id` (an extractor-assigned key,
unique within one `ExtractionResult`) rather than a row id; the persistence
step resolves `local_id` -> `extracted_entities.id` when inserting.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

EntityType = Literal["function", "class", "route", "schema", "hook", "dependency"]
EdgeType = Literal["calls", "hook_binding", "global", "include", "route_binding", "inherits"]


@dataclass(frozen=True)
class ExtractedEntity:
    local_id: str
    entity_type: EntityType
    name: str
    file_path: str
    raw_facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedEdge:
    from_local_id: str
    edge_type: EdgeType
    to_local_id: str | None = None  # None for edges to external/global targets
    # Where the edge came from (extractor, upstream relation, confidence, line). Empty for
    # the PHP/WordPress/Laravel extractors, whose edges are all directly parsed facts;
    # GraphifyExtractor fills it because its cross-file resolution can be INFERRED.
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractionResult:
    entities: list[ExtractedEntity] = field(default_factory=list)
    edges: list[ExtractedEdge] = field(default_factory=list)
    # Per-extractor file coverage report (what was extracted, partial, unsupported, ...),
    # persisted onto ingestion_jobs.extraction_coverage. None when an extractor has
    # nothing to report beyond its entities.
    coverage: dict[str, Any] | None = None


@runtime_checkable
class Extractor(Protocol):
    def detect(self, repo_path: Path) -> bool:
        """Return True if this extractor applies to the given repo."""
        ...

    def extract(self, repo_path: Path) -> ExtractionResult:
        """Return entities + edges. Pure parsing, no LLM calls."""
        ...
