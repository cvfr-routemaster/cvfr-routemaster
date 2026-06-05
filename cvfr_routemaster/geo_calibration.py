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

"""
Lat/lon <-> normalized chart coordinates (u, v in [0, 1]) via Lambert Conformal
Conic projection + least-squares 2D affine.

The pipeline is two stages:

    (lat, lon)  --[LCC projection]-->  (X, Y)  --[6-DoF affine]-->  (u, v)

**Stage 1 — LCC projection** (:func:`lcc_project`). Aviation VFR charts of
Israel are produced on the Lambert Conformal Conic projection per ICAO Annex
4 (ICAO 1:500k VFR / CVFR). LCC is *conformal* (preserves angles) but is
neither a simple linear function of (lat, lon) nor a cylindrical projection;
meridians converge toward the pole as a wedge of the cone, so the
east-west scale at lat 30 differs from lat 33 by ~3% across a single chart
sheet, and lines of constant longitude curve slightly toward the central
meridian. Using a planar (lon * cos(mean_lat), -lat) approximation instead
(which this codebase did until v2.5) leaves a ~15 chart-pixel structural
residual on north sheet click anchors that no per-sheet affine can absorb,
because the residual is the projection's curvature itself.

The empirical confirmation of LCC and the choice of parameters lives in
``scratch/diagnose_lcc_projection_fit.py``; a quick summary:

* Per-sheet click RMS drops from ~16 / ~8 px (planar) to ~2.5 / ~1.3 px
  (LCC + affine) -- an 85% reduction.
* Cross-sheet disagreement at the chart seam drops from ~8 px to ~5 px
  before joint LSQ trades click for seam; with the joint LSQ further
  absorbing the per-sheet residuals, the post-joint disagreement is
  expected to be ~1-2 px.

The LCC parameters (:data:`LCC_PHI_1_DEG`, :data:`LCC_PHI_2_DEG`,
:data:`LCC_LAMBDA_0_DEG`, :data:`LCC_PHI_0_DEG`) are fixed module-level
constants because ICAO mandates them per latitude band and they do not
change between chart editions or print runs. If a future chart load uses
a non-ICAO projection (e.g. Transverse Mercator like ITM), the constants
can be promoted to a per-chart parameter without touching call sites --
all callers use the public :meth:`SheetGeoCalibration.lonlat_to_uv` /
:meth:`SheetGeoCalibration.uv_to_lonlat` API.

**Stage 2 — 6-DoF affine** (:func:`_lsq_affine`). After LCC projection the
chart should be a true linear function of (X, Y) (modulo per-sheet print
and scan distortions: paper stretch, scan skew, slight chart rotation,
non-uniform scale between the two prints). The affine has six degrees of
freedom: full 2x2 matrix M plus translation t, so it absorbs rotation,
two independent scales, and shear -- exactly the distortions a print/scan
roundtrip can introduce. We require N >= 3 anchors; with N = 4 there are
8 equations and 6 unknowns, so 2 redundant equations average out click
noise.

**Axes orientation.** Image ``v`` increases **downward** (south on a north-up
chart) but :func:`lcc_project` returns ``Y`` increasing **northward** by
standard cartographic convention. The 6-DoF affine that follows absorbs
the sign flip together with all the other per-sheet distortions, so we
keep the LCC formula in its textbook form rather than embedding image-
axis assumptions at the projection layer.

Persistence includes a fingerprint of the source PDF so calibration is
discarded when the file changes. Only the click anchors are serialised --
the LCC pipeline is recomputed on load, so a re-launch picks up any
projection-constant changes automatically (no migration needed).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QPointF

CALIBRATION_FILE_VERSION = 1

# Max difference allowed between saved sheet position/scale and current layout (scene coords).
MAP_LAYOUT_POS_EPS = 0.5
MAP_LAYOUT_SCALE_EPS = 1e-4


def pdf_fingerprint(path: Path) -> dict[str, Any]:
    p = path.resolve()
    st = p.stat()
    return {"path": str(p), "mtime_ns": st.st_mtime_ns, "size": st.st_size}


def fingerprints_match(stored: dict[str, Any] | None, path: Path) -> bool:
    """Path-independent fingerprint check — same contract as the
    equivalent helpers in :mod:`cvfr_routemaster.altitude_cache`,
    :mod:`cvfr_routemaster.waypoint_cache`, and
    :mod:`cvfr_routemaster.map_image_cache`.

    The cached ``path`` field is still serialised to the calibration
    JSON for diagnostics ("which PDF was this calibration captured
    against?") but is intentionally **not** compared at load time, so
    a release zip that lands in a different absolute directory on
    the friend's machine — or on the user's own machine after they
    restructure into ``map-pdfs/`` — still hits the bundled
    calibration as long as the chart PDF's bytes (and therefore its
    size + the mtime preserved by ``shutil.copy2`` / zip-extract on
    Windows NTFS) survived intact.

    Without this guard, the v2 release would prompt the friend to
    re-calibrate north/south on first launch even though we shipped
    a perfectly valid ``geo_calibration.json`` — the same trap the
    other three caches already side-step.
    """
    if not stored or not path.is_file():
        return False
    cur = pdf_fingerprint(path)
    return (
        stored.get("mtime_ns") == cur["mtime_ns"]
        and stored.get("size") == cur["size"]
    )


def map_layout_matches(
    stored: dict[str, Any] | None,
    current: dict[str, float] | None,
    *,
    pos_eps: float = MAP_LAYOUT_POS_EPS,
    scale_eps: float = MAP_LAYOUT_SCALE_EPS,
) -> bool:
    """Whether the pixmap item layout (x, y, scale) matches saved calibration layout."""
    if stored is None or current is None:
        return False
    try:
        sx, sy = float(stored["x"]), float(stored["y"])
        ss = float(stored["scale"])
        cx, cy = float(current["x"]), float(current["y"])
        cs = float(current["scale"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        abs(sx - cx) <= pos_eps
        and abs(sy - cy) <= pos_eps
        and abs(ss - cs) <= scale_eps
    )


MIN_ANCHORS = 3


# ---------------------------------------------------------------------------
# Lambert Conformal Conic projection
# ---------------------------------------------------------------------------
#
# Standard parallels and central meridian for Israel's CVFR / ICAO 1:500k
# charts. ICAO Annex 4 mandates LCC for VFR charts at 30-80 deg latitude;
# the parallels should bracket the chart's lat extent (~29.5-33 N) so
# scale distortion is minimised across the chart. Verified empirically:
# residual per-anchor click RMS drops 5-6x when these parameters replace
# the legacy planar (lon * cos(mean_lat), -lat) approximation.
# See ``scratch/diagnose_lcc_projection_fit.py`` for the parameter sweep.
LCC_PHI_1_DEG = 29.0  # southern standard parallel
LCC_PHI_2_DEG = 33.0  # northern standard parallel
LCC_LAMBDA_0_DEG = 35.0  # central meridian
LCC_PHI_0_DEG = 31.0  # latitude of origin (midway between parallels)


def _compute_lcc_shape_constants(
    phi_1_deg: float, phi_2_deg: float
) -> tuple[float, float]:
    """LCC shape constants ``n`` (cone constant) and ``F`` (polar scale)
    that depend only on the two standard parallels.

    Snyder (1987) "Map Projections -- A Working Manual", USGS,
    eqs. (15-3) and (15-2). ``n`` controls how "cone-y" the projection is
    (``n = sin(phi)`` is the single-parallel degenerate case);
    ``F`` is the polar scale factor that re-appears in ``rho_0``.

    Numerically stable at any (phi_1, phi_2) inside (-pi/2, pi/2) with
    phi_1 != phi_2 -- our use only ever passes the fixed module constants
    so this is informational rather than load-bearing robustness.
    """
    phi_1 = math.radians(phi_1_deg)
    phi_2 = math.radians(phi_2_deg)
    cos1 = math.cos(phi_1)
    cos2 = math.cos(phi_2)
    if abs(phi_1_deg - phi_2_deg) < 1e-9:
        # Degenerate single-parallel case: n = sin(phi_1). The 2-parallel
        # log form below would divide by zero here.
        n = math.sin(phi_1)
    else:
        n = math.log(cos1 / cos2) / math.log(
            math.tan(math.pi / 4.0 + phi_2 / 2.0)
            / math.tan(math.pi / 4.0 + phi_1 / 2.0)
        )
    F = cos1 * math.pow(math.tan(math.pi / 4.0 + phi_1 / 2.0), n) / n
    return n, F


# Pre-computed at import time -- the parameters never change at runtime.
_LCC_N, _LCC_F = _compute_lcc_shape_constants(LCC_PHI_1_DEG, LCC_PHI_2_DEG)
_LCC_RHO_0 = _LCC_F / math.pow(
    math.tan(math.pi / 4.0 + math.radians(LCC_PHI_0_DEG) / 2.0), _LCC_N
)
_LCC_LAMBDA_0_RAD = math.radians(LCC_LAMBDA_0_DEG)


def lcc_project(lat_deg: float, lon_deg: float) -> tuple[float, float]:
    """Project geographic (lat, lon) to LCC plane coordinates (X, Y).

    Snyder eqs. (15-1) and (15-4), spherical form. Output is dimensionless
    (Earth-radius-normalised); the downstream :class:`SheetGeoCalibration`
    affine absorbs scale, false-easting/northing, and any rotation, so
    units don't matter -- only the *shape* of the LCC distortion does.

    Sign convention: ``X`` increases **eastward** and ``Y`` increases
    **northward** -- the standard cartographic convention (Snyder p. 105).
    The image-pixel convention is the opposite for ``v`` (downward =
    south), but we don't bother flipping ``Y`` here: the downstream
    6-DoF affine has more than enough freedom to absorb the sign,
    along with rotation, scale, shear, and translation. Forcing the
    LCC output into image-axis orientation would clutter the formula
    for no functional benefit.
    """
    phi = math.radians(lat_deg)
    # rho = F / tan(pi/4 + phi/2)^n  -- distance from cone apex.
    rho = _LCC_F / math.pow(math.tan(math.pi / 4.0 + phi / 2.0), _LCC_N)
    theta = _LCC_N * (math.radians(lon_deg) - _LCC_LAMBDA_0_RAD)
    X = rho * math.sin(theta)
    Y = _LCC_RHO_0 - rho * math.cos(theta)
    return X, Y


def lcc_unproject(x: float, y: float) -> tuple[float, float]:
    """Inverse LCC: (X, Y) -> (lat_deg, lon_deg).

    Snyder eqs. (15-5), (15-6), (15-8), (15-9), spherical form. Useful for
    the ``uv_to_lonlat`` direction (clicking a scene point to recover the
    underlying geographic coordinate).

    ``Y`` is measured **southward** to match :func:`lcc_project`; the
    re-derivation of ``rho`` uses ``rho_0 - Y`` accordingly.
    """
    yy = _LCC_RHO_0 - y
    # rho = sign(n) * sqrt(X^2 + (rho_0 - Y)^2) -- magnitude of the
    # radius vector in the conic plane. For positive n (northern
    # hemisphere) the sign is always +.
    rho = math.copysign(math.sqrt(x * x + yy * yy), _LCC_N)
    if rho == 0.0:
        # Exactly at the cone apex: pole. Return the latitude pole of
        # the appropriate hemisphere; longitude is indeterminate so use
        # the central meridian.
        return (math.copysign(90.0, _LCC_N), LCC_LAMBDA_0_DEG)
    theta = math.atan2(x, yy)
    lon = LCC_LAMBDA_0_DEG + math.degrees(theta / _LCC_N)
    # phi = 2 * atan((F / rho)^(1/n)) - pi/2.
    phi = 2.0 * math.atan(math.pow(_LCC_F / rho, 1.0 / _LCC_N)) - math.pi / 2.0
    return math.degrees(phi), lon


def _lsq_affine(
    src: list[tuple[float, float]],
    dst: list[tuple[float, float]],
) -> tuple[
    Callable[[float, float], tuple[float, float]],
    Callable[[float, float], tuple[float, float]],
]:
    """Closed-form least-squares 2D affine: ``M·[x, y]ᵀ + t ≈ [u, v]ᵀ``.

    M is a full 2×2 matrix ``[[a, b], [c, d]]``; t is ``(tx, ty)``. The two output rows
    decouple: minimise ``Σ (a·sx + b·sy − du)²`` for (a, b) and ``Σ (c·sx + d·sy − dv)²``
    for (c, d), with the shared Gram matrix ``[[Σsx², Σsxsy], [Σsxsy, Σsy²]]``. Translation
    falls out of the centroids: ``t = d_c − M·s_c``.

    Pure-Python (no NumPy dep). Requires N ≥ 3 (the Gram is rank-deficient with two
    collinear sources, which is always the case at N = 2).
    """
    n = len(src)
    if n != len(dst):
        raise ValueError("Source and destination point counts must match.")
    if n < MIN_ANCHORS:
        raise ValueError(f"Calibration needs at least {MIN_ANCHORS} anchor points.")

    cx_s = sum(p[0] for p in src) / n
    cy_s = sum(p[1] for p in src) / n
    cx_d = sum(p[0] for p in dst) / n
    cy_d = sum(p[1] for p in dst) / n

    s_xx = s_xy = s_yy = 0.0
    s_xu = s_yu = s_xv = s_yv = 0.0
    for (x, y), (u, v) in zip(src, dst):
        sx, sy = x - cx_s, y - cy_s
        du, dv = u - cx_d, v - cy_d
        s_xx += sx * sx
        s_xy += sx * sy
        s_yy += sy * sy
        s_xu += sx * du
        s_yu += sy * du
        s_xv += sx * dv
        s_yv += sy * dv

    det = s_xx * s_yy - s_xy * s_xy
    if abs(det) < 1e-18:
        raise ValueError("Calibration sources are collinear or coincident.")
    inv00 = s_yy / det
    inv01 = -s_xy / det
    inv11 = s_xx / det

    a = inv00 * s_xu + inv01 * s_yu
    b = inv01 * s_xu + inv11 * s_yu
    c = inv00 * s_xv + inv01 * s_yv
    d = inv01 * s_xv + inv11 * s_yv

    tx = cx_d - (a * cx_s + b * cy_s)
    ty = cy_d - (c * cx_s + d * cy_s)

    inv_det = a * d - b * c
    if abs(inv_det) < 1e-18:
        raise ValueError("Affine matrix is singular; cannot invert calibration.")
    ia = d / inv_det
    ib = -b / inv_det
    ic = -c / inv_det
    id_ = a / inv_det
    itx = -(ia * tx + ib * ty)
    ity = -(ic * tx + id_ * ty)

    def forward(x: float, y: float) -> tuple[float, float]:
        return (a * x + b * y + tx, c * x + d * y + ty)

    def inverse(u: float, v: float) -> tuple[float, float]:
        return (ia * u + ib * v + itx, ic * u + id_ * v + ity)

    return forward, inverse


@dataclass
class CalibrationPoint:
    code: str
    lat: float
    lon: float
    u: float  # 0..1 across pixmap width
    v: float  # 0..1 across pixmap height


@dataclass
class SheetGeoCalibration:
    """6-DoF affine LSQ fit between LCC-projected coords ``(X, Y)`` and
    normalised image coords ``(u, v)``.

    ``points`` holds N >= 3 :class:`CalibrationPoint` anchors expressed in
    geographic ``(lat, lon)``. Internally each anchor is run through
    :func:`lcc_project` before fitting the 6-DoF affine, so the affine
    only has to absorb per-sheet print/scan distortions (paper stretch,
    scan skew, rotation, non-uniform scale between the two prints) --
    *not* the projection's curvature, which used to leave a structural
    ~15 chart-pixel residual on the north sheet under the legacy
    ``(lon * cos(mean_lat), -lat)`` planar approximation.

    The default capture flow uses 4 anchors; see the module docstring
    for why a similarity is not enough on top of LCC (anisotropic per-
    sheet distortions like paper stretch can introduce shear that only
    a full 6-DoF affine can model).
    """

    pdf_fp: dict[str, Any]
    points: list[CalibrationPoint] = field(default_factory=list)
    map_layout: dict[str, float] | None = None  # x, y, scale of pixmap item when saved
    _forward: Callable[[float, float], tuple[float, float]] | None = None
    _inverse: Callable[[float, float], tuple[float, float]] | None = None
    # Legacy field: under the pre-LCC planar approximation this was
    # cos(mean anchor lat). Now LCC handles the cos-correction itself
    # (and more), so this is always 1.0 -- kept on the dataclass and on
    # JointCalibration.{north,south}_lon_scale for callsite backward
    # compatibility (e.g. ``apply_joint_affine_overrides``'s second arg).
    _lon_scale: float = 1.0
    _residual_uv: float = 0.0  # RMS residual at the anchors in UV units (informational)

    def __post_init__(self) -> None:
        if len(self.points) < MIN_ANCHORS:
            raise ValueError(f"Calibration needs at least {MIN_ANCHORS} anchor points.")
        codes = [p.code.strip().upper() for p in self.points]
        if len(set(codes)) != len(codes):
            raise ValueError("Calibration points must use distinct waypoint codes.")
        # LCC handles the projection curvature entirely; the 6-DoF
        # affine that follows only absorbs per-sheet print/scan
        # distortions. There's no more "mean anchor lat" tuning here.
        self._lon_scale = 1.0
        src = [lcc_project(p.lat, p.lon) for p in self.points]
        dst = [(p.u, p.v) for p in self.points]
        self._forward, self._inverse = _lsq_affine(src, dst)
        sq = 0.0
        for (sx, sy), (u, v) in zip(src, dst):
            fu, fv = self._forward(sx, sy)
            sq += (fu - u) ** 2 + (fv - v) ** 2
        self._residual_uv = math.sqrt(sq / len(self.points))

    def apply_joint_affine_overrides(
        self,
        coefficients: tuple[float, float, float, float, float, float],
        lon_scale: float = 1.0,
    ) -> None:
        """Replace the internally-fit affine with externally-supplied
        coefficients (e.g. the joint-LSQ result from
        :func:`compute_joint_calibration`).

        Why this exists: ``__post_init__`` fits an *independent* 6-DoF
        affine to this sheet's clicks alone, which is the right thing
        when the sheet is being used in isolation. When both sheets
        are calibrated, we re-solve the affines *jointly* with the
        layout so that the same lat/lon lands at the same scene
        position from either sheet's perspective at the shared
        overlap anchors. The joint solver produces 6-tuples we then
        push back into each :class:`SheetGeoCalibration` here so all
        the downstream code (``lonlat_to_uv``, ``uv_to_lonlat``,
        :func:`lonlat_to_scene`, marker / tile placement) picks up
        the joint result transparently.

        Args:
            coefficients: ``(a, b, c, d, tx, ty)`` such that
                ``u = a * X + b * Y + tx`` and ``v = c * X + d * Y + ty``
                with ``(X, Y)`` the LCC-projected coordinates of
                ``(lat, lon)`` per :func:`lcc_project`. Same
                parameterisation :class:`JointCalibration` uses.
            lon_scale: Legacy field, ignored. Kept in the signature so
                the existing call sites (``main_window.py``,
                ``test_geo_calibration.py``, the scratch scripts) that
                pass ``joint.north_lon_scale`` continue to work
                unchanged. Under LCC the projection no longer needs a
                cos-based anisotropy correction.

        The recorded ``_residual_uv`` is recomputed from the
        overridden affine, so it correctly reflects the *joint*-fit
        residual rather than the original independent-fit one.
        """
        del lon_scale  # legacy no-op; see docstring
        a, b, c, d, tx, ty = coefficients
        self._lon_scale = 1.0

        inv_det = a * d - b * c
        if abs(inv_det) < 1e-18:
            raise ValueError(
                "Joint affine matrix is singular; cannot invert calibration."
            )
        ia = d / inv_det
        ib = -b / inv_det
        ic = -c / inv_det
        id_ = a / inv_det
        itx = -(ia * tx + ib * ty)
        ity = -(ic * tx + id_ * ty)

        def _forward(x: float, y: float) -> tuple[float, float]:
            return (a * x + b * y + tx, c * x + d * y + ty)

        def _inverse(u: float, v: float) -> tuple[float, float]:
            return (ia * u + ib * v + itx, ic * u + id_ * v + ity)

        self._forward = _forward
        self._inverse = _inverse

        sq = 0.0
        for p in self.points:
            x, y = lcc_project(p.lat, p.lon)
            fu, fv = _forward(x, y)
            sq += (fu - p.u) ** 2 + (fv - p.v) ** 2
        self._residual_uv = math.sqrt(sq / len(self.points)) if self.points else 0.0

    @property
    def residual_uv(self) -> float:
        """RMS anchor residual in UV (0..1) units."""
        return self._residual_uv

    def lonlat_to_uv(self, lon: float, lat: float) -> tuple[float, float]:
        assert self._forward is not None
        x, y = lcc_project(lat, lon)
        return self._forward(x, y)

    def uv_to_lonlat(self, u: float, v: float) -> tuple[float, float]:
        assert self._inverse is not None
        xs, ys = self._inverse(u, v)
        lat_deg, lon_deg = lcc_unproject(xs, ys)
        # Historical contract: returns ``(lon, lat)``, NOT ``(lat, lon)``.
        # See call sites in satellite_overlay, satellite_overlay_math,
        # main_window, etc.
        return lon_deg, lat_deg

    def uv_to_lcc_xy(self, u: float, v: float) -> tuple[float, float]:
        """Inverse of the *affine* stage only: ``(u, v) -> (X_lcc, Y_lcc)``.

        Unlike :meth:`uv_to_lonlat` this stops *before* unprojecting LCC
        back to (lat, lon), so the relationship between ``(u, v)`` and
        ``(X_lcc, Y_lcc)`` is exactly affine (6 DoF). The renderer needs
        this for vectorised UV-grid warps: it computes ``(X, Y)`` over
        the grid in one cheap matrix operation, then runs vectorised
        LCC un-projection at the end. Going through
        :meth:`uv_to_lonlat` directly would lose the affine structure
        and force a per-pixel scalar projection.

        Three-point probes of this map are exact in the same way the
        legacy planar pipeline's three-point ``(u, v) -> (lon, lat)``
        probes were -- so the renderer's inverse-affine probe still
        works to floating-point precision, just one layer earlier in
        the pipeline.
        """
        assert self._inverse is not None
        return self._inverse(u, v)


def calibration_from_points(
    pdf_fp: dict[str, Any],
    *points: CalibrationPoint,
    map_layout: dict[str, float] | None = None,
) -> SheetGeoCalibration:
    """Build a SheetGeoCalibration from N ≥ 3 anchor points (4 in the live capture flow).

    Variadic for the call sites that build the list from a sequence comprehension.
    """
    if len(points) < MIN_ANCHORS:
        raise ValueError(f"Calibration needs at least {MIN_ANCHORS} anchor points.")
    codes = [p.code.strip().upper() for p in points]
    if len(set(codes)) != len(codes):
        raise ValueError("Calibration points must use distinct waypoint codes.")
    return SheetGeoCalibration(
        pdf_fp=pdf_fp, points=list(points), map_layout=map_layout
    )


def _point_to_dict(p: CalibrationPoint) -> dict[str, Any]:
    return {"code": p.code, "lat": p.lat, "lon": p.lon, "u": p.u, "v": p.v}


def _point_from_dict(d: dict[str, Any]) -> CalibrationPoint:
    return CalibrationPoint(
        code=str(d["code"]),
        lat=float(d["lat"]),
        lon=float(d["lon"]),
        u=float(d["u"]),
        v=float(d["v"]),
    )


def sheet_to_dict(cal: SheetGeoCalibration) -> dict[str, Any]:
    """Serialise a calibration to a JSON-compatible dict."""
    out: dict[str, Any] = {
        "pdf": cal.pdf_fp,
        "points": [_point_to_dict(p) for p in cal.points],
    }
    if cal.map_layout is not None:
        out["map_layout"] = dict(cal.map_layout)
    return out


def _map_layout_from_dict(obj: Any) -> dict[str, float] | None:
    if not isinstance(obj, dict):
        return None
    try:
        return {
            "x": float(obj["x"]),
            "y": float(obj["y"]),
            "scale": float(obj["scale"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def sheet_from_dict(d: dict[str, Any]) -> SheetGeoCalibration | None:
    """Read a calibration from JSON. Returns ``None`` for malformed/under-anchored data."""
    try:
        fp = d["pdf"]
        ml = _map_layout_from_dict(d.get("map_layout"))
        raw_points = d.get("points")
        if not isinstance(raw_points, list) or len(raw_points) < MIN_ANCHORS:
            return None
        points = [_point_from_dict(entry) for entry in raw_points]
        return SheetGeoCalibration(pdf_fp=fp, points=points, map_layout=ml)
    except (KeyError, TypeError, ValueError):
        return None


def lonlat_to_scene(
    pixmap_item,
    cal: SheetGeoCalibration,
    lon: float,
    lat: float,
) -> QPointF | None:
    """Map lon/lat to scene coordinates using pixmap item's rect (normalized u,v)."""
    try:
        u, v = cal.lonlat_to_uv(lon, lat)
    except (ValueError, ZeroDivisionError):
        return None
    br = pixmap_item.boundingRect()
    if br.width() <= 0 or br.height() <= 0:
        return None
    local = QPointF(u * br.width(), v * br.height())
    return pixmap_item.mapToScene(local)


def calibration_json_path(project_root: Path, mode_id: str | None = None) -> Path:
    d = project_root / ".cvfr_routemaster"
    if mode_id is not None:
        d = d / mode_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "geo_calibration.json"


def load_saved_calibration(
    project_root: Path, mode_id: str | None = None
) -> dict[str, Any]:
    path = calibration_json_path(project_root, mode_id)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_calibration_payload(
    project_root: Path, payload: dict[str, Any], mode_id: str | None = None
) -> None:
    path = calibration_json_path(project_root, mode_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_sheet_calibration_or_reason(
    raw: dict[str, Any],
    sheet_key: str,
    pdf_path: Path | None,
    current_map_layout: dict[str, float] | None,
    sheet_title: str,
) -> tuple[SheetGeoCalibration | None, str | None]:
    """
    Load calibration for one sheet, or return a short reason it cannot be used.

    ``current_map_layout`` must be ``{"x","y","scale"}`` for the pixmap item in scene space
    (required whenever charts are on screen); if it does not match the saved layout, calibration
    is rejected so lat/lon overlays stay aligned with the raster.
    """
    block = raw.get(sheet_key)
    if not isinstance(block, dict):
        return None, f"{sheet_title} chart is not calibrated yet."

    if pdf_path is None or not pdf_path.is_file():
        return None, f"{sheet_title} chart PDF is missing — check Settings."

    fp = block.get("pdf")
    if not isinstance(fp, dict) or not fingerprints_match(fp, pdf_path):
        return None, f"{sheet_title} chart PDF changed — calibrate again."

    cal = sheet_from_dict(block)
    if cal is None:
        return None, f"{sheet_title} chart calibration data is invalid."

    stored_layout = _map_layout_from_dict(block.get("map_layout"))
    if stored_layout is None:
        return None, (
            f"{sheet_title} chart needs calibration "
            "(saved data has no layout lock from when the sheet was placed)."
        )

    if current_map_layout is None:
        return None, f"{sheet_title} chart layout is not available yet."

    if not map_layout_matches(stored_layout, current_map_layout):
        return None, (
            f"{sheet_title} chart was moved or scaled since it was calibrated — calibrate again."
        )

    return cal, None


def try_load_sheet_calibration(
    raw: dict[str, Any],
    sheet_key: str,
    pdf_path: Path,
    *,
    current_map_layout: dict[str, float] | None = None,
) -> SheetGeoCalibration | None:
    """
    Loader for tests and tools. If ``current_map_layout`` is omitted, the saved ``map_layout``
    in JSON (when present) is used as the current layout so file-roundtrip checks stay self-consistent.
    """
    block = raw.get(sheet_key)
    layout = current_map_layout
    if layout is None and isinstance(block, dict):
        layout = _map_layout_from_dict(block.get("map_layout"))
    cal, _ = load_sheet_calibration_or_reason(
        raw, sheet_key, pdf_path, layout, "Sheet"
    )
    return cal


def build_payload(
    north: SheetGeoCalibration | None,
    south: SheetGeoCalibration | None,
) -> dict[str, Any]:
    return {
        "version": CALIBRATION_FILE_VERSION,
        "north": sheet_to_dict(north) if north else None,
        "south": sheet_to_dict(south) if south else None,
    }


# ---------------------------------------------------------------------------
# Cross-sheet alignment from overlap anchors
# ---------------------------------------------------------------------------

# Below this many shared overlap anchors the (scale, tx, ty) LSQ is either
# under-determined (1 anchor → 2 equations, 3 unknowns) or only critically
# determined with no error margin (1.5 anchors's worth). We require at least 2
# so the system has 4 ≥ 3 equations *and* one redundant equation that lets us
# report a meaningful residual.
MIN_OVERLAP_ALIGNMENT_ANCHORS = 2


@dataclass
class OverlapAlignment:
    """Result of fitting the south sheet's ``(scale, tx, ty)`` such that the
    user's clicks at shared overlap anchors land at the same scene position
    from both sheets, with the north sheet pinned at scale 1.0 / offset (0, 0).

    ``residual_px`` is the RMS scene-pixel residual at the shared anchors —
    i.e. how far the LSQ best-fit south layout still mis-aligns each anchor
    from where the same lat/lon lands on the (pinned) north sheet. With
    well-clicked anchors and no inter-sheet rotation or non-uniform scale,
    this should be a few px or less; large values flag click slop or a
    structural mismatch (e.g. a meaningfully rotated chart sheet) that a
    pure scale+translation cannot absorb.
    """

    scale: float
    tx: float
    ty: float
    residual_px: float
    shared_codes: tuple[str, ...]


def compute_overlap_aligned_layout(
    north_cal: SheetGeoCalibration,
    south_cal: SheetGeoCalibration,
    north_pixmap_size: tuple[float, float],
    south_pixmap_size: tuple[float, float],
) -> OverlapAlignment | None:
    """Solve the south sheet's layout (uniform scale + 2D translation) so the
    user's clicks at shared overlap anchors line up across the two sheets.

    Model: north is pinned at scale 1.0 / offset (0, 0). For each anchor whose
    ICAO code appears in both sheets' calibration point lists,

    .. math::

        u_n W_n &= s \\cdot u_s W_s + (1 - s) \\cdot W_s / 2 + t_x \\\\
        v_n H_n &= s \\cdot v_s H_s + (1 - s) \\cdot H_s / 2 + t_y

    where ``(W, H)`` are pixmap pixel dimensions and ``(u, v)`` are the
    user's saved normalised clicks.

    **Coordinate convention — important.** This codebase sets every chart
    pixmap item's ``transformOriginPoint`` to the pixmap centre (see
    ``main_window._prepare_map_sheet_item`` and ``scale_selected_layer``)
    so Alt-wheel scaling pivots around the visual centre rather than the
    top-left corner. Qt then computes scene position as

        ``scene = pos + scale · local + (1 − scale) · origin``

    with ``origin = (W/2, H/2)``. The extra ``(1 − s) · W/2`` term above is
    exactly that — a fixed contribution that vanishes at ``s = 1`` (north's
    pinned identity) and grows linearly with ``|1 − s|`` (~14 px per 0.4%
    scale change on a 7000-pixel-wide chart). Skipping it in the LSQ
    produced a consistent ~14 px westward shift of the south sheet at every
    overlap anchor — caught by the user noticing the seam looked aligned
    but the *whole* stitched chart had slid left after the first pass of
    auto-alignment.

    Linearising in ``(s, t_x, t_y)``, each anchor contributes two rows:

    * u-row: coefficients ``[(u_s − 0.5) · W_s, 1, 0]``, rhs ``u_n W_n − W_s/2``
    * v-row: coefficients ``[(v_s − 0.5) · H_s, 0, 1]``, rhs ``v_n H_n − H_s/2``

    Stacking across all shared anchors yields an overdetermined linear
    system; we solve via the 3×3 normal equations + Cramer's rule (no
    NumPy dep, matches :func:`_lsq_affine`).

    Returns ``None`` if fewer than :data:`MIN_OVERLAP_ALIGNMENT_ANCHORS`
    codes are shared, or if the system is degenerate.

    Caller responsibility: applying the result. We do not touch the
    :class:`SheetGeoCalibration` instances or any pixmap items; this is
    pure math so it can be unit-tested without Qt or the chart pipeline.
    """
    n_by_code = {p.code.strip().upper(): p for p in north_cal.points}
    s_by_code = {p.code.strip().upper(): p for p in south_cal.points}
    shared = sorted(set(n_by_code) & set(s_by_code))
    if len(shared) < MIN_OVERLAP_ALIGNMENT_ANCHORS:
        return None

    W_n, H_n = float(north_pixmap_size[0]), float(north_pixmap_size[1])
    W_s, H_s = float(south_pixmap_size[0]), float(south_pixmap_size[1])
    if W_n <= 0 or H_n <= 0 or W_s <= 0 or H_s <= 0:
        return None

    half_W_s = W_s * 0.5
    half_H_s = H_s * 0.5

    ata = [[0.0] * 3 for _ in range(3)]
    atb = [0.0, 0.0, 0.0]
    for code in shared:
        n_pt = n_by_code[code]
        s_pt = s_by_code[code]
        # u-row: scene_x_south = s · u_s W_s + (1-s) · W_s/2 + tx
        #                     = s · (u_s W_s − W_s/2) + W_s/2 + tx
        # match against north scene_x = u_n W_n  =>
        # s · (u_s W_s − W_s/2) + tx = u_n W_n − W_s/2
        a0, a1, a2 = s_pt.u * W_s - half_W_s, 1.0, 0.0
        b = n_pt.u * W_n - half_W_s
        ata[0][0] += a0 * a0
        ata[0][1] += a0 * a1
        ata[0][2] += a0 * a2
        ata[1][1] += a1 * a1
        ata[1][2] += a1 * a2
        ata[2][2] += a2 * a2
        atb[0] += a0 * b
        atb[1] += a1 * b
        atb[2] += a2 * b
        # v-row: same shape with H_s in place of W_s, and the ty coefficient
        # in column 2 instead of column 1.
        a0, a1, a2 = s_pt.v * H_s - half_H_s, 0.0, 1.0
        b = n_pt.v * H_n - half_H_s
        ata[0][0] += a0 * a0
        ata[0][1] += a0 * a1
        ata[0][2] += a0 * a2
        ata[1][1] += a1 * a1
        ata[1][2] += a1 * a2
        ata[2][2] += a2 * a2
        atb[0] += a0 * b
        atb[1] += a1 * b
        atb[2] += a2 * b
    ata[1][0] = ata[0][1]
    ata[2][0] = ata[0][2]
    ata[2][1] = ata[1][2]

    def _det3(m: list[list[float]]) -> float:
        return (
            m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
        )

    det = _det3(ata)
    if abs(det) < 1e-18:
        return None

    def _replaced(col: int) -> list[list[float]]:
        out = [row[:] for row in ata]
        for r in range(3):
            out[r][col] = atb[r]
        return out

    scale = _det3(_replaced(0)) / det
    tx = _det3(_replaced(1)) / det
    ty = _det3(_replaced(2)) / det

    if not math.isfinite(scale) or scale <= 0:
        return None

    sq_sum = 0.0
    for code in shared:
        n_pt = n_by_code[code]
        s_pt = s_by_code[code]
        nx = n_pt.u * W_n  # north's scene_x with pinned identity layout
        ny = n_pt.v * H_n
        # south's scene with center-origin convention
        sx = s_pt.u * W_s * scale + (1.0 - scale) * half_W_s + tx
        sy = s_pt.v * H_s * scale + (1.0 - scale) * half_H_s + ty
        sq_sum += (nx - sx) ** 2 + (ny - sy) ** 2
    residual_px = math.sqrt(sq_sum / len(shared))

    return OverlapAlignment(
        scale=scale,
        tx=tx,
        ty=ty,
        residual_px=residual_px,
        shared_codes=tuple(shared),
    )


# ──────────────────────────────────────────────────────────────────────
# Joint LSQ over (north_affine, south_affine, layout)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JointCalibration:
    """Result of jointly fitting north_affine, south_affine, and the south
    pixmap's ``(scale, tx, ty)`` layout via alternating LSQ.

    Replaces the previous two-step pipeline (per-sheet independent affines
    via :func:`_lsq_affine` plus a click-derived layout via
    :func:`compute_overlap_aligned_layout`) with a single objective that
    couples all three sets of parameters through the shared overlap
    anchors. Total parameter count: 6 + 6 + 3 = 15. Total observation
    equations (with ``N_n`` / ``N_s`` per-sheet anchor counts and
    ``N_shared`` shared anchors):

    * ``2 · N_n`` north-click residuals against north_affine
    * ``2 · N_s`` south-click residuals against south_affine
    * ``2 · N_shared`` cross-sheet *scene-position* residuals — i.e.
      ``u_n W_n − (s · u_s W_s + (1 − s) · W_s/2 + t_x_layout)`` at
      every shared anchor, and the analogous v-row.

    All residuals are measured in scene pixels, so the LSQ minimises
    one consistent unit across the whole system. Sub-problem-wise the
    fit is linear (each of the three blocks given the other two), so
    we solve via alternating least squares — three closed-form 3×3
    normal-equation solves per iteration, typically converging in <8
    iterations because the click-based independent fits make an
    excellent initial guess.

    What this buys you, compared to the old pipeline:

      * The chart-pixmap layout and the satellite-tile placement now
        agree on where lat/lon lands in scene at the shared anchors
        *by construction*. Away from the shared anchors the
        disagreement is bounded by the chart's residual non-affineness
        (Lambert Conformal Conic curvature over the chart extent),
        which is the irreducible floor unless we switch projection
        models.
      * Each per-sheet affine is *slightly* worse at fitting its own
        clicks than the independent fit was — the joint loss trades
        click-fidelity for cross-sheet consistency. In practice this
        is sub-pixel because click noise is ~1 px and the consistency
        was off by ~30 px, so the optimum heavily weights consistency.

    Attributes
    ----------
    north_affine, south_affine
        ``(a, b, c, d, tx, ty)`` mapping
        ``(X, Y) -> (u, v)`` where ``(X, Y) = lcc_project(lat, lon)``.
        Same shape as the independent ``SheetGeoCalibration._forward``
        would produce -- the joint fit just adjusts the coefficients
        to also satisfy the cross-sheet consistency rows.
    layout
        ``(scale, tx, ty)`` for the south pixmap item, with the north
        pinned at ``(0, 0)`` scale ``1.0``. Same convention as
        :class:`OverlapAlignment`.
    north_lon_scale, south_lon_scale
        Legacy fields. Always ``1.0`` under LCC because the projection
        already absorbs the cos(lat) anisotropy that the pre-LCC
        ``(lon * cos(mean_lat), -lat)`` planar approximation needed.
        Kept on the dataclass so the existing call sites that pass them
        into :meth:`SheetGeoCalibration.apply_joint_affine_overrides`
        continue to compile unchanged.
    shared_codes
        Codes that appear in both sheets' point lists, ordered for
        deterministic iteration.
    iterations, converged
        Diagnostic: how many alternating-LSQ iterations ran and whether
        the loop terminated by hitting the convergence threshold (rather
        than the ``max_iterations`` cap).
    click_residual_north_px, click_residual_south_px
        RMS pixel-space residual of each sheet's click anchors against
        its joint-fit affine. Compare to the independent-fit residuals
        to see how much per-sheet accuracy was traded for cross-sheet
        consistency.
    consistency_residual_px
        RMS scene-pixel disagreement at the shared anchors between
        north's *affine-predicted* scene position and the south's
        layout-transformed *affine-predicted* scene position. Drives
        the satellite-tile placement consistency across the seam.
    chart_residual_px
        RMS scene-pixel disagreement at the shared anchors between
        north's *click* scene position and the south's layout-
        transformed *click* scene position. Drives the chart-pixmap
        feature alignment across the seam (the user-visible chart-
        on-chart join error). Distinct from
        ``consistency_residual_px`` because clicks and affine
        predictions differ by the per-sheet click residual — both
        cannot be zero simultaneously in the presence of any
        Lambert-vs-affine projection misfit, so the layout LSQ
        balances them.
    """

    north_affine: tuple[float, float, float, float, float, float]
    south_affine: tuple[float, float, float, float, float, float]
    layout: tuple[float, float, float]
    north_lon_scale: float
    south_lon_scale: float
    shared_codes: tuple[str, ...]
    iterations: int
    converged: bool
    click_residual_north_px: float
    click_residual_south_px: float
    consistency_residual_px: float
    chart_residual_px: float


def _solve_3x3_normal_equations(
    ata: list[list[float]], atb: list[float]
) -> tuple[float, float, float] | None:
    """Solve the 3-variable normal-equations system ``AᵀA · x = Aᵀb`` via
    Cramer's rule. Used by both the per-affine sub-problems and the
    layout sub-problem of :func:`compute_joint_calibration`.

    Returns ``None`` when the matrix is singular (rank-deficient anchor
    geometry — e.g. all clicks collinear in source-plane coordinates).
    """
    det = (
        ata[0][0] * (ata[1][1] * ata[2][2] - ata[1][2] * ata[2][1])
        - ata[0][1] * (ata[1][0] * ata[2][2] - ata[1][2] * ata[2][0])
        + ata[0][2] * (ata[1][0] * ata[2][1] - ata[1][1] * ata[2][0])
    )
    if abs(det) < 1e-18:
        return None

    def _det3(m: list[list[float]]) -> float:
        return (
            m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
        )

    def _replaced(col: int) -> list[list[float]]:
        out = [row[:] for row in ata]
        for r in range(3):
            out[r][col] = atb[r]
        return out

    return (
        _det3(_replaced(0)) / det,
        _det3(_replaced(1)) / det,
        _det3(_replaced(2)) / det,
    )


def _lsq_from_rows(
    rows: list[tuple[tuple[float, float, float], float]],
) -> tuple[float, float, float] | None:
    """Accumulate ``AᵀA`` and ``Aᵀb`` from ``[(coeffs, rhs), ...]`` and
    solve the 3-variable LSQ. Each input row's residual contributes
    ``(coeffs · x - rhs)²`` to the loss — i.e. all rows are weighted
    equally. Pre-weight the inputs yourself to control relative weights.
    """
    ata = [[0.0] * 3 for _ in range(3)]
    atb = [0.0, 0.0, 0.0]
    for (c0, c1, c2), rhs in rows:
        atb[0] += c0 * rhs
        atb[1] += c1 * rhs
        atb[2] += c2 * rhs
        ata[0][0] += c0 * c0
        ata[0][1] += c0 * c1
        ata[0][2] += c0 * c2
        ata[1][1] += c1 * c1
        ata[1][2] += c1 * c2
        ata[2][2] += c2 * c2
    ata[1][0] = ata[0][1]
    ata[2][0] = ata[0][2]
    ata[2][1] = ata[1][2]
    return _solve_3x3_normal_equations(ata, atb)


def _evaluate_affine(
    affine: tuple[float, float, float, float, float, float],
    x: float,
    y: float,
) -> tuple[float, float]:
    """Apply a 6-tuple affine ``(a, b, c, d, tx, ty)`` to ``(x, y)``."""
    a, b, c, d, tx, ty = affine
    return a * x + b * y + tx, c * x + d * y + ty


def compute_joint_calibration(
    north_points: list[CalibrationPoint],
    south_points: list[CalibrationPoint],
    north_pixmap_size: tuple[float, float],
    south_pixmap_size: tuple[float, float],
    *,
    max_iterations: int = 64,
    convergence_loss_px: float = 1e-4,
) -> JointCalibration | None:
    """Jointly fit ``(north_affine, south_affine, layout)`` so that all
    three sets of parameters are simultaneously LSQ-optimal under the
    coupling

    .. math::

        u_n W_n &= s \\cdot u_s W_s + (1 - s) \\cdot W_s / 2 + t_x \\\\
        v_n H_n &= s \\cdot v_s H_s + (1 - s) \\cdot H_s / 2 + t_y

    at every shared overlap anchor. See :class:`JointCalibration` for
    the motivation and the residual decomposition the optimiser is
    minimising.

    Why alternating LSQ (rather than full Gauss-Newton or a global
    non-linear solver):

      * Each block (north_affine, south_affine, layout) is *linear* in
        its own parameters given the other two fixed, so each
        sub-problem is a closed-form 3×3 normal-equation solve. No
        Jacobian assembly, no step-size search, pure linear algebra
        per iteration.
      * The independent fits make an excellent warm start (we're
        usually within a few percent of the joint optimum from the
        start), so convergence is fast and reliable for the regime we
        care about (s ≈ 1, click noise ≈ 1 px). For our 7-anchor
        configurations the loop typically terminates in 3–6 iterations.
      * No NumPy / SciPy dependency — fits the rest of the calibration
        pipeline's pure-Python style.

    The loop monitors the *pixel-space loss* across all residuals and
    stops when one iteration's improvement falls below
    ``convergence_loss_px``. The minimum viable shared-anchor count
    matches :data:`MIN_OVERLAP_ALIGNMENT_ANCHORS` — with 2 anchors the
    layout is exactly determined (3 unknowns vs 4 equations after the
    layout sub-problem's scale-symmetry); 3 gives a redundancy and
    sub-pixel residuals at the anchors.

    Returns ``None`` when there's no joint optimum — too few anchors,
    too few shared anchors, degenerate source geometry (collinear),
    non-positive pixmap dimensions, or the iteration diverges
    (defensive: in practice this doesn't happen for the inputs the app
    actually produces, but the math is non-convex in ``s · α_s`` so
    we guard against it).
    """
    if (
        len(north_points) < MIN_ANCHORS
        or len(south_points) < MIN_ANCHORS
    ):
        return None
    n_by_code = {p.code.strip().upper(): p for p in north_points}
    s_by_code = {p.code.strip().upper(): p for p in south_points}
    shared = sorted(set(n_by_code) & set(s_by_code))
    if len(shared) < MIN_OVERLAP_ALIGNMENT_ANCHORS:
        return None

    W_n, H_n = float(north_pixmap_size[0]), float(north_pixmap_size[1])
    W_s, H_s = float(south_pixmap_size[0]), float(south_pixmap_size[1])
    if W_n <= 0 or H_n <= 0 or W_s <= 0 or H_s <= 0:
        return None
    half_W_s = W_s * 0.5
    half_H_s = H_s * 0.5

    # Under LCC, both sheets project through *the same* projection
    # parameters, so the two sheets' affines are directly comparable
    # in the (X, Y) plane. The legacy ``cos(mean_lat)`` per-sheet
    # anisotropy correction was a small but meaningful contributor to
    # seam disagreement (north's mean lat ~32.1, south's ~30.8, so the
    # two sheets used slightly different X-axis scales -- they
    # *cancelled* at the seam under the joint LSQ trade-off but only
    # imperfectly). LCC eliminates the issue at the projection layer.
    cos_lat_n = 1.0
    cos_lat_s = 1.0

    def _src_n(p: CalibrationPoint) -> tuple[float, float]:
        return lcc_project(p.lat, p.lon)

    def _src_s(p: CalibrationPoint) -> tuple[float, float]:
        return lcc_project(p.lat, p.lon)

    # ── Initial guess: independent per-sheet affines + click-based layout.
    try:
        # Use the same closed-form solver the rest of the module uses, then
        # extract the coefficient 6-tuple from its (forward, inverse) pair.
        n_src = [_src_n(p) for p in north_points]
        n_dst = [(p.u, p.v) for p in north_points]
        s_src = [_src_s(p) for p in south_points]
        s_dst = [(p.u, p.v) for p in south_points]
        n_fwd, _ = _lsq_affine(n_src, n_dst)
        s_fwd, _ = _lsq_affine(s_src, s_dst)
    except (ValueError, ZeroDivisionError):
        return None

    # _lsq_affine returns closures; reverse-engineer the coefficients by
    # probing three points. Cheaper than re-deriving and keeps a single
    # source of truth for the closed-form solver.
    def _extract_6tuple(
        forward: Callable[[float, float], tuple[float, float]],
    ) -> tuple[float, float, float, float, float, float]:
        u0, v0 = forward(0.0, 0.0)
        u1, v1 = forward(1.0, 0.0)
        u2, v2 = forward(0.0, 1.0)
        return u1 - u0, u2 - u0, v1 - v0, v2 - v0, u0, v0

    a_n, b_n, c_n, d_n, tx_n_aff, ty_n_aff = _extract_6tuple(n_fwd)
    a_s, b_s, c_s, d_s, tx_s_aff, ty_s_aff = _extract_6tuple(s_fwd)

    # Initial layout from the click-based fit so the loop starts close
    # to the optimum. The function we're already shipping does exactly
    # this; reuse it.
    initial_alignment = compute_overlap_aligned_layout(
        SheetGeoCalibration(
            pdf_fp={}, points=list(north_points), map_layout=None
        ),
        SheetGeoCalibration(
            pdf_fp={}, points=list(south_points), map_layout=None
        ),
        (W_n, H_n),
        (W_s, H_s),
    )
    if initial_alignment is None:
        return None
    scale = initial_alignment.scale
    tx_layout = initial_alignment.tx
    ty_layout = initial_alignment.ty

    def _total_loss() -> float:
        loss = 0.0
        n_aff = (a_n, b_n, c_n, d_n, tx_n_aff, ty_n_aff)
        s_aff = (a_s, b_s, c_s, d_s, tx_s_aff, ty_s_aff)
        for p in north_points:
            x, y = _src_n(p)
            u_pred, v_pred = _evaluate_affine(n_aff, x, y)
            loss += (W_n * (u_pred - p.u)) ** 2
            loss += (H_n * (v_pred - p.v)) ** 2
        for p in south_points:
            x, y = _src_s(p)
            u_pred, v_pred = _evaluate_affine(s_aff, x, y)
            loss += (W_s * (u_pred - p.u)) ** 2
            loss += (H_s * (v_pred - p.v)) ** 2
        for code in shared:
            n_pt = n_by_code[code]
            s_pt = s_by_code[code]
            u_n_pred, v_n_pred = _evaluate_affine(n_aff, *_src_n(n_pt))
            u_s_pred, v_s_pred = _evaluate_affine(s_aff, *_src_s(s_pt))
            # Affine-derived consistency (sat-stitch).
            scene_n_x_aff = u_n_pred * W_n
            scene_n_y_aff = v_n_pred * H_n
            scene_s_x_aff = (
                scale * u_s_pred * W_s + (1.0 - scale) * half_W_s + tx_layout
            )
            scene_s_y_aff = (
                scale * v_s_pred * H_s + (1.0 - scale) * half_H_s + ty_layout
            )
            loss += (scene_n_x_aff - scene_s_x_aff) ** 2
            loss += (scene_n_y_aff - scene_s_y_aff) ** 2
            # Click-derived chart_diff (chart-on-chart). The layout
            # sub-problem fits *both* sets of rows, so the convergence
            # criterion has to track both.
            scene_n_x_click = n_pt.u * W_n
            scene_n_y_click = n_pt.v * H_n
            scene_s_x_click = (
                scale * s_pt.u * W_s + (1.0 - scale) * half_W_s + tx_layout
            )
            scene_s_y_click = (
                scale * s_pt.v * H_s + (1.0 - scale) * half_H_s + ty_layout
            )
            loss += (scene_n_x_click - scene_s_x_click) ** 2
            loss += (scene_n_y_click - scene_s_y_click) ** 2
        return loss

    prev_loss = _total_loss()
    converged = False
    completed_iterations = 0
    for iteration in range(max_iterations):
        completed_iterations = iteration + 1

        # ── Sub-problem 1: Update α_n with α_s, layout fixed.
        # Variables (in pixel-scaled form): A_n = a_n · W_n, B_n = b_n · W_n,
        # Tx_n_px = tx_n · W_n. Click row at anchor i contributes
        # coeffs = (X_n_i, Y_n_i, 1), rhs = W_n · u_click_i — residual is
        # already pixel-space because A_n · X + B_n · Y + Tx_n_px equals
        # u_n_pred · W_n. Shared-anchor consistency row at code k:
        # rhs = s · u_s_pred · W_s + (1 − s) · W_s/2 + t_x_layout (i.e. the
        # scene_x the south side predicts). Same coefficient shape.
        u_rows_n: list[tuple[tuple[float, float, float], float]] = []
        v_rows_n: list[tuple[tuple[float, float, float], float]] = []
        for p in north_points:
            x, y = _src_n(p)
            u_rows_n.append(((x, y, 1.0), W_n * p.u))
            v_rows_n.append(((x, y, 1.0), H_n * p.v))
        for code in shared:
            n_pt = n_by_code[code]
            s_pt = s_by_code[code]
            x_n, y_n = _src_n(n_pt)
            u_s_pred, v_s_pred = _evaluate_affine(
                (a_s, b_s, c_s, d_s, tx_s_aff, ty_s_aff),
                *_src_s(s_pt),
            )
            target_u = scale * u_s_pred * W_s + (1.0 - scale) * half_W_s + tx_layout
            target_v = scale * v_s_pred * H_s + (1.0 - scale) * half_H_s + ty_layout
            u_rows_n.append(((x_n, y_n, 1.0), target_u))
            v_rows_n.append(((x_n, y_n, 1.0), target_v))
        sol_u_n = _lsq_from_rows(u_rows_n)
        sol_v_n = _lsq_from_rows(v_rows_n)
        if sol_u_n is None or sol_v_n is None:
            return None
        A_n_px, B_n_px, Tx_n_px = sol_u_n
        C_n_px, D_n_px, Ty_n_px = sol_v_n
        a_n, b_n, tx_n_aff = A_n_px / W_n, B_n_px / W_n, Tx_n_px / W_n
        c_n, d_n, ty_n_aff = C_n_px / H_n, D_n_px / H_n, Ty_n_px / H_n

        # ── Sub-problem 2: Update α_s with α_n, layout fixed.
        # Variables (pixel-scaled): A_s = a_s · W_s, B_s = b_s · W_s,
        # Tx_s_px = tx_s · W_s. Click rows: coeffs = (X_s, Y_s, 1),
        # rhs = W_s · u_click. Consistency rows: rearranging
        # ``u_n_pred · W_n = s · u_s_pred · W_s + (1 − s) · W_s/2 + t_x``
        # for u_s_pred · W_s gives the rhs, with row coeffs scaled by
        # ``s`` so the residual stays in pixel-space (otherwise the
        # consistency row is implicitly weighted by 1/s relative to
        # clicks). For s ≈ 1 this is a small effect but we do it
        # correctly to keep the gradient flow clean.
        u_rows_s: list[tuple[tuple[float, float, float], float]] = []
        v_rows_s: list[tuple[tuple[float, float, float], float]] = []
        for p in south_points:
            x, y = _src_s(p)
            u_rows_s.append(((x, y, 1.0), W_s * p.u))
            v_rows_s.append(((x, y, 1.0), H_s * p.v))
        for code in shared:
            n_pt = n_by_code[code]
            s_pt = s_by_code[code]
            x_s, y_s = _src_s(s_pt)
            u_n_pred, v_n_pred = _evaluate_affine(
                (a_n, b_n, c_n, d_n, tx_n_aff, ty_n_aff),
                *_src_n(n_pt),
            )
            target_u_pixel = u_n_pred * W_n - (1.0 - scale) * half_W_s - tx_layout
            target_v_pixel = v_n_pred * H_n - (1.0 - scale) * half_H_s - ty_layout
            u_rows_s.append(((scale * x_s, scale * y_s, scale), target_u_pixel))
            v_rows_s.append(((scale * x_s, scale * y_s, scale), target_v_pixel))
        sol_u_s = _lsq_from_rows(u_rows_s)
        sol_v_s = _lsq_from_rows(v_rows_s)
        if sol_u_s is None or sol_v_s is None:
            return None
        A_s_px, B_s_px, Tx_s_px = sol_u_s
        C_s_px, D_s_px, Ty_s_px = sol_v_s
        a_s, b_s, tx_s_aff = A_s_px / W_s, B_s_px / W_s, Tx_s_px / W_s
        c_s, d_s, ty_s_aff = C_s_px / H_s, D_s_px / H_s, Ty_s_px / H_s

        # ── Sub-problem 3: Update layout (s, tx_layout, ty_layout) with
        # both affines fixed. We jointly LSQ TWO sets of rows:
        #
        #   * AFFINE-consistency rows: scene_n_affine_pred = scene_s_affine_pred
        #     after layout. Minimising these drives the satellite-tile placement
        #     (which uses the affines) to agree across the seam.
        #
        #   * CLICK-chart_diff rows: scene_n_click = scene_s_click after
        #     layout. Minimising these drives the chart-pixmap content
        #     to agree across the seam (this is what
        #     compute_overlap_aligned_layout fits exclusively).
        #
        # The two sets disagree by the per-sheet click residual of the
        # affine fit — they cannot both be zero unless the affines fit
        # the clicks exactly, which the Lambert-vs-affine projection
        # misfit precludes. Fitting only the affine rows (which we did
        # in an earlier draft) drove sat-stitch to ~5 px but blew up
        # chart-on-chart to ~14 px, because the layout shifted to
        # match the affine *predictions* rather than the clicks the
        # chart pixmap actually contains. Fitting only the click rows
        # (the legacy compute_overlap_aligned_layout) is the inverse
        # trade — chart-on-chart ≈ 3 px, sat-stitch ≈ 30 px at edges.
        # Including both with equal weighting lands the layout at the
        # LSQ midpoint: each metric ends up at roughly half the click
        # residual (~6 px each in our 7-anchor configuration), which
        # is the smallest *combined* visible step at the seam.
        layout_rows: list[tuple[tuple[float, float, float], float]] = []
        for code in shared:
            n_pt = n_by_code[code]
            s_pt = s_by_code[code]
            u_n_pred, v_n_pred = _evaluate_affine(
                (a_n, b_n, c_n, d_n, tx_n_aff, ty_n_aff), *_src_n(n_pt)
            )
            u_s_pred, v_s_pred = _evaluate_affine(
                (a_s, b_s, c_s, d_s, tx_s_aff, ty_s_aff), *_src_s(s_pt)
            )
            # Affine-derived consistency rows (sat-stitch).
            layout_rows.append(
                (
                    (u_s_pred * W_s - half_W_s, 1.0, 0.0),
                    u_n_pred * W_n - half_W_s,
                )
            )
            layout_rows.append(
                (
                    (v_s_pred * H_s - half_H_s, 0.0, 1.0),
                    v_n_pred * H_n - half_H_s,
                )
            )
            # Click-derived chart_diff rows (chart-on-chart).
            layout_rows.append(
                (
                    (s_pt.u * W_s - half_W_s, 1.0, 0.0),
                    n_pt.u * W_n - half_W_s,
                )
            )
            layout_rows.append(
                (
                    (s_pt.v * H_s - half_H_s, 0.0, 1.0),
                    n_pt.v * H_n - half_H_s,
                )
            )
        sol_layout = _lsq_from_rows(layout_rows)
        if sol_layout is None:
            return None
        scale, tx_layout, ty_layout = sol_layout
        if not math.isfinite(scale) or scale <= 0:
            return None

        loss = _total_loss()
        if not math.isfinite(loss):
            return None
        if prev_loss - loss < convergence_loss_px and iteration > 0:
            converged = True
            break
        prev_loss = loss

    # ── Final residual diagnostics. The per-sheet click RMS is in scene
    # pixels (multiplied by W or H so u-residual and v-residual contribute
    # comparable units). consistency_residual_px is the scene-pixel RMS
    # of cross-sheet disagreement at the shared anchors *after* the
    # joint fit — the headline number for "did the joint fit make the
    # sat stitch consistent".
    def _click_rms_px(
        points: list[CalibrationPoint],
        affine: tuple[float, float, float, float, float, float],
        cos_lat: float,
        W: float,
        H: float,
    ) -> float:
        del cos_lat  # legacy parameter; LCC handles projection internally
        sq = 0.0
        for p in points:
            x, y = lcc_project(p.lat, p.lon)
            u_pred, v_pred = _evaluate_affine(affine, x, y)
            sq += (W * (u_pred - p.u)) ** 2 + (H * (v_pred - p.v)) ** 2
        return math.sqrt(sq / len(points)) if points else 0.0

    n_aff_final = (a_n, b_n, c_n, d_n, tx_n_aff, ty_n_aff)
    s_aff_final = (a_s, b_s, c_s, d_s, tx_s_aff, ty_s_aff)
    consistency_sq = 0.0
    chart_sq = 0.0
    for code in shared:
        n_pt = n_by_code[code]
        s_pt = s_by_code[code]
        u_n_pred, v_n_pred = _evaluate_affine(n_aff_final, *_src_n(n_pt))
        u_s_pred, v_s_pred = _evaluate_affine(s_aff_final, *_src_s(s_pt))
        scene_n_x = u_n_pred * W_n
        scene_n_y = v_n_pred * H_n
        scene_s_x = scale * u_s_pred * W_s + (1.0 - scale) * half_W_s + tx_layout
        scene_s_y = scale * v_s_pred * H_s + (1.0 - scale) * half_H_s + ty_layout
        consistency_sq += (scene_n_x - scene_s_x) ** 2 + (
            scene_n_y - scene_s_y
        ) ** 2
        scene_n_x_click = n_pt.u * W_n
        scene_n_y_click = n_pt.v * H_n
        scene_s_x_click = scale * s_pt.u * W_s + (1.0 - scale) * half_W_s + tx_layout
        scene_s_y_click = scale * s_pt.v * H_s + (1.0 - scale) * half_H_s + ty_layout
        chart_sq += (scene_n_x_click - scene_s_x_click) ** 2 + (
            scene_n_y_click - scene_s_y_click
        ) ** 2

    return JointCalibration(
        north_affine=n_aff_final,
        south_affine=s_aff_final,
        layout=(scale, tx_layout, ty_layout),
        north_lon_scale=cos_lat_n,
        south_lon_scale=cos_lat_s,
        shared_codes=tuple(shared),
        iterations=completed_iterations,
        converged=converged,
        click_residual_north_px=_click_rms_px(
            north_points, n_aff_final, cos_lat_n, W_n, H_n
        ),
        click_residual_south_px=_click_rms_px(
            south_points, s_aff_final, cos_lat_s, W_s, H_s
        ),
        consistency_residual_px=math.sqrt(consistency_sq / len(shared)),
        chart_residual_px=math.sqrt(chart_sq / len(shared)),
    )
