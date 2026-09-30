"""Larger executor run — tasks_agent3.md Phase 6, fifth task; settles specs_agent3.md
§9's open questions on `max_attempts`, the sandbox timeout, and per-phase parallelism.

`tests/test_load_large_repo.py`'s generator (a synthetic multi-plugin WordPress
site, 5,000+ entities) needs the real `php` binary through Agent 1, then real
Leiden clustering through Agent 2, to produce a plan — neither is available on
every dev machine (this one has no `php`). Rather than block this task on that,
a smaller synthetic plan is built directly at the DB-row level (the same
technique `test_agent3_units.py`/`test_agent3_redelivery.py` already use to
exercise `run_prepare_stage` for real without `php`): 12 small, representative
WordPress-utility-style PHP functions, split into two phases so half the units
are mutually independent (the shape that would let per-phase parallelism help)
and the other half each depend on one phase-1 unit (to exercise a real
dependency-context payload, not just isolated units).

Unlike every other Agent 3 test, this one makes **real Azure OpenAI calls**
(genuine cost, not `FakeLLM`) through the real sandbox (genuine Docker
containers, not a monkeypatched `run_pytest`) — it's a measurement run, not a
regression test, so it's opt-in only: skipped unless `AGENT3_RUN_LOAD_TEST=1`
is set. Run with `-s` to see the per-unit report printed at the end; the
numbers get transcribed into `design_agent3.md` by hand, not read back
programmatically by anything.

The real Azure OpenAI client is wrapped (`_MeteredLLM`) to record each call's
wall time and token usage without changing what's actually sent — genuine
requests and genuine cost either way, just observed. `mark_unit_running`/
`update_unit_execution` are similarly wrapped to time each unit end to end
(LLM calls + sandbox run together, exactly what a reviewer waits on).
"""

import os
import time
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv()

from document_analyzer.agent3.worker import units as units_module  # noqa: E402
from document_analyzer.agent3.worker.assemble import run_assemble_stage, run_final_test_stage  # noqa: E402
from document_analyzer.agent3.worker.prepare import run_prepare_stage  # noqa: E402
from document_analyzer.agent3.worker.run import ExecutionState  # noqa: E402
from document_analyzer.agent3.worker.sandbox import SandboxParams, SandboxUnavailableError, check_sandbox_available  # noqa: E402
from document_analyzer.supabase_client import insert_scoped  # noqa: E402
from document_analyzer.worker.summarize import get_azure_openai_client as get_real_azure_openai_client  # noqa: E402

from agent3_fixtures import make_tarball as _make_tarball  # noqa: E402
from agent3_fixtures import seed_plan as _seed_plan  # noqa: E402
from fakes import FakeClient  # noqa: E402

ORG = "org-1"
JOB_ID = "ej-load-1"

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT3_RUN_LOAD_TEST") != "1",
    reason="opt-in only: makes real Azure OpenAI calls (real cost) and needs Docker + the runner image; set AGENT3_RUN_LOAD_TEST=1 to run",
)

PARAMS = {
    "model_deployment": os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.6-luna"),
    "max_attempts": 3,
    "max_context_chars": 60_000,
    "sandbox_timeout_seconds": 120,
    "sandbox_memory_mb": 1024,
    "runner_image": "galvanise-agent3-runner:0.1",
}

# --------------------------------------------------------------------------- #
# 12 small, representative WordPress-utility-style functions: 8 independent
# (phase 1), 4 each depending on one phase-1 function (phase 2).
# --------------------------------------------------------------------------- #

_INDEPENDENT = [
    ("format_price", ["$amount = floatval($input);", "return '$' . number_format($amount, 2);"]),
    ("sanitize_slug", ["$slug = strtolower($input);", "$slug = preg_replace('/[^a-z0-9-]+/', '-', $slug);", "return trim($slug, '-');"]),
    ("validate_email", ["return filter_var($input, FILTER_VALIDATE_EMAIL) !== false;"]),
    ("truncate_excerpt", ["$max = 120;", "if (strlen($input) <= $max) {", "    return $input;", "}", "return substr($input, 0, $max) . '...';"]),
    ("normalize_phone", ["$digits = preg_replace('/\\\\D+/', '', $input);", "return substr($digits, 0, 15);"]),
    ("slugify_tag", ["$tag = trim($input);", "return str_replace(' ', '-', strtolower($tag));"]),
    (
        "format_file_size",
        [
            "$bytes = intval($input);",
            "$units = ['B', 'KB', 'MB', 'GB'];",
            "$i = 0;",
            "while ($bytes >= 1024 && $i < count($units) - 1) {",
            "    $bytes /= 1024;",
            "    $i++;",
            "}",
            "return round($bytes, 1) . ' ' . $units[$i];",
        ],
    ),
    ("mask_credit_card", ["$digits = preg_replace('/\\\\D+/', '', $input);", "return str_repeat('*', max(0, strlen($digits) - 4)) . substr($digits, -4);"]),
]

_DEPENDENT = [
    ("format_price_with_tax", "format_price", ["$taxed = floatval($input) * 1.08;", "return format_price($taxed);"]),
    ("build_product_slug", "sanitize_slug", ["$slug = sanitize_slug($input);", "return 'product-' . $slug;"]),
    ("validate_contact_email", "validate_email", ["if (!validate_email($input)) {", "    return false;", "}", "return true;"]),
    ("format_upload_size", "format_file_size", ["$size = format_file_size($input);", "return 'Uploaded: ' . $size;"]),
]


def _php_source(name: str, body_lines: list[str]) -> tuple[str, int, int]:
    """Returns (source, line, end_line) — `line` is the `function` line, `end_line`
    the closing brace, 1-indexed, matching raw_facts's real-extractor convention."""
    header = ["<?php", "", f"function {name}($input) {{"]
    lines = [*header, *[f"    {l}" for l in body_lines], "}"]
    return "\n".join(lines) + "\n", len(header), len(header) + len(body_lines) + 1


def _contract(name: str, entity_id: str, *, outgoing_to: str | None = None) -> dict:
    return {
        "public_interface": [{"entity_id": entity_id, "name": name, "kind": "function", "class": None, "params": ["input"], "file_path": f"{name}.php"}],
        "incoming_call_sites": [],
        "outgoing_dependencies": [{"to_unit_id": outgoing_to, "call_count": 1, "hook_binding_count": 0}] if outgoing_to else [],
        "external_calls": [],
        "hooks_bound": [],
        "routes_bound": [],
        "side_effect_signals": [],
        "unverified_member_ids": [],
        "merged_cluster_ids": [],
    }


def _seed_unit(client, plan, job, *, name: str, phase_order: int, unit_order: int, outgoing_to: str | None, files: dict) -> str:
    cluster_id, entity_id = f"cluster-{name}", f"e-{name}"
    insert_scoped(client, "cluster_members", ORG, {"cluster_id": cluster_id, "entity_id": entity_id, "canonical_entity_id": entity_id})
    body_lines = files["body_lines"]
    source, line, end_line = _php_source(name, body_lines)
    files["tarball"][f"{name}.php"] = source.encode("utf-8")
    insert_scoped(
        client, "extracted_entities", ORG,
        {"id": entity_id, "job_id": job["id"], "entity_type": "function", "name": name, "file_path": f"{name}.php", "raw_facts": {"line": line, "end_line": end_line, "params": ["input"]}},
    )
    insert_scoped(
        client, "knowledge_summaries", ORG,
        {"job_id": job["id"], "level": "entity", "entity_id": entity_id, "summary_text": f"Utility function {name}.", "risk_level": "low", "grounding_status": "verified"},
    )
    insert_scoped(client, "cluster_reports", ORG, {"cluster_id": cluster_id, "summary_text": f"Report for {name}.", "risk_level": "low", "grounding_status": "verified"})
    insert_scoped(
        client, "migration_plan_items", ORG,
        {"plan_id": plan["id"], "cluster_id": cluster_id, "phase_cluster_id": f"phase-{phase_order}", "phase_order": phase_order, "unit_order": unit_order, "test_contract": _contract(name, entity_id, outgoing_to=outgoing_to)},
    )
    insert_scoped(
        client, "unit_executions", ORG,
        {"execution_job_id": JOB_ID, "cluster_id": cluster_id, "phase_cluster_id": f"phase-{phase_order}", "phase_order": phase_order, "unit_order": unit_order, "status": "pending", "attempts": 0, "flags": []},
    )
    return cluster_id


def _build_synthetic_plan(client) -> tuple[dict, dict]:
    job, plan, _project = _seed_plan(client)
    files = {"tarball": {}, "body_lines": None}
    name_to_cluster = {}
    for order, (name, body_lines) in enumerate(_INDEPENDENT, start=1):
        files["body_lines"] = body_lines
        name_to_cluster[name] = _seed_unit(client, plan, job, name=name, phase_order=1, unit_order=order, outgoing_to=None, files=files)
    for order, (name, dep_name, body_lines) in enumerate(_DEPENDENT, start=1):
        files["body_lines"] = body_lines
        _seed_unit(client, plan, job, name=name, phase_order=2, unit_order=order, outgoing_to=name_to_cluster[dep_name], files=files)
    client.storage.from_("agent1-snapshots").upload(job["source_snapshot_path"], _make_tarball(files["tarball"]))
    insert_scoped(client, "execution_jobs", ORG, {"id": JOB_ID, "project_id": "p1", "plan_id": plan["id"], "source_job_id": job["id"], "status": "running", "params": PARAMS})
    return plan, job


def _execution_job(plan: dict, job: dict) -> dict:
    return {"id": JOB_ID, "org_id": ORG, "plan_id": plan["id"], "source_job_id": job["id"], "params": PARAMS}


# --------------------------------------------------------------------------- #
# Metering
# --------------------------------------------------------------------------- #


class _MeteredLLM:
    """Wraps the real Azure OpenAI client's `chat.completions.create` — genuine
    requests and genuine cost either way, just observed: wall time plus
    prompt/completion/total token usage, tagged by which unit and which kind of
    call (generate vs. behaviour) it was."""

    def __init__(self, real_client):
        self._real = real_client
        self.calls: list[dict] = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        messages = kwargs["messages"]
        system = messages[0]["content"]
        kind = "behaviour" if "writing pytest behaviour tests" in system else "generate"
        unit_slug = system.rsplit("unit_slug is: ", 1)[-1].strip()
        start = time.monotonic()
        response = self._real.chat.completions.create(**kwargs)
        elapsed = time.monotonic() - start
        usage = getattr(response, "usage", None)
        self.calls.append(
            {
                "unit_slug": unit_slug,
                "kind": kind,
                "duration_s": round(elapsed, 3),
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        )
        return response


def _skip_unless_sandbox_ready():
    try:
        check_sandbox_available(SandboxParams())
    except SandboxUnavailableError as exc:
        pytest.skip(f"sandbox not available: {exc}")


def _print_report(unit_timing: dict, llm: _MeteredLLM, units_elapsed: float, assemble_elapsed: float, rows: list[dict]) -> None:
    print("\n\n=== Agent 3 load run report (tasks_agent3.md Phase 6, task 5) ===\n")
    print(f"{'cluster_id':30} {'status':18} {'attempts':8} {'wall_s':8}")
    for cluster_id, timing in sorted(unit_timing.items()):
        wall_s = round(timing["end"] - timing["start"], 2) if "end" in timing else None
        print(f"{cluster_id:30} {timing.get('status', '?'):18} {timing.get('attempts', '?'):<8} {wall_s}")

    print("\n--- LLM calls ---")
    print(f"{'unit_slug':22} {'kind':11} {'duration_s':11} {'prompt':8} {'completion':11} {'total':8}")
    for call in llm.calls:
        print(f"{call['unit_slug']:22} {call['kind']:11} {call['duration_s']:<11} {call['prompt_tokens']!s:8} {call['completion_tokens']!s:11} {call['total_tokens']!s:8}")

    total_tokens = sum(c["total_tokens"] or 0 for c in llm.calls)
    total_llm_time = sum(c["duration_s"] for c in llm.calls)
    statuses = {r["status"] for r in rows}
    attempts = [r["attempts"] for r in rows]
    print("\n--- Totals ---")
    print(f"units: {len(rows)}, statuses seen: {sorted(statuses)}")
    print(f"attempts: min={min(attempts)} max={max(attempts)} avg={sum(attempts) / len(attempts):.2f}")
    print(f"LLM calls: {len(llm.calls)}, total tokens: {total_tokens}, total LLM wall time: {total_llm_time:.1f}s")
    print(f"units stage wall time: {units_elapsed:.1f}s (serial, {len(rows)} units)")
    print(f"assemble+final_test wall time: {assemble_elapsed:.1f}s")
    print(
        "\nNote: $ cost depends on this Azure deployment's own per-token rate "
        f"({PARAMS['model_deployment']!r}), which this script has no way to know — "
        "multiply total tokens by that rate to get a dollar figure.\n"
    )


def test_agent3_load_run_records_per_unit_time_tokens_and_cost(monkeypatch):
    _skip_unless_sandbox_ready()
    client = FakeClient(org_id=ORG)
    plan, job = _build_synthetic_plan(client)
    execution_job = _execution_job(plan, job)

    state = ExecutionState()
    run_prepare_stage(client, execution_job, state)
    assert len(state.items) == len(_INDEPENDENT) + len(_DEPENDENT)

    llm = _MeteredLLM(get_real_azure_openai_client())
    monkeypatch.setattr(units_module, "get_azure_openai_client", lambda: llm)

    unit_timing: dict[str, dict] = {}
    real_mark_unit_running = units_module.mark_unit_running
    real_update_unit_execution = units_module.update_unit_execution

    def timed_mark_unit_running(c, execution_job_id, org_id, cluster_id):
        unit_timing[cluster_id] = {"start": time.monotonic()}
        return real_mark_unit_running(c, execution_job_id, org_id, cluster_id)

    def timed_update_unit_execution(c, execution_job_id, org_id, cluster_id, **fields):
        if cluster_id in unit_timing:
            unit_timing[cluster_id]["end"] = time.monotonic()
            unit_timing[cluster_id]["status"] = fields.get("status")
            unit_timing[cluster_id]["attempts"] = fields.get("attempts")
        return real_update_unit_execution(c, execution_job_id, org_id, cluster_id, **fields)

    monkeypatch.setattr(units_module, "mark_unit_running", timed_mark_unit_running)
    monkeypatch.setattr(units_module, "update_unit_execution", timed_update_unit_execution)

    units_start = time.monotonic()
    units_module.run_units_stage(client, execution_job, state)
    units_elapsed = time.monotonic() - units_start

    assemble_start = time.monotonic()
    run_assemble_stage(client, execution_job, state)
    run_final_test_stage(client, execution_job, state)
    assemble_elapsed = time.monotonic() - assemble_start

    rows = client.table("unit_executions").rows
    assert len(rows) == len(_INDEPENDENT) + len(_DEPENDENT)
    assert all(r["status"] in ("passed", "failed_verification", "error") for r in rows)

    _print_report(unit_timing, llm, units_elapsed, assemble_elapsed, rows)

    job_row = client.table("execution_jobs").rows[0]
    print(f"final_test_summary: {job_row.get('final_test_summary')}")
    print(f"counts: {job_row.get('counts')}")
