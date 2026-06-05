# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the CVFR Route Master Linux release.

Driven by ``scripts/build_release_for_linux.py`` — invoke that, not
PyInstaller directly, so the ``release-linux/`` folder is fully
populated (PDFs + seed cache + README + .desktop file) at the same
time the ELF is built. Direct ``pyinstaller cvfr-routemaster-linux.spec``
invocations still work for iterating on PyInstaller-side problems in
isolation; you just won't get the surrounding distribution payload.

Why a separate spec from ``cvfr-routemaster.spec`` (Windows)?

The two specs differ in three substantive ways:

1. **No bundled Tesseract.** Linux Tesseract is dynamically linked
   against ~50 system shared libraries living at fixed FHS paths
   (``/usr/lib/x86_64-linux-gnu/lib*.so.*``) — bundling it
   reliably means ``ldd``-walking the dep graph and patching rpath,
   which is fragile across glibc versions. The Linux release
   instead relies on a one-time
   ``apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-heb``
   on the user's Debian box. ``pytesseract`` is still a hidden
   import so the Python wrapper imports cleanly even if the
   binary isn't installed yet (the user gets a friendly Qt
   dialog with the apt command instead of a ModuleNotFoundError
   stack trace).

2. **No ``console=False``.** That flag exists to suppress the
   stray cmd.exe window that Windows pops up alongside a GUI
   PyInstaller binary; on Linux there's no equivalent footgun
   because ELF binaries don't auto-spawn a terminal — they
   inherit the parent terminal if launched from a shell, or
   none if launched from a desktop entry.

3. **No icon embedding.** Linux desktop integration uses an
   external PNG referenced by a ``.desktop`` file, not an icon
   baked into the binary. The Linux build script generates
   ``release-linux/icon.png`` separately and the ``.desktop``
   file points at it.

Otherwise the spec mirrors Windows: same hidden imports (PySide6
sub-modules + ``fitz`` + ``pytesseract``), same excludes (tkinter,
unittest, pytest, PIL.ImageQt), same ``--onefile`` shape with no
bundled data files (PDFs / cache / icon all live next to the binary
in ``release-linux/`` so the user can swap a chart cycle without
rebuilding).
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# ``__file__`` isn't defined when PyInstaller execs a spec file, but
# spec files run with the cwd set to the directory containing the
# spec — Path('.') is the repo root.
REPO_ROOT = Path('.').resolve()

block_cipher = None

# IMPORTANT — PySide6 plugin bundling on Linux.
#
# PyInstaller's stock PySide6 hook on Linux (verified with PySide6
# 6.11 + PyInstaller 6.20 on Debian 13) bundles only the Qt shared
# libraries (``Qt/lib/*.so``) and skips the entire ``Qt/plugins/``
# tree. Without ``Qt/plugins/platforms/libqxcb.so`` (or any platform
# plugin) the binary cannot open a window — Qt aborts at startup
# with "Could not find the Qt platform plugin". Image format
# plugins (``Qt/plugins/imageformats/``), Qt translations, and
# style plugins are similarly omitted.
#
# ``collect_all('PySide6')`` walks the entire installed PySide6
# tree and returns ``(datas, binaries, hiddenimports)`` covering
# everything PyInstaller could reasonably need: source files,
# Qt shared libs, plugin .so files, .qm translation files, and
# the small JSON/conf files Qt looks up at runtime. This is a
# superset of what we strictly need (it adds ~30 MiB to the
# binary in the form of plugins we'll never use, like canbus
# or geoservices) but the alternative — manually enumerating
# the subset — is brittle across PySide6 minor versions.
#
# This bug does *not* manifest on Windows because the Windows
# PyInstaller hook bundles plugins through a different code path
# (``windeployqt``-style enumeration) that the Linux hook lacks.
pyside_datas, pyside_binaries, pyside_hiddenimports = collect_all('PySide6')


a = Analysis(
    ['cvfr_routemaster/__main__.py'],
    pathex=[str(REPO_ROOT)],
    binaries=pyside_binaries,
    # Bundle PySide6's data tree (which includes Qt/plugins/ —
    # critically the ``platforms/libqxcb.so`` plugin without which
    # the binary refuses to start). PDFs and seed cache live
    # alongside the binary in ``release-linux/`` so the user can
    # swap chart cycles without rebuilding.
    #
    # Two additional bundled assets, both inside
    # ``cvfr_routemaster/resources/``:
    #
    #   * ``app_icon.png`` — Qt's ``setWindowIcon`` needs a real
    #     path at launch; the .desktop launcher's icon reference
    #     doesn't reach the window-decoration layer (Wayland/X11
    #     title bars source their icon from the process via
    #     ``setWindowIcon``, not from the .desktop entry).
    #
    #   * ``airplane_mode_icon.png`` — the MainWindow toolbar's
    #     airplane-mode toggle uses this PNG as its action icon.
    #
    # Same path-resolution trick as the Windows spec: PyInstaller
    # rewrites ``__file__`` for the cvfr_routemaster package to
    # point at the extracted ``sys._MEIPASS`` copy, so
    # ``cvfr_routemaster.app_icon._icon_path()`` /
    # ``_airplane_mode_icon_path()`` just work.
    datas=pyside_datas + [
        (str(REPO_ROOT / 'cvfr_routemaster' / 'resources' / 'app_icon.png'),
         'cvfr_routemaster/resources'),
        (str(REPO_ROOT / 'cvfr_routemaster' / 'resources' / 'airplane_mode_icon.png'),
         'cvfr_routemaster/resources'),
        # ``aircraft_wake.json`` — bundled ICAO-type-designator →
        # wake-turbulence-category lookup for the VATSIM traffic
        # overlay (v2 feature). Mirrored verbatim from the
        # Windows spec so the two builds stay in lockstep on
        # bundled package data; PyInstaller rewrites
        # ``__file__`` for the ``cvfr_routemaster`` package at
        # extraction time so the resource resolution in
        # ``vatsim_feed._wake_db_path`` works in both modes.
        # Forgetting this entry on the Linux side would silently
        # paint every plane gray on the friend's box — the
        # "unknown" wake fallback is the same defensive behaviour
        # we rely on for unrecognised types, so the failure mode
        # is degraded rather than crashing, which makes the
        # mirror discipline that much more important.
        (str(REPO_ROOT / 'cvfr_routemaster' / 'resources' / 'aircraft_wake.json'),
         'cvfr_routemaster/resources'),
    ],
    hiddenimports=pyside_hiddenimports + [
        # PySide6 — most are auto-discovered, but we belt-and-brace
        # the ones we use directly because the auto-discovery
        # occasionally misses Qt sub-modules referenced via
        # ``QtCore.Qt.<enum>`` rather than direct import.
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'PySide6.QtSvg',
        # PyMuPDF — public name is ``fitz``; the package's internal
        # extension modules are picked up by PyInstaller's auto-hook.
        'fitz',
        # pytesseract is the Python wrapper around the ``tesseract``
        # CLI binary. Listed here so the import succeeds at
        # module-load time, which lets ``back_page_ocr`` raise its
        # platform-aware "install with apt" RuntimeError when the
        # CLI is missing — instead of the import itself failing
        # with ModuleNotFoundError before the friendly handler
        # even runs.
        'pytesseract',
        # numpy is a top-level import in ``cvfr_routemaster.map_crop``
        # (vectorised white-margin detection on the rendered chart
        # pixmap). PyInstaller's static analyser already finds it via
        # the import graph, but the Linux release v2 shipped without
        # numpy because the WSL build venv was assembled with an
        # explicit pip-install list that didn't include it, and the
        # build script swallowed the warning. Listing it here is
        # belt-and-braces alongside the new warn-file scanner in
        # ``scripts/_pyinstaller_warnings``: the scanner is what
        # actually fails the build, but having numpy in
        # ``hiddenimports`` means a future build venv that *does*
        # have numpy installed still bundles it even if the
        # scanner is somehow bypassed.
        'numpy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Excluding test packages and dev-only deps shaves ~10MB off the
    # binary and stops PyInstaller from chasing spurious dependencies.
    excludes=[
        'tkinter',
        'unittest',
        'pytest',
        'PIL.ImageQt',  # uses PyQt5 → drags it in if not excluded
    ],
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
    # See module docstring in ``cvfr-routemaster.spec`` for why UPX
    # is off — same reasoning applies on Linux (no AV heuristic
    # concern, but UPX-compressed binaries are slower to start and
    # the size savings are marginal next to the bundled Qt+PySide6).
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Linux doesn't embed icons in the ELF — see module docstring.
)
