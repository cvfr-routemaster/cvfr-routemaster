"""Font-settings persistence + dialog + theme-application contract tests.

Three layers under test:

1. **Persistence** (``settings_store.load_font_sizes`` /
   ``save_font_sizes`` plus their ``load_airplane_font_sizes`` /
   ``save_airplane_font_sizes`` siblings) — round-trip integer
   values through ``QSettings`` with defaults for unset keys,
   independently per profile.
2. **Theme application** (``ui_theme.apply_dark_theme``) — the
   resulting ``QApplication.styleSheet()`` must contain the
   user-chosen ``font-size`` rules for the three selectors
   (``QTableView``, ``QLabel#routeText``, ``QLabel#mapHint``).
3. **Dialog** (``FontSettingsDialog``) — seeds spinboxes from the
   incoming ``FontSizes`` (both profiles), clamps out-of-range
   incoming values, and ``chosen_sizes()`` /
   ``chosen_airplane_sizes()`` return the user's edits.

We isolate ``QSettings`` per-test by monkey-patching
``settings_store._settings`` to point at a temp INI file, so the
tests never touch the user's real CVFR Route Master settings.
"""

from __future__ import annotations

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from cvfr_routemaster import settings_store  # noqa: E402
from cvfr_routemaster.font_settings_dialog import FontSettingsDialog  # noqa: E402
from cvfr_routemaster.settings_store import (  # noqa: E402
    DEFAULT_AIRPLANE_HINT_FONT_PX,
    DEFAULT_AIRPLANE_ROUTE_TEXT_FONT_PX,
    DEFAULT_AIRPLANE_TABLE_FONT_PX,
    DEFAULT_HINT_FONT_PX,
    DEFAULT_ROUTE_TEXT_FONT_PX,
    DEFAULT_TABLE_FONT_PX,
    DEFAULT_TRAFFIC_ICON_SIZE_PX,
    DEFAULT_WAYPOINT_MARKER_SIZE_PX,
    FONT_SIZE_MAX_PX,
    FONT_SIZE_MIN_PX,
    SHIPPED_FONT_SETTINGS_FILE,
    TRAFFIC_ICON_SIZE_MAX_PX,
    TRAFFIC_ICON_SIZE_MIN_PX,
    WAYPOINT_MARKER_SIZE_MAX_PX,
    WAYPOINT_MARKER_SIZE_MIN_PX,
    FontSizes,
    default_airplane_font_sizes,
    default_font_sizes,
    load_airplane_font_sizes,
    load_font_sizes,
    load_traffic_icon_size_px,
    load_waypoint_marker_size_px,
    save_airplane_font_sizes,
    save_font_sizes,
    save_traffic_icon_size_px,
    save_waypoint_marker_size_px,
)
from cvfr_routemaster.ui_theme import apply_dark_theme  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """Shared QApplication for the module — Qt requires exactly one
    per process and re-instantiating between tests crashes the
    style-sheet machinery."""
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Redirect ``settings_store._settings()`` to a per-test INI file
    so these tests never read or write the user's real config.

    Same pattern as ``test_window_layout_persistence.isolated_settings``
    — we explicitly request ``IniFormat`` because the Windows native
    registry backend isn't auto-isolated by ``tmp_path``.
    """
    ini_path = tmp_path / "test_settings.ini"

    def _factory() -> QSettings:
        return QSettings(str(ini_path), QSettings.Format.IniFormat)

    monkeypatch.setattr(settings_store, "_settings", _factory)
    return ini_path


# ---- defaults & round-trip ----------------------------------------------


def test_default_font_sizes_match_documented_constants():
    """The defaults dataclass must reproduce the three module-level
    ``DEFAULT_*`` constants — those constants are the public API
    used by the dialog and the theme, so they must stay in sync
    with what ``load_font_sizes`` returns on first launch."""
    sizes = default_font_sizes()
    assert sizes.table_px == DEFAULT_TABLE_FONT_PX
    assert sizes.route_text_px == DEFAULT_ROUTE_TEXT_FONT_PX
    assert sizes.hint_px == DEFAULT_HINT_FONT_PX


def test_default_airplane_font_sizes_match_documented_constants():
    """Airplane-mode counterpart of the normal-mode default check.

    The airplane profile is independent of the normal one and uses
    its own three ``DEFAULT_AIRPLANE_*`` constants — typically larger
    than the normal defaults to match in-flight reading distance.
    """
    sizes = default_airplane_font_sizes()
    assert sizes.table_px == DEFAULT_AIRPLANE_TABLE_FONT_PX
    assert sizes.route_text_px == DEFAULT_AIRPLANE_ROUTE_TEXT_FONT_PX
    assert sizes.hint_px == DEFAULT_AIRPLANE_HINT_FONT_PX


def test_airplane_defaults_distinct_from_normal_defaults():
    """The two profiles must have *different* defaults — that's the
    user-visible reason the airplane profile exists at all. If a
    future refactor accidentally re-points the airplane defaults
    at the normal ones, this test catches it before the user does.
    """
    assert default_airplane_font_sizes() != default_font_sizes()


def test_load_returns_defaults_when_nothing_saved(isolated_settings):
    """First-launch contract: no QSettings entry → defaults.

    Critical for the "ship a fresh release on a clean machine"
    workflow — the user should see the historic, unchanged
    rendering until they consciously pick a different size in the
    dialog.
    """
    sizes = load_font_sizes()
    assert sizes == default_font_sizes()


def test_save_then_load_round_trips_chosen_values(isolated_settings):
    """Whatever sizes go in must come back out byte-for-byte.

    A silent mutation (e.g. QSettings dropping the int type and
    handing back a string) would produce a TypeError at QSS-build
    time on next launch.
    """
    sizes = FontSizes(table_px=15, route_text_px=18, hint_px=22)
    save_font_sizes(sizes)
    assert load_font_sizes() == sizes


def test_save_overwrites_previous(isolated_settings):
    """Two consecutive saves: the second one wins.

    Catches an off-by-one schema-name collision (e.g. accidentally
    writing to a different key on save vs. read) that would
    otherwise show up only after a power-user session.
    """
    save_font_sizes(FontSizes(table_px=10, route_text_px=10, hint_px=10))
    save_font_sizes(FontSizes(table_px=20, route_text_px=21, hint_px=22))
    assert load_font_sizes() == FontSizes(
        table_px=20, route_text_px=21, hint_px=22
    )


def test_partial_save_falls_back_to_defaults_per_field(
    isolated_settings, monkeypatch
):
    """When only some keys are persisted (e.g. forwards-compat with
    a future fourth knob, or a hand-edited INI), each unset field
    must independently fall back to its default — not all-defaults
    on first miss.
    """
    s = settings_store._settings()
    # Only one of the three keys is set.
    s.setValue("font_table_px", 17)
    s.sync()
    sizes = load_font_sizes()
    assert sizes.table_px == 17
    assert sizes.route_text_px == DEFAULT_ROUTE_TEXT_FONT_PX
    assert sizes.hint_px == DEFAULT_HINT_FONT_PX


def test_airplane_save_then_load_round_trips_chosen_values(isolated_settings):
    """Airplane profile round-trip — same contract as the normal
    profile, exercised over the ``airplane_font_*`` QSettings keys.
    """
    sizes = FontSizes(table_px=28, route_text_px=24, hint_px=22)
    save_airplane_font_sizes(sizes)
    assert load_airplane_font_sizes() == sizes


def test_airplane_load_returns_defaults_when_nothing_saved(isolated_settings):
    """First-launch contract for the airplane profile: empty
    QSettings → airplane defaults (NOT normal defaults).
    """
    assert load_airplane_font_sizes() == default_airplane_font_sizes()


def test_airplane_and_normal_profiles_are_independent(isolated_settings):
    """Saving one profile must not perturb the other. This is the
    user-visible contract: the user can tune in-flight sizes
    without disturbing their bench-test sizes, and vice versa.
    """
    normal = FontSizes(table_px=11, route_text_px=12, hint_px=15)
    airplane = FontSizes(table_px=30, route_text_px=26, hint_px=28)
    save_font_sizes(normal)
    save_airplane_font_sizes(airplane)
    assert load_font_sizes() == normal
    assert load_airplane_font_sizes() == airplane
    # Re-saving the normal profile must not splash into airplane.
    save_font_sizes(FontSizes(table_px=9, route_text_px=10, hint_px=11))
    assert load_airplane_font_sizes() == airplane


def test_min_max_bounds_are_sane():
    """Lower bound must be readable; upper bound must not exceed
    common max font sizes for desktop GUIs. Sanity check so a
    future bump can't silently render the dialog useless.
    """
    assert FONT_SIZE_MIN_PX >= 6
    assert FONT_SIZE_MAX_PX <= 96
    assert FONT_SIZE_MIN_PX < FONT_SIZE_MAX_PX


# ---- theme application --------------------------------------------------


def test_apply_dark_theme_uses_supplied_font_sizes(qapp):
    """The QSS the theme installs must contain the user-chosen
    ``font-size`` rules for the three selectors. Bug-class: a
    refactor that forgot to thread one of the values would silently
    fall back to the default px and the user's preference would
    appear to "not stick".
    """
    sizes = FontSizes(table_px=14, route_text_px=16, hint_px=20)
    apply_dark_theme(qapp, sizes)
    sheet = qapp.styleSheet()
    # ``QTableView`` is the tables selector — must end up with the
    # user-chosen size as its ``font-size`` rule.
    assert "font-size: 14px;" in sheet
    assert "font-size: 16px;" in sheet
    assert "font-size: 20px;" in sheet
    # Selectors must also be present so the rules actually bind.
    assert "QTableView" in sheet
    assert "QLabel#routeText" in sheet
    assert "QLabel#mapHint" in sheet


def test_apply_dark_theme_falls_back_to_defaults_when_none(qapp):
    """Backwards-compat: callers that don't supply font sizes
    (existing tests, ad-hoc scripts) must still render with the
    documented defaults.
    """
    apply_dark_theme(qapp)
    sheet = qapp.styleSheet()
    assert f"font-size: {DEFAULT_TABLE_FONT_PX}px;" in sheet
    assert f"font-size: {DEFAULT_ROUTE_TEXT_FONT_PX}px;" in sheet
    assert f"font-size: {DEFAULT_HINT_FONT_PX}px;" in sheet


def test_apply_dark_theme_is_idempotent_with_different_sizes(qapp):
    """Re-applying with new sizes must fully replace the previous
    QSS — Qt's ``setStyleSheet`` semantics demand this, and the
    "user opens Font Settings, changes a size, OKs" path relies
    on it.
    """
    apply_dark_theme(qapp, FontSizes(table_px=10, route_text_px=10, hint_px=10))
    apply_dark_theme(qapp, FontSizes(table_px=30, route_text_px=31, hint_px=32))
    sheet = qapp.styleSheet()
    assert "font-size: 30px;" in sheet
    assert "font-size: 31px;" in sheet
    assert "font-size: 32px;" in sheet
    # Old values must not linger — pure substring presence isn't
    # enough on its own (a future selector could repeat a size),
    # but each of the previous values was uniquely tied to one
    # selector here so checking absence is meaningful.
    assert "font-size: 10px;" not in sheet


# ---- dialog ------------------------------------------------------------


def test_dialog_seeds_spinboxes_from_supplied_sizes(qapp):
    """Opening the dialog with a given :class:`FontSizes` must
    populate the three normal-mode spinboxes with those exact
    values, so the user sees their current preference (not the
    defaults) when they re-open the dialog.
    """
    sizes = FontSizes(table_px=13, route_text_px=17, hint_px=21)
    dlg = FontSettingsDialog(
        sizes,
        default_airplane_font_sizes(),
        DEFAULT_TRAFFIC_ICON_SIZE_PX,
        DEFAULT_WAYPOINT_MARKER_SIZE_PX,
    )
    try:
        assert dlg.chosen_sizes() == sizes
    finally:
        dlg.deleteLater()


def test_dialog_seeds_airplane_spinboxes_from_supplied_sizes(qapp):
    """Airplane-mode counterpart of the seeding test: the second
    profile must populate its own three spinboxes independently of
    the normal-mode trio. Catches a wiring bug where the dialog
    might paint both profiles from the same input — which would
    silently hide the dev's airplane-mode preference.
    """
    normal = FontSizes(table_px=12, route_text_px=12, hint_px=18)
    airplane = FontSizes(table_px=28, route_text_px=24, hint_px=30)
    dlg = FontSettingsDialog(
        normal,
        airplane,
        DEFAULT_TRAFFIC_ICON_SIZE_PX,
        DEFAULT_WAYPOINT_MARKER_SIZE_PX,
    )
    try:
        assert dlg.chosen_sizes() == normal
        assert dlg.chosen_airplane_sizes() == airplane
    finally:
        dlg.deleteLater()


def test_dialog_clamps_out_of_range_incoming_values(qapp):
    """A corrupt QSettings entry (e.g. ``-5`` or ``9999``) must not
    propagate into the dialog — the spinbox would otherwise reject
    the value silently or display garbage. We clamp to the
    documented ``[FONT_SIZE_MIN_PX, FONT_SIZE_MAX_PX]`` range for
    both profiles, since either could be corrupted independently.
    """
    weird_normal = FontSizes(
        table_px=FONT_SIZE_MIN_PX - 10,
        route_text_px=FONT_SIZE_MAX_PX + 50,
        hint_px=FONT_SIZE_MIN_PX,
    )
    weird_airplane = FontSizes(
        table_px=FONT_SIZE_MAX_PX + 100,
        route_text_px=FONT_SIZE_MIN_PX - 5,
        hint_px=FONT_SIZE_MAX_PX + 1,
    )
    dlg = FontSettingsDialog(
        weird_normal,
        weird_airplane,
        DEFAULT_TRAFFIC_ICON_SIZE_PX,
        DEFAULT_WAYPOINT_MARKER_SIZE_PX,
    )
    try:
        chosen = dlg.chosen_sizes()
        assert chosen.table_px == FONT_SIZE_MIN_PX
        assert chosen.route_text_px == FONT_SIZE_MAX_PX
        assert chosen.hint_px == FONT_SIZE_MIN_PX
        chosen_air = dlg.chosen_airplane_sizes()
        assert chosen_air.table_px == FONT_SIZE_MAX_PX
        assert chosen_air.route_text_px == FONT_SIZE_MIN_PX
        assert chosen_air.hint_px == FONT_SIZE_MAX_PX
    finally:
        dlg.deleteLater()


def test_dialog_chosen_sizes_reflects_user_edits(qapp):
    """The dialog must surface user edits via ``chosen_sizes()``
    and ``chosen_airplane_sizes()`` so the controller can persist
    both profiles together. Simulated by reaching into the
    internal spinboxes — the dialog only exposes the two
    ``chosen_*`` accessors as its read API, so this protects
    against a rename that breaks the controller wiring.
    """
    dlg = FontSettingsDialog(
        default_font_sizes(),
        default_airplane_font_sizes(),
        DEFAULT_TRAFFIC_ICON_SIZE_PX,
        DEFAULT_WAYPOINT_MARKER_SIZE_PX,
    )
    try:
        dlg._table_spin.setValue(14)
        dlg._route_text_spin.setValue(15)
        dlg._hint_spin.setValue(19)
        dlg._airplane_table_spin.setValue(26)
        dlg._airplane_route_text_spin.setValue(22)
        dlg._airplane_hint_spin.setValue(20)
        assert dlg.chosen_sizes() == FontSizes(
            table_px=14, route_text_px=15, hint_px=19
        )
        assert dlg.chosen_airplane_sizes() == FontSizes(
            table_px=26, route_text_px=22, hint_px=20
        )
    finally:
        dlg.deleteLater()


def test_dialog_accept_returns_accepted_code(qapp):
    """Smoke test: the OK button is wired to ``accept`` so the
    controller's ``exec() == Accepted`` path is reachable. We
    accept the dialog directly (rather than simulating a click
    through Qt's event queue) since the wiring is what we're
    testing, not the click delivery.
    """
    dlg = FontSettingsDialog(
        default_font_sizes(),
        default_airplane_font_sizes(),
        DEFAULT_TRAFFIC_ICON_SIZE_PX,
        DEFAULT_WAYPOINT_MARKER_SIZE_PX,
    )
    try:
        dlg.accept()
        assert dlg.result() == QDialog.DialogCode.Accepted
    finally:
        dlg.deleteLater()


def test_dialog_reject_returns_rejected_code(qapp):
    """Cancel must produce ``Rejected`` so the controller's
    "skip save + skip re-apply" guard works.
    """
    dlg = FontSettingsDialog(
        default_font_sizes(),
        default_airplane_font_sizes(),
        DEFAULT_TRAFFIC_ICON_SIZE_PX,
        DEFAULT_WAYPOINT_MARKER_SIZE_PX,
    )
    try:
        dlg.reject()
        assert dlg.result() == QDialog.DialogCode.Rejected
    finally:
        dlg.deleteLater()


# ---- shipped font_settings.json fallback --------------------------------
#
# Mirrors the ``map_layout.json`` mechanism that ships the dev's
# calibration-time sheet position across to a fresh-machine release.
# Same priority ladder: QSettings → shipped JSON → hard-coded defaults.


def _write_shipped_font_file(
    project_root,
    sizes: FontSizes,
    airplane: FontSizes | None = None,
    traffic_icon_size_px: int | None = None,
    waypoint_marker_size_px: int | None = None,
) -> None:
    """Test helper — write a release-shaped
    ``.cvfr_routemaster/font_settings.json`` under ``project_root``
    so ``load_font_sizes(project_root)`` can pick it up via the
    shipped-file rung.

    If ``airplane`` is supplied, also emit the airplane-mode keys
    (``airplane_*_px``) so ``load_airplane_font_sizes(project_root)``
    sees them. If ``traffic_icon_size_px`` is supplied, emit the
    ``traffic_icon_size_px`` flat key alongside the font fields so
    ``load_traffic_icon_size_px(project_root)`` sees it.
    ``waypoint_marker_size_px`` is similarly emitted under the
    ``waypoint_marker_size_px`` flat key for the marker-size
    shipped-default rung.

    Callers that pass only ``sizes`` write a legacy-shaped file
    (normal-mode font keys only) — matches how a shipped file from
    before the airplane-profile / traffic-icon / marker-size
    features looks.
    """
    import json

    cache_dir = project_root / ".cvfr_routemaster"
    cache_dir.mkdir(parents=True, exist_ok=True)
    blob: dict[str, int] = {
        "table_px": sizes.table_px,
        "route_text_px": sizes.route_text_px,
        "hint_px": sizes.hint_px,
    }
    if airplane is not None:
        blob.update(
            {
                "airplane_table_px": airplane.table_px,
                "airplane_route_text_px": airplane.route_text_px,
                "airplane_hint_px": airplane.hint_px,
            }
        )
    if traffic_icon_size_px is not None:
        blob["traffic_icon_size_px"] = int(traffic_icon_size_px)
    if waypoint_marker_size_px is not None:
        blob["waypoint_marker_size_px"] = int(waypoint_marker_size_px)
    (cache_dir / SHIPPED_FONT_SETTINGS_FILE).write_text(json.dumps(blob))


def test_load_falls_back_to_shipped_file_when_qsettings_empty(
    isolated_settings, tmp_path
):
    """Fresh machine: QSettings is empty *and* a shipped
    ``font_settings.json`` exists — the loader must return the
    shipped sizes (the dev's preference), not the hard-coded
    defaults.

    Critical for the "ship a fresh release on a friend's machine"
    flow — without this rung the friend's first launch shows the
    bare baseline UI instead of the size the dev tested against.
    """
    shipped = FontSizes(table_px=16, route_text_px=14, hint_px=22)
    _write_shipped_font_file(tmp_path, shipped)
    assert load_font_sizes(tmp_path) == shipped


def test_load_qsettings_wins_over_shipped_file(isolated_settings, tmp_path):
    """Once the user has explicitly saved their own font preference
    via the dialog (which writes to QSettings), their choice must
    override the dev's shipped default on that machine — otherwise
    the shipped file would feel like an override the user keeps
    having to dismiss.
    """
    _write_shipped_font_file(
        tmp_path, FontSizes(table_px=16, route_text_px=14, hint_px=22)
    )
    user_choice = FontSizes(table_px=10, route_text_px=11, hint_px=12)
    save_font_sizes(user_choice)
    assert load_font_sizes(tmp_path) == user_choice


def test_load_partial_qsettings_mixes_with_shipped_per_field(
    isolated_settings, tmp_path
):
    """If only some of the QSettings keys are populated (e.g. a
    future schema bump that adds a fourth font knob, or a
    migration that dropped one of them), each unset field must
    independently fall back to the shipped value — not all-or-
    nothing.
    """
    _write_shipped_font_file(
        tmp_path, FontSizes(table_px=16, route_text_px=14, hint_px=22)
    )
    s = settings_store._settings()
    s.setValue("font_table_px", 99)
    s.sync()
    sizes = load_font_sizes(tmp_path)
    assert sizes.table_px == 99
    assert sizes.route_text_px == 14
    assert sizes.hint_px == 22


def test_load_falls_back_to_defaults_when_shipped_file_absent(
    isolated_settings, tmp_path
):
    """``project_root`` exists but has no ``font_settings.json`` —
    bare dev checkout that never ran a release build. Loader must
    return the hard-coded defaults rather than e.g. crashing on a
    missing-file probe.
    """
    assert load_font_sizes(tmp_path) == default_font_sizes()


def test_load_falls_back_to_defaults_on_corrupted_shipped_file(
    isolated_settings, tmp_path
):
    """A truncated / hand-edited / partially-written JSON shipped
    file must NOT bring down the app — the loader treats it as
    "no shipped file" and falls through to the defaults.
    """
    cache_dir = tmp_path / ".cvfr_routemaster"
    cache_dir.mkdir()
    (cache_dir / SHIPPED_FONT_SETTINGS_FILE).write_text(
        "{not valid json"
    )
    assert load_font_sizes(tmp_path) == default_font_sizes()


def test_load_falls_back_to_defaults_when_shipped_file_missing_fields(
    isolated_settings, tmp_path
):
    """Partial shipped file (missing one of the required keys)
    must fall through to defaults rather than silently mixing
    half-shipped + half-default values — a half-populated file is
    a bug signal, and rolling up to defaults gives a clean
    "ignore the broken file" semantic.
    """
    import json

    cache_dir = tmp_path / ".cvfr_routemaster"
    cache_dir.mkdir()
    (cache_dir / SHIPPED_FONT_SETTINGS_FILE).write_text(
        json.dumps({"table_px": 14, "route_text_px": 13})
    )
    assert load_font_sizes(tmp_path) == default_font_sizes()


def test_load_falls_back_to_defaults_on_non_dict_shipped_file(
    isolated_settings, tmp_path
):
    """Shipped file is valid JSON but the wrong shape (e.g. a list
    or a number) — loader rejects and falls through.
    """
    cache_dir = tmp_path / ".cvfr_routemaster"
    cache_dir.mkdir()
    (cache_dir / SHIPPED_FONT_SETTINGS_FILE).write_text("[12, 14, 18]")
    assert load_font_sizes(tmp_path) == default_font_sizes()


def test_load_without_project_root_skips_shipped_lookup(
    isolated_settings, tmp_path
):
    """Backwards-compat: legacy callers that don't pass
    ``project_root`` must keep the old behaviour (QSettings →
    defaults), even when a shipped file happens to exist in the
    workspace. Catches a refactor that accidentally promotes the
    shipped file into a global / process-wide override.
    """
    _write_shipped_font_file(
        tmp_path, FontSizes(table_px=16, route_text_px=14, hint_px=22)
    )
    assert load_font_sizes() == default_font_sizes()


# ---- airplane-profile shipped-file fallback -----------------------------


def test_load_airplane_falls_back_to_shipped_file_when_present(
    isolated_settings, tmp_path
):
    """Fresh-machine scenario for the airplane profile: QSettings is
    empty, the shipped file contains ``airplane_*`` keys → loader
    returns the shipped airplane sizes (the dev's flight-mode
    preference), not the hard-coded airplane defaults.
    """
    shipped_normal = FontSizes(table_px=12, route_text_px=12, hint_px=18)
    shipped_airplane = FontSizes(table_px=30, route_text_px=26, hint_px=28)
    _write_shipped_font_file(tmp_path, shipped_normal, shipped_airplane)
    assert load_airplane_font_sizes(tmp_path) == shipped_airplane


def test_load_airplane_falls_back_to_defaults_on_legacy_shipped_file(
    isolated_settings, tmp_path
):
    """Backwards-compat: a shipped file produced before the airplane-
    profile feature contains only the three normal-mode keys.
    Loading the airplane profile from that file must NOT crash and
    must fall through to the hard-coded airplane defaults rather
    than mistakenly returning the normal-mode shipped values under
    the airplane field names.
    """
    _write_shipped_font_file(
        tmp_path,
        FontSizes(table_px=14, route_text_px=15, hint_px=19),
        airplane=None,
    )
    assert load_airplane_font_sizes(tmp_path) == default_airplane_font_sizes()


def test_load_airplane_qsettings_wins_over_shipped(isolated_settings, tmp_path):
    """User has explicitly tuned airplane sizes via the dialog
    (which writes to QSettings) → their choice trumps the shipped
    airplane default, just like the normal-profile contract.
    """
    _write_shipped_font_file(
        tmp_path,
        FontSizes(table_px=12, route_text_px=12, hint_px=18),
        FontSizes(table_px=30, route_text_px=26, hint_px=28),
    )
    user = FontSizes(table_px=22, route_text_px=20, hint_px=24)
    save_airplane_font_sizes(user)
    assert load_airplane_font_sizes(tmp_path) == user


# ---- write_shipped_font_settings (build-time writer) --------------------


def test_write_shipped_font_settings_skips_when_qsettings_empty(
    isolated_settings, tmp_path
):
    """Build-time writer must NOT emit a file when the dev's
    QSettings has none of the font-size keys — the release falls
    through to the in-app defaults, and emitting a redundant file
    that mirrors the defaults would mask a future change-of-
    defaults.
    """
    from scripts._restamp_cache_fingerprints import (
        write_shipped_font_settings,
    )

    cache_dir = tmp_path / ".cvfr_routemaster"
    cache_dir.mkdir()
    report = write_shipped_font_settings(tmp_path)
    assert report.written is None
    assert report.reason == "qsettings_empty"
    assert not (cache_dir / SHIPPED_FONT_SETTINGS_FILE).exists()


def test_write_shipped_font_settings_emits_file_from_qsettings(
    isolated_settings, tmp_path
):
    """When the dev has saved font preferences, the writer must
    persist them under ``release/.cvfr_routemaster/`` in the
    documented JSON shape — that's the on-disk contract
    ``_load_shipped_font_sizes`` reads back.
    """
    from scripts._restamp_cache_fingerprints import (
        write_shipped_font_settings,
    )

    save_font_sizes(FontSizes(table_px=15, route_text_px=13, hint_px=22))
    cache_dir = tmp_path / ".cvfr_routemaster"
    cache_dir.mkdir()
    report = write_shipped_font_settings(tmp_path)
    assert report.written is not None
    assert report.written.name == SHIPPED_FONT_SETTINGS_FILE
    assert report.sizes == {"table_px": 15, "route_text_px": 13, "hint_px": 22}
    # Round-trip via the loader confirms the file is honoured.
    # (Use a NEW isolated QSettings so the writer's source values
    # don't shadow the shipped-file rung we're verifying.)
    s = settings_store._settings()
    s.clear()
    s.sync()
    assert load_font_sizes(tmp_path) == FontSizes(
        table_px=15, route_text_px=13, hint_px=22
    )


def test_write_shipped_font_settings_creates_cache_dir(
    isolated_settings, tmp_path
):
    """The writer must create ``.cvfr_routemaster/`` if it doesn't
    exist yet (defensive against being called on a pristine
    release tree where ``_copy_seed_cache`` hasn't run). Catches
    a refactor that drops the ``mkdir(parents=True, exist_ok=True)``
    safety net.
    """
    from scripts._restamp_cache_fingerprints import (
        write_shipped_font_settings,
    )

    save_font_sizes(FontSizes(table_px=14, route_text_px=14, hint_px=20))
    report = write_shipped_font_settings(tmp_path)
    assert report.written is not None
    assert (tmp_path / ".cvfr_routemaster").is_dir()
    assert (tmp_path / ".cvfr_routemaster" / SHIPPED_FONT_SETTINGS_FILE).is_file()


def test_write_shipped_font_settings_omits_airplane_when_unset(
    isolated_settings, tmp_path
):
    """Per-profile gating: when the dev has saved only normal-mode
    sizes, the shipped file must emit *only* the normal-mode keys.
    Emitting the airplane defaults would freeze them into the
    release, which would override a future change-of-defaults on
    friends' machines that copy this shipped file forward.
    """
    from scripts._restamp_cache_fingerprints import (
        write_shipped_font_settings,
    )

    save_font_sizes(FontSizes(table_px=15, route_text_px=13, hint_px=22))
    report = write_shipped_font_settings(tmp_path)
    assert report.written is not None
    assert report.sizes == {
        "table_px": 15,
        "route_text_px": 13,
        "hint_px": 22,
    }


def test_write_shipped_font_settings_omits_normal_when_only_airplane_set(
    isolated_settings, tmp_path
):
    """Mirror of the previous test for the inverse case: dev tuned
    only airplane sizes. The writer must dump only the
    ``airplane_*`` keys so the normal-mode defaults stay open to
    future changes.
    """
    from scripts._restamp_cache_fingerprints import (
        write_shipped_font_settings,
    )

    save_airplane_font_sizes(FontSizes(table_px=28, route_text_px=24, hint_px=26))
    report = write_shipped_font_settings(tmp_path)
    assert report.written is not None
    assert report.sizes == {
        "airplane_table_px": 28,
        "airplane_route_text_px": 24,
        "airplane_hint_px": 26,
    }


def test_write_shipped_font_settings_emits_both_profiles_when_both_saved(
    isolated_settings, tmp_path
):
    """When the dev has tuned both profiles, the shipped file must
    capture both — the friend's first launch should reproduce the
    dev's airplane-mode preferences in addition to their normal-
    mode ones.
    """
    from scripts._restamp_cache_fingerprints import (
        write_shipped_font_settings,
    )

    save_font_sizes(FontSizes(table_px=16, route_text_px=14, hint_px=22))
    save_airplane_font_sizes(FontSizes(table_px=30, route_text_px=26, hint_px=28))
    report = write_shipped_font_settings(tmp_path)
    assert report.written is not None
    assert report.sizes == {
        "table_px": 16,
        "route_text_px": 14,
        "hint_px": 22,
        "airplane_table_px": 30,
        "airplane_route_text_px": 26,
        "airplane_hint_px": 28,
    }
    # Round-trip both profiles through the loaders on a wiped
    # QSettings to confirm the writer's JSON honours both
    # shipped-file rungs simultaneously.
    s = settings_store._settings()
    s.clear()
    s.sync()
    assert load_font_sizes(tmp_path) == FontSizes(
        table_px=16, route_text_px=14, hint_px=22
    )
    assert load_airplane_font_sizes(tmp_path) == FontSizes(
        table_px=30, route_text_px=26, hint_px=28
    )


# ---- traffic-icon-size persistence -------------------------------------
#
# v2 prep — see ``ROADMAP-NEXT.md``. The traffic-icon size shares the
# shipped ``font_settings.json`` blob with the font fields (one
# document covers the dev's whole "display preferences" snapshot)
# but lives under its own QSettings key with no airplane-vs-normal
# split.


def test_traffic_icon_size_min_max_bounds_are_sane():
    """The dialog clamps to ``[TRAFFIC_ICON_SIZE_MIN_PX,
    TRAFFIC_ICON_SIZE_MAX_PX]``; the bounds must be strictly
    positive and have min < default < max so the spinbox can
    represent the default and accept edits in either direction.
    """
    assert TRAFFIC_ICON_SIZE_MIN_PX > 0
    assert TRAFFIC_ICON_SIZE_MIN_PX < DEFAULT_TRAFFIC_ICON_SIZE_PX
    assert DEFAULT_TRAFFIC_ICON_SIZE_PX < TRAFFIC_ICON_SIZE_MAX_PX


def test_traffic_icon_size_load_returns_default_when_nothing_saved(
    isolated_settings,
):
    """Bare dev checkout / fresh-machine release before the user
    opens the dialog: QSettings is empty and there's no shipped
    file — loader returns the documented default.
    """
    assert load_traffic_icon_size_px() == DEFAULT_TRAFFIC_ICON_SIZE_PX


def test_traffic_icon_size_save_then_load_round_trips(isolated_settings):
    """Round-trip: persist a value through QSettings and read it
    back unchanged. Catches a typo in the QSettings key name —
    a writer/reader key mismatch would silently fall back to the
    default and the user's choice would never stick across
    launches.
    """
    save_traffic_icon_size_px(40)
    assert load_traffic_icon_size_px() == 40


def test_traffic_icon_size_save_overwrites_previous(isolated_settings):
    """Re-saving must replace the previous QSettings value, not
    accumulate a list or keep the first write — protects against
    a refactor that switches to ``setValue`` of a list type.
    """
    save_traffic_icon_size_px(20)
    save_traffic_icon_size_px(36)
    assert load_traffic_icon_size_px() == 36


def test_traffic_icon_size_load_falls_back_to_shipped_file(
    isolated_settings, tmp_path
):
    """Fresh-machine release scenario: QSettings is empty, the
    shipped ``font_settings.json`` carries a ``traffic_icon_size_px``
    key — loader returns the shipped value rather than the
    hard-coded default. Same priority ladder as the font
    fields.
    """
    _write_shipped_font_file(
        tmp_path,
        FontSizes(table_px=12, route_text_px=12, hint_px=18),
        traffic_icon_size_px=32,
    )
    assert load_traffic_icon_size_px(tmp_path) == 32


def test_traffic_icon_size_qsettings_wins_over_shipped(
    isolated_settings, tmp_path
):
    """Once the user has saved a preference via the dialog, it
    overrides the shipped default on that machine — same
    user-wins-locally contract as the font fields.
    """
    _write_shipped_font_file(
        tmp_path,
        FontSizes(table_px=12, route_text_px=12, hint_px=18),
        traffic_icon_size_px=32,
    )
    save_traffic_icon_size_px(48)
    assert load_traffic_icon_size_px(tmp_path) == 48


def test_traffic_icon_size_load_falls_back_to_default_on_legacy_shipped_file(
    isolated_settings, tmp_path
):
    """A shipped file produced before the traffic-icon feature
    contains only the font keys. Loading the icon size from that
    file must NOT crash and must fall through to the hard-coded
    default rather than e.g. confusing one of the font fields
    for the icon size.
    """
    _write_shipped_font_file(
        tmp_path,
        FontSizes(table_px=14, route_text_px=15, hint_px=19),
        traffic_icon_size_px=None,
    )
    assert load_traffic_icon_size_px(tmp_path) == DEFAULT_TRAFFIC_ICON_SIZE_PX


def test_traffic_icon_size_load_without_project_root_skips_shipped(
    isolated_settings, tmp_path
):
    """Backwards-compat: legacy callers that don't pass
    ``project_root`` keep the QSettings-or-default behaviour even
    when a shipped file exists in the workspace. Mirrors
    :func:`test_load_without_project_root_skips_shipped_lookup`.
    """
    _write_shipped_font_file(
        tmp_path,
        FontSizes(table_px=12, route_text_px=12, hint_px=18),
        traffic_icon_size_px=32,
    )
    assert load_traffic_icon_size_px() == DEFAULT_TRAFFIC_ICON_SIZE_PX


# ---- waypoint-marker-size persistence -----------------------------------
#
# Same three-rung ladder as traffic-icon size: QSettings → shipped
# ``font_settings.json`` → hard-coded default. Different default and
# different flat key (``waypoint_marker_size_px``) — the two on-chart
# overlay knobs are independent.


def test_waypoint_marker_size_min_max_bounds_are_sane():
    """Mirrors :func:`test_traffic_icon_size_min_max_bounds_are_sane`
    for the marker-size codomain."""
    assert WAYPOINT_MARKER_SIZE_MIN_PX > 0
    assert WAYPOINT_MARKER_SIZE_MIN_PX < DEFAULT_WAYPOINT_MARKER_SIZE_PX
    assert DEFAULT_WAYPOINT_MARKER_SIZE_PX < WAYPOINT_MARKER_SIZE_MAX_PX


def test_waypoint_marker_size_load_returns_default_when_nothing_saved(
    isolated_settings,
):
    """First-launch fallback path."""
    assert (
        load_waypoint_marker_size_px()
        == DEFAULT_WAYPOINT_MARKER_SIZE_PX
    )


def test_waypoint_marker_size_save_then_load_round_trips(isolated_settings):
    """Round-trip via QSettings — catches a writer/reader key
    mismatch (which would silently fall through to the default)."""
    save_waypoint_marker_size_px(36)
    assert load_waypoint_marker_size_px() == 36


def test_waypoint_marker_size_save_overwrites_previous(isolated_settings):
    """Re-saving replaces the previous value rather than
    accumulating."""
    save_waypoint_marker_size_px(20)
    save_waypoint_marker_size_px(28)
    assert load_waypoint_marker_size_px() == 28


def test_waypoint_marker_size_load_falls_back_to_shipped_file(
    isolated_settings, tmp_path
):
    """Fresh-machine release: shipped JSON carries the marker
    size — loader returns it."""
    _write_shipped_font_file(
        tmp_path,
        FontSizes(table_px=12, route_text_px=12, hint_px=18),
        waypoint_marker_size_px=30,
    )
    assert load_waypoint_marker_size_px(tmp_path) == 30


def test_waypoint_marker_size_qsettings_wins_over_shipped(
    isolated_settings, tmp_path
):
    """User edit overrides shipped default on that machine."""
    _write_shipped_font_file(
        tmp_path,
        FontSizes(table_px=12, route_text_px=12, hint_px=18),
        waypoint_marker_size_px=30,
    )
    save_waypoint_marker_size_px(22)
    assert load_waypoint_marker_size_px(tmp_path) == 22


def test_waypoint_marker_size_load_falls_back_to_default_on_legacy_shipped(
    isolated_settings, tmp_path
):
    """Legacy shipped file pre-dating the marker-size feature
    (file present, key absent) falls through to the hard-coded
    default without crashing."""
    _write_shipped_font_file(
        tmp_path,
        FontSizes(table_px=14, route_text_px=15, hint_px=19),
        waypoint_marker_size_px=None,
    )
    assert (
        load_waypoint_marker_size_px(tmp_path)
        == DEFAULT_WAYPOINT_MARKER_SIZE_PX
    )


def test_dialog_seeds_waypoint_marker_size_from_supplied_value(qapp):
    """Opening the dialog with a marker-size value seeds the
    spinbox with that exact value (mirrors the traffic-icon
    seeding contract)."""
    dlg = FontSettingsDialog(
        default_font_sizes(),
        default_airplane_font_sizes(),
        DEFAULT_TRAFFIC_ICON_SIZE_PX,
        waypoint_marker_size_px=28,
    )
    try:
        assert dlg.chosen_waypoint_marker_size_px() == 28
    finally:
        dlg.deleteLater()


def test_dialog_clamps_out_of_range_waypoint_marker_size(qapp):
    """A corrupt QSettings marker-size value (out of the
    spinbox bounds) is clamped at dialog-open time so the
    spinbox shows a renderable value."""
    dlg = FontSettingsDialog(
        default_font_sizes(),
        default_airplane_font_sizes(),
        DEFAULT_TRAFFIC_ICON_SIZE_PX,
        waypoint_marker_size_px=WAYPOINT_MARKER_SIZE_MAX_PX + 100,
    )
    try:
        assert (
            dlg.chosen_waypoint_marker_size_px()
            == WAYPOINT_MARKER_SIZE_MAX_PX
        )
    finally:
        dlg.deleteLater()
    dlg2 = FontSettingsDialog(
        default_font_sizes(),
        default_airplane_font_sizes(),
        DEFAULT_TRAFFIC_ICON_SIZE_PX,
        waypoint_marker_size_px=WAYPOINT_MARKER_SIZE_MIN_PX - 50,
    )
    try:
        assert (
            dlg2.chosen_waypoint_marker_size_px()
            == WAYPOINT_MARKER_SIZE_MIN_PX
        )
    finally:
        dlg2.deleteLater()


def test_dialog_chosen_waypoint_marker_size_reflects_user_edits(qapp):
    """User edits to the marker-size spinbox surface via
    :meth:`chosen_waypoint_marker_size_px`."""
    dlg = FontSettingsDialog(
        default_font_sizes(),
        default_airplane_font_sizes(),
        DEFAULT_TRAFFIC_ICON_SIZE_PX,
        DEFAULT_WAYPOINT_MARKER_SIZE_PX,
    )
    try:
        dlg._waypoint_marker_size_spin.setValue(32)
        assert dlg.chosen_waypoint_marker_size_px() == 32
    finally:
        dlg.deleteLater()


# ---- traffic-icon-size dialog ------------------------------------------


def test_dialog_seeds_traffic_icon_size_from_supplied_value(qapp):
    """Opening the dialog with a given icon-size value must
    populate the spinbox with that exact value — same seeding
    contract as the font knobs, otherwise re-opening the dialog
    after a save would wipe the user's preference back to the
    default.
    """
    dlg = FontSettingsDialog(
        default_font_sizes(),
        default_airplane_font_sizes(),
        traffic_icon_size_px=42,
    )
    try:
        assert dlg.chosen_traffic_icon_size_px() == 42
    finally:
        dlg.deleteLater()


def test_dialog_clamps_out_of_range_traffic_icon_size(qapp):
    """A corrupt QSettings entry (e.g. ``-5`` or ``9999``) must not
    propagate into the dialog — the spinbox would otherwise reject
    the value silently or display garbage. Mirrors the font-field
    clamp test.
    """
    dlg = FontSettingsDialog(
        default_font_sizes(),
        default_airplane_font_sizes(),
        traffic_icon_size_px=TRAFFIC_ICON_SIZE_MAX_PX + 100,
    )
    try:
        assert dlg.chosen_traffic_icon_size_px() == TRAFFIC_ICON_SIZE_MAX_PX
    finally:
        dlg.deleteLater()
    dlg2 = FontSettingsDialog(
        default_font_sizes(),
        default_airplane_font_sizes(),
        traffic_icon_size_px=TRAFFIC_ICON_SIZE_MIN_PX - 50,
    )
    try:
        assert dlg2.chosen_traffic_icon_size_px() == TRAFFIC_ICON_SIZE_MIN_PX
    finally:
        dlg2.deleteLater()


def test_dialog_chosen_traffic_icon_size_reflects_user_edits(qapp):
    """The dialog must surface user edits to the icon-size spinbox
    via :meth:`chosen_traffic_icon_size_px` so the controller can
    persist alongside the font choices. Reaches into the internal
    spinbox to simulate a user edit (the public API is the
    accessor).
    """
    dlg = FontSettingsDialog(
        default_font_sizes(),
        default_airplane_font_sizes(),
        DEFAULT_TRAFFIC_ICON_SIZE_PX,
    )
    try:
        dlg._traffic_icon_size_spin.setValue(36)
        assert dlg.chosen_traffic_icon_size_px() == 36
    finally:
        dlg.deleteLater()


# ---- traffic-icon-size build-time writer -------------------------------


def test_write_shipped_font_settings_emits_traffic_icon_size_when_set(
    isolated_settings, tmp_path
):
    """When the dev has set the traffic-icon size in QSettings, the
    build-time writer must include it in the shipped file under
    the ``traffic_icon_size_px`` flat key — that's how a friend's
    fresh-machine launch picks up the dev's preference.
    """
    from scripts._restamp_cache_fingerprints import (
        write_shipped_font_settings,
    )

    save_traffic_icon_size_px(36)
    report = write_shipped_font_settings(tmp_path)
    assert report.written is not None
    assert report.sizes == {"traffic_icon_size_px": 36}
    # Round-trip via the loader on a wiped QSettings so we're
    # genuinely reading the shipped-file rung, not just echoing
    # the source QSettings value back.
    s = settings_store._settings()
    s.clear()
    s.sync()
    assert load_traffic_icon_size_px(tmp_path) == 36


def test_write_shipped_font_settings_omits_traffic_icon_size_when_unset(
    isolated_settings, tmp_path
):
    """Per-key gating: when the dev hasn't explicitly set the
    icon size, the shipped file must NOT include the key. Same
    rationale as the per-profile gating for fonts — emitting a
    redundant ``DEFAULT_TRAFFIC_ICON_SIZE_PX`` would freeze the
    current default into the release and shadow a future bump.
    """
    from scripts._restamp_cache_fingerprints import (
        write_shipped_font_settings,
    )

    save_font_sizes(FontSizes(table_px=14, route_text_px=14, hint_px=20))
    report = write_shipped_font_settings(tmp_path)
    assert report.written is not None
    assert "traffic_icon_size_px" not in report.sizes


def test_write_shipped_font_settings_emits_all_sections_when_all_set(
    isolated_settings, tmp_path
):
    """When the dev has tuned both font profiles AND the icon
    size, the shipped file must capture all three sections in one
    flat document — friend's first launch then reproduces the
    full Display-Settings snapshot.
    """
    from scripts._restamp_cache_fingerprints import (
        write_shipped_font_settings,
    )

    save_font_sizes(FontSizes(table_px=16, route_text_px=14, hint_px=22))
    save_airplane_font_sizes(FontSizes(table_px=30, route_text_px=26, hint_px=28))
    save_traffic_icon_size_px(40)
    report = write_shipped_font_settings(tmp_path)
    assert report.written is not None
    assert report.sizes == {
        "table_px": 16,
        "route_text_px": 14,
        "hint_px": 22,
        "airplane_table_px": 30,
        "airplane_route_text_px": 26,
        "airplane_hint_px": 28,
        "traffic_icon_size_px": 40,
    }
    s = settings_store._settings()
    s.clear()
    s.sync()
    assert load_font_sizes(tmp_path) == FontSizes(
        table_px=16, route_text_px=14, hint_px=22
    )
    assert load_airplane_font_sizes(tmp_path) == FontSizes(
        table_px=30, route_text_px=26, hint_px=28
    )
    assert load_traffic_icon_size_px(tmp_path) == 40


def test_write_shipped_font_settings_emits_only_traffic_when_only_traffic_set(
    isolated_settings, tmp_path
):
    """Inverse of the per-profile-only tests: when the dev has
    only touched the traffic-icon size, the shipped file must
    contain only that key — no font defaults frozen in.
    """
    from scripts._restamp_cache_fingerprints import (
        write_shipped_font_settings,
    )

    save_traffic_icon_size_px(28)
    report = write_shipped_font_settings(tmp_path)
    assert report.written is not None
    assert report.sizes == {"traffic_icon_size_px": 28}
