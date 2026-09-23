"""tasks.md Phase 6: "Load test: a large repo (5,000-10,000+ entities,
representative of a real WordPress site with several plugins) — confirm
fan-out concurrency cap actually bounds cost/time as expected, and that
ingestion resource bounds reject oversized input cleanly."

Every other test in this suite that touches PhpExtractor/WordPressExtractor,
summarize_fanout, or resource_bounds uses a handful of entities/files — enough
to prove the logic is *correct*, never enough to prove it holds up at the
scale a real WordPress site with several plugins actually produces. This file
is the one place that generates a repo sized for that (via the real
PhpExtractor/WordPressExtractor subprocesses, same parsers a live ingestion
job uses) and pushes it through the two things tasks.md calls out:

1. summarize_fanout.py's concurrency cap, run over the full generated entity
   set with a fake (but real-shaped) Azure OpenAI client — confirms peak
   in-flight calls never exceeds the cap and that wall-clock time actually
   scales with entity_count/cap rather than being silently unbounded.
2. worker/fetch.py's MAX_FILE_COUNT bound, checked at its real default
   (50,000) rather than a monkeypatched-down value like every other fetch
   test uses — confirms the entry-count guard is still cheap and still
   rejects cleanly (no extraction) at the actual production threshold.

A third, live-only check persists the generated repo's entities through
persist.py's real batch insert (insert_many_scoped) at this scale — task 43's
own comment flagged this exact scenario ("can run into the thousands of rows
per job (tasks.md Phase 6 load test)") as the thing to validate here, since a
single multi-row INSERT silently truncating (e.g. a Postgrest row cap) would
otherwise misalign persist.py's local_id -> db id zip without any error.
"""

import io
import os
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv()

from document_analyzer.resource_bounds import MAX_FILE_COUNT  # noqa: E402
from document_analyzer.supabase_client import get_service_role_client, insert_scoped  # noqa: E402
from document_analyzer.worker.fetch import SourceFetchError, fetch_source  # noqa: E402
from document_analyzer.worker.persist import persist_extraction_result  # noqa: E402
from document_analyzer.worker.php_extractor import PhpExtractor  # noqa: E402
from document_analyzer.worker.summarize import EntitySummary, SummarizationError  # noqa: E402
from document_analyzer.worker.summarize_fanout import summarize_entities  # noqa: E402
from document_analyzer.worker.wordpress_extractor import WordPressExtractor  # noqa: E402

from fakes import FakeClient  # noqa: E402

N_PLUGINS = 15
FILES_PER_PLUGIN = 12
FUNCTIONS_PER_FILE = 25
METHODS_PER_CLASS = 5
HOOKS_PER_FILE = 2


def _generate_wordpress_like_repo(repo_root: Path) -> None:
    """Writes a synthetic multi-plugin WordPress site sized to land in the
    5,000-10,000+ entity range once run through the real extractors: N_PLUGINS
    plugins, each with FILES_PER_PLUGIN files, each file with FUNCTIONS_PER_FILE
    top-level functions, one class with METHODS_PER_CLASS methods, and
    HOOKS_PER_FILE add_action registrations (plus one register_post_type per
    plugin) — the same shape of signal WordPressExtractor looks for."""
    for p in range(N_PLUGINS):
        plugin_dir = repo_root / "wp-content" / "plugins" / f"plugin-{p}"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        for f in range(FILES_PER_PLUGIN):
            lines = ["<?php"]
            fn_names = []
            for k in range(FUNCTIONS_PER_FILE):
                name = f"plugin{p}_file{f}_fn{k}"
                fn_names.append(name)
                lines.append(f"function {name}() {{ return {k}; }}")
            lines.append(f"class Plugin{p}File{f}Helper {{")
            for m in range(METHODS_PER_CLASS):
                lines.append(f"    public function method{m}() {{ return {m}; }}")
            lines.append("}")
            for h in range(HOOKS_PER_FILE):
                hook_name = f"plugin{p}_file{f}_hook{h}"
                callback = fn_names[h % len(fn_names)]
                lines.append(f"add_action('{hook_name}', '{callback}');")
            if f == 0:
                lines.append(f"register_post_type('plugin{p}_cpt', array('label' => 'Plugin {p} CPT'));")
            (plugin_dir / f"file-{f}.php").write_text("\n".join(lines) + "\n")


@pytest.fixture(scope="module")
def large_repo(tmp_path_factory):
    repo_root = tmp_path_factory.mktemp("large-wp-repo")
    _generate_wordpress_like_repo(repo_root)
    return repo_root


@pytest.fixture(scope="module")
def extraction_results(large_repo):
    """Runs the real PhpExtractor + WordPressExtractor subprocesses once for the
    whole module — every test below reuses the same result rather than re-parsing
    ~180 files per test."""
    php = PhpExtractor()
    wp = WordPressExtractor()
    assert php.detect(large_repo)
    assert wp.detect(large_repo)
    return php.extract(large_repo), wp.extract(large_repo)


def test_generated_repo_produces_load_test_scale_entities(extraction_results):
    php_result, wp_result = extraction_results
    total = len(php_result.entities) + len(wp_result.entities)

    assert total >= 5000, f"generated repo only produced {total} entities — not representative load-test scale"


def test_concurrency_cap_bounds_peak_in_flight_calls_at_load_scale(extraction_results):
    """Runs the real summarize_fanout.summarize_entities over the whole generated
    entity set (thousands, not the handful every other fan-out test uses) with a
    fake Azure OpenAI client whose only job is to track overlap and hold briefly —
    confirms the cap actually bounds concurrency (cost) and that total wall time
    scales with entity_count/cap (time), not with entity_count alone."""
    php_result, _ = extraction_results
    entities = php_result.entities
    cap = 10
    hold_seconds = 0.005

    class _Tracker:
        def __init__(self):
            self._lock = threading.Lock()
            self._current = 0
            self.max_concurrent = 0
            self.call_count = 0

        def create(self, **kwargs):
            with self._lock:
                self._current += 1
                self.max_concurrent = max(self.max_concurrent, self._current)
                self.call_count += 1
            time.sleep(hold_seconds)
            with self._lock:
                self._current -= 1
            return type(
                "Resp",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {"message": type("Msg", (), {"content": '{"summary": "x", "risk_level": "low", "risk_factors": []}'})()},
                        )()
                    ]
                },
            )()

    tracker = _Tracker()
    client = type("Client", (), {"chat": type("Chat", (), {"completions": tracker})()})()

    start = time.monotonic()
    results = summarize_entities(entities, client=client, max_workers=cap)
    elapsed = time.monotonic() - start

    assert len(results) == len(entities)
    assert tracker.call_count == len(entities)
    assert tracker.max_concurrent <= cap
    # Genuinely saturates the cap under real load rather than incidentally staying low.
    assert tracker.max_concurrent == cap

    expected_min_elapsed = (len(entities) / cap) * hold_seconds
    # Bounded below (proves work is actually serialized past the cap, not silently
    # run fully in parallel) with slack for scheduling overhead; no upper bound
    # asserted here since machine load is variable — the max_concurrent==cap
    # assertion above is what actually pins "cost" down.
    assert elapsed >= expected_min_elapsed * 0.7


def test_max_file_count_default_rejects_an_oversized_archive_without_extracting(tmp_path):
    """Uses the real default MAX_FILE_COUNT (50,000), not a monkeypatched-down value
    like every other fetch test — confirms the entry-count guard is still cheap to
    check and still rejects cleanly at the actual production threshold."""
    entry_count = MAX_FILE_COUNT + 1
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as archive:
        for i in range(entry_count):
            archive.writestr(f"file-{i}.php", "")

    client = FakeClient()
    client.storage.from_("agent1-uploads").upload("org-1/job-1/bomb.zip", buf.getvalue(), None)

    start = time.monotonic()
    with pytest.raises(SourceFetchError, match="exceeding max allowed count"):
        fetch_source(client, {"source_type": "upload", "source_ref": "org-1/job-1/bomb.zip"}, tmp_path)
    elapsed = time.monotonic() - start

    # The "repo" dir itself is created before the entry-count check (same as every
    # other fetch test), but nothing was ever extracted into it.
    extract_dir = tmp_path / "repo"
    assert not extract_dir.exists() or list(extract_dir.iterdir()) == []
    assert elapsed < 10  # entry-count check reads the central directory only, no decompression


_LIVE_ENV_VARS = ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"]


@pytest.mark.skipif(
    not all(os.environ.get(name) for name in _LIVE_ENV_VARS),
    reason="requires a live Supabase project (SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY)",
)
def test_persisting_the_full_entity_set_completes_correctly_at_scale(extraction_results):
    """persist.py's local_id -> db id resolution (task 43) assumes a single
    multi-row INSERT ... RETURNING comes back complete and in order — this is the
    scenario that assumption was explicitly deferred to (task 43's own comment
    names this exact load test). A silently truncated response (e.g. a Postgrest
    row cap) would misalign the zip without raising, so the real check here is the
    persisted count matching exactly, not just "it didn't crash"."""
    php_result, wp_result = extraction_results
    service_client = get_service_role_client()
    org_id = str(uuid.uuid4())
    job = insert_scoped(
        service_client, "ingestion_jobs", org_id, {"source_type": "upload", "source_ref": "load-test-large-repo"}
    ).data[0]

    try:
        start = time.monotonic()
        persist_extraction_result(service_client, job["id"], org_id, php_result)
        persist_extraction_result(service_client, job["id"], org_id, wp_result)
        elapsed = time.monotonic() - start

        persisted = (
            service_client.table("extracted_entities").select("id", count="exact").eq("job_id", job["id"]).execute()
        )
        expected_count = len(php_result.entities) + len(wp_result.entities)

        assert persisted.count == expected_count
        assert elapsed < 180  # generous — a batch insert of thousands of rows, not a strict SLA
    finally:
        for table in [
            "secret_findings",
            "summary_cache",
            "knowledge_summaries",
            "entity_edges",
            "extracted_entities",
            "ingestion_jobs",
        ]:
            service_client.table(table).delete().eq("org_id", org_id).execute()
