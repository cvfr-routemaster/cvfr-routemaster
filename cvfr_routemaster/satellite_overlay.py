"""Per-tile satellite overlay for the chart view.

This module replaces the v3 single-pixmap "warp the whole chart"
renderer with a tile-grain overlay: one :class:`QGraphicsPixmapItem`
per Web Mercator tile, transformed into chart-pixel coordinates via
the per-tile affine fit from :mod:`satellite_overlay_math`. The
overlay's job is purely the *placement* and *lifecycle* of those
items; the math module handles geometry and the
:mod:`satellite_tiles`/:mod:`satellite_fetch` layer handles disk
I/O and HTTP.

Why per-tile instead of warp
----------------------------

The v3 warp downsamples z=14 imagery to chart-pixmap resolution
(~60–75 m/px on an Israel sheet vs the 9.5 m/px the source has),
so zooming the QGraphicsView past 100 % shows obvious nearest-
neighbour blockiness. Rendering at chart resolution wastes 6–8 ×
of available detail; rendering at native z=14 resolution would
require a ~6.5 GB output array per sheet, which is untenable.

The tile-overlay approach sidesteps the trade-off entirely:

* Each tile is drawn at its native 256 × 256 pixels via
  :class:`QGraphicsPixmapItem`. When the user zooms to 100 %,
  one tile pixel = one screen pixel — no blockiness.
* Memory scales with *visible* tiles, not chart area. The Qt
  scene graph + ``QPixmapCache`` together handle eviction.
* No big up-front render. Tiles paint lazily on first display
  in the viewport; the user sees content ~immediately on toggle.
* Missing tiles show a "Loading Tile…" placeholder until the
  fetch completes (see :mod:`satellite_overlay_fetch`); other
  tiles around them still draw, so the overlay degrades
  gracefully on an incomplete cache.

Geometry guarantee
------------------

The chart calibration is itself a 2-D affine
(:func:`geo_calibration._lsq_affine`), and a Mercator tile's 4
``(lon, lat)`` corners form an axis-aligned rectangle (lon depends
only on world-pixel-x, lat only on y), so the per-tile 3-corner
affine fit explains the 4th corner exactly to floating-point
precision — see ``test_tile_to_chart_transform_residual_is_negligible_for_affine_cal``.
Tiles align *mathematically* with the chart, no seams. The math
module tracks a per-tile residual anyway so the overlay can fall
back to placeholder for any future calibration whose residual
exceeds :data:`MAX_TILE_RESIDUAL_PX`.

Z-order
-------

Tile items are parented to the chart's :class:`QGraphicsPixmapItem`
(`_north_item` / `_south_item`). Children always paint after their
parent within the parent's z-slot, so tile items appear *above*
the chart pixmap (covering it where loaded) and *below* anything
in higher scene-level z values (routes ``z=100``, traffic
``z=200``, etc.). Pan/zoom on the chart item propagates to the
children automatically — no manual layout sync needed.

Lifecycle
---------

The overlay is constructed once per chart sheet on map-load (or
on satellite-toggle, whichever fires first), torn down on chart
clear, and exposes an :meth:`set_visible` hook tied to the
toolbar toggle. It keeps a strong reference to every tile item it
created so a panic-tear-down in :meth:`closeEvent` doesn't leak
items out from under Qt's scene-deletion path.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import QGraphicsPixmapItem

from cvfr_routemaster.satellite_overlay_math import (
    MAX_TILE_RESIDUAL_PX,
    TileTransform,
    enumerate_chart_tiles,
    tile_to_chart_transform,
)
from cvfr_routemaster.satellite_tiles import (
    TILE_SIZE_PX,
    TileCache,
    TileCoord,
    tile_for_lonlat,
    world_pixel_to_lonlat,
)

if TYPE_CHECKING:
    from cvfr_routemaster.geo_calibration import SheetGeoCalibration


@dataclass(frozen=True)
class ChartSeamPartition:
    """Encapsulates the chart-pixmap-seam partition used by the
    :class:`SatelliteOverlay` and :class:`WaypointMarkerOverlay` to
    deduplicate tiles and waypoint markers across the two chart sheets.

    What replaced the old UV-distance partition
    -------------------------------------------

    Earlier the partition compared each tile centre's UV distance to
    ``(0.5, 0.5)`` under both sheets' calibrations and gave the tile
    to whichever sheet's centre it was closer to. That worked when
    the two affines agreed on lat/lon → scene placement, but after
    the joint-LSQ fix in :func:`compute_joint_calibration` the two
    affines still leave a ~5–10 px disagreement near the overlap
    edges, and the UV-distance partition put the seam at the locus
    of equal UV distances rather than at the visible chart-pixmap
    seam. So the user saw the *satellite* tiles step at one scene_y
    (the UV-equidistant locus) while the *chart pixmaps* stepped at
    a different scene_y (south's top edge), creating visibly
    decoupled chart-vs-satellite alignment at the seam.

    The chart-seam partition forces both steps to coincide: a tile
    is owned by whichever sheet's *chart pixmap* covers it. Now the
    chart and satellite seams are at exactly the same scene_y, and
    any per-sheet affine disagreement on lat/lon shows up as a
    single combined step rather than two separately-shifted ones.

    Symmetric ownership (with one explicit asymmetric escape hatch)
    ---------------------------------------------------------------

    The partition is computed via *north's* calibration only (not
    self's, not peer's), so both overlays — north and south — make
    the same ownership decision for any given lat/lon. That
    eliminates the "owned by both" and "owned by neither" failure
    modes that would otherwise arise where the two affines
    disagree on which side of the seam a lat/lon belongs to.

    The asymmetric escape hatch: :meth:`item_owned_by_peer` accepts
    a ``north_extension_chart_px`` keyword that **only** widens
    north's territory by that many chart-px past the seam, leaving
    south's threshold at the seam unchanged. Satellite-tile
    overlays use this to have north enumerate one extra tile-row
    past the partition boundary, so the ~4–5 chart-px affine-
    disagreement gap between north's last tile and south's first
    tile gets filled by north's spill-over row. Where both sheets
    end up claiming the same tile (the overlap row), the caller
    resolves the duplicate via z-ordering (south wins, drawn on
    top — see ``sheet_z_bump`` on :class:`MultiZoomSatelliteOverlay`),
    so south's visible territory is identical to the un-extended
    partition and the only visual delta is the gap-sliver
    north now paints into. The waypoint marker overlay calls
    :meth:`item_owned_by_peer` *without* the extension keyword,
    keeping the strict-exclusive partition (no double-rendered
    markers).

    Items where north's calibration fails to project (e.g. tiles
    way outside the chart bbox) default to "peer owns" for the
    north overlay and "self owns" for the south overlay — i.e.
    the south sheet handles unprojectable lat/lons, which is
    almost always the right answer because south's coverage
    extends further south than the chart bbox check would catch.
    """

    north_calibration: "SheetGeoCalibration"
    """The north sheet's calibration, used to project lat/lon to a
    scene_y for the partition decision regardless of which sheet's
    overlay is asking. Always north's because north is pinned at
    scale 1.0 / position (0, 0), so north's ``v × H_n`` equals
    scene_y directly without any layout-composition arithmetic."""

    north_pixmap_height: float
    """Height of north's chart pixmap in pixels. Used to convert
    north's ``v`` (in [0, 1]) into scene_y for the partition
    threshold check."""

    chart_seam_scene_y: float
    """Scene-y coordinate of the chart-pixmap seam — south's chart
    pixmap's top edge after layout. Items at scene_y below this
    threshold are in north's chart-rendering territory; items at
    or below are in south's. Computed by the overlay-construction
    code as ``south_pos_y + (1 - south_scale) × south_height / 2``
    so it exactly matches Qt's
    :meth:`QGraphicsItem.transformOriginPoint` semantics."""

    self_is_north: bool
    """Which side of the seam this overlay's items live on. ``True``
    for the overlay attached to north's chart pixmap; ``False``
    for south. Inverts the inequality the
    :meth:`item_owned_by_peer` check uses."""

    def item_owned_by_peer(
        self,
        lon: float,
        lat: float,
        *,
        north_extension_chart_px: float = 0.0,
    ) -> bool:
        """Whether this overlay should *skip* an item at the given
        lat/lon because the peer sheet's overlay owns it.

        Implementation: project (lon, lat) to north's UV space,
        derive its scene_y by multiplying by north's pixmap height
        (north is pinned at identity), and compare to the seam.
        ``self_is_north`` flips the sign so both overlays use the
        same threshold but make opposite decisions about which
        side they keep.

        Parameters
        ----------
        lon, lat
            Item's geographic position.
        north_extension_chart_px
            How many chart-pixels past the seam north's territory
            is *additionally* allowed to claim, on top of its
            normal "everything above the seam" share. Asymmetric —
            this **only** widens north's claim; south's threshold
            stays at the seam unchanged. Designed so satellite-tile
            overlays can have north enumerate one extra mercator
            tile row past the partition boundary, filling the
            ~4–5 chart-px gap that the residual affine disagreement
            between the two sheets opens up between north's last
            tile (placed via north's projection) and south's first
            tile (placed via south's projection). The peer (south)
            also draws the overlap row, so where both sheets enumerate
            the same tile, the per-overlay z-ordering decides which
            wins (see ``sheet_z_bump`` on
            :class:`MultiZoomSatelliteOverlay`) — we arrange for
            south to win in the overlap so south's territory
            renders unchanged from the un-extended partition, and
            north's extra tile only shows in the narrow gap-sliver
            south's first tile doesn't reach. Default ``0.0``
            preserves strict exclusive ownership — used by the
            waypoint marker overlay where any overlap would
            visibly double-render markers.
        """
        try:
            _, v_n = self.north_calibration.lonlat_to_uv(lon, lat)
        except (ValueError, ZeroDivisionError):
            # North can't place this lat/lon (well outside the
            # chart bbox). Defensively let *south* render it —
            # that's where unprojectable lat/lons typically live
            # geographically, and the south overlay's bbox
            # enumeration would already have filtered out tiles
            # actually outside its sheet.
            return self.self_is_north
        scene_y = v_n * self.north_pixmap_height
        if self.self_is_north:
            # North claims everything strictly above its (possibly
            # extended) seam threshold. Peer (south) owns the rest.
            effective_threshold = (
                self.chart_seam_scene_y + float(north_extension_chart_px)
            )
            return scene_y >= effective_threshold
        # South. Threshold is always the un-extended seam — the
        # extension is asymmetric and only widens north's claim
        # (the peer here, from south's perspective), it does not
        # narrow south's. The overlap region — where both sheets'
        # overlays now claim the same item — is resolved by the
        # caller via z-ordering rather than by the partition.
        return scene_y < self.chart_seam_scene_y


#: Top-level scene z-value for satellite tile items.
#:
#: Tile items are *top-level* scene items (not children of the
#: chart pixmap they project onto) — necessary because in
#: QGraphicsScene a child item cannot paint above its parent's
#: top-level siblings, and the two chart pixmaps live at
#: different z values (north z=0 vs south z=10, see
#: :class:`cvfr_routemaster.main_window._ChartSheetItem` and the
#: layering rationale in :meth:`MainWindow.select_layer`). If
#: tiles were children of their chart pixmap, every north-sheet
#: tile inside the lat overlap zone would be hidden by south's
#: chart pixmap on top — that's the "missing satellite stripe
#: across the stitch zone" failure mode the user reported and
#: which a parent-child tile layout cannot fix.
#:
#: Picked above both chart pixmaps' z (>10) so tiles always
#: paint over the chart backdrop, and below the waypoint marker
#: overlay (z=20, see
#: :data:`cvfr_routemaster.waypoint_marker_overlay.WAYPOINT_MARKER_Z`)
#: so markers stay readable on top of imagery — same visual
#: layering the previous parent-child stack delivered, just
#: arranged so the result is independent of which chart pixmap
#: is currently selected as the top-z sheet.
SATELLITE_TILE_Z: float = 15.0

#: Color of the loading-placeholder tile fill. Fully transparent so
#: the underlying CVFR chart pixmap shows through anywhere the
#: satellite cache hasn't filled yet, rather than the chart being
#: blanketed in an opaque grey wall of placeholders. Tiles fade in
#: by *replacing* the transparent placeholder with the real imagery
#: as fetches land; until then the user keeps the chart-as-fallback,
#: which is exactly the right UX (the CVFR chart is the authoritative
#: navigation reference; satellite imagery is an overlay on top of
#: it). Was opaque ``QColor(96, 96, 96)`` in earlier builds — that
#: covered the chart with grey for zoom-out views where most tiles
#: at the active coarse zoom (z=12) weren't yet cached, defeating
#: the whole point of having a high-quality chart underneath.
PLACEHOLDER_FILL: QColor = QColor(0, 0, 0, 0)

#: Color of the placeholder text. With a fully-transparent fill the
#: text isn't visible either; kept for backward compatibility with
#: tests that import it.
PLACEHOLDER_TEXT: QColor = QColor(220, 220, 220, 0)

#: Default cap on the number of decoded tile pixmaps held in the
#: per-overlay LRU. At 256 × 256 × 4 bytes ≈ 192 KB / tile (Qt's
#: native pixmap stride includes alpha + alignment), 1500 tiles is
#: ~290 MB per overlay = ~580 MB total for both sheets at one zoom.
#: Sized to comfortably hold *every* tile of a fully zoomed-out
#: chart at the coarsest configured zoom (z=12 over the Israel
#: bbox is ~1300 tiles), so the zoom-out viewport — which can have
#: the whole chart's worth of tiles in view simultaneously — never
#: thrashes against the cap. At zoom-in only the visible viewport's
#: worth (~200 tiles) is loaded; the cap rarely binds in practice.
#: Callers can lower this on memory-constrained machines via the
#: constructor.
DEFAULT_LOADED_TILE_CAP: int = 1500

#: Padding factor applied to the visible scene rect before the
#: visibility test. ``0.5`` = expand by 50 % of width/height in
#: each direction. The pad keeps tiles loaded slightly off-screen
#: so panning by less than half a viewport doesn't trigger a
#: massive load/unload sweep — i.e. it's hysteresis against
#: jittery scroll events. Total area scales by ``(1 + 2*pad)²``
#: so 0.5 → 4× area; in tile-count terms a 50-tile viewport
#: becomes 200, well under the LRU cap.
DEFAULT_VISIBILITY_PAD_FACTOR: float = 0.5

#: Max number of cache-hit tiles loaded (= JPEG-decoded +
#: ``setPixmap``-applied) per :meth:`SatelliteOverlay.update_visibility`
#: call.
#:
#: Each load is a synchronous GUI-thread JPEG decode (~5 ms on a
#: modern desktop for a 256×256 web-mercator tile, often more on
#: slower machines). Without a cap, the moment the user crosses a
#: zoom-level threshold the overlay has to decode every newly-
#: visible tile at the new zoom in one call — typically 200+
#: tiles → ~1 s GUI freeze → the noticeable "snap to new zoom"
#: jank the user reported.
#:
#: 32 tiles per call keeps the worst-case per-batch latency below
#: ~200 ms (under the 250 ms threshold where Qt stops repainting
#: and the UI feels frozen); the caller schedules a follow-up
#: ``update_visibility`` ~30 ms later via
#: :meth:`MainWindow._schedule_satellite_visibility_continuation`,
#: so a full viewport's worth of fresh tiles drains over ~6
#: frames while pan / repaint events still get processed in
#: between. Tunable: raise on machines with fast SSD + JPEG
#: decoder, lower on slow machines.
DEFAULT_MAX_LOADS_PER_VISIBILITY: int = 32


def make_loading_placeholder() -> QPixmap:
    """Build the 256 × 256 transparent placeholder pixmap.

    Shared by every tile item that doesn't yet have its real
    pixmap loaded. One allocation per overlay instance; tile items
    hold a refcount so memory cost is a single tile-sized pixmap
    regardless of how many thousand tiles are pending.

    The pixmap is fully transparent: unloaded tiles draw nothing
    of their own, so the CVFR chart parented underneath remains
    visible until satellite imagery replaces it. This keeps a
    zoom-out view with a sparsely-cached coarse-zoom layer looking
    like the original chart rather than a wall of grey — the
    satellite overlay is an *additive* enhancement on top of the
    chart, never a destructive replacement.

    The placeholder is rebuilt per overlay construction (cost
    negligible, ~1 ms) rather than cached at module level because
    Qt requires a ``QApplication`` to exist before any ``QPixmap``
    is allocated, which a module-level cache would have to
    defer-evaluate.
    """
    pix = QPixmap(TILE_SIZE_PX, TILE_SIZE_PX)
    # ``fill`` with a transparent QColor requires the pixmap to
    # have an alpha channel; Qt's QPixmap is alpha-enabled by
    # default on Windows + macOS + most Linux platforms but
    # belt-and-braces calling ``fill(Qt.transparent)`` directly
    # works regardless.
    pix.fill(Qt.GlobalColor.transparent)
    return pix


def _decode_tile_pixmap(content: bytes) -> QPixmap | None:
    """Decode a tile's on-disk JPEG bytes into a :class:`QPixmap`.

    Returns ``None`` on malformed bytes; the overlay treats that
    case the same as a cache miss (placeholder stays up). PIL's
    JPEG decode would also work but :meth:`QPixmap.loadFromData`
    keeps the result on Qt's side of the buffer divide and skips
    the numpy round-trip we needed for the warp renderer.
    """
    pix = QPixmap()
    if pix.loadFromData(content):
        return pix
    return None


class SatelliteOverlay:
    """Manages the per-tile :class:`QGraphicsPixmapItem`s for one
    chart sheet.

    Construction enumerates every tile in the chart's lat/lon
    bbox, computes its per-tile affine, creates an item parented
    to the chart pixmap, applies the placeholder pixmap, and
    eagerly upgrades any tile whose JPEG bytes are already on disk
    to the real pixmap. Subsequent cache changes (e.g. a
    bulk-fetch worker filling tiles in the background) propagate
    via :meth:`refresh_from_cache`.

    The overlay holds strong references to every item it created
    so the GUI's :meth:`closeEvent` can tear them down
    deterministically; setting ``parentItem(chart_item)`` alone is
    not enough because Qt's scene cleanup happens in scene-graph
    order which doesn't always interleave correctly with
    Python-side dict cleanup.

    Tiles are *top-level* scene items
    ---------------------------------

    Tile items are added to ``chart_item.scene()`` directly
    rather than as children of ``chart_item``. This is the only
    layout that lets a tile paint above *both* chart pixmaps —
    QGraphicsScene's painter sorts items by walking up to a
    common ancestor and comparing siblings at that level, so a
    child of the north sheet's pixmap (z=0) is always painted
    before the south sheet's pixmap (z=10) and is consequently
    hidden by it in the overlap zone (was the "missing satellite
    stripe across the sheet stitch zone" failure mode the user
    reported when we tried per-sheet partitioning under the
    original child-of-chart parenting). At top level with a
    z-value above both chart pixmaps, tiles paint over both —
    independently of which chart pixmap currently sits on top.

    Because tiles are no longer children of the chart pixmap,
    Qt's parent-child transform inheritance doesn't propagate
    chart pan / scale to them automatically. Instead each tile's
    transform is computed once at construction as
    ``chart_item.sceneTransform() * tile_to_chart_transform`` and
    re-applied whenever the chart pixmap's geometry changes —
    the overlay registers a listener on ``chart_item`` (which is
    expected to be a
    :class:`cvfr_routemaster.main_window._ChartSheetItem`) and the
    listener invokes :meth:`_apply_chart_transform` on every
    pos / scale / rotation update — including the intermediate
    ones from the joint LSQ apply step and from any future
    programmatic re-pose path. (Historically this also covered
    a live Alt+drag, which has been removed; the listener is
    still required by the remaining transform-mutation paths.)
    """

    def __init__(
        self,
        *,
        chart_item: QGraphicsPixmapItem,
        calibration: "SheetGeoCalibration",
        pixmap_size: tuple[int, int],
        target_zoom: int,
        tile_cache: TileCache,
        placeholder: QPixmap | None = None,
        loaded_tile_cap: int = DEFAULT_LOADED_TILE_CAP,
        visibility_pad_factor: float = DEFAULT_VISIBILITY_PAD_FACTOR,
        tile_z_value: float = SATELLITE_TILE_Z,
        chart_seam_partition: ChartSeamPartition | None = None,
    ) -> None:
        """Build the overlay for one chart sheet.

        Parameters
        ----------
        chart_item
            The chart pixmap item whose calibration places these
            tiles. Used for: (a) reading
            ``chart_item.sceneTransform()`` so the top-level tile
            items track chart pan / scale; (b) discovering
            ``chart_item.scene()`` to add tiles to; (c) registering
            a geometry-change listener if ``chart_item`` is a
            :class:`cvfr_routemaster.main_window._ChartSheetItem`.
            Plain :class:`QGraphicsPixmapItem` instances work too —
            transforms are captured at construction and the
            overlay just doesn't update on later chart moves
            (acceptable for tests, where the chart is stationary).
        calibration
            The chart's :class:`SheetGeoCalibration`. Used to
            place each tile in chart-pixel coords; not stored
            beyond construction (per-tile transforms are
            pre-computed and cached per item).
        pixmap_size
            ``(width, height)`` of the chart pixmap, in pixels.
            Defines the chart-pixel coordinate space the tile
            transforms are expressed in.
        target_zoom
            Web Mercator zoom level to draw at. Determines tile
            count + per-tile ground extent.
        tile_cache
            The disk-backed tile cache. The overlay only *reads*
            from it; writes are someone else's responsibility
            (bulk-fetch worker, on-demand fetch worker).
        placeholder
            Optional pre-built placeholder pixmap. Tests pass a
            fixture pixmap to skip the QPainter call (which is
            slow under headless Qt). Production callers pass
            ``None`` and the overlay builds its own.
        loaded_tile_cap
            Max number of decoded tile pixmaps held in the
            per-overlay LRU. Excess loads evict the least-recently-
            visible tile back to placeholder. Default
            :data:`DEFAULT_LOADED_TILE_CAP`.
        visibility_pad_factor
            Hysteresis pad applied to the viewport rect before the
            visibility check. See
            :data:`DEFAULT_VISIBILITY_PAD_FACTOR`. Use ``0.0`` to
            test exact viewport boundaries (no hysteresis).
        tile_z_value
            :meth:`QGraphicsItem.setZValue` applied to every item
            built by this overlay. Since tiles are *top-level*
            scene items (see class docstring), this is the
            absolute scene z — not relative to a chart pixmap
            parent. :class:`MultiZoomSatelliteOverlay` stacks
            zoom layers in coarse-under-fine order by passing a
            ``tile_z_value`` slightly offset per zoom level
            (e.g. ``SATELLITE_TILE_Z + zoom * 0.01``) so finer
            tiles paint over coarser tiles, giving the
            "fall back to coarse zoom while finer hasn't loaded
            yet" behaviour for free out of normal Qt scene-graph
            painting — no compositing code on our side. Defaults
            to :data:`SATELLITE_TILE_Z` for standalone (single-
            zoom) overlay usage.
        chart_seam_partition
            Optional :class:`ChartSeamPartition` describing the
            location of the chart-pixmap seam and which side this
            overlay's sheet sits on. When set, each tile's
            ownership is decided by where its centre's projected
            scene_y falls relative to the seam: tiles above the
            seam are owned by the north sheet's overlay; tiles
            at or below by the south sheet's overlay. See
            :class:`ChartSeamPartition` for the full rationale —
            in short, this aligns the satellite-tile seam with
            the visible chart-pixmap seam at the same scene_y,
            so any per-sheet affine residual produces a single
            combined chart-and-satellite step rather than two
            separately-shifted ones the user would perceive as
            decoupled.

            Crucially, the partition uses *north's* calibration
            for the threshold check regardless of which sheet
            this overlay belongs to, so both sheets' overlays
            make identical ownership decisions for any lat/lon —
            no tile is "owned by neither" or "owned by both".
            That symmetric property is what eliminates both the
            missing-satellite-stripe failure mode and the
            double-rendered overlap-zone tiles.

            Pass ``None`` (default) for the legacy "every tile
            in the chart bbox" semantics — useful for tests and
            single-sheet setups.
        """
        self._chart_item = chart_item
        self._calibration = calibration
        self._chart_seam_partition = chart_seam_partition
        self._pixmap_size = pixmap_size
        self._target_zoom = target_zoom
        self._tile_cache = tile_cache
        self._placeholder: QPixmap = (
            placeholder if placeholder is not None else make_loading_placeholder()
        )
        self._loaded_cap = int(loaded_tile_cap)
        self._visibility_pad = float(visibility_pad_factor)
        self._tile_z_value = float(tile_z_value)
        # Chart-px height of one mercator tile-row at the chart-seam
        # latitude for this overlay's target zoom. Used as the
        # ``north_extension_chart_px`` argument when consulting the
        # partition, so north's territory is widened by exactly one
        # tile-row past the seam — closing the ~4 chart-px affine-
        # disagreement gap that otherwise opens between north's
        # last tile and south's first tile at the partition boundary.
        # Computed once at construction (depends only on the seam
        # lat + the target zoom, not on per-tile geometry) and
        # cached because :meth:`_tile_owned_by_peer` is called once
        # per enumerated tile and the math is non-trivial enough to
        # not want to repeat per call. ``0.0`` whenever the partition
        # is ``None`` (single-sheet usage / tests) or when the
        # seam-lat tile height couldn't be derived (defensive
        # fallback — falls back to strict-exclusive partition rather
        # than crashing the overlay).
        self._tile_partition_extension_chart_px: float = (
            self._compute_seam_tile_height_chart_px()
            if chart_seam_partition is not None
            else 0.0
        )
        self._items: dict[TileCoord, QGraphicsPixmapItem] = {}
        # Per-tile QTransform from tile-local pixmap pixels into
        # chart-pixel coords (i.e. as if the chart pixmap had an
        # identity scene transform). Cached so
        # :meth:`_apply_chart_transform` can re-apply
        # ``chart_item.sceneTransform() * local`` cheaply on every
        # chart move event — recomputing the
        # :func:`tile_to_chart_transform` would project ten thousand
        # tile corners per drag-step.
        self._tile_local_transforms: dict[TileCoord, QTransform] = {}
        # LRU of currently-loaded coords. Recency = "last touched
        # by an update_visibility() call". OrderedDict gives O(1)
        # move-to-end + O(1) popitem(last=False) which is exactly
        # what an LRU eviction policy needs.
        self._loaded_lru: OrderedDict[TileCoord, None] = OrderedDict()
        self._visible_default: bool = False
        # Most recent viewport rect (in scene coords) seen by
        # update_visibility. ``refresh_from_cache`` consults this
        # to decide whether a freshly-fetched tile should be loaded
        # immediately (currently visible) or wait for the next
        # viewport pass (off-screen). ``None`` until the first
        # update_visibility call — in that case
        # refresh_from_cache loads conservatively (i.e. always),
        # because we can't yet rule the tile out as off-screen.
        self._last_visible_rect: QRectF | None = None

        self._build_items(calibration)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build_items(
        self, calibration: "SheetGeoCalibration"
    ) -> None:
        """Enumerate tiles, compute transforms, create items as
        placeholders.

        No pixmaps are loaded here — tile loading is lazy, driven by
        :meth:`update_visibility` once the GUI knows which tiles the
        user is actually looking at. Eagerly creating items is cheap
        (~5 KB of QGraphicsItem state per tile, ~25 MB total for
        an Israel-coverage sheet at z=14) and lets the per-tile
        affines + transforms be computed once at startup rather
        than on every viewport change.

        Tiles whose per-tile residual exceeds
        :data:`MAX_TILE_RESIDUAL_PX` are silently skipped — for the
        current calibration this can't happen (the residual is
        floating-point zero), but the check guards future
        calibrations whose projection isn't a global affine.
        """
        for coord in enumerate_chart_tiles(
            calibration, self._pixmap_size, self._target_zoom
        ):
            if (
                self._chart_seam_partition is not None
                and self._tile_owned_by_peer(coord)
            ):
                # Tile centre falls on the peer sheet's side of the
                # chart-pixmap seam (in scene-y), past the one-row
                # extension we grant north — the peer's overlay
                # places it. Skipping here keeps the satellite-tile
                # seam coincident with the chart-pixmap seam (modulo
                # the controlled one-row overlap that backfills the
                # affine-disagreement gap), so chart-on-chart and
                # sat-on-sat steps land at the same scene-y instead
                # of producing two visibly decoupled rendering
                # boundaries with a hairline gap between them.
                continue
            try:
                tt = tile_to_chart_transform(
                    coord, calibration, self._pixmap_size
                )
            except (ValueError, ZeroDivisionError):
                # Defensive — collinear projection or weird edge
                # case. Skip; the placeholder gap is preferable to
                # crashing the whole overlay.
                continue
            if tt.residual_px > MAX_TILE_RESIDUAL_PX:
                continue
            item = self._make_tile_item(coord, tt)
            self._items[coord] = item
        # Now that ``self._items`` is fully populated, push the
        # current chart geometry through every tile in one pass.
        # Sets each tile's absolute scene transform from
        # ``chart_item.sceneTransform() * local`` — see
        # :meth:`_apply_chart_transform` for the math.
        self._apply_chart_transform()
        # And subscribe to chart geometry changes so subsequent
        # pan / scale / rotate updates re-flow into the tiles.
        # Plain ``QGraphicsPixmapItem`` chart items (test
        # fixtures) don't expose the listener API; we don't
        # subscribe in that case, which is fine because their
        # geometry never changes.
        add_listener = getattr(
            self._chart_item, "add_geometry_listener", None
        )
        if callable(add_listener):
            add_listener(self._apply_chart_transform)

    def _tile_owned_by_peer(self, coord: TileCoord) -> bool:
        """Return ``True`` iff the peer sheet's overlay should
        render this tile instead of ours, under the chart-seam
        partition.

        Concretely:

        1. Invert web-mercator on the tile centre to get
           ``(lon, lat)``.
        2. Forward the decision to
           :meth:`ChartSeamPartition.item_owned_by_peer`, with
           ``north_extension_chart_px`` set to one mercator
           tile-row's chart-px height at the seam latitude for
           this overlay's target zoom — see
           :attr:`_tile_partition_extension_chart_px` for how that
           value is derived. The extension widens *north's*
           territory by one tile-row past the seam (south's
           threshold stays put), so the residual affine-
           disagreement gap between north's last tile and south's
           first tile gets covered by north's spill-over row. In
           the tile-row that ends up claimed by both, south's
           overlay wins via the per-sheet z-bump applied by
           :class:`MultiZoomSatelliteOverlay`, so south's visible
           territory is identical to the un-extended partition.

        Pure math, no Qt; cheap enough to call once per tile at
        construction. See :class:`ChartSeamPartition` for the
        symmetry argument that bounds the overlap to a single
        tile-row.
        """
        assert self._chart_seam_partition is not None  # noqa: S101
        cx_world = (coord.x + 0.5) * TILE_SIZE_PX
        cy_world = (coord.y + 0.5) * TILE_SIZE_PX
        lon, lat = world_pixel_to_lonlat(cx_world, cy_world, coord.z)
        return self._chart_seam_partition.item_owned_by_peer(
            lon,
            lat,
            north_extension_chart_px=self._tile_partition_extension_chart_px,
        )

    def _compute_seam_tile_height_chart_px(self) -> float:
        """Chart-pixel height of one mercator tile-row at the
        chart-seam latitude, for ``self._target_zoom``.

        Driving the ``north_extension_chart_px`` argument to the
        partition. Computed once at construction because the seam
        latitude and target zoom are both fixed for the life of
        the overlay, and the routine projects through north's
        calibration twice + does two web-mercator inversions —
        cheap enough to do per-construction but worth not
        repeating per tile.

        Returns 0.0 if the partition's north calibration can't
        project the inverted seam-lat back (only happens for a
        pathological calibration whose UV bbox is degenerate);
        in that case the overlay silently falls back to strict-
        exclusive partition rather than crashing the build.
        """
        assert self._chart_seam_partition is not None  # noqa: S101
        partition = self._chart_seam_partition
        H_n = partition.north_pixmap_height
        if H_n <= 0.0:
            return 0.0
        # Seam scene_y → north's UV → lon/lat. North is pinned at
        # identity, so v_n × H_n == scene_y directly. The seam_lon
        # picked from u=0.5 is incidental — we only need *some*
        # lon to evaluate the tile column; tile rows at a given
        # zoom span the same lat range across every column, so the
        # height we get out is column-independent (modulo
        # floating-point noise from projecting through the affine
        # twice — well under 1 chart-px for any realistic cal).
        v_n_seam = partition.chart_seam_scene_y / H_n
        try:
            seam_lon, seam_lat = partition.north_calibration.uv_to_lonlat(
                0.5, v_n_seam
            )
        except (ValueError, ZeroDivisionError):
            return 0.0
        # Locate the boundary tile at this lat/lon + our target
        # zoom, then read its top/bottom edges back to lat via
        # web-mercator inversion at the tile-corner world-pixel
        # rows.
        try:
            boundary_tile = tile_for_lonlat(
                seam_lon, seam_lat, self._target_zoom
            )
        except (ValueError, ZeroDivisionError):
            return 0.0
        cx_world = (boundary_tile.x + 0.5) * TILE_SIZE_PX
        py_top = boundary_tile.y * TILE_SIZE_PX
        py_bot = (boundary_tile.y + 1) * TILE_SIZE_PX
        try:
            _, lat_top = world_pixel_to_lonlat(
                cx_world, py_top, self._target_zoom
            )
            _, lat_bot = world_pixel_to_lonlat(
                cx_world, py_bot, self._target_zoom
            )
        except (ValueError, ZeroDivisionError):
            return 0.0
        # Project the two tile-edge lats back through north's
        # calibration to scene_y, take the absolute difference.
        # ``seam_lon`` for both projections so the column drops
        # out — we want a pure row-height.
        try:
            _, v_top = partition.north_calibration.lonlat_to_uv(
                seam_lon, lat_top
            )
            _, v_bot = partition.north_calibration.lonlat_to_uv(
                seam_lon, lat_bot
            )
        except (ValueError, ZeroDivisionError):
            return 0.0
        return abs(v_bot - v_top) * H_n

    def _make_tile_item(
        self, coord: TileCoord, tt: TileTransform
    ) -> QGraphicsPixmapItem:
        """Allocate one tile item, position it in scene coords, set
        the placeholder pixmap.

        Tile items are *top-level* scene items (parent = ``None``)
        added directly to ``chart_item.scene()``. The per-tile
        :class:`TileTransform` provides chart-pixel coordinates,
        which are pre-composed with ``chart_item.sceneTransform()``
        in :meth:`_apply_chart_transform` (called once at the end
        of :meth:`_build_items` once every tile exists, and again
        on every chart geometry change). See the class docstring
        for the rationale on top-level parenting.
        """
        item = QGraphicsPixmapItem(self._placeholder)
        # The 9-coefficient projective transform packed into Qt's
        # row-major constructor order — the chart-pixel-space
        # transform. The scene-space transform is composed from
        # this and the chart pixmap's current sceneTransform in
        # :meth:`_apply_chart_transform`; we stash the local one
        # here so we don't have to project tile corners through
        # ``tile_to_chart_transform`` again every time the chart
        # moves.
        #
        # Projective (not just affine) because the v3 LCC pipeline
        # makes the Mercator-tile → chart-scene map non-linear over
        # a tile's extent. An 8-DOF homography fitted through all 4
        # tile corners places each corner at its true scene position
        # exactly, which closes the thin-line seam artifact that an
        # affine fit (off by the LCC residual at the held-out 4th
        # corner) produces at every tile boundary. For inputs that
        # happen to be exactly affine, the homography solver
        # returns ``m13 = m23 = 0`` and ``m33 = 1``, reducing the
        # 9-arg QTransform constructor below to an affine identity.
        (
            m11, m12, m13,
            m21, m22, m23,
            m31, m32, m33,
        ) = tt.to_qtransform_components()
        local = QTransform(
            m11, m12, m13,
            m21, m22, m23,
            m31, m32, m33,
        )
        self._tile_local_transforms[coord] = local
        item.setZValue(self._tile_z_value)
        # Default invisible — the toolbar toggle flips this later.
        # If we created the overlay while the toggle was already on,
        # ``set_visible`` is called immediately after construction.
        item.setVisible(self._visible_default)
        # Add to the same scene the chart pixmap lives in. Tests
        # that construct overlays before adding the chart pixmap
        # to a scene (rare; the test fixture in
        # ``chart_setup`` does the addItem first) get ``None`` here
        # and the tile simply isn't visible until something else
        # adds it — same failure mode as today, just shifted by
        # one indirection. Production always has the scene
        # available because :func:`MainWindow._build_satellite_overlays`
        # runs after the chart pixmaps are inserted.
        scene = self._chart_item.scene()
        if scene is not None:
            scene.addItem(item)
        # ``ItemUsesExtendedStyleOption`` would be set if we needed
        # the per-paint rect for LOD; we don't (vanilla
        # QGraphicsPixmapItem painting is fine), so we skip it.
        return item

    def _apply_chart_transform(self) -> None:
        """Recompute every tile's scene transform from the
        composition of ``local_transform`` and
        ``chart_item.sceneTransform()``.

        Invoked once at the end of :meth:`_build_items` to set
        the initial placement, and again from the geometry
        listener attached to ``chart_item`` whenever the chart
        pixmap's pos / scale / rotation / transform changes —
        every step of the joint-calibration layout apply, every
        Alt+wheel scale-tick, layout reset, and layout load.

        Math: a top-level item ``T`` with ``pos=(0,0)`` and
        ``transform=M`` has ``T.sceneTransform() == M``. We want
        the tile's effective scene mapping to equal what it
        would be if it were a child of ``chart_item`` with
        ``transform=local`` — i.e. *apply ``local`` first*
        (tile-pixels → chart-pixels), *then ``chart_st``*
        (chart-pixels → scene). In Qt's :class:`QTransform`
        algebra ``A * B`` means *apply A first, then B*
        — the LEFT-multiply / row-vector convention, opposite
        to the right-multiply convention you'd see in a textbook
        — so the correct composition is ``local * chart_st``,
        not the other way around. Verified empirically: with
        the natural order swapped, a pure ``chart_item.setPos``
        translation produces a tile-transform delta scaled by
        the calibration's rotation / scale rather than the bare
        translation magnitude.

        Cost: one ``QTransform`` multiply + ``setTransform`` per
        tile per call. With ~10 k tiles in the worst case (3
        zooms × 2 sheets × ~1.7 k tiles each at z=14 over
        Israel), this is ~10⁴ small-matrix mults per chart move
        event — well under a millisecond on any modern CPU and
        cheap compared to the JPEG decoding that
        :meth:`update_visibility` triggers anyway.
        """
        chart_st = self._chart_item.sceneTransform()
        for coord, item in self._items.items():
            local = self._tile_local_transforms.get(coord)
            if local is None:
                # Shouldn't happen — every tile is built with a
                # cached local transform — but be defensive
                # because a missing entry would otherwise leave
                # the tile at identity and visibly drift away
                # from the chart.
                continue
            item.setTransform(local * chart_st)

    # ------------------------------------------------------------------
    # Cache integration
    # ------------------------------------------------------------------

    def _load_tile_into_item(
        self,
        coord: TileCoord,
        item: QGraphicsPixmapItem,
    ) -> bool:
        """Read ``coord`` from the disk cache, decode, install on
        ``item``. Adds the coord to the LRU on success.

        Returns ``True`` when a pixmap was installed; ``False`` for
        cache miss or decode failure (caller leaves the placeholder
        in place either way). Doesn't enforce the LRU cap — that's
        the caller's job after a batch of loads, so we don't
        thrash the cap on every single-tile call.
        """
        content = self._tile_cache.get(coord)
        if content is None:
            return False
        pix = _decode_tile_pixmap(content)
        if pix is None:
            return False
        item.setPixmap(pix)
        # Adding (or moving) ``coord`` to the end of the LRU marks
        # it most-recently-used; popping from the front evicts the
        # oldest. ``OrderedDict[coord] = None`` works for both
        # insert and move-to-end, so a single line covers both
        # the "first load" and "re-load after eviction" paths.
        self._loaded_lru[coord] = None
        self._loaded_lru.move_to_end(coord)
        return True

    def _evict_to_cap(self) -> int:
        """Pop LRU entries until the count is back under the cap.

        Returns the eviction count for status-bar / test
        bookkeeping. Each evicted item gets the placeholder back —
        the QGraphicsItem stays put (it has the right transform
        and parent), only the pixmap is swapped, so re-loading
        later is just a JPEG decode.
        """
        evicted = 0
        while len(self._loaded_lru) > self._loaded_cap:
            coord, _ = self._loaded_lru.popitem(last=False)
            self._items[coord].setPixmap(self._placeholder)
            evicted += 1
        return evicted

    def update_visibility(
        self,
        scene_rect: QRectF,
        max_loads: int | None = None,
    ) -> tuple[int, int, list[TileCoord], bool]:
        """Sync each tile's pixmap state to whether it's in
        ``scene_rect`` (with hysteresis pad).

        This is the primary hook driving tile loading. Wired into
        the GUI's QGraphicsView scroll/resize/zoom signals (see
        :meth:`MainWindow._update_satellite_visibility`); fires
        every time the visible scene rect could have changed.

        Algorithm
        ---------

        1. Pad ``scene_rect`` by :attr:`_visibility_pad`.
        2. Walk every item; if its scene-bounding-rect intersects
           the padded rect, it's "visible".
        3. For visible tiles already in the LRU: ``move_to_end``
           (mark as most-recently-used).
        4. For visible tiles not in the LRU: try to load from
           cache. Success increments :attr:`loaded_count`; failure
           appends the coord to ``visible_misses`` (caller can
           hand these to the on-demand fetch worker).
        5. After the walk, evict LRU tiles down to
           :attr:`_loaded_cap`. Evicted items revert to placeholder.

        Returns
        -------
        ``(loaded_now, evicted_now, visible_misses, more_pending)``

        ``more_pending`` is ``True`` iff we hit the per-call load
        cap while there were still visible cache-hit tiles that
        needed loading — the caller is expected to schedule
        another ``update_visibility`` call shortly so the
        remaining tiles get loaded in a subsequent batch (this is
        how zoom-level switches stay smooth: a 200-tile load
        spread across 6 frames feels instantaneous, while
        doing it in one frame freezes the GUI for ~1 s).

        Parameters
        ----------
        scene_rect
            Current viewport in scene coords.
        max_loads
            Hard cap on the number of cache-hit tiles to
            ``_load_tile_into_item`` during this call (each
            load is a JPEG decode + ``setPixmap``, ~5 ms each
            on a modern desktop). When the cap is hit, the
            remaining visible cache-hit tiles stay on their
            placeholder for this pass and ``more_pending`` flips
            to ``True``. ``None`` (the default) means no cap —
            useful for tests that want a deterministic "all
            visible loaded after one call" state. Production
            callers pass :data:`DEFAULT_MAX_LOADS_PER_VISIBILITY`
            (32) which is sized so the worst-case batch latency
            (~160 ms) stays below the 250 ms threshold where Qt
            stops repainting and the UI feels frozen.

        Performance
        -----------

        A 10 k-item walk takes ~10 ms on a modern desktop (each
        iteration is a rect intersection + dict lookup). Callers
        should still throttle / debounce to avoid running this on
        every scroll-pixel; ~100 ms throttle in
        :meth:`MainWindow._schedule_satellite_visibility_update`
        is the right cadence for a smooth pan.
        """
        pad_x = scene_rect.width() * self._visibility_pad
        pad_y = scene_rect.height() * self._visibility_pad
        padded = scene_rect.adjusted(-pad_x, -pad_y, pad_x, pad_y)
        self._last_visible_rect = QRectF(padded)

        loaded_now = 0
        visible_misses: list[TileCoord] = []
        more_pending = False
        for coord, item in self._items.items():
            if not item.sceneBoundingRect().intersects(padded):
                continue
            if coord in self._loaded_lru:
                self._loaded_lru.move_to_end(coord)
                continue
            if max_loads is not None and loaded_now >= max_loads:
                # Cap hit: stop loading but keep walking the
                # remaining items so we don't miss any
                # ``visible_misses`` (those go to the on-demand
                # fetcher and aren't gated by the decode budget —
                # network latency dominates them, not GUI-thread
                # work). The next ``update_visibility`` call
                # will pick up the un-loaded cache hits.
                more_pending = True
                continue
            if self._load_tile_into_item(coord, item):
                loaded_now += 1
            else:
                # Visible + not in LRU + not in cache → caller
                # may want to fetch it on-demand. We don't fire
                # the request ourselves so the overlay stays
                # decoupled from the fetch layer; main_window
                # routes these to ``OnDemandFetchWorker``.
                visible_misses.append(coord)
        evicted_now = self._evict_to_cap()
        return loaded_now, evicted_now, visible_misses, more_pending

    def refresh_from_cache(
        self, only_coords: Iterable[TileCoord] | None = None
    ) -> int:
        """Load freshly-cached tiles that are currently visible.

        Called by the bulk-fetch worker on every successful tile
        write (with a single-coord whitelist) and by the toolbar
        toggle-on path (no whitelist → full sheet). Lazy
        semantics: a tile is only loaded if it's in the most
        recent visible rect (or no rect has been seen yet — in
        which case we conservatively load everything available,
        because we don't yet have a way to rule the tile out).

        Parameters
        ----------
        only_coords
            Optional whitelist of coords to check. ``None`` walks
            every item — useful on toggle-on when we want every
            in-cache visible tile to load immediately.

        Returns
        -------
        int
            Number of tiles that transitioned from placeholder to
            loaded in this call.
        """
        last_rect = self._last_visible_rect
        if only_coords is None:
            if last_rect is None:
                # Walking every item with no visible-rect filter
                # would decode every cached tile in the layer —
                # at z=15 that's ~80k JPEG decodes on the GUI
                # thread, freezing the app for many seconds and
                # producing the "Not Responding" window the user
                # reported when a bulk-fetch worker emitted
                # ``finished`` for a layer whose viewport had
                # never been pushed (typical at startup for
                # layers above the active zoom, which
                # ``MultiZoomSatelliteOverlay.update_visibility``
                # deliberately skips activating). Callers that
                # genuinely want a visibility-blind load use
                # :meth:`eager_load_all_cached` instead — this
                # path is reserved for "refresh the *visible*
                # tiles, walking every item to find which are
                # visible". Without a visible-rect to filter
                # against, the answer is "no tiles are known to
                # be visible", so we load nothing.
                return 0
            coords: Iterable[TileCoord] = self._items.keys()
        else:
            coords = [c for c in only_coords if c in self._items]

        loaded_now = 0
        for coord in coords:
            if coord in self._loaded_lru:
                # Already loaded — touch the LRU so a freshly-
                # fetched in-cache tile that the user is currently
                # looking at doesn't get evicted in the same
                # update.
                self._loaded_lru.move_to_end(coord)
                continue
            item = self._items[coord]
            if last_rect is not None and not item.sceneBoundingRect().intersects(
                last_rect
            ):
                continue
            if self._load_tile_into_item(coord, item):
                loaded_now += 1
        if loaded_now:
            self._evict_to_cap()
        return loaded_now

    def eager_load_all_cached(self) -> int:
        """Visibility-blind load of every cached tile.

        Bypasses the lazy + LRU machinery — used by tests that
        want a deterministic "all cached tiles loaded" state, and
        by the (deprecated, but kept for parity) "load everything
        before showing" code path. In production the user gets
        better latency from lazy loading, so the GUI never calls
        this; the LRU cap *does* apply (evicts excess) so memory
        is still bounded.
        """
        loaded_now = 0
        for coord, item in self._items.items():
            if coord in self._loaded_lru:
                continue
            if self._load_tile_into_item(coord, item):
                loaded_now += 1
        if loaded_now:
            self._evict_to_cap()
        return loaded_now

    # ------------------------------------------------------------------
    # Visibility
    # ------------------------------------------------------------------

    def set_visible(self, on: bool) -> None:
        """Show/hide every tile item in lockstep with the toolbar
        toggle. Idempotent."""
        self._visible_default = bool(on)
        for item in self._items.values():
            item.setVisible(bool(on))

    def is_visible(self) -> bool:
        return self._visible_default

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def teardown(self) -> None:
        """Remove every tile item from the scene and drop our refs.

        Called from the GUI's chart-clear path and from
        :meth:`closeEvent`. Idempotent — safe to call multiple
        times. After teardown the overlay is unusable; callers
        should construct a fresh instance.

        Deregisters the chart-geometry listener (if registered)
        before removing items, so a stray geometry-change event
        in the middle of teardown can't fire
        :meth:`_apply_chart_transform` against half-dismantled
        state.
        """
        remove_listener = getattr(
            self._chart_item, "remove_geometry_listener", None
        )
        if callable(remove_listener):
            remove_listener(self._apply_chart_transform)
        for item in self._items.values():
            scene = item.scene()
            if scene is not None:
                scene.removeItem(item)
            # Tiles are no longer parented to chart_item (see
            # :class:`SatelliteOverlay` docstring) but keep the
            # null-parent call defensively in case a future
            # change re-parents them — Qt's child-list otherwise
            # keeps the item alive.
            item.setParentItem(None)
        self._items.clear()
        self._tile_local_transforms.clear()
        self._loaded_lru.clear()

    # ------------------------------------------------------------------
    # Inspection (mostly for tests)
    # ------------------------------------------------------------------

    def tile_count(self) -> int:
        """Total number of tile items in this overlay (excluding
        any skipped due to residual)."""
        return len(self._items)

    def loaded_count(self) -> int:
        """Number of tile items currently displaying real pixmaps
        (rest show placeholders)."""
        return len(self._loaded_lru)

    def has_tile(self, coord: TileCoord) -> bool:
        """Whether the overlay has an item for ``coord`` (i.e. the
        coord falls inside the chart's bbox at our target zoom)."""
        return coord in self._items

    def is_tile_loaded(self, coord: TileCoord) -> bool:
        """Whether the item for ``coord`` currently has a real
        pixmap (vs the placeholder)."""
        return coord in self._loaded_lru

    def missing_coords(self) -> list[TileCoord]:
        """Return the list of tile coords that have an item but no
        real pixmap. Used by the on-demand fetch worker (Phase 7e)
        to know which tiles to enqueue and by tests as a quick
        completeness check."""
        return [c for c in self._items if c not in self._loaded_lru]

    def loaded_cap(self) -> int:
        """Configured LRU cap; mostly for tests asserting on the
        memory bound."""
        return self._loaded_cap


# ---------------------------------------------------------------------------
# Multi-zoom overlay
# ---------------------------------------------------------------------------


#: View scale at or above which the highest configured zoom level is
#: selected. Below this, each halving of the view scale steps the
#: chosen zoom down by one. With the default ``[12, 13, 14, 15]``
#: set and the current anchor of ``6.0`` that means:
#:
#: * view_scale > 3.0   → z=15
#: * (1.5, 3.0]         → z=14
#: * (0.75, 1.5]        → z=13
#: * <= 0.75            → z=12
#:
#: Why ``6.0``: this anchor preserves the empirically-tuned
#: z=12/z=13/z=14 boundaries from the previous 3-level configuration
#: (default fit-to-screen 0.5–0.6 → z=12; medium-detail band
#: 0.75–1.5 → z=13; first-detail layer 1.5+ → z=14) and slots
#: z=15 in *above* z=14 as the deep-zoom "deep airport detail"
#: layer. z=15 doesn't take over until the user has zoomed past
#: ×3.0 — i.e. when they're already on z=14 with full detail
#: visible and explicitly want still more resolution (typical
#: use-case: ~3 nm viewport for circling around an unfamiliar
#: airfield).
#:
#: At view-scale 3.0+ on the Israel chart a z=15 tile (256 sat-px,
#: ~33.5 chart-px wide) renders into ~100 screen-px, i.e. ~2.5×
#: downsampling -- the same ratio z=14 had at its boundary under
#: the previous 3.0 anchor, so the perceptual escalation feels
#: identical to the user.
#:
#: Anchor history:
#:
#: * ``1.0`` (original): placed the z=13/z=14 boundary at
#:   view_scale = 0.5 -- *inside* typical fit-to-screen, leading
#:   to z=14 being loaded at ~7.6× downsampling. ~16× excess tile
#:   budget at default.
#: * ``3.0``: anchored to the user's empirical detail-kick-in
#:   threshold of view-scale 1.5. z=12 became the default
#:   fit-to-screen layer; z=14 was the deepest layer.
#: * ``6.0`` (current): doubles the previous anchor so the new
#:   z=15 layer slots above z=14 without disturbing the
#:   z=12/z=13/z=14 boundaries the user already verified.
#:
#: The "halve the scale → step zoom down by one" rule is preserved
#: because it gives clean per-octave boundaries that compose
#: cleanly with the multi-zoom layered-fallback stack: every
#: octave of zoom-in promotes the active layer by one step;
#: every coarser layer remains as a free cached fallback.
MULTIZOOM_BASE_VIEW_SCALE: float = 6.0


def select_zoom_for_view_scale(
    view_scale: float, zoom_levels: Iterable[int]
) -> int:
    """Pick the active zoom level for a given view scale.

    Algorithm: anchor the highest configured zoom at
    ``view_scale == MULTIZOOM_BASE_VIEW_SCALE`` (currently
    ``6.0``). For each halving of the view scale below the
    anchor, step the chosen zoom level down by one. Clamp to the
    lowest configured zoom so very-zoomed-out users still see
    *something*.

    See the constant's docstring for the rationale behind the
    ``6.0`` anchor — short version, on the default
    ``[12, 13, 14, 15]`` set it places the typical fit-to-screen
    view scale (~0.5–0.6) firmly on z=12 (the cheapest tile
    set), z=13 covers (~0.75–1.5), z=14 covers (~1.5–3.0) as
    the first-detail layer, and z=15 kicks in above ×3.0 as the
    deep-airport-detail layer.

    Parameters
    ----------
    view_scale
        View transform's m11 scale factor — i.e. how many screen
        pixels a chart pixel maps to. Must be > 0; values <= 0
        are clamped to a tiny positive number to keep the log
        operation defined (a zero-or-negative scale shouldn't
        happen in practice but we don't want to crash on it).
    zoom_levels
        Iterable of available zoom integers. The function picks
        from this set; if ``view_scale`` lands the algorithm at
        a level not present, the next-lower available zoom is
        used instead. Empty iterables raise ``ValueError`` —
        there's no sensible answer.

    Returns
    -------
    int
        The chosen zoom level, guaranteed to be present in
        ``zoom_levels``.

    Examples
    --------
    With the default anchor of ``MULTIZOOM_BASE_VIEW_SCALE = 6.0``
    and the default ``[12, 13, 14, 15]`` set:

    >>> select_zoom_for_view_scale(6.0, [12, 13, 14, 15])
    15
    >>> select_zoom_for_view_scale(3.0, [12, 13, 14, 15])
    14
    >>> select_zoom_for_view_scale(1.5, [12, 13, 14, 15])
    13
    >>> select_zoom_for_view_scale(0.75, [12, 13, 14, 15])
    12
    >>> select_zoom_for_view_scale(0.5, [12, 13, 14, 15])
    12
    >>> select_zoom_for_view_scale(10.0, [12, 13, 14, 15])
    15

    The boundaries are at exact halvings of the anchor — i.e.
    view_scale > 3.0 selects z=15, (1.5, 3.0] selects z=14,
    (0.75, 1.5] selects z=13, and <= 0.75 clamps at z=12. A user
    sitting at the default fit-to-screen view scale (~0.5–0.6 on
    Israel charts) lands on z=12.
    """
    levels = sorted(set(int(z) for z in zoom_levels))
    if not levels:
        raise ValueError("zoom_levels must not be empty")
    highest = levels[-1]
    lowest = levels[0]

    safe_scale = max(float(view_scale), 1e-6)
    # log2(scale / base): zero at the anchor, -1 at half-anchor,
    # -2 at quarter-anchor, etc. We want the boundaries at exact
    # halvings — i.e. with the default anchor of 6.0 and the
    # default [12, 13, 14, 15] set:
    #
    #   scale ∈ (3.0, 6.0]    → step 0 (highest zoom, z=15)
    #   scale ∈ (1.5, 3.0]    → step 1 (one coarser, z=14)
    #   scale ∈ (0.75, 1.5]   → step 2 (two coarser, z=13)
    #   scale ∈ (0.375, 0.75] → step 3 (three coarser, z=12)
    #   scale <= 0.375        → clamped at the lowest zoom (z=12)
    #
    # ``ceil`` gives the right behaviour:
    #
    # * 5.99   → log2(5.99/6) ≈ -0.0024 → ceil = 0  → step 0 (no jitter)
    # * 3.0    → log2(3/6) = -1         → ceil = -1 → step 1 (boundary)
    # * 2.0    → log2(2/6) ≈ -1.58      → ceil = -1 → step 1 (same band)
    # * 1.5    → log2(1.5/6) = -2       → ceil = -2 → step 2 (next boundary)
    #
    # ``floor`` would put 5.99 at step 1 — visible flicker for
    # tiny scale dips.
    log_ratio = math.log2(safe_scale / MULTIZOOM_BASE_VIEW_SCALE)
    steps_down = max(0, -int(math.ceil(log_ratio)))
    target = highest - steps_down
    if target >= highest:
        return highest
    if target <= lowest:
        return lowest
    # Round target down to the nearest *available* level (caller
    # might pass a non-contiguous set like [12, 14]).
    for z in reversed(levels):
        if z <= target:
            return z
    return lowest


class MultiZoomSatelliteOverlay:
    """Wraps several :class:`SatelliteOverlay` instances (one per
    configured zoom level) and selects which zoom to display
    based on the current view scale.

    Goal: at zoom-out the user gets a small number of large
    coarse-zoom tiles (e.g. z=12 with ~600 tiles for the chart
    bbox) rather than thousands of microscopic z=14 tiles. At
    near-default view scale we use the finest configured zoom so
    that subsequent zoom-in still has detail to show.

    The wrapper itself is a thin coordinator: each per-zoom
    :class:`SatelliteOverlay` continues to manage its own LRU,
    transforms, and tile items. Visibility is the only thing that
    moves between the zooms — when active zoom switches from
    z=14 to z=13 we set z=14's items to ``setVisible(False)``
    and hand the visible-rect update to z=13's overlay.

    Constructed once per chart sheet, parented to the chart
    pixmap item (via the per-zoom :class:`SatelliteOverlay`s).
    The wrapper itself owns no Qt items.
    """

    def __init__(
        self,
        *,
        chart_item: QGraphicsPixmapItem,
        calibration: "SheetGeoCalibration",
        pixmap_size: tuple[int, int],
        zoom_levels: Iterable[int],
        tile_cache: TileCache,
        placeholder: QPixmap | None = None,
        loaded_tile_cap: int = DEFAULT_LOADED_TILE_CAP,
        visibility_pad_factor: float = DEFAULT_VISIBILITY_PAD_FACTOR,
        initial_view_scale: float = MULTIZOOM_BASE_VIEW_SCALE,
        chart_seam_partition: ChartSeamPartition | None = None,
        sheet_z_bump: float = 0.0,
    ) -> None:
        """Construct one per-zoom overlay for each configured zoom.

        Parameters mirror :class:`SatelliteOverlay`'s; the only
        differences are:

        * ``zoom_levels`` replaces ``target_zoom`` (an iterable
          of ints, all of which get an overlay instance).
        * ``initial_view_scale`` picks the initial active zoom.

        ``chart_seam_partition`` is forwarded to every per-zoom
        :class:`SatelliteOverlay` so the chart-seam ownership
        rule applies uniformly across z=12 / z=13 / z=14 —
        otherwise an unowned coarse layer would still show
        through under an owned fine layer at the boundary and
        produce a misaligned ghost of the same tile from the
        wrong sheet's calibration.

        ``sheet_z_bump`` adds a tiny offset to every per-zoom
        ``tile_z_value`` so the two sheets' overlays land at
        distinct, deterministic scene z's even though they each
        run the same ``SATELLITE_TILE_Z + zoom * 0.01`` formula
        underneath. The chart-seam partition's north-extension
        deliberately produces a one-tile-row overlap where both
        sheets' overlays enumerate the same ``(z, x, y)`` (the
        spill-over row that closes the affine-disagreement gap);
        without a deterministic z-tie-break, Qt's painter order
        for items at identical z is implementation-defined and
        the user would see a coin-flip between north's and south's
        projection in the overlap row from one frame to the next.
        Convention: pass ``0.0`` (default) for north's overlay and
        a small positive value (e.g. ``0.005``) for south's so
        south wins in the overlap, keeping south's visible
        territory identical to the un-extended partition. The
        bump must be small relative to the per-zoom step
        (``0.01``) so coarser zooms still sit under finer ones
        across both sheets; ``0.005`` halves the per-zoom gap
        which is plenty of separation while preserving the
        coarse-under-fine ordering.
        """
        levels = sorted(set(int(z) for z in zoom_levels))
        if not levels:
            raise ValueError("zoom_levels must not be empty")

        # Build a placeholder once and pass it to every per-zoom
        # overlay so we share the pixmap across thousands of
        # tile items rather than instantiating it three times.
        ph = placeholder if placeholder is not None else make_loading_placeholder()

        self._overlays: dict[int, SatelliteOverlay] = {}
        for z in levels:
            # Per-zoom item z-value layered so coarser overlays
            # sit *under* finer ones inside the chart pixmap's
            # parent z-slot. Z=12 paints first, then z=13 on top,
            # then z=14 on top of that — so where a finer-zoom
            # tile has loaded imagery it covers the coarser tile
            # underneath, but where the finer tile is still on the
            # (transparent) placeholder the coarser tile shows
            # through. That's the entire fallback-rendering
            # implementation: stack-by-z, no compositing code.
            # The 0.01 step keeps all per-zoom values inside a
            # narrow band around :data:`SATELLITE_TILE_Z` so we
            # don't accidentally collide with other Qt z-slots
            # (route, traffic, etc. — see module docstring).
            # ``sheet_z_bump`` is added on top — ``0.0`` for north,
            # ``0.005`` for south — to break z-ties in the one-row
            # partition-overlap where both sheets enumerate the
            # same tile. With a half-step bump south's per-zoom
            # tile_z still sits comfortably under the next-finer
            # zoom (e.g. south z=12 → 15.125 < north z=13 → 15.13),
            # preserving the coarse-under-fine layering.
            tile_z = SATELLITE_TILE_Z + z * 0.01 + float(sheet_z_bump)
            self._overlays[z] = SatelliteOverlay(
                chart_item=chart_item,
                calibration=calibration,
                pixmap_size=pixmap_size,
                target_zoom=z,
                tile_cache=tile_cache,
                placeholder=ph,
                # Each per-zoom overlay gets the *full* configured
                # cap, not ``cap / num_zooms``. Only one zoom is
                # active (visible + actively loading) at any time;
                # the inactive ones keep whatever they had loaded
                # from a previous active session as a warm cache
                # for back-and-forth zoom changes, capped by their
                # own LRU. Splitting the cap was a pre-launch
                # over-correction that caused thrash at zoom-out
                # where the active coarse-zoom overlay (z=12) has
                # the whole chart's worth of tiles in the
                # viewport (~1300) but the divided cap (~166) was
                # an order of magnitude too small — tiles loaded
                # then immediately evicted on the same pass,
                # leaving the user staring at placeholders. The
                # full-cap-per-zoom worst-case memory is bounded
                # by ``num_zooms × cap × tile_size`` ≈ 870 MB per
                # sheet for the 1500/3-zoom default, but realistic
                # usage tops out around 290 MB per sheet because
                # only the active zoom hits its cap.
                loaded_tile_cap=loaded_tile_cap,
                visibility_pad_factor=visibility_pad_factor,
                tile_z_value=tile_z,
                chart_seam_partition=chart_seam_partition,
            )
        self._zoom_levels = levels
        self._visible_default = False
        self._active_zoom: int = select_zoom_for_view_scale(
            initial_view_scale, levels
        )
        self._last_visible_rect: QRectF | None = None

    # ------------------------------------------------------------------
    # Active zoom selection
    # ------------------------------------------------------------------

    def update_active_zoom_for_scale(self, view_scale: float) -> bool:
        """Recompute the active zoom from ``view_scale``.

        Returns ``True`` iff the active zoom changed (caller may
        want to log / re-trigger a visibility update).

        The visibility model is *layered*, not exclusive: every
        zoom level at-or-below the new active zoom becomes
        visible (so coarser layers act as a fallback under the
        active one), and every zoom level above becomes hidden
        (finer-than-active layers are unwanted at the current
        view scale — they'd just be invisible-microtile noise
        on top of the imagery we actually want).
        """
        new_zoom = select_zoom_for_view_scale(view_scale, self._zoom_levels)
        if new_zoom == self._active_zoom:
            return False
        self._active_zoom = new_zoom
        if self._visible_default:
            self._sync_per_zoom_visibility()
        return True

    def active_zoom(self) -> int:
        """Currently-active zoom level."""
        return self._active_zoom

    def zoom_levels(self) -> list[int]:
        """Snapshot of configured zoom levels (asc)."""
        return list(self._zoom_levels)

    def _sync_per_zoom_visibility(self) -> None:
        """Walk per-zoom overlays and flip ``set_visible`` to match
        the wrapper's layered-fallback policy:

        * wrapper hidden → every per-zoom overlay hidden.
        * wrapper visible → every per-zoom overlay with
          ``z <= active_zoom`` visible, the rest hidden.

        Centralised so :meth:`set_visible` and
        :meth:`update_active_zoom_for_scale` apply the same rule
        — historical drift between the two was the source of the
        "tiles never appear at intermediate zoom" UX bug.
        """
        for z, ov in self._overlays.items():
            ov.set_visible(
                self._visible_default and z <= self._active_zoom
            )

    # ------------------------------------------------------------------
    # Visibility / lazy-load — delegate to every active-or-coarser overlay
    # ------------------------------------------------------------------

    def update_visibility(
        self,
        scene_rect: QRectF,
        view_scale: float | None = None,
        max_loads: int | None = DEFAULT_MAX_LOADS_PER_VISIBILITY,
    ) -> tuple[int, int, list[TileCoord], bool]:
        """Drive tile loading across every layered (visible) zoom.

        If ``view_scale`` is provided, the active zoom is
        recomputed first; this is the primary entry point from
        :meth:`MainWindow._update_satellite_visibility`.

        Loading semantics are *layered*: we call
        :meth:`SatelliteOverlay.update_visibility` on every
        per-zoom overlay with ``z <= active_zoom`` (i.e. the
        ones we actually want to *draw* — coarser zooms serve
        as fallback under the active layer). Finer-than-active
        overlays are skipped to avoid wasting LRU budget on
        invisible layers.

        Returns
        -------
        ``(loaded_now, evicted_now, visible_misses, more_pending)``

        Aggregated across every loaded layer. The
        ``visible_misses`` list keeps each miss's zoom level
        encoded in :attr:`TileCoord.z`, so the GUI's on-demand
        fetch dispatcher can request the right zoom's tile from
        Esri without needing additional bookkeeping.
        ``more_pending`` is ``True`` iff *any* per-zoom overlay
        still has visible-cache-hit tiles waiting after the
        per-call load cap was hit; the caller schedules a
        follow-up to drain them. ``max_loads`` is split *across*
        the loaded layers proportional to their remaining budget
        so that, e.g., a freshly-activated z=14 layer doesn't
        starve the already-warm z=12 / z=13 fallback layers from
        getting their few missing tiles in.

        Parameters
        ----------
        scene_rect, view_scale
            See :meth:`SatelliteOverlay.update_visibility`.
        max_loads
            Aggregate per-call load cap across all loaded
            per-zoom overlays. Default
            :data:`DEFAULT_MAX_LOADS_PER_VISIBILITY` (32). Pass
            ``None`` from tests for the legacy "load everything
            in one pass" semantics.
        """
        if view_scale is not None:
            self.update_active_zoom_for_scale(view_scale)
        self._last_visible_rect = QRectF(scene_rect)
        loaded_total = 0
        evicted_total = 0
        misses_total: list[TileCoord] = []
        more_pending = False
        # Budget remaining to spend on cache-hit decodes this
        # call. Decremented after each per-zoom pass; when it
        # hits zero we stop loading (but still walk the
        # remaining layers so they get evict + misses-discovery
        # — those are cheap).
        budget = max_loads
        for z, ov in self._overlays.items():
            if z > self._active_zoom:
                # Finer than the active zoom: we don't want these
                # painted (they'd cover the coarser fallback) so
                # there's no point loading them. They stay hidden
                # via ``_sync_per_zoom_visibility`` and their
                # items keep their transparent placeholders.
                continue
            loaded, evicted, misses, layer_more = ov.update_visibility(
                scene_rect, max_loads=budget
            )
            loaded_total += loaded
            evicted_total += evicted
            misses_total.extend(misses)
            if layer_more:
                more_pending = True
            if budget is not None:
                budget = max(0, budget - loaded)
        return loaded_total, evicted_total, misses_total, more_pending

    def refresh_from_cache(
        self, only_coords: Iterable[TileCoord] | None = None
    ) -> int:
        """Forward a cache-refresh to the matching per-zoom overlay.

        ``only_coords`` may contain coords from any zoom level;
        we partition them by ``coord.z`` and dispatch to each
        overlay's ``refresh_from_cache`` independently. With
        ``None`` we refresh every per-zoom overlay *at or below
        the active zoom* — layers above the active zoom are
        hidden anyway (see :meth:`update_visibility`'s
        ``z > self._active_zoom`` skip) and refreshing them
        decodes JPEGs for tiles that will never be painted.

        Why the active-zoom gate
        ------------------------

        On a default ``[12, 13, 14, 15]`` set, the bulk-fetch
        worker's ``finished → _on_satellite_finished`` slot used
        to walk *every* layer here. When the active zoom was
        z=12 the z=13/14/15 layers had never had
        :meth:`update_visibility` called on them (multi-zoom
        explicitly skips them), so their
        :attr:`_last_visible_rect` was ``None``, and the per-zoom
        ``refresh_from_cache(None)`` then loaded *every cached
        tile* in those layers (~105 k decodes total). That's the
        multi-second GUI-thread freeze the user reported as
        "Not Responding after loading the sat map" — surfaced by
        ``Ctrl+C`` hitting :meth:`TileCache.get` deep inside the
        finished-slot's refresh loop.

        Skipping ``z > active_zoom`` mirrors what
        :meth:`update_visibility` already does for the same
        reason. The user-visible payoff is the active layer's
        viewport-visible tiles getting refreshed (so freshly-
        written bulk tiles appear) without dragging in the
        invisible layers' full caches. When the user later zooms
        into a finer layer, :meth:`update_visibility` activates
        that layer and loads its visible tiles then.
        """
        if only_coords is None:
            total = 0
            for z, ov in self._overlays.items():
                if z > self._active_zoom:
                    continue
                total += ov.refresh_from_cache(None)
            return total
        # Partition by zoom; one dispatch per non-empty bucket.
        buckets: dict[int, list[TileCoord]] = {}
        for c in only_coords:
            buckets.setdefault(int(c.z), []).append(c)
        total = 0
        for z, coords in buckets.items():
            ov = self._overlays.get(z)
            if ov is None:
                continue
            total += ov.refresh_from_cache(coords)
        return total

    def eager_load_all_cached(self) -> int:
        """Visibility-blind load across every layered (visible)
        per-zoom overlay.

        Walks every overlay with ``z <= active_zoom`` — the
        layered-fallback model means coarser zooms are also
        rendered under the active one, so pre-warming them on
        toggle-on lets the user see the imagery the cache has
        for *all* layers immediately rather than only the
        active layer. Finer-than-active overlays are skipped
        (they're hidden and would just consume LRU budget).
        """
        total = 0
        for z, ov in self._overlays.items():
            if z > self._active_zoom:
                continue
            total += ov.eager_load_all_cached()
        return total

    # ------------------------------------------------------------------
    # Visibility (whole wrapper)
    # ------------------------------------------------------------------

    def set_visible(self, on: bool) -> None:
        """Show or hide the wrapper.

        On show: every per-zoom overlay with ``z <= active_zoom``
        becomes visible (layered fallback) — coarser layers act
        as a base under the active one so missing-tile gaps
        fall back gracefully through z=14 → z=13 → z=12 →
        chart-underneath.
        On hide: every overlay is hidden.
        """
        on = bool(on)
        self._visible_default = on
        self._sync_per_zoom_visibility()

    def is_visible(self) -> bool:
        return self._visible_default

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def teardown(self) -> None:
        """Tear down every per-zoom overlay. Idempotent."""
        for ov in self._overlays.values():
            ov.teardown()
        self._overlays.clear()
        self._zoom_levels = []

    # ------------------------------------------------------------------
    # Inspection (mostly for tests / diagnostics)
    # ------------------------------------------------------------------

    def overlay_for_zoom(self, z: int) -> SatelliteOverlay | None:
        """Return the per-zoom overlay (or ``None`` if no such
        zoom is configured). Used by code paths that need the
        original :class:`SatelliteOverlay` API for the active
        zoom (e.g. on-demand fetch hooks)."""
        return self._overlays.get(int(z))

    def active_overlay(self) -> SatelliteOverlay:
        """Convenience accessor for the currently-active overlay."""
        return self._overlays[self._active_zoom]

    def total_tile_count(self) -> int:
        """Sum of tile counts across all zooms — diagnostic only."""
        return sum(ov.tile_count() for ov in self._overlays.values())

    def total_loaded_count(self) -> int:
        """Sum of loaded counts across all zooms."""
        return sum(ov.loaded_count() for ov in self._overlays.values())
