"""What a run has discovered about the repository, for the live stats — design.md §2
(`stats.updated`), tasks_frontend.md A1.6 "Discovered stats while running".

Pure over the files on disk (already boilerplate-stripped): no database, no LLM, nothing is
executed. Returns the flat `{name: number}` map `stats.updated` carries:

- `loc`: lines across recognised source files.
- `language.<Name>`: number of source files in that language.
- `framework.<Name>`: `1` for each framework the extractors recognise.
- `dependencies`: distinct packages declared in composer.json, package.json and requirements.txt.

The keys use a dot-prefixed name rather than a nested object because the stats map is flat.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Extension -> display name. Source code only: markup, styles, data and docs are not "code".
LANGUAGES = {
    ".php": "PHP", ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java", ".kt": "Kotlin", ".go": "Go",
    ".rb": "Ruby", ".rs": "Rust", ".cs": "C#", ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++",
    ".hpp": "C++", ".swift": "Swift", ".scala": "Scala", ".sh": "Shell",
}
MAX_FILE_BYTES = 2_000_000  # a larger file is counted as a file but its lines are not read
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _line_count(path: Path) -> int:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return 0
        data = path.read_bytes()
    except OSError:
        return 0
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _json_keys(path: Path, *sections: str) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return set()
    names: set[str] = set()
    for section in sections:
        block = data.get(section) if isinstance(data, dict) else None
        if isinstance(block, dict):
            names.update(block)
    return names


def _requirements(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return set()
    names = set()
    for line in lines:
        if line.lstrip().startswith(("#", "-")):
            continue
        match = _REQUIREMENT_NAME.match(line)
        if match:
            names.add(match.group(1).lower())
    return names


def count_dependencies(repo_path: Path) -> int:
    """Distinct packages declared by the repo's own manifests. `php` and `ext-*` pseudo-packages
    in composer.json are platform requirements, not dependencies."""
    packages: set[tuple[str, str]] = set()
    for manifest in repo_path.rglob("composer.json"):
        for name in _json_keys(manifest, "require", "require-dev"):
            if name != "php" and not name.startswith("ext-"):
                packages.add(("composer", name))
    for manifest in repo_path.rglob("package.json"):
        packages.update(("npm", name) for name in _json_keys(manifest, "dependencies", "devDependencies"))
    for manifest in repo_path.rglob("requirements.txt"):
        packages.update(("pip", name) for name in _requirements(manifest))
    return len(packages)


def detect_frameworks(repo_path: Path) -> list[str]:
    """Frameworks the framework extractors recognise, so the live stats and the extractors agree."""
    from document_analyzer.worker.laravel_extractor import LaravelExtractor
    from document_analyzer.worker.wordpress_extractor import WordPressExtractor

    found = []
    if WordPressExtractor().detect(repo_path):
        found.append("WordPress")
    if LaravelExtractor().detect(repo_path):
        found.append("Laravel")
    return found


def discover_repo(repo_path: Path) -> dict[str, int]:
    files_by_language: dict[str, int] = {}
    loc = 0
    for path in repo_path.rglob("*"):
        language = LANGUAGES.get(path.suffix.lower())
        if language is None or not path.is_file():
            continue
        files_by_language[language] = files_by_language.get(language, 0) + 1
        loc += _line_count(path)

    stats: dict[str, int] = {"loc": loc, "dependencies": count_dependencies(repo_path)}
    stats.update({f"language.{name}": count for name, count in files_by_language.items()})
    stats.update({f"framework.{name}": 1 for name in detect_frameworks(repo_path)})
    return stats
