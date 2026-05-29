"""Per-sheet waypoint-marker overlay for the satellite view.

The chart's printed VRP / waypoint triangles are baked into the
chart pixmap (``chart.png``); when satellite imagery covers that
pixmap they're invisible. The user's requirement is that *every*
chart waypoint stays visible — and clickable for shift-add /
shift-remove routing — when satellite mode is on.

This module renders one triangle + Hebrew-name label per
waypoint as a single custom :class:`QGraphicsItem`. Each item is
parented to the chart pixmap (so chart pan/scale/translate
propagate) but flagged with
:attr:`QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations`,
which drops both the chart's user-applied transform and the
view's zoom/pan from the rendering pipeline. The position is
mapped through transforms (so the marker stays anchored to its
chart-pixel coord), but the rendering geometry is in *screen
pixels* — a 16-pixel triangle stays 16 pixels wide whether the
user is at 50 % zoom showing all of Israel or 400 % zoom on a
single airfield. Mirrors the pattern :class:`_TrafficPlaneItem`
uses for VATSIM traffic icons.

Triangle styling matches the printed chart's convention, with
visibility aids the printed chart doesn't need (a high-contrast
concentric border) added on top — pure black strokes vanish into
dark satellite imagery (cypress forests, nighttime city blocks,
sea):

* **Mandatory reporting points (חובה)** — chart-yellow-filled
  triangle, apex up, with the yellow-black-yellow ringed border.
* **On-demand reporting points (דרישה)** — outlined triangle
  (no fill), apex up, yellow-black-yellow ringed border.
* **Airports / airstrips (ARP, plus any ``LL[A-Z]{2}`` code in
  the OCR even if classified as mandatory because it's also a
  transit reporting point, e.g. Massada / LLMZ)** — outlined
  inverted triangle (apex *down*), blue-black-blue ringed
  border, label *above* the triangle (mirror of the
  reporting-point markers). Drawn at a slightly lower z than
  the reporting-point markers so when both stack on the same
  waypoint (Massada has both because it's an airfield used as
  a transit reporting point) the reporting-point marker sits
  *on top* of the airport marker — which is how the user
  described the desired visual.
* **Unknown / empty reporting type** — defensively rendered as
  on-demand outline (matches the chart's "I don't know what this
  is" fallback better than the more emphatic mandatory fill).

Labels show the Hebrew name (``WaypointRecord.name_he``) when
present, falling back to ``WaypointRecord.code`` when not — the
back-pages OCR sometimes leaves the name field blank for
auxiliary points but always extracts the code. Text is white on
a translucent black backing rectangle: gives consistent
readability over any imagery (sand, green, urban grey, sea blue)
without the per-glyph fill-vs-stroke fighting we'd get from a
pen-stroked text item. The label sits *below* the triangle's
base for upward-apex markers (reporting points) and *above* the
triangle's base for downward-apex markers (airports) so the
label is always on the "open" side of the triangle's silhouette
and doesn't crowd the apex point.

Why a single custom QGraphicsItem (not three siblings)
------------------------------------------------------

An earlier design used three siblings per waypoint
(``QGraphicsPolygonItem`` + ``QGraphicsRectItem`` +
``QGraphicsTextItem``). That worked when the marker scaled with
the chart, but breaks once we want fixed screen-pixel size: each
sibling would honour ``ItemIgnoresTransformations`` separately,
anchoring its own ``setPos`` independently. When the parent
scales 2×, the *gap* between the triangle's anchor and the
label's anchor doubles in screen space — the layout falls apart.

Putting everything in one item with one ``paint()`` method
keeps the relative geometry (triangle apex above origin, label
below) constant in screen pixels regardless of any parent
transform. Same reason :class:`_TrafficPlaneItem` is structured
this way.

Click handling
--------------

The marker items do *not* intercept mouse events — they
``setAcceptedMouseButtons(Qt.NoButton)``. Click routing for shift-
add-waypoint goes through :class:`MapGraphicsView.mousePressEvent`
which already maps the click position to (sheet, lat, lon) and
calls :meth:`MainWindow._nearest_waypoint_to`. That path is
projection-independent and works identically with or without
satellite imagery on screen — the only thing that was lost was
the *visual* affordance, which this overlay restores.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPixmapItem,
    QStyleOptionGraphicsItem,
    QWidget,
)

from cvfr_routemaster.waypoint_styles import HE_MANDATORY, HE_ON_DEMAND

if TYPE_CHECKING:
    from cvfr_routemaster.geo_calibration import SheetGeoCalibration
    from cvfr_routemaster.satellite_overlay import ChartSeamPartition
    from cvfr_routemaster.waypoint_types import WaypointRecord


# --- Styling constants ----------------------------------------------------

#: Z-value for marker items. Sits *above* the satellite tile z
#: (15.0, see
#: :data:`cvfr_routemaster.satellite_overlay.SATELLITE_TILE_Z`) and
#: both chart pixmaps (z=0 north, z=10 south). Route / traffic /
#: arrow overlays live on z=100+ on the scene proper, so this
#: stack ordering puts waypoint markers between satellite imagery
#: and those higher overlays — exactly where the user expects to
#: find them when planning a route.
#:
#: Marker items are *top-level* scene items (added directly to
#: ``chart_item.scene()`` rather than parented under
#: ``chart_item``), for the same reason satellite tiles are: a
#: child item cannot paint above its parent's top-level siblings,
#: and we need markers visible regardless of which chart pixmap
#: is currently top-z. See
#: :class:`cvfr_routemaster.satellite_overlay.SatelliteOverlay`'s
#: class docstring for the full painter-order rationale.
WAYPOINT_MARKER_Z: float = 20.0

#: Z-value for airport / airstrip markers. Sits just *below*
#: :data:`WAYPOINT_MARKER_Z` so that when an airport doubles as a
#: reporting point (Massada / LLMZ is the canonical case — it's an
#: airfield used as a transit-route reporting point) the
#: reporting-point triangle paints on top of the airport triangle.
#: The 0.5 offset is enough to settle Qt's painter order
#: deterministically without colliding with the satellite tile z
#: (15.0) or the chart pixmap z-values (0 / 10) below.
AIRPORT_MARKER_Z: float = WAYPOINT_MARKER_Z - 0.5

#: Triangle side length in *screen* pixels. The marker uses
#: ``ItemIgnoresTransformations`` so this is a literal device-px
#: value, not a chart-pixel value that scales with zoom.
#:
#: 24 px is the default — bumped 50 % from the original 16 px
#: after user feedback that the original marker was too small to
#: read at typical satellite-view zoom levels (the satellite
#: imagery is much busier than the printed chart so the marker
#: needs more visual weight to be findable). User-overridable
#: via Display Settings; see
#: :func:`cvfr_routemaster.settings_store.load_waypoint_marker_size_px`.
DEFAULT_TRIANGLE_SIDE_PX: float = 24.0

#: Triangle fill colour for *mandatory* (חובה) reporting points —
#: chart-yellow, matching the printed convention's mandatory
#: triangles. Previously solid black; the change to yellow makes
#: the marker pop against satellite imagery (where black would
#: blend with shadow / urban dark areas and reduce legibility).
#: The black border layer below preserves the high-contrast
#: outline so the marker remains findable against the bright
#: chart-yellow of the inside.
MANDATORY_FILL: QColor = QColor(255, 215, 0, 255)

#: Outer ring of the triangle border. Black: this is the layer
#: that *finds* the marker against any background — black against
#: sand / sea / forest / urban is uniformly high-contrast, which
#: is the property we need most from the outermost ring.
TRIANGLE_BORDER_OUTER: QColor = QColor(0, 0, 0, 255)

#: Middle ring of the border. Yellow between two blacks gives
#: the marker its iconic halo; it also matches the chart's
#: printed yellow-on-black triangle border convention so chart
#: mode and satellite mode read as the same symbol.
TRIANGLE_BORDER_MID: QColor = QColor(255, 215, 0, 255)

#: Inner ring of the border. Black again to give the triangle a
#: sharp edge against its own yellow fill (mandatory) or against
#: whatever's behind the unfilled triangle (on-demand). Pairs
#: with the outer black to bracket the yellow halo.
TRIANGLE_BORDER_INNER: QColor = QColor(0, 0, 0, 255)

#: Pen widths for the three concentric rings, in *screen pixels*
#: (because the item ignores transformations). Each subsequent
#: stroke covers the inner half of the previous stroke, so the
#: visible widths are:
#:
#: * outer yellow ring: ``(OUTER - MID) / 2`` = 1 px on each side
#: * black ring:        ``(MID - INNER) / 2`` = 1 px on each side
#: * inner yellow ring: ``INNER / 2``         = 0.5 px on each side
#:
#: Total visual border thickness ≈ 2.5 px on each side of the
#: triangle's logical edge. With the equilateral triangle's 16 px
#: side that's a meaningful, but not overwhelming, halo.
TRIANGLE_BORDER_OUTER_PX: float = 5.0
TRIANGLE_BORDER_MID_PX: float = 3.0
TRIANGLE_BORDER_INNER_PX: float = 1.0

#: Outer / middle / inner ring colours for the airport-marker
#: triangle. Same concentric-ring structure as the reporting-point
#: marker — outer black for "find the marker against any
#: background", middle colour as the halo, inner black to sharpen
#: the edge against the (no-fill) interior — but with the halo
#: colour switched from chart-yellow to a saturated tailwind-blue
#: ``#3b82f6`` so the user reads "airport, not a reporting point"
#: at a glance. The halo is the load-bearing visual differentiator
#: because the *shape* (equilateral triangle) is otherwise
#: identical, just inverted.
AIRPORT_BORDER_OUTER: QColor = QColor(0, 0, 0, 255)
AIRPORT_BORDER_MID: QColor = QColor(59, 130, 246, 255)  # #3b82f6
AIRPORT_BORDER_INNER: QColor = QColor(0, 0, 0, 255)

#: Label font size, in *screen* pixels (point sizes don't fit
#: cleanly here — the item ignores DPI scaling and we want the
#: label to look the same regardless of the user's display).
LABEL_FONT_PX: int = 11

#: Vertical gap between the triangle's bottom edge (the base, now
#: that the apex points up) and the label's top edge.
LABEL_OFFSET_PX: float = 3.0

#: Padding around the text inside the label backing rect. Gives
#: white text a small breathing room from the rect edge so it
#: doesn't look cramped.
LABEL_PADDING_PX: float = 2.0

#: Translucent black for the label's backing rectangle. Alpha
#: ``180`` (≈ 70 %) is enough to make white text readable over
#: any z=14 imagery without completely obscuring the underlying
#: scene — pilots glancing for landmarks shouldn't lose all
#: visual context behind every label.
LABEL_BACKGROUND: QColor = QColor(0, 0, 0, 180)

#: Label text colour. White on translucent black is the most
#: legible combo across the imagery palette (sand, green, urban
#: grey, sea blue).
LABEL_TEXT_COLOR: QColor = QColor(255, 255, 255, 255)


# --- Helpers --------------------------------------------------------------


def _triangle_polygon(
    cx: float,
    cy: float,
    side_px: float,
    *,
    apex_down: bool = False,
) -> QPolygonF:
    """Equilateral triangle centred at ``(cx, cy)`` with the given
    side length.

    By default the apex points **up** — matches the printed
    Israeli VFR chart convention so the user reads the same shape
    in chart mode and satellite mode. When ``apex_down=True`` the
    triangle is flipped along the horizontal axis through its
    centroid, so the single point faces down — this is the
    airport/airstrip variant. The centroid stays at ``(cx, cy)``
    in either orientation.

    In Qt scene coordinates y increases downward, so "apex up"
    means the apex has a *smaller* y than the base. "Apex down"
    inverts this — apex y is larger.
    """
    # Equilateral triangle math: side ``s`` → height ``h = s * √3 / 2``.
    # Place the apex 2h/3 above (or below) the centroid and the base
    # midpoint h/3 below (or above) it so the geometric centre is
    # exactly (cx, cy).
    half_side = side_px * 0.5
    height = side_px * 0.8660254037844387  # √3 / 2
    if apex_down:
        base_y = cy - height / 3.0           # base above origin
        apex_y = cy + height * 2.0 / 3.0     # apex below origin
    else:
        base_y = cy + height / 3.0           # base below origin (apex up)
        apex_y = cy - height * 2.0 / 3.0     # apex above origin (apex up)
    return QPolygonF(
        [
            QPointF(cx - half_side, base_y),  # left base vertex
            QPointF(cx + half_side, base_y),  # right base vertex
            QPointF(cx, apex_y),              # apex
        ]
    )


def _classify_reporting_type(reporting_type: str) -> str:
    """Map a raw OCR ``reporting_type`` value to a small enum.

    Returns one of:

    * ``"mandatory"`` — chart-yellow-filled triangle.
    * ``"on_demand"`` — outline-only triangle.
    * ``"arp"`` — aerodrome reference point; this classifier
      identifies them, but the overlay no longer renders a
      *reporting-point* triangle for them (an airport marker is
      drawn instead — see :func:`_is_airport`).
    * ``"unknown"`` — outline-only (defensive fallback).

    The OCR can produce extra whitespace or stray punctuation
    around the literal Hebrew strings, so we normalise with
    ``.strip()`` before comparison. ARP is matched
    case-insensitively because the back-pages writer is
    inconsistent ("ARP" vs "Arp" vs "arp" all observed).
    """
    s = (reporting_type or "").strip()
    if s == HE_MANDATORY:
        return "mandatory"
    if s == HE_ON_DEMAND:
        return "on_demand"
    if s.upper() == "ARP":
        return "arp"
    return "unknown"


#: Compiled ICAO-airport-code pattern for Israel: 4 letters, leading
#: ``LL`` (the ICAO assignment for Israel) followed by two more
#: alphabetic characters. Used by :func:`_is_airport` as the
#: secondary classifier that catches airfields the OCR lists with a
#: non-ARP ``reporting_type`` — the canonical case is Massada / LLMZ,
#: which is also a transit-route reporting point and so appears
#: with ``reporting_type == "חובה"`` rather than ``"ARP"``.
_ICAO_ISRAEL_AIRPORT_CODE = re.compile(r"^LL[A-Z]{2}$")


def _is_airport(w: "WaypointRecord") -> bool:
    """Decide whether a waypoint also deserves an *airport* marker
    (the blue inverted triangle) on top of (or instead of) any
    reporting-point marker.

    Two sources of truth, OR-ed together:

    1. ``reporting_type == "ARP"`` — the OCR's primary aerodrome
       reference-point classification. Catches all 25 ARP-classified
       records in the current chart's back-pages (LLBG, LLHA, LLHZ,
       LLER, LLOV, ..., plus the two non-ICAO airstrips KKDEM and
       GVULT).
    2. Code matches ``^LL[A-Z]{2}$`` — the Israeli ICAO airport
       pattern. Catches Massada (LLMZ) which is classified as
       ``"חובה"`` (mandatory reporting point) in the OCR because
       the chart lists it primarily as a transit-route waypoint
       even though it's also an airfield. The user expects both
       markers in that case (airport blue triangle underneath,
       mandatory yellow triangle on top).

    The second rule is robust against the current chart's data —
    every ``LL[A-Z]{2}`` code in the back-pages either is already
    classified ARP (rule 1 wins) or is LLMZ (the only OR-promotion
    case). If a future chart adds an ``LL[A-Z]{2}`` code that
    *isn't* an airfield, it would get a false-positive airport
    marker; that's an acceptable trade because such codes are
    extraordinarily rare in Israeli aviation data and the alternative
    (a hard-coded airport list) is much more brittle when new charts
    arrive.
    """
    rt = (w.reporting_type or "").strip().upper()
    if rt == "ARP":
        return True
    code = (w.code or "").strip().upper()
    return bool(_ICAO_ISRAEL_AIRPORT_CODE.match(code))


def _label_text_for(w: "WaypointRecord") -> str:
    """Pick the user-facing label string.

    Hebrew name preferred — that's what the printed chart shows
    and what the user reads while planning. Falls back to the
    ICAO-ish code when the OCR didn't capture a name (rare but
    happens on a few auxiliary points whose printed-chart label
    is just a bare letter and no Hebrew word).
    """
    name = (w.name_he or "").strip()
    if name:
        return name
    return (w.code or "").strip()


# --- Single waypoint marker item -----------------------------------------


class _WaypointMarkerItem(QGraphicsItem):
    """One waypoint rendered at fixed screen-pixel size.

    Position via :meth:`setPos` to chart-pixel coords (the parent
    is the chart pixmap item). The
    :attr:`ItemIgnoresTransformations` flag means the painter's
    transform is identity at draw time — local geometry is in
    device pixels around the position point.

    Render order inside ``paint``:

    1. Optional black triangle fill (mandatory reporting points
       only) — drawn first so the border layers paint on top.
    2. Yellow outer border stroke.
    3. Black middle border stroke (covers the centre of the
       yellow stroke, leaving a 1-px outer-yellow ring).
    4. Yellow inner border stroke (covers the centre of the
       black stroke, leaving a 1-px black ring and a 0.5-px
       innermost-yellow ring).
    5. Translucent black label backing rect.
    6. White label text (rendered through ``drawText`` with
       single-line + alignment flags so Hebrew bidi shaping
       runs correctly via Qt's text layout).

    Step (1) is positioned via the same polygon used by steps
    (2)–(4); the strokes naturally surround the fill. Steps (5)
    and (6) live in their own sub-rect below the triangle.
    """

    def __init__(
        self,
        *,
        kind: str,
        label_text: str,
        font: QFont,
        side_px: float,
        parent: QGraphicsPixmapItem | None,
    ) -> None:
        super().__init__(parent)
        self._kind = kind
        self._label_text = label_text
        self._font = font
        self._side_px = float(side_px)

        # Airport markers point down (apex below the base); every
        # other kind points up. The corresponding label sits on the
        # opposite side of the centroid from the apex — i.e. above
        # the marker for apex-down and below for apex-up — so the
        # label is always on the "open" (flat-base) side of the
        # triangle's silhouette.
        self._apex_down = kind == "airport"

        # Triangle polygon in local (screen-pixel) coords centred
        # at the origin. The factory handles both apex orientations
        # so tests + legacy paths reuse the same shape source.
        self._triangle = _triangle_polygon(
            0.0, 0.0, self._side_px, apex_down=self._apex_down
        )

        # Label geometry — measured once; ``paint()`` reuses.
        fm = QFontMetricsF(self._font)
        text_w = fm.horizontalAdvance(self._label_text) if self._label_text else 0.0
        text_h = fm.height()
        self._text_w = text_w
        self._text_h = text_h
        self._text_ascent = fm.ascent()

        # Geometry depends on apex orientation:
        #   apex_up   → base at y = +h/3, apex at y = -2h/3, label below.
        #   apex_down → base at y = -h/3, apex at y = +2h/3, label above.
        # ``triangle_base_y`` is the y-coordinate of the flat edge that
        # the label sits next to (offset by LABEL_OFFSET_PX, on the
        # side away from the apex).
        height = self._side_px * 0.8660254037844387
        if self._apex_down:
            triangle_base_y = -height / 3.0
            triangle_apex_y = height * 2.0 / 3.0
        else:
            triangle_base_y = height / 3.0
            triangle_apex_y = -height * 2.0 / 3.0

        # Label rect: centred horizontally, sitting on the side
        # opposite the apex. For apex-up that's below the base
        # (``bg_top = base + LABEL_OFFSET_PX``); for apex-down that's
        # above the base (``bg_top = base - LABEL_OFFSET_PX - bg_h``,
        # i.e. shift up by the full label height plus the gap).
        bg_left = -text_w * 0.5 - LABEL_PADDING_PX
        bg_w = text_w + 2 * LABEL_PADDING_PX
        bg_h = text_h + 2 * LABEL_PADDING_PX
        if self._apex_down:
            bg_top = triangle_base_y - LABEL_OFFSET_PX - bg_h
        else:
            bg_top = triangle_base_y + LABEL_OFFSET_PX
        self._label_bg_rect = QRectF(bg_left, bg_top, bg_w, bg_h)
        # Text rect: same column, inset by padding. drawText uses
        # this rect with ``AlignCenter`` flags so the visual
        # placement is exact regardless of fontmetrics drift.
        self._label_text_rect = QRectF(
            bg_left + LABEL_PADDING_PX,
            bg_top + LABEL_PADDING_PX,
            text_w,
            text_h,
        )

        # Bounding rect: union of triangle bbox (with outer-pen
        # slack so the halo isn't clipped) and the label backing
        # rect. Spatial indexing uses this; conservative bounds
        # avoid rendering glitches at the cost of a few extra
        # "is-visible?" intersection tests. For apex-up the
        # triangle's top is the apex (smaller y) and bottom is the
        # base; for apex-down those swap, so we take min/max of
        # the two y candidates rather than hard-coding which is
        # which.
        outer_pen_slack = TRIANGLE_BORDER_OUTER_PX * 0.5 + 1.0
        triangle_left = -self._side_px * 0.5 - outer_pen_slack
        triangle_right = self._side_px * 0.5 + outer_pen_slack
        triangle_top = min(triangle_apex_y, triangle_base_y) - outer_pen_slack
        triangle_bottom = max(triangle_apex_y, triangle_base_y) + outer_pen_slack
        x0 = min(triangle_left, bg_left)
        x1 = max(triangle_right, bg_left + bg_w)
        y0 = min(triangle_top, bg_top)
        y1 = max(triangle_bottom, bg_top + bg_h)
        self._brect = QRectF(x0, y0, x1 - x0, y1 - y0)

        self.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
        )
        # Click-through: shift-click routing maps the click against
        # the chart, not the marker. An opaque marker would block
        # "shift-click on a waypoint adds it to the route".
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(WAYPOINT_MARKER_Z)
        self.setVisible(False)

    # --- Introspection (mostly for tests) -------------------------------

    @property
    def kind(self) -> str:
        """``"mandatory"`` | ``"on_demand"`` | ``"unknown"`` —
        determines triangle fill mode."""
        return self._kind

    @property
    def label_text(self) -> str:
        """User-facing label string (Hebrew name preferred)."""
        return self._label_text

    @property
    def label_bg_rect(self) -> QRectF:
        """Local-coord rect of the label backing — exposed so
        tests can verify layout without re-implementing the
        font-metrics math."""
        return QRectF(self._label_bg_rect)

    # --- QGraphicsItem overrides ----------------------------------------

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt camelCase)
        return self._brect

    def paint(  # noqa: N802 (Qt camelCase)
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,  # noqa: ARG002
        widget: QWidget | None = None,  # noqa: ARG002
    ) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 1. Mandatory triangles get a chart-yellow fill drawn
        #    underneath the border layers. On-demand, airport, and
        #    unknown triangles skip this — the inside of their
        #    triangle shows whatever satellite tile is below
        #    (airports stay outline-only per the user's "like a
        #    non-mandatory reporting point triangle" spec).
        if self._kind == "mandatory":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(MANDATORY_FILL))
            painter.drawPolygon(self._triangle)

        # 2-4. Concentric ring border. Three rings, each subsequent
        #      stroke half-overdraws the previous one centred on the
        #      polygon edge, leaving visible rings of
        #      (outer - mid)/2, (mid - inner)/2, inner/2 pixels
        #      respectively on each side.
        #
        #      Reporting-point markers use yellow-black-yellow
        #      (chart-yellow halo matches the printed chart
        #      convention). Airport markers use blue-black-blue
        #      (blue halo distinguishes airports from reporting
        #      points at a glance even with identical shape).
        if self._kind == "airport":
            ring_palette = (
                (TRIANGLE_BORDER_OUTER_PX, AIRPORT_BORDER_OUTER),
                (TRIANGLE_BORDER_MID_PX, AIRPORT_BORDER_MID),
                (TRIANGLE_BORDER_INNER_PX, AIRPORT_BORDER_INNER),
            )
        else:
            ring_palette = (
                (TRIANGLE_BORDER_OUTER_PX, TRIANGLE_BORDER_OUTER),
                (TRIANGLE_BORDER_MID_PX, TRIANGLE_BORDER_MID),
                (TRIANGLE_BORDER_INNER_PX, TRIANGLE_BORDER_INNER),
            )
        for width, color in ring_palette:
            pen = QPen(color)
            pen.setWidthF(width)
            # Round joins at the apex avoid the spike that flat
            # joins would produce on an acute corner. Round caps
            # match for visual consistency on any open-edge
            # rendering.
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(self._triangle)

        # 5. Label backing — only paint if there's actual text;
        #    a zero-width rect would paint a hairline that looks
        #    like junk under the marker.
        if not self._label_text:
            return
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(LABEL_BACKGROUND))
        painter.drawRect(self._label_bg_rect)

        # 6. White label text. ``drawText(rect, flags, text)``
        #    routes through Qt's text layout (with bidi shaping),
        #    which renders Hebrew correctly without any per-glyph
        #    work in user code. The font we set was bold; weight
        #    is preserved across drawText calls.
        painter.setFont(self._font)
        painter.setPen(LABEL_TEXT_COLOR)
        painter.drawText(
            self._label_text_rect,
            int(
                Qt.AlignmentFlag.AlignCenter
                | Qt.TextFlag.TextSingleLine
            ),
            self._label_text,
        )


# --- Overlay container ----------------------------------------------------


class WaypointMarkerOverlay:
    """Per-sheet collection of waypoint marker items.

    Construction enumerates the waypoint list, classifies each
    entry by reporting type, projects through the calibration,
    and creates one :class:`_WaypointMarkerItem` for every entry
    whose projected coords land *inside* the chart pixmap rect.
    Out-of-sheet waypoints are skipped — they belong to the other
    sheet (or to neither, in the case of test fixtures whose
    lat/lon doesn't intersect any chart bbox). ARPs are also
    skipped — the chart shows airports as their own symbol, not
    as triangles.

    Visibility starts off; toggle on/off in lockstep with the
    satellite mode toolbar action.
    """

    def __init__(
        self,
        *,
        chart_item: QGraphicsPixmapItem,
        calibration: "SheetGeoCalibration",
        pixmap_size: tuple[int, int],
        waypoints: Iterable["WaypointRecord"],
        triangle_side_px: float = DEFAULT_TRIANGLE_SIDE_PX,
        z_value: float = WAYPOINT_MARKER_Z,
        font_family: str = "Arial",
        chart_seam_partition: "ChartSeamPartition | None" = None,
    ) -> None:
        """Build the overlay.

        Parameters
        ----------
        chart_item
            Chart pixmap item this overlay is associated with.
            Marker items are *not* parented to ``chart_item``
            (they're top-level scene items at z = ``z_value``,
            above both chart pixmaps); the overlay uses
            ``chart_item`` only to (a) read its current
            ``sceneTransform()`` for the initial marker
            placement, (b) discover ``chart_item.scene()`` to
            add markers into, and (c) register a geometry-change
            listener if ``chart_item`` is a
            :class:`cvfr_routemaster.main_window._ChartSheetItem`
            so subsequent chart pan / scale / rotate updates
            re-flow into the markers.
        calibration
            Per-sheet calibration; used once at construction to
            project every waypoint's lat/lon to chart-pixel
            coords.
        pixmap_size
            ``(width, height)`` of the chart pixmap. Waypoints
            falling outside this rect are skipped.
        waypoints
            Iterable of :class:`WaypointRecord`. Iterated once at
            construction; mutations to the original list have no
            effect afterwards.
        triangle_side_px
            Side length of each triangle in *screen* pixels (the
            item ignores transformations, so chart-pixel units
            don't apply).
        z_value
            Absolute scene z-value for marker items. See
            :data:`WAYPOINT_MARKER_Z`.
        font_family
            Family used for labels. Defaults to ``"Arial"``;
            Hebrew rendering relies on whichever font the system
            substitutes for the Hebrew range — Arial covers
            Hebrew on both Windows and Linux out of the box.
        chart_seam_partition
            Optional
            :class:`cvfr_routemaster.satellite_overlay.ChartSeamPartition`
            describing where the chart-pixmap seam is and which
            side this overlay's sheet sits on. When set, a
            waypoint that both sheets could otherwise project is
            assigned to whichever sheet owns the chart-pixmap
            territory it falls in — the same rule the satellite
            overlay applies to tiles. Without this, every
            waypoint in the chart-overlap strip renders twice
            (once per sheet) because each sheet's pixmap covers
            it, which looks like a UI bug. ``None`` disables
            the partition and every projectable waypoint gets a
            marker — the right call for unit tests with a single
            chart and for legacy callers, less so for the
            running app.
        """
        self._chart_item = chart_item
        self._z_value = z_value
        self._chart_seam_partition = chart_seam_partition
        self._items: list[_WaypointMarkerItem] = []
        # Per-marker chart-pixel position (the (cx, cy) used to
        # be the marker's `setPos` arg when markers were
        # children of the chart pixmap; Qt's parent transform
        # mapped that to scene coords for free). With markers
        # now top-level we cache (cx, cy) so
        # :meth:`_apply_chart_transform` can re-project through
        # ``chart_item.sceneTransform()`` on every chart move
        # without recomputing the lon/lat → UV → chart-pixel
        # chain.
        self._chart_positions: list[QPointF] = []
        self._visible = False

        w_px, h_px = int(pixmap_size[0]), int(pixmap_size[1])
        if w_px <= 0 or h_px <= 0:
            return

        # Bold font for the labels — at small font sizes (~11 px)
        # bold weight makes the difference between "readable" and
        # "soup of pixels" against busy imagery. The white-on-black
        # combo helps too but bold is what wins legibility.
        label_font = QFont(font_family, LABEL_FONT_PX)
        label_font.setBold(True)
        label_font.setPixelSize(LABEL_FONT_PX)

        scene = chart_item.scene()

        for w in waypoints:
            kind = _classify_reporting_type(w.reporting_type)
            is_airport = _is_airport(w)
            # Pre-LCC behaviour was: skip ARP rows entirely because
            # airports had their own chart symbology baked into the
            # printed pixmap. Under satellite mode that pixmap is
            # covered, so airports become invisible without the new
            # blue-triangle marker. We now produce up to *two*
            # markers per record:
            #
            #   * an *airport* marker (blue inverted triangle, label
            #     above) for any record where ``_is_airport`` is true;
            #   * a *reporting-point* marker (existing yellow apex-up
            #     triangle, label below) when the OCR classifies the
            #     record as mandatory / on_demand / unknown.
            #
            # ARP-classified records produce only the airport marker
            # (their ``kind`` is "arp" → no reporting-point marker).
            # Massada / LLMZ produces both (it's ICAO LL[A-Z]{2} →
            # airport marker; reporting_type=חובה → mandatory marker
            # stacked on top via the lower AIRPORT_MARKER_Z).
            if not is_airport and kind == "arp":
                # Should not normally happen — every ARP record is
                # also caught by ``_is_airport``. Defensive: skip
                # rather than emit zero markers silently.
                continue

            try:
                u, v = calibration.lonlat_to_uv(w.lon, w.lat)
            except (ValueError, ZeroDivisionError):
                continue
            cx = u * w_px
            cy = v * h_px
            if not (0.0 <= cx <= w_px and 0.0 <= cy <= h_px):
                # Belongs to the other sheet (or to neither, e.g.
                # back-pages waypoints with lat/lon over open
                # Mediterranean).
                continue

            if (
                self._chart_seam_partition is not None
                and self._chart_seam_partition.item_owned_by_peer(w.lon, w.lat)
            ):
                # Waypoint lat/lon falls on the peer sheet's side of
                # the chart-pixmap seam; let the peer's overlay
                # paint it. Without this skip, the user sees doubled
                # markers for every waypoint inside the overlap
                # strip (one item per sheet's overlay, both rendered
                # at the same z).
                continue

            chart_pos = QPointF(cx, cy)

            if is_airport:
                airport_item = _WaypointMarkerItem(
                    kind="airport",
                    label_text=_label_text_for(w),
                    font=label_font,
                    side_px=triangle_side_px,
                    parent=None,
                )
                # Airport markers sit at a slightly lower z so any
                # reporting-point marker drawn on the same chart
                # position (Massada) paints on top.
                airport_item.setZValue(z_value - 0.5)
                self._chart_positions.append(chart_pos)
                self._items.append(airport_item)
                if scene is not None:
                    scene.addItem(airport_item)

            if kind in ("mandatory", "on_demand", "unknown"):
                rpt_item = _WaypointMarkerItem(
                    kind=kind,
                    label_text=_label_text_for(w),
                    font=label_font,
                    side_px=triangle_side_px,
                    parent=None,
                )
                rpt_item.setZValue(z_value)
                self._chart_positions.append(chart_pos)
                self._items.append(rpt_item)
                if scene is not None:
                    scene.addItem(rpt_item)
        # Apply the initial chart-to-scene transform so the
        # markers land in the right scene position right away,
        # before any live chart move events fire.
        self._apply_chart_transform()
        # Subscribe to chart geometry updates so subsequent
        # pan / scale / rotate events re-flow into the markers.
        # Same listener API as
        # :class:`cvfr_routemaster.satellite_overlay.SatelliteOverlay`
        # uses — graceful no-op against plain
        # :class:`QGraphicsPixmapItem` chart fixtures used in
        # tests.
        add_listener = getattr(
            chart_item, "add_geometry_listener", None
        )
        if callable(add_listener):
            add_listener(self._apply_chart_transform)

    def _apply_chart_transform(self) -> None:
        """Re-position every marker from its stored chart-pixel
        coords through ``chart_item.sceneTransform()``.

        Marker visuals use
        :attr:`QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations`
        so the triangle / label stay device-pixel sized
        regardless of zoom — only the marker's *position* needs
        to follow the chart. That position is just the result of
        mapping the cached ``(cx, cy)`` chart-pixel through the
        chart pixmap's current scene transform.

        Called once at the end of :meth:`__init__` (initial
        placement) and again from the geometry listener on every
        chart move event (the joint LSQ layout apply step, any
        Alt+wheel scale-tick, and future programmatic re-pose
        paths all push intermediate transform updates through).
        """
        chart_st = self._chart_item.sceneTransform()
        for marker, chart_pos in zip(
            self._items, self._chart_positions, strict=True
        ):
            marker.setPos(chart_st.map(chart_pos))

    # ------------------------------------------------------------------
    # Visibility
    # ------------------------------------------------------------------

    def set_visible(self, on: bool) -> None:
        """Toggle visibility for all marker items.

        Idempotent. Cheap — ``setVisible`` on a QGraphicsItem just
        flips a bit and queues a paint; no per-item cost beyond
        the dirty-region update.
        """
        on = bool(on)
        if on == self._visible:
            return
        self._visible = on
        for it in self._items:
            it.setVisible(on)

    def is_visible(self) -> bool:
        """Whether the overlay is currently showing markers."""
        return self._visible

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def teardown(self) -> None:
        """Remove every marker item from the scene; idempotent.

        Mirrors :meth:`SatelliteOverlay.teardown` — same
        chart-clear / closeEvent code path needs both overlays
        gone before the chart pixmap items get re-created.

        Deregisters the chart-geometry listener (if registered)
        before removing items, so a stray geometry-change event
        in the middle of teardown can't fire
        :meth:`_apply_chart_transform` against half-dismantled
        state — the iteration would zip a depleted ``_items``
        list with a depleted ``_chart_positions`` list and
        either crash on ``strict=True`` or silently skip the
        update, depending on call ordering.
        """
        remove_listener = getattr(
            self._chart_item, "remove_geometry_listener", None
        )
        if callable(remove_listener):
            remove_listener(self._apply_chart_transform)
        for item in self._items:
            scene = item.scene()
            if scene is not None:
                scene.removeItem(item)
            # Markers are no longer parented to the chart pixmap
            # (see ``__init__`` docstring) but keep the
            # null-parent call defensively in case a future
            # change re-parents them.
            item.setParentItem(None)
        self._items.clear()
        self._chart_positions.clear()

    # ------------------------------------------------------------------
    # Inspection (mostly for tests)
    # ------------------------------------------------------------------

    def marker_count(self) -> int:
        """Number of waypoints with markers in this overlay."""
        return len(self._items)

    def marker_items(self) -> list[QGraphicsItem]:
        """Snapshot list of marker items, for tests / debugging."""
        return list(self._items)
