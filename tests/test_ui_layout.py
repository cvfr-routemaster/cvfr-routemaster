"""Tests for the post-restructure UI layer.

Covers four families:

1. ``CalibrationOptionsDialog`` — that the buttons exist with the documented
   action codes and that clicking each one closes the dialog with that code.
2. ``SettingsDialog`` — that the new "Load now" button is present, that its
   validation gates the ``LOAD_NOW`` return code, and that the title now
   reads "Map File Settings".
3. ``MainWindow`` toolbar — that the toolbar surfaces *exactly* the
   nine intended visible actions (Map File Settings, Map Calibration
   Options, Display Settings, Airplane mode, Hide Waypoint View,
   Hide Usage Hints, Show VATSIM traffic, Satellite view, Legal and
   Copyright Info) across the four titled groups, plus the hidden
   Cancel calibration action; no stragglers from the old layout.
   (The legacy "Download Satellite Imagery…" button was removed
   in v3.3+ when the bulk download became unconditional.)
4. Hint-label wording and styling — bright white, ``mapHint`` object name,
   reworded copy, and the waypoint hint placed *below* its table.

Several of these need a real Qt scene tree, so a session-wide ``QApplication``
and an isolated ``QSettings`` factory are mandatory. Without isolation the
tests would scribble onto the user's real ``CVFRRouteMaster`` registry hive.
"""

from __future__ import annotations

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QLabel,
    QPushButton,
    QToolBar,
    QVBoxLayout,
)

from cvfr_routemaster import settings_store  # noqa: E402
from cvfr_routemaster.calibration_options_dialog import (  # noqa: E402
    CalibrationOptionsDialog,
)
from cvfr_routemaster.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """Single ``QApplication`` per test module — Qt forbids more than one
    per process and recreating it between tests is both wasteful and
    occasionally crashes Windows builds."""
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Redirect ``settings_store._settings()`` to a temp INI file so any
    ``save_*`` call from the code under test stays inside the test
    sandbox. Returns the INI path for assertion-side reads."""
    ini_path = tmp_path / "test_settings.ini"

    def _factory() -> QSettings:
        return QSettings(str(ini_path), QSettings.Format.IniFormat)

    monkeypatch.setattr(settings_store, "_settings", _factory)
    return ini_path


# ---------------------------------------------------------------------------
# CalibrationOptionsDialog
# ---------------------------------------------------------------------------


def test_calibration_options_dialog_exposes_six_action_buttons(
    qapp: QApplication,
) -> None:
    """The dialog must surface all six advertised actions, each registered
    against its documented constant. The constants are the public
    contract — labels are free to drift, codes are not."""
    dlg = CalibrationOptionsDialog(None, n_anchors=4)
    expected_codes = {
        CalibrationOptionsDialog.ACTION_REOCR_WAYPOINTS,
        CalibrationOptionsDialog.ACTION_FIT_MAP,
        CalibrationOptionsDialog.ACTION_RESET_LAYOUT,
        CalibrationOptionsDialog.ACTION_CALIBRATE_NORTH,
        CalibrationOptionsDialog.ACTION_CALIBRATE_SOUTH,
        CalibrationOptionsDialog.ACTION_CLEAR_CALIBRATION,
    }
    for code in expected_codes:
        btn = dlg.button_for(code)
        assert btn is not None, f"missing button for action code {code}"
        assert isinstance(btn, QPushButton)
    # Defensive: no extra rogue codes registered (e.g. accidental copy/paste
    # of a button with a placeholder code that shouldn't ship).
    assert set(dlg._action_buttons.keys()) == expected_codes


def test_calibration_options_dialog_action_codes_are_distinct(
    qapp: QApplication,
) -> None:
    """Two buttons sharing a code would silently route to the wrong slot
    and the user would never see why. Pin uniqueness explicitly so a
    refactor that reuses a constant fails here, not in production."""
    codes = [
        CalibrationOptionsDialog.ACTION_REOCR_WAYPOINTS,
        CalibrationOptionsDialog.ACTION_FIT_MAP,
        CalibrationOptionsDialog.ACTION_RESET_LAYOUT,
        CalibrationOptionsDialog.ACTION_CALIBRATE_NORTH,
        CalibrationOptionsDialog.ACTION_CALIBRATE_SOUTH,
        CalibrationOptionsDialog.ACTION_CLEAR_CALIBRATION,
    ]
    assert len(set(codes)) == len(codes)


def test_calibration_options_dialog_action_codes_dont_collide_with_qdialog(
    qapp: QApplication,
) -> None:
    """The action codes must sit clear of ``QDialog.Accepted`` (1) and
    ``Rejected`` (0) so the controller's dispatch can tell a real
    button click from a window-close. Same goes for the
    ``CalibrationInstructionDialog`` constants in the 1001/1002 range —
    keep our codes 1100+ as documented."""
    from cvfr_routemaster.calibration_instruction_dialog import (
        CalibrationInstructionDialog,
    )

    forbidden = {
        int(QDialog.DialogCode.Accepted),
        int(QDialog.DialogCode.Rejected),
        CalibrationInstructionDialog.CALIBRATE_NORTH,
        CalibrationInstructionDialog.CALIBRATE_SOUTH,
        SettingsDialog.LOAD_NOW,
    }
    ours = {
        CalibrationOptionsDialog.ACTION_REOCR_WAYPOINTS,
        CalibrationOptionsDialog.ACTION_FIT_MAP,
        CalibrationOptionsDialog.ACTION_RESET_LAYOUT,
        CalibrationOptionsDialog.ACTION_CALIBRATE_NORTH,
        CalibrationOptionsDialog.ACTION_CALIBRATE_SOUTH,
        CalibrationOptionsDialog.ACTION_CLEAR_CALIBRATION,
    }
    assert ours.isdisjoint(forbidden)


def test_calibration_options_dialog_button_click_emits_action_code(
    qapp: QApplication,
) -> None:
    """Clicking each button must close the dialog with that button's
    action code — not Accepted, not Rejected, not the previous
    button's code."""
    for code in (
        CalibrationOptionsDialog.ACTION_REOCR_WAYPOINTS,
        CalibrationOptionsDialog.ACTION_FIT_MAP,
        CalibrationOptionsDialog.ACTION_RESET_LAYOUT,
        CalibrationOptionsDialog.ACTION_CALIBRATE_NORTH,
        CalibrationOptionsDialog.ACTION_CALIBRATE_SOUTH,
        CalibrationOptionsDialog.ACTION_CLEAR_CALIBRATION,
    ):
        dlg = CalibrationOptionsDialog(None, n_anchors=4)
        btn = dlg.button_for(code)
        assert btn is not None
        # ``QPushButton.click`` synchronously triggers the wired slot,
        # which calls ``self.done(code)`` on the dialog and ends its
        # local event loop with that result. ``dlg.result()`` then
        # carries the integer back here.
        btn.click()
        assert dlg.result() == code


def test_calibration_options_dialog_includes_inline_instructions(
    qapp: QApplication,
) -> None:
    """The whole point of restructuring the help button away was that
    the instructions become *visible* on the dialog rather than
    living behind another click. Sanity-check that the dialog still
    contains a wrapped label whose text mentions the calibration
    workflow keywords."""
    dlg = CalibrationOptionsDialog(None, n_anchors=4)
    labels = dlg.findChildren(QLabel)
    big_text = " ".join(lbl.text() for lbl in labels)
    assert "calibrate" in big_text.lower() or "calibration" in big_text.lower()
    # The inline instructions should mention the Shift+click workflow so
    # we're sure the *full* explanation is present, not just a heading.
    assert "shift+click" in big_text.lower()
    # And the anchor count must surface so the user knows what to expect.
    assert "4" in big_text


# ---------------------------------------------------------------------------
# SettingsDialog
# ---------------------------------------------------------------------------


def test_settings_dialog_window_title_is_map_file_settings(qapp: QApplication) -> None:
    """The dialog is the user-facing "Map File Settings" hub now —
    the previous generic "Settings" title undersells what it does."""
    dlg = SettingsDialog("", "", "", autoload_on_start=False)
    assert dlg.windowTitle() == "Map File Settings"


def test_settings_dialog_has_load_now_button(qapp: QApplication) -> None:
    """The "Load maps & waypoints now" button is the dialog's third
    action (after Ok and Cancel) — verify it exists and carries the
    distinct LOAD_NOW return-code constant in its connected slot.

    We can't easily inspect the connected slot, but we can verify the
    button is reachable via the public attribute and that clicking
    it (with valid paths) ends the dialog with ``LOAD_NOW``."""
    dlg = SettingsDialog("", "", "", autoload_on_start=False)
    assert isinstance(dlg._load_now_btn, QPushButton)
    # Label uses ``&&`` to escape Qt's mnemonic ampersand, so the on-screen
    # text reads "Load maps & waypoints now".
    assert "Load maps" in dlg._load_now_btn.text()
    assert "now" in dlg._load_now_btn.text().lower()


def test_settings_dialog_load_now_validates_paths(
    qapp: QApplication, monkeypatch
) -> None:
    """An invalid path set must NOT close the dialog with LOAD_NOW —
    otherwise the controller would fire a guaranteed-to-fail load on
    nonexistent files. We monkeypatch QMessageBox.warning so the
    validation path runs headless."""
    from PySide6.QtWidgets import QMessageBox

    # Empty paths → validation fails → dialog stays open (we just
    # call ``_accept_validate_and_load`` directly so the test isn't
    # tied to button-click event plumbing).
    warned: list[bool] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args, **_kw: warned.append(True),
    )
    dlg = SettingsDialog("", "", "", autoload_on_start=False)
    dlg._accept_validate_and_load()
    assert warned == [True]
    # ``result()`` defaults to 0 (Rejected) when the dialog hasn't
    # closed — confirms ``done(LOAD_NOW)`` never fired.
    assert dlg.result() != SettingsDialog.LOAD_NOW


def test_settings_dialog_load_now_closes_with_load_now_code(
    qapp: QApplication, tmp_path
) -> None:
    """With three real PDF files on disk, clicking Load Now must close
    the dialog with the ``LOAD_NOW`` code so the controller fires a
    load. The PDF files are touched empty — the dialog only checks
    ``Path.is_file()``, not content."""
    n = tmp_path / "n.pdf"
    s = tmp_path / "s.pdf"
    b = tmp_path / "b.pdf"
    for p in (n, s, b):
        p.write_bytes(b"")

    dlg = SettingsDialog(str(n), str(s), str(b), autoload_on_start=False)
    dlg._accept_validate_and_load()
    assert dlg.result() == SettingsDialog.LOAD_NOW


# ---------------------------------------------------------------------------
# Hint label styling and wording
# ---------------------------------------------------------------------------


def test_route_panel_hint_uses_map_hint_object_name(qapp: QApplication) -> None:
    """The route panel's footer hint must opt into the unified
    ``mapHint`` style so the bright-white / 50%-larger QSS rules from
    ``ui_theme`` apply automatically."""
    from cvfr_routemaster.route_panel import RoutePanel

    panel = RoutePanel()
    assert panel._hint_label.objectName() == "mapHint"


def test_route_panel_hint_text_explains_intermediate_clicks(qapp: QApplication) -> None:
    """The reworded copy must spell out the intermediate-point case
    without the older "empty chart space" phrasing — that wording
    was misleading because the chart is rarely actually empty under
    a click."""
    from cvfr_routemaster.route_panel import RoutePanel

    panel = RoutePanel()
    text = panel._hint_label.text().lower()
    assert "empty chart space" not in text
    assert "published waypoint" in text
    # Mentions the workflow for adding a custom sub-point on a
    # non-waypoint feature (road / coastline / landmark).
    assert "shift+left-click" in text
    assert "polyline sub-point" in text


def test_route_panel_totals_label_color_is_white(qapp: QApplication) -> None:
    """Totals row used to be ``#888`` — promoted to bright white so it
    reads as a primary answer to the planning question, not a
    disabled-style annotation."""
    from cvfr_routemaster.route_panel import RoutePanel

    panel = RoutePanel()
    style = panel._totals_label.styleSheet().lower()
    assert "#ffffff" in style


def test_ui_theme_map_hint_is_bright_white_and_18px(qapp: QApplication) -> None:
    """Pin the QSS contract for ``QLabel#mapHint`` so a future tweak
    can't quietly demote the hints back to a muted grey or shrink
    them past the 50%-larger threshold the user asked for."""
    from cvfr_routemaster import ui_theme

    src = ui_theme.apply_dark_theme.__code__
    # Inspect the literal QSS string by re-running the theme function on
    # the live application — checking the applied stylesheet is the
    # most direct way to verify both colour and size made it through.
    ui_theme.apply_dark_theme(qapp)
    qss = qapp.styleSheet()
    assert "QLabel#mapHint" in qss
    # The selector block must contain both the white colour and the 18 px
    # font size; we look for them in the same window of text.
    block_start = qss.index("QLabel#mapHint")
    block = qss[block_start : block_start + 400]
    assert "#ffffff" in block
    assert "18px" in block
    # Defensive: assert we no longer ship the old 12 px / #b0b0b0 muted
    # style anywhere in the QSS — a stale rule lower in the cascade
    # would silently win.
    assert "color: #b0b0b0" not in qss
    # Keep the variable referenced so linters don't flag it.
    assert src is not None


# ---------------------------------------------------------------------------
# MainWindow toolbar contents
# ---------------------------------------------------------------------------


@pytest.fixture
def main_window(qapp: QApplication, tmp_path, isolated_settings, monkeypatch):
    """Construct a ``MainWindow`` rooted at a clean tmp directory.

    Two important isolations:
    - ``isolated_settings`` redirects ``QSettings`` to a per-test INI so the
      window's ``closeEvent`` doesn't write to the user's real registry.
    - ``_maybe_autoload_on_start`` is no-op'd on the instance so the
      150 ms autoload timer can't kick a load chain mid-test (it's
      gated on ``_sources_set()``, but the shipped
      ``chart_sources.json`` does populate the defaults, so the
      gate is *not* an effective firewall on its own — explicit
      monkeypatching is required).

    Teardown — why the explicit ``DeferredDelete`` drain
    ----------------------------------------------------

    The previous cleanup (``close() + deleteLater() +
    processEvents()`` once) silently leaked the entire
    ``MainWindow`` widget tree across tests. ``deleteLater``
    schedules destruction for the next event-loop iteration,
    but Qt fully tears a tree down only when *every* child's
    own deferred-delete event has been processed too. A
    deep ``MainWindow`` hierarchy (≈30+ direct child widgets,
    plus their grandchildren, plus the ``QComboBox`` popup
    ``QFrame`` instances which are technically top-level
    because popups need their own window) requires several
    drain rounds.

    The leak was directly measured at the time of this fix —
    ``tests/_leak_probe.py`` (a throwaway probe script) showed
    ``len(QApplication.topLevelWidgets())`` growing as
    ``[2, 4, 6, 8, 10]`` across five iterations with the old
    cleanup (one leaked ``MainWindow`` + one leaked ``QFrame``
    per iteration), and a clean ``[0, 0, 0, 0, 0]`` with the
    drain pattern below.

    Symptom that motivated the fix: two tests later in the
    file (``test_airplane_mode_toggle_hides_map_and_waypoint_panes``
    and ``test_hide_usage_hints_survives_airplane_mode_round_trip``)
    appeared to "hang indefinitely" when the full file ran.
    Both tests toggle airplane mode, which calls
    ``_on_airplane_mode_toggled`` →
    ``_reapply_active_font_theme`` →
    ``apply_dark_theme`` →
    ``QApplication.setStyle("Fusion")`` +
    ``setStyleSheet(...)``. Each of those is a global polish
    pass that walks *every* live top-level widget tree.
    Multiply the polish-pass cost by N accumulated
    ``MainWindow`` trees (and their thousands of descendants)
    and you get the multi-minute "hang" the user observed.

    The ``sendPostedEvents(None, QEvent.DeferredDelete)`` call
    explicitly drains the deferred-delete queue without waiting
    for the next idle iteration; ``processEvents()`` then runs
    any side-effect events (e.g. a child's destruction posting
    its own delete) that the drain produced. Three rounds is
    empirically enough for the MainWindow tree's depth; a
    deeper window hierarchy would need more, but the cost of an
    extra round is negligible (a few microseconds when the
    queue is already empty).

    NB: this pattern is safe ONLY because the fixture doesn't
    call ``w.show()``. An attempt to apply the same drain to
    a shown ``MainWindow`` produced an
    ``ACCESS_VIOLATION`` (0xC0000005) — shown widgets retain
    additional native-window state that the forced drain
    tears down out of order. If a future test needs to show
    the window it must use its own teardown path.
    """
    from cvfr_routemaster.main_window import MainWindow

    w = MainWindow(tmp_path)
    monkeypatch.setattr(w, "_maybe_autoload_on_start", lambda: None)
    yield w
    w.close()
    w.deleteLater()
    for _ in range(3):
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()


def _toolbar_button_texts(tb) -> list[str]:
    """Return the visible-button text labels from the main toolbar
    in left-to-right order.

    The toolbar is now structured as four :class:`QFrame` groups
    each containing :class:`QToolButton` instances wired to a
    :class:`QAction` via ``setDefaultAction(...)``, plus one
    free-standing ``Cancel calibration`` button outside the
    groups. We walk every :class:`QToolButton` descendant in
    creation order; that traversal order matches the visual
    left-to-right order because each group's layout populates
    buttons in the same order they're passed to
    :meth:`_make_toolbar_group`, and Qt's ``findChildren`` is
    deterministic in insertion order.

    Filters to *visible* buttons so the hidden cancel-calibration
    entry only shows up while a calibration is in progress.
    """
    from PySide6.QtWidgets import QToolButton

    return [
        btn.defaultAction().text()
        for btn in tb.findChildren(QToolButton)
        if btn.defaultAction() is not None
        and btn.defaultAction().isVisible()
    ]


def test_main_toolbar_visible_actions_are_only_ten(main_window) -> None:
    """The toolbar must surface exactly nine visible commands across
    its four titled groups, in this left-to-right order:

    1. Program Settings group:
       * Map File Settings…
       * Map Calibration Options…
       * Display Settings…
    2. View Toggles group:
       * Airplane mode
       * Hide Waypoint View
       * Hide Usage Hints
       * Show VATSIM traffic
    3. Satellite View Options group:
       * Satellite view
    4. Program Information group:
       * Legal and Copyright Info…

    Anything else means a button got added or the restructure
    missed something.

    Two visible-action removals relative to older versions of
    this app are now baked into this list:

    * "Export waypoints to CSV…" — removed entirely in the
      original toolbar restructure (see :meth:`_build_actions`).
    * "Download Satellite Imagery…" — removed in v3.3+ when the
      bulk satellite-imagery download became unconditional and
      automatic. The replacement flow shows a one-time
      informational notice on first launch and silently
      resumes on subsequent launches; see
      :meth:`MainWindow._show_first_download_notice_and_start`.

    The Legal and Copyright Info button is the v3.3 addition
    (originally labelled "Copyright Information…"; renamed so
    the intended-use / liability framing was obvious at the
    entry point) — pin it here so a future cleanup pass can't
    drop the legal-info surface point by accident.
    """
    tb = main_window.findChild(QToolBar, "mainActionsToolBar")
    assert tb is not None
    assert _toolbar_button_texts(tb) == [
        "Map File Settings…",
        "Map Calibration Options…",
        "Display Settings…",
        "Airplane mode",
        "Hide Waypoint View",
        "Hide Usage Hints",
        "Show VATSIM traffic",
        "Satellite view",
        "Legal and Copyright Info…",
    ]


def test_main_toolbar_includes_hidden_cancel_calibration_action(main_window) -> None:
    """Cancel calibration is part of the toolbar but hidden by default —
    only made visible while a calibration is in progress. Keeping it
    on the toolbar (rather than only inside the calibration overlay)
    means Esc isn't the only discoverable cancel affordance.

    After the toolbar restructure the cancel button lives OUTSIDE
    the four titled groups (so its transient show/hide doesn't
    visually disrupt the groups). It's still findable as a child
    QAction on the main window.
    """
    cancel_action = main_window.findChild(QAction, "act_cancel_calibration")
    assert cancel_action is not None
    assert cancel_action.isVisible() is False


def test_main_toolbar_no_legacy_individual_calibration_actions(main_window) -> None:
    """The previous toolbar exposed Re-OCR, Reset map layout, Calibrate
    north/south, Clear calibration, Calibration instructions, Fit map,
    and Load maps & waypoints as individual actions. After the
    restructure all of those live inside the two sub-dialogs (Map
    File Settings and Map Calibration Options) and must NOT appear
    on the main toolbar any more."""
    tb = main_window.findChild(QToolBar, "mainActionsToolBar")
    assert tb is not None
    forbidden = {
        "Re-OCR waypoints from PDF",
        "Reset map layout",
        "Calibrate north map…",
        "Calibrate south map…",
        "Clear geo calibration…",
        "Calibration instructions…",
        "Fit map",
        "Load maps & waypoints",
    }
    assert not (forbidden & set(_toolbar_button_texts(tb)))


def test_main_toolbar_does_not_include_export_csv_button(main_window) -> None:
    """The "Export waypoints to CSV…" toolbar entry was removed
    when the toolbar was restructured into the (originally three,
    now four) titled groups — the feature lives only in the
    (still-present) slot method for now, with no UI affordance.
    The button must not sneak back via a future refactor; pin its
    absence so that happens loudly.
    """
    tb = main_window.findChild(QToolBar, "mainActionsToolBar")
    assert tb is not None
    assert "Export waypoints to CSV…" not in _toolbar_button_texts(tb)
    # The QAction itself is gone too — it isn't a child of any
    # widget any more.
    csv_action = main_window.findChild(QAction, "act_export_waypoints_csv")
    assert csv_action is None


# ---------------------------------------------------------------------------
# Save / Load flight plan — MainWindow controller integration. Exercises the
# full _on_save_plan_requested / _on_load_plan_requested pipeline with
# QFileDialog monkeypatched out so the tests run headless.
# ---------------------------------------------------------------------------


def _make_test_waypoint(code: str, lat: float, lon: float):
    """Minimal WaypointRecord factory for save/load integration tests."""
    from cvfr_routemaster.waypoint_types import WaypointRecord

    return WaypointRecord(
        index=0,
        code=code,
        name_he="",
        reporting_type="MR",
        lat=lat,
        lon=lon,
        lat_dms="",
        lon_dms="",
    )


def test_save_plan_then_load_plan_round_trips_the_route(
    main_window, tmp_path, monkeypatch
) -> None:
    """End-to-end contract: a route saved through ``_on_save_plan_requested``
    re-loads through ``_on_load_plan_requested`` into a structurally
    identical route on the same waypoint database. This is the whole
    point of the feature — if this property breaks, the user loses work.

    The file goes through real disk IO (``tmp_path``) so any encoding /
    newline / extension-normalisation bug surfaces here rather than
    waiting for a real-world report. QFileDialog is monkeypatched to
    return the predetermined path so the test runs headless.
    """
    # Populate the controller's waypoint export so the load-side lookup
    # can resolve the named-fix tokens we're about to save and re-load.
    llbg = _make_test_waypoint("LLBG", 32.0, 34.88)
    darom = _make_test_waypoint("DAROM", 31.55, 34.55)
    llha = _make_test_waypoint("LLHA", 31.72, 35.0)
    main_window._waypoints_export = [llbg, darom, llha]

    # Build a route with one intermediate so the save-with-intermediates
    # contract is also exercised.
    main_window._route.append_waypoint(llbg)
    main_window._route.append_intermediate(31.55, 34.55)
    main_window._route.append_waypoint(llha)

    save_path = tmp_path / "round-trip.cvfr"
    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getSaveFileName",
        lambda *_a, **_kw: (str(save_path), ""),
    )
    expected_text = "LLBG 3133N03433E LLHA"
    main_window._on_save_plan_requested(expected_text)
    assert save_path.is_file()
    # File contents are exactly the route string + a single trailing LF;
    # no UTF-8 BOM, no CRLF, no extra blank lines.
    raw = save_path.read_bytes()
    assert raw == (expected_text + "\n").encode("utf-8")

    # Now wipe the in-memory route and load the file back.
    main_window._route.clear()
    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getOpenFileName",
        lambda *_a, **_kw: (str(save_path), ""),
    )
    main_window._on_load_plan_requested()

    points = main_window._route.points()
    assert len(points) == 3
    # First and third points are real fixes — waypoint slot populated.
    assert points[0].waypoint is not None and points[0].waypoint.code == "LLBG"
    assert points[2].waypoint is not None and points[2].waypoint.code == "LLHA"
    # Middle point is an intermediate — no waypoint, lat/lon survives the
    # whole-minute round-trip the formatter performs.
    assert points[1].waypoint is None
    assert abs(points[1].lat - 31.55) < 1.0 / 60.0
    assert abs(points[1].lon - 34.55) < 1.0 / 60.0


def test_save_plan_forces_cvfr_extension_when_user_omits_it(
    main_window, tmp_path, monkeypatch
) -> None:
    """A user typing ``myplan`` (no extension) under the .cvfr filter
    should still end up with ``myplan.cvfr`` on disk so the friend-facing
    Load dialog (defaulting to *.cvfr) lists it. Qt's getSaveFileName
    extension handling is platform-dependent — the controller
    normalises explicitly to keep behaviour uniform across Linux,
    Windows, and WSL.
    """
    raw_path = tmp_path / "myplan"
    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getSaveFileName",
        lambda *_a, **_kw: (str(raw_path), ""),
    )
    main_window._on_save_plan_requested("LLBG LLHA")

    # The bare ``myplan`` file does NOT exist; the controller suffixed it.
    assert not raw_path.is_file()
    assert (tmp_path / "myplan.cvfr").is_file()


def test_load_plan_shows_warning_popup_and_leaves_route_intact_on_garbage(
    main_window, tmp_path, monkeypatch
) -> None:
    """Malformed input must not corrupt the live route. Pre-load a known
    route, point the loader at a garbage file, and verify both:
      * a warning popup fired (we monkeypatch QMessageBox.warning),
      * the in-memory route is unchanged after the failed load.

    The malformed-file path is the user's primary safety net — a hand-
    edited plan with a typo, or an old-cycle plan with stale codes,
    should fail loud rather than silently mutate the route.
    """
    from PySide6.QtWidgets import QMessageBox

    llbg = _make_test_waypoint("LLBG", 32.0, 34.88)
    main_window._waypoints_export = [llbg]
    main_window._route.append_waypoint(llbg)
    pre_count = len(main_window._route.points())

    bad_file = tmp_path / "garbage.cvfr"
    bad_file.write_text("totally-not-a-plan", encoding="utf-8")

    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getOpenFileName",
        lambda *_a, **_kw: (str(bad_file), ""),
    )
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, body, *args, **kw: warned.append((title, body)),
    )

    main_window._on_load_plan_requested()

    assert len(warned) == 1, "Expected exactly one warning popup"
    title, body = warned[0]
    assert "flight plan" in title.lower()
    # The user-facing body should quote the parser's "malformed" framing
    # and ideally the offending byte so the user knows where to look.
    assert "malformed" in body.lower()
    # Critical safety property: the live route is untouched.
    assert len(main_window._route.points()) == pre_count
    assert main_window._route.points()[0].waypoint is not None
    assert main_window._route.points()[0].waypoint.code == "LLBG"


def test_load_plan_shows_warning_when_a_code_is_unknown(
    main_window, tmp_path, monkeypatch
) -> None:
    """The file's syntax is valid but a 4/5-letter token can't be
    resolved against the loaded waypoint database. The handler must
    surface this as a specific "unknown code" warning (NOT the generic
    malformed-file message), name the offending code, and leave the
    live route untouched so the user can recover.
    """
    from PySide6.QtWidgets import QMessageBox

    # Only LLBG is in the database; ZZZZZ won't resolve.
    llbg = _make_test_waypoint("LLBG", 32.0, 34.88)
    main_window._waypoints_export = [llbg]

    plan_file = tmp_path / "unknown-code.cvfr"
    plan_file.write_text("LLBG ZZZZZ\n", encoding="utf-8")

    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getOpenFileName",
        lambda *_a, **_kw: (str(plan_file), ""),
    )
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, body, *args, **kw: warned.append((title, body)),
    )

    main_window._on_load_plan_requested()

    assert len(warned) == 1
    body = warned[0][1]
    # Specifies which code is the offender, not a generic message.
    assert "ZZZZZ" in body
    # Atomic load: nothing should have been appended.
    assert main_window._route.is_empty()


def test_load_plan_silently_returns_when_user_cancels_file_dialog(
    main_window, monkeypatch
) -> None:
    """Cancelling the open dialog (Qt returns empty path) is not an error
    state — no popup, no route mutation. A popup here would train the
    user to dismiss "errors" reflexively, which would mask real ones."""
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getOpenFileName",
        lambda *_a, **_kw: ("", ""),
    )
    popped: list[bool] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_a, **_kw: popped.append(True),
    )
    main_window._on_load_plan_requested()
    assert popped == []
    assert main_window._route.is_empty()


def test_main_window_map_hint_describes_modifier_free_navigation(main_window) -> None:
    """The map footer hint now describes plain drag-to-pan and
    wheel-to-zoom — the user dropped the Ctrl modifier so the chart
    can be operated one-handed mid-flight. Alt-based sheet
    adjustments and the sheet-selection click moved into the
    calibration workflow and shouldn't clutter the always-visible
    footer.
    """
    text = main_window._map_hint.text().lower()
    # New copy must explain the two primary navigation gestures.
    assert "drag" in text
    assert "wheel" in text
    assert "zoom" in text
    # The Ctrl modifier was deliberately dropped — make sure the
    # footer doesn't drift back to mentioning it on a copy edit.
    assert "ctrl" not in text
    # Alt-based navigation lives under Calibration Options now.
    assert "alt+drag" not in text
    assert "alt+wheel" not in text
    # Sheet-selection click also lives under the calibration workflow.
    assert "click a sheet" not in text
    # And the calibration "shift+click" mention belongs in the
    # Calibration Options dialog, not in the chart footer.
    assert "shift+click" not in text


# ---------------------------------------------------------------------------
# Map-pane wake-category legend (gated on Show VATSIM traffic toggle)
# ---------------------------------------------------------------------------


def test_map_hint_omits_legend_when_traffic_toggle_is_off(main_window) -> None:
    """Default state: traffic toggle is OFF → the map hint shows
    only the nav primitives, no legend. The user opted out of the
    overlay; cluttering the footer with a legend they can't tie
    to anything visible would be noise."""
    # Sanity: default toggle state is off.
    assert not main_window._act_show_vatsim_traffic.isChecked()
    text = main_window._map_hint.text().lower()
    # Nav line still present.
    assert "drag" in text
    # Legend keywords must NOT appear.
    assert "light" not in text
    assert "medium" not in text
    assert "heavy" not in text
    assert "super" not in text
    assert "no flight plan" not in text
    # And no inline colour spans either.
    assert 'style="color:' not in main_window._map_hint.text()


def test_map_hint_adds_legend_when_traffic_toggle_is_on(main_window) -> None:
    """Toggling Show VATSIM traffic on must add the wake-category
    legend (Light / Medium / Heavy / Super / No flight plan) as a
    second line in the map hint, with each entry coloured to match
    the on-chart silhouette palette via inline ``<span
    style="color:...">`` markup."""
    main_window._act_show_vatsim_traffic.setChecked(True)
    text = main_window._map_hint.text()
    # Nav line still present.
    assert "drag" in text.lower()
    # All five legend categories present.
    assert "Light" in text
    assert "Medium" in text
    assert "Heavy" in text
    assert "Super" in text
    assert "No flight plan" in text
    # Inline coloured markers.
    assert 'style="color:' in text
    # Reset state for following tests in the same fixture-share.
    main_window._act_show_vatsim_traffic.setChecked(False)


def test_map_hint_legend_disappears_when_traffic_toggle_off(main_window) -> None:
    """Turning the toggle off after it was on must REMOVE the
    legend entirely, not just hide it. The label's text must
    revert to nav-only so even if the user un-hides the hints
    later, the legend doesn't ghost back in."""
    main_window._act_show_vatsim_traffic.setChecked(True)
    assert "Light" in main_window._map_hint.text()
    main_window._act_show_vatsim_traffic.setChecked(False)
    text = main_window._map_hint.text()
    assert "Light" not in text
    assert "Medium" not in text
    assert "Super" not in text


def test_map_hint_legend_uses_wake_color_palette(main_window) -> None:
    """Each legend entry's inline colour must come from the
    canonical :data:`cvfr_routemaster.traffic_overlay.WAKE_COLOR`
    palette so the legend swatches and the on-chart silhouettes
    stay in sync. Pin every category by hex to catch a palette
    edit that forgets to update one side."""
    from cvfr_routemaster.traffic_overlay import WAKE_COLOR
    from cvfr_routemaster.vatsim_feed import WAKE_UNKNOWN

    main_window._act_show_vatsim_traffic.setChecked(True)
    text = main_window._map_hint.text()
    for wake in ("L", "M", "H", "J", WAKE_UNKNOWN):
        color_hex = WAKE_COLOR[wake].name()  # ``#rrggbb``
        assert color_hex in text, f"missing colour {color_hex} for wake {wake}"
    main_window._act_show_vatsim_traffic.setChecked(False)


def test_map_hint_uses_rich_text_format(main_window) -> None:
    """The legend uses inline HTML (``<span style="color:...">``),
    so the ``QLabel`` must be in rich-text mode. ``AutoText``
    would also render it correctly, but pinning ``RichText`` is
    explicit about our intent and avoids surprises if a future
    Qt heuristic change misclassifies our content."""
    assert main_window._map_hint.textFormat() == Qt.TextFormat.RichText


def test_waypoint_table_hint_sits_below_table(main_window) -> None:
    """The waypoint pane's hint label moved from above the table to
    below — matches the left and centre panes' "controls/data above,
    explanation below" rhythm. Verify by walking the right pane's
    layout and confirming the hint widget appears *after* the table
    in the QVBoxLayout's child order."""
    hint = main_window._waypoint_table_hint
    table = main_window._table
    # Walk up: the hint and table share a common parent layout (right pane VBox).
    parent = hint.parentWidget()
    assert parent is not None
    assert parent is table.parentWidget()
    layout: QVBoxLayout = parent.layout()  # type: ignore[assignment]
    assert isinstance(layout, QVBoxLayout)

    # Find the indices of the table-containing layout/widget and the hint.
    hint_index: int | None = None
    table_index: int | None = None
    for i in range(layout.count()):
        item = layout.itemAt(i)
        # Hint is added with addWidget → ``item.widget()`` is the label.
        if item.widget() is hint:
            hint_index = i
        # The table is wrapped in an HBox via addLayout — recurse one level
        # to find it.
        sub_layout = item.layout()
        if sub_layout is not None:
            for j in range(sub_layout.count()):
                if sub_layout.itemAt(j).widget() is table:
                    table_index = i
                    break
    assert hint_index is not None, "hint label not found in right pane layout"
    assert table_index is not None, "table not found in right pane layout"
    assert hint_index > table_index, (
        f"waypoint hint should appear after the table "
        f"(hint at {hint_index}, table at {table_index})"
    )


def test_waypoint_table_hint_text_uses_maintains_current_zoom_level(
    main_window,
) -> None:
    """Wording change requested: "same zoom" → "maintains current zoom
    level" so the user knows the chart won't snap to a different
    scale when they click a Hebrew name."""
    text = main_window._waypoint_table_hint.text().lower()
    assert "maintains current zoom level" in text
    assert "(same zoom)" not in text


def test_waypoint_table_hint_uses_map_hint_object_name(main_window) -> None:
    """Same styling contract as the other two pane hints — bright white,
    18 px — driven by the ``mapHint`` object name."""
    assert main_window._waypoint_table_hint.objectName() == "mapHint"


# ---------------------------------------------------------------------------
# Airplane mode
# ---------------------------------------------------------------------------


def test_airplane_mode_action_is_checkable_and_starts_unpressed(
    main_window,
) -> None:
    """The airplane-mode toggle must be checkable so the toolbar
    renders it pressed/unpressed (the user explicitly asked for the
    button to "look pressed when we are in this mode"). It must
    also start unchecked on every launch — airplane mode is a
    viewing-mode toggle, not a persistent preference; persisting it
    would mean a fresh launch could surprise the user with the
    chart missing.
    """
    act = main_window.findChild(QAction, "act_toggle_airplane_mode")
    assert act is not None
    assert act.isCheckable()
    assert act.isChecked() is False


def test_airplane_mode_toggle_hides_map_and_waypoint_panes(main_window) -> None:
    """Toggling airplane mode on must hide both the map column and
    the waypoint pane, leaving the route panel as the only visible
    splitter child. Toggling it back off must restore both."""
    # Start: everything visible.
    assert main_window._map_column.isVisibleTo(main_window)
    assert main_window._waypoint_pane.isVisibleTo(main_window)
    assert main_window._route_panel.isVisibleTo(main_window)

    main_window._act_airplane_mode.setChecked(True)
    # In airplane mode: map + waypoint hidden, route panel still visible.
    assert main_window._map_column.isVisibleTo(main_window) is False
    assert main_window._waypoint_pane.isVisibleTo(main_window) is False
    assert main_window._route_panel.isVisibleTo(main_window)

    main_window._act_airplane_mode.setChecked(False)
    # Back to normal layout.
    assert main_window._map_column.isVisibleTo(main_window)
    assert main_window._waypoint_pane.isVisibleTo(main_window)
    assert main_window._route_panel.isVisibleTo(main_window)


def test_airplane_mode_hides_route_panels_clear_route_button(main_window) -> None:
    """Inside the route panel, the Clear-route button must vanish in
    airplane mode (the user explicitly asked for this — no
    accidental wipes mid-flight) and reappear when airplane mode
    is toggled off."""
    panel = main_window._route_panel
    # Start: clear-route button is part of the panel (may be enabled or
    # disabled depending on whether a route has been built — visibility
    # is the contract that matters here, not enabled-state).
    assert panel._clear_route_btn.isVisibleTo(panel)

    main_window._act_airplane_mode.setChecked(True)
    assert panel._clear_route_btn.isVisibleTo(panel) is False
    assert panel.is_airplane_mode() is True

    main_window._act_airplane_mode.setChecked(False)
    assert panel._clear_route_btn.isVisibleTo(panel)
    assert panel.is_airplane_mode() is False


def test_airplane_mode_hides_route_panel_footer_hint(main_window) -> None:
    """The route panel's multi-paragraph "how to build a route" hint
    is pure noise once the chart is hidden — the pilot can't act on
    Shift-click instructions when there's no chart to click. Verify
    it hides on toggle-on and reappears on toggle-off."""
    panel = main_window._route_panel
    hint = panel._hint_label
    assert hint.isVisibleTo(panel)

    main_window._act_airplane_mode.setChecked(True)
    assert hint.isVisibleTo(panel) is False

    main_window._act_airplane_mode.setChecked(False)
    assert hint.isVisibleTo(panel)


def test_airplane_mode_stretches_route_table_to_fill_window(main_window) -> None:
    """The route table is normally pinned to its natural content width
    (with a trailing stretch widget eating the slack in the pane) so
    its rightmost column never floats off against an over-wide
    splitter slot. Airplane mode collapses the map + waypoint pane
    and the user explicitly asked for the table to *fill* the
    resulting full-window width AND for each column to auto-resize
    when content (e.g. a longer intermediate label like ``DAROM.1``
    or a Ctrl+wheel-bumped table font) needs more room. So the
    panel must:

      * Lift the ``setMaximumWidth`` natural-width pin (Qt uses
        ``QWIDGETSIZE_MAX`` as the "no cap" sentinel — ``16777215``).
      * Keep every column on ``ResizeToContents`` initially so each
        section reports its natural width.
      * NOT enable ``setStretchLastSection`` — that would dump all
        the leftover viewport width onto a single trailing column
        (Time), which read as visually jarring when the font was
        small. The current design uses
        :meth:`_redistribute_airplane_column_widths` (called on a
        ``QTimer.singleShot(0)`` after the toggle) to spread the
        leftover across every visible column proportionally, which
        keeps the visual balance.

    Toggling airplane mode off must reverse both: drop the
    stretch-last flag (cleared belt-and-braces during entry too) and
    re-apply the natural-width pin so the trailing-stretch widget
    owns the slack again.
    """
    from PySide6.QtWidgets import QHeaderView

    panel = main_window._route_panel
    header = panel._table.horizontalHeader()

    # Baseline: content-sized, no stretch-last, and a real
    # (non-MAX) cap pinning the table to its content width.
    assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
    assert header.stretchLastSection() is False
    pre_cap = panel._table.maximumWidth()
    assert pre_cap < 16777215, (
        f"baseline max width should be a content-derived cap, "
        f"got {pre_cap} (== QWIDGETSIZE_MAX)"
    )

    main_window._act_airplane_mode.setChecked(True)
    # The max-width cap is gone (sentinel value) so the table is
    # free to span the viewport. The stretch-last-section flag
    # stays OFF — proportional redistribution replaces it.
    assert panel._table.maximumWidth() == 16777215
    assert header.stretchLastSection() is False

    main_window._act_airplane_mode.setChecked(False)
    assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
    assert header.stretchLastSection() is False
    # After leaving airplane mode the natural-width pin is restored.
    # We don't assert the exact pixel value (it's font-and-dpi
    # dependent and Qt may have re-laid the table out before the
    # caller can sample it) — what matters is that the cap is back
    # to a finite, content-derived value rather than the sentinel.
    assert panel._table.maximumWidth() < 16777215


def test_airplane_mode_redistributes_column_widths_proportionally(
    main_window,
) -> None:
    """Airplane mode must spread leftover viewport width across
    every visible column in proportion to its content width — not
    pile it all onto the last column (which is what
    ``setStretchLastSection(True)`` would do, and what an earlier
    implementation actually did before the user flagged it as
    "jarring when the font is small").

    Direct invocation strategy
    --------------------------

    The redistribution is normally scheduled via
    ``QTimer.singleShot(0, ...)`` so Qt can finish the layout pass
    before we measure the viewport. In a unit-test setting we
    can't reliably pump that timer without ``processEvents`` (and
    that opens up flakiness with offscreen platform plugins), so
    we instead force a synthetic viewport width and call the
    private redistribution method directly. The contract under
    test is the *distribution math*, not the scheduling — those
    are exercised separately by the event-filter test.
    """
    from PySide6.QtWidgets import QHeaderView

    panel = main_window._route_panel
    header = panel._table.horizontalHeader()
    main_window._act_airplane_mode.setChecked(True)

    # Force a known viewport size so the math is deterministic.
    # The route panel sits inside a QSplitter on a hidden main
    # window, so the actual viewport width varies between CI
    # machines; resizing the table directly is the simplest way
    # to nail down the leftover budget.
    panel._table.resize(1200, 400)
    panel._table.viewport().resize(1180, 380)

    panel._redistribute_airplane_column_widths()

    # After redistribution the header must be in Interactive mode
    # (the only mode where ``resizeSection`` widths stick) and the
    # sum of visible-section widths must equal the viewport width
    # exactly (with the rounding remainder absorbed by the last
    # visible section so the right edge lands flush).
    assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.Interactive
    visible_widths = [
        header.sectionSize(i)
        for i in range(header.count())
        if not panel._table.isColumnHidden(i)
    ]
    total = sum(visible_widths)
    assert total == panel._table.viewport().width()
    # No single column should hold more than ~half of the
    # viewport — i.e. the slack is genuinely *distributed*, not
    # piled on a single column. ``Reporting`` is the widest
    # content-driven column for typical headers but should never
    # eat more than 50% of the viewport in the empty-route case.
    assert max(visible_widths) < total / 2, (
        "expected proportional distribution, but one column "
        f"holds {max(visible_widths)} of {total} px"
    )


def test_airplane_mode_idempotent_toggle_is_a_noop(main_window) -> None:
    """``RoutePanel.set_airplane_mode`` must be idempotent — calling
    it twice with the same value mustn't fire spurious visibility
    changes (and the controller's ``_on_airplane_mode_toggled``
    is only ever invoked with state changes by Qt, but a test that
    pokes the panel directly should still see consistent state)."""
    panel = main_window._route_panel
    panel.set_airplane_mode(True)
    assert panel.is_airplane_mode() is True
    panel.set_airplane_mode(True)  # no-op
    assert panel.is_airplane_mode() is True
    panel.set_airplane_mode(False)
    assert panel.is_airplane_mode() is False
    panel.set_airplane_mode(False)  # no-op
    assert panel.is_airplane_mode() is False


# ---------------------------------------------------------------------------
# Hide Waypoint View
# ---------------------------------------------------------------------------


def test_hide_waypoint_view_action_is_checkable_and_starts_unpressed(
    main_window,
) -> None:
    """The Hide Waypoint View toggle must be checkable and start
    unchecked on every launch — like Airplane mode it's a viewing
    state, not a persistent preference, so a fresh start always
    shows the waypoint pane.
    """
    act = main_window.findChild(QAction, "act_toggle_hide_waypoint_view")
    assert act is not None
    assert act.isCheckable()
    assert act.isChecked() is False


def test_hide_waypoint_view_hides_only_waypoint_pane(main_window) -> None:
    """Toggling Hide Waypoint View on must hide the waypoint pane but
    leave the map column and route panel visible — distinct from
    Airplane mode which hides both."""
    assert main_window._map_column.isVisibleTo(main_window)
    assert main_window._waypoint_pane.isVisibleTo(main_window)
    assert main_window._route_panel.isVisibleTo(main_window)

    main_window._act_hide_waypoint_view.setChecked(True)
    assert main_window._waypoint_pane.isVisibleTo(main_window) is False
    assert main_window._map_column.isVisibleTo(main_window)
    assert main_window._route_panel.isVisibleTo(main_window)

    main_window._act_hide_waypoint_view.setChecked(False)
    assert main_window._waypoint_pane.isVisibleTo(main_window)
    assert main_window._map_column.isVisibleTo(main_window)
    assert main_window._route_panel.isVisibleTo(main_window)


def test_hide_waypoint_view_survives_airplane_mode_round_trip(main_window) -> None:
    """If the user presses Hide Waypoint View, then enters Airplane
    mode (which hides the pane unconditionally), then leaves
    Airplane mode — the pane must stay hidden, not snap back to
    visible. Without the explicit re-apply in
    ``_on_airplane_mode_toggled`` the pane would be re-shown by the
    airplane-mode toggle off."""
    main_window._act_hide_waypoint_view.setChecked(True)
    assert main_window._waypoint_pane.isVisibleTo(main_window) is False

    main_window._act_airplane_mode.setChecked(True)
    assert main_window._waypoint_pane.isVisibleTo(main_window) is False

    main_window._act_airplane_mode.setChecked(False)
    # Hide Waypoint View is still pressed — the pane must remain hidden.
    assert main_window._waypoint_pane.isVisibleTo(main_window) is False

    main_window._act_hide_waypoint_view.setChecked(False)
    assert main_window._waypoint_pane.isVisibleTo(main_window)


# ---------------------------------------------------------------------------
# Hide Usage Hints
# ---------------------------------------------------------------------------


def test_hide_usage_hints_action_is_checkable_and_starts_unpressed(
    main_window,
) -> None:
    """The Hide Usage Hints toggle must be checkable and start
    unchecked on every launch so the hints are visible by default
    for first-time / occasional users."""
    act = main_window.findChild(QAction, "act_toggle_hide_usage_hints")
    assert act is not None
    assert act.isCheckable()
    assert act.isChecked() is False


def test_hide_usage_hints_hides_all_three_hint_labels(main_window) -> None:
    """Toggling Hide Usage Hints on must hide every ``QLabel#mapHint``
    footer: the route panel's instruction block, the map column's
    drag/zoom primer, and the waypoint pane's click-affordance
    description. Toggling off must bring all three back.
    """
    route_hint = main_window._route_panel._hint_label
    map_hint = main_window._map_hint
    wp_hint = main_window._waypoint_table_hint
    for h in (route_hint, map_hint, wp_hint):
        assert h.isVisibleTo(h.parentWidget())

    main_window._act_hide_usage_hints.setChecked(True)
    assert route_hint.isVisibleTo(route_hint.parentWidget()) is False
    assert map_hint.isVisibleTo(map_hint.parentWidget()) is False
    assert wp_hint.isVisibleTo(wp_hint.parentWidget()) is False

    main_window._act_hide_usage_hints.setChecked(False)
    for h in (route_hint, map_hint, wp_hint):
        assert h.isVisibleTo(h.parentWidget())


def test_hide_usage_hints_survives_airplane_mode_round_trip(main_window) -> None:
    """Airplane mode independently hides the route panel hint. If the
    user has Hide Usage Hints pressed when they leave Airplane mode,
    the route hint must remain hidden — not flicker back to visible
    just because airplane-mode toggled it off."""
    route_hint = main_window._route_panel._hint_label

    main_window._act_hide_usage_hints.setChecked(True)
    assert route_hint.isVisibleTo(route_hint.parentWidget()) is False

    main_window._act_airplane_mode.setChecked(True)
    # Airplane mode also hides the route hint, so still hidden.
    assert route_hint.isVisibleTo(route_hint.parentWidget()) is False

    main_window._act_airplane_mode.setChecked(False)
    # Hide Usage Hints is still pressed — must remain hidden.
    assert route_hint.isVisibleTo(route_hint.parentWidget()) is False

    main_window._act_hide_usage_hints.setChecked(False)
    assert route_hint.isVisibleTo(route_hint.parentWidget())
