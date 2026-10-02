"""report/charts.py — deterministic, safe, greyscale-readable SVG (tasks_report.md Phase 5).

Goldens live in tests/fixtures/report_charts/. To regenerate after an intended change:

    UPDATE_GOLDEN=1 uv run python -m pytest tests/test_report_charts.py
"""

import copy
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from document_analyzer.report import charts
from document_analyzer.report.charts import estimate_range_bar_svg, heat_map_svg, phase_diagram_svg

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "report_charts"
FULL = json.loads((FIXTURES / "report_project" / "full.golden.json").read_text(encoding="utf-8"))
HOSTILE = '<script>alert(1)</script>"&\'><img src=x onerror=alert(2)>'


def _assert_golden(name: str, actual: str) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(actual + "\n", encoding="utf-8")
    assert path.read_text(encoding="utf-8") == actual + "\n", f"{name} differs; rerun with UPDATE_GOLDEN=1 if intended"


def _root(svg: str) -> ET.Element:
    return ET.fromstring(svg)  # raises on malformed XML


def _texts(svg: str) -> list[str]:
    return ["".join(t.itertext()) for t in _root(svg).iter("text")]


def assert_safe(svg: str) -> None:
    root = _root(svg)
    assert root.tag == "svg"
    banned = {"script", "foreignObject", "a", "image", "use", "style", "iframe", "link"}
    assert not banned & {el.tag for el in root.iter()}
    for el in root.iter():
        for name, value in el.attrib.items():
            assert not name.lower().startswith("on"), (el.tag, name)
            assert name.lower() not in {"href", "xlink:href", "src"}, (el.tag, name)
            assert "http" not in value.lower() and "javascript:" not in value.lower(), (el.tag, name, value)
    assert "xmlns" not in svg and "://" not in svg


# --- heat map ---------------------------------------------------------------------------------------


def test_heat_map_matches_golden():
    _assert_golden("heat_map.svg", heat_map_svg(FULL["risk"]["heat_map"]))


def test_heat_map_prints_every_count_as_text():
    rows = FULL["risk"]["heat_map"]
    svg = heat_map_svg(rows)
    texts = _texts(svg)
    for row in rows:
        assert row["subsystem"] in texts
    counts = [t for t in texts if t.isdigit()]
    assert counts == [str(row[level]) for row in rows for level in ("low", "medium", "high")]  # zeros too
    for level in ("Low", "Medium", "High"):
        assert level in texts


def test_heat_map_levels_differ_by_border_not_only_colour():
    svg = heat_map_svg([{"subsystem": "a", "low": 1, "medium": 1, "high": 1}])
    rects = [r for r in _root(svg).iter("rect")]
    assert [r.get("stroke-width") for r in rects] == ["1", "1.5", "2.5"]
    assert [r.get("stroke-dasharray") for r in rects] == [None, "5 3", None]


def test_heat_map_shade_scales_with_count_and_zero_has_no_fill():
    svg = heat_map_svg([{"subsystem": "a", "low": 0, "medium": 2, "high": 10}])
    opacities = [float(r.get("fill-opacity")) for r in _root(svg).iter("rect")]
    assert opacities[0] == 0 and 0 < opacities[1] < opacities[2] <= 0.8


def test_heat_map_caps_rows_and_says_so():
    rows = [{"subsystem": f"s{i:02d}", "low": 1, "medium": 0, "high": 0} for i in range(45)]
    texts = _texts(heat_map_svg(rows))
    assert "s39" in texts and "s40" not in texts
    assert "+5 more subsystem(s) not shown" in texts


def test_heat_map_truncates_long_names_but_keeps_the_full_one_in_the_title():
    name = "a-very-long-subsystem-name-that-would-overflow-the-label-column"
    svg = heat_map_svg([{"subsystem": name, "low": 1, "medium": 0, "high": 0}])
    assert any(t.endswith("…") and len(t) <= charts.LABEL_CHARS for t in _texts(svg))
    assert name in "".join(t.text or "" for t in _root(svg).iter("title"))


def test_heat_map_empty_is_none():
    assert heat_map_svg([]) is None


# --- phase diagram -------------------------------------------------------------------------------------


def test_phase_diagram_matches_golden():
    _assert_golden("phase_diagram.svg", phase_diagram_svg(FULL["plan"]))


def _plan(n, deps=()):
    return {
        "phases": [{"order": i, "label": f"P{i}", "units": [{"risk_level": "low"}]} for i in range(1, n + 1)],
        "phase_dependencies": [{"from": a, "to": b} for a, b in deps],
    }


def test_phase_diagram_labels_every_phase_with_units_and_risk():
    texts = _texts(phase_diagram_svg(FULL["plan"]))
    assert {"Phase 1", "Phase 2", "Foundation", "Shop", "2 units"} <= set(texts)
    assert "High risk" in texts  # phase 1 holds a high-risk unit; phase risk is the highest unit risk
    assert "1 unit" in texts or "2 units" in texts


def test_phase_diagram_draws_one_arrow_per_distinct_dependency_with_a_text_alternative():
    svg = phase_diagram_svg(_plan(3, [(2, 1), (3, 2), (3, 2), (3, 3), (3, 9)]))  # duplicate, self and unknown ignored
    root = _root(svg)
    arrows = [p for p in root.iter("path") if p.get("marker-end")]
    assert len(arrows) == 2
    assert "phase 2 depends on phase 1" in "".join(root.find("desc").itertext())
    assert any("Phase 1 comes before phase 2" in "".join(t.itertext()) for t in root.iter("title"))


def test_phase_diagram_separates_mutual_dependencies():
    svg = phase_diagram_svg(_plan(2, [(2, 1), (1, 2)]))
    paths = [p.get("d") for p in _root(svg).iter("path") if p.get("marker-end")]
    assert len(paths) == 2 and paths[0] != paths[1]


def test_phase_diagram_wraps_rows():
    root = _root(phase_diagram_svg(_plan(12)))
    ys = {float(r.get("y")) for r in root.iter("rect")}
    assert len(ys) == 3  # 12 phases at 5 per row
    assert float(root.get("width")) == 2 * charts.MARGIN + 5 * charts.BOX_W + 4 * charts.GAP_X


def test_phase_diagram_orders_phases_regardless_of_input_order():
    plan = _plan(3)
    shuffled = copy.deepcopy(plan)
    shuffled["phases"].reverse()
    assert phase_diagram_svg(plan) == phase_diagram_svg(shuffled)


def test_phase_diagram_empty_is_none():
    assert phase_diagram_svg({"phases": []}) is None
    assert phase_diagram_svg({}) is None


def test_phase_without_risk_says_so():
    plan = {"phases": [{"order": 1, "label": None, "units": []}], "phase_dependencies": []}
    texts = _texts(phase_diagram_svg(plan))
    assert "Risk not recorded" in texts and "(unlabelled)" in texts and "0 units" in texts


# --- estimate bar ---------------------------------------------------------------------------------------


def test_estimate_bar_matches_golden():
    _assert_golden("estimate_range.svg", estimate_range_bar_svg("Rework effort", 2.0, 8.0, 6, "hours"))
    _assert_golden("estimate_single.svg", estimate_range_bar_svg("Generation time", 7020, 7020, 0, "s"))


def test_estimate_bar_prints_both_ends_and_the_sample_size():
    texts = _texts(estimate_range_bar_svg("Rework effort", 2.0, 8.0, 6, "hours"))
    assert "Median 2 hours" in texts and "Cautious 8 hours" in texts and "Rework effort" in texts
    assert "Based on 6 comparable past unit(s)" in texts


def test_estimate_bar_cold_start_and_single_value_are_stated():
    texts = _texts(estimate_range_bar_svg("Generation time", 7020, 7020, 0, "s"))
    assert "Default assumptions, no comparable past work (n = 0)" in texts
    assert "7,020 s, one value (no spread)" in texts


def test_estimate_bar_labels_never_overlap_even_for_close_values():
    # Regression (rendered check): median 28 and cautious 40 printed on top of each other.
    root = _root(estimate_range_bar_svg("Rework effort", 28, 40, 0, "hours"))
    labels = {"".join(t.itertext()): t for t in root.iter("text")}
    median, cautious = labels["Median 28 hours"], labels["Cautious 40 hours"]
    assert float(median.get("y")) < 54 < float(cautious.get("y"))  # above and below the track
    for text in (median, cautious):
        x = float(text.get("x"))
        width_estimate = len("".join(text.itertext())) * 7
        left = x if text.get("text-anchor") == "start" else x - width_estimate
        assert 0 <= left and left + width_estimate <= float(root.get("width"))


def test_estimate_bar_geometry_is_inside_the_track_and_ordered():
    root = _root(estimate_range_bar_svg("x", 10, 40, 5))
    bar = next(r for r in root.iter("rect"))
    x, w = float(bar.get("x")), float(bar.get("width"))
    assert 24 <= x < x + w <= 536 and w > 0


def test_estimate_bar_handles_zero():
    svg = estimate_range_bar_svg("Failed units", 0, 0, 0)
    assert "0, one value (no spread)" in _texts(svg)
    assert_safe(svg)


# --- safety and determinism (all charts) ----------------------------------------------------------------


def _all_charts():
    return [
        heat_map_svg(FULL["risk"]["heat_map"]),
        phase_diagram_svg(FULL["plan"]),
        estimate_range_bar_svg("Rework effort", 2.0, 8.0, 6, "hours"),
        estimate_range_bar_svg("Single", 5, 5, 0),
    ]


@pytest.mark.parametrize("svg", _all_charts())
def test_every_chart_is_safe_inline_svg(svg):
    assert_safe(svg)
    assert svg.startswith("<svg ") and svg.endswith("</svg>") and 'role="img"' in svg
    assert _root(svg).find("title") is not None and _root(svg).find("desc") is not None


def test_charts_are_deterministic():
    assert _all_charts() == _all_charts()
    assert heat_map_svg(FULL["risk"]["heat_map"]) == heat_map_svg(copy.deepcopy(FULL["risk"]["heat_map"]))


def test_hostile_text_is_escaped_in_every_chart():
    heat = heat_map_svg([{"subsystem": HOSTILE, "low": 1, "medium": 1, "high": 1}])
    plan = {"phases": [{"order": 1, "label": HOSTILE, "units": []}, {"order": 2, "label": HOSTILE, "units": []}],
            "phase_dependencies": [{"from": 2, "to": 1}]}
    bar = estimate_range_bar_svg(HOSTILE, 1, 2, 3, HOSTILE)
    for svg in (heat, phase_diagram_svg(plan), bar):
        assert_safe(svg)
        assert "<script" not in svg and "<img" not in svg
        assert "&lt;script&gt;" in svg or "&lt;" in svg
        assert HOSTILE in "".join("".join(el.itertext()) for el in _root(svg).iter())  # present only as text


def test_charts_use_no_colour_only_encoding_in_text():
    # Every level name appears as text, so a greyscale print still names the column.
    texts = _texts(heat_map_svg(FULL["risk"]["heat_map"]))
    assert {"Low", "Medium", "High"} <= set(texts)
    assert not re.search(r"\bred\b|\bgreen\b", " ".join(texts), re.I)
