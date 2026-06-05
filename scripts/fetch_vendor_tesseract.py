"""
Download Tesseract OCR into vendor/tesseract for a self-contained Windows build.

Steps:
  1. Fetch Hebrew + English fast models into vendor/tesseract/tessdata/ (all platforms).
  2. On Windows only: download the UB Mannheim installer and run a silent install into vendor/tesseract.

Run from repository root:  py scripts/fetch_vendor_tesseract.py

Tesseract is licensed under Apache-2.0. UB Mannheim binaries: same stack as upstream Tesseract.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

# UB Mannheim Windows 64-bit installer (includes runtime DLLs and default tessdata).
_Windows_INSTALLER_URL = (
    "https://github.com/UB-Mannheim/tesseract/releases/download/"
    "v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe"
)
_TESSDATA_FAST = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"
_LANGS = ("eng", "heb")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading\n  {url}\n  -> {dest}")
    req = urllib.request.Request(url, headers={"User-Agent": "cvfr-routemaster-vendor-fetch"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        dest.write_bytes(resp.read())


def fetch_tessdata(install_dir: Path) -> None:
    td = install_dir / "tessdata"
    td.mkdir(parents=True, exist_ok=True)
    for lang in _LANGS:
        out = td / f"{lang}.traineddata"
        if out.is_file() and out.stat().st_size > 10_000:
            print(f"Skip existing {out.name}")
            continue
        _download(_TESSDATA_FAST.format(lang=lang), out)
        print(f"OK {out.name} ({out.stat().st_size // 1024} KiB)")


def fetch_windows_engine(target_dir: Path, *, cache_dir: Path) -> None:
    if sys.platform != "win32":
        return
    exe = target_dir / "tesseract.exe"
    if exe.is_file():
        print(f"Skip installer: {exe} already exists.")
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    installer = cache_dir / "tesseract-ocr-w64-setup.exe"
    if not installer.is_file():
        _download(_Windows_INSTALLER_URL, installer)
    target_dir = target_dir.resolve()
    # Inno Setup: install unpacked tree directly under target_dir (contains tesseract.exe, tessdata, DLLs).
    args = [
        str(installer),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CLOSEAPPLICATIONS",
        f"/DIR={target_dir}",
    ]
    print("Running silent installer (may take a minute, may prompt for elevation)...")
    subprocess.run(args, check=True)
    if not exe.is_file():
        raise SystemExit(
            f"Expected {exe} after install. If silent install failed, install manually from:\n"
            f"  {_Windows_INSTALLER_URL}\n"
            f"and choose directory:\n  {target_dir}"
        )
    print(f"Engine installed to {target_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only-tessdata",
        action="store_true",
        help="Only download eng/heb traineddata (no Windows installer).",
    )
    args = parser.parse_args()

    root = _project_root()
    install_dir = root / "vendor" / "tesseract"
    cache_dir = root / "vendor" / "cache"

    if args.only_tessdata:
        fetch_tessdata(install_dir)
        print("Done (--only-tessdata).")
        return

    if sys.platform != "win32":
        fetch_tessdata(install_dir)
        print(
            "Non-Windows: tessdata saved under vendor/tesseract/tessdata/. "
            "Install tesseract-ocr + Hebrew via your package manager, or place the tesseract binary under vendor/tesseract/."
        )
        return

    if shutil.which("tesseract") is None:
        print("Note: tesseract not on PATH; installing bundled engine into vendor/tesseract.")

    fetch_windows_engine(install_dir, cache_dir=cache_dir)
    fetch_tessdata(install_dir)
    print()
    print("Verify:")
    print(rf'  "{install_dir}\tesseract.exe" --list-langs')
    print("You should see eng and heb.")


if __name__ == "__main__":
    main()
