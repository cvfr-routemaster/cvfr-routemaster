# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the CVFR Route Master Windows release.

Driven by ``scripts/build_release.py`` — invoke that, not PyInstaller
directly, so the ``release/`` folder is fully populated (PDFs + seed
cache + README) at the same time the .exe is built. Direct
``pyinstaller cvfr-routemaster.spec`` invocations still work for
iterating on PyInstaller-side problems in isolation; you just won't
get the surrounding distribution payload.

Design choices:

- ``--onefile`` mode (single ``.exe`` that self-extracts dependencies
  to a temp dir at launch). This is what the user asked for as the
  shipping unit; the slow first-run extraction is acceptable for a
  share-with-a-friend distribution and the friend gets exactly one
  file to double-click.

- ``console=False`` — pure GUI app, so suppressing the cmd window
  matters. Without this, every launch flashes a black console
  alongside the splash dialog.

- ``upx=False`` — UPX compression shrinks the .exe by ~30% but
  notoriously trips Windows Defender / SmartScreen / third-party
  AV heuristics. The ~30% saving isn't worth a "this file might
  be malicious" dialog on the friend's machine.

- We intentionally do NOT bundle the chart PDFs, the
  ``.cvfr_routemaster/`` cache, or the icon as ``datas`` —
  they live next to the .exe in ``release/`` so they can be
  individually updated, and so cache writes during use survive
  across launches (writing into ``sys._MEIPASS`` would be lost
  on next start because PyInstaller wipes the temp dir).

- Hidden imports cover PySide6 sub-modules + PyMuPDF (``fitz``)
  internals that PyInstaller's auto-discovery occasionally misses.
  ``pytesseract`` is listed as an optional best-effort import: if
  the friend has Tesseract installed system-wide they can re-OCR;
  if not, the cached waypoints handle the normal case.
"""

from pathlib import Path

# ``__file__`` isn't defined when PyInstaller execs a spec file, but
# spec files run with the cwd set to the directory containing the
# spec — Path('.') is the repo root.
REPO_ROOT = Path('.').resolve()
ICON_PATH = REPO_ROOT / 'release' / 'icon.ico'

block_cipher = None


a = Analysis(
    ['cvfr_routemaster/__main__.py'],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    # PDFs, seed cache, and ``release/icon.ico`` deliberately live
    # alongside the .exe in ``release/`` (see module docstring).
    #
    # The exceptions are the two PNGs under
    # ``cvfr_routemaster/resources/``:
    #
    #   * ``app_icon.png`` — the running app sets the window /
    #     taskbar icon via Qt's ``setWindowIcon(QIcon(<path>))``,
    #     which needs a real path to a PNG at launch. The .ico
    #     baked into the .exe by PyInstaller gives the Windows
    #     taskbar/title-bar a launcher icon, but Qt's in-window
    #     setWindowIcon happens at Python level and needs its own
    #     file.
    #
    #   * ``airplane_mode_icon.png`` — the toolbar's airplane-mode
    #     toggle uses this PNG as its action icon. Same runtime
    #     contract as the app icon (QIcon needs a real path).
    #
    # We bundle both PNGs inside the package so
    # ``cvfr_routemaster.app_icon._icon_path()`` /
    # ``_airplane_mode_icon_path()`` resolve to real extracted
    # paths inside ``sys._MEIPASS`` at launch — no frozen/dev
    # branch needed in the loaders.
    datas=[
        (str(REPO_ROOT / 'cvfr_routemaster' / 'resources' / 'app_icon.png'),
         'cvfr_routemaster/resources'),
        (str(REPO_ROOT / 'cvfr_routemaster' / 'resources' / 'airplane_mode_icon.png'),
         'cvfr_routemaster/resources'),
        # ``aircraft_wake.json`` — bundled ICAO-type-designator →
        # wake-turbulence-category lookup used by
        # ``cvfr_routemaster.vatsim_feed.load_aircraft_wake_db`` to
        # colour-code VATSIM traffic icons (v2 feature). Same
        # path-resolution trick as the PNGs above:
        # ``Path(__file__).parent / "resources"`` resolves into
        # ``sys._MEIPASS/cvfr_routemaster/resources/`` at runtime.
        # If this entry goes missing the app keeps working — every
        # pilot just renders gray (the "unknown" wake category) —
        # but the Linux spec must mirror this entry to avoid a
        # silent build-tree drift.
        (str(REPO_ROOT / 'cvfr_routemaster' / 'resources' / 'aircraft_wake.json'),
         'cvfr_routemaster/resources'),
    ],
    hiddenimports=[
        # PySide6 — most are auto-discovered, but we belt-and-brace
        # the ones we use directly because the auto-discovery
        # occasionally misses Qt sub-modules referenced via
        # ``QtCore.Qt.<enum>`` rather than direct import.
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'PySide6.QtSvg',
        # PyMuPDF — the public name is ``fitz`` but the package
        # ships internal extension modules that PyInstaller's
        # auto-hook handles, so we just need the top-level here.
        'fitz',
        # pytesseract is optional — friend doesn't normally need it
        # because waypoints_cache.json ships pre-built. Listing it
        # ensures the import succeeds at module-load time so the
        # "Re-OCR" code path can fail gracefully with a clear
        # "Tesseract executable not found" message rather than
        # "ModuleNotFoundError: No module named 'pytesseract'".
        'pytesseract',
        # numpy is a top-level import in ``cvfr_routemaster.map_crop``
        # (vectorised white-margin detection on the rendered chart
        # pixmap). PyInstaller's static analyser already finds it via
        # the import graph, but listing it here defensively means a
        # build venv assembled with ``pip install --no-deps`` (or one
        # where numpy is uninstalled by accident) gets a clear
        # "missing dependency: numpy" build-time error from
        # ``scripts/_pyinstaller_warnings`` instead of shipping a
        # binary that crashes at launch with
        # ``ModuleNotFoundError: No module named 'numpy'``.
        # See ROADMAP "Linux release crashes at launch" for the
        # original bug this guards against.
        'numpy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Excluding test packages and dev-only deps shaves ~10MB off the
    # .exe and stops PyInstaller from chasing spurious dependencies.
    excludes=[
        'tkinter',
        'unittest',
        'pytest',
        'PIL.ImageQt',  # uses PyQt5 → drags it in if not excluded
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='cvfr-routemaster',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # See module docstring for why UPX is off.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Pure-GUI app — suppress the cmd window. Without ``console=False``,
    # every double-click flashes a black console alongside the splash.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Use the generated icon if it exists; PyInstaller falls back to
    # its default Python icon if the file is missing, so a fresh
    # checkout that hasn't run ``generate_release_icon.py`` yet
    # still builds rather than failing at the spec-load stage.
    icon=str(ICON_PATH) if ICON_PATH.is_file() else None,
)
