"""Shared waypoint styling constants.

These live separately from the table widgets so the master waypoint table on
the right pane and the planned-route table on the left pane stay visually
identical without coupling either of them to the other (and without setting
up a circular import between ``main_window`` and ``route_panel``).

If/when more shared waypoint styling needs a home (link colours, fonts, …),
add it here rather than re-duplicating it inline.
"""

from __future__ import annotations


#: Hebrew literal for "mandatory" (חובה) reporting type, as it appears in the
#: back-page OCR output. Defined as a Unicode escape so this source file stays
#: ASCII-clean across editor/encoding setups.
HE_MANDATORY: str = "\u05d7\u05d5\u05d1\u05d4"  # חובה

#: Hebrew literal for "on demand" (דרישה) reporting type — same OCR-source
#: convention as :data:`HE_MANDATORY`.
HE_ON_DEMAND: str = "\u05d3\u05e8\u05d9\u05e9\u05d4"  # דרישה

#: Foreground colours for the reporting-type column. Picked for legibility on
#: the dark table background (``#1e1e1e`` per ``ui_theme``) and to keep each
#: type semantically distinct at a glance:
#:
#: - **חובה (mandatory)** → red — the strongest "must do" signal
#: - **דרישה (on demand)** → saturated yellow — secondary attention
#: - **ARP (aerodrome reference point)** → pink so it doesn't blur together
#:   with the much more numerous reporting points
#:
#: Keys are the exact strings produced by ``back_page_ocr``'s parser
#: (``_DRIV`` / ``_CHOVA`` there) so any drift in OCR output stays correctly
#: coloured.
REPORTING_TYPE_COLORS: dict[str, str] = {
    HE_MANDATORY: "#ef4444",
    HE_ON_DEMAND: "#facc15",
    "ARP": "#ec4899",
}

#: Blue link colour for the Hebrew-name (reporting-name) cells. Underlined cells
#: in this colour are the "click to centre the map on this waypoint" link in
#: both the master waypoint table and the route panel — keeping the colour in
#: one place ensures the two tables stay visually consistent.
WAYPOINT_NAME_LINK_BLUE: str = "#60a5fa"

#: Green link colour for the ICAO-code cells. Underlined cells in this colour
#: are the "click to open in external map provider" link in both tables.
WAYPOINT_CODE_LINK_GREEN: str = "#4ade80"
