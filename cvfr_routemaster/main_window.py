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

from __future__ import annotations

import csv
import logging
import math
import sqlite3
from collections.abc import Callable
from pathlib import Path
from dataclasses import dataclass
from typing import Any

_LOG = logging.getLogger(__name__)

from PySide6.QtCore import (
    QEvent,
    QMetaObject,
    QModelIndex,
    QObject,
    QPointF,
    QSortFilterProxyModel,
    Signal,
    Slot,
    Qt,
    QThread,
    QTimer,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCloseEvent,
    QCursor,
    QFont,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QSplitter,
    QStatusBar,
    QTableView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from cvfr_routemaster.external_map_links import (
    MAP_LINK_PROVIDERS,
    MAP_LINK_ZOOM,
    external_map_url,
    open_external_url,
)
from cvfr_routemaster.font_wheel_resize import CtrlWheelFontResizer
from cvfr_routemaster.calibration_anchors import (
    select_anchors_for_sheet,
    select_overlap_anchors,
)
from cvfr_routemaster.calibration_instruction_dialog import CalibrationInstructionDialog
from cvfr_routemaster.geo_calibration import (
    MIN_ANCHORS,
    CalibrationPoint,
    JointCalibration,
    SheetGeoCalibration,
    build_payload,
    calibration_from_points,
    compute_joint_calibration,
    fingerprints_match,
    load_saved_calibration,
    load_sheet_calibration_or_reason,
    lonlat_to_scene,
    map_layout_matches,
    pdf_fingerprint,
    save_calibration_payload,
    sheet_from_dict,
)
from cvfr_routemaster import APP_NAME, app_title, layout_diag
from cvfr_routemaster.cache_restamp import restamp_sheet_fingerprints
from cvfr_routemaster.chart_download_error_dialog import (
    ACTION_OPEN_SETTINGS,
    ACTION_RETRY,
    ChartDownloadErrorDialog,
)
from cvfr_routemaster.chart_source import (
    ChartFetchError,
    ChartSource,
    cache_path_for_sheet,
    download_chart_pdf,
    load_manifest,
    needs_download,
    normalize_url,
    save_manifest,
)
from cvfr_routemaster.altitude_arrows import (
    AltitudeArrow,
    GeoAltitudeArrow,
    match_altitudes_for_route,
    project_arrows_to_lonlat,
)
from cvfr_routemaster.altitude_worker import AltitudeArrowsWorker
from cvfr_routemaster.map_graphics_view import MapGraphicsView
from cvfr_routemaster.route import (
    FlightPlanParseError,
    ParsedPlanCode,
    ParsedPlanCoord,
    Route,
    default_save_plan_name,
    find_nearest_waypoint,
    great_circle_distance_nm,
    parse_icao_route_string,
)
from cvfr_routemaster.route_panel import RoutePanel
from cvfr_routemaster.ui_theme import apply_dark_theme
from cvfr_routemaster.map_loader import MapLoadWorker, SheetRenderInfo
from cvfr_routemaster.font_settings_dialog import FontSettingsDialog
from cvfr_routemaster.settings_dialog import SettingsDialog
from cvfr_routemaster.settings_store import (
    FontSizes,
    load_airplane_font_sizes,
    load_autoload_enabled,
    load_font_sizes,
    load_map_layout,
    load_map_link_provider,
    load_map_view_navigation,
    load_pdf_paths,
    load_satellite_notice_shown,
    load_satellite_zoom,
    load_show_satellite,
    load_show_vatsim_traffic,
    load_traffic_icon_size_px,
    load_waypoint_marker_size_px,
    load_waypoint_show_latlon_cols,
    load_window_layout,
    save_airplane_font_sizes,
    save_autoload_enabled,
    save_font_sizes,
    save_map_layout,
    save_map_link_provider,
    save_map_view_navigation,
    save_pdf_paths,
    save_satellite_notice_shown,
    save_show_satellite,
    save_show_vatsim_traffic,
    save_traffic_icon_size_px,
    save_waypoint_marker_size_px,
    save_waypoint_show_latlon_cols,
    save_window_layout,
)
from cvfr_routemaster.satellite_dialog import (
    show_completion_toast,
    show_first_download_notice,
)
from cvfr_routemaster.satellite_fetch import (
    count_cached_tiles_in_bbox,
    read_download_state,
    tiles_to_fetch_for_bbox,
)
from cvfr_routemaster.satellite_demand_worker import OnDemandFetchWorker
from cvfr_routemaster.satellite_overlay import (
    ChartSeamPartition,
    MultiZoomSatelliteOverlay,
    select_zoom_for_view_scale,
)
from cvfr_routemaster.plane_tracking import compute_tracking_view_center
from cvfr_routemaster.waypoint_marker_overlay import WaypointMarkerOverlay
from cvfr_routemaster.satellite_tiles import (
    ESRI_ATTRIBUTION,
    ESRI_WORLD_IMAGERY_TEMPLATE,
    ISRAEL_BBOX,
    USER_AGENT as SATELLITE_USER_AGENT,
    TileCache,
    TileCoord,
    count_tiles_for_bbox,
)
from cvfr_routemaster.satellite_worker import SatelliteWorker
from cvfr_routemaster.traffic_demo import demo_pilots  # noqa: F401  (kept for tests + future demo-mode toggle)
from cvfr_routemaster.traffic_overlay import WAKE_COLOR, TrafficOverlay
from cvfr_routemaster.vatsim_feed import (
    Pilot,
    WAKE_UNKNOWN,
    load_aircraft_wake_db,
)
from cvfr_routemaster.vatsim_worker import VatsimWorker
from cvfr_routemaster.waypoint_cache import load_cached_waypoints, save_waypoint_cache
from cvfr_routemaster.waypoints import WaypointRecord, load_waypoints_from_back_pdf, records_to_sqlite


_COLS = [
    "Code",
    "Name",
    "Type",
    "Lat °",
    "Lon °",
    "Lat DMS",
    "Lon DMS",
]

# Indices of the four numeric lat/lon columns (decimal °, DMS for both axes).
# These are the columns hidden by the "Show lat/lon columns" toggle on the
# waypoint pane. Computed by name so a future column reorder of ``_COLS``
# stays in sync without anyone having to remember magic indices here.
_WAYPOINT_LATLON_COL_INDICES: tuple[int, ...] = tuple(
    _COLS.index(name) for name in ("Lat °", "Lon °", "Lat DMS", "Lon DMS")
)

# Link styling shared with route_panel; see waypoint_styles for rationale.
from cvfr_routemaster.waypoint_styles import (  # noqa: E402  (grouped here for related-imports clarity)
    WAYPOINT_CODE_LINK_GREEN as _WAYPOINT_CODE_LINK_GREEN,
    WAYPOINT_NAME_LINK_BLUE as _WAYPOINT_NAME_LINK_BLUE,
)

# Type-column foreground palette + Hebrew reporting-type literals are shared with
# the route panel; keeping them in one module guarantees both tables colour-code
# identically. See ``waypoint_styles`` for the full rationale per entry.
from cvfr_routemaster.waypoint_styles import (
    HE_MANDATORY as _HE_MANDATORY,  # noqa: F401  (kept exported for any back-pages tooling that imports it)
    HE_ON_DEMAND as _HE_ON_DEMAND,  # noqa: F401
    REPORTING_TYPE_COLORS as _REPORTING_TYPE_COLORS,
)

# Number of *edge* anchors per sheet for the multi-anchor affine LSQ calibration. The
# model has 6 degrees of freedom and needs ``MIN_ANCHORS = 3`` (geo_calibration.py); 4
# edge anchors give one redundant equation pair so click noise averages out, with
# diminishing returns past that. Bump to 5 if anchors still drift on a particular chart;
# never drop below 3.
_CALIBRATION_EDGE_ANCHOR_TARGET = 4

# Number of *shared overlap* anchors per sheet. These get clicked once on each sheet
# during calibration so both affines are pinned to the same lat/lon inside the chart
# overlap strip — that's what stops the satellite-tile discontinuity along the seam
# (the affines used to extrapolate tens of pixels into the strip, disagreeing).
# Three anchors (Sderot at the north edge, Omer at the south edge, Ein Gedi to the
# east) form a triangle that fully brackets the seam strip — see
# ``calibration_anchors._DEFAULT_OVERLAP_ANCHORS`` and ``_PREFERRED_OVERLAP_CODES``
# for the geometric rationale.
_CALIBRATION_OVERLAP_ANCHOR_TARGET = 3

# Total clicks the user makes per sheet during calibration. Edge anchors come first,
# then the shared overlap anchors west-to-east. Kept as a derived constant so callers
# that just want "how many clicks will I be asked for?" don't have to add the two
# halves themselves.
_CALIBRATION_ANCHOR_TARGET = (
    _CALIBRATION_EDGE_ANCHOR_TARGET + _CALIBRATION_OVERLAP_ANCHOR_TARGET
)

# After both sheets are calibrated, we jointly LSQ-solve the two affines and
# the south layout so the chart-pixmap layout, the satellite-tile placement,
# and the chart-feature alignment at the shared anchors are all
# simultaneously optimal. Two residuals come out of that fit:
#
#   * ``chart_residual_px`` — chart-on-chart misalignment at shared anchors
#     (the user-visible chart pixmap seam jump).
#   * ``consistency_residual_px`` — sat-tile placement disagreement at shared
#     anchors (what drives the visible sat-stitch step at the seam).
#
# Both cannot be driven to zero simultaneously when the chart's underlying
# Lambert projection doesn't fit a 6-DoF affine exactly — the joint LSQ
# balances them. On a clean calibration the larger of the two should still
# come in well under this threshold; if it doesn't, a click is likely
# off-centre and the user should re-calibrate. Set generously above the
# typical ~6 px joint-LSQ floor (vs the previous ~3 px click-only
# threshold) because the joint fit makes a different, more conservative
# trade-off than the legacy click-only layout did.
_OVERLAP_ALIGNMENT_WARN_PX = 12.0

# --- Route-on-map overlay styling ----------------------------------------
#
# Marker-pen aesthetic for the planned-route polyline: bold red, semi-transparent
# so the chart symbols underneath stay legible, and round caps/joins so the line
# reads as a single highlighter stroke rather than a series of straight LCD
# segments. The width is *cosmetic* (in device pixels, not scene units), so the
# stroke stays the same on-screen weight regardless of the user's zoom level —
# zooming in to read a fix shouldn't make the overlay swallow the chart.
_ROUTE_OVERLAY_RGBA: tuple[int, int, int, int] = (220, 38, 38, 150)
_ROUTE_OVERLAY_WIDTH_PX: float = 21.0
# Z-value placed above both sheets (north=0, south=10) but well below any future
# cursor / hint overlay so dialog-style transient items can still float above.
_ROUTE_OVERLAY_Z: float = 100.0

# Origin marker — a transparent solid-fill dot drawn over the route's first
# point so a freshly-set origin is visually obvious even when no second point
# has been added yet. Without this, a single-point route is just an empty
# chart with a row in the panel; the user can't tell which fix they actually
# clicked. It also keeps the start-point legible after the rest of the route
# is removed (or after Clear leaves only the origin behind).
#
# Diameter is *twice* the line width per the spec — when the polyline starts
# rendering on top, the dot still pokes out past it on either side, giving
# the start point an unmistakable "you are here" emphasis. RGBA matches the
# line colour exactly so the dot reads as the same ink with extra emphasis,
# not as a separate visual element.
_ROUTE_ORIGIN_MARKER_DIAMETER_PX: float = 2.0 * _ROUTE_OVERLAY_WIDTH_PX
# Just above the polyline so the dot draws over the line's starting endpoint
# rather than being half-hidden by it.
_ROUTE_ORIGIN_MARKER_Z: float = _ROUTE_OVERLAY_Z + 1.0


# Route-click snap tolerances (great-circle nautical miles).
#
# *Add* uses a 1.0 nm snap. Israeli CVFR points can sit only a few nm apart
# in dense areas (e.g. DAROM ↔ GALIM ≈ 3 nm), but the closer-wins tiebreak
# in ``find_nearest_waypoint`` makes overlapping snap zones safe: even where
# two waypoints both qualify, the nearer one wins, so the only way a user
# can land an intermediate is by clicking ≥ 1.0 nm from any named fix —
# which they essentially never want to do anyway except inside the tight
# IKKEA / MEHOL / LLRS / SUPER / NTAIM cluster (where the user's intent is
# almost always one of those named fixes, not an intermediate bend).
#
# The 1.0 nm value is sized to absorb a known back-pages-table anomaly:
# SIRNI's published coordinates (31° 55'41" N, 34° 49'48" E) sit ~0.96 nm
# NE of where the chart artist drew SIRNI's triangle (and where Wikipedia
# places the Netzer Sereni kibbutz). A click on the visible triangle was
# therefore falling through to "intermediate" under the previous 0.5 nm
# snap. Bumping to 1.0 nm picks SIRNI up by 0.04 nm — see the pair-overlap
# analysis below for why "barely fits" is acceptable here and why the
# closer-wins rule keeps the dense cluster unambiguous.
#
# Pair-overlap profile across the 198-waypoint database at 1.0 nm:
# only 6 pairs fall under the snap radius (all "real" geographic
# neighbours): IKKEA↔MEHOL (0.65), MEHOL↔LLRS (0.67), IKKEA↔NTAIM (0.71),
# SUPER↔LLRS (0.81), ZRANA↔RANNO (0.82), EVLYM↔GILAM (0.96). Every other
# pair stays ≥ 1.0 nm apart, so increasing the radius to 1.0 leaves single-
# waypoint snap zones genuinely non-ambiguous everywhere except this
# cluster, where closer-wins handles it.
#
# Trade-off: a click on the SIRNI triangle now snaps to *SIRNI*, but the
# route point adopts the published (table) coords — not the click coords —
# so the drawn polyline lands ~0.55 nm NE of the chart icon for SIRNI
# specifically. Same compromise applies for any future similar table-vs-
# chart drift. Accepted: the named-fix label is operationally important
# (filed plan, ATC) and the geometry error is below chart precision.
#
# *Remove* keeps a much looser tolerance — the user is intentionally
# targeting a specific route point, so forgiveness on the click is the
# right UX. Unchanged at 4.0 nm.
_ROUTE_ADD_SNAP_NM: float = 1.0
_ROUTE_REMOVE_SNAP_NM: float = 4.0


#: Mean Earth radius in nautical miles. Used by :func:`_haversine_nm`
#: for the view-info status-bar label. The "63" mnemonic is also worth
#: keeping in mind — one minute of arc on a great circle is one
#: nautical mile by definition (the meter was a deliberate
#: re-derivation a century later), so ``circumference / (360 × 60) ==
#: 1 NM``, i.e. R_nm = (60 × 180) / π ≈ 3440.065. We use the standard
#: WGS-84-ish mean value (3440.065) rather than the slightly different
#: polar / equatorial extremes; the resulting <0.3 % error is
#: negligible against the 1-NM precision the indicator renders.
_EARTH_RADIUS_NM: float = 3440.065

#: How long the satellite-overlay refresh queue accumulates per-tile
#: fetch signals before draining them as a single batched
#: ``refresh_from_cache`` call. Tuned for the dual goals of (a)
#: visible "tile appears within X ms of landing in cache" feedback
#: — ~30 ms is below the human flicker-fusion threshold so it reads
#: as instantaneous — and (b) coalescing enough signals together
#: that GUI-thread work amortises (a single batch of 50 tiles costs
#: roughly the same as 5 individual refreshes thanks to the per-call
#: dict-walk overhead being paid once). Increasing this beyond ~80
#: ms produces a perceptible "tile arrives late" lag during bulk
#: download; decreasing below ~10 ms eats the coalescing benefit.
SATELLITE_REFRESH_DEBOUNCE_MS: int = 30

#: Delay between successive batched cache-hit decode passes when
#: a viewport refresh / zoom-level switch ran into the per-call
#: load cap and still has visible tiles waiting to load.
#:
#: ~30 ms is short enough that the user perceives the imagery as
#: "streaming in" rather than "loaded all at once then paused"
#: (the threshold below which successive frames blur into smooth
#: motion is ~16 ms / 60 Hz; 30 ms is comfortably within the
#: smooth band for a low-frequency event like tile fill-in).
#: It's also long enough that Qt's paint event gets to run
#: between batches — a 0 ms reschedule starves repaint in
#: practice because the singleShot fires inside the same event
#: loop iteration as the previous batch's return.
SATELLITE_VISIBILITY_CONTINUATION_MS: int = 30

#: Maximum queued-but-not-yet-drained per-tile refresh coords.
#: Bounds memory and GUI-thread work if the timer is starved (e.g.
#: a long-running paint stalls the event loop). At the cap we
#: silently drop the oldest queued coords — the next visibility
#: sweep through ``_update_satellite_visibility`` will rediscover
#: any coords we lost and re-enqueue them via the on-demand worker
#: if they're actually visible. Set well above the typical
#: visibility-sweep miss count (~200) so this is a true overflow
#: guard, not an everyday throttle.
SATELLITE_REFRESH_QUEUE_CAP: int = 2000


def _haversine_nm(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance between two lat/lon points, in nautical
    miles.

    Pure spherical haversine; we ignore WGS-84 ellipsoidal flattening
    because the relative error (≤0.5 % at any latitude) is dwarfed by
    every other source of uncertainty in the view-width readout
    (calibration residual ≈ a few % at Israel scale, viewport-corner
    rounding, sheet-to-sheet projection offsets). Adding pyproj just
    for the polish would be overkill.

    Inputs in *degrees* — the function does the radians conversion
    internally. Returns nautical miles to match the cockpit-facing
    units the rest of the app uses (route legs, distances in the
    waypoint table, etc.).
    """
    rlat1 = math.radians(lat1)
    rlat2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2.0) ** 2
    )
    # ``atan2`` over ``asin`` for numerical stability at near-antipodal
    # points; immaterial for the few-hundred-NM extents we ever pass
    # in here, but the cost is identical and the habit is right.
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return _EARTH_RADIUS_NM * c


class _ChartSheetItem(QGraphicsPixmapItem):
    """Chart pixmap item that broadcasts geometry changes to listeners.

    Drop-in :class:`QGraphicsPixmapItem` for the north / south
    sheet pixmaps with one extra capability: per-instance Python
    callbacks invoked whenever the item's pos / scale / rotation /
    transform changes. Used by :class:`SatelliteOverlay` and
    :class:`WaypointMarkerOverlay` to recompute the
    chart-to-scene transform for their *top-level* scene items —
    those overlays no longer parent their items under the chart
    pixmap (parented children can't paint above their parent's
    top-level siblings, which is what produced the missing-
    satellite-stripe across the sheet stitch zone), so they need
    an explicit "the chart just moved" signal.

    Why a subclass and not the existing
    ``persist_map_layout()`` chokepoint: that fires only on
    scale-tick / reset / joint-calibration apply, but programmatic
    setPos / setScale paths (e.g. the auto-anchor alignment step,
    or future "animate this chart into place" code) can issue many
    intermediate updates that need to push through to the overlays
    before persist runs. Children of the chart pixmap track those
    intermediate updates automatically (Qt's parent-child
    transform inheritance); top-level overlay items must catch
    each one or they'll visibly lag behind the chart.
    ``itemChange`` is Qt's hook for *every* such update.

    Listener safety: callbacks are invoked from within
    ``itemChange``, which Qt calls during scene-graph maintenance
    — we wrap each invocation in a try/except so an overlay
    blowing up during teardown doesn't poison the whole chart
    pipeline (Qt would propagate the exception out of itemChange
    and into the event loop, killing later listeners and possibly
    the next frame).
    """

    # `_listeners` is a per-instance attribute set in __init__;
    # using a class-level default of `()` would tempt aliasing
    # if a callback mutated it (which we don't, but a sharp edge
    # we shouldn't leave lying around).
    _geometry_listeners: list[Callable[[], None]]

    def __init__(self, pixmap: QPixmap) -> None:
        super().__init__(pixmap)
        # ItemSendsGeometryChanges = 0x800 — without this flag,
        # ``itemChange`` is NOT called for ItemPositionHasChanged
        # (Qt's optimisation default skips the dispatch). The
        # other "Has*" notifications fire regardless of the flag,
        # but setting it here keeps the rule explicit and matches
        # Qt's documentation guidance.
        self.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges,
            True,
        )
        self._geometry_listeners = []

    def add_geometry_listener(self, callback: Callable[[], None]) -> None:
        """Register ``callback`` to be invoked on pos / scale /
        rotation / transform changes. Idempotent at the
        ``callback`` identity level — registering the same
        function twice does call it twice (intentional, matches
        Qt's signal/slot semantics)."""
        self._geometry_listeners.append(callback)

    def remove_geometry_listener(self, callback: Callable[[], None]) -> None:
        """Unregister a previously-added callback. Silently
        ignores non-registered callbacks so teardown is safe to
        call defensively."""
        try:
            self._geometry_listeners.remove(callback)
        except ValueError:
            pass

    def itemChange(
        self,
        change: QGraphicsItem.GraphicsItemChange,
        value: Any,
    ) -> Any:
        # The four "Has*Changed" notifications fire *after* the
        # item's geometry attribute is already updated, so
        # listeners reading ``self.sceneTransform()`` see the new
        # transform — exactly what overlays need to recompute
        # their tile placements.
        if change in (
            QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged,
            QGraphicsItem.GraphicsItemChange.ItemScaleHasChanged,
            QGraphicsItem.GraphicsItemChange.ItemRotationHasChanged,
            QGraphicsItem.GraphicsItemChange.ItemTransformHasChanged,
        ):
            # Snapshot the list before iterating in case a
            # listener triggers a teardown that mutates it
            # (overlays remove themselves on teardown).
            for cb in list(self._geometry_listeners):
                try:
                    cb()
                except Exception:
                    # Don't let one bad overlay break the whole
                    # chart pipeline — Qt is already mid-
                    # itemChange and propagating from here
                    # poisons subsequent notifications. The
                    # offending overlay will need its own
                    # logging.
                    pass
        return super().itemChange(change, value)


def _prepare_map_sheet_item(item: QGraphicsPixmapItem) -> None:
    """Pivot every chart-pixmap scale (the joint LSQ's setScale, the
    Alt+wheel escape hatch, etc.) around the pixmap centre so saved
    pos/scale reload identically and so scaling visually balloons in
    place rather than skating off the top-left corner."""
    br = item.boundingRect()
    if br.width() > 0 and br.height() > 0:
        item.setTransformOriginPoint(br.center())


def _sort_by_name_he(records: list[WaypointRecord]) -> list[WaypointRecord]:
    """Lexicographic order on Hebrew name (empty names sort first)."""
    return sorted(records, key=lambda r: (r.name_he or ""))


def _force_stop_threads(
    threads: list["QThread | None"],
    *,
    polite_timeout_ms: int = 1500,
    force_timeout_ms: int = 500,
) -> None:
    """Wait briefly for each thread, ``terminate()`` any straggler.

    Extracted from :meth:`MainWindow._stop_workers_for_shutdown`
    so the time-bounded stop logic is testable without standing
    up an entire ``MainWindow``. The signalling phase (set stop
    flags, queue stop slots) is *not* in this helper because it's
    worker-specific; this helper assumes the caller has already
    asked each worker to stop and just enforces the time budget.

    Algorithm:

    1. For each ``QThread`` in ``threads`` (``None`` entries are
       skipped silently — convenient when some workers were never
       started), call ``thread.wait(polite_timeout_ms)``.
    2. If the thread is still ``isRunning()`` after the wait — i.e.
       stuck in I/O, blocking syscall, or just slow to unwind —
       call ``thread.terminate()`` to force-kill the OS thread,
       then ``thread.wait(force_timeout_ms)`` to give the
       termination a moment to complete.
    3. Swallow ``RuntimeError`` per-thread because the underlying
       C++ ``QThread`` object may have been deleted concurrently
       by the ``finished → deleteLater`` wiring; that's the
       outcome we wanted anyway.

    Worst-case wall-clock cost: ``len(threads) × (polite_timeout_ms
    + force_timeout_ms)``. With defaults that's 2 s per stuck
    thread; for three threads on shutdown the worst case is 6 s,
    typical case is ~0 ms (every thread already finished by the
    time we get here).

    ``QThread.terminate()`` is documented as dangerous because it
    leaves resources in an inconsistent state — but on app
    shutdown we don't care, and the satellite cache's tmp-file +
    atomic-rename write discipline limits worst-case damage to a
    single ``.tmp`` file left behind, which is cleaned up on the
    next launch.

    Args:
        threads: List of ``QThread`` instances (or ``None`` for
            workers that were never started). All non-``None``
            threads are processed in input order.
        polite_timeout_ms: Per-thread polite wait, in
            milliseconds. Default 1500 ms — enough for a worker
            sitting in its "check stop flag between iterations"
            loop, not enough to wait out a blocking HTTP call.
        force_timeout_ms: Per-thread post-``terminate`` wait, in
            milliseconds. Default 500 ms — terminate is
            effectively synchronous on every supported platform;
            this is mostly a safety margin.
    """
    for thread in threads:
        if thread is None:
            continue
        try:
            thread.wait(polite_timeout_ms)
            if thread.isRunning():
                thread.terminate()
                thread.wait(force_timeout_ms)
        except RuntimeError:
            # Underlying C++ QThread already destroyed (e.g. via
            # ``finished → deleteLater`` racing this path).
            # That's the outcome we wanted.
            pass


def _plan_satellite_zoom_chain(
    levels: list[int],
) -> list[tuple[int, bool]]:
    """Compute the bulk-fetch chain order for a set of zoom levels.

    Pure function so the policy choice is testable in isolation
    from the QThread / QObject machinery that drives the actual
    fetch. Returns the chain as a list of ``(zoom, persist_state)``
    tuples in execution order.

    Policy:

    * **Coarsest first.** z=12 (~1,300 tiles over Israel) runs
      before z=13 (~5,000) before z=14 (~17,900) before z=15
      (~71,600). The multi-zoom overlay's layered-fallback
      behaviour means whichever zooms are cached are usable
      immediately; running smallest-first hands the user a
      fully-covered satellite layer in minutes rather than
      hours.
    * **``persist_state=True`` only on the finest (last) link.**
      The state file's whole job is enabling cross-session
      resume of *one* big interrupted fetch; only the largest
      zoom is big enough to be worth resuming. Smaller links
      re-enumerate cheaply via :func:`tiles_to_fetch_for_bbox`
      on the next launch (a couple of seconds of ``os.stat``
      calls vs. the multi-hour fetch).
    * **At most one link with persist=True.** If the user
      configured only one zoom (e.g. ``satellite_zoom = 12``,
      collapsing the set to ``[12]``), that one link still gets
      ``persist=True`` — same rationale, "the only download we
      have is the one worth resuming".

    Why this order matters specifically *now*: under the
    pre-anchor-change (`MULTIZOOM_BASE_VIEW_SCALE = 1.0`)
    multi-zoom selector, default fit-to-screen view was on the
    finest zoom (z=14 historically), so the user spent most of
    their time at the finest level and "finest-first" matched
    "useful-first". Under the current `MULTIZOOM_BASE_VIEW_SCALE
    = 6.0` anchor the default is z=12 and z=15 only matters
    when zoomed past view-scale 3.0 — so finest-first would
    make the user stare at a gray map for hours while the only
    zoom they actually use at default view fills in last. This
    helper exists so the policy is unambiguous and a future
    anchor change can't silently invalidate it.

    Args:
        levels: Configured zoom levels. Must be a non-empty
            iterable of integers; duplicates are tolerated
            (deduped) and the input doesn't have to be sorted
            (we sort internally).

    Returns:
        Chain spec — list of ``(zoom, persist_state)`` tuples
        in execution order. Empty input returns ``[]``.

    Examples:
        >>> _plan_satellite_zoom_chain([12, 13, 14, 15])
        [(12, False), (13, False), (14, False), (15, True)]
        >>> _plan_satellite_zoom_chain([14])
        [(14, True)]
        >>> _plan_satellite_zoom_chain([13, 12, 14])
        [(12, False), (13, False), (14, True)]
        >>> _plan_satellite_zoom_chain([])
        []
    """
    deduped = sorted(set(int(z) for z in levels))
    if not deduped:
        return []
    plan: list[tuple[int, bool]] = []
    for z in deduped[:-1]:
        plan.append((z, False))
    plan.append((deduped[-1], True))
    return plan


def _warm_text_rendering_caches() -> None:
    """Prime Qt's text-rendering pipeline for Hebrew script.

    The first user-facing render that contains Hebrew text in this
    app is the route panel's Hebrew route string label, which is
    populated and shown the moment the user adds the first waypoint
    to an empty route. On a cold start — particularly the first run
    on a fresh Debian / Linux desktop — that initial Hebrew paint can
    cost the better part of a second, because Qt has to:

      1. Walk fontconfig to enumerate every installed font that
         declares Hebrew-script coverage, and build a script-tagged
         fallback chain.
      2. Initialise HarfBuzz shaping state for the Hebrew block.
      3. Populate the glyph cache for the actually-drawn glyphs.

    Subsequent Hebrew paints reuse all three caches and are
    effectively free, which is why the user perceives the lag as
    "first source-airport click takes ~1s, everything after is
    instant". The right-hand waypoint table also renders Hebrew
    names — but ``QTableView`` paints cells via the style's
    ``drawItemText`` path, while ``QLabel`` paints via
    ``QStaticText``/``QPainter::drawText``; the two share enough
    underlying machinery to populate fontconfig and HarfBuzz, but
    not enough to spare the label's first paint from a meaningful
    layout cost, so we explicitly exercise the ``QPainter`` path
    here.

    We render a small mixed Hebrew + Latin + digits string into an
    off-screen 1×1 ``QPixmap``. The pixmap is discarded; the
    side-effect we want is purely the cache population. Wrapped in a
    broad ``except`` so a missing Hebrew font (extremely unlikely
    given the OS image, but defensively) downgrades to a no-op
    rather than aborting startup.

    Invoked via :class:`QTimer.singleShot` from the main window's
    constructor so the cost is paid on the next event-loop iteration
    after the window paints — the user sees the window almost
    immediately and the warmup runs invisibly while they read the
    chart for the first time.
    """
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPixmap

        # Mixed-script sample matching the kinds of strings the route
        # panel actually renders: Hebrew name, ICAO code, altitude.
        sample = "דרום LLHA 1600"
        QFontMetrics(QFont()).horizontalAdvance(sample)
        pix = QPixmap(1, 1)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        try:
            painter.drawText(0, 0, sample)
        finally:
            painter.end()
    except Exception:  # noqa: BLE001
        # Warming the cache is purely a perf optimisation — if Qt or
        # fontconfig misbehaves here, the worst case is the original
        # ~1s first-click stall, which is still a working app.
        return


def _make_sim_only_banner() -> QLabel:
    """Build the red 'simulator use only' warning banner that frames the map.

    Used twice — once above the chart view and once below it, between the
    view and the action-hint footer — so the disclaimer stays in the
    user's eye-line whether they're reading the chart or scanning the
    panel headers above it.

    Visual treatment is intentionally loud: bold red text on a light-red
    fill, surrounded by a 2-px solid red border with rounded corners.
    The padding is generous (8 px / 12 px) so the banner reads as a
    distinct pane rather than a stripe of text against the chart.
    """
    label = QLabel("THIS PROGRAM IS FOR SIMULATOR USE ONLY!")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setObjectName("simulatorOnlyBanner")
    label.setStyleSheet(
        "QLabel#simulatorOnlyBanner {"
        "  color: #b91c1c;"
        "  background-color: #fee2e2;"
        "  border: 2px solid #b91c1c;"
        "  border-radius: 6px;"
        "  font-weight: bold;"
        "  font-size: 13px;"
        "  letter-spacing: 1px;"
        "  padding: 6px 12px;"
        "  margin: 0px;"
        "}"
    )
    # Banner must not steal vertical space from the map; size policy is
    # Fixed in the vertical direction so the chart absorbs every spare
    # pixel below it. Horizontal Expanding so the banner spans the full
    # width of the map column.
    from PySide6.QtWidgets import QSizePolicy

    label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return label


class WaypointsOcrWorker(QObject):
    """Runs back-pages OCR on a worker thread (PyMuPDF + Tesseract)."""

    finished = Signal(object, object)
    failed = Signal(str)

    def __init__(self, back_path: str) -> None:
        super().__init__()
        self._back_path = back_path

    def run(self) -> None:
        try:
            records, tag = load_waypoints_from_back_pdf(self._back_path)
            self.finished.emit(records, tag)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class WaypointSortProxy(QSortFilterProxyModel):
    """Stable sorting using ``Qt.UserRole`` when set (numbers vs strings)."""

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        src = self.sourceModel()
        if src is None:
            return False
        col = self.sortColumn()
        li = src.index(left.row(), col, left.parent())
        ri = src.index(right.row(), col, right.parent())
        vl = src.data(li, Qt.ItemDataRole.UserRole)
        vr = src.data(ri, Qt.ItemDataRole.UserRole)
        if vl is not None and vr is not None:
            try:
                if isinstance(vl, (int, float)) and isinstance(vr, (int, float)):
                    return float(vl) < float(vr)
            except (TypeError, ValueError):
                pass
            return str(vl).casefold() < str(vr).casefold()
        return super().lessThan(left, right)


@dataclass(frozen=True)
class _ChartSeamPartitionBuilder:
    """Reusable scaffolding that turns the *layout-level* chart-seam
    parameters (north's calibration, north's pixmap height, the
    seam's scene-y) into per-sheet :class:`ChartSeamPartition`
    instances on demand.

    Why a builder rather than a single shared :class:`ChartSeamPartition`:
    the partition's ``self_is_north`` flag differs between the
    north and south overlays, but every other field is identical.
    Constructing the partition twice independently (once per
    overlay) duplicates the layout-math and is error-prone if one
    callsite drifts. A small builder keeps the shared fields in
    one place and stamps out the two per-sheet variants by
    flipping just that one bit.

    The builder is produced by
    :meth:`MainWindow._build_chart_seam_partition` and consumed by
    the satellite + waypoint marker overlay constructors via
    ``builder.for_north()`` / ``builder.for_south()``.
    """

    north_calibration: SheetGeoCalibration
    north_pixmap_height: float
    chart_seam_scene_y: float

    def for_north(self) -> ChartSeamPartition:
        """Partition the north overlay should use."""
        return ChartSeamPartition(
            north_calibration=self.north_calibration,
            north_pixmap_height=self.north_pixmap_height,
            chart_seam_scene_y=self.chart_seam_scene_y,
            self_is_north=True,
        )

    def for_south(self) -> ChartSeamPartition:
        """Partition the south overlay should use."""
        return ChartSeamPartition(
            north_calibration=self.north_calibration,
            north_pixmap_height=self.north_pixmap_height,
            chart_seam_scene_y=self.chart_seam_scene_y,
            self_is_north=False,
        )


# ---------------------------------------------------------------------------
# Chart-source helpers (module-level so tests can exercise them without
# spinning up a full MainWindow)
# ---------------------------------------------------------------------------


_SHEET_KEY_BY_INDEX: tuple[str, ...] = ("north", "south", "back")


def _resolve_chart_sources_silent(
    sources: tuple[str, str, str],
    project_root: Path,
) -> tuple[str, str, str]:
    """Eagerly resolve URL-cached sources to their local-file paths
    WITHOUT triggering any network call.

    Per-source behaviour:

    * **Empty string** -> empty string (no-op).
    * **Local path** -> the path string unchanged. The caller's
      ``is_file()`` gates handle a path that doesn't exist on
      disk (it just looks "not loaded yet").
    * **URL with a cache hit** (manifest URL matches AND the
      cached PDF file exists) -> the local cache path. This is
      the returning-user steady-state path; the load can proceed
      without showing a download progress dialog at all.
    * **URL with no cache hit** -> empty string. The download
      flow inside ``_ensure_chart_sources_resolved`` will fetch
      it the next time ``_load_all`` runs (autoload-on-startup
      gates on ``_sources_set()``, so this case still triggers
      a load — the empty ``_*_path`` simply means "needs a
      network fetch", not "skip this sheet").

    Returns:
        ``(north_path, south_path, back_path)`` 3-tuple of strings.
        Each entry is either an empty string (source not yet
        resolvable to a real file) or a path to an existing
        on-disk PDF.
    """
    resolved: list[str] = []
    for source, sheet_key in zip(sources, _SHEET_KEY_BY_INDEX):
        if not source:
            resolved.append("")
            continue
        chart_src = ChartSource(raw=source)
        if chart_src.is_url:
            try:
                normalized = chart_src.normalized_url()
                if not needs_download(sheet_key, normalized, project_root):
                    resolved.append(str(cache_path_for_sheet(sheet_key, project_root)))
                    continue
            except (ValueError, OSError):
                # Defensive: a corrupt cache dir / manifest must
                # NOT crash construction. Treat as "not yet
                # resolvable", let the download flow handle it.
                pass
            resolved.append("")
            continue
        # Local-path source — pass through unchanged.
        resolved.append(source)
    return tuple(resolved)  # type: ignore[return-value]


class MainWindow(QMainWindow):
    #: Cross-thread signal used to hand a missing satellite tile coord
    #: to the on-demand fetch worker. The worker's ``enqueue`` slot is
    #: connected to this with a :class:`Qt.QueuedConnection`, so emitting
    #: from the GUI thread reliably routes the call to the worker's
    #: own event loop without us having to fiddle with
    #: :class:`QMetaObject.invokeMethod` + :class:`Q_ARG`.
    #:
    #: This signal replaces a direct ``QMetaObject.invokeMethod(...,
    #: Q_ARG(object, coord))`` call we used to make from
    #: :meth:`_update_satellite_visibility`. That worked on older
    #: PySide6 builds but recent releases tightened the
    #: ``Q_ARG`` type-lookup path and reject the bare ``object``
    #: meta-type with ``RuntimeError: qArgDataFromPyType: Unable to
    #: find a QMetaType for "object"``. A vanilla Python-typed signal
    #: bypasses ``Q_ARG`` entirely — Qt's signal machinery handles
    #: cross-thread Python-object marshalling via ``PyObject*`` and
    #: queued connections internally, with no metatype registration
    #: required.
    _satellite_enqueue_tile = Signal(object)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._table.viewport() and event.type() == QEvent.Type.MouseMove:
            if isinstance(event, QMouseEvent):
                idx = self._table.indexAt(event.position().toPoint())
                if idx.isValid() and idx.column() in (0, 1):
                    self._table.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                else:
                    self._table.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        elif self._calibration_overlay is not None and obj is self._view.viewport():
            t = event.type()
            if t == QEvent.Type.Resize:
                self._position_calibration_overlay()
            elif (
                t == QEvent.Type.MouseMove
                and self._calibration_overlay.isVisible()
                and isinstance(event, QMouseEvent)
            ):
                self._follow_calibration_overlay_to_cursor(event.position().toPoint())
        return super().eventFilter(obj, event)

    def __init__(self, project_root: Path) -> None:
        super().__init__()
        # Stand up the layout diagnostic logger before anything else touches a
        # sheet item: every setPos / setScale event below this point gets
        # captured for the south-sheet startup-move investigation.
        layout_diag.init(project_root)
        # Attributes referenced by eventFilter MUST be set before any installEventFilter
        # call below (Qt may invoke the filter during widget construction).
        self._calibration_overlay: QLabel | None = None
        self._calibration_reticle_cursor: QCursor | None = None
        self._project_root = project_root
        self._did_place_window = False
        self.setWindowTitle(app_title())
        # Title-bar icon. Qt sources the title-bar icon from the
        # window itself (NOT from ``QApplication.windowIcon``), so
        # setting it on the app alone leaves the MainWindow's chrome
        # showing the default. ``app_icon()`` returns an empty
        # QIcon when the bundled PNG is missing, and ``setWindowIcon``
        # is a no-op on an empty QIcon, so this stays safe on a
        # fresh checkout that hasn't run the icon generator.
        from cvfr_routemaster.app_icon import app_icon

        self.setWindowIcon(app_icon())
        self.resize(1600, 900)

        # Two parallel triples for chart sources:
        #
        #   * ``_source_*`` is the raw QSettings value (could be a
        #     local path OR an http(s):// URL). This is the "intent"
        #     persisted across launches.
        #   * ``_*_path`` is the resolved local filesystem path that
        #     PyMuPDF and every downstream cache module actually
        #     opens. For a path source it equals the source verbatim;
        #     for a URL source it equals the cached PDF location
        #     under ``<project_root>/.cvfr_routemaster/charts/``,
        #     but ONLY once the download has succeeded. Until then
        #     ``_*_path`` is the empty string and the download flow
        #     inside ``_ensure_chart_sources_resolved()`` handles
        #     the network fetch when ``_load_all`` runs.
        #
        # ``_resolve_chart_sources_silent`` does the eager resolve
        # at construction time: any URL source whose cache file is
        # already present and matches the manifest URL gets its
        # ``_*_path`` set immediately, so a returning user who
        # downloaded charts last session sees the autoload fire
        # without any network round-trip. A first-time user whose
        # ``_*_path`` ends up empty still triggers autoload (the
        # gate in ``_maybe_autoload_on_start`` is sources-set, not
        # paths-valid) — the empty path simply signals "needs a
        # network fetch" to ``_ensure_chart_sources_resolved``.
        (
            self._source_north,
            self._source_south,
            self._source_back,
        ) = load_pdf_paths(project_root)
        (
            self._north_path,
            self._south_path,
            self._back_path,
        ) = _resolve_chart_sources_silent(
            (self._source_north, self._source_south, self._source_back),
            project_root,
        )
        layout_diag.log(
            "session.paths",
            autoload=load_autoload_enabled(),
            source_north=self._source_north,
            source_south=self._source_south,
            source_back=self._source_back,
            north=self._north_path,
            south=self._south_path,
            back=self._back_path,
        )

        self._db = sqlite3.connect(":memory:")
        self._db.row_factory = sqlite3.Row

        self._scene = QGraphicsScene(self)
        self._north_item: _ChartSheetItem | None = None
        self._south_item: _ChartSheetItem | None = None
        self._selected: str = "south"
        # Polyline overlay drawn on top of the chart sheets so the planned route
        # is visible at a glance. Lazily created in _redraw_route_overlay() and
        # cleared whenever the maps reload, the route changes, or a sheet is
        # moved/scaled (so the line stays anchored to its lat/lon).
        self._route_overlay_item: QGraphicsPathItem | None = None
        # Companion origin-marker dot for the route's first point. Drawn even
        # when the polyline isn't (single-point route) so a just-set origin is
        # always visible. Same lazy lifecycle as ``_route_overlay_item``.
        self._route_origin_marker_item: QGraphicsEllipseItem | None = None

        # Live VATSIM traffic overlay (v2 feature; see ROADMAP-NEXT.md and
        # cvfr_routemaster.traffic_overlay). Always-instantiated manager so
        # call sites can ``set_pilots`` / ``clear`` without nil-checks; the
        # current visibility state is owned by the toolbar toggle. The
        # projection callback flips argument order — TrafficOverlay takes
        # ``(lon, lat)`` (matches the VATSIM v3 datafeed field order),
        # ``_project_route_point_to_scene`` takes ``(lat, lon)``. Eagerly
        # constructed *before* ``_build_actions`` runs so the toggle's
        # initial ``setChecked(...)`` (which can fire ``toggled`` if the
        # saved state is True) finds the overlay already in place.
        self._traffic_overlay = TrafficOverlay(
            self._scene,
            lambda lon, lat: self._project_route_point_to_scene(lat, lon),
        )

        # Live VATSIM poller — lazily created when the user flips the
        # "Show VATSIM traffic" toolbar toggle on, torn down when they
        # flip it off (and on app close). The worker lives on its own
        # QThread so its blocking HTTP fetch never freezes the GUI;
        # ``pilots_updated`` and ``fetch_failed`` are queued back to
        # the GUI thread by Qt's signal machinery. See
        # cvfr_routemaster.vatsim_worker for the lifecycle rationale.
        self._vatsim_thread: QThread | None = None
        self._vatsim_worker: VatsimWorker | None = None
        # Latest pilot snapshot from the worker. The overlay redraws
        # use this list (not the demo fixture) once we're live; an
        # empty list is the legitimate "nobody flying in Israeli
        # airspace right now" state, distinct from "we haven't
        # received our first fetch yet" (which is ``None``).
        self._latest_vatsim_pilots: list[Pilot] | None = None

        # Callsign of the VATSIM pilot the user has clicked to
        # "follow". When non-``None`` the viewport re-centres on
        # every fresh VATSIM snapshot (every ~15 s) so the plane
        # stays framed with two-thirds of the viewport ahead of
        # itself along its heading (see
        # :func:`cvfr_routemaster.plane_tracking.compute_tracking_view_center`).
        # The visual side — a yellow halo on the matching plane —
        # lives in :class:`TrafficOverlay` and is kept in sync
        # via :meth:`set_tracked_callsign`. Stored as a string
        # (not as a reference to the ``_TrafficPlaneItem`` itself)
        # because the items are torn down and rebuilt on every
        # snapshot; the callsign is the stable identifier that
        # survives.
        self._tracking_callsign: str | None = None

        # --- v3 satellite-imagery state ---------------------------------
        # See ROADMAP-NEXT.md and cvfr_routemaster.satellite_*. The
        # satellite layer is a per-tile overlay (one
        # ``QGraphicsPixmapItem`` per Web Mercator tile, parented to
        # the chart pixmap and transformed into chart-pixel coords).
        # The overlays toggle visibility together with
        # ``_act_show_satellite``; tiles draw lazily as the cache
        # fills. ``None`` until ``_on_map_finished`` builds them.
        #
        # Each per-sheet overlay is a :class:`MultiZoomSatelliteOverlay`
        # that internally manages one :class:`SatelliteOverlay` per
        # configured zoom level (default ``[12, 13, 14]``). Active
        # zoom is selected from the view scale so zoomed-out users
        # see fewer, coarser tiles instead of a sea of tiny ones.
        self._north_sat_overlay: MultiZoomSatelliteOverlay | None = None
        self._south_sat_overlay: MultiZoomSatelliteOverlay | None = None
        # Bulk-fetch chain state. The chain runs *coarsest-first*
        # so a fresh user gets a usable z=12 satellite layer (full
        # Israel coverage in ~1,300 tiles, minutes at polite
        # pacing) before any of the bigger zooms eat their hours
        # of download. Only the largest zoom (highest configured,
        # typically z=15) writes the cache's download-state JSON,
        # because that's the only fetch big enough that a user
        # would want interrupt-and-resume across sessions; the
        # smaller links re-enumerate cheaply via the on-disk
        # cache on the next launch.
        #
        # Each tuple is ``(zoom_level, persist_state)``. The chain
        # is built up-front in :meth:`_start_satellite_worker` and
        # popped one link at a time in
        # :meth:`_on_satellite_finished`. Empty list = "no more
        # links queued" = "we're done or only one link was
        # planned and we're on it now".
        self._satellite_pending_zoom_chain: list[tuple[int, bool]] = []
        self._satellite_running_zoom: int = 0
        # Per-zoom download progress for the multi-line status-bar
        # readout. Populated at app-load time from
        # :func:`tiles_to_fetch_for_bbox` (which checks the cache
        # directly) so the label is accurate even before any worker
        # signal has fired this session, and updated live by the
        # bulk worker's ``progress`` / ``tile_fetched`` /
        # ``finished`` signals. Schema:
        #
        #     {z: {'completed': int, 'total': int, 'done': bool}}
        #
        # ``done`` is the explicit "this zoom is fully cached" flag
        # set when the worker emits ``finished`` for a zoom whose
        # ``completed == total``; we can't infer it from the ratio
        # alone because Esri returns 404 for tiles outside its
        # coverage area (treated as ``completed`` but not actually
        # on disk). Reset at app start; not persisted.
        self._sat_progress_per_zoom: dict[int, dict[str, int | bool]] = {}
        # Per-sheet waypoint-marker overlay. Renders triangles +
        # code labels as scene items so they remain visible (and
        # the chart-pixmap-baked triangles remain clickable for
        # routing) when satellite imagery covers the chart pixmap.
        # ``None`` until ``_on_map_finished`` builds them, same
        # lifecycle as the satellite overlays.
        self._north_wp_marker_overlay: WaypointMarkerOverlay | None = None
        self._south_wp_marker_overlay: WaypointMarkerOverlay | None = None
        # Bulk-fetch worker + thread; lazily spun up the first time
        # the chart loads (via :meth:`_show_first_download_notice_and_start`
        # for fresh installs or :meth:`_check_satellite_resume_on_startup`
        # for returning users with partial caches) and torn down
        # on completion / app exit. ``None`` means "not running".
        self._satellite_thread: QThread | None = None
        self._satellite_worker: SatelliteWorker | None = None
        # On-demand fetch worker + thread; lazily spun up the first
        # time the visibility walk surfaces a missing visible tile,
        # then runs for the rest of the session. Distinct from the
        # bulk worker — see :mod:`cvfr_routemaster.satellite_demand_worker`
        # for the rationale (different work-shape, different lifecycle).
        # ``None`` means "not running"; a present worker may have an
        # empty queue (idle).
        self._satellite_demand_thread: QThread | None = None
        self._satellite_demand_worker: OnDemandFetchWorker | None = None
        # Debounce / coalesce buffer for per-tile overlay refreshes
        # driven by the bulk-fetch worker (``tile_fetched``) and
        # the on-demand worker (``tile_ready``). The pre-fix
        # behaviour was one ``refresh_from_cache(only_coords=
        # [coord])`` call per signal — at high tile arrival rates
        # (>50 / s, easily hit by either worker on a fast
        # connection) this serialised the GUI thread on JPEG
        # decode + ``setPixmap`` + repaint, which is the user-
        # reported "really jarring when you move around" symptom.
        # Coalescing into one batch per ~30 ms slashes the
        # per-tile overhead and makes pan/zoom feel smooth
        # while imagery streams in. Cap guards against unbounded
        # growth if the GUI thread is wedged for some other
        # reason — at the cap we drop oldest queued coords (the
        # next visibility sweep will rediscover them).
        self._pending_satellite_refresh: list[TileCoord] = []
        self._satellite_refresh_timer = QTimer(self)
        self._satellite_refresh_timer.setSingleShot(True)
        self._satellite_refresh_timer.setInterval(
            SATELLITE_REFRESH_DEBOUNCE_MS
        )
        self._satellite_refresh_timer.timeout.connect(
            self._drain_satellite_refresh_queue
        )
        # Status-bar progress widget (lazily created on first attach
        # so users who never enable satellite mode pay zero pixels).
        self._sat_progress_label: QLabel | None = None
        # Permanent attribution label at the bottom-right; required by
        # Esri's ToS when their imagery is on screen. Visibility is
        # bound to the satellite-view toggle so it doesn't shout at
        # the user when they're in chart mode.
        self._sat_attribution_label: QLabel | None = None
        # Diagnostic status-bar widget showing the current viewport
        # width in nautical miles and the satellite-overlay zoom
        # level that the current view-scale would select. The
        # user-facing purpose is twofold: (a) pilots planning a
        # leg want to eyeball "how far across is this view" at a
        # glance without measuring on the chart, and (b) the
        # zoom-level readout makes it possible to verify that
        # multi-zoom switching is firing where you'd expect — if
        # you zoom out and the indicator never crosses from z=14
        # to z=13 to z=12, the switching logic is broken (or the
        # view scale never reached the thresholds, which itself
        # is a useful thing to see). Built lazily like the other
        # satellite labels.
        self._view_info_label: QLabel | None = None
        # First-launch prompt is fired exactly once per session
        # (after the chart loads, which is the first moment when
        # showing a modal makes sense). This guard short-circuits
        # subsequent map reloads from re-prompting.
        self._sat_first_launch_prompt_shown: bool = False

        self._view = MapGraphicsView()
        self._view.setScene(self._scene)
        self._view.set_controller(self)
        # Debounced satellite-overlay visibility update. The view
        # emits ``viewport_changed`` from scroll + resize + zoom-
        # pan; the timer collapses bursts of rapid emissions
        # (touchpad scroll, drag-pan) into a single
        # ``update_visibility`` call ~100 ms after the user stops
        # interacting. Without the debounce, a 60 fps scroll would
        # run a 10 k-item walk every frame and stutter on weaker
        # boxes.
        self._sat_visibility_timer = QTimer(self)
        self._sat_visibility_timer.setSingleShot(True)
        self._sat_visibility_timer.setInterval(100)
        self._sat_visibility_timer.timeout.connect(
            self._update_satellite_visibility
        )
        self._view.viewport_changed.connect(
            self._schedule_satellite_visibility_update
        )
        # The view-info label refreshes immediately on every
        # viewport_changed (no debounce). The work is trivial — a
        # handful of arithmetic ops + one ``setText`` — so we
        # update at full event-stream rate rather than waiting
        # for the satellite-overlay debounce to settle. That
        # lets the user see the indicator move in lock-step with
        # the scroll wheel, which is what makes it useful as a
        # zoom-switch verification tool. Cheap and high-value.
        self._view.viewport_changed.connect(
            self._update_view_info_label
        )

        self._wp_model = QStandardItemModel(0, len(_COLS))
        self._wp_model.setHorizontalHeaderLabels(_COLS)
        self._wp_proxy = WaypointSortProxy(self)
        self._wp_proxy.setSourceModel(self._wp_model)
        self._wp_proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self._wp_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._wp_proxy.setFilterKeyColumn(-1)

        self._table = QTableView()
        self._table.setModel(self._wp_proxy)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        # Columns auto-size to content, the trailing column does NOT stretch to fill —
        # together with the maximum-width pin set in _apply_table_natural_width(), this
        # keeps the table exactly content-wide so the rightmost column never floats off
        # against the splitter edge when the right pane gets wider than the data needs.
        h_header = self._table.horizontalHeader()
        h_header.setStretchLastSection(False)
        h_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.clicked.connect(self._on_waypoint_table_clicked)
        self._table.viewport().installEventFilter(self)
        # Track scrollbar appearance/disappearance so the natural-width pin recomputes
        # when filtering or sorting changes whether the vertical scrollbar is shown.
        self._table.verticalScrollBar().rangeChanged.connect(
            lambda _lo, _hi: self._apply_table_natural_width()
        )

        search = QLineEdit()
        search.setPlaceholderText("Filter (matches any column)…")
        search.textChanged.connect(self._wp_proxy.setFilterFixedString)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Search:"))
        filter_row.addWidget(search, 1)

        self._map_link_provider_combo = QComboBox()
        for pid, label in MAP_LINK_PROVIDERS:
            self._map_link_provider_combo.addItem(label, pid)
        self._map_link_provider_combo.setToolTip(
            "Waypoint ICAO codes open your browser to aerial/satellite views with labels "
            "(zoom matched across Bing, Google, Apple)."
        )
        _lp_ix = self._map_link_provider_combo.findData(load_map_link_provider())
        self._map_link_provider_combo.setCurrentIndex(_lp_ix if _lp_ix >= 0 else 0)
        self._map_link_provider_combo.currentIndexChanged.connect(
            self._on_map_link_provider_changed
        )

        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("Open code links in:"))
        provider_row.addWidget(self._map_link_provider_combo, 1)

        # Optional lat/lon columns. Most flight-planning interactions key off the
        # code, the Hebrew name, and the chart link — the four numeric lat/lon
        # columns are reference info that's rarely consulted, so we hide them by
        # default and let the user opt in. The toggled state is persisted in
        # QSettings so the choice survives across sessions.
        self._show_latlon_chk = QCheckBox("Show lat/lon columns")
        self._show_latlon_chk.setToolTip(
            "Show or hide the four numeric lat/lon columns "
            "(Lat°, Lon°, Lat DMS, Lon DMS) in the waypoint table. "
            "Hidden by default — the code, Hebrew name, and chart link cover "
            "most flight-planning needs and the table stays narrower without them."
        )
        self._show_latlon_chk.setChecked(load_waypoint_show_latlon_cols())
        self._show_latlon_chk.toggled.connect(self._on_show_latlon_toggled)
        # Apply the initial saved state to the empty (header-only) table so the
        # columns start hidden if that's the user's preference, before any rows
        # land. ``_set_waypoint_rows`` later calls the same helper so the state
        # is reapplied after every model rebuild — a model reset would otherwise
        # leak the columns back in if Qt resets section visibility.
        self._apply_latlon_column_visibility()

        latlon_row = QHBoxLayout()
        latlon_row.addWidget(self._show_latlon_chk)
        latlon_row.addStretch(1)

        # Waypoint-table hint — placed *below* the table (added to ``rl``
        # after the ``table_strip`` layout) so the visual flow matches the
        # left and centre panes (header on top, data in the middle, hint
        # at the bottom). Wording calls out that clicking a Hebrew name
        # *maintains the current zoom level*, so the user knows the chart
        # won't snap to a different scale on click.
        wp_table_hint = QLabel(
            "Waypoint table: green ICAO codes open aerial/satellite views; "
            "blue Hebrew names pan the map to that waypoint on the calibrated chart "
            "(maintains current zoom level)."
        )
        wp_table_hint.setWordWrap(True)
        wp_table_hint.setObjectName("mapHint")
        # Stable object name for tests so a future copy-edit of the wording
        # doesn't have to chase its way through ``findChildren`` results.
        wp_table_hint.setProperty("hintRole", "waypointTableHint")

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addLayout(filter_row)
        rl.addLayout(provider_row)
        rl.addLayout(latlon_row)
        # The table sits in an HBox alongside a trailing stretch. Explicit stretch
        # factor 1000:1 means the table greedily claims the full pane width, then
        # ``setMaximumWidth`` (set by _apply_table_natural_width once columns size to
        # content) caps it at the natural content width and Qt redistributes the
        # leftover horizontal space to the trailing stretch — i.e. an empty band on
        # the right of the pane. When the pane is narrower than content the trailing
        # stretch collapses to ~1px and the table's own horizontal scrollbar takes
        # over, exactly the resize behaviour the user asked for.
        table_strip = QHBoxLayout()
        table_strip.setContentsMargins(0, 0, 0, 0)
        table_strip.setSpacing(0)
        table_strip.addWidget(self._table, 1000)
        table_strip.addStretch(1)
        rl.addLayout(table_strip, 1)
        # Hint sits *after* the table strip so it appears at the bottom of the
        # pane, mirroring the left and centre panes' "controls/data above,
        # explanation below" rhythm.
        rl.addWidget(wp_table_hint)
        # Tracked for tests — same rationale as the route-panel's hint reference.
        self._waypoint_table_hint = wp_table_hint
        # Tracked on ``self`` so the airplane-mode toggle can hide the
        # entire waypoint pane (search bar + provider combo + lat/lon
        # checkbox + table + hint) by hiding this one widget. Same
        # QSplitter-collapse mechanism as ``_map_column``.
        self._waypoint_pane = right

        # Map hint — pared down to the navigation primitives that work in
        # both calibrated and uncalibrated states. Sheet-selection click
        # and the Alt+wheel scale escape hatch are part of the
        # calibration workflow (covered by the Map Calibration Options
        # dialog), so they don't belong in the always-visible footer hint
        # where they only invite accidental moves of an already-aligned
        # chart. (The old Alt+drag gesture was removed entirely when the
        # joint LSQ layout solver superseded manual alignment.)
        #
        # Rich text is enabled because the wake-category legend (added
        # when the VATSIM traffic toggle is on — see
        # ``_update_map_hint_text``) uses inline ``<span style="color:
        # ...">`` to colour each category's marker square. Plain ASCII
        # works the same in rich-text mode, so the no-legend case is a
        # straight pass-through with no visual difference.
        self._map_hint = QLabel()
        self._map_hint.setTextFormat(Qt.TextFormat.RichText)
        self._map_hint.setWordWrap(True)
        self._map_hint.setObjectName("mapHint")
        # Initial text without the legend; ``_update_map_hint_text`` is
        # called once the toolbar exists (the legend's visibility is
        # gated on the traffic toolbar toggle, which doesn't exist
        # during this widget-construction pass) so we can't compute
        # the full text here.
        self._map_hint.setText("Map: drag pans · wheel zooms.")

        # Two SIMULATOR-USE-ONLY warning banners frame the map (one above,
        # one below) so the disclaimer is in the user's eye-line both when
        # they're looking at the chart and when they glance at the action
        # hint. Real-world VFR navigation requires the official Israeli
        # CVFR chart in print form and a current ATIS/NOTAM check; this
        # tool is for VATSIM and similar simulator workflows only. The red
        # border + bold red text combination is the standard "do not use
        # this for the real thing" pattern.
        self._sim_only_banner_top = _make_sim_only_banner()
        self._sim_only_banner_bottom = _make_sim_only_banner()

        map_column = QWidget()
        map_col_layout = QVBoxLayout(map_column)
        map_col_layout.setContentsMargins(0, 0, 0, 0)
        map_col_layout.setSpacing(4)
        map_col_layout.addWidget(self._sim_only_banner_top, 0)
        map_col_layout.addWidget(self._view, 1)
        map_col_layout.addWidget(self._sim_only_banner_bottom, 0)
        map_col_layout.addWidget(self._map_hint, 0)
        # Tracked on ``self`` because the airplane-mode toggle hides /
        # restores the entire middle column in one shot (sim-only
        # banners + map view + map hint all together) — the QSplitter
        # collapses the slot when a child is hidden and re-expands it
        # on show, which is the cleanest way to give the route panel
        # the full window width without re-parenting widgets.
        self._map_column = map_column

        # Flight-route state and the left-hand pane that displays it. The panel reads
        # the route on demand (via _refresh_route_panel) and emits speed_changed when the
        # user adjusts the cruise speed; we re-render on either kind of change.
        self._route = Route()
        self._route_panel = RoutePanel()
        self._route_panel.speed_changed.connect(self._on_route_speed_changed)
        self._route_panel.route_point_clicked.connect(self._on_route_point_clicked)
        self._route_panel.reporting_name_clicked.connect(self._on_route_reporting_name_clicked)
        self._route_panel.clear_route_requested.connect(self._on_clear_route_requested)
        self._route_panel.save_plan_requested.connect(self._on_save_plan_requested)
        self._route_panel.load_plan_requested.connect(self._on_load_plan_requested)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._route_panel)
        splitter.addWidget(map_column)
        splitter.addWidget(right)
        # Single source of truth for pane proportions: stored as a tuple (route, map,
        # waypoints) and applied to both ``setStretchFactor`` (for resize behaviour)
        # and ``_apply_splitter_ratio`` (initial sizing). QSplitter has no public
        # getter for stretch factors, so we read them from this attribute instead.
        self._pane_stretch: tuple[int, ...] = (3, 7, 3)
        for i, f in enumerate(self._pane_stretch):
            splitter.setStretchFactor(i, f)
        self.setCentralWidget(splitter)
        self._splitter = splitter

        # Restore the previous session's window geometry + splitter sizes
        # *here*, after both the main window's frame and the splitter
        # exist but before the deferred default-ratio sizing fires. The
        # restore is opt-in (load returns ``None`` on first launch / a
        # corrupt entry), so the hard-coded ``self.resize(1600, 900)``
        # above and the ``_apply_splitter_ratio`` deferred call below
        # remain the safe defaults when nothing's persisted yet.
        #
        # ``_window_layout_restored`` gates the deferred default-ratio
        # call: if we restored splitter sizes here, ``_apply_splitter_ratio``
        # must NOT run and overwrite the user's saved pane proportions.
        # Off-screen safety lives in the existing ``_ensure_window_on_screen``
        # which runs on the first ``showEvent`` — it clamps a restored
        # geometry that fell off the work area (e.g. user unplugged the
        # monitor the window was last on) back into the primary screen's
        # available rect.
        self._window_layout_restored: bool = False
        saved_layout = load_window_layout()
        if saved_layout is not None:
            geom_bytes, splitter_bytes = saved_layout
            if self.restoreGeometry(geom_bytes):
                self._window_layout_restored = True
            if self._splitter.restoreState(splitter_bytes):
                self._window_layout_restored = True

        self._records_raw: list[WaypointRecord] | None = None
        self._waypoints_export: list[WaypointRecord] = []

        self._geo_north: SheetGeoCalibration | None = None
        self._geo_south: SheetGeoCalibration | None = None
        self._calibrate_state: dict[str, object] | None = None
        # After QMessageBox.instruction closes, ignore map clicks briefly (avoids OK click-through).
        self._calibrate_map_input_armed: bool = False
        # ``self._calibration_overlay`` is initialised at the very top of __init__ — it
        # must exist before any installEventFilter call (Qt may fire the filter during
        # widget construction).

        self._map_thread: QThread | None = None
        self._map_worker: MapLoadWorker | None = None
        self._wp_ocr_thread: QThread | None = None
        self._wp_ocr_worker: WaypointsOcrWorker | None = None
        self._waypoints_ocr_then_maps: bool = True
        self._progress: QProgressDialog | None = None

        # Altitude-arrow extraction state.
        #
        # ``_render_info_by_sheet`` is captured from the map worker after it
        # finishes (or recovers from the PNG cache) — the altitude worker
        # needs the exact ``CropMeta`` and DPI used during rendering to put
        # arrows in the same pixmap-UV space the calibration anchors live in.
        #
        # ``_altitude_arrows_*`` start empty; they're populated when the
        # altitude worker fires its ``finished`` signal. Until that happens
        # the route panel's altitude column shows "unknown" for every leg —
        # which is the right answer when the data genuinely isn't available
        # yet, and avoids a confusing flash of altitudes the moment maps
        # appear.
        self._render_info_by_sheet: dict[str, SheetRenderInfo] = {}
        self._altitude_arrows_north: list[AltitudeArrow] = []
        self._altitude_arrows_south: list[AltitudeArrow] = []
        self._alt_thread: QThread | None = None
        self._alt_worker: AltitudeArrowsWorker | None = None

        self._build_actions()

        # Ctrl+wheel font resizer — application-wide event filter
        # that translates Ctrl+scroll over a table, route-text
        # label, or hint label into a 1-px adjustment of the
        # corresponding Font Settings knob (same three categories
        # the dialog exposes). See
        # :mod:`cvfr_routemaster.font_wheel_resize` for the full
        # routing rules and the rationale for consuming every
        # Ctrl+wheel event regardless of whether a category matched.
        # Held as an attribute so ``QApplication.installEventFilter``
        # has a stable Python-side owner (the filter's lifetime
        # ends when ``self`` is destroyed); the object also doubles
        # as a test handle for asserting the filter is wired up.
        app_instance = QApplication.instance()
        if app_instance is not None:
            # Pass a bound predicate so the resizer can route to the
            # airplane-mode font profile whenever Airplane mode is
            # pressed in. The predicate is re-evaluated on every
            # wheel event, so the user can toggle Airplane mode
            # mid-session and the very next Ctrl+wheel adjusts the
            # right profile.
            self._ctrl_wheel_font_resizer = CtrlWheelFontResizer(
                project_root,
                lambda: self._act_airplane_mode.isChecked(),
                self,
            )
            app_instance.installEventFilter(self._ctrl_wheel_font_resizer)

        status = QStatusBar()
        self.setStatusBar(status)
        status.showMessage("Ready.")

        QTimer.singleShot(0, self._apply_splitter_ratio)
        QTimer.singleShot(150, self._maybe_autoload_on_start)
        # If the user previously saved the "Show VATSIM traffic"
        # toggle as ON, the toolbar action's ``setChecked`` ran
        # *before* the toggled-signal slot was connected, so the
        # worker never spun up automatically. Mirror that initial
        # state explicitly here on a deferred singleShot so the
        # widget tree is fully constructed first (status bar
        # exists, toolbar exists, etc.) before we kick off the
        # background poller. The worker itself is robust to the
        # chart not being loaded yet — it just collects empty
        # snapshots until the calibration callbacks make pilots
        # projectable.
        QTimer.singleShot(200, self._restore_vatsim_traffic_state_at_startup)
        # Warm the Hebrew text-shaping pipeline before the user's
        # first click. See :func:`_warm_text_rendering_caches` for
        # the full reasoning — short version: the very first time
        # Qt has to render Hebrew glyphs in a ``QLabel`` (which
        # happens when the user clicks the source airport and the
        # route panel sets the Hebrew route string for the first
        # time) Qt walks fontconfig to build a Hebrew-script font
        # fallback chain and primes HarfBuzz. On Debian and other
        # fresh Linux desktops with a cold fontconfig cache that
        # walk can cost the better part of a second, which the
        # user perceives as "the source airport takes ages to
        # appear, but every subsequent waypoint is instant". By
        # paying that cost during the idle moment right after the
        # window appears we shift the perceived stall from a
        # user-facing interaction to startup, where a brief delay
        # is already expected.
        QTimer.singleShot(0, _warm_text_rendering_caches)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._did_place_window:
            self._did_place_window = True
            QTimer.singleShot(0, self._ensure_window_on_screen)

    def _ensure_window_on_screen(self) -> None:
        """Keep the first frame inside the work area (fixes off-screen launch after monitor changes).

        Skipped while maximized/fullscreen — Qt itself migrates those windows
        to the primary screen when their saved screen vanishes, and ``move()``
        on a maximized window is a no-op that would silently desync the
        restored-normal-geometry Qt tracks underneath.
        """
        app = QApplication.instance()
        if app is None:
            return
        if self.isMaximized() or self.isFullScreen():
            return
        frame = self.frameGeometry()
        center = frame.center()
        screen = QGuiApplication.screenAt(center)
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        ag = screen.availableGeometry()
        if frame.width() > ag.width():
            self.resize(min(frame.width(), ag.width()), self.height())
            frame = self.frameGeometry()
        if frame.height() > ag.height():
            self.resize(frame.width(), min(frame.height(), ag.height()))
            frame = self.frameGeometry()
        x = max(ag.left(), min(frame.x(), ag.right() - frame.width() + 1))
        y = max(ag.top(), min(frame.y(), ag.bottom() - frame.height() + 1))
        self.move(x, y)

    def _maybe_autoload_on_start(self) -> None:
        """Kick off the load chain at startup if autoload is enabled.

        The gate is "are all three sources configured?" — NOT "are
        local PDFs already on disk?" The latter would silently skip
        autoload on a fresh install where the shipped
        ``chart_sources.json`` populates the source fields with the
        default CAAI URLs but no PDF has been downloaded yet, leaving
        the user staring at an empty viewport with no signal that
        action is required. By gating on sources instead, the URL
        download flow inside ``_ensure_chart_sources_resolved`` (which
        ``_load_all`` calls as its first step) shows the progress
        dialog and surfaces failures via the error modal — both of
        which the user expects.

        Partially-configured sources (1 or 2 of 3) also no-op here:
        ``_load_all`` would just pop the "Set all three map sources…"
        nag dialog, which is annoying on every launch. The user has
        explicitly cleared one or more fields; let them open Settings
        themselves.
        """
        if not load_autoload_enabled():
            return
        if not self._sources_set():
            return
        self._load_all()

    def _sources_set(self) -> bool:
        """True iff all three chart sources (north, south, back) are
        configured.

        Each entry may be a local file path or an HTTP(S) URL — we
        don't validate the value here; the actual download / file
        open happens later in ``_load_all`` →
        ``_ensure_chart_sources_resolved``.
        """
        return all(
            bool(s)
            for s in (
                self._source_north,
                self._source_south,
                self._source_back,
            )
        )

    def _apply_splitter_ratio(self) -> None:
        """Initial pane sizes proportional to ``self._pane_stretch``.

        Defers via QTimer until the splitter has positive width — on first show the
        splitter has no width yet and ``setSizes`` would silently no-op.

        Reading the proportions from ``_pane_stretch`` (the same tuple used to call
        ``setStretchFactor``) keeps the initial sizing in sync if a future change
        adds, removes, or re-weights a pane — which is exactly the kind of drift
        that previously made the waypoint pane collapse after the route panel was
        inserted (the old version handed only two widths to a three-pane splitter,
        leaving the trailing pane at zero). ``QSplitter`` has no public getter for
        stretch factors, so we deliberately keep our own copy.

        Skips entirely when a previous session's pane sizes were restored from
        QSettings — the user's saved proportions take precedence over the
        hard-coded defaults. Without this gate the deferred timer would race the
        restore and silently flatten panes back to the (3, 7, 3) ratio on every
        startup.
        """
        if getattr(self, "_window_layout_restored", False):
            return
        w = self._splitter.width()
        if w <= 0:
            QTimer.singleShot(0, self._apply_splitter_ratio)
            return
        n = self._splitter.count()
        if n <= 0:
            return
        factors = list(self._pane_stretch[:n])
        if len(factors) < n:
            factors.extend([1] * (n - len(factors)))
        total = sum(factors) or 1
        self._splitter.setSizes([int(w * f / total) for f in factors])

    def _build_actions(self) -> None:
        """Top toolbar — four titled, rounded-border groups of
        related buttons.

        Layout (left-to-right):

        1. **Program Settings** group:

           * **Map File Settings** — the chart-PDF paths and
             autoload toggle.
           * **Map Calibration Options** — re-OCR, fit/reset
             layout, calibrate north/south, clear calibration.
           * **Display Settings** — font sizes and traffic-icon
             size (the dialog covers more than just fonts now).

        2. **View Toggles** group:

           * **Airplane mode** — collapse the UI to "route only"
             for in-flight reading on a small secondary monitor.
           * **Hide Waypoint View** — hide just the right-hand
             waypoint pane; chart + route panel stay visible.
           * **Hide Usage Hints** — hide every usage-hint footer
             (route panel, chart, waypoint pane) to free
             vertical space once the gestures are memorised.
           * **Show VATSIM traffic** — render the live traffic
             overlay on the chart.

        3. **Satellite View Options** group:

           * **Satellite view** — swap the chart background for
             Esri World Imagery. The bulk download that feeds
             this view runs automatically in the background; an
             informational notice is shown once per install
             before the first download begins, and subsequent
             launches silently resume any partial download.
             See :meth:`_show_first_download_notice_and_start`
             and :meth:`_check_satellite_resume_on_startup`.

        4. **Program Information** group:

           * **Legal and Copyright Info…** — open a popup with
             the AGPLv3 boilerplate, author / contact, the
             accompanying source-bundle path (AGPLv3 §6(a)),
             Israeli CVFR chart-data attribution (CAAI / State
             of Israel), third-party software licenses, and the
             flight-simulator-only intended-use disclaimer
             (framed as a warranty disclaimer per AGPLv3 §7(a)).

        The hidden ``Cancel calibration`` button is appended
        OUTSIDE the four groups, to the right of "Program
        Information". It only appears while a calibration is
        in progress, so placing it inside any group would
        visually disrupt the group layout for ~2 seconds of the
        user's session. As an out-of-group floating button it
        slides in and out without rearranging the rest of the
        toolbar.

        The user-requested removal: the previous "Export
        waypoints to CSV…" toolbar entry is gone. The
        ``_export_waypoints_csv`` method itself remains in
        place (orphaned but harmless) so its docstring
        references in nearby methods stay accurate; a future
        cleanup can delete it once nothing else references it.

        Implementation note: each group is a
        :class:`QFrame` (no built-in frame shape; the rounded
        border is drawn by an object-name-scoped QSS rule so
        only the three groups are styled — other QFrames in
        the app are untouched). Buttons are :class:`QToolButton`
        instances that proxy each :class:`QAction` via
        ``setDefaultAction(...)``, so the existing tooltips,
        checkable-state, slot wiring, icons, and object names
        (``act_open_map_file_settings``, etc.) all carry over
        unchanged. The action object names are what the test
        suite uses to locate buttons, so the new layout doesn't
        break any of those selectors.
        """
        tb = QToolBar()
        tb.setMovable(False)
        # Object name lets the test suite (and any future QSS selector) target
        # this specific toolbar without ``findChildren(QToolBar)`` heuristics.
        tb.setObjectName("mainActionsToolBar")
        self.addToolBar(tb)

        # ------------------------------------------------------------
        # Create every QAction up front. Wiring (tooltips, slot
        # connections, checkable state, icons, object names) is
        # IDENTICAL to the previous flat-toolbar implementation so
        # the only visible delta from the user's perspective is the
        # group containers around the same buttons — every existing
        # test selector that locates an action by object name
        # continues to work.
        # ------------------------------------------------------------

        act_settings = QAction("Map File Settings…", self)
        act_settings.setObjectName("act_open_map_file_settings")
        act_settings.setToolTip(
            "Configure the chart PDF paths, autoload-on-startup behaviour, and "
            "load the maps and waypoints."
        )
        act_settings.triggered.connect(self._open_settings)

        act_cal_options = QAction("Map Calibration Options…", self)
        act_cal_options.setObjectName("act_open_calibration_options")
        act_cal_options.setToolTip(
            "Re-OCR waypoints, fit / reset the chart layout, calibrate north or "
            "south, or clear the saved calibration. Includes on-screen "
            "instructions for what calibration does and how to perform it."
        )
        act_cal_options.triggered.connect(self._open_calibration_options)

        # Object name remains ``act_open_font_settings`` for backward
        # compatibility with anything that targeted the action by
        # name; the user-visible label is "Display Settings…"
        # because the dialog covers more than fonts (traffic-icon
        # size lives there too — see :class:`FontSettingsDialog`).
        act_font_settings = QAction("Display Settings…", self)
        act_font_settings.setObjectName("act_open_font_settings")
        act_font_settings.setToolTip(
            "Adjust on-screen display sizes: font sizes for the "
            "waypoint/route tables, the route-text labels above "
            "the route table, the three usage-hint panes, and the "
            "size of VATSIM traffic plane icons on the chart."
        )
        act_font_settings.triggered.connect(self._open_font_settings)

        # Airplane mode — checkable, off-by-default, with the
        # classic phone "airplane mode" silhouette as its icon so
        # it reads at a glance. Toggling it collapses the UI to
        # "route pane only" for in-flight reading; toggling again
        # restores the full chart + waypoint layout.
        from cvfr_routemaster.app_icon import airplane_mode_icon

        self._act_airplane_mode = QAction("Airplane mode", self)
        self._act_airplane_mode.setObjectName("act_toggle_airplane_mode")
        self._act_airplane_mode.setCheckable(True)
        self._act_airplane_mode.setChecked(False)
        self._act_airplane_mode.setIcon(airplane_mode_icon())
        self._act_airplane_mode.setToolTip(
            "Hide the chart and waypoint pane, expand the route pane to the "
            "full window. The route table and the ICAO / Hebrew / totals "
            "strings above it stretch across the screen for easy reading "
            "in-flight. Toggle off to bring the chart and waypoint table back."
        )
        self._act_airplane_mode.toggled.connect(self._on_airplane_mode_toggled)

        # Hide Waypoint View — checkable, off-by-default. Strict
        # subset of airplane mode: hides only the right-hand
        # waypoint pane.
        self._act_hide_waypoint_view = QAction("Hide Waypoint View", self)
        self._act_hide_waypoint_view.setObjectName("act_toggle_hide_waypoint_view")
        self._act_hide_waypoint_view.setCheckable(True)
        self._act_hide_waypoint_view.setChecked(False)
        self._act_hide_waypoint_view.setToolTip(
            "Hide just the waypoint pane (search bar, provider combo, "
            "lat/lon toggle, master table, and pane hint). The map and "
            "route panel stay visible — use this when you want the chart "
            "and route side-by-side without the master waypoint list "
            "taking up space. Toggle off to bring the pane back."
        )
        self._act_hide_waypoint_view.toggled.connect(
            self._on_hide_waypoint_view_toggled
        )

        # Hide Usage Hints — checkable, off-by-default. Hides
        # every usage-hint footer (route, chart, waypoint).
        self._act_hide_usage_hints = QAction("Hide Usage Hints", self)
        self._act_hide_usage_hints.setObjectName("act_toggle_hide_usage_hints")
        self._act_hide_usage_hints.setCheckable(True)
        self._act_hide_usage_hints.setChecked(False)
        self._act_hide_usage_hints.setToolTip(
            "Hide every usage-hint footer (route panel, chart, and "
            "waypoint pane) to free vertical space. Toggle off to "
            "bring the hints back."
        )
        self._act_hide_usage_hints.toggled.connect(
            self._on_hide_usage_hints_toggled
        )

        # Show VATSIM traffic — live traffic overlay toggle.
        self._act_show_vatsim_traffic = QAction("Show VATSIM traffic", self)
        self._act_show_vatsim_traffic.setObjectName(
            "act_toggle_show_vatsim_traffic"
        )
        self._act_show_vatsim_traffic.setCheckable(True)
        self._act_show_vatsim_traffic.setToolTip(
            "Show or hide live VATSIM traffic on the chart. Plane "
            "icons are colour-coded by wake-turbulence category "
            "(L/M/H/Super) with a callsign label and a tooltip "
            "showing aircraft type, altitude, groundspeed, heading, "
            "and route. Icon size is set in Display Settings. Click "
            "a plane to track it (the viewport will follow it on "
            "every VATSIM update); click empty chart to release."
        )
        # ``setChecked`` may fire ``toggled``; the slot is robust to
        # being called before the chart is loaded (set_pilots silently
        # skips unprojectable pilots while no sheet is calibrated).
        self._act_show_vatsim_traffic.setChecked(load_show_vatsim_traffic())
        self._act_show_vatsim_traffic.toggled.connect(
            self._on_show_vatsim_traffic_toggled
        )

        # Satellite view — Esri World Imagery toggle.
        self._act_show_satellite = QAction("Satellite view", self)
        self._act_show_satellite.setObjectName("act_toggle_show_satellite")
        self._act_show_satellite.setCheckable(True)
        self._act_show_satellite.setToolTip(
            "Switch the chart background to Esri World Imagery "
            "satellite tiles. Routes, traffic, and altitudes stay "
            "pixel-correct because the satellite mosaic is warped "
            "into the chart's projection. First use requires "
            "downloading ~330 MB of imagery (one-time)."
        )
        self._act_show_satellite.setChecked(load_show_satellite())
        self._act_show_satellite.toggled.connect(
            self._on_show_satellite_toggled
        )

        # NOTE: a "Download Satellite Imagery…" toolbar action used
        # to live here. It was removed in v3.3+ when the satellite-
        # download flow stopped being a user-driven decision and
        # became unconditional (one-time informational notice on
        # first launch, silent resume on subsequent launches). With
        # the download now automatic, a button to "trigger the
        # download" had no remaining role — see
        # :meth:`_show_first_download_notice_and_start` and
        # :meth:`_check_satellite_resume_on_startup`.

        # Program Information group — single button surfaces the
        # Copyright Information dialog (AGPLv3 boilerplate, source
        # offer, chart-data attribution, third-party licenses,
        # intended-use disclaimer). Kept in its own toolbar group
        # because a recipient inspecting an unfamiliar binary should
        # find legal info in one obvious place (its own labelled
        # group), not tucked at the end of "Satellite View Options".
        self._act_copyright_info = QAction("Legal and Copyright Info…", self)
        self._act_copyright_info.setObjectName("act_show_copyright_info")
        self._act_copyright_info.setToolTip(
            "Show copyright, license (AGPLv3), source-code request, "
            "chart-data attribution, third-party software licenses, "
            "and the intended-use disclaimer."
        )
        self._act_copyright_info.triggered.connect(
            self._on_copyright_info_triggered
        )

        # Hidden by default — appears only while a calibration run is active
        # so an unused button doesn't sit greyed out in the chrome. Esc is
        # the keyboard equivalent (handled in ``keyPressEvent``).
        self._act_cancel_cal = QAction("Cancel calibration", self)
        self._act_cancel_cal.setObjectName("act_cancel_calibration")
        self._act_cancel_cal.setVisible(False)
        self._act_cancel_cal.setToolTip(
            "Abort the in-progress calibration (Esc also works)."
        )
        self._act_cancel_cal.triggered.connect(self._cancel_calibration)

        # ------------------------------------------------------------
        # Build the three titled group frames and add them to the
        # toolbar.
        # ------------------------------------------------------------

        program_group = self._make_toolbar_group(
            "Program Settings",
            "group_program_settings",
            [act_settings, act_cal_options, act_font_settings],
        )
        view_toggles_group = self._make_toolbar_group(
            "View Toggles",
            "group_view_toggles",
            [
                self._act_airplane_mode,
                self._act_hide_waypoint_view,
                self._act_hide_usage_hints,
                self._act_show_vatsim_traffic,
            ],
        )
        satellite_group = self._make_toolbar_group(
            "Satellite View Options",
            "group_satellite_view_options",
            [self._act_show_satellite],
        )
        program_info_group = self._make_toolbar_group(
            "Program Information",
            "group_program_information",
            [self._act_copyright_info],
        )

        # Object-name-scoped stylesheet so we don't accidentally
        # style every QFrame in the app. The titles use a slightly
        # bolder weight so they read as captions rather than
        # ordinary text. ``palette(mid)`` resolves to the current
        # theme's mid-grey, so the border looks right in both light
        # and dark themes without hard-coding a colour.
        #
        # Checked-state visual: every Qt toolbar style we tested
        # collapses the native "pressed" affordance to near-zero
        # contrast once any QSS touches the toolbar's button
        # children (the QSS overrides Qt's built-in checked-state
        # paint without re-providing it). We bring it back with
        # an explicit ``QToolButton:checked`` rule scoped to the
        # three group frames so checkable buttons (Airplane mode,
        # Hide Waypoint View, Hide Usage Hints, Show VATSIM
        # traffic, Satellite view) light up green when active.
        #
        # The colour is the Garmin glass-cockpit "active mode"
        # green — instantly readable to a pilot as "this mode is
        # ON". Chosen specifically because:
        #
        # 1. Aviation UI convention. G1000/G3000/G5000 PFD/MFD
        #    mode annunciators light up in this shade when a mode
        #    is engaged, so the visual vocabulary maps directly.
        # 2. Distinct from every other colour in our palette: the
        #    wake-Medium icon green (#3DDC84) is brighter and only
        #    appears on the chart; the tracking halo amber
        #    (#FFD400) is a different hue; calibration red is
        #    different hue; the group border palette-mid is grey.
        # 3. AA-contrast against white text in the dark theme.
        #
        # Hover/pressed gradients are slightly brighter / darker
        # so the button still feels responsive when the user
        # mouses over an already-checked button (Qt's default
        # hover visual is suppressed by our background-color rule).
        tb.setStyleSheet(
            "QFrame#group_program_settings,"
            " QFrame#group_view_toggles,"
            " QFrame#group_satellite_view_options,"
            " QFrame#group_program_information {"
            "    border: 1px solid palette(mid);"
            "    border-radius: 8px;"
            "    margin: 2px 4px;"
            "    padding: 2px 4px;"
            "}"
            "QLabel#group_program_settings_title,"
            " QLabel#group_view_toggles_title,"
            " QLabel#group_satellite_view_options_title,"
            " QLabel#group_program_information_title {"
            "    font-weight: bold;"
            "    padding-bottom: 2px;"
            "}"
            "QFrame#group_program_settings QToolButton:checked,"
            " QFrame#group_view_toggles QToolButton:checked,"
            " QFrame#group_satellite_view_options QToolButton:checked,"
            " QFrame#group_program_information QToolButton:checked {"
            "    background-color: #1e7a3e;"
            "    color: white;"
            "    border: 1px solid #155a2c;"
            "    border-radius: 4px;"
            "    padding: 2px 6px;"
            "}"
            "QFrame#group_program_settings QToolButton:checked:hover,"
            " QFrame#group_view_toggles QToolButton:checked:hover,"
            " QFrame#group_satellite_view_options QToolButton:checked:hover,"
            " QFrame#group_program_information QToolButton:checked:hover {"
            "    background-color: #26954c;"
            "}"
            "QFrame#group_program_settings QToolButton:checked:pressed,"
            " QFrame#group_view_toggles QToolButton:checked:pressed,"
            " QFrame#group_satellite_view_options QToolButton:checked:pressed,"
            " QFrame#group_program_information QToolButton:checked:pressed {"
            "    background-color: #155a2c;"
            "}"
        )

        tb.addWidget(program_group)
        tb.addWidget(view_toggles_group)
        tb.addWidget(satellite_group)
        tb.addWidget(program_info_group)

        # Cancel-calibration sits OUTSIDE the groups so its
        # transient show/hide doesn't visually rearrange the
        # group containers around it.
        #
        # Subtlety: we wire it via ``tb.addAction(...)`` rather
        # than ``tb.addWidget(QToolButton.setDefaultAction(...))``.
        # The two look identical on the surface (both end up with
        # a QToolButton in the toolbar's child tree), but the
        # ``setDefaultAction`` path only syncs text / icon /
        # tooltip from the action — NOT the action's ``visible``
        # property. ``tb.addAction`` uses Qt's native
        # action-widget machinery, which DOES propagate visibility,
        # so toggling ``_act_cancel_cal.setVisible(True)`` at
        # calibration start makes the button appear (and
        # ``setVisible(False)`` at calibration end makes it
        # disappear) without any explicit visibility-sync code.
        # The first version of the restructure used
        # ``setDefaultAction`` and left the button visible
        # full-time, which is the bug this comment exists to
        # prevent a future maintainer from re-introducing.
        tb.addAction(self._act_cancel_cal)

        # Final hint refresh once every toolbar action has been
        # created. The mid-method refresh after the VATSIM toggle
        # only sees the VATSIM checked-state (satellite toggle
        # doesn't exist yet at that point), so for sessions where
        # the satellite toggle was on at last shutdown the hint
        # would otherwise lack the loading-logic explainer line
        # until the chart-loaded path runs. Cheap to redo here.
        self._update_map_hint_text()

    def _make_toolbar_group(
        self,
        title: str,
        object_name: str,
        actions: list[QAction],
    ) -> QFrame:
        """Build one of the three top-toolbar groups.

        Layout per group:

        * A bold-weight :class:`QLabel` title centred above
        * A horizontal row of :class:`QToolButton` instances, one
          per supplied :class:`QAction`, each wired via
          ``setDefaultAction(...)``

        Visual styling (rounded border, padding) is applied at
        the toolbar level via an object-name-scoped stylesheet —
        see :meth:`_build_actions`. This method only assigns the
        object names that stylesheet targets.

        ``setDefaultAction(...)`` is the correct way to wire a
        QToolButton to a QAction: the button picks up the
        action's text, icon, tooltip, and checked-state, and
        ``clicked`` is routed through the action's ``triggered``
        signal automatically. This means every existing slot
        connection on the action keeps working — we don't have
        to re-connect anything per-button.

        Airplane mode is the one action that wants its icon
        rendered next to the text (the cellphone "airplane
        mode" silhouette is the visual affordance the user
        asked for). Every other button on this toolbar is
        text-only so a per-action style override applies the
        ``ToolButtonTextBesideIcon`` mode only to that single
        button rather than toolbar-wide.
        """
        frame = QFrame()
        frame.setObjectName(object_name)
        # Default Box+Sunken shape would compete with our QSS
        # rounded border; flatten the QFrame so only the
        # stylesheet draws.
        frame.setFrameShape(QFrame.Shape.NoFrame)

        vlay = QVBoxLayout(frame)
        vlay.setContentsMargins(4, 2, 4, 2)
        vlay.setSpacing(2)

        title_label = QLabel(title, frame)
        title_label.setObjectName(f"{object_name}_title")
        title_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        vlay.addWidget(title_label)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        for action in actions:
            btn = QToolButton(frame)
            btn.setDefaultAction(action)
            # Per-action button style: airplane mode shows its
            # icon next to the text; everything else stays
            # text-only.
            if action is self._act_airplane_mode:
                btn.setToolButtonStyle(
                    Qt.ToolButtonStyle.ToolButtonTextBesideIcon
                )
            else:
                btn.setToolButtonStyle(
                    Qt.ToolButtonStyle.ToolButtonTextOnly
                )
            row.addWidget(btn)
        vlay.addLayout(row)
        return frame

    def _on_copyright_info_triggered(self) -> None:
        """Show the modal Copyright Information dialog.

        Lazy-import :class:`ProgramInfoDialog` to keep
        ``main_window`` import-time cost flat — the dialog pulls in
        a small but non-zero chunk of HTML/text constants that the
        90% of sessions never opening it have no reason to load
        eagerly.

        We construct a fresh dialog on every click rather than
        caching one, because the dialog reads the version string at
        construction time via ``build_copyright_info_html`` — if a
        dev attaches a debugger and bumps ``__version__`` between
        clicks (or, in a test, monkeypatches it), every new click
        sees the new version without us having to invalidate a
        cached widget.
        """
        from cvfr_routemaster.program_info_dialog import ProgramInfoDialog

        dlg = ProgramInfoDialog(self)
        dlg.exec()

    def _open_font_settings(self) -> None:
        """Show the Display Settings dialog, persist the user's
        choices on Accept (both font profiles plus the traffic-icon
        size, in one transaction), and re-apply the *currently
        active* font profile so the new sizes take effect
        immediately.

        The method is still named ``_open_font_settings`` to keep
        the diff small; the dialog now covers fonts *and* the
        traffic-icon size (introduced in v2 — see
        ``ROADMAP-NEXT.md``). Renaming the method would touch
        every call site for no behaviour change, so we let the
        method name lag the user-visible label.

        Re-applying via :func:`apply_dark_theme` (rather than
        manually walking widgets and calling ``setFont``) keeps the
        QSS as the single source of truth: every widget Qt
        re-polishes picks up the new ``font-size`` rule on the next
        paint pass, which is exactly the "user changed font size,
        all the affected surfaces re-render together" experience
        the user expects. The traffic-icon size doesn't flow
        through QSS — when the actual traffic overlay lands the
        save here will trigger an overlay rebuild via signal; for
        now it's persisted but visually inert because there are no
        plane icons drawn yet.

        On Cancel the function is a no-op — nothing is saved,
        nothing is re-applied — so the dialog is a safe "try it
        and see" surface even though we don't show a live preview.
        """
        # Pass ``project_root`` so re-opening the dialog on a release
        # machine with no QSettings yet still shows the shipped
        # defaults (from ``font_settings.json``) rather than the
        # hard-coded baseline — matches what the user sees on screen.
        # Three values loaded together (two font profiles + one
        # traffic-icon size); the dialog renders them as a single
        # form so the user can adjust them in one pass.
        current_normal = load_font_sizes(self._project_root)
        current_airplane = load_airplane_font_sizes(self._project_root)
        current_traffic_icon_size_px = load_traffic_icon_size_px(self._project_root)
        current_waypoint_marker_size_px = load_waypoint_marker_size_px(
            self._project_root
        )
        dlg = FontSettingsDialog(
            current_normal,
            current_airplane,
            current_traffic_icon_size_px,
            current_waypoint_marker_size_px,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        save_font_sizes(dlg.chosen_sizes())
        save_airplane_font_sizes(dlg.chosen_airplane_sizes())
        save_traffic_icon_size_px(dlg.chosen_traffic_icon_size_px())
        save_waypoint_marker_size_px(dlg.chosen_waypoint_marker_size_px())
        self._reapply_active_font_theme()
        # Icon size may have changed — rebuild the traffic overlay so the
        # new size takes effect immediately. No-op when the toolbar toggle
        # is off.
        self._refresh_traffic_overlay()
        # Marker size may have changed — rebuild the waypoint
        # marker overlays so the new size takes effect immediately.
        # No-op when there are no overlays in place yet (chart
        # hasn't loaded).
        self._rebuild_waypoint_marker_overlays()

    def _reapply_active_font_theme(self) -> None:
        """Re-apply the dark theme using whichever font profile
        matches the current airplane-mode state.

        The single source of truth for "what profile is active" is
        ``self._act_airplane_mode.isChecked()`` (set up in
        :meth:`_build_actions`). Three call sites converge here:

          1. Font Settings dialog accept — both profiles were just
             persisted and we need to re-render with the new active
             values.
          2. Airplane-mode toolbar toggle — switching modes flips
             the profile pointer, so re-render with the new
             profile's sizes.
          3. Ctrl+wheel font resizer (indirectly, via
             :func:`apply_dark_theme` it calls directly) — this
             method isn't on the wheel path itself but the same
             "match active profile" rule lives in the resizer too.

        ``QApplication.instance()`` is statically typed as
        ``QCoreApplication``; ``apply_dark_theme`` calls
        ``QApplication``-only methods so we narrow the type
        before passing it through.
        """
        app = QApplication.instance()
        if not isinstance(app, QApplication):
            return
        sizes = self._active_font_sizes()
        apply_dark_theme(app, sizes)

    def _active_font_sizes(self) -> FontSizes:
        """Return the :class:`FontSizes` profile that should be
        applied right now based on the airplane-mode toolbar toggle.

        ``hasattr`` defensive check: this method may be called
        during the brief window before ``_build_actions`` creates
        ``_act_airplane_mode`` (e.g. by a unit test that
        constructs a partially-built MainWindow). The fallback is
        the normal-mode profile, which matches the toolbar's
        default off-state.
        """
        if (
            hasattr(self, "_act_airplane_mode")
            and self._act_airplane_mode.isChecked()
        ):
            return load_airplane_font_sizes(self._project_root)
        return load_font_sizes(self._project_root)

    def _on_airplane_mode_toggled(self, on: bool) -> None:
        """Enter / leave airplane mode in response to the toolbar toggle.

        Airplane mode is a viewing-only state — the underlying route,
        waypoint database, calibration, and map state are all untouched.
        We just hide the chart column and the waypoint pane so the
        route panel takes the full window width, and tell the route
        panel to hide its own clear-route button and footer hint.

        Splitter behaviour: hiding a child widget of a ``QSplitter``
        collapses its slot; the remaining visible children share the
        space according to the existing stretch factors (in our case
        the route panel is the only visible child, so it claims 100%).
        Showing the child restores it at its previous size — Qt
        tracks per-child sizes internally for exactly this case, so
        no manual size save/restore is needed on the controller side.

        Args:
            on: New checked state from ``QAction.toggled``. ``True``
                means the user just pressed the button in;
                ``False`` means they just toggled it back out.
        """
        self._map_column.setVisible(not on)
        self._waypoint_pane.setVisible(not on)
        self._route_panel.set_airplane_mode(on)
        # Re-apply the other two view-mode toggles when leaving
        # Airplane mode so their state takes effect again:
        #   * Hide Waypoint View — Airplane mode hides the pane
        #     unconditionally, so on the way OUT we must re-hide
        #     it ourselves if the user had pressed Hide Waypoint
        #     View before / during Airplane mode.
        #   * Hide Usage Hints — Airplane mode also overrides the
        #     route-pane hint visibility (it forces it off), so on
        #     the way OUT ``set_airplane_mode(False)`` re-shows it
        #     and we need to re-hide it if Hide Usage Hints is on.
        # On the way IN, Airplane mode trumps both toggles for the
        # widgets it controls, so these re-applies are no-ops.
        if not on and self._act_hide_waypoint_view.isChecked():
            self._waypoint_pane.setVisible(False)
        if not on and self._act_hide_usage_hints.isChecked():
            self._route_panel._hint_label.setVisible(False)
        # Swap font profiles: airplane mode has its own independent
        # ``FontSizes`` set (typically larger for in-flight reading
        # at right-seat / secondary-monitor distance). Re-applying
        # the dark theme with the new profile's sizes rewrites the
        # QSS in one shot so every table, route-text label, and
        # hint label picks up the new ``font-size`` rule on the
        # next paint pass.
        self._reapply_active_font_theme()

    def _on_hide_waypoint_view_toggled(self, on: bool) -> None:
        """Show/hide just the waypoint pane (right-hand splitter slot).

        Independent of Airplane mode: when Airplane mode is on the
        pane is already hidden, so we leave the visibility alone;
        when Airplane mode is off, this toggle is the only thing
        gating ``self._waypoint_pane`` visibility. The
        ``_on_airplane_mode_toggled`` method reads this action's
        check state when leaving Airplane mode to decide whether to
        keep the pane hidden, so the two toggles compose without
        either having to disable the other.
        """
        # Airplane mode collapses both the map column and the
        # waypoint pane; while it's active this toggle has no
        # additional effect (and we mustn't fight Airplane mode by
        # re-showing the pane while it's pressed in).
        if self._act_airplane_mode.isChecked():
            return
        self._waypoint_pane.setVisible(not on)

    def _on_hide_usage_hints_toggled(self, on: bool) -> None:
        """Show/hide every ``QLabel#mapHint`` footer at once.

        The three labels are:

        * ``self._route_panel._hint_label`` — route-pane footer with
          the Shift+click instructions and override-cell tutorial.
        * ``self._map_hint`` — map-pane footer with the drag-pans
          / wheel-zooms primer.
        * ``self._waypoint_table_hint`` — waypoint-pane footer that
          describes the green / blue click affordances.

        Airplane mode hides the route-pane hint independently; when
        it's pressed in we leave that one alone (the airplane-mode
        machinery is the single source of truth for its own visual
        rule) and toggle only the other two. When Airplane mode is
        released, the route-pane hint reverts to whatever Airplane
        mode dictates, so we re-apply this toggle from
        :meth:`_on_airplane_mode_toggled` after it restores the
        normal layout.
        """
        # Inverse semantics: ``on`` (i.e. "hide hints button is
        # pressed in") means hints should NOT be visible.
        visible = not on
        self._map_hint.setVisible(visible)
        self._waypoint_table_hint.setVisible(visible)
        # The route-pane hint is owned by RoutePanel and also tied
        # to Airplane mode; while that mode is active it forces the
        # hint hidden and we shouldn't fight it.
        if not self._route_panel.is_airplane_mode():
            self._route_panel._hint_label.setVisible(visible)

    def _update_map_hint_text(self) -> None:
        """Refresh the map-pane usage-hint label text.

        Multi-line composition based on which toolbar toggles
        the user has enabled:

        * Always: nav primitives line
          (``"Map: drag pans · wheel zooms."``).
        * **VATSIM traffic on** — legends the wake-category
          colours used by the on-chart traffic icons, with a
          coloured square marker beside each label so the
          legend reads visually rather than requiring the user
          to parse text descriptions.
        * **Satellite view on** — explains the
          download-and-render logic so the user knows what to
          expect when imagery is still streaming in (coarse
          layers fill in under finer layers as the chain
          finishes them).

        The hint's *visibility* is governed independently by
        the "Hide Usage Hints" toggle, which hides the entire
        ``_map_hint`` label. We don't have to gate on it here —
        if hints are hidden the label is invisible regardless of
        what we set as its text. Setting the text always keeps
        the two states consistent: when the user un-hides the
        hints, the legend immediately reflects the current
        toggle states.

        Inline RGB literals match the QColor entries in
        :data:`cvfr_routemaster.traffic_overlay.WAKE_COLOR` —
        they're cheap to mirror by hand here (five colours,
        unlikely to change) and avoid making the QSS or the
        traffic-overlay module reach into one another.
        """
        lines: list[str] = ["Map: drag pans · wheel zooms."]
        # ``_update_map_hint_text`` runs once during ``_build_actions``
        # *between* creating the VATSIM toggle and the satellite
        # toggle (so the initial hint reflects the saved VATSIM
        # state from the very first paint). At that moment
        # ``_act_show_satellite`` doesn't exist yet — guard with
        # ``getattr`` so we don't blow up; the post-creation
        # re-render in ``_build_actions`` and the satellite toggle
        # handler both call us again with the action attached.
        sat_action = getattr(self, "_act_show_satellite", None)
        if sat_action is not None and sat_action.isChecked():
            # Compact two-clause explainer. Phrased in terms
            # the user will see in the status bar
            # (``z=12 / z=13 / z=14``, "downloading", etc.) so
            # they can correlate the hint with the progress
            # readout. Kept on a single line for terseness;
            # the bullet glyph (·) is the same separator we
            # use for the traffic legend below.
            lines.append(
                "Satellite: app downloads coarse → fine "
                "(z=12 → z=13 → z=14); coarser zooms render "
                "under finer ones so gaps in z=14 are filled "
                "by z=13 / z=12 until the chain catches up."
            )
        if self._act_show_vatsim_traffic.isChecked():
            # Wake-category legend. Order = visual ordering on a
            # typical apron from smallest to largest aircraft, plus
            # the no-flight-plan fallback at the end (because it's
            # the "we don't know" bucket and reads naturally last).
            legend_entries: list[tuple[str, str]] = [
                ("L", "Light"),
                ("M", "Medium"),
                ("H", "Heavy"),
                ("J", "Super"),
                (WAKE_UNKNOWN, "No flight plan"),
            ]
            # ``QColor.name()`` produces a ``#rrggbb`` string
            # suitable for inline CSS; alpha is dropped on
            # purpose because the legend's solid square doesn't
            # need the silhouette's 235/255 translucency.
            chips = []
            for wake, label in legend_entries:
                color_hex = WAKE_COLOR[wake].name()
                # ``■`` (BLACK SQUARE) is widely supported in
                # the default QFontDatabase fallback chain on
                # Windows / Linux / macOS, doesn't depend on
                # emoji fonts, and renders at the same height
                # as surrounding text.
                chips.append(
                    f'<span style="color:{color_hex}">■</span> {label}'
                )
            lines.append("Traffic icons: " + " · ".join(chips))
        self._map_hint.setText("<br>".join(lines))

    def _on_show_vatsim_traffic_toggled(self, on: bool) -> None:
        """Handle the "Show VATSIM traffic" toolbar toggle.

        Three responsibilities:

        1. Persist the new state to QSettings so the choice survives
           app restarts.
        2. Spin up or tear down the live VATSIM poller — when the
           toggle goes on we start a fresh worker on a dedicated
           thread; when it goes off we ask the worker to stop and
           wait for the thread to exit cleanly.
        3. Refresh the on-chart overlay (populate from the latest
           snapshot if we already have one, or clear when toggling
           off) and update the map-pane usage hint to add or
           remove the colour legend.
        """
        save_show_vatsim_traffic(on)
        if on:
            self._start_vatsim_worker()
            # If a previous session left a snapshot in memory we
            # could repaint it here, but in practice a fresh worker
            # delivers a snapshot within ~1-2 s of starting, so we
            # let that drive the first paint.
            self._refresh_traffic_overlay()
        else:
            self._stop_vatsim_worker()
            self._latest_vatsim_pilots = None
            self._traffic_overlay.clear()
        self._update_map_hint_text()

    def _refresh_traffic_overlay(self) -> None:
        """Rebuild the traffic overlay from the current pilot source.

        No-op when the toolbar toggle is off — keeps the call sites
        naive: anything that *could* affect projected positions
        (calibration completion, sheet move/scale, icon-size change
        via Display Settings) calls this method, and it cheaply
        does nothing when the overlay is hidden.

        Pilot source is the latest VATSIM worker snapshot
        (:attr:`_latest_vatsim_pilots`). Until the worker delivers
        its first fetch the snapshot is ``None`` and we draw
        nothing; once it lands the snapshot is a (possibly empty)
        list of :class:`Pilot` records that we hand to the
        overlay's idempotent ``set_pilots`` rebuild.

        The yellow selection halo (set by
        :meth:`set_tracked_callsign`) survives ``set_pilots``
        automatically — the overlay re-applies its tracking state
        to the freshly-rebuilt items at the end of every call.
        """
        if not self._act_show_vatsim_traffic.isChecked():
            return
        if self._latest_vatsim_pilots is None:
            return
        icon_size = load_traffic_icon_size_px(self._project_root)
        self._traffic_overlay.set_pilots(
            self._latest_vatsim_pilots,
            icon_size_px=icon_size,
        )

    # --- Plane tracking (click-to-follow) ---------------------------

    def set_tracked_callsign(self, callsign: str | None) -> None:
        """Mark a VATSIM pilot as the user's tracking target, or
        clear it.

        Called from :class:`MapGraphicsView` when the user
        plain-clicks on a plane (start tracking that callsign) or
        on empty chart while tracking (stop). Idempotent —
        re-asserting the same target or clearing an already-empty
        selection is a no-op. The visual side (yellow halo on the
        matching plane in :class:`TrafficOverlay`) is updated
        immediately; the next VATSIM snapshot then re-centres the
        viewport via :meth:`_on_vatsim_pilots_updated`.

        Status-bar messages:

        * ``None -> <callsign>`` (begin tracking): brief "Tracking
          ``CALLSIGN``" message.
        * ``<callsign> -> None`` (release): brief "Tracking
          stopped" message.
        * Same value twice: silent (no status churn from a no-op).

        The user explicitly opted for an instant snap on each
        VATSIM update (every 15 s), so we do NOT call ``centerOn``
        from here — that would jump the viewport on the click,
        which feels disconnected from the user's gesture. The
        recenter happens on the next pilot snapshot through the
        normal update path.
        """
        previous = self._tracking_callsign
        if previous == callsign:
            return
        self._tracking_callsign = callsign
        self._traffic_overlay.set_tracked_callsign(callsign)
        if callsign is not None:
            self.statusBar().showMessage(f"Tracking {callsign}.", 4000)
        else:
            self.statusBar().showMessage("Tracking stopped.", 3000)

    def tracked_callsign(self) -> str | None:
        """Currently tracked callsign, or ``None`` if no plane is
        being followed. Surfaced for
        :class:`MapGraphicsView`'s plain-click branch (which only
        needs to fire the "release" edit if tracking is actually
        active — otherwise a click on empty chart would emit a
        spurious ``None`` and flash the "Tracking stopped"
        message).
        """
        return self._tracking_callsign

    def _recenter_on_tracked_pilot(self) -> None:
        """Snap the viewport so the tracked pilot sits two-thirds
        of the viewport ahead of itself along its heading.

        Called from :meth:`_on_vatsim_pilots_updated` on every
        fresh VATSIM snapshot. Three terminating conditions:

        1. No tracking active (``_tracking_callsign is None``) —
           no-op, the most common case.
        2. Tracked callsign isn't in the new snapshot — surface a
           status-bar message, clear the tracking state, and
           stop. This is the user-spec'd "stop with message" when
           the pilot disconnects.
        3. Tracked callsign IS in the snapshot but its lon/lat
           doesn't project to a scene point (no sheet calibrated,
           pilot off-chart). Treat as a transient miss: leave
           tracking state alone, skip the recenter for this tick,
           and let the next snapshot try again. Better than
           kicking the user out of tracking on a single
           off-chart blip.
        """
        callsign = self._tracking_callsign
        if callsign is None:
            return
        pilots = self._latest_vatsim_pilots or []
        pilot = next((p for p in pilots if p.callsign == callsign), None)
        if pilot is None:
            self._tracking_callsign = None
            self._traffic_overlay.set_tracked_callsign(None)
            self.statusBar().showMessage(
                f"Tracking stopped: {callsign} no longer in feed.",
                5000,
            )
            return
        scene_pt = self._project_route_point_to_scene(pilot.lat, pilot.lon)
        if scene_pt is None:
            return
        viewport = self._view.viewport()
        view_scale = float(self._view.transform().m11()) or 1.0
        target = compute_tracking_view_center(
            scene_pt,
            float(pilot.heading_deg),
            int(viewport.width()),
            int(viewport.height()),
            view_scale,
        )
        self._view.centerOn(target)

    # --- VATSIM worker lifecycle ------------------------------------

    def _start_vatsim_worker(self) -> None:
        """Spin up a fresh :class:`VatsimWorker` on its own
        :class:`QThread` and start polling.

        Idempotent: a previously-running worker is left alone (the
        toolbar toggle prevents this in practice but the guard is
        cheap). Loads the wake-category database from disk on the
        GUI thread before construction — it's a small JSON read
        (<10 ms) and we'd rather get a single startup hiccup here
        than mid-poll.
        """
        if self._vatsim_thread is not None:
            return
        try:
            wake_db = load_aircraft_wake_db()
        except Exception as exc:  # noqa: BLE001
            # Falling back to an empty DB lets the worker still
            # surface pilots — they'll all map to "unknown" wake
            # (gray icons), which is wrong-ish but functional.
            # Surface the failure in the status bar so the user
            # knows something went sideways.
            self.statusBar().showMessage(
                f"Couldn't load aircraft wake DB: {exc}. "
                "Using empty fallback (every plane will show as 'unknown').",
                10000,
            )
            wake_db = {}

        self._vatsim_thread = QThread(self)
        self._vatsim_worker = VatsimWorker(wake_db)
        self._vatsim_worker.moveToThread(self._vatsim_thread)
        # Worker's start_polling slot creates the QTimer on the
        # worker thread; ``started`` fires there so the queued
        # connection ends up running on the right thread.
        self._vatsim_thread.started.connect(self._vatsim_worker.start_polling)
        self._vatsim_worker.pilots_updated.connect(
            self._on_vatsim_pilots_updated
        )
        self._vatsim_worker.fetch_failed.connect(self._on_vatsim_fetch_failed)
        # ``finished → thread.quit`` via DirectConnection so the
        # quit flag is set *on the worker thread*, after
        # ``stop_polling`` has already torn the QTimer down on its
        # own affinity thread. The historical bug — where the GUI
        # called ``thread.quit()`` immediately after queuing
        # ``stop_polling`` — raced the queued event dispatch and
        # left the QTimer alive on a dead thread, producing
        # ``QObject::killTimer: Timers cannot be stopped from
        # another thread`` at QApplication teardown. See
        # :meth:`VatsimWorker.stop_polling`'s "Why the worker
        # emits finished itself" section for the full rationale.
        self._vatsim_worker.finished.connect(
            self._vatsim_thread.quit,
            Qt.ConnectionType.DirectConnection,
        )
        # 304-not-modified path is silent for the user — no status
        # bar traffic needed; the previous snapshot stays drawn.
        self._vatsim_thread.start()
        self.statusBar().showMessage(
            "VATSIM: connecting to live datafeed…", 4000
        )

    def _stop_vatsim_worker(self) -> None:
        """Tear down the worker + thread cleanly.

        Three-step shutdown:

        1. **Pre-set the stop flag from the GUI thread** so an
           in-flight HTTP fetch's post-fetch check
           (:meth:`VatsimWorker._on_tick`) sees the new value as
           soon as it returns and skips emitting a stale
           ``pilots_updated`` after we've already torn down our
           handlers. ``_stopped`` is a plain Python bool and the
           GIL makes the write atomic; both threads see a
           coherent value without explicit synchronization.
        2. **Synchronously** stop polling via a
           ``BlockingQueuedConnection`` invocation of
           :meth:`VatsimWorker.stop_polling`. The GUI thread
           blocks until the slot has actually run on the worker
           thread, which is the only way to guarantee the
           ``QTimer`` is stopped + destroyed on its own affinity
           thread *before* we ask the loop to exit. (The
           ordinary ``QueuedConnection`` variant raced
           ``thread.quit()`` — see "Why blocking" below.)
        3. ``quit`` the thread's event loop and ``wait`` for it
           to exit. By the time we get here, the timer is gone,
           so ``wait`` is just waiting for the loop's last
           bookkeeping to complete.

        Why blocking
        ------------

        The previous implementation used
        ``Qt.ConnectionType.QueuedConnection`` and immediately
        followed up with ``self._vatsim_thread.quit()`` + a
        ``wait(12_000)``. That works most of the time, but it
        loses a race whenever the worker is *idle* (between
        15-second polls) at the moment of close:

        * ``invokeMethod(QueuedConnection)`` posts a
          ``MetaCallEvent`` onto the worker thread's queue.
        * ``thread.quit()`` doesn't queue an event — it sets
          the event loop's exit flag directly. The currently-
          blocked ``QEventLoop::exec`` wakes immediately
          (because of the quit-flag check on every loop
          iteration) and returns *without dispatching the
          pending ``MetaCallEvent``*.
        * Result: ``stop_polling`` never runs. The
          ``QTimer`` stays alive on the worker thread, which
          has now exited.
        * ``wait()`` returns fast (no race to wait on),
          ``deleteLater`` posts a DeferredDelete onto a thread
          that no longer has an event loop, so the worker
          isn't actually destructed.
        * At ``QApplication`` teardown, Qt force-destroys the
          orphaned worker from the main thread. The destructor
          finds an active ``QTimer`` whose affinity thread is
          gone and emits both
          ``QObject::killTimer: Timers cannot be stopped from
          another thread`` and
          ``QObject::~QObject: Timers cannot be stopped from
          another thread`` warnings.

        ``BlockingQueuedConnection`` closes the race by
        guaranteeing the slot runs (and returns) on the worker
        thread before the GUI thread proceeds to ``quit()``.
        Deadlock risk is bounded: the worker's
        :meth:`stop_polling` does pure local work
        (``timer.stop()``, ``self._timer = None``, set a flag)
        and never blocks on the GUI thread, so the GUI thread's
        wait here is at most as long as whatever event the
        worker is currently processing.

        Wait timeout
        ------------

        ``wait(12_000)`` still covers the worst case of a
        post-``stop_polling`` event-loop wind-down hitting a
        slow ``_on_tick`` already in flight when the close
        arrived (the 10 s HTTP read timeout in
        :data:`vatsim_feed.DEFAULT_TIMEOUT_S` plus a 2 s
        margin). The *blocking* invokeMethod itself absorbs
        most of that wait — by the time we call ``quit()``
        the worker is already at a safe iteration boundary —
        but the 12 s ceiling stays as a defence in depth.
        """
        if self._vatsim_thread is None:
            return
        if self._vatsim_worker is not None:
            self._vatsim_worker._stopped = True  # noqa: SLF001
            try:
                QMetaObject.invokeMethod(
                    self._vatsim_worker,
                    "stop_polling",
                    Qt.ConnectionType.BlockingQueuedConnection,
                )
            except RuntimeError:
                # Defensive: if the worker C++ object was
                # already torn down (e.g. a ``finished`` signal
                # raced this method), the invocation raises.
                # That's the outcome we wanted anyway — the
                # timer is gone with the worker — so swallow
                # and proceed to ``quit`` the thread.
                pass
        self._vatsim_thread.quit()
        # See "Wait timeout" in the docstring for the 12 s value.
        self._vatsim_thread.wait(12_000)
        if self._vatsim_worker is not None:
            self._vatsim_worker.deleteLater()
            self._vatsim_worker = None
        self._vatsim_thread.deleteLater()
        self._vatsim_thread = None

    # --- v3 satellite imagery ----------------------------------------

    def _satellite_cache_root(self) -> Path:
        """Directory tree under which :class:`TileCache` stores tiles.

        Lives next to the rest of the app's per-project state so a
        user who clones a release tree to a new machine just copies
        ``.cvfr_routemaster/`` and brings their satellite cache with
        them. Created on demand by :class:`TileCache.put`.
        """
        return self._project_root / ".cvfr_routemaster" / "satellite_tiles"

    def _satellite_tile_cache(self) -> TileCache:
        """A fresh :class:`TileCache` rooted at this project's cache dir.

        Worker and renderer each call this independently so they
        never share mutable state — the ``TileCache`` is a thin
        path-math wrapper, not a stateful service.
        """
        return TileCache(self._satellite_cache_root())

    @Slot()
    def _schedule_satellite_visibility_continuation(self) -> None:
        """Schedule one more ``_update_satellite_visibility`` after
        a brief delay, used when the previous call hit the
        per-pass cache-hit decode cap.

        Distinct from
        :meth:`_schedule_satellite_visibility_update` (the
        debounce path driven by ``viewport_changed``) because
        this one isn't viewport-event-driven — the viewport
        hasn't changed, we just need another batch to finish
        decoding the visible tiles. Reuses the same target slot
        so the same code path (with all its overlay-null /
        toggle-off guards) handles both. Single-shot, no
        coalescing: if a *user* viewport event arrives between
        now and the continuation firing, the debounce timer
        will reset and the continuation becomes redundant —
        but ``_update_satellite_visibility`` is cheap-to-run
        idempotently (no-op if nothing to load), so we let
        both fire rather than try to cancel one.
        """
        QTimer.singleShot(
            SATELLITE_VISIBILITY_CONTINUATION_MS,
            self._update_satellite_visibility,
        )

    def _schedule_satellite_visibility_update(self) -> None:
        """Restart the debounce timer.

        Called from ``MapGraphicsView.viewport_changed`` on every
        scroll / zoom / resize. Restarting a single-shot timer
        coalesces bursts of events into one
        ``_update_satellite_visibility`` call after the user
        stops moving the view. Cheap to call (no-op if no overlay
        is active or the timer is already pending).
        """
        # No overlays in place yet, or satellite mode off → skip
        # entirely. Avoids running the visibility walk for users
        # who never enable the feature.
        if not self._act_show_satellite.isChecked():
            return
        if self._north_sat_overlay is None and self._south_sat_overlay is None:
            return
        self._sat_visibility_timer.start()  # restarts on each call

    @Slot()
    def _satellite_zoom_levels(self) -> list[int]:
        """Configured zoom levels for the multi-zoom overlay.

        Derived from :func:`load_satellite_zoom` (the user's
        configured *top* zoom; default 15): we always anchor the
        floor at z=12 — the cheapest tile layer, used as the
        permanent fallback under every other zoom — and include
        every integer level from z=12 up to and including the
        user's top. The multi-zoom overlay's layered-fallback
        behaviour (load everything at-or-below the active zoom)
        means the user gets the coarser layers "for free" as a
        cached base whenever they zoom out, and the cheapest
        layer is always loaded as a safety net under finer
        layers.

        Why a permanent z=12 floor rather than "top-2..top": the
        previous "top-2..top" rule meant a user who set
        ``satellite_zoom = 15`` got ``[13, 14, 15]`` — z=12
        dropped out of the fallback stack, contradicting the
        explicit "load z=12 always" performance preference.
        Always-z=12 keeps the cheapest layer in the fallback
        stack regardless of how high the user pushes the top.

        Returns
        -------
        list[int]
            Zoom levels in ascending order; always at least one
            element. Default ``[12, 13, 14, 15]`` for the default
            configuration; ``[12]`` only if the user explicitly
            set ``satellite_zoom`` to 12. With ``satellite_zoom``
            at the ``MAX_SATELLITE_ZOOM`` of 16 you'd get
            ``[12, 13, 14, 15, 16]``.
        """
        top = max(12, int(load_satellite_zoom()))
        return list(range(12, top + 1))

    def _update_satellite_visibility(self) -> None:
        """Compute the current viewport in scene coords and push it
        to both overlays.

        Wired to the debounce timer's ``timeout`` signal. The
        viewport rect is mapped through the view's transform
        (``mapToScene(viewport().rect())``) so it accounts for
        zoom/pan/scale exactly as Qt would render — the overlay's
        per-tile ``sceneBoundingRect().intersects(rect)`` check is
        then a clean visibility test.

        We also pass the view's current scale (``transform().m11()``)
        so the multi-zoom wrapper can switch between configured
        zoom levels (z=12/13/14 by default). The active zoom only
        changes when the scale crosses a threshold; mid-pan calls
        with the same scale trigger no zoom switch.

        Side-effect: any visible tile that isn't in the disk
        cache is forwarded to the on-demand fetch worker via
        :meth:`_ensure_satellite_demand_worker`. This is what
        makes lazy load *complete*: the user pans to a never-
        downloaded area, the visibility walk reports misses, and
        the demand worker fetches them while the placeholder is
        still on screen. ``tile_ready`` then triggers a refresh
        and the placeholder swaps to imagery.
        """
        if not self._act_show_satellite.isChecked():
            return
        # ``mapToScene(QRect)`` returns a polygon (rotated views
        # produce non-axis-aligned scene-space quads). Take the
        # bounding rect — for our use case overlays don't care
        # about polygon precision; loading a tile that's just
        # outside a rotated viewport is a non-issue.
        scene_rect = self._view.mapToScene(
            self._view.viewport().rect()
        ).boundingRect()
        view_scale = float(self._view.transform().m11()) or 1.0
        all_misses: list[TileCoord] = []
        any_more_pending = False
        for ov in (self._north_sat_overlay, self._south_sat_overlay):
            if ov is not None:
                _loaded, _evicted, misses, more_pending = ov.update_visibility(
                    scene_rect, view_scale
                )
                all_misses.extend(misses)
                if more_pending:
                    any_more_pending = True
        if any_more_pending:
            # The per-call load cap was hit on at least one
            # sheet — there are still visible cache-hit tiles
            # waiting to be decoded into their items. Schedule
            # another pass shortly so the remaining tiles
            # stream in over the next few frames. The interval
            # is short enough that the user perceives a smooth
            # fill rather than a "first half loads instantly,
            # rest waits for me to wiggle the view" gap, and
            # long enough for Qt to actually repaint between
            # batches (a 0 ms reschedule starves the paint
            # event in practice).
            self._schedule_satellite_visibility_continuation()
        if all_misses:
            # Lazy-start the demand worker on the first miss —
            # users who never pan to an uncached region pay
            # nothing for this feature. The first call connects
            # our ``_satellite_enqueue_tile`` signal to the worker's
            # ``enqueue`` slot with a queued connection, so we
            # just emit per coord and Qt routes the call onto the
            # worker's own event loop. No ``QMetaObject.invokeMethod``
            # / ``Q_ARG`` dance — newer PySide6 builds reject
            # ``Q_ARG(object, ...)`` outright (no QMetaType
            # registered for the bare ``object`` type), so the
            # signal-based path is also the only one that works
            # portably across PySide6 versions.
            self._ensure_satellite_demand_worker()
            for coord in all_misses:
                self._satellite_enqueue_tile.emit(coord)

    def _build_satellite_overlays(self) -> None:
        """Construct per-sheet :class:`SatelliteOverlay` instances.

        Called from :meth:`_on_map_finished` after the chart
        pixmap items + calibrations are in place. Each overlay
        enumerates every Web Mercator tile in its sheet's lat/lon
        bbox and creates one ``QGraphicsPixmapItem`` per tile,
        parented to the chart pixmap (so chart pan/zoom carries
        the overlay along automatically). Eager construction of
        the items is cheap (~5000 items per sheet at z=14, well
        under 50 ms in practice); pixmap *loading* is lazy in the
        sense that only tiles already on disk get their real
        pixmap immediately — the rest sit on the "Loading Tile…"
        placeholder until the bulk-fetch worker writes them or
        the on-demand fetcher (Phase 7e) brings them in.

        A sheet without a calibration (e.g. user hasn't placed
        anchors yet) gets no overlay — calling ``set_visible`` on
        a ``None`` overlay is a no-op so the toolbar toggle stays
        functional in that state, just with no satellite imagery
        for the un-calibrated sheet.
        """
        cache = self._satellite_tile_cache()
        zoom_levels = self._satellite_zoom_levels()
        sat_visible = bool(self._act_show_satellite.isChecked())
        # Initial view scale picks the active zoom inside the
        # multi-zoom wrapper. We read it once here; subsequent
        # zoom changes flow through ``_update_satellite_visibility``
        # which calls ``update_visibility(scene_rect, view_scale)``.
        initial_scale = float(self._view.transform().m11()) or 1.0

        # Build the chart-seam partition once, share it across both
        # sheets' multi-zoom overlays. This replaces the previous
        # UV-distance partition that picked tiles by closeness to
        # each sheet's pixmap centre — see :class:`ChartSeamPartition`
        # docstring for the rationale (chart-seam partition aligns
        # the satellite-tile boundary with the visible chart-pixmap
        # boundary at the same scene_y, so any per-sheet affine
        # disagreement produces a single combined step rather than
        # two decoupled ones).
        chart_seam = self._build_chart_seam_partition()
        # ``sheet_z_bump`` tie-breaks z-order in the one-row
        # partition-overlap where both sheets' overlays enumerate
        # the same tile (the spill-over row that closes the
        # affine-disagreement gap at the seam — see
        # :class:`ChartSeamPartition` and
        # :class:`MultiZoomSatelliteOverlay`). ``0.0`` for north,
        # ``0.005`` for south so south wins in the overlap and
        # south's visible territory stays identical to the
        # un-extended partition; the only visible delta of the
        # extension is the gap-sliver north now paints into.
        for chart_item, cal, partition_for_sheet, sheet_z_bump, set_overlay in (
            (
                self._north_item,
                self._geo_north,
                chart_seam.for_north() if chart_seam is not None else None,
                0.0,
                lambda ov: setattr(self, "_north_sat_overlay", ov),
            ),
            (
                self._south_item,
                self._geo_south,
                chart_seam.for_south() if chart_seam is not None else None,
                0.005,
                lambda ov: setattr(self, "_south_sat_overlay", ov),
            ),
        ):
            if chart_item is None or cal is None:
                continue
            pix = chart_item.pixmap()
            w = int(pix.width())
            h = int(pix.height())
            if w <= 0 or h <= 0:
                continue
            ov = MultiZoomSatelliteOverlay(
                chart_item=chart_item,
                calibration=cal,
                pixmap_size=(w, h),
                zoom_levels=zoom_levels,
                tile_cache=cache,
                initial_view_scale=initial_scale,
                chart_seam_partition=partition_for_sheet,
                sheet_z_bump=sheet_z_bump,
            )
            ov.set_visible(sat_visible)
            set_overlay(ov)

        # Waypoint marker overlays sit on top of the satellite
        # tiles so VRPs stay visible (and clickable for the
        # routing pipeline) when satellite imagery covers the
        # chart's printed triangles. Built right after the
        # satellite overlays so they share the same per-sheet
        # construction path; lifecycle in ``_clear_map_items``
        # tears both down together.
        self._build_waypoint_marker_overlays()

    def _build_waypoint_marker_overlays(self) -> None:
        """Construct per-sheet :class:`WaypointMarkerOverlay` instances.

        Sourced from :attr:`_waypoints_export` (the same list the
        nearest-waypoint routing pipeline uses), so any waypoint
        the user can shift-add gets a marker. Out-of-sheet
        waypoints are filtered inside the overlay during
        construction; calling code doesn't need to partition
        north vs south manually.

        Triangle side length comes from
        :func:`load_waypoint_marker_size_px` so the user's
        Display Settings choice is honoured at chart-load time
        and after a "Reapply / Rebuild" via
        :meth:`_rebuild_waypoint_marker_overlays`.

        Visibility starts in lockstep with the satellite-view
        toggle. When the user is in chart mode the overlays are
        hidden so the chart's printed triangles aren't visually
        doubled by these scene-item triangles.
        """
        waypoints = list(self._waypoints_export)
        sat_on = bool(self._act_show_satellite.isChecked())
        marker_side_px = float(
            load_waypoint_marker_size_px(self._project_root)
        )
        # The chart-seam partition mirrors what the satellite overlay
        # uses for tiles: a waypoint inside the chart-overlap strip
        # projects inside *both* pixmaps, and without a partition each
        # per-sheet overlay adds its own marker, so the user sees two
        # triangles for every overlap waypoint. Routing both overlays
        # through the same partition keeps the marker boundary aligned
        # with the chart-pixmap seam — the same scene_y the satellite
        # tile boundary sits at — so the visible "this side is north,
        # that side is south" demarcation is consistent across charts,
        # tiles, and markers.
        chart_seam = self._build_chart_seam_partition()
        for chart_item, cal, partition_for_sheet, set_attr in (
            (
                self._north_item,
                self._geo_north,
                chart_seam.for_north() if chart_seam is not None else None,
                lambda ov: setattr(
                    self, "_north_wp_marker_overlay", ov
                ),
            ),
            (
                self._south_item,
                self._geo_south,
                chart_seam.for_south() if chart_seam is not None else None,
                lambda ov: setattr(
                    self, "_south_wp_marker_overlay", ov
                ),
            ),
        ):
            if chart_item is None or cal is None:
                continue
            pix = chart_item.pixmap()
            w = int(pix.width())
            h = int(pix.height())
            if w <= 0 or h <= 0:
                continue
            ov = WaypointMarkerOverlay(
                chart_item=chart_item,
                calibration=cal,
                pixmap_size=(w, h),
                waypoints=waypoints,
                triangle_side_px=marker_side_px,
                chart_seam_partition=partition_for_sheet,
            )
            ov.set_visible(sat_on)
            set_attr(ov)

    def _rebuild_waypoint_marker_overlays(self) -> None:
        """Tear down + reconstruct both per-sheet waypoint marker
        overlays, picking up any changes to the user-configured
        marker size (and any other marker-style knob a future
        Display Settings extension exposes).

        Used by :meth:`_open_font_settings` after the user
        accepts a size change. No-op when no overlays have been
        built yet (chart still loading). Markers are cheap to
        rebuild (a few hundred items each), so we always do the
        full tear-down rather than try to live-resize each item;
        live resize would require re-laying out every marker's
        label-rect math, and triangle-size changes hit that
        codepath, so a clean rebuild is the simpler honest
        contract.
        """
        for attr in (
            "_north_wp_marker_overlay",
            "_south_wp_marker_overlay",
        ):
            ov = getattr(self, attr, None)
            if ov is not None:
                ov.teardown()
                setattr(self, attr, None)
        self._build_waypoint_marker_overlays()

    def _rebuild_overlays_after_calibration_change(self) -> None:
        """Tear down + reconstruct the satellite and waypoint marker
        overlays so they pick up the latest per-sheet calibration.

        Called from :meth:`_finalize_auto_anchor_calibration` after a
        sheet's affine has been saved to ``self._geo_north`` /
        ``self._geo_south`` and persisted to disk. Necessary because:

        * :meth:`_build_satellite_overlays` runs exactly once per
          session, from :meth:`_on_map_finished`, when ``cal is None``
          for any not-yet-calibrated sheet — and the build loop skips
          ``None`` calibrations entirely. A first-time calibration
          (or a recalibration after a reset) lands with both
          overlay attributes still ``None``, so toggling the
          Satellite-view action would do nothing without this
          rebuild.
        * Even on a *re-calibration* (overlays already exist),
          the existing items hold a stale affine — tiles would be
          placed against the old calibration while the chart
          pixmaps already moved. Tear-down + rebuild is the cheap
          honest contract; per-item retransform would require
          poking calibration internals on the overlay.

        Both sheets are rebuilt unconditionally even when only one
        sheet was just calibrated, because each overlay carries a
        :class:`ChartSeamPartition` reference (which itself bakes in
        north's calibration and the chart-seam scene-Y). If only the
        *just-calibrated* side were rebuilt, the peer overlay would
        still hold a stale partition and could either drop tiles
        wrongly or double-render in the overlap strip.

        Visibility tracking happens inside
        :meth:`_build_satellite_overlays` (it reads the toolbar
        toggle on construction), so this method doesn't need a
        separate ``set_visible`` pass. If satellite mode is on,
        we additionally push the current viewport so tiles start
        loading right away rather than waiting for the next pan
        or zoom event.
        """
        for attr in (
            "_north_sat_overlay",
            "_south_sat_overlay",
        ):
            ov = getattr(self, attr, None)
            if ov is not None:
                ov.teardown()
                setattr(self, attr, None)
        for attr in (
            "_north_wp_marker_overlay",
            "_south_wp_marker_overlay",
        ):
            ov = getattr(self, attr, None)
            if ov is not None:
                ov.teardown()
                setattr(self, attr, None)
        self._build_satellite_overlays()
        if self._act_show_satellite.isChecked():
            self._update_satellite_visibility()

    def _ensure_satellite_status_widgets(self) -> None:
        """Attach the satellite progress + attribution labels to the
        status bar lazily. Idempotent; called whenever we'd want
        the widgets present (toggle on, worker start)."""
        if self._sat_progress_label is None:
            label = QLabel()
            label.setObjectName("satellite_progress_label")
            label.setVisible(False)
            self.statusBar().addPermanentWidget(label)
            self._sat_progress_label = label
        if self._sat_attribution_label is None:
            attr = QLabel(ESRI_ATTRIBUTION)
            attr.setObjectName("satellite_attribution_label")
            attr.setVisible(False)
            attr.setToolTip(
                "Satellite imagery is licensed from Esri and is "
                "displayed under their World Imagery service "
                "attribution requirement."
            )
            self.statusBar().addPermanentWidget(attr)
            self._sat_attribution_label = attr

    def _ensure_view_info_widget(self) -> None:
        """Attach the view-info label (viewport width in NM + active
        satellite zoom) to the status bar lazily.

        The label is *always* visible — both pieces of information
        are useful regardless of whether satellite mode is on. The
        zoom-level readout in particular is the diagnostic the user
        needs to verify that the multi-zoom resolution-switching
        logic is firing at the view-scale boundaries they expect.

        Inserted *before* the satellite progress label in the
        permanent-widget list so it shows up to the left of the
        bulk-fetch progress text — keeps "always-on" information
        anchored at a stable horizontal position and "transient"
        download progress on the right.
        """
        if self._view_info_label is not None:
            return
        label = QLabel("View: —")
        label.setObjectName("view_info_label")
        label.setToolTip(
            "Approximate width of the visible chart area in nautical "
            "miles, plus the satellite-tile zoom level the multi-zoom "
            "overlay would render at the current view scale.\n\n"
            "z=15 is the finest available imagery (≈4 m/px); z=14, "
            "z=13, and z=12 are progressively coarser fallbacks. "
            "The selector keeps you on the cheapest layer that "
            "still shows useful detail: z=12 by default, escalating "
            "to z=13 once you zoom past ×0.75, to z=14 past ×1.5, "
            "and to z=15 past ×3.0 (boundaries are at view-scale "
            "0.75, 1.5, and 3.0). z=12 is always loaded as the "
            "permanent base layer regardless of which finer layer "
            "is active."
        )
        self.statusBar().addPermanentWidget(label)
        self._view_info_label = label

    @Slot()
    def _update_view_info_label(self) -> None:
        """Recompute the viewport-width-in-NM + active-zoom readout
        and push it to the status-bar label.

        Cheap — a handful of coord transforms + one ``setText``.
        Wired to the view's ``viewport_changed`` signal without
        a debounce so the indicator tracks scrolls / wheel-zooms
        in real time; that's what makes it useful as the
        zoom-switch verification tool the user asked for.

        Gracefully degrades when calibration isn't loaded yet
        (shows ``—`` for both fields rather than crashing on a
        ``None`` calibration). Always renders *some* text so a
        present-but-blank label doesn't get mistaken for an
        application freeze.
        """
        self._ensure_view_info_widget()
        if self._view_info_label is None:
            # Defensive — ``_ensure_view_info_widget`` should have
            # populated this, but if the status bar isn't yet ready
            # (very early in init) we bail rather than crash.
            return
        try:
            width_nm = self._viewport_width_nm()
        except Exception:  # noqa: BLE001 — diagnostic widget, never
            # let an arithmetic edge case (a degenerate calibration,
            # a viewport that maps outside both sheets, etc.) take
            # down the rest of the app. The label just shows "—".
            width_nm = None
        view_scale = float(self._view.transform().m11())
        zoom_levels = self._satellite_zoom_levels()
        if zoom_levels and view_scale > 0:
            active_z = select_zoom_for_view_scale(view_scale, zoom_levels)
            z_str = f"z={active_z}"
        else:
            z_str = "z=—"
        if width_nm is not None and math.isfinite(width_nm):
            # Variable precision: a wide view (>10 nm) needs only
            # whole-NM resolution; a zoomed-in view (≤10 nm) benefits
            # from one decimal place. Avoids reading "1 nm" when the
            # actual value is 1.4 nm — a meaningful difference for
            # circuit-pattern situational awareness.
            if width_nm >= 10.0:
                width_str = f"{width_nm:,.0f} NM"
            else:
                width_str = f"{width_nm:.1f} NM"
        else:
            width_str = "— NM"
        # Include the raw view-scale so a user comparing against
        # the documented boundary table (0.5 / 0.25) can see
        # exactly where they are; the active zoom alone hides the
        # answer to "is this view scale anywhere near a boundary?"
        self._view_info_label.setText(
            f"View: {width_str} · scale {view_scale:.3f} · sat {z_str}"
        )

    def _viewport_width_nm(self) -> float | None:
        """Approximate the width of the visible viewport in nautical
        miles by sampling two points at the same y on the viewport's
        midline and converting both to lat/lon via the appropriate
        sheet's calibration.

        Returns ``None`` if neither sheet's calibration is loaded
        or the viewport center doesn't fall inside any calibrated
        sheet (e.g. user has panned to a region of the scene that
        sits between the two sheet pixmaps). The caller renders
        ``"— NM"`` in that case.

        Why two points at the same y (not the full diagonal): the
        user wants to know "how wide is the chart area I'm looking
        at right now", which is naturally the horizontal extent.
        A diagonal would conflate width and height. East-west span
        also happens to be the dominant component for VFR mission
        planning across an Israel-sized country (one-degree-of-lon
        ≈ 51 NM at 32 °N).

        Uses the spherical great-circle (haversine) formula. At the
        ≤200 NM extents we ever care about, a flat-Earth
        ``Δlon · cos(lat) · 60`` approximation would be accurate to
        ~0.1 % — haversine costs one extra ``sin / asin`` pair, well
        worth it for a stable cross-latitude readout.
        """
        vp_screen = self._view.viewport().rect()
        if vp_screen.width() <= 0 or vp_screen.height() <= 0:
            return None
        vp_scene = self._view.mapToScene(vp_screen).boundingRect()
        center_scene = vp_scene.center()
        # Walk both sheets; pick the one whose chart pixmap contains
        # the viewport center. Doing it center-anchored rather than
        # "majority of viewport overlaps which sheet" keeps the
        # answer stable across small pans across the sheet seam.
        # ``mapFromScene`` accounts for the sheet's own pos +
        # scale (the joint LSQ layout solver sets these per sheet
        # at calibration time, and Alt+wheel can still nudge each
        # sheet's scale independently of the global view transform),
        # so the uv coordinates we feed the calibration are always
        # in the calibration's own pixel space.
        for chart_item, cal in (
            (self._north_item, self._geo_north),
            (self._south_item, self._geo_south),
        ):
            if chart_item is None or cal is None:
                continue
            pix = chart_item.pixmap()
            w = pix.width()
            h = pix.height()
            if w <= 0 or h <= 0:
                continue
            local_centre = chart_item.mapFromScene(center_scene)
            if not (
                0.0 <= local_centre.x() <= w
                and 0.0 <= local_centre.y() <= h
            ):
                continue
            # Sample left/right at the same scene-y so we measure
            # horizontal extent specifically (see docstring).
            left_scene = QPointF(vp_scene.left(), center_scene.y())
            right_scene = QPointF(vp_scene.right(), center_scene.y())
            left_local = chart_item.mapFromScene(left_scene)
            right_local = chart_item.mapFromScene(right_scene)
            try:
                lon_l, lat_l = cal.uv_to_lonlat(
                    left_local.x() / w, left_local.y() / h
                )
                lon_r, lat_r = cal.uv_to_lonlat(
                    right_local.x() / w, right_local.y() / h
                )
            except (AssertionError, ValueError, ZeroDivisionError):
                # Calibration not yet fitted, or an extrapolation
                # past the calibration's valid range. Treat as "no
                # info" rather than guessing.
                continue
            return _haversine_nm(lat_l, lon_l, lat_r, lon_r)
        return None

    @Slot(bool)
    def _on_show_satellite_toggled(self, on: bool) -> None:
        """Toolbar's "Satellite view" toggle handler.

        Two responsibilities:

        1. Persist the new state to QSettings so the choice
           survives across sessions.
        2. Toggle the per-tile satellite overlays on top of each
           chart pixmap. The chart pixmap stays visible underneath
           — tile items collectively cover it where loaded, and the
           "Loading Tile…" placeholder covers it where the cache
           hasn't filled yet, so the user always has a visually
           coherent view (no flash of black).

        Cache empty → kick the first-launch prompt so the user
        knows why the placeholders are everywhere.
        """
        save_show_satellite(bool(on))
        self._ensure_satellite_status_widgets()
        if self._sat_attribution_label is not None:
            self._sat_attribution_label.setVisible(bool(on))
        # Seed the per-zoom progress map from disk on toggle-on
        # so the multi-line status-bar readout is accurate
        # before any worker has fired. Cheap (filesystem
        # ``exists`` per candidate tile) and safe to re-run if
        # the user toggles satellite on/off repeatedly.
        if on:
            self._seed_satellite_progress_per_zoom()
        # Refresh the map-hint to add/remove the satellite
        # loading-logic explainer line as the toggle flips.
        self._update_map_hint_text()

        # Flip overlay visibility. The chart pixmap stays visible
        # always — tiles cover it where loaded, placeholder covers
        # the rest. No more chart-hide/sat-show juggling.
        for ov in (self._north_sat_overlay, self._south_sat_overlay):
            if ov is not None:
                ov.set_visible(bool(on))
        # Waypoint markers track the satellite toggle: visible
        # when the chart's printed triangles are covered, hidden
        # otherwise so the chart-baked triangles aren't doubled.
        for wp_ov in (
            self._north_wp_marker_overlay,
            self._south_wp_marker_overlay,
        ):
            if wp_ov is not None:
                wp_ov.set_visible(bool(on))

        if not on:
            return

        cache = self._satellite_tile_cache()
        # ``is_empty`` short-circuits on the first cached file
        # (one ``scandir`` call); the previous ``size_bytes() == 0``
        # walked the whole provider tree summing sizes, which at
        # ~107k tiles can stall the GUI thread for many seconds
        # — the second-half of the "Not Responding" window on
        # toggle-on at startup. We only need a yes/no answer here.
        cache_is_empty = cache.is_empty()
        # Empty cache + toggle-on = the user wants imagery but
        # we don't have any yet. Same handling as the first-launch
        # path: show the one-time notice (if it hasn't been shown
        # already) and start the bulk download. If the notice
        # *was* already shown in a prior session, just start the
        # worker silently — the user has already been told what
        # to expect, and the download was probably interrupted or
        # never got going for some other reason.
        if cache_is_empty:
            if not load_satellite_notice_shown():
                self._show_first_download_notice_and_start()
            elif self._satellite_thread is None:
                self._start_satellite_worker()

        # Push the current viewport to both overlays so they
        # load whatever's in cache + on-screen *immediately* —
        # rather than waiting for the next scroll. Bypasses the
        # debounce timer because the user is sitting in front of
        # an empty overlay right now and shouldn't have to wait
        # 100 ms for tiles to start rendering.
        self._update_satellite_visibility()

    def _show_first_download_notice_and_start(self) -> None:
        """Show the one-time informational notice, then start the
        bulk-fetch worker.

        Called when (a) the chart finishes loading on a fresh
        install (no notice recorded yet) and (b) the user toggles
        Satellite view on with an empty cache and no recorded
        notice. The notice is purely informational — the bulk
        download will start regardless of how the user dismisses
        the dialog (OK button, X-close, Esc) because there is no
        accept/decline branching in the v3.3+ flow.

        Idempotent across the current session via the
        ``_sat_first_launch_prompt_shown`` guard: if the notice
        has already fired in this session (e.g. the user toggled
        sat-view on, dismissed the notice, then toggled it off
        and back on), don't re-show it. The notice will still
        not appear on the next session because the post-dismiss
        ``save_satellite_notice_shown(True)`` call persists the
        fact across launches.
        """
        if self._sat_first_launch_prompt_shown:
            return
        self._sat_first_launch_prompt_shown = True

        # Multi-zoom: count tiles across all configured levels so
        # the notice quotes the real download size. The default
        # ``[12, 13, 14, 15]`` set totals roughly the primary
        # count plus ~25k tiles for the three coarser levels.
        # We're frank about the number so the user understands
        # what the program is about to chew through.
        levels = self._satellite_zoom_levels()
        min_lat, max_lat, min_lon, max_lon = ISRAEL_BBOX
        tile_count = sum(
            count_tiles_for_bbox(
                min_lat=min_lat,
                max_lat=max_lat,
                min_lon=min_lon,
                max_lon=max_lon,
                z=z,
            )
            for z in levels
        )

        show_first_download_notice(
            self,
            tile_count=tile_count,
            zoom_levels=levels,
        )
        # Persist BEFORE starting the worker so a crash between
        # "dialog dismissed" and "worker started" doesn't re-show
        # the notice on next launch — the user has already been
        # informed; resume should be silent.
        save_satellite_notice_shown(True)
        self._start_satellite_worker()

    def _satellite_check_on_map_loaded(self) -> None:
        """One-stop dispatcher fired after the chart finishes loading.

        Three possible actions, all mutually exclusive:

        1. First-launch notice + start — the one-time
           informational notice hasn't been shown yet. We show
           it once per session (subject to the
           ``_sat_first_launch_prompt_shown`` guard) and start
           the worker.
        2. Silent resume — the notice has already been shown in
           some prior session, but the cache is incomplete. The
           worker picks up where it left off.
        3. Re-render the satellite warp — user has the toggle on
           from a prior session and the chart just (re)loaded
           with fresh calibration; the warp needs to be
           recomputed against the new pixmap dimensions.
        """
        if (
            not load_satellite_notice_shown()
            and not self._sat_first_launch_prompt_shown
        ):
            self._show_first_download_notice_and_start()
        else:
            self._check_satellite_resume_on_startup()
        if self._act_show_satellite.isChecked():
            # Sync visibility to the just-built sat items + render.
            self._on_show_satellite_toggled(True)

    def _check_satellite_resume_on_startup(self) -> None:
        """Silently resume any partial bulk-fetch download.

        Called once the chart is loaded — earlier and any modal
        we showed would stack on a blank window. There is no
        prompt in v3.3+: the user has already been informed
        (via :func:`show_first_download_notice` in a prior
        session) that the download resumes across interruptions,
        so we just spin the worker up if work remains.

        With multi-zoom downloads, this also kicks off a chain
        for users who have the primary zoom fully cached from a
        pre-multi-zoom version of the app but are missing the
        secondary zooms (z=13, z=12). We detect that by scanning
        the cache directly via :func:`count_cached_tiles_in_bbox`
        (scandir-based, fast).
        """
        layout_diag.log(
            "satellite.resume_check_start",
            notice_shown=load_satellite_notice_shown(),
        )
        # Guard: don't double-start a worker. The first-launch
        # path may have spun one up already; this method should
        # only kick a fresh worker for the silent-resume case.
        if self._satellite_thread is not None:
            return
        # We deliberately do *not* seed the per-zoom progress map
        # here. ``_seed_satellite_progress_per_zoom`` calls
        # ``tiles_to_fetch_for_bbox`` for every configured zoom,
        # which does one ``Path.exists`` per candidate tile — on
        # the default ``[12, 13, 14, 15]`` set that's ~107 k
        # stat calls on the GUI thread, freezing the app for
        # 5-10 s on startup right when the user is least
        # tolerant of unresponsiveness (they just launched).
        # Instead, the chain's per-worker initial progress emit
        # (see :meth:`SatelliteWorker.start_fetch`) populates
        # each zoom's entry as the chain reaches it; with the
        # coarsest-first order a returning user with z=12/13/14
        # cached sees the full status-bar readout populate over
        # ~1-2 s rather than freezing for ~10 s. The toggle-on
        # path (:meth:`_on_show_satellite_toggled`) still seeds
        # eagerly because the user explicitly asked to see the
        # imagery and is OK with a brief load.
        cache = self._satellite_tile_cache()
        state = read_download_state(cache)
        layout_diag.log(
            "satellite.resume_check_state",
            has_state_file=state is not None,
            primary_complete=(state is not None and state.is_complete()),
        )
        # Primary not yet complete → resume the full chain (which
        # picks up where state-file's primary zoom left off, then
        # chains to secondaries).
        if state is None or not state.is_complete():
            self._start_satellite_worker()
            return
        # Primary complete — check if any secondary zoom needs work.
        levels = sorted(self._satellite_zoom_levels(), reverse=True)
        primary = levels[0] if levels else 0
        secondaries = [z for z in levels if z != primary]
        if not secondaries:
            return
        min_lat, max_lat, min_lon, max_lon = ISRAEL_BBOX
        pending: list[int] = []
        for z in secondaries:
            # Use the bulk-scandir helpers rather than
            # ``tiles_to_fetch_for_bbox`` (per-tile ``stat``).
            # We only need a "is any tile missing?" yes/no, which
            # ``cached < total`` answers without ever materialising
            # the missing-tile list. On a returning user with the
            # secondaries mostly cached this is ~3 syscalls per
            # zoom instead of ~18-26 k — the gap between
            # "essentially instant startup" and the "Not Responding"
            # window the user reported.
            total = count_tiles_for_bbox(
                min_lat=min_lat,
                max_lat=max_lat,
                min_lon=min_lon,
                max_lon=max_lon,
                z=z,
            )
            cached = count_cached_tiles_in_bbox(
                cache=cache,
                min_lat=min_lat,
                max_lat=max_lat,
                min_lon=min_lon,
                max_lon=max_lon,
                z=z,
            )
            if cached < total:
                pending.append(z)
        if not pending:
            return
        # Plan the remainder of the chain through the same
        # coarsest-first / persist-on-finest policy that
        # ``_start_satellite_worker`` uses. The primary
        # (highest-configured) zoom is already complete on this
        # branch — that's the invariant guarding this code path —
        # so it's *not* in ``pending``; every remaining link is a
        # coarser zoom and the persist-on-finest rule means the
        # *finest of the remaining ones* gets persist=True. That
        # is, if the user's full chain is [12, 13, 14, 15] and
        # z=15 is already done, the remaining chain is
        # [12, 13, 14] with persist=True on z=14 (the last in
        # this sub-chain). Re-doing z=14's enumeration on the
        # next launch is cheap (~18k stat calls, ~10-15 s) so
        # the persist=True is mostly a courtesy in this branch.
        chain = _plan_satellite_zoom_chain(pending)
        first_zoom, first_persist = chain[0]
        self._satellite_pending_zoom_chain = list(chain[1:])
        self._start_satellite_worker_for_zoom(
            first_zoom, persist=first_persist
        )

    def _start_satellite_worker(self) -> None:
        """Begin the multi-zoom bulk-fetch chain.

        Chain order is computed by :func:`_plan_satellite_zoom_chain`
        (see its docstring for the full rationale): coarsest-first,
        with ``persist_state=True`` only on the final (finest) link.
        On the default ``[12, 13, 14, 15]`` set that produces:
        z=12 (persist=False) → z=13 (persist=False) → z=14
        (persist=False) → z=15 (persist=True). The single
        resumable link is the one big enough that a user would
        want to interrupt and continue across sessions.

        Idempotent: returns immediately if a worker is already
        running. The manual trigger path surfaces the no-op
        through a status-bar message.
        """
        if self._satellite_thread is not None:
            return
        chain = _plan_satellite_zoom_chain(self._satellite_zoom_levels())
        if not chain:
            return
        first_zoom, first_persist = chain[0]
        # Queue the remainder; ``_on_satellite_finished`` pops one
        # link at a time. The chain may be empty after popping
        # the head (single-level configuration).
        self._satellite_pending_zoom_chain = list(chain[1:])
        layout_diag.log(
            "satellite.chain_plan",
            chain=",".join(f"{z}:{p}" for z, p in chain),
            n_links=len(chain),
        )
        self._start_satellite_worker_for_zoom(
            first_zoom, persist=first_persist
        )

    def _start_satellite_worker_for_zoom(
        self, zoom: int, *, persist: bool
    ) -> None:
        """Spin up a :class:`SatelliteWorker` for a specific zoom.

        Internal helper used by both the chain's first link
        (primary zoom, ``persist=True``) and subsequent links
        (secondary zooms, ``persist=False``). Wires the same
        signals + auto-cleanup chain so the calling code never
        needs to think about which zoom is running.

        Parameters
        ----------
        zoom
            Which zoom level to fetch tiles for.
        persist
            Whether the worker writes to the cache's
            ``_download_state.json``. Only the primary zoom does;
            secondaries skip persistence.
        """
        cache = self._satellite_tile_cache()
        self._ensure_satellite_status_widgets()
        self._satellite_running_zoom = int(zoom)
        layout_diag.log(
            "satellite.worker_starting",
            zoom=int(zoom),
            persist=bool(persist),
            pending_chain_len=len(self._satellite_pending_zoom_chain),
        )

        self._satellite_thread = QThread(self)
        self._satellite_worker = SatelliteWorker(
            cache,
            bbox=ISRAEL_BBOX,
            zoom=zoom,
            persist_state=persist,
        )
        self._satellite_worker.moveToThread(self._satellite_thread)
        self._satellite_thread.started.connect(
            self._satellite_worker.start_fetch
        )
        self._satellite_worker.progress.connect(
            self._on_satellite_progress
        )
        self._satellite_worker.tile_fetched.connect(
            self._on_satellite_tile_fetched
        )
        self._satellite_worker.finished.connect(
            self._on_satellite_finished
        )
        self._satellite_worker.failed.connect(
            self._on_satellite_failed
        )
        # Same shutdown chain we use for the on-demand worker
        # (see ``_ensure_satellite_demand_worker``): ``finished``
        # drives ``worker.deleteLater`` *on the worker thread* so
        # any pending ``QTimer.singleShot`` self-callback gets
        # killed by Qt's normal child-destruction on the right
        # thread — without this, the timer is killed later from
        # the GUI thread (when the QThread itself is destroyed)
        # and Qt logs ``QObject::killTimer: Timers cannot be
        # stopped from another thread``. ``thread.finished`` then
        # triggers ``thread.deleteLater`` from the GUI thread,
        # which is fine — the QThread itself is owned by the GUI.
        self._satellite_worker.finished.connect(
            self._satellite_worker.deleteLater
        )
        # CRITICAL: ``thread.quit`` is wired DirectConnection (not
        # AutoConnection) so it runs IMMEDIATELY on the worker
        # thread when ``worker.finished`` fires, instead of being
        # queued to the GUI thread. Why this matters: the QThread
        # was created with ``QThread(self)`` so its Qt thread
        # affinity is the GUI thread, which means auto-connection
        # chooses *queued*. Queued ``thread.quit`` then sits in
        # the GUI thread's event queue *behind* the queued
        # ``_on_satellite_finished`` slot — and that slot calls
        # ``_cleanup_satellite_worker_refs`` which itself calls
        # ``thread.wait(30_000)``, blocking the GUI thread's
        # event loop. The queued ``thread.quit`` can't be
        # processed, so the wait can't succeed, so we sit on it
        # for the full 30 s timeout per chain transition. With
        # the post-anchor-6.0 chain order (z=12 → 13 → 14 → 15)
        # a returning user with z=12/13/14 already cached burns
        # ~90 s of freezing on three back-to-back cached-zoom
        # transitions before the actual z=15 fetch finally
        # starts. ``QThread.quit`` is explicitly documented as
        # thread-safe, so direct-calling it from the emitter
        # (worker) thread is correct and breaks the deadlock.
        self._satellite_worker.finished.connect(
            self._satellite_thread.quit,
            Qt.ConnectionType.DirectConnection,
        )
        self._satellite_thread.finished.connect(
            self._satellite_thread.deleteLater
        )
        self._satellite_thread.start()
        if self._sat_progress_label is not None:
            self._sat_progress_label.setVisible(True)
        # Show the running zoom in the status bar so users know
        # progress is moving across multiple levels (e.g. "z=14
        # 12345/30000" then "z=13 1234/7800"). The
        # progress-handler updates the label later; this is the
        # initial "we just started" message.
        self.statusBar().showMessage(
            f"Satellite imagery: starting download (z={zoom})…", 5000
        )

    def _stop_satellite_worker(self) -> None:
        """Explicitly abort a running bulk-fetch worker.

        Used by the close/abort code paths where the worker is
        *still actively walking the tile list* — i.e. there's a
        pending ``QTimer.singleShot(_fetch_next_tile)`` on the
        worker thread that needs to wind down before we can
        clear our references. For the natural-finish path use
        :meth:`_cleanup_satellite_worker_refs` instead; calling
        this method after ``finished`` has already fired will
        crash with ``RuntimeError: Internal C++ object already
        deleted`` because the ``finished → worker.deleteLater``
        wiring destroys the worker's C++ side on the worker
        thread before this method's queued caller gets a chance
        to run.

        Mirrors :meth:`_stop_vatsim_worker`'s pre-set-the-flag
        pattern. The ``QTimer.singleShot`` chain inside the worker
        sees the flag at the next tile boundary and emits
        ``finished``; the auto-cleanup wiring set up in
        :meth:`_start_satellite_worker` then routes through
        ``worker.deleteLater`` (on the worker thread) and
        ``thread.quit`` so the worker is destroyed on its own
        thread before the loop exits.

        We do NOT call ``thread.quit()`` ourselves any more —
        that's the auto-wiring's job, and double-quitting would
        racing the deferred-delete event ahead of the quit event
        in the worker's queue, which is exactly what we need to
        kill the worker's pending timers on the right thread.

        Likewise we don't manually ``deleteLater`` the worker or
        thread; both are wired through ``finished`` already.
        """
        if self._satellite_thread is None:
            return
        worker = self._satellite_worker
        if worker is not None:
            # Belt-and-braces: between this method's caller and
            # the actual worker access below, the worker's
            # ``finished`` signal may have fired (e.g. completion
            # at the very same instant the user clicked Close).
            # Catch the resulting RuntimeError rather than letting
            # it propagate — by the time we'd be raising, the
            # worker has already destroyed itself anyway, which
            # is the outcome we wanted.
            try:
                # Setting ``_stopped`` from the GUI thread is a
                # write to a bool — safe under the GIL — and
                # short-circuits the next iteration of the chain
                # even before the queued ``stop_fetch`` slot
                # lands.
                worker._stopped = True  # noqa: SLF001
                QMetaObject.invokeMethod(
                    worker,
                    "stop_fetch",
                    Qt.ConnectionType.QueuedConnection,
                )
            except RuntimeError:
                pass
        # 30 s covers the worst case of a tile fetch hung at the
        # full HTTP timeout (15 s default) plus a margin for the
        # final state-file write to complete.
        self._satellite_thread.wait(30_000)
        # Drop our references; Qt destruction is in-flight via
        # the auto-cleanup wiring set up in _start_satellite_worker.
        self._satellite_worker = None
        self._satellite_thread = None

    def _cleanup_satellite_worker_refs(self) -> None:
        """Clear Python references to the bulk-fetch worker after
        its ``finished`` signal has fired.

        Distinct from :meth:`_stop_satellite_worker`: the worker
        has *already* self-destructed via the
        ``finished → worker.deleteLater`` wiring on its own
        thread, so the C++ Qt object is gone. Any access to it
        from here (``QMetaObject.invokeMethod``, attribute reads
        going through shiboken, etc.) raises ``RuntimeError:
        Internal C++ object already deleted`` — which is the
        crash the user reported at 4423/5127 on the z=13 fetch.

        We just wait briefly for the thread to finish winding
        down (``thread.quit`` is wired DirectConnection on the
        ``finished`` chain in
        :meth:`_start_satellite_worker_for_zoom`, so by the
        time this slot runs on the GUI thread the worker
        thread's event loop has already been told to exit and
        is either gone or finishing up within a few ms), then
        clear our local refs.

        The wait is bounded at 2 s for safety; with the
        DirectConnection wiring the wait should always be
        sub-millisecond, but a generous cap avoids us hanging
        if something exotic (e.g. a future addition to the
        worker that holds a kernel resource) delays exit.
        Before the DirectConnection fix this same wait was
        ``thread.wait(30_000)`` and *deadlocked* during chain
        transitions: ``thread.quit`` was queued to the GUI
        thread behind our own ``_on_satellite_finished`` slot,
        so the wait couldn't succeed until the 30 s timeout
        elapsed — three back-to-back cached zooms then froze
        the UI for ~90 s before the actual z=15 fetch could
        start. Direct-connected thread.quit broke the deadlock;
        the now-trivial wait gets a small, fixed budget.
        """
        thread = self._satellite_thread
        self._satellite_worker = None
        self._satellite_thread = None
        if thread is None:
            layout_diag.log("satellite.cleanup", outcome="no_thread")
            return
        try:
            waited = thread.wait(2_000)
            layout_diag.log(
                "satellite.cleanup",
                outcome="waited",
                wait_succeeded=bool(waited),
            )
        except RuntimeError:
            # Thread itself may have been deleteLater'd already
            # (``thread.finished → thread.deleteLater``); shiboken
            # raises on access. Same outcome we wanted: nothing
            # left to do.
            layout_diag.log(
                "satellite.cleanup",
                outcome="thread_already_deleted",
            )

    def _ensure_satellite_demand_worker(self) -> OnDemandFetchWorker:
        """Lazy-start the on-demand fetch worker on its own QThread.

        Idempotent: re-calls return the existing worker. Started
        the first time the visibility walk reports a visible miss
        — i.e. the user is actively looking at an uncached tile.
        Users who never enable satellite mode never spin up the
        worker / thread, paying zero cost.

        Returns the worker so the caller can immediately enqueue.
        """
        if self._satellite_demand_worker is not None:
            return self._satellite_demand_worker
        cache = self._satellite_tile_cache()
        thread = QThread(self)
        worker = OnDemandFetchWorker(
            tile_cache=cache,
            url_template=ESRI_WORLD_IMAGERY_TEMPLATE,
            user_agent=SATELLITE_USER_AGENT,
        )
        worker.moveToThread(thread)
        # The worker doesn't need a ``started`` hook: it sits
        # idle until ``enqueue`` is invoked. ``request_stop`` /
        # ``cancel_pending`` likewise route through queued slot
        # invocation. So we just start the thread and let it run.
        worker.tile_ready.connect(self._on_satellite_demand_tile_ready)
        worker.tile_failed.connect(self._on_satellite_demand_tile_failed)
        # Cross-thread enqueue path: the GUI thread emits
        # ``_satellite_enqueue_tile(coord)``, Qt's signal machinery
        # marshals the call onto the worker thread's event loop
        # via the explicit queued connection. This replaces an
        # earlier ``QMetaObject.invokeMethod`` + ``Q_ARG(object,
        # coord)`` call which fails on recent PySide6 with
        # ``RuntimeError: qArgDataFromPyType: Unable to find a
        # QMetaType for "object"`` — those builds tightened the
        # metatype-lookup path and no longer accept the bare
        # ``object`` type. Signal-based queuing handles Python
        # objects via ``PyObject*`` internally and side-steps the
        # registration requirement entirely.
        self._satellite_enqueue_tile.connect(
            worker.enqueue, Qt.ConnectionType.QueuedConnection
        )
        # Shutdown chain — see ``_stop_satellite_demand_worker``
        # for the rationale. Connecting ``finished`` to both the
        # thread's quit and the worker's deleteLater (in that
        # order) guarantees the worker is destroyed *on its own
        # thread* before the event loop exits. Without this,
        # pending ``QTimer.singleShot`` self-callbacks would
        # later be killed from the GUI thread, producing the
        # ``QObject::killTimer: Timers cannot be stopped from
        # another thread`` warning. ``thread.finished`` →
        # ``thread.deleteLater`` then cleans the QThread up
        # safely from the GUI thread (it's already done its job).
        worker.finished.connect(worker.deleteLater)
        # ``thread.quit`` is wired DirectConnection (not
        # AutoConnection) so it runs immediately on the worker
        # thread instead of being queued behind GUI-thread slots.
        # See the matching comment in
        # ``_start_satellite_worker_for_zoom`` for the full
        # write-up; same deadlock risk applies here on the
        # ``_stop_satellite_demand_worker`` polite-wait path.
        worker.finished.connect(
            thread.quit, Qt.ConnectionType.DirectConnection
        )
        thread.finished.connect(thread.deleteLater)
        thread.start()
        self._satellite_demand_thread = thread
        self._satellite_demand_worker = worker
        return worker

    def _stop_satellite_demand_worker(self) -> None:
        """Polite shutdown of the on-demand fetch worker.

        ``request_stop`` is queued onto the worker thread; the
        worker emits ``finished`` immediately on receipt, which
        is wired to the worker's ``deleteLater`` *and* the
        thread's ``quit``. The worker self-destructs on its own
        thread (killing its pending timers from the right
        thread), then the event loop exits, then ``wait()``
        returns and Qt cleans the QThread up via
        ``thread.finished`` → ``thread.deleteLater``.

        We deliberately do *not* call ``deleteLater`` ourselves
        here — those calls are already wired via ``finished``;
        an extra one would post a duplicate event that fires
        after the object is gone (no-op but ugly).
        """
        if self._satellite_demand_thread is None:
            return
        worker = self._satellite_demand_worker
        if worker is not None:
            # Disconnect the enqueue signal before requesting stop:
            # any in-flight visibility update that emits between
            # here and the worker's actual destruction would otherwise
            # post one last queued enqueue onto a soon-to-be-deleted
            # worker. Qt would handle it gracefully (queued events
            # on a dead receiver are dropped) but the disconnect
            # makes the intent explicit and saves a frame of work
            # we'd just throw away.
            try:
                self._satellite_enqueue_tile.disconnect(worker.enqueue)
            except (RuntimeError, TypeError):
                # ``disconnect`` raises when no such connection
                # exists — possible if ``_ensure_satellite_demand_worker``
                # never reached the connect line (e.g. constructor
                # raised mid-init). Swallow; the goal here is "be
                # sure we're disconnected", not "verify we were".
                pass
            QMetaObject.invokeMethod(
                worker,
                "request_stop",
                Qt.ConnectionType.QueuedConnection,
            )
        # 15 s covers the worst case of a single in-flight HTTP
        # request hung at the demand-worker timeout (8 s) plus a
        # margin. The throttle interval (200 ms) is irrelevant —
        # ``request_stop`` immediately emits ``finished`` and
        # short-circuits the loop.
        self._satellite_demand_thread.wait(15_000)
        # Drop our references; the actual Qt destruction is
        # already in flight via the wiring above.
        self._satellite_demand_worker = None
        self._satellite_demand_thread = None

    def _stop_workers_for_shutdown(
        self, *, polite_timeout_ms: int = 1500, force_timeout_ms: int = 500
    ) -> None:
        """Force-stop every worker thread, in parallel, with a hard
        time budget.

        Distinct from the individual ``_stop_*_worker`` methods
        because those are designed for *in-session* tear-downs
        (user toggles a feature off; we want their work
        persisted cleanly, so a 12-30 s polite wait is fine).
        At *shutdown* the user clicked the red X and expects the
        window gone in well under a second. Stacking the three
        polite waits sequentially could otherwise stall the
        close by up to ~57 s (12 + 30 + 15).

        Algorithm:

        1. **Signal everyone first.** Every worker has a fast
           "set ``_stopped`` + queue stop slot" path that returns
           immediately; we call them all back-to-back without
           waiting. The queued stop slots then run on each
           respective worker thread in parallel.
        2. **Wait briefly on all threads in parallel.** Each
           ``QThread.wait(polite_timeout_ms)`` blocks the GUI
           thread, but we wait at most ``polite_timeout_ms``
           total across all threads (not per-thread, because by
           the time we get to thread N the earlier threads have
           already had ``polite_timeout_ms`` to finish — they're
           either done or they're stuck in I/O and won't
           finish in time anyway).
        3. **Terminate stragglers.** ``QThread.terminate()`` is
           normally dangerous (leaves resources in inconsistent
           state) but acceptable here: app is shutting down, and
           the cache's tmp-file + atomic-rename write discipline
           limits worst-case blast radius to a single ``.tmp``
           file left behind, which the next launch ignores (the
           non-tmp file either exists, in which case the tmp is
           overwritten on next put, or doesn't, in which case
           ``cache.has`` returns False and the tile is just
           re-fetched).

        Why the warning the user reported (``QThread: Destroyed
        while thread '' is still running``) was happening: the
        previous sequential-stops path waited on the bulk worker
        for up to 30 s while it was sitting in a blocking
        ``urllib.request.urlopen`` call. Even when the wait
        eventually returned, the QThread object's destruction
        chain raced against MainWindow's teardown — and if Qt's
        parent-chain cleanup reached the QThread before its OS
        thread had fully unwound, that's the warning. The hard
        terminate here removes the race entirely.

        Args:
            polite_timeout_ms: How long (total) to give workers
                to finish gracefully before falling back to
                terminate. Default 1500 ms — enough for the
                "between tiles" case (worker checks
                ``_stopped`` at the top of every iteration), not
                enough to wait out a stuck HTTP request.
            force_timeout_ms: How long to wait after
                ``terminate()`` before considering the thread
                actually gone. Default 500 ms — terminate is
                effectively synchronous on Windows
                (``TerminateThread``) and Linux
                (``pthread_cancel`` + immediate handler), so
                this is mostly a safety margin.
        """
        # Phase 1: signal everyone to stop, no waits.
        for signal in (
            self._signal_vatsim_worker_stop,
            self._signal_satellite_bulk_worker_stop,
            self._signal_satellite_demand_worker_stop,
        ):
            try:
                signal()
            except Exception:  # noqa: BLE001 — shutdown must never raise.
                # Signal failures here just mean we'll hit the
                # terminate fallback for that worker; better than
                # propagating the exception out of closeEvent.
                pass

        # Phase 2 + 3: wait briefly on every thread, then
        # terminate any straggler still running. Delegated to
        # ``_force_stop_threads`` so the time-bound logic is
        # testable in isolation from MainWindow.
        _force_stop_threads(
            [
                self._vatsim_thread,
                self._satellite_thread,
                self._satellite_demand_thread,
            ],
            polite_timeout_ms=polite_timeout_ms,
            force_timeout_ms=force_timeout_ms,
        )
        self._vatsim_thread = None
        self._satellite_thread = None
        self._satellite_demand_thread = None
        # Clear worker refs too — the Qt-side ``deleteLater``
        # wiring on ``finished`` will (or has) handled the actual
        # destruction; we just drop our Python handles so any
        # stray method call would NameError instead of poking
        # a freed object.
        self._vatsim_worker = None
        self._satellite_worker = None
        self._satellite_demand_worker = None

    def _signal_vatsim_worker_stop(self) -> None:
        """Non-blocking 'tell the VATSIM worker to stop' — used by
        :meth:`_stop_workers_for_shutdown`. Sets the stop flag
        and queues the stop slot, then returns immediately
        without waiting. Idempotent + no-op when the worker
        isn't running.

        Does **not** call ``self._vatsim_thread.quit()`` here:
        ``stop_polling`` emits ``finished`` once the QTimer has
        been torn down on the worker thread, and the wired
        ``finished → thread.quit`` DirectConnection sets the
        quit flag from inside the worker thread itself. Calling
        ``quit()`` here would race the queued ``stop_polling``
        event dispatch (the loop's quit-flag check would beat
        event processing) and leak the QTimer onto a dead
        thread — see ``VatsimWorker.stop_polling``'s rationale
        for the full failure mode.
        """
        worker = self._vatsim_worker
        if worker is None:
            return
        try:
            worker._stopped = True  # noqa: SLF001
            QMetaObject.invokeMethod(
                worker,
                "stop_polling",
                Qt.ConnectionType.QueuedConnection,
            )
        except RuntimeError:
            # C++ object already gone — fine, that's the goal.
            pass

    def _signal_satellite_bulk_worker_stop(self) -> None:
        """Non-blocking 'tell the satellite bulk worker to stop' —
        sets the stop flag and queues ``stop_fetch``, then
        returns. Idempotent + no-op when the worker isn't
        running."""
        worker = self._satellite_worker
        if worker is None:
            return
        try:
            worker._stopped = True  # noqa: SLF001
            QMetaObject.invokeMethod(
                worker,
                "stop_fetch",
                Qt.ConnectionType.QueuedConnection,
            )
        except RuntimeError:
            pass

    def _signal_satellite_demand_worker_stop(self) -> None:
        """Non-blocking 'tell the on-demand worker to stop' —
        disconnects the enqueue signal and queues
        ``request_stop``, then returns. Idempotent + no-op when
        the worker isn't running."""
        worker = self._satellite_demand_worker
        if worker is None:
            return
        try:
            try:
                self._satellite_enqueue_tile.disconnect(worker.enqueue)
            except (RuntimeError, TypeError):
                pass
            QMetaObject.invokeMethod(
                worker,
                "request_stop",
                Qt.ConnectionType.QueuedConnection,
            )
        except RuntimeError:
            pass

    @Slot(object)
    def _on_satellite_demand_tile_ready(self, coord: object) -> None:
        """Queue a refresh for the just-fetched on-demand coord.

        Per-tile refreshes were the source of the user-reported
        UI jank: every on-demand fetch fired a separate
        ``refresh_from_cache`` which re-decoded the tile,
        called ``setPixmap``, and triggered a Qt repaint —
        ~200 of those in quick succession (a single visibility
        sweep's worth of cache misses) blocked the GUI thread
        for 1-2 s. We instead queue the coord and let the
        debounced ``_drain_satellite_refresh_queue`` slot batch
        them into one ``refresh_from_cache`` call per overlay,
        cutting the per-batch overhead by roughly the batch
        size. See :meth:`_queue_satellite_tile_refresh` for the
        coalescing logic.
        """
        if not isinstance(coord, TileCoord):
            return
        self._queue_satellite_tile_refresh(coord)
        # Bump per-zoom completed for on-demand fetches too so
        # the status bar tracks every tile that lands on disk,
        # not just bulk-worker tiles. Cheap; just a dict update.
        entry = self._sat_progress_per_zoom.get(coord.z)
        if entry is not None and not entry.get("done", False):
            completed = int(entry.get("completed", 0)) + 1
            total = int(entry.get("total", 0))
            entry["completed"] = min(completed, total) if total > 0 else completed
            self._refresh_satellite_progress_label()

    @Slot(object, str)
    def _on_satellite_demand_tile_failed(
        self, coord: object, message: str
    ) -> None:
        """Log on-demand fetch failures.

        We don't surface these to the user: a failed on-demand
        fetch leaves the placeholder up, which is already the
        right UX. The status bar would just churn through error
        messages on a flaky network. Logged for diagnostic
        purposes.
        """
        if isinstance(coord, TileCoord):
            _LOG.debug(
                "On-demand fetch failed for %s: %s", coord, message
            )

    @Slot(int, int)
    def _on_satellite_progress(self, completed: int, total: int) -> None:
        """Record per-zoom progress and refresh the status-bar
        label.

        The label now shows every configured zoom level at once
        (``z=12 1,330 ✓ · z=13 4,423 / 5,127 (86 %) · z=14 …``)
        so the user can see the whole multi-zoom chain's state
        rather than just whichever zoom is currently running.
        Live updates only mutate the running zoom's entry; the
        others stay at their initial-state-from-cache value
        (populated by :meth:`_seed_satellite_progress_per_zoom`)
        until the chain's worker reaches them.
        """
        z = self._satellite_running_zoom
        if z:
            entry = self._sat_progress_per_zoom.setdefault(
                z, {"completed": 0, "total": 0, "done": False}
            )
            entry["completed"] = int(completed)
            # Worker may revise total mid-run if Esri returns 404
            # for tiles outside its coverage. Track the latest.
            if total > 0:
                entry["total"] = int(total)
        self._refresh_satellite_progress_label()

    def _refresh_satellite_progress_label(self) -> None:
        """Render :attr:`_sat_progress_per_zoom` to the status bar.

        Empty / not-yet-seeded zooms are skipped; an all-empty
        map shows ``"Satellite: idle"``. Format per zoom:

        * Fully done   → ``z=12 1,330 ✓``
        * Mid-fetch    → ``z=13 4,423 / 5,127 (86 %)``
        * Untouched    → ``z=14 — / 20,240``

        Joined with ``·`` separators for a compact single line
        that fits the status bar without truncation at typical
        window widths. Bar shown iff at least one zoom has any
        signal recorded.
        """
        if self._sat_progress_label is None:
            return
        if not self._sat_progress_per_zoom:
            self._sat_progress_label.setText("Satellite: idle")
            self._sat_progress_label.setVisible(True)
            return
        chunks: list[str] = []
        # Sort ascending so the natural reading order is
        # coarse → fine, matching the download chain order
        # after the post-fix bulk reordering.
        for z in sorted(self._sat_progress_per_zoom.keys()):
            entry = self._sat_progress_per_zoom[z]
            completed = int(entry.get("completed", 0))
            total = int(entry.get("total", 0))
            done = bool(entry.get("done", False))
            if done:
                # Checkmark ahead of the number reads as "this
                # zoom is finished, here's how many tiles it
                # holds" — the count is informational only at
                # this point, not a progress fraction.
                chunks.append(f"z={z} ✓ {completed:,} tiles")
            elif total > 0:
                pct = 100.0 * completed / total
                chunks.append(
                    f"z={z} {completed:,} / {total:,} tiles ({pct:.0f} %)"
                )
            else:
                chunks.append(f"z={z} — tiles")
        self._sat_progress_label.setText(
            "Satellite: " + " · ".join(chunks)
        )
        self._sat_progress_label.setVisible(True)

    def _seed_satellite_progress_per_zoom(self) -> None:
        """Populate :attr:`_sat_progress_per_zoom` from the cache.

        Called when the satellite toggle goes on, or whenever the
        chart finishes loading after a session-start resume. For
        every configured zoom we compute the bbox total (cheap
        arithmetic) and ask the cache how many of those tiles are
        already on disk; ``done = (cached == total)`` gives us an
        accurate starting point before any worker signal fires.

        This is what makes the status-bar readout meaningful on
        a launch where the user already has z=12 fully cached
        but z=13 / z=14 are still being filled — without seeding
        we'd show ``z=12 —`` until the worker happens to start
        on z=12, which would be confusing.

        Performance: :func:`count_cached_tiles_in_bbox` batches
        directory enumeration via :func:`os.scandir` instead of
        the per-tile :meth:`TileCache.has` (one ``stat`` syscall
        each) that the old implementation used. At the four-zoom
        default that's ~200 syscalls instead of ~107k — the
        difference between "essentially instant" and the 5-30 s
        "Not Responding" startup window the user reported when
        satellite view was on from a prior session.
        """
        cache = self._satellite_tile_cache()
        min_lat, max_lat, min_lon, max_lon = ISRAEL_BBOX
        for z in self._satellite_zoom_levels():
            try:
                total = count_tiles_for_bbox(
                    min_lat=min_lat,
                    max_lat=max_lat,
                    min_lon=min_lon,
                    max_lon=max_lon,
                    z=z,
                )
                completed = count_cached_tiles_in_bbox(
                    cache=cache,
                    min_lat=min_lat,
                    max_lat=max_lat,
                    min_lon=min_lon,
                    max_lon=max_lon,
                    z=z,
                )
            except Exception:
                # Cache layout error or bbox helper failure —
                # leave this zoom unseeded; the worker's
                # ``progress`` signal will fill it in later.
                continue
            # ``count_cached`` can in principle exceed ``total`` if
            # the cache holds an out-of-bbox file that happens to
            # land inside the bbox range after a chart-bbox edit —
            # ``min(...)`` keeps the progress fraction sane.
            completed = max(0, min(completed, total))
            self._sat_progress_per_zoom[z] = {
                "completed": completed,
                "total": total,
                "done": total > 0 and completed >= total,
            }
        self._refresh_satellite_progress_label()

    def _mark_zoom_progress_done(self, z: int) -> None:
        """Stamp a zoom level as fully complete in the per-zoom
        progress map.

        Called from :meth:`_on_satellite_finished` after a
        worker's natural completion (we know the worker walked
        through every queued tile because it didn't bail via
        ``failed``). Idempotent — re-calls for an already-done
        zoom are a no-op except for label refresh, which is
        cheap.
        """
        if not z:
            return
        entry = self._sat_progress_per_zoom.setdefault(
            z, {"completed": 0, "total": 0, "done": False}
        )
        # Pin completed to total so the label reads as ``done``
        # even if the worker emitted finished without a final
        # ``progress`` flush (rare but possible at the tail of
        # a 404-heavy zoom where every remaining tile was an
        # ``http_skip``).
        total = int(entry.get("total", 0))
        if total > 0:
            entry["completed"] = total
        entry["done"] = True
        self._refresh_satellite_progress_label()

    def _queue_satellite_tile_refresh(self, coord: TileCoord) -> None:
        """Append ``coord`` to the debounce buffer; (re)start timer.

        The single-shot timer means the buffer drains
        :data:`SATELLITE_REFRESH_DEBOUNCE_MS` after the *last*
        enqueue, not the first — so a steady stream of arriving
        tiles coalesces into one batch at the end of the burst,
        which is the smoothness behaviour we want. A long
        steady stream (e.g. bulk fetch on a fast connection)
        does eventually drain mid-stream because Qt timers
        aren't refreshed by ``start()`` of an already-active
        timer the way some other event loops do — wait, that's
        wrong, ``QTimer.start()`` *does* reset the interval —
        but at sustained arrival rates above 1 / interval the
        timer never gets a chance to fire because every
        enqueue restarts it. That's fine: when the burst
        stops the next enqueue's timer fires normally, and in
        the meantime the cap below keeps the buffer bounded.
        """
        if not isinstance(coord, TileCoord):
            return
        self._pending_satellite_refresh.append(coord)
        if len(self._pending_satellite_refresh) > SATELLITE_REFRESH_QUEUE_CAP:
            # Trim from the front: the oldest coords are most
            # likely to have been overwritten by the user
            # scrolling away. Bounded GUI-thread work is more
            # important than perfect refresh ordering.
            overflow = (
                len(self._pending_satellite_refresh)
                - SATELLITE_REFRESH_QUEUE_CAP
            )
            del self._pending_satellite_refresh[:overflow]
        # ``start`` on an active single-shot timer restarts the
        # interval; effectively "this is the freshest signal,
        # wait another N ms for stragglers".
        self._satellite_refresh_timer.start()

    @Slot()
    def _drain_satellite_refresh_queue(self) -> None:
        """Drain the queued coords into one ``refresh_from_cache``
        call per overlay.

        Bulk-mode refresh is significantly cheaper than per-tile
        refresh per coord at high arrival rates — the overlay
        partitions the coords by zoom internally and walks each
        per-zoom items dict once, vs walking and looking up
        every per-coord signal separately. The drain is wired
        to a single-shot timer rather than directly to
        ``tile_ready`` / ``tile_fetched`` to keep the
        coalescing window small enough that users still see
        tiles arrive in near-real-time as they pan.

        Safe to invoke with an empty queue (no-op).
        """
        if not self._pending_satellite_refresh:
            return
        # Move-and-clear pattern: capture the current queue into
        # ``batch`` before invoking the (potentially long-
        # running) refresh, so any tile signals that arrive
        # mid-drain go to a *fresh* queue and aren't lost / re-
        # walked. Net effect: each coord is refreshed once
        # exactly, regardless of timer racing.
        batch = self._pending_satellite_refresh
        self._pending_satellite_refresh = []
        for ov in (self._north_sat_overlay, self._south_sat_overlay):
            if ov is not None:
                ov.refresh_from_cache(only_coords=batch)

    @Slot(object)
    def _on_satellite_tile_fetched(self, coord: object) -> None:
        """Queue a debounced refresh for the just-fetched bulk
        tile.

        Bulk-fetch can deliver tiles at >50 Hz on a fast
        connection — firing ``refresh_from_cache`` per signal
        meant the GUI thread spent most of its time decoding
        JPEGs + calling ``setPixmap``, which is the visible
        jank the user reported when satellite imagery is
        loading. Queue + debounce instead: collect coords for
        ~30 ms, then refresh all overlays once. See
        :meth:`_queue_satellite_tile_refresh`.

        Overlays are nullable because the bulk worker can run
        before the chart finishes loading (cache prep on app
        start), in which case we just queue tiles for a later
        refresh — :meth:`_on_show_satellite_toggled` and
        :meth:`_on_map_finished` both call
        ``refresh_from_cache()`` to catch up.
        """
        # ``coord`` arrives via Qt's queued signal as ``object``
        # (the worker emits ``Signal(object)`` to avoid registering
        # a custom Qt type for the dataclass). Cast at the call
        # boundary; a bad coord is silently ignored downstream.
        if not isinstance(coord, TileCoord):
            return
        self._queue_satellite_tile_refresh(coord)

    @Slot()
    def _on_satellite_finished(self) -> None:
        """A bulk-fetch worker emitted ``finished`` — chain to the
        next pending zoom or finalise the multi-zoom run.

        ``finished`` covers both "downloaded everything for this
        zoom" and "user paused mid-fetch". The persisted
        DownloadState distinguishes the two for the *primary*
        zoom; secondaries don't persist state, so we treat their
        finish as "done" unconditionally (any tile we missed will
        come back through on-demand fetch later).
        """
        # Read state *before* we tear the worker down — afterwards
        # the worker reference is None and we'd lose the diagnostic.
        cache = self._satellite_tile_cache()
        state = read_download_state(cache)
        was_complete = state is not None and state.is_complete()
        finished_zoom = self._satellite_running_zoom
        layout_diag.log(
            "satellite.worker_finished",
            zoom=finished_zoom,
            was_complete=was_complete,
            pending_chain_len=len(self._satellite_pending_zoom_chain),
        )

        # The worker has emitted ``finished`` — by the time this
        # GUI-thread slot runs, the ``finished → deleteLater``
        # wiring set up in ``_start_satellite_worker_for_zoom`` has
        # almost certainly already destroyed the worker's C++ side
        # on the worker thread. We must *not* call the worker (or
        # ``QMetaObject.invokeMethod`` against it) here — that's
        # what produced the ``RuntimeError: Internal C++ object
        # (SatelliteWorker) already deleted`` crash the user
        # reported. ``_cleanup_satellite_worker_refs`` only waits
        # for the thread to fully exit and clears our Python refs;
        # no worker access.
        self._cleanup_satellite_worker_refs()
        # Stash the just-finished zoom's progress as done before
        # the chain reseeds ``_satellite_running_zoom`` for the
        # next link; ensures the status-bar readout reflects the
        # transition (z=12 ✓, z=13 starting, ...) even in the
        # tight window between finished and the next start.
        self._mark_zoom_progress_done(finished_zoom)
        # Final overlay refresh — picks up any tiles the worker
        # wrote in its last few iterations before signalling
        # finished. Only when sat view is currently ON, though:
        # with sat view OFF the per-zoom overlays' tile items
        # are invisible *and* their internal
        # ``_last_visible_rect`` is ``None``, which makes
        # ``refresh_from_cache(None)`` walk every single tile
        # item and call ``TileCache.get`` on each (the lazy-load
        # loop's "no visible rect known yet, conservatively
        # load everything" branch). On the default
        # ``[12, 13, 14, 15]`` set that's ~213 k cache reads
        # per chain transition × two sheets — multi-second
        # GUI-thread freeze for *invisible* work. When sat view
        # ON, the same refresh is cheap because the visible-rect
        # filter rules out everything outside the current
        # viewport. The next time the user toggles sat view ON,
        # :meth:`_on_show_satellite_toggled` does a full refresh
        # via :meth:`SatelliteOverlay.eager_load_all_cached`
        # which catches up any tiles we skipped here.
        if self._act_show_satellite.isChecked():
            for ov in (self._north_sat_overlay, self._south_sat_overlay):
                if ov is not None:
                    ov.refresh_from_cache()

        # Chain to the next pending zoom level, if any. Each link
        # carries its own persist flag (set by
        # :func:`_plan_satellite_zoom_chain`); for the default
        # ``[12, 13, 14, 15]`` set the only persist=True link is
        # the last one (z=15).
        #
        # Crucial detail: we kick off the next link via
        # ``QTimer.singleShot(0, ...)`` rather than calling
        # :meth:`_start_satellite_worker_for_zoom` synchronously.
        # The reason is lifecycle hygiene around the just-finished
        # worker + thread pair:
        #
        # When the previous worker emitted ``finished`` on its
        # worker thread, the signal-slot wiring set up in
        # :meth:`_start_satellite_worker_for_zoom` triggered
        # ``worker.deleteLater`` (queued for the worker thread's
        # own event-loop teardown) and ``thread.quit``
        # (DirectConnection). The thread's event loop then exits
        # and emits ``thread.finished``, which is auto-connected
        # to ``thread.deleteLater`` — *queued onto this very GUI
        # thread* because the QThread object's affinity is the
        # GUI thread (we constructed it as ``QThread(self)``).
        # In other words, by the time this slot runs there is at
        # least one and possibly several teardown events still
        # sitting in the GUI's event queue behind us.
        #
        # If we synchronously construct + ``moveToThread`` +
        # ``start()`` the next worker/thread pair here, we're
        # interleaving the construction of a brand-new
        # QThread + QObject + signal-slot graph with the
        # not-yet-processed destruction of the previous one. On
        # cached-zoom chains (z=12, z=13 fully populated from a
        # prior session) this happens many times per second, and
        # it was correlated with a hard fail-fast crash in
        # Qt6Core.dll (BEX64 ``0xc0000409``) on the v3.3 Windows
        # build right after the chart finished loading. The
        # crash signature is consistent with a Q_ASSERT firing
        # somewhere deep in the dispatcher — exactly the kind of
        # thing this interleaving could plausibly tickle.
        #
        # Hopping through ``QTimer.singleShot(0, ...)`` returns
        # control to the GUI event loop first, which lets the
        # ``thread.deleteLater`` and any other pending teardown
        # events drain before the next pair is constructed. The
        # user-visible effect is one extra event-loop iteration
        # of latency between chain links (microseconds), which
        # is undetectable. The code-hygiene effect is that the
        # rapid create / destroy pattern is broken up into
        # clean, non-overlapping phases — which is what the Qt
        # threading model assumes when it asserts on object
        # affinity invariants.
        if self._satellite_pending_zoom_chain:
            next_zoom, next_persist = (
                self._satellite_pending_zoom_chain.pop(0)
            )
            layout_diag.log(
                "satellite.chain_transition_scheduled",
                next_zoom=next_zoom,
                next_persist=next_persist,
                remaining_after=len(self._satellite_pending_zoom_chain),
            )
            QTimer.singleShot(
                0,
                lambda z=next_zoom, p=next_persist: (
                    self._start_satellite_worker_for_zoom(z, persist=p)
                ),
            )
            return

        # Multi-zoom chain finished — surface completion in the
        # status bar. The toast modal only fires when the
        # *finest* (highest) zoom finishes, because under the
        # coarsest-first chain order the finest zoom is also the
        # last link, and "everything is cached now" is the
        # completion event worth toasting. Intermediate links
        # (z=12, z=13, z=14 finishing along the way) update the
        # status bar quietly without a modal.
        levels = self._satellite_zoom_levels()
        finest_zoom = max(levels) if levels else finished_zoom
        if was_complete and finished_zoom == finest_zoom:
            if self._sat_progress_label is not None:
                self._sat_progress_label.setText("Satellite: ready")
            self.statusBar().showMessage(
                "Satellite imagery download complete.", 8000
            )
            # Modal toast only on the very first completion (when
            # the user actually waited for it). On a "fully cached
            # already, walked the bbox in 2 s and finished" path
            # the worker never emits any progress events worth
            # toasting, so check that we actually fetched something
            # this session before nagging.
            if state is not None and state.completed_at is not None:
                show_completion_toast(self, total_tiles=state.total_tiles)
        elif was_complete:
            # An intermediate chain link finished (typically z=12
            # or z=13 or z=14) — quiet status bar update, no
            # toast; the chain is still rolling toward the finest
            # zoom.
            if self._sat_progress_label is not None:
                self._sat_progress_label.setText("Satellite: ready")
        else:
            if self._sat_progress_label is not None:
                self._sat_progress_label.setText("Satellite: paused")

    @Slot(str)
    def _on_satellite_failed(self, message: str) -> None:
        """Worker bailed with an unrecoverable error — surface it
        and tear down. Most common cause is a disk-space error on
        the cache write.

        Unlike the natural-finish path, the worker is still
        *alive* here: the worker's ``failed`` branch returns
        without rescheduling a tile-fetch iteration, but it
        doesn't emit ``finished`` either. The
        ``finished → deleteLater`` auto-cleanup chain therefore
        never fires; if we routed through
        :meth:`_stop_satellite_worker` we'd queue ``stop_fetch``
        (a no-op given there's no pending iteration), then sit on
        ``thread.wait(30_000)`` for the full timeout — a 30 s
        GUI-thread freeze on top of an already user-visible
        failure. Tear down explicitly instead: quit the thread,
        delete the worker, clear refs.
        """
        layout_diag.log(
            "satellite.worker_failed",
            zoom=self._satellite_running_zoom,
            message=message,
        )
        worker = self._satellite_worker
        thread = self._satellite_thread
        self._satellite_worker = None
        self._satellite_thread = None
        if worker is not None:
            # ``deleteLater`` on the GUI thread schedules the
            # destruction event on the *worker* thread's event
            # loop (because that's where the worker lives), so
            # we won't get the "timers stopped from another
            # thread" warning the manual delete would produce.
            try:
                worker.deleteLater()
            except RuntimeError:
                pass
        if thread is not None:
            try:
                thread.quit()
                # 5 s budget — the thread has nothing to wait
                # for (worker bailed without rescheduling), so
                # ``quit`` returns almost immediately. The
                # margin guards against an unrelated long-
                # running event in the thread's queue.
                thread.wait(5_000)
            except RuntimeError:
                pass
        if self._sat_progress_label is not None:
            self._sat_progress_label.setText("Satellite: failed")
        self.statusBar().showMessage(
            f"Satellite imagery error: {message}", 12_000
        )

    @Slot(list)
    def _on_vatsim_pilots_updated(self, pilots: list) -> None:
        """Receive a fresh snapshot from the worker and redraw.

        ``pilots`` is a (possibly empty) list of :class:`Pilot`.
        Empty is the legitimate "nobody flying in Israeli airspace
        right now" state — we still call ``set_pilots`` so the
        overlay clears any pilots from the previous tick.

        Status-bar message on every snapshot helps the user see
        the polling is alive; we keep it short ("VATSIM: N
        pilots") to avoid eating into longer-lived messages.

        Plane tracking: after the overlay rebuild we run the
        recenter pass — this is the only natural cadence (every
        15 s) at which the tracked pilot's position changes, so
        we tie the viewport snap to it. The recenter pass is also
        responsible for the "tracked pilot dropped out of the
        feed" detection: if the previously-tracked callsign isn't
        in the new snapshot it clears tracking and posts the
        appropriate status message, overriding the generic
        "VATSIM: N pilots" line below.
        """
        self._latest_vatsim_pilots = list(pilots)
        # Refresh through the canonical path so calibration /
        # icon-size logic is applied uniformly. ``set_pilots``
        # re-applies the tracking-halo visual at the end.
        self._refresh_traffic_overlay()
        # Snap the viewport to the tracked pilot's new position
        # (or clear tracking with a status message if they
        # disconnected). The recenter call is silent when no
        # tracking is active, so the common case is a cheap
        # early-return.
        self._recenter_on_tracked_pilot()
        n = len(self._latest_vatsim_pilots)
        self.statusBar().showMessage(
            f"VATSIM: {n} pilot{'s' if n != 1 else ''} in Israeli airspace.",
            3000,
        )

    @Slot(str)
    def _on_vatsim_fetch_failed(self, message: str) -> None:
        """Surface a fetch error in the status bar; keep polling.

        The worker handles this transparently — the timer is still
        running, so the next tick will retry. We just tell the
        user *something* went wrong so they can diagnose if the
        overlay stays empty (network down, VATSIM upstream
        outage, etc.).
        """
        self.statusBar().showMessage(f"VATSIM: {message}", 8000)

    def _restore_vatsim_traffic_state_at_startup(self) -> None:
        """If the toggle was saved as ON, kick off the worker now.

        See the caller (the singleShot in __init__) for why we
        defer this rather than firing it inside ``_build_actions``:
        the toolbar action's initial ``setChecked`` runs before
        signals are connected, so the toggled slot never fires
        for the restored value. This method is the explicit
        "match the saved state" hook.

        Safe to call when the toggle is OFF — it just no-ops and
        leaves the hint in its no-traffic form.
        """
        if self._act_show_vatsim_traffic.isChecked():
            self._start_vatsim_worker()
        # Also refresh the map-pane hint so the legend shows up
        # immediately on a fresh launch where traffic-on was
        # previously saved (rather than waiting for the user to
        # toggle hints or some other trigger).
        self._update_map_hint_text()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and self._calibrate_state is not None:
            self._cancel_calibration()
            event.accept()
            return
        super().keyPressEvent(event)

    def try_calibration_click(self, scene_pos: QPointF, event: QMouseEvent) -> bool:
        """Return True if the click was consumed by the calibration workflow."""
        if self._calibrate_state is None:
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        sheet = self._calibrate_state["sheet"]
        if not isinstance(sheet, str) or sheet not in ("north", "south"):
            self._cancel_calibration()
            return True
        mods = event.modifiers()
        ctrl_only = (
            (mods & Qt.KeyboardModifier.ControlModifier)
            and not (mods & Qt.KeyboardModifier.ShiftModifier)
            and not (mods & Qt.KeyboardModifier.AltModifier)
        )
        if ctrl_only:
            return False
        if not self._calibrate_map_input_armed:
            if mods & Qt.KeyboardModifier.ShiftModifier:
                self.statusBar().showMessage(
                    "Calibration: wait a moment after OK, then Shift+click the triangle on the chart.",
                    5000,
                )
            else:
                self.statusBar().showMessage(
                    f"Calibration ({sheet}): after you close the dialog, wait briefly, then "
                    "Shift+click the center of the triangle (Esc to cancel).",
                    6000,
                )
            return True
        if not (mods & Qt.KeyboardModifier.ShiftModifier):
            self.statusBar().showMessage(
                f"Calibration ({sheet}): hold Shift and click the center of the triangle "
                "(Esc to cancel).",
                6000,
            )
            return True
        item = self._sheet_pixmap_item_at(sheet, scene_pos)
        if item is None:
            QMessageBox.warning(
                self,
                "Calibration",
                f"Shift+click on the {sheet} map image (not the other sheet).",
            )
            return True
        local = item.mapFromScene(scene_pos)
        br = item.boundingRect()
        if br.width() <= 0 or br.height() <= 0:
            return True
        u = local.x() / br.width()
        v = local.y() / br.height()
        raw_uvs = self._calibrate_state.get("uvs")
        uvs = raw_uvs if isinstance(raw_uvs, list) else []
        self._calibrate_state["uvs"] = uvs
        uvs.append((u, v))
        self._calibrate_map_input_armed = False
        self._hide_calibration_overlay()
        anchors = self._calibrate_state.get("anchors")
        anchor_count = len(anchors) if isinstance(anchors, tuple) else 0
        if len(uvs) < anchor_count:
            QTimer.singleShot(0, self._prompt_calibration_step)
            return True
        if len(uvs) == anchor_count and anchor_count >= MIN_ANCHORS:
            self._finalize_auto_anchor_calibration()
            return True
        self._calibrate_state["uvs"] = []
        return True

    # ------------------------------------------------------------------
    # Route building (Shift+left = add nearest WP, Shift+right = remove nearest)
    # ------------------------------------------------------------------

    def try_route_click(self, scene_pos: QPointF, event: QMouseEvent) -> bool:
        """Handle Shift+left/right clicks for route management.

        Returns True if the click was consumed (so the view should not propagate it).
        Does *nothing* during calibration — route clicks must not race the anchor
        capture flow. Both add and remove fall back to a clear status-bar message when
        the chart isn't calibrated, instead of silently dropping the click."""
        if self._calibrate_state is not None:
            return False
        mods = event.modifiers()
        if not (mods & Qt.KeyboardModifier.ShiftModifier):
            return False
        is_left = event.button() == Qt.MouseButton.LeftButton
        is_right = event.button() == Qt.MouseButton.RightButton
        if not (is_left or is_right):
            return False

        located = self._scene_pos_to_lat_lon(scene_pos)
        if located is None:
            self.statusBar().showMessage(
                "Route: calibrate at least one chart sheet before adding waypoints.",
                4000,
            )
            return True
        _sheet_id, lat, lon = located

        if is_left:
            wp = self._nearest_waypoint_to(lat, lon, _ROUTE_ADD_SNAP_NM)
            if wp is not None:
                if self._route.append_waypoint(wp):
                    self.statusBar().showMessage(
                        f"Route: added {wp.code} ({len(self._route)} point"
                        f"{'s' if len(self._route) != 1 else ''}).",
                        4000,
                    )
                else:
                    self.statusBar().showMessage(
                        f"Route: {wp.code} is already the last waypoint — skipped.",
                        3000,
                    )
                self._refresh_route_panel()
                return True

            # No real waypoint within the snap radius — treat the click as an
            # intermediate polyline sub-point. Intermediates anchor their display
            # name to the *previous* real waypoint (e.g. DAROM.1, DAROM.2, …),
            # so we refuse the very first click in an empty route and tell the
            # user why instead of silently dropping it.
            if self._route.is_empty():
                self.statusBar().showMessage(
                    "Route: add a chart waypoint first; intermediate points are "
                    "named after the previous waypoint (e.g. DAROM.1).",
                    5000,
                )
                return True
            if self._route.append_intermediate(lat, lon):
                last_label = self._route.display_labels()[-1]
                self.statusBar().showMessage(
                    f"Route: added intermediate {last_label} ({len(self._route)} points).",
                    4000,
                )
            else:
                self.statusBar().showMessage(
                    "Route: that intermediate is at the same coordinates as the "
                    "previous point — skipped.",
                    3000,
                )
            self._refresh_route_panel()
            return True

        # Shift+right: remove the route point closest to the click.
        idx = self._route.nearest_index(lat, lon, max_nm=_ROUTE_REMOVE_SNAP_NM)
        if idx is None:
            self.statusBar().showMessage(
                f"Route: no route point within {_ROUTE_REMOVE_SNAP_NM:.0f} nm of the click.",
                4000,
            )
            return True
        # Resolve the label *before* removing so it survives the index shift —
        # we still want to tell the user which point disappeared.
        removed_label = self._route.display_labels()[idx]
        self._route.remove_at(idx)
        self.statusBar().showMessage(
            f"Route: removed {removed_label} ({len(self._route)} point"
            f"{'s' if len(self._route) != 1 else ''}).",
            4000,
        )
        self._refresh_route_panel()
        return True

    def _scene_pos_to_lat_lon(
        self, scene_pos: QPointF
    ) -> tuple[str, float, float] | None:
        """Project a scene point back to ``(sheet_id, lat, lon)`` via whichever sheet's
        calibration covers it. Prefers south when both overlap (south is painted on top
        in the scene, matching the no-modifier sheet-selection logic so the selected
        chart and the resolved waypoint always agree)."""
        for sheet_id in ("south", "north"):
            cal = self._geo_south if sheet_id == "south" else self._geo_north
            item = self._layer_item(sheet_id)
            if cal is None or item is None:
                continue
            br = item.boundingRect()
            if br.width() <= 0 or br.height() <= 0:
                continue
            local = item.mapFromScene(scene_pos)
            u = local.x() / br.width()
            v = local.y() / br.height()
            if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
                continue
            try:
                lon, lat = cal.uv_to_lonlat(u, v)
            except (ValueError, ZeroDivisionError, AssertionError):
                continue
            return (sheet_id, lat, lon)
        return None

    def _nearest_waypoint_to(
        self, lat: float, lon: float, max_nm: float
    ) -> WaypointRecord | None:
        """Find the export-list waypoint closest to ``(lat, lon)``, gated by ``max_nm``.

        Thin wrapper around :func:`cvfr_routemaster.route.find_nearest_waypoint`
        — the actual scan + closer-wins tiebreak logic lives there as a pure
        function so it's exercisable without spinning up Qt or a MainWindow.
        See the snap-radius docs above ``_ROUTE_ADD_SNAP_NM`` for why the
        closer-wins rule is what makes the 1.0 nm radius safe even where two
        waypoints sit < 1.0 nm apart.
        """
        return find_nearest_waypoint(self._waypoints_export, lat, lon, max_nm)

    def _refresh_route_panel(self) -> None:
        """Push the current ``Route`` snapshot into the UI panel and redraw the
        on-map polyline; safe to call from any change site (add, remove, speed
        change). Speed-only changes don't move the line, but redrawing on every
        invocation is cheap and keeps the two views in lockstep without
        bookkeeping in the callers.

        Per-segment altitudes are recomputed from the latest extracted arrows
        and the current calibration — so any change that could shift either
        side (a route edit, a calibration change, an arrow-extraction
        completion, a new sheet load) flows through here uniformly.
        """
        altitudes_per_segment = self._compute_altitudes_for_route()
        self._route_panel.set_route(
            self._route,
            altitudes_per_segment=altitudes_per_segment,
        )
        self._redraw_route_overlay()
        # Traffic overlay sits on the same "anything that could move my
        # anchored items" path as the route polyline (calibration just
        # completed, sheets just moved/scaled, etc.). Refreshing it
        # alongside the route keeps both overlays in lockstep without
        # per-call-site bookkeeping. No-op while the toolbar toggle
        # is off.
        self._refresh_traffic_overlay()

    def _compute_altitudes_for_route(self) -> list[tuple[int, ...]] | None:
        """Project extracted altitude arrows through current calibrations and
        match each segment against them.

        Returns one tuple per segment (in ``self._route.segments()`` order),
        or ``None`` when there's no calibration *and* no arrows yet — the
        route panel reads ``None`` as "controller has nothing to say" and
        defaults every leg to "unknown".

        We deliberately re-project arrows on every call rather than caching
        ``GeoAltitudeArrow`` lists alongside the calibration: calibration
        changes are rare but matter for correctness, and the projection
        itself is sub-millisecond per arrow.

        Matching is route-level (not per-segment) so the matcher can
        competitively assign each arrow to its single best segment —
        without that, an arrow that legitimately labels leg X tends to
        also fit inside leg X+1's tube near the shared waypoint.
        """
        segs = self._route.segments()
        if not segs:
            return None

        if not self._altitude_arrows_north and not self._altitude_arrows_south:
            return None

        geo: dict[str, list[GeoAltitudeArrow]] = {}
        if self._geo_north is not None and self._altitude_arrows_north:
            geo["north"] = project_arrows_to_lonlat(
                self._altitude_arrows_north, self._geo_north,
            )
        if self._geo_south is not None and self._altitude_arrows_south:
            geo["south"] = project_arrows_to_lonlat(
                self._altitude_arrows_south, self._geo_south,
            )

        if not geo:
            # Arrows are extracted but no sheet is calibrated yet — every
            # segment will fall back to "unknown". Returning ``None`` here
            # is equivalent for the panel's rendering, but the empty-list
            # path keeps the contract crisp ("arrows known, nothing matched").
            return [() for _ in segs]

        return match_altitudes_for_route(segs, geo)

    def _on_route_speed_changed(self, _speed: float) -> None:
        """Speed input changed — re-render so the time column reflects the new value
        without altering route geometry."""
        self._refresh_route_panel()

    def _on_clear_route_requested(self) -> None:
        """Drop every point from the route in response to a user-confirmed
        Clear-route press in the panel.

        The panel has already obtained the user's confirmation via its
        own dialog — we trust the signal and proceed unconditionally.
        After clearing, refresh the route panel and redraw the overlay
        so the chart's red polyline and origin marker disappear in the
        same beat as the table empties."""
        self._route.clear()
        self._refresh_route_panel()

    def _on_save_plan_requested(self, plan_text: str) -> None:
        """Save the current ICAO Field 15 route string to a ``.cvfr`` file.

        The panel pre-composes ``plan_text`` (always with intermediates so
        the saved file round-trips exactly), so this handler is just a thin
        file-IO + dialog layer:

        1. Show a native QFileDialog Save-As, parented to the main window,
           defaulting to ``<project_root>/<origin>-<destination>.cvfr``
           (e.g. ``LLIB-LLMZ.cvfr``) per
           :func:`default_save_plan_name`. Edge-case routes (empty,
           all-intermediates, or origin == destination) fall back to a
           single-name or ``flight-plan.cvfr`` form so the dialog always
           opens with a usable suggestion.
        2. Force the ``.cvfr`` extension when the user typed a bare name —
           PySide6's getSaveFileName respects the filter on Windows but
           NOT on Linux / WSL (Qt bug, depends on the native dialog
           backend), so we normalise here to keep behaviour uniform.
        3. Write ``plan_text`` + a single trailing newline as UTF-8 with
           LF line endings; an OS-level write failure surfaces as a
           critical message box (consistent with ``_export_waypoints_csv``).
        4. Status-bar confirmation on success.

        Empty / cancelled dialogs return silently — no popup churn.
        """
        if not plan_text:
            return  # belt-and-braces: panel shouldn't have emitted
        default_name = default_save_plan_name(self._route)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save flight plan",
            str(self._project_root / default_name),
            "CVFR Flight Plan (*.cvfr);;All files (*.*)",
        )
        if not path:
            return
        out = Path(path)
        # If the user typed "myplan" (no extension) under the cvfr filter,
        # Qt on some platforms leaves the path unsuffixed. Force the
        # extension so the friend-facing Load dialog (which defaults to
        # *.cvfr) lists it without the user having to switch filters.
        if out.suffix.lower() != ".cvfr":
            out = out.with_suffix(".cvfr")
        try:
            # newline="\n" forces a single LF terminator on every platform —
            # the file is sent across OSes (WSL/Linux/Windows) and a stray
            # CRLF in the middle would break the strict single-space-
            # between-tokens grammar the parser enforces. Writing LF here
            # matches what ``parse_icao_route_string`` happily strips on
            # the load side, so a round-trip stays clean.
            with open(out, "w", encoding="utf-8", newline="\n") as f:
                f.write(plan_text)
                f.write("\n")
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Save flight plan",
                f"Could not write the flight plan file:\n{exc}",
            )
            return
        self.statusBar().showMessage(f"Saved flight plan to {out}.", 10000)

    def _on_load_plan_requested(self) -> None:
        """Load a flight plan from a ``.cvfr`` file into the current route.

        Validation is strict and atomic: parse + resolve every token first,
        and only mutate the in-memory route once every check has passed.
        Any failure leaves the previously-loaded route untouched, so a
        misclick on Load while a plan is in progress is recoverable as long
        as the failed plan is rejected (the common case for a hand-edited
        file with a typo, or a file from an older waypoint cycle where a
        code no longer resolves).

        Failure modes (each surfaces as a ``QMessageBox.warning``):

        * **File read error** — the OS refused to open / read the chosen
          file (permissions, vanished file, etc.).
        * **Grammar error** — the parser raised :class:`FlightPlanParseError`,
          quoted verbatim with the 1-based token position.
        * **Unknown code** — the parser accepted a 4/5-letter token but no
          such waypoint exists in this build's waypoint database. This
          usually means the saved plan was authored against a newer chart
          cycle than is loaded — the message tells the user which code
          can't be resolved so they can either update their waypoints or
          edit the plan.
        """
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load flight plan",
            str(self._project_root),
            "CVFR Flight Plan (*.cvfr);;All files (*.*)",
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Load flight plan",
                f"Could not read the flight plan file:\n{exc}",
            )
            return
        try:
            tokens = parse_icao_route_string(text)
        except FlightPlanParseError as exc:
            # Surface the parser's message verbatim; it already mentions the
            # 1-based position + offending token text via its formatted
            # ``str()`` form. Adding context here ("flight plan is malformed")
            # frames the situation without burying the parser's exact
            # complaint.
            pos_hint = f" (token #{exc.position})" if exc.position is not None else ""
            QMessageBox.warning(
                self,
                "Load flight plan",
                (
                    f"The flight plan file is malformed and was not loaded"
                    f"{pos_hint}:\n\n{exc}"
                ),
            )
            return

        # Resolution stage: every alphabetic token must map to a
        # WaypointRecord. We do this in a separate pass (not interleaved
        # with parsing) so a structurally-valid file with one unknown code
        # produces a single coherent error message rather than failing
        # midway through a partial route assembly. Coord tokens are already
        # fully resolved by the parser — they carry float lat/lon — so they
        # need no lookup here.
        #
        # The resolved list holds (lat, lon, waypoint_or_None) so the
        # mutation pass below stays a single ``for`` without needing to
        # re-isinstance through the parser's ADT.
        resolved: list[tuple[float, float, WaypointRecord | None]] = []
        for idx, tok in enumerate(tokens, start=1):
            if isinstance(tok, ParsedPlanCode):
                wp = self._lookup_waypoint(tok.code)
                if wp is None:
                    QMessageBox.warning(
                        self,
                        "Load flight plan",
                        (
                            f"Flight plan references waypoint code {tok.code!r} "
                            f"at token #{idx}, but no waypoint with that code "
                            f"is loaded.\n\n"
                            f"This usually means the plan was authored against "
                            f"a different chart cycle. Update your waypoint "
                            f"database (Map File Settings → Load maps & "
                            f"waypoints) or edit the plan to use a known code."
                        ),
                    )
                    return
                resolved.append((wp.lat, wp.lon, wp))
            elif isinstance(tok, ParsedPlanCoord):
                resolved.append((tok.lat, tok.lon, None))
            else:  # pragma: no cover — exhaustive isinstance branch
                # Defensive: parse_icao_route_string only ever returns the
                # two ParsedPlan* subclasses; if a future third kind is added
                # without updating this resolver, fail loud here so the new
                # variant can't silently drop on the floor.
                raise RuntimeError(f"Unexpected parsed token type: {type(tok)!r}")

        # All tokens validated — now mutate. From here on the loader cannot
        # fail in a way that leaves the route half-built; either the previous
        # contents survive untouched (the early-return paths above) or the
        # new contents fully replace them (this block).
        self._route.clear()
        for lat, lon, wp in resolved:
            if wp is not None:
                self._route.append_waypoint(wp)
            else:
                self._route.append_intermediate(lat, lon)
        self._refresh_route_panel()
        self.statusBar().showMessage(
            f"Loaded flight plan from {path} ({len(resolved)} point(s)).", 10000
        )

    def _on_route_point_clicked(self, _label: str, lat: float, lon: float) -> None:
        """Open the configured external map provider for a clicked route-table cell.

        Fires for both real waypoints (green codes) and intermediate sub-points
        (grey ``--> CODE.N`` cells) — the panel emits a uniform
        ``(label, lat, lon)`` tuple so this handler doesn't need to distinguish
        them. The label is informational; only the coordinates drive the URL.

        Mirrors the green-code click behaviour of the master waypoint table on
        the right pane; having the same interaction work in both tables means a
        planned waypoint or polyline sub-point can be researched without
        scrolling back to the master list."""
        open_external_url(
            external_map_url(lat, lon, self._current_map_link_provider())
        )

    def _on_route_reporting_name_clicked(self, code: str) -> None:
        """Centre the map on a route-table Reporting cell's waypoint.

        The route panel only emits this for cells backed by a real waypoint, so
        the lookup in ``_waypoints_export`` should always find a match. We
        defend against the import-list-out-of-sync case anyway with a status-bar
        fallback rather than letting a stale code crash the centering call."""
        for wp in self._waypoints_export:
            if wp.code == code:
                self._center_map_on_waypoint(wp)
                return
        self.statusBar().showMessage(
            f"Route: waypoint {code} is no longer in the export list — cannot centre.",
            4000,
        )

    # ------------------------------------------------------------------
    # Route polyline overlay on the chart
    # ------------------------------------------------------------------

    def _project_route_point_to_scene(
        self, lat: float, lon: float
    ) -> QPointF | None:
        """Project a (lat, lon) to scene coordinates via the most appropriate
        calibrated sheet.

        Selection rule: prefer whichever calibrated sheet the point falls inside
        (UV in [0, 1]); if both are calibrated and the point is in-bounds for
        both (overlap zone), prefer south because it's painted on top in the
        scene and that's the sheet the user is interacting with there. If
        neither is in-bounds, fall back to whichever sheet is calibrated so the
        line still shows up at *some* projected position rather than silently
        being dropped — better to show a slightly off-chart leg than to leave
        the user wondering why their route disappeared.
        """
        best_in_bounds: QPointF | None = None
        best_fallback: QPointF | None = None
        for sheet_id in ("south", "north"):
            cal = self._geo_south if sheet_id == "south" else self._geo_north
            item = self._layer_item(sheet_id)
            if cal is None or item is None:
                continue
            try:
                u, v = cal.lonlat_to_uv(lon, lat)
            except (ValueError, ZeroDivisionError):
                continue
            scene_pt = lonlat_to_scene(item, cal, lon, lat)
            if scene_pt is None:
                continue
            if 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0:
                # First in-bounds hit wins — south is checked first so it
                # naturally takes priority in the overlap region.
                if best_in_bounds is None:
                    best_in_bounds = scene_pt
            elif best_fallback is None:
                best_fallback = scene_pt
        return best_in_bounds if best_in_bounds is not None else best_fallback

    def _redraw_route_overlay(self) -> None:
        """Rebuild the planned-route polyline + origin-marker overlay from scratch.

        Cheap (one ``QGraphicsPathItem`` for the line, one
        ``QGraphicsEllipseItem`` for the origin dot) and idempotent, so
        call sites can invoke it on any change — route mutation,
        calibration completion, sheet move/scale — without bookkeeping.

        The polyline is drawn only when there are ≥2 projectable points;
        the origin dot is drawn whenever there's at least one. The dot
        uses ``ItemIgnoresTransformations`` so its diameter stays in
        screen pixels at every zoom level, matching the polyline's
        cosmetic-width pen.
        """
        if self._route_overlay_item is not None:
            self._scene.removeItem(self._route_overlay_item)
            self._route_overlay_item = None
        if self._route_origin_marker_item is not None:
            self._scene.removeItem(self._route_origin_marker_item)
            self._route_origin_marker_item = None

        points = self._route.points()
        if not points:
            return

        scene_points: list[QPointF] = []
        for p in points:
            sp = self._project_route_point_to_scene(p.lat, p.lon)
            if sp is not None:
                scene_points.append(sp)
        if not scene_points:
            return

        # Origin marker: drawn first so the polyline lays on top of it and
        # the line's start endpoint sits flush against the dot's centre.
        # Local geometry centred on (0,0) + ItemIgnoresTransformations +
        # setPos(scene_pt) gives a fixed-pixel-size dot anchored to the
        # origin's lat/lon as the user pans/zooms.
        diameter = _ROUTE_ORIGIN_MARKER_DIAMETER_PX
        radius = diameter / 2.0
        dot = QGraphicsEllipseItem(-radius, -radius, diameter, diameter)
        dot.setBrush(QBrush(QColor(*_ROUTE_OVERLAY_RGBA)))
        dot.setPen(QPen(Qt.PenStyle.NoPen))
        dot.setPos(scene_points[0])
        dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        dot.setZValue(_ROUTE_ORIGIN_MARKER_Z)
        # Same rationale as the polyline below — the dot must not absorb
        # chart clicks intended for waypoint add/remove.
        dot.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._scene.addItem(dot)
        self._route_origin_marker_item = dot

        if len(scene_points) < 2:
            return

        path = QPainterPath()
        path.moveTo(scene_points[0])
        for sp in scene_points[1:]:
            path.lineTo(sp)

        item = QGraphicsPathItem(path)
        pen = QPen(QColor(*_ROUTE_OVERLAY_RGBA))
        pen.setWidthF(_ROUTE_OVERLAY_WIDTH_PX)
        # Cosmetic: width is in device pixels, not scene units, so the marker
        # stays the same visual weight at every zoom level.
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        item.setPen(pen)
        item.setZValue(_ROUTE_OVERLAY_Z)
        # The overlay is purely informational — it must not capture clicks
        # intended for the chart underneath (sheet selection, calibration,
        # route add/remove). The transparent stroke would otherwise still
        # accept hits along its width.
        item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._scene.addItem(item)
        self._route_overlay_item = item

    def _begin_calibration(self, sheet: str) -> None:
        if sheet not in ("north", "south"):
            return
        if self._north_item is None or self._south_item is None:
            QMessageBox.information(self, "Calibration", "Load maps first.")
            return
        if not self._waypoints_export:
            QMessageBox.information(self, "Calibration", "Load waypoints first.")
            return
        anchors = select_anchors_for_sheet(
            self._waypoints_export,
            sheet,
            _CALIBRATION_EDGE_ANCHOR_TARGET,
            n_overlap=_CALIBRATION_OVERLAP_ANCHOR_TARGET,
        )
        if anchors is None or len(anchors) < MIN_ANCHORS:
            QMessageBox.warning(
                self,
                "Calibration",
                "Could not pick suitable anchor waypoints for this sheet. "
                "Ensure the waypoint list has enough entries.",
            )
            return
        # The trailing ``n_overlap`` anchors in ``anchors`` are the *shared*
        # overlap-strip points — same records for both north and south. We
        # cache their codes so the prompt-step dialog can call them out as
        # "you'll click this one on both sheets" and the user understands
        # the duplicate-prompt later isn't a UI bug. ``select_anchors_for_sheet``
        # may have dropped some overlap anchors (collision with edge codes,
        # or the band was too sparse), so we re-derive the set from the actual
        # returned list and the database rather than assuming the requested
        # count was honoured.
        overlap_anchors = select_overlap_anchors(
            self._waypoints_export, n=_CALIBRATION_OVERLAP_ANCHOR_TARGET
        )
        overlap_codes = {r.code.casefold() for r in overlap_anchors}
        self._calibrate_state = {
            "sheet": sheet,
            "uvs": [],
            "anchors": anchors,
            "overlap_codes": overlap_codes,
        }
        self._calibrate_map_input_armed = False
        self._act_cancel_cal.setVisible(True)
        self._set_calibration_reticle_cursor(True)
        self.select_layer(sheet)
        self._fit_view_to_calibration_sheet(sheet)
        n_overlap_in_set = sum(
            1 for a in anchors if a.code.casefold() in overlap_codes
        )
        overlap_note = (
            f" (last {n_overlap_in_set} are *shared overlap* anchors — "
            "you'll click them again when you calibrate the other sheet)"
            if n_overlap_in_set
            else ""
        )
        self.statusBar().showMessage(
            f"Calibration ({sheet}): follow the dialogs — {len(anchors)} Shift+clicks on "
            f"triangle centers{overlap_note}. Esc to cancel.",
            0,
        )
        QTimer.singleShot(0, self._prompt_calibration_step)

    def _prompt_calibration_step(self) -> None:
        if self._calibrate_state is None:
            return
        self._calibrate_map_input_armed = False
        self._hide_calibration_overlay()
        uvs = self._calibrate_state.get("uvs")
        if not isinstance(uvs, list):
            return
        anchors = self._calibrate_state.get("anchors")
        if (
            not isinstance(anchors, tuple)
            or len(anchors) < MIN_ANCHORS
            or not all(isinstance(x, WaypointRecord) for x in anchors)
        ):
            return
        n = len(uvs)
        if n >= len(anchors):
            return
        r = anchors[n]
        title = f"Calibration point {n + 1} of {len(anchors)}"
        heb = f"\n{r.name_he}" if r.name_he else ""
        # Shared overlap anchors get a small extra note in the prompt body
        # so the user understands why the same waypoint is appearing on
        # *both* sheets during calibration — it's intentional (it pins both
        # affines to the same lat/lon across the seam), not a UI bug.
        overlap_codes = self._calibrate_state.get("overlap_codes") or set()
        is_overlap = (
            isinstance(overlap_codes, set)
            and r.code.casefold() in overlap_codes
        )
        overlap_note = (
            "\n\nThis is a shared overlap anchor — you will click the same "
            "waypoint again when you calibrate the other sheet. Clicking it on "
            "both sheets is what keeps the satellite imagery aligned across "
            "the seam between them."
            if is_overlap
            else ""
        )
        QMessageBox.information(
            self,
            title,
            f"Waypoint {r.code}{heb}\n\n"
            "Close this dialog, then Shift+click the exact center of its triangle symbol on "
            "this chart (middle of the marker). The reminder banner over the map will keep "
            f"showing the target code.{overlap_note}",
        )
        QTimer.singleShot(350, self._arm_calibration_map_input)

    def _arm_calibration_map_input(self) -> None:
        if self._calibrate_state is None:
            return
        self._calibrate_map_input_armed = True
        anchors = self._calibrate_state.get("anchors")
        raw_uvs = self._calibrate_state.get("uvs")
        uvs = raw_uvs if isinstance(raw_uvs, list) else []
        if (
            isinstance(anchors, tuple)
            and len(anchors) >= MIN_ANCHORS
            and len(uvs) < len(anchors)
            and isinstance(anchors[len(uvs)], WaypointRecord)
        ):
            self._show_calibration_overlay(
                anchors[len(uvs)], len(uvs) + 1, len(anchors)
            )
        self.statusBar().showMessage(
            "Calibration: Shift+click the triangle center on the chart now.",
            8000,
        )

    def _build_calibration_reticle_cursor(self) -> QCursor:
        """Triangle bullseye reticle for VRP-triangle calibration clicks.

        The reticle is a stack of four concentric, point-up, equilateral triangle
        outlines around a centroid marker (small ring + black-haloed white dot)::

                          △                ←  yellow  (outer band, outermost line)
                         △                 ←  blue
                        △                  ←  BLANK   (the chart's printed edge sits here)
                       △                   ←  blue
                      △                    ←  yellow  (inner band, innermost line)
                       ●                   ←  centroid: ring + dot (click hot-spot)

        Aiming protocol:

          * Zoom the chart until the printed VRP triangle is roughly the size
            of the reticle. The reticle pixmap is rendered in screen pixels so
            it never resizes with the chart — zoom is the only way to match.
          * Align the chart's three printed triangle *edges* so each one falls
            inside the BLANK gap between the two blue lines, with the same
            blue / yellow stack visible on both sides. The eye locks onto the
            symmetric alternation far more reliably than it does onto a single
            line crossing a single edge — sub-pixel rotational *and*
            positional errors both surface as the chart edge drifting toward
            one blue line and away from the other.
          * The centroid ring gives a coarse-grain "you are at the middle"
            cue; the dot inside it is the click hot-spot Qt uses for the
            actual click coordinate, so the recorded point is exactly the
            centroid of the printed VRP triangle when the dot sits on it.

        Colour rationale — ICAO 1:500k Israel charts mix yellow-tan land,
        green vegetation, blue water, and magenta / pink airspace tints.
        Yellow stays visible on blue / green / magenta backgrounds; blue
        stays visible on yellow-tan / pink. Alternating the two — without
        a black halo — keeps the reticle lines *thin and crisp* (a halo
        softens the precise edge by a pixel either side, which matters
        when each click is a calibration anchor). The centroid dot keeps
        its black halo because at 1-px diameter the white core needs a
        dark surround to hold contrast against any chart hue.

        Hot-spot is the centre pixel — Qt uses that for click coords, so
        the centroid dot is exactly the click point.
        """
        size = 64
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setBrush(Qt.BrushStyle.NoBrush)
            cx = cy = size / 2.0
            # Triangle band geometry. ``r`` is the centroid-to-vertex distance
            # (circumradius). Five levels (yellow-blue-BLANK-blue-yellow)
            # spaced ``spacing`` apart along the circumradius. The third level
            # is the blank gap where the chart triangle's printed edge sits.
            r_outer = 28.0
            spacing = 2.4
            line_w = 1.2
            yellow = QColor(255, 235, 0, 255)
            blue = QColor(60, 110, 255, 255)
            drawn_levels = (
                (0, yellow),
                (1, blue),
                # 2 → blank (chart edge target)
                (3, blue),
                (4, yellow),
            )
            sqrt3_over_2 = math.sqrt(3.0) / 2.0
            for idx, color in drawn_levels:
                r = r_outer - idx * spacing
                apex = QPointF(cx, cy - r)
                base_l = QPointF(cx - r * sqrt3_over_2, cy + r / 2.0)
                base_r = QPointF(cx + r * sqrt3_over_2, cy + r / 2.0)
                pen = QPen(color, line_w)
                pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
                p.setPen(pen)
                path = QPainterPath(apex)
                path.lineTo(base_r)
                path.lineTo(base_l)
                path.closeSubpath()
                p.drawPath(path)
            # Centroid ring (coarse "you're at the middle" cue). Black outer
            # so it reads against any chart hue; thin enough not to compete
            # with the dot for being THE centre.
            p.setPen(QPen(QColor(0, 0, 0, 235), 1.1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), 4.5, 4.5)
            # Centre dot: black halo + white core for sub-pixel pinpoint
            # contrast against any chart colour. Hot-spot of the cursor.
            # Halo radius (3.0) is large enough to leave ≥1 px of visible
            # black ring around the white core; white-core radius (1.5)
            # is just large enough that the four centre pixels under the
            # hot-spot (Qt picks one of them for the click coordinate) are
            # fully white, not partially-covered grey — so the dot reads
            # as a crisp pinpoint at any chart background hue.
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 235))
            p.drawEllipse(QPointF(cx, cy), 3.0, 3.0)
            p.setBrush(QColor(255, 255, 255, 255))
            p.drawEllipse(QPointF(cx, cy), 1.5, 1.5)
        finally:
            p.end()
        return QCursor(pm, size // 2, size // 2)

    def _set_calibration_reticle_cursor(self, on: bool) -> None:
        """Apply / remove the precision reticle on the map viewport for calibration clicks."""
        vp = self._view.viewport()
        if on:
            if self._calibration_reticle_cursor is None:
                self._calibration_reticle_cursor = self._build_calibration_reticle_cursor()
            vp.setCursor(self._calibration_reticle_cursor)
        else:
            vp.unsetCursor()

    def _ensure_calibration_overlay(self) -> QLabel:
        """Lazily create the floating "select <code>" reminder painted over the map view."""
        if self._calibration_overlay is None:
            lbl = QLabel(self._view.viewport())
            lbl.setStyleSheet(
                "QLabel{"
                "background: rgba(255, 215, 0, 235);"
                "color: #1c1c1c;"
                "border: 2px solid #1c1c1c;"
                "border-radius: 6px;"
                "padding: 6px 14px;"
                "font-size: 14pt;"
                "font-weight: 600;"
                "}"
            )
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            # The label must never swallow map clicks (calibration relies on Shift+click hits).
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            lbl.hide()
            self._calibration_overlay = lbl
            # Mouse tracking ensures we get MouseMove without buttons pressed, so the
            # reminder follows the cursor while the user hunts for the triangle.
            self._view.viewport().setMouseTracking(True)
            self._view.setMouseTracking(True)
            self._view.viewport().installEventFilter(self)
        return self._calibration_overlay

    def _show_calibration_overlay(
        self, target: WaypointRecord, step_index: int, total: int
    ) -> None:
        lbl = self._ensure_calibration_overlay()
        sheet = (
            self._calibrate_state.get("sheet") if self._calibrate_state else None
        )
        sheet_text = sheet if isinstance(sheet, str) else "?"
        heb = (
            f"<br/><span style='font-size:11pt; font-weight:500;'>{target.name_he}</span>"
            if target.name_he
            else ""
        )
        lbl.setText(
            f"Shift+click triangle for <b>{target.code}</b>"
            f"<span style='font-size:11pt; font-weight:500;'>"
            f"&nbsp;&nbsp;({sheet_text} sheet, point {step_index} of {total})</span>{heb}"
        )
        lbl.adjustSize()
        self._position_calibration_overlay()
        lbl.show()
        lbl.raise_()

    def _hide_calibration_overlay(self) -> None:
        if self._calibration_overlay is not None:
            self._calibration_overlay.hide()

    def _position_calibration_overlay(self) -> None:
        """Initial / fallback placement: top-centre of the viewport.

        Used when the overlay is first shown (no cursor position yet) and on viewport
        resize. Once the user moves the mouse over the map, ``_follow_calibration_overlay
        _to_cursor`` takes over and the banner trails the cursor.
        """
        lbl = self._calibration_overlay
        if lbl is None:
            return
        vp = self._view.viewport()
        lbl.adjustSize()
        x = max(8, (vp.width() - lbl.width()) // 2)
        lbl.move(x, 12)

    def _follow_calibration_overlay_to_cursor(self, cursor_pos) -> None:
        """Reposition the reminder near the cursor, auto-flipping at viewport edges.

        The cursor tells us where the user is looking — for a hunting-for-triangle task,
        a banner pinned to the top of the screen forces a glance away from the work. We
        offset diagonally below-right of the cursor by a comfortable margin and flip the
        offset to above / left when that would push the banner off the viewport, keeping
        it always visible without ever covering the click target itself.
        """
        lbl = self._calibration_overlay
        if lbl is None:
            return
        vp = self._view.viewport()
        lbl.adjustSize()
        margin = 12
        offset = 28  # px — enough that the cursor never overlaps the banner.
        w, h = lbl.width(), lbl.height()
        cx, cy = cursor_pos.x(), cursor_pos.y()

        x = cx + offset
        if x + w + margin > vp.width():
            x = cx - offset - w
        y = cy + offset
        if y + h + margin > vp.height():
            y = cy - offset - h

        x = max(margin, min(x, vp.width() - w - margin))
        y = max(margin, min(y, vp.height() - h - margin))
        lbl.move(x, y)

    def _fit_view_to_calibration_sheet(self, sheet_id: str) -> None:
        """Fit the whole sheet in the viewport with padding so calibration starts context-zoomed, not cropped."""
        item = self._layer_item(sheet_id)
        if item is None:
            return
        rect = item.sceneBoundingRect()
        if not rect.isValid() or rect.width() <= 0 or rect.height() <= 0:
            return
        px = rect.width() * 0.08
        py = rect.height() * 0.08
        pad = max(px, py, 64.0)
        padded = rect.adjusted(-pad, -pad, pad, pad)
        self._view.fitInView(padded, Qt.AspectRatioMode.KeepAspectRatio)

    def _cancel_calibration(self) -> None:
        if self._calibrate_state is None:
            return
        self._calibrate_state = None
        self._calibrate_map_input_armed = False
        self._hide_calibration_overlay()
        self._set_calibration_reticle_cursor(False)
        self._act_cancel_cal.setVisible(False)
        self.statusBar().showMessage("Calibration cancelled.", 5000)

    def _sheet_pixmap_item_at(self, sheet_id: str, scene_pos: QPointF) -> QGraphicsPixmapItem | None:
        """True if scene_pos lies inside the sheet pixmap's rect (not opaque pixels only).

        QGraphicsPixmapItem.contains() uses the item shape (opaque areas). Overlapping
        transparent south over north would reject clicks on symbols that show through.
        """
        item = self._layer_item(sheet_id)
        if item is None:
            return None
        lp = item.mapFromScene(scene_pos)
        if not item.boundingRect().contains(lp):
            return None
        return item

    def _lookup_waypoint(self, code: str) -> WaypointRecord | None:
        cf = code.casefold()
        for r in self._waypoints_export:
            if r.code.casefold() == cf:
                return r
        return None

    def _finalize_auto_anchor_calibration(self) -> None:
        st = self._calibrate_state
        uvs = st.get("uvs") if st else None
        anchors = st.get("anchors") if st else None
        if (
            not st
            or not isinstance(uvs, list)
            or not isinstance(anchors, tuple)
            or len(anchors) < MIN_ANCHORS
            or len(uvs) != len(anchors)
        ):
            self._cancel_calibration()
            return
        if not all(isinstance(a, WaypointRecord) for a in anchors):
            self._cancel_calibration()
            return
        sheet = st["sheet"]
        if not isinstance(sheet, str) or sheet not in ("north", "south"):
            self._cancel_calibration()
            return
        path_str = self._north_path if sheet == "north" else self._south_path
        pdf_path = Path(path_str) if path_str else Path()
        if not pdf_path.is_file():
            QMessageBox.warning(self, "Calibration", "PDF path is not set or missing.")
            self._cancel_calibration()
            return

        # Reject calibrations where the database lat/lon span of the chosen anchors is
        # tiny (e.g. all clicks on the same waypoint type or accidentally only two
        # near-coincident points): LSQ would still succeed but extrapolation would be
        # terrible. Same UV check guards against the user clicking nearly the same pixel
        # twice (which makes the lat/lon span "real" but the image span useless).
        lats = [a.lat for a in anchors]
        lons = [a.lon for a in anchors]
        lon_span = max(lons) - min(lons)
        lat_span = max(lats) - min(lats)
        if math.hypot(lon_span, lat_span) < 2e-5:
            QMessageBox.warning(
                self,
                "Calibration",
                "Anchor waypoints are too close in the database; cannot calibrate.",
            )
            self._cancel_calibration()
            return
        u_span = max(p[0] for p in uvs) - min(p[0] for p in uvs)
        v_span = max(p[1] for p in uvs) - min(p[1] for p in uvs)
        if math.hypot(u_span, v_span) < 0.03:
            QMessageBox.warning(
                self,
                "Calibration",
                "The Shift+clicks are clustered too tightly on the image; "
                "cancel and run calibration again.",
            )
            self._cancel_calibration()
            return

        try:
            fp = pdf_fingerprint(pdf_path)
            cal_points = [
                CalibrationPoint(code=a.code, lat=a.lat, lon=a.lon, u=uv[0], v=uv[1])
                for a, uv in zip(anchors, uvs)
            ]
            layout_snap = self._current_sheet_layout(sheet)
            if layout_snap is None:
                QMessageBox.warning(self, "Calibration", "Could not read the map layout; try again.")
                self._cancel_calibration()
                return
            cal = calibration_from_points(fp, *cal_points, map_layout=layout_snap)
        except ValueError as exc:
            QMessageBox.warning(self, "Calibration", str(exc))
            self._cancel_calibration()
            return

        if sheet == "north":
            self._geo_north = cal
        else:
            self._geo_south = cal
        self._calibrate_state = None
        self._act_cancel_cal.setVisible(False)
        self._hide_calibration_overlay()
        self._set_calibration_reticle_cursor(False)
        self._persist_geo_calibration()
        # Once *both* sheets are calibrated, derive the sheet layout (south's
        # scale + xy translation, north pinned at identity) from the user's
        # clicks at the shared overlap anchors — no more Alt-wheel eyeballing.
        # ``_align_sheets_from_overlap_anchors`` is a no-op when only one
        # sheet has a calibration. It re-saves both calibrations' map_layout
        # and the on-disk ``map_layout.json`` to match the freshly applied
        # sheet positions so ``_invalidate_geo_if_layout_mismatch`` stays
        # quiet on next launch.
        joint = self._align_sheets_from_overlap_anchors()
        # The satellite + waypoint-marker overlays were built once
        # at ``_on_map_finished`` time, before any calibration
        # existed — both overlay attributes are still ``None`` on
        # a first-run calibration and the toolbar's Satellite view
        # toggle would otherwise do nothing. Tear down + rebuild
        # picks up the just-saved affine and (re)constructs the
        # tile + marker scene items. Both sheets get rebuilt
        # together because each overlay carries a
        # ``peer_calibration`` reference for the chart-seam
        # partition; see
        # :meth:`_rebuild_overlays_after_calibration_change` for
        # the rationale.
        self._rebuild_overlays_after_calibration_change()
        # Newly-calibrated sheet may now contain (or better-position) some of
        # the planned route's points — refresh the route panel so the
        # altitude column projects this sheet's arrows for the first time
        # *and* the polyline snaps to its correct chart position. Going
        # through the panel-refresh path is the canonical update site;
        # ``_redraw_route_overlay`` is folded inside it.
        self._refresh_route_panel()
        codes = " / ".join(a.code for a in anchors)
        residual_pct = cal.residual_uv * 100.0  # rough-percent of pixmap width
        msg = (
            f"Saved geo calibration for {sheet} sheet ({len(anchors)} anchors: {codes}). "
            f"Per-sheet affine residual ≈ {residual_pct:.2f}% of chart width."
        )
        if joint is not None:
            msg += (
                f" Joint LSQ aligned ({'/'.join(joint.shared_codes)}): "
                f"south scale {joint.layout[0]:.5f}, chart residual "
                f"{joint.chart_residual_px:.2f} px, sat-stitch residual "
                f"{joint.consistency_residual_px:.2f} px ({joint.iterations} iters)."
            )
        self.statusBar().showMessage(msg, 12000)
        # Warn if either user-visible alignment metric is over budget.
        # ``chart_residual_px`` drives the chart-pixmap seam jump the user
        # sees on every visit; ``consistency_residual_px`` drives the
        # sat-tile seam jump. Either being over budget means a click is
        # off-centre on one of the shared anchors.
        if joint is not None:
            worst_px = max(
                joint.chart_residual_px, joint.consistency_residual_px
            )
            if worst_px > _OVERLAP_ALIGNMENT_WARN_PX:
                QMessageBox.warning(
                    self,
                    "Calibration",
                    "Sheets were auto-aligned, but the overlap clicks disagree "
                    f"by up to {worst_px:.1f} px (threshold "
                    f"{_OVERLAP_ALIGNMENT_WARN_PX:.0f} px). "
                    "One of the shared overlap anchors "
                    f"({', '.join(joint.shared_codes)}) was likely clicked "
                    "off-centre on one sheet. Re-calibrate that sheet and try "
                    "to land each overlap-anchor click squarely on the "
                    "triangle's centre.",
                )
        self._maybe_prompt_calibrate_other_sheet_after(sheet)

    def _align_sheets_from_overlap_anchors(self) -> JointCalibration | None:
        """Jointly fit the two per-sheet affines and the south layout from
        every available click, with the shared overlap anchors acting as
        the cross-sheet coupling.

        The "3+4" alignment strategy in user-speak: solve all 15
        parameters (6 north affine + 6 south affine + 3 layout) in a
        single LSQ rather than the previous two-step pipeline
        (per-sheet independent affines + click-derived layout). The
        joint fit balances the chart-on-chart click residuals against
        the affine-derived sat-stitch consistency residual at the
        shared anchors, landing both at roughly half the per-sheet
        click residual instead of the lopsided
        ``(chart ≈ 3 px, sat ≈ 30 px)`` the legacy pipeline produced.

        Pins the north sheet at scale 1.0 / position (0, 0) by convention
        — the joint LSQ's "layout" output describes south's
        ``(scale, tx, ty)`` relative to north's pinned identity.

        Side effects (only when both sheets have a calibration and at least
        :data:`MIN_OVERLAP_ALIGNMENT_ANCHORS` shared codes between them):

        * Overrides each :class:`SheetGeoCalibration`'s internal affine
          with the joint-fit coefficients via
          :meth:`apply_joint_affine_overrides`, so all downstream
          ``lonlat_to_uv`` / ``uv_to_lonlat`` callers pick up the joint
          fit transparently — including the satellite tile placement
          that motivated the whole change.
        * Moves and scales the chart pixmap items to the joint layout.
        * Updates each sheet's saved ``map_layout`` so the on-disk
          fingerprint matches the new layout — otherwise
          :meth:`_invalidate_geo_if_layout_mismatch` would discard the
          freshly-saved calibrations on the next ``persist_map_layout``
          call.
        * Re-persists ``geo_calibration.json`` and ``map_layout.json``.
        * Refreshes the scene rect (sheets may now occupy a slightly
          different region).

        Returns the :class:`JointCalibration` so the caller can surface
        its residuals to the user, or ``None`` if alignment was skipped
        (peer sheet not yet calibrated, no shared overlap codes,
        degenerate LSQ).

        Idempotent: calling it twice with no intervening recalibration
        produces the same layout, modulo numerical noise.
        """
        if self._geo_north is None or self._geo_south is None:
            return None
        if self._north_item is None or self._south_item is None:
            return None
        npix = self._north_item.pixmap()
        spix = self._south_item.pixmap()
        if npix.isNull() or spix.isNull():
            return None
        joint = compute_joint_calibration(
            list(self._geo_north.points),
            list(self._geo_south.points),
            (float(npix.width()), float(npix.height())),
            (float(spix.width()), float(spix.height())),
        )
        if joint is None:
            return None
        layout_diag.log(
            "joint_calibration.computed",
            shared=",".join(joint.shared_codes),
            iterations=joint.iterations,
            converged=joint.converged,
            scale=joint.layout[0],
            tx=joint.layout[1],
            ty=joint.layout[2],
            chart_residual_px=joint.chart_residual_px,
            consistency_residual_px=joint.consistency_residual_px,
            click_residual_north_px=joint.click_residual_north_px,
            click_residual_south_px=joint.click_residual_south_px,
        )
        self._apply_joint_calibration(joint)
        self._persist_geo_calibration()
        self.persist_map_layout()
        self._refresh_scene_rect()
        # Deliberately do NOT call ``_fit_map`` here: the user is mid-flow
        # (just finished clicking the last anchor of the second sheet) and
        # snapping back to full-chart view would be jarring. The pixmaps
        # may shift by a hundred or so scene pixels (north was at some
        # scale ≠ 1 before recalibration), so the user sees the seam
        # tighten in place rather than the camera jumping somewhere else.
        return joint

    def _build_chart_seam_partition(self) -> "_ChartSeamPartitionBuilder | None":
        """Build a sheet-agnostic chart-seam partition builder from the
        current chart layout + north's calibration.

        Returns a small helper whose ``for_north()`` / ``for_south()``
        methods produce :class:`ChartSeamPartition` instances tailored
        to each sheet's overlay (same threshold values, different
        ``self_is_north`` flag). Returns ``None`` when the partition
        can't be defined yet — e.g. either chart pixmap is missing,
        the north calibration is missing, or either pixmap has zero
        dimensions. In that case callers fall back to the "no
        partition / draw every projectable tile" path.

        Splitting the partition construction off into a dedicated
        helper keeps the satellite + waypoint overlay builders free
        of geometry / layout math — they just ask for
        ``builder.for_north()`` or ``builder.for_south()`` and pass
        the result to the overlay constructor.
        """
        if (
            self._geo_north is None
            or self._north_item is None
            or self._south_item is None
        ):
            return None
        npix = self._north_item.pixmap()
        spix = self._south_item.pixmap()
        if npix.isNull() or spix.isNull():
            return None
        H_n = float(npix.height())
        H_s = float(spix.height())
        if H_n <= 0 or H_s <= 0:
            return None
        south_pos_y = float(self._south_item.pos().y())
        south_scale = float(self._south_item.scale())
        # ``QGraphicsItem.transformOriginPoint`` is set to the pixmap
        # centre in :meth:`_prepare_map_sheet_item`, so the local
        # (0, 0) corner maps to scene at
        # ``south_pos_y + (1 - south_scale) · H_s / 2``. That scene_y
        # is the visible top edge of south's chart pixmap — and
        # therefore the seam between north's chart and south's.
        chart_seam_scene_y = south_pos_y + (1.0 - south_scale) * H_s / 2.0
        return _ChartSeamPartitionBuilder(
            north_calibration=self._geo_north,
            north_pixmap_height=H_n,
            chart_seam_scene_y=chart_seam_scene_y,
        )

    def _apply_joint_calibration(self, joint: JointCalibration) -> None:
        """Push a :class:`JointCalibration` result onto the in-memory
        calibrations and chart pixmap items.

        Splits out from :meth:`_align_sheets_from_overlap_anchors` so the
        startup auto-upgrade path (:meth:`_reapply_overlap_alignment_from_saved_clicks_if_changed`)
        can apply a freshly-computed joint fit without re-running all the
        persistence side-effects in that method. Callers are responsible
        for persisting if they need to.
        """
        assert self._geo_north is not None
        assert self._geo_south is not None
        assert self._north_item is not None
        assert self._south_item is not None
        self._geo_north.apply_joint_affine_overrides(
            joint.north_affine, joint.north_lon_scale
        )
        self._geo_south.apply_joint_affine_overrides(
            joint.south_affine, joint.south_lon_scale
        )
        scale, tx, ty = joint.layout
        self._north_item.setPos(0.0, 0.0)
        self._north_item.setScale(1.0)
        self._south_item.setPos(tx, ty)
        self._south_item.setScale(scale)
        self._geo_north.map_layout = {"x": 0.0, "y": 0.0, "scale": 1.0}
        self._geo_south.map_layout = {
            "x": float(tx),
            "y": float(ty),
            "scale": float(scale),
        }

    def _reapply_overlap_alignment_from_saved_clicks_if_changed(self) -> None:
        """Idempotent startup re-alignment: if both sheets have valid
        saved click data and the joint-LSQ layout differs from what's
        currently applied to the chart items, re-apply and re-persist.

        Purpose: upgrade users in place when the alignment math itself
        changes — e.g. moving from the legacy click-based
        ``compute_overlap_aligned_layout`` to the joint LSQ over both
        affines and the layout introduced for the 3+4 stitch fix —
        without forcing them to re-click 14 anchors. The clicks
        themselves stay the source of truth; we just re-derive the
        layout from them on every launch.

        This method only writes the **layout** (chart pixmap positions
        and scale) and the persisted map_layout fields. The matching
        joint-fit *affine* overrides on
        ``self._geo_north`` / ``self._geo_south`` are applied later,
        in :meth:`_apply_joint_affine_overrides_at_startup`, after
        :meth:`_reload_geo_calibration_from_disk` populates those
        attributes. Splitting the two halves keeps this method
        independent of in-memory cal state — it can run before any
        ``self._geo_*`` exists, which is the order
        :meth:`_on_map_finished` happens to use.

        The :class:`SheetGeoCalibration` objects are constructed via
        ``sheet_from_dict`` rather than
        ``load_sheet_calibration_or_reason`` because the latter
        rejects calibrations whose saved ``map_layout`` doesn't match
        the current sheet pose — which is exactly the condition we
        want to *fix* here, not bail on.

        Conservative thresholds in :func:`map_layout_matches` (0.5 px
        / 1e-4 scale) make the persistence side-effect a no-op on
        every subsequent launch where the joint fit produces the
        same layout.
        """
        if self._north_item is None or self._south_item is None:
            return
        raw = load_saved_calibration(self._project_root)
        north_block = raw.get("north") if isinstance(raw, dict) else None
        south_block = raw.get("south") if isinstance(raw, dict) else None
        if not isinstance(north_block, dict) or not isinstance(south_block, dict):
            return
        n_path = Path(self._north_path) if self._north_path else None
        s_path = Path(self._south_path) if self._south_path else None
        if n_path is None or s_path is None:
            return
        if not fingerprints_match(north_block.get("pdf"), n_path):
            return
        if not fingerprints_match(south_block.get("pdf"), s_path):
            return
        try:
            n_cal = sheet_from_dict(north_block)
            s_cal = sheet_from_dict(south_block)
        except (ValueError, TypeError, KeyError):
            return
        if n_cal is None or s_cal is None:
            return
        npix = self._north_item.pixmap()
        spix = self._south_item.pixmap()
        if npix.isNull() or spix.isNull():
            return
        joint = compute_joint_calibration(
            list(n_cal.points),
            list(s_cal.points),
            (float(npix.width()), float(npix.height())),
            (float(spix.width()), float(spix.height())),
        )
        if joint is None:
            return
        cur_n = self._current_sheet_layout("north")
        cur_s = self._current_sheet_layout("south")
        target_n = {"x": 0.0, "y": 0.0, "scale": 1.0}
        target_s = {
            "x": float(joint.layout[1]),
            "y": float(joint.layout[2]),
            "scale": float(joint.layout[0]),
        }
        if cur_n is not None and cur_s is not None:
            if map_layout_matches(target_n, cur_n) and map_layout_matches(
                target_s, cur_s
            ):
                return
        layout_diag.log(
            "joint_calibration.startup_layout_reapply",
            shared=",".join(joint.shared_codes),
            iterations=joint.iterations,
            converged=joint.converged,
            scale=joint.layout[0],
            tx=joint.layout[1],
            ty=joint.layout[2],
            chart_residual_px=joint.chart_residual_px,
            consistency_residual_px=joint.consistency_residual_px,
            old_north=cur_n,
            old_south=cur_s,
        )
        scale, tx_layout, ty_layout = joint.layout
        self._north_item.setPos(0.0, 0.0)
        self._north_item.setScale(1.0)
        self._south_item.setPos(tx_layout, ty_layout)
        self._south_item.setScale(scale)
        # Write the corrected ``map_layout`` field back into the calibration
        # JSON so :meth:`_reload_geo_calibration_from_disk` accepts the
        # calibrations on the next pass instead of rejecting them for layout
        # mismatch.
        north_block["map_layout"] = dict(target_n)
        south_block["map_layout"] = dict(target_s)
        save_calibration_payload(self._project_root, raw)
        # Also rewrite ``map_layout.json`` so a *no-calibration-present*
        # restart path lands on the same numbers rather than the stale
        # buggy layout.
        self.persist_map_layout()
        self._refresh_scene_rect()

    def _apply_joint_affine_overrides_at_startup(self) -> None:
        """Run the joint LSQ on the freshly-loaded
        ``self._geo_north`` / ``self._geo_south`` and push the
        joint-fit affine coefficients back into them so downstream
        callers (satellite overlay, waypoint marker overlay,
        :func:`lonlat_to_scene`, the routing pipeline) see the joint
        fit rather than the per-sheet independent fits that
        :meth:`SheetGeoCalibration.__post_init__` produces.

        Counterpart to
        :meth:`_reapply_overlap_alignment_from_saved_clicks_if_changed`
        — that one writes the *layout* before the cals are loaded;
        this one writes the *affine overrides* after. The two halves
        must agree on the joint result (they re-run joint LSQ on the
        same clicks against the same pixmap sizes, both deterministic),
        so any layout the layout-pass produced will match the affine
        overrides this pass produces.

        Skipped silently when either sheet is uncalibrated — the
        legacy independent-fit affines stay in place, which is the
        right call when there's no shared overlap anchor to couple
        the two affines through anyway.
        """
        if self._geo_north is None or self._geo_south is None:
            return
        if self._north_item is None or self._south_item is None:
            return
        npix = self._north_item.pixmap()
        spix = self._south_item.pixmap()
        if npix.isNull() or spix.isNull():
            return
        joint = compute_joint_calibration(
            list(self._geo_north.points),
            list(self._geo_south.points),
            (float(npix.width()), float(npix.height())),
            (float(spix.width()), float(spix.height())),
        )
        if joint is None:
            return
        layout_diag.log(
            "joint_calibration.startup_affine_override",
            shared=",".join(joint.shared_codes),
            iterations=joint.iterations,
            converged=joint.converged,
            chart_residual_px=joint.chart_residual_px,
            consistency_residual_px=joint.consistency_residual_px,
            click_residual_north_px=joint.click_residual_north_px,
            click_residual_south_px=joint.click_residual_south_px,
        )
        self._geo_north.apply_joint_affine_overrides(
            joint.north_affine, joint.north_lon_scale
        )
        self._geo_south.apply_joint_affine_overrides(
            joint.south_affine, joint.south_lon_scale
        )

    def _maybe_prompt_calibrate_other_sheet_after(self, just_completed: str) -> None:
        """After saving one sheet, offer the instruction dialog if the other sheet is still uncalibrated."""
        if just_completed == "north" and self._geo_south is None:
            QTimer.singleShot(
                200,
                lambda: self._open_calibration_instruction_dialog(
                    ["South chart is not calibrated yet."]
                ),
            )
        elif just_completed == "south" and self._geo_north is None:
            QTimer.singleShot(
                200,
                lambda: self._open_calibration_instruction_dialog(
                    ["North chart is not calibrated yet."]
                ),
            )

    def _persist_geo_calibration(self) -> None:
        payload = build_payload(self._geo_north, self._geo_south)
        try:
            save_calibration_payload(self._project_root, payload)
        except OSError:
            pass

    def _current_sheet_layout(self, sheet_id: str) -> dict[str, float] | None:
        item = self._layer_item(sheet_id)
        if item is None:
            return None
        p = item.pos()
        return {
            "x": float(p.x()),
            "y": float(p.y()),
            "scale": float(item.scale()),
        }

    def _reload_geo_calibration_from_disk(self) -> list[str]:
        """
        Load geo calibration from disk, matching each sheet’s PDF fingerprint and
        the current on-screen position/scale. Returns user-facing issue lines for
        any sheet that must be (re)calibrated.
        """
        raw = load_saved_calibration(self._project_root)
        n_path = Path(self._north_path) if self._north_path else None
        s_path = Path(self._south_path) if self._south_path else None
        n_layout = self._current_sheet_layout("north")
        s_layout = self._current_sheet_layout("south")
        issues: list[str] = []
        self._geo_north, en = load_sheet_calibration_or_reason(
            raw, "north", n_path, n_layout, "North"
        )
        if en:
            issues.append(en)
        self._geo_south, es = load_sheet_calibration_or_reason(
            raw, "south", s_path, s_layout, "South"
        )
        if es:
            issues.append(es)
        return issues

    def _open_calibration_instruction_dialog(self, issues: list[str]) -> None:
        dlg = CalibrationInstructionDialog(
            self,
            issues,
            show_north_button=self._geo_north is None,
            show_south_button=self._geo_south is None,
            n_anchors=_CALIBRATION_ANCHOR_TARGET,
        )
        code = dlg.exec()
        if code == CalibrationInstructionDialog.CALIBRATE_NORTH:
            self._begin_calibration("north")
        elif code == CalibrationInstructionDialog.CALIBRATE_SOUTH:
            self._begin_calibration("south")

    def _open_calibration_options(self) -> None:
        """Show the Map Calibration Options dialog and dispatch the chosen action.

        Each action is invoked via ``QTimer.singleShot(0, ...)`` so the dialog
        has fully closed before the action's own modal (confirmation,
        progress bar, calibration overlay) appears. Without that gap the
        new modal would briefly stack on top of the closing options dialog
        and Qt's modality stack would steal focus back to the wrong window.
        """
        from cvfr_routemaster.calibration_options_dialog import CalibrationOptionsDialog

        dlg = CalibrationOptionsDialog(self, n_anchors=_CALIBRATION_ANCHOR_TARGET)
        code = dlg.exec()
        # Map each action constant to the controller method that owns the work.
        # Every entrypoint already does its own "are maps loaded?" gate where
        # required, so the dispatch table itself stays trivial.
        dispatch = {
            CalibrationOptionsDialog.ACTION_REOCR_WAYPOINTS: self._reload_waypoints_ocr_only,
            CalibrationOptionsDialog.ACTION_FIT_MAP: self._fit_map,
            CalibrationOptionsDialog.ACTION_RESET_LAYOUT: self._reset_map_layout_confirm,
            CalibrationOptionsDialog.ACTION_CALIBRATE_NORTH: lambda: self._begin_calibration("north"),
            CalibrationOptionsDialog.ACTION_CALIBRATE_SOUTH: lambda: self._begin_calibration("south"),
            CalibrationOptionsDialog.ACTION_CLEAR_CALIBRATION: self._clear_saved_geo_calibration,
        }
        action = dispatch.get(code)
        if action is not None:
            QTimer.singleShot(0, action)

    def _invalidate_geo_if_layout_mismatch(self) -> None:
        """Clear calibration that no longer matches moved/scaled sheets; persist and warn."""
        if self._north_item is None:
            return
        cleared: list[str] = []
        n_layout = self._current_sheet_layout("north")
        s_layout = self._current_sheet_layout("south")
        if self._geo_north and self._geo_north.map_layout and n_layout:
            if not map_layout_matches(self._geo_north.map_layout, n_layout):
                self._geo_north = None
                cleared.append("North")
        if self._geo_south and self._geo_south.map_layout and s_layout:
            if not map_layout_matches(self._geo_south.map_layout, s_layout):
                self._geo_south = None
                cleared.append("South")
        if not cleared:
            return
        self._persist_geo_calibration()
        # Cleared calibration → previously-projected arrows for that sheet
        # no longer apply. Push the route panel through a refresh so the
        # altitude column drops back to "unknown" for those segments
        # immediately, rather than waiting for the next route edit.
        self._refresh_route_panel()
        issues = []
        if "North" in cleared:
            issues.append(
                "North chart was moved or scaled since it was calibrated — calibrate again."
            )
        if "South" in cleared:
            issues.append(
                "South chart was moved or scaled since it was calibrated — calibrate again."
            )
        self.statusBar().showMessage(
            "Chart calibration cleared for moved or scaled sheet(s). "
            "Open Map Calibration Options… to recalibrate.",
            12000,
        )
        QTimer.singleShot(
            300,
            lambda iss=list(issues): self._open_calibration_instruction_dialog(iss),
        )

    def _clear_saved_geo_calibration(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clear geo calibration",
            "Remove saved north/south chart calibration for this project?\n"
            "(You can recreate it with Calibrate north/south map.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._geo_north = None
        self._geo_south = None
        self._persist_geo_calibration()
        # Manually-cleared calibration: same reasoning as the auto-clear
        # path — refresh so the altitude column reflects the new "no
        # calibration" state without waiting for the user to edit the route.
        self._refresh_route_panel()
        self.statusBar().showMessage("Cleared saved geo calibration.", 8000)
        if self._north_item is not None and self._south_item is not None:
            cal_issues = self._reload_geo_calibration_from_disk()
            if cal_issues:
                QTimer.singleShot(
                    150,
                    lambda iss=list(cal_issues): self._open_calibration_instruction_dialog(iss),
                )

    def _reset_map_layout_confirm(self) -> None:
        if self._north_item is None or self._south_item is None:
            QMessageBox.information(self, "Maps", "Load maps first.")
            return
        reply = QMessageBox.question(
            self,
            "Reset map layout",
            "Reset the north and south sheets to the default position and 100% scale?\n"
            "(PDFs are not reloaded; waypoints are unchanged.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        layout_diag.log("reset_map_layout.user_confirmed")
        self._north_item.setPos(0.0, 0.0)
        self._north_item.setScale(1.0)
        self._south_item.setScale(1.0)
        nh = float(self._north_item.pixmap().height())
        self._south_item.setPos(0.0, nh)
        self._selected = "south"
        self.select_layer("south")
        self._refresh_scene_rect()
        self.persist_map_layout()
        self._fit_map()
        self.statusBar().showMessage("Map layout reset to default.", 6000)

    def _open_settings(self) -> None:
        """Open *Map File Settings*. Two acceptable return codes:

        - ``QDialog.Accepted`` — save sources + autoload toggle, no further action.
        - ``SettingsDialog.LOAD_NOW`` — same persist + immediately fire
          ``_load_all`` so the user sees the result of an edited source
          without going back to a separate toolbar button.

        Anything else (Cancel, ESC, window close) leaves persisted state
        unchanged.

        Each "source" string is either a local PDF path or an
        ``http(s)://`` URL. The dialog hands back whatever the
        user typed; we persist that verbatim and re-resolve
        through ``_resolve_chart_sources_silent`` so a URL source
        with an existing cache file gets its ``_*_path`` updated
        immediately (avoids requiring the user to "click Load now"
        when the cache happens to be warm).
        """
        dlg = SettingsDialog(
            self._source_north,
            self._source_south,
            self._source_back,
            autoload_on_start=load_autoload_enabled(),
            parent=self,
        )
        code = dlg.exec()
        if code not in (QDialog.DialogCode.Accepted, SettingsDialog.LOAD_NOW):
            return
        n, s, b = dlg.paths()
        self._source_north, self._source_south, self._source_back = n, s, b
        save_pdf_paths(n, s, b)
        save_autoload_enabled(dlg.autoload_on_start())
        # Re-resolve cached URLs silently — if the user pasted a
        # URL whose cache already happens to exist (e.g. they
        # switched between two URLs both already downloaded), the
        # cached path becomes ``_*_path`` immediately without a
        # network round-trip. URLs needing a fresh download stay
        # as empty strings on ``_*_path`` and get handled by the
        # download flow inside ``_load_all``.
        (
            self._north_path,
            self._south_path,
            self._back_path,
        ) = _resolve_chart_sources_silent(
            (n, s, b), self._project_root
        )
        if code == SettingsDialog.LOAD_NOW:
            # Defer one event loop tick so the settings dialog is fully closed
            # before the load progress dialog stacks on top — same modality
            # ordering rationale as the calibration options dispatcher.
            QTimer.singleShot(0, self._load_all)

    def _ensure_chart_sources_resolved(self) -> bool:
        """Download any URL-source charts that aren't already cached.

        Called from ``_load_all`` as its first step. For each of
        the three sheets:

        * If the source is empty, skip (the caller's downstream
          check surfaces the "set sources first" message).
        * If the source is a local path, copy it into the
          corresponding ``_*_path`` field. No network call.
        * If the source is a URL whose cache file already exists
          and whose manifest URL matches, set ``_*_path`` to the
          cache path. No network call.
        * Otherwise download interactively (the progress dialog
          set up by ``_load_all`` is mutated to show byte
          progress). On success, restamp the shipped cache JSON
          fingerprints to match the freshly-downloaded file's
          ``(mtime_ns, size)`` so the next load step's cache
          checks pass cleanly.

        On download failure, show the error modal
        (:class:`ChartDownloadErrorDialog`) and either retry, open
        Settings (and abort the current load), or cancel.

        Returns:
            ``True`` if all three sheets are now resolved (every
            ``_*_path`` is a real on-disk PDF). ``False`` if the
            user cancelled out or opened Settings — in either
            case the caller must stop and tear down its progress
            dialog.
        """
        sources_by_sheet: tuple[tuple[str, str], ...] = (
            ("north", self._source_north),
            ("south", self._source_south),
            ("back", self._source_back),
        )
        path_attr_by_sheet: dict[str, str] = {
            "north": "_north_path",
            "south": "_south_path",
            "back": "_back_path",
        }

        for sheet_key, source in sources_by_sheet:
            if not source:
                # Will be caught by the caller's "set sources
                # first" check. Don't try to fetch here.
                continue
            chart_src = ChartSource(raw=source)
            if chart_src.is_local_path:
                setattr(self, path_attr_by_sheet[sheet_key], source)
                continue
            if not chart_src.is_url:
                # Empty or unclassifiable — skip; caller's
                # downstream check will surface the message.
                continue
            # URL source. Either cache hit (no network) or
            # download (interactive).
            normalized = chart_src.normalized_url()
            if not needs_download(sheet_key, normalized, self._project_root):
                cached = cache_path_for_sheet(sheet_key, self._project_root)
                setattr(self, path_attr_by_sheet[sheet_key], str(cached))
                continue
            # Need to fetch. Loop on Retry until success / cancel /
            # open-settings.
            cache_path = self._download_chart_with_retry(
                sheet_key=sheet_key, url=normalized
            )
            if cache_path is None:
                return False
            setattr(
                self, path_attr_by_sheet[sheet_key], str(cache_path)
            )
            # Restamp the shipped cache JSON fingerprints so the
            # subsequent cache-validity checks (waypoint, render,
            # altitude, calibration) hit instead of triggering a
            # full regeneration cascade. Errors here are non-fatal
            # — worst case the cache misses and the user sees a
            # slower load.
            try:
                restamp_sheet_fingerprints(
                    self._project_root, sheet_key, cache_path
                )
            except (FileNotFoundError, OSError, ValueError) as exc:
                layout_diag.log(
                    "chart_source.restamp_failed",
                    sheet=sheet_key,
                    error=str(exc),
                )
        return True

    def _download_chart_with_retry(
        self, *, sheet_key: str, url: str
    ) -> Path | None:
        """Download ``url`` to its cache path, prompting the user
        on failure via :class:`ChartDownloadErrorDialog`.

        Returns the local cache :class:`Path` on success or
        ``None`` if the user gave up (Cancel) or opened Settings
        (in which case the caller should abort the current load
        and let the Settings dialog's exit hook re-trigger
        loading once the user re-saves).
        """
        dest = cache_path_for_sheet(sheet_key, self._project_root)
        while True:
            try:
                if self._progress is not None:
                    self._progress.setLabelText(
                        f"Connecting to download "
                        f"{sheet_key.capitalize()} sheet\u2026"
                    )
                    QApplication.processEvents()
                download_chart_pdf(
                    url,
                    sheet_key=sheet_key,
                    dest=dest,
                    on_progress=self._on_chart_download_progress,
                )
                # Update the manifest only after the rename
                # succeeds. (``download_chart_pdf`` does the
                # rename atomically; manifest write here means
                # "URL X is canonically the source of this
                # cached file from now on".)
                manifest = load_manifest(self._project_root)
                manifest[sheet_key] = url
                save_manifest(self._project_root, manifest)
                return dest
            except ChartFetchError as exc:
                code = self._show_chart_download_error_dialog(exc)
                if code == ACTION_RETRY:
                    continue
                if code == ACTION_OPEN_SETTINGS:
                    # Defer to the next event-loop tick so the
                    # error dialog is fully gone before Settings
                    # appears.
                    QTimer.singleShot(0, self._open_settings)
                    return None
                # Cancel / Esc / window-close.
                return None

    def _on_chart_download_progress(
        self, label: str, completed: int, total: int
    ) -> None:
        """Update the load progress dialog with download bytes.

        Called from inside :func:`chart_source.download_chart_pdf`
        (synchronous, same thread). We mutate the existing
        ``self._progress`` rather than spawning a second dialog,
        so the user sees a single uninterrupted "Loading\u2026"
        modal throughout the download → restamp → render → OCR
        sequence.

        When ``total > 0`` we switch from indeterminate spinner
        (``setRange(0, 0)``) to a determinate bar
        (``setRange(0, total)``); when the server didn't
        advertise Content-Length we stay indeterminate. Either
        way we update the label so the user sees which sheet
        is being fetched.
        """
        if self._progress is None:
            return
        self._progress.setLabelText(label)
        if total > 0:
            # First call may have total=0 then non-zero on
            # subsequent calls — switch ranges only when total
            # appears. Order matters: setMaximum before setValue
            # to avoid Qt momentarily clamping value to the old
            # range.
            if self._progress.maximum() != total:
                self._progress.setRange(0, total)
            self._progress.setValue(min(completed, total))
        # Spin the event loop so the label / value updates
        # actually paint between chunks.
        QApplication.processEvents()

    def _show_chart_download_error_dialog(
        self, exc: ChartFetchError
    ) -> int:
        """Show the error modal and return its action code.

        The action codes are
        :data:`ChartDownloadErrorDialog.ACTION_RETRY` (1301),
        :data:`ChartDownloadErrorDialog.ACTION_OPEN_SETTINGS`
        (1302), or :data:`QDialog.DialogCode.Rejected` (0)
        for Cancel / Esc / window close.

        Hides the load progress dialog before showing the error
        so the user sees a clean error modal rather than two
        stacked modals. The progress dialog is brought back
        before this method returns so the caller's load flow
        can continue (Retry case) or tear down (Cancel /
        OpenSettings case).
        """
        if self._progress is not None:
            self._progress.hide()
        dlg = ChartDownloadErrorDialog(
            parent=self,
            sheet_key=exc.sheet_key,
            failure_url=exc.url,
            failure_reason=str(exc),
            project_root=self._project_root,
        )
        code = dlg.exec()
        if self._progress is not None and code == ACTION_RETRY:
            self._progress.show()
            QApplication.processEvents()
        return code

    def _cleanup_progress_dialog(self) -> None:
        """Close and clear the shared ``self._progress`` modal.

        Idempotent — calling on an already-closed / never-created
        dialog is a no-op. Used by ``_load_all`` and the error
        paths to ensure we don't leave a stuck modal blocking the
        user's interaction with the main window."""
        if self._progress is None:
            return
        try:
            self._progress.close()
        except RuntimeError:
            # Qt object already deleted — fine.
            pass
        self._progress = None

    def select_layer(self, sheet_id: str) -> None:
        if sheet_id not in ("north", "south"):
            return
        self._selected = sheet_id
        # South paints above north; selection only affects Alt+wheel scale target.
        if self._north_item:
            self._north_item.setZValue(0.0)
        if self._south_item:
            self._south_item.setZValue(10.0)

    def scale_selected_layer(self, factor: float) -> None:
        item = self._layer_item(self._selected)
        if item is None:
            return
        br = item.boundingRect()
        item.setTransformOriginPoint(br.center())
        old_scale = float(item.scale())
        ns = max(0.12, min(8.0, item.scale() * factor))
        item.setScale(ns)
        layout_diag.log(
            "sheet.alt_wheel_scale",
            sheet=self._selected,
            factor=float(factor),
            old_scale=old_scale,
            new_scale=ns,
        )
        self._refresh_scene_rect()
        self.persist_map_layout()

    def persist_map_layout(self) -> None:
        if self._north_item is None or self._south_item is None:
            return
        layout_diag.snapshot_sheets(
            "persist_map_layout.about_to_save",
            self._north_item,
            self._south_item,
        )
        save_map_layout(
            north_x=float(self._north_item.pos().x()),
            north_y=float(self._north_item.pos().y()),
            north_scale=float(self._north_item.scale()),
            south_x=float(self._south_item.pos().x()),
            south_y=float(self._south_item.pos().y()),
            south_scale=float(self._south_item.scale()),
            selected=self._selected,
        )
        self._invalidate_geo_if_layout_mismatch()
        # Sheets just moved/scaled — re-project the route overlay so each
        # vertex stays anchored to its lat/lon rather than its old scene XY.
        self._redraw_route_overlay()
        # Traffic overlay's plane positions are projected through the same
        # calibration as the route, so a sheet move/scale shifts both. No-op
        # when the toolbar toggle is off.
        self._refresh_traffic_overlay()
        # Satellite tile items and waypoint marker items are
        # top-level scene items (not children of the chart
        # pixmap), so chart pan/scale doesn't propagate to them
        # via Qt's parent-child transform inheritance. Instead
        # each overlay registers a geometry-change listener on
        # the chart pixmap (which is a :class:`_ChartSheetItem`,
        # not a plain :class:`QGraphicsPixmapItem`) and re-flows
        # its tile / marker transforms from inside that listener
        # on every pos / scale / transform change — including
        # the intermediate ones the joint-calibration apply path
        # emits before it reaches this persist call. So nothing
        # needs to be done here for tile / marker positioning:
        # by the time persist_map_layout runs, the listeners
        # have already pushed the new transforms through.
        # And recompute the scene rect so the scroll bars actually reach the
        # new edges. ``scale_selected_layer`` does this inline for the
        # Alt+wheel escape hatch, but the joint LSQ apply path and any
        # future programmatic re-pose call into ``persist_map_layout``
        # without doing the refresh themselves — the symptom on the old
        # manual Alt+drag was that pulling sheets far apart left the
        # outer parts unreachable until a scale-tick. Doing it here keeps
        # every "the layout just changed" caller automatically covered.
        self._refresh_scene_rect()

    def _layer_item(self, sheet_id: str) -> QGraphicsPixmapItem | None:
        if sheet_id == "north":
            return self._north_item
        if sheet_id == "south":
            return self._south_item
        return None

    def lonlat_on_sheet_scene(self, sheet_id: str, lon: float, lat: float) -> QPointF | None:
        """Project lon/lat to scene coordinates when this sheet is calibrated (for overlays / tests)."""
        if sheet_id not in ("north", "south"):
            return None
        cal = self._geo_north if sheet_id == "north" else self._geo_south
        item = self._layer_item(sheet_id)
        if cal is None or item is None:
            return None
        return lonlat_to_scene(item, cal, lon, lat)

    def _clear_map_items(self) -> None:
        # Drop the route overlay first; once the sheets vanish there's no
        # calibrated frame to re-project against, so leaving the old polyline
        # or origin dot behind would only show stale geometry against the
        # next pair of sheets.
        if self._route_overlay_item is not None:
            self._scene.removeItem(self._route_overlay_item)
            self._route_overlay_item = None
        if self._route_origin_marker_item is not None:
            self._scene.removeItem(self._route_origin_marker_item)
            self._route_origin_marker_item = None
        # Traffic overlay's plane items live in the same scene and project
        # through the same calibration as the route. Drop them too so a
        # subsequent map reload + recalibration starts from a clean slate
        # rather than ghosting in the old positions.
        self._traffic_overlay.clear()
        # Tear down satellite tile overlays before the chart
        # pixmaps so the next ``_on_map_finished`` recreates the
        # pair from scratch. ``teardown`` removes every per-tile
        # item from the scene; nulling the references afterwards
        # lets a fresh ``SatelliteOverlay`` be built when the new
        # chart's calibration arrives.
        for ov in (self._north_sat_overlay, self._south_sat_overlay):
            if ov is not None:
                ov.teardown()
        self._north_sat_overlay = None
        self._south_sat_overlay = None
        # Waypoint marker overlays share the same lifecycle —
        # tear them down before the chart pixmaps disappear so
        # parented items don't dangle off a deleted parent.
        for wp_ov in (
            self._north_wp_marker_overlay,
            self._south_wp_marker_overlay,
        ):
            if wp_ov is not None:
                wp_ov.teardown()
        self._north_wp_marker_overlay = None
        self._south_wp_marker_overlay = None
        for it in (self._north_item, self._south_item):
            if it is not None:
                self._scene.removeItem(it)
        self._north_item = None
        self._south_item = None

    def _refresh_scene_rect(self) -> None:
        self._scene.setSceneRect(self._scene.itemsBoundingRect().normalized())

    @staticmethod
    def _item(display: str, sort_key: object | None = None) -> QStandardItem:
        it = QStandardItem(display)
        it.setEditable(False)
        it.setData(sort_key if sort_key is not None else display, Qt.ItemDataRole.UserRole)
        return it

    def _current_map_link_provider(self) -> str:
        d = self._map_link_provider_combo.currentData()
        return d if isinstance(d, str) else load_map_link_provider()

    def _update_code_item_tooltip(self, it: QStandardItem, r: WaypointRecord) -> None:
        prov = self._current_map_link_provider()
        label = next((lbl for pid, lbl in MAP_LINK_PROVIDERS if pid == prov), "map")
        it.setToolTip(
            f"Open {r.code} in {label} (aerial / satellite with labels, zoom ~{MAP_LINK_ZOOM:g}): "
            f"{r.lat:.6f}, {r.lon:.6f}"
        )

    def _waypoint_code_item(self, r: WaypointRecord) -> QStandardItem:
        it = self._item(r.code, r.code)
        f = QFont(it.font())
        f.setUnderline(True)
        it.setFont(f)
        # Green link styling (readable on dark table chrome).
        it.setForeground(QColor(_WAYPOINT_CODE_LINK_GREEN))
        self._update_code_item_tooltip(it, r)
        return it

    def _waypoint_name_he_item(self, r: WaypointRecord) -> QStandardItem:
        it = self._item(r.name_he or "", r.name_he or "")
        f = QFont(it.font())
        f.setUnderline(True)
        it.setFont(f)
        it.setForeground(QColor(_WAYPOINT_NAME_LINK_BLUE))
        it.setToolTip(
            "Center the map on this waypoint on the calibrated chart (keeps current zoom)."
        )
        return it

    def _refresh_code_link_tooltips(self) -> None:
        for row in range(self._wp_model.rowCount()):
            if row >= len(self._waypoints_export):
                break
            ci = self._wp_model.item(row, 0)
            if ci is not None:
                self._update_code_item_tooltip(ci, self._waypoints_export[row])

    def _on_map_link_provider_changed(self, _index: int) -> None:
        data = self._map_link_provider_combo.currentData()
        if isinstance(data, str):
            save_map_link_provider(data)
        self._refresh_code_link_tooltips()

    def _on_waypoint_table_clicked(self, index: QModelIndex) -> None:
        if not index.isValid() or index.column() not in (0, 1):
            return
        src = self._wp_proxy.mapToSource(index)
        row = src.row()
        if row < 0 or row >= len(self._waypoints_export):
            return
        r = self._waypoints_export[row]
        col = index.column()
        if col == 0:
            open_external_url(
                external_map_url(r.lat, r.lon, self._current_map_link_provider())
            )
            return
        self._center_map_on_waypoint(r)

    def _center_map_on_waypoint(self, r: WaypointRecord) -> None:
        """Pan the map view (same zoom) to the waypoint on the best matching calibrated sheet.

        Always centers on the calibrated projection, even if the result lies just past the
        depicted chart edge (south-extreme fixes like EILAT/LLER often sit outside the
        south pixmap's [0,1] UV but the user still wants to see where they would be).
        Sheet selection rules:

        - Both sheets calibrated and at least one projects **in-bounds** (within ~1% UV
          slack): pick the in-bounds sheet whose UV is closer to the chart centre.
        - No sheet projects in-bounds: pick whichever sheet **overshoots** the [0,1] UV
          window the least (smallest off-chart distance), so the user lands as close as
          possible to where the fix actually is.
        """
        if self._north_item is None or self._south_item is None:
            QMessageBox.information(self, "Map", "Load maps first.")
            return

        def projection(sheet_id: str) -> tuple[str, bool, float, float, QPointF] | None:
            cal = self._geo_north if sheet_id == "north" else self._geo_south
            item = self._layer_item(sheet_id)
            if cal is None or item is None:
                return None
            try:
                u, v = cal.lonlat_to_uv(r.lon, r.lat)
            except (ValueError, ZeroDivisionError):
                return None
            pt = lonlat_to_scene(item, cal, r.lon, r.lat)
            if pt is None:
                return None
            eps = 1e-2
            in_bounds = (-eps <= u <= 1.0 + eps) and (-eps <= v <= 1.0 + eps)
            off_u = max(0.0, -u, u - 1.0)
            off_v = max(0.0, -v, v - 1.0)
            off_dist = math.hypot(off_u, off_v)
            du, dv = u - 0.5, v - 0.5
            center_dist = math.hypot(du, dv)
            return (sheet_id, in_bounds, off_dist, center_dist, pt)

        candidates = [
            c for c in (projection(s) for s in ("north", "south")) if c is not None
        ]
        if not candidates:
            QMessageBox.information(
                self,
                "Map",
                "Calibrate the north/south charts first "
                "(Map Calibration Options… → Calibrate north/south map…).",
            )
            return

        in_bounds_pool = [c for c in candidates if c[1]]
        if in_bounds_pool:
            sid, _ib, _off, _cd, pt = min(in_bounds_pool, key=lambda c: c[3])
            on_chart = True
        else:
            sid, _ib, _off, _cd, pt = min(candidates, key=lambda c: c[2])
            on_chart = False

        self._view.centerOn(pt)
        self.select_layer(sid)
        heb = r.name_he.strip() if r.name_he else "(no Hebrew name)"
        if on_chart:
            self.statusBar().showMessage(
                f"Centered on {r.code} — {heb} ({sid} chart).",
                6000,
            )
        else:
            self.statusBar().showMessage(
                f"Centered on {r.code} — {heb}. Projection lands just off the "
                f"{sid} chart edge; pan to verify.",
                9000,
            )

    def _set_waypoint_rows(self, records: list[WaypointRecord]) -> None:
        ordered = _sort_by_name_he(list(records))
        self._waypoints_export = ordered
        self._table.setSortingEnabled(False)
        self._wp_model.removeRows(0, self._wp_model.rowCount())
        for r in ordered:
            type_text = r.reporting_type or ""
            type_item = self._item(type_text, type_text)
            type_colour = _REPORTING_TYPE_COLORS.get(type_text.strip())
            if type_colour is not None:
                type_item.setForeground(QColor(type_colour))
            row = [
                self._waypoint_code_item(r),
                self._waypoint_name_he_item(r),
                type_item,
                self._item(f"{r.lat:.6f}", r.lat),
                self._item(f"{r.lon:.6f}", r.lon),
                self._item(r.lat_dms, r.lat_dms),
                self._item(r.lon_dms, r.lon_dms),
            ]
            for i, it in enumerate(row):
                if i in (3, 4):
                    it.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
            self._wp_model.appendRow(row)
        self._table.setSortingEnabled(True)
        self._wp_proxy.sort(1, Qt.SortOrder.AscendingOrder)
        self._table.resizeColumnsToContents()
        # Re-apply the visibility state in case the model rebuild reset section
        # visibility (Qt is inconsistent here across versions / proxy models).
        self._apply_latlon_column_visibility()
        self._apply_table_natural_width()

    def _apply_latlon_column_visibility(self) -> None:
        """Show or hide the four lat/lon columns per the current checkbox state.

        Operates on the *view's* horizontal header (``setColumnHidden`` is a
        view-level call, not a model-level one), so the proxy and the model
        stay untouched — the columns are simply not painted. Filtering and
        sorting still work on the underlying values, which is the right
        behaviour: a hidden lat/lon column can still match the search box.
        """
        show = self._show_latlon_chk.isChecked()
        for col in _WAYPOINT_LATLON_COL_INDICES:
            self._table.setColumnHidden(col, not show)

    def _on_show_latlon_toggled(self, checked: bool) -> None:
        save_waypoint_show_latlon_cols(bool(checked))
        self._apply_latlon_column_visibility()
        # Hiding/showing columns changes the table's natural content width by
        # several hundred pixels — re-pin so the trailing stretch absorbs the
        # slack instead of leaving an empty band that doesn't redraw, and so a
        # narrow pane gets the horizontal scrollbar back when columns reappear.
        self._table.resizeColumnsToContents()
        self._apply_table_natural_width()

    def _apply_table_natural_width(self) -> None:
        """Pin the table's max width to exactly its content width.

        ``QHeaderView.length()`` is the same value Qt itself compares against the
        viewport when deciding whether to show the *horizontal* scrollbar, so it is
        the authoritative column total — far more reliable than summing per-section
        sizes (which can disagree with ``length()`` by a pixel or two when QSS adds
        section padding/borders that the per-section reader doesn't reflect).

        The vertical scrollbar slot is **always** reserved, not only when
        ``verticalScrollBar().isVisible()`` is currently True. Otherwise we hit a
        race: this method runs immediately after data load, before Qt finishes the
        layout pass that decides to show the scrollbar — ``isVisible()`` lies and
        returns False, we save 17 px we shouldn't, layout then adds the scrollbar
        which shrinks the viewport by exactly that amount, and a stray horizontal
        scrollbar appears. Reserving unconditionally costs at most ~17 px of empty
        viewport when the row count fits without scrolling — a much better trade
        than the spurious horizontal scrollbar the user reported.

        A small breathing-room margin (4 px) covers the QSS padding/border quirks on
        ``QHeaderView::section`` and ``QTableView::item`` so the last column never
        ends up exactly flush with the viewport edge.
        """
        if self._wp_model.columnCount() <= 0:
            return
        total = self._table.horizontalHeader().length()
        total += self._table.frameWidth() * 2
        total += self._table.verticalScrollBar().sizeHint().width()
        total += 4
        self._table.setMaximumWidth(total)

    def _load_all(self) -> None:
        # Catch the truly-unset case first — user hasn't filled in
        # Map File Settings at all. This is distinct from the
        # "URL source not yet downloaded" case (where _source_* is
        # set but _*_path is empty); the resolver below handles
        # that. Here we're catching "user opened the program for
        # the first time, the shipped defaults aren't populated
        # for some reason, AND they haven't touched Settings yet".
        if not (
            self._source_north and self._source_south and self._source_back
        ):
            QMessageBox.information(
                self,
                "Settings",
                "Set all three map sources (path or URL) in Settings\u2026 first.",
            )
            return

        self._progress = QProgressDialog(self)
        self._progress.setWindowTitle(app_title("Loading"))
        self._progress.setCancelButton(None)
        self._progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress.setMinimumDuration(0)
        self._progress.setRange(0, 0)
        self._progress.show()
        QApplication.processEvents()

        # Download any URL-sourced charts that aren't already
        # cache-hit. Returns False if the user cancelled (or
        # opened Settings to switch sources, in which case the
        # caller of Settings re-triggers ``_load_all``).
        if not self._ensure_chart_sources_resolved():
            self._cleanup_progress_dialog()
            return

        # Sources were resolved (or were already local paths); the
        # legacy "path is a real file" guard reasserts here so a
        # bad local-path source (file deleted between launches)
        # still surfaces a clear message rather than hitting
        # PyMuPDF's confusing "Cannot open file" error later.
        if not (self._north_path and self._south_path and self._back_path):
            QMessageBox.information(
                self,
                "Settings",
                "Could not resolve all three map sources. Open "
                "Settings\u2026 to check each path or URL.",
            )
            self._cleanup_progress_dialog()
            return

        cached = load_cached_waypoints(self._project_root, self._back_path)
        if cached is not None:
            # See the matching comment in
            # ``_start_map_load_after_waypoints``: after the last
            # PDF download the bar is parked at 100 %; reset back
            # to indeterminate before the next phase's label
            # change so the user sees motion, not a stuck bar.
            self._progress.setRange(0, 0)
            self._progress.setValue(0)
            self._progress.setLabelText("Loading waypoints from cache…")
            QApplication.processEvents()
            self._finalize_waypoints_load(cached, from_cache=True)
            self._start_map_load_after_waypoints()
            return

        self._waypoints_ocr_then_maps = True
        # OCR can take 30-90 seconds on the back-pages PDF; same
        # determinate-bar-parked-at-100% concern as the cache-hit
        # branch above. Indeterminate spinner conveys "still
        # working".
        self._progress.setRange(0, 0)
        self._progress.setValue(0)
        self._progress.setLabelText(
            "Scanning back-pages PDF (OCR) — ensure Tesseract + Hebrew data are installed…"
        )
        QApplication.processEvents()
        self._begin_waypoints_ocr_worker()

    def _reload_waypoints_ocr_only(self) -> None:
        if not self._back_path or not Path(self._back_path).is_file():
            QMessageBox.information(
                self,
                "Waypoints",
                "Set a valid back-pages PDF path in Settings… first.",
            )
            return
        self._waypoints_ocr_then_maps = False
        if self._progress is None:
            self._progress = QProgressDialog(self)
            self._progress.setWindowTitle(app_title("Waypoints"))
            self._progress.setCancelButton(None)
            self._progress.setWindowModality(Qt.WindowModality.WindowModal)
            self._progress.setMinimumDuration(0)
            self._progress.setRange(0, 0)
            self._progress.show()
        self._progress.setLabelText(
            "Re-scanning back-pages PDF (OCR) — ensure Tesseract + Hebrew data are installed…"
        )
        QApplication.processEvents()
        self._begin_waypoints_ocr_worker()

    def _finalize_waypoints_load(
        self,
        records_raw: list[WaypointRecord],
        *,
        from_cache: bool,
    ) -> None:
        self._records_raw = records_raw
        records_to_sqlite(records_raw, self._db)
        self._set_waypoint_rows(records_raw)
        if from_cache:
            msg = (
                f"Waypoints: {len(records_raw)} rows (cached — delete .cvfr_routemaster/"
                f'waypoints_cache.json or use "Re-OCR waypoints" to refresh).'
            )
        else:
            msg = f"Waypoints: {len(records_raw)} rows from OCR; cache updated."
        self.statusBar().showMessage(msg, 10000)

    def _begin_waypoints_ocr_worker(self) -> None:
        self._wp_ocr_thread = QThread(self)
        self._wp_ocr_worker = WaypointsOcrWorker(self._back_path)
        self._wp_ocr_worker.moveToThread(self._wp_ocr_thread)
        self._wp_ocr_thread.started.connect(self._wp_ocr_worker.run)
        self._wp_ocr_worker.finished.connect(self._on_waypoints_ocr_finished)
        self._wp_ocr_worker.failed.connect(self._on_waypoints_ocr_failed)
        self._wp_ocr_worker.finished.connect(self._wp_ocr_thread.quit)
        self._wp_ocr_worker.failed.connect(self._wp_ocr_thread.quit)
        self._wp_ocr_thread.finished.connect(self._cleanup_wp_ocr_thread)
        self._wp_ocr_thread.start()

    def _cleanup_wp_ocr_thread(self) -> None:
        if self._wp_ocr_worker:
            self._wp_ocr_worker.deleteLater()
            self._wp_ocr_worker = None
        if self._wp_ocr_thread:
            self._wp_ocr_thread.deleteLater()
            self._wp_ocr_thread = None

    def _on_waypoints_ocr_finished(self, records: object, source_tag: object) -> None:
        if not isinstance(records, list):
            self._on_waypoints_ocr_failed("Invalid OCR result.")
            return
        try:
            save_waypoint_cache(
                self._project_root,
                self._back_path,
                records,
                str(source_tag) if source_tag is not None else "hybrid",
            )
        except OSError:
            pass
        self._finalize_waypoints_load(records, from_cache=False)
        if self._waypoints_ocr_then_maps:
            self._start_map_load_after_waypoints()
        else:
            if self._progress:
                self._progress.close()
                self._progress = None

    def _on_waypoints_ocr_failed(self, msg: str) -> None:
        if self._progress:
            self._progress.close()
            self._progress = None
        QMessageBox.critical(self, "Waypoints", msg)

    def _start_map_load_after_waypoints(self) -> None:
        if self._progress:
            # Reset the bar back to indeterminate (spinner) mode.
            # After a fresh PDF download the dialog is in
            # determinate mode at 100 % (last download's
            # ``setValue(total)``), and the next phase — render —
            # has no per-tile progress emissions; without this
            # reset the bar sits at 100 % for the entire 30-90 s
            # render, which reads to the user as "the program is
            # done but the map never appeared, so it's frozen."
            # ``setRange(0, 0)`` is Qt's "indeterminate / busy"
            # mode; the bar animates a marquee strip, which is the
            # visual cue we need: "still working, not frozen."
            #
            # Order matters: setRange before setValue, because
            # ``setValue`` clamps to the current ``maximum`` and
            # we don't want a leftover-100% blip while the
            # range is mid-update.
            self._progress.setRange(0, 0)
            self._progress.setValue(0)
            self._progress.setLabelText(
                "Rendering chart images from PDF — this can take "
                "30-90 seconds on first launch. Please wait\u2026"
            )
            QApplication.processEvents()

        self._map_thread = QThread(self)
        self._map_worker = MapLoadWorker(
            self._north_path,
            self._south_path,
            project_root=self._project_root,
        )
        self._map_worker.moveToThread(self._map_thread)
        self._map_thread.started.connect(self._map_worker.run)
        self._map_worker.progress.connect(self._on_map_progress)
        self._map_worker.finished.connect(self._on_map_finished)
        self._map_worker.failed.connect(self._on_map_failed)
        self._map_worker.finished.connect(self._map_thread.quit)
        self._map_worker.failed.connect(self._map_thread.quit)
        self._map_thread.finished.connect(self._cleanup_map_thread)
        self._map_thread.start()

    def _cleanup_map_thread(self) -> None:
        if self._map_worker:
            self._map_worker.deleteLater()
            self._map_worker = None
        if self._map_thread:
            self._map_thread.deleteLater()
            self._map_thread = None
        if self._progress:
            self._progress.close()
            self._progress = None

    def _on_map_progress(self, msg: str) -> None:
        if self._progress:
            self._progress.setLabelText(msg)
        self.statusBar().showMessage(msg)

    def _on_map_finished(self, payload: object) -> None:
        if self._progress:
            self._progress.close()
            self._progress = None
        if (
            not isinstance(payload, tuple)
            or len(payload) != 2
            or payload[0] is None
            or payload[1] is None
        ):
            QMessageBox.warning(self, "Map", "Could not build map images.")
            return
        img_n, img_s = payload[0], payload[1]
        if img_n.isNull() or img_s.isNull():
            QMessageBox.warning(self, "Map", "Could not build map images.")
            return

        # Snapshot the worker's render geometry *before* the QThread tears
        # down — the altitude worker needs the same DPI + CropMeta to put
        # arrows in calibration-compatible pixmap UV.
        if self._map_worker is not None and self._map_worker.render_info:
            self._render_info_by_sheet = dict(self._map_worker.render_info)

        self._clear_map_items()

        pix_n = QPixmap.fromImage(img_n)
        pix_s = QPixmap.fromImage(img_s)
        self._north_item = _ChartSheetItem(pix_n)
        self._south_item = _ChartSheetItem(pix_s)
        _prepare_map_sheet_item(self._north_item)
        _prepare_map_sheet_item(self._south_item)
        role = MapGraphicsView.SHEET_ROLE
        self._north_item.setData(role, "north")
        self._south_item.setData(role, "south")
        layout_diag.log(
            "on_map_finished.pixmaps_ready",
            n_pw=int(pix_n.width()),
            n_ph=int(pix_n.height()),
            s_pw=int(pix_s.width()),
            s_ph=int(pix_s.height()),
        )

        layout = load_map_layout(self._project_root)
        layout_diag.log(
            "on_map_finished.load_map_layout",
            present=layout is not None,
            **(
                {
                    f"loaded_{k}": v
                    for k, v in (layout.items() if layout else ())
                }
            ),
        )
        if layout:
            self._north_item.setPos(layout["north_x"], layout["north_y"])
            self._north_item.setScale(layout["north_scale"])
            self._south_item.setPos(layout["south_x"], layout["south_y"])
            self._south_item.setScale(layout["south_scale"])
            sel = layout.get("selected", "south")
            if sel in ("north", "south"):
                self._selected = sel
        else:
            self._north_item.setPos(0.0, 0.0)
            self._north_item.setScale(1.0)
            self._south_item.setScale(1.0)
            h = float(pix_n.height())
            self._south_item.setPos(0.0, h)

        layout_diag.snapshot_sheets(
            "on_map_finished.after_apply", self._north_item, self._south_item
        )

        self._scene.addItem(self._north_item)
        self._scene.addItem(self._south_item)
        self._north_item.setZValue(0.0)
        self._south_item.setZValue(10.0)
        self.select_layer(self._selected)
        self._refresh_scene_rect()
        # If both sheets have valid saved clicks, re-run the auto-alignment
        # using the *current* math before the formal calibration loader
        # checks layout-vs-saved-map_layout. This is the upgrade path for
        # users who calibrated under an older auto-align (e.g. the v1 pass
        # that skipped the centre-origin pre-shift and left every chart
        # ~14 px west of where it should be): on next launch the corrected
        # math reapplies, syncs the saved map_layout fields, and the user
        # sees the right alignment without having to re-click anything.
        self._reapply_overlap_alignment_from_saved_clicks_if_changed()
        cal_issues = self._reload_geo_calibration_from_disk()
        # Push the joint-LSQ affine coefficients onto the freshly-
        # loaded ``self._geo_*`` cals so the satellite tile placement
        # and the waypoint marker partition both see the joint fit
        # rather than each sheet's independent-fit affine. The
        # corresponding layout was already applied by
        # :meth:`_reapply_overlap_alignment_from_saved_clicks_if_changed`
        # (using local cals before ``self._geo_*`` existed), so this
        # call only updates the in-memory affine state, leaving the
        # chart-pixmap pose alone.
        self._apply_joint_affine_overrides_at_startup()
        # The per-tile satellite overlay needs a calibration to
        # place tiles, so it's built *after* the calibration reload
        # rather than alongside the chart pixmaps. Each overlay's
        # tile items are top-level scene items (not parented under
        # the chart pixmap, see
        # :class:`cvfr_routemaster.satellite_overlay.SatelliteOverlay`
        # class docstring); the chart-to-scene transform sync
        # happens automatically via a geometry-change listener
        # the overlay registers on its
        # :class:`_ChartSheetItem`. Visibility is driven
        # separately via the toolbar toggle.
        self._build_satellite_overlays()
        # If the user already had satellite mode enabled when this
        # chart finished loading, push the current viewport rect
        # so tiles start loading immediately — without this the
        # first viewport rect arrives only after the user
        # scrolls/zooms, leaving an all-placeholder view in the
        # meantime. ``_update_satellite_visibility`` is a no-op
        # when the toggle is off, so it's always safe to call.
        QTimer.singleShot(0, self._update_satellite_visibility)
        layout_diag.snapshot_sheets(
            "on_map_finished.after_scene_setup", self._north_item, self._south_item
        )
        QTimer.singleShot(0, self._apply_saved_map_view)
        if cal_issues:
            QTimer.singleShot(
                50,
                lambda iss=list(cal_issues): self._open_calibration_instruction_dialog(iss),
            )
        self.statusBar().showMessage(
            "Maps loaded — sheet positions restored; zoom/pan restored when saved.",
            8000,
        )

        # v3 satellite-imagery: now that the chart is up and the
        # window has a non-blank backdrop, we can show the
        # first-launch consent dialog (if the user has never been
        # asked) or quietly resume an interrupted bulk fetch (if
        # they've already accepted in a prior session). Deferred a
        # tick so it stacks cleanly on top of the just-rendered
        # chart rather than racing the on_map_finished paint.
        QTimer.singleShot(0, self._satellite_check_on_map_loaded)

        # Kick off altitude-arrow extraction. Cheap on a warm cache (<50 ms)
        # and an off-thread ~30-s walk on a cold one, so this is fire-and-
        # forget: when the worker emits ``finished`` we'll refresh the
        # route panel with real altitudes; until then every leg shows
        # "unknown".
        self._start_altitude_extraction()

    def _start_altitude_extraction(self) -> None:
        """Spawn the altitude-arrow worker for both sheets if we have the
        prerequisites in place.

        Prerequisites: both PDF paths set, render-info captured from the
        most recent map load (gives us per-sheet DPI + CropMeta), and no
        prior worker still running. Failure to satisfy any of those is a
        no-op rather than a warning — the column simply stays "unknown"
        and a future trigger (e.g. user reloads maps) gets a fresh chance.
        """
        if (
            not self._north_path
            or not self._south_path
            or not self._render_info_by_sheet
        ):
            return
        if self._alt_thread is not None:
            return  # already running; results will arrive shortly

        north_info = self._render_info_by_sheet.get("north")
        south_info = self._render_info_by_sheet.get("south")
        if north_info is None or south_info is None:
            return

        self._alt_thread = QThread(self)
        self._alt_worker = AltitudeArrowsWorker(
            self._north_path,
            self._south_path,
            project_root=self._project_root,
            north_render_dpi=north_info.render_dpi,
            north_crop=north_info.crop,
            south_render_dpi=south_info.render_dpi,
            south_crop=south_info.crop,
        )
        self._alt_worker.moveToThread(self._alt_thread)
        self._alt_thread.started.connect(self._alt_worker.run)
        self._alt_worker.progress.connect(self._on_altitudes_progress)
        self._alt_worker.finished.connect(self._on_altitudes_finished)
        self._alt_worker.failed.connect(self._on_altitudes_failed)
        self._alt_worker.finished.connect(self._alt_thread.quit)
        self._alt_worker.failed.connect(self._alt_thread.quit)
        self._alt_thread.finished.connect(self._cleanup_alt_thread)
        self._alt_thread.start()

    def _cleanup_alt_thread(self) -> None:
        if self._alt_worker is not None:
            self._alt_worker.deleteLater()
            self._alt_worker = None
        if self._alt_thread is not None:
            self._alt_thread.deleteLater()
            self._alt_thread = None

    def _on_altitudes_progress(self, msg: str) -> None:
        # Surface the worker's progress in the status bar but don't block
        # the user — the GUI is fully usable while extraction runs in the
        # background, and the route panel will refresh transparently when
        # the data lands.
        self.statusBar().showMessage(msg, 4000)

    def _on_altitudes_finished(
        self,
        north_arrows: list,
        south_arrows: list,
    ) -> None:
        """Stash the per-sheet arrow lists and refresh the route panel.

        We list-cast defensively because the worker emits via Qt's signal
        marshalling — historically these arrive intact, but a defensive
        copy here costs nothing and protects the UI from any future
        signal-shape change in the worker module."""
        self._altitude_arrows_north = list(north_arrows)
        self._altitude_arrows_south = list(south_arrows)
        layout_diag.log(
            "altitudes.extracted",
            n_north=len(self._altitude_arrows_north),
            n_south=len(self._altitude_arrows_south),
        )
        self.statusBar().showMessage(
            f"Altitude data ready — {len(self._altitude_arrows_north)} north arrows, "
            f"{len(self._altitude_arrows_south)} south arrows.",
            5000,
        )
        self._refresh_route_panel()

    def _on_altitudes_failed(self, msg: str) -> None:
        # Keep the existing arrow lists so a transient failure (e.g. a
        # corrupt cache file caught mid-write) doesn't blow away values
        # the user already has on screen. Status-bar message is enough —
        # the column will show "unknown" for any segment that didn't
        # already have a match.
        self.statusBar().showMessage(msg, 8000)

    def _on_map_failed(self, msg: str) -> None:
        if self._progress:
            self._progress.close()
            self._progress = None
        QMessageBox.critical(self, "Map load failed", msg)

    def _fit_map(self) -> None:
        self._view.resetTransform()
        self._view.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def _apply_saved_map_view(self) -> None:
        layout_diag.snapshot_sheets(
            "apply_saved_map_view.enter", self._north_item, self._south_item
        )
        data = load_map_view_navigation()
        if not data:
            self._fit_map()
            layout_diag.snapshot_sheets(
                "apply_saved_map_view.exit_fit", self._north_item, self._south_item
            )
            return
        try:
            t = QTransform(
                float(data["m11"]),
                float(data["m12"]),
                float(data["m13"]),
                float(data["m21"]),
                float(data["m22"]),
                float(data["m23"]),
                float(data["m31"]),
                float(data["m32"]),
                float(data["m33"]),
            )
        except (KeyError, TypeError, ValueError):
            self._fit_map()
            layout_diag.snapshot_sheets(
                "apply_saved_map_view.exit_bad_transform",
                self._north_item,
                self._south_item,
            )
            return
        self._view.resetTransform()
        self._view.setTransform(t)
        self._view.horizontalScrollBar().setValue(int(data["scroll_h"]))
        self._view.verticalScrollBar().setValue(int(data["scroll_v"]))
        layout_diag.snapshot_sheets(
            "apply_saved_map_view.exit_restored",
            self._north_item,
            self._south_item,
        )

    def _persist_map_view_navigation(self) -> None:
        tr = self._view.transform()
        save_map_view_navigation(
            m11=float(tr.m11()),
            m12=float(tr.m12()),
            m13=float(tr.m13()),
            m21=float(tr.m21()),
            m22=float(tr.m22()),
            m23=float(tr.m23()),
            m31=float(tr.m31()),
            m32=float(tr.m32()),
            m33=float(tr.m33()),
            scroll_h=int(self._view.horizontalScrollBar().value()),
            scroll_v=int(self._view.verticalScrollBar().value()),
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        layout_diag.snapshot_sheets(
            "close_event.before_persist", self._north_item, self._south_item
        )
        # Fast parallel teardown of all worker threads. The
        # individual ``_stop_*_worker`` methods each do a polite
        # 12-30 s ``wait`` after queueing their stop signal —
        # appropriate for in-session toggle-offs (where the user
        # wants their state persisted cleanly) but a terrible UX
        # at *shutdown*: the user clicked the red X and expects
        # the window gone in well under a second. Sequential
        # waits stacked together could otherwise stall the close
        # by up to 57 s (12 + 30 + 15). ``_stop_workers_for_shutdown``
        # signals everyone in parallel, waits briefly (default
        # 2 s total budget), and ``QThread.terminate()``s any
        # straggler still mid-HTTP-call so the user always sees
        # a snappy close.
        self._stop_workers_for_shutdown()
        # The per-tile satellite overlays don't own threads —
        # they're pure scene items. ``_clear_map_items`` (called
        # implicitly by Qt's scene cleanup as the window goes
        # away) will tear them down; we don't need an explicit
        # stop here.
        if self._north_item is not None and self._south_item is not None:
            self._persist_map_view_navigation()
        # Persist window geometry + pane sizes for the next session. Done
        # in ``closeEvent`` (not on every resize) so we save exactly the
        # final frame the user had — after any drag-resize, monitor change,
        # or maximize toggle — without flooding QSettings with
        # intermediate states. ``saveGeometry`` already encodes the
        # maximized/fullscreen flag, so a window closed maximized comes
        # back maximized next time.
        try:
            save_window_layout(
                geometry=bytes(self.saveGeometry()),
                splitter_state=bytes(self._splitter.saveState()),
            )
        except Exception:  # pragma: no cover - persistence must never block close
            layout_diag.log("session.end.window_layout_save_failed")
        layout_diag.log("session.end")
        super().closeEvent(event)

    def _export_waypoints_csv(self) -> None:
        if self._records_raw is None:
            QMessageBox.information(
                self,
                "Export",
                'No waypoints loaded yet. Open "Map File Settings…" and click '
                '"Load maps & waypoints now" first.',
            )
            return
        default_name = "waypoints.csv"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export waypoints to CSV",
            str(self._project_root / default_name),
            "CSV (*.csv);;All files (*.*)",
        )
        if not path:
            return
        rows = _sort_by_name_he(list(self._waypoints_export))
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(_COLS)
                for r in rows:
                    w.writerow(
                        [
                            r.code,
                            r.name_he,
                            r.reporting_type,
                            f"{r.lat:.6f}",
                            f"{r.lon:.6f}",
                            r.lat_dms,
                            r.lon_dms,
                        ]
                    )
        except OSError as exc:
            QMessageBox.critical(self, "Export", f"Could not write the file:\n{exc}")
            return
        self.statusBar().showMessage(
            f"Exported {len(rows)} waypoint(s) to {path}.", 10000
        )


def run_app(
    project_root: Path,
    *,
    app: QApplication | None = None,
    splash: QProgressDialog | None = None,
) -> int:
    """Run the Qt event loop. Pass ``app`` (and optional ``splash``) from ``__main__`` for earliest splash."""
    # Cached map PNGs can decode to >256 MiB RGB; Qt otherwise rejects the load.
    from PySide6.QtGui import QImageReader

    try:
        QImageReader.setAllocationLimit(0)
    except AttributeError:
        pass

    own_app = app is None
    if own_app:
        app = QApplication([])
        app.setApplicationName(APP_NAME)
        app.setOrganizationName("CVFRRouteMaster")
        # Mirror the __main__.py icon-setup so the standalone
        # ``run_app`` entrypoint (used by tests and ad-hoc scripts
        # that skip the package's main()) still gets the branded
        # taskbar / Alt-Tab icon.
        from cvfr_routemaster.app_icon import app_icon

        app.setWindowIcon(app_icon())
        # ``load_font_sizes`` returns the user's saved Font Settings
        # preferences (or, on a fresh release machine, the shipped
        # defaults from ``font_settings.json``; otherwise the
        # hard-coded baseline), so the standalone entrypoint that
        # skips ``__main__.py`` still picks up the same fonts the
        # ``__main__.py`` path would have applied.
        from cvfr_routemaster.settings_store import load_font_sizes

        apply_dark_theme(app, load_font_sizes(project_root))
        splash = QProgressDialog(None)
        splash.setWindowTitle(app_title())
        splash.setLabelText("Starting…")
        splash.setRange(0, 0)
        splash.setCancelButton(None)
        splash.setWindowModality(Qt.WindowModality.ApplicationModal)
        splash.setMinimumDuration(0)
        splash.show()
        QApplication.processEvents()

    assert app is not None

    w = MainWindow(project_root)
    if splash is not None:
        splash.close()
        splash.deleteLater()
    w.show()
    QApplication.processEvents()
    return app.exec()
