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

import math
from typing import Any

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QCursor, QResizeEvent, QWheelEvent
from PySide6.QtWidgets import QGraphicsView


def _wheel_vertical_delta(event: QWheelEvent) -> float:
    """
    Reliable vertical wheel delta across mice, touchpads, and Windows high-resolution scroll.
    Qt typically uses 120 units per physical notch for angleDelta().y().
    """
    dy = float(event.angleDelta().y())
    if dy == 0.0:
        dy = float(event.pixelDelta().y())
    if dy == 0.0:
        dy = float(event.angleDelta().x())
    if dy == 0.0:
        dy = float(event.pixelDelta().x())
    if event.inverted():
        dy = -dy
    return dy


def _zoom_factor_from_wheel(delta_y: float, *, per_notch: float) -> float:
    """Convert accumulated wheel delta to a multiplicative zoom factor."""
    return math.pow(per_notch, delta_y / 120.0)


# Smaller step = finer zoom (multiplicative factor per 120-unit notch).
_VIEW_ZOOM_PER_NOTCH = 1.055
_LAYER_ZOOM_PER_NOTCH = 1.055
# Fine-grained sheet-scale modifier used during the "perfectly align
# the two sheets" finishing pass. ~0.05% per wheel notch (~100× finer
# than the coarse step). On a 4000-px-wide sheet that's ~2 px of
# linear motion per notch — small enough that pixel-level drift
# between the two sheets can be dialled out without overshooting,
# but big enough that the user still feels each notch land instead
# of having to spin the wheel through dozens of dead clicks.
#
# 0.5% was the first attempt at "fine"; the user reported it was
# still too coarse at typical CVFR sheet sizes, so we drop another
# decade to get genuinely pixel-resolution control. Engaged by
# adding Shift on top of Alt+wheel.
_LAYER_ZOOM_PER_NOTCH_FINE = 1.0005

# How far (in viewport pixels) the cursor must travel between mouse-press
# and the first mouse-move before we treat a plain left-button gesture as
# a pan instead of a sheet-selection click. Below this threshold the
# gesture is interpreted as a click on mouse-release; at-or-above it the
# cursor switches to closed-hand and subsequent moves scroll the view.
#
# 5 px is the same de-facto threshold Qt's own drag-detect machinery uses
# (``QApplication.startDragDistance()`` defaults to 4–10 depending on
# platform) — small enough that a deliberate drag feels instant, large
# enough that a jittery click (especially on a touchpad while operating
# the cursor with the pilot's non-dominant hand mid-flight, which is the
# whole reason the modifier-free flow exists in the first place) is
# still classified as a click and selects the sheet.
_PAN_DRAG_THRESHOLD_PX = 5


class MapGraphicsView(QGraphicsView):
    """
    - Click (no modifier) on a sheet: select north/south.
    - During calibration: Shift+click records points (plain clicks show a hint).
    - Outside calibration: Shift+left adds the chart waypoint nearest the click to the
      flight route; Shift+right removes the route point nearest the click.
    - Plain left-drag: pan the view. A press-then-release without crossing the
      drag-distance threshold (see ``_PAN_DRAG_THRESHOLD_PX``) is still
      interpreted as a sheet-selection click — only an actual drag converts
      to a pan, so the one-handed mouse workflow (the whole reason this
      doesn't need a Ctrl modifier any more) keeps the click-to-select
      affordance intact.
    - Plain wheel: zoom the whole view (was Ctrl+wheel before; modifier
      dropped so a pilot mid-flight can zoom one-handed).
    - Alt + wheel: scale the selected map layer (delegates to MainWindow). Used
      as an escape hatch for fine-tuning the joint-LSQ-derived layout — the
      joint fit handles sheet alignment automatically, so the user shouldn't
      need this in normal operation. Adding Shift drops the step to ~0.05 %
      per notch for pixel-level dial-in.
    - Alt + left-button gestures are intentionally a no-op (consumed without
      changing state). The previous Alt + drag gesture moved a chart sheet
      manually and predates the joint LSQ layout solver — keeping it would
      let the user silently invalidate the math-optimal sheet pose, so it's
      been removed. The press is still swallowed so the underlying
      QGraphicsView default doesn't start a rubber-band selection on it.
    """

    SHEET_ROLE = 500

    #: Emitted whenever the visible scene rect could have changed —
    #: after a scroll, a wheel-zoom (which Qt routes through
    #: ``scrollContentsBy`` for the AnchorUnderMouse re-centre), or
    #: a window resize. Consumers (currently the satellite tile
    #: overlay's lazy-load driver) connect this to a debounced slot
    #: so they don't run a 10 k-item visibility walk on every
    #: scrolled pixel.
    #:
    #: We deliberately don't try to compute the *new* rect here
    #: and pass it as the signal payload — the consumer maps via
    #: ``self.mapToScene(self.viewport().rect()).boundingRect()``,
    #: which is what they actually need (it accounts for the view
    #: transform; passing a viewport-local rect would require the
    #: consumer to redo the transform anyway).
    viewport_changed = Signal()

    #: Emitted *only* on an actual widget resize (not on scroll or zoom).
    #: ``viewport_changed`` fires for scroll/zoom/resize alike, so it can't
    #: distinguish a real geometry change from a programmatic scroll — which
    #: matters for the startup one-shot map re-fit (a saved-scroll restore
    #: emits ``viewport_changed`` at the *same* size and would otherwise be
    #: mistaken for "layout settled"). This signal fires once per genuine
    #: resize, so the controller can re-apply the saved view as the window
    #: converges on its final size at startup.
    view_resized = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._controller: Any = None
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.viewport().setCursor(QCursor(Qt.CursorShape.ArrowCursor))

        # Plain-left-button gesture state machine:
        #   * ``_pan_press_pos`` is the viewport-local QPointF where the
        #     button went down. ``None`` means "no plain-left gesture in
        #     progress" (the button is up, or it went down under a
        #     modifier and was consumed by Shift-add-waypoint / etc).
        #   * ``_pan_active`` flips to True the first time mouseMoveEvent
        #     sees the cursor leave a ``_PAN_DRAG_THRESHOLD_PX`` radius
        #     around the press point. From then on the gesture is
        #     definitely a pan and mouseReleaseEvent **must not** dispatch
        #     the sheet-selection click — that mis-fire would
        #     unintentionally swap the active sheet at the end of every
        #     pan.
        #   * ``_pan_press_sheet`` records the sheet id under the press
        #     point so a release-without-drag can deterministically
        #     select that sheet.
        self._pan_press_pos: QPointF | None = None
        self._pan_press_sheet: str | None = None
        self._pan_active: bool = False
        self._pan_last_pos: QPointF | None = None
        # Cursor in effect on the viewport BEFORE a pan promoted the
        # gesture to "drag". Captured the moment we flip the cursor to
        # closed-hand and restored verbatim on mouseReleaseEvent so a
        # pan-in-calibration-mode doesn't drop the precision reticle:
        # the controller installs the reticle via
        # ``QGraphicsView.viewport().setCursor(...)``, the pan
        # state-machine here would otherwise overwrite that with the
        # closed-hand and then "restore" to a hard-coded ArrowCursor,
        # leaving the user calibrating with the default cursor.
        self._pan_prev_cursor: QCursor | None = None

    def set_controller(self, controller: Any) -> None:
        self._controller = controller

    # ------------------------------------------------------------------
    # Viewport change notifications (for satellite overlay lazy load)
    # ------------------------------------------------------------------

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        """Emit ``viewport_changed`` after every scroll or zoom-pan.

        Qt routes scroll bar moves *and* the implicit re-centre done
        by AnchorUnderMouse during wheel zoom through this method,
        so a single override catches both event paths. We forward
        to ``super()`` first so the actual scroll happens, then
        emit — the consumer reads ``mapToScene(viewport().rect())``
        and gets the post-scroll rect.
        """
        super().scrollContentsBy(dx, dy)
        self.viewport_changed.emit()

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Emit ``viewport_changed`` on window resize.

        A window resize without a scroll changes the visible scene
        rect (the viewport got bigger or smaller in scene-px terms,
        even though the centre stayed put), and ``scrollContentsBy``
        does *not* fire in that case — Qt re-flows the scroll
        bars but doesn't pretend it's a scroll. So we override
        resize separately.
        """
        super().resizeEvent(event)
        self.viewport_changed.emit()
        self.view_resized.emit()

    def wheelEvent(self, event: QWheelEvent) -> None:
        mods = event.modifiers()
        dy = _wheel_vertical_delta(event)

        if mods & Qt.KeyboardModifier.AltModifier:
            # Alt+wheel scales the *selected* map layer (north or south)
            # independently of the view transform. The joint-LSQ calibration
            # produces a math-optimal sheet pose automatically, so this is
            # an escape hatch for the rare case the user wants to nudge the
            # solver's result rather than the everyday alignment tool it
            # used to be. Sibling gesture Alt+drag was removed entirely on
            # the same rationale.
            #
            # Adding Shift swaps the coarse 5.5%-per-notch step for the
            # fine 0.05%-per-notch step so the user can dial in the last
            # pixel or two without overshooting.
            if self._controller and abs(dy) >= 1e-6:
                per_notch = (
                    _LAYER_ZOOM_PER_NOTCH_FINE
                    if mods & Qt.KeyboardModifier.ShiftModifier
                    else _LAYER_ZOOM_PER_NOTCH
                )
                factor = _zoom_factor_from_wheel(dy, per_notch=per_notch)
                self._controller.scale_selected_layer(factor)
            event.accept()
            return
        # Plain wheel (no modifier) now zooms the view.
        #
        # Pre-flight-test version of this app required Ctrl+wheel to zoom
        # because the default QGraphicsView pan-on-wheel could shove the
        # chart out of frame on a sloppy scroll. The user has since asked
        # to drop the Ctrl modifier so the chart can be zoomed
        # one-handed mid-flight (one hand on the yoke, one on the
        # mouse); a misframe is now recoverable via a plain drag-pan, so
        # the cost of "accidental scroll might zoom by a tick" is lower
        # than the cost of "needs two hands to zoom".
        if abs(dy) < 1e-6:
            event.accept()
            return
        factor = _zoom_factor_from_wheel(dy, per_notch=_VIEW_ZOOM_PER_NOTCH)
        self.scale(factor, factor)
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            scene_pt = self.mapToScene(event.position().toPoint())
            mods = event.modifiers()
            if mods & Qt.KeyboardModifier.AltModifier:
                # Alt+left used to start a manual chart-sheet drag — that
                # gesture predates the joint LSQ layout solver, which now
                # produces an optimal sheet pose automatically from the
                # shared overlap anchors. Hand-dragging the chart silently
                # invalidates that math-optimal pose, so the gesture has
                # been removed.
                #
                # We still swallow the press so the underlying
                # QGraphicsView default doesn't start a rubber-band
                # selection on Alt+left and so the press doesn't get
                # mis-classified as the start of a plain-left pan on the
                # subsequent move events. Alt+wheel (scale) and Alt+Shift
                # +wheel (fine scale) remain wired through ``wheelEvent``
                # as the escape hatch for nudging the solver's result.
                event.accept()
                return

            # Shift-anchored gestures (calibration anchor recording,
            # route waypoint add) must fire on PRESS so the click point
            # is the literal pixel the user aimed at — deferring these
            # to release would let the cursor drift by one or two
            # pixels between the click-down "I aimed at exactly the
            # triangle centre" and the click-up that follows.
            if mods & Qt.KeyboardModifier.ShiftModifier:
                if self._controller and self._controller.try_calibration_click(
                    scene_pt, event
                ):
                    event.accept()
                    return
                if self._controller and self._controller.try_route_click(
                    scene_pt, event
                ):
                    event.accept()
                    return

            # Plain (or Ctrl-only) left press — the user is starting
            # either a click (sheet-selection, or a "use Shift+click"
            # hint during calibration) or a drag-pan. We can't tell
            # which yet, so DON'T fire ``try_calibration_click`` here.
            # That call's plain-click branch shows a status-bar hint
            # ("hold Shift") AND returns True to consume the event,
            # which is exactly the behaviour that broke pan-during-
            # calibration in the previous build — the press never
            # reached the pan state machine. Deferring the call to
            # mouseReleaseEvent (only on a sub-threshold click, see
            # below) preserves the hint for genuine plain-clicks
            # while letting a press-and-drag pan freely.
            #
            # Ctrl is accepted alongside the no-modifier case as a
            # belt-and-braces alias (the user explicitly dropped the
            # Ctrl requirement, but old habits die hard and Ctrl+drag
            # would otherwise be a dead gesture).
            if not (mods & Qt.KeyboardModifier.AltModifier) and not (
                mods & Qt.KeyboardModifier.ShiftModifier
            ):
                # Pre-compute the sheet id at press time so a
                # sub-threshold release later selects what the user
                # initially aimed at, race-free against any
                # intervening layout change.
                chosen_sid: str | None = None
                for it in reversed(self.scene().items(scene_pt)):
                    chosen_sid = self._sheet_id(it)
                    if chosen_sid in ("north", "south"):
                        break
                self._pan_press_pos = QPointF(event.position())
                self._pan_press_sheet = chosen_sid
                self._pan_active = False
                self._pan_last_pos = QPointF(event.position())
                event.accept()
                return

        elif event.button() == Qt.MouseButton.RightButton:
            # Shift+right removes the closest route point. Calibration never consumes
            # right-clicks, so this is unconditional outside the modifier check.
            if (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                and self._controller
            ):
                scene_pt = self.mapToScene(event.position().toPoint())
                if self._controller.try_route_click(scene_pt, event):
                    event.accept()
                    return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._pan_press_pos is not None:
            pos = QPointF(event.position())
            if not self._pan_active:
                # Promote to pan only once the cursor leaves a small
                # threshold around the press point. Until then the
                # gesture is still ambiguous (click vs. drag) and
                # forwarding scroll-bar deltas this early would
                # produce a 1-pixel jitter on every click.
                dx = pos.x() - self._pan_press_pos.x()
                dy = pos.y() - self._pan_press_pos.y()
                if (dx * dx + dy * dy) >= (
                    _PAN_DRAG_THRESHOLD_PX * _PAN_DRAG_THRESHOLD_PX
                ):
                    self._pan_active = True
                    # Snapshot whatever cursor is on the viewport
                    # *right now* so mouseReleaseEvent can restore it
                    # verbatim. This is what keeps the calibration
                    # reticle alive across a pan: the controller
                    # installs the reticle via
                    # ``viewport().setCursor(reticle_pixmap_cursor)``,
                    # and without this snapshot we'd overwrite it with
                    # closed-hand and then "restore" to a hard-coded
                    # ArrowCursor on release — leaving the user
                    # mid-calibration with a plain pointer instead of
                    # the precision target ring.
                    self._pan_prev_cursor = QCursor(self.viewport().cursor())
                    # Switch to the closed-hand cursor so the user
                    # immediately sees that they're now panning (not
                    # selecting).
                    self.viewport().setCursor(
                        QCursor(Qt.CursorShape.ClosedHandCursor)
                    )
            if self._pan_active and self._pan_last_pos is not None:
                delta = pos - self._pan_last_pos
                self._pan_last_pos = pos
                h = self.horizontalScrollBar()
                v = self.verticalScrollBar()
                h.setValue(h.value() - int(delta.x()))
                v.setValue(v.value() - int(delta.y()))
                event.accept()
                return
            # Pan not yet active — still record the latest position so
            # the first promoted move computes its delta against the
            # last sub-threshold position rather than the original
            # press point (gives a smoother start-of-pan).
            self._pan_last_pos = pos
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._pan_press_pos is not None
        ):
            was_active = self._pan_active
            press_sheet = self._pan_press_sheet
            self._pan_press_pos = None
            self._pan_press_sheet = None
            self._pan_active = False
            self._pan_last_pos = None
            if was_active:
                # End of an actual pan — restore the pre-pan cursor
                # (calibration reticle if calibration was active, plain
                # arrow otherwise) and DON'T dispatch a sheet-selection
                # click. The user was navigating the chart, not picking
                # a sheet. Fall back to ArrowCursor if for some reason
                # we never captured a prev cursor (shouldn't happen,
                # belt-and-braces against a future code path that flips
                # _pan_active without going through mouseMoveEvent).
                prev = self._pan_prev_cursor
                self._pan_prev_cursor = None
                if prev is not None:
                    self.viewport().setCursor(prev)
                else:
                    self.viewport().setCursor(QCursor(Qt.CursorShape.ArrowCursor))
                event.accept()
                return
            # Sub-threshold gesture: this was a click, not a drag.
            #
            # Calibration check first: if the workflow is active and
            # the user did a plain click (no Shift), ``try_calibration_click``
            # returns True and shows a status-bar hint reminding them
            # to hold Shift on the triangle. Without this branch the
            # press-then-release-without-moving sequence would silently
            # do nothing in calibration mode (the calibration hint
            # used to fire on press, but the press now belongs to the
            # pan state machine so the hint moved here).
            if self._controller:
                scene_pt = self.mapToScene(event.position().toPoint())
                if self._controller.try_calibration_click(scene_pt, event):
                    event.accept()
                    return
                # Click-to-track VATSIM traffic. The hit-test runs
                # only OUTSIDE calibration (the calibration-click
                # check above already short-circuited that mode)
                # because during calibration every plain click is
                # supposed to surface the "hold Shift" hint, not
                # start tracking. A hit means the user clicked a
                # plane: start tracking that callsign and consume
                # the event so the sheet-selection branch below
                # doesn't ALSO fire (selecting the underlying
                # sheet on every "I want to follow this plane"
                # click would be confusing). A miss + active
                # tracking means the user clicked elsewhere on the
                # chart: stop tracking before falling through to
                # sheet-selection — clicking somewhere new is the
                # natural "release the plane" gesture.
                hit_callsign = self._hit_test_traffic_callsign(scene_pt)
                if hit_callsign is not None:
                    self._controller.set_tracked_callsign(hit_callsign)
                    event.accept()
                    return
                if self._controller.tracked_callsign() is not None:
                    self._controller.set_tracked_callsign(None)
                # Outside calibration, a plain click on a sheet selects
                # that sheet so the subsequent Alt+wheel scale-tuning
                # gesture targets the right one. ``press_sheet`` is the
                # sheet captured at press-time so an intervening layout
                # change can't redirect the selection.
                if press_sheet:
                    self._controller.select_layer(press_sheet)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    @staticmethod
    def _sheet_id(item) -> str | None:  # noqa: ANN001
        while item is not None:
            sid = item.data(MapGraphicsView.SHEET_ROLE)
            if sid in ("north", "south"):
                return str(sid)
            item = item.parentItem()
        return None

    def _hit_test_traffic_callsign(self, scene_pt: QPointF) -> str | None:
        """Topmost VATSIM traffic plane callsign under ``scene_pt``,
        or ``None`` if no plane sits at that point.

        Used by the plain-click branch of ``mouseReleaseEvent`` to
        implement the "click a plane to track it" gesture. Walks
        ``scene().items(scene_pt)`` in Qt's natural ordering
        (front-most first), looking for instances of
        :class:`cvfr_routemaster.traffic_overlay._TrafficPlaneItem`.

        The traffic items carry
        :attr:`ItemIgnoresTransformations` so their visual size is
        in screen pixels, but Qt's hit-testing accounts for that
        automatically — the polygon used for hit-testing is the
        item's ``boundingRect`` transformed back into scene
        coordinates by Qt internally, so the on-screen silhouette
        and label are both clickable at any zoom level.

        Local import of ``_TrafficPlaneItem`` avoids pulling the
        whole traffic overlay module (and through it, the VATSIM
        feed parser, wake-category tables, etc.) into the chart
        view's import graph at module load time. The hit-test is
        called only on actual sub-threshold clicks, so the lazy
        import cost is paid once on first interaction at most.
        """
        from cvfr_routemaster.traffic_overlay import _TrafficPlaneItem

        scene = self.scene()
        if scene is None:
            return None
        for it in scene.items(scene_pt):
            if isinstance(it, _TrafficPlaneItem):
                return it.callsign
        return None
