"""Load test for the planner at 5,000-10,000+ entities — tasks_agent2.md Phase 6.

Reuses `tests/test_load_large_repo.py`'s synthetic multi-plugin WordPress site
(real PhpExtractor/WordPressExtractor, real Agent 1 persist step into a fake
Supabase client), then runs the real planner worker (`process_planning_job`, all
four stages) over it. Only the LLM and the embedder are faked; the fake LLM
counts overlapping calls so the report stage's fan-out cap can be checked.

Stage timings and peak memory are printed (run with `-s`) and recorded in
design_agent2.md section 15. Assertions are loose bounds, not an SLA: the point is
that the run finishes, every entity is planned exactly once, and the fan-out
never exceeds the cap.
"""

import json
import os
import shutil
import sys
import threading
import time

import pytest

from document_analyzer.agent2.worker import report as report_module
from document_analyzer.agent2.worker import run as run_module
from document_analyzer.worker.persist import persist_extraction_result
from document_analyzer.worker.php_extractor import PhpExtractor
from document_analyzer.worker.wordpress_extractor import WordPressExtractor

from fakes import FakeClient
from test_load_large_repo import _generate_wordpress_like_repo

pytestmark = pytest.mark.skipif(shutil.which("php") is None, reason="needs the php binary for Agent 1's extractors")

PARAMS = {"seed": 42, "alpha": 0.5, "max_unit_size": 25, "hub_percentile": 99, "leiden_resolution": 1.0}
HOLD_SECONDS = 0.005  # long enough for calls to overlap, short enough for hundreds of reports


def _peak_rss_mb() -> float:
    """Peak resident memory of this process so far (it only ever grows)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                )
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        return counters.PeakWorkingSetSize / 1e6
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1e6 if sys.platform == "darwin" else peak / 1e3


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class CountingLLM:
    """Real-shaped responses; tracks how many report calls overlap."""

    def __init__(self):
        self.chat = type("Chat", (), {"completions": self})()
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak_in_flight = 0
        self.calls = 0

    def create(self, **kwargs):
        if "fact-checking" in kwargs["messages"][0]["content"]:
            return _Resp(json.dumps({"grounded": True, "issues": []}))
        with self._lock:
            self.calls += 1
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        time.sleep(HOLD_SECONDS)
        with self._lock:
            self.in_flight -= 1
        return _Resp(json.dumps({"label": "A unit", "summary_text": "Does things."}))


def test_planner_completes_at_load_scale_and_report_fanout_respects_the_cap(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "large-wp-repo"
    _generate_wordpress_like_repo(repo)

    client = FakeClient()
    client.cascades["clusters"] = [
        ("clusters", "parent_cluster_id"),
        ("cluster_members", "cluster_id"),
        ("cluster_reports", "cluster_id"),
        ("cluster_edges", "from_cluster_id"),
    ]
    for extractor in (PhpExtractor(), WordPressExtractor()):
        persist_extraction_result(client, "job-1", "org-1", extractor.extract(repo))
    entity_count = len(client.table("extracted_entities").rows)
    assert entity_count >= 5000, f"only {entity_count} entities, not load-test scale"
    client.table("planning_jobs").insert(
        {"id": "pj-1", "org_id": "org-1", "project_id": "project-1", "source_job_id": "job-1", "status": "queued",
         "progress_stage": None, "params": PARAMS}
    ).execute()

    llm = CountingLLM()
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-test")
    monkeypatch.setattr(run_module, "get_azure_openai_client", lambda: llm)
    monkeypatch.setattr(run_module, "embed_text", lambda text: [0.0] * 384)
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)

    timings: dict[str, float] = {}

    def timed(stage_name, fn):
        def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                timings[stage_name] = time.monotonic() - start

        return wrapper

    stages = [(name, timed(name, fn)) for name, fn in run_module._stages()]
    monkeypatch.setattr(run_module, "_stages", lambda: stages)

    rss_before = _peak_rss_mb()
    start = time.monotonic()
    run_module.process_planning_job("pj-1", "org-1")
    total = time.monotonic() - start
    rss_peak = _peak_rss_mb()

    [job] = client.table("planning_jobs").rows
    assert (job["status"], job.get("error")) == ("done", None)

    # Every canonical entity is planned in exactly one unit.
    members = client.table("cluster_members").rows
    assert len({m["entity_id"] for m in members}) == len(members)
    units = [c for c in client.table("clusters").rows if c["level"] == "unit"]
    phases = [c for c in client.table("clusters").rows if c["level"] == "phase"]
    assert len(client.table("migration_plan_items").rows) == len(units)

    # The fan-out cap bounds report concurrency, and the load really did fan out.
    cap = report_module.REPORT_CONCURRENCY
    assert 1 < llm.peak_in_flight <= cap
    assert llm.calls == len(units) + len(phases)

    assert total < 600  # generous: not an SLA

    with capsys.disabled():
        print(
            f"\nPLANNER LOAD: entities={entity_count} units={len(units)} phases={len(phases)} "
            f"report_calls={llm.calls} cap={cap} peak_in_flight={llm.peak_in_flight}\n"
            f"  stage seconds: " + ", ".join(f"{k}={v:.1f}" for k, v in timings.items()) + f", total={total:.1f}\n"
            f"  peak RSS: {rss_peak:.0f} MB (was {rss_before:.0f} MB before the planner ran)"
        )
