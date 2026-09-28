"""GraphifyExtractor (worker/graphify/) — mapping, file selection, mode handling, output
validation and the container command. No Docker: the container boundary is injected."""

import json
import os
from pathlib import Path

import pytest

from document_analyzer.graphify_worker import protocol as p
from document_analyzer.graphify_worker.adapter import canonicalise
from document_analyzer.worker.graphify import runner as runner_module
from document_analyzer.worker.graphify.extractor import (
    GraphifyExtractionError,
    GraphifyExtractor,
    map_output,
    select_files,
)
from document_analyzer.worker.graphify.runner import (
    GraphifyParams,
    GraphifyRunError,
    GraphifyUnavailableError,
    JobPaths,
    NoAnalysableFiles,
    build_docker_command,
)
from document_analyzer.worker.graphify.validate import (
    Expected,
    GraphifyOutputError,
    ValidatedOutput,
    validate_output,
)

from fakes import stub_colima

FIXTURES = Path(__file__).parent / "fixtures" / "graphify"


@pytest.fixture(autouse=True)
def _colima(monkeypatch, tmp_path):
    """No real Colima in unit tests: the engine resolver is stubbed (tests/fakes.py)."""
    return stub_colima(monkeypatch, tmp_path / "colima-shared")
MANIFEST = {
    "digest": "cap-digest",
    "graphify_version": "0.9.68",
    "extensions": [
        {"extension": ".py", "category": "code", "availability": "installed"},
        {"extension": ".ts", "category": "code", "availability": "installed"},
        {"extension": ".php", "category": "code", "availability": "installed"},
        {"extension": ".md", "category": "document", "availability": "installed"},
        {"extension": ".kt", "category": "code", "availability": "unavailable"},
    ],
}


def _golden():
    raw = json.loads((FIXTURES / "polyglot.raw.json").read_text())
    nodes, edges, _stats = canonicalise(raw, Path("/source"), {})
    return nodes, edges


# --- mapping ------------------------------------------------------------------------


def test_map_output_turns_callables_and_classes_into_entities_only():
    nodes, edges = _golden()
    entities, _edges, stats = map_output(nodes, edges)

    assert {(e.entity_type, e.name, e.file_path) for e in entities} >= {
        ("function", "helper", "pkg/alpha/util.py"),
        ("class", "Greeter", "pkg/alpha/util.py"),
        ("function", "greet", "pkg/alpha/util.py"),
        ("class", "Counter", "web/math.ts"),
    }
    # File and rationale nodes are reported, never silently turned into entities.
    assert stats["nodes_not_entities_by_kind"] == {"file": 8, "rationale": 1}
    assert all(e.raw_facts["extractor"] == "graphify" for e in entities)


def test_map_output_folds_class_method_structure_into_raw_facts():
    nodes, edges = _golden()
    entities, _edges, _stats = map_output(nodes, edges)
    by_name = {(e.name, e.file_path): e for e in entities}

    assert by_name[("Greeter", "pkg/alpha/util.py")].raw_facts["methods"] == ["greet"]
    assert by_name[("greet", "pkg/alpha/util.py")].raw_facts["class"] == "Greeter"
    assert by_name[("run", "pkg/main.py")].raw_facts["calls"] == ["Greeter", "helper"]


def test_map_output_keeps_calls_with_provenance_and_counts_every_dropped_relation():
    nodes, edges = _golden()
    entities, mapped, stats = map_output(nodes, edges)
    ids = {e.local_id for e in entities}

    assert mapped and all(e.edge_type == "calls" for e in mapped)
    assert all(e.from_local_id in ids and e.to_local_id in ids for e in mapped)
    assert {e.provenance["confidence"] for e in mapped} == {"EXTRACTED", "INFERRED"}
    assert all(e.provenance["extractor"] == "graphify" for e in mapped)
    # Nothing disappears without being counted.
    kept = sum(stats["edges_kept_by_relation"].values())
    dropped = sum(stats["edges_dropped_by_relation"].values())
    assert kept + dropped == stats["edges"] == len(edges)
    assert stats["edges_dropped_by_relation"]["imports (file-level endpoint)"] == 4


def test_map_output_maps_inheritance_and_entity_level_imports():
    def node(eid, kind, name):
        return {"entity_id": eid, "kind": kind, "name": name, "relative_path": "a.py", "line_start": 1, "line_end": None}

    nodes = [node("e1", "class", "Child"), node("e2", "class", "Base"), node("e3", "function", "f")]

    def edge(src, relation, tgt):
        return {"source_id": src, "target_id": tgt, "relation": relation, "confidence": "EXTRACTED"}

    entities, mapped, _ = map_output(nodes, [edge("e1", "inherits", "e2"), edge("e3", "imports_from", "e2")])

    assert [(e.edge_type, e.provenance["relation"]) for e in mapped] == [("inherits", "inherits"), ("include", "imports_from")]
    assert next(e for e in entities if e.name == "Child").raw_facts["extends"] == ["Base"]


# --- file selection -----------------------------------------------------------------


def test_select_files_leaves_php_to_the_php_extractors_and_accounts_for_the_rest():
    files = ["app.py", "plugin.php", "README.md", "big.ts", "Main.kt", "Makefile"]
    sizes = {"big.ts": 10_000}
    selected, report = select_files(files, sizes, MANIFEST, max_file_bytes=5_000)

    assert selected == ["app.py"]
    assert report["files_claimed_by_php_extractors"] == 1
    assert report["files_oversized_sample"] == ["big.ts"]
    # Non-code categories and parsers the image doesn't have are "no code extractor".
    assert report["files_without_code_extractor_by_extension"] == {".md": 1, ".kt": 1, "(none)": 1}


# --- modes --------------------------------------------------------------------------


def _unavailable(_params):
    raise GraphifyUnavailableError("docker daemon is not reachable")


def _repo(tmp_path):
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    (tmp_path / "plugin.php").write_text("<?php function g() {}\n")
    return tmp_path


def test_detect_is_false_when_off_or_only_php(tmp_path):
    (tmp_path / "plugin.php").write_text("<?php\n")
    assert GraphifyExtractor(mode="auto").detect(tmp_path) is False
    (tmp_path / "app.py").write_text("x = 1\n")
    assert GraphifyExtractor(mode="auto").detect(tmp_path) is True
    assert GraphifyExtractor(mode="off").detect(tmp_path) is False


def test_auto_mode_records_unavailable_instead_of_failing(tmp_path):
    result = GraphifyExtractor(mode="auto", check=_unavailable).extract(_repo(tmp_path))
    assert result.entities == []
    assert result.coverage["graphify"]["status"] == "unavailable"
    assert "not reachable" in result.coverage["graphify"]["reason"]


def test_required_mode_fails_the_job_when_unavailable(tmp_path):
    with pytest.raises(GraphifyExtractionError, match="required but unavailable"):
        GraphifyExtractor(mode="required", check=_unavailable).extract(_repo(tmp_path))


def test_a_run_failure_fails_the_job_even_in_auto_mode(tmp_path):
    def failing_run(*_args):
        raise GraphifyRunError("graphify extraction timed out after 900s")

    extractor = GraphifyExtractor(mode="auto", check=lambda _p: None, capabilities=lambda _i: MANIFEST, run=failing_run)
    with pytest.raises(GraphifyExtractionError, match="timed out"):
        extractor.extract(_repo(tmp_path))


def test_no_analysable_files_is_reported_not_raised(tmp_path):
    def run(*_args):
        raise NoAnalysableFiles("NO_SUPPORTED_FILES: parse_failed=1")

    extractor = GraphifyExtractor(mode="auto", check=lambda _p: None, capabilities=lambda _i: MANIFEST, run=run)
    result = extractor.extract(_repo(tmp_path))
    assert result.coverage["graphify"]["status"] == "no_analysable_files"


def test_extract_sends_only_selected_files_and_maps_the_validated_output(tmp_path):
    nodes, edges = _golden()
    seen = {}

    def run(repo_path, files, params, job_id, capability_digest):
        seen.update(files=files, job_id=job_id, digest=capability_digest)
        coverage = {"files": [{"path": "app.py", "status": "partial", "reason": "syntax errors"}],
                    "counts_by_status": {"partial": 1}}
        return ValidatedOutput(nodes=nodes, edges=edges, coverage=coverage, manifest=_manifest_obj())

    extractor = GraphifyExtractor(
        mode="auto", check=lambda _p: None, capabilities=lambda _i: MANIFEST, run=run, job_id="job-9"
    )
    result = extractor.extract(_repo(tmp_path))

    assert seen == {"files": ["app.py"], "job_id": "job-9", "digest": "cap-digest"}
    assert len(result.entities) == 11
    graphify = result.coverage["graphify"]
    assert graphify["status"] == "ran"
    assert graphify["files_claimed_by_php_extractors"] == 1
    assert graphify["not_extracted_sample"] == [{"path": "app.py", "status": "partial", "reason": "syntax errors"}]


def _manifest_obj():
    from document_analyzer.worker.graphify.validate import OutputManifest

    return OutputManifest.model_validate(
        {
            "schema_version": 1, "job_id": "j", "attempt_id": "a", "snapshot_id": "j", "source_digest": "s",
            "options_hash": "o", "capability_digest": "c", "graphify_version": "0.9.68", "graph_digest": "0" * 64,
            "counts": {}, "files": {}, "measurements": {"parse_seconds": 0.1},
        }
    )


# --- output validation ----------------------------------------------------------------


EXPECTED = Expected(job_id="job-1", attempt_id="att-1", snapshot_id="job-1", source_digest="src",
                    options_hash="opt", max_output_bytes=10_000_000)


def _write_output(root: Path, nodes, edges, **overrides):
    root.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, lines in ((p.NODES_FILE, [p.canonical_dumps(n) for n in nodes]),
                        (p.EDGES_FILE, [p.canonical_dumps(e) for e in edges])):
        data = "".join(line + "\n" for line in lines).encode()
        (root / name).write_bytes(data)
        files[name] = {"sha256": p.sha256_hex(data), "bytes": len(data), "records": len(lines)}
    coverage = p.canonical_dumps({"files": []}).encode()
    (root / p.COVERAGE_FILE).write_bytes(coverage)
    files[p.COVERAGE_FILE] = {"sha256": p.sha256_hex(coverage), "bytes": len(coverage), "records": 0}
    manifest = {
        "schema_version": 1, "job_id": "job-1", "attempt_id": "att-1", "snapshot_id": "job-1",
        "source_digest": "src", "options_hash": "opt", "capability_digest": "", "graphify_version": "0.9.68",
        "graph_digest": "0" * 64, "counts": {"nodes": len(nodes), "edges": len(edges)}, "files": files,
        **overrides,
    }
    (root / p.MANIFEST_FILE).write_text(p.canonical_dumps(manifest))


def test_validate_output_accepts_well_formed_output(tmp_path):
    nodes, edges = _golden()
    _write_output(tmp_path / "out", nodes, edges)
    output = validate_output(tmp_path / "out", EXPECTED)
    assert len(output.nodes) == len(nodes) and len(output.edges) == len(edges)


@pytest.mark.parametrize(
    "tamper, message",
    [
        (lambda root: (root / p.NODES_FILE).write_bytes((root / p.NODES_FILE).read_bytes() + b"\n"), "size mismatch"),
        (lambda root: (root / "extra.txt").write_text("x"), "unexpected file"),
        (lambda root: (root / p.MANIFEST_FILE).unlink(), "manifest missing"),
        (lambda root: os.symlink("/etc/passwd", root / p.ERROR_FILE), "symlink"),
    ],
)
def test_validate_output_rejects_tampered_output(tmp_path, tamper, message):
    nodes, edges = _golden()
    root = tmp_path / "out"
    _write_output(root, nodes, edges)
    tamper(root)
    with pytest.raises(GraphifyOutputError, match=message):
        validate_output(root, EXPECTED)


def test_validate_output_rejects_output_for_another_request(tmp_path):
    nodes, edges = _golden()
    _write_output(tmp_path / "out", nodes, edges, attempt_id="someone-else")
    with pytest.raises(GraphifyOutputError, match="attempt_id"):
        validate_output(tmp_path / "out", EXPECTED)


def test_validate_output_rejects_dangling_edges(tmp_path):
    nodes, edges = _golden()
    _write_output(tmp_path / "out", nodes[1:], edges)
    with pytest.raises(GraphifyOutputError):
        validate_output(tmp_path / "out", EXPECTED)


# --- container command and lifecycle ----------------------------------------------------


def test_docker_command_is_isolated_and_mounts_source_read_only(tmp_path):
    cmd = build_docker_command(JobPaths(tmp_path), GraphifyParams(image="img:1", memory_mb=512), "c1")
    joined = " ".join(cmd)

    for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--user 10001:10001", "--memory 512m", "--memory-swap 512m", "--pull never"):
        assert flag in joined
    assert f"type=bind,src={tmp_path}/source,dst=/source,readonly" in cmd
    assert f"type=bind,src={tmp_path}/control,dst=/control,readonly" in cmd
    assert f"type=bind,src={tmp_path}/output,dst=/output" in cmd
    assert not any(arg.startswith("--env") and "KEY" in arg for arg in cmd)
    assert cmd[-2:] == ["img:1", "analyse"]


def test_docker_command_rejects_a_comma_in_a_mount_path(tmp_path):
    with pytest.raises(ValueError, match="comma"):
        build_docker_command(JobPaths(tmp_path / "a,b"), GraphifyParams(), "c1")


class _Proc:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def _fake_docker(monkeypatch, returncode, write=None):
    calls = []

    def fake_run(cmd, **kwargs):
        assert kwargs["env"]["DOCKER_HOST"] == "unix:///fake/colima/docker.sock"  # never the ambient context
        calls.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            root = Path(next(a for a in cmd if a.endswith(",dst=/output")).split(",")[1].removeprefix("src="))
            request = json.loads((root.parent / "control" / p.REQUEST_FILE).read_text())
            if write:
                write(root, request)
            return _Proc(returncode)
        return _Proc(0)

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    return calls


def test_run_extraction_copies_only_selected_regular_files_validates_and_cleans_up(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("def a():\n    pass\n")
    (repo / "secret.env").write_text("TOKEN=x")
    workdir = tmp_path / "work"
    nodes, edges = _golden()

    def write(output, request):
        assert sorted(p.relative_to(output.parent / "source").as_posix()
                      for p in (output.parent / "source").rglob("*") if p.is_file()) == ["pkg/a.py"]
        _write_output(output, nodes, edges, attempt_id=request["attempt_id"], source_digest=request["source_digest"],
                      options_hash=request["options_hash"])

    calls = _fake_docker(monkeypatch, 0, write)
    output = runner_module.run_extraction(repo, ["pkg/a.py"], GraphifyParams(workdir=str(workdir)), "job-1")

    assert len(output.nodes) == len(nodes)
    assert any(cmd[:3] == ["docker", "rm", "-f"] for cmd in calls)  # container removed by name
    assert list(workdir.iterdir()) == []  # copied source and output deleted


def test_run_extraction_refuses_a_symlink(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "link.py").symlink_to("/etc/passwd")
    _fake_docker(monkeypatch, 0)
    with pytest.raises(GraphifyRunError, match="not a regular file"):
        runner_module.run_extraction(repo, ["link.py"], GraphifyParams(workdir=str(tmp_path / "w")), "job-1")


@pytest.mark.parametrize(
    "code, error",
    [(p.EXIT_NO_ANALYSABLE_FILES, NoAnalysableFiles), (137, GraphifyRunError), (p.EXIT_FAILED, GraphifyRunError),
     (125, GraphifyUnavailableError)],
)
def test_run_extraction_maps_worker_exit_codes(tmp_path, monkeypatch, code, error):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    _fake_docker(monkeypatch, code)
    with pytest.raises(error):
        runner_module.run_extraction(repo, ["a.py"], GraphifyParams(workdir=str(tmp_path / "w")), "job-1")
    assert list((tmp_path / "w").iterdir()) == []
