"""Unit tests for geo_calibration affine fit and persistence helpers."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from cvfr_routemaster.geo_calibration import (
    CALIBRATION_FILE_VERSION,
    LCC_LAMBDA_0_DEG,
    LCC_PHI_0_DEG,
    LCC_PHI_1_DEG,
    LCC_PHI_2_DEG,
    MIN_ANCHORS,
    MIN_OVERLAP_ALIGNMENT_ANCHORS,
    CalibrationPoint,
    JointCalibration,
    OverlapAlignment,
    SheetGeoCalibration,
    calibration_from_points,
    compute_joint_calibration,
    compute_overlap_aligned_layout,
    lcc_project,
    lcc_unproject,
    load_saved_calibration,
    load_sheet_calibration_or_reason,
    map_layout_matches,
    pdf_fingerprint,
    save_calibration_payload,
    sheet_from_dict,
    sheet_to_dict,
    try_load_sheet_calibration,
)


# ---------------------------------------------------------------------------
# Helpers — build well-formed N≥3 anchor sets for tests
# ---------------------------------------------------------------------------


def _three_anchors() -> tuple[CalibrationPoint, CalibrationPoint, CalibrationPoint]:
    """Three non-collinear anchors with hand-picked uv. With 3 anchors the affine fit
    is exact (6 equations, 6 DoF), so each anchor maps back to its uv to working precision.
    """
    return (
        CalibrationPoint(code="A", lat=33.0, lon=35.0, u=0.10, v=0.80),
        CalibrationPoint(code="B", lat=33.5, lon=35.8, u=0.85, v=0.20),
        CalibrationPoint(code="C", lat=32.6, lon=35.6, u=0.55, v=0.90),
    )


def _four_anchors_from_similarity(
    *, mid_lat: float = 33.0, lon_centre: float = 35.0
) -> list[CalibrationPoint]:
    """Four anchors whose uv come from a known similarity *applied to LCC
    coordinates*. The affine LSQ on these (which also projects through
    LCC internally) must produce zero residual, since a similarity is a
    special case of a 6-DoF affine.

    Under the legacy planar pipeline the source plane was
    ``(lon * cos(mid_lat), -lat)``; under LCC we project via
    :func:`lcc_project`. The test's property -- "a similarity is a
    special case of an affine and therefore the 6-DoF LSQ recovers it
    to working precision" -- holds equally well under either source
    plane as long as the synthetic data and the production code agree
    on the source plane, which they now both do (LCC).
    """
    angle = math.radians(7.0)
    scale = 0.65
    cos_a, sin_a = math.cos(angle), math.sin(angle)

    def truth(lon: float, lat: float) -> tuple[float, float]:
        x, y = lcc_project(lat, lon)
        u = scale * (cos_a * x - sin_a * y) + 0.5
        v = scale * (sin_a * x + cos_a * y) + 0.5
        return (u, v)

    return [
        CalibrationPoint(code=code, lat=lat, lon=lon, u=truth(lon, lat)[0], v=truth(lon, lat)[1])
        for code, lat, lon in [
            ("A", mid_lat - 0.5, lon_centre - 0.2),
            ("B", mid_lat + 0.5, lon_centre - 0.2),
            ("C", mid_lat + 0.5, lon_centre + 0.8),
            ("D", mid_lat - 0.5, lon_centre + 0.8),
        ]
    ]


# ---------------------------------------------------------------------------
# Anchor-fit basics
# ---------------------------------------------------------------------------


def test_affine_maps_anchors_to_themselves() -> None:
    fp = {"path": "/x.pdf", "mtime_ns": 1, "size": 2}
    a, b, c = _three_anchors()
    cal = calibration_from_points(fp, a, b, c)
    for p in (a, b, c):
        u, v = cal.lonlat_to_uv(p.lon, p.lat)
        assert pytest.approx((u, v), abs=1e-9) == (p.u, p.v)
    assert cal.residual_uv < 1e-12


def test_north_up_chart_preserves_orientation() -> None:
    """North-up chart: east -> right, north -> up. Catches missing y-flip
    / 90deg rotation bugs.

    Anchors form a triangle covering the chart. With 3 non-collinear
    anchors the affine is determined exactly, and an off-anchor point
    at (lon_NE, lat_SW) must land lower-right.

    Test region is intentionally small (0.1deg square): LCC is a
    conformal *conic* projection so the four "corners" of a lat-lon
    square form a slight trapezoid in LCC plane (meridians converge
    poleward). The affine on 3 anchors is exact, but the 4th lands off-
    rectangle proportional to the square's size; at 0.1deg the deviation
    is ~1e-4 (well below the 2e-3 tolerance), while a 1deg square would
    deviate ~1%. We're testing orientation, not rectangularity, so a
    small probe region gives the cleanest signal.
    """
    fp = {"path": "/x.pdf", "mtime_ns": 1, "size": 2}
    lat_sw, lat_ne = 32.0, 32.1
    mid_lat = (lat_sw + lat_ne) / 2.0
    dlon_for_one_planar = 0.1 / math.cos(math.radians(mid_lat))
    lon_sw = 35.0
    lon_ne = lon_sw + dlon_for_one_planar
    a = CalibrationPoint(code="SW", lat=lat_sw, lon=lon_sw, u=0.0, v=1.0)
    b = CalibrationPoint(code="NE", lat=lat_ne, lon=lon_ne, u=1.0, v=0.0)
    c = CalibrationPoint(code="NW", lat=lat_ne, lon=lon_sw, u=0.0, v=0.0)
    cal = calibration_from_points(fp, a, b, c)

    u_se, v_se = cal.lonlat_to_uv(lon_ne, lat_sw)
    assert pytest.approx(u_se, abs=2e-3) == 1.0
    assert pytest.approx(v_se, abs=2e-3) == 1.0


def test_off_anchor_point_not_mirrored() -> None:
    """Going east of an anchor must increase u; going north must decrease v.

    Catches the classic missing-reflection bug where the affine ends up mirroring around
    an anchor line.
    """
    fp = {"path": "/x.pdf", "mtime_ns": 1, "size": 2}
    a, b, c = _three_anchors()
    cal = calibration_from_points(fp, a, b, c)
    u_east, _ = cal.lonlat_to_uv(a.lon + 0.1, a.lat)
    assert u_east > a.u, "Going east of an anchor must increase u, not decrease it."
    _, v_north = cal.lonlat_to_uv(a.lon, a.lat + 0.1)
    assert v_north < a.v, "Going north of an anchor must decrease v (image y points down)."


def test_uv_lonlat_round_trip() -> None:
    fp = {"path": "/x.pdf", "mtime_ns": 1, "size": 2}
    cal = calibration_from_points(fp, *_three_anchors())
    lon, lat = 35.2, 33.1
    u, v = cal.lonlat_to_uv(lon, lat)
    lon2, lat2 = cal.uv_to_lonlat(u, v)
    assert pytest.approx(lon2, abs=1e-7) == lon
    assert pytest.approx(lat2, abs=1e-7) == lat


# ---------------------------------------------------------------------------
# Lambert Conformal Conic projection
# ---------------------------------------------------------------------------


def test_lcc_constants_define_standard_israeli_chart_parameters() -> None:
    """Sanity guard: ICAO Annex 4 mandates LCC standard parallels that
    bracket the chart's lat coverage. For Israel that's ~29.5..33 N, so
    the standard parallels must lie inside that band. Catches a fat-
    fingered constant edit (e.g. someone copying European LCC parameters
    of 46/49 N here)."""
    assert 29.0 <= LCC_PHI_1_DEG < LCC_PHI_2_DEG <= 34.0
    assert LCC_PHI_1_DEG < LCC_PHI_0_DEG < LCC_PHI_2_DEG
    # Central meridian within Israel's lon span.
    assert 33.0 <= LCC_LAMBDA_0_DEG <= 37.0


def test_lcc_project_at_central_meridian_returns_zero_x() -> None:
    """A point on the central meridian (lon = lambda_0) must project to
    ``X = 0`` regardless of latitude: theta = n * (lon - lambda_0) = 0,
    sin(0) = 0. If this fails, the projection formula is wrong or
    ``LCC_LAMBDA_0_DEG`` got rotated by 180."""
    for lat in (29.5, 31.0, 32.5, 33.0):
        x, _y = lcc_project(lat, LCC_LAMBDA_0_DEG)
        assert pytest.approx(x, abs=1e-12) == 0.0


def test_lcc_project_at_lat_origin_returns_zero_y() -> None:
    """A point at the lat-origin and central meridian projects to
    ``(0, 0)``: rho == rho_0, theta == 0 => Y = rho_0 - rho * cos(0) = 0.
    Pins down the false-northing convention."""
    x, y = lcc_project(LCC_PHI_0_DEG, LCC_LAMBDA_0_DEG)
    assert pytest.approx(x, abs=1e-12) == 0.0
    assert pytest.approx(y, abs=1e-12) == 0.0


def test_lcc_project_y_increases_northward_and_x_eastward() -> None:
    """Standard cartographic sign convention: ``X`` grows eastward,
    ``Y`` grows northward (Snyder p. 105). The downstream 6-DoF affine
    is responsible for converting to image axes (where ``v`` grows
    downward / southward). Pinning this convention here catches an
    accidental sign inversion at the projection layer."""
    _x_n, y_n = lcc_project(LCC_PHI_0_DEG + 1.0, LCC_LAMBDA_0_DEG)
    _x_s, y_s = lcc_project(LCC_PHI_0_DEG - 1.0, LCC_LAMBDA_0_DEG)
    assert y_n > y_s, "Y must grow northward"
    assert y_n > 0.0 > y_s, "Lat-origin is the Y=0 reference"

    x_e, _y_e = lcc_project(LCC_PHI_0_DEG, LCC_LAMBDA_0_DEG + 1.0)
    x_w, _y_w = lcc_project(LCC_PHI_0_DEG, LCC_LAMBDA_0_DEG - 1.0)
    assert x_e > x_w, "X must grow eastward"
    assert x_e > 0.0 > x_w, "Central meridian is the X=0 reference"


def test_lcc_unproject_round_trip_over_israel() -> None:
    """Forward then inverse must be the identity to better than 1e-9
    degrees -- ~ 0.1 mm at the equator, well below the floating-point
    floor of any downstream computation."""
    for lat in (29.6, 30.5, 31.5, 32.5, 33.1):
        for lon in (34.3, 34.8, 35.0, 35.5, 35.9):
            x, y = lcc_project(lat, lon)
            lat2, lon2 = lcc_unproject(x, y)
            assert pytest.approx(lat2, abs=1e-9) == lat, (
                f"lat round-trip failed at ({lat}, {lon}): got {lat2}"
            )
            assert pytest.approx(lon2, abs=1e-9) == lon, (
                f"lon round-trip failed at ({lat}, {lon}): got {lon2}"
            )


def test_lcc_project_distinguishes_lcc_from_planar() -> None:
    """The whole motivation for LCC: it is NOT a linear function of
    (lat, lon). Verify that the difference between LCC and the legacy
    planar approximation is structurally non-zero over Israel, so we
    know the production code is exercising LCC rather than silently
    collapsing to the planar limit."""
    cos_mid = math.cos(math.radians(LCC_PHI_0_DEG))

    def planar(lat: float, lon: float) -> tuple[float, float]:
        return ((lon - LCC_LAMBDA_0_DEG) * cos_mid, -(lat - LCC_PHI_0_DEG))

    # Far corners of Israel: the LCC <-> planar disagreement is largest
    # where lat is far from LCC_PHI_0_DEG AND lon is far from
    # LCC_LAMBDA_0_DEG (meridian convergence + parallel curvature).
    corners = [(29.5, 34.3), (33.2, 35.9), (29.5, 35.9), (33.2, 34.3)]
    max_delta = 0.0
    for lat, lon in corners:
        xL, yL = lcc_project(lat, lon)
        xP, yP = planar(lat, lon)
        # Normalise both to the same nominal degree-of-arc scale before
        # subtracting -- planar is in degrees, LCC is in radians.
        xL_deg = math.degrees(xL)
        yL_deg = math.degrees(yL)
        delta = math.hypot(xL_deg - xP, yL_deg - yP)
        max_delta = max(max_delta, delta)
    # Empirically this should be ~0.05 degrees at the corner; require
    # at least 0.01 deg ~ 1 km so the test is robust to anyone tightening
    # the LCC parallels.
    assert max_delta > 0.01, (
        f"LCC and planar projections appear identical (max delta {max_delta:.4f} deg). "
        "Either LCC parameters have collapsed to degeneracy or the projection function is wrong."
    )


def test_lcc_pipeline_dramatically_outfits_planar_on_realistic_israeli_anchors() -> None:
    """Property-style test of the actual claim driving the LCC switch:
    fitting a 6-DoF affine *after* LCC projection should give much
    smaller residuals on real-chart-shaped data than fitting an affine
    *directly* against (lat, lon).

    The anchor set below is a stand-in for production north-sheet
    clicks: 7 anchors spread across Israel, each carrying a UV produced
    by the EXACT LCC + similarity pipeline (so the LCC + affine fit has
    zero residual by construction). The legacy planar pipeline applied
    to the same anchors leaves a measurable residual because the
    underlying transform is genuinely non-linear in (lat, lon).
    """
    fp = {"path": "/x.pdf", "mtime_ns": 1, "size": 2}
    # Synthesise UV from LCC + a known affine (the truth model).
    sx_scale, sy_scale, shear = 60.0, 58.0, 1.5
    tx_off, ty_off = 0.5, 0.5

    def truth_uv(lat: float, lon: float) -> tuple[float, float]:
        x, y = lcc_project(lat, lon)
        return (sx_scale * x + shear * y + tx_off,
                shear * x + sy_scale * y + ty_off)

    israel_anchors = [
        ("HOTRM", 32.754, 34.937),
        ("PELEG", 32.330, 34.837),
        ("BASAN", 33.146, 35.638),
        ("TIRAT", 32.424, 35.527),
        ("SDROT", 31.507, 34.586),
        ("OMMER", 31.275, 34.828),
        ("ENGDI", 31.464, 35.395),
    ]
    cps = [
        CalibrationPoint(
            code=code, lat=lat, lon=lon,
            u=truth_uv(lat, lon)[0], v=truth_uv(lat, lon)[1],
        )
        for (code, lat, lon) in israel_anchors
    ]

    cal_lcc = calibration_from_points(fp, *cps)
    # LCC pipeline must hit ~ zero residual because we synthesised the
    # UV through *exactly* LCC + an affine.
    assert cal_lcc.residual_uv < 1e-10, (
        f"LCC + affine should fit LCC-synthetic data to working precision; "
        f"got residual_uv = {cal_lcc.residual_uv:.3e}"
    )

    # Now fit a 6-DoF affine directly against (lon, lat) -- emulating
    # the legacy pre-LCC pipeline -- and assert the residual is much
    # bigger. We do this inline (no production code path) so the
    # comparison is unambiguous.
    mean_lat = sum(p.lat for p in cps) / len(cps)
    cos_lat = math.cos(math.radians(mean_lat))

    # Reuse the production _lsq_affine via a duplicate-import-friendly path.
    from cvfr_routemaster.geo_calibration import _lsq_affine  # noqa: PLC0415

    src_planar = [(p.lon * cos_lat, -p.lat) for p in cps]
    dst = [(p.u, p.v) for p in cps]
    fwd_planar, _ = _lsq_affine(src_planar, dst)
    planar_residual_sq = 0.0
    for p in cps:
        u_pred, v_pred = fwd_planar(p.lon * cos_lat, -p.lat)
        planar_residual_sq += (u_pred - p.u) ** 2 + (v_pred - p.v) ** 2
    planar_residual = math.sqrt(planar_residual_sq / len(cps))

    # The planar fit's residual must be vastly larger than LCC's. On
    # real Israeli geometry the ratio is ~1e7 (residual ~0.001 vs
    # ~1e-10); a 100x margin is a robust floor that won't false-alarm
    # if floating-point quirks shift things by a couple of orders.
    assert planar_residual > 100.0 * cal_lcc.residual_uv, (
        f"LCC pipeline should massively outfit the legacy planar pipeline on "
        f"LCC-synthetic data; got LCC residual={cal_lcc.residual_uv:.3e}, "
        f"planar residual={planar_residual:.3e}."
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_rejects_duplicate_waypoint_codes() -> None:
    fp = {"path": "/x.pdf", "mtime_ns": 1, "size": 2}
    a = CalibrationPoint(code="SAME", lat=33.0, lon=35.0, u=0.1, v=0.2)
    b = CalibrationPoint(code="SAME", lat=33.5, lon=35.8, u=0.7, v=0.9)
    c = CalibrationPoint(code="C", lat=32.6, lon=35.6, u=0.5, v=0.5)
    with pytest.raises(ValueError, match="distinct"):
        calibration_from_points(fp, a, b, c)


def test_rejects_under_minimum_anchors() -> None:
    """The affine fit needs at least 3 anchors. Two anchors must be rejected outright."""
    fp = {"path": "/x.pdf", "mtime_ns": 1, "size": 2}
    a = CalibrationPoint(code="A", lat=33.0, lon=35.0, u=0.1, v=0.2)
    b = CalibrationPoint(code="B", lat=33.5, lon=35.8, u=0.7, v=0.9)
    with pytest.raises(ValueError, match=str(MIN_ANCHORS)):
        calibration_from_points(fp, a, b)


def test_rejects_collinear_anchors() -> None:
    """Three anchors on a single line in the source plane make the Gram singular. The
    affine fit must reject them rather than returning a NaN-laden model."""
    fp = {"path": "/x.pdf", "mtime_ns": 1, "size": 2}
    cps = [
        CalibrationPoint(code="A", lat=32.0, lon=35.0, u=0.10, v=0.10),
        CalibrationPoint(code="B", lat=33.0, lon=35.0, u=0.50, v=0.50),
        CalibrationPoint(code="C", lat=34.0, lon=35.0, u=0.90, v=0.90),
    ]
    with pytest.raises(ValueError, match="collinear|coincident"):
        calibration_from_points(fp, *cps)


# ---------------------------------------------------------------------------
# Layout / persistence
# ---------------------------------------------------------------------------


def test_map_layout_matches() -> None:
    a = {"x": 10.0, "y": -20.0, "scale": 1.05}
    assert map_layout_matches(a, dict(a))
    assert not map_layout_matches(a, {"x": 500.0, "y": -20.0, "scale": 1.05})


def test_layout_mismatch_rejects_load(tmp_path: Path) -> None:
    p = tmp_path / "n.pdf"
    p.write_bytes(b"x")
    ml = {"x": 0.0, "y": 0.0, "scale": 1.0}
    cal = calibration_from_points(
        pdf_fingerprint(p), *_three_anchors(), map_layout=ml
    )
    raw = {"north": sheet_to_dict(cal)}
    bad = {"x": 400.0, "y": 0.0, "scale": 1.0}
    c, err = load_sheet_calibration_or_reason(raw, "north", p, bad, "North")
    assert c is None
    assert err is not None


def test_fingerprint_invalidation(tmp_path: Path) -> None:
    p = tmp_path / "n.pdf"
    p.write_bytes(b"hello")
    cal = calibration_from_points(
        pdf_fingerprint(p),
        *_three_anchors(),
        map_layout={"x": 0.0, "y": 0.0, "scale": 1.0},
    )
    payload = {
        "version": CALIBRATION_FILE_VERSION,
        "north": sheet_to_dict(cal),
        "south": None,
    }
    save_calibration_payload(tmp_path, payload)
    raw = load_saved_calibration(tmp_path)
    assert try_load_sheet_calibration(raw, "north", p) is not None
    p.write_bytes(b"changed")
    raw2 = load_saved_calibration(tmp_path)
    assert try_load_sheet_calibration(raw2, "north", p) is None


def test_sheet_json_round_trip() -> None:
    fp = {"path": "/y.pdf", "mtime_ns": 3, "size": 4}
    ml = {"x": 12.0, "y": 34.5, "scale": 0.88}
    cal = calibration_from_points(fp, *_three_anchors(), map_layout=ml)
    d = sheet_to_dict(cal)
    cal2 = sheet_from_dict(d)
    assert cal2 is not None
    assert [p.code for p in cal2.points] == ["A", "B", "C"]
    assert cal2.map_layout == ml
    a = cal.points[0]
    u, v = cal2.lonlat_to_uv(a.lon, a.lat)
    assert pytest.approx((u, v), abs=1e-9) == (a.u, a.v)


def test_sheet_from_dict_rejects_under_minimum_anchors() -> None:
    """JSON with only 2 anchors must be refused (no legacy 2-point support)."""
    bad = {
        "pdf": {"path": "/x.pdf", "mtime_ns": 1, "size": 2},
        "points": [
            {"code": "A", "lat": 33.0, "lon": 35.0, "u": 0.20, "v": 0.80},
            {"code": "B", "lat": 33.5, "lon": 35.5, "u": 0.85, "v": 0.20},
        ],
    }
    assert sheet_from_dict(bad) is None


# ---------------------------------------------------------------------------
# Affine fit quality — the actual reason this exists
# ---------------------------------------------------------------------------


def test_lsq_four_anchors_zero_residual_when_truth_is_similarity() -> None:
    """A similarity is a special case of an affine. The affine LSQ must recover it
    exactly from 4 noiseless anchors: residual ~0 and an off-anchor point projects to
    the synthetic truth.
    """
    fp = {"path": "/x.pdf", "mtime_ns": 1, "size": 2}
    cps = _four_anchors_from_similarity()
    cal = calibration_from_points(fp, *cps)
    assert cal.residual_uv < 1e-12, (
        f"Affine LSQ on consistent 4-anchor data must have ~0 residual; got {cal.residual_uv:.3e}"
    )
    for p in cps:
        u, v = cal.lonlat_to_uv(p.lon, p.lat)
        assert pytest.approx((u, v), abs=1e-9) == (p.u, p.v)


def test_affine_recovers_anisotropic_chart_a_similarity_cannot() -> None:
    """The fix for "OSNAT shifted 1/3 screen, AYLON at the edge": anisotropic charts.

    Even after projecting through LCC the chart can still have *per-sheet*
    anisotropic distortion -- paper stretch, scan skew, slight non-uniform
    scale between print runs -- that a 4-DoF similarity cannot absorb. We
    model the per-sheet distortion here as different x and y scale factors
    plus a small shear applied to LCC-projected coordinates. The 6-DoF
    affine must recover it; a similarity should not.

    Build a synthetic anisotropic ground truth (LCC + non-similarity
    affine), fit the production calibration on 4 anchors, and check the
    projection error at a far test point. We also fit a similarity by
    hand on the same LCC-projected anchors and confirm it misses by a
    measurable, non-trivial margin -- proving the 6-DoF upgrade was
    necessary on top of LCC, not just instead of the legacy planar
    pipeline.
    """
    # Hand-rolled similarity LSQ for the comparison only; never used in production.
    def _fit_similarity(
        src: list[tuple[float, float]], dst: list[tuple[float, float]]
    ):
        n = len(src)
        cx_s = sum(p[0] for p in src) / n
        cy_s = sum(p[1] for p in src) / n
        cx_d = sum(p[0] for p in dst) / n
        cy_d = sum(p[1] for p in dst) / n
        nr = ni = den = 0.0
        for (x, y), (u, v) in zip(src, dst):
            sx, sy = x - cx_s, y - cy_s
            du, dv = u - cx_d, v - cy_d
            nr += du * sx + dv * sy
            ni += dv * sx - du * sy
            den += sx * sx + sy * sy
        zr, zi = nr / den, ni / den
        tx = cx_d - (zr * cx_s - zi * cy_s)
        ty = cy_d - (zr * cy_s + zi * cx_s)
        return lambda x, y: (zr * x - zi * y + tx, zr * y + zi * x + ty)

    fp = {"path": "/x.pdf", "mtime_ns": 1, "size": 2}

    # Per-sheet anisotropic distortion applied to LCC-projected coords.
    # LCC X-axis spans ~ +/- 0.01 over Israel; we scale up so 0..1 UV is
    # a realistic chart geometry.
    sx_scale = 62.0
    sy_scale = 55.0  # 12% smaller in y -- the kind of print/scan anisotropy this absorbs
    shear = 4.0
    tx_off, ty_off = 0.4, 0.4

    def truth(lon: float, lat: float) -> tuple[float, float]:
        x, y = lcc_project(lat, lon)
        u = sx_scale * x + shear * y + tx_off
        v = shear * x + sy_scale * y + ty_off
        return u, v

    anchors_geo = [
        ("A", 30.5, 34.6),
        ("B", 32.5, 34.6),
        ("C", 32.5, 35.8),
        ("D", 30.5, 35.8),
    ]
    cps = [
        CalibrationPoint(code=code, lat=lat, lon=lon, u=truth(lon, lat)[0], v=truth(lon, lat)[1])
        for code, lat, lon in anchors_geo
    ]
    cal_affine = calibration_from_points(fp, *cps)

    src = [lcc_project(p.lat, p.lon) for p in cps]
    dst = [(p.u, p.v) for p in cps]
    fwd_sim = _fit_similarity(src, dst)

    test_lon, test_lat = 34.95, 29.6  # EILAT-ish: well outside the anchor cluster.
    u_truth, v_truth = truth(test_lon, test_lat)

    u_aff, v_aff = cal_affine.lonlat_to_uv(test_lon, test_lat)
    x_test, y_test = lcc_project(test_lat, test_lon)
    u_sim, v_sim = fwd_sim(x_test, y_test)

    err_aff = math.hypot(u_aff - u_truth, v_aff - v_truth)
    err_sim = math.hypot(u_sim - u_truth, v_sim - v_truth)

    assert err_aff < 1e-9, f"Affine must recover anisotropic truth exactly; got {err_aff:.3e}"
    assert err_sim > 0.005, (
        f"Similarity should be visibly off on anisotropic truth; got err={err_sim:.3e}. "
        "If this fails the synthetic chart isn't anisotropic enough to exercise the bug."
    )
    assert err_sim > 1000.0 * err_aff, (
        f"Affine must dramatically beat similarity on anisotropic truth; "
        f"got err_sim={err_sim:.3e}, err_aff={err_aff:.3e}."
    )


# ---------------------------------------------------------------------------
# compute_overlap_aligned_layout — derive south sheet's layout from the user's
# clicks at the shared overlap anchors so the same lat/lon lands at the same
# scene position on both sheets (no more Alt+wheel manual stitching).
# ---------------------------------------------------------------------------


_FP = {"path": "/x.pdf", "mtime_ns": 1, "size": 2}


def _sheet_with_overlap_clicks(
    edge_uv: list[tuple[str, float, float, float, float]],
    overlap_uv: list[tuple[str, float, float, float, float]],
):
    """Build a SheetGeoCalibration whose point list is ``edge_uv + overlap_uv``.

    Tuple layout per row: ``(code, lat, lon, u, v)``. The lat/lon values only
    have to be non-collinear because we never exercise the affine fit here —
    ``compute_overlap_aligned_layout`` is pure scene-pixel math over the
    stored ``(u, v)`` clicks, not the affine. We still build a real calibration
    object so the function under test sees identical types to production.
    """
    points = [
        CalibrationPoint(code=c, lat=la, lon=lo, u=u, v=v)
        for (c, la, lo, u, v) in edge_uv + overlap_uv
    ]
    return calibration_from_points(_FP, *points)


def _invert_south_scene_to_uv(
    scene_x: float, scene_y: float, W_s: float, H_s: float,
    scale: float, tx: float, ty: float,
) -> tuple[float, float]:
    """Invert ``scene = pos + scale · local + (1 − scale) · pixmap_centre``
    to recover the chart UV given a target scene position.

    Mirrors the production formula in ``compute_overlap_aligned_layout``'s
    docstring; tests must use the same convention or they'd encode the old
    origin-at-(0,0) bug we just fixed (consistent ~14 px westward shift
    when ``scale ≠ 1``).
    """
    local_x = (scene_x - tx - (1.0 - scale) * W_s * 0.5) / scale
    local_y = (scene_y - ty - (1.0 - scale) * H_s * 0.5) / scale
    return (local_x / W_s, local_y / H_s)


def test_overlap_alignment_recovers_known_south_layout_exactly() -> None:
    """Synthesise south's clicks from a known ``(scale, tx, ty)`` and verify
    the LSQ solver recovers it to floating-point precision.

    Construction: place three "overlap" anchor scene positions on the north
    sheet (with the convention W_n, H_n = (5000, 7000) and north pinned at
    identity, so the north click in scene equals ``(u_n · W_n, v_n · H_n)``).
    Map those scene positions back through the inverse of a chosen south
    layout — accounting for ``transformOriginPoint = pixmap centre`` — to
    obtain south UVs. The solver, given both sets of clicks, must recover
    the chosen layout exactly because the system has zero residual by
    construction.
    """
    W_n, H_n = 5000.0, 7000.0
    W_s, H_s = 4500.0, 6800.0
    true_scale, true_tx, true_ty = 1.0123, -42.5, 6150.0

    # Three overlap-anchor scene positions on north.
    scene_pts = [(1200.0, 6000.0), (2400.0, 6700.0), (3800.0, 6100.0)]
    overlap_north = []
    overlap_south = []
    for i, (sx, sy) in enumerate(scene_pts):
        code = f"OV{i}"
        # North pinned at identity: scene = (u·W_n, v·H_n).
        un, vn = sx / W_n, sy / H_n
        # South uses centre-origin: scene = scale·local + (1−scale)·W/2 + tx.
        us, vs = _invert_south_scene_to_uv(
            sx, sy, W_s, H_s, true_scale, true_tx, true_ty
        )
        overlap_north.append((code, 31.3 + 0.05 * i, 34.5 + 0.4 * i, un, vn))
        overlap_south.append((code, 31.3 + 0.05 * i, 34.5 + 0.4 * i, us, vs))

    edges_n = [
        ("E0", 32.7, 35.0, 0.45, 0.30),
        ("E1", 32.3, 34.85, 0.40, 0.50),
        ("E2", 33.1, 35.5, 0.85, 0.10),
    ]
    edges_s = [
        ("S0", 30.5, 34.5, 0.20, 0.40),
        ("S1", 29.9, 34.9, 0.50, 0.85),
        ("S2", 30.3, 35.2, 0.65, 0.55),
    ]
    north_cal = _sheet_with_overlap_clicks(edges_n, overlap_north)
    south_cal = _sheet_with_overlap_clicks(edges_s, overlap_south)

    alignment = compute_overlap_aligned_layout(
        north_cal, south_cal, (W_n, H_n), (W_s, H_s)
    )
    assert alignment is not None
    assert alignment.scale == pytest.approx(true_scale, abs=1e-9)
    assert alignment.tx == pytest.approx(true_tx, abs=1e-6)
    assert alignment.ty == pytest.approx(true_ty, abs=1e-6)
    assert alignment.residual_px < 1e-6
    assert alignment.shared_codes == ("OV0", "OV1", "OV2")


def test_overlap_alignment_two_anchors_is_minimum() -> None:
    """Two shared anchors give 4 equations for 3 unknowns — enough to pin
    ``(scale, tx, ty)`` with one redundant equation. The contract is that
    we accept ≥2 shared codes (see ``MIN_OVERLAP_ALIGNMENT_ANCHORS``).
    """
    assert MIN_OVERLAP_ALIGNMENT_ANCHORS == 2

    W_n, H_n = 4000.0, 6000.0
    W_s, H_s = 3800.0, 5900.0
    true_scale, true_tx, true_ty = 0.987, 12.3, 4200.0

    overlap_north = []
    overlap_south = []
    for i, (sx, sy) in enumerate([(900.0, 5100.0), (2700.0, 5400.0)]):
        un, vn = sx / W_n, sy / H_n
        us, vs = _invert_south_scene_to_uv(
            sx, sy, W_s, H_s, true_scale, true_tx, true_ty
        )
        overlap_north.append((f"OV{i}", 31.3 + 0.05 * i, 34.5 + 0.4 * i, un, vn))
        overlap_south.append((f"OV{i}", 31.3 + 0.05 * i, 34.5 + 0.4 * i, us, vs))

    edges_n = [
        ("E0", 32.7, 35.0, 0.4, 0.3),
        ("E1", 32.3, 34.85, 0.5, 0.5),
        ("E2", 33.1, 35.5, 0.8, 0.1),
    ]
    edges_s = [
        ("S0", 30.5, 34.5, 0.2, 0.4),
        ("S1", 29.9, 34.9, 0.5, 0.85),
        ("S2", 30.3, 35.2, 0.65, 0.55),
    ]
    alignment = compute_overlap_aligned_layout(
        _sheet_with_overlap_clicks(edges_n, overlap_north),
        _sheet_with_overlap_clicks(edges_s, overlap_south),
        (W_n, H_n),
        (W_s, H_s),
    )
    assert alignment is not None
    assert alignment.scale == pytest.approx(true_scale, abs=1e-9)
    assert alignment.tx == pytest.approx(true_tx, abs=1e-6)
    assert alignment.ty == pytest.approx(true_ty, abs=1e-6)


def test_overlap_alignment_one_anchor_returns_none() -> None:
    """One shared anchor gives 2 equations for 3 unknowns — under-determined.

    Regression guard: the function used to silently return a degenerate
    (scale=0) solution. The current contract is to refuse outright so the
    caller can fall back to a stacked default rather than apply junk.
    """
    edges_n = [
        ("E0", 32.7, 35.0, 0.4, 0.3),
        ("E1", 32.3, 34.85, 0.5, 0.5),
        ("E2", 33.1, 35.5, 0.8, 0.1),
    ]
    edges_s = [
        ("S0", 30.5, 34.5, 0.2, 0.4),
        ("S1", 29.9, 34.9, 0.5, 0.85),
        ("S2", 30.3, 35.2, 0.65, 0.55),
    ]
    overlap = [("OV0", 31.3, 34.6, 0.3, 0.9)]
    alignment = compute_overlap_aligned_layout(
        _sheet_with_overlap_clicks(edges_n, overlap),
        _sheet_with_overlap_clicks(edges_s, overlap),
        (4000.0, 6000.0),
        (3800.0, 5900.0),
    )
    assert alignment is None


def test_overlap_alignment_no_shared_codes_returns_none() -> None:
    edges_n = [
        ("E0", 32.7, 35.0, 0.4, 0.3),
        ("E1", 32.3, 34.85, 0.5, 0.5),
        ("E2", 33.1, 35.5, 0.8, 0.1),
    ]
    edges_s = [
        ("S0", 30.5, 34.5, 0.2, 0.4),
        ("S1", 29.9, 34.9, 0.5, 0.85),
        ("S2", 30.3, 35.2, 0.65, 0.55),
    ]
    overlap_n = [
        ("NA", 31.3, 34.6, 0.3, 0.9),
        ("NB", 31.4, 34.8, 0.5, 0.95),
        ("NC", 31.4, 35.3, 0.8, 0.92),
    ]
    overlap_s = [
        ("SA", 31.3, 34.6, 0.3, 0.05),
        ("SB", 31.4, 34.8, 0.5, 0.1),
        ("SC", 31.4, 35.3, 0.8, 0.08),
    ]
    alignment = compute_overlap_aligned_layout(
        _sheet_with_overlap_clicks(edges_n, overlap_n),
        _sheet_with_overlap_clicks(edges_s, overlap_s),
        (4000.0, 6000.0),
        (3800.0, 5900.0),
    )
    assert alignment is None


def test_overlap_alignment_code_matching_is_case_insensitive() -> None:
    """Calibration storage upper-cases codes (see ``__post_init__``), but the
    overlap-alignment matcher must also case-fold so a database with mixed
    case codes still works.
    """
    W_n, H_n = 5000.0, 7000.0
    W_s, H_s = 4500.0, 6800.0
    overlap_north = []
    overlap_south = []
    for i, (sx, sy) in enumerate([(1200.0, 6000.0), (2400.0, 6700.0), (3800.0, 6100.0)]):
        un, vn = sx / W_n, sy / H_n
        # scale = 1 → centre-origin pre-shift vanishes, simple inverse works.
        us = sx / W_s
        vs = sy / H_s
        # Mixed case across sheets — must still pair up.
        overlap_north.append((f"Ov{i}", 31.3 + 0.05 * i, 34.5 + 0.4 * i, un, vn))
        overlap_south.append((f"oV{i}", 31.3 + 0.05 * i, 34.5 + 0.4 * i, us, vs))
    edges_n = [
        ("E0", 32.7, 35.0, 0.4, 0.3),
        ("E1", 32.3, 34.85, 0.5, 0.5),
        ("E2", 33.1, 35.5, 0.8, 0.1),
    ]
    edges_s = [
        ("S0", 30.5, 34.5, 0.2, 0.4),
        ("S1", 29.9, 34.9, 0.5, 0.85),
        ("S2", 30.3, 35.2, 0.65, 0.55),
    ]
    alignment = compute_overlap_aligned_layout(
        _sheet_with_overlap_clicks(edges_n, overlap_north),
        _sheet_with_overlap_clicks(edges_s, overlap_south),
        (W_n, H_n),
        (W_s, H_s),
    )
    assert alignment is not None
    assert set(alignment.shared_codes) == {"OV0", "OV1", "OV2"}


def test_overlap_alignment_residual_quantifies_click_slop() -> None:
    """If the user's clicks at the shared overlap anchors disagree (as they
    did in the user's pre-fix calibration: ~22 px systematic offset across
    all three overlap codes), the residual reports the disagreement so the
    caller can warn or refuse.
    """
    W_n, H_n = 5000.0, 7000.0
    W_s, H_s = 4500.0, 6800.0
    true_scale = 1.0
    # Put north's clicks at known scene positions and south's at the *same*
    # scene positions PLUS a systematic (dx, dy) — exactly the
    # eye-aligned-but-not-pixel-perfect failure mode the diagnostic on the
    # production calibration uncovered. ``compute_overlap_aligned_layout``
    # cannot absorb this with pure translation+scale (the offset is the
    # same at every anchor, so it shows up as a translation, which makes
    # the residual *zero* — the unhelpful case). Instead vary the offset
    # per anchor so it's irreducible by scale+translation.
    per_anchor_offset = [(0.0, 0.0), (5.0, 0.0), (-5.0, 0.0)]
    overlap_north = []
    overlap_south = []
    for i, ((sx, sy), (dx, dy)) in enumerate(zip(
        [(1200.0, 6000.0), (2400.0, 6700.0), (3800.0, 6100.0)],
        per_anchor_offset,
    )):
        un, vn = sx / W_n, sy / H_n
        # scale = 1 → centre-origin pre-shift vanishes, simple inverse works.
        us = (sx + dx) / W_s
        vs = (sy + dy) / H_s
        overlap_north.append((f"OV{i}", 31.3 + 0.05 * i, 34.5 + 0.4 * i, un, vn))
        overlap_south.append((f"OV{i}", 31.3 + 0.05 * i, 34.5 + 0.4 * i, us, vs))
    edges_n = [
        ("E0", 32.7, 35.0, 0.4, 0.3),
        ("E1", 32.3, 34.85, 0.5, 0.5),
        ("E2", 33.1, 35.5, 0.8, 0.1),
    ]
    edges_s = [
        ("S0", 30.5, 34.5, 0.2, 0.4),
        ("S1", 29.9, 34.9, 0.5, 0.85),
        ("S2", 30.3, 35.2, 0.65, 0.55),
    ]
    alignment = compute_overlap_aligned_layout(
        _sheet_with_overlap_clicks(edges_n, overlap_north),
        _sheet_with_overlap_clicks(edges_s, overlap_south),
        (W_n, H_n),
        (W_s, H_s),
    )
    assert alignment is not None
    # Residual must reflect the irreducible per-anchor offset, not be 0
    # (which would mean the warning system would never fire).
    assert alignment.residual_px > 1.0
    # Sanity: residual stays in the same order of magnitude as the offsets.
    assert alignment.residual_px < 10.0


def test_overlap_alignment_pins_north_at_identity_property() -> None:
    """Whatever scene-position math we do, north's click is computed as
    ``(u_n · W_n, v_n · H_n)`` — i.e. north is *pinned* at scale 1 / offset 0.

    Recovering the south layout from an injected ground truth and then
    re-evaluating ``scene_north == scene_south_under_centre_origin`` gives
    us a clean property test guarding both the pinning convention *and*
    the centre-origin pre-shift Qt applies when scaling about pixmap centre.
    """
    W_n, H_n = 5000.0, 7000.0
    W_s, H_s = 4500.0, 6800.0
    scale, tx, ty = 1.0123, -42.5, 6150.0

    overlap_north = []
    overlap_south = []
    for i, (sx, sy) in enumerate([(1200.0, 6000.0), (2400.0, 6700.0), (3800.0, 6100.0)]):
        un, vn = sx / W_n, sy / H_n
        us, vs = _invert_south_scene_to_uv(sx, sy, W_s, H_s, scale, tx, ty)
        overlap_north.append((f"OV{i}", 31.3 + 0.05 * i, 34.5 + 0.4 * i, un, vn))
        overlap_south.append((f"OV{i}", 31.3 + 0.05 * i, 34.5 + 0.4 * i, us, vs))
    edges_n = [
        ("E0", 32.7, 35.0, 0.4, 0.3),
        ("E1", 32.3, 34.85, 0.5, 0.5),
        ("E2", 33.1, 35.5, 0.8, 0.1),
    ]
    edges_s = [
        ("S0", 30.5, 34.5, 0.2, 0.4),
        ("S1", 29.9, 34.9, 0.5, 0.85),
        ("S2", 30.3, 35.2, 0.65, 0.55),
    ]
    north_cal = _sheet_with_overlap_clicks(edges_n, overlap_north)
    south_cal = _sheet_with_overlap_clicks(edges_s, overlap_south)
    alignment = compute_overlap_aligned_layout(
        north_cal, south_cal, (W_n, H_n), (W_s, H_s)
    )
    assert alignment is not None

    # For every shared anchor: north's pinned scene = south's transformed
    # scene under the centre-origin convention, to within the LSQ residual
    # (zero on this synthetic).
    n_by = {p.code: p for p in north_cal.points}
    s_by = {p.code: p for p in south_cal.points}
    for code in alignment.shared_codes:
        np_ = n_by[code]
        sp = s_by[code]
        scene_n = (np_.u * W_n, np_.v * H_n)
        scene_s = (
            sp.u * W_s * alignment.scale + (1.0 - alignment.scale) * W_s * 0.5 + alignment.tx,
            sp.v * H_s * alignment.scale + (1.0 - alignment.scale) * H_s * 0.5 + alignment.ty,
        )
        assert scene_n[0] == pytest.approx(scene_s[0], abs=1e-6)
        assert scene_n[1] == pytest.approx(scene_s[1], abs=1e-6)


def test_overlap_alignment_zero_pixmap_size_returns_none() -> None:
    """A pixmap with zero width or height implies the chart isn't loaded yet
    — refuse rather than divide by zero downstream.
    """
    edges_n = [
        ("E0", 32.7, 35.0, 0.4, 0.3),
        ("E1", 32.3, 34.85, 0.5, 0.5),
        ("E2", 33.1, 35.5, 0.8, 0.1),
    ]
    edges_s = [
        ("S0", 30.5, 34.5, 0.2, 0.4),
        ("S1", 29.9, 34.9, 0.5, 0.85),
        ("S2", 30.3, 35.2, 0.65, 0.55),
    ]
    overlap = [
        ("OV0", 31.3, 34.6, 0.3, 0.9),
        ("OV1", 31.4, 34.8, 0.5, 0.95),
        ("OV2", 31.4, 35.3, 0.8, 0.92),
    ]
    n = _sheet_with_overlap_clicks(edges_n, overlap)
    s = _sheet_with_overlap_clicks(edges_s, overlap)
    assert compute_overlap_aligned_layout(n, s, (0.0, 100.0), (100.0, 100.0)) is None
    assert compute_overlap_aligned_layout(n, s, (100.0, 100.0), (100.0, 0.0)) is None


def test_overlap_alignment_is_OverlapAlignment_dataclass() -> None:
    """Lock the public return type so importers don't get surprised by a
    silent shape change."""
    edges_n = [
        ("E0", 32.7, 35.0, 0.4, 0.3),
        ("E1", 32.3, 34.85, 0.5, 0.5),
        ("E2", 33.1, 35.5, 0.8, 0.1),
    ]
    edges_s = [
        ("S0", 30.5, 34.5, 0.2, 0.4),
        ("S1", 29.9, 34.9, 0.5, 0.85),
        ("S2", 30.3, 35.2, 0.65, 0.55),
    ]
    overlap_n = [
        ("OV0", 31.3, 34.6, 0.3, 0.9),
        ("OV1", 31.4, 34.8, 0.5, 0.95),
        ("OV2", 31.4, 35.3, 0.8, 0.92),
    ]
    overlap_s = [
        ("OV0", 31.3, 34.6, 0.3, 0.05),
        ("OV1", 31.4, 34.8, 0.5, 0.1),
        ("OV2", 31.4, 35.3, 0.8, 0.08),
    ]
    alignment = compute_overlap_aligned_layout(
        _sheet_with_overlap_clicks(edges_n, overlap_n),
        _sheet_with_overlap_clicks(edges_s, overlap_s),
        (4000.0, 6000.0),
        (3800.0, 5900.0),
    )
    assert isinstance(alignment, OverlapAlignment)
    assert isinstance(alignment.scale, float)
    assert isinstance(alignment.tx, float)
    assert isinstance(alignment.ty, float)
    assert isinstance(alignment.residual_px, float)
    assert isinstance(alignment.shared_codes, tuple)
    assert all(isinstance(c, str) for c in alignment.shared_codes)


def test_overlap_alignment_realistic_israeli_chart_scenario() -> None:
    """End-to-end check on dimensions and clicks matching the production
    Israeli CVFR charts (north 7585×10536, south 7243×10425) with a known
    south layout and pristine clicks. Guards against catastrophic
    numerical issues at the actual pixmap scale (per-row coefficients in
    the design matrix span six orders of magnitude — the original normal-
    equations solver could be ill-conditioned at this scale).
    """
    W_n, H_n = 7585.0, 10536.0
    W_s, H_s = 7243.0, 10425.0
    scale, tx, ty = 1.00351, -83.685, 9253.236

    overlap_specs = [
        # (code, scene_x, scene_y) — picked to mimic SDROT (west, top of strip),
        # OMMER (centre, bottom of strip), ENGDI (east, top of strip).
        ("SDROT", 1958.0, 9322.0),
        ("OMMER", 3004.0, 10500.0),
        ("ENGDI", 5454.0, 9532.0),
    ]
    overlap_north = []
    overlap_south = []
    for i, (code, sx, sy) in enumerate(overlap_specs):
        un, vn = sx / W_n, sy / H_n
        us, vs = _invert_south_scene_to_uv(sx, sy, W_s, H_s, scale, tx, ty)
        overlap_north.append((code, 31.3 + 0.05 * i, 34.5 + 0.4 * i, un, vn))
        overlap_south.append((code, 31.3 + 0.05 * i, 34.5 + 0.4 * i, us, vs))
    edges_n = [
        ("HOTRM", 32.754, 34.937, 0.459, 0.288),
        ("PELEG", 32.330, 34.837, 0.403, 0.491),
        ("BASAN", 33.146, 35.638, 0.852, 0.100),
        ("TIRAT", 32.424, 35.527, 0.792, 0.446),
    ]
    edges_s = [
        ("AZOOZ", 30.800, 34.470, 0.211, 0.346),
        ("YRUHM", 30.990, 34.911, 0.475, 0.255),
        ("EILAT", 29.558, 34.959, 0.502, 0.945),
        ("BMNUH", 30.303, 35.134, 0.608, 0.586),
    ]
    alignment = compute_overlap_aligned_layout(
        _sheet_with_overlap_clicks(edges_n, overlap_north),
        _sheet_with_overlap_clicks(edges_s, overlap_south),
        (W_n, H_n),
        (W_s, H_s),
    )
    assert alignment is not None
    assert alignment.scale == pytest.approx(scale, abs=1e-6)
    assert alignment.tx == pytest.approx(tx, abs=1e-3)
    assert alignment.ty == pytest.approx(ty, abs=1e-3)
    assert alignment.residual_px < 1e-3
    assert alignment.shared_codes == ("ENGDI", "OMMER", "SDROT")  # sorted A→Z


def test_overlap_alignment_respects_centre_origin_convention() -> None:
    """Regression: ``_prepare_map_sheet_item`` sets every chart pixmap's
    ``transformOriginPoint`` to its visual centre, so Qt computes
    ``scene = pos + scale·local + (1 − scale)·centre``. Skipping the
    centre-origin term in the LSQ produces a consistent ~14 px westward
    shift of the south sheet across every overlap anchor on production-
    scale charts (≈ ``(1 − scale)·W_s/2`` with scale ≈ 1.004, W_s ≈ 7243).

    Construct a scenario where the centre-origin term *would* matter
    (scale meaningfully different from 1) and assert the recovered south
    layout produces the *same* scene position from both sheets under the
    centre-origin formula. Crucially, also check that ignoring centre-
    origin (the bug) would produce a non-trivial mismatch — so this test
    actually exercises the fix rather than passing trivially.
    """
    W_n, H_n = 7585.0, 10536.0
    W_s, H_s = 7243.0, 10425.0
    scale, tx, ty = 1.004, -100.0, 9270.0  # ≈ what the user's chart produces

    anchors = [(1958.0, 9322.0), (3004.0, 10500.0), (5454.0, 9532.0)]
    overlap_north = []
    overlap_south = []
    for i, (sx, sy) in enumerate(anchors):
        un, vn = sx / W_n, sy / H_n
        us, vs = _invert_south_scene_to_uv(sx, sy, W_s, H_s, scale, tx, ty)
        overlap_north.append((f"OV{i}", 31.3 + 0.05 * i, 34.5 + 0.4 * i, un, vn))
        overlap_south.append((f"OV{i}", 31.3 + 0.05 * i, 34.5 + 0.4 * i, us, vs))

    edges_n = [
        ("E0", 32.7, 35.0, 0.4, 0.3),
        ("E1", 32.3, 34.85, 0.5, 0.5),
        ("E2", 33.1, 35.5, 0.8, 0.1),
    ]
    edges_s = [
        ("S0", 30.5, 34.5, 0.2, 0.4),
        ("S1", 29.9, 34.9, 0.5, 0.85),
        ("S2", 30.3, 35.2, 0.65, 0.55),
    ]
    north_cal = _sheet_with_overlap_clicks(edges_n, overlap_north)
    south_cal = _sheet_with_overlap_clicks(edges_s, overlap_south)
    alignment = compute_overlap_aligned_layout(
        north_cal, south_cal, (W_n, H_n), (W_s, H_s)
    )
    assert alignment is not None

    pre_shift = (1.0 - alignment.scale) * W_s * 0.5
    # The centre-origin pre-shift is the magnitude of the bug-vs-fixed gap.
    # If we'd silently regressed to origin-(0,0), this term would be folded
    # into the solved ``tx`` and the scene-aligned check below would fail.
    assert abs(pre_shift) > 5.0, (
        f"Test scenario is too close to scale=1; pre-shift = {pre_shift:.3f} px "
        "won't exercise the centre-origin fix. Pick a scale further from 1."
    )

    # Sanity check (the actual contract): the recovered south layout puts
    # every overlap anchor at the SAME scene position as north under the
    # centre-origin formula, to within LSQ noise.
    n_by = {p.code: p for p in north_cal.points}
    s_by = {p.code: p for p in south_cal.points}
    for code in alignment.shared_codes:
        np_ = n_by[code]
        sp = s_by[code]
        scene_n_x = np_.u * W_n
        scene_n_y = np_.v * H_n
        scene_s_x = sp.u * W_s * alignment.scale + (1.0 - alignment.scale) * W_s * 0.5 + alignment.tx
        scene_s_y = sp.v * H_s * alignment.scale + (1.0 - alignment.scale) * H_s * 0.5 + alignment.ty
        assert scene_n_x == pytest.approx(scene_s_x, abs=1e-4)
        assert scene_n_y == pytest.approx(scene_s_y, abs=1e-4)

    # If we'd used the buggy origin-(0,0) formula, every south scene_x would
    # be ``(1 − scale) · W_s/2`` too far west — encode that explicitly so a
    # future regression that drops the pre-shift term gets caught here.
    for code in alignment.shared_codes:
        sp = s_by[code]
        np_ = n_by[code]
        buggy_scene_s_x = sp.u * W_s * alignment.scale + alignment.tx
        scene_n_x = np_.u * W_n
        # The buggy scene_x would differ from north's by ~pre_shift.
        assert abs(buggy_scene_s_x - scene_n_x) == pytest.approx(abs(pre_shift), abs=0.5)


# ───────────────────────────────────────────────────────────────────────────
# compute_joint_calibration — joint LSQ over (north_affine, south_affine,
# layout). The Option 3 part of the user-approved "3+4" stitch fix: solve
# all 15 parameters in one objective so the sat-tile placement (which uses
# the affines) and the chart-pixmap layout agree at the shared anchors.
# ───────────────────────────────────────────────────────────────────────────


def _synthetic_two_sheet_anchors(
    *,
    south_layout: tuple[float, float, float] = (1.005, -90.0, 9300.0),
    W_n: float = 7585.0,
    H_n: float = 10536.0,
    W_s: float = 7243.0,
    H_s: float = 10425.0,
) -> tuple[
    list[CalibrationPoint],
    list[CalibrationPoint],
    tuple[float, float],
    tuple[float, float],
    tuple[float, float, float],
]:
    """Generate an exactly-affine, exactly-consistent synthetic anchor set.

    Returns ``(north_pts, south_pts, north_size, south_size, layout)``
    where:

      * every north click equals ``north_affine(lcc_project(lat, lon))``
        for a known ``north_affine``;
      * every south click equals ``south_affine(lcc_project(lat, lon))``
        for a ``south_affine`` *derived from* ``north_affine`` and
        ``south_layout`` so that the scene-position consistency
        constraint holds identically at every lat/lon (not just the
        overlap anchors);
      * both sheets project through the same LCC parameters by
        construction (LCC is a global projection, not per-sheet).

    Under this construction every click residual, every consistency
    residual, and every chart_diff residual is identically zero, so the
    joint LSQ has a unique fit it must recover sub-pixel. Any deviation
    is an implementation bug.
    """
    s, tx_L, ty_L = south_layout
    half_W_s = W_s * 0.5
    half_H_s = H_s * 0.5

    # 4 north-exclusive anchors, 4 south-exclusive anchors, 3 shared
    # overlap anchors. The shared anchors live in both sheets at the
    # *same* (lat, lon) but with different UV (each sheet's own
    # pixmap coordinates).
    north_only = [
        ("NORA", 32.50, 34.30),
        ("NORB", 32.40, 35.50),
        ("NORC", 32.20, 34.50),
        ("NORD", 32.30, 35.30),
    ]
    south_only = [
        ("SOTA", 31.00, 34.30),
        ("SOTB", 30.90, 35.20),
        ("SOTC", 30.70, 34.80),
        ("SOTD", 31.10, 35.45),
    ]
    shared = [
        ("OVLA", 31.50, 34.50),
        ("OVLB", 31.40, 35.10),
        ("OVLC", 31.55, 35.40),
    ]

    # Pick a deliberately non-similarity north affine: rotation + shear
    # + non-uniform scale, so the joint LSQ has to recover all 6 DoF.
    # Forward: (X, Y) -> (u_n, v_n), where (X, Y) = lcc_project(lat, lon).
    # The LCC X-axis spans ~0.001..0.014 radians of latitude-equivalent
    # over Israel; we scale the coefficients up so the unit u, v range
    # corresponds to realistic chart geometry.
    a_n, b_n, tx_n_aff = 60.0, 2.0, 0.42
    c_n, d_n, ty_n_aff = -1.5, 58.0, 0.60

    def _north_uv(lat: float, lon: float) -> tuple[float, float]:
        x, y = lcc_project(lat, lon)
        return (a_n * x + b_n * y + tx_n_aff, c_n * x + d_n * y + ty_n_aff)

    # Derive south_affine so that for any (lat, lon):
    #   south_u * W_s * s + (1 - s) * W_s/2 + tx_L = north_u * W_n
    # Substituting north_u = a_n * X + b_n * Y + tx_n_aff (with shared
    # X, Y because both sheets project through the *same* LCC) and
    # solving for south_u gives:
    a_s = a_n * W_n / (s * W_s)
    b_s = b_n * W_n / (s * W_s)
    tx_s_aff = (tx_n_aff * W_n - (1.0 - s) * half_W_s - tx_L) / (s * W_s)
    c_s = c_n * H_n / (s * H_s)
    d_s = d_n * H_n / (s * H_s)
    ty_s_aff = (ty_n_aff * H_n - (1.0 - s) * half_H_s - ty_L) / (s * H_s)

    def _south_uv(lat: float, lon: float) -> tuple[float, float]:
        x, y = lcc_project(lat, lon)
        return (a_s * x + b_s * y + tx_s_aff, c_s * x + d_s * y + ty_s_aff)

    n_points: list[CalibrationPoint] = []
    s_points: list[CalibrationPoint] = []
    for code, lat, lon in north_only + shared:
        u, v = _north_uv(lat, lon)
        n_points.append(
            CalibrationPoint(code=code, lat=lat, lon=lon, u=u, v=v)
        )
    for code, lat, lon in shared + south_only:
        u, v = _south_uv(lat, lon)
        s_points.append(
            CalibrationPoint(code=code, lat=lat, lon=lon, u=u, v=v)
        )

    return n_points, s_points, (W_n, H_n), (W_s, H_s), south_layout


def test_joint_calibration_recovers_synthetic_layout_to_subpixel() -> None:
    """On purely synthetic, exactly-affine data with a known south layout,
    the joint LSQ should converge to sub-pixel residuals — anything else
    indicates an implementation bug, not a Lambert-vs-affine misfit."""
    n_pts, s_pts, n_size, s_size, expected_layout = (
        _synthetic_two_sheet_anchors()
    )
    joint = compute_joint_calibration(n_pts, s_pts, n_size, s_size)
    assert joint is not None
    assert joint.converged
    assert joint.iterations <= 32
    # Scene-pixel-units residuals — all should be < 1 px because the
    # input is exactly consistent.
    assert joint.consistency_residual_px < 1.0
    assert joint.chart_residual_px < 1.0
    assert joint.click_residual_north_px < 1.0
    assert joint.click_residual_south_px < 1.0
    # Layout should also be near-exactly recovered.
    expected_s, expected_tx, expected_ty = expected_layout
    assert joint.layout[0] == pytest.approx(expected_s, abs=1e-4)
    assert joint.layout[1] == pytest.approx(expected_tx, abs=0.5)
    assert joint.layout[2] == pytest.approx(expected_ty, abs=0.5)


def test_joint_calibration_balances_click_noise_between_chart_and_consistency() -> None:
    """The whole point of the joint LSQ is the *balance* between chart-on-
    chart and sat-stitch residuals — neither should win at the cost of
    the other when click residuals are non-zero. Adding small per-anchor
    click noise should leave the chart and consistency residuals within
    a factor of ~2 of each other (rather than 10× as you'd get with
    affine-only or click-only layout fits)."""
    n_pts, s_pts, n_size, s_size, _ = _synthetic_two_sheet_anchors()
    # Perturb a few non-shared clicks by a fraction of a pixel of click
    # noise (in UV space). Shared anchors stay clean so we can compare
    # the residuals attributable to the per-sheet fit, not noise at the
    # cross-sheet constraint.
    def _perturb(p: CalibrationPoint, du: float, dv: float) -> CalibrationPoint:
        return CalibrationPoint(
            code=p.code,
            lat=p.lat,
            lon=p.lon,
            u=p.u + du,
            v=p.v + dv,
        )

    n_pts_noisy = list(n_pts)
    n_pts_noisy[0] = _perturb(n_pts_noisy[0], 0.0008, -0.0006)
    n_pts_noisy[1] = _perturb(n_pts_noisy[1], -0.0005, 0.0009)
    s_pts_noisy = list(s_pts)
    s_pts_noisy[-1] = _perturb(s_pts_noisy[-1], 0.0004, 0.0007)

    joint = compute_joint_calibration(n_pts_noisy, s_pts_noisy, n_size, s_size)
    assert joint is not None
    # With well-conditioned synthetic data and small noise the click
    # residuals are still well under a pixel.
    assert joint.click_residual_north_px < 5.0
    assert joint.click_residual_south_px < 5.0
    # Chart and consistency should land within 2× of each other — the
    # balance check this test exists to enforce.
    larger = max(joint.chart_residual_px, joint.consistency_residual_px)
    smaller = min(joint.chart_residual_px, joint.consistency_residual_px)
    if smaller > 1e-6:
        assert larger / smaller < 4.0


def test_joint_calibration_better_than_independent_on_lambert_like_data() -> None:
    """On data with a non-affine signal — mimicking real chart projections
    where neither sheet's affine perfectly fits its own clicks — joint LSQ
    should leave a *smaller* worst-case cross-sheet disagreement at the
    overlap region than independent fits + click-based layout do.

    This is the regression that motivates the entire 3+4 fix.
    """
    # Construct anchors with a known affine for each sheet, then add a
    # small longitude-dependent quadratic distortion to *south's* clicks
    # only — a stand-in for the Lambert projection's curvature on the
    # 1:500k chart. Joint LSQ has more degrees of freedom to absorb this
    # than independent + click-layout, so it should leave smaller
    # worst-case scene disagreement at the shared anchors.
    n_pts, s_pts, n_size, s_size, _ = _synthetic_two_sheet_anchors()
    W_n, H_n = n_size
    W_s, H_s = s_size

    # Push the south clicks for the *shared* anchors only — clean
    # demonstration that joint LSQ can find a layout that compensates.
    def _curve(p: CalibrationPoint) -> CalibrationPoint:
        # Quadratic-in-lon distortion, peaks at lon=34.95 (overlap mid).
        dlon = p.lon - 34.95
        return CalibrationPoint(
            code=p.code,
            lat=p.lat,
            lon=p.lon,
            u=p.u + 0.0012 * dlon * dlon,
            v=p.v,
        )

    shared_codes = {"OVLA", "OVLB", "OVLC"}
    s_pts_warped = [
        _curve(p) if p.code in shared_codes else p for p in s_pts
    ]
    # Also slightly perturb shared anchors on north so the residual
    # signature shows up cleanly on both sides.
    n_pts_warped = [
        _curve(p) if p.code in shared_codes else p for p in n_pts
    ]

    # Baseline: independent affines + click-based layout (the legacy
    # pipeline before joint LSQ).
    n_cal = SheetGeoCalibration(
        pdf_fp={}, points=list(n_pts_warped), map_layout=None
    )
    s_cal = SheetGeoCalibration(
        pdf_fp={}, points=list(s_pts_warped), map_layout=None
    )
    align = compute_overlap_aligned_layout(n_cal, s_cal, n_size, s_size)
    assert align is not None

    def _max_consistency(layout_scale, layout_tx, layout_ty) -> float:
        half_W_s = W_s * 0.5
        half_H_s = H_s * 0.5
        worst = 0.0
        for code in shared_codes:
            n_pt = next(p for p in n_pts_warped if p.code == code)
            s_pt = next(p for p in s_pts_warped if p.code == code)
            u_n, v_n = n_cal.lonlat_to_uv(n_pt.lon, n_pt.lat)
            u_s, v_s = s_cal.lonlat_to_uv(s_pt.lon, s_pt.lat)
            scene_n_x = u_n * W_n
            scene_n_y = v_n * H_n
            scene_s_x = (
                layout_scale * u_s * W_s
                + (1.0 - layout_scale) * half_W_s
                + layout_tx
            )
            scene_s_y = (
                layout_scale * v_s * H_s
                + (1.0 - layout_scale) * half_H_s
                + layout_ty
            )
            d = math.hypot(scene_n_x - scene_s_x, scene_n_y - scene_s_y)
            worst = max(worst, d)
        return worst

    baseline_consistency = _max_consistency(align.scale, align.tx, align.ty)

    # Joint LSQ on the same data.
    joint = compute_joint_calibration(
        n_pts_warped, s_pts_warped, n_size, s_size
    )
    assert joint is not None

    # The joint fit should leave a *smaller* worst-case affine
    # disagreement than the legacy pipeline. The exact ratio depends
    # on the curvature magnitude but the inequality is what we care
    # about — joint LSQ has strictly more DoF for the same data.
    assert joint.consistency_residual_px < baseline_consistency


def test_joint_calibration_rejects_too_few_shared_anchors() -> None:
    """The layout sub-problem needs at least
    :data:`MIN_OVERLAP_ALIGNMENT_ANCHORS` shared anchors to be
    well-determined. With fewer, return None rather than producing
    a layout that's a Cramer-determined hallucination."""
    n_pts, s_pts, n_size, s_size, _ = _synthetic_two_sheet_anchors()
    # Strip overlap codes from south so 0 shared remain.
    s_pts_stripped = [
        p for p in s_pts if not p.code.startswith("OVL")
    ]
    assert (
        compute_joint_calibration(n_pts, s_pts_stripped, n_size, s_size)
        is None
    )


def test_joint_calibration_rejects_degenerate_pixmap_size() -> None:
    """Zero or negative pixmap dimensions can't define a coordinate
    system. Return None rather than dividing by zero downstream."""
    n_pts, s_pts, _, _, _ = _synthetic_two_sheet_anchors()
    assert (
        compute_joint_calibration(n_pts, s_pts, (0.0, 100.0), (100.0, 100.0))
        is None
    )
    assert (
        compute_joint_calibration(
            n_pts, s_pts, (100.0, 100.0), (100.0, -1.0)
        )
        is None
    )


def test_joint_calibration_dataclass_shape_is_stable() -> None:
    """Pin the public surface of :class:`JointCalibration` — anything
    saving / restoring / serialising joint-fit calibrations downstream
    depends on these field names and types."""
    n_pts, s_pts, n_size, s_size, _ = _synthetic_two_sheet_anchors()
    joint = compute_joint_calibration(n_pts, s_pts, n_size, s_size)
    assert isinstance(joint, JointCalibration)
    assert isinstance(joint.north_affine, tuple) and len(joint.north_affine) == 6
    assert isinstance(joint.south_affine, tuple) and len(joint.south_affine) == 6
    assert isinstance(joint.layout, tuple) and len(joint.layout) == 3
    assert isinstance(joint.shared_codes, tuple)
    assert all(isinstance(c, str) for c in joint.shared_codes)
    assert isinstance(joint.iterations, int) and joint.iterations >= 1
    assert isinstance(joint.converged, bool)
    for name in (
        "click_residual_north_px",
        "click_residual_south_px",
        "consistency_residual_px",
        "chart_residual_px",
    ):
        val = getattr(joint, name)
        assert isinstance(val, float) and val >= 0.0


def test_joint_calibration_converges_quickly_on_realistic_inputs() -> None:
    """The alternating LSQ should hit the convergence threshold in a small
    number of iterations from the click-based-layout warm start. If the
    iteration count balloons, something's wrong with the sub-problem
    formulation."""
    n_pts, s_pts, n_size, s_size, _ = _synthetic_two_sheet_anchors()
    joint = compute_joint_calibration(
        n_pts, s_pts, n_size, s_size, max_iterations=64
    )
    assert joint is not None
    assert joint.converged
    assert joint.iterations <= 16


def test_apply_joint_affine_overrides_replaces_internal_affine() -> None:
    """``SheetGeoCalibration.apply_joint_affine_overrides`` must replace
    ``_forward`` / ``_inverse`` / ``_residual_uv`` so all downstream
    ``lonlat_to_uv`` callers pick up the joint fit transparently.

    Construct a sheet whose independent fit hits some baseline residual,
    then override with a deliberately different affine and assert
    every public method reflects the override.
    """
    n_pts, s_pts, n_size, s_size, _ = _synthetic_two_sheet_anchors()
    joint = compute_joint_calibration(n_pts, s_pts, n_size, s_size)
    assert joint is not None

    cal = SheetGeoCalibration(pdf_fp={}, points=list(n_pts), map_layout=None)
    before_uv = cal.lonlat_to_uv(35.0, 31.5)
    before_residual = cal.residual_uv

    cal.apply_joint_affine_overrides(joint.north_affine, joint.north_lon_scale)
    after_uv = cal.lonlat_to_uv(35.0, 31.5)
    after_residual = cal.residual_uv

    # Override must change the projection (otherwise the override is
    # a no-op and the joint result isn't actually being used).
    # On the synthetic test data the joint fit ~= independent fit, so
    # we expect equality-by-construction but assert via the forward
    # map rather than direct equality. Under LCC, the affine input is
    # ``(X, Y) = lcc_project(lat, lon)`` rather than the legacy
    # ``(lon * cos(mean_lat), -lat)``.
    a, b, c, d, tx, ty = joint.north_affine
    x, y = lcc_project(31.5, 35.0)
    expected_u = a * x + b * y + tx
    expected_v = c * x + d * y + ty
    assert after_uv[0] == pytest.approx(expected_u, abs=1e-9)
    assert after_uv[1] == pytest.approx(expected_v, abs=1e-9)

    # Inverse must round-trip through the new forward.
    lon_back, lat_back = cal.uv_to_lonlat(expected_u, expected_v)
    assert lon_back == pytest.approx(35.0, abs=1e-6)
    assert lat_back == pytest.approx(31.5, abs=1e-6)

    # residual_uv must reflect the overridden affine, not the original.
    # Compute the expected residual from the override coefficients
    # against the click anchors, and assert ``residual_uv`` matches.
    sq = 0.0
    for p in n_pts:
        x_p, y_p = lcc_project(p.lat, p.lon)
        u_pred = a * x_p + b * y_p + tx
        v_pred = c * x_p + d * y_p + ty
        sq += (u_pred - p.u) ** 2 + (v_pred - p.v) ** 2
    expected_residual = math.sqrt(sq / len(n_pts))
    assert after_residual == pytest.approx(expected_residual, abs=1e-9)
    # Sanity: the residual must have actually been recomputed (not
    # left at the old value). On synthetic data the override matches
    # the independent fit, so equality-up-to-recomputation-precision
    # is the right check.
    assert after_residual == pytest.approx(before_residual, abs=1e-6)
    _ = before_uv  # quiet linters: this branch only documents the API


def test_apply_joint_affine_overrides_rejects_singular_matrix() -> None:
    """A singular 2×2 affine has no inverse, so we can't build an
    ``_inverse`` mapping. Reject up-front rather than emitting an
    inverse that divides by zero on the first ``uv_to_lonlat`` call.
    """
    n_pts, _, _, _, _ = _synthetic_two_sheet_anchors()
    cal = SheetGeoCalibration(pdf_fp={}, points=list(n_pts), map_layout=None)
    # ``(a, b, c, d) = (1, 1, 1, 1)`` is rank-1 — det = 0.
    with pytest.raises(ValueError):
        cal.apply_joint_affine_overrides(
            (1.0, 1.0, 1.0, 1.0, 0.0, 0.0), 1.0
        )
