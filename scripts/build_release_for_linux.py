"""End-to-end builder for the CVFR Route Master Linux release folder.

Runs the full pipeline that produces ``release-linux/`` ready to ship
to a Debian-derivative box (target: Debian 13; should also work on
Ubuntu 24.04+, Mint 22+, and other glibc >= 2.40 distros built since
~late 2025):

    release-linux/
    ├── cvfr-routemaster              ← single-file PyInstaller ELF binary
    ├── icon.png                      ← 256×256 launcher icon (referenced by .desktop)
    ├── cvfr-routemaster.desktop      ← Desktop Entry template (run install-shortcut.sh to deploy)
    ├── install-shortcut.sh           ← one-shot script that creates a system menu entry
    ├── check-runtime-deps.sh         ← target-box runtime-libs probe
    ├── run-on-wsl.sh                 ← WSL-only wrapper (sets QT_QPA_PLATFORM=xcb;
    │                                   no-op + irrelevant on native Linux,
    │                                   typically removed from the friend-facing tarball)
    ├── README.txt                    ← user-facing one-pager
    └── .cvfr_routemaster/            ← seed cache copied from your dev caches
        ├── .v4_migrated              ← marks the tree as already v4-namespaced
        ├── font_settings.json        ← derived dev font sizes (GLOBAL, optional)
        ├── cvfr/                     ← CVFR mode namespace
        │   ├── geo_calibration.json
        │   ├── altitude_arrows_north.json
        │   ├── altitude_arrows_south.json
        │   ├── waypoints_cache.json
        │   ├── map_images_meta.json
        │   ├── chart_sources.json    ← the default CAAI URLs for CVFR
        │   └── map_layout.json       ← derived sheet positions/scales
        └── lsa/                      ← LSA mode namespace (same JSON set)
            └── ...

The chart PDFs themselves are NOT shipped — Israeli government
terms of use prohibit redistribution. On first launch the program
downloads them from the URLs in ``chart_sources.json`` into
``.cvfr_routemaster/charts/``.

Why no ``tesseract/`` subfolder (vs the Windows release)?

Linux Tesseract is dynamically linked against ~50 system shared
libraries at fixed FHS paths; reliably bundling it portably means
``ldd``-walking the dep graph and patching rpath, which is fragile
across glibc versions and silently breaks when transitive deps load
via ``dlopen`` at runtime. The Linux release instead expects a
one-time::

    sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-heb

on the user's box. The OCR path (back-pages waypoint extraction)
only runs when the shipped ``waypoints_cache.json`` invalidates —
i.e. when the user swaps in a new chart cycle or hits the "Re-OCR
waypoints" menu item. Until then no Tesseract is needed at all.

When the OCR path *does* fire and Tesseract isn't installed, the
user sees a Qt dialog with the exact ``apt install`` command above
(see ``back_page_ocr._tesseract_missing_message``), so the only
friction is "click OK, paste one apt command, relaunch."

Pipeline:

    1. Sanity-check prerequisites (running on Linux + 3 PDFs +
       calibration JSON exist).
    2. Generate / refresh ``release-linux/icon.png`` (256×256 PNG
       using the same compass/route artwork as the Windows .ico).
    3. Wipe and recreate ``release-linux/`` (preserving the freshly-
       regenerated icon).
    4. Invoke PyInstaller against ``cvfr-routemaster-linux.spec``.
    5. Move the freshly-built binary out of PyInstaller's ``dist/``
       into ``release-linux/`` and ensure it's executable.
    6. Copy the seed ``.cvfr_routemaster/`` cache into ``release-linux/``
       (NO chart PDFs — those are downloaded by the runtime on first use).
    7. Bake the shipped derived JSONs: ``chart_sources.json``
       (CAAI URLs), ``map_layout.json`` (from calibration), and
       ``font_settings.json`` (from dev QSettings).
    8. Write the ``.desktop`` Desktop Entry template (with placeholder
       paths) + the ``install-shortcut.sh`` deployment script + the
       ``check-runtime-deps.sh`` lib probe + the ``run-on-wsl.sh``
       WSL launcher wrapper + the friend-facing ``README.txt``.
    9. Print a summary (folder size, file count, "next steps").

This script must be run on a Linux system. PyInstaller is platform-
specific — you can't cross-compile a Linux ELF from Windows. Two
practical options for the user (who develops on Windows):

- **WSL Debian** — install once, then ``cd /mnt/<drive>/<path-to-repo>
  && python3 scripts/build_release_for_linux.py``.
- **Build directly on the Debian 13 laptop** — ``git clone`` (or rsync)
  the repo there, run the script natively. Simplest if the laptop
  is already set up for VATSIM use.

Usage::

    python3 scripts/build_release_for_linux.py

Or, when driving the build from a PowerShell session on the Windows
dev box, invoke the wrapper that handles venv activation and dodges
three-layer (PowerShell → wsl → bash) quoting::

    wsl -d Debian -- bash /mnt/<drive>/<path-to-repo>/scripts/_wsl_build_linux.sh

See ``scripts/_wsl_build_linux.sh`` for the venv layout the wrapper
expects (one-time setup is a Debian-side ``python3 -m venv ~/cvfr-build-venv``
followed by ``pip install -r requirements-dev.txt``).

Optional flags::

    --skip-pyinstaller   Skip the PyInstaller invocation; useful when
                         iterating on the README / PDF-copy logic
                         without re-running the slow ELF build.
    --skip-icon          Don't regenerate the icon.

Run from the repo root.
"""

from __future__ import annotations

import argparse
import shutil
import stat
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = REPO_ROOT / "release-linux"
DIST_DIR = REPO_ROOT / "dist"
BUILD_DIR = REPO_ROOT / "build"
SPEC_FILE = REPO_ROOT / "cvfr-routemaster-linux.spec"
DEV_CACHE_DIR = REPO_ROOT / ".cvfr_routemaster"

# The stem PyInstaller derives from ``SPEC_FILE`` for its per-build
# subdirectory under ``build/`` and for the warn-file name. Kept as
# its own constant so the warn-scan step stays correct if the spec
# file is ever renamed.
SPEC_STEM = "cvfr-routemaster-linux"

# Top-level package the warn-scan filters importers by. A missing
# top-level import from anywhere under this prefix fails the build;
# misses from PIL / PySide6 / pytesseract are PyInstaller's normal
# noise (third-party libs have many optional top-level imports we
# don't care about).
APP_PACKAGE = "cvfr_routemaster"

# The chart PDFs are NOT shipped in the release (v3.3+: Israeli
# government terms of use prohibit redistribution). Checked at
# build-prereq time (see ``_check_prerequisites``) from the per-mode
# runtime cache the app downloads into
# ``.cvfr_routemaster/<mode_id>/charts/`` rather than fixed dev PDFs at
# the repo root, so a fresh checkout calibrated via the normal download
# flow still builds and the check covers every registered mode. The
# runtime fetches from the CAAI URLs in the map-mode registry.

# Files inside ``.cvfr_routemaster/`` that are worth seeding into the
# release. Same set as the Windows build — see the equivalent constant
# in ``scripts/build_release.py`` for the per-file rationale.
#
# v3.3+ change: ``map_north.png`` and ``map_south.png`` are NO
# LONGER shipped (they're rendered output of the chart PDFs, which
# Israeli government terms of use prohibit redistribution of). The
# runtime renders them on first chart-load against the just-
# downloaded PDFs.
CACHE_FILES: tuple[str, ...] = (
    "geo_calibration.json",
    "altitude_arrows_north.json",
    "altitude_arrows_south.json",
    "waypoints_cache.json",
    "map_images_meta.json",
)

# Linux-launcher icon at 256×256 — the standard size for GNOME/KDE/XFCE
# launchers. Most desktop environments accept any sane size and rescale
# as needed; 256 is large enough to look sharp on hi-DPI displays
# without bloating the release.
ICON_SIZE = 256

EXE_NAME = "cvfr-routemaster"

# Marker the runtime drops once the one-time v3.3→v4 migration has
# run (``cvfr_routemaster.mode_migration.MARKER_FILENAME``). A v4
# release already ships its seed under per-mode namespaces, so we
# ship the marker too — telling a fresh install "already v4, no flat
# caches to relocate".
V4_MIGRATION_MARKER = ".v4_migrated"


def _seed_mode_ids() -> tuple[str, ...]:
    """Mode ids whose seed caches the release bundles.

    Sourced from the live ``map_modes`` registry (see the Windows
    build script's identical helper) so registering a new chart
    product extends the build automatically.
    """
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from cvfr_routemaster import map_modes
    finally:
        sys.path.pop(0)
    return map_modes.mode_ids()


# Runtime apt packages the user must have on the target Debian box.
#
# Derived experimentally by:
#   1. Building the ELF in WSL Debian 13.
#   2. Running it under ldd (binary itself + every plugin .so PyInstaller
#      bundles in the self-extract tree at /tmp/_MEIxxxx/PySide6/Qt/plugins/).
#   3. Mapping each "not found" .so back to its providing apt package via
#      apt-file. Pruning the list to libs needed by plugins we actually
#      load (libqxcb, libqoffscreen, libqsvg, ...) — not every plugin
#      collect_all('PySide6') drags in (libpulse for unused QtMultimedia,
#      libnss3 for unused QtWebEngine, etc.).
#
# Two tiers:
#
# - ``RUNTIME_QT_APT_PACKAGES``: required for the binary to start at all.
#   Without these, Qt aborts at platform-plugin init with "Could not load
#   the Qt platform plugin 'xcb'" — which is the failure mode users hit
#   on minimal Debian installs (no desktop env) or on Debian boxes that
#   never installed any other Qt6 GUI app. ``libxcb-cursor0`` in particular
#   is the well-known Qt6-on-Debian-12+/Ubuntu-24.04+ gotcha; it's not in
#   the typical desktop-environment dep set.
#
# - ``RUNTIME_OCR_APT_PACKAGES``: optional — only needed if the user
#   re-OCRs (when waypoints_cache.json invalidates because the chart cycle
#   changed). The shipped seed cache means the typical user never needs
#   these. The ``-heb`` package is critical because back-pages text is
#   Hebrew; without it Tesseract returns garbage despite "appearing
#   to work" with just ``-eng``.
#
# These constants drive both ``check-runtime-deps.sh`` (the helper script
# we ship in release-linux/) and the ``README.txt`` apt-install snippets.
# Keeping them as Python lists rather than scattered string literals so
# regression tests can verify the README and the script both reference
# the same package set.
RUNTIME_QT_APT_PACKAGES: tuple[str, ...] = (
    # X11 keyboard handling. libxkbcommon0 ships on every Debian desktop
    # install but is absent on minimal/server images.
    "libxkbcommon0",
    "libxkbcommon-x11-0",
    # Font stack. libfreetype6 + libfontconfig1 are nearly always present
    # because every modern app needs them, but we list them for completeness.
    "libfreetype6",
    "libfontconfig1",
    # OpenGL / EGL. Even the "software-only" Qt platform plugin path
    # links against these, so they have to be present even on boxes with
    # no real GPU. Mesa provides these via libgl1 + libegl1 + libopengl0.
    "libgl1",
    "libegl1",
    "libopengl0",
    # GLib (Qt event loop integration). The ``t64`` suffix is Debian 13's
    # 64-bit time_t transition package; older distros call it libglib2.0-0.
    # apt is smart enough to redirect the legacy name to the t64 variant
    # so we ship the t64 name (the modern correct one).
    "libglib2.0-0t64",
    # XCB libraries needed by the xcb platform plugin (Qt6's default on X11).
    # libxcb-cursor0 is the one most often missing — it's a Qt6-specific
    # dependency that Qt5 didn't have, so older "I have a Debian desktop
    # installed already" reasoning doesn't cover it.
    "libxcb1",
    "libxcb-cursor0",
    "libxcb-icccm4",
    "libxcb-image0",
    "libxcb-keysyms1",
    "libxcb-randr0",
    "libxcb-render0",
    "libxcb-render-util0",
    "libxcb-shape0",
    "libxcb-shm0",
    "libxcb-sync1",
    "libxcb-util1",
    "libxcb-xkb1",
    # X11 client libs underneath xcb.
    "libx11-6",
    "libx11-xcb1",
    # D-Bus client. Qt uses it for accessibility, theme detection, and
    # screensaver coordination. Almost always present but listed for
    # completeness on minimal installs.
    "libdbus-1-3",
    # A fallback font set. PySide6 doesn't bundle fonts, so if the system
    # has none Qt falls back to a non-rendering placeholder and you get
    # blank labels everywhere. fonts-dejavu-core is ~1 MB and ubiquitous.
    "fonts-dejavu-core",
)

RUNTIME_OCR_APT_PACKAGES: tuple[str, ...] = (
    "tesseract-ocr",
    "tesseract-ocr-eng",
    "tesseract-ocr-heb",
)


# Mapping of apt package name → the ``soname`` filename ``check-runtime-deps.sh``
# greps for in ``ldconfig -p``. Used by the runtime-check helper to detect
# missing libs without needing dpkg-query (which doesn't work cleanly when
# packages were installed via flatpak/snap/manual rpm-into-deb conversion).
# Keep aligned with RUNTIME_QT_APT_PACKAGES — the set of *.so probes here
# must cover every package above except the font package (which dpkg
# detects directly).
RUNTIME_LIB_PROBES: tuple[tuple[str, str], ...] = (
    ("libxkbcommon0", "libxkbcommon.so.0"),
    ("libxkbcommon-x11-0", "libxkbcommon-x11.so.0"),
    ("libfreetype6", "libfreetype.so.6"),
    ("libfontconfig1", "libfontconfig.so.1"),
    ("libgl1", "libGL.so.1"),
    ("libegl1", "libEGL.so.1"),
    ("libopengl0", "libOpenGL.so.0"),
    ("libglib2.0-0t64", "libglib-2.0.so.0"),
    ("libxcb1", "libxcb.so.1"),
    ("libxcb-cursor0", "libxcb-cursor.so.0"),
    ("libxcb-icccm4", "libxcb-icccm.so.4"),
    ("libxcb-image0", "libxcb-image.so.0"),
    ("libxcb-keysyms1", "libxcb-keysyms.so.1"),
    ("libxcb-randr0", "libxcb-randr.so.0"),
    ("libxcb-render0", "libxcb-render.so.0"),
    ("libxcb-render-util0", "libxcb-render-util.so.0"),
    ("libxcb-shape0", "libxcb-shape.so.0"),
    ("libxcb-shm0", "libxcb-shm.so.0"),
    ("libxcb-sync1", "libxcb-sync.so.1"),
    ("libxcb-util1", "libxcb-util.so.1"),
    ("libxcb-xkb1", "libxcb-xkb.so.1"),
    ("libx11-6", "libX11.so.6"),
    ("libx11-xcb1", "libX11-xcb.so.1"),
    ("libdbus-1-3", "libdbus-1.so.3"),
)


def _step(msg: str) -> None:
    print(f"\n=== {msg} ===")


def _check_prerequisites() -> None:
    """Fail fast if the environment can't produce a valid release.

    Linux-specific checks layered on top of the Windows prereq set:

    - Refuse to run on non-Linux hosts. PyInstaller is platform-
      specific — a "build" on Windows would produce a Windows .exe
      no matter what spec we pass. Better to bail with a clear
      message than ship an .exe pretending to be a Linux release.
    - Same chart-PDF + calibration-JSON checks as the Windows
      build, because the user's dev cache is the source of truth
      for what gets seeded into the release.

    No Tesseract check (the Linux release deliberately doesn't
    bundle it — see module docstring).
    """
    _step("Checking prerequisites")
    if not sys.platform.startswith("linux"):
        print(
            f"ERROR: this script must run on Linux (current platform: "
            f"{sys.platform!r}). PyInstaller can't cross-compile "
            "a Linux ELF from Windows or macOS. Run this from WSL "
            "Debian or directly on your Debian box.",
            file=sys.stderr,
        )
        sys.exit(1)

    missing: list[str] = []
    # Lazy import (same rationale as ``_seed_mode_ids``): keep the
    # package off the module-scope import path.
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from cvfr_routemaster import map_modes
        from cvfr_routemaster.chart_source import cache_path_for_sheet
    finally:
        sys.path.pop(0)
    # v4: require the downloaded chart PDFs + a warm calibration for
    # every registered chart product so a fresh install opens
    # geo-referenced in any mode. Charts live under
    # ``.cvfr_routemaster/<mode_id>/charts/`` (downloaded from CAAI;
    # not shipped — redistribution prohibited).
    for mode_id in map_modes.mode_ids():
        mode = map_modes.get_mode(mode_id)
        for sheet_key in mode.sheet_keys:
            pdf = cache_path_for_sheet(sheet_key, REPO_ROOT, mode_id)
            if not pdf.is_file():
                missing.append(
                    f"  - .cvfr_routemaster/{mode_id}/charts/{pdf.name} "
                    f"(open the app in {mode_id.upper()} mode so it "
                    "downloads the sheet from CAAI)"
                )
        cal = DEV_CACHE_DIR / mode_id / "geo_calibration.json"
        if not cal.is_file():
            missing.append(
                f"  - .cvfr_routemaster/{mode_id}/geo_calibration.json "
                f"(open the app in {mode_id.upper()} mode at least once "
                "and complete the 'Calibrate north / Calibrate south' "
                "anchors so the user inherits your calibration)"
            )
    if missing:
        print("ERROR: missing prerequisites:", file=sys.stderr)
        for line in missing:
            print(line, file=sys.stderr)
        sys.exit(1)
    print("All prerequisites present.")


def _regenerate_icon() -> None:
    """Render the 256×256 launcher PNG using the same compass/route
    artwork as the Windows .ico generator.

    We import the private ``_render_icon`` helper from the icon
    generator rather than shelling out to it, because the existing
    generator's CLI only emits .ico (Windows multi-resolution
    container) — Linux desktop entries want a single .png, and
    refactoring the CLI to support both formats was more work
    than just calling the renderer directly.
    """
    _step(f"Regenerating release-linux/icon.png ({ICON_SIZE}×{ICON_SIZE})")
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        from generate_release_icon import _render_icon
    finally:
        sys.path.pop(0)
    img = _render_icon(ICON_SIZE)
    RELEASE_DIR.mkdir(exist_ok=True)
    out = RELEASE_DIR / "icon.png"
    img.save(out, format="PNG")
    print(f"  {out.relative_to(REPO_ROOT)} ({out.stat().st_size:,} bytes)")


def _clean_release_dir() -> None:
    """Wipe ``release-linux/`` to a known state — but preserve the
    freshly-regenerated ``icon.png`` so the next steps don't have to
    re-render it.

    PyInstaller's own ``build/`` and ``dist/`` are also wiped so a
    stale half-built tree from a previous attempt doesn't bleed into
    this one.
    """
    _step("Cleaning release-linux/ build/ dist/")
    icon_path = RELEASE_DIR / "icon.png"
    icon_bytes = icon_path.read_bytes() if icon_path.is_file() else None

    for d in (RELEASE_DIR, DIST_DIR, BUILD_DIR):
        if d.is_dir():
            shutil.rmtree(d)
            print(f"  removed {d.relative_to(REPO_ROOT)}/")

    RELEASE_DIR.mkdir()
    if icon_bytes is not None:
        icon_path.write_bytes(icon_bytes)
        print(f"  preserved icon.png ({len(icon_bytes):,} bytes)")


def _run_pyinstaller() -> None:
    """Run PyInstaller. No retry loop on Linux because Defender-style
    AV-locking-files races aren't a thing here — the build either
    works or fails for a deterministic reason worth surfacing
    immediately."""
    _step("Running PyInstaller (this takes 2-5 minutes)")
    cmd = [
        sys.executable,
        "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        str(SPEC_FILE),
    ]
    print("  $ " + " ".join(cmd))
    subprocess.run(cmd, cwd=str(REPO_ROOT), check=True)
    print("PyInstaller finished.")


def _scan_pyinstaller_warnings() -> None:
    """Fail the build if PyInstaller's warn file lists missing
    top-level imports from inside the application package.

    Catches the failure mode that shipped Linux release v2: the
    WSL build venv was assembled without numpy, PyInstaller's
    analyser correctly flagged
    ``missing module named numpy - imported by cvfr_routemaster.map_crop (top-level)``,
    but the build script previously didn't enforce anything and the
    binary crashed on first launch with ``ModuleNotFoundError``.

    See ``scripts/_pyinstaller_warnings.py`` for the parser and the
    filtering rationale (why we ignore non-top-level qualifiers and
    third-party importers).
    """
    # Imported lazily so the rest of the build script doesn't pay
    # the import cost when running with --skip-pyinstaller, and so
    # ``scripts/`` consumers that don't have ``scripts/`` on
    # ``sys.path`` (e.g. ad-hoc one-shot CLI invocations) can still
    # import the module-level functions.
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from scripts._pyinstaller_warnings import (
            format_missing_imports_message,
            scan_missing_top_level_imports,
        )
    finally:
        sys.path.pop(0)

    _step("Scanning PyInstaller warn file for missing top-level imports")
    warn_path = BUILD_DIR / SPEC_STEM / f"warn-{SPEC_STEM}.txt"
    if not warn_path.is_file():
        # PyInstaller always writes this file on a successful run, so
        # missing-warn-file is itself a sign something is wrong with
        # the build — surface it but don't fail (the binary may still
        # be fine; we just couldn't verify).
        print(
            f"  WARNING: expected warn file {warn_path.relative_to(REPO_ROOT)} "
            "is missing; cannot verify top-level imports.",
            file=sys.stderr,
        )
        return
    missing = scan_missing_top_level_imports(warn_path, APP_PACKAGE)
    if missing:
        print(format_missing_imports_message(missing), file=sys.stderr)
        sys.exit(1)
    print(
        f"  no unresolved top-level imports from {APP_PACKAGE}.* "
        f"({warn_path.relative_to(REPO_ROOT)})"
    )


def _copy_exe() -> None:
    """Move the freshly-built ELF into ``release-linux/`` and ensure
    it has the executable bit set.

    PyInstaller already produces an executable file (mode 0755) and
    ``shutil.copy2`` preserves the mode bits, so the ``chmod +x``
    here is belt-and-braces — handles the edge case where the source
    tree (e.g. a Windows-mounted filesystem under WSL) silently
    strips Unix permission bits.
    """
    _step(f"Moving {EXE_NAME} into release-linux/")
    src = DIST_DIR / EXE_NAME
    if not src.is_file():
        print(
            f"ERROR: PyInstaller did not produce {src}.\n"
            "Inspect the PyInstaller output above for build errors.",
            file=sys.stderr,
        )
        sys.exit(1)
    dst = RELEASE_DIR / EXE_NAME
    shutil.copy2(src, dst)
    # Add +x for owner/group/other regardless of source mode, so a
    # WSL-mounted-NTFS source tree (which can't store the +x bit)
    # doesn't ship a non-executable binary.
    cur = dst.stat().st_mode
    dst.chmod(cur | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    size_mb = dst.stat().st_size / (1024 * 1024)
    print(f"  {dst.relative_to(REPO_ROOT)} ({size_mb:.1f} MiB, executable)")


# v3.3+ no longer copies the chart PDFs into the release bundle — the
# runtime downloads them from CAAI URLs on first use. The previous
# ``_copy_charts()`` function (and its companion ``release-linux/
# map-pdfs/`` subdirectory) have been removed. The WSL 9P mtime-
# flooring bug that motivated the old build-time restamp step is no
# longer reachable because no PDFs are copied through the bridge.


def _write_shipped_derived_files() -> None:
    """Bake derived JSONs into ``release-linux/.cvfr_routemaster/``.

    Symmetric with the Windows build's ``_write_shipped_derived_files``
    — see that function's docstring (in ``scripts/build_release.py``)
    for the full rationale. The three files written here:

    1. ``chart_sources.json`` — the three CAAI URLs the runtime
       fetches from on first launch.
    2. ``map_layout.json`` — derived from shipped calibration.
    3. ``font_settings.json`` — derived from the dev's QSettings.

    The pre-v3.3 mtime-restamp step is gone — no PDFs are shipped,
    so there are no PDFs to align cache fingerprints against. The
    runtime restamp (in ``cvfr_routemaster.cache_restamp``)
    rewrites cache mtimes after each successful chart download so
    the cache validity check passes against the just-downloaded
    file. The shipped cache JSON's ``size`` field — which the
    runtime restamp does NOT overwrite — is what gates correctness:
    cache is valid iff the shipped size matches the downloaded
    size (i.e. CAAI is serving the same byte content the dev
    calibrated against). Cookbook step 1 makes the dev verify
    this before each release.
    """
    # ``write_shipped_font_settings`` does a delayed
    # ``from cvfr_routemaster.settings_store import ...`` at call
    # time. Keep REPO_ROOT on ``sys.path`` for the delayed import.
    sys.path.insert(0, str(REPO_ROOT))
    from scripts._restamp_cache_fingerprints import (
        write_shipped_font_settings,
        write_shipped_map_layout,
    )
    from cvfr_routemaster import map_modes
    from cvfr_routemaster.settings_store import save_shipped_chart_sources

    # Per-mode chart_sources.json + map_layout.json (CVFR + LSA);
    # see the Windows build's equivalent for the rationale.
    for mode in map_modes.all_modes():
        sources = {
            sheet.key: sheet.default_url
            for sheet in mode.sheets
            if sheet.key in ("north", "south", "back")
        }
        _step(
            f"[{mode.mode_id}] Writing shipped chart_sources.json "
            "(default CAAI URLs the runtime fetches on first launch)"
        )
        save_shipped_chart_sources(RELEASE_DIR, sources, mode_id=mode.mode_id)
        for sheet_key, url in sources.items():
            print(f"  {mode.mode_id}/{sheet_key:<6} -> ...{url[-60:]}")

        _step(
            f"[{mode.mode_id}] Writing shipped map_layout.json so "
            "calibration loads on first launch (QSettings doesn't ship)"
        )
        layout_report = write_shipped_map_layout(RELEASE_DIR, mode_id=mode.mode_id)
        if layout_report.written is not None:
            print(f"  wrote: {mode.mode_id}/{layout_report.written.name}")
            for k, v in (layout_report.layout or {}).items():
                print(f"    {k} = {v}")
        elif layout_report.reason == "file_absent":
            print(
                f"  [{mode.mode_id}] no geo_calibration.json shipped "
                "(nothing to derive from)"
            )
        elif layout_report.reason == "no_layouts":
            print(
                f"  [{mode.mode_id}] geo_calibration.json has no usable "
                "map_layout blocks"
            )
        elif layout_report.reason == "meta_missing":
            # geo_calibration.json present (so calibration is shipped),
            # but map_images_meta.json missing — we can't compute the
            # default south Y for an uncalibrated sheet. That's a build
            # ordering bug: _copy_seed_cache must ship both files.
            print(
                f"ERROR: [{mode.mode_id}] needed map_images_meta.json to "
                "compute default placement for an uncalibrated sheet, but "
                "it's absent.\n  Did _copy_seed_cache() ship both files?",
                file=sys.stderr,
            )
            sys.exit(1)

    _step(
        "Writing shipped font_settings.json so the dev's UI font "
        "sizes ride the release (QSettings doesn't ship)"
    )
    fonts_report = write_shipped_font_settings(RELEASE_DIR)
    if fonts_report.written is not None:
        print(f"  wrote: {fonts_report.written.name}")
        for k, v in (fonts_report.sizes or {}).items():
            print(f"    {k} = {v}")
    elif fonts_report.reason == "qsettings_empty":
        # Same rationale as the Windows build script — not an
        # error, just means the dev never customised fonts on
        # this box.
        print("  dev QSettings has no font-size keys; falling through to defaults")


def _sanitize_shipped_cache_paths() -> None:
    """Strip the dev box's absolute paths from every shipped cache
    JSON, leaving just the PDF basename.

    Without this step, the cache JSONs ship the dev's filesystem
    layout (``C:\\flying\\cvfr-routemaster\\<pdf>`` — the dev runs
    the app on Windows even though the build target is Linux) inside
    every release bundle. The Linux build pipeline copies the
    Windows-written dev caches verbatim through WSL's 9P bridge, so
    without sanitisation the Linux release also ships the
    Windows-style absolute paths.

    No functional purpose — the cache validity check uses
    ``mtime_ns`` + ``size``, not the ``path`` string. See
    ``scripts/_sanitize_shipped_cache_paths.py`` for the full
    rationale.

    Must run **after** ``_write_shipped_derived_files`` because
    those helpers write the JSONs back to disk — running
    sanitisation before would have its rewrites clobbered.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from scripts._sanitize_shipped_cache_paths import (
        sanitize_shipped_cache_paths,
    )

    _step("Sanitising dev paths in shipped .cvfr_routemaster/ JSONs")
    report = sanitize_shipped_cache_paths(RELEASE_DIR)
    if not report.updates and not report.skipped:
        print("  no absolute paths to sanitise (cache already clean)")
    for cache_file, fields in report.updates.items():
        print(f"  {cache_file}:")
        for json_path, before, after in fields:
            print(f"    {json_path}: {before} -> {after}")
    if report.skipped:
        print(f"  skipped (unreadable/malformed): {', '.join(report.skipped)}")
    if report.updates:
        print(f"  total fields sanitised: {report.total_fields_updated()}")


def _copy_seed_cache() -> None:
    """Copy the per-mode seed cache JSONs into the release.

    Symmetric with the Windows build's ``_copy_seed_cache`` — see
    that docstring (in ``scripts/build_release.py``) for the full
    rationale. v4 ships each chart product's calibration / altitude /
    waypoint / map-images caches under ``.cvfr_routemaster/<mode_id>/``
    plus a ``.v4_migrated`` marker. Rendered PNGs and downloaded
    ``charts/`` PDFs are not shipped (derivative CAAI material).
    """
    _step("Copying per-mode seed .cvfr_routemaster/ caches into release-linux/")
    root = RELEASE_DIR / ".cvfr_routemaster"
    root.mkdir()
    grand_copied = 0
    for mode_id in _seed_mode_ids():
        src_dir = DEV_CACHE_DIR / mode_id
        dst_dir = root / mode_id
        dst_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        skipped: list[str] = []
        for name in CACHE_FILES:
            src = src_dir / name
            if not src.is_file():
                skipped.append(name)
                continue
            dst = dst_dir / name
            shutil.copy2(src, dst)
            size_kb = dst.stat().st_size / 1024
            print(f"  {mode_id}/{name} ({size_kb:,.1f} KiB)")
            copied += 1
            grand_copied += 1
        print(f"  [{mode_id}] copied {copied}/{len(CACHE_FILES)} cache files")
        if skipped:
            print(
                f"  [{mode_id}] skipped (not present in dev cache): "
                f"{', '.join(skipped)}\n"
                "  ↪ these are optional; the user's first launch will "
                "regenerate them transparently."
            )

    marker = root / V4_MIGRATION_MARKER
    marker.write_text("v4\n", encoding="utf-8")
    print(f"  wrote {V4_MIGRATION_MARKER} (release ships v4-namespaced seeds)")
    print(f"  total: {grand_copied} cache files across {len(_seed_mode_ids())} modes")


def _write_desktop_entry_template() -> None:
    """Write a Desktop Entry template + a tiny installer script.

    The .desktop file uses ``${INSTALL_DIR}`` placeholders rather
    than absolute paths because PyInstaller doesn't know where the
    user will eventually extract the release tarball. The installer
    script (``install-shortcut.sh``) substitutes the placeholder
    with the directory it's run from, copies the result to the
    user's local applications dir, copies the icon to the local
    icons dir, and refreshes the desktop database — one command,
    no editing required.
    """
    _step("Writing .desktop entry + install-shortcut.sh")

    desktop = RELEASE_DIR / "cvfr-routemaster.desktop"
    desktop.write_text(
        # The ``${INSTALL_DIR}`` token is replaced by install-shortcut.sh.
        # ``Categories=Utility;Education;`` lets the entry appear under
        # both menus — Education is where Linux distros typically file
        # flight-sim and aviation tools.
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=CVFR Route Master\n"
        "GenericName=VFR Route Planner\n"
        "Comment=Plan VFR routes on Israeli CVFR charts\n"
        "Exec=${INSTALL_DIR}/cvfr-routemaster\n"
        "Icon=${INSTALL_DIR}/icon.png\n"
        "Path=${INSTALL_DIR}\n"
        "Terminal=false\n"
        "Categories=Utility;Education;\n"
        "Keywords=aviation;flight;vfr;route;chart;israel;\n"
        "StartupNotify=true\n",
        encoding="utf-8",
    )
    print(f"  {desktop.relative_to(REPO_ROOT)}")

    installer = RELEASE_DIR / "install-shortcut.sh"
    installer.write_text(
        # POSIX sh, not bash — works on every Debian-derivative install
        # without the "bash not found" failure mode.
        # Strategy: this script runs from the same directory as the
        # extracted release; ``$(cd "$(dirname "$0")" && pwd)`` resolves
        # that absolute path even if the user invoked the script via
        # a relative path or symlink.
        "#!/bin/sh\n"
        "# Install a system-menu launcher for CVFR Route Master.\n"
        "#\n"
        "# Run once after extracting the release:\n"
        "#\n"
        "#     cd /path/to/release-linux\n"
        "#     ./install-shortcut.sh\n"
        "#\n"
        "# Idempotent: re-running just refreshes the entry to point at\n"
        "# the current extraction path (handy if you move the folder).\n"
        "set -e\n"
        "\n"
        "INSTALL_DIR=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
        "APPS_DIR=\"$HOME/.local/share/applications\"\n"
        "ICON_DIR=\"$HOME/.local/share/icons\"\n"
        "DESKTOP_FILE=\"$APPS_DIR/cvfr-routemaster.desktop\"\n"
        "\n"
        "mkdir -p \"$APPS_DIR\" \"$ICON_DIR\"\n"
        "\n"
        "# Substitute ${INSTALL_DIR} into the template using sed; using\n"
        "# a delimiter other than / so paths containing / don't need\n"
        "# escaping (paths are guaranteed not to contain |).\n"
        "sed \"s|\\${INSTALL_DIR}|$INSTALL_DIR|g\" \\\n"
        "    \"$INSTALL_DIR/cvfr-routemaster.desktop\" > \"$DESKTOP_FILE\"\n"
        "chmod 644 \"$DESKTOP_FILE\"\n"
        "\n"
        "# Copy the icon to ~/.local/share/icons/ so desktop environments\n"
        "# that look for icons there (XFCE, some GNOME setups) find it\n"
        "# without needing the absolute Icon= path. The .desktop file\n"
        "# uses an absolute path so this copy is belt-and-braces.\n"
        "cp \"$INSTALL_DIR/icon.png\" \"$ICON_DIR/cvfr-routemaster.png\"\n"
        "\n"
        "# Refresh the desktop database if the tool is available; not\n"
        "# fatal if it's not (the entry still appears, just maybe after\n"
        "# the next session restart).\n"
        "if command -v update-desktop-database >/dev/null 2>&1; then\n"
        "    update-desktop-database \"$APPS_DIR\" 2>/dev/null || true\n"
        "fi\n"
        "\n"
        "echo \"Installed launcher: $DESKTOP_FILE\"\n"
        "echo \"  Exec: $INSTALL_DIR/cvfr-routemaster\"\n"
        "echo \"  Icon: $INSTALL_DIR/icon.png\"\n"
        "echo \"\"\n"
        "echo \"Look for 'CVFR Route Master' in your application menu\"\n"
        "echo \"under Utility or Education. May take a session restart\"\n"
        "echo \"on some desktop environments.\"\n",
        encoding="utf-8",
    )
    cur = installer.stat().st_mode
    installer.chmod(cur | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  {installer.relative_to(REPO_ROOT)} (executable)")


def _write_check_runtime_deps_script() -> None:
    """Emit ``release-linux/check-runtime-deps.sh``.

    A diagnostic helper the user runs on the *target* Debian box (not the
    build machine) to verify every Qt runtime library and the optional
    Tesseract binaries are present *before* trying to launch the app.
    The motivation is the failure mode where ``./cvfr-routemaster``
    silently exits with no visible error because Qt's platform-plugin
    init fails — by which point the user has nothing to debug from.

    The script:
      * Probes each required ``lib*.so.*`` file via ``ldconfig -p``
        (works without dpkg, so it survives manual installs / containers).
      * Falls back to ``dpkg -s`` when ``ldconfig`` doesn't list the
        soname (rare but happens for fresh installs before
        ``ldconfig`` has refreshed its cache).
      * Probes Tesseract via ``which`` and ``tesseract --list-langs``
        to catch the partial-install case (binary present but ``-heb``
        missing, which is the most common Tesseract failure mode for
        this app because back-pages text is Hebrew).
      * Prints a single, copy-pasteable ``sudo apt install`` line for
        whatever was missing — so the user's remediation is one command
        regardless of how many libs are absent.

    POSIX sh, not bash — same reasoning as install-shortcut.sh: works
    on every Debian-derivative install without depending on bash being
    present in /bin.
    """
    _step("Writing release-linux/check-runtime-deps.sh")
    out = RELEASE_DIR / "check-runtime-deps.sh"

    qt_probe_lines = "\n".join(
        f'check_lib "{pkg}" "{soname}"' for pkg, soname in RUNTIME_LIB_PROBES
    )
    qt_font_pkg = "fonts-dejavu-core"
    ocr_pkgs = " ".join(RUNTIME_OCR_APT_PACKAGES)
    qt_pkgs_for_apt = " ".join(RUNTIME_QT_APT_PACKAGES)

    out.write_text(
        "#!/bin/sh\n"
        "# Sanity-check that this Debian box has the runtime libraries\n"
        "# CVFR Route Master needs. Run BEFORE first launch on a new box.\n"
        "#\n"
        "#     ./check-runtime-deps.sh\n"
        "#\n"
        "# Exit codes:\n"
        "#   0 = everything is present, you can run ./cvfr-routemaster\n"
        "#   1 = one or more Qt runtime libs missing — script prints the\n"
        "#       exact `sudo apt install ...` command to fix it\n"
        "#   2 = Tesseract is missing or the Hebrew language pack is\n"
        "#       absent — fine to ignore unless you plan to re-OCR a\n"
        "#       new chart cycle's back-pages\n"
        "#\n"
        "# Why this script exists: when Qt can't find a platform plugin\n"
        "# at startup the binary aborts silently (no visible window, no\n"
        "# stderr unless you launched it from a terminal). This script\n"
        "# catches the missing-libs case before that failure mode.\n"
        "set -u\n"
        "\n"
        "MISSING_QT=\"\"\n"
        "MISSING_OCR=\"\"\n"
        "RC=0\n"
        "\n"
        "have_lib() {\n"
        "    # Probe via ldconfig -p (the dynamic linker's view of the\n"
        "    # installed shared libraries). More reliable than dpkg -s\n"
        "    # because it doesn't depend on dpkg's database being\n"
        "    # accurate (Snap / Flatpak / manual installs still register\n"
        "    # in the linker cache).\n"
        "    soname=\"$1\"\n"
        "    if ldconfig -p 2>/dev/null | grep -q \"$soname\"; then\n"
        "        return 0\n"
        "    fi\n"
        "    return 1\n"
        "}\n"
        "\n"
        "check_lib() {\n"
        "    pkg=\"$1\"\n"
        "    soname=\"$2\"\n"
        "    if have_lib \"$soname\"; then\n"
        "        printf '  [ OK ]  %-22s (%s)\\n' \"$pkg\" \"$soname\"\n"
        "    else\n"
        "        printf '  [MISS]  %-22s (%s)\\n' \"$pkg\" \"$soname\"\n"
        "        MISSING_QT=\"$MISSING_QT $pkg\"\n"
        "        RC=1\n"
        "    fi\n"
        "}\n"
        "\n"
        "echo 'Checking Qt runtime libraries...'\n"
        f"{qt_probe_lines}\n"
        "\n"
        "# fonts-dejavu-core is a font package, not a shared library, so\n"
        "# it doesn't show up in ldconfig. Probe via dpkg-query, falling\n"
        "# back to checking for /usr/share/fonts/truetype/dejavu/ which\n"
        "# is what the package installs.\n"
        f"if dpkg-query -W -f='${{Status}}' {qt_font_pkg} 2>/dev/null \\\n"
        "        | grep -q 'install ok installed'; then\n"
        f"    printf '  [ OK ]  %-22s (font set)\\n' '{qt_font_pkg}'\n"
        "elif [ -d /usr/share/fonts/truetype/dejavu ]; then\n"
        f"    printf '  [ OK ]  %-22s (DejaVu fonts present, package not via dpkg)\\n' '{qt_font_pkg}'\n"
        "else\n"
        f"    printf '  [MISS]  %-22s (DejaVu fonts not installed)\\n' '{qt_font_pkg}'\n"
        f"    MISSING_QT=\"$MISSING_QT {qt_font_pkg}\"\n"
        "    RC=1\n"
        "fi\n"
        "\n"
        "echo\n"
        "echo 'Checking Tesseract OCR (only needed if you re-OCR a new chart cycle)...'\n"
        "if command -v tesseract >/dev/null 2>&1; then\n"
        "    printf '  [ OK ]  tesseract (%s)\\n' \"$(tesseract --version 2>&1 | head -1)\"\n"
        "    # Hebrew language pack is the one specifically needed for\n"
        "    # back-pages OCR. Tesseract starts and runs without it but\n"
        "    # returns garbage on Hebrew text — confusing failure mode\n"
        "    # we want to surface here, not at OCR time.\n"
        "    LANGS=$(tesseract --list-langs 2>&1 | tail -n +2)\n"
        "    if printf '%s\\n' \"$LANGS\" | grep -qx 'heb'; then\n"
        "        echo '  [ OK ]  Hebrew language pack (heb)'\n"
        "    else\n"
        "        echo '  [MISS]  Hebrew language pack (heb) — back-pages OCR will fail'\n"
        "        MISSING_OCR='tesseract-ocr-heb'\n"
        "        [ \"$RC\" -eq 0 ] && RC=2\n"
        "    fi\n"
        "    if printf '%s\\n' \"$LANGS\" | grep -qx 'eng'; then\n"
        "        echo '  [ OK ]  English language pack (eng)'\n"
        "    else\n"
        "        echo '  [MISS]  English language pack (eng)'\n"
        "        MISSING_OCR=\"$MISSING_OCR tesseract-ocr-eng\"\n"
        "        [ \"$RC\" -eq 0 ] && RC=2\n"
        "    fi\n"
        "else\n"
        "    echo '  [MISS]  tesseract not installed'\n"
        f"    MISSING_OCR='{ocr_pkgs}'\n"
        "    [ \"$RC\" -eq 0 ] && RC=2\n"
        "fi\n"
        "\n"
        "echo\n"
        "if [ -z \"$MISSING_QT\" ] && [ -z \"$MISSING_OCR\" ]; then\n"
        "    echo 'All runtime dependencies satisfied. You can run ./cvfr-routemaster.'\n"
        "    exit 0\n"
        "fi\n"
        "\n"
        "if [ -n \"$MISSING_QT\" ]; then\n"
        "    echo 'MISSING Qt runtime libraries (the binary will not start without these):'\n"
        "    echo\n"
        "    echo \"    sudo apt install --no-install-recommends$MISSING_QT\"\n"
        "    echo\n"
        "fi\n"
        "if [ -n \"$MISSING_OCR\" ]; then\n"
        "    echo 'MISSING Tesseract (only needed if you re-OCR a new chart cycle):'\n"
        "    echo\n"
        "    echo \"    sudo apt install --no-install-recommends $MISSING_OCR\"\n"
        "    echo\n"
        "fi\n"
        "exit $RC\n",
        encoding="utf-8",
    )
    cur = out.stat().st_mode
    out.chmod(cur | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  {out.relative_to(REPO_ROOT)} (executable)")
    print(
        f"  Runtime probe covers {len(RUNTIME_LIB_PROBES)} shared libs "
        f"+ DejaVu fonts + Tesseract (eng + heb)"
    )


def _write_wsl_launcher_script() -> None:
    """Emit ``release-linux/run-on-wsl.sh``.

    A thin wrapper around ``./cvfr-routemaster`` that detects WSL and
    forces ``QT_QPA_PLATFORM=xcb`` so Qt routes through WSLg's X11
    bridge instead of its Wayland compositor (Weston). On native
    Linux the wrapper is a transparent passthrough — it does NOT
    override Qt's platform-plugin choice, so a bare-metal Debian /
    Ubuntu desktop keeps the full Wayland + GPU compositing path.

    Why this exists
    ---------------

    Under WSL2 + WSLg, Qt apps connect to Weston (WSLg's bundled
    Wayland compositor running inside the WSL system-distro VM).
    Weston intermittently wedges into a software "copy mode"
    rendering state on startup — symptom is the application window
    never appearing, just a tiny taskbar entry whose title is
    prefixed with ``[WARN: COPY MODE]`` (Weston is literally
    advertising the degraded state in the title bar). The process
    runs normally inside; only the surface composition is broken.
    This is a known Microsoft/WSL issue:

      * microsoft/wslg#972  ("After each reboot [WARN: COPY-MODE]
        happens, many program fails to start")
      * microsoft/wslg#1278 ("WARN: COPY_MODE on window titles")
      * microsoft/WSL#12616 ("WSLg, Wayland, Ubuntu 24.04, PyQt6")

    The reliable application-side workaround documented across
    those threads is to bypass Wayland by forcing the xcb
    (X11) platform plugin. WSLg also runs an X server (XWayland
    backed by the same Weston instance, but the X11 surface path
    doesn't hit the copy-mode bug). Setting ``QT_QPA_PLATFORM=xcb``
    routes the window through that path and the GUI appears
    immediately.

    Why detect WSL instead of forcing xcb unconditionally
    ------------------------------------------------------

    On bare-metal Linux there is no Weston, no copy-mode failure,
    and no benefit to dropping out of Wayland — the native
    compositor (Mutter / KWin / Sway / etc.) does full
    GPU-accelerated compositing and ``QT_QPA_PLATFORM=xcb`` would
    silently degrade that to legacy X11. So the wrapper checks
    ``/proc/version`` for the Microsoft kernel signature and only
    overrides the platform plugin in the WSL case. Friends running
    the release on real Debian / Ubuntu boxes get the native path
    untouched.

    POSIX sh, not bash — same reasoning as ``install-shortcut.sh``
    and ``check-runtime-deps.sh``: works on every Debian-derivative
    without depending on /bin/bash being present.
    """
    _step("Writing release-linux/run-on-wsl.sh")
    out = RELEASE_DIR / "run-on-wsl.sh"

    out.write_text(
        "#!/bin/sh\n"
        "# Launch CVFR Route Master with the WSLg-friendly Qt platform.\n"
        "#\n"
        "# Background: under WSL2, Qt apps go through WSLg's Weston\n"
        "# Wayland compositor, which intermittently wedges into\n"
        "# '[WARN: COPY MODE]' state on launch -- symptom is a tiny\n"
        "# taskbar entry but no visible window, even though the process\n"
        "# is running fine. This is a known Microsoft/WSL issue\n"
        "# (microsoft/wslg #972 / #1278; microsoft/WSL #12616), not\n"
        "# anything in CVFR Route Master itself.\n"
        "#\n"
        "# The reliable workaround is to route Qt through WSLg's X11\n"
        "# bridge instead of Wayland by setting QT_QPA_PLATFORM=xcb.\n"
        "# This wrapper does that only when it detects WSL via\n"
        "# /proc/version; on native Linux it leaves Qt's platform\n"
        "# auto-detect alone so a bare-metal Debian / Ubuntu desktop\n"
        "# keeps the full native-Wayland path with GPU compositing.\n"
        "#\n"
        "# Use this wrapper on WSL only. On native Linux you can just\n"
        "# run ./cvfr-routemaster directly -- this wrapper is a no-op\n"
        "# on bare metal anyway, but the friend-facing release tarball\n"
        "# omits it to keep the install footprint to the ELF + assets.\n"
        "set -u\n"
        "\n"
        "SELF_DIR=$(cd \"$(dirname \"$0\")\" && pwd)\n"
        "BIN=\"$SELF_DIR/cvfr-routemaster\"\n"
        "\n"
        "if [ ! -x \"$BIN\" ]; then\n"
        "    echo \"ERROR: $BIN not found or not executable.\" >&2\n"
        "    echo \"       Run this wrapper from the extracted release-linux/ folder.\" >&2\n"
        "    exit 127\n"
        "fi\n"
        "\n"
        "# Detect WSL by inspecting /proc/version. Microsoft's WSL\n"
        "# kernels always advertise themselves with 'microsoft' (WSL1\n"
        "# + WSL2 since 2019) or 'WSL' in the kernel release string --\n"
        "# see https://learn.microsoft.com/en-us/windows/wsl/faq for\n"
        "# the documented detection contract. We grep both spellings\n"
        "# case-insensitively to cover any future Microsoft kernel\n"
        "# rebrand without re-shipping the wrapper.\n"
        "if [ -r /proc/version ] && grep -qiE 'microsoft|wsl' /proc/version; then\n"
        "    export QT_QPA_PLATFORM=xcb\n"
        "    echo \"WSL detected -- setting QT_QPA_PLATFORM=xcb to bypass WSLg's Weston compositor.\"\n"
        "    echo \"(On native Linux this wrapper is a no-op; run ./cvfr-routemaster directly there.)\"\n"
        "fi\n"
        "\n"
        "exec \"$BIN\" \"$@\"\n",
        encoding="utf-8",
        # newline="\n" forces LF-only line endings even when this
        # script is invoked from a Windows Python interpreter (e.g.
        # a one-shot ``python -c "..."`` to regenerate the wrapper
        # from PowerShell). Without it, Python's text-mode write
        # translates ``\n`` to ``\r\n`` on Windows, and bash on the
        # WSL side then reads the shebang as ``#!/bin/sh\r`` and
        # aborts with "cannot execute: required file not found"
        # (the kernel literally tries to exec an interpreter named
        # ``/bin/sh\r`` which doesn't exist). The build pipeline is
        # only supposed to run on Linux, but a regenerate-one-file
        # convenience path on Windows is plausible enough to defend.
        newline="\n",
    )
    cur = out.stat().st_mode
    out.chmod(cur | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  {out.relative_to(REPO_ROOT)} (executable)")


def _write_readme() -> None:
    _step("Writing release-linux/README.txt")
    readme_path = RELEASE_DIR / "README.txt"

    # Produce the apt-install command lines from the same Python lists
    # used by check-runtime-deps.sh — keeps the README and the helper
    # script from drifting apart. ``textwrap``-style line wrapping is
    # done by hand (with explicit \ continuations) because shell
    # heredocs preserve whitespace literally and we want the README
    # to look the same as what the user would copy-paste into a terminal.
    qt_apt_lines = "\n".join(
        f"           {pkg} \\" if i < len(RUNTIME_QT_APT_PACKAGES) - 1
        else f"           {pkg}"
        for i, pkg in enumerate(RUNTIME_QT_APT_PACKAGES)
    )
    ocr_apt_line = " ".join(RUNTIME_OCR_APT_PACKAGES)

    readme_path.write_text(
        # ASCII-only for the same reason the Windows README is — avoids
        # mojibake on whichever pager / text editor the user happens
        # to open this with.
        "CVFR Route Master (Linux)\n"
        "=========================\n\n"
        "Target: Debian 13 (Trixie) and derivatives. Should also work\n"
        "on Ubuntu 24.04+, Mint 22+, and other distros built on glibc\n"
        ">= 2.40 (which is what Debian 13 ships).\n\n"
        "Quick start\n"
        "-----------\n"
        "1. Extract this folder somewhere user-writable (e.g. your home\n"
        "   directory). Don't put it in /opt or /usr/local -- the app\n"
        "   writes small cache files next to itself.\n\n"
        "2. Run the runtime-deps check:\n\n"
        "       ./check-runtime-deps.sh\n\n"
        "   It enumerates which Qt libraries and (optional) Tesseract\n"
        "   packages are missing, and prints the exact `sudo apt install`\n"
        "   commands to fix them. On a typical Debian 13 desktop install\n"
        "   most are already present; the most common gap is libxcb-cursor0\n"
        "   (a Qt6-specific dep that older Debian setups didn't have).\n\n"
        "3. Install the runtime libs the check script flagged. If you\n"
        "   skipped step 2 and want the canonical lists upfront:\n\n"
        "   Required (the binary will not start without these):\n\n"
        "       sudo apt install --no-install-recommends \\\n"
        f"{qt_apt_lines}\n\n"
        "   Optional, only when re-OCRing a new chart cycle's back-pages:\n\n"
        f"       sudo apt install --no-install-recommends {ocr_apt_line}\n\n"
        "   The shipped waypoint cache (.cvfr_routemaster/waypoints_cache.json)\n"
        "   means Tesseract isn't needed at first launch -- only when you\n"
        "   swap in a newer chart cycle. The app surfaces the apt command\n"
        "   in a dialog if it ever needs OCR and Tesseract is absent.\n\n"
        "4. Run the app:\n\n"
        "       cd ~/cvfr-routemaster   # or wherever you extracted\n"
        "       ./cvfr-routemaster\n\n"
        "   Startup takes 15-25 seconds the first time as PyInstaller\n"
        "   self-extracts the bundled Qt + Python tree (~450 MiB) to /tmp/.\n"
        "   Subsequent launches are faster because /tmp/ is tmpfs (RAM)\n"
        "   on most distros.\n\n"
        "5. (Optional) Add a launcher to your desktop's app menu:\n\n"
        "       ./install-shortcut.sh\n\n"
        "   This adds 'CVFR Route Master' to the application menu under\n"
        "   Utility / Education and gives you a clickable launcher with\n"
        "   the proper icon. Idempotent -- re-run any time you move the\n"
        "   folder.\n\n"
        "What's in this folder\n"
        "---------------------\n"
        "  cvfr-routemaster              The application itself (ELF binary,\n"
        "                                ~286 MiB -- bundles Qt + Python).\n"
        "  icon.png                      256x256 launcher icon.\n"
        "  cvfr-routemaster.desktop      Desktop Entry template (used by\n"
        "                                install-shortcut.sh).\n"
        "  install-shortcut.sh           One-shot script to add the app\n"
        "                                to your system menu.\n"
        "  check-runtime-deps.sh         Verifies the target box has the\n"
        "                                Qt runtime libs + Tesseract before\n"
        "                                you try to launch.\n"
        "  README.txt                    This file.\n"
        "  .cvfr_routemaster/            Calibration + cached chart data\n"
        "                                + the default CAAI chart URLs for\n"
        "                                each chart product (CVFR and LSA),\n"
        "                                one folder per product. Hidden from\n"
        "                                `ls` by default but must travel with\n"
        "                                the binary. The downloaded chart PDFs\n"
        "                                will also live under\n"
        "                                .cvfr_routemaster/<product>/charts/\n"
        "                                after first launch.\n\n"
        "This program ships two Israeli chart products: CVFR (the default)\n"
        "and LSA. Use the toolbar's chart-type toggle to switch between them;\n"
        "each downloads its own PDFs the first time you open it and keeps its\n"
        "own cached charts and calibration.\n\n"
        "First launch (downloads CVFR charts)\n"
        "------------------------------------\n"
        "The Israeli CVFR chart PDFs are NOT shipped with this program --\n"
        "Israeli government terms of use prohibit redistribution. On first\n"
        "launch the program downloads the three current CVFR PDFs (north,\n"
        "south, back-pages) directly from the Civil Aviation Authority of\n"
        "Israel's website. The three default URLs are already filled in\n"
        "for you in Settings... -> Map File Settings -- just click 'Load\n"
        "maps & waypoints now' the first time and a progress dialog will\n"
        "fetch each PDF (about 3-5 MiB each, ~30 seconds on a normal\n"
        "internet connection). The downloaded PDFs are cached locally\n"
        "and reused on every subsequent launch -- no further network\n"
        "calls.\n\n"
        "If a download fails (firewall, no internet, etc.) a dialog will\n"
        "appear with the URL and a Retry button. You can also click 'Open\n"
        "Map File Settings...' to switch to a different URL or to a local\n"
        "PDF path you already have on disk.\n\n"
        "After the first-launch download finishes, the maps will line up\n"
        "with their geographic coordinates with no calibration on your\n"
        "part -- the calibration that came with this folder is reused.\n\n"
        "Updating to a new chart cycle\n"
        "-----------------------------\n"
        "When CAAI publishes a new CVFR edition, this program will need a\n"
        "release update -- the calibration is built against a specific\n"
        "edition. If you want to use a newer PDF before the program is\n"
        "updated, drop the PDF on disk and paste its full path into the\n"
        "matching field in Map File Settings (or update the URL field).\n"
        "The app will detect the change, drop its caches for the affected\n"
        "sheets, re-render the maps, and re-OCR the waypoint table (takes\n"
        "a few minutes one time, and is when you'll need Tesseract\n"
        "installed -- see step 3 above).\n\n"
        "If something goes wrong\n"
        "-----------------------\n"
        "* If ./cvfr-routemaster exits silently with no visible window: run\n"
        "  ./check-runtime-deps.sh first; the most common cause is a missing\n"
        "  Qt runtime library (libxcb-cursor0 in particular).\n"
        "* If a chart download fails repeatedly: the error dialog shows the\n"
        "  URL it's trying to fetch. Open the URL in your web browser to\n"
        "  confirm CAAI is reachable from your network. If you can download\n"
        "  the PDF in the browser but not from the program, save the PDF to\n"
        "  disk and paste the full path into Map File Settings for that\n"
        "  sheet instead.\n"
        "* If you see 'Tesseract OCR not found', run the optional apt install\n"
        "  command in step 3 above. The app will work normally on the next\n"
        "  launch (no need to reinstall the release).\n"
        "* If the maps load but coordinates look wrong: the calibration\n"
        "  files in .cvfr_routemaster/ were left out of the tarball. Ask\n"
        "  the sender for the .cvfr_routemaster folder.\n"
        "* If ./cvfr-routemaster says 'Permission denied': the executable\n"
        "  bit got stripped during transfer. Run: chmod +x cvfr-routemaster\n"
        "  ./check-runtime-deps.sh   ./install-shortcut.sh\n"
        "* If the app still refuses to launch and check-runtime-deps.sh says\n"
        "  everything's OK, run it from a terminal so you see PyInstaller's\n"
        "  stderr output and the Qt platform plugin diagnostics:\n\n"
        "       QT_DEBUG_PLUGINS=1 ./cvfr-routemaster\n",
        encoding="utf-8",
    )
    print(f"  {readme_path.relative_to(REPO_ROOT)}")


def _summary() -> None:
    _step("Summary")
    total = 0
    files = 0
    for p in RELEASE_DIR.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
            files += 1
    print(
        f"  release-linux/ contains {files} files, {total / (1024 * 1024):.1f} MiB total\n"
        f"  Next steps:\n"
        f"    1. Tar the *contents* of release-linux/ (NOT the\n"
        f"       release-linux/ folder itself) and ship to the target\n"
        f"       Debian box, e.g.:\n"
        f"         tar -czf cvfr-routemaster-linux.tar.gz -C release-linux .\n"
        f"    2. On the target box, after extracting, run\n"
        f"       ./check-runtime-deps.sh first -- it enumerates which\n"
        f"       Qt runtime apt packages are missing and prints the exact\n"
        f"       `sudo apt install ...` command (the most common gap is\n"
        f"       libxcb-cursor0).\n"
        f"    3. Optionally `sudo apt install tesseract-ocr\n"
        f"       tesseract-ocr-eng tesseract-ocr-heb` if the user plans\n"
        f"       to re-OCR a new chart cycle's back-pages.\n"
        f"    4. Then ./cvfr-routemaster (or ./install-shortcut.sh first\n"
        f"       to add a system menu launcher)."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--skip-pyinstaller",
        action="store_true",
        help="Skip the (slow) PyInstaller step; iterate on payload-copy logic faster.",
    )
    parser.add_argument(
        "--skip-icon",
        action="store_true",
        help="Don't regenerate icon.png; use whatever is already in release-linux/.",
    )
    args = parser.parse_args()

    _check_prerequisites()
    if not args.skip_icon:
        _regenerate_icon()
    _clean_release_dir()
    if not args.skip_pyinstaller:
        _run_pyinstaller()
        # Scan BEFORE the copy: a flagged warn-file means the binary
        # would crash at launch, and there's no value in copying the
        # broken ELF into release-linux/ just to be told moments
        # later that the build is rejected.
        _scan_pyinstaller_warnings()
        _copy_exe()
    else:
        print("\n[--skip-pyinstaller] PyInstaller step skipped; binary not refreshed.")
    _copy_seed_cache()
    # Must run AFTER _copy_seed_cache (needs the cache JSONs to
    # mutate AND write_shipped_map_layout reads the shipped
    # geo_calibration.json + map_images_meta.json).
    _write_shipped_derived_files()
    # Must run LAST because it rewrites the same JSON files that
    # _write_shipped_derived_files emits — running it earlier
    # would leave the dev's absolute paths in the derived JSONs.
    _sanitize_shipped_cache_paths()
    _write_desktop_entry_template()
    _write_check_runtime_deps_script()
    _write_wsl_launcher_script()
    _write_readme()
    _summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
