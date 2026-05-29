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

"""Hand-fed Pilot fixtures for visual testing of the traffic
overlay before the live VATSIM poller lands.

Goal: let the user toggle "Show VATSIM traffic" on a freshly
loaded chart and immediately see all five wake categories drawn
in their chosen colour, scaled correctly, rotated to their
heading, and labelled with a callsign — *without* needing
anyone to actually be flying on VATSIM at that moment.

Once the live poller in
:mod:`cvfr_routemaster.vatsim_worker` lands, this fixture stops
being on the hot path; it stays around as a deterministic visual
regression target (and a fallback when VATSIM is offline,
upstream is changed, or the user is on a flight without
internet).

Coverage targets one pilot per wake category plus a sixth so the
"L" colour is exercised twice with different headings — that's
the trickiest visual to validate (light singles often share
colour with VFR trainers in the same airspace, so testing two
helps confirm the per-callsign rotation works right).
"""

from __future__ import annotations

from cvfr_routemaster.vatsim_feed import WAKE_UNKNOWN, Pilot


def demo_pilots() -> list[Pilot]:
    """Return six hand-crafted pilots covering all five wake
    categories, scattered across Israeli airspace at sensible
    positions and headings.

    Positions sit inside the LLLL chart's calibrated coverage so
    they all project successfully. Headings, altitudes and
    groundspeeds are realistic-but-arbitrary — picked to look
    plausible on the chart, not to model any specific real-world
    flight.

    Layout (north → south):

    * ``CLX5N`` (Heavy, B748) — northern Israel near the Lebanon
      border, westbound at FL380.
    * ``4XBEN`` (Light, C172) — Galilee, southbound VFR.
    * ``4XGGG`` (Light, DA40) — central Israel, NE-bound VFR.
    * ``ELY323`` (Medium, B738) — over Tel Aviv area, eastbound
      climbing out toward Cyprus.
    * ``4XCAL`` (Unknown — no flight plan) — south of Tel Aviv
      coast, westbound VFR squawking 7000.
    * ``UAE204`` (Super, A380) — south Israel near Eilat,
      NW-bound at FL400.
    """
    return [
        Pilot(
            cid=10001,
            callsign="ELY323",
            name="ELY 323",
            lat=32.00,
            lon=34.90,
            altitude_ft=28000,
            groundspeed_kts=420,
            heading_deg=87,
            transponder="2435",
            aircraft_type="B738",
            wake="M",
            flight_rules="I",
            departure="LLBG",
            arrival="LCLK",
        ),
        Pilot(
            cid=10002,
            callsign="4XCAL",
            name="GA Cessna",
            lat=31.45,
            lon=34.85,
            altitude_ft=2500,
            groundspeed_kts=95,
            heading_deg=270,
            transponder="7000",
            aircraft_type=None,
            wake=WAKE_UNKNOWN,
            flight_rules="V",
            departure="",
            arrival="",
        ),
        Pilot(
            cid=10003,
            callsign="4XBEN",
            name="VFR Trainer",
            lat=32.55,
            lon=35.10,
            altitude_ft=4500,
            groundspeed_kts=110,
            heading_deg=180,
            transponder="1024",
            aircraft_type="C172",
            wake="L",
            flight_rules="V",
            departure="LLHZ",
            arrival="LLHA",
        ),
        Pilot(
            cid=10004,
            callsign="CLX5N",
            name="CARGOLUX HEAVY",
            lat=33.10,
            lon=35.20,
            altitude_ft=38000,
            groundspeed_kts=481,
            heading_deg=270,
            transponder="1745",
            aircraft_type="B748",
            wake="H",
            flight_rules="I",
            departure="OJAI",
            arrival="ELLX",
        ),
        Pilot(
            cid=10005,
            callsign="UAE204",
            name="EMIRATES SUPER",
            lat=30.85,
            lon=35.50,
            altitude_ft=40000,
            groundspeed_kts=515,
            heading_deg=315,
            transponder="2156",
            aircraft_type="A388",
            wake="J",
            flight_rules="I",
            departure="OMDB",
            arrival="EGLL",
        ),
        Pilot(
            cid=10006,
            callsign="4XGGG",
            name="Local light",
            lat=32.30,
            lon=34.95,
            altitude_ft=3000,
            groundspeed_kts=120,
            heading_deg=45,
            transponder="1200",
            aircraft_type="DA40",
            wake="L",
            flight_rules="V",
            departure="LLBS",
            arrival="LLBG",
        ),
    ]
