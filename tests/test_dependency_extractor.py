"""DependencyExtractor / discovery.declared_packages — design.md section 5, tasks_agent2.md Phase 7."""

import json
from pathlib import Path

from document_analyzer.worker.dependency_extractor import DependencyExtractor
from document_analyzer.worker.discovery import count_dependencies, declared_packages
from document_analyzer.worker.extract import Extractor


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _by_name(result):
    return {(e.raw_facts["ecosystem"], e.raw_facts["manifest_path"], e.name): e for e in result.entities}


def test_it_satisfies_the_extractor_protocol():
    assert isinstance(DependencyExtractor(), Extractor)


def test_detect_is_false_without_manifests_or_packages(tmp_path):
    _write(tmp_path, "a.php", "<?php")
    _write(tmp_path, "composer.json", json.dumps({"require": {"php": ">=8", "ext-json": "*"}}))

    assert DependencyExtractor().detect(tmp_path) is False


def test_each_package_becomes_a_dependency_entity_with_its_declared_facts(tmp_path):
    _write(tmp_path, "composer.json", json.dumps(
        {"require": {"php": ">=8", "monolog/monolog": "^3"}, "require-dev": {"phpunit/phpunit": "^10"}}))
    _write(tmp_path, "web/package.json", json.dumps({"dependencies": {"react": "^19"}, "devDependencies": {"vite": "^8"}}))
    _write(
        tmp_path,
        "requirements.txt",
        "\n".join(["# c", "requests==2.31  # pinned", "Flask[async]>=3 ; python_version>'3.8'", "numpy", "-r other.txt", ""]),
    )

    result = DependencyExtractor().extract(tmp_path)
    entities = _by_name(result)

    assert result.edges == []
    assert {e.entity_type for e in result.entities} == {"dependency"}
    assert entities[("composer", "composer.json", "monolog/monolog")].raw_facts == {
        "ecosystem": "composer", "version_spec": "^3", "manifest_path": "composer.json", "dev": False}
    assert entities[("composer", "composer.json", "phpunit/phpunit")].raw_facts["dev"] is True
    assert entities[("npm", "web/package.json", "vite")].raw_facts["dev"] is True
    assert entities[("npm", "web/package.json", "react")].file_path == "web/package.json"
    assert entities[("pip", "requirements.txt", "requests")].raw_facts["version_spec"] == "==2.31"
    assert entities[("pip", "requirements.txt", "flask")].raw_facts["version_spec"] == ">=3"
    assert entities[("pip", "requirements.txt", "numpy")].raw_facts["version_spec"] is None
    assert not any(name == "php" or name.startswith("ext-") for _, _, name in entities)


def test_the_same_package_in_two_manifests_is_two_entities_but_one_in_the_count(tmp_path):
    _write(tmp_path, "composer.json", json.dumps({"require": {"monolog/monolog": "^3"}}))
    _write(tmp_path, "other/composer.json", json.dumps({"require": {"monolog/monolog": "^2"}}))

    result = DependencyExtractor().extract(tmp_path)

    assert len(result.entities) == 2
    assert len({e.local_id for e in result.entities}) == 2  # local ids are unique within a result
    assert count_dependencies(tmp_path) == 1


def test_entity_count_matches_the_declared_package_rows_and_order_is_stable(tmp_path):
    _write(tmp_path, "composer.json", json.dumps({"require": {"b/b": "1", "a/a": "1"}}))

    first = [e.name for e in DependencyExtractor().extract(tmp_path).entities]

    assert first == ["a/a", "b/b"] == [r["name"] for r in declared_packages(tmp_path)]


def test_a_non_string_version_is_recorded_as_none_and_malformed_manifests_are_ignored(tmp_path):
    _write(tmp_path, "composer.json", json.dumps({"require": {"x/y": {"version": "1"}}}))
    _write(tmp_path, "package.json", "{not json")

    [entity] = DependencyExtractor().extract(tmp_path).entities

    assert entity.raw_facts["version_spec"] is None
