"""
Map calibration options dialog.

Companion to :mod:`cvfr_routemaster.settings_dialog` — the *calibration* counterpart
to *Map File Settings*. Houses every command that's relevant only while the user
is wrangling the chart calibration / layout, so the main toolbar can stay tight
to the three things that matter once the map is loaded and aligned (file
settings, calibration options, CSV export).

The dialog also displays the calibration *instructions* directly on the screen
(not behind a button), so a curious user can read the explanation alongside the
action buttons without an extra click. The auto-prompt path for missing or
stale calibration still uses :class:`CalibrationInstructionDialog` — that's a
different UX (modal alert with a "start now" CTA), and conflating the two would
make the alert dialog awkward.

Action dispatch uses the same ``QDialog.done(code)`` pattern as
:class:`CalibrationInstructionDialog` so the controller has one familiar place
to switch on the result. Each action constant is a distinct return code in the
``1100+`` range (well clear of Qt's standard dialog codes and the 1001/1002
codes used by the instruction dialog). The controller calls the corresponding
slot via ``QTimer.singleShot(0, ...)`` so the dialog has fully closed before any
follow-up modal appears.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
)


class CalibrationOptionsDialog(QDialog):
    """Hub dialog for re-OCR, layout reset, calibrate, and clear-calibration actions.

    Parameters
    ----------
    parent
        The owning :class:`MainWindow` (used for modal parenting only).
    n_anchors
        Number of anchor waypoints the calibration flow will pick per sheet —
        surfaced in the on-screen instructions so the text matches what the
        user is about to see.
    """

    # Distinct return codes for each button so the caller can dispatch with a
    # single ``if code == ...`` chain. Values are deliberately well above
    # Qt's ``QDialog.Accepted`` / ``Rejected`` (0/1) and the instruction
    # dialog's 1001/1002 to avoid any future collision.
    ACTION_REOCR_WAYPOINTS = 1101
    ACTION_FIT_MAP = 1102
    ACTION_RESET_LAYOUT = 1103
    ACTION_CALIBRATE_NORTH = 1104
    ACTION_CALIBRATE_SOUTH = 1105
    ACTION_CLEAR_CALIBRATION = 1106

    def __init__(self, parent, *, n_anchors: int) -> None:
        super().__init__(parent)
        self.setWindowTitle("Map Calibration Options")
        self.setModal(True)
        self.resize(620, 640)

        root = QVBoxLayout(self)

        # On-screen instructions block — same wording as
        # CalibrationInstructionDialog's intro so a returning user gets the
        # same explanation in either entrypoint, but here it's always
        # visible (the user explicitly asked for the instructions to be
        # part of the dialog rather than gated behind another button).
        instructions = QLabel(
            "<p><b>What is calibration?</b> Calibration ties real-world latitude / "
            "longitude to pixels on each chart sheet, which is what lets the app "
            "draw your route on top of the map and snap clicks to nearby "
            "waypoints. Each sheet (north, south) is calibrated independently.</p>"
            "<p><b>How does it work?</b> When you start a calibration the app "
            f"picks <b>{n_anchors}</b> well-spread anchor waypoints from the "
            "database for that sheet (more anchors average out click error and "
            "tighten the fit), then walks you through each one:</p>"
            "<ol>"
            "<li>Read each prompt — it names the waypoint (ICAO and Hebrew "
            "name).</li>"
            "<li><b>Shift+click</b> the <b>center</b> of that waypoint's "
            "triangle on the chart, in order. Plain drag pans the map "
            "and the plain scroll wheel zooms in until the precision reticle "
            "— a yellow / blue concentric triangle bullseye — is roughly "
            "the same size as the chart triangle, then nudge until each "
            "printed edge sits in the blank gap between the two blue lines.</li>"
            "</ol>"
            "<p><b>Aligning the two sheets is automatic.</b> As soon as "
            "both sheets are calibrated, the app jointly LSQ-solves the "
            "two affine fits together with the south sheet's scale and "
            "position from the shared overlap anchors. The same lat/lon "
            "then lands at the same screen pixel on both sheets without "
            "any further input from you — no hand-dragging or eyeball "
            "alignment.</p>"
            "<p><b>Escape hatch (rare):</b> if you want to nudge the "
            "solver's result, the selected sheet can still be rescaled "
            "with <b>Alt+wheel</b> (and <b>Alt+Shift+wheel</b> for the "
            "very-fine ~0.05% per notch pass). The manual drag gesture "
            "was removed because hand-dragging silently invalidates the "
            "math-optimal pose.</p>"
            "<p><b>Important:</b> calibration is saved together with the "
            "sheet's <b>position and scale</b> on screen. If you resize "
            "a sheet via Alt+wheel or click <i>Reset map layout</i>, you "
            "must calibrate that sheet again so the joint solver re-runs.</p>"
        )
        instructions.setWordWrap(True)
        instructions.setTextFormat(Qt.TextFormat.RichText)
        instructions.setOpenExternalLinks(False)
        root.addWidget(instructions)

        # Buttons stack — vertical, one per row, full-width so the labels
        # and tooltips have room to breathe. Each button closes the dialog
        # with its own action code; the controller dispatches.
        #
        # Order roughly mirrors a typical workflow:
        #   1. Re-OCR (rebuild waypoint database from PDF).
        #   2. Fit map / Reset layout (chart positioning).
        #   3. Calibrate north / south.
        #   4. Clear calibration (the destructive "start over" path).
        action_specs: tuple[tuple[str, str, int], ...] = (
            (
                "Re-OCR waypoints from PDF",
                "Run full OCR on the back-pages PDF again, update the table, "
                "and refresh the on-disk cache. Use after you've replaced the "
                "back-pages PDF with an updated chart cycle.",
                self.ACTION_REOCR_WAYPOINTS,
            ),
            (
                "Fit map to view",
                "Zoom and pan the chart so both sheets are visible at once.",
                self.ACTION_FIT_MAP,
            ),
            (
                "Reset map layout",
                "Place the north sheet at the origin and the south sheet "
                "directly below it at 100% scale. Does not reload the PDFs. "
                "Calibration tied to the previous layout will be cleared.",
                self.ACTION_RESET_LAYOUT,
            ),
            (
                "Calibrate north map…",
                "Auto-picked anchor waypoints: follow the prompts and "
                "Shift+click each triangle center on the north chart. "
                "Saved with map layout; invalidated if the north PDF or its "
                "position/scale changes.",
                self.ACTION_CALIBRATE_NORTH,
            ),
            (
                "Calibrate south map…",
                "Auto-picked anchor waypoints: follow the prompts and "
                "Shift+click each triangle center on the south chart. "
                "Saved with map layout; invalidated if the south PDF or its "
                "position/scale changes.",
                self.ACTION_CALIBRATE_SOUTH,
            ),
            (
                "Clear geo calibration…",
                "Remove the saved north/south lat/lon mapping from disk for "
                "this project folder. You can recreate it with the "
                "Calibrate buttons above.",
                self.ACTION_CLEAR_CALIBRATION,
            ),
        )

        # Public ``_action_buttons`` mapping for tests so a future copy/paste
        # rename of a label doesn't quietly break a button — the action
        # constants are the contract, not the user-visible text.
        self._action_buttons: dict[int, QPushButton] = {}
        for label, tooltip, code in action_specs:
            btn = QPushButton(label)
            btn.setToolTip(tooltip)
            btn.clicked.connect(lambda _checked=False, c=code: self.done(c))
            root.addWidget(btn)
            self._action_buttons[code] = btn

        footer = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        footer.rejected.connect(self.reject)
        close_btn = footer.button(QDialogButtonBox.StandardButton.Close)
        if close_btn is not None:
            close_btn.setDefault(True)
        root.addWidget(footer)

    def button_for(self, action_code: int) -> QPushButton | None:
        """Return the ``QPushButton`` registered for ``action_code``, or ``None``.

        Useful for tests that want to invoke a specific action without relying
        on visible label text.
        """
        return self._action_buttons.get(action_code)
