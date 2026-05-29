"""Tests for :mod:`cvfr_routemaster.font_wheel_resize`.

Two layers:

1. Pure-Python routing rules (``_font_category``, ``_adjust``,
   ``_clamp``) — exercised without a running event loop because they
   only depend on widget identity and integer math.

2. The :class:`CtrlWheelFontResizer` event filter itself — exercised
   by synthesising :class:`QWheelEvent` instances and pushing them
   through the filter. The filter's job is to translate Ctrl+wheel
   gestures into Font Settings adjustments, and to silently *consume*
   every Ctrl+wheel event (whether or not it changed a font) so the
   gesture can't accidentally trigger the underlying widget's own
   wheel behaviour (map zoom, spinbox value step, etc.).
"""

from __future__ import annotations

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from cvfr_routemaster import settings_store  # noqa: E402
from cvfr_routemaster.font_wheel_resize import (  # noqa: E402
    CtrlWheelFontResizer,
    _adjust,
    _clamp,
    _font_category,
)
from cvfr_routemaster.settings_store import (  # noqa: E402
    FONT_SIZE_MAX_PX,
    FONT_SIZE_MIN_PX,
    FontSizes,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Redirect ``settings_store._settings()`` to a temp INI file so
    wheel-driven ``save_font_sizes`` writes don't scribble onto the
    user's real CVFRRouteMaster registry hive."""
    from PySide6.QtCore import QSettings

    ini_path = tmp_path / "test_settings.ini"
    monkeypatch.setattr(
        settings_store,
        "_settings",
        lambda: QSettings(str(ini_path), QSettings.Format.IniFormat),
    )
    return ini_path


# ---------------------------------------------------------------------------
# _font_category (pure routing logic)
# ---------------------------------------------------------------------------


def test_font_category_returns_table_for_qtableview(qapp) -> None:
    table = QTableView()
    try:
        assert _font_category(table) == "table"
    finally:
        table.deleteLater()


def test_font_category_walks_up_to_qtableview_ancestor(qapp) -> None:
    """A wheel event delivered to the table's viewport / header
    must still resolve to the enclosing ``QTableView`` — the routing
    walks up the parent chain rather than requiring an exact match."""
    table = QTableView()
    try:
        viewport = table.viewport()
        assert viewport is not None
        assert _font_category(viewport) == "table"
    finally:
        table.deleteLater()


def test_font_category_returns_route_text_for_tagged_label(qapp) -> None:
    label = QLabel("ICAO Field 15 string")
    label.setObjectName("routeText")
    try:
        assert _font_category(label) == "route_text"
    finally:
        label.deleteLater()


def test_font_category_returns_hint_for_tagged_label(qapp) -> None:
    label = QLabel("Map: drag pans · wheel zooms.")
    label.setObjectName("mapHint")
    try:
        assert _font_category(label) == "hint"
    finally:
        label.deleteLater()


def test_font_category_returns_none_for_untagged_widget(qapp) -> None:
    """Widgets without a matching ancestor (e.g. a QSpinBox cruise
    speed input) must return ``None`` so the filter can fall back
    to the silent-consume branch instead of accidentally bumping
    a stale category."""
    spin = QSpinBox()
    try:
        assert _font_category(spin) is None
    finally:
        spin.deleteLater()


def test_font_category_returns_none_for_unrelated_label(qapp) -> None:
    """A QLabel without ``routeText`` or ``mapHint`` object names
    must not route to either category — otherwise every QLabel in
    the app would silently fight for the same font knob."""
    label = QLabel("just a label, no special tag")
    try:
        assert _font_category(label) is None
    finally:
        label.deleteLater()


# ---------------------------------------------------------------------------
# _clamp / _adjust (pure integer math)
# ---------------------------------------------------------------------------


def test_clamp_respects_min_max_bounds() -> None:
    assert _clamp(FONT_SIZE_MIN_PX - 1) == FONT_SIZE_MIN_PX
    assert _clamp(FONT_SIZE_MAX_PX + 1) == FONT_SIZE_MAX_PX
    assert _clamp(12) == 12


def test_adjust_bumps_only_requested_category() -> None:
    base = FontSizes(table_px=12, route_text_px=14, hint_px=18)
    bigger = _adjust(base, "table", 1)
    assert bigger == FontSizes(table_px=13, route_text_px=14, hint_px=18)

    bigger = _adjust(base, "route_text", 2)
    assert bigger == FontSizes(table_px=12, route_text_px=16, hint_px=18)

    smaller = _adjust(base, "hint", -3)
    assert smaller == FontSizes(table_px=12, route_text_px=14, hint_px=15)


def test_adjust_clamps_at_bounds() -> None:
    at_min = FontSizes(
        table_px=FONT_SIZE_MIN_PX,
        route_text_px=FONT_SIZE_MIN_PX,
        hint_px=FONT_SIZE_MIN_PX,
    )
    # Decrementing below MIN must clamp to MIN (and the dataclass
    # equality lets the filter detect a no-op via the ``new == old``
    # short-circuit).
    assert _adjust(at_min, "table", -1) == at_min

    at_max = FontSizes(
        table_px=FONT_SIZE_MAX_PX,
        route_text_px=FONT_SIZE_MAX_PX,
        hint_px=FONT_SIZE_MAX_PX,
    )
    assert _adjust(at_max, "table", 1) == at_max


def test_adjust_unknown_category_is_no_op() -> None:
    base = FontSizes(table_px=12, route_text_px=14, hint_px=18)
    assert _adjust(base, "not-a-category", 1) == base


# ---------------------------------------------------------------------------
# CtrlWheelFontResizer (event filter integration)
# ---------------------------------------------------------------------------


def _make_wheel_event(
    pos: QPoint, dy: int, *, modifiers: Qt.KeyboardModifier
) -> QWheelEvent:
    """Build a minimal :class:`QWheelEvent` for the event filter.

    PySide6's ``QWheelEvent`` constructor takes the position twice
    (local + global), a pixel-delta point, an angle-delta point,
    the button state, modifiers, the scroll phase, and an
    inverted flag. We don't care about most of these for the
    filter's logic — only ``angleDelta().y()`` and ``modifiers()``
    are read — but Qt requires sensible values for all of them.
    """
    return QWheelEvent(
        QPointF(pos),
        QPointF(pos),
        QPoint(0, 0),
        QPoint(0, dy),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


def test_event_filter_ignores_plain_wheel(qapp, isolated_settings, tmp_path) -> None:
    """Wheel events without the Ctrl modifier must NOT be consumed —
    the existing handlers on the map view, route table, and cruise
    speed spinbox all rely on plain-wheel events to do their normal
    jobs (view zoom, scroll, value step)."""
    resizer = CtrlWheelFontResizer(tmp_path)
    label = QLabel("dummy")
    label.setObjectName("routeText")
    try:
        evt = _make_wheel_event(
            QPoint(0, 0), 120, modifiers=Qt.KeyboardModifier.NoModifier
        )
        assert resizer.eventFilter(label, evt) is False
    finally:
        label.deleteLater()


def test_event_filter_ignores_non_wheel_events(qapp, tmp_path) -> None:
    """Random non-wheel events flowing through the filter must pass
    through untouched. The filter is installed on QApplication so
    EVERY event in the app routes through it — wrongly returning
    True for any other event type would break basic interaction."""
    resizer = CtrlWheelFontResizer(tmp_path)
    label = QLabel("dummy")
    try:
        evt = QEvent(QEvent.Type.MouseMove)
        assert resizer.eventFilter(label, evt) is False
    finally:
        label.deleteLater()


def test_event_filter_bumps_table_font_when_scrolled_over_table(
    qapp, isolated_settings, tmp_path, monkeypatch
) -> None:
    """Ctrl+wheel up over a QTableView must bump ``table_px`` by 1
    and call ``apply_dark_theme`` so the new size renders
    immediately. The other two knobs must stay put."""
    captured: list[FontSizes] = []
    monkeypatch.setattr(
        "cvfr_routemaster.font_wheel_resize.apply_dark_theme",
        lambda _app, sizes: captured.append(sizes),
    )

    table = QTableView()
    table.show()
    try:
        # Force-route the widget-under-cursor lookup to our table.
        monkeypatch.setattr(
            "cvfr_routemaster.font_wheel_resize.QApplication.widgetAt",
            staticmethod(lambda _pos: table),
        )

        resizer = CtrlWheelFontResizer(tmp_path)
        evt = _make_wheel_event(
            QPoint(0, 0), 120, modifiers=Qt.KeyboardModifier.ControlModifier
        )
        baseline = settings_store.load_font_sizes()
        assert resizer.eventFilter(table, evt) is True
        new_sizes = settings_store.load_font_sizes()
        assert new_sizes.table_px == baseline.table_px + 1
        assert new_sizes.route_text_px == baseline.route_text_px
        assert new_sizes.hint_px == baseline.hint_px
        assert captured == [new_sizes]
    finally:
        table.hide()
        table.deleteLater()


def test_event_filter_bumps_hint_font_when_scrolled_over_hint(
    qapp, isolated_settings, tmp_path, monkeypatch
) -> None:
    """Ctrl+wheel down over a ``QLabel#mapHint`` must decrement
    ``hint_px`` and leave the other two knobs alone."""
    captured: list[FontSizes] = []
    monkeypatch.setattr(
        "cvfr_routemaster.font_wheel_resize.apply_dark_theme",
        lambda _app, sizes: captured.append(sizes),
    )

    hint = QLabel("map hint text")
    hint.setObjectName("mapHint")
    hint.show()
    try:
        monkeypatch.setattr(
            "cvfr_routemaster.font_wheel_resize.QApplication.widgetAt",
            staticmethod(lambda _pos: hint),
        )

        resizer = CtrlWheelFontResizer(tmp_path)
        evt = _make_wheel_event(
            QPoint(0, 0), -120, modifiers=Qt.KeyboardModifier.ControlModifier
        )
        baseline = settings_store.load_font_sizes()
        assert resizer.eventFilter(hint, evt) is True
        new_sizes = settings_store.load_font_sizes()
        assert new_sizes.hint_px == baseline.hint_px - 1
        assert new_sizes.table_px == baseline.table_px
        assert new_sizes.route_text_px == baseline.route_text_px
        assert captured == [new_sizes]
    finally:
        hint.hide()
        hint.deleteLater()


def test_event_filter_bumps_route_text_when_scrolled_over_routetext_label(
    qapp, isolated_settings, tmp_path, monkeypatch
) -> None:
    captured: list[FontSizes] = []
    monkeypatch.setattr(
        "cvfr_routemaster.font_wheel_resize.apply_dark_theme",
        lambda _app, sizes: captured.append(sizes),
    )

    label = QLabel("ICAO LLBG LLHA")
    label.setObjectName("routeText")
    label.show()
    try:
        monkeypatch.setattr(
            "cvfr_routemaster.font_wheel_resize.QApplication.widgetAt",
            staticmethod(lambda _pos: label),
        )

        resizer = CtrlWheelFontResizer(tmp_path)
        evt = _make_wheel_event(
            QPoint(0, 0), 120, modifiers=Qt.KeyboardModifier.ControlModifier
        )
        baseline = settings_store.load_font_sizes()
        assert resizer.eventFilter(label, evt) is True
        new_sizes = settings_store.load_font_sizes()
        assert new_sizes.route_text_px == baseline.route_text_px + 1
        assert new_sizes.table_px == baseline.table_px
        assert new_sizes.hint_px == baseline.hint_px
        assert captured == [new_sizes]
    finally:
        label.hide()
        label.deleteLater()


def test_event_filter_consumes_ctrl_wheel_on_non_target_widget(
    qapp, isolated_settings, tmp_path, monkeypatch
) -> None:
    """Ctrl+wheel over a widget that doesn't match any of the three
    font categories (e.g. a QSpinBox like the cruise-speed input)
    must still be *consumed* — letting it propagate would invoke
    the spinbox's value-step or the map view's zoom, which the
    user didn't intend by adding the Ctrl modifier. No font
    setting must change in this branch."""
    apply_calls: list[FontSizes] = []
    monkeypatch.setattr(
        "cvfr_routemaster.font_wheel_resize.apply_dark_theme",
        lambda _app, sizes: apply_calls.append(sizes),
    )
    save_calls: list[FontSizes] = []
    monkeypatch.setattr(
        "cvfr_routemaster.font_wheel_resize.save_font_sizes",
        lambda sizes: save_calls.append(sizes),
    )

    spin = QSpinBox()
    spin.show()
    try:
        monkeypatch.setattr(
            "cvfr_routemaster.font_wheel_resize.QApplication.widgetAt",
            staticmethod(lambda _pos: spin),
        )

        resizer = CtrlWheelFontResizer(tmp_path)
        evt = _make_wheel_event(
            QPoint(0, 0), 120, modifiers=Qt.KeyboardModifier.ControlModifier
        )
        assert resizer.eventFilter(spin, evt) is True
        assert save_calls == []
        assert apply_calls == []
    finally:
        spin.hide()
        spin.deleteLater()


def test_event_filter_consumes_at_clamp_boundary_without_writing(
    qapp, isolated_settings, tmp_path, monkeypatch
) -> None:
    """When the targeted font category is already at the clamp
    boundary in the requested direction, the event filter must
    still consume the wheel event (so a runaway scroll past the
    max can't bubble up to a zoom handler) but must NOT write to
    settings or re-apply the theme — both would be no-op churn."""
    # Pre-load settings at the maximum.
    settings_store.save_font_sizes(
        FontSizes(
            table_px=FONT_SIZE_MAX_PX,
            route_text_px=12,
            hint_px=18,
        )
    )
    apply_calls: list[FontSizes] = []
    monkeypatch.setattr(
        "cvfr_routemaster.font_wheel_resize.apply_dark_theme",
        lambda _app, sizes: apply_calls.append(sizes),
    )
    save_calls: list[FontSizes] = []
    monkeypatch.setattr(
        "cvfr_routemaster.font_wheel_resize.save_font_sizes",
        lambda sizes: save_calls.append(sizes),
    )

    table = QTableView()
    table.show()
    try:
        monkeypatch.setattr(
            "cvfr_routemaster.font_wheel_resize.QApplication.widgetAt",
            staticmethod(lambda _pos: table),
        )

        resizer = CtrlWheelFontResizer(tmp_path)
        evt = _make_wheel_event(
            QPoint(0, 0), 120, modifiers=Qt.KeyboardModifier.ControlModifier
        )
        assert resizer.eventFilter(table, evt) is True
        assert save_calls == []
        assert apply_calls == []
    finally:
        table.hide()
        table.deleteLater()


# ---------------------------------------------------------------------------
# Airplane-mode profile routing
# ---------------------------------------------------------------------------


def test_event_filter_bumps_airplane_profile_when_predicate_true(
    qapp, isolated_settings, tmp_path, monkeypatch
) -> None:
    """When the ``airplane_mode_active`` predicate returns True,
    Ctrl+wheel must route the bump through ``save_airplane_font_sizes``
    instead of ``save_font_sizes`` — leaving the normal-mode profile
    in QSettings completely untouched.

    Direct contract: the same wheel gesture in airplane mode must NOT
    perturb the user's normal-mode preferences, even by accident.
    """
    captured: list[FontSizes] = []
    monkeypatch.setattr(
        "cvfr_routemaster.font_wheel_resize.apply_dark_theme",
        lambda _app, sizes: captured.append(sizes),
    )
    # Seed each profile with a distinct value so the test can tell
    # them apart by inspection — same shape as the real two-profile
    # contract.
    settings_store.save_font_sizes(
        FontSizes(table_px=12, route_text_px=12, hint_px=18)
    )
    settings_store.save_airplane_font_sizes(
        FontSizes(table_px=24, route_text_px=20, hint_px=18)
    )

    table = QTableView()
    table.show()
    try:
        monkeypatch.setattr(
            "cvfr_routemaster.font_wheel_resize.QApplication.widgetAt",
            staticmethod(lambda _pos: table),
        )

        resizer = CtrlWheelFontResizer(tmp_path, airplane_mode_active=lambda: True)
        evt = _make_wheel_event(
            QPoint(0, 0), 120, modifiers=Qt.KeyboardModifier.ControlModifier
        )
        assert resizer.eventFilter(table, evt) is True

        # Normal profile must be untouched.
        normal_after = settings_store.load_font_sizes()
        assert normal_after == FontSizes(
            table_px=12, route_text_px=12, hint_px=18
        )
        # Airplane profile's table_px bumped by 1; other fields stable.
        airplane_after = settings_store.load_airplane_font_sizes()
        assert airplane_after == FontSizes(
            table_px=25, route_text_px=20, hint_px=18
        )
        assert captured == [airplane_after]
    finally:
        table.hide()
        table.deleteLater()


def test_event_filter_bumps_normal_profile_when_predicate_false(
    qapp, isolated_settings, tmp_path, monkeypatch
) -> None:
    """Mirror of the previous test: predicate returns False
    (airplane mode is off), so the wheel must route through the
    normal-mode save path and leave the airplane profile alone.
    Verifies the routing is genuinely conditional on the
    predicate, not always one or the other.
    """
    captured: list[FontSizes] = []
    monkeypatch.setattr(
        "cvfr_routemaster.font_wheel_resize.apply_dark_theme",
        lambda _app, sizes: captured.append(sizes),
    )
    settings_store.save_font_sizes(
        FontSizes(table_px=12, route_text_px=12, hint_px=18)
    )
    settings_store.save_airplane_font_sizes(
        FontSizes(table_px=24, route_text_px=20, hint_px=18)
    )

    table = QTableView()
    table.show()
    try:
        monkeypatch.setattr(
            "cvfr_routemaster.font_wheel_resize.QApplication.widgetAt",
            staticmethod(lambda _pos: table),
        )

        resizer = CtrlWheelFontResizer(tmp_path, airplane_mode_active=lambda: False)
        evt = _make_wheel_event(
            QPoint(0, 0), 120, modifiers=Qt.KeyboardModifier.ControlModifier
        )
        assert resizer.eventFilter(table, evt) is True

        normal_after = settings_store.load_font_sizes()
        assert normal_after == FontSizes(
            table_px=13, route_text_px=12, hint_px=18
        )
        airplane_after = settings_store.load_airplane_font_sizes()
        # Airplane untouched.
        assert airplane_after == FontSizes(
            table_px=24, route_text_px=20, hint_px=18
        )
        assert captured == [normal_after]
    finally:
        table.hide()
        table.deleteLater()


def test_event_filter_default_predicate_is_normal_profile(
    qapp, isolated_settings, tmp_path, monkeypatch
) -> None:
    """Backwards-compat: callers that don't supply
    ``airplane_mode_active`` (test setup, older entry points) get
    the normal-mode profile by default. The pre-airplane code
    paths must keep working unchanged.
    """
    monkeypatch.setattr(
        "cvfr_routemaster.font_wheel_resize.apply_dark_theme",
        lambda _app, _sizes: None,
    )
    settings_store.save_font_sizes(
        FontSizes(table_px=12, route_text_px=12, hint_px=18)
    )
    settings_store.save_airplane_font_sizes(
        FontSizes(table_px=24, route_text_px=20, hint_px=18)
    )

    table = QTableView()
    table.show()
    try:
        monkeypatch.setattr(
            "cvfr_routemaster.font_wheel_resize.QApplication.widgetAt",
            staticmethod(lambda _pos: table),
        )
        resizer = CtrlWheelFontResizer(tmp_path)  # no predicate
        evt = _make_wheel_event(
            QPoint(0, 0), 120, modifiers=Qt.KeyboardModifier.ControlModifier
        )
        assert resizer.eventFilter(table, evt) is True
        assert settings_store.load_font_sizes() == FontSizes(
            table_px=13, route_text_px=12, hint_px=18
        )
        # Airplane profile untouched by default-predicate path.
        assert settings_store.load_airplane_font_sizes() == FontSizes(
            table_px=24, route_text_px=20, hint_px=18
        )
    finally:
        table.hide()
        table.deleteLater()


# ---------------------------------------------------------------------------
# Wiring: the filter is installed on QApplication by MainWindow
# ---------------------------------------------------------------------------


def test_main_window_installs_ctrl_wheel_font_resizer(
    qapp, isolated_settings, tmp_path, monkeypatch
) -> None:
    """MainWindow must construct and install a
    :class:`CtrlWheelFontResizer` on the QApplication during
    ``__init__`` so the gesture works the moment the window
    appears — no need for the user to click anywhere first to
    "activate" the filter."""
    from cvfr_routemaster.main_window import MainWindow

    w = MainWindow(tmp_path)
    monkeypatch.setattr(w, "_maybe_autoload_on_start", lambda: None)
    try:
        assert hasattr(w, "_ctrl_wheel_font_resizer")
        assert isinstance(w._ctrl_wheel_font_resizer, CtrlWheelFontResizer)
    finally:
        w.close()
        w.deleteLater()
        qapp.processEvents()
