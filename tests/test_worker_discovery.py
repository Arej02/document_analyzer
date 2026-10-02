"""worker/discovery.py — live discovered stats (tasks_frontend.md A1.6)."""

import json

from document_analyzer.worker.discovery import MAX_FILE_BYTES, count_dependencies, discover_repo


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_counts_source_files_and_lines_by_language(tmp_path):
    _write(tmp_path, "a.php", "<?php\necho 1;\necho 2;\n")  # 3 lines
    _write(tmp_path, "b.php", "<?php\n")  # 1 line
    _write(tmp_path, "web/app.js", "let x\nlet y")  # 2 lines, no trailing newline
    _write(tmp_path, "web/App.tsx", "export {}\n")  # 1 line, TypeScript
    _write(tmp_path, "README.md", "not code\n" * 50)
    _write(tmp_path, "style.css", "a {}\n" * 50)

    stats = discover_repo(tmp_path)

    assert stats["loc"] == 7
    assert stats["language.PHP"] == 2
    assert stats["language.JavaScript"] == 1
    assert stats["language.TypeScript"] == 1
    assert not any(key.startswith("language.") and key.endswith(("Markdown", "CSS")) for key in stats)


def test_an_empty_repo_reports_zeros_not_missing_keys(tmp_path):
    stats = discover_repo(tmp_path)

    assert stats == {"loc": 0, "dependencies": 0}


def test_an_oversized_file_counts_as_a_file_but_its_lines_are_not_read(tmp_path):
    (tmp_path / "big.py").write_bytes(b"x = 1\n" * (MAX_FILE_BYTES // 6 + 10))
    _write(tmp_path, "small.py", "y = 2\n")

    stats = discover_repo(tmp_path)

    assert stats["language.Python"] == 2
    assert stats["loc"] == 1


def test_dependencies_are_distinct_across_manifests_and_skip_platform_requirements(tmp_path):
    _write(tmp_path, "composer.json", json.dumps({"require": {"php": ">=8", "ext-json": "*", "monolog/monolog": "^3"}, "require-dev": {"phpunit/phpunit": "^10"}}))
    _write(tmp_path, "web/package.json", json.dumps({"dependencies": {"react": "^19"}, "devDependencies": {"vite": "^8"}}))
    _write(tmp_path, "requirements.txt", "# comment\nrequests==2.31\n-r other.txt\nFlask>=3\n\n")
    _write(tmp_path, "other/composer.json", json.dumps({"require": {"monolog/monolog": "^3"}}))  # same package again

    assert count_dependencies(tmp_path) == 6  # monolog, phpunit, react, vite, requests, flask


def test_a_malformed_manifest_is_ignored(tmp_path):
    _write(tmp_path, "composer.json", "{not json")
    _write(tmp_path, "package.json", json.dumps(["not", "an", "object"]))

    assert count_dependencies(tmp_path) == 0


def test_frameworks_come_from_the_framework_extractors(tmp_path):
    _write(tmp_path, "plugin.php", "<?php\nadd_action('init', 'setup');\n")

    stats = discover_repo(tmp_path)

    assert stats["framework.WordPress"] == 1
    assert "framework.Laravel" not in stats


def test_emit_discovered_stats_sends_the_map_and_never_raises(tmp_path, monkeypatch):
    from document_analyzer.worker import run as run_module

    class Emitter:
        def __init__(self):
            self.calls = []

        def stats(self, **counts):
            self.calls.append(counts)

    persisted = []
    monkeypatch.setattr(
        run_module, "persist_discovery_stats", lambda client, job_id, org_id, stats: persisted.append((job_id, org_id, stats))
    )
    _write(tmp_path, "a.py", "print(1)\n")
    emitter = Emitter()
    run_module._emit_discovered_stats(emitter, tmp_path, None, "job-1", "org-1")
    expected = {"loc": 1, "dependencies": 0, "language.Python": 1}
    assert emitter.calls == [expected]
    assert persisted == [("job-1", "org-1", expected)]

    def boom(_path):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(run_module, "discover_repo", boom)
    run_module._emit_discovered_stats(emitter, tmp_path, None, "job-1", "org-1")  # logs and carries on
    assert len(emitter.calls) == 1
