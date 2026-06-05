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
Extract CVFR altitude-arrow labels from a chart PDF and match them to route
segments at flight-plan time.

Israeli CVFR charts mark each route's permitted flight altitude(s) with small
yellow arrow labels printed alongside the magenta route line. Each arrow's
**tip points in the direction of travel** the altitude applies to (so a
two-way route gets a pair of arrows pointing at each other), and stacked
numbers inside one arrow mean a band — e.g. ``1600`` over ``800`` is an
"800 to 1600 ft" sandwich for that direction.

The chart's PDFs encode all of this as **vector text + vector drawings**, so
no OCR is needed:

- ``page.get_drawings()`` returns each yellow-fill polygon (the arrow shape)
  with its bbox and full path geometry.
- ``page.get_text("dict")`` returns each digit string with its bbox.
- A numeric span whose *centre* lies inside an arrow's rect is one of that
  arrow's altitude labels.
- The arrow's compass direction falls out of the offset between the *path*
  centroid (vertices cluster near the converging tip) and the *bbox* centre
  (a flat-tail arrow's bbox is biased toward the tail). The direction sign
  is a function of where in the bbox the path mass lives.

The whole extractor is one offline pass per chart PDF; results are cached on
disk via :mod:`cvfr_routemaster.altitude_cache` so subsequent runs hit a JSON
file instead of re-walking ~150k drawings.

Matching is run live for each ``RouteSegment``. We project arrows from
PDF-pt → cropped pixmap UV (using the :class:`CropMeta` captured at render
time) → lat/lon (using the user's geo calibration), then keep arrows whose
centre is within ``MATCH_RADIUS_NM`` of the segment line *and* whose
heading is within ``MATCH_PARITY_TOL_DEG`` of being parallel-or-anti-
parallel to the segment direction (a coarse perpendicular-route filter).

Disambiguating which of a two-way segment's two arrows is "ours" is the
hard part. The centroid-based tip extraction is too noisy on real chart
arrows (a rounded-rectangle body has more vertices than a sharp tip, so
the centroid ends up biased toward the tail rather than the tip), so we
sidestep arrow direction extraction entirely and lean on a sturdier
chart-printing convention instead: **the OUR-direction altitude label is
to the right of the flight path** — east of north-bound legs, south of
east-bound legs, and so on. The matcher therefore prefers right-of-track
candidates and only falls back to a left-of-track arrow when the chart
breaks convention (BOREN → HOTRM is the canonical example).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import fitz

from cvfr_routemaster.geo_calibration import SheetGeoCalibration
from cvfr_routemaster.map_crop import CropMeta
from cvfr_routemaster.route import RouteSegment, true_bearing_deg


# ---------------------------------------------------------------------------
# Tunable constants — see module docstring for the rationale per threshold
# ---------------------------------------------------------------------------

#: How parallel an arrow's heading must be to the segment's true bearing
#: to count as "for OUR travel direction" rather than "for the reverse".
#:
#: With the concavity-based bearing extractor this is a real bearing, not
#: an inverted heuristic, so anti-parallel arrows are reliably the chart's
#: OPPOSITE-direction labels and should never match a forward-going leg.
#:
#: 30° is tight enough to keep an arrow that legitimately belongs to one
#: leg from "leaking" into the adjacent leg when the route kinks by more
#: than 30°: in real CVFR routes, a single label is associated with one
#: leg whose own bearing matches the arrow's bearing to within a few
#: degrees, so genuine matches sit comfortably below 20° of fwd-diff.
#: Empirically, real-route diagnostics on the LLHZ→…→LLHA leg series
#: showed every correct match below 16° fwd-diff while the few wrong
#: matches sat in the 39–41° range (arrows from the next/previous leg
#: peeking in across a 30–40° route bend); 30° cleanly separates the
#: two populations.
MATCH_PARALLEL_TOL_DEG: float = 30.0


#: Match radius around a segment's great-circle line.
#:
#: Sized to cover three sources of slack stacked on top of each other:
#:   - The arrow tail's geometric offset from the route line — the tail sits
#:     on the line but our extracted "arrow position" is at the *bbox edge*
#:     opposite the tip (we walk inward from the bbox centre toward the tail
#:     side). Even after that correction the chart sometimes prints arrows
#:     with their tail a fraction of a millimetre off the line.
#:   - Residual error from the user's geo calibration. The LSQ fit usually
#:     lands at 0.1–0.3% of chart width which translates to ≤0.1 nm at the
#:     1:500K Israeli CVFR sheet scale.
#:   - The arrow's own physical extent — a 5 mm-long arrow at 1:500K is
#:     ~1.5 nm end-to-end, so 0.5 nm is still well inside one arrow's worth
#:     of slack and won't claim altitudes from a parallel route.
#:
#: Empirically validated against the LLHZ→...→LLHA route on the north sheet:
#: parallel-direction matches sit at 0.24–0.58 nm off the segment line
#: (the FRDIS→BOREN, HOTRM→DAROM, GALIM→LLHA legs all have their genuine
#: arrow just past 0.5 nm). 0.65 nm is the smallest radius that catches
#: every legitimate match on that test route without admitting a wrong-
#: direction arrow.
MATCH_RADIUS_NM: float = 0.65


#: Wider matching radius applied **only** to segments where at least one
#: endpoint is a free-clicked intermediate point (``RoutePoint.waypoint is
#: None``). Real ICAO 5-letter waypoints carry chart-precise coordinates
#: from the published waypoint list, so the strict ``MATCH_RADIUS_NM``
#: gate is appropriate for legs entirely between them. Free-clicked
#: intermediates are user-discretion approximations of the chart route
#: line — a click can sit up to ~0.7 nm off the "true" CVFR route line
#: (one arc-minute is 1 nm, and ICAO Field 15 coords are rounded to the
#: nearest minute, so the rendered ``DDMMNDDDMME`` label has up to a
#: 0.5 nm half-side error in each axis, ≈ 0.7 nm diagonal).
#:
#: Empirical case: the LLHA→LLHZ inverse route's middle GALIM↔DAROM sub-
#: leg has its yellow ``(2000, 1000)`` arrow pair sitting 1.02 nm from the
#: ICAO-rounded segment line. With the strict 0.65 nm radius this leg
#: returned ``unknown`` even though the GUI's actual click coords found
#: the match cleanly. 1.30 nm is the smallest radius that covers this
#: case while still being well clear of crossing-route arrows on the
#: same chart (the closest cross-track distractor in our diagnostic
#: traces sits at 2.6+ nm).
#:
#: Crucially, **competitive matching arbitrates fairly across segments**
#: regardless of which radius they use. An arrow that matches *both* a
#: real-waypoint segment (strict gate) and an adjacent intermediate
#: segment (loose gate) goes to whichever segment has the lower
#: ``(class_rank, score)`` — the loose radius for intermediates only
#: gives them a *chance* to compete; it never lets them steal arrows
#: from precise legs.
MATCH_RADIUS_NM_INTERMEDIATE: float = 1.30


#: Maximum geographic distance from a primary altitude arrow to one of its
#: side-by-side stacked alternates. Israeli CVFR charts sometimes print two
#: (or three) yellow arrows in a row for the same leg — each carrying a
#: different altitude that ATC can clear (canonical example: BAZRA→DEROR
#: northbound has a 1600 arrow next to an 800 arrow when leaving CTR
#: HERTZLIA). Once a segment's primary arrow is picked we sweep within this
#: radius around the primary's position to gather alternates.
#:
#: 0.55 nm covers ~3 arrow widths (1:500K chart, 5 mm-wide arrows ≈ 1.5 nm)
#: which is comfortably wider than any side-by-side cluster I've seen on
#: the North/South sheets, and tight enough that an unrelated parallel
#: route's arrow wouldn't get conflated. Empirical separations on real
#: chart pairs:
#:   - BAZRA→DEROR  1600 ↔ 800 alternate ≈ 0.40 nm
#:   - DAROM→HOTRM  2000 ↔ 1000 alternate ≈ 0.50 nm
#: Both fit comfortably under 0.55 nm.
MATCH_STACK_RADIUS_NM: float = 0.55


#: A stacked alternate must point in essentially the same direction as the
#: primary — same chart-leg arrows are printed parallel, never at a kink.
#: 15° is more than enough margin for extraction noise, and far below the
#: 60° perpendicular zone where crossing-route arrows live.
MATCH_STACK_BEARING_TOL_DEG: float = 15.0


#: Maximum allowed *along-segment* overshoot past either endpoint when
#: testing an arrow's perpendicular foot against the segment.
#:
#: ``_great_circle_distance_to_segment_nm`` returns the great-circle
#: distance to the nearest endpoint when the foot lies outside [A, B],
#: which is the right answer for "how far from the segment is this
#: point?" but the wrong question for "does this chart arrow label
#: this segment?". An arrow whose foot projects substantially *past*
#: an endpoint belongs — by chart convention — to whatever route
#: continues beyond that endpoint, not to OUR leg.
#:
#: Concrete bug this gate resolves (Dead Sea route, May 13 2026): the
#: ``3147N03530E → ALMOG`` westbound sub-leg (true bearing 279.8°) was
#: picking up a ``(4000,)`` arrow at @(31.801, 35.450) whose bearing
#: (292°) put it within the parallel tolerance, and whose endpoint-
#: clamped cross-track (0.73 nm) fell inside the intermediate leg's
#: 1.30 nm loose radius. The arrow actually labels the chart's route
#: continuing westbound *from* ALMOG along highway 1 — its foot sits
#: ~0.42 nm past the segment's terminal endpoint. The two real 4000
#: arrows near ALMOG (one east, one west) are paired with the chart's
#: through-route, not with the user's terminating leg.
#:
#: 0.30 nm threshold: chosen to sit comfortably between the two
#: closest competing populations on the real charts:
#:
#:   - Confirmed false positives have overshoot ≥ 0.42 nm (the Dead
#:     Sea ALMOG case above). 0.30 nm rejects those with 40% safety
#:     margin.
#:   - The worst legitimate match we've seen — the LLHA→LLHZ inverse
#:     route's ``3250N03458E → 3249N03457E`` middle sub-leg — has
#:     overshoot 0.26 nm because the user's ICAO-minute-rounded
#:     intermediate clicks sit slightly inside the chart-published
#:     (2000, 1000) arrow pair's longitudinal extent. 0.30 nm
#:     accepts that with a 0.04 nm cushion.
#:
#: A tighter threshold (0.20 nm) was tried first and broke the
#: LLHA→LLHZ regression — that one false negative pinned the floor.
#: A looser threshold (≥0.40 nm) would re-admit the Dead Sea bug.
#: The 0.30 nm value is the smallest threshold that catches every
#: known bug while preserving every known-good match.
#:
#: This gate alone isn't sufficient: the *user's actual click*
#: position (sub-minute precision, vs the ICAO-minute display
#: label) shifts the bug's overshoot from 0.42 nm down to 0.29 nm
#: — JUST inside the 0.30 nm budget. The complementary
#: ``MATCH_PARALLEL_TOL_DEG_PAST_ENDPOINT`` gate below catches the
#: remaining slack: a past-endpoint arrow must also be extremely
#: well-aligned with the segment bearing (≤ 15° fwd-diff) to count,
#: which the bug's 23° fwd-diff fails.
MATCH_MAX_ENDPOINT_OVERSHOOT_NM: float = 0.30


#: Tighter parallel tolerance applied specifically to arrows whose
#: perpendicular foot lies past either segment endpoint
#: (``overshoot > 0``). On-segment arrows still use the wider
#: ``MATCH_PARALLEL_TOL_DEG`` budget.
#:
#: Rationale: a past-endpoint arrow is by chart convention labeling
#: something *other* than the segment between A and B — either the
#: route continuing past B, or the route preceding A. Such arrows
#: must be very tightly aligned with the segment's bearing to count
#: as borderline-acceptable matches. The looser 30° tolerance is
#: justified for on-segment arrows because chart printing tolerance
#: + extraction noise can introduce ~5° of jitter on an arrow that's
#: genuinely for OUR leg; past-endpoint arrows don't get that
#: courtesy because the foot-past-endpoint signature is itself
#: already a "wrong leg" indicator that we're trying to soften.
#:
#: 15° vs the on-segment 30° budget: the only two known populations
#: of past-endpoint arrows in our regression coverage are:
#:
#:   * **Bug (Dead Sea, ALMOG)** — fwd-diff 23.6° at the user's
#:     actual click position. Rejected with 8.6° margin.
#:   * **LLHA→LLHZ inverse middle sub-leg** — fwd-diff 0.9° on the
#:     genuine ``(2000, 1000)`` chart-label arrows. Accepted with
#:     14.1° margin.
#:
#: The 22° gap between these two known populations gives the
#: threshold ample headroom in either direction. Pinned by
#: ``test_parallel_tol_past_endpoint_is_in_safe_band``.
MATCH_PARALLEL_TOL_DEG_PAST_ENDPOINT: float = 15.0


#: Penalty weight on parallel-bearing diff when scoring how well an arrow
#: fits a segment for **competitive matching** (each arrow goes to its
#: single best segment so an arrow that is the label for leg X cannot be
#: stolen by adjacent leg Y just because Y's tube also reaches it).
#:
#: 0.01 nm/° means a 25° fwd-diff difference equates to 0.25 nm of cross-
#: track distance difference — calibrated against the inverse-route
#: diagnostic where the FP arrows sat at fwd-diff 28°/4.6° (Δ ≈ 24°) on
#: their adjacent vs primary segments and ~0.25 nm closer to their primary.
#: With this weight the primary always wins the assignment.
MATCH_FWD_DIFF_SCORE_WEIGHT: float = 0.01


# ---------------------------------------------------------------------------
# Shared-bend arrow rescue (post-pass thresholds).
#
# When two consecutive route legs share a real waypoint AND the chart
# places ONE altitude arrow at the bend whose body is drawn along the
# bisector of the two legs (not along either leg individually), neither
# leg's standard gates can claim it — the arrow is parallel to neither
# leg by enough margin to register, and one or both legs have it just
# outside their per-leg radius. The per-leg pass therefore leaves both
# legs "unknown" even though a chart-reading pilot sees a single corridor
# altitude.
#
# Canonical case: HTZUK→KNTRY→LLHZ on the LLMZ→LLHZ northbound. The
# route turns ~68° at KNTRY (HTZUK→KNTRY bearing 103.6°, KNTRY→LLHZ
# 35.9°). The chart's 1200 arrow sits just NE of HTZUK at bearing 71.4°,
# within 1.7° of the (104+36)/2 = 70° bisector. Standard gates fail
# both legs: HTZUK→KNTRY at fwd-diff 32.2° (just past the 30° parallel
# tolerance), KNTRY→LLHZ at 1.07 nm cross-track (well past the 0.65 nm
# real-waypoint radius). Pilots read it as 1200 for both legs.
#
# The post-pass runs AFTER competitive matching + stacking finalize per-
# leg verdicts, so it can only fire on legs that ended up empty AND on
# arrows that no other leg claimed. The gates below are deliberately
# narrow so the rescue can't propagate altitudes through corridors the
# chart intentionally leaves unlabeled (e.g. NSHRM→SIRNI=1200 followed
# by SIRNI→NTAIM=unknown, where the chart's corridor altitude actually
# changes at SIRNI).


#: Minimum bend angle (in degrees) at the shared waypoint for the
#: rescue to consider a pair of legs. Below this the standard per-leg
#: parallel tolerance (30°) should already have caught any single
#: corridor arrow on one of the two legs — the rescue would only fire
#: as a redundant copy. 30° matches the on-segment parallel tolerance:
#: below 30° both legs share a class-rank-eligible bearing band, at or
#: above 30° they diverge enough that a single arrow can only be
#: bisector-aligned, not leg-parallel.
MATCH_BEND_RESCUE_MIN_BEND_DEG: float = 30.0


#: Maximum angular deviation (in degrees) between the candidate arrow's
#: bearing and the bisector of the two legs' bearings. The chart's
#: bisector signature is geometrically precise — a designer placing one
#: arrow at a bend draws its body along the angle bisector almost
#: exactly. The HTZUK→KNTRY→LLHZ case lands at 1.65° off. 15° gives
#: ample headroom for chart-print + extraction jitter without admitting
#: arrows that are essentially leg-parallel (which would have fwd-diff
#: ≈ bend_angle/2 from the bisector — well outside 15° for the bend
#: angles this gate considers).
MATCH_BEND_RESCUE_BISECTOR_TOL_DEG: float = 15.0


#: Maximum perpendicular distance (in nautical miles) from the
#: candidate arrow to the **closer** of the two adjacent legs' lines
#: (endpoint-clamped, same as the standard cross-track gate). Keeps
#: the rescue local to the bend — an arrow drifting far from both
#: legs is almost certainly labeling a different chart route. 0.5 nm
#: is wider than the standard 0.65 nm real-waypoint radius / 2 to
#: absorb cases where the bend arrow sits near one leg but well off
#: the other, while still rejecting arrows in the next corridor over.
MATCH_BEND_RESCUE_MAX_LEG_DIST_NM: float = 0.5


# ---------------------------------------------------------------------------
# Wide-corridor rescue constants — the third post-pass that handles charts
# where the route's waypoint chain sits on one edge of a wide airway and
# the altitude-arrow column is broadcast in the open space on the other
# edge (the classic HRTZ coastal corridor: SB 800 ft labels printed
# 0.8–1.8 nm west of the SFAIM→APOLN→…→TYONA chain, well outside the
# strict 0.65 nm primary radius).
# ---------------------------------------------------------------------------


#: Extended cross-track radius (in nautical miles) for the wide-corridor
#: rescue's candidate gate. Sized to cover the worst legitimate
#: on-segment 800 ft arrow on the HRTZ corridor (the ARENA→HTZUK label
#: at 1.78 nm cross-track), with a small headroom (~0.02 nm) before
#: starting to admit arrows from genuinely-different corridors. Per-
#: arrow competition + the tight bearing gate below keep the rescue
#: from smearing labels across nearby corridors.
MATCH_WIDE_CORRIDOR_RADIUS_NM: float = 1.8


#: Maximum fwd-diff (in degrees) between a candidate rescue arrow's
#: bearing and the segment bearing. Tighter than the primary 30°
#: parallel-tolerance budget because we're paying for a much wider
#: cross-track allowance — an arrow has to be clearly along the
#: corridor (not merely "vaguely parallel and in the area") to claim a
#: leg via this rescue. 20° catches every legitimate HRTZ coastal
#: label on the LLHZ→LLMZ ground truth, including SIRNI→NSHRM at
#: 15.7°, without admitting cross-corridor arrows that the primary
#: gate already filtered.
MATCH_WIDE_CORRIDOR_FWD_DIFF_DEG: float = 20.0


#: Cross-track radius (nautical miles) for *left*-side wide-corridor
#: rescue. The main rescue admits parallel-RIGHT arrows out to
#: :data:`MATCH_WIDE_CORRIDOR_RADIUS_NM` (1.8 nm) because the chart's
#: predominant convention prints the corridor's altitude column to the
#: right of the route line. Left-of-track labels do occur (e.g. the LSA
#: NIRYA→ZMGID westbound 1700 ft arrow printed ~0.77 nm south of the
#: route), but a left arrow admitted at the full 1.8 nm is far more
#: likely to be a neighbouring corridor's label, so the left side gets a
#: deliberately tight radius — just past the 0.65 nm primary gate, with
#: ~0.13 nm headroom over the NIRYA case. The same strict
#: :data:`MATCH_WIDE_CORRIDOR_FWD_DIFF_DEG` parallelism + zero-overshoot
#: gates still apply, so only an arrow clearly drawn ALONG this leg (not
#: merely near it) qualifies.
MATCH_WIDE_CORRIDOR_LEFT_RADIUS_NM: float = 0.90


#: Plausible CVFR altitude range. Excludes ground / spot heights (typically
#: < 300 ft) and IFR-only flight levels (we cap at FL95 ~ 9500 ft to admit
#: any plausible VFR ceiling without admitting four-digit obstacle heights).
_PLAUSIBLE_ALT_MIN_FT: int = 300
_PLAUSIBLE_ALT_MAX_FT: int = 9500

#: Altitude labels on Israeli CVFR charts are uniformly multiples of 100.
#: Filtering by this single rule eliminates almost all spot-height /
#: obstacle-elevation false positives (1138, 2431, 3346, …) without losing a
#: single CVFR altitude.
_ALTITUDE_STEP_FT: int = 100

#: Yellow arrows on the chart are tiny — roughly 6×6 to 24×24 pt at the
#: chart's native scale. Yellow fills above this size are almost always
#: airspace / terrain banding (TMA shading, MOA blocks, restricted regions),
#: which contain the wrong kind of numbers.
_MAX_ARROW_SIDE_PT: float = 28.0
_MIN_ARROW_SIDE_PT: float = 4.0

#: Aspect ratio gate — arrow shapes are at most 4× as long as wide. Yellow
#: rectangles much longer than that are usually scale-rule patches.
_MAX_ARROW_ASPECT_RATIO: float = 4.0

#: Maximum number of vector-path *items* in a real altitude arrow.
#:
#: A real CVFR altitude arrow is a simple notched-tail polygon — typically
#: 5–7 path items (one ``moveto`` plus a handful of ``lineto`` segments and
#: a closing path). Settlements on the chart, by contrast, are drawn as
#: amorphous yellow blobs with dozens of edges (Umm El Fahm has 43 path
#: items; larger towns reach into the hundreds). When such a settlement
#: blob's bbox happens to swallow a nearby altitude digit span — and the
#: blob's bbox happens to clear the size + aspect-ratio gates above —
#: the extractor used to emit a *phantom* altitude arrow whose bearing
#: (derived from the blob's largest concavity) was unrelated to any
#: real route direction.
#:
#: The cap is conservative: 15 items is roughly 2.5× the largest plausible
#: real-arrow item count, which gives ample headroom for unusually-decorated
#: arrows while still rejecting the multi-vertex settlement / lake / forest
#: blobs the same yellow-fill palette is shared with. Bump if a future
#: chart edition introduces ornate altitude labels that exceed this; never
#: drop below 10, as a notched bidirectional arrow can legitimately reach
#: into the high single digits.
_MAX_ARROW_PATH_ITEMS: int = 15

#: Path-item *kinds* (PyMuPDF's first-element tag on each item) that must
#: never appear in a real altitude arrow. Real arrows are pure polygons —
#: only ``'l'`` (line-to) plus the implicit ``'m'`` (move-to) and
#: ``'re'`` (rectangle) items. Holding-pattern symbols, by contrast, are
#: racetracks (two parallel straight sides + two semicircular ends) whose
#: ends are rendered as cubic Bézier curves — so their item list always
#: contains four ``'c'`` items (two per semicircle) interleaved with two
#: ``'l'`` items for the straight sides.
#:
#: Empirically (see ``scripts/debug_eiron_holding.py``): of 459 yellow
#: shapes on the north sheet that pass the size, aspect-ratio, and
#: path-item-count gates, exactly 7 contain Bézier items, and every one
#: of those 7 has the identical ``{'c': 4, 'l': 2}`` racetrack signature
#: at chart positions consistent with VFR holding patterns (Umm El Fahm
#: NE of EIRON, north of Megiddo, north of Tel Aviv, near Eilat, etc.).
#:
#: The bug this filter resolves: the EIRON-area holding pattern at
#: @(32.500, 35.040) was being emitted as a *bidirectional* 2500 ft
#: arrow (no concave tail notch ⇒ ``_arrow_bearing_pdf_deg`` returns
#: ``None`` ⇒ classified bidirectional), which the matcher would then
#: pick up for the EIRON.1→EIRON sub-leg of the LLIB→LLHZ reverse
#: route — even though there's no real 2500 ft arrow in that sub-leg
#: at all (the user flies the whole ZMGID→EIRON corridor at 3000 ft).
_FORBIDDEN_ARROW_PATH_KINDS: frozenset[str] = frozenset({"c", "qu"})

#: A "yellow-ish" arrow fill is high R, high G, low B. The chart's actual
#: arrow fill is around (1.0, 0.94, 0.33); we widen the band slightly to
#: tolerate any future palette tweaks.
_YELLOW_R_MIN: float = 0.85
_YELLOW_G_MIN: float = 0.75
_YELLOW_B_MAX: float = 0.55


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AltitudeArrow:
    """One CVFR altitude-arrow extracted from a chart PDF.

    All coordinates live in the **cropped pixmap's UV space** ``[0, 1]²`` —
    the same space the geo calibration's anchors were captured in. Combined
    with a :class:`SheetGeoCalibration` this maps cleanly to lat/lon, no
    further crop transform required.

    ``bearing_deg`` is true compass bearing in degrees (0 = north, 90 = east).
    Israeli CVFR charts are north-up to better than half a degree, so we
    treat PDF +x as east and PDF +y as south at extraction time and tag the
    bearing accordingly.

    ``bidirectional`` is True for dual-headed arrows — those drawn with a
    triangular tip on *both* short edges (no concave tail notch). The single
    altitude inside such an arrow applies to flight in both directions
    *along the arrow's body axis*. For bidirectional arrows ``bearing_deg``
    records that body-axis compass bearing (see
    :func:`_arrow_bidirectional_axis_bearing_pdf`): the matcher accepts the
    arrow for any segment that runs parallel **or** anti-parallel to the
    axis within the parallel-tolerance budget, and rejects it for segments
    that cross the axis (so e.g. a horizontal RIDNG↔ROKCH bidirectional
    arrow does not label our SW-going RIDNG→CLORE leg).

    ``altitudes_ft`` is a tuple sorted *top-to-bottom on the chart*, matching
    the chart's printed convention where the highest altitude is on top of a
    stacked label (e.g. ``(1600, 800)``). The route panel renders this with a
    line break between values so the cell preserves the visual stacking.
    """

    u: float
    v: float
    bearing_deg: float
    altitudes_ft: tuple[int, ...]
    bidirectional: bool = False


@dataclass(frozen=True)
class GeoAltitudeArrow:
    """An :class:`AltitudeArrow` projected through a calibrated chart.

    Built once per (arrows × calibration) pair before matching, so the per-
    segment matcher does cheap distance/bearing math on lat/lon directly
    instead of re-projecting on every comparison.

    See :class:`AltitudeArrow` for ``bidirectional`` semantics — preserved
    through the projection so the matcher can tell single- and dual-headed
    arrows apart.
    """

    lat: float
    lon: float
    bearing_deg: float
    altitudes_ft: tuple[int, ...]
    bidirectional: bool = False


# ---------------------------------------------------------------------------
# Filters & helpers
# ---------------------------------------------------------------------------


def _is_yellowish_fill(fill: tuple[float, float, float] | None) -> bool:
    """True for the printed-arrow yellow; False for unfilled drawings or any
    other colour. PyMuPDF returns ``None`` for stroked-only drawings."""
    if fill is None:
        return False
    if len(fill) < 3:
        return False
    r, g, b = float(fill[0]), float(fill[1]), float(fill[2])
    return r >= _YELLOW_R_MIN and g >= _YELLOW_G_MIN and b <= _YELLOW_B_MAX


def _is_plausible_altitude(text: str) -> int | None:
    """Parse a span's text as an altitude in feet, or return ``None`` to reject.

    Accepted: a 3- or 4-digit integer, multiple of 100, in the 300–9500 ft
    range. Rejected: anything with a leading sign, decimals, separators, or
    that fails any of the three constraints.
    """
    t = text.strip()
    if not t or not t.isdigit() or not 1 <= len(t) <= 5:
        return None
    n = int(t)
    if n < _PLAUSIBLE_ALT_MIN_FT or n > _PLAUSIBLE_ALT_MAX_FT:
        return None
    if n % _ALTITUDE_STEP_FT != 0:
        return None
    return n


def _bbox_centre(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) * 0.5, (bbox[1] + bbox[3]) * 0.5)


def _path_vertices(items: Iterable[Any]) -> list[tuple[float, float]]:
    """Flatten the per-segment items returned by ``page.get_drawings()`` into
    a single ordered vertex list.

    PyMuPDF reports each path segment as a tuple ``(opcode, *points)``: ``"l"``
    for a line (we take the endpoint), ``"c"`` for a cubic bezier (we keep all
    points so the centroid reflects the curve mass, not just the endpoints),
    and ``"re"`` for an axis-aligned rectangle (synthesise four corners).
    Any unknown opcode is skipped silently — the centroid is a sample
    statistic, not an exact reconstruction.
    """
    pts: list[tuple[float, float]] = []
    for it in items:
        if not it:
            continue
        op = it[0]
        if op == "l":
            try:
                p = it[2]
            except IndexError:
                continue
            pts.append((float(p.x), float(p.y)))
        elif op == "c":
            for q in it[1:]:
                try:
                    pts.append((float(q.x), float(q.y)))
                except (AttributeError, TypeError, ValueError):
                    pass
        elif op == "re":
            try:
                r = it[1]
                pts.extend(
                    [
                        (float(r.x0), float(r.y0)),
                        (float(r.x1), float(r.y0)),
                        (float(r.x1), float(r.y1)),
                        (float(r.x0), float(r.y1)),
                    ]
                )
            except (AttributeError, TypeError, ValueError):
                pass
    return pts


#: Smallest interior-angle deviation from straight that counts as a "real"
#: concave vertex on the arrow's polygon. The chart's altitude arrows are
#: drawn highway-sign-shaped — a rectangular body with a triangular tip on
#: one short edge and a triangular concave notch cut into the opposite edge.
#: The notch apex is the only meaningful concave vertex on a single-headed
#: arrow; this threshold ignores the small concave wobbles that fall out of
#: rounded-corner approximations and PDF stroking artefacts.
_MIN_CONCAVE_DEFLECTION_DEG: float = 25.0


def _polygon_signed_area(verts: list[tuple[float, float]]) -> float:
    """Twice the signed shoelace area for a closed polygon.

    Sign is positive for CCW polygons in math (Y-up) coordinates and negative
    for CW. Used as the orientation key when classifying convex vs concave
    vertices: the convex turn-direction matches the sign of the area.
    """
    a = 0.0
    n = len(verts)
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a


def _find_concave_apex_pdf(
    rect: fitz.Rect,
    items: Iterable[Any],
    *,
    min_deflection_deg: float = _MIN_CONCAVE_DEFLECTION_DEG,
) -> tuple[float, float] | None:
    """Find the most prominent concave vertex on the arrow's polygon path.

    For a typical highway-sign-shaped chart arrow there is exactly one
    significant concave vertex — the apex of the triangular notch cut into
    the *tail* edge. The vector from the bbox centre to that apex points
    AWAY from the arrow's heading, so callers can flip it to recover the
    direction of travel.

    Walks the polygon, classifies each vertex by the cross product of its
    incoming and outgoing edge vectors against the polygon's overall
    orientation (CW vs CCW). Concave = opposite sign. The concave vertex
    with the largest deflection from a straight line wins — that filters
    out the small wiggles that come from rounded-corner approximations.

    Returns ``None`` when:

    * the polygon is degenerate (fewer than 4 vertices, zero area), or
    * no concave vertex deflects by more than ``min_deflection_deg``.

    The second case indicates a **dual-headed arrow** — both short edges
    are convex tips, so there's no notch. Callers should treat such arrows
    as bidirectional altitudes.
    """
    verts = _path_vertices(items)
    n = len(verts)
    if n < 4:
        return None
    signed_area = _polygon_signed_area(verts)
    if abs(signed_area) < 1e-6:
        return None
    convex_sign = 1.0 if signed_area > 0 else -1.0
    min_deflection_rad = math.radians(min_deflection_deg)

    best_apex: tuple[float, float] | None = None
    best_dev = 0.0
    for i in range(n):
        prev_x, prev_y = verts[(i - 1) % n]
        curr_x, curr_y = verts[i]
        next_x, next_y = verts[(i + 1) % n]
        e1x = curr_x - prev_x
        e1y = curr_y - prev_y
        e2x = next_x - curr_x
        e2y = next_y - curr_y
        cross = e1x * e2y - e1y * e2x
        # Concave: cross has OPPOSITE sign from convex_sign.
        if cross * convex_sign >= 0:
            continue
        len1 = math.hypot(e1x, e1y)
        len2 = math.hypot(e2x, e2y)
        if len1 < 1e-9 or len2 < 1e-9:
            continue
        dot = (e1x * e2x + e1y * e2y) / (len1 * len2)
        dot = max(-1.0, min(1.0, dot))
        deflection = math.acos(dot)  # 0 = straight, π = full reverse
        if deflection > best_dev:
            best_dev = deflection
            best_apex = (curr_x, curr_y)

    if best_apex is None or best_dev < min_deflection_rad:
        return None
    return best_apex


def _arrow_bearing_pdf_deg(
    rect: fitz.Rect,
    items: Iterable[Any],
) -> float | None:
    """Estimate the arrow's compass heading from its tail-notch geometry.

    Algorithm:

    1. Find the polygon's most-deflected concave vertex — the apex of the
       triangular notch on the chart's highway-sign-shaped arrow tail.
    2. The direction from the bbox centre to that apex points along the
       arrow's *tail* axis, i.e. *opposite* the heading.
    3. Heading = bearing of ``(bbox_centre - notch_apex)``.

    PDF page coordinates have +x east and +y south (page-down on a
    north-up chart), so an offset ``(dx, dy)`` in PDF space converts to a
    compass bearing via ``atan2(dx, -dy)`` (north = 0°, east = 90°).

    Returns ``None`` when no significant concave vertex is found — that's
    the dual-headed arrow signature, and callers route those through the
    matcher's bidirectional path instead of trying to assign a heading.
    """
    apex = _find_concave_apex_pdf(rect, items)
    if apex is None:
        return None
    cx = (rect.x0 + rect.x1) * 0.5
    cy = (rect.y0 + rect.y1) * 0.5
    apex_x, apex_y = apex
    # Tip direction = away from notch apex.
    dx = cx - apex_x
    dy = cy - apex_y
    if math.hypot(dx, dy) < 1e-9:
        return None
    return (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0


def _arrow_tail_anchor_pdf(
    rect: fitz.Rect,
    items: Iterable[Any],
) -> tuple[float, float] | None:
    """Return the arrow's tail anchor in PDF-page coordinates.

    The tail is where the arrow attaches to the printed route line; that's
    the point we want to test for proximity to a route segment. The bbox
    centre is biased toward the tip by ~half the arrow's long axis (since
    the arrow extends from the tail toward the tip in one direction only),
    so matching against the bbox centre puts arrows ~0.3–0.5 nm off the
    chart's actual route line at 1:500K.

    Implementation: walk a ray from the bbox centre toward the notch apex
    (i.e. AWAY from the heading recovered by :func:`_arrow_bearing_pdf_deg`)
    and intersect it with the four bbox edges. The smallest positive ``t``
    lands on the tail-side edge of the bbox.

    Returns ``None`` when no concave apex is found — dual-headed arrows
    have no obvious tail and callers should anchor them at the bbox centre
    instead (see :func:`_arrow_bidirectional_anchor_pdf`).
    """
    apex = _find_concave_apex_pdf(rect, items)
    if apex is None:
        return None
    cx = (rect.x0 + rect.x1) * 0.5
    cy = (rect.y0 + rect.y1) * 0.5
    apex_x, apex_y = apex
    # Unit vector pointing from bbox centre TOWARD the notch apex (= tail).
    udx = apex_x - cx
    udy = apex_y - cy
    norm = math.hypot(udx, udy)
    if norm < 1e-9:
        return None
    udx /= norm
    udy /= norm

    candidates: list[float] = []
    if udx > 1e-12:
        candidates.append((rect.x1 - cx) / udx)
    elif udx < -1e-12:
        candidates.append((rect.x0 - cx) / udx)
    if udy > 1e-12:
        candidates.append((rect.y1 - cy) / udy)
    elif udy < -1e-12:
        candidates.append((rect.y0 - cy) / udy)
    positives = [c for c in candidates if c > 0.0]
    if not positives:
        return cx, cy
    t = min(positives)
    return cx + udx * t, cy + udy * t


def _arrow_bidirectional_anchor_pdf(
    rect: fitz.Rect,
) -> tuple[float, float]:
    """Anchor a dual-headed arrow at its bbox centre.

    Dual-headed arrows have no notch and therefore no preferred tail edge —
    they sit *on* the route line rather than adjacent to it. The bbox
    centre lands on the route line at 1:500K, which is exactly what the
    matcher's distance gate wants.
    """
    return ((rect.x0 + rect.x1) * 0.5, (rect.y0 + rect.y1) * 0.5)


def _arrow_bidirectional_axis_bearing_pdf(
    items: Iterable[Any],
) -> float | None:
    """Estimate the body-axis compass bearing of a dual-headed arrow.

    A bidirectional altitude arrow on the chart has two triangular tips on
    opposite ends of a rectangular body. The two tip apexes are the
    *furthest-apart* vertices on the polygon path; the chord that connects
    them lies along the arrow's body axis, which is the chart's labelling
    axis (the arrow points along it in both directions).

    Algorithm:

    1. Flatten the path into a vertex list via :func:`_path_vertices`.
    2. Find the pair of vertices ``(p_i, p_j)`` with the largest squared
       distance — that's the tip-to-tip chord. ``O(n²)`` over the
       ``≤ _MAX_ARROW_PATH_ITEMS`` vertices is trivially cheap.
    3. Convert the chord's PDF vector ``(dx, dy)`` to a compass bearing.
       PDF coordinates have +x east and +y south, so a chart-up vector
       maps to ``atan2(dx, -dy)``.

    Returns a bearing in ``[0°, 360°)``. The matcher treats the axis as
    direction-agnostic (collapsing modulo 180°), so it doesn't matter
    which tip the chord "points from"; both endpoints give axes that
    compare identically once collapsed.

    Returns ``None`` only for degenerate paths (fewer than two distinct
    vertices, or a path where every vertex coincides numerically) —
    callers should skip emitting such arrows entirely rather than fall
    back to a meaningless placeholder bearing that would let the arrow
    match any segment direction.
    """
    verts = _path_vertices(items)
    n = len(verts)
    if n < 2:
        return None

    best_d2 = -1.0
    best_dx = 0.0
    best_dy = 0.0
    for i in range(n):
        xi, yi = verts[i]
        for j in range(i + 1, n):
            xj, yj = verts[j]
            dx = xj - xi
            dy = yj - yi
            d2 = dx * dx + dy * dy
            if d2 > best_d2:
                best_d2 = d2
                best_dx = dx
                best_dy = dy

    if best_d2 < 1e-12:
        return None

    return (math.degrees(math.atan2(best_dx, -best_dy)) + 360.0) % 360.0


def _flatten_numeric_spans(page: fitz.Page) -> list[dict[str, Any]]:
    """Extract every digit-only text span on the page with its bbox.

    Used to find altitude labels: each survivor is later checked for
    containment inside an arrow rect. We deliberately skip non-digit spans
    here (place names, route codes, font samples) because they can't
    represent a flight altitude no matter where they land geometrically.
    """
    out: list[dict[str, Any]] = []
    raw = page.get_text("dict")
    for block in raw.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text or not text.strip().isdigit():
                    continue
                out.append(
                    {
                        "text": text.strip(),
                        "bbox": tuple(span.get("bbox", (0, 0, 0, 0))),
                    }
                )
    return out


def _is_arrow_shape(rect: fitz.Rect) -> bool:
    """Cheap shape gate before we bother walking the path geometry."""
    w = float(rect.width)
    h = float(rect.height)
    if w < _MIN_ARROW_SIDE_PT or h < _MIN_ARROW_SIDE_PT:
        return False
    if w > _MAX_ARROW_SIDE_PT or h > _MAX_ARROW_SIDE_PT:
        return False
    long_side = max(w, h)
    short_side = max(min(w, h), 1e-6)
    return (long_side / short_side) <= _MAX_ARROW_ASPECT_RATIO


def _pdf_pt_to_pixmap_uv(
    px: float,
    py: float,
    *,
    render_dpi: float,
    crop: CropMeta,
) -> tuple[float, float] | None:
    """Convert a PDF-page point in points to cropped-pixmap UV.

    Returns ``None`` when the projected pixel falls outside the cropped
    pixmap's rect — that arrow lives in a margin we trimmed away, so by
    construction it can't be on the calibrated chart and we drop it.
    """
    if crop.cropped_w <= 0 or crop.cropped_h <= 0:
        return None
    z = render_dpi / 72.0
    src_x = px * z
    src_y = py * z
    cx = src_x - crop.offset_x
    cy = src_y - crop.offset_y
    u = cx / crop.cropped_w
    v = cy / crop.cropped_h
    if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
        return None
    return u, v


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_altitude_arrows(
    pdf_path: Path | str,
    *,
    render_dpi: float,
    crop: CropMeta,
) -> list[AltitudeArrow]:
    """Walk the PDF and return one :class:`AltitudeArrow` per altitude label.

    Both ``render_dpi`` and ``crop`` come from the rendering pipeline (see
    :mod:`cvfr_routemaster.map_loader`) and together describe the exact
    PDF-pt → cropped-pixmap-UV transform the geo calibration was captured
    against. Mismatching them is silently safe — the projection's bounds
    check will simply drop out-of-range arrows — but the result will be
    wrong, so callers should plumb the same values used for rendering.

    The implementation walks ``page.get_drawings()`` once, building per-arrow
    candidates, and ``page.get_text("dict")`` once for the digit spans. The
    inner ``arrow × spans`` containment test is O(M*N) but with M ≈ 3,400 and
    N ≈ 2,000 on a typical chart, finishes well under a second on a laptop —
    which is fine because this whole function runs offline once per PDF and
    is then cached.
    """
    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)
    try:
        if doc.page_count < 1:
            return []
        page = doc[0]
        drawings = page.get_drawings()
        numeric_spans = _flatten_numeric_spans(page)

        out: list[AltitudeArrow] = []
        for d in drawings:
            if not _is_yellowish_fill(d.get("fill")):
                continue
            rect = d.get("rect")
            if rect is None or not isinstance(rect, fitz.Rect):
                continue
            if not _is_arrow_shape(rect):
                continue
            # Path-complexity gate — see ``_MAX_ARROW_PATH_ITEMS``. Yellow
            # settlement blobs (Umm El Fahm and friends) clear the size +
            # aspect gates above but carry an order of magnitude more
            # vector-path items than a real arrow does, and their bboxes
            # routinely overlap the bbox of a nearby real altitude arrow —
            # which used to produce a phantom altitude with a bogus bearing
            # derived from the blob's concavity. Reject anything more
            # complex than a notched-tail polygon here, before we waste
            # work on bearing extraction or digit-containment tests.
            items = d.get("items", [])
            if len(items) > _MAX_ARROW_PATH_ITEMS:
                continue
            # Curve-segment gate — see ``_FORBIDDEN_ARROW_PATH_KINDS``. Real
            # CVFR altitude arrows are 100% straight-line polygons (only
            # ``'l'`` items in the path). Holding-pattern symbols share the
            # arrow-yellow palette and clear every other gate above, but
            # their racetrack shape is rendered as semicircular ``'c'``
            # cubic Béziers at each end. A single curve item is enough
            # signal to reject — the EIRON-area phantom that motivated
            # this filter has the canonical ``{'c': 4, 'l': 2}`` signature.
            if any(it and it[0] in _FORBIDDEN_ARROW_PATH_KINDS for it in items):
                continue

            # Harvest plausible altitude tokens whose bbox centre lies inside
            # the arrow's rect. Sorting by the span's *top* y matches the
            # chart's "highest altitude on top" stacking convention.
            inside: list[tuple[int, float]] = []  # (altitude_ft, top_y)
            for s in numeric_spans:
                bb = s["bbox"]
                cx, cy = _bbox_centre(bb)
                if not (rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1):
                    continue
                alt = _is_plausible_altitude(s["text"])
                if alt is None:
                    continue
                inside.append((alt, float(bb[1])))

            if not inside:
                continue

            inside.sort(key=lambda pair: pair[1])
            altitudes = tuple(alt for alt, _ in inside)

            # Drop within-arrow altitude outliers — values that are much
            # smaller than the largest altitude in the same arrow are almost
            # certainly stray non-altitude numbers (obstacle elevations,
            # radio frequencies, road numbers) whose bbox happened to land
            # inside the yellow arrow rect. Real CVFR altitude bands stack
            # related values (e.g. 1600/800, 2000/1000) — ratios under 2:1.
            # We accept ratios up to 3:1 to give safety margin without
            # admitting a stray "400" sitting inside a "1500" arrow.
            if len(altitudes) >= 2:
                hi = max(altitudes)
                threshold = hi // 3
                kept = tuple(a for a in altitudes if a >= threshold)
                if kept:
                    altitudes = kept

            bearing = _arrow_bearing_pdf_deg(rect, items)
            bidirectional = bearing is None
            if bidirectional:
                # Dual-headed arrow: no concave tail notch. Anchor at the
                # bbox centre (the route line passes through it) and
                # record the arrow's *body axis* compass bearing — the
                # tip-to-tip chord through the polygon. The matcher
                # treats this axis as direction-agnostic but still
                # rejects bidirectional arrows whose body runs across
                # the segment direction (e.g. a horizontal RIDNG↔ROKCH
                # arrow shouldn't label our SW-going RIDNG→CLORE leg).
                anchor_x, anchor_y = _arrow_bidirectional_anchor_pdf(rect)
                axis_bearing = _arrow_bidirectional_axis_bearing_pdf(items)
                if axis_bearing is None:
                    # Degenerate polygon with no resolvable axis: better
                    # to drop the arrow than emit one that would match
                    # any direction unconditionally and likely steal
                    # a competitive match from the true labelling arrow.
                    continue
                bearing_value = axis_bearing
            else:
                # Single-headed arrow: anchor on the tail edge so the
                # matcher's distance gate measures arrow-to-route-line
                # along the chart's actual labelling.
                tail = _arrow_tail_anchor_pdf(rect, items)
                if tail is None:
                    # Should not happen given _arrow_bearing_pdf_deg
                    # succeeded, but keep belt-and-braces.
                    continue
                anchor_x, anchor_y = tail
                bearing_value = float(bearing)

            uv = _pdf_pt_to_pixmap_uv(
                anchor_x,
                anchor_y,
                render_dpi=render_dpi,
                crop=crop,
            )
            if uv is None:
                continue

            out.append(
                AltitudeArrow(
                    u=float(uv[0]),
                    v=float(uv[1]),
                    bearing_deg=bearing_value,
                    altitudes_ft=altitudes,
                    bidirectional=bidirectional,
                )
            )
        return out
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Matching arrows to route segments
# ---------------------------------------------------------------------------


def project_arrows_to_lonlat(
    arrows: Iterable[AltitudeArrow],
    cal: SheetGeoCalibration,
) -> list[GeoAltitudeArrow]:
    """Project each arrow's pixmap UV through ``cal`` to lat/lon.

    Run once per (arrow-set, calibration) pair before matching. Errors from a
    ``ZeroDivisionError`` in the inverse fit (an anchor degeneracy) drop the
    offending arrow rather than raising, so a partially calibrated session
    still gets best-effort altitudes for the parts it can project.
    """
    out: list[GeoAltitudeArrow] = []
    for a in arrows:
        try:
            lon, lat = cal.uv_to_lonlat(a.u, a.v)
        except (ValueError, ZeroDivisionError):
            continue
        out.append(
            GeoAltitudeArrow(
                lat=float(lat),
                lon=float(lon),
                bearing_deg=float(a.bearing_deg),
                altitudes_ft=tuple(a.altitudes_ft),
                bidirectional=bool(a.bidirectional),
            )
        )
    return out


def _great_circle_distance_to_segment_nm(
    seg_a_lat: float,
    seg_a_lon: float,
    seg_b_lat: float,
    seg_b_lon: float,
    p_lat: float,
    p_lon: float,
) -> float:
    """Cross-track distance in nautical miles, clamped to the segment endpoints.

    Uses the small-angle local-tangent-plane approximation (km per degree
    latitude * cos(lat) for longitude) which is well under 1% accurate for
    the < 200 nm legs CVFR routes deal with. Beyond either endpoint, the
    return value is the great-circle distance to that endpoint, which is
    what the matcher wants — an arrow living past the segment's end belongs
    to a different segment, not this one.
    """
    from cvfr_routemaster.route import great_circle_distance_nm

    # Convert to a local equirectangular plane centred on the segment midpoint.
    mid_lat = 0.5 * (seg_a_lat + seg_b_lat)
    cos_mid = math.cos(math.radians(mid_lat))
    nm_per_deg_lat = 60.0  # 1 nm = 1 minute of latitude (definition).
    nm_per_deg_lon = 60.0 * cos_mid

    ax = (seg_a_lon - p_lon) * nm_per_deg_lon
    ay = (seg_a_lat - p_lat) * nm_per_deg_lat
    bx = (seg_b_lon - p_lon) * nm_per_deg_lon
    by = (seg_b_lat - p_lat) * nm_per_deg_lat
    abx = bx - ax
    aby = by - ay
    seg_len2 = abx * abx + aby * aby
    if seg_len2 < 1e-9:
        return great_circle_distance_nm(p_lat, p_lon, seg_a_lat, seg_a_lon)

    # Parameter t of the projection onto the segment, clamped to [0, 1].
    apx = -ax
    apy = -ay
    t = max(0.0, min(1.0, (apx * abx + apy * aby) / seg_len2))

    if t <= 0.0:
        return great_circle_distance_nm(p_lat, p_lon, seg_a_lat, seg_a_lon)
    if t >= 1.0:
        return great_circle_distance_nm(p_lat, p_lon, seg_b_lat, seg_b_lon)

    proj_x = ax + t * abx
    proj_y = ay + t * aby
    return math.hypot(proj_x, proj_y)


def _distance_and_overshoot_to_segment_nm(
    seg_a_lat: float,
    seg_a_lon: float,
    seg_b_lat: float,
    seg_b_lon: float,
    p_lat: float,
    p_lon: float,
) -> tuple[float, float]:
    """Same as :func:`_great_circle_distance_to_segment_nm`, additionally
    returning the *along-segment* overshoot in nautical miles.

    ``overshoot_nm`` is:

    * ``0`` when the perpendicular foot from ``P`` onto segment ``AB``
      lies within the segment range ``[A, B]`` (i.e. the projection
      parameter ``t`` is in ``[0, 1]``).
    * Positive otherwise, equal to how far past the nearest endpoint
      the foot would lie along the segment direction. For ``t < 0``
      this is ``-t * seg_length``; for ``t > 1`` it is
      ``(t - 1) * seg_length``.

    The caller uses ``overshoot_nm`` as a separate gate from the
    cross-track distance: a small overshoot is unavoidable due to
    extraction / calibration noise, but a large overshoot means the
    arrow is *past* the segment's end and belongs (by chart
    convention) to whatever route continues from that endpoint, not
    to OUR leg.
    """
    from cvfr_routemaster.route import great_circle_distance_nm

    mid_lat = 0.5 * (seg_a_lat + seg_b_lat)
    cos_mid = math.cos(math.radians(mid_lat))
    nm_per_deg_lat = 60.0
    nm_per_deg_lon = 60.0 * cos_mid

    ax = (seg_a_lon - p_lon) * nm_per_deg_lon
    ay = (seg_a_lat - p_lat) * nm_per_deg_lat
    bx = (seg_b_lon - p_lon) * nm_per_deg_lon
    by = (seg_b_lat - p_lat) * nm_per_deg_lat
    abx = bx - ax
    aby = by - ay
    seg_len2 = abx * abx + aby * aby
    if seg_len2 < 1e-9:
        return (
            great_circle_distance_nm(p_lat, p_lon, seg_a_lat, seg_a_lon),
            0.0,
        )

    apx = -ax
    apy = -ay
    raw_t = (apx * abx + apy * aby) / seg_len2
    seg_len = math.sqrt(seg_len2)

    if raw_t <= 0.0:
        return (
            great_circle_distance_nm(p_lat, p_lon, seg_a_lat, seg_a_lon),
            -raw_t * seg_len,
        )
    if raw_t >= 1.0:
        return (
            great_circle_distance_nm(p_lat, p_lon, seg_b_lat, seg_b_lon),
            (raw_t - 1.0) * seg_len,
        )

    proj_x = ax + raw_t * abx
    proj_y = ay + raw_t * aby
    return (math.hypot(proj_x, proj_y), 0.0)


def _circular_diff_deg(a: float, b: float) -> float:
    """Smallest absolute angular difference between two compass bearings."""
    d = (a - b) % 360.0
    if d > 180.0:
        d = 360.0 - d
    return d


def _axis_diff_deg(a: float, b: float) -> float:
    """Smallest angle between two **undirected** axes (modulo 180°).

    Returned value is in ``[0°, 90°]``. Use this when comparing a
    bidirectional altitude arrow's body axis against a route-segment
    bearing — the arrow labels both flight directions along its axis, so
    a 180°-offset segment is just as "parallel" as a 0°-offset one.

    Equivalent to ``min(_circular_diff_deg(a, b), 180 - _circular_diff_deg(a, b))``
    but spelt out so call sites read self-documenting.
    """
    d = _circular_diff_deg(a, b)
    return min(d, 180.0 - d)


def _bisector_bearing_deg(b1: float, b2: float) -> float:
    """Return the bearing (in degrees, ``[0, 360)``) that bisects the
    smaller angle between two compass bearings.

    Implemented via unit-vector averaging in compass-bearing space
    (north = 0°, east = 90°, clockwise) so wraparound near 0°/360° is
    handled correctly. The unit vector for bearing ``b`` is
    ``(sin b, cos b)`` so ``atan2(Σ sin, Σ cos)`` gives the bisector.

    Degenerate case: when ``b1`` and ``b2`` are exactly antipodal
    (180° apart) the vector sum is ``(0, 0)`` and the bisector is
    geometrically undefined (any of the two perpendicular directions
    is equally valid). We return ``b1 + 90`` as a deterministic,
    side-stable choice; this case never actually arises in the
    bend-arrow rescue because the min-bend gate filters bend angles
    well below 180°, but the helper handles it for robustness.

    Used by the shared-bend altitude-arrow rescue to detect when a
    single chart arrow placed at a route turn labels both adjacent
    legs (its bearing matches the bisector of the two leg bearings).
    """
    r1 = math.radians(b1)
    r2 = math.radians(b2)
    sx = math.sin(r1) + math.sin(r2)
    cy = math.cos(r1) + math.cos(r2)
    if abs(sx) < 1e-9 and abs(cy) < 1e-9:
        return (b1 + 90.0) % 360.0
    return math.degrees(math.atan2(sx, cy)) % 360.0


def _arrow_side_of_segment(
    seg_from_lat: float,
    seg_from_lon: float,
    seg_to_lat: float,
    seg_to_lon: float,
    arrow_lat: float,
    arrow_lon: float,
) -> int:
    """Return +1 if the arrow lies LEFT of the FROM→TO segment direction,
    -1 if RIGHT, 0 if it sits on the line.

    Uses the 2-D cross product of the segment direction vector and the
    "FROM-to-arrow" offset vector. We work in raw (lon, lat) units rather
    than converting to a metric tangent plane: the cross product's *sign*
    is invariant under any axis-aligned scaling (and longitude-vs-latitude
    is precisely that), so the side classification is correct at Israeli
    latitudes without a reprojection.

    Convention check (rotation 90° clockwise from forward = right side):
        - Going north,  forward = (0, +ΔLat) → right is east (+ΔLon).
        - Going east,   forward = (+ΔLon, 0) → right is south (-ΔLat).
        - Going south,  forward = (0, -ΔLat) → right is west (-ΔLon).
        - Going west,   forward = (-ΔLon, 0) → right is north (+ΔLat).
    All four cases give cross < 0 for an arrow sitting on the right of the
    flight path, which matches the chart's printed convention.
    """
    seg_vx = seg_to_lon - seg_from_lon
    seg_vy = seg_to_lat - seg_from_lat
    off_x = arrow_lon - seg_from_lon
    off_y = arrow_lat - seg_from_lat
    cross = seg_vx * off_y - seg_vy * off_x
    if cross > 1e-12:
        return 1
    if cross < -1e-12:
        return -1
    return 0


# --- internal helpers for the route-level competitive matcher ----------------


# Direction-class ranks used both for filtering and for breaking ties between
# segments competing for the same arrow. Lower is better.
_CLASS_PARALLEL_RIGHT = 0
_CLASS_PARALLEL_LEFT = 1
_CLASS_BIDIRECTIONAL = 2


@dataclass(frozen=True)
class _ArrowSegFit:
    """How well a single arrow fits a single segment.

    Captured once per (arrow, segment) pair so the per-segment selection
    and the global "which segment owns this arrow?" pass can share work.
    """

    class_rank: int  # _CLASS_PARALLEL_RIGHT / _LEFT / _BIDIRECTIONAL
    distance_nm: float
    fwd_diff_deg: float  # 0 for bidirectional (no heading)
    # Along-segment overshoot in nm (see `_distance_and_overshoot_to_segment_nm`).
    # Exactly ``0.0`` when the perpendicular foot from the arrow onto the
    # segment lies inside the segment endpoints; strictly positive when the
    # foot projects past either endpoint along the segment direction.
    # Used by ``_fit_key`` to tier on-segment matches ahead of past-endpoint
    # matches *within the same direction class* — see ``_fit_key`` docstring
    # for the full rationale.
    overshoot_nm: float = 0.0


def _evaluate_arrow_for_segment(
    arrow: GeoAltitudeArrow,
    segment: RouteSegment,
    seg_bearing_deg: float,
    *,
    radius_nm: float,
    parallel_tol_deg: float,
    max_endpoint_overshoot_nm: float = MATCH_MAX_ENDPOINT_OVERSHOOT_NM,
    past_endpoint_parallel_tol_deg: float = (
        MATCH_PARALLEL_TOL_DEG_PAST_ENDPOINT
    ),
) -> _ArrowSegFit | None:
    """Return how well ``arrow`` fits ``segment``, or ``None`` if it doesn't.

    Applies the same gates as the legacy per-segment matcher — radius,
    parallel-only direction filter, and side classification — plus
    two complementary past-endpoint gates so arrows whose perpendicular
    foot projects past either endpoint can't quietly claim this leg's
    altitude when they actually label the chart route continuing past
    that endpoint:

    * **Overshoot gate** (``max_endpoint_overshoot_nm``) — kills arrows
      that are substantively past an endpoint regardless of bearing.
    * **Past-endpoint parallel-tolerance gate**
      (``past_endpoint_parallel_tol_deg``) — kills past-endpoint
      arrows that are only loosely aligned with the segment direction.
      An arrow with the foot ON the segment still gets the wider
      ``parallel_tol_deg`` budget because chart-print + extraction
      jitter on a genuine OUR-leg arrow can reach the higher single
      digits. A past-endpoint arrow doesn't get that courtesy because
      the foot-past-endpoint signature is itself already a "this is
      probably a different leg's label" indicator that we're trying
      to soften — we only let one through when its bearing argues
      very strongly that it really is OUR leg's continuation.

    Result is a pure data record so the route-level matcher can
    compare fits across segments.
    """
    d, overshoot = _distance_and_overshoot_to_segment_nm(
        segment.from_point.lat,
        segment.from_point.lon,
        segment.to_point.lat,
        segment.to_point.lon,
        arrow.lat,
        arrow.lon,
    )
    if d > radius_nm:
        return None
    if overshoot > max_endpoint_overshoot_nm:
        return None
    if arrow.bidirectional:
        # Dual-headed arrows label flight in *both* directions along
        # their body axis. Accept only when the segment runs parallel
        # OR anti-parallel to that axis — an arrow whose body runs
        # across our segment direction (e.g. an E-W bidirectional arrow
        # at a N-S route segment) is labelling a different corridor
        # that just happens to sit near our line, and must be rejected.
        # Bidirectional fits still go into their own class so any
        # directional candidate beats them in competition.
        axis_diff = _axis_diff_deg(arrow.bearing_deg, seg_bearing_deg)
        if axis_diff > parallel_tol_deg:
            return None
        if overshoot > 0.0 and axis_diff > past_endpoint_parallel_tol_deg:
            return None
        return _ArrowSegFit(
            class_rank=_CLASS_BIDIRECTIONAL,
            distance_nm=d,
            fwd_diff_deg=axis_diff,
            overshoot_nm=overshoot,
        )
    fwd_diff = _circular_diff_deg(arrow.bearing_deg, seg_bearing_deg)
    if fwd_diff > parallel_tol_deg:
        # Anti-parallel (180° off) or perpendicular crossing — the chart's
        # extraction made the bearing reliable, so we reject outright.
        return None
    if overshoot > 0.0 and fwd_diff > past_endpoint_parallel_tol_deg:
        # Past-endpoint arrow that isn't extremely-tightly aligned with
        # the segment bearing. See the function docstring + the
        # ``MATCH_PARALLEL_TOL_DEG_PAST_ENDPOINT`` rationale for why
        # this needs to be tighter than the on-segment 30° budget.
        return None
    side = _arrow_side_of_segment(
        segment.from_point.lat,
        segment.from_point.lon,
        segment.to_point.lat,
        segment.to_point.lon,
        arrow.lat,
        arrow.lon,
    )
    return _ArrowSegFit(
        class_rank=(
            _CLASS_PARALLEL_RIGHT if side < 0 else _CLASS_PARALLEL_LEFT
        ),
        distance_nm=d,
        fwd_diff_deg=fwd_diff,
        overshoot_nm=overshoot,
    )


def _fit_score(fit: _ArrowSegFit) -> float:
    """Numeric badness for ranking arrows *within the same class*.

    Combines cross-track distance and parallel-bearing diff so that an
    arrow which is geometrically closer **and** more strictly parallel
    beats one that's only one-or-the-other. The weight is calibrated so
    that 25° of fwd-diff is worth ~0.25 nm of cross-track — enough to
    consistently route an arrow to its true segment when an adjacent leg
    sits within radius but at a sharper bearing offset.
    """
    return fit.distance_nm + MATCH_FWD_DIFF_SCORE_WEIGHT * fit.fwd_diff_deg


_ONSEG_TIER_ON_SEGMENT = 0
_ONSEG_TIER_PAST_ENDPOINT = 1


def _onseg_tier(fit: _ArrowSegFit) -> int:
    """Tier "foot-on-segment" ahead of "foot-past-endpoint" within a class.

    Exposed as a tiny helper so the rule is named where it's enforced and
    the test suite can pin both halves of the tier explicitly. The cutoff
    is strict (``> 0.0``) because :func:`_distance_and_overshoot_to_segment_nm`
    returns *exactly* ``0.0`` when the perpendicular foot lies inside the
    segment endpoints — no floating-point fuzz to worry about.
    """
    if fit.overshoot_nm > 0.0:
        return _ONSEG_TIER_PAST_ENDPOINT
    return _ONSEG_TIER_ON_SEGMENT


def _fit_key(fit: _ArrowSegFit) -> tuple[int, int, float]:
    """Composite ``(class_rank, on-segment tier, score)`` used for both
    per-segment ranking and cross-segment competitive assignment.

    The tier sits *between* ``class_rank`` and the numeric score on
    purpose:

    * **Within the same direction class**, an arrow whose perpendicular
      foot lies *along* the segment beats any arrow whose foot would
      project *past* the segment's end — even when the past-endpoint
      arrow is closer cross-track. By chart convention, an arrow
      labels the route flying over (or paralleling) it, not the route
      that ends just before it; without this tier, a "next leg" label
      sitting right at our shared waypoint can occasionally out-score
      our own leg's genuine arrow whose body is printed mid-segment
      (canonical case: SORES→SHARG, where the 3300 arrow at Eyal
      Junction is on-segment and the 2300 arrow at SHARG itself
      projects just past SHARG into the SHARG→LTRUN continuation).
    * **The tier is secondary to ``class_rank``**, so a right-of-track
      past-endpoint arrow still beats a left-of-track on-segment arrow.
      The side-of-track signal is a stronger same-direction indicator
      than the along-vs-past signal, so we don't want to override it.
    * **A solitary past-endpoint candidate still wins by default.**
      ``min`` over its single fit picks tier 1, and that's still the
      best (only) answer. The change only matters when an on-segment
      alternative is in the same ``class_rank`` and otherwise also
      passes the existing gates (radius, parallel tolerance,
      past-endpoint overshoot, past-endpoint stricter parallel
      tolerance). Those gates already filter out the wildly-past or
      wildly-misaligned candidates before we get here.
    """
    return (fit.class_rank, _onseg_tier(fit), _fit_score(fit))


def _segment_radius_nm(
    segment: RouteSegment,
    *,
    waypoint_radius_nm: float,
    intermediate_radius_nm: float,
) -> float:
    """Pick the cross-track radius gate for ``segment``.

    Real-waypoint legs (both endpoints have ``RoutePoint.waypoint is not None``)
    use the strict ``waypoint_radius_nm`` because their endpoints come from
    the chart's official waypoint list. Any leg with at least one free-
    clicked intermediate uses the looser ``intermediate_radius_nm`` to
    absorb the user-discretion error in the click. See
    :data:`MATCH_RADIUS_NM_INTERMEDIATE` for the full rationale and
    competitive-matching invariant that keeps this safe.
    """
    if (
        segment.from_point.waypoint is not None
        and segment.to_point.waypoint is not None
    ):
        return waypoint_radius_nm
    return intermediate_radius_nm


def match_altitudes_for_route(
    segments: list[RouteSegment],
    arrows_by_sheet: dict[str, list[GeoAltitudeArrow]],
    *,
    radius_nm: float = MATCH_RADIUS_NM,
    intermediate_radius_nm: float = MATCH_RADIUS_NM_INTERMEDIATE,
    parallel_tol_deg: float = MATCH_PARALLEL_TOL_DEG,
    stack_radius_nm: float = MATCH_STACK_RADIUS_NM,
    stack_bearing_tol_deg: float = MATCH_STACK_BEARING_TOL_DEG,
    max_endpoint_overshoot_nm: float = MATCH_MAX_ENDPOINT_OVERSHOOT_NM,
    past_endpoint_parallel_tol_deg: float = (
        MATCH_PARALLEL_TOL_DEG_PAST_ENDPOINT
    ),
    bend_rescue_min_bend_deg: float = MATCH_BEND_RESCUE_MIN_BEND_DEG,
    bend_rescue_bisector_tol_deg: float = (
        MATCH_BEND_RESCUE_BISECTOR_TOL_DEG
    ),
    bend_rescue_max_leg_dist_nm: float = MATCH_BEND_RESCUE_MAX_LEG_DIST_NM,
    wide_corridor_radius_nm: float = MATCH_WIDE_CORRIDOR_RADIUS_NM,
    wide_corridor_left_radius_nm: float = MATCH_WIDE_CORRIDOR_LEFT_RADIUS_NM,
    wide_corridor_fwd_diff_deg: float = MATCH_WIDE_CORRIDOR_FWD_DIFF_DEG,
) -> list[tuple[int, ...]]:
    """Match altitude arrows to *every* segment of a planned route at once.

    Returns one altitude tuple per segment, in the same order as
    ``segments``. An empty tuple means "unknown" for that leg.

    Three pieces of intelligence sit on top of the per-segment matcher:

    1. **Competitive matching** — each arrow is assigned to the single
       segment it fits best (lowest ``(class_rank, score)`` among the
       segments it passes the gates for). This kills the otherwise-
       common false positive where an arrow that legitimately labels
       leg X also fits inside leg X+1's tube near the shared waypoint
       (e.g. the inverse route's ``3250N03457E → DAROM`` was stealing
       ``DAROM → HOTRM``'s southbound 2000 arrow before competition was
       added). Without this, an arrow can't simultaneously be the chart
       label for two different legs.

    2. **Stacked alternates** — once a segment's primary arrow is
       picked, any other arrow within ``stack_radius_nm`` of the primary
       (geographic distance), in the same direction class, with a
       bearing within ``stack_bearing_tol_deg`` of the primary, and
       *not* already competitively claimed by a different segment, has
       its altitudes merged into the result. This is the chart
       convention "when two yellow arrows are printed side by side for
       the same leg, ATC may clear EITHER altitude" — canonical case is
       BAZRA→DEROR (1600 next to 800 leaving CTR HERTZLIA). Stacked
       alternates are *not* gated by ``radius_nm`` from the segment
       line, since the secondary arrow is by definition further out
       than the primary; instead they ride on the primary's anchor.

    3. **Shared-bend arrow rescue** — when two consecutive real-waypoint
       legs both end up empty after phases 1 + 2, search for an
       unclaimed chart arrow at the bend whose body is drawn along the
       *bisector* of the two leg bearings. Such an arrow labels the
       corridor through the bend even though it's parallel to neither
       leg individually (canonical case: HTZUK→KNTRY→LLHZ at the LLHZ
       Class-D approach, where a single 1200 arrow sits along the
       (104°+36°)/2 = 70° bisector between the two legs). The rescue
       only fires when both adjacent legs are real-waypoint and both
       are otherwise unknown — it cannot trample a per-leg match nor
       propagate altitudes through chart-intentional corridor changes
       (e.g. NSHRM→SIRNI→NTAIM, where SIRNI is correctly the boundary
       between the labeled 1200 corridor and the next, unlabeled one).
       See ``MATCH_BEND_RESCUE_*`` constants for the gate values and
       the rationale behind each.

    4. **Wide-corridor rescue** — handles charts where the route's
       waypoint chain sits on one edge of a wide airway and the
       chart designer printed the altitude-arrow column in the open
       space on the OTHER edge (1.0–1.8 nm cross-track). Canonical
       case: the HRTZ coastal corridor, where the user clicks
       SFAIM→APOLN→ARENA→HTZUK→RIDNG→CLORE→TYONA along the eastern
       triangle chain but the SB 800 ft arrows are printed along the
       LBG TMA boundary 1.0–1.8 nm to the west of that chain. The
       rescue only fires for legs still unknown after phases 1–3
       and considers only **on-segment, tightly bearing-aligned,
       unclaimed** arrows: parallel-right out to
       ``wide_corridor_radius_nm`` and parallel-left out to the tighter
       ``wide_corridor_left_radius_nm``. Each candidate arrow is assigned
       to the unknown segment it fits best (lowest cross-track among
       eligible legs), so a single chart label doesn't smear across
       multiple legs even when the wide radius would allow it
       geometrically. See ``MATCH_WIDE_CORRIDOR_*`` constants for the
       gate values and the rationale.

    Output ordering inside a tuple: primary's altitudes first (in their
    chart order — band high then low for stacked numerics in one arrow),
    then alternates ordered by distance from the primary. Duplicates are
    dropped so a re-printed altitude doesn't clutter the cell.
    """
    if not segments:
        return []

    flat_arrows: list[GeoAltitudeArrow] = []
    for arrows in arrows_by_sheet.values():
        flat_arrows.extend(arrows)
    if not flat_arrows:
        return [() for _ in segments]

    seg_bearings = [
        true_bearing_deg(
            s.from_point.lat, s.from_point.lon,
            s.to_point.lat, s.to_point.lon,
        )
        for s in segments
    ]
    seg_radii = [
        _segment_radius_nm(
            s,
            waypoint_radius_nm=radius_nm,
            intermediate_radius_nm=intermediate_radius_nm,
        )
        for s in segments
    ]

    # Phase 1: per-(arrow, segment) fit caching + competitive assignment.
    #
    # Each segment evaluates each arrow against its OWN radius gate
    # (strict for real-waypoint legs, loose for intermediate-involving
    # legs). The composite ``(class_rank, score)`` ranking is unchanged
    # — a closer / more-parallel arrow always wins, regardless of which
    # radius let it through. This keeps the loose radius from biasing
    # competition: an intermediate leg can only "win" an arrow that a
    # real-waypoint leg also reaches if the intermediate's geometry
    # actually fits better.
    fits: list[list[_ArrowSegFit | None]] = [
        [None] * len(segments) for _ in flat_arrows
    ]
    best_seg_for_arrow: list[int | None] = [None] * len(flat_arrows)

    for ai, arrow in enumerate(flat_arrows):
        best_si: int | None = None
        best_key: tuple[int, float] | None = None
        for si, seg in enumerate(segments):
            fit = _evaluate_arrow_for_segment(
                arrow, seg, seg_bearings[si],
                radius_nm=seg_radii[si],
                parallel_tol_deg=parallel_tol_deg,
                max_endpoint_overshoot_nm=max_endpoint_overshoot_nm,
                past_endpoint_parallel_tol_deg=past_endpoint_parallel_tol_deg,
            )
            fits[ai][si] = fit
            if fit is None:
                continue
            key = _fit_key(fit)
            if best_key is None or key < best_key:
                best_key = key
                best_si = si
        best_seg_for_arrow[ai] = best_si

    # Phase 1.5: real-waypoint segments reclaim stacked alternates from
    # intermediate segments.
    #
    # The loose intermediate radius lets an intermediate leg pick up an
    # arrow at e.g. 1.0 nm cross-track. That same arrow may be the
    # *stacked alternate* for an adjacent real-waypoint leg whose
    # primary sits comfortably inside the strict radius, with the
    # alternate just outside it (the canonical case is DAROM→HOTRM:
    # primary `(2000,)` at 0.40 nm, alternate `(1000,)` at 0.86 nm —
    # the alternate is *also* ~1.0 nm from the GALIM.2→DAROM
    # intermediate leg). Without this reclaim phase the intermediate
    # would steal the alternate, leaving the real-waypoint leg's cell
    # showing only the primary altitude.
    #
    # The reclaim is conservative: it only takes arrows that an
    # intermediate currently owns, and only when those arrows are
    # within ``stack_radius_nm`` of the real-waypoint segment's primary
    # AND match the primary's bearing / side (the same rules phase 2
    # applies for stacking). It cannot poach from another real-waypoint
    # segment, because both have chart-precise geometry and competition
    # already settled their dispute correctly.
    from cvfr_routemaster.route import great_circle_distance_nm as _gc_dist

    def _seg_is_real_waypoint(s: RouteSegment) -> bool:
        return (
            s.from_point.waypoint is not None
            and s.to_point.waypoint is not None
        )

    for si, seg in enumerate(segments):
        if not _seg_is_real_waypoint(seg):
            continue
        owned_now = [
            (ai, fits[ai][si])
            for ai in range(len(flat_arrows))
            if best_seg_for_arrow[ai] == si and fits[ai][si] is not None
        ]
        if not owned_now:
            continue
        primary_ai, _primary_fit = min(
            owned_now, key=lambda c: _fit_key(c[1])  # type: ignore[arg-type]
        )
        primary = flat_arrows[primary_ai]
        primary_side = (
            _arrow_side_of_segment(
                seg.from_point.lat, seg.from_point.lon,
                seg.to_point.lat, seg.to_point.lon,
                primary.lat, primary.lon,
            )
            if not primary.bidirectional
            else 0
        )
        for ai, arrow in enumerate(flat_arrows):
            if ai == primary_ai:
                continue
            owner = best_seg_for_arrow[ai]
            if owner is None or owner == si:
                continue
            if _seg_is_real_waypoint(segments[owner]):
                continue  # don't poach from another precise leg
            if arrow.bidirectional != primary.bidirectional:
                continue
            d_to_primary = _gc_dist(
                primary.lat, primary.lon, arrow.lat, arrow.lon,
            )
            if d_to_primary > stack_radius_nm:
                continue
            if not arrow.bidirectional:
                if (
                    _circular_diff_deg(arrow.bearing_deg, primary.bearing_deg)
                    > stack_bearing_tol_deg
                ):
                    continue
                alt_side = _arrow_side_of_segment(
                    seg.from_point.lat, seg.from_point.lon,
                    seg.to_point.lat, seg.to_point.lon,
                    arrow.lat, arrow.lon,
                )
                if primary_side * alt_side < 0:
                    continue
            # Synthesise a fit for (ai, si) so phase 2 sees the arrow as
            # owned by this segment. The arrow may be outside the
            # segment's strict per-segment radius, which is exactly why
            # we're reclaiming it via the primary-anchored stack sweep
            # rather than the segment-line gate.
            if fits[ai][si] is None:
                d_to_seg, overshoot_to_seg = (
                    _distance_and_overshoot_to_segment_nm(
                        seg.from_point.lat, seg.from_point.lon,
                        seg.to_point.lat, seg.to_point.lon,
                        arrow.lat, arrow.lon,
                    )
                )
                if arrow.bidirectional:
                    axis_diff = _axis_diff_deg(
                        arrow.bearing_deg, seg_bearings[si]
                    )
                    fits[ai][si] = _ArrowSegFit(
                        class_rank=_CLASS_BIDIRECTIONAL,
                        distance_nm=d_to_seg,
                        fwd_diff_deg=axis_diff,
                        overshoot_nm=overshoot_to_seg,
                    )
                else:
                    fwd_diff = _circular_diff_deg(
                        arrow.bearing_deg, seg_bearings[si]
                    )
                    fits[ai][si] = _ArrowSegFit(
                        class_rank=(
                            _CLASS_PARALLEL_RIGHT
                            if alt_side < 0
                            else _CLASS_PARALLEL_LEFT
                        ),
                        distance_nm=d_to_seg,
                        fwd_diff_deg=fwd_diff,
                        overshoot_nm=overshoot_to_seg,
                    )
            best_seg_for_arrow[ai] = si

    # Phase 2: per-segment primary selection + stacking.
    from cvfr_routemaster.route import great_circle_distance_nm

    results: list[tuple[int, ...]] = []
    for si, seg in enumerate(segments):
        owned = [
            (ai, fits[ai][si])
            for ai in range(len(flat_arrows))
            if best_seg_for_arrow[ai] == si and fits[ai][si] is not None
        ]
        if not owned:
            results.append(())
            continue

        # Primary = best fit among the segment's owned arrows.
        primary_ai, primary_fit = min(
            owned, key=lambda c: _fit_key(c[1])  # type: ignore[arg-type]
        )
        primary = flat_arrows[primary_ai]

        altitudes_in_order: list[int] = list(primary.altitudes_ft)
        seen: set[int] = set(altitudes_in_order)

        # Stacking: gather alternates near the primary that aren't already
        # claimed by another segment. Sweep all arrows (not just this
        # segment's owned ones) so a side-by-side alternate that sits
        # outside the segment's own cross-track radius can still be picked
        # up — the primary acts as the centre of the search circle.
        primary_side = (
            _arrow_side_of_segment(
                seg.from_point.lat, seg.from_point.lon,
                seg.to_point.lat, seg.to_point.lon,
                primary.lat, primary.lon,
            )
            if not primary.bidirectional
            else 0
        )
        stacked: list[tuple[float, GeoAltitudeArrow]] = []
        for ai, arrow in enumerate(flat_arrows):
            if ai == primary_ai:
                continue
            owner = best_seg_for_arrow[ai]
            if owner is not None and owner != si:
                # Belongs to a different leg's label; the chart placed it
                # specifically for that leg, so it's not OUR alternate.
                continue
            if arrow.bidirectional != primary.bidirectional:
                # A directional arrow next to a bidirectional one is a
                # different chart annotation, not a stacked alternate.
                continue
            d_to_primary = great_circle_distance_nm(
                primary.lat, primary.lon, arrow.lat, arrow.lon,
            )
            if d_to_primary > stack_radius_nm:
                continue
            if not primary.bidirectional:
                if (
                    _circular_diff_deg(arrow.bearing_deg, primary.bearing_deg)
                    > stack_bearing_tol_deg
                ):
                    continue
                # Same-side rule: chart convention puts the OUR-direction
                # arrows of a leg on a single side of the route line; a
                # cross-side parallel arrow is the opposite-direction leg's
                # label, not OUR stacked alternate.
                alt_side = _arrow_side_of_segment(
                    seg.from_point.lat, seg.from_point.lon,
                    seg.to_point.lat, seg.to_point.lon,
                    arrow.lat, arrow.lon,
                )
                if primary_side * alt_side < 0:
                    continue
            stacked.append((d_to_primary, arrow))

        for _, arrow in sorted(stacked, key=lambda x: x[0]):
            for alt in arrow.altitudes_ft:
                if alt not in seen:
                    altitudes_in_order.append(alt)
                    seen.add(alt)

        results.append(tuple(altitudes_in_order))

    # Phase 3: shared-bend arrow rescue.
    #
    # See the function docstring (and the ``MATCH_BEND_RESCUE_*`` constant
    # block) for the full chart-convention rationale. Implementation walks
    # consecutive segment pairs (si, si+1) and for each pair whose
    # eligibility gates pass — both real-waypoint, both currently empty,
    # bend angle >= ``bend_rescue_min_bend_deg`` — picks the best
    # unclaimed bisector-aligned arrow nearby and attributes its
    # altitudes to BOTH legs.
    #
    # "Unclaimed" means ``best_seg_for_arrow[ai] is None``, i.e. the
    # standard competitive pass found no segment whose gates the arrow
    # passes. That set is small (typically a handful of arrows for a
    # whole chart) so the inner scan is cheap even for long routes.
    # The rescue NEVER reads from ``best_seg_for_arrow`` non-None
    # entries — a per-leg match always wins over the bend signature.
    for si in range(len(segments) - 1):
        seg_a = segments[si]
        seg_b = segments[si + 1]

        if not (
            _seg_is_real_waypoint(seg_a) and _seg_is_real_waypoint(seg_b)
        ):
            continue
        if results[si] or results[si + 1]:
            continue
        # The two legs must share a waypoint (the matcher assumes a
        # contiguous route, so this is normally guaranteed; defensive
        # equality check keeps the rescue safe against synthetic
        # non-contiguous inputs).
        if (
            abs(seg_a.to_point.lat - seg_b.from_point.lat) > 1e-9
            or abs(seg_a.to_point.lon - seg_b.from_point.lon) > 1e-9
        ):
            continue

        bend_deg = _circular_diff_deg(
            seg_bearings[si], seg_bearings[si + 1]
        )
        if bend_deg < bend_rescue_min_bend_deg:
            continue

        bisector_deg = _bisector_bearing_deg(
            seg_bearings[si], seg_bearings[si + 1]
        )

        best_rescue_ai: int | None = None
        best_rescue_score: float | None = None
        for ai, arrow in enumerate(flat_arrows):
            if best_seg_for_arrow[ai] is not None:
                continue
            if arrow.bidirectional:
                # Bidirectional arrows label both flow directions; the
                # standard matcher handles them in their own class. The
                # bisector-bend signature relies on a directional bearing
                # we can compare against the route's bisector, which
                # bidirectional arrows don't carry — skip them so the
                # standard matcher's bidirectional handling stays in
                # charge.
                continue
            bis_diff = _circular_diff_deg(arrow.bearing_deg, bisector_deg)
            if bis_diff > bend_rescue_bisector_tol_deg:
                continue
            # Distance to the closer of the two legs' lines (endpoint-
            # clamped — same semantics as the standard cross-track gate).
            d_a, _ = _distance_and_overshoot_to_segment_nm(
                seg_a.from_point.lat, seg_a.from_point.lon,
                seg_a.to_point.lat, seg_a.to_point.lon,
                arrow.lat, arrow.lon,
            )
            d_b, _ = _distance_and_overshoot_to_segment_nm(
                seg_b.from_point.lat, seg_b.from_point.lon,
                seg_b.to_point.lat, seg_b.to_point.lon,
                arrow.lat, arrow.lon,
            )
            d_min = min(d_a, d_b)
            if d_min > bend_rescue_max_leg_dist_nm:
                continue
            # Score prefers tighter bisector alignment + closer
            # polyline-distance. Score weights mirror the per-leg
            # ``_fit_score`` style so the rescue's "best" candidate
            # picks the geometrically tightest arrow when more than one
            # qualifies (rare but possible at busy chart bends).
            score = d_min + MATCH_FWD_DIFF_SCORE_WEIGHT * bis_diff
            if best_rescue_score is None or score < best_rescue_score:
                best_rescue_score = score
                best_rescue_ai = ai

        if best_rescue_ai is None:
            continue

        rescue_arrow = flat_arrows[best_rescue_ai]
        rescued = tuple(rescue_arrow.altitudes_ft)
        results[si] = rescued
        results[si + 1] = rescued
        # Mark the arrow as owned by the first leg so a later iteration
        # (or a subsequent feature) doesn't treat it as still-unclaimed
        # and rescue it onto another bend.
        best_seg_for_arrow[best_rescue_ai] = si

    # Phase 4: wide-corridor rescue.
    #
    # See the function docstring (and the ``MATCH_WIDE_CORRIDOR_*``
    # constants) for the chart-convention rationale: this handles
    # corridor labels printed in the open space on the opposite edge
    # of a wide airway from the user's clicked waypoint chain, which
    # the strict 0.65 nm primary radius can't reach (HRTZ coastal SB
    # 800 ft labels at 1.0–1.8 nm cross-track is the canonical case).
    #
    # Gates the rescue applies, all conjunctive:
    #   * Segment is real-waypoint (both endpoints carry a waypoint
    #     record; no free-clicked intermediates participate, otherwise
    #     corridor labels would smear across user sub-leg chains).
    #   * Segment is currently unknown after phases 1–3 (this rescue
    #     can never overwrite a per-leg primary, stack, or bend
    #     rescue).
    #   * Candidate arrow is directional (single-headed) and unclaimed.
    #     Bidirectional arrows already get an axis-parallel gate in
    #     phase 1; their semantics differ enough that wide-corridor
    #     rescue would just duplicate phase 1's logic at a riskier
    #     radius.
    #   * Side-specific cross-track cap: RIGHT (parallel-right) arrows —
    #     the predominant chart labelling convention — out to the full
    #     ``wide_corridor_radius_nm`` (1.8 nm); LEFT arrows only out to the
    #     much tighter ``wide_corridor_left_radius_nm`` (~0.9 nm), since a
    #     left arrow admitted at 1.8 nm is very likely a neighbouring
    #     corridor's label. An on-the-line arrow (side 0) is left to the
    #     primary gate.
    #   * Foot lies on segment (no endpoint overshoot) — the arrow
    #     must geographically sit alongside THIS leg, not the next one.
    #   * Cross-track ≤ ``wide_corridor_radius_nm``.
    #   * Fwd-diff ≤ ``wide_corridor_fwd_diff_deg`` (tightened from
    #     the primary 30° budget since we're paying for a wider
    #     cross-track allowance).
    #
    # Competition: an arrow that qualifies for multiple unknown
    # segments goes to the one it fits best by cross-track distance.
    # That mirrors phase 1's competitive matching and keeps a single
    # chart label from being attributed to multiple legs.
    eligible: dict[int, list[tuple[int, float, float]]] = {}
    # eligible[ai] = list of (si, distance_nm, fwd_diff_deg) for unknown
    # segments this arrow qualifies for.
    for ai, arrow in enumerate(flat_arrows):
        if best_seg_for_arrow[ai] is not None:
            continue
        if arrow.bidirectional:
            continue
        for si, seg in enumerate(segments):
            if results[si]:
                continue
            if not _seg_is_real_waypoint(seg):
                continue
            d, overshoot = _distance_and_overshoot_to_segment_nm(
                seg.from_point.lat, seg.from_point.lon,
                seg.to_point.lat, seg.to_point.lon,
                arrow.lat, arrow.lon,
            )
            if overshoot > 0.0:
                continue
            side = _arrow_side_of_segment(
                seg.from_point.lat, seg.from_point.lon,
                seg.to_point.lat, seg.to_point.lon,
                arrow.lat, arrow.lon,
            )
            # Side-specific cross-track cap. The chart's predominant
            # convention prints the altitude column to the RIGHT of the
            # route, so the right side gets the full wide radius; the
            # LEFT side (e.g. LSA NIRYA→ZMGID's 1700 arrow ~0.77 nm south
            # of a westbound leg) is admitted only just past the primary
            # gate, where a parallel on-segment arrow is overwhelmingly
            # the leg's own offset label rather than a neighbour's.
            # ``side``: -1 = RIGHT of travel, +1 = LEFT, 0 = on the line.
            if side < 0:
                cap = wide_corridor_radius_nm
            elif side > 0:
                cap = wide_corridor_left_radius_nm
            else:
                continue  # on the line — primary gate already handled it
            if d > cap:
                continue
            fwd_diff = _circular_diff_deg(
                arrow.bearing_deg, seg_bearings[si]
            )
            if fwd_diff > wide_corridor_fwd_diff_deg:
                continue
            eligible.setdefault(ai, []).append((si, d, fwd_diff))

    # Pick the best segment for each eligible arrow (closest by
    # cross-track, fwd-diff as tiebreaker via the same scoring weight
    # the rest of the matcher uses).
    chosen: list[tuple[int, int, float, float]] = []
    # (ai, si, distance_nm, fwd_diff_deg) — sorted later so segments
    # with multiple competing arrows get assigned the geometrically
    # tightest one first.
    for ai, cands in eligible.items():
        best = min(
            cands,
            key=lambda c: c[1] + MATCH_FWD_DIFF_SCORE_WEIGHT * c[2],
        )
        chosen.append((ai, best[0], best[1], best[2]))

    # If two arrows want the same segment, give it to the tighter
    # one and let the runner-up arrow keep looking (it might fit a
    # different unknown segment further down its candidate list).
    chosen.sort(
        key=lambda t: t[2] + MATCH_FWD_DIFF_SCORE_WEIGHT * t[3]
    )
    for ai, si, _d, _fd in chosen:
        if results[si]:
            continue  # already taken by a tighter rescue match
        rescue_arrow = flat_arrows[ai]
        results[si] = tuple(rescue_arrow.altitudes_ft)
        best_seg_for_arrow[ai] = si

    return results


def match_altitudes_for_segment(
    segment: RouteSegment,
    arrows_by_sheet: dict[str, list[GeoAltitudeArrow]],
    *,
    radius_nm: float = MATCH_RADIUS_NM,
    intermediate_radius_nm: float = MATCH_RADIUS_NM_INTERMEDIATE,
    parallel_tol_deg: float = MATCH_PARALLEL_TOL_DEG,
    stack_radius_nm: float = MATCH_STACK_RADIUS_NM,
    stack_bearing_tol_deg: float = MATCH_STACK_BEARING_TOL_DEG,
    max_endpoint_overshoot_nm: float = MATCH_MAX_ENDPOINT_OVERSHOOT_NM,
    past_endpoint_parallel_tol_deg: float = (
        MATCH_PARALLEL_TOL_DEG_PAST_ENDPOINT
    ),
) -> tuple[int, ...]:
    """Single-segment convenience wrapper around :func:`match_altitudes_for_route`.

    Forwards to the route-level matcher with a single-segment list, so a
    caller that doesn't have the rest of the route handy still gets the
    same priority logic and stacked-alternate behaviour. The competitive-
    matching step is a no-op when there's only one segment, but stacking
    still works (and is the main reason a caller wants to keep using
    this entry point — e.g. tests).

    Returns an empty tuple when nothing matches (the route panel renders
    the cell as "unknown").
    """
    return match_altitudes_for_route(
        [segment], arrows_by_sheet,
        radius_nm=radius_nm,
        intermediate_radius_nm=intermediate_radius_nm,
        parallel_tol_deg=parallel_tol_deg,
        stack_radius_nm=stack_radius_nm,
        stack_bearing_tol_deg=stack_bearing_tol_deg,
        max_endpoint_overshoot_nm=max_endpoint_overshoot_nm,
        past_endpoint_parallel_tol_deg=past_endpoint_parallel_tol_deg,
    )[0]
