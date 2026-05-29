"""
Locate a vendored Tesseract under one of two well-known layouts and configure pytesseract.

Two supported layouts (checked in this order):

1. **Clean release layout** — what ``scripts/build_release.py`` ships next to the .exe::

       <app>/tesseract/tesseract.exe          # Windows
       <app>/tesseract/tessdata/heb.traineddata
       <app>/tesseract/tessdata/eng.traineddata

   This is the layout a user sees when they unzip the friend-shippable
   ``release/`` bundle: ``tesseract/`` lives next to the .exe in plain
   sight (no ``vendor/`` developer-jargon prefix).

2. **Dev layout** — what ``scripts/fetch_vendor_tesseract.py`` produces in a dev checkout::

       <repo>/vendor/tesseract/tesseract.exe
       <repo>/vendor/tesseract/tessdata/heb.traineddata
       <repo>/vendor/tesseract/tessdata/eng.traineddata

   Kept verbatim so existing dev environments and the
   ``--only-tessdata`` fetch flow continue to work without changes.

We deliberately accept both rather than renaming dev to match release:
the dev tree's ``vendor/`` already contains a few hundred MB of
Tesseract artefacts that nobody wants to re-download just to rename a
folder, and ``vendor/`` is a meaningful word in a source repo.

``TESSDATA_PREFIX`` is set to whichever ``tessdata/`` directory we
actually found (the folder that holds the ``.traineddata`` files —
**not** its parent; tesseract's own error text is misleading on that
point).

If neither layout is present, the system ``tesseract`` on PATH is
used (development fallback for someone who installed Tesseract
system-wide).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_configured = False

# Subfolder names checked under :func:`application_root`, **in priority
# order**. The release layout wins when both are present so that a dev
# who happens to also have a release/ folder checked out doesn't end up
# silently using the slimmed-down release Tesseract instead of the
# fuller dev one (release/tesseract/ excludes training tools and
# osd.traineddata, which a dev script might still want).
#
# Tuple-of-tuples instead of a flat tuple so each entry can be a
# multi-segment path without callers having to reassemble it.
_TESSERACT_SUBDIRS: tuple[tuple[str, ...], ...] = (
    ("tesseract",),
    ("vendor", "tesseract"),
)


def application_root() -> Path:
    """Directory the app treats as "next to itself".

    - **Dev / source checkout**: the repo root
      (``<repo>/cvfr_routemaster/tesseract_runtime.py`` →
      ``parents[1]`` is ``<repo>``).
    - **Frozen / PyInstaller --onefile**: the directory containing the
      .exe itself, NOT ``sys._MEIPASS`` — the bundled Tesseract sits
      next to the .exe so we can swap charts/tessdata without
      rebuilding.

    Mirrors :func:`cvfr_routemaster.__main__._project_root`.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _tesseract_search_bases(root: Path) -> list[Path]:
    """Concrete candidate directories to look in for the Tesseract install."""
    return [root.joinpath(*parts) for parts in _TESSERACT_SUBDIRS]


def bundled_tesseract_exe(root: Path | None = None) -> Path | None:
    """First ``tesseract.exe`` (or POSIX ``tesseract``) we find under
    one of the supported layouts, or ``None`` if neither is present."""
    root = root or application_root()
    for base in _tesseract_search_bases(root):
        if sys.platform == "win32":
            p = base / "tesseract.exe"
            if p.is_file():
                return p
            continue
        # POSIX dev install — try ``bin/tesseract`` then bare ``tesseract``.
        p = base / "bin" / "tesseract"
        if p.is_file():
            return p
        p2 = base / "tesseract"
        if p2.is_file():
            return p2
    return None


def bundled_tessdata_dir(root: Path | None = None) -> Path | None:
    """First ``tessdata/`` directory we find under one of the supported
    layouts, or ``None`` if neither is present.

    Tesseract's ``TESSDATA_PREFIX`` env var must point at the folder
    that *contains* the ``*.traineddata`` files (its docs are
    misleading on this point — it asks for the parent in error text
    but actually wants this directory).
    """
    root = root or application_root()
    for base in _tesseract_search_bases(root):
        td = (base / "tessdata").resolve()
        if td.is_dir():
            return td
    return None


def configure_bundled_tesseract(root: Path | None = None) -> Path | None:
    """Point pytesseract at vendored ``tesseract`` if present. Returns exe path or None."""
    global _configured
    root = root or application_root()
    exe = bundled_tesseract_exe(root)
    if exe is None:
        return None

    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = str(exe.resolve())
    tess = bundled_tessdata_dir(root)
    if tess is not None:
        os.environ["TESSDATA_PREFIX"] = str(tess)
    _configured = True
    return exe


def is_configured() -> bool:
    return _configured
