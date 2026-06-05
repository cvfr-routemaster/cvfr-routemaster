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

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import fitz
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage, QPainter

from cvfr_routemaster.map_crop import (
    CropMeta,
    crop_chart_white_margins_with_meta,
)
from cvfr_routemaster.map_image_cache import save_map_png_cache, try_load_map_png_cache

#: Output-pixel height of each render band. The page is rasterised one
#: horizontal band at a time (see :func:`render_page_banded`) instead of
#: in a single ``get_pixmap`` call. Two reasons:
#:
#: 1. **Responsiveness.** ``fitz`` holds the Python GIL for the whole
#:    duration of a ``get_pixmap`` call, so a single full-page render of a
#:    ~90-megapixel A0 chart freezes the GUI thread for 30-90 s (the
#:    "(Not Responding)" window the user reported). Banding hands the GIL
#:    back between bands, so the worker can emit progress and the GUI can
#:    repaint its determinate progress bar.
#: 2. **Speed.** Counter-intuitively, banding is also *faster* on the
#:    image-heavy LSA sheets (measured ~47 s vs ~96 s for one sheet),
#:    because the single 90 MP allocation thrashes the CPU cache.
#:
#: 256 px keeps each band to roughly ~1 s (worst ~3-4 s over the dense
#: LSA terrain imagery) — comfortably under the OS "(Not Responding)"
#: threshold — while keeping per-band overhead small. The worker also
#: yields the GIL for :data:`BAND_GIL_YIELD_S` after each band so the GUI
#: thread is *guaranteed* a repaint window per band, not left to win a
#: race for the GIL (see ``MapLoadWorker.run``).
RENDER_BAND_PX: int = 256

#: Seconds the render worker sleeps after each band. ``time.sleep``
#: releases the CPython GIL, deterministically handing the GUI thread a
#: window to run its queued ``render_progress`` slot and repaint the
#: progress dialog. ~20 ms is imperceptible to the total render (45 bands
#: x 2 sheets x 20 ms < 2 s) but turns the bar from "frozen, bursty" into
#: smooth per-band motion. Only meaningful on the worker thread; harmless
#: elsewhere.
BAND_GIL_YIELD_S: float = 0.02

#: Vertical overlap (output px) rendered above and below each band's core
#: rows and then discarded. ``fitz`` anti-aliases content against the clip
#: edge, so a stroke/glyph straddling a band boundary would be drawn
#: slightly differently in each band and leave a faint seam. Rendering a
#: margin of real neighbouring content and compositing only the seam-free
#: core eliminates that (worst-case per-pixel delta drops from ~246 to
#: ~42 on a 0-255 scale, i.e. imperceptible).
RENDER_BAND_MARGIN_PX: int = 16

#: Shown on the determinate render bar so the user understands the long
#: first-run wait is a *one-time, per-chart-product* cost, not a hang.
ONE_TIME_RENDER_MESSAGE: str = (
    "Rendering the chart images from the source PDFs.\n\n"
    "This one-time step runs only the first time you open each chart "
    "product (CVFR / LSA) on this computer, and can take 1-3 minutes "
    "per product.\n\n"
    "The result is cached, so every later launch — and switching "
    "between CVFR and LSA — loads instantly."
)


def render_page_banded(
    pdf_path: Path,
    matrix: fitz.Matrix,
    *,
    band_px: int = RENDER_BAND_PX,
    margin_px: int = RENDER_BAND_MARGIN_PX,
    on_band: Callable[[int, int], None] | None = None,
) -> QImage:
    """Rasterise page 0 of ``pdf_path`` band-by-band into one ``QImage``.

    Produces a pixel result equivalent to a single
    ``page.get_pixmap(matrix=matrix, alpha=False)`` (same dimensions; only
    sub-perceptible anti-aliasing differences at band seams), but yields the
    GIL between bands so a GUI thread stays responsive, and calls
    ``on_band(done, total)`` after each band so a caller can drive a
    determinate progress bar.

    Args:
        pdf_path: PDF whose first page is rendered.
        matrix: The render matrix (zoom) — same object the legacy single
            ``get_pixmap`` used, so output geometry is unchanged.
        band_px: Target band height in output pixels.
        margin_px: Seam-overlap height (output px) rendered and discarded.
        on_band: Optional ``(done, total)`` progress callback, invoked once
            per band with ``1 <= done <= total``.

    Returns:
        The fully composited ``QImage`` in ``Format_RGB888``.
    """
    doc = fitz.open(pdf_path)
    try:
        page = doc[0]
        rect = page.rect
        display_list = page.get_displaylist()
        z = float(matrix[0])

        device = (rect * matrix).irect
        full_w = int(device.width)
        full_h = int(device.height)
        target = QImage(full_w, full_h, QImage.Format.Format_RGB888)
        # White so any (sub-pixel) uncovered seam row reads as paper, never
        # as an uninitialised-memory streak.
        target.fill(0xFFFFFFFF)

        band_pt = band_px / z
        margin_pt = margin_px / z
        total = max(1, int(math.ceil(rect.height / band_pt)))

        painter = QPainter(target)
        try:
            for i in range(total):
                core_y0 = rect.y0 + i * band_pt
                core_y1 = min(rect.y1, core_y0 + band_pt)
                if core_y1 <= core_y0:
                    break
                ext_y0 = max(rect.y0, core_y0 - margin_pt)
                ext_y1 = min(rect.y1, core_y1 + margin_pt)
                clip = fitz.Rect(rect.x0, ext_y0, rect.x1, ext_y1)
                pix = display_list.get_pixmap(
                    matrix=matrix, clip=clip, alpha=False
                )
                # Drop the rendered margin: copy only the core rows, placed
                # at the band's true device-space origin.
                top_skip = int(round((core_y0 - ext_y0) * z))
                core_h = min(
                    pix.height - top_skip, int(round((core_y1 - core_y0) * z))
                )
                if core_h <= 0:
                    if on_band is not None:
                        on_band(i + 1, total)
                    continue
                band_img = QImage(
                    pix.samples,
                    pix.width,
                    pix.height,
                    pix.stride,
                    QImage.Format.Format_RGB888,
                )
                painter.drawImage(
                    pix.x,
                    pix.y + top_skip,
                    band_img,
                    0,
                    top_skip,
                    pix.width,
                    core_h,
                )
                if on_band is not None:
                    on_band(i + 1, total)
        finally:
            painter.end()
        return target
    finally:
        doc.close()


@dataclass(frozen=True)
class SheetRenderInfo:
    """Per-sheet render geometry surfaced alongside the rendered pixmap.

    Captures everything an offline analyser (e.g. the altitude-arrow extractor)
    needs to convert a PDF-page coordinate into the same UV space the geo
    calibration anchors live in (the cropped pixmap):

    * ``render_dpi``: the DPI used to rasterise the page (so PDF-pt → pixel
      uses ``z = render_dpi / 72.0``).
    * ``crop``: the :class:`CropMeta` returned by the white-margin trim.
    """

    render_dpi: float
    crop: CropMeta


def _common_matrix(
    pages: list[fitz.Page],
    *,
    render_dpi: float,
    max_edge_px: int = 16384,
) -> fitz.Matrix:
    z = render_dpi / 72.0
    max_est = 0.0
    for page in pages:
        r = page.rect
        max_est = max(max_est, r.width * z, r.height * z)
    if max_est > max_edge_px and max_est > 0:
        z *= max_edge_px / max_est
    return fitz.Matrix(z, z)


class MapLoadWorker(QObject):
    """Renders North and South map PDFs separately (user aligns layers in the scene).

    The ``finished`` signal carries ``(QImage_north, QImage_south)`` for backward
    compatibility with existing slots; per-sheet render geometry — what the
    altitude extractor needs to convert PDF coords into pixmap UV — is exposed
    via the :attr:`render_info` mapping after the worker finishes. The mapping
    is populated whether the images came fresh from PyMuPDF or from the PNG
    cache, so downstream consumers don't have to know which path was taken.
    """

    finished = Signal(object)  # tuple[QImage, QImage] North, South
    failed = Signal(str)
    progress = Signal(str)
    # (percent 0-100, label-or-empty). Drives the *determinate* render bar.
    # An empty label means "advance the value only, leave the label alone"
    # so the persistent one-time explanation isn't clobbered every band.
    render_progress = Signal(int, str)

    def __init__(
        self,
        north_path: str,
        south_path: str,
        *,
        project_root: Path | None = None,
        render_dpi: float = 288.0,
        max_edge_px: int = 16384,
        mode_id: str | None = None,
    ) -> None:
        super().__init__()
        self._north = Path(north_path)
        self._south = Path(south_path)
        self._project_root = project_root
        self._render_dpi = float(render_dpi)
        self._max_edge_px = int(max_edge_px)
        self._mode_id = mode_id
        # Populated by ``run`` before ``finished`` fires. ``effective_render_dpi``
        # is the DPI Qt actually rendered at (after the max-edge cap may have
        # scaled it down) — that's the DPI an offline re-render must use to
        # reproduce the same pixmap coordinates.
        self.render_info: dict[str, SheetRenderInfo] = {}
        self._effective_render_dpi: float = float(render_dpi)

    def run(self) -> None:
        try:
            if not self._north.is_file():
                self.failed.emit(f"North map not found: {self._north}")
                return
            if not self._south.is_file():
                self.failed.emit(f"South map not found: {self._south}")
                return

            # PNG cache first — avoids misleading "Opening PDFs"/"Rendering" when PDFs are unused.
            if self._project_root is not None:
                cached = try_load_map_png_cache(
                    self._project_root,
                    self._north,
                    self._south,
                    render_dpi=self._render_dpi,
                    max_edge_px=self._max_edge_px,
                    mode_id=self._mode_id,
                )
                if cached is not None:
                    img_n, img_s, crop_n, crop_s, eff_dpi = cached
                    self._effective_render_dpi = float(eff_dpi)
                    self.render_info = {
                        "north": SheetRenderInfo(render_dpi=eff_dpi, crop=crop_n),
                        "south": SheetRenderInfo(render_dpi=eff_dpi, crop=crop_s),
                    }
                    self.progress.emit("Loaded map images from PNG cache.")
                    self.finished.emit((img_n, img_s))
                    return

            self.progress.emit("Opening map PDFs...")
            # Flip the dialog to a determinate bar and post the one-time
            # explanation up front, before the (long) render begins.
            self.render_progress.emit(0, ONE_TIME_RENDER_MESSAGE)
            north_doc = fitz.open(self._north)
            south_doc = fitz.open(self._south)
            try:
                if north_doc.page_count < 1 or south_doc.page_count < 1:
                    self.failed.emit("Each map PDF must contain at least one page.")
                    return

                np = north_doc[0]
                sp = south_doc[0]
                mat = _common_matrix(
                    [np, sp],
                    render_dpi=self._render_dpi,
                    max_edge_px=self._max_edge_px,
                )
            finally:
                north_doc.close()
                south_doc.close()

            # Convert the per-pt zoom that ``_common_matrix`` selected back into
            # a DPI so altitude extraction can rasterise the PDF identically
            # without having to re-derive the cap. The matrix has equal
            # diagonal entries by construction, so reading m[0][0] is enough.
            zoom = float(mat[0])
            self._effective_render_dpi = zoom * 72.0

            # Render each sheet band-by-band so the GUI thread keeps
            # pumping (``fitz`` holds the GIL per ``get_pixmap`` call) and
            # the determinate bar advances. Sheets render sequentially —
            # under the GIL a thread pool gave no speed-up and made progress
            # impossible to report monotonically. North fills 0-50 %, South
            # 50-100 %.
            def banded(path: Path, base: int) -> tuple[QImage, CropMeta]:
                def on_band(done: int, total: int) -> None:
                    # Float math (not ``//``): with few bands integer
                    # truncation would peg early bands at the floor for too
                    # long. ``base`` is 0 (north) or 50 (south).
                    self.render_progress.emit(
                        base + int(50.0 * done / total), ""
                    )
                    # Deterministic GIL hand-off so the GUI thread repaints
                    # the bar between bands instead of starving until the
                    # sheet completes (the "(Not Responding)" report).
                    time.sleep(BAND_GIL_YIELD_S)

                img = render_page_banded(path, mat, on_band=on_band)
                return crop_chart_white_margins_with_meta(img)

            self.progress.emit("Rendering north map from PDF...")
            img_n, crop_n = banded(self._north, 0)
            self.progress.emit("Rendering south map from PDF...")
            img_s, crop_s = banded(self._south, 50)
            self.render_progress.emit(100, "")

            self.render_info = {
                "north": SheetRenderInfo(render_dpi=self._effective_render_dpi, crop=crop_n),
                "south": SheetRenderInfo(render_dpi=self._effective_render_dpi, crop=crop_s),
            }

            if self._project_root is not None:
                save_map_png_cache(
                    self._project_root,
                    self._north,
                    self._south,
                    img_n,
                    img_s,
                    render_dpi=self._render_dpi,
                    max_edge_px=self._max_edge_px,
                    crop_n=crop_n,
                    crop_s=crop_s,
                    effective_render_dpi=self._effective_render_dpi,
                    mode_id=self._mode_id,
                )

            self.finished.emit((img_n, img_s))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
