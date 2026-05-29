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

"""Application-wide Ctrl+wheel font-size resizer.

Three font categories — table, route text, hint — are reachable by
Ctrl+scrolling over the corresponding widget, mirroring the three
knobs in the Font Settings dialog. This lets the pilot adjust the
display size mid-flight without opening the dialog (e.g. the route
text label needs to be larger because turbulence is making the cabin
shake, or the hint footer needs to shrink so the table can take a
few more pixels of vertical space).

Routing rule: walk up the widget tree from the widget under the
cursor at the time of the wheel event. The first match wins.

  * Any ``QTableView`` ancestor → ``table_px``
  * Any ``QLabel`` with ``objectName == "routeText"`` → ``route_text_px``
  * Any ``QLabel`` with ``objectName == "mapHint"`` → ``hint_px``

Ctrl+wheel events that don't land on any of the three target
categories are silently *consumed* (the event filter still returns
``True``). This is deliberate: the alternative — letting the event
propagate to the receiving widget — would invoke pre-existing wheel
handlers, most notably ``QGraphicsView``'s view-zoom and
``QSpinBox``'s value arrow. Neither of those is what the user meant
by adding the Ctrl modifier, and silently flipping the cruise-speed
spinbox while the user is trying to resize the hint label would be
the worst kind of UI surprise. So the filter trades "Ctrl+wheel does
nothing on the map" for "Ctrl+wheel never does the wrong thing".

The filter is installed on the ``QApplication`` so it runs *before*
any widget's own ``wheelEvent`` — that ordering is critical for
beating ``QSpinBox`` / ``QAbstractScrollArea`` to the punch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QCursor, QWheelEvent
from PySide6.QtWidgets import QApplication, QLabel, QTableView, QWidget

from cvfr_routemaster.settings_store import (
    FONT_SIZE_MAX_PX,
    FONT_SIZE_MIN_PX,
    FontSizes,
    load_airplane_font_sizes,
    load_font_sizes,
    save_airplane_font_sizes,
    save_font_sizes,
)
from cvfr_routemaster.ui_theme import apply_dark_theme


#: One pixel per wheel notch. Responsive enough that a noticeable
#: change happens on the first detent, but slow enough that the user
#: can land on a specific px size without overshooting. Wheel events
#: report ``angleDelta().y()`` in 120-unit steps (one detent on a
#: standard mouse wheel); we read only the *sign* and apply the
#: step, ignoring the magnitude. This makes precision-trackpad
#: kinetic-scroll bursts (which can come in as several hundred
#: angle-units per event) behave the same as wheel detents instead
#: of jumping the font size by 10+ px in one flick.
_STEP_PX_PER_NOTCH: int = 1


def _font_category(widget: QWidget) -> str | None:
    """Return the font category that applies to ``widget``, or
    ``None`` if it isn't inside one of the three font-target
    ancestors.

    Walks up the parent chain so a wheel event delivered to a cell
    viewport, a header section, or any other internal child of a
    ``QTableView`` still resolves to the enclosing table. The same
    walk handles future cases where a tagged ``QLabel`` wraps a
    layout with child widgets.
    """
    cur: QWidget | None = widget
    while cur is not None:
        if isinstance(cur, QTableView):
            return "table"
        if isinstance(cur, QLabel):
            name = cur.objectName()
            if name == "routeText":
                return "route_text"
            if name == "mapHint":
                return "hint"
        cur = cur.parentWidget()
    return None


def _clamp(value: int) -> int:
    """Clamp ``value`` to the user-facing min/max range. We use the
    same bounds as the Font Settings dialog so the wheel-driven path
    and the dialog-driven path can never diverge in what values
    they'll accept."""
    return max(FONT_SIZE_MIN_PX, min(FONT_SIZE_MAX_PX, value))


def _adjust(sizes: FontSizes, category: str, delta_px: int) -> FontSizes:
    """Return a new :class:`FontSizes` with ``delta_px`` applied to
    one category and the other two untouched. The result is clamped
    to the ``FONT_SIZE_MIN_PX`` / ``FONT_SIZE_MAX_PX`` range.

    A no-op (the input value already at the clamp boundary in the
    delta direction) returns an equivalent dataclass; callers compare
    against the input to detect this case before paying the
    ``save_font_sizes`` + ``apply_dark_theme`` cost.
    """
    if category == "table":
        return FontSizes(
            table_px=_clamp(sizes.table_px + delta_px),
            route_text_px=sizes.route_text_px,
            hint_px=sizes.hint_px,
        )
    if category == "route_text":
        return FontSizes(
            table_px=sizes.table_px,
            route_text_px=_clamp(sizes.route_text_px + delta_px),
            hint_px=sizes.hint_px,
        )
    if category == "hint":
        return FontSizes(
            table_px=sizes.table_px,
            route_text_px=sizes.route_text_px,
            hint_px=_clamp(sizes.hint_px + delta_px),
        )
    return sizes


class CtrlWheelFontResizer(QObject):
    """Application-wide event filter that turns Ctrl+wheel scrolls
    into in-place font-size adjustments.

    Install via :meth:`QApplication.installEventFilter` so wheel
    events are intercepted before the receiver widget's own
    ``wheelEvent`` runs. ``QSpinBox`` and ``QAbstractScrollArea``
    (and therefore ``QGraphicsView`` and ``QTableView``) all accept
    wheel events natively, so any later interception would lose the
    race.

    The filter consumes *every* Ctrl+wheel event regardless of
    whether it actually changed a font — see the module docstring
    for the reasoning. Plain wheel events (no Ctrl modifier) pass
    through untouched so the existing map zoom, spinbox arrows, and
    scroll-area scrolling all keep working.

    Two font profiles are supported: a normal-mode profile (the
    historical one) and a separate airplane-mode profile. The
    ``airplane_mode_active`` callable injected at construction time
    decides which profile a given wheel event mutates — passing the
    predicate (rather than the route panel or the toolbar action
    directly) keeps the resizer free of Main-Window-shape
    dependencies and easy to unit-test with a fixed-True or
    fixed-False stub.
    """

    def __init__(
        self,
        project_root: Path,
        airplane_mode_active: Callable[[], bool] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._project_root = project_root
        # Default-False predicate so tests that don't care about the
        # airplane profile can construct the resizer with just the
        # project root, matching the pre-airplane-profile signature.
        self._airplane_mode_active: Callable[[], bool] = (
            airplane_mode_active if airplane_mode_active is not None else (lambda: False)
        )

    def eventFilter(  # noqa: N802 (Qt overrides use camelCase)
        self, watched: QObject, event: QEvent
    ) -> bool:
        if event.type() != QEvent.Type.Wheel:
            return False
        if not isinstance(event, QWheelEvent):
            # Defensive: ``QEvent.Type.Wheel`` is always a
            # ``QWheelEvent`` in stock Qt, but a future Qt version
            # or a PySide stub could in principle dispatch a
            # subclass. ``isinstance`` is cheap and keeps the
            # asserter-free path simple.
            return False
        if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            return False
        # Use the cursor's actual screen position to find the widget
        # under it. ``watched`` is the event's delivery target,
        # which is correct for most widgets but wrong for widgets
        # that grab wheel events on behalf of their viewport (Qt's
        # scroll areas route wheel through the scroll-area class,
        # not the viewport child) — using ``QCursor.pos()`` +
        # ``widgetAt`` always lands on the literally-under-cursor
        # widget, which is what the user is reasoning about.
        widget = QApplication.widgetAt(QCursor.pos())
        if widget is None:
            return True
        category = _font_category(widget)
        if category is None:
            return True
        dy = event.angleDelta().y()
        if dy == 0:
            return True
        sign = 1 if dy > 0 else -1
        delta_px = sign * _STEP_PX_PER_NOTCH

        # Profile selection: read the airplane predicate once and
        # branch the entire load/adjust/save trio through the same
        # decision. Re-evaluating the predicate after the load
        # would risk a race where the user toggles airplane mode
        # mid-scroll and we'd write the airplane sizes back to the
        # normal QSettings keys.
        airplane_active = bool(self._airplane_mode_active())
        if airplane_active:
            current = load_airplane_font_sizes(self._project_root)
        else:
            current = load_font_sizes(self._project_root)
        new_sizes = _adjust(current, category, delta_px)
        if new_sizes == current:
            # Hit a clamp boundary — still consume so the wheel event
            # can't bubble up and zoom the underlying pane.
            return True
        if airplane_active:
            save_airplane_font_sizes(new_sizes)
        else:
            save_font_sizes(new_sizes)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_dark_theme(app, new_sizes)
        return True
