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

"""Locate and load the app icon at runtime.

The icon ships as a PNG bundled inside ``cvfr_routemaster/resources/``
(written by ``scripts/generate_release_icon.py``), so it's
distributed both with the dev source tree (for ``py -m
cvfr_routemaster``) and with the PyInstaller frozen build (via the
spec file's ``datas`` clause — ``cvfr_routemaster/resources`` is
copied verbatim into ``sys._MEIPASS`` at launch).

Loading the icon requires a real filesystem path because Qt's
``QIcon`` constructor that accepts a string treats it as a path.
In both dev and frozen modes ``Path(__file__).parent / "resources"``
resolves to a real on-disk directory:

  * **Dev**: it's just ``<repo>/cvfr_routemaster/resources/``.
  * **Frozen**: PyInstaller writes the python source / package
    data to ``sys._MEIPASS`` and rewrites each module's
    ``__file__`` so it points at the extracted copy. So
    ``Path(__file__).parent`` is
    ``<sys._MEIPASS>/cvfr_routemaster/`` and the resources/ sub-
    folder sits exactly where the spec dropped it.

This module exposes one function (:func:`app_icon`) that returns a
:class:`QIcon` — call it once at app startup, push it onto
``QApplication.setWindowIcon`` (which propagates to every
top-level window that doesn't override) and onto the MainWindow's
own ``setWindowIcon`` (Qt's title-bar icon is sourced from the
window itself, not the app, so both setters are needed).

If the bundled file is missing (a build that pre-dates the
runtime PNG, or a manual checkout that hasn't run the icon
generator yet) the function returns an empty ``QIcon`` so the
caller stays safe — Qt falls back to its own default rather than
crashing on an invalid path.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon


def _icon_path() -> Path:
    """The on-disk location of the bundled ``app_icon.png``.

    Pure-Python resolution (no ``importlib.resources``) because:
      * ``importlib.resources.files`` returns a ``Traversable``
        that's only guaranteed to be a real ``Path`` in modes
        we already support (filesystem package + PyInstaller
        --onefile extraction). For a binary asset that we need
        to hand off to Qt as a string, the simpler path
        arithmetic is honest about the contract.
      * PyInstaller is well-documented to extract package data
        files alongside the source modules and rewrite
        ``__file__`` accordingly; the spec file enumerates
        ``cvfr_routemaster/resources/*`` in its ``datas``
        clause so this lookup hits in both modes without
        needing per-mode branches.
    """
    return Path(__file__).resolve().parent / "resources" / "app_icon.png"


def app_icon() -> QIcon:
    """Return the bundled app icon, or an empty :class:`QIcon` if
    the PNG isn't on disk (defensive: ``scripts/generate_release_icon.py``
    creates it as part of the release build, but a fresh
    checkout that has never run the icon generator will be
    missing the file and we don't want that to crash startup).
    """
    path = _icon_path()
    if not path.is_file():
        return QIcon()
    return QIcon(str(path))


def _airplane_mode_icon_path() -> Path:
    """The on-disk location of the bundled ``airplane_mode_icon.png``.

    Same resolution strategy as :func:`_icon_path` — both files live
    in ``cvfr_routemaster/resources/`` and ship via the spec file's
    ``datas`` clause that copies the entire resources/ folder into
    PyInstaller's runtime tree.
    """
    return Path(__file__).resolve().parent / "resources" / "airplane_mode_icon.png"


def airplane_mode_icon() -> QIcon:
    """Return the bundled airplane-mode toolbar glyph (a white
    tilted-airplane silhouette), or an empty :class:`QIcon` if
    ``airplane_mode_icon.png`` isn't on disk.

    Same defensive contract as :func:`app_icon`: a fresh checkout
    that hasn't run ``scripts/generate_release_icon.py`` yet
    will fall back to an empty icon, and Qt renders the toolbar
    button with its text-only fallback — so the airplane-mode
    toggle is still functionally usable, just less pretty.
    """
    path = _airplane_mode_icon_path()
    if not path.is_file():
        return QIcon()
    return QIcon(str(path))
