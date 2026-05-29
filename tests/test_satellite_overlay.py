"""Tests for :mod:`cvfr_routemaster.satellite_overlay` — the per-tile
satellite overlay manager.

Coverage ladder mirrors the module's own structure:

1. **Placeholder pixmap** — :func:`make_loading_placeholder` has the
   right size and is fully transparent (the design uses transparent
   placeholders so the underlying CVFR chart shows through any tile
   slot that isn't yet loaded; a non-transparent fill would block
   the chart and was the opaque-grey-wall bug in earlier builds).
2. **Decode helper** — round-trip a real PNG through
   :func:`_decode_tile_pixmap` and assert it produces a non-null
   pixmap with the expected dimensions.
3. **Overlay manager** — construction enumerates tiles, eager-loads
   any in cache, places items as children of the chart pixmap with
   the right transform, exposes the documented inspection API,
   and tears down cleanly. We don't paint anything; per the
   ``test_traffic_overlay`` precedent, validating actual pixels is
   a visual-regression concern.

The fixture-style is the same as
:mod:`tests.test_traffic_overlay`: a module-scope ``QApplication``
so :class:`QGraphicsScene` mutation doesn't segfault.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

PySide6 = pytest.importorskip("PySide6")
PIL = pytest.importorskip("PIL")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QRectF  # noqa: E402
from PySide6.QtGui import QPixmap, QTransform  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QGraphicsPixmapItem,
    QGraphicsScene,
)

from cvfr_routemaster.geo_calibration import (  # noqa: E402
    CalibrationPoint,
    calibration_from_points,
)
from cvfr_routemaster.main_window import (  # noqa: E402
    _ChartSheetItem,
)
from cvfr_routemaster.satellite_overlay import (  # noqa: E402
    MULTIZOOM_BASE_VIEW_SCALE,
    PLACEHOLDER_FILL,
    SATELLITE_TILE_Z,
    ChartSeamPartition,
    MultiZoomSatelliteOverlay,
    SatelliteOverlay,
    _decode_tile_pixmap,
    make_loading_placeholder,
    select_zoom_for_view_scale,
)
from cvfr_routemaster.satellite_overlay_math import (  # noqa: E402
    tile_to_chart_transform,
)
from cvfr_routemaster.satellite_tiles import (  # noqa: E402
    TILE_SIZE_PX,
    TileCache,
    TileCoord,
    tile_for_lonlat,
    world_pixel_to_lonlat,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """One QApplication per process — Qt requires this for
    QGraphicsScene mutation, QPainter, QPixmap allocation."""
    app = QApplication.instance() or QApplication([])
    return app


def _make_israel_calibration():
    """4-anchor Israeli VFR calibration; matches
    ``test_satellite_overlay_math``'s fixture so the geometry tests
    and overlay tests share the same projection."""
    pdf_fp = {"sha256": "test", "size": 1234}
    points = [
        CalibrationPoint(
            code="LLHA", lat=32.81, lon=35.04, u=0.10, v=0.15
        ),
        CalibrationPoint(
            code="LLER", lat=30.59, lon=34.62, u=0.05, v=0.85
        ),
        CalibrationPoint(
            code="LLOV", lat=29.55, lon=34.96, u=0.20, v=0.95
        ),
        CalibrationPoint(
            code="LLMR", lat=30.65, lon=34.80, u=0.12, v=0.50
        ),
    ]
    return calibration_from_points(pdf_fp, *points)


def _make_solid_jpeg(rgb: tuple[int, int, int]) -> bytes:
    """Build a 256×256 solid-colour JPEG. Used to populate the
    test cache without hitting the network."""
    img = Image.new("RGB", (TILE_SIZE_PX, TILE_SIZE_PX), color=rgb)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# --- Placeholder pixmap ------------------------------------------------


class TestLoadingPlaceholder:
    def test_size_matches_tile_dimensions(self, qapp: QApplication) -> None:
        pix = make_loading_placeholder()
        assert pix.width() == TILE_SIZE_PX
        assert pix.height() == TILE_SIZE_PX

    def test_is_fully_transparent(self, qapp: QApplication) -> None:
        # The placeholder is intentionally fully transparent so the
        # CVFR chart parented underneath shows through any tile that
        # isn't yet loaded. Earlier builds used an opaque grey fill
        # which blanketed the chart with placeholders whenever the
        # active coarse-zoom layer (z=12) was sparsely cached — that
        # broke the entire "satellite is an additive overlay on top
        # of the chart" UX. Sample all four corners + the centre so
        # any future regression that fills with a non-zero alpha
        # gets caught regardless of where it paints.
        pix = make_loading_placeholder()
        img = pix.toImage()
        for px, py in (
            (0, 0),
            (TILE_SIZE_PX - 1, 0),
            (0, TILE_SIZE_PX - 1),
            (TILE_SIZE_PX - 1, TILE_SIZE_PX - 1),
            (TILE_SIZE_PX // 2, TILE_SIZE_PX // 2),
        ):
            c = img.pixelColor(px, py)
            assert c.alpha() == 0, (
                f"Placeholder pixel ({px},{py}) has alpha "
                f"{c.alpha()}; expected 0 (fully transparent)"
            )
        # PLACEHOLDER_FILL itself must also be transparent so any
        # caller (e.g. a debug variant) that reuses the constant
        # gets the same chart-shows-through behaviour.
        assert PLACEHOLDER_FILL.alpha() == 0


# --- Decode helper -----------------------------------------------------


class TestDecodeTilePixmap:
    def test_decodes_valid_jpeg(self, qapp: QApplication) -> None:
        data = _make_solid_jpeg((200, 50, 50))
        pix = _decode_tile_pixmap(data)
        assert pix is not None
        assert pix.width() == TILE_SIZE_PX
        assert pix.height() == TILE_SIZE_PX

    def test_returns_none_for_garbage(self, qapp: QApplication) -> None:
        assert _decode_tile_pixmap(b"\x00\x01\x02\x03") is None

    def test_returns_none_for_empty(self, qapp: QApplication) -> None:
        assert _decode_tile_pixmap(b"") is None


# --- Overlay manager ---------------------------------------------------


class TestSatelliteOverlay:
    """The overlay's job: build one tile item per tile in the chart
    bbox, transform it correctly, eager-load anything in cache, and
    expose enough inspection API for the GUI to wire it up."""

    @pytest.fixture
    def cache_with_one_tile(
        self, tmp_path: Path
    ) -> tuple[TileCache, TileCoord, bytes]:
        cache = TileCache(tmp_path)
        # LLBG centre at z=14.
        coord = tile_for_lonlat(34.886, 32.005, z=14)
        data = _make_solid_jpeg((123, 200, 50))
        cache.put(coord, data)
        return cache, coord, data

    @pytest.fixture
    def empty_cache(self, tmp_path: Path) -> TileCache:
        return TileCache(tmp_path)

    @pytest.fixture
    def chart_setup(
        self, qapp: QApplication
    ) -> tuple[QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]]:
        scene = QGraphicsScene()
        chart_pix = QPixmap(6000, 8000)
        chart_pix.fill()  # black is fine; we never paint
        chart_item = QGraphicsPixmapItem(chart_pix)
        scene.addItem(chart_item)
        return scene, chart_item, (6000, 8000)

    def test_build_creates_items_for_chart_bbox(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
        )
        # An Israel-coverage chart at z=14 has thousands of tiles;
        # lower-bound to catch a regression that drops enumeration.
        assert ov.tile_count() >= 1000
        ov.teardown()

    def test_items_added_to_chart_scene(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        # Tile items live as top-level scene items (not children
        # of the chart pixmap) so they can paint above either
        # chart sheet — see :class:`SatelliteOverlay`'s class
        # docstring for the rationale and the new
        # ``test_tiles_are_top_level_scene_items`` test for the
        # explicit invariant.
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
        )
        sample_coord = tile_for_lonlat(34.886, 32.005, z=14)
        assert ov.has_tile(sample_coord)
        item = next(iter(ov._items.values()))  # noqa: SLF001
        assert item.scene() is scene
        assert item.parentItem() is None
        assert item.zValue() == SATELLITE_TILE_Z
        ov.teardown()

    def test_tile_transform_is_applied(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
        )
        coord = tile_for_lonlat(34.886, 32.005, z=14)
        item = ov._items[coord]  # noqa: SLF001
        # Compare the item's transform to the math-module's
        # computation — they must agree exactly (no QTransform
        # round-off) to within float precision. The tile transform
        # is now an 8-DOF projective (homography) packed into all
        # 9 QTransform coefficients; for typical Israeli tiles the
        # m13/m23 perspective components are very small but nonzero,
        # which is the whole point of the projective fix.
        tt = tile_to_chart_transform(coord, cal, pixmap_size=size)
        (
            m11, m12, m13,
            m21, m22, m23,
            m31, m32, m33,
        ) = tt.to_qtransform_components()
        item_tr: QTransform = item.transform()
        assert item_tr.m11() == pytest.approx(m11)
        assert item_tr.m12() == pytest.approx(m12)
        assert item_tr.m13() == pytest.approx(m13)
        assert item_tr.m21() == pytest.approx(m21)
        assert item_tr.m22() == pytest.approx(m22)
        assert item_tr.m23() == pytest.approx(m23)
        assert item_tr.m31() == pytest.approx(m31)
        assert item_tr.m32() == pytest.approx(m32)
        assert item_tr.m33() == pytest.approx(m33)
        ov.teardown()

    def test_construction_is_lazy_no_eager_load(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        cache_with_one_tile: tuple[TileCache, TileCoord, bytes],
    ) -> None:
        """Lazy semantics: construction creates items but loads no
        pixmaps. The first ``update_visibility`` (or explicit
        ``eager_load_all_cached`` for tests) decides what loads.
        """
        scene, chart_item, size = chart_setup
        cache, cached_coord, _ = cache_with_one_tile
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=cache,
        )
        # The cached tile is *not* loaded just because we built
        # the overlay. ``is_tile_loaded`` is the lazy contract.
        assert not ov.is_tile_loaded(cached_coord)
        assert ov.loaded_count() == 0
        # Total complement is consistent.
        assert len(ov.missing_coords()) == ov.tile_count()
        ov.teardown()

    def test_eager_load_all_cached_loads_in_cache_tiles(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        cache_with_one_tile: tuple[TileCache, TileCoord, bytes],
    ) -> None:
        scene, chart_item, size = chart_setup
        cache, cached_coord, _ = cache_with_one_tile
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=cache,
        )
        loaded = ov.eager_load_all_cached()
        assert loaded == 1
        assert ov.is_tile_loaded(cached_coord)
        assert ov.loaded_count() == 1
        ov.teardown()

    def test_refresh_with_explicit_coords_loads_before_first_viewport(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """``refresh_from_cache(only_coords=[c])`` works even before
        the first ``update_visibility`` — the on-demand fetcher's
        ``tile_ready → refresh_from_cache(only_coords=[coord])``
        wiring (see :class:`OnDemandFetchWorker`) is the load
        path for tiles the user is *currently* trying to see and
        the GUI is sitting on a placeholder. Explicit coords
        bypass the visible-rect filter precisely because the
        caller has already vetted relevance.

        The ``None`` companion path (refresh *every* tile)
        intentionally does *not* load anything in this state —
        see :func:`test_refresh_none_without_viewport_is_noop`
        for the rationale; that path is a no-op specifically to
        avoid the multi-second GUI-thread freeze
        ``_on_satellite_finished`` used to produce when a layer
        had never been activated.
        """
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
        )
        coord = tile_for_lonlat(34.886, 32.005, z=14)
        empty_cache.put(coord, _make_solid_jpeg((50, 100, 200)))
        assert ov.refresh_from_cache(only_coords=[coord]) == 1
        assert ov.is_tile_loaded(coord)
        ov.teardown()

    def test_refresh_none_without_viewport_is_noop(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """``refresh_from_cache(None)`` on a layer that has never
        seen a viewport must be a no-op — the freeze regression.

        Background: ``_on_satellite_finished`` calls
        ``MultiZoomSatelliteOverlay.refresh_from_cache()`` (no
        args) after a bulk-fetch worker emits ``finished``. The
        multi-zoom helper fans out to each per-zoom layer's
        ``refresh_from_cache(None)``. With the layered-fallback
        model, layers *above* the active zoom never have
        ``update_visibility`` called, so their
        :attr:`_last_visible_rect` is ``None``. The old
        implementation interpreted ``None`` as "no rect to
        filter against, walk every item" — which for a fully-
        cached z=15 layer over Israel meant ~80 k JPEG decodes
        on the GUI thread. Combined with z=13 and z=14 the
        single ``_on_satellite_finished`` call decoded ~105 k
        tiles, producing the multi-second "Not Responding"
        window the user reported.

        The contract now is: ``refresh_from_cache(None)`` is the
        "refresh visible tiles" path, and "no viewport seen yet"
        means "no tiles are known to be visible", which means
        load nothing. Callers that want a visibility-blind load
        of every cached tile have an explicit method —
        :meth:`SatelliteOverlay.eager_load_all_cached`.

        Pin: with several cached tiles, no viewport ever pushed,
        ``refresh_from_cache(None)`` must return 0 and load
        nothing. The follow-up
        :func:`test_refresh_none_loads_after_viewport_seen` then
        shows the same call DOES load once a viewport is known.
        """
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
        )
        # Populate three nearby tiles in the cache — without the
        # no-viewport guard the old code would load all three.
        coords = [
            tile_for_lonlat(34.886, 32.005, z=14),
            tile_for_lonlat(34.89, 32.01, z=14),
            tile_for_lonlat(34.88, 32.0, z=14),
        ]
        for c in coords:
            empty_cache.put(c, _make_solid_jpeg((50, 100, 200)))
        loaded = ov.refresh_from_cache(None)
        assert loaded == 0, (
            "refresh_from_cache(None) without a viewport must be a "
            "no-op — otherwise it decodes every cached tile and "
            "freezes the GUI thread (multi-second 'Not Responding' "
            "regression on _on_satellite_finished)."
        )
        for c in coords:
            assert not ov.is_tile_loaded(c)
        ov.teardown()

    def test_refresh_none_loads_after_viewport_seen(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """Once a viewport has been pushed, ``refresh_from_cache(None)``
        does load the visible cached tiles. This is the
        :meth:`_on_satellite_finished` path's intended payoff —
        a bulk-fetch worker just dropped tiles on disk, refresh
        decodes the ones currently in view.
        """
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
            visibility_pad_factor=0.0,
        )
        coord = tile_for_lonlat(34.886, 32.005, z=14)
        empty_cache.put(coord, _make_solid_jpeg((50, 100, 200)))
        # Push a viewport that covers the whole chart so the
        # tile is visible.
        chart_w, chart_h = size
        ov.update_visibility(QRectF(0, 0, chart_w, chart_h))
        # The viewport sweep itself may have loaded the tile —
        # refresh_from_cache(None) should be a happy no-op /
        # cheap rescan of already-loaded tiles. Either way the
        # tile ends up loaded and refresh returns a sensible
        # non-negative count without raising.
        loaded = ov.refresh_from_cache(None)
        assert loaded >= 0
        assert ov.is_tile_loaded(coord)
        ov.teardown()

    def test_refresh_skips_offscreen_after_viewport(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """Once a viewport rect has been seen, refresh skips tiles
        outside it. Bulk-fetch tile_fetched(coord) for off-screen
        tiles is a no-op until the user pans/zooms to bring them
        into view."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
            visibility_pad_factor=0.0,  # exact viewport, no hysteresis
        )
        # Tiny viewport in the top-left of the chart — the LLBG
        # tile (centre-ish) should be off-screen.
        ov.update_visibility(QRectF(0, 0, 100, 100))
        far_coord = tile_for_lonlat(34.886, 32.005, z=14)
        empty_cache.put(far_coord, _make_solid_jpeg((50, 100, 200)))
        assert ov.refresh_from_cache(only_coords=[far_coord]) == 0
        assert not ov.is_tile_loaded(far_coord)
        ov.teardown()

    def test_refresh_skips_already_loaded(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        cache_with_one_tile: tuple[TileCache, TileCoord, bytes],
    ) -> None:
        scene, chart_item, size = chart_setup
        cache, coord, _ = cache_with_one_tile
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=cache,
        )
        # First refresh: loads the single cached tile.
        ov.refresh_from_cache(only_coords=[coord])
        assert ov.is_tile_loaded(coord)
        # A second refresh shouldn't re-decode — return value tells
        # us no new transitions happened.
        assert ov.refresh_from_cache(only_coords=[coord]) == 0
        ov.teardown()

    def test_refresh_ignores_unknown_coord(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
        )
        # A tile coord outside the chart bbox: refresh should
        # silently skip it (not raise, not crash).
        outside_coord = TileCoord(z=14, x=0, y=0)
        assert not ov.has_tile(outside_coord)
        assert ov.refresh_from_cache(only_coords=[outside_coord]) == 0
        ov.teardown()

    def test_set_visible_propagates(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
        )
        assert not ov.is_visible()
        ov.set_visible(True)
        assert ov.is_visible()
        # Sample a few items.
        items = list(ov._items.values())[:5]  # noqa: SLF001
        for it in items:
            assert it.isVisible()
        ov.set_visible(False)
        for it in items:
            assert not it.isVisible()
        ov.teardown()

    def test_teardown_removes_items_from_scene(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        # Note item count BEFORE building the overlay so we measure
        # the delta cleanly.
        before = len(scene.items())
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
        )
        added = len(scene.items()) - before
        assert added == ov.tile_count()
        ov.teardown()
        # All overlay items gone from the scene.
        assert len(scene.items()) == before
        assert ov.tile_count() == 0

    def test_teardown_is_idempotent(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
        )
        ov.teardown()
        ov.teardown()  # second call is a no-op, must not raise

    def test_update_visibility_loads_visible_tiles(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        # Populate cache for two tiles: one near LLBG (chart
        # centre), one near LLER (south of chart).
        coord_centre = tile_for_lonlat(34.886, 32.005, z=14)
        coord_south = tile_for_lonlat(34.62, 30.59, z=14)
        empty_cache.put(coord_centre, _make_solid_jpeg((10, 200, 10)))
        empty_cache.put(coord_south, _make_solid_jpeg((200, 10, 10)))
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
            visibility_pad_factor=0.0,
        )
        # A viewport that covers the centre tile but not the south
        # one. The centre tile's chart-pixel position depends on
        # the calibration, so we use the item's own scene rect to
        # construct a guaranteed-overlapping viewport.
        centre_item = ov._items[coord_centre]  # noqa: SLF001
        rect = centre_item.sceneBoundingRect().adjusted(-50, -50, 50, 50)
        loaded, evicted, misses, more_pending = ov.update_visibility(rect)
        assert loaded == 1
        assert ov.is_tile_loaded(coord_centre)
        assert not ov.is_tile_loaded(coord_south)
        assert evicted == 0
        # Unlimited (default) ``max_loads`` means everything
        # visible fit in one pass — nothing pending.
        assert not more_pending
        # ``misses`` may include neighbouring tiles that fell into
        # the rect but aren't in cache — that's fine, they're the
        # tiles the demand-fetch worker would queue. The contract
        # we care about: the cached tile got loaded, the off-
        # screen one didn't.
        assert coord_centre not in misses
        assert coord_south not in misses  # not visible at all
        ov.teardown()

    def test_update_visibility_returns_visible_misses(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """A visible tile that's *not* in the cache is reported in
        ``visible_misses`` so the on-demand fetch worker can be
        asked to fetch it."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
            visibility_pad_factor=0.0,
        )
        coord = tile_for_lonlat(34.886, 32.005, z=14)
        item = ov._items[coord]  # noqa: SLF001
        rect = item.sceneBoundingRect().adjusted(-1, -1, 1, 1)
        loaded, _evicted, misses, _more_pending = ov.update_visibility(rect)
        assert loaded == 0
        assert coord in misses
        ov.teardown()

    def test_update_visibility_lru_eviction(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """When more tiles fit in the visible rect than the LRU
        cap allows, oldest-touched tiles get evicted back to
        placeholder. The eviction is the memory bound for an
        Israel-coverage overlay; without it we'd hold ~1 GB of
        decoded pixmaps when fully cached."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
            visibility_pad_factor=0.0,
            loaded_tile_cap=3,
        )
        # Pick 8 sample coords whose *items* intersect a viewport
        # around LLBG. ``ov._items.keys()[:8]`` doesn't work
        # because ``enumerate_chart_tiles`` walks tiles whose
        # *lat/lon* bbox touches the chart bbox — extrapolating
        # the affine to (u=0, v=0) puts the first tiles at
        # negative chart-pixel coords, outside any reasonable
        # viewport.
        coord_centre = tile_for_lonlat(34.886, 32.005, z=14)
        centre_rect = ov._items[coord_centre].sceneBoundingRect()  # noqa: SLF001
        tw, th = centre_rect.width(), centre_rect.height()
        viewport = centre_rect.adjusted(
            -tw * 5.0, -th * 5.0, tw * 5.0, th * 5.0
        )
        visible_coords = [
            c
            for c, item in ov._items.items()  # noqa: SLF001
            if item.sceneBoundingRect().intersects(viewport)
        ]
        assert len(visible_coords) >= 8, (
            "Need at least 8 visible tiles for the LRU eviction "
            "test to be meaningful"
        )
        sample_coords = visible_coords[:8]
        for c in sample_coords:
            empty_cache.put(c, _make_solid_jpeg((20, 20, 200)))
        loaded, evicted, _misses, _more = ov.update_visibility(viewport)
        assert ov.loaded_count() == 3, (
            f"Expected LRU cap of 3, got {ov.loaded_count()}"
        )
        assert loaded == 8
        assert evicted == 5
        ov.teardown()

    def test_update_visibility_pad_factor_keeps_tiles_loaded(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """The pad factor should keep tiles loaded that are *just*
        outside the viewport — hysteresis against jittery scrolls.
        """
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        coord = tile_for_lonlat(34.886, 32.005, z=14)
        empty_cache.put(coord, _make_solid_jpeg((10, 200, 10)))

        # Build with 0.5 pad factor.
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
            visibility_pad_factor=0.5,
        )
        item = ov._items[coord]  # noqa: SLF001
        tile_rect = item.sceneBoundingRect()
        tw = tile_rect.width()
        # Viewport ending 10 px to the left of the tile, with
        # width = tile_width * 0.5. Pad factor 0.5 expands the
        # viewport by 0.5 * 0.5 * tile_width = 0.25 * tile_width
        # in each direction; that more than covers the 10 px gap,
        # so the padded rect reaches into the tile and the tile
        # counts as visible. Without the pad, the bare viewport
        # ends 10 px short of the tile — verified below.
        viewport = QRectF(
            tile_rect.left() - tw * 0.5 - 10.0,
            tile_rect.top(),
            tw * 0.5,
            tile_rect.height(),
        )
        assert not viewport.intersects(tile_rect)
        loaded, _evicted, _misses, _more = ov.update_visibility(viewport)
        assert loaded == 1
        assert ov.is_tile_loaded(coord)
        ov.teardown()

    def test_update_visibility_unloads_via_eviction_only(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """Tiles that were loaded but are now off-screen are
        *not* immediately unloaded — they're held by the LRU
        until cap pressure evicts them. Lets a back-and-forth
        pan stay smooth without re-decoding."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        coord_a = tile_for_lonlat(34.5, 31.5, z=14)
        coord_b = tile_for_lonlat(35.0, 32.0, z=14)
        empty_cache.put(coord_a, _make_solid_jpeg((100, 100, 100)))
        empty_cache.put(coord_b, _make_solid_jpeg((200, 200, 200)))
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
            visibility_pad_factor=0.0,
            loaded_tile_cap=10,  # plenty of headroom
        )
        # First: viewport over coord_a → loads it.
        item_a = ov._items[coord_a]  # noqa: SLF001
        rect_a = item_a.sceneBoundingRect().adjusted(-1, -1, 1, 1)
        ov.update_visibility(rect_a)
        assert ov.is_tile_loaded(coord_a)
        # Now move viewport away to coord_b. coord_a is no longer
        # visible but should *stay* loaded (LRU hold).
        item_b = ov._items[coord_b]  # noqa: SLF001
        rect_b = item_b.sceneBoundingRect().adjusted(-1, -1, 1, 1)
        ov.update_visibility(rect_b)
        assert ov.is_tile_loaded(coord_a), (
            "coord_a should still be loaded — LRU has cap 10, no eviction yet"
        )
        assert ov.is_tile_loaded(coord_b)
        ov.teardown()

    def test_loaded_cap_default_is_1500(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """Pin the default cap. Sized to comfortably hold every
        tile of the coarsest zoom layer (z=12 over the Israel bbox
        ≈ 1300 tiles per sheet) so a fully zoomed-out viewport
        doesn't thrash against the cap. A future lift would silently
        triple memory usage; a future drop would re-introduce the
        zoom-out thrash bug, so pin it both ways."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
        )
        assert ov.loaded_cap() == 1500
        ov.teardown()

    def test_update_visibility_respects_max_loads_cap(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """When ``max_loads`` is set, ``update_visibility`` stops
        decoding once it hits the cap and flags ``more_pending``.
        This is the mechanism that smooths out zoom-level switches:
        a 200-tile load spreads across multiple frames instead of
        freezing the GUI for ~1 s."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
            visibility_pad_factor=0.0,
            loaded_tile_cap=1000,
        )
        # Pick a viewport covering many tiles + seed every visible
        # one in the cache. With the cap at 3 we expect exactly 3
        # loaded + ``more_pending=True``.
        coord_centre = tile_for_lonlat(34.886, 32.005, z=14)
        centre_rect = ov._items[coord_centre].sceneBoundingRect()  # noqa: SLF001
        tw, th = centre_rect.width(), centre_rect.height()
        viewport = centre_rect.adjusted(
            -tw * 3.0, -th * 3.0, tw * 3.0, th * 3.0
        )
        visible_coords = [
            c
            for c, item in ov._items.items()  # noqa: SLF001
            if item.sceneBoundingRect().intersects(viewport)
        ]
        assert len(visible_coords) >= 5
        for c in visible_coords:
            empty_cache.put(c, _make_solid_jpeg((10, 20, 30)))
        loaded, _evicted, _misses, more_pending = ov.update_visibility(
            viewport, max_loads=3
        )
        assert loaded == 3
        assert more_pending is True

        # Second pass with a generous cap drains every remaining
        # visible-cache-hit tile in one go.
        loaded2, _evicted, _misses, more_pending2 = ov.update_visibility(
            viewport, max_loads=len(visible_coords) + 10
        )
        assert loaded2 == len(visible_coords) - 3
        assert more_pending2 is False
        ov.teardown()

    def test_tiles_are_top_level_scene_items(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """Tile items must be parented to the scene, not to the
        chart pixmap — otherwise QGraphicsScene's painter walks
        the parent chain to compare across top-level siblings
        and ends up painting north-sheet tiles *before* the
        south-sheet pixmap, hiding them in the lat overlap zone
        (the "missing satellite stripe" failure mode)."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=12,
            tile_cache=empty_cache,
        )
        assert len(ov._items) > 0  # noqa: SLF001
        for coord, item in ov._items.items():  # noqa: SLF001
            assert item.parentItem() is None, (
                f"tile {coord} unexpectedly parented to "
                f"{item.parentItem()!r} — must be top-level so "
                f"it can paint above either chart pixmap"
            )
            assert item.scene() is scene, (
                f"tile {coord} not added to chart's scene "
                f"(scene={item.scene()!r})"
            )
        ov.teardown()

    def test_tile_z_above_chart_pixmaps(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """Tile z must be > 10 (the south chart pixmap's z, see
        :meth:`MainWindow.select_layer`) so tiles paint over
        both chart pixmaps regardless of which one is currently
        top-z."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
        )
        for item in ov._items.values():  # noqa: SLF001
            # SATELLITE_TILE_Z is 15.0; the multi-zoom wrapper
            # offsets it by ``zoom * 0.01`` for stacking-order
            # control, so the single-zoom overlay uses exactly
            # 15.0 here.
            assert item.zValue() == SATELLITE_TILE_Z, (
                f"unexpected z {item.zValue()} (expected "
                f"{SATELLITE_TILE_Z})"
            )
            assert item.zValue() > 10.0, (
                "tile z must exceed south chart pixmap z=10"
            )
        ov.teardown()

    def test_tile_transform_tracks_chart_geometry_via_listener(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """With a chart item that exposes
        ``add_geometry_listener``, moving / scaling the chart
        must re-flow every tile's scene transform — the chart
        pixmap is the calibration anchor and tiles must follow
        it through pan / scale (no parent-child inheritance now
        that tiles are top-level)."""
        scene, _plain_chart, size = chart_setup
        # Replace the plain chart pixmap with a
        # ``_ChartSheetItem`` so listener subscription kicks in.
        scene.removeItem(_plain_chart)
        chart_item = _ChartSheetItem(_plain_chart.pixmap())
        scene.addItem(chart_item)
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=12,
            tile_cache=empty_cache,
        )
        # Pick any tile; record its pre-move scene transform
        # (specifically the translation components — those move
        # with pos, scale moves with scale, but pos is the
        # simplest signal).
        sample = next(iter(ov._items.values()))  # noqa: SLF001
        pre_transform = sample.sceneTransform()
        pre_dx, pre_dy = pre_transform.dx(), pre_transform.dy()
        # Move the chart by a known offset; the listener must
        # propagate to the tile.
        chart_item.setPos(123.0, 456.0)
        post_transform = sample.sceneTransform()
        post_dx, post_dy = post_transform.dx(), post_transform.dy()
        # The chart's translation is composed *into* every tile's
        # transform, so the tile's scene translation should shift
        # by exactly the chart's setPos delta.
        assert post_dx - pre_dx == pytest.approx(123.0)
        assert post_dy - pre_dy == pytest.approx(456.0)
        ov.teardown()

    def test_chart_seam_partition_covers_overlap_with_at_most_one_row_double_owned(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """With :class:`ChartSeamPartition` set on both overlays, every
        tile that both sheets enumerate must be owned by *at least*
        one of them (missing-band invariant), and the set of tiles
        owned by **both** is bounded to exactly one mercator tile-row
        per column at the seam latitude (controlled-overlap invariant).

        The one-row double-ownership is the deliberate north-extension
        backfill — north's overlay enumerates one extra row past the
        seam to close the residual affine-disagreement gap between
        north's last tile and south's first tile. Where both sheets
        enumerate the same tile in this row, the per-sheet z-bump
        applied by :class:`MultiZoomSatelliteOverlay` decides which
        wins visually (south by convention), so south's *visible*
        territory remains identical to a strict-exclusive partition
        — only the gap-sliver north paints into is observably
        different from the un-extended partition.
        """
        scene, chart_item, size = chart_setup
        pdf_fp = {"sha256": "test", "size": 1234}
        north_cal = calibration_from_points(
            pdf_fp,
            CalibrationPoint(
                code="LLHA", lat=32.81, lon=35.04, u=0.10, v=0.15
            ),
            CalibrationPoint(
                code="LLER", lat=30.59, lon=34.62, u=0.05, v=0.85
            ),
            CalibrationPoint(
                code="LLOV", lat=29.55, lon=34.96, u=0.20, v=0.95
            ),
            CalibrationPoint(
                code="LLMR", lat=30.65, lon=34.80, u=0.12, v=0.50
            ),
        )
        south_cal = calibration_from_points(
            pdf_fp,
            CalibrationPoint(
                code="LLHA", lat=32.81, lon=35.04, u=0.10, v=0.85
            ),
            CalibrationPoint(
                code="LLER", lat=30.59, lon=34.62, u=0.05, v=0.15
            ),
            CalibrationPoint(
                code="LLOV", lat=29.55, lon=34.96, u=0.20, v=0.05
            ),
            CalibrationPoint(
                code="LLMR", lat=30.65, lon=34.80, u=0.12, v=0.50
            ),
        )

        # Place the seam at half the chart height — splits the bbox
        # roughly in half so both ``self_is_north`` and
        # ``self_is_north=False`` sides have a meaningful chunk.
        H_n = float(size[1])
        seam_y = H_n * 0.5
        north_partition = ChartSeamPartition(
            north_calibration=north_cal,
            north_pixmap_height=H_n,
            chart_seam_scene_y=seam_y,
            self_is_north=True,
        )
        south_partition = ChartSeamPartition(
            north_calibration=north_cal,
            north_pixmap_height=H_n,
            chart_seam_scene_y=seam_y,
            self_is_north=False,
        )

        ov_north = SatelliteOverlay(
            chart_item=chart_item,
            calibration=north_cal,
            pixmap_size=size,
            target_zoom=12,
            tile_cache=empty_cache,
            chart_seam_partition=north_partition,
        )
        ov_south = SatelliteOverlay(
            chart_item=chart_item,
            calibration=south_cal,
            pixmap_size=size,
            target_zoom=12,
            tile_cache=empty_cache,
            chart_seam_partition=south_partition,
        )

        north_keys = set(ov_north._items.keys())  # noqa: SLF001
        south_keys = set(ov_south._items.keys())  # noqa: SLF001

        # Reference: unpartitioned enumeration sets.
        ref_north = SatelliteOverlay(
            chart_item=chart_item,
            calibration=north_cal,
            pixmap_size=size,
            target_zoom=12,
            tile_cache=empty_cache,
        )
        ref_south = SatelliteOverlay(
            chart_item=chart_item,
            calibration=south_cal,
            pixmap_size=size,
            target_zoom=12,
            tile_cache=empty_cache,
        )
        ref_north_keys = set(ref_north._items.keys())  # noqa: SLF001
        ref_south_keys = set(ref_south._items.keys())  # noqa: SLF001
        ref_north.teardown()
        ref_south.teardown()

        # Missing-band invariant: no tile in the joint-enumeration
        # set is dropped by both partitioned overlays.
        overlap = ref_north_keys & ref_south_keys
        for c in overlap:
            assert (c in north_keys) or (c in south_keys), (
                f"tile {c} dropped by both chart-seam-partitioned "
                f"overlays — missing-band regression"
            )

        # Controlled-overlap invariant: where the two partitioned
        # overlays both render a tile, the duplicates form **one**
        # mercator tile-row per column at most. (Before the
        # north-extension backfill this set was empty — strict
        # exclusivity. After the backfill, it's exactly the row
        # whose centre lies between the seam and one tile-row's
        # worth of chart-px past it.)
        double_owned = north_keys & south_keys
        ys_per_x: dict[int, set[int]] = {}
        for c in double_owned:
            ys_per_x.setdefault(c.x, set()).add(c.y)
        for x, ys in ys_per_x.items():
            assert len(ys) <= 1, (
                f"column x={x} has {len(ys)} tile-rows double-owned "
                f"by both sheets ({sorted(ys)}) — controlled-overlap "
                f"invariant expects at most one"
            )

        # Sheet-unique tiles must stay with whichever sheet exclusively
        # enumerates them.
        for c in ref_north_keys - ref_south_keys:
            assert c in north_keys, (
                f"tile {c} unique to north calibration was filtered out "
                f"by the chart-seam-partitioned north overlay"
            )
        for c in ref_south_keys - ref_north_keys:
            assert c in south_keys, (
                f"tile {c} unique to south calibration was filtered out "
                f"by the chart-seam-partitioned south overlay"
            )

        ov_north.teardown()
        ov_south.teardown()

    def test_chart_seam_partition_extension_closes_affine_gap_at_seam(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """The asymmetric north-extension widens north's territory by
        exactly one tile-row past the seam, so north's overlay
        enumerates the **boundary tile-row** (the row whose centre
        sits just past the seam in scene-y under north's projection)
        on top of every tile-row it already kept.

        Without the extension the boundary-row tiles would be
        exclusively south's. The extension is asymmetric (south's
        threshold is unchanged), so the only set difference vs. the
        un-extended partition lives on north's side.
        """
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        H_n = float(size[1])
        seam_y = H_n * 0.5
        north_partition = ChartSeamPartition(
            north_calibration=cal,
            north_pixmap_height=H_n,
            chart_seam_scene_y=seam_y,
            self_is_north=True,
        )

        # Build the overlay so its constructor computes the extension
        # exactly the way production does.
        ov_with_extension = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=12,
            tile_cache=empty_cache,
            chart_seam_partition=north_partition,
        )
        extension_chart_px = (
            ov_with_extension._tile_partition_extension_chart_px  # noqa: SLF001
        )
        assert extension_chart_px > 0.0, (
            "North-extension chart-px should be positive when a "
            "partition is set; got 0.0 (the production overlay would "
            "silently regress to the strict-exclusive partition)."
        )

        # Construct a second overlay with the extension hand-disabled
        # (zero-extension partition) to get the un-extended baseline
        # north would have rendered before the fix.
        # Use a subclass override so we don't have to re-introduce a
        # strict-exclusive code path that production no longer
        # exercises.
        class _StrictExclusiveOverlay(SatelliteOverlay):
            def _compute_seam_tile_height_chart_px(self) -> float:
                return 0.0

        ov_strict = _StrictExclusiveOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=12,
            tile_cache=empty_cache,
            chart_seam_partition=north_partition,
        )

        extended_keys = set(ov_with_extension._items.keys())  # noqa: SLF001
        strict_keys = set(ov_strict._items.keys())  # noqa: SLF001

        # The extended overlay keeps a strict superset of the
        # un-extended overlay's tiles (asymmetric — only widens
        # north's claim, never narrows it).
        assert strict_keys <= extended_keys, (
            "Extension narrowed north's claim — should only widen it. "
            f"Strict-only tiles missing from extended: "
            f"{strict_keys - extended_keys}"
        )

        # The extra tiles (extended − strict) form a single contiguous
        # row at the seam: their centres' scene_y under north's
        # calibration must all fall in (seam_y, seam_y + extension].
        extra = extended_keys - strict_keys
        assert extra, (
            "Extension should have added at least one row of tiles to "
            "north's claim — empty diff means the boundary row never "
            "crossed the seam in this fixture, which means the test "
            "fixture doesn't exercise the fix."
        )
        for coord in extra:
            cx_world = (coord.x + 0.5) * TILE_SIZE_PX
            cy_world = (coord.y + 0.5) * TILE_SIZE_PX
            lon, lat = world_pixel_to_lonlat(cx_world, cy_world, coord.z)
            _u, v = cal.lonlat_to_uv(lon, lat)
            scene_y = v * H_n
            assert seam_y <= scene_y <= seam_y + extension_chart_px, (
                f"Extra tile {coord} (centre scene_y={scene_y:.2f}) "
                f"lies outside the seam-row band "
                f"({seam_y:.2f} .. {seam_y + extension_chart_px:.2f}] "
                f"— extension exceeded one tile-row"
            )

        # And the extra tiles form one row per column — not multiple
        # rows. (Equivalent to the controlled-overlap invariant of the
        # cross-sheet test above, but pinned per-column here so the
        # geometry is testable without two calibrations.)
        ys_per_x: dict[int, set[int]] = {}
        for coord in extra:
            ys_per_x.setdefault(coord.x, set()).add(coord.y)
        for x, ys in ys_per_x.items():
            assert len(ys) == 1, (
                f"Column x={x} has {len(ys)} extra tile-rows "
                f"({sorted(ys)}); the extension should add exactly one "
                f"row per column"
            )

        ov_with_extension.teardown()
        ov_strict.teardown()

    def test_chart_seam_partition_follows_north_calibration_scene_y(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """The partition's threshold lives in scene_y under north's
        calibration. Moving the seam threshold down should shift more
        tiles to north (and fewer to south); moving it up should do
        the reverse. This pins the partition direction so a future
        refactor can't silently invert it.
        """
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        H_n = float(size[1])
        partitions_at_seam = {
            0.10: ChartSeamPartition(
                north_calibration=cal,
                north_pixmap_height=H_n,
                chart_seam_scene_y=H_n * 0.10,
                self_is_north=True,
            ),
            0.90: ChartSeamPartition(
                north_calibration=cal,
                north_pixmap_height=H_n,
                chart_seam_scene_y=H_n * 0.90,
                self_is_north=True,
            ),
        }
        counts: dict[float, int] = {}
        for frac, partition in partitions_at_seam.items():
            ov = SatelliteOverlay(
                chart_item=chart_item,
                calibration=cal,
                pixmap_size=size,
                target_zoom=12,
                tile_cache=empty_cache,
                chart_seam_partition=partition,
            )
            counts[frac] = len(ov._items)  # noqa: SLF001
            ov.teardown()

        # A higher-fraction seam (deeper into the chart) means *more*
        # of north's chart is "above the seam", so the north overlay
        # keeps more tiles.
        assert counts[0.90] > counts[0.10], (
            f"chart-seam partition direction is inverted: "
            f"seam at 90% height kept {counts[0.90]} tiles, "
            f"seam at 10% height kept {counts[0.10]} — should be the "
            f"other way around"
        )

    def test_teardown_deregisters_geometry_listener(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """After ``teardown()`` further chart moves must NOT fire
        the overlay's transform-apply callback — otherwise we'd
        index into a cleared ``_items`` dict and silently no-op
        on every chart pan."""
        scene, _plain_chart, size = chart_setup
        scene.removeItem(_plain_chart)
        chart_item = _ChartSheetItem(_plain_chart.pixmap())
        scene.addItem(chart_item)
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=12,
            tile_cache=empty_cache,
        )
        # One listener registered (the overlay's).
        listeners = chart_item._geometry_listeners  # noqa: SLF001
        assert len(listeners) == 1
        ov.teardown()
        # Deregistered.
        assert len(chart_item._geometry_listeners) == 0  # noqa: SLF001

    def test_uses_supplied_placeholder(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        # Supplying a custom placeholder avoids the QPainter call
        # in tests; verify it's actually used. The test placeholder
        # is a tiny 4×4 green pixmap so it's distinguishable.
        custom = QPixmap(4, 4)
        custom.fill()
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        ov = SatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            target_zoom=14,
            tile_cache=empty_cache,
            placeholder=custom,
        )
        sample_coord = tile_for_lonlat(34.886, 32.005, z=14)
        item = ov._items[sample_coord]  # noqa: SLF001
        # Placeholder pixmaps share the same Qt internal ID after
        # ``setPixmap``; checking via cacheKey() is the safe way
        # to verify identity (vs ``is`` which Qt may not preserve).
        assert item.pixmap().cacheKey() == custom.cacheKey()
        ov.teardown()


# ---------------------------------------------------------------------------
# select_zoom_for_view_scale — pure-math
# ---------------------------------------------------------------------------


class TestSelectZoomForViewScale:
    # The function anchors the highest configured zoom at
    # ``MULTIZOOM_BASE_VIEW_SCALE`` (currently 6.0) and steps the
    # chosen zoom down by one for every halving of the view scale
    # below that anchor. These tests pin both the anchor invariant
    # (so accidentally moving the anchor breaks them loudly) and
    # the per-band boundary semantics on the default 4-level set
    # ``[12, 13, 14, 15]``:
    #
    #   view_scale > 3.0          → z=15
    #   view_scale ∈ (1.5, 3.0]   → z=14
    #   view_scale ∈ (0.75, 1.5]  → z=13
    #   view_scale ≤ 0.75         → z=12 (clamped at lowest)
    #
    # The z=12/z=13/z=14 boundaries are inherited verbatim from
    # the previous 3-level configuration (the user verified them
    # empirically); the new ``6.0`` anchor doubles the previous
    # ``3.0`` so the new z=15 layer slots above z=14 without
    # disturbing the verified boundaries.
    #
    # See ``MULTIZOOM_BASE_VIEW_SCALE``'s docstring for the full
    # rationale.

    def test_at_base_scale_uses_highest_zoom(self) -> None:
        """At the anchor view scale, pick the finest configured
        zoom so subsequent zoom-ins still have detail."""
        assert (
            select_zoom_for_view_scale(MULTIZOOM_BASE_VIEW_SCALE, [12, 13, 14])
            == 14
        )

    def test_above_base_scale_uses_highest_zoom(self) -> None:
        """Zoomed in past the anchor — still use the finest. Adding
        z=15 / z=16 levels would require a separate "zoom up"
        ladder; today we cap at the highest configured."""
        assert select_zoom_for_view_scale(MULTIZOOM_BASE_VIEW_SCALE, [12, 13, 14]) == 14
        assert select_zoom_for_view_scale(10.0 * MULTIZOOM_BASE_VIEW_SCALE, [12, 13, 14]) == 14

    def test_each_halving_steps_zoom_down_one(self) -> None:
        """The "halve scale → drop one zoom" rule, anchored at
        ``MULTIZOOM_BASE_VIEW_SCALE``. Test on a 3-level
        ``[12, 13, 14]`` set so each halving traverses one named
        band: ``half`` → second-from-top, ``quarter`` → lowest,
        further down → clamped to lowest. The exact view_scale
        values move with the anchor; the *rule* doesn't."""
        half = MULTIZOOM_BASE_VIEW_SCALE / 2.0
        quarter = MULTIZOOM_BASE_VIEW_SCALE / 4.0
        assert select_zoom_for_view_scale(half, [12, 13, 14]) == 13
        assert select_zoom_for_view_scale(quarter, [12, 13, 14]) == 12
        assert select_zoom_for_view_scale(quarter / 2.5, [12, 13, 14]) == 12

    def test_just_below_anchor_keeps_highest(self) -> None:
        """Tiny dips below the anchor shouldn't trigger a zoom
        switch — the user hasn't really zoomed out, and a full
        level swap for a few-% scale change would feel jittery.
        ``ceil`` of log2 puts the boundary at exact halvings of the
        anchor, so anything in (anchor/2, anchor] keeps the
        highest zoom."""
        anchor = MULTIZOOM_BASE_VIEW_SCALE
        half = anchor / 2.0
        assert select_zoom_for_view_scale(anchor * 0.999, [12, 13, 14]) == 14
        assert select_zoom_for_view_scale(anchor * 0.6, [12, 13, 14]) == 14
        assert select_zoom_for_view_scale(anchor, [12, 13, 14]) == 14
        # The boundary is at the half-anchor inclusive (because
        # ceil(-1.0) = -1). Just at and below it we step down to
        # the next coarser level.
        assert select_zoom_for_view_scale(half, [12, 13, 14]) == 13
        assert select_zoom_for_view_scale(half * 0.999, [12, 13, 14]) == 13

    # The four regression-guard tests below pin specific
    # view_scale → zoom mappings on the *default* 4-level set
    # ``[12, 13, 14, 15]``. They form a band-by-band sweep
    # (z=12 → z=13 → z=14 → z=15) so a regression at any
    # boundary screams loudly. Together they're the canonical
    # spec for "what the user sees when satellite mode is on
    # with default settings".

    def test_default_fit_to_screen_scale_uses_lowest_zoom(self) -> None:
        """Regression guard for the optimization decision: at the
        typical fit-to-screen view scale on the Israel chart
        (~0.5–0.6), we explicitly want z=12 — the cheapest tile
        set to download/decode/paint. If someone moves the anchor
        back down without a deliberate decision, this test should
        scream. The upper bound here is ``0.75`` — the z=12/z=13
        boundary the user chose empirically — so anything in
        ``[0.4, 0.75]`` must still land on z=12 even on the
        4-level default set."""
        for fit_scale in (0.4, 0.5, 0.55, 0.6, 0.7, 0.75):
            assert (
                select_zoom_for_view_scale(fit_scale, [12, 13, 14, 15]) == 12
            ), f"fit_scale={fit_scale} unexpectedly picked a finer zoom"

    def test_mid_low_band_uses_z13(self) -> None:
        """Regression guard for the z=13 band: between the user-
        chosen z=12/z=13 boundary (0.75) and the z=13/z=14
        boundary (1.5), the selector should pick z=13. Pins
        "z=13 exists as a meaningful intermediate layer"."""
        for mid_scale in (0.8, 1.0, 1.25, 1.4, 1.5):
            assert (
                select_zoom_for_view_scale(mid_scale, [12, 13, 14, 15]) == 13
            ), f"mid_scale={mid_scale} unexpectedly picked a non-z=13 zoom"

    def test_first_detail_band_uses_z14(self) -> None:
        """Regression guard for the z=14 band: between the
        z=13/z=14 boundary (1.5) and the new z=14/z=15 boundary
        (3.0), the selector should pick z=14 — the previous
        "first detail" layer, preserved verbatim from the 3-level
        configuration. Pins "z=14 keeps its old behaviour even
        with z=15 added on top"."""
        for first_detail_scale in (1.51, 1.75, 2.0, 2.5, 3.0):
            assert (
                select_zoom_for_view_scale(first_detail_scale, [12, 13, 14, 15])
                == 14
            ), f"first_detail_scale={first_detail_scale} unexpectedly picked a non-z=14 zoom"

    def test_deep_detail_band_uses_z15(self) -> None:
        """Regression guard for the new z=15 band: above the
        z=14/z=15 boundary (3.0), the selector should pick z=15
        — the deep-airport-detail layer the user enabled for
        close-to-airport situational awareness."""
        for deep_scale in (3.01, 4.0, 5.0, 6.0, 10.0):
            assert (
                select_zoom_for_view_scale(deep_scale, [12, 13, 14, 15]) == 15
            ), f"deep_scale={deep_scale} unexpectedly picked a coarser zoom"

    def test_clamps_at_lowest_when_extremely_zoomed_out(self) -> None:
        """Even at view_scale=0.001 (zoomed all the way out), we
        should still display the coarsest available zoom — not
        return None or crash."""
        assert select_zoom_for_view_scale(0.001, [12, 13, 14]) == 12

    def test_handles_non_contiguous_zoom_set(self) -> None:
        """``[12, 14]`` (no z=13). At a scale that "wants" z=13,
        the algorithm should return the next-lower available
        (z=12)."""
        # At half the anchor, the normal rule says "highest - 1 =
        # 13" — not in the set, so step down to 12.
        assert select_zoom_for_view_scale(MULTIZOOM_BASE_VIEW_SCALE / 2.0, [12, 14]) == 12

    def test_single_zoom_always_returns_that_zoom(self) -> None:
        """Edge case: user configured only one zoom (e.g. via
        ``satellite_zoom = 12`` collapsing the [-2, -1, 0] set
        down to [12])."""
        for scale in (0.01, 0.5, 1.0, 5.0):
            assert select_zoom_for_view_scale(scale, [12]) == 12

    def test_zero_or_negative_scale_clamps_to_lowest(self) -> None:
        """Defensive: a zero or negative view scale shouldn't
        crash the log2 call. Clamps to the lowest zoom (which is
        what an "infinitely zoomed out" view would want)."""
        assert select_zoom_for_view_scale(0.0, [12, 13, 14]) == 12
        assert select_zoom_for_view_scale(-1.0, [12, 13, 14]) == 12

    def test_empty_zoom_levels_raises(self) -> None:
        """No zoom levels = no answer; bubbling the error up is
        better than silently returning 0 or 14."""
        with pytest.raises(ValueError):
            select_zoom_for_view_scale(1.0, [])


# ---------------------------------------------------------------------------
# MultiZoomSatelliteOverlay — wrapper coordinating multiple per-zoom overlays
# ---------------------------------------------------------------------------


class TestMultiZoomSatelliteOverlay:
    """Verifies the wrapper switches between configured zoom levels
    based on view scale, hides inactive overlays, and dispatches
    cache refreshes to the right per-zoom overlay."""

    @pytest.fixture
    def chart_setup(
        self, qapp: QApplication
    ) -> tuple[QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]]:
        scene = QGraphicsScene()
        chart_pix = QPixmap(6000, 8000)
        chart_pix.fill()
        chart_item = QGraphicsPixmapItem(chart_pix)
        scene.addItem(chart_item)
        return scene, chart_item, (6000, 8000)

    @pytest.fixture
    def empty_cache(self, tmp_path: Path) -> TileCache:
        return TileCache(tmp_path)

    def test_constructs_one_overlay_per_zoom(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        custom = QPixmap(4, 4)
        custom.fill()
        ov = MultiZoomSatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            zoom_levels=[12, 13, 14],
            tile_cache=empty_cache,
            placeholder=custom,
        )
        # Each zoom's per-zoom overlay is reachable; tile counts
        # are non-zero. Coarser zooms have fewer tiles.
        z14 = ov.overlay_for_zoom(14)
        z13 = ov.overlay_for_zoom(13)
        z12 = ov.overlay_for_zoom(12)
        assert z14 is not None
        assert z13 is not None
        assert z12 is not None
        assert z14.tile_count() > z13.tile_count() > z12.tile_count()
        ov.teardown()

    def test_dedups_zoom_levels(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """Repeated zooms in the input collapse to a single overlay
        — the wrapper's set semantics."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        custom = QPixmap(4, 4)
        custom.fill()
        ov = MultiZoomSatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            zoom_levels=[14, 14, 14],
            tile_cache=empty_cache,
            placeholder=custom,
        )
        assert ov.zoom_levels() == [14]
        ov.teardown()

    def test_initial_view_scale_picks_active_zoom(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """initial_view_scale at 0.25 should land on z=12 active."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        custom = QPixmap(4, 4)
        custom.fill()
        ov = MultiZoomSatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            zoom_levels=[12, 13, 14],
            tile_cache=empty_cache,
            placeholder=custom,
            initial_view_scale=0.25,
        )
        assert ov.active_zoom() == 12
        ov.teardown()

    def test_set_visible_shows_active_and_coarser_layers(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """When the wrapper is visible, the active zoom *and*
        every coarser zoom should be visible — the multi-zoom
        overlay layers them as a fallback stack (z=12 under
        z=13 under z=14) so missing-tile gaps in the active
        layer fall through to whatever the coarser layers
        loaded. Finer-than-active zooms remain hidden because
        they'd just cover the layers we actually want to draw.
        """
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        custom = QPixmap(4, 4)
        custom.fill()
        ov = MultiZoomSatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            zoom_levels=[12, 13, 14],
            tile_cache=empty_cache,
            placeholder=custom,
            # At the anchor view scale the finest configured zoom
            # is active. Driving the test from the constant keeps
            # it correct if/when the anchor moves.
            initial_view_scale=MULTIZOOM_BASE_VIEW_SCALE,
        )
        ov.set_visible(True)
        z14 = ov.overlay_for_zoom(14)
        z13 = ov.overlay_for_zoom(13)
        z12 = ov.overlay_for_zoom(12)
        assert z14 is not None and z13 is not None and z12 is not None
        # Active zoom visible.
        assert z14.is_visible()
        # Coarser zooms also visible — they're the fallback
        # base layers stacked under the active zoom.
        assert z13.is_visible()
        assert z12.is_visible()
        ov.teardown()

    def test_active_zoom_changes_on_scale_step_down(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """Stepping the view scale down by one octave (i.e. into the
        (anchor/4, anchor/2] band) should hand control from z=14
        to z=13: z=14 (now above active) hides, while z=13 stays
        visible as the new top layer. z=12 (below active) also
        stays visible as the fallback base."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        custom = QPixmap(4, 4)
        custom.fill()
        ov = MultiZoomSatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            zoom_levels=[12, 13, 14],
            tile_cache=empty_cache,
            placeholder=custom,
            initial_view_scale=MULTIZOOM_BASE_VIEW_SCALE,  # → z=14
        )
        ov.set_visible(True)
        assert ov.active_zoom() == 14
        # Drop into the (anchor/4, anchor/2] band, which the
        # algorithm maps to "one step below highest".
        step_down_scale = MULTIZOOM_BASE_VIEW_SCALE * 0.4
        changed = ov.update_active_zoom_for_scale(step_down_scale)
        assert changed is True
        assert ov.active_zoom() == 13
        # After the step-down: z=14 (above active) hidden;
        # z=13 (active) visible; z=12 (below active) visible.
        z14 = ov.overlay_for_zoom(14)
        z13 = ov.overlay_for_zoom(13)
        z12 = ov.overlay_for_zoom(12)
        assert z14 is not None and z13 is not None and z12 is not None
        assert not z14.is_visible()
        assert z13.is_visible()
        assert z12.is_visible()
        ov.teardown()

    def test_no_change_returns_false(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        custom = QPixmap(4, 4)
        custom.fill()
        ov = MultiZoomSatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            zoom_levels=[12, 13, 14],
            tile_cache=empty_cache,
            placeholder=custom,
            initial_view_scale=MULTIZOOM_BASE_VIEW_SCALE,
        )
        # Re-applying the anchor scale is a no-op; bumping it 50 %
        # over the anchor stays in the highest-zoom band, so still
        # a no-op. (Both should leave the active zoom at the
        # highest configured.)
        assert ov.update_active_zoom_for_scale(MULTIZOOM_BASE_VIEW_SCALE) is False
        assert (
            ov.update_active_zoom_for_scale(MULTIZOOM_BASE_VIEW_SCALE * 1.5) is False
        )
        ov.teardown()

    def test_refresh_none_skips_layers_above_active_zoom(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        tmp_path: Path,
    ) -> None:
        """``MultiZoomSatelliteOverlay.refresh_from_cache(None)``
        must skip per-zoom layers above the active zoom — those
        layers are hidden anyway and refreshing them decodes
        JPEGs for tiles that will never be painted.

        Background: this is the second half of the
        "Not Responding on launch" regression. ``update_visibility``
        on the multi-zoom already skips ``z > active_zoom`` —
        layers above the active zoom thus never have their
        per-zoom ``_last_visible_rect`` set. The old
        ``refresh_from_cache(None)`` then walked those layers
        with no rect filter, loading *every* cached tile in them
        — at z=15 that's ~80 k JPEG decodes on the GUI thread.
        Symmetrising with ``update_visibility``'s active-zoom
        gate fixes it.

        Pin: with the active zoom at z=12 and a tile cached at
        z=14, ``refresh_from_cache(None)`` must NOT load the
        z=14 tile (and must not raise just because the z=13/14
        layers haven't been activated yet).
        """
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        cache = TileCache(tmp_path)

        coord_z12 = tile_for_lonlat(34.886, 32.005, z=12)
        coord_z14 = tile_for_lonlat(34.886, 32.005, z=14)
        cache.put(coord_z12, _make_solid_jpeg((255, 0, 0)))
        cache.put(coord_z14, _make_solid_jpeg((0, 0, 255)))

        custom = QPixmap(4, 4)
        custom.fill()
        ov = MultiZoomSatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            zoom_levels=[12, 13, 14],
            tile_cache=cache,
            placeholder=custom,
            # A view scale that pins active zoom to z=12 (the
            # coarsest configured) so z=13 + z=14 are above it.
            initial_view_scale=MULTIZOOM_BASE_VIEW_SCALE * 0.1,
        )
        # Sanity: active zoom is the coarsest.
        assert ov.active_zoom() == 12
        # Push a viewport so z=12 (and only z=12) has a visible
        # rect — mirrors the runtime state where
        # ``_update_satellite_visibility`` has run but only
        # activated the layers at or below the active zoom.
        chart_w, chart_h = size
        ov.update_visibility(
            QRectF(0, 0, chart_w, chart_h),
            view_scale=MULTIZOOM_BASE_VIEW_SCALE * 0.1,
        )
        # The z=12 layer's tile should be loaded by the viewport
        # sweep; the z=14 layer was never activated.
        z12 = ov.overlay_for_zoom(12)
        z14 = ov.overlay_for_zoom(14)
        assert z12 is not None and z14 is not None
        assert z14._last_visible_rect is None  # noqa: SLF001 — the regression precondition

        # The regression call: refresh_from_cache(None) must not
        # touch the z=14 layer.
        ov.refresh_from_cache(None)
        assert not z14.is_tile_loaded(coord_z14), (
            "z=14 layer above active zoom must not be refreshed — "
            "the no-rect-set walk would JPEG-decode every cached "
            "tile in it and freeze the GUI thread."
        )
        ov.teardown()

    def test_refresh_from_cache_partitions_by_zoom(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        tmp_path: Path,
    ) -> None:
        """A coord at z=13 should refresh the z=13 overlay only —
        passing it to the z=14 or z=12 overlay would be a no-op
        (no item with that coord) but the dispatch keeps the
        wrapper internals robust to mixed-zoom batches."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        cache = TileCache(tmp_path)

        # Seed cache with one tile at each zoom near LLBG.
        coord_z14 = tile_for_lonlat(34.886, 32.005, z=14)
        coord_z13 = tile_for_lonlat(34.886, 32.005, z=13)
        cache.put(coord_z14, _make_solid_jpeg((255, 0, 0)))
        cache.put(coord_z13, _make_solid_jpeg((0, 255, 0)))

        custom = QPixmap(4, 4)
        custom.fill()
        ov = MultiZoomSatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            zoom_levels=[12, 13, 14],
            tile_cache=cache,
            placeholder=custom,
            initial_view_scale=MULTIZOOM_BASE_VIEW_SCALE,
        )
        # Partition refresh: only z=13 coord goes to the z=13
        # overlay; the per-zoom overlays load their respective
        # tiles into their own LRU.
        ov.refresh_from_cache(only_coords=[coord_z13])
        z13 = ov.overlay_for_zoom(13)
        z14 = ov.overlay_for_zoom(14)
        assert z13 is not None and z14 is not None
        assert z13.is_tile_loaded(coord_z13)
        # z=14 overlay shouldn't have loaded its tile from this
        # call (we only passed a z=13 coord).
        assert not z14.is_tile_loaded(coord_z14)
        ov.teardown()

    def test_teardown_clears_all_overlays(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        custom = QPixmap(4, 4)
        custom.fill()
        ov = MultiZoomSatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            zoom_levels=[12, 13, 14],
            tile_cache=empty_cache,
            placeholder=custom,
        )
        before_total = ov.total_tile_count()
        assert before_total > 0
        ov.teardown()
        # After teardown every per-zoom overlay is gone.
        assert ov.total_tile_count() == 0
        # Idempotent — second teardown shouldn't throw.
        ov.teardown()

    def test_empty_zoom_levels_raises(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        custom = QPixmap(4, 4)
        custom.fill()
        with pytest.raises(ValueError):
            MultiZoomSatelliteOverlay(
                chart_item=chart_item,
                calibration=cal,
                pixmap_size=size,
                zoom_levels=[],
                tile_cache=empty_cache,
                placeholder=custom,
            )

    def test_finer_layers_stack_above_coarser(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """The wrapper paints coarser zooms below finer ones so
        the fallback model is purely scene-graph driven: a
        loaded finer-zoom tile covers the coarser tile beneath
        it; an empty finer-zoom slot (transparent placeholder)
        lets the coarser tile show through. Pin the relationship
        as a strict ordering on per-item z-values so a future
        refactor can't silently invert it (which would render
        the fallback layers *on top of* the active layer and
        produce a multi-zoom moiré bug)."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        custom = QPixmap(4, 4)
        custom.fill()
        ov = MultiZoomSatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            zoom_levels=[12, 13, 14],
            tile_cache=empty_cache,
            placeholder=custom,
        )
        # Sample one item from each per-zoom overlay and compare
        # their z-values. The exact value doesn't matter, only
        # the ordering coarser < finer.
        z12_ov = ov.overlay_for_zoom(12)
        z13_ov = ov.overlay_for_zoom(13)
        z14_ov = ov.overlay_for_zoom(14)
        assert z12_ov is not None and z13_ov is not None and z14_ov is not None
        z12_item = next(iter(z12_ov._items.values()))
        z13_item = next(iter(z13_ov._items.values()))
        z14_item = next(iter(z14_ov._items.values()))
        assert z12_item.zValue() < z13_item.zValue() < z14_item.zValue()
        ov.teardown()

    def test_sheet_z_bump_breaks_overlap_z_ties_without_inverting_zoom_order(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        empty_cache: TileCache,
    ) -> None:
        """The chart-seam partition's north-extension lets both
        sheets enumerate the same tile in the seam row, and Qt's
        painter order for items at identical z is implementation-
        defined. ``sheet_z_bump`` adds a small per-sheet offset so
        the duplicate-tile case always renders deterministically
        (south on top, by convention). The bump must remain
        strictly smaller than the per-zoom step (0.01) so
        ``coarse-under-fine`` ordering still holds across both
        sheets — e.g. south's z=12 must still sit under north's
        z=13. This test pins both invariants."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        custom = QPixmap(4, 4)
        custom.fill()

        ov_north = MultiZoomSatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            zoom_levels=[12, 13, 14],
            tile_cache=empty_cache,
            placeholder=custom,
            sheet_z_bump=0.0,
        )
        ov_south = MultiZoomSatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            zoom_levels=[12, 13, 14],
            tile_cache=empty_cache,
            placeholder=custom,
            sheet_z_bump=0.005,
        )

        def _sample_z(
            wrapper: MultiZoomSatelliteOverlay, zoom: int
        ) -> float:
            per_zoom = wrapper.overlay_for_zoom(zoom)
            assert per_zoom is not None
            item = next(iter(per_zoom._items.values()))  # noqa: SLF001
            return float(item.zValue())

        # 1. At every zoom, south sits above north so the
        # one-row partition-overlap renders deterministically.
        for z in (12, 13, 14):
            assert _sample_z(ov_south, z) > _sample_z(ov_north, z), (
                f"south's z={z} tiles should sit above north's z={z} "
                f"tiles to break the partition-overlap z-tie"
            )

        # 2. Across zooms within a single sheet, coarser stays
        # below finer (this also holds in the single-sheet test
        # above; re-check here in the two-sheet setup).
        for wrapper in (ov_north, ov_south):
            assert (
                _sample_z(wrapper, 12)
                < _sample_z(wrapper, 13)
                < _sample_z(wrapper, 14)
            )

        # 3. Across sheets *and* zooms: every fine-zoom tile from
        # either sheet must sit above every coarser-zoom tile from
        # the *other* sheet too — otherwise the coarse-under-fine
        # fallback would partially invert at the seam-overlap row.
        # This is the load-bearing constraint that bounds
        # ``sheet_z_bump < 0.01``.
        for coarse_z in (12, 13):
            fine_z = coarse_z + 1
            coarse_north = _sample_z(ov_north, coarse_z)
            coarse_south = _sample_z(ov_south, coarse_z)
            fine_north = _sample_z(ov_north, fine_z)
            fine_south = _sample_z(ov_south, fine_z)
            max_coarse = max(coarse_north, coarse_south)
            min_fine = min(fine_north, fine_south)
            assert max_coarse < min_fine, (
                f"sheet_z_bump too large: max(z={coarse_z}) "
                f"= {max_coarse} >= min(z={fine_z}) = {min_fine}; "
                f"coarse-under-fine is violated across sheets."
            )

        ov_north.teardown()
        ov_south.teardown()

    def test_update_visibility_loads_all_layers_at_or_below_active(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
        tmp_path: Path,
    ) -> None:
        """``update_visibility`` should drive tile loading on
        every zoom layer at-or-below the active zoom — that's
        how the coarser fallback layers acquire data to paint
        under the active layer. Finer-than-active layers stay
        un-loaded (they'd just consume LRU budget on invisible
        items)."""
        scene, chart_item, size = chart_setup
        cal = _make_israel_calibration()
        cache = TileCache(tmp_path)

        # Seed cache with one tile at each zoom near the chart
        # centre so the visibility walk will see a hit at every
        # active-or-below layer.
        from cvfr_routemaster.satellite_tiles import tile_for_lonlat

        coord_z14 = tile_for_lonlat(34.886, 32.005, z=14)
        coord_z13 = tile_for_lonlat(34.886, 32.005, z=13)
        coord_z12 = tile_for_lonlat(34.886, 32.005, z=12)
        cache.put(coord_z14, _make_solid_jpeg((255, 0, 0)))
        cache.put(coord_z13, _make_solid_jpeg((0, 255, 0)))
        cache.put(coord_z12, _make_solid_jpeg((0, 0, 255)))

        custom = QPixmap(4, 4)
        custom.fill()
        ov = MultiZoomSatelliteOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            zoom_levels=[12, 13, 14],
            tile_cache=cache,
            placeholder=custom,
            initial_view_scale=MULTIZOOM_BASE_VIEW_SCALE,  # → z=14 active
            visibility_pad_factor=0.0,
        )

        # A viewport that comfortably contains all three seeded
        # coords (the chart pixmap is ~5500×7500 px, the tile
        # at z=14 near LLBG sits well inside).
        rect = QRectF(0, 0, size[0], size[1])
        ov.update_visibility(rect, view_scale=MULTIZOOM_BASE_VIEW_SCALE)

        z14_ov = ov.overlay_for_zoom(14)
        z13_ov = ov.overlay_for_zoom(13)
        z12_ov = ov.overlay_for_zoom(12)
        assert z14_ov is not None and z13_ov is not None and z12_ov is not None
        # At active=z=14, every layer at-or-below the active
        # should have loaded its in-viewport seed tile.
        assert z14_ov.is_tile_loaded(coord_z14)
        assert z13_ov.is_tile_loaded(coord_z13)
        assert z12_ov.is_tile_loaded(coord_z12)
        ov.teardown()
