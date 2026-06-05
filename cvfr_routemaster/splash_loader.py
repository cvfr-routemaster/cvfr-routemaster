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

"""Startup-splash helpers that keep the splash *moving* during the heavy
work of launching the app.

The problem this solves: the slowest part of startup is importing
:mod:`cvfr_routemaster.main_window` (which transitively pulls in PySide6
widgets, PyMuPDF, the satellite/altitude pipelines, …) — in a frozen
PyInstaller build that can be several seconds of synchronous work on the
GUI thread. While that import runs, the GUI thread is blocked, so an
*indeterminate* :class:`QProgressDialog` can't advance its busy marquee
and Windows renders it as a static, misleading "stuck at ~50 %" bar.

The fix is to run the blocking callable on a **worker thread** while the
GUI thread spins a local :class:`QEventLoop`. With the event loop live, a
:class:`QTimer` can drive a *determinate* "creep" on the splash so the user
sees real forward motion, and the splash never freezes mid-animation.

This module deliberately imports only :mod:`PySide6.QtCore` /
:mod:`PySide6.QtWidgets` (already loaded by the time ``QApplication`` exists)
so importing *it* is cheap — it must not drag in the very modules whose slow
import we're trying to mask.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from PySide6.QtCore import QEventLoop, QObject, QThread, QTimer, Signal

T = TypeVar("T")


class _CallableWorker(QObject):
    """Runs a zero-arg callable on its thread and reports the outcome.

    Exceptions are captured and re-emitted (rather than crashing the worker
    thread) so the caller can re-raise them on the GUI thread with a clean
    traceback string."""

    done = Signal(object)
    error = Signal(str)

    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except BaseException as exc:  # noqa: BLE001 — surface, don't swallow
            import traceback

            self.error.emit(
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            )
            return
        self.done.emit(result)


def run_off_thread_while_pumping(
    fn: Callable[[], T], *, wait_ms: int = 30000
) -> T:
    """Run ``fn`` on a worker thread while spinning a local event loop.

    Returns whatever ``fn`` returns. If ``fn`` raises, the exception text is
    re-raised here as a :class:`RuntimeError` on the calling (GUI) thread.

    The local :class:`QEventLoop` keeps GUI timers + paint events flowing
    while ``fn`` executes off-thread — that's what lets a splash animation
    keep ticking during an otherwise-blocking import. ``wait_ms`` bounds the
    post-loop ``QThread.wait`` so a wedged worker can't hang shutdown.
    """
    loop = QEventLoop()
    box: dict[str, object] = {}

    worker = _CallableWorker(fn)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    def _on_done(result: object) -> None:
        box["result"] = result
        loop.quit()

    def _on_error(msg: str) -> None:
        box["error"] = msg
        loop.quit()

    worker.done.connect(_on_done)
    worker.error.connect(_on_error)
    thread.start()
    loop.exec()
    thread.quit()
    thread.wait(wait_ms)
    worker.deleteLater()
    thread.deleteLater()

    if "error" in box:
        raise RuntimeError(f"Background startup task failed:\n{box['error']}")
    return box["result"]  # type: ignore[return-value]


def load_with_creeping_splash(
    splash: object,
    fn: Callable[[], T],
    *,
    creep_interval_ms: int = 60,
    creep_cap: int = 90,
    creep_step: int = 2,
) -> T:
    """Run ``fn`` off-thread, driving ``splash`` as a determinate creep bar.

    ``splash`` is a :class:`QProgressDialog` (or anything exposing
    ``setRange``/``setValue``/``value``). The bar creeps from 1 toward
    ``creep_cap`` while ``fn`` runs, then jumps to 100 on completion — so the
    user always sees motion and never a frozen marquee. The creep is driven
    by a :class:`QTimer` that ticks on the local event loop spun by
    :func:`run_off_thread_while_pumping`.
    """
    try:
        splash.setRange(0, 100)  # type: ignore[attr-defined]
        splash.setValue(1)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - splash is a plain QProgressDialog
        pass

    timer = QTimer()
    timer.setInterval(creep_interval_ms)

    def _tick() -> None:
        try:
            v = int(splash.value())  # type: ignore[attr-defined]
            if v < creep_cap:
                splash.setValue(min(creep_cap, v + creep_step))  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            pass

    timer.timeout.connect(_tick)
    timer.start()
    try:
        result = run_off_thread_while_pumping(fn)
    finally:
        timer.stop()
    # Intentionally leave the bar at the creep cap (not 100): the caller
    # still has to construct the main window, which it reflects by advancing
    # the bar the rest of the way and closing the splash when truly done.
    try:
        splash.setValue(creep_cap)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass
    return result
