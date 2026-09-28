"""Graphify multi-language extraction for Agent 1 — ported from Galvanise.

`image` builds the extraction image from a curated context, `runner` executes one
disposable, hardened container per job and validates what it wrote (`validate`), and
`extractor` maps the validated output onto the `Extractor` protocol (extract.py) so it
runs beside PhpExtractor / WordPressExtractor / LaravelExtractor. See design.md §5a.
"""
