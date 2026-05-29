"""Tests for MainWindow's plane-tracking integration glue.

The pure math (where to centre the view given a heading) is pinned
by :mod:`tests.test_plane_tracking_math`. The traffic-overlay-side
selection visual is pinned by
:mod:`tests.test_traffic_overlay`. What's left is the integration:

* :meth:`MainWindow.set_tracked_callsign` — propagating state to
  the overlay, status-bar messaging, idempotence.
* :meth:`MainWindow.tracked_callsign` — accessor used by
  ``MapGraphicsView``.
* :meth:`MainWindow._recenter_on_tracked_pilot` — the
  per-snapshot recenter pass: pilot lookup, lost-pilot detection
  (clear + status message), unprojectable-pilot resilience
  (transient skip without dropping tracking), and the actual
  ``centerOn`` call.

Approach: rather than spin up a full ``QMainWindow`` (which needs
maps loaded, calibration anchors, etc.), we call the unbound
methods on a ``_FakeMainWindow`` that exposes only the attributes
the methods touch. This keeps each test laser-focused on a single
behaviour and avoids dragging the chart-loader pipeline into the
test boot path.
"""

from __future__ import annotations

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QPointF  # noqa: E402

from cvfr_routemaster.main_window import MainWindow  # noqa: E402
from cvfr_routemaster.vatsim_feed import Pilot  # noqa: E402


# ---------------------------------------------------------------
# Fakes — just enough surface to satisfy the methods under test.
# ---------------------------------------------------------------


class _FakeOverlay:
    """Stand-in for :class:`TrafficOverlay`. Records every
    ``set_tracked_callsign`` call so tests can assert on the
    selection edits the MainWindow makes."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def set_tracked_callsign(self, callsign: str | None) -> None:
        self.calls.append(callsign)


class _FakeStatusBar:
    """Captures status-bar messages so tests can assert on the
    "Tracking ..." / "Tracking stopped" / "no longer in feed"
    strings without involving a real ``QMainWindow``."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, int]] = []

    def showMessage(self, text: str, timeout_ms: int = 0) -> None:  # noqa: N802
        self.messages.append((text, timeout_ms))


class _FakeViewport:
    def __init__(self, w: int = 1200, h: int = 900) -> None:
        self._w = w
        self._h = h

    def width(self) -> int:
        return self._w

    def height(self) -> int:
        return self._h


class _FakeTransform:
    """Mimics ``QTransform`` enough for the ``.m11()`` accessor the
    recenter helper reads as the device-pixels-per-scene-unit
    scale."""

    def __init__(self, scale: float = 1.0) -> None:
        self._scale = scale

    def m11(self) -> float:
        return self._scale


class _FakeView:
    def __init__(self, *, viewport_w: int = 1200, viewport_h: int = 900,
                 view_scale: float = 1.0) -> None:
        self._viewport = _FakeViewport(viewport_w, viewport_h)
        self._transform = _FakeTransform(view_scale)
        self.center_on_calls: list[QPointF] = []

    def viewport(self) -> _FakeViewport:
        return self._viewport

    def transform(self) -> _FakeTransform:
        return self._transform

    def centerOn(self, point: QPointF) -> None:  # noqa: N802
        # Take a copy so a caller mutating its QPointF afterwards
        # can't retroactively rewrite the test's evidence.
        self.center_on_calls.append(QPointF(point))


class _FakeMainWindow:
    """Carries only the attributes ``set_tracked_callsign``,
    ``tracked_callsign``, and ``_recenter_on_tracked_pilot``
    actually read or write. Constructed bare and customised
    per-test."""

    def __init__(
        self,
        *,
        pilots: list[Pilot] | None = None,
        projection: callable = lambda lat, lon: QPointF(lon, lat),
        view: _FakeView | None = None,
    ) -> None:
        self._tracking_callsign: str | None = None
        self._latest_vatsim_pilots = pilots
        self._traffic_overlay = _FakeOverlay()
        self._view = view if view is not None else _FakeView()
        self._status_bar = _FakeStatusBar()
        self._projection = projection

    # MainWindow uses ``self.statusBar()`` (the Qt accessor on
    # QMainWindow). Mirror that as a method so the unbound calls
    # work unchanged.
    def statusBar(self) -> _FakeStatusBar:  # noqa: N802
        return self._status_bar

    def _project_route_point_to_scene(
        self, lat: float, lon: float
    ) -> QPointF | None:
        return self._projection(lat, lon)


def _make_pilot(
    callsign: str = "EZE1",
    *,
    lat: float = 32.0,
    lon: float = 35.0,
    heading_deg: int = 90,
) -> Pilot:
    """Compact Pilot factory for tests that only care about
    identity + position + heading."""
    return Pilot(
        cid=1,
        callsign=callsign,
        name="Test",
        lat=lat,
        lon=lon,
        altitude_ft=10000,
        groundspeed_kts=200,
        heading_deg=heading_deg,
        transponder="1234",
        aircraft_type="B738",
        wake="M",
        flight_rules="I",
        departure="LLBG",
        arrival="LCLK",
    )


# ---------------------------------------------------------------
# set_tracked_callsign — state + overlay + status bar
# ---------------------------------------------------------------


class TestSetTrackedCallsign:
    def test_begin_tracking_sets_state_and_propagates_to_overlay(
        self,
    ) -> None:
        mw = _FakeMainWindow()
        MainWindow.set_tracked_callsign(mw, "EZE1")
        assert mw._tracking_callsign == "EZE1"
        assert mw._traffic_overlay.calls == ["EZE1"]

    def test_begin_tracking_emits_status_message(self) -> None:
        mw = _FakeMainWindow()
        MainWindow.set_tracked_callsign(mw, "EZE1")
        assert any(
            "Tracking EZE1" in msg for msg, _ in mw._status_bar.messages
        )

    def test_stop_tracking_clears_state_and_emits_message(self) -> None:
        mw = _FakeMainWindow()
        MainWindow.set_tracked_callsign(mw, "EZE1")
        mw._traffic_overlay.calls.clear()
        mw._status_bar.messages.clear()

        MainWindow.set_tracked_callsign(mw, None)
        assert mw._tracking_callsign is None
        assert mw._traffic_overlay.calls == [None]
        assert any(
            "Tracking stopped" in msg
            for msg, _ in mw._status_bar.messages
        )

    def test_setting_same_callsign_twice_is_noop(self) -> None:
        """A second click on the same already-tracked plane mustn't
        flash the status message again or re-issue the overlay
        edit; the user shouldn't see "Tracking EZE1" pop up every
        time they click on the same plane."""
        mw = _FakeMainWindow()
        MainWindow.set_tracked_callsign(mw, "EZE1")
        mw._traffic_overlay.calls.clear()
        mw._status_bar.messages.clear()

        MainWindow.set_tracked_callsign(mw, "EZE1")
        assert mw._traffic_overlay.calls == []
        assert mw._status_bar.messages == []

    def test_clearing_when_already_clear_is_noop(self) -> None:
        """An empty-chart click on an untracked viewport must not
        emit a spurious "Tracking stopped" message — the click
        was a no-op from the tracking-state perspective."""
        mw = _FakeMainWindow()
        MainWindow.set_tracked_callsign(mw, None)
        assert mw._traffic_overlay.calls == []
        assert mw._status_bar.messages == []

    def test_switching_callsign_is_one_edit(self) -> None:
        """Switching from one tracked plane to another fires one
        overlay edit (the new callsign), not two (clear + set)."""
        mw = _FakeMainWindow()
        MainWindow.set_tracked_callsign(mw, "EZE1")
        mw._traffic_overlay.calls.clear()

        MainWindow.set_tracked_callsign(mw, "AA42")
        assert mw._traffic_overlay.calls == ["AA42"]
        assert mw._tracking_callsign == "AA42"


# ---------------------------------------------------------------
# tracked_callsign accessor
# ---------------------------------------------------------------


class TestTrackedCallsignAccessor:
    def test_returns_none_by_default(self) -> None:
        mw = _FakeMainWindow()
        assert MainWindow.tracked_callsign(mw) is None

    def test_returns_set_value(self) -> None:
        mw = _FakeMainWindow()
        MainWindow.set_tracked_callsign(mw, "EZE1")
        assert MainWindow.tracked_callsign(mw) == "EZE1"

    def test_returns_none_after_clear(self) -> None:
        mw = _FakeMainWindow()
        MainWindow.set_tracked_callsign(mw, "EZE1")
        MainWindow.set_tracked_callsign(mw, None)
        assert MainWindow.tracked_callsign(mw) is None


# ---------------------------------------------------------------
# _recenter_on_tracked_pilot — the per-snapshot work
# ---------------------------------------------------------------


class TestRecenterOnTrackedPilot:
    def test_noop_when_no_tracking_active(self) -> None:
        mw = _FakeMainWindow(pilots=[_make_pilot()])
        MainWindow._recenter_on_tracked_pilot(mw)
        assert mw._view.center_on_calls == []
        # No status message, no overlay edit, no state mutation.
        assert mw._status_bar.messages == []
        assert mw._traffic_overlay.calls == []
        assert mw._tracking_callsign is None

    def test_recenters_view_on_tracked_pilot(self) -> None:
        """A tracked pilot whose lat/lon projects to a scene point
        triggers exactly one ``centerOn`` call. The exact target
        point is exercised by the pure-math test file; here we
        only assert that we *called* centerOn (not where)."""
        pilot = _make_pilot(callsign="EZE1", lat=32.0, lon=35.0)
        mw = _FakeMainWindow(pilots=[pilot])
        MainWindow.set_tracked_callsign(mw, "EZE1")
        mw._status_bar.messages.clear()  # discard the "Tracking" message

        MainWindow._recenter_on_tracked_pilot(mw)
        assert len(mw._view.center_on_calls) == 1

    def test_recenter_target_matches_math_helper(self) -> None:
        """Pin the integration to the pure-math helper's output for
        a specific axis-aligned case. East heading -> centre is
        scene-x of pilot + W/6 (since view_scale=1, projection is
        identity in our fake)."""
        # East heading (90deg) means forward = (1, 0), so the
        # centre offset = (W/6, 0) at view_scale=1.
        pilot = _make_pilot(callsign="EZE1", lat=10.0, lon=20.0,
                            heading_deg=90)
        view = _FakeView(viewport_w=1200, viewport_h=900, view_scale=1.0)
        # Projection: identity-ish (lon=scene_x, lat=scene_y).
        mw = _FakeMainWindow(
            pilots=[pilot],
            view=view,
            projection=lambda lat, lon: QPointF(lon, lat),
        )
        MainWindow.set_tracked_callsign(mw, "EZE1")

        MainWindow._recenter_on_tracked_pilot(mw)
        assert len(view.center_on_calls) == 1
        target = view.center_on_calls[0]
        # Pilot at (lon=20, lat=10). East heading -> centre.x =
        # pilot.x + W/6 = 20 + 200 = 220; centre.y = pilot.y = 10.
        assert target.x() == pytest.approx(20.0 + 1200 / 6.0)
        assert target.y() == pytest.approx(10.0)

    def test_lost_pilot_clears_tracking_with_message(self) -> None:
        """The tracked callsign isn't in the new snapshot: the
        method clears tracking, notifies the overlay, and emits
        the "no longer in feed" status message."""
        mw = _FakeMainWindow(pilots=[_make_pilot(callsign="OTHER")])
        MainWindow.set_tracked_callsign(mw, "EZE1")
        mw._traffic_overlay.calls.clear()
        mw._status_bar.messages.clear()

        MainWindow._recenter_on_tracked_pilot(mw)
        assert mw._tracking_callsign is None
        assert mw._traffic_overlay.calls == [None]
        assert any(
            "EZE1" in msg and "no longer in feed" in msg
            for msg, _ in mw._status_bar.messages
        )
        # Lost pilot -> no recenter (there's nothing to centre on).
        assert mw._view.center_on_calls == []

    def test_unprojectable_pilot_is_transient_skip(self) -> None:
        """If the tracked pilot is in the snapshot but their lon/lat
        doesn't project (no sheet calibrated, far off-chart), we
        leave tracking state alone and skip this tick's recenter.
        The user keeps the halo + the tracking-on state, ready for
        the next snapshot to retry."""
        pilot = _make_pilot(callsign="EZE1")
        mw = _FakeMainWindow(
            pilots=[pilot],
            projection=lambda lat, lon: None,
        )
        MainWindow.set_tracked_callsign(mw, "EZE1")
        mw._traffic_overlay.calls.clear()
        mw._status_bar.messages.clear()

        MainWindow._recenter_on_tracked_pilot(mw)
        # State preserved.
        assert mw._tracking_callsign == "EZE1"
        # No overlay edit, no status message.
        assert mw._traffic_overlay.calls == []
        assert mw._status_bar.messages == []
        # And of course no centerOn call.
        assert mw._view.center_on_calls == []

    def test_empty_pilot_snapshot_triggers_lost_message(self) -> None:
        """An empty pilot list is the legitimate "nobody airborne
        in Israeli airspace" state. If we were tracking a pilot,
        that's a lost-pilot from this snapshot's perspective."""
        mw = _FakeMainWindow(pilots=[])
        MainWindow.set_tracked_callsign(mw, "EZE1")
        mw._status_bar.messages.clear()

        MainWindow._recenter_on_tracked_pilot(mw)
        assert mw._tracking_callsign is None
        assert any(
            "no longer in feed" in msg
            for msg, _ in mw._status_bar.messages
        )

    def test_none_pilot_snapshot_treated_as_empty(self) -> None:
        """``_latest_vatsim_pilots`` is ``None`` before the first
        snapshot arrives. Reaching the recenter pass in that state
        shouldn't crash; treat it as an empty list (lost-pilot
        path), since we can't possibly find the tracked callsign
        in a snapshot that hasn't been received."""
        mw = _FakeMainWindow(pilots=None)
        MainWindow.set_tracked_callsign(mw, "EZE1")

        # Must not raise.
        MainWindow._recenter_on_tracked_pilot(mw)
        assert mw._tracking_callsign is None
