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

"""One-time v3.3 → v4 migration of per-project state into the CVFR
mode namespace.

Through v3.3 the app kept all of its per-project cache state flat
under ``<project_root>/.cvfr_routemaster/`` and all of its per-sheet
QSettings under flat keys (``pdf_north``, ``map_north_x``, …). v4
namespaces per-mode state under ``.cvfr_routemaster/<mode_id>/`` and
under the ``modes/<mode_id>/`` QSettings group so multiple chart
products (CVFR, LSA, …) can coexist.

This module performs a **one-time, non-destructive** relocation of
the legacy CVFR state into the ``cvfr`` namespace so an upgrading
user's existing calibration / rendered maps / waypoint cache / saved
layout carry over seamlessly:

* Cache files are **copied** (not moved) into ``cvfr/``; the original
  flat files are left in place as an implicit backup. If anything is
  wrong with the migrated copy the user can still find the original.
* QSettings keys are **copied** into the ``modes/cvfr/`` group; the
  flat keys are left untouched (they simply stop being read).
* A marker file (:data:`MARKER_FILENAME`) makes the migration
  idempotent — it runs at most once per project root, and is a no-op
  on fresh installs (which already ship their seeds under ``cvfr/``).

Globals that are deliberately NOT migrated (they stay shared across
modes): ``font_settings.json``, ``settings.ini``, ``satellite_tiles/``,
and the layout debug log.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QSettings

from . import map_modes
from .settings_store import (
    CURRENT_MAP_MODE_KEY,
    _mode_key,
    _settings,
    load_current_map_mode,
)

#: Marker written under the flat ``.cvfr_routemaster/`` dir once the
#: migration has run. Hidden-style leading dot so it sorts with other
#: dotfiles and doesn't look like app data.
MARKER_FILENAME: str = ".v4_migrated"

#: Flat cache files relocated (copied) into ``cvfr/``. Each is a file
#: that lived directly under ``.cvfr_routemaster/`` in v3.3.
_FLAT_CACHE_FILES: tuple[str, ...] = (
    "geo_calibration.json",
    "map_north.png",
    "map_south.png",
    "map_images_meta.json",
    "waypoints_cache.json",
    "altitude_arrows_north.json",
    "altitude_arrows_south.json",
    "chart_sources.json",
    "map_layout.json",
)

#: Flat cache *directories* relocated (copied) into ``cvfr/``.
_FLAT_CACHE_DIRS: tuple[str, ...] = ("charts",)

#: Flat QSettings keys copied into the ``modes/cvfr/`` group. These
#: are exactly the per-mode keys the settings_store now reads through
#: :func:`_mode_key`.
_PER_MODE_QSETTINGS_KEYS: tuple[str, ...] = (
    "pdf_north",
    "pdf_south",
    "pdf_back",
    "map_layout_saved",
    "map_north_x",
    "map_north_y",
    "map_north_scale",
    "map_south_x",
    "map_south_y",
    "map_south_scale",
    "map_selected_sheet",
    "map_view_saved",
    "map_view_m11",
    "map_view_m12",
    "map_view_m13",
    "map_view_m21",
    "map_view_m22",
    "map_view_m23",
    "map_view_m31",
    "map_view_m32",
    "map_view_m33",
    "map_view_scroll_h",
    "map_view_scroll_v",
)


def _flat_dir(project_root: Path) -> Path:
    return project_root / ".cvfr_routemaster"


def marker_path(project_root: Path) -> Path:
    """Path to the one-time migration marker for ``project_root``."""
    return _flat_dir(project_root) / MARKER_FILENAME


def already_migrated(project_root: Path) -> bool:
    """True iff the v4 migration has already run for ``project_root``."""
    return marker_path(project_root).is_file()


def _copy_flat_caches(project_root: Path, mode_id: str) -> list[str]:
    """Copy legacy flat cache files/dirs into ``<flat>/<mode_id>/``.

    Existing destinations are never overwritten (a fresh v4 release
    that already shipped seeds under ``cvfr/`` wins over an older flat
    copy that happens to coexist). Returns the relative names copied,
    for diagnostics / tests.
    """
    flat = _flat_dir(project_root)
    dest_base = flat / mode_id
    copied: list[str] = []
    if not flat.is_dir():
        return copied
    dest_base.mkdir(parents=True, exist_ok=True)

    for name in _FLAT_CACHE_FILES:
        src = flat / name
        dst = dest_base / name
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            copied.append(name)

    for name in _FLAT_CACHE_DIRS:
        src = flat / name
        dst = dest_base / name
        if src.is_dir() and not dst.exists():
            shutil.copytree(src, dst)
            copied.append(name + "/")

    return copied


def _copy_qsettings_keys(settings: QSettings, mode_id: str) -> list[str]:
    """Copy flat per-mode QSettings keys into the ``modes/<mode>/``
    group. Never overwrites a value already present in the group.
    Returns the keys copied, for diagnostics / tests."""
    copied: list[str] = []
    for key in _PER_MODE_QSETTINGS_KEYS:
        if not settings.contains(key):
            continue
        target = _mode_key(key, mode_id)
        if settings.contains(target):
            continue
        settings.setValue(target, settings.value(key))
        copied.append(key)
    return copied


def migrate_v33_to_v4(project_root: Path) -> bool:
    """Run the one-time v3.3 → v4 relocation for ``project_root``.

    Idempotent: a no-op (returns ``False``) once the marker exists.
    On first run it copies legacy flat CVFR caches into ``cvfr/``,
    copies flat per-mode QSettings keys into the ``modes/cvfr/`` group,
    sets ``current_map_mode`` to ``cvfr`` if unset, writes the marker,
    and returns ``True``.

    Non-destructive: originals (flat files and flat QSettings keys) are
    left in place as a backup.
    """
    flat = _flat_dir(project_root)
    if already_migrated(project_root):
        return False

    mode_id = map_modes.DEFAULT_MODE_ID

    # Caches: only if the flat dir exists at all. A brand-new install
    # has nothing here; we still drop the marker below so we don't
    # re-scan every launch.
    if flat.is_dir():
        _copy_flat_caches(project_root, mode_id)

    settings = _settings()
    _copy_qsettings_keys(settings, mode_id)
    if not load_current_map_mode():
        settings.setValue(CURRENT_MAP_MODE_KEY, mode_id)
    settings.sync()

    # Write the marker last, so an interrupted migration retries next
    # launch rather than silently leaving state half-relocated. The
    # copy steps skip existing destinations, so a retry is safe.
    flat.mkdir(parents=True, exist_ok=True)
    marker_path(project_root).write_text("v4\n", encoding="utf-8")
    return True
