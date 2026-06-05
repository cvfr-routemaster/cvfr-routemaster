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

"""Unit tests for the light FULL_OCR name cleaner used by the LSA path.

The LSA reporting-point names are recovered by OCR'ing each meta cell
on its own (:func:`_name_type_per_cell`), which returns the Hebrew name
verbatim. :func:`_clean_ocr_name_light` then does *only* safe
normalization. These tests pin the exact rows the user reported as
mangled/empty on the live LSA chart so a future drive-by change to the
cleaner — or an accidental reuse of the heavy, recovery-oriented
:func:`_polish_waypoint_name_he` on this clean text — fails loudly.

The cases are pure-Python string transforms (no Tesseract / no PDF), so
they run anywhere.
"""

from __future__ import annotations

import pytest

from cvfr_routemaster.back_page_ocr import (
    _clean_ocr_name_light,
    _polish_waypoint_name_he,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Single-word names — must pass through untouched.
        ("שפך", "שפך"),  # SHEFC (was erased by the union-strip OCR)
        ("גילת", "גילת"),  # GILAT (was "גילת ורה רעש")
        # Two-word names — must keep both words, no trailing junk.
        ("בת שלמה", "בת שלמה"),  # BTSLM (was "בת שלמה רז רז")
        ("בית שמש", "בית שמש"),  # BSEMS (was "בית שמש רז רדז")
        ("גלילות מזרח", "גלילות מזרח"),  # GLILE (was "גלילות מזרח רו")
        ("עין גדי", "עין גדי"),  # ENGDI (was erased)
        ("עין יהב", "עין יהב"),  # LLEY (was erased)
        # Gershayim place name — the heavy polish truncates this to
        # "הר ל" (its stem matcher finds הר inside מהר); the light
        # cleaner keeps it whole.
        ('כרם מהר"ל', 'כרם מהר"ל'),  # CNHRL
        # Trailing footnote asterisk gets trimmed.
        ("דור*", "דור"),  # DOORS
    ],
)
def test_light_cleaner_preserves_real_names(raw: str, expected: str) -> None:
    assert _clean_ocr_name_light(raw) == expected


def test_light_cleaner_reattaches_detached_final_letter() -> None:
    """ORAKV regression: the LSA per-cell OCR split the trailing ``א`` off
    ``עקיבא`` and re-ordered it to the front (``"אור עקיבא"`` →
    ``"א אור עקיב"``). A lone leading Hebrew letter followed only by Hebrew
    words is reattached to the final token, restoring the placename."""
    assert _clean_ocr_name_light("א אור עקיב") == "אור עקיבא"


def test_light_cleaner_leaves_multi_letter_first_word_untouched() -> None:
    """The reattach repair triggers *only* on a lone single-letter lead, so
    genuine multi-word names (a real ≥2-letter first word) pass through."""
    assert _clean_ocr_name_light("בת שלמה") == "בת שלמה"
    assert _clean_ocr_name_light("עין גדי") == "עין גדי"


def test_light_cleaner_strips_leading_border_and_lead_in() -> None:
    """A stray cell-border rule or a non-Hebrew lead-in is dropped, and
    the cleaner keeps everything from the first Hebrew letter on."""
    assert _clean_ocr_name_light("| גילת") == "גילת"
    assert _clean_ocr_name_light("7 עין גדי") == "עין גדי"


def test_heavy_polish_would_mangle_kerem_maharal() -> None:
    """Documents *why* the light cleaner exists: the CVFR-tuned heavy
    polish destroys this clean LSA name (its place-name stem matcher
    catches הר inside מהר). If a future polish fix makes this pass
    through intact, this test will flag that the divergence is gone and
    the two paths could potentially be reconciled."""
    assert _clean_ocr_name_light('כרם מהר"ל') == 'כרם מהר"ל'
    assert _polish_waypoint_name_he('כרם מהר"ל') != 'כרם מהר"ל'
