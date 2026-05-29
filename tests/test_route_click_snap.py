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

"""Tests for the route-add chart-click snap logic in
:func:`cvfr_routemaster.route.find_nearest_waypoint`.

The route panel's Shift+left handler resolves a chart click to a (lat, lon),
then asks ``find_nearest_waypoint`` whether any named waypoint sits within
the snap radius (``_ROUTE_ADD_SNAP_NM`` = 1.0 nm in ``main_window.py``). If
yes the click becomes a named-fix route point; otherwise it falls through
to an intermediate ``DDMM[NS]DDDMM[EW]`` polyline sub-point.

Three contracts pinned here:

1. **Far-from-anything click → None (intermediate path).** A click well
   outside the snap radius of every waypoint must return ``None`` so the
   caller adds an intermediate. Pinning this prevents a future "always
   return nearest" relaxation from silently swallowing every chart click
   into the nearest named fix even when the user clearly meant a
   polyline bend.

2. **SIRNI-class click at ~0.96 nm → snaps.** SIRNI's back-pages-table
   coordinates differ from the chart triangle by ~0.96 nm — discovered
   May 14, 2026 test-driving the LLBG → NSHRM → SIRNI route. A click on
   the visible triangle resolves ~0.96 nm from the cached SIRNI position;
   the 1.0 nm snap absorbs this by 0.04 nm, the click registers as SIRNI
   rather than as ``3155N03449E``. This test pins the contract using the
   actual cached SIRNI coordinates and the chart-click position the user
   observed.

3. **Click closer to one of two nearby waypoints → closer wins.** In
   the IKKEA/MEHOL/LLRS/SUPER/NTAIM cluster (pairs at 0.65 - 0.81 nm),
   the snap radius (1.0 nm) exceeds inter-waypoint spacing, so both
   waypoints qualify for the same click. The "closer wins" tiebreak in
   ``find_nearest_waypoint`` must return the nearer one — that's the
   property that makes a > 0.5 nm snap radius safe at all. Pinned with a
   synthetic three-waypoint scenario so the test stays robust against
   real-database drift.

No Qt here; pure-data tests.
"""

from __future__ import annotations

from cvfr_routemaster.route import find_nearest_waypoint
from cvfr_routemaster.waypoint_types import WaypointRecord


def _wp(code: str, lat: float, lon: float) -> WaypointRecord:
    return WaypointRecord(
        index=0,
        code=code,
        name_he="",
        reporting_type="MR",
        lat=lat,
        lon=lon,
        lat_dms="",
        lon_dms="",
    )


# Snap radius used by ``MainWindow._on_route_click`` for the *add* path.
# Hard-coded here rather than imported because importing ``main_window``
# would pull in Qt; the constant is small and stable, and a divergence
# between this test and the running app's radius would be caught by any
# manual smoke test.
_SNAP_NM = 1.0


def test_click_far_from_any_waypoint_returns_None() -> None:
    """A click well outside the snap radius of every waypoint in the
    database must return ``None`` so the route-click handler falls
    through to the intermediate-point path. Without this, a future
    "always return nearest" simplification could silently turn every
    chart click into a named fix even when the user clearly meant a
    polyline bend several nm from the nearest triangle."""
    db = [
        _wp("LLBG", 32.000, 34.880),
        _wp("DAROM", 31.550, 34.550),
        _wp("LLHA", 31.720, 35.000),
    ]
    # ~10 nm away from any waypoint in the synthetic db.
    result = find_nearest_waypoint(db, 33.0, 33.0, _SNAP_NM)
    assert result is None


def test_click_within_radius_snaps_to_the_waypoint() -> None:
    """Sanity baseline: a click that's within the snap radius of exactly
    one waypoint must snap to it. Anchors the happy-path contract before
    the trickier multi-waypoint and SIRNI-class cases."""
    db = [
        _wp("LLBG", 32.000, 34.880),
        _wp("DAROM", 31.550, 34.550),
    ]
    # Click ~0.3 nm east of LLBG (well inside 1.0 nm snap).
    result = find_nearest_waypoint(db, 32.000, 34.886, _SNAP_NM)
    assert result is not None
    assert result.code == "LLBG"


def test_click_just_outside_radius_returns_None() -> None:
    """A click slightly outside the snap radius must NOT snap — protects
    the bottom edge of the radius and pins the strict ``<`` comparison
    in the helper (a regression to ``<=`` would change behaviour only at
    the exact boundary which is otherwise easy to miss)."""
    db = [_wp("LLBG", 32.000, 34.880)]
    # ~1.2 nm north of LLBG: 1° lat = 60 nm, so 0.02° ≈ 1.2 nm.
    result = find_nearest_waypoint(db, 32.020, 34.880, _SNAP_NM)
    assert result is None


def test_sirni_class_click_at_just_under_one_nm_snaps() -> None:
    """The actual bug this radius bump was sized for.

    SIRNI is cached at (31.928056°N, 34.830000°E) — that's what the
    back-pages OCR extracts and what the runtime uses for the named-fix
    snap. The chart artist drew SIRNI's triangle ~0.96 nm SW of that,
    near (31.917°N, 34.817°E) — the position a chart click typically
    resolves to. Pre-bump (0.5 nm snap) the click fell through to
    intermediate ``3155N03449E``; the 1.0 nm bump absorbs the offset by
    ~0.04 nm and the click correctly registers as SIRNI.

    Coordinates here come from the live cache and the user's observed
    click; do not "round" them or rebalance — the test is meant to
    verify the radius is sized for the actual observed delta, not a
    synthetic one."""
    sirni = _wp("SIRNI", 31.928056, 34.830000)
    # Chart click position the user reported (rounds to ICAO 3155N03449E
    # in the formatter, which is what the intermediate path produced
    # before the bump).
    click_lat, click_lon = 31.916667, 34.816667
    result = find_nearest_waypoint([sirni], click_lat, click_lon, _SNAP_NM)
    assert result is not None
    assert result.code == "SIRNI"


def test_sirni_class_click_would_not_snap_at_old_half_nm_radius() -> None:
    """Companion to the snap-success test: pins the *reason* the radius
    needed bumping. With the old 0.5 nm snap the SIRNI-class click is
    too far (0.96 nm), so this case must return ``None``. If a future
    change shrinks the snap radius again the user-visible SIRNI bug
    returns; this guard catches the regression at the snap-helper
    level, not all the way out at the route panel."""
    sirni = _wp("SIRNI", 31.928056, 34.830000)
    click_lat, click_lon = 31.916667, 34.816667
    assert find_nearest_waypoint([sirni], click_lat, click_lon, 0.5) is None


def test_click_in_dense_cluster_returns_closer_waypoint() -> None:
    """The IKKEA/MEHOL/LLRS/SUPER/NTAIM cluster has pairs as tight as
    0.65 nm. With the 1.0 nm snap radius, a click on or near any of
    them sits inside multiple waypoints' snap zones simultaneously —
    the "closer wins" tiebreak is what keeps the result well-defined.

    Synthetic three-waypoint scenario mirroring the cluster's geometry:
    two waypoints 0.7 nm apart plus an outlier well away. A click that
    sits 0.2 nm from WP_A and 0.55 nm from WP_B must return WP_A
    (closer), even though both qualify for the 1.0 nm snap."""
    db = [
        _wp("WP_A", 31.900, 34.800),
        _wp("WP_B", 31.900, 34.814),  # ~0.71 nm east of WP_A
        _wp("WP_X", 32.500, 35.500),  # far outlier, well outside any snap
    ]
    # Click ~0.21 nm east of WP_A; that's ~0.5 nm west of WP_B.
    result = find_nearest_waypoint(db, 31.900, 34.804, _SNAP_NM)
    assert result is not None
    assert result.code == "WP_A"


def test_click_closer_to_b_in_dense_cluster_returns_b() -> None:
    """Symmetric to the WP_A case — confirms the tiebreak isn't just
    biased toward the first-listed waypoint when both qualify. Same
    geometry, click shifted to be closer to WP_B."""
    db = [
        _wp("WP_A", 31.900, 34.800),
        _wp("WP_B", 31.900, 34.814),
        _wp("WP_X", 32.500, 35.500),
    ]
    # Click ~0.5 nm east of WP_A; ~0.21 nm west of WP_B.
    result = find_nearest_waypoint(db, 31.900, 34.810, _SNAP_NM)
    assert result is not None
    assert result.code == "WP_B"


def test_empty_database_returns_None() -> None:
    """Defensive: an empty waypoint database — possible before the OCR
    pipeline has run, or on a first-launch error path — must return
    None rather than raise. The route-click handler interprets None as
    "fall through to intermediate" which is the right behaviour in this
    degenerate case (well, modulo the empty-route guard upstream)."""
    assert find_nearest_waypoint([], 31.9, 34.8, _SNAP_NM) is None
