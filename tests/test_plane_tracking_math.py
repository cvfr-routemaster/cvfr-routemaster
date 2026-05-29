"""Tests for :mod:`cvfr_routemaster.plane_tracking`.

The pure math helper computes where to centre the ``QGraphicsView``
so a tracked VATSIM plane sits two-thirds of the viewport ahead of
itself along its heading and one-third behind. The user spec pins
four cardinal cases exactly; these tests enforce those, plus a few
diagonals to catch a regression that would, say, accidentally drop
the y-axis offset (the kind of thing a careless refactor of the
sin/cos pair could do).

Why a separate test file: the helper is Qt-light (it imports
``QPointF`` for the return type and nothing else), so spinning up
a ``QApplication`` for these specific assertions is wasteful. We
keep them in their own module to make that boundary explicit.
"""

from __future__ import annotations

import math

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QPointF  # noqa: E402

from cvfr_routemaster.plane_tracking import (  # noqa: E402
    compute_tracking_view_center,
)


# ---------------------------------------------------------------
# Cardinal cases — the user spec pins these exactly.
# ---------------------------------------------------------------


# A square 1200x1200 viewport with view_scale=1 keeps the
# bookkeeping trivial: viewport pixel == scene unit, so the
# returned ``QPointF`` reads directly as "plane is at this scene
# pos and centre is offset by this many pixels".
SQUARE_W = 1200
SQUARE_H = 1200
SQUARE_SIXTH = 200.0  # W / 6 == H / 6 in the square case


class TestCardinalHeadings:
    """All four cardinal headings land on exactly 1/3 from the
    corresponding edge, with the perpendicular axis at the
    viewport centre line.
    """

    def test_north_heading_places_plane_in_bottom_third(self) -> None:
        # Forward = (0, -1). Plane should sit at viewport position
        # (W/2, 2H/3), i.e. centre.y + H/6 below the view centre.
        # So the *view centre* in scene coords must be H/6 ABOVE
        # the plane (smaller y = higher up in Qt's y-down world).
        plane = QPointF(500.0, 500.0)

        centre = compute_tracking_view_center(
            plane, heading_deg=0.0,
            view_w_px=SQUARE_W, view_h_px=SQUARE_H, view_scale=1.0,
        )

        assert centre.x() == pytest.approx(plane.x(), abs=1e-9)
        assert centre.y() == pytest.approx(plane.y() - SQUARE_SIXTH)

    def test_east_heading_places_plane_in_left_third(self) -> None:
        # Forward = (1, 0). Plane at viewport (W/3, H/2), so
        # centre is W/6 to the RIGHT of the plane in scene coords.
        plane = QPointF(500.0, 500.0)

        centre = compute_tracking_view_center(
            plane, heading_deg=90.0,
            view_w_px=SQUARE_W, view_h_px=SQUARE_H, view_scale=1.0,
        )

        assert centre.x() == pytest.approx(plane.x() + SQUARE_SIXTH)
        assert centre.y() == pytest.approx(plane.y(), abs=1e-9)

    def test_south_heading_places_plane_in_top_third(self) -> None:
        # Forward = (0, 1). Plane at viewport (W/2, H/3), centre
        # H/6 BELOW the plane.
        plane = QPointF(500.0, 500.0)

        centre = compute_tracking_view_center(
            plane, heading_deg=180.0,
            view_w_px=SQUARE_W, view_h_px=SQUARE_H, view_scale=1.0,
        )

        assert centre.x() == pytest.approx(plane.x(), abs=1e-9)
        assert centre.y() == pytest.approx(plane.y() + SQUARE_SIXTH)

    def test_west_heading_places_plane_in_right_third(self) -> None:
        # Forward = (-1, 0). Plane at viewport (2W/3, H/2), centre
        # W/6 to the LEFT of the plane.
        plane = QPointF(500.0, 500.0)

        centre = compute_tracking_view_center(
            plane, heading_deg=270.0,
            view_w_px=SQUARE_W, view_h_px=SQUARE_H, view_scale=1.0,
        )

        assert centre.x() == pytest.approx(plane.x() - SQUARE_SIXTH)
        assert centre.y() == pytest.approx(plane.y(), abs=1e-9)


# ---------------------------------------------------------------
# Diagonal cases — pin the per-axis decomposition.
# ---------------------------------------------------------------


class TestDiagonalHeadings:
    """Diagonals decompose into the per-axis cardinal formulae.

    Both axes carry a contribution proportional to sin/cos of the
    heading; a refactor that drops one of those would let the
    plane drift off the heading axis at non-cardinal angles, which
    is exactly the regression these guards catch.
    """

    def test_northeast_heading_offsets_both_axes_positively(self) -> None:
        # Heading 45 = NE. forward = (sin45, -cos45) = (0.707, -0.707).
        # Centre offset: (+0.707 * W/6, -0.707 * H/6).
        plane = QPointF(0.0, 0.0)
        s = math.sin(math.radians(45.0))
        expected_dx = s * SQUARE_W / 6.0
        expected_dy = -s * SQUARE_H / 6.0  # cos45 == sin45

        centre = compute_tracking_view_center(
            plane, heading_deg=45.0,
            view_w_px=SQUARE_W, view_h_px=SQUARE_H, view_scale=1.0,
        )

        assert centre.x() == pytest.approx(expected_dx)
        assert centre.y() == pytest.approx(expected_dy)

    def test_southwest_heading_mirrors_northeast(self) -> None:
        # Heading 225 = SW. Mirror image of NE, both signs flip.
        plane = QPointF(0.0, 0.0)
        s = math.sin(math.radians(45.0))

        centre = compute_tracking_view_center(
            plane, heading_deg=225.0,
            view_w_px=SQUARE_W, view_h_px=SQUARE_H, view_scale=1.0,
        )

        assert centre.x() == pytest.approx(-s * SQUARE_W / 6.0)
        assert centre.y() == pytest.approx(s * SQUARE_H / 6.0)

    def test_diagonal_plane_stays_inside_viewport(self) -> None:
        # For any heading the plane's viewport-position offset
        # from centre is bounded by W/6 in x and H/6 in y, so the
        # plane is guaranteed to sit inside the inner 2/3 box of
        # the viewport — well clear of the edges regardless of
        # aspect ratio. Sanity-check this on a non-square viewport
        # at a non-axis-aligned heading.
        plane = QPointF(0.0, 0.0)
        w, h = 1920, 1080

        for heading in range(0, 360, 17):
            centre = compute_tracking_view_center(
                plane, heading_deg=float(heading),
                view_w_px=w, view_h_px=h, view_scale=1.0,
            )
            # plane viewport offset from centre is the NEGATIVE of
            # the scene centre's offset from plane (modulo view_scale=1)
            plane_offset_from_view_centre_x = -centre.x()
            plane_offset_from_view_centre_y = -centre.y()
            assert abs(plane_offset_from_view_centre_x) <= w / 6.0 + 1e-9
            assert abs(plane_offset_from_view_centre_y) <= h / 6.0 + 1e-9


# ---------------------------------------------------------------
# view_scale and aspect-ratio scaling.
# ---------------------------------------------------------------


class TestViewScaleAndAspectRatio:
    """The viewport offset is in screen pixels, the scene offset
    is screen-pixels / view_scale. Pin that conversion so a future
    refactor doesn't accidentally double-apply (or drop) the scale.
    """

    def test_offset_shrinks_when_view_is_zoomed_in(self) -> None:
        # At view_scale=2 (zoomed in), one screen pixel is half a
        # scene unit, so the same desired viewport offset
        # translates to HALF the scene offset.
        plane = QPointF(0.0, 0.0)

        centre = compute_tracking_view_center(
            plane, heading_deg=90.0,  # east
            view_w_px=SQUARE_W, view_h_px=SQUARE_H, view_scale=2.0,
        )

        assert centre.x() == pytest.approx(SQUARE_SIXTH / 2.0)
        assert centre.y() == pytest.approx(0.0, abs=1e-9)

    def test_nonsquare_viewport_offsets_proportionally(self) -> None:
        # 1920x1080 viewport, heading east: only the x axis carries
        # an offset, scaled to W/6 of the wider edge.
        plane = QPointF(0.0, 0.0)

        centre = compute_tracking_view_center(
            plane, heading_deg=90.0,
            view_w_px=1920, view_h_px=1080, view_scale=1.0,
        )

        assert centre.x() == pytest.approx(1920 / 6.0)
        assert centre.y() == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------
# Degenerate-input guards.
# ---------------------------------------------------------------


class TestDegenerateInputs:
    """The helper has to be total — MainWindow may call it before
    the QGraphicsView's transform has finished settling, before
    any chart is loaded, etc. Returning the plane's own scene pos
    (a no-op offset) is the safe fallback in those cases.
    """

    @pytest.mark.parametrize(
        "w, h",
        [(0, 1000), (1000, 0), (0, 0), (-1, 100), (100, -1)],
    )
    def test_zero_or_negative_viewport_returns_plane_pos(
        self, w: int, h: int
    ) -> None:
        plane = QPointF(42.0, -17.0)
        centre = compute_tracking_view_center(
            plane, heading_deg=33.0,
            view_w_px=w, view_h_px=h, view_scale=1.0,
        )
        assert centre.x() == pytest.approx(plane.x())
        assert centre.y() == pytest.approx(plane.y())

    def test_zero_view_scale_is_treated_as_unit(self) -> None:
        plane = QPointF(0.0, 0.0)
        # A zero or negative view_scale shouldn't divide-by-zero
        # — it's promoted to 1.0 and treated like an unscaled
        # transform.
        centre_zero = compute_tracking_view_center(
            plane, heading_deg=90.0,
            view_w_px=SQUARE_W, view_h_px=SQUARE_H, view_scale=0.0,
        )
        centre_unit = compute_tracking_view_center(
            plane, heading_deg=90.0,
            view_w_px=SQUARE_W, view_h_px=SQUARE_H, view_scale=1.0,
        )
        assert centre_zero.x() == pytest.approx(centre_unit.x())
        assert centre_zero.y() == pytest.approx(centre_unit.y())

    def test_heading_wraps_modulo_360(self) -> None:
        # heading=450 == heading=90 (sin/cos are periodic).
        plane = QPointF(0.0, 0.0)
        centre_450 = compute_tracking_view_center(
            plane, heading_deg=450.0,
            view_w_px=SQUARE_W, view_h_px=SQUARE_H, view_scale=1.0,
        )
        centre_90 = compute_tracking_view_center(
            plane, heading_deg=90.0,
            view_w_px=SQUARE_W, view_h_px=SQUARE_H, view_scale=1.0,
        )
        assert centre_450.x() == pytest.approx(centre_90.x())
        assert centre_450.y() == pytest.approx(centre_90.y())
