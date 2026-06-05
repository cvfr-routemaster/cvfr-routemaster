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
Background worker that extracts altitude arrows for both chart sheets.

Extraction takes ~30 s per sheet on a cold cache (PyMuPDF walks ~150 k vector
drawings on the north chart) so it must not run on the GUI thread; on a warm
cache it's <50 ms but we still keep it off the GUI thread for symmetry.

The worker uses :func:`cvfr_routemaster.altitude_cache.try_load_altitude_arrows`
first, only falling back to :func:`extract_altitude_arrows` on a miss. After a
successful fresh extraction it persists the result via
:func:`save_altitude_arrows`, so the next launch hits the cache. The cache
fingerprint is keyed on the PDF + the *exact* render parameters
(``render_dpi`` and the per-sheet :class:`CropMeta`) — different rendering
yields different pixmap UV coordinates and would silently misplace arrows
without that gate.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from cvfr_routemaster.altitude_arrows import AltitudeArrow, extract_altitude_arrows
from cvfr_routemaster.altitude_cache import save_altitude_arrows, try_load_altitude_arrows
from cvfr_routemaster.map_crop import CropMeta


class AltitudeArrowsWorker(QObject):
    """One-shot worker that extracts altitude arrows for both chart sheets.

    Emits exactly one of:

    * ``finished(north_arrows, south_arrows)`` — both sheets succeeded. Each
      list is cropped-pixmap-UV :class:`AltitudeArrow` records, ready for
      projection through the per-sheet :class:`SheetGeoCalibration`.
    * ``failed(message)`` — a per-sheet failure that prevented either list
      from being built; the GUI surfaces this in the status bar.

    The two sheets are extracted in parallel via :class:`ThreadPoolExecutor`
    because PyMuPDF releases the GIL for its PDF parsing — same trick the
    map loader uses to halve cold-render time.
    """

    finished = Signal(list, list)  # (north_arrows, south_arrows)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(
        self,
        north_path: str | Path,
        south_path: str | Path,
        *,
        project_root: Path,
        north_render_dpi: float,
        north_crop: CropMeta,
        south_render_dpi: float,
        south_crop: CropMeta,
        mode_id: str | None = None,
    ) -> None:
        super().__init__()
        self._north_path = Path(north_path)
        self._south_path = Path(south_path)
        self._project_root = project_root
        self._north_render_dpi = float(north_render_dpi)
        self._north_crop = north_crop
        self._south_render_dpi = float(south_render_dpi)
        self._south_crop = south_crop
        self._mode_id = mode_id

    def _load_or_extract(
        self,
        pdf: Path,
        sheet: str,
        render_dpi: float,
        crop: CropMeta,
    ) -> list[AltitudeArrow]:
        cached = try_load_altitude_arrows(
            self._project_root, pdf, sheet, render_dpi=render_dpi, crop=crop,
            mode_id=self._mode_id,
        )
        if cached is not None:
            self.progress.emit(f"Loaded altitude arrows for {sheet} from cache.")
            return cached

        self.progress.emit(f"Extracting altitude arrows from {sheet} chart...")
        arrows = extract_altitude_arrows(pdf, render_dpi=render_dpi, crop=crop)
        save_altitude_arrows(
            self._project_root, pdf, sheet, arrows, render_dpi=render_dpi, crop=crop,
            mode_id=self._mode_id,
        )
        return arrows

    def run(self) -> None:
        try:
            if not self._north_path.is_file() or not self._south_path.is_file():
                self.failed.emit(
                    "Altitude extraction skipped: north or south PDF not found."
                )
                return

            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_n = pool.submit(
                    self._load_or_extract,
                    self._north_path,
                    "north",
                    self._north_render_dpi,
                    self._north_crop,
                )
                fut_s = pool.submit(
                    self._load_or_extract,
                    self._south_path,
                    "south",
                    self._south_render_dpi,
                    self._south_crop,
                )
                north = fut_n.result()
                south = fut_s.result()

            self.finished.emit(list(north), list(south))
        except Exception as exc:  # noqa: BLE001
            # PyMuPDF can throw a variety of fitz / OS errors; surface the
            # message so the user sees what went wrong rather than a silent
            # "altitude column shows unknown for everything".
            self.failed.emit(f"Altitude extraction failed: {exc}")
