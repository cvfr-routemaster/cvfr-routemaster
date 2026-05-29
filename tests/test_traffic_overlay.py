"""Tests for :mod:`cvfr_routemaster.traffic_overlay` — the on-chart
VATSIM traffic rendering layer.

The overlay has three layers worth testing in isolation:

1. **Visual encoding tables** — :data:`WAKE_COLOR` and
   :data:`WAKE_SCALE` cover all five wake categories and the
   ``unknown`` fallback. Pure dict lookups, no Qt/scene needed.
2. **Silhouette geometry** — the normalised airplane path returned
   by ``_build_silhouette_path`` should be non-empty, closed,
   bounded by ``(±0.5, ±0.5)``, and produce a sensible bounding
   box. No painting required.
3. **Manager lifecycle** — :class:`TrafficOverlay` adding/removing
   :class:`_TrafficPlaneItem` instances on a real
   :class:`QGraphicsScene` based on the projection callback's
   results. This needs a ``QApplication`` but no actual rendering
   (we never show a window).

We intentionally don't trigger Qt's painting pipeline in these
tests — paint() takes a live QPainter that comes from a render
target, and validating the actual pixels is a job for visual
regression testing, not unit tests.
"""

from __future__ import annotations

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QPointF, QSettings  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QGraphicsItem,
    QGraphicsScene,
)

from cvfr_routemaster import settings_store  # noqa: E402
from cvfr_routemaster.settings_store import (  # noqa: E402
    load_show_vatsim_traffic,
    save_show_vatsim_traffic,
)
from cvfr_routemaster.traffic_overlay import (  # noqa: E402
    TRAFFIC_OVERLAY_Z,
    WAKE_COLOR,
    WAKE_SCALE,
    TrafficOverlay,
    _build_silhouette_path,
    _format_altitude_label,
    _format_bottom_line,
    _format_speed_label,
    _format_top_line,
    _format_tooltip,
    _GROUND_SPEED_THRESHOLDS_KT,
    _is_on_ground,
    _SILHOUETTE_PATH,
    _TrafficPlaneItem,
)
from cvfr_routemaster.vatsim_feed import WAKE_UNKNOWN, Pilot  # noqa: E402


# --- Module-level fixtures ----------------------------------------------


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """One QApplication per process — Qt enforces this. Without a
    QApplication any ``QGraphicsScene`` mutation segfaults on some
    platforms because Qt's resource-system isn't initialised.
    """
    app = QApplication.instance() or QApplication([])
    return app


def _make_pilot(
    *,
    callsign: str = "TEST01",
    cid: int = 1,
    lat: float = 32.0,
    lon: float = 35.0,
    altitude_ft: int = 10000,
    groundspeed_kts: int = 200,
    heading_deg: int = 90,
    aircraft_type: str | None = "B738",
    wake: str = "M",
    flight_rules: str = "I",
    departure: str = "LLBG",
    arrival: str = "LCLK",
) -> Pilot:
    """Build a Pilot with sensible defaults for tests that only
    care about one or two fields. Keeps each test focused on its
    actual assertion rather than spelling out 13 unchanging
    dataclass fields."""
    return Pilot(
        cid=cid,
        callsign=callsign,
        name="Test Pilot",
        lat=lat,
        lon=lon,
        altitude_ft=altitude_ft,
        groundspeed_kts=groundspeed_kts,
        heading_deg=heading_deg,
        transponder="2000",
        aircraft_type=aircraft_type,
        wake=wake,
        flight_rules=flight_rules,
        departure=departure,
        arrival=arrival,
    )


# --- Wake-encoding tables -----------------------------------------------


class TestWakeColors:
    """The colour palette is the user-visible identity of each
    wake category — these tests guard against accidental palette
    edits that would silently break the visual encoding."""

    def test_covers_all_five_wake_categories(self) -> None:
        assert set(WAKE_COLOR.keys()) == {"L", "M", "H", "J", WAKE_UNKNOWN}

    def test_each_color_is_distinct(self) -> None:
        """No two wake categories should share an RGB triple — a
        clash would make pilots visually indistinguishable, which
        is the whole point of having different colours per
        category."""
        rgbs = {(c.red(), c.green(), c.blue()) for c in WAKE_COLOR.values()}
        assert len(rgbs) == 5

    def test_alpha_is_translucent_but_visible(self) -> None:
        """Alpha lives in the 200-250 band: high enough that the
        silhouette reads as solid, low enough that overlapping the
        route polyline doesn't completely hide either layer."""
        for color in WAKE_COLOR.values():
            assert 200 <= color.alpha() <= 250

    def test_unknown_color_is_desaturated_gray(self) -> None:
        """The ``unknown`` colour should read as "we don't know"
        rather than "we know it's a Medium". Equal RGB channels
        produce a neutral gray; we check that explicitly."""
        c = WAKE_COLOR[WAKE_UNKNOWN]
        assert c.red() == c.green() == c.blue()


class TestWakeScales:
    """Per-class size multipliers reinforce the colour cue: lights
    draw smaller, heavies bigger, Super biggest. The tests pin the
    *relative* ordering (L < M < H < J) rather than the exact
    numbers — a future tuning pass can shift the values without
    breaking tests, as long as the ordering stays right."""

    def test_covers_all_five_wake_categories(self) -> None:
        assert set(WAKE_SCALE.keys()) == {"L", "M", "H", "J", WAKE_UNKNOWN}

    def test_medium_is_baseline(self) -> None:
        """``M`` is the user's "typical airliner" reference; the
        icon-size setting expresses pixel-size for a Medium and
        every other class scales relative to it."""
        assert WAKE_SCALE["M"] == 1.0

    def test_unknown_is_baseline(self) -> None:
        """An unfiled-plan pilot defaults to Medium-sized — we have
        no information that says they're particularly small or
        large, so neutral sizing matches the neutral colour."""
        assert WAKE_SCALE[WAKE_UNKNOWN] == 1.0

    def test_relative_size_ordering(self) -> None:
        """Light < Medium < Heavy < Super. The visual cue should
        agree with the physical reality of wake-category size."""
        assert WAKE_SCALE["L"] < WAKE_SCALE["M"]
        assert WAKE_SCALE["M"] < WAKE_SCALE["H"]
        assert WAKE_SCALE["H"] < WAKE_SCALE["J"]


# --- Silhouette geometry ------------------------------------------------


class TestSilhouettePath:
    """Geometry of the airplane silhouette as built by
    ``_build_silhouette_path``. We don't validate the exact
    polygon — that's a visual judgement — but we pin enough
    invariants that a future refactor of the path-building code
    can't silently break the overall shape."""

    def test_path_is_not_empty(self) -> None:
        """An empty path would render nothing — sanity check that
        the construction actually emitted line segments."""
        path = _build_silhouette_path()
        assert not path.isEmpty()
        assert path.elementCount() > 5

    def test_module_level_singleton_matches_builder(self) -> None:
        """The module-level ``_SILHOUETTE_PATH`` is built once at
        import. Re-running the builder must produce a path with
        the same shape so tests using either form are equivalent."""
        rebuilt = _build_silhouette_path()
        assert _SILHOUETTE_PATH.elementCount() == rebuilt.elementCount()
        # Bounding rect comparison: same shape, same bbox.
        a = _SILHOUETTE_PATH.boundingRect()
        b = rebuilt.boundingRect()
        assert abs(a.x() - b.x()) < 1e-9
        assert abs(a.y() - b.y()) < 1e-9
        assert abs(a.width() - b.width()) < 1e-9
        assert abs(a.height() - b.height()) < 1e-9

    def test_bounded_by_unit_square(self) -> None:
        """All vertices must sit inside ``(±0.5, ±0.5)`` so the
        normalisation contract holds — callers scale the path
        by their desired pixel size assuming a 1.0×1.0 source.
        A vertex outside this box would draw past the bounding
        rect of every plane item."""
        rect = _SILHOUETTE_PATH.boundingRect()
        assert -0.5 <= rect.left() <= 0.5
        assert -0.5 <= rect.right() <= 0.5
        assert -0.5 <= rect.top() <= 0.5
        assert -0.5 <= rect.bottom() <= 0.5

    def test_nose_is_north_of_tail(self) -> None:
        """The path is drawn with +Y pointing aft (south, in
        compass terms with heading 0 = north). The nose's Y
        coordinate (top of the silhouette in the local frame)
        must therefore be more negative than the tail's. This is
        the contract that makes ``setRotation(heading_deg)`` line
        up with VATSIM's standard heading convention."""
        rect = _SILHOUETTE_PATH.boundingRect()
        assert rect.top() < rect.bottom()

    def test_path_is_horizontally_symmetric_in_extremes(self) -> None:
        """Wingtips on left and right should reach the same
        absolute X — an asymmetric silhouette would visually
        bias every plane to one side. Extremes (the leftmost and
        rightmost X across all path elements) must mirror around
        zero within floating-point tolerance."""
        xs = [
            _SILHOUETTE_PATH.elementAt(i).x
            for i in range(_SILHOUETTE_PATH.elementCount())
        ]
        assert abs(min(xs) + max(xs)) < 1e-6


# --- Tooltip text -------------------------------------------------------


class TestTooltipFormatting:
    """Tooltip is the one piece of pilot detail visible without
    clicking — the formatting rules are user-facing and worth
    pinning."""

    def test_includes_callsign_first(self) -> None:
        """First line is callsign on its own — easiest to read at
        a glance when the tooltip pops up."""
        pilot = _make_pilot(callsign="ELY323")
        text = _format_tooltip(pilot)
        assert text.startswith("ELY323\n")

    def test_includes_aircraft_type_and_wake(self) -> None:
        pilot = _make_pilot(aircraft_type="B738", wake="M")
        assert "B738 · M" in _format_tooltip(pilot)

    def test_marks_missing_aircraft_type_as_no_flight_plan(self) -> None:
        """Pilots without a filed flight plan have
        ``aircraft_type=None``; the tooltip should reflect that
        fact rather than rendering ``None · unknown``."""
        pilot = _make_pilot(aircraft_type=None, wake=WAKE_UNKNOWN)
        text = _format_tooltip(pilot)
        assert "(no flight plan)" in text

    def test_kinematics_line_includes_alt_gs_hdg(self) -> None:
        pilot = _make_pilot(altitude_ft=28000, groundspeed_kts=420, heading_deg=87)
        text = _format_tooltip(pilot)
        assert "ALT 28000 ft" in text
        assert "GS 420 kt" in text
        # Heading is zero-padded to three digits.
        assert "HDG 087°" in text

    def test_route_line_skipped_when_no_flight_plan(self) -> None:
        """An IFR flight has DEP→ARR; a VFR pilot with no plan
        filed has neither, and the tooltip should omit the route
        line entirely rather than showing ``? → ?``."""
        pilot = _make_pilot(departure="", arrival="")
        text = _format_tooltip(pilot)
        assert "→" not in text

    def test_route_line_shows_question_mark_for_partial(self) -> None:
        """Partial flight plans (only DEP filed, only ARR filed)
        are rare but should not crash; the missing side renders
        as ``?``."""
        pilot = _make_pilot(departure="LLBG", arrival="")
        assert "LLBG → ?" in _format_tooltip(pilot)


# --- Altitude label formatting -----------------------------------------


class TestAltitudeLabel:
    """The altitude readout drawn beneath the callsign on the
    chart. Two formats with sharply different conventions, so the
    formatter is worth pinning thoroughly:

    * VFR: raw foot integer with no zero-padding (so ``800ft``,
      not ``00800ft``).
    * IFR (and mixed Y/Z): always three-digit flight level
      (``FL050``, not ``FL50``)."""

    def test_vfr_uses_foot_integer_without_padding(self) -> None:
        pilot = _make_pilot(altitude_ft=2500, flight_rules="V")
        assert _format_altitude_label(pilot) == "2500ft"

    def test_vfr_low_altitude_no_leading_zeros(self) -> None:
        """The whole point of the no-padding rule: 800 ft is
        ``800ft``, not ``00800ft`` or ``800.0ft``."""
        pilot = _make_pilot(altitude_ft=800, flight_rules="V")
        assert _format_altitude_label(pilot) == "800ft"

    def test_vfr_zero_altitude(self) -> None:
        """VATSIM occasionally reports 0 ft for parked aircraft;
        the formatter shouldn't choke."""
        pilot = _make_pilot(altitude_ft=0, flight_rules="V")
        assert _format_altitude_label(pilot) == "0ft"

    def test_vfr_negative_altitude_clamped_to_zero(self) -> None:
        """VATSIM can briefly emit negative altitudes for
        aircraft below their reported reference plane (rare data
        glitch). Clamp to 0 rather than show ``-100ft`` and
        confuse the reader."""
        pilot = _make_pilot(altitude_ft=-100, flight_rules="V")
        assert _format_altitude_label(pilot) == "0ft"

    def test_ifr_uses_three_digit_flight_level(self) -> None:
        pilot = _make_pilot(altitude_ft=28000, flight_rules="I")
        assert _format_altitude_label(pilot) == "FL280"

    def test_ifr_low_flight_level_zero_pads_to_three_digits(self) -> None:
        """The flight-level convention is *always* three digits.
        FL50 (5,000 ft) renders as ``FL050``."""
        pilot = _make_pilot(altitude_ft=5000, flight_rules="I")
        assert _format_altitude_label(pilot) == "FL050"

    def test_ifr_uses_floor_division_not_rounding(self) -> None:
        """Per the VATSIM convention, 31,350 ft is FL313 (floor),
        not FL314 (round). Pin the floor-divide so a future
        refactor doesn't subtly change the rule."""
        pilot = _make_pilot(altitude_ft=31350, flight_rules="I")
        assert _format_altitude_label(pilot) == "FL313"

    def test_ifr_high_flight_level_three_digits(self) -> None:
        pilot = _make_pilot(altitude_ft=40000, flight_rules="I")
        assert _format_altitude_label(pilot) == "FL400"

    def test_mixed_y_uses_flight_level(self) -> None:
        """``Y`` = IFR-then-VFR mid-flight. Y plans always file a
        flight-level portion and ATC reads them at flight levels,
        so FL is the right default."""
        pilot = _make_pilot(altitude_ft=18000, flight_rules="Y")
        assert _format_altitude_label(pilot) == "FL180"

    def test_mixed_z_uses_flight_level(self) -> None:
        """``Z`` = VFR-then-IFR mid-flight. Same rationale as Y —
        the upper portion is filed at FL."""
        pilot = _make_pilot(altitude_ft=22000, flight_rules="Z")
        assert _format_altitude_label(pilot) == "FL220"

    def test_unknown_flight_rules_with_filed_plan_defaults_to_flight_level(
        self,
    ) -> None:
        """A plan that WAS filed but carries an empty / non-canonical
        flight-rules string is still treated as IFR for label
        purposes — the explicit ``"V"`` is the only string that
        triggers the foot-altitude path within the filed-plan
        branch. (The no-plan branch is a separate test; the
        ``aircraft_type`` is non-None here so that branch
        doesn't kick in.)"""
        pilot = _make_pilot(
            altitude_ft=15000, flight_rules="", aircraft_type="B738"
        )
        assert _format_altitude_label(pilot) == "FL150"

    def test_no_flight_plan_filed_uses_feet(self) -> None:
        """The bug-fix case from the live VATSIM feed: a GA
        pilot at 2,891 ft with ``flight_plan: null`` (which the
        parser flattens to ``aircraft_type=None`` +
        ``flight_rules=""``) was previously rendering as
        ``FL028`` — jarring next to airliners in real flight
        levels. After the fix, the label shows raw feet.
        """
        pilot = _make_pilot(
            altitude_ft=2891,
            aircraft_type=None,
            flight_rules="",
            wake=WAKE_UNKNOWN,
        )
        assert _format_altitude_label(pilot) == "2891ft"

    def test_no_flight_plan_filed_uses_feet_even_at_airliner_altitude(
        self,
    ) -> None:
        """Defensive: no-plan branch is keyed on ``aircraft_type
        is None``, not on altitude. A no-plan pilot somehow
        cruising at FL280 should still show ``28000ft`` — the
        label honestly reflects "we don't know what this aircraft
        is, treat the altitude as a raw value". Showing
        ``FL280`` would imply the pilot is in the FL system,
        which a no-plan flight isn't."""
        pilot = _make_pilot(
            altitude_ft=28000,
            aircraft_type=None,
            flight_rules="",
            wake=WAKE_UNKNOWN,
        )
        assert _format_altitude_label(pilot) == "28000ft"

    def test_lowercase_v_treated_as_vfr(self) -> None:
        """Defensive: VATSIM occasionally emits lowercase rules.
        We normalise to uppercase before the binary check so a
        ``"v"`` plan still renders in feet."""
        pilot = _make_pilot(altitude_ft=3500, flight_rules="v")
        assert _format_altitude_label(pilot) == "3500ft"

    def test_on_ground_overrides_vfr_format(self) -> None:
        """A VFR pilot below the ground threshold reads as
        ``GRND``, not ``Nft``. The override is at the *top* of
        the formatter so a parked Cessna doesn't show ``0ft`` —
        it shows ``GRND`` to match the icon's visible
        non-motion."""
        pilot = _make_pilot(
            altitude_ft=0, groundspeed_kts=0, wake="L", flight_rules="V"
        )
        assert _format_altitude_label(pilot) == "GRND"

    def test_on_ground_overrides_ifr_format(self) -> None:
        """A parked airliner with an IFR plan should also read
        as ``GRND`` rather than ``FL000`` — the latter reads
        misleadingly as "at the surface in controlled airspace"
        next to a clearly-stationary icon."""
        pilot = _make_pilot(
            altitude_ft=0, groundspeed_kts=0, wake="M", flight_rules="I"
        )
        assert _format_altitude_label(pilot) == "GRND"

    def test_taxiing_below_threshold_shows_grnd(self) -> None:
        """Taxiing aircraft (low GS, possibly some altitude noise)
        should still read GRND. We don't try to second-guess with
        altitude — the user's spec is purely groundspeed-based."""
        pilot = _make_pilot(
            altitude_ft=50, groundspeed_kts=15, wake="M", flight_rules="I"
        )
        assert _format_altitude_label(pilot) == "GRND"

    def test_just_above_threshold_shows_normal_format(self) -> None:
        """Right at the threshold edge: a plane *exactly* at the
        threshold or above counts as airborne. Pin the strict
        less-than comparison so the edge case is unambiguous."""
        pilot = _make_pilot(
            altitude_ft=2500,
            groundspeed_kts=_GROUND_SPEED_THRESHOLDS_KT["M"],
            wake="M",
            flight_rules="V",
        )
        assert _format_altitude_label(pilot) == "2500ft"


# --- Ground detection --------------------------------------------------


class TestGroundDetection:
    """Per-wake-category groundspeed thresholds for "on the
    ground" classification. These are the central source of
    truth for ground/air state on the chart, so worth pinning
    each category and the boundary behaviour."""

    def test_thresholds_cover_all_wake_categories(self) -> None:
        assert set(_GROUND_SPEED_THRESHOLDS_KT.keys()) == {
            "L",
            "M",
            "H",
            "J",
            WAKE_UNKNOWN,
        }

    def test_thresholds_scale_with_wake_class(self) -> None:
        """Stall speeds rise with aircraft weight, and so do our
        thresholds. L < M < H < J — anything else would
        misclassify high-speed taxiing on the heavies."""
        assert (
            _GROUND_SPEED_THRESHOLDS_KT["L"]
            < _GROUND_SPEED_THRESHOLDS_KT["M"]
            < _GROUND_SPEED_THRESHOLDS_KT["H"]
            < _GROUND_SPEED_THRESHOLDS_KT["J"]
        )

    def test_unknown_uses_lightweight_threshold(self) -> None:
        """A pilot without a filed plan is almost always a small
        GA aircraft — using the lightweight threshold matches
        reality more often than treating them like an airliner."""
        assert (
            _GROUND_SPEED_THRESHOLDS_KT[WAKE_UNKNOWN]
            == _GROUND_SPEED_THRESHOLDS_KT["L"]
        )

    def test_light_below_threshold_is_on_ground(self) -> None:
        pilot = _make_pilot(groundspeed_kts=30, wake="L")
        assert _is_on_ground(pilot) is True

    def test_light_at_threshold_is_airborne(self) -> None:
        """Strict less-than: at the threshold value we consider
        the plane airborne. The threshold itself is the lowest
        speed at which the plane *might* be flying, so equality
        rounds toward "in the air"."""
        pilot = _make_pilot(
            groundspeed_kts=_GROUND_SPEED_THRESHOLDS_KT["L"], wake="L"
        )
        assert _is_on_ground(pilot) is False

    def test_light_above_threshold_is_airborne(self) -> None:
        pilot = _make_pilot(groundspeed_kts=120, wake="L")
        assert _is_on_ground(pilot) is False

    def test_medium_below_threshold_is_on_ground(self) -> None:
        pilot = _make_pilot(groundspeed_kts=80, wake="M")
        assert _is_on_ground(pilot) is True

    def test_medium_above_threshold_is_airborne(self) -> None:
        pilot = _make_pilot(groundspeed_kts=420, wake="M")
        assert _is_on_ground(pilot) is False

    def test_heavy_below_threshold_is_on_ground(self) -> None:
        """A 747 at 110 kt is still rolling. Thresholds for
        heavies are higher than for medium because the heavy's
        rotation speed is closer to V2 (~140 kt)."""
        pilot = _make_pilot(groundspeed_kts=110, wake="H")
        assert _is_on_ground(pilot) is True

    def test_heavy_above_threshold_is_airborne(self) -> None:
        pilot = _make_pilot(groundspeed_kts=480, wake="H")
        assert _is_on_ground(pilot) is False

    def test_super_below_threshold_is_on_ground(self) -> None:
        pilot = _make_pilot(groundspeed_kts=130, wake="J")
        assert _is_on_ground(pilot) is True

    def test_super_above_threshold_is_airborne(self) -> None:
        pilot = _make_pilot(groundspeed_kts=480, wake="J")
        assert _is_on_ground(pilot) is False

    def test_unknown_wake_uses_unknown_threshold(self) -> None:
        """A pilot with no filed plan (wake='unknown') gets the
        lightweight threshold. A 4XCAL squawking 1200 at 30 kt
        is almost certainly taxiing."""
        pilot = _make_pilot(groundspeed_kts=30, wake=WAKE_UNKNOWN)
        assert _is_on_ground(pilot) is True

    def test_zero_groundspeed_is_always_on_ground(self) -> None:
        """A plane reporting 0 kt is always classed as on ground,
        regardless of wake category — every threshold is
        greater than zero by construction."""
        for wake in ("L", "M", "H", "J", WAKE_UNKNOWN):
            pilot = _make_pilot(groundspeed_kts=0, wake=wake)
            assert _is_on_ground(pilot) is True, f"{wake} at 0 kt should be on ground"

    def test_unrecognised_wake_falls_back_to_unknown_threshold(self) -> None:
        """A wake string that isn't in the canonical set should
        fall back to the unknown threshold rather than crashing
        on a missing key. Defensive contract for forward-
        compatibility with future wake additions."""
        pilot = _make_pilot(groundspeed_kts=30, wake="XX_NOT_A_REAL_CATEGORY")
        assert _is_on_ground(pilot) is True


# --- Speed label formatting --------------------------------------------


class TestSpeedLabel:
    """The groundspeed component of the bottom line of the
    on-chart label. Format is ``<kt>kt`` with no separator
    and no prefix — see :func:`_format_speed_label` for the
    rationale."""

    def test_formats_speed_with_kt_suffix(self) -> None:
        pilot = _make_pilot(groundspeed_kts=420)
        assert _format_speed_label(pilot) == "420kt"

    def test_zero_speed(self) -> None:
        """A parked plane reads as ``0kt`` — informative
        ("not currently moving") and gives the bottom line a
        consistent two-component shape regardless of motion
        state (``GRND/0kt`` for parked, ``GRND/12kt`` for
        taxiing, ``FL280/420kt`` for cruising)."""
        pilot = _make_pilot(groundspeed_kts=0)
        assert _format_speed_label(pilot) == "0kt"

    def test_low_speed_without_padding(self) -> None:
        """No zero-padding: 12 kt taxiing reads as ``12kt``,
        not ``012kt``. Same convention as the foot-altitude
        formatter."""
        pilot = _make_pilot(groundspeed_kts=12)
        assert _format_speed_label(pilot) == "12kt"

    def test_high_speed(self) -> None:
        pilot = _make_pilot(groundspeed_kts=515)
        assert _format_speed_label(pilot) == "515kt"

    def test_negative_speed_clamped_to_zero(self) -> None:
        """VATSIM occasionally emits glitchy negative
        groundspeeds; clamping to 0 avoids ``-1kt`` confusion."""
        pilot = _make_pilot(groundspeed_kts=-5)
        assert _format_speed_label(pilot) == "0kt"


# --- Composed top line: <callsign>/<icao_type> --------------------------


class TestFormatTopLine:
    """Top line of the on-chart label —
    ``<callsign>/<icao_type_code>`` with a graceful fallback
    when no aircraft type is filed. See
    :func:`_format_top_line` for the rationale on the
    no-type fallback."""

    def test_includes_callsign_and_type_when_both_present(self) -> None:
        pilot = _make_pilot(callsign="ELY323", aircraft_type="B738")
        assert _format_top_line(pilot) == "ELY323/B738"

    def test_omits_separator_when_aircraft_type_is_none(self) -> None:
        """No filed plan → no aircraft type. We render just the
        callsign rather than ``ELY323/?`` because the unknown-
        wake colour already conveys "no plan filed" and the
        bare callsign is the cleanest fallback at small icon
        sizes."""
        pilot = _make_pilot(callsign="4XBLG", aircraft_type=None)
        assert _format_top_line(pilot) == "4XBLG"

    def test_omits_separator_when_aircraft_type_is_empty(self) -> None:
        """Defensive: VATSIM occasionally emits an empty string
        rather than null for missing fields. Treat ``""`` the
        same as ``None``."""
        pilot = _make_pilot(callsign="4XBLG", aircraft_type="")
        assert _format_top_line(pilot) == "4XBLG"

    def test_strips_whitespace_around_aircraft_type(self) -> None:
        """Some upstreams pad short ICAO codes with trailing
        spaces. Strip so we don't render ``"ELY323/B738 "`` and
        ruin the right-aligned bounding rect."""
        pilot = _make_pilot(callsign="ELY323", aircraft_type="  B738  ")
        assert _format_top_line(pilot) == "ELY323/B738"

    def test_whitespace_only_type_is_treated_as_missing(self) -> None:
        pilot = _make_pilot(callsign="4XBLG", aircraft_type="   ")
        assert _format_top_line(pilot) == "4XBLG"

    def test_super_heavy_type_renders_with_slash(self) -> None:
        pilot = _make_pilot(callsign="UAE201", aircraft_type="A388")
        assert _format_top_line(pilot) == "UAE201/A388"


# --- Composed bottom line: <alt|FL|GRND>/<speed> -----------------------


class TestFormatBottomLine:
    """Bottom line of the on-chart label, composed from the
    altitude formatter and the speed formatter joined by
    ``/``. Each component carries its own unit suffix so
    the slash is purely a visual divider."""

    def test_combines_flight_level_and_speed(self) -> None:
        pilot = _make_pilot(
            altitude_ft=28000, groundspeed_kts=420, flight_rules="I"
        )
        assert _format_bottom_line(pilot) == "FL280/420kt"

    def test_combines_vfr_altitude_and_speed(self) -> None:
        # Wake="L" threshold is 50 kt, so 85 kt cleanly registers
        # as airborne — the GRND override from
        # :func:`_is_on_ground` only fires below the per-wake
        # threshold, and we want this test focused on the
        # altitude branch, not the ground heuristic.
        pilot = _make_pilot(
            altitude_ft=2500,
            groundspeed_kts=85,
            wake="L",
            flight_rules="V",
        )
        assert _format_bottom_line(pilot) == "2500ft/85kt"

    def test_grnd_when_on_ground_keeps_speed_component(self) -> None:
        """A parked plane shows ``GRND/0kt`` — the GRND override
        on the altitude side and the literal 0 on the speed
        side together convey "on the ground and not moving"."""
        pilot = _make_pilot(
            groundspeed_kts=0, wake="M", flight_rules="I"
        )
        assert _format_bottom_line(pilot) == "GRND/0kt"

    def test_grnd_distinguishes_taxi_from_parked(self) -> None:
        """A taxiing plane shows ``GRND/12kt`` — same GRND
        prefix as parked, but the speed component lets the
        viewer tell movement from stillness without having to
        glance back at the icon."""
        pilot = _make_pilot(
            groundspeed_kts=12, wake="M", flight_rules="I"
        )
        assert _format_bottom_line(pilot) == "GRND/12kt"

    def test_uses_altitude_formatter_branches(self) -> None:
        """The bottom line is just the existing altitude and
        speed formatters joined by ``/``. Sanity-check the
        composition: the altitude branch is whichever the
        altitude formatter would have picked alone."""
        ifr = _make_pilot(
            altitude_ft=8000, groundspeed_kts=200, flight_rules="I"
        )
        vfr = _make_pilot(
            altitude_ft=4500, groundspeed_kts=120, flight_rules="V"
        )
        assert _format_bottom_line(ifr).startswith(
            _format_altitude_label(ifr) + "/"
        )
        assert _format_bottom_line(vfr).startswith(
            _format_altitude_label(vfr) + "/"
        )


# --- _TrafficPlaneItem properties --------------------------------------


class TestTrafficPlaneItem:
    """The single plane item is a custom ``QGraphicsItem``. Several
    of its attributes are *contract* with the rest of the app
    (mouse-button policy, z-value, transformation flag) — pin them."""

    def test_ignores_view_transformations(self, qapp) -> None:  # noqa: ARG002
        """The whole point of the silhouette being constant
        screen-pixel size is the
        :attr:`ItemIgnoresTransformations` flag. Without it, a
        zoomed-in chart would inflate every plane until none fit
        on screen."""
        item = _TrafficPlaneItem(_make_pilot(), icon_size_px=24)
        assert item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations

    def test_does_not_accept_mouse_buttons(self, qapp) -> None:  # noqa: ARG002
        """Plane items are purely informational; a click on a
        plane must pass through to the chart so route add/remove
        and sheet selection still work."""
        from PySide6.QtCore import Qt

        item = _TrafficPlaneItem(_make_pilot(), icon_size_px=24)
        assert item.acceptedMouseButtons() == Qt.MouseButton.NoButton

    def test_z_value_is_above_route_overlay(self, qapp) -> None:  # noqa: ARG002
        """Z 200 sits above the route polyline (z=100) and origin
        marker (z=101) so a plane crossing its own route is
        visible on top rather than hidden."""
        item = _TrafficPlaneItem(_make_pilot(), icon_size_px=24)
        assert item.zValue() == TRAFFIC_OVERLAY_Z
        assert item.zValue() > 101.0

    def test_tooltip_includes_callsign(self, qapp) -> None:  # noqa: ARG002
        item = _TrafficPlaneItem(_make_pilot(callsign="UAL801"), icon_size_px=24)
        assert "UAL801" in item.toolTip()

    def test_size_scales_with_wake_category(self, qapp) -> None:  # noqa: ARG002
        """Internal ``_size`` should equal ``icon_size_px ×
        WAKE_SCALE[wake]``. Pinning this keeps the visual-encoding
        contract intact across the icon-size dialog and the future
        Ctrl+wheel-on-plane resizer."""
        light = _TrafficPlaneItem(_make_pilot(wake="L"), icon_size_px=20)
        medium = _TrafficPlaneItem(_make_pilot(wake="M"), icon_size_px=20)
        super_ = _TrafficPlaneItem(_make_pilot(wake="J"), icon_size_px=20)
        assert light._size == pytest.approx(20 * WAKE_SCALE["L"])
        assert medium._size == pytest.approx(20.0)
        assert super_._size == pytest.approx(20 * WAKE_SCALE["J"])

    def test_size_falls_back_for_unknown_wake_category(self, qapp) -> None:  # noqa: ARG002
        """A pilot whose ``wake`` field somehow contains a
        non-canonical string (data corruption, future schema
        change) should still render at the baseline size rather
        than crashing on a missing key."""
        item = _TrafficPlaneItem(
            _make_pilot(wake="XX_NOT_A_REAL_CATEGORY"), icon_size_px=24
        )
        assert item._size == pytest.approx(24.0)

    def test_color_falls_back_for_unknown_wake(self, qapp) -> None:  # noqa: ARG002
        """Colour fallback for a non-canonical wake string should
        be the ``unknown`` gray, not a crash. Same defensive
        contract as the size fallback."""
        item = _TrafficPlaneItem(
            _make_pilot(wake="XX_NOT_A_REAL_CATEGORY"), icon_size_px=24
        )
        assert item._color.rgb() == WAKE_COLOR[WAKE_UNKNOWN].rgb()

    def test_bounding_rect_encompasses_silhouette_and_label(
        self, qapp  # noqa: ARG002
    ) -> None:
        """Bounding rect must cover at least the silhouette square
        on the left and the callsign label area on the right —
        otherwise Qt's spatial indexing would clip part of the
        item out of the viewport during scrolling."""
        item = _TrafficPlaneItem(_make_pilot(callsign="ABCDEF"), icon_size_px=20)
        rect = item.boundingRect()
        # Silhouette extends to the left of zero…
        assert rect.left() < -5
        # …and the label extends well to the right.
        assert rect.right() > 30

    def test_two_line_label_top_above_bottom(
        self, qapp  # noqa: ARG002
    ) -> None:
        """The two lines stack identity (top) over motion-state
        (bottom) — i.e. the top anchor's y is strictly less
        than the bottom anchor's y. Without this contract the
        lines could end up on the same row or in the wrong
        reading order."""
        item = _TrafficPlaneItem(_make_pilot(), icon_size_px=36)
        assert item._top_anchor.y() < item._bottom_anchor.y()

    def test_two_line_label_anchors_straddle_silhouette_midline(
        self, qapp  # noqa: ARG002
    ) -> None:
        """The block is centred so the top line sits above y=0
        (above the silhouette midline) and the bottom line sits
        below. Guards against a regression that would push the
        whole block above or below the silhouette."""
        item = _TrafficPlaneItem(_make_pilot(), icon_size_px=36)
        assert item._top_anchor.y() < 0
        assert item._bottom_anchor.y() > 0

    def test_two_line_label_lines_are_horizontally_aligned(
        self, qapp  # noqa: ARG002
    ) -> None:
        """Both lines anchor at the same X — that's what makes
        the stacked block read as a single column rather than a
        zig-zag. If a future refactor accidentally indents one
        line, this test catches it."""
        item = _TrafficPlaneItem(_make_pilot(), icon_size_px=36)
        assert item._top_anchor.x() == item._bottom_anchor.x()

    def test_two_line_label_anchored_to_right_of_silhouette(
        self, qapp  # noqa: ARG002
    ) -> None:
        """Both anchors sit to the right of the silhouette's
        rightmost edge (x = +size/2) so the text doesn't overlap
        the icon. A small gap is included; we just check the
        anchor is past the silhouette half-width."""
        item = _TrafficPlaneItem(_make_pilot(), icon_size_px=36)
        assert item._top_anchor.x() > 36 / 2
        assert item._bottom_anchor.x() > 36 / 2

    def test_top_text_combines_callsign_and_aircraft_type(
        self, qapp  # noqa: ARG002
    ) -> None:
        """End-to-end: the on-chart top line for a pilot with a
        filed plan reads ``<callsign>/<type>``. This is the
        integration test for :func:`_format_top_line` plumbed
        through to the QGraphicsItem."""
        pilot = _make_pilot(callsign="ELY323", aircraft_type="B738")
        item = _TrafficPlaneItem(pilot, icon_size_px=36)
        assert item._top_text == "ELY323/B738"

    def test_top_text_is_just_callsign_when_no_aircraft_type(
        self, qapp  # noqa: ARG002
    ) -> None:
        """A VFR pilot with no filed plan shows just the
        callsign on the top line (no trailing slash)."""
        pilot = _make_pilot(callsign="4XBLG", aircraft_type=None)
        item = _TrafficPlaneItem(pilot, icon_size_px=36)
        assert item._top_text == "4XBLG"

    def test_bottom_text_combines_altitude_and_speed(
        self, qapp  # noqa: ARG002
    ) -> None:
        """End-to-end: the on-chart bottom line reads
        ``<alt>/<speed>``. Validates both the IFR (FL) and
        VFR (ft) altitude branches make it through. The VFR
        case uses ``wake="L"`` so an 85 kt groundspeed reads
        as airborne (threshold 50 kt) rather than tripping
        the GRND override that fires for the default M wake
        (threshold 100 kt)."""
        ifr = _make_pilot(
            altitude_ft=28000, groundspeed_kts=420, flight_rules="I"
        )
        vfr = _make_pilot(
            altitude_ft=2500,
            groundspeed_kts=85,
            wake="L",
            flight_rules="V",
        )
        ifr_item = _TrafficPlaneItem(ifr, icon_size_px=36)
        vfr_item = _TrafficPlaneItem(vfr, icon_size_px=36)
        assert ifr_item._bottom_text == "FL280/420kt"
        assert vfr_item._bottom_text == "2500ft/85kt"

    def test_bottom_text_uses_grnd_when_on_ground(
        self, qapp  # noqa: ARG002
    ) -> None:
        """A parked airliner's bottom line reads ``GRND/0kt`` —
        the GRND override comes from :func:`_format_altitude_label`
        and the bottom-line composer carries it through."""
        parked = _make_pilot(
            altitude_ft=0, groundspeed_kts=0, wake="M", flight_rules="I"
        )
        taxiing = _make_pilot(
            altitude_ft=50, groundspeed_kts=12, wake="M", flight_rules="I"
        )
        parked_item = _TrafficPlaneItem(parked, icon_size_px=36)
        taxiing_item = _TrafficPlaneItem(taxiing, icon_size_px=36)
        assert parked_item._bottom_text == "GRND/0kt"
        assert taxiing_item._bottom_text == "GRND/12kt"

    def test_bounding_rect_grows_with_two_line_label(
        self, qapp  # noqa: ARG002
    ) -> None:
        """The two-line label is taller than a single line, so
        the bounding rect should accommodate at least the
        silhouette's full square plus extra vertical space for
        the second line + halo. With icon size 20 px and bold
        font 9 pt, two stacked lines are ~30 px tall; the rect
        height must clear that."""
        item = _TrafficPlaneItem(_make_pilot(callsign="ABCDEF"), icon_size_px=20)
        rect = item.boundingRect()
        assert rect.height() >= 30


# --- TrafficOverlay manager --------------------------------------------


class TestTrafficOverlay:
    """Lifecycle tests for :class:`TrafficOverlay` against a real
    :class:`QGraphicsScene`. We use a no-op projection (identity
    on lon/lat) for tests that want every pilot to project, and
    a returns-None projection for tests that want every pilot
    skipped."""

    def test_initially_empty(self, qapp) -> None:  # noqa: ARG002
        scene = QGraphicsScene()
        overlay = TrafficOverlay(scene, lambda lon, lat: QPointF(lon, lat))
        assert len(overlay) == 0
        assert len(scene.items()) == 0

    def test_set_pilots_adds_one_item_per_pilot(self, qapp) -> None:  # noqa: ARG002
        scene = QGraphicsScene()
        overlay = TrafficOverlay(scene, lambda lon, lat: QPointF(lon, lat))
        pilots = [
            _make_pilot(callsign="A1", cid=1),
            _make_pilot(callsign="A2", cid=2),
            _make_pilot(callsign="A3", cid=3),
        ]
        overlay.set_pilots(pilots, icon_size_px=24)
        assert len(overlay) == 3
        # Three traffic items are now in the scene; nothing else
        # was added because we constructed the scene empty.
        assert len(scene.items()) == 3

    def test_set_pilots_replaces_previous_items(self, qapp) -> None:  # noqa: ARG002
        """Idempotent rebuild contract: a second call clears the
        first call's items rather than stacking up. Otherwise
        every refresh would leak a generation of stale planes."""
        scene = QGraphicsScene()
        overlay = TrafficOverlay(scene, lambda lon, lat: QPointF(lon, lat))
        first = [_make_pilot(callsign="A", cid=1), _make_pilot(callsign="B", cid=2)]
        second = [_make_pilot(callsign="C", cid=3)]
        overlay.set_pilots(first, icon_size_px=24)
        overlay.set_pilots(second, icon_size_px=24)
        assert len(overlay) == 1
        assert len(scene.items()) == 1

    def test_clear_removes_every_item(self, qapp) -> None:  # noqa: ARG002
        scene = QGraphicsScene()
        overlay = TrafficOverlay(scene, lambda lon, lat: QPointF(lon, lat))
        overlay.set_pilots(
            [_make_pilot(callsign="A", cid=1), _make_pilot(callsign="B", cid=2)],
            icon_size_px=24,
        )
        overlay.clear()
        assert len(overlay) == 0
        assert len(scene.items()) == 0

    def test_clear_is_safe_when_already_empty(self, qapp) -> None:  # noqa: ARG002
        """Clearing twice in a row must not raise — the lifecycle
        hooks in MainWindow ought to be naive about whether
        anything was drawn last time."""
        scene = QGraphicsScene()
        overlay = TrafficOverlay(scene, lambda lon, lat: QPointF(lon, lat))
        overlay.clear()
        overlay.clear()
        assert len(overlay) == 0

    def test_pilots_with_unprojectable_lonlat_are_silently_skipped(
        self, qapp  # noqa: ARG002
    ) -> None:
        """When the projection callback returns None — typically
        because no sheet is calibrated yet, or the pilot is
        outside both sheets' coverage — the pilot must be
        skipped silently rather than failing the whole repaint."""
        scene = QGraphicsScene()
        overlay = TrafficOverlay(scene, lambda lon, lat: None)
        overlay.set_pilots(
            [_make_pilot(callsign="A", cid=1), _make_pilot(callsign="B", cid=2)],
            icon_size_px=24,
        )
        assert len(overlay) == 0
        assert len(scene.items()) == 0

    def test_set_pilots_uses_projection_for_position(self, qapp) -> None:  # noqa: ARG002
        """The projection callback's return value drives the
        ``setPos`` of each item — not the raw lat/lon. A test
        callback that always returns ``QPointF(100, 200)`` should
        place every plane at that scene point."""
        scene = QGraphicsScene()
        anchor = QPointF(100, 200)
        overlay = TrafficOverlay(scene, lambda lon, lat: anchor)
        overlay.set_pilots(
            [_make_pilot(callsign="A", cid=1)],
            icon_size_px=24,
        )
        items = scene.items()
        assert len(items) == 1
        assert items[0].pos() == anchor

    def test_partially_unprojectable_list(self, qapp) -> None:  # noqa: ARG002
        """When some pilots project and others don't, only the
        projectable ones get drawn — and the projectable ones
        should still all appear."""
        scene = QGraphicsScene()
        # Project pilots whose latitude is positive; skip negatives.
        overlay = TrafficOverlay(
            scene,
            lambda lon, lat: QPointF(lon, lat) if lat > 0 else None,
        )
        pilots = [
            _make_pilot(callsign="A", cid=1, lat=32.0, lon=35.0),
            _make_pilot(callsign="B", cid=2, lat=-45.0, lon=170.0),
            _make_pilot(callsign="C", cid=3, lat=33.0, lon=35.5),
        ]
        overlay.set_pilots(pilots, icon_size_px=24)
        assert len(overlay) == 2

    def test_icon_size_propagates_to_items(self, qapp) -> None:  # noqa: ARG002
        """The ``icon_size_px`` keyword should reach every
        ``_TrafficPlaneItem`` so the user's size choice in
        Display Settings drives the actual pixel size at render
        time."""
        scene = QGraphicsScene()
        overlay = TrafficOverlay(scene, lambda lon, lat: QPointF(lon, lat))
        overlay.set_pilots(
            [_make_pilot(callsign="A", cid=1, wake="M")],
            icon_size_px=40,
        )
        items = scene.items()
        assert len(items) == 1
        # M scale = 1.0, so size == icon_size_px exactly.
        assert items[0]._size == pytest.approx(40.0)


# --- Tracking selection (click-to-track yellow halo) -------------------


class TestTrackingSelection:
    """The "click a plane to track it" feature decorates exactly
    one ``_TrafficPlaneItem`` with a yellow halo and re-applies
    that decoration across every ``set_pilots`` rebuild (which
    happens on every 15 s VATSIM tick). These tests pin both the
    per-item flag and the overlay-level callsign tracking.
    """

    def test_item_starts_unselected(self, qapp) -> None:  # noqa: ARG002
        item = _TrafficPlaneItem(_make_pilot(callsign="EZE1"), 32)
        assert item.is_selected() is False

    def test_set_selected_flips_flag(self, qapp) -> None:  # noqa: ARG002
        item = _TrafficPlaneItem(_make_pilot(callsign="EZE1"), 32)
        item.set_selected(True)
        assert item.is_selected() is True
        item.set_selected(False)
        assert item.is_selected() is False

    def test_callsign_property_matches_pilot(self, qapp) -> None:  # noqa: ARG002
        """The hit-test in ``map_graphics_view`` translates an
        item hit into a callsign string; the property is the only
        public surface for that translation."""
        item = _TrafficPlaneItem(_make_pilot(callsign="ELY323"), 32)
        assert item.callsign == "ELY323"

    def test_bounding_rect_accommodates_halo(self, qapp) -> None:  # noqa: ARG002
        """The bounding rect must include enough margin around the
        silhouette to fit the selection halo at its full padding +
        stroke width. Otherwise the halo's outer edge clips on
        toggle, leaving a stale strip outside the previously-
        tight bbox.
        """
        from cvfr_routemaster.traffic_overlay import (
            _TRACKING_HALO_PADDING_PX,
            _TRACKING_HALO_WIDTH_PX,
        )

        size = 32
        item = _TrafficPlaneItem(_make_pilot(callsign="EZE1"), size)
        # The halo's outer extent above and below the silhouette
        # centre is half the silhouette size, plus the standoff
        # padding, plus half the stroke width.
        halo_outer = (
            size / 2.0
            + _TRACKING_HALO_PADDING_PX
            + _TRACKING_HALO_WIDTH_PX / 2.0
        )
        brect = item.boundingRect()
        # Silhouette is centred at the local origin, so the bbox
        # must extend at least ``halo_outer`` above and below in y
        # and at least ``halo_outer`` to the LEFT (the label
        # extends further to the right, so the right edge is
        # already comfortably outside the halo).
        assert -brect.top() >= halo_outer
        assert brect.bottom() >= halo_outer
        assert -brect.left() >= halo_outer

    def test_overlay_starts_with_no_tracked_callsign(self, qapp) -> None:  # noqa: ARG002
        scene = QGraphicsScene()
        overlay = TrafficOverlay(scene, lambda lon, lat: QPointF(lon, lat))
        assert overlay.tracked_callsign() is None

    def test_set_tracked_callsign_selects_matching_item(
        self, qapp  # noqa: ARG002
    ) -> None:
        scene = QGraphicsScene()
        overlay = TrafficOverlay(scene, lambda lon, lat: QPointF(lon, lat))
        overlay.set_pilots(
            [
                _make_pilot(callsign="A", cid=1),
                _make_pilot(callsign="B", cid=2),
                _make_pilot(callsign="C", cid=3),
            ],
            icon_size_px=24,
        )
        overlay.set_tracked_callsign("B")
        assert overlay.tracked_callsign() == "B"
        a = overlay.find_callsign("A")
        b = overlay.find_callsign("B")
        c = overlay.find_callsign("C")
        assert a is not None and b is not None and c is not None
        assert a.is_selected() is False
        assert b.is_selected() is True
        assert c.is_selected() is False

    def test_set_tracked_callsign_to_none_clears_selection(
        self, qapp  # noqa: ARG002
    ) -> None:
        scene = QGraphicsScene()
        overlay = TrafficOverlay(scene, lambda lon, lat: QPointF(lon, lat))
        overlay.set_pilots(
            [_make_pilot(callsign="A", cid=1)], icon_size_px=24,
        )
        overlay.set_tracked_callsign("A")
        assert overlay.find_callsign("A").is_selected() is True
        overlay.set_tracked_callsign(None)
        assert overlay.tracked_callsign() is None
        assert overlay.find_callsign("A").is_selected() is False

    def test_set_tracked_callsign_with_unknown_callsign_selects_nothing(
        self, qapp  # noqa: ARG002
    ) -> None:
        """Passing a callsign that no item matches is not an
        error — the state is stashed, no item gets a halo, and
        the next ``set_pilots`` that DOES include that callsign
        will apply the visual. This matches the lost-pilot resume
        path (user keeps tracking on a brief feed dropout)."""
        scene = QGraphicsScene()
        overlay = TrafficOverlay(scene, lambda lon, lat: QPointF(lon, lat))
        overlay.set_pilots(
            [_make_pilot(callsign="A", cid=1)], icon_size_px=24,
        )
        overlay.set_tracked_callsign("NOT_PRESENT")
        assert overlay.tracked_callsign() == "NOT_PRESENT"
        assert overlay.find_callsign("A").is_selected() is False

    def test_tracking_survives_set_pilots_rebuild(
        self, qapp  # noqa: ARG002
    ) -> None:
        """The 15 s VATSIM tick tears down every plane item and
        rebuilds from scratch. The selection visual must follow
        the callsign across that rebuild — that's the whole point
        of stashing the tracked callsign at the manager level
        rather than on the items."""
        scene = QGraphicsScene()
        overlay = TrafficOverlay(scene, lambda lon, lat: QPointF(lon, lat))
        overlay.set_pilots(
            [_make_pilot(callsign="EZE1", cid=1)], icon_size_px=24,
        )
        overlay.set_tracked_callsign("EZE1")
        # Simulate a fresh VATSIM snapshot: same callsign, new
        # position, same icon size. The item is a brand-new
        # ``_TrafficPlaneItem`` instance after this call.
        overlay.set_pilots(
            [_make_pilot(callsign="EZE1", cid=1, lat=33.0, lon=35.5)],
            icon_size_px=24,
        )
        item = overlay.find_callsign("EZE1")
        assert item is not None
        assert item.is_selected() is True

    def test_tracking_clears_when_callsign_drops_out(
        self, qapp  # noqa: ARG002
    ) -> None:
        """If the tracked plane vanishes from the snapshot, no item
        is selected; the tracked-callsign string is preserved so
        that if the plane comes back later the halo resumes.
        Stop-on-drop behaviour is MainWindow's responsibility (it
        shows the status-bar message and then calls
        ``set_tracked_callsign(None)``)."""
        scene = QGraphicsScene()
        overlay = TrafficOverlay(scene, lambda lon, lat: QPointF(lon, lat))
        overlay.set_pilots(
            [_make_pilot(callsign="EZE1", cid=1)], icon_size_px=24,
        )
        overlay.set_tracked_callsign("EZE1")
        # Snapshot without EZE1 — different pilot.
        overlay.set_pilots(
            [_make_pilot(callsign="OTHER", cid=2)], icon_size_px=24,
        )
        assert overlay.tracked_callsign() == "EZE1"
        assert overlay.find_callsign("EZE1") is None
        assert overlay.find_callsign("OTHER").is_selected() is False
        # Pilot reappears -> halo restored without any extra
        # set_tracked_callsign call.
        overlay.set_pilots(
            [_make_pilot(callsign="EZE1", cid=1)], icon_size_px=24,
        )
        assert overlay.find_callsign("EZE1").is_selected() is True


# --- Demo fixture sanity check -----------------------------------------


class TestDemoPilots:
    """The hand-fed demo fixture is the bridge to the live poller —
    pin the contract that it covers all five wake categories so
    the user can validate the whole visual encoding by toggling
    the overlay on once."""

    def test_demo_covers_all_five_wake_categories(self) -> None:
        from cvfr_routemaster.traffic_demo import demo_pilots

        wakes = {p.wake for p in demo_pilots()}
        assert wakes == {"L", "M", "H", "J", WAKE_UNKNOWN}

    def test_demo_pilots_are_in_israeli_airspace(self) -> None:
        """All demo pilots should sit inside Israel's rough
        bounding box (29.5°-33.3° N, 34.2°-35.8° E) so they
        actually project against the Israel CVFR chart's
        calibrated coverage."""
        from cvfr_routemaster.traffic_demo import demo_pilots

        for p in demo_pilots():
            assert 29.5 <= p.lat <= 33.5, f"{p.callsign} lat {p.lat} out of bounds"
            assert 34.2 <= p.lon <= 35.8, f"{p.callsign} lon {p.lon} out of bounds"

    def test_demo_pilots_have_unique_callsigns(self) -> None:
        from cvfr_routemaster.traffic_demo import demo_pilots

        pilots = demo_pilots()
        callsigns = [p.callsign for p in pilots]
        assert len(callsigns) == len(set(callsigns))

    def test_demo_pilots_have_unique_cids(self) -> None:
        from cvfr_routemaster.traffic_demo import demo_pilots

        pilots = demo_pilots()
        cids = [p.cid for p in pilots]
        assert len(cids) == len(set(cids))


# --- show_vatsim_traffic persistence -----------------------------------


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Redirect ``settings_store._settings()`` at a per-test INI
    file so persistence tests never touch the user's real CVFR
    Route Master config. Same pattern as
    ``test_window_layout_persistence`` and ``test_font_settings``.
    """
    ini_path = tmp_path / "test_settings.ini"

    def _factory() -> QSettings:
        return QSettings(str(ini_path), QSettings.Format.IniFormat)

    monkeypatch.setattr(settings_store, "_settings", _factory)
    return ini_path


class TestShowVatsimTrafficPersistence:
    """Round-trip the "Show VATSIM traffic" toolbar toggle through
    QSettings. The toggle's contract is simple — boolean in,
    boolean out, default ``False`` for first launch — so the
    surface area is small but worth pinning."""

    def test_default_is_false_when_nothing_saved(self, isolated_settings) -> None:
        """First-launch contract: a fresh install starts with the
        overlay off. The user opts in via the toolbar, not the
        other way around — keeps the chart unencumbered out of
        the box for users who don't use VATSIM."""
        assert load_show_vatsim_traffic() is False

    def test_save_true_round_trips(self, isolated_settings) -> None:
        """Persist ``True`` and read it back unchanged. Anything
        else means the QSettings layer is silently coercing the
        value (e.g. to a string)."""
        save_show_vatsim_traffic(True)
        assert load_show_vatsim_traffic() is True

    def test_save_false_round_trips(self, isolated_settings) -> None:
        """Persist ``False`` after saving ``True`` — the second
        save must overwrite, not merge."""
        save_show_vatsim_traffic(True)
        save_show_vatsim_traffic(False)
        assert load_show_vatsim_traffic() is False

    def test_save_returns_actual_bool(self, isolated_settings) -> None:
        """``load`` is typed ``bool`` — make sure callers can
        rely on that contract without isinstance gymnastics. The
        QSettings ``value(..., bool)`` cast handles this, but a
        future refactor might break it."""
        save_show_vatsim_traffic(True)
        loaded = load_show_vatsim_traffic()
        assert isinstance(loaded, bool)
        assert loaded is True

    def test_save_overwrites_previous_value(self, isolated_settings) -> None:
        """Multiple toggles in one session: the latest value wins.
        Without overwrite semantics the user could end up with
        whatever state they were in at first toggle, regardless
        of subsequent toggles."""
        save_show_vatsim_traffic(False)
        save_show_vatsim_traffic(True)
        save_show_vatsim_traffic(False)
        save_show_vatsim_traffic(True)
        assert load_show_vatsim_traffic() is True

    def test_save_coerces_truthy_inputs(self, isolated_settings) -> None:
        """``save_show_vatsim_traffic`` is annotated as ``bool``
        but Python won't enforce it; the implementation explicitly
        ``bool()``-coerces the input. A truthy non-bool (1, "x")
        therefore loads back as ``True`` rather than crashing."""
        save_show_vatsim_traffic(1)  # type: ignore[arg-type]
        assert load_show_vatsim_traffic() is True


# Module-level QGuiApplication import is only here to silence the
# import-time error you'd get if a tester imported this file
# without a Qt platform plugin available. The qapp fixture below
# does the real work; this just ensures the import line itself is
# legitimate noise.
_ = QGuiApplication
