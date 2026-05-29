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

"""Modal dialog shown when a chart-PDF download fails.

User-visible contract
---------------------

Exactly one situation triggers this dialog: a URL source for one
of the three CVFR sheets (north / south / back) could not be
fetched. The dialog must give the user three actionable paths:

1. **Retry now** — most failures are transient (flaky Wi-Fi, CDN
   hiccup). Hitting Retry re-runs the same download immediately.
2. **Manual fallback** — the user opens the URL in a browser,
   downloads the PDF themselves, drops it at the expected cache
   path. The dialog must show BOTH the URL AND the expected
   cache path, with copy buttons, because asking users to
   remember either is a recipe for "the program just doesn't
   work" feedback.
3. **Cancel** — give up on this load attempt. The user can still
   open Map File Settings to point at a different URL / a local
   path.

Per the user's explicit instruction ("the modal needs to show
the links, people won't know to go to the copyright info to
obtain the links from there"), this dialog surfaces ALL three
chart URLs — not just the one that failed — so a user whose
network is partly broken (one URL blocked at the CDN tier,
others reachable) has every chart's URL handy without needing
to dismiss this dialog and re-open Settings.

What this dialog does NOT do
----------------------------

* No retry-then-continue control flow. The caller (typically
  ``main_window._ensure_chart_sources_resolved``) loops on
  ``exec()`` until it gets Cancel — this dialog just reports
  what the user picked.
* No download progress UI. That belongs to the
  :class:`QProgressDialog` already in use by ``_load_all`` and
  is updated via a callback from ``chart_source.download_chart_pdf``.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from cvfr_routemaster.chart_source import (
    CAAI_CHART_URLS,
    SHEET_DISPLAY_NAMES,
    SHEET_KEYS,
    cache_path_for_sheet,
)


# Custom return codes — distinct from QDialog's Accepted/Rejected so
# the controller can switch on three states (Retry / Cancel /
# OpenSettings) rather than just two. Same numerology pattern as
# ``CalibrationOptionsDialog`` (1100+) and ``SettingsDialog.LOAD_NOW``
# (1201) so dialog-dispatch code stays consistent.
ACTION_RETRY = 1301
ACTION_OPEN_SETTINGS = 1302


class ChartDownloadErrorDialog(QDialog):
    """Modal error dialog for chart-PDF download failures.

    Construction parameters identify which sheet failed and why;
    the dialog walks the rest from module constants (URLs from
    :data:`cvfr_routemaster.chart_source.CAAI_CHART_URLS`, cache
    paths from :func:`chart_source.cache_path_for_sheet`).

    Use:

    ```
    dlg = ChartDownloadErrorDialog(
        parent=self,
        sheet_key="north",
        failure_url="https://...",
        failure_reason="HTTP 404: Not Found",
        project_root=self._project_root,
    )
    code = dlg.exec()
    if code == ACTION_RETRY:
        ...
    elif code == ACTION_OPEN_SETTINGS:
        ...
    else:  # Rejected (Cancel button or Esc)
        ...
    ```
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        sheet_key: str,
        failure_url: str,
        failure_reason: str,
        project_root: Path,
    ) -> None:
        super().__init__(parent)
        self._project_root = project_root

        sheet_label = SHEET_DISPLAY_NAMES.get(sheet_key, sheet_key)
        self.setWindowTitle(f"Download failed: {sheet_label}")
        self.setObjectName("ChartDownloadErrorDialog")
        self.setModal(True)
        self.resize(620, 520)
        self.setMinimumSize(560, 380)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # Headline: which sheet failed.
        headline = QLabel(
            f"<b>Could not download the {sheet_label}.</b>"
        )
        headline.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(headline)

        # Failing URL — selectable so a user can copy-paste even
        # if the explicit Copy button below is somehow not visible
        # (Qt occasionally clips QPushButton labels on HiDPI
        # displays the first time the dialog is shown).
        url_row = QVBoxLayout()
        url_label = QLabel("URL we tried to fetch:")
        url_row.addWidget(url_label)
        url_value = QLabel(failure_url)
        url_value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        url_value.setWordWrap(True)
        url_value.setStyleSheet("padding: 4px; background: palette(base);")
        url_row.addWidget(url_value)
        root.addLayout(url_row)

        # Reason — what urllib / the server said. Wrapped because
        # network errors can spew long messages and we don't want
        # the dialog to grow horizontally past readable.
        reason_label = QLabel(f"<b>Reason:</b> {failure_reason}")
        reason_label.setWordWrap(True)
        root.addWidget(reason_label)

        # What the user can do — three bullet-point fallback paths.
        # Bullets are ASCII-friendly because Qt's HTML renderer in
        # QLabel handles them faster than a styled <ul>.
        cache_target = cache_path_for_sheet(sheet_key, project_root)
        instructions = QLabel(
            "<b>You can:</b>"
            "<ul>"
            "<li>Click <b>Retry</b> to try the same download again "
            "(most failures are a transient network glitch).</li>"
            "<li>Click <b>Open Map File Settings\u2026</b> to switch "
            "this sheet to a different URL or a local PDF path "
            "you already have on disk.</li>"
            "<li>Open the URL in your web browser, save the PDF "
            "manually, and place it at:"
            f"<br><code>{cache_target}</code><br>"
            "then click <b>Retry</b>.</li>"
            "</ul>"
        )
        instructions.setWordWrap(True)
        instructions.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(instructions)

        # Separator before the all-URLs reference block. Visual
        # break so the user reads "what to do" first and "all
        # URLs for reference" second.
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(sep)

        all_urls_label = QLabel(
            "<b>All chart URLs</b> (in case you want to download "
            "more than one manually):"
        )
        root.addWidget(all_urls_label)

        for key in SHEET_KEYS:
            root.addWidget(self._build_url_row(key))

        # Action buttons. Three options — Retry, Open Settings,
        # Cancel — wired to distinct return codes so the caller
        # can dispatch cleanly.
        buttons = QDialogButtonBox(self)
        buttons.setObjectName("chartDownloadErrorButtons")
        retry_btn = QPushButton("Retry")
        retry_btn.setObjectName("chartDownloadRetryButton")
        retry_btn.setDefault(True)
        retry_btn.clicked.connect(lambda: self.done(ACTION_RETRY))
        buttons.addButton(retry_btn, QDialogButtonBox.ButtonRole.AcceptRole)

        settings_btn = QPushButton("Open Map File Settings\u2026")
        settings_btn.setObjectName("chartDownloadOpenSettingsButton")
        settings_btn.clicked.connect(lambda: self.done(ACTION_OPEN_SETTINGS))
        buttons.addButton(settings_btn, QDialogButtonBox.ButtonRole.ActionRole)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("chartDownloadCancelButton")
        cancel_btn.clicked.connect(self.reject)
        buttons.addButton(cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)

        root.addWidget(buttons)

    def _build_url_row(self, sheet_key: str) -> QWidget:
        """Build a row showing one sheet's URL with a Copy button.

        Layout: sheet label, URL (selectable, wrapped), Copy
        button. The Copy button writes the URL to the system
        clipboard (via ``QGuiApplication.clipboard()``) so the
        user can paste it into a browser or another machine
        without having to mouse-select-copy.
        """
        row = QWidget()
        row.setObjectName(f"chartDownloadUrlRow_{sheet_key}")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)

        label_text = f"<b>{SHEET_DISPLAY_NAMES[sheet_key]}:</b>"
        label = QLabel(label_text)
        label.setMinimumWidth(110)
        lay.addWidget(label)

        url = CAAI_CHART_URLS[sheet_key]
        url_label = QLabel(url)
        url_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        url_label.setWordWrap(True)
        url_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        lay.addWidget(url_label, 1)

        copy_btn = QPushButton("Copy")
        copy_btn.setObjectName(f"chartDownloadCopyButton_{sheet_key}")
        copy_btn.setToolTip(
            f"Copy the {SHEET_DISPLAY_NAMES[sheet_key]} URL to the "
            f"clipboard so you can paste it into a browser."
        )
        copy_btn.clicked.connect(
            lambda checked=False, u=url, b=copy_btn: self._copy_url_to_clipboard(u, b)
        )
        lay.addWidget(copy_btn)

        return row

    @staticmethod
    def _copy_url_to_clipboard(url: str, anchor_btn: QPushButton) -> None:
        """Place ``url`` on the system clipboard and show a brief
        tooltip confirming the action.

        The tooltip is a low-friction confirmation — there's no
        modal "URL copied" popup blocking the user's next step,
        but a transient toast at the click site says "Copied" so
        the user knows the button did something."""
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(url)
        # Brief positive confirmation. ``mapToGlobal`` anchors the
        # tooltip to the button so it's adjacent to the click,
        # not at some arbitrary screen position.
        QToolTip.showText(
            anchor_btn.mapToGlobal(anchor_btn.rect().bottomRight()),
            "Copied",
            anchor_btn,
        )
