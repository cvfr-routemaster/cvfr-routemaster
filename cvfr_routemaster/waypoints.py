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

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Mapping

from cvfr_routemaster.back_page_ocr import extract_waypoints_ocr
from cvfr_routemaster.map_modes import MapMode, WaypointStrategy
from cvfr_routemaster.waypoint_types import WaypointRecord


def load_waypoints_from_back_pdf(path: Path | str) -> tuple[list[WaypointRecord], str]:
    path = Path(path)
    if not path.is_file():
        return [], "missing"
    records = extract_waypoints_ocr(path)
    return records, "hybrid"


def _dedup_by_code(records: Iterable[WaypointRecord]) -> list[WaypointRecord]:
    """Collapse cross-sheet duplicates, re-indexing 1..N in order.

    LSA ships the same national reporting-point list on both the north
    and south sheets; merging the two extractions would double every
    point. We dedup on ``(code, lat, lon)`` rather than ``code`` alone:
    the same point on both sheets has identical coordinates and collapses
    to one, but two genuinely *different* points that happen to share a
    code — e.g. נבטים and נגב, both stamped ``LLNV`` near Nevatim AFB —
    have different coordinates and are both kept. First-seen wins (north
    is extracted first), so the result is deterministic. Coordinates are
    rounded to ~1 m so float noise can't manufacture a false distinct.
    """
    seen: set[tuple[str, int, int]] = set()
    out: list[WaypointRecord] = []
    for rec in records:
        code = rec.code.strip().upper()
        if not code:
            continue
        key = (code, round(rec.lat * 1e5), round(rec.lon * 1e5))
        if key in seen:
            continue
        seen.add(key)
        out.append(replace(rec, index=len(out) + 1, code=code))
    return out


def load_waypoints_for_mode(
    mode: MapMode,
    paths_by_sheet: Mapping[str, Path | str],
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[WaypointRecord], str]:
    """Extract, merge, and dedup reporting points for ``mode``.

    ``paths_by_sheet`` maps a mode sheet key (``"back"`` for CVFR;
    ``"north"`` / ``"south"`` for LSA) to the resolved local PDF path.
    Each of the mode's :class:`WaypointSource` entries is mined with the
    mode's strategy (vector-hybrid vs full-OCR) and page selector; the
    results are concatenated in source order and deduped by code.

    ``progress`` is an optional ``(done, total, sheet_key)`` callback
    forwarded per source so a GUI can show a determinate "OCR row X of
    N" bar during the (potentially multi-minute) full-OCR scans. The
    counts reset per source — each source reports its own row total —
    and ``sheet_key`` names the source so the label can say which sheet
    is being scanned.

    Returns ``(records, tag)``. ``tag`` records the strategy for the
    waypoint cache's diagnostics. A source whose PDF is missing is
    skipped (so a partially-downloaded mode still yields whatever it
    can rather than raising).
    """
    full_ocr = mode.waypoint_strategy is WaypointStrategy.FULL_OCR
    merged: list[WaypointRecord] = []
    used_any = False
    for source in mode.waypoint_sources:
        raw = paths_by_sheet.get(source.sheet_key)
        if not raw:
            continue
        path = Path(raw)
        if not path.is_file():
            continue
        used_any = True
        source_progress: Callable[[int, int], None] | None = None
        if progress is not None:
            key = source.sheet_key
            source_progress = lambda done, total, _k=key: progress(
                done, total, _k
            )
        merged.extend(
            extract_waypoints_ocr(
                path,
                full_ocr=full_ocr,
                pages=source.pages,
                progress=source_progress,
            )
        )

    if not used_any:
        return [], "missing"
    tag = "ocr" if full_ocr else "hybrid"
    return _dedup_by_code(merged), tag


def records_to_sqlite(records: Iterable[WaypointRecord], conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS waypoints")
    # No UNIQUE/PRIMARY KEY on a data column: a code is *not* unique — LSA
    # stamps two distinct points (נבטים and נגב) with ``LLNV`` near Nevatim
    # AFB — and we don't want to depend on ``wp_idx`` uniqueness from
    # externally-written cache data either. SQLite's implicit rowid is the
    # row identity; ``code`` keeps a non-unique index for lookups.
    conn.execute(
        """
        CREATE TABLE waypoints (
            wp_idx INTEGER NOT NULL,
            code TEXT NOT NULL,
            name_he TEXT,
            reporting_type TEXT,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            lat_dms TEXT NOT NULL,
            lon_dms TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX idx_waypoints_code ON waypoints (code)")
    conn.executemany(
        """
        INSERT INTO waypoints (wp_idx, code, name_he, reporting_type, lat, lon, lat_dms, lon_dms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r.index,
                r.code,
                r.name_he,
                r.reporting_type,
                r.lat,
                r.lon,
                r.lat_dms,
                r.lon_dms,
            )
            for r in records
        ],
    )
    conn.commit()
