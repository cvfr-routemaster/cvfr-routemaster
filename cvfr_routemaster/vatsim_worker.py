"""Background worker that polls the VATSIM v3 datafeed on a fixed
cadence and emits filtered :class:`Pilot` snapshots to the GUI
thread.

Architecture
------------

The worker is a :class:`QObject` designed to be moved onto a
:class:`QThread`. Once on its thread, the GUI starts it via the
thread's ``started`` signal, which invokes :meth:`start_polling`
on the worker's thread; that method creates a :class:`QTimer`
(also on the worker thread, since it's constructed there) and
fires an immediate first fetch. Subsequent fetches happen every
``interval_ms`` ms.

Why a worker thread at all? :func:`fetch_vatsim_data` makes a
blocking ``urllib`` HTTP call that can take >1 s on a slow
network — running it on the GUI thread would freeze the chart.
The QTimer-on-worker-thread pattern keeps every fetch off the GUI
thread and uses Qt's queued-signal machinery to deliver the
results back to the GUI thread without explicit locking.

Signals
-------

* ``pilots_updated(list[Pilot])`` — emitted after every
  successful fetch + bbox filter, including the empty-list case
  ("nobody flying in Israeli airspace right now"). Receivers
  should treat the list as the *full current state*, not a
  delta.

* ``fetch_failed(str)`` — emitted when a fetch raises
  :class:`VatsimFetchError` (network down, JSON parse failure,
  upstream 5xx). The GUI surfaces this in the status bar.
  Polling continues — a transient failure shouldn't tear down
  the worker, the next tick gets a fresh chance.

* ``poll_skipped()`` — emitted when the upstream returns ``304
  Not Modified`` (because we sent ``If-Modified-Since``). No
  pilot list ships with this signal — receivers should keep
  showing whatever they last drew. Useful for showing a "still
  connected, no new data" indicator if the GUI ever wants one.

VATSIM Code-of-Conduct compliance
---------------------------------

Polling cadence defaults to 15 s, matching the upstream's
publish cadence and the Code of Conduct's "don't hammer the
servers" guidance. ``If-Modified-Since`` is sent on every
request after the first so the server can short-circuit with a
304. Custom ``User-Agent`` (configured in
:mod:`cvfr_routemaster.vatsim_feed`) identifies this client
and the maintainer's VATSIM CID per the upstream's contact
requirements.

Israel airspace bbox
--------------------

The default bounding box covers Israeli airspace plus a small
buffer for traffic transitioning from neighbouring FIRs (Cyprus
to the west-northwest, Egypt to the south, Jordan to the east).
Filtering at the worker level cuts the list from ~10 000 global
pilots to a handful before crossing thread boundaries — saves
one signal payload per tick and keeps the GUI thread's
``pilots_updated`` slot trivially fast.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from cvfr_routemaster.vatsim_feed import (
    Pilot,
    VatsimFetchError,
    WakeDB,
    fetch_vatsim_data,
    filter_to_bbox,
)

# --- Israel airspace bounding box ---------------------------------------
# Lat/lon bounds wide enough to cover the calibrated Israeli CVFR chart
# coverage plus a buffer for traffic transitioning from Cyprus, Egypt,
# Jordan, Lebanon, and Syria. Buffer is intentionally generous (~50 nm
# beyond the chart edges) so a plane on approach into LLBG from over
# the Mediterranean doesn't pop into existence at the chart edge — it
# fades in once it's within a quarter-hour of the calibrated area.
#
# Tightening this bbox is cheap (worker-side filter), but loosening it
# means a larger payload crossing the worker→GUI signal boundary on
# every tick, which we'd rather avoid even though the visible-region
# filter on the chart side is the real correctness gate.
ISRAEL_BBOX_MIN_LAT: float = 29.0
ISRAEL_BBOX_MAX_LAT: float = 34.0
ISRAEL_BBOX_MIN_LON: float = 33.5
ISRAEL_BBOX_MAX_LON: float = 36.5

# --- Polling cadence ----------------------------------------------------
# 15 seconds matches the upstream's publish cadence
# (https://data.vatsim.net/v3/vatsim-data.json regenerates roughly every
# 15 s). Going faster wastes our CPU + their bandwidth without yielding
# fresher data; going slower means our planes lag real positions by up
# to 30 s, which is noticeable when watching a fast jet cross a 30 nm
# leg. 15 s is the sweet spot every other VATSIM client uses (vatSpy,
# VATSIM Radar, vPilot's traffic display).
DEFAULT_POLL_INTERVAL_MS: int = 15_000


class VatsimWorker(QObject):
    """Periodic VATSIM datafeed poller. Lives on a worker
    :class:`QThread`; emits filtered pilot snapshots to the GUI
    thread via Qt's queued-signal machinery.

    The class is deliberately stateless beyond the
    ``last_modified`` cache and the timer — every tick is an
    independent fetch + parse + filter pipeline whose only
    persistent side-effect is updating ``last_modified``. That
    keeps the worker safe to restart (just create a new instance)
    and keeps the test surface tiny: feed a mocked
    :func:`fetch_vatsim_data` an arranged response and assert the
    resulting signal payload.

    Construction-vs-start split
    ---------------------------

    The constructor is cheap and runs on whichever thread spawns
    it (typically the GUI thread). It does *not* touch the timer
    — Qt requires the timer to live on the same thread that
    starts it, so the timer is created lazily inside
    :meth:`start_polling`, which the GUI invokes via the
    QThread's ``started`` signal (which fires on the worker
    thread). Without this split, the timer would be parented to
    the GUI thread and fire its ``timeout`` slot back on the GUI
    thread, defeating the whole purpose of the worker.
    """

    pilots_updated = Signal(list)  # list[Pilot]
    fetch_failed = Signal(str)
    poll_skipped = Signal()
    # Emitted on the worker thread after :meth:`stop_polling` has
    # stopped the QTimer and dropped its Python reference. The GUI
    # wires this to ``thread.quit`` with ``DirectConnection`` so the
    # quit-flag is set on the worker thread *after* the timer is
    # already gone — avoiding the race documented in
    # :meth:`stop_polling` ("Why the worker emits ``finished`` itself").
    finished = Signal()

    def __init__(
        self,
        wake_db: WakeDB,
        *,
        min_lat: float = ISRAEL_BBOX_MIN_LAT,
        max_lat: float = ISRAEL_BBOX_MAX_LAT,
        min_lon: float = ISRAEL_BBOX_MIN_LON,
        max_lon: float = ISRAEL_BBOX_MAX_LON,
        interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
    ) -> None:
        super().__init__()
        self._wake_db = wake_db
        self._min_lat = float(min_lat)
        self._max_lat = float(max_lat)
        self._min_lon = float(min_lon)
        self._max_lon = float(max_lon)
        self._interval_ms = int(interval_ms)
        # Cached HTTP "Last-Modified" header from the previous
        # successful fetch; sent back as ``If-Modified-Since`` on
        # the next request so VATSIM can short-circuit with 304
        # when there's nothing new. None on first launch (and
        # after a clean restart) so the very first request is
        # unconditional and always returns a fresh snapshot.
        self._last_modified: str | None = None
        # Timer is created lazily on the worker thread inside
        # ``start_polling`` — see class docstring for rationale.
        self._timer: QTimer | None = None
        # Stop flag for graceful shutdown — set by
        # ``stop_polling`` before the timer is torn down so an
        # in-flight tick that races the stop call returns early
        # rather than emitting a stale signal after teardown.
        self._stopped: bool = False

    # --- Lifecycle slots (called via queued connections) -------

    @Slot()
    def start_polling(self) -> None:
        """Create the :class:`QTimer` on the worker thread and
        kick off an immediate first fetch.

        Idempotent: calling twice is a no-op (the timer's already
        running). Designed to be wired to
        ``QThread.started`` so the GUI thread doesn't have to
        manually marshal the call.
        """
        if self._timer is not None:
            return
        self._stopped = False
        self._timer = QTimer()
        self._timer.setInterval(self._interval_ms)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()
        # Immediate fetch so the user sees data within a couple
        # of seconds of toggling the overlay on, rather than
        # having to wait for the first 15-s tick.
        self._on_tick()

    @Slot()
    def stop_polling(self) -> None:
        """Stop the timer and prevent any further emissions.

        Designed to be invoked via ``QMetaObject.invokeMethod``
        with ``Qt.QueuedConnection`` (or ``BlockingQueuedConnection``)
        so the body actually runs on the worker thread (where the
        timer was created and therefore the only thread that's
        allowed to stop it). Sets ``_stopped`` so an in-flight
        ``_on_tick`` aborts before emitting, avoiding stale signals
        racing the teardown.

        Emits :attr:`finished` at the very end so the GUI's
        ``worker.finished → thread.quit`` DirectConnection wiring
        sets the event-loop quit flag *on the worker thread* —
        the only thread it's safe to set it from at this point in
        the teardown sequence.

        Idempotent — safe to call repeatedly. The second call
        finds ``self._timer is None`` and just re-emits
        :attr:`finished`, which is fine: the wired-up ``thread.quit``
        slot is itself idempotent.

        Why the worker emits ``finished`` itself
        ----------------------------------------

        The previous design had the GUI call ``thread.quit()``
        directly from ``_signal_vatsim_worker_stop`` immediately
        after posting the queued ``stop_polling`` invocation. That
        races: ``thread.quit()`` doesn't queue an event, it sets
        the event loop's exit flag directly. The worker's
        ``QEventLoop::exec`` checks the quit flag at the top of
        every iteration and exits *without dispatching the pending
        ``MetaCallEvent``* — meaning ``stop_polling`` never runs.
        The QTimer survives on the now-dead thread, and Qt warns
        ``QObject::killTimer: Timers cannot be stopped from
        another thread`` + ``QObject::~QObject: Timers cannot be
        stopped from another thread`` once each at QApplication
        teardown.

        Having the worker emit :attr:`finished` (and the GUI wire
        it to ``thread.quit`` via ``DirectConnection``) closes the
        race: the quit flag is set only after ``stop_polling`` has
        finished running on the worker thread, with the timer
        already torn down. No event ordering involved.

        Why no ``deleteLater`` on the timer
        -----------------------------------

        It would seem natural to ``self._timer.deleteLater()``
        here for symmetry with the lazy ``QTimer()`` creation
        in :meth:`start_polling`, but ``deleteLater`` posts a
        ``DeferredDelete`` event into the worker thread's queue.
        Once we've emitted :attr:`finished` and the wired
        ``thread.quit`` slot has run, the event loop exits and any
        pending ``DeferredDelete`` events are stranded — same
        failure mode as the historical bug above.

        The fix: drop the Python reference instead. The
        timer is freestanding (no parent), so PySide6's
        Python wrapper owns its lifetime; setting
        ``self._timer = None`` decrements the wrapper's
        refcount to zero and synchronously destructs the
        C++ ``QTimer`` on the current (worker) thread,
        which is also its affinity thread — exactly where
        Qt requires it.
        """
        self._stopped = True
        if self._timer is not None:
            self._timer.stop()
            # Synchronous teardown on the worker thread via
            # Python refcount drop — see the docstring's
            # "Why no deleteLater" section for the rationale.
            self._timer = None
        # Final step: notify the GUI so the wired
        # ``thread.quit`` DirectConnection fires *here* on the
        # worker thread, after the timer is already gone.
        self.finished.emit()

    # --- Tick handler -----------------------------------------

    @Slot()
    def _on_tick(self) -> None:
        """One poll cycle: fetch, parse, bbox-filter, emit.

        Errors are caught and emitted as ``fetch_failed`` rather
        than allowed to propagate — a Python exception in a Qt
        slot crashes the event loop, and we'd rather show a
        status-bar error and try again on the next tick.

        The 304-not-modified path emits ``poll_skipped`` (no
        pilot list) so the GUI can keep showing whatever it
        last drew. Receivers MUST handle ``pilots_updated``
        with an empty list as well — that's the legitimate
        "nobody flying" case, distinct from "we got a 304".
        """
        if self._stopped:
            return
        try:
            result = fetch_vatsim_data(
                self._wake_db,
                last_modified=self._last_modified,
            )
        except VatsimFetchError as exc:
            self.fetch_failed.emit(str(exc))
            return
        # Re-check stopped *after* the blocking fetch so a stop
        # call that races a long HTTP round-trip doesn't trigger
        # a stale emit.
        if self._stopped:
            return
        if result.not_modified:
            self.poll_skipped.emit()
            return
        if result.last_modified:
            self._last_modified = result.last_modified
        pilots = filter_to_bbox(
            result.pilots,
            min_lat=self._min_lat,
            max_lat=self._max_lat,
            min_lon=self._min_lon,
            max_lon=self._max_lon,
        )
        self.pilots_updated.emit(pilots)

    # --- Test helpers -----------------------------------------

    @property
    def last_modified(self) -> str | None:
        """Read-only view of the cached If-Modified-Since header.

        Exposed for tests that want to assert the worker is
        actually persisting the header across ticks; not used
        from the GUI side.
        """
        return self._last_modified

    @property
    def is_running(self) -> bool:
        """``True`` between :meth:`start_polling` and
        :meth:`stop_polling`. Tests use this to assert the
        teardown path actually stopped the timer.
        """
        return self._timer is not None and not self._stopped


__all__ = [
    "DEFAULT_POLL_INTERVAL_MS",
    "ISRAEL_BBOX_MAX_LAT",
    "ISRAEL_BBOX_MAX_LON",
    "ISRAEL_BBOX_MIN_LAT",
    "ISRAEL_BBOX_MIN_LON",
    "VatsimWorker",
]


# Re-exports purely for test convenience — keeps importers from
# having to know that ``Pilot`` lives in vatsim_feed when they're
# already importing the worker.
_ = Pilot
