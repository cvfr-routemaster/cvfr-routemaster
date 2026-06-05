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

"""Toolbar Map Type switcher + ``_switch_map_mode`` orchestration (v4)."""

from __future__ import annotations

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QSettings  # noqa: E402
from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import QApplication, QToolButton  # noqa: E402

from cvfr_routemaster import settings_store  # noqa: E402
from cvfr_routemaster.route import Route  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    ini_path = tmp_path / "test_settings.ini"
    monkeypatch.setattr(
        settings_store,
        "_settings",
        lambda: QSettings(str(ini_path), QSettings.Format.IniFormat),
    )
    return ini_path


@pytest.fixture
def main_window(qapp, tmp_path, isolated_settings, monkeypatch):
    from cvfr_routemaster.main_window import MainWindow

    w = MainWindow(tmp_path)
    # Neither the startup autoload nor a switch-triggered load should run
    # real downloads/rendering in these unit tests.
    monkeypatch.setattr(w, "_maybe_autoload_on_start", lambda: None)
    monkeypatch.setattr(w, "_load_all", lambda: None)
    yield w
    w.close()
    w.deleteLater()
    for _ in range(3):
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()


def test_sources_set_is_mode_aware_lsa_needs_no_back(
    qapp, tmp_path, isolated_settings, monkeypatch
) -> None:
    """Regression for the recurring 'second-launch-in-LSA black screen'.

    LSA is a 2-sheet product (north + south, NO back page), so its
    ``pdf_back`` source is legitimately empty. The autoload gate
    (``_sources_set``) must check only the sources the *active mode*
    declares — otherwise it reads LSA's empty back as 'not configured',
    skips autoload entirely, and the chart never builds (the user sees a
    black viewport until a CVFR↔LSA toggle runs ``_load_all`` directly).
    """
    from cvfr_routemaster.main_window import MainWindow

    settings_store.save_pdf_paths("north.pdf", "south.pdf", "", "lsa")
    settings_store.save_current_map_mode("lsa")
    settings_store.save_autoload_enabled(True)

    # Capture the real gate, then neutralise the __init__ singleShot(150)
    # autoload so it can't fire during teardown; we invoke the real method
    # explicitly below.
    real_autoload = MainWindow._maybe_autoload_on_start
    monkeypatch.setattr(MainWindow, "_maybe_autoload_on_start", lambda self: None)
    w = MainWindow(tmp_path)
    try:
        assert w._map_mode_id == "lsa"
        # Empty back must NOT block autoload for a 2-sheet mode.
        assert w._sources_set() is True

        called: list[int] = []
        monkeypatch.setattr(w, "_load_all", lambda: called.append(1))
        # Drive the REAL gate (not the fixture's no-op) — it must fire.
        real_autoload(w)
        assert called == [1], "autoload must run for LSA with north+south set"
    finally:
        w.close()
        w.deleteLater()
        for _ in range(3):
            qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            qapp.processEvents()


def test_sources_set_cvfr_still_requires_back(
    qapp, tmp_path, isolated_settings, monkeypatch
) -> None:
    """CVFR is a 3-sheet product; an empty back page means it's genuinely
    not fully configured, so autoload must still hold off."""
    from cvfr_routemaster.main_window import MainWindow

    settings_store.save_pdf_paths("north.pdf", "south.pdf", "", "cvfr")
    settings_store.save_current_map_mode("cvfr")

    monkeypatch.setattr(MainWindow, "_maybe_autoload_on_start", lambda self: None)
    w = MainWindow(tmp_path)
    try:
        assert w._map_mode_id == "cvfr"
        assert w._sources_set() is False
    finally:
        w.close()
        w.deleteLater()
        for _ in range(3):
            qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            qapp.processEvents()


def test_chart_download_auto_retries_then_succeeds(
    main_window, monkeypatch
) -> None:
    """A transient download failure is retried automatically (no user
    prompt) and the second attempt succeeds — matching the observed
    'always works on the second try' behaviour."""
    from cvfr_routemaster import main_window as mw
    from cvfr_routemaster.chart_source import ChartFetchError

    w = main_window
    w._progress = None
    monkeypatch.setattr(w, "_wait_responsive_ms", lambda ms: None)

    calls = {"n": 0}

    def fake_download(url, *, sheet_key, dest, on_progress):
        calls["n"] += 1
        if calls["n"] < 2:
            raise ChartFetchError("transient", url=url, sheet_key=sheet_key)

    monkeypatch.setattr(mw, "download_chart_pdf", fake_download)
    monkeypatch.setattr(mw, "load_manifest", lambda root, mode: {})
    monkeypatch.setattr(mw, "save_manifest", lambda root, m, mode: None)

    dialog_calls: list[int] = []
    monkeypatch.setattr(
        w, "_show_chart_download_error_dialog", lambda exc: dialog_calls.append(1)
    )

    dest = w._download_chart_with_retry(sheet_key="north", url="http://x/n.pdf")
    assert calls["n"] == 2  # 1 transient failure + 1 success
    assert dialog_calls == []  # the user was never prompted
    assert dest is not None


def test_chart_download_dialog_only_after_three_attempts(
    main_window, monkeypatch
) -> None:
    """A sustained failure exhausts all three auto-attempts before the
    manual Retry/Settings/Cancel dialog is shown exactly once."""
    from cvfr_routemaster import main_window as mw
    from cvfr_routemaster.chart_source import ChartFetchError

    w = main_window
    w._progress = None
    monkeypatch.setattr(w, "_wait_responsive_ms", lambda ms: None)

    calls = {"n": 0}

    def always_fail(url, *, sheet_key, dest, on_progress):
        calls["n"] += 1
        raise ChartFetchError("down", url=url, sheet_key=sheet_key)

    monkeypatch.setattr(mw, "download_chart_pdf", always_fail)

    dialog_calls: list[int] = []

    def fake_dialog(exc):
        dialog_calls.append(1)
        return 0  # neither RETRY nor OPEN_SETTINGS → treated as Cancel

    monkeypatch.setattr(w, "_show_chart_download_error_dialog", fake_dialog)

    dest = w._download_chart_with_retry(sheet_key="south", url="http://x/s.pdf")
    assert calls["n"] == w._CHART_DOWNLOAD_AUTO_ATTEMPTS == 3
    assert dialog_calls == [1]
    assert dest is None


def test_map_type_switcher_buttons_present(main_window) -> None:
    # Each registered mode is a checkable toggle action (no popup menu).
    cvfr_act = main_window.findChild(QAction, "act_map_mode_cvfr")
    lsa_act = main_window.findChild(QAction, "act_map_mode_lsa")
    assert cvfr_act is not None and lsa_act is not None
    assert cvfr_act.isCheckable() and lsa_act.isCheckable()
    # Rendered as real toolbar buttons (QToolButton) backed by those
    # actions, with the bilingual switcher labels.
    btns = main_window.findChildren(QToolButton)
    texts = {b.text() for b in btns if b.defaultAction() is not None}
    assert 'CVFR - כטר"מ' in texts
    assert 'LSA - אז"מ' in texts


def test_starts_in_cvfr_with_cvfr_checked(main_window) -> None:
    assert main_window._map_mode_id == "cvfr"
    cvfr_act = main_window.findChild(QAction, "act_map_mode_cvfr")
    lsa_act = main_window.findChild(QAction, "act_map_mode_lsa")
    assert cvfr_act.isChecked()
    assert not lsa_act.isChecked()
    assert "CVFR" in cvfr_act.text()


def test_startup_restores_last_used_mode(
    qapp, tmp_path, isolated_settings, monkeypatch
) -> None:
    """A window built after a prior session saved ``lsa`` opens in LSA.

    Guards the close-in-LSA / reopen-in-LSA contract: the active mode is
    persisted globally on switch and read back by ``MainWindow.__init__``
    via ``load_current_map_mode`` + ``coerce_mode_id``.
    """
    settings_store.save_current_map_mode("lsa")

    from cvfr_routemaster.main_window import MainWindow

    w = MainWindow(tmp_path)
    monkeypatch.setattr(w, "_maybe_autoload_on_start", lambda: None)
    monkeypatch.setattr(w, "_load_all", lambda: None)
    try:
        assert w._map_mode_id == "lsa"
        assert w.findChild(QAction, "act_map_mode_lsa").isChecked()
        assert not w.findChild(QAction, "act_map_mode_cvfr").isChecked()
    finally:
        w.close()
        w.deleteLater()
        for _ in range(3):
            qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            qapp.processEvents()


def test_switch_to_lsa_updates_identity_and_sources(main_window) -> None:
    main_window._switch_map_mode("lsa")

    assert main_window._map_mode_id == "lsa"
    assert main_window._map_mode.mode_id == "lsa"
    # LSA's registry default URLs become the configured sources.
    assert "-08" in main_window._source_north
    assert "-08" in main_window._source_south
    # LSA has no back sheet — its source stays empty.
    assert main_window._source_back == ""
    # Persisted globally.
    assert settings_store.load_current_map_mode() == "lsa"
    # Toggle buttons reflect the new mode: LSA lit, CVFR unlit.
    assert main_window.findChild(QAction, "act_map_mode_lsa").isChecked()
    assert not main_window.findChild(QAction, "act_map_mode_cvfr").isChecked()


def test_switch_preserves_per_mode_route(main_window) -> None:
    # Give CVFR a distinct in-memory route object, then switch away and
    # back; the same CVFR route object must return.
    cvfr_route = main_window._route
    main_window._switch_map_mode("lsa")
    assert main_window._route is not cvfr_route  # fresh LSA route
    lsa_route = main_window._route
    main_window._switch_map_mode("cvfr")
    assert main_window._route is cvfr_route
    # And LSA's route is retained for a subsequent switch back.
    main_window._switch_map_mode("lsa")
    assert main_window._route is lsa_route


def test_switch_to_same_mode_is_noop(main_window, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(main_window, "_load_all", lambda: calls.append("load"))
    main_window._switch_map_mode("cvfr")  # already cvfr
    assert calls == []
    assert main_window._map_mode_id == "cvfr"


def _fake_image():
    from PySide6.QtGui import QImage

    img = QImage(4, 4, QImage.Format.Format_RGB888)
    img.fill(0)
    return img


def test_install_cached_maps_miss_returns_false(main_window, monkeypatch) -> None:
    finalize_calls: list[object] = []
    monkeypatch.setattr(
        main_window,
        "_finalize_map_images",
        lambda *a, **k: finalize_calls.append(a),
    )
    assert main_window._install_cached_maps("cvfr") is False
    assert finalize_calls == []


def test_install_cached_maps_hit_calls_finalize(main_window, monkeypatch) -> None:
    img_n, img_s = _fake_image(), _fake_image()
    main_window._map_image_cache["cvfr"] = (img_n, img_s, {"north": object()})
    finalize_calls: list[tuple] = []
    monkeypatch.setattr(
        main_window,
        "_finalize_map_images",
        lambda *a, **k: finalize_calls.append(a),
    )
    assert main_window._install_cached_maps("cvfr") is True
    assert len(finalize_calls) == 1
    # Same cached image objects are handed to the finalize.
    assert finalize_calls[0][0] is img_n
    assert finalize_calls[0][1] is img_s


def test_install_cached_maps_null_entry_is_dropped(main_window, monkeypatch) -> None:
    from PySide6.QtGui import QImage

    main_window._map_image_cache["cvfr"] = (QImage(), QImage(), {})
    monkeypatch.setattr(main_window, "_finalize_map_images", lambda *a, **k: None)
    assert main_window._install_cached_maps("cvfr") is False
    # The garbage entry is purged so the next load re-renders.
    assert "cvfr" not in main_window._map_image_cache


def test_invalidate_map_image_cache(main_window) -> None:
    img_n, img_s = _fake_image(), _fake_image()
    main_window._map_image_cache["cvfr"] = (img_n, img_s, {})
    main_window._map_image_cache["lsa"] = (img_n, img_s, {})
    main_window._invalidate_map_image_cache("cvfr")
    assert "cvfr" not in main_window._map_image_cache
    assert "lsa" in main_window._map_image_cache
    main_window._invalidate_map_image_cache()  # clear all
    assert main_window._map_image_cache == {}


def test_start_map_load_uses_cache_fast_path(main_window, monkeypatch) -> None:
    """A cached mode rebuilds synchronously without spawning the worker."""
    img_n, img_s = _fake_image(), _fake_image()
    main_window._map_image_cache["cvfr"] = (img_n, img_s, {})
    finalize_calls: list[tuple] = []
    monkeypatch.setattr(
        main_window,
        "_finalize_map_images",
        lambda *a, **k: finalize_calls.append(a),
    )
    monkeypatch.setattr(
        main_window, "_preload_other_modes_in_background", lambda: None
    )
    main_window._start_map_load_after_waypoints()
    assert len(finalize_calls) == 1
    # No background render thread was created for the active mode.
    assert main_window._map_thread is None
    assert main_window._map_worker is None


def test_on_map_finished_populates_cache(main_window, monkeypatch) -> None:
    img_n, img_s = _fake_image(), _fake_image()
    monkeypatch.setattr(main_window, "_finalize_map_images", lambda *a, **k: None)
    monkeypatch.setattr(
        main_window, "_preload_other_modes_in_background", lambda: None
    )
    main_window._on_map_finished((img_n, img_s))
    assert "cvfr" in main_window._map_image_cache
    cached_n, cached_s, _info = main_window._map_image_cache["cvfr"]
    assert cached_n is img_n and cached_s is img_s


def test_restored_view_shows_chart_no_items_is_true(main_window) -> None:
    # With no chart items there's nothing to miss, so the defensive
    # fit-to-chart fallback must not trigger.
    main_window._north_item = None
    main_window._south_item = None
    assert main_window._restored_view_shows_chart() is True


class _FakeItem:
    """Stand-in chart item exposing only ``sceneBoundingRect``."""

    def __init__(self, rect):
        self._rect = rect

    def sceneBoundingRect(self):
        return self._rect


class _FakeViewport:
    def __init__(self, w: int, h: int):
        from PySide6.QtCore import QRect

        self._rect = QRect(0, 0, w, h)

    def rect(self):
        return self._rect


class _FakeView:
    """Minimal QGraphicsView stand-in: a fixed viewport size and a fixed
    visible-scene rect returned from ``mapToScene``."""

    def __init__(self, vp_w: int, vp_h: int, visible_scene_rect):
        self._vp = _FakeViewport(vp_w, vp_h)
        self._visible = visible_scene_rect

    def viewport(self):
        return self._vp

    def mapToScene(self, _rect):
        from PySide6.QtGui import QPolygonF

        return QPolygonF(self._visible)


def _coverage_case(main_window, vp_w, vp_h, sheets, visible):
    from PySide6.QtCore import QRectF

    orig_view = main_window._view
    orig_north = main_window._north_item
    orig_south = main_window._south_item
    main_window._north_item = _FakeItem(QRectF(sheets))
    # Single sheet suffices: south united with north == north here.
    main_window._south_item = _FakeItem(QRectF(sheets))
    main_window._view = _FakeView(vp_w, vp_h, QRectF(visible))
    try:
        return main_window._restored_view_shows_chart()
    finally:
        # Restore the real view/items so teardown's closeEvent
        # (_persist_map_view_navigation → self._view.transform()) works.
        main_window._view = orig_view
        main_window._north_item = orig_north
        main_window._south_item = orig_south


def test_restored_view_deep_zoom_inside_chart_is_shown(main_window) -> None:
    # Deep zoom: the visible rect is wholly inside the chart → the chart
    # fills the viewport (coverage ~1). Must be accepted.
    from PySide6.QtCore import QRectF

    shown = _coverage_case(
        main_window, 800, 600,
        sheets=QRectF(0, 0, 1000, 1000),
        visible=QRectF(100, 100, 200, 200),
    )
    assert shown is True


def test_restored_view_zoomed_out_chart_contained_is_shown(main_window) -> None:
    # Zoom out: chart fully contained in the visible rect (coverage ~1 of
    # the chart area). Must be accepted — never override a legit zoom-out.
    from PySide6.QtCore import QRectF

    shown = _coverage_case(
        main_window, 800, 600,
        sheets=QRectF(0, 0, 1000, 1000),
        visible=QRectF(-500, -500, 3000, 3000),
    )
    assert shown is True


def test_restored_view_entirely_off_chart_is_not_shown(main_window) -> None:
    from PySide6.QtCore import QRectF

    shown = _coverage_case(
        main_window, 800, 600,
        sheets=QRectF(0, 0, 1000, 1000),
        visible=QRectF(5000, 5000, 200, 200),
    )
    assert shown is False


def test_restored_view_sliver_overlap_is_not_shown(main_window) -> None:
    # A near-miss that overlaps by a tiny corner used to pass the bare
    # ``intersects()`` check and leave the user staring at a black panel.
    from PySide6.QtCore import QRectF

    shown = _coverage_case(
        main_window, 800, 600,
        sheets=QRectF(0, 0, 1000, 1000),
        visible=QRectF(990, 990, 1000, 1000),
    )
    assert shown is False


def test_restored_view_zero_size_viewport_is_not_shown(main_window) -> None:
    # A 0x0 viewport must report "not shown" so the caller fits rather
    # than trusting a transform applied against an unsized viewport.
    from PySide6.QtCore import QRectF

    shown = _coverage_case(
        main_window, 0, 0,
        sheets=QRectF(0, 0, 1000, 1000),
        visible=QRectF(0, 0, 1000, 1000),
    )
    assert shown is False


def _build_real_chart_scene(w, qapp):
    """Populate the window's real scene/view with two chart sheets and show
    it at a real size — exercising the actual view machinery rather than
    fakes. Returns once the viewport has a usable size."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtWidgets import QGraphicsPixmapItem

    img = QImage(800, 600, QImage.Format.Format_RGB888)
    img.fill(Qt.GlobalColor.darkGreen)
    pm = QPixmap.fromImage(img)
    north = QGraphicsPixmapItem(pm)
    south = QGraphicsPixmapItem(pm)
    south.setPos(0.0, 600.0)
    w._scene.addItem(north)
    w._scene.addItem(south)
    w._north_item = north
    w._south_item = south
    w._scene.setSceneRect(w._scene.itemsBoundingRect())
    w.resize(1000, 800)
    w.show()
    qapp.processEvents()
    return north, south


def test_apply_saved_map_view_fits_real_scene_when_no_saved_nav(
    main_window, qapp
) -> None:
    """End-to-end on the *real* view/scene: with no saved navigation,
    applying the map view fits the chart into the viewport and the chart is
    actually shown (not a black panel)."""
    w = main_window
    w._map_mode_id = "lsa"
    _build_real_chart_scene(w, qapp)
    vp = w._view.viewport().rect()
    if vp.width() <= 0 or vp.height() <= 0:
        import pytest

        pytest.skip("headless platform gave a 0x0 viewport; geometry untestable")
    w._apply_saved_map_view()
    assert w._restored_view_shows_chart() is True


def test_startup_refit_fires_on_resize_not_on_same_size_viewport_change(
    main_window, qapp, monkeypatch
) -> None:
    """Regression for the recurring '2nd-launch LSA black until a mode
    toggle' bug.

    The startup re-fit must:
      * re-apply the saved view on a *genuine* viewport resize while armed
        (the window converging on its final size after a fast PNG-cache
        load), and
      * NOT be triggered or disarmed by a same-size ``viewport_changed`` —
        which the programmatic saved-scroll restore emits, and which the
        previous implementation mistook for 'layout settled', disarming the
        re-fit before the real resize ever arrived.
      * stop re-applying once the settle window closes, so a later manual
        resize doesn't clobber the user's navigation.
    """
    w = main_window
    w._map_mode_id = "lsa"
    _build_real_chart_scene(w, qapp)
    vp = w._view.viewport().rect()
    if vp.width() <= 0 or vp.height() <= 0:
        import pytest

        pytest.skip("headless platform gave a 0x0 viewport; geometry untestable")

    calls: list[int] = []
    monkeypatch.setattr(w, "_apply_saved_map_view", lambda: calls.append(1))

    # Finalize arms the re-fit window.
    w._map_view_layout_pending = True

    # Same-size viewport_changed (saved-scroll restore) must be inert.
    w._view.viewport_changed.emit()
    assert calls == []
    assert w._map_view_layout_pending is True

    # Genuine resize while armed → exactly one re-apply.
    w._view.view_resized.emit()
    assert calls == [1]
    # Still armed (multiple settle resizes may follow) until the timer fires.
    assert w._map_view_layout_pending is True

    # Once the settle window closes, resizes no longer re-apply.
    w._clear_map_view_layout_pending()
    w._view.view_resized.emit()
    assert calls == [1]


def test_switch_to_built_mode_restores_scene_without_reload(
    main_window, monkeypatch
) -> None:
    """Switching to an already-built mode is an O(1) scene swap, no reload."""
    from PySide6.QtWidgets import QGraphicsScene
    from cvfr_routemaster.main_window import _ModeScene

    w = main_window
    lsa_scene = QGraphicsScene(w)
    w._mode_scenes["lsa"] = _ModeScene(
        scene=lsa_scene, built=True, selected="north"
    )
    load_calls: list[int] = []
    monkeypatch.setattr(w, "_load_all", lambda: load_calls.append(1))
    monkeypatch.setattr(w, "_apply_saved_map_view", lambda: None)
    monkeypatch.setattr(w, "_sync_restored_satellite_state", lambda: None)

    w._switch_map_mode("lsa")

    assert w._map_mode_id == "lsa"
    assert w._scene is lsa_scene
    assert w._view.scene() is lsa_scene
    assert w._selected == "north"
    # The whole point: a built scene is restored, never rebuilt.
    assert load_calls == []
    # Traffic overlay follows the active scene.
    assert w._traffic_overlay._scene is lsa_scene


def test_switch_to_unbuilt_mode_activates_fresh_scene_and_loads(
    main_window, monkeypatch
) -> None:
    w = main_window
    original_scene = w._scene
    load_calls: list[int] = []
    monkeypatch.setattr(w, "_load_all", lambda: load_calls.append(1))

    w._switch_map_mode("lsa")

    assert w._map_mode_id == "lsa"
    assert w._scene is not original_scene  # a fresh empty scene
    assert w._view.scene() is w._scene
    assert load_calls == [1]
    # The outgoing CVFR scene stays resident for an instant switch back.
    assert "cvfr" in w._mode_scenes
    assert w._mode_scenes["cvfr"].scene is original_scene


def test_switch_round_trip_keeps_scenes_resident(main_window, monkeypatch) -> None:
    from PySide6.QtWidgets import QGraphicsScene
    from cvfr_routemaster.main_window import _ModeScene

    w = main_window
    cvfr_scene = w._scene
    w._mode_scenes["cvfr"].built = True  # pretend CVFR finished a load
    lsa_scene = QGraphicsScene(w)
    w._mode_scenes["lsa"] = _ModeScene(scene=lsa_scene, built=True)
    monkeypatch.setattr(w, "_load_all", lambda: None)
    monkeypatch.setattr(w, "_apply_saved_map_view", lambda: None)
    monkeypatch.setattr(w, "_sync_restored_satellite_state", lambda: None)

    w._switch_map_mode("lsa")
    assert w._scene is lsa_scene
    w._switch_map_mode("cvfr")
    assert w._scene is cvfr_scene
    assert w._view.scene() is cvfr_scene
