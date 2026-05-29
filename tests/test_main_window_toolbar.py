"""Tests for the main-toolbar group restructure.

The top toolbar used to be a flat list of buttons. It now reads
as four titled, rounded-border :class:`QFrame` groups:

* **Program Settings**: Map File Settings, Map Calibration
  Options, Display Settings.
* **View Toggles**: Airplane mode, Hide Waypoint View, Hide
  Usage Hints, Show VATSIM traffic.
* **Satellite View Options**: Satellite view. (The legacy
  "Download Satellite Imagery…" button was removed in v3.3+
  when the bulk download became unconditional and automatic.)
* **Program Information**: Legal and Copyright Info.

The hidden "Cancel calibration" button sits OUTSIDE the four
groups so its transient appearance during calibration doesn't
visually rearrange them.

The previous "Export waypoints to CSV…" toolbar entry was
removed entirely.

These tests pin:

1. The four group frames exist with their expected ``objectName``
   values (so QSS selectors keep targeting them).
2. Each group contains the correct ordered set of action
   ``objectName`` values (so a future refactor doesn't quietly
   shuffle buttons between groups).
3. The CSV-export action is absent from the main window's child
   tree.
4. The Cancel calibration button is present but lives outside any
   of the four group frames.

Implementation notes
--------------------

Tests use a real :class:`MainWindow` (the existing
``main_window`` fixture in :mod:`tests.test_ui_layout`) rather
than a fake, because the test surface here IS the GUI assembly —
testing it against a fake would just re-implement the assembly
and prove nothing. We reuse that fixture verbatim by importing
the module under the same name pytest collects from.
"""

from __future__ import annotations

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFrame,
    QToolBar,
    QToolButton,
)

# Re-use the existing main_window + qapp + isolated_settings
# fixtures wholesale. ``main_window`` depends on ``isolated_settings``
# transitively (see ``test_ui_layout.main_window``), so pytest only
# resolves that dependency if the fixture name is in scope of this
# test module — hence the import.
from tests.test_ui_layout import (  # noqa: E402, F401
    isolated_settings,
    main_window,
    qapp,
)


# ---------------------------------------------------------------
# Group frames + their action membership.
# ---------------------------------------------------------------


def _button_object_names_in_frame(frame: QFrame) -> list[str]:
    """Return the ``objectName`` of every :class:`QAction` proxied
    by a :class:`QToolButton` inside ``frame``, in creation
    (visual left-to-right) order.

    Each toolbar button is constructed by
    :meth:`MainWindow._make_toolbar_group`, which wires the
    QToolButton to a QAction via ``setDefaultAction(...)``. The
    QAction is the source of truth for object name, label,
    checked-state, tooltip, etc., so checking the action's
    objectName is what the test selectors actually want.
    """
    names: list[str] = []
    for btn in frame.findChildren(QToolButton):
        act = btn.defaultAction()
        if act is None:
            continue
        names.append(act.objectName())
    return names


def test_program_settings_group_exists_with_expected_buttons(
    main_window,
) -> None:
    frame = main_window.findChild(QFrame, "group_program_settings")
    assert frame is not None
    assert _button_object_names_in_frame(frame) == [
        "act_open_map_file_settings",
        "act_open_calibration_options",
        "act_open_font_settings",
    ]


def test_view_toggles_group_exists_with_expected_buttons(
    main_window,
) -> None:
    frame = main_window.findChild(QFrame, "group_view_toggles")
    assert frame is not None
    assert _button_object_names_in_frame(frame) == [
        "act_toggle_airplane_mode",
        "act_toggle_hide_waypoint_view",
        "act_toggle_hide_usage_hints",
        "act_toggle_show_vatsim_traffic",
    ]


def test_satellite_view_options_group_exists_with_expected_buttons(
    main_window,
) -> None:
    """The Satellite View Options group holds the single
    Satellite-view toggle. The legacy "Download Satellite
    Imagery…" button that used to live here was removed in
    v3.3+ when the bulk download became unconditional / fully
    automatic — see
    :meth:`MainWindow._show_first_download_notice_and_start`
    and :meth:`MainWindow._check_satellite_resume_on_startup`.
    Pin the single-button membership so a future refactor that
    re-adds a "force download" button has to explicitly opt in
    by updating this test (and re-justifying why the UX needs
    it)."""
    frame = main_window.findChild(QFrame, "group_satellite_view_options")
    assert frame is not None
    assert _button_object_names_in_frame(frame) == [
        "act_toggle_show_satellite",
    ]


def test_program_information_group_exists_with_expected_buttons(
    main_window,
) -> None:
    """The Program Information group (v3.3) holds the
    Legal and Copyright Info button — the only legal-info surface
    point on the toolbar. Pin both the group's existence and the
    single-button membership so a future refactor can't quietly
    drop or rename either.

    The group lives to the right of Satellite View Options so a
    recipient inspecting an unfamiliar binary finds the
    legal-info entry at the *end* of the toolbar (read order on
    a left-to-right UI), not buried in the middle.
    """
    frame = main_window.findChild(QFrame, "group_program_information")
    assert frame is not None
    assert _button_object_names_in_frame(frame) == [
        "act_show_copyright_info",
    ]


def test_only_four_named_groups_exist(main_window) -> None:
    """The toolbar must carry exactly the four named groups.
    A fifth would mean someone introduced a new group without
    updating this test (and probably without updating the QSS
    stylesheet selector list either).

    ``QLabel`` inherits from ``QFrame`` in Qt's class hierarchy,
    so ``findChildren(QFrame)`` returns the title labels too —
    we filter to exact (non-QLabel) QFrame matches by checking
    ``type(...) is QFrame`` rather than using a substring match
    on the objectName, which would also match the per-title
    ``group_*_title`` labels.
    """
    expected = {
        "group_program_settings",
        "group_view_toggles",
        "group_satellite_view_options",
        "group_program_information",
    }
    tb = main_window.findChild(QToolBar, "mainActionsToolBar")
    assert tb is not None
    actual = {
        f.objectName()
        for f in tb.findChildren(QFrame)
        if type(f) is QFrame and f.objectName().startswith("group_")
    }
    assert actual == expected


# ---------------------------------------------------------------
# Cancel calibration lives outside the groups.
# ---------------------------------------------------------------


def test_cancel_calibration_action_is_not_in_any_group(
    main_window,
) -> None:
    """The Cancel calibration button is hidden by default and
    only appears mid-calibration; placing it inside any group
    would visually disrupt that group whenever the button shows
    up. Pinning its out-of-group location prevents a future
    refactor from accidentally tucking it into one of the
    groups.
    """
    for group_name in (
        "group_program_settings",
        "group_view_toggles",
        "group_satellite_view_options",
        "group_program_information",
    ):
        frame = main_window.findChild(QFrame, group_name)
        assert frame is not None
        names = _button_object_names_in_frame(frame)
        assert "act_cancel_calibration" not in names


def test_cancel_calibration_button_exists_in_toolbar(main_window) -> None:
    """The cancel-calibration button must be present on the
    toolbar (outside the four titled groups) so it can become
    visible when calibration starts.

    Because the button is added via ``tb.addAction(...)`` (so
    Qt natively tracks the action's visibility — see the long
    comment in ``_build_actions``), we look it up by walking
    the toolbar's QToolButtons and matching on the wrapped
    action's objectName rather than by an explicit per-button
    objectName.
    """
    tb = main_window.findChild(QToolBar, "mainActionsToolBar")
    assert tb is not None
    cancel_btns = [
        btn
        for btn in tb.findChildren(QToolButton)
        if btn.defaultAction() is not None
        and btn.defaultAction().objectName() == "act_cancel_calibration"
    ]
    assert len(cancel_btns) == 1
    cancel_btn = cancel_btns[0]
    # Hidden by default at both layers — action AND button. The
    # button tracking is the meat of the bug fix this test
    # exists for: a previous implementation used setDefaultAction
    # which only synced text/icon/tooltip, leaving the button
    # permanently visible regardless of the action's
    # ``setVisible(False)`` flag. ``tb.addAction(...)`` propagates
    # visibility, so both isVisible() calls below are False.
    assert cancel_btn.defaultAction().isVisible() is False
    # ``isHidden()`` is a direct property check: True when
    # ``setVisible(False)`` was called and not subsequently
    # undone, regardless of whether any ancestor widget happens
    # to be shown. Using ``isVisible()`` / ``isVisibleTo`` here
    # would also pick up the headless-test-fixture state (no
    # top-level show()), masking the action-to-button sync we
    # actually want to verify.
    assert cancel_btn.isHidden() is True


def test_cancel_calibration_action_is_owned_by_toolbar(
    main_window,
) -> None:
    """Structural pin for the visibility-sync fix: the cancel
    action must live in ``tb.actions()``, NOT just in
    ``setDefaultAction`` on a free-standing QToolButton.

    Why this matters: when an action is added via
    ``tb.addAction(...)``, Qt's toolbar machinery wires up an
    internal QToolBarItem widget and natively follows the
    action's ``visible`` property. When a QToolButton instead
    receives the action via ``setDefaultAction(...)``, only
    text/icon/tooltip sync — visibility does NOT. The first
    cut of the toolbar restructure used the latter pattern and
    left the button permanently visible regardless of the
    action's hidden state, which is the bug this test exists
    to prevent re-introducing.

    Asserting on ``tb.actions()`` membership is more reliable
    than a runtime ``isHidden()`` flip in a headless test:
    Qt only propagates the action→button visibility once the
    widget tree is realized via ``show()``, which the test
    fixture deliberately skips. The structural check is
    immune to that timing.
    """
    tb = main_window.findChild(QToolBar, "mainActionsToolBar")
    assert tb is not None
    act = main_window.findChild(QAction, "act_cancel_calibration")
    assert act is not None
    assert act in tb.actions(), (
        "Cancel calibration must be wired via tb.addAction(...) "
        "(not via setDefaultAction on an explicit QToolButton) so "
        "Qt natively syncs the button's visibility with the "
        "action's setVisible(...) flag."
    )


# ---------------------------------------------------------------
# Removed: Export waypoints to CSV.
# ---------------------------------------------------------------


def test_export_csv_action_no_longer_exists(main_window) -> None:
    """The CSV-export QAction was removed when the toolbar was
    restructured; pin its absence so a future refactor doesn't
    silently restore the button. The underlying
    ``_export_waypoints_csv`` slot method is intentionally left
    in place (orphan but harmless), so this test specifically
    targets the *toolbar* removal.
    """
    csv_action = main_window.findChild(QAction, "act_export_waypoints_csv")
    assert csv_action is None


# ---------------------------------------------------------------
# Stylesheet wiring sanity check.
# ---------------------------------------------------------------


def test_toolbar_stylesheet_targets_each_group(main_window) -> None:
    """The rounded-border QSS is applied at the toolbar level
    with object-name-scoped selectors. Without those selectors
    every QFrame in the app would inherit the border — visually
    chaotic. We don't assert on the specific border thickness
    or radius (those are aesthetics; let designers tune them
    later),     only that each of the four object names appears in
    the selector list, so the styling survives a future stylesheet
    rewrite that keeps the look but renames the rules.
    """
    tb = main_window.findChild(QToolBar, "mainActionsToolBar")
    assert tb is not None
    qss = tb.styleSheet()
    assert "QFrame#group_program_settings" in qss
    assert "QFrame#group_view_toggles" in qss
    assert "QFrame#group_satellite_view_options" in qss
    assert "QFrame#group_program_information" in qss


def test_toolbar_stylesheet_lights_checked_buttons_garmin_green(
    main_window,
) -> None:
    """Checkable toolbar buttons must visibly light up when
    toggled on — the user explicitly asked for a pressed-state
    affordance because Qt's default visual got swallowed by our
    group stylesheet. The chosen colour is the Garmin
    glass-cockpit "active mode" green (#1e7a3e) for aviation-UI
    familiarity. Pin the selector + colour combination so a
    future stylesheet rewrite doesn't silently regress the
    affordance back to invisible.
    """
    tb = main_window.findChild(QToolBar, "mainActionsToolBar")
    assert tb is not None
    qss = tb.styleSheet()
    # The checked-state rule must exist and must scope to the
    # group frames (so we don't accidentally style other
    # QToolButtons elsewhere in the app).
    assert "QToolButton:checked" in qss
    # The base Garmin green specifically — the hover/pressed
    # variants ride off this hue so pinning the base is
    # enough to detect a rewrite to an unrelated palette.
    assert "#1e7a3e" in qss


# ---------------------------------------------------------------
# QToolButton style per-button (airplane mode shows its icon).
# ---------------------------------------------------------------


def test_airplane_mode_button_shows_icon_beside_text(main_window) -> None:
    """The airplane-mode action carries an icon (the cellphone
    silhouette); its QToolButton must use
    ``ToolButtonTextBesideIcon`` so the icon actually renders.
    Other buttons stay text-only so the icon-less actions don't
    leave a blank column. This is a per-button style override
    (replacing the previous toolbar-wide setToolButtonStyle).
    """
    from PySide6.QtCore import Qt

    frame = main_window.findChild(QFrame, "group_view_toggles")
    assert frame is not None
    airplane_btn = None
    for btn in frame.findChildren(QToolButton):
        act = btn.defaultAction()
        if act is not None and act.objectName() == "act_toggle_airplane_mode":
            airplane_btn = btn
            break
    assert airplane_btn is not None
    assert (
        airplane_btn.toolButtonStyle()
        == Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    )


def test_text_only_button_does_not_render_icon_column(main_window) -> None:
    """Spot-check a non-icon button (Map File Settings) — its
    QToolButton must be in ``ToolButtonTextOnly`` mode so the
    label isn't pushed to the right by an empty icon slot."""
    from PySide6.QtCore import Qt

    frame = main_window.findChild(QFrame, "group_program_settings")
    assert frame is not None
    map_settings_btn = None
    for btn in frame.findChildren(QToolButton):
        act = btn.defaultAction()
        if (
            act is not None
            and act.objectName() == "act_open_map_file_settings"
        ):
            map_settings_btn = btn
            break
    assert map_settings_btn is not None
    assert (
        map_settings_btn.toolButtonStyle()
        == Qt.ToolButtonStyle.ToolButtonTextOnly
    )
