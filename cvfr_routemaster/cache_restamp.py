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

"""Runtime cache-fingerprint restamping after URL-sourced chart downloads.

What this module is for
-----------------------

The shipped cache JSONs (``geo_calibration.json``, ``map_images_meta.json``,
``waypoints_cache.json``, ``altitude_arrows_north.json``,
``altitude_arrows_south.json``) carry per-PDF fingerprint blocks
of the form ``{"path": str, "mtime_ns": int, "size": int}``.

At build time, those fingerprints reflect the dev's local PDFs at
the moment the dev's app wrote the cache. In v3.2 and earlier the
build script also COPIED the PDFs into the release bundle and ran
``restamp_cache_fingerprints`` to align the cache JSON mtimes with
the *shipped copies'* mtimes (which the WSL 9P bridge floored to
whole seconds — see ``scripts/_restamp_cache_fingerprints.py`` for
the gory backstory).

In v3.3+, the release does NOT ship the chart PDFs (Israeli
government terms of use prohibit redistribution). The PDFs are
downloaded on first run into ``<project_root>/.cvfr_routemaster/charts/``.
That means the shipped fingerprints (referencing the dev's
build-time mtime/size) do not match the user's just-downloaded
PDFs — and every cache JSON's fingerprint check would fail on
first launch unless we restamp at runtime.

What this module does
---------------------

After a successful download:

1. Stat the downloaded PDF to get its current ``(mtime_ns, size,
   path)``.
2. For each cache JSON that has a fingerprint block keyed to
   this sheet, overwrite the block with the new stat values.
3. Atomically rewrite each cache JSON.

The mapping from sheet (``north`` / ``south`` / ``back``) to the
specific cache-JSON-file + JSON-path inside it lives in
:data:`SHEET_FINGERPRINT_BINDINGS`. Adding a sixth cache JSON is
a one-place edit there.

Implementation note: we trust the URL serves the same byte
content the dev calibrated against (the user picked the source;
the build cookbook step 1 has the dev verify this before each
release). If a future CAAI publication changes byte content but
not URL, the (size) part of the cache fingerprint check still
correctly rejects the old cache and triggers re-render / re-OCR /
re-calibrate. We aren't suppressing that safety net — we're only
restamping the (mtime_ns) drift that's a copy-operation artifact,
not a content-difference signal.

What this module deliberately does NOT do
-----------------------------------------

* No HTTP. The download already happened (in ``chart_source``).
  This module only touches local files.
* No Qt. Tests run without a ``QApplication``.
* No knowledge of the calibration model, render parameters, or
  altitude-arrow geometry — only the fingerprint blocks. The
  individual cache modules continue to own their own schemas.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final


# ---------------------------------------------------------------------------
# Binding table: per-sheet, which cache JSON files carry fingerprint
# blocks for the sheet's PDF, and where inside the JSON does that
# block live.
# ---------------------------------------------------------------------------


#: For each sheet, list ``(cache_filename, json_path_tuple)`` pairs.
#: ``json_path_tuple`` walks the JSON dict from root to the dict
#: containing ``mtime_ns`` / ``size`` / ``path`` keys. Empty tuple
#: would mean "root dict carries the fingerprint" — none of the
#: current caches do this, but we handle it defensively in
#: :func:`_walk_to_fingerprint_block`.
#:
#: Source of truth for this mapping is the cache-module code in
#: :mod:`cvfr_routemaster.geo_calibration`,
#: :mod:`cvfr_routemaster.map_image_cache`,
#: :mod:`cvfr_routemaster.waypoint_cache`, and the altitude-arrows
#: extractor. If any of those modules grow a new fingerprint block,
#: add the binding here too (and add a test in
#: :mod:`tests.test_cache_restamp`).
SHEET_FINGERPRINT_BINDINGS: Final[dict[str, tuple[tuple[str, tuple[str, ...]], ...]]] = {
    "north": (
        ("geo_calibration.json", ("north", "pdf")),
        ("altitude_arrows_north.json", ("pdf",)),
        ("map_images_meta.json", ("north_pdf",)),
    ),
    "south": (
        ("geo_calibration.json", ("south", "pdf")),
        ("altitude_arrows_south.json", ("pdf",)),
        ("map_images_meta.json", ("south_pdf",)),
    ),
    "back": (
        ("waypoints_cache.json", ("back_pdf",)),
    ),
}


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestampFieldUpdate:
    """One ``(cache_file, field_path)`` site that was restamped.

    ``cache_file`` is the filename relative to
    ``<project_root>/.cvfr_routemaster/``. ``field_path`` is the
    dotted JSON path (e.g. ``"north.pdf"``) the block lived at.
    ``old_mtime_ns`` / ``new_mtime_ns`` document the actual drift
    that motivated this restamp — useful in test output and any
    future diagnostic logging.
    """

    cache_file: str
    field_path: str
    old_mtime_ns: int
    new_mtime_ns: int
    old_size: int
    new_size: int


@dataclass(frozen=True)
class SheetRestampReport:
    """What happened during one sheet's restamp pass.

    Attributes:
      sheet_key: ``north`` / ``south`` / ``back``.
      pdf_path: Path to the downloaded PDF we restamped against.
      updates: Per-cache-file list of restamp updates. Cache files
        whose binding existed but whose JSON didn't contain the
        expected field path are silently skipped (older schemas /
        partial state). Cache files whose binding existed but
        whose file is absent on disk are listed in ``skipped``.
      skipped: Cache filenames that were in the binding table but
        not present on disk. This is the steady-state outcome for
        a user who never calibrated one of the sheets — that
        ``altitude_arrows_<sheet>.json`` simply doesn't exist —
        and is not an error.
    """

    sheet_key: str
    pdf_path: Path
    updates: list[RestampFieldUpdate] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _walk_to_fingerprint_block(
    root: object, path: tuple[str, ...]
) -> dict | None:
    """Descend ``path`` segments from ``root``; return the leaf dict
    or ``None`` if any segment is missing / not a dict.

    Defensive: a cache JSON might have a partial structure (e.g.
    ``geo_calibration.json`` with only the ``north`` block populated
    because the dev never calibrated south). Returning ``None``
    lets the caller skip that fingerprint site without raising.
    """
    cur: object = root
    for seg in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
    return cur if isinstance(cur, dict) else None


def _atomic_write_json(path: Path, data: object) -> None:
    """Write JSON atomically: ``path.tmp`` then replace.

    Same pattern as :func:`cvfr_routemaster.chart_source.save_manifest`.
    Indented for readability; preserves Unicode (``ensure_ascii=False``)
    so a future Hebrew-name field doesn't get garbled to escape
    sequences in the on-disk JSON.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def restamp_sheet_fingerprints(
    project_root: Path,
    sheet_key: str,
    pdf_path: Path,
) -> SheetRestampReport:
    """Restamp every cache-JSON fingerprint block that references
    ``sheet_key`` to match ``pdf_path``'s current ``(mtime_ns, size)``.

    Called right after :func:`chart_source.resolve_chart_source`
    returns a freshly-downloaded PDF for ``sheet_key``. Idempotent:
    a second call after no PDF mutation is a no-op (everything is
    already in sync). Pure with respect to ``pdf_path`` (stat
    syscalls only; never reads or writes the PDF itself).

    Args:
        project_root: The app's project root. ``<project_root>/
            .cvfr_routemaster/`` is where the cache JSONs live.
        sheet_key: Which sheet was downloaded. Must be one of the
            keys in :data:`SHEET_FINGERPRINT_BINDINGS`.
        pdf_path: The just-downloaded (or cached) PDF whose stat
            values become the new fingerprint.

    Returns:
        A :class:`SheetRestampReport` summarising every change.

    Raises:
        FileNotFoundError: if ``pdf_path`` doesn't exist (callers
            shouldn't hit this — the path is the one
            ``resolve_chart_source`` just returned after the
            atomic rename — so this is treated as a programmer
            error worth raising for).
        ValueError: if ``sheet_key`` is not a known sheet.
    """
    if sheet_key not in SHEET_FINGERPRINT_BINDINGS:
        raise ValueError(
            f"unknown sheet_key {sheet_key!r}; expected one of "
            f"{tuple(SHEET_FINGERPRINT_BINDINGS.keys())}"
        )
    if not pdf_path.is_file():
        raise FileNotFoundError(
            f"PDF for sheet {sheet_key!r} not at {pdf_path}; "
            f"restamp expected a downloaded file."
        )

    stat = pdf_path.stat()
    new_mtime_ns: int = stat.st_mtime_ns
    new_size: int = stat.st_size
    new_path_str = str(pdf_path)

    report = SheetRestampReport(sheet_key=sheet_key, pdf_path=pdf_path)
    cache_dir = project_root / ".cvfr_routemaster"

    for cache_filename, json_path in SHEET_FINGERPRINT_BINDINGS[sheet_key]:
        cache_file = cache_dir / cache_filename
        if not cache_file.is_file():
            # Cache JSON simply absent — common for a fresh
            # install where the user hasn't shipped the seed
            # cache, or for an optional cache the dev never
            # generated (e.g. altitude_arrows_south.json when
            # south was never calibrated).
            report.skipped.append(cache_filename)
            continue
        try:
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupt cache: skip rather than crash. The cache
            # module's load path will detect the corruption on
            # its own next launch and reject the cache.
            report.skipped.append(cache_filename)
            continue
        block = _walk_to_fingerprint_block(raw, json_path)
        if block is None:
            # Binding pointed at a path that's not in this cache
            # — older schema, partial state, or a fresh cache that
            # hasn't yet seeded the relevant sub-dict. Skip
            # silently, same rationale as missing-file.
            report.skipped.append(cache_filename)
            continue

        # Record the diff for the report BEFORE mutating, so the
        # report is faithful even when we overwrite. Default zero
        # values handle the "field was absent and is being newly
        # populated" case (an older shipped cache that pre-dates
        # one of the keys).
        old_mtime = int(block.get("mtime_ns", 0) or 0)
        old_size = int(block.get("size", 0) or 0)

        # Always update path so a debugger inspecting the cache
        # sees a path that exists; the cache check itself doesn't
        # use the path (it's informational since the
        # ``_sanitize_shipped_cache_paths`` step), but a stale
        # absolute path from the build host is confusing.
        block["path"] = new_path_str
        block["mtime_ns"] = new_mtime_ns
        block["size"] = new_size

        _atomic_write_json(cache_file, raw)
        dotted = ".".join(json_path) if json_path else "<root>"
        report.updates.append(
            RestampFieldUpdate(
                cache_file=cache_filename,
                field_path=dotted,
                old_mtime_ns=old_mtime,
                new_mtime_ns=new_mtime_ns,
                old_size=old_size,
                new_size=new_size,
            )
        )

    return report
