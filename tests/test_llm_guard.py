"""Guard: `shared/llm.py` is the only module that may talk to the `openai` SDK.

Every Azure OpenAI call has to go through `MeteredLLMClient` so it is recorded in
`llm_invocations` (design.md section 7). A module that imports `openai` or builds its
own `AzureOpenAI` could make an unmetered call, so this scans the source tree and fails
on any such module other than `shared/llm.py`. Annotations use the `LLMClient` protocol
from `shared/llm.py` instead.
"""

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "backend" / "document_analyzer"
ALLOWED = PACKAGE_ROOT / "shared" / "llm.py"
# Source trees that are not part of the package's own code.
SKIPPED_DIRS = {".venv", "__pycache__", "node_modules"}


def _python_files():
    for path in PACKAGE_ROOT.rglob("*.py"):
        if SKIPPED_DIRS.isdisjoint(path.parts):
            yield path


def _violations(source: str) -> list[str]:
    """Descriptions of every openai import or AzureOpenAI use in `source`."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if a.name.split(".")[0] == "openai"]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").split(".")[0] == "openai":
                found.append(f"from {node.module} import ...")
            found += [f"import of {a.name}" for a in node.names if a.name == "AzureOpenAI"]
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name in ("AzureOpenAI", "AsyncAzureOpenAI", "OpenAI", "AsyncOpenAI"):
                found.append(f"{name}(...) call at line {node.lineno}")
    # `importlib.import_module("openai")` / `__import__("openai")` would dodge the above.
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            value = node.args[0].value
            if name in ("import_module", "__import__") and isinstance(value, str) and value.split(".")[0] == "openai":
                found.append(f"{name}({value!r}) at line {node.lineno}")
    return found


def test_only_shared_llm_touches_the_openai_sdk():
    offenders = {}
    scanned = 0
    for path in _python_files():
        scanned += 1
        if path == ALLOWED:
            continue
        found = _violations(path.read_text(encoding="utf-8"))
        if found:
            offenders[str(path.relative_to(PACKAGE_ROOT))] = found
    assert scanned > 50, f"guard scanned only {scanned} files; is PACKAGE_ROOT {PACKAGE_ROOT} right?"
    assert not offenders, (
        "only shared/llm.py may import openai or construct AzureOpenAI (design.md section 7); "
        f"use LLMClient for annotations and get_azure_openai_client() for a client: {offenders}"
    )


def test_the_allowed_module_really_is_the_one_that_uses_openai():
    # Keeps the guard honest: if shared/llm.py stopped using the SDK, or moved, the guard's
    # exemption would silently protect nothing.
    assert ALLOWED.exists()
    assert _violations(ALLOWED.read_text(encoding="utf-8"))


def test_the_scanner_catches_each_way_of_reaching_the_sdk():
    for bad in (
        "import openai",
        "import openai.types",
        "from openai import AzureOpenAI",
        "from openai.lib.azure import AzureOpenAI",
        "from somewhere import AzureOpenAI",
        "client = AzureOpenAI(api_key='x')",
        "client = openai.AzureOpenAI()",
        "importlib.import_module('openai')",
    ):
        assert _violations(bad), bad
    for fine in (
        "from document_analyzer.shared.llm import LLMClient",
        "def f(client: LLMClient): ...",
        "import json",
    ):
        assert not _violations(fine), fine
