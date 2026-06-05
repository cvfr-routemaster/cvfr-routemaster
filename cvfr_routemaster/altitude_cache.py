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

"""
Per-chart-PDF disk cache of extracted altitude arrows.

A full extraction over both north and south chart PDFs takes ~30 s on a
laptop (mostly walking ~150 k vector drawings per page), so we cache the
flattened result on disk. The cache invalidates whenever:

* the PDF path / size / mtime changes (i.e. the user pointed at a new chart
  or AIS released an update), or
* the rendering parameters (DPI / max-edge cap) that produced the
  :class:`CropMeta` change — different parameters give different pixmap
  UV coordinates, so the cached arrows would be off, or
* the format version bumps (when this module's schema changes).

The cache is small (one short JSON record per arrow), so we don't bother
with binary or per-arrow files — one JSON per sheet is plenty.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cvfr_routemaster.altitude_arrows import AltitudeArrow
from cvfr_routemaster.map_crop import CropMeta

# Bump whenever the on-disk schema changes in a way that would mis-load OR
# whenever the extraction logic changes such that previously-cached arrows
# would now be wrong if reused.
#
# v1 → v2: arrow position changed from bbox-centre to tail-anchor (the bbox
# edge opposite the tip direction). Old caches stored arrows displaced by
# ~half the arrow's long axis from the chart's route line; v2 places them
# on the line, which is the coordinate frame the matcher expects.
# v2 → v3: within-arrow altitude outlier filter added (drop a stray
# sub-1/3-of-max number that landed inside an arrow rect, e.g. the spurious
# "400" inside a real "1500" arrow that polluted the GALIM→LLHA cell).
# v3 → v4: heading extraction switched from vertex-centroid heuristic to a
# concave-notch (highway-sign tail) detector, plus first-class support for
# bidirectional dual-headed arrows. Old caches stored bearings produced by
# a heuristic that's inverted on real chart geometry, and didn't carry the
# new ``bidirectional`` flag — both invalidate the matcher's results.
# v4 → v5: ``_MAX_ARROW_PATH_ITEMS`` gate added to the extractor so settlement
# blobs (Umm El Fahm and friends — yellow polygons with dozens of vertices
# whose bbox happens to swallow a nearby altitude digit span) no longer get
# emitted as phantom altitude arrows with bearings derived from the blob's
# concavity. Old caches contain those phantoms; the EIRON.1→ZMGID leg in
# particular used to pick up a bogus ``(3000,)`` from the Umm El Fahm blob.
# v5 → v6: ``_FORBIDDEN_ARROW_PATH_KINDS`` curve-segment gate added so
# holding-pattern racetrack symbols (parallel sides + semicircular Bézier
# ends, classic ``{'c': 4, 'l': 2}`` signature) no longer get emitted as
# phantom *bidirectional* altitude arrows. Old caches contain those
# phantoms; the EIRON-area holding pattern in particular polluted the
# EIRON.1→EIRON sub-leg of the LLIB→LLHZ reverse route with a bogus
# ``(2500,)`` even though no real 2500 ft arrow exists in that sub-leg.
# v6 → v7: bidirectional arrows now record their body-axis compass bearing
# (the tip-to-tip chord through the polygon) in ``bearing_deg`` instead of
# a flat ``0.0`` placeholder, and the matcher gates them on
# parallel-OR-antiparallel alignment against the segment direction. Old
# caches contain ``bearing_deg = 0.0`` for every bidirectional arrow, so
# the matcher's new axis gate would mis-accept E-W chart corridors for
# N-S route segments (and vice versa) until extraction reruns. The
# RIDNG→CLORE wrong-1200 match on the LLHZ→LLMZ route is the canonical
# regression this resolves.
ALTITUDE_CACHE_FORMAT_VERSION = 7


def _cache_dir(project_root: Path, mode_id: str | None = None) -> Path:
    d = project_root / ".cvfr_routemaster"
    if mode_id is not None:
        d = d / mode_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path(project_root: Path, sheet: str, mode_id: str | None = None) -> Path:
    return _cache_dir(project_root, mode_id) / f"altitude_arrows_{sheet}.json"


def _pdf_fp(path: Path) -> dict[str, Any]:
    p = path.resolve()
    st = p.stat()
    return {"path": str(p), "mtime_ns": st.st_mtime_ns, "size": st.st_size}


def _fp_match(cached: dict[str, Any], path: Path) -> bool:
    """Return True if the cache fingerprint matches the supplied PDF.

    Comparison is intentionally **path-independent**: only ``mtime_ns``
    and ``size`` participate. The cached ``path`` field is still
    written to disk for diagnostics ("which PDF produced this cache?")
    but is never compared, so a release zip that lands in
    ``C:\\Users\\Friend\\...`` instead of ``c:\\flying\\...`` still
    hits the cache as long as the PDF's bytes — and therefore its
    size, and the mtime preserved by ``shutil.copy2`` / zip-extraction
    on Windows — survived intact. Without this, every fresh install
    would burn 3-5 minutes re-extracting altitude arrows on first
    launch even though the bundled cache is already correct.

    A coincidental size+mtime collision between two different PDFs is
    theoretically possible but vanishingly unlikely (mtime is
    nanosecond-resolution on Windows NTFS); the chart PDFs are also
    immutable across the release so the failure mode "two distinct
    PDFs match" can't actually occur in this app's distribution
    pipeline.
    """
    if not path.is_file():
        return False
    cur = _pdf_fp(path)
    return (
        cached.get("mtime_ns") == cur["mtime_ns"]
        and cached.get("size") == cur["size"]
    )


def _crop_meta_to_dict(c: CropMeta) -> dict[str, int]:
    return {
        "offset_x": int(c.offset_x),
        "offset_y": int(c.offset_y),
        "source_w": int(c.source_w),
        "source_h": int(c.source_h),
        "cropped_w": int(c.cropped_w),
        "cropped_h": int(c.cropped_h),
    }


def _crop_meta_match(cached: Any, current: CropMeta) -> bool:
    if not isinstance(cached, dict):
        return False
    cur = _crop_meta_to_dict(current)
    return all(int(cached.get(k, -1)) == cur[k] for k in cur)


def try_load_altitude_arrows(
    project_root: Path,
    pdf_path: Path | str,
    sheet: str,
    *,
    render_dpi: float,
    crop: CropMeta,
    mode_id: str | None = None,
) -> list[AltitudeArrow] | None:
    """Return cached arrows if the manifest matches the current PDF + render
    parameters, otherwise ``None`` (telling the caller to extract afresh).

    A *missing* file returns ``None`` rather than raising, so first-run / cold-
    cache behaviour is the same as a stale-cache miss — the caller's
    ``extract → save`` sequence is the single source of truth.
    """
    pdf_path = Path(pdf_path)
    cache_file = _cache_path(project_root, sheet, mode_id)
    if not cache_file.is_file():
        return None
    try:
        meta = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None

    if int(meta.get("format_version", 0)) != ALTITUDE_CACHE_FORMAT_VERSION:
        return None
    if float(meta.get("render_dpi", -1)) != float(render_dpi):
        return None
    if not _fp_match(meta.get("pdf", {}), pdf_path):
        return None
    if not _crop_meta_match(meta.get("crop"), crop):
        return None

    raw = meta.get("arrows")
    if not isinstance(raw, list):
        return None

    out: list[AltitudeArrow] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        try:
            alts = tuple(int(v) for v in r["altitudes_ft"])
            out.append(
                AltitudeArrow(
                    u=float(r["u"]),
                    v=float(r["v"]),
                    bearing_deg=float(r["bearing_deg"]),
                    altitudes_ft=alts,
                    bidirectional=bool(r.get("bidirectional", False)),
                )
            )
        except (KeyError, TypeError, ValueError):
            # One bad record shouldn't poison the whole cache; keep going so
            # the caller still gets the rest. If everything's bad, the test
            # `if not out` in the caller will treat the cache as empty.
            continue
    return out


def save_altitude_arrows(
    project_root: Path,
    pdf_path: Path | str,
    sheet: str,
    arrows: list[AltitudeArrow],
    *,
    render_dpi: float,
    crop: CropMeta,
    mode_id: str | None = None,
) -> None:
    """Persist ``arrows`` for this PDF + render parameters.

    Silently no-ops when the PDF disappeared between extraction and write
    (rare but possible if the user moved files mid-load) — the next run will
    treat the missing manifest as a cold cache and re-extract.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        return

    payload = {
        "format_version": ALTITUDE_CACHE_FORMAT_VERSION,
        "render_dpi": float(render_dpi),
        "pdf": _pdf_fp(pdf_path),
        "crop": _crop_meta_to_dict(crop),
        "arrows": [
            {
                "u": float(a.u),
                "v": float(a.v),
                "bearing_deg": float(a.bearing_deg),
                "altitudes_ft": list(a.altitudes_ft),
                "bidirectional": bool(a.bidirectional),
            }
            for a in arrows
        ],
    }

    cache_file = _cache_path(project_root, sheet, mode_id)
    try:
        cache_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        # Cache write failures are non-fatal — extraction will simply rerun
        # on the next launch. Logging here would just be noise; the layout
        # diag log on the same run already records "altitudes.extracted=N".
        return


__all__ = [
    "ALTITUDE_CACHE_FORMAT_VERSION",
    "save_altitude_arrows",
    "try_load_altitude_arrows",
]
