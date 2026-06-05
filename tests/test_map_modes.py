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

"""Tests for the map-mode registry (cvfr_routemaster.map_modes).

Phase 1 ships CVFR only; these tests pin the data model and the CVFR
mode's contract so the later LSA/Helicopter additions can't silently
regress the shape every other subsystem depends on.
"""

from __future__ import annotations

import pytest

from cvfr_routemaster import map_modes as mm
from cvfr_routemaster.chart_source import (
    CAAI_CHART_URLS,
    CACHE_FILENAMES,
    SHEET_KEYS,
)


def test_default_mode_is_cvfr() -> None:
    assert mm.DEFAULT_MODE_ID == "cvfr"
    assert mm.default_mode().mode_id == "cvfr"


def test_registry_lists_cvfr() -> None:
    assert "cvfr" in mm.mode_ids()
    assert mm.has_mode("cvfr")
    assert not mm.has_mode("does-not-exist")


def test_get_unknown_mode_raises() -> None:
    with pytest.raises(KeyError):
        mm.get_mode("nope")


def test_coerce_unknown_falls_back_to_default() -> None:
    assert mm.coerce_mode_id(None) == "cvfr"
    assert mm.coerce_mode_id("") == "cvfr"
    assert mm.coerce_mode_id("helicopter-not-yet") == "cvfr"
    assert mm.coerce_mode_id("cvfr") == "cvfr"


def test_cvfr_mode_matches_legacy_chart_source_constants() -> None:
    """The CVFR mode must reproduce the long-standing chart_source
    triple exactly, so the Phase 1 refactor is behavior-preserving."""
    cvfr = mm.get_mode("cvfr")
    assert cvfr.sheet_keys == SHEET_KEYS
    for key in SHEET_KEYS:
        sheet = cvfr.sheet(key)
        assert sheet.default_url == CAAI_CHART_URLS[key]
        assert sheet.cache_pdf_filename == CACHE_FILENAMES[key]


def test_cvfr_map_vs_waypoint_sheets() -> None:
    cvfr = mm.get_mode("cvfr")
    assert cvfr.map_sheet_keys == ("north", "south")
    assert cvfr.waypoint_sheet_keys == ("back",)
    assert cvfr.sheet("north").role is mm.SheetRole.MAP
    assert cvfr.sheet("south").role is mm.SheetRole.MAP
    assert cvfr.sheet("back").role is mm.SheetRole.WAYPOINTS


def test_cvfr_waypoint_strategy_is_vector_hybrid() -> None:
    assert mm.get_mode("cvfr").waypoint_strategy is mm.WaypointStrategy.VECTOR_HYBRID


def test_cvfr_waypoint_source_is_back_all_pages() -> None:
    cvfr = mm.get_mode("cvfr")
    assert len(cvfr.waypoint_sources) == 1
    ws = cvfr.waypoint_sources[0]
    assert ws.sheet_key == "back"
    assert ws.pages is None


def test_cvfr_overlap_codes() -> None:
    assert mm.get_mode("cvfr").overlap_codes == ("SDROT", "OMMER", "ENGDI")


def test_cache_namespace_is_mode_id() -> None:
    assert mm.get_mode("cvfr").cache_namespace == "cvfr"


def test_sheet_unknown_key_raises() -> None:
    with pytest.raises(KeyError):
        mm.get_mode("cvfr").sheet("nope")


# --- LSA mode (v4) ---------------------------------------------------------


def test_registry_lists_lsa_after_cvfr() -> None:
    # Insertion order drives the UI switcher order: CVFR first.
    assert mm.mode_ids() == ("cvfr", "lsa")
    assert mm.has_mode("lsa")


def test_lsa_two_map_sheets_no_back() -> None:
    lsa = mm.get_mode("lsa")
    assert lsa.sheet_keys == ("north", "south")
    assert lsa.map_sheet_keys == ("north", "south")
    assert lsa.sheet("north").role is mm.SheetRole.MAP
    assert lsa.sheet("south").role is mm.SheetRole.MAP


def test_lsa_waypoints_from_both_sheets_page_two() -> None:
    lsa = mm.get_mode("lsa")
    assert lsa.waypoint_strategy is mm.WaypointStrategy.FULL_OCR
    assert lsa.waypoint_sheet_keys == ("north", "south")
    for ws in lsa.waypoint_sources:
        assert ws.pages == (1,)  # reporting points live on page index 1


def test_lsa_cache_filenames_distinct_from_cvfr() -> None:
    lsa = mm.get_mode("lsa")
    names = {lsa.sheet(k).cache_pdf_filename for k in lsa.sheet_keys}
    assert names == {"lsa_north.pdf", "lsa_south.pdf"}


def test_lsa_urls_are_b08_edition() -> None:
    lsa = mm.get_mode("lsa")
    for key in lsa.sheet_keys:
        url = lsa.sheet(key).default_url
        assert url.startswith("https://www.gov.il/")
        assert "-08" in url


def test_lsa_overlap_codes_pin_the_seam() -> None:
    # Seam VRPs identified via dev calibration: Tel Arad / Beit Kama /
    # Nahal Bessor, spread east→west across the ~31.3-31.4°N seam band
    # and printed on both halves. Non-empty means the joint two-sheet
    # calibration prompts for these shared overlap anchors.
    assert mm.get_mode("lsa").overlap_codes == ("TARAD", "BKAMA", "NBSOR")


def test_lsa_namespace_is_lsa() -> None:
    assert mm.get_mode("lsa").cache_namespace == "lsa"
