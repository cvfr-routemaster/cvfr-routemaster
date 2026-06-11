"""End-to-end builder for the CVFR Route Master Windows release folder.

Runs the full pipeline that produces ``release/`` ready to ship to a
friend on Windows 11:

    release/
    ├── cvfr-routemaster.exe              ← single-file PyInstaller build
    ├── icon.ico                          ← compass/route icon (also baked into the .exe)
    ├── README.txt                        ← friend-facing one-pager
    ├── LICENSE                           ← AGPLv3 + copyright-holder block
    ├── tesseract/                        ← slim Tesseract OCR engine for back-pages
    │   ├── tesseract.exe                 ← OCR binary
    │   ├── *.dll                         ← runtime DLL deps
    │   └── tessdata/
    │       ├── eng.traineddata
    │       └── heb.traineddata
    ├── source/                           ← AGPLv3 §6(a) source bundle
    │   └── cvfr-routemaster-source.zip   ← runnable source (py -m cvfr_routemaster)
    └── .cvfr_routemaster/                ← seed cache copied from your dev caches
        ├── .v4_migrated                  ← marks the tree as already v4-namespaced
        ├── font_settings.json            ← shipped font sizing defaults (GLOBAL,
        │                                   shared across all chart products)
        ├── cvfr/                         ← CVFR mode namespace
        │   ├── chart_sources.json        ← default CAAI URLs; first-launch targets
        │   ├── map_layout.json           ← shipped north/south layout defaults
        │   ├── geo_calibration.json
        │   ├── altitude_arrows_north.json
        │   ├── altitude_arrows_south.json
        │   ├── map_images_meta.json
        │   └── waypoints_cache.json
        └── lsa/                          ← LSA mode namespace (same JSON set)
            └── ...

**Map PDFs are no longer shipped.** Israeli AIP terms forbid
redistribution of the CVFR sheets, so v3.3+ releases ship the
default CAAI URLs in ``chart_sources.json`` and the application
downloads the three PDFs (north / south / back) on first launch
into ``.cvfr_routemaster/charts/`` under the user's release
folder. Rendered map PNGs are similarly not shipped — those are
derivative works of the AIP material. Both render on demand from
the freshly-downloaded PDFs the first time the user opens the
app.

Why subfolders for ``tesseract/``? The release root is the first
thing the friend sees on extraction; keeping it to three items
(the .exe, the icon, and the README) makes it obvious what to
double-click. Bulky payloads — the OCR engine — live in
clearly-named subfolders.

Why a SLIM Tesseract bundle? The full UB Mannheim install
(``vendor/tesseract/``) is ~239 MiB across 132 files: the 99 MiB
``libtesseract-5.dll`` is unavoidable (it IS the OCR engine), but
the install also ships a dozen training utilities (``lstmtraining``,
``text2image``, etc.), HTML man pages, an OSD/script-detection
language model, and Java-based GUI tools. None of those run from the
back-pages OCR path, so we copy only the OCR-runtime subset:
``tesseract.exe`` + every ``*.dll`` + ``tessdata/eng.traineddata``
+ ``tessdata/heb.traineddata``. That trims ~72 MiB.

Pipeline:

    1. Sanity-check prerequisites (3 dev PDFs at repo root for
       calibration verification + calibration JSON +
       vendor/tesseract/tesseract.exe all exist). The PDFs are
       required at the dev repo root because the cookbook's
       step-0.5 calibration check launches the dev build against
       the freshly-downloaded defaults; we don't COPY them into
       the release any more.
    2. Generate / refresh the icon (skipped if up-to-date).
    3. Wipe and recreate ``release/`` (everything except the icon,
       which we just regenerated).
    4. Invoke PyInstaller against ``cvfr-routemaster.spec``.
    5. Move the freshly-built .exe out of PyInstaller's ``dist/`` into
       ``release/``.
    6. Copy the slim Tesseract subset into ``release/tesseract/``.
    7. Copy the seed ``.cvfr_routemaster/`` cache into ``release/``.
    8. Bake ``chart_sources.json``, ``map_layout.json``, and
       ``font_settings.json`` into the seed cache (so first launch
       finds the CAAI URL defaults and the right pane layout).
    9. Sanitise absolute paths out of the seed cache JSONs (replace
       dev-machine absolute paths with bare basenames so no PII
       leaks in the shipped fingerprints).
    10. Build ``release/source/cvfr-routemaster-source.zip`` — the
        complete AGPLv3 §6(a) "Corresponding Source" bundle. Contains
        the ``cvfr_routemaster/`` package + ``requirements.txt`` +
        ``LICENSE`` + a generated ``README.txt`` explaining
        ``py -m cvfr_routemaster``. Tests, scripts, dev cache,
        vendored Tesseract, and design notes are intentionally
        excluded (not part of the program).
    11. Copy ``LICENSE`` from the repo root into ``release/`` next
        to the .exe. AGPLv3 §4 requires the license text accompany
        every copy of the binary; without this file, a recipient
        who unzips ``release/`` has no visible copy of the license
        their copy is governed by.
    12. Write ``release/README.txt``.
    13. Print a summary (folder size, file count, friend-facing
        "next steps" instructions).

Usage::

    python scripts/build_release.py

Optional flags::

    --skip-pyinstaller   Skip the PyInstaller invocation; useful when
                         iterating on the README / PDF-copy logic
                         without re-running the slow .exe build.
    --skip-icon          Don't regenerate the icon (use whatever is
                         already in ``release/icon.ico``; helpful
                         after a manual edit).

Run from the repo root.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = REPO_ROOT / "release"
DIST_DIR = REPO_ROOT / "dist"
BUILD_DIR = REPO_ROOT / "build"
SPEC_FILE = REPO_ROOT / "cvfr-routemaster.spec"
DEV_CACHE_DIR = REPO_ROOT / ".cvfr_routemaster"
DEV_TESSERACT_DIR = REPO_ROOT / "vendor" / "tesseract"

# The stem PyInstaller derives from ``SPEC_FILE`` for its per-build
# subdirectory under ``build/`` and for the warn-file name. Kept as
# its own constant so the warn-scan step stays correct if the spec
# file is ever renamed.
SPEC_STEM = "cvfr-routemaster"

# Top-level package the warn-scan filters importers by. A missing
# top-level import from anywhere under this prefix fails the build;
# misses from PIL / PySide6 / pytesseract are PyInstaller's normal
# noise (third-party libs have many optional top-level imports we
# don't care about).
APP_PACKAGE = "cvfr_routemaster"

# Subfolders inside the release root.
RELEASE_TESSERACT_SUBDIR = "tesseract"
RELEASE_SOURCE_SUBDIR = "source"

# The source-archive filename inside ``release/source/``. The
# ``SOURCE_BUNDLE_RELPATH`` constant in
# :mod:`cvfr_routemaster.program_info_dialog` references this
# same path (``source/cvfr-routemaster-source.zip``) so the
# Legal and Copyright Info dialog can point users at it; keep
# the two in sync.
SOURCE_ZIP_NAME = "cvfr-routemaster-source.zip"

# Top-level files at the repo root we copy into the source
# bundle alongside the package directory itself.
#
# - ``requirements.txt`` is required so a recipient can
#   ``pip install -r requirements.txt`` before running
#   ``py -m cvfr_routemaster``.
# - ``LICENSE`` is required by AGPLv3 §4 ("you must conspicuously
#   and appropriately publish on each copy an appropriate copyright
#   notice; keep intact all notices stating that this License [...]
#   apply to the code"). Without it, a recipient who unzips the
#   source bundle has no visible copy of the license their copy
#   is governed by; the per-file headers point at LICENSE, so the
#   pointer would dangle.
#
# Operator-facing notes inside the bundle (how to install
# dependencies, how to launch from source) live in a generated
# README.txt that the build writes from scratch (see
# ``_bundle_source_zip``).
SOURCE_BUNDLE_TOP_FILES: tuple[str, ...] = ("requirements.txt", "LICENSE")

# The chart PDFs are NOT shipped in the release (v3.3+: Israeli
# government terms of use prohibit redistribution of the CAAI charts).
# They're checked at build-prereq time — see ``_check_prerequisites`` —
# but from the per-mode runtime cache the app downloads into
# ``.cvfr_routemaster/<mode_id>/charts/`` rather than a fixed set of dev
# PDFs at the repo root, so a fresh dev checkout that calibrated via the
# normal download flow (no manually-placed PDFs) still builds, and the
# check naturally extends to every registered mode (CVFR + LSA).
#
# The runtime fetches PDFs from the CAAI URLs in the map-mode registry,
# which we also bake into the shipped ``chart_sources.json`` so a
# fresh-install user sees the URLs as defaults.

# Files inside ``.cvfr_routemaster/`` that are worth seeding into the
# release. Calibration is the critical one (without it the friend's
# first launch lands them in the calibration-instructions dialog);
# the rest are JSON caches that save the friend several minutes of
# cold-start work on first launch.
#
# ``waypoints_cache.json`` lets the app populate the waypoint table
# without invoking Tesseract; ``altitude_arrows_*.json`` skips the
# 3-5 minute altitude-arrow extraction; ``map_images_meta.json``
# skips the PDF→PNG render pass once the user has downloaded the
# charts.
#
# v3.3+ change: ``map_north.png`` and ``map_south.png`` are NO
# LONGER shipped. The render output is derived from the chart PDFs
# (which Israeli government terms of use prohibit redistribution
# of), so the rendered raster carries the same restriction. The
# friend's first chart-load triggers a one-time render against the
# just-downloaded PDFs (~30s on a modern CPU); subsequent launches
# read from the locally-built ``map_north.png`` / ``map_south.png``
# the runtime creates next to the shipped JSON metadata.
#
# Files are *optional*: any missing entry is skipped with a notice
# rather than failing the build, so a partial dev cache still
# produces a valid release.
CACHE_FILES: tuple[str, ...] = (
    "geo_calibration.json",
    "altitude_arrows_north.json",
    "altitude_arrows_south.json",
    "waypoints_cache.json",
    "map_images_meta.json",
)

# Traineddata files we ship — eng for English text, heb for the
# Hebrew name + reporting-type columns on the back-pages PDF.
# Everything else in vendor/tesseract/tessdata/ (osd.traineddata for
# orientation/script detection, the *.jar GUI tools, pdf.ttf for PDF
# generation) is unused by the back-pages OCR path and dropped.
TESSDATA_KEEP: tuple[str, ...] = ("eng.traineddata", "heb.traineddata")

EXE_NAME = "cvfr-routemaster.exe"

# Marker file the runtime drops once the one-time v3.3→v4 migration
# has run (see ``cvfr_routemaster.mode_migration.MARKER_FILENAME``).
# A v4 release already ships its seed under the per-mode namespaces,
# so we ship the marker too — that tells a fresh install "the layout
# is already v4, don't try to relocate non-existent flat caches".
V4_MIGRATION_MARKER = ".v4_migrated"


def _seed_mode_ids() -> tuple[str, ...]:
    """Mode ids whose seed caches the release bundles.

    Sourced from the live ``map_modes`` registry so registering a new
    chart product automatically extends the build (prereq check + seed
    copy + derived files) without a parallel edit here. Imported lazily
    — the build script otherwise avoids importing the package at module
    scope, and this keeps ``--help`` fast and import-error-free if the
    package can't load for an unrelated reason.
    """
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from cvfr_routemaster import map_modes
    finally:
        sys.path.pop(0)
    return map_modes.mode_ids()


def _step(msg: str) -> None:
    print(f"\n=== {msg} ===")


def _check_prerequisites() -> None:
    """Fail fast if the dev project is missing anything we'd need to
    copy. A friend who gets a release without the calibration JSON
    would see "Calibration required" on first launch and immediately
    be back to step zero — better to refuse to build than to ship
    a broken bundle."""
    _step("Checking prerequisites")
    missing: list[str] = []
    # Lazy import (same rationale as ``_seed_mode_ids``): keep the
    # package off the module-scope import path.
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from cvfr_routemaster import map_modes
        from cvfr_routemaster.chart_source import cache_path_for_sheet
    finally:
        sys.path.pop(0)
    # v4: each shipped chart product owns a per-mode cache namespace
    # under ``.cvfr_routemaster/<mode_id>/``. For every registered mode
    # require (a) the downloaded chart PDFs the dev calibrated against
    # and (b) a warm calibration, so a fresh install lands the recipient
    # on geo-referenced charts in whichever mode they open first (CVFR by
    # default, LSA on switch) without the "please re-calibrate" dialog.
    #
    # The chart PDFs live under ``.cvfr_routemaster/<mode_id>/charts/``
    # (downloaded by the app from CAAI; NOT shipped — redistribution
    # prohibited). Gated here because the shipped cache/calibration JSONs
    # are only meaningful against the exact bytes the dev calibrated
    # against; the runtime ``size`` fingerprint re-checks this against
    # the recipient's own download.
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
                "anchors so the friend inherits your calibration)"
            )
    tess_exe = DEV_TESSERACT_DIR / "tesseract.exe"
    if not tess_exe.is_file():
        missing.append(
            f"  - {tess_exe.relative_to(REPO_ROOT)} (run "
            "`py scripts/fetch_vendor_tesseract.py` once to download "
            "the UB Mannheim Tesseract installer + heb/eng "
            "traineddata into vendor/tesseract/)"
        )
    for td in TESSDATA_KEEP:
        f = DEV_TESSERACT_DIR / "tessdata" / td
        if not f.is_file():
            missing.append(
                f"  - {f.relative_to(REPO_ROOT)} (re-run "
                "`py scripts/fetch_vendor_tesseract.py --only-tessdata` "
                "to fetch the missing traineddata file)"
            )
    if missing:
        print("ERROR: missing prerequisites:", file=sys.stderr)
        for line in missing:
            print(line, file=sys.stderr)
        sys.exit(1)
    print("All prerequisites present.")


def _regenerate_icon() -> None:
    _step("Regenerating release/icon.ico")
    icon_script = REPO_ROOT / "scripts" / "generate_release_icon.py"
    subprocess.run(
        [sys.executable, str(icon_script)],
        cwd=str(REPO_ROOT),
        check=True,
    )


def _clean_release_dir() -> None:
    """Wipe ``release/`` to a known state — but preserve the freshly-
    regenerated ``icon.ico`` so the next step (PyInstaller) can find
    it. PyInstaller's own ``build/`` and ``dist/`` directories are
    also cleaned because stale artifacts there occasionally cause
    surprising behaviour ("you fixed the bug but PyInstaller
    re-used the old object")."""
    _step("Cleaning release/ build/ dist/")
    icon_path = RELEASE_DIR / "icon.ico"
    icon_bytes = icon_path.read_bytes() if icon_path.is_file() else None

    for d in (RELEASE_DIR, DIST_DIR, BUILD_DIR):
        if d.is_dir():
            shutil.rmtree(d)
            print(f"  removed {d.relative_to(REPO_ROOT)}/")

    RELEASE_DIR.mkdir()
    if icon_bytes is not None:
        icon_path.write_bytes(icon_bytes)
        print(f"  preserved icon.ico ({len(icon_bytes):,} bytes)")


def _run_pyinstaller() -> None:
    """Run PyInstaller with a small retry loop.

    Windows Defender (and most other real-time AV scanners) routinely
    grab a brief read-handle on every freshly-written executable to
    scan it. PyInstaller creates ``build/<name>/base_library.zip``
    and a few helper executables and then tries to overwrite or
    delete them seconds later, and if the AV scanner is mid-scan
    when that happens you get
    ``PermissionError: [WinError 32] The process cannot access the
    file because it is being used by another process``.

    The lock is transient — Defender typically releases within 1-3
    seconds — so a small retry with backoff sidesteps the race
    without needing the user to add an AV exclusion. We blow away
    PyInstaller's ``build/`` and ``dist/`` between attempts so the
    second try starts from a clean slate (a half-built ``build/``
    can sometimes also trip up the next attempt).
    """
    _step("Running PyInstaller (this takes 2-5 minutes)")
    cmd = [
        sys.executable,
        "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        str(SPEC_FILE),
    ]
    print("  $ " + " ".join(cmd))

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            subprocess.run(cmd, cwd=str(REPO_ROOT), check=True)
            print("PyInstaller finished.")
            return
        except subprocess.CalledProcessError as exc:
            if attempt == max_attempts:
                raise
            backoff_s = 5 * attempt
            print(
                f"\n  PyInstaller failed (attempt {attempt}/{max_attempts}, "
                f"exit {exc.returncode}) — likely Windows Defender holding "
                f"a build artifact. Waiting {backoff_s}s for the AV scanner "
                f"to release file locks, then cleaning ``build/``+``dist/`` "
                f"and retrying...",
                file=sys.stderr,
            )
            time.sleep(backoff_s)
            for d in (DIST_DIR, BUILD_DIR):
                if d.is_dir():
                    # Best-effort cleanup. If the directory itself is
                    # locked we just plough on — PyInstaller will write
                    # over what it can.
                    try:
                        shutil.rmtree(d)
                    except OSError:
                        pass


def _scan_pyinstaller_warnings() -> None:
    """Fail the build if PyInstaller's warn file lists missing
    top-level imports from inside the application package.

    This guard would have caught the Linux release v2 bug at build
    time: the WSL venv was assembled without numpy, PyInstaller
    flagged the unresolved
    ``cvfr_routemaster.map_crop -> numpy (top-level)`` import, and
    the build script (this one's Linux sibling) shipped a binary
    that crashed on first launch with ``ModuleNotFoundError``. The
    Windows build happens to work because numpy lands in the dev
    Python install as a transitive dep of something else — that's
    luck, not robustness, so the check belongs here too.

    See ``scripts/_pyinstaller_warnings.py`` for the parser and the
    filtering rationale (why non-top-level qualifiers and third-party
    importers are ignored).
    """
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
        # missing-warn-file means something is off — surface it but
        # don't block the build (best-effort check).
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
    _step(f"Moving {EXE_NAME} into release/")
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
    size_mb = dst.stat().st_size / (1024 * 1024)
    print(f"  {dst.relative_to(REPO_ROOT)} ({size_mb:.1f} MiB)")


# v3.3+ no longer copies the chart PDFs into the release bundle —
# the runtime downloads them from CAAI URLs on first use. The
# previous ``_copy_charts()`` function (and its companion
# ``release/map-pdfs/`` subdirectory) have been removed. Search
# ``ROADMAP.md`` for "map-fetch" if you need the history.


def _copy_license() -> None:
    """Copy ``LICENSE`` from the repo root into ``release/`` next to
    the .exe.

    Required by AGPLv3 §4: every copy of the binary must "conspicuously
    and appropriately publish on each copy an appropriate copyright
    notice" and "keep intact all notices stating that this License
    [...] apply to the code." Putting ``LICENSE`` next to
    ``cvfr-routemaster.exe`` is the simplest way to satisfy both
    clauses for a desktop-binary distribution.

    The per-file headers at the top of every shipped source file
    point users back to this file via the standard "see
    <http://www.gnu.org/licenses/>" line; without this copy in the
    release folder, that pointer would be the only place a recipient
    could find the actual license text. The source bundle gets its
    own copy via ``SOURCE_BUNDLE_TOP_FILES``.
    """
    _step("Copying LICENSE into release/")
    src = REPO_ROOT / "LICENSE"
    if not src.is_file():
        print(
            f"ERROR: LICENSE missing at repo root ({src}).\n"
            "AGPLv3 requires the license text to ship with every copy.",
            file=sys.stderr,
        )
        sys.exit(1)
    dst = RELEASE_DIR / "LICENSE"
    shutil.copy2(src, dst)
    size_kb = dst.stat().st_size / 1024
    print(f"  {dst.relative_to(REPO_ROOT)} ({size_kb:.1f} KiB)")


def _copy_slim_tesseract() -> None:
    """Copy the OCR-runtime subset of vendor/tesseract/ into
    release/tesseract/ — see the module docstring for what's kept and
    what's dropped, and why.

    Three categories of file in the source tree:

    1. **Always copy**: ``tesseract.exe`` (the OCR binary) and every
       ``*.dll`` (runtime deps — leptonica for image processing, ICU
       for Unicode, libarchive for multi-page TIFF, etc.). We keep
       *all* DLLs rather than try to reverse-engineer which ones
       tesseract.exe doesn't use, because the dependency graph
       changes between UB Mannheim builds and a missing DLL fails
       cryptically at OCR time, not at install time.

    2. **Always drop**: every other ``.exe`` (training tools like
       ``lstmtraining``, ``text2image``, ``cntraining``;
       ``tesseract-uninstall.exe``; ``winpath.exe``), every
       ``*.html`` man page, the ``doc/`` directory.

    3. **Tessdata, allowlist**: only ``eng.traineddata`` and
       ``heb.traineddata`` from ``tessdata/``. Drops
       ``osd.traineddata`` (10 MiB orientation/script detection,
       unused by us), the ``*.jar`` Java GUI tools, ``pdf.ttf``,
       and the empty ``eng.user-*`` placeholders.

    Net effect: ~239 MiB → ~167 MiB.
    """
    _step(f"Copying slim Tesseract into release/{RELEASE_TESSERACT_SUBDIR}/")
    target = RELEASE_DIR / RELEASE_TESSERACT_SUBDIR
    target.mkdir(exist_ok=True)

    src_root = DEV_TESSERACT_DIR
    files_copied = 0
    bytes_copied = 0
    bytes_skipped = 0

    for entry in sorted(src_root.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir():
            # Tessdata is the only subdirectory we descend into; doc/,
            # if present, is unconditionally dropped (just HTML man
            # pages — see UB Mannheim installer layout).
            if entry.name.lower() != "tessdata":
                bytes_skipped += sum(
                    f.stat().st_size for f in entry.rglob("*") if f.is_file()
                )
            continue
        suffix = entry.suffix.lower()
        keep = (
            entry.name.lower() == "tesseract.exe"
            or suffix == ".dll"
        )
        if not keep:
            bytes_skipped += entry.stat().st_size
            continue
        dst = target / entry.name
        shutil.copy2(entry, dst)
        files_copied += 1
        bytes_copied += entry.stat().st_size

    # Tessdata (allowlist).
    src_tessdata = src_root / "tessdata"
    dst_tessdata = target / "tessdata"
    dst_tessdata.mkdir(exist_ok=True)
    for name in TESSDATA_KEEP:
        src = src_tessdata / name
        dst = dst_tessdata / name
        shutil.copy2(src, dst)
        files_copied += 1
        bytes_copied += src.stat().st_size
    # Account for the dropped tessdata files in the size summary so
    # the "skipped" number meaningfully reflects what we left behind.
    if src_tessdata.is_dir():
        for entry in src_tessdata.iterdir():
            if entry.is_file() and entry.name not in TESSDATA_KEEP:
                bytes_skipped += entry.stat().st_size

    print(
        f"  copied {files_copied} files ({bytes_copied / (1024 * 1024):.1f} MiB), "
        f"skipped ~{bytes_skipped / (1024 * 1024):.1f} MiB of "
        "training tools / HTML docs / unused tessdata"
    )


def _write_shipped_derived_files() -> None:
    """Bake derived JSONs into ``release/.cvfr_routemaster/``.

    Three files get written here, all derived rather than copied:

    1. ``chart_sources.json`` — the three CAAI URLs from
       :data:`cvfr_routemaster.chart_source.CAAI_CHART_URLS`,
       which the runtime uses as first-run defaults. Single
       source of truth lives in ``chart_source.py``; we
       re-serialise it into the bundled JSON so the runtime can
       read the defaults without importing the package
       constants at install-discovery time.
    2. ``map_layout.json`` — derived from the shipped
       ``geo_calibration.json``'s ``map_layout`` blocks via
       :func:`write_shipped_map_layout`. Lets the friend's first
       launch place the chart sheets at their calibrated
       positions before QSettings has any state.
    3. ``font_settings.json`` — the dev's current font-size
       choices, baked from QSettings via
       :func:`write_shipped_font_settings`. Same QSettings-isn't-
       portable rationale.

    The pre-v3.3 ``_restamp_cache_fingerprints`` step
    (rewriting cache JSON ``mtime_ns`` to match shipped PDF
    mtimes) is GONE because the release no longer ships PDFs
    against which to align mtimes. The shipped cache JSONs
    carry the dev's PDF stat values as of build time; the
    runtime restamp (see
    :func:`cvfr_routemaster.cache_restamp.restamp_sheet_fingerprints`)
    overwrites those after each successful chart download so
    the fingerprint check passes against the just-downloaded
    file. The ``size`` field — which the runtime restamp does
    NOT overwrite — is the meaningful correctness gate: cache
    is valid iff the shipped size matches the downloaded size
    (i.e. iff CAAI is serving the same byte content the dev
    calibrated against). Cookbook step 1 makes the dev verify
    this before each release.
    """
    # ``write_shipped_font_settings`` does a delayed
    # ``from cvfr_routemaster.settings_store import ...`` at call
    # time. REPO_ROOT must stay on ``sys.path`` for that delayed
    # import to resolve.
    sys.path.insert(0, str(REPO_ROOT))
    from scripts._restamp_cache_fingerprints import (
        write_shipped_font_settings,
        write_shipped_map_layout,
    )
    from cvfr_routemaster import map_modes
    from cvfr_routemaster.settings_store import save_shipped_chart_sources

    # Per-mode chart_sources.json + map_layout.json. The URL set is
    # the registry's source of truth (CVFR's three CAAI sheets, LSA's
    # two), so a new chart product is attributed and seeded by
    # registering one MapMode — no edit here.
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
            # Print a truncated tail so the build log shows which URL
            # was baked without spamming the gov.il path padding.
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
        # Dev never opened the Font Settings dialog — nothing to
        # ship, the release falls through to the hard-coded
        # defaults. Not an error: this is the expected outcome
        # for a build on a brand-new dev box.
        print("  dev QSettings has no font-size keys; falling through to defaults")


def _sanitize_shipped_cache_paths() -> None:
    """Strip the dev box's absolute paths from every shipped cache
    JSON, leaving just the PDF basename.

    Without this step, the cache JSONs ship the dev's filesystem
    layout (``C:\\flying\\cvfr-routemaster\\<pdf>``) inside every
    release bundle — a small privacy/info leak with no functional
    purpose (the cache validity check uses ``mtime_ns`` + ``size``,
    not the ``path`` string). See
    ``scripts/_sanitize_shipped_cache_paths.py`` for the full
    rationale and the schema-blind walk that handles all five cache
    shapes uniformly.

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

    v4 layout: each chart product owns ``.cvfr_routemaster/<mode_id>/``
    holding its calibration, altitude-arrow caches, waypoint cache,
    and map-images metadata. We copy that JSON set for every
    registered mode (CVFR + LSA today) so a fresh install opens warm
    in any mode. The big rendered PNGs (``map_*.png``) and the
    downloaded ``charts/`` PDFs are deliberately NOT shipped — both
    are derivative works of the CAAI material the gov.il terms forbid
    redistributing; the runtime regenerates the PNGs on first
    chart-load against the freshly-downloaded PDFs.

    We also drop the ``.v4_migrated`` marker so the runtime's
    one-time flat→namespaced migration is a no-op on this already-v4
    tree (there are no flat caches to relocate).
    """
    _step("Copying per-mode seed .cvfr_routemaster/ caches into release/")
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
                "  ↪ these are optional; the friend's first launch will "
                "regenerate them transparently."
            )

    marker = root / V4_MIGRATION_MARKER
    marker.write_text("v4\n", encoding="utf-8")
    print(f"  wrote {V4_MIGRATION_MARKER} (release ships v4-namespaced seeds)")
    print(f"  total: {grand_copied} cache files across {len(_seed_mode_ids())} modes")


def _bundle_source_zip() -> None:
    """Build the AGPL §6(a) source bundle at
    ``release/source/cvfr-routemaster-source.zip``.

    Contents (deterministic):

    * The entire ``cvfr_routemaster/`` package, including the
      ``resources/`` subfolder, minus any ``__pycache__/`` or
      ``*.pyc`` debris.
    * The shipped ``.cvfr_routemaster/`` seed cache (default
      CAAI URLs, calibration, layout, font sizing, waypoint
      cache, altitude-arrow caches, map-images metadata). We
      take this from ``release/.cvfr_routemaster/`` rather than
      from the dev tree because the release copy has already
      been path-sanitised by ``_sanitize_shipped_cache_paths``
      — dev-absolute paths are stripped to bare basenames. A
      recipient who unzips the bundle into ``some-folder/`` and
      runs ``py -m cvfr_routemaster`` from there gets the
      identical first-launch experience as a .exe recipient:
      pre-populated CAAI URLs, working calibration, no manual
      Settings step.
    * ``requirements.txt`` (so a recipient can resolve runtime
      deps with one ``pip install`` command).
    * A generated ``README.txt`` explaining how to run from
      source.

    Explicitly NOT shipped — these are NOT part of the runnable
    program (so they aren't ``Corresponding Source`` under AGPL
    section 1) and either come from upstream distributors with
    their own source distributions (third-party binaries) or
    are pure dev-machine artifacts:

    * ``tests/`` — pinning tests for development.
    * ``scripts/`` — build automation.
    * ``vendor/`` — third-party binaries (Tesseract); upstream
      ships its own source.
    * ``*.md``, ``*.mdc``, ``ROADMAP*`` etc. — design notes.
    * ``cvfr-routemaster*.spec`` — PyInstaller build config.

    Why this is sufficient under AGPLv3 §6(a): the package
    directory + seed cache shipped here IS the program plus
    its first-launch configuration. Combined with
    ``requirements.txt`` a recipient with Python 3.10+ can
    install deps via pip and run ``py -m cvfr_routemaster``
    against the unzipped folder, with the same map-loading,
    calibration, and waypoint behaviour the .exe gives. The
    third-party deps' own licenses are attributed in-app
    under "Third-party software" in the Legal and Copyright
    Info dialog; their source code is available from their
    own distributors (PyPI / GitHub) under the terms of their
    own licenses, which is what AGPL §6 explicitly
    contemplates for combined works.

    The archive uses ``ZIP_DEFLATED`` for compression and
    walks the package in sorted order so two builds against
    the same tree produce byte-identical zips (modulo file
    mtimes, which Python zipfile preserves but doesn't matter
    for diffs against a known-good archive).
    """
    _step("Building source bundle (release/source/)")
    source_root = RELEASE_DIR / RELEASE_SOURCE_SUBDIR
    source_root.mkdir(parents=True, exist_ok=True)
    zip_path = source_root / SOURCE_ZIP_NAME

    package_root = REPO_ROOT / APP_PACKAGE
    if not package_root.is_dir():
        print(
            f"ERROR: missing package directory {package_root}", file=sys.stderr
        )
        sys.exit(1)

    # Take the seed cache from the RELEASE folder, not the dev
    # tree. The release folder's .cvfr_routemaster/ has been
    # through ``_copy_seed_cache``, ``_write_shipped_derived_files``,
    # and ``_sanitize_shipped_cache_paths`` — i.e. it's the
    # exact JSON set a friend running the .exe sees on first
    # launch. Taking it from REPO_ROOT/.cvfr_routemaster/ would
    # ship the dev's full machine state (absolute PDF paths,
    # rendered map PNGs, downloaded chart PDFs, satellite tile
    # cache) — all of which would either leak dev paths,
    # bloat the zip by ~hundreds of MiB, or violate the CAAI
    # redistribution restriction on the rendered PDFs.
    release_cache_root = RELEASE_DIR / ".cvfr_routemaster"
    if not release_cache_root.is_dir():
        print(
            f"ERROR: missing seed cache {release_cache_root} — "
            f"_copy_seed_cache must run before _bundle_source_zip",
            file=sys.stderr,
        )
        sys.exit(1)

    bundle_readme = _source_bundle_readme_text()

    # zipfile.ZIP_DEFLATED works on Windows without any extra
    # build tooling; ZIP_LZMA would compress better but Windows'
    # built-in Explorer extractor doesn't always handle it and
    # the recipient is exactly the audience we don't want to
    # ask "install 7-Zip first" of.
    file_count = 0
    total_bytes = 0
    with zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as zf:
        # 1. Package tree. We rglob in sorted order so the
        # archive layout is deterministic across builds, and
        # filter out anything under ``__pycache__/`` plus any
        # bytecode debris a stray ``import`` left behind.
        for src in sorted(package_root.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(REPO_ROOT)
            parts = rel.parts
            if "__pycache__" in parts:
                continue
            if src.suffix == ".pyc":
                continue
            # ``arcname`` uses forward-slash separators
            # explicitly — zip's wire format and every cross-
            # platform unzipper expects them.
            arcname = "/".join(rel.parts)
            zf.write(src, arcname=arcname)
            file_count += 1
            total_bytes += src.stat().st_size

        # 2. Sanitised seed cache from release/.cvfr_routemaster/.
        # We defensively skip any PDFs, PNGs, or satellite-tile
        # subfolders that might exist — the seed cache should
        # be JSON-only metadata, but excluding non-JSON here
        # protects us if a future change to _copy_seed_cache
        # starts copying chart payloads (which we do NOT have
        # redistribution rights to).
        for src in sorted(release_cache_root.rglob("*")):
            if not src.is_file():
                continue
            if src.suffix.lower() not in {".json"}:
                print(
                    f"  skipping non-JSON in seed cache: "
                    f"{src.relative_to(release_cache_root)}"
                )
                continue
            rel = src.relative_to(RELEASE_DIR)
            arcname = "/".join(rel.parts)
            zf.write(src, arcname=arcname)
            file_count += 1
            total_bytes += src.stat().st_size

        # 3. Top-level metadata files (requirements.txt).
        for fname in SOURCE_BUNDLE_TOP_FILES:
            src = REPO_ROOT / fname
            if not src.is_file():
                print(
                    f"WARNING: source-bundle top file missing: {fname}",
                    file=sys.stderr,
                )
                continue
            zf.write(src, arcname=fname)
            file_count += 1
            total_bytes += src.stat().st_size

        # 4. Bundle-local README explaining how to run from source.
        zf.writestr("README.txt", bundle_readme)
        file_count += 1
        total_bytes += len(bundle_readme.encode("utf-8"))

    zip_size = zip_path.stat().st_size
    print(
        f"  {zip_path.relative_to(REPO_ROOT)}\n"
        f"  {file_count} files, "
        f"{total_bytes / 1024:.0f} KiB uncompressed -> "
        f"{zip_size / 1024:.0f} KiB compressed"
    )


def _source_bundle_readme_text() -> str:
    """Render the README.txt that goes inside the source zip.

    Kept ASCII-only for the same reason as the release README:
    a recipient opening it in Windows Notepad on a default
    code page should see no mojibake. The instructions cover
    the three things a Python-literate but cvfr-unfamiliar
    reader needs: dependency install, run command, and where
    the license lives.

    The Python version floor (3.10) is the minimum the
    production code requires (PySide6 6.6 binds support
    starting at 3.9, but we use 3.10+ syntax in several
    modules — ``match`` statements and ``X | Y`` unions
    without ``from __future__``). Pinning the floor in the
    README saves a "why won't this import?" round trip.
    """
    return (
        "CVFR Route Master - Source bundle\n"
        "=================================\n\n"
        "This archive contains the complete corresponding source\n"
        "code for the CVFR Route Master program, distributed under\n"
        "the GNU Affero General Public License v3 in satisfaction\n"
        "of AGPLv3 section 6(a). The same source is also available\n"
        "from the program author (see the Legal and Copyright Info\n"
        "dialog inside the program for contact details).\n\n"
        "What's in here\n"
        "--------------\n"
        "  cvfr_routemaster/      The runnable program package.\n"
        "                         Includes:\n"
        "                           - All Python modules.\n"
        "                           - The 'resources/' subfolder\n"
        "                             with bundled data files.\n"
        "                           - __main__.py so 'py -m\n"
        "                             cvfr_routemaster' works.\n"
        "  .cvfr_routemaster/     Seed cache and configuration:\n"
        "                         the default CAAI chart URLs,\n"
        "                         calibration, layout, font\n"
        "                         sizing, and waypoint cache.\n"
        "                         Identical to the seed cache\n"
        "                         shipped beside the .exe so\n"
        "                         first launch from source gives\n"
        "                         the same experience: pre-\n"
        "                         configured URLs, working\n"
        "                         calibration, no manual setup.\n"
        "  requirements.txt       Runtime Python dependencies.\n"
        "  LICENSE                Full text of the GNU Affero\n"
        "                         General Public License v3, which\n"
        "                         this program is distributed under,\n"
        "                         with the project copyright-holder\n"
        "                         block at the top.\n"
        "  README.txt             This file.\n\n"
        "Third-party dependencies (PySide6, PyMuPDF, Pillow,\n"
        "pytesseract, NumPy) are obtained via pip from their\n"
        "own distributors under their own licenses; their source\n"
        "is available from PyPI / GitHub under those licenses.\n"
        "The bundled Tesseract OCR binary that ships with the\n"
        "compiled .exe is a separate work governed by its own\n"
        "Apache 2.0 license; its source is available from the\n"
        "Tesseract upstream project.\n\n"
        "Running from source\n"
        "-------------------\n"
        "1. Install Python 3.10 or newer\n"
        "   (https://www.python.org/downloads/).\n"
        "2. Unzip this archive somewhere convenient. The\n"
        "   examples below assume the unzipped contents live\n"
        "   in a folder called 'cvfr-routemaster-source/'.\n"
        "3. Open a terminal in the PARENT of that folder.\n"
        "4. (Optional but recommended) create a virtualenv:\n"
        "       py -m venv .venv\n"
        "       .venv\\Scripts\\activate          (Windows)\n"
        "       source .venv/bin/activate         (Linux/macOS)\n"
        "5. Install runtime dependencies:\n"
        "       py -m pip install -r cvfr-routemaster-source/requirements.txt\n"
        "6. Run the program:\n"
        "       cd cvfr-routemaster-source\n"
        "       py -m cvfr_routemaster\n\n"
        "   (The command is 'py -m cvfr_routemaster' because\n"
        "    that's the Python package name; do not rename the\n"
        "    package folder or the import will break.)\n\n"
        "Optional: bundled OCR\n"
        "---------------------\n"
        "The compiled .exe release ships a slim Tesseract OCR\n"
        "engine for re-scanning the back-pages PDF. When running\n"
        "from source, the program looks for Tesseract on your\n"
        "PATH (see cvfr_routemaster/tesseract_runtime.py for the\n"
        "resolution order). If you don't need to re-OCR the\n"
        "back-pages (the shipped waypoint cache covers the\n"
        "current chart cycle), Tesseract is not required.\n\n"
        "License\n"
        "-------\n"
        "This program is licensed under the GNU Affero General\n"
        "Public License v3 or later. The full license text is\n"
        "included as LICENSE at the top level of this archive\n"
        "(also available at https://www.gnu.org/licenses/agpl-3.0.html)\n"
        "and is surfaced inside the program via the 'Legal and\n"
        "Copyright Info' toolbar button.\n"
    )


def _write_readme() -> None:
    _step("Writing release/README.txt")
    readme_path = RELEASE_DIR / "README.txt"
    readme_path.write_text(
        # README is intentionally short, ASCII-only, and avoids any
        # Unicode that a notepad-on-default-codepage friend might see
        # as mojibake. It tells the user how to launch, what the
        # first-run download looks like, where not to put the folder,
        # how to inspect the license, and how to get help.
        "CVFR Route Master\n"
        "=================\n\n"
        "Intended use\n"
        "------------\n"
        "This program is intended for flight-simulator use only. Real-\n"
        "world aviation use is at your own risk and is not contemplated\n"
        "by this software. It is not a substitute for official charts,\n"
        "NOTAMs, weather briefings, or any other official flight-planning\n"
        "material. Always cross-check against current AIP material before\n"
        "any simulated flight. See toolbar -> Program Information ->\n"
        "Legal and Copyright Info for the full notice.\n\n"
        "How to run\n"
        "----------\n"
        "1. Unzip this folder somewhere user-writable (your Documents\n"
        "   or Desktop is perfect). Do NOT put it in 'Program Files' --\n"
        "   the app needs to write small cache files next to itself.\n"
        "2. Double-click cvfr-routemaster.exe.\n"
        "3. First launch: Windows SmartScreen may say 'Windows protected\n"
        "   your PC -- unknown publisher'. Click 'More info' then 'Run\n"
        "   anyway'. (The .exe is unsigned because this is a private\n"
        "   share; nothing about that warning means anything is wrong.)\n"
        "4. Startup takes 5-15 seconds the first time as Windows extracts\n"
        "   the bundled Qt + Python libraries. Subsequent launches are\n"
        "   faster.\n\n"
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
        "Two chart products: CVFR and LSA\n"
        "--------------------------------\n"
        "This program ships two Israeli chart products: CVFR (the default,\n"
        "shown on first launch) and LSA. Use the toolbar's chart-type\n"
        "toggle to switch between them. Each product downloads its own\n"
        "PDFs the first time you open it (same one-time fetch as above)\n"
        "and keeps its own cached charts and calibration, so switching\n"
        "back and forth after that is instant and offline.\n\n"
        "What's in this folder\n"
        "---------------------\n"
        "  cvfr-routemaster.exe          The application itself.\n"
        "  icon.ico                      App icon (cosmetic only).\n"
        "  README.txt                    This file.\n"
        "  LICENSE                       Full text of the GNU Affero\n"
        "                                General Public License v3,\n"
        "                                which this program is\n"
        "                                distributed under, with the\n"
        "                                project copyright-holder block\n"
        "                                at the top.\n"
        "  tesseract/                    Bundled OCR engine. Used to read\n"
        "                                Hebrew names off the back-pages\n"
        "                                PDF when the cached waypoint\n"
        "                                table needs to be regenerated.\n"
        "  .cvfr_routemaster/            Calibration + cached chart data\n"
        "                                + the default CAAI chart URLs for\n"
        "                                each chart product (CVFR and LSA),\n"
        "                                one folder per product. Hidden in\n"
        "                                Explorer by default but must travel\n"
        "                                with the .exe. The downloaded chart\n"
        "                                PDFs will also live under\n"
        "                                .cvfr_routemaster/<product>/charts/\n"
        "                                after first launch.\n"
        "  source/cvfr-routemaster-      The complete program source\n"
        "  source.zip                    code, distributed alongside the\n"
        "                                binary in satisfaction of\n"
        "                                AGPLv3 section 6(a). Most users\n"
        "                                can ignore this; see the\n"
        "                                'Source code (advanced users)'\n"
        "                                section below.\n\n"
        "After the first-launch download finishes, the maps will line up\n"
        "with their geographic coordinates with no calibration on your\n"
        "part -- the calibration that came with this folder is reused.\n\n"
        "Source code (advanced users)\n"
        "----------------------------\n"
        "This program is free software licensed under the GNU Affero\n"
        "General Public License v3 or later. The complete source code\n"
        "is bundled with this release under source/. To run from source\n"
        "instead of from the compiled .exe:\n\n"
        "  1. Install Python 3.10 or newer from python.org.\n"
        "  2. Unzip source/cvfr-routemaster-source.zip somewhere.\n"
        "  3. In a terminal in that folder:\n"
        "       py -m pip install -r requirements.txt\n"
        "       py -m cvfr_routemaster\n\n"
        "Full instructions live in README.txt inside the source zip.\n"
        "Running from source is NOT required to use the program -- the\n"
        "compiled .exe in this folder is the supported entry point for\n"
        "everyone except developers and recipients exercising their\n"
        "AGPL rights.\n\n"
        "Updating to a new chart cycle\n"
        "-----------------------------\n"
        "When CAAI publishes a new CVFR edition, this program will need a\n"
        "release update -- the calibration is built against a specific\n"
        "edition. If you want to use a newer PDF before the program is\n"
        "updated, drop the PDF on disk and paste its full path into the\n"
        "matching field in Map File Settings (or update the URL field if\n"
        "the new edition is at a new URL). The program will re-render\n"
        "the maps and re-OCR the waypoint table, which may take a few\n"
        "minutes and may also require re-calibration via the Calibration\n"
        "Options dialog.\n\n"
        "If something goes wrong\n"
        "-----------------------\n"
        "* If a chart download fails repeatedly: the error dialog shows\n"
        "  the URL it's trying to fetch. Open the URL in your web browser\n"
        "  to confirm CAAI is reachable from your network. If you can\n"
        "  download the PDF in the browser but not from the program, save\n"
        "  the PDF to disk and paste the full path into Map File Settings\n"
        "  for that sheet instead.\n"
        "* If the maps load but coordinates look wrong: the calibration\n"
        "  files in .cvfr_routemaster/ were left out of the zip. Ask the\n"
        "  sender for the .cvfr_routemaster folder.\n"
        "* If the .exe won't launch at all: paste any error message back\n"
        "  to the sender.\n"
        "* If you see 'Tesseract OCR not found' on a re-OCR: the tesseract/\n"
        "  folder was left out of the zip. Ask the sender to re-share with\n"
        "  the full release/ contents.\n"
        "* Antivirus may flag the .exe -- the bundled-Python format used\n"
        "  here (PyInstaller --onefile) sometimes triggers heuristics.\n"
        "  An exception in your AV for this folder resolves it.\n",
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
        f"  release/ contains {files} files, {total / (1024 * 1024):.1f} MiB total\n"
        f"  Next steps:\n"
        f"    1. Smoke-test by double-clicking release/{EXE_NAME}.\n"
        f"    2. Zip the *contents* of release/ (NOT the release/ folder\n"
        f"       itself) and send to your friend.\n"
        f"    3. Tell them to extract somewhere user-writable and run\n"
        f"       {EXE_NAME}."
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
        help="Don't regenerate icon.ico; use whatever is already in release/.",
    )
    args = parser.parse_args()

    _check_prerequisites()
    if not args.skip_icon:
        _regenerate_icon()
    _clean_release_dir()
    if not args.skip_pyinstaller:
        _run_pyinstaller()
        # Scan BEFORE the copy: a flagged warn-file means the .exe
        # would crash at launch, and there's no value in moving a
        # broken binary into release/ just to be rejected moments
        # later.
        _scan_pyinstaller_warnings()
        _copy_exe()
    else:
        print("\n[--skip-pyinstaller] PyInstaller step skipped; .exe not refreshed.")
    _copy_slim_tesseract()
    _copy_seed_cache()
    # Must run AFTER _copy_seed_cache (needs the cache JSONs to
    # mutate AND write_shipped_map_layout reads the shipped
    # geo_calibration.json + map_images_meta.json).
    _write_shipped_derived_files()
    # Must run LAST because it rewrites the same JSON files that
    # _write_shipped_derived_files emits — running it earlier would
    # leave the dev's absolute paths in the derived JSONs.
    _sanitize_shipped_cache_paths()
    _bundle_source_zip()
    _copy_license()
    _write_readme()
    _summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
