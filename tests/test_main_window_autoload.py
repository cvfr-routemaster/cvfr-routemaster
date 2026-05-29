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

"""Tests for :meth:`MainWindow._maybe_autoload_on_start` and its
``_sources_set()`` gate.

The regression covered here was a v3.3 first-launch failure: with the
shipped ``chart_sources.json`` populating ``_source_*`` with the three
CAAI URLs, autoload silently no-op'd on first launch because the legacy
``_paths_valid()`` gate required the resolved ``_*_path`` fields to
point at on-disk PDFs (which they don't until the URL has been
downloaded). The user saw an empty viewport, no progress dialog, no
error — just nothing.

The fix replaces ``_paths_valid()`` with ``_sources_set()``: autoload
fires whenever all three sources are configured (URL or local path),
and the URL download flow inside ``_ensure_chart_sources_resolved``
shows the progress dialog interactively.

**Test design note** — we deliberately do NOT spin up a full
:class:`MainWindow` here. The functions under test (``_sources_set``
and ``_maybe_autoload_on_start``) only read three instance attributes
(``_source_north`` / ``_source_south`` / ``_source_back``), call
:func:`settings_store.load_autoload_enabled`, and dispatch to
``_load_all``. None of that needs a Qt widget. Avoiding a real
``MainWindow`` (which constructs ~10 child widgets, signals, and
timers) sidesteps the Qt-state-stacking pollution observed in
``test_ui_layout.py`` when many ``MainWindow`` instances are created
sequentially within one pytest process.

The trade-off: a test harness that's not 100% type-identical to the
production class. We mitigate by binding the REAL unbound methods
from ``MainWindow`` onto the harness, so any future signature drift
in those methods immediately breaks these tests.
"""

from __future__ import annotations

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings  # noqa: E402

from cvfr_routemaster import settings_store  # noqa: E402
from cvfr_routemaster.main_window import MainWindow  # noqa: E402
from cvfr_routemaster.settings_store import (  # noqa: E402
    save_autoload_enabled,
    save_pdf_paths,
)


CAAI_NORTH = (
    "https://www.gov.il/BlobFolder/guide/aip/he/"
    "aip_%D7%91'-03%20CVFR%20%D7%A6%D7%A4%D7%95%D7%A0%D7%99-.pdf"
)
CAAI_SOUTH = (
    "https://www.gov.il/BlobFolder/guide/aip/he/"
    "aip_%D7%91'-03%20CVFR%20%D7%93%D7%A8%D7%95%D7%9E%D7%99.pdf"
)
CAAI_BACK = (
    "https://www.gov.il/BlobFolder/guide/aip/he/"
    "aip_%D7%91'-03CVFR%20%D7%90%D7%97%D7%95%D7%A8%D7%99.pdf"
)


class _AutoloadHarness:
    """Minimal duck-typed surface for the autoload-gate methods.

    Holds the three ``_source_*`` strings the production code reads,
    counts ``_load_all`` invocations, and binds the REAL unbound
    methods from :class:`MainWindow` so any signature/contract drift
    is caught here too.
    """

    def __init__(self, north: str, south: str, back: str) -> None:
        self._source_north = north
        self._source_south = south
        self._source_back = back
        self.load_all_calls: int = 0

    def _load_all(self) -> None:
        self.load_all_calls += 1

    _sources_set = MainWindow._sources_set
    _maybe_autoload_on_start = MainWindow._maybe_autoload_on_start


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Redirect ``QSettings`` to a per-test INI so ``save_pdf_paths`` /
    ``save_autoload_enabled`` stay out of the user's real registry."""
    ini_path = tmp_path / "test_settings.ini"

    def _factory() -> QSettings:
        return QSettings(str(ini_path), QSettings.Format.IniFormat)

    monkeypatch.setattr(settings_store, "_settings", _factory)
    return ini_path


# ---------------------------------------------------------------------------
# _sources_set() unit tests
# ---------------------------------------------------------------------------


def test_sources_set_returns_true_when_all_three_urls_present():
    h = _AutoloadHarness(CAAI_NORTH, CAAI_SOUTH, CAAI_BACK)
    assert h._sources_set() is True


def test_sources_set_returns_true_when_all_three_local_paths_present(tmp_path):
    n = tmp_path / "north.pdf"
    s = tmp_path / "south.pdf"
    b = tmp_path / "back.pdf"
    for p in (n, s, b):
        p.write_bytes(b"")
    h = _AutoloadHarness(str(n), str(s), str(b))
    assert h._sources_set() is True


def test_sources_set_returns_true_for_mixed_url_and_local(tmp_path):
    local_pdf = tmp_path / "north.pdf"
    local_pdf.write_bytes(b"")
    h = _AutoloadHarness(str(local_pdf), CAAI_SOUTH, CAAI_BACK)
    assert h._sources_set() is True


def test_sources_set_returns_false_when_any_source_empty():
    h = _AutoloadHarness(CAAI_NORTH, "", CAAI_BACK)
    assert h._sources_set() is False


def test_sources_set_returns_false_when_all_sources_empty():
    h = _AutoloadHarness("", "", "")
    assert h._sources_set() is False


def test_sources_set_returns_false_when_north_only_set():
    h = _AutoloadHarness(CAAI_NORTH, "", "")
    assert h._sources_set() is False


# ---------------------------------------------------------------------------
# _maybe_autoload_on_start() behaviour
# ---------------------------------------------------------------------------


def test_autoload_fires_load_all_when_sources_are_caai_urls(isolated_settings):
    """The v3.3 first-launch regression.

    Sources are the three CAAI URLs (the shipped default state).
    Before the fix, the legacy ``_paths_valid()`` gate would block
    autoload because no PDFs were downloaded yet. After the fix,
    ``_sources_set()`` returns True on the strength of the source
    strings alone and ``_load_all`` MUST be called so the download
    flow can start.
    """
    save_autoload_enabled(True)
    h = _AutoloadHarness(CAAI_NORTH, CAAI_SOUTH, CAAI_BACK)
    h._maybe_autoload_on_start()
    assert h.load_all_calls == 1


def test_autoload_fires_load_all_when_sources_are_local_paths(
    tmp_path, isolated_settings
):
    save_autoload_enabled(True)
    n = tmp_path / "north.pdf"
    s = tmp_path / "south.pdf"
    b = tmp_path / "back.pdf"
    for p in (n, s, b):
        p.write_bytes(b"")
    h = _AutoloadHarness(str(n), str(s), str(b))
    h._maybe_autoload_on_start()
    assert h.load_all_calls == 1


def test_autoload_fires_load_all_when_sources_are_mixed(
    tmp_path, isolated_settings
):
    save_autoload_enabled(True)
    local_pdf = tmp_path / "north.pdf"
    local_pdf.write_bytes(b"")
    h = _AutoloadHarness(str(local_pdf), CAAI_SOUTH, CAAI_BACK)
    h._maybe_autoload_on_start()
    assert h.load_all_calls == 1


def test_autoload_does_not_fire_when_autoload_disabled(isolated_settings):
    save_autoload_enabled(False)
    h = _AutoloadHarness(CAAI_NORTH, CAAI_SOUTH, CAAI_BACK)
    h._maybe_autoload_on_start()
    assert h.load_all_calls == 0


def test_autoload_does_not_fire_when_sources_partial(isolated_settings):
    """A user who's cleared one of the three source fields shouldn't
    get a nag dialog on every launch. Autoload no-ops; they'll find
    the empty viewport and open Settings themselves."""
    save_autoload_enabled(True)
    h = _AutoloadHarness(CAAI_NORTH, "", CAAI_BACK)
    h._maybe_autoload_on_start()
    assert h.load_all_calls == 0


def test_autoload_does_not_fire_when_no_sources_set(isolated_settings):
    save_autoload_enabled(True)
    h = _AutoloadHarness("", "", "")
    h._maybe_autoload_on_start()
    assert h.load_all_calls == 0


def test_autoload_disabled_takes_precedence_over_sources_set(isolated_settings):
    """If autoload is off AND sources are set, the explicit user
    preference wins (no-op). Verifies the order of checks inside
    ``_maybe_autoload_on_start`` — autoload-disabled is checked
    first."""
    save_autoload_enabled(False)
    h = _AutoloadHarness(CAAI_NORTH, CAAI_SOUTH, CAAI_BACK)
    h._maybe_autoload_on_start()
    assert h.load_all_calls == 0


def test_save_pdf_paths_does_not_call_load_all_directly(isolated_settings):
    """Sanity belt-and-braces: persisting sources doesn't call
    ``_load_all``. The autoload trigger lives only inside
    ``_maybe_autoload_on_start``."""
    h = _AutoloadHarness("", "", "")
    save_pdf_paths(CAAI_NORTH, CAAI_SOUTH, CAAI_BACK)
    assert h.load_all_calls == 0
