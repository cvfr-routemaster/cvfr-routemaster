"""Tests for the v3.3+ INI-backed QSettings store.

Pre-v3.3 the program persisted user preferences in the OS-native
QSettings backend: Windows registry under
``HKCU\\Software\\CVFRRouteMaster\\<APP>``, Linux
``~/.config/CVFRRouteMaster/<APP>.conf``, macOS plist. v3.3+
switches to an explicit ``IniFormat`` store at
``<project_root>/settings.ini`` so:

  * Wipe-the-folder equals wipe-the-state (the v3.3 RC migration
    confusion that prompted this work).
  * "Drop the install folder anywhere" portability stops being
    broken by registry state that survives folder relocation.
  * No platform-native pollution (HKCU registry on Windows,
    ``~/.config/`` on Linux, plists on macOS).
  * Same on-disk layout on every platform — easier to reason
    about, easier to back up.

These tests pin three properties of the new backend:

1. :func:`_settings_ini_path` resolves the INI under the project
   root (dev mode); in frozen mode it resolves next to the EXE.
2. :func:`_settings` returns a ``QSettings`` whose round-trip is
   actually persisted to that INI file (not to the registry / not
   to anywhere else).
3. :func:`_migrate_legacy_native_settings_if_needed` copies the
   pre-v3.3 native store into the new INI and clears the native
   store; subsequent calls are no-ops; and an already-populated
   INI suppresses the migration even when the legacy store still
   has data (i.e. someone else's pre-v3.3 settings would never
   bleed in over a current install).

The migration test deliberately uses a *fake* legacy backend
(another IniFormat QSettings at a tmp path) rather than the
real registry/plist. Driving the real registry from a test
would require admin rights on Windows, would leak state into
the developer's actual ``HKCU`` hive, and wouldn't test
anything different from the IniFormat-substitute approach —
``QSettings.allKeys() / value() / clear()`` are backend-agnostic
by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings  # noqa: E402

from cvfr_routemaster import settings_store  # noqa: E402
from cvfr_routemaster.settings_store import (  # noqa: E402
    SETTINGS_INI_FILENAME,
    _migrate_legacy_native_settings_if_needed,
    _settings,
    _settings_ini_path,
    _settings_root,
)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_settings_ini_filename_is_stable() -> None:
    """The INI filename must be a constant, not a function of the
    user / OS / locale. Anything that varies between machines
    would break the "drop the folder on another machine, your
    settings come along" contract. Pin the literal."""
    assert SETTINGS_INI_FILENAME == "settings.ini"


def test_settings_root_in_dev_mode_is_repo_root() -> None:
    """In a source checkout (``sys.frozen`` is False), the INI
    must live at the repo root — the same directory the chart
    PDFs and ``.cvfr_routemaster/`` already live in. Pin this
    by checking that ``_settings_root()`` matches the parent of
    the package directory."""
    expected = Path(settings_store.__file__).resolve().parents[1]
    assert _settings_root() == expected


def test_settings_root_in_frozen_mode_is_executable_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """In a PyInstaller frozen build, the INI must live next to
    the EXE — same directory the seed ``.cvfr_routemaster/`` and
    bundled PDFs land in. Simulate the frozen state by setting
    ``sys.frozen`` and pointing ``sys.executable`` at a fake EXE
    under tmp_path, then confirm the root resolves to its
    parent."""
    fake_exe = tmp_path / "fake-cvfr-routemaster.exe"
    fake_exe.write_bytes(b"\x4d\x5a")  # MZ header so the path is plausible
    monkeypatch.setattr(settings_store.sys, "frozen", True, raising=False)
    monkeypatch.setattr(settings_store.sys, "executable", str(fake_exe))

    assert _settings_root() == tmp_path


def test_settings_ini_path_combines_root_and_filename() -> None:
    """The INI path is just ``_settings_root() / SETTINGS_INI_FILENAME``.
    Pin the combinator so a future refactor that introduces a
    subdirectory (or a per-user suffix) breaks here and forces
    the call-sites to be re-examined."""
    assert _settings_ini_path() == _settings_root() / SETTINGS_INI_FILENAME


# ---------------------------------------------------------------------------
# _settings() round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ini_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect :func:`_settings_root` to a tmp directory so the
    INI created during the test lives in pytest's tmp tree
    rather than the developer's actual repo root. Returns the
    directory the INI will end up in."""
    monkeypatch.setattr(
        settings_store, "_settings_root", lambda: tmp_path
    )
    return tmp_path


def test_settings_returns_iniformat_handle(fake_ini_root: Path) -> None:
    """The returned QSettings must be IniFormat — *not* native.
    A future "let's just go back to NativeFormat for taskbar
    integration" patch would break the portability contract;
    this test catches that immediately."""
    handle = _settings()
    assert handle.format() == QSettings.Format.IniFormat
    assert Path(handle.fileName()) == fake_ini_root / SETTINGS_INI_FILENAME


def test_settings_round_trip_writes_to_ini_file(
    fake_ini_root: Path,
) -> None:
    """Writing through ``_settings()`` must materialise as an
    actual on-disk INI file at the project-root path. Pin this
    end-to-end so a future change that, say, redirects to
    ``QStandardPaths.writableLocation`` would fail loudly."""
    handle = _settings()
    handle.setValue("test_round_trip_key", "round-trip value")
    handle.sync()

    ini_path = fake_ini_root / SETTINGS_INI_FILENAME
    assert ini_path.is_file(), (
        f"Expected INI at {ini_path}, but it does not exist. "
        f"Directory contents: {list(fake_ini_root.iterdir())}"
    )

    # Read the file back through a brand-new QSettings instance
    # to prove the value really crossed disk (not just kept in
    # the original QSettings' in-memory buffer).
    reread = QSettings(str(ini_path), QSettings.Format.IniFormat)
    assert reread.value("test_round_trip_key", "", str) == "round-trip value"


def test_settings_ini_contents_are_plaintext(fake_ini_root: Path) -> None:
    """The whole point of the INI switch is that preferences are
    inspectable / editable / deletable with a text editor. Pin
    the plaintext property by writing a value, then grepping
    the file contents for it. Defends against any future
    refactor that swaps in an encrypted or binary INI variant."""
    handle = _settings()
    handle.setValue("inspectable_key", "human_readable_marker")
    handle.sync()

    ini_path = fake_ini_root / SETTINGS_INI_FILENAME
    body = ini_path.read_text(encoding="utf-8", errors="strict")
    assert "inspectable_key" in body
    assert "human_readable_marker" in body


# ---------------------------------------------------------------------------
# Native -> INI migration
# ---------------------------------------------------------------------------


def _make_fake_legacy_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    preload: dict[str, object] | None = None,
) -> Path:
    """Build a fake "legacy native" QSettings backend at a tmp INI
    path, optionally pre-populated with key/value pairs. Patches
    :func:`_legacy_native_settings` to return a fresh
    ``QSettings`` against that file each time it's called.

    Returns the tmp INI path so tests can inspect / mutate it
    directly when needed.
    """
    legacy_path = tmp_path / "fake_legacy.ini"
    if preload:
        seed = QSettings(str(legacy_path), QSettings.Format.IniFormat)
        for key, value in preload.items():
            seed.setValue(key, value)
        seed.sync()

    def _factory() -> QSettings:
        return QSettings(str(legacy_path), QSettings.Format.IniFormat)

    monkeypatch.setattr(
        settings_store, "_legacy_native_settings", _factory
    )
    return legacy_path


def test_migration_no_op_when_ini_already_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the new INI already exists on disk, the migration helper
    must NOT touch the legacy backend even if it's populated.
    This is what protects an existing v3.3+ install from being
    overwritten by stale registry state when (e.g.) the user
    sideloads a one-off older build that re-creates legacy keys."""
    ini_path = tmp_path / "settings.ini"
    # Pre-create the INI with a sentinel value the migration must NOT overwrite.
    existing = QSettings(str(ini_path), QSettings.Format.IniFormat)
    existing.setValue("preexisting", "must_not_be_touched")
    existing.sync()

    legacy_path = _make_fake_legacy_store(
        monkeypatch,
        tmp_path,
        preload={"legacy_key": "should_not_appear"},
    )

    _migrate_legacy_native_settings_if_needed(ini_path)

    after = QSettings(str(ini_path), QSettings.Format.IniFormat)
    assert after.value("preexisting", "", str) == "must_not_be_touched"
    assert after.value("legacy_key", "", str) == "", (
        "Migration must not run when the INI already exists; "
        "legacy_key should not have been copied in."
    )
    # Legacy backend untouched (still has its data).
    legacy_after = QSettings(str(legacy_path), QSettings.Format.IniFormat)
    assert legacy_after.value("legacy_key", "", str) == "should_not_appear"


def test_migration_no_op_when_legacy_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh-install case: no INI exists, no legacy keys exist.
    Migration must be a no-op — must not create an empty INI
    just to mark migration done (we use the INI's *existence*
    as the done-marker, so creating it eagerly would short-
    circuit the lazy INI-creation that ``_settings()`` relies
    on for fresh installs)."""
    ini_path = tmp_path / "settings.ini"
    _make_fake_legacy_store(monkeypatch, tmp_path, preload=None)

    _migrate_legacy_native_settings_if_needed(ini_path)

    assert not ini_path.exists(), (
        "Empty legacy backend should leave the INI uncreated "
        "(fresh-install case)."
    )


def test_migration_copies_keys_and_clears_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The main happy path. With a populated legacy backend and
    no INI, the helper must:

    1. Create the INI with every legacy key/value copied across.
    2. Wipe our entries from the legacy backend so the registry
       (or Linux ~/.config, or macOS plist) stops carrying our
       state.
    """
    ini_path = tmp_path / "settings.ini"
    legacy_path = _make_fake_legacy_store(
        monkeypatch,
        tmp_path,
        preload={
            "font_table_px": 14,
            "show_satellite": False,
            "satellite_download_decision": "accepted",
            "map_link_provider": "bing",
        },
    )

    _migrate_legacy_native_settings_if_needed(ini_path)

    assert ini_path.exists(), "Migration must create the INI file"
    after = QSettings(str(ini_path), QSettings.Format.IniFormat)
    # QSettings serialises ints as strings in INI format. Compare
    # against the round-trip-stringified value to avoid type-coercion
    # surprises on platforms that serialise differently.
    assert str(after.value("font_table_px")) == "14"
    assert after.value("show_satellite", True, bool) is False
    assert (
        after.value("satellite_download_decision", "", str) == "accepted"
    )
    assert after.value("map_link_provider", "", str) == "bing"

    # Legacy backend wiped of OUR keys.
    legacy_after = QSettings(str(legacy_path), QSettings.Format.IniFormat)
    assert legacy_after.allKeys() == [], (
        "Migration must clear() the legacy backend's keys so we stop "
        "polluting the OS-native preference store. Remaining keys: "
        f"{legacy_after.allKeys()!r}"
    )


def test_migration_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running the migration twice in a row must be safe. After
    the first call the INI exists, so the second call is a
    no-op — confirm by writing a sentinel into the INI between
    the two calls and verifying it survives."""
    ini_path = tmp_path / "settings.ini"
    _make_fake_legacy_store(
        monkeypatch,
        tmp_path,
        preload={"k": "v"},
    )

    _migrate_legacy_native_settings_if_needed(ini_path)
    assert ini_path.exists()

    # User-written sentinel between the two migration calls.
    handle = QSettings(str(ini_path), QSettings.Format.IniFormat)
    handle.setValue("sentinel", "still_here_after_second_migrate")
    handle.sync()

    # Second migration call (same arguments). Must not blow away
    # the sentinel.
    _migrate_legacy_native_settings_if_needed(ini_path)

    after = QSettings(str(ini_path), QSettings.Format.IniFormat)
    assert (
        after.value("sentinel", "", str)
        == "still_here_after_second_migrate"
    )


def test_settings_triggers_migration_on_first_call(
    fake_ini_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production-side wiring check: calling :func:`_settings`
    on a fresh install with legacy state present must (a)
    migrate, then (b) return a handle whose round-trip
    reflects the migrated values. This is the path that runs
    on the user's machine the first time the v3.3+ build
    launches against pre-v3.3 settings."""
    ini_path = fake_ini_root / SETTINGS_INI_FILENAME
    assert not ini_path.exists()

    legacy_path = _make_fake_legacy_store(
        monkeypatch,
        fake_ini_root,
        preload={"font_table_px": 22, "show_satellite": True},
    )

    handle = _settings()

    # New INI exists.
    assert ini_path.is_file()
    # Migrated values are readable through the returned handle.
    assert str(handle.value("font_table_px")) == "22"
    assert handle.value("show_satellite", False, bool) is True
    # Legacy backend got cleared as part of the migration.
    legacy_after = QSettings(str(legacy_path), QSettings.Format.IniFormat)
    assert legacy_after.allKeys() == []


def test_settings_does_not_re_migrate_when_ini_exists(
    fake_ini_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once migration has run, ``_settings()`` must read from
    the INI only and never re-touch the legacy backend — even
    if a (malicious / stale / lab-only) tool wrote new values
    back into the legacy store after migration. Pin this so
    the wipe-and-stop-reading contract isn't accidentally
    weakened."""
    ini_path = fake_ini_root / SETTINGS_INI_FILENAME
    # Simulate post-migration state: INI exists with a value.
    handle1 = _settings()
    handle1.setValue("post_migration_key", "live_value")
    handle1.sync()
    assert ini_path.is_file()

    # Someone re-pollutes the legacy backend after migration.
    legacy_path = _make_fake_legacy_store(
        monkeypatch,
        fake_ini_root,
        preload={"post_migration_key": "should_be_ignored"},
    )

    handle2 = _settings()
    assert (
        handle2.value("post_migration_key", "", str) == "live_value"
    ), (
        "Second _settings() call must read the INI's live_value, "
        "not the legacy backend's polluting value."
    )
    # Legacy backend is left alone (not cleared, not read).
    legacy_after = QSettings(str(legacy_path), QSettings.Format.IniFormat)
    assert legacy_after.value("post_migration_key", "", str) == (
        "should_be_ignored"
    )


def test_migration_preserves_legacy_value_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-v3.3 keys were a mix of strings, ints, and bools, all
    serialised by QSettings' native backend. The migration must
    preserve enough information that the *typed* read APIs
    (``load_font_table_px`` etc., which pass an int default to
    ``QSettings.value(...)``) still return the correct values
    on the new INI. Pin this by writing a representative mix
    and reading each one back through the same typed-default
    pattern the production loaders use."""
    ini_path = tmp_path / "settings.ini"
    _make_fake_legacy_store(
        monkeypatch,
        tmp_path,
        preload={
            "font_route_text_px": 16,
            "autoload_on_start": "true",
            "map_link_provider": "bing",
            "map_view_m11": 0.4370563762577312,
            "map_view_scroll_h": 970,
        },
    )

    _migrate_legacy_native_settings_if_needed(ini_path)

    after = QSettings(str(ini_path), QSettings.Format.IniFormat)
    assert after.value("font_route_text_px", 0, int) == 16
    # autoload_on_start was stored as a "true" string in the
    # legacy backend (QSettings doesn't always type-tag ints/
    # bools across backends). The bool typed read must coerce
    # it correctly.
    assert after.value("autoload_on_start", False, bool) is True
    assert after.value("map_link_provider", "", str) == "bing"
    # Float values need to round-trip the magnitude — exact-bits
    # precision varies between backends, but 4 sig figs is well
    # within INI text precision.
    assert (
        abs(after.value("map_view_m11", 0.0, float) - 0.4370563762577312)
        < 1e-9
    )
    assert after.value("map_view_scroll_h", 0, int) == 970


# ---------------------------------------------------------------------------
# Negative tests — what the new backend MUST NOT do
# ---------------------------------------------------------------------------


def test_settings_does_not_use_native_format(
    fake_ini_root: Path,  # noqa: ARG001
) -> None:
    """Belt-and-braces: a separate explicit assertion that the
    returned format is NOT ``NativeFormat``. ``NativeFormat`` on
    Windows == registry; pin its absence loudly so a regression
    that silently slides back to it (via ``QSettings(org, app)``)
    fails this test by name."""
    handle = _settings()
    assert handle.format() != QSettings.Format.NativeFormat


def test_settings_ini_path_is_absolute() -> None:
    """The INI path must always be absolute so a working-
    directory change between launches doesn't shift it.
    PyInstaller's ``sys.executable`` is absolute; dev's
    ``Path(__file__).resolve().parents[1]`` is absolute; this
    test pins the contract."""
    assert _settings_ini_path().is_absolute()
