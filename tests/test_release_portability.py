"""Path-portability tests for the disk caches that get bundled into a
PyInstaller release zip, plus the frozen-mode project-root resolver.

Background:
    ``scripts/build_release.py`` packages this app for a Windows friend
    by putting ``cvfr-routemaster.exe`` next to the three CVFR PDFs and
    a seeded ``.cvfr_routemaster/`` cache subfolder. The cache JSONs
    were originally written on the dev machine against
    ``<dev-repo-root>\\<pdf>`` paths; on the friend's machine the
    same files land under whatever folder they unzipped into
    (``C:\\Users\\Friend\\Documents\\release\\``, etc.).

    For this to work, the cache fingerprint comparison can NOT depend
    on the absolute path the cache was originally written against —
    only on data the PDF *itself* carries (``size`` is intrinsic;
    ``mtime_ns`` survives ``shutil.copy2`` and zip-extract on Windows
    NTFS). The ``path`` field is still serialised to disk for
    diagnostics ("which PDF was this cache built from?") but is never
    compared at load time.

    These tests pin that contract for all three caches that get
    bundled (altitude arrows, waypoints, rendered chart PNGs) and
    cover the frozen-mode project-root resolver in
    :mod:`cvfr_routemaster.__main__` so a future refactor can't
    accidentally point at PyInstaller's temp ``_MEIPASS`` extraction
    directory and brick a release.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from cvfr_routemaster.altitude_arrows import AltitudeArrow
from cvfr_routemaster.altitude_cache import (
    save_altitude_arrows,
    try_load_altitude_arrows,
)
from cvfr_routemaster.map_crop import CropMeta
from cvfr_routemaster.waypoint_cache import (
    load_cached_waypoints,
    save_waypoint_cache,
)
from cvfr_routemaster.waypoint_types import WaypointRecord


def _make_pdf(folder: Path, name: str = "fake.pdf", payload: bytes | None = None) -> Path:
    p = folder / name
    p.write_bytes(payload or b"%PDF-1.4 portability fixture\n")
    return p


def _crop() -> CropMeta:
    return CropMeta(
        offset_x=0, offset_y=0,
        source_w=2000, source_h=1000,
        cropped_w=2000, cropped_h=1000,
    )


def _arrows() -> list[AltitudeArrow]:
    return [
        AltitudeArrow(u=0.10, v=0.20, bearing_deg=0.0, altitudes_ft=(2000,)),
        AltitudeArrow(u=0.50, v=0.50, bearing_deg=180.0, altitudes_ft=(1500,)),
    ]


def _waypoints() -> list[WaypointRecord]:
    return [
        WaypointRecord(
            index=0, code="LLHZ", name_he="הרצליה",
            reporting_type="MR", lat=32.18, lon=34.83,
            lat_dms="32°10'48\"N", lon_dms="34°49'48\"E",
        ),
        WaypointRecord(
            index=1, code="BAZRA", name_he="בצרה",
            reporting_type="MR", lat=32.205, lon=34.886,
            lat_dms="32°12'18\"N", lon_dms="34°53'09\"E",
        ),
    ]


# ---------------------------------------------------------------------------
# Altitude-arrow cache portability (mirrors test in test_altitude_cache.py;
# duplicated here as part of the release-portability *suite* so a single
# pytest pattern covers all three caches together)
# ---------------------------------------------------------------------------


def test_altitude_cache_hits_after_copying_to_a_new_root(tmp_path: Path) -> None:
    """Dev writes the altitude cache against one absolute PDF path; the
    release zip is unpacked elsewhere on the friend's machine so the
    PDF's absolute path differs but its bytes are identical.

    The altitude cache must still hit — without this contract every
    fresh release burns 3-5 minutes re-extracting altitude arrows
    from both sheets on first launch."""
    dev_root = tmp_path / "dev"
    dev_root.mkdir()
    dev_pdf = _make_pdf(dev_root, "chart.pdf")
    crop = _crop()
    save_altitude_arrows(
        dev_root, dev_pdf, "north", _arrows(), render_dpi=288.0, crop=crop
    )

    friend_root = tmp_path / "C_Users_Friend_Documents_release"
    friend_root.mkdir()
    friend_pdf = friend_root / "chart.pdf"
    shutil.copy2(dev_pdf, friend_pdf)
    shutil.copytree(
        dev_root / ".cvfr_routemaster",
        friend_root / ".cvfr_routemaster",
    )

    out = try_load_altitude_arrows(
        friend_root, friend_pdf, "north", render_dpi=288.0, crop=crop,
    )
    assert out == _arrows()


# ---------------------------------------------------------------------------
# Waypoint cache portability
# ---------------------------------------------------------------------------


def test_waypoint_cache_hits_after_copying_to_a_new_root(tmp_path: Path) -> None:
    """The friend's machine resolves the back-pages PDF to a different
    absolute path than the dev machine. The waypoint cache must still
    hit — otherwise the friend's first launch would either re-OCR the
    back pages (slow + needs Tesseract installed) or, if Tesseract
    isn't on PATH, fail entirely."""
    dev_root = tmp_path / "dev"
    dev_root.mkdir()
    dev_pdf = _make_pdf(dev_root, "back.pdf")
    save_waypoint_cache(dev_root, dev_pdf, _waypoints(), source="ocr")

    friend_root = tmp_path / "release"
    friend_root.mkdir()
    friend_pdf = friend_root / "back.pdf"
    shutil.copy2(dev_pdf, friend_pdf)
    shutil.copytree(
        dev_root / ".cvfr_routemaster",
        friend_root / ".cvfr_routemaster",
    )

    out = load_cached_waypoints(friend_root, friend_pdf)
    assert out == _waypoints()


def test_waypoint_cache_still_invalidates_when_pdf_size_changes(
    tmp_path: Path,
) -> None:
    """Defensive sanity check: dropping the path comparison must not
    weaken the *content* invalidation. A back-pages PDF that has been
    edited (different bytes → different size) must still invalidate
    the cache so the friend re-extracts against the new content."""
    root = tmp_path / "root"
    root.mkdir()
    pdf = _make_pdf(root, "back.pdf", payload=b"original content")
    save_waypoint_cache(root, pdf, _waypoints(), source="ocr")

    pdf.write_bytes(b"different content with a different size altogether")
    out = load_cached_waypoints(root, pdf)
    assert out is None


# ---------------------------------------------------------------------------
# Map-image cache portability
# ---------------------------------------------------------------------------


def test_map_image_cache_hits_after_copying_to_a_new_root(tmp_path: Path) -> None:
    """Dev writes the rendered chart PNGs (north + south) against one
    absolute PDF path; the friend's machine has the same PDF bytes
    at a different absolute path. The map-image cache must still
    hit so the friend doesn't sit through a fresh PDF→PNG render
    pass on every launch."""
    PySide6 = pytest.importorskip("PySide6")  # noqa: N806
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    from cvfr_routemaster.map_image_cache import (
        save_map_png_cache,
        try_load_map_png_cache,
    )

    # QImage save/load needs a Q*Application alive. We deliberately
    # use ``QApplication`` (not the lighter ``QCoreApplication`` /
    # ``QGuiApplication``) so the singleton is compatible with the
    # rest of the suite — ``test_route_panel.py`` instantiates
    # widgets that require ``QApplication`` and Qt only allows one
    # Q*Application instance per process. Creating a non-QApplication
    # instance here would mean a later
    # ``QApplication.instance() or QApplication([])`` in another test
    # returns the wrong-type singleton and the next QWidget call
    # crashes with STATUS_STACK_BUFFER_OVERRUN.
    _ = QApplication.instance() or QApplication([])

    dev_root = tmp_path / "dev"
    dev_root.mkdir()
    dev_north = _make_pdf(dev_root, "north.pdf", payload=b"north bytes")
    dev_south = _make_pdf(dev_root, "south.pdf", payload=b"south bytes")

    img_n = QImage(8, 8, QImage.Format.Format_RGBA8888)
    img_n.fill(0xFF000000)
    img_s = QImage(8, 8, QImage.Format.Format_RGBA8888)
    img_s.fill(0xFFFFFFFF)
    crop = _crop()

    save_map_png_cache(
        dev_root, dev_north, dev_south,
        img_n=img_n, img_s=img_s,
        render_dpi=288.0, max_edge_px=8,
        crop_n=crop, crop_s=crop,
        effective_render_dpi=288.0,
    )

    friend_root = tmp_path / "release"
    friend_root.mkdir()
    friend_north = friend_root / "north.pdf"
    friend_south = friend_root / "south.pdf"
    shutil.copy2(dev_north, friend_north)
    shutil.copy2(dev_south, friend_south)
    shutil.copytree(
        dev_root / ".cvfr_routemaster",
        friend_root / ".cvfr_routemaster",
    )

    out = try_load_map_png_cache(
        friend_root, friend_north, friend_south,
        render_dpi=288.0, max_edge_px=8,
    )
    assert out is not None, (
        "map-image cache should hit on the friend's machine even though "
        "the PDF absolute paths differ from the dev machine's"
    )


# ---------------------------------------------------------------------------
# Geo-calibration cache portability
# ---------------------------------------------------------------------------


def test_geo_calibration_cache_hits_after_copying_to_a_new_root(tmp_path: Path) -> None:
    """The fourth cache (joining altitude arrows, waypoints, and map
    PNGs from above): the per-sheet north/south chart calibration.
    Stored in ``.cvfr_routemaster/geo_calibration.json`` with a
    ``pdf`` fingerprint per sheet block, this is what tells the app
    where to render route lines on the chart pixmap.

    Without path-independence here, a v2 release prompts the friend
    (and the user themselves, after restructuring PDFs into
    ``map-pdfs/``) to re-calibrate north + south on first launch
    even though we shipped a perfectly valid calibration JSON —
    a manual 8-anchor click ritual that takes ~5 minutes per sheet
    and the friend has no chart-anchor knowledge to do correctly
    in the first place.
    """
    from cvfr_routemaster.geo_calibration import (
        CalibrationPoint,
        calibration_from_points,
        load_sheet_calibration_or_reason,
        pdf_fingerprint,
        sheet_to_dict,
    )

    dev_root = tmp_path / "dev"
    dev_root.mkdir()
    dev_pdf = _make_pdf(dev_root, "north.pdf", payload=b"north chart bytes")

    anchors = [
        CalibrationPoint(code="A", lat=32.0, lon=34.0, u=0.10, v=0.10),
        CalibrationPoint(code="B", lat=32.5, lon=34.5, u=0.50, v=0.50),
        CalibrationPoint(code="C", lat=32.2, lon=34.8, u=0.80, v=0.20),
        CalibrationPoint(code="D", lat=31.9, lon=34.2, u=0.20, v=0.80),
    ]
    map_layout = {"x": 0.0, "y": 0.0, "scale": 1.0}
    cal = calibration_from_points(
        pdf_fingerprint(dev_pdf), *anchors, map_layout=map_layout
    )
    raw = {"north": sheet_to_dict(cal)}

    friend_root = tmp_path / "C_Users_Friend_Documents_release"
    friend_root.mkdir()
    friend_subdir = friend_root / "map-pdfs"
    friend_subdir.mkdir()
    friend_pdf = friend_subdir / "north.pdf"
    shutil.copy2(dev_pdf, friend_pdf)

    out, err = load_sheet_calibration_or_reason(
        raw, "north", friend_pdf, map_layout, "North"
    )
    assert err is None, (
        f"calibration cache should hit on the friend's machine even "
        f"though the PDF lives at a different absolute path "
        f"({friend_pdf} vs the cached {dev_pdf}); got error: {err}"
    )
    assert out is not None
    assert len(out.points) == 4


def test_geo_calibration_cache_still_invalidates_when_pdf_size_changes(
    tmp_path: Path,
) -> None:
    """Defensive sanity check (mirrors the equivalent waypoint-cache
    test): dropping the path comparison must NOT weaken content
    invalidation. A north chart with materially different bytes
    (size differs) must still invalidate the calibration so the
    user re-anchors against the new chart's coordinate grid."""
    from cvfr_routemaster.geo_calibration import (
        CalibrationPoint,
        calibration_from_points,
        load_sheet_calibration_or_reason,
        pdf_fingerprint,
        sheet_to_dict,
    )

    root = tmp_path / "root"
    root.mkdir()
    pdf = _make_pdf(root, "north.pdf", payload=b"original north chart bytes")
    anchors = [
        CalibrationPoint(code="A", lat=32.0, lon=34.0, u=0.10, v=0.10),
        CalibrationPoint(code="B", lat=32.5, lon=34.5, u=0.50, v=0.50),
        CalibrationPoint(code="C", lat=32.2, lon=34.8, u=0.80, v=0.20),
        CalibrationPoint(code="D", lat=31.9, lon=34.2, u=0.20, v=0.80),
    ]
    map_layout = {"x": 0.0, "y": 0.0, "scale": 1.0}
    cal = calibration_from_points(pdf_fingerprint(pdf), *anchors, map_layout=map_layout)
    raw = {"north": sheet_to_dict(cal)}

    pdf.write_bytes(b"completely different north chart bytes with another size")

    out, err = load_sheet_calibration_or_reason(raw, "north", pdf, map_layout, "North")
    assert out is None
    assert err is not None and "changed" in err.lower()


# ---------------------------------------------------------------------------
# Frozen-mode project-root resolver
# ---------------------------------------------------------------------------


def test_project_root_uses_repo_root_in_dev() -> None:
    """In a normal source checkout (no PyInstaller in play) the resolver
    must point at the repo root — that's where the tests, the README,
    and the dev workflow assume the PDFs and ``.cvfr_routemaster/``
    live."""
    from cvfr_routemaster.__main__ import _project_root

    root = _project_root()
    # Fingerprint of "this is the repo root": both sentinel paths exist.
    assert (root / "cvfr_routemaster" / "__main__.py").is_file()
    assert (root / "ROADMAP.md").is_file()


def test_project_root_uses_executable_dir_when_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When PyInstaller sets ``sys.frozen = True``, the resolver must
    return ``Path(sys.executable).parent`` — the directory holding
    the .exe, where ``scripts/build_release.py`` drops the PDFs and
    seed cache. Returning anything else (in particular
    ``sys._MEIPASS``, the PyInstaller temp extraction dir) would
    leave the app looking for PDFs inside a temp folder that gets
    wiped on shutdown — the friend's app would silently never find
    the chart data."""
    from cvfr_routemaster.__main__ import _project_root

    fake_exe_dir = tmp_path / "PortableApp"
    fake_exe_dir.mkdir()
    fake_exe = fake_exe_dir / "cvfr-routemaster.exe"
    fake_exe.write_bytes(b"")  # contents irrelevant; we only need the path

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    root = _project_root()
    assert root == fake_exe_dir.resolve(), (
        f"frozen-mode root should be the .exe's parent dir; got {root}"
    )


# ---------------------------------------------------------------------------
# Tesseract bundled-install lookup (release ``tesseract/`` AND dev
# ``vendor/tesseract/`` layouts)
# ---------------------------------------------------------------------------


def _make_tesseract_install(root: Path, subdir: tuple[str, ...]) -> Path:
    """Create a fake Tesseract install tree at ``root/<subdir>/`` with
    a placeholder ``tesseract.exe`` and a ``tessdata/`` folder holding
    eng + heb traineddata stubs. Returns the install directory."""
    base = root.joinpath(*subdir)
    base.mkdir(parents=True)
    (base / "tesseract.exe").write_bytes(b"fake-tesseract-binary")
    td = base / "tessdata"
    td.mkdir()
    (td / "eng.traineddata").write_bytes(b"eng-stub")
    (td / "heb.traineddata").write_bytes(b"heb-stub")
    return base


def test_tesseract_lookup_finds_install_under_release_layout(tmp_path: Path) -> None:
    """The release zip places Tesseract at ``<release>/tesseract/`` —
    the clean, friend-facing layout that ``scripts/build_release.py``
    ships. The lookup helpers must find ``tesseract.exe`` and the
    ``tessdata/`` folder there without needing the legacy ``vendor/``
    prefix."""
    from cvfr_routemaster.tesseract_runtime import (
        bundled_tessdata_dir,
        bundled_tesseract_exe,
    )

    base = _make_tesseract_install(tmp_path, ("tesseract",))

    exe = bundled_tesseract_exe(tmp_path)
    assert exe == base / "tesseract.exe"

    td = bundled_tessdata_dir(tmp_path)
    assert td == (base / "tessdata").resolve()


def test_tesseract_lookup_finds_install_under_dev_layout(tmp_path: Path) -> None:
    """The dev tree places Tesseract at ``<repo>/vendor/tesseract/`` —
    the layout ``scripts/fetch_vendor_tesseract.py`` populates. The
    lookup helpers must continue to resolve there for unchanged dev
    workflows."""
    from cvfr_routemaster.tesseract_runtime import (
        bundled_tessdata_dir,
        bundled_tesseract_exe,
    )

    base = _make_tesseract_install(tmp_path, ("vendor", "tesseract"))

    exe = bundled_tesseract_exe(tmp_path)
    assert exe == base / "tesseract.exe"

    td = bundled_tessdata_dir(tmp_path)
    assert td == (base / "tessdata").resolve()


def test_tesseract_lookup_prefers_release_layout_over_dev_layout(tmp_path: Path) -> None:
    """If both layouts coexist (an unusual case — someone unzipped a
    release on top of a dev tree, or copy-pasted both subfolders into
    the same parent during testing), the release layout wins. The
    release tesseract/ subset is the one the user explicitly chose to
    ship, so honour that intent rather than silently falling back to
    the heavier dev install."""
    from cvfr_routemaster.tesseract_runtime import bundled_tesseract_exe

    release_base = _make_tesseract_install(tmp_path, ("tesseract",))
    _make_tesseract_install(tmp_path, ("vendor", "tesseract"))

    exe = bundled_tesseract_exe(tmp_path)
    assert exe == release_base / "tesseract.exe", (
        "When both <root>/tesseract/ and <root>/vendor/tesseract/ "
        "exist, the lookup must prefer the clean release layout."
    )


def test_tesseract_lookup_returns_none_when_neither_layout_present(tmp_path: Path) -> None:
    """Empty root → both helpers return ``None`` so ``back_page_ocr``
    can fall back to system ``tesseract`` on PATH."""
    from cvfr_routemaster.tesseract_runtime import (
        bundled_tessdata_dir,
        bundled_tesseract_exe,
    )

    assert bundled_tesseract_exe(tmp_path) is None
    assert bundled_tessdata_dir(tmp_path) is None


# ---------------------------------------------------------------------------
# PDF auto-discovery — release ``map-pdfs/`` and dev project root
# ---------------------------------------------------------------------------


def _create_chart_pdfs(folder: Path) -> None:
    """Drop placeholder files with the canonical CVFR chart names so
    ``load_pdf_paths`` can discover them.

    The bytes are *minimal but non-empty* (a stub PDF header) because
    ``_autodiscover_pdf`` requires ``size > 0`` — that's the guard
    that prevents stale 0-byte fixtures from masquerading as valid
    charts (see
    ``test_load_pdf_paths_falls_back_to_autodiscovery_when_qsetting_path_is_empty_file``
    for the regression that motivated the size check).
    """
    folder.mkdir(parents=True, exist_ok=True)
    for name in (
        "CVFR-NORTH-OCT-2025-UPD2.pdf",
        "CVFR-SOUTH-OCT-2025-UPD2.pdf",
        "CVFR-BACK-PAGES-OCT-2025-UPD2.pdf",
    ):
        (folder / name).write_bytes(b"%PDF-1.4 stub\n")


def _isolate_qsettings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect ``settings_store._settings()`` to a per-test isolated
    INI file so we don't pollute or read the user's real CVFR
    settings store.

    Why monkeypatch the helper rather than ``QSettings.setPath``?
    Even though v3.3+ production code already uses
    ``QSettings.Format.IniFormat`` with an explicit project-root
    path (so there's nothing platform-native left to redirect),
    stubbing ``_settings`` keeps the test's isolation contract
    one-line and trivial: every code path inside
    ``settings_store`` reads/writes the test's tmp INI regardless
    of which path-resolution logic ``_settings()`` grows in the
    future. It also bypasses the legacy-native-store migration
    helper, which is the right default for tests that don't want
    to exercise that path.
    """
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QSettings

    from cvfr_routemaster import settings_store

    ini_path = tmp_path / "isolated_qsettings.ini"

    def _isolated_settings() -> QSettings:
        # Construct fresh each call to mirror the real helper's contract
        # (callers may call .setValue / .sync between calls and expect
        # to see those writes on the next read). Same INI file across
        # calls = same persistence semantics as the real registry.
        return QSettings(str(ini_path), QSettings.Format.IniFormat)

    monkeypatch.setattr(settings_store, "_settings", _isolated_settings)


def test_load_pdf_paths_autodiscovers_charts_in_map_pdfs_subfolder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release layout places the three chart PDFs under
    ``<release>/map-pdfs/``. With QSettings empty (the friend's first
    launch on a fresh machine) the auto-discovery must find them
    there."""
    _isolate_qsettings(monkeypatch, tmp_path)
    from cvfr_routemaster.settings_store import load_pdf_paths

    project_root = tmp_path / "release"
    project_root.mkdir()
    pdf_dir = project_root / "map-pdfs"
    _create_chart_pdfs(pdf_dir)

    north, south, back = load_pdf_paths(project_root)
    assert Path(north) == pdf_dir / "CVFR-NORTH-OCT-2025-UPD2.pdf"
    assert Path(south) == pdf_dir / "CVFR-SOUTH-OCT-2025-UPD2.pdf"
    assert Path(back) == pdf_dir / "CVFR-BACK-PAGES-OCT-2025-UPD2.pdf"


def test_load_pdf_paths_still_autodiscovers_charts_in_project_root_for_dev_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dev tree puts PDFs at the repo root (no ``map-pdfs/``
    subfolder). Auto-discovery must continue to work there for an
    unchanged dev workflow."""
    _isolate_qsettings(monkeypatch, tmp_path)
    from cvfr_routemaster.settings_store import load_pdf_paths

    project_root = tmp_path / "repo"
    _create_chart_pdfs(project_root)

    north, south, back = load_pdf_paths(project_root)
    assert Path(north) == project_root / "CVFR-NORTH-OCT-2025-UPD2.pdf"
    assert Path(south) == project_root / "CVFR-SOUTH-OCT-2025-UPD2.pdf"
    assert Path(back) == project_root / "CVFR-BACK-PAGES-OCT-2025-UPD2.pdf"


def test_load_pdf_paths_prefers_map_pdfs_subfolder_over_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a chart name resolves under both ``<root>/map-pdfs/`` and
    ``<root>/`` (the case where someone unzipped the release tree on
    top of an existing dev checkout), the release layout wins. This
    is the same priority order the Tesseract lookup uses, for the
    same reason: the release subfolder represents the user's
    explicit intent."""
    _isolate_qsettings(monkeypatch, tmp_path)
    from cvfr_routemaster.settings_store import load_pdf_paths

    project_root = tmp_path / "mixed"
    project_root.mkdir()
    _create_chart_pdfs(project_root)
    pdf_dir = project_root / "map-pdfs"
    _create_chart_pdfs(pdf_dir)

    north, _, _ = load_pdf_paths(project_root)
    assert Path(north) == pdf_dir / "CVFR-NORTH-OCT-2025-UPD2.pdf"


def test_load_pdf_paths_falls_back_to_autodiscovery_when_qsetting_path_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Realistic upgrade case: the user's QSettings still points at
    a stale absolute path (e.g. an old dev-machine location like
    ``<old-dev-root>\\CVFR-NORTH-...``) after they restructured the
    on-disk layout to use ``map-pdfs/``. When the stale absolute path no longer exists,
    auto-discovery must kick in and resolve through the new layout
    so the user doesn't get a "PDF not found" error and have to
    manually re-pick all three files in Settings.

    This is also the friend's situation when their QSettings was
    populated by a prior different release that pointed at
    a now-missing path.
    """
    _isolate_qsettings(monkeypatch, tmp_path)
    from cvfr_routemaster import settings_store
    from cvfr_routemaster.settings_store import load_pdf_paths

    # Seed via the same isolated _settings() helper that
    # load_pdf_paths reads through, so the writes are visible to it.
    stale = tmp_path / "no-such-folder" / "missing.pdf"
    s = settings_store._settings()
    s.setValue("pdf_north", str(stale))
    s.setValue("pdf_south", str(stale))
    s.setValue("pdf_back", str(stale))
    s.sync()

    project_root = tmp_path / "release"
    project_root.mkdir()
    pdf_dir = project_root / "map-pdfs"
    _create_chart_pdfs(pdf_dir)

    north, south, back = load_pdf_paths(project_root)
    assert Path(north).is_file() and Path(north).parent == pdf_dir
    assert Path(south).is_file() and Path(south).parent == pdf_dir
    assert Path(back).is_file() and Path(back).parent == pdf_dir


def test_load_pdf_paths_falls_back_to_autodiscovery_when_qsetting_path_is_empty_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for a real-world bug: a stray test fixture wrote
    ``pdf_north = <pytest tmp 0-byte file>`` to the user's QSettings
    during a session where test isolation was broken. On the next
    .exe launch, ``Path(...).is_file()`` was True (the tmp file
    still existed) so the stale path won, and PyMuPDF crashed with
    "Cannot open empty file" inside the Map-load worker.

    The fix is to require ``size > 0`` in addition to ``is_file()``
    when accepting a QSettings PDF path; an empty file is never a
    valid CVFR chart (real charts are megabytes).

    Same mechanism also catches a truncated download / disk-full
    mid-copy that leaves a chart at 0 bytes.
    """
    _isolate_qsettings(monkeypatch, tmp_path)
    from cvfr_routemaster import settings_store
    from cvfr_routemaster.settings_store import load_pdf_paths

    empty_north = tmp_path / "stale" / "CVFR-NORTH-OCT-2025-UPD2.pdf"
    empty_north.parent.mkdir()
    empty_north.write_bytes(b"")
    assert empty_north.is_file() and empty_north.stat().st_size == 0

    s = settings_store._settings()
    s.setValue("pdf_north", str(empty_north))
    s.sync()

    project_root = tmp_path / "release"
    project_root.mkdir()
    pdf_dir = project_root / "map-pdfs"
    _create_chart_pdfs(pdf_dir)
    # Make the autodiscovery target non-empty so it's actually
    # accepted (the helper that creates the fixture writes 0-byte
    # files; we need a usable one for this test specifically).
    (pdf_dir / "CVFR-NORTH-OCT-2025-UPD2.pdf").write_bytes(b"%PDF-1.4 minimal\n")

    north, _, _ = load_pdf_paths(project_root)
    assert Path(north).parent == pdf_dir, (
        "An empty QSettings PDF path should be ignored and "
        "autodiscovery should resolve through map-pdfs/."
    )


def test_load_pdf_paths_keeps_qsetting_path_when_it_still_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive sanity check on the stale-path fallback: if the user
    *did* explicitly pick a custom PDF path in Settings… and that
    file is still there, we must NOT silently override it with an
    auto-discovery hit. Custom paths exist precisely because some
    users want to point at PDFs in unusual locations."""
    _isolate_qsettings(monkeypatch, tmp_path)
    from cvfr_routemaster import settings_store
    from cvfr_routemaster.settings_store import load_pdf_paths

    custom_dir = tmp_path / "custom-charts"
    _create_chart_pdfs(custom_dir)
    custom_north = custom_dir / "CVFR-NORTH-OCT-2025-UPD2.pdf"

    # Same-helper write so the value is visible to load_pdf_paths' read.
    s = settings_store._settings()
    s.setValue("pdf_north", str(custom_north))
    s.sync()

    # Auto-discovery would otherwise resolve north to the project_root copy:
    project_root = tmp_path / "release"
    project_root.mkdir()
    _create_chart_pdfs(project_root / "map-pdfs")

    north, _, _ = load_pdf_paths(project_root)
    assert Path(north) == custom_north, (
        "When the QSettings PDF path still resolves to an existing "
        "file, auto-discovery must defer to the user's explicit choice."
    )


# ---------------------------------------------------------------------------
# Slim-Tesseract bundling (build-script behaviour)
# ---------------------------------------------------------------------------


def _make_full_tesseract_install(root: Path) -> Path:
    """Synthesise a UB-Mannheim-shaped ``vendor/tesseract/`` tree —
    just the file *names* that drive ``_copy_slim_tesseract``'s
    keep/drop decisions, with deliberately-tiny content so the test
    runs fast. The slim-copy logic only reads names + sizes, never
    PE headers or PDF/font internals.
    """
    base = root / "vendor" / "tesseract"
    base.mkdir(parents=True)

    # Always-keep payload.
    (base / "tesseract.exe").write_bytes(b"x" * 1500)
    for dll in (
        "libtesseract-5.dll", "libleptonica-6.dll", "libicudt75.dll",
        "libpng16-16.dll", "libjpeg-8.dll", "libtiff-6.dll",
    ):
        (base / dll).write_bytes(b"x" * 100)

    # Always-drop payload.
    for trainer in (
        "lstmtraining.exe", "text2image.exe", "cntraining.exe",
        "tesseract-uninstall.exe", "winpath.exe",
    ):
        (base / trainer).write_bytes(b"x" * 9000)
    for doc in ("tesseract.1.html", "lstmtraining.1.html"):
        (base / doc).write_bytes(b"x" * 50)
    docdir = base / "doc"
    docdir.mkdir()
    (docdir / "INSTALL.html").write_bytes(b"x" * 50)

    # Tessdata: allowlist eng + heb, drop everything else.
    td = base / "tessdata"
    td.mkdir()
    (td / "eng.traineddata").write_bytes(b"e" * 4000)
    (td / "heb.traineddata").write_bytes(b"h" * 900)
    (td / "osd.traineddata").write_bytes(b"o" * 10000)
    (td / "pdf.ttf").write_bytes(b"f" * 100)
    (td / "ScrollView.jar").write_bytes(b"j" * 100)

    return base


def test_copy_slim_tesseract_keeps_tesseract_exe_and_dlls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release tesseract/ folder must include ``tesseract.exe``
    and every ``*.dll``. Missing any of these means the OCR engine
    fails to launch on the friend's machine — a missing DLL throws
    a system-modal "MyApp.exe - System Error" dialog before any
    Python code runs."""
    from scripts import build_release

    src_base = _make_full_tesseract_install(tmp_path)
    monkeypatch.setattr(build_release, "DEV_TESSERACT_DIR", src_base)
    monkeypatch.setattr(build_release, "RELEASE_DIR", tmp_path / "release")
    (tmp_path / "release").mkdir()

    build_release._copy_slim_tesseract()

    out_base = tmp_path / "release" / "tesseract"
    assert (out_base / "tesseract.exe").is_file()
    for dll in (
        "libtesseract-5.dll", "libleptonica-6.dll", "libicudt75.dll",
        "libpng16-16.dll", "libjpeg-8.dll", "libtiff-6.dll",
    ):
        assert (out_base / dll).is_file(), f"missing runtime DLL: {dll}"


def test_copy_slim_tesseract_drops_training_tools_and_html_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anything that isn't ``tesseract.exe`` or a ``*.dll`` must NOT
    end up in the release. This is what gets the bundle from ~239
    MiB down to ~167 MiB; if the keep/drop logic accidentally
    matches an ``.exe`` name suffix the size budget blows up."""
    from scripts import build_release

    src_base = _make_full_tesseract_install(tmp_path)
    monkeypatch.setattr(build_release, "DEV_TESSERACT_DIR", src_base)
    monkeypatch.setattr(build_release, "RELEASE_DIR", tmp_path / "release")
    (tmp_path / "release").mkdir()

    build_release._copy_slim_tesseract()

    out_base = tmp_path / "release" / "tesseract"
    for dropped in (
        "lstmtraining.exe", "text2image.exe", "cntraining.exe",
        "tesseract-uninstall.exe", "winpath.exe",
        "tesseract.1.html", "lstmtraining.1.html",
    ):
        assert not (out_base / dropped).exists(), f"should not have shipped: {dropped}"
    assert not (out_base / "doc").exists(), "doc/ HTML man-page tree must be dropped"


def test_copy_slim_tesseract_keeps_eng_and_heb_traineddata_and_drops_others(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tessdata is an allowlist: only eng + heb. ``osd.traineddata``
    is the big one to keep out (10 MiB orientation/script detection,
    not used by us); the ``*.jar`` Java GUI tools and ``pdf.ttf``
    PDF-output font are also dropped."""
    from scripts import build_release

    src_base = _make_full_tesseract_install(tmp_path)
    monkeypatch.setattr(build_release, "DEV_TESSERACT_DIR", src_base)
    monkeypatch.setattr(build_release, "RELEASE_DIR", tmp_path / "release")
    (tmp_path / "release").mkdir()

    build_release._copy_slim_tesseract()

    out_td = tmp_path / "release" / "tesseract" / "tessdata"
    assert (out_td / "eng.traineddata").is_file()
    assert (out_td / "heb.traineddata").is_file()
    assert not (out_td / "osd.traineddata").exists()
    assert not (out_td / "pdf.ttf").exists()
    assert not (out_td / "ScrollView.jar").exists()


def test_copy_slim_tesseract_preserves_mtime_for_runtime_byte_compare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shutil.copy2`` (not ``shutil.copy``) is used so file mtimes
    survive the copy. We don't currently key any cache off the
    Tesseract install but several future things might (e.g. an
    "is the bundled tesseract up to date with the dev one?" check),
    and ``copy`` vs ``copy2`` is a one-character footgun worth
    pinning."""
    from scripts import build_release

    src_base = _make_full_tesseract_install(tmp_path)
    monkeypatch.setattr(build_release, "DEV_TESSERACT_DIR", src_base)
    monkeypatch.setattr(build_release, "RELEASE_DIR", tmp_path / "release")
    (tmp_path / "release").mkdir()

    build_release._copy_slim_tesseract()

    out_exe = tmp_path / "release" / "tesseract" / "tesseract.exe"
    assert out_exe.stat().st_mtime_ns == (src_base / "tesseract.exe").stat().st_mtime_ns
