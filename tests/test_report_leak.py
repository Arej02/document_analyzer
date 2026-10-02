"""Leak test (tasks_report.md Phase 5, design_report.md §8.1): nothing that must not leave the system
appears in the report JSON, the HTML or the text of the PDF.

The pipeline is real where it matters: Agent 1's `process_job` runs the real PHP extractor and the real
evidence/chunking, secret redaction and persistence over a fixture repo (tests/fixtures/report_leak_repo)
that holds fake secrets and distinctive source lines. Only the LLM calls are fakes, and they are
*adversarial*: summaries quote source lines verbatim and one embeds a secret, as a careless model might.
The reporter worker then runs its real stages (collect, estimate, summarize, the content gate, publish)
for both tiers, and the stored `report_json` is rendered to HTML and, when the renderer image is built,
to a PDF. The Full tier needs a plan and calibration history, which are seeded rows (Agent 2 and 3 have
their own pipelines and tests); the history belongs to *another org* and carries distinctive costs.

Must never appear: the secrets, any run of source text (checked here with a word-window test that is
independent of the gate's own n-gram check), any `[REDACTED:` marker, any internal cost value or
cost-bearing field name, anything of another org's. The sensitivity tests bypass the gate and prove this
test would notice.
"""

import copy
import io
import json
import re
import shutil
from pathlib import Path

import pytest

from document_analyzer.report import leak_gate
from document_analyzer.report.pdf import PdfParams, render_pdf
from document_analyzer.report.render import render_report_html
from document_analyzer.report.worker import run as report_run
from document_analyzer.shared import container_engine
from document_analyzer.worker import embed as embed_module
from document_analyzer.worker import run as agent1_run
from document_analyzer.worker.fetch import FetchResult
from document_analyzer.worker.ground import GroundingResult
from document_analyzer.worker.ground_retry import GroundingRetryResult
from document_analyzer.worker.summarize import EntitySummary
from document_analyzer.worker.summarize_module import ModuleSummary
from document_analyzer.worker.summarize_subsystem import RepoSummary, SubsystemSummary
from tests.fakes import FakeClient

REPO = Path(__file__).parent / "fixtures" / "report_leak_repo"
PROJECT_FIXTURE = Path(__file__).parent / "fixtures" / "report_project" / "project.json"
ORG, OTHER_ORG, JOB, PROJECT = "org-1", "org-2", "job-1", "project-1"

SECRETS = ["AKIAIOSFODNN7EXAMPLE", "sk_live_51LeakCanaryAbCdEfGh1234", "hunter22-LEAK-canary", "hunter22", "LeakCanary"]
# Source text that is not an entity name or a path, so it has no legitimate reason to be in a report.
SOURCE_LINES = [
    "$this->ledger->reconcile_quarterly_zebra_totals($order, 'XQ-7731-ZEBRA')",
    "DELETE FROM ledger_archive_nebula WHERE order_ref",
    "Remember to flush the quokka cache before invoicing the harbourmaster account",
    "papaya consignment totals",
]
SOURCE_TOKENS = ["reconcile_quarterly_zebra_totals", "XQ-7731-ZEBRA", "ledger_archive_nebula", "quokka", "harbourmaster", "papaya consignment", "gateway.internal"]
OTHER_ORG_CANARY = "OTHERORG-CANARY-LABEL"
COST_FIELD_NAMES = ["ai_cost", "cost_usd", "price_basis", "median_cost", "unpriced", "prompt_tokens", "completion_tokens", "total_tokens", "cached_prompt_tokens", "llm_invocations", "report_internal_estimates"]
HISTORY_COSTS = [round(4.2137 + i * 0.0113, 4) for i in range(24)]

def _renderer_ready() -> bool:
    try:
        container_engine.verify_engine()
        return container_engine.image_present(PdfParams.from_job_params().image)
    except Exception:  # noqa: BLE001
        return False


needs_renderer = pytest.mark.skipif(not _renderer_ready(), reason="renderer image not built (uv run document-analyzer-build-report-image)")


LEAKY_EXEC_SUMMARY = (
    "A small ledger application. It calls $this->ledger->reconcile_quarterly_zebra_totals($order, 'XQ-7731-ZEBRA') "
    "and connects with hunter22-LEAK-canary, key AKIAIOSFODNN7EXAMPLE. Remember to flush the quokka cache before invoicing the harbourmaster account."
)


class LeakyLLM:
    """The report's executive-summary model: a summary that repeats source and secrets, and a checker that agrees."""

    def __init__(self):
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        from document_analyzer.report import exec_summary

        if kwargs["messages"][0]["content"] == exec_summary.INSTRUCTIONS:
            content = json.dumps({"summary": LEAKY_EXEC_SUMMARY})
        else:
            content = json.dumps({"grounded": True, "issues": []})
        message = type("M", (), {"content": content})()
        return type("R", (), {"choices": [type("C", (), {"message": message})()], "usage": None, "model": None})()


_ENTITY_SUMMARIES = {
    # name: (text, risk, factors). The leaky ones quote the source or carry a secret.
    "reconcile": ("It computes totals with $this->ledger->reconcile_quarterly_zebra_totals($order, 'XQ-7731-ZEBRA') and applies a filter.", "high", ["dynamic filter"]),
    "archive": ("Runs DELETE FROM ledger_archive_nebula WHERE order_ref = ' . $orderId . ' directly against the database.", "high", ["raw sql built by concatenation"]),
    "connect_gateway": ("Opens a PDO connection to gateway.internal with 'password' => 'hunter22-LEAK-canary' in its config.", "high", ["hardcoded credential"]),
    "summarise_invoice_papaya": ("Formats an invoice line for display.", "medium", []),
    "Ledger": ("Groups the ledger reconciliation and archiving operations.", "medium", []),
}


def _fake_summarize_entity(entity, client=None, excerpt=None):
    text, risk, factors = _ENTITY_SUMMARIES.get(entity.name, (f"Summary of {entity.name}.", "low", []))
    return EntitySummary(text, risk, factors)


def _grounded(facts, generate, max_retries=None, client=None):
    return GroundingRetryResult(summary=generate(), grounding=GroundingResult(verdict="grounded", issues=[]), retries_used=0)


def _seed_plan_and_history(client: FakeClient) -> None:
    """A plan for the Full tier (rows of the report fixture project) and calibration history that
    belongs to another org: 24 completed, priced units in the xs/low bucket, with distinctive costs."""
    for table in ("planning_jobs", "migration_plans", "migration_plan_items", "clusters", "cluster_reports", "cluster_edges", "cluster_members"):
        client.table(table).rows.extend(copy.deepcopy(json.loads(PROJECT_FIXTURE.read_text(encoding="utf-8"))[table]))
    client.table("migration_plans").rows[0]["project_id"] = PROJECT
    client.table("clusters").rows.append({"id": "h1", "org_id": OTHER_ORG, "kind": "community", "size": 3, "label": OTHER_ORG_CANARY})
    client.table("cluster_reports").rows.append({"cluster_id": "h1", "org_id": OTHER_ORG, "risk_level": "low", "grounding_status": "verified"})
    for i, cost in enumerate(HISTORY_COSTS):
        execution_job = f"other-ej-{i}"
        client.table("unit_executions").rows.append(
            {"id": f"ue-{i}", "org_id": OTHER_ORG, "execution_job_id": execution_job, "cluster_id": "h1", "status": "passed", "attempts": 1,
             "started_at": "2026-09-30T10:00:00+00:00", "finished_at": "2026-09-30T10:03:00+00:00", "updated_at": "2026-09-30T10:03:00+00:00"}
        )
        client.table("llm_cost_by_execution_unit").rows.append(
            {"job_id": execution_job, "cluster_id": "h1", "org_id": OTHER_ORG, "cost_usd": cost, "unpriced_calls": 0}
        )


def _run_agent1(monkeypatch, client: FakeClient) -> None:
    client.table("projects").insert({"id": PROJECT, "org_id": ORG, "source_type": "github", "source_ref": "https://github.com/example/leak-repo.git"}).execute()
    client.table("ingestion_jobs").insert(
        {"id": JOB, "org_id": ORG, "project_id": PROJECT, "source_type": "github", "source_ref": "https://github.com/example/leak-repo.git", "status": "queued"}
    ).execute()

    def fetch(_client, _job, dest_dir):
        repo = Path(dest_dir) / "repo"
        shutil.copytree(REPO, repo)
        return FetchResult(repo, "abc1234")

    monkeypatch.setenv("AGENT1_GRAPHIFY", "off")
    monkeypatch.setattr(agent1_run, "get_service_role_client", lambda: client)
    monkeypatch.setattr(agent1_run, "fetch_source", fetch)
    monkeypatch.setattr(embed_module, "embed_texts", lambda texts: [[float(len(t)), 0.0] for t in texts])
    monkeypatch.setattr(agent1_run, "summarize_entity", _fake_summarize_entity)
    monkeypatch.setattr(agent1_run, "check_grounding_with_retry", _grounded)
    monkeypatch.setattr(agent1_run, "summarize_module", lambda fp, es, client=None: ModuleSummary(f"Module {fp}: $this->ledger->reconcile_quarterly_zebra_totals($order, 'XQ-7731-ZEBRA')", "medium", []))
    monkeypatch.setattr(agent1_run, "summarize_subsystem", lambda n, c, client=None: SubsystemSummary(f"Subsystem {n} keeps the quokka cache warm", "medium", []))
    monkeypatch.setattr(agent1_run, "summarize_repo", lambda c, client=None: RepoSummary(LEAKY_EXEC_SUMMARY, "high", []))
    monkeypatch.setattr(agent1_run, "get_azure_openai_client", lambda: object())
    agent1_run.process_job(JOB, ORG)


def _run_reports(monkeypatch, client: FakeClient, *, bypass_gate: bool = False, bypass_rendered: bool = False, expect_done: bool = True) -> dict[str, dict]:
    monkeypatch.setattr(report_run, "get_service_role_client", lambda: client)
    monkeypatch.setattr(report_run, "get_azure_openai_client", lambda: LeakyLLM())
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-test")
    if bypass_gate:
        monkeypatch.setattr(leak_gate, "apply_content_gate", lambda report, index: (report, [], []))
    if bypass_rendered:
        monkeypatch.setattr(leak_gate, "scan_rendered", lambda text, index=None: [])
    reports: dict[str, dict] = {}
    for tier in ("xray", "full"):
        job_id = f"rj-{tier}"
        client.table("report_generation_jobs").insert(
            {"id": job_id, "org_id": ORG, "project_id": PROJECT, "tier": tier, "source_job_id": JOB,
             "plan_id": "plan-1" if tier == "full" else None, "plan_version": 3 if tier == "full" else None,
             "status": "queued", "progress_stage": None, "params": {}, "created_by": "user-1"}
        ).execute()
        report_run.process_report_job(job_id, ORG)
        job = next(r for r in client.table("report_generation_jobs").rows if r["id"] == job_id)
        if not expect_done:
            continue
        assert job["status"] == "done", job.get("error")
        reports[tier] = next(r for r in client.table("assessment_reports").rows if r["generation_job_id"] == job_id)["report_json"]
    return reports


class World:
    def __init__(self, client, reports):
        self.client, self.reports = client, reports

    @property
    def chunks(self) -> list[str]:
        return [row["content"] for row in self.client.table("source_chunks").rows]

    @property
    def internal(self) -> dict:
        [row] = self.client.table("report_internal_estimates").rows
        return row

    def haystacks(self) -> dict[str, str]:
        out = {}
        for tier, report in self.reports.items():
            out[f"{tier} json"] = json.dumps(report, ensure_ascii=False)
            out[f"{tier} html"] = render_report_html(report)
        out["report events"] = json.dumps([e for e in self.client.table("workflow_events").rows if e["workflow_kind"] == "report_generation"])
        return out

    @property
    def stored(self) -> dict[str, bytes]:
        """Every object the worker put in the `reports` bucket, by path."""
        return dict(self.client.storage.from_("reports")._objects)


def build_world(bypass_gate: bool = False, bypass_rendered: bool = False, expect_done: bool = True) -> World:
    monkeypatch = pytest.MonkeyPatch()
    try:
        client = FakeClient(ORG)
        _run_agent1(monkeypatch, client)
        job = client.table("ingestion_jobs").rows[0]
        assert job["status"] == "done", job.get("error")
        _seed_plan_and_history(client)
        return World(client, _run_reports(monkeypatch, client, bypass_gate=bypass_gate, bypass_rendered=bypass_rendered, expect_done=expect_done))
    finally:
        monkeypatch.undo()


@pytest.fixture(scope="module")
def world() -> World:
    return build_world()


# --- the independent leak detector -------------------------------------------------------------------------


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


def source_windows(chunks: list[str], allowed_text: str, n: int = 6) -> set[str]:
    """Every n-word window of the stored source that is not just names and paths a report may show."""
    allowed = " ".join(_words(allowed_text))
    found = set()
    for chunk in chunks:
        words = _words(chunk)
        for i in range(len(words) - n + 1):
            window = " ".join(words[i : i + n])
            if window not in allowed:
                found.add(window)
    return found


def leaks_in(text: str, world: World) -> list[str]:
    """Why `text` is a leak, or []: a secret, a marker, source text, an internal cost value or field name."""
    problems = []
    lowered = text.lower()
    problems += [f"secret {s!r}" for s in SECRETS if s.lower() in lowered]
    problems += ["redaction marker"] if "[redacted" in lowered or "redacted:" in lowered else []
    problems += [f"source token {t!r}" for t in SOURCE_TOKENS + SOURCE_LINES if t.lower() in lowered]
    row = world.internal
    cost_values = [str(row[k]) for k in ("ai_cost_p50", "ai_cost_p90") if row[k] is not None] + [f"{c}" for c in HISTORY_COSTS]
    problems += [f"internal cost {v}" for v in cost_values if re.search(rf"(?<![\d.]){re.escape(v)}(?!\d)", text)]
    problems += [f"cost field {f!r}" for f in COST_FIELD_NAMES if f in lowered]
    problems += [f"other org {OTHER_ORG_CANARY}"] if OTHER_ORG_CANARY.lower() in lowered else []
    entities = world.client.table("extracted_entities").rows
    allowed_text = " ".join([e["name"] for e in entities] + [e["file_path"] for e in entities])
    words_text = " ".join(_words(text))
    problems += [f"source window {w!r}" for w in sorted(source_windows(world.chunks, allowed_text)) if w in words_text][:5]
    return problems


# --- preconditions: the test would be vacuous without these -----------------------------------------------------


def test_pipeline_really_extracted_and_stored_source_and_redacted_the_secrets(world):
    names = {e["name"] for e in world.client.table("extracted_entities").rows}
    assert {"reconcile", "archive", "connect_gateway", "Ledger"} <= names
    chunks = "\n".join(world.chunks)
    assert "reconcile_quarterly_zebra_totals" in chunks and "ledger_archive_nebula" in chunks  # source is in the database
    assert "hunter22-LEAK-canary" not in chunks and "[REDACTED:" in chunks  # secrets were redacted on the way in
    assert world.client.table("secret_findings").rows  # and recorded as findings


def test_the_fakes_really_leak_into_the_data_the_report_reads(world):
    summaries = " ".join(r["summary_text"] for r in world.client.table("knowledge_summaries").rows)
    for leaked in ("reconcile_quarterly_zebra_totals", "ledger_archive_nebula", "hunter22-LEAK-canary"):
        assert leaked in summaries


def test_the_internal_estimate_has_distinctive_priced_numbers(world):
    row = world.internal
    assert row["n"] >= 20 and row["ai_cost_p50"] is not None and row["ai_cost_p90"] is not None
    # The estimate is a total over the plan's units, so it is a multiple of the per-unit history costs.
    assert 0 < row["ai_cost_p50"] <= row["ai_cost_p90"]
    assert row["ai_cost_p50"] > max(HISTORY_COSTS) and str(row["ai_cost_p50"]) not in {str(c) for c in HISTORY_COSTS}


def test_the_gate_fired_so_there_was_something_to_catch(world):
    for report in world.reports.values():
        assert report["executive_summary"]["status"] == "withheld"
        assert any(l["code"] == "summaries_withheld" for l in report["limitations"])


# --- the leak test itself --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["xray", "full"])
def test_report_json_leaks_nothing(world, tier):
    assert leaks_in(json.dumps(world.reports[tier], ensure_ascii=False), world) == []


@pytest.mark.parametrize("tier", ["xray", "full"])
def test_report_html_leaks_nothing(world, tier):
    assert leaks_in(render_report_html(world.reports[tier]), world) == []


def test_report_events_leak_nothing(world):
    assert leaks_in(world.haystacks()["report events"], world) == []


def test_customer_report_has_no_cost_bearing_keys_anywhere(world):
    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for value in node:
                yield from keys(value)

    for report in world.reports.values():
        assert not [k for k in keys(report) if any(bad in k.lower() for bad in ("cost", "token", "price", "internal"))]


def test_legitimate_content_survives_the_gate(world):
    """The gate must not be the reason nothing leaks: names, paths, counts and clean summaries stay."""
    xray = world.reports["xray"]
    text = json.dumps(xray)
    assert "app/Ledger.php" in text and "connect_gateway" in text
    assert xray["glance"]["entities_by_type"].get("function", 0) >= 4
    explanations = [e["explanation"] for h in xray["risk"]["hotspots"] for e in h["entities"]]
    assert "Formats an invoice line for display." in explanations or "Groups the ledger reconciliation and archiving operations." in explanations
    assert any(e == leak_gate.WITHHELD_SUMMARY for e in explanations)  # and the leaky ones were replaced, not dropped


# --- the files actually stored in the bucket ------------------------------------------------------------------------------


def test_the_worker_stored_one_html_and_one_pdf_per_report(world):
    rows = world.client.table("assessment_reports").rows
    assert len(rows) == 2 and all(r["pdf_status"] == "done" for r in rows)
    expected = {p for r in rows for p in (r["html_path"], r["pdf_path"])}
    assert set(world.stored) == expected
    assert all(path.startswith(f"{ORG}/{r['id']}/") for r in rows for path in (r["html_path"], r["pdf_path"]))


@pytest.mark.parametrize("tier", ["xray", "full"])
def test_stored_html_artifact_leaks_nothing_and_is_what_the_json_renders_to(world, tier):
    row = next(r for r in world.client.table("assessment_reports").rows if r["tier"] == tier)
    html = world.stored[row["html_path"]].decode("utf-8")
    assert html == render_report_html(row["report_json"])
    assert leaks_in(html, world) == []


@needs_renderer
@pytest.mark.parametrize("tier", ["xray", "full"])
def test_stored_pdf_artifact_leaks_nothing(world, tier):
    from pypdf import PdfReader

    row = next(r for r in world.client.table("assessment_reports").rows if r["tier"] == tier)
    pdf = world.stored[row["pdf_path"]]
    text = " ".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(pdf)).pages)
    assert "Executive summary" in text and leaks_in(text, world) == []


# --- known limit ----------------------------------------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="Known gap: a bare secret value in prose matches no scanner pattern, and the stored source is "
                   "already redacted so it cannot overlap either. The defence is that the model only ever sees the redacted excerpt "
                   "(worker/evidence.py). Flips to XPASS, failing, if the gate ever learns to catch it: then delete this marker.")
def test_a_bare_secret_value_in_prose_is_caught():
    assert leak_gate.scan_text("Opens a connection using the password hunter22-LEAK-canary.", None)


# --- sensitivity: the test fails when the gate is bypassed ---------------------------------------------------------------


def test_the_detector_notices_planted_leaks(world):
    for planted in (SECRETS[0], SOURCE_TOKENS[0], "[REDACTED:jwt]", "ai_cost_p50", str(HISTORY_COSTS[3]), OTHER_ORG_CANARY):
        assert leaks_in(f"clean text {planted} clean text", world), planted
    assert leaks_in("A small shop with 3 subsystems and 12 findings.", world) == []


def test_bypassing_both_gates_makes_the_test_fail():
    ungated = build_world(bypass_gate=True, bypass_rendered=True)
    problems = {tier: leaks_in(json.dumps(report), ungated) for tier, report in ungated.reports.items()}
    assert all(problems.values()), "with the gates bypassed the leak test must find leaks"
    flat = " ".join(" ".join(p) for p in problems.values())
    assert "hunter22" in flat and "reconcile_quarterly_zebra_totals" in flat


def test_with_only_the_content_gate_bypassed_the_rendered_gate_fails_closed():
    """Defence in depth (design_report.md section 8.1, gate 3): leaky free text that reaches the template is caught
    in the rendered text. The job fails, no report row exists and nothing is uploaded."""
    world = build_world(bypass_gate=True, expect_done=False)
    jobs = world.client.table("report_generation_jobs").rows
    assert [j["status"] for j in jobs] == ["failed", "failed"]
    assert all("rendered report failed the content gate" in j["error"] for j in jobs)
    assert not any(secret in " ".join(j["error"] for j in jobs) for secret in SECRETS)
    assert world.client.table("assessment_reports").rows == [] and world.stored == {}


# --- the PDF ---------------------------------------------------------------------------------------------------------------


@needs_renderer
@pytest.mark.parametrize("tier", ["xray", "full"])
def test_pdf_text_leaks_nothing(world, tier):
    from pypdf import PdfReader

    pdf = render_pdf(render_report_html(world.reports[tier]), f"rj-{tier}", "2026-10-01", PdfParams(timeout_seconds=120))
    text = " ".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(pdf)).pages)
    assert "Executive summary" in text  # the text really was extracted
    assert leaks_in(text, world) == []
    # The PDF stream itself is compressed, but the metadata and any uncompressed strings are checked too.
    assert not any(s.encode() in pdf for s in SECRETS)
