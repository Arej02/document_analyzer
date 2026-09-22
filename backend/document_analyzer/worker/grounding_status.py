"""Worker step: persist the grounding verdict — tasks.md Phase 4
"On exhausted retries: set grounding_status = unverified, continue rather than
fail the job".

Runs on the async worker only, via the service-role client (bypasses RLS) —
same write pattern as status.py's mark_job_running. Takes the outcome of
ground_retry.py's `check_grounding_with_retry` for one already-persisted
`knowledge_summaries` row (persist_summary.py) and writes its final
`grounding_status`: `verified` when the retry loop ended grounded, or
`unverified` when the retry cap was exhausted without a grounded result —
flipping the row from persist_summary.py's default-`unverified` draft state
(design.md §1's "knowledge_summaries (draft)") to its resolved state.

"continue rather than fail the job" (specs.md FR5, CLAUDE.md "Grounding
checker failures never block ingestion"): this function has no failure path
tied to the grounding *outcome* at all — an ungrounded result after retries is
written as `unverified` exactly the same way a grounded one is written as
`verified`, never raised as an error. Only a genuine Supabase write failure
would raise, same as any other worker write.

Filtered by both `id` and `org_id`, not just `id`, as defense-in-depth (same
as status.py's mark_job_running) — CLAUDE.md's cross-tenant-leak warning for
worker writes.

Not built here — separate, not-yet-built Phase 4 tasks: the job-only-
reaches-`done`-once-every-summary-is-verified/unverified gate, and cascading
re-summarization of ancestor summaries. This module only writes one summary
row's grounding_status; it isn't yet wired into any call site (same standing
caveat as the rest of the worker pipeline).
"""

from typing import Literal

from supabase import Client

from document_analyzer.worker.ground_retry import GroundingRetryResult

GroundingStatus = Literal["verified", "unverified"]


def grounding_status_for(retry_result: GroundingRetryResult) -> GroundingStatus:
    """Maps a retry loop's final verdict to the persisted grounding_status: "verified"
    when it ended grounded, "unverified" when the retry cap was exhausted without one."""
    return "verified" if retry_result.grounding.verdict == "grounded" else "unverified"


def persist_grounding_status(
    client: Client, summary_id: str, org_id: str, retry_result: GroundingRetryResult
) -> None:
    """Writes the final grounding_status for one already-persisted knowledge_summaries
    row, derived from `retry_result` via grounding_status_for. Never raises based on the
    grounding outcome itself — see module docstring."""
    status = grounding_status_for(retry_result)
    (
        client.table("knowledge_summaries")
        .update({"grounding_status": status})
        .eq("id", summary_id)
        .eq("org_id", org_id)
        .execute()
    )
