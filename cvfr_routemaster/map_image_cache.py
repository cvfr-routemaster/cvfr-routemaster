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

"""
Disk cache for rendered north/south map images (PNG).

Invalidates when either map PDF path/size/mtime changes, or render settings / logic version change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtGui import QImage

from cvfr_routemaster.map_crop import CropMeta

# Bumped from 1 → 2: manifest now also stores per-sheet ``crop_meta`` (offset
# and source/cropped dimensions) so downstream consumers — in particular the
# altitude-arrow extractor — can reconstruct the PDF-pt → cropped-pixmap-UV
# transform without having to re-render the PDF on a cache hit.
CACHE_FORMAT_VERSION = 2
# Bump when crop logic, DPI defaults, or parallel render pipeline meaningfully changes.
MAP_RENDER_LOGIC_VERSION = 1


def _cache_dir(project_root: Path) -> Path:
    d = project_root / ".cvfr_routemaster"
    d.mkdir(exist_ok=True)
    return d


def _pdf_fp(path: Path) -> dict[str, Any]:
    p = path.resolve()
    st = p.stat()
    return {"path": str(p), "mtime_ns": st.st_mtime_ns, "size": st.st_size}


def _fp_match(cached: dict[str, Any], path: Path) -> bool:
    """Path-independent fingerprint check — see the equivalent helper in
    :mod:`cvfr_routemaster.altitude_cache` for the full rationale.
    Briefly: the cached ``path`` field is written for diagnostics but
    not compared, so a release zip that lands in a different absolute
    directory on the friend's machine still hits the bundled rendered-
    map PNG cache as long as the source PDF's bytes (and therefore its
    size + the mtime preserved by ``shutil.copy2`` / zip-extract on
    Windows) survived intact. Without this, every fresh install
    would re-render the chart PNGs at startup even though the
    bundled cache is correct.
    """
    if not path.is_file():
        return False
    cur = _pdf_fp(path)
    return (
        cached.get("mtime_ns") == cur["mtime_ns"]
        and cached.get("size") == cur["size"]
    )


def _crop_meta_to_dict(c: CropMeta) -> dict[str, int]:
    return {
        "offset_x": int(c.offset_x),
        "offset_y": int(c.offset_y),
        "source_w": int(c.source_w),
        "source_h": int(c.source_h),
        "cropped_w": int(c.cropped_w),
        "cropped_h": int(c.cropped_h),
    }


def _crop_meta_from_dict(d: Any) -> CropMeta | None:
    if not isinstance(d, dict):
        return None
    try:
        return CropMeta(
            offset_x=int(d["offset_x"]),
            offset_y=int(d["offset_y"]),
            source_w=int(d["source_w"]),
            source_h=int(d["source_h"]),
            cropped_w=int(d["cropped_w"]),
            cropped_h=int(d["cropped_h"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def try_load_map_png_cache(
    project_root: Path,
    north_path: str | Path,
    south_path: str | Path,
    *,
    render_dpi: float,
    max_edge_px: int,
) -> tuple[QImage, QImage, CropMeta, CropMeta, float] | None:
    """Return ``(img_n, img_s, crop_n, crop_s, effective_render_dpi)`` on a hit.

    ``effective_render_dpi`` is the DPI actually used during the original
    render — the same value the worker would compute now. It's persisted so
    altitude-arrow extraction (which has to re-open the PDF and reproduce
    PDF-pt → pixel) can reuse the exact rasterisation parameters even when
    a future code path bumps the requested DPI.

    Returns ``None`` whenever the manifest, PDFs, or PNGs are missing or
    don't match the current render parameters, which forces a fresh render.
    """
    np = Path(north_path)
    sp = Path(south_path)
    if not np.is_file() or not sp.is_file():
        return None

    base = _cache_dir(project_root)
    meta_path = base / "map_images_meta.json"
    png_n = base / "map_north.png"
    png_s = base / "map_south.png"
    if not meta_path.is_file() or not png_n.is_file() or not png_s.is_file():
        return None

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None

    if meta.get("cache_format_version") != CACHE_FORMAT_VERSION:
        return None
    if int(meta.get("map_render_logic_version", 0)) != MAP_RENDER_LOGIC_VERSION:
        return None
    if float(meta.get("render_dpi", 0)) != float(render_dpi):
        return None
    if int(meta.get("max_edge_px", -1)) != int(max_edge_px):
        return None

    cn = meta.get("north_pdf")
    cs = meta.get("south_pdf")
    if not isinstance(cn, dict) or not isinstance(cs, dict):
        return None
    if not _fp_match(cn, np) or not _fp_match(cs, sp):
        return None

    crop_n = _crop_meta_from_dict(meta.get("north_crop"))
    crop_s = _crop_meta_from_dict(meta.get("south_crop"))
    if crop_n is None or crop_s is None:
        return None

    try:
        eff_dpi = float(meta.get("effective_render_dpi", render_dpi))
    except (TypeError, ValueError):
        return None

    img_n = QImage(str(png_n))
    img_s = QImage(str(png_s))
    if img_n.isNull() or img_s.isNull():
        return None
    return img_n, img_s, crop_n, crop_s, eff_dpi


def save_map_png_cache(
    project_root: Path,
    north_path: str | Path,
    south_path: str | Path,
    img_n: QImage,
    img_s: QImage,
    *,
    render_dpi: float,
    max_edge_px: int,
    crop_n: CropMeta,
    crop_s: CropMeta,
    effective_render_dpi: float,
) -> None:
    """Write PNGs and manifest after a fresh render.

    ``crop_n``/``crop_s`` are the cropping metadata returned by
    :func:`crop_chart_white_margins_with_meta` for each sheet. They're
    persisted so a subsequent cache-hit run can reproduce the
    PDF-pt → cropped-pixmap-UV transform without re-rendering.
    """
    np = Path(north_path)
    sp = Path(south_path)
    if img_n.isNull() or img_s.isNull() or not np.is_file() or not sp.is_file():
        return

    base = _cache_dir(project_root)
    png_n = base / "map_north.png"
    png_s = base / "map_south.png"
    meta_path = base / "map_images_meta.json"

    if not img_n.save(str(png_n), "PNG") or not img_s.save(str(png_s), "PNG"):
        return

    payload = {
        "cache_format_version": CACHE_FORMAT_VERSION,
        "map_render_logic_version": MAP_RENDER_LOGIC_VERSION,
        "render_dpi": float(render_dpi),
        "effective_render_dpi": float(effective_render_dpi),
        "max_edge_px": int(max_edge_px),
        "north_pdf": _pdf_fp(np),
        "south_pdf": _pdf_fp(sp),
        "north_crop": _crop_meta_to_dict(crop_n),
        "south_crop": _crop_meta_to_dict(crop_s),
    }
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
