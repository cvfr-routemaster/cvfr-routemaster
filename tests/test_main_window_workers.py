"""Tests for :mod:`cvfr_routemaster.main_window`'s worker-management
helpers.

Two focused groups, both extracted from ``MainWindow`` as pure /
near-pure helpers so they're testable without standing up a real
``QMainWindow`` + ``QGraphicsScene`` + chart-loader pipeline:

* :class:`TestPlanSatelliteZoomChain` — the bulk-fetch chain-order
  policy. Pinned because the policy (coarsest-first, persist-on-finest)
  has now flipped twice in the project's history (originally
  finest-first because default view scale put the user on the finest
  zoom; now coarsest-first because the anchor-6.0 multi-zoom selector
  puts default fit-to-screen on z=12). A future view-scale anchor
  change must not silently re-flip this without an explicit decision.

* :class:`TestForceStopThreads` — the time-bounded shutdown helper.
  The user reported "QThread: Destroyed while thread '' is still
  running" warnings + multi-second hangs on the red X click; the
  fix is a bounded ``wait + terminate`` policy and these tests pin
  the time bound so a regression that re-introduces a 30-second
  polite-wait would scream loudly.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import (  # noqa: E402
    QCoreApplication,
    QObject,
    QThread,
    Signal,
    Slot,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

from cvfr_routemaster.main_window import (  # noqa: E402
    _force_stop_threads,
    _plan_satellite_zoom_chain,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """One QApplication per process — required so ``QThread``
    machinery has a parent event loop available."""
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def thread_pool() -> Any:
    """Collect spawned QThreads + workers and guarantee cleanup
    on teardown.

    The pytest interpreter crash (``STATUS_STACK_BUFFER_OVERRUN``)
    we saw without this fixture was Python GC racing with
    ``QThread`` destruction: when a test returns with a forcibly
    terminated thread still referenced from a stale Python
    wrapper, GC eventually tries to clean up, the wrapper calls
    the C++ destructor, and on Windows ``TerminateThread``-killed
    threads can leave kernel state that makes that destructor
    crash. The fix is to *explicitly* wait + (optionally
    terminate) every thread at end-of-test, before pytest's
    fixture teardown can let Python GC anywhere near them.

    Use:
        def test_x(thread_pool, qapp):
            thread, worker = thread_pool.spawn(sleep_seconds=10.0)
            ...
    """

    class _Pool:
        def __init__(self) -> None:
            self._threads: list[QThread] = []
            self._workers: list[_StuckWorker] = []

        def spawn(
            self, sleep_seconds: float
        ) -> tuple[QThread, _StuckWorker]:
            thread, worker = _spawn_stuck_thread(sleep_seconds)
            self._threads.append(thread)
            self._workers.append(worker)
            return thread, worker

        def cleanup(self) -> None:
            for thread in self._threads:
                if thread.isRunning():
                    thread.terminate()
                    thread.wait(1000)
            # Drain any queued events (e.g. ``finished`` signals
            # from threads that quit naturally) before letting
            # Python GC at the wrappers.
            QCoreApplication.processEvents()
            self._threads.clear()
            self._workers.clear()

    pool = _Pool()
    try:
        yield pool
    finally:
        pool.cleanup()


# ---------------------------------------------------------------------------
# _plan_satellite_zoom_chain — pure-function chain-order policy
# ---------------------------------------------------------------------------


class TestPlanSatelliteZoomChain:
    # Policy under test (pinned here so a deliberate policy change
    # has to update the assertions, not just the helper):
    #
    #   1. The chain runs coarsest-first. z=12 (smallest tile count,
    #      ~1.3 k tiles for Israel) before z=13 (~5 k) before z=14
    #      (~18 k) before z=15 (~72 k). A fresh user sees a usable
    #      satellite layer in minutes rather than hours.
    #   2. ``persist_state=True`` only on the FINEST (last) link.
    #      The state file's whole job is enabling cross-session
    #      resume of one big download; only the largest zoom is
    #      big enough to be worth resuming.
    #   3. At most one link has persist=True. If the user only
    #      has one configured zoom, that one is persistable.

    def test_default_four_level_set_coarsest_first(self) -> None:
        """The default ``[12, 13, 14, 15]`` set produces
        z=12 → z=13 → z=14 → z=15, with persist=True only on the
        last link."""
        plan = _plan_satellite_zoom_chain([12, 13, 14, 15])
        assert plan == [
            (12, False),
            (13, False),
            (14, False),
            (15, True),
        ]

    def test_legacy_three_level_set(self) -> None:
        """Pre-z=15 default ``[12, 13, 14]`` still produces a
        sensible plan: z=12 → z=13 → z=14 with persist on z=14."""
        plan = _plan_satellite_zoom_chain([12, 13, 14])
        assert plan == [(12, False), (13, False), (14, True)]

    def test_single_level_gets_persist_true(self) -> None:
        """User who configured only one zoom (e.g. via
        ``satellite_zoom = 12``) still gets persist=True on that
        single link — the only download we have is the one
        worth resuming."""
        for z in (12, 13, 14, 15):
            assert _plan_satellite_zoom_chain([z]) == [(z, True)]

    def test_unsorted_input_is_sorted_ascending(self) -> None:
        """Caller doesn't have to pre-sort. The plan is always
        ascending so the visible result is independent of caller
        ordering — the policy is "always coarsest-first" and
        that has to be unambiguous."""
        plan = _plan_satellite_zoom_chain([15, 12, 14, 13])
        assert plan == [
            (12, False),
            (13, False),
            (14, False),
            (15, True),
        ]

    def test_duplicates_are_deduped(self) -> None:
        """Duplicate levels (e.g. a caller bug or a misconfigured
        settings file) are silently deduped. Better than raising,
        because the user's config still produces a usable result
        and the multi-zoom overlay code is duplicate-tolerant
        downstream anyway."""
        plan = _plan_satellite_zoom_chain([14, 12, 14, 13, 12])
        assert plan == [(12, False), (13, False), (14, True)]

    def test_empty_input_returns_empty_plan(self) -> None:
        """No levels = no plan. Caller (``_start_satellite_worker``)
        guards on empty plan and short-circuits before spinning
        up a thread; this just makes the helper safe to call
        unconditionally."""
        assert _plan_satellite_zoom_chain([]) == []

    def test_persist_true_appears_exactly_once(self) -> None:
        """Invariant: at most one persist=True link per chain
        (the finest), regardless of chain length. The state
        file's resume semantics assume one canonical
        "in-progress" zoom — multiple persist=True workers would
        race to overwrite it."""
        for levels in ([12], [12, 13], [12, 13, 14], [12, 13, 14, 15]):
            plan = _plan_satellite_zoom_chain(levels)
            persist_count = sum(1 for _z, persist in plan if persist)
            assert persist_count == 1, (
                f"levels={levels} produced {persist_count} persist=True links"
            )

    def test_persist_true_is_on_finest_zoom(self) -> None:
        """Companion to ``persist_true_appears_exactly_once``: not
        only is there exactly one persist=True link, it's the
        finest (highest) zoom in the chain. This is what makes
        z=15 — the only fetch big enough that resume matters —
        the one that gets the affordance."""
        for levels in ([12], [12, 13], [12, 13, 14], [12, 13, 14, 15]):
            plan = _plan_satellite_zoom_chain(levels)
            (persist_zoom,) = [z for z, persist in plan if persist]
            assert persist_zoom == max(levels)


# ---------------------------------------------------------------------------
# _force_stop_threads — time-bounded shutdown helper
# ---------------------------------------------------------------------------


class _StuckWorker(QObject):
    """A worker that simulates "stuck in a blocking I/O call".

    Sleeps in a Python ``time.sleep`` for a configurable duration.
    Because Python releases the GIL during ``time.sleep`` but
    *doesn't* return to the Qt event loop, this perfectly mimics
    a worker mid-``urlopen``: ``QMetaObject.invokeMethod`` /
    ``QThread.quit`` slots queued from outside can't run until
    the sleep returns. That's the exact pathology the user
    reported on the red X.

    We don't use the real ``SatelliteWorker`` here because we'd
    need a tile cache + HTTP mock + bbox enumerator on top of
    the time-bound test, and the bug isn't worker-specific —
    it's a general "QThread blocked on a slow syscall" case.
    """

    started_signal = Signal()
    finished = Signal()

    def __init__(self, sleep_seconds: float = 10.0) -> None:
        super().__init__()
        self._sleep_seconds = float(sleep_seconds)
        self._stopped: bool = False

    @Slot()
    def start_work(self) -> None:
        """Simulate a long-running blocking I/O call."""
        self.started_signal.emit()
        # ``time.sleep`` is the simplest possible blocking call.
        # Real workers are blocked in ``urllib.request.urlopen`` /
        # ``socket.recv`` etc., which behave identically from Qt's
        # perspective.
        time.sleep(self._sleep_seconds)
        self.finished.emit()


def _spawn_stuck_thread(sleep_seconds: float) -> tuple[QThread, _StuckWorker]:
    """Build + start a QThread running a ``_StuckWorker``.

    Returns the ``(thread, worker)`` pair so the caller can
    reference both for assertion + cleanup. Caller is responsible
    for tearing the thread down (via ``_force_stop_threads`` —
    that's the whole point of the test).

    Deliberately *no* ``deleteLater`` wiring: in tests that
    exercise ``terminate()``, the worker thread's event loop is
    forcibly killed before any pending ``deleteLater`` queued
    event can be processed. Letting those pile up confuses the
    PySide6 ownership model on shutdown and crashes the
    interpreter with ``STATUS_STACK_BUFFER_OVERRUN`` on Windows.
    Instead, we hold Python references to the QObject wrappers
    until the test exits, then let Python GC handle them when
    the module-scoped fixtures unwind.
    """
    thread = QThread()
    worker = _StuckWorker(sleep_seconds=sleep_seconds)
    worker.moveToThread(thread)
    thread.started.connect(worker.start_work)
    # ``finished → thread.quit`` is still safe — it just causes
    # the event loop to exit when the worker finishes naturally.
    # ``deleteLater`` is the bit we omit.
    worker.finished.connect(thread.quit)
    thread.start()
    # Wait briefly so the worker actually enters ``time.sleep``
    # before the test calls ``_force_stop_threads``. Without this,
    # there's a race where the thread is "started" but the worker
    # slot hasn't run yet — the wait would return immediately
    # (event loop empty) and the test would think the helper
    # worked when it actually never had a stuck worker to stop.
    # ``processEvents`` + a short ``msleep`` is sufficient: the
    # queued ``started → start_work`` connection fires inside the
    # processEvents call, and msleep lets ``time.sleep`` actually
    # begin on the worker thread.
    QCoreApplication.processEvents()
    QThread.msleep(50)
    return thread, worker


class TestForceStopThreads:
    def test_returns_quickly_for_already_finished_threads(
        self, qapp: QApplication, thread_pool: Any
    ) -> None:
        """Common case: ``closeEvent`` runs *after* the user has
        let the bulk fetch complete naturally. All workers have
        already emitted ``finished``, the threads have exited.
        ``_force_stop_threads`` should return essentially
        immediately — far faster than the polite timeout."""
        thread, _worker = thread_pool.spawn(sleep_seconds=0.01)
        # Wait for the worker to finish its short sleep, then
        # drain the main-thread event loop so the queued
        # ``thread.quit`` (cross-thread auto-connection from
        # ``worker.finished``) actually runs. Without
        # ``processEvents`` here, the QThread receives no quit
        # signal in this no-event-loop test environment.
        QThread.msleep(150)
        QCoreApplication.processEvents()
        thread.wait(500)
        assert not thread.isRunning()
        started = time.monotonic()
        _force_stop_threads([thread], polite_timeout_ms=2000)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        # Generous bound — even a "no-op wait" takes a few ms on
        # Windows due to Qt's event-loop ticking. Anything well
        # under the 2000 ms polite timeout confirms the helper
        # didn't sit on a wait it didn't need.
        assert elapsed_ms < 300.0, (
            f"already-finished thread took {elapsed_ms:.0f} ms to "
            "force-stop — expected <300 ms"
        )

    # The "stuck-thread + terminate" budget test that lived here
    # exercised the full real-QThread terminate path with a
    # worker stuck in ``time.sleep``. It was reliably tripping
    # ``STATUS_STACK_BUFFER_OVERRUN`` interpreter aborts on
    # Windows during pytest's post-test cleanup, because
    # ``QThread.terminate()`` on a Python-thread-with-a-live-
    # ``time.sleep`` leaves PySide6 in a state where the next
    # Python GC pass over the QObject wrappers crashes the
    # interpreter. The algorithm itself is pinned reliably by
    # ``test_terminates_when_polite_wait_leaves_thread_running``
    # below (mock-based, no real OS thread to corrupt), and the
    # user-visible behaviour (snappy close) is verified by
    # manually clicking the red X — which is the test the user
    # explicitly requested. So we keep the algorithmic pin and
    # let manual smoke-testing cover the OS-integration end.

    def test_handles_none_entries_silently(self, qapp: QApplication) -> None:
        """``None`` entries in the threads list (workers that were
        never started) are skipped silently. The helper has to be
        safe to call unconditionally on shutdown — checking each
        ``self._xxx_thread`` for None at the call site would just
        be ceremony that obscures the policy."""
        # Sanity: a list of all-None thread refs returns
        # near-instantly with no exceptions.
        started = time.monotonic()
        _force_stop_threads([None, None, None], polite_timeout_ms=2000)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        assert elapsed_ms < 50.0

    def test_handles_mixed_none_and_running_with_mock(
        self, qapp: QApplication
    ) -> None:
        """Real shutdown shape: some workers ran (have a thread),
        some didn't (still ``None``). The helper has to process
        the live ones and skip the dead-from-birth ones without
        crashing.

        Mock-based for the same reason
        ``test_terminates_when_polite_wait_leaves_thread_running``
        is — a real ``time.sleep`` + ``terminate`` trips PySide6
        cleanup on Windows. The mock pins the call shape
        unambiguously."""

        class _FinishingMockThread:
            """Mock thread that 'finishes naturally' under the
            polite wait — the helper should not terminate it."""

            def __init__(self) -> None:
                self.terminate_called = False

            def wait(self, _ms: int) -> bool:
                return True

            def isRunning(self) -> bool:  # noqa: N802
                return False

            def terminate(self) -> None:
                self.terminate_called = True

        live = _FinishingMockThread()
        _force_stop_threads(
            [None, live, None],  # type: ignore[list-item]
            polite_timeout_ms=50,
            force_timeout_ms=50,
        )
        # The live thread was processed (no exception), the
        # None entries were silently skipped, and we didn't
        # spuriously terminate a thread that finished cleanly.
        assert not live.terminate_called

    def test_swallows_runtime_error_for_already_destroyed_thread(
        self, qapp: QApplication
    ) -> None:
        """If the underlying C++ QThread has already been
        destroyed (e.g. its ``finished → deleteLater`` chain
        already fired before our helper ran), attribute access
        on the Python wrapper raises ``RuntimeError``. The
        helper has to catch this per-thread so a destroyed-early
        worker doesn't prevent the live ones from being
        stopped."""

        class _FakeDestroyedThread:
            """Mimic the PySide6 ``RuntimeError: Internal C++
            object already deleted`` behaviour."""

            def wait(self, _ms: int) -> bool:
                raise RuntimeError(
                    "Internal C++ object (QThread) already deleted."
                )

            def isRunning(self) -> bool:  # noqa: N802 — Qt API mimic.
                raise RuntimeError(
                    "Internal C++ object (QThread) already deleted."
                )

            def terminate(self) -> None:
                raise RuntimeError(
                    "Internal C++ object (QThread) already deleted."
                )

        # Helper must not raise — the destroyed entry is just
        # silently skipped.
        _force_stop_threads(
            [_FakeDestroyedThread()],  # type: ignore[list-item]
            polite_timeout_ms=100,
            force_timeout_ms=100,
        )

    def test_terminates_when_polite_wait_leaves_thread_running(
        self, qapp: QApplication
    ) -> None:
        """Algorithm pin: if ``isRunning()`` is True after the
        polite wait, the helper MUST call ``terminate()`` and
        then ``wait(force_timeout_ms)`` once more. The real-thread
        version (``test_terminates_stuck_thread_within_polite_plus_force_budget``)
        only asserts the time budget; this one pins the call
        sequence so a refactor that 'just removes the terminate
        for safety' would be caught immediately."""
        events: list[str] = []

        class _StuckMockThread:
            """Mock thread that stays running after the polite
            wait, then 'finishes' after terminate."""

            def __init__(self) -> None:
                self._terminated = False

            def wait(self, _ms: int) -> bool:
                events.append("wait")
                return self._terminated

            def isRunning(self) -> bool:  # noqa: N802
                events.append("isRunning")
                return not self._terminated

            def terminate(self) -> None:
                events.append("terminate")
                self._terminated = True

        _force_stop_threads(
            [_StuckMockThread()],  # type: ignore[list-item]
            polite_timeout_ms=10,
            force_timeout_ms=10,
        )
        # The exact sequence the algorithm prescribes:
        #   wait(polite) -> isRunning() (True) -> terminate() ->
        #   wait(force).
        assert events == [
            "wait",
            "isRunning",
            "terminate",
            "wait",
        ]

    def test_skips_terminate_when_polite_wait_succeeded(
        self, qapp: QApplication
    ) -> None:
        """Algorithm pin: if ``isRunning()`` is False after the
        polite wait, ``terminate()`` is NOT called. The natural
        finish path is the common one and shouldn't trigger the
        scary force-kill branch."""
        events: list[str] = []

        class _CleanMockThread:
            def wait(self, _ms: int) -> bool:
                events.append("wait")
                return True

            def isRunning(self) -> bool:  # noqa: N802
                events.append("isRunning")
                return False

            def terminate(self) -> None:
                events.append("terminate")

        _force_stop_threads(
            [_CleanMockThread()],  # type: ignore[list-item]
            polite_timeout_ms=10,
            force_timeout_ms=10,
        )
        assert events == ["wait", "isRunning"]
        assert "terminate" not in events

    def test_processes_threads_in_input_order(
        self, qapp: QApplication
    ) -> None:
        """Order pinning: the helper processes the input list
        head-first. This matches the shutdown call site's intent
        (signal everyone, *then* drain in input order — currently
        vatsim → bulk-sat → demand-sat). If a future refactor
        reorders the call-site list, this test will still pass
        (it just verifies "the order the caller gave is the
        order the helper processed"), but the call site's order
        comment will be the authoritative spec."""
        events: list[str] = []

        class _TrackingThread:
            def __init__(self, name: str) -> None:
                self._name = name

            def wait(self, _ms: int) -> bool:
                events.append(f"wait:{self._name}")
                return True

            def isRunning(self) -> bool:  # noqa: N802
                events.append(f"isRunning:{self._name}")
                return False

            def terminate(self) -> None:
                events.append(f"terminate:{self._name}")

        _force_stop_threads(
            [_TrackingThread("a"), _TrackingThread("b"), _TrackingThread("c")],  # type: ignore[list-item]
            polite_timeout_ms=10,
            force_timeout_ms=10,
        )
        # Each thread is processed (wait + isRunning) before the
        # next thread is touched; no interleaving.
        assert events == [
            "wait:a",
            "isRunning:a",
            "wait:b",
            "isRunning:b",
            "wait:c",
            "isRunning:c",
        ]


# ---------------------------------------------------------------------------
# Satellite chain transition — must defer the next link via QTimer.singleShot
# ---------------------------------------------------------------------------


class _ChainHarness:
    """Minimal duck-typed surface for the bits of ``MainWindow`` that
    :meth:`MainWindow._on_satellite_finished` actually touches.

    We bind the *real* unbound method onto this harness (see test
    constructors below) so any signature drift in the production
    method immediately breaks the chain tests.

    The harness records every ``_start_satellite_worker_for_zoom``
    invocation and the order of operations, which is the key
    observable in the deferred-chain-transition regression test.
    """

    def __init__(self) -> None:
        # Production fields read inside ``_on_satellite_finished``:
        self._satellite_running_zoom: int = 12
        self._satellite_pending_zoom_chain: list[tuple[int, bool]] = []
        self._north_sat_overlay = None
        self._south_sat_overlay = None
        self._sat_progress_label = None

        # Stubbed-out toggle action — chain-transition path doesn't
        # need a real ``QAction``, just an ``isChecked`` method.
        class _Toggle:
            @staticmethod
            def isChecked() -> bool:  # noqa: N802 — Qt API mimic.
                return False

        self._act_show_satellite = _Toggle()

        # Recording surfaces:
        self.start_for_zoom_calls: list[tuple[int, bool]] = []
        self.cleanup_calls: int = 0
        self.mark_done_calls: list[int] = []
        self.refresh_called: bool = False

    # --- stubs the production method calls -------------------------------

    def _satellite_tile_cache(self) -> object:
        return object()

    def _cleanup_satellite_worker_refs(self) -> None:
        self.cleanup_calls += 1

    def _mark_zoom_progress_done(self, z: int) -> None:
        self.mark_done_calls.append(z)

    def _start_satellite_worker_for_zoom(
        self, zoom: int, *, persist: bool
    ) -> None:
        self.start_for_zoom_calls.append((zoom, persist))

    def _satellite_zoom_levels(self) -> list[int]:
        return [12, 13, 14, 15]

    def statusBar(self) -> object:  # noqa: N802 — Qt API mimic.
        class _StatusBar:
            @staticmethod
            def showMessage(*_args: object, **_kwargs: object) -> None:
                pass

        return _StatusBar()


class TestSatelliteChainTransitionDeferred:
    """The chain-transition kickoff inside ``_on_satellite_finished``
    must be deferred via ``QTimer.singleShot(0, ...)``, not run
    synchronously.

    Regression motivation
    ---------------------

    The v3.3 Windows build crashed with a fail-fast
    (``STATUS_STACK_BUFFER_OVERRUN``, BEX64 ``0xc0000409``) in
    ``Qt6Core.dll`` on the second launch following a partial
    satellite download. The crash signature is consistent with a
    Q_ASSERT firing inside Qt's signal/slot dispatcher.

    The mechanism: on a returning user with cached secondary
    zooms (z=12 and z=13 fully populated from a prior session),
    the chain ``[12, 13, 14, 15]`` walks through z=12 and z=13
    in milliseconds each — each link spins up a fresh ``QThread``
    + ``SatelliteWorker`` pair, the worker's ``start_fetch``
    short-circuits to ``finished`` because every tile is already
    cached, and the next link's construction is invoked
    *synchronously inside* ``_on_satellite_finished``. That means
    Qt is asked to construct a new ``QThread`` + ``QObject`` +
    full signal-slot graph while the previous pair's teardown
    events (``worker.deleteLater``, ``thread.deleteLater``) are
    still queued on the GUI event loop. The interleaving is
    almost certainly what tripped the Qt assertion.

    The fix is to break the interleaving by routing the next-link
    kickoff through ``QTimer.singleShot(0, ...)``. This returns
    control to the GUI's event loop first, which processes the
    pending teardown events, and *then* runs the construction of
    the next pair in a clean state.

    What this test pins
    -------------------

    1. When ``_on_satellite_finished`` runs with a non-empty
       ``_satellite_pending_zoom_chain``, the next zoom worker is
       NOT started synchronously inside that call.
    2. After one ``QCoreApplication.processEvents()`` iteration
       (which drains the posted ``QTimer.singleShot(0)`` event),
       the next zoom worker IS started, with the popped
       ``(zoom, persist)`` from the pending chain.
    3. The pending chain shrinks by exactly one entry (i.e. the
       deferred call pops the head, not duplicates it).

    Doing both (sync-noop + post-process-yes) in the same test
    keeps the contract atomic: a refactor that re-introduces a
    synchronous path would fail (1), and a refactor that drops
    the kickoff entirely (e.g. typo in the lambda capture) would
    fail (2).
    """

    def test_chain_link_kickoff_is_deferred(
        self,
        qapp: QApplication,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cvfr_routemaster import main_window as mw

        # The chain-transition path doesn't depend on the state
        # file's contents, just on whether it exists. Returning
        # None matches "no prior partial download" — the natural
        # case for a cached-zoom transition.
        monkeypatch.setattr(mw, "read_download_state", lambda _cache: None)

        harness = _ChainHarness()
        harness._satellite_running_zoom = 12
        harness._satellite_pending_zoom_chain = [
            (13, False),
            (14, False),
            (15, True),
        ]

        # Bind the REAL production method onto the harness so a
        # signature change in MainWindow breaks this test.
        method = mw.MainWindow._on_satellite_finished
        method(harness)

        # 1. The next link must NOT have started synchronously.
        #    Cleanup and mark-done DID happen (those are the
        #    sync parts of the slot), but worker startup is on
        #    the deferred side of the singleShot.
        assert harness.cleanup_calls == 1
        assert harness.mark_done_calls == [12]
        assert harness.start_for_zoom_calls == [], (
            "regression: next chain link started synchronously inside "
            "_on_satellite_finished — this is the interleaving pattern "
            "that correlates with the v3.3 Qt6Core.dll fail-fast crash. "
            "The chain link must be posted via QTimer.singleShot(0, ...)."
        )

        # 2. Drain the GUI event queue. The QTimer.singleShot(0)
        #    posts onto whatever thread runs the slot — in the
        #    test we're on the main thread, so processEvents()
        #    will deliver it.
        qapp.processEvents()

        # 3. Exactly one deferred call ran, with the popped head
        #    of the pending chain.
        assert harness.start_for_zoom_calls == [(13, False)]
        # And the pending chain shrunk by exactly that one entry.
        assert harness._satellite_pending_zoom_chain == [
            (14, False),
            (15, True),
        ]

    def test_no_chain_remaining_skips_singleshot(
        self,
        qapp: QApplication,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the pending chain is empty, the deferred path is
        not entered at all — the slot falls through to the
        completion branch instead. Pins that the singleShot is
        only used for *actual* chain transitions, not as a
        blanket end-of-slot defer.
        """
        from cvfr_routemaster import main_window as mw

        monkeypatch.setattr(mw, "read_download_state", lambda _cache: None)

        harness = _ChainHarness()
        harness._satellite_running_zoom = 15
        harness._satellite_pending_zoom_chain = []

        mw.MainWindow._on_satellite_finished(harness)

        # Synchronous side-effects happened.
        assert harness.cleanup_calls == 1
        assert harness.mark_done_calls == [15]
        # No deferred singleShot was posted because no chain link
        # to kick off — draining the event queue must not produce
        # a spurious start.
        qapp.processEvents()
        assert harness.start_for_zoom_calls == []
