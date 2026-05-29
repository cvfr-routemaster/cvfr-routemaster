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
