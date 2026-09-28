"""Capability manifest built from the installed Graphify release.

The extension list comes from Graphify's own dispatch table at runtime, so an extension
missing from the descriptive tables below is still reported (as language ``unmapped``)
rather than silently dropped. Availability is established by actually parsing a small
sample per language inside the worker image, not by trusting package metadata.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.metadata
import io
import platform
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import CAPABILITY_SCHEMA_VERSION, canonical_dumps, sha256_hex

GRAPHIFY_DIST = "graphifyy"


@dataclass(frozen=True)
class LanguageInfo:
    language: str
    category: str  # code | build | config | markup | document
    mode: str  # tree-sitter | custom | robotframework | xml
    parser_dist: str | None = None  # distribution providing the parser
    parser_module: str | None = None  # importable module proving the parser is present
    fallback_mode: str | None = None  # upstream fallback when the parser is absent


# Keyed by Graphify extractor function name (verified against graphifyy 0.9.68 source).
EXTRACTORS: dict[str, LanguageInfo] = {
    "extract_python": LanguageInfo(
        "python", "code", "tree-sitter", "tree-sitter-python", "tree_sitter_python"
    ),
    "extract_js": LanguageInfo(
        "javascript/typescript",
        "code",
        "tree-sitter",
        "tree-sitter-typescript",
        "tree_sitter_typescript",
    ),
    "extract_go": LanguageInfo("go", "code", "tree-sitter", "tree-sitter-go", "tree_sitter_go"),
    "extract_rust": LanguageInfo(
        "rust", "code", "tree-sitter", "tree-sitter-rust", "tree_sitter_rust"
    ),
    "extract_r": LanguageInfo(
        "r", "code", "tree-sitter", "tree-sitter-language-pack", "tree_sitter_language_pack"
    ),
    "extract_solidity": LanguageInfo(
        "solidity", "code", "tree-sitter", "tree-sitter-solidity", "tree_sitter_solidity"
    ),
    "extract_java": LanguageInfo(
        "java", "code", "tree-sitter", "tree-sitter-java", "tree_sitter_java"
    ),
    "extract_groovy": LanguageInfo(
        "groovy", "code", "tree-sitter", "tree-sitter-groovy", "tree_sitter_groovy"
    ),
    "extract_c": LanguageInfo("c", "code", "tree-sitter", "tree-sitter-c", "tree_sitter_c"),
    "extract_cpp": LanguageInfo("cpp", "code", "tree-sitter", "tree-sitter-cpp", "tree_sitter_cpp"),
    "extract_ruby": LanguageInfo(
        "ruby", "code", "tree-sitter", "tree-sitter-ruby", "tree_sitter_ruby"
    ),
    "extract_csharp": LanguageInfo(
        "csharp", "code", "tree-sitter", "tree-sitter-c-sharp", "tree_sitter_c_sharp"
    ),
    "extract_cobol": LanguageInfo("cobol", "code", "custom"),
    "extract_vbnet": LanguageInfo(
        "vbnet", "code", "tree-sitter", "tree-sitter-vb-dotnet", "tree_sitter_vb_dotnet"
    ),
    "extract_kotlin": LanguageInfo(
        "kotlin", "code", "tree-sitter", "tree-sitter-kotlin", "tree_sitter_kotlin"
    ),
    "extract_scala": LanguageInfo(
        "scala", "code", "tree-sitter", "tree-sitter-scala", "tree_sitter_scala"
    ),
    "extract_php": LanguageInfo("php", "code", "tree-sitter", "tree-sitter-php", "tree_sitter_php"),
    "extract_blade": LanguageInfo("blade", "code", "custom"),
    "extract_swift": LanguageInfo(
        "swift", "code", "tree-sitter", "tree-sitter-swift", "tree_sitter_swift"
    ),
    "extract_lua": LanguageInfo("lua", "code", "tree-sitter", "tree-sitter-lua", "tree_sitter_lua"),
    "extract_zig": LanguageInfo("zig", "code", "tree-sitter", "tree-sitter-zig", "tree_sitter_zig"),
    "extract_powershell": LanguageInfo(
        "powershell", "code", "tree-sitter", "tree-sitter-powershell", "tree_sitter_powershell"
    ),
    "extract_powershell_manifest": LanguageInfo(
        "powershell-manifest",
        "config",
        "tree-sitter",
        "tree-sitter-powershell",
        "tree_sitter_powershell",
    ),
    "extract_elixir": LanguageInfo(
        "elixir", "code", "tree-sitter", "tree-sitter-elixir", "tree_sitter_elixir"
    ),
    "extract_erlang": LanguageInfo(
        "erlang", "code", "tree-sitter", "tree-sitter-language-pack", "tree_sitter_language_pack"
    ),
    "extract_objc": LanguageInfo(
        "objective-c", "code", "tree-sitter", "tree-sitter-objc", "tree_sitter_objc"
    ),
    "extract_julia": LanguageInfo(
        "julia", "code", "tree-sitter", "tree-sitter-julia", "tree_sitter_julia"
    ),
    "extract_fortran": LanguageInfo(
        "fortran", "code", "tree-sitter", "tree-sitter-fortran", "tree_sitter_fortran"
    ),
    "extract_vue": LanguageInfo(
        "vue", "code", "tree-sitter", "tree-sitter-typescript", "tree_sitter_typescript"
    ),
    "extract_svelte": LanguageInfo(
        "svelte", "code", "tree-sitter", "tree-sitter-typescript", "tree_sitter_typescript"
    ),
    "extract_astro": LanguageInfo(
        "astro", "code", "tree-sitter", "tree-sitter-typescript", "tree_sitter_typescript"
    ),
    "extract_dart": LanguageInfo("dart", "code", "custom"),
    "extract_ocaml": LanguageInfo(
        "ocaml", "code", "tree-sitter", "tree-sitter-ocaml", "tree_sitter_ocaml"
    ),
    "extract_commonlisp": LanguageInfo(
        "commonlisp", "code", "tree-sitter", "tree-sitter-commonlisp", "tree_sitter_commonlisp"
    ),
    "extract_verilog": LanguageInfo(
        "verilog", "code", "tree-sitter", "tree-sitter-verilog", "tree_sitter_verilog"
    ),
    "extract_sql": LanguageInfo("sql", "code", "tree-sitter", "tree-sitter-sql", "tree_sitter_sql"),
    "extract_markdown": LanguageInfo("markdown", "document", "custom"),
    "extract_pascal": LanguageInfo(
        "pascal",
        "code",
        "tree-sitter",
        "tree-sitter-pascal",
        "tree_sitter_pascal",
        fallback_mode="regex",
    ),
    "extract_delphi_form": LanguageInfo("delphi-form", "markup", "custom"),
    "extract_lazarus_form": LanguageInfo("lazarus-form", "markup", "custom"),
    "extract_lazarus_package": LanguageInfo("lazarus-package", "build", "xml"),
    "extract_bash": LanguageInfo(
        "bash", "code", "tree-sitter", "tree-sitter-bash", "tree_sitter_bash"
    ),
    "extract_json": LanguageInfo(
        "json", "config", "tree-sitter", "tree-sitter-json", "tree_sitter_json"
    ),
    "extract_terraform": LanguageInfo(
        "terraform", "code", "tree-sitter", "tree-sitter-hcl", "tree_sitter_hcl"
    ),
    "extract_dm": LanguageInfo("dm", "code", "tree-sitter", "tree-sitter-dm", "tree_sitter_dm"),
    "extract_dmi": LanguageInfo("dm-icon", "markup", "custom"),
    "extract_dmm": LanguageInfo("dm-map", "markup", "custom"),
    "extract_dmf": LanguageInfo("dm-interface", "markup", "custom"),
    "extract_sln": LanguageInfo("visual-studio-solution", "build", "custom"),
    "extract_slnx": LanguageInfo("visual-studio-solution", "build", "xml"),
    "extract_csproj": LanguageInfo("msbuild-project", "build", "xml"),
    "extract_xaml": LanguageInfo("xaml", "markup", "xml"),
    "extract_razor": LanguageInfo("razor", "code", "custom"),
    "extract_robot": LanguageInfo("robot", "code", "robotframework", "robotframework", "robot.api"),
    "extract_apex": LanguageInfo("apex", "code", "custom"),
}

# Minimal smoke samples: each must parse into at least one symbol beyond the file node
# unless the language is listed in FILE_NODE_ONLY_OK.
SAMPLES: dict[str, str] = {
    ".py": "def hello():\n    return 1\n",
    ".js": "function hello() { return 1; }\n",
    ".jsx": "function Hello() { return <div/>; }\n",
    ".mjs": "export function hello() { return 1; }\n",
    ".cjs": "function hello() { return 1; }\nmodule.exports = { hello };\n",
    ".ts": "export function hello(): number { return 1; }\n",
    ".tsx": "export function Hello() { return <div/>; }\n",
    ".mts": "export function hello(): number { return 1; }\n",
    ".cts": "export function hello(): number { return 1; }\n",
    ".go": "package main\n\nfunc Hello() int { return 1 }\n",
    ".rs": "fn hello() -> i32 { 1 }\n",
    ".r": "hello <- function(x) {\n  x + 1\n}\n",
    ".sol": "pragma solidity ^0.8.0;\ncontract Hello { function hi() public {} }\n",
    ".java": "class Hello { void hi() {} }\n",
    ".groovy": "class Hello { def hi() { 1 } }\n",
    ".gradle": "task hello {\n  doLast { println 'hi' }\n}\n",
    ".c": "int hello(void) { return 1; }\n",
    ".h": "int hello(void);\n",
    ".cpp": "class Hello { public: int hi() { return 1; } };\n",
    ".cc": "int hello() { return 1; }\n",
    ".cxx": "int hello() { return 1; }\n",
    ".hpp": "class Hello { public: int hi(); };\n",
    ".cu": "__global__ void hello() {}\n",
    ".cuh": "int hello();\n",
    ".metal": "int hello() { return 1; }\n",
    ".rb": "class Hello\n  def hi\n    1\n  end\nend\n",
    ".rake": "def hello\n  1\nend\n",
    ".cs": "class Hello { void Hi() {} }\n",
    ".cbl": (
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. HELLO.\n"
        "       PROCEDURE DIVISION.\n       MAIN-PARA.\n           STOP RUN.\n"
    ),
    ".vb": "Public Class Hello\n    Public Sub Hi()\n    End Sub\nEnd Class\n",
    ".kt": "class Hello {\n    fun hi(): Int {\n        return 1\n    }\n}\n",
    ".kts": "fun hello(): Int {\n    return 1\n}\n",
    ".scala": "object Hello { def hi(): Int = 1 }\n",
    ".php": "<?php\nfunction hello() { return 1; }\n",
    ".swift": "func hello() -> Int { return 1 }\n",
    ".lua": "function hello()\n  return 1\nend\n",
    ".zig": "fn hello() i32 {\n    return 1;\n}\n",
    ".ps1": "function Get-Hello {\n  return 1\n}\n",
    ".psd1": "@{\n  RootModule = 'Hello.psm1'\n  ModuleVersion = '1.0'\n}\n",
    ".ex": "defmodule Hello do\n  def hi, do: 1\nend\n",
    ".erl": "-module(hello).\n-export([hi/0]).\nhi() -> 1.\n",
    ".m": "@interface Hello : NSObject\n- (int)hi;\n@end\n@implementation Hello\n"
    "- (int)hi { return 1; }\n@end\n",
    ".mm": "@interface Hello : NSObject\n- (int)hi;\n@end\n",
    ".jl": "function hello()\n    return 1\nend\n",
    ".f90": "subroutine hello()\n  print *, 'hi'\nend subroutine hello\n",
    ".vue": "<script>\nexport default { methods: { hi() { return 1 } } }\n"
    "function helper() { return 1 }\n</script>\n",
    ".svelte": "<script>\n  function hello() { return 1; }\n</script>\n<p>hi</p>\n",
    ".astro": "---\nfunction hello() { return 1; }\n---\n<p>hi</p>\n",
    ".dart": "class Hello {\n  int hi() {\n    return 1;\n  }\n}\n",
    ".ml": "let hello x = x + 1\n",
    ".mli": "val hello : int -> int\n",
    ".lisp": "(defun hello (x) (+ x 1))\n",
    ".v": "module hello(input a, output b);\n  assign b = a;\nendmodule\n",
    ".sv": "module hello(input a, output b);\n  assign b = a;\nendmodule\n",
    ".sql": "CREATE TABLE hello (id INT);\nCREATE VIEW hi AS SELECT id FROM hello;\n",
    ".md": "# Hello\n\n## Section\n\nText.\n",
    ".pas": "unit Hello;\ninterface\nprocedure Hi;\nimplementation\nprocedure Hi;\n"
    "begin\nend;\nend.\n",
    ".dfm": "object Form1: TForm1\n  Caption = 'Hi'\n  object Button1: TButton\n  end\nend\n",
    ".lfm": "object Form1: TForm1\n  Caption = 'Hi'\n  object Button1: TButton\n  end\nend\n",
    ".lpk": '<?xml version="1.0"?>\n<CONFIG>\n  <Package Version="4">\n    <Name Value="hello"/>\n'
    '    <Files Count="1">\n      <Item1>\n        <Filename Value="hello.pas"/>\n'
    "      </Item1>\n    </Files>\n  </Package>\n</CONFIG>\n",
    ".sh": "hello() {\n  echo hi\n}\n",
    ".json": '{"name": "hello", "dependencies": {"left-pad": "1.3.0"}}\n',
    ".tf": 'resource "aws_s3_bucket" "hello" {\n  bucket = "hello"\n}\n',
    ".dm": "/proc/hello()\n\treturn 1\n",
    ".dme": '#include "hello.dm"\n',
    ".dmi": "not a PNG icon sheet\n",
    ".dmm": '"a" = (/turf/floor,/area/hello)\n\n(1,1,1) = {"\na\n"}\n',
    ".dmf": 'window "main"\n\telem "map"\n\t\ttype = MAP\n',
    ".f": "      SUBROUTINE HELLO\n      PRINT *, 'HI'\n      END\n",
    ".F": "      SUBROUTINE HELLO\n      PRINT *, 'HI'\n      END\n",
    ".cob": (
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. HELLO.\n"
        "       PROCEDURE DIVISION.\n       MAIN-PARA.\n           STOP RUN.\n"
    ),
    ".cpy": "       01 HELLO-REC.\n          05 HELLO-ID PIC 9(4).\n",
    ".sln": (
        "Microsoft Visual Studio Solution File, Format Version 12.00\n"
        'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Hello", "Hello\\Hello.csproj", '
        '"{11111111-1111-1111-1111-111111111111}"\nEndProject\n'
    ),
    ".slnx": '<Solution>\n  <Project Path="Hello/Hello.csproj" />\n</Solution>\n',
    ".csproj": '<Project Sdk="Microsoft.NET.Sdk">\n  <ItemGroup>\n'
    '    <PackageReference Include="Newtonsoft.Json" Version="13.0.1" />\n'
    "  </ItemGroup>\n</Project>\n",
    ".xaml": '<Window x:Class="Hello.MainWindow" '
    'xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" '
    'xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">\n'
    '  <Button Click="OnClick" />\n</Window>\n',
    ".razor": '@page "/hello"\n<h1>Hi</h1>\n@code {\n  void Hi() { }\n}\n',
    ".robot": "*** Keywords ***\nSay Hello\n    Log    hi\n",
    ".cls": "public class Hello {\n    public void hi() { }\n}\n",
    ".trigger": "trigger HelloTrigger on Account (before insert) {\n}\n",
}

# Extractors whose smallest self-contained sample yields only a file-level node: header
# declarations, binary icon sheets, and import-oriented template/config extractors that
# emit relations only when the referenced files exist. For these the smoke check proves the
# parser loads and accepts the file without error.
FILE_NODE_ONLY_OK = frozenset(
    {".dmi", ".dmm", ".h", ".cuh", ".dme", ".astro", ".svelte", ".gradle", ".psd1", ".cpy"}
)


def _sample_for(ext: str, extractor_name: str, dispatch: dict[str, Any]) -> tuple[str, str] | None:
    """Sample for ``ext``, else the sample of a sibling extension with the same extractor."""
    if ext in SAMPLES:
        return ext, SAMPLES[ext]
    for other in sorted(dispatch):
        other_name = getattr(dispatch[other], "__name__", "")
        if other_name == extractor_name and other in SAMPLES and other.lower() == other:
            return other, SAMPLES[other]
    return None


def _dist_version(dist: str | None) -> str | None:
    if not dist:
        return None
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def _module_present(module: str | None) -> bool:
    if not module:
        return True
    try:
        importlib.import_module(module)
    except Exception:
        return False
    return True


def graphify_version() -> str:
    return importlib.metadata.version(GRAPHIFY_DIST)


def _smoke(
    ext: str, extractor: Any, safe_extract: Any, workdir: Path, dispatch: dict[str, Any]
) -> dict[str, Any]:
    found = _sample_for(ext, getattr(extractor, "__name__", ""), dispatch)
    if found is None:
        return {"smoke": "no_sample"}
    sample_ext, sample = found
    path = workdir / f"sample{ext}"
    path.write_text(sample, encoding="utf-8")
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        result = safe_extract(extractor, path, scan_root=workdir)
    nodes = result.get("nodes") or []
    if result.get("error"):
        return {"smoke": "error", "smoke_error": str(result["error"])[:300]}
    path.unlink()
    detail: dict[str, Any] = {"smoke_nodes": len(nodes), "smoke_sample": sample_ext}
    symbols = max(len(nodes) - 1, 0)
    if not nodes:
        return {"smoke": "no_nodes", **detail}
    if symbols == 0 and ext not in FILE_NODE_ONLY_OK:
        return {"smoke": "no_symbols", **detail}
    basis = "symbols" if symbols else "file-accepted"
    return {"smoke": "ok", "smoke_basis": basis, **detail}


def build_manifest(run_smoke: bool = True) -> dict[str, Any]:
    from graphify import extract as gx  # imported lazily: only present in the worker image

    dispatch: dict[str, Any] = dict(gx._DISPATCH)
    extras_for_ext: dict[str, str] = dict(getattr(gx, "_EXTRA_FOR_EXTENSION", {}))
    entries: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="graphify-cap-") as tmp:
        workdir = Path(tmp)
        for ext in sorted(dispatch):
            extractor = dispatch[ext]
            fname = getattr(extractor, "__name__", repr(extractor))
            info = EXTRACTORS.get(fname)
            parser_present = _module_present(info.parser_module) if info else False
            if info is None:
                availability = "unmapped"
            elif parser_present:
                availability = "installed"
            elif info.fallback_mode:
                availability = "fallback"
            else:
                availability = "unavailable"
            entry: dict[str, Any] = {
                "extension": ext,
                "language": info.language if info else "unmapped",
                "category": info.category if info else "unknown",
                "extractor": fname,
                "mode": (
                    info.fallback_mode
                    if info and availability == "fallback"
                    else (info.mode if info else "unknown")
                ),
                "parser_dist": info.parser_dist if info else None,
                "parser_version": _dist_version(info.parser_dist) if info else None,
                "graphify_extra": extras_for_ext.get(ext),
                "availability": availability,
            }
            if run_smoke:
                entry.update(_smoke(ext, extractor, gx._safe_extract, workdir, dispatch))
            entries.append(entry)
    from graphify.manifest_ingest import PACKAGE_MANIFEST_NAMES
    from graphify.mcp_ingest import MCP_CONFIG_FILENAMES

    routes = {
        # Routed by exact (lower-cased) filename before suffix dispatch.
        "package_manifests": sorted(PACKAGE_MANIFEST_NAMES),
        # Routed by exact filename before generic JSON.
        "mcp_configs": sorted(MCP_CONFIG_FILENAMES),
        # Extensionless files whose shebang names one of these interpreters.
        "shebang_interpreters": sorted(getattr(gx, "_SHEBANG_DISPATCH", {})),
        "blade_suffix": ".blade.php",
        "notes": [
            "*.m files without Objective-C markers are declined upstream (reported unsupported)",
            ".h files are routed to C, C++ or Objective-C by content",
        ],
    }
    body = {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "graphify_dist": GRAPHIFY_DIST,
        "graphify_version": graphify_version(),
        "python_version": platform.python_version(),
        "machine": platform.machine(),
        "extensions": entries,
        "filename_routes": routes,
    }
    body["digest"] = sha256_hex(canonical_dumps({k: v for k, v in body.items()}))
    return body


def check_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return problems that must fail an image build (advertised capability not working)."""
    problems = []
    for entry in manifest["extensions"]:
        ext = entry["extension"]
        if entry["availability"] in ("unavailable", "unmapped"):
            problems.append(f"{ext}: parser {entry['availability']} ({entry['extractor']})")
        smoke = entry.get("smoke")
        if smoke not in (None, "ok"):
            detail = entry.get("smoke_error") or f"nodes={entry.get('smoke_nodes')}"
            problems.append(f"{ext}: smoke {smoke} {detail}")
    return problems


def main_capabilities() -> int:
    sys.stdout.write(canonical_dumps(build_manifest()) + "\n")
    return 0
