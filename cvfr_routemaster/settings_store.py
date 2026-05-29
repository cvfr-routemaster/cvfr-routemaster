from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings

from cvfr_routemaster import APP_NAME
from cvfr_routemaster.external_map_links import normalize_map_link_provider


ORG = "CVFRRouteMaster"
# QSettings identity key: deliberately version-agnostic. Bumping
# ``__version__`` must NOT change ``APP`` or every user with saved
# preferences will silently roll back to defaults on the next
# launch (QSettings keys are scoped by ORG + APP). Pull the literal
# from the package's ``APP_NAME`` constant so the name stays in
# sync with the window title's name component (only the version
# suffix differs between the two).
APP = APP_NAME


# --- Font-size preferences ------------------------------------------------
#
# Three knobs, each in CSS pixels (to match the existing
# ``ui_theme`` stylesheet which already uses ``font-size: NNpx;`` for
# the hint labels — mixing px and pt would force two reasoning models
# inside the same stylesheet and round-trip painful between the
# dialog spinboxes and the rendered output).
#
# Defaults picked so a fresh install renders identically to the
# pre-font-settings build:
#
#   * Tables and route text were not styled at all → app default
#     QApplication font (Segoe UI 9pt ≈ 12px at 96 dpi on Windows;
#     comparable Cantarell/DejaVu sizes on Linux). We pin to ``12``
#     px so the rendered size is stable regardless of which OS the
#     release runs on.
#   * Usage hints already had ``font-size: 18px`` baked into
#     ``QLabel#mapHint`` — we preserve that.
#
# Min/max bounds are advisory (the dialog uses them to clamp the
# spinboxes); the loader doesn't enforce them so a user who hand-
# edits QSettings to push past the limit just gets what they asked
# for. The lower bound stops them rendering an unreadable 1-pixel
# font and the upper bound stops them blowing up the layout to the
# point that columns wrap badly.
DEFAULT_TABLE_FONT_PX = 12
DEFAULT_ROUTE_TEXT_FONT_PX = 12
DEFAULT_HINT_FONT_PX = 18

# Airplane-mode defaults — a second, independent profile applied
# only while ``MainWindow._act_airplane_mode`` is pressed. The
# rationale is that airplane mode is the "in-flight reading view":
# the user typically has the laptop on the right-hand seat or a
# secondary monitor a couple of feet away, which is a meaningfully
# longer reading distance than the bench setup the normal-mode
# defaults are tuned for. Bigger tables and bigger route-text
# labels there are the standard ask. The hint default is kept at
# the same 18 px as normal-mode hints — every ``QLabel#mapHint`` is
# already hidden in airplane mode, so the hint knob is preserved
# mostly for round-trip cleanliness (the dialog, QSettings, and
# shipped-file paths all walk three fields).
DEFAULT_AIRPLANE_TABLE_FONT_PX = 24
DEFAULT_AIRPLANE_ROUTE_TEXT_FONT_PX = 20
DEFAULT_AIRPLANE_HINT_FONT_PX = 18

FONT_SIZE_MIN_PX = 8
FONT_SIZE_MAX_PX = 48

# --- Traffic icon size --------------------------------------------------
#
# The on-chart silhouette for VATSIM traffic (introduced in v2 — see
# ``ROADMAP-NEXT.md``) is drawn at runtime via ``QPainterPath`` rather
# than a bundled raster, so its size is a single integer in screen
# pixels measured **nose-to-tail** along the silhouette's long axis.
# Wake-category scaling (L=0.85x, M=1.0x, H=1.2x, J=1.4x) is applied
# on top of this base value at render time, so a setting of 36 px
# means a Cessna shows at ~31 px and a 747 at ~43 px.
#
# We keep this as a single global value rather than splitting it
# per-profile (normal vs airplane) like the font sizes for a
# pragmatic reason: airplane mode hides the chart entirely, so
# there's no airplane-mode reading distance to tune the icons for.
# Splitting later is cheap if a use case appears.
#
# Default of 36 px (1.5x the original 24 px after first-flight visual
# review): the smaller default crowded the silhouette and made the
# callsign labels — which Qt sizes proportionally to the icon — too
# small to read against busy chart regions. 36 px gives roughly a
# 16-pt bold callsign that stays legible at typical chart-zoom on a
# 1920x1080 monitor, while still being comfortably below the 96 px
# upper bound for users who want even bigger icons. Ten planes in
# the LLBG vicinity at 36 px still fit without blanketing the chart.
DEFAULT_TRAFFIC_ICON_SIZE_PX = 36

# Min/max enforced at edit time by the spinbox in the dialog and by
# the Ctrl+wheel-on-plane resizer (see ``font_wheel_resize`` for the
# pattern this will follow). The ``font_wheel_resize`` analogue
# leaves QSettings unclamped so a hand-edited value is honoured;
# we mirror that contract here.
TRAFFIC_ICON_SIZE_MIN_PX = 8
TRAFFIC_ICON_SIZE_MAX_PX = 96

# Default screen-pixel side length for the waypoint-marker
# triangles (VRPs). 24 px is the post-launch default — the
# original 16 px tested poorly against satellite imagery (busy
# backgrounds dwarfed the marker), so the default was bumped
# 50 % after user feedback. The same value is exported by
# :data:`cvfr_routemaster.waypoint_marker_overlay.DEFAULT_TRIANGLE_SIDE_PX`;
# keeping them in sync is enforced by a unit test rather than
# importing one from the other (settings_store deliberately
# avoids importing from UI modules so a settings reset can run
# without dragging Qt into the import graph).
DEFAULT_WAYPOINT_MARKER_SIZE_PX = 24

# Min/max bracket for the Display Settings spinbox + any future
# Ctrl+wheel-on-marker resizer. The same "QSettings honours
# hand-edited out-of-range values, only the editor clamps" rule
# we use for fonts and traffic icons applies — see
# :func:`load_waypoint_marker_size_px`.
WAYPOINT_MARKER_SIZE_MIN_PX = 8
WAYPOINT_MARKER_SIZE_MAX_PX = 96

# Release-side fallback file (same shape as the live values, written by
# ``scripts/_restamp_cache_fingerprints.write_shipped_font_settings``).
# Mirrors the ``map_layout.json`` mechanism so a friend (or the VATSIM
# laptop on first launch) inherits the dev's calibrated *visual*
# preferences end-to-end — calibration uses ``map_layout.json``, fonts
# use this file.
SHIPPED_FONT_SETTINGS_FILE = "font_settings.json"


@dataclass(frozen=True)
class FontSizes:
    """Per-area font-size preferences, in CSS pixels.

    ``table_px`` covers both the waypoint and route tables — the user
    asked for them to track together because they're the two
    data-grid surfaces in the app and visually the user reads them
    side-by-side.

    ``route_text_px`` covers the three labels that sit above the
    route table inside ``RoutePanel``: the ICAO Field 15 string
    (``_route_string_label``), the Hebrew paperwork string
    (``_hebrew_string_label``), and the totals summary
    (``_totals_label``). All three are tagged with
    ``objectName="routeText"`` so a single QSS selector hits them.

    ``hint_px`` covers the three usage-hint labels (waypoint-table
    hint, map hint, route-panel hint). All three were already
    tagged with ``objectName="mapHint"`` for the existing
    bright-white styling; the font-size is now user-controlled
    instead of the previous hard-coded 18 px.
    """

    table_px: int
    route_text_px: int
    hint_px: int


def default_font_sizes() -> FontSizes:
    """Normal-mode (non-airplane) sizes the app uses when no per-user
    override is saved.

    Function (not a frozen module-level constant) so callers can't
    accidentally mutate the dataclass; also keeps the indirection
    open for a future "different defaults on hi-dpi" decision
    without forcing a global rename.
    """
    return FontSizes(
        table_px=DEFAULT_TABLE_FONT_PX,
        route_text_px=DEFAULT_ROUTE_TEXT_FONT_PX,
        hint_px=DEFAULT_HINT_FONT_PX,
    )


def default_airplane_font_sizes() -> FontSizes:
    """Airplane-mode sizes — the second profile applied while
    airplane mode is active. See the ``DEFAULT_AIRPLANE_*_FONT_PX``
    constants above for the reasoning behind the larger defaults
    (right-seat / secondary-monitor reading distance).
    """
    return FontSizes(
        table_px=DEFAULT_AIRPLANE_TABLE_FONT_PX,
        route_text_px=DEFAULT_AIRPLANE_ROUTE_TEXT_FONT_PX,
        hint_px=DEFAULT_AIRPLANE_HINT_FONT_PX,
    )

# Filename of the release-shipped default sheet layout. Lives inside
# ``<project_root>/.cvfr_routemaster/`` alongside the four cache files
# the release tree ships. Generated by the build script (see
# ``scripts/_restamp_cache_fingerprints.write_shipped_map_layout``)
# from ``geo_calibration.json``'s ``map_layout`` blocks. Consumed
# by :func:`load_map_layout` as a fallback when QSettings is empty
# (i.e. first-launch on a fresh machine).
SHIPPED_MAP_LAYOUT_FILE = "map_layout.json"


#: Filename for the project-root INI that v3.3+ uses to persist
#: all user preferences. Sits next to the EXE in a frozen build
#: and at the repo root when running ``py -m cvfr_routemaster``.
#: Same layout on every platform — no registry on Windows, no
#: ``~/.config/`` on Linux, no plist on macOS. Wipe-the-folder
#: equals wipe-the-state, and a user copying the install folder
#: between machines (or backing it up) carries their preferences
#: along automatically.
SETTINGS_INI_FILENAME = "settings.ini"


def _settings_root() -> Path:
    """Resolve the directory the INI file should live in.

    Mirrors the frozen-vs-dev switch from
    :func:`cvfr_routemaster.__main__._project_root` (duplicated
    here rather than imported because :mod:`__main__` is only
    safely importable as the program's entry point, and several
    callers — tests, build scripts, the ``run_app`` re-entry —
    pull :mod:`settings_store` at module-import time).

    * Frozen / PyInstaller --onefile: ``Path(sys.executable).parent``.
      This is the release/ folder next to the EXE, the same
      directory that holds the PDFs and ``.cvfr_routemaster/``.
    * Dev / source checkout: the repo root (two levels above this
      file). Tests that need an isolated INI monkeypatch
      :func:`_settings` directly; they don't need to override the
      root.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _settings_ini_path() -> Path:
    """Absolute path of the INI file backing :func:`_settings`."""
    return _settings_root() / SETTINGS_INI_FILENAME


def _legacy_native_settings() -> QSettings:
    """The OS-native QSettings location we used before v3.3+.

    On Windows that resolved to the registry under
    ``HKCU\\Software\\CVFRRouteMaster\\<APP>``; on Linux to
    ``~/.config/CVFRRouteMaster/<APP>.conf``; on macOS to a
    plist in ``~/Library/Preferences/``. The new INI strategy
    has replaced all three — this constructor exists only so
    :func:`_migrate_legacy_native_settings_if_needed` can read
    pre-v3.3 preferences out of the old location and then wipe
    it.
    """
    return QSettings(ORG, APP)


def _migrate_legacy_native_settings_if_needed(ini_path: Path) -> None:
    """One-shot migration from the OS-native QSettings backend
    (registry on Windows, ``~/.config/...`` on Linux, plist on
    macOS) into the v3.3+ project-root INI file.

    Triggered automatically the first time :func:`_settings` is
    called when the INI file doesn't exist yet. Idempotent:
    once the INI exists on disk (after the first successful
    call to ``setValue`` + ``sync``) the migration short-circuits
    and the legacy backend is never read again.

    Behaviour:

    1. If the INI already exists → no-op (already migrated, or
       v3.3+ first-launch on a truly fresh machine that wrote a
       value before this call). Avoids re-reading the (now
       empty) legacy backend on every launch.
    2. Otherwise read every key from the legacy native location.
       If it's empty too → no-op (fresh install, nothing to
       migrate). The INI will be created lazily by the next
       ``setValue`` call.
    3. Otherwise copy each (key, value) pair into the new INI,
       flush, then call ``clear()`` + ``sync()`` on the legacy
       backend to wipe the registry/plist/config-file entries
       this app created. We deliberately do NOT delete the
       parent group container — Qt's ``clear()`` only removes
       keys we created, and the empty parent that remains is
       Qt's responsibility to GC (it usually does on next
       ``sync``).

    Why migrate AND clear rather than just migrate:

    * Wipes the historical pollution of the user's registry /
      preference store. That's exactly the property the user
      asked for ("displeased we had them in the first place").
    * Prevents drift: if a future patch ever reads the legacy
      backend by mistake, it'll find nothing — there's a single
      source of truth from this point on.
    """
    if ini_path.exists():
        return
    legacy = _legacy_native_settings()
    keys = legacy.allKeys()
    if not keys:
        return
    ini = QSettings(str(ini_path), QSettings.Format.IniFormat)
    for key in keys:
        ini.setValue(key, legacy.value(key))
    ini.sync()
    legacy.clear()
    legacy.sync()


def _settings() -> QSettings:
    """The v3.3+ project-root INI-backed QSettings handle.

    Every call constructs a fresh ``QSettings`` instance pointed
    at the same on-disk INI file. The legacy native backend is
    migrated on the first call after an upgrade and never
    consulted again.

    Tests that need an isolated settings store monkeypatch this
    function directly (see ``isolated_settings`` fixtures in
    ``tests/``), which bypasses the migration helper entirely —
    the helper only matters in production where the INI sits at
    a fixed project-root location.
    """
    ini_path = _settings_ini_path()
    _migrate_legacy_native_settings_if_needed(ini_path)
    return QSettings(str(ini_path), QSettings.Format.IniFormat)


# Subdirectories under ``project_root`` that auto-discovery walks
# looking for the three CVFR chart PDFs, in priority order.
#
# - ``("map-pdfs",)`` is the layout ``scripts/build_release.py`` ships
#   inside the friend-facing release zip (PDFs in their own subfolder
#   so the release root stays uncluttered).
# - ``()`` is the dev layout — PDFs sit directly in the repo root,
#   alongside ``cvfr_routemaster/`` and ``scripts/``, which is how
#   ``py -m cvfr_routemaster`` has always discovered them.
#
# We accept both rather than forcing dev to mirror the release layout
# so existing checkouts keep working without anyone having to move
# 16 MiB of PDFs into a new folder. The release layout wins when the
# PDF exists in both places, which only matters for someone who
# unzipped a release tree on top of a dev tree (uncommon, but the
# release layout is what we want to honour in that case).
_PDF_AUTODISCOVERY_SUBDIRS: tuple[tuple[str, ...], ...] = (
    ("map-pdfs",),
    (),
)

_PDF_AUTODISCOVERY_NAMES: tuple[tuple[str, str], ...] = (
    ("CVFR-NORTH-OCT-2025-UPD2.pdf", "north"),
    ("CVFR-SOUTH-OCT-2025-UPD2.pdf", "south"),
    ("CVFR-BACK-PAGES-OCT-2025-UPD2.pdf", "back"),
)


def _autodiscover_pdf(project_root: Path, filename: str) -> str:
    """First location under ``project_root`` (in priority order) that
    holds a non-empty ``filename``, or the empty string if none did.

    Empty (0-byte) candidates are skipped because they're never a
    valid CVFR chart — a real chart PDF is megabytes — and a
    leftover 0-byte file (test fixture, interrupted download, etc.)
    would otherwise short-circuit autodiscovery and bubble up to
    the loader as a "Cannot open empty file" error from PyMuPDF.
    """
    for subdir in _PDF_AUTODISCOVERY_SUBDIRS:
        candidate = project_root.joinpath(*subdir, filename)
        if candidate.is_file() and candidate.stat().st_size > 0:
            return str(candidate)
    return ""


def _looks_like_url(value: str) -> bool:
    """True iff ``value`` looks like an ``http(s)://`` URL.

    Mirrors :meth:`cvfr_routemaster.chart_source.ChartSource.is_url`
    but kept inline here to avoid a circular import (chart_source
    only needs to import APP_NAME / __version__ from the package,
    not from settings_store). The check is intentionally
    conservative — scheme prefix only — so a half-typed URL during
    settings editing isn't misclassified.
    """
    lowered = value.lower().lstrip()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _qsetting_path_is_usable(value: str) -> bool:
    """A QSettings PDF source counts as "usable" only if either:

    1. The value is a URL (``http(s)://...``). URL sources are
       intentional and we should not autodiscover-around them
       just because the cached file doesn't exist yet on this
       machine; the chart-fetch layer at runtime is responsible
       for downloading on first use.
    2. The value is a local path, the file exists, AND it has
       non-zero size.

    Why the size check? Two real-world ways the path can be set but
    point at garbage:

    1. **Test pollution** (the cause of the immediate bug this guard
       fixes). A regression test wrote ``pdf_north = <pytest tmp
       path to a 0-byte fixture>`` to the user's real registry-backed
       QSettings during a session where test isolation was broken;
       the tmp file persisted long enough for the next .exe launch
       to load the path, then PyMuPDF crashed with "Cannot open
       empty file".

    2. **Truncated download / disk-full mid-copy.** A friend
       replaces a chart with a newer-cycle PDF, the download / copy
       gets interrupted, the file lands at 0 bytes. Without this
       guard the next launch surfaces the same PyMuPDF error
       instead of falling back to autodiscovery.

    For a truly-empty value (``""``), autodiscovery should still
    fire so a fresh install picks up bundled defaults.
    """
    if not value:
        return False
    if _looks_like_url(value):
        return True
    p = Path(value)
    if not p.is_file():
        return False
    try:
        return p.stat().st_size > 0
    except OSError:
        return False


# ---------------------------------------------------------------------------
# chart_sources.json — first-run URL defaults
# ---------------------------------------------------------------------------
#
# Lives at ``<project_root>/.cvfr_routemaster/chart_sources.json``.
# Shipped by the build script (analogous to
# ``font_settings.json`` and ``map_layout.json``) so a fresh-install
# recipient with empty QSettings sees the CAAI URLs as defaults
# the very first time they open Map File Settings.
#
# Schema:
#
#   {
#     "north": "https://www.gov.il/...",
#     "south": "https://www.gov.il/...",
#     "back":  "https://www.gov.il/..."
#   }
#
# Keys not in ``("north", "south", "back")`` are ignored; missing
# keys fall through to the empty string (and on to filesystem
# autodiscovery, for the unlikely zero-network friend who happens
# to have local PDFs).
#
# We keep this separate from QSettings because:
#
# * QSettings is per-user state. Saving a URL there means "this
#   user explicitly chose this URL" — surviving across program
#   updates, which is what we want for user-overridden URLs.
# * ``chart_sources.json`` is per-release state — the URL set the
#   build was built around. When a new release ships with a
#   different default URL, that's seen by users who never
#   customised, AND by users who did customise the URL field is
#   still preserved (their QSettings wins).

_CHART_SOURCES_FILENAME: str = "chart_sources.json"


def chart_sources_json_path(project_root: Path) -> Path:
    """Path to the shipped chart-sources defaults JSON."""
    return project_root / ".cvfr_routemaster" / _CHART_SOURCES_FILENAME


def load_shipped_chart_sources(project_root: Path) -> dict[str, str]:
    """Return ``{sheet: source_string}`` from the shipped defaults
    JSON, or ``{}`` if no file exists / is malformed.

    "Source string" here is whatever the build cooked in — for
    v3.3+ releases that's the three CAAI URLs. Earlier releases
    might not ship this file at all, in which case the empty
    dict signals to ``load_pdf_paths`` to fall through to the
    legacy filesystem autodiscovery.

    Only keys in ``("north", "south", "back")`` are returned; the
    string-valued requirement filters out an accidental ``null``
    or non-string value that a hand-edit might introduce.
    """
    path = chart_sources_json_path(project_root)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("north", "south", "back"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    return out


def save_shipped_chart_sources(
    project_root: Path,
    sources: dict[str, str],
) -> None:
    """Write the chart-sources defaults JSON.

    Called by the build script during release packaging
    (analogous to ``write_shipped_map_layout`` /
    ``write_shipped_font_settings``). Atomic via ``.tmp``-then-
    rename so a crashed build can't leave the file half-written.

    Keys not in ``("north", "south", "back")`` are dropped.
    """
    target_dir = project_root / ".cvfr_routemaster"
    target_dir.mkdir(parents=True, exist_ok=True)
    clean = {
        key: sources[key]
        for key in ("north", "south", "back")
        if key in sources
        and isinstance(sources[key], str)
        and sources[key]
    }
    target = chart_sources_json_path(project_root)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(clean, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)


def load_pdf_paths(project_root: Path | None = None) -> tuple[str, str, str]:
    """Resolve the three chart-source strings (path or URL each)
    with this precedence:

    1. **Explicit QSettings value.** If the user has saved a path
       or URL via Map File Settings, that wins. A previously-saved
       local path that has since disappeared on disk falls through
       (treated as "not usable") so the user doesn't get stuck on
       a stale path after re-installing.
    2. **Shipped defaults** in ``chart_sources.json``. v3.3+
       releases ship the CAAI URLs here. A user with empty
       QSettings on first launch sees the URLs and gets the
       download-on-first-use flow.
    3. **Legacy filesystem autodiscovery** in ``map-pdfs/``. Kept
       for back-compat with v3.2 release bundles that distributed
       the PDFs alongside the binary. New releases never trigger
       this path because the PDFs aren't shipped any more.

    Returns ``(north, south, back)`` as a 3-tuple. Any of the
    three may be ``""`` if none of the layers found a value —
    the caller is expected to treat that as "user hasn't set
    this yet" and prompt via Map File Settings.
    """
    s = _settings()
    north = s.value("pdf_north", "", str)
    south = s.value("pdf_south", "", str)
    back = s.value("pdf_back", "", str)

    if project_root:
        shipped = load_shipped_chart_sources(project_root)
        # Layer 2 — chart_sources.json defaults.
        if not _qsetting_path_is_usable(north):
            north = shipped.get("north", north)
        if not _qsetting_path_is_usable(south):
            south = shipped.get("south", south)
        if not _qsetting_path_is_usable(back):
            back = shipped.get("back", back)
        # Layer 3 — legacy filesystem autodiscovery, only if the
        # shipped-defaults layer ALSO didn't supply a value.
        if not _qsetting_path_is_usable(north):
            north = _autodiscover_pdf(project_root, "CVFR-NORTH-OCT-2025-UPD2.pdf") or north
        if not _qsetting_path_is_usable(south):
            south = _autodiscover_pdf(project_root, "CVFR-SOUTH-OCT-2025-UPD2.pdf") or south
        if not _qsetting_path_is_usable(back):
            back = _autodiscover_pdf(project_root, "CVFR-BACK-PAGES-OCT-2025-UPD2.pdf") or back
    return north, south, back


def save_pdf_paths(north: str, south: str, back: str) -> None:
    """Persist the three chart-source strings (path or URL each).

    No validation — the Settings dialog has already done that.
    URLs and local paths go through the same QSettings keys
    (``pdf_north`` / ``pdf_south`` / ``pdf_back``) since they
    semantically represent the same thing: "where do I get this
    sheet from".
    """
    s = _settings()
    s.setValue("pdf_north", north)
    s.setValue("pdf_south", south)
    s.setValue("pdf_back", back)


def load_autoload_enabled() -> bool:
    return bool(_settings().value("autoload_on_start", True, bool))


def save_autoload_enabled(enabled: bool) -> None:
    _settings().setValue("autoload_on_start", bool(enabled))


def load_map_layout(project_root: Path | None = None) -> dict[str, Any] | None:
    """Return saved north/south sheet positions/scales, or None to
    fall back to hard-coded vertical-stack defaults.

    Lookup order:

      1. **QSettings** (per-user, per-machine: Windows registry on
         Windows, ``~/.config/CVFRRouteMaster/`` on Linux). The
         user's own saved layout from previous app sessions. Highest
         priority because it represents the user's intent on *this*
         machine — once they've moved a sheet here, that
         customisation stays.

      2. **``<project_root>/.cvfr_routemaster/map_layout.json``** —
         the release-shipped first-launch default. Generated by the
         build script (see
         ``scripts/_restamp_cache_fingerprints.write_shipped_map_layout``)
         from ``geo_calibration.json``'s ``map_layout`` blocks, so
         when a friend (or the VATSIM laptop on first launch) loads
         a pre-calibrated release with an empty QSettings, the
         sheets land at exactly the layout the calibration was
         captured against. Without this rung the release's shipped
         ``geo_calibration.json`` would be rejected on first launch:
         ``map_layout_matches`` would compare the calibration's
         recorded ``map_layout`` (the dev's custom drag position)
         against the auto-placed default sheet position and reject
         them as different, triggering the modal "please
         re-calibrate" prompt — a several-minute manual ritual on a
         release that's *supposed* to ship a fully-warm cache.

      3. **``None``** — caller falls back to the hard-coded default
         vertical-stack placement (north at (0, 0), south at
         (0, n_pixmap_h)). Only happens when QSettings is empty
         *and* no ``map_layout.json`` is present — e.g. a dev
         checkout that's never been calibrated, or a release that
         was built before this mechanism existed.

    The QSettings → shipped-file fallback ordering is deliberate:
    once the user manually moves a sheet (which calls
    :func:`save_map_layout`, persisting to QSettings), QSettings is
    populated and the shipped-file rung is no longer consulted on
    that machine. So the shipped layout is genuinely a *first-launch*
    default, not an override the user has to keep dismissing.

    Args:
        project_root: The app's project directory (the one that
            contains ``.cvfr_routemaster/``). Required to find the
            shipped fallback. Optional for backwards-compatibility
            with old callers and tests; passing ``None`` skips the
            file fallback and the function behaves exactly like the
            pre-fallback version.
    """
    s = _settings()
    if s.value("map_layout_saved", False, bool):
        return {
            "north_x": float(s.value("map_north_x", 0.0)),
            "north_y": float(s.value("map_north_y", 0.0)),
            "north_scale": float(s.value("map_north_scale", 1.0)),
            "south_x": float(s.value("map_south_x", 0.0)),
            "south_y": float(s.value("map_south_y", 0.0)),
            "south_scale": float(s.value("map_south_scale", 1.0)),
            "selected": str(s.value("map_selected_sheet", "south")),
        }
    if project_root is not None:
        return _load_shipped_map_layout(project_root)
    return None


def _load_shipped_map_layout(project_root: Path) -> dict[str, Any] | None:
    """Read ``<project_root>/.cvfr_routemaster/map_layout.json`` and
    return it shaped like a QSettings-loaded layout, or ``None`` on
    any failure mode.

    Failure modes that map to ``None`` (so the caller cleanly falls
    through to hard-coded defaults rather than crashing on a
    half-broken shipped file):

      * File absent — dev checkout never calibrated, or older
        release that shipped before this mechanism.
      * I/O error reading the file.
      * Top-level JSON parse failure (corrupted file, partial write).
      * Top-level value isn't a dict.
      * Any required field missing or non-numeric — we don't try to
        "fix up" a half-populated file; better to fall through to
        defaults than to silently mix shipped + default values.

    The ``selected`` field is treated as best-effort and defaults to
    ``"south"`` (matches the in-app default) if absent or non-string,
    because the selected-sheet annotation is a UX preference, not a
    correctness invariant — calibration validity doesn't depend on it.
    """
    path = project_root / ".cvfr_routemaster" / SHIPPED_MAP_LAYOUT_FILE
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return {
            "north_x": float(raw["north_x"]),
            "north_y": float(raw["north_y"]),
            "north_scale": float(raw["north_scale"]),
            "south_x": float(raw["south_x"]),
            "south_y": float(raw["south_y"]),
            "south_scale": float(raw["south_scale"]),
            "selected": (
                str(raw["selected"])
                if isinstance(raw.get("selected"), str)
                else "south"
            ),
        }
    except (KeyError, TypeError, ValueError):
        return None


def save_map_layout(
    *,
    north_x: float,
    north_y: float,
    north_scale: float,
    south_x: float,
    south_y: float,
    south_scale: float,
    selected: str,
) -> None:
    s = _settings()
    s.setValue("map_layout_saved", True)
    s.setValue("map_north_x", north_x)
    s.setValue("map_north_y", north_y)
    s.setValue("map_north_scale", north_scale)
    s.setValue("map_south_x", south_x)
    s.setValue("map_south_y", south_y)
    s.setValue("map_south_scale", south_scale)
    s.setValue("map_selected_sheet", selected)


def save_map_view_navigation(
    *,
    m11: float,
    m12: float,
    m13: float,
    m21: float,
    m22: float,
    m23: float,
    m31: float,
    m32: float,
    m33: float,
    scroll_h: int,
    scroll_v: int,
) -> None:
    """Persist zoom/pan of the map QGraphicsView between sessions."""
    s = _settings()
    s.setValue("map_view_saved", True)
    s.setValue("map_view_m11", m11)
    s.setValue("map_view_m12", m12)
    s.setValue("map_view_m13", m13)
    s.setValue("map_view_m21", m21)
    s.setValue("map_view_m22", m22)
    s.setValue("map_view_m23", m23)
    s.setValue("map_view_m31", m31)
    s.setValue("map_view_m32", m32)
    s.setValue("map_view_m33", m33)
    s.setValue("map_view_scroll_h", int(scroll_h))
    s.setValue("map_view_scroll_v", int(scroll_v))


def load_map_view_navigation() -> dict[str, float | int] | None:
    s = _settings()
    if not s.value("map_view_saved", False, bool):
        return None
    try:
        return {
            "m11": float(s.value("map_view_m11", 1.0)),
            "m12": float(s.value("map_view_m12", 0.0)),
            "m13": float(s.value("map_view_m13", 0.0)),
            "m21": float(s.value("map_view_m21", 0.0)),
            "m22": float(s.value("map_view_m22", 1.0)),
            "m23": float(s.value("map_view_m23", 0.0)),
            "m31": float(s.value("map_view_m31", 0.0)),
            "m32": float(s.value("map_view_m32", 0.0)),
            "m33": float(s.value("map_view_m33", 1.0)),
            "scroll_h": int(s.value("map_view_scroll_h", 0)),
            "scroll_v": int(s.value("map_view_scroll_v", 0)),
        }
    except (TypeError, ValueError):
        return None


def load_map_link_provider() -> str:
    """Which external map site to use when opening a waypoint Code link (bing/google/apple)."""
    return normalize_map_link_provider(str(_settings().value("map_link_provider", "bing", str)))


def save_map_link_provider(provider: str) -> None:
    _settings().setValue("map_link_provider", normalize_map_link_provider(provider))


def save_window_layout(
    *,
    geometry: bytes | bytearray,
    splitter_state: bytes | bytearray,
) -> None:
    """Persist the main window's geometry + the central splitter's pane
    sizes so the next session opens with the same layout the user closed
    with.

    Both payloads are opaque Qt-serialised blobs:
    - ``geometry`` comes from ``QMainWindow.saveGeometry()`` and encodes
      window position, size, screen identity, and maximized/fullscreen
      state in one binary record. We store it as raw bytes via
      ``QSettings.setValue`` (which round-trips ``bytes`` through
      ``QByteArray`` natively).
    - ``splitter_state`` comes from ``QSplitter.saveState()`` and
      encodes per-pane sizes plus collapsed/expanded flags.

    We deliberately don't try to interpret the bytes here — Qt already
    handles forward/backward compatibility within a major version. A
    future Qt API break would simply make ``restoreGeometry`` return
    ``False`` and the loader fall back to defaults; no parsing on our
    side means no parser to keep in sync.
    """
    s = _settings()
    s.setValue("window_layout_saved", True)
    s.setValue("window_geometry", bytes(geometry))
    s.setValue("window_splitter_state", bytes(splitter_state))


def load_window_layout() -> tuple[bytes, bytes] | None:
    """Return ``(geometry_bytes, splitter_state_bytes)`` if a layout was
    previously saved, otherwise ``None``.

    A missing saved-flag returns ``None`` so the first launch falls back
    to the hard-coded default size and stretch ratios. Either payload
    that round-trips as ``None`` from QSettings is treated as missing
    so a partially-corrupted entry doesn't crash startup — Qt's restore
    APIs would just return ``False`` on that input anyway, but bailing
    out early keeps the logic in the caller short.
    """
    s = _settings()
    if not s.value("window_layout_saved", False, bool):
        return None
    geom = s.value("window_geometry")
    split = s.value("window_splitter_state")
    if geom is None or split is None:
        return None
    try:
        return bytes(geom), bytes(split)
    except (TypeError, ValueError):
        return None


def load_font_sizes(project_root: Path | None = None) -> FontSizes:
    """Return the per-area font sizes, walking a three-rung
    priority ladder:

      1. **QSettings** (per-machine, per-user — Windows registry on
         Windows, ``~/.config/CVFRRouteMaster/`` on Linux). The
         user's own font-size choices for *this* machine. Once
         they've opened the Font Settings dialog and clicked OK
         even once, every individual field is present in QSettings
         and that machine's preference wins for the rest of time.

      2. **``<project_root>/.cvfr_routemaster/font_settings.json``** —
         the release-shipped first-launch default. Written by
         ``scripts/_restamp_cache_fingerprints.write_shipped_font_settings``
         from the *dev's* current QSettings at build-time, so a
         friend (or the VATSIM laptop on first launch) inherits
         the same visual configuration the dev was using when
         they cut the release. Mirrors the ``map_layout.json``
         mechanism for calibration — both files exist to thread
         dev-side QSettings preferences across to a machine where
         QSettings is empty.

      3. **Hard-coded defaults** (``DEFAULT_*_FONT_PX``). Last
         resort, only hit on a dev checkout that's never been
         calibrated *and* a release that pre-dates this
         mechanism. Preserves the original behaviour from before
         font shipping existed.

    Each field is read independently at every rung, so a partial
    save (or an older QSettings layout that only knows about a
    subset of the fields) cleanly fills-in-defaults rather than
    returning all-defaults the moment one field is missing. That
    makes a forwards-compat schema bump (e.g. adding a fourth
    font knob) painless: existing users keep the three values
    they had and pick up the new default for the fourth.

    No range clamping: the dialog enforces ``FONT_SIZE_MIN_PX`` /
    ``FONT_SIZE_MAX_PX`` at edit time, but a user who hand-edits
    QSettings to a stranger value gets that value back unchanged
    so the round-trip is honest.

    Args:
        project_root: The app's project directory (the one that
            contains ``.cvfr_routemaster/``). Required to find the
            shipped fallback file. Optional for backwards-compat
            with old callers and tests; passing ``None`` skips the
            shipped-file rung and the function behaves like the
            pre-shipping version.
    """
    shipped = (
        _load_shipped_font_sizes(project_root)
        if project_root is not None
        else None
    )
    s = _settings()
    table_default = shipped.table_px if shipped is not None else DEFAULT_TABLE_FONT_PX
    route_default = (
        shipped.route_text_px if shipped is not None else DEFAULT_ROUTE_TEXT_FONT_PX
    )
    hint_default = shipped.hint_px if shipped is not None else DEFAULT_HINT_FONT_PX
    return FontSizes(
        table_px=int(s.value("font_table_px", table_default, int)),
        route_text_px=int(s.value("font_route_text_px", route_default, int)),
        hint_px=int(s.value("font_hint_px", hint_default, int)),
    )


def _load_shipped_font_sizes(project_root: Path) -> FontSizes | None:
    """Read the *normal-mode* font sizes from
    ``<project_root>/.cvfr_routemaster/font_settings.json`` and
    return them as a :class:`FontSizes`, or ``None`` on any failure
    mode.

    Failure modes that map to ``None`` (so the caller cleanly falls
    through to hard-coded defaults rather than crashing on a
    half-broken shipped file):

      * File absent — dev checkout that's never run a release
        build, or older release that shipped before this mechanism.
      * I/O error reading the file.
      * Top-level JSON parse failure (corrupted file, partial write).
      * Top-level value isn't a dict.
      * Any required field missing or non-numeric — we don't try to
        "fix up" a half-populated file; better to fall through to
        defaults than to silently mix shipped + default values.
    """
    raw = _read_shipped_font_settings_blob(project_root)
    if raw is None:
        return None
    try:
        return FontSizes(
            table_px=int(raw["table_px"]),
            route_text_px=int(raw["route_text_px"]),
            hint_px=int(raw["hint_px"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _load_shipped_airplane_font_sizes(project_root: Path) -> FontSizes | None:
    """Read the *airplane-mode* font sizes from
    ``font_settings.json`` if the dev's build wrote them.

    The same JSON file holds both profiles, with airplane sizes
    living under the ``airplane_*`` flat keys (mirroring the
    ``airplane_font_*`` prefix used in QSettings). Older releases
    that pre-date the airplane-profile feature have only the three
    normal-mode keys and return ``None`` here — callers then fall
    through to :func:`default_airplane_font_sizes`. This keeps the
    upgrade path painless: a dev who built before the feature can
    ship a release that ignores airplane mode entirely, and a fresh
    install picks up the new defaults.
    """
    raw = _read_shipped_font_settings_blob(project_root)
    if raw is None:
        return None
    try:
        return FontSizes(
            table_px=int(raw["airplane_table_px"]),
            route_text_px=int(raw["airplane_route_text_px"]),
            hint_px=int(raw["airplane_hint_px"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _read_shipped_font_settings_blob(project_root: Path) -> dict | None:
    """Read and parse ``font_settings.json`` once. Returns the raw
    dict or ``None`` on any I/O / parse failure.

    Factored out so both profile loaders share one source of truth
    for the file location, parse rules, and "shape isn't a dict"
    fast-out — otherwise the airplane and normal loaders would
    diverge on edge cases (e.g. a corrupted file would have one
    profile return None and the other crash, which would
    asymmetrically mix shipped + default values).
    """
    path = project_root / ".cvfr_routemaster" / SHIPPED_FONT_SETTINGS_FILE
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def load_airplane_font_sizes(project_root: Path | None = None) -> FontSizes:
    """Return the airplane-mode font sizes, with the same three-rung
    priority ladder :func:`load_font_sizes` uses — QSettings first,
    then the shipped ``font_settings.json``, then the hard-coded
    :func:`default_airplane_font_sizes`.

    Airplane keys in QSettings use the ``airplane_font_*`` prefix to
    keep them lexicographically grouped and distinct from the
    normal-mode ``font_*`` keys; both sets coexist independently so
    leaving / re-entering airplane mode doesn't disturb the
    user's normal-mode preferences.
    """
    shipped = (
        _load_shipped_airplane_font_sizes(project_root)
        if project_root is not None
        else None
    )
    s = _settings()
    table_default = (
        shipped.table_px if shipped is not None else DEFAULT_AIRPLANE_TABLE_FONT_PX
    )
    route_default = (
        shipped.route_text_px
        if shipped is not None
        else DEFAULT_AIRPLANE_ROUTE_TEXT_FONT_PX
    )
    hint_default = (
        shipped.hint_px if shipped is not None else DEFAULT_AIRPLANE_HINT_FONT_PX
    )
    return FontSizes(
        table_px=int(s.value("airplane_font_table_px", table_default, int)),
        route_text_px=int(
            s.value("airplane_font_route_text_px", route_default, int)
        ),
        hint_px=int(s.value("airplane_font_hint_px", hint_default, int)),
    )


def save_font_sizes(sizes: FontSizes) -> None:
    """Persist the three per-area *normal-mode* font sizes.

    Takes the whole :class:`FontSizes` dataclass rather than three
    positional ints so the call site mirrors the dataclass it just
    built — refactoring (e.g. splitting ``table_px`` into separate
    waypoint/route knobs) only mutates the dataclass, not every
    save call.
    """
    s = _settings()
    s.setValue("font_table_px", int(sizes.table_px))
    s.setValue("font_route_text_px", int(sizes.route_text_px))
    s.setValue("font_hint_px", int(sizes.hint_px))


def save_airplane_font_sizes(sizes: FontSizes) -> None:
    """Persist the three per-area *airplane-mode* font sizes. Stored
    under ``airplane_font_*`` keys so this profile is independent
    of the normal-mode :func:`save_font_sizes` writes.
    """
    s = _settings()
    s.setValue("airplane_font_table_px", int(sizes.table_px))
    s.setValue("airplane_font_route_text_px", int(sizes.route_text_px))
    s.setValue("airplane_font_hint_px", int(sizes.hint_px))


def load_traffic_icon_size_px(project_root: Path | None = None) -> int:
    """Return the on-chart VATSIM-traffic plane-icon base size in pixels.

    Three-rung priority ladder, identical in shape to
    :func:`load_font_sizes`:

      1. **QSettings** (per-machine, per-user). Once the user has
         interacted with the Display Settings dialog (or Ctrl+scrolled
         on a plane once) this rung is populated and wins.

      2. **``<project_root>/.cvfr_routemaster/font_settings.json``** —
         the release-shipped first-launch default. The file is
         shared with the font-size shipping mechanism (one JSON
         covers the dev's whole "display preferences" snapshot)
         under the flat key ``traffic_icon_size_px``. Older shipped
         files that pre-date this feature simply lack the key and
         fall through to the hard-coded default — no migration
         needed.

      3. **``DEFAULT_TRAFFIC_ICON_SIZE_PX``**. Last resort.

    No range clamping at the loader: the dialog enforces
    ``TRAFFIC_ICON_SIZE_MIN_PX`` / ``TRAFFIC_ICON_SIZE_MAX_PX`` at
    edit time, and a user who hand-edits QSettings to a stranger
    value gets that value back unchanged so the round-trip is
    honest. Same contract as :func:`load_font_sizes`.

    Args:
        project_root: The app's project directory (the one that
            contains ``.cvfr_routemaster/``). Required to find the
            shipped fallback file. Optional for backwards-compat
            with old callers and tests; passing ``None`` skips the
            shipped-file rung.
    """
    shipped = (
        _load_shipped_traffic_icon_size_px(project_root)
        if project_root is not None
        else None
    )
    s = _settings()
    default = shipped if shipped is not None else DEFAULT_TRAFFIC_ICON_SIZE_PX
    return int(s.value("traffic_icon_size_px", default, int))


def _load_shipped_traffic_icon_size_px(project_root: Path) -> int | None:
    """Read the traffic-icon size from the shared ``font_settings.json``
    if the dev's build wrote it.

    Returns ``None`` if the file is absent, malformed, or simply
    pre-dates this feature (i.e. the JSON has the font-size keys
    but no ``traffic_icon_size_px``). Callers then fall through to
    :data:`DEFAULT_TRAFFIC_ICON_SIZE_PX`. Mirrors
    :func:`_load_shipped_airplane_font_sizes` — the shared blob
    reader handles every "file present but malformed" branch
    uniformly so this loader and the font-profile loaders never
    diverge on edge cases.
    """
    raw = _read_shipped_font_settings_blob(project_root)
    if raw is None:
        return None
    try:
        return int(raw["traffic_icon_size_px"])
    except (KeyError, TypeError, ValueError):
        return None


def save_traffic_icon_size_px(value: int) -> None:
    """Persist the traffic-icon base size in pixels.

    Stored under the flat ``traffic_icon_size_px`` QSettings key,
    deliberately *not* prefixed with ``font_`` because it isn't a
    font dimension and the airplane-vs-normal split that the font
    keys carry doesn't apply (one global icon size — see the
    rationale on :data:`DEFAULT_TRAFFIC_ICON_SIZE_PX`).
    """
    _settings().setValue("traffic_icon_size_px", int(value))


def load_waypoint_marker_size_px(
    project_root: Path | None = None,
) -> int:
    """Return the on-chart waypoint-marker triangle side length in
    pixels (the size of a VRP marker drawn on top of the
    satellite overlay).

    Three-rung priority ladder, identical in shape to
    :func:`load_traffic_icon_size_px`:

      1. **QSettings** (per-machine, per-user). Set whenever the
         user adjusts the spinbox in Display Settings; wins once
         present.
      2. **``<project_root>/.cvfr_routemaster/font_settings.json``** —
         the release-shipped first-launch default under the flat
         key ``waypoint_marker_size_px``. Older shipped blobs
         that pre-date this feature simply lack the key and fall
         through to the hard-coded default.
      3. **``DEFAULT_WAYPOINT_MARKER_SIZE_PX``**. Last resort.

    No range clamping at the loader: the dialog enforces
    ``WAYPOINT_MARKER_SIZE_MIN_PX`` / ``WAYPOINT_MARKER_SIZE_MAX_PX``
    at edit time. A hand-edited QSettings value outside the range
    is honoured unchanged so the round-trip is honest, same
    contract as the font + traffic-icon loaders.

    Args:
        project_root: The app's project directory (the one that
            contains ``.cvfr_routemaster/``). Required to find
            the shipped fallback file. Optional for backwards
            compat with old callers and tests; passing ``None``
            skips the shipped-file rung.
    """
    shipped = (
        _load_shipped_waypoint_marker_size_px(project_root)
        if project_root is not None
        else None
    )
    default = (
        shipped if shipped is not None else DEFAULT_WAYPOINT_MARKER_SIZE_PX
    )
    return int(
        _settings().value("waypoint_marker_size_px", default, int)
    )


def _load_shipped_waypoint_marker_size_px(
    project_root: Path,
) -> int | None:
    """Read the waypoint-marker size from the shared
    ``font_settings.json`` if the dev's build wrote it.

    Returns ``None`` if the file is absent, malformed, or simply
    pre-dates this feature (i.e. the JSON has the font + traffic
    keys but no ``waypoint_marker_size_px``). Callers then fall
    through to :data:`DEFAULT_WAYPOINT_MARKER_SIZE_PX`. Mirrors
    :func:`_load_shipped_traffic_icon_size_px` — same shared blob
    reader so the file format stays uniform across all three
    "display preferences" knobs (fonts, traffic icon, marker).
    """
    raw = _read_shipped_font_settings_blob(project_root)
    if raw is None:
        return None
    try:
        return int(raw["waypoint_marker_size_px"])
    except (KeyError, TypeError, ValueError):
        return None


def save_waypoint_marker_size_px(value: int) -> None:
    """Persist the waypoint-marker triangle side length in pixels.

    Stored under the flat ``waypoint_marker_size_px`` QSettings
    key, alongside the traffic-icon key. Both are "on-chart
    overlay geometry" knobs and live outside the font-size
    profile machinery (no airplane-vs-normal split because
    airplane mode hides the chart anyway).
    """
    _settings().setValue("waypoint_marker_size_px", int(value))


def load_waypoint_show_latlon_cols() -> bool:
    """Whether the waypoint pane shows the four lat/lon columns (Lat°, Lon°,
    Lat DMS, Lon DMS).

    Default is False — for typical flight planning the user works from the
    code/Hebrew name + reporting type, and the chart link fills in geography
    visually. Hiding the four numeric columns keeps the table readable in a
    narrow pane. Persisted across sessions so the user's choice sticks.
    """
    return bool(_settings().value("waypoint_show_latlon_cols", False, bool))


def save_waypoint_show_latlon_cols(show: bool) -> None:
    _settings().setValue("waypoint_show_latlon_cols", bool(show))


# --- Traffic overlay visibility -----------------------------------------
# View-mode toggle for the live VATSIM traffic overlay (v2 feature; see
# ROADMAP-NEXT.md and cvfr_routemaster.traffic_overlay). Default is False
# so a fresh install starts with the overlay off — the user opts into the
# extra visual layer when they want it. Persisted across sessions so the
# choice sticks the same way the airplane-mode and hide-usage-hints
# toggles do.
#
# No shipped-default rung here on purpose: the visibility is a runtime
# preference, not a default-the-user-might-want-on-first-launch. If we
# ever want to ship "traffic on by default" it'd be a one-line addition
# of a shipped-JSON lookup, mirroring load_traffic_icon_size_px.


def load_show_vatsim_traffic() -> bool:
    """Whether the live VATSIM traffic overlay is enabled.

    Default is ``False`` so a freshly installed app starts with no
    traffic drawn — the user opts in via the toolbar toggle when
    they want the extra visual layer. Persisted across sessions
    via :class:`QSettings`.
    """
    return bool(_settings().value("show_vatsim_traffic", False, bool))


def save_show_vatsim_traffic(show: bool) -> None:
    """Persist the current state of the "Show VATSIM traffic"
    toolbar toggle. Called from the toggle's ``toggled`` slot in
    :class:`MainWindow` so user changes survive app restarts.
    """
    _settings().setValue("show_vatsim_traffic", bool(show))


# --- Satellite view preferences -----------------------------------------
# Three preference keys for the v3 satellite-imagery feature
# (see ROADMAP-NEXT.md):
#
# * ``show_satellite``: view-mode toggle for the toolbar's "Satellite
#   view" QAction. Same default (off) and same lifecycle as
#   ``show_vatsim_traffic`` — fresh installs start in chart mode and
#   the user opts into the satellite render.
# * ``satellite_notice_shown``: boolean — has the user been shown the
#   one-time informational notice about the satellite-imagery bulk
#   download (size, resume-on-interrupt, etc.)? Drives a single
#   pre-download notification: if it's False on a fresh install with
#   an empty tile cache we show the notice and start the download;
#   if it's True we silently resume any partial download. The
#   notice is **informational only** — there is no accept/decline
#   path; the program needs the tiles to function and downloads
#   them unconditionally. The flag exists only so we don't re-show
#   the same notice on every launch.
#
#   Migration: pre-v3.3 builds recorded a tri-state under the
#   ``satellite_download_decision`` key (``""``/``"accepted"``/
#   ``"declined"``) when the design included an accept-or-decline
#   prompt. :func:`load_satellite_notice_shown` reads the new key
#   if it exists; otherwise it interprets the legacy key as
#   "shown" iff the user previously accepted. Anything else
#   ("declined" or unset) is treated as "not shown" so the
#   simpler v3.3+ notice gets a chance to surface once on first
#   launch under the new flow.
# * ``satellite_target_zoom``: Web Mercator zoom level for the bulk
#   fetch. Default :data:`DEFAULT_SATELLITE_ZOOM` matches
#   :data:`satellite_tiles.DEFAULT_TARGET_ZOOM`. User-configurable in
#   the range ``[MIN, MAX]`` (12..16) in a future Display Settings
#   panel — for now it's a direct QSettings read/write so a power
#   user can edit ``QSettings`` by hand.

#: Default satellite zoom; mirrors
#: :data:`cvfr_routemaster.satellite_tiles.DEFAULT_TARGET_ZOOM`. Kept
#: as a module-level constant here so callers in :mod:`main_window`
#: don't have to import ``satellite_tiles`` just for the default.
DEFAULT_SATELLITE_ZOOM: int = 15

#: Allowed satellite zoom range. Lower is faster + smaller cache;
#: higher is more detail. Out-of-range values from QSettings clamp
#: silently to this band.
MIN_SATELLITE_ZOOM: int = 12
MAX_SATELLITE_ZOOM: int = 16


def load_show_satellite() -> bool:
    """Whether the satellite-view toggle is on (default off)."""
    return bool(_settings().value("show_satellite", False, bool))


def save_show_satellite(show: bool) -> None:
    """Persist the toolbar's satellite-view toggle."""
    _settings().setValue("show_satellite", bool(show))


def load_satellite_notice_shown() -> bool:
    """Whether the one-time satellite-download notice has been shown.

    The notice is purely informational (size estimate + resume-on-
    interrupt explanation); the download itself happens
    unconditionally, since the program needs the imagery to be
    useful. This flag exists only so that the same notice doesn't
    pop on every launch.

    Returns ``True`` if a prior session already surfaced the
    notice (so this session should silently resume any partial
    download), ``False`` otherwise (so this session should show
    the notice once and then kick off the bulk download).

    Migration from the pre-v3.3 tri-state decision key:

    * Old ``"accepted"`` → ``True``. The user has already seen
      the (older, more verbose) consent dialog and authorised
      the download; no need to surface the new notice.
    * Old ``"declined"`` → ``False``. The decline path no
      longer exists; let the v3.3+ notice introduce the user
      to the (now-unconditional) download flow.
    * Old ``""`` / unset → ``False``. Fresh install.

    Manual ``QSettings`` edits that put something nonsensical
    (a number, a stray quoted string) into the new key default
    to ``False`` rather than raising, so a corrupted user
    profile re-surfaces the notice instead of skipping it.
    """
    s = _settings()
    if s.contains("satellite_notice_shown"):
        return bool(s.value("satellite_notice_shown", False, bool))
    legacy = str(s.value("satellite_download_decision", "", str) or "")
    return legacy == "accepted"


def save_satellite_notice_shown(shown: bool) -> None:
    """Persist that the satellite-download notice has been shown.

    Boolean only — there's no tri-state any more. Callers that
    want to "reset" the notice (so it appears again next launch)
    pass ``False``; the normal path is to call this with ``True``
    right after the notice dialog closes for the first time.
    """
    _settings().setValue("satellite_notice_shown", bool(shown))


def load_satellite_zoom() -> int:
    """The user's chosen Web Mercator zoom for the satellite warp.

    Clamped to ``[MIN_SATELLITE_ZOOM, MAX_SATELLITE_ZOOM]`` on read
    so a corrupted QSettings entry never asks the renderer to
    fetch impossibly-detailed tiles.
    """
    raw = _settings().value(
        "satellite_target_zoom", DEFAULT_SATELLITE_ZOOM, int
    )
    try:
        z = int(raw)
    except (TypeError, ValueError):
        z = DEFAULT_SATELLITE_ZOOM
    if z < MIN_SATELLITE_ZOOM:
        return MIN_SATELLITE_ZOOM
    if z > MAX_SATELLITE_ZOOM:
        return MAX_SATELLITE_ZOOM
    return z


def save_satellite_zoom(zoom: int) -> None:
    """Persist the satellite zoom, clamped to the supported range."""
    z = max(MIN_SATELLITE_ZOOM, min(MAX_SATELLITE_ZOOM, int(zoom)))
    _settings().setValue("satellite_target_zoom", z)
