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

"""Map-mode registry: the data-driven description of each chart product.

Why this module exists
----------------------

Until v4 the app worked with exactly one Israeli chart product —
CVFR — and hard-coded the assumption everywhere as the sheet triple
``("north", "south", "back")``. v4 adds the ability to switch
between chart products (CVFR now, LSA next, Helicopter/IFR later).

Rather than scatter ``if mode == "lsa"`` branches across the
codebase, every mode-specific fact lives here in a :class:`MapMode`
value, and the rest of the app asks the registry. Adding a new
chart product becomes "register one :class:`MapMode` and ship its
seed data", not "edit twenty files".

What a mode describes
---------------------

* **sheets** — the configured PDFs the user supplies a URL/path for.
  Each :class:`SheetDef` has a :class:`SheetRole`:
    - ``MAP`` sheets are rendered to a pixmap, geo-calibrated, and
      mined for yellow altitude arrows.
    - ``WAYPOINTS`` sheets are OCR'd for the reporting-point table
      but never rendered (CVFR's back-pages PDF).
* **waypoint_sources** — where the reporting-point list comes from.
  Each :class:`WaypointSource` names a configured sheet's PDF plus an
  optional page selector. CVFR reads its dedicated back-pages PDF
  (all pages); LSA reads page 2 (index 1) of *both* its map PDFs and
  merges the two lists, deduping by code.
* **waypoint_strategy** — how to read the table:
    - ``VECTOR_HYBRID`` — PDF vector text for code/lat/lon plus OCR
      for the Hebrew name/type columns (CVFR; its PDF has a usable
      Unicode text layer).
    - ``FULL_OCR`` — OCR every column (LSA; its embedded fonts have
      no Unicode mapping, so ``page.get_text`` returns mojibake and
      only rasterize-then-OCR recovers the values).
* **overlap_codes** — preferred shared reporting points that fall in
  the north/south seam band, used to pin the joint two-sheet
  calibration. Empty means "let the geometry-based anchor selector
  pick"; a non-empty hint biases selection toward known seam VRPs.

Cache + settings namespacing
----------------------------

Every mode owns a private namespace under
``<project_root>/.cvfr_routemaster/<mode_id>/`` for its rendered
PNGs, calibration, waypoint cache, altitude caches, and downloaded
PDFs, and a matching QSettings group. Satellite tiles and global
display preferences are deliberately NOT namespaced — they are
shared across modes (the satellite imagery covers all of Israel; a
user's font sizing should not reset when they switch chart product).

Projection
----------

All currently-modeled Israeli products use the same Lambert
Conformal Conic parameters baked into :mod:`geo_calibration`, so
this module does not yet carry per-mode projection constants. The
field is intentionally omitted rather than duplicated; if a future
product (e.g. IFR at a different scale/datum) needs different
constants, add a ``projection`` field here and thread it into
``geo_calibration`` at that time.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final

from .chart_source import (
    CAAI_CHART_URLS,
    CACHE_FILENAMES,
    SHEET_DISPLAY_NAMES,
)


class SheetRole(enum.Enum):
    """What a configured PDF is used for within a mode."""

    #: Rendered to a pixmap, geo-calibrated, mined for altitude arrows.
    MAP = "map"
    #: OCR'd for the reporting-point table; never rendered.
    WAYPOINTS = "waypoints"


class WaypointStrategy(enum.Enum):
    """How to read a mode's reporting-point table from its PDF."""

    #: PDF vector text for code/lat/lon + OCR for Hebrew name/type.
    #: Requires the PDF to carry a usable Unicode text layer (CVFR).
    VECTOR_HYBRID = "vector_hybrid"
    #: OCR every column. Required when the PDF's embedded fonts lack a
    #: Unicode mapping so ``page.get_text`` returns mojibake (LSA).
    FULL_OCR = "full_ocr"


@dataclass(frozen=True)
class SheetDef:
    """One configured PDF within a mode.

    ``key`` is the stable symbolic name (``"north"`` / ``"south"`` /
    ``"back"``) used as the cache/calibration/settings sub-key.
    ``cache_pdf_filename`` is the stable filename the URL-downloaded
    copy is stored under inside the mode's ``charts/`` cache dir.
    ``render_page`` is the 0-based page index rendered as the map (only
    meaningful for ``role == MAP``).
    """

    key: str
    role: SheetRole
    display_name: str
    cache_pdf_filename: str
    default_url: str
    render_page: int = 0


@dataclass(frozen=True)
class WaypointSource:
    """Where a slice of the reporting-point list comes from.

    ``sheet_key`` references a :class:`SheetDef` in the same mode.
    ``pages`` is a tuple of 0-based page indices to mine, or ``None``
    to mine every page (CVFR back-pages). LSA uses ``pages=(1,)`` on
    each of its two map sheets.
    """

    sheet_key: str
    pages: tuple[int, ...] | None = None


@dataclass(frozen=True)
class MapMode:
    """A complete, data-driven description of one chart product."""

    mode_id: str
    display_name: str
    sheets: tuple[SheetDef, ...]
    waypoint_sources: tuple[WaypointSource, ...]
    waypoint_strategy: WaypointStrategy
    overlap_codes: tuple[str, ...] = ()
    #: Bilingual label shown on the toolbar's Map Type toggle button
    #: (e.g. ``'CVFR - כטר"מ'``). Falls back to ``display_name`` via
    #: :attr:`switcher_label` when left empty.
    toolbar_label: str = ""

    # -- derived views -----------------------------------------------------

    @property
    def cache_namespace(self) -> str:
        """Sub-directory / QSettings-group name for this mode's state."""
        return self.mode_id

    @property
    def switcher_label(self) -> str:
        """Text for the toolbar Map Type toggle button.

        Uses the bilingual :attr:`toolbar_label` when set, otherwise
        the plain :attr:`display_name` — so a future mode that forgets
        to set a label still renders a sensible button.
        """
        return self.toolbar_label or self.display_name

    def sheet(self, key: str) -> SheetDef:
        """Return the :class:`SheetDef` for ``key`` or raise ``KeyError``."""
        for s in self.sheets:
            if s.key == key:
                return s
        raise KeyError(f"mode {self.mode_id!r} has no sheet {key!r}")

    @property
    def sheet_keys(self) -> tuple[str, ...]:
        """All configured sheet keys (each needs a URL/path field)."""
        return tuple(s.key for s in self.sheets)

    @property
    def map_sheet_keys(self) -> tuple[str, ...]:
        """Sheet keys rendered as map layers, in declared order."""
        return tuple(s.key for s in self.sheets if s.role is SheetRole.MAP)

    @property
    def waypoint_sheet_keys(self) -> tuple[str, ...]:
        """Configured sheet keys that supply reporting points."""
        # Preserve declared order, dedupe (LSA references two sheets;
        # CVFR one). A sheet may be both a map and a waypoint source
        # (LSA north/south), which is exactly why this is derived from
        # ``waypoint_sources`` rather than from ``role``.
        seen: list[str] = []
        for ws in self.waypoint_sources:
            if ws.sheet_key not in seen:
                seen.append(ws.sheet_key)
        return tuple(seen)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Canonical id of the default / first-run mode. CVFR is the product
#: the app shipped with through v3.3, so an upgrading user lands here.
DEFAULT_MODE_ID: Final[str] = "cvfr"

#: CVFR — the original product. Built from the long-standing
#: :mod:`chart_source` constants so existing imports keep working and
#: there is a single source of truth for the CVFR URLs/filenames.
_CVFR_MODE: Final[MapMode] = MapMode(
    mode_id="cvfr",
    display_name="CVFR",
    sheets=(
        SheetDef(
            key="north",
            role=SheetRole.MAP,
            display_name=SHEET_DISPLAY_NAMES["north"],
            cache_pdf_filename=CACHE_FILENAMES["north"],
            default_url=CAAI_CHART_URLS["north"],
        ),
        SheetDef(
            key="south",
            role=SheetRole.MAP,
            display_name=SHEET_DISPLAY_NAMES["south"],
            cache_pdf_filename=CACHE_FILENAMES["south"],
            default_url=CAAI_CHART_URLS["south"],
        ),
        SheetDef(
            key="back",
            role=SheetRole.WAYPOINTS,
            display_name=SHEET_DISPLAY_NAMES["back"],
            cache_pdf_filename=CACHE_FILENAMES["back"],
            default_url=CAAI_CHART_URLS["back"],
        ),
    ),
    waypoint_sources=(WaypointSource(sheet_key="back", pages=None),),
    waypoint_strategy=WaypointStrategy.VECTOR_HYBRID,
    overlap_codes=("SDROT", "OMMER", "ENGDI"),
    toolbar_label='CVFR - כטר"מ',
)

#: LSA (Light Sport Aircraft) chart URLs. Unlike CVFR these are *not*
#: in :mod:`chart_source` (whose dicts are the CVFR north/south/back
#: triple) — LSA owns its own URLs here so the registry is the single
#: source of truth for the multi-mode URL set (the Legal/Copyright
#: dialog enumerates ``all_modes()`` → each sheet's ``default_url``).
#:
#: The AIP edition tag here is ``b'-08``; bump alongside CVFR's ``b'-03``
#: when CAAI republishes (see the build cookbook's calibration-drift
#: verification step).
_LSA_NORTH_URL: Final[str] = (
    "https://www.gov.il/BlobFolder/guide/aip/he/"
    "aip_%D7%91'-08%20%D7%92%D7%99%D7%9C%D7%99%D7%95%D7%9F%20"
    "%D7%A6%D7%A4%D7%95%D7%A0%D7%99.pdf"
)
_LSA_SOUTH_URL: Final[str] = (
    "https://www.gov.il/BlobFolder/guide/aip/he/"
    "aip_%D7%91'-08%20%D7%92%D7%99%D7%9C%D7%99%D7%95%D7%9F%20"
    "%D7%93%D7%A8%D7%95%D7%9E%D7%99.pdf"
)

#: LSA — two map halves, no separate back-pages PDF. The
#: reporting-point list is printed on page 2 (index 1) of *both* the
#: north and south PDFs (the same national list on each); the loader
#: OCRs both and dedups by code. ``overlap_codes`` pins the north/south
#: seam: TARAD (Tel Arad, east), BKAMA (Beit Kama, central), and NBSOR
#: (Nahal Bessor, west) all sit in the ~31.3-31.4°N seam band and are
#: printed on both halves, so they're well-spread (east→west) shared
#: anchors for the joint two-sheet fit. Codes verified against the
#: OCR'd LSA waypoint list (BKAMA = בית קמה, distinct from KKAMA =
#: כפר כמא further north).
_LSA_MODE: Final[MapMode] = MapMode(
    mode_id="lsa",
    display_name="LSA",
    sheets=(
        SheetDef(
            key="north",
            role=SheetRole.MAP,
            display_name="North sheet",
            cache_pdf_filename="lsa_north.pdf",
            default_url=_LSA_NORTH_URL,
            render_page=0,
        ),
        SheetDef(
            key="south",
            role=SheetRole.MAP,
            display_name="South sheet",
            cache_pdf_filename="lsa_south.pdf",
            default_url=_LSA_SOUTH_URL,
            render_page=0,
        ),
    ),
    waypoint_sources=(
        WaypointSource(sheet_key="north", pages=(1,)),
        WaypointSource(sheet_key="south", pages=(1,)),
    ),
    waypoint_strategy=WaypointStrategy.FULL_OCR,
    overlap_codes=("TARAD", "BKAMA", "NBSOR"),
    toolbar_label='LSA - אז"מ',
)

#: The registry. Insertion order is the order modes appear in the UI
#: switcher. Future products (Helicopter, IFR) are added in later
#: phases.
_MODES: Final[dict[str, MapMode]] = {
    _CVFR_MODE.mode_id: _CVFR_MODE,
    _LSA_MODE.mode_id: _LSA_MODE,
}


def all_modes() -> tuple[MapMode, ...]:
    """Every registered mode, in UI / switcher order."""
    return tuple(_MODES.values())


def mode_ids() -> tuple[str, ...]:
    """Every registered mode id, in UI / switcher order."""
    return tuple(_MODES.keys())


def has_mode(mode_id: str) -> bool:
    """True iff ``mode_id`` is a registered mode."""
    return mode_id in _MODES


def get_mode(mode_id: str) -> MapMode:
    """Return the :class:`MapMode` for ``mode_id`` or raise ``KeyError``."""
    try:
        return _MODES[mode_id]
    except KeyError:
        raise KeyError(
            f"unknown map mode {mode_id!r}; registered: {tuple(_MODES)}"
        ) from None


def default_mode() -> MapMode:
    """The default / first-run mode (CVFR)."""
    return _MODES[DEFAULT_MODE_ID]


def coerce_mode_id(mode_id: str | None) -> str:
    """Return ``mode_id`` if registered, else :data:`DEFAULT_MODE_ID`.

    Used when reading a persisted ``current_map_mode`` that might name
    a mode this build no longer ships (downgrade) or has never heard of
    (corrupted settings) — we fall back to CVFR rather than crash.
    """
    if mode_id and mode_id in _MODES:
        return mode_id
    return DEFAULT_MODE_ID
