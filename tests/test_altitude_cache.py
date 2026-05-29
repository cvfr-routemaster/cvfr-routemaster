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

"""Tests for the altitude-arrow disk cache.

The cache is just a thin JSON wrapper, so the tests focus on round-trip
fidelity, fingerprint-based invalidation, and graceful degradation when a
manifest is corrupt/partial — exactly the cases that would silently produce
wrong altitudes if mishandled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cvfr_routemaster.altitude_arrows import AltitudeArrow
from cvfr_routemaster.altitude_cache import (
    save_altitude_arrows,
    try_load_altitude_arrows,
)
from cvfr_routemaster.map_crop import CropMeta


def _make_pdf(tmp_path: Path, name: str = "fake.pdf") -> Path:
    p = tmp_path / name
    # The cache only inspects size + mtime, so any non-empty bytes work.
    p.write_bytes(b"%PDF-1.4 not really a PDF, but enough for fingerprinting\n")
    return p


def _crop() -> CropMeta:
    return CropMeta(
        offset_x=10, offset_y=20,
        source_w=2000, source_h=1000,
        cropped_w=1900, cropped_h=900,
    )


def _arrows() -> list[AltitudeArrow]:
    return [
        AltitudeArrow(u=0.10, v=0.20, bearing_deg=0.0, altitudes_ft=(2000,)),
        AltitudeArrow(u=0.50, v=0.50, bearing_deg=180.0, altitudes_ft=(1600, 800)),
        AltitudeArrow(u=0.90, v=0.85, bearing_deg=90.0, altitudes_ft=(1500,)),
    ]


def test_cold_cache_returns_none(tmp_path: Path):
    pdf = _make_pdf(tmp_path)
    out = try_load_altitude_arrows(
        tmp_path, pdf, "north", render_dpi=288.0, crop=_crop(),
    )
    assert out is None


def test_round_trip_preserves_arrow_data(tmp_path: Path):
    pdf = _make_pdf(tmp_path)
    crop = _crop()
    save_altitude_arrows(tmp_path, pdf, "north", _arrows(), render_dpi=288.0, crop=crop)

    out = try_load_altitude_arrows(
        tmp_path, pdf, "north", render_dpi=288.0, crop=crop,
    )
    assert out == _arrows()


def test_cache_invalidates_when_render_dpi_changes(tmp_path: Path):
    pdf = _make_pdf(tmp_path)
    crop = _crop()
    save_altitude_arrows(tmp_path, pdf, "north", _arrows(), render_dpi=288.0, crop=crop)

    out = try_load_altitude_arrows(
        tmp_path, pdf, "north", render_dpi=300.0, crop=crop,
    )
    assert out is None  # different DPI → different pixmap UV → don't accept


def test_cache_invalidates_when_crop_meta_changes(tmp_path: Path):
    """If the chart was re-rendered with a different crop, our pixmap-UV
    arrows are no longer in the calibration's coordinate system. The cache
    must reject in that case so the user gets fresh extraction."""
    pdf = _make_pdf(tmp_path)
    save_altitude_arrows(tmp_path, pdf, "north", _arrows(), render_dpi=288.0, crop=_crop())

    different_crop = CropMeta(
        offset_x=15, offset_y=20,
        source_w=2000, source_h=1000,
        cropped_w=1900, cropped_h=900,
    )
    out = try_load_altitude_arrows(
        tmp_path, pdf, "north", render_dpi=288.0, crop=different_crop,
    )
    assert out is None


def test_cache_invalidates_when_pdf_mtime_changes(tmp_path: Path):
    pdf = _make_pdf(tmp_path)
    crop = _crop()
    save_altitude_arrows(tmp_path, pdf, "north", _arrows(), render_dpi=288.0, crop=crop)

    pdf.write_bytes(b"%PDF-1.4 the user replaced the chart with an updated edition\n")
    out = try_load_altitude_arrows(
        tmp_path, pdf, "north", render_dpi=288.0, crop=crop,
    )
    assert out is None


def test_cache_invalidates_when_pdf_disappears(tmp_path: Path):
    pdf = _make_pdf(tmp_path)
    crop = _crop()
    save_altitude_arrows(tmp_path, pdf, "north", _arrows(), render_dpi=288.0, crop=crop)
    pdf.unlink()

    out = try_load_altitude_arrows(
        tmp_path, pdf, "north", render_dpi=288.0, crop=crop,
    )
    assert out is None


def test_separate_caches_per_sheet(tmp_path: Path):
    """North and south PDFs share the same project root; their caches must
    not collide. Saving north shouldn't poison south."""
    pdf = _make_pdf(tmp_path)
    crop = _crop()
    save_altitude_arrows(tmp_path, pdf, "north", _arrows(), render_dpi=288.0, crop=crop)

    out_n = try_load_altitude_arrows(
        tmp_path, pdf, "north", render_dpi=288.0, crop=crop,
    )
    out_s = try_load_altitude_arrows(
        tmp_path, pdf, "south", render_dpi=288.0, crop=crop,
    )
    assert out_n == _arrows()
    assert out_s is None


def test_cache_handles_corrupt_json_gracefully(tmp_path: Path):
    """A truncated manifest must not crash the loader — just miss."""
    pdf = _make_pdf(tmp_path)
    crop = _crop()
    save_altitude_arrows(tmp_path, pdf, "north", _arrows(), render_dpi=288.0, crop=crop)

    cache_file = tmp_path / ".cvfr_routemaster" / "altitude_arrows_north.json"
    cache_file.write_text("{ this is not valid json", encoding="utf-8")

    out = try_load_altitude_arrows(
        tmp_path, pdf, "north", render_dpi=288.0, crop=crop,
    )
    assert out is None


def test_save_no_ops_when_pdf_missing(tmp_path: Path):
    """If the PDF was moved between extraction and save, the manifest must
    not be written — otherwise the next run would load arrows tied to a
    nonexistent fingerprint and never re-extract."""
    pdf = _make_pdf(tmp_path)
    pdf.unlink()

    save_altitude_arrows(tmp_path, pdf, "north", _arrows(), render_dpi=288.0, crop=_crop())

    cache_file = tmp_path / ".cvfr_routemaster" / "altitude_arrows_north.json"
    assert not cache_file.is_file()


def test_cache_is_path_portable_across_directories(tmp_path: Path):
    """Release-bundle scenario: the dev machine writes a cache against
    ``<dev-repo-root>\\CVFR-NORTH.pdf``; the friend's machine copies
    the PDF + the cache JSON into their own folder (e.g.
    ``C:\\Users\\Friend\\Documents\\release\\``) where the PDF's
    *absolute path* differs but its bytes are identical.

    The cache must hit on the friend's machine — otherwise every
    fresh release would burn 3-5 minutes re-extracting altitude
    arrows on first launch even though the bundled cache is already
    correct. Pinned by comparing only ``mtime_ns`` + ``size`` (which
    survive ``shutil.copy2`` / zip-extract on Windows), not the
    absolute ``path`` field (which is still written for diagnostics).
    """
    import shutil

    dev_root = tmp_path / "dev"
    dev_root.mkdir()
    dev_pdf = _make_pdf(dev_root, "north.pdf")
    crop = _crop()
    save_altitude_arrows(dev_root, dev_pdf, "north", _arrows(), render_dpi=288.0, crop=crop)

    friend_root = tmp_path / "friend"
    friend_root.mkdir()
    friend_pdf = friend_root / "north.pdf"
    # ``shutil.copy2`` preserves mtime — same guarantee zip-extract gives.
    shutil.copy2(dev_pdf, friend_pdf)
    # Copy the entire .cvfr_routemaster dir so the cache JSON moves
    # with the PDF, just like the release zip would.
    shutil.copytree(
        dev_root / ".cvfr_routemaster", friend_root / ".cvfr_routemaster"
    )

    out = try_load_altitude_arrows(
        friend_root, friend_pdf, "north", render_dpi=288.0, crop=crop,
    )
    assert out == _arrows(), (
        "cache should hit on the friend's machine even though the PDF's "
        "absolute path differs from where it was originally extracted"
    )


def test_cache_skips_individually_corrupt_arrow_records(tmp_path: Path):
    """A single bad record in the manifest shouldn't make us throw away the
    other 999 — load-with-best-effort matches how :class:`AltitudeArrow`
    consumers degrade (an unmatched segment shows 'unknown', not a crash)."""
    pdf = _make_pdf(tmp_path)
    crop = _crop()
    save_altitude_arrows(tmp_path, pdf, "north", _arrows(), render_dpi=288.0, crop=crop)

    cache_file = tmp_path / ".cvfr_routemaster" / "altitude_arrows_north.json"
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    payload["arrows"].append({"missing": "fields"})  # corrupt record at the tail
    cache_file.write_text(json.dumps(payload), encoding="utf-8")

    out = try_load_altitude_arrows(
        tmp_path, pdf, "north", render_dpi=288.0, crop=crop,
    )
    assert out == _arrows()
