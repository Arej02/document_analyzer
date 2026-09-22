"""Worker step: boilerplate filter — tasks.md Phase 2
"Boilerplate filter (heuristics for wp-includes/, vendor/, etc.); log filtered
files somewhere queryable (even a simple table or job_id)".

Cheap directory-name heuristics (specs.md FR3: "before anything reaches the
LLM") — no parsing. Excludes vendored/third-party/core code that isn't the
repo owner's own, so extraction doesn't waste time on it and the knowledge
base doesn't fill up with WordPress-core or Composer/npm-dependency noise.

Filtered files are logged to the `filtered_files` table (design.md §2), via
the service-role client, rather than silently dropped — specs.md FR3 is
explicit that a heuristic can false-positive on a customized vendor fork, so
what got excluded and why must be auditable per job, not silent.
"""

from dataclasses import dataclass
from pathlib import Path

from supabase import Client

from document_analyzer.supabase_client import insert_many_scoped

_BOILERPLATE_DIR_REASONS = {
    "vendor": "composer_vendor_directory",
    "node_modules": "node_modules_directory",
    "wp-includes": "wordpress_core",
    "wp-admin": "wordpress_core",
    ".git": "vcs_directory",
}


@dataclass(frozen=True)
class FilteredFile:
    file_path: str  # relative to repo root, forward-slash separated
    reason: str


def boilerplate_reason(relative_path: Path) -> str | None:
    """Returns a reason string if a directory component of `relative_path` is known boilerplate, else None."""
    for part in relative_path.parts[:-1]:  # exclude the filename itself
        reason = _BOILERPLATE_DIR_REASONS.get(part)
        if reason is not None:
            return reason
    return None


def filter_boilerplate(repo_path: Path, files: list[Path]) -> tuple[list[Path], list[FilteredFile]]:
    """Splits `files` (absolute paths under repo_path) into (kept, filtered)."""
    kept: list[Path] = []
    filtered: list[FilteredFile] = []
    for file_path in files:
        relative = file_path.relative_to(repo_path)
        reason = boilerplate_reason(relative)
        if reason is None:
            kept.append(file_path)
        else:
            filtered.append(FilteredFile(file_path=relative.as_posix(), reason=reason))
    return kept, filtered


def log_filtered_files(client: Client, job_id: str, org_id: str, filtered: list[FilteredFile]) -> None:
    if not filtered:
        return
    rows = [{"job_id": job_id, "file_path": f.file_path, "reason": f.reason} for f in filtered]
    insert_many_scoped(client, "filtered_files", org_id, rows)
