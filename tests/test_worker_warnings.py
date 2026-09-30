"""Warnings as `warn`-level activity events — design.md §2 (tasks_frontend.md A1.6)."""

from document_analyzer.worker import run as run_module
from document_analyzer.worker.extract import ExtractionResult


class _Emitter:
    def __init__(self):
        self.activity_calls = []

    def activity(self, message, refs=None, level="info"):
        self.activity_calls.append({"message": message, "refs": refs or [], "level": level})


def _graphify(**fields):
    return ExtractionResult(coverage={"graphify": fields})


def test_graphify_unavailable_warns_that_other_languages_were_not_analysed():
    emitter = _Emitter()

    run_module._warn_on_extraction_gaps(emitter, [_graphify(status="unavailable", reason="not installed")])

    [call] = emitter.activity_calls
    assert call["level"] == "warn"
    assert "not analysed" in call["message"] and "not installed" in call["message"]
    assert call["refs"] == []


def test_partly_extracted_files_warn_with_file_refs_capped_and_counted():
    emitter = _Emitter()
    sample = [{"path": f"src/f{i}.py", "status": "partial"} for i in range(25)]

    run_module._warn_on_extraction_gaps(
        emitter, [_graphify(status="ran", not_extracted_count=40, not_extracted_sample=sample)]
    )

    [call] = emitter.activity_calls
    assert call["level"] == "warn"
    assert call["message"].startswith("40 files were only partly extracted")
    assert [r["value"] for r in call["refs"]] == [f"src/f{i}.py" for i in range(run_module.MAX_WARNING_FILES)]
    assert all(r["kind"] == "file" for r in call["refs"])


def test_a_single_file_uses_the_singular():
    emitter = _Emitter()

    run_module._warn_on_extraction_gaps(
        emitter, [_graphify(status="ran", not_extracted_count=1, not_extracted_sample=[{"path": "a.py"}])]
    )

    assert emitter.activity_calls[0]["message"].startswith("1 file was only partly")


def test_a_clean_extraction_emits_no_warning():
    emitter = _Emitter()

    run_module._warn_on_extraction_gaps(
        emitter,
        [_graphify(status="ran", not_extracted_count=0), ExtractionResult(), _graphify(status="no_candidate_files")],
    )

    assert emitter.activity_calls == []


def test_skipped_evidence_files_warn_and_none_means_no_warning():
    emitter = _Emitter()
    run_module._warn_on_evidence_gaps(emitter, {"files_skipped_count": 0, "files_skipped": []})
    assert emitter.activity_calls == []

    run_module._warn_on_evidence_gaps(
        emitter, {"files_skipped_count": 2, "files_skipped": [{"path": "big.sql", "reason": "unreadable"}, {"path": "x.bin"}]}
    )

    [call] = emitter.activity_calls
    assert call["level"] == "warn"
    assert call["message"].startswith("2 files were skipped when building source evidence")
    assert [r["value"] for r in call["refs"]] == ["big.sql", "x.bin"]
