"""report/render.py + templates/ — design_report.md §8.2: self-contained, escaped, labelled.

Renders the Phase 3 golden `report_json` files (both tiers). The safety checks parse the HTML
rather than grep it, so a hostile entity name shown as *text* is not mistaken for a URL."""

import copy
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from document_analyzer.report import render
from document_analyzer.report.render import TEMPLATE_VERSION, TEMPLATES_DIR, render_report_html

FIXTURE = Path(__file__).parent / "fixtures" / "report_project"
FORBIDDEN_TAGS = {"script", "iframe", "frame", "object", "embed", "link", "form", "base", "meta-refresh", "svg-script"}
URL_ATTRS = {"src", "href", "srcset", "action", "formaction", "poster", "data", "xlink:href", "background", "cite"}
EXTERNAL_IN_CSS = re.compile(r"@import|url\(\s*(?!['\"]?data:)|//[a-z0-9.-]+\.[a-z]{2,}", re.I)


class _Audit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[str] = []
        self.attrs: list[tuple[str, str, str]] = []  # (tag, name, value)
        self.style_blocks: list[str] = []
        self.text: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend((tag, name, value or "") for name, value in attrs)
        self._in_style = tag == "style"

    def handle_endtag(self, tag):
        self._in_style = False

    def handle_data(self, data):
        (self.style_blocks if self._in_style else self.text).append(data)


def audit(html: str) -> _Audit:
    parser = _Audit()
    parser.feed(html)
    parser.close()
    return parser


def assert_self_contained(html: str) -> _Audit:
    parsed = audit(html)
    assert not FORBIDDEN_TAGS & set(parsed.tags), f"forbidden tag in {sorted(FORBIDDEN_TAGS & set(parsed.tags))}"
    assert "<script" not in html.lower()
    handlers = [(t, n) for t, n, _ in parsed.attrs if n.lower().startswith("on")]
    assert not handlers, f"event handler attribute(s): {handlers}"
    for tag, name, value in parsed.attrs:
        if name.lower() in URL_ATTRS:
            assert value.startswith("#") or value.startswith("data:"), f"<{tag} {name}={value!r}> is not inline"
        if name.lower() == "style":
            assert not EXTERNAL_IN_CSS.search(value), f"external reference in style attribute: {value!r}"
    css = "\n".join(parsed.style_blocks)
    assert css, "the stylesheet must be inlined"
    assert not EXTERNAL_IN_CSS.search(css), "external reference in the inline stylesheet"
    assert all(u.lower().startswith("data:") for u in re.findall(r"url\(([^)]+)\)", css)), "non-data url() in CSS"
    return parsed


def _load(name: str) -> dict:
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def xray():
    return _load("xray.golden.json")


@pytest.fixture(scope="module")
def full():
    return _load("full.golden.json")


def _text(html: str) -> str:
    return " ".join(" ".join(audit(html).text).split())


# --- self-contained --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["xray.golden.json", "full.golden.json"])
def test_goldens_render_self_contained(name):
    html = render_report_html(_load(name))
    assert html.startswith("<!doctype html>")
    assert_self_contained(html)
    assert html.count("@font-face") == 2 and html.count("data:font/woff2;base64,") == 2


def test_no_external_url_anywhere_in_the_bytes_even_as_text(full):
    # The goldens hold no URLs, so any http(s):// in the output would come from the template itself.
    html = render_report_html(full)
    assert not re.search(r"https?://", html)


def test_template_files_have_no_scripts_or_external_urls_and_one_safe_filter():
    template = (TEMPLATES_DIR / "report.html.j2").read_text(encoding="utf-8")
    assert "<script" not in template.lower() and not re.search(r"https?://|\bon\w+\s*=", template)
    assert template.count("|safe") == 1  # only the static stylesheet is emitted unescaped
    css = (TEMPLATES_DIR / "report.css").read_text(encoding="utf-8")
    assert not EXTERNAL_IN_CSS.search(css)


def test_render_is_deterministic_and_template_version_pinned(full):
    first, second = render_report_html(full), render_report_html(copy.deepcopy(full))
    assert first == second
    assert f'content="assessment-report-template-{TEMPLATE_VERSION}"' in first
    assert TEMPLATE_VERSION == "2"


# --- hostile repository text ---------------------------------------------------------------------


HOSTILE = '<script>alert(1)</script><img src=x onerror=alert(2)>" onmouseover="alert(3)'


def test_hostile_names_are_escaped_everywhere_they_can_appear(full):
    report = copy.deepcopy(full)
    report["cover"]["project_name"] = HOSTILE
    report["architecture"]["subsystems"][0].update(name=HOSTILE, summary=HOSTILE, summary_grounding="unverified", modules=[HOSTILE])
    report["architecture"]["hubs"] = [{"name": HOSTILE, "file_path": HOSTILE, "degree": 3}]
    report["risk"]["heat_map"][0]["subsystem"] = HOSTILE
    report["risk"]["hotspots"] = [{"subsystem": HOSTILE, "more": 1, "entities": [
        {"name": HOSTILE, "file_path": HOSTILE, "risk_level": "high", "signal": HOSTILE, "explanation": HOSTILE}]}]
    report["glance"]["languages"] = [{"name": HOSTILE, "files": 1}]
    report["glance"]["files_filtered"] = [{"reason": HOSTILE, "count": 1}]
    report["security"]["secrets_by_reason"] = [{"reason": HOSTILE, "count": 1, "locations": [{"file_path": HOSTILE, "field_path": HOSTILE}]}]
    report["plan"]["phases"][0]["label"] = HOSTILE
    report["plan"]["phases"][0]["units"][0]["label"] = HOSTILE
    report["limitations"] = [{"code": "x", "text": HOSTILE}]
    report["executive_summary"]["text"] = HOSTILE
    report["next_steps"] = {"heading": HOSTILE, "body": HOSTILE, "contact": HOSTILE}

    html = render_report_html(report)
    parsed = assert_self_contained(html)
    assert "<script>alert" not in html and "<img src=x" not in html
    assert html.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 10  # shown as text in every place
    assert parsed.tags.count("script") == 0 and parsed.tags.count("img") == 0


def test_urls_in_repository_text_stay_inert_text(full):
    report = copy.deepcopy(full)
    report["architecture"]["subsystems"][0]["summary"] = "See http://evil.example/x and //evil.example/y"
    report["architecture"]["subsystems"][0]["summary_grounding"] = "verified"
    html = render_report_html(report)
    parsed = assert_self_contained(html)  # no src/href carries it
    assert "http://evil.example/x" in " ".join(parsed.text)


# --- content -------------------------------------------------------------------------------------


def test_cover_has_tier_project_snapshot_and_plan(full, xray):
    text = _text(render_report_html(full))
    assert "Full Assessment" in text and full["cover"]["project_name"] in text and full["cover"]["snapshot_date"] in text
    assert f"Version {full['plan']['version']}, {full['plan']['status'].replace('_', ' ')}" in text
    xtext = _text(render_report_html(xray))
    assert "Codebase X-ray" in xtext and "Migration plan" not in xtext


def test_xray_has_no_plan_or_estimate_sections(xray):
    parsed = audit(render_report_html(xray))
    ids = {v for _, n, v in parsed.attrs if n == "id"}
    assert {"summary", "glance", "architecture", "risk", "security", "limitations", "next"} <= ids
    assert not {"plan", "estimate"} & ids


def test_full_has_every_section_in_order(full):
    ids = [v for _, n, v in audit(render_report_html(full)).attrs if n == "id" and v in
           {"summary", "glance", "architecture", "risk", "security", "plan", "estimate", "limitations", "next"}]
    assert ids == ["summary", "glance", "architecture", "risk", "security", "plan", "estimate", "limitations", "next"]


def test_draft_watermark_only_for_an_unapproved_plan(full):
    approved = copy.deepcopy(full)
    approved["cover"]["watermark"] = None
    assert 'class="watermark"' not in render_report_html(approved)
    draft = copy.deepcopy(full)
    draft["cover"]["watermark"] = "draft_plan"
    html = render_report_html(draft)
    assert 'class="watermark"' in html and "has not been approved" in _text(html)


@pytest.mark.parametrize("status,shown", [("unverified", True), ("verified", False)])
def test_executive_summary_unverified_label(full, status, shown):
    report = copy.deepcopy(full)
    report["executive_summary"] = {"text": "First para.\n\nSecond para.", "status": status, "note": "Could not be checked."}
    html = render_report_html(report)
    summary = html.split('id="summary"')[1].split("</section>")[0]
    assert ("Unverified" in summary) is shown
    assert summary.count("<p>") >= 2 and "Second para." in summary
    assert ("Could not be checked." in summary) is shown


def test_withheld_summary_shows_the_fixed_sentence_only(full):
    report = copy.deepcopy(full)
    report["executive_summary"] = {"text": "", "status": "withheld", "note": "x"}
    summary = render_report_html(report).split('id="summary"')[1].split("</section>")[0]
    assert render.WITHHELD_SUMMARY_TEXT in summary and "Unverified" not in summary


def test_unverified_labels_on_subsystem_summaries_and_plan_units(full):
    report = copy.deepcopy(full)
    report["architecture"]["subsystems"][0].update(summary="Does things.", summary_grounding="unverified")
    report["architecture"]["subsystems"][1].update(summary="Fine.", summary_grounding="verified")
    for phase in report["plan"]["phases"]:  # the golden already has one unverified unit: start from none
        for unit in phase["units"]:
            unit["grounding_status"] = "verified"
    report["plan"]["phases"][0]["units"][0]["grounding_status"] = "unverified"
    html = render_report_html(report)
    arch = html.split('id="architecture"')[1].split("</section>")[0]
    assert arch.count("Unverified") == 1
    plan = html.split('id="plan"')[1].split("</section>")[0]
    assert plan.count("Unverified") == 1


def test_estimate_has_ranges_footnotes_and_the_heuristic_label(full):
    report = copy.deepcopy(full)
    report["estimate"]["manual_effort"] = {
        "label": "Heuristic", "developer_days": {"p50": 54.9, "p90": 82.4}, "limitations": ["No coefficient for entity type(s): hook."],
        "coefficients": {"per_entity": {"function": 0.1, "class": 0.3}, "risk_multiplier": {"low": 1.0, "high": 2.5},
                         "per_facade_cycle": 2.0, "per_oversized_unit": 1.0, "p90_factor": 1.5, "applied_risk_multiplier": 1.11},
    }
    report["estimate"]["quote"] = {"currency": "USD", "method": "markup", "low": 12000, "high": 18000}
    html = render_report_html(report)
    est = html.split('id="estimate"')[1].split("</section>")[0]
    text = _text(est)
    assert "Heuristic" in est and "54.9 to 82.4 developer-days" in text
    assert "USD 12,000 to USD 18,000" in text
    assert "function 0.1" in text and "applied 1.1" in text and "No coefficient for entity type(s): hook." in text
    for n in range(1, 6):  # every numbered reference has its footnote
        assert f'href="#fn-' in est and est.count("<li id=\"fn-") >= 5
    refs = set(re.findall(r'href="#(fn-[a-z]+)"', est))
    assert refs <= set(re.findall(r'<li id="(fn-[a-z]+)"', est))


def test_estimate_without_manual_effort_or_price_hides_them(full):
    est = render_report_html(full).split('id="estimate"')[1].split("</section>")[0]
    assert "Heuristic" not in est and "Price range" not in est and 'id="fn-manual"' not in est


def test_estimate_basis_wording(full):
    report = copy.deepcopy(full)
    report["estimate"].update(basis="defaults", n=0, units_from_history=0)
    assert "no comparable past work" in _text(render_report_html(report))


def test_missing_data_reads_as_not_recorded_not_blank(xray):
    report = copy.deepcopy(xray)
    report["glance"].update(loc=None, dependencies=None, languages=[], frameworks=[], coverage=None)
    text = _text(render_report_html(report))
    assert text.count("Not recorded for this run") >= 3 and "Parse coverage is not recorded" in text
    risk = copy.deepcopy(xray)
    risk["risk"]["hotspots"] = [{"subsystem": "app", "more": 0, "entities": [
        {"name": "f", "file_path": "a.php", "risk_level": "high", "signal": None, "explanation": "Does x."}]}]
    assert "not recorded for this run" in _text(render_report_html(risk))


def test_coverage_by_language_accepts_the_stored_shape(xray):
    # extraction_coverage stores {language: {status: count}}; the Phase 3 fixture used {language: count}.
    report = copy.deepcopy(xray)
    report["glance"]["coverage"]["by_language"] = {"javascript/typescript": {"extracted": 8, "failed": 1}}
    assert "javascript/typescript (8 extracted, 1 failed)" in _text(render_report_html(report))


def test_limitations_and_next_steps_render(full):
    text = _text(render_report_html(full))
    for item in full["limitations"]:
        assert item["text"] in text
    assert full["next_steps"]["heading"] in text and full["next_steps"]["contact"] in text


def test_every_pill_and_cell_carries_text_not_colour_alone(full):
    html = render_report_html(full)
    for pill in re.findall(r'<span class="pill pill-(?:low|medium|high)">([^<]*)</span>', html):
        assert pill.endswith(" risk")
    heat = html.split('class="heat"')[1].split("</table>")[0]
    assert re.search(r"<td class=\"n\">\d+</td>", heat)  # counts printed, not shaded cells


def test_worker_uses_the_pinned_template_version():
    from document_analyzer.report.worker import run

    assert run.TEMPLATE_VERSION == TEMPLATE_VERSION
