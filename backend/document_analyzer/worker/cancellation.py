"""Cooperative job cancellation — design.md §4 `POST /api/agent1/jobs/{job_id}/cancel`.

The API flips `ingestion_jobs.status` to `cancelled`; the worker notices at its next check
and stops starting new work. `raise_if_cancelled` is called at every stage boundary and at
the start of each entity summary / fold, so work already in flight finishes but nothing new
begins. The database read is throttled per job (entity summaries run in a thread pool and
would otherwise make one query per entity), and once a cancel is seen it is remembered, so
every other thread raises without another query.

A failed status read is logged and treated as "not cancelled": a database blip must never
kill a healthy job.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 1.0


class JobCancelled(Exception):
    """The job's status is `cancelled`; the pipeline should stop."""


_lock = threading.Lock()
_last_checked: dict[str, float] = {}
_cancelled: set[str] = set()


def raise_if_cancelled(
    client, job_id: str, org_id: str, *, force: bool = False, now: Callable[[], float] = time.monotonic
) -> None:
    """Raises JobCancelled if the job has been cancelled. `force` skips the throttle (used right
    around the final done transition, where a stale answer would report a cancelled job as done)."""
    with _lock:
        if job_id in _cancelled:
            raise JobCancelled(job_id)
        current = now()
        if not force and current - _last_checked.get(job_id, float("-inf")) < CHECK_INTERVAL_SECONDS:
            return
        _last_checked[job_id] = current

    try:
        rows = (
            client.table("ingestion_jobs")
            .select("status")
            .eq("id", job_id)
            .eq("org_id", org_id)
            .limit(1)
            .execute()
            .data
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not read status of job %s to check for cancellation", job_id, exc_info=True)
        return

    if rows and rows[0].get("status") == "cancelled":
        with _lock:
            _cancelled.add(job_id)
        raise JobCancelled(job_id)


def forget_job(job_id: str) -> None:
    """Drops the per-job state once the job is finished with."""
    with _lock:
        _last_checked.pop(job_id, None)
        _cancelled.discard(job_id)
