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

import re


_DEG = "\u00b0"  # degree sign, ASCII-safe source file

_DMS_E = re.compile(
    rf"(?P<deg>\d{{1,3}})\s*{_DEG}\s*(?P<min>\d{{1,2}})\s*'\s*(?P<sec>\d{{1,2}}(?:\.\d+)?)\s*\"\s*E",
    re.UNICODE,
)
_DMS_N = re.compile(
    rf"(?P<deg>\d{{1,2}})\s*{_DEG}\s*(?P<min>\d{{1,2}})\s*'\s*(?P<sec>\d{{1,2}}(?:\.\d+)?)\s*\"\s*N",
    re.UNICODE,
)


def dms_to_decimal(deg: str, minute: str, sec: str, positive: bool = True) -> float:
    v = float(deg) + float(minute) / 60.0 + float(sec) / 3600.0
    return v if positive else -v


def parse_lon_dms(text: str) -> float | None:
    m = _DMS_E.search(text)
    if not m:
        return None
    return dms_to_decimal(m.group("deg"), m.group("min"), m.group("sec"), positive=True)


def parse_lat_dms(text: str) -> float | None:
    m = _DMS_N.search(text)
    if not m:
        return None
    return dms_to_decimal(m.group("deg"), m.group("min"), m.group("sec"), positive=True)


def first_lat_dms_match(text: str) -> str | None:
    """Literal substring of the first north latitude DMS in *text* (for stripping from OCR lines)."""
    m = _DMS_N.search(text)
    return m.group(0) if m else None


def first_lon_dms_match(text: str) -> str | None:
    """Literal substring of the first east longitude DMS in *text*."""
    m = _DMS_E.search(text)
    return m.group(0) if m else None
