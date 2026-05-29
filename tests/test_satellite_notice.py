"""Tests for the v3.3+ satellite-imagery notification flow.

In v3.3+ the satellite-imagery download is unconditional: there is
no accept-or-decline prompt, no resume-vs-restart dialog, no manual
trigger button. The only user-facing surface is a one-shot
informational notice that fires once per install before the first
download begins.

These tests pin the contract that flow rests on:

  1. :func:`load_satellite_notice_shown` / :func:`save_satellite_notice_shown`
     persist a boolean across QSettings round-trips.
  2. The legacy tri-state ``satellite_download_decision`` key from
     pre-v3.3 builds migrates correctly: ``"accepted"`` → notice
     was already shown (silent resume); anything else → notice
     re-fires.
  3. :func:`show_first_download_notice` builds and exec's without
     crashing, and the text body actually contains the four pieces
     of information the v3.3 redesign promised the user:
     download size, zoom range, resume-on-interrupt language, and
     the Esri attribution.

The first two are pure ``settings_store`` tests that don't need
Qt's widget machinery (``QSettings`` is loaded from ``QtCore``).
The third uses ``qapp`` and monkeypatches ``QMessageBox.exec`` so
the dialog never actually blocks the test runner.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from cvfr_routemaster import satellite_dialog  # noqa: E402
from cvfr_routemaster import settings_store  # noqa: E402
from cvfr_routemaster.satellite_dialog import (  # noqa: E402
    show_first_download_notice,
)
from cvfr_routemaster.settings_store import (  # noqa: E402
    load_satellite_notice_shown,
    save_satellite_notice_shown,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """One QApplication per module, same pattern as the other UI
    tests in this suite."""
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def isolated_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect ``settings_store._settings()`` to a per-test INI
    file so legacy-key migration tests don't leak into one
    another's QSettings state. Returns the INI path for caller-
    side reads / writes."""
    ini_path = tmp_path / "test_settings.ini"

    def _factory() -> QSettings:
        return QSettings(str(ini_path), QSettings.Format.IniFormat)

    monkeypatch.setattr(settings_store, "_settings", _factory)
    return ini_path


# ---------------------------------------------------------------------------
# satellite_notice_shown round-trip
# ---------------------------------------------------------------------------


def test_load_returns_false_on_fresh_install(
    isolated_settings: Path,  # noqa: ARG001
) -> None:
    """Default on a fresh install (no settings at all) must be
    ``False`` so the notice fires on first launch. Both the new
    key and the legacy key are absent; the function must NOT
    raise."""
    assert load_satellite_notice_shown() is False


def test_save_true_then_load_returns_true(
    isolated_settings: Path,  # noqa: ARG001
) -> None:
    """The most basic round trip: write True, read True back.
    Same-session, no need for QSettings to flush to disk."""
    save_satellite_notice_shown(True)
    assert load_satellite_notice_shown() is True


def test_save_false_then_load_returns_false(
    isolated_settings: Path,  # noqa: ARG001
) -> None:
    """Round-trip for the opposite branch. Distinct from "fresh
    install" because the key is now *set* to a falsy value, not
    *absent* — verify both code paths in :func:`load` agree."""
    save_satellite_notice_shown(False)
    assert load_satellite_notice_shown() is False


def test_save_persists_across_settings_factory_recreation(
    isolated_settings: Path,
) -> None:
    """The notice flag must survive a QSettings re-open, the way
    it would across an actual program restart. Pin this by
    forcing a sync to disk and re-instantiating QSettings against
    the same INI path."""
    save_satellite_notice_shown(True)
    QSettings(str(isolated_settings), QSettings.Format.IniFormat).sync()
    s2 = QSettings(str(isolated_settings), QSettings.Format.IniFormat)
    # Read directly so we don't rely on the monkeypatched factory's
    # caching behaviour either.
    raw = s2.value("satellite_notice_shown", False, bool)
    assert bool(raw) is True


# ---------------------------------------------------------------------------
# Legacy-key migration (pre-v3.3 -> v3.3+)
# ---------------------------------------------------------------------------


def test_legacy_accepted_decision_migrates_to_notice_shown_true(
    isolated_settings: Path,
) -> None:
    """A returning user who previously clicked "Download Now" on
    the old v3.2 first-launch dialog should NOT see the new
    notice — they've already been informed. Pin the migration
    by writing the legacy key with ``"accepted"`` and verifying
    :func:`load_satellite_notice_shown` returns True without
    the new key being present."""
    s = QSettings(str(isolated_settings), QSettings.Format.IniFormat)
    s.setValue("satellite_download_decision", "accepted")
    s.sync()
    assert load_satellite_notice_shown() is True


def test_legacy_declined_decision_migrates_to_notice_shown_false(
    isolated_settings: Path,
) -> None:
    """A returning user who previously clicked "Skip for Now" on
    the v3.2 dialog should now see the v3.3+ notice — the
    decline path no longer exists, so they need to be
    re-onboarded into the simpler unconditional-download flow.
    Pin the migration."""
    s = QSettings(str(isolated_settings), QSettings.Format.IniFormat)
    s.setValue("satellite_download_decision", "declined")
    s.sync()
    assert load_satellite_notice_shown() is False


def test_legacy_empty_decision_migrates_to_notice_shown_false(
    isolated_settings: Path,
) -> None:
    """A returning user who launched v3.2 but never answered the
    dialog (closed it via X / Esc) should see the new notice
    too — they're effectively a fresh install from the v3.3+
    state machine's perspective."""
    s = QSettings(str(isolated_settings), QSettings.Format.IniFormat)
    s.setValue("satellite_download_decision", "")
    s.sync()
    assert load_satellite_notice_shown() is False


def test_new_key_takes_precedence_over_legacy(
    isolated_settings: Path,
) -> None:
    """If both keys are set (e.g. a v3.3+ user who later runs an
    older build and back again), the new key wins. Pin this so
    we can't accidentally fall back to the legacy key after the
    new one is already authoritative."""
    s = QSettings(str(isolated_settings), QSettings.Format.IniFormat)
    s.setValue("satellite_download_decision", "declined")
    s.setValue("satellite_notice_shown", True)
    s.sync()
    assert load_satellite_notice_shown() is True


# ---------------------------------------------------------------------------
# show_first_download_notice
# ---------------------------------------------------------------------------


def _captured_message_text(box: QMessageBox) -> str:
    """Glue together the message box's text + informative text so a
    single substring search can hit either."""
    return f"{box.text()}\n{box.informativeText()}"


def test_show_first_download_notice_promises_size(qapp: QApplication) -> None:  # noqa: ARG001
    """The notice must quote the tile count and an approximate
    disk size so the user understands what's about to happen.
    We monkeypatch ``QMessageBox.exec`` to a no-op so the
    dialog never actually blocks the test runner, but
    ``box.setInformativeText`` has already been called by the
    time exec runs, so capturing the text is reliable."""
    captured: dict[str, str] = {}

    def fake_exec(self: QMessageBox) -> int:
        captured["body"] = _captured_message_text(self)
        return int(QMessageBox.StandardButton.Ok)

    with patch.object(QMessageBox, "exec", fake_exec):
        show_first_download_notice(
            None, tile_count=12_345, zoom_levels=[12, 13, 14, 15]
        )

    body = captured.get("body", "")
    assert "12,345" in body, (
        f"Notice must show the formatted tile count (with thousands "
        f"separator). Got: {body!r}"
    )
    assert "MB" in body or "GB" in body, (
        f"Notice must quote a size in MB or GB. Got: {body!r}"
    )


def test_show_first_download_notice_promises_resume(
    qapp: QApplication,  # noqa: ARG001
) -> None:
    """Must explicitly tell the user that the download resumes
    across program restarts — the whole point of the v3.3+
    redesign was making interruption safe and not requiring a
    consent dialog. Pin the resume language verbatim enough that
    a future re-wording removing the promise fails this test."""
    captured: dict[str, str] = {}

    def fake_exec(self: QMessageBox) -> int:
        captured["body"] = _captured_message_text(self)
        return int(QMessageBox.StandardButton.Ok)

    with patch.object(QMessageBox, "exec", fake_exec):
        show_first_download_notice(
            None, tile_count=10_000, zoom_levels=[12, 13]
        )

    body = captured.get("body", "")
    assert "resume" in body.lower(), (
        f"Notice must contain the word 'resume' (or 'resumes') so "
        f"the user knows interruption is safe. Got: {body!r}"
    )


def test_show_first_download_notice_shows_zoom_range(
    qapp: QApplication,  # noqa: ARG001
) -> None:
    """Must surface the zoom range so a user inspecting the cache
    directory can correlate the on-disk z-folders with the
    notice text. Default config is ``[12, 13, 14, 15]``."""
    captured: dict[str, str] = {}

    def fake_exec(self: QMessageBox) -> int:
        captured["body"] = _captured_message_text(self)
        return int(QMessageBox.StandardButton.Ok)

    with patch.object(QMessageBox, "exec", fake_exec):
        show_first_download_notice(
            None, tile_count=10_000, zoom_levels=[12, 13, 14, 15]
        )

    body = captured.get("body", "")
    assert "z=12" in body and "z=15" in body, (
        f"Notice must surface the zoom range (z=12-z=15 for the "
        f"default chain). Got: {body!r}"
    )


def test_show_first_download_notice_carries_esri_attribution(
    qapp: QApplication,  # noqa: ARG001
) -> None:
    """Esri's tile-service terms require attribution wherever the
    cached imagery is surfaced. The Legal & Copyright dialog
    carries the same attribution, but a user dismissing the
    notice without later visiting Legal must still have seen the
    attribution at least once. Pin Esri specifically."""
    captured: dict[str, str] = {}

    def fake_exec(self: QMessageBox) -> int:
        captured["body"] = _captured_message_text(self)
        return int(QMessageBox.StandardButton.Ok)

    with patch.object(QMessageBox, "exec", fake_exec):
        show_first_download_notice(
            None, tile_count=10_000, zoom_levels=[12, 13, 14, 15]
        )

    body = captured.get("body", "")
    assert "Esri" in body, (
        f"Notice must attribute Esri (tile-service terms require "
        f"attribution wherever the cached imagery is surfaced). "
        f"Got: {body!r}"
    )


def test_show_first_download_notice_uses_information_icon(
    qapp: QApplication,  # noqa: ARG001
) -> None:
    """Notice is informational, not consensual. Pin
    ``QMessageBox.Icon.Information`` so a future "let's make it
    a Warning" refactor (which would visually re-frame it as
    something the user needs to react to) fails here. The user
    has no decision to make."""
    captured_icon: dict[str, QMessageBox.Icon] = {}

    def fake_exec(self: QMessageBox) -> int:
        captured_icon["icon"] = self.icon()
        return int(QMessageBox.StandardButton.Ok)

    with patch.object(QMessageBox, "exec", fake_exec):
        show_first_download_notice(
            None, tile_count=1_000, zoom_levels=[12]
        )

    assert (
        captured_icon.get("icon") == QMessageBox.Icon.Information
    ), (
        f"Notice must use Information icon, not Warning/Question. "
        f"Got: {captured_icon.get('icon')!r}"
    )


def test_show_first_download_notice_has_only_ok_button(
    qapp: QApplication,  # noqa: ARG001
) -> None:
    """No accept/decline branching in v3.3+. Pin that the dialog
    surfaces exactly one button (OK). A future refactor that
    re-introduces a "skip" or "later" button fails this test
    by design — the whole point of the v3.3+ flow is making the
    download not-a-choice."""
    captured_buttons: dict[str, QMessageBox.StandardButtons] = {}

    def fake_exec(self: QMessageBox) -> int:
        captured_buttons["buttons"] = self.standardButtons()
        return int(QMessageBox.StandardButton.Ok)

    with patch.object(QMessageBox, "exec", fake_exec):
        show_first_download_notice(
            None, tile_count=1_000, zoom_levels=[12]
        )

    buttons = captured_buttons.get("buttons")
    assert buttons == QMessageBox.StandardButton.Ok, (
        f"Notice must have only the OK button. Got: {buttons!r}"
    )


# ---------------------------------------------------------------------------
# Removed-symbol guard
# ---------------------------------------------------------------------------


def test_legacy_consent_functions_are_gone() -> None:
    """The old consent-flow trio (``prompt_first_launch``,
    ``confirm_decline_warning``, ``prompt_resume``) was deleted
    in v3.3+. Pin their absence so a future "restore consent
    flow" refactor has to make a conscious decision about why,
    rather than silently re-adding dialogs the user explicitly
    asked us to remove."""
    assert not hasattr(satellite_dialog, "prompt_first_launch")
    assert not hasattr(satellite_dialog, "confirm_decline_warning")
    assert not hasattr(satellite_dialog, "prompt_resume")
