"""Smoke tests for ``RoutePanel`` — focused on the user-visible, controller-
facing pieces that pure ``Route`` tests can't exercise: button-enabled state,
origin-only table row rendering, signal emission via the Clear button.

These tests need a ``QApplication`` because ``QWidget`` instantiation requires
one. We use a module-level fixture so the cost (a few ms per session) is paid
once even if pytest-qt isn't installed — keeping the test suite portable for
contributors who don't want the GUI test stack pulled in.
"""

from __future__ import annotations

import pytest

# Import lazily inside fixtures so a missing PySide6 (CI subset) just skips
# instead of crashing collection.
PySide6 = pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cvfr_routemaster.route import Route  # noqa: E402
from cvfr_routemaster.route_panel import (  # noqa: E402
    _ATC_VISIBILITY_COLS,
    _COL_ALT,
    _COL_CTR,
    _COL_DIST,
    _COL_FREQ,
    _COL_MAG_BRG,
    _COL_NEW_CTR,
    _COL_NEW_FREQ,
    _COL_TIME,
    _CTR_TEXT_COLOR,
    _FREQUENCY_REGEX,
    _NEW_CTR_TEXT_COLOR,
    _OVERRIDABLE_COLS,
    _OVERRIDE_COLOR,
    _OVERRIDE_SUFFIX,
    _ROLE_HAS_OVERRIDE,
    _OverridableCellDelegate,
    _parse_override,
    RoutePanel,
)
from cvfr_routemaster.waypoint_types import WaypointRecord  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """One ``QApplication`` for every test in this file. Qt forbids more
    than one per process, so reusing the same instance is mandatory; the
    module scope keeps the cost bounded even with many small tests."""
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


def _wp(code: str, lat: float, lon: float, *, idx: int = 0, name_he: str = "") -> WaypointRecord:
    return WaypointRecord(
        index=idx,
        code=code,
        name_he=name_he,
        reporting_type="MR",
        lat=lat,
        lon=lon,
        lat_dms="",
        lon_dms="",
    )


def test_clear_button_is_disabled_for_empty_route(qapp: QApplication) -> None:
    """No points → nothing to clear → button disabled. The button text /
    tooltip live alongside the action so a hovered user sees the
    irreversibility warning even before the dialog."""
    panel = RoutePanel()
    panel.set_route(Route())
    assert panel._clear_route_btn.isEnabled() is False


def test_clear_button_is_enabled_when_origin_only(qapp: QApplication) -> None:
    """A single origin point is enough to enable Clear — the button is
    explicitly the way to walk back from a placed origin without picking
    a second waypoint first."""
    route = Route()
    route.append_waypoint(_wp("LLHZ", 32.18, 34.83))
    panel = RoutePanel()
    panel.set_route(route)
    assert panel._clear_route_btn.isEnabled() is True


def test_clear_button_is_enabled_for_multi_point_route(qapp: QApplication) -> None:
    """Sanity: the normal multi-leg case must of course be clearable."""
    route = Route()
    route.append_waypoint(_wp("LLHZ", 32.18, 34.83))
    route.append_waypoint(_wp("BAZRA", 32.205, 34.886))
    panel = RoutePanel()
    panel.set_route(route)
    assert panel._clear_route_btn.isEnabled() is True


# ---------------------------------------------------------------------------
# Save / Load flight plan buttons — wired into the route panel's title row
# next to the Clear button. Order is contractual: Save | Load | Clear with
# Clear rightmost as a misclick-safety against the destructive action.
# ---------------------------------------------------------------------------


def test_save_load_clear_buttons_all_exist_on_the_panel(qapp: QApplication) -> None:
    """Sanity: the three live-route action buttons are all wired up on the
    panel as Python attributes the controller / tests can reach. A missing
    attribute would mean the buttons were defined as locals and the
    enabled-state updates in ``set_route`` would silently no-op."""
    panel = RoutePanel()
    assert isinstance(panel._save_plan_btn, PySide6.QtWidgets.QPushButton)
    assert isinstance(panel._load_plan_btn, PySide6.QtWidgets.QPushButton)
    assert isinstance(panel._clear_route_btn, PySide6.QtWidgets.QPushButton)


def test_save_load_clear_button_text_labels_are_user_visible_words(
    qapp: QApplication,
) -> None:
    """Buttons need actual labels the pilot can read at a glance — pinning
    the text catches an accidental ``setText("")`` or a translation typo
    that would turn them into mystery click-targets in airplane mode."""
    panel = RoutePanel()
    assert panel._save_plan_btn.text() == "Save plan"
    assert panel._load_plan_btn.text() == "Load plan"
    assert panel._clear_route_btn.text() == "Clear route"


def test_save_load_clear_buttons_appear_in_order_save_load_clear_rightmost(
    qapp: QApplication,
) -> None:
    """Misclick safety: Clear must be rightmost so a user reaching for Save
    or Load can't slip and wipe their route. Pinning the visual *order*
    (not just the existence of all three) catches a refactor that moves
    Clear to the middle of the cluster."""
    panel = RoutePanel()
    layout = panel._title_row
    indices = {
        panel._save_plan_btn: layout.indexOf(panel._save_plan_btn),
        panel._load_plan_btn: layout.indexOf(panel._load_plan_btn),
        panel._clear_route_btn: layout.indexOf(panel._clear_route_btn),
    }
    # Every button is actually placed in the title-row layout (no orphans).
    assert all(idx >= 0 for idx in indices.values()), (
        f"At least one button isn't in the title row layout: {indices}"
    )
    # Save is leftmost of the three; Clear is rightmost.
    assert (
        indices[panel._save_plan_btn]
        < indices[panel._load_plan_btn]
        < indices[panel._clear_route_btn]
    ), (
        f"Buttons not in Save | Load | Clear order: {indices}. "
        f"Clear must be rightmost so a save-reaching misclick can't "
        f"wipe the route."
    )


def test_save_plan_button_is_disabled_for_empty_route(qapp: QApplication) -> None:
    """Saving an empty route would write the panel's empty-state hint
    string (or nothing) to disk — neither useful, both confusing on the
    reload side. Same disabled-when-empty rule as the Clear button."""
    panel = RoutePanel()
    panel.set_route(Route())
    assert panel._save_plan_btn.isEnabled() is False


def test_save_plan_button_is_enabled_when_route_has_any_point(qapp: QApplication) -> None:
    """The mirror of Clear's "single origin enables me" rule: a single
    placed origin is a savable state (e.g. the user wants to checkpoint
    "started planning here" before adding the rest of the route)."""
    route = Route()
    route.append_waypoint(_wp("LLHZ", 32.18, 34.83))
    panel = RoutePanel()
    panel.set_route(route)
    assert panel._save_plan_btn.isEnabled() is True


def test_load_plan_button_is_always_enabled(qapp: QApplication) -> None:
    """Load is the way you populate an empty route — disabling it on
    empty-state would create an unrecoverable "can't load until you
    add a point" loop. Pin enabled=True for both empty and non-empty
    so a future "improvement" can't silently break this."""
    panel_empty = RoutePanel()
    panel_empty.set_route(Route())
    assert panel_empty._load_plan_btn.isEnabled() is True

    route = Route()
    route.append_waypoint(_wp("LLHZ", 32.18, 34.83))
    panel_full = RoutePanel()
    panel_full.set_route(route)
    assert panel_full._load_plan_btn.isEnabled() is True


def test_save_plan_button_click_emits_save_plan_requested_with_route_string(
    qapp: QApplication,
) -> None:
    """The panel pre-composes the ICAO Field 15 string and passes it through
    the signal — the controller receives a ready-to-write payload and
    doesn't have to re-derive it from the route model. Pin the exact
    string so a "I'll just call ``str(route)``" regression can't substitute
    a different format."""
    captured: list[str] = []

    route = Route()
    route.append_waypoint(_wp("LLBG", 32.0, 34.88))
    route.append_waypoint(_wp("DAROM", 31.55, 34.55))

    panel = RoutePanel()
    panel.set_route(route)
    panel.save_plan_requested.connect(captured.append)

    panel._save_plan_btn.click()
    assert captured == ["LLBG DAROM"]


def test_save_plan_button_click_always_includes_intermediates_in_payload(
    qapp: QApplication,
) -> None:
    """Even when the user has un-checked "Include intermediate points"
    (display preference), the *saved* file must contain every intermediate
    coord — otherwise polyline detail is silently lost on round-trip.
    The display checkbox is a view setting, not a save setting."""
    route = Route()
    route.append_waypoint(_wp("LLBG", 32.0, 34.88))
    route.append_intermediate(31.55, 34.55)
    route.append_waypoint(_wp("LLHA", 31.72, 35.0))

    panel = RoutePanel()
    # Force the display checkbox off, simulating a user who hides
    # intermediates above the table.
    panel._include_intermediates_chk.setChecked(False)
    panel.set_route(route)

    captured: list[str] = []
    panel.save_plan_requested.connect(captured.append)
    panel._save_plan_btn.click()

    assert len(captured) == 1
    # The intermediate coord token must appear in the payload even though
    # the user's display has it hidden.
    assert "3133N03433E" in captured[0]
    assert captured[0].split() == ["LLBG", "3133N03433E", "LLHA"]


def test_load_plan_button_click_emits_load_plan_requested(qapp: QApplication) -> None:
    """The Load button doesn't compose anything — it's a pure intent
    signal handed to the controller, which then runs the file dialog +
    parse choreography. Pinning the bare emission ensures the wiring
    exists; the per-file parse / error / resolve logic is covered by
    test_flight_plan.py and the controller-level integration tests."""
    fired: list[bool] = []
    panel = RoutePanel()
    panel.load_plan_requested.connect(lambda: fired.append(True))
    panel._load_plan_btn.click()
    assert fired == [True]


def test_origin_only_renders_single_table_row_with_from_filled(qapp: QApplication) -> None:
    """The origin-only state shows one row whose FROM cell carries the
    origin's display label (so the user can confirm which fix they
    actually clicked) and every other column is blank — including
    Reporting and Type, which only describe a leg's *destination*."""
    route = Route()
    route.append_waypoint(_wp("LLHZ", 32.18, 34.83, name_he="הרצליה"))
    panel = RoutePanel()
    panel.set_route(route)

    assert panel._model.rowCount() == 1
    # Column 0 = From → "LLHZ"
    assert panel._model.item(0, 0).text() == "LLHZ"
    # Every other column must be empty. Indexing matches _ROUTE_TABLE_COLS.
    for col in range(1, panel._model.columnCount()):
        assert panel._model.item(0, col).text() == "", (
            f"column {col} should be empty in origin-only mode"
        )


def test_origin_only_row_disappears_once_a_second_waypoint_is_added(qapp: QApplication) -> None:
    """The placeholder row is strictly an empty-route affordance. With a
    real leg, the table flips to the standard one-row-per-segment
    rendering and the placeholder must not linger."""
    route = Route()
    route.append_waypoint(_wp("LLHZ", 32.18, 34.83))
    panel = RoutePanel()
    panel.set_route(route)
    assert panel._model.rowCount() == 1
    assert panel._model.item(0, 1).text() == ""  # TO empty (origin-only)

    route.append_waypoint(_wp("BAZRA", 32.205, 34.886))
    panel.set_route(route)
    assert panel._model.rowCount() == 1
    # Now TO is populated (BAZRA), confirming the row is segment-derived,
    # not the placeholder.
    assert panel._model.item(0, 1).text() == "BAZRA"


def test_clear_route_signal_fires_when_user_confirms(qapp: QApplication, monkeypatch) -> None:
    """The Clear button's click handler must surface a confirmation
    dialog before signalling the controller. We monkeypatch the
    ``QMessageBox.exec`` implementation to return ``Yes`` so the test
    runs headless without a real modal dialog."""
    from PySide6.QtWidgets import QMessageBox

    route = Route()
    route.append_waypoint(_wp("LLHZ", 32.18, 34.83))
    panel = RoutePanel()
    panel.set_route(route)

    received: list[bool] = []
    panel.clear_route_requested.connect(lambda: received.append(True))

    monkeypatch.setattr(
        QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Yes,
    )
    panel._on_clear_route_clicked()

    assert received == [True]


def test_clear_route_signal_suppressed_when_user_cancels(qapp: QApplication, monkeypatch) -> None:
    """A No-pressed confirmation must not emit the request — the button
    is the only safety net before destructive route loss, so the
    contract on the No path is "absolutely no signal"."""
    from PySide6.QtWidgets import QMessageBox

    route = Route()
    route.append_waypoint(_wp("LLHZ", 32.18, 34.83))
    panel = RoutePanel()
    panel.set_route(route)

    received: list[bool] = []
    panel.clear_route_requested.connect(lambda: received.append(True))

    monkeypatch.setattr(
        QMessageBox, "exec", lambda self: QMessageBox.StandardButton.No,
    )
    panel._on_clear_route_clicked()

    assert received == []


# ---------------------------------------------------------------------------
# ATC handoff columns (CTR / Freq / New CTR / New Freq)
# ---------------------------------------------------------------------------


def _two_leg_route() -> Route:
    """Build a small two-segment route used by the ATC-column tests.

    Three real waypoints (LLHZ → BAZRA → DEROR) so we have two legs and
    can exercise per-row persistence under re-renders. The exact lat/lon
    don't matter for these tests — we never compute geometry against
    them — but they need to be plausible so ``Route.segments()`` can
    derive bearings/distances without warnings."""
    route = Route()
    route.append_waypoint(_wp("LLHZ", 32.18, 34.83))
    route.append_waypoint(_wp("BAZRA", 32.205, 34.886))
    route.append_waypoint(_wp("DEROR", 32.243, 34.927))
    return route


def test_atc_columns_are_present_and_in_correct_order(qapp: QApplication) -> None:
    """The four ATC columns sit between Type (col 3) and MAG BRG (col 8).

    Order matters: CTR → Freq → New CTR → New Freq reads as a left-to-
    right "now → next" pair. If the column indices drift this test
    fails immediately rather than silently rendering data into the
    wrong slot."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    headers = [
        panel._model.headerData(c, Qt.Orientation.Horizontal)
        for c in range(panel._model.columnCount())
    ]
    assert headers[_COL_CTR] == "CTR"
    assert headers[_COL_FREQ] == "Freq"
    assert headers[_COL_NEW_CTR] == "New CTR"
    assert headers[_COL_NEW_FREQ] == "New Freq"


def test_atc_cells_are_editable_and_segment_cells_are_not(qapp: QApplication) -> None:
    """User-input columns must be editable; everything else must NOT be.

    Editability is what gates the double-click / typing UX, and it's
    the cleanest hook for "this column accepts user data" — so we test
    it directly rather than poking at edit triggers."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())

    for col in (_COL_CTR, _COL_FREQ, _COL_NEW_CTR, _COL_NEW_FREQ):
        item = panel._model.item(0, col)
        assert item is not None
        assert item.flags() & Qt.ItemFlag.ItemIsEditable, (
            f"column {col} ({panel._model.headerData(col, Qt.Orientation.Horizontal)}) "
            "should be editable"
        )

    # Spot-check: the To cell (real waypoint, also column 1) must NOT be
    # editable — it's a clickable link, not a text field.
    to_item = panel._model.item(0, 1)
    assert not (to_item.flags() & Qt.ItemFlag.ItemIsEditable)


def test_ctr_columns_use_magenta_and_cyan_foreground(qapp: QApplication) -> None:
    """Visual contract: CTR is magenta, New CTR is cyan. Both colours
    must come through the model so the HTML-table copy preserves them
    on paste into Word."""
    panel = RoutePanel()
    panel.set_route(_two_leg_route())

    ctr_item = panel._model.item(0, _COL_CTR)
    new_ctr_item = panel._model.item(0, _COL_NEW_CTR)

    assert ctr_item.foreground().color().name().lower() == _CTR_TEXT_COLOR.lower()
    assert (
        new_ctr_item.foreground().color().name().lower()
        == _NEW_CTR_TEXT_COLOR.lower()
    )


def test_atc_inputs_persist_across_rerenders(qapp: QApplication) -> None:
    """Typed CTR / Freq values survive a full re-render of the table.

    The render loop rebuilds every row from scratch on speed change,
    route mutation, and external altitude updates — without
    persistence the user would lose every typed value the moment they
    nudge the cruise speed. Keying by ``(from, to)`` is the contract
    the test pins."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    route = _two_leg_route()
    panel.set_route(route)

    panel._model.setData(
        panel._model.index(0, _COL_CTR), "TLV_TWR", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(0, _COL_FREQ), "118.4", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(0, _COL_NEW_CTR), "TLV_APP", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(0, _COL_NEW_FREQ), "120.500", Qt.ItemDataRole.EditRole
    )

    panel.set_route(route)

    assert panel._model.item(0, _COL_CTR).text() == "TLV_TWR"
    assert panel._model.item(0, _COL_FREQ).text() == "118.4"
    assert panel._model.item(0, _COL_NEW_CTR).text() == "TLV_APP"
    assert panel._model.item(0, _COL_NEW_FREQ).text() == "120.500"


def test_atc_inputs_are_keyed_per_leg(qapp: QApplication) -> None:
    """Different legs must hold independent ATC values. Pinning the key
    to ``(from_label, to_label)`` rather than row-index means inserting
    a waypoint earlier in the route doesn't shift everyone's
    frequencies down by one row."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())

    panel._model.setData(
        panel._model.index(0, _COL_FREQ), "118.4", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(1, _COL_FREQ), "120.500", Qt.ItemDataRole.EditRole
    )

    assert panel._model.item(0, _COL_FREQ).text() == "118.4"
    assert panel._model.item(1, _COL_FREQ).text() == "120.500"


def test_frequency_regex_accepts_short_and_long_decimal(qapp: QApplication) -> None:
    """Sanity-check the strict regex used by the freq delegate. ``118.4``
    and ``118.475`` are both legal aviation comm channels; anything
    else (letters, four decimals, two-digit prefix, missing dot, etc.)
    is rejected."""
    import re

    rx = re.compile(_FREQUENCY_REGEX)
    assert rx.match("118.4")
    assert rx.match("118.475")
    assert rx.match("120.500")
    assert rx.match("999.9")

    assert not rx.match("118")
    assert not rx.match("118.")
    assert not rx.match("118.4567")
    assert not rx.match("18.45")
    assert not rx.match("abc.123")
    assert not rx.match("118,4")
    assert not rx.match(" 118.4 ")


def test_frequency_delegate_rejects_malformed_commit(qapp: QApplication) -> None:
    """The delegate's ``setModelData`` must drop a malformed string
    instead of writing it. We can't easily fake a real Qt edit
    flow without ``pytest-qt``, but we can drive ``setModelData``
    directly with a stubbed ``QLineEdit``-like object."""
    from PySide6.QtCore import QModelIndex, Qt
    from PySide6.QtWidgets import QLineEdit

    from cvfr_routemaster.route_panel import _FrequencyCellDelegate

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    delegate = _FrequencyCellDelegate(panel._table)
    index: QModelIndex = panel._model.index(0, _COL_FREQ)

    panel._model.setData(index, "118.4", Qt.ItemDataRole.EditRole)
    assert panel._model.item(0, _COL_FREQ).text() == "118.4"

    editor = QLineEdit()
    editor.setText("garbage")
    delegate.setModelData(editor, panel._model, index)

    assert panel._model.item(0, _COL_FREQ).text() == "118.4"

    editor.setText("")
    delegate.setModelData(editor, panel._model, index)
    assert panel._model.item(0, _COL_FREQ).text() == ""


def test_html_table_copy_includes_headers_and_ctr_colours(qapp: QApplication) -> None:
    """The HTML payload produced by Ctrl+C must contain the column
    headers, every selected cell's text, and the CTR colour rules
    (so a Word paste preserves the magenta/cyan styling)."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    panel._model.setData(
        panel._model.index(0, _COL_CTR), "TLV_TWR", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(0, _COL_NEW_CTR), "TLV_APP", Qt.ItemDataRole.EditRole
    )

    rows = list(range(panel._model.rowCount()))
    cols = list(range(panel._model.columnCount()))
    html = panel._render_selection_as_html(rows, cols)

    assert "<table" in html and "</table>" in html
    assert "<th" in html and "<td" in html
    assert "CTR" in html and "Freq" in html and "New CTR" in html and "New Freq" in html
    assert "MAG BRG" in html and "Alt (ft)" in html
    assert "TLV_TWR" in html and "TLV_APP" in html
    assert _CTR_TEXT_COLOR.lower() in html.lower()
    assert _NEW_CTR_TEXT_COLOR.lower() in html.lower()


def test_plain_text_copy_is_tab_separated(qapp: QApplication) -> None:
    """Plain-text fallback uses TSV — terminals and code editors that
    strip HTML should still see a parseable table."""
    panel = RoutePanel()
    panel.set_route(_two_leg_route())

    rows = list(range(panel._model.rowCount()))
    cols = list(range(panel._model.columnCount()))
    plain = panel._render_selection_as_plain(rows, cols)

    lines = plain.splitlines()
    assert len(lines) == panel._model.rowCount() + 1  # header + per-row
    header_cols = lines[0].split("\t")
    assert header_cols[_COL_CTR] == "CTR"
    assert header_cols[_COL_FREQ] == "Freq"
    assert header_cols[_COL_NEW_CTR] == "New CTR"
    assert header_cols[_COL_NEW_FREQ] == "New Freq"


# ---------------------------------------------------------------------------
# Cell-value overrides (MAG BRG / Alt / Dist)
# ---------------------------------------------------------------------------
#
# These tests pin the contract added alongside the override flow:
# * the three columns are editable + decorated with a custom delegate;
# * a stored override repaints the cell red with an asterisk suffix;
# * a Dist override propagates into the leg's Time and the totals row;
# * overrides survive a full re-render (same persistence guarantee as
#   the ATC inputs they live next to);
# * right-click restore (per-cell + per-column) drops overrides cleanly;
# * malformed input is rejected at the delegate without smuggling
#   garbage into the model.


def test_overridable_columns_are_editable(qapp: QApplication) -> None:
    """MAG BRG / Alt / Dist must be editable so the override flow can
    open a delegate editor on them; the Time column stays read-only
    because it's derived from Dist + cruise speed (changing it
    directly would lie about either the distance or the speed)."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    for col in (_COL_MAG_BRG, _COL_ALT, _COL_DIST):
        item = panel._model.item(0, col)
        assert item is not None
        assert item.flags() & Qt.ItemFlag.ItemIsEditable, (
            f"column {col} should be editable for the override flow"
        )
    time_item = panel._model.item(0, _COL_TIME)
    assert not (time_item.flags() & Qt.ItemFlag.ItemIsEditable), (
        "Time is computed from Dist + cruise speed and must not be "
        "directly editable"
    )


def test_unknown_altitude_cell_is_editable_so_user_can_supply_one(
    qapp: QApplication,
) -> None:
    """A leg where the matcher returned no altitude shows ``"unknown"``,
    but the user must still be able to override it — that's a primary
    use case (chart-printed altitude the matcher missed, or an
    ATC-assigned altitude). The cell stays editable in either state."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    route = _two_leg_route()
    panel.set_route(route, altitudes_per_segment=[(), (1500,)])
    cell0 = panel._model.item(0, _COL_ALT)
    cell1 = panel._model.item(1, _COL_ALT)
    assert cell0.text() == "unknown"
    assert cell1.text() == "1500"
    assert cell0.flags() & Qt.ItemFlag.ItemIsEditable
    assert cell1.flags() & Qt.ItemFlag.ItemIsEditable


def test_mag_brg_override_renders_red_with_asterisk(qapp: QApplication) -> None:
    """A typed MAG BRG override repaints the cell red, suffixes the
    value with ``"*"`` so an asterisked print preserves the marker
    on a colourless flight log, and tags the cell with
    ``_ROLE_HAS_OVERRIDE`` so the right-click menu can find it."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    panel._model.setData(
        panel._model.index(0, _COL_MAG_BRG), "120", Qt.ItemDataRole.EditRole
    )
    cell = panel._model.item(0, _COL_MAG_BRG)
    assert cell.text() == f"120°M{_OVERRIDE_SUFFIX}"
    assert cell.foreground().color().name().lower() == _OVERRIDE_COLOR.lower()
    assert cell.data(_ROLE_HAS_OVERRIDE) is True


def test_alt_override_renders_red_with_asterisk(qapp: QApplication) -> None:
    """Single-value altitude override displays the same way as the
    other override columns: red text, asterisk suffix, override tag."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route(), altitudes_per_segment=[(1500,), (1500,)])
    panel._model.setData(
        panel._model.index(0, _COL_ALT), "2500", Qt.ItemDataRole.EditRole
    )
    cell = panel._model.item(0, _COL_ALT)
    assert cell.text() == f"2500{_OVERRIDE_SUFFIX}"
    assert cell.foreground().color().name().lower() == _OVERRIDE_COLOR.lower()
    assert cell.data(_ROLE_HAS_OVERRIDE) is True


def test_alt_override_supports_multi_value_stack(qapp: QApplication) -> None:
    """A comma-separated stack ``"1600,800"`` renders one value per
    line, asterisked per line — preserves the chart's stacked-altitude
    visual convention even for hand-entered stacks."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    panel._model.setData(
        panel._model.index(0, _COL_ALT), "1600,800", Qt.ItemDataRole.EditRole
    )
    cell = panel._model.item(0, _COL_ALT)
    expected = f"1600{_OVERRIDE_SUFFIX}\n800{_OVERRIDE_SUFFIX}"
    assert cell.text() == expected
    assert cell.foreground().color().name().lower() == _OVERRIDE_COLOR.lower()


def test_dist_override_renders_red_with_asterisk(qapp: QApplication) -> None:
    """Dist override displays as ``"<value>*"`` (with the leading
    whitespace the right-aligned numeric format preserves) in red."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    panel._model.setData(
        panel._model.index(0, _COL_DIST), "12.3", Qt.ItemDataRole.EditRole
    )
    cell = panel._model.item(0, _COL_DIST)
    assert cell.text().endswith(f"12.3{_OVERRIDE_SUFFIX}")
    assert cell.foreground().color().name().lower() == _OVERRIDE_COLOR.lower()


def test_dist_override_recomputes_time_for_segment(qapp: QApplication) -> None:
    """The Time cell on a row with an overridden Dist must reflect the
    new distance / cruise speed, not the original great-circle value.
    This is the main reason Dist is overridable — pilots want time
    math that adds up to whatever distance they planned for."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    speed = panel.cruise_speed_kts()  # 90 by default
    panel._model.setData(
        panel._model.index(0, _COL_DIST), "9.0", Qt.ItemDataRole.EditRole
    )
    # 9 nm / 90 kt = 0.1 h = 6 min = 360 s → "00:06:00"
    expected_secs = 9.0 / speed * 3600.0
    expected_hms_minute = int(expected_secs // 60)
    time_text = panel._model.item(0, _COL_TIME).text()
    # Use a minute-of-time check rather than exact string — gives the
    # rendering a tiny bit of rounding latitude without losing the
    # contract that "Dist override drives Time recompute".
    assert f":{expected_hms_minute:02d}:" in time_text, (
        f"Time cell '{time_text}' should reflect 9 nm at {int(speed)} kt "
        f"(~{expected_hms_minute} min), not the original great-circle time"
    )


def test_dist_override_changes_totals_line(qapp: QApplication) -> None:
    """The 'Total: X nm at Y kt' line above the table must add up to
    the same numbers the user sees in the table cells — otherwise an
    overridden 12.3* leg silently de-overrides itself in the total."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    pre = panel._totals_label.text()
    panel._model.setData(
        panel._model.index(0, _COL_DIST), "100.0", Qt.ItemDataRole.EditRole
    )
    post = panel._totals_label.text()
    assert pre != post, "totals line should refresh after a Dist override"
    assert "100" in post or "1" in post.split("·")[0], (
        f"totals line '{post}' should reflect the overridden Dist of 100 nm"
    )


def test_overrides_persist_across_full_rerender(qapp: QApplication) -> None:
    """A typed override survives a ``set_route`` re-render — same
    contract as the ATC inputs. Otherwise nudging the cruise speed
    or recomputing altitudes would silently wipe overrides."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    route = _two_leg_route()
    panel.set_route(route)
    panel._model.setData(
        panel._model.index(0, _COL_MAG_BRG), "200", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(0, _COL_ALT), "2500", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(0, _COL_DIST), "15.0", Qt.ItemDataRole.EditRole
    )

    panel.set_route(route)

    assert panel._model.item(0, _COL_MAG_BRG).text() == f"200°M{_OVERRIDE_SUFFIX}"
    assert panel._model.item(0, _COL_ALT).text() == f"2500{_OVERRIDE_SUFFIX}"
    assert panel._model.item(0, _COL_DIST).text().endswith(f"15.0{_OVERRIDE_SUFFIX}")
    # Sanity: the override tag survives too, so the right-click menu
    # still works after a re-render.
    for col in (_COL_MAG_BRG, _COL_ALT, _COL_DIST):
        assert panel._model.item(0, col).data(_ROLE_HAS_OVERRIDE) is True


def test_overrides_are_keyed_per_leg(qapp: QApplication) -> None:
    """Different legs must hold independent overrides, same as the
    ATC-input persistence keying. Pinning by ``(from, to)`` rather
    than row index means inserting a waypoint earlier doesn't shift
    everyone's overrides down by one row."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    panel._model.setData(
        panel._model.index(0, _COL_MAG_BRG), "100", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(1, _COL_MAG_BRG), "200", Qt.ItemDataRole.EditRole
    )
    assert panel._model.item(0, _COL_MAG_BRG).text() == f"100°M{_OVERRIDE_SUFFIX}"
    assert panel._model.item(1, _COL_MAG_BRG).text() == f"200°M{_OVERRIDE_SUFFIX}"


def test_restore_cell_override_drops_red_asterisk(qapp: QApplication) -> None:
    """The per-cell restore handler clears the override and re-renders
    the cell with the computed value — no asterisk, no red, no
    override tag."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    panel._model.setData(
        panel._model.index(0, _COL_MAG_BRG), "200", Qt.ItemDataRole.EditRole
    )
    assert panel._model.item(0, _COL_MAG_BRG).data(_ROLE_HAS_OVERRIDE) is True

    panel._restore_cell_override(0, _COL_MAG_BRG)

    cell = panel._model.item(0, _COL_MAG_BRG)
    assert _OVERRIDE_SUFFIX not in cell.text()
    assert not cell.data(_ROLE_HAS_OVERRIDE)


def test_restore_all_column_overrides_clears_every_override_in_column(
    qapp: QApplication,
) -> None:
    """The header restore handler walks every leg in the route and drops
    its override for the targeted column. Other columns' overrides on
    the same row stay untouched (column-scoped, not row-scoped)."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    panel._model.setData(
        panel._model.index(0, _COL_MAG_BRG), "100", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(1, _COL_MAG_BRG), "200", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(0, _COL_ALT), "1500", Qt.ItemDataRole.EditRole
    )

    panel._restore_all_overrides_in_column(_COL_MAG_BRG)

    assert _OVERRIDE_SUFFIX not in panel._model.item(0, _COL_MAG_BRG).text()
    assert _OVERRIDE_SUFFIX not in panel._model.item(1, _COL_MAG_BRG).text()
    # Alt override on row 0 must survive — this is a column-scoped
    # restore, not a row-scoped one.
    assert panel._model.item(0, _COL_ALT).text() == f"1500{_OVERRIDE_SUFFIX}"


def test_column_has_any_override_only_true_for_columns_with_overrides(
    qapp: QApplication,
) -> None:
    """``_column_has_any_override`` is the gate the header context menu
    uses to enable / disable "Restore all <col> values". It must
    flip on the first override and back off after a full restore."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    assert panel._column_has_any_override(_COL_MAG_BRG) is False

    panel._model.setData(
        panel._model.index(0, _COL_MAG_BRG), "100", Qt.ItemDataRole.EditRole
    )
    assert panel._column_has_any_override(_COL_MAG_BRG) is True

    panel._restore_all_overrides_in_column(_COL_MAG_BRG)
    assert panel._column_has_any_override(_COL_MAG_BRG) is False
    # Non-overridable columns always report False — the header menu
    # uses this same gate to decide whether to show its menu at all.
    assert panel._column_has_any_override(_COL_CTR) is False


def test_overridable_delegate_strips_unit_and_asterisk_for_editor(
    qapp: QApplication,
) -> None:
    """Opening an editor on an overridden ``046°M*`` MAG BRG cell must
    pre-populate the editor with the bare ``"046"`` so the user
    re-edits with one keystroke instead of having to manually delete
    the cosmetic ``"°M*"`` first. Same contract for Dist (``"*"``)
    and Alt (``"*"`` plus newline-to-comma collapse)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLineEdit

    panel = RoutePanel()
    panel.set_route(_two_leg_route())

    panel._model.setData(
        panel._model.index(0, _COL_MAG_BRG), "100", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(0, _COL_ALT), "1600,800", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(0, _COL_DIST), "12.5", Qt.ItemDataRole.EditRole
    )

    delegate_brg = _OverridableCellDelegate(_COL_MAG_BRG)
    delegate_alt = _OverridableCellDelegate(_COL_ALT)
    delegate_dist = _OverridableCellDelegate(_COL_DIST)

    editor = QLineEdit()
    delegate_brg.setEditorData(editor, panel._model.index(0, _COL_MAG_BRG))
    assert editor.text() == "100"
    delegate_alt.setEditorData(editor, panel._model.index(0, _COL_ALT))
    assert editor.text() == "1600,800"
    delegate_dist.setEditorData(editor, panel._model.index(0, _COL_DIST))
    assert editor.text() == "12.5"


def test_overridable_delegate_silently_drops_malformed_commit(
    qapp: QApplication,
) -> None:
    """Garbage typed into a MAG BRG / Alt / Dist editor must not
    overwrite the cell — the delegate's ``setModelData`` re-validates
    via ``_parse_override`` and silently no-ops on failure (mirrors
    the frequency-delegate contract)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLineEdit

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    panel._model.setData(
        panel._model.index(0, _COL_MAG_BRG), "100", Qt.ItemDataRole.EditRole
    )
    delegate = _OverridableCellDelegate(_COL_MAG_BRG)
    editor = QLineEdit()
    editor.setText("garbage")
    delegate.setModelData(editor, panel._model, panel._model.index(0, _COL_MAG_BRG))
    # The pre-existing override must still be in place.
    assert panel._model.item(0, _COL_MAG_BRG).text() == f"100°M{_OVERRIDE_SUFFIX}"

    # Out-of-range MAG BRG (361) is also rejected — the regex would
    # accept three digits but the value-range check in
    # ``_parse_override`` rejects > 360.
    editor.setText("361")
    delegate.setModelData(editor, panel._model, panel._model.index(0, _COL_MAG_BRG))
    assert panel._model.item(0, _COL_MAG_BRG).text() == f"100°M{_OVERRIDE_SUFFIX}"


def test_overridable_delegate_empty_commit_clears_override(
    qapp: QApplication,
) -> None:
    """Committing an empty string from the editor (delete-everything-
    then-Enter) routes through the data-changed handler's
    "remove the override" branch, which restores the computed
    value — same effect as the right-click restore action, just
    via the keyboard."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLineEdit

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    panel._model.setData(
        panel._model.index(0, _COL_MAG_BRG), "100", Qt.ItemDataRole.EditRole
    )
    assert panel._model.item(0, _COL_MAG_BRG).data(_ROLE_HAS_OVERRIDE) is True

    delegate = _OverridableCellDelegate(_COL_MAG_BRG)
    editor = QLineEdit()
    editor.setText("")
    delegate.setModelData(editor, panel._model, panel._model.index(0, _COL_MAG_BRG))

    cell = panel._model.item(0, _COL_MAG_BRG)
    assert _OVERRIDE_SUFFIX not in cell.text()
    assert not cell.data(_ROLE_HAS_OVERRIDE)


def test_parse_override_canonicalises_inputs(qapp: QApplication) -> None:
    """Parser contract: equivalent typed forms collapse to one canonical
    storage string. ``"46"`` and ``"046"`` both store as ``"046"``;
    ``"12"`` and ``"12.0"`` both store as ``"12.0"``;
    ``"1600,  800"`` and ``"1600,800"`` both store as
    ``"1600,800"``. Without canonicalisation a leg could accumulate
    two equivalent overrides under different keys."""
    assert _parse_override(_COL_MAG_BRG, "46") == ("046", 46)
    assert _parse_override(_COL_MAG_BRG, "046") == ("046", 46)
    assert _parse_override(_COL_MAG_BRG, "0") == ("000", 0)
    assert _parse_override(_COL_MAG_BRG, "360") == ("360", 360)
    # Out-of-range and malformed → None.
    assert _parse_override(_COL_MAG_BRG, "361") is None
    assert _parse_override(_COL_MAG_BRG, "garbage") is None
    assert _parse_override(_COL_MAG_BRG, "") is None

    assert _parse_override(_COL_DIST, "12") == ("12.0", 12.0)
    assert _parse_override(_COL_DIST, "12.0") == ("12.0", 12.0)
    assert _parse_override(_COL_DIST, "12.34") == ("12.3", 12.34)
    assert _parse_override(_COL_DIST, "0") is None  # zero-length leg rejected
    assert _parse_override(_COL_DIST, "abc") is None

    assert _parse_override(_COL_ALT, "1500") == ("1500", (1500,))
    assert _parse_override(_COL_ALT, "1600,800") == ("1600,800", (1600, 800))
    assert _parse_override(_COL_ALT, "1600, 800") == ("1600,800", (1600, 800))
    assert _parse_override(_COL_ALT, "0") is None  # zero altitude rejected
    assert _parse_override(_COL_ALT, "abc") is None


def test_delegate_commit_does_not_warn_about_orphaned_editor(
    qapp: QApplication,
) -> None:
    """Regression: committing through ``_OverridableCellDelegate``
    while a real editor is open must not print Qt's

      ``QAbstractItemView::commitData called with an editor that does
      not belong to this view``

    warning. The original bug was that the data-changed handler
    re-rendered the table *synchronously* (``removeRows`` + re-append),
    which broke the editor↔index mapping while Qt was still mid-commit
    and the editor's subsequent focus-out would re-enter ``commitData``
    with an index that no longer existed. The fix defers the
    re-render via ``QTimer.singleShot(0, ...)`` so the editor is
    closed and removed from the view's internal map before any rows
    move underneath it.

    We capture Qt messages via ``qInstallMessageHandler`` (the only
    supported way to intercept ``qWarning``) and exercise the full
    Qt edit cycle: open an editor on a Dist cell, commit a value,
    pump events to drain the deferred render, then close the
    editor. Any captured message containing the warning substring
    fails the test.
    """
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    from PySide6.QtWidgets import QStyleOptionViewItem

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    panel.show()  # the view needs to be realised for editor focus events

    captured: list[str] = []

    def _capture(_msg_type: QtMsgType, _ctx, message: str) -> None:
        captured.append(message)

    prev = qInstallMessageHandler(_capture)
    try:
        delegate = panel._table.itemDelegateForColumn(_COL_DIST)
        index = panel._model.index(0, _COL_DIST)
        # Drive the *real* edit pipeline: open an editor through the
        # view, type, commit. ``QAbstractItemView.edit`` sets up the
        # editor↔index mapping the warning is sensitive to.
        assert panel._table.edit(
            index,
            panel._table.EditTrigger.AllEditTriggers,
            None,
        )
        editor = panel._table.indexWidget(index) or panel._table.focusWidget()
        # Some Qt versions return the editor via ``QApplication.focusWidget``
        # rather than ``indexWidget`` — both are acceptable; we just need
        # something to feed back through ``commitData``.
        if editor is None:
            # Fall back to creating an editor manually + invoking
            # commitData via the public hook. This still exercises the
            # exact ``setModelData`` → ``dataChanged`` → re-render
            # sequence the bug lived in.
            editor = delegate.createEditor(
                panel._table.viewport(), QStyleOptionViewItem(), index
            )
            delegate.setEditorData(editor, index)
            editor.setText("99.9")
            delegate.setModelData(editor, panel._model, index)
        else:
            editor.setText("99.9")
            panel._table.commitData(editor)
            panel._table.closeEditor(
                editor, delegate.EndEditHint.NoHint
            )

        # Drain the ``QTimer.singleShot(0, ...)`` deferred re-render
        # so the cell really repaints (mirrors what would happen on
        # the user's next event-loop tick after committing).
        qapp.processEvents()
    finally:
        qInstallMessageHandler(prev)
        panel.hide()
        panel.deleteLater()

    offending = [m for m in captured if "does not belong to this view" in m]
    assert not offending, (
        "Delegate commit produced the orphaned-editor warning that the "
        f"deferred-render fix is supposed to suppress: {offending}"
    )


# ---------------------------------------------------------------------------
# Max-route-altitude suffix on the totals line
# ---------------------------------------------------------------------------
#
# The totals line above the table reads
#
#   ``Total: X nm · HH:MM:SS at K kt · Max route alt: Y ft``
#
# (or ``"unknown"`` for the alt slot when no leg has altitude data).
# The suffix has to honour Feature 1 Alt overrides so a user-typed
# ceiling drives the max, and a stacked-altitude leg has to contribute
# its top value rather than a sum or an average.


def test_max_route_alt_renders_in_totals_line_for_known_altitudes(
    qapp: QApplication,
) -> None:
    """Two legs at 1500 and 2500 → the totals line spells out
    ``Max route alt: 2500 ft`` after the cruise-speed clause."""
    panel = RoutePanel()
    panel.set_route(
        _two_leg_route(),
        altitudes_per_segment=[(1500,), (2500,)],
    )
    text = panel._totals_label.text()
    assert "Max route alt: 2500 ft" in text, text
    # Order matters — the suffix sits *after* the ``at N kt`` clause
    # so the totals line still reads "what / how long / how fast / how
    # high" left-to-right.
    assert text.index("at ") < text.index("Max route alt:"), text


def test_max_route_alt_picks_highest_value_across_legs(
    qapp: QApplication,
) -> None:
    """Max is route-wide, not per-leg — a 4500 ft leg sandwiched
    between two 1500 ft legs must still drive the suffix to 4500."""
    panel = RoutePanel()
    panel.set_route(
        _two_leg_route(),
        altitudes_per_segment=[(1500,), (4500,)],
    )
    assert "Max route alt: 4500 ft" in panel._totals_label.text()


def test_max_route_alt_uses_max_within_stacked_altitude_cell(
    qapp: QApplication,
) -> None:
    """A stacked-altitude leg contributes its own *per-leg max* to the
    route-wide max, not its first or last value. Tests the contract
    that ``"1600 over 800"`` means the leg climbs as high as 1600 ft."""
    panel = RoutePanel()
    panel.set_route(
        _two_leg_route(),
        altitudes_per_segment=[(1600, 800), (1500,)],
    )
    assert "Max route alt: 1600 ft" in panel._totals_label.text()


def test_max_route_alt_honours_alt_override(qapp: QApplication) -> None:
    """An Alt override on any leg drives the route-wide max — overrides
    are the user's authoritative answer, so a 5500-ft override on a
    leg the matcher thought was 1500 ft must propagate to the
    totals line."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(
        _two_leg_route(),
        altitudes_per_segment=[(1500,), (1500,)],
    )
    pre = panel._totals_label.text()
    assert "Max route alt: 1500 ft" in pre

    panel._model.setData(
        panel._model.index(0, _COL_ALT), "5500", Qt.ItemDataRole.EditRole
    )
    assert "Max route alt: 5500 ft" in panel._totals_label.text()


def test_max_route_alt_skips_unknown_legs(qapp: QApplication) -> None:
    """An ``unknown`` leg (empty altitude tuple) must be skipped, not
    counted as 0 — the route-wide max takes whatever the *known* legs
    say. Without this guard a single missed-by-matcher leg would
    silently mask a real 4500-ft ceiling on another leg."""
    panel = RoutePanel()
    panel.set_route(
        _two_leg_route(),
        altitudes_per_segment=[(), (4500,)],
    )
    assert "Max route alt: 4500 ft" in panel._totals_label.text()


def test_max_route_alt_renders_unknown_when_no_leg_has_altitude(
    qapp: QApplication,
) -> None:
    """When the matcher returned no altitude for any leg (chart not
    calibrated, all legs missed, or no per-segment list supplied at
    all), the suffix still appears in the totals line but reads
    ``Max route alt: unknown`` so the user can see the field exists
    and what it would mean if calibration were available."""
    panel = RoutePanel()
    # All-empty list — every leg's altitude is unknown.
    panel.set_route(_two_leg_route(), altitudes_per_segment=[(), ()])
    assert "Max route alt: unknown" in panel._totals_label.text()

    # No list supplied at all — same observable outcome.
    panel2 = RoutePanel()
    panel2.set_route(_two_leg_route())
    assert "Max route alt: unknown" in panel2._totals_label.text()


def test_max_route_alt_is_hidden_when_route_has_no_segments(
    qapp: QApplication,
) -> None:
    """The totals label is hidden for the empty / origin-only route
    states, so the max-alt suffix shouldn't be visible (or
    computable, for that matter) when there are no legs to total."""
    panel = RoutePanel()
    panel.set_route(Route())
    assert panel._totals_label.isVisible() is False
    assert panel._effective_max_altitude_ft() is None

    # Origin-only: still no segments → still hidden.
    route = Route()
    route.append_waypoint(_wp("LLHZ", 32.18, 34.83))
    panel.set_route(route)
    assert panel._totals_label.isVisible() is False


def test_max_route_alt_recomputes_after_alt_override_is_restored(
    qapp: QApplication,
) -> None:
    """Restoring an Alt override (cell-level or column-level) must
    recompute the max from the original computed values — otherwise
    a hand-edit that the user cleared would leave a stale max in
    the totals line."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(
        _two_leg_route(),
        altitudes_per_segment=[(1500,), (1500,)],
    )
    panel._model.setData(
        panel._model.index(0, _COL_ALT), "9000", Qt.ItemDataRole.EditRole
    )
    assert "Max route alt: 9000 ft" in panel._totals_label.text()

    panel._restore_cell_override(0, _COL_ALT)
    assert "Max route alt: 1500 ft" in panel._totals_label.text()


# ---------------------------------------------------------------------------
# "Show ATC columns" visibility checkbox
# ---------------------------------------------------------------------------
#
# Compact toggle above the table that hides/shows the four ATC-handoff
# columns (CTR / Freq / New CTR / New Freq) for plotting-style narrow
# views. Critical contracts:
# * checked by default so the briefing-style table renders out-of-the-box;
# * unchecking only affects display — model items, edit flags, delegates,
#   and ``_atc_inputs`` are all untouched, so every typed value reappears
#   on re-check;
# * a render that fires while hidden (e.g. cruise-speed nudge) still
#   repopulates the underlying cells from ``_atc_inputs`` so toggling
#   visibility back on never loses data even after multiple re-renders
#   in the hidden state.


def test_show_atc_checkbox_is_visible_and_checked_by_default(
    qapp: QApplication,
) -> None:
    """Default state: the briefing-style full-width table is what a
    user opening the app for the first time should see, so the
    checkbox starts checked and the four ATC columns are visible."""
    panel = RoutePanel()
    assert panel._show_atc_chk.isChecked() is True
    panel.set_route(_two_leg_route())
    for col in _ATC_VISIBILITY_COLS:
        assert panel._table.isColumnHidden(col) is False, (
            f"column {col} should be visible when the checkbox is checked"
        )


def test_show_atc_checkbox_unchecked_hides_all_four_atc_columns(
    qapp: QApplication,
) -> None:
    """Unchecking collapses the four ATC columns simultaneously
    without affecting any other column (FROM / TO / Reporting / Type
    / MAG BRG / Alt / Dist / Time stay visible)."""
    panel = RoutePanel()
    panel.set_route(_two_leg_route())

    panel._show_atc_chk.setChecked(False)

    for col in _ATC_VISIBILITY_COLS:
        assert panel._table.isColumnHidden(col) is True, (
            f"ATC column {col} should be hidden when the checkbox is unchecked"
        )
    # Spot-check the non-ATC columns: From, MAG BRG, and Time must
    # stay visible — Feature 3 is column-scoped, not row-scoped.
    for col in (0, _COL_MAG_BRG, _COL_TIME):
        assert panel._table.isColumnHidden(col) is False, (
            f"non-ATC column {col} should remain visible after the toggle"
        )


def test_show_atc_checkbox_round_trip_restores_columns(
    qapp: QApplication,
) -> None:
    """Re-checking after an unchecked state must restore exactly the
    same four columns to visible — a noisy round-trip would suggest
    the toggle is mutating model state instead of just display state."""
    panel = RoutePanel()
    panel.set_route(_two_leg_route())

    panel._show_atc_chk.setChecked(False)
    panel._show_atc_chk.setChecked(True)

    for col in _ATC_VISIBILITY_COLS:
        assert panel._table.isColumnHidden(col) is False, (
            f"column {col} should be visible again after re-checking"
        )


def test_atc_values_survive_a_hide_show_cycle(qapp: QApplication) -> None:
    """User-typed ATC values must reappear after a hide → show cycle.
    The toggle is a *display* concern, not a *data* concern — losing
    typed values on hide would silently destroy planning work."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    panel._model.setData(
        panel._model.index(0, _COL_CTR), "TLV_TWR", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(0, _COL_FREQ), "118.4", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(0, _COL_NEW_CTR), "TLV_APP", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(0, _COL_NEW_FREQ), "120.500", Qt.ItemDataRole.EditRole
    )

    panel._show_atc_chk.setChecked(False)
    panel._show_atc_chk.setChecked(True)

    assert panel._model.item(0, _COL_CTR).text() == "TLV_TWR"
    assert panel._model.item(0, _COL_FREQ).text() == "118.4"
    assert panel._model.item(0, _COL_NEW_CTR).text() == "TLV_APP"
    assert panel._model.item(0, _COL_NEW_FREQ).text() == "120.500"


def test_atc_values_survive_rerender_while_hidden(qapp: QApplication) -> None:
    """A re-render that happens *while* the ATC columns are hidden
    (e.g. user nudges the cruise speed with the columns collapsed)
    must still repopulate the model items from ``_atc_inputs`` so
    re-checking later restores everything. Without this guarantee
    the persistence wins from ``_atc_inputs`` would silently regress
    in the hidden state."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    route = _two_leg_route()
    panel.set_route(route)
    panel._model.setData(
        panel._model.index(0, _COL_CTR), "TLV_TWR", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(0, _COL_FREQ), "118.4", Qt.ItemDataRole.EditRole
    )

    panel._show_atc_chk.setChecked(False)
    # Force a full re-render in the hidden state — same code path as a
    # cruise-speed nudge or a route mutation triggers.
    panel.set_route(route)
    panel._show_atc_chk.setChecked(True)

    assert panel._model.item(0, _COL_CTR).text() == "TLV_TWR"
    assert panel._model.item(0, _COL_FREQ).text() == "118.4"


def test_show_atc_checkbox_does_not_change_atc_inputs_dict(
    qapp: QApplication,
) -> None:
    """Defensive check on the toggle's contract: the ``_atc_inputs``
    persistence dict — the source of truth that survives every
    full re-render — must be exactly identical before and after a
    visibility toggle. Hiding a column is *not* the same gesture as
    clearing it, and conflating the two would let a "show me a
    narrower table" flow silently double as a "wipe my CTR
    notes" flow."""
    from PySide6.QtCore import Qt

    panel = RoutePanel()
    panel.set_route(_two_leg_route())
    panel._model.setData(
        panel._model.index(0, _COL_CTR), "TLV_TWR", Qt.ItemDataRole.EditRole
    )
    panel._model.setData(
        panel._model.index(1, _COL_NEW_FREQ), "120.500", Qt.ItemDataRole.EditRole
    )
    snapshot = {k: dict(v) for k, v in panel._atc_inputs.items()}

    panel._show_atc_chk.setChecked(False)
    panel._show_atc_chk.setChecked(True)

    after = {k: dict(v) for k, v in panel._atc_inputs.items()}
    assert after == snapshot


def test_atc_visibility_cols_constant_matches_column_indices(
    qapp: QApplication,
) -> None:
    """Self-defending sanity check: the visibility toggle's column set
    must exactly equal ``{CTR, Freq, New CTR, New Freq}``. The toggle
    handler keys off this constant, so a typo or accidental drift
    (e.g. accidentally hiding ``MAG BRG``) would be caught here."""
    assert set(_ATC_VISIBILITY_COLS) == {
        _COL_CTR,
        _COL_FREQ,
        _COL_NEW_CTR,
        _COL_NEW_FREQ,
    }
    # Order matters too — the handler iterates in this order, and
    # snapshot debugging is friendlier when the order is fixed.
    assert _ATC_VISIBILITY_COLS == (
        _COL_CTR,
        _COL_FREQ,
        _COL_NEW_CTR,
        _COL_NEW_FREQ,
    )


def test_overridable_cols_constant_matches_column_indices(
    qapp: QApplication,
) -> None:
    """Self-defending sanity check: the public ``_OVERRIDABLE_COLS``
    constant must exactly equal ``{MAG BRG, Alt, Dist}`` (no more,
    no less). The override flow logic — render loop, delegates,
    context menus — all key off this set, so a typo or accidental
    addition (e.g. Time becoming overridable) would be caught here
    rather than discovered at runtime."""
    assert _OVERRIDABLE_COLS == frozenset({_COL_MAG_BRG, _COL_ALT, _COL_DIST})
    # Time stays out — it's strictly derived from Dist + cruise speed.
    assert _COL_TIME not in _OVERRIDABLE_COLS
