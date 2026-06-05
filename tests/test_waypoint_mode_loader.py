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

"""Tests for the per-mode waypoint loader (merge + dedup + strategy)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cvfr_routemaster import waypoints as wp_mod
from cvfr_routemaster.map_modes import (
    MapMode,
    SheetDef,
    SheetRole,
    WaypointSource,
    WaypointStrategy,
)
from cvfr_routemaster.waypoint_types import WaypointRecord


def _rec(code: str, lat: float = 31.0, lon: float = 34.0) -> WaypointRecord:
    return WaypointRecord(
        index=1,
        code=code,
        name_he="שם",
        reporting_type="חובה",
        lat=lat,
        lon=lon,
        lat_dms="x",
        lon_dms="y",
    )


def _two_map_sheet_mode() -> MapMode:
    """LSA-shaped mode: two map sheets, full-OCR, page-2 sources."""
    return MapMode(
        mode_id="testlsa",
        display_name="TestLSA",
        sheets=(
            SheetDef("north", SheetRole.MAP, "N", "n.pdf", "http://n"),
            SheetDef("south", SheetRole.MAP, "S", "s.pdf", "http://s"),
        ),
        waypoint_sources=(
            WaypointSource("north", pages=(1,)),
            WaypointSource("south", pages=(1,)),
        ),
        waypoint_strategy=WaypointStrategy.FULL_OCR,
    )


def _single_source_mode() -> MapMode:
    """CVFR-shaped mode: dedicated back PDF, vector-hybrid, all pages."""
    return MapMode(
        mode_id="testcvfr",
        display_name="TestCVFR",
        sheets=(
            SheetDef("back", SheetRole.WAYPOINTS, "B", "b.pdf", "http://b"),
        ),
        waypoint_sources=(WaypointSource("back", pages=None),),
        waypoint_strategy=WaypointStrategy.VECTOR_HYBRID,
    )


def test_merge_and_dedup_by_code(tmp_path: Path, monkeypatch) -> None:
    np = tmp_path / "n.pdf"
    sp = tmp_path / "s.pdf"
    np.write_bytes(b"%PDF")
    sp.write_bytes(b"%PDF")

    calls: list[tuple[str, bool, object]] = []

    def fake_extract(path, *, full_ocr=False, pages=None, progress=None):
        calls.append((Path(path).name, full_ocr, pages))
        if Path(path).name == "n.pdf":
            return [_rec("ALFA"), _rec("BRAVO")]
        return [_rec("BRAVO"), _rec("CHARLIE")]  # BRAVO duplicates north

    monkeypatch.setattr(wp_mod, "extract_waypoints_ocr", fake_extract)

    records, tag = wp_mod.load_waypoints_for_mode(
        _two_map_sheet_mode(), {"north": np, "south": sp}
    )

    assert tag == "ocr"
    assert [r.code for r in records] == ["ALFA", "BRAVO", "CHARLIE"]
    # Re-indexed 1..N in merged order; north's BRAVO wins (first seen).
    assert [r.index for r in records] == [1, 2, 3]
    # Strategy + page selector forwarded to the extractor for each source.
    assert calls == [("n.pdf", True, (1,)), ("s.pdf", True, (1,))]


def test_records_to_sqlite_allows_duplicate_codes() -> None:
    """The waypoints table must store two points sharing a code (נבטים and
    נגב, both ``LLNV``) — a UNIQUE/PRIMARY-KEY on ``code`` would raise
    ``IntegrityError`` and abort the whole map load."""
    from dataclasses import replace

    conn = sqlite3.connect(":memory:")
    # Distinct row identity comes from the index/rowid, not the code.
    recs = [
        replace(_rec("LLNV", lat=31.2133, lon=35.0183), index=1),
        replace(_rec("LLNV", lat=31.1950, lon=35.0383), index=2),
    ]
    wp_mod.records_to_sqlite(recs, conn)
    rows = conn.execute(
        "SELECT code, lat, lon FROM waypoints ORDER BY wp_idx"
    ).fetchall()
    assert [r[0] for r in rows] == ["LLNV", "LLNV"]
    assert {(round(r[1], 4), round(r[2], 4)) for r in rows} == {
        (31.2133, 35.0183),
        (31.1950, 35.0383),
    }


def test_same_code_distinct_coords_both_kept(tmp_path: Path, monkeypatch) -> None:
    """Two genuinely different points that share a code (e.g. נבטים and נגב,
    both OCR'd as ``LLNV`` near Nevatim AFB) must both survive dedup because
    their coordinates differ — only true cross-sheet duplicates (same code
    *and* same coordinates) collapse."""
    np = tmp_path / "n.pdf"
    sp = tmp_path / "s.pdf"
    np.write_bytes(b"%PDF")
    sp.write_bytes(b"%PDF")

    def fake_extract(path, *, full_ocr=False, pages=None, progress=None):
        if Path(path).name == "n.pdf":
            # Same code, different locations — both real points.
            return [_rec("LLNV", lat=31.2133, lon=35.0183),
                    _rec("LLNV", lat=31.1950, lon=35.0383)]
        # South repeats the identical national list — pure duplicates.
        return [_rec("LLNV", lat=31.2133, lon=35.0183),
                _rec("LLNV", lat=31.1950, lon=35.0383)]

    monkeypatch.setattr(wp_mod, "extract_waypoints_ocr", fake_extract)

    records, _ = wp_mod.load_waypoints_for_mode(
        _two_map_sheet_mode(), {"north": np, "south": sp}
    )
    # Both distinct LLNV kept; the cross-sheet exact duplicates collapsed.
    assert [r.code for r in records] == ["LLNV", "LLNV"]
    assert {(round(r.lat, 4), round(r.lon, 4)) for r in records} == {
        (31.2133, 35.0183),
        (31.1950, 35.0383),
    }
    assert [r.index for r in records] == [1, 2]


def test_single_source_uses_vector_hybrid(tmp_path: Path, monkeypatch) -> None:
    bp = tmp_path / "b.pdf"
    bp.write_bytes(b"%PDF")

    seen: list[tuple[bool, object]] = []

    def fake_extract(path, *, full_ocr=False, pages=None, progress=None):
        seen.append((full_ocr, pages))
        return [_rec("ZULU")]

    monkeypatch.setattr(wp_mod, "extract_waypoints_ocr", fake_extract)

    records, tag = wp_mod.load_waypoints_for_mode(
        _single_source_mode(), {"back": bp}
    )
    assert tag == "hybrid"
    assert [r.code for r in records] == ["ZULU"]
    assert seen == [(False, None)]


def test_progress_callback_forwarded_per_source(
    tmp_path: Path, monkeypatch
) -> None:
    """The loader must forward a ``(done, total, sheet_key)`` progress
    callback to each source's extractor so a GUI can show a determinate
    bar during the slow full-OCR scans. Each source reports its own row
    counts, tagged with its sheet key."""
    np = tmp_path / "n.pdf"
    sp = tmp_path / "s.pdf"
    np.write_bytes(b"%PDF")
    sp.write_bytes(b"%PDF")

    def fake_extract(path, *, full_ocr=False, pages=None, progress=None):
        # Each source emits a tiny 0..2 progression so we can assert the
        # loader wires the per-source callback and the row counts flow.
        if progress is not None:
            progress(0, 2)
            progress(1, 2)
            progress(2, 2)
        code = "N" if Path(path).name == "n.pdf" else "S"
        return [_rec(code)]

    monkeypatch.setattr(wp_mod, "extract_waypoints_ocr", fake_extract)

    seen: list[tuple[int, int, str]] = []
    wp_mod.load_waypoints_for_mode(
        _two_map_sheet_mode(),
        {"north": np, "south": sp},
        progress=lambda done, total, key: seen.append((done, total, key)),
    )

    assert seen == [
        (0, 2, "north"),
        (1, 2, "north"),
        (2, 2, "north"),
        (0, 2, "south"),
        (1, 2, "south"),
        (2, 2, "south"),
    ]


def test_missing_sources_returns_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        wp_mod, "extract_waypoints_ocr", lambda *a, **k: [_rec("X")]
    )
    records, tag = wp_mod.load_waypoints_for_mode(
        _two_map_sheet_mode(),
        {"north": tmp_path / "nope_n.pdf", "south": tmp_path / "nope_s.pdf"},
    )
    assert records == []
    assert tag == "missing"


def test_partial_sources_still_extracts_available(tmp_path: Path, monkeypatch) -> None:
    np = tmp_path / "n.pdf"
    np.write_bytes(b"%PDF")
    monkeypatch.setattr(
        wp_mod,
        "extract_waypoints_ocr",
        lambda path, *, full_ocr=False, pages=None, progress=None: [_rec("ONLY")],
    )
    records, tag = wp_mod.load_waypoints_for_mode(
        _two_map_sheet_mode(),
        {"north": np, "south": tmp_path / "missing_s.pdf"},
    )
    assert [r.code for r in records] == ["ONLY"]
    assert tag == "ocr"
