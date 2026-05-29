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

"""Project-wide pytest fixtures.

This conftest exists primarily to enforce one global safety
property:

  **No test, anywhere in the suite, may touch the developer's
  real OS-native QSettings store** — the Windows registry under
  ``HKCU\\Software\\CVFRRouteMaster``, or
  ``~/.config/CVFRRouteMaster/`` on Linux, or the equivalent
  plist on macOS.

Why this matters
----------------

v3.3+ switched user preferences from the OS-native QSettings
backend to a project-root ``settings.ini`` file (see
:mod:`cvfr_routemaster.settings_store`). As part of that
switch, :func:`settings_store._settings` runs a one-shot
migration on first call: read everything from the legacy
native backend, copy into the new INI, then ``clear()`` the
native backend. That clear is irrevocable.

If a test triggers ``_settings()`` (directly or via any public
loader) without also isolating the legacy backend, the
migration will:

  1. Read the developer's real registry / config-file values.
  2. Copy them into the test's tmp INI.
  3. **Clear the real registry / config-file.**
  4. Pytest then cleans up tmp, taking the copy with it.

Net result: the developer's accumulated personal preferences
(window layout, font sizes, calibration positions, satellite
notice state) get silently wiped by the test suite. The
production code is doing exactly what it was designed to do
— it's the tests that are leaking state.

The autouse fixture below routes
``settings_store._legacy_native_settings`` to a per-test tmp
INI before any test code runs, so the migration path (even if
accidentally triggered) reads from + clears the throwaway file
instead of the real native store. Tests that *want* to exercise
the migration explicitly can override this isolation by
monkeypatching ``_legacy_native_settings`` themselves within
the test body (see
``tests/test_settings_ini_backend.py::_make_fake_legacy_store``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    from PySide6.QtCore import QSettings
    from cvfr_routemaster import settings_store

    _SETTINGS_STORE_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 absent in some envs
    _SETTINGS_STORE_AVAILABLE = False


@pytest.fixture(autouse=True)
def _isolate_legacy_native_settings(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Autouse safety net: redirect
    :func:`settings_store._legacy_native_settings` to a per-test
    empty INI file so the one-shot migration helper in
    :func:`settings_store._settings` can never read or clear the
    developer's real native QSettings backend.

    Per-test tmp file (not per-session) so two tests that *do*
    intentionally exercise migration don't see each other's
    leftover state.

    No-op if PySide6 isn't importable (some CI environments
    skip GUI tests entirely; the rest of the suite still needs
    to import this conftest).
    """
    if not _SETTINGS_STORE_AVAILABLE:
        return

    isolation_path: Path = tmp_path_factory.mktemp(
        "legacy_native_isolation"
    ) / "fake_native.ini"

    def _isolated_legacy_native_settings() -> QSettings:
        # Fresh handle per call (mirrors the production factory's
        # contract). Same file each time so a test that writes via
        # an explicit override of ``_legacy_native_settings``
        # within its own body can still read its own writes back.
        return QSettings(str(isolation_path), QSettings.Format.IniFormat)

    monkeypatch.setattr(
        settings_store,
        "_legacy_native_settings",
        _isolated_legacy_native_settings,
    )
