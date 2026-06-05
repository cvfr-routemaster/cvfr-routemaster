"""Diagnose why a planned route is showing "unknown" altitudes.

Recreates the full GUI pipeline offline:
1. Load the cached waypoints (no PDF re-extraction).
2. Build a Route from a typed-in token list (waypoint codes + ICAO coordinates).
3. Load the cached altitude arrows for both sheets.
4. Load the saved geo calibration; project the arrows.
5. For every segment: report its bearing/length, the matcher's verdict, and
   the closest 5 candidate arrows with their distance, sidedness, parity
   diff, and arrow bearing — so we can see which of the matcher's three
   gates (radius / parity / sidedness) accepted or rejected each one.

Run from the project root::

    python scripts/debug_route_altitudes.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cvfr_routemaster.altitude_arrows import (  # noqa: E402
    GeoAltitudeArrow,
    MATCH_PARALLEL_TOL_DEG,
    MATCH_RADIUS_NM,
    MATCH_STACK_RADIUS_NM,
    _arrow_side_of_segment,
    _circular_diff_deg,
    _great_circle_distance_to_segment_nm,
    extract_altitude_arrows,
    match_altitudes_for_route,
    project_arrows_to_lonlat,
)
from cvfr_routemaster.altitude_cache import (  # noqa: E402
    save_altitude_arrows,
    try_load_altitude_arrows,
)
from cvfr_routemaster.geo_calibration import (  # noqa: E402
    load_saved_calibration,
    load_sheet_calibration_or_reason,
)
from cvfr_routemaster.map_crop import CropMeta  # noqa: E402
from cvfr_routemaster.route import (  # noqa: E402
    Route,
    RoutePoint,
    RouteSegment,
    great_circle_distance_nm,
    magnetic_bearing_deg,
    true_bearing_deg,
)
from cvfr_routemaster.waypoint_types import WaypointRecord  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NORTH_PDF = PROJECT_ROOT / "CVFR-NORTH-OCT-2025-UPD2.pdf"
SOUTH_PDF = PROJECT_ROOT / "CVFR-SOUTH-OCT-2025-UPD2.pdf"

DEFAULT_ROUTE_TOKENS = [
    "LLHZ", "BAZRA", "DEROR", "SHARO", "ZYAAR", "HADRA", "FRDIS", "BOREN",
    "HOTRM", "DAROM", "3249N03457E", "3250N03458E", "GALIM", "LLHA",
]


def _parse_route_tokens(argv: list[str]) -> list[str]:
    """CLI: pass tokens space-separated, or omit for the default LLHZ→…→LLHA."""
    if len(argv) > 1:
        joined = " ".join(argv[1:])
        return [t for t in joined.split() if t.strip()]
    return list(DEFAULT_ROUTE_TOKENS)


ROUTE_TOKENS = _parse_route_tokens(sys.argv)


def _load_waypoint_dict() -> dict[str, WaypointRecord]:
    cache_file = PROJECT_ROOT / ".cvfr_routemaster" / "waypoints_cache.json"
    if not cache_file.is_file():
        print(f"[!] waypoint cache missing: {cache_file}")
        return {}
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    out: dict[str, WaypointRecord] = {}
    for item in raw.get("records", []):
        if not isinstance(item, dict):
            continue
        try:
            wp = WaypointRecord(
                index=int(item["index"]),
                code=str(item["code"]),
                name_he=str(item.get("name_he", "")),
                reporting_type=str(item.get("reporting_type", "")),
                lat=float(item["lat"]),
                lon=float(item["lon"]),
                lat_dms=str(item["lat_dms"]),
                lon_dms=str(item["lon_dms"]),
            )
            out[wp.code] = wp
        except (KeyError, TypeError, ValueError):
            continue
    return out


_ICAO_COORD_RE = re.compile(
    r"^(?P<lat_d>\d{2})(?P<lat_m>\d{2})(?P<lat_h>[NS])"
    r"(?P<lon_d>\d{3})(?P<lon_m>\d{2})(?P<lon_h>[EW])$"
)


def _parse_icao_coord(token: str) -> tuple[float, float] | None:
    m = _ICAO_COORD_RE.match(token)
    if m is None:
        return None
    lat = int(m["lat_d"]) + int(m["lat_m"]) / 60.0
    if m["lat_h"] == "S":
        lat = -lat
    lon = int(m["lon_d"]) + int(m["lon_m"]) / 60.0
    if m["lon_h"] == "W":
        lon = -lon
    return lat, lon


def _build_segments(
    tokens: list[str],
    waypoints: dict[str, WaypointRecord],
) -> list[dict[str, object]]:
    """Produce a list of plain dicts (lat/lon/label/waypoint of each end +
    bearing) so we can match using the same code path as the GUI.

    The ``waypoint`` slot is the actual ``WaypointRecord`` for real ICAO
    5-letter codes and ``None`` for free-clicked / ICAO-coord intermediates.
    This drives the per-segment radius selection (strict for legs between
    real waypoints, loose for any leg with at least one intermediate),
    so the diagnostic faithfully mirrors the GUI's matcher behaviour
    instead of treating every segment as intermediate."""
    pts: list[dict[str, object]] = []
    for tok in tokens:
        wp = waypoints.get(tok)
        if wp is not None:
            pts.append(
                {
                    "label": wp.code,
                    "lat": wp.lat,
                    "lon": wp.lon,
                    "kind": "wp",
                    "waypoint": wp,
                }
            )
            continue
        coord = _parse_icao_coord(tok)
        if coord is None:
            print(f"[!] cannot resolve token {tok!r} (not in waypoint cache and not ICAO coord)")
            continue
        lat, lon = coord
        pts.append(
            {
                "label": tok,
                "lat": lat,
                "lon": lon,
                "kind": "intermediate",
                "waypoint": None,
            }
        )

    segs: list[dict[str, object]] = []
    for i in range(1, len(pts)):
        a = pts[i - 1]
        b = pts[i]
        segs.append(
            {
                "from": a,
                "to": b,
                "bearing_true": true_bearing_deg(a["lat"], a["lon"], b["lat"], b["lon"]),
                "bearing_mag": magnetic_bearing_deg(a["lat"], a["lon"], b["lat"], b["lon"]),
                "distance_nm": great_circle_distance_nm(
                    a["lat"], a["lon"], b["lat"], b["lon"]
                ),
            }
        )
    return segs


def _load_arrows_for_sheet(sheet: str, pdf: Path) -> list:
    """Read the cached arrows, but don't fingerprint-validate against current
    render parameters (so this script works even if the cache was made by an
    old run). The schema is small enough that a tolerant load is safe."""
    cache_file = PROJECT_ROOT / ".cvfr_routemaster" / f"altitude_arrows_{sheet}.json"
    if not cache_file.is_file():
        print(f"[!] no cached arrows for {sheet}: {cache_file}")
        return []
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    crop_d = raw.get("crop", {})
    try:
        crop = CropMeta(
            offset_x=int(crop_d["offset_x"]),
            offset_y=int(crop_d["offset_y"]),
            source_w=int(crop_d["source_w"]),
            source_h=int(crop_d["source_h"]),
            cropped_w=int(crop_d["cropped_w"]),
            cropped_h=int(crop_d["cropped_h"]),
        )
    except (KeyError, TypeError, ValueError):
        print(f"[!] cached crop meta for {sheet} is malformed")
        return []
    dpi = float(raw.get("render_dpi", 0))
    arrows = try_load_altitude_arrows(
        PROJECT_ROOT, pdf, sheet, render_dpi=dpi, crop=crop,
    )
    if arrows is None:
        # The cache fingerprint moved (PDF mtime/size changed, render
        # parameters drifted, OR — most commonly during local dev — the
        # cache schema version bumped). The GUI auto-recovers by
        # re-extracting on next launch; mirror that here so the script
        # is self-healing too.
        print(
            f"[!] {sheet} cache rejected (schema/PDF/render mismatch); "
            f"re-extracting from {pdf.name} live (this takes ~10–20 s)…"
        )
        fresh = extract_altitude_arrows(pdf, render_dpi=dpi, crop=crop)
        print(f"    extracted {len(fresh)} arrows; writing back to cache")
        save_altitude_arrows(
            PROJECT_ROOT, pdf, sheet, fresh, render_dpi=dpi, crop=crop,
        )
        return fresh
    return arrows


def main() -> int:
    print("=" * 78)
    print(f"diagnosing route altitudes for: {' '.join(ROUTE_TOKENS)}")
    print("=" * 78)

    waypoints = _load_waypoint_dict()
    print(f"\nwaypoint cache: {len(waypoints)} records")

    segs = _build_segments(ROUTE_TOKENS, waypoints)
    print(f"built {len(segs)} segments\n")

    n_arrows = _load_arrows_for_sheet("north", NORTH_PDF)
    s_arrows = _load_arrows_for_sheet("south", SOUTH_PDF)
    print(f"\ncached arrows: north={len(n_arrows)}  south={len(s_arrows)}")

    raw = load_saved_calibration(PROJECT_ROOT)
    if not raw:
        print("\n[!] no saved calibration on disk — every segment will show 'unknown'.")
        return 0

    n_layout = (raw.get("north") or {}).get("map_layout")
    s_layout = (raw.get("south") or {}).get("map_layout")
    cal_n, en = load_sheet_calibration_or_reason(raw, "north", NORTH_PDF, n_layout, "North")
    cal_s, es = load_sheet_calibration_or_reason(raw, "south", SOUTH_PDF, s_layout, "South")
    print(f"\ncalibration: north={'OK' if cal_n else f'NONE ({en})'}  "
          f"south={'OK' if cal_s else f'NONE ({es})'}")

    geo: dict[str, list[GeoAltitudeArrow]] = {}
    if cal_n is not None and n_arrows:
        geo["north"] = project_arrows_to_lonlat(n_arrows, cal_n)
    if cal_s is not None and s_arrows:
        geo["south"] = project_arrows_to_lonlat(s_arrows, cal_s)
    print(f"projected geo arrows: north={len(geo.get('north', []))}  "
          f"south={len(geo.get('south', []))}\n")

    if not geo:
        print("[!] no projected arrows (no calibrated sheet has arrows). Aborting.")
        return 0

    # Per-segment diagnostic
    print("=" * 78)
    print("per-segment diagnostic — each segment's matcher verdict")
    print("=" * 78)

    radius_nm = MATCH_RADIUS_NM
    parallel_tol = MATCH_PARALLEL_TOL_DEG
    print(
        f"thresholds: radius={radius_nm} nm, "
        f"parallel tolerance=+/-{parallel_tol} deg from segment bearing, "
        f"stack radius={MATCH_STACK_RADIUS_NM} nm"
    )
    print(
        "side: R=right of FROM->TO (chart's our-direction convention), "
        "L=left, .=on line, ?=bidirectional (no side preference)"
    )
    print(
        "type: PR=parallel-right (best), PL=parallel-left (chart anomaly), "
        "AR/AL=anti-parallel (rejected), PP=perpendicular (rejected), "
        "BI=bidirectional"
    )

    flat_arrows = [(sheet, ga) for sheet, lst in geo.items() for ga in lst]

    # Compute the *route-level* matcher's actual verdict (with competitive
    # matching + stacking) so the diagnostic output reflects what the GUI
    # would show. The per-segment forensic dump below is still useful for
    # understanding *why* a particular segment matched or didn't, but the
    # final tuple shown for each segment now comes from the same code
    # path as the route panel.
    real_route_segments: list[RouteSegment] = []
    for seg in segs:
        a, b = seg["from"], seg["to"]
        real_route_segments.append(
            RouteSegment(
                from_point=RoutePoint(
                    lat=a["lat"], lon=a["lon"], waypoint=a.get("waypoint"),
                ),
                to_point=RoutePoint(
                    lat=b["lat"], lon=b["lon"], waypoint=b.get("waypoint"),
                ),
                from_label=str(a["label"]),
                to_label=str(b["label"]),
                distance_nm=float(seg["distance_nm"]),
                mag_bearing_deg=float(seg["bearing_mag"]),
            )
        )
    route_level_alts = match_altitudes_for_route(real_route_segments, geo)

    for idx, seg in enumerate(segs, 1):
        a = seg["from"]
        b = seg["to"]
        seg_brg = seg["bearing_true"]
        actual_alts = route_level_alts[idx - 1]
        actual_str = (
            ",".join(str(v) for v in actual_alts) if actual_alts else "unknown"
        )
        print(
            f"\n[{idx:2d}/{len(segs)}] {a['label']:<20s} -> {b['label']:<20s}  "
            f"len={seg['distance_nm']:5.1f} nm  brg(true)={seg_brg:5.1f} deg  "
            f"=> ROUTE MATCHER: {actual_str}"
        )

        scored: list[tuple[float, float, int, str, GeoAltitudeArrow]] = []
        for sheet, ga in flat_arrows:
            d_nm = _great_circle_distance_to_segment_nm(
                a["lat"], a["lon"], b["lat"], b["lon"], ga.lat, ga.lon,
            )
            forward_diff = _circular_diff_deg(ga.bearing_deg, seg_brg)
            side = _arrow_side_of_segment(
                a["lat"], a["lon"], b["lat"], b["lon"], ga.lat, ga.lon,
            )
            scored.append((d_nm, forward_diff, side, sheet, ga))

        def _classify(d_nm, forward_diff, side, ga):
            if ga.bidirectional:
                return "BI"
            if forward_diff <= parallel_tol:
                return "PR" if side < 0 else "PL"
            # Anti-parallel: forward-diff in (180-parallel_tol, 180+parallel_tol)
            if abs(forward_diff - 180.0) <= parallel_tol:
                return "AR" if side < 0 else "AL"
            return "PP"  # perpendicular

        parallel_right = [
            s for s in scored
            if s[0] <= radius_nm and not s[4].bidirectional and s[1] <= parallel_tol and s[2] < 0
        ]
        parallel_left = [
            s for s in scored
            if s[0] <= radius_nm and not s[4].bidirectional and s[1] <= parallel_tol and s[2] >= 0
        ]
        bidirectional_pass = [
            s for s in scored if s[0] <= radius_nm and s[4].bidirectional
        ]
        if parallel_right:
            d, fd, side, sh, ga = min(parallel_right, key=lambda s: s[0])
            print(
                f"   MATCH (parallel-right, {sh}): alts={ga.altitudes_ft}  "
                f"dist={d:.3f} nm  fwd-diff={fd:.1f}  arrow_brg={ga.bearing_deg:.1f}"
            )
        elif parallel_left:
            d, fd, side, sh, ga = min(parallel_left, key=lambda s: s[0])
            print(
                f"   MATCH (parallel-left, {sh}): alts={ga.altitudes_ft}  "
                f"dist={d:.3f} nm  fwd-diff={fd:.1f}  arrow_brg={ga.bearing_deg:.1f}"
            )
        elif bidirectional_pass:
            d, fd, side, sh, ga = min(bidirectional_pass, key=lambda s: s[0])
            print(
                f"   MATCH (bidirectional, {sh}): alts={ga.altitudes_ft}  "
                f"dist={d:.3f} nm"
            )
        else:
            print("   UNKNOWN")

        # Always show the closest 5 candidates so we see which gate killed
        # each — even on a successful match it's useful to confirm we
        # picked the right arrow, not just "an" arrow.
        print("   top 5 closest:")
        scored.sort(key=lambda s: s[0])
        for d, fd, side, sh, ga in scored[:5]:
            r_ok = "Y" if d <= radius_nm else "N"
            cls = _classify(d, fd, side, ga)
            side_lbl = "?" if ga.bidirectional else ("R" if side < 0 else ("L" if side > 0 else "."))
            print(
                f"     dist={d:6.3f} nm [r={r_ok}]  "
                f"type={cls}  side={side_lbl}  "
                f"fwd-diff={fd:5.1f}  "
                f"sheet={sh:5s}  arrow_brg={ga.bearing_deg:5.1f}  "
                f"alts={ga.altitudes_ft}  "
                f"@({ga.lat:6.3f},{ga.lon:6.3f})"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
