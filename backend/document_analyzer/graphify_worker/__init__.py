"""Stdlib-only Graphify extraction worker, ported from Galvanise (`galvanise.worker`).

Ships inside the `document-analyzer-graphify` image (backend/graphify_runner/) and runs
only there, as `python -m graphify_worker`; Graphify itself is never installed in the
API/worker environment. The host side (worker/graphify/) imports `protocol` from here
for the shared output contract, which is why this package must stay standard-library
only.
"""
