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

"""Tests for window-geometry + splitter-state persistence.

Two complementary layers are covered:

1. ``settings_store.save_window_layout`` / ``load_window_layout`` round-trip
   raw byte payloads through ``QSettings`` correctly, including the
   "nothing saved yet" → ``None`` case and a partial-corruption case.
2. The bytes produced by ``QMainWindow.saveGeometry`` and
   ``QSplitter.saveState`` survive that round-trip and are accepted by
   their respective ``restoreGeometry`` / ``restoreState`` counterparts —
   i.e. our serialisation pipeline doesn't mangle the Qt blobs.

We isolate ``QSettings`` per-test by monkey-patching
``settings_store._settings`` to point at a temp INI file, so these tests
never touch the user's real CVFR Route Master settings.
"""

from __future__ import annotations

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QSplitter  # noqa: E402

from cvfr_routemaster import settings_store  # noqa: E402
from cvfr_routemaster.settings_store import (  # noqa: E402
    load_window_layout,
    save_window_layout,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Redirect ``settings_store._settings()`` to a per-test INI file so the
    tests never read or write the user's real config.

    Using ``IniFormat`` explicitly (rather than the native registry on
    Windows) keeps the tests fully self-contained — the temp file lives
    under ``tmp_path`` and is auto-cleaned by pytest.
    """
    ini_path = tmp_path / "test_settings.ini"

    def _factory() -> QSettings:
        return QSettings(str(ini_path), QSettings.Format.IniFormat)

    monkeypatch.setattr(settings_store, "_settings", _factory)
    return ini_path


def test_load_window_layout_returns_none_when_nothing_saved(isolated_settings):
    """First-launch contract: no saved entry → ``None`` so callers fall
    back to their hard-coded defaults instead of trying to restore an
    empty/garbage byte blob."""
    assert load_window_layout() is None


def test_save_then_load_round_trips_exact_bytes(isolated_settings):
    """Whatever bytes go in must come back out byte-for-byte. Qt's
    ``restoreGeometry`` is finicky about its input format — any silent
    mutation by ``QSettings`` (e.g. dropping NULs, re-encoding) would
    cause silent restore failures in production."""
    geom = bytes(range(64))
    split = bytes(b"splitter-state-payload-\x00\x01\x02\xff")
    save_window_layout(geometry=geom, splitter_state=split)
    loaded = load_window_layout()
    assert loaded is not None
    assert loaded == (geom, split)


def test_save_overwrites_previous(isolated_settings):
    """Persisting a fresh layout must replace the old one — otherwise
    closing the app a second time at a new size would silently keep the
    first session's geometry forever."""
    save_window_layout(geometry=b"first", splitter_state=b"first-split")
    save_window_layout(geometry=b"second", splitter_state=b"second-split")
    assert load_window_layout() == (b"second", b"second-split")


def test_qmainwindow_geometry_round_trips_through_settings(qapp, isolated_settings):
    """End-to-end check: a real ``QMainWindow``'s saveGeometry blob,
    persisted and restored via our settings layer, is accepted by
    ``restoreGeometry`` (returns True) and reproduces the same size on a
    second window. Catches any encoding bug between Qt's binary format
    and the QSettings INI escape rules."""
    src = QMainWindow()
    try:
        src.resize(1234, 567)
        save_window_layout(
            geometry=bytes(src.saveGeometry()),
            splitter_state=b"",
        )
        loaded = load_window_layout()
        assert loaded is not None
        geom_bytes, _ = loaded

        dst = QMainWindow()
        try:
            assert dst.restoreGeometry(geom_bytes) is True
            dst.show()
            qapp.processEvents()
            assert dst.size().width() == 1234
            assert dst.size().height() == 567
        finally:
            dst.close()
            dst.deleteLater()
    finally:
        src.deleteLater()


def test_qsplitter_state_round_trips_through_settings(qapp, isolated_settings):
    """Same idea for splitter pane sizes: bytes from ``saveState`` must
    survive QSettings and be accepted by ``restoreState``. We use the
    sizes set on the source splitter as the ground truth and assert the
    destination splitter ends up with the same proportions."""
    src = QSplitter(Qt.Orientation.Horizontal)
    try:
        for _ in range(3):
            src.addWidget(QLabel("pane"))
        src.setSizes([100, 700, 200])
        src.resize(1000, 400)
        src.show()
        qapp.processEvents()
        sizes_before = list(src.sizes())

        save_window_layout(
            geometry=b"",
            splitter_state=bytes(src.saveState()),
        )
        loaded = load_window_layout()
        assert loaded is not None
        _, split_bytes = loaded

        dst = QSplitter(Qt.Orientation.Horizontal)
        try:
            for _ in range(3):
                dst.addWidget(QLabel("pane"))
            dst.resize(1000, 400)
            dst.show()
            qapp.processEvents()
            assert dst.restoreState(split_bytes) is True
            qapp.processEvents()
            assert list(dst.sizes()) == sizes_before
        finally:
            dst.close()
            dst.deleteLater()
    finally:
        src.close()
        src.deleteLater()


def test_load_window_layout_handles_missing_individual_keys(
    isolated_settings, monkeypatch
):
    """Defensive: if the saved-flag is set but one of the payload keys
    went missing (manual edits, partial migration), the loader must
    return ``None`` rather than blow up — the GUI then silently falls
    back to defaults instead of crashing on startup.
    """
    s = settings_store._settings()
    s.setValue("window_layout_saved", True)
    s.setValue("window_geometry", b"some-geometry")
    s.sync()
    assert load_window_layout() is None
