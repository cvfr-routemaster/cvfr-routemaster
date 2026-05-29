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

"""Unit tests for :class:`cvfr_routemaster.map_graphics_view.MapGraphicsView`.

The view's mouse-state machine is small but load-bearing for the chart
calibration workflow — it owns the modifier vocabulary (plain drag pans,
plain wheel zooms, Shift adds anchors / route waypoints, Alt+wheel
rescales the selected sheet) and it owns the *absence* of one gesture
that used to exist: Alt+drag manual sheet movement, removed when the
joint LSQ calibration solver took over alignment. These tests pin both
the present-tense gestures and the explicit "Alt+left is a no-op" rule
so a future refactor can't silently reintroduce hand-dragging.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent, QPixmap, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QGraphicsPixmapItem,
    QGraphicsScene,
)

from cvfr_routemaster.map_graphics_view import MapGraphicsView  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    return app


@pytest.fixture()
def view_with_sheet(qapp: QApplication):
    """A scene containing a single ``QGraphicsPixmapItem`` tagged as the
    south sheet, sitting inside a :class:`MapGraphicsView`. The view is
    shown (``show()``) so Qt assigns it a real viewport rect and the
    mouse-event coordinate mapping resolves; tests rely on that.
    """
    scene = QGraphicsScene()
    pm = QPixmap(200, 200)
    pm.fill(Qt.GlobalColor.white)
    item = QGraphicsPixmapItem(pm)
    item.setData(MapGraphicsView.SHEET_ROLE, "south")
    item.setPos(0.0, 0.0)
    item.setScale(1.0)
    scene.addItem(item)

    view = MapGraphicsView()
    view.setScene(scene)
    view.resize(400, 400)
    view.show()
    # Reset the scene rect so QGraphicsView's auto-fit doesn't reposition
    # things between tests.
    scene.setSceneRect(-1000.0, -1000.0, 2000.0, 2000.0)
    qapp.processEvents()
    yield view, item
    view.hide()
    view.deleteLater()
    qapp.processEvents()


def _press(view: MapGraphicsView, pos: QPointF, modifiers: Qt.KeyboardModifier) -> None:
    """Synthesize a left-button press at ``pos`` (viewport-local) with
    the given modifier flags. We construct the event directly rather
    than via ``QTest.mousePress`` so we can attach arbitrary modifier
    combinations (``QTest`` is awkward about that on some Qt builds).
    """
    ev = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        pos,
        view.viewport().mapToGlobal(pos.toPoint()),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        modifiers,
    )
    view.mousePressEvent(ev)


def _move(view: MapGraphicsView, pos: QPointF, modifiers: Qt.KeyboardModifier) -> None:
    ev = QMouseEvent(
        QEvent.Type.MouseMove,
        pos,
        view.viewport().mapToGlobal(pos.toPoint()),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        modifiers,
    )
    view.mouseMoveEvent(ev)


def _release(view: MapGraphicsView, pos: QPointF, modifiers: Qt.KeyboardModifier) -> None:
    ev = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        pos,
        view.viewport().mapToGlobal(pos.toPoint()),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        modifiers,
    )
    view.mouseReleaseEvent(ev)


# ---------------------------------------------------------------------------
# Alt + left is a no-op — historical Alt+drag gesture removed
# ---------------------------------------------------------------------------


class TestAltLeftIsNoOp:
    """The Alt+drag chart-sheet move gesture used to be the way to align
    the two sheets manually. The joint LSQ calibration solver superseded
    it, and hand-dragging would silently invalidate the math-optimal
    pose, so the gesture was removed. The press is still swallowed so
    Qt's default rubber-band selection doesn't start on Alt+left, but
    nothing else changes — no item moves, no controller call fires, no
    pan begins.
    """

    def test_alt_press_does_not_move_the_sheet(
        self, view_with_sheet
    ) -> None:
        view, item = view_with_sheet
        start_pos = item.pos()
        start_scale = item.scale()
        _press(
            view,
            QPointF(100.0, 100.0),
            Qt.KeyboardModifier.AltModifier,
        )
        assert item.pos() == start_pos
        assert item.scale() == start_scale

    def test_alt_drag_does_not_move_the_sheet(
        self, view_with_sheet
    ) -> None:
        """Press + drag-move + release with Alt held — the historical
        full gesture. The chart pixmap must not move a pixel.
        """
        view, item = view_with_sheet
        start_pos = item.pos()
        _press(
            view,
            QPointF(100.0, 100.0),
            Qt.KeyboardModifier.AltModifier,
        )
        _move(
            view,
            QPointF(150.0, 140.0),
            Qt.KeyboardModifier.AltModifier,
        )
        _move(
            view,
            QPointF(200.0, 180.0),
            Qt.KeyboardModifier.AltModifier,
        )
        _release(
            view,
            QPointF(200.0, 180.0),
            Qt.KeyboardModifier.AltModifier,
        )
        assert item.pos() == start_pos

    def test_alt_press_does_not_start_a_pan(
        self, view_with_sheet
    ) -> None:
        """Alt+left must not be silently mis-classified as the start of
        a plain-left pan — that's the bug shape the swallow-but-no-op
        comment in ``mousePressEvent`` is explicitly guarding against.
        ``_pan_press_pos`` is the canonical marker for "a pan gesture is
        in progress"; it must stay ``None``.
        """
        view, _item = view_with_sheet
        _press(
            view,
            QPointF(100.0, 100.0),
            Qt.KeyboardModifier.AltModifier,
        )
        assert view._pan_press_pos is None  # noqa: SLF001
        assert view._pan_active is False  # noqa: SLF001

    def test_no_alt_drag_state_attribute_remains(
        self, view_with_sheet
    ) -> None:
        """The old ``_alt_drag`` / ``_alt_item_start`` instance
        attributes are gone — pinning their absence guarantees a future
        copy/paste can't accidentally restore the half-removed gesture
        in a state-storing-but-no-action form.
        """
        view, _item = view_with_sheet
        assert not hasattr(view, "_alt_drag")
        assert not hasattr(view, "_alt_item_start")


# ---------------------------------------------------------------------------
# Alt + wheel still rescales the selected sheet — escape hatch retained
# ---------------------------------------------------------------------------


class _RecordingController:
    """Stub controller exposing the methods ``MapGraphicsView`` calls:
    only ``scale_selected_layer`` is exercised in these tests, but the
    others have to exist so the view's ``getattr``-free call sites don't
    blow up if a future change starts calling them on wheel events.
    """

    def __init__(self) -> None:
        self.scale_calls: list[float] = []
        self.select_calls: list[str] = []
        # Click-to-track plumbing (see TestTrafficClickToTrack below).
        # ``_tracked`` mirrors what MainWindow would carry; the test
        # records every ``set_tracked_callsign`` call (including
        # explicit ``None`` releases) so a test can assert on the
        # full sequence of selection edits.
        self.tracked_calls: list[str | None] = []
        self._tracked: str | None = None

    def scale_selected_layer(self, factor: float) -> None:
        self.scale_calls.append(factor)

    def select_layer(self, sheet_id: str) -> None:
        self.select_calls.append(sheet_id)

    def try_calibration_click(self, *_args, **_kwargs) -> bool:
        return False

    def try_route_click(self, *_args, **_kwargs) -> bool:
        return False

    def set_tracked_callsign(self, callsign: str | None) -> None:
        self.tracked_calls.append(callsign)
        self._tracked = callsign

    def tracked_callsign(self) -> str | None:
        return self._tracked


def _wheel(
    view: MapGraphicsView,
    pos: QPointF,
    delta_y: int,
    modifiers: Qt.KeyboardModifier,
) -> None:
    """Synthesize a vertical wheel event at ``pos`` (viewport-local).

    ``delta_y`` is the Qt ``angleDelta().y()`` value in 1/8-degree units —
    120 = one physical notch (the convention every Qt-supported mouse and
    OS reports to). Positive values scroll "up" / zoom in by convention.
    """
    from PySide6.QtCore import QPoint

    ev = QWheelEvent(
        pos,
        view.viewport().mapToGlobal(pos.toPoint()),
        QPoint(0, 0),
        QPoint(0, int(delta_y)),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
        Qt.MouseEventSource.MouseEventNotSynthesized,
    )
    view.wheelEvent(ev)


class TestAltWheelEscapeHatch:
    def test_alt_wheel_delegates_to_scale_selected_layer(
        self, view_with_sheet
    ) -> None:
        """Alt+wheel must still call the controller's scale hook — it's
        the documented escape hatch for nudging the joint-LSQ-derived
        sheet pose.
        """
        view, _item = view_with_sheet
        ctrl = _RecordingController()
        view.set_controller(ctrl)
        _wheel(
            view,
            QPointF(100.0, 100.0),
            delta_y=120,
            modifiers=Qt.KeyboardModifier.AltModifier,
        )
        assert len(ctrl.scale_calls) == 1, ctrl.scale_calls
        # One notch up at the coarse step is ~+5.5 % per notch.
        assert 1.0 < ctrl.scale_calls[0] < 1.1

    def test_alt_shift_wheel_uses_fine_per_notch_step(
        self, view_with_sheet
    ) -> None:
        """Adding Shift swaps the coarse ~5.5 %-per-notch step for the
        fine ~0.05 %-per-notch step. Pin the magnitude band so a future
        constant tweak doesn't silently inflate the "fine" step into
        something the user notices as a jump.
        """
        view, _item = view_with_sheet
        ctrl = _RecordingController()
        view.set_controller(ctrl)
        _wheel(
            view,
            QPointF(100.0, 100.0),
            delta_y=120,
            modifiers=Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.ShiftModifier,
        )
        assert len(ctrl.scale_calls) == 1
        factor = ctrl.scale_calls[0]
        assert 1.0 < factor < 1.001, factor


# ---------------------------------------------------------------------------
# Click a VATSIM plane to track it; click empty chart to release tracking
# ---------------------------------------------------------------------------


class TestTrafficClickToTrack:
    """A plain (no-modifier) left click on a ``_TrafficPlaneItem`` must
    fire ``set_tracked_callsign(<callsign>)`` on the controller and
    consume the event so the sheet-selection branch does NOT also run
    (selecting the underlying sheet on every "follow this plane" click
    would be jarring). A plain left click on empty chart, when tracking
    is currently active, must release tracking by calling
    ``set_tracked_callsign(None)`` and then fall through to the normal
    sheet-selection logic.
    """

    @pytest.fixture
    def view_with_traffic(self, qapp: QApplication):
        """Scene with one sheet pixmap + one traffic plane sitting at a
        known scene coordinate. The view is shown so the
        viewport-to-scene mapping resolves.
        """
        from cvfr_routemaster.traffic_overlay import _TrafficPlaneItem
        from cvfr_routemaster.vatsim_feed import Pilot

        scene = QGraphicsScene()
        # Background sheet under the traffic item so the
        # sheet-selection fall-through has something to pick.
        pm = QPixmap(400, 400)
        pm.fill(Qt.GlobalColor.white)
        sheet = QGraphicsPixmapItem(pm)
        sheet.setData(MapGraphicsView.SHEET_ROLE, "south")
        sheet.setPos(0.0, 0.0)
        scene.addItem(sheet)

        pilot = Pilot(
            cid=1,
            callsign="TRACKME",
            name="Test",
            lat=32.0, lon=35.0,
            altitude_ft=10000,
            groundspeed_kts=200,
            heading_deg=90,
            transponder="1234",
            aircraft_type="B738",
            wake="M",
            flight_rules="I",
            departure="LLBG",
            arrival="LCLK",
        )
        plane = _TrafficPlaneItem(pilot, icon_size_px=32)
        # Put the plane at a known scene coordinate so the click point
        # under it is deterministic. The plane uses
        # ItemIgnoresTransformations, so its visual extent is in
        # screen pixels around this scene point.
        plane.setPos(200.0, 200.0)
        scene.addItem(plane)

        view = MapGraphicsView()
        view.setScene(scene)
        view.resize(400, 400)
        view.show()
        scene.setSceneRect(-200.0, -200.0, 800.0, 800.0)
        # Centre the view on the plane's scene position so the
        # viewport-pixel (200, 200) maps to the scene point we put
        # the plane at. With AnchorUnderMouse already set, ``centerOn``
        # is the cleanest way to pin the view-to-scene mapping for
        # the test.
        view.centerOn(200.0, 200.0)
        qapp.processEvents()
        yield view, plane, pilot
        view.hide()
        view.deleteLater()
        qapp.processEvents()

    def _viewport_pos_of(self, view: MapGraphicsView, scene_pt: QPointF) -> QPointF:
        """Map a scene coordinate to a viewport-local QPointF the
        synthetic mouse events accept."""
        # ``mapFromScene(QPointF)`` returns a ``QPoint`` already; the
        # synthetic mouse helpers want a ``QPointF``, so wrap it.
        p = view.mapFromScene(scene_pt)
        return QPointF(p)

    def test_plain_click_on_plane_starts_tracking(
        self, view_with_traffic
    ) -> None:
        view, plane, pilot = view_with_traffic
        ctrl = _RecordingController()
        view.set_controller(ctrl)

        # Click straight on the plane's scene anchor.
        pt = self._viewport_pos_of(view, QPointF(200.0, 200.0))
        _press(view, pt, Qt.KeyboardModifier.NoModifier)
        _release(view, pt, Qt.KeyboardModifier.NoModifier)

        assert ctrl.tracked_calls == [pilot.callsign]
        # The plane-click must NOT also fire sheet-selection — that
        # would yank the user's selected layer on every track click.
        assert ctrl.select_calls == []

    def test_plain_click_on_empty_chart_while_tracking_releases(
        self, view_with_traffic
    ) -> None:
        view, _plane, pilot = view_with_traffic
        ctrl = _RecordingController()
        ctrl.set_tracked_callsign(pilot.callsign)
        # Discard the bookkeeping side-effect of the setup call so we
        # can assert on the click's *own* tracking edit.
        ctrl.tracked_calls.clear()
        view.set_controller(ctrl)

        # Click far away from the plane (still on the south sheet).
        pt = self._viewport_pos_of(view, QPointF(50.0, 50.0))
        _press(view, pt, Qt.KeyboardModifier.NoModifier)
        _release(view, pt, Qt.KeyboardModifier.NoModifier)

        # The release fired exactly one tracking edit, and it cleared
        # the selection.
        assert ctrl.tracked_calls == [None]
        # Sheet selection still runs on a release with no plane hit —
        # the click was on chart, not on a plane.
        assert ctrl.select_calls == ["south"]

    def test_plain_click_on_empty_chart_without_tracking_is_noop(
        self, view_with_traffic
    ) -> None:
        """If nothing is currently being tracked, an empty-chart click
        must not call ``set_tracked_callsign`` at all — emitting a
        spurious ``None`` would create noise in the tracking-edit
        observation channel (e.g. the future status-bar "tracking
        stopped" message would fire on every chart click)."""
        view, _plane, _pilot = view_with_traffic
        ctrl = _RecordingController()
        view.set_controller(ctrl)

        pt = self._viewport_pos_of(view, QPointF(50.0, 50.0))
        _press(view, pt, Qt.KeyboardModifier.NoModifier)
        _release(view, pt, Qt.KeyboardModifier.NoModifier)

        assert ctrl.tracked_calls == []
        # Sheet selection still fires on a plain click on empty chart.
        assert ctrl.select_calls == ["south"]

    def test_hit_test_helper_returns_topmost_callsign(
        self, view_with_traffic
    ) -> None:
        """The ``_hit_test_traffic_callsign`` helper is the contract
        the click branch relies on; pinning it directly avoids
        flakiness around exactly where a synthesised mouse event
        lands inside the silhouette."""
        view, _plane, pilot = view_with_traffic
        assert view._hit_test_traffic_callsign(  # noqa: SLF001
            QPointF(200.0, 200.0)
        ) == pilot.callsign

    def test_hit_test_helper_returns_none_off_plane(
        self, view_with_traffic
    ) -> None:
        view, _plane, _pilot = view_with_traffic
        # Empty area well away from the plane but still on the sheet.
        assert (
            view._hit_test_traffic_callsign(QPointF(50.0, 50.0))  # noqa: SLF001
            is None
        )
