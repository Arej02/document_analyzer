"""The commit plan — design_agent3.md §15.2, §15.3, §15.14.

Turns the delivery manifest (`manifest.json`, written by stage `final_test`) plus the
assembled project's files into an ordered list of real git commits: a scaffold commit,
one commit per unit in plan order, and a final report commit. The same plan feeds the
zip's `.git` (`worker/git_history.py`) and the GitHub push (`delivery/`), so the two can
never describe different histories. Every commit and tree SHA comes from
`git_objects.py`.

Pure: no DB, network, git binary or filesystem. Its callers (worker and API) load the
manifest and files themselves.

Untrusted text: unit slugs, statuses, attempts and flags are validated against fixed
patterns/sets and are the ONLY manifest values that reach a commit message. Phase and
unit *labels* (LLM-derived from repository content) are carried through on `BranchGroup`
only for the PR renderer, which must neutralise them (§15.9); they never enter a commit.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .git_objects import FileRef, Identity, build_tree, commit_sha, file_refs, normalize_message
from .project_layout import (
    README_PATH,
    STUB_README,
    UNIT_SLUG_PATTERN,
    UNITS_INIT_PATH,
    render_units_init,
    scaffold_path_allowed,
    unit_path_allowed,
)

MANIFEST_VERSION = 1

# Fixed identity and baseline date (§15.2, §15.14). `.invalid` is a reserved TLD: this
# address can never receive mail or be claimed by a real GitHub account.
AUTHOR_NAME = "Galvanise Agent 3"
AUTHOR_EMAIL = "noreply@galvanise.invalid"
# 2000-01-01T00:00:00Z. Constant on purpose: the baseline commit must be byte-identical
# across executions so an empty target repo can be initialised with the very same commit.
BASELINE_TIMESTAMP = 946684800
BASELINE_MESSAGE = "Initial commit"

KNOWN_STATUSES = frozenset({"passed", "failed_verification", "error"})
KNOWN_FLAGS = frozenset({"unverified_inputs", "built_on_failed_dependency", "stubbed"})


class CommitPlanError(ValueError):
    """The manifest or file set is not something we are willing to commit."""


@dataclass(frozen=True)
class Base:
    """What the first commit sits on. Defaults (None) to the zip's baseline commit B0.
    For delivery, the repo's real head: its commit SHA and its full recursive tree."""

    commit_sha: str
    entries: Mapping[str, FileRef]


@dataclass(frozen=True)
class PlannedCommit:
    index: int
    kind: str  # "baseline" | "scaffold" | "unit" | "report"
    phase_order: int | None
    cluster_id: str | None
    message: str  # normalised, ends in exactly one newline
    # Files this commit adds or replaces, `{path: bytes}`. Never deletes anything.
    changes: Mapping[str, bytes]
    timestamp: int
    parent_sha: str | None
    tree_sha: str
    commit_sha: str


@dataclass(frozen=True)
class BranchGroup:
    """The commits that go on one branch / one PR (§15.4)."""

    key: str  # "scaffold" or "phase-{n}"
    kind: str  # "scaffold" | "phase"
    phase_order: int | None
    phase_cluster_id: str | None
    label: str | None  # UNTRUSTED — neutralise before rendering (§15.9)
    commits: tuple[PlannedCommit, ...]


@dataclass(frozen=True)
class CommitPlan:
    # The zip's root; None when a real `base` was supplied.
    baseline: PlannedCommit | None
    groups: tuple[BranchGroup, ...]
    root_sha: str  # the commit the scaffold commit's parent is (B0, or the supplied base)
    final_tree_sha: str

    @property
    def commits(self) -> list[PlannedCommit]:
        ordered = [self.baseline] if self.baseline else []
        for group in self.groups:
            ordered.extend(group.commits)
        return ordered


@dataclass
class _Unit:
    cluster_id: str
    unit_slug: str
    status: str
    attempts: int
    flags: list[str]
    files: list[str]
    label: str | None = None


@dataclass
class _Phase:
    phase_order: int
    phase_cluster_id: str | None
    label: str | None
    units: list[_Unit] = field(default_factory=list)


def _identity(timestamp: int) -> Identity:
    return Identity(AUTHOR_NAME, AUTHOR_EMAIL, timestamp)


def baseline_files() -> dict[str, bytes]:
    return {README_PATH: STUB_README.encode("utf-8")}


def baseline_commit() -> PlannedCommit:
    """B0: a root commit whose tree is the stub README alone (§15.14)."""
    changes = baseline_files()
    tree = build_tree(file_refs(changes))
    identity = _identity(BASELINE_TIMESTAMP)
    sha = commit_sha(tree.root_sha, [], identity, identity, BASELINE_MESSAGE)
    return PlannedCommit(
        index=0,
        kind="baseline",
        phase_order=None,
        cluster_id=None,
        message=normalize_message(BASELINE_MESSAGE),
        changes=changes,
        timestamp=BASELINE_TIMESTAMP,
        parent_sha=None,
        tree_sha=tree.root_sha,
        commit_sha=sha,
    )


def tree_sha_of_files(files: Mapping[str, bytes]) -> str:
    return build_tree(file_refs(files)).root_sha


def _epoch(created_at: Any) -> int:
    if not isinstance(created_at, str) or not created_at:
        raise CommitPlanError("manifest has no created_at (regenerate this execution)")
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise CommitPlanError(f"manifest created_at {created_at!r} is not an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CommitPlanError(message)


def _parse_phases(manifest: Mapping[str, Any]) -> list[_Phase]:
    raw_phases = manifest.get("phases")
    _require(isinstance(raw_phases, list), "manifest.phases must be a list")
    phases: list[_Phase] = []
    seen_orders: set[int] = set()
    for raw in raw_phases:
        _require(isinstance(raw, dict), "manifest phase must be an object")
        order = raw.get("phase_order")
        _require(isinstance(order, int) and not isinstance(order, bool) and order >= 0, "phase_order must be a non-negative int")
        _require(order not in seen_orders, f"duplicate phase_order {order}")
        _require(not phases or order > phases[-1].phase_order, "phases must be in increasing phase_order")
        seen_orders.add(order)
        phase = _Phase(order, raw.get("phase_cluster_id"), raw.get("label") if isinstance(raw.get("label"), str) else None)
        raw_units = raw.get("units")
        _require(isinstance(raw_units, list) and raw_units, f"phase {order} has no units")
        for raw_unit in raw_units:
            _require(isinstance(raw_unit, dict), "manifest unit must be an object")
            slug = raw_unit.get("unit_slug")
            _require(isinstance(slug, str) and UNIT_SLUG_PATTERN.match(slug) is not None, f"bad unit_slug {slug!r}")
            status = raw_unit.get("status")
            _require(status in KNOWN_STATUSES, f"unit {slug}: unknown status {status!r}")
            attempts = raw_unit.get("attempts")
            _require(isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0, f"unit {slug}: bad attempts")
            flags = raw_unit.get("flags") or []
            _require(isinstance(flags, list) and set(flags) <= KNOWN_FLAGS, f"unit {slug}: unknown flags {flags!r}")
            unit_files = raw_unit.get("files")
            _require(isinstance(unit_files, list) and unit_files, f"unit {slug} has no files")
            cluster_id = raw_unit.get("cluster_id")
            _require(isinstance(cluster_id, str) and cluster_id, f"unit {slug}: missing cluster_id")
            phase.units.append(
                _Unit(
                    cluster_id=cluster_id,
                    unit_slug=slug,
                    status=status,
                    attempts=attempts,
                    flags=sorted(flags),
                    files=list(unit_files),
                    label=raw_unit.get("label") if isinstance(raw_unit.get("label"), str) else None,
                )
            )
        phases.append(phase)
    return phases


def _unit_message(phase: _Phase, unit: _Unit) -> str:
    tags = []
    if unit.status != "passed":
        tags.append(unit.status)
    if "stubbed" in unit.flags:
        tags.append("stubbed")
    subject = f"Migrate unit {unit.unit_slug}" + "".join(f" [{tag}]" for tag in tags)
    body = [f"Phase {phase.phase_order}, status {unit.status}, attempts {unit.attempts}."]
    if unit.flags:
        body.append("Flags: " + ", ".join(unit.flags) + ".")
    return normalize_message(subject + "\n\n" + "\n".join(body))


def build_commit_plan(
    manifest: Mapping[str, Any], files: Mapping[str, bytes], *, base: Base | None = None
) -> CommitPlan:
    """`files` is the assembled project as `{path: bytes}`, WITHOUT any `.git/` entries.
    With `base=None` the plan is the zip's history, rooted on the baseline commit B0 and
    the last tree must equal `files` exactly (checked here). With a real `base` (delivery)
    the first commit's parent is `base.commit_sha` and every tree is `base.entries` with
    our files layered on top (ours win a path collision)."""
    _require(manifest.get("version") == MANIFEST_VERSION, f"unsupported manifest version {manifest.get('version')!r}")
    created = _epoch(manifest.get("created_at"))
    phases = _parse_phases(manifest)

    scaffold_paths = manifest.get("scaffold")
    _require(isinstance(scaffold_paths, list), "manifest.scaffold must be a list")
    for path in scaffold_paths:
        _require(isinstance(path, str) and scaffold_path_allowed(path), f"scaffold path not allowed: {path!r}")
    _require(README_PATH not in scaffold_paths and UNITS_INIT_PATH not in scaffold_paths, "scaffold must not list README.md or app/units/__init__.py")

    claimed: dict[str, str] = {}

    def claim(path: str, owner: str) -> None:
        _require(path in files, f"{owner}: {path!r} is not in the project files")
        _require(path not in claimed, f"{path!r} is claimed by both {claimed.get(path)} and {owner}")
        claimed[path] = owner

    for path in scaffold_paths:
        claim(path, "scaffold")
    for phase in phases:
        for unit in phase.units:
            for path in unit.files:
                _require(isinstance(path, str) and unit_path_allowed(path, unit.unit_slug), f"unit {unit.unit_slug}: path not allowed: {path!r}")
                claim(path, f"unit {unit.unit_slug}")
    extras = sorted(set(files) - set(claimed) - {README_PATH, UNITS_INIT_PATH})
    _require(not extras, f"project files not covered by the manifest: {extras[:5]}")
    _require(README_PATH in files and UNITS_INIT_PATH in files, "project files must include README.md and app/units/__init__.py")

    all_slugs = [unit.unit_slug for phase in phases for unit in phase.units]
    _require(len(all_slugs) == len(set(all_slugs)), "duplicate unit_slug in manifest")
    _require(
        files[UNITS_INIT_PATH] == render_units_init(all_slugs).encode("utf-8"),
        "app/units/__init__.py in the project does not import exactly the manifest's units in plan order",
    )

    baseline = None
    if base is None:
        baseline = baseline_commit()
        root_sha, entries = baseline.commit_sha, dict(file_refs(baseline.changes))
    else:
        root_sha, entries = base.commit_sha, dict(base.entries)

    parent_sha = root_sha
    index = 0

    def make(kind: str, phase_order: int | None, cluster_id: str | None, message: str, changes: dict[str, bytes]) -> PlannedCommit:
        nonlocal parent_sha, index
        index += 1
        entries.update(file_refs(changes))
        tree = build_tree(entries)
        timestamp = created + index
        identity = _identity(timestamp)
        sha = commit_sha(tree.root_sha, [parent_sha], identity, identity, message)
        commit = PlannedCommit(
            index=index,
            kind=kind,
            phase_order=phase_order,
            cluster_id=cluster_id,
            message=normalize_message(message),
            changes=changes,
            timestamp=timestamp,
            parent_sha=parent_sha,
            tree_sha=tree.root_sha,
            commit_sha=sha,
        )
        parent_sha = sha
        return commit

    scaffold_changes = {path: files[path] for path in scaffold_paths}
    scaffold_changes[README_PATH] = STUB_README.encode("utf-8")
    scaffold_changes[UNITS_INIT_PATH] = render_units_init([]).encode("utf-8")
    scaffold_commit = make(
        "scaffold",
        None,
        None,
        "Add project scaffold\n\nFixed template files, the CI workflow and pinned requirements. "
        "Generated units arrive in the pull requests stacked on this one.",
        scaffold_changes,
    )

    groups: list[BranchGroup] = []
    committed_slugs: list[str] = []
    phase_commits: list[list[PlannedCommit]] = []
    for phase in phases:
        commits: list[PlannedCommit] = []
        for unit in phase.units:
            committed_slugs.append(unit.unit_slug)
            changes = {path: files[path] for path in unit.files}
            changes[UNITS_INIT_PATH] = render_units_init(committed_slugs).encode("utf-8")
            commits.append(make("unit", phase.phase_order, unit.cluster_id, _unit_message(phase, unit), changes))
        phase_commits.append(commits)

    report_commit = make(
        "report",
        phases[-1].phase_order if phases else None,
        None,
        "Add migration report\n\nReplaces the stub README with the full per-unit verdict table.",
        {README_PATH: files[README_PATH]},
    )

    scaffold_group_commits = [scaffold_commit] + ([report_commit] if not phases else [])
    groups.append(BranchGroup("scaffold", "scaffold", None, None, None, tuple(scaffold_group_commits)))
    for position, phase in enumerate(phases):
        commits = phase_commits[position]
        if position == len(phases) - 1:
            commits = commits + [report_commit]
        groups.append(
            BranchGroup(f"phase-{phase.phase_order}", "phase", phase.phase_order, phase.phase_cluster_id, phase.label, tuple(commits))
        )

    final_tree_sha = report_commit.tree_sha
    if base is None:
        _require(
            final_tree_sha == tree_sha_of_files(files),
            "internal error: the last commit's tree does not equal the project files",
        )
    return CommitPlan(baseline=baseline, groups=tuple(groups), root_sha=root_sha, final_tree_sha=final_tree_sha)
