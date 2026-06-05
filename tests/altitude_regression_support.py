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

"""Shared helpers for the route-altitude regression tests.

The CVFR regression (``test_route_altitude_regression.py``) grew a full
set of fixture loaders and a token→segment route builder. The LSA map
mode reuses the exact same matcher pipeline against its own snapshot
fixtures, so those helpers are factored here, parameterized by a
``fixtures_dir``, to avoid copy-paste drift between the two modes.

The contract is identical to the CVFR module's: load the four snapshot
caches (waypoints, north/south altitude arrows, geo calibration), project
the arrows to lon/lat once, and translate a route token list (real
5-letter codes + ICAO ``DDMM[NS]DDDMM[EW]`` intermediate coords) into the
``RouteSegment`` shape the matcher consumes. The assertion contract per
segment is the ``(from_label, to_label, altitudes_ft)`` triple — bearings,
distances, and times are intentionally not locked in.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

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

REQUIRED_FIXTURES = (
    "waypoints_cache.json",
    "altitude_arrows_north.json",
    "altitude_arrows_south.json",
    "geo_calibration.json",
)

_ICAO_COORD_RE = re.compile(
    r"^(?P<lat_d>\d{2})(?P<lat_m>\d{2})(?P<lat_h>[NS])"
    r"(?P<lon_d>\d{3})(?P<lon_m>\d{2})(?P<lon_h>[EW])$"
)


def parse_icao_coord(token: str) -> tuple[float, float] | None:
    """Decode an ICAO Field-15 ``DDMM[NS]DDDMM[EW]`` token to (lat, lon).

    Whole-minute precision is intentional: it matches the coord precision
    the GUI formats for a free-clicked intermediate point's display label.
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


def load_waypoints(fixtures_dir: Path) -> dict[str, WaypointRecord]:
    """Load the snapshot waypoint cache as ``{code: WaypointRecord}``.

    A code may map to several real points (LSA stamps both נבטים and נגב
    ``LLNV``); last-seen wins for the dict, but route tokens that need a
    specific one should use an ICAO coord instead. Only coordinates are
    used by the matcher.
    """
    raw = json.loads(
        (fixtures_dir / "waypoints_cache.json").read_text(encoding="utf-8")
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


def load_arrows_for_sheet(fixtures_dir: Path, sheet: str) -> list[AltitudeArrow]:
    """Build ``AltitudeArrow`` objects directly from the JSON snapshot,
    bypassing the PDF-fingerprint gate that would invalidate a bundled
    fixture."""
    raw = json.loads(
        (fixtures_dir / f"altitude_arrows_{sheet}.json").read_text(
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


def load_calibrations(fixtures_dir: Path) -> dict[str, SheetGeoCalibration]:
    """Reconstruct per-sheet calibration objects from the snapshot
    (``sheet_from_dict`` doesn't touch the PDF)."""
    raw = json.loads(
        (fixtures_dir / "geo_calibration.json").read_text(encoding="utf-8")
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


def build_matcher_geo(fixtures_dir: Path) -> dict[str, list]:
    """Project both sheets' arrows to lon/lat once (the per-test cost is
    then just the matcher run)."""
    cals = load_calibrations(fixtures_dir)
    geo: dict[str, list] = {}
    for sheet in ("north", "south"):
        if sheet in cals:
            arrows = load_arrows_for_sheet(fixtures_dir, sheet)
            if arrows:
                geo[sheet] = project_arrows_to_lonlat(arrows, cals[sheet])
    return geo


def build_route_segments(
    tokens: list[str],
    waypoints: dict[str, WaypointRecord],
) -> list[RouteSegment]:
    """Translate a route token list into ``RouteSegment`` objects.

    Real 5-letter codes become ``RoutePoint`` carrying the matching
    ``WaypointRecord`` (strict matcher radius); ICAO coord tokens become
    ``RoutePoint(waypoint=None)`` (loose radius). An unknown token raises
    ``ValueError`` so a typo fails fast.
    """
    points: list[RoutePoint] = []
    labels: list[str] = []
    for tok in tokens:
        wp = waypoints.get(tok)
        if wp is not None:
            points.append(RoutePoint(lat=wp.lat, lon=wp.lon, waypoint=wp))
            labels.append(wp.code)
            continue
        coord = parse_icao_coord(tok)
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
                mag_bearing_deg=magnetic_bearing_deg(a.lat, a.lon, b.lat, b.lon),
            )
        )
    return segments


def verdicts_from_tokens(
    tokens: list[str],
    waypoints: dict[str, WaypointRecord],
    geo: dict[str, list],
) -> list[tuple[str, str, tuple[int, ...]]]:
    """Run the matcher and return ``(from, to, altitudes)`` per segment."""
    segments = build_route_segments(tokens, waypoints)
    alts_per_seg = match_altitudes_for_route(segments, geo)
    return [
        (seg.from_label, seg.to_label, alts)
        for seg, alts in zip(segments, alts_per_seg)
    ]
