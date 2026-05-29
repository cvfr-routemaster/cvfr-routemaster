from __future__ import annotations

import sys
from pathlib import Path


def _project_root() -> Path:
    """Resolve the directory the app should treat as its "project root".

    Project root is the folder that holds the three CVFR PDFs and the
    ``.cvfr_routemaster/`` cache subfolder — i.e. everything the app
    reads / writes at runtime.

    Two execution modes:

    - **Dev / source checkout** (``python -m cvfr_routemaster``):
      ``__file__`` lives at ``<repo>/cvfr_routemaster/__main__.py`` so
      ``parents[1]`` walks two levels up to the repo root, where the
      PDFs and the ``.cvfr_routemaster/`` folder also live. This is
      the layout the tests and the dev README assume.

    - **Frozen / PyInstaller --onefile build**: ``getattr(sys, "frozen",
      False)`` is True and ``sys._MEIPASS`` points at a temp
      extraction directory containing the *bundled python code*. We
      do **not** want that path — the PDFs and writable cache are
      *not* bundled inside the exe (they sit beside it in the
      release/ folder so they can be independently updated, and so
      cache writes survive across launches). Instead we use
      ``Path(sys.executable).parent``, the directory containing the
      .exe itself, which is exactly where ``scripts/build_release.py``
      drops the PDFs and the seed ``.cvfr_routemaster/`` folder.

    Mirrors the same frozen-mode switch already used in
    :func:`cvfr_routemaster.tesseract_runtime.application_root`.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def main() -> None:
    """Show the splash before importing MainWindow so startup never looks hung during imports."""
    root = _project_root()

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImageReader
    from PySide6.QtWidgets import QApplication, QProgressDialog

    try:
        QImageReader.setAllocationLimit(0)
    except AttributeError:
        pass

    from cvfr_routemaster import APP_NAME

    app = QApplication([])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("CVFRRouteMaster")

    # Push the app icon onto QApplication BEFORE any window
    # (including the splash) is constructed — Windows / WSLg pick
    # up the taskbar icon from the first window the app shows, so
    # setting it post-show would leave the splash's taskbar entry
    # rendered with Qt's default Python icon. ``app_icon()`` returns
    # an empty QIcon when the bundled PNG isn't on disk (defensive
    # for a fresh checkout that hasn't run the icon generator), and
    # ``setWindowIcon`` is a no-op on an empty QIcon, so this never
    # downgrades from a working icon to a broken one.
    from cvfr_routemaster.app_icon import app_icon

    app.setWindowIcon(app_icon())

    from cvfr_routemaster.settings_store import load_font_sizes
    from cvfr_routemaster.ui_theme import apply_dark_theme

    # Honour the user's saved Font Settings preferences at the
    # earliest possible point (before the splash + MainWindow
    # imports kick in). Pass ``project_root`` so a first-launch on
    # a release with no QSettings rolls up to the shipped
    # ``font_settings.json`` (written by the build script from the
    # dev's QSettings) instead of the hard-coded defaults — same
    # rationale as the ``map_layout.json`` mechanism: a friend
    # inheriting the release sees the same UI sizing the dev
    # configured, not the bare defaults.
    apply_dark_theme(app, load_font_sizes(root))

    from cvfr_routemaster import app_title

    splash = QProgressDialog(None)
    splash.setWindowTitle(app_title())
    splash.setLabelText("Starting…")
    splash.setRange(0, 0)
    splash.setCancelButton(None)
    splash.setWindowModality(Qt.WindowModality.ApplicationModal)
    splash.setMinimumDuration(0)
    splash.show()
    app.processEvents()

    from cvfr_routemaster.main_window import run_app

    raise SystemExit(run_app(root, app=app, splash=splash))


if __name__ == "__main__":
    main()
