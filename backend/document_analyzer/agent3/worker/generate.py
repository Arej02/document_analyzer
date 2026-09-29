"""Stage `units` step 2: generate one unit's code — design_agent3.md §4 step 2,
specs_agent3.md FR4.

One Azure OpenAI call (client from `worker/summarize.py::get_azure_openai_client`,
`model_deployment` from the job's frozen `params` — never read from env here, so a
later env change can't alter what an existing job produces) with structured output
`{files: [{path, content}], symbol_map: {...}}`, then validated:
- every path is under `app/units/{unit_slug}/`, with no `..` or absolute path;
- every file's content passes `ast.parse` (this module never `exec`s or imports it,
  CLAUDE.md "untrusted input, no code execution" — parsing is the only thing done
  with generated code here too);
- `symbol_map` covers every `test_contract.public_interface` entity id.

A validation failure counts as a failed attempt: the invalid response and a
description of the problem are fed back as the next turn in the same
conversation, up to `max_attempts` (specs FR4, design §4 step 5). Exhausting
every attempt raises `GenerationError` — a unit-level failure (`error`), never
a job-level one.
"""

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from openai import AzureOpenAI

from .context import UnitContext

# design_agent3.md §8: the rules every generation prompt includes. Static across
# every unit and every job, so its length is what callers reserve out of
# max_context_chars via `context.py::build_unit_context(..., rules_reserved_chars=...)`.
TARGET_ARCHITECTURE_RULES = """You are translating one migration unit of a legacy PHP/WordPress \
codebase into Python, to fit into an existing FastAPI project. You are given this unit's \
members (with source and facts), its test contract, a report describing what it does, the \
symbol maps and signatures of the units it depends on, and the exported symbol names of every \
other unit generated so far. Do not assume anything about the rest of the codebase beyond that.

Follow these rules exactly:
- A plain function becomes a module-level function in your own package, snake_case name, with \
type hints from the given `params` where known.
- A class and its methods become a Python class with the same methods; a static method stays \
static.
- `add_action`/`add_filter` become `@events.on("hook_name", priority=n)` at module level \
(`from app import events`). `do_action` -> `events.fire(...)`, `apply_filters` -> \
`events.apply_filters(...)`.
- `register_post_type` / post meta become a SQLAlchemy model in `app/models/{name}.py` — only \
if this unit owns that schema entity.
- `$wpdb` queries become SQLAlchemy calls through a session from `app.db`, always \
parameterized. Never build SQL by string concatenation or interpolation.
- `add_shortcode` / REST routes / `admin-ajax` handlers become an `APIRouter` exposed as your \
package's own `router` attribute, on its `__init__.py` — that is the only way the running app \
will find it.
- Any `wp_*` / PHP builtin with no direct equivalent goes through `app.wp_compat` (for example \
`get_option`, `update_post_meta`, `wp_remote_get`, `esc_html`, `__`). If `wp_compat` doesn't \
have it, add a private adapter inside your own package and mark it `# TODO(wp_compat)` — never \
modify `app.wp_compat` itself.
- No mutable module-level globals shared across units; use a constant or a settings object.
- Every file you write must be a path under `app/units/<your unit_slug>/` (given below) — \
nothing outside it, and never `app/main.py`, `app/events.py`, `app/db.py` or `app/wp_compat/*`.
- Your package's `__init__.py` must import every other file in your package (for example \
`importlib.import_module(f"{__name__}.module")`), so that importing your package alone — which \
is what the running app and every contract test do — always runs your module-level code (hook \
registrations) and exposes your `router`, whichever file you actually put them in.

Respond with a single JSON object and nothing else, in this exact shape:
{"files": [{"path": "app/units/<unit_slug>/....py", "content": "..."}],
 "symbol_map": {"<entity_id>": {"python_module": "...", "python_name": "...", "kind": "function|method|class"}}}

`symbol_map` must have one entry for every `public_interface` entity id in the test contract \
you were given, naming exactly where that symbol lives in the files you wrote. For a method \
(`kind: "method"`), `python_name` is "ClassName.method_name" and `python_module` is the module \
the class itself lives in — that is the only way a downstream contract test can tell which \
class a method landed on from these three fields."""


class GenerationError(Exception):
    """Every attempt was invalid; unit_executions.status = 'error' (never a job-level failure)."""


class _ValidationFailure(Exception):
    """One attempt's response didn't validate; drives the retry loop, never escapes this module."""


@dataclass(frozen=True)
class GeneratedFile:
    path: str
    content: str


@dataclass(frozen=True)
class GeneratedUnit:
    files: list[GeneratedFile] = field(default_factory=list)
    symbol_map: dict[str, dict] = field(default_factory=dict)
    attempts: int = 0


def _validate_path(path: str, prefix: str) -> None:
    posix_path = PurePosixPath(path)
    if posix_path.is_absolute() or ".." in posix_path.parts:
        raise _ValidationFailure(f"unsafe file path: {path!r}")
    if not path.startswith(prefix):
        raise _ValidationFailure(f"file path must be under {prefix!r}: {path!r}")


def _validate_files(raw_files, prefix: str) -> list[GeneratedFile]:
    if not isinstance(raw_files, list) or not raw_files:
        raise _ValidationFailure(f"'files' must be a non-empty list: {raw_files!r}")
    files: list[GeneratedFile] = []
    for entry in raw_files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(
            entry.get("content"), str
        ):
            raise _ValidationFailure(f"each file must be {{'path': str, 'content': str}}: {entry!r}")
        path, content = entry["path"], entry["content"]
        _validate_path(path, prefix)
        try:
            ast.parse(content, filename=path)
        except SyntaxError as exc:
            raise _ValidationFailure(f"{path} does not parse as Python: {exc}") from exc
        files.append(GeneratedFile(path=path, content=content))
    return files


def _validate_symbol_map(raw_symbol_map, context: UnitContext) -> dict[str, dict]:
    if not isinstance(raw_symbol_map, dict):
        raise _ValidationFailure(f"'symbol_map' must be an object: {raw_symbol_map!r}")

    required_ids = {entry["entity_id"] for entry in (context.test_contract.get("public_interface") or [])}
    missing = sorted(required_ids - raw_symbol_map.keys())
    if missing:
        raise _ValidationFailure(f"symbol_map is missing entries for public_interface ids: {missing}")

    for entity_id, symbol in raw_symbol_map.items():
        required_keys = ("python_module", "python_name", "kind")
        if not (isinstance(symbol, dict) and all(isinstance(symbol.get(k), str) and symbol.get(k) for k in required_keys)):
            raise _ValidationFailure(
                f"symbol_map[{entity_id!r}] must have non-empty python_module/python_name/kind: {symbol!r}"
            )
    return raw_symbol_map


def _validate_response(content: str | None, unit_slug: str, context: UnitContext) -> tuple[list[GeneratedFile], dict]:
    try:
        parsed = json.loads(content) if content is not None else None
    except json.JSONDecodeError as exc:
        raise _ValidationFailure(f"response was not valid JSON: {content!r}") from exc
    if not isinstance(parsed, dict):
        raise _ValidationFailure(f"response JSON was not an object: {parsed!r}")

    files = _validate_files(parsed.get("files"), f"app/units/{unit_slug}/")
    symbol_map = _validate_symbol_map(parsed.get("symbol_map"), context)
    return files, symbol_map


def _initial_messages(context: UnitContext, unit_slug: str, extra_feedback: str | None = None) -> list[dict]:
    system = f"{TARGET_ARCHITECTURE_RULES}\n\nYour unit_slug is: {unit_slug}"
    user = context.text
    if extra_feedback:
        # units.py's outer retry loop (design_agent3.md §4 step 5): the previous
        # outer round produced code that parsed and covered the contract, but
        # failed its tests in the sandbox. Distinct from `problem` below, which is
        # this function's own inner loop reacting to its own validation failures.
        user = f"{context.text}\n\nThe previous attempt's generated code failed these tests:\n{extra_feedback}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def generate_unit_code(
    context: UnitContext,
    unit_slug: str,
    llm_client: AzureOpenAI,
    model_deployment: str,
    max_attempts: int,
    extra_feedback: str | None = None,
) -> GeneratedUnit:
    """design_agent3.md §4 step 2. Raises `GenerationError` if every attempt is invalid.

    `extra_feedback` seeds the initial prompt with a previous *outer* round's sandbox
    test failures (units.py, §4 step 5) — a different retry loop than this function's
    own inner one, which only reacts to its own validation failures (bad JSON, path,
    syntax, missing symbol). Optional and additive: omitted, this is unchanged from
    before 2026-09-28."""
    messages = _initial_messages(context, unit_slug, extra_feedback)
    problem = ""

    for attempt in range(1, max_attempts + 1):
        response = llm_client.chat.completions.create(
            model=model_deployment,
            response_format={"type": "json_object"},
            messages=messages,
        )
        content = response.choices[0].message.content
        try:
            files, symbol_map = _validate_response(content, unit_slug, context)
        except _ValidationFailure as exc:
            problem = str(exc)
            messages = [
                *messages,
                {"role": "assistant", "content": content or ""},
                {
                    "role": "user",
                    "content": f"That response was invalid: {problem}. Fix it and respond again with the "
                    "same JSON shape and nothing else.",
                },
            ]
            continue
        return GeneratedUnit(files=files, symbol_map=symbol_map, attempts=attempt)

    raise GenerationError(f"unit {unit_slug}: generation failed after {max_attempts} attempts: {problem}")


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _snake_case(name: str) -> str:
    """Same conversion as `tests_gen.py`'s (duplicated, not imported, to avoid a
    circular import: `tests_gen.py` already imports from this module)."""
    return _CAMEL_BOUNDARY.sub("_", name).lower()


def _stub_function_source(name: str, params: list[str]) -> str:
    return f"def {name}({', '.join(params)}):\n    raise NotImplementedError({name!r})"


def _stub_class_source(class_name: str, methods: list[dict]) -> str:
    blocks = [
        f"    def {m['name']}({', '.join(['self', *m['params']])}):\n        raise NotImplementedError({m['name']!r})"
        for m in sorted(methods, key=lambda m: m["entity_id"])
    ]
    return f"class {class_name}:\n" + "\n\n".join(blocks)


def generate_stub_unit(item: dict, unit_slug: str) -> GeneratedUnit:
    """design_agent3.md §4 "Failed dependencies", specs FR7. Deterministic (no LLM)
    stand-in for a unit that produced no usable code (`error`) or a transitive
    dependency that hasn't run yet (an unresolved cross-phase `needs_facade` cycle,
    §13, can put a "dependency" after the current unit in plan order). Every
    `public_interface` entry becomes a function or method that raises
    `NotImplementedError`; the symbol map uses the exact same
    `{python_module, python_name, kind}` shape — including the `ClassName.method_name`
    dotted convention for methods (§6) — as a real `GeneratedUnit`'s, so downstream
    context-building, contract-test generation and the workspace's own imports treat
    a stub identically to a real attempt."""
    public_interface = (item.get("test_contract") or {}).get("public_interface") or []
    module = f"app.units.{unit_slug}.module"

    functions: list[str] = []
    classes: dict[str, list[dict]] = {}
    symbol_map: dict[str, dict] = {}

    for entry in sorted(public_interface, key=lambda e: e["entity_id"]):
        name = entry["name"]
        params = [_snake_case(p) for p in (entry.get("params") or []) if p]
        class_name = entry.get("class")
        if entry.get("kind") == "method" and class_name:
            classes.setdefault(class_name, []).append({"entity_id": entry["entity_id"], "name": name, "params": params})
            symbol_map[entry["entity_id"]] = {"python_module": module, "python_name": f"{class_name}.{name}", "kind": "method"}
        else:
            functions.append(_stub_function_source(name, params))
            symbol_map[entry["entity_id"]] = {"python_module": module, "python_name": name, "kind": "function"}

    class_blocks = [_stub_class_source(name, methods) for name, methods in sorted(classes.items())]
    body = "\n\n\n".join(
        ['"""Stub: this unit\'s generation did not produce usable code; every symbol below raises NotImplementedError."""',
         *functions, *class_blocks]
    )
    return GeneratedUnit(
        files=[GeneratedFile(path=f"app/units/{unit_slug}/module.py", content=body + "\n")],
        symbol_map=symbol_map,
        attempts=0,
    )
