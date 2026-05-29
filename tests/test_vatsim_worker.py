"""Tests for :class:`cvfr_routemaster.vatsim_worker.VatsimWorker`.

The worker has three behaviours worth pinning:

1. **Tick → fetch → emit** — calling ``_on_tick`` (the slot the
   internal QTimer wires to) synchronously runs the
   fetch/parse/filter pipeline and emits ``pilots_updated`` with
   the bbox-filtered list.
2. **If-Modified-Since caching** — the worker remembers the
   ``Last-Modified`` header from each successful fetch and feeds
   it back as ``last_modified=...`` on the next call. A
   ``not_modified`` response triggers ``poll_skipped`` instead
   of ``pilots_updated`` so the GUI keeps the old list.
3. **Lifecycle** — ``start_polling`` is idempotent and creates
   the timer on the calling thread (so when wired to
   ``QThread.started`` it lives on the worker thread).
   ``stop_polling`` tears the timer down and prevents stale
   emissions even if a fetch was in-flight.

We don't actually start a :class:`QThread` here — the tests run
the worker on the GUI thread via direct method calls and signal
connections. That's enough to validate the logic without the
threading complexity getting in the way of fast, deterministic
tests.

HTTP is mocked — no test makes a real call to data.vatsim.net.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cvfr_routemaster.vatsim_feed import (  # noqa: E402
    FetchResult,
    Pilot,
    VatsimFetchError,
)
from cvfr_routemaster.vatsim_worker import (  # noqa: E402
    DEFAULT_POLL_INTERVAL_MS,
    ISRAEL_BBOX_MAX_LAT,
    ISRAEL_BBOX_MAX_LON,
    ISRAEL_BBOX_MIN_LAT,
    ISRAEL_BBOX_MIN_LON,
    VatsimWorker,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """One QApplication per process — required for any
    QObject/QTimer machinery to work."""
    app = QApplication.instance() or QApplication([])
    return app


def _make_pilot(
    *,
    callsign: str = "TEST01",
    cid: int = 1,
    lat: float = 32.0,
    lon: float = 35.0,
    altitude_ft: int = 10000,
    groundspeed_kts: int = 200,
    heading_deg: int = 90,
    aircraft_type: str | None = "B738",
    wake: str = "M",
    flight_rules: str = "I",
    departure: str = "LLBG",
    arrival: str = "LCLK",
) -> Pilot:
    return Pilot(
        cid=cid,
        callsign=callsign,
        name="Test Pilot",
        lat=lat,
        lon=lon,
        altitude_ft=altitude_ft,
        groundspeed_kts=groundspeed_kts,
        heading_deg=heading_deg,
        transponder="2000",
        aircraft_type=aircraft_type,
        wake=wake,
        flight_rules=flight_rules,
        departure=departure,
        arrival=arrival,
    )


# --- Israel bbox constants ----------------------------------------------


class TestIsraelBboxConstants:
    """The bbox covers the calibrated chart area plus a small
    buffer for transitioning traffic. Pin the rough shape so a
    future tightening doesn't accidentally exclude Eilat (south
    end) or northern Israel."""

    def test_lat_bounds_cover_eilat_to_lebanon(self) -> None:
        # Eilat ≈ 29.55°N, Lebanon border ≈ 33.1°N.
        assert ISRAEL_BBOX_MIN_LAT <= 29.55
        assert ISRAEL_BBOX_MAX_LAT >= 33.10

    def test_lon_bounds_cover_coast_to_eastern_border(self) -> None:
        # Tel Aviv coast ≈ 34.77°E, eastern Jordan ≈ 35.6°E.
        assert ISRAEL_BBOX_MIN_LON <= 34.77
        assert ISRAEL_BBOX_MAX_LON >= 35.60

    def test_default_poll_interval_is_15_seconds(self) -> None:
        """15 s matches VATSIM's publish cadence and the Code of
        Conduct's polite-polling guidance. Tests that depend on
        the cadence (e.g. UI smoke tests) read this constant
        rather than hardcoding 15000."""
        assert DEFAULT_POLL_INTERVAL_MS == 15_000


# --- Tick logic ---------------------------------------------------------


class TestWorkerTickLogic:
    """Direct tests on the ``_on_tick`` slot — driving each fetch
    case by patching :func:`fetch_vatsim_data` and asserting the
    correct signal fires (or doesn't)."""

    def test_emits_pilots_on_fresh_fetch(self, qapp) -> None:  # noqa: ARG002
        worker = VatsimWorker(wake_db={"B738": "M"})
        captured: list[list[Pilot]] = []
        worker.pilots_updated.connect(captured.append)

        result = FetchResult(
            pilots=[_make_pilot()],
            not_modified=False,
            last_modified="Sun, 17 May 2026 12:00:00 GMT",
        )
        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            return_value=result,
        ):
            worker._on_tick()

        assert len(captured) == 1
        assert len(captured[0]) == 1
        assert captured[0][0].callsign == "TEST01"

    def test_caches_last_modified_for_next_tick(
        self, qapp  # noqa: ARG002
    ) -> None:
        """First fetch returns a Last-Modified header; the worker
        should hand it back to the next ``fetch_vatsim_data`` call
        as ``last_modified=...``. That's the If-Modified-Since
        caching contract."""
        worker = VatsimWorker(wake_db={})
        result = FetchResult(
            pilots=[],
            not_modified=False,
            last_modified="Sun, 17 May 2026 12:00:00 GMT",
        )
        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            return_value=result,
        ) as mock_fetch:
            worker._on_tick()
            worker._on_tick()

        assert mock_fetch.call_count == 2
        # Second call should pass the cached Last-Modified back.
        kwargs = mock_fetch.call_args_list[1].kwargs
        assert kwargs["last_modified"] == "Sun, 17 May 2026 12:00:00 GMT"

    def test_first_tick_sends_no_if_modified_since(
        self, qapp  # noqa: ARG002
    ) -> None:
        """A freshly-constructed worker has no cached header, so
        the very first fetch is unconditional. Tests assert the
        kwarg is explicitly None."""
        worker = VatsimWorker(wake_db={})
        result = FetchResult(
            pilots=[], not_modified=False, last_modified=None
        )
        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            return_value=result,
        ) as mock_fetch:
            worker._on_tick()

        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["last_modified"] is None

    def test_not_modified_emits_poll_skipped_not_pilots_updated(
        self, qapp  # noqa: ARG002
    ) -> None:
        """A 304 response means upstream had nothing new. Worker
        must emit ``poll_skipped`` (no payload) so the GUI keeps
        showing whatever it last drew, NOT ``pilots_updated``
        with an empty list (which would clear the overlay)."""
        worker = VatsimWorker(wake_db={})
        worker._last_modified = "Sun, 17 May 2026 12:00:00 GMT"
        captured_pilots: list[list[Pilot]] = []
        captured_skips: list[None] = []
        worker.pilots_updated.connect(captured_pilots.append)
        worker.poll_skipped.connect(lambda: captured_skips.append(None))

        result = FetchResult(pilots=[], not_modified=True, last_modified=None)
        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            return_value=result,
        ):
            worker._on_tick()

        assert captured_pilots == []
        assert len(captured_skips) == 1

    def test_empty_pilot_list_still_emits_pilots_updated(
        self, qapp  # noqa: ARG002
    ) -> None:
        """Empty list (200 OK with zero pilots in bbox) is a
        legitimate state — "nobody flying right now". Distinct
        from 304 (handled above). The GUI must clear any
        previous pilots so the overlay matches reality."""
        worker = VatsimWorker(wake_db={})
        captured: list[list[Pilot]] = []
        worker.pilots_updated.connect(captured.append)

        result = FetchResult(
            pilots=[], not_modified=False, last_modified="x"
        )
        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            return_value=result,
        ):
            worker._on_tick()

        assert captured == [[]]

    def test_fetch_failure_emits_fetch_failed_keeps_polling(
        self, qapp  # noqa: ARG002
    ) -> None:
        """A network error should surface as ``fetch_failed`` and
        the worker should NOT crash or stop polling. The next
        tick gets a fresh chance — verified by patching with two
        different responses (failure then success) and ticking
        twice."""
        worker = VatsimWorker(wake_db={})
        captured_pilots: list[list[Pilot]] = []
        captured_errors: list[str] = []
        worker.pilots_updated.connect(captured_pilots.append)
        worker.fetch_failed.connect(captured_errors.append)

        responses = [
            VatsimFetchError("Network unreachable"),
            FetchResult(
                pilots=[_make_pilot()], not_modified=False, last_modified="x"
            ),
        ]

        def _side_effect(*args, **kwargs):  # noqa: ARG001
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            side_effect=_side_effect,
        ):
            worker._on_tick()
            worker._on_tick()

        assert captured_errors == ["Network unreachable"]
        assert len(captured_pilots) == 1

    def test_bbox_filter_applied(self, qapp) -> None:  # noqa: ARG002
        """Pilots outside the configured bbox must be dropped
        before emission. Default bbox is Israel; pilots over
        Cyprus / mid-ocean / mid-Australia must not survive."""
        worker = VatsimWorker(wake_db={})
        captured: list[list[Pilot]] = []
        worker.pilots_updated.connect(captured.append)

        in_israel = _make_pilot(callsign="ELY1", lat=32.0, lon=34.9, cid=1)
        in_cyprus = _make_pilot(callsign="CY1", lat=35.0, lon=33.0, cid=2)
        in_australia = _make_pilot(
            callsign="QFA1", lat=-33.0, lon=151.0, cid=3
        )

        result = FetchResult(
            pilots=[in_israel, in_cyprus, in_australia],
            not_modified=False,
            last_modified="x",
        )
        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            return_value=result,
        ):
            worker._on_tick()

        emitted = captured[0]
        callsigns = {p.callsign for p in emitted}
        assert "ELY1" in callsigns
        assert "CY1" not in callsigns
        assert "QFA1" not in callsigns

    def test_custom_bbox_overrides_default(self, qapp) -> None:  # noqa: ARG002
        """Bbox is configurable via __init__ kwargs. Test that a
        deliberately tiny bbox excludes everything — confirms the
        kwarg actually flows through to the filter."""
        worker = VatsimWorker(
            wake_db={},
            min_lat=89.0,
            max_lat=89.5,
            min_lon=0.0,
            max_lon=0.5,
        )
        captured: list[list[Pilot]] = []
        worker.pilots_updated.connect(captured.append)

        result = FetchResult(
            pilots=[_make_pilot()],
            not_modified=False,
            last_modified="x",
        )
        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            return_value=result,
        ):
            worker._on_tick()

        assert captured == [[]]


# --- Lifecycle ----------------------------------------------------------


class TestWorkerLifecycle:
    """``start_polling`` / ``stop_polling`` correctness. These
    tests don't run the worker on a real QThread because the
    tick logic doesn't depend on threading and a real QThread
    adds non-deterministic timing to the tests; the lifecycle
    contract (timer created/stopped, idempotency, racing-safe
    teardown) is what we care about."""

    def test_initial_state_is_not_running(self, qapp) -> None:  # noqa: ARG002
        worker = VatsimWorker(wake_db={})
        assert worker.is_running is False
        assert worker.last_modified is None

    def test_start_polling_creates_timer_and_marks_running(
        self, qapp  # noqa: ARG002
    ) -> None:
        worker = VatsimWorker(wake_db={})
        # Patch fetch so the immediate first tick doesn't try to
        # hit the network.
        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            return_value=FetchResult(
                pilots=[], not_modified=False, last_modified=None
            ),
        ):
            worker.start_polling()
        try:
            assert worker.is_running is True
        finally:
            worker.stop_polling()

    def test_start_polling_is_idempotent(self, qapp) -> None:  # noqa: ARG002
        """Calling ``start_polling`` twice in a row must not
        create two timers — that'd double the fetch rate. The
        second call is a no-op."""
        worker = VatsimWorker(wake_db={})
        fetch_count = 0

        def _counting_fetch(*args, **kwargs):  # noqa: ARG001
            nonlocal fetch_count
            fetch_count += 1
            return FetchResult(
                pilots=[], not_modified=False, last_modified=None
            )

        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            side_effect=_counting_fetch,
        ):
            worker.start_polling()
            worker.start_polling()
            worker.start_polling()
        try:
            # Each start_polling does an immediate first tick;
            # the no-op guard means only the *first* one ticks.
            assert fetch_count == 1
        finally:
            worker.stop_polling()

    def test_stop_polling_marks_not_running(self, qapp) -> None:  # noqa: ARG002
        worker = VatsimWorker(wake_db={})
        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            return_value=FetchResult(
                pilots=[], not_modified=False, last_modified=None
            ),
        ):
            worker.start_polling()
        worker.stop_polling()
        assert worker.is_running is False

    def test_stop_polling_is_safe_when_not_started(
        self, qapp  # noqa: ARG002
    ) -> None:
        """Calling ``stop_polling`` before ``start_polling`` (or
        twice) must not raise — close-event handlers in the GUI
        call this unconditionally during shutdown."""
        worker = VatsimWorker(wake_db={})
        worker.stop_polling()
        worker.stop_polling()  # twice for good measure
        assert worker.is_running is False

    def test_tick_after_stop_does_not_emit(self, qapp) -> None:  # noqa: ARG002
        """Race-safety check: if a fetch is already in flight when
        ``stop_polling`` runs, the post-fetch ``_stopped`` re-check
        must short-circuit before emitting. We simulate by
        flipping ``_stopped`` mid-fetch via the side-effect."""
        worker = VatsimWorker(wake_db={})
        captured: list[list[Pilot]] = []
        worker.pilots_updated.connect(captured.append)

        def _set_stopped_and_return(*args, **kwargs):  # noqa: ARG001
            worker._stopped = True
            return FetchResult(
                pilots=[_make_pilot()],
                not_modified=False,
                last_modified="x",
            )

        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            side_effect=_set_stopped_and_return,
        ):
            worker._on_tick()

        assert captured == []

    def test_tick_when_stopped_does_not_fetch(self, qapp) -> None:  # noqa: ARG002
        """The very first ``_stopped`` check skips even the
        network call — useful so a pending Qt event in the
        worker thread's queue doesn't trigger a fetch after
        teardown."""
        worker = VatsimWorker(wake_db={})
        worker._stopped = True
        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data"
        ) as mock_fetch:
            worker._on_tick()
        mock_fetch.assert_not_called()

    def test_stop_polling_emits_finished(
        self, qapp  # noqa: ARG002
    ) -> None:
        """Pin the shutdown-race fix: ``stop_polling`` must emit
        :attr:`VatsimWorker.finished` after tearing the timer
        down. The GUI wires that signal to ``thread.quit`` with
        ``DirectConnection`` so the worker thread sets its own
        event-loop quit flag *after* the QTimer is already gone.

        Historical bug (reintroduced if this contract regresses):
        the GUI called ``thread.quit()`` immediately after queuing
        ``stop_polling``, the quit-flag check beat event
        dispatch, ``stop_polling`` never ran, and at QApplication
        teardown Qt warned ``QObject::killTimer: Timers cannot
        be stopped from another thread`` + ``QObject::~QObject:
        Timers cannot be stopped from another thread``. Without
        this emission the wired DirectConnection has nothing to
        latch onto and the race is back. See
        :meth:`VatsimWorker.stop_polling`'s "Why the worker emits
        finished itself" section for the full failure mode.
        """
        worker = VatsimWorker(wake_db={})
        finished_count = 0

        def _bump() -> None:
            nonlocal finished_count
            finished_count += 1

        worker.finished.connect(_bump)
        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            return_value=FetchResult(
                pilots=[], not_modified=False, last_modified=None
            ),
        ):
            worker.start_polling()
        worker.stop_polling()
        assert finished_count == 1, (
            "stop_polling must emit finished exactly once so the "
            "GUI's finished→thread.quit DirectConnection wiring "
            "sets the event-loop quit flag on the worker thread "
            "*after* the QTimer is gone."
        )

    def test_stop_polling_emits_finished_even_when_never_started(
        self, qapp  # noqa: ARG002
    ) -> None:
        """``stop_polling`` from a never-started worker must still
        emit ``finished`` so the GUI's shutdown-fast-path wiring
        terminates the worker thread cleanly. The fast-path
        ``_signal_vatsim_worker_stop`` no longer calls
        ``thread.quit()`` directly (avoiding the historical
        race) and instead relies entirely on this signal — if
        ``finished`` isn't emitted, ``_force_stop_threads``
        falls through to ``terminate()``, which leaks any
        QObjects on the worker thread.
        """
        worker = VatsimWorker(wake_db={})
        finished_count = 0
        worker.finished.connect(lambda: None)  # smoke

        def _bump() -> None:
            nonlocal finished_count
            finished_count += 1

        worker.finished.connect(_bump)
        worker.stop_polling()
        assert finished_count == 1

    def test_stop_polling_clears_timer_reference(
        self, qapp  # noqa: ARG002
    ) -> None:
        """Pin the no-``deleteLater`` teardown: after
        ``stop_polling`` the worker must hold no Python
        reference to the QTimer at all. PySide6 owns the
        freestanding ``QTimer()`` via the wrapper's
        refcount, and dropping the ref is what synchronously
        destructs the C++ object on the worker thread (its
        affinity thread) — see :meth:`VatsimWorker.stop_polling`.
        If a future change accidentally swaps the ``= None``
        for ``deleteLater()``, ownership transfers to Qt
        and the C++ object lingers in a deferred-delete
        event, leaking until app exit and triggering
        cross-thread destruction warnings.
        """
        worker = VatsimWorker(wake_db={})
        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            return_value=FetchResult(
                pilots=[], not_modified=False, last_modified=None
            ),
        ):
            worker.start_polling()
            assert worker._timer is not None  # sanity
        worker.stop_polling()
        assert worker._timer is None

    def test_start_after_stop_creates_fresh_timer(
        self, qapp  # noqa: ARG002
    ) -> None:
        """Restart contract: the toolbar toggle off-then-on
        path needs ``start_polling`` after ``stop_polling`` to
        spin up a new timer rather than being a silent no-op
        because the old reference still lurks somewhere.
        """
        worker = VatsimWorker(wake_db={})
        with patch(
            "cvfr_routemaster.vatsim_worker.fetch_vatsim_data",
            return_value=FetchResult(
                pilots=[], not_modified=False, last_modified=None
            ),
        ):
            worker.start_polling()
            first_timer = worker._timer
            worker.stop_polling()
            assert worker._timer is None
            worker.start_polling()
            second_timer = worker._timer
        try:
            assert second_timer is not None
            assert second_timer is not first_timer
            assert worker.is_running is True
        finally:
            worker.stop_polling()


# Process pending events at the end of each test so any cross-test
# Qt event noise (queued metacalls, signal forwarders) clears
# before the next test constructs a new worker. We intentionally
# don't use ``QTimer.deleteLater`` for the worker's own timer
# (see :meth:`VatsimWorker.stop_polling` for why), but other
# parts of the test surface still post events that we want to
# drain to keep the environment tidy.
@pytest.fixture(autouse=True)
def _drain_pending_events(qapp):  # noqa: ARG001
    yield
    QCoreApplication.processEvents()
