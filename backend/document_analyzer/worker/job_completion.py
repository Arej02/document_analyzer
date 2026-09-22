"""Worker step: job completion gate — tasks.md Phase 4
"Job only reaches `status = done` once all summaries are either `verified` or
`unverified` (never blocks on a single bad one)".

Runs on the async worker only, via the service-role client (bypasses RLS) —
same write pattern as status.py's mark_job_running (the mirror-image
transition at the other end of the pipeline: queued -> running there,
running -> done here).

`knowledge_summaries.grounding_status` is a NOT NULL enum of only `verified`/
`unverified` (design.md §2) — grounding_status.py's persist_grounding_status
always writes one of those two, never a third "still pending" value. So once
an entity has a persisted entity-level `knowledge_summaries` row at all, its
grounding has already necessarily been resolved one way or the other.
"Ready for completion" therefore reduces to: does every one of this job's
`extracted_entities` rows have a matching entity-level `knowledge_summaries`
row yet? (A job with zero entities — e.g. an empty/all-boilerplate repo — is
trivially ready.)

`mark_job_done_if_ready` deliberately never inspects *which* grounding_status
value any row carries, and never sets `status = failed` on account of one —
a job where every summary ended up `unverified` is exactly as eligible to
finish as one where every summary `verified`. That's what "never blocks on a
single bad one" means here (specs.md FR5, CLAUDE.md: "Grounding checker
failures never block ingestion").

Module/subsystem/repo-level summaries are not part of this gate: entity-level
summarization is the fan-out (summarize_fanout.py) that can only be as
complete as its slowest/worst entity, and specs.md FR4's "one level up at a
time" means module/subsystem/repo summaries are each derived only after their
own children are already done — so entity-level completeness is the actual
bottleneck condition worth gating on.

Not built here — the not-yet-built worker main loop is what would actually
call this after fanning grounding out across a job's entities; this module
only checks/writes job completion, it never runs summarization or grounding
itself.

`mark_job_done_if_ready` also updates `projects.latest_job_id` to point at
this job once (and only once) the `done` transition actually happens —
tasks.md Phase 5 "Update projects.latest_job_id once the new job reaches
done", design.md §4's `POST /projects/{project_id}/reingest` note. Scoped by
both `id` and `org_id` on the `projects` update, same defense-in-depth as
every other worker write here. A job whose `ingestion_jobs` row has no
`project_id` (shouldn't happen in practice — every job is created against a
resolved-or-created project, jobs.py) skips this update rather than raising,
same as any other worker write hitting a row that doesn't exist.
"""

from supabase import Client


def job_ready_for_completion(client: Client, job_id: str, org_id: str) -> bool:
    """True once every extracted_entities row for this job has a matching entity-level
    knowledge_summaries row — see module docstring for why that's equivalent to "every
    summary has been through grounding + retry and resolved to verified/unverified"."""
    entities = (
        client.table("extracted_entities").select("id").eq("job_id", job_id).eq("org_id", org_id).execute().data
    )
    entity_summaries = (
        client.table("knowledge_summaries")
        .select("entity_id")
        .eq("job_id", job_id)
        .eq("org_id", org_id)
        .eq("level", "entity")
        .execute()
        .data
    )
    summarized_entity_ids = {row["entity_id"] for row in entity_summaries}
    return all(entity["id"] in summarized_entity_ids for entity in entities)


def _update_project_latest_job_id(client: Client, project_id: str, org_id: str, job_id: str) -> None:
    (
        client.table("projects")
        .update({"latest_job_id": job_id})
        .eq("id", project_id)
        .eq("org_id", org_id)
        .execute()
    )


def mark_job_done_if_ready(client: Client, job_id: str, org_id: str) -> bool:
    """Transitions ingestion_jobs.status -> done, progress_stage -> None, but only if
    job_ready_for_completion is true; otherwise does nothing. Returns whether the
    transition actually happened — i.e. a matching `id` + `org_id` row was found and
    updated, the same `id`-and-`org_id` defense-in-depth filter as status.py's
    mark_job_running, so a job under another org is never touched even if the readiness
    check (scoped to the *caller's* org_id) happened to read as vacuously ready. Never
    influenced by which grounding_status any summary carries — see module docstring.

    On an actual transition, also points `projects.latest_job_id` at this job — see
    module docstring."""
    if not job_ready_for_completion(client, job_id, org_id):
        return False

    result = (
        client.table("ingestion_jobs")
        .update({"status": "done", "progress_stage": None})
        .eq("id", job_id)
        .eq("org_id", org_id)
        .execute()
    )
    if not result.data:
        return False

    project_id = result.data[0].get("project_id")
    if project_id:
        _update_project_latest_job_id(client, project_id, org_id, job_id)

    return True
