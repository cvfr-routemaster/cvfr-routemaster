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

"""Geometry for the per-tile satellite overlay.

This module is the "where does each tile go on the chart?" half of the
satellite overlay: pure Python, no Qt dependency, no numpy. Everything
here is testable in isolation with stdlib only, which matters because
the projection-mismatch-residual claim ("sub-pixel at z=14 over Israel")
is the load-bearing assumption that lets us swap an O(chart-area) warp
for an O(visible-tiles) overlay. Wrong residuals here mean overlays
will misalign at tile boundaries, so the math gets its own dedicated
module + extensive tests.

Background — why we need a projective transform per tile
--------------------------------------------------------

The chart calibration is a 4-anchor Lambert Conformal Conic fit
(see :class:`SheetGeoCalibration`). The satellite tiles are Web
Mercator (EPSG:3857). At our latitudes (~31° N) and the small
extent of one z=14 tile (~2.4 km on a side) the two projections
agree to a fraction of a pixel — but the two projections *do not*
agree at chart scale (10–100 km), so we can't just place all tiles
with a single global transform. Each tile gets its own 8-DOF
projective transform (homography) fit to its own 4 corners, valid
for *that tile's footprint*.

Why projective, not affine?
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Under the v3 LCC pipeline the composed map ``tile-pixel → world
Mercator (lon, lat) → LCC (X, Y) → chart-UV → chart-scene-px`` is
*not* globally affine: the ``(lon, lat) → (X, Y)`` step is
non-linear and the ``world-Mercator → (lon, lat)`` step contributes
a (smaller) non-linearity too. A 6-DOF affine fitted through 3 of
the tile's 4 corners therefore mis-places the 4th by the local LCC
curvature integrated over the tile span — ~0.30 chart-px at z=12,
~0.075 at z=13, ~0.019 at z=14 (over Israel).

That residual is *concentrated at a single corner*: the SW (the
held-out one). At every 4-tile X-junction the geographic point P
is the SE corner of the NW tile (exact in NW's fit), the NE of the
SW tile (exact), the NW of the SE tile (exact), and the SW of the
NE tile (held out -> residual-off). Adjacent tile fits disagree
on the scene position of P by exactly that residual, opening a
hairline gap along every western and southern tile edge — visible
as thin vertical white lines at every tile column boundary even
at z=14 (verified empirically post-LCC).

An 8-DOF projective transform fitted through *all 4* tile corners
removes this entirely: the homography maps each corner to its true
scene position exactly. Adjacent tiles share the 2 corners of every
common edge by construction (both calibrations agree on the
projection of any single lat/lon), and a projective transform maps
the straight line between 2 source points to the straight line
between their 2 image points, so the shared edge is *one* straight
line in scene space — no gap, no overlap, no anti-aliasing seam.

Stage breakdown
---------------

1. :func:`tile_corners_lonlat` gives the 4 lat/lon corners of a
   Mercator tile.
2. The calibration's :meth:`SheetGeoCalibration.lonlat_to_uv`
   converts each corner to chart UV (∈ [0, 1]²); multiplying by
   the chart pixmap's ``(width, height)`` gives chart-scene pixel
   coords.
3. :func:`fit_homography_4pt` fits the 8-DOF projective transform
   that maps the tile's image-pixel-space corners (a known 256×256
   box) exactly to the 4 chart-scene corners.
4. The tile *center* (image-pixel ``(128, 128)``) is *not* used in
   the fit; we project it through the fitted homography and measure
   the residual against its true scene coords (computed via
   ``world_pixel_to_lonlat`` of the tile-center world pixel through
   the calibration). This residual is a direct probe of the LCC
   curvature *interior to the tile* — i.e. the deviation the
   projective fit cannot capture given only 4 boundary samples.
   The overlay manager rejects tiles whose residual exceeds
   :data:`MAX_TILE_RESIDUAL_PX` (1 chart pixel).

The "use 4 corners exactly, validate at the center" pattern means
the residual is now a *projection-quality* measure on interior
geometry rather than a per-tile boundary-disagreement measure.
Boundary disagreement is structurally zero under the projective
fit. :func:`fit_affine_3pt` / :func:`affine_apply` remain as
reusable primitives (they're tested independently and other
diagnostics use them) but :func:`tile_to_chart_transform` no
longer relies on them.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cvfr_routemaster.satellite_tiles import (
    TILE_SIZE_PX,
    TileCoord,
    bbox_to_tiles,
    world_pixel_to_lonlat,
)

if TYPE_CHECKING:
    from cvfr_routemaster.geo_calibration import SheetGeoCalibration


#: Maximum residual (in chart-scene pixels) between the homography's
#: prediction at the tile *center* and the true scene position of
#: that center computed through the calibration, measured per tile.
#: A tile that exceeds this is treated as too distorted to render
#: cleanly and falls back to the missing-tile placeholder. The
#: threshold is deliberately conservative: 1 px means "never
#: visibly misaligned even at maximum zoom" — at our latitudes the
#: actual center residual at z=14 over Israel is ~0.005 px, well
#: under the limit, but a future user (e.g. a Russian VFR chart at
#: 60° N where Mercator vs LCC residuals at z=14 jump to ~1 px per
#: tile) would correctly fall back rather than ship misaligned
#: tiles. Note: under the projective (homography) fit the residual
#: at every *corner* is exactly zero, so the relevant residual is
#: the held-out tile center.
MAX_TILE_RESIDUAL_PX: float = 1.0


@dataclass(frozen=True, slots=True)
class TileTransform:
    """8-DOF projective transform mapping tile image-space pixels
    to chart scene-space pixels.

    Stored as 9 coefficients in row-major matrix order::

        | m11 m12 m13 |
        | m21 m22 m23 |
        | m31 m32 m33 |

    Applied to a tile-pixel point ``(x, y)`` in homogeneous form
    ``[x, y, 1]`` with Qt's *row-vector / column-major* convention::

        w'      = m13 * x + m23 * y + m33
        scene_x = (m11 * x + m21 * y + m31) / w'
        scene_y = (m12 * x + m22 * y + m32) / w'

    Where ``(x, y)`` is in the tile's own image coordinate system
    (0..256 in both axes, with origin at the top-left of the tile
    image — same as :class:`PIL.Image` and :class:`QPixmap`).

    Why projective and not affine?
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Under the v3 LCC calibration the chart-side projection of a
    Mercator tile's 4 corners is *not* an affine map — there's
    residual LCC curvature integrated over the tile. A 6-DOF affine
    fit through 3 corners leaves the 4th off by that residual (~0.30
    chart-px at z=12 over Israel), concentrated at the SW tile
    corner where it shows up as a visible thin-line seam artifact
    between adjacent tiles. An 8-DOF projective transform fitted
    through all 4 corners lands every corner exactly; adjacent tiles
    therefore share their 2 common-edge corners by construction
    (both fits agree on the projection of any single lat/lon point)
    and the projective image of the straight line between those 2
    corners is the same straight line in scene space for both
    tiles — closing the seam.

    The :attr:`residual_px` field captures the Euclidean distance,
    in chart-scene pixels, between the homography's prediction of
    the *tile center* and that center's true scene position
    computed directly through the calibration. Under projective
    every corner residual is structurally zero, so the center is
    the natural held-out probe point — it measures the LCC
    curvature *interior to the tile* that the 4-corner fit can't
    capture. This is the metric by which the overlay manager
    decides whether the tile is safe to draw — see
    :data:`MAX_TILE_RESIDUAL_PX`.

    For affine cases (no perspective), ``m13 = m23 = 0`` and
    ``m33 = 1``, so ``w' = 1`` and the formulas reduce to the
    standard 6-DoF form. The :func:`fit_homography_4pt` solver
    returns those values for genuinely-affine inputs (e.g. when
    the chart calibration itself is purely affine), so this class
    correctly degenerates to an affine in that case.
    """

    m11: float
    m12: float
    m13: float
    m21: float
    m22: float
    m23: float
    m31: float
    m32: float
    m33: float
    residual_px: float

    def to_qtransform_components(
        self,
    ) -> tuple[
        float, float, float,
        float, float, float,
        float, float, float,
    ]:
        """Return the 9 coefficients in the order Qt's
        :meth:`QTransform.__init__(m11, m12, m13, m21, m22, m23, m31, m32, m33)`
        expects.

        Qt stores 2-D projective transforms as a 3×3 matrix where::

            x' = (m11 * x + m21 * y + m31) / (m13 * x + m23 * y + m33)
            y' = (m12 * x + m22 * y + m32) / (m13 * x + m23 * y + m33)

        Our field names already match Qt's element naming exactly,
        so this method just packs them in row-major (matrix-natural)
        order. The math module doesn't import Qt — the caller does
        the actual ``QTransform(m11, m12, m13, m21, m22, m23, m31,
        m32, m33)`` construction with these numbers.
        """
        return (
            self.m11, self.m12, self.m13,
            self.m21, self.m22, self.m23,
            self.m31, self.m32, self.m33,
        )


def tile_corners_lonlat(
    coord: TileCoord,
) -> tuple[
    tuple[float, float],
    tuple[float, float],
    tuple[float, float],
    tuple[float, float],
]:
    """Return the 4 (lon, lat) corners of a Web Mercator tile.

    Order matches :data:`TILE_IMAGE_CORNERS_PX` below: NW, NE, SE,
    SW. World-pixel y increases southward (Web Mercator
    convention) so a tile's *top* edge is at world-pixel
    ``y = ty * TILE_SIZE_PX`` (= NORTH edge in lat) and its
    *bottom* edge is at ``(ty + 1) * TILE_SIZE_PX`` (= SOUTH).

    Lat/lon are computed via :func:`world_pixel_to_lonlat`, which
    is the canonical Web Mercator inverse — the round-trip
    ``lonlat → world_pixel → lonlat`` is identity to float
    precision, so this function commutes with :func:`tile_for_lonlat`.
    """
    z, tx, ty = coord.z, coord.x, coord.y
    px_left = float(tx * TILE_SIZE_PX)
    px_right = float((tx + 1) * TILE_SIZE_PX)
    py_top = float(ty * TILE_SIZE_PX)
    py_bottom = float((ty + 1) * TILE_SIZE_PX)
    nw = world_pixel_to_lonlat(px_left, py_top, z)
    ne = world_pixel_to_lonlat(px_right, py_top, z)
    se = world_pixel_to_lonlat(px_right, py_bottom, z)
    sw = world_pixel_to_lonlat(px_left, py_bottom, z)
    return nw, ne, se, sw


#: The 4 corners of a tile's image, in tile-pixel space, in the
#: same order as :func:`tile_corners_lonlat`'s output (NW, NE, SE,
#: SW). This is the source side of the affine fit.
TILE_IMAGE_CORNERS_PX: tuple[
    tuple[float, float],
    tuple[float, float],
    tuple[float, float],
    tuple[float, float],
] = (
    (0.0, 0.0),
    (float(TILE_SIZE_PX), 0.0),
    (float(TILE_SIZE_PX), float(TILE_SIZE_PX)),
    (0.0, float(TILE_SIZE_PX)),
)


def fit_affine_3pt(
    src_pts: tuple[
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
    ],
    dst_pts: tuple[
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
    ],
) -> tuple[float, float, float, float, float, float]:
    """Fit the 6-DOF affine that maps each ``src_pts[i]`` exactly to
    ``dst_pts[i]``.

    Returns ``(a, b, c, d, e, f)`` such that::

        dst_x = a * src_x + b * src_y + c
        dst_y = d * src_x + e * src_y + f

    Solves the two independent 3×3 linear systems (one for each
    output coordinate) by Cramer's rule. Closed-form, no numpy,
    no degenerate-matrix-handling library — but does raise
    :class:`ValueError` if the source points are collinear (in
    which case there's no unique affine).

    The split-by-output-coord approach is justified because the
    two systems share the same source-point matrix: we compute
    its determinant once and reuse for both ``(a, b, c)`` and
    ``(d, e, f)`` solves.
    """
    (x0, y0), (x1, y1), (x2, y2) = src_pts
    (u0, v0), (u1, v1), (u2, v2) = dst_pts

    # Determinant of the source-point matrix
    #     [ x0  y0  1 ]
    #     [ x1  y1  1 ]
    #     [ x2  y2  1 ]
    # expanded along the last column (cofactors all ±1).
    det = (
        x0 * (y1 - y2)
        - y0 * (x1 - x2)
        + (x1 * y2 - x2 * y1)
    )
    if abs(det) < 1e-12:
        raise ValueError(
            "Cannot fit affine: source points are collinear "
            f"(determinant {det!r}). Pick three non-collinear points."
        )

    inv_det = 1.0 / det

    # Solve for (a, b, c) — the row that produces dst_x.
    a = (
        u0 * (y1 - y2)
        - y0 * (u1 - u2)
        + (u1 * y2 - u2 * y1)
    ) * inv_det
    b = (
        x0 * (u1 - u2)
        - u0 * (x1 - x2)
        + (x1 * u2 - x2 * u1)
    ) * inv_det
    c = (
        x0 * (y1 * u2 - y2 * u1)
        - y0 * (x1 * u2 - x2 * u1)
        + u0 * (x1 * y2 - x2 * y1)
    ) * inv_det

    # Solve for (d, e, f) — the row that produces dst_y.
    d = (
        v0 * (y1 - y2)
        - y0 * (v1 - v2)
        + (v1 * y2 - v2 * y1)
    ) * inv_det
    e = (
        x0 * (v1 - v2)
        - v0 * (x1 - x2)
        + (x1 * v2 - x2 * v1)
    ) * inv_det
    f = (
        x0 * (y1 * v2 - y2 * v1)
        - y0 * (x1 * v2 - x2 * v1)
        + v0 * (x1 * y2 - x2 * y1)
    ) * inv_det

    return a, b, c, d, e, f


def affine_apply(
    coeffs: tuple[float, float, float, float, float, float],
    x: float,
    y: float,
) -> tuple[float, float]:
    """Apply an affine ``(a, b, c, d, e, f)`` to a single point.

    Inline-able trivially but pulled out as a function so tests
    can hit it directly. No longer used by
    :func:`tile_to_chart_transform` (which uses the projective
    :func:`homography_apply` instead) but kept as a primitive for
    diagnostics that benchmark the legacy affine fit.
    """
    a, b, c, d, e, f = coeffs
    return (a * x + b * y + c, d * x + e * y + f)


def _solve_linear_system_8x8(
    matrix: list[list[float]],
    rhs: list[float],
) -> list[float]:
    """Solve an 8×8 linear system ``A x = b`` by Gaussian
    elimination with partial pivoting. Pure stdlib, no numpy.

    Dedicated to the 8×8 case used by :func:`fit_homography_4pt`:
    fast enough (~hundreds of ns per solve) and small enough not to
    need scipy. Raises :class:`ValueError` if the matrix is
    singular within ``1e-12`` — for the 4-corner homography case
    that only happens with collinear or coincident input points
    (degenerate tile geometry), which the caller treats the same as
    any other ``ValueError`` from a fit (skip the tile).

    Inputs are *mutated* in place — caller is expected to pass
    freshly-built rows. Augments the matrix with ``rhs`` as the
    9th column rather than allocating a separate vector, which
    keeps memory traffic predictable.
    """
    n = 8
    # Build augmented matrix [A | b]. Each row gets the rhs entry
    # appended; we then never reference ``rhs`` again.
    aug = [row + [rhs[i]] for i, row in enumerate(matrix)]
    for k in range(n):
        # Partial pivot: find row in [k, n) with largest |aug[r][k]|
        # and swap it into row k. Stabilises elimination when a
        # diagonal entry is small or zero.
        pivot_row = k
        pivot_val = abs(aug[k][k])
        for r in range(k + 1, n):
            v = abs(aug[r][k])
            if v > pivot_val:
                pivot_val = v
                pivot_row = r
        if pivot_val < 1e-12:
            raise ValueError(
                "Singular 8×8 linear system in homography solve "
                f"(pivot {pivot_val!r} at column {k}). The 4 source "
                "or 4 destination points are likely collinear or "
                "coincident."
            )
        if pivot_row != k:
            aug[k], aug[pivot_row] = aug[pivot_row], aug[k]
        # Eliminate column k below the pivot.
        pivot = aug[k][k]
        for r in range(k + 1, n):
            factor = aug[r][k] / pivot
            if factor == 0.0:
                continue
            row_r = aug[r]
            row_k = aug[k]
            for j in range(k, n + 1):
                row_r[j] -= factor * row_k[j]
    # Back-substitute. ``x[i] = (aug[i][n] - sum(aug[i][j]*x[j] for
    # j>i)) / aug[i][i]``.
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        total = aug[i][n]
        for j in range(i + 1, n):
            total -= aug[i][j] * x[j]
        x[i] = total / aug[i][i]
    return x


def fit_homography_4pt(
    src_pts: tuple[
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
    ],
    dst_pts: tuple[
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
    ],
) -> tuple[
    float, float, float,
    float, float, float,
    float, float, float,
]:
    """Fit the 8-DOF projective transform (homography) that maps
    each ``src_pts[i]`` exactly to ``dst_pts[i]``.

    Returns 9 coefficients in row-major matrix order ``(m11, m12,
    m13, m21, m22, m23, m31, m32, m33)`` matching Qt's
    :class:`QTransform` field naming. The result satisfies, for
    every input correspondence ``(x, y) → (X, Y)``::

        w'  = m13 * x + m23 * y + m33
        X   = (m11 * x + m21 * y + m31) / w'
        Y   = (m12 * x + m22 * y + m32) / w'

    ``m33`` is normalized to ``1.0`` in the returned tuple — the
    projective form is homogeneous (scaling all 9 coefficients
    yields the same transform) so we pick this normalization. The
    solver fixes the 8 remaining coefficients by the 8 equations
    arising from the 4 correspondences (2 equations each).

    Equation derivation: rearranging ``X = (m11*x + m21*y + m31) /
    w'`` to ``X * w' = m11*x + m21*y + m31`` and substituting
    ``w' = m13*x + m23*y + 1`` gives::

        m11*x + m21*y + m31 - X*m13*x - X*m23*y = X        ...(A)
        m12*x + m22*y + m32 - Y*m13*x - Y*m23*y = Y        ...(B)

    Each correspondence contributes one (A) row and one (B) row,
    for 8 rows in 8 unknowns ``[m11, m21, m31, m12, m22, m32, m13,
    m23]``. Solved in closed form (well, Gaussian elimination) by
    :func:`_solve_linear_system_8x8`.

    Notes
    -----

    *  When the 4 destination points form an affine image of the 4
       source points (i.e. ``dst`` is genuinely affine in ``src``,
       which happens when the chart calibration itself is purely
       affine and the world-Mercator → chart map is affine over
       the tile), the solver returns ``m13 = m23 = 0`` and ``m33
       = 1`` to within machine precision — i.e. it correctly
       degenerates to the pure-affine case.
    *  Raises :class:`ValueError` if the input points are collinear
       or coincident (singular 8×8 system); see
       :func:`_solve_linear_system_8x8`.
    """
    matrix: list[list[float]] = []
    rhs: list[float] = []
    for (sx, sy), (dx, dy) in zip(src_pts, dst_pts, strict=True):
        # Equation (A): coefficients of [m11, m21, m31, m12, m22,
        # m32, m13, m23] in the X equation, with rhs = X.
        matrix.append(
            [sx, sy, 1.0, 0.0, 0.0, 0.0, -dx * sx, -dx * sy]
        )
        rhs.append(dx)
        # Equation (B): same unknown ordering, Y equation, rhs = Y.
        matrix.append(
            [0.0, 0.0, 0.0, sx, sy, 1.0, -dy * sx, -dy * sy]
        )
        rhs.append(dy)
    solution = _solve_linear_system_8x8(matrix, rhs)
    m11, m21, m31, m12, m22, m32, m13, m23 = solution
    m33 = 1.0
    return (m11, m12, m13, m21, m22, m23, m31, m32, m33)


def homography_apply(
    coeffs: tuple[
        float, float, float,
        float, float, float,
        float, float, float,
    ],
    x: float,
    y: float,
) -> tuple[float, float]:
    """Apply a homography (9 coefficients in row-major order) to a
    single point.

    Computes ``w' = m13*x + m23*y + m33`` and divides through.
    Raises :class:`ValueError` if ``|w'| < 1e-12`` — the point sits
    on the projective transform's vanishing line and has no finite
    image. For tile-render geometry this can't happen with sane
    inputs (the chart pixmap is finite, so the homography sends
    finite tile-pixel-space points to finite scene-space points),
    but it's a useful safety check during development.
    """
    m11, m12, m13, m21, m22, m23, m31, m32, m33 = coeffs
    w = m13 * x + m23 * y + m33
    if abs(w) < 1e-12:
        raise ValueError(
            f"Homography apply: w-coordinate {w!r} ~ 0 at "
            f"({x}, {y}). Point is on the vanishing line."
        )
    sx = (m11 * x + m21 * y + m31) / w
    sy = (m12 * x + m22 * y + m32) / w
    return (sx, sy)


def tile_to_chart_transform(
    coord: TileCoord,
    calibration: "SheetGeoCalibration",
    pixmap_size: tuple[int, int],
) -> TileTransform:
    """Compute the 8-DOF projective transform that places a tile in
    chart-scene coords.

    The fit uses all 4 of the tile's corners (NW, NE, SE, SW) and
    measures the projection-quality residual at the held-out
    *tile center* — see module docstring for the rationale.

    Pre-conditions
    --------------
    * ``calibration`` must already be initialized (i.e.
      :meth:`SheetGeoCalibration._fit_curves` has run); a fresh
      calibration that hasn't seen any anchor points raises an
      :class:`AssertionError` from ``lonlat_to_uv``.
    * ``pixmap_size`` is the chart pixmap's ``(width, height)`` in
      pixels — the calibration's UV is normalized to ``[0, 1]²``,
      so multiplying by ``(width, height)`` gives the chart's
      pixel-space scene coords.

    Side effects
    ------------
    None. The function is pure — no caching, no I/O. The overlay
    manager calls this once per tile per session and caches the
    result itself.
    """
    width, height = pixmap_size
    if width <= 0 or height <= 0:
        raise ValueError(
            f"pixmap_size must be positive; got {pixmap_size!r}"
        )

    corners_lonlat = tile_corners_lonlat(coord)
    # Convert each lat/lon corner to chart-scene pixel coords:
    # calibration.lonlat_to_uv gives normalized UV ∈ [0, 1]², we
    # scale by the chart pixmap's pixel dimensions to get scene
    # px. The scene rect's origin is at chart-pixmap (0, 0), so
    # this gives the same coordinate system the chart pixmap item
    # itself uses — overlays placed in these coords stay aligned
    # with the chart through every Qt transform (zoom, pan, etc.).
    scene_corners: list[tuple[float, float]] = []
    for lon, lat in corners_lonlat:
        u, v = calibration.lonlat_to_uv(lon, lat)
        scene_corners.append((u * width, v * height))

    # Fit projective transform exactly through all 4 corners. The
    # homography has 8 DOF and the 4-corner-pair correspondences
    # provide 8 equations, so the fit is exact — every corner
    # lands at its true scene position and adjacent tiles share
    # their 2 common-edge corners by construction (no seam gap).
    coeffs = fit_homography_4pt(
        TILE_IMAGE_CORNERS_PX,
        (
            scene_corners[0],
            scene_corners[1],
            scene_corners[2],
            scene_corners[3],
        ),
    )

    # Probe projection quality at the tile center (held out from
    # the fit). The center is at tile-pixel (128, 128), which in
    # Web Mercator world-pixel space is ((tx + 0.5) * 256,
    # (ty + 0.5) * 256). Convert that back to (lon, lat), then
    # through the calibration, and compare to the homography's
    # prediction. Under LCC over a z=14 Israeli tile this residual
    # is ~0.005 chart-px; under an extreme-curvature chart (e.g.
    # 60° N) it stays under 1 px down to z=14 and the
    # MAX_TILE_RESIDUAL_PX guard rejects tiles where it doesn't.
    center_tile_px: tuple[float, float] = (
        float(TILE_SIZE_PX) * 0.5,
        float(TILE_SIZE_PX) * 0.5,
    )
    center_world_x = (coord.x + 0.5) * float(TILE_SIZE_PX)
    center_world_y = (coord.y + 0.5) * float(TILE_SIZE_PX)
    center_lon, center_lat = world_pixel_to_lonlat(
        center_world_x, center_world_y, coord.z
    )
    u_c, v_c = calibration.lonlat_to_uv(center_lon, center_lat)
    true_center = (u_c * width, v_c * height)
    proj_center = homography_apply(coeffs, *center_tile_px)
    residual_px = math.hypot(
        proj_center[0] - true_center[0],
        proj_center[1] - true_center[1],
    )

    m11, m12, m13, m21, m22, m23, m31, m32, m33 = coeffs
    return TileTransform(
        m11=m11, m12=m12, m13=m13,
        m21=m21, m22=m22, m23=m23,
        m31=m31, m32=m32, m33=m33,
        residual_px=residual_px,
    )


def enumerate_chart_tiles(
    calibration: "SheetGeoCalibration",
    pixmap_size: tuple[int, int],
    target_zoom: int,
) -> Iterator[TileCoord]:
    """Yield every tile whose lat/lon bbox intersects the chart's
    lat/lon bbox at ``target_zoom``.

    The chart's lat/lon bbox is computed by probing the 4 chart
    corners (UV = (0,0), (1,0), (1,1), (0,1)) through the
    calibration's inverse, then taking the axis-aligned bbox of
    those 4 points. Any extra tiles that fall inside that bbox
    but outside the chart's actual quadrilateral are harmless
    — their transformed scene-coords land outside the chart's
    scene rect and Qt's BSP clipping skips them at draw time.
    The cost is a modest tile-count over-estimate (a few % at
    chart scale) which we trade gladly for the simplicity of
    "build once, never re-enumerate".

    Yields
    ------
    :class:`TileCoord` per tile, in (z, x, y) iteration order
    inherited from :func:`bbox_to_tiles` (row-major over y, then
    x). The order isn't load-bearing — the overlay manager
    consumes the iterator into a dict keyed by coord.
    """
    width, height = pixmap_size
    if width <= 0 or height <= 0:
        raise ValueError(
            f"pixmap_size must be positive; got {pixmap_size!r}"
        )
    if target_zoom < 0:
        raise ValueError(f"target_zoom must be >= 0; got {target_zoom}")

    chart_corners_uv = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    lons: list[float] = []
    lats: list[float] = []
    for u, v in chart_corners_uv:
        lon, lat = calibration.uv_to_lonlat(u, v)
        lons.append(lon)
        lats.append(lat)

    yield from bbox_to_tiles(
        min_lat=min(lats),
        max_lat=max(lats),
        min_lon=min(lons),
        max_lon=max(lons),
        z=target_zoom,
    )
