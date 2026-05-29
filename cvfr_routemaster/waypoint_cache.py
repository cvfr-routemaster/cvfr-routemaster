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
Disk cache for waypoints extracted from the back-pages PDF.

Invalidates when the back PDF path, size, or mtime changes, or when EXTRACTOR_LOGIC_VERSION bumps.
(Map PNG cache lives in :mod:`cvfr_routemaster.map_image_cache`.)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cvfr_routemaster.waypoint_types import WaypointRecord

CACHE_FORMAT_VERSION = 1
# Increment when OCR/heuristics change so users pick up new extraction without relying on PDF edits.
EXTRACTOR_LOGIC_VERSION = 1


def cache_file_path(project_root: Path) -> Path:
    d = project_root / ".cvfr_routemaster"
    d.mkdir(exist_ok=True)
    return d / "waypoints_cache.json"


def _back_fingerprint(path: Path) -> dict[str, Any]:
    p = path.resolve()
    st = p.stat()
    return {"path": str(p), "mtime_ns": st.st_mtime_ns, "size": st.st_size}


def load_cached_waypoints(project_root: Path, back_path: str | Path) -> list[WaypointRecord] | None:
    """Return records if cache exists and matches back PDF + extractor version; else None."""
    bp = Path(back_path)
    if not bp.is_file():
        return None
    cf = cache_file_path(project_root)
    if not cf.is_file():
        return None
    try:
        raw = json.loads(cf.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if raw.get("cache_format_version") != CACHE_FORMAT_VERSION:
        return None
    if int(raw.get("extractor_logic_version", 0)) != EXTRACTOR_LOGIC_VERSION:
        return None
    fb = raw.get("back_pdf")
    if not isinstance(fb, dict) or not _fingerprints_match(fb, bp):
        return None
    rows = raw.get("records")
    if not isinstance(rows, list) or not rows:
        return None
    out: list[WaypointRecord] = []
    try:
        for item in rows:
            if not isinstance(item, dict):
                return None
            out.append(_record_from_dict(item))
    except (KeyError, TypeError, ValueError):
        return None
    return out


def _fingerprints_match(cached_back: dict[str, Any], path: Path) -> bool:
    """Path-independent fingerprint check — see the equivalent helper in
    :mod:`cvfr_routemaster.altitude_cache` for the full rationale.
    Briefly: the cached ``path`` field is written for diagnostics but
    not compared, so a release zip that lands in a different absolute
    directory on the friend's machine still hits the bundled cache as
    long as the back-pages PDF's bytes (and therefore its size + the
    mtime preserved by ``shutil.copy2`` / zip-extract on Windows)
    survived intact.
    """
    if not path.is_file():
        return False
    cur = _back_fingerprint(path)
    return (
        cached_back.get("mtime_ns") == cur["mtime_ns"]
        and cached_back.get("size") == cur["size"]
    )


def _record_from_dict(d: dict[str, Any]) -> WaypointRecord:
    return WaypointRecord(
        index=int(d["index"]),
        code=str(d["code"]),
        name_he=str(d.get("name_he", "")),
        reporting_type=str(d.get("reporting_type", "")),
        lat=float(d["lat"]),
        lon=float(d["lon"]),
        lat_dms=str(d["lat_dms"]),
        lon_dms=str(d["lon_dms"]),
    )


def _record_to_dict(r: WaypointRecord) -> dict[str, Any]:
    return {
        "index": r.index,
        "code": r.code,
        "name_he": r.name_he,
        "reporting_type": r.reporting_type,
        "lat": r.lat,
        "lon": r.lon,
        "lat_dms": r.lat_dms,
        "lon_dms": r.lon_dms,
    }


def save_waypoint_cache(
    project_root: Path,
    back_path: str | Path,
    records: list[WaypointRecord],
    source: str,
) -> None:
    bp = Path(back_path)
    if not bp.is_file():
        return
    payload = {
        "cache_format_version": CACHE_FORMAT_VERSION,
        "extractor_logic_version": EXTRACTOR_LOGIC_VERSION,
        "back_pdf": _back_fingerprint(bp),
        "source": source,
        "records": [_record_to_dict(r) for r in records],
    }
    cf = cache_file_path(project_root)
    cf.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
