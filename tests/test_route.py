"""Tests for route math (distance, bearing, time) and the Route container —
including intermediate user-click points used to model polyline legs."""

from __future__ import annotations

import math

import pytest

from cvfr_routemaster.route import (
    CVFR_MAX_SPEED_KTS,
    ISRAEL_MAGNETIC_VARIATION_DEG_E,
    Route,
    RoutePoint,
    RouteSegment,
    format_hms,
    format_icao_coord,
    great_circle_distance_nm,
    magnetic_bearing_deg,
    segment_time_seconds,
    to_hebrew_route_string,
    to_icao_route_string,
    true_bearing_deg,
)
from cvfr_routemaster.waypoint_types import WaypointRecord


def _wp(
    code: str,
    lat: float,
    lon: float,
    *,
    idx: int = 0,
    name_he: str = "",
) -> WaypointRecord:
    return WaypointRecord(
        index=idx,
        code=code,
        name_he=name_he,
        reporting_type="",
        lat=lat,
        lon=lon,
        lat_dms="",
        lon_dms="",
    )


# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------


def test_distance_zero_when_same_point():
    assert great_circle_distance_nm(32.0, 34.85, 32.0, 34.85) == pytest.approx(0.0, abs=1e-9)


def test_distance_one_minute_of_latitude_is_one_nm():
    """One arcminute of latitude on a great circle is exactly one nautical mile by
    historical definition; spherical Haversine should reproduce this to ≈ 0.1%."""
    d = great_circle_distance_nm(32.0, 34.85, 32.0 + 1.0 / 60.0, 34.85)
    assert d == pytest.approx(1.0, rel=2e-3)


def test_distance_tlv_to_haifa_known_leg():
    """Sanity check against the well-known TLV→HFA leg (~49 nm)."""
    d = great_circle_distance_nm(32.011, 34.886, 32.809, 35.043)
    assert 47.0 < d < 51.0


def test_distance_is_symmetric():
    a = great_circle_distance_nm(31.5, 34.5, 33.0, 35.5)
    b = great_circle_distance_nm(33.0, 35.5, 31.5, 34.5)
    assert a == pytest.approx(b, rel=1e-12)


# ---------------------------------------------------------------------------
# True & magnetic bearing
# ---------------------------------------------------------------------------


def test_true_bearing_due_north():
    """Going north along a meridian should give 000°T (allow tiny FP slop)."""
    b = true_bearing_deg(32.0, 35.0, 33.0, 35.0)
    assert b == pytest.approx(0.0, abs=1e-6)


def test_true_bearing_due_east_at_equator():
    b = true_bearing_deg(0.0, 35.0, 0.0, 35.5)
    assert b == pytest.approx(90.0, abs=1e-6)


def test_true_bearing_due_south():
    b = true_bearing_deg(33.0, 35.0, 32.0, 35.0)
    assert b == pytest.approx(180.0, abs=1e-6)


def test_true_bearing_zero_for_coincident_points():
    """Bearing is undefined for coincident points; we return 0 as a stable fallback so
    display code never has to deal with NaN."""
    assert true_bearing_deg(32.0, 35.0, 32.0, 35.0) == 0.0


def test_magnetic_bearing_subtracts_east_variation():
    """For Israel's chart-printed +5°E variation, magnetic bearing = true − var."""
    true_n = true_bearing_deg(32.0, 35.0, 33.0, 35.0)
    mag_n = magnetic_bearing_deg(32.0, 35.0, 33.0, 35.0)
    assert true_n == pytest.approx(0.0, abs=1e-6)
    assert mag_n == pytest.approx((360.0 - ISRAEL_MAGNETIC_VARIATION_DEG_E) % 360.0, abs=1e-6)


def test_magnetic_bearing_explicit_zero_variation_matches_true():
    t = true_bearing_deg(32.0, 35.0, 32.5, 35.5)
    m = magnetic_bearing_deg(32.0, 35.0, 32.5, 35.5, magvar_e=0.0)
    assert m == pytest.approx(t, abs=1e-9)


def test_magnetic_bearing_in_range():
    """Result must always live in [0, 360); regression guard for the modulo."""
    for var in (-10.0, 0.0, 4.5, 30.0):
        for (lat1, lon1, lat2, lon2) in [
            (32.0, 35.0, 33.0, 36.0),
            (33.0, 36.0, 32.0, 35.0),
            (32.0, 35.0, 32.0, 36.0),
            (32.0, 35.0, 31.0, 35.0),
        ]:
            b = magnetic_bearing_deg(lat1, lon1, lat2, lon2, magvar_e=var)
            assert 0.0 <= b < 360.0


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def test_segment_time_distance_over_speed():
    """30 nm at 60 kts = 0.5 h = 1800 s."""
    assert segment_time_seconds(30.0, 60.0) == pytest.approx(1800.0, abs=1e-9)


def test_segment_time_zero_speed_returns_zero():
    assert segment_time_seconds(30.0, 0.0) == 0.0
    assert segment_time_seconds(30.0, -10.0) == 0.0


@pytest.mark.parametrize(
    "secs, expected",
    [
        (0.0, "00:00:00"),
        (59.0, "00:00:59"),
        (60.0, "00:01:00"),
        (3599.0, "00:59:59"),
        (3600.0, "01:00:00"),
        (3661.0, "01:01:01"),
        (36000.0, "10:00:00"),
    ],
)
def test_format_hms_canonical_cases(secs, expected):
    assert format_hms(secs) == expected


def test_format_hms_rounds_to_nearest_second():
    assert format_hms(59.4) == "00:00:59"
    assert format_hms(59.6) == "00:01:00"


def test_format_hms_clamps_negative_and_nan():
    assert format_hms(-5.0) == "00:00:00"
    assert format_hms(float("nan")) == "00:00:00"


# ---------------------------------------------------------------------------
# Route container — basic ops with real waypoints
# ---------------------------------------------------------------------------


def test_route_starts_empty():
    r = Route()
    assert len(r) == 0
    assert r.is_empty()
    assert r.segments() == []
    assert r.display_labels() == []


def test_route_append_waypoint_and_segments():
    r = Route()
    a = _wp("AAA", 32.0, 34.85, idx=1)
    b = _wp("BBB", 32.5, 35.0, idx=2)
    c = _wp("CCC", 33.0, 35.0, idx=3)
    assert r.append_waypoint(a)
    assert r.append_waypoint(b)
    assert r.append_waypoint(c)
    assert len(r) == 3
    segs = r.segments()
    assert len(segs) == 2
    assert all(isinstance(s, RouteSegment) for s in segs)
    assert segs[0].from_label == "AAA"
    assert segs[0].to_label == "BBB"
    assert segs[1].from_label == "BBB"
    assert segs[1].to_label == "CCC"
    # All endpoints are real waypoints in this scenario.
    for s in segs:
        assert s.from_point.is_waypoint and s.to_point.is_waypoint
        assert s.distance_nm > 0.0
        assert 0.0 <= s.mag_bearing_deg < 360.0


def test_route_refuses_consecutive_duplicate_waypoint():
    """Clicking the same triangle twice in a row should not insert a 0-length leg."""
    r = Route()
    a = _wp("AAA", 32.0, 34.85)
    assert r.append_waypoint(a)
    assert not r.append_waypoint(a)
    assert len(r) == 1


def test_route_allows_non_consecutive_waypoint_repeat():
    """A route may legitimately revisit a fix (e.g. holding pattern)."""
    r = Route()
    a = _wp("AAA", 32.0, 34.85)
    b = _wp("BBB", 32.5, 35.0)
    assert r.append_waypoint(a)
    assert r.append_waypoint(b)
    assert r.append_waypoint(a)
    assert r.display_labels() == ["AAA", "BBB", "AAA"]


def test_route_remove_at_index():
    r = Route()
    a = _wp("AAA", 32.0, 34.85)
    b = _wp("BBB", 32.5, 35.0)
    c = _wp("CCC", 33.0, 35.0)
    for w in (a, b, c):
        r.append_waypoint(w)
    assert r.remove_at(1)
    assert r.display_labels() == ["AAA", "CCC"]
    assert not r.remove_at(99)
    assert not r.remove_at(-1)


def test_route_nearest_index_returns_closest_within_tolerance():
    r = Route()
    a = _wp("AAA", 32.00, 34.85)
    b = _wp("BBB", 32.50, 35.00)
    r.append_waypoint(a)
    r.append_waypoint(b)
    assert r.nearest_index(32.001, 34.851, max_nm=2.0) == 0
    assert r.nearest_index(32.499, 35.001, max_nm=2.0) == 1


def test_route_nearest_index_returns_none_when_too_far():
    r = Route()
    r.append_waypoint(_wp("AAA", 32.0, 34.85))
    assert r.nearest_index(31.5, 34.0, max_nm=2.0) is None


def test_route_nearest_index_picks_first_of_repeated_waypoints():
    """Non-consecutive duplicates: ``nearest_index`` returns the first match by
    position, which is the deterministic choice the click handler relies on."""
    r = Route()
    a = _wp("AAA", 32.000, 34.850)
    b = _wp("BBB", 32.200, 35.000)
    r.append_waypoint(a)
    r.append_waypoint(b)
    r.append_waypoint(_wp("AAA", 32.000, 34.850))
    idx = r.nearest_index(32.000, 34.850, max_nm=1.0)
    assert idx == 0


def test_route_clear():
    r = Route()
    r.append_waypoint(_wp("AAA", 32.0, 34.85))
    r.append_waypoint(_wp("BBB", 33.0, 35.0))
    r.clear()
    assert r.is_empty()
    assert r.segments() == []
    assert r.display_labels() == []


def test_route_segment_time_from_speed():
    """A segment's time follows from the externally-supplied speed."""
    r = Route()
    a = _wp("AAA", 32.0, 35.0)
    b = _wp("BBB", 33.0, 35.0)  # ~60 nm due north
    r.append_waypoint(a)
    r.append_waypoint(b)
    seg = r.segments()[0]
    # 60 nm at 120 kts = 30 min = 1800 s; allow some tolerance from non-exact distance.
    assert seg.time_seconds(120.0) == pytest.approx(1800.0, rel=2e-2)


# ---------------------------------------------------------------------------
# Route container — intermediate (user-click) points
# ---------------------------------------------------------------------------


def test_intermediate_refused_on_empty_route():
    """Intermediates anchor their display label to the previous real waypoint, so
    they may not be the first point in the route."""
    r = Route()
    assert not r.append_intermediate(32.0, 34.9)
    assert r.is_empty()


def test_intermediate_refused_when_same_coords_as_previous_point():
    """Two clicks at the same coordinates would produce a 0-length leg."""
    r = Route()
    r.append_waypoint(_wp("AAA", 32.0, 34.85))
    assert r.append_intermediate(32.10, 34.95)
    # Second intermediate identical to the first → refused.
    assert not r.append_intermediate(32.10, 34.95)
    assert len(r) == 2


def test_intermediates_label_with_previous_waypoint_ordinal():
    """DAROM, two intermediate clicks, then GALIM → labels DAROM, DAROM.1, DAROM.2,
    GALIM (matches the user's described UX)."""
    r = Route()
    r.append_waypoint(_wp("DAROM", 31.50, 34.50))
    r.append_intermediate(31.55, 34.55)
    r.append_intermediate(31.60, 34.60)
    r.append_waypoint(_wp("GALIM", 31.65, 34.65))
    assert r.display_labels() == ["DAROM", "DAROM.1", "DAROM.2", "GALIM"]


def test_intermediates_ordinal_resets_at_each_real_waypoint():
    """Counter starts at .1 again after a new real waypoint enters the route."""
    r = Route()
    r.append_waypoint(_wp("AAA", 31.0, 34.0))
    r.append_intermediate(31.1, 34.1)
    r.append_waypoint(_wp("BBB", 31.2, 34.2))
    r.append_intermediate(31.3, 34.3)
    r.append_intermediate(31.4, 34.4)
    r.append_waypoint(_wp("CCC", 31.5, 34.5))
    assert r.display_labels() == ["AAA", "AAA.1", "BBB", "BBB.1", "BBB.2", "CCC"]


def test_segments_carry_intermediate_labels_and_endpoint_kinds():
    """Each segment records both labels *and* whether each endpoint is a real
    waypoint — the panel renders ``--> CODE.N`` for intermediates and a clickable
    code for real waypoints, so it needs both pieces of information."""
    r = Route()
    r.append_waypoint(_wp("DAROM", 31.50, 34.50))
    r.append_intermediate(31.55, 34.55)
    r.append_waypoint(_wp("GALIM", 31.65, 34.65))
    segs = r.segments()
    assert len(segs) == 2
    s1, s2 = segs
    assert s1.from_label == "DAROM" and s1.to_label == "DAROM.1"
    assert s1.from_point.is_waypoint is True
    assert s1.to_point.is_waypoint is False
    assert s2.from_label == "DAROM.1" and s2.to_label == "GALIM"
    assert s2.from_point.is_waypoint is False
    assert s2.to_point.is_waypoint is True


def test_intermediate_distance_uses_clicked_coords():
    """Distance for the polyline sub-leg should reflect the intermediate's actual
    coordinates, not anything derived from the surrounding waypoints."""
    r = Route()
    r.append_waypoint(_wp("AAA", 32.0, 35.0))
    r.append_intermediate(32.5, 35.0)  # ~30 nm due north
    r.append_waypoint(_wp("BBB", 33.0, 35.0))  # another ~30 nm due north
    segs = r.segments()
    assert segs[0].distance_nm == pytest.approx(30.0, rel=2e-2)
    assert segs[1].distance_nm == pytest.approx(30.0, rel=2e-2)
    # Total polyline distance = ~60 nm, equal to the chord here only because both
    # legs are on the same meridian — that's intentional, the test demonstrates
    # the *math* is per sub-leg.


def test_remove_intermediate_preserves_other_intermediate_labels_and_renumbers():
    """Removing the second intermediate (DAROM.2) should leave DAROM, DAROM.1,
    GALIM behind. Removing the first should renumber: DAROM, DAROM.1
    (was DAROM.2), GALIM."""
    r = Route()
    r.append_waypoint(_wp("DAROM", 31.50, 34.50))
    r.append_intermediate(31.55, 34.55)
    r.append_intermediate(31.60, 34.60)
    r.append_waypoint(_wp("GALIM", 31.65, 34.65))
    # Remove the SECOND intermediate (index 2, label "DAROM.2").
    assert r.remove_at(2)
    assert r.display_labels() == ["DAROM", "DAROM.1", "GALIM"]
    # Now remove the remaining intermediate (index 1).
    assert r.remove_at(1)
    assert r.display_labels() == ["DAROM", "GALIM"]


def test_remove_real_waypoint_reanchors_following_intermediates():
    """Removing DAROM in [DAROM, DAROM.1, GALIM] re-anchors the intermediate to
    whatever real waypoint precedes it — there's none here, so the fallback
    'VIA' base kicks in. This proves labels are computed at render time, not
    cached."""
    r = Route()
    r.append_waypoint(_wp("DAROM", 31.50, 34.50))
    r.append_intermediate(31.55, 34.55)
    r.append_waypoint(_wp("GALIM", 31.65, 34.65))
    assert r.remove_at(0)  # remove DAROM
    assert r.display_labels() == ["VIA.1", "GALIM"]


def test_nearest_index_finds_intermediate_points_too():
    """Shift+right should be able to remove intermediates as well as waypoints."""
    r = Route()
    r.append_waypoint(_wp("AAA", 32.0, 35.0))
    r.append_intermediate(32.25, 35.0)
    r.append_waypoint(_wp("BBB", 32.5, 35.0))
    # Click essentially on the intermediate's coords.
    idx = r.nearest_index(32.25, 35.0, max_nm=1.0)
    assert idx == 1


def test_route_point_dataclass_is_immutable():
    """``RoutePoint`` is frozen so route mutation goes through ``Route`` methods."""
    p = RoutePoint(lat=32.0, lon=35.0, waypoint=None)
    with pytest.raises(Exception):
        p.lat = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Constants / sanity
# ---------------------------------------------------------------------------


def test_cvfr_speed_limit_is_180():
    assert CVFR_MAX_SPEED_KTS == 180


def test_israel_variation_is_eastward_and_small():
    """Sanity bound — the chart-printed value is currently 5°E (2025 cycle),
    never negative and never larger than a single-digit east drift."""
    assert 0.0 < ISRAEL_MAGNETIC_VARIATION_DEG_E < 10.0


# ---------------------------------------------------------------------------
# ICAO Field 15 coordinate formatting
# ---------------------------------------------------------------------------


def test_icao_coord_israel_canonical_example():
    """Doc 4444 form for an Israeli CVFR-area click: 11 chars total."""
    assert format_icao_coord(31.55, 34.55) == "3133N03433E"
    assert len(format_icao_coord(31.55, 34.55)) == 11


def test_icao_coord_zero_minutes_is_zero_padded():
    """Whole-degree points get 00 in the minutes field, not nothing."""
    assert format_icao_coord(32.0, 35.0) == "3200N03500E"


def test_icao_coord_southern_hemisphere_uses_S():
    assert format_icao_coord(-31.55, 34.55) == "3133S03433E"


def test_icao_coord_western_hemisphere_uses_W():
    assert format_icao_coord(31.55, -34.55) == "3133N03433W"


def test_icao_coord_negative_both_hemispheres():
    """Both signs flip independently — coverage that abs() gates the sign letter
    rather than being tied to the other coordinate."""
    assert format_icao_coord(-31.55, -34.55) == "3133S03433W"


def test_icao_coord_three_digit_longitude_padding():
    """Field 15 always uses 3 digits for longitude degrees, even for small values."""
    assert format_icao_coord(31.0, 7.5) == "3100N00730E"
    assert format_icao_coord(31.0, 175.5) == "3100N17530E"


def test_icao_coord_minute_rounding():
    """Minutes are rounded to nearest whole minute."""
    # 0.5167° = 31'00.12" → rounds to 31'
    assert format_icao_coord(32.5167, 34.5) == "3231N03430E"
    # 0.500° = 30'00" → rounds to 30'
    assert format_icao_coord(32.5, 34.5) == "3230N03430E"


def test_icao_coord_60_minute_carry():
    """Edge case: minutes that round up to 60 must carry into degrees, not emit
    DDDMM = NN60 (which is malformed)."""
    # 31°59.7' → rounds to 60' → carries to 32°00'
    assert format_icao_coord(31.995, 34.995) == "3200N03500E"


def test_icao_coord_zero_zero():
    """Equator + prime meridian — 0/0 should still produce a valid token."""
    assert format_icao_coord(0.0, 0.0) == "0000N00000E"


# ---------------------------------------------------------------------------
# ICAO Field 15 route string
# ---------------------------------------------------------------------------


def test_to_icao_route_string_empty_route():
    assert to_icao_route_string(Route()) == ""


def test_to_icao_route_string_only_waypoints():
    """Route of pure waypoints renders as space-separated codes."""
    r = Route()
    r.append_waypoint(_wp("DAROM", 31.50, 34.50))
    r.append_waypoint(_wp("GALIM", 31.65, 34.65))
    r.append_waypoint(_wp("KANOT", 31.80, 34.80))
    assert to_icao_route_string(r) == "DAROM GALIM KANOT"


def test_to_icao_route_string_mixed_default_includes_intermediates():
    """Default ``include_intermediates=True`` interleaves coords between waypoints."""
    r = Route()
    r.append_waypoint(_wp("DAROM", 31.50, 34.50))
    r.append_intermediate(31.55, 34.55)
    r.append_intermediate(31.60, 34.60)
    r.append_waypoint(_wp("GALIM", 31.65, 34.65))
    s = to_icao_route_string(r)
    assert s == "DAROM 3133N03433E 3136N03436E GALIM"


def test_to_icao_route_string_excluding_intermediates_drops_them():
    """``include_intermediates=False`` produces the 'filed waypoints' string —
    intermediates are dropped, not collapsed to a placeholder."""
    r = Route()
    r.append_waypoint(_wp("DAROM", 31.50, 34.50))
    r.append_intermediate(31.55, 34.55)
    r.append_intermediate(31.60, 34.60)
    r.append_waypoint(_wp("GALIM", 31.65, 34.65))
    assert to_icao_route_string(r, include_intermediates=False) == "DAROM GALIM"


def test_to_icao_route_string_only_intermediates_after_waypoint_removal():
    """Edge case: a single waypoint followed by intermediates whose anchor was
    removed. The string still composes — orphan intermediates are valid Field 15
    tokens (just coords)."""
    r = Route()
    r.append_waypoint(_wp("DAROM", 31.50, 34.50))
    r.append_intermediate(31.55, 34.55)
    r.append_intermediate(31.60, 34.60)
    r.remove_at(0)  # drop DAROM, leaving two orphan intermediates
    assert to_icao_route_string(r) == "3133N03433E 3136N03436E"
    assert to_icao_route_string(r, include_intermediates=False) == ""


def test_to_icao_route_string_single_waypoint():
    r = Route()
    r.append_waypoint(_wp("DAROM", 31.50, 34.50))
    assert to_icao_route_string(r) == "DAROM"
    assert to_icao_route_string(r, include_intermediates=False) == "DAROM"


# ---------------------------------------------------------------------------
# Hebrew route string (Israeli flight-plan / flight-school paperwork view)
# ---------------------------------------------------------------------------


def test_to_hebrew_route_string_empty_route():
    assert to_hebrew_route_string(Route()) == ""


def test_to_hebrew_route_string_uses_hebrew_names_for_real_waypoints():
    """Real waypoints render with ``name_he``; intermediates use the same ICAO
    coordinate token as the ICAO row so a leg's geometry stays comparable
    line-to-line."""
    r = Route()
    r.append_waypoint(_wp("DAROM", 31.50, 34.50, name_he="דרום"))
    r.append_intermediate(31.55, 34.55)
    r.append_waypoint(_wp("GALIM", 31.65, 34.65, name_he="גלים"))
    assert to_hebrew_route_string(r) == "דרום 3133N03433E גלים"


def test_to_hebrew_route_string_falls_back_to_code_when_name_he_missing():
    """If the back-pages OCR didn't recover a Hebrew name, surfacing the ICAO
    code keeps the route string complete instead of silently dropping a fix."""
    r = Route()
    r.append_waypoint(_wp("DAROM", 31.50, 34.50, name_he=""))
    r.append_waypoint(_wp("GALIM", 31.65, 34.65, name_he="גלים"))
    assert to_hebrew_route_string(r) == "DAROM גלים"


def test_to_hebrew_route_string_excluding_intermediates_drops_them():
    """Intermediate-coords toggle must drop both rows the same way — the panel
    presents one checkbox governing both ICAO and Hebrew strings."""
    r = Route()
    r.append_waypoint(_wp("DAROM", 31.50, 34.50, name_he="דרום"))
    r.append_intermediate(31.55, 34.55)
    r.append_intermediate(31.60, 34.60)
    r.append_waypoint(_wp("GALIM", 31.65, 34.65, name_he="גלים"))
    assert to_hebrew_route_string(r, include_intermediates=False) == "דרום גלים"


def test_to_hebrew_route_string_only_intermediates_after_waypoint_removal():
    """Same orphan-intermediate edge case as the ICAO formatter."""
    r = Route()
    r.append_waypoint(_wp("DAROM", 31.50, 34.50, name_he="דרום"))
    r.append_intermediate(31.55, 34.55)
    r.append_intermediate(31.60, 34.60)
    r.remove_at(0)
    assert to_hebrew_route_string(r) == "3133N03433E 3136N03436E"
    assert to_hebrew_route_string(r, include_intermediates=False) == ""


def test_to_hebrew_route_string_single_waypoint():
    r = Route()
    r.append_waypoint(_wp("DAROM", 31.50, 34.50, name_he="דרום"))
    assert to_hebrew_route_string(r) == "דרום"
    assert to_hebrew_route_string(r, include_intermediates=False) == "דרום"
