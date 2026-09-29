"""Stage `units` step 1: build one unit's LLM context — design_agent3.md §4 step 1,
specs_agent3.md FR3.

Pure over an already-loaded `ExecutionState` (run.py) — no DB calls, and the only
filesystem access is reading already-unpacked, already-validated source files back
out of `state.snapshot_dir` (`prepare.py` did the untrusted unpack, §3). Every
`file_path` read here is re-resolved strictly inside `state.snapshot_dir`, because
`raw_facts.file_path` ultimately comes from parsing the untrusted repo and this
module is a second, independent place that turns a path string into a filesystem
read (CLAUDE.md "untrusted input").

Deliberately excluded from this module: the §8 target-architecture rules text
(static, not per-unit data — `generate.py` assembles it into the final prompt) and
prompt formatting beyond a deterministic size-measurable payload. `generate.py`
turns `UnitContext` into the actual model call.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


class ContextError(Exception):
    """A unit-level failure while assembling context (unit_executions.status = 'error',
    never a job-level failure — CLAUDE.md 'grounding failures never block')."""


class ContextTooLargeError(ContextError):
    """The assembled payload exceeds the unit's share of `max_context_chars`."""


@dataclass
class UnitContext:
    cluster_id: str
    member_slices: list[dict] = field(default_factory=list)
    unverified_member_ids: list[str] = field(default_factory=list)
    report_text: str = ""
    test_contract: dict = field(default_factory=dict)
    dependencies: list[dict] = field(default_factory=list)
    # Fully-qualified "module.name" of every symbol any earlier-finished unit exports,
    # not only this unit's declared dependencies (design_agent3.md §13 sparse-graph
    # mitigation) — sorted, deduplicated.
    earlier_unit_symbols: list[str] = field(default_factory=list)
    # Deterministic JSON serialization of everything above, used both to measure size
    # and as the payload `generate.py` hands to the model.
    text: str = ""

    @property
    def total_chars(self) -> int:
        return len(self.text)


def _resolve_member_path(snapshot_dir: Path, file_path: str) -> Path:
    """Rejects a `raw_facts.file_path` that isn't strictly inside the snapshot: an
    absolute path, a `..` component, or (via a symlink) a resolved target outside it.
    Mirrors `prepare.py::unpack_snapshot`'s guard, at the point this data is turned
    into a filesystem read for the second time. `raw_facts.file_path` is always a
    POSIX-style relative path (from git / the extractors), so it's checked as one
    even here on Windows, where `PureWindowsPath("/etc/passwd").is_absolute()` is
    (surprisingly) False — it has no drive letter."""
    posix_rel = PurePosixPath(file_path)
    if posix_rel.is_absolute() or ".." in posix_rel.parts:
        raise ContextError(f"unsafe file_path in raw_facts: {file_path!r}")
    root = snapshot_dir.resolve()
    candidate = (snapshot_dir / Path(*posix_rel.parts)).resolve()
    if candidate != root and root not in candidate.parents:
        raise ContextError(f"file_path escapes the snapshot: {file_path!r}")
    return candidate


def _read_lines(path: Path, file_path: str) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ContextError(f"could not read {file_path!r} from the snapshot: {exc}") from exc


def _source_slice(snapshot_dir: Path, entities: dict[str, dict], entity: dict) -> str | None:
    """§3/§4 step 1: `file_path` lines `line..end_line`, with a method's class header
    line prepended. `None` (no slice) for a member with no `line` at all (a hook,
    route, schema entity, or an older extractor run). A `line` with no `end_line` is a
    data error, not a "no span" case."""
    raw_facts = entity.get("raw_facts") or {}
    line = raw_facts.get("line")
    if line is None:
        return None
    end_line = raw_facts.get("end_line")
    if end_line is None:
        raise ContextError(
            f"entity {entity['id']} ({entity.get('name')}) in {entity['file_path']!r} has a line "
            "but no end_line in raw_facts"
        )

    path = _resolve_member_path(snapshot_dir, entity["file_path"])
    lines = _read_lines(path, entity["file_path"])
    if not (0 < line <= end_line <= len(lines)):
        raise ContextError(
            f"entity {entity['id']} ({entity.get('name')}) has an out-of-range span "
            f"{line}..{end_line} in {entity['file_path']!r} ({len(lines)} lines)"
        )
    slice_lines = lines[line - 1 : end_line]

    class_name = raw_facts.get("class")
    if class_name:
        header = _class_header(snapshot_dir, entities, entity["file_path"], class_name)
        if header is not None:
            slice_lines = [header, *slice_lines]
    return "\n".join(slice_lines)


def _class_header(snapshot_dir: Path, entities: dict[str, dict], file_path: str, class_name: str) -> str | None:
    for candidate in entities.values():
        if (
            candidate["entity_type"] == "class"
            and candidate["name"] == class_name
            and candidate["file_path"] == file_path
        ):
            class_line = (candidate.get("raw_facts") or {}).get("line")
            if class_line is None:
                return None
            path = _resolve_member_path(snapshot_dir, file_path)
            lines = _read_lines(path, file_path)
            return lines[class_line - 1] if 0 < class_line <= len(lines) else None
    return None


def _report_text(state, item: dict) -> str:
    """§4 step 1: the unit's own report, or (for a merged item with no report of its
    own) its constituent units' reports joined, same stand-in rule as
    `agent2/router.py::_unit_sources` (design_agent2.md §7, §9.2)."""
    parts = []
    for source_id in item["report_source_cluster_ids"]:
        report = state.cluster_reports.get(source_id)
        if report:
            parts.append(f"[{source_id}] ({report.get('risk_level')}) {report.get('summary_text', '')}")
    return "\n".join(parts)


def _dependency_context(state, item: dict, items_by_cluster_id: dict[str, dict]) -> list[dict]:
    """§4 step 1: for each declared dependency, its symbol map joined with its own
    `test_contract.public_interface` — the contract already pins the parameter shape,
    so nothing here needs the dependency's actual generated file text (design_agent3.md
    §4, updated 2026-09-27)."""
    dependencies = []
    for dep in (item.get("test_contract") or {}).get("outgoing_dependencies") or []:
        to_unit_id = dep["to_unit_id"]
        symbol_map = state.symbol_maps.get(to_unit_id)
        signatures = []
        if symbol_map:
            to_item = items_by_cluster_id.get(to_unit_id)
            public_interface = (to_item.get("test_contract") or {}).get("public_interface") if to_item else []
            for entry in public_interface or []:
                symbol = symbol_map.get(entry["entity_id"])
                if symbol:
                    signatures.append({**entry, **symbol})
        dependencies.append(
            {
                "to_unit_id": to_unit_id,
                "call_count": dep.get("call_count"),
                "hook_binding_count": dep.get("hook_binding_count"),
                "symbol_map": symbol_map,
                "signatures": sorted(signatures, key=lambda s: s["entity_id"]),
            }
        )
    return sorted(dependencies, key=lambda d: d["to_unit_id"])


def _earlier_unit_symbols(state) -> list[str]:
    """§13 sparse-call-graph mitigation: every earlier-finished unit's exported symbol
    names, not only this unit's declared outgoing_dependencies."""
    names = set()
    for symbol_map in state.symbol_maps.values():
        for symbol in symbol_map.values():
            module = symbol.get("python_module")
            name = symbol.get("python_name")
            if module and name:
                names.add(f"{module}.{name}")
    return sorted(names)


def build_unit_context(item: dict, state, max_context_chars: int, rules_reserved_chars: int = 0) -> UnitContext:
    """design_agent3.md §4 step 1. `rules_reserved_chars` is the length of the fixed §8
    rules block `generate.py` will add on top of this payload, so the two together can
    never exceed the job's frozen `params.max_context_chars`."""
    entities = state.entities
    unverified_member_ids = list((item.get("test_contract") or {}).get("unverified_member_ids") or [])
    unverified = set(unverified_member_ids)

    member_slices = []
    for entity_id in item["member_entity_ids"]:
        entity = entities.get(entity_id)
        if entity is None:
            continue
        summary = state.summaries.get(entity_id)
        member_slices.append(
            {
                "entity_id": entity_id,
                "entity_type": entity["entity_type"],
                "name": entity["name"],
                "file_path": entity["file_path"],
                "raw_facts": entity.get("raw_facts") or {},
                "summary_text": summary.get("summary_text") if summary and entity_id not in unverified else None,
                "source": _source_slice(state.snapshot_dir, entities, entity),
            }
        )
    member_slices.sort(key=lambda m: m["entity_id"])

    items_by_cluster_id = {i["cluster_id"]: i for i in state.items}
    context = UnitContext(
        cluster_id=item["cluster_id"],
        member_slices=member_slices,
        unverified_member_ids=sorted(unverified_member_ids),
        report_text=_report_text(state, item),
        test_contract=item.get("test_contract") or {},
        dependencies=_dependency_context(state, item, items_by_cluster_id),
        earlier_unit_symbols=_earlier_unit_symbols(state),
    )
    context.text = json.dumps(
        {
            "member_slices": context.member_slices,
            "report_text": context.report_text,
            "test_contract": context.test_contract,
            "dependencies": context.dependencies,
            "earlier_unit_symbols": context.earlier_unit_symbols,
        },
        sort_keys=True,
        default=str,
    )

    budget = max_context_chars - rules_reserved_chars
    if context.total_chars > budget:
        raise ContextTooLargeError(
            f"unit {item['cluster_id']} context is {context.total_chars} chars, exceeding its budget of "
            f"{budget} (max_context_chars={max_context_chars}, rules_reserved_chars={rules_reserved_chars})"
        )
    return context
