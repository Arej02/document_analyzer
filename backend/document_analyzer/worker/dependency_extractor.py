"""Declared packages as `dependency` entities — design.md section 5 (`DependencyExtractor`),
tasks_agent2.md Phase 7.

Reuses `worker/discovery.py::declared_packages` (the manifest parsing behind the live
`dependencies` stat), so the stat and the persisted entities agree. Pure parsing: no LLM,
no Supabase, nothing executed. No edges, and the entities are never summarized (design.md section 5).
"""

from pathlib import Path

from document_analyzer.worker.discovery import declared_packages
from document_analyzer.worker.extract import ExtractedEntity, ExtractionResult


class DependencyExtractor:
    def detect(self, repo_path: Path) -> bool:
        return bool(declared_packages(repo_path))

    def extract(self, repo_path: Path) -> ExtractionResult:
        entities = [
            ExtractedEntity(
                local_id=f"dependency:{row['ecosystem']}:{row['manifest_path']}:{row['name']}",
                entity_type="dependency",
                name=row["name"],
                file_path=row["manifest_path"],
                raw_facts={
                    "ecosystem": row["ecosystem"],
                    "version_spec": row["version_spec"],
                    "manifest_path": row["manifest_path"],
                    "dev": row["dev"],
                },
            )
            for row in declared_packages(repo_path)
        ]
        return ExtractionResult(entities=entities)
