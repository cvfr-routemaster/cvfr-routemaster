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

"""Tests for Vertical Mode (portrait layout) and for persistence of the
Hide Waypoint View / Hide Usage Hints / Vertical Mode toggles."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtGui import QAction  # noqa: E402

from cvfr_routemaster import settings_store  # noqa: E402
from tests.test_ui_layout import (  # noqa: E402, F401
    isolated_settings,
    main_window,
    qapp,
)


@contextmanager
def _fresh_window(qapp, tmp_path, monkeypatch):
    """Construct a MainWindow *after* the test has seeded settings, with
    the same teardown as the shared ``main_window`` fixture."""
    from cvfr_routemaster.main_window import MainWindow

    w = MainWindow(tmp_path)
    monkeypatch.setattr(w, "_maybe_autoload_on_start", lambda: None)
    try:
        yield w
    finally:
        w.close()
        w.deleteLater()
        for _ in range(3):
            qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            qapp.processEvents()


# ---------------------------------------------------------------------------
# settings_store round-trips
# ---------------------------------------------------------------------------


def test_layout_toggles_default_off(isolated_settings) -> None:
    assert settings_store.load_hide_waypoint_view() is False
    assert settings_store.load_hide_usage_hints() is False
    assert settings_store.load_vertical_mode() is False
    assert settings_store.load_vertical_splitter_states() is None


def test_layout_toggles_round_trip(isolated_settings) -> None:
    settings_store.save_hide_waypoint_view(True)
    settings_store.save_hide_usage_hints(True)
    settings_store.save_vertical_mode(True)
    assert settings_store.load_hide_waypoint_view() is True
    assert settings_store.load_hide_usage_hints() is True
    assert settings_store.load_vertical_mode() is True


def test_vertical_splitter_states_round_trip(isolated_settings) -> None:
    main = b"main-\x00\x01\xff"
    bottom = b"bottom-\x00\x02"
    settings_store.save_vertical_splitter_states(main_state=main, bottom_state=bottom)
    assert settings_store.load_vertical_splitter_states() == (main, bottom)


# ---------------------------------------------------------------------------
# View toggles persist across sessions
# ---------------------------------------------------------------------------


def test_hide_toggles_are_saved_when_pressed(main_window) -> None:
    main_window._act_hide_waypoint_view.setChecked(True)
    main_window._act_hide_usage_hints.setChecked(True)
    assert settings_store.load_hide_waypoint_view() is True
    assert settings_store.load_hide_usage_hints() is True

    main_window._act_hide_waypoint_view.setChecked(False)
    main_window._act_hide_usage_hints.setChecked(False)
    assert settings_store.load_hide_waypoint_view() is False
    assert settings_store.load_hide_usage_hints() is False


def test_persisted_hide_toggles_are_applied_on_launch(
    qapp, tmp_path, isolated_settings, monkeypatch
) -> None:
    settings_store.save_hide_waypoint_view(True)
    settings_store.save_hide_usage_hints(True)
    with _fresh_window(qapp, tmp_path, monkeypatch) as w:
        assert w._act_hide_waypoint_view.isChecked()
        assert w._act_hide_usage_hints.isChecked()
        assert w._waypoint_pane.isVisibleTo(w) is False
        assert w._map_column.isVisibleTo(w)
        for hint in (w._route_panel._hint_label, w._map_hint, w._waypoint_table_hint):
            assert hint.isVisibleTo(hint.parentWidget()) is False


# ---------------------------------------------------------------------------
# Vertical Mode
# ---------------------------------------------------------------------------


def test_vertical_mode_action_is_checkable_and_starts_unpressed(main_window) -> None:
    act = main_window.findChild(QAction, "act_toggle_vertical_mode")
    assert act is not None
    assert act.isCheckable()
    assert act.isChecked() is False
    assert main_window._layout_stack.currentWidget() is main_window._splitter


def test_vertical_mode_puts_map_on_top_and_tables_below(main_window) -> None:
    w = main_window
    w._act_vertical_mode.setChecked(True)

    assert w._layout_stack.currentWidget() is w._vsplitter
    assert w._vsplitter.widget(0) is w._map_column
    assert w._vsplitter.widget(1) is w._bottom_splitter
    assert w._bottom_splitter.widget(0) is w._route_panel
    assert w._bottom_splitter.widget(1) is w._waypoint_pane
    assert w._splitter.count() == 0
    for pane in (w._map_column, w._route_panel, w._waypoint_pane):
        assert pane.isVisibleTo(w)
    assert settings_store.load_vertical_mode() is True


def test_vertical_mode_off_restores_landscape_order(main_window) -> None:
    w = main_window
    w._act_vertical_mode.setChecked(True)
    w._act_vertical_mode.setChecked(False)

    assert w._layout_stack.currentWidget() is w._splitter
    assert [w._splitter.widget(i) for i in range(w._splitter.count())] == [
        w._route_panel,
        w._map_column,
        w._waypoint_pane,
    ]
    assert w._bottom_splitter.count() == 0
    for pane in (w._map_column, w._route_panel, w._waypoint_pane):
        assert pane.isVisibleTo(w)
    assert settings_store.load_vertical_mode() is False


def test_vertical_mode_respects_hide_waypoint_view_and_airplane_mode(
    main_window,
) -> None:
    w = main_window
    w._act_hide_waypoint_view.setChecked(True)
    w._act_vertical_mode.setChecked(True)
    assert w._waypoint_pane.isVisibleTo(w) is False
    assert w._map_column.isVisibleTo(w)

    w._act_airplane_mode.setChecked(True)
    assert w._map_column.isVisibleTo(w) is False
    assert w._route_panel.isVisibleTo(w)

    w._act_airplane_mode.setChecked(False)
    w._act_vertical_mode.setChecked(False)
    assert w._waypoint_pane.isVisibleTo(w) is False
    assert w._map_column.isVisibleTo(w)


def test_vertical_mode_wraps_toolbar_onto_two_rows(main_window) -> None:
    w = main_window
    second_row = w._toolbar_groups_second_row
    assert all(w._toolbar_row_top.indexOf(g) >= 0 for g in second_row)

    w._act_vertical_mode.setChecked(True)
    assert all(w._toolbar_row_bottom.indexOf(g) >= 0 for g in second_row)
    assert all(w._toolbar_row_top.indexOf(g) < 0 for g in second_row)

    w._act_vertical_mode.setChecked(False)
    assert all(w._toolbar_row_top.indexOf(g) >= 0 for g in second_row)
    assert w._toolbar_row_bottom.count() == 1  # only the trailing stretch


def test_persisted_vertical_mode_is_applied_on_launch(
    qapp, tmp_path, isolated_settings, monkeypatch
) -> None:
    settings_store.save_vertical_mode(True)
    with _fresh_window(qapp, tmp_path, monkeypatch) as w:
        assert w._act_vertical_mode.isChecked()
        assert w._layout_stack.currentWidget() is w._vsplitter
        assert w._vsplitter.widget(0) is w._map_column
        assert w._toolbar_row_bottom.indexOf(w._toolbar_groups_second_row[0]) >= 0
