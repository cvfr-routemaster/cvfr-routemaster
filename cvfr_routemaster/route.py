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

"""
Flight route model: an ordered list of route points and the per-segment math
(distance, magnetic bearing, time) flight planning needs.

Two kinds of points coexist in one ordered list:

1. **Real waypoints** — chart fixes from the CVFR back-pages, picked by Shift+clicking
   the triangle on the chart. Display label is the ICAO/CVFR code, e.g. ``DAROM``.

2. **Intermediate points** — user clicks on empty chart space, used to model the
   reality that a leg between two reporting points is not always a straight line
   (see the DAROM → GALIM polyline). They are displayed as ``<previous_wp>.<N>``
   where ``N`` is a 1-based ordinal that resets at each real waypoint, e.g. a leg
   from DAROM with two intermediate clicks before reaching GALIM yields the labels
   ``DAROM, DAROM.1, DAROM.2, GALIM``.

Each adjacent pair becomes its own ``RouteSegment`` with its own distance, bearing
and time — so a polyline leg shows up as multiple table rows that share the
``DAROM.*`` prefix until the next real waypoint.

This module is **UI-free**. The cruise speed that turns distance into segment time
is supplied by the caller (the route panel) so the geometry caches stay valid
across speed changes without recomputation.

**Magnetic variation.** Israeli VFR charts publish isogonic lines around 4°–5°E;
we approximate the country with a single value. This is accurate to well under a
degree across the operating area and is good enough for cockpit headings, but the
constant lives here so we have a single place to refine later (per-point WMM/IGRF
lookup is possible but overkill for a 200 km × 50 km country).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Union

from cvfr_routemaster.waypoint_types import WaypointRecord


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Magnetic declination over Israel, eastward (positive). Taken from the Israeli
#: CVFR chart's compass-rose legend, which prints a country-wide variation value
#: for the chart's effective year. The 2025 cycle's legend reads "VAR 5°E 2025"
#: with a small annual eastward drift noted alongside. One country-wide value is
#: fine for VFR heading calls; chart-printed bearings are integer-rounded and the
#: drafting tolerance dominates any geoid-model nicety.
#:
#: Update this value whenever the chart's printed variation changes (i.e. every
#: new chart cycle / every ~5 years). Verify by re-running
#: ``tests/test_route_chart_headings.py`` (regression pinning a sample of
#: chart-printed mag bearings to within ±1°).
ISRAEL_MAGNETIC_VARIATION_DEG_E: float = 5.0

#: CVFR airspace speed limit in Israel (knots). Aircraft cleared on Civil VFR
#: routes must not exceed this; the route panel warns if a higher cruise speed is
#: entered.
CVFR_MAX_SPEED_KTS: int = 180

#: Earth mean radius in km — Haversine reference radius (WGS-84 mean).
_EARTH_R_KM: float = 6371.0

#: Conversion factor: 1 km = 0.5399568… nm (1 nm = 1852 m exactly, by definition).
_NM_PER_KM: float = 1.0 / 1.852

#: Display label fallback used when an intermediate is somehow recorded before any
#: real waypoint exists. ``Route.append_intermediate`` refuses this case so it
#: should not appear in practice; the constant lets ``display_labels`` stay total.
_ORPHAN_INTERMEDIATE_BASE: str = "VIA"

#: Tolerance (degrees) for considering two clicked points "the same" — used to
#: refuse zero-length consecutive intermediates. ~1e-6° ≈ 11 cm at the equator,
#: well below any meaningful chart-click precision.
_SAME_POINT_DEG_TOL: float = 1e-6


# ---------------------------------------------------------------------------
# Geodesy
# ---------------------------------------------------------------------------


def great_circle_distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Spherical great-circle distance in nautical miles (Haversine).

    Sub-percent accurate over typical CVFR leg lengths (≤ 200 nm); see ``_EARTH_R_KM``
    for the radius used. The spherical model avoids any ellipsoidal dependency and
    is well within the precision a pilot needs for ETA/fuel planning.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return _EARTH_R_KM * c * _NM_PER_KM


def find_nearest_waypoint(
    waypoints: "Iterable[WaypointRecord]",
    lat: float,
    lon: float,
    max_nm: float,
) -> "WaypointRecord | None":
    """Return the waypoint closest to ``(lat, lon)`` within ``max_nm`` great-
    circle nautical miles, or ``None`` if none qualify.

    Pure data, no Qt — pulled out of ``MainWindow._nearest_waypoint_to`` so
    the route-add snap behaviour is unit-testable without the full app
    booting. The route-click handler delegates here and supplies the live
    waypoint-export list.

    "Closer wins" tiebreaking: a linear pass over the iterable keeps the
    smallest distance seen so far; equal distances (essentially impossible
    with float lat/lon) resolve to the first-listed waypoint. That rule is
    what lets the route-add snap radius safely exceed the minimum inter-
    waypoint spacing — overlapping snap zones still resolve cleanly to the
    nearer waypoint instead of the first one scanned.

    The Israeli CVFR database is ~200 waypoints; a linear scan is well
    within budget and avoids spatial-index ceremony.
    """
    best: "WaypointRecord | None" = None
    best_d = max_nm
    for wp in waypoints:
        d = great_circle_distance_nm(lat, lon, wp.lat, wp.lon)
        if d < best_d:
            best_d = d
            best = wp
    return best


def true_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial true bearing of the great circle from point 1 to point 2 (degrees, 0–360).

    Returns 0.0 when the two points coincide; great-circle bearing is undefined
    there but a stable fallback is more useful than NaN for downstream display.
    """
    if lat1 == lat2 and lon1 == lon2:
        return 0.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def magnetic_bearing_deg(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    *,
    magvar_e: float = ISRAEL_MAGNETIC_VARIATION_DEG_E,
) -> float:
    """Magnetic bearing in degrees (0–360), assuming an east-positive variation.

    Convention: ``mag = true − var_E``. With Israel's chart-printed +5°E
    declination, a true heading of 010° flies as a magnetic heading of ~005°.
    """
    return (true_bearing_deg(lat1, lon1, lat2, lon2) - magvar_e + 360.0) % 360.0


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------


def segment_time_seconds(distance_nm: float, speed_kts: float) -> float:
    """``distance / speed`` in seconds, with a defensive guard for zero/negative speed."""
    if speed_kts <= 0.0:
        return 0.0
    return distance_nm / speed_kts * 3600.0


def format_hms(seconds: float) -> str:
    """Format a duration as ``HH:MM:SS``.

    HH is included unconditionally (zero-padded) so the column has a predictable
    width; in practice CVFR legs are minutes long so HH is normally ``00``, but a
    250 nm leg at 90 kts is over 2 h 45 m and the format must accommodate it
    without surprise.
    """
    if seconds < 0.0 or not math.isfinite(seconds):
        seconds = 0.0
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Route points + segments
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutePoint:
    """One point in the planned route.

    Either a real chart waypoint (``waypoint`` is the corresponding ``WaypointRecord``)
    or an intermediate user-clicked point (``waypoint`` is ``None``). The geometry
    fields ``lat``/``lon`` are the authoritative coordinates used for distance and
    bearing math; for real waypoints they're copied from the ``WaypointRecord`` at
    construction time so callers don't need to dereference twice.
    """

    lat: float
    lon: float
    waypoint: WaypointRecord | None

    @property
    def is_waypoint(self) -> bool:
        return self.waypoint is not None


@dataclass(frozen=True)
class RouteSegment:
    """One leg of a route, with pre-computed geometry and resolved display labels.

    ``from_label`` / ``to_label`` are the names the table cells should show — they
    bake in the intermediate-point ordinal scheme (``DAROM.1`` etc.) so the panel
    doesn't have to repeat the labelling logic.

    Time is derived from speed on demand; cruise speed is a UI input that changes
    independently of geometry.
    """

    from_point: RoutePoint
    to_point: RoutePoint
    from_label: str
    to_label: str
    distance_nm: float
    mag_bearing_deg: float

    def time_seconds(self, speed_kts: float) -> float:
        return segment_time_seconds(self.distance_nm, speed_kts)


class Route:
    """Ordered list of route points (real waypoints + user-clicked intermediates),
    with derived per-segment geometry.

    Add/remove operations are by waypoint, by ad-hoc lat/lon, or by index/proximity
    — together that's everything the chart click handler needs. Consecutive
    duplicates (same waypoint code, or an intermediate at the same coordinates as
    the previous point) are refused so the user can't create zero-length legs;
    non-consecutive repeats are allowed (a route may legitimately revisit a fix).
    """

    def __init__(self) -> None:
        self._points: list[RoutePoint] = []

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------

    def points(self) -> list[RoutePoint]:
        return list(self._points)

    def __len__(self) -> int:
        return len(self._points)

    def is_empty(self) -> bool:
        return not self._points

    def display_labels(self) -> list[str]:
        """Per-point display labels, with intermediate points named
        ``<previous_waypoint_code>.<N>`` (1-based ordinal, resets at each real
        waypoint).

        If an intermediate somehow precedes any real waypoint (shouldn't happen
        because :meth:`append_intermediate` refuses that case, but display logic
        has to stay total) it falls back to ``VIA.<N>``.
        """
        out: list[str] = []
        last_real_code: str | None = None
        sub_counter = 0
        for p in self._points:
            if p.waypoint is not None:
                last_real_code = p.waypoint.code
                sub_counter = 0
                out.append(p.waypoint.code)
            else:
                sub_counter += 1
                base = last_real_code if last_real_code is not None else _ORPHAN_INTERMEDIATE_BASE
                out.append(f"{base}.{sub_counter}")
        return out

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def append_waypoint(self, wp: WaypointRecord) -> bool:
        """Append a real waypoint. Returns False if the same waypoint code is already
        the last point (would produce a zero-length leg)."""
        if (
            self._points
            and self._points[-1].waypoint is not None
            and self._points[-1].waypoint.code == wp.code
        ):
            return False
        self._points.append(RoutePoint(lat=wp.lat, lon=wp.lon, waypoint=wp))
        return True

    def append_intermediate(self, lat: float, lon: float) -> bool:
        """Append a user-clicked intermediate point at ``(lat, lon)``.

        Refused (returns False) if:
        - the route is empty — an intermediate's display label is anchored to the
          *previous* real waypoint, so we need at least one waypoint in front of
          it. The caller should surface a UX message in this case.
        - the previous point is at the same coordinates (within ``_SAME_POINT_DEG_TOL``)
          — that would be a zero-length leg.
        """
        if not self._points:
            return False
        last = self._points[-1]
        if (
            abs(last.lat - lat) < _SAME_POINT_DEG_TOL
            and abs(last.lon - lon) < _SAME_POINT_DEG_TOL
        ):
            return False
        self._points.append(RoutePoint(lat=lat, lon=lon, waypoint=None))
        return True

    def remove_at(self, index: int) -> bool:
        if 0 <= index < len(self._points):
            del self._points[index]
            return True
        return False

    def nearest_index(self, lat: float, lon: float, *, max_nm: float) -> int | None:
        """Index of the route point closest to ``(lat, lon)``, or None if no point is
        within ``max_nm`` great-circle nautical miles. Considers all points equally
        — real waypoints and intermediates alike — which matches the user's intent
        that Shift+right should remove whatever route point is closest to the click."""
        best_idx: int | None = None
        best_d = max_nm
        for i, p in enumerate(self._points):
            d = great_circle_distance_nm(lat, lon, p.lat, p.lon)
            if d < best_d:
                best_d = d
                best_idx = i
        return best_idx

    def clear(self) -> None:
        self._points.clear()

    # ------------------------------------------------------------------
    # Derived: segments
    # ------------------------------------------------------------------

    def segments(
        self, *, magvar_e: float = ISRAEL_MAGNETIC_VARIATION_DEG_E
    ) -> list[RouteSegment]:
        """One :class:`RouteSegment` between each adjacent pair of route points, in
        order, with display labels resolved.

        Empty list while the route has 0 or 1 points — there's no leg to fly yet.
        """
        labels = self.display_labels()
        out: list[RouteSegment] = []
        for i in range(len(self._points) - 1):
            a, b = self._points[i], self._points[i + 1]
            out.append(
                RouteSegment(
                    from_point=a,
                    to_point=b,
                    from_label=labels[i],
                    to_label=labels[i + 1],
                    distance_nm=great_circle_distance_nm(a.lat, a.lon, b.lat, b.lon),
                    mag_bearing_deg=magnetic_bearing_deg(
                        a.lat, a.lon, b.lat, b.lon, magvar_e=magvar_e
                    ),
                )
            )
        return out


# ---------------------------------------------------------------------------
# ICAO Field 15 route-string formatting
# ---------------------------------------------------------------------------


def format_icao_coord(lat: float, lon: float) -> str:
    """Format a latitude/longitude as an ICAO Field 15 'degrees-and-minutes' point
    (11 characters).

    Per ICAO Doc 4444 Appendix 2, an explicit coordinate point in a flight-plan
    route is written as ``DDMM[N|S]DDDMM[E|W]``: 2 digits of latitude degrees +
    2 digits of latitude minutes + N/S, then 3 digits of longitude degrees + 2
    digits of longitude minutes + E/W. No spaces, no decimal point.

    Example: ``31.55°N 34.55°E → '3133N03433E'``.

    Implementation notes:
    - Minutes are *rounded* to the nearest whole minute. Sub-minute precision
      is meaningless for chart-clicked intermediates anyway (~1.85 km / 1 nm
      per minute of latitude is well below the practical click resolution).
    - The 60' carry — e.g. 31°59.7' rounds to 32°00' — is handled explicitly
      so we never emit ``3160N`` (which is malformed).
    """
    abs_lat = abs(lat)
    lat_deg = int(abs_lat)
    lat_min = int(round((abs_lat - lat_deg) * 60.0))
    if lat_min >= 60:
        lat_min -= 60
        lat_deg += 1
    ns = "N" if lat >= 0 else "S"

    abs_lon = abs(lon)
    lon_deg = int(abs_lon)
    lon_min = int(round((abs_lon - lon_deg) * 60.0))
    if lon_min >= 60:
        lon_min -= 60
        lon_deg += 1
    ew = "E" if lon >= 0 else "W"

    return f"{lat_deg:02d}{lat_min:02d}{ns}{lon_deg:03d}{lon_min:02d}{ew}"


def to_icao_route_string(route: "Route", *, include_intermediates: bool = True) -> str:
    """Render ``route`` as a single ICAO Field 15 route string.

    Real waypoints contribute their published code (e.g. ``DAROM``); intermediate
    user-clicked points contribute their coordinates per :func:`format_icao_coord`.
    Tokens are separated by single spaces — the canonical ICAO separator.

    When ``include_intermediates`` is False, intermediate points are *dropped*
    entirely (not collapsed) so the resulting string is the "filed waypoints"
    view of the route. The route's geometric totals (distance, time) remain
    based on the actual flown polyline regardless of this flag — what you file
    isn't always what you fly, and the panel surfaces both honestly.

    Returns the empty string for an empty route, so callers can use the result
    directly in display code without a None-check.
    """
    tokens: list[str] = []
    for p in route.points():
        if p.waypoint is not None:
            tokens.append(p.waypoint.code)
        elif include_intermediates:
            tokens.append(format_icao_coord(p.lat, p.lon))
    return " ".join(tokens)


def to_hebrew_route_string(route: "Route", *, include_intermediates: bool = True) -> str:
    """Render ``route`` as a single space-separated string for Hebrew paperwork.

    Real waypoints contribute their **Hebrew name** (``WaypointRecord.name_he``,
    e.g. ``דרום``), with the published code as a fallback if the OCR didn't
    recover a Hebrew name for that fix — better to show the code than to drop
    a point silently.

    Intermediate user-clicked points contribute the same ``DDMM[N|S]DDDMM[E|W]``
    coordinate token as the ICAO row (see :func:`format_icao_coord`). Holding
    the coordinate format constant across both rows keeps a leg's geometry
    instantly comparable line-to-line, and the format is internationally
    readable so it copy-pastes cleanly into Israeli flight-plan or flight-school
    paperwork without re-formatting.

    The ``include_intermediates`` flag mirrors :func:`to_icao_route_string`:
    when False, intermediate points are dropped (not collapsed). The same
    checkbox in the route panel governs both rows so the two strings always
    represent the same "what we file" view of the route.

    Returns the empty string for an empty route — callers can use the result
    directly in display code without a None-check.
    """
    tokens: list[str] = []
    for p in route.points():
        if p.waypoint is not None:
            # Fall back to the ICAO code when the back-pages OCR didn't capture
            # a Hebrew name for this fix; surfacing *something* is better than
            # silently dropping a real waypoint, and the code remains unambiguous.
            tokens.append(p.waypoint.name_he or p.waypoint.code)
        elif include_intermediates:
            tokens.append(format_icao_coord(p.lat, p.lon))
    return " ".join(tokens)


# ---------------------------------------------------------------------------
# Default Save-plan filename derivation
# ---------------------------------------------------------------------------

#: Last-resort filename used by :func:`default_save_plan_name` when a route
#: has no usable named endpoints (empty route, or — structurally impossible
#: through the public mutators but defended against here — only intermediates).
#: Matches the historical hard-coded default from before this helper landed
#: so existing user muscle memory still works for the degenerate cases.
_DEFAULT_SAVE_PLAN_FALLBACK_NAME = "flight-plan.cvfr"

#: Strict ``[A-Za-z0-9]`` whitelist used by :func:`default_save_plan_name` to
#: keep the default filename portable across Windows / Linux / macOS without
#: needing per-platform quoting. Israeli CVFR waypoint codes (LLBG, AMNON,
#: ZUKIM, …) are already ASCII alnum so this is defensive against future or
#: synthetic codes; if a sanitised endpoint strips to the empty string we
#: degrade to the generic fallback rather than ship a half-stripped name.
_FILENAME_SAFE_CHARS_RE: re.Pattern[str] = re.compile(r"[^A-Za-z0-9]")


def default_save_plan_name(route: "Route") -> str:
    """Default filename for the Save-plan dialog, derived from the route's
    first and last *named* waypoints.

    Convention: ``<origin>-<destination>.cvfr`` (e.g. ``LLIB-LLMZ.cvfr``).
    This mirrors how pilots refer to a flight plan on paper and makes the
    saved file sort adjacent to the matching ``<origin>-<destination>.ods``
    paperwork export. The user can still rename inside the Save-As dialog;
    the helper only supplies the default suggestion.

    Algorithm: walk the route's points from each end and pick the first
    point whose ``waypoint`` is not ``None`` (intermediates are skipped).
    Codes are then sanitised through an ASCII-alnum whitelist before being
    joined; the alnum check is defensive — current Israeli waypoint codes
    are all ``[A-Z]{4,5}`` by construction.

    Edge cases (kept defensive even though Save-plan is already disabled on
    empty routes by ``RoutePanel._save_plan_btn.setEnabled(has_points)``):

    * **All intermediates or empty route** — no named fix anywhere → fall
      back to ``flight-plan.cvfr`` rather than synthesise a coord-token
      filename like ``3145N03528E-3126N03523E.cvfr`` (valid on disk but
      reads poorly).
    * **Single named fix** — one-point route, *or* origin code equals
      destination code after skipping intermediates (a route that returns
      to its origin) → ``<code>.cvfr``. ``LLIB-LLIB.cvfr`` would just be
      noisier than ``LLIB.cvfr`` for the same information.
    * **Filesystem-hostile characters** — strip anything outside
      ``[A-Za-z0-9]`` from each endpoint code before joining; if either
      side strips to empty, degrade to ``flight-plan.cvfr``.

    Returns a bare filename (no path component); the caller composes it
    with the target directory.
    """
    points = route.points()

    origin_code: str | None = None
    for p in points:
        if p.waypoint is not None:
            origin_code = p.waypoint.code
            break

    dest_code: str | None = None
    for p in reversed(points):
        if p.waypoint is not None:
            dest_code = p.waypoint.code
            break

    if origin_code is None or dest_code is None:
        # All-intermediates or empty route: nothing to derive a name from.
        return _DEFAULT_SAVE_PLAN_FALLBACK_NAME

    safe_origin = _FILENAME_SAFE_CHARS_RE.sub("", origin_code)
    safe_dest = _FILENAME_SAFE_CHARS_RE.sub("", dest_code)
    if not safe_origin or not safe_dest:
        return _DEFAULT_SAVE_PLAN_FALLBACK_NAME

    if safe_origin == safe_dest:
        # One named fix overall, or a returns-to-origin route.
        return f"{safe_origin}.cvfr"

    return f"{safe_origin}-{safe_dest}.cvfr"


# ---------------------------------------------------------------------------
# ICAO Field 15 route-string parsing (inverse of the formatters above)
# ---------------------------------------------------------------------------
#
# The Save / Load Flight Plan feature persists a route as a single ICAO Field 15
# line — the exact same string the route panel already displays above the table,
# produced by :func:`to_icao_route_string` with ``include_intermediates=True``.
# That choice keeps the file format:
#
# * **Calibration-independent** — the saved data is geographic (codes + ICAO
#   coords), not pixel-space, so plans survive a re-calibration of the chart
#   without becoming gibberish.
# * **Human-readable** — a saved plan is a single line of ASCII the pilot can
#   eyeball, edit in a text editor, or paste into ATC paperwork.
# * **Trivially round-trippable** — formatter and parser share the same grammar
#   so ``parse(format(route)) == structure(route)`` is the contract pinned by
#   the test suite.
#
# Grammar (strict, intentionally narrow per user spec):
#
#     PLAN  := TOKEN (' ' TOKEN)*
#     TOKEN := AIRPORT | WAYPOINT | COORD
#     AIRPORT  := [A-Z]{4}                 # 4-letter ICAO airport code
#     WAYPOINT := [A-Z]{5}                 # 5-letter CVFR waypoint code
#     COORD    := \d{2}\d{2}[NS]\d{3}\d{2}[EW]
#                                          # 11-char ICAO Field 15 coord token
#                                          # (DD°MM'[N|S] DDD°MM'[E|W], whole min)
#
# Whitespace tolerance: outer whitespace (leading/trailing including a final
# newline) is stripped before parsing; internal separator MUST be a single
# space. Multiple consecutive spaces, tabs, or non-ASCII whitespace are
# rejected — the format is the format. Mixed-case letters are rejected so the
# stored file matches what the formatter emits exactly; this also prevents a
# class of "did you mean LLBG?" near-miss bugs from sneaking through.
#
# Code lookup (4- and 5-letter tokens to real waypoints) is intentionally NOT
# part of this parser: the parser is pure / UI-free and the waypoint database
# lives elsewhere. Callers resolve ``ParsedPlanCode.code`` against their own
# lookup and surface unknown-code errors with their own (usually UI-side)
# message.

#: Compiled regex for the 11-char ICAO Field 15 coordinate token. Capturing
#: groups give the parser cheap access to the four numeric components without
#: a second slicing pass. Anchored on both ends so a token like ``3133N03433E1``
#: is rejected outright rather than partially matched.
_ICAO_COORD_TOKEN_RE: re.Pattern[str] = re.compile(
    r"^(\d{2})(\d{2})([NS])(\d{3})(\d{2})([EW])$"
)

#: Compiled regex for 4- or 5-letter alphabetic codes. Uppercase only — see the
#: grammar note above for the rationale.
_ALPHA_CODE_TOKEN_RE: re.Pattern[str] = re.compile(r"^[A-Z]{4,5}$")


@dataclass(frozen=True)
class ParsedPlanCode:
    """A 4- or 5-letter alphabetic token parsed from a flight-plan string.

    Carries the raw uppercase code only; resolving it to a concrete
    :class:`WaypointRecord` is the caller's responsibility (the parser is
    deliberately UI-free and database-free — see module-level grammar notes).

    Attributes:
        code: The 4- or 5-letter code, guaranteed uppercase ASCII letters.
    """

    code: str


@dataclass(frozen=True)
class ParsedPlanCoord:
    """An 11-char ICAO Field 15 coordinate token parsed from a flight-plan string.

    The parser decodes the four numeric components and validates ranges (degrees
    and minutes both in their respective bounds) so callers receive a sanitized
    ``(lat, lon)`` pair they can plot directly. The original text is preserved
    on ``text`` so downstream error / status messages can quote what the user
    actually wrote rather than the lossy re-formatted form.

    Attributes:
        lat: Decimal-degree latitude, negative for South.
        lon: Decimal-degree longitude, negative for West.
        text: The original 11-char token, e.g. ``"3133N03433E"``.
    """

    lat: float
    lon: float
    text: str


#: Public alias for the union of token kinds returned by :func:`parse_icao_route_string`.
#: Wrapping in a name (rather than spelling the Union inline at every callsite)
#: keeps signatures readable when the list of kinds inevitably grows (e.g.
#: should a future spec accept seconds-precision coords as a fourth kind).
ParsedPlanToken = Union[ParsedPlanCode, ParsedPlanCoord]


class FlightPlanParseError(ValueError):
    """Raised when an input route string cannot be parsed by :func:`parse_icao_route_string`.

    Carries enough context for a UI-side error popup to point the user at the
    exact byte they need to fix — ``position`` is 1-based (the first token in
    the file is "token 1") to match how a pilot would count tokens by eye, and
    ``token`` is the raw text of the offending token (or the whole input for
    "empty plan" / "extra whitespace" cases where there's no single token to
    blame).

    The :func:`__str__` form is fit to drop straight into a
    ``QMessageBox.warning`` body — short, sentence-cased, with the position and
    the offender quoted.
    """

    def __init__(
        self, message: str, *, position: int | None = None, token: str | None = None
    ) -> None:
        super().__init__(message)
        self.position = position
        self.token = token


def parse_icao_coord_token(token: str) -> tuple[float, float]:
    """Decode an 11-char ICAO Field 15 coordinate token into ``(lat, lon)``.

    The inverse of :func:`format_icao_coord`. Strict about the canonical 11-char
    ``DDMM[NS]DDDMM[EW]`` shape with no decimal point — that's the only shape
    the formatter ever emits and the only shape the flight-plan grammar admits.
    Minutes are validated in 0-59; degrees in 0-90 (lat) / 0-180 (lon).
    The N/S/E/W hemisphere indicator is what flips sign.

    Raises:
        ValueError: when ``token`` is the wrong length / shape / out-of-range.
            (No bespoke exception class here — callers in this module wrap
            their own ``FlightPlanParseError`` around the failure with the
            higher-level position context the user sees.)
    """
    m = _ICAO_COORD_TOKEN_RE.match(token)
    if m is None:
        raise ValueError(
            f"Coordinate token must match DDMM[NS]DDDMM[EW] (11 characters); got {token!r}."
        )
    lat_deg = int(m.group(1))
    lat_min = int(m.group(2))
    ns = m.group(3)
    lon_deg = int(m.group(4))
    lon_min = int(m.group(5))
    ew = m.group(6)

    if lat_min >= 60:
        raise ValueError(
            f"Latitude minutes must be 0-59 in {token!r}; got {lat_min:02d}."
        )
    if lon_min >= 60:
        raise ValueError(
            f"Longitude minutes must be 0-59 in {token!r}; got {lon_min:02d}."
        )
    if lat_deg > 90 or (lat_deg == 90 and lat_min != 0):
        raise ValueError(f"Latitude degrees out of range in {token!r}; got {lat_deg:02d}.")
    if lon_deg > 180 or (lon_deg == 180 and lon_min != 0):
        raise ValueError(f"Longitude degrees out of range in {token!r}; got {lon_deg:03d}.")

    lat = lat_deg + lat_min / 60.0
    if ns == "S":
        lat = -lat
    lon = lon_deg + lon_min / 60.0
    if ew == "W":
        lon = -lon
    return lat, lon


def parse_icao_route_string(text: str) -> list[ParsedPlanToken]:
    """Parse an ICAO Field 15 route line into a list of structured tokens.

    The inverse of :func:`to_icao_route_string` (called with
    ``include_intermediates=True``). Designed to round-trip exactly — a string
    produced by the formatter, re-parsed through this function, yields a token
    list whose structure mirrors the source route's points one-for-one.

    Strict grammar (see module-level grammar notes for the BNF). The only
    permitted tokens are 4- or 5-letter uppercase codes and 11-char ICAO
    coordinate tokens; the only permitted separator between tokens is a single
    ASCII space. Outer whitespace (leading / trailing, including a final
    newline) is stripped before parsing so a saved file with a trailing LF
    parses identically to its in-memory form, but no other whitespace
    relaxation is allowed.

    Args:
        text: Raw route string, typically the entire contents of a saved
            flight-plan file. May contain a leading / trailing newline.

    Returns:
        A list of :class:`ParsedPlanCode` and :class:`ParsedPlanCoord` tokens
        in source order. The list is never empty on a successful return — an
        empty input raises :class:`FlightPlanParseError` instead, because an
        empty flight plan is not a meaningful save/load artefact.

    Raises:
        FlightPlanParseError: when the input does not match the grammar.
            The exception's ``position`` (1-based token index) and ``token``
            (offending substring) are set when meaningful, so the UI can
            quote the exact byte the pilot needs to fix.
    """
    stripped = text.strip()
    if not stripped:
        raise FlightPlanParseError(
            "Flight plan is empty — no waypoints or coordinates found."
        )

    # Reject any whitespace other than the single-space separator the grammar
    # specifies. Splitting on a raw " " (not str.split()) is what keeps double
    # spaces, tabs, and other whitespace from being silently collapsed into
    # token boundaries — ``"AAAA  BBBB".split()`` would return two tokens, but
    # ``"AAAA  BBBB".split(" ")`` returns three (the middle one being empty),
    # which we detect and reject.
    raw_tokens = stripped.split(" ")
    parsed: list[ParsedPlanToken] = []
    for idx, raw in enumerate(raw_tokens, start=1):
        if raw == "":
            raise FlightPlanParseError(
                "Tokens must be separated by exactly one space — found two or "
                "more consecutive spaces (or a leading/trailing space inside "
                "the plan body).",
                position=idx,
                token=raw,
            )
        if _ALPHA_CODE_TOKEN_RE.match(raw):
            parsed.append(ParsedPlanCode(code=raw))
            continue
        if _ICAO_COORD_TOKEN_RE.match(raw):
            try:
                lat, lon = parse_icao_coord_token(raw)
            except ValueError as exc:
                # Coordinate-token shape matched but a range check failed
                # (e.g. minutes 60+, latitude 91°). Surface the underlying
                # validator's message verbatim so the user sees which field
                # was the offender, but stamp position + token onto the
                # parser-level exception so the UI can format consistently.
                raise FlightPlanParseError(
                    str(exc),
                    position=idx,
                    token=raw,
                ) from exc
            parsed.append(ParsedPlanCoord(lat=lat, lon=lon, text=raw))
            continue
        raise FlightPlanParseError(
            f"Token {raw!r} is not a 4-letter airport code, a 5-letter "
            f"waypoint code, or an 11-character ICAO coordinate "
            f"(e.g. ``3133N03433E``).",
            position=idx,
            token=raw,
        )

    return parsed
