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

"""Crop rendered chart tiles toward the map panel (drop margins and pale bands).

Two flavours of the public crop are exposed:

* :func:`crop_chart_white_margins` — convenience wrapper, returns just the
  cropped ``QImage``. Used by the map worker to feed the on-screen pixmap.

* :func:`crop_chart_white_margins_with_meta` — returns ``(QImage, CropMeta)``
  where ``CropMeta`` records the offset and source/cropped dimensions. The
  altitude-arrow extractor needs this to convert PDF-page coordinates into the
  same normalised UV space the geo calibration anchors live in (the cropped
  pixmap, *not* the raw rendered page).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtGui import QImage


@dataclass(frozen=True)
class CropMeta:
    """Geometry recovered from a single :func:`crop_chart_white_margins` call.

    ``offset_x``/``offset_y`` are the top-left pixel of the cropped image
    expressed in the *source* image's coordinate system, and
    ``cropped_w``/``cropped_h`` are the cropped image's dimensions. Together
    with the source dimensions they give the linear map that takes a pixel in
    the rendered (pre-crop) pixmap to a normalised UV coordinate in the
    calibrated (post-crop) pixmap::

        u = (px_src - offset_x) / cropped_w
        v = (py_src - offset_y) / cropped_h

    A :class:`CropMeta` with ``offset_x == 0``, ``offset_y == 0``, and
    ``source_*`` equal to ``cropped_*`` is the identity (no crop), which is
    what we return when the image was too small to crop or no margins were
    detected. That keeps callers' projection math branch-free.
    """

    offset_x: int
    offset_y: int
    source_w: int
    source_h: int
    cropped_w: int
    cropped_h: int


def _rgb888_to_gray_f32(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    buf = bytes(img.constBits()[: bpl * h])
    arr = np.frombuffer(buf, dtype=np.uint8).reshape((h, bpl))
    rgb = arr[:, : w * 3].reshape((h, w, 3))
    return (
        0.299 * rgb[:, :, 0].astype(np.float32)
        + 0.587 * rgb[:, :, 1].astype(np.float32)
        + 0.114 * rgb[:, :, 2].astype(np.float32)
    )


def _strip_paper_mean_edges_once(
    gray: np.ndarray,
    *,
    pale_min: float,
    pale_row_frac: float,
    min_edge_mean: float,
) -> tuple[int, int, int, int] | None:
    """One pass: crop to rows/columns that are not “mostly blank paper” edges."""
    h, w = gray.shape
    if h < 2 or w < 2:
        return None

    pale_frac_r = np.mean(gray >= pale_min, axis=1)
    mean_r = np.mean(gray, axis=1)
    margin_r = (pale_frac_r >= pale_row_frac) & (mean_r >= min_edge_mean)

    pale_frac_c = np.mean(gray >= pale_min, axis=0)
    mean_c = np.mean(gray, axis=0)
    margin_c = (pale_frac_c >= pale_row_frac) & (mean_c >= min_edge_mean)

    if margin_r.all() or margin_c.all():
        return None

    content_r = ~margin_r
    if not content_r.any():
        return None
    y0 = int(np.argmax(content_r))
    y1 = h - 1 - int(np.argmax(content_r[::-1]))

    content_c = ~margin_c
    if not content_c.any():
        return None
    x0 = int(np.argmax(content_c))
    x1 = w - 1 - int(np.argmax(content_c[::-1]))

    if y1 < y0 or x1 < x0:
        return None
    return y0, y1, x0, x1


def _strip_bright_edges_once(
    gray: np.ndarray,
    *,
    bright_mean: float,
) -> tuple[int, int, int, int] | None:
    """One pass: crop away edges whose mean luminance is still ≥ bright_mean."""
    h, w = gray.shape
    if h < 2 or w < 2:
        return None

    mean_r = np.mean(gray, axis=1)
    mean_c = np.mean(gray, axis=0)
    dark_r = mean_r < bright_mean
    dark_c = mean_c < bright_mean

    if not dark_r.any() or not dark_c.any():
        return None

    y0 = int(np.argmax(dark_r))
    y1 = h - 1 - int(np.argmax(dark_r[::-1]))
    x0 = int(np.argmax(dark_c))
    x1 = w - 1 - int(np.argmax(dark_c[::-1]))

    if y1 < y0 or x1 < x0:
        return None
    return y0, y1, x0, x1


def _max_run_dark_1d(samples: np.ndarray, thresh: float) -> int:
    """Longest consecutive run of pixels darker than ``thresh``."""
    m = (samples < float(thresh)).astype(np.bool_)
    if not np.any(m):
        return 0
    padded = np.concatenate((np.array([False]), m, np.array([False])))
    d = np.diff(padded.astype(np.int8))
    starts = np.where(d == 1)[0]
    ends = np.where(d == -1)[0]
    if starts.size == 0:
        return int(m.size)
    return int(np.max(ends - starts))


def _detect_black_frame_edges(
    gray: np.ndarray,
    *,
    dark_thresh: float,
    min_run_frac: float,
    scan_frac: float,
) -> tuple[int, int, int, int] | None:
    """
    Find axis-aligned guide lines that match long horizontal/vertical black segments
    (chart outer frame). Returns (top_y, bottom_y, left_x, right_x) line centers/rows.
    """
    h, w = gray.shape
    if h < 32 or w < 32:
        return None

    y_band = max(8, int(h * scan_frac))
    x_band = max(8, int(w * scan_frac))
    min_run_y = max(32, int(w * min_run_frac))
    min_run_x = max(32, int(h * min_run_frac))

    top_y = None
    for y in range(min(y_band, h)):
        if _max_run_dark_1d(gray[y], dark_thresh) >= min_run_y:
            top_y = y
            break

    bottom_y = None
    for y in range(h - 1, max(h - y_band, -1), -1):
        if _max_run_dark_1d(gray[y], dark_thresh) >= min_run_y:
            bottom_y = y
            break

    left_x = None
    for x in range(min(x_band, w)):
        if _max_run_dark_1d(gray[:, x], dark_thresh) >= min_run_x:
            left_x = x
            break

    right_x = None
    for x in range(w - 1, max(w - x_band, -1), -1):
        if _max_run_dark_1d(gray[:, x], dark_thresh) >= min_run_x:
            right_x = x
            break

    if None in (top_y, bottom_y, left_x, right_x):
        return None
    if bottom_y <= top_y or right_x <= left_x:
        return None
    return top_y, bottom_y, left_x, right_x


def crop_to_black_chart_frame_with_offset(
    img: QImage,
    *,
    dark_thresh: float = 72.0,
    min_run_frac: float = 0.32,
    scan_frac: float = 0.18,
    inset_px: int = 6,
) -> tuple[QImage, int, int]:
    """Inner-frame crop returning ``(cropped, dx, dy)``.

    ``dx`` / ``dy`` are the new image's top-left pixel position in the input
    image's coordinate system. Returns ``(img, 0, 0)`` when the frame cannot
    be found or the inset would collapse the image, so callers can compose
    multiple cropping passes with simple integer addition.
    """
    if img.isNull():
        return img, 0, 0
    hi, wi = img.height(), img.width()
    if hi < 32 or wi < 32:
        return img, 0, 0

    gray = _rgb888_to_gray_f32(img)
    edges = _detect_black_frame_edges(
        gray,
        dark_thresh=dark_thresh,
        min_run_frac=min_run_frac,
        scan_frac=scan_frac,
    )
    if edges is None:
        for dt_extra, relaxed in ((10.0, 0.26), (18.0, 0.20)):
            edges = _detect_black_frame_edges(
                gray,
                dark_thresh=dark_thresh + dt_extra,
                min_run_frac=relaxed,
                scan_frac=min(0.24, scan_frac + 0.05),
            )
            if edges is not None:
                break
    if edges is None:
        return img, 0, 0

    top_y, bottom_y, left_x, right_x = edges
    iy0 = top_y + inset_px
    iy1 = bottom_y - inset_px
    ix0 = left_x + inset_px
    ix1 = right_x - inset_px

    if iy1 <= iy0 or ix1 <= ix0:
        return img, 0, 0
    cw, ch = ix1 - ix0 + 1, iy1 - iy0 + 1
    if cw < wi * 0.15 or ch < hi * 0.15:
        return img, 0, 0

    out = img.copy(ix0, iy0, cw, ch)
    if out.isNull():
        return img, 0, 0
    return out, ix0, iy0


def crop_to_black_chart_frame(
    img: QImage,
    *,
    dark_thresh: float = 72.0,
    min_run_frac: float = 0.32,
    scan_frac: float = 0.18,
    inset_px: int = 6,
) -> QImage:
    """Same as :func:`crop_to_black_chart_frame_with_offset` but discards the offset."""
    out, _, _ = crop_to_black_chart_frame_with_offset(
        img,
        dark_thresh=dark_thresh,
        min_run_frac=min_run_frac,
        scan_frac=scan_frac,
        inset_px=inset_px,
    )
    return out


def _nonwhite_bbox(
    gray: np.ndarray,
    *,
    cutoff: float,
) -> tuple[int, int, int, int] | None:
    mask = gray <= float(cutoff)
    if not np.any(mask):
        return None
    ys, xs = np.where(mask)
    return int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())


def crop_chart_white_margins_with_meta(
    img: QImage,
    *,
    pale_min: float = 250.0,
    pale_row_frac: float = 0.84,
    min_edge_mean: float = 244.0,
    bright_mean: float = 244.0,
    nonwhite_cutoff: float = 252.5,
    max_rounds: int = 8,
) -> tuple[QImage, CropMeta]:
    """Crop a rendered chart and return both the cropped image and its
    :class:`CropMeta`.

    The two-stage pipeline is unchanged from the legacy
    :func:`crop_chart_white_margins`: alternate mostly-paper edge stripping
    and mean-brightness peeling, finalize with a non-white bbox, then refine
    via :func:`crop_to_black_chart_frame_with_offset` if a printed frame is
    detected. The wrapper threads each stage's offset back through to the
    caller so altitude-arrow extraction can reverse the crop later.

    When the image is too small or no margins are detected, returns the input
    image with an *identity* :class:`CropMeta` (zero offset, source = cropped)
    — the caller's projection math then short-circuits to the trivial case.
    """
    src_w = max(0, int(img.width()))
    src_h = max(0, int(img.height()))
    identity = CropMeta(
        offset_x=0,
        offset_y=0,
        source_w=src_w,
        source_h=src_h,
        cropped_w=src_w,
        cropped_h=src_h,
    )

    if img.isNull() or src_w < 16 or src_h < 16:
        return img, identity

    gray = _rgb888_to_gray_f32(img)
    oy, ox = 0, 0
    cur = gray

    for _ in range(max_rounds):
        changed = False

        b = _strip_paper_mean_edges_once(
            cur,
            pale_min=pale_min,
            pale_row_frac=pale_row_frac,
            min_edge_mean=min_edge_mean,
        )
        if b is None:
            return img, identity
        py0, py1, px0, px1 = b
        if py0 != 0 or py1 != cur.shape[0] - 1 or px0 != 0 or px1 != cur.shape[1] - 1:
            oy += py0
            ox += px0
            cur = cur[py0 : py1 + 1, px0 : px1 + 1]
            changed = True
            if cur.shape[0] < 8 or cur.shape[1] < 8:
                return img, identity

        b2 = _strip_bright_edges_once(cur, bright_mean=bright_mean)
        if b2 is None:
            break
        by0, by1, bx0, bx1 = b2
        if by0 != 0 or by1 != cur.shape[0] - 1 or bx0 != 0 or bx1 != cur.shape[1] - 1:
            oy += by0
            ox += bx0
            cur = cur[by0 : by1 + 1, bx0 : bx1 + 1]
            changed = True
            if cur.shape[0] < 8 or cur.shape[1] < 8:
                return img, identity

        if not changed:
            break

    inner = _nonwhite_bbox(cur, cutoff=nonwhite_cutoff)
    if inner is None:
        return img, identity
    iy0, iy1, ix0, ix1 = inner

    y0r = oy + iy0
    y1r = oy + iy1
    x0r = ox + ix0
    x1r = ox + ix1

    cw, ch = x1r - x0r + 1, y1r - y0r + 1
    if cw < src_w * 0.05 or ch < src_h * 0.05:
        return img, identity

    out = img.copy(x0r, y0r, cw, ch)
    if out.isNull():
        return img, identity

    out2, dx2, dy2 = crop_to_black_chart_frame_with_offset(out)
    if out2.isNull():
        return out, CropMeta(
            offset_x=int(x0r),
            offset_y=int(y0r),
            source_w=src_w,
            source_h=src_h,
            cropped_w=int(cw),
            cropped_h=int(ch),
        )
    return out2, CropMeta(
        offset_x=int(x0r) + int(dx2),
        offset_y=int(y0r) + int(dy2),
        source_w=src_w,
        source_h=src_h,
        cropped_w=int(out2.width()),
        cropped_h=int(out2.height()),
    )


def crop_chart_white_margins(
    img: QImage,
    *,
    pale_min: float = 250.0,
    pale_row_frac: float = 0.84,
    min_edge_mean: float = 244.0,
    bright_mean: float = 244.0,
    nonwhite_cutoff: float = 252.5,
    max_rounds: int = 8,
) -> QImage:
    """Crop each sheet toward the printed map (image-only convenience wrapper)."""
    out, _ = crop_chart_white_margins_with_meta(
        img,
        pale_min=pale_min,
        pale_row_frac=pale_row_frac,
        min_edge_mean=min_edge_mean,
        bright_mean=bright_mean,
        nonwhite_cutoff=nonwhite_cutoff,
        max_rounds=max_rounds,
    )
    return out


def crop_south_top_white_margin(
    img: QImage,
    *,
    search_frac: float = 0.5,
    max_trim_frac: float = 0.28,
) -> QImage:
    """Legacy name; same as :func:`crop_chart_white_margins`."""
    _ = (search_frac, max_trim_frac)
    return crop_chart_white_margins(img)
