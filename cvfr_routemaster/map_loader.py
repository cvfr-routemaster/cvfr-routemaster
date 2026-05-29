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

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import fitz
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from cvfr_routemaster.map_crop import (
    CropMeta,
    crop_chart_white_margins_with_meta,
)
from cvfr_routemaster.map_image_cache import save_map_png_cache, try_load_map_png_cache


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

    def __init__(
        self,
        north_path: str,
        south_path: str,
        *,
        project_root: Path | None = None,
        render_dpi: float = 288.0,
        max_edge_px: int = 16384,
    ) -> None:
        super().__init__()
        self._north = Path(north_path)
        self._south = Path(south_path)
        self._project_root = project_root
        self._render_dpi = float(render_dpi)
        self._max_edge_px = int(max_edge_px)
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

            def render_side(path: Path) -> tuple[QImage, CropMeta]:
                doc = fitz.open(path)
                try:
                    page = doc[0]
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    img = QImage(
                        pix.samples,
                        pix.width,
                        pix.height,
                        pix.stride,
                        QImage.Format.Format_RGB888,
                    ).copy()
                    return crop_chart_white_margins_with_meta(img)
                finally:
                    doc.close()

            self.progress.emit("Rendering north & south maps from PDF (parallel)...")
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_n = pool.submit(render_side, self._north)
                fut_s = pool.submit(render_side, self._south)
                img_n, crop_n = fut_n.result()
                img_s, crop_s = fut_s.result()

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
                )

            self.finished.emit((img_n, img_s))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
