"""On-demand satellite tile fetcher.

Companion to the per-tile :class:`SatelliteOverlay`: when the
overlay's lazy-load discovers that a *visible* tile is missing from
the disk cache, this worker fetches it in the background and emits
:sig:`tile_ready` once the bytes are written. The overlay then picks
the tile up via ``refresh_from_cache(only_coords=[coord])`` and the
"Loading Tile…" placeholder swaps to the real imagery.

Why a separate worker (vs. the existing bulk-fetch worker)
----------------------------------------------------------

The bulk-fetch worker walks the entire Israel bbox in a fixed
geographic order at startup-time. It can't be re-tasked to "fetch
this specific tile right now" without inventing a complex priority
queue + back-off coordination — and even then its mental model is
"download all of Israel", not "respond to user pans". Splitting them
keeps each worker simple:

* :class:`SatelliteWorker` (bulk): static plan, runs to completion,
  resumable across sessions.
* :class:`OnDemandFetchWorker` (this module): dynamic queue,
  best-effort, GUI-driven.

They share the disk cache (atomic file writes — no race) and a
similar throttle, but they don't otherwise coordinate. If both run
concurrently, total request rate is at most
``2 × throttle⁻¹`` ≈ 10 req/s — well under Esri's published limit.

Queue semantics
---------------

* **Bounded** at :data:`DEFAULT_MAX_PENDING` (200 coords). User
  panning rapidly enqueues many tiles; once full, *oldest* entries
  drop. This is the right policy because the user has moved past
  the now-evicted tiles by the time the queue saturates — fetching
  them would just decode-and-evict from the overlay's LRU.
* **De-duplicated** via a parallel ``set`` of in-flight + queued
  coords. Re-enqueuing a coord that's already pending is a no-op.
* **FIFO** within the cap. Pure list-pop-from-front; no priority
  ordering. The visibility-driven nature of the call site (the user
  is looking at this tile *now*) means anything in the queue is
  current; explicit priority would just complicate the worker
  without changing user-visible behaviour.

Threading model
---------------

Mirrors :class:`SatelliteWorker`'s pattern: ``QObject`` lives on a
dedicated ``QThread``, work is driven by chained
``QTimer.singleShot(throttle_ms)`` calls so the thread's event
loop stays responsive (queued ``enqueue`` and ``request_stop``
calls land between fetches), and each fetch is a single
``urllib.request`` round-trip via :func:`fetch_and_cache_tile`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from cvfr_routemaster.satellite_fetch import fetch_and_cache_tile
from cvfr_routemaster.satellite_tiles import TileCoord

if TYPE_CHECKING:
    from cvfr_routemaster.satellite_tiles import TileCache


_LOG = logging.getLogger(__name__)


#: Cap on the in-flight + queued coord count. Past this, oldest
#: entries drop on every new enqueue. Sized to comfortably hold a
#: zoom-out viewport's worth of misses without dropping: at the
#: coarsest configured zoom (z=12 over the Israel bbox) ~1300
#: tiles can be visible simultaneously, and a single pan of an
#: empty cache enqueues all of them. The previous 200-tile cap
#: silently dropped 1100 of those, so most of the chart never
#: got fetched — the user saw a thrash of placeholders that
#: didn't fill in even after minutes of waiting. 1000 lets one
#: full-chart pan land without losses while still bounding the
#: per-fetch RAM footprint of the queue itself (negligible —
#: each entry is a 12-byte tuple wrapped in a dataclass).
DEFAULT_MAX_PENDING: int = 1000

#: Wall-clock minimum interval between successive fetches, in ms.
#: Was 200 ms (5 req/s) to be polite to Esri; that turned out to
#: be far below what Esri actually allows and far below what the
#: zoomed-out cache-miss case needs to be usable. Esri's published
#: rate ceiling is hundreds of req/s per IP, with 429 backoff
#: kicking in only if you blow past it; 50 ms (20 req/s) is well
#: within that envelope and brings a worst-case full-chart z=12
#: backfill (~1300 tiles) from ~4-5 minutes down to ~65 s. The
#: bulk-fetch worker doesn't share this throttle (it just fires
#: one request after another at the natural HTTP cadence), so
#: combined steady-state when both workers run concurrently is
#: ~30 req/s, comfortably under Esri's threshold and well-handled
#: by the existing 429-backoff path if it ever does trip.
DEFAULT_THROTTLE_MS: int = 50

#: Per-fetch HTTP timeout. Keep tighter than the bulk worker's
#: default — on-demand requests should fail fast and let the
#: overlay's "Loading Tile…" placeholder remain rather than
#: blocking the queue behind one slow tile.
DEFAULT_TIMEOUT_S: float = 8.0


class OnDemandFetchWorker(QObject):
    """Fetches individual satellite tiles in response to GUI requests.

    Signals
    -------
    tile_ready(object):
        Emitted with the :class:`TileCoord` of a successfully
        fetched + cached tile. The argument is typed ``object``
        rather than ``TileCoord`` because Qt's signal-type system
        doesn't auto-register dataclasses; the GUI slot does an
        ``isinstance`` check at the boundary.
    tile_failed(object, str):
        Emitted when a fetch attempt produces an unrecoverable
        error (malformed URL, 4xx other than 404, irrecoverable
        network issue). 404s and rate-limit 429s are *not*
        surfaced this way — they're either retried (429) or
        silently treated as "tile doesn't exist" (404).

    Slots
    -----
    enqueue(object):
        Append a coord to the pending queue. Idempotent on
        already-enqueued / in-flight coords. Safe to call from
        any thread; Qt routes the call through the worker's
        event loop via the standard slot-invocation machinery.
    request_stop():
        Polite shutdown — no new fetches will start, but a
        currently-running fetch finishes its HTTP round-trip
        before the thread exits.
    """

    tile_ready = Signal(object)
    tile_failed = Signal(object, str)
    finished = Signal()

    def __init__(
        self,
        *,
        tile_cache: "TileCache",
        url_template: str,
        user_agent: str,
        max_pending: int = DEFAULT_MAX_PENDING,
        throttle_ms: int = DEFAULT_THROTTLE_MS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        super().__init__()
        self._cache = tile_cache
        self._url_template = url_template
        self._user_agent = user_agent
        self._max_pending = int(max_pending)
        self._throttle_ms = int(throttle_ms)
        self._timeout_s = float(timeout_s)
        # Pending FIFO + dedup set. The lock guards both — the
        # GUI thread mutates them via ``enqueue`` (queued slot
        # invocation lands the call here on the worker's thread,
        # but a vanilla Python list/set still needs a lock against
        # the worker's own ``_process_next`` reads). In practice
        # both happen on the worker thread once the
        # QueuedConnection routes them, so the lock is mostly
        # belt-and-braces against future direct-call refactors.
        self._lock = threading.Lock()
        self._pending: list[TileCoord] = []
        self._enqueued: set[TileCoord] = set()
        # ``_processing`` is True between the moment we fire the
        # first ``QTimer.singleShot(_process_next)`` for a non-
        # empty queue and the moment ``_process_next`` discovers
        # the queue is empty and gives up the loop. Without the
        # flag, every ``enqueue`` would schedule another
        # ``_process_next`` and we'd run the queue at full clip
        # ignoring the throttle.
        self._processing = False
        self._stopped = False
        # Set when ``request_stop`` is called and the next fetch
        # boundary should exit cleanly. Read on every iteration.
        # Idempotency guard: ``finished`` should be emitted at most
        # once per worker lifetime. Without this, every chained
        # ``_process_next`` that runs after ``request_stop`` would
        # also emit (the chain doesn't immediately stop — it sees
        # the flag at its next turn). One emission, one teardown.
        self._finished_emitted = False

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @Slot(object)
    def enqueue(self, coord: object) -> None:
        """Append ``coord`` to the fetch queue.

        Drops the oldest entry when the cap is reached — see the
        module docstring for why FIFO-with-drop-oldest is the
        right policy.

        Silently ignores non-:class:`TileCoord` arguments (e.g. a
        Qt signal accidentally wired to this slot with a different
        payload). A bad enqueue should never crash the worker; the
        worst case is the user notices a tile didn't fetch.
        """
        if not isinstance(coord, TileCoord):
            return
        if self._stopped:
            return
        with self._lock:
            if coord in self._enqueued:
                return
            if len(self._pending) >= self._max_pending:
                dropped = self._pending.pop(0)
                self._enqueued.discard(dropped)
            self._pending.append(coord)
            self._enqueued.add(coord)
            need_kick = not self._processing
            if need_kick:
                self._processing = True
        if need_kick:
            # Run on the worker's event loop. ``QTimer.singleShot``
            # is the right cross-thread post mechanism: the timer
            # is owned by ``self`` (which is moveToThread'd to the
            # worker thread), so the timeout fires on the worker
            # thread's loop.
            QTimer.singleShot(0, self._process_next)

    @Slot()
    def request_stop(self) -> None:
        """Polite shutdown.

        Sets the stop flag (read by ``_process_next`` at every
        iteration) AND emits ``finished`` immediately so any
        wired-up shutdown chain (``finished`` → ``thread.quit`` /
        ``deleteLater``) runs without waiting for the next
        scheduled timer to fire.

        The immediate emit matters for the *idle* worker case: a
        worker with an empty queue and no pending timer would
        never run ``_process_next`` again, so the only source of
        a ``finished`` emission would be the next ``enqueue`` —
        which never comes once the GUI is shutting down. Without
        this immediate emit, the GUI's ``thread.wait()`` would
        hit its full timeout on every clean exit.

        Idempotent: subsequent calls observe ``_finished_emitted``
        and skip the re-emit. The worker's main loop also checks
        ``_finished_emitted`` so a pending timer firing after
        ``request_stop`` doesn't re-emit either.
        """
        self._stopped = True
        if not self._finished_emitted:
            self._finished_emitted = True
            self.finished.emit()

    @Slot(object)
    def cancel_pending(self, coord: object) -> None:
        """Drop ``coord`` from the queue if it's still pending.

        Called by the GUI when a tile that we requested earlier
        becomes invisible (e.g. user panned far away) — saves a
        wasted fetch on a tile we'll never display. No-op if the
        coord is currently being fetched.
        """
        if not isinstance(coord, TileCoord):
            return
        with self._lock:
            if coord not in self._enqueued:
                return
            try:
                self._pending.remove(coord)
            except ValueError:
                # Already in flight — nothing to cancel; let it
                # finish (cheap to throw away the result if the
                # coord is no longer needed by the time it lands).
                return
            self._enqueued.discard(coord)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _process_next(self) -> None:
        """Dequeue + fetch one tile, then schedule the next.

        On success: emit ``tile_ready(coord)`` and chain another
        ``_process_next`` after :attr:`_throttle_ms`.

        On failure: emit ``tile_failed(coord, msg)`` and chain.
        """
        if self._stopped:
            self._processing = False
            if not self._finished_emitted:
                self._finished_emitted = True
                self.finished.emit()
            return
        with self._lock:
            if not self._pending:
                self._processing = False
                return
            coord = self._pending.pop(0)
            # Don't discard from ``_enqueued`` yet — keep it there
            # until the fetch completes so a parallel enqueue of
            # the same coord still dedups.
        outcome = None
        try:
            outcome = fetch_and_cache_tile(
                coord,
                self._cache,
                template=self._url_template,
                user_agent=self._user_agent,
                timeout=self._timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            # ``fetch_and_cache_tile`` maps the common error cases
            # to ``FetchOutcome``; reaching this branch means an
            # unexpected exception type (out-of-disk on the
            # cache.put, etc.). Log, surface, carry on.
            _LOG.warning(
                "On-demand fetch raised for %s: %s", coord, exc
            )
            self.tile_failed.emit(coord, str(exc))
        finally:
            with self._lock:
                self._enqueued.discard(coord)

        if outcome is not None:
            # ``FetchOutcome.kind`` is a discriminator string —
            # see :class:`satellite_fetch.FetchOutcome`. Five
            # disjoint shapes, each with its own handling.
            kind = outcome.kind
            if kind == "fetched":
                # Fresh tile, bytes already in cache.
                self.tile_ready.emit(coord)
            elif kind == "rate_limited":
                # Honour Retry-After. ``time.sleep`` blocks the
                # worker thread but the GUI is unaffected; no
                # other on-demand work makes progress until the
                # server is willing to talk to us again, which
                # is exactly the right behaviour.
                wait_s = float(outcome.retry_after_seconds or 0.0)
                if wait_s > 0:
                    time.sleep(min(wait_s, 30.0))
                # Re-enqueue at the front so the ratelimited
                # tile is the next thing we try (preferable to
                # appending — the user is likely still looking
                # at it). Skip the re-enqueue if a parallel
                # ``cancel_pending`` already removed the coord
                # from the consumer's interest set.
                with self._lock:
                    if coord not in self._enqueued:
                        self._pending.insert(0, coord)
                        self._enqueued.add(coord)
            elif kind == "missing":
                # Tile genuinely doesn't exist at this address
                # (over-ocean, outside provider coverage). The
                # placeholder stays up; that's fine UX — there's
                # nothing to load. Logged at DEBUG only.
                _LOG.debug("On-demand 404 for %s", coord)
            elif kind == "network_error" or kind == "http_error":
                self.tile_failed.emit(
                    coord,
                    f"{kind}: {outcome.reason or 'unknown'}",
                )
            else:
                self.tile_failed.emit(
                    coord, f"unexpected outcome kind: {kind}"
                )

        # Schedule next, even if we just stopped — the next call
        # discovers ``_stopped`` and emits ``finished``.
        QTimer.singleShot(self._throttle_ms, self._process_next)

    # ------------------------------------------------------------------
    # Inspection (mostly for tests)
    # ------------------------------------------------------------------

    def pending_count(self) -> int:
        """Snapshot of the queue depth — for status-bar / tests."""
        with self._lock:
            return len(self._pending)

    def is_enqueued(self, coord: TileCoord) -> bool:
        """Whether ``coord`` is currently in the queue or in flight."""
        with self._lock:
            return coord in self._enqueued
