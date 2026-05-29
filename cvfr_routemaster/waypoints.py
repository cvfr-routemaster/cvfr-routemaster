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
from pathlib import Path
from typing import Iterable

from cvfr_routemaster.back_page_ocr import extract_waypoints_ocr
from cvfr_routemaster.waypoint_types import WaypointRecord


def load_waypoints_from_back_pdf(path: Path | str) -> tuple[list[WaypointRecord], str]:
    path = Path(path)
    if not path.is_file():
        return [], "missing"
    records = extract_waypoints_ocr(path)
    return records, "hybrid"


def records_to_sqlite(records: Iterable[WaypointRecord], conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS waypoints")
    conn.execute(
        """
        CREATE TABLE waypoints (
            wp_idx INTEGER NOT NULL,
            code TEXT PRIMARY KEY,
            name_he TEXT,
            reporting_type TEXT,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            lat_dms TEXT NOT NULL,
            lon_dms TEXT NOT NULL
        )
        """
    )
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
