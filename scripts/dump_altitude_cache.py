"""End-to-end smoke: render → extract → cache → print sample.

Mirrors what ``MapLoadWorker`` + ``AltitudeArrowsWorker`` do in production,
but synchronously and with print statements, so we can eyeball the
harvested altitudes and the resulting cache JSON without launching the GUI.
Run from the project root::

    python scripts/dump_altitude_cache.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz
from PySide6.QtGui import QImage, QGuiApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cvfr_routemaster.altitude_arrows import (  # noqa: E402
    extract_altitude_arrows,
    match_altitudes_for_segment,
    project_arrows_to_lonlat,
)
from cvfr_routemaster.altitude_cache import (  # noqa: E402
    save_altitude_arrows,
    try_load_altitude_arrows,
)
from cvfr_routemaster.geo_calibration import load_saved_calibration, load_sheet_calibration_or_reason  # noqa: E402
from cvfr_routemaster.map_crop import crop_chart_white_margins_with_meta  # noqa: E402
from cvfr_routemaster.map_loader import _common_matrix  # noqa: E402


def _render_one(path: Path, mat: fitz.Matrix):
    doc = fitz.open(path)
    try:
        page = doc[0]
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = QImage(
            pix.samples, pix.width, pix.height, pix.stride,
            QImage.Format.Format_RGB888,
        ).copy()
        return crop_chart_white_margins_with_meta(img)
    finally:
        doc.close()


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    north = project_root / "CVFR-NORTH-OCT-2025-UPD2.pdf"
    south = project_root / "CVFR-SOUTH-OCT-2025-UPD2.pdf"
    if not north.is_file() or not south.is_file():
        print("North or South PDF missing; aborting.")
        return 1

    # Qt needs a QGuiApplication just to construct QImages.
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    _ = app

    n_doc = fitz.open(north)
    s_doc = fitz.open(south)
    try:
        mat = _common_matrix(
            [n_doc[0], s_doc[0]],
            render_dpi=288.0,
            max_edge_px=16384,
        )
    finally:
        n_doc.close()
        s_doc.close()
    eff_dpi = float(mat[0]) * 72.0
    print(f"effective render DPI = {eff_dpi:.2f}")

    print("rendering both sheets in parallel...")
    with ThreadPoolExecutor(max_workers=2) as pool:
        fn = pool.submit(_render_one, north, mat)
        fs = pool.submit(_render_one, south, mat)
        img_n, crop_n = fn.result()
        img_s, crop_s = fs.result()

    for label, img, crop in (
        ("north", img_n, crop_n),
        ("south", img_s, crop_s),
    ):
        print(
            f"  {label}: pixmap={img.width()}x{img.height()}, "
            f"crop offset=({crop.offset_x},{crop.offset_y}), "
            f"source={crop.source_w}x{crop.source_h}"
        )

    # Use the cache if warm, extract otherwise — same path as the worker.
    print("\nextracting / loading arrows...")
    arrows: dict[str, list] = {}
    for sheet, pdf, crop in (
        ("north", north, crop_n),
        ("south", south, crop_s),
    ):
        cached = try_load_altitude_arrows(
            project_root, pdf, sheet, render_dpi=eff_dpi, crop=crop,
        )
        if cached is not None:
            print(f"  {sheet}: cache HIT, {len(cached)} arrows")
            arrows[sheet] = cached
            continue
        print(f"  {sheet}: cache MISS, extracting from PDF...")
        a = extract_altitude_arrows(pdf, render_dpi=eff_dpi, crop=crop)
        save_altitude_arrows(
            project_root, pdf, sheet, a, render_dpi=eff_dpi, crop=crop,
        )
        arrows[sheet] = a
        print(f"  {sheet}: extracted + cached, {len(a)} arrows")

    for sheet, lst in arrows.items():
        all_alts = [v for a in lst for v in a.altitudes_ft]
        top = Counter(all_alts).most_common(8)
        stacked = sum(1 for a in lst if len(a.altitudes_ft) >= 2)
        print(
            f"\n{sheet}: {len(lst)} arrows, "
            f"{stacked} with stacked altitudes, "
            f"top altitudes: {top}"
        )
        print("  first 6 arrows (uv, bearing, altitudes):")
        for a in lst[:6]:
            print(
                f"    u={a.u:.4f}  v={a.v:.4f}  "
                f"brg={a.bearing_deg:6.1f}°  "
                f"alts={a.altitudes_ft}"
            )

    cache_file = project_root / ".cvfr_routemaster" / "altitude_arrows_north.json"
    if cache_file.is_file():
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            print(
                f"\ncache file: {cache_file.relative_to(project_root)} — "
                f"size={cache_file.stat().st_size:,} bytes, "
                f"{len(payload.get('arrows', []))} records"
            )
        except (OSError, json.JSONDecodeError) as exc:
            print(f"\ncache file unreadable: {exc}")

    # If the user has a calibration on disk, project + match a couple of
    # sample segments to see what the GUI would show. The GUI passes a
    # current map layout to the calibration loader so the cached layout
    # has something to match against; here, we pre-load the saved layout
    # straight from the calibration payload (it's the same dict we'd have
    # captured live from the sheet item) so the projection actually runs.
    raw = load_saved_calibration(project_root)
    if raw:
        print("\nfound saved calibration; projecting arrows...")
        n_layout = (raw.get("north") or {}).get("map_layout")
        s_layout = (raw.get("south") or {}).get("map_layout")
        cal_n, _ = load_sheet_calibration_or_reason(
            raw, "north", north, n_layout, "North",
        )
        cal_s, _ = load_sheet_calibration_or_reason(
            raw, "south", south, s_layout, "South",
        )
        geo: dict[str, list] = {}
        if cal_n is not None:
            geo["north"] = project_arrows_to_lonlat(arrows["north"], cal_n)
        if cal_s is not None:
            geo["south"] = project_arrows_to_lonlat(arrows["south"], cal_s)
        print(
            f"  geo arrows: north={len(geo.get('north', []))}, "
            f"south={len(geo.get('south', []))}"
        )
        if geo:
            print("  first 4 projected (north) — lat, lon, brg, alts:")
            for ga in geo.get("north", [])[:4]:
                print(
                    f"    lat={ga.lat:7.4f}  lon={ga.lon:7.4f}  "
                    f"brg={ga.bearing_deg:6.1f}°  alts={ga.altitudes_ft}"
                )
    else:
        print("\nno saved calibration; skipping segment matching demo.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
