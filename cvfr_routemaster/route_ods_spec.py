"""
CVFR plotted-route spreadsheet layout (P0 — column contract).

Derived from project examples:

- ``LLHA-LLIB.ods`` — Haifa → Rosh Pina direction
- ``LLIB-LLHA.ods`` — Rosh Pina → Haifa direction

Sheet name, Hebrew headers, row roles, and footer labels must stay aligned with these
files so future export (ODS/CSV) matches pilot workflow.

All user-visible Hebrew strings below are copied verbatim from the examples.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# ---------------------------------------------------------------------------
# Sheet
# ---------------------------------------------------------------------------

ROUTE_SHEET_NAME: Final[str] = "Sheet1"

# ---------------------------------------------------------------------------
# Row layout (0-based indices for the example files)
# ---------------------------------------------------------------------------

# Row 0: planned cruise speed (knots) — label col A, value col B.
CRUISE_SPEED_ROW: Final[int] = 0
# Row 1: blank spacer in examples.
# Row 2: column headers for the leg table.
LEG_HEADER_ROW: Final[int] = 2
# Row 3+: leg rows until a blank “from” cell ends the leg block (in examples).
FIRST_LEG_DATA_ROW: Final[int] = 3

# Footer labels appear on fixed rows in the examples (after placeholder leg rows).
# LLHA-LLIB uses max altitude 2500; LLIB-LLHA uses 3000 — values are route-specific.
SUMMARY_MAX_ALT_ROW: Final[int] = 23
SUMMARY_TOTAL_DISTANCE_ROW: Final[int] = 24
SUMMARY_TOTAL_TIME_ROW: Final[int] = 25
SUMMARY_ROUTE_CODES_ROW: Final[int] = 26

# ---------------------------------------------------------------------------
# Row 0 — cruise speed
# ---------------------------------------------------------------------------

CRUISE_SPEED_LABEL_HE: Final[str] = "מהירות שיוט מתוכננת (קשר)"

# ---------------------------------------------------------------------------
# Leg table — 13 columns (A–M), Hebrew headers on row LEG_HEADER_ROW
# ---------------------------------------------------------------------------

# Machine keys (stable English ids for code); order matches column index 0..12.
ROUTE_COLUMN_KEYS: Final[tuple[str, ...]] = (
    "from_place_he",  # ממקום
    "to_place_he",  # למקום
    "reporting_type_he",  # סוג דיווח
    "controller_he",  # בקר
    "report_freq_he",  # תדר דיווח
    "handoff_controller_he",  # מעבר לבקר
    "handoff_freq_he",  # מעבר לתדר
    "magnetic_track",  # כיוון מגנטי (e.g. 074)
    "altitude_ft",  # גובה
    "distance_nm",  # מרחק (NM)
    "planned_time_hhmm",  # זמן מתוכנן (H:MM)
    "five_letter_code",  # 5 LETTER CODE (empty in examples)
    "bing_note_en",  # English note column (header only in examples)
)

ROUTE_HEADER_ROW_HE: Final[tuple[str, ...]] = (
    "ממקום",
    "למקום",
    "סוג דיווח",
    "בקר",
    "תדר דיווח",
    "מעבר לבקר",
    "מעבר לתדר",
    "כיוון מגנטי",
    "גובה",
    "מרחק (NM)",
    "זמן מתוכנן",
    "5 LETTER CODE",
    "The link leads to Bing Maps at the specified coordinate -->",
)

BING_NOTE_HEADER_EN: Final[str] = ROUTE_HEADER_ROW_HE[-1]

assert len(ROUTE_COLUMN_KEYS) == len(ROUTE_HEADER_ROW_HE) == 13

# ---------------------------------------------------------------------------
# Footer labels (Hebrew) — column positions match examples
# ---------------------------------------------------------------------------

SUMMARY_MAX_ALT_LABEL_HE: Final[str] = "גובה מירבי"
SUMMARY_TOTAL_DISTANCE_LABEL_HE: Final[str] = "מרחק כולל"
SUMMARY_TOTAL_TIME_LABEL_HE: Final[str] = "זמן כולל"
SUMMARY_ROUTE_CODES_LABEL_HE: Final[str] = "מסלול לתכנית"

# In examples, "מרחק כולל" sits in the same column as מרחק (NM) (leg distance column).
# "זמן כולל" aligns under "זמן מתוכנן".
# "מסלול לתכנית" label is left of the space-separated ICAO tokens.

# ---------------------------------------------------------------------------
# Example route token strings (space-separated waypoint codes, for regression checks)
# ---------------------------------------------------------------------------

# From row "מסלול לתכנית" in each example file (space-separated ICAO-like codes).
EXAMPLE_ROUTE_CODES_LLHA_LLIB: Final[str] = "GILAM EVLYM SEGEV ZALMN DESHE AMNON"
EXAMPLE_ROUTE_CODES_LLIB_LLHA: Final[str] = "AMNON DESHE ZALMN SEGEV EVLYM GILAM"


@dataclass(frozen=True)
class RouteOdsColumn:
    """One logical column in the leg grid."""

    index: int
    key: str
    header_he: str


def route_columns() -> tuple[RouteOdsColumn, ...]:
    return tuple(
        RouteOdsColumn(i, ROUTE_COLUMN_KEYS[i], ROUTE_HEADER_ROW_HE[i])
        for i in range(len(ROUTE_COLUMN_KEYS))
    )
