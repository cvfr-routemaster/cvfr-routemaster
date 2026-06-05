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

"""Tests for automatic calibration anchor selection."""

from __future__ import annotations

from cvfr_routemaster.calibration_anchors import (
    candidates_for_sheet,
    select_anchor_pair,
    select_anchor_pair_from_candidates,
    select_anchors_for_sheet,
    select_anchors_from_candidates,
    select_overlap_anchors,
    _non_overlap_lat_band,
    _PREFERRED_OVERLAP_CODES,
)
from cvfr_routemaster.waypoint_types import WaypointRecord


def _r(
    i: int, code: str, lat: float, lon: float, *, reporting_type: str = ""
) -> WaypointRecord:
    return WaypointRecord(
        index=i,
        code=code,
        name_he="",
        reporting_type=reporting_type,
        lat=lat,
        lon=lon,
        lat_dms="",
        lon_dms="",
    )


def test_select_pair_prefers_mid_distance() -> None:
    """Three colinear-ish points: separation closest to target beats too-close or too-far."""
    cand = [
        _r(0, "AAA", 33.0, 35.0),
        _r(1, "BBB", 33.25, 35.0),
        _r(2, "CCC", 33.9, 35.0),
    ]
    pair = select_anchor_pair_from_candidates(
        cand, target_km=48.0, min_km=20.0, max_km=90.0
    )
    assert pair is not None
    codes = {pair[0].code, pair[1].code}
    assert codes == {"AAA", "BBB"}


def test_candidates_split_by_median_lat() -> None:
    hi = [_r(0, "N1", 33.5, 35.0), _r(1, "N2", 33.6, 35.1)]
    lo = [_r(2, "S1", 31.0, 34.8), _r(3, "S2", 31.1, 34.9)]
    all_r = hi + lo
    north = candidates_for_sheet(all_r, "north")
    south = candidates_for_sheet(all_r, "south")
    assert len(north) >= 2
    assert len(south) >= 2
    assert max(r.lat for r in south) <= min(r.lat for r in north)


def test_non_overlap_band_north_keeps_upper_two_thirds_lat() -> None:
    """North pool drops the lower third of its latitude span when enough points remain."""
    lo_s = [_r(0, "SA", 29.0, 35.0), _r(1, "SB", 29.1, 35.1)]
    hi_n = [
        _r(2, "N1", 33.55, 35.0),
        _r(3, "N2", 33.60, 35.1),
        _r(4, "N3", 33.80, 35.2),
        _r(5, "N4", 33.85, 35.3),
    ]
    all_r = lo_s + hi_n
    north = candidates_for_sheet(all_r, "north")
    codes = {r.code for r in north}
    assert codes == {"N3", "N4"}


def test_non_overlap_band_south_keeps_lower_two_thirds_lat() -> None:
    """South pool drops the upper third of its latitude span when enough points remain."""
    core_s = [_r(0, "S1", 31.00, 35.0), _r(1, "S2", 31.05, 35.1)]
    edge_s = [_r(2, "S3", 31.40, 35.2), _r(3, "S4", 31.45, 35.3)]
    hi_n = [
        _r(4, "N1", 33.50, 35.0),
        _r(5, "N2", 33.60, 35.1),
        _r(6, "N3", 33.70, 35.2),
        _r(7, "N4", 33.80, 35.3),
    ]
    all_r = core_s + edge_s + hi_n
    south = candidates_for_sheet(all_r, "south")
    codes = {r.code for r in south}
    assert codes == {"S1", "S2"}


def test_non_overlap_band_degenerate_span_falls_back() -> None:
    cand = [_r(0, "A", 33.0, 35.0), _r(1, "B", 33.0, 35.1)]
    assert _non_overlap_lat_band(cand, "north") is None


def test_arp_rows_excluded_from_anchor_pool() -> None:
    """ARP reporting types must not be chosen as calibration anchors."""
    arp_hi = _r(0, "ARPX", 33.9, 35.0, reporting_type="ARP")
    arp_lo = _r(1, "ARPY", 31.0, 35.0, reporting_type=" arp ")
    fix = [
        _r(2, "S1", 31.2, 34.8),
        _r(3, "S2", 31.3, 34.9),
        _r(4, "N1", 33.5, 35.1),
        _r(5, "N2", 33.6, 35.2),
    ]
    all_r = [arp_hi, arp_lo, *fix]
    north = candidates_for_sheet(all_r, "north")
    south = candidates_for_sheet(all_r, "south")
    codes = {r.code for r in north} | {r.code for r in south}
    assert "ARPX" not in codes and "ARPY" not in codes
    assert {"N1", "N2"} & {r.code for r in north}
    assert {"S1", "S2"} & {r.code for r in south}


def test_select_anchors_from_candidates_returns_n_unique_well_spread() -> None:
    """N-anchor picker: requested N anchors, all distinct, with the best pair seeded first.

    Uses the explicit-candidate API to bypass the median-split / lat-band filter, which
    would otherwise trim small synthetic pools below the requested count.
    """
    cand = [
        _r(0, "S0", 30.0, 34.5),
        _r(1, "S1", 30.6, 35.5),
        _r(2, "S2", 30.6, 34.7),
        _r(3, "S3", 30.0, 35.5),
        _r(4, "S4", 30.3, 35.0),
        _r(5, "S5", 30.4, 34.8),
    ]
    chosen = select_anchors_from_candidates(cand, 4)
    assert chosen is not None
    assert len(chosen) == 4
    assert len({r.code for r in chosen}) == 4


def test_select_anchors_from_candidates_caps_at_pool_size() -> None:
    """If the pool only has 3 candidates, return 3 even when 5 are requested."""
    cand = [
        _r(0, "S0", 30.0, 34.5),
        _r(1, "S1", 30.5, 35.0),
        _r(2, "S2", 30.6, 34.7),
    ]
    chosen = select_anchors_from_candidates(cand, 5)
    assert chosen is not None
    assert len(chosen) == 3
    assert {r.code for r in chosen} == {"S0", "S1", "S2"}


def test_select_anchors_extension_picks_farthest_remaining() -> None:
    """3rd anchor must maximise its minimum planar distance to the seed pair.

    A, B sit ~48 km apart along the same latitude — the sweet-spot target — so the seed
    pair is (A, B). C sits exactly between them (24 km to each); D sits 0.5° north of
    centre (60 km to each). Farthest-point extension must pick D third, not C.
    """
    cand = [
        _r(0, "A", 30.0, 34.75),
        _r(1, "B", 30.0, 35.25),
        _r(2, "C", 30.0, 35.00),
        _r(3, "D", 30.5, 35.00),
    ]
    chosen = select_anchors_from_candidates(cand, 3)
    assert chosen is not None
    assert len(chosen) == 3
    codes = [r.code for r in chosen]
    assert {codes[0], codes[1]} == {"A", "B"}, (
        f"Sweet-spot seed pair must be (A, B) at 48 km; got {codes[:2]}."
    )
    assert codes[2] == "D", (
        f"Farthest-point extension must pick D (60 km from both seeds) over "
        f"C (24 km from both seeds); got 3rd = {codes[2]}."
    )


def test_select_anchors_for_sheet_edge_only_returns_sheet_locked_codes() -> None:
    """End-to-end with ``n_overlap=0``: edge-only wrapper still returns sheet-locked
    anchors. Guards the legacy "all anchors live in the sheet's pool" behaviour for any
    caller that explicitly opts out of the shared-overlap phase."""
    from cvfr_routemaster.geo_calibration import MIN_ANCHORS

    pool = [
        _r(0, "SA", 29.5, 34.8),
        _r(1, "SB", 29.6, 35.2),
        _r(2, "SC", 29.7, 34.6),
        _r(3, "SD", 30.0, 35.4),
        _r(4, "SE", 30.4, 35.0),
        _r(5, "SF", 30.5, 34.9),
        _r(6, "NA", 33.0, 34.9),
        _r(7, "NB", 33.1, 35.0),
        _r(8, "NC", 33.2, 35.1),
        _r(9, "ND", 33.3, 35.2),
        _r(10, "NE", 33.4, 35.3),
        _r(11, "NF", 33.5, 35.4),
    ]
    chosen = select_anchors_for_sheet(pool, "south", 4, n_overlap=0)
    assert chosen is not None
    assert len(chosen) >= MIN_ANCHORS
    assert all(r.code.startswith("S") for r in chosen)


def test_select_anchors_for_sheet_appends_shared_overlap_anchors() -> None:
    """``select_anchors_for_sheet`` with the default ``n_overlap=2``
    returns the edge anchors followed by the two preferred overlap
    anchors (SDROT, ENGDI) — clicked once on each sheet so both
    affines are pinned to identical lat/lon at those points."""
    pool = [
        _r(0, "SA", 29.5, 34.8),
        _r(1, "SB", 29.6, 35.2),
        _r(2, "SC", 29.7, 34.6),
        _r(3, "SD", 30.0, 35.4),
        # The preferred overlap VRPs at their real database coordinates.
        _r(4, "SDROT", 31.5067, 34.5856),
        _r(5, "ENGDI", 31.4642, 35.3947),
        _r(6, "NA", 33.0, 34.9),
        _r(7, "NB", 33.1, 35.0),
        _r(8, "NC", 33.2, 35.1),
        _r(9, "ND", 33.3, 35.2),
    ]
    chosen_south = select_anchors_for_sheet(pool, "south", 4, n_overlap=2)
    chosen_north = select_anchors_for_sheet(pool, "north", 4, n_overlap=2)
    assert chosen_south is not None
    assert chosen_north is not None
    south_tail = chosen_south[-2:]
    north_tail = chosen_north[-2:]
    assert {r.code for r in south_tail} == {"SDROT", "ENGDI"}
    assert {r.code for r in north_tail} == {"SDROT", "ENGDI"}
    assert tuple(r.code for r in south_tail) == tuple(
        r.code for r in north_tail
    ), "Overlap-anchor ordering must match across sheets (west-to-east)."
    south_head = chosen_south[:-2]
    north_head = chosen_north[:-2]
    assert all(r.code.startswith("S") for r in south_head)
    assert all(r.code.startswith("N") for r in north_head)


def test_select_anchors_for_sheet_honours_per_mode_overlap_codes() -> None:
    """The per-mode ``preferred_overlap_codes`` override (v4 map modes)
    selects seam anchors from the caller's code list, not the CVFR
    default triangle."""
    pool = [
        _r(0, "SA", 29.5, 34.8),
        _r(1, "SB", 29.6, 35.2),
        _r(2, "SC", 29.7, 34.6),
        _r(3, "SD", 30.0, 35.4),
        # CVFR default codes present, plus a custom seam VRP.
        _r(4, "SDROT", 31.5067, 34.5856),
        _r(5, "ENGDI", 31.4642, 35.3947),
        _r(6, "ZUKIM", 31.50, 35.36),
        _r(7, "NA", 33.0, 34.9),
        _r(8, "NB", 33.1, 35.0),
        _r(9, "NC", 33.2, 35.1),
        _r(10, "ND", 33.3, 35.2),
    ]
    chosen = select_anchors_for_sheet(
        pool,
        "south",
        4,
        n_overlap=1,
        preferred_overlap_codes=("ZUKIM",),
    )
    assert chosen is not None
    assert chosen[-1].code == "ZUKIM"
    assert "SDROT" not in {r.code for r in chosen}


def test_select_anchors_for_sheet_empty_overlap_codes_is_edge_only() -> None:
    """A mode with no seam VRPs (``preferred_overlap_codes=()``) yields
    edge anchors only — no seam pinning — even if n_overlap > 0."""
    pool = [
        _r(0, "SA", 29.5, 34.8),
        _r(1, "SB", 29.6, 35.2),
        _r(2, "SC", 29.7, 34.6),
        _r(3, "SD", 30.0, 35.4),
        _r(4, "SE", 30.4, 35.0),
        _r(5, "SF", 30.5, 34.9),
        _r(6, "SDROT", 31.5067, 34.5856),
        _r(7, "ENGDI", 31.4642, 35.3947),
        _r(8, "NA", 33.0, 34.9),
        _r(9, "NB", 33.1, 35.0),
        _r(10, "NC", 33.2, 35.1),
        _r(11, "ND", 33.3, 35.2),
        _r(12, "NE", 33.4, 35.3),
        _r(13, "NF", 33.5, 35.4),
    ]
    chosen = select_anchors_for_sheet(
        pool, "south", 4, n_overlap=3, preferred_overlap_codes=()
    )
    assert chosen is not None
    assert len(chosen) == 4
    assert "SDROT" not in {r.code for r in chosen}
    assert "ENGDI" not in {r.code for r in chosen}


def test_select_anchors_for_sheet_overlap_anchors_immune_to_db_bias() -> None:
    """Regression: the previous lat-band / lon-extremes algorithms picked
    wrong fixes (YASSD, NAAMA, AMIOZ, ZMGEN) depending on which way the
    database population leaned. The code-lookup approach pins the
    overlap anchors to specific codes regardless of pool shape — verify
    by stuffing the pool with realistic-looking distractor VRPs at
    coordinates that would've fooled the earlier heuristics."""
    pool = [
        _r(0, "SA", 29.5, 34.6),
        _r(1, "SB", 29.6, 35.2),
        _r(2, "SC", 30.0, 34.5),
        _r(3, "SD", 30.2, 35.4),
        _r(4, "SE", 30.6, 34.8),
        _r(5, "SF", 30.8, 35.0),
        # The preferred overlap VRPs.
        _r(6, "SDROT", 31.5067, 34.5856),
        _r(7, "ENGDI", 31.4642, 35.3947),
        # Distractors at every coordinate that broke a previous
        # heuristic: AMIOZ just south of Omer, ZMGEN as the
        # lon-extremes seed, YASSD inside the old (too wide) lat band.
        _r(8, "AMIOZ", 31.2567, 34.7),
        _r(9, "ZMGEN", 31.2939, 34.4322),
        _r(10, "YASSD", 31.82, 34.63),
        # Heavy north bias to match the real database shape.
        *[
            _r(20 + i, f"N{i}", 32.0 + 0.05 * i, 34.7 + 0.05 * (i % 5))
            for i in range(20)
        ],
    ]
    chosen_south = select_anchors_for_sheet(pool, "south", 4, n_overlap=2)
    chosen_north = select_anchors_for_sheet(pool, "north", 4, n_overlap=2)
    assert chosen_south is not None
    assert chosen_north is not None
    # The trailing two anchors are the preferred pair, no matter how
    # the database is biased.
    assert {r.code for r in chosen_south[-2:]} == {"SDROT", "ENGDI"}
    assert {r.code for r in chosen_north[-2:]} == {"SDROT", "ENGDI"}
    # The distractors must not appear in the *overlap* (trailing two)
    # positions of either sheet — that's the bug. A distractor showing
    # up as an *edge* anchor for the sheet that owns it geographically
    # (e.g. AMIOZ on the south sheet) is fine: it's a real VRP, the
    # edge selector is allowed to pick it.
    for distractor in ("AMIOZ", "ZMGEN", "YASSD"):
        assert distractor not in {r.code for r in chosen_south[-2:]}
        assert distractor not in {r.code for r in chosen_north[-2:]}


def test_preferred_overlap_codes_default_triangle() -> None:
    """The shipped default is the user-confirmed *triangle* of overlap
    VRPs: Sderot (north edge), Omer (south edge), Ein Gedi (east).
    Hardcoded check so any future edit to the tuple stays deliberate —
    the rest of the calibration UX (instruction dialog wording, the
    "you'll click this on both sheets" prompt note, the click budget
    in main_window) all depend on this exact set."""
    assert _PREFERRED_OVERLAP_CODES == ("SDROT", "OMMER", "ENGDI")


def test_select_overlap_anchors_default_returns_triangle_west_to_east() -> None:
    """End-to-end check of the *production user experience*: with the
    default preferred list and a pool containing the three preferred
    VRPs at their real database coordinates, the selector returns all
    three, sorted west-to-east — that's the order the calibration
    prompts will present them in. Pinning the ordering here decouples
    the UX from whatever west-to-east tie-break the implementation
    happens to use."""
    pool = [
        _r(0, "SDROT", 31.5067, 34.5856),
        _r(1, "OMMER", 31.2747, 34.8283),
        _r(2, "ENGDI", 31.4642, 35.3947),
        # Distractors at various lats/lons inside and outside the band.
        _r(3, "AMIOZ", 31.2567, 34.7000),
        _r(4, "ZMGEN", 31.2939, 34.4322),
        _r(5, "BKAMA", 31.4417, 34.7656),
    ]
    chosen = select_overlap_anchors(pool)
    # Default n now comes from ``_DEFAULT_OVERLAP_ANCHORS`` — should be 3.
    assert tuple(r.code for r in chosen) == ("SDROT", "OMMER", "ENGDI")
    # Sanity check on the actual lons used for ordering, so a future
    # database refresh that nudges coordinates can't silently flip the
    # prompt sequence on the user.
    assert [r.lon for r in chosen] == sorted(r.lon for r in chosen)


def test_select_overlap_anchors_partial_n_picks_preferred_in_order() -> None:
    """Subset behaviour: asking for fewer anchors than the preferred
    list contains returns the first ``n`` codes the selector resolves,
    sorted west-to-east. Other in-band VRPs (BKAMA here) must never
    sneak in just because the caller asked for a smaller ``n``."""
    pool = [
        _r(0, "S1", 29.5, 34.8),
        _r(1, "S2", 29.8, 35.0),
        # Two of the three preferred VRPs at their real coordinates;
        # OMMER deliberately absent so n=2 cleanly yields SDROT+ENGDI.
        _r(2, "SDROT", 31.5067, 34.5856),
        _r(3, "ENGDI", 31.4642, 35.3947),
        # In-band non-preferred VRP — must NOT be picked.
        _r(4, "BKAMA", 31.4417, 34.7656),
        _r(5, "N1", 33.0, 35.1),
    ]
    chosen = select_overlap_anchors(pool, n=2)
    # SDROT (lon 34.59) and ENGDI (lon 35.39) are the only resolvable
    # preferred codes; sorted west-to-east gives (SDROT, ENGDI).
    assert tuple(r.code for r in chosen) == ("SDROT", "ENGDI")


def test_select_overlap_anchors_skips_codes_outside_lat_window() -> None:
    """Sanity-check: if a row in the database happens to have the same
    code as a preferred overlap VRP but a wildly wrong latitude (e.g. a
    publisher typo swaps coordinates), the selector skips it rather
    than blindly using the wrong fix."""
    pool = [
        # SDROT, but with a lat outside the sanity window — selector
        # must ignore it.
        _r(0, "SDROT", 31.82, 34.63),
        _r(1, "ENGDI", 31.4642, 35.3947),
    ]
    chosen = select_overlap_anchors(pool, n=2)
    # Only ENGDI passed all checks; SDROT was rejected for lat, and the
    # selector is all-or-nothing for n>=2 → empty result.
    assert chosen == ()


def test_select_overlap_anchors_rejects_amioz_even_if_named_sderot_swap() -> None:
    """Regression: the previous lat-band algorithm picked AMIOZ
    (31.2567°, 1 NM south of Omer, **not** in the overlap) as the first
    south-sheet calibration prompt. The code-lookup approach can't
    pick AMIOZ at all because it isn't in the preferred list — verify
    by including AMIOZ alongside the real preferred codes and
    asserting it never makes the cut."""
    pool = [
        _r(0, "AMIOZ", 31.2567, 34.7),
        _r(1, "SDROT", 31.5067, 34.5856),
        _r(2, "ENGDI", 31.4642, 35.3947),
    ]
    chosen = select_overlap_anchors(pool, n=2)
    codes = [r.code for r in chosen]
    assert codes == ["SDROT", "ENGDI"]
    assert "AMIOZ" not in codes


def test_select_overlap_anchors_rejects_zmgen_lon_extreme() -> None:
    """Regression: the previous lon-extremes algorithm selected ZMGEN
    (Tzomet Magen, lon 34.43° — west of Sderot at lon 34.59°) as the
    western overlap anchor, because it was the westmost VRP in the lat
    band even though ZMGEN isn't a recognisable overlap landmark. The
    code-lookup approach skips it for the same reason it skips AMIOZ."""
    pool = [
        _r(0, "ZMGEN", 31.2939, 34.4322),
        _r(1, "SDROT", 31.5067, 34.5856),
        _r(2, "ENGDI", 31.4642, 35.3947),
    ]
    chosen = select_overlap_anchors(pool, n=2)
    codes = [r.code for r in chosen]
    assert codes == ["SDROT", "ENGDI"]
    assert "ZMGEN" not in codes


def test_select_overlap_anchors_returns_empty_when_preferred_codes_missing() -> None:
    """All-or-nothing: if the database doesn't contain enough of the
    preferred codes, the selector returns empty. The caller falls back
    to edge-only calibration — better to lose the seam-pinning benefit
    than to silently substitute a wrong-sheet anchor."""
    pool = [
        _r(0, "S1", 28.0, 34.5),
        _r(1, "S2", 28.1, 34.6),
        _r(2, "SDROT", 31.5067, 34.5856),
        # No ENGDI in the database.
        _r(3, "N1", 33.0, 35.1),
    ]
    assert select_overlap_anchors(pool, n=2) == ()


def test_select_overlap_anchors_excludes_arps() -> None:
    """If a row's reporting type is ARP it gets skipped, even if the
    code matches one in the preferred list. Defensive: the shipped
    preferred list contains only mandatory-reporting fixes, but the
    filter belongs in the selector in case a future edit slips an ARP
    code in."""
    pool = [
        _r(0, "SDROT", 31.5067, 34.5856, reporting_type="ARP"),
        _r(1, "ENGDI", 31.4642, 35.3947),
    ]
    chosen = select_overlap_anchors(pool, n=2)
    assert chosen == ()


def test_select_overlap_anchors_case_insensitive_code_match() -> None:
    """Codes match case-insensitively. The shipped database uses ALL
    CAPS but the selector should still work if a caller passes a
    differently-cased preferred list, and vice versa."""
    pool = [
        _r(0, "sdrot", 31.5067, 34.5856),
        _r(1, "EnGdI", 31.4642, 35.3947),
    ]
    chosen = select_overlap_anchors(pool, n=2)
    assert tuple(r.code.casefold() for r in chosen) == ("sdrot", "engdi")


def test_select_overlap_anchors_n_above_preferred_list_size_returns_empty() -> None:
    """Asking for more anchors than the preferred list contains is a
    caller bug, but the selector handles it deterministically: returns
    empty rather than partial. The caller's edge-anchor fallback then
    kicks in."""
    pool = [
        _r(0, "SDROT", 31.5067, 34.5856),
        _r(1, "ENGDI", 31.4642, 35.3947),
    ]
    # Default preferred list has 2 entries; asking for 3 must yield empty.
    assert select_overlap_anchors(pool, n=3) == ()


def test_select_overlap_anchors_accepts_custom_preferred_codes() -> None:
    """The preferred list is overridable so future callers (or tests)
    can pick a different anchor pair without monkey-patching the
    module constant."""
    pool = [
        _r(0, "ALPHA", 31.40, 34.50),
        _r(1, "BETA", 31.40, 35.00),
        _r(2, "GAMMA", 31.40, 35.50),
    ]
    chosen = select_overlap_anchors(
        pool, n=2, preferred_codes=("ALPHA", "GAMMA")
    )
    assert tuple(r.code for r in chosen) == ("ALPHA", "GAMMA")


def test_select_anchors_for_sheet_skips_overlap_when_preferred_codes_missing() -> None:
    """If the database doesn't contain the preferred overlap codes (e.g.
    a deliberately minimal pool, a future chart version that renames
    them, etc.), the selector must fall back gracefully to edge-only
    anchors rather than fail the calibration or substitute some other
    overlap-strip VRP that isn't in the preferred list."""
    pool = [
        _r(0, "SA", 28.0, 34.8),
        _r(1, "SB", 28.1, 35.2),
        _r(2, "SC", 28.2, 34.6),
        _r(3, "SD", 28.3, 35.4),
        # Only one of the two preferred codes is present.
        _r(4, "SDROT", 31.5067, 34.5856),
        _r(5, "NA", 35.0, 34.9),
        _r(6, "NB", 35.1, 35.0),
        _r(7, "NC", 35.2, 35.1),
        _r(8, "ND", 35.3, 35.2),
    ]
    south_chosen = select_anchors_for_sheet(pool, "south", 4, n_overlap=2)
    assert south_chosen is not None
    south_codes = {r.code for r in south_chosen}
    # SDROT alone is not enough; selector returns ``()`` from the
    # overlap phase and the wrapper falls back to edge-only.
    assert "SDROT" not in south_codes
