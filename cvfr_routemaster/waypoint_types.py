"""Waypoint row from the CVFR back-pages listing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WaypointRecord:
    index: int
    code: str
    name_he: str
    reporting_type: str
    lat: float
    lon: float
    lat_dms: str
    lon_dms: str
