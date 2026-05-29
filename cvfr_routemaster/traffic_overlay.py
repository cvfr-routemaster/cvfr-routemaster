"""On-chart VATSIM traffic rendering layer (v2 feature; see
``ROADMAP-NEXT.md``).

This module owns the *visual* side of the live-traffic display: a
:class:`TrafficOverlay` manager that holds a list of plane items in
the main :class:`QGraphicsScene` and rebuilds them on every
:meth:`set_pilots` call. The data side (parsing the v3 datafeed,
HTTP fetching, wake-category lookup) lives in
:mod:`cvfr_routemaster.vatsim_feed`; the Qt-thread layer that
drives periodic polling will land later as
``cvfr_routemaster.vatsim_worker``.

What gets drawn
---------------

For each :class:`~cvfr_routemaster.vatsim_feed.Pilot` we build one
:class:`_TrafficPlaneItem` — a custom :class:`QGraphicsItem` that
paints, in a single call:

1. A top-down airplane silhouette (filled polygon with a thin dark
   outline), rotated to match the pilot's heading.
2. A bold callsign label to the right of the silhouette, with a
   black halo so it stays legible against the chart's coloured
   surfaces (and, eventually, satellite imagery in v3).

The whole item carries
:attr:`QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations`
so its on-screen pixel size stays constant as the user zooms the
chart — exactly the route-origin-marker pattern in
:func:`cvfr_routemaster.main_window._redraw_route_overlay`.

Visual encoding
---------------

Five wake categories, each with a distinct colour and a per-class
size multiplier on top of the user's icon-size setting:

==============  ============  =============  ====================
Wake            Colour        Size scale     Typical aircraft
==============  ============  =============  ====================
``"L"``         cyan          0.85x          C172, DA40, BE76
``"M"``         green         1.00x          B738, A320, E190
``"H"``         orange        1.20x          B748, A35K, B77W
``"J"``         magenta       1.40x          A380, An-225
``"unknown"``   gray          1.00x          no flight plan filed
==============  ============  =============  ====================

The user's :data:`traffic_icon_size_px` (from QSettings — see
:mod:`cvfr_routemaster.settings_store`) sets the base nose-to-tail
length in screen pixels. The wake scale multiplies it: a Cessna
shows at 85% and an A380 at 140% of the user's chosen base.

Why a custom :class:`QGraphicsItem` (and not a group + children)
----------------------------------------------------------------

Two children — silhouette + callsign — would be the obvious shape,
but they want different transforms: the silhouette rotates with
heading, the callsign stays upright for legibility. Putting both
into a :class:`QGraphicsItemGroup` and rotating only one child gets
tangled up with how
:attr:`ItemIgnoresTransformations` propagates through groups (the
flag interacts with child transforms in ways that vary across Qt
versions). A custom :class:`QGraphicsItem` with a single
``paint()`` that draws both elements directly is more robust and
keeps everything in one bounding box for spatial indexing.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QStyleOptionGraphicsItem,
    QWidget,
)

from cvfr_routemaster.vatsim_feed import WAKE_UNKNOWN, Pilot

# --- Z order -------------------------------------------------------------

#: Z-value for traffic items. Sits above the route polyline (z=100)
#: and origin marker (z=101) so traffic visibly draws on top of the
#: planned route — pilots are flying it, after all. Well below any
#: future modal-overlay z (calibration overlay et al. live on the
#: viewport, not in the scene). Cosmetic only — every traffic item
#: sets ``AcceptedMouseButtons = NoButton`` so click ordering
#: doesn't matter.
TRAFFIC_OVERLAY_Z: float = 200.0


# --- Wake-category visual encoding ---------------------------------------

#: Colour per wake category. Five colourblind-distinguishable hues
#: with matching saturation so no one category fades into the chart.
#: Alpha 235/255 keeps the silhouette readable against busy chart
#: regions (e.g. the route polyline) without flattening the icon
#: into the background. The unknown gray is deliberately
#: desaturated so a chart full of unfiled-plan VFR pilots reads as
#: "we don't know what these are" rather than "the same as a
#: medium airliner".
WAKE_COLOR: dict[str, QColor] = {
    "L": QColor(30, 200, 211, 235),
    "M": QColor(61, 220, 132, 235),
    "H": QColor(255, 140, 26, 235),
    "J": QColor(232, 58, 153, 235),
    WAKE_UNKNOWN: QColor(158, 158, 158, 235),
}

#: Per-wake-class size multiplier on top of the user's icon-size
#: setting. The user's value corresponds to a "typical airliner"
#: (Medium); lights draw 15% smaller and heavies 20% larger so the
#: relative size cue reinforces the colour cue. The Super class is
#: 40% larger to capture how absurdly outsized an A380 actually is.
WAKE_SCALE: dict[str, float] = {
    "L": 0.85,
    "M": 1.0,
    "H": 1.2,
    "J": 1.4,
    WAKE_UNKNOWN: 1.0,
}

#: Border colours. The silhouette gets a three-row black/white/black
#: ring outside its boundary so the wake-colour fill pops against any
#: underlying chart region (busy green-and-yellow chart areas, future
#: satellite imagery, the route polyline). Drawn as three concentric
#: cosmetic-pen strokes from widest to narrowest, then the wake-colour
#: fill on top — see :meth:`_TrafficPlaneItem.paint` for the layered
#: stroke trick that produces the three rows from three single
#: drawPath calls.
#:
#: Black uses alpha 230/255 (not full 255) so a plane crossing
#: another plane shows a hint of the underlying icon rather than a
#: solid black slab; white is full alpha because partially
#: transparent white turns muddy against most chart colours.
_BORDER_BLACK = QColor(0, 0, 0, 230)
_BORDER_WHITE = QColor(255, 255, 255, 255)

#: Callsign label palette. White glyph fill with a 2-pixel black halo
#: is the universally readable "map label" pattern (Google, Apple, OS
#: charts) — keeps the callsign legible against every chart colour
#: without competing visually with the silhouette's wake-category
#: hue. The wake colour is already encoded by the silhouette right
#: next to the label, so the text doesn't need to also encode it.
_CALLSIGN_FILL = QColor(255, 255, 255, 255)
_CALLSIGN_HALO = QColor(0, 0, 0, 230)

#: Yellow selection halo for "this plane is being tracked".
#:
#: Drawn as a circular ring around the silhouette when a plane is
#: the user's tracking target — see :meth:`_TrafficPlaneItem.paint`
#: and the click-to-track flow in ``map_graphics_view`` /
#: ``main_window``. The hue is the high-visibility "amber" yellow
#: aviation already uses for warning indicators (PFD/MFD cautions,
#: runway hold-short lines), so it reads as "this is the one I
#: picked" without needing a colour key. Full alpha so the ring
#: stays crisp against busy chart backgrounds; the
#: heading-and-wake-aware concentric border around the silhouette
#: stays visible *inside* the ring because the halo is drawn as
#: an OUTLINE (no fill), not a disc.
_TRACKING_HALO_COLOR = QColor(255, 212, 0, 255)

#: Ring thickness in screen pixels for the tracking halo. Three
#: pixels reads as "deliberate selection affordance" rather than
#: "stray pixel" at every icon size we currently ship; thinner
#: looked like aliasing on a busy chart, thicker started
#: competing with the silhouette's own concentric border for
#: visual weight.
_TRACKING_HALO_WIDTH_PX = 3.0

#: Extra padding between the silhouette's outer edge and the inner
#: edge of the tracking halo, in screen pixels. The silhouette
#: already carries its own 3-pixel black/white/black border (see
#: ``_make_border_pen`` and ``paint``), so the halo needs to stand
#: off the OUTSIDE of that border, not the silhouette polygon
#: itself, or the halo would visually fuse with the existing
#: concentric ring and stop reading as a separate "selected"
#: signal. Three pixels of standoff leaves a clean breathing-room
#: gap.
_TRACKING_HALO_PADDING_PX = 3.0


# --- Silhouette path -----------------------------------------------------


def _build_silhouette_path() -> QPainterPath:
    """Build the normalised airplane silhouette as a single closed
    polygonal path.

    Coordinates are normalised so the nose-to-tail length is 1.0
    and the centre of the silhouette is at (0, 0) — callers scale
    by the desired pixel size at paint time. The shape walks
    clockwise from the nose:

    * Pointed nose at ``(0, -0.5)``.
    * Forward fuselage taper to ``(±0.04, -0.30)``.
    * Wing root at ``(±0.04, -0.05)``, swept forward of the
      half-length so the centre of pressure feels right visually.
    * Wingtips at ``(±0.50, 0.06)`` with a slight aft sweep —
      wingspan equals nose-to-tail length, the standard
      reading-from-above rule of thumb.
    * Aft fuselage to ``(±0.04, 0.32)``.
    * Tail wings at ``(±0.20, 0.42)``, span 40% of wing span.
    * Tailcone closes at ``(0, 0.50)``.

    The path is returned closed so callers can fill it as one
    polygon — the QPainter outline-and-fill machinery handles a
    single ``drawPath`` call correctly.

    Built once at module import (the result is cached in
    :data:`_SILHOUETTE_PATH`); the path is immutable from
    QPainter's perspective so reusing it across all plane items is
    safe.
    """
    path = QPainterPath()
    path.moveTo(0.00, -0.50)  # nose
    # Right side, walking aft.
    path.lineTo(0.04, -0.30)
    path.lineTo(0.04, -0.05)
    path.lineTo(0.50, 0.06)  # right wingtip leading edge
    path.lineTo(0.46, 0.10)  # right wingtip trailing edge
    path.lineTo(0.04, 0.10)  # back to fuselage
    path.lineTo(0.04, 0.32)
    path.lineTo(0.20, 0.42)  # right horizontal stab leading edge
    path.lineTo(0.18, 0.46)  # trailing edge
    path.lineTo(0.04, 0.46)
    path.lineTo(0.03, 0.50)  # right side of tailcone
    # Left side, mirroring back to the nose.
    path.lineTo(-0.03, 0.50)
    path.lineTo(-0.04, 0.46)
    path.lineTo(-0.18, 0.46)
    path.lineTo(-0.20, 0.42)
    path.lineTo(-0.04, 0.32)
    path.lineTo(-0.04, 0.10)
    path.lineTo(-0.46, 0.10)
    path.lineTo(-0.50, 0.06)
    path.lineTo(-0.04, -0.05)
    path.lineTo(-0.04, -0.30)
    path.closeSubpath()
    return path


_SILHOUETTE_PATH = _build_silhouette_path()


# --- Ground detection ---------------------------------------------------

#: Per-wake-category groundspeed thresholds (in knots) below which
#: we assume a fixed-wing aircraft is on the ground rather than
#: airborne. VATSIM's v3 datafeed has no explicit "on ground" flag
#: — the kinematic indicator we get is groundspeed, and a plane
#: rolling on the runway / taxiing / parked is the only situation
#: where GS sits well below stall speed.
#:
#: Thresholds are scaled per wake class because stall speeds
#: scale with wake category. A Cessna 172 (L) at 45 kt is
#: realistically on a taxiway; an A380 (J) at 45 kt definitely
#: is. Conversely a 737 (M) at 80 kt could be on takeoff
#: roll OR taxiing — we lean toward "on ground" for anything
#: that low because the airborne case is brief (a few seconds
#: of takeoff roll) and the parked/taxi case dominates the
#: low-GS space at that wake class.
#:
#: Values are deliberately conservative (well below typical
#: rotation speeds) so we never label a climbing-out airliner
#: as "on the ground". The trade-off: a taxiing heavy at 95 kt
#: shows "FL000" until it crosses 120 kt, which is acceptable
#: — high-speed taxis are short-lived and the hand-off to "in
#: the air" happens cleanly at rotation.
#:
#: ``unknown`` defaults to the lightweight threshold because a
#: pilot without a filed plan is almost always a small GA bug
#: smasher squawking 1200; treating them like a light single
#: matches reality more often than treating them like an
#: airliner.
_GROUND_SPEED_THRESHOLDS_KT: dict[str, int] = {
    "L": 50,
    "M": 100,
    "H": 120,
    "J": 140,
    WAKE_UNKNOWN: 50,
}


def _is_on_ground(pilot: Pilot) -> bool:
    """Return True if the pilot's groundspeed is below the
    per-wake-category threshold for "definitely not airborne".

    Pure groundspeed-based heuristic — see
    :data:`_GROUND_SPEED_THRESHOLDS_KT` for the rationale and
    per-class numbers. The function is the single source of
    truth for ground/air classification on the chart; both the
    altitude-label override (``GRND`` instead of FL/ft) and
    any future colour or icon variation should consult it
    rather than inlining the threshold check.
    """
    threshold = _GROUND_SPEED_THRESHOLDS_KT.get(
        pilot.wake, _GROUND_SPEED_THRESHOLDS_KT[WAKE_UNKNOWN]
    )
    return pilot.groundspeed_kts < threshold


# --- On-chart altitude label --------------------------------------------


def _format_altitude_label(pilot: Pilot) -> str:
    """Build the second-line altitude readout shown beneath the
    callsign on the chart.

    Four cases, in priority order:

    * **On ground** (per :func:`_is_on_ground`) → ``"GRND"``.
      Takes precedence over every other branch because a parked
      A380 with ``flight_rules="I"`` would otherwise show
      ``"FL000"``, which reads as "at the surface in
      controlled airspace" — technically true but misleading
      next to a clearly-stationary icon.

    * **No flight plan filed** (``aircraft_type is None``) →
      ``"<altitude>ft"``. VATSIM's v3 datafeed sets
      ``flight_plan`` to ``null`` for pilots flying without a
      filed plan, which the parser flattens to
      ``aircraft_type=None``. Most of those pilots are GA
      flying VFR at low altitudes (a 2,891 ft circuit reads as
      "FL028" in the FL-default world, which is genuinely
      jarring next to the rest of the Israeli CVFR traffic).
      Treat absent-plan as VFR for label purposes.

    * **VFR** (``flight_rules == "V"``) → ``"<altitude>ft"``.
      Matches the Israeli CVFR convention where altitudes are
      reported as raw foot values (e.g. ``2500ft`` for the
      2,500-foot reporting altitude).

    * **Filed plan, non-VFR** (``flight_rules`` is ``I`` / ``Y`` /
      ``Z`` / empty-but-plan-was-filed) → ``"FL<NNN>"``, always
      three digits, computed by integer-dividing the altitude
      by 100. Three digits is the universal flight-level
      convention (FL050, FL280, FL400) and matches what VATSIM
      clients type into their flight plans. We use ``//``
      rather than rounding — ``31350`` is FL313 in
      flight-following speak, not FL314.

    Y and Z (mixed) plans always file a flight-level portion
    and ATC reads them at flight levels, so FL is the right
    default for anything that isn't an explicit ``"V"`` (when
    a plan was filed at all). A filed plan with a malformed /
    empty ``flight_rules`` string still goes to FL: the plan's
    existence signals the pilot is in the IFR system in some
    form, and showing them at "feet" alongside other airliners
    would obscure that.

    Negative altitudes (which VATSIM occasionally reports for
    parked aircraft below the reference plane) are clamped to
    ``0`` so we never render ``-100ft`` and confuse the reader.
    """
    if _is_on_ground(pilot):
        return "GRND"
    altitude_ft = max(0, pilot.altitude_ft)
    if pilot.aircraft_type is None:
        return f"{altitude_ft}ft"
    rules = pilot.flight_rules.strip().upper() if pilot.flight_rules else ""
    if rules == "V":
        return f"{altitude_ft}ft"
    return f"FL{altitude_ft // 100:03d}"


# --- On-chart groundspeed label -----------------------------------------


def _format_speed_label(pilot: Pilot) -> str:
    """Build the groundspeed component of the bottom line.

    Format: ``"<kt>kt"`` with no separator and no prefix —
    space at small icon sizes is at a premium. The kt suffix
    is enough to disambiguate from the altitude/FL component
    when both share a slash-separated bottom line (so a
    bottom line of ``"FL280/420kt"`` is unambiguous about
    which number is which).

    Negative or sentinel-zero groundspeeds are clamped to 0
    so we never render ``"-1kt"`` from a glitchy upstream
    sample. Always emitted — even for parked aircraft (which
    will read ``"0kt"``) so the bottom line stays a fixed
    two-component readout regardless of motion state.
    ``GRND`` on the same line already conveys "on the
    ground"; the ``0kt`` here adds the "and not currently
    moving" detail.
    """
    speed = max(0, pilot.groundspeed_kts)
    return f"{speed}kt"


# --- Composed two-line label --------------------------------------------


def _format_top_line(pilot: Pilot) -> str:
    """Build line 1 of the on-chart label:
    ``<callsign>/<icao_type_code>`` (e.g. ``ELY323/B738``).

    When the pilot has not filed an aircraft type — typically
    a VFR squawk-1200 with no flight plan, where
    ``pilot.aircraft_type`` is ``None`` — we omit the slash
    and the suffix entirely and render just the callsign.
    Showing ``"<callsign>/?"`` would be more uniform but adds
    visual noise to the tightest visual cohort (small GA on
    short hops); the unknown-wake colour (gray icon) already
    conveys "no plan filed" at a glance, so the bare
    callsign is the cleanest fallback.

    Whitespace in the type designator is stripped to defend
    against upstream feeds that occasionally pad short codes
    with trailing spaces.
    """
    code = (pilot.aircraft_type or "").strip()
    if code:
        return f"{pilot.callsign}/{code}"
    return pilot.callsign


def _format_bottom_line(pilot: Pilot) -> str:
    """Build line 2 of the on-chart label:
    ``<altitude-or-FL-or-GRND>/<speed>`` (e.g.
    ``"FL280/420kt"``, ``"2500ft/85kt"``, ``"GRND/12kt"``).

    Composes the altitude formatter (which already handles
    the FL/ft branch and the on-ground override) with the
    groundspeed formatter, joined by ``"/"``. Each component
    carries its own unit suffix or prefix (``ft``, ``FL``,
    ``GRND``, ``kt``), so the slash is purely a visual
    divider — no risk of misreading which number is which.
    """
    return f"{_format_altitude_label(pilot)}/{_format_speed_label(pilot)}"


# --- Tooltip text --------------------------------------------------------


def _format_tooltip(pilot: Pilot) -> str:
    """Build the multi-line tooltip text for one pilot.

    Three lines (with the third optional):

    1. Callsign on its own line — easiest to read at a glance.
    2. Aircraft type + wake category, or "(no flight plan)" + wake
       when ``aircraft_type`` is ``None``.
    3. Kinematics: altitude, groundspeed, heading. Always shown.
    4. Optional: ``DEP → ARR`` when either airport is known. Skipped
       entirely for VFR pilots with no plan filed (cluttering the
       tooltip with ``"? → ?"`` would be worse than omitting it).
    """
    lines = [pilot.callsign]
    if pilot.aircraft_type:
        lines.append(f"{pilot.aircraft_type} · {pilot.wake}")
    else:
        lines.append(f"(no flight plan) · {pilot.wake}")
    lines.append(
        f"ALT {pilot.altitude_ft} ft · "
        f"GS {pilot.groundspeed_kts} kt · "
        f"HDG {pilot.heading_deg:03d}°"
    )
    if pilot.departure or pilot.arrival:
        dep = pilot.departure or "?"
        arr = pilot.arrival or "?"
        lines.append(f"{dep} → {arr}")
    return "\n".join(lines)


# --- Single plane item ---------------------------------------------------


class _TrafficPlaneItem(QGraphicsItem):
    """One pilot rendered on the chart.

    Geometry is in *screen pixels* (the
    :attr:`ItemIgnoresTransformations` flag drops the view's
    zoom/pan from the rendering pipeline), so the silhouette and
    callsign stay constant size regardless of how far the user has
    zoomed in. The item is positioned at the pilot's projected
    scene coordinates via :meth:`setPos`; the ``paint`` method
    draws relative to that anchor.

    Click-through: ``AcceptedMouseButtons = NoButton`` so the
    plane item never absorbs a click intended for the chart
    underneath (route add, sheet selection, calibration). The
    tooltip still triggers on hover — Qt routes hover and tooltip
    events independently of mouse-button acceptance.
    """

    def __init__(self, pilot: Pilot, icon_size_px: int) -> None:
        super().__init__()
        self._pilot = pilot
        # Selection state for the "track this plane" feature. The
        # overlay flips this via :meth:`set_selected` whenever the
        # user clicks a plane to track it (or clicks empty chart
        # to release). When True, ``paint`` draws a yellow ring
        # around the silhouette so the user can see WHICH pilot
        # is being followed even in a busy airspace.
        self._selected: bool = False
        # Per-pilot size in screen pixels. The user's icon-size
        # value corresponds to "Medium"; lights and heavies scale
        # off it. Stored as float so the half-size offsets used in
        # paint() don't need extra coercion.
        wake_scale = WAKE_SCALE.get(pilot.wake, 1.0)
        self._size = float(icon_size_px) * wake_scale
        self._color = WAKE_COLOR.get(pilot.wake, WAKE_COLOR[WAKE_UNKNOWN])
        # Heading rotation lives on the silhouette only — the
        # callsign stays upright for legibility, so we apply
        # rotation inside paint() rather than via setRotation().
        self._heading = float(pilot.heading_deg)

        # Pre-build the callsign font + measure its text once;
        # paint() runs on every repaint and we don't want to redo
        # font-metrics work on each.
        self._font = QFont()
        # Bold at ~45% of the icon size lands the label readable
        # without overpowering the silhouette. Min 8 pt so the
        # text never becomes microscopic at the smallest icon
        # sizes (8 px nose-to-tail).
        font_pt = max(8, int(icon_size_px * 0.45))
        self._font.setPointSize(font_pt)
        self._font.setBold(True)
        fm = QFontMetrics(self._font)

        # Two-line label, composed:
        #   line 1: <callsign>/<icao_type>      (or just <callsign>
        #                                        if no plan filed)
        #   line 2: <alt|FL|GRND>/<speed>kt
        # Each line packs two pieces of info separated by ``/``
        # so the user can read identity (line 1) and motion
        # state (line 2) at a glance. Two lines is meaningfully
        # more compact than the earlier three-line layout
        # (≈45 px vs ≈70 px tall at default 36 px icon / 16 pt
        # bold), which directly reduces label-overlap density
        # in busy ramp areas like LLBG.
        self._top_text = _format_top_line(pilot)
        self._bottom_text = _format_bottom_line(pilot)
        top_w = fm.horizontalAdvance(self._top_text)
        bottom_w = fm.horizontalAdvance(self._bottom_text)
        # Bounding-box width tracks whichever line is wider so
        # the halo on the longer line doesn't poke past the
        # spatial-indexing rect. Either line can win depending
        # on the pilot — long callsign/type ("CARGOLUX/A388")
        # vs. long altitude/speed ("FL280/420kt") — so we take
        # the max.
        self._text_width = max(top_w, bottom_w)

        # Two-line vertical layout, centred around y=0
        # (silhouette midline). The per-line gap straddles y=0
        # — line 1's glyphs end ``gap/2`` above, line 2's
        # begin ``gap/2`` below. Solving for the baselines:
        #   line 1 baseline: y_baseline + descent = -gap/2
        #                 →  y_baseline = -gap/2 - descent
        #   line 2 baseline: y_baseline - ascent  = +gap/2
        #                 →  y_baseline = +gap/2 + ascent
        # ``ascent`` includes the leading whitespace above the
        # cap height, which keeps the visual centre of the
        # two-line block close to y=0 without needing per-glyph
        # metrics.
        ascent = fm.ascent()
        descent = fm.descent()
        line_gap_px = 2
        anchor_x = self._size / 2 + 4
        self._top_anchor = QPointF(
            anchor_x, -line_gap_px / 2 - descent
        )
        self._bottom_anchor = QPointF(
            anchor_x, line_gap_px / 2 + ascent
        )

        # Bounding rect must encompass silhouette + 3-pixel border
        # ring + selection halo + both label lines + label halo,
        # so Qt's spatial indexing knows when to repaint us. The
        # border extends 3 px outside the silhouette on every side
        # (see ``paint`` for the layered concentric-stroke
        # geometry); the label halo extends ~2 px outside each
        # glyph. The selection halo, when active, adds another
        # ``_TRACKING_HALO_PADDING_PX`` of standoff + a stroke half-
        # width of ``_TRACKING_HALO_WIDTH_PX / 2`` outside the
        # silhouette. We include that extent in the bbox
        # unconditionally (rather than dynamically resizing the
        # bbox on selection toggle) so flipping ``_selected`` never
        # leaves a stale strip outside the previously-tight bbox.
        # The cost is a couple of extra pixels on every plane's
        # spatial-index footprint, which is negligible.
        #
        # Vertical extent is ``max(silhouette_half, half_text_block)``
        # on each side — at default 36 px icon / 16 pt bold the
        # silhouette and text block are roughly the same size, so
        # the rect grows to whichever is taller. Cheap to be
        # generous; the bbox only affects spatial indexing, not
        # what's drawn.
        border_slack = (
            4.0
            + _TRACKING_HALO_PADDING_PX
            + _TRACKING_HALO_WIDTH_PX / 2.0
        )
        text_half_height = ascent + descent + line_gap_px / 2
        half_y = max(self._size / 2, text_half_height) + border_slack
        right_extent = anchor_x + self._text_width + border_slack
        half_x_left = self._size / 2 + border_slack
        self._brect = QRectF(
            -half_x_left,
            -half_y,
            half_x_left + right_extent,
            half_y * 2,
        )

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(TRAFFIC_OVERLAY_Z)
        self.setToolTip(_format_tooltip(pilot))

    # --- Selection state (for click-to-track) ---------------------

    @property
    def callsign(self) -> str:
        """The pilot's callsign — exposed so the click hit-test in
        ``MapGraphicsView`` can translate a scene-level item hit
        into the string key MainWindow's tracking state uses.

        Kept read-only because the underlying ``Pilot`` is itself
        immutable (a frozen dataclass over each VATSIM snapshot);
        the item is rebuilt from scratch on every snapshot, so
        there's no live mutation path here.
        """
        return self._pilot.callsign

    def is_selected(self) -> bool:
        """Whether this plane is the user's current tracking
        target. Cheap accessor for tests and for callers that
        rebuild the overlay and need to re-apply selection state
        across the rebuild (see :meth:`TrafficOverlay.set_pilots`).
        """
        return self._selected

    def set_selected(self, value: bool) -> None:
        """Flip the selection state and request a repaint.

        Called by :meth:`TrafficOverlay.set_tracked_callsign` on
        every overlay rebuild (15 s) so the yellow halo follows
        the user's click selection across pilot updates. ``update``
        schedules a repaint inside the item's bounding rect — no
        extra invalidation work needed because the bbox already
        accounts for the halo's outer extent (see ``__init__``).
        """
        value = bool(value)
        if self._selected == value:
            return
        self._selected = value
        self.update()

    # --- QGraphicsItem overrides ----------------------------------

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt camelCase)
        return self._brect

    def paint(  # noqa: N802 (Qt camelCase)
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,  # noqa: ARG002
        widget: QWidget | None = None,  # noqa: ARG002
    ) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 0. Yellow tracking halo (only when this plane is the
        # user's tracking target). Drawn FIRST so it sits behind
        # every other visual layer — the silhouette + its
        # black/white/black concentric border draw on top, so the
        # halo reads as a clean ring around the icon rather than
        # competing with the wake colour for foreground attention.
        #
        # Geometry: a stroked circle centred on the plane's local
        # origin with radius ``size/2 + padding + width/2``. The
        # ``+ width/2`` lifts the inside edge of the stroke clear
        # of the silhouette's outer concentric border (which lives
        # 3 px outside the silhouette polygon — see Layer 1
        # comment below), and the ``+ padding`` adds the
        # breathing-room gap so the halo is recognisably separate
        # from that border. No fill: a yellow disc would hide the
        # plane underneath.
        if self._selected:
            halo_radius = (
                self._size / 2.0
                + _TRACKING_HALO_PADDING_PX
                + _TRACKING_HALO_WIDTH_PX / 2.0
            )
            halo_pen = QPen(
                _TRACKING_HALO_COLOR,
                _TRACKING_HALO_WIDTH_PX,
                Qt.PenStyle.SolidLine,
            )
            halo_pen.setCosmetic(True)
            painter.setPen(halo_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(
                QPointF(0.0, 0.0), halo_radius, halo_radius
            )

        # 1. Silhouette: rotate by heading, scale the normalised path
        # to icon size, then draw four layers from widest to
        # narrowest. Cosmetic pens make widths device-pixels regardless
        # of the painter scale, which is exactly what we need: the
        # border thickness is "three rows of pixels" on screen, not
        # three units of normalised silhouette space.
        #
        # The concentric-stroke trick produces a three-row
        # black/white/black ring entirely *outside* the silhouette
        # boundary (so the wake-colour fill goes right up to the
        # boundary, no recess) from just three drawPath calls:
        #
        #   Layer 1: black 6-px stroke   → 3 px on each side of edge
        #   Layer 2: white 4-px stroke   → 2 px each side
        #                                  (overpaints layer 1's
        #                                   inner 2 px on each side,
        #                                   leaving 1 px outermost
        #                                   black visible)
        #   Layer 3: black 2-px stroke   → 1 px each side
        #                                  (overpaints layer 2's
        #                                   inner 1 px each side,
        #                                   leaving 1 px middle white
        #                                   visible)
        #   Layer 4: wake-colour fill    → covers the entire interior,
        #                                  overpainting the inside-half
        #                                  of every stroke
        #
        # End result, walking from outside in: 1 px outermost black,
        # 1 px white, 1 px innermost black (still outside the
        # boundary), then the wake-colour fill. The wake-colour fill
        # extends right to the boundary, so the silhouette's shape
        # stays intact and the border simply enlarges it by 3 px on
        # every side.
        painter.save()
        painter.rotate(self._heading)
        painter.scale(self._size, self._size)
        # Layer 1: outermost black ring.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(self._make_border_pen(_BORDER_BLACK, 6.0))
        painter.drawPath(_SILHOUETTE_PATH)
        # Layer 2: white ring (overpaints the middle of layer 1).
        painter.setPen(self._make_border_pen(_BORDER_WHITE, 4.0))
        painter.drawPath(_SILHOUETTE_PATH)
        # Layer 3: innermost black ring (overpaints the middle of
        # layer 2). No fill yet — that's a separate pass so the
        # fill cleanly wipes every stroke's inside-half.
        painter.setPen(self._make_border_pen(_BORDER_BLACK, 2.0))
        painter.drawPath(_SILHOUETTE_PATH)
        # Layer 4: wake-colour fill, no pen. Overpaints every
        # stroke's inside-half so the fill goes right to the
        # boundary.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(self._color))
        painter.drawPath(_SILHOUETTE_PATH)
        painter.restore()

        # 2. Two-line composed label.
        #   line 1: <callsign>/<type>    (identity)
        #   line 2: <alt|FL|GRND>/<speed>kt   (motion state)
        # Both lines are NOT rotated (always horizontal) and both
        # use the same white-fill + 2-pixel-black-halo treatment —
        # the universally readable "map label" pattern from every
        # commercial map renderer. Halo is drawn first as a
        # stroked glyph path; the white glyph drawText on top
        # wipes the inner half of the stroke, leaving only the
        # ~2 px halo visible outside each glyph. Order matters:
        # drawing the halo as the glyph's *outline only* would
        # leave the inside of every glyph transparent and the
        # chart underneath would bleed through, defeating the
        # legibility halo.
        painter.setFont(self._font)
        self._draw_label_line(painter, self._top_anchor, self._top_text)
        self._draw_label_line(painter, self._bottom_anchor, self._bottom_text)

    @staticmethod
    def _draw_label_line(
        painter: QPainter, baseline: QPointF, text: str
    ) -> None:
        """Render one line of the on-chart label with a halo + fill.

        Two passes:

        1. Build a ``QPainterPath`` of the glyph outlines via
           :meth:`QPainterPath.addText`, stroke it with a thick
           black pen — this draws a halo extending ``~2 px`` outside
           each glyph (the stroke is centred on the glyph edge, so
           half of the stroke goes outside, half goes inside).
        2. Draw the actual text in white via :meth:`QPainter.drawText`
           at the same baseline. The white glyph fills the entire
           glyph interior, wiping out the inside-half of the halo
           stroke and leaving only the outside-half visible.

        End result: white glyphs with a ~2 px black halo around
        every character. Works against any underlying chart
        colour.
        """
        text_path = QPainterPath()
        text_path.addText(baseline, painter.font(), text)
        halo_pen = QPen(
            _CALLSIGN_HALO,
            4.0,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(halo_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(text_path)
        painter.setPen(_CALLSIGN_FILL)
        painter.drawText(baseline, text)

    @staticmethod
    def _make_border_pen(color: QColor, width_px: float) -> QPen:
        """Build a cosmetic pen for one of the silhouette's
        concentric border strokes.

        Cosmetic = width is interpreted in device pixels regardless
        of the painter's current scale, which is exactly what we
        want: the border thickness is "screen pixels around the
        silhouette", not "fraction of silhouette height". Round
        caps/joins keep the corners of the silhouette polygon
        smooth at every scale, so the three rings track each
        other neatly across the wing/tail vertices.
        """
        pen = QPen(color, width_px)
        pen.setCosmetic(True)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        return pen


# --- Overlay manager -----------------------------------------------------


class TrafficOverlay:
    """Manages the lifecycle of a set of :class:`_TrafficPlaneItem`
    instances in a :class:`QGraphicsScene`.

    The class is deliberately stateless beyond the item list — every
    :meth:`set_pilots` call tears down the previous items and
    rebuilds from scratch. That's the same pattern
    :func:`cvfr_routemaster.main_window._redraw_route_overlay`
    uses, and it keeps the call sites simple: any change (new
    pilot list, calibration completes, icon size changes) is
    expressed as one ``set_pilots`` call.

    Thread safety: all methods must run on the GUI thread (Qt
    enforces this for any :class:`QGraphicsScene` mutation). The
    Qt worker layer lands later and will marshal the
    ``pilots_updated`` signal to this thread before calling
    ``set_pilots``.

    The projection callback is injected so the overlay doesn't
    depend on :class:`MainWindow` directly — easier to unit-test,
    and easier to swap projection logic (chart vs. satellite mode
    in v3) without touching the overlay.
    """

    def __init__(
        self,
        scene: QGraphicsScene,
        project_lonlat: Callable[[float, float], QPointF | None],
    ) -> None:
        self._scene = scene
        self._project = project_lonlat
        self._items: list[_TrafficPlaneItem] = []
        # Callsign of the plane the user has clicked to "track".
        # ``None`` means no tracking; otherwise the matching item
        # gets a yellow halo every time ``set_pilots`` rebuilds
        # the overlay. Stored at the manager level (not on each
        # item) because the items are torn down every snapshot —
        # we need a stable identifier that survives the rebuild
        # so the visual selection can be re-applied to the freshly
        # constructed item with the same callsign.
        self._tracked_callsign: str | None = None

    def set_pilots(
        self,
        pilots: list[Pilot],
        *,
        icon_size_px: int,
    ) -> None:
        """Tear down the current plane items and rebuild from
        ``pilots`` at the given icon size.

        Pilots whose ``(lon, lat)`` doesn't project to a scene
        point — typically because no sheet is calibrated yet, or
        the pilot is far enough off-chart that neither sheet's
        affine is defined for them — are silently skipped. Better
        to draw the in-bounds traffic than to fail the whole
        repaint when one pilot strays outside the chart's
        coverage.

        Idempotent: calling with the same arguments twice produces
        the same on-screen state. Callers can therefore be naive
        about whether a refresh is needed; the cost is one teardown
        + rebuild per call, which is cheap (typically < 50 plane
        items even at peak Israeli airspace).

        Tracking state (the yellow halo set via
        :meth:`set_tracked_callsign`) survives the rebuild: after
        repopulating ``self._items`` we re-apply ``set_selected``
        to the new item whose callsign matches
        ``self._tracked_callsign``. The user's selection therefore
        stays visually attached to the same pilot across every
        15 s VATSIM update without the caller having to
        re-issue ``set_tracked_callsign`` after each one.
        """
        self.clear()
        for pilot in pilots:
            scene_pt = self._project(pilot.lon, pilot.lat)
            if scene_pt is None:
                continue
            item = _TrafficPlaneItem(pilot, icon_size_px)
            item.setPos(scene_pt)
            self._scene.addItem(item)
            self._items.append(item)
        if self._tracked_callsign is not None:
            self._apply_tracking_visual()

    def set_tracked_callsign(self, callsign: str | None) -> None:
        """Mark exactly one plane (or none) as the user's tracking
        target.

        ``callsign`` resolves to a case-sensitive exact-match
        against ``pilot.callsign`` — VATSIM callsigns are
        case-sensitive in the datafeed (everyone uses upper but
        we don't case-normalise here because the rest of the
        pipeline doesn't either). Passing ``None`` clears the
        selection (no plane shows a halo).

        Idempotent: re-selecting the same callsign or re-clearing
        an already-empty selection is a no-op. Callers can wire
        this up to a click handler without needing to track the
        previous state on their end.

        Returns immediately after stashing the callsign and
        re-walking the items; the halo repaint is scheduled by
        each item's ``set_selected`` -> ``update`` and arrives on
        the next Qt event-loop tick.
        """
        self._tracked_callsign = callsign
        self._apply_tracking_visual()

    def tracked_callsign(self) -> str | None:
        """Currently tracked callsign, or ``None`` if no plane is
        selected. Pure accessor — surfaced for tests and for the
        ``map_graphics_view`` plain-click branch that decides
        whether a click on empty chart should fire a "stop
        tracking" status message.
        """
        return self._tracked_callsign

    def find_callsign(self, callsign: str) -> _TrafficPlaneItem | None:
        """Return the item whose callsign matches, or ``None``.

        Lookup is linear in the item count; we don't keep a dict
        keyed by callsign because the typical overlay has well
        under 50 items, the iteration cost is negligible next to
        the per-frame paint cost, and the simpler list-of-items
        structure stays uniform across the rebuild flow.
        """
        for item in self._items:
            if item.callsign == callsign:
                return item
        return None

    def _apply_tracking_visual(self) -> None:
        """Walk the items, set exactly the matching one selected.

        Centralises the "make sure visual state matches
        ``self._tracked_callsign``" invariant so both
        :meth:`set_tracked_callsign` (caller flipped the state)
        and :meth:`set_pilots` (rebuild just discarded the
        previous item list) can share one implementation. Items
        whose callsign doesn't match are explicitly deselected so
        a callsign change correctly *deselects* the previous
        target — not just the typical case where the new target
        and old happen to be different items.
        """
        target = self._tracked_callsign
        for item in self._items:
            item.set_selected(target is not None and item.callsign == target)

    def clear(self) -> None:
        """Remove every plane item from the scene.

        Safe to call multiple times (no-op when already empty)
        and from any state — the lifecycle hooks in
        :class:`MainWindow` invoke this on the "show traffic"
        toggle off and on chart unload.
        """
        for item in self._items:
            self._scene.removeItem(item)
        self._items.clear()

    def __len__(self) -> int:
        """Number of plane items currently rendered. Convenience
        for tests and the future "12 aircraft visible" status-bar
        readout when the live poller lands.
        """
        return len(self._items)
