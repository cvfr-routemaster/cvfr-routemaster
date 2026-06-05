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

"""Regression tests for the tolerant FULL_OCR DMS recovery (LSA path).

The strict :mod:`cvfr_routemaster.coords` parser is correct for CVFR's
clean vector text but rejected ~35 real LSA reporting points per sheet
whose OCR'd coordinate cells had mangled *separators* (the digits were
fine). :func:`_ocr_dms_recover` letter-corrects the obvious digit
confusions, takes the first six digits as ``DD MM SS``, and gates the
result against the Israel envelope.

These cases pin the exact mangled strings observed on the live LSA chart
(captured by a full per-row audit) so a future change to the recovery —
or an accidental tightening back to the strict parser on this path —
fails loudly. They are pure-Python string transforms (no Tesseract / no
PDF), so they run anywhere.
"""

from __future__ import annotations

import pytest

from cvfr_routemaster.back_page_ocr import (
    _IL_LAT_HI,
    _IL_LAT_LO,
    _IL_LON_HI,
    _IL_LON_LO,
    _ocr_dms_recover,
)


def _lat(s: str):
    return _ocr_dms_recover(s, hemi="N", lo=_IL_LAT_LO, hi=_IL_LAT_HI)


def _lon(s: str):
    return _ocr_dms_recover(s, hemi="E", lo=_IL_LON_LO, hi=_IL_LON_HI)


def _close(a: float, b: float) -> bool:
    return abs(a - b) < 1e-9


# (raw OCR cell, expected deg, min, sec) — mangled strings straight from
# the live-chart audit. The expected value is the correct chart reading.
LAT_CASES = [
    ("ZOFAR-lat", "30\u00b0 33'35\" N", 30, 33, 35),  # clean control
    ("ALLON", "32\u00b0 2'29\" N", 32, 2, 29),  # single-digit minute
    ("deg3-glue", "317 20' 14\" N", 31, 20, 14),  # ° read as a digit on deg
    ("LLKZ", "30\u00b0 51\u00b0 32\" N", 30, 51, 32),  # ' -> degree sign
    ("YOTVT", "29\u00b0 54 03\" N", 29, 54, 3),  # dropped separator
    ("NAHEM", "31\u00b0 44'43 N", 31, 44, 43),  # dropped seconds quote
    ("MYTAR", "31\u00b0 18\u00b0 53\" N", 31, 18, 53),
    ("DIMON", "31\u00b0 04\u00b0 04\" N", 31, 4, 4),
    ("KKANA", "32\u00b0 45 33\" N", 32, 45, 33),
    ("IZHRW", "32\u00b0 11 39\" N", 32, 11, 39),
]

LON_CASES = [
    ("ZOFAR", "35\u00b0 11\u00b0 12\" E", 35, 11, 12),  # the reported point
    ("FAZEL", "35\u00b0 27 47\" E", 35, 27, 47),
    ("LLMG", "35\u00b0 14\" 05\" E", 35, 14, 5),  # '/" swap
    ("TZUBA", "35\u00b0 O7'14\" E", 35, 7, 14),  # O -> 0
    ("OLGAH", "34\u00b0 S157\" E", 34, 51, 57),  # S -> 5, merged digits
    ("LLGV", "34\u00b0 27'37\u00b0 E", 34, 27, 37),
    ("ZGOAL", "34\u00b0 47\u00b0 16\" E", 34, 47, 16),
    ("MOVIL", "35\u00b0 14\u00b0 00\" E", 35, 14, 0),
    ("LLIB", "35\u00b0 34\u00b0 15\" E", 35, 34, 15),
]


@pytest.mark.parametrize("tag, raw, deg, mn, sec", LAT_CASES)
def test_lat_recovery(tag: str, raw: str, deg: int, mn: int, sec: int) -> None:
    val, disp = _lat(raw)
    assert val is not None, tag
    assert _close(val, deg + mn / 60.0 + sec / 3600.0), tag
    assert disp == f"{deg}\u00b0 {mn:02d}' {sec:02d}\" N", tag


@pytest.mark.parametrize("tag, raw, deg, mn, sec", LON_CASES)
def test_lon_recovery(tag: str, raw: str, deg: int, mn: int, sec: int) -> None:
    val, disp = _lon(raw)
    assert val is not None, tag
    assert _close(val, deg + mn / 60.0 + sec / 3600.0), tag
    assert disp == f"{deg}\u00b0 {mn:02d}' {sec:02d}\" E", tag


def test_clean_row_matches_strict_value() -> None:
    """A clean cell yields the same decimal the strict parser would."""
    from cvfr_routemaster.coords import parse_lat_dms

    raw = "32\u00b0 38'26\" N"
    val, _ = _lat(raw)
    assert val is not None
    assert _close(val, parse_lat_dms(raw))


def test_out_of_envelope_is_dropped() -> None:
    """A mis-segmented stream that lands outside Israel returns None so the
    row is dropped rather than placed at a wrong location."""
    val, _ = _lat("99\u00b0 99 99\" N")
    assert val is None
    # Plausible-digits but far north/east of Israel.
    val, _ = _lon("88\u00b0 11 12\" E")
    assert val is None


def test_too_few_digits_is_dropped() -> None:
    val, _ = _lat("3 N")
    assert val is None
