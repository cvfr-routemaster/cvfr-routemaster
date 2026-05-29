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
