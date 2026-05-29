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

"""Program Information dialog — Copyright / License / Attribution.

What this dialog is for
-----------------------

Reachable from the main toolbar's *Program Information* group via
the *Copyright Information* button. It surfaces every piece of
legal text a recipient of this binary needs to read:

* The AGPLv3 boilerplate (the program is AGPL-licensed because
  PyMuPDF, one of our run-time dependencies, is AGPLv3 — copyleft
  flows up to the whole work).
* The author's contact email (also the source-code request
  channel; AGPLv3 §6 requires the source offer to remain valid
  for at least three years).
* Attribution for the Israeli CVFR charts (CAAI / State of
  Israel; charts are NOT distributed with the program — see
  ``ROADMAP-NEXT.md`` for the dynamic-fetch design).
* Third-party software licenses for every direct dependency we
  ship in the release bundle (Python, Qt/PySide6, PyMuPDF,
  Pillow, NumPy, pytesseract, Tesseract OCR, PyInstaller), plus
  a catch-all sentence for transitive deps.
* The intended-use disclaimer — flight-simulator use only;
  framed as a warranty disclaimer per AGPLv3 §7(a) because §10
  forbids imposing further use restrictions on downstream
  recipients.

Why a dedicated module
----------------------

Three reasons we keep this out of ``main_window.py``:

1. **Visibility under code review**. Legal text is the kind of
   content that must survive refactors verbatim; pulling it into
   its own file (with its own tests) means an unrelated
   ``main_window`` change can't quietly mutate the license
   summary. ``tests/test_program_info_dialog.py`` pins both the
   AGPL boilerplate paragraph and the intended-use paragraph
   character-for-character against the strings the user signed
   off on.
2. **Single source of truth for the version**. The dialog's
   leading line ("CVFR Route Master v<X> — ...") reads from
   ``cvfr_routemaster.__version__`` so a version bump in one
   place (the package's ``__version__``) propagates to the
   window title (via ``app_title``), the splash, AND the legal
   dialog all together. The build cookbook (``.cursor/rules/
   build-releases.mdc`` step 0) leans on this invariant.
3. **Testability without a Qt scene**. ``build_copyright_info_html``
   is a pure function — tests can assert on its output as a
   string without spinning up ``QApplication``.

HTML rendering subset
---------------------

``QTextBrowser`` uses Qt's rich-text subset, which supports:
``<h2>``/``<h3>``/``<p>``/``<ul>``/``<li>``/``<b>``/``<i>``/``<a
href="...">``/``<br>``/``<hr>`` and a small chunk of inline CSS.
We deliberately stay within this subset (no flexbox, no grid, no
``<details>``) so the dialog renders identically on every Qt
build PySide6 ships across Windows and Linux.

External links open via ``setOpenExternalLinks(True)`` →
``QDesktopServices``: a click on a ``mailto:`` opens the
system's default mail client; a click on an ``https://`` URL
opens the default browser. We do NOT want QTextBrowser to try
to *navigate* to the URL in-document (its default behaviour for
plain hrefs) — that would load an empty page over our text and
strand the user with no way back.
"""

from __future__ import annotations

import html
from urllib.parse import unquote

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from cvfr_routemaster import APP_NAME, display_version
from cvfr_routemaster.chart_source import (
    CAAI_CHART_URLS as _SHEET_URLS,
    SHEET_DISPLAY_NAMES,
    SHEET_KEYS,
)

# ---------------------------------------------------------------------------
# Public constants — exposed so tests can pin the exact wording the user
# signed off on without re-parsing the rendered HTML.
# ---------------------------------------------------------------------------

CONTACT_EMAIL = "cvfr.routemaster@gmail.com"
COPYRIGHT_YEAR = "2026"
COPYRIGHT_HOLDER = "Lev F."

#: Relative path (under the release root) at which the
#: corresponding source archive is shipped alongside the
#: binary. AGPLv3 §6(a) permits source-availability to be
#: satisfied by accompanying the object code with the source
#: on a durable medium — we ship it in the same zip the user
#: receives the .exe in, so the dialog can point at the file
#: by name rather than promising a future request channel
#: under §6(b). See :func:`_source_section_html` for the
#: rendered wording.
SOURCE_BUNDLE_RELPATH = "source/cvfr-routemaster-source.zip"

#: Canonical AGPLv3 "How to Apply" boilerplate (opensource.org/license/agpl-3.0).
#: Stored as one constant per paragraph so the test file can assert on
#: each paragraph individually; concatenated by ``build_copyright_info_html``.
AGPL_PARA_LICENSE_GRANT = (
    "This program is free software: you can redistribute it and/or "
    "modify it under the terms of the GNU Affero General Public "
    "License as published by the Free Software Foundation, either "
    "version 3 of the License, or (at your option) any later version."
)
AGPL_PARA_WARRANTY_DISCLAIMER = (
    "This program is distributed in the hope that it will be useful, "
    "but WITHOUT ANY WARRANTY; without even the implied warranty of "
    "MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the "
    "GNU Affero General Public License for more details."
)
AGPL_PARA_LICENSE_LINK = (
    "You should have received a copy of the GNU Affero General "
    "Public License along with this program. If not, see "
    '<a href="http://www.gnu.org/licenses/">http://www.gnu.org/licenses/</a>.'
)

#: Approved intended-use paragraph. Framed as a warranty disclaimer
#: (AGPLv3 §7(a) explicitly allows this) rather than a use restriction
#: (AGPLv3 §10 forbids imposing further restrictions on downstream
#: recipients). The wording is the user's final-revision text and
#: tests pin it verbatim.
INTENDED_USE_PARAGRAPH = (
    "This program is intended for flight-simulator use only. The "
    "author disclaims any warranty of fitness for use in real-world "
    "aviation; any such use is entirely at the user's own risk and "
    "is not contemplated by this software. This program is not a "
    "substitute for official charts, NOTAMs, weather briefings, or "
    "any other official flight-planning material. Always cross-check "
    "against current AIP material before any simulated flight."
)

#: CAAI chart URLs the program will fetch on first run (the chart
#: PDFs are NOT distributed with the binary; Israeli government
#: terms of use prohibit redistribution but allow personal
#: download). Re-keyed here from the sheet-identity-keyed dict in
#: :mod:`cvfr_routemaster.chart_source` to use the human-facing
#: labels the dialog's Chart data section displays. The URL
#: strings themselves remain the single source of truth in
#: ``chart_source.CAAI_CHART_URLS``.
CAAI_CHART_URLS = {
    SHEET_DISPLAY_NAMES[key]: _SHEET_URLS[key] for key in SHEET_KEYS
}

CAAI_TERMS_URL = "https://www.gov.il/he/pages/gov_terms_of_use"
AGPL_FULL_TEXT_URL = "https://opensource.org/license/agpl-3.0"

#: Third-party dependency table. Each entry is
#: ``(name, copyright_holder, license)`` — the dialog renders one
#: ``<li>`` per row. Pinned in a list (not a dict) so the order is
#: stable across runs and a test can assert on it without
#: hash-ordering surprises.
THIRD_PARTY_COMPONENTS = [
    (
        "Python",
        "Python Software Foundation (PSF)",
        "Python Software Foundation License",
    ),
    (
        "Qt 6 (via PySide6)",
        "The Qt Company Ltd. and contributors",
        "GNU Lesser General Public License v3 (LGPLv3)",
    ),
    (
        "PySide6",
        "The Qt Company Ltd. and contributors",
        "GNU Lesser General Public License v3 (LGPLv3)",
    ),
    (
        "PyMuPDF",
        "Artifex Software, Inc.",
        "GNU Affero General Public License v3 (AGPLv3)",
    ),
    (
        "Pillow",
        "Jeffrey A. Clark, Secret Labs AB, Fredrik Lundh, and contributors",
        "HPND (Historical Permission Notice and Disclaimer)",
    ),
    (
        "NumPy",
        "NumPy Developers",
        "BSD 3-Clause License",
    ),
    (
        "pytesseract",
        "Matthias A. Lee",
        "Apache License 2.0",
    ),
    (
        "Tesseract OCR",
        "Google Inc., Hewlett-Packard, and Tesseract contributors",
        "Apache License 2.0",
    ),
    (
        "PyInstaller",
        "PyInstaller Development Team",
        "GNU General Public License v2, with a bootloader exception "
        "that permits redistribution of non-GPL applications packaged "
        "with PyInstaller",
    ),
]

#: Catch-all sentence for indirect / transitive dependencies. Each
#: of the above pulls in a small graph of sub-packages whose
#: licenses are individually permissive but too numerous to
#: enumerate. The wording acknowledges them and points at the
#: source-code offer as the canonical place to get the full list.
TRANSITIVE_DEPENDENCIES_NOTE = (
    "The components above transitively depend on additional "
    "open-source libraries whose licenses are individually "
    "permissive (MIT / BSD / Apache-2.0 / PSF style) and which are "
    "redistributed unmodified inside this binary. A complete "
    "manifest of every shipped Python distribution and its license "
    "is available in the accompanying source archive (see Source "
    "code, above)."
)


def _source_offer_text() -> str:
    """Return the source-code-availability sentence.

    AGPLv3 §6 lets the distributor pick from several methods of
    making source available to anyone who receives the binary.
    We use §6(a): "Convey the object code in […] a physical
    distribution medium, accompanied by the Corresponding
    Source fixed on a durable physical medium customarily used
    for software interchange." Practically that means: the
    same zip the user gets the .exe in also contains a
    ``source/`` folder with the full program source. No
    request-channel, no three-year offer, no email round-trip
    — the source is right there next to the binary.

    The address-for-correspondence is still surfaced (because
    the AGPL boilerplate includes one and a contact link is
    user-friendly), but the legally-binding source-availability
    statement is now grounded in §6(a)'s
    "accompanies the object code" mechanism.

    Pulled into its own helper so the test file can pin the
    structured statement without committing to an exact
    sentence template.
    """
    return (
        f"The complete corresponding source code for this "
        f"program is distributed alongside the binary in the "
        f"<code>{SOURCE_BUNDLE_RELPATH}</code> archive that "
        f"accompanies this release, in satisfaction of "
        f"AGPLv3 §6(a). Unzip the archive and follow its "
        f"<code>README.txt</code> to build and run from source. "
        f"For questions about the source contact "
        f'<a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>.'
    )


def _human_readable_url(url: str) -> str:
    """Return a display-friendly form of a percent-encoded URL.

    The CAAI chart URLs published on gov.il contain percent-
    encoded Hebrew characters (e.g.
    ``...aip_%D7%91'-03%20CVFR%20%D7%A6%D7%A4%D7%95%D7%A0%D7%99-.pdf``)
    because RFC 3986 requires non-ASCII characters in URI path
    components to be percent-encoded. The encoded form is correct
    on the wire — it's what ``urllib.request`` will fetch — but
    it's unreadable in the legal-info dialog: a reader scanning
    the Chart-data section sees a wall of ``%D7%XX`` bytes
    instead of the Hebrew sheet names (e.g. ``ב'-03 CVFR צפוני``,
    ``ב'-03 CVFR דרומי``, ``ב'-03CVFR אחורי``).

    The fix is the standard one for legal-display URLs: keep the
    href attribute exactly as published (so the HTTP fetch on
    click is byte-equal to a browser's), and decode the *visible
    text* via :func:`urllib.parse.unquote` so the Hebrew renders
    natively. QTextBrowser handles Hebrew + bidi rendering
    automatically — the same rich-text engine the route panel
    uses for the Hebrew route string, so we don't need to prime
    fallback fonts here.

    HTML escaping is applied with ``quote=False`` because the
    decoded text goes into element *content*, not an attribute.
    The single-quote in CAAI's sheet names (``ב'-03``) is
    therefore allowed to pass through as the literal Unicode
    apostrophe rather than being escaped to ``&#x27;`` — which
    would itself defeat the readability gain.

    The caller (``_chart_data_section_html``) decides separately
    how to render the href attribute; see the comment there for
    why we don't full-escape that side either.
    """
    return html.escape(unquote(url), quote=False)


def _chart_data_section_html() -> str:
    """Render the Chart-data attribution section.

    Each CAAI URL is shown with a friendly label (North / South
    / Back-pages) and a clickable link whose visible text is the
    human-readable (Hebrew-decoded) form of the URL — see
    :func:`_human_readable_url` for the rationale and the trade-
    off between wire-format href and display-format text.
    """
    rows: list[str] = []
    for label, url in CAAI_CHART_URLS.items():
        # The href intentionally uses ``quote=False``: gov.il's
        # CAAI URLs are a hardcoded module-level constant
        # (``chart_source.CAAI_CHART_URLS``), they contain only
        # percent-encoded bytes + ASCII + a literal apostrophe
        # ``'`` (in ``aip_%D7%91'-03``). The apostrophe is
        # legal as a literal character inside a double-quoted
        # HTML attribute per HTML5 §13.1.2.3, and we want to
        # preserve it verbatim because ``html.escape(quote=True)``
        # would emit ``&#x27;`` — which Qt's text-engine would
        # unescape on click, so functionally identical, but a
        # reader looking at the raw HTML (or a test inspecting
        # the dialog source) would have to mentally decode the
        # entity. ``quote=False`` still escapes ``<``, ``>``,
        # and ``&`` (the three characters that would actually
        # break the markup) so we're defended against any
        # future URL revision that grows one of those.
        href_safe = html.escape(url, quote=False)
        text_safe = _human_readable_url(url)
        rows.append(
            f'<li><b>{label}:</b> <a href="{href_safe}">{text_safe}</a></li>'
        )
    rows_html = "\n".join(rows)
    return (
        "<h2>Chart data</h2>"
        "<p>The Israeli CVFR charts displayed by this program are "
        "published by the Civil Aviation Authority of Israel (CAAI) "
        "and are <b>&copy; State of Israel</b>. The chart PDFs and "
        "their rendered images are <b>not distributed with this "
        "program</b> in compliance with the gov.il terms of use. "
        "On first run, the program fetches the latest published "
        "PDFs from the CAAI website on demand from the following "
        "URLs:</p>"
        f"<ul>{rows_html}</ul>"
        "<p>Use of the CAAI charts is governed by the gov.il terms "
        "of use: "
        f'<a href="{CAAI_TERMS_URL}">{CAAI_TERMS_URL}</a>.</p>'
    )


def _third_party_section_html() -> str:
    """Render the Third-party software section.

    Each row is rendered as ``<b>Name</b> — © Holder. License.``
    so the license sits at the end of the line, where a reader
    scanning the column quickly finds it. The transitive-deps
    catch-all sentence follows the list.

    Holders whose canonical form already ends in a period (e.g.
    ``Artifex Software, Inc.``) have it stripped before we
    re-append the row separator, so the rendered text reads
    ``Inc. License`` rather than the double-stop ``Inc..
    License``. We preserve the period on the license side because
    the license string is variable-length sentence-fragment text
    where the period is the row terminator, not an abbreviation.
    """
    rows: list[str] = []
    for name, holder, license_text in THIRD_PARTY_COMPONENTS:
        clean_holder = holder.rstrip(".").rstrip()
        rows.append(
            f"<li><b>{name}</b> &mdash; &copy; {clean_holder}. "
            f"{license_text}.</li>"
        )
    rows_html = "\n".join(rows)
    return (
        "<h2>Third-party software</h2>"
        "<p>This program is built on, and redistributes binaries of, "
        "the following open-source components. Each is used under "
        "the terms of its respective license; the copyright "
        "holders retain their respective rights.</p>"
        f"<ul>{rows_html}</ul>"
        f"<p>{TRANSITIVE_DEPENDENCIES_NOTE}</p>"
    )


def build_copyright_info_html() -> str:
    """Build the full Copyright Information HTML.

    Pure function — depends only on module-level constants and
    ``cvfr_routemaster.display_version()``. Tests assert on the
    string without spinning up a ``QApplication``.

    The version is read from ``cvfr_routemaster.display_version()``
    so a ``__version__`` bump propagates here automatically (the
    build cookbook's step 0 verifies the title-bar version
    matches the dialog version, and that invariant rests on this
    single source of truth)."""
    title_line = (
        f"<p style='font-size:11pt;'><b>{APP_NAME} v{display_version()}</b> "
        f"&mdash; an Israel CVFR route-planning assistant for "
        f"flight-simulator use.</p>"
    )

    copyright_line = (
        f"<p>Copyright &copy; {COPYRIGHT_YEAR} {COPYRIGHT_HOLDER} "
        f'&mdash; contact: <a href="mailto:{CONTACT_EMAIL}">'
        f"{CONTACT_EMAIL}</a>.</p>"
    )

    license_section = (
        "<h2>License</h2>"
        f"<p>{AGPL_PARA_LICENSE_GRANT}</p>"
        f"<p>{AGPL_PARA_WARRANTY_DISCLAIMER}</p>"
        f"<p>{AGPL_PARA_LICENSE_LINK}</p>"
        f'<p>Full license text: <a href="{AGPL_FULL_TEXT_URL}">'
        f"{AGPL_FULL_TEXT_URL}</a>.</p>"
    )

    source_section = (
        "<h2>Source code</h2>"
        f"<p>{_source_offer_text()}</p>"
    )

    intended_use_section = (
        "<h2>Intended use</h2>"
        f"<p>{INTENDED_USE_PARAGRAPH}</p>"
    )

    # Order matters: the intended-use / sim-only paragraph is the
    # most legally-significant chunk in the dialog (warranty
    # disclaimer framed under AGPLv3 §7(a)). It belongs
    # immediately after the AGPL boilerplate it relies on, NOT at
    # the bottom of the page where a reader scrolling past the
    # Chart-data and Third-party-software lists would never see
    # it. Recipient assumption: "if I read the license at the top,
    # I see what I'm bound to." Anything after Source-code,
    # Chart-data, or Third-party software is read as
    # supplementary attribution that a casual reader skips.
    return (
        "<html><body>"
        + title_line
        + copyright_line
        + license_section
        + intended_use_section
        + source_section
        + _chart_data_section_html()
        + _third_party_section_html()
        + "</body></html>"
    )


class ProgramInfoDialog(QDialog):
    """Modal dialog rendering :func:`build_copyright_info_html` in a
    scrollable :class:`QTextBrowser`.

    Sized at ~720x640 so the AGPL boilerplate paragraph and the
    Third-party software list both fit on screen without scrolling
    on a 1080p monitor — a recipient scanning quickly should see
    "AGPL, source on request, sim-only" without having to scroll
    past the dependency table.

    External links (``mailto:`` and ``https://``) open via
    :func:`QDesktopServices.openUrl` thanks to
    ``setOpenExternalLinks(True)`` — clicking does NOT navigate the
    browser inside the dialog, which would strand the user on an
    empty page.

    The dialog is non-resizable in the *minimum* axis (the layout
    needs ~680 px wide to keep the dependency-list lines readable
    without mid-word wrapping) but resizable upward for users on
    larger screens.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Legal and Copyright Info")
        self.setObjectName("ProgramInfoDialog")
        self.setModal(True)
        self.resize(720, 640)
        self.setMinimumSize(680, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        browser = QTextBrowser(self)
        browser.setObjectName("programInfoBrowser")
        browser.setOpenExternalLinks(True)
        browser.setHtml(build_copyright_info_html())
        browser.setReadOnly(True)
        # ``QTextEdit`` (the QTextBrowser base) defaults to
        # ``Qt.TextInteractionFlag.TextEditorInteraction`` when
        # not read-only; even when read-only we want explicit
        # link interaction so a screen-reader / keyboard user can
        # tab to and activate links.
        browser.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        root.addWidget(browser, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.setObjectName("programInfoButtons")
        buttons.rejected.connect(self.reject)
        # ``Close`` is wired through the ``rejected`` signal by Qt
        # convention even though it's not a "cancel" semantically;
        # we accept that convention rather than custom-roling the
        # button because it keeps the keyboard contract (Esc =
        # close) working without extra wiring.
        root.addWidget(buttons)

        # Browser receives initial keyboard focus so PageDown /
        # arrow-keys scroll the content immediately without an
        # extra click — but the Close button is the *default*
        # button (Enter closes the dialog from anywhere), which
        # matches the user expectation for a read-only info popup.
        browser.setFocus()
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_btn is not None:
            close_btn.setDefault(True)
            close_btn.setObjectName("programInfoCloseButton")
