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

"""Tests for :class:`cvfr_routemaster.satellite_worker.SatelliteWorker`.

The worker is the bulk-fetch driver — it composes the satellite_fetch
primitives (``fetch_and_cache_tile`` + ``DownloadState``) into a
chained, stoppable, resumable walk. The tests pin five behaviours:

  1. ``start_fetch`` plans correctly: enumerates the bbox, subtracts
     already-cached tiles, persists an initial state, and emits an
     initial progress tick.
  2. The chained walk terminates: every tile in the to-fetch list is
     attempted exactly once; ``finished`` fires after the last.
  3. Stop is responsive: setting the stop flag aborts the chain at
     the next tile boundary and persists ``user_decision="paused"``.
  4. Resume math is honest: a prior DownloadState with N completed
     tiles makes the progress emit start at N rather than 0.
  5. Outcome handling: ``missing`` advances silently; ``rate_limited``
     and ``network_error`` retry up to MAX_TRANSIENT_RETRIES_PER_TILE
     and then advance; ``http_error`` advances immediately;
     ``fetched`` emits ``tile_fetched``.

The QTimer-based chain is tested by patching ``QTimer.singleShot`` to
call the slot synchronously — that turns the asynchronous chain into
recursive calls which complete deterministically inside the test.
The bbox sizes used here keep recursion depth comfortably under
Python's default 1000-frame limit.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cvfr_routemaster.satellite_fetch import (  # noqa: E402
    DownloadState,
    FetchOutcome,
    read_download_state,
    write_download_state,
)
from cvfr_routemaster.satellite_tiles import (  # noqa: E402
    TileCache,
    TileCoord,
)
from cvfr_routemaster.satellite_worker import (  # noqa: E402
    MAX_TRANSIENT_RETRIES_PER_TILE,
    PROGRESS_EMIT_EVERY_N_TILES,
    STATE_PERSIST_EVERY_N_TILES,
    SatelliteWorker,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """One QApplication per process — required so the QObject
    machinery (signals, slots) works without warnings."""
    app = QApplication.instance() or QApplication([])
    return app


# Tiny test bbox: ~0.05° square at z=10. Covers 1-2 tiles, which is
# plenty for unit tests and keeps recursion depth (≈ tile count when
# QTimer.singleShot is patched to be synchronous) trivially small.
_TEST_BBOX = (32.10, 32.15, 35.10, 35.15)
_TEST_ZOOM = 10


def _patch_singleshot_synchronous():
    """Replace QTimer.singleShot with a synchronous slot caller.

    The chain pattern in :meth:`SatelliteWorker._fetch_next_tile`
    schedules itself via ``QTimer.singleShot(0, ...)``. In a unit
    test there's no Qt event loop running, so without this patch
    the chain would stall after the first tile. Calling the slot
    synchronously turns the chain into recursion, which is fine for
    the small bbox sizes the tests use.
    """
    return patch(
        "cvfr_routemaster.satellite_worker.QTimer.singleShot",
        side_effect=lambda _ms, slot: slot(),
    )


def _fixed_outcome(kind: str, content: bytes = b"x" * 512) -> FetchOutcome:
    """Hand-construct a FetchOutcome of the requested kind for
    test injection. We avoid the classmethods because some of them
    validate inputs (e.g. ``fetched`` rejects empty bytes) and we
    want pure mock control here.
    """
    if kind == "fetched":
        return FetchOutcome.fetched(content)
    if kind == "missing":
        return FetchOutcome.missing(reason="404 Not Found", status=404)
    if kind == "rate_limited":
        return FetchOutcome.rate_limited(retry_after_seconds=0.0)
    if kind == "network_error":
        return FetchOutcome.network_error(reason="timeout")
    if kind == "http_error":
        return FetchOutcome.http_error(status=403, reason="Forbidden")
    raise ValueError(f"unknown FetchOutcome kind: {kind!r}")


# --- Planning + initial state -------------------------------------------


class TestStartFetchPlanning:
    """``start_fetch`` should: enumerate the bbox, persist the
    initial state, emit an initial progress tick, and either kick
    off the walk or short-circuit on a fully-cached cache."""

    def test_initial_progress_tick_fires(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        cache = TileCache(tmp_path)
        worker = SatelliteWorker(
            cache, bbox=_TEST_BBOX, zoom=_TEST_ZOOM, sleep_fn=lambda _s: None
        )
        progress: list[tuple[int, int]] = []
        worker.progress.connect(
            lambda done, total: progress.append((done, total))
        )

        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile",
            return_value=_fixed_outcome("fetched"),
        ):
            worker.start_fetch()

        # First entry must be the planning emit (0 / total). Subsequent
        # entries come from periodic + final emits inside the walk.
        assert progress
        first_done, first_total = progress[0]
        assert first_done == 0
        assert first_total > 0

    def test_writes_initial_download_state(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        cache = TileCache(tmp_path)
        worker = SatelliteWorker(
            cache, bbox=_TEST_BBOX, zoom=_TEST_ZOOM, sleep_fn=lambda _s: None
        )
        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile",
            return_value=_fixed_outcome("fetched"),
        ):
            worker.start_fetch()

        state = read_download_state(cache)
        assert state is not None
        assert state.zoom == _TEST_ZOOM
        assert state.bbox == _TEST_BBOX
        assert state.total_tiles > 0

    def test_short_circuits_on_full_cache(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        """If every tile is already on disk, the worker should emit
        ``finished`` immediately without ever calling
        ``fetch_and_cache_tile``."""
        cache = TileCache(tmp_path)
        # Pre-populate every tile in the bbox.
        from cvfr_routemaster.satellite_tiles import bbox_to_tiles  # noqa: PLC0415

        for c in bbox_to_tiles(*_TEST_BBOX, _TEST_ZOOM):
            cache.put(c, b"x" * 512)

        worker = SatelliteWorker(
            cache, bbox=_TEST_BBOX, zoom=_TEST_ZOOM, sleep_fn=lambda _s: None
        )
        finished: list[bool] = []
        worker.finished.connect(lambda: finished.append(True))

        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile"
        ) as fetch_mock:
            worker.start_fetch()

        assert finished == [True]
        fetch_mock.assert_not_called()

    def test_full_cache_first_progress_is_total_not_zero(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        """All-cached fast path emits ``(total, total)`` as the
        FIRST (and only) progress tick — not ``(0, total)``.

        This is the contract that lets the GUI's status-bar
        readout transition straight from "untouched" to
        "z=N ✓ N tiles" without flickering through
        "z=N 0 / N (0 %)" on a returning user's startup chain.
        Before this contract, the worker emitted ``(0, total)``
        first and ``_mark_zoom_progress_done`` (called from the
        GUI's ``finished`` handler) was what eventually
        promoted the entry to "done" — but the GUI could paint
        the intermediate state once. With the seed-on-startup
        path removed (it was a multi-second GUI freeze), the
        worker's emits are the only signal the label gets and
        an honest first emit prevents the flicker."""
        cache = TileCache(tmp_path)
        from cvfr_routemaster.satellite_tiles import bbox_to_tiles  # noqa: PLC0415

        for c in bbox_to_tiles(*_TEST_BBOX, _TEST_ZOOM):
            cache.put(c, b"x" * 512)

        worker = SatelliteWorker(
            cache, bbox=_TEST_BBOX, zoom=_TEST_ZOOM, sleep_fn=lambda _s: None
        )
        progress: list[tuple[int, int]] = []
        worker.progress.connect(
            lambda done, total: progress.append((done, total))
        )

        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile"
        ):
            worker.start_fetch()

        assert progress, "worker must emit at least one progress tick"
        first_done, first_total = progress[0]
        # The honest contract: first emit on the all-cached path
        # is ``(total, total)``, not ``(0, total)``. Total must
        # match across; ``done`` must equal ``total``.
        assert first_total > 0
        assert first_done == first_total, (
            f"expected (total, total) but got ({first_done}, {first_total})"
        )

    def test_idempotent_start(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        """Calling ``start_fetch`` twice is a no-op on the second
        call (worker is single-shot)."""
        cache = TileCache(tmp_path)
        worker = SatelliteWorker(
            cache, bbox=_TEST_BBOX, zoom=_TEST_ZOOM, sleep_fn=lambda _s: None
        )
        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile",
            return_value=_fixed_outcome("fetched"),
        ) as fetch_mock:
            worker.start_fetch()
            calls_after_first = fetch_mock.call_count
            worker.start_fetch()
            assert fetch_mock.call_count == calls_after_first


# --- Walk + finish ------------------------------------------------------


class TestWalkAndFinish:
    """The walk visits every uncached tile and finishes."""

    def test_every_uncached_tile_is_fetched_exactly_once(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        cache = TileCache(tmp_path)
        from cvfr_routemaster.satellite_tiles import bbox_to_tiles  # noqa: PLC0415

        expected_coords = bbox_to_tiles(*_TEST_BBOX, _TEST_ZOOM)

        worker = SatelliteWorker(
            cache, bbox=_TEST_BBOX, zoom=_TEST_ZOOM, sleep_fn=lambda _s: None
        )
        finished: list[bool] = []
        tile_fetched: list[TileCoord] = []
        worker.finished.connect(lambda: finished.append(True))
        worker.tile_fetched.connect(tile_fetched.append)

        seen: list[TileCoord] = []

        def fake_fetch(coord, cache, **_kwargs):  # noqa: ARG001
            seen.append(coord)
            cache.put(coord, b"x" * 512)
            return _fixed_outcome("fetched")

        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile",
            side_effect=fake_fetch,
        ):
            worker.start_fetch()

        assert finished == [True]
        assert seen == expected_coords  # row-major order preserved
        assert tile_fetched == expected_coords

    def test_state_marked_complete_at_end(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        cache = TileCache(tmp_path)
        worker = SatelliteWorker(
            cache, bbox=_TEST_BBOX, zoom=_TEST_ZOOM, sleep_fn=lambda _s: None
        )
        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile"
        ) as fetch_mock:
            fetch_mock.side_effect = (
                lambda coord, cache, **_k: (
                    cache.put(coord, b"x" * 512),
                    _fixed_outcome("fetched"),
                )[1]
            )
            worker.start_fetch()

        state = read_download_state(cache)
        assert state is not None
        assert state.user_decision == "complete"
        assert state.completed_at is not None
        assert state.is_complete()

    def test_final_progress_emit_reads_full(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        """The last progress emit before ``finished`` must report
        ``done == total`` so the GUI can read 100 %."""
        cache = TileCache(tmp_path)
        worker = SatelliteWorker(
            cache, bbox=_TEST_BBOX, zoom=_TEST_ZOOM, sleep_fn=lambda _s: None
        )
        progress: list[tuple[int, int]] = []
        worker.progress.connect(
            lambda d, t: progress.append((d, t))
        )

        def fake_fetch(coord, cache, **_kwargs):
            cache.put(coord, b"x" * 512)
            return _fixed_outcome("fetched")

        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile",
            side_effect=fake_fetch,
        ):
            worker.start_fetch()

        assert progress
        last_done, last_total = progress[-1]
        assert last_done == last_total
        assert last_total > 0


# --- Stop ---------------------------------------------------------------


class TestStop:
    """``stop_fetch`` aborts the chain at the next tile boundary."""

    def test_stop_terminates_the_chain(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        """Stop after the first tile and confirm no further tiles
        are fetched, plus state is persisted as ``paused``."""
        cache = TileCache(tmp_path)
        worker = SatelliteWorker(
            cache, bbox=_TEST_BBOX, zoom=_TEST_ZOOM, sleep_fn=lambda _s: None
        )
        finished: list[bool] = []
        worker.finished.connect(lambda: finished.append(True))

        call_count = {"n": 0}

        def fake_fetch(coord, cache, **_kwargs):
            call_count["n"] += 1
            cache.put(coord, b"x" * 512)
            # Stop after the first fetch — the next chain hop sees
            # the flag and aborts before the second fetch.
            if call_count["n"] == 1:
                worker.stop_fetch()
            return _fixed_outcome("fetched")

        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile",
            side_effect=fake_fetch,
        ):
            worker.start_fetch()

        assert finished == [True]
        # Exactly one fetch happened despite the planned bbox having
        # ≥ 1 tile (the test bbox covers 1-2 tiles).
        assert call_count["n"] == 1

        state = read_download_state(cache)
        assert state is not None
        assert state.user_decision == "paused"
        assert not state.is_complete() or state.total_tiles == 1


# --- Resume -------------------------------------------------------------


class TestResume:
    """A prior DownloadState should drive the progress base."""

    def test_progress_starts_at_prior_completed(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        cache = TileCache(tmp_path)
        # Pretend a prior session already completed 1 tile out of N.
        from cvfr_routemaster.satellite_tiles import (  # noqa: PLC0415
            count_tiles_for_bbox,
        )

        total = count_tiles_for_bbox(*_TEST_BBOX, _TEST_ZOOM)
        prior = DownloadState(
            zoom=_TEST_ZOOM,
            bbox=_TEST_BBOX,
            total_tiles=total,
            completed_tiles=1,
            user_decision="paused",
            started_at="2024-01-01T00:00:00Z",
        )
        write_download_state(prior, cache)

        worker = SatelliteWorker(
            cache, bbox=_TEST_BBOX, zoom=_TEST_ZOOM, sleep_fn=lambda _s: None
        )
        progress: list[tuple[int, int]] = []
        worker.progress.connect(
            lambda d, t: progress.append((d, t))
        )
        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile"
        ) as fetch_mock:
            def fake_fetch(coord, cache, **_kwargs):
                cache.put(coord, b"x" * 512)
                return _fixed_outcome("fetched")
            fetch_mock.side_effect = fake_fetch
            worker.start_fetch()

        # The first emit must be at the prior base (1), not 0.
        assert progress
        first_done, _ = progress[0]
        assert first_done == 1

    def test_secondary_zoom_seeds_completed_base_from_filesystem(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        """Secondary zooms (``persist_state=False``) don't have a
        prior :class:`DownloadState` to read, but the multi-zoom
        chain still expects to resume work on every cold launch
        — the bulk-fetch chain skips re-downloads via
        :func:`tiles_to_fetch_for_bbox` so the *work* always
        resumes, but the *progress display* used to lie: it
        showed ``0 / total`` at session start even when 99% of
        the tiles were already on disk.

        After the fix, a secondary-zoom worker seeds its
        ``_completed_base`` from :func:`count_cached_tiles_in_bbox`
        on the filesystem, so the first progress emit honestly
        reflects "this many tiles are already done".

        Setup: pre-cache two tiles inside the bbox, then run the
        worker with ``persist_state=False``. Assert the first
        progress emit has ``done >= 2``, NOT ``done == 0``.

        We use a higher zoom (``z=14``) than the module-default
        ``_TEST_ZOOM=10`` here because the module bbox is
        intentionally tiny (~0.05° square) which lands inside a
        single tile at z=10. At z=14 the same bbox spans a
        handful of tiles — enough headroom to drop two
        pre-cached samples without exhausting the bbox."""
        from cvfr_routemaster.satellite_fetch import bbox_to_tiles  # noqa: PLC0415

        test_zoom = 14
        cache = TileCache(tmp_path)
        coords = bbox_to_tiles(*_TEST_BBOX, test_zoom)
        assert len(coords) >= 3, (
            f"test premise: bbox at z={test_zoom} must contain "
            f">=3 tiles to leave at least one for the worker to "
            f"fetch after we pre-cache two; got {len(coords)}"
        )
        cache.put(coords[0], b"x" * 512)
        cache.put(coords[1], b"y" * 512)

        worker = SatelliteWorker(
            cache,
            bbox=_TEST_BBOX,
            zoom=test_zoom,
            sleep_fn=lambda _s: None,
            persist_state=False,
        )
        progress: list[tuple[int, int]] = []
        worker.progress.connect(lambda d, t: progress.append((d, t)))
        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile"
        ) as fetch_mock:
            def fake_fetch(coord, cache, **_kwargs):
                cache.put(coord, b"x" * 512)
                return _fixed_outcome("fetched")
            fetch_mock.side_effect = fake_fetch
            worker.start_fetch()

        assert progress, "worker must emit at least one progress tick"
        first_done, first_total = progress[0]
        assert first_done >= 2, (
            f"Secondary-zoom worker must seed _completed_base from "
            f"already-cached tiles on the filesystem; expected "
            f"first emit done >= 2 (we pre-cached 2 tiles), got "
            f"first emit = ({first_done}, {first_total})"
        )

    def test_secondary_zoom_with_empty_cache_starts_at_zero(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        """Sibling guard for the test above: a secondary-zoom
        worker with an empty cache and no prior state file must
        still start at progress ``0 / total``, not at some bogus
        non-zero value the filesystem scan invented. This pins
        that :func:`count_cached_tiles_in_bbox` returns 0 on an
        empty cache and that we attribute that 0 to
        ``_completed_base`` correctly."""
        cache = TileCache(tmp_path)
        worker = SatelliteWorker(
            cache,
            bbox=_TEST_BBOX,
            zoom=_TEST_ZOOM,
            sleep_fn=lambda _s: None,
            persist_state=False,
        )
        progress: list[tuple[int, int]] = []
        worker.progress.connect(lambda d, t: progress.append((d, t)))
        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile"
        ) as fetch_mock:
            def fake_fetch(coord, cache, **_kwargs):
                cache.put(coord, b"x" * 512)
                return _fixed_outcome("fetched")
            fetch_mock.side_effect = fake_fetch
            worker.start_fetch()

        assert progress
        first_done, _ = progress[0]
        assert first_done == 0, (
            f"Empty cache must seed _completed_base to 0; got "
            f"first emit done = {first_done}"
        )


# --- Outcome handling ---------------------------------------------------


class TestOutcomeHandling:
    """Per-outcome dispatch table: which outcomes advance, retry, or
    surface, and which ones emit ``tile_fetched``."""

    def test_missing_advances_silently(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        cache = TileCache(tmp_path)
        worker = SatelliteWorker(
            cache, bbox=_TEST_BBOX, zoom=_TEST_ZOOM, sleep_fn=lambda _s: None
        )
        tile_fetched: list[TileCoord] = []
        worker.tile_fetched.connect(tile_fetched.append)
        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile",
            return_value=_fixed_outcome("missing"),
        ):
            worker.start_fetch()
        # No tile_fetched emits because nothing was successfully fetched.
        assert tile_fetched == []

    def test_http_error_advances_silently(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        cache = TileCache(tmp_path)
        worker = SatelliteWorker(
            cache, bbox=_TEST_BBOX, zoom=_TEST_ZOOM, sleep_fn=lambda _s: None
        )
        finished: list[bool] = []
        worker.finished.connect(lambda: finished.append(True))
        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile",
            return_value=_fixed_outcome("http_error"),
        ):
            worker.start_fetch()
        assert finished == [True]

    def test_rate_limited_retries_up_to_max(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        """A rate-limited tile retries until MAX, then advances.
        The fake sleep is captured so we can assert backoff was
        invoked."""
        cache = TileCache(tmp_path)
        sleeps: list[float] = []
        worker = SatelliteWorker(
            cache,
            bbox=_TEST_BBOX,
            zoom=_TEST_ZOOM,
            sleep_fn=lambda s: sleeps.append(float(s)),
        )

        # Always rate-limit the first tile, then succeed on subsequent
        # tiles so the test terminates quickly. Track call counts per
        # tile coord so we can verify the retry cap.
        call_counts: dict[TileCoord, int] = {}
        first_coord: list[TileCoord] = []

        def fake_fetch(coord, cache, **_kwargs):
            call_counts[coord] = call_counts.get(coord, 0) + 1
            if not first_coord:
                first_coord.append(coord)
            if coord == first_coord[0]:
                return _fixed_outcome("rate_limited")
            cache.put(coord, b"x" * 512)
            return _fixed_outcome("fetched")

        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile",
            side_effect=fake_fetch,
        ):
            worker.start_fetch()

        # The first tile was retried up to MAX_TRANSIENT_RETRIES_PER_TILE
        # times before the worker gave up and moved on.
        assert call_counts[first_coord[0]] == MAX_TRANSIENT_RETRIES_PER_TILE
        # And we slept at least (MAX - 1) times before giving up.
        assert len(sleeps) >= MAX_TRANSIENT_RETRIES_PER_TILE - 1

    def test_network_error_retries_then_advances(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        cache = TileCache(tmp_path)
        sleeps: list[float] = []
        worker = SatelliteWorker(
            cache,
            bbox=_TEST_BBOX,
            zoom=_TEST_ZOOM,
            sleep_fn=lambda s: sleeps.append(float(s)),
        )
        finished: list[bool] = []
        worker.finished.connect(lambda: finished.append(True))

        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile",
            return_value=_fixed_outcome("network_error"),
        ):
            worker.start_fetch()

        # All tiles failed transiently — but the walk still completed.
        assert finished == [True]
        assert sleeps  # we slept at least once on backoff


# --- Persistence cadence ------------------------------------------------


class TestPersistence:
    """The worker writes ``DownloadState`` periodically inside the
    walk so a crash mid-fetch loses bounded progress."""

    def test_state_persistence_constants_sane(self) -> None:
        # Persist cadence must be at least as coarse as the progress
        # cadence — otherwise we'd write the file faster than we
        # update the GUI, which is silly.
        assert STATE_PERSIST_EVERY_N_TILES >= PROGRESS_EMIT_EVERY_N_TILES
        assert PROGRESS_EMIT_EVERY_N_TILES > 0
        assert STATE_PERSIST_EVERY_N_TILES > 0


# --- Failure path -------------------------------------------------------


class TestFailurePath:
    """Hard cache I/O errors should emit ``failed``, not ``finished``."""

    def test_cache_write_error_emits_failed(
        self, tmp_path: Path, qapp  # noqa: ARG002
    ) -> None:
        cache = TileCache(tmp_path)
        worker = SatelliteWorker(
            cache, bbox=_TEST_BBOX, zoom=_TEST_ZOOM, sleep_fn=lambda _s: None
        )
        failed: list[str] = []
        finished: list[bool] = []
        worker.failed.connect(failed.append)
        worker.finished.connect(lambda: finished.append(True))

        def explode(coord, cache, **_kwargs):  # noqa: ARG001
            raise OSError("simulated disk full")

        with _patch_singleshot_synchronous(), patch(
            "cvfr_routemaster.satellite_worker.fetch_and_cache_tile",
            side_effect=explode,
        ):
            worker.start_fetch()

        assert failed
        assert "satellite cache write failed" in failed[0]
        assert finished == []
