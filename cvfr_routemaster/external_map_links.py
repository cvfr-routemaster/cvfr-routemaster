"""
Build URLs for opening a waypoint in Bing / Google / Apple Maps in the system browser.

Aerial / satellite with road labels where the provider supports it in the URL.
Zoom is calibrated to match a comfortable Bing level (~14.8) on all three.

Also exposes :func:`open_external_url`, a small wrapper around
:class:`QDesktopServices` that fixes the well-known Linux + PyInstaller
foot-gun: a frozen bundle sets ``LD_LIBRARY_PATH`` to its own extracted
Qt libraries, and Qt's ``openUrl`` then ``fork()`` + ``exec()``-s
``xdg-open`` which inherits that environment. The system browser
launched by ``xdg-open`` then either picks up the bundle's incompatible
Qt/libstdc++ and crashes on startup, or silently bails out — either
way the user just sees nothing happen when they click a map link. The
helper invokes ``xdg-open`` ourselves on Linux with the original
``LD_LIBRARY_PATH`` (which PyInstaller stashes in
``LD_LIBRARY_PATH_ORIG``) restored, so the browser uses the system's
own libraries.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

# Supported stored preference values (QSettings).
MAP_LINK_PROVIDER_BING = "bing"
MAP_LINK_PROVIDER_GOOGLE = "google"
MAP_LINK_PROVIDER_APPLE = "apple"

MAP_LINK_PROVIDERS: tuple[tuple[str, str], ...] = (
    (MAP_LINK_PROVIDER_BING, "Bing Maps"),
    (MAP_LINK_PROVIDER_GOOGLE, "Google Maps"),
    (MAP_LINK_PROVIDER_APPLE, "Apple Maps"),
)

# User-calibrated: Bing "lvl" in the web app. Google "@…z" and Apple "z" use the same
# number so scale is in the same ballpark (each provider still differs slightly).
MAP_LINK_ZOOM = 14.8


def normalize_map_link_provider(value: str | None) -> str:
    allowed = {p for p, _ in MAP_LINK_PROVIDERS}
    if value in allowed:
        return value
    return MAP_LINK_PROVIDER_BING


def external_map_url(lat: float, lon: float, provider: str) -> QUrl:
    """Aerial/satellite with labels (where URL allows), centered on (lat, lon)."""
    p = normalize_map_link_provider(provider)
    z = MAP_LINK_ZOOM
    if p == MAP_LINK_PROVIDER_BING:
        # style=h = aerial with labels (style=a is pure aerial, no road names).
        # cp = latitude~longitude; lvl accepts decimals in the web client.
        return QUrl(f"https://www.bing.com/maps?cp={lat}~{lon}&lvl={z}&style=h")
    if p == MAP_LINK_PROVIDER_GOOGLE:
        # Satellite/imagery with the usual labels toggle available in the UI; z matches ~Bing lvl.
        return QUrl(f"https://www.google.com/maps/@{lat},{lon},{z}z/data=!3m1!1e3")
    return QUrl(f"https://maps.apple.com/?ll={lat},{lon}&z={z}")


# ---------------------------------------------------------------------------
# Cross-platform URL launcher
# ---------------------------------------------------------------------------


def _sanitized_linux_env() -> dict[str, str]:
    """Return a copy of ``os.environ`` with PyInstaller's library-path
    overrides reverted to their pre-bundle values.

    PyInstaller's bootloader, when it launches the user's frozen
    program, prepends its temporary extraction directory to
    ``LD_LIBRARY_PATH`` (and ``DYLD_LIBRARY_PATH`` on macOS) so the
    bundled Qt / Python shared objects are found first. Before doing
    so it saves the original values in ``LD_LIBRARY_PATH_ORIG`` etc.
    so child processes can restore them — exactly what we need for
    ``xdg-open``, which we want to behave as if it were spawned from
    a normal shell environment.

    We also drop ``LD_PRELOAD`` outright — if the user set one it
    shouldn't leak into the browser process, and PyInstaller doesn't
    set this itself.
    """
    env = os.environ.copy()
    for key in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        orig_key = f"{key}_ORIG"
        if orig_key in env:
            env[key] = env[orig_key]
        else:
            env.pop(key, None)
    env.pop("LD_PRELOAD", None)
    return env


def open_external_url(url: QUrl) -> bool:
    """Open ``url`` in the user's default browser.

    On Linux we bypass ``QDesktopServices.openUrl`` and spawn
    ``xdg-open`` directly with a cleaned environment so a
    PyInstaller-bundled binary doesn't poison the child process's
    library search path (see module docstring). The function returns
    ``True`` when the child was successfully spawned; the child's
    own success in actually opening a browser is asynchronous and
    not observable from Python.

    Falls back to :meth:`QDesktopServices.openUrl` on Linux when
    ``xdg-open`` is not on ``PATH``, and uses it unconditionally on
    every non-Linux platform (Qt's macOS / Windows implementations
    don't suffer from the LD_LIBRARY_PATH problem).
    """
    if sys.platform.startswith("linux"):
        xdg = shutil.which("xdg-open")
        if xdg is not None:
            try:
                subprocess.Popen(
                    [xdg, url.toString()],
                    env=_sanitized_linux_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return True
            except OSError:
                # Fall through to Qt's fallback so the user at least
                # gets Qt's own error reporting via the status-bar
                # caller, rather than a silent no-op.
                pass
    return QDesktopServices.openUrl(url)
