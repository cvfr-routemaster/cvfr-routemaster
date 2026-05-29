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

"""Tests for the v3.3+ build-script changes.

Two new contracts the build scripts must honour:

1. The CAAI chart PDFs are NOT copied into the release bundle
   (the previous ``_copy_charts`` step is gone). Israeli
   government terms of use prohibit redistribution; the runtime
   downloads from URLs on first use.
2. The release bundle ships ``chart_sources.json`` containing
   the three default CAAI URLs, written by the new
   ``_write_shipped_derived_files`` step. Without this file, a
   fresh-install user opens Map File Settings to three empty
   fields and has no idea what to fill in.

The other v3.3+ deletion — ``map_north.png`` / ``map_south.png``
from ``CACHE_FILES`` — is pinned in
``tests/test_release_for_linux.py:test_cache_files_does_not_include_rendered_pngs``
for the Linux script; mirror that here for the Windows script.

This module deliberately tests the *constants and call structure*
of the build scripts (source-level inspection plus light
monkeypatch-based execution) rather than running the full build —
that would require PyInstaller and a slow ~6 min wall-clock pass
unsuitable for the inner regression loop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_windows_build_script_no_longer_defines_copy_charts() -> None:
    """Mirror of the Linux counterpart — both scripts must drop
    ``_copy_charts``. Reintroducing it would re-ship the CAAI
    PDFs in violation of gov.il terms of use."""
    from scripts import build_release

    assert not hasattr(build_release, "_copy_charts"), (
        "v3.3+ removed _copy_charts — re-adding it would ship "
        "CAAI PDFs in violation of gov.il terms of use"
    )


def test_windows_cache_files_does_not_include_rendered_pngs() -> None:
    """``map_north.png`` and ``map_south.png`` are rendered output
    of the chart PDFs. Since the PDFs themselves are non-
    redistributable, the rendered raster carries the same
    restriction. v3.3+ must not ship them."""
    from scripts import build_release

    forbidden = {"map_north.png", "map_south.png"}
    leaked = forbidden & set(build_release.CACHE_FILES)
    assert not leaked, (
        f"v3.3+ must not include rendered chart PNGs in CACHE_FILES; "
        f"found: {leaked}"
    )


def test_windows_build_script_no_longer_uses_release_pdf_subdir() -> None:
    """The ``map-pdfs/`` subfolder constant is gone — keeping it
    around would invite a copy-paste regression where a future
    contributor uses it to ship a "side-channel" of unredacted
    PDFs. The relevant subdirectory in v3.3+ is
    ``.cvfr_routemaster/charts/`` and it's created by the
    runtime, not the build."""
    from scripts import build_release

    assert not hasattr(build_release, "RELEASE_PDF_SUBDIR"), (
        "v3.3+ removed RELEASE_PDF_SUBDIR — re-adding it would "
        "suggest the release ships PDFs in a side directory"
    )


# ---------------------------------------------------------------------------
# Source-level call sequence
# ---------------------------------------------------------------------------


def test_windows_build_script_main_calls_write_shipped_derived_files() -> None:
    """The bare ``_write_shipped_derived_files()`` call must appear
    in ``main()`` at indent-4. This is the replacement for the
    pre-v3.3 ``_restamp_cache_fingerprints()`` call."""
    src = (
        Path(__file__).parent.parent / "scripts" / "build_release.py"
    ).read_text(encoding="utf-8")
    assert "    _write_shipped_derived_files()" in src, (
        "main() must call _write_shipped_derived_files() after "
        "_copy_seed_cache()"
    )


def test_linux_build_script_main_calls_write_shipped_derived_files() -> None:
    """Same as the Windows test, mirrored to the Linux pipeline."""
    src = (
        Path(__file__).parent.parent
        / "scripts"
        / "build_release_for_linux.py"
    ).read_text(encoding="utf-8")
    assert "    _write_shipped_derived_files()" in src, (
        "main() must call _write_shipped_derived_files() after "
        "_copy_seed_cache()"
    )


# ---------------------------------------------------------------------------
# Direct execution of _write_shipped_derived_files
# ---------------------------------------------------------------------------


def _make_fake_release(release_root: Path) -> None:
    """Populate ``release_root`` with the minimum structure needed
    for ``_write_shipped_derived_files`` to do its work without
    bailing on "missing meta".

    Concretely: a ``.cvfr_routemaster/`` directory with a stub
    ``geo_calibration.json`` carrying ``map_layout`` blocks for
    both sheets, and a stub ``map_images_meta.json`` so
    :func:`write_shipped_map_layout`'s fallback math has the
    inputs it needs."""
    cache_dir = release_root / ".cvfr_routemaster"
    cache_dir.mkdir(parents=True, exist_ok=True)
    geo = {
        "north": {
            "pdf": {"path": "N.pdf", "mtime_ns": 100, "size": 1000},
            "map_layout": {"x": 0.0, "y": 0.0, "scale": 1.0},
            "anchors": [],
        },
        "south": {
            "pdf": {"path": "S.pdf", "mtime_ns": 200, "size": 2000},
            "map_layout": {"x": 0.0, "y": 2000.0, "scale": 1.0},
            "anchors": [],
        },
    }
    (cache_dir / "geo_calibration.json").write_text(
        json.dumps(geo, indent=2), encoding="utf-8"
    )
    meta = {
        "north_pdf": {"path": "N.pdf", "mtime_ns": 100, "size": 1000},
        "south_pdf": {"path": "S.pdf", "mtime_ns": 200, "size": 2000},
        "north_crop": {"height_px": 2000},
        "south_crop": {"height_px": 2000},
    }
    (cache_dir / "map_images_meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


def test_windows_write_shipped_derived_files_writes_chart_sources_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The new build step must bake the three CAAI URLs into
    ``release/.cvfr_routemaster/chart_sources.json``. Otherwise
    a fresh-install user opens Map File Settings to three empty
    fields with no idea what to paste."""
    from scripts import build_release
    from cvfr_routemaster.chart_source import CAAI_CHART_URLS

    release_root = tmp_path / "release"
    release_root.mkdir()
    _make_fake_release(release_root)

    monkeypatch.setattr(build_release, "RELEASE_DIR", release_root)
    monkeypatch.setattr(build_release, "REPO_ROOT", tmp_path)

    build_release._write_shipped_derived_files()

    out = json.loads(
        (release_root / ".cvfr_routemaster" / "chart_sources.json").read_text(
            encoding="utf-8"
        )
    )
    assert out == {
        "north": CAAI_CHART_URLS["north"],
        "south": CAAI_CHART_URLS["south"],
        "back": CAAI_CHART_URLS["back"],
    }


def test_linux_write_shipped_derived_files_writes_chart_sources_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Linux mirror of the Windows test above — same contract."""
    from scripts import build_release_for_linux
    from cvfr_routemaster.chart_source import CAAI_CHART_URLS

    release_root = tmp_path / "release-linux"
    release_root.mkdir()
    _make_fake_release(release_root)

    monkeypatch.setattr(
        build_release_for_linux, "RELEASE_DIR", release_root
    )
    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", tmp_path)

    build_release_for_linux._write_shipped_derived_files()

    out = json.loads(
        (release_root / ".cvfr_routemaster" / "chart_sources.json").read_text(
            encoding="utf-8"
        )
    )
    assert out == {
        "north": CAAI_CHART_URLS["north"],
        "south": CAAI_CHART_URLS["south"],
        "back": CAAI_CHART_URLS["back"],
    }


# ---------------------------------------------------------------------------
# README updates
# ---------------------------------------------------------------------------


def test_windows_readme_mentions_first_launch_download_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The README must tell users about the first-launch download
    flow. A v3.2 release explained what was in ``map-pdfs/``; in
    v3.3+ the equivalent narrative is "the program downloads
    these from CAAI on first launch"."""
    from scripts import build_release

    release_root = tmp_path / "release"
    release_root.mkdir()

    monkeypatch.setattr(build_release, "RELEASE_DIR", release_root)
    monkeypatch.setattr(build_release, "REPO_ROOT", tmp_path)

    build_release._write_readme()
    text = (release_root / "README.txt").read_text(encoding="utf-8")
    assert "download" in text.lower(), (
        "README must explain the first-launch download flow"
    )
    assert "Map File Settings" in text, (
        "README must point users to Map File Settings for URL fallback"
    )
    # Negative: the v3.2 ``map-pdfs/`` artefact line must not appear
    # any more — that directory isn't shipped.
    assert "map-pdfs/" not in text, (
        "v3.3+ release must not advertise a map-pdfs/ subfolder"
    )


def test_windows_readme_includes_simulator_use_disclaimer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The README must reproduce the simulator-only disclaimer
    (verbatim wording lives in
    ``cvfr_routemaster.program_info_dialog.INTENDED_USE_PARAGRAPH``
    — the README's short-form is the user's first encounter with
    this constraint, well before they discover the in-app
    Copyright Information dialog)."""
    from scripts import build_release

    release_root = tmp_path / "release"
    release_root.mkdir()

    monkeypatch.setattr(build_release, "RELEASE_DIR", release_root)
    monkeypatch.setattr(build_release, "REPO_ROOT", tmp_path)

    build_release._write_readme()
    text = (release_root / "README.txt").read_text(encoding="utf-8")
    assert "simulator" in text.lower(), (
        "README must include the flight-simulator-only disclaimer"
    )


# ---------------------------------------------------------------------------
# CAAI URL coverage
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# LICENSE coverage (AGPLv3 §4)
# ---------------------------------------------------------------------------


def test_windows_source_bundle_top_files_includes_license_and_requirements() -> None:
    """AGPLv3 §4 obliges every copy of the program to "conspicuously
    and appropriately publish on each copy an appropriate copyright
    notice" and "keep intact all notices stating that this License
    [...] apply to the code."

    The source bundle therefore MUST include ``LICENSE`` at its top
    level — every shipped ``.py`` file points readers back to that
    file via the "see <http://www.gnu.org/licenses/>" line in the
    per-file header, so the pointer would dangle otherwise.

    Also pin ``requirements.txt`` (a deletion would break
    ``py -m pip install -r requirements.txt`` for source recipients)
    so a future refactor of this tuple has to consciously preserve
    both files."""
    from scripts import build_release

    assert "LICENSE" in build_release.SOURCE_BUNDLE_TOP_FILES, (
        "LICENSE must be in SOURCE_BUNDLE_TOP_FILES — AGPLv3 §4 "
        "requires the license text to ship with every copy of the "
        "program, source bundle included"
    )
    assert "requirements.txt" in build_release.SOURCE_BUNDLE_TOP_FILES, (
        "requirements.txt must be in SOURCE_BUNDLE_TOP_FILES — "
        "without it 'py -m pip install -r requirements.txt' breaks"
    )


def test_windows_build_script_defines_copy_license() -> None:
    """The build script must expose a ``_copy_license`` step so the
    release folder ships ``LICENSE`` next to the .exe. Renaming or
    removing this function would silently drop the license file
    from the binary distribution."""
    from scripts import build_release

    assert hasattr(build_release, "_copy_license"), (
        "build_release.py must define _copy_license() — without it "
        "the release folder ships no LICENSE next to the .exe, "
        "violating AGPLv3 §4"
    )


def test_windows_build_script_main_calls_copy_license() -> None:
    """``_copy_license()`` must be wired into ``main()`` at indent-4
    so the pipeline actually runs it. Defining the function but
    never calling it would be just as bad as not defining it."""
    src = (
        Path(__file__).parent.parent / "scripts" / "build_release.py"
    ).read_text(encoding="utf-8")
    assert "    _copy_license()" in src, (
        "main() must call _copy_license() so LICENSE ends up in "
        "release/ next to the .exe"
    )


def test_copy_license_writes_license_into_release_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end exercise of ``_copy_license``: with a stub repo
    root that has a LICENSE file and an empty release dir, the
    function must produce ``release/LICENSE`` byte-identical to
    the source.

    Uses a tiny stub-LICENSE rather than the real one so the test
    does not depend on the AGPL text staying byte-stable across
    future LICENSE edits (e.g. adding a co-author)."""
    from scripts import build_release

    fake_repo = tmp_path
    fake_release = fake_repo / "release"
    fake_release.mkdir()
    stub_license = b"STUB LICENSE\nCopyright (C) 2026 Lev F.\n"
    (fake_repo / "LICENSE").write_bytes(stub_license)

    monkeypatch.setattr(build_release, "REPO_ROOT", fake_repo)
    monkeypatch.setattr(build_release, "RELEASE_DIR", fake_release)

    build_release._copy_license()

    out = fake_release / "LICENSE"
    assert out.is_file(), "release/LICENSE must exist after _copy_license"
    assert out.read_bytes() == stub_license, (
        "release/LICENSE must be byte-identical to repo-root LICENSE"
    )


def test_copy_license_fails_loud_if_repo_license_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If somebody deletes ``LICENSE`` from the repo root,
    ``_copy_license`` must abort the build via ``sys.exit`` rather
    than silently producing a release without a license file.
    The whole point of having this step is to refuse to ship a
    binary that violates AGPLv3 §4."""
    from scripts import build_release

    fake_repo = tmp_path
    fake_release = fake_repo / "release"
    fake_release.mkdir()
    monkeypatch.setattr(build_release, "REPO_ROOT", fake_repo)
    monkeypatch.setattr(build_release, "RELEASE_DIR", fake_release)

    with pytest.raises(SystemExit) as excinfo:
        build_release._copy_license()
    assert excinfo.value.code == 1, (
        "_copy_license must exit(1) when LICENSE is missing at "
        "repo root — silent success here would be a serious "
        "compliance bug"
    )


def test_windows_readme_listing_mentions_license_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The friend-facing release README must list ``LICENSE`` in its
    "What's in this folder" inventory. If we ship the file but
    never tell the user it exists, recipients miss the most
    important meta-document in the folder."""
    from scripts import build_release

    release_root = tmp_path / "release"
    release_root.mkdir()
    monkeypatch.setattr(build_release, "RELEASE_DIR", release_root)
    monkeypatch.setattr(build_release, "REPO_ROOT", tmp_path)

    build_release._write_readme()
    text = (release_root / "README.txt").read_text(encoding="utf-8")
    assert "LICENSE" in text, (
        "release README must list LICENSE in the 'What's in this "
        "folder' section so recipients know it's there"
    )


def test_source_bundle_readme_mentions_license_file() -> None:
    """The README inside the source.zip must list ``LICENSE`` in
    its inventory and direct readers to it from its License
    section — same rationale as the release README test above,
    mirrored for the source-bundle audience."""
    from scripts import build_release

    text = build_release._source_bundle_readme_text()
    assert "LICENSE" in text, (
        "source-bundle README must list LICENSE in its 'What's in "
        "here' section"
    )
    # The License section in the bundle README should explicitly tell
    # readers the full text is included as LICENSE at the top of the
    # archive — not just point them to gnu.org.
    assert "LICENSE at the top level" in text or "as LICENSE" in text, (
        "source-bundle README License section must point at the "
        "bundled LICENSE file, not only at the gnu.org URL"
    )


# ---------------------------------------------------------------------------
# CAAI URL coverage
# ---------------------------------------------------------------------------


def test_caai_urls_in_chart_source_match_what_program_info_dialog_displays() -> None:
    """``chart_source.CAAI_CHART_URLS`` is the single source of
    truth; ``program_info_dialog.CAAI_CHART_URLS`` re-keys them
    by human label. Catch a regression where someone updates one
    dict and forgets the other."""
    from cvfr_routemaster.chart_source import (
        CAAI_CHART_URLS as canonical,
        SHEET_DISPLAY_NAMES,
        SHEET_KEYS,
    )
    from cvfr_routemaster.program_info_dialog import (
        CAAI_CHART_URLS as dialog_view,
    )

    for key in SHEET_KEYS:
        display = SHEET_DISPLAY_NAMES[key]
        assert dialog_view[display] == canonical[key], (
            f"URL drift for sheet {key!r} between chart_source and "
            f"program_info_dialog"
        )
