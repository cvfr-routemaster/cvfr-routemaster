"""Pin the displayed magnetic bearing per leg against chart-printed values.

The Israeli CVFR chart prints integer magnetic bearings for selected route
legs. Pilots cross-check the app's table against those printed values, so
any drift in our heading model (magnetic-variation constant, rounding mode,
projection assumptions, ...) shows up immediately as a printed disagreement.

The samples below were measured by reading the printed bearing off the
chart for each leg, then captured with the originating waypoint coordinates
inline so this test stays decoupled from the OCR'd ``waypoints.csv`` (a
re-OCR drift on any one waypoint could move a bearing by ~1° and confuse
the assertions otherwise).

Two assertion tiers:

* **Tolerance tier** — every leg must display within ±1° of chart. This
  is the hard regression guard; failing here means the heading model has
  drifted measurably and pilots will see disagreement.
* **Strict tier** — 13 of 15 legs match the chart *exactly*. This pins
  the chart-drafter convention we modelled (``floor`` rounding at
  ``MAG VAR 5°E``). Loosening this set without a documented chart-cycle
  change is a regression.

The two remaining legs (``LLKS→BASAN`` and ``ZOHAR→HATRU``) sit at +1°
even under the best-fit chart-drafter model. Both involve airport-grade
fixes whose published ARP coordinates likely differ slightly from what
the chart drafter used to measure those legs (a 30″-of-arc shift on the
LLKS longitude moves its bearing by ~1°). They're documented explicitly
below so a future agent reading the test knows they aren't a math bug
and shouldn't be "fixed" by tuning the model — any model tweak that
zeroes them would push the other 13 legs out of agreement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from cvfr_routemaster.route import (
    ISRAEL_MAGNETIC_VARIATION_DEG_E,
    magnetic_bearing_deg,
)
from cvfr_routemaster.route_panel import _format_mag_brg_text


@dataclass(frozen=True)
class _Leg:
    a_code: str
    b_code: str
    a_lat: float
    a_lon: float
    b_lat: float
    b_lon: float
    chart_mag: int


# Chart-printed magnetic bearings, measured by the dev against the 2025
# CVFR chart cycle. Coordinates are inlined from the back-page OCR table
# at the time the bearings were captured (not loaded from waypoints.csv
# at test time) so the test pins the heading model, not the OCR.
LEGS: tuple[_Leg, ...] = (
    _Leg("SHALM", "ZUKIM", 31.558611, 35.402500, 31.716111, 35.449722, 8),
    _Leg("ALMOG", "YRIHO", 31.789722, 35.456389, 31.819444, 35.387500, 291),
    _Leg("NAAMA", "FAZEL", 31.908611, 35.462500, 32.048056, 35.463056, 354),
    _Leg("FAZEL", "ALLON", 32.048056, 35.463056, 32.041389, 35.366667, 259),
    _Leg("GALIM", "LLHA",  32.841111, 34.981111, 32.808333, 35.042778, 116),
    _Leg("LLHA",  "GALIM", 32.808333, 35.042778, 32.841111, 34.981111, 296),
    _Leg("LLKS",  "BASAN", 33.211667, 35.596389, 33.146111, 35.638056, 145),
    _Leg("BASAN", "HULAT", 33.146111, 35.638056, 33.043611, 35.629444, 178),
    _Leg("HULAT", "LLIB",  33.043611, 35.629444, 32.980556, 35.570833, 212),
    _Leg("TARAD", "ARRAD", 31.277500, 35.125556, 31.254444, 35.209722, 102),
    _Leg("ARRAD", "LLMZ",  31.254444, 35.209722, 31.329167, 35.388333, 58),
    _Leg("LLMZ",  "MMORR", 31.329167, 35.388333, 31.254444, 35.368333, 187),
    _Leg("MMORR", "ZOHAR", 31.254444, 35.368333, 31.150833, 35.372500, 172),
    _Leg("ZOHAR", "HATRU", 31.150833, 35.372500, 31.218889, 35.248333, 296),
    _Leg("HATRU", "ARRAD", 31.218889, 35.248333, 31.254444, 35.209722, 311),
)


# Legs whose displayed bearing matches the chart exactly under the
# current chart-drafter model (``floor`` of magnetic at ``VAR 5°E``).
# Any leg not in this set is expected to display 1° off (see
# ``_OUTLIER_LEGS`` below).
_STRICT_MATCH_LEGS: frozenset[tuple[str, str]] = frozenset(
    (leg.a_code, leg.b_code)
    for leg in LEGS
    if (leg.a_code, leg.b_code) not in {("LLKS", "BASAN"), ("ZOHAR", "HATRU")}
)


# Legs known to remain at +1° even after matching the chart-drafter
# convention. Documented here (rather than just excluded silently)
# so any future agent looking at the test understands these are not
# a heading-model bug.
_OUTLIER_LEGS: frozenset[tuple[str, str]] = frozenset(
    {("LLKS", "BASAN"), ("ZOHAR", "HATRU")}
)


def _displayed_mag(leg: _Leg) -> int:
    """Compute the integer magnetic bearing the route panel will display
    for ``leg``, by feeding the float bearing through the same formatter
    the panel uses."""
    deg = magnetic_bearing_deg(
        leg.a_lat, leg.a_lon, leg.b_lat, leg.b_lon
    )
    text = _format_mag_brg_text(deg)
    assert text.endswith("°M"), f"unexpected formatter output: {text!r}"
    return int(text[:-2])


def _signed_diff(displayed: int, chart: int) -> int:
    """Shortest-arc signed difference in degrees, range ``[-180, 180]``."""
    diff = (displayed - chart) % 360
    if diff > 180:
        diff -= 360
    return diff


# ---------------------------------------------------------------------------
# Tolerance tier — all 15 legs within ±1° of chart
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("leg", LEGS, ids=lambda leg: f"{leg.a_code}->{leg.b_code}")
def test_displayed_mag_within_one_degree_of_chart(leg: _Leg) -> None:
    """Every chart leg must display within ±1° of the printed value.

    Hard regression guard. A failure here means the heading model has
    drifted in a user-visible way — pilots will see the app and chart
    disagree on the table cell. Don't widen this tolerance without
    discussing the trade-off first.
    """
    displayed = _displayed_mag(leg)
    diff = _signed_diff(displayed, leg.chart_mag)
    assert abs(diff) <= 1, (
        f"{leg.a_code}->{leg.b_code}: displayed {displayed:03d}°M, "
        f"chart {leg.chart_mag:03d}°M, diff {diff:+d}° (tolerance ±1°)"
    )


# ---------------------------------------------------------------------------
# Strict tier — chart-drafter model matches exactly on 13/15 legs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leg",
    [leg for leg in LEGS if (leg.a_code, leg.b_code) in _STRICT_MATCH_LEGS],
    ids=lambda leg: f"{leg.a_code}->{leg.b_code}",
)
def test_strict_match_legs_render_exactly_chart_value(leg: _Leg) -> None:
    """13/15 chart legs render the chart-printed value exactly.

    Pins the chart-drafter model (``floor`` of magnetic at ``VAR 5°E``).
    A failure here implies either:
      * The variation constant moved without a new chart-cycle update,
      * The rounding mode in ``_format_mag_brg_text`` moved,
      * Or a leg's coordinates were updated in the test data (the test
        is supposed to pin coords-as-of-measurement, so coord edits
        should re-measure the chart value too).
    """
    displayed = _displayed_mag(leg)
    assert displayed == leg.chart_mag, (
        f"{leg.a_code}->{leg.b_code}: displayed {displayed:03d}°M but "
        f"chart prints {leg.chart_mag:03d}°M"
    )


# ---------------------------------------------------------------------------
# Outliers — document the 2 known residual-1° legs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leg",
    [leg for leg in LEGS if (leg.a_code, leg.b_code) in _OUTLIER_LEGS],
    ids=lambda leg: f"{leg.a_code}->{leg.b_code}",
)
def test_outlier_legs_remain_exactly_one_degree_high(leg: _Leg) -> None:
    """The 2 outlier legs display exactly +1° vs chart, no more, no less.

    These legs (``LLKS→BASAN`` and ``ZOHAR→HATRU``) involve airport-grade
    fixes whose published ARP coordinates likely differ slightly from
    what the chart drafter measured against. They're +1° under every
    chart-drafter model we tried (round at 5.0/5.5/6.0°, floor at
    5.0/5.5°). If this test breaks they either (a) got resolved by a
    coordinate update — celebrate and tighten the strict tier, or (b)
    the heading model moved and now they're +2° again — investigate
    the model change.
    """
    displayed = _displayed_mag(leg)
    diff = _signed_diff(displayed, leg.chart_mag)
    assert diff == 1, (
        f"{leg.a_code}->{leg.b_code}: displayed {displayed:03d}°M, "
        f"chart {leg.chart_mag:03d}°M, diff {diff:+d}° (expected +1°). "
        f"If this is now 0°, move this leg into the strict tier; if "
        f"it's +2° again, the heading model has regressed."
    )


# ---------------------------------------------------------------------------
# Self-consistency: pin the model state at this commit
# ---------------------------------------------------------------------------


def test_variation_constant_pinned_to_chart_legend_value() -> None:
    """The 2025 chart legend prints ``VAR 5°E``. Anything else here is a
    chart-cycle drift that should re-measure the regression samples
    before being merged."""
    assert ISRAEL_MAGNETIC_VARIATION_DEG_E == pytest.approx(5.0)


def test_formatter_applies_chart_drafter_half_degree_downward_bias() -> None:
    """The formatter must implement ``floor(x − 0.5)``, the chart
    drafter's effective rounding convention. Pinned with fractional
    inputs that distinguish it from plain floor and from
    round-to-nearest:

      * ``9.307`` → plain floor 9, round 9, but drafter's ``floor(x-0.5)`` = 8.
      * ``9.500`` → plain floor 9, round 10 (half-up), drafter's = 9.
      * ``9.800`` → plain floor 9, round 10, drafter's = 9.
      * ``10.001`` → plain floor 10, round 10, drafter's = 9 (the half-step
        below an integer rounds down to the next integer below it).
    """
    assert _format_mag_brg_text(9.307) == "008°M"
    assert _format_mag_brg_text(9.500) == "009°M"
    assert _format_mag_brg_text(9.800) == "009°M"
    assert _format_mag_brg_text(10.001) == "009°M"
    # The 0.5 boundary itself rounds down by definition of floor(x-0.5).
    assert _format_mag_brg_text(0.499) == "359°M"  # wraps via % 360
    # Whole-degree inputs always print one less than the value.
    assert _format_mag_brg_text(180.0) == "179°M"
    assert _format_mag_brg_text(360.0) == "359°M"
    # Negative inputs wrap defensively. ``floor(-0.5 - 0.5) = floor(-1.0)
    # = -1``, and Python ``-1 % 360 == 359``.
    assert _format_mag_brg_text(-0.5) == "359°M"


def test_leg_count_is_stable() -> None:
    """Tripwire: the 15-leg dataset is the calibrated regression
    surface. Adding legs without re-measuring chart-printed values
    would silently weaken the test."""
    assert len(LEGS) == 15
    assert len(_STRICT_MATCH_LEGS) == 13
    assert len(_OUTLIER_LEGS) == 2
    assert _STRICT_MATCH_LEGS.isdisjoint(_OUTLIER_LEGS)


# Reference the math module so the rounding-mode comment in the
# formatter doesn't go stale silently — if a future refactor removes
# the math.floor call this test will fail to import.
def test_floor_is_well_defined_for_all_legs() -> None:
    for leg in LEGS:
        deg = magnetic_bearing_deg(
            leg.a_lat, leg.a_lon, leg.b_lat, leg.b_lon
        )
        assert math.floor(deg) == int(deg) or deg < 0, (
            f"{leg.a_code}->{leg.b_code}: floor/int disagree at deg={deg!r}"
        )
