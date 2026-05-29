from __future__ import annotations

import urllib.parse
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class SettingsDialog(QDialog):
    """Map File Settings: path-or-URL per sheet, plus startup behaviour.

    The dialog accepts EITHER a local filesystem path OR an
    ``http(s)://`` URL in each of the three source fields. v3.3+
    releases ship the three default CAAI URLs (see
    :data:`cvfr_routemaster.chart_source.CAAI_CHART_URLS`); the user
    rarely needs to edit them, but the URL field is what enables the
    dev's workflow of "swap in a new CAAI URL pre-release to grab the
    updated chart for re-calibration".

    The Browse button next to each field still pops a local-file
    picker — there's no widget affordance for picking a URL because
    URLs are pasted in, not selected. Users on a fresh release with
    URLs already populated by the build's shipped ``chart_sources.json``
    typically never click Browse at all.

    Action codes:

    * Ok (``QDialog.Accepted``): save settings, dismiss.
    * Load now (``LOAD_NOW`` = 1201): save settings and immediately
      fire ``_load_all`` on the parent window. Useful after pasting
      a new URL — the user wants to see the download / render flow
      kick off right away.
    * Cancel (``QDialog.Rejected``): discard changes.

    Validation rules:

    * Every field must be non-empty.
    * URL fields must parse via :func:`urllib.parse.urlsplit` AND
      have a non-empty scheme + netloc. A typo like ``htts://...`` is
      rejected so the user sees the issue here rather than when the
      download attempt fails minutes later.
    * Local-path fields must exist on disk with non-zero size. This
      is the legacy v3.2 contract — friends running with local PDFs
      from a previous release continue to work without re-pasting URLs.
    """

    # Custom return code for "save and load now". Distinct from
    # ``QDialog.Accepted`` (1) and ``Rejected`` (0), and well clear of the
    # 1101+ codes used by ``CalibrationOptionsDialog`` so a future merger of
    # dialog dispatch doesn't collide. The controller switches on this code
    # in addition to ``Accepted`` to decide whether to call ``_load_all``.
    LOAD_NOW = 1201

    def __init__(
        self,
        north: str,
        south: str,
        back: str,
        *,
        autoload_on_start: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Map File Settings")
        self._north = QLineEdit(north)
        self._south = QLineEdit(south)
        self._back = QLineEdit(back)
        # Placeholder copy hints at the path-or-URL contract. Shown
        # only when the field is empty — once a value is set the
        # placeholder vanishes, so it doesn't visually clutter the
        # populated-defaults state.
        placeholder = "Local PDF path or https:// URL"
        self._north.setPlaceholderText(placeholder)
        self._south.setPlaceholderText(placeholder)
        self._back.setPlaceholderText(placeholder)

        self._autoload = QCheckBox("Load maps and waypoints automatically on startup")
        self._autoload.setChecked(autoload_on_start)
        self._autoload.setToolTip(
            "When all three sources are set (and any URL sources are "
            "already downloaded into the cache), load without opening "
            "this dialog. URL sources that haven't yet been fetched "
            "will be downloaded interactively the first time you click "
            "Load now."
        )

        form = QFormLayout()
        form.addRow("North map PDF:", self._row(self._north))
        form.addRow("South map PDF:", self._row(self._south))
        form.addRow("Back pages PDF:", self._row(self._back))

        hint = QLabel(
            "Each field accepts either a local PDF path or an "
            "<code>https://</code> URL. URL sources are downloaded on "
            "first use and cached under <code>.cvfr_routemaster/charts/</code> "
            "— a successful download is reused on every subsequent launch "
            "(no network calls in steady state). The Browse button picks "
            "a local file; for a URL, paste it directly into the field. "
            "<br><br>"
            "The north and south sheets are aligned automatically by the "
            "joint LSQ calibration solver. Alt+scroll on the map can still "
            "rescale the selected sheet as an escape hatch (Alt+Shift+scroll "
            "for the fine pass); positions and scales are remembered for the "
            "next launch."
        )
        hint.setWordWrap(True)

        # "Load maps & waypoints now" is a third action alongside Ok/Cancel.
        # We deliberately keep it visually separate from the standard
        # button box so the user reads it as a *do something extra* command
        # rather than an alternative way to dismiss the dialog. Validation
        # mirrors the Ok path so an invalid set of paths cannot trigger a
        # load that's guaranteed to fail.
        self._load_now_btn = QPushButton("Load maps && waypoints now")
        self._load_now_btn.setToolTip(
            "Validate the sources above, save them, and immediately reload "
            "the map sheets and waypoint database (downloading any URL "
            "sources whose cache is missing or whose URL changed). Use "
            "after changing a source to see the result without relaunching."
        )
        self._load_now_btn.clicked.connect(self._accept_validate_and_load)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_validate)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addWidget(hint)
        root.addLayout(form)
        root.addWidget(self._autoload)
        root.addWidget(self._load_now_btn)
        root.addWidget(buttons)

    def _row(self, field: QLineEdit) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        browse = QPushButton("Browse…")
        browse.setToolTip(
            "Pick a local PDF file. For a URL source, paste the URL "
            "directly into the field instead."
        )
        browse.clicked.connect(lambda: self._browse(field))
        lay.addWidget(field, 1)
        lay.addWidget(browse)
        return w

    def _browse(self, field: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select PDF", "", "PDF (*.pdf)")
        if path:
            field.setText(path)

    @staticmethod
    def _looks_like_url(text: str) -> bool:
        """Return True iff ``text`` begins with an ``http(s)://`` scheme.

        Conservative — scheme prefix only. We don't accept other
        schemes (``ftp://``, ``file://``, etc.) because the download
        machinery is ``urllib`` over HTTPS only and a non-HTTP URL
        would just produce a confusing error at fetch time.
        """
        lowered = text.lower().lstrip()
        return lowered.startswith("http://") or lowered.startswith("https://")

    @staticmethod
    def _validate_url(text: str) -> str | None:
        """Validate a URL source string. Return ``None`` if valid,
        else a human-facing error message.

        Conditions for a valid URL source:

        * Parses via :func:`urllib.parse.urlsplit` without raising.
        * Scheme is exactly ``http`` or ``https``.
        * Netloc is non-empty (catches typos like ``https:/example``
          where the user dropped a slash).
        * Path is non-empty (catches ``https://example.com`` with no
          resource to fetch).

        Returning an error string rather than ``True``/``False``
        lets the dialog tell the user which of the four conditions
        failed, which is much more useful than a generic
        "URL invalid" toast.
        """
        try:
            parts = urllib.parse.urlsplit(text.strip())
        except ValueError as exc:
            return f"could not parse: {exc}"
        if parts.scheme not in ("http", "https"):
            return (
                f"unsupported URL scheme {parts.scheme!r} (expected "
                f"http or https)"
            )
        if not parts.netloc:
            return "URL is missing the host portion (after the //)"
        if not parts.path or parts.path == "/":
            return "URL does not point at a specific file"
        return None

    def _validate_paths(self) -> bool:
        """Shared validation used by both Ok and Load-now.

        Each source field must validate as either a non-empty URL
        OR a non-empty path to an existing file. Mixed mode is
        permitted — north could be a URL and south a local path.

        Returns True when every field passes; otherwise pops a
        warning and returns False so the caller can bail.
        """
        for label, text in (
            ("North map", self._north.text().strip()),
            ("South map", self._south.text().strip()),
            ("Back pages", self._back.text().strip()),
        ):
            if not text:
                QMessageBox.warning(
                    self,
                    "Incomplete",
                    "Please set all three map sources (path or URL).",
                )
                return False
            if self._looks_like_url(text):
                err = self._validate_url(text)
                if err is not None:
                    QMessageBox.warning(
                        self,
                        "Invalid URL",
                        f"{label} URL: {err}\n\n{text}",
                    )
                    return False
                continue
            # Treat as local path.
            if not Path(text).is_file():
                QMessageBox.warning(
                    self,
                    "Missing file",
                    f"{label} source is neither an http(s):// URL nor an "
                    f"existing file:\n{text}",
                )
                return False
        return True

    def _accept_validate(self) -> None:
        if self._validate_paths():
            self.accept()

    def _accept_validate_and_load(self) -> None:
        """Bound to the *Load now* button — validate, then close with the
        ``LOAD_NOW`` return code so the controller knows to fire a load
        immediately after persisting the source edits."""
        if self._validate_paths():
            self.done(self.LOAD_NOW)

    def paths(self) -> tuple[str, str, str]:
        return (
            self._north.text().strip(),
            self._south.text().strip(),
            self._back.text().strip(),
        )

    def autoload_on_start(self) -> bool:
        return self._autoload.isChecked()
