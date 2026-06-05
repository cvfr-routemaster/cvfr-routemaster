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

"""The off-thread startup loader that keeps the splash animating."""

from __future__ import annotations

import time

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cvfr_routemaster.splash_loader import (  # noqa: E402
    load_with_creeping_splash,
    run_off_thread_while_pumping,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def test_returns_callable_result(qapp) -> None:
    assert run_off_thread_while_pumping(lambda: 21 * 2) == 42


def test_propagates_exception_as_runtime_error(qapp) -> None:
    def boom():
        raise ValueError("kaboom")

    with pytest.raises(RuntimeError) as ei:
        run_off_thread_while_pumping(boom)
    # The original traceback text is surfaced for diagnosis.
    assert "kaboom" in str(ei.value)


class _FakeSplash:
    """Minimal QProgressDialog stand-in recording value changes."""

    def __init__(self) -> None:
        self._min = 0
        self._max = 0
        self._value = 0
        self.history: list[int] = []

    def setRange(self, lo: int, hi: int) -> None:  # noqa: N802 — Qt API mimic
        self._min, self._max = lo, hi

    def setValue(self, v: int) -> None:  # noqa: N802 — Qt API mimic
        self._value = v
        self.history.append(v)

    def value(self) -> int:
        return self._value

    def maximum(self) -> int:
        return self._max


def test_creep_advances_while_work_runs(qapp) -> None:
    """While the off-thread callable sleeps, the splash bar must creep
    forward (the event loop is live, so the creep QTimer ticks)."""
    splash = _FakeSplash()

    def slow():
        # ``sleep`` releases the GIL, so the GUI thread's creep timer fires.
        time.sleep(0.35)
        return "loaded"

    result = load_with_creeping_splash(
        splash, slow, creep_interval_ms=20, creep_step=3, creep_cap=90
    )
    assert result == "loaded"
    # Determinate range, and the bar moved beyond the initial value.
    assert splash._max == 100
    assert max(splash.history) > 1
    # Never overshoots the cap (caller finishes the last stretch).
    assert max(splash.history) <= 90
