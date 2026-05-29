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

"""Smoke test for the calibration reticle cursor builder.

The reticle is a pixmap-based Qt cursor whose visual contract matters
for calibration accuracy:

* a centre hot-spot pixel with a white core (the click point Qt records),
* concentric yellow + blue triangle outlines around it (the aiming
  bullseye the user lines up against the chart's printed VRP triangle),
* a blank gap in the middle of the colour stack (where the printed
  chart edge sits when the reticle is correctly aligned).

If a refactor accidentally drops the blue ring, paints both rings in
the same colour, deletes the centre dot, or moves the centroid away
from the pixmap centre (which would silently offset the cursor
hot-spot from the click target), the calibration is degraded *but the
app still runs and looks roughly right* — exactly the kind of
regression that's invisible to a smoke run and only surfaces as a
larger residual on the next recalibration. These pixel-level checks
fail CI immediately if the contract breaks.

The reticle method is stateless (doesn't touch ``self``), so we call
it through ``MainWindow.__dict__`` without instantiating a window —
keeps the test fast and free of any side-effects from main-window
construction (cache wiring, satellite worker creation, etc.).
"""

from __future__ import annotations

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtGui import QCursor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cvfr_routemaster.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """Shared QApplication — Qt requires exactly one per process and
    QPixmap / QPainter need a GUI app to exist before they're used."""
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def reticle_pixmap(qapp):
    """Build the reticle cursor once and return its pixmap for the
    suite's per-pixel assertions. We call the method via the class so
    the test doesn't depend on a fully-constructed ``MainWindow`` —
    the builder is intentionally stateless."""

    class _UnusedSelf:
        pass

    cursor = MainWindow._build_calibration_reticle_cursor(_UnusedSelf())
    assert isinstance(cursor, QCursor)
    pm = cursor.pixmap()
    assert pm.width() == 64 and pm.height() == 64, (
        f"reticle pixmap is not 64×64: got {pm.width()}×{pm.height()}"
    )
    return pm


def test_centre_dot_has_opaque_white_core(reticle_pixmap) -> None:
    """The centroid hot-spot pixel must be opaque + near-white.

    Qt uses the pixel under the hot-spot as the click coordinate, so
    the user's eye needs an unambiguous "this is THE click point"
    target. A white core inside a black halo is the highest-contrast
    pinpoint we can paint at sub-pixel scale; the assertion guards
    against a refactor that accidentally drops the white fill (e.g.
    leaves only the halo) and turns the dot into a black blob whose
    centre is no longer visually distinguishable from the halo.
    """
    img = reticle_pixmap.toImage()
    px = img.pixelColor(32, 32)
    assert px.alpha() > 200, (
        f"centre pixel is transparent (alpha={px.alpha()}); the click "
        "hot-spot must be opaque so the user sees where the click lands"
    )
    assert px.red() > 200 and px.green() > 200 and px.blue() > 200, (
        f"centre pixel is not near-white "
        f"(rgb={px.red()},{px.green()},{px.blue()}); the dot's white "
        "core was lost, the centroid will read as a black blob and "
        "sub-pixel aiming degrades"
    )


def test_outer_band_contains_yellow_along_apex_column(reticle_pixmap) -> None:
    """The outermost triangle (apex at y=4) must be yellow.

    Yellow on the outside of the band is half of the "yellow+blue
    covers every chart hue" contract; if it goes missing the reticle
    becomes invisible on blue water or green vegetation regions.
    """
    img = reticle_pixmap.toImage()
    for y in range(3, 7):
        px = img.pixelColor(32, y)
        if (
            px.alpha() > 50
            and px.red() > 200
            and px.green() > 180
            and px.blue() < 80
        ):
            return
    pytest.fail(
        "No yellow pixel found along the outer-apex column (x=32, y∈[3,7]) — "
        "the outermost ring of the yellow-blue-blank-blue-yellow band is "
        "missing or not yellow"
    )


def test_blue_band_present_between_outer_yellow_and_blank(reticle_pixmap) -> None:
    """The blue band (level 1, r ≈ 25.6) must contain blue pixels.

    Apex column hits the blue band at y ≈ 32 − 25.6 = 6.4 ± 1 px.
    Both the outer-yellow / blue and blue / inner-yellow blue rings
    bracket the chart-edge target gap — losing the blue ring breaks
    the "alternating colours bracket the chart's printed edge" aiming
    cue.
    """
    img = reticle_pixmap.toImage()
    for y in range(5, 9):
        px = img.pixelColor(32, y)
        if (
            px.alpha() > 50
            and px.blue() > 180
            and px.red() < 130
            and px.green() < 180
        ):
            return
    pytest.fail(
        "No blue pixel found along the apex column at the expected blue-band "
        "position (x=32, y∈[5,9]) — second-level (blue) ring is missing or "
        "the wrong colour"
    )


def test_blank_gap_between_blue_rings_is_transparent(reticle_pixmap) -> None:
    """Level-2 of the band (r ≈ 28 − 2·2.4 = 23.2) must be the BLANK
    gap — i.e. fully transparent on the apex column at y ≈ 32 − 23.2
    ≈ 8.8. If a future change "helpfully" fills this with a colour
    the user loses the chart-edge target.
    """
    img = reticle_pixmap.toImage()
    px = img.pixelColor(32, 9)
    assert px.alpha() < 50, (
        f"Blank gap between the two blue rings is painted "
        f"(alpha={px.alpha()}) — the chart's printed triangle edge no "
        "longer has a colour-free zone to fall into"
    )


def test_cursor_hot_spot_is_pixmap_centre(reticle_pixmap, qapp) -> None:
    """The cursor's hot-spot must be at the pixmap's centre, which is
    also the triangle's centroid. If hot-spot ≠ centroid the recorded
    click is systematically offset from the dot the user is aiming at,
    introducing a per-anchor bias in calibration that has nothing to do
    with the user's aim — pure mathematical drift.
    """

    class _UnusedSelf:
        pass

    cursor = MainWindow._build_calibration_reticle_cursor(_UnusedSelf())
    spot = cursor.hotSpot()
    assert spot.x() == 32 and spot.y() == 32, (
        f"cursor hot-spot is at ({spot.x()},{spot.y()}); must be at the "
        "pixmap centre (32,32) which is the triangle's centroid and the "
        "centre dot — otherwise clicks are offset from where the user aims"
    )
