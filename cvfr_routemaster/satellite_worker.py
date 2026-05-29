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

"""Bulk-fetch worker for the v3 satellite-imagery feature.

Lives on a :class:`QThread`; drives a serial walk over every
satellite tile that covers Israel at the configured zoom, writes
each to the on-disk :class:`TileCache`, and emits progress signals
that the GUI plumbs into the status bar.

Why a worker thread
-------------------

The bulk fetch is ~19 000 tiles at the default ``z=14`` (≈330 MB,
≈9 min on a typical residential connection); doing this on the GUI
thread would freeze the chart for the entire download. Each tile
is one blocking HTTP GET in :func:`satellite_fetch.fetch_tile`, so
the cumulative wall time is sensitive to per-request latency, not
CPU. A worker thread + Qt's queued signals delivers the progress
ticks back to the GUI without locking.

Why chained ``QTimer.singleShot(0)`` instead of a tight loop
------------------------------------------------------------

Two reasons:

  1. **Stop responsiveness.** A naïve ``while idx < total: fetch()``
     loop blocks the worker's event loop for the duration of the
     download. Any ``stop_fetch`` call queued from the GUI sits in
     the queue until the loop exits — i.e. forever. Chaining
     ``QTimer.singleShot(0, _fetch_next_tile)`` returns to the
     event loop between tiles, so a queued ``stop_fetch`` lands
     immediately after the in-flight tile completes.
  2. **Crash-safe persistence.** The :class:`DownloadState` JSON
     is written every :data:`STATE_PERSIST_EVERY_N_TILES` tiles so
     a crash mid-download loses at most that many tiles' worth of
     progress on resume. With a tight loop we'd have to pepper the
     loop body with periodic flushes; with the chain it's a single
     guard at the top of ``_fetch_next_tile``.

The downside is one extra Python event-loop hop per tile (~20 µs).
Negligible compared to a ~100 ms HTTP round-trip.

Resume semantics
----------------

On :meth:`start_fetch` the worker reads the existing download state
(if any) via :func:`satellite_fetch.read_download_state`. If the
state's ``(bbox, zoom)`` matches the current run's parameters, we
treat it as a resume: ``completed_tiles`` is preserved as the
running base, and the to-fetch list is built by
:func:`satellite_fetch.tiles_to_fetch_for_bbox` which subtracts
already-cached tiles. If the state mismatches (different zoom,
different bbox, different provider), it's stale and gets
overwritten on the first persist.

Signals
-------

* ``progress(completed, total)`` — emitted every
  :data:`PROGRESS_EMIT_EVERY_N_TILES` tile fetches AND on the very
  first / very last tile. Both ints. Sub-emit cadence is the right
  trade-off between status-bar refresh smoothness and queued-signal
  overhead.
* ``tile_fetched(TileCoord)`` — emitted after every successful
  fetch (not on missing / rate-limited / network-error tiles). The
  GUI can use this to incrementally re-render the satellite view
  if the fetched tile lies within the current viewport.
* ``finished()`` — emitted when the fetch list is exhausted (all
  tiles either fetched, missing, or permanently errored). The GUI
  side typically follows with a ``QThread.quit()``.
* ``failed(str)`` — emitted only on a hard error that aborts the
  whole bulk fetch (cache I/O failure, e.g. disk full). Transient
  per-tile errors are logged to debug output and the walk continues.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from cvfr_routemaster.satellite_fetch import (
    DEFAULT_RATE_LIMIT_BACKOFF_S,
    DEFAULT_TIMEOUT_S,
    DownloadState,
    count_cached_tiles_in_bbox,
    fetch_and_cache_tile,
    read_download_state,
    tiles_to_fetch_for_bbox,
    write_download_state,
)
from cvfr_routemaster.satellite_tiles import (
    DEFAULT_TARGET_ZOOM,
    ESRI_WORLD_IMAGERY_TEMPLATE,
    ISRAEL_BBOX,
    TileCache,
    TileCoord,
    count_tiles_for_bbox,
)

# Persist DownloadState every N tiles. Smaller = less progress lost
# on a crash but more disk I/O. 100 strikes the balance: at our
# ~100 ms per-tile median that's a flush every ~10 s of wall time,
# matching the "you won't lose more than a few seconds of work"
# user expectation.
STATE_PERSIST_EVERY_N_TILES: int = 100

# Progress signal cadence — also dictated by the same trade-off,
# but tuned for status-bar smoothness rather than disk durability.
# 25 tiles ≈ 2.5 s @ 100 ms each, which lets the user see the
# percentage move while still keeping the queued-signal traffic
# very low (~4 emits/s peak).
PROGRESS_EMIT_EVERY_N_TILES: int = 25

# Maximum number of consecutive transient failures
# (network_error / rate_limited) before the walk gives up on a
# tile and moves on to the next. Caps the "internet went down
# mid-download" damage to a bounded delay rather than letting one
# bad tile freeze the whole bulk fetch.
MAX_TRANSIENT_RETRIES_PER_TILE: int = 3


class SatelliteWorker(QObject):
    """Bulk-fetch the satellite tile mosaic for the configured bbox.

    Construction-vs-start split mirrors :class:`VatsimWorker`: the
    constructor is cheap and runs on whichever thread spawns the
    worker (typically the GUI thread); the actual planning and
    fetching happens inside :meth:`start_fetch`, which is intended
    to run on the worker thread (wired to ``QThread.started``).

    The worker is single-shot: once it emits ``finished`` or
    ``failed``, it should be discarded. Re-runs are achieved by
    constructing a fresh instance — keeps the lifecycle simple and
    avoids stuck "should I reset state?" branches.
    """

    progress = Signal(int, int)
    tile_fetched = Signal(object)  # TileCoord
    finished = Signal()
    failed = Signal(str)

    def __init__(
        self,
        cache: TileCache,
        *,
        bbox: tuple[float, float, float, float] = ISRAEL_BBOX,
        zoom: int = DEFAULT_TARGET_ZOOM,
        url_template: str = ESRI_WORLD_IMAGERY_TEMPLATE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        rate_limit_backoff_s: float = DEFAULT_RATE_LIMIT_BACKOFF_S,
        sleep_fn: "object | None" = None,
        persist_state: bool = True,
    ) -> None:
        super().__init__()
        self._cache = cache
        self._bbox = bbox
        self._zoom = int(zoom)
        self._url_template = url_template
        self._timeout_s = float(timeout_s)
        self._rate_limit_backoff_s = float(rate_limit_backoff_s)
        # Test seam: tests inject a no-op sleep so rate-limit
        # backoff doesn't extend a unit-test by 30 s.
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep
        # When False, ``_write_state`` is a no-op. The multi-zoom
        # bulk-fetch chain uses this for the *secondary* zooms
        # (typically z=13 and z=12) — we don't want them to
        # overwrite the primary zoom's resume state, and they're
        # small enough (~10 k tiles total) that re-enumerating
        # the missing set on every launch is cheap. The primary
        # zoom (highest configured, default 14) keeps
        # ``persist_state=True`` so users can interrupt and
        # resume the big download.
        self._persist_state = bool(persist_state)

        self._to_fetch: list[TileCoord] = []
        self._fetch_idx: int = 0
        self._total: int = 0
        self._completed_base: int = 0  # carried over from prior session
        self._completed_this_session: int = 0
        self._missing_count: int = 0
        self._error_count: int = 0
        self._consecutive_transient_failures_for_idx: int = 0
        self._stopped: bool = False
        self._started: bool = False

    # --- Lifecycle slots --------------------------------------------------

    @Slot()
    def start_fetch(self) -> None:
        """Plan the fetch list and kick off the chained walk.

        Idempotent on the worker thread side: a second call after
        the first has begun is a no-op. Wired to
        :class:`QThread`'s ``started`` signal in the standard
        worker pattern.
        """
        if self._started:
            return
        self._started = True
        self._stopped = False

        try:
            min_lat, max_lat, min_lon, max_lon = self._bbox
            self._total = count_tiles_for_bbox(
                min_lat=min_lat,
                max_lat=max_lat,
                min_lon=min_lon,
                max_lon=max_lon,
                z=self._zoom,
            )
            existing = read_download_state(self._cache)
            if (
                existing is not None
                and existing.zoom == self._zoom
                and existing.bbox == tuple(self._bbox)
            ):
                # Primary-zoom resume path: trust the persisted
                # ``completed_tiles`` counter from a prior session.
                # The state file is only written by the primary
                # (final/highest) link in the chain, so this branch
                # almost always fires for the primary zoom. Any
                # tiles that *should* be on disk but aren't (e.g.
                # the cache directory was partially deleted) get
                # re-enumerated by ``tiles_to_fetch_for_bbox``.
                self._completed_base = int(existing.completed_tiles)
            else:
                # No matching state file for this (zoom, bbox).
                # Two sub-cases:
                #
                # 1. Fresh start with no prior progress at this zoom
                #    — ``_completed_base`` should be 0.
                # 2. Mid-chain secondary zoom on a resumed bulk
                #    fetch — the bulk-fetch chain (see
                #    :func:`_plan_satellite_zoom_chain`) deliberately
                #    does NOT persist state for non-primary zooms
                #    because the state file only carries one
                #    ``(zoom, bbox)`` and the primary owns it. So
                #    on every cold launch, secondary zooms walk in
                #    with ``existing.zoom != self._zoom`` and
                #    ``_completed_base`` would default to 0 — even
                #    if 9,800 of the 10,000 tiles at this zoom are
                #    already on disk. The progress bar would then
                #    read ``0 / 10000`` at start, jump to
                #    ``200 / 10000`` (2 %) as the small missing set
                #    is filled, and complete; the user perceives
                #    "two-percent download" instead of "almost done".
                #
                # Distinguish the two by asking the filesystem: how
                # many tiles in this bbox at this zoom are already
                # cached? We use
                # :func:`count_cached_tiles_in_bbox` rather than
                # ``len(tiles_to_fetch_for_bbox(...))`` because the
                # former is O(directories) and the latter is
                # O(tiles) — same correctness, ~100-400x faster on
                # Windows NTFS. At z=13 over Israel the scandir-
                # based walk is well under a second, on the worker
                # thread so the GUI never stalls. We attribute the
                # already-cached count to ``_completed_base`` so
                # progress reads ``9800 / 10000`` at start and
                # smoothly fills the remaining 2 % — matching the
                # user's mental model that "interrupting and
                # restarting picks up where I left off".
                #
                # If the scan fails (permissions, race with a
                # concurrent prune) we fall back to 0 rather than
                # raising — progress display is best-effort, the
                # downloaded-set check downstream is authoritative.
                min_lat, max_lat, min_lon, max_lon = self._bbox
                try:
                    already_cached = count_cached_tiles_in_bbox(
                        min_lat=min_lat,
                        max_lat=max_lat,
                        min_lon=min_lon,
                        max_lon=max_lon,
                        z=self._zoom,
                        cache=self._cache,
                    )
                except Exception:  # noqa: BLE001 — best-effort seed.
                    already_cached = 0
                self._completed_base = int(already_cached)

            self._to_fetch = tiles_to_fetch_for_bbox(
                cache=self._cache,
                min_lat=min_lat,
                max_lat=max_lat,
                min_lon=min_lon,
                max_lon=max_lon,
                z=self._zoom,
            )
            self._fetch_idx = 0
            self._completed_this_session = 0

            # Persist the (possibly fresh) state at session start
            # so a crash before the first persist tick still leaves
            # a sensible state file behind.
            self._write_state(decision="downloading")

            if not self._to_fetch:
                # Already-complete cache: short-circuit straight to
                # ``finished``. Emit (total, total) — not the
                # default initial ``(completed_base, total)`` — so
                # the GUI's status-bar readout goes directly to
                # the ``z=N ✓ N tiles`` display without flickering
                # through ``z=N 0 / N (0 %)`` first. The flicker
                # was masked while ``_seed_satellite_progress_per_zoom``
                # pre-populated the entry on the GUI side; with
                # that seeding skipped (it was a multi-second
                # GUI-thread freeze on startup), the worker's
                # progress emits are the only signal the GUI gets,
                # so we make them honest. ``_completed_base`` is
                # only authoritative for the *primary* zoom; for
                # secondaries it stays 0 even when every tile is
                # cached, which makes ``_effective_completed`` an
                # unreliable initial-progress source.
                self._write_state(decision="complete", completed=True)
                self.progress.emit(self._total, self._total)
                self.finished.emit()
                return

            # Emit an initial progress tick so the status bar shows
            # 0 / total (or N / total on resume) the moment the
            # download begins, not only after the first persist.
            initial_completed = self._effective_completed()
            self.progress.emit(initial_completed, self._total)

            QTimer.singleShot(0, self._fetch_next_tile)
        except Exception as exc:  # noqa: BLE001 — last-resort guard.
            # An exception in start_fetch is unrecoverable for this
            # session — we can't have planned the walk. Emit failed
            # with a human-readable summary and let the GUI tear
            # the worker down.
            self.failed.emit(f"satellite fetch planning failed: {exc}")

    @Slot()
    def stop_fetch(self) -> None:
        """Set the stop flag; the chained walk self-terminates at
        the next tile boundary.

        Designed to be invoked via ``QMetaObject.invokeMethod``
        with ``Qt.QueuedConnection`` from the GUI thread — sets
        the flag and is otherwise a no-op. Persistence of the
        current progress is performed by ``_fetch_next_tile``
        when it sees the flag.

        Idempotent — safe to call repeatedly.
        """
        self._stopped = True

    # --- Tile walk --------------------------------------------------------

    @Slot()
    def _fetch_next_tile(self) -> None:
        """One tile per call; reschedules itself until the list is
        exhausted or a stop has been requested.

        The body is structured as a sequence of decision points
        (stop? next index? fetch? persist? emit? schedule?) rather
        than nested control flow so each branch is independently
        readable and the persistence + progress logic is in one
        place per outcome.
        """
        if self._stopped:
            self._write_state(decision="paused")
            self.finished.emit()
            return

        if self._fetch_idx >= len(self._to_fetch):
            # Ran out of tiles — the walk is done.
            self._write_state(decision="complete", completed=True)
            # Final progress tick so the GUI reads 100 % on completion
            # even if the last batch didn't trip the periodic emit.
            self.progress.emit(self._effective_completed(), self._total)
            self.finished.emit()
            return

        coord = self._to_fetch[self._fetch_idx]
        try:
            outcome = fetch_and_cache_tile(
                coord=coord,
                cache=self._cache,
                template=self._url_template,
                timeout=self._timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — last-resort guard.
            # I/O failure (disk full, permission denied) — the
            # whole bulk fetch is now compromised. Emit failed and
            # bail.
            self.failed.emit(f"satellite cache write failed: {exc}")
            return

        kind = outcome.kind
        advance = True
        if kind == "fetched":
            self._completed_this_session += 1
            self._consecutive_transient_failures_for_idx = 0
            self.tile_fetched.emit(coord)
        elif kind == "missing":
            self._missing_count += 1
            self._consecutive_transient_failures_for_idx = 0
        elif kind == "rate_limited":
            # Sleep for the upstream-suggested backoff (or the
            # configured default), then retry the same tile. Cap
            # the retry count so a permanently-blocked endpoint
            # doesn't lock the walk forever.
            self._consecutive_transient_failures_for_idx += 1
            if (
                self._consecutive_transient_failures_for_idx
                >= MAX_TRANSIENT_RETRIES_PER_TILE
            ):
                self._error_count += 1
                self._consecutive_transient_failures_for_idx = 0
            else:
                advance = False
                backoff = (
                    outcome.retry_after_seconds
                    if outcome.retry_after_seconds is not None
                    else self._rate_limit_backoff_s
                )
                self._sleep(float(backoff))
        elif kind == "network_error":
            self._consecutive_transient_failures_for_idx += 1
            if (
                self._consecutive_transient_failures_for_idx
                >= MAX_TRANSIENT_RETRIES_PER_TILE
            ):
                self._error_count += 1
                self._consecutive_transient_failures_for_idx = 0
            else:
                advance = False
                self._sleep(min(self._rate_limit_backoff_s, 5.0))
        elif kind == "http_error":
            # Probably a config bug (wrong template / dead provider).
            # Move on — we'd rather render the rest of the cache
            # than freeze on a uniformly-broken URL.
            self._error_count += 1
            self._consecutive_transient_failures_for_idx = 0
        else:
            # Unknown outcome kind — should be impossible per the
            # FETCH_OUTCOME_KINDS contract, but fail closed.
            self._error_count += 1
            self._consecutive_transient_failures_for_idx = 0

        if advance:
            self._fetch_idx += 1

        # Periodic progress + persistence. We tick the emit-counter
        # off the *advance count* not the absolute index so a tile
        # that retried 3 times still only generates one progress
        # emit when it finally moves on.
        if (
            advance
            and self._fetch_idx > 0
            and self._fetch_idx % PROGRESS_EMIT_EVERY_N_TILES == 0
        ):
            self.progress.emit(self._effective_completed(), self._total)
        if (
            advance
            and self._fetch_idx > 0
            and self._fetch_idx % STATE_PERSIST_EVERY_N_TILES == 0
        ):
            self._write_state(decision="downloading")

        # Schedule the next iteration. Returning to the event loop
        # between tiles is what makes ``stop_fetch`` responsive.
        QTimer.singleShot(0, self._fetch_next_tile)

    # --- Helpers ----------------------------------------------------------

    def _effective_completed(self) -> int:
        """Total tiles completed across this and any prior session.

        Capped at ``self._total`` so an off-by-one in the resume
        base never drives the progress bar past 100 %.
        """
        n = self._completed_base + self._completed_this_session
        return min(n, self._total)

    def _write_state(
        self,
        *,
        decision: str = "downloading",
        completed: bool = False,
    ) -> None:
        """Persist the current bulk-fetch progress to the cache's
        download-state JSON.

        Wraps :func:`write_download_state` with a try / log / continue
        because state-file I/O is best-effort: losing it means the
        worst case is the next launch re-enumerates from scratch
        (still finds cached tiles via filesystem, just walks the
        bbox once to subtract them). We don't want a transient
        write error to take the whole bulk fetch down.
        """
        if not self._persist_state:
            # Secondary-zoom workers in the multi-zoom chain
            # (e.g. z=13 / z=12 after the primary z=14 is done)
            # share the cache directory but not the state file —
            # writing here would clobber the primary zoom's
            # resume state. Their progress is tracked transiently
            # via ``progress`` signal emissions only.
            return
        try:
            now = self._now_iso()
            existing = read_download_state(self._cache)
            started_at = (
                existing.started_at
                if existing
                and existing.zoom == self._zoom
                and existing.bbox == tuple(self._bbox)
                and existing.started_at
                else now
            )
            state = DownloadState(
                zoom=self._zoom,
                bbox=tuple(self._bbox),
                total_tiles=self._total,
                completed_tiles=self._effective_completed(),
                user_decision=decision,
                started_at=started_at,
                completed_at=now if completed else None,
            )
            write_download_state(state, self._cache)
        except Exception:  # noqa: BLE001 — best-effort persistence.
            # Non-fatal; the next periodic tick gets another go.
            return

    @staticmethod
    def _now_iso() -> str:
        """Wall-clock ISO-8601 timestamp; broken out as a method so
        tests can patch a deterministic clock without monkey-patching
        the ``datetime`` module globally.
        """
        from datetime import datetime, timezone  # noqa: PLC0415

        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # --- Read-only test introspection -------------------------------------

    @property
    def total(self) -> int:
        """Total tiles in the planned bbox (constant after start_fetch)."""
        return self._total

    @property
    def completed(self) -> int:
        """Combined completed-tile count (this + prior sessions)."""
        return self._effective_completed()

    @property
    def is_running(self) -> bool:
        """True between :meth:`start_fetch` and stop / finish."""
        return self._started and not self._stopped


__all__ = [
    "MAX_TRANSIENT_RETRIES_PER_TILE",
    "PROGRESS_EMIT_EVERY_N_TILES",
    "STATE_PERSIST_EVERY_N_TILES",
    "SatelliteWorker",
]
