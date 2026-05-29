"""Tests for the v3.3+ path-or-URL acceptance in
:class:`cvfr_routemaster.settings_dialog.SettingsDialog`.

The dialog grew URL-accepting validation alongside the existing
local-path validation. These tests pin the contract that:

* A well-formed ``http(s)://`` URL is accepted as a source.
* A malformed URL is rejected with a specific message (so the
  user can fix it without trial-and-error).
* A local path still works the same way as v3.2 did.
* Mixed mode (one URL + two paths, etc.) is permitted — the dev's
  pre-release workflow needs to swap in a new URL for re-cal while
  keeping the other sheets pointed at local files.
* Browse stays as a local-file picker (no URL affordance).
"""

from __future__ import annotations

from pathlib import Path

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFileDialog,
    QMessageBox,
)

from cvfr_routemaster.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """Single ``QApplication`` per module."""
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# URL detection (static helper)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "https://www.gov.il/path/x.pdf",
        "http://example.com/file.pdf",
        "HTTPS://example.com/x.pdf",
        "  https://example.com/x.pdf  ",
    ],
)
def test_looks_like_url_accepts_http_and_https(text: str) -> None:
    """Both schemes count; case-insensitive; surrounding
    whitespace tolerated (mirrors what a paste from the address
    bar typically contains)."""
    assert SettingsDialog._looks_like_url(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "C:/charts/north.pdf",
        "/home/me/north.pdf",
        "./relative/path.pdf",
        "ftp://example.com/x.pdf",
        "file:///home/me/x.pdf",
        "ht",
        "http",  # bare scheme without the //, not a URL
    ],
)
def test_looks_like_url_rejects_non_http_inputs(text: str) -> None:
    """Anything that isn't ``http(s)://...`` must NOT trigger
    URL-validation mode — this includes other URL schemes
    (``ftp://``, ``file://``) that the download machinery doesn't
    support, mid-edit half-strings (``ht``), and bare paths."""
    assert not SettingsDialog._looks_like_url(text)


# ---------------------------------------------------------------------------
# URL validation (static helper)
# ---------------------------------------------------------------------------


def test_validate_url_accepts_well_formed_caai_url() -> None:
    """The actual CAAI URLs we ship as defaults must pass
    validation — otherwise a user with the shipped defaults
    couldn't click Load now."""
    from cvfr_routemaster.chart_source import CAAI_CHART_URLS

    for url in CAAI_CHART_URLS.values():
        assert SettingsDialog._validate_url(url) is None, (
            f"CAAI default URL must validate: {url}"
        )


def test_validate_url_rejects_unsupported_scheme() -> None:
    """ftp:// and file:// have valid URL syntax but the download
    machinery only handles http(s). Reject early with a
    message naming the unsupported scheme."""
    err = SettingsDialog._validate_url("ftp://example.com/x.pdf")
    assert err is not None
    assert "ftp" in err.lower() or "unsupported" in err.lower()


def test_validate_url_rejects_missing_host() -> None:
    """``https:///x.pdf`` parses to empty netloc — the user
    almost certainly meant ``https://example.com/x.pdf``."""
    err = SettingsDialog._validate_url("https:///x.pdf")
    assert err is not None
    assert "host" in err.lower() or "//" in err


def test_validate_url_rejects_missing_path() -> None:
    """``https://example.com`` (no resource) is not actionable —
    we have to know which file to fetch. Reject with a clear
    message."""
    err = SettingsDialog._validate_url("https://example.com")
    assert err is not None
    assert "specific" in err.lower() or "path" in err.lower()


def test_validate_url_rejects_bare_root_path() -> None:
    """``https://example.com/`` (path == "/") is also ambiguous —
    the server's root index page isn't a PDF."""
    err = SettingsDialog._validate_url("https://example.com/")
    assert err is not None


# ---------------------------------------------------------------------------
# Dialog-level path validation (mixed modes)
# ---------------------------------------------------------------------------


def test_dialog_accepts_three_urls(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The all-URL case — what a fresh v3.3+ install sees out of
    the box. Must validate without complaint."""
    warned: list[tuple] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kw: warned.append(args),
    )

    dlg = SettingsDialog(
        "https://www.gov.il/path/n.pdf",
        "https://www.gov.il/path/s.pdf",
        "https://www.gov.il/path/b.pdf",
        autoload_on_start=False,
    )
    assert dlg._validate_paths() is True
    assert warned == [], f"unexpected warnings: {warned}"


def test_dialog_accepts_mixed_url_and_local_path(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dev's pre-release workflow: paste a new URL for one sheet
    (to re-calibrate against an updated CAAI publication) while
    leaving the others as local paths. Must validate."""
    warned: list[tuple] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kw: warned.append(args),
    )

    local_south = tmp_path / "south.pdf"
    local_back = tmp_path / "back.pdf"
    local_south.write_bytes(b"%PDF-1.4\nx")
    local_back.write_bytes(b"%PDF-1.4\nx")

    dlg = SettingsDialog(
        "https://www.gov.il/path/n.pdf",
        str(local_south),
        str(local_back),
        autoload_on_start=False,
    )
    assert dlg._validate_paths() is True
    assert warned == []


def test_dialog_rejects_empty_url_text(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty field is still rejected, with the v3.2 wording
    updated to talk about ``sources`` rather than ``paths`` (it
    could be either)."""
    warned: list[tuple] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kw: warned.append(args),
    )

    dlg = SettingsDialog("", "", "", autoload_on_start=False)
    assert dlg._validate_paths() is False
    assert len(warned) == 1
    # Validate the user-facing message is about the missing source.
    title, body = warned[0][1], warned[0][2]
    assert title == "Incomplete"
    assert "source" in body.lower() or "path" in body.lower()


def test_dialog_rejects_malformed_url_with_specific_message(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user pasting ``htts://`` (typo) must see a clear
    "URL invalid" warning with the offending source identified
    — not "missing file" (which is misleading when the user
    knows they typed a URL)."""
    warned: list[tuple] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kw: warned.append(args),
    )

    local = tmp_path / "ok.pdf"
    local.write_bytes(b"%PDF-1.4\nx")
    # First field has a typo — note the URL detector still fires
    # on ``http://`` substring, so even a half-typed URL is
    # validated as a URL (not as a path).
    dlg = SettingsDialog(
        "http:///missing_host.pdf",  # typo: missing host
        str(local),
        str(local),
        autoload_on_start=False,
    )
    assert dlg._validate_paths() is False
    title = warned[0][1]
    assert "URL" in title or "url" in title.lower()


def test_dialog_rejects_unsupported_scheme(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ftp://`` is a valid-looking URL but our downloader is
    HTTP only. The dialog must catch this here so the user fixes
    it before kicking off a load that would fail seconds later."""
    warned: list[tuple] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kw: warned.append(args),
    )

    local = tmp_path / "ok.pdf"
    local.write_bytes(b"%PDF-1.4\nx")
    # ftp:// does NOT trigger our URL detector (we only match
    # http(s)://) so the dialog will try to validate it as a
    # local path → fails the is_file() check → "neither URL
    # nor existing file" message.
    dlg = SettingsDialog(
        "ftp://example.com/x.pdf",
        str(local),
        str(local),
        autoload_on_start=False,
    )
    assert dlg._validate_paths() is False
    body = warned[0][2]
    # The path-mode error message names BOTH paths and URLs so
    # the user gets a hint that URLs use a specific scheme.
    assert "URL" in body or "url" in body.lower() or "http" in body.lower()


def test_dialog_rejects_local_path_not_a_file(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy contract: a local path that doesn't exist is
    rejected. Same v3.2 behaviour, updated wording (now mentions
    URL alternative so the user knows that's an option)."""
    warned: list[tuple] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kw: warned.append(args),
    )

    dlg = SettingsDialog(
        "C:/does/not/exist.pdf",
        "C:/does/not/exist2.pdf",
        "C:/does/not/exist3.pdf",
        autoload_on_start=False,
    )
    assert dlg._validate_paths() is False
    title = warned[0][1]
    assert "Missing" in title or "file" in title.lower()


# ---------------------------------------------------------------------------
# Placeholder copy + Browse semantics
# ---------------------------------------------------------------------------


def test_dialog_lineedits_show_path_or_url_placeholder(
    qapp: QApplication,
) -> None:
    """The empty-state placeholder must hint at the path-or-URL
    contract. Without this, a fresh-install user opening the
    dialog with all three fields empty has no idea URLs are
    acceptable."""
    dlg = SettingsDialog("", "", "", autoload_on_start=False)
    assert "URL" in dlg._north.placeholderText()
    assert "URL" in dlg._south.placeholderText()
    assert "URL" in dlg._back.placeholderText()


def test_dialog_browse_still_picks_local_file_only(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Browse button still pops the local-file picker. URLs
    are pasted in directly — there's no URL-picker affordance.
    Verify by intercepting QFileDialog and confirming Browse
    routes there."""
    intercepted: list[bool] = []

    def fake_get_open_file_name(*_args, **_kw):
        intercepted.append(True)
        return ("", "")

    monkeypatch.setattr(QFileDialog, "getOpenFileName", fake_get_open_file_name)

    dlg = SettingsDialog("", "", "", autoload_on_start=False)
    dlg._browse(dlg._north)
    assert intercepted == [True]


# ---------------------------------------------------------------------------
# Hint copy mentions the URL contract
# ---------------------------------------------------------------------------


def test_dialog_hint_label_mentions_url_support(
    qapp: QApplication,
) -> None:
    """The static hint label must tell the user URLs are accepted
    — otherwise the path-or-URL placeholder hint alone is the
    only signal. Without the explicit explanation a user might
    miss the new behaviour entirely."""
    dlg = SettingsDialog("", "", "", autoload_on_start=False)
    # Walk the dialog's QLabel children and look for the hint.
    from PySide6.QtWidgets import QLabel

    labels = [
        lbl.text() for lbl in dlg.findChildren(QLabel)
    ]
    combined = " ".join(labels)
    assert "URL" in combined or "url" in combined.lower(), (
        "Settings dialog hint must mention URL support"
    )
    assert "https" in combined.lower(), (
        "Settings dialog hint should mention the https:// scheme"
    )
