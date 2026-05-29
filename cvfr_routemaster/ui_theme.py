"""Application-wide dark theme (maps stay full color; only Qt chrome is styled)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from cvfr_routemaster.settings_store import FontSizes, default_font_sizes


def _stylesheet(sizes: FontSizes) -> str:
    """Build the full QSS stylesheet with the supplied font sizes
    baked into the three user-controlled selectors:

      * ``QTableView`` — both the waypoint table and the route
        table inherit this size. ``QHeaderView::section`` doesn't
        override it, so the column headers scale alongside the
        data rows (which is what the user expects when they bump
        "tables font size").
      * ``QLabel#routeText`` — the three labels stacked above the
        route table inside ``RoutePanel`` (ICAO Field 15 string,
        Hebrew paperwork string, totals summary). All three are
        tagged with ``objectName="routeText"``.
      * ``QLabel#mapHint`` — the three usage-hint labels (waypoint-
        table hint, map hint, route-panel hint). The label class
        also applies bright-white + asymmetric padding so the
        hints stay visually distinct from regular labels.

    Pulling this into a helper (rather than building the string
    inline in :func:`apply_dark_theme`) keeps the QSS body literal
    and ``re``/``str.replace``-free — the font sizes are the only
    things that interpolate, and they do so via f-string
    placeholders in obvious named positions.
    """
    return f"""
        QWidget {{
            background-color: #1e1e1e;
            color: #e8e8e8;
        }}
        QMainWindow, QDialog {{
            background-color: #252526;
        }}
        QToolBar {{
            background-color: #333333;
            border: none;
            spacing: 8px;
            padding: 4px;
        }}
        QToolBar QToolButton {{
            background-color: transparent;
            color: #e8e8e8;
            padding: 4px 10px;
        }}
        QToolBar QToolButton:hover {{
            background-color: #3d3d3d;
        }}
        QStatusBar {{
            background-color: #252526;
            color: #cccccc;
        }}
        QSplitter::handle {{
            background-color: #3c3c3c;
        }}
        QLineEdit, QSpinBox, QDoubleSpinBox {{
            background-color: #3c3c3c;
            border: 1px solid #555555;
            border-radius: 4px;
            padding: 4px 8px;
            selection-background-color: #264f78;
        }}
        QTableView {{
            background-color: #1e1e1e;
            alternate-background-color: #252526;
            gridline-color: #3d3d3d;
            selection-background-color: #264f78;
            selection-color: #ffffff;
            font-size: {sizes.table_px}px;
        }}
        /* Do not set `color` here — it overrides QStandardItem.setForeground (e.g. green Code links). */
        QTableView::item {{
            padding: 2px 6px;
        }}
        QHeaderView::section {{
            background-color: #333333;
            color: #e8e8e8;
            padding: 6px;
            border: 1px solid #444444;
        }}
        QCheckBox {{
            spacing: 8px;
        }}
        QCheckBox::indicator {{
            width: 16px;
            height: 16px;
        }}
        QProgressDialog {{
            background-color: #252526;
        }}
        QProgressBar {{
            border: 1px solid #555555;
            border-radius: 3px;
            text-align: center;
            background-color: #3c3c3c;
            color: #e8e8e8;
        }}
        QProgressBar::chunk {{
            background-color: #0e639c;
        }}
        QGraphicsView {{
            border: none;
            background-color: #121212;
        }}
        QLabel {{
            color: #e8e8e8;
        }}
        /*
         * Route-text cluster: the three labels stacked above the
         * route table (ICAO Field 15 string, Hebrew paperwork
         * string, totals summary). Tagged with
         * ``objectName="routeText"`` so this selector hits all
         * three with a single rule. Size user-controlled via the
         * "Font settings" menu.
         */
        QLabel#routeText {{
            font-size: {sizes.route_text_px}px;
        }}
        /*
         * Unified style for every instructional/hint label across the three
         * panes (route panel footer, map footer, waypoint table footer).
         *
         * - Bright white (#ffffff) replaces the previous muted #b0b0b0: on
         *   the dark theme background the muted variant read as disabled
         *   copy, which buried important interaction hints.
         * - Font size is user-controlled via the "Font settings" menu;
         *   the historic default (18 px) is preserved as the out-of-the-
         *   box value so first-launch rendering doesn't shift.
         * - Padding stays asymmetric (more above than below) so the hint
         *   floats just under its companion widget without crowding the
         *   pane edge.
         */
        QLabel#mapHint {{
            color: #ffffff;
            font-size: {sizes.hint_px}px;
            padding: 6px 4px 2px 4px;
        }}
        QMessageBox {{
            background-color: #252526;
        }}
        QPushButton {{
            background-color: #3c3c3c;
            border: 1px solid #555555;
            border-radius: 4px;
            padding: 6px 14px;
            min-width: 72px;
        }}
        QPushButton:hover {{
            background-color: #4a4a4a;
        }}
        QPushButton:pressed {{
            background-color: #2d2d2d;
        }}
        """


def apply_dark_theme(
    app: QApplication, font_sizes: FontSizes | None = None
) -> None:
    """Apply the dark theme + user-controlled font sizes.

    Args:
        app: The running ``QApplication``.
        font_sizes: Per-area font-size preferences. ``None`` falls
            back to :func:`cvfr_routemaster.settings_store.default_font_sizes`
            so callers that don't care about font customisation
            (e.g. one-shot test harnesses) don't have to thread a
            ``FontSizes`` instance through. The MainWindow passes
            the user's saved sizes at startup and re-calls this
            function with the new sizes whenever the Font Settings
            dialog accepts.

    Re-callable: ``QApplication.setStyleSheet`` replaces the entire
    sheet, and the QSS pipeline re-renders affected widgets without
    needing each widget to be repolished individually — so the
    "user changed font size, re-apply" path is just another call to
    this function.
    """
    # Ask Windows for a dark caption where supported (Qt 6.5+).
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    except (AttributeError, TypeError, RuntimeError):
        pass

    app.setStyle("Fusion")
    sizes = font_sizes if font_sizes is not None else default_font_sizes()
    app.setStyleSheet(_stylesheet(sizes))
