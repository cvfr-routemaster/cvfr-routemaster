"""Force a fresh re-extraction of altitude arrows for both chart sheets.

Reads the existing cache to recover the (DPI, crop) the GUI used the last
time it rendered the chart, then re-runs ``extract_altitude_arrows`` from
the current code and overwrites the on-disk cache. Use after editing the
extractor (e.g. tightening a filter) when you want to validate the change
offline without launching the GUI.

Run from the project root::

    python scripts/reextract_altitude_arrows.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cvfr_routemaster.altitude_arrows import extract_altitude_arrows  # noqa: E402
from cvfr_routemaster.altitude_cache import save_altitude_arrows  # noqa: E402
from cvfr_routemaster.map_crop import CropMeta  # noqa: E402


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    sheets = (
        ("north", "CVFR-NORTH-OCT-2025-UPD2.pdf"),
        ("south", "CVFR-SOUTH-OCT-2025-UPD2.pdf"),
    )
    for sheet, pdf_name in sheets:
        cache_path = project / ".cvfr_routemaster" / f"altitude_arrows_{sheet}.json"
        if not cache_path.is_file():
            print(f"[{sheet}] no existing cache at {cache_path}; skip")
            continue
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        crop_d = cached["crop"]
        crop = CropMeta(
            offset_x=crop_d["offset_x"],
            offset_y=crop_d["offset_y"],
            source_w=crop_d["source_w"],
            source_h=crop_d["source_h"],
            cropped_w=crop_d["cropped_w"],
            cropped_h=crop_d["cropped_h"],
        )
        dpi = float(cached["render_dpi"])
        pdf_path = project / pdf_name
        was = len(cached.get("arrows", []))
        print(f"[{sheet}] re-extracting from {pdf_name} at dpi={dpi} ...", flush=True)
        t0 = time.time()
        arrows = extract_altitude_arrows(pdf_path, render_dpi=dpi, crop=crop)
        elapsed = time.time() - t0
        print(
            f"[{sheet}] extracted {len(arrows)} arrows in {elapsed:.1f}s "
            f"(was {was} pre-fix; delta {len(arrows) - was:+d})"
        )
        save_altitude_arrows(
            project, pdf_path, sheet, arrows, render_dpi=dpi, crop=crop,
        )
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
