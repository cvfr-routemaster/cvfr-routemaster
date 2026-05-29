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
Modal instructions for chart georeferencing: when calibration is missing or invalidated.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)


class CalibrationInstructionDialog(QDialog):
    """Interactive prompt with steps and optional shortcuts to start calibrating."""

    CALIBRATE_NORTH = 1001
    CALIBRATE_SOUTH = 1002

    def __init__(
        self,
        parent,
        issues: list[str],
        *,
        show_north_button: bool,
        show_south_button: bool,
        n_anchors: int,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Chart calibration")
        self.setModal(True)
        self.resize(520, 420)

        root = QVBoxLayout(self)

        intro = QLabel(
            "<p><b>Why calibrate?</b> Routes and coordinates are aligned to the raster map only after "
            "you tie real lat/lon to several known points on each sheet you use.</p>"
            "<p><b>How:</b> open <i>Map Calibration Options…</i> from the toolbar and choose "
            "<i>Calibrate north map…</i> or <i>Calibrate south map…</i>. The app picks "
            f"<b>{n_anchors}</b> well-spread anchor waypoints from the database for that "
            "sheet (more anchors average out click error and tighten the fit), then:</p>"
            "<ol>"
            "<li>Read each prompt — it names the waypoint (ICAO and Hebrew name).</li>"
            "<li><b>Shift+click</b> the <b>center</b> of that waypoint’s triangle on the chart, in order "
            "(plain drag pans the map; plain wheel zooms in until the precision reticle — a yellow / blue "
            "concentric triangle bullseye — is roughly the same size as the chart triangle, then nudge "
            "until each printed edge sits in the blank gap between the two blue lines).</li>"
            "</ol>"
            "<p><b>Shared overlap anchors.</b> The last few clicks on each sheet are "
            "<i>shared overlap anchors</i> — waypoints inside the strip where the two sheets overlap. "
            "The app picks the same waypoints for both sheets, so you click each shared anchor "
            "<b>once on the north sheet and once on the south sheet</b> during their respective "
            "calibrations. That pins both calibrations to the same lat/lon across the seam, "
            "which is what keeps the satellite imagery aligned along it. The prompt tells you when "
            "a click is a shared overlap anchor.</p>"
            "<p><b>Sheet alignment is automatic.</b> As soon as both sheets are calibrated, the app "
            "runs a joint least-squares fit over both per-sheet affines and the south sheet's "
            "scale and position, anchored on the shared overlap clicks. The same lat/lon then lands "
            "at the same screen pixel on both sheets without any hand-tuning. The status bar reports "
            "the chart-on-chart and sat-stitch residuals (each typically a few pixels); if either "
            "warns that the residual is large, one of the overlap anchors was clicked off-centre and "
            "the offending sheet should be recalibrated.</p>"
            "<p><b>Important:</b> calibration is saved together with the auto-derived sheet layout. "
            "Alt+wheel (and Alt+Shift+wheel) still rescale the selected sheet as an escape hatch, "
            "but the old Alt+drag movement gesture was removed once the joint solver took over "
            "alignment — hand-dragging silently invalidates the math-optimal pose. If you do touch "
            "Alt+wheel or click <i>Reset map layout</i>, you must recalibrate so the joint solver "
            "re-runs.</p>"
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setOpenExternalLinks(False)
        root.addWidget(intro)

        if issues:
            root.addWidget(QLabel("<b>Needs attention:</b>"))
            for msg in issues:
                row = QLabel(f"• {msg}")
                row.setWordWrap(True)
                root.addWidget(row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        self._btn_north = QPushButton("Start — Calibrate north…")
        self._btn_north.setVisible(show_north_button)
        self._btn_north.clicked.connect(lambda: self.done(self.CALIBRATE_NORTH))
        btn_row.addWidget(self._btn_north)

        self._btn_south = QPushButton("Start — Calibrate south…")
        self._btn_south.setVisible(show_south_button)
        self._btn_south.clicked.connect(lambda: self.done(self.CALIBRATE_SOUTH))
        btn_row.addWidget(self._btn_south)

        root.addLayout(btn_row)

        footer = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        footer.rejected.connect(self.reject)
        close_btn = footer.button(QDialogButtonBox.StandardButton.Close)
        if close_btn:
            close_btn.setDefault(True)
        root.addWidget(footer)
