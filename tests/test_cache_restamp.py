"""Tests for :mod:`cvfr_routemaster.cache_restamp`.

The runtime restamp closes the loop on the v3.3+ chart-download
flow: after a successful download, the shipped cache JSON
fingerprints need to be adjusted so the existing
``(mtime_ns, size)`` validity check passes against the
just-downloaded file's stat values.

Without restamping, every first launch would invalidate every
cache and trigger the re-render / re-OCR / re-calibrate cascade
the build pipeline's seed-cache exists specifically to avoid.
These tests pin the contract that the restamp is correctly
scoped (right cache files, right JSON sub-paths) and idempotent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cvfr_routemaster.cache_restamp import (
    SHEET_FINGERPRINT_BINDINGS,
    SheetRestampReport,
    restamp_sheet_fingerprints,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_pdf(path: Path, *, size: int = 1024) -> None:
    """Write a small but valid-looking PDF placeholder. The cache
    machinery never opens the file we write here — only stats it
    — so we don't need a real PDF, just an on-disk file of the
    expected approximate size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n" + b"x" * (size - 14) + b"\n%%EOF\n")


def _write_cache(
    cache_dir: Path, filename: str, content: dict
) -> Path:
    """Write a JSON cache file with the given content. Returns the
    written path so tests can re-read after a restamp pass."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / filename
    target.write_text(json.dumps(content, indent=2), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Binding-table sanity checks
# ---------------------------------------------------------------------------


def test_sheet_fingerprint_bindings_cover_every_sheet_key() -> None:
    """Every sheet identity defined in ``chart_source.SHEET_KEYS``
    must have at least one binding here — otherwise a downloaded
    sheet would never get its fingerprints restamped, and the
    cache would silently invalidate on first launch."""
    from cvfr_routemaster.chart_source import SHEET_KEYS

    for key in SHEET_KEYS:
        assert key in SHEET_FINGERPRINT_BINDINGS, (
            f"Missing fingerprint binding for sheet {key!r}"
        )
        assert SHEET_FINGERPRINT_BINDINGS[key], (
            f"Empty binding list for sheet {key!r}"
        )


def test_sheet_fingerprint_bindings_match_known_cache_files() -> None:
    """Pin the cache filenames the bindings reference. If a future
    refactor renames one of the cache JSONs (e.g.
    ``waypoints_cache.json`` → ``waypoint_cache_v2.json``) the
    restamp would silently no-op without this test catching it."""
    referenced = {
        cache_file
        for bindings in SHEET_FINGERPRINT_BINDINGS.values()
        for cache_file, _ in bindings
    }
    assert referenced == {
        "geo_calibration.json",
        "altitude_arrows_north.json",
        "altitude_arrows_south.json",
        "map_images_meta.json",
        "waypoints_cache.json",
    }


# ---------------------------------------------------------------------------
# Restamp behaviour — single sheet
# ---------------------------------------------------------------------------


def test_restamp_updates_geo_calibration_north(tmp_path: Path) -> None:
    """The most user-visible cache: ``geo_calibration.json``. A
    stale fingerprint here means the user gets prompted to
    re-pick 8 anchor waypoints on first launch — disastrous UX.
    Pin that restamping wires the new mtime into the right
    sub-block."""
    cache_dir = tmp_path / ".cvfr_routemaster"
    pdf_path = cache_dir / "charts" / "cvfr_north.pdf"
    _write_pdf(pdf_path)

    _write_cache(
        cache_dir,
        "geo_calibration.json",
        {
            "north": {
                "pdf": {
                    "path": "C:/old/dev/CVFR-NORTH.pdf",
                    "mtime_ns": 1000,
                    "size": 99,
                },
                "anchors": [],
            },
            "south": {
                "pdf": {
                    "path": "C:/old/dev/CVFR-SOUTH.pdf",
                    "mtime_ns": 2000,
                    "size": 88,
                },
                "anchors": [],
            },
        },
    )

    report = restamp_sheet_fingerprints(tmp_path, "north", pdf_path)
    assert isinstance(report, SheetRestampReport)
    # The north block must be updated; south must NOT be touched.
    reloaded = json.loads(
        (cache_dir / "geo_calibration.json").read_text(encoding="utf-8")
    )
    new_stat = pdf_path.stat()
    assert reloaded["north"]["pdf"]["mtime_ns"] == new_stat.st_mtime_ns
    assert reloaded["north"]["pdf"]["size"] == new_stat.st_size
    assert reloaded["north"]["pdf"]["path"] == str(pdf_path)
    # South untouched:
    assert reloaded["south"]["pdf"]["mtime_ns"] == 2000


def test_restamp_updates_waypoints_cache_for_back_sheet(tmp_path: Path) -> None:
    """Back sheet is the OCR source. A stale fingerprint here
    means the user re-OCRs on first launch (which needs Tesseract
    installed — not always true on a fresh Linux desktop). Pin
    the back-sheet restamp."""
    cache_dir = tmp_path / ".cvfr_routemaster"
    pdf_path = cache_dir / "charts" / "cvfr_back.pdf"
    _write_pdf(pdf_path)

    _write_cache(
        cache_dir,
        "waypoints_cache.json",
        {
            "back_pdf": {
                "path": "/old/dev/back.pdf",
                "mtime_ns": 1000,
                "size": 99,
            },
            "records": [],
        },
    )

    restamp_sheet_fingerprints(tmp_path, "back", pdf_path)
    reloaded = json.loads(
        (cache_dir / "waypoints_cache.json").read_text(encoding="utf-8")
    )
    new_stat = pdf_path.stat()
    assert reloaded["back_pdf"]["mtime_ns"] == new_stat.st_mtime_ns
    assert reloaded["back_pdf"]["size"] == new_stat.st_size


def test_restamp_updates_every_binding_for_north(tmp_path: Path) -> None:
    """North downloads must restamp ALL three north-binding cache
    files: geo_calibration, altitude_arrows_north, map_images_meta."""
    cache_dir = tmp_path / ".cvfr_routemaster"
    pdf_path = cache_dir / "charts" / "cvfr_north.pdf"
    _write_pdf(pdf_path)

    _write_cache(
        cache_dir,
        "geo_calibration.json",
        {"north": {"pdf": {"mtime_ns": 1, "size": 1, "path": "old"}}},
    )
    _write_cache(
        cache_dir,
        "altitude_arrows_north.json",
        {"pdf": {"mtime_ns": 1, "size": 1, "path": "old"}, "arrows": []},
    )
    _write_cache(
        cache_dir,
        "map_images_meta.json",
        {"north_pdf": {"mtime_ns": 1, "size": 1, "path": "old"}},
    )

    report = restamp_sheet_fingerprints(tmp_path, "north", pdf_path)
    assert len(report.updates) == 3, (
        f"all three north bindings must update; got {report.updates}"
    )
    # Every cache file must read back with the new stat.
    new_stat = pdf_path.stat()
    for cache_filename, json_path in SHEET_FINGERPRINT_BINDINGS["north"]:
        reloaded = json.loads(
            (cache_dir / cache_filename).read_text(encoding="utf-8")
        )
        # Walk to the fingerprint block.
        node = reloaded
        for seg in json_path:
            node = node[seg]
        assert node["mtime_ns"] == new_stat.st_mtime_ns, (
            f"{cache_filename}@{json_path} not restamped"
        )


# ---------------------------------------------------------------------------
# Skipping behaviour
# ---------------------------------------------------------------------------


def test_restamp_silently_skips_missing_cache_files(tmp_path: Path) -> None:
    """Optional caches (e.g. ``altitude_arrows_south.json`` when
    the dev only calibrated north) are commonly absent on a fresh
    install. Restamp must NOT raise — it should report them as
    ``skipped`` so a debugger can see the no-op was intentional."""
    cache_dir = tmp_path / ".cvfr_routemaster"
    pdf_path = cache_dir / "charts" / "cvfr_north.pdf"
    _write_pdf(pdf_path)
    # No cache files at all — every binding should land in ``skipped``.

    report = restamp_sheet_fingerprints(tmp_path, "north", pdf_path)
    assert report.updates == []
    expected_count = len(SHEET_FINGERPRINT_BINDINGS["north"])
    assert len(report.skipped) == expected_count


def test_restamp_silently_skips_corrupt_cache_json(tmp_path: Path) -> None:
    """A corrupt cache JSON must NOT block the program. Skip and
    move on; the cache module's own load path will detect the
    corruption later and reject the cache on its own terms."""
    cache_dir = tmp_path / ".cvfr_routemaster"
    pdf_path = cache_dir / "charts" / "cvfr_north.pdf"
    _write_pdf(pdf_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "geo_calibration.json").write_text(
        "{not valid json", encoding="utf-8"
    )

    report = restamp_sheet_fingerprints(tmp_path, "north", pdf_path)
    assert "geo_calibration.json" in report.skipped


def test_restamp_silently_skips_when_field_path_missing(tmp_path: Path) -> None:
    """A cache JSON whose schema doesn't carry the expected
    field path (e.g. ``geo_calibration.json`` with only a south
    block, no north) must NOT crash. The binding for north is
    silently dropped."""
    cache_dir = tmp_path / ".cvfr_routemaster"
    pdf_path = cache_dir / "charts" / "cvfr_north.pdf"
    _write_pdf(pdf_path)

    _write_cache(
        cache_dir,
        "geo_calibration.json",
        {"south": {"pdf": {"mtime_ns": 1, "size": 1, "path": "old"}}},
    )

    report = restamp_sheet_fingerprints(tmp_path, "north", pdf_path)
    # geo_calibration was present but missing the north sub-block
    # → skipped, not crashed.
    assert "geo_calibration.json" in report.skipped


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_restamp_is_idempotent(tmp_path: Path) -> None:
    """Calling restamp twice in a row with no PDF mutation must
    produce identical on-disk state. This guarantees a launch
    that successfully restamps and then crashes mid-load doesn't
    leave the cache in a different shape than a clean second
    launch."""
    cache_dir = tmp_path / ".cvfr_routemaster"
    pdf_path = cache_dir / "charts" / "cvfr_north.pdf"
    _write_pdf(pdf_path)

    _write_cache(
        cache_dir,
        "geo_calibration.json",
        {"north": {"pdf": {"mtime_ns": 1, "size": 1, "path": "old"}}},
    )

    restamp_sheet_fingerprints(tmp_path, "north", pdf_path)
    after_first = (cache_dir / "geo_calibration.json").read_text(encoding="utf-8")
    restamp_sheet_fingerprints(tmp_path, "north", pdf_path)
    after_second = (cache_dir / "geo_calibration.json").read_text(encoding="utf-8")
    assert after_first == after_second


def test_restamp_raises_on_missing_pdf(tmp_path: Path) -> None:
    """Caller bug protection: if the PDF the resolver claims to
    have downloaded isn't on disk, restamp should NOT silently
    succeed (it would write zero/garbage mtime/size into the
    cache and the next launch would re-render anyway). Raise
    loudly so the calling code's bug becomes visible
    immediately."""
    with pytest.raises(FileNotFoundError):
        restamp_sheet_fingerprints(
            tmp_path, "north", tmp_path / "nope.pdf"
        )


def test_restamp_raises_on_unknown_sheet(tmp_path: Path) -> None:
    """Defensive: an unknown sheet identity is a programming bug,
    not a recoverable runtime condition."""
    pdf_path = tmp_path / "x.pdf"
    _write_pdf(pdf_path)
    with pytest.raises(ValueError, match="unknown sheet_key"):
        restamp_sheet_fingerprints(tmp_path, "east", pdf_path)


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def test_restamp_atomic_rename_leaves_no_tmp_files(tmp_path: Path) -> None:
    """The restamp writes via ``<file>.tmp`` then rename. A
    successful pass must clean up the tmp sentinels — otherwise
    a future inspector seeing them would suspect a crashed
    write."""
    cache_dir = tmp_path / ".cvfr_routemaster"
    pdf_path = cache_dir / "charts" / "cvfr_north.pdf"
    _write_pdf(pdf_path)
    _write_cache(
        cache_dir,
        "geo_calibration.json",
        {"north": {"pdf": {"mtime_ns": 1, "size": 1, "path": "old"}}},
    )

    restamp_sheet_fingerprints(tmp_path, "north", pdf_path)
    tmps = list(cache_dir.glob("*.tmp"))
    assert tmps == [], f"leftover tmp files: {tmps}"


# ---------------------------------------------------------------------------
# Report structure
# ---------------------------------------------------------------------------


def test_restamp_report_records_old_and_new_values(tmp_path: Path) -> None:
    """The report should carry the actual drift values so a
    debugger / log message can show what changed. Without the
    old/new pair, diagnosing "why was this cache invalid?" is
    just guessing."""
    cache_dir = tmp_path / ".cvfr_routemaster"
    pdf_path = cache_dir / "charts" / "cvfr_north.pdf"
    _write_pdf(pdf_path)

    _write_cache(
        cache_dir,
        "geo_calibration.json",
        {"north": {"pdf": {"mtime_ns": 12345, "size": 67, "path": "old"}}},
    )

    report = restamp_sheet_fingerprints(tmp_path, "north", pdf_path)
    geo_updates = [
        u for u in report.updates if u.cache_file == "geo_calibration.json"
    ]
    assert len(geo_updates) == 1
    u = geo_updates[0]
    assert u.field_path == "north.pdf"
    assert u.old_mtime_ns == 12345
    assert u.old_size == 67
    new_stat = pdf_path.stat()
    assert u.new_mtime_ns == new_stat.st_mtime_ns
    assert u.new_size == new_stat.st_size
