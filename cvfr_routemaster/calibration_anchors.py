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
Pick waypoint records to use as fixed anchors for chart calibration.

The live calibration uses the affine LSQ fit in :mod:`cvfr_routemaster.geo_calibration`,
which needs ``MIN_ANCHORS`` points (currently 3) and benefits from extra anchors as click-
noise averaging. :func:`select_anchors_for_sheet` is therefore the entry point — it
returns ``N + n_overlap`` well-distributed anchor waypoints for a sheet via three stages:

1. **Edge anchors.** Filter the database by sheet (north vs south, by latitude split with
   fallbacks), and prefer candidates *away* from the strip where the two chart sheets
   overlap: the **upper 2/3** of the north sheet's latitude span and the **lower 2/3** of
   the south sheet's span. Airport **ARP** reporting rows are excluded — they are markedly
   worse triangle-click targets than enroute fixes. Seed with a pair whose separation is in
   a "sweet spot" — far enough apart that a sub-pixel click error does not rotate the model
   much, but close enough that chart non-linearity does not dominate — then **farthest-
   point-sample** the rest: each next anchor maximises its minimum planar distance to the
   anchors already chosen. The resulting spread keeps LSQ error small near chart edges (the
   regime where a centre-clustered anchor set drifted on south-chart fixes like EILAT/LLER).

2. **Shared overlap anchors.** Pick a small, deterministic set of waypoints inside the
   *overlap strip* (a narrow band of latitudes centred on the waypoint pool's median).
   These are returned as the same records for both ``"north"`` and ``"south"`` calls, so
   the user clicks each overlap anchor **once on each sheet** during calibration; the
   resulting affines are then pinned to identical lat/lon at those points and *interpolate*
   across the seam instead of extrapolating. This is what eliminates the satellite-tile
   discontinuity that the edge-anchor-only flow used to produce in the Dead Sea / coastline
   region (where both affines were extrapolating tens of pixels into the overlap and
   disagreeing).

3. **Combine.** Edge anchors come first (the user clicks the far-edge fixes first while the
   reticle aim is fresh), then shared overlap anchors in west-to-east lon order (so the
   user's eye sweeps the seam left → right on each sheet, building consistent muscle
   memory across both calibrations).

The pair-selecting helpers (:func:`select_anchor_pair`, :func:`select_anchor_pair_from_candidates`)
are kept as the seeding step and as a building block for tests; they are not used directly
to build a calibration.
"""

from __future__ import annotations

import math

from cvfr_routemaster.geo_calibration import MIN_ANCHORS
from cvfr_routemaster.waypoint_types import WaypointRecord

_KM_PER_DEG_LAT = 111.0

#: Allowlist of waypoint codes used as **shared overlap anchors** — clicked
#: once on each sheet during calibration so both affines are pinned to
#: identical lat/lon at those points. The ordering here is the order
#: prompts are presented to the user; we keep them west-to-east so the
#: visual sweep during calibration matches the published-chart layout.
#:
#: Each entry is a mandatory ("חובה") VRP printed on both Israeli CVFR
#: sheets:
#:
#: * ``SDROT`` — Sderot (~31.51°N, 34.59°E). The **northmost** point in
#:   the overlap strip. Mandatory reporting point, large town, easy
#:   triangle to spot on both sheets.
#: * ``OMMER`` — Omer (~31.27°N, 34.83°E). The **southmost** point in
#:   the overlap strip. Mandatory reporting point, a town just north of
#:   Beer Sheva.
#: * ``ENGDI`` — Ein Gedi (~31.46°N, 35.39°E). The **eastmost** point in
#:   the overlap strip. Mandatory reporting point, distinctive Dead Sea
#:   coastline location.
#:
#: Three anchors form a triangle that fully brackets the overlap strip in
#: latitude (Sderot at the top, Omer at the bottom) and in longitude
#: (Sderot west, Ein Gedi east). Two were enough to pin translation and
#: east-west tilt, but they sat in the top 20% of the strip in lat and
#: left the seam-middle (~lat 31.39°, where the UV-distance partition
#: flips tile ownership) effectively extrapolated. The third anchor
#: makes the seam-middle a strictly interior point of the anchor hull,
#: which is what the second-order Taylor remainder of an affine fit
#: actually cares about.
#:
#: We pick by **code**, not by any latitude / longitude heuristic, because
#: the database contains plenty of fixes inside the overlap latitude band
#: that aren't genuinely on both charts. The earlier "westmost waypoint in
#: the lat band" heuristic would for example have selected ``ZMGEN``
#: (Tzomet Magen at lon 34.43°, well west of Sderot at lon 34.59°) instead
#: of ``SDROT`` — and ``ZMGEN`` is *not* a reliable overlap landmark even
#: though its latitude is in range. Similarly, ``AMIOZ`` at lat 31.26° is
#: only 0.018° south of Omer (the southmost actual overlap VRP) and would
#: slip through any lat-band-only filter.
#:
#: To add a fourth (or replace one of these), update the tuple. The list
#: must contain mandatory-reporting VRPs that print on *both* sheets — if
#: in doubt, verify by eye on the published chart before adding. Note
#: that diminishing returns kick in past the third anchor: the strip is
#: already fully bracketed in both lat and lon, so a fourth mostly buys
#: LSQ-noise averaging (~√(3/4) ≈ 13% reduction) without further
#: shrinking the geometric extrapolation gap.
_PREFERRED_OVERLAP_CODES: tuple[str, ...] = ("SDROT", "OMMER", "ENGDI")

#: Sanity-check latitude window. The preferred codes above are looked up
#: in the waypoint database by code, but the lookup also asserts the lat
#: falls inside this window — a defence in depth against a publisher
#: typo that would silently change a VRP's coordinates (e.g. swapping
#: Sderot's lat with Ashdod Yam's). The window is wider than strictly
#: necessary for the two preferred codes; tightening it doesn't buy more
#: safety because the preferred list is already explicit.
_OVERLAP_LAT_MIN_DEG: float = 31.27
_OVERLAP_LAT_MAX_DEG: float = 31.55

#: Default number of *shared* overlap anchors per sheet. The user clicks each
#: of these on **both** sheets, so this directly costs ``2 × n_overlap`` extra
#: clicks across both calibrations. Three anchors (Sderot + Ein Gedi + Omer)
#: form a triangle that brackets the seam strip in both lat and lon, so the
#: seam-middle becomes a strictly interior point of the anchor hull — that's
#: what the second-order Taylor remainder of the affine fit cares about. Two
#: were the original ship value and pinned only the strip's north edge, which
#: left the seam-middle extrapolated.
_DEFAULT_OVERLAP_ANCHORS: int = 3


def _is_arp_reporting_type(reporting_type: str) -> bool:
    t = (reporting_type or "").strip().casefold()
    return t == "arp"


def _records_for_anchor_pool(records: list[WaypointRecord]) -> list[WaypointRecord]:
    """Drop ARP rows — airport reference points are poor triangle-click targets vs enroute fixes."""
    return [r for r in records if not _is_arp_reporting_type(r.reporting_type)]


def _planar_km(a: WaypointRecord, b: WaypointRecord, mid_lat_rad: float) -> float:
    """Approximate great-circle distance in km using local equirectangular scaling."""
    k = math.cos(mid_lat_rad)
    dx = (a.lon - b.lon) * k * _KM_PER_DEG_LAT
    dy = (a.lat - b.lat) * _KM_PER_DEG_LAT
    return math.hypot(dx, dy)


def _non_overlap_lat_band(
    cand: list[WaypointRecord], sheet_id: str
) -> list[WaypointRecord] | None:
    """Keep north waypoints in the upper 2/3 of this pool’s lat range, south in the lower 2/3.

    Assumes ``cand`` is already the median-split half for that sheet. The excluded third is
    the edge toward the other sheet (overlap on the chart).
    """
    if len(cand) < 2 or sheet_id not in ("north", "south"):
        return None
    lat_lo = min(r.lat for r in cand)
    lat_hi = max(r.lat for r in cand)
    span = lat_hi - lat_lo
    if span < 1e-7:
        return None
    if sheet_id == "north":
        cutoff = lat_lo + span / 3.0
        out = [r for r in cand if r.lat >= cutoff]
    else:
        cutoff = lat_lo + (2.0 / 3.0) * span
        out = [r for r in cand if r.lat <= cutoff]
    return out if len(out) >= 2 else None


def candidates_for_sheet(records: list[WaypointRecord], sheet_id: str) -> list[WaypointRecord]:
    """Heuristic pool: north chart ≈ higher latitude half of the list, south ≈ lower half."""
    records = _records_for_anchor_pool(records)
    if len(records) < 2:
        return list(records)
    if sheet_id not in ("north", "south"):
        return list(records)

    lats = sorted(r.lat for r in records)
    med = lats[len(lats) // 2]
    lat_buffer = 0.35

    if sheet_id == "north":
        cand = [r for r in records if r.lat >= med]
        if len(cand) < 2:
            cand = [r for r in records if r.lat >= med - lat_buffer]
    else:
        cand = [r for r in records if r.lat < med]
        if len(cand) < 2:
            cand = [r for r in records if r.lat < med + lat_buffer]

    if len(cand) < 2:
        return list(records)
    band = _non_overlap_lat_band(cand, sheet_id)
    if band is not None:
        return band
    return cand


def select_anchor_pair_from_candidates(
    cand: list[WaypointRecord],
    *,
    target_km: float = 48.0,
    min_km: float = 24.0,
    max_km: float = 88.0,
) -> tuple[WaypointRecord, WaypointRecord] | None:
    """
    Pick the best pair from an explicit candidate list (tests and advanced callers).

    Preference: planar distance closest to ``target_km`` while staying in [min_km, max_km].
    If no pair fits, relax bounds.
    """
    if len(cand) < 2:
        return None

    mid_lat = sum(r.lat for r in cand) / len(cand)
    mid_rad = math.radians(mid_lat)

    def scan_band(lo: float, hi: float) -> tuple[WaypointRecord, WaypointRecord] | None:
        best_pair: tuple[WaypointRecord, WaypointRecord] | None = None
        best_score = float("inf")
        n = len(cand)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = cand[i], cand[j]
                d = _planar_km(a, b, mid_rad)
                if d < lo or d > hi:
                    continue
                score = abs(d - target_km)
                if score < best_score:
                    best_score = score
                    best_pair = (a, b)
        return best_pair

    pair = scan_band(min_km, max_km)
    if pair is None:
        pair = scan_band(18.0, 110.0)
    if pair is None:
        pair = scan_band(12.0, 180.0)

    if pair is None:
        return None

    a, b = pair
    if a.code.casefold() > b.code.casefold():
        a, b = b, a
    return (a, b)


def select_anchor_pair(
    records: list[WaypointRecord],
    sheet_id: str,
    *,
    target_km: float = 48.0,
    min_km: float = 24.0,
    max_km: float = 88.0,
) -> tuple[WaypointRecord, WaypointRecord] | None:
    """Return two distinct waypoints for ``sheet_id``, ordered by ICAO code (deterministic)."""
    cand = candidates_for_sheet(records, sheet_id)
    return select_anchor_pair_from_candidates(
        cand, target_km=target_km, min_km=min_km, max_km=max_km
    )


def select_anchors_from_candidates(
    cand: list[WaypointRecord],
    n: int,
    *,
    target_km: float = 48.0,
    min_km: float = 24.0,
    max_km: float = 88.0,
) -> tuple[WaypointRecord, ...] | None:
    """Pick up to N well-spread anchors from an explicit candidate list (tests / advanced).

    1. Seed with the best pair from :func:`select_anchor_pair_from_candidates` — sweet-spot
       separation, robust to click error.
    2. Greedily extend by **farthest-point sampling**: each next pick maximises its
       minimum planar distance to the anchors already chosen. This produces a
       well-distributed set that keeps LSQ error small near the chart edges.

    Returns ``None`` if the pool yields fewer than ``MIN_ANCHORS`` candidates. Returns
    fewer than ``n`` anchors if the pool is small (capped at ``len(cand)``, but never
    fewer than ``MIN_ANCHORS``).
    """
    if n < MIN_ANCHORS:
        raise ValueError(f"Need at least {MIN_ANCHORS} anchors.")
    if len(cand) < MIN_ANCHORS:
        return None
    seed = select_anchor_pair_from_candidates(
        cand, target_km=target_km, min_km=min_km, max_km=max_km
    )
    if seed is None:
        return None

    chosen: list[WaypointRecord] = list(seed)
    chosen_codes = {r.code.casefold() for r in chosen}
    target = min(n, len(cand))
    if target <= 2:
        return tuple(chosen)

    mid_lat = sum(r.lat for r in cand) / len(cand)
    mid_rad = math.radians(mid_lat)

    while len(chosen) < target:
        best: WaypointRecord | None = None
        best_min_d = -1.0
        for r in cand:
            if r.code.casefold() in chosen_codes:
                continue
            min_d = min(_planar_km(r, c, mid_rad) for c in chosen)
            if min_d > best_min_d:
                best_min_d = min_d
                best = r
        if best is None:
            break
        chosen.append(best)
        chosen_codes.add(best.code.casefold())

    return tuple(chosen)


def select_overlap_anchors(
    records: list[WaypointRecord],
    n: int = _DEFAULT_OVERLAP_ANCHORS,
    *,
    preferred_codes: tuple[str, ...] = _PREFERRED_OVERLAP_CODES,
    lat_min_deg: float = _OVERLAP_LAT_MIN_DEG,
    lat_max_deg: float = _OVERLAP_LAT_MAX_DEG,
) -> tuple[WaypointRecord, ...]:
    """Return ``n`` waypoints from the preferred-overlap allowlist — to be
    clicked **once on each sheet** during calibration.

    Pinning both calibrations to the same lat/lon at these points forces
    the two affines to **interpolate** rather than extrapolate across the
    seam. Without overlap anchors, both sheets' affines have to extrapolate
    by tens of kilometres into the strip from their edge-anchor centroids;
    the extrapolations disagree, and that disagreement is what shows up as
    the visible satellite-tile discontinuity along the seam.

    Resolution rules (in order):

    1. Iterate ``preferred_codes`` in order and look each code up in
       ``records`` by case-folded equality. Skip codes that are absent,
       ARP rows, or outside the ``[lat_min_deg, lat_max_deg]`` sanity
       window.
    2. If at least ``n`` codes resolved cleanly, return the first ``n``
       sorted west-to-east (so calibration prompts present them in the
       same lon order on both sheets — consistent muscle memory).
    3. Otherwise return an empty tuple. We deliberately **do not** fall
       back to a lat-band heuristic: that's the algorithm that picked
       ZMGEN / AMIOZ / etc. instead of the actual chart-overlap VRPs,
       and silently selecting a wrong-sheet anchor would defeat the
       point of the shared-anchor mechanism.

    Returning ``()`` when the preferred list can't be resolved is by
    design — better to lose the seam-pinning benefit (calibration falls
    back to edge anchors only) than to pin both sheets to a waypoint that
    physically isn't printed on one of them.
    """
    if n < 1:
        return tuple()
    if n > len(preferred_codes):
        return tuple()

    code_lookup: dict[str, WaypointRecord] = {}
    for r in records:
        key = r.code.casefold()
        if key not in code_lookup:
            code_lookup[key] = r

    chosen: list[WaypointRecord] = []
    for code in preferred_codes:
        rec = code_lookup.get(code.casefold())
        if rec is None:
            continue
        if _is_arp_reporting_type(rec.reporting_type):
            continue
        if not (lat_min_deg - 1e-9 <= rec.lat <= lat_max_deg + 1e-9):
            continue
        chosen.append(rec)

    if len(chosen) < n:
        return tuple()
    chosen = chosen[:n]
    return tuple(sorted(chosen, key=lambda r: (r.lon, r.code.casefold())))


def select_anchors_for_sheet(
    records: list[WaypointRecord],
    sheet_id: str,
    n: int,
    *,
    n_overlap: int = _DEFAULT_OVERLAP_ANCHORS,
    preferred_overlap_codes: tuple[str, ...] = _PREFERRED_OVERLAP_CODES,
    target_km: float = 48.0,
    min_km: float = 24.0,
    max_km: float = 88.0,
) -> tuple[WaypointRecord, ...] | None:
    """Return ``N`` edge anchors followed by ``n_overlap`` shared overlap anchors.

    Edge anchors are picked via :func:`candidates_for_sheet` (median split +
    non-overlap lat band + ARP exclusion) then
    :func:`select_anchors_from_candidates` — the existing far-edge, well-spread
    selection that worked correctly before; nothing changes for sheets where
    the overlap-anchor phase yields nothing.

    Overlap anchors are picked via :func:`select_overlap_anchors` and are
    **deterministic in ``records``** — same waypoint database → same overlap
    anchors regardless of which sheet asks. The user therefore sees the same
    overlap-anchor names in the same west-to-east order on both calibrations,
    builds muscle memory for the targets, and ends up with two calibrations
    pinned to identical lat/lon at those points (eliminating seam drift).

    Overlap anchors that happen to collide with edge-anchor codes are dropped
    from the overlap set — a single waypoint can't be clicked twice with the
    same lat/lon and yield independent fit information. In practice the
    edge / overlap pools are disjoint (the lat-band filters exclude each
    other) so this safety net almost never fires.

    Pass ``n_overlap=0`` to opt out of the shared-anchor phase entirely
    (used by tests that want to exercise the edge-anchor pipeline in
    isolation).

    Returns ``None`` only when the *edge* phase fails (too few candidates
    for ``N`` anchors). A working edge set + empty overlap set is still a
    valid return — the calibration will work, just without the seam
    pinning benefit.
    """
    cand = candidates_for_sheet(records, sheet_id)
    edge = select_anchors_from_candidates(
        cand, n, target_km=target_km, min_km=min_km, max_km=max_km
    )
    if edge is None:
        return None
    if n_overlap < 1 or not preferred_overlap_codes:
        return edge
    overlap = select_overlap_anchors(
        records, n=n_overlap, preferred_codes=preferred_overlap_codes
    )
    if not overlap:
        return edge
    edge_codes = {r.code.casefold() for r in edge}
    overlap = tuple(r for r in overlap if r.code.casefold() not in edge_codes)
    return tuple(edge) + overlap
