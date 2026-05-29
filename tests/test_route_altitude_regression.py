# CVFR Route Master — an Israel CVFR route-planning assistant
# for flight-simulator use.
# Copyright (C) 2026 Lev F.
#
# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU Affero General Public
# License as published by the Free Software Foundation, either
# version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with this program. If not, see
# <http://www.gnu.org/licenses/>.
#
# This program is intended for flight-simulator use only. The
# author disclaims any warranty of fitness for use in real-world
# aviation; any such use is entirely at the user's own risk and
# is not contemplated by this software. This program is not a
# substitute for official charts, NOTAMs, weather briefings, or
# any other official flight-planning material. Always cross-check
# against current AIP material before any simulated flight.

"""Regression tests for the route-altitude matcher against the user's
manually verified ground truth on the LLHZ↔LLHA round-trip.

These two tests are the *source-of-truth* gate for any future change to
the altitude-matching pipeline (arrow extraction, projection, the
matcher's per-segment radius / parallel tolerance / stacking rules,
etc.). The expected ``(altitudes_ft)`` tuple per segment was confirmed
by the user — pilot-level accuracy — by reading the actual chart
arrows for both directions of the route.

Test design choices:

* **Snapshot fixtures.** ``tests/fixtures/altitude_regression/`` carries
  a frozen copy of the four cache files that drive the matcher:
  the waypoint records, the per-sheet extracted altitude arrows
  (north + south), and the geo calibration. The live caches under
  ``.cvfr_routemaster/`` are deliberately *not* used — those move when
  the user re-extracts arrows or recalibrates, and the regression test
  must stay reproducible across runs.

* **No PDF fingerprint check.** ``try_load_altitude_arrows`` rejects a
  cache whose PDF mtime/size has shifted, which would defeat the
  purpose of bundled snapshot data. We bypass it by constructing
  ``AltitudeArrow`` instances directly from the JSON. Likewise we
  build ``SheetGeoCalibration`` via ``sheet_from_dict`` (no PDF
  fingerprint validation) so the fixtures don't depend on the PDFs
  being present.

* **From / To / Alt only.** Per user request, the assertion contract
  is the (from_label, to_label, altitudes_tuple) tuple per segment.
  Bearings, distances, and times are intentionally **not** locked in
  — they depend on minor coord choices that the user reserves the
  right to change without breaking these tests.

* **Real-waypoint vs intermediate plumbing.** Each ``RoutePoint`` is
  built with its actual ``WaypointRecord`` for ICAO 5-letter codes
  and ``None`` for ICAO-coord intermediates. This drives the
  matcher's per-segment radius selection (strict 0.65 nm for legs
  between official waypoints, loose 1.30 nm for legs involving a
  free-clicked intermediate). Without this, every segment would be
  treated as intermediate and the test would diverge from the GUI's
  actual behaviour.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cvfr_routemaster.altitude_arrows import (
    AltitudeArrow,
    match_altitudes_for_route,
    project_arrows_to_lonlat,
)
from cvfr_routemaster.geo_calibration import SheetGeoCalibration, sheet_from_dict
from cvfr_routemaster.route import (
    RoutePoint,
    RouteSegment,
    great_circle_distance_nm,
    magnetic_bearing_deg,
)
from cvfr_routemaster.waypoint_types import WaypointRecord


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "altitude_regression"


_ICAO_COORD_RE = re.compile(
    r"^(?P<lat_d>\d{2})(?P<lat_m>\d{2})(?P<lat_h>[NS])"
    r"(?P<lon_d>\d{3})(?P<lon_m>\d{2})(?P<lon_h>[EW])$"
)


def _parse_icao_coord(token: str) -> tuple[float, float] | None:
    """Decode an ICAO Field 15 ``DDMM[NS]DDDMM[EW]`` coord to (lat, lon).

    The strings used in these tests come straight from the user's
    plotted route; rounding to whole minutes is intentional so the
    test exercises the same coord-precision conditions the GUI sees
    when it formats an intermediate-point label.
    """
    m = _ICAO_COORD_RE.match(token)
    if m is None:
        return None
    lat = int(m["lat_d"]) + int(m["lat_m"]) / 60.0
    if m["lat_h"] == "S":
        lat = -lat
    lon = int(m["lon_d"]) + int(m["lon_m"]) / 60.0
    if m["lon_h"] == "W":
        lon = -lon
    return lat, lon


def _load_waypoints() -> dict[str, WaypointRecord]:
    """Load the snapshotted waypoint cache as a {code: WaypointRecord}
    dict. Each record carries lat/lon and the Hebrew name; only the
    coordinates are used by the regression matcher, but constructing
    the full record keeps the route-segment objects identical to the
    GUI's runtime shape."""
    raw = json.loads(
        (FIXTURES_DIR / "waypoints_cache.json").read_text(encoding="utf-8")
    )
    out: dict[str, WaypointRecord] = {}
    for item in raw.get("records", []):
        if not isinstance(item, dict):
            continue
        try:
            wp = WaypointRecord(
                index=int(item["index"]),
                code=str(item["code"]),
                name_he=str(item.get("name_he", "")),
                reporting_type=str(item.get("reporting_type", "")),
                lat=float(item["lat"]),
                lon=float(item["lon"]),
                lat_dms=str(item["lat_dms"]),
                lon_dms=str(item["lon_dms"]),
            )
            out[wp.code] = wp
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _load_arrows_for_sheet(sheet: str) -> list[AltitudeArrow]:
    """Build ``AltitudeArrow`` objects directly from the JSON snapshot.

    Bypasses ``try_load_altitude_arrows`` because that function gates on
    PDF mtime/size which would invalidate the bundled fixture. The
    schema is small enough that a tolerant load is safe — same code
    path as ``debug_route_altitudes.py`` uses for offline diagnosis.
    """
    raw = json.loads(
        (FIXTURES_DIR / f"altitude_arrows_{sheet}.json").read_text(
            encoding="utf-8"
        )
    )
    out: list[AltitudeArrow] = []
    for r in raw.get("arrows", []):
        if not isinstance(r, dict):
            continue
        try:
            alts = tuple(int(v) for v in r["altitudes_ft"])
            out.append(
                AltitudeArrow(
                    u=float(r["u"]),
                    v=float(r["v"]),
                    bearing_deg=float(r["bearing_deg"]),
                    altitudes_ft=alts,
                    bidirectional=bool(r.get("bidirectional", False)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _load_calibrations() -> dict[str, SheetGeoCalibration]:
    """Reconstruct the per-sheet calibration objects from the snapshot.

    ``sheet_from_dict`` doesn't touch the PDF, so we get a working
    calibration without needing the chart files in the test
    environment. Both north and south are required for these routes
    — the LLHZ↔LLHA polyline crosses the seam and uses arrows from
    both sheets.
    """
    raw = json.loads(
        (FIXTURES_DIR / "geo_calibration.json").read_text(encoding="utf-8")
    )
    out: dict[str, SheetGeoCalibration] = {}
    for sheet in ("north", "south"):
        block = raw.get(sheet)
        if not isinstance(block, dict):
            continue
        cal = sheet_from_dict(block)
        if cal is not None:
            out[sheet] = cal
    return out


def _build_route_segments(
    tokens: list[str],
    waypoints: dict[str, WaypointRecord],
) -> list[RouteSegment]:
    """Translate a ``LLHZ BAZRA … 3249N03457E … LLHA``-style token list
    into the ``RouteSegment`` shape the matcher consumes.

    Real ICAO 5-letter codes map to a ``RoutePoint`` carrying the
    corresponding ``WaypointRecord`` (so the matcher classifies the
    leg as real-waypoint and uses the strict radius). ICAO coord
    strings produce ``RoutePoint(waypoint=None)`` — the matcher
    treats those as free-click intermediates and applies the loose
    radius to absorb the ±0.5-arc-minute rounding error in the
    written coord. Tokens that are neither raise ``ValueError`` so
    a typo in a test fails fast with a clear message instead of
    silently dropping a leg.
    """
    points: list[RoutePoint] = []
    labels: list[str] = []
    for tok in tokens:
        wp = waypoints.get(tok)
        if wp is not None:
            points.append(RoutePoint(lat=wp.lat, lon=wp.lon, waypoint=wp))
            labels.append(wp.code)
            continue
        coord = _parse_icao_coord(tok)
        if coord is None:
            raise ValueError(
                f"unknown route token {tok!r}: not in waypoint cache and "
                "not a parseable ICAO coord"
            )
        lat, lon = coord
        points.append(RoutePoint(lat=lat, lon=lon, waypoint=None))
        labels.append(tok)

    segments: list[RouteSegment] = []
    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        segments.append(
            RouteSegment(
                from_point=a,
                to_point=b,
                from_label=labels[i - 1],
                to_label=labels[i],
                distance_nm=great_circle_distance_nm(a.lat, a.lon, b.lat, b.lon),
                mag_bearing_deg=magnetic_bearing_deg(
                    a.lat, a.lon, b.lat, b.lon
                ),
            )
        )
    return segments


@pytest.fixture(scope="module")
def matcher_geo() -> dict[str, list]:
    """Project both sheets' arrows to lat/lon once per test module.

    Caching at module scope keeps the cost (one transform-and-build
    pass per sheet) outside the per-test budget — the actual
    regression assertions then run in milliseconds.
    """
    cals = _load_calibrations()
    geo: dict[str, list] = {}
    for sheet in ("north", "south"):
        if sheet in cals:
            arrows = _load_arrows_for_sheet(sheet)
            if arrows:
                geo[sheet] = project_arrows_to_lonlat(arrows, cals[sheet])
    return geo


@pytest.fixture(scope="module")
def waypoints() -> dict[str, WaypointRecord]:
    return _load_waypoints()


def _verdicts_from_tokens(
    tokens: list[str],
    waypoints: dict[str, WaypointRecord],
    geo: dict[str, list],
) -> list[tuple[str, str, tuple[int, ...]]]:
    """Run the matcher and return ``(from, to, altitudes)`` per segment.

    Drops every other column from the route table — distances,
    bearings, times — because the user's regression contract is:
    *the From/To labels and the altitude tuple are stable; the rest
    can change*. Returning a flat list of these triples makes the
    test assertions read like a small spreadsheet diff.
    """
    segments = _build_route_segments(tokens, waypoints)
    alts_per_seg = match_altitudes_for_route(segments, geo)
    return [
        (seg.from_label, seg.to_label, alts)
        for seg, alts in zip(segments, alts_per_seg)
    ]


# ---------------------------------------------------------------------------
# Forward route: LLHZ → LLHA (Herzliya to Haifa via the coastal corridor)
# ---------------------------------------------------------------------------


# User-verified ground-truth altitude tuples for every leg of the route.
# Each entry is ``(from_label, to_label, altitudes_ft)``. ``()`` means the
# user expects "unknown" for that segment — typically because:
#   - the leg starts/ends at an airport (LLHZ→BAZRA is the controlled-
#     departure stub, no chart altitude),
#   - the leg's chart only shows the *opposite-direction* arrow
#     (ZYAAR→HADRA northbound — the chart has only a southbound 2000),
#   - the sub-leg is too short to carry its own arrow (DAROM.1↔DAROM.2,
#     DAROM.2→GALIM are user clicks splitting one chart leg into three).
_FORWARD_TOKENS: list[str] = [
    "LLHZ", "BAZRA", "DEROR", "SHARO", "ZYAAR", "HADRA",
    "FRDIS", "BOREN", "HOTRM", "DAROM",
    "3249N03457E", "3251N03458E",
    "GALIM", "LLHA",
]
_FORWARD_EXPECTED: list[tuple[str, str, tuple[int, ...]]] = [
    ("LLHZ",        "BAZRA",       ()),
    ("BAZRA",       "DEROR",       (1600, 800)),
    ("DEROR",       "SHARO",       (1500,)),
    ("SHARO",       "ZYAAR",       (1500,)),
    ("ZYAAR",       "HADRA",       ()),
    ("HADRA",       "FRDIS",       (1500,)),
    ("FRDIS",       "BOREN",       (1500,)),
    ("BOREN",       "HOTRM",       (1500,)),
    ("HOTRM",       "DAROM",       (1500,)),
    ("DAROM",       "3249N03457E", (1500,)),
    ("3249N03457E", "3251N03458E", ()),
    ("3251N03458E", "GALIM",       ()),
    ("GALIM",       "LLHA",        (1500,)),
]


def test_forward_route_llhz_to_llha_against_user_ground_truth(
    waypoints: dict[str, WaypointRecord],
    matcher_geo: dict[str, list],
) -> None:
    """Forward LLHZ→LLHA round-trip — 13 legs, all altitudes verified by
    the user against the printed CVFR North chart on 2026-05-07.
    """
    actual = _verdicts_from_tokens(_FORWARD_TOKENS, waypoints, matcher_geo)
    assert actual == _FORWARD_EXPECTED


# ---------------------------------------------------------------------------
# Inverse route: LLHA → LLHZ (Haifa to Herzliya, opposite-direction arrows)
# ---------------------------------------------------------------------------


# Inverse uses different intermediate clicks than the forward direction
# (3250N03458E vs 3251N03458E for the second sub-point) — that's the
# user's actual GUI re-plot, captured verbatim. The matcher must still
# return the user's verified ground truth for both polyline shapes.
_INVERSE_TOKENS: list[str] = [
    "LLHA", "GALIM",
    "3250N03458E", "3249N03457E",
    "DAROM", "HOTRM", "BOREN", "FRDIS", "HADRA",
    "ZYAAR", "SHARO", "DEROR", "BAZRA", "LLHZ",
]
_INVERSE_EXPECTED: list[tuple[str, str, tuple[int, ...]]] = [
    ("LLHA",        "GALIM",       (2000, 1000)),
    ("GALIM",       "3250N03458E", ()),
    ("3250N03458E", "3249N03457E", (2000, 1000)),
    ("3249N03457E", "DAROM",       ()),
    ("DAROM",       "HOTRM",       (2000, 1000)),
    ("HOTRM",       "BOREN",       (2000,)),
    ("BOREN",       "FRDIS",       (2000,)),
    ("FRDIS",       "HADRA",       (2000,)),
    ("HADRA",       "ZYAAR",       (2000,)),
    ("ZYAAR",       "SHARO",       ()),
    ("SHARO",       "DEROR",       (2000,)),
    ("DEROR",       "BAZRA",       (2000,)),
    ("BAZRA",       "LLHZ",        ()),
]


def test_inverse_route_llha_to_llhz_against_user_ground_truth(
    waypoints: dict[str, WaypointRecord],
    matcher_geo: dict[str, list],
) -> None:
    """Inverse LLHA→LLHZ round-trip — 13 legs of the opposite-direction
    chart arrows. This is the route that pinned down the loose-radius
    fix for free-clicked intermediates: GALIM.1→GALIM.2 sits 1.0 nm
    cross-track from its (2000, 1000) arrow pair, well outside the
    strict 0.65 nm radius.
    """
    actual = _verdicts_from_tokens(_INVERSE_TOKENS, waypoints, matcher_geo)
    assert actual == _INVERSE_EXPECTED


# ---------------------------------------------------------------------------
# Forward route #2: LLHZ → LLIB (Herzliya → Rosh Pina via Jezreel valley)
# ---------------------------------------------------------------------------
#
# Locked-in alongside the LLHZ↔LLHA round-trip on 2026-05-09 after the
# extractor's ``_MAX_ARROW_PATH_ITEMS`` gate landed. This route exercises
# three things the LLHZ↔LLHA pair didn't reach:
#
# * **Settlement-blob rejection.** Pre-gate, the EIRON.1→ZMGID leg used to
#   pick up a phantom ``(3000,)`` from the Umm El Fahm settlement marker
#   (a yellow blob with 43 path items whose bbox swallowed a nearby
#   altitude digit span). With the gate the phantom is gone and the leg
#   correctly returns ``()`` because the only real 3000 in the area
#   points southwest — anti-parallel to the segment's NE bearing.
#
# * **Lucky-coincidence acknowledgement.** The ZMGID→LLMG leg returns
#   ``(2500,)`` from a real 2500 arrow whose tail anchors *roughly* on
#   the segment line. The arrow actually labels the ZMGID→AFULA chart
#   route (which passes through LLMG visually but bends a little before
#   it), and falls within the parallel tolerance of the ZMGID→LLMG
#   bearing as well — so the matcher's verdict happens to be the
#   correct chart altitude, just for a slightly off-by-bearing reason.
#   Captured here verbatim so a future tightening of the parallel
#   tolerance fails this assertion loudly enough that we re-evaluate.
#
# * **Single-coord intermediate.** ``3231N03507E`` is the EIRON→ZMGID
#   midpoint click ("EIRON.1"); the LLHA route uses two such clicks in
#   a row, here we have just one. Confirms the loose-radius selection
#   still kicks in for legs touching only one intermediate.
_FORWARD_LLHZ_LLIB_TOKENS: list[str] = [
    "LLHZ", "BAZRA", "DEROR", "SHARO", "ZYAAR", "HADRA", "EIRON",
    "3231N03507E",
    "ZMGID", "LLMG", "AFULA", "TAVOR", "DESHE", "AMNON", "LLIB",
]
_FORWARD_LLHZ_LLIB_EXPECTED: list[tuple[str, str, tuple[int, ...]]] = [
    ("LLHZ",        "BAZRA",       ()),
    ("BAZRA",       "DEROR",       (1600, 800)),
    ("DEROR",       "SHARO",       (1500,)),
    ("SHARO",       "ZYAAR",       (1500,)),
    ("ZYAAR",       "HADRA",       ()),
    ("HADRA",       "EIRON",       (2500,)),
    ("EIRON",       "3231N03507E", (2500,)),
    # EIRON.1→ZMGID — was phantom-3000 pre-fix; now correctly unknown
    # because the only real 3000 nearby points SW (anti-parallel to the
    # NE segment bearing). See header comment.
    ("3231N03507E", "ZMGID",       ()),
    # ZMGID→LLMG — lucky-coincidence 2500. See header comment.
    ("ZMGID",       "LLMG",        (2500,)),
    ("LLMG",        "AFULA",       ()),
    ("AFULA",       "TAVOR",       (2500,)),
    ("TAVOR",       "DESHE",       (2500,)),
    ("DESHE",       "AMNON",       (2500,)),
    ("AMNON",       "LLIB",        (2500,)),
]


def test_forward_route_llhz_to_llib_against_user_ground_truth(
    waypoints: dict[str, WaypointRecord],
    matcher_geo: dict[str, list],
) -> None:
    """Forward LLHZ→LLIB — 14 legs of the user's next sim flight, all
    altitudes verified by the user against the printed CVFR North chart
    on 2026-05-09. Locked in alongside the path-items gate that finally
    suppresses the EIRON.1→ZMGID Umm El Fahm phantom.
    """
    actual = _verdicts_from_tokens(_FORWARD_LLHZ_LLIB_TOKENS, waypoints, matcher_geo)
    assert actual == _FORWARD_LLHZ_LLIB_EXPECTED


# ---------------------------------------------------------------------------
# Inverse route #2: LLIB → LLHZ (Rosh Pina → Herzliya, opposite-direction)
# ---------------------------------------------------------------------------
#
# Locked-in alongside the curve-segment gate (``_FORBIDDEN_ARROW_PATH_KINDS``)
# on 2026-05-09. Three legs deserve commentary:
#
# * **EIRON.1 → EIRON ⇒ ()** — pre-fix this used to return ``(2500,)`` from
#   a phantom *bidirectional* arrow that was actually the holding-pattern
#   racetrack just NE of EIRON's triangle. The ``{'c': 4, 'l': 2}``
#   curve-item signature is now caught by the extractor's curve-segment
#   gate, so the matcher correctly returns "unknown": no real altitude
#   arrow exists in this sub-leg, and the only PR 3000 in the area is
#   competitively claimed by the next leg (EIRON→HADRA). The user flies
#   the whole ZMGID→EIRON corridor at 3000 ft in real life, but the chart
#   only labels the first half (ZMGID→EIRON.1) explicitly.
#
# * **AFULA → LLMG ⇒ (3000,)** — same structural lucky-coincidence as the
#   forward ZMGID→LLMG = 2500 leg, just at a different altitude: the
#   3000 PR arrow at @(32.608, 35.274) actually labels a different
#   chart route that bends slightly through LLMG, but lies within the
#   matcher's parallel tolerance of the AFULA→LLMG bearing. Captured
#   here verbatim so a future tightening fails this assertion loudly
#   enough that we re-evaluate. (Forward LLMG→AFULA = unknown because
#   the same arrow becomes anti-parallel reversing direction.)
#
# * **HADRA → ZYAAR ⇒ (2000,)** — the chart's southbound 2000 arrow at
#   @(32.451, 34.914) is parallel-LEFT in this direction (chart anomaly:
#   the printed arrow is on the opposite side of the airway from the
#   convention). The matcher accepts parallel-left as a fallback when
#   no parallel-right exists, which is the right call here. Forward
#   direction (ZYAAR→HADRA) returns unknown because that same arrow
#   becomes anti-parallel.
_INVERSE_LLIB_LLHZ_TOKENS: list[str] = [
    "LLIB", "AMNON", "DESHE", "TAVOR", "AFULA", "LLMG", "ZMGID",
    "3231N03507E",
    "EIRON", "HADRA", "ZYAAR", "SHARO", "DEROR", "BAZRA", "LLHZ",
]
_INVERSE_LLIB_LLHZ_EXPECTED: list[tuple[str, str, tuple[int, ...]]] = [
    ("LLIB",        "AMNON",       (3000,)),
    ("AMNON",       "DESHE",       (3000,)),
    ("DESHE",       "TAVOR",       (3000,)),
    ("TAVOR",       "AFULA",       (3000,)),
    # AFULA→LLMG — lucky-coincidence 3000. See header comment.
    ("AFULA",       "LLMG",        (3000,)),
    ("LLMG",        "ZMGID",       ()),
    ("ZMGID",       "3231N03507E", (3000,)),
    # EIRON.1→EIRON — was holding-pattern phantom 2500 pre-fix; now
    # correctly unknown after the curve-segment gate. See header.
    ("3231N03507E", "EIRON",       ()),
    ("EIRON",       "HADRA",       (3000,)),
    # HADRA→ZYAAR — parallel-LEFT southbound 2000 (chart anomaly).
    # See header comment.
    ("HADRA",       "ZYAAR",       (2000,)),
    ("ZYAAR",       "SHARO",       ()),
    ("SHARO",       "DEROR",       (2000,)),
    ("DEROR",       "BAZRA",       (2000,)),
    ("BAZRA",       "LLHZ",        ()),
]


def test_inverse_route_llib_to_llhz_against_user_ground_truth(
    waypoints: dict[str, WaypointRecord],
    matcher_geo: dict[str, list],
) -> None:
    """Inverse LLIB→LLHZ — 14 legs of the user's return flight, locked in
    alongside the curve-segment gate that suppresses the EIRON-area
    holding-pattern phantom. Pinned forms the symmetric counterpart to
    the LLHZ→LLIB forward regression.
    """
    actual = _verdicts_from_tokens(_INVERSE_LLIB_LLHZ_TOKENS, waypoints, matcher_geo)
    assert actual == _INVERSE_LLIB_LLHZ_EXPECTED


# ---------------------------------------------------------------------------
# Forward route #3: LLIB → LLMZ (Rosh Pina → Masada via Jordan / Dead Sea)
# ---------------------------------------------------------------------------
#
# Locked-in on 2026-05-13 alongside the two-gate past-endpoint fix
# (``MATCH_MAX_ENDPOINT_OVERSHOOT_NM`` + ``MATCH_PARALLEL_TOL_DEG_PAST_ENDPOINT``).
# This is the user's plotted VATSIM flight from Rosh Pina (LLIB) south
# along the Jordan Valley and Dead Sea coast to Masada (LLMZ), verified
# leg-by-leg against the printed CVFR North + South charts. Eight free-
# clicked intermediates split the long Jordan-Valley and Dead-Sea
# corridors into ICAO-Field-15-style sub-legs (the ``TIRAT.1/2``,
# ``ARGMN.1``, ``NAAMA.1/2/3``, ``ALMOG.1``, and ``ENGDI.1`` clicks).
#
# Three structurally interesting points the route exercises:
#
# * **Two past-endpoint Highway-1 false positives killed.** Pre-fix the
#   ``3147N03530E → ALMOG`` (Highway 1 west of ALMOG) and
#   ``3126N03523E → LLMZ`` (Dead Sea corridor south of LLMZ) legs both
#   returned phantom altitudes from chart arrows whose feet project
#   past the leg's terminal waypoint. Both now correctly ``()``; the
#   ALMOG case is *the* canonical regression for the two-gate fix and
#   is captured at sub-minute click precision by the companion test
#   ``test_dead_sea_almog_leg_with_precise_click_coords`` (this
#   rounded-coord ICAO test path leaves the precise-coord margin
#   uncovered, so the companion test is independently necessary).
#
# * **Stacked free-click chains stay clean.** The NAAMA→ALMOG region
#   has THREE intermediate clicks in a row (``NAAMA.1`` / ``NAAMA.2``
#   / ``NAAMA.3``); all three sub-legs correctly resolve to ``()``
#   because no chart arrow's bearing aligns with the kinked route
#   the user clicked. The matcher's competitive-matching + per-leg
#   loose-radius policy handles the chain without an arrow leaking
#   between adjacent sub-legs.
#
# * **NAAMA → 3153N03531E ⇒ (3500,)** — the 3500 arrow at @(31.898,
#   35.469) bearing 109.5° legitimately labels the ESE-bound NAAMA
#   approach. Its foot is on the segment, so neither past-endpoint
#   gate fires; this is the canonical "intermediate leg, on-segment
#   chart arrow, correctly matched" shape.
_FORWARD_LLIB_LLMZ_TOKENS: list[str] = [
    "LLIB", "AMNON", "DESHE", "ALUMT", "KOYAR", "EITAN", "TIRAT",
    "3218N03533E", "3213N03533E",
    "ARGMN",
    "3205N03530E",
    "FAZEL", "NAAMA",
    "3153N03531E", "3150N03532E", "3147N03530E",
    "ALMOG",
    "3145N03528E",
    "ZUKIM", "SHALM", "ENGDI",
    "3126N03523E",
    "LLMZ",
]
_FORWARD_LLIB_LLMZ_EXPECTED: list[tuple[str, str, tuple[int, ...]]] = [
    # Northern leg — Sea of Galilee + Bet She'an Valley at 3000.
    ("LLIB",        "AMNON",       (3000,)),
    ("AMNON",       "DESHE",       (3000,)),
    ("DESHE",       "ALUMT",       (3000,)),
    ("ALUMT",       "KOYAR",       (3000,)),
    ("KOYAR",       "EITAN",       (3000,)),
    ("EITAN",       "TIRAT",       (3000,)),
    # TIRAT southbound — the first sub-leg picks up the 3000 chart
    # arrow on the route line; the next two free-click sub-legs are
    # off-route enough to return unknown.
    ("TIRAT",       "3218N03533E", (3000,)),
    ("3218N03533E", "3213N03533E", ()),
    ("3213N03533E", "ARGMN",       ()),
    # ARGMN southbound — first sub-leg back on the chart route at
    # 3000, second sub-leg into FAZEL is short and unannotated.
    ("ARGMN",       "3205N03530E", (3000,)),
    ("3205N03530E", "FAZEL",       ()),
    # FAZEL → NAAMA: 3500 corridor begins.
    ("FAZEL",       "NAAMA",       (3500,)),
    # NAAMA southeast / south sub-legs: only the first picks up its
    # ESE-bound 3500 arrow; the kinked free-click chain through to
    # ALMOG correctly returns unknown for the next three sub-legs.
    ("NAAMA",       "3153N03531E", (3500,)),
    ("3153N03531E", "3150N03532E", ()),
    ("3150N03532E", "3147N03530E", ()),
    # THE Dead Sea past-endpoint bug — pre-fix returned (4000,) from
    # the Highway 1 chart arrow west of ALMOG. Two-gate fix resolves.
    ("3147N03530E", "ALMOG",       ()),
    # ALMOG southbound — first sub-leg on the chart's 3500 route,
    # then a short connector into ZUKIM that's unannotated.
    ("ALMOG",       "3145N03528E", (3500,)),
    ("3145N03528E", "ZUKIM",       ()),
    # Dead Sea western shore — long stretch at 3500.
    ("ZUKIM",       "SHALM",       (3500,)),
    ("SHALM",       "ENGDI",       (3500,)),
    ("ENGDI",       "3126N03523E", (3500,)),
    # Second past-endpoint kill — pre-fix returned (3500,) from arrows
    # whose feet project ~0.47 nm past LLMZ. Now correctly unknown.
    ("3126N03523E", "LLMZ",        ()),
]


def test_forward_route_llib_to_llmz_against_user_ground_truth(
    waypoints: dict[str, WaypointRecord],
    matcher_geo: dict[str, list],
) -> None:
    """Forward LLIB→LLMZ — 22 legs of the user's Jordan/Dead Sea
    coastal route, all altitudes verified by the user against the
    printed CVFR North + South charts on 2026-05-13. This is the
    route that motivated the two-gate past-endpoint fix
    (``MATCH_MAX_ENDPOINT_OVERSHOOT_NM`` +
    ``MATCH_PARALLEL_TOL_DEG_PAST_ENDPOINT``); see the test header
    comment for the three structural shapes it pins down."""
    actual = _verdicts_from_tokens(
        _FORWARD_LLIB_LLMZ_TOKENS, waypoints, matcher_geo
    )
    assert actual == _FORWARD_LLIB_LLMZ_EXPECTED


def test_dead_sea_almog_leg_with_precise_click_coords(
    waypoints: dict[str, WaypointRecord],
    matcher_geo: dict[str, list],
) -> None:
    """Faithfully reproduce the live-app bug shape: the GUI stores
    free-clicked intermediates at *sub-minute* lat/lon precision,
    but the display label (and ``_parse_icao_coord`` in the other
    tests) rounds them to whole ICAO minutes. That rounding
    matters here — the bug arrow's along-segment overshoot is

      * **0.42 nm** when the leg is built from the ICAO-minute
        display label ``3147N03530E`` (32.78333, 35.5)
      * **0.29 nm** when the leg is built from the user's actual
        click position (≈ 31.791, 35.497, derived from the GUI's
        reported 2.1 nm leg distance and 264°M bearing)

    The 0.30 nm overshoot gate alone catches the rounded-coord
    case but quietly fails to catch the precise-coord live case
    by a 0.01 nm margin. The complementary
    ``MATCH_PARALLEL_TOL_DEG_PAST_ENDPOINT`` gate is what catches
    the live case — the bug arrow's bearing (292°) sits 23.6° off
    the leg bearing (268°), outside the 15° past-endpoint
    tolerance.

    This test would have passed even WITHOUT the parallel-tol-
    past-endpoint gate (because the overshoot gate's rounded-coord
    margin is just enough for the ICAO test path), but the
    precise-coord shape is what the user actually plots, so the
    test must exercise it. Without the second gate this test fails.
    """
    almog = waypoints["ALMOG"]
    # User's plotted click corresponds to leg distance 2.1 nm and
    # magnetic bearing 264°M from ALMOG (≈ 268.4°T at Israeli
    # variation), placing the click at ~ 31°47.4′N 35°29.8′E. The
    # GUI rounds this to the ``3147N03530E`` display label.
    click_lat = 31.0 + 47.4 / 60.0  # 31.79
    click_lon = 35.0 + 29.8 / 60.0  # 35.49666...
    almog_point = RoutePoint(lat=almog.lat, lon=almog.lon, waypoint=almog)
    click_point = RoutePoint(lat=click_lat, lon=click_lon, waypoint=None)
    seg = RouteSegment(
        from_point=click_point,
        to_point=almog_point,
        from_label="3147N03530E",
        to_label="ALMOG",
        distance_nm=great_circle_distance_nm(
            click_lat, click_lon, almog.lat, almog.lon
        ),
        mag_bearing_deg=magnetic_bearing_deg(
            click_lat, click_lon, almog.lat, almog.lon
        ),
    )
    alts = match_altitudes_for_route([seg], matcher_geo)
    assert alts == [()], (
        f"3147N03530E → ALMOG leg (precise click) should match unknown "
        f"— no chart-printed altitude arrow points along this leg's "
        f"direction with its tail anchored on the segment. Got {alts}."
    )


# ---------------------------------------------------------------------------
# Inverse route #3: LLMZ → LLHZ (Masada → Herzliya, full Dead-Sea-to-coast)
# ---------------------------------------------------------------------------
#
# Locked-in on 2026-05-14 alongside two same-session matcher additions:
#
# * The on-segment vs past-endpoint tier in ``_fit_key`` (see the
#   ``test_matcher_prefers_on_segment_alternative_over_past_endpoint``
#   case in ``test_altitude_arrows.py``), which makes the SORES→SHARG
#   single-leg state pick the on-segment 3300 arrow at Eyal Junction
#   over the past-endpoint 2300 arrow at SHARG itself.
# * The shared-bend arrow rescue (Phase 3 of
#   ``match_altitudes_for_route``), which captures the canonical
#   HTZUK→KNTRY→LLHZ 1200 corridor where a single chart arrow at the
#   bend labels both legs along the bisector of their bearings
#   (≈70°, vs the legs' 104°/36° individual bearings).
#
# This is the user's plotted route from Masada (LLMZ) north along the
# Dead Sea, over the Judean hills into the Eyal/Latrun corridor, then
# along the coast through HRTZ/IKKEA cluster up into the LLHZ Class-D
# approach. 27 legs, every altitude verified by the user against the
# printed CVFR South + North charts on 2026-05-14.
#
# Three structural shapes the route exercises that the prior
# regressions didn't:
#
# * **On-segment vs past-endpoint tier (SORES→SHARG, SHARG→LTRUN).**
#   Pre-fix the SORES→SHARG single-leg state showed 2300 because the
#   2300 arrow at SHARG itself is closer cross-track (endpoint-clamped)
#   than the 3300 arrow on the Eyal Junction segment line; only adding
#   the SHARG→LTRUN leg made competition re-assign correctly. The
#   ``_onseg_tier`` slot in ``_fit_key`` now picks on-segment over
#   past-endpoint within the same direction class, so the single-leg
#   case already returns 3300 — and the two-leg state stays correct.
#
# * **Shared-bend arrow rescue (HTZUK→KNTRY, KNTRY→LLHZ).** Pre-fix
#   both legs returned "unknown" because the single 1200 corridor
#   arrow at the LLHZ Class-D approach is bisector-aligned (bearing
#   71.4°) rather than parallel to either leg, just outside the 30°
#   parallel tolerance on HTZUK→KNTRY and well outside the 0.65 nm
#   real-waypoint radius on KNTRY→LLHZ. The rescue attributes the
#   arrow to BOTH legs once its eligibility gates pass.
#
# * **Free-click intermediate sanity (3145N03528E→ALMOG, etc.).**
#   The 3145N03528E intermediate north of ZUKIM correctly returns
#   unknown because no arrow's bearing aligns with the intermediate-
#   click direction. Same pattern as the LLIB→LLMZ NAAMA chain,
#   exercised here on the opposite-direction Dead Sea leg.
_INVERSE_LLMZ_LLHZ_TOKENS: list[str] = [
    "LLMZ",
    "3126N03522E",
    "ENGDI", "SHALM", "ZUKIM",
    "3145N03528E",
    "ALMOG", "YRIHO", "MIHMS", "ANATA", "HNINA", "HAREL", "SORES",
    "SHARG", "LTRUN", "AYLON", "NSHRM", "SIRNI", "NTAIM", "IKKEA",
    "MEHOL", "SUPER", "TYONA", "CLORE", "RIDNG", "HTZUK", "KNTRY",
    "LLHZ",
]
_INVERSE_LLMZ_LLHZ_EXPECTED: list[tuple[str, str, tuple[int, ...]]] = [
    # Dead Sea northbound corridor — 4000 from LLMZ to ZUKIM along the
    # western Dead Sea shore.
    ("LLMZ",        "3126N03522E", (4000,)),
    ("3126N03522E", "ENGDI",       ()),
    ("ENGDI",       "SHALM",       (4000,)),
    ("SHALM",       "ZUKIM",       (4000,)),
    ("ZUKIM",       "3145N03528E", (4000,)),
    # 3145N03528E → ALMOG: short connector, unannotated on the chart.
    ("3145N03528E", "ALMOG",       ()),
    # Judean hills climb — ALMOG westbound through the Jerusalem
    # corridor, picking up the 4000+5000 stacked altitudes at the
    # YRIHO band before settling on 5000 across the central legs.
    ("ALMOG",       "YRIHO",       (4000,)),
    ("YRIHO",       "MIHMS",       (4000, 5000)),
    ("MIHMS",       "ANATA",       (5000,)),
    ("ANATA",       "HNINA",       (5000,)),
    ("HNINA",       "HAREL",       (5000,)),
    ("HAREL",       "SORES",       (5000,)),
    # SORES→SHARG/SHARG→LTRUN — the on-segment-tier case. Pre-fix
    # the single-leg SORES→SHARG state showed 2300; with the
    # ``_onseg_tier`` slot in ``_fit_key`` the on-segment 3300 wins
    # immediately. SHARG→LTRUN keeps the 2300 arrow that was always
    # its correct match.
    ("SORES",       "SHARG",       (3300,)),
    ("SHARG",       "LTRUN",       (2300,)),
    # LTRUN→AYLON→NSHRM→SIRNI — straightforward parallel-right
    # matches at descending altitudes (1600/1200/1200) as the route
    # heads northwest along Highway 1's corridor.
    ("LTRUN",       "AYLON",       (1600,)),
    ("AYLON",       "NSHRM",       (1200,)),
    ("NSHRM",       "SIRNI",       (1200,)),
    # SIRNI cluster through to SUPER — the chart explicitly does not
    # label this short segment chain (the corridor altitude changes at
    # SIRNI), and the matcher correctly returns unknown for each.
    # IMPORTANT: this is the safety case for the shared-bend rescue —
    # the bend rescue must NOT fire here even though several pairs
    # have both adjacent legs unknown, because the bends here are
    # below the 30° min-bend gate (max ~22° at NTAIM→IKKEA→MEHOL).
    ("SIRNI",       "NTAIM",       ()),
    ("NTAIM",       "IKKEA",       ()),
    ("IKKEA",       "MEHOL",       ()),
    ("MEHOL",       "SUPER",       ()),
    # SUPER → TYONA → CLORE → RIDNG → HTZUK — coastal corridor at
    # 1200, straightforward per-leg matches with bearings ~330°/360°
    # alternating slightly as the route weaves between fixes.
    ("SUPER",       "TYONA",       (1200,)),
    ("TYONA",       "CLORE",       (1200,)),
    ("CLORE",       "RIDNG",       (1200,)),
    ("RIDNG",       "HTZUK",       (1200,)),
    # HTZUK → KNTRY → LLHZ — the shared-bend rescue case. The
    # corridor's single 1200 arrow at bearing 71.4° lies on the
    # bisector of the two legs (104° + 36° → 70°) and is rescued
    # onto BOTH legs as the chart-reading pilot would interpret.
    ("HTZUK",       "KNTRY",       (1200,)),
    ("KNTRY",       "LLHZ",        (1200,)),
]


def test_inverse_route_llmz_to_llhz_against_user_ground_truth(
    waypoints: dict[str, WaypointRecord],
    matcher_geo: dict[str, list],
) -> None:
    """Inverse LLMZ→LLHZ — 27 legs of the user's Masada-to-Herzliya
    plotted route, all altitudes verified by the user against the
    printed CVFR South + North charts on 2026-05-14. Locked in
    alongside the on-segment-vs-past-endpoint tier in ``_fit_key``
    (SORES→SHARG case) and the shared-bend arrow rescue
    (HTZUK→KNTRY→LLHZ case). The route exercises 24 single-leg
    matches plus the 2-leg bend rescue plus the SIRNI-cluster
    "rescue-must-not-fire" safety pattern, so a regression in any of
    those three pieces makes this assertion fail loudly enough that
    the offending leg is identifiable from the diff.
    """
    actual = _verdicts_from_tokens(
        _INVERSE_LLMZ_LLHZ_TOKENS, waypoints, matcher_geo
    )
    assert actual == _INVERSE_LLMZ_LLHZ_EXPECTED


# ---------------------------------------------------------------------------
# LLHZ→LLMZ — Herzliya-to-Masada plotted route, the reverse direction
# of the test above. Verified against the printed CVFR charts on
# 2026-05-14 (same flying day). 30 legs.
#
# This is the route that surfaced two bugs at once:
#
# * **Bidirectional arrow with no body-axis bearing.** The RIDNG→CLORE
#   leg was wrongly matching the bidirectional 1200 ft arrow between
#   RIDNG and ROKCH whose body axis runs east-west (labelling the
#   RIDNG↔ROKCH corridor, not our SW-going route). The extractor used
#   to drop the arrow's body-axis bearing to a flat ``0.0`` placeholder,
#   so the matcher had no signal that this arrow doesn't apply to a
#   SW-going leg. Fix: record the tip-to-tip chord bearing in
#   ``bearing_deg``; matcher applies a parallel-OR-antiparallel gate.
#
# * **HRTZ coastal corridor's broadcast SB 800 labels.** The chart
#   prints the SB 800 ft labels in a column 1.0–1.8 nm west of the
#   route's waypoint chain (the chart designer's "open space" along
#   the LBG TMA boundary). Six consecutive coastal legs (SFAIM→APOLN
#   through CLORE→TYONA) plus SIRNI→NSHRM came back unknown under the
#   strict 0.65 nm primary radius. Fix: wide-corridor rescue (phase 4)
#   admits parallel-right, on-segment, tightly bearing-aligned arrows
#   out to 1.8 nm for legs still unknown after phases 1–3.
#
# Both fixes are exercised by this regression in a single end-to-end
# pass: RIDNG→CLORE going from wrong-1200 to 800, and the entire
# coastal corridor going from unknown to 800.
_FORWARD_LLHZ_LLMZ_TOKENS: list[str] = [
    "LLHZ",
    "3212N03450E",
    "SFAIM", "APOLN", "ARENA", "HTZUK", "RIDNG", "CLORE", "TYONA",
    "SUPER", "MEHOL", "IKKEA", "NTAIM", "SIRNI", "NSHRM", "AYLON",
    "LTRUN", "SHARG", "SORES", "HAREL", "HNINA", "ANATA", "DUMIM",
    "YRIHO", "ALMOG",
    "3145N03528E",
    "ZUKIM", "SHALM", "ENGDI",
    "3126N03523E",
    "LLMZ",
]
_FORWARD_LLHZ_LLMZ_EXPECTED: list[tuple[str, str, tuple[int, ...]]] = [
    # LLHZ departure / Herzliya CTR — first two legs are unannotated
    # short connectors out of the airport before the corridor begins.
    ("LLHZ",        "3212N03450E", ()),
    ("3212N03450E", "SFAIM",       ()),
    # HRTZ coastal corridor southbound — six legs of the wide-corridor
    # rescue. Every SB 800 ft label sits 0.66–1.78 nm west of the
    # waypoint chain (outside the strict 0.65 nm radius), so phase 4
    # picks them up.
    ("SFAIM",       "APOLN",       (800,)),
    ("APOLN",       "ARENA",       (800,)),
    ("ARENA",       "HTZUK",       (800,)),
    ("HTZUK",       "RIDNG",       (800,)),
    # RIDNG→CLORE — both fixes meet here. The bidirectional 1200 arrow
    # in the RIDNG↔ROKCH wedge would have wrongly won pre-axis-bearing-
    # fix; with the axis gate it's rejected for our SW direction and
    # the wide-corridor rescue picks up the SB 800 label.
    ("RIDNG",       "CLORE",       (800,)),
    ("CLORE",       "TYONA",       (800,)),
    # TYONA→SUPER — primary catches this one already (closer 0.56 nm,
    # bearing-aligned), no rescue needed. Pinning to make sure the
    # rescue doesn't somehow re-attribute it.
    ("TYONA",       "SUPER",       (800,)),
    # SIRNI cluster — same as the reverse direction: the chart leaves
    # the SUPER→...→NTAIM short chain unannotated, and the matcher
    # correctly returns unknown for each. The wide-corridor rescue
    # MUST NOT fire here even though every gate looks superficially
    # similar — there's no on-segment parallel-right SB 800 arrow in
    # this latitude band (the chart simply doesn't label these legs).
    ("SUPER",       "MEHOL",       ()),
    ("MEHOL",       "IKKEA",       ()),
    ("IKKEA",       "NTAIM",       ()),
    # NTAIM→SIRNI: primary catches the 800. SIRNI→NSHRM: wide-corridor
    # rescue picks up the eastbound 800 arrow at 0.82 nm south of the
    # route line, fwd-diff 15.7°. This is exactly the case that drove
    # the rescue's bearing gate to 20° (a 15° gate would have missed
    # it).
    ("NTAIM",       "SIRNI",       (800,)),
    ("SIRNI",       "NSHRM",       (800,)),
    # NSHRM→AYLON→...→SORES — Highway 1 corridor inland, primary
    # matches throughout. Same altitudes as the reverse direction's
    # SORES→...→NSHRM run but read in increasing-altitude order as
    # we climb out of the coastal plain.
    ("NSHRM",       "AYLON",       (800,)),
    ("AYLON",       "LTRUN",       (1200,)),
    ("LTRUN",       "SHARG",       (1800,)),
    ("SHARG",       "SORES",       (2800,)),
    # SORES→HAREL→...→ANATA — Judean hills at 4500 ft. Note the
    # asymmetric reading vs the reverse route: going west to east the
    # chart's stacked 4000+5000 band at YRIHO is read as 4500
    # (single-arrow chart label for this direction), whereas going
    # east to west the same chart cell appears as (4000, 5000).
    ("SORES",       "HAREL",       (4500,)),
    ("HAREL",       "HNINA",       (4500,)),
    ("HNINA",       "ANATA",       (4500,)),
    ("ANATA",       "DUMIM",       (4500,)),
    # DUMIM→YRIHO→ALMOG — Jordan Valley descent to the Dead Sea, 3500.
    ("DUMIM",       "YRIHO",       (3500,)),
    ("YRIHO",       "ALMOG",       (3500,)),
    # ALMOG→3145N03528E: free-click intermediate, real-waypoint legs
    # on both sides; chart's 3500 label sits along this leg.
    ("ALMOG",       "3145N03528E", (3500,)),
    # 3145N03528E→ZUKIM: short connector down to ZUKIM — unannotated,
    # mirrors the reverse direction's (3145N03528E→ALMOG) leg.
    ("3145N03528E", "ZUKIM",       ()),
    # ZUKIM→SHALM→ENGDI — Dead Sea southbound corridor at 3500.
    ("ZUKIM",       "SHALM",       (3500,)),
    ("SHALM",       "ENGDI",       (3500,)),
    # ENGDI→3126N03523E: chart's southbound 3500 label still applies
    # at this latitude band; the free-click intermediate is a
    # bearing-aligned continuation of the corridor.
    ("ENGDI",       "3126N03523E", (3500,)),
    # 3126N03523E→LLMZ: terminal connector into LLMZ, unannotated.
    ("3126N03523E", "LLMZ",        ()),
]


def test_forward_route_llhz_to_llmz_against_user_ground_truth(
    waypoints: dict[str, WaypointRecord],
    matcher_geo: dict[str, list],
) -> None:
    """Forward LLHZ→LLMZ — 30 legs of the user's Herzliya-to-Masada
    plotted route, verified against the printed CVFR charts on
    2026-05-14. This is the route that drove both the bidirectional
    axis-bearing fix (RIDNG→CLORE) and the wide-corridor rescue
    (SFAIM→…→TYONA coastal chain plus SIRNI→NSHRM).

    Pinning this protects:

    * Bidirectional arrows with non-trivial body axes (RIDNG↔ROKCH
      1200) don't leak into orthogonal route directions.
    * The wide-corridor rescue catches the HRTZ coastal SB 800 arrows
      that sit outside the strict primary radius.
    * The rescue doesn't fire on the SIRNI cluster's intentionally-
      unannotated chain (SUPER→MEHOL→IKKEA→NTAIM), so the chart's
      explicit "no corridor label here" still reads as unknown.

    Any future drive-by retune of the bidirectional axis gate, the
    wide-corridor radius/bearing constants, or the rescue's eligibility
    filters has to come through this assertion.
    """
    actual = _verdicts_from_tokens(
        _FORWARD_LLHZ_LLMZ_TOKENS, waypoints, matcher_geo
    )
    assert actual == _FORWARD_LLHZ_LLMZ_EXPECTED


# ---------------------------------------------------------------------------
# Sanity checks on the fixtures
# ---------------------------------------------------------------------------


def test_fixtures_directory_contains_required_snapshots() -> None:
    """Fail fast with a clear message if a fixture file goes missing —
    much easier to diagnose than a downstream JSON-decode error."""
    required = [
        "waypoints_cache.json",
        "altitude_arrows_north.json",
        "altitude_arrows_south.json",
        "geo_calibration.json",
    ]
    missing = [f for f in required if not (FIXTURES_DIR / f).is_file()]
    assert missing == [], (
        f"missing regression fixtures in {FIXTURES_DIR}: {missing}. "
        "Restore them from .cvfr_routemaster/ if the snapshots were "
        "accidentally deleted."
    )


def test_geo_arrows_built_for_both_sheets(
    matcher_geo: dict[str, list],
) -> None:
    """Both the north and south chart contribute arrows to the LLHZ↔LLHA
    routes, so a missing sheet here would silently turn half the
    expected-altitude assertions into spurious ``unknown``s. Catch
    that early."""
    assert "north" in matcher_geo, "north-sheet arrows missing — calibration?"
    assert len(matcher_geo["north"]) > 0, "north-sheet arrow list is empty"
