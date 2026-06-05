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

"""Regression tests for the banded chart-render path
(:mod:`cvfr_routemaster.map_loader`).

Why this file exists
--------------------

Rendering the chart PDFs to on-screen images is *the* basic
first-run function of the app, yet it had no test — and it
regressed into a multi-minute GUI freeze (PyMuPDF's
``get_pixmap`` holds the GIL for the whole render of a ~90 MP A0
sheet, so a single full-page render starves the GUI thread and
Windows paints "(Not Responding)"). The fix renders each page one
horizontal band at a time so the GIL is handed back between
bands; that also lets the worker report determinate progress.

These tests pin the two properties that fix depends on:

* **Correctness** — a banded render must produce the *same image*
  (identical dimensions; pixel-identical on vector content) as the
  legacy single ``get_pixmap`` call, so calibration/altitude
  geometry captured against the old renderer still lines up.
* **Progress** — the worker must emit monotonic ``render_progress``
  ending at 100 %, and surface the one-time explanation exactly
  once, so the long first-run wait reads as deliberate, not hung.

They use a synthetic PDF built on the fly (the real CAAI charts
are copyrighted and not shipped/committed), so the suite stays
self-contained.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

pytest.importorskip("fitz")
pytest.importorskip("PySide6")

import fitz  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cvfr_routemaster.map_loader import (  # noqa: E402
    ONE_TIME_RENDER_MESSAGE,
    RENDER_BAND_PX,
    MapLoadWorker,
    render_page_banded,
)


@pytest.fixture(scope="module")
def _gui_app() -> QApplication:
    """A ``QApplication`` so ``QImage``/``QPainter`` raster ops work
    headless. Match the rest of the GUI suite's app type (a single shared
    ``QApplication`` per process) to avoid a Qt teardown crash from mixing
    application classes."""
    return QApplication.instance() or QApplication([])


def _make_synthetic_chart_pdf(path: Path, *, width: int, height: int) -> None:
    """Write a tall multi-element PDF: a dense grid + text crossing many
    band boundaries, so a banded render genuinely exercises seam handling
    rather than rendering a single trivial band."""
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    shape = page.new_shape()
    for y in range(0, height, 37):
        shape.draw_line((0, y), (width, y))
    for x in range(0, width, 53):
        shape.draw_line((x, 0), (x, height))
    shape.finish(color=(0, 0, 0), width=1.2)
    shape.commit()
    # Text near the vertical centre so it straddles an interior band seam.
    page.insert_text((40, height / 2.0), "TEST CHART 12345", fontsize=44)
    doc.save(str(path))
    doc.close()


def _qimage_to_rgb_array(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    h, w, bpl = img.height(), img.width(), img.bytesPerLine()
    buf = bytes(img.constBits()[: bpl * h])
    return np.frombuffer(buf, np.uint8).reshape(h, bpl)[:, : w * 3].reshape(h, w, 3)


def _full_render(path: Path, mat: fitz.Matrix) -> QImage:
    doc = fitz.open(str(path))
    try:
        pix = doc[0].get_pixmap(matrix=mat, alpha=False)
        return QImage(
            pix.samples,
            pix.width,
            pix.height,
            pix.stride,
            QImage.Format.Format_RGB888,
        ).copy()
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# render_page_banded — correctness
# ---------------------------------------------------------------------------


def test_banded_render_matches_full_render_dimensions(
    _gui_app: QGuiApplication, tmp_path: Path
) -> None:
    """The composited banded image must be exactly the same size as a
    single full-page ``get_pixmap`` — any off-by-one in the band-offset
    math would shift calibration anchors."""
    pdf = tmp_path / "synth.pdf"
    _make_synthetic_chart_pdf(pdf, width=600, height=2000)
    mat = fitz.Matrix(1.7, 1.7)

    full = _full_render(pdf, mat)
    banded = render_page_banded(pdf, mat)

    assert (banded.width(), banded.height()) == (full.width(), full.height())


def test_banded_render_is_pixel_identical_on_vector_content(
    _gui_app: QGuiApplication, tmp_path: Path
) -> None:
    """On pure vector content (lines + text) the seam-overlap margin makes
    the banded result pixel-identical to a full render — proving banding
    isn't a lossy approximation."""
    pdf = tmp_path / "synth.pdf"
    _make_synthetic_chart_pdf(pdf, width=600, height=2000)
    mat = fitz.Matrix(1.7, 1.7)

    full = _qimage_to_rgb_array(_full_render(pdf, mat))
    banded = _qimage_to_rgb_array(render_page_banded(pdf, mat))

    assert full.shape == banded.shape
    assert np.array_equal(full, banded)


def test_banded_render_spans_multiple_bands(
    _gui_app: QGuiApplication, tmp_path: Path
) -> None:
    """A tall page must actually be split — otherwise the GIL-yielding
    property we rely on (and the progress emission) wouldn't kick in."""
    pdf = tmp_path / "synth.pdf"
    _make_synthetic_chart_pdf(pdf, width=600, height=2000)
    mat = fitz.Matrix(1.7, 1.7)

    out_h = render_page_banded(pdf, mat).height()
    expected_bands = max(1, math.ceil(out_h / RENDER_BAND_PX))
    assert expected_bands >= 3  # sanity: this fixture is tall enough

    calls: list[tuple[int, int]] = []
    render_page_banded(pdf, mat, on_band=lambda d, t: calls.append((d, t)))

    assert [c[0] for c in calls] == list(range(1, expected_bands + 1))
    assert all(total == expected_bands for _done, total in calls)


def test_banded_render_progress_callback_is_monotonic(
    _gui_app: QGuiApplication, tmp_path: Path
) -> None:
    """``done`` must climb 1..total with the same ``total`` throughout, so a
    caller can compute a never-decreasing percentage."""
    pdf = tmp_path / "synth.pdf"
    _make_synthetic_chart_pdf(pdf, width=400, height=1600)
    mat = fitz.Matrix(2.0, 2.0)

    calls: list[tuple[int, int]] = []
    render_page_banded(pdf, mat, on_band=lambda d, t: calls.append((d, t)))

    assert calls, "expected at least one band"
    dones = [d for d, _ in calls]
    assert dones == sorted(dones)
    assert dones[-1] == calls[-1][1]


# ---------------------------------------------------------------------------
# MapLoadWorker — end-to-end render path + progress contract
# ---------------------------------------------------------------------------


def test_worker_emits_one_time_message_then_monotonic_progress_to_100(
    _gui_app: QGuiApplication, tmp_path: Path
) -> None:
    """Driving the worker over synthetic north/south PDFs must:

    * surface the one-time explanation exactly once (label populated), and
    * stream a non-decreasing percentage that ends at 100,

    so the GUI's determinate bar moves forward and the user is told this is
    a one-time cost."""
    north = tmp_path / "north.pdf"
    south = tmp_path / "south.pdf"
    _make_synthetic_chart_pdf(north, width=500, height=1500)
    _make_synthetic_chart_pdf(south, width=500, height=1500)

    # project_root=None skips the PNG disk cache, exercising the pure
    # render path (the one that froze the GUI).
    worker = MapLoadWorker(str(north), str(south), project_root=None)

    progress: list[tuple[int, str]] = []
    finished: list[object] = []
    failed: list[str] = []
    worker.render_progress.connect(lambda pct, lbl: progress.append((pct, lbl)))
    worker.finished.connect(lambda payload: finished.append(payload))
    worker.failed.connect(lambda msg: failed.append(msg))

    # Called synchronously (no thread): same-thread signals fire inline.
    worker.run()

    assert not failed, f"render failed: {failed}"

    labelled = [lbl for _pct, lbl in progress if lbl]
    assert labelled == [ONE_TIME_RENDER_MESSAGE], (
        "the one-time explanation must be emitted exactly once"
    )

    pcts = [pct for pct, _lbl in progress]
    assert pcts == sorted(pcts), f"percent went backwards: {pcts}"
    assert pcts[0] == 0
    assert pcts[-1] == 100
    # North fills the first half, South the second — so we must cross 50.
    assert any(p >= 50 for p in pcts)


def test_worker_finishes_with_two_nonnull_images(
    _gui_app: QGuiApplication, tmp_path: Path
) -> None:
    """The whole point: ``finished`` carries a usable (north, south) image
    pair built off the banded renderer."""
    north = tmp_path / "north.pdf"
    south = tmp_path / "south.pdf"
    _make_synthetic_chart_pdf(north, width=500, height=1500)
    _make_synthetic_chart_pdf(south, width=500, height=1500)

    worker = MapLoadWorker(str(north), str(south), project_root=None)
    finished: list[object] = []
    worker.finished.connect(lambda payload: finished.append(payload))
    worker.run()

    assert len(finished) == 1
    payload = finished[0]
    assert isinstance(payload, tuple) and len(payload) == 2
    img_n, img_s = payload
    assert isinstance(img_n, QImage) and not img_n.isNull()
    assert isinstance(img_s, QImage) and not img_s.isNull()
    assert worker.render_info.keys() == {"north", "south"}


def test_one_time_message_states_it_happens_once() -> None:
    """Guard the user-facing wording: it must actually communicate the
    once-per-product nature (the explicit ask). A future copy edit that
    drops that meaning should fail here."""
    text = ONE_TIME_RENDER_MESSAGE.lower()
    assert "one-time" in text or "once" in text
    assert "cvfr" in text and "lsa" in text
    assert "instant" in text  # future launches load instantly from cache
