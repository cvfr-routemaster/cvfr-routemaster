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
