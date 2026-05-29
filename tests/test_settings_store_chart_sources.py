"""Tests for the v3.3+ chart-sources fallback layer in
:mod:`cvfr_routemaster.settings_store`.

What's new in v3.3
-------------------

* ``_qsetting_path_is_usable`` now accepts URL strings as "usable"
  so URL sources stored in QSettings aren't second-guessed by the
  autodiscovery fallback layer.
* ``load_pdf_paths`` consults ``chart_sources.json`` as a
  second-tier fallback (between QSettings and filesystem
  autodiscovery) so a fresh install with the shipped defaults
  populates the three CAAI URLs automatically.
* ``save_shipped_chart_sources`` writes the JSON atomically; the
  build script calls this during release packaging.

These tests pin the precedence order — QSettings → chart_sources.json
→ legacy autodiscovery — which is the contract the rest of the
chart-fetch flow rests on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings  # noqa: E402

from cvfr_routemaster import settings_store  # noqa: E402
from cvfr_routemaster.settings_store import (  # noqa: E402
    _looks_like_url,
    _qsetting_path_is_usable,
    chart_sources_json_path,
    load_pdf_paths,
    load_shipped_chart_sources,
    save_shipped_chart_sources,
)


@pytest.fixture
def isolated_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect ``settings_store._settings()`` to a temp INI file
    so test mutations don't leak into the user's real registry.
    Returns the INI path for caller-side reads / writes."""
    ini_path = tmp_path / "test_settings.ini"

    def _factory() -> QSettings:
        return QSettings(str(ini_path), QSettings.Format.IniFormat)

    monkeypatch.setattr(settings_store, "_settings", _factory)
    return ini_path


# ---------------------------------------------------------------------------
# URL helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/x.pdf",
        "http://example.com/x.pdf",
        "HTTPS://Example.com/x.pdf",
        "  https://x.com/y.pdf  ",
    ],
)
def test_looks_like_url_accepts_http_schemes(text: str) -> None:
    assert _looks_like_url(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "C:/x.pdf",
        "/home/me/x.pdf",
        "ftp://example.com/x.pdf",
        "file:///x.pdf",
    ],
)
def test_looks_like_url_rejects_non_http(text: str) -> None:
    assert not _looks_like_url(text)


# ---------------------------------------------------------------------------
# _qsetting_path_is_usable
# ---------------------------------------------------------------------------


def test_qsetting_path_is_usable_accepts_url_without_filesystem_check(
    tmp_path: Path,
) -> None:
    """A URL stored in QSettings is always considered usable —
    the file might not yet exist on disk but that's expected for
    a first-run URL source. The chart-fetch layer handles the
    download lazily."""
    assert _qsetting_path_is_usable("https://example.com/never-fetched.pdf")


def test_qsetting_path_is_usable_rejects_missing_local_file(
    tmp_path: Path,
) -> None:
    """A local path pointing at a non-existent file is NOT usable
    — autodiscovery should fire to find a real PDF instead."""
    assert not _qsetting_path_is_usable(str(tmp_path / "missing.pdf"))


def test_qsetting_path_is_usable_rejects_empty_local_file(
    tmp_path: Path,
) -> None:
    """A 0-byte file is the test-pollution / interrupted-download
    case — treat as missing so autodiscovery fires."""
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert not _qsetting_path_is_usable(str(empty))


def test_qsetting_path_is_usable_accepts_real_local_file(
    tmp_path: Path,
) -> None:
    """Legacy v3.2 contract: real, non-empty local PDF is usable."""
    real = tmp_path / "real.pdf"
    real.write_bytes(b"%PDF-1.4\n" + b"x" * 100)
    assert _qsetting_path_is_usable(str(real))


def test_qsetting_path_is_usable_rejects_empty_string() -> None:
    """The truly-empty case — QSettings default for never-set
    keys. Autodiscovery / chart_sources fallback must fire."""
    assert not _qsetting_path_is_usable("")


# ---------------------------------------------------------------------------
# load_shipped_chart_sources / save_shipped_chart_sources
# ---------------------------------------------------------------------------


def test_load_shipped_chart_sources_returns_empty_when_missing(
    tmp_path: Path,
) -> None:
    """No file => empty dict. v3.2 release bundles don't ship
    this file; the loader must NOT crash on them."""
    assert load_shipped_chart_sources(tmp_path) == {}


def test_load_shipped_chart_sources_returns_empty_when_corrupt(
    tmp_path: Path,
) -> None:
    """A malformed JSON must NOT block the program — fall through
    to the next fallback (filesystem autodiscovery)."""
    target_dir = tmp_path / ".cvfr_routemaster"
    target_dir.mkdir()
    chart_sources_json_path(tmp_path).write_text("{not valid", encoding="utf-8")
    assert load_shipped_chart_sources(tmp_path) == {}


def test_save_then_load_shipped_chart_sources_roundtrip(
    tmp_path: Path,
) -> None:
    """The build script writes the file; first-run loads it.
    Validate the strings round-trip verbatim (no normalisation
    happens at this layer)."""
    sources = {
        "north": "https://www.gov.il/path/n.pdf",
        "south": "https://www.gov.il/path/s.pdf",
        "back": "https://www.gov.il/path/b.pdf",
    }
    save_shipped_chart_sources(tmp_path, sources)
    assert load_shipped_chart_sources(tmp_path) == sources


def test_save_shipped_chart_sources_drops_unknown_keys(tmp_path: Path) -> None:
    """An accidental ``"east"`` key (or a typo from manual JSON
    editing) must NOT pollute the on-disk file."""
    save_shipped_chart_sources(
        tmp_path,
        {
            "north": "https://x.com/n.pdf",
            "garbage": "anything",
        },
    )
    loaded = load_shipped_chart_sources(tmp_path)
    assert "garbage" not in loaded
    assert loaded == {"north": "https://x.com/n.pdf"}


def test_save_shipped_chart_sources_drops_empty_values(tmp_path: Path) -> None:
    """Empty-string values are meaningless — they'd appear as
    "no default" to the load path, which is the same as not
    being in the dict. Drop them at write time so the on-disk
    file is minimal."""
    save_shipped_chart_sources(
        tmp_path,
        {"north": "https://x.com/n.pdf", "south": "", "back": "  "},
    )
    loaded = load_shipped_chart_sources(tmp_path)
    assert "north" in loaded
    assert "south" not in loaded


def test_save_shipped_chart_sources_atomic_rename(tmp_path: Path) -> None:
    """No ``.tmp`` sentinel left over after a successful write."""
    save_shipped_chart_sources(tmp_path, {"north": "https://x.com/n.pdf"})
    target = chart_sources_json_path(tmp_path)
    assert target.is_file()
    tmp = target.with_suffix(target.suffix + ".tmp")
    assert not tmp.exists()


def test_save_shipped_chart_sources_creates_parent_dir(tmp_path: Path) -> None:
    """The build script may invoke save on a directory tree that
    doesn't have ``.cvfr_routemaster/`` yet (e.g. building into
    a clean ``release/`` folder). The save must create the
    parent so the build doesn't fail with FileNotFoundError."""
    project_root = tmp_path / "fresh_release"
    project_root.mkdir()
    # No .cvfr_routemaster/ subdirectory yet:
    assert not (project_root / ".cvfr_routemaster").exists()
    save_shipped_chart_sources(
        project_root, {"north": "https://x.com/n.pdf"}
    )
    assert (project_root / ".cvfr_routemaster").is_dir()
    assert load_shipped_chart_sources(project_root) == {
        "north": "https://x.com/n.pdf"
    }


# ---------------------------------------------------------------------------
# load_pdf_paths — precedence order
# ---------------------------------------------------------------------------


def test_load_pdf_paths_qsettings_wins_over_shipped_defaults(
    tmp_path: Path,
    isolated_settings: Path,
) -> None:
    """If the user has set QSettings explicitly (URL or path), it
    must win over the shipped ``chart_sources.json`` defaults.
    Otherwise a user with a custom URL would see it silently
    overridden by a release upgrade."""
    save_shipped_chart_sources(
        tmp_path,
        {
            "north": "https://default.example/n.pdf",
            "south": "https://default.example/s.pdf",
            "back": "https://default.example/b.pdf",
        },
    )
    s = QSettings(str(isolated_settings), QSettings.Format.IniFormat)
    s.setValue("pdf_north", "https://custom.example/n.pdf")
    s.setValue("pdf_south", "https://custom.example/s.pdf")
    s.setValue("pdf_back", "https://custom.example/b.pdf")
    s.sync()

    n, s_, b = load_pdf_paths(tmp_path)
    assert n == "https://custom.example/n.pdf"
    assert s_ == "https://custom.example/s.pdf"
    assert b == "https://custom.example/b.pdf"


def test_load_pdf_paths_falls_through_to_shipped_defaults_when_qsettings_empty(
    tmp_path: Path,
    isolated_settings: Path,  # noqa: ARG001 — ensures the empty INI is in use
) -> None:
    """Fresh install: QSettings empty, chart_sources.json present
    => use the shipped URLs. This is the v3.3+ release first-run
    path."""
    save_shipped_chart_sources(
        tmp_path,
        {
            "north": "https://default.example/n.pdf",
            "south": "https://default.example/s.pdf",
            "back": "https://default.example/b.pdf",
        },
    )

    n, s, b = load_pdf_paths(tmp_path)
    assert n == "https://default.example/n.pdf"
    assert s == "https://default.example/s.pdf"
    assert b == "https://default.example/b.pdf"


def test_load_pdf_paths_falls_through_to_autodiscovery_when_shipped_absent(
    tmp_path: Path,
    isolated_settings: Path,  # noqa: ARG001
) -> None:
    """Legacy v3.2 bundle (no chart_sources.json, but PDFs in
    ``map-pdfs/``) must keep working — friends with an old
    release shouldn't break on upgrade."""
    # No chart_sources.json. Drop PDFs in the legacy map-pdfs/ layout.
    pdfs = tmp_path / "map-pdfs"
    pdfs.mkdir()
    for name in (
        "CVFR-NORTH-OCT-2025-UPD2.pdf",
        "CVFR-SOUTH-OCT-2025-UPD2.pdf",
        "CVFR-BACK-PAGES-OCT-2025-UPD2.pdf",
    ):
        (pdfs / name).write_bytes(b"%PDF-1.4\n" + b"x" * 100)

    n, s, b = load_pdf_paths(tmp_path)
    assert "CVFR-NORTH" in n
    assert "CVFR-SOUTH" in s
    assert "CVFR-BACK" in b


def test_load_pdf_paths_mixed_mode_qsettings_path_plus_shipped_url_fallback(
    tmp_path: Path,
    isolated_settings: Path,
) -> None:
    """If QSettings has a usable value for SOME sheets but not
    others, the missing sheets fall through to chart_sources.json
    independently. This is the realistic "dev customised one
    sheet, the others stayed on defaults" scenario."""
    save_shipped_chart_sources(
        tmp_path,
        {
            "north": "https://default.example/n.pdf",
            "south": "https://default.example/s.pdf",
            "back": "https://default.example/b.pdf",
        },
    )
    real_north = tmp_path / "my_local_north.pdf"
    real_north.write_bytes(b"%PDF-1.4\n" + b"x" * 100)
    s = QSettings(str(isolated_settings), QSettings.Format.IniFormat)
    s.setValue("pdf_north", str(real_north))
    # south and back left unset — should fall through to shipped defaults.
    s.sync()

    n, s_, b = load_pdf_paths(tmp_path)
    assert n == str(real_north)
    assert s_ == "https://default.example/s.pdf"
    assert b == "https://default.example/b.pdf"


def test_load_pdf_paths_url_in_qsettings_skips_autodiscovery(
    tmp_path: Path,
    isolated_settings: Path,
) -> None:
    """A URL in QSettings must NOT trigger filesystem
    autodiscovery (it'd find a stale local PDF and silently
    override the user's URL choice). The URL is the user's
    intent; we honour it."""
    # Set up bait: a real PDF that would match the autodiscovery
    # filename, so if the URL were treated as "not usable" the
    # autodiscovery would return this local path.
    pdfs = tmp_path / "map-pdfs"
    pdfs.mkdir()
    bait = pdfs / "CVFR-NORTH-OCT-2025-UPD2.pdf"
    bait.write_bytes(b"%PDF-1.4\n" + b"x" * 100)

    s = QSettings(str(isolated_settings), QSettings.Format.IniFormat)
    s.setValue("pdf_north", "https://custom.example/n.pdf")
    s.sync()

    n, _, _ = load_pdf_paths(tmp_path)
    # The URL must win, not the autodiscovered local PDF.
    assert n == "https://custom.example/n.pdf"
    assert "CVFR-NORTH" not in n


def test_load_pdf_paths_empty_when_no_layer_supplies_value(
    tmp_path: Path,
    isolated_settings: Path,  # noqa: ARG001
) -> None:
    """All three fallback layers empty => empty strings, caller
    expected to handle by prompting via Settings dialog. The
    function must NOT crash on this 0-layer case."""
    n, s, b = load_pdf_paths(tmp_path)
    assert (n, s, b) == ("", "", "")


# ---------------------------------------------------------------------------
# CAAI URL defaults integrate with chart_source
# ---------------------------------------------------------------------------


def test_shipped_defaults_can_carry_real_caai_urls(
    tmp_path: Path,
    isolated_settings: Path,  # noqa: ARG001
) -> None:
    """The full integration: write the CAAI URLs as shipped
    defaults, load via load_pdf_paths, get back URL strings that
    chart_source can normalize. Catches a regression where one
    layer of the stack stops accepting another's output."""
    from cvfr_routemaster.chart_source import CAAI_CHART_URLS, normalize_url

    save_shipped_chart_sources(tmp_path, CAAI_CHART_URLS)
    n, s, b = load_pdf_paths(tmp_path)
    assert n == CAAI_CHART_URLS["north"]
    assert s == CAAI_CHART_URLS["south"]
    assert b == CAAI_CHART_URLS["back"]
    # The normalization step downstream must accept these as
    # already-canonical (idempotent).
    for url in (n, s, b):
        assert normalize_url(url) == url


def test_chart_sources_json_lives_in_app_cache_subdir(tmp_path: Path) -> None:
    """The JSON's location is peer to the existing seed cache
    JSONs (``font_settings.json``, ``map_layout.json``). Pin
    the layout so a build-script refactor can't quietly move it
    somewhere the runtime won't find it."""
    assert chart_sources_json_path(tmp_path) == (
        tmp_path / ".cvfr_routemaster" / "chart_sources.json"
    )
