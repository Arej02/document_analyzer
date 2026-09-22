from pathlib import Path

from document_analyzer.worker.extract import (
    Extractor,
    ExtractedEdge,
    ExtractedEntity,
    ExtractionResult,
)


class _FakeExtractor:
    """Minimal stand-in confirming the Protocol is satisfiable without inheritance."""

    def detect(self, repo_path: Path) -> bool:
        return (repo_path / "marker.php").exists()

    def extract(self, repo_path: Path) -> ExtractionResult:
        entity = ExtractedEntity(
            local_id="fn:marker.php:do_thing",
            entity_type="function",
            name="do_thing",
            file_path="marker.php",
            raw_facts={"params": []},
        )
        edge = ExtractedEdge(from_local_id=entity.local_id, edge_type="calls", to_local_id=None)
        return ExtractionResult(entities=[entity], edges=[edge])


def test_fake_extractor_satisfies_extractor_protocol():
    extractor: Extractor = _FakeExtractor()
    assert isinstance(extractor, Extractor)


def test_extract_returns_entities_and_edges(tmp_path):
    (tmp_path / "marker.php").write_text("<?php function do_thing() {}")

    extractor = _FakeExtractor()
    assert extractor.detect(tmp_path) is True

    result = extractor.extract(tmp_path)
    assert len(result.entities) == 1
    assert result.entities[0].entity_type == "function"
    assert len(result.edges) == 1
    assert result.edges[0].from_local_id == result.entities[0].local_id
    assert result.edges[0].to_local_id is None


def test_extraction_result_defaults_to_empty():
    result = ExtractionResult()
    assert result.entities == []
    assert result.edges == []
