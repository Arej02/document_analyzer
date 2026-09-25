r"""Structural extraction: Laravel route -> controller bindings.

Implements the `Extractor` protocol (extract.py, design.md §5) for the one
piece of Laravel's wiring that `PhpExtractor`'s plain call-graph parsing can
never see: a route doesn't *call* its controller method in source text, the
framework's router invokes it by convention at request time — the same shape
of problem `WordPressExtractor` solves for hooks, just via `routes/*.php`
instead of `add_action`/`add_filter`.

Added after a live planning run against the `hospital_test` Laravel repo came
back with an almost entirely disconnected dependency graph (`graph_stats`:
`unit_edges: 0`, `phase_edges: 0`) even though the repo has real structure —
`_resolve_edges` in `php_extractor.py` only resolves a call whose target is
*defined in this repo*, and a Laravel controller's own callers are Eloquent
statics/framework helpers (correctly unresolved) while its own *caller* (the
router) isn't expressed as a call at all.

Scope (deliberately conservative, regex/text-based rather than a PHP AST
subprocess — route registrations are declarative one-liners, not code that
needs real parsing):
- Reads `routes/web.php`/`routes/api.php` only (not `console.php`/`channels.php`
  — Artisan commands and broadcast channels aren't HTTP routes).
- Resolves `Route::{verb}('path', [Controller::class, 'method'])` (modern
  array syntax), `Route::{verb}('path', 'Controller@method')` (classic string
  syntax — a bare, unqualified `Controller` in this form is resolved against
  Laravel's default `App\Http\Controllers` namespace, matching
  `RouteServiceProvider`'s own convention even with no `use` import), a bare
  single-action `Route::{verb}('path', Controller::class)` (invokable
  controller, resolved to its `__invoke` method), and `Route::resource`/
  `Route::apiResource` (expanded into their standard RESTful actions).
- A controller short name is resolved to a fully-qualified class name via
  this file's own `use` imports (including `use X as Y;`), then to a file
  path via `composer.json`'s `autoload.psr-4` map — the same resolution PHP
  itself would do. No `use` import and no default-namespace fallback (the
  array-syntax case) means the class can't be located, so no edge is emitted
  — that mirrors a real unresolvable reference, not a shortcut.
- Not handled, by design, as a v1 scoping limit rather than an oversight:
  closures as a route target (no local entity to point at — matches
  `PhpExtractor`'s "unresolved calls aren't edges" precedent), route groups'
  `prefix`/`middleware` (the emitted route's `path` is the literal string
  passed to `Route::*`, not the fully composed URI), `Route::controller(...)
  ->group(...)` shorthand, `Route::match([...], ...)`, and grouped `use
  X\{A, B};` imports.

Entity/edge mapping (design.md §2's enums already have exactly what this
needs — no schema change beyond one new `edge_type` value):
- Each resolved route becomes one `route` entity (`raw_facts`: `http_method`,
  `path`, `controller`, and `action` for a `resource`/`apiResource` member).
- The bound controller method becomes its own lightweight `function` entity
  scoped to this extractor's own `ExtractionResult` — same pattern as
  `WordPressExtractor`'s hook-callback duplicates (`extract.py`'s `local_id`
  contract: independently local_id-scoped per extractor, reconciled by
  Agent 2's canonicalization on `(file_path, name, entity_type, raw_facts.class)`,
  `design_agent2.md` §3.2), not a reference into `PhpExtractor`'s own result.
- A `route_binding` edge connects that method entity to the route entity —
  method binds to route, mirroring `hook_binding`'s callback-binds-to-hook
  direction. Agent 2's graph stage (`design_agent2.md` §3.1) and everything
  downstream that already treats `hook_binding` as a generic "framework
  invokes this" edge picks `route_binding` up for free once it's in
  `GRAPH_EDGE_TYPES` — no other pipeline code needed to change, other than
  `test_contract.py` splitting `hooks_bound`/`routes_bound` by the target's
  actual `entity_type` so a route's name doesn't get mislabeled as a hook.
"""

import json
import re
from pathlib import Path

from document_analyzer.worker.extract import ExtractedEdge, ExtractedEntity, ExtractionResult

_ROUTE_FILES = ("routes/web.php", "routes/api.php")

_RESOURCE_ACTIONS = {
    "index": "GET",
    "create": "GET",
    "store": "POST",
    "show": "GET",
    "edit": "GET",
    "update": "PUT",
    "destroy": "DELETE",
}
_API_RESOURCE_ACTIONS = {k: v for k, v in _RESOURCE_ACTIONS.items() if k not in ("create", "edit")}

_USE_RE = re.compile(r"^\s*use\s+([A-Za-z0-9_\\]+)(?:\s+as\s+([A-Za-z0-9_]+))?\s*;", re.MULTILINE)

# The target group matches exactly the shapes we can resolve — an array
# literal, a quoted string, or a bare `Class::class` — so a closure or
# anything else simply fails to match at all, rather than matching garbage.
_ROUTE_CALL_RE = re.compile(
    r"Route::(?P<verb>get|post|put|patch|delete|options|any|resource|apiResource)\s*\(\s*"
    r"['\"](?P<path>[^'\"]*)['\"]\s*,\s*"
    r"(?P<target>\[[^\]]*\]|'[^']*'|\"[^\"]*\"|[A-Za-z0-9_\\]+::class)"
)
_ARRAY_TARGET_RE = re.compile(r"\[\s*([A-Za-z0-9_\\]+)::class\s*,\s*['\"]([A-Za-z0-9_]+)['\"]\s*\]")
_STRING_TARGET_RE = re.compile(r"^['\"]([A-Za-z0-9_\\]+)@([A-Za-z0-9_]+)['\"]$")
_CLASS_ONLY_RE = re.compile(r"^([A-Za-z0-9_\\]+)::class$")

_DEFAULT_CONTROLLER_NAMESPACE = "App\\Http\\Controllers"


class LaravelExtractor:
    """Extractor protocol implementation for Laravel route -> controller edges (design.md §5)."""

    def detect(self, repo_path: Path) -> bool:
        deps = self._composer_deps(repo_path)
        if "laravel/framework" not in deps:
            return False
        return any((repo_path / rel).exists() for rel in _ROUTE_FILES)

    def extract(self, repo_path: Path) -> ExtractionResult:
        psr4 = self._psr4_map(repo_path)
        entities: list[ExtractedEntity] = []
        edges: list[ExtractedEdge] = []
        target_ids: dict[tuple[str, str, str], str] = {}  # (file_path, class_short, method) -> local_id

        def target_entity(fqcn: str, method_name: str) -> str | None:
            file_path = self._resolve_class_file(fqcn, psr4, repo_path)
            if file_path is None:
                return None
            class_short = fqcn.rsplit("\\", 1)[-1]
            key = (file_path, class_short, method_name)
            if key not in target_ids:
                local_id = f"route_target:{file_path}:{class_short}:{method_name}"
                target_ids[key] = local_id
                entities.append(
                    ExtractedEntity(
                        local_id=local_id,
                        entity_type="function",
                        name=method_name,
                        file_path=file_path,
                        raw_facts={"class": class_short, "role": "route_target"},
                    )
                )
            return target_ids[key]

        for rel_path in _ROUTE_FILES:
            file = repo_path / rel_path
            if not file.exists():
                continue
            try:
                content = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            aliases = self._use_aliases(content)

            for match in _ROUTE_CALL_RE.finditer(content):
                verb = match.group("verb")
                path = match.group("path")
                target = match.group("target").strip()

                if verb in ("resource", "apiResource"):
                    fqcn = self._resolve_class_only(target, aliases)
                    if fqcn is None:
                        continue
                    class_short = fqcn.rsplit("\\", 1)[-1]
                    actions = _API_RESOURCE_ACTIONS if verb == "apiResource" else _RESOURCE_ACTIONS
                    for action, http_method in actions.items():
                        target_local_id = target_entity(fqcn, action)
                        if target_local_id is None:
                            continue
                        route_local_id = f"route:{rel_path}:{verb}:{path}:{action}"
                        entities.append(
                            ExtractedEntity(
                                local_id=route_local_id,
                                entity_type="route",
                                name=f"{http_method} {path} ({action})",
                                file_path=rel_path,
                                raw_facts={
                                    "http_method": http_method,
                                    "path": path,
                                    "action": action,
                                    "controller": f"{class_short}::{action}",
                                },
                            )
                        )
                        edges.append(
                            ExtractedEdge(
                                from_local_id=target_local_id, edge_type="route_binding", to_local_id=route_local_id
                            )
                        )
                    continue

                resolved = self._resolve_target(target, aliases)
                if resolved is None:
                    continue
                fqcn, method_name = resolved
                target_local_id = target_entity(fqcn, method_name)
                if target_local_id is None:
                    continue
                http_method = verb.upper()
                class_short = fqcn.rsplit("\\", 1)[-1]
                route_local_id = f"route:{rel_path}:{http_method}:{path}"
                entities.append(
                    ExtractedEntity(
                        local_id=route_local_id,
                        entity_type="route",
                        name=f"{http_method} {path}",
                        file_path=rel_path,
                        raw_facts={
                            "http_method": http_method,
                            "path": path,
                            "controller": f"{class_short}::{method_name}",
                        },
                    )
                )
                edges.append(
                    ExtractedEdge(from_local_id=target_local_id, edge_type="route_binding", to_local_id=route_local_id)
                )

        return ExtractionResult(entities=entities, edges=edges)

    @staticmethod
    def _use_aliases(content: str) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for fqcn, alias in _USE_RE.findall(content):
            fqcn = fqcn.lstrip("\\")
            short = alias or fqcn.rsplit("\\", 1)[-1]
            aliases[short] = fqcn
        return aliases

    @staticmethod
    def _resolve_class_only(target: str, aliases: dict[str, str]) -> str | None:
        m = _CLASS_ONLY_RE.match(target)
        if not m:
            return None
        short = m.group(1).lstrip("\\")
        return aliases.get(short, short) if "\\" not in short else short

    @classmethod
    def _resolve_target(cls, target: str, aliases: dict[str, str]) -> tuple[str, str] | None:
        m = _ARRAY_TARGET_RE.match(target)
        if m:
            short = m.group(1).lstrip("\\")
            method = m.group(2)
            fqcn = aliases.get(short, short) if "\\" not in short else short
            return fqcn, method

        m = _STRING_TARGET_RE.match(target)
        if m:
            class_part, method = m.group(1), m.group(2)
            if "\\" in class_part:
                return class_part.lstrip("\\"), method
            # Bare "Controller@method": Laravel's classic RouteServiceProvider
            # convention resolves this against the default controller namespace
            # even with no `use` import for it.
            return f"{_DEFAULT_CONTROLLER_NAMESPACE}\\{class_part}", method

        m = _CLASS_ONLY_RE.match(target)
        if m:
            # A bare `Controller::class` target is an invokable single-action controller.
            short = m.group(1).lstrip("\\")
            fqcn = aliases.get(short, short) if "\\" not in short else short
            return fqcn, "__invoke"

        return None  # closures and anything else unresolvable

    @staticmethod
    def _composer_deps(repo_path: Path) -> dict:
        try:
            data = json.loads((repo_path / "composer.json").read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {**(data.get("require") or {}), **(data.get("require-dev") or {})}

    @staticmethod
    def _psr4_map(repo_path: Path) -> dict[str, str]:
        try:
            data = json.loads((repo_path / "composer.json").read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return {}
        return (data.get("autoload") or {}).get("psr-4") or {}

    @staticmethod
    def _resolve_class_file(fqcn: str, psr4: dict[str, str], repo_path: Path) -> str | None:
        fqcn = fqcn.lstrip("\\")
        best_prefix, best_dir = "", None
        for prefix, directory in psr4.items():
            if fqcn.startswith(prefix) and len(prefix) > len(best_prefix):
                best_prefix, best_dir = prefix, directory
        if best_dir is None:
            return None
        remainder = fqcn[len(best_prefix):].replace("\\", "/")
        rel = f"{best_dir.rstrip('/')}/{remainder}.php"
        return rel if (repo_path / rel).exists() else None
