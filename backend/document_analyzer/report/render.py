"""Report HTML renderer (design_report.md §8.2): `report_json` -> one self-contained HTML file.

A deterministic Jinja2 render (same `report_json` + same `TEMPLATE_VERSION` gives the same
bytes). The file has inline CSS, the two embedded fonts as data URIs, no scripts, no external
URLs and no `on*` attributes. Every value goes through autoescape: entity names, paths and
summaries come from the analysed repository and are attacker-controlled, so they only ever
appear as text nodes. The only unescaped content is this module's own static CSS and font
data. Charts (`charts.py`, a later Phase 5 task) will be passed in as pre-built, trusted SVG.

`TEMPLATE_VERSION` is pinned here and recorded in `report_json.inputs.template_version`; bump
it whenever `templates/` changes the output.
"""

import base64
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

TEMPLATE_VERSION = "2"

TEMPLATES_DIR = Path(__file__).with_name("templates")
TEMPLATE_NAME = "report.html.j2"

TIER_LABELS = {"xray": "Codebase X-ray", "full": "Full Assessment"}
NOT_RECORDED = "Not recorded for this run"
WITHHELD_SUMMARY_TEXT = "The executive summary was withheld by a content check."

# Same Latin subsets the app uses (frontend/src/fonts.css), variable weight.
_FONTS = (("Geist", "geist-latin.woff2"), ("Geist Mono", "geist-mono-latin.woff2"))


def _words(value) -> str:
    return str(value).replace("_", " ") if value is not None else ""


def _num(value) -> str:
    if value is None:
        return NOT_RECORDED
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def _duration(seconds) -> str:
    if seconds is None:
        return NOT_RECORDED
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} h {minutes} min"
    return f"{hours} h" if hours else f"{minutes} min"


def _paragraphs(text) -> list[str]:
    return [p.strip() for p in str(text or "").split("\n\n") if p.strip()]


def _money(value, currency) -> str:
    return f"{currency} {value:,.0f}"


@lru_cache(maxsize=1)
def _font_css() -> str:
    faces = []
    for family, filename in _FONTS:
        data = base64.b64encode((TEMPLATES_DIR / "fonts" / filename).read_bytes()).decode("ascii")
        faces.append(
            f"@font-face{{font-family:'{family}';font-style:normal;font-weight:100 900;font-display:block;"
            f"src:url(data:font/woff2;base64,{data}) format('woff2');}}"
        )
    return "\n".join(faces)


@lru_cache(maxsize=1)
def _css() -> str:
    return _font_css() + "\n" + (TEMPLATES_DIR / "report.css").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=("j2", "html"), default=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(words=_words, num=_num, duration=_duration, paragraphs=_paragraphs, money=_money)
    return env


def render_report_html(report: dict) -> str:
    """The full HTML document for a `report_json` (either tier)."""
    return _environment().get_template(TEMPLATE_NAME).render(
        r=report,
        css=_css(),  # static, from this package: the only value emitted unescaped
        tier_label=TIER_LABELS.get(report["tier"], report["tier"]),
        template_version=TEMPLATE_VERSION,
        withheld_text=WITHHELD_SUMMARY_TEXT,
    )
