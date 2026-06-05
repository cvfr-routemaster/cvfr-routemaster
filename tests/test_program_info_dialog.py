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

"""Tests for :mod:`cvfr_routemaster.program_info_dialog`.

These tests pin the contract that matters legally: every
paragraph the user signed off on appears verbatim in the rendered
HTML (so a future refactor can't silently mutate the license
summary), every external link is reachable, and the dialog reads
the version from ``cvfr_routemaster.__version__`` so the build
cookbook's "bump in one place" invariant survives.

Two layers:

* **Pure-function layer** — assertions against
  :func:`build_copyright_info_html` and the module-level
  constants. These don't need a ``QApplication`` and run in
  milliseconds.
* **Qt layer** — actual :class:`ProgramInfoDialog` construction
  to verify the QTextBrowser actually carries the generated
  HTML, links open externally, and the dialog has the expected
  title / size hints.
"""

from __future__ import annotations

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QTextBrowser  # noqa: E402

from cvfr_routemaster import APP_NAME, __version__, display_version  # noqa: E402
from cvfr_routemaster import map_modes  # noqa: E402
from cvfr_routemaster.program_info_dialog import (  # noqa: E402
    AGPL_FULL_TEXT_URL,
    AGPL_PARA_LICENSE_GRANT,
    AGPL_PARA_LICENSE_LINK,
    AGPL_PARA_WARRANTY_DISCLAIMER,
    CAAI_CHART_URLS,
    CAAI_TERMS_URL,
    CONTACT_EMAIL,
    COPYRIGHT_HOLDER,
    COPYRIGHT_YEAR,
    INTENDED_USE_PARAGRAPH,
    SOURCE_BUNDLE_RELPATH,
    THIRD_PARTY_COMPONENTS,
    TRANSITIVE_DEPENDENCIES_NOTE,
    ProgramInfoDialog,
    build_copyright_info_html,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """One ``QApplication`` per module — required to construct the
    Qt widget under test, but cheap and reused across all
    Qt-layer tests in this file."""
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Constants — the user-signed-off literals.
# ---------------------------------------------------------------------------


def test_contact_email_is_the_signed_off_address() -> None:
    """The contact email is the author-correspondence channel
    surfaced in the dialog. (It is NOT the AGPLv3 §6 source-
    request channel any more — we ship source alongside the
    binary under §6(a), so no request-channel is needed at all.
    The address is still included as a courtesy for users who
    want to ask questions about the source.) Pin the exact
    address so a typo in a future refactor can't silently send
    users to a different inbox.
    """
    assert CONTACT_EMAIL == "cvfr.routemaster@gmail.com"


def test_copyright_holder_and_year() -> None:
    """The copyright holder is stored as ``Lev F.`` (the user's
    chosen public attribution; not the full name) and the year is
    the v3.3 release year."""
    assert COPYRIGHT_HOLDER == "Lev F."
    assert COPYRIGHT_YEAR == "2026"


def test_source_bundle_relpath_is_under_source_folder() -> None:
    """AGPLv3 §6(a) is satisfied by accompanying the binary
    with the corresponding source on the same distribution
    medium. We use the convention ``source/<archive>.zip`` so
    the source is at a stable, predictable path inside the
    release zip; the dialog points users at the exact path.

    The forward-slash separator is canonical (works inside zip
    archives and on every host OS); a future change to a
    different relative path or extension would update this
    pin too.
    """
    assert SOURCE_BUNDLE_RELPATH == "source/cvfr-routemaster-source.zip"


def test_agpl_license_grant_paragraph_matches_canonical_wording() -> None:
    """The AGPLv3 "How to Apply" boilerplate from
    opensource.org/license/agpl-3.0 must appear verbatim — this
    is the license statement of record. Any rewording, even
    cosmetic, would weaken the unambiguous "this program is
    AGPL-licensed" message and could be argued to be a different
    license offer entirely."""
    assert (
        AGPL_PARA_LICENSE_GRANT
        == "This program is free software: you can redistribute it "
        "and/or modify it under the terms of the GNU Affero "
        "General Public License as published by the Free "
        "Software Foundation, either version 3 of the License, "
        "or (at your option) any later version."
    )


def test_agpl_warranty_disclaimer_paragraph_matches_canonical_wording() -> None:
    """Canonical AGPLv3 warranty-disclaimer wording — pinned for
    the same reason as the license grant. This is the paragraph
    the intended-use disclaimer rides on (AGPLv3 §7(a) permits
    warranty disclaimers; that's how we frame the
    flight-simulator-only language)."""
    assert (
        AGPL_PARA_WARRANTY_DISCLAIMER
        == "This program is distributed in the hope that it will be "
        "useful, but WITHOUT ANY WARRANTY; without even the implied "
        "warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR "
        "PURPOSE. See the GNU Affero General Public License for "
        "more details."
    )


def test_agpl_license_link_paragraph_includes_canonical_url() -> None:
    """The "see <http://www.gnu.org/licenses/>" line is part of
    the canonical boilerplate. The URL must be the one the FSF
    publishes (not a paraphrase or a redirect)."""
    assert "http://www.gnu.org/licenses/" in AGPL_PARA_LICENSE_LINK


def test_intended_use_paragraph_is_the_approved_wording() -> None:
    """User-approved final wording for the intended-use section.
    Framed as a warranty disclaimer (AGPLv3 §7(a) permits this)
    rather than a use restriction (AGPLv3 §10 forbids imposing
    further use restrictions on downstream recipients).

    Pinned character-for-character because legal text drift is
    exactly the silent-regression class this test exists to
    prevent.
    """
    assert (
        INTENDED_USE_PARAGRAPH
        == "This program is intended for flight-simulator use only. "
        "The author disclaims any warranty of fitness for use in "
        "real-world aviation; any such use is entirely at the "
        "user's own risk and is not contemplated by this software. "
        "This program is not a substitute for official charts, "
        "NOTAMs, weather briefings, or any other official "
        "flight-planning material. Always cross-check against "
        "current AIP material before any simulated flight."
    )


def test_intended_use_does_not_attempt_to_restrict_real_world_use() -> None:
    """AGPLv3 §10 forbids imposing further use restrictions on
    downstream recipients. The intended-use paragraph must therefore
    use *disclaimer* language ("at the user's own risk"), NOT
    *prohibition* language ("expressly prohibited", "you must
    not"). Catching this here prevents a well-meaning future
    edit that strengthens the wording from accidentally
    introducing an AGPL-incompatible clause."""
    lowered = INTENDED_USE_PARAGRAPH.lower()
    forbidden_phrases = [
        "expressly prohibit",
        "you must not",
        "you may not",
        "is prohibited",
        "is forbidden",
    ]
    for phrase in forbidden_phrases:
        assert phrase not in lowered, (
            f"Intended use must not impose use restrictions "
            f"(AGPLv3 §10); found prohibition phrase: {phrase!r}"
        )


def test_caai_chart_urls_cover_all_three_sheets() -> None:
    """Three CAAI sheets matter for the route planner: north,
    south, back-pages. Each one's URL must be present so the
    on-first-run fetcher has somewhere to grab it from. The keys
    are the human-facing labels the dialog displays — pin them
    so the labels match the build-cookbook expectations."""
    assert set(CAAI_CHART_URLS.keys()) == {
        "North sheet",
        "South sheet",
        "Back-pages",
    }
    for label, url in CAAI_CHART_URLS.items():
        assert url.startswith("https://www.gov.il/"), (
            f"{label!r} URL must point at the gov.il domain; got {url!r}"
        )
        assert url.lower().endswith(".pdf"), (
            f"{label!r} URL must point at a PDF; got {url!r}"
        )


def test_caai_terms_url_is_the_canonical_gov_il_terms_page() -> None:
    """The Israeli government terms-of-use page is the
    authoritative source for "what you may do with these
    charts". Pin the URL so the link in the dialog actually
    points there."""
    assert CAAI_TERMS_URL == "https://www.gov.il/he/pages/gov_terms_of_use"


def test_agpl_full_text_url_is_opensource_org() -> None:
    """opensource.org is the canonical place we point users to
    for the full AGPLv3 text. opensource.org's URL is stable;
    pin it so a future ``setHtml`` rewrite can't silently drop
    the link to the full license."""
    assert AGPL_FULL_TEXT_URL == "https://opensource.org/license/agpl-3.0"


def test_third_party_components_cover_every_runtime_dep() -> None:
    """Every dependency we redistribute inside the release
    bundle must appear in the attribution table. Pin the set so
    a new dependency added to the build (e.g. someone vendoring
    a fast-path C extension) shows up here and prompts the dev
    to add it."""
    component_names = {name for name, _, _ in THIRD_PARTY_COMPONENTS}
    assert component_names == {
        "Python",
        "Qt 6 (via PySide6)",
        "PySide6",
        "PyMuPDF",
        "Pillow",
        "NumPy",
        "pytesseract",
        "Tesseract OCR",
        "PyInstaller",
    }


def test_third_party_components_each_have_holder_and_license() -> None:
    """Every row must populate all three fields (name, holder,
    license). An empty holder or license would render as ``© .
    License.`` in the dialog — visually confusing and
    legally meaningless."""
    for name, holder, license_text in THIRD_PARTY_COMPONENTS:
        assert name, "component name missing"
        assert holder, f"copyright holder missing for {name!r}"
        assert license_text, f"license missing for {name!r}"


def test_pymupdf_is_attributed_as_agpl() -> None:
    """PyMuPDF is the dependency that forced this whole program
    to be AGPL (copyleft flows up). Explicitly verify its row
    names AGPL so the user reading the attribution table can
    trace WHY the whole program is AGPL, not just THAT it is."""
    pymupdf = next(
        row for row in THIRD_PARTY_COMPONENTS if row[0] == "PyMuPDF"
    )
    _, holder, license_text = pymupdf
    assert "Artifex" in holder, (
        "PyMuPDF copyright holder must include Artifex Software"
    )
    assert "AGPL" in license_text, (
        "PyMuPDF must be attributed as AGPL — this is the license "
        "that forces the whole program to be AGPL-licensed"
    )


def test_pyinstaller_attribution_mentions_bootloader_exception() -> None:
    """PyInstaller is GPLv2 *with* a bootloader exception that
    explicitly permits packaging non-GPL apps. Without naming
    the exception the row reads as "everything packaged with
    PyInstaller becomes GPL", which is the misconception this
    test exists to prevent."""
    pyinstaller = next(
        row for row in THIRD_PARTY_COMPONENTS if row[0] == "PyInstaller"
    )
    _, _, license_text = pyinstaller
    assert "bootloader exception" in license_text.lower(), (
        "PyInstaller license note must explain the bootloader "
        "exception so the user understands that packaging "
        "doesn't force GPL on the whole app"
    )


def test_transitive_dependencies_note_acknowledges_indirect_deps() -> None:
    """The catch-all sentence must mention that the full
    transitive manifest is available with the source code —
    otherwise a recipient who notices an un-attributed indirect
    dep has no obvious recourse."""
    assert "transitively" in TRANSITIVE_DEPENDENCIES_NOTE.lower()
    assert "source code" in TRANSITIVE_DEPENDENCIES_NOTE.lower()


# ---------------------------------------------------------------------------
# Rendered HTML — every signed-off literal must appear, and the
# version must come from the package's __version__.
# ---------------------------------------------------------------------------


def test_rendered_html_includes_app_name_and_version() -> None:
    """The leading line is the dialog's "what am I looking at"
    cue. Both the app name and the *short* version
    (``display_version()``, so ``v3.3`` rather than ``v3.3.0``)
    must appear so a recipient knows immediately what they
    have."""
    html = build_copyright_info_html()
    assert APP_NAME in html
    assert f"v{display_version()}" in html


def test_rendered_html_version_tracks_module_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``__version__`` bump must propagate to the dialog
    automatically. Without this, the build cookbook's "bump in
    one place" promise is broken and the dialog would lie about
    the running version.

    Pinned via the dialog's own title-line pattern (``APP_NAME
    v<X>``), NOT a bare ``"v3"`` substring search: the rendered
    HTML legitimately contains ``LGPLv3``, ``License v3``,
    ``AGPLv3``, etc. as part of third-party license names, so
    a substring search would false-positive on every run. The
    title line is the one place the *program's own* version
    appears, so a tight selector for that line is what catches
    a drifted version.
    """
    import cvfr_routemaster as pkg

    monkeypatch.setattr(pkg, "__version__", "4.0.0")
    html = build_copyright_info_html()
    assert f"<b>{APP_NAME} v4.0</b>" in html, (
        "Dialog title line must reflect the (monkeypatched) "
        "version 4.0.0 as 'v4.0'"
    )
    assert f"<b>{APP_NAME} v3" not in html, (
        "Dialog title line must NOT still read 'v3.x' after "
        "the package version is bumped to 4.0.0"
    )

    monkeypatch.setattr(pkg, "__version__", "3.3.1")
    html = build_copyright_info_html()
    assert f"<b>{APP_NAME} v3.3.1</b>" in html, (
        "Patch-version bump must surface the full v3.3.1 (no "
        "trailing-zero trim should hide the hotfix segment)"
    )


def test_rendered_html_contains_mailto_link_for_contact_email() -> None:
    """The contact email must render as a ``mailto:`` link, not
    plain text. Plain-text email addresses force the user to
    copy-paste, which most users won't do for a "just FYI"
    info dialog — the mailto: makes the contact channel
    one-click."""
    html = build_copyright_info_html()
    assert f'href="mailto:{CONTACT_EMAIL}"' in html


def test_rendered_html_contains_agpl_paragraphs_verbatim() -> None:
    """All three AGPLv3 boilerplate paragraphs must appear
    verbatim in the rendered HTML — they are the license
    statement of record."""
    html = build_copyright_info_html()
    assert AGPL_PARA_LICENSE_GRANT in html
    assert AGPL_PARA_WARRANTY_DISCLAIMER in html
    # The license-link paragraph already contains HTML markup
    # so a literal ``in`` check requires the same encoding.
    assert "http://www.gnu.org/licenses/" in html


def test_rendered_html_points_users_at_the_accompanying_source_archive() -> None:
    """AGPLv3 §6(a) is satisfied by accompanying the binary
    with source. The rendered dialog must (a) say there's a
    Source code section, (b) name the exact relative path the
    user can find the source archive at, and (c) explicitly
    cite §6(a) so a reader who knows the license can see at a
    glance which §6 sub-clause is being relied on.

    The previous wording (a §6(b) "available upon request"
    offer with a three-year window) is gone — we don't need it
    because the source travels with the binary now."""
    html = build_copyright_info_html()
    assert "Source code" in html
    assert SOURCE_BUNDLE_RELPATH in html
    assert "6(a)" in html
    # The §6(b) request-channel wording must NOT appear — it
    # would falsely imply a written-offer regime we no longer
    # rely on.
    assert "available upon request" not in html
    assert "three years" not in html
    assert "3 years" not in html


def test_rendered_html_contains_all_three_caai_chart_urls() -> None:
    """Every CAAI URL must render so the on-first-run fetcher's
    sources are visible to the user (and so a user without the
    program can still grab the charts manually for personal
    use).

    The URL must appear in the ``href`` (the clickable target);
    the visible *link text* is the human-readable Hebrew-decoded
    form (see :func:`_human_readable_url` and the trio of tests
    below). Checking via ``href="..."`` rather than bare substring
    ensures the assertion is robust against the link-text being
    cosmetically transformed."""
    html = build_copyright_info_html()
    for url in CAAI_CHART_URLS.values():
        assert f'href="{url}"' in html, (
            f"CAAI URL missing from dialog href: {url}"
        )


def test_rendered_html_enumerates_every_mode_chart_url() -> None:
    """Every chart product the app can switch to (CVFR, LSA, …)
    must have *all* of its sheet source URLs attributed in the
    Chart-data section. Driving this assertion off the
    ``map_modes`` registry — rather than the CVFR-only
    ``chart_source`` constants — pins the v4 contract that the
    legal dialog enumerates the whole registry, so adding a new
    mode (and shipping its seed) surfaces its URLs here without
    a separate edit to the dialog."""
    html = build_copyright_info_html()
    for mode in map_modes.all_modes():
        for sheet in mode.sheets:
            assert f'href="{sheet.default_url}"' in html, (
                f"{mode.mode_id}/{sheet.key} URL missing from the "
                f"Legal dialog: {sheet.default_url}"
            )


def test_rendered_html_includes_the_lsa_chart_urls() -> None:
    """Regression guard for the v4 phase-4 change: the LSA chart
    URLs (AIP edition ``ב'-08``) must appear in the dialog. Before
    v4 the dialog only listed the three CVFR sheets; this test
    fails loudly if a refactor drops back to CVFR-only
    enumeration."""
    html = build_copyright_info_html()
    lsa = map_modes.get_mode("lsa")
    assert lsa.sheets, "LSA mode unexpectedly has no sheets"
    for sheet in lsa.sheets:
        assert f'href="{sheet.default_url}"' in html, (
            f"LSA {sheet.key} URL missing from dialog: {sheet.default_url}"
        )
    # The AIP edition tag distinguishes LSA (b'-08) from CVFR
    # (b'-03); confirm at least one LSA URL carries it so we know
    # the LSA set — not a duplicate of the CVFR set — was rendered.
    assert "08" in "".join(s.default_url for s in lsa.sheets)


def test_chart_section_labels_each_mode_by_display_name() -> None:
    """Each mode's URLs are grouped under a bold heading naming
    the chart product (``CVFR`` / ``LSA``) so a reader can tell
    which product a given URL belongs to."""
    html = build_copyright_info_html()
    for mode in map_modes.all_modes():
        assert f"<b>{mode.display_name}</b>" in html, (
            f"Chart-data section missing the {mode.display_name!r} "
            f"product heading"
        )


def test_caai_url_hrefs_keep_percent_encoded_form_for_wire_correctness() -> None:
    """The ``href`` attribute on each CAAI link must hold the
    exact published URL, percent-encoded Hebrew and all.

    Why this matters: when a user clicks the link, Qt hands the
    href to ``QDesktopServices.openUrl`` which passes it to the
    OS shell. The OS shell then hands the URL to the default
    browser, which in turn passes it to gov.il's HTTP server.
    gov.il's URL routing matches *byte-for-byte* on the
    percent-encoded form — sending raw Hebrew bytes instead of
    ``%D7%XX`` returns 404 from their CDN even though the
    decoded string would look identical to a human.

    So we display the decoded form to humans, but the href —
    the bytes that actually travel the wire — must remain the
    published, percent-encoded form. This test pins that
    invariant so a future "let's just store the decoded form"
    refactor can't silently break click-through to gov.il."""
    html = build_copyright_info_html()
    for label, url in CAAI_CHART_URLS.items():
        assert "%D7" in url, (
            f"Test premise broken: CAAI URL for {label!r} no "
            f"longer contains %D7-encoded Hebrew; update the test"
        )
        assert f'href="{url}"' in html, (
            f"href for {label!r} must be the exact percent-encoded "
            f"URL gov.il publishes, not a decoded variant"
        )


def test_caai_url_visible_text_is_hebrew_decoded_not_percent_encoded() -> None:
    """The visible link text in the Chart-data section must show
    the URL with percent-escapes decoded — i.e. native Hebrew
    characters where ``%D7%XX`` triplets used to be.

    Why: a reader scanning the dialog for legal context shouldn't
    have to mentally decode ``%D7%91%27-03%20CVFR%20%D7%A6%D7%A4%D7%95%D7%A0%D7%99-``
    to confirm which sheet a URL belongs to. The decoded form
    reads as ``ב'-03 CVFR צפוני-`` which is exactly the wording
    on the CAAI sheet's title block.

    We assert on two specific Hebrew substrings drawn from the
    decoded URLs (``צפוני`` = "north", ``דרומי`` = "south") so
    the test fails loudly if a future code change accidentally
    re-encodes the visible text or replaces ``unquote`` with a
    no-op. Picking sheet-name fragments rather than the whole
    decoded URL keeps the test stable against future gov.il
    URL revisions that change the year or sheet edition while
    preserving the same north/south/back vocabulary."""
    html = build_copyright_info_html()
    assert "צפוני" in html, (
        "Chart-data section must show the decoded Hebrew sheet "
        "name 'צפוני' (north) in the visible link body — found "
        "neither the decoded Hebrew nor a recognisable substitute"
    )
    assert "דרומי" in html, (
        "Chart-data section must show the decoded Hebrew sheet "
        "name 'דרומי' (south) in the visible link body"
    )
    assert "אחורי" in html, (
        "Chart-data section must show the decoded Hebrew sheet "
        "name 'אחורי' (back-pages) in the visible link body"
    )


def test_caai_url_link_text_is_not_the_percent_encoded_form() -> None:
    """The element *content* between ``<a href=...>`` and ``</a>``
    for each CAAI link must NOT be the percent-encoded URL.

    Sibling-pinning of the two tests above: even if a future
    refactor accidentally inlines ``f'<a href="{url}">{url}</a>'``
    (the pre-fix form), the previous test could still pass if
    the dialog *also* prints the decoded Hebrew elsewhere. This
    test specifically extracts the link-element content for
    each CAAI URL and confirms the encoded form is absent from
    *that* span.

    Done by string-slicing rather than HTML parsing because the
    dialog's HTML is small and known-shape; pulling in
    ``BeautifulSoup`` for a five-link assertion is overkill."""
    html = build_copyright_info_html()
    for label, url in CAAI_CHART_URLS.items():
        href_tag = f'href="{url}">'
        idx = html.find(href_tag)
        assert idx != -1, (
            f"href anchor for {label!r} not found — test premise broken"
        )
        body_start = idx + len(href_tag)
        body_end = html.find("</a>", body_start)
        assert body_end != -1, (
            f"closing </a> for {label!r} not found — malformed HTML"
        )
        link_body = html[body_start:body_end]
        # The visible text must not be the raw percent-encoded form.
        # We don't assert the link_body equals the unquoted URL
        # exactly because HTML escaping may have transformed ``&``
        # or ``<`` characters, but no such characters appear in
        # the gov.il URLs today — the literal-equality check
        # below is therefore safe and informative.
        assert "%D7" not in link_body, (
            f"Visible link text for {label!r} still contains "
            f"percent-encoded Hebrew (%D7…). Expected decoded "
            f"Hebrew characters in the link body. Got: "
            f"{link_body[:80]!r}"
        )


def test_rendered_html_contains_caai_terms_link() -> None:
    """Use of the CAAI charts is governed by the gov.il terms of
    use. The terms page must be a clickable link, not just
    mentioned in prose."""
    html = build_copyright_info_html()
    assert f'href="{CAAI_TERMS_URL}"' in html


def test_rendered_html_contains_state_of_israel_attribution() -> None:
    """The "© State of Israel" attribution for the CAAI charts
    is the legally-required part of the chart-data section.
    Pin it so a future rewording can't drop it."""
    html = build_copyright_info_html()
    assert "State of Israel" in html


def test_rendered_html_does_not_render_double_periods() -> None:
    """Holder strings that already end in a period (``Artifex
    Software, Inc.``) used to render as ``Inc..`` because the
    row template unconditionally appended a row-separator
    period. The renderer now strips a trailing period from the
    holder before re-appending the separator — pin the absence
    of any ``..`` so a future template tweak that drops the
    strip doesn't re-introduce the typo. Constrained to the
    Third-party software list because elsewhere in the HTML
    a literal ``..`` (e.g. ellipsis) might be intended."""
    html = build_copyright_info_html()
    third_party_block = html.split("<h2>Third-party software</h2>", 1)[1]
    third_party_block = third_party_block.split("<h2>", 1)[0]
    assert ".." not in third_party_block, (
        "Third-party software section must not contain double "
        "periods (typically Inc.. or Inc.. License). Strip "
        "trailing period from holder before appending the row "
        "separator."
    )


def test_rendered_html_contains_every_third_party_component() -> None:
    """Every component in ``THIRD_PARTY_COMPONENTS`` must appear
    in the rendered HTML. Missing one would create an
    attribution gap."""
    html = build_copyright_info_html()
    for name, holder, license_text in THIRD_PARTY_COMPONENTS:
        assert name in html, f"Component name missing: {name}"
        assert holder in html, f"Holder missing for {name}: {holder}"


def test_rendered_html_contains_intended_use_paragraph_verbatim() -> None:
    """The intended-use paragraph is the most legally-sensitive
    chunk in the dialog (it's the warranty-disclaimer
    flight-simulator-only framing). It must appear verbatim."""
    html = build_copyright_info_html()
    assert INTENDED_USE_PARAGRAPH in html


def test_rendered_html_uses_section_headings_for_skimmability() -> None:
    """A wall of text is unreadable. The dialog must use ``<h2>``
    headings to break content into scannable sections — confirm
    the section names are present so a future "let's flatten
    this to one paragraph" refactor fails here."""
    html = build_copyright_info_html()
    for heading in (
        "<h2>License</h2>",
        "<h2>Source code</h2>",
        "<h2>Chart data</h2>",
        "<h2>Third-party software</h2>",
        "<h2>Intended use</h2>",
    ):
        assert heading in html, f"Missing section heading: {heading}"


def test_rendered_html_places_intended_use_immediately_after_license() -> None:
    """The intended-use / sim-only paragraph is a warranty
    disclaimer framed under AGPLv3 §7(a). It must appear
    immediately after the License section, NOT at the bottom of
    the dialog where a casual scroller past the Source-code,
    Chart-data, and Third-party-software lists would miss it.

    User report that motivated this pin: the previous layout
    buried the limitation-of-liability paragraph at the very
    end, and the recipient testing the v3.3 candidate noted
    that "no one will see it down there." Moving it up to right
    after the AGPL boilerplate it relies on is the UX choice
    backed by the legal one (a reader who stops at the license
    block also sees the disclaimer).

    Pinned via string positions so any future reorganisation
    that drops it past Source-code / Chart-data / Third-party
    software fails this test by name. We assert it precedes
    each of those sections — not just that it follows License
    — so even a refactor that adds a new section between
    Intended-use and the rest can't silently push the
    disclaimer back below content."""
    html = build_copyright_info_html()
    pos_license = html.find("<h2>License</h2>")
    pos_intended = html.find("<h2>Intended use</h2>")
    pos_source = html.find("<h2>Source code</h2>")
    pos_chart = html.find("<h2>Chart data</h2>")
    pos_third = html.find("<h2>Third-party software</h2>")
    assert pos_license != -1
    assert pos_intended != -1
    assert pos_source != -1
    assert pos_chart != -1
    assert pos_third != -1
    assert pos_license < pos_intended, (
        "Intended use section must come AFTER License"
    )
    assert pos_intended < pos_source, (
        "Intended use must come BEFORE Source code "
        "(legal-disclaimer-prominence rule)"
    )
    assert pos_intended < pos_chart, (
        "Intended use must come BEFORE Chart data"
    )
    assert pos_intended < pos_third, (
        "Intended use must come BEFORE Third-party software"
    )


# ---------------------------------------------------------------------------
# Qt widget layer.
# ---------------------------------------------------------------------------


def test_dialog_constructs_with_expected_title_and_modality(
    qapp: QApplication,
) -> None:
    """The dialog must self-identify as ``Legal and Copyright
    Info`` (the same label the toolbar button uses to summon it
    — the rename from the older ``Copyright Information`` was a
    UX call to make the limitation-of-liability framing
    obvious at the entry point) and must be modal so the user
    can't accidentally interact with the chart while reading
    legal text and miss something important."""
    dlg = ProgramInfoDialog(None)
    try:
        assert dlg.windowTitle() == "Legal and Copyright Info"
        assert dlg.isModal()
    finally:
        dlg.deleteLater()
        qapp.processEvents()


def test_dialog_text_browser_carries_the_generated_html(
    qapp: QApplication,
) -> None:
    """The QTextBrowser inside the dialog must carry the output
    of :func:`build_copyright_info_html` — links, paragraphs,
    headings, and all. We assert on a sample of distinctive
    substrings (AGPL grant + email + CAAI URL) rather than the
    full string because Qt's HTML serializer normalises
    whitespace and re-emits attribute quoting in ways that
    aren't 1:1 with the input."""
    dlg = ProgramInfoDialog(None)
    try:
        browser = dlg.findChild(QTextBrowser, "programInfoBrowser")
        assert browser is not None
        rendered = browser.toHtml()
        # Sample five distinctive substrings — one per major
        # section — so this test catches a "browser got an
        # empty string" regression even if Qt's serializer
        # rewrites the HTML on read-back.
        assert APP_NAME in rendered
        assert CONTACT_EMAIL in rendered
        assert "Affero" in rendered
        assert "State of Israel" in rendered
        assert "flight-simulator use only" in rendered
    finally:
        dlg.deleteLater()
        qapp.processEvents()


def test_dialog_browser_opens_links_externally(
    qapp: QApplication,
) -> None:
    """``setOpenExternalLinks(True)`` is what wires clicks on
    ``mailto:`` / ``https://`` links to ``QDesktopServices`` →
    the user's default mail client / browser. Without it,
    QTextBrowser tries to *navigate* in-document and the
    dialog's content disappears."""
    dlg = ProgramInfoDialog(None)
    try:
        browser = dlg.findChild(QTextBrowser, "programInfoBrowser")
        assert browser is not None
        assert browser.openExternalLinks() is True
    finally:
        dlg.deleteLater()
        qapp.processEvents()


def test_dialog_has_close_button_with_keyboard_default(
    qapp: QApplication,
) -> None:
    """Enter must close the dialog from anywhere. Pin the Close
    button as the dialog's default — this is what makes the
    "I just want to glance and close" keyboard workflow
    feel right."""
    from PySide6.QtWidgets import QDialogButtonBox

    dlg = ProgramInfoDialog(None)
    try:
        buttons = dlg.findChild(QDialogButtonBox, "programInfoButtons")
        assert buttons is not None
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        assert close_btn is not None
        assert close_btn.isDefault()
    finally:
        dlg.deleteLater()
        qapp.processEvents()


def test_dialog_size_is_large_enough_for_third_party_table(
    qapp: QApplication,
) -> None:
    """The Third-party software list is the longest section.
    Sized at ~720x640 so the AGPL boilerplate and the
    dependency table both fit on screen without scrolling on a
    1080p monitor — sanity-check the minimum-size hint so
    a future tidy-up that shrinks the dialog doesn't truncate
    the list mid-row."""
    dlg = ProgramInfoDialog(None)
    try:
        # The default ``resize()`` value should be >= the
        # minimum and large enough to comfortably display the
        # dependency table without horizontal scroll. Both
        # checks are loose (cover the case where Qt's HiDPI
        # scaling adjusts the px values slightly) but tight
        # enough to catch a "someone resized to 320x240"
        # regression.
        assert dlg.minimumWidth() >= 600
        assert dlg.minimumHeight() >= 380
    finally:
        dlg.deleteLater()
        qapp.processEvents()
