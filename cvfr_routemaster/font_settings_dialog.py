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

"""Modal dialog letting the user adjust on-screen display sizes.

Originally a font-only dialog (and still named ``font_settings_dialog``
on disk to keep diffs small for downstream tooling); the dialog now
covers two related concerns:

1. **Font sizes** — three knobs per profile, two profiles:

   * Tables (waypoint + route, both scale together)
   * Route text (the three labels stacked above the route table)
   * Usage hints (the three help-text panes)

   Profiles:

   * **Normal mode** — the default reading view, optimised for the
     full chart-plus-tables-plus-waypoints layout on a single monitor.
   * **Airplane mode** — the in-flight reading view used while the
     chart and waypoint pane are hidden. The user typically reads
     this on a right-seat / secondary monitor at a longer distance,
     so the airplane defaults are larger.

2. **Traffic display** — one knob:

   * Plane icon size (pixels nose-to-tail) for the on-chart VATSIM
     traffic silhouettes (v2 feature — see ``ROADMAP-NEXT.md``).

   This is a single global value rather than per-profile because
   airplane mode hides the chart entirely, so there's no second
   reading distance to tune for.

The dialog edits a working copy of every value; on Accept the
controller persists via the matching ``save_*`` functions in
:mod:`cvfr_routemaster.settings_store` and re-applies the
*currently active* font profile so the new sizes take effect
immediately without a restart. On Cancel nothing is touched.

The class name remains ``FontSettingsDialog`` — historical, and
preserved to keep the import surface small for tests. The
user-facing window title and toolbar label both read "Display
settings" so the broader scope is what users see.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from cvfr_routemaster.settings_store import (
    DEFAULT_WAYPOINT_MARKER_SIZE_PX,
    FONT_SIZE_MAX_PX,
    FONT_SIZE_MIN_PX,
    TRAFFIC_ICON_SIZE_MAX_PX,
    TRAFFIC_ICON_SIZE_MIN_PX,
    WAYPOINT_MARKER_SIZE_MAX_PX,
    WAYPOINT_MARKER_SIZE_MIN_PX,
    FontSizes,
)


class FontSettingsDialog(QDialog):
    """Display-settings editor with seven spinboxes total:

    * Six for fonts — three per profile (normal and airplane).
      Each profile is grouped inside its own ``QGroupBox`` so the
      spatial layout makes it obvious that the two columns of
      spinboxes belong to two independent profiles, not one set of
      "default vs override" knobs.

    * One for the on-chart VATSIM traffic silhouette size, in its
      own "Traffic display" group below the two font groups.
      Single global value (not per-profile) — see
      :data:`cvfr_routemaster.settings_store.DEFAULT_TRAFFIC_ICON_SIZE_PX`
      for the rationale (airplane mode hides the chart entirely).

    Bounds:

    * Font spinboxes use ``[FONT_SIZE_MIN_PX, FONT_SIZE_MAX_PX]`` so
      the user can't accidentally key in a value that would render
      the UI unusable (1-px fonts unreadable on the low end;
      100-px fonts blow up table column widths on the high end).
    * Traffic-icon spinbox uses
      ``[TRAFFIC_ICON_SIZE_MIN_PX, TRAFFIC_ICON_SIZE_MAX_PX]`` —
      tighter on the high end (96 px) because beyond that a single
      plane silhouette starts hiding waypoint dots underneath.

    Beyond either set of bounds the user can still hand-edit
    QSettings, by design — see ``load_font_sizes`` /
    ``load_traffic_icon_size_px``.

    Suffix " px" on every spinbox makes the unit explicit; the
    underlying :class:`FontSizes` stores CSS pixels because the
    existing ``ui_theme`` stylesheet already uses px everywhere
    and mixing px and pt would force two reasoning models on the
    same stylesheet. The traffic-icon size is in screen pixels
    measured nose-to-tail.

    The class name is historical (the dialog used to be font-only);
    the user-visible window title is "Display settings".
    """

    def __init__(
        self,
        current: FontSizes,
        airplane: FontSizes,
        traffic_icon_size_px: int,
        waypoint_marker_size_px: int = DEFAULT_WAYPOINT_MARKER_SIZE_PX,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Display settings")
        self.setModal(True)

        # Two profile-scoped triples of font-size spinboxes. The
        # same min/max range applies to all six because the
        # underlying QSS px values share a font-size codomain — no
        # reason for one knob to allow values another rejects.
        self._table_spin = self._make_font_spin(current.table_px)
        self._route_text_spin = self._make_font_spin(current.route_text_px)
        self._hint_spin = self._make_font_spin(current.hint_px)

        self._airplane_table_spin = self._make_font_spin(airplane.table_px)
        self._airplane_route_text_spin = self._make_font_spin(airplane.route_text_px)
        self._airplane_hint_spin = self._make_font_spin(airplane.hint_px)

        # Traffic-icon size — one global spinbox, distinct codomain
        # from the font knobs (different min/max bounds). Built via
        # a separate factory so a future "different scaling on
        # hi-DPI" decision for either category doesn't entangle.
        self._traffic_icon_size_spin = self._make_traffic_icon_spin(
            traffic_icon_size_px,
        )

        # Waypoint-marker (VRP triangle) size — second on-chart
        # overlay knob, shares the "different from fonts" codomain
        # with the traffic-icon spinbox but uses its own
        # min/max constants so a future widening of one doesn't
        # silently widen the other.
        self._waypoint_marker_size_spin = self._make_waypoint_marker_spin(
            waypoint_marker_size_px,
        )

        # Normal-mode group — applies whenever airplane mode is OFF.
        # Object names on the group boxes let tests target them by
        # role without scraping window titles or label text.
        normal_group = QGroupBox("Normal mode")
        normal_group.setObjectName("fontSettingsNormalGroup")
        normal_form = QFormLayout(normal_group)
        normal_form.addRow("Tables (waypoints + route):", self._table_spin)
        normal_form.addRow(
            "Route text (above route table):", self._route_text_spin
        )
        normal_form.addRow("Usage hints (help panes):", self._hint_spin)

        # Airplane-mode group — applies whenever airplane mode is ON.
        # Sits below the normal-mode group so the reading order
        # mirrors the "default → in-flight override" mental model.
        airplane_group = QGroupBox("Airplane mode")
        airplane_group.setObjectName("fontSettingsAirplaneGroup")
        airplane_form = QFormLayout(airplane_group)
        airplane_form.addRow(
            "Tables (waypoints + route):", self._airplane_table_spin
        )
        airplane_form.addRow(
            "Route text (above route table):", self._airplane_route_text_spin
        )
        airplane_form.addRow(
            "Usage hints (help panes):", self._airplane_hint_spin
        )

        # Traffic-display group — sits below the two font profiles
        # because it covers a different concern (on-chart overlay
        # geometry) and shouldn't be misread as a third font row.
        # Object name follows the same ``displaySettings…Group``
        # convention so a future test can target it directly.
        traffic_group = QGroupBox("Traffic display")
        traffic_group.setObjectName("displaySettingsTrafficGroup")
        traffic_form = QFormLayout(traffic_group)
        traffic_form.addRow(
            "Plane icon size (nose-to-tail):", self._traffic_icon_size_spin
        )

        # Waypoint-markers group — sits below traffic display
        # because both are on-chart-overlay sizes (vs the font
        # groups above which control text). Object name follows
        # the same ``displaySettings…Group`` convention as the
        # traffic group.
        markers_group = QGroupBox("Waypoint markers")
        markers_group.setObjectName("displaySettingsMarkersGroup")
        markers_form = QFormLayout(markers_group)
        markers_form.addRow(
            "Marker triangle size:", self._waypoint_marker_size_spin
        )

        # Brief explanatory blurb above the groups. Calls out the
        # two-profile model for fonts and the global scope of the
        # traffic-icon knob so the user reads the layout correctly.
        hint = QLabel(
            "Adjust on-screen display sizes. Normal-mode font sizes apply "
            "during regular use; airplane-mode sizes apply only while the "
            "Airplane mode toolbar toggle is pressed. The traffic-display "
            "icon size is global — it controls the size of VATSIM plane "
            "silhouettes drawn on the chart. Changes apply immediately on "
            "OK; press Cancel to discard."
        )
        hint.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(normal_group)
        layout.addWidget(airplane_group)
        layout.addWidget(traffic_group)
        layout.addWidget(markers_group)
        layout.addWidget(buttons)

    def _make_font_spin(self, value: int) -> QSpinBox:
        """Spinbox factory for the six font-size fields. Range and
        clamp use the font-size codomain (``FONT_SIZE_*``).
        """
        spin = QSpinBox()
        spin.setRange(FONT_SIZE_MIN_PX, FONT_SIZE_MAX_PX)
        spin.setSuffix(" px")
        # Clamp the incoming value into range — a user with a
        # corrupted QSettings entry shouldn't crash the dialog.
        spin.setValue(max(FONT_SIZE_MIN_PX, min(FONT_SIZE_MAX_PX, int(value))))
        return spin

    def _make_traffic_icon_spin(self, value: int) -> QSpinBox:
        """Spinbox factory for the single traffic-icon-size field.

        Separate from :meth:`_make_font_spin` because the codomains
        differ (font max is 48 px; icon max is 96 px) — sharing a
        factory would couple the two ranges and risk a future
        change to one accidentally widening the other.
        """
        spin = QSpinBox()
        spin.setRange(TRAFFIC_ICON_SIZE_MIN_PX, TRAFFIC_ICON_SIZE_MAX_PX)
        spin.setSuffix(" px")
        spin.setValue(
            max(
                TRAFFIC_ICON_SIZE_MIN_PX,
                min(TRAFFIC_ICON_SIZE_MAX_PX, int(value)),
            )
        )
        return spin

    def _make_waypoint_marker_spin(self, value: int) -> QSpinBox:
        """Spinbox factory for the waypoint-marker-size field.

        Separate codomain (``WAYPOINT_MARKER_SIZE_*``) from both
        the font and traffic-icon factories — the marker is a
        different on-chart element with its own legibility
        constraints (typical sizes 16–32 px). Same "clamp on
        edit" pattern as the others so a corrupted QSettings
        value can't crash the dialog.
        """
        spin = QSpinBox()
        spin.setRange(
            WAYPOINT_MARKER_SIZE_MIN_PX, WAYPOINT_MARKER_SIZE_MAX_PX
        )
        spin.setSuffix(" px")
        spin.setValue(
            max(
                WAYPOINT_MARKER_SIZE_MIN_PX,
                min(WAYPOINT_MARKER_SIZE_MAX_PX, int(value)),
            )
        )
        return spin

    def chosen_sizes(self) -> FontSizes:
        """The *normal-mode* :class:`FontSizes` the user committed
        (call after ``exec()`` returns ``QDialog.Accepted``)."""
        return FontSizes(
            table_px=int(self._table_spin.value()),
            route_text_px=int(self._route_text_spin.value()),
            hint_px=int(self._hint_spin.value()),
        )

    def chosen_airplane_sizes(self) -> FontSizes:
        """The *airplane-mode* :class:`FontSizes` the user committed.

        Two separate accessors (one per profile) rather than a single
        tuple/dataclass return so callers can choose to save just
        one profile at a time if they ever need to — e.g. a future
        "reset airplane mode to defaults" affordance.
        """
        return FontSizes(
            table_px=int(self._airplane_table_spin.value()),
            route_text_px=int(self._airplane_route_text_spin.value()),
            hint_px=int(self._airplane_hint_spin.value()),
        )

    def chosen_traffic_icon_size_px(self) -> int:
        """The traffic-icon base size (nose-to-tail, in pixels) the
        user committed.

        Distinct accessor (rather than folded into
        :meth:`chosen_sizes`) because the underlying knob isn't
        per-profile and shouldn't accidentally inherit airplane-vs-
        normal save plumbing if a future caller copy-pastes the
        font flow.
        """
        return int(self._traffic_icon_size_spin.value())

    def chosen_waypoint_marker_size_px(self) -> int:
        """The waypoint-marker triangle side length (in pixels) the
        user committed.

        Distinct accessor mirroring
        :meth:`chosen_traffic_icon_size_px` so callers can persist
        each knob through its own ``save_*`` function without
        unfolding tuples.
        """
        return int(self._waypoint_marker_size_spin.value())
