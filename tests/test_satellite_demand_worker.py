"""Unit tests for :class:`OnDemandFetchWorker`.

Drives the worker synchronously by calling
:meth:`OnDemandFetchWorker._process_next` directly rather than via
the chained ``QTimer.singleShot`` mechanism. That way each test
verifies behaviour deterministically without spinning up a
QApplication event loop or sleeping. Production code paths the
direct ``_process_next`` call to schedule itself; the throttle and
the chaining are not what's under test here — the behaviour we
care about is queue management + outcome dispatch.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

if QCoreApplication.instance() is None:
    _APP = QApplication.instance() or QApplication(sys.argv[:1])

from cvfr_routemaster.satellite_demand_worker import (  # noqa: E402
    DEFAULT_MAX_PENDING,
    DEFAULT_THROTTLE_MS,
    OnDemandFetchWorker,
)
from cvfr_routemaster.satellite_fetch import FetchOutcome  # noqa: E402
from cvfr_routemaster.satellite_tiles import TileCache, TileCoord  # noqa: E402


def _make_worker(tmp_path: Path, **overrides: object) -> OnDemandFetchWorker:
    cache = TileCache(root=tmp_path / "tiles")
    kwargs = {
        "tile_cache": cache,
        "url_template": "https://example.invalid/{z}/{x}/{y}.jpg",
        "user_agent": "test-agent",
    }
    kwargs.update(overrides)  # type: ignore[arg-type]
    return OnDemandFetchWorker(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_max_pending_is_1000(self) -> None:
        # Sized to fit a zoom-out viewport's worth of misses
        # without dropping: at the coarsest configured zoom
        # (z=12 over Israel) ~1300 tiles can be simultaneously
        # visible and a single pan-into-empty-cache enqueues all
        # of them. The previous 200-tile cap dropped ~85% of
        # those, leaving most of the chart un-backfilled.
        assert DEFAULT_MAX_PENDING == 1000

    def test_default_throttle_is_50_ms(self) -> None:
        # 20 req/s — well within Esri's published per-IP rate
        # ceiling and fast enough that a full zoom-out backfill
        # of an uncached coarse zoom (~1300 tiles) completes in
        # ~65 s rather than the ~4-5 min the previous 200 ms
        # (5 req/s) throttle implied. Any 429s still trip the
        # existing backoff path so we degrade gracefully if Esri
        # ever pushes back.
        assert DEFAULT_THROTTLE_MS == 50


# ---------------------------------------------------------------------------
# enqueue / dedup / cap
# ---------------------------------------------------------------------------


class TestEnqueue:
    def test_enqueue_adds_to_queue(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path)
        coord = TileCoord(z=14, x=1, y=2)
        worker.enqueue(coord)
        assert worker.pending_count() == 1
        assert worker.is_enqueued(coord)

    def test_enqueue_ignores_non_tilecoord(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path)
        worker.enqueue("not-a-coord")  # type: ignore[arg-type]
        worker.enqueue(None)  # type: ignore[arg-type]
        worker.enqueue(42)  # type: ignore[arg-type]
        assert worker.pending_count() == 0

    def test_enqueue_dedups_within_pending(self, tmp_path: Path) -> None:
        """Re-enqueueing a coord that's already pending is a no-op.
        Without dedup the queue would balloon under repeated
        update_visibility calls (every pan re-reports the same
        misses while the worker is busy)."""
        worker = _make_worker(tmp_path)
        coord = TileCoord(z=14, x=1, y=2)
        worker.enqueue(coord)
        worker.enqueue(coord)
        worker.enqueue(coord)
        assert worker.pending_count() == 1

    def test_enqueue_drops_oldest_when_full(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path, max_pending=3)
        coords = [TileCoord(z=14, x=i, y=0) for i in range(5)]
        for c in coords:
            worker.enqueue(c)
        assert worker.pending_count() == 3
        # Oldest two coords (idx 0 and 1) should have been dropped;
        # the cap-window contains only the most recent three.
        assert not worker.is_enqueued(coords[0])
        assert not worker.is_enqueued(coords[1])
        for c in coords[2:]:
            assert worker.is_enqueued(c)

    def test_enqueue_after_stop_is_noop(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path)
        worker.request_stop()
        worker.enqueue(TileCoord(z=14, x=1, y=2))
        assert worker.pending_count() == 0


# ---------------------------------------------------------------------------
# cancel_pending
# ---------------------------------------------------------------------------


class TestCancelPending:
    def test_cancel_removes_from_queue(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path)
        coord_a = TileCoord(z=14, x=1, y=0)
        coord_b = TileCoord(z=14, x=2, y=0)
        worker.enqueue(coord_a)
        worker.enqueue(coord_b)
        worker.cancel_pending(coord_a)
        assert not worker.is_enqueued(coord_a)
        assert worker.is_enqueued(coord_b)
        assert worker.pending_count() == 1

    def test_cancel_unknown_coord_is_noop(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path)
        worker.cancel_pending(TileCoord(z=14, x=99, y=99))


# ---------------------------------------------------------------------------
# Outcome dispatch
# ---------------------------------------------------------------------------


class TestOutcomeDispatch:
    """Verify that ``_process_next`` translates each
    :class:`FetchOutcome` shape into the right signal / queue
    state.

    We patch ``fetch_and_cache_tile`` so the test never hits the
    network; the worker's real fetch path is exercised end-to-end
    by integration tests.
    """

    def test_fetched_emits_tile_ready(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path, throttle_ms=0)
        coord = TileCoord(z=14, x=1, y=2)
        worker.enqueue(coord)
        ready: list[object] = []
        worker.tile_ready.connect(lambda c: ready.append(c))
        with patch(
            "cvfr_routemaster.satellite_demand_worker.fetch_and_cache_tile",
            return_value=FetchOutcome.fetched(b"jpegbytes"),
        ):
            worker._process_next()  # noqa: SLF001
        # Process the event loop so queued signal dispatches run.
        QCoreApplication.processEvents()
        assert coord in ready
        assert not worker.is_enqueued(coord)

    def test_missing_does_not_emit(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path, throttle_ms=0)
        coord = TileCoord(z=14, x=1, y=2)
        worker.enqueue(coord)
        ready: list[object] = []
        failed: list[object] = []
        worker.tile_ready.connect(lambda c: ready.append(c))
        worker.tile_failed.connect(lambda c, m: failed.append((c, m)))
        with patch(
            "cvfr_routemaster.satellite_demand_worker.fetch_and_cache_tile",
            return_value=FetchOutcome.missing(reason="404"),
        ):
            worker._process_next()  # noqa: SLF001
        QCoreApplication.processEvents()
        assert ready == []
        assert failed == []
        assert not worker.is_enqueued(coord)

    def test_network_error_emits_tile_failed(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path, throttle_ms=0)
        coord = TileCoord(z=14, x=1, y=2)
        worker.enqueue(coord)
        failed: list[tuple[object, str]] = []
        worker.tile_failed.connect(
            lambda c, m: failed.append((c, m))
        )
        with patch(
            "cvfr_routemaster.satellite_demand_worker.fetch_and_cache_tile",
            return_value=FetchOutcome.network_error(reason="timeout"),
        ):
            worker._process_next()  # noqa: SLF001
        QCoreApplication.processEvents()
        assert len(failed) == 1
        assert failed[0][0] == coord
        assert "timeout" in failed[0][1]

    def test_http_error_emits_tile_failed(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path, throttle_ms=0)
        coord = TileCoord(z=14, x=1, y=2)
        worker.enqueue(coord)
        failed: list[tuple[object, str]] = []
        worker.tile_failed.connect(
            lambda c, m: failed.append((c, m))
        )
        with patch(
            "cvfr_routemaster.satellite_demand_worker.fetch_and_cache_tile",
            return_value=FetchOutcome.http_error(status=403, reason="forbidden"),
        ):
            worker._process_next()  # noqa: SLF001
        QCoreApplication.processEvents()
        assert len(failed) == 1
        assert failed[0][0] == coord
        assert "forbidden" in failed[0][1]

    def test_rate_limited_reenqueues(self, tmp_path: Path) -> None:
        """A 429 should put the coord *back* at the front of the
        queue (with the throttle stretched to honour Retry-After)
        rather than emitting tile_failed / dropping the work.

        We deliberately don't ``processEvents`` here — that would
        let the chained ``QTimer.singleShot(_process_next)`` fire,
        run another fetch (now outside the mock context), and hit
        real DNS. Same-thread direct signal connections deliver
        synchronously, so all the assertions are valid right
        after the explicit ``_process_next`` call.
        """
        worker = _make_worker(tmp_path, throttle_ms=0)
        coord = TileCoord(z=14, x=1, y=2)
        worker.enqueue(coord)
        ready: list[object] = []
        failed: list[object] = []
        worker.tile_ready.connect(lambda c: ready.append(c))
        worker.tile_failed.connect(
            lambda c, m: failed.append((c, m))
        )
        with patch(
            "cvfr_routemaster.satellite_demand_worker.fetch_and_cache_tile",
            return_value=FetchOutcome.rate_limited(retry_after_seconds=None),
        ), patch(
            "cvfr_routemaster.satellite_demand_worker.time.sleep"
        ) as sleep_mock:
            worker._process_next()  # noqa: SLF001
        assert ready == []
        assert failed == []
        # Coord is back in the queue.
        assert worker.is_enqueued(coord)
        assert worker.pending_count() == 1
        # The default rate-limited backoff is non-zero, so we
        # expect at least one sleep call.
        assert sleep_mock.call_count >= 1

    def test_unexpected_outcome_kind_is_surfaced(
        self, tmp_path: Path
    ) -> None:
        worker = _make_worker(tmp_path, throttle_ms=0)
        coord = TileCoord(z=14, x=1, y=2)
        worker.enqueue(coord)
        failed: list[tuple[object, str]] = []
        worker.tile_failed.connect(
            lambda c, m: failed.append((c, m))
        )
        # Construct an outcome with a kind the worker doesn't
        # recognise. Wraps the dataclass to hit the fallback
        # branch defensively.
        weird = FetchOutcome(kind="aliens", reason="?")
        with patch(
            "cvfr_routemaster.satellite_demand_worker.fetch_and_cache_tile",
            return_value=weird,
        ):
            worker._process_next()  # noqa: SLF001
        QCoreApplication.processEvents()
        assert len(failed) == 1
        assert "aliens" in failed[0][1]

    def test_fetch_raises_emits_tile_failed(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path, throttle_ms=0)
        coord = TileCoord(z=14, x=1, y=2)
        worker.enqueue(coord)
        failed: list[tuple[object, str]] = []
        worker.tile_failed.connect(
            lambda c, m: failed.append((c, m))
        )
        with patch(
            "cvfr_routemaster.satellite_demand_worker.fetch_and_cache_tile",
            side_effect=OSError("disk full"),
        ):
            worker._process_next()  # noqa: SLF001
        QCoreApplication.processEvents()
        assert len(failed) == 1
        assert "disk full" in failed[0][1]
        # And the coord is no longer enqueued (we're not retrying
        # generic exceptions; only rate_limited gets re-queued).
        assert not worker.is_enqueued(coord)


# ---------------------------------------------------------------------------
# Stop semantics
# ---------------------------------------------------------------------------


class TestStop:
    def test_request_stop_emits_finished_immediately(
        self, tmp_path: Path
    ) -> None:
        """``request_stop`` is the GUI's only knock on the worker
        once teardown begins; the worker must emit ``finished``
        synchronously so the wired ``deleteLater`` event is
        queued *on the worker's own thread* before
        ``thread.quit`` lands. Without this immediate emit, an
        idle worker (empty queue, no pending timer) would never
        run ``_process_next`` again — and the GUI's
        ``thread.wait()`` would hit its full timeout for nothing.
        """
        worker = _make_worker(tmp_path, throttle_ms=0)
        worker.enqueue(TileCoord(z=14, x=1, y=2))
        finished_count: list[int] = [0]
        worker.finished.connect(
            lambda: finished_count.__setitem__(0, finished_count[0] + 1)
        )
        worker.request_stop()
        # Finished fired exactly once on the request_stop call.
        assert finished_count[0] == 1
        # The coord stays in the queue (stop short-circuits before
        # the dequeue), but with the worker stopped the queue is
        # effectively dead — is_enqueued reports the coord still
        # there, but no further _process_next will run.
        assert worker.is_enqueued(TileCoord(z=14, x=1, y=2))

    def test_finished_emits_at_most_once(
        self, tmp_path: Path
    ) -> None:
        """Multiple ``_process_next`` calls after stop should each
        notice the stop flag, but ``finished`` only emits the
        first time. Without the idempotency guard, every chained
        timer + the request_stop call would re-emit; consumers
        expect a single terminal."""
        worker = _make_worker(tmp_path, throttle_ms=0)
        finished_count: list[int] = [0]
        worker.finished.connect(
            lambda: finished_count.__setitem__(0, finished_count[0] + 1)
        )
        worker.request_stop()
        # First emit happens here — by the immediate-emit rule.
        assert finished_count[0] == 1
        # Subsequent stops + drained _process_next calls don't
        # re-emit thanks to the ``_finished_emitted`` flag.
        worker.request_stop()
        for _ in range(5):
            worker._process_next()  # noqa: SLF001
        assert finished_count[0] == 1
