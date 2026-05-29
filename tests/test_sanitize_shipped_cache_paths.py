"""Tests for :mod:`scripts._sanitize_shipped_cache_paths`.

The helper strips dev-box absolute paths (``C:\\flying\\...``)
from every shipped cache JSON, leaving just the PDF basename.
These tests pin both the structural contract (which fields get
rewritten in which schemas) and the privacy contract (no
absolute-path leaks survive a sanitisation pass).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


RELEASE_CACHE_SUBDIR = ".cvfr_routemaster"


def _write_cache(root: Path, name: str, payload: dict) -> Path:
    """Write ``payload`` to ``root / .cvfr_routemaster / name`` and
    return the full path. Mirrors the layout
    ``sanitize_shipped_cache_paths`` expects so we don't have to
    duplicate the helper's directory-resolution logic in tests."""
    cache_dir = root / RELEASE_CACHE_SUBDIR
    cache_dir.mkdir(exist_ok=True, parents=True)
    cache_file = cache_dir / name
    cache_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return cache_file


def _read_cache(root: Path, name: str) -> dict:
    return json.loads(
        (root / RELEASE_CACHE_SUBDIR / name).read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# Schema-by-schema rewrite behaviour
# ---------------------------------------------------------------------------


def test_single_pdf_flat_schema_path_gets_basename(tmp_path: Path) -> None:
    """``waypoints_cache.json`` / ``altitude_arrows_*.json`` shape:
    one ``pdf`` (or ``back_pdf``) block at top level with a ``path``
    field. The path must collapse to just the filename."""
    from scripts._sanitize_shipped_cache_paths import (
        sanitize_shipped_cache_paths,
    )

    _write_cache(
        tmp_path,
        "altitude_arrows_north.json",
        {
            "format_version": 6,
            "pdf": {
                "path": "C:\\flying\\cvfr-routemaster\\CVFR-NORTH-OCT-2025-UPD2.pdf",
                "mtime_ns": 1777797121000000000,
                "size": 7910912,
            },
            "arrows": [],
        },
    )

    report = sanitize_shipped_cache_paths(tmp_path)

    assert report.total_fields_updated() == 1
    assert report.skipped == []
    cached = _read_cache(tmp_path, "altitude_arrows_north.json")
    assert cached["pdf"]["path"] == "CVFR-NORTH-OCT-2025-UPD2.pdf"
    assert cached["pdf"]["mtime_ns"] == 1777797121000000000
    assert cached["pdf"]["size"] == 7910912


def test_dual_pdf_flat_schema_both_paths_get_basename(tmp_path: Path) -> None:
    """``map_images_meta.json`` shape: ``north_pdf`` + ``south_pdf``
    flat at top level. Both paths must collapse independently."""
    from scripts._sanitize_shipped_cache_paths import (
        sanitize_shipped_cache_paths,
    )

    _write_cache(
        tmp_path,
        "map_images_meta.json",
        {
            "cache_format_version": 2,
            "north_pdf": {
                "path": "C:\\flying\\cvfr-routemaster\\CVFR-NORTH-OCT-2025-UPD2.pdf",
                "mtime_ns": 111,
                "size": 7910912,
            },
            "south_pdf": {
                "path": "C:\\flying\\cvfr-routemaster\\CVFR-SOUTH-OCT-2025-UPD2.pdf",
                "mtime_ns": 222,
                "size": 8697353,
            },
        },
    )

    report = sanitize_shipped_cache_paths(tmp_path)

    assert report.total_fields_updated() == 2
    cached = _read_cache(tmp_path, "map_images_meta.json")
    assert cached["north_pdf"]["path"] == "CVFR-NORTH-OCT-2025-UPD2.pdf"
    assert cached["south_pdf"]["path"] == "CVFR-SOUTH-OCT-2025-UPD2.pdf"


def test_dual_pdf_nested_schema_both_paths_get_basename(tmp_path: Path) -> None:
    """``geo_calibration.json`` shape: ``north.pdf`` + ``south.pdf``
    nested under per-sheet blocks. Both nested paths must collapse."""
    from scripts._sanitize_shipped_cache_paths import (
        sanitize_shipped_cache_paths,
    )

    _write_cache(
        tmp_path,
        "geo_calibration.json",
        {
            "version": 1,
            "north": {
                "pdf": {
                    "path": "C:\\flying\\cvfr-routemaster\\CVFR-NORTH-OCT-2025-UPD2.pdf",
                    "mtime_ns": 111,
                    "size": 7910912,
                },
                "points": [{"code": "HOTRM"}],
                "map_layout": {"x": 0.0, "y": 0.0, "scale": 1.0},
            },
            "south": {
                "pdf": {
                    "path": "C:\\flying\\cvfr-routemaster\\CVFR-SOUTH-OCT-2025-UPD2.pdf",
                    "mtime_ns": 222,
                    "size": 8697353,
                },
                "points": [{"code": "OMER"}],
                "map_layout": {"x": 100.0, "y": 200.0, "scale": 1.0},
            },
        },
    )

    report = sanitize_shipped_cache_paths(tmp_path)

    assert report.total_fields_updated() == 2
    cached = _read_cache(tmp_path, "geo_calibration.json")
    assert cached["north"]["pdf"]["path"] == "CVFR-NORTH-OCT-2025-UPD2.pdf"
    assert cached["south"]["pdf"]["path"] == "CVFR-SOUTH-OCT-2025-UPD2.pdf"
    # Anchor data and map layout (the actually-valuable calibration
    # state) must be preserved bit-for-bit through sanitisation —
    # exactly the same contract the restamp helper has.
    assert cached["north"]["points"] == [{"code": "HOTRM"}]
    assert cached["south"]["map_layout"] == {"x": 100.0, "y": 200.0, "scale": 1.0}


# ---------------------------------------------------------------------------
# Privacy contract
# ---------------------------------------------------------------------------


def test_posix_style_dev_paths_also_collapse(tmp_path: Path) -> None:
    """Linux-built caches use ``/``-separated absolute paths
    (e.g. when the dev runs the app inside WSL). Those must collapse
    to the basename too — not just Windows ``\\``-separated paths."""
    from scripts._sanitize_shipped_cache_paths import (
        sanitize_shipped_cache_paths,
    )

    _write_cache(
        tmp_path,
        "altitude_arrows_north.json",
        {
            "pdf": {
                "path": "/home/dev/cvfr-routemaster/CVFR-NORTH-OCT-2025-UPD2.pdf",
                "mtime_ns": 1,
                "size": 1,
            },
            "arrows": [],
        },
    )

    sanitize_shipped_cache_paths(tmp_path)

    cached = _read_cache(tmp_path, "altitude_arrows_north.json")
    assert cached["pdf"]["path"] == "CVFR-NORTH-OCT-2025-UPD2.pdf"


def test_no_absolute_paths_remain_after_sanitisation(tmp_path: Path) -> None:
    """The privacy contract: after a sanitisation pass, none of the
    shipped JSON files contain any string that looks like a dev-box
    absolute path. Pins the privacy outcome end-to-end rather than
    the per-field behaviour above."""
    from scripts._sanitize_shipped_cache_paths import (
        sanitize_shipped_cache_paths,
    )

    _write_cache(
        tmp_path,
        "waypoints_cache.json",
        {
            "back_pdf": {
                "path": "C:\\flying\\cvfr-routemaster\\CVFR-BACK-PAGES-OCT-2025-UPD2.pdf",
                "mtime_ns": 1,
                "size": 1,
            },
        },
    )
    _write_cache(
        tmp_path,
        "geo_calibration.json",
        {
            "north": {
                "pdf": {
                    "path": "C:\\flying\\cvfr-routemaster\\CVFR-NORTH-OCT-2025-UPD2.pdf",
                    "mtime_ns": 1,
                    "size": 1,
                },
            },
        },
    )

    sanitize_shipped_cache_paths(tmp_path)

    for cache_file in (tmp_path / RELEASE_CACHE_SUBDIR).glob("*.json"):
        body = cache_file.read_text(encoding="utf-8")
        # Tight check: the dev's actual repo root must not survive
        # any sanitisation pass. (We deliberately include the
        # literal verbatim string here even though it costs a
        # cleartext "C:/flying/..." in the test file — this is the
        # exact pattern we don't want shipped, and pinning the
        # negation in a test source is the cheapest way to keep
        # someone from accidentally re-introducing the leak.)
        assert "C:\\flying\\cvfr-routemaster" not in body, (
            f"Dev path leaked through sanitisation in {cache_file.name}: "
            f"{body[:200]}..."
        )
        assert "C:/flying/cvfr-routemaster" not in body


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_idempotent_re_running_is_a_noop(tmp_path: Path) -> None:
    """Calling ``sanitize_shipped_cache_paths`` twice must produce
    the same end state as calling it once. Matters because the
    helper is invoked from a build pipeline that can be re-run
    against an already-shipped tree (manual smoke-test workflow,
    or an interrupted-then-resumed build)."""
    from scripts._sanitize_shipped_cache_paths import (
        sanitize_shipped_cache_paths,
    )

    _write_cache(
        tmp_path,
        "altitude_arrows_north.json",
        {
            "pdf": {
                "path": "C:\\flying\\cvfr-routemaster\\CVFR-NORTH-OCT-2025-UPD2.pdf",
                "mtime_ns": 1,
                "size": 1,
            },
            "arrows": [],
        },
    )

    first = sanitize_shipped_cache_paths(tmp_path)
    second = sanitize_shipped_cache_paths(tmp_path)

    assert first.total_fields_updated() == 1
    assert second.total_fields_updated() == 0
    cached = _read_cache(tmp_path, "altitude_arrows_north.json")
    assert cached["pdf"]["path"] == "CVFR-NORTH-OCT-2025-UPD2.pdf"


def test_already_clean_basename_path_is_left_alone(tmp_path: Path) -> None:
    """If a cache was generated by a future app version that already
    writes basenames (or by an earlier sanitisation pass), the
    helper must not "rewrite" the field to itself — there's nothing
    to fix, and treating it as a rewrite would inflate the build's
    'fields sanitised' summary line."""
    from scripts._sanitize_shipped_cache_paths import (
        sanitize_shipped_cache_paths,
    )

    _write_cache(
        tmp_path,
        "altitude_arrows_south.json",
        {
            "pdf": {
                "path": "CVFR-SOUTH-OCT-2025-UPD2.pdf",
                "mtime_ns": 1,
                "size": 1,
            },
            "arrows": [],
        },
    )

    report = sanitize_shipped_cache_paths(tmp_path)

    assert report.total_fields_updated() == 0
    assert report.updates == {}


def test_missing_cache_dir_returns_empty_report(tmp_path: Path) -> None:
    """A release tree without ``.cvfr_routemaster/`` (e.g. someone
    asked the helper to process the wrong folder) must return an
    empty report instead of raising — same defensive contract as
    ``restamp_cache_fingerprints``."""
    from scripts._sanitize_shipped_cache_paths import (
        sanitize_shipped_cache_paths,
    )

    report = sanitize_shipped_cache_paths(tmp_path)

    assert report.updates == {}
    assert report.skipped == []
    assert report.total_fields_updated() == 0


def test_malformed_json_is_skipped_not_crashed(tmp_path: Path) -> None:
    """A malformed cache JSON in the shipped tree must be recorded
    in ``skipped`` rather than crashing the whole sanitisation
    pass. The rest of the cache files in the same release tree
    must still get processed correctly — partial failure must not
    block the build."""
    from scripts._sanitize_shipped_cache_paths import (
        sanitize_shipped_cache_paths,
    )

    (tmp_path / RELEASE_CACHE_SUBDIR).mkdir()
    (tmp_path / RELEASE_CACHE_SUBDIR / "altitude_arrows_north.json").write_text(
        "this is not json", encoding="utf-8"
    )
    _write_cache(
        tmp_path,
        "altitude_arrows_south.json",
        {
            "pdf": {
                "path": "C:\\flying\\cvfr-routemaster\\CVFR-SOUTH-OCT-2025-UPD2.pdf",
                "mtime_ns": 1,
                "size": 1,
            },
            "arrows": [],
        },
    )

    report = sanitize_shipped_cache_paths(tmp_path)

    assert "altitude_arrows_north.json" in report.skipped
    assert "altitude_arrows_south.json" in report.updates
    assert report.total_fields_updated() == 1


def test_fingerprint_block_without_mtime_ns_is_ignored(tmp_path: Path) -> None:
    """The helper's pattern-detector requires both ``path`` and
    ``mtime_ns`` to consider a dict a fingerprint block. A future
    schema that adds a ``path`` field elsewhere (e.g. a "source
    URL" pointing to a fetched chart) must not be accidentally
    rewritten. Pins that scoping rule."""
    from scripts._sanitize_shipped_cache_paths import (
        sanitize_shipped_cache_paths,
    )

    _write_cache(
        tmp_path,
        "future_schema.json",
        {
            "source": {
                # path-shaped, but not a PDF fingerprint block —
                # no mtime_ns. Sanitiser must leave it alone.
                "path": "C:\\flying\\cvfr-routemaster\\source.pdf",
                "url": "https://example.com/chart.pdf",
            },
        },
    )

    report = sanitize_shipped_cache_paths(tmp_path)

    assert report.total_fields_updated() == 0
    cached = _read_cache(tmp_path, "future_schema.json")
    # The not-a-fingerprint-block path is preserved verbatim. The
    # privacy concern about this future schema, when it arrives,
    # is the responsibility of whoever adds it — they'll add a
    # case here and broaden the detector.
    assert (
        cached["source"]["path"]
        == "C:\\flying\\cvfr-routemaster\\source.pdf"
    )


# ---------------------------------------------------------------------------
# Build-script integration
# ---------------------------------------------------------------------------


def test_windows_build_script_calls_sanitiser_after_derived_files() -> None:
    """Pin that the Windows build pipeline runs sanitisation
    after ``_write_shipped_derived_files`` (because that helper
    writes the JSONs back to disk, sanitising first would have
    its rewrites clobbered). v3.3+ renamed the predecessor step
    from ``_restamp_cache_fingerprints`` to
    ``_write_shipped_derived_files`` — see
    ``scripts/build_release.py``'s changelog comment near the
    function definition for context. Source-level check so a
    future refactor that reorders the steps trips this test
    before shipping a broken bundle."""
    src = (
        Path(__file__).parent.parent / "scripts" / "build_release.py"
    ).read_text(encoding="utf-8")
    derived_idx = src.index("    _write_shipped_derived_files()")
    sanitise_idx = src.index("    _sanitize_shipped_cache_paths()")
    assert derived_idx < sanitise_idx, (
        "_sanitize_shipped_cache_paths() must be called AFTER "
        "_write_shipped_derived_files() in build_release.py — "
        "otherwise the derived-file writes would clobber the "
        "sanitised paths."
    )


def test_linux_build_script_calls_sanitiser_after_derived_files() -> None:
    """Same contract as the Windows version, applied to the
    Linux pipeline."""
    src = (
        Path(__file__).parent.parent
        / "scripts"
        / "build_release_for_linux.py"
    ).read_text(encoding="utf-8")
    derived_idx = src.index("    _write_shipped_derived_files()")
    sanitise_idx = src.index("    _sanitize_shipped_cache_paths()")
    assert derived_idx < sanitise_idx, (
        "_sanitize_shipped_cache_paths() must be called AFTER "
        "_write_shipped_derived_files() in build_release_for_linux.py "
        "— otherwise the derived-file writes would clobber the "
        "sanitised paths."
    )
