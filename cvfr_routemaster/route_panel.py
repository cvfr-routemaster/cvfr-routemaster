"""
Left-pane widget that owns the cruise-speed input, the segment table, and the
>180 kt warning. The widget is stateless apart from the speed value — every
refresh fully re-renders rows from a :class:`Route` snapshot supplied by the
controller, so there is no risk of the view drifting from the model after
add/remove operations.

Cells in the FROM/TO columns come in two flavours, both clickable:

- **Real waypoint** — green underlined link styling, identical to the waypoint
  table on the right. Clicking opens the configured external map provider for
  that fix.
- **Intermediate user-click point** — prefixed ``--> CODE.N`` in dim grey,
  also underlined. Clicking opens the same external map provider centred on
  the user-clicked coordinates. The grey-vs-green colour split preserves the
  visual hierarchy (real reporting points stand out, polyline sub-points are
  subordinate) while the shared underline tells the user both are clickable.

Both cell kinds emit the same :attr:`route_point_clicked` signal so the
controller has a single dispatch site.
"""

from __future__ import annotations

import math
import re

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QRegularExpression,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QKeySequence,
    QRegularExpressionValidator,
    QShortcut,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from cvfr_routemaster.route import (
    CVFR_MAX_SPEED_KTS,
    Route,
    RoutePoint,
    RouteSegment,
    format_hms,
    segment_time_seconds,
    to_hebrew_route_string,
    to_icao_route_string,
)
from cvfr_routemaster.waypoint_styles import (
    REPORTING_TYPE_COLORS,
    WAYPOINT_CODE_LINK_GREEN,
    WAYPOINT_NAME_LINK_BLUE,
)


# Default cruise speed for trainer / club single-engine VFR aircraft (Cessna 152
# and slower types around the Israeli flight schools cruise here). User can change
# it freely up to the spinbox max, with a warning above the CVFR limit.
_DEFAULT_CRUISE_KTS: int = 90

# Spinbox bounds. Min protects time math from div-by-zero territory; max accommodates
# the rare twin/turbine scenario without becoming a foot-gun.
_SPIN_MIN_KTS: int = 40
_SPIN_MAX_KTS: int = 250

# Cap the cruise-speed spinbox at a width that fits ``" 250 kts "`` plus the up/down
# arrow buttons with a comfortable margin. Without this the spinbox stretches to
# the full pane width because Qt's default size policy on a QSpinBox is Expanding —
# the result was a giant input next to a tiny label, drowning out the much more
# important route-string rows below it.
_SPEED_INPUT_MAX_WIDTH_PX: int = 110

# Column order matters — the click handler keys off the FROM/TO indices and
# render code populates positionally. The two "Reporting"/"Type" columns
# describe the *destination* of each row (the TO point), giving the pilot a
# preview of what they'll be reporting on arrival. "Alt (ft)" sits between
# heading and distance because that's how a CVFR briefing reads naturally:
# *which way → at what altitude → how far → for how long*.
_ROUTE_TABLE_COLS: tuple[str, ...] = (
    "From",
    "To",
    "Reporting",
    "Type",
    # The four ATC-handoff columns sit between Type and MAG BRG. They live
    # here, not at the end of the row, because a CVFR briefing reads each
    # leg as "what's the *destination*, who am I talking to there, what's
    # the next handoff" — the ATC information is leg-context, not flight
    # math, so it belongs adjacent to the destination columns and ahead
    # of the bearing/altitude/distance/time fields.
    "CTR",
    "Freq",
    "New CTR",
    "New Freq",
    "MAG BRG",
    "Alt (ft)",
    "Dist (nm)",
    "Time",
)

# Column indices used by the click handler and cell factories to dispatch
# by column. Keep these in sync with _ROUTE_TABLE_COLS above.
_COL_FROM: int = 0
_COL_TO: int = 1
_COL_REPORTING: int = 2
_COL_TYPE: int = 3
_COL_CTR: int = 4
_COL_FREQ: int = 5
_COL_NEW_CTR: int = 6
_COL_NEW_FREQ: int = 7
_COL_MAG_BRG: int = 8
_COL_ALT: int = 9
_COL_DIST: int = 10
_COL_TIME: int = 11

# Set of editable user-input columns (CTR / Freq / New CTR / New Freq).
# Centralised so the editability + custom-delegate logic stays in lockstep
# with the column-index constants above.
_USER_INPUT_COLS: frozenset[int] = frozenset(
    {_COL_CTR, _COL_FREQ, _COL_NEW_CTR, _COL_NEW_FREQ}
)

# Set of ATC-handoff columns the "Show ATC columns" checkbox toggles.
# Same four indices as ``_USER_INPUT_COLS`` today, but conceptually
# distinct — the visibility toggle is a *display* concern (briefing
# vs. plotting view), while ``_USER_INPUT_COLS`` is an *editability*
# concern. Splitting the constants means a future read-only ATC
# column (or a new editable column outside the ATC group) wouldn't
# accidentally inherit the wrong policy from the other.
_ATC_VISIBILITY_COLS: tuple[int, ...] = (
    _COL_CTR,
    _COL_FREQ,
    _COL_NEW_CTR,
    _COL_NEW_FREQ,
)

# Set of computed-value columns where the user is allowed to override the
# computed display with a manually-typed value. The override survives every
# render, persists per-leg in ``RoutePanel._cell_overrides``, and is shown
# in red with an asterisk suffix so a glance at the table tells the pilot
# *this is a hand-edit, not what the geometry / chart says*.
#
# - ``MAG BRG`` (degrees, integer 0–360): correct for an old chart that
#   prints a different bearing than the great-circle calc, or to round to
#   the leg's own designated track on a published airway.
# - ``Alt (ft)`` (one or more positive integers, comma-separated): override
#   the chart-derived altitude when the matcher couldn't find an arrow,
#   when the pilot's planning altitude differs from the chart label, or
#   to record an ATC-assigned altitude for a leg.
# - ``Dist (nm)`` (positive float, up to 1 dp): override the great-circle
#   distance with a chart-printed value or a slightly longer flown route.
#   When ``Dist`` is overridden the leg's ``Time`` cell auto-recomputes
#   from the new distance + cruise speed (Time itself is *not* editable).
_OVERRIDABLE_COLS: frozenset[int] = frozenset(
    {_COL_MAG_BRG, _COL_ALT, _COL_DIST}
)

# Regex contracts for what a valid override looks like, per column. They
# run on every editor commit; an unparseable string is silently dropped so
# the cell keeps its previous value (same UX as the frequency delegate).
#
# - MAG BRG: 1–3 digits, 0–360 inclusive (360 wraps to N for the ``°M``
#   display but is accepted as a typed value).
# - Alt: comma-separated positive integers, leading-/trailing-spaces around
#   each comma allowed for casual typing. Each value 1–5 digits so a
#   sub-FL300 ceiling fits without admitting six-figure typos.
# - Dist: 1–4 digit whole number with optional 1–2 digit decimal. Big
#   enough for any practical CVFR leg (~100 nm tops in Israel), tight
#   enough that a ``"1.234"`` typo lands in the editor without smuggling
#   a four-decimal value into the totals.
_MAG_BRG_OVERRIDE_REGEX: str = r"^\d{1,3}$"
_ALT_OVERRIDE_REGEX: str = r"^\d{1,5}(\s*,\s*\d{1,5})*$"
_DIST_OVERRIDE_REGEX: str = r"^\d{1,4}(\.\d{1,2})?$"

# Visual treatment for an overridden cell. Bright red so the eye picks
# the manual edits out from a screen full of computed values; the ``*``
# suffix is the pilot-conventional "this is *not* the chart's value"
# annotation (and survives a copy-paste into a flight log where colour
# is lost). The same colour and suffix are applied per altitude line in
# a stacked-altitude override (``"1600*"`` on its own line, ``"800*"``
# on the next).
_OVERRIDE_COLOR: str = "#ff5555"
_OVERRIDE_SUFFIX: str = "*"

# Strict frequency format: aviation VHF channels in the 100–999 MHz range
# expressed as XXX.Y or XXX.YYY. Anything else (e.g. 9-digit Morse spellings,
# accidental letters, two-digit prefixes, four decimals) is rejected so a
# cell can't silently carry a typo into the user's flight log.
_FREQUENCY_REGEX: str = r"^\d{3}\.(\d{3}|\d)$"

# Foreground colours for the two CTR text columns. Magenta (current CTR) and
# cyan (next-handoff CTR) form a high-contrast complementary pair against
# the table's dark theme so the user can scan the row left-to-right and
# instantly identify "who am I talking to now / who's next".
_CTR_TEXT_COLOR: str = "#ff00ff"  # magenta
_NEW_CTR_TEXT_COLOR: str = "#00ffff"  # cyan

# String shown in the "Alt (ft)" cell when no altitude arrow matched the
# segment. Three possible reasons (chart isn't calibrated for the leg, the
# leg lies between two intermediates, or the extractor missed an arrow) all
# converge on the same operationally honest answer: we don't know — go look
# at the chart.
_ALT_UNKNOWN_TEXT: str = "unknown"

# Dim grey for intermediate sub-point labels. Picked to read cleanly on the dark
# table chrome (`#1e1e1e` per ui_theme) while clearly subordinate to the green
# real-waypoint codes — a visual "this is a synthetic point" cue.
_INTERMEDIATE_FOREGROUND: str = "#9ca3af"

# Visual prefix that turns ``DAROM.1`` into ``--> DAROM.1`` for intermediate-point
# cells. Plain ASCII (rather than ↪ or →) so terminal-style monospaced rendering
# in the table stays predictable across fonts.
_INTERMEDIATE_PREFIX: str = "--> "

# Custom data role used to stash the (label, lat, lon) tuple on FROM/TO cells.
# The click handler reads this back to know what to emit; intermediates and
# real waypoints both carry a payload (only the colour and label differ).
_ROLE_WP_PAYLOAD: int = int(Qt.ItemDataRole.UserRole) + 1

# Custom data role for the Reporting column: stashes the waypoint code so the
# click handler can emit ``reporting_name_clicked`` without re-deriving it from
# the rendered Hebrew text. Intermediate-row cells leave this unset.
_ROLE_REPORTING_PAYLOAD: int = int(Qt.ItemDataRole.UserRole) + 2

# Custom data role: tag set on overridable cells whose displayed value is a
# user-typed override (vs the geometry-/chart-derived computed value). The
# cell context-menu reads this back to know whether to offer "Restore
# computed value", and the column-header context-menu walks the column to
# decide whether "Restore all <col> values" should be enabled. Storing it
# on the cell rather than re-deriving from text keeps the menu logic tight
# (no need to peer into ``_cell_overrides`` from the dispatch site) and
# makes the per-cell state self-describing in a debugger.
_ROLE_HAS_OVERRIDE: int = int(Qt.ItemDataRole.UserRole) + 3


class RoutePanel(QWidget):
    """Cruise-speed input + segment table + summary line.

    Signals:
        speed_changed(float): emitted whenever the cruise speed value changes.
        route_point_clicked(str, float, float): emitted when the user clicks a
            linkable FROM/TO cell — either a real waypoint or an intermediate
            sub-point. Arguments are ``(label, lat, lon)``: ``label`` is the
            displayed token (a waypoint code like ``DAROM`` or an intermediate
            ordinal like ``DAROM.1``), ``lat``/``lon`` are the underlying
            geographic coordinates the controller should open in the configured
            external map provider.
        reporting_name_clicked(str): emitted when the user clicks the blue
            Hebrew-name cell in the Reporting column. The argument is the
            *waypoint code* (e.g. ``"DAROM"``) — the controller looks the
            record up in its waypoint export list and centres the map on it,
            mirroring the same interaction in the master waypoint table.
        clear_route_requested(): emitted when the user has confirmed the
            "are you sure?" dialog on the Clear button. The controller is
            expected to drop every point from the route and refresh.
    """

    speed_changed = Signal(float)
    route_point_clicked = Signal(str, float, float)
    reporting_name_clicked = Signal(str)
    clear_route_requested = Signal()
    # Save / Load flight plan. The panel emits these as bare "the user
    # asked for X" intent signals; the actual file dialog + read/write +
    # parse + error-popup choreography lives in MainWindow so the panel
    # stays UI-only and doesn't grow a dependency on QFileDialog,
    # filesystem paths, or the waypoint-lookup callback the parser
    # resolution stage needs. ``save_plan_requested`` carries the
    # already-composed ICAO Field 15 string so the controller doesn't
    # have to re-derive it from the route model — the panel is the
    # source of truth for "what the user sees above the table".
    save_plan_requested = Signal(str)
    load_plan_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._title = QLabel("Route")
        self._title.setStyleSheet("font-weight: bold;")

        # When checked, intermediate user-click points appear in the ICAO and
        # Hebrew route strings as DDMM[N|S]DDDMM[E|W] coordinates per Field 15.
        # When unchecked they are dropped entirely so both strings contain only
        # published waypoints — the "filed waypoints" view of a polyline route.
        # The flag does NOT affect the segment table or totals: what you fly is
        # always shown in full, what you file is the operator's choice.
        self._include_intermediates_chk = QCheckBox("Include intermediate points (coords)")
        self._include_intermediates_chk.setChecked(True)
        self._include_intermediates_chk.setToolTip(
            "When checked, intermediate sub-points are emitted in both the ICAO "
            "route string and the Hebrew paperwork string as ICAO Field 15 "
            "coordinates (e.g. 3133N03433E for 31°33′N 34°33′E).\n"
            "When unchecked, only published waypoint codes / Hebrew names appear — "
            "useful when filing a flight plan that only references named fixes.\n\n"
            "The segment table and totals always reflect the full polyline regardless "
            "of this setting."
        )
        self._include_intermediates_chk.toggled.connect(self._on_include_intermediates_toggled)

        # "Clear route" wipes every point in one shot. Sits next to the
        # "Include intermediate points" checkbox so all route-level
        # actions cluster on the same header row. Disabled while the
        # route is empty so the button doesn't sit there inviting a
        # no-op confirmation. The destructive action is gated by an
        # explicit "are you sure?" dialog because there is no undo —
        # the click is the only thing standing between the user and a
        # cleared plan.
        self._clear_route_btn = QPushButton("Clear route")
        self._clear_route_btn.setToolTip(
            "Remove every waypoint and intermediate point from the current "
            "route. A confirmation dialog will appear first — once confirmed, "
            "the action cannot be undone."
        )
        self._clear_route_btn.setEnabled(False)
        self._clear_route_btn.clicked.connect(self._on_clear_route_clicked)

        # "Save plan" / "Load plan" mirror the Clear button's visual weight so
        # all three live-route actions form a single right-aligned cluster on
        # the header row. Their order is deliberate: Save | Load | Clear, with
        # Clear rightmost — the destructive button sits farthest from the
        # benign "Save" so a user reaching to save their work can't slip and
        # wipe the route. Save is also disabled while the route is empty so
        # there's no foot-gun of "saving" the empty-state hint string to disk;
        # Load stays enabled regardless because loading IS how you populate
        # an empty route.
        self._save_plan_btn = QPushButton("Save plan")
        self._save_plan_btn.setToolTip(
            "Save the current route to a flight-plan file (.cvfr) — just the "
            "ICAO Field 15 string shown above, no calibration or map state. "
            "Re-loadable on any machine that has the same waypoint database."
        )
        self._save_plan_btn.setEnabled(False)
        self._save_plan_btn.clicked.connect(self._on_save_plan_clicked)

        self._load_plan_btn = QPushButton("Load plan")
        self._load_plan_btn.setToolTip(
            "Load a flight plan from a .cvfr file. Replaces the current "
            "route. Malformed files are rejected with an explanation."
        )
        self._load_plan_btn.clicked.connect(self._on_load_plan_clicked)

        # ``self._title_row`` holds the right-aligned cluster of header
        # buttons (Save / Load / Clear) plus the intermediates checkbox.
        # Exposed as a panel attribute (not a local) so tests can assert
        # the visual order of the three action buttons relative to one
        # another — the order is contractually Save | Load | Clear with
        # Clear rightmost, and a regression that re-ordered them would
        # be invisible from outside this constructor otherwise.
        self._title_row = QHBoxLayout()
        self._title_row.setContentsMargins(0, 0, 0, 0)
        self._title_row.addWidget(self._title)
        self._title_row.addStretch(1)
        self._title_row.addWidget(self._include_intermediates_chk)
        # Order in the header row: Save | Load | Clear. Save is leftmost of
        # the live-route action cluster, Clear rightmost — see the buttons'
        # construction block above for the misclick-safety rationale.
        self._title_row.addWidget(self._save_plan_btn)
        self._title_row.addWidget(self._load_plan_btn)
        self._title_row.addWidget(self._clear_route_btn)

        self._speed_label = QLabel("Planned Cruise Speed:")
        self._speed_input = QSpinBox()
        self._speed_input.setRange(_SPIN_MIN_KTS, _SPIN_MAX_KTS)
        self._speed_input.setSingleStep(5)
        self._speed_input.setSuffix(" kts")
        self._speed_input.setValue(_DEFAULT_CRUISE_KTS)
        self._speed_input.setToolTip(
            f"Planned true cruise speed used to compute segment times.\n"
            f"Israel CVFR airspace limits speed to {CVFR_MAX_SPEED_KTS} kt; "
            f"a warning appears if this value is exceeded."
        )
        self._speed_input.valueChanged.connect(self._on_speed_changed)
        # Warn only when the user *commits* a new value over the limit, so we don't
        # nag during arrow-step transit (e.g. 178 → 179 → 180 → 181 → 182).
        self._speed_input.editingFinished.connect(self._maybe_warn_over_cvfr)
        self._speed_input.setMaximumWidth(_SPEED_INPUT_MAX_WIDTH_PX)
        self._was_over_limit: bool = False

        # No stretch factor on the spinbox — combined with the trailing stretch and
        # the explicit max-width above, the input stays at its natural size next to
        # its label and the rest of the row is empty space, instead of the spinbox
        # ballooning to fill the whole pane.
        speed_row = QHBoxLayout()
        speed_row.setContentsMargins(0, 0, 0, 0)
        speed_row.addWidget(self._speed_label)
        speed_row.addWidget(self._speed_input)
        speed_row.addStretch(1)

        # Two route-string rows above the table:
        #
        # 1. ICAO Field 15 (e.g. ``DAROM 3133N03433E GALIM``) — what an
        #    international flight-plan form expects.
        # 2. Hebrew paperwork (e.g. ``דרום 3133N03433E גלים``) — same route,
        #    waypoint codes swapped for Hebrew names, for Israeli flight plans
        #    in Hebrew and internal flight-school paperwork.
        #
        # Both labels are word-wrapped + ``TextSelectableByMouse|Keyboard`` so the
        # user can copy either into a form without re-typing. They share the same
        # "Include intermediate points (coords)" checkbox — the same geometric
        # decision applies to both, and surfacing one toggle is the right
        # cognitive load for the user.
        self._route_string_label = QLabel("Route is empty — Shift+left-click a chart waypoint to add it.")
        self._route_string_label.setWordWrap(True)
        self._route_string_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self._route_string_label.setToolTip(
            "ICAO Field 15 route string — selectable for copy & paste."
        )
        # ``routeText`` is the QSS selector hook that lets the
        # user's Font Settings dialog scale this label + the Hebrew
        # paperwork label + the totals label together. See
        # ``ui_theme.QLabel#routeText`` for the rule.
        self._route_string_label.setObjectName("routeText")

        # Hebrew row sits directly under the ICAO row. Hidden when the route is
        # empty so we don't show two empty-state placeholders; the ICAO row's
        # placeholder is enough to tell the user there's nothing to copy yet.
        self._hebrew_string_label = QLabel("")
        self._hebrew_string_label.setWordWrap(True)
        self._hebrew_string_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self._hebrew_string_label.setToolTip(
            "Hebrew route string for Israeli flight-plan and flight-school "
            "paperwork — selectable for copy & paste. Real waypoints render "
            "with their Hebrew name; intermediate sub-points use the same ICAO "
            "coordinate format as the row above so a leg's geometry stays "
            "comparable line-to-line."
        )
        # Same ``routeText`` tag as the ICAO Field 15 label so the
        # Font Settings dialog scales both together.
        self._hebrew_string_label.setObjectName("routeText")
        self._hebrew_string_label.hide()

        # Totals are computed from the actual flown polyline (always — independent
        # of the include-intermediates checkbox). Hidden when there's nothing to
        # total (route has fewer than two points).
        self._totals_label = QLabel("")
        self._totals_label.setWordWrap(True)
        # Bright white instead of the previous muted ``#888`` — the totals row
        # is the answer to the planning question ("how far / how long") and
        # the user needs to read it at a glance, not squint at it. The size
        # follows the ``routeText`` cluster (see Font Settings) so a user
        # who's bumped the route-text size sees the totals scale alongside.
        # Inline ``color`` rule sets *only* color, so the QSS ``font-size``
        # rule from ``QLabel#routeText`` still wins for sizing.
        self._totals_label.setStyleSheet("color: #ffffff;")
        self._totals_label.setObjectName("routeText")
        self._totals_label.hide()

        self._model = QStandardItemModel(0, len(_ROUTE_TABLE_COLS), self)
        self._model.setHorizontalHeaderLabels(list(_ROUTE_TABLE_COLS))

        self._table = _RouteTableView()
        self._table.setModel(self._model)
        # Editing is enabled in general but only the four user-input
        # columns (CTR / Freq / New CTR / New Freq) actually accept it
        # because the cell factories for the other columns set
        # ``setEditable(False)`` on their items. ``DoubleClicked`` and
        # ``EditKeyPressed`` are the lightest pair of triggers that still
        # let the user start typing on F2 or by simply double-clicking,
        # without the auto-on-focus behaviour that would interrupt
        # ``Ctrl+C`` selections.
        # ``DoubleClicked`` is the standard edit-on-action trigger;
        # ``SelectedClicked`` adds the spreadsheet-style "single-click on
        # an already-selected cell starts editing" flow that pilots
        # transferring data from a paper plog expect (Excel/Sheets both
        # do this). ``EditKeyPressed``/``AnyKeyPressed`` keep keyboard-
        # only navigation working: Tab/arrow to the cell, type, commit.
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        # Allow per-cell selection for table copy: a Word-style table copy
        # should let the user pick out e.g. just the CTR column. Rows are
        # still the natural unit, but the model's contiguous-selection
        # mode covers both grid rectangles and full rows on its own.
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectItems)
        self._table.setSelectionMode(QTableView.SelectionMode.ContiguousSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        # Frequency cells use a regex-validated ``QLineEdit`` editor — see
        # ``_FrequencyCellDelegate`` for the rationale. Bound here, on the
        # specific column indices, so the CTR (alphanumeric) cells get
        # the default text editor while only the Freq columns enforce
        # the XXX.Y / XXX.YYY format on commit.
        self._table.setItemDelegateForColumn(_COL_FREQ, _FrequencyCellDelegate(self._table))
        self._table.setItemDelegateForColumn(
            _COL_NEW_FREQ, _FrequencyCellDelegate(self._table)
        )
        # Override-capable columns each get their own validating delegate
        # (per-column placeholder + regex live inside the delegate, see
        # ``_OverridableCellDelegate``). Without this the user would type
        # into the same cell whose display already includes the asterisk
        # and unit suffix and have to surgically delete the cosmetic
        # glyphs first — which is also the bug the delegate's
        # ``setEditorData`` deliberately avoids.
        for col in (_COL_MAG_BRG, _COL_ALT, _COL_DIST):
            self._table.setItemDelegateForColumn(
                col, _OverridableCellDelegate(col, self._table)
            )
        h_header = self._table.horizontalHeader()
        # Each column auto-sizes to its content; the trailing column does *not* stretch
        # to fill — combined with the maximum-width pin set in
        # _apply_table_natural_width, this makes the table exactly content-wide so the
        # rightmost column never floats off against the splitter edge when the pane is
        # wider than the data needs (matches the waypoint table on the right).
        h_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        h_header.setStretchLastSection(False)
        # Re-pin the table width whenever the vertical scrollbar appears or
        # disappears — it's an additional constant we need to budget for, and it
        # toggles independently of our explicit re-renders (e.g. a row count that
        # crosses the visible-rows threshold). The natural-width pin doesn't
        # apply in airplane mode (the table is deliberately stretched to fill
        # the window there), so the handler is a no-op in that state.
        self._table.verticalScrollBar().rangeChanged.connect(
            lambda _lo, _hi: self._maybe_apply_table_natural_width()
        )
        # Click handler: opens the configured external map for cells that
        # represent real waypoints (identified by the per-cell payload role).
        self._table.clicked.connect(self._on_table_clicked)

        # Route-pane footer hint — the only place that explains how to *build*
        # a route by clicking on the chart. Wording deliberately does NOT
        # talk about "empty chart space"; you usually click on something
        # (a road, a coastline feature, a visual landmark) that just isn't
        # itself a published waypoint. Calling that out explicitly avoids
        # the older copy's confusion ("I can't click anywhere — there's
        # always something under the cursor").
        #
        # The hint also covers the three feature groups specific to the
        # route panel — chart clicks, table-cell links, and the new
        # MAG BRG / Alt / Dist override flow — separated by ``<br><br>``
        # so it reads as three short paragraphs instead of a wall of
        # text. The ``mapHint`` object name opts the label into the
        # unified bright-white / 50%-larger styling defined in
        # ``ui_theme.py``.
        hint = QLabel(
            "Shift+left-click a chart triangle to add a published waypoint to the "
            "route. Shift+left-click anywhere else on the chart — a road junction, "
            "coastline feature, or any visual landmark that isn't itself a "
            "published waypoint — to add a custom polyline sub-point named "
            "<b>&lt;previous-waypoint&gt;.N</b>. Shift+right-click a route point on "
            "the chart to remove it."
            "<br><br>"
            "Click a green code in the table to open it in the configured map "
            "provider. Click a Hebrew name in the Reporting column to centre the "
            "map on that waypoint."
            "<br><br>"
            "Double-click a <b>MAG BRG</b>, <b>Alt (ft)</b>, or <b>Dist (nm)</b> "
            "cell to override the computed value with a manually-typed one — "
            "useful when the chart prints a different bearing or altitude than "
            "the great-circle calc, when the matcher couldn't find an altitude "
            "arrow, or when ATC assigned a non-chart altitude. Overridden cells "
            "appear in <span style='color:#ff5555;'>red with an asterisk</span> "
            "(e.g. <span style='color:#ff5555;'>1600*</span>); changing "
            "<b>Dist</b> auto-recomputes the leg's <b>Time</b> from the new "
            "distance and the cruise speed. Right-click a red cell to restore "
            "the computed value, or right-click the column header to restore "
            "every override in that column at once."
        )
        hint.setWordWrap(True)
        hint.setObjectName("mapHint")
        # Property tag mirrors the waypoint-pane hint so tests can target this
        # specific label via ``findChildren`` + a property check rather than
        # relying on the on-screen text.
        hint.setProperty("hintRole", "routePanelHint")
        # Hold a reference for tests + future restyling without re-walking the
        # widget tree.
        self._hint_label = hint

        # "Show ATC columns" toggle. Lives directly above the table so
        # the checkbox is visually associated with what it shows/hides;
        # checked by default so the table renders the briefing-style
        # full row out-of-the-box. Unchecking it collapses the four
        # ATC-handoff columns (CTR / Freq / New CTR / New Freq) for a
        # narrower plotting-style view, *without* clearing their
        # values — the model items and ``_atc_inputs`` are untouched
        # by ``setColumnHidden``, so re-checking restores everything
        # the user typed (including across a full re-render in the
        # hidden state, since ``_render`` rebuilds rows from
        # ``_atc_inputs`` regardless of visibility).
        self._show_atc_chk = QCheckBox(
            "Show ATC columns (CTR / Freq / New CTR / New Freq)"
        )
        self._show_atc_chk.setChecked(True)
        self._show_atc_chk.setToolTip(
            "Show or hide the four ATC-handoff columns at once.\n\n"
            "Unchecked: the columns collapse to give a narrower "
            "table while plotting; the values you've typed are "
            "preserved and reappear on re-check.\n"
            "Checked: full briefing view with the CTR / Freq / "
            "New CTR / New Freq cells visible and editable."
        )
        self._show_atc_chk.toggled.connect(self._on_show_atc_toggled)

        # The checkbox sits in its own row, left-aligned with the
        # table (trailing stretch absorbs the slack horizontal space
        # the same way ``table_strip`` does for the table itself).
        atc_visibility_row = QHBoxLayout()
        atc_visibility_row.setContentsMargins(0, 0, 0, 0)
        atc_visibility_row.addWidget(self._show_atc_chk)
        atc_visibility_row.addStretch(1)

        # Wrap the table in an HBox with a trailing stretch widget. The table gets
        # a much higher stretch (1000:1) so it consumes available horizontal space up
        # to its setMaximumWidth pin; once that pin caps it, Qt redistributes the
        # leftover width to the trailing stretch — i.e. an empty band on the right
        # rather than a stretched-out last column or a forever-trailing scrollbar.
        table_strip = QHBoxLayout()
        table_strip.setContentsMargins(0, 0, 0, 0)
        table_strip.setSpacing(0)
        table_strip.addWidget(self._table, 1000)
        table_strip.addStretch(1)

        # Layout order, top to bottom:
        #   1. Cruise speed (compact input on its own line).
        #   2. "Route" header + "Include intermediate points" checkbox.
        #   3. ICAO Field 15 string.
        #   4. Hebrew paperwork string (hidden when route is empty).
        #   5. Totals (hidden until there's a leg to total).
        #   6. Segment table.
        #   7. Click-to-add hint.
        # The speed input lives above the header so it reads as a planning
        # *input* the user sets first; the header + intermediates checkbox
        # then sits immediately above the route strings it actually controls.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(speed_row)
        layout.addLayout(self._title_row)
        layout.addWidget(self._route_string_label)
        layout.addWidget(self._hebrew_string_label)
        layout.addWidget(self._totals_label)
        layout.addLayout(atc_visibility_row)
        layout.addLayout(table_strip, 1)
        layout.addWidget(hint)

        # Airplane-mode state. When ``True`` the panel hides its
        # ``Clear route`` button and the footer hint so the route table
        # + the labels above it can take the full window width as a
        # focused "in-flight reading view". The flag is also consulted
        # in :meth:`set_route` so that re-renders triggered by speed /
        # ATC-visibility / route-change events don't accidentally
        # un-hide the clear-route button via the usual enable/disable
        # path. False is the default — airplane mode starts off on
        # every launch (it's a viewing mode, not a persistent
        # preference).
        self._airplane_mode: bool = False

        # Re-entry guard for the airplane-mode column-width
        # redistribution. The redistribution swaps the header
        # resize mode (ResizeToContents → Interactive), which
        # itself triggers ``QEvent.Type.Resize`` on the table
        # viewport. Without this guard the viewport event filter
        # would schedule another redistribution, which would
        # schedule yet another, and so on. See
        # :meth:`_redistribute_airplane_column_widths` for the full
        # rationale.
        self._redistributing_columns: bool = False

        # Watch the table viewport for resize / style-change /
        # font-change events so we can re-balance column widths
        # whenever the available space or the metrics change.
        # Filter only does work while airplane mode is on; the
        # short-circuit lives inside :meth:`eventFilter`.
        self._table.viewport().installEventFilter(self)

        # Initial natural-width pin so the empty (header-only) table starts at a
        # sensible narrow size instead of expanding to whatever the splitter pane
        # gives it before the first set_route() call.
        self._table.resizeColumnsToContents()
        self._apply_table_natural_width()

        # Last route handed to set_route() — kept so the panel can re-render on
        # speed and checkbox changes without requiring the controller to push the
        # route again. Not a copy: ``Route`` is mutated in place by the controller
        # and we only ever read from it during render.
        self._last_route: Route | None = None

        # Last per-segment altitude tuples handed to set_route() — kept for the
        # same reason as ``_last_route``: speed/checkbox-driven re-renders
        # mustn't drop the altitude column. ``None`` means the controller has
        # never supplied altitudes (the column will render every leg as
        # "unknown"); an empty list means there are no segments to annotate.
        self._last_altitudes_per_segment: list[tuple[int, ...]] | None = None

        # User-entered ATC handoff data (CTR / Freq / New CTR / New Freq).
        # Keyed by ``(from_label, to_label)`` so the values survive every
        # full re-render of the table (which happens on every speed
        # change, route mutation, calibration completion, etc.). The
        # inner dict is keyed by the column index so the four cells
        # round-trip together. Real waypoints have stable labels
        # (e.g. ``"BAZRA"``), and intermediate ``CODE.N`` labels are
        # stable as long as preceding waypoints don't change — which is
        # the same locality guarantee the route-string and altitude
        # caches rely on, so the same trade-off applies.
        self._atc_inputs: dict[tuple[str, str], dict[int, str]] = {}
        # User overrides for the three computed-value columns
        # (MAG BRG / Alt / Dist). Same per-leg keying as ``_atc_inputs``
        # so the same persistence trade-off applies, and the inner dict
        # is keyed by column index so the three columns round-trip
        # together. Values are *canonical* override strings (the form
        # ``_parse_override`` returns after normalising whitespace and
        # zero-padding) so a typed ``"46"`` and ``"046"`` collapse to
        # the same key/value pair instead of accumulating both.
        self._cell_overrides: dict[tuple[str, str], dict[int, str]] = {}
        # Re-entrancy guard for the data-changed handler. The handler
        # mutates the model after parsing an override (to repaint the
        # cell with the asterisk + unit suffix), and that mutation
        # would re-fire ``dataChanged`` and recurse forever without
        # this flag.
        self._suspend_data_changed: bool = False
        # Connect once: every commit through the editable delegates funnels
        # through ``QStandardItemModel.dataChanged``, and our handler
        # mirrors the new text into ``_atc_inputs`` / ``_cell_overrides``
        # so the next render reproduces it.
        self._model.dataChanged.connect(self._on_model_data_changed)

        # Per-cell context menu — only meaningful on overridable cells
        # that *actually* carry an override (the ``_ROLE_HAS_OVERRIDE``
        # tag set by the cell factory). Anywhere else the menu is empty
        # and Qt simply doesn't show it. The right-click handler runs
        # in viewport-local coordinates so it can target the exact
        # ``QModelIndex`` under the cursor; ``setContextMenuPolicy``
        # lets us own dispatch instead of fighting Qt's defaults.
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_cell_context_menu)
        # Per-column header context menu — one item, "Restore all <col>
        # values", enabled iff the column is overridable AND has at
        # least one override to restore. Same custom-policy wiring as
        # the body so we don't have to subclass the header just for
        # this.
        h_header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        h_header.customContextMenuRequested.connect(self._show_header_context_menu)

        # Standard Ctrl+C copy handler. Default ``QTableView`` Ctrl+C
        # produces only a tab-separated plain-text dump; we override it
        # with an HTML-table copy so pasting into Word / Excel / Outlook
        # preserves the table grid. The plain-text fallback is still
        # included on the clipboard for terminals and code editors.
        copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self._table)
        copy_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy_shortcut.activated.connect(self._copy_selection_to_clipboard)

        # Wrap multi-line altitude cells. Without this, a "1600\n800" tuple
        # collapses to a single elided line. ``setWordWrap(True)`` plus
        # ``resizeRowsToContents`` after each render gives us natural row
        # heights that grow only for stacked-altitude rows and stay tight
        # for the (much more common) single-altitude case.
        self._table.setWordWrap(True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cruise_speed_kts(self) -> float:
        return float(self._speed_input.value())

    def set_airplane_mode(self, on: bool) -> None:
        """Toggle the panel's airplane-mode display.

        Airplane mode is the "in-flight reading view": the user has
        the chart calibrated, the route planned, and is now flying
        with the laptop on the right-seat. The map and waypoint pane
        are gone (handled by ``MainWindow`` — that's where the
        QSplitter children live), and within the route panel itself
        we hide the two surfaces the pilot can't act on usefully
        while airborne:

          * **Clear route**: a one-click destructive button that no
            pilot should be one accidental click away from in
            turbulence.
          * **Footer hint**: the multi-paragraph "how to build a
            route by Shift-clicking the chart" instructions. The
            chart isn't visible in airplane mode and the pilot
            already built the route on the ground, so the
            instructions are pure noise.

        Speed, intermediates checkbox, ATC-columns toggle, and the
        route table itself (plus the ICAO / Hebrew / totals labels
        above it) all stay visible — the pilot still needs to read
        the plan and may want to revise cruise speed mid-flight if
        winds aren't what the briefing said.

        Args:
            on: ``True`` to enter airplane mode, ``False`` to leave it.
                Idempotent: calling with the current value is a no-op.
        """
        if self._airplane_mode == on:
            return
        self._airplane_mode = on
        self._clear_route_btn.setVisible(not on)
        self._hint_label.setVisible(not on)
        self._apply_airplane_mode_table_sizing(on)

    def _apply_airplane_mode_table_sizing(self, on: bool) -> None:
        """Stretch the route table to the full pane width in airplane
        mode; restore the content-width pin on exit.

        In the default (non-airplane) layout the table is pinned to
        its natural width via ``setMaximumWidth`` so the rightmost
        column never floats off against an over-wide splitter pane —
        the trailing stretch widget in ``table_strip`` absorbs the
        slack instead, mirroring the waypoint pane on the right.

        Airplane mode collapses both the map column and the
        waypoint pane, so the route panel takes the whole window
        width. The user explicitly asked for the table to span that
        full width in flight. We achieve that by:

          * Lifting the ``setMaximumWidth`` pin to
            ``QWIDGETSIZE_MAX`` so the table is free to grow with
            its layout slot.
          * Keeping every column on ``ResizeToContents`` so each
            column auto-resizes whenever its content changes —
            critically, when the user grows the table font via
            Ctrl+wheel mid-flight, or when an intermediate point
            like ``DAROM.1`` makes the FROM/TO column wider than
            its short headers would imply. ``QHeaderView.Stretch``
            mode (an earlier attempt at this) freezes columns to
            their initial proportions and refuses to grow them
            when content needs more space, so wider intermediate
            labels would simply get clipped.
          * Calling :meth:`_redistribute_airplane_column_widths`
            after every layout-relevant event so any leftover
            viewport width is distributed *proportionally* across
            every visible column rather than piled onto a single
            trailing column. The earlier
            ``setStretchLastSection(True)`` approach put all the
            slack on Time, which looked jarring when the font was
            small (Time column became huge while every other
            column stayed narrow). The proportional pass instead
            grows each column by ``leftover_px × (col_content_width
            / total_content_width)`` — wide columns like
            ``Reporting`` get more slack, narrow ones like ``Time``
            get less, and the result reads as a uniformly-stretched
            full-bleed table.

        Leaving airplane mode reverses both changes: drop the
        stretch-last flag (always cleared but kept for belt-and-
        braces), run a content-resize pass, and re-pin the natural
        width so the next render restores the compact in-pane look.
        """
        header = self._table.horizontalHeader()
        if on:
            # PySide6 doesn't re-export Qt's ``QWIDGETSIZE_MAX``
            # macro through its Python bindings, so use the literal
            # value (``(1 << 24) - 1``) — that's the documented
            # sentinel ``setMaximumWidth`` interprets as "no cap".
            self._table.setMaximumWidth(16777215)
            # We deliberately do NOT use ``setStretchLastSection``
            # here — the proportional redistribution below replaces
            # it. Belt-and-braces clear the flag in case a prior
            # exit path left it on (e.g. test setup poking the
            # header directly).
            header.setStretchLastSection(False)
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            # Defer the first redistribution to the next event-loop
            # iteration: when this method is called from
            # ``set_airplane_mode``, the QSplitter hasn't yet given
            # the route panel its post-collapse width, so
            # ``viewport().width()`` would report the pre-resize
            # value and the math would be off. Letting Qt finish
            # the layout pass first guarantees an accurate viewport
            # width when the distribution runs.
            QTimer.singleShot(0, self._redistribute_airplane_column_widths)
        else:
            header.setStretchLastSection(False)
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            self._table.resizeColumnsToContents()
            self._table.resizeRowsToContents()
            self._apply_table_natural_width()

    def _redistribute_airplane_column_widths(self) -> None:
        """Spread leftover viewport width proportionally across every
        visible column.

        Airplane mode wants the table to fill the window edge to
        edge AND every column to auto-grow with its content. Qt's
        built-in header modes can't deliver both at once:

          * ``ResizeToContents`` alone leaves the table flush with
            content width and the right side of the window empty.
          * ``Stretch`` alone freezes columns at their initial
            proportional share and refuses to grow them when
            content widens.
          * ``ResizeToContents`` + ``setStretchLastSection(True)``
            piles ALL leftover onto the last column.

        The proportional pass we run here is the missing third
        option. The algorithm:

          1. Force a ``resizeColumnsToContents`` snapshot so every
             section reports its natural (content-driven) size.
          2. Sum those sizes; subtract from the viewport width to
             get the leftover.
          3. Switch the header to ``Interactive`` mode (this is the
             only mode where ``resizeSection`` widths actually
             stick — ``ResizeToContents`` and ``Stretch`` overwrite
             explicit sizes on the next layout pass).
          4. For each visible section, add
             ``leftover × (natural / total_natural)`` to its
             natural width. The last visible section absorbs the
             rounding remainder so the table's right edge lands
             exactly at the viewport edge instead of one pixel
             short.

        Re-entry protection: the ``setSectionResizeMode`` calls in
        steps 1 and 3 can themselves trigger viewport resize
        events, which the event filter on the viewport would
        otherwise schedule another redistribution for. The
        ``_redistributing_columns`` flag short-circuits the filter
        during a redistribution so the cascade collapses to one
        pass.
        """
        if not self._airplane_mode:
            return
        if self._redistributing_columns:
            return
        # Defensive guard against the QTimer.singleShot(0, ...)
        # firing after ``self._table``'s C++ side has been
        # destroyed. This happens in two real scenarios:
        #
        # 1. **Teardown race in tests.** The test fixture's
        #    cleanup explicitly drains DeferredDelete events to
        #    avoid leaking a ``MainWindow`` tree per test. If
        #    the previously-running test left a pending
        #    redistribute timer (typical for any test that
        #    toggles airplane mode), the drain may fire the
        #    timer after the table's wrapper has already been
        #    destroyed.
        # 2. **User closes the window during a pending
        #    redistribute.** Real-world equivalent: airplane
        #    mode toggle scheduled the timer, the user clicked
        #    the close button before the next layout pass, the
        #    timer's QObject parent is the route panel which is
        #    deleted as part of the close, but the timer fires
        #    one extra time before its own destruction is
        #    processed.
        #
        # Both surface as ``RuntimeError`` from any method call
        # on the dead Python wrapper. The early access here
        # (``horizontalHeader()``, ``viewport()``) is enough of
        # a probe; anything past this point would have
        # short-circuited via the C++ check chain anyway.
        try:
            header = self._table.horizontalHeader()
            viewport_width = self._table.viewport().width()
        except RuntimeError:
            return
        if viewport_width <= 0:
            return
        self._redistributing_columns = True
        try:
            # Step 1: snap to content widths so we can read each
            # column's natural size.
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            self._table.resizeColumnsToContents()
            natural = []
            for i in range(header.count()):
                if self._table.isColumnHidden(i):
                    natural.append(0)
                else:
                    natural.append(header.sectionSize(i))
            total_natural = sum(natural)
            if total_natural <= 0:
                return
            leftover = viewport_width - total_natural
            if leftover <= 0:
                # Content already exceeds the viewport — leave
                # the columns at their content widths and let
                # QTableView's horizontal scrollbar take over.
                return
            # Step 2: switch to Interactive so explicit widths stick.
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            visible_indices = [
                i for i, w in enumerate(natural) if w > 0
            ]
            if not visible_indices:
                return
            last_visible = visible_indices[-1]
            extra_remaining = leftover
            for i in visible_indices:
                if i == last_visible:
                    new_w = natural[i] + extra_remaining
                else:
                    share = int(leftover * (natural[i] / total_natural))
                    new_w = natural[i] + share
                    extra_remaining -= share
                header.resizeSection(i, new_w)
        finally:
            self._redistributing_columns = False

    def eventFilter(  # noqa: N802 (Qt overrides use camelCase)
        self, watched: QObject, event: QEvent
    ) -> bool:
        """Re-distribute column widths whenever the route table's
        viewport resizes, restyles, or changes font.

        Three event types fire when the user does something the
        redistribution should react to:

          * ``QEvent.Type.Resize`` — the QSplitter handed the route
            panel a new width (e.g. the user dragged a splitter
            handle, or Airplane mode toggled and collapsed the
            other panes).
          * ``QEvent.Type.StyleChange`` — :func:`apply_dark_theme`
            wrote a new stylesheet (e.g. the user Ctrl+wheeled
            the font, or accepted a new size in the dialog). The
            new stylesheet may rebalance column content widths
            because the new font has different metrics.
          * ``QEvent.Type.FontChange`` — Qt also fires this
            directly when a widget's font changes via the
            stylesheet pipeline; catching it here is belt-and-
            braces with ``StyleChange``.

        The redistribution is deferred via
        :meth:`QTimer.singleShot` so the current layout pass
        completes (and the viewport reports its post-resize
        width) before we measure it.

        Filter never *consumes* the event — we always fall through
        to the default handler so Qt's own resize / repaint flow
        is unaffected.
        """
        if watched is self._table.viewport() and self._airplane_mode:
            event_type = event.type()
            if event_type in (
                QEvent.Type.Resize,
                QEvent.Type.StyleChange,
                QEvent.Type.FontChange,
            ):
                if not self._redistributing_columns:
                    QTimer.singleShot(
                        0, self._redistribute_airplane_column_widths
                    )
        return super().eventFilter(watched, event)

    def is_airplane_mode(self) -> bool:
        """Whether the panel is currently in airplane-mode display.

        Exposed so the controller (and tests) can read the panel's
        state without poking at ``_airplane_mode`` directly.
        """
        return self._airplane_mode

    def set_route(
        self,
        route: Route,
        *,
        altitudes_per_segment: list[tuple[int, ...]] | None = None,
    ) -> None:
        """Refresh the table from the supplied route snapshot at the current speed.

        ``altitudes_per_segment`` is the controller's per-segment altitude
        match (one tuple per segment, in the same order as
        ``route.segments()``). Tuples are rendered top-to-bottom on a single
        cell with newlines between values, preserving the chart's visual
        stacking. ``None`` or an empty tuple means we couldn't determine the
        altitude for that leg, and the cell shows ``"unknown"``. Length
        mismatches (rare, indicates a controller bug) are tolerated by
        truncating the supplied list to the segment count.
        """
        self._last_route = route
        self._last_altitudes_per_segment = (
            list(altitudes_per_segment) if altitudes_per_segment is not None else None
        )
        self._render(route, self.cruise_speed_kts())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _on_speed_changed(self, _val: int) -> None:
        self.speed_changed.emit(float(_val))

    def _on_include_intermediates_toggled(self, _checked: bool) -> None:
        """Re-render so the route string reflects the new flag. Cheap — the
        segment table content doesn't depend on the flag, so this is essentially
        just a label update plus the standard cell rebuild."""
        if self._last_route is not None:
            self._render(self._last_route, self.cruise_speed_kts())

    def _on_show_atc_toggled(self, checked: bool) -> None:
        """Show or hide the four ATC-handoff columns at once.

        ``QTableView.setColumnHidden`` is a pure display toggle — the
        underlying ``QStandardItem`` objects, the cells' edit flags
        and delegates, and ``_atc_inputs`` are all untouched. That
        means re-checking restores every typed value automatically,
        and a render that happens while hidden (e.g. a cruise-speed
        nudge) repopulates the cells from ``_atc_inputs`` whether
        the column is visible or not.

        After toggling we re-pin the table's natural width
        (``QHeaderView.length()`` returns the sum of *visible*
        sections, so dropping four columns shrinks the pin
        automatically — without the manual pin call the table would
        keep its old maximum width and leave a wide trailing stretch
        beside it).
        """
        for col in _ATC_VISIBILITY_COLS:
            self._table.setColumnHidden(col, not checked)
        # Two-way split, same shape as :meth:`_render`:
        #   * Non-airplane mode: snap columns to content and re-pin
        #     the natural width so the trailing stretch absorbs the
        #     slack.
        #   * Airplane mode: route through the proportional
        #     redistributor instead — calling
        #     ``resizeColumnsToContents`` directly would wipe out the
        #     Interactive-mode widths the last redistribution set,
        #     and skipping the redistribution would leave the table
        #     content-width with empty space on the right of the
        #     viewport.
        if self._airplane_mode:
            self._redistribute_airplane_column_widths()
        else:
            self._table.resizeColumnsToContents()
            self._apply_table_natural_width()

    def _on_clear_route_clicked(self) -> None:
        """Confirm with the user before emitting ``clear_route_requested``.

        The dialog wording is intentionally explicit about irreversibility —
        clearing a partially-built route mid-planning is the most common
        accidental loss-of-work in this app, and a generic "are you sure?"
        underplays that. The default focused button is "No" so an
        accidental Enter press doesn't wipe the route.
        """
        if self._last_route is None or self._last_route.is_empty():
            return  # button shouldn't have been clickable, but be defensive
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("Clear route?")
        confirm.setText("Are you sure you want to clear the route?")
        confirm.setInformativeText(
            "Every waypoint and intermediate point in the current route will "
            "be removed. This cannot be undone."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.No)
        if confirm.exec() == QMessageBox.StandardButton.Yes:
            self.clear_route_requested.emit()

    def _on_save_plan_clicked(self) -> None:
        """Emit ``save_plan_requested`` carrying the ICAO Field 15 string.

        The string is sourced from :func:`to_icao_route_string` with
        ``include_intermediates=True`` — the saved plan always contains every
        intermediate coord regardless of the user's "Include intermediate
        points" display checkbox, because that checkbox is purely a *view*
        preference (whether to show intermediates above the table). Dropping
        intermediates on save would silently lose the polyline detail the
        user might have spent time placing on the chart, which is exactly
        the kind of "I thought my work was saved" foot-gun a save button
        must never have.
        """
        if self._last_route is None or self._last_route.is_empty():
            return  # button shouldn't be enabled, but stay defensive
        plan_text = to_icao_route_string(self._last_route, include_intermediates=True)
        if not plan_text:
            # Defensive: ``to_icao_route_string`` returns "" only for an empty
            # route, which the is_empty() check above already filtered. The
            # belt-and-braces guard ensures a future change to the formatter
            # (e.g. a route that contains only filtered intermediates) can't
            # silently produce a zero-byte file.
            return
        self.save_plan_requested.emit(plan_text)

    def _on_load_plan_clicked(self) -> None:
        """Emit ``load_plan_requested``; the controller handles the file dialog.

        No "are you sure?" gate here: Load overwrites the current route by
        design (parallel to "Open file" in any editor), and the malformed-input
        rejection path leaves the existing route untouched, so a misclick on
        Load while a plan is in progress is recoverable as long as the user
        cancels the file dialog. The controller is responsible for any
        confirmation flow when overwriting a non-empty in-memory route.
        """
        self.load_plan_requested.emit()

    def _maybe_warn_over_cvfr(self) -> None:
        """Show a warning the first time the committed speed crosses the CVFR limit.

        The "first time per crossing" gate keeps the dialog from re-popping every
        Enter press the user makes while they're already at, say, 200 kt. Going
        back to ≤180 and then up again will warn again — which is the desired
        teaching moment."""
        v = self._speed_input.value()
        over = v > CVFR_MAX_SPEED_KTS
        if over and not self._was_over_limit:
            QMessageBox.warning(
                self,
                "Cruise speed above CVFR limit",
                (
                    f"You entered {v} kt as the planned cruise speed.\n\n"
                    f"The maximum allowed CVFR speed in Israeli airspace is "
                    f"{CVFR_MAX_SPEED_KTS} kt. Verify this is appropriate for your "
                    f"flight plan and clearance."
                ),
            )
        self._was_over_limit = over

    def _on_table_clicked(self, index: QModelIndex) -> None:
        """Dispatch table clicks by column and stored payload.

        - FROM / TO with a ``(label, lat, lon)`` payload → ``route_point_clicked``
          (real waypoints and intermediates both fire this).
        - Reporting column with a waypoint code stashed in
          :data:`_ROLE_REPORTING_PAYLOAD` → ``reporting_name_clicked`` (real
          waypoints only — intermediates have no Hebrew name and no payload).
        - Type and numeric columns carry no payload and are silently ignored.
        """
        if not index.isValid():
            return
        col = index.column()
        if col in (_COL_FROM, _COL_TO):
            payload = self._model.data(index, _ROLE_WP_PAYLOAD)
            if not isinstance(payload, tuple) or len(payload) != 3:
                return
            label, lat, lon = payload
            if not isinstance(label, str):
                return
            try:
                self.route_point_clicked.emit(label, float(lat), float(lon))
            except (TypeError, ValueError):
                return
            return
        if col == _COL_REPORTING:
            code = self._model.data(index, _ROLE_REPORTING_PAYLOAD)
            if isinstance(code, str) and code:
                self.reporting_name_clicked.emit(code)

    def _render(self, route: Route, speed_kts: float) -> None:
        self._model.removeRows(0, self._model.rowCount())
        segs = route.segments()

        # The clear button is the destructive counterpart to add/remove —
        # disable it when there's nothing to clear so the user doesn't get
        # an "are you sure?" dialog for a no-op. We treat *any* point in
        # the route (including a lone origin) as worth offering to clear.
        # Save Plan piggybacks on the same emptiness check: there is no
        # value in saving the empty-state hint string to disk, and a file
        # that re-parses as "no waypoints found" is just user confusion.
        # Load Plan stays enabled regardless — loading IS how an empty
        # route gets populated.
        has_points = len(route.points()) > 0
        self._clear_route_btn.setEnabled(has_points)
        self._save_plan_btn.setEnabled(has_points)

        # Two route-string rows: ICAO Field 15 (always visible — its empty-state
        # text is the panel's "no route yet" hint), and the Hebrew paperwork
        # variant (only shown when there's actually a route to render, since one
        # empty-state line is enough). Both honour the include-intermediates
        # checkbox so the strings stay in sync about *what* they're describing.
        include = self._include_intermediates_chk.isChecked()
        icao = to_icao_route_string(route, include_intermediates=include)
        if icao:
            self._route_string_label.setText(icao)
            self._route_string_label.setStyleSheet(
                "font-family: 'Consolas', 'Courier New', monospace; font-weight: bold;"
            )
        else:
            self._route_string_label.setText(
                "Route is empty — Shift+left-click a chart waypoint to add it."
            )
            # Empty-state text functions as an instruction; promote it to the
            # same bright-white treatment as the footer hint so it doesn't
            # read as muted/disabled placeholder copy.
            self._route_string_label.setStyleSheet("color: #ffffff;")

        hebrew = to_hebrew_route_string(route, include_intermediates=include)
        if hebrew:
            self._hebrew_string_label.setText(hebrew)
            # Bold + a touch larger than the body so the Hebrew row reads as
            # peer-equivalent to the ICAO row above. We deliberately don't force
            # a monospaced family here — Hebrew glyphs render poorly in
            # Consolas/Courier on most systems, and the user is copy-pasting
            # into Word/forms where the destination font wins anyway.
            self._hebrew_string_label.setStyleSheet("font-weight: bold;")
            self._hebrew_string_label.show()
        else:
            self._hebrew_string_label.hide()

        # Totals row: only meaningful with a real leg in the route. Always
        # reflects the *flown* polyline, regardless of include_intermediates.
        # The total honours per-leg distance overrides so the row math adds
        # up to what the table cells display (otherwise an overridden 12.3*
        # leg would silently revert to its computed value in the total).
        if segs:
            total_nm = sum(self._effective_distance_nm(s) for s in segs)
            total_secs = sum(
                self._effective_time_seconds(s, speed_kts) for s in segs
            )
            # Max-altitude suffix on the totals line. Folded into the
            # same string (rather than its own label) so the line still
            # reads as one logical "what does this route look like at a
            # glance" answer; the dot separator matches the existing
            # ``nm · time at kts`` rhythm. Renders ``"unknown"`` when
            # *no* leg has an altitude (chart not calibrated, all legs
            # missed by the matcher, etc.) so the field's presence
            # itself communicates "we tried to compute this and the
            # answer is unknown" — more useful than silently dropping
            # the suffix.
            max_alt = self._effective_max_altitude_ft()
            max_alt_text = f"{max_alt} ft" if max_alt is not None else _ALT_UNKNOWN_TEXT
            self._totals_label.setText(
                f"Total: {total_nm:.1f} nm · {format_hms(total_secs)} "
                f"at {int(speed_kts)} kt · Max route alt: {max_alt_text}"
            )
            self._totals_label.show()

            # Per-segment altitudes. We tolerate a missing or short list by
            # falling back to the empty tuple (rendered as "unknown") rather
            # than indexing past the end — that way a controller race during
            # calibration changes can't crash the panel.
            alt_list = self._last_altitudes_per_segment or []
            # Suppress the data-changed handler's override-write path
            # while we rebuild the table. ``appendRow`` doesn't fire
            # ``dataChanged`` for a brand-new row, but the per-cell
            # ``setText`` calls inside the cell factories can — and
            # without this guard a render would overwrite the override
            # store with the freshly-rendered text.
            self._suspend_data_changed = True
            try:
                for idx, seg in enumerate(segs):
                    alts = alt_list[idx] if idx < len(alt_list) else ()
                    leg_key = (seg.from_label, seg.to_label)
                    stored = self._atc_inputs.get(leg_key, {})
                    overrides = self._cell_overrides.get(leg_key, {})
                    eff_time_secs = self._effective_time_seconds(seg, speed_kts)
                    # "Reporting" + "Type" describe the *destination* of
                    # this leg (the TO point) — i.e. what the pilot will
                    # be reporting on arrival. Intermediates have
                    # neither, so both cells stay empty for sub-segment
                    # rows.
                    row = [
                        _endpoint_cell(seg.from_point, seg.from_label),
                        _endpoint_cell(seg.to_point, seg.to_label),
                        _reporting_name_cell(seg.to_point),
                        _reporting_type_cell(seg.to_point),
                        _ctr_cell(stored.get(_COL_CTR, ""), is_new=False),
                        _freq_cell(stored.get(_COL_FREQ, "")),
                        _ctr_cell(stored.get(_COL_NEW_CTR, ""), is_new=True),
                        _freq_cell(stored.get(_COL_NEW_FREQ, "")),
                        _mag_brg_cell(
                            seg.mag_bearing_deg,
                            override_str=overrides.get(_COL_MAG_BRG),
                        ),
                        _altitude_cell(
                            alts, override_str=overrides.get(_COL_ALT)
                        ),
                        _dist_cell(
                            seg.distance_nm,
                            override_str=overrides.get(_COL_DIST),
                        ),
                        _numeric_cell(format_hms(eff_time_secs)),
                    ]
                    self._model.appendRow(row)
            finally:
                self._suspend_data_changed = False
        else:
            self._totals_label.hide()
            # Origin-only state: no segments to render but a single
            # waypoint has been placed. Show one table row with the FROM
            # cell populated (so the user can confirm which fix they
            # picked and click through to the external map) and every
            # other column empty — the segment-derived fields (TO, MAG
            # BRG, altitude, distance, time) only make sense once a
            # second point exists, so leaving them blank reads as
            # "waiting for the next click" rather than asserting bogus
            # values. The origin row pairs with the on-chart origin dot
            # so the user gets the same confirmation in both views.
            points = route.points()
            if len(points) == 1:
                origin = points[0]
                origin_label = route.display_labels()[0]
                # Origin-only row: every other cell is blank including the
                # four ATC columns. The user *could* still want to file
                # a starting frequency (ground/clearance at the origin
                # airfield), so the four ATC cells are still editable —
                # they just default to empty.
                row = [_endpoint_cell(origin, origin_label)] + [
                    _empty_cell() for _ in range(len(_ROUTE_TABLE_COLS) - 1)
                ]
                self._model.appendRow(row)

        # Resize rows to whatever fits the *current* contents (including
        # multi-line altitude cells). Column sizing splits two ways:
        #
        #   * **Non-airplane mode** — call ``resizeColumnsToContents`` and
        #     re-pin the natural width so the trailing stretch widget
        #     (not the table) absorbs any slack horizontal space.
        #
        #   * **Airplane mode** — call
        #     :meth:`_redistribute_airplane_column_widths`, which itself
        #     does a ``resizeColumnsToContents`` pass first and then
        #     proportionally distributes leftover viewport width across
        #     all visible columns. Calling ``resizeColumnsToContents``
        #     here in airplane mode would actually undo the
        #     redistribution because Qt resets the Interactive widths to
        #     content widths during the call. The redistribution does
        #     its own measurement + restoration; we just route through
        #     it.
        self._table.resizeRowsToContents()
        if self._airplane_mode:
            self._redistribute_airplane_column_widths()
        else:
            self._table.resizeColumnsToContents()
            self._apply_table_natural_width()

    def _on_model_data_changed(
        self, top_left: QModelIndex, bottom_right: QModelIndex, _roles: list[int]
    ) -> None:
        """Mirror user edits in editable columns to the per-leg persistence
        dicts (``_atc_inputs`` for the four ATC columns, ``_cell_overrides``
        for MAG BRG / Alt / Dist).

        Fires for every model edit, so we filter to editable columns
        before touching either persistence dict — segment-driven
        re-renders emit ``dataChanged`` too, and the
        ``_suspend_data_changed`` guard plus the column filter together
        make sure the storage dicts only ever get written with
        user-typed values, never with the freshly-rendered cosmetic
        text (which for an override would include the asterisk + unit
        suffix and round-trip lossy).

        Keyed by ``(from_label, to_label)`` so a re-render after speed /
        route change reproduces the typed values for legs that survive.
        Override columns also re-render once after a successful commit
        so the cell repaints with the asterisk + unit suffix and the
        Time / totals cells pick up the new effective distance.
        """
        if self._suspend_data_changed:
            return
        col0 = top_left.column()
        col1 = bottom_right.column()
        # Quick bail-out if nothing in the changed range is editable.
        if not any(
            (c in _USER_INPUT_COLS or c in _OVERRIDABLE_COLS)
            for c in range(col0, col1 + 1)
        ):
            return
        any_override_change = False
        for row in range(top_left.row(), bottom_right.row() + 1):
            from_item = self._model.item(row, _COL_FROM)
            to_item = self._model.item(row, _COL_TO)
            if from_item is None or to_item is None:
                continue
            from_label = _strip_intermediate_prefix(from_item.text())
            to_label = _strip_intermediate_prefix(to_item.text())
            if not from_label or not to_label:
                continue  # origin-only row — TO is blank
            leg_key = (from_label, to_label)
            for col in range(col0, col1 + 1):
                cell = self._model.item(row, col)
                if cell is None:
                    continue
                value = cell.text()
                if col in _USER_INPUT_COLS:
                    if value:
                        self._atc_inputs.setdefault(leg_key, {})[col] = value
                    else:
                        self._atc_inputs.get(leg_key, {}).pop(col, None)
                        if (
                            leg_key in self._atc_inputs
                            and not self._atc_inputs[leg_key]
                        ):
                            del self._atc_inputs[leg_key]
                elif col in _OVERRIDABLE_COLS:
                    # Empty value commits the cell back to its computed
                    # form (delegate's "user cleared the field" path).
                    if not value:
                        if (
                            leg_key in self._cell_overrides
                            and col in self._cell_overrides[leg_key]
                        ):
                            del self._cell_overrides[leg_key][col]
                            if not self._cell_overrides[leg_key]:
                                del self._cell_overrides[leg_key]
                            any_override_change = True
                        continue
                    parsed = _parse_override(col, value)
                    if parsed is None:
                        # The delegate already rejects unparseable text
                        # before reaching the model, but defend in depth
                        # for any future code path that calls
                        # ``model.setData`` directly with bad input.
                        continue
                    canonical, _ = parsed
                    prior = self._cell_overrides.get(leg_key, {}).get(col)
                    if prior == canonical:
                        # Idempotent re-commit — no need to re-render
                        # for a no-op.
                        continue
                    self._cell_overrides.setdefault(leg_key, {})[col] = canonical
                    any_override_change = True
        if any_override_change and self._last_route is not None:
            # Re-render so the cell repaints with the asterisk + unit
            # suffix, the Time cell on the affected row picks up the
            # new effective distance, and the totals line refreshes.
            #
            # CRITICAL: when this handler fired in response to a delegate
            # commit, the table is still mid-commit (in EditingState) and
            # the editor widget is still parented to the viewport with a
            # live ``editor → index`` mapping. Calling ``_render`` here
            # would synchronously ``removeRows`` the row whose editor
            # just committed, breaking that mapping; the editor then
            # loses focus, Qt re-enters ``commitData`` on it, fails to
            # find an index, and prints
            #
            #   QAbstractItemView::commitData called with an editor that
            #   does not belong to this view
            #
            # to the console. The warning is harmless (the value is
            # already in the model) but it points at a real
            # use-after-free shape we want to avoid. Defer the re-render
            # to the next event loop iteration so Qt finishes the commit
            # cycle (closeEditor → editor destroyed → editor map
            # cleaned up) before we rebuild the rows.
            #
            # When the handler is reached *outside* an active edit (e.g.
            # ``model.setData`` called directly from a test or a future
            # controller-side override-injection API), we want
            # synchronous re-render semantics so the caller can read the
            # repainted cell straight after the commit. ``state()``
            # tells us which path we're on without having to plumb a
            # flag through every entry point.
            if self._table.state() == QAbstractItemView.State.EditingState:
                QTimer.singleShot(0, self._rerender_for_current_route)
            else:
                self._render(self._last_route, self.cruise_speed_kts())

    def _rerender_for_current_route(self) -> None:
        """Re-render hook used by the deferred-render path in
        :meth:`_on_model_data_changed`. Pulled out as a named method
        so the ``QTimer.singleShot`` connection is debuggable in a
        traceback (lambda would show up as ``<lambda>`` and obscure
        the call site)."""
        if self._last_route is not None:
            self._render(self._last_route, self.cruise_speed_kts())

    # ------------------------------------------------------------------
    # Override helpers
    # ------------------------------------------------------------------

    def _effective_distance_nm(self, seg: RouteSegment) -> float:
        """Return the per-leg distance the table renders, totals, and
        time-recompute logic should all agree on.

        That's the user's manually-typed override if present and
        parseable, otherwise the segment's computed great-circle
        distance. A stored-but-unparseable value (which should never
        happen because ``_parse_override`` is the only writer of
        ``_cell_overrides``) silently falls back to the computed
        value rather than poisoning the math.
        """
        leg_key = (seg.from_label, seg.to_label)
        raw = self._cell_overrides.get(leg_key, {}).get(_COL_DIST)
        if raw:
            parsed = _parse_override(_COL_DIST, raw)
            if parsed is not None:
                _, value = parsed
                return float(value)  # type: ignore[arg-type]
        return float(seg.distance_nm)

    def _effective_time_seconds(
        self, seg: RouteSegment, speed_kts: float
    ) -> float:
        """Time = effective_distance / cruise_speed. Time itself is not
        directly editable per spec — overriding ``Dist`` is the path
        through which a hand-edited time enters the table."""
        return segment_time_seconds(self._effective_distance_nm(seg), speed_kts)

    def _effective_altitudes_for_segment(
        self, seg: RouteSegment, computed: tuple[int, ...]
    ) -> tuple[int, ...]:
        """Per-leg altitude tuple the renderer / max-alt totals should
        agree on. Override wins over the controller-supplied computed
        value; an unparseable stored override silently falls back to
        the computed value (defence-in-depth — ``_parse_override`` is
        the only writer of ``_cell_overrides`` so this branch should
        never fire in practice).

        ``computed`` comes from ``_last_altitudes_per_segment`` (or
        ``()`` when the controller hasn't supplied data for this leg)
        — already the same tuple the cell factory consumes, so the
        max-alt math and the cell display can never disagree."""
        leg_key = (seg.from_label, seg.to_label)
        raw = self._cell_overrides.get(leg_key, {}).get(_COL_ALT)
        if raw:
            parsed = _parse_override(_COL_ALT, raw)
            if parsed is not None:
                _, values = parsed
                return tuple(int(v) for v in values)  # type: ignore[arg-type]
        return computed

    def _effective_max_altitude_ft(self) -> int | None:
        """Maximum altitude (ft) across every leg of the current route.

        Stacked-altitude legs contribute their own per-leg maximum to
        the route-wide maximum (a ``"1600 over 800"`` chart label
        means the leg climbs as high as 1600 ft, so 1600 is what
        propagates). User overrides win over computed values so an
        override-driven climb correctly raises the route-wide max.

        Returns ``None`` when no leg has any altitude data — the
        totals-line caller renders that as ``"unknown"`` rather than
        suppressing the suffix entirely, so the field's presence
        documents *what was attempted* even when the answer is
        unavailable.
        """
        if self._last_route is None:
            return None
        segs = self._last_route.segments()
        if not segs:
            return None
        alt_list = self._last_altitudes_per_segment or []
        max_so_far: int | None = None
        for idx, seg in enumerate(segs):
            computed = alt_list[idx] if idx < len(alt_list) else ()
            alts = self._effective_altitudes_for_segment(seg, computed)
            if not alts:
                continue
            leg_max = max(alts)
            if max_so_far is None or leg_max > max_so_far:
                max_so_far = leg_max
        return max_so_far

    def _has_override_at(self, row: int, col: int) -> bool:
        """``True`` iff the cell at (row, col) currently displays a
        user-typed override (i.e. the cell factory tagged it with
        ``_ROLE_HAS_OVERRIDE``). Used by the cell-context-menu
        dispatcher to decide whether to offer the restore action."""
        item = self._model.item(row, col)
        if item is None:
            return False
        return bool(item.data(_ROLE_HAS_OVERRIDE))

    def _column_has_any_override(self, col: int) -> bool:
        """``True`` iff at least one cell in ``col`` currently displays
        an override. Used by the header-context-menu dispatcher to
        decide whether to enable "Restore all <col> values"."""
        if col not in _OVERRIDABLE_COLS:
            return False
        for row in range(self._model.rowCount()):
            if self._has_override_at(row, col):
                return True
        return False

    def _restore_cell_override(self, row: int, col: int) -> None:
        """Drop the override at (row, col) and re-render so the cell
        repaints with the computed value + tooltip. No-op if the cell
        has no override or the row's leg key can't be resolved (e.g.
        the row was deleted between the right-click and the menu
        action — defensive)."""
        from_item = self._model.item(row, _COL_FROM)
        to_item = self._model.item(row, _COL_TO)
        if from_item is None or to_item is None:
            return
        leg_key = (
            _strip_intermediate_prefix(from_item.text()),
            _strip_intermediate_prefix(to_item.text()),
        )
        leg_overrides = self._cell_overrides.get(leg_key)
        if not leg_overrides or col not in leg_overrides:
            return
        del leg_overrides[col]
        if not leg_overrides:
            del self._cell_overrides[leg_key]
        if self._last_route is not None:
            self._render(self._last_route, self.cruise_speed_kts())

    def _restore_all_overrides_in_column(self, col: int) -> None:
        """Drop every override stored under ``col`` across every leg in
        ``_cell_overrides`` and re-render. No-op when the column has
        no overrides — the menu item should already have been disabled
        in that case, but the no-op makes the API self-defensive."""
        if col not in _OVERRIDABLE_COLS:
            return
        empty_keys: list[tuple[str, str]] = []
        any_dropped = False
        for leg_key, leg_overrides in self._cell_overrides.items():
            if col in leg_overrides:
                del leg_overrides[col]
                any_dropped = True
            if not leg_overrides:
                empty_keys.append(leg_key)
        for k in empty_keys:
            del self._cell_overrides[k]
        if any_dropped and self._last_route is not None:
            self._render(self._last_route, self.cruise_speed_kts())

    def _show_cell_context_menu(self, pos: QPoint) -> None:
        """Right-click handler on the table body. Currently the only
        action is "Restore computed value" on overridden cells; if the
        click landed on a cell without an override the menu is empty
        and Qt skips showing it (so plain right-click on a normal
        cell stays a no-op rather than popping an empty popup)."""
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        row, col = index.row(), index.column()
        if col not in _OVERRIDABLE_COLS or not self._has_override_at(row, col):
            return
        header_text = str(
            self._model.headerData(col, Qt.Orientation.Horizontal) or ""
        )
        menu = QMenu(self._table)
        action = QAction(f"Restore computed {header_text}", menu)
        action.setToolTip(
            f"Drop the manually-entered override and repaint the cell "
            f"with the geometry-/chart-derived computed value for "
            f"{header_text}."
        )
        action.triggered.connect(lambda _checked=False: self._restore_cell_override(row, col))
        menu.addAction(action)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _show_header_context_menu(self, pos: QPoint) -> None:
        """Right-click handler on the column header. Offers "Restore all
        <col> values" for the three overridable columns; the action is
        disabled when the column has no overrides to drop, so the user
        sees the affordance even when there's nothing to clean up
        (rather than a context menu that mysteriously appears and
        disappears)."""
        h_header = self._table.horizontalHeader()
        col = h_header.logicalIndexAt(pos)
        if col < 0 or col not in _OVERRIDABLE_COLS:
            return
        header_text = str(
            self._model.headerData(col, Qt.Orientation.Horizontal) or ""
        )
        menu = QMenu(self._table)
        action = QAction(f"Restore all {header_text} values", menu)
        action.setToolTip(
            f"Drop every manually-entered override in the {header_text} "
            "column for every leg in the current route, repainting each "
            "affected cell with its computed value."
        )
        action.setEnabled(self._column_has_any_override(col))
        action.triggered.connect(
            lambda _checked=False: self._restore_all_overrides_in_column(col)
        )
        menu.addAction(action)
        menu.exec(h_header.mapToGlobal(pos))

    def _copy_selection_to_clipboard(self) -> None:
        """Copy the current selection (or the whole table when nothing is
        selected) onto the clipboard as both an HTML table and a plain-
        text tab-separated dump.

        Word, Excel, and Outlook all read ``text/html`` first, so pasting
        into any of them lands the data as a real grid with the original
        cell colours preserved. The plain-text fallback covers terminal
        editors and the parts of GitHub Issues that strip HTML.
        """
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import QMimeData

        rows_cols = self._collect_selected_rows_cols()
        if not rows_cols:
            return
        rows, cols = rows_cols
        html = self._render_selection_as_html(rows, cols)
        plain = self._render_selection_as_plain(rows, cols)
        mime = QMimeData()
        mime.setHtml(html)
        mime.setText(plain)
        QGuiApplication.clipboard().setMimeData(mime)

    def _collect_selected_rows_cols(self) -> tuple[list[int], list[int]] | None:
        """Resolve the user's selection into sorted row + column index lists.

        Falls back to the entire table when the user pressed Ctrl+C with
        no explicit selection (the conventional "copy everything"
        gesture) so the shortcut always produces *something*. An empty
        model returns ``None`` and the caller silently no-ops.
        """
        n_rows = self._model.rowCount()
        n_cols = self._model.columnCount()
        if n_rows == 0 or n_cols == 0:
            return None

        selection = self._table.selectionModel()
        indexes = selection.selectedIndexes() if selection is not None else []
        if not indexes:
            return list(range(n_rows)), list(range(n_cols))
        rows = sorted({idx.row() for idx in indexes})
        cols = sorted({idx.column() for idx in indexes})
        return rows, cols

    def _render_selection_as_html(
        self, rows: list[int], cols: list[int]
    ) -> str:
        """Build a Word-friendly HTML ``<table>`` for the supplied rows
        and columns. Cell colours come from the model's foreground role
        so the magenta CTR / cyan New CTR styling is preserved on
        paste. The table is wrapped in a minimal HTML document so MIME
        consumers that look for a full payload don't choke."""
        header_cells = "".join(
            f"<th style=\"border:1px solid #888;padding:4px 8px;\">"
            f"{self._model.headerData(c, Qt.Orientation.Horizontal)}</th>"
            for c in cols
        )
        body_rows: list[str] = []
        for r in rows:
            cells: list[str] = []
            for c in cols:
                item = self._model.item(r, c)
                text = item.text() if item is not None else ""
                fg = item.foreground().color() if item is not None else QColor()
                colour_css = (
                    f"color:{fg.name(QColor.NameFormat.HexRgb)};"
                    if item is not None and fg.isValid() and fg.alpha() > 0
                    else ""
                )
                cells.append(
                    f"<td style=\"border:1px solid #888;padding:4px 8px;"
                    f"{colour_css}\">{_html_escape(text)}</td>"
                )
            body_rows.append("<tr>" + "".join(cells) + "</tr>")
        return (
            "<html><body><table style=\"border-collapse:collapse;\">"
            f"<thead><tr>{header_cells}</tr></thead>"
            f"<tbody>{''.join(body_rows)}</tbody>"
            "</table></body></html>"
        )

    def _render_selection_as_plain(
        self, rows: list[int], cols: list[int]
    ) -> str:
        """Fallback plain-text rendering — TSV-style with header row.

        Mirrors what users expect from a generic Ctrl+C on a table,
        but limited to the actually-selected cells (so a per-column
        copy doesn't dump the whole row)."""
        lines: list[str] = []
        header = "\t".join(
            str(self._model.headerData(c, Qt.Orientation.Horizontal)) for c in cols
        )
        lines.append(header)
        for r in rows:
            cells = []
            for c in cols:
                item = self._model.item(r, c)
                cells.append(item.text() if item is not None else "")
            lines.append("\t".join(cells))
        return "\n".join(lines)

    def _maybe_apply_table_natural_width(self) -> None:
        """Re-pin the natural width unless airplane mode owns column
        sizing.

        Thin guard around :meth:`_apply_table_natural_width` used by
        the vertical-scrollbar ``rangeChanged`` connection. In
        airplane mode the table is left free to span the full
        viewport width (no max-width pin) and column widths are
        managed by :meth:`_redistribute_airplane_column_widths`, so
        any re-pin would fight that pipeline and snap the table
        back to content width mid-flight when the scrollbar appears
        or disappears.
        """
        if self._airplane_mode:
            return
        self._apply_table_natural_width()

    def _apply_table_natural_width(self) -> None:
        """Pin the table's max width to exactly its content width.

        Mirrors the waypoint-table treatment on the right pane:
        - ``QHeaderView.length()`` gives Qt's authoritative column total (more
          reliable than summing per-section sizes when QSS adds section
          padding/borders).
        - The vertical scrollbar slot is reserved unconditionally, not gated on
          ``isVisible()`` — checking visibility races with Qt's layout pass and
          can mistakenly skip the reservation when the scrollbar is *about* to
          appear.
        - A 4-px breathing-room margin covers QSS padding/border quirks on
          ``QHeaderView::section`` and ``QTableView::item`` so the last column
          never ends up exactly flush with the viewport edge.
        """
        if self._model.columnCount() <= 0:
            return
        total = self._table.horizontalHeader().length()
        total += self._table.frameWidth() * 2
        total += self._table.verticalScrollBar().sizeHint().width()
        total += 4
        self._table.setMaximumWidth(total)


# ---------------------------------------------------------------------------
# Cell factories
# ---------------------------------------------------------------------------


def _endpoint_cell(point: RoutePoint, label: str) -> QStandardItem:
    """Build a FROM/TO cell.

    Both real waypoints and intermediates are clickable links — the only
    differences are colour (green vs grey) and the ``--> `` prefix on
    intermediates. Both carry a ``(label, lat, lon)`` payload so the table
    click handler dispatches them through the same code path; the controller
    opens the configured external map provider at those coordinates.

    The shared underline + colour split tells the eye "both clickable, but the
    grey ones are synthetic sub-points" without losing the visual hierarchy
    that real reporting points should dominate.
    """
    if point.is_waypoint and point.waypoint is not None:
        wp = point.waypoint
        it = QStandardItem(label)
        it.setEditable(False)
        it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        f = QFont(it.font())
        f.setUnderline(True)
        it.setFont(f)
        it.setForeground(QColor(WAYPOINT_CODE_LINK_GREEN))
        it.setToolTip(
            f"Open {wp.code} in the external map provider configured below the "
            "waypoint table (Bing / Google / Apple)."
        )
        it.setData((wp.code, float(wp.lat), float(wp.lon)), _ROLE_WP_PAYLOAD)
        return it

    it = QStandardItem(f"{_INTERMEDIATE_PREFIX}{label}")
    it.setEditable(False)
    it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    f = QFont(it.font())
    f.setUnderline(True)
    it.setFont(f)
    it.setForeground(QColor(_INTERMEDIATE_FOREGROUND))
    it.setToolTip(
        f"Intermediate route point at {point.lat:.4f}°, {point.lon:.4f}° — "
        "click to open these coordinates in the external map provider."
    )
    # Use the *display label* as the payload's first slot — it's what the user
    # sees (e.g. "DAROM.1") and is purely informational on the controller side
    # (the lat/lon are the actual driver of the URL).
    it.setData((label, float(point.lat), float(point.lon)), _ROLE_WP_PAYLOAD)
    return it


def _numeric_cell(text: str) -> QStandardItem:
    """Right-aligned, non-editable plain-text cell — used for MAG BRG / distance / time."""
    it = QStandardItem(text)
    it.setEditable(False)
    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return it


def _empty_cell() -> QStandardItem:
    """Blank, non-editable, non-clickable placeholder cell.

    Used by the origin-only table row where every column other than
    ``From`` has nothing to render — TO/Reporting/Type/MAG BRG/Alt/Dist/
    Time are all segment-derived and a route with one point has no
    segments yet. Returning a styleless empty ``QStandardItem`` keeps
    the row geometry consistent with populated rows so the table still
    auto-sizes correctly.
    """
    it = QStandardItem("")
    it.setEditable(False)
    return it


def _ctr_cell(text: str, *, is_new: bool) -> QStandardItem:
    """Editable text cell for a CTR (controller) name.

    Two variants share this factory — ``CTR`` (the controller currently
    in contact at the leg's destination) and ``New CTR`` (the next-
    handoff controller). They differ only in foreground colour; both
    accept any alphanumeric string the user types. Validation is
    deliberately permissive on these cells because real-world VATSIM
    CTR call-signs vary widely (``LLBG_TWR``, ``Tel-Aviv RDR``, the
    occasional Hebrew transliteration) and the program's job is to
    surface the field, not to second-guess the user's typing.

    Magenta vs cyan was chosen to read on both light and dark themes
    (the colour pair is the canonical print-vs-screen complementary
    that doesn't blend into either background tier of the alternating
    rows) and to give the user a left-to-right "now → next" gradient
    when scanning the row.
    """
    it = QStandardItem(text)
    it.setEditable(True)
    it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    it.setForeground(
        QColor(_NEW_CTR_TEXT_COLOR if is_new else _CTR_TEXT_COLOR)
    )
    f = QFont(it.font())
    f.setBold(True)
    it.setFont(f)
    it.setToolTip(
        "Click or double-click to type the controller's call-sign for "
        "this reporting point. Free-form text — VATSIM call-signs, "
        "informal short-codes, or transliterations are all accepted."
    )
    return it


def _freq_cell(text: str) -> QStandardItem:
    """Editable text cell for an ATC frequency in MHz.

    Validation (``XXX.Y`` or ``XXX.YYY``) lives on the cell's editor
    (see :class:`_FrequencyCellDelegate`) so the user can never commit
    a malformed value through the GUI. The cell itself just renders the
    stored text right-aligned and keeps a tooltip describing the format.
    Empty text is intentionally allowed: a leg may have no assigned
    frequency yet at the time of planning, and forcing the user to fill
    something in would be worse than leaving it blank.
    """
    it = QStandardItem(text)
    it.setEditable(True)
    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    it.setToolTip(
        "ATC frequency in MHz. Format: XXX.Y or XXX.YYY (e.g. 118.4, "
        "118.475). Leave empty if not yet assigned. Anything outside "
        "this format is rejected on commit."
    )
    return it


def _reporting_name_cell(point: RoutePoint) -> QStandardItem:
    """Hebrew name of the TO waypoint as a blue link, or empty for intermediates.

    Mirrors the master waypoint table's blue-name interaction: clicking centres
    the map on this waypoint (same zoom). Having the same affordance in both
    tables means a planned waypoint can be located on the chart without
    scrolling back to the right pane.

    Default left alignment is the right call here — Qt's bidi engine handles
    Hebrew RTL glyph order natively while keeping the cell content positioned
    consistently with every other column.
    """
    if point.waypoint is None or not point.waypoint.name_he:
        it = QStandardItem("")
        it.setEditable(False)
        it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return it
    it = QStandardItem(point.waypoint.name_he)
    it.setEditable(False)
    it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    f = QFont(it.font())
    f.setUnderline(True)
    it.setFont(f)
    it.setForeground(QColor(WAYPOINT_NAME_LINK_BLUE))
    it.setToolTip(
        "Center the map on this waypoint on the calibrated chart (keeps current zoom)."
    )
    it.setData(point.waypoint.code, _ROLE_REPORTING_PAYLOAD)
    return it


def _parse_override(col: int, raw: str) -> tuple[str, object] | None:
    """Validate + canonicalise a user-typed override for an overridable column.

    Returns ``(canonical_storage_text, parsed_value)`` on success — the
    storage text is what we put in ``_cell_overrides`` (whitespace
    stripped, comma-list normalised) and ``parsed_value`` is the
    computational form (``int`` for MAG BRG, ``tuple[int, ...]`` for
    Alt, ``float`` for Dist) used by the renderer and the totals
    line. Returns ``None`` for any unparseable input — the caller
    must treat that as "leave the existing value alone".

    Empty/whitespace-only ``raw`` returns ``None`` so the data-changed
    handler can route a cleared cell through its "remove the override"
    branch instead of the validation path.

    The regexes in ``_*_OVERRIDE_REGEX`` are the gate; this function
    re-runs them and then applies any column-specific value-range
    checks (e.g. MAG BRG ≤ 360, Alt > 0). Splitting "regex syntax" from
    "value range" keeps each level cheap and independently testable.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if col == _COL_MAG_BRG:
        if not re.match(_MAG_BRG_OVERRIDE_REGEX, text):
            return None
        try:
            value = int(text)
        except ValueError:
            return None
        if not 0 <= value <= 360:
            return None
        # Normalise to a 3-digit storage form so a typed "46" and a
        # typed "046" round-trip through the override store identically.
        return f"{value:03d}", value
    if col == _COL_ALT:
        if not re.match(_ALT_OVERRIDE_REGEX, text):
            return None
        try:
            parts = tuple(int(p.strip()) for p in text.split(","))
        except ValueError:
            return None
        if any(p <= 0 for p in parts):
            return None
        # Storage form: comma-separated integers without whitespace, so a
        # later ``"1600,  800"`` typing variant doesn't create a second
        # logically-identical override key.
        return ",".join(str(p) for p in parts), parts
    if col == _COL_DIST:
        if not re.match(_DIST_OVERRIDE_REGEX, text):
            return None
        try:
            value_f = float(text)
        except ValueError:
            return None
        if value_f <= 0.0:
            return None
        # Normalise to one decimal place so the computed-value formatter
        # and the override formatter line up visually after a re-render
        # (e.g. typing "12" and typing "12.0" both render as "12.0*").
        return f"{value_f:.1f}", value_f
    return None


def _format_mag_brg_text(degrees: float) -> str:
    """Render a magnetic bearing in the ``046°M`` form used in the table.

    The integer printed by the Israeli CVFR chart for a given leg is
    consistently one less than what naive ``round(continuous)`` would
    yield — measured empirically against 15 chart legs (see
    ``tests/test_route_chart_headings.py``). The drafter's effective
    convention is ``floor(continuous − 0.5)``, i.e. round-half-down
    with a 0.5° downward bias. Equivalent reformulations:

        floor(continuous − 0.5)
        ≡ floor(true_bearing − (var_E + 0.5°))
        ≡ floor(true_bearing − 5.5°)            # at chart var = 5°E
        ≡ round(true_bearing − 6.0°)            # round-half-to-even

    All four predict the same integer in every case. We pick the
    first formulation so the two moving parts stay independently
    legible: ``ISRAEL_MAGNETIC_VARIATION_DEG_E = 5.0`` matches the
    chart legend's printed "VAR 5°E 2025" verbatim, and the extra
    0.5° offset lives in the display layer as a drafter-convention
    adjustment (not a quiet bump to the variation constant). The
    likely physical origin of the 0.5° gap is mid-cycle eastward
    drift the chart's Hebrew note acknowledges — chart prints to be
    valid through the cycle, not just at issue date.

    Under this model 13/15 chart legs render the chart's printed
    value exactly and the remaining 2 are within ±1° (those two
    involve airport-grade fixes whose ARP coordinates likely differ
    slightly from what the drafter measured against — see the
    outlier-pinning test in ``test_route_chart_headings.py``).

    The ``% 360`` wrap is defensive: ``magnetic_bearing_deg`` already
    returns values in ``[0, 360)``, but ``math.floor`` of e.g.
    a slightly-negative bearing would otherwise format as ``-1``.

    Used only for **computed** bearings (the dataclass ``mag_bearing_deg``
    coming out of :func:`magnetic_bearing_deg`). User-typed integer
    overrides go through :func:`_format_mag_brg_override_text` instead,
    which renders them verbatim — the chart-drafter rounding only
    applies to the conversion from a continuous magnetic bearing to
    its printed integer, and a user who types ``"120"`` expects to see
    ``"120°M"`` back, not ``"119°M"``.
    """
    return f"{int(math.floor(degrees - 0.5)) % 360:03d}°M"


def _format_mag_brg_override_text(value: int) -> str:
    """Render an integer **override** value in the ``046°M`` form.

    Separate from :func:`_format_mag_brg_text` because the chart-
    drafter half-degree downward adjustment in that function only
    makes sense when converting a continuous magnetic bearing to its
    printed integer. Override values arrive already integer-quantised
    via :func:`_parse_override`, so any further rounding would alter
    what the user explicitly typed. ``"120"`` in must come back as
    ``"120°M"`` out — the override-tier regression in
    ``tests/test_route_panel.py`` pins this contract.
    """
    return f"{int(value) % 360:03d}°M"


def _format_dist_text(nm: float) -> str:
    """Render a distance in the right-aligned ``  12.3`` form used in the
    table. Width-padded to match the existing computed cell (the
    asterisk suffix is added separately by the cell factory).
    """
    return f"{nm:6.1f}"


def _mag_brg_cell(
    computed_deg: float, override_str: str | None = None
) -> QStandardItem:
    """Build the ``MAG BRG`` cell.

    If ``override_str`` is set, parse it back to an int and render the
    user's value with the override colour + asterisk suffix; otherwise
    render the computed bearing in the standard right-aligned format.
    Either way the cell is editable so the user can replace / re-edit
    the value via the standard double-click flow.
    """
    parsed = _parse_override(_COL_MAG_BRG, override_str) if override_str else None
    if parsed is not None:
        _, value = parsed
        text = f"{_format_mag_brg_override_text(int(value))}{_OVERRIDE_SUFFIX}"
        it = QStandardItem(text)
        it.setEditable(True)
        it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        it.setForeground(QColor(_OVERRIDE_COLOR))
        it.setData(True, _ROLE_HAS_OVERRIDE)
        it.setToolTip(
            f"Overridden MAG BRG: {value:03d}° (manually entered).\n"
            f"Computed value: {_format_mag_brg_text(computed_deg)}.\n\n"
            "Right-click to restore the computed value."
        )
        return it
    it = QStandardItem(_format_mag_brg_text(computed_deg))
    it.setEditable(True)
    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    it.setToolTip(
        "Computed magnetic bearing for this leg.\n\n"
        "Double-click to override with a manually-typed value (1–360°). "
        "Overridden cells are shown in red with an asterisk; right-click "
        "to restore."
    )
    return it


def _dist_cell(
    computed_nm: float, override_str: str | None = None
) -> QStandardItem:
    """Build the ``Dist (nm)`` cell.

    Distance overrides feed back into the per-leg time computation (the
    Time cell in the same row recomputes from the override + cruise
    speed) and into the ``Total: X nm at Y kt`` line above the table.
    """
    parsed = _parse_override(_COL_DIST, override_str) if override_str else None
    if parsed is not None:
        _, value = parsed
        text = f"{_format_dist_text(float(value))}{_OVERRIDE_SUFFIX}"
        it = QStandardItem(text)
        it.setEditable(True)
        it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        it.setForeground(QColor(_OVERRIDE_COLOR))
        it.setData(True, _ROLE_HAS_OVERRIDE)
        it.setToolTip(
            f"Overridden distance: {value:.1f} nm (manually entered).\n"
            f"Computed value: {computed_nm:.1f} nm.\n\n"
            "The Time cell recomputes from the overridden distance and "
            "the current cruise speed.\n\n"
            "Right-click to restore the computed value."
        )
        return it
    it = QStandardItem(_format_dist_text(computed_nm))
    it.setEditable(True)
    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    it.setToolTip(
        "Computed great-circle distance for this leg.\n\n"
        "Double-click to override with a manually-typed value (positive "
        "decimal nm). Time recomputes automatically. Overridden cells "
        "are shown in red with an asterisk; right-click to restore."
    )
    return it


def _altitude_cell(
    altitudes: tuple[int, ...], override_str: str | None = None
) -> QStandardItem:
    """Render a tuple of altitudes (top-to-bottom on the chart) as a single
    multi-line right-aligned cell, or ``"unknown"`` when the tuple is empty.

    The newline-separated rendering preserves the chart's visual stacking —
    on the printed chart a "1600 over 800" arrow is read top-down, and that's
    exactly how the cell wraps. The text-selectable interaction below
    means the user can copy the value out of the cell into a flight log
    without re-typing.

    A muted grey is used for the "unknown" placeholder so a missing altitude
    doesn't draw the eye more than a real one — its job is only to confirm
    the column rendered, not to be salient.

    When ``override_str`` is set, the user's manually-typed altitudes are
    rendered with the override colour and an asterisk per line (preserves
    the per-line stacking even for multi-altitude overrides like
    ``"1600,800"``).
    """
    parsed = _parse_override(_COL_ALT, override_str) if override_str else None
    if parsed is not None:
        _, values = parsed
        text = "\n".join(f"{v}{_OVERRIDE_SUFFIX}" for v in values)
        it = QStandardItem(text)
        it.setEditable(True)
        it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        it.setForeground(QColor(_OVERRIDE_COLOR))
        it.setData(True, _ROLE_HAS_OVERRIDE)
        if altitudes:
            computed_text = ", ".join(str(v) for v in altitudes)
        else:
            computed_text = _ALT_UNKNOWN_TEXT
        it.setToolTip(
            "Overridden altitude (manually entered): "
            + ", ".join(str(v) for v in values)
            + " ft.\nComputed value: "
            + computed_text
            + ".\n\nRight-click to restore the computed value."
        )
        return it

    if not altitudes:
        it = QStandardItem(_ALT_UNKNOWN_TEXT)
        it.setEditable(True)
        it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        it.setForeground(QColor("#888"))
        it.setToolTip(
            "No altitude arrow matched this segment.\n\n"
            "Possible reasons:\n"
            "• the chart isn't calibrated yet (set both anchor pairs);\n"
            "• the chart doesn't print an altitude for this direction\n"
            "  (some legs are one-way only on the chart);\n"
            "• the segment runs over a region not covered by either chart;\n"
            "• the segment slightly deviates from the chart's drawn route\n"
            "  line — try inspecting the chart for nearby altitude labels.\n\n"
            "Double-click to override with a manually-typed altitude "
            "(or comma-separated stack like 1600,800)."
        )
        return it

    text = "\n".join(str(v) for v in altitudes)
    it = QStandardItem(text)
    it.setEditable(True)
    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if len(altitudes) >= 2:
        hi = max(altitudes)
        lo = min(altitudes)
        it.setToolTip(
            f"CVFR route altitude band for this leg: {lo} ft to {hi} ft.\n"
            f"Stacked label on chart: " + ", ".join(str(v) for v in altitudes) + ".\n\n"
            "Double-click to override with a manually-typed altitude "
            "(or comma-separated stack)."
        )
    else:
        it.setToolTip(
            f"CVFR route altitude for this leg: {altitudes[0]} ft.\n\n"
            "Double-click to override with a manually-typed altitude "
            "(or comma-separated stack like 1600,800)."
        )
    return it


def _reporting_type_cell(point: RoutePoint) -> QStandardItem:
    """Reporting-type label (חובה / דרישה / ARP) coloured per
    :data:`waypoint_styles.REPORTING_TYPE_COLORS`. Empty for intermediates."""
    if point.waypoint is None:
        it = QStandardItem("")
        it.setEditable(False)
        it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return it
    text = point.waypoint.reporting_type or ""
    it = QStandardItem(text)
    it.setEditable(False)
    it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    colour = REPORTING_TYPE_COLORS.get(text.strip())
    if colour is not None:
        it.setForeground(QColor(colour))
    return it


def _strip_intermediate_prefix(text: str) -> str:
    """Strip the ``--> `` prefix that ``_endpoint_cell`` prepends to
    intermediate-point labels so the persistence key matches the bare
    waypoint code (``DAROM.1`` rather than ``--> DAROM.1``).
    Idempotent on real-waypoint cells which never carry the prefix."""
    if text.startswith(_INTERMEDIATE_PREFIX):
        return text[len(_INTERMEDIATE_PREFIX) :]
    return text


def _html_escape(text: str) -> str:
    """Minimal HTML entity escape for the table-copy payload.

    Stops Word/Excel from interpreting ATC call-signs that happen to
    contain ``<`` or ``&`` as markup. Single/double quotes are not
    escaped since they're legal CSS-attribute fillers and we wrap our
    inline styles in double quotes.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Custom QTableView + delegates
# ---------------------------------------------------------------------------


class _RouteTableView(QTableView):
    """Thin ``QTableView`` subclass.

    Two reasons for the subclass:

    1. We attach a ``QShortcut`` for ``Ctrl+C`` in :class:`RoutePanel` and
       want a stable parent that survives style polish without the shortcut
       getting swallowed by editor focus stealing — owning the view as a
       distinct type makes future selector-based QSS straightforward.

    2. ``QTableView`` itself is otherwise sufficient; no extra signals or
       behaviours are needed today, but having the type hook here avoids
       a refactor when those naturally accumulate.
    """

    pass


class _FrequencyCellDelegate(QStyledItemDelegate):
    """Item delegate for the two ``Freq`` / ``New Freq`` columns.

    Constructs a ``QLineEdit`` editor wired to a ``QRegularExpressionValidator``
    matching :data:`_FREQUENCY_REGEX` (``XXX.Y`` or ``XXX.YYY``). The
    validator runs on every keystroke, *and* on commit via ``setModelData``
    so an aborted edit (e.g. user types ``118.``, hits Tab) cannot smuggle
    a partial value into the model. Empty strings are explicitly allowed
    so the user can also clear an existing entry.

    We intentionally *don't* enforce a real-world frequency band check
    (108–137 MHz aviation VHF, 118–137 MHz comm sub-band): real VATSIM
    sessions occasionally use out-of-band test frequencies and we'd
    rather show a typo than lose a legitimate entry. The format gate is
    the strict line; the value gate is the user's responsibility.
    """

    def createEditor(self, parent, _option, _index) -> QLineEdit:  # noqa: ANN001
        editor = QLineEdit(parent)
        regex = QRegularExpression(_FREQUENCY_REGEX)
        editor.setValidator(QRegularExpressionValidator(regex, editor))
        editor.setPlaceholderText("XXX.Y or XXX.YYY")
        return editor

    def setModelData(self, editor: QLineEdit, model, index) -> None:  # noqa: ANN001
        """Reject malformed input on commit — re-validate the final string
        against the regex before letting it land in the model. An empty
        string is treated as "clear the cell" and accepted."""
        text = editor.text().strip()
        if text and not re.match(_FREQUENCY_REGEX, text):
            return  # silently drop the commit; cell keeps its previous value
        model.setData(index, text, Qt.ItemDataRole.EditRole)


class _OverridableCellDelegate(QStyledItemDelegate):
    """Item delegate for the three override-capable columns
    (``MAG BRG``, ``Alt (ft)``, ``Dist (nm)``).

    Two responsibilities, both about *separating display text from
    edit text* so the user-typed override round-trips cleanly through
    the cosmetic asterisk + unit-suffix decoration the cell display
    adds back on render:

    1. ``setEditorData`` strips the ``"*"`` suffix and any column-
       specific unit suffix (``"°M"`` for MAG BRG) from the cell's
       text before populating the editor. Without this, opening an
       editor on a ``"046°M*"`` cell would show ``"046°M*"`` and
       force the user to delete the cosmetic glyphs by hand. Stripping
       only the *cosmetic* parts (never the digits) keeps the original
       value pre-selected so a re-edit is one keystroke away.

    2. ``setModelData`` runs the per-column regex + value-range
       validation via :func:`_parse_override` and only lets a parseable
       value land in the model. Garbage input is silently dropped (the
       cell keeps its previous text) so the user's previous override —
       or the computed value — is never overwritten by a typo.
    """

    def __init__(self, column: int, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        if column not in _OVERRIDABLE_COLS:
            raise ValueError(
                f"_OverridableCellDelegate is only valid for {_OVERRIDABLE_COLS}, "
                f"not column {column}"
            )
        self._column = column

    def createEditor(self, parent, _option, _index) -> QLineEdit:  # noqa: ANN001
        editor = QLineEdit(parent)
        if self._column == _COL_MAG_BRG:
            editor.setPlaceholderText("0–360")
            regex = QRegularExpression(_MAG_BRG_OVERRIDE_REGEX)
        elif self._column == _COL_ALT:
            editor.setPlaceholderText("e.g. 1500 or 1600,800")
            regex = QRegularExpression(_ALT_OVERRIDE_REGEX)
        else:  # _COL_DIST
            editor.setPlaceholderText("nm (e.g. 12.3)")
            regex = QRegularExpression(_DIST_OVERRIDE_REGEX)
        editor.setValidator(QRegularExpressionValidator(regex, editor))
        return editor

    def setEditorData(self, editor: QLineEdit, index) -> None:  # noqa: ANN001
        """Pre-populate the editor with the *bare* numeric form of the
        cell's value — strip ``"°M"``/``"*"`` for MAG BRG, strip ``"*"``
        for Dist, strip ``"*"`` and join multi-line altitude stacks
        with commas for Alt. The user sees something they can directly
        re-type rather than something they have to surgically clean
        first."""
        raw = str(index.data(Qt.ItemDataRole.EditRole) or "")
        if self._column == _COL_MAG_BRG:
            cleaned = raw.replace("°M", "").replace(_OVERRIDE_SUFFIX, "").strip()
        elif self._column == _COL_ALT:
            # Multi-line altitude cell: ``"1600\n800"`` (or asterisked
            # ``"1600*\n800*"``) becomes ``"1600,800"`` for editing.
            # An ``"unknown"`` cell produces an empty editor.
            stripped = raw.replace(_OVERRIDE_SUFFIX, "").strip()
            if stripped == _ALT_UNKNOWN_TEXT:
                cleaned = ""
            else:
                parts = [p.strip() for p in stripped.splitlines() if p.strip()]
                cleaned = ",".join(parts)
        else:  # _COL_DIST
            cleaned = raw.replace(_OVERRIDE_SUFFIX, "").strip()
        editor.setText(cleaned)
        editor.selectAll()

    def setModelData(self, editor: QLineEdit, model, index) -> None:  # noqa: ANN001
        text = editor.text().strip()
        if not text:
            # Empty commit ⇒ "clear my override". Write the empty
            # sentinel so the panel's data-changed handler sees the
            # change and can route it through the remove-override
            # branch. The next render will repaint the cell with the
            # computed value.
            model.setData(index, "", Qt.ItemDataRole.EditRole)
            return
        parsed = _parse_override(self._column, text)
        if parsed is None:
            return  # silently drop; cell keeps previous text
        canonical, _value = parsed
        # Hand the *canonical* override text to the model so a typed
        # ``"  46  "`` and a typed ``"046"`` round-trip identically.
        # The data-changed handler then stores this string verbatim
        # in ``_cell_overrides`` and triggers a re-render that
        # reformats it with the unit suffix + asterisk.
        model.setData(index, canonical, Qt.ItemDataRole.EditRole)
