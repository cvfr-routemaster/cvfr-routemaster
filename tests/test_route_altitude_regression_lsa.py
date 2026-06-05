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

"""Route-altitude regression for the **LSA** map mode.

Same source-of-truth contract as ``test_route_altitude_regression.py``
(CVFR), against the LSA snapshot fixtures under
``tests/fixtures/lsa_altitude_regression/``. Shared loaders live in
``tests/altitude_regression_support.py``.

The per-leg ground-truth tuples below are confirmed by the user reading
the printed LSA chart arrows. To add a verified route:

1. Plot it in the app on the LSA map; note each leg's altitude(s).
2. Append a ``(tokens, expected, label)`` entry to ``GROUND_TRUTH_ROUTES``.
   ``tokens`` are 5-letter codes and/or ICAO ``DDMM[NS]DDDMM[EW]`` coords;
   ``expected`` is one ``(from, to, (alts,))`` triple per leg (``()`` =
   "unknown"/no chart arrow for that leg).

Until at least one route is logged, the parametrized test is skipped (the
fixture-sanity tests still run, so a broken calibration/arrow snapshot is
caught immediately).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.altitude_regression_support import (
    REQUIRED_FIXTURES,
    build_matcher_geo,
    load_waypoints,
    verdicts_from_tokens,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "lsa_altitude_regression"


# (tokens, expected_triples, human_label). Populate from user-verified
# LSA chart readings — see module docstring. Each expected entry is
# (from_label, to_label, altitudes_ft_tuple).
GROUND_TRUTH_ROUTES: list[
    tuple[list[str], list[tuple[str, str, tuple[int, ...]]], str]
] = [
    # Negev: Ein Yahav (LLEY) up to Teyman/LLBS, user-confirmed off the
    # printed LSA chart (2026-06-03). Two legs read "unknown": LLEY→ZOFAR
    # has no arrow on the chart, and BOKER→TLLIM's only arrow lies ~0.70 nm
    # cross-track — 0.05 nm past the 0.65 nm pickup radius — because the
    # charted route curves while our segment is the straight BOKER→TLLIM
    # chord. (The 0.65 nm radius is calibrated against CVFR; we keep it.)
    (
        [
            "LLEY", "ZOFAR", "HRGVS", "RUHOT", "KNFHA", "OVDAT", "BOKER",
            "TLLIM", "NEGEV", "HOVAV", "OHLIM", "ZSARA", "HTIVA", "NCITY",
            "KUVSH", "LLBS",
        ],
        [
            ("LLEY", "ZOFAR", ()),
            ("ZOFAR", "HRGVS", (2900,)),
            ("HRGVS", "RUHOT", (3500,)),
            ("RUHOT", "KNFHA", (3500,)),
            ("KNFHA", "OVDAT", (3000,)),
            ("OVDAT", "BOKER", (3000,)),
            ("BOKER", "TLLIM", ()),
            ("TLLIM", "NEGEV", (2500,)),
            ("NEGEV", "HOVAV", (2000,)),
            ("HOVAV", "OHLIM", (2000,)),
            ("OHLIM", "ZSARA", (2000,)),
            ("ZSARA", "HTIVA", (2000,)),
            ("HTIVA", "NCITY", (2000,)),
            ("NCITY", "KUVSH", (2000,)),
            ("KUVSH", "LLBS", (1000,)),
        ],
        "Negev LLEY->LLBS 2026-06-03",
    ),
    # Same BOKER→TLLIM curved leg, split at a user-provided intermediate
    # (3057N03447E). With the intermediate, BOKER→3057N03447E now picks up
    # the 3000 ft arrow (segment runs along the charted curve), and the
    # second half 3057N03447E→TLLIM has no arrow — user-confirmed. This
    # pins the curved-leg behaviour: the full-route "unknown" above is a
    # geometry artefact, not a missing arrow.
    (
        ["BOKER", "3057N03447E", "TLLIM"],
        [
            ("BOKER", "3057N03447E", (3000,)),
            ("3057N03447E", "TLLIM", ()),
        ],
        "Negev BOKER->TLLIM curved-leg split 2026-06-03",
    ),
    # Dead Sea western shore: Metzada (LLMZ) north past Ein Gedi (ENGDI),
    # Mitzpe Shalem (SHALM), Zukim, up the rift to the Samaria hills and
    # out to LLES, user-confirmed off both printed sheets (2026-06-03).
    # Notable seam case: ENGDI->SHALM and SHALM->ZUKIM read 800 from
    # *north-sheet* northbound arrows in the north/south overlap band. The
    # south sheet's coastal coverage ends mid-leg (last arrow ~31.50N) with
    # nothing there, so the matcher correctly takes the north sheet's
    # authoritative label across the seam rather than a phantom. The two
    # "unknown" legs (3145N03528E->ALMOG, 3149N03522E->DUMIM) have no chart
    # arrow on those sub-segments. ALMOG and DUMIM are reached via ICAO
    # intermediates the user clicked to follow the curving rift route.
    (
        [
            "LLMZ", "ENGDI", "SHALM", "ZUKIM", "3145N03528E", "ALMOG",
            "3149N03522E", "DUMIM", "SHAHR", "ALLON", "FAZEL",
            "3205N03530E", "ZADAM", "MCORA", "IZHRE", "IZHRW", "KEDUM",
            "BRAON", "KCUCH", "AZRIL", "LLES",
        ],
        [
            ("LLMZ", "ENGDI", (800,)),
            ("ENGDI", "SHALM", (800,)),
            ("SHALM", "ZUKIM", (800,)),
            ("ZUKIM", "3145N03528E", (800,)),
            ("3145N03528E", "ALMOG", ()),
            ("ALMOG", "3149N03522E", (1900,)),
            ("3149N03522E", "DUMIM", ()),
            ("DUMIM", "SHAHR", (3000,)),
            ("SHAHR", "ALLON", (3000,)),
            ("ALLON", "FAZEL", (3000,)),
            ("FAZEL", "3205N03530E", (800,)),
            ("3205N03530E", "ZADAM", (800,)),
            ("ZADAM", "MCORA", (800,)),
            ("MCORA", "IZHRE", (2800,)),
            ("IZHRE", "IZHRW", (2800,)),
            ("IZHRW", "KEDUM", (2400,)),
            ("KEDUM", "BRAON", (1800,)),
            ("BRAON", "KCUCH", (1400,)),
            ("KCUCH", "AZRIL", (1000,)),
            ("AZRIL", "LLES", ()),
        ],
        "Dead Sea LLMZ->LLES 2026-06-03",
    ),
    # Golan + Sea of Galilee: Qiryat Shmona (LLKS) over the Hermon foothills,
    # down the Golan ridge, around the eastern Kinneret shore and out to Rosh
    # Pina (LLIB), user-confirmed (2026-06-04). Two free-clicked ICAO
    # intermediates (BNTAL.1=3307N03546E, ZBIZD.1=3253N03539E) follow the
    # curving ridge/shore route. Stacked band MIGDL->DESHE reads (2000, 800)
    # — the chart's "2000/800" two-altitude arrow. The "unknown" legs
    # (3307N03546E->AVITL, ZBIMS->ZBIZD, ZBIZD->3253N03539E, ZAMID->LLIB)
    # have no chart arrow on those sub-segments.
    (
        [
            "LLKS", "HGSRM", "SNNIR", "ZMSDA", "BNTAL", "3307N03546E",
            "AVITL", "NAFCH", "ZBIMS", "ZBIZD", "3253N03539E", "ENGEV",
            "HAONN", "ZEMAH", "MIGDL", "DESHE", "ZAMID", "LLIB",
        ],
        [
            ("LLKS", "HGSRM", (1700,)),
            ("HGSRM", "SNNIR", (5000,)),
            ("SNNIR", "ZMSDA", (5000,)),
            ("ZMSDA", "BNTAL", (5000,)),
            ("BNTAL", "3307N03546E", (5000,)),
            ("3307N03546E", "AVITL", ()),
            ("AVITL", "NAFCH", (4200,)),
            ("NAFCH", "ZBIMS", (3300,)),
            ("ZBIMS", "ZBIZD", ()),
            ("ZBIZD", "3253N03539E", ()),
            ("3253N03539E", "ENGEV", (1700,)),
            ("ENGEV", "HAONN", (1300,)),
            ("HAONN", "ZEMAH", (1300,)),
            ("ZEMAH", "MIGDL", (800,)),
            ("MIGDL", "DESHE", (2000, 800)),
            ("DESHE", "ZAMID", (2000,)),
            ("ZAMID", "LLIB", ()),
        ],
        "Golan-Kinneret LLKS->LLIB 2026-06-04",
    ),
    # Lower Galilee + Jezreel: Haifa-area exit (LLHA) east over the hills to
    # Tavor, then southwest down to Megiddo (LLMG), user-confirmed
    # (2026-06-04). This route pins the LEFT-side wide-corridor rescue:
    # NIRYA->ZMGID is a westbound leg whose 1700 ft arrow is printed ~0.77 nm
    # SOUTH (left) of the route line — perfectly parallel (0 deg) with zero
    # overshoot, but 0.12 nm past the 0.65 nm primary radius. The phase-4
    # rescue previously admitted parallel-right arrows only; it now also
    # admits parallel-left arrows within a tight 0.90 nm cap, so this leg
    # reads 1700 instead of unknown. TAVOR.1=3238N03520E is the free-clicked
    # intermediate; the unknown legs (LLHA->GILAM, 3238N03520E->MRHVA,
    # ZMGID->LLMG) have no chart arrow on those sub-segments.
    (
        [
            "LLHA", "GILAM", "EVLYM", "MOVIL", "KKANA", "ZGLNI", "TAVOR",
            "3238N03520E", "MRHVA", "NIRYA", "ZMGID", "LLMG",
        ],
        [
            ("LLHA", "GILAM", ()),
            ("GILAM", "EVLYM", (1300,)),
            ("EVLYM", "MOVIL", (1300,)),
            ("MOVIL", "KKANA", (1700,)),
            ("KKANA", "ZGLNI", (1700,)),
            ("ZGLNI", "TAVOR", (1700,)),
            ("TAVOR", "3238N03520E", (1700,)),
            ("3238N03520E", "MRHVA", ()),
            ("MRHVA", "NIRYA", (1700,)),
            ("NIRYA", "ZMGID", (1700,)),
            ("ZMGID", "LLMG", ()),
        ],
        "Lower Galilee LLHA->LLMG 2026-06-04",
    ),
]


@pytest.fixture(scope="module")
def matcher_geo() -> dict[str, list]:
    return build_matcher_geo(FIXTURES_DIR)


@pytest.fixture(scope="module")
def waypoints():
    return load_waypoints(FIXTURES_DIR)


# ---------------------------------------------------------------------------
# Fixture sanity (active now — independent of any ground-truth route)
# ---------------------------------------------------------------------------


def test_fixtures_directory_contains_required_snapshots() -> None:
    missing = [f for f in REQUIRED_FIXTURES if not (FIXTURES_DIR / f).is_file()]
    assert missing == [], (
        f"missing LSA regression fixtures in {FIXTURES_DIR}: {missing}. "
        "Restore them from .cvfr_routemaster/lsa/ if accidentally deleted."
    )


def test_geo_arrows_built_for_both_sheets(matcher_geo: dict[str, list]) -> None:
    """Both LSA sheets must contribute projected arrows; a missing sheet
    would silently turn altitude assertions into spurious unknowns."""
    for sheet in ("north", "south"):
        assert sheet in matcher_geo, f"{sheet}-sheet LSA arrows missing — calibration?"
        assert len(matcher_geo[sheet]) > 0, f"{sheet}-sheet LSA arrow list is empty"


# ---------------------------------------------------------------------------
# User-verified ground-truth routes (parametrized; skipped until populated)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not GROUND_TRUTH_ROUTES,
    reason="no user-verified LSA ground-truth routes logged yet",
)
@pytest.mark.parametrize(
    "tokens, expected, label",
    GROUND_TRUTH_ROUTES,
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_lsa_route_against_user_ground_truth(
    tokens: list[str],
    expected: list[tuple[str, str, tuple[int, ...]]],
    label: str,
    waypoints,
    matcher_geo: dict[str, list],
) -> None:
    actual = verdicts_from_tokens(tokens, waypoints, matcher_geo)
    assert actual == expected, f"LSA route mismatch for {label!r}"
