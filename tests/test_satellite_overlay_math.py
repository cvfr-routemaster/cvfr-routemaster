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

"""Geometry tests for the per-tile satellite overlay.

The "render z=14 tiles natively at chart-scene coords" plan rests
on two quantitative claims:

1. The residual between a Web Mercator tile's true 4-corner
   footprint and the 8-DOF projective transform fitted to those
   4 corners (and probed at the tile *center*) is well under 1
   chart pixel for every tile in an Israel-coverage chart.
2. Adjacent tiles' projective transforms agree *exactly* on their
   shared corners, so no thin-line seam appears at tile boundaries.

If either claim breaks, the overlay strategy breaks — tiles will
either misalign at their boundaries (claim 1) or open visible
hairline gaps between neighbours (claim 2). The white-line seam
artifacts the user observed after the v3 LCC switch were claim
(2) failing under the previous 3-corner-affine fit.

These tests pin the math at four levels:

1. **Closed-form correctness** of ``fit_affine_3pt`` /
   ``affine_apply`` (retained as primitives) and
   ``fit_homography_4pt`` / ``homography_apply`` (the new
   load-bearing pair): identity + known transforms, round-trip,
   degenerate-input rejection.
2. **Tile-corner geometry** of ``tile_corners_lonlat``: round-trip
   against ``world_pixel_to_lonlat`` and consistency with
   ``tile_for_lonlat``.
3. **Sub-pixel residual** of ``tile_to_chart_transform`` (now
   measured at the held-out tile center) against a calibration
   built from realistic Israeli VFR anchor points.
4. **Seam-closing invariant**: two adjacent tiles' projective
   transforms map their shared geographic corners to *identical*
   scene positions — the property that makes the per-tile fit
   safe to render without visible seams.
"""

from __future__ import annotations

import math

import pytest

from cvfr_routemaster.geo_calibration import (
    CalibrationPoint,
    calibration_from_points,
)
from cvfr_routemaster.satellite_overlay_math import (
    MAX_TILE_RESIDUAL_PX,
    TILE_IMAGE_CORNERS_PX,
    TileTransform,
    affine_apply,
    enumerate_chart_tiles,
    fit_affine_3pt,
    fit_homography_4pt,
    homography_apply,
    tile_corners_lonlat,
    tile_to_chart_transform,
)
from cvfr_routemaster.satellite_tiles import (
    TILE_SIZE_PX,
    TileCoord,
    tile_for_lonlat,
    world_pixel_to_lonlat,
)


# --- Affine fit ----------------------------------------------------------


class TestFitAffine3pt:
    """``fit_affine_3pt`` solves a 3-point exact-fit linear system —
    no slack to test, just need to verify each closed-form coefficient
    is computed correctly. Tests build affines from primitive
    transforms (identity, translate, scale, rotate, shear) and check
    that the fit recovers the original coefficients."""

    def test_identity(self) -> None:
        src = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))
        dst = src
        a, b, c, d, e, f = fit_affine_3pt(src, dst)
        assert a == pytest.approx(1.0)
        assert b == pytest.approx(0.0)
        assert c == pytest.approx(0.0)
        assert d == pytest.approx(0.0)
        assert e == pytest.approx(1.0)
        assert f == pytest.approx(0.0)

    def test_translation(self) -> None:
        src = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))
        dst = ((10.0, -5.0), (11.0, -5.0), (10.0, -4.0))
        a, b, c, d, e, f = fit_affine_3pt(src, dst)
        assert a == pytest.approx(1.0)
        assert b == pytest.approx(0.0)
        assert c == pytest.approx(10.0)
        assert d == pytest.approx(0.0)
        assert e == pytest.approx(1.0)
        assert f == pytest.approx(-5.0)

    def test_uniform_scale(self) -> None:
        src = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))
        dst = ((0.0, 0.0), (3.0, 0.0), (0.0, 3.0))
        a, b, c, d, e, f = fit_affine_3pt(src, dst)
        assert a == pytest.approx(3.0)
        assert e == pytest.approx(3.0)
        assert b == pytest.approx(0.0)
        assert d == pytest.approx(0.0)

    def test_rotation_90deg(self) -> None:
        # Rotate 90° CCW: (x, y) → (-y, x).
        src = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))
        dst = ((0.0, 0.0), (0.0, 1.0), (-1.0, 0.0))
        a, b, c, d, e, f = fit_affine_3pt(src, dst)
        assert a == pytest.approx(0.0, abs=1e-9)
        assert b == pytest.approx(-1.0)
        assert d == pytest.approx(1.0)
        assert e == pytest.approx(0.0, abs=1e-9)

    def test_shear(self) -> None:
        # Horizontal shear: (x, y) → (x + 0.5y, y).
        src = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))
        dst = ((0.0, 0.0), (1.0, 0.0), (0.5, 1.0))
        a, b, c, d, e, f = fit_affine_3pt(src, dst)
        assert a == pytest.approx(1.0)
        assert b == pytest.approx(0.5)
        assert e == pytest.approx(1.0)
        assert d == pytest.approx(0.0)

    def test_combined_transform(self) -> None:
        # Build a known affine, push 3 points through it, fit, check
        # we recover the original coefficients.
        truth = (1.5, -0.3, 7.0, 0.4, 1.1, -2.5)

        def push(x: float, y: float) -> tuple[float, float]:
            return affine_apply(truth, x, y)

        src = ((0.0, 0.0), (10.0, 5.0), (-2.0, 8.0))
        dst = (push(*src[0]), push(*src[1]), push(*src[2]))
        recovered = fit_affine_3pt(src, dst)
        for got, want in zip(recovered, truth, strict=True):
            assert got == pytest.approx(want, abs=1e-9)

    def test_collinear_source_raises(self) -> None:
        # All three source points on a single line → no unique affine.
        src = ((0.0, 0.0), (1.0, 1.0), (2.0, 2.0))
        dst = ((0.0, 0.0), (1.0, 0.0), (2.0, 0.0))
        with pytest.raises(ValueError, match="collinear"):
            fit_affine_3pt(src, dst)


class TestAffineApply:
    """``affine_apply`` is a 1-line helper but it's the inverse of
    ``fit_affine_3pt`` for the test suite — round-tripping through
    fit + apply is the strongest check on each individually."""

    def test_round_trip(self) -> None:
        truth = (2.0, 0.5, -1.0, -0.3, 1.7, 4.0)
        for x, y in [(0.0, 0.0), (3.0, 7.0), (-1.5, 2.5)]:
            ax, ay = affine_apply(truth, x, y)
            # Apply by hand and compare.
            assert ax == pytest.approx(2.0 * x + 0.5 * y - 1.0)
            assert ay == pytest.approx(-0.3 * x + 1.7 * y + 4.0)


# --- Homography fit ------------------------------------------------------


class TestFitHomography4pt:
    """``fit_homography_4pt`` solves an 8-DOF projective transform
    that maps 4 source points exactly to 4 destination points.
    Tests build homographies from known-good cases (degenerate
    affines, pure perspective, combined affine+perspective) and
    check the fit recovers a result that round-trips through
    ``homography_apply`` to within machine precision."""

    def test_identity(self) -> None:
        # 4 corners of a unit square mapped to themselves should
        # return the identity-equivalent: m11=m22=m33=1, all other
        # off-diagonal entries 0.
        src = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
        coeffs = fit_homography_4pt(src, src)
        m11, m12, m13, m21, m22, m23, m31, m32, m33 = coeffs
        assert m11 == pytest.approx(1.0)
        assert m22 == pytest.approx(1.0)
        assert m33 == pytest.approx(1.0)
        for value in (m12, m13, m21, m23, m31, m32):
            assert value == pytest.approx(0.0, abs=1e-9)

    def test_affine_input_yields_zero_perspective_coefficients(self) -> None:
        """If the 4 destination points are an affine image of the 4
        source points, the homography solver should recover the
        affine *and* return m13 = m23 = 0, m33 = 1 — i.e. the
        projective form correctly degenerates to the affine case
        for affine inputs. This is the property that keeps the
        per-tile transform identical to the legacy affine when the
        calibration itself is purely affine (e.g. unit tests with a
        synthetic linear calibration)."""
        # Build a known affine (rotation + scale + translation).
        truth_affine = (1.5, -0.3, 7.0, 0.4, 1.1, -2.5)

        def push(x: float, y: float) -> tuple[float, float]:
            return affine_apply(truth_affine, x, y)

        src = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
        dst = tuple(push(*p) for p in src)
        coeffs = fit_homography_4pt(src, dst)
        m11, m12, m13, m21, m22, m23, m31, m32, m33 = coeffs

        # Perspective row must collapse to (0, 0, 1) for affine input.
        assert m13 == pytest.approx(0.0, abs=1e-9)
        assert m23 == pytest.approx(0.0, abs=1e-9)
        assert m33 == pytest.approx(1.0)
        # And the affine sub-block must match the truth affine, in
        # Qt's (m11=a, m21=b, m31=c, m12=d, m22=e, m32=f) mapping.
        a, b, c, d, e, f = truth_affine
        assert m11 == pytest.approx(a)
        assert m21 == pytest.approx(b)
        assert m31 == pytest.approx(c)
        assert m12 == pytest.approx(d)
        assert m22 == pytest.approx(e)
        assert m32 == pytest.approx(f)

    def test_pure_perspective_quad_round_trips(self) -> None:
        """Fit a homography to a non-affine quad-to-quad mapping
        (the destination is a real trapezoid, not a parallelogram)
        and verify all 4 correspondences round-trip exactly through
        ``homography_apply``. This is the case where the projective
        DOF actually matters — an affine fit could not solve this."""
        # Source: unit square. Destination: trapezoid (top edge
        # shorter than bottom edge -- a genuine perspective view).
        src = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
        dst = ((0.2, 0.0), (0.8, 0.0), (1.0, 1.0), (0.0, 1.0))
        coeffs = fit_homography_4pt(src, dst)
        # The perspective row should be nonzero — this isn't an affine.
        m13 = coeffs[2]
        m23 = coeffs[5]
        assert abs(m13) + abs(m23) > 1e-6, (
            "trapezoid-to-square should require perspective DOF"
        )
        # Every source corner must map exactly to its destination.
        for s, d in zip(src, dst, strict=True):
            got = homography_apply(coeffs, *s)
            assert got[0] == pytest.approx(d[0], abs=1e-12)
            assert got[1] == pytest.approx(d[1], abs=1e-12)

    def test_collinear_source_raises(self) -> None:
        # All 4 source points on a single line → singular 8×8 system.
        src = ((0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (3.0, 3.0))
        dst = ((0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0))
        with pytest.raises(ValueError, match="Singular"):
            fit_homography_4pt(src, dst)

    def test_three_collinear_source_raises(self) -> None:
        # 3 of 4 source points collinear → also singular.
        src = ((0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (1.0, 1.0))
        dst = ((0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (1.0, 1.0))
        with pytest.raises(ValueError, match="Singular"):
            fit_homography_4pt(src, dst)


class TestHomographyApply:
    """``homography_apply`` is the projective inverse of
    ``fit_homography_4pt``: round-tripping through fit + apply is
    the strongest check on each individually, but a few direct
    apply-with-known-coeffs tests pin the formula too."""

    def test_identity_homography_returns_input(self) -> None:
        # Identity row-major: m11=m22=m33=1, rest zero.
        identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        for x, y in [(0.0, 0.0), (3.0, 7.0), (-1.5, 2.5)]:
            sx, sy = homography_apply(identity, x, y)
            assert sx == pytest.approx(x)
            assert sy == pytest.approx(y)

    def test_pure_translation(self) -> None:
        # m11=m22=m33=1, m31=10, m32=-5 — translates by (10, -5).
        coeffs = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 10.0, -5.0, 1.0)
        sx, sy = homography_apply(coeffs, 3.0, 4.0)
        assert sx == pytest.approx(13.0)
        assert sy == pytest.approx(-1.0)

    def test_w_zero_raises(self) -> None:
        # Construct a homography where w' vanishes at (1, 0): set
        # m13 = -1, m33 = 1 → w'(1, 0) = -1*1 + 0 + 1 = 0.
        coeffs = (1.0, 0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        with pytest.raises(ValueError, match="vanishing line"):
            homography_apply(coeffs, 1.0, 0.0)


# --- Tile geometry -------------------------------------------------------


class TestTileCornersLonlat:
    """A tile's corner geometry is a Web Mercator inverse on its
    world-pixel bounds. The function should be a thin wrapper over
    :func:`world_pixel_to_lonlat`, ordered NW, NE, SE, SW (matching
    the screen-space convention of top-left → top-right → bottom-
    right → bottom-left).
    """

    def test_corners_match_world_pixel_inverse(self) -> None:
        coord = TileCoord(z=14, x=9779, y=6652)  # LLBG-ish
        nw, ne, se, sw = tile_corners_lonlat(coord)

        # Compute corners directly from world_pixel_to_lonlat for
        # comparison.
        z = coord.z
        tx = coord.x
        ty = coord.y
        expected_nw = world_pixel_to_lonlat(
            tx * TILE_SIZE_PX, ty * TILE_SIZE_PX, z
        )
        expected_ne = world_pixel_to_lonlat(
            (tx + 1) * TILE_SIZE_PX, ty * TILE_SIZE_PX, z
        )
        expected_se = world_pixel_to_lonlat(
            (tx + 1) * TILE_SIZE_PX, (ty + 1) * TILE_SIZE_PX, z
        )
        expected_sw = world_pixel_to_lonlat(
            tx * TILE_SIZE_PX, (ty + 1) * TILE_SIZE_PX, z
        )
        assert nw == expected_nw
        assert ne == expected_ne
        assert se == expected_se
        assert sw == expected_sw

    def test_corner_ordering_is_nw_ne_se_sw(self) -> None:
        # Expand the lat/lon ordering: NW.lat == NE.lat (top edge),
        # SW.lat == SE.lat (bottom edge), NW.lon == SW.lon (left
        # edge), NE.lon == SE.lon (right edge). NW.lat > SW.lat and
        # NE.lon > NW.lon (Israel is in the NE quadrant; longitudes
        # increase rightward, latitudes increase upward).
        coord = TileCoord(z=14, x=9779, y=6652)
        nw, ne, se, sw = tile_corners_lonlat(coord)
        assert nw[1] == pytest.approx(ne[1])  # top edge same lat
        assert sw[1] == pytest.approx(se[1])  # bottom edge same lat
        assert nw[0] == pytest.approx(sw[0])  # left edge same lon
        assert ne[0] == pytest.approx(se[0])  # right edge same lon
        assert nw[1] > sw[1]  # NW north of SW
        assert ne[0] > nw[0]  # NE east of NW

    def test_tile_for_lonlat_round_trip(self) -> None:
        # The tile that contains a corner-adjacent point should be
        # the original tile. Use the centre of each tile's footprint
        # to avoid edge-case ambiguity at exact tile boundaries.
        coord = TileCoord(z=14, x=9779, y=6652)
        nw, ne, se, sw = tile_corners_lonlat(coord)
        centre_lon = (nw[0] + ne[0]) / 2.0
        centre_lat = (nw[1] + sw[1]) / 2.0
        recovered = tile_for_lonlat(centre_lon, centre_lat, coord.z)
        assert recovered == coord


class TestTileImageCornersOrdering:
    """Pin the image-space corner order; if a future refactor flips
    this, every overlay tile will end up flipped horizontally or
    vertically and the regression has to fail loudly here, not
    silently in the rendered output."""

    def test_corners_are_nw_ne_se_sw_in_image_pixels(self) -> None:
        nw, ne, se, sw = TILE_IMAGE_CORNERS_PX
        assert nw == (0.0, 0.0)
        assert ne == (float(TILE_SIZE_PX), 0.0)
        assert se == (float(TILE_SIZE_PX), float(TILE_SIZE_PX))
        assert sw == (0.0, float(TILE_SIZE_PX))


# --- Tile-to-chart transform fit ----------------------------------------


def _make_israel_calibration():
    """Build a 4-anchor calibration using realistic Israeli VFR
    anchor points (LLHA, LLER, LLOV, LLMR). UV values are arbitrary
    but distinct so the calibration's LCC fit has signal to work
    with; lat/lon are real airport coordinates so the
    Mercator-vs-LCC residual is measured under realistic conditions
    rather than against a degenerate test fixture."""
    pdf_fp = {"sha256": "test", "size": 1234}
    points = [
        # Haifa: 32.81°N 35.04°E — top-left of the chart.
        CalibrationPoint(
            code="LLHA", lat=32.81, lon=35.04, u=0.10, v=0.15
        ),
        # Eilat-Ramon: 30.59°N 34.62°E — bottom-left.
        CalibrationPoint(
            code="LLER", lat=30.59, lon=34.62, u=0.05, v=0.85
        ),
        # Eilat-old: 29.55°N 34.96°E — bottom-right.
        CalibrationPoint(
            code="LLOV", lat=29.55, lon=34.96, u=0.20, v=0.95
        ),
        # Mitzpe Ramon: 30.65°N 34.80°E — middle.
        CalibrationPoint(
            code="LLMR", lat=30.65, lon=34.80, u=0.12, v=0.50
        ),
    ]
    return calibration_from_points(pdf_fp, *points)


def test_tile_to_chart_transform_returns_tile_transform() -> None:
    # Smoke test that the function plumbs through and returns the
    # expected dataclass shape.
    cal = _make_israel_calibration()
    coord = TileCoord(z=14, x=9779, y=6652)
    tt = tile_to_chart_transform(coord, cal, pixmap_size=(6000, 8000))
    assert isinstance(tt, TileTransform)
    assert tt.residual_px >= 0.0


def test_tile_to_chart_transform_subpixel_residual_at_z14_israel() -> None:
    """**The load-bearing assertion.**

    Pick several tiles spanning the chart's lat/lon footprint (corner
    tiles + centre) and verify each tile's residual is well under
    :data:`MAX_TILE_RESIDUAL_PX`. If this test ever fails, the
    overlay strategy is invalid for that calibration and the
    fallback to the warp renderer (or a different zoom level) needs
    to engage.

    Under the v3 LCC calibration + 8-DOF projective per-tile fit,
    the homography places all 4 corners of every tile *exactly* at
    their true scene positions (by construction — 4 correspondences
    fully determine an 8-DOF transform). The residual reported by
    :class:`TileTransform.residual_px` is therefore the *interior*
    deviation: the gap between the homography's prediction at the
    held-out tile center and the calibration's projection of that
    same center. This is a measure of LCC curvature *within* the
    tile that the 4-corner fit can't capture; over Israeli tiles
    at z=14 it's ~0.005 chart-px, well under
    :data:`MAX_TILE_RESIDUAL_PX`.

    The bound stays at :data:`MAX_TILE_RESIDUAL_PX` so the test
    keeps working for any future user (e.g. a 60° N chart) where
    the residual would creep up to fractional pixels but stay
    safely under 1 px down to z=14 — at which point this check
    would still catch a regression that pushed it past 1 px.
    """
    cal = _make_israel_calibration()
    pixmap_size = (6000, 8000)
    sample_lonlats = [
        (35.04, 32.81),  # LLHA
        (34.80, 30.65),  # LLMR
        (34.96, 29.55),  # LLOV
        (34.62, 30.59),  # LLER
        (34.85, 31.50),  # central Israel
    ]
    for lon, lat in sample_lonlats:
        coord = tile_for_lonlat(lon, lat, z=14)
        tt = tile_to_chart_transform(coord, cal, pixmap_size=pixmap_size)
        assert tt.residual_px < MAX_TILE_RESIDUAL_PX, (
            f"Tile {coord} residual {tt.residual_px:.4f} px exceeds "
            f"{MAX_TILE_RESIDUAL_PX} px — projection mismatch is "
            f"larger than expected"
        )


def test_tile_to_chart_transform_residual_scales_quadratically_with_tile_size() -> None:
    """Under the v3 LCC calibration + 8-DOF projective per-tile fit
    the residual is measured at the *tile center* (held out from
    the 4-corner fit) — every tile *corner* is placed exactly by
    construction. The center residual captures the LCC curvature
    interior to the tile that the corner-anchored projective
    transform can't represent.

    Empirically the center residual quarters at each zoom step:
    halving the tile span quarters the integrated LCC curvature
    (it's quadratic in tile span — second-order Taylor remainder).
    At the production zooms (z >= 12) the per-tile center residual
    is well under :data:`MAX_TILE_RESIDUAL_PX`, so the overlay
    never silently drops a tile for projection mismatch. At the
    coarsest viable zooms (z = 10) it can exceed 1 px -- production
    never queries z = 10, but if anyone widens
    :func:`_satellite_zoom_levels` to include it the overlay's
    MAX_TILE_RESIDUAL_PX guard will correctly reject those tiles.

    This test pins the quadratic-decay relationship so a future
    refactor that accidentally amplifies the LCC curvature (e.g. by
    using a single-parallel LCC, or a projection mismatched with
    the chart) shows up as a regression *here* rather than as a
    visible misalignment in production.

    Compared to the previous 3-corner-affine fit the absolute
    residual magnitudes are about 4x smaller at every zoom (because
    the projective transform averages the LCC curvature across all
    4 corners rather than concentrating it at the SW held-out
    corner). The quadratic-decay structure is unchanged — it's a
    property of LCC, not of the fit.
    """
    cal = _make_israel_calibration()
    pixmap_size = (6000, 8000)
    lon, lat = 34.85, 31.50
    residuals: dict[int, float] = {}
    for z in (10, 11, 12, 13, 14, 16):
        coord = tile_for_lonlat(lon, lat, z=z)
        tt = tile_to_chart_transform(coord, cal, pixmap_size)
        residuals[z] = tt.residual_px

    # Production-zoom guard: every zoom we actually use must fit
    # comfortably under MAX_TILE_RESIDUAL_PX.
    for z in (12, 13, 14, 16):
        assert residuals[z] < MAX_TILE_RESIDUAL_PX, (
            f"z={z} residual {residuals[z]} px exceeds "
            f"{MAX_TILE_RESIDUAL_PX} px production threshold"
        )

    # Quadratic decay: each finer zoom should reduce residual by
    # roughly 4x (tolerate +/- 10% slack for floating-point noise).
    for z in (11, 12, 13):
        ratio = residuals[z] / residuals[z + 1]
        assert 3.5 < ratio < 4.5, (
            f"Residual ratio at z={z} -> z={z + 1} should be ~4x "
            f"(LCC curvature is quadratic in tile span); got {ratio:.2f}. "
            f"Residuals: {residuals}"
        )


def test_tile_to_chart_transform_rejects_nonpositive_pixmap() -> None:
    cal = _make_israel_calibration()
    coord = TileCoord(z=14, x=9779, y=6652)
    with pytest.raises(ValueError, match="pixmap_size"):
        tile_to_chart_transform(coord, cal, pixmap_size=(0, 100))


def test_tile_transform_to_qtransform_components_order() -> None:
    """The :meth:`to_qtransform_components` method must pack the 9
    coefficients in the order Qt's :class:`QTransform.__init__(m11,
    m12, m13, m21, m22, m23, m31, m32, m33)` expects — i.e.
    row-major over the 3x3 matrix. A quiet reorder here would
    silently warp every overlay tile.
    """
    tt = TileTransform(
        m11=1.0, m12=2.0, m13=3.0,
        m21=4.0, m22=5.0, m23=6.0,
        m31=7.0, m32=8.0, m33=9.0,
        residual_px=0.0,
    )
    components = tt.to_qtransform_components()
    assert components == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0)


def test_tile_transform_round_trip_through_homography_apply() -> None:
    """All 4 of a tile's fitted corners map back through the
    homography to within machine precision — the 8-DOF projective
    fit is exact on the 4 correspondences by construction.

    Previously this test only checked 3 corners (the held-in set
    under the legacy 6-DOF affine fit); now all 4 are in the fit
    so all 4 must reproduce exactly. The 4th-corner check is the
    seam-closing property at the per-tile level — if it ever
    fails, adjacent tiles will no longer share corners exactly
    and the white-line artifact will return.
    """
    cal = _make_israel_calibration()
    coord = TileCoord(z=14, x=9779, y=6652)
    pixmap_size = (6000, 8000)
    tt = tile_to_chart_transform(coord, cal, pixmap_size=pixmap_size)
    coeffs = (
        tt.m11, tt.m12, tt.m13,
        tt.m21, tt.m22, tt.m23,
        tt.m31, tt.m32, tt.m33,
    )

    # Compute each corner's expected scene position directly and
    # verify the homography reproduces it. Unlike the legacy
    # 3-corner-affine test, ALL 4 corners are in the fit.
    nw_lonlat, ne_lonlat, se_lonlat, sw_lonlat = tile_corners_lonlat(coord)
    width, height = pixmap_size
    for img_corner, lonlat in zip(
        TILE_IMAGE_CORNERS_PX,
        (nw_lonlat, ne_lonlat, se_lonlat, sw_lonlat),
        strict=True,
    ):
        u, v = cal.lonlat_to_uv(*lonlat)
        expected = (u * width, v * height)
        got = homography_apply(coeffs, *img_corner)
        assert got[0] == pytest.approx(expected[0], abs=1e-6)
        assert got[1] == pytest.approx(expected[1], abs=1e-6)


def test_adjacent_tiles_share_boundary_corners_exactly() -> None:
    """**Seam-closing invariant.**

    The visible white-line artifact at every tile boundary under
    the legacy 3-corner-affine fit came from adjacent tiles' fits
    disagreeing on the scene position of their shared corners by
    the LCC residual (~0.30 chart-px at z=12 over Israel,
    concentrated at the held-out SW corner of each tile).

    Under the 8-DOF projective fit each tile maps all 4 of its
    corners exactly to their true scene positions, so adjacent
    tiles agree to within floating-point noise (~1e-9 chart-px)
    on every shared corner. This test pins that invariant: a
    future refactor that drops back to an affine fit, or that
    introduces a per-tile transform that doesn't honour the
    4-corner exactness, will fail here loudly.

    The test exercises the X-junction where 4 tiles meet at a
    single geographic point — the worst-case scenario for
    inter-tile disagreement.
    """
    cal = _make_israel_calibration()
    pixmap_size = (6000, 8000)
    # Pick a central z=12 tile and probe the X-junction at its NW
    # corner (= SE corner of the tile diagonally up-left, etc.).
    self_coord = tile_for_lonlat(34.85, 31.50, z=12)
    # The 4 tiles whose corners meet at the X-junction at the NW
    # corner of self_coord: tile coords with +y = south, +x = east.
    nw_tile = TileCoord(z=12, x=self_coord.x - 1, y=self_coord.y - 1)
    ne_tile = TileCoord(z=12, x=self_coord.x,     y=self_coord.y - 1)
    sw_tile = TileCoord(z=12, x=self_coord.x - 1, y=self_coord.y)
    se_tile = self_coord
    # Shared point P is the SE corner of nw_tile (idx 2 in NW,NE,SE,SW
    # ordering), SW of ne_tile (idx 3), NE of sw_tile (idx 1), NW of
    # se_tile (idx 0).
    tile_and_corner_index = {
        "nw_tile": (nw_tile, 2),
        "ne_tile": (ne_tile, 3),  # the previously-residual-off one
        "sw_tile": (sw_tile, 1),
        "se_tile": (se_tile, 0),
    }
    scene_positions: dict[str, tuple[float, float]] = {}
    for label, (coord, idx) in tile_and_corner_index.items():
        tt = tile_to_chart_transform(coord, cal, pixmap_size=pixmap_size)
        coeffs = (
            tt.m11, tt.m12, tt.m13,
            tt.m21, tt.m22, tt.m23,
            tt.m31, tt.m32, tt.m33,
        )
        scene_positions[label] = homography_apply(
            coeffs, *TILE_IMAGE_CORNERS_PX[idx]
        )

    # All 4 must land at the same scene position to within float
    # noise. Compute the worst pairwise distance.
    positions = list(scene_positions.values())
    worst = max(
        math.hypot(positions[i][0] - positions[j][0],
                   positions[i][1] - positions[j][1])
        for i in range(len(positions))
        for j in range(i + 1, len(positions))
    )
    assert worst < 1e-7, (
        f"Adjacent tiles' homography fits disagree at the X-junction "
        f"by {worst:.3e} chart-px. Under the projective fit this "
        f"should be floating-point noise (~1e-9). Positions: "
        f"{scene_positions}"
    )


# --- Chart-tile enumeration --------------------------------------------


class TestEnumerateChartTiles:
    def test_yields_tiles_intersecting_chart_bbox(self) -> None:
        cal = _make_israel_calibration()
        tiles = list(
            enumerate_chart_tiles(
                cal, pixmap_size=(6000, 8000), target_zoom=14
            )
        )
        # An Israel-coverage chart at z=14 should yield several
        # thousand tiles. Lower-bound it at 1000 to catch a
        # regression that drops the bbox computation entirely; the
        # actual count for the Israeli calibration above is in the
        # 4–8k range.
        assert len(tiles) >= 1000
        # Spot-check: a known central tile (e.g. LLBG at z=14) is
        # in the result.
        llbg_tile = tile_for_lonlat(34.886, 32.005, z=14)
        assert llbg_tile in tiles

    def test_zoom_level_propagates(self) -> None:
        cal = _make_israel_calibration()
        tiles_z10 = list(
            enumerate_chart_tiles(
                cal, pixmap_size=(6000, 8000), target_zoom=10
            )
        )
        tiles_z12 = list(
            enumerate_chart_tiles(
                cal, pixmap_size=(6000, 8000), target_zoom=12
            )
        )
        # Each zoom level up multiplies tiles by 4. z=12 should
        # have ~16× as many as z=10. Loose bound to stay robust.
        assert len(tiles_z12) > len(tiles_z10) * 8
        # All emitted tiles have the requested zoom.
        assert all(c.z == 10 for c in tiles_z10)
        assert all(c.z == 12 for c in tiles_z12)

    def test_rejects_nonpositive_pixmap(self) -> None:
        cal = _make_israel_calibration()
        with pytest.raises(ValueError, match="pixmap_size"):
            list(enumerate_chart_tiles(cal, (0, 100), 14))

    def test_rejects_negative_zoom(self) -> None:
        cal = _make_israel_calibration()
        with pytest.raises(ValueError, match="target_zoom"):
            list(enumerate_chart_tiles(cal, (100, 100), -1))


def test_max_tile_residual_px_is_one_pixel() -> None:
    """Pin the threshold; a future refactor that bumps this to e.g.
    5 px would silently allow visibly misaligned tiles, so the
    constant gets its own test."""
    assert MAX_TILE_RESIDUAL_PX == pytest.approx(1.0)
