"""Plan staleness (design_agent2.md §9.1), shared by Agent 2's and Agent 3's APIs.

A plan is pinned to one Agent 1 job. Once the project's `latest_job_id` moves
past that job (a re-ingestion finished), the plan is stale. There's no
cross-agent trigger, so each API runs `mark_stale_plans` before reading or
acting on a plan. Moved here from agent2/router.py when Agent 3 needed it
(design_agent3.md §9), so neither agent imports the other's router.
"""

# Plan statuses the stale check may move to `stale` (everything but `rejected`).
STALEABLE_STATUSES = ("draft", "in_review", "approved")


def is_stale(source_job_id: str, latest_job_id: str | None) -> bool:
    return latest_job_id is not None and source_job_id != latest_job_id


def mark_stale_plans(service_client, org_id: str, project_id: str, latest_job_id: str | None) -> None:
    """Every plan of this project pinned to a source job other than `latest_job_id`
    moves to `stale`, from any state except `rejected`. Service-role because
    migration_plans has no authenticated update policy.
    """
    rows = (
        service_client.table("migration_plans")
        .select("id, status, source_job_id")
        .eq("org_id", org_id)
        .eq("project_id", project_id)
        .in_("status", STALEABLE_STATUSES)
        .execute()
    ).data
    stale_ids = [row["id"] for row in rows if is_stale(row["source_job_id"], latest_job_id)]
    if stale_ids:
        (
            service_client.table("migration_plans")
            .update({"status": "stale"})
            .eq("org_id", org_id)
            .in_("id", stale_ids)
            .execute()
        )
