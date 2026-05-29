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

"""Tests for :mod:`cvfr_routemaster.external_map_links`.

Two responsibilities are covered:

1. ``external_map_url`` — URL construction for each provider, with the
   user-calibrated zoom level baked into the right query parameter and
   the latitude / longitude formatted into the provider's expected
   placeholder. These are pure-text assertions that don't need Qt.

2. ``open_external_url`` — the Linux-side LD_LIBRARY_PATH-cleansing
   wrapper that fixes the PyInstaller foot-gun where Qt's stock
   :class:`QDesktopServices.openUrl` spawns ``xdg-open`` with the
   bundle's Qt libraries on the library path, breaking the system
   browser. The wrapper spawns ``xdg-open`` ourselves with the
   pre-bundle environment restored from PyInstaller's
   ``LD_LIBRARY_PATH_ORIG`` save-slot, and falls back to Qt's
   ``QDesktopServices`` on non-Linux platforms or when ``xdg-open``
   isn't on ``PATH``.
"""

from __future__ import annotations

import sys

import pytest

from PySide6.QtCore import QUrl

from cvfr_routemaster.external_map_links import (
    MAP_LINK_PROVIDER_APPLE,
    MAP_LINK_PROVIDER_BING,
    MAP_LINK_PROVIDER_GOOGLE,
    _sanitized_linux_env,
    external_map_url,
    normalize_map_link_provider,
    open_external_url,
)


# ---------------------------------------------------------------------------
# external_map_url
# ---------------------------------------------------------------------------


def test_external_map_url_bing_uses_cp_and_style_h() -> None:
    """Bing uses ``cp=lat~lon`` and ``style=h`` (aerial *with* labels —
    ``style=a`` is pure aerial without road names, which makes the
    chart unreadable when correlating with airspace boundaries)."""
    url = external_map_url(31.55, 34.55, MAP_LINK_PROVIDER_BING).toString()
    assert "bing.com/maps" in url
    assert "cp=31.55~34.55" in url
    assert "style=h" in url
    assert "lvl=" in url


def test_external_map_url_google_uses_atsign_and_data_1e3() -> None:
    """Google's ``data=!3m1!1e3`` tail puts the map into satellite
    mode; without it the URL opens at the user's last-used map
    style which may be the road view."""
    url = external_map_url(31.55, 34.55, MAP_LINK_PROVIDER_GOOGLE).toString()
    assert "google.com/maps" in url
    assert "@31.55,34.55" in url
    assert "data=!3m1!1e3" in url


def test_external_map_url_apple_uses_ll_and_z() -> None:
    url = external_map_url(31.55, 34.55, MAP_LINK_PROVIDER_APPLE).toString()
    assert "maps.apple.com" in url
    assert "ll=31.55,34.55" in url
    assert "z=" in url


def test_external_map_url_unknown_provider_falls_back_to_bing() -> None:
    """``normalize_map_link_provider`` quietly snaps any unrecognised
    value to Bing — same fallback the URL builder respects so a stale
    QSettings entry doesn't crash route-table clicks."""
    assert normalize_map_link_provider("does-not-exist") == MAP_LINK_PROVIDER_BING
    url = external_map_url(31.55, 34.55, "does-not-exist").toString()
    assert "bing.com" in url


# ---------------------------------------------------------------------------
# _sanitized_linux_env
# ---------------------------------------------------------------------------


def test_sanitized_env_restores_ld_library_path_from_orig(monkeypatch) -> None:
    """PyInstaller saves the original ``LD_LIBRARY_PATH`` in
    ``LD_LIBRARY_PATH_ORIG`` before prepending the bundle path; the
    sanitiser must restore the original value so xdg-open's children
    see a normal shell environment."""
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxxxxx/qt-libs")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/home/user/.local/lib")
    env = _sanitized_linux_env()
    assert env["LD_LIBRARY_PATH"] == "/home/user/.local/lib"


def test_sanitized_env_drops_ld_library_path_when_no_orig(monkeypatch) -> None:
    """If the bundle was built without a ``_ORIG`` save (older
    PyInstaller, or the user launched with their own LD_LIBRARY_PATH
    that we shouldn't propagate to the browser) we drop the variable
    outright — a missing LD_LIBRARY_PATH is the safer default for the
    child process than leaking the bundle's path."""
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxxxxx/qt-libs")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    env = _sanitized_linux_env()
    assert "LD_LIBRARY_PATH" not in env


def test_sanitized_env_drops_ld_preload(monkeypatch) -> None:
    """``LD_PRELOAD`` is dropped unconditionally — PyInstaller doesn't
    set it but if the user did, it shouldn't leak into the browser
    process and risk breaking arbitrary system libraries."""
    monkeypatch.setenv("LD_PRELOAD", "/usr/lib/libfoo.so")
    env = _sanitized_linux_env()
    assert "LD_PRELOAD" not in env


# ---------------------------------------------------------------------------
# open_external_url
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="LD_LIBRARY_PATH workaround is Linux-specific",
)
def test_open_external_url_spawns_xdg_open_on_linux(monkeypatch) -> None:
    """On Linux, when ``xdg-open`` is on PATH, the wrapper must
    bypass ``QDesktopServices.openUrl`` and spawn ``xdg-open``
    directly with the sanitised environment. The QDesktopServices
    fallback is reserved for the no-``xdg-open`` case."""
    calls: list[dict] = []

    def fake_which(name: str) -> str | None:
        return "/usr/bin/xdg-open" if name == "xdg-open" else None

    class FakePopen:
        def __init__(self, args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(
        "cvfr_routemaster.external_map_links.shutil.which", fake_which
    )
    monkeypatch.setattr(
        "cvfr_routemaster.external_map_links.subprocess.Popen", FakePopen
    )

    def boom(*_args, **_kwargs):
        raise AssertionError(
            "QDesktopServices.openUrl must not be called when xdg-open is "
            "available — that defeats the purpose of the wrapper"
        )

    monkeypatch.setattr(
        "cvfr_routemaster.external_map_links.QDesktopServices.openUrl", boom
    )

    assert open_external_url(QUrl("https://example.com/")) is True
    assert len(calls) == 1
    args = calls[0]["args"]
    assert args[0] == "/usr/bin/xdg-open"
    assert args[1] == "https://example.com/"
    # ``env`` must be supplied explicitly (not None) so subprocess
    # uses our sanitised copy rather than inheriting the bundle's.
    assert calls[0]["kwargs"].get("env") is not None


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Fallback path is Linux-specific",
)
def test_open_external_url_falls_back_to_qt_when_no_xdg_open(monkeypatch) -> None:
    """If ``xdg-open`` isn't on PATH the wrapper must defer to
    ``QDesktopServices.openUrl`` so at least Qt's own discovery
    paths get a shot — a minimal Linux install without xdg-utils
    is still a possible target."""
    monkeypatch.setattr(
        "cvfr_routemaster.external_map_links.shutil.which", lambda _n: None
    )
    qt_calls: list[QUrl] = []
    monkeypatch.setattr(
        "cvfr_routemaster.external_map_links.QDesktopServices.openUrl",
        lambda url: qt_calls.append(url) or True,
    )
    assert open_external_url(QUrl("https://example.com/")) is True
    assert len(qt_calls) == 1
    assert qt_calls[0].toString() == "https://example.com/"
