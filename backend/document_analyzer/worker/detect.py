"""Worker step: language/framework detection — tasks.md Phase 1
"Language/framework detection: plain PHP vs. WordPress plugin/theme structure".

Cheap structural heuristics only (file/dir presence, header comments) — no
parsing, no LLM calls. This is bookkeeping ahead of Phase 2's `Extractor`
protocol (design.md §5), which does its own, per-extractor `detect()` for
choosing which extractor actually runs on which files.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Framework = Literal["wordpress", "plain_php"]
WordPressStructure = Literal["plugin", "theme", "site"]

# Standard WordPress plugin/theme header comment blocks — see
# https://developer.wordpress.org/plugins/plugin-basics/header-requirements/
_PLUGIN_HEADER_RE = re.compile(rb"^[ \t]*\*?[ \t]*Plugin Name[ \t]*:", re.IGNORECASE | re.MULTILINE)
_THEME_HEADER_RE = re.compile(rb"^[ \t]*\*?[ \t]*Theme Name[ \t]*:", re.IGNORECASE | re.MULTILINE)

_WORDPRESS_CORE_MARKERS = ("wp-config.php", "wp-load.php", "wp-settings.php")
_WORDPRESS_CORE_DIRS = ("wp-content", "wp-includes", "wp-admin")

_HEADER_SCAN_DEPTH = 2  # repo root, or one directory down (e.g. plugin-slug/plugin-slug.php)
_HEADER_SCAN_BYTES = 4096


@dataclass(frozen=True)
class LanguageProfile:
    framework: Framework
    wordpress_structure: WordPressStructure | None
    php_file_count: int


def detect_language(repo_path: Path) -> LanguageProfile:
    php_files = list(repo_path.rglob("*.php"))

    wordpress_structure = _detect_wordpress_structure(repo_path, php_files)
    framework: Framework = "wordpress" if wordpress_structure else "plain_php"

    return LanguageProfile(
        framework=framework,
        wordpress_structure=wordpress_structure,
        php_file_count=len(php_files),
    )


def _detect_wordpress_structure(repo_path: Path, php_files: list[Path]) -> WordPressStructure | None:
    if any((repo_path / marker).is_file() for marker in _WORDPRESS_CORE_MARKERS):
        return "site"
    if any((repo_path / directory).is_dir() for directory in _WORDPRESS_CORE_DIRS):
        return "site"

    if _any_shallow_file_matches(repo_path, php_files, _PLUGIN_HEADER_RE):
        return "plugin"

    style_css = repo_path / "style.css"
    if style_css.is_file() and _matches_header(style_css, _THEME_HEADER_RE):
        return "theme"

    return None


def _any_shallow_file_matches(repo_path: Path, candidates: list[Path], pattern: re.Pattern) -> bool:
    for candidate in candidates:
        try:
            depth = len(candidate.relative_to(repo_path).parts)
        except ValueError:
            continue
        if depth > _HEADER_SCAN_DEPTH:
            continue
        if _matches_header(candidate, pattern):
            return True
    return False


def _matches_header(file_path: Path, pattern: re.Pattern) -> bool:
    try:
        head = file_path.read_bytes()[:_HEADER_SCAN_BYTES]
    except OSError:
        return False
    return bool(pattern.search(head))
