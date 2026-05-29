"""
Tests for altitude-arrow extraction + matching.

Two layers:

1. **Pure unit tests** (no PDF needed) — colour gate, altitude plausibility,
   bearing extraction on synthetic arrow paths, segment-cross-track math,
   matcher acceptance/rejection.

2. **Integration smoke tests** against the real chart PDFs sitting at the
   project root. They run only when both PDFs exist (so the suite stays
   green on a stripped checkout) and assert *coarse* invariants — we expect
   the harvest to have hundreds of arrows on each sheet, with values
   dominated by the canonical CVFR altitudes (1000, 1200, 1500, 2000, ...).
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from cvfr_routemaster.altitude_arrows import (
    AltitudeArrow,
    GeoAltitudeArrow,
    MATCH_BEND_RESCUE_BISECTOR_TOL_DEG,
    MATCH_BEND_RESCUE_MAX_LEG_DIST_NM,
    MATCH_BEND_RESCUE_MIN_BEND_DEG,
    MATCH_FWD_DIFF_SCORE_WEIGHT,
    MATCH_PARALLEL_TOL_DEG,
    MATCH_RADIUS_NM,
    MATCH_RADIUS_NM_INTERMEDIATE,
    MATCH_STACK_BEARING_TOL_DEG,
    MATCH_STACK_RADIUS_NM,
    MATCH_MAX_ENDPOINT_OVERSHOOT_NM,
    MATCH_PARALLEL_TOL_DEG_PAST_ENDPOINT,
    MATCH_WIDE_CORRIDOR_FWD_DIFF_DEG,
    MATCH_WIDE_CORRIDOR_RADIUS_NM,
    _ArrowSegFit,
    _CLASS_BIDIRECTIONAL,
    _CLASS_PARALLEL_LEFT,
    _CLASS_PARALLEL_RIGHT,
    _FORBIDDEN_ARROW_PATH_KINDS,
    _MAX_ARROW_PATH_ITEMS,
    _ONSEG_TIER_ON_SEGMENT,
    _ONSEG_TIER_PAST_ENDPOINT,
    _arrow_bearing_pdf_deg,
    _arrow_bidirectional_axis_bearing_pdf,
    _arrow_side_of_segment,
    _arrow_tail_anchor_pdf,
    _axis_diff_deg,
    _bisector_bearing_deg,
    _circular_diff_deg,
    _distance_and_overshoot_to_segment_nm,
    _fit_key,
    _great_circle_distance_to_segment_nm,
    _is_arrow_shape,
    _is_plausible_altitude,
    _is_yellowish_fill,
    _onseg_tier,
    _pdf_pt_to_pixmap_uv,
    extract_altitude_arrows,
    match_altitudes_for_route,
    match_altitudes_for_segment,
)
from cvfr_routemaster.map_crop import CropMeta
from cvfr_routemaster.route import Route, RouteSegment, RoutePoint
from cvfr_routemaster.waypoint_types import WaypointRecord


# ---------------------------------------------------------------------------
# Yellow filter
# ---------------------------------------------------------------------------


def test_yellow_filter_accepts_canonical_arrow_fill():
    # The colour PyMuPDF reports for the actual chart arrow.
    assert _is_yellowish_fill((1.0, 0.944, 0.333))


def test_yellow_filter_rejects_white_and_grey():
    assert not _is_yellowish_fill((1.0, 1.0, 1.0))
    assert not _is_yellowish_fill((0.5, 0.5, 0.5))


def test_yellow_filter_rejects_magenta():
    # The chart's route-code magenta — must NOT be confused with arrow yellow.
    assert not _is_yellowish_fill((0.706, 0.122, 0.514))


def test_yellow_filter_rejects_none_for_strokes_only():
    assert not _is_yellowish_fill(None)


# ---------------------------------------------------------------------------
# Altitude plausibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("800", 800),
        ("1500", 1500),
        ("2000", 2000),
        ("9500", 9500),
        ("300", 300),
    ],
)
def test_plausible_altitude_accepts_canonical_values(text: str, expected: int):
    assert _is_plausible_altitude(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "  ",
        "0",
        "100",        # too low (CVFR floor is 300 ft)
        "200",
        "9600",       # over the cap
        "10000",
        "350",        # not a multiple of 100
        "1138",       # spot height — sub-multiple of 100
        "2431",       # obstacle elevation
        "3346",
        "1.5",        # decimal
        "2,000",      # separator
        "-500",       # signed
        "12A",
    ],
)
def test_plausible_altitude_rejects_non_altitudes(text: str):
    assert _is_plausible_altitude(text) is None


# ---------------------------------------------------------------------------
# Arrow shape gate
# ---------------------------------------------------------------------------


def _rect(x0: float, y0: float, x1: float, y1: float):
    """Lightweight stand-in for ``fitz.Rect`` — only ``width``/``height`` and
    the four scalars are needed by ``_is_arrow_shape``."""
    import fitz

    return fitz.Rect(x0, y0, x1, y1)


def test_arrow_shape_accepts_typical_yellow_arrow_size():
    # ~10×10 pt — the dominant altitude-arrow size on the north chart.
    assert _is_arrow_shape(_rect(100, 100, 110, 110))


def test_arrow_shape_rejects_too_small():
    assert not _is_arrow_shape(_rect(100, 100, 102, 102))


def test_arrow_shape_rejects_too_large():
    # Bigger than the 28-pt cap — picks up TMA shading and obstacle blocks.
    assert not _is_arrow_shape(_rect(100, 100, 230, 200))


def test_arrow_shape_rejects_extreme_aspect_ratio():
    # 50×6 → AR ~8, way past the 4× cap; this is a scale-rule patch.
    assert not _is_arrow_shape(_rect(100, 100, 150, 106))


# ---------------------------------------------------------------------------
# Path-complexity gate (settlement-blob filter)
#
# These tests pin the contract of ``_MAX_ARROW_PATH_ITEMS``: real CVFR
# altitude arrows are simple polygons (typically 5–7 path items — a
# notched chevron). Settlements / lakes / forests on the same chart are
# also drawn in the arrow-yellow palette and routinely *clear* the size
# and aspect-ratio gates, but their boundaries have an order of
# magnitude more vector-path items (Umm El Fahm has 43; a city like
# Tel Aviv runs into the hundreds). Without a path-complexity gate
# such a blob's bbox can swallow a nearby altitude digit span, and the
# extractor would then emit a phantom arrow whose bearing was derived
# from the blob's largest concavity (NE for Umm El Fahm; the real
# 3000 ft arrow at the same spot points SW). That phantom is what
# poisoned the EIRON.1→ZMGID leg of the LLHZ→LLIB route and motivated
# the gate.
# ---------------------------------------------------------------------------


def test_max_arrow_path_items_is_in_safe_band():
    """The threshold should be well above any real arrow's path-item count
    (≤ ~7 in practice) but well below the simplest settlement blob
    (Umm El Fahm = 43). Anything in [10, 25] is defensible; outside
    that range the gate is either too tight (kills legitimate arrows)
    or too loose (re-admits the phantoms it was added to suppress).
    """
    assert isinstance(_MAX_ARROW_PATH_ITEMS, int)
    assert 10 <= _MAX_ARROW_PATH_ITEMS <= 25, (
        f"_MAX_ARROW_PATH_ITEMS={_MAX_ARROW_PATH_ITEMS} is outside the safe "
        "band of [10, 25]; tightening below 10 risks rejecting a notched "
        "bidirectional arrow, loosening above 25 risks re-admitting "
        "settlement-blob phantoms (Umm El Fahm = 43 path items)."
    )


def test_max_arrow_path_items_rejects_known_settlement_blob_size():
    """Concrete values from the EIRON.1 → ZMGID investigation:

    * The legitimate SW-pointing 3000 ft arrow at @(32.553, 35.160) had
      6 path items — it must pass the gate.
    * The Umm El Fahm settlement blob (a yellow polygon whose bbox
      swallowed the same "3000" digit span) had 43 path items — it
      must be rejected so it doesn't emit a phantom arrow with a
      blob-derived NE bearing.

    Pin both endpoints so a future tweak that inverts the comparator
    or moves the threshold past one of these landmarks fails loudly.
    """
    real_arrow_items = 6
    umm_el_fahm_items = 43
    assert real_arrow_items <= _MAX_ARROW_PATH_ITEMS, (
        f"the 6-item SW 3000 arrow that legitimately labels the "
        f"ZMGID→EIRON.1 leg must clear the gate "
        f"(threshold={_MAX_ARROW_PATH_ITEMS})"
    )
    assert umm_el_fahm_items > _MAX_ARROW_PATH_ITEMS, (
        f"the 43-item Umm El Fahm settlement blob must be rejected "
        f"by the gate (threshold={_MAX_ARROW_PATH_ITEMS}); otherwise "
        f"the EIRON.1→ZMGID phantom 3000 returns and breaks the "
        f"LLHZ→LLIB regression"
    )


# ---------------------------------------------------------------------------
# Curve-segment gate (holding-pattern racetrack filter)
#
# These tests pin the contract of ``_FORBIDDEN_ARROW_PATH_KINDS``: real
# CVFR altitude arrows are 100% straight-line polygons. Holding-pattern
# symbols share the arrow-yellow palette and clear every other gate
# (size, aspect ratio, path-item count, altitude-text containment), but
# their racetrack shape is rendered with cubic-Bézier semicircular ends
# — the canonical PyMuPDF item-kind tag is ``'c'`` (cubic) or ``'qu'``
# (quadratic). A single such item is enough to reject; the EIRON-area
# phantom that motivated this filter has a clean ``{'c': 4, 'l': 2}``
# signature (4 cubics — 2 per semicircle — plus the 2 straight sides).
# ---------------------------------------------------------------------------


def test_forbidden_arrow_path_kinds_includes_cubic_and_quadratic_beziers():
    """The set must include both ``'c'`` and ``'qu'`` — these are PyMuPDF's
    item-kind tags for cubic and quadratic Bézier curves respectively.
    Either one is sufficient signal that the shape is not an arrow:
    arrows are pure polygons, anything with a curved side is either a
    holding pattern or some other rounded chart symbol that we don't
    want to mistake for an altitude annotation.
    """
    assert "c" in _FORBIDDEN_ARROW_PATH_KINDS
    assert "qu" in _FORBIDDEN_ARROW_PATH_KINDS
    assert "l" not in _FORBIDDEN_ARROW_PATH_KINDS, (
        "real arrows are made of 'l' (line) items — listing 'l' as "
        "forbidden would reject every legitimate arrow on the chart"
    )
    assert "m" not in _FORBIDDEN_ARROW_PATH_KINDS, (
        "every path starts with an implicit 'm' (moveto); listing it as "
        "forbidden would reject every yellow drawing"
    )
    assert "re" not in _FORBIDDEN_ARROW_PATH_KINDS, (
        "axis-aligned 're' (rect) items appear in some simple arrow "
        "renderings; rejecting them would lose legitimate arrows"
    )


def test_forbidden_arrow_path_kinds_rejects_canonical_holding_pattern_signature():
    """The EIRON-area holding-pattern racetrack at @(32.500, 35.040) has
    item kinds ``{'c': 4, 'l': 2}``: four cubic-Bézier semicircle
    halves (two per end of the racetrack) and two straight sides.

    The gate is implemented as ``any(it[0] in FORBIDDEN for it in items)``
    so even a single ``'c'`` item is enough to trip rejection. Real
    arrows have zero curve items — this asymmetry is what makes the
    filter both safe and effective.
    """
    holding_pattern_kinds = ["c", "c", "l", "c", "c", "l"]
    real_arrow_kinds = ["l", "l", "l", "l", "l", "l"]
    assert any(k in _FORBIDDEN_ARROW_PATH_KINDS for k in holding_pattern_kinds), (
        "the canonical holding-pattern racetrack signature {'c': 4, 'l': 2} "
        "must be rejected by the curve-segment gate; otherwise the "
        "EIRON.1→EIRON LLIB→LLHZ leg phantom returns"
    )
    assert not any(k in _FORBIDDEN_ARROW_PATH_KINDS for k in real_arrow_kinds), (
        "a pure-line polygon (real arrow signature) must NOT trip the "
        "curve-segment gate"
    )


# ---------------------------------------------------------------------------
# Direction from synthetic path geometry
#
# The chart's altitude arrows are highway-sign-shaped: a rectangular body
# with a triangular tip on one short edge and a triangular concave notch
# cut into the opposite (tail) edge. The bearing extractor finds the most
# prominent concave vertex (the notch apex) and returns the heading
# pointing AWAY from it.
# ---------------------------------------------------------------------------


def _line_chain(points: list[tuple[float, float]]):
    """Synthesise a list of ('l', start, end) items mirroring what PyMuPDF
    returns. ``end`` is the only point ``_path_vertices`` reads off line
    items — we still pass start for shape parity."""
    import fitz

    out = []
    for i in range(1, len(points)):
        s = fitz.Point(*points[i - 1])
        e = fitz.Point(*points[i])
        out.append(("l", s, e))
    return out


def _arrow_pointing_north(centre_x: float = 100.0, centre_y: float = 100.0):
    """Highway-sign arrow with tip at smaller PDF y (chart north).

    Polygon, walked CCW in PDF Y-down (which is CW in math Y-up):
    top-left body corner → tip → top-right body corner → bottom-right body
    corner → notch apex (inward) → bottom-left body corner → close.
    """
    tip_y = centre_y - 8.0
    tail_y = centre_y + 6.0
    notch_apex_y = centre_y + 2.0  # 4 pt inward from tail edge
    half_w = 3.0
    pts = [
        (centre_x - half_w, centre_y - 2.0),  # top-left body / left tip shoulder
        (centre_x, tip_y),                     # tip
        (centre_x + half_w, centre_y - 2.0),  # top-right body / right tip shoulder
        (centre_x + half_w, tail_y),           # bottom-right body / right notch shoulder
        (centre_x, notch_apex_y),              # notch apex
        (centre_x - half_w, tail_y),           # bottom-left body / left notch shoulder
        (centre_x - half_w, centre_y - 2.0),  # close
    ]
    return _rect(centre_x - half_w, tip_y, centre_x + half_w, tail_y), _line_chain(pts)


def _arrow_pointing_south(centre_x: float = 100.0, centre_y: float = 100.0):
    """Highway-sign arrow with tip at larger PDF y (chart south)."""
    tip_y = centre_y + 8.0
    tail_y = centre_y - 6.0
    notch_apex_y = centre_y - 2.0
    half_w = 3.0
    pts = [
        (centre_x - half_w, centre_y + 2.0),  # bottom-left body / left tip shoulder
        (centre_x, tip_y),                     # tip
        (centre_x + half_w, centre_y + 2.0),  # bottom-right body / right tip shoulder
        (centre_x + half_w, tail_y),           # top-right body / right notch shoulder
        (centre_x, notch_apex_y),              # notch apex
        (centre_x - half_w, tail_y),           # top-left body / left notch shoulder
        (centre_x - half_w, centre_y + 2.0),  # close
    ]
    return _rect(centre_x - half_w, tail_y, centre_x + half_w, tip_y), _line_chain(pts)


def _arrow_pointing_east(centre_x: float = 100.0, centre_y: float = 100.0):
    """Highway-sign arrow with tip at larger PDF x (chart east)."""
    tip_x = centre_x + 8.0
    tail_x = centre_x - 6.0
    notch_apex_x = centre_x - 2.0
    half_h = 3.0
    pts = [
        (centre_x + 2.0, centre_y - half_h),  # right body top / top tip shoulder
        (tip_x, centre_y),                     # tip
        (centre_x + 2.0, centre_y + half_h),  # right body bottom / bottom tip shoulder
        (tail_x, centre_y + half_h),           # left body bottom / bottom notch shoulder
        (notch_apex_x, centre_y),              # notch apex
        (tail_x, centre_y - half_h),           # left body top / top notch shoulder
        (centre_x + 2.0, centre_y - half_h),  # close
    ]
    return _rect(tail_x, centre_y - half_h, tip_x, centre_y + half_h), _line_chain(pts)


def _arrow_pointing_west(centre_x: float = 100.0, centre_y: float = 100.0):
    """Highway-sign arrow with tip at smaller PDF x (chart west)."""
    tip_x = centre_x - 8.0
    tail_x = centre_x + 6.0
    notch_apex_x = centre_x + 2.0
    half_h = 3.0
    pts = [
        (centre_x - 2.0, centre_y - half_h),  # left body top / top tip shoulder
        (tip_x, centre_y),                     # tip
        (centre_x - 2.0, centre_y + half_h),  # left body bottom / bottom tip shoulder
        (tail_x, centre_y + half_h),           # right body bottom / bottom notch shoulder
        (notch_apex_x, centre_y),              # notch apex
        (tail_x, centre_y - half_h),           # right body top / top notch shoulder
        (centre_x - 2.0, centre_y - half_h),  # close
    ]
    return _rect(tip_x, centre_y - half_h, tail_x, centre_y + half_h), _line_chain(pts)


def _dual_headed_arrow(centre_x: float = 100.0, centre_y: float = 100.0):
    """A dual-headed (bidirectional) arrow: triangular tips on both short
    edges, no concave notch. Used in the LLBG-PARDS area of the real chart
    for shared-altitude two-way segments. The bearing extractor must
    return ``None`` for these, signalling the caller to treat the arrow
    as bidirectional."""
    east_tip_x = centre_x + 8.0
    west_tip_x = centre_x - 8.0
    half_h = 3.0
    pts = [
        (centre_x + 2.0, centre_y - half_h),  # east body top
        (east_tip_x, centre_y),                # east tip
        (centre_x + 2.0, centre_y + half_h),  # east body bottom
        (centre_x - 2.0, centre_y + half_h),  # west body bottom
        (west_tip_x, centre_y),                # west tip
        (centre_x - 2.0, centre_y - half_h),  # west body top
        (centre_x + 2.0, centre_y - half_h),  # close
    ]
    return (
        _rect(west_tip_x, centre_y - half_h, east_tip_x, centre_y + half_h),
        _line_chain(pts),
    )


def test_bearing_from_north_pointing_arrow_is_zero():
    rect, items = _arrow_pointing_north()
    bearing = _arrow_bearing_pdf_deg(rect, items)
    assert bearing is not None
    assert min(bearing, 360.0 - bearing) < 5.0  # accept tiny FP slop


def test_bearing_from_east_pointing_arrow_is_ninety():
    rect, items = _arrow_pointing_east()
    bearing = _arrow_bearing_pdf_deg(rect, items)
    assert bearing is not None
    assert abs(bearing - 90.0) < 5.0


def test_bearing_from_south_pointing_arrow_is_one_eighty():
    rect, items = _arrow_pointing_south()
    bearing = _arrow_bearing_pdf_deg(rect, items)
    assert bearing is not None
    assert abs(bearing - 180.0) < 5.0


def test_bearing_from_west_pointing_arrow_is_two_seventy():
    rect, items = _arrow_pointing_west()
    bearing = _arrow_bearing_pdf_deg(rect, items)
    assert bearing is not None
    assert abs(bearing - 270.0) < 5.0


def test_bearing_returns_none_for_empty_path():
    rect, _ = _arrow_pointing_north()
    assert _arrow_bearing_pdf_deg(rect, []) is None


def test_bearing_returns_none_for_dual_headed_arrow():
    """No concave notch ⇒ no preferred direction. The matcher's bidirectional
    path picks these up later via the ``bidirectional`` flag set when the
    extractor sees a ``None`` bearing."""
    rect, items = _dual_headed_arrow()
    assert _arrow_bearing_pdf_deg(rect, items) is None


# ---------------------------------------------------------------------------
# Bidirectional axis bearing — the tip-to-tip chord through a dual-headed
# arrow's polygon. Recorded in ``bearing_deg`` so the matcher can gate
# bidirectional arrows on parallel-OR-antiparallel alignment with the
# segment direction (instead of accepting them unconditionally).
# ---------------------------------------------------------------------------


def _dual_headed_arrow_ns(centre_x: float = 100.0, centre_y: float = 100.0):
    """A vertical dual-headed arrow: tips at smaller and larger PDF y
    (chart north and chart south). Body axis runs N-S, so the axis
    bearing should canonicalise to 0° / 180°."""
    north_tip_y = centre_y - 8.0
    south_tip_y = centre_y + 8.0
    half_w = 3.0
    pts = [
        (centre_x - half_w, centre_y - 2.0),  # north body left shoulder
        (centre_x, north_tip_y),               # north tip
        (centre_x + half_w, centre_y - 2.0),  # north body right shoulder
        (centre_x + half_w, centre_y + 2.0),  # south body right shoulder
        (centre_x, south_tip_y),               # south tip
        (centre_x - half_w, centre_y + 2.0),  # south body left shoulder
        (centre_x - half_w, centre_y - 2.0),  # close
    ]
    return (
        _rect(centre_x - half_w, north_tip_y, centre_x + half_w, south_tip_y),
        _line_chain(pts),
    )


def _dual_headed_arrow_diagonal_ne_sw(
    centre_x: float = 100.0, centre_y: float = 100.0
):
    """A 45°-rotated dual-headed arrow with tips to the NE and SW. PDF +y
    is south, so 'NE' = (+x, -y) and 'SW' = (-x, +y). The body-axis
    compass bearing should resolve to ~45° (NE) or ~225° (SW), which
    collapse to the same undirected axis."""
    ne_tip_x, ne_tip_y = centre_x + 8.0, centre_y - 8.0
    sw_tip_x, sw_tip_y = centre_x - 8.0, centre_y + 8.0
    pts = [
        (centre_x + 2.0, centre_y - 4.0),  # NE body right shoulder
        (ne_tip_x, ne_tip_y),               # NE tip
        (centre_x + 4.0, centre_y - 2.0),  # NE body left shoulder
        (centre_x - 2.0, centre_y + 4.0),  # SW body left shoulder
        (sw_tip_x, sw_tip_y),               # SW tip
        (centre_x - 4.0, centre_y + 2.0),  # SW body right shoulder
        (centre_x + 2.0, centre_y - 4.0),  # close
    ]
    return (
        _rect(sw_tip_x, ne_tip_y, ne_tip_x, sw_tip_y),
        _line_chain(pts),
    )


def test_axis_bearing_east_west_dual_headed_arrow_is_90_or_270():
    """The default ``_dual_headed_arrow`` helper makes an E-W oriented
    arrow (tips at larger/smaller PDF x). The tip-to-tip chord points
    east or west, so the compass bearing should land near 90° / 270°."""
    _, items = _dual_headed_arrow()
    axis = _arrow_bidirectional_axis_bearing_pdf(items)
    assert axis is not None
    # Either tip-orientation is acceptable since the axis is undirected.
    assert _axis_diff_deg(axis, 90.0) < 5.0


def test_axis_bearing_north_south_dual_headed_arrow_is_0_or_180():
    _, items = _dual_headed_arrow_ns()
    axis = _arrow_bidirectional_axis_bearing_pdf(items)
    assert axis is not None
    assert _axis_diff_deg(axis, 0.0) < 5.0


def test_axis_bearing_diagonal_dual_headed_arrow_is_45_or_225():
    """Robust to rotation — diagonal arrows resolve to the right axis."""
    _, items = _dual_headed_arrow_diagonal_ne_sw()
    axis = _arrow_bidirectional_axis_bearing_pdf(items)
    assert axis is not None
    assert _axis_diff_deg(axis, 45.0) < 5.0


def test_axis_bearing_returns_none_for_empty_path():
    assert _arrow_bidirectional_axis_bearing_pdf([]) is None


def test_axis_bearing_returns_none_for_degenerate_coincident_points():
    """A path whose vertices all coincide numerically has no resolvable
    body axis — the helper must say so rather than emit a junk bearing
    derived from floating-point noise."""
    import fitz

    p = fitz.Point(100.0, 100.0)
    items = [("l", p, p)] * 5
    assert _arrow_bidirectional_axis_bearing_pdf(items) is None


def test_axis_diff_collapses_anti_parallel_to_parallel():
    """The undirected-axis helper must treat 0° and 180° as equivalent
    (both run along the same axis), and 90° / 270° as the maximally
    different perpendicular."""
    assert _axis_diff_deg(0.0, 0.0) == pytest.approx(0.0)
    assert _axis_diff_deg(0.0, 180.0) == pytest.approx(0.0)
    assert _axis_diff_deg(0.0, 90.0) == pytest.approx(90.0)
    assert _axis_diff_deg(45.0, 225.0) == pytest.approx(0.0)
    assert _axis_diff_deg(45.0, 135.0) == pytest.approx(90.0)
    # Small offsets near anti-parallel: still small in the axis frame.
    assert _axis_diff_deg(10.0, 195.0) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Matcher gate — the bidirectional axis-parallel check, which replaces the
# previous "accept everything within radius" pass.
# ---------------------------------------------------------------------------


def test_matcher_rejects_bidirectional_arrow_perpendicular_to_segment():
    """A horizontal (E-W) bidirectional arrow next to a N-S segment
    labels a different corridor entirely — the matcher must reject it
    even though it sits well within the proximity radius. This is the
    RIDNG↔ROKCH 1200 arrow case on the LLHZ→LLMZ route in miniature:
    bidirectional arrow body crosses our flight direction."""
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)  # north-going
    # Arrow on the segment line, body axis east-west (90° / 270°).
    arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.0, bearing_deg=90.0, altitudes_ft=(1200,),
        bidirectional=True,
    )
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == ()


def test_matcher_accepts_bidirectional_arrow_along_segment_axis():
    """N-S bidirectional arrow next to a north-going segment — the body
    axis is parallel to our travel direction, so the altitude applies."""
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)  # north-going
    arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.0, bearing_deg=0.0, altitudes_ft=(1200,),
        bidirectional=True,
    )
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == (1200,)


def test_matcher_accepts_bidirectional_arrow_anti_parallel_to_segment():
    """Same N-S axis on a SOUTH-going segment — anti-parallel is just as
    valid as parallel for a bidirectional arrow, which by definition
    labels flight in both directions along its body."""
    seg = _segment("B", 32.2, 35.0, "A", 32.0, 35.0)  # south-going
    arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.0, bearing_deg=0.0, altitudes_ft=(1200,),
        bidirectional=True,
    )
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == (1200,)


def test_matcher_accepts_bidirectional_arrow_with_180_offset_axis_bearing():
    """The extractor may emit the body-axis bearing as either tip
    direction (the longest-chord helper picks whichever ordering happens
    to come up). Both must compare identically — i.e. an arrow with
    ``bearing_deg=180.0`` (south) must accept a north-going segment just
    as readily as one with ``bearing_deg=0.0``."""
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)  # north-going
    arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.0, bearing_deg=180.0, altitudes_ft=(1200,),
        bidirectional=True,
    )
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == (1200,)


def test_matcher_rejects_bidirectional_arrow_at_45_to_segment_outside_tol():
    """The parallel-tolerance budget (30°) applies in the axis frame too.
    An axis 45° off the segment direction is well outside the gate."""
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)  # north-going
    arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.0, bearing_deg=45.0, altitudes_ft=(1200,),
        bidirectional=True,
    )
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == ()


# ---------------------------------------------------------------------------
# Tail anchor — the route attachment point used as the arrow's "position"
# ---------------------------------------------------------------------------


def test_tail_anchor_for_north_arrow_lies_on_bottom_edge():
    """An up-pointing arrow's tail is on the bottom of its bbox (highest y in
    PDF coords). Anchoring there puts the arrow on the chart's route line
    instead of half a long-axis above it."""
    rect, items = _arrow_pointing_north(centre_x=100.0, centre_y=100.0)
    tail = _arrow_tail_anchor_pdf(rect, items)
    assert tail is not None
    tx, ty = tail
    assert abs(tx - 100.0) < 1.0  # x on the centre line
    assert abs(ty - rect.y1) < 1e-6  # y on the bottom (tail) edge


def test_tail_anchor_for_east_arrow_lies_on_left_edge():
    rect, items = _arrow_pointing_east(centre_x=100.0, centre_y=100.0)
    tail = _arrow_tail_anchor_pdf(rect, items)
    assert tail is not None
    tx, ty = tail
    assert abs(tx - rect.x0) < 1e-6
    assert abs(ty - 100.0) < 1.0


def test_tail_anchor_for_south_arrow_lies_on_top_edge():
    rect, items = _arrow_pointing_south(centre_x=100.0, centre_y=100.0)
    tail = _arrow_tail_anchor_pdf(rect, items)
    assert tail is not None
    tx, ty = tail
    assert abs(tx - 100.0) < 1.0
    assert abs(ty - rect.y0) < 1e-6


def test_tail_anchor_returns_none_for_empty_path():
    rect, _ = _arrow_pointing_north()
    assert _arrow_tail_anchor_pdf(rect, []) is None


def test_tail_anchor_returns_none_for_dual_headed_arrow():
    """Dual-headed arrows have no tail edge to anchor on; the extractor
    drops them onto the bbox centre via a separate code path."""
    rect, items = _dual_headed_arrow()
    assert _arrow_tail_anchor_pdf(rect, items) is None


def test_tail_anchor_offsets_position_significantly_from_bbox_centre():
    """End-to-end: the tail must be measurably closer to the chart line than
    the bbox centre — otherwise we get the same broken matches we had before
    the v1→v2 cache bump."""
    rect, items = _arrow_pointing_east(centre_x=100.0, centre_y=100.0)
    tail = _arrow_tail_anchor_pdf(rect, items)
    assert tail is not None
    tx, _ = tail
    bbox_centre_x = (rect.x0 + rect.x1) * 0.5
    # For a horizontal arrow with width ~14, the tail (left edge) sits 7 pt
    # left of centre — a meaningful displacement, not a nudge.
    assert (bbox_centre_x - tx) > 5.0


# ---------------------------------------------------------------------------
# PDF-pt → cropped pixmap UV
# ---------------------------------------------------------------------------


def test_pdf_pt_to_uv_identity_crop():
    """No crop → just a uniform pt × dpi/72 → divide by pixel dims."""
    crop = CropMeta(
        offset_x=0, offset_y=0,
        source_w=2000, source_h=1000,
        cropped_w=2000, cropped_h=1000,
    )
    # 100 pt at 144 DPI = 200 pixels = 0.1 of the 2000-px width.
    uv = _pdf_pt_to_pixmap_uv(100.0, 50.0, render_dpi=144.0, crop=crop)
    assert uv is not None
    assert uv == pytest.approx((0.1, 0.1), rel=1e-12)


def test_pdf_pt_to_uv_with_offset_crop():
    """A trimmed margin shifts the projection's origin."""
    crop = CropMeta(
        offset_x=200, offset_y=100,
        source_w=2000, source_h=1000,
        cropped_w=1600, cropped_h=800,
    )
    # Pt 200 at 144 DPI = 400 px, minus 200-px offset = 200 px,
    # divided by 1600-px cropped width = 0.125.
    uv = _pdf_pt_to_pixmap_uv(200.0, 100.0, render_dpi=144.0, crop=crop)
    assert uv is not None
    assert uv == pytest.approx((0.125, 0.125), rel=1e-12)


def test_pdf_pt_to_uv_returns_none_when_outside_cropped_area():
    """Arrow projected into a trimmed margin → caller must drop it."""
    crop = CropMeta(
        offset_x=200, offset_y=100,
        source_w=2000, source_h=1000,
        cropped_w=1600, cropped_h=800,
    )
    # 50 pt × 144/72 = 100 px, minus 200 offset = -100 → u < 0 → None.
    assert _pdf_pt_to_pixmap_uv(50.0, 50.0, render_dpi=144.0, crop=crop) is None


# ---------------------------------------------------------------------------
# Cross-track distance and bearing helpers
# ---------------------------------------------------------------------------


def test_circular_diff_handles_wraparound():
    assert _circular_diff_deg(10.0, 350.0) == pytest.approx(20.0)
    assert _circular_diff_deg(350.0, 10.0) == pytest.approx(20.0)
    assert _circular_diff_deg(0.0, 180.0) == pytest.approx(180.0)


def test_circular_diff_zero_for_identical():
    assert _circular_diff_deg(123.0, 123.0) == pytest.approx(0.0)


def test_cross_track_zero_when_point_on_segment_midpoint():
    """A point at the geometric midpoint of a short segment is 0 nm off."""
    a_lat, a_lon = 32.0, 35.0
    b_lat, b_lon = 32.1, 35.1
    p_lat, p_lon = 32.05, 35.05
    d = _great_circle_distance_to_segment_nm(a_lat, a_lon, b_lat, b_lon, p_lat, p_lon)
    assert d < 0.05  # well under our 0.5 nm match radius


def test_cross_track_clamps_to_endpoint_when_point_is_past_segment_end():
    """A point well past TO returns the great-circle distance to TO, not the
    perpendicular distance to the (extended) segment line — that's how the
    matcher refuses to attribute an arrow to the wrong segment of a multi-
    leg route."""
    a_lat, a_lon = 32.0, 35.0
    b_lat, b_lon = 32.1, 35.0
    # 5 nm north of B, on the segment's bearing line.
    p_lat, p_lon = 32.1 + 5.0 / 60.0, 35.0
    d = _great_circle_distance_to_segment_nm(a_lat, a_lon, b_lat, b_lon, p_lat, p_lon)
    assert 4.5 < d < 5.5


def test_distance_and_overshoot_zero_overshoot_on_segment_midpoint():
    """A point whose perpendicular foot lies on the segment has zero
    overshoot — the gate only fires when the foot is past an endpoint."""
    a_lat, a_lon = 32.0, 35.0
    b_lat, b_lon = 32.1, 35.1
    p_lat, p_lon = 32.05, 35.05
    d, overshoot = _distance_and_overshoot_to_segment_nm(
        a_lat, a_lon, b_lat, b_lon, p_lat, p_lon
    )
    assert d < 0.05
    assert overshoot == pytest.approx(0.0, abs=1e-6)


def test_distance_and_overshoot_reports_overshoot_past_endpoint_b():
    """A point past endpoint B along the segment direction reports a
    positive overshoot equal to how far past B the foot would be."""
    a_lat, a_lon = 32.0, 35.0
    b_lat, b_lon = 32.1, 35.0
    # 0.5 nm north of B along the segment's bearing line. The foot
    # projects to t > 1, with along-segment overshoot of ~0.5 nm.
    p_lat, p_lon = 32.1 + 0.5 / 60.0, 35.0
    d, overshoot = _distance_and_overshoot_to_segment_nm(
        a_lat, a_lon, b_lat, b_lon, p_lat, p_lon
    )
    assert d == pytest.approx(0.5, abs=0.05)
    assert overshoot == pytest.approx(0.5, abs=0.05)


def test_distance_and_overshoot_reports_overshoot_past_endpoint_a():
    """Symmetric of the past-B case: a point south of A along the
    segment's reverse bearing reports a positive overshoot."""
    a_lat, a_lon = 32.0, 35.0
    b_lat, b_lon = 32.1, 35.0
    # 0.3 nm south of A, on the segment's reverse bearing. Foot at t < 0.
    p_lat, p_lon = 32.0 - 0.3 / 60.0, 35.0
    d, overshoot = _distance_and_overshoot_to_segment_nm(
        a_lat, a_lon, b_lat, b_lon, p_lat, p_lon
    )
    assert d == pytest.approx(0.3, abs=0.05)
    assert overshoot == pytest.approx(0.3, abs=0.05)


def test_distance_and_overshoot_perpendicular_point_reports_zero_overshoot():
    """A point offset perpendicularly from the segment midpoint reports
    its cross-track distance and zero overshoot — the foot is on the
    segment, the gate does not fire."""
    a_lat, a_lon = 32.0, 35.0
    b_lat, b_lon = 32.1, 35.0
    # 0.4 nm east of the segment midpoint. The foot lies exactly on the
    # segment (t = 0.5), so overshoot is 0 and distance ≈ 0.4 nm.
    mid_lat = 32.05
    p_lat = mid_lat
    cos_lat = math.cos(math.radians(mid_lat))
    p_lon = 35.0 + (0.4 / 60.0) / cos_lat
    d, overshoot = _distance_and_overshoot_to_segment_nm(
        a_lat, a_lon, b_lat, b_lon, p_lat, p_lon
    )
    assert d == pytest.approx(0.4, abs=0.02)
    assert overshoot == pytest.approx(0.0, abs=1e-6)


def test_match_endpoint_overshoot_threshold_is_in_safe_band():
    """The default ``MATCH_MAX_ENDPOINT_OVERSHOOT_NM`` must stay tight
    enough to catch the canonical past-endpoint bug (the Dead Sea
    ``3147N03530E → ALMOG`` case has overshoot ~0.42 nm at ICAO-
    minute-rounded coords) while accepting the worst-known legitimate
    match (the LLHA→LLHZ inverse route's ``3250N03458E → 3249N03457E``
    middle sub-leg has overshoot ~0.26 nm). Anything below 0.26 nm
    would regress that intermediate-leg match; anything at or above
    0.40 nm would re-admit the Dead Sea bug at the rounded-coord
    test path. Pinned here so a future tuning pass doesn't silently
    land outside the narrow safe band."""
    assert 0.26 < MATCH_MAX_ENDPOINT_OVERSHOOT_NM < 0.40


def test_parallel_tol_past_endpoint_is_in_safe_band():
    """``MATCH_PARALLEL_TOL_DEG_PAST_ENDPOINT`` is the second gate that
    catches the Dead Sea bug's *precise-coord* shape (sub-minute click
    positions yield overshoot 0.29 nm — just inside the 0.30 nm
    overshoot gate — but fwd-diff 23.6°, outside the past-endpoint
    parallel budget). The known-good LLHA→LLHZ legit past-endpoint
    match has fwd-diff 0.9°. 15° is the value pinned here: anything
    above 23° would re-admit the Dead Sea bug; anything below 1°
    would regress the LLHA→LLHZ match. The 22° gap between the two
    populations gives ample headroom in either direction."""
    assert 1.0 < MATCH_PARALLEL_TOL_DEG_PAST_ENDPOINT < 23.0


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------


def _wp(code: str, lat: float, lon: float) -> WaypointRecord:
    return WaypointRecord(
        index=0, code=code, name_he="", reporting_type="",
        lat=lat, lon=lon, lat_dms="", lon_dms="",
    )


def _segment(
    from_code: str, from_lat: float, from_lon: float,
    to_code: str, to_lat: float, to_lon: float,
) -> RouteSegment:
    fp = RoutePoint(lat=from_lat, lon=from_lon, waypoint=_wp(from_code, from_lat, from_lon))
    tp = RoutePoint(lat=to_lat, lon=to_lon, waypoint=_wp(to_code, to_lat, to_lon))
    from cvfr_routemaster.route import great_circle_distance_nm, magnetic_bearing_deg
    return RouteSegment(
        from_point=fp,
        to_point=tp,
        from_label=from_code,
        to_label=to_code,
        distance_nm=great_circle_distance_nm(from_lat, from_lon, to_lat, to_lon),
        mag_bearing_deg=magnetic_bearing_deg(from_lat, from_lon, to_lat, to_lon),
    )


# Sidedness primitives — the matcher's main disambiguator.


def test_arrow_side_for_north_segment_with_eastern_arrow_is_right():
    """Going north, east is right — the OUR-direction arrow per CVFR
    convention. Cross-product sign should be -1 (RIGHT)."""
    side = _arrow_side_of_segment(
        seg_from_lat=32.0, seg_from_lon=35.0,
        seg_to_lat=32.2, seg_to_lon=35.0,
        arrow_lat=32.1, arrow_lon=35.01,  # east of the line
    )
    assert side == -1


def test_arrow_side_for_north_segment_with_western_arrow_is_left():
    side = _arrow_side_of_segment(
        seg_from_lat=32.0, seg_from_lon=35.0,
        seg_to_lat=32.2, seg_to_lon=35.0,
        arrow_lat=32.1, arrow_lon=34.99,  # west of the line
    )
    assert side == 1


def test_arrow_side_for_east_segment_with_southern_arrow_is_right():
    """Going east, south is right (rotate forward 90° clockwise)."""
    side = _arrow_side_of_segment(
        seg_from_lat=32.0, seg_from_lon=35.0,
        seg_to_lat=32.0, seg_to_lon=35.2,
        arrow_lat=31.99, arrow_lon=35.1,  # south of the line
    )
    assert side == -1


def test_arrow_side_for_west_segment_with_northern_arrow_is_right():
    """Going west, north is right."""
    side = _arrow_side_of_segment(
        seg_from_lat=32.0, seg_from_lon=35.2,
        seg_to_lat=32.0, seg_to_lon=35.0,
        arrow_lat=32.01, arrow_lon=35.1,  # north of the line
    )
    assert side == -1


def test_arrow_side_for_south_segment_with_western_arrow_is_right():
    """Going south, west is right."""
    side = _arrow_side_of_segment(
        seg_from_lat=32.2, seg_from_lon=35.0,
        seg_to_lat=32.0, seg_to_lon=35.0,
        arrow_lat=32.1, arrow_lon=34.99,  # west of the line
    )
    assert side == -1


def test_arrow_side_for_arrow_on_segment_line_is_zero():
    """An arrow exactly on the line gets side 0 — these fall back into the
    'left or on the line' bucket so they can still match if no clear right-
    side candidate exists."""
    side = _arrow_side_of_segment(
        seg_from_lat=32.0, seg_from_lon=35.0,
        seg_to_lat=32.2, seg_to_lon=35.0,
        arrow_lat=32.1, arrow_lon=35.0,  # on the line
    )
    assert side == 0


# Matcher behaviour on synthetic two-way segments.


def test_matcher_accepts_parallel_right_side_arrow():
    """The textbook case: a north-bound segment with a north-pointing arrow
    sitting east of the line (right of the flight path). Returns its
    altitudes."""
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)
    arrow = GeoAltitudeArrow(lat=32.1, lon=35.001, bearing_deg=0.0, altitudes_ft=(2000,))
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == (2000,)


def test_matcher_rejects_anti_parallel_arrow():
    """The opposing arrow on a two-way leg has its heading 180° from ours
    (it's the chart's labelling for the REVERSE direction of travel). The
    parallel-only filter rejects it outright, regardless of side — its
    altitude isn't valid for our flight."""
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)  # northbound
    # Same right side, but pointing south.
    arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.001, bearing_deg=180.0, altitudes_ft=(2000,),
    )
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == ()


def test_matcher_prefers_parallel_right_over_parallel_left():
    """Both arrows parallel to the segment but on opposite sides: the
    chart's right-of-track convention favours the right-side one."""
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)
    right_arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.001, bearing_deg=0.0, altitudes_ft=(1500,),
    )
    left_arrow = GeoAltitudeArrow(
        lat=32.1, lon=34.999, bearing_deg=0.0, altitudes_ft=(2000,),
    )
    out = match_altitudes_for_segment(seg, {"north": [left_arrow, right_arrow]})
    assert out == (1500,)


def test_matcher_falls_back_to_parallel_left_when_no_parallel_right():
    """The BOREN→HOTRM exception: the chart prints the OUR-direction arrow
    on the left of the track. With nothing parallel on the right, the
    matcher takes the closest parallel left-side candidate."""
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)
    # An anti-parallel arrow on the right (rejected as opposite direction)…
    anti_right = GeoAltitudeArrow(
        lat=32.1, lon=35.001, bearing_deg=180.0, altitudes_ft=(2000,),
    )
    # …and a parallel arrow on the left (chart anomaly, but bearing trumps
    # sidedness — this is OUR direction).
    parallel_left = GeoAltitudeArrow(
        lat=32.1, lon=34.999, bearing_deg=0.0, altitudes_ft=(1500,),
    )
    out = match_altitudes_for_segment(seg, {"north": [anti_right, parallel_left]})
    assert out == (1500,)


def test_matcher_rejects_perpendicular_crossing_route_arrow():
    """An east-west route's arrow that happens to lie inside our north-bound
    segment's tube must NOT match — it's a foreign route. The parallel-only
    filter catches it (90° from forward, well past the parallel tolerance)."""
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)
    perp_arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.001, bearing_deg=90.0, altitudes_ft=(2000,),
    )
    assert match_altitudes_for_segment(seg, {"north": [perp_arrow]}) == ()


def test_matcher_rejects_arrow_too_far_from_segment_line():
    """A correctly-aligned arrow well outside *every* gate fails — past
    the strict 0.65 nm primary radius AND past the 1.8 nm wide-corridor
    rescue radius."""
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)
    # ~2.5 nm east of the segment line at lat 32 — past both gates.
    far_lon = 35.0 + 3.0 / 60.0
    arrow = GeoAltitudeArrow(
        lat=32.1, lon=far_lon, bearing_deg=0.0, altitudes_ft=(2000,),
    )
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == ()


def test_matcher_rejects_arrow_whose_foot_projects_past_terminal_endpoint():
    """The Dead Sea bug (May 13 2026): an arrow whose perpendicular foot
    sits substantially past the segment's TO endpoint belongs by chart
    convention to whatever route continues from that endpoint, not to
    OUR leg. The endpoint-clamped cross-track distance can fool the
    radius gate — the overshoot gate is what kicks it out.

    Synthetic shape mirroring the bug: a northbound 4 nm leg, with an
    arrow 0.5 nm past the TO endpoint plus a small cross-track offset
    so it lands at ~0.5 nm endpoint-clamped distance (inside the 0.65
    nm strict radius) and bears the same direction as the segment.
    Without the overshoot gate the matcher would have accepted it.
    """
    seg = _segment("A", 32.0, 35.0, "B", 32.0667, 35.0)  # ~4 nm north
    # 0.5 nm north of B (foot at t > 1, overshoot ~0.5 nm) + a tiny
    # cross-track offset so the *clamped* endpoint distance still fits
    # the strict radius gate.
    cos_lat = math.cos(math.radians(32.0667))
    past_lat = 32.0667 + 0.5 / 60.0
    past_lon = 35.0 + (0.05 / 60.0) / cos_lat
    arrow = GeoAltitudeArrow(
        lat=past_lat, lon=past_lon, bearing_deg=0.0, altitudes_ft=(4000,),
    )
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == ()


def test_matcher_rejects_arrow_whose_foot_projects_past_origin_endpoint():
    """Symmetric of the past-TO case: an arrow whose foot lies before
    the FROM endpoint is the *previous* leg's chart label, not ours."""
    seg = _segment("A", 32.0, 35.0, "B", 32.0667, 35.0)
    # 0.5 nm south of A, on the segment's reverse bearing line.
    arrow = GeoAltitudeArrow(
        lat=32.0 - 0.5 / 60.0, lon=35.0, bearing_deg=0.0, altitudes_ft=(4000,),
    )
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == ()


def test_matcher_accepts_arrow_with_small_endpoint_overshoot():
    """Real chart arrows occasionally land just past a waypoint by a few
    hundred feet — calibration noise + the bbox-vs-tail anchor wobble.
    The overshoot gate has a 0.20 nm budget specifically to keep those
    legitimate matches; only substantively past-endpoint arrows get
    kicked out."""
    seg = _segment("A", 32.0, 35.0, "B", 32.0667, 35.0)
    # 0.10 nm past B — well inside the 0.20 nm overshoot budget.
    arrow = GeoAltitudeArrow(
        lat=32.0667 + 0.10 / 60.0, lon=35.0, bearing_deg=0.0, altitudes_ft=(1500,),
    )
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == (1500,)


def test_matcher_overshoot_gate_fires_on_intermediate_loose_radius_leg():
    """Pin the actual bug shape: an intermediate-leg loose radius
    (1.30 nm) used to let through arrows up to ~0.7 nm past an endpoint
    because the endpoint-clamped cross-track stayed inside the loose
    gate. The overshoot gate is the second line of defence that finally
    rejects those.
    """
    fp = RoutePoint(lat=32.0, lon=35.0, waypoint=None)  # free-clicked
    tp = RoutePoint(
        lat=32.0667, lon=35.0,
        waypoint=_wp("B", 32.0667, 35.0),  # real waypoint
    )
    from cvfr_routemaster.route import great_circle_distance_nm, magnetic_bearing_deg
    seg = RouteSegment(
        from_point=fp, to_point=tp,
        from_label="3200N03500E", to_label="B",
        distance_nm=great_circle_distance_nm(fp.lat, fp.lon, tp.lat, tp.lon),
        mag_bearing_deg=magnetic_bearing_deg(fp.lat, fp.lon, tp.lat, tp.lon),
    )
    # 0.5 nm past B + 0.3 nm cross-track. Endpoint-clamped distance
    # ≈ 0.58 nm — inside the loose 1.30 nm radius — but along-segment
    # overshoot is 0.5 nm, well past the 0.30 nm threshold.
    cos_lat = math.cos(math.radians(32.0667))
    arrow = GeoAltitudeArrow(
        lat=32.0667 + 0.5 / 60.0,
        lon=35.0 + (0.3 / 60.0) / cos_lat,
        bearing_deg=0.0,
        altitudes_ft=(4000,),
    )
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == ()


def test_matcher_rejects_past_endpoint_arrow_with_loose_parallel_alignment():
    """Pin the precise-coord Dead Sea bug shape. An arrow whose foot
    is only slightly past the segment endpoint (overshoot inside the
    overshoot-budget) but whose bearing is loosely aligned with the
    segment (fwd-diff > the past-endpoint parallel-tol budget) must
    still be rejected — past-endpoint arrows don't get the wider
    30° tolerance the on-segment case enjoys.
    """
    seg = _segment("A", 32.0, 35.0, "B", 32.0, 34.9)  # westbound, ~5 nm
    # 0.15 nm past B (overshoot inside the 0.30 nm budget) + some
    # perpendicular offset so the endpoint-clamped distance stays in
    # the radius. Bearing 248° — 22° off the segment's 270° westbound
    # bearing, inside the on-segment 30° tolerance but outside the
    # 15° past-endpoint tolerance.
    cos_lat = math.cos(math.radians(32.0))
    past_lon = 34.9 - (0.15 / 60.0) / cos_lat
    arrow = GeoAltitudeArrow(
        lat=32.0 + 0.1 / 60.0,  # slight perpendicular offset
        lon=past_lon,
        bearing_deg=248.0,
        altitudes_ft=(4000,),
    )
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == ()


def test_matcher_accepts_past_endpoint_arrow_with_tight_parallel_alignment():
    """Symmetric of the previous test: a past-endpoint arrow whose
    bearing is *very tightly* aligned with the segment (e.g. the
    LLHA→LLHZ middle sub-leg's legitimate match at fwd-diff 0.9°)
    must still be accepted. The conditional gate is for *loosely*
    aligned past-endpoint arrows, not all of them.
    """
    seg = _segment("A", 32.0, 35.0, "B", 32.0, 34.9)  # westbound
    cos_lat = math.cos(math.radians(32.0))
    past_lon = 34.9 - (0.15 / 60.0) / cos_lat
    arrow = GeoAltitudeArrow(
        lat=32.0 + 0.1 / 60.0,
        lon=past_lon,
        bearing_deg=271.0,  # 1° off segment bearing of 270°
        altitudes_ft=(1500,),
    )
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == (1500,)


# On-segment vs past-endpoint tier (the SORES→SHARG fix).
#
# The matcher's existing gates already reject the wildly-past-endpoint
# arrows (overshoot > 0.30 nm) and the loosely-aligned past-endpoint
# arrows (fwd-diff > 15° past the endpoint). Both gates leave a window
# where *both* an on-segment arrow and a past-endpoint arrow can pass
# and end up competing on their numeric scores alone. Before the
# ``_onseg_tier`` ranking layer, the past-endpoint arrow could win that
# tie simply by being closer cross-track than the on-segment arrow
# (the endpoint-clamp puts its distance at the endpoint-to-arrow great-
# circle distance, which is often smaller than the on-segment arrow's
# perpendicular distance for a mid-segment label). The tier sits
# between ``class_rank`` and the numeric score in ``_fit_key`` and
# breaks that tie in favour of the on-segment arrow — by chart
# convention the arrow that *lies along* our leg labels our leg, not
# the one that lies just past its end (which labels the route
# continuing from that endpoint). See ``_fit_key``'s docstring for
# the full breakdown.


def test_fit_key_tiers_on_segment_ahead_of_past_endpoint_within_class():
    """Two right-of-track arrows of comparable raw score, one
    on-segment and one past-endpoint, should sort with the on-segment
    one first. The tier slot lives between ``class_rank`` and the
    score, so this holds even when the past-endpoint arrow has a
    *better* numeric score."""
    onseg = _ArrowSegFit(
        class_rank=_CLASS_PARALLEL_RIGHT,
        distance_nm=0.30,
        fwd_diff_deg=10.0,
        overshoot_nm=0.0,
    )
    past_end = _ArrowSegFit(
        class_rank=_CLASS_PARALLEL_RIGHT,
        distance_nm=0.05,  # closer cross-track…
        fwd_diff_deg=0.0,  # …and more tightly parallel…
        overshoot_nm=0.10,  # …but past the endpoint.
    )
    # On-segment wins despite a strictly worse raw score.
    assert _fit_key(onseg) < _fit_key(past_end)
    # And the tier slot is exactly what's separating them.
    assert _onseg_tier(onseg) == _ONSEG_TIER_ON_SEGMENT
    assert _onseg_tier(past_end) == _ONSEG_TIER_PAST_ENDPOINT


def test_fit_key_class_rank_still_dominates_overshoot_tier():
    """The tier is *secondary* to ``class_rank`` — a right-of-track
    past-endpoint arrow still beats a left-of-track on-segment arrow.
    The side-of-track signal is a stronger same-direction indicator
    than the along-vs-past signal, so the tier must not override it.
    """
    right_past = _ArrowSegFit(
        class_rank=_CLASS_PARALLEL_RIGHT,
        distance_nm=0.30,
        fwd_diff_deg=2.0,
        overshoot_nm=0.10,
    )
    left_onseg = _ArrowSegFit(
        class_rank=_CLASS_PARALLEL_LEFT,
        distance_nm=0.05,
        fwd_diff_deg=0.0,
        overshoot_nm=0.0,
    )
    assert _fit_key(right_past) < _fit_key(left_onseg)


def test_fit_key_tier_threshold_is_strict_zero():
    """``_onseg_tier`` partitions on overshoot ``> 0.0`` exactly —
    ``_distance_and_overshoot_to_segment_nm`` returns *exactly* ``0.0``
    when the perpendicular foot lies inside the segment, so any
    positive overshoot is treated as past-endpoint. No floating-point
    fuzz."""
    onseg = _ArrowSegFit(
        class_rank=_CLASS_PARALLEL_RIGHT,
        distance_nm=0.2,
        fwd_diff_deg=0.0,
        overshoot_nm=0.0,
    )
    tiny_past = _ArrowSegFit(
        class_rank=_CLASS_PARALLEL_RIGHT,
        distance_nm=0.2,
        fwd_diff_deg=0.0,
        overshoot_nm=1e-9,
    )
    assert _onseg_tier(onseg) == _ONSEG_TIER_ON_SEGMENT
    assert _onseg_tier(tiny_past) == _ONSEG_TIER_PAST_ENDPOINT


def test_matcher_prefers_on_segment_alternative_over_past_endpoint():
    """Integration: SORES→SHARG-style scenario on a single synthetic
    leg. Two right-of-track arrows compete for the same segment:

    * **On-segment, mid-leg** with a 3300 label and a small bearing
      offset and a noticeable cross-track distance — its perpendicular
      foot lies *along* the segment.
    * **At-the-endpoint, slightly past** with a 2300 label and bearing
      tightly aligned with the segment, sitting *just past* the TO
      endpoint (overshoot inside the 0.30 nm budget AND fwd-diff
      inside the 15° past-endpoint parallel-tol budget). Without the
      on-segment tier, this arrow's smaller endpoint-clamped distance
      would win the score competition.

    Expected: the on-segment 3300 arrow wins, just like the chart-
    reading human's eye does on the real SORES→SHARG leg.
    """
    seg = _segment("A", 32.0, 35.0, "B", 32.10, 35.0)  # ~6 nm northbound
    cos_lat = math.cos(math.radians(32.05))

    onseg_3300 = GeoAltitudeArrow(
        lat=32.05,  # mid-segment along-track
        lon=35.0 + (0.30 / 60.0) / cos_lat,  # 0.3 nm right of line
        bearing_deg=8.0,  # 8° off segment bearing (well inside 30°)
        altitudes_ft=(3300,),
    )
    past_2300 = GeoAltitudeArrow(
        lat=32.10 + (0.10 / 60.0),  # 0.10 nm past B (overshoot 0.10 nm)
        lon=35.0 + (0.05 / 60.0) / cos_lat,  # 0.05 nm right of line
        bearing_deg=1.0,  # tightly parallel (inside past-endpoint 15°)
        altitudes_ft=(2300,),
    )
    out = match_altitudes_for_segment(
        seg, {"sheet": [onseg_3300, past_2300]}
    )
    assert out == (3300,)


def test_matcher_keeps_past_endpoint_arrow_when_no_on_segment_alternative():
    """Solitary past-endpoint candidate still wins by default — the
    on-segment tier is a tiebreaker between candidates that both pass
    the existing gates, never a hard reject on its own. Mirrors the
    LLHA→LLHZ middle sub-leg case where the genuine chart label
    happens to project just past the leg's endpoint and there's no
    on-segment alternative to compete with."""
    seg = _segment("A", 32.0, 35.0, "B", 32.10, 35.0)
    cos_lat = math.cos(math.radians(32.05))
    only_past = GeoAltitudeArrow(
        lat=32.10 + (0.10 / 60.0),  # past B, inside overshoot budget
        lon=35.0 + (0.05 / 60.0) / cos_lat,
        bearing_deg=1.0,  # tightly parallel
        altitudes_ft=(2000,),
    )
    out = match_altitudes_for_segment(seg, {"sheet": [only_past]})
    assert out == (2000,)


# Bisector-bearing helper.


def test_bisector_bearing_handles_simple_pair():
    """Bisector of two same-quadrant bearings is their straight average."""
    out = _bisector_bearing_deg(60.0, 120.0)
    assert math.isclose(out, 90.0, abs_tol=1e-6)


def test_bisector_bearing_handles_wraparound():
    """Bisector of (350°, 10°) is 0° (or 360°) — straight up north, not
    180° south. Plain averaging would give 180° which is the back
    bisector; the vector-sum form picks the *acute* bisector, which is
    what the bend-arrow signature wants."""
    out = _bisector_bearing_deg(350.0, 10.0)
    # Allow either 0.0 or a wraparound numerical artefact near 360.
    folded = out if out < 180.0 else out - 360.0
    assert math.isclose(folded, 0.0, abs_tol=1e-6)


def test_bisector_bearing_matches_htzuk_kntry_llhz_case():
    """Pin the canonical HTZUK→KNTRY→LLHZ bend the rescue is targeted at:
    leg bearings 103.6° (ESE) and 35.9° (NE) should bisect to ~69.7°,
    very close to the chart's 71.4° corridor arrow."""
    out = _bisector_bearing_deg(103.6, 35.9)
    # Straight-average bisector is (103.6 + 35.9) / 2 = 69.75°; the
    # vector-sum form coincides exactly here because the two bearings
    # are within the same hemisphere and well shy of antipodal.
    assert math.isclose(out, 69.75, abs_tol=0.05)


def test_bisector_bearing_handles_antipodal_degenerate():
    """Two bearings exactly 180° apart have no geometrically-meaningful
    bisector (any perpendicular direction is equally valid). The helper
    returns ``b1 + 90`` as a stable deterministic choice. This case
    never arises in the bend-arrow rescue because the min-bend gate is
    well below 180°, but the helper handles it for robustness."""
    out = _bisector_bearing_deg(0.0, 180.0)
    assert math.isclose(out, 90.0, abs_tol=1e-6)


# Shared-bend arrow rescue (post-pass).
#
# The rescue fires only on the narrow case where:
# 1. Two consecutive real-waypoint legs share a waypoint at a bend.
# 2. Neither leg has a primary chart-arrow match.
# 3. Bend angle exceeds ``MATCH_BEND_RESCUE_MIN_BEND_DEG`` (30°).
# 4. An unclaimed chart arrow sits near at least one leg's line and
#    its bearing matches the bisector of the two legs within
#    ``MATCH_BEND_RESCUE_BISECTOR_TOL_DEG`` (15°).
# Canonical case: HTZUK→KNTRY→LLHZ at LLHZ's Class-D approach. See
# ``MATCH_BEND_RESCUE_*`` constants for the gate values and the
# rationale behind each.


def _two_leg_route(
    a_code: str, a_lat: float, a_lon: float,
    b_code: str, b_lat: float, b_lon: float,
    c_code: str, c_lat: float, c_lon: float,
) -> tuple[RouteSegment, RouteSegment]:
    """Build a two-segment route A→B→C with all three points as real
    waypoints, since the bend rescue only fires on real-waypoint legs."""
    seg_ab = _segment(a_code, a_lat, a_lon, b_code, b_lat, b_lon)
    seg_bc = _segment(b_code, b_lat, b_lon, c_code, c_lat, c_lon)
    return seg_ab, seg_bc


def test_bend_rescue_attributes_bisector_arrow_to_both_legs():
    """Pin the HTZUK→KNTRY→LLHZ-shaped case. Build a synthetic 2-leg
    bend with the chart-style geometry:

    * Leg A→B bears 104° (ESE), short (1.2 nm).
    * Leg B→C bears 36° (NE), longer (~3 nm).
    * Bend at B is ~68° — well above the 30° min-bend gate.
    * Bisector = (104° + 36°) / 2 = 70°.
    * Arrow bearing 71° (≈1° off bisector), placed near A on a side
      where neither leg's standard gate accepts it (fwd-diff 33° on
      A→B is just past parallel tol; perpendicular distance 1.0+ nm
      on B→C is past the real-waypoint radius gate). With the rescue,
      both legs receive the arrow's altitude.
    """
    # A = HTZUK-ish, B = KNTRY-ish, C = LLHZ-ish.
    seg_ab, seg_bc = _two_leg_route(
        "A", 32.146, 34.778,
        "B", 32.141, 34.801,
        "C", 32.179, 34.834,
    )
    bend_arrow = GeoAltitudeArrow(
        lat=32.148, lon=34.782,  # ~0.24 nm NE of A, 1.06 nm from B
        bearing_deg=71.4,
        altitudes_ft=(1200,),
    )
    out = match_altitudes_for_route(
        [seg_ab, seg_bc], {"north": [bend_arrow]}
    )
    assert out == [(1200,), (1200,)]


def test_bend_rescue_skips_when_either_leg_already_matched():
    """Rescue must never trample a per-leg match. Same synthetic bend,
    but add a strong on-segment arrow specifically for leg A→B with a
    different altitude; the bend-bisector arrow is still present.
    Expected: A→B gets its proper match, B→C stays unknown (no
    propagation of the matched arrow's altitude through the bend).
    This is the NSHRM→SIRNI→NTAIM-style safety: a corridor whose
    altitude legitimately *changes* at the shared waypoint must not
    be smeared across both sides by the rescue."""
    seg_ab, seg_bc = _two_leg_route(
        "A", 32.146, 34.778,
        "B", 32.141, 34.801,
        "C", 32.179, 34.834,
    )
    cos_lat = math.cos(math.radians(32.143))
    on_segment_arrow_ab = GeoAltitudeArrow(
        # On the A→B line, mid-segment, bearing aligned with leg.
        lat=32.1435,
        lon=34.7895 + (0.05 / 60.0) / cos_lat,
        bearing_deg=104.0,  # ~matches A→B bearing
        altitudes_ft=(2500,),
    )
    bend_arrow = GeoAltitudeArrow(
        lat=32.148, lon=34.782, bearing_deg=71.4, altitudes_ft=(1200,),
    )
    out = match_altitudes_for_route(
        [seg_ab, seg_bc], {"north": [on_segment_arrow_ab, bend_arrow]}
    )
    # A→B picks the per-leg arrow. B→C stays unknown (no propagation).
    assert out[0] == (2500,)
    assert out[1] == ()


def test_bend_rescue_skips_when_bend_angle_below_threshold():
    """A near-straight pair of legs (bend < 30°) shouldn't trigger the
    rescue — if a corridor arrow exists for an almost-straight run, the
    standard parallel-tolerance gate would have caught it on at least
    one of the two legs. Firing the rescue here risks admitting arrows
    the standard matcher already considered and correctly rejected."""
    # Bend ~10° — both legs going generally north with a tiny dogleg.
    seg_ab, seg_bc = _two_leg_route(
        "A", 32.00, 35.00,
        "B", 32.10, 35.00,
        "C", 32.20, 35.02,
    )
    bisector_arrow = GeoAltitudeArrow(
        # Off-segment arrow whose bearing matches the (~5°) bisector;
        # if the rescue ignored the bend gate it would attribute this
        # arrow to both legs spuriously.
        lat=32.10, lon=35.012,
        bearing_deg=5.0,
        altitudes_ft=(1500,),
    )
    out = match_altitudes_for_route(
        [seg_ab, seg_bc], {"north": [bisector_arrow]}
    )
    # The arrow may legitimately match one of the legs via the standard
    # gates (it's parallel-ish and in radius), but the rescue itself
    # must NOT fire — i.e. it must not write the same altitude to both
    # legs when one of them is otherwise unknown.
    assert not (out[0] == (1500,) and out[1] == (1500,) and out[0] == out[1])


def test_bend_rescue_skips_when_bisector_diff_too_large():
    """Arrow bearing well off the bisector — looks like a one-leg arrow
    that just happens to be near the bend. The bisector signature is
    geometrically precise (a designer drawing one arrow for a bend
    aligns its body to the bisector almost exactly), so an arrow more
    than 15° off-bisector is signaling something else and must not
    trigger the rescue."""
    seg_ab, seg_bc = _two_leg_route(
        "A", 32.146, 34.778,
        "B", 32.141, 34.801,
        "C", 32.179, 34.834,
    )
    # Bisector is ~70°; this arrow points ~40°, way off.
    off_bisector_arrow = GeoAltitudeArrow(
        lat=32.148, lon=34.782, bearing_deg=40.0, altitudes_ft=(1200,),
    )
    out = match_altitudes_for_route(
        [seg_ab, seg_bc], {"north": [off_bisector_arrow]}
    )
    assert out == [(), ()]


def test_bend_rescue_skips_when_arrow_too_far_from_both_legs():
    """An arrow whose nearest perpendicular distance to either leg is
    well past ``MATCH_BEND_RESCUE_MAX_LEG_DIST_NM`` doesn't belong to
    this bend's corridor, even if its bearing matches the bisector.
    Limits the rescue's geographic reach so it can't grab a bisector-
    aligned arrow from a neighbouring chart route."""
    seg_ab, seg_bc = _two_leg_route(
        "A", 32.146, 34.778,
        "B", 32.141, 34.801,
        "C", 32.179, 34.834,
    )
    cos_lat = math.cos(math.radians(32.16))
    far_bisector_arrow = GeoAltitudeArrow(
        # Bearing matches bisector, but position is ~1.5 nm from any
        # leg of the route — well past the 0.5 nm gate.
        lat=32.16,
        lon=34.778 - (1.5 / 60.0) / cos_lat,
        bearing_deg=71.4,
        altitudes_ft=(1200,),
    )
    out = match_altitudes_for_route(
        [seg_ab, seg_bc], {"north": [far_bisector_arrow]}
    )
    assert out == [(), ()]


def test_bend_rescue_skips_when_intermediate_leg_involved():
    """The rescue only fires on real-waypoint legs (both endpoints
    have a backing ``WaypointRecord``). Legs touching a free-clicked
    intermediate use the loose intermediate radius already and live
    on the other side of the matcher's precision spectrum — letting
    the rescue cross that boundary would smear corridor altitudes
    through user-clicked sub-leg chains (canonical risk: the NAAMA→
    3153N03531E → 3150N03532E → 3147N03530E chain in the LLIB→LLMZ
    regression, where each sub-leg is correctly unknown). Verify by
    making the shared point a free-clicked intermediate (waypoint=None)
    and confirming the rescue declines even with a perfect arrow."""
    # Construct two legs where the shared point B is a free-clicked
    # intermediate (waypoint=None), not a real waypoint. Use the same
    # HTZUK-KNTRY-LLHZ geometry otherwise so the arrow geometry is a
    # known rescue candidate in the real-waypoint variant.
    a_wp = _wp("A", 32.146, 34.778)
    c_wp = _wp("C", 32.179, 34.834)
    a_pt = RoutePoint(lat=a_wp.lat, lon=a_wp.lon, waypoint=a_wp)
    b_pt = RoutePoint(
        lat=32.141, lon=34.801, waypoint=None,  # intermediate
    )
    c_pt = RoutePoint(lat=c_wp.lat, lon=c_wp.lon, waypoint=c_wp)

    from cvfr_routemaster.route import (
        great_circle_distance_nm, magnetic_bearing_deg,
    )

    seg_ab = RouteSegment(
        from_point=a_pt, to_point=b_pt, from_label="A", to_label="3208N03448E",
        distance_nm=great_circle_distance_nm(a_pt.lat, a_pt.lon, b_pt.lat, b_pt.lon),
        mag_bearing_deg=magnetic_bearing_deg(a_pt.lat, a_pt.lon, b_pt.lat, b_pt.lon),
    )
    seg_bc = RouteSegment(
        from_point=b_pt, to_point=c_pt, from_label="3208N03448E", to_label="C",
        distance_nm=great_circle_distance_nm(b_pt.lat, b_pt.lon, c_pt.lat, c_pt.lon),
        mag_bearing_deg=magnetic_bearing_deg(b_pt.lat, b_pt.lon, c_pt.lat, c_pt.lon),
    )
    bend_arrow = GeoAltitudeArrow(
        lat=32.148, lon=34.782, bearing_deg=71.4, altitudes_ft=(1200,),
    )
    out = match_altitudes_for_route([seg_ab, seg_bc], {"north": [bend_arrow]})
    assert out == [(), ()]


def test_bend_rescue_does_not_fire_on_solitary_segment():
    """A single-segment route has no consecutive-leg pair, so the bend
    rescue has nothing to act on. Verifies the rescue handles the
    boundary case gracefully (no IndexError, no spurious matches)."""
    seg = _segment("A", 32.146, 34.778, "B", 32.141, 34.801)
    bend_arrow = GeoAltitudeArrow(
        lat=32.148, lon=34.782, bearing_deg=71.4, altitudes_ft=(1200,),
    )
    out = match_altitudes_for_route([seg], {"north": [bend_arrow]})
    # Single segment: with fwd-diff 32.2° vs segment bearing 104°,
    # the standard parallel tolerance (30°) still rejects this arrow.
    # The rescue requires a *pair* of consecutive legs, so it also
    # can't fire. Expected: unknown.
    assert out == [()]


def test_bend_rescue_constants_are_in_safe_band():
    """Pin the rescue's three threshold constants so a future drive-by
    re-tuning has to come through this test."""
    assert math.isclose(MATCH_BEND_RESCUE_MIN_BEND_DEG, 30.0)
    assert math.isclose(MATCH_BEND_RESCUE_BISECTOR_TOL_DEG, 15.0)
    assert math.isclose(MATCH_BEND_RESCUE_MAX_LEG_DIST_NM, 0.5)


# ---------------------------------------------------------------------------
# Wide-corridor rescue (phase 4) — admits parallel-right corridor labels
# beyond the strict primary radius for legs that came back unknown. Sized
# for the HRTZ coastal southbound corridor where the SB 800 ft arrow
# column sits 1.0–1.8 nm west of the user's waypoint chain.
# ---------------------------------------------------------------------------


def _real_seg_north(
    from_lat: float = 32.0,
    to_lat: float = 32.2,
    lon: float = 35.0,
) -> RouteSegment:
    """A northbound real-waypoint segment along a fixed meridian. The
    matcher's wide-corridor rescue only considers real-waypoint legs,
    so most rescue tests want this rather than ``_intermediate_segment``."""
    return _segment("A", from_lat, lon, "B", to_lat, lon)


def test_wide_corridor_rescue_admits_far_parallel_right_on_unknown_leg():
    """The canonical happy path: an arrow 1.5 nm east of a northbound
    real-waypoint segment, perfectly parallel, on the right side, foot
    on segment — outside the strict 0.65 nm primary radius but inside
    the 1.8 nm rescue radius. Primary returns unknown; rescue picks it
    up and the result carries the arrow's altitude."""
    seg = _real_seg_north()
    # 1.5 nm east at lat 32 ≈ 0.0295° lon
    arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.0 + 1.5 / 50.83, bearing_deg=0.0,
        altitudes_ft=(800,),
    )
    out = match_altitudes_for_route([seg], {"north": [arrow]})
    assert out[0] == (800,)


def test_wide_corridor_rescue_loosens_bearing_tolerance_only_to_20_deg():
    """The rescue uses ``MATCH_WIDE_CORRIDOR_FWD_DIFF_DEG`` (20°) — wider
    than nothing but tighter than the primary 30°. Pin both sides of
    that boundary: 15° in, 25° out."""
    seg = _real_seg_north()
    in_tol = GeoAltitudeArrow(
        lat=32.1, lon=35.0 + 1.5 / 50.83, bearing_deg=15.0,
        altitudes_ft=(800,),
    )
    out_tol = GeoAltitudeArrow(
        lat=32.1, lon=35.0 + 1.5 / 50.83, bearing_deg=25.0,
        altitudes_ft=(900,),
    )
    assert match_altitudes_for_route(
        [seg], {"north": [in_tol]}
    )[0] == (800,)
    assert match_altitudes_for_route(
        [seg], {"north": [out_tol]}
    )[0] == ()


def test_wide_corridor_rescue_rejects_parallel_left_arrows():
    """The rescue is gated to parallel-right arrows only — wide-corridor
    chart layouts overwhelmingly print labels on the right of the
    direction of flow. An arrow 1.5 nm to the LEFT of a northbound leg
    is more likely a neighbouring corridor's label than ours."""
    seg = _real_seg_north()
    left_arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.0 - 1.5 / 50.83, bearing_deg=0.0,
        altitudes_ft=(800,),
    )
    assert match_altitudes_for_route(
        [seg], {"north": [left_arrow]}
    )[0] == ()


def test_wide_corridor_rescue_rejects_arrow_beyond_extended_radius():
    """Past 1.8 nm cross-track the rescue stops admitting too. A
    parallel-right, perfectly aligned arrow at 2.5 nm fails the
    extended radius gate just like the primary gate."""
    seg = _real_seg_north()
    far_arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.0 + 2.5 / 50.83, bearing_deg=0.0,
        altitudes_ft=(800,),
    )
    assert match_altitudes_for_route(
        [seg], {"north": [far_arrow]}
    )[0] == ()


def test_wide_corridor_rescue_rejects_arrow_past_segment_endpoint():
    """The rescue requires the arrow's perpendicular foot to lie on
    segment (no endpoint overshoot). An arrow north of the TO endpoint,
    parallel-right of the northbound direction, would otherwise look
    geometrically inviting at 1.0 nm endpoint-clamped distance — but
    the chart convention says it labels whatever continues from the TO
    endpoint, not OUR leg."""
    seg = _real_seg_north()
    past_arrow = GeoAltitudeArrow(
        lat=32.3, lon=35.0 + 1.0 / 50.83, bearing_deg=0.0,
        altitudes_ft=(800,),
    )
    assert match_altitudes_for_route(
        [seg], {"north": [past_arrow]}
    )[0] == ()


def test_wide_corridor_rescue_does_not_fire_on_intermediate_legs():
    """The rescue is restricted to real-waypoint segments. Free-clicked
    intermediate legs (no ``waypoint`` record on either endpoint) get
    the loose primary radius already; admitting them to the rescue
    too would smear corridor labels across long chains of user-clicked
    points without bound."""
    inter = _intermediate_segment("I1", 32.0, 35.0, "I2", 32.2, 35.0)
    far_arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.0 + 1.6 / 50.83, bearing_deg=0.0,
        altitudes_ft=(800,),
    )
    # Past the intermediate's 1.30 nm loose radius but inside the
    # rescue's 1.8 nm radius. The intermediate's loose primary doesn't
    # reach it, and the rescue refuses to (intermediate gate).
    out = match_altitudes_for_route([inter], {"north": [far_arrow]})
    assert out[0] == ()


def test_wide_corridor_rescue_skips_when_leg_already_matched():
    """If a leg already has a primary match, the rescue is a no-op —
    it never overwrites an existing verdict. Setup: an on-strict-radius
    arrow that primary matches, plus a wider-radius arrow with a
    different altitude that the rescue *could* have admitted. The
    second arrow stays unclaimed and the result is the primary's."""
    seg = _real_seg_north()
    close_arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.0 + 0.3 / 50.83, bearing_deg=0.0,
        altitudes_ft=(2000,),
    )
    far_arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.0 + 1.5 / 50.83, bearing_deg=0.0,
        altitudes_ft=(800,),
    )
    out = match_altitudes_for_route(
        [seg], {"north": [close_arrow, far_arrow]}
    )
    # Primary captured close_arrow (2000); rescue can't overwrite. The
    # far_arrow's 800 doesn't get stacked because it's well outside
    # stack_radius_nm from the primary, so the result is just 2000.
    assert out[0] == (2000,)


def test_wide_corridor_rescue_skips_arrow_already_claimed_elsewhere():
    """An arrow that primary matched onto leg X can't get re-attributed
    to leg Y by the rescue. Setup: two adjacent legs sharing a
    waypoint, where the arrow's primary fit is on leg A; leg B is
    unknown and the same arrow would be a rescue candidate for B.
    Expected: A keeps the match, B stays unknown."""
    # Leg A: same northbound geometry as the canonical happy-path test.
    # Leg B: continues from A's TO point further north.
    seg_a = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)
    seg_b = _segment("B", 32.2, 35.0, "C", 32.4, 35.0)
    # Arrow that primary-matches A (close to A's line) and would also
    # qualify as a wide-corridor rescue candidate for B if it weren't
    # already claimed (still parallel-right of B's direction, on B's
    # segment line, but well past A's strict radius — sits 0.3 nm
    # east of the meridian at lat 32.1, right between A and B).
    arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.0 + 0.3 / 50.83, bearing_deg=0.0,
        altitudes_ft=(2500,),
    )
    out = match_altitudes_for_route([seg_a, seg_b], {"north": [arrow]})
    assert out[0] == (2500,)
    assert out[1] == ()


def test_wide_corridor_rescue_competition_picks_tightest_segment():
    """When a single arrow geometrically qualifies for multiple unknown
    legs, the rescue assigns it to the leg it fits best (lowest cross-
    track). Setup: an arrow placed so it's a rescue candidate for two
    adjacent unknown legs; the closer one gets the match, the other
    stays unknown.

    Two adjacent legs on parallel meridians; arrow positioned between
    them but slightly closer to the second."""
    seg_a = _segment("A", 32.0, 34.99, "B", 32.2, 34.99)  # west meridian
    seg_b = _segment("B2", 32.0, 35.01, "C", 32.2, 35.01)  # east meridian
    # Arrow sits 1.2 nm east of A's line (in radius for A's rescue) and
    # 0.6 nm west of B's line — primary catches B at 0.6 nm strict.
    arrow = GeoAltitudeArrow(
        lat=32.1, lon=34.99 + 1.2 / 50.83, bearing_deg=0.0,
        altitudes_ft=(1500,),
    )
    out = match_altitudes_for_route(
        [seg_a, seg_b], {"north": [arrow]},
    )
    # Primary on B wins outright; rescue on A doesn't get the arrow.
    assert out[0] == ()
    assert out[1] == (1500,)


def test_wide_corridor_rescue_constants_are_in_safe_band():
    """Pin the rescue's two threshold constants so a future drive-by
    re-tuning has to come through this test."""
    assert math.isclose(MATCH_WIDE_CORRIDOR_RADIUS_NM, 1.8)
    assert math.isclose(MATCH_WIDE_CORRIDOR_FWD_DIFF_DEG, 20.0)


def test_matcher_returns_primary_band_first_then_stacked_alternates():
    """Multiple parallel right-side arrows are *all* valid altitude options
    on the chart — ATC may clear any of them depending on the situation,
    so the matcher concatenates them in proximity order: the primary's
    band first (closest to the line), then alternates ordered by distance
    from the primary."""
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)
    closer_right = GeoAltitudeArrow(
        lat=32.1, lon=35.001, bearing_deg=0.0, altitudes_ft=(1600, 800),
    )
    farther_right = GeoAltitudeArrow(
        lat=32.1, lon=35.005, bearing_deg=0.0, altitudes_ft=(2000,),
    )
    out = match_altitudes_for_segment(seg, {"north": [farther_right, closer_right]})
    # Primary band (1600, 800), then the next arrow's altitude appended.
    assert out == (1600, 800, 2000)


def test_matcher_returns_only_primary_when_alternate_is_outside_stack_radius():
    """Two parallel arrows on the same long segment but far enough apart
    along-track aren't side-by-side stacked alternates — they're two
    different chart labels printed at different points along the route.
    Stacking sweeps within ``MATCH_STACK_RADIUS_NM`` of the primary, so
    a far-along-track second arrow doesn't merge into the primary's
    output."""
    seg = _segment("A", 32.0, 35.0, "B", 32.5, 35.0)  # ~30 nm long
    primary = GeoAltitudeArrow(
        lat=32.1, lon=35.001, bearing_deg=0.0, altitudes_ft=(1500,),
    )
    # Same lon, 0.013° further north. Cross-track ~0.05 nm (passes
    # radius), but 0.78 nm from the primary (past stack_radius=0.55).
    far_alt = GeoAltitudeArrow(
        lat=32.113, lon=35.001, bearing_deg=0.0, altitudes_ft=(2000,),
    )
    out = match_altitudes_for_segment(seg, {"north": [primary, far_alt]})
    assert out == (1500,)


def test_matcher_does_not_stack_left_side_arrow_with_right_side_primary():
    """Chart convention puts OUR-direction arrows on a single side of the
    route line. A parallel arrow on the *opposite* side, even within the
    stack radius, is a different chart label (most likely the opposite-
    direction leg's label) and must not contaminate our stack."""
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)  # northbound
    right_primary = GeoAltitudeArrow(
        lat=32.1, lon=35.001, bearing_deg=0.0, altitudes_ft=(1500,),
    )
    # Left of segment (lon < 35.0), parallel, well within stack radius.
    left_alt = GeoAltitudeArrow(
        lat=32.1, lon=34.999, bearing_deg=0.0, altitudes_ft=(2000,),
    )
    out = match_altitudes_for_segment(seg, {"north": [right_primary, left_alt]})
    assert out == (1500,)


def test_matcher_does_not_stack_arrow_owned_by_a_different_segment():
    """When two segments compete for the same arrow (e.g. an arrow near
    the shared waypoint between adjacent legs), the loser must NOT
    secretly inherit the winner's arrow as a stack alternate."""
    seg_a = _segment("A", 32.00, 35.0, "B", 32.10, 35.0)  # northbound, 6 nm
    seg_b = _segment("B", 32.10, 35.0, "C", 32.20, 35.0)  # northbound, 6 nm
    # Arrow sits very close to seg_b's centre (best fit) but is also
    # within seg_a's tube near its TO endpoint.
    contested = GeoAltitudeArrow(
        lat=32.150, lon=35.001, bearing_deg=0.0, altitudes_ft=(2000,),
    )
    # Each segment also has its own clear primary so the competition has
    # somewhere to land.
    seg_a_primary = GeoAltitudeArrow(
        lat=32.050, lon=35.001, bearing_deg=0.0, altitudes_ft=(1500,),
    )
    out = match_altitudes_for_route(
        [seg_a, seg_b],
        {"north": [seg_a_primary, contested]},
    )
    # contested goes to seg_b (closer to its centre); seg_a only gets
    # its own primary; the contested altitude must NOT leak into seg_a's
    # output via stacking.
    assert out[0] == (1500,)
    assert out[1] == (2000,)


def test_matcher_route_level_competitive_matching_kills_cross_leg_false_positive():
    """The inverse-route bug: an arrow that legitimately labels DAROM→HOTRM
    (south-bound, near DAROM) was getting attributed to the previous
    segment 3250N03457E→DAROM because that segment's tube also reaches
    the arrow near its TO endpoint. Competitive matching gives the arrow
    to whichever segment fits it best (lower fwd-diff and cross-track
    distance), so the previous-leg false positive disappears."""
    # Two southbound segments meeting at "DAROM" (32.78, 34.93).
    seg_prev = _segment(
        "X", 32.833, 34.950,  # 3250N03457E
        "DAROM", 32.78, 34.93,
    )
    seg_next = _segment(
        "DAROM", 32.78, 34.93,
        "HOTRM", 32.74, 34.93,
    )
    # Arrow sits just south of DAROM, parallel to both segments. It's
    # closer to seg_next (its true label) and more strictly parallel to
    # it (seg_prev bears ~189°, seg_next bears ~180°, arrow bears 184°).
    arrow = GeoAltitudeArrow(
        lat=32.770, lon=34.928, bearing_deg=184.0, altitudes_ft=(2000,),
    )
    out = match_altitudes_for_route([seg_prev, seg_next], {"north": [arrow]})
    assert out[0] == ()      # seg_prev: unknown — its alternate is gone
    assert out[1] == (2000,)  # seg_next: gets its true label


def test_matcher_route_level_stacks_side_by_side_alternates():
    """The BAZRA→DEROR case: two yellow arrows printed parallel and
    side-by-side, each carrying a single altitude (1600 next to 800).
    The matcher must surface both as the segment's altitude options."""
    seg = _segment(
        "BAZRA", 32.205, 34.886,
        "DEROR", 32.265, 34.898,
    )
    # 1600 closer to the route line, 800 a bit further east — both
    # parallel-right of the northbound leg.
    arrow_1600 = GeoAltitudeArrow(
        lat=32.226, lon=34.891, bearing_deg=6.7, altitudes_ft=(1600,),
    )
    arrow_800 = GeoAltitudeArrow(
        lat=32.225, lon=34.898, bearing_deg=6.7, altitudes_ft=(800,),
    )
    out = match_altitudes_for_route([seg], {"north": [arrow_1600, arrow_800]})
    # 1600 is the primary (closer); 800 stacks in via stack-from-primary
    # even though its own cross-track distance is past the segment radius.
    assert out[0] == (1600, 800)


def test_matcher_route_level_stacks_alternates_outside_segment_radius():
    """Stacked alternates ride on the primary's anchor, not the segment's
    cross-track radius — a side-by-side arrow whose own cross-track
    distance exceeds ``MATCH_RADIUS_NM`` still merges in if it sits
    within ``MATCH_STACK_RADIUS_NM`` of the primary.

    At 32°N, 1° of longitude ≈ 50.9 nm, so:
      - primary at lon=35.008 → cross-track 0.41 nm (passes 0.65 radius)
      - alternate at lon=35.018 → cross-track 0.92 nm (fails radius)
      - alternate-to-primary distance ≈ 0.51 nm (passes 0.55 stack)
    """
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)  # northbound
    primary = GeoAltitudeArrow(
        lat=32.1, lon=35.008, bearing_deg=0.0, altitudes_ft=(2000,),
    )
    alternate = GeoAltitudeArrow(
        lat=32.1, lon=35.018, bearing_deg=0.0, altitudes_ft=(1000,),
    )
    out = match_altitudes_for_route([seg], {"north": [primary, alternate]})
    assert out[0] == (2000, 1000)


def test_matcher_route_level_returns_empty_when_no_segments():
    """Edge case: no segments → no matches."""
    assert match_altitudes_for_route([], {"north": [], "south": []}) == []


# ---------------------------------------------------------------------------
# Per-segment radius (real-waypoint vs intermediate)
# ---------------------------------------------------------------------------


def _intermediate_segment(
    from_label: str, from_lat: float, from_lon: float,
    to_label: str, to_lat: float, to_lon: float,
) -> RouteSegment:
    """Build a segment whose endpoints are both free-clicked intermediates
    (``RoutePoint.waypoint=None``). Mirrors how the GUI represents a leg
    between two intermediate clicks — drives the matcher into using the
    loose ``MATCH_RADIUS_NM_INTERMEDIATE`` radius gate.
    """
    fp = RoutePoint(lat=from_lat, lon=from_lon, waypoint=None)
    tp = RoutePoint(lat=to_lat, lon=to_lon, waypoint=None)
    from cvfr_routemaster.route import great_circle_distance_nm, magnetic_bearing_deg
    return RouteSegment(
        from_point=fp,
        to_point=tp,
        from_label=from_label,
        to_label=to_label,
        distance_nm=great_circle_distance_nm(from_lat, from_lon, to_lat, to_lon),
        mag_bearing_deg=magnetic_bearing_deg(from_lat, from_lon, to_lat, to_lon),
    )


def test_matcher_intermediate_leg_uses_loose_radius_to_catch_far_arrow():
    """An arrow at ~1.0 nm cross-track should match an intermediate-only
    leg (loose 1.30 nm radius) but not a real-waypoint leg's *primary*
    pass (strict 0.65 nm radius). The fix this pins ensures the
    GALIM.1↔GALIM.2 case from the LLHA→LLHZ inverse route returns its
    (2000, 1000) tuple instead of ``unknown``.

    The wide-corridor rescue (a later post-pass with a 1.8 nm extended
    radius for parallel-right corridor labels) would otherwise let the
    real-waypoint leg catch the arrow too — this test disables that
    pass via ``wide_corridor_radius_nm=0.0`` so the assertion isolates
    the primary-vs-intermediate radius distinction. The combined
    real-waypoint-plus-rescue behaviour is pinned separately in
    ``test_wide_corridor_rescue_*`` below.

    At 32°N, 1° lon ≈ 50.9 nm. Place a parallel-right arrow at lon
    offset 0.020° (≈1.0 nm cross-track) — outside strict but inside
    loose.
    """
    # Real-waypoint segment: strict radius — arrow at 1.0 nm rejected
    # by primary, rescue disabled here.
    real_seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)  # northbound
    arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.020, bearing_deg=0.0, altitudes_ft=(2000,),
    )
    real_out = match_altitudes_for_route(
        [real_seg], {"north": [arrow]}, wide_corridor_radius_nm=0.0,
    )
    assert real_out[0] == (), (
        "real-waypoint segment must reject arrows beyond strict radius "
        "in the primary pass"
    )

    # Intermediate-only segment with the same geometry: loose radius
    # accepts the same arrow.
    inter_seg = _intermediate_segment(
        "I1", 32.0, 35.0, "I2", 32.2, 35.0,  # same northbound geometry
    )
    inter_out = match_altitudes_for_route(
        [inter_seg], {"north": [arrow]}, wide_corridor_radius_nm=0.0,
    )
    assert inter_out[0] == (2000,), (
        "intermediate segment must accept the same arrow under loose radius"
    )


def test_matcher_real_waypoint_reclaims_stacked_alternate_from_intermediate():
    """An arrow that's a stacked alternate for a real-waypoint leg must
    not be siphoned off by an adjacent intermediate leg whose loose
    radius reaches it.

    Setup mirrors the inverse-route bug: real-waypoint leg DAROM→HOTRM
    has a primary arrow within strict radius and a stacked alternate
    just outside it. An adjacent intermediate leg also reaches the
    alternate via its loose radius. Without the reclaim phase, the
    intermediate would steal the alternate, leaving DAROM→HOTRM with
    only the primary altitude.
    """
    # Real-waypoint leg (northbound). Primary arrow at cross-track ~0.4 nm
    # (inside strict 0.65), stacked alternate at ~0.92 nm (outside
    # strict but inside loose). Both parallel, both right-of-track.
    real_leg = _segment("DAROM", 32.0, 35.0, "HOTRM", 32.2, 35.0)
    primary = GeoAltitudeArrow(
        lat=32.1, lon=35.008, bearing_deg=0.0, altitudes_ft=(2000,),
    )
    alternate = GeoAltitudeArrow(
        lat=32.1, lon=35.018, bearing_deg=0.0, altitudes_ft=(1000,),
    )

    # Adjacent intermediate leg, oriented so its segment line passes
    # close enough to the alternate that its loose radius would steal
    # it without the reclaim phase. We position the intermediate leg
    # north-east of the real leg so it overlaps the alternate's
    # geographic neighbourhood.
    inter_leg = _intermediate_segment(
        "I1", 32.18, 35.030, "I2", 32.22, 35.020,
    )

    out = match_altitudes_for_route(
        [real_leg, inter_leg], {"north": [primary, alternate]},
    )
    # Real-waypoint leg keeps both primary and alternate; intermediate
    # leg gets nothing (it shouldn't claim a precise leg's stacked
    # alternate).
    assert out[0] == (2000, 1000), (
        f"real-waypoint leg should keep both primary and stacked alternate; "
        f"got {out[0]!r}"
    )
    assert out[1] == (), (
        f"intermediate leg should not steal the real-waypoint leg's "
        f"stacked alternate; got {out[1]!r}"
    )


def test_matcher_intermediate_reclaim_does_not_block_legitimate_intermediate_match():
    """The reclaim phase should only fire when a real-waypoint leg has
    a primary near the contested arrow. An intermediate leg with no
    real-waypoint competitor must still get its arrow under the
    loose radius — this is the GALIM.1↔GALIM.2 case where no
    real-waypoint segment is anywhere near the relevant arrows."""
    # Two intermediates only — no real-waypoint legs in the route.
    inter_leg = _intermediate_segment(
        "GALIM.1", 32.0, 35.0, "GALIM.2", 32.2, 35.0,
    )
    arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.020, bearing_deg=0.0, altitudes_ft=(2000, 1000),
    )
    out = match_altitudes_for_route([inter_leg], {"north": [arrow]})
    assert out[0] == (2000, 1000)


def test_matcher_route_level_returns_empty_tuples_when_no_arrows():
    """Edge case: no arrows for any sheet → every segment is unknown."""
    seg_a = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)
    seg_b = _segment("B", 32.2, 35.0, "C", 32.4, 35.0)
    assert match_altitudes_for_route(
        [seg_a, seg_b], {"north": [], "south": []},
    ) == [(), ()]


def test_matcher_searches_both_sheets():
    """The matcher iterates both north and south arrow lists transparently —
    a south-sheet arrow can match a route segment when only south is
    calibrated."""
    seg = _segment("A", 31.0, 35.0, "B", 31.2, 35.0)
    n_arrow = GeoAltitudeArrow(
        lat=33.0, lon=35.001, bearing_deg=0.0, altitudes_ft=(2000,),
    )
    s_arrow = GeoAltitudeArrow(
        lat=31.1, lon=35.001, bearing_deg=10.0, altitudes_ft=(1500,),
    )
    out = match_altitudes_for_segment(seg, {"north": [n_arrow], "south": [s_arrow]})
    assert out == (1500,)


def test_matcher_returns_empty_tuple_for_no_match():
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)
    assert match_altitudes_for_segment(seg, {"north": [], "south": []}) == ()


def test_matcher_handles_diagonal_segments_with_correct_sidedness():
    """A NE-going segment (heading ~45°) has its right-of-track to the SE.
    An arrow placed SE of the segment (right side) and parallel to its
    heading should win, even with a closer parallel arrow NW of it."""
    seg = _segment("A", 32.0, 35.0, "B", 32.1, 35.1)  # NE-bound
    # Right of NE = SE of the line. Place arrow slightly SE of midpoint.
    # Cross-track ≈ 0.003·√2 ≈ 0.4 nm — inside the 0.65 nm radius.
    right_arrow = GeoAltitudeArrow(
        lat=32.047, lon=35.053, bearing_deg=45.0, altitudes_ft=(1500,),
    )
    # Left of NE = NW of the line. Closer to the line than right_arrow but
    # on the wrong (left) side. Same heading (parallel) so the right-side
    # preference is what disambiguates.
    left_arrow = GeoAltitudeArrow(
        lat=32.052, lon=35.048, bearing_deg=45.0, altitudes_ft=(2000,),
    )
    out = match_altitudes_for_segment(seg, {"north": [left_arrow, right_arrow]})
    assert out == (1500,)


def test_matcher_accepts_bidirectional_arrow_for_either_direction():
    """A dual-headed arrow's altitude applies regardless of the segment's
    travel direction — the matcher must accept it for both FROM→TO and
    TO→FROM segments. Bidirectional arrows are identified by the
    ``bidirectional`` flag (the bearing is a placeholder)."""
    seg_n = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)  # northbound
    seg_s = _segment("B", 32.2, 35.0, "A", 32.0, 35.0)  # southbound
    arrow = GeoAltitudeArrow(
        lat=32.1, lon=35.0, bearing_deg=0.0, altitudes_ft=(1200,),
        bidirectional=True,
    )
    assert match_altitudes_for_segment(seg_n, {"north": [arrow]}) == (1200,)
    assert match_altitudes_for_segment(seg_s, {"north": [arrow]}) == (1200,)


def test_matcher_prefers_directional_arrow_over_bidirectional():
    """When both a directional right-side arrow and a bidirectional arrow
    are nearby, the directional one wins — it carries the more specific
    direction signal, so it's the chart's labelling for OUR direction."""
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)
    bi = GeoAltitudeArrow(
        lat=32.1, lon=35.0, bearing_deg=0.0, altitudes_ft=(1200,),
        bidirectional=True,
    )
    directional = GeoAltitudeArrow(
        lat=32.1, lon=35.001, bearing_deg=0.0, altitudes_ft=(2000,),
        bidirectional=False,
    )
    out = match_altitudes_for_segment(seg, {"north": [bi, directional]})
    assert out == (2000,)


def test_matcher_rejects_bidirectional_arrow_outside_radius():
    """The radius gate still applies to bidirectional arrows — proximity
    is the precondition for any match."""
    seg = _segment("A", 32.0, 35.0, "B", 32.2, 35.0)
    far_lon = 35.0 + 1.0 / 60.0  # ~0.85 nm east at 32°N, past the 0.5 nm gate
    arrow = GeoAltitudeArrow(
        lat=32.1, lon=far_lon, bearing_deg=0.0, altitudes_ft=(1200,),
        bidirectional=True,
    )
    assert match_altitudes_for_segment(seg, {"north": [arrow]}) == ()


def test_match_thresholds_have_sane_defaults():
    """Sanity-check our defaults — tighten/loosen consciously, not by accident.

    The 0.65 nm radius is calibrated to catch every legitimate match on
    the LLHZ→...→LLHA test route (some legs have their genuine arrow at
    0.55–0.6 nm) without admitting wrong-direction arrows. The 30°
    parallel tolerance is tight enough to stop arrows from one leg
    leaking into an adjacent leg whenever the route kinks by more than
    30° — empirically every correct match on the LLHZ test route sat
    below 16° fwd-diff, while every wrong match sat in the 39–41° range,
    so 30° cleanly separates the populations.

    The stacking constants are sized for two-arrow CTR-exit clusters
    (BAZRA→DEROR's 1600 next to 800 sits ~0.4 nm apart; DAROM→HOTRM's
    2000 next to 1000 sits ~0.5 nm apart): 0.55 nm covers both with
    comfortable headroom but stops short of an unrelated parallel
    route's arrow. 15° of parallel-bearing wiggle is generous for
    extraction noise yet far below the 60° perpendicular zone.
    """
    assert MATCH_RADIUS_NM == pytest.approx(0.65)
    assert MATCH_PARALLEL_TOL_DEG == pytest.approx(30.0)
    assert MATCH_STACK_RADIUS_NM == pytest.approx(0.55)
    assert MATCH_STACK_BEARING_TOL_DEG == pytest.approx(15.0)
    # The fwd-diff weight in the per-arrow score: 0.01 nm/° means a 25°
    # spread between two segments' fwd-diff is worth 0.25 nm of cross-
    # track distance — calibrated against the inverse-route diagnostic.
    assert MATCH_FWD_DIFF_SCORE_WEIGHT == pytest.approx(0.01)
    # The looser radius applies only to legs with at least one free-
    # clicked intermediate. 1.30 nm covers the worst-case ICAO-rounding
    # diagonal (one arc-minute = 1 nm; rounded coord can be ~0.7 nm
    # off the actual click) plus headroom; tighter than that misses
    # legitimate matches like GALIM.1↔GALIM.2 in the LLHA→LLHZ
    # inverse route, looser risks pulling in a parallel-route arrow
    # from a leg the user didn't add to their plan.
    assert MATCH_RADIUS_NM_INTERMEDIATE == pytest.approx(1.30)
    assert MATCH_RADIUS_NM_INTERMEDIATE > MATCH_RADIUS_NM, (
        "intermediate radius must be looser than the strict real-waypoint "
        "radius — that's the whole point of the per-segment selection"
    )


# ---------------------------------------------------------------------------
# Integration smoke against the real chart PDFs (if present)
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _pdf_or_skip(name: str) -> Path:
    p = _project_root() / name
    if not p.is_file():
        pytest.skip(f"chart PDF not present: {name}")
    return p


def test_extract_north_chart_returns_plausible_population():
    """End-to-end on the real north PDF: the harvest should be in the
    hundreds of arrows, dominated by canonical CVFR altitudes."""
    pdf = _pdf_or_skip("CVFR-NORTH-OCT-2025-UPD2.pdf")
    # An identity crop covering the whole rendered pixmap. With 288 DPI,
    # the pixmap is roughly 7937 × 11339 px — but we don't actually need
    # the cropped UV to be valid for the test, just the count and altitude
    # distribution. Use generous source/cropped dims so the projection
    # bounds-check accepts everything.
    crop = CropMeta(
        offset_x=0, offset_y=0,
        source_w=20000, source_h=20000,
        cropped_w=20000, cropped_h=20000,
    )
    arrows = extract_altitude_arrows(pdf, render_dpi=288.0, crop=crop)

    # Hundreds of arrows on the north chart; well above any false-positive
    # noise floor and well below "every yellow blob with a number".
    assert 200 <= len(arrows) <= 2000, len(arrows)

    # All harvested altitudes must pass the plausibility filter — that's
    # the contract _is_plausible_altitude is supposed to guarantee.
    for a in arrows:
        for v in a.altitudes_ft:
            assert _is_plausible_altitude(str(v)) == v

    # Sanity: the most-common altitudes are the canonical CVFR values.
    from collections import Counter

    flat = [v for a in arrows for v in a.altitudes_ft]
    top = {v for v, _ in Counter(flat).most_common(8)}
    expected = {800, 1000, 1200, 1500, 1600, 2000, 2500, 3000, 3500, 4000}
    assert len(top & expected) >= 5, top


def test_extract_south_chart_returns_plausible_population():
    pdf = _pdf_or_skip("CVFR-SOUTH-OCT-2025-UPD2.pdf")
    crop = CropMeta(
        offset_x=0, offset_y=0,
        source_w=20000, source_h=20000,
        cropped_w=20000, cropped_h=20000,
    )
    arrows = extract_altitude_arrows(pdf, render_dpi=288.0, crop=crop)
    assert 100 <= len(arrows) <= 2000, len(arrows)
    for a in arrows:
        for v in a.altitudes_ft:
            assert _is_plausible_altitude(str(v)) == v


def test_extract_returns_arrows_with_sorted_altitude_tuples():
    """When an arrow has stacked numbers, they come out top-to-bottom on the
    chart. Top-to-bottom for stacked altitude bands == high-to-low (highest
    altitude on top is the printed convention), so the tuple must be
    monotonically non-increasing."""
    pdf = _pdf_or_skip("CVFR-NORTH-OCT-2025-UPD2.pdf")
    crop = CropMeta(
        offset_x=0, offset_y=0,
        source_w=20000, source_h=20000,
        cropped_w=20000, cropped_h=20000,
    )
    arrows = extract_altitude_arrows(pdf, render_dpi=288.0, crop=crop)
    stacked = [a for a in arrows if len(a.altitudes_ft) >= 2]
    assert stacked, "expected at least one stacked-altitude arrow on the north chart"
    # Most stacked arrows should be high-to-low in their tuple ordering.
    high_to_low = sum(
        1 for a in stacked if list(a.altitudes_ft) == sorted(a.altitudes_ft, reverse=True)
    )
    assert high_to_low / len(stacked) >= 0.7, (
        f"only {high_to_low}/{len(stacked)} stacked tuples are monotonically descending"
    )
