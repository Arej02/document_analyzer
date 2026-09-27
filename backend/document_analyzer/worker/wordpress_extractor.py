"""Structural extraction: WordPress hooks/CPTs/meta — tasks.md Phase 2
"WordPressExtractor: hooks, CPTs, meta".

Implements the `Extractor` protocol (extract.py, design.md §5) for
WordPress-specific structure that plain AST parsing alone doesn't surface:
hook registrations/invocations, custom post types, and meta registrations.
Deliberately separate from `PhpExtractor` (design.md §5: "New language
support = new class implementing this protocol, nothing else in the pipeline
changes") — its own subprocess script (`backend/php/wp_extract.php`), its own
`detect()`, its own `ExtractionResult`. Pure parsing, no LLM calls, no
Supabase writes — persistence is a separate later task.

Entity/edge mapping (the schema has no dedicated "cpt"/"meta" entity_type —
design.md §2's `extracted_entities.entity_type` enum is
function/class/route/schema/hook/dependency):
- A hook registration or invocation (add_action/add_filter/do_action/
  apply_filters, and the `_ref_array` variants) becomes one `hook` entity per
  distinct hook *name*, aggregating every occurrence found (registration or
  invocation, action or filter) into `raw_facts["occurrences"]`.
- `register_post_type` and `register_post_meta`/`register_meta` calls each
  become a `schema` entity (`raw_facts["kind"]` = `post_type` or `meta`) —
  CPTs and meta are structural/content-schema definitions, not functions.
- A named or `Class::method`/`self::method` callback passed to add_action/
  add_filter becomes its own lightweight `function` entity scoped to this
  extractor's own `ExtractionResult` (`raw_facts["role"] = "hook_callback"`),
  distinct from any fuller entity `PhpExtractor` may produce for the same
  underlying function — the two extractors' `ExtractionResult`s are
  independently local_id-scoped (extract.py), so there is no way for this
  extractor to reference an entity it didn't itself create; reconciling
  duplicates across extractors is future work for the not-yet-built
  persistence step, not solved here.
- A `hook_binding` edge connects that callback entity to the hook entity —
  only for *registrations* (add_action/add_filter) with a resolvable named
  callback; a closure, an unresolvable callback shape, or a plain invocation
  (do_action/apply_filters — firing a hook, not binding to one) produces no
  edge, matching `entity_edges`'s literal meaning of "binding".
- CPT/meta entities get no edges: none of `entity_edges`'s edge_type values
  (calls/hook_binding/global/include) describes "defines a schema", so
  fabricating one would misrepresent the relationship.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from document_analyzer.worker.extract import ExtractedEdge, ExtractedEntity, ExtractionResult

_WP_SCRIPT = Path(__file__).resolve().parents[2] / "php" / "wp_extract.php"

_SIGNAL_RE = re.compile(
    rb"\b(add_action|add_filter|do_action|apply_filters|register_post_type|register_post_meta|register_meta)\s*\("
)


class WordPressExtractionError(Exception):
    """Raised when the WordPress-extraction subprocess fails or returns unparseable output."""


class WordPressExtractor:
    """Extractor protocol implementation for WordPress hooks/CPTs/meta (design.md §5)."""

    def __init__(self, php_binary: str = "php"):
        self._php_binary = php_binary

    def detect(self, repo_path: Path) -> bool:
        for php_file in repo_path.rglob("*.php"):
            try:
                content = php_file.read_bytes()
            except OSError:
                continue
            if _SIGNAL_RE.search(content):
                return True
        return False

    def extract(self, repo_path: Path) -> ExtractionResult:
        files = self._run_php_script(repo_path)

        hooks: dict[str, dict[str, Any]] = {}
        callbacks: dict[tuple[str, str], str] = {}
        entities: list[ExtractedEntity] = []
        edges: list[ExtractedEdge] = []

        def hook_entity(hook_name: str) -> dict[str, Any]:
            # No "file_path" key here on purpose: it's set by the first occurrence's own
            # hook.setdefault("file_path", file_path) call below. Pre-seeding it to None
            # would make that setdefault a no-op (the key would already be "present",
            # just with a None value), leaving every hook entity's file_path permanently
            # null — which extracted_entities.file_path's NOT NULL constraint rejects.
            return hooks.setdefault(
                hook_name,
                {"local_id": f"hook:{hook_name}", "kinds": set(), "occurrences": []},
            )

        def callback_entity(file_path: str, callback_name: str) -> str:
            key = (file_path, callback_name)
            if key not in callbacks:
                local_id = f"hook_callback:{file_path}:{callback_name}"
                callbacks[key] = local_id
                entities.append(
                    ExtractedEntity(
                        local_id=local_id,
                        entity_type="function",
                        name=callback_name,
                        file_path=file_path,
                        raw_facts={"role": "hook_callback"},
                    )
                )
            return callbacks[key]

        for file_result in files:
            if file_result.get("error"):
                continue
            file_path = file_result["file"]

            for reg in file_result.get("hook_registrations", []):
                hook_name = reg["hook_name"]
                if hook_name is None:
                    continue
                hook = hook_entity(hook_name)
                hook["kinds"].add(reg["hook_type"])
                hook.setdefault("file_path", file_path)
                hook["occurrences"].append(
                    {
                        "role": "register",
                        "kind": reg["hook_type"],
                        "file": file_path,
                        "line": reg["line"],
                        "end_line": reg["end_line"],
                        "callback": reg["callback_name"],
                        "priority": reg["priority"],
                        "accepted_args": reg["accepted_args"],
                    }
                )

                if reg["callback_name"] is not None:
                    callback_local_id = callback_entity(file_path, reg["callback_name"])
                    edges.append(
                        ExtractedEdge(
                            from_local_id=callback_local_id,
                            edge_type="hook_binding",
                            to_local_id=hook["local_id"],
                        )
                    )

            for inv in file_result.get("hook_invocations", []):
                hook_name = inv["hook_name"]
                if hook_name is None:
                    continue
                hook = hook_entity(hook_name)
                hook["kinds"].add(inv["hook_type"])
                hook.setdefault("file_path", file_path)
                hook["occurrences"].append(
                    {
                        "role": "invoke",
                        "kind": inv["hook_type"],
                        "file": file_path,
                        "line": inv["line"],
                        "end_line": inv["end_line"],
                    }
                )

            for cpt in file_result.get("cpts", []):
                slug = cpt["slug"]
                local_id = f"schema:{file_path}:cpt:{slug or 'dynamic'}:{cpt['line']}"
                entities.append(
                    ExtractedEntity(
                        local_id=local_id,
                        entity_type="schema",
                        name=slug or f"(dynamic post type at {file_path}:{cpt['line']})",
                        file_path=file_path,
                        raw_facts={
                            "kind": "post_type",
                            "args": cpt["args"],
                            "line": cpt["line"],
                            "end_line": cpt["end_line"],
                        },
                    )
                )

            for meta in file_result.get("metas", []):
                meta_key = meta["meta_key"]
                local_id = f"schema:{file_path}:meta:{meta_key or 'dynamic'}:{meta['line']}"
                entities.append(
                    ExtractedEntity(
                        local_id=local_id,
                        entity_type="schema",
                        name=meta_key or f"(dynamic meta at {file_path}:{meta['line']})",
                        file_path=file_path,
                        raw_facts={
                            "kind": "meta",
                            "object_type": meta["object_type"],
                            "args": meta["args"],
                            "line": meta["line"],
                            "end_line": meta["end_line"],
                        },
                    )
                )

        for hook_name, hook in hooks.items():
            entities.append(
                ExtractedEntity(
                    local_id=hook["local_id"],
                    entity_type="hook",
                    name=hook_name,
                    file_path=hook["file_path"],
                    raw_facts={"kinds": sorted(hook["kinds"]), "occurrences": hook["occurrences"]},
                )
            )

        return ExtractionResult(entities=entities, edges=edges)

    def _run_php_script(self, repo_path: Path) -> list[dict[str, Any]]:
        try:
            result = subprocess.run(
                [self._php_binary, str(_WP_SCRIPT), str(repo_path)],
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise WordPressExtractionError(f"could not run wordpress extractor: {exc}") from exc

        if result.returncode != 0:
            raise WordPressExtractionError(f"wordpress extractor failed: {result.stderr.strip()}")

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise WordPressExtractionError(f"wordpress extractor produced invalid JSON: {exc}") from exc
