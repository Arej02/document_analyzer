"""Worker step: diff a job's freshly-persisted entities against the project's
previous job, by content_hash — design.md §2/§3, specs.md FR6, tasks.md Phase 5
"Diff against the project's previous job by content_hash; only re-extract/
re-summarize changed files".

Runs on the async worker only, via the service-role client (bypasses RLS) —
same as every other worker/*.py step. Called after persist.py has written the
current job's `extracted_entities` rows (content_hash is computed there, at
insert time), and before entity-level summarization starts.

Matches entities across jobs by (file_path, name, entity_type) — the same
identity persist.py's content_hash is computed over — against the most recent
*other*, already-`done` job for the same project_id. An entity whose
content_hash is unchanged from that match is `unchanged`; anything else (the
content differs, or the entity/file is new since then) is `changed`. A file
that had entities in the previous job but has none in the current one is
`removed`. A project with no prior `done` job (first-time ingestion) has
nothing to diff against, so everything in the current job counts as changed.

**Scope note, deliberately not done here:** re-summarization is *not*
re-implemented in this module — it's already handled, for any entity whose
content_hash is unchanged (regardless of which job it last appeared in), by
the pre-existing `summary_cache` lookup `worker/run.py::_process_one_entity`
already does before ever calling the LLM (Phase 3). This module's job is to
compute and persist the changed/unchanged/removed breakdown as an auditable,
queryable count — not to duplicate that reuse path. Re-*extraction* similarly
still runs unconditionally against the whole current source tree: content_hash
can only be computed after parsing, and no raw per-file hash is persisted
anywhere a future job could diff against before parsing (design.md §2 already
notes entity-level content_hash is "the only content_hash concept the data
model defines") — flagged as a known limitation rather than silently
worked around; the parse itself is cheap and has no LLM cost, so it isn't the
resource this task is protecting.
"""

from dataclasses import dataclass, field

from supabase import Client

_ENTITY_COLUMNS = "file_path, name, entity_type, content_hash"


@dataclass(frozen=True)
class JobDiff:
    previous_job_id: str | None
    changed_file_paths: set[str] = field(default_factory=set)
    unchanged_file_paths: set[str] = field(default_factory=set)
    removed_file_paths: set[str] = field(default_factory=set)

    @property
    def changed_file_count(self) -> int:
        return len(self.changed_file_paths)

    @property
    def unchanged_file_count(self) -> int:
        return len(self.unchanged_file_paths)

    @property
    def removed_file_count(self) -> int:
        return len(self.removed_file_paths)


def _find_previous_job_id(client: Client, org_id: str, project_id: str, current_job_id: str) -> str | None:
    result = (
        client.table("ingestion_jobs")
        .select("id, created_at")
        .eq("org_id", org_id)
        .eq("project_id", project_id)
        .eq("status", "done")
        .order("created_at", desc=True)
        .execute()
    )
    for row in result.data:
        if row["id"] != current_job_id:
            return row["id"]
    return None


def diff_against_previous_job(client: Client, org_id: str, project_id: str, current_job_id: str) -> JobDiff:
    """Reads the current job's already-persisted entities and the project's previous
    `done` job's entities directly from `extracted_entities`, and classifies every
    file that appears in either side. Safe to call even for a job with zero entities
    (an all-boilerplate repo) — every previously-seen file is then `removed`."""
    previous_job_id = _find_previous_job_id(client, org_id, project_id, current_job_id)

    current_rows = (
        client.table("extracted_entities")
        .select(_ENTITY_COLUMNS)
        .eq("org_id", org_id)
        .eq("job_id", current_job_id)
        .execute()
    ).data

    if previous_job_id is None:
        changed_files = {row["file_path"] for row in current_rows}
        return JobDiff(previous_job_id=None, changed_file_paths=changed_files)

    previous_rows = (
        client.table("extracted_entities")
        .select(_ENTITY_COLUMNS)
        .eq("org_id", org_id)
        .eq("job_id", previous_job_id)
        .execute()
    ).data
    previous_by_key = {(row["file_path"], row["name"], row["entity_type"]): row["content_hash"] for row in previous_rows}
    previous_file_paths = {row["file_path"] for row in previous_rows}

    changed_files: set[str] = set()
    unchanged_files: set[str] = set()
    seen_current_files: set[str] = set()

    for row in current_rows:
        file_path = row["file_path"]
        seen_current_files.add(file_path)
        key = (file_path, row["name"], row["entity_type"])
        if previous_by_key.get(key) == row["content_hash"]:
            unchanged_files.add(file_path)
        else:
            changed_files.add(file_path)

    # A file can contain both an unchanged entity and a changed/new one — treat the
    # whole file as changed in that case, so "unchanged" only means nothing in it moved.
    unchanged_files -= changed_files

    removed_files = previous_file_paths - seen_current_files

    return JobDiff(
        previous_job_id=previous_job_id,
        changed_file_paths=changed_files,
        unchanged_file_paths=unchanged_files,
        removed_file_paths=removed_files,
    )


def persist_diff_counts(client: Client, job_id: str, org_id: str, diff: JobDiff) -> None:
    """Writes the diff's file counts onto the job's own `ingestion_jobs` row. Filtered
    by both `id` and `org_id`, same defense-in-depth as status.py's other worker writes."""
    (
        client.table("ingestion_jobs")
        .update(
            {
                "changed_file_count": diff.changed_file_count,
                "unchanged_file_count": diff.unchanged_file_count,
                "removed_file_count": diff.removed_file_count,
            }
        )
        .eq("id", job_id)
        .eq("org_id", org_id)
        .execute()
    )
