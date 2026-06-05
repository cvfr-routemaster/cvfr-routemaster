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

"""Tests for the multi-source waypoint cache (LSA: north+south sources)."""

from __future__ import annotations

import time
from pathlib import Path

from cvfr_routemaster.waypoint_cache import (
    cache_file_path,
    load_cached_waypoints,
    load_cached_waypoints_multi,
    save_waypoint_cache_multi,
)
from cvfr_routemaster.waypoint_types import WaypointRecord


def _recs() -> list[WaypointRecord]:
    return [
        WaypointRecord(1, "ALFA", "א", "חובה", 31.0, 34.0, "a", "b"),
        WaypointRecord(2, "BRAVO", "ב", "דרישה", 32.0, 35.0, "c", "d"),
    ]


def _mk(tmp_path: Path, name: str, data: bytes = b"%PDF-1.4") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_multi_round_trip(tmp_path: Path) -> None:
    n = _mk(tmp_path, "n.pdf")
    s = _mk(tmp_path, "s.pdf")
    sources = [("north", n), ("south", s)]

    save_waypoint_cache_multi(tmp_path, sources, _recs(), "ocr", "lsa")
    out = load_cached_waypoints_multi(tmp_path, sources, "lsa")

    assert out is not None
    assert [r.code for r in out] == ["ALFA", "BRAVO"]
    # Lives under the per-mode namespace.
    assert cache_file_path(tmp_path, "lsa").is_file()


def test_multi_invalidated_when_a_source_changes(tmp_path: Path) -> None:
    n = _mk(tmp_path, "n.pdf")
    s = _mk(tmp_path, "s.pdf")
    sources = [("north", n), ("south", s)]
    save_waypoint_cache_multi(tmp_path, sources, _recs(), "ocr", "lsa")

    # Mutate the south PDF's bytes (and therefore mtime/size).
    time.sleep(0.01)
    s.write_bytes(b"%PDF-1.4 changed-bytes-here")

    assert load_cached_waypoints_multi(tmp_path, sources, "lsa") is None


def test_multi_invalidated_when_source_set_differs(tmp_path: Path) -> None:
    n = _mk(tmp_path, "n.pdf")
    s = _mk(tmp_path, "s.pdf")
    save_waypoint_cache_multi(
        tmp_path, [("north", n), ("south", s)], _recs(), "ocr", "lsa"
    )
    # Querying with only one of the two sources must miss.
    assert load_cached_waypoints_multi(tmp_path, [("north", n)], "lsa") is None


def test_multi_missing_source_file_misses(tmp_path: Path) -> None:
    n = _mk(tmp_path, "n.pdf")
    s = _mk(tmp_path, "s.pdf")
    sources = [("north", n), ("south", s)]
    save_waypoint_cache_multi(tmp_path, sources, _recs(), "ocr", "lsa")
    s.unlink()
    assert load_cached_waypoints_multi(tmp_path, sources, "lsa") is None


def test_single_loader_does_not_read_multi_cache(tmp_path: Path) -> None:
    """A multi-source cache file must not validate as a single-source
    cache (different schema), so the two never cross-read."""
    n = _mk(tmp_path, "n.pdf")
    s = _mk(tmp_path, "s.pdf")
    save_waypoint_cache_multi(
        tmp_path, [("north", n), ("south", s)], _recs(), "ocr", "lsa"
    )
    # The legacy single-source loader keys on "back_pdf"; the multi
    # payload has "sources" instead, so it should miss.
    assert load_cached_waypoints(tmp_path, n, "lsa") is None
