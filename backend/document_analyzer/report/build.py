"""Assessment Report builder (design_report.md §5–6): assembles `report_json`.

Runs the collectors and puts their output, plus the pieces other stages produce
(estimate, executive summary, next steps), into the versioned schema. Everything
here is deterministic: collectors sort explicitly, limitations are de-duplicated
and sorted, and `canonical_json` fixes key order, so equal `inputs` give
byte-equal deterministic sections. `generated_at` is passed in and is not part
of that contract.

X-ray leaves `plan` and `estimate` out (the keys are absent, not null). A Full
Assessment needs a plan pinned to the report's own job and in an eligible status
(`ReportInputError` otherwise); a plan that isn't approved sets
`cover.watermark = "draft_plan"`.
"""

import json

from supabase import Client

from document_analyzer.report import collect
from document_analyzer.report.collect import ReportInputError
from document_analyzer.report.limitations import build_limitations

SCHEMA_VERSION = 1
TIERS = ("xray", "full")

NO_EXEC_SUMMARY = {"text": "", "status": "withheld", "note": "The executive summary was not generated."}


def plan_units(plan: dict) -> list[dict]:
    """The estimator's unit list (`{size, risk_level}`), in plan order, from `collect_plan` output."""
    return [{"size": u["size"], "risk_level": u["risk_level"]} for phase in plan["phases"] for u in phase["units"]]


def _merge_limitations(*groups: list[dict]) -> list[dict]:
    unique = {(l["code"], l["text"]) for group in groups for l in group}
    return [{"code": code, "text": text} for code, text in sorted(unique)]


def build_report_json(
    client: Client,
    org_id: str,
    *,
    job_id: str,
    tier: str,
    project_name: str,
    snapshot_date: str,
    generated_at: str,
    template_version: str,
    estimator_version: str,
    estimator_config_hash: str,
    next_steps: dict,
    plan_id: str | None = None,
    estimate: dict | None = None,
    pricing_version: str | None = None,
    calibration_snapshot_id: str | None = None,
    executive_summary: dict | None = None,
    extra_limitations: list[dict] | None = None,
    config: dict | None = None,
    calibration_snapshot: dict | None = None,
) -> dict:
    """`estimate` is the estimator's `customer` part only (never the internal part). `config` is
    the parsed estimator config (`quality`, `calibration` thresholds); defaults apply without it."""
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    if tier == "xray" and (plan_id or estimate):
        raise ValueError("an X-ray has no plan or estimate")
    if tier == "full":
        if not plan_id:
            raise ReportInputError("a Full Assessment needs a plan")
        if estimate is None:
            raise ReportInputError("a Full Assessment needs an estimate")

    plan_row = collect.load_plan_row(client, org_id, plan_id, job_id) if tier == "full" else None

    glance, glance_limits = collect.collect_glance(client, org_id, job_id)
    architecture, arch_limits = collect.collect_architecture(client, org_id, job_id, plan=plan_row)
    risk, risk_limits = collect.collect_risk(client, org_id, job_id)
    security, security_limits = collect.collect_security(client, org_id, job_id)
    plan, plan_limits = collect.collect_plan(client, org_id, job_id, plan_id) if plan_row else (None, [])

    generated = build_limitations(
        collect.collect_quality(client, org_id, job_id),
        config or {},
        estimate=estimate,
        executive_summary=executive_summary,
        calibration_snapshot=calibration_snapshot,
    )

    report = {
        "schema_version": SCHEMA_VERSION,
        "tier": tier,
        "inputs": {
            "source_job_id": job_id,
            "plan_id": plan_id,
            "plan_version": plan_row["version"] if plan_row else None,
            "plan_status": plan_row["status"] if plan_row else None,
            "estimator_version": estimator_version,
            "estimator_config_hash": estimator_config_hash,
            "pricing_version": pricing_version,
            "template_version": template_version,
            "calibration_snapshot_id": calibration_snapshot_id,
        },
        "generated_at": generated_at,
        "cover": {
            "project_name": project_name,
            "snapshot_date": snapshot_date,
            "watermark": "draft_plan" if plan_row and plan_row["status"] != "approved" else None,
        },
        "executive_summary": executive_summary or dict(NO_EXEC_SUMMARY),
        "glance": glance,
        "architecture": architecture,
        "risk": risk,
        "security": security,
    }
    if plan is not None:
        report["plan"] = plan
        report["estimate"] = estimate
    report["limitations"] = _merge_limitations(
        glance_limits, arch_limits, risk_limits, security_limits, plan_limits, generated, extra_limitations or []
    )
    report["next_steps"] = next_steps
    return report


def canonical_json(report: dict) -> str:
    """Stable serialisation: sorted keys, no whitespace variation. Equal reports are byte-equal."""
    return json.dumps(report, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
