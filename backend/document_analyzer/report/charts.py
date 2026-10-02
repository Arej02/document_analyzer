"""Deterministic inline SVG charts for the report (design_report.md §8.2, specs_report.md §5.4, §5.6, §6).

Three pure functions, each returning one SVG string (or None when there is nothing to draw):
`heat_map_svg` (subsystem x risk level), `phase_diagram_svg` (phases in order with dependency
arrows) and `estimate_range_bar_svg` (median to cautious range with the sample size).

Rules every chart follows:
- Deterministic: no clocks, randomness or dict-order dependence; numbers are formatted by one
  helper, so equal inputs give byte-equal output (snapshot-tested).
- Readable in print and greyscale: every cell, box and bar carries its number or label as text,
  and risk levels differ by border style (thin / dashed / heavy) as well as shade, never by colour
  alone.
- Safe: all text is XML-escaped (names come from the analysed repository), attributes hold only
  numbers and this module's own constants, and there is no `xmlns` (the SVG is inline in HTML),
  no `<script>`, no `href` and no external reference of any kind.

The output is trusted markup built here, which is why a template may emit it unescaped.
"""

from xml.sax.saxutils import escape

INK = "#16181d"
MUTED = "#5d6573"
RULE = "#c9ced8"
SOFT = "#f4f5f8"
ACCENT = "#3b3f8f"
LEVEL_COLOUR = {"low": "#2f6b3f", "medium": "#8a5a00", "high": "#a3271f"}
# Border style per level, so levels stay distinguishable without colour: (stroke width, dash array).
LEVEL_STROKE = {"low": ("1", ""), "medium": ("1.5", "5 3"), "high": ("2.5", "")}
FONT = "Geist, 'Segoe UI', Helvetica, Arial, sans-serif"
FONT_MONO = "'Geist Mono', ui-monospace, Consolas, monospace"

MAX_HEAT_ROWS = 40
LABEL_CHARS = 26


def _n(value: float) -> str:
    """One number format for every coordinate and label: at most one decimal, no trailing zero."""
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _thousands(value: float) -> str:
    return f"{int(value):,}" if float(value).is_integer() else f"{value:,.1f}"


def _truncate(text, limit: int = LABEL_CHARS) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _text(x, y, content, *, size=12, weight="400", anchor="start", fill=INK, mono=False) -> str:
    family = FONT_MONO if mono else FONT
    return (
        f'<text x="{_n(x)}" y="{_n(y)}" font-family="{family}" font-size="{size}" font-weight="{weight}" '
        f'text-anchor="{anchor}" fill="{fill}">{escape(str(content))}</text>'
    )


def _svg(width: float, height: float, title: str, desc: str, body: list[str], defs: str = "") -> str:
    return (
        f'<svg class="chart" role="img" width="{_n(width)}" height="{_n(height)}" viewBox="0 0 {_n(width)} {_n(height)}" '
        f'preserveAspectRatio="xMidYMid meet">'
        f"<title>{escape(title)}</title><desc>{escape(desc)}</desc>{defs}" + "".join(body) + "</svg>"
    )


# --- risk heat map -----------------------------------------------------------------------------

_HEAT_LEVELS = ("low", "medium", "high")


def heat_map_svg(heat_map: list[dict]) -> str | None:
    """Subsystems (rows) by risk level (columns), the count printed in every cell. Shade deepens
    with the count (scaled to the largest cell); the border shows the level."""
    if not heat_map:
        return None
    rows = heat_map[:MAX_HEAT_ROWS]
    hidden = len(heat_map) - len(rows)
    peak = max((row[level] for row in rows for level in _HEAT_LEVELS), default=0) or 1

    label_w, cell_w, cell_h, head_h, pad = 200, 96, 34, 30, 8
    width = pad * 2 + label_w + cell_w * len(_HEAT_LEVELS)
    height = pad * 2 + head_h + cell_h * len(rows) + (22 if hidden else 0) + 20
    body: list[str] = []
    for col, level in enumerate(_HEAT_LEVELS):
        x = pad + label_w + col * cell_w + cell_w / 2
        body.append(_text(x, pad + 19, level.capitalize(), size=12, weight="600", anchor="middle", fill=MUTED))
    for i, row in enumerate(rows):
        y = pad + head_h + i * cell_h
        body.append(_text(pad, y + cell_h / 2 + 4, _truncate(row["subsystem"]), size=12.5))
        for col, level in enumerate(_HEAT_LEVELS):
            count = row[level]
            x = pad + label_w + col * cell_w
            width_attr, dash = LEVEL_STROKE[level]
            dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
            strength = 0.1 + 0.7 * count / peak if count else 0
            dark = strength > 0.5
            fill = (
                f'<rect x="{_n(x + 2)}" y="{_n(y + 2)}" width="{cell_w - 4}" height="{cell_h - 4}" rx="3" '
                f'fill="{LEVEL_COLOUR[level]}" fill-opacity="{strength:.2f}" stroke="{INK if count else RULE}" '
                f'stroke-width="{width_attr if count else "1"}"{dash_attr if count else ""}>'
                f'<title>{escape(str(row["subsystem"]))}: {count} {level} risk</title></rect>'
            )
            body.append(fill)
            body.append(
                _text(x + cell_w / 2, y + cell_h / 2 + 5, count, size=14, weight="700" if count else "400",
                      anchor="middle", fill="#ffffff" if dark else (INK if count else MUTED))
            )
    foot = pad + head_h + cell_h * len(rows)
    if hidden:
        body.append(_text(pad, foot + 18, f"+{hidden} more subsystem(s) not shown", size=11.5, fill=MUTED))
    body.append(_text(pad, height - pad - 2, "Each cell is a count of code elements. Darker means more; border style marks the level.", size=10.5, fill=MUTED))
    desc = "; ".join(f'{row["subsystem"]}: {row["low"]} low, {row["medium"]} medium, {row["high"]} high' for row in rows)
    return _svg(width, height, "Risk by subsystem", desc, body)


# --- phase order diagram ------------------------------------------------------------------------

_RISK_RANK = {"low": 1, "medium": 2, "high": 3}
BOX_W, BOX_H, GAP_X, GAP_Y, PER_ROW, MARGIN = 132, 78, 52, 64, 5, 16


def _phase_risk(phase: dict) -> str | None:
    levels = [u.get("risk_level") for u in phase.get("units", []) if u.get("risk_level") in _RISK_RANK]
    return max(levels, key=_RISK_RANK.get) if levels else None


def _edge_point(box: tuple[float, float], toward: tuple[float, float]) -> tuple[float, float]:
    """Where the line from a box's centre toward `toward` leaves the box."""
    cx, cy = box[0] + BOX_W / 2, box[1] + BOX_H / 2
    dx, dy = toward[0] - cx, toward[1] - cy
    if dx == 0 and dy == 0:
        return cx, cy
    scale = min((BOX_W / 2) / abs(dx) if dx else float("inf"), (BOX_H / 2) / abs(dy) if dy else float("inf"))
    return cx + dx * scale, cy + dy * scale


def phase_diagram_svg(plan: dict) -> str | None:
    """Phases as boxes in order, wrapped `PER_ROW` to a row, with an arrow from each phase to every
    phase that depends on it. `plan` is the report's `plan` section."""
    phases = sorted(plan.get("phases", []), key=lambda p: p["order"])
    if not phases:
        return None
    origin: dict[int, tuple[float, float]] = {}
    for i, phase in enumerate(phases):
        row, col = divmod(i, PER_ROW)
        origin[phase["order"]] = (MARGIN + col * (BOX_W + GAP_X), MARGIN + row * (BOX_H + GAP_Y))
    rows = (len(phases) - 1) // PER_ROW + 1
    width = MARGIN * 2 + min(len(phases), PER_ROW) * BOX_W + (min(len(phases), PER_ROW) - 1) * GAP_X
    height = MARGIN * 2 + rows * BOX_H + (rows - 1) * GAP_Y + 22

    deps = sorted(
        {(d["to"], d["from"]) for d in plan.get("phase_dependencies", []) if d["to"] in origin and d["from"] in origin and d["to"] != d["from"]}
    )  # (prerequisite, dependent)
    defs = (
        '<defs><marker id="ph-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0L10 5L0 10z" fill="{INK}"/></marker></defs>'
    )
    body: list[str] = []
    pairs = {pair for pair in deps}
    for before, after in deps:
        a, b = origin[before], origin[after]
        ca, cb = (a[0] + BOX_W / 2, a[1] + BOX_H / 2), (b[0] + BOX_W / 2, b[1] + BOX_H / 2)
        start, end = _edge_point(a, cb), _edge_point(b, ca)
        mx, my = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = (dx * dx + dy * dy) ** 0.5 or 1
        bow = 13 if (after, before) in pairs else 8  # mutual dependencies bow apart so both arrows show
        ctrl = (mx - dy / length * bow, my + dx / length * bow)
        body.append(
            f'<path d="M{_n(start[0])} {_n(start[1])}Q{_n(ctrl[0])} {_n(ctrl[1])} {_n(end[0])} {_n(end[1])}" fill="none" '
            f'stroke="{INK}" stroke-width="1.3" marker-end="url(#ph-arrow)"><title>Phase {before} comes before phase {after}</title></path>'
        )
    for phase in phases:
        x, y = origin[phase["order"]]
        risk = _phase_risk(phase)
        stroke_w, dash = LEVEL_STROKE.get(risk, ("1", ""))
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        units = len(phase.get("units", []))
        body.append(
            f'<rect x="{_n(x)}" y="{_n(y)}" width="{BOX_W}" height="{BOX_H}" rx="6" fill="{SOFT}" stroke="{INK}" '
            f'stroke-width="{stroke_w}"{dash_attr}><title>Phase {phase["order"]}: {escape(str(phase.get("label", "")))}</title></rect>'
        )
        body.append(_text(x + 10, y + 20, f'Phase {phase["order"]}', size=12.5, weight="700"))
        body.append(_text(x + 10, y + 38, _truncate(phase.get("label") or "(unlabelled)", 19), size=11.5))
        body.append(_text(x + 10, y + 55, f"{units} unit{'s' if units != 1 else ''}", size=11, fill=MUTED))
        body.append(_text(x + 10, y + 69, f"{risk.capitalize()} risk" if risk else "Risk not recorded", size=11, weight="600" if risk == "high" else "400", fill=MUTED if risk != "high" else INK))
    body.append(_text(MARGIN, height - 8, "An arrow from one phase to another means the second needs the first finished before it starts.", size=10.5, fill=MUTED))
    desc = "; ".join(f"phase {after} depends on phase {before}" for before, after in deps) or "no dependencies between phases"
    return _svg(width, height, "Phase order", f"{len(phases)} phase(s); {desc}", body, defs)


# --- estimate range bar -------------------------------------------------------------------------


def estimate_range_bar_svg(label: str, low: float, high: float, n: int, unit: str = "") -> str:
    """One estimate as a bar from the median (`low`, p50) to the cautious case (`high`, p90), with
    both values printed and the sample size under it. The median is labelled above the track and
    the cautious case below it, so close values never overlap. `low == high` draws a single marker
    and says there is no spread. `unit` is appended to the printed values (e.g. "hours")."""
    width, height, left, right, track_y = 560, 118, 24, 536, 54
    top = high * 1.15 if high > 0 else 1
    scale = (right - left) / top

    def x_of(value: float) -> float:
        return left + value * scale

    def anchor(x: float) -> str:  # labels grow toward the middle, so they stay inside the chart
        return "start" if x < (left + right) / 2 else "end"

    suffix = f" {unit}" if unit else ""
    body = [
        _text(left, 18, label, size=13, weight="600"),
        f'<line x1="{left}" y1="{track_y}" x2="{right}" y2="{track_y}" stroke="{RULE}" stroke-width="2"/>',
        _text(left, track_y + 20, "0", size=10.5, fill=MUTED),
        _text(right, track_y + 20, f"{_thousands(top)}{suffix}", size=10.5, anchor="end", fill=MUTED),
    ]
    if high > low:
        x1, x2 = x_of(low), x_of(high)
        body.append(
            f'<rect x="{_n(x1)}" y="{track_y - 9}" width="{_n(max(x2 - x1, 2))}" height="18" rx="3" fill="{ACCENT}" fill-opacity="0.85" '
            f'stroke="{INK}" stroke-width="1.2"><title>{escape(label)}: {_thousands(low)} to {_thousands(high)}{escape(suffix)}</title></rect>'
        )
        body.append(f'<line x1="{_n(x1)}" y1="{track_y - 14}" x2="{_n(x1)}" y2="{track_y + 14}" stroke="{INK}" stroke-width="2"/>')
        body.append(f'<line x1="{_n(x2)}" y1="{track_y - 14}" x2="{_n(x2)}" y2="{track_y + 14}" stroke="{INK}" stroke-width="2"/>')
        body.append(_text(x1, track_y - 20, f"Median {_thousands(low)}{suffix}", size=11.5, weight="600", anchor=anchor(x1)))
        body.append(_text(x2, track_y + 40, f"Cautious {_thousands(high)}{suffix}", size=11.5, weight="600", anchor=anchor(x2)))
    else:
        x1 = x_of(low)
        body.append(
            f'<path d="M{_n(x1)} {track_y - 11}L{_n(x1 + 11)} {track_y}L{_n(x1)} {track_y + 11}L{_n(x1 - 11)} {track_y}z" fill="{ACCENT}" stroke="{INK}" stroke-width="1.2">'
            f"<title>{escape(label)}: {_thousands(low)}{escape(suffix)}</title></path>"
        )
        body.append(_text(x1, track_y - 20, f"{_thousands(low)}{suffix}, one value (no spread)", size=11.5, weight="600", anchor=anchor(x1)))
    basis = f"Based on {n} comparable past unit(s)" if n else "Default assumptions, no comparable past work (n = 0)"
    body.append(_text(left, height - 10, basis, size=11, fill=MUTED))
    return _svg(width, height, label, f"{label}: median {_thousands(low)}{suffix}, cautious {_thousands(high)}{suffix}; n = {n}", body)
