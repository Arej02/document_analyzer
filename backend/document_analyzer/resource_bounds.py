"""Ingestion resource bounds — tasks.md Phase 2
"Enforce ingestion resource bounds (max upload size / max file count)".

specs.md NFR2: "Ingestion input itself is bounded before extraction starts:
a max upload size and a max file/entity count are enforced, and an oversized
submission is rejected with a clear error rather than being partially
processed." Untrusted input (CLAUDE.md) — these exist to bound the blast
radius of a hostile or accidental zip bomb / oversized repo, not just UX.

Shared by both the request layer (`storage.py`, upload size — known at
request time) and the worker layer (`worker/fetch.py`, file count — only
knowable once an archive/repo is actually inspected/cloned). Env-configurable,
starting defaults per specs.md's own pattern for the concurrency cap and
retry cap ("starting default — revisit after Phase 6 load testing"); Phase 6's
5,000-10,000+ entity load test is a real repo with many more *files* than
entities (assets, vendored code before boilerplate filtering, etc.), so
MAX_FILE_COUNT is set well above that to avoid rejecting legitimate large
repos.
"""

import os

MAX_UPLOAD_SIZE_BYTES = int(os.environ.get("AGENT1_MAX_UPLOAD_SIZE_BYTES", 500 * 1024 * 1024))  # 500 MB
MAX_FILE_COUNT = int(os.environ.get("AGENT1_MAX_FILE_COUNT", 50_000))
