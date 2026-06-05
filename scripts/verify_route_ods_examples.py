#!/usr/bin/env python3
"""
Verify example route ODS files next to the project match ``route_ods_spec`` (P0).

Run from repo root:
  py scripts/verify_route_ods_examples.py
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# Repo root: parent of scripts/
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cvfr_routemaster.route_ods_spec import (  # noqa: E402
    BING_NOTE_HEADER_EN,
    CRUISE_SPEED_LABEL_HE,
    EXAMPLE_ROUTE_CODES_LLHA_LLIB,
    EXAMPLE_ROUTE_CODES_LLIB_LLHA,
    LEG_HEADER_ROW,
    ROUTE_HEADER_ROW_HE,
    ROUTE_SHEET_NAME,
    SUMMARY_MAX_ALT_LABEL_HE,
    SUMMARY_ROUTE_CODES_LABEL_HE,
    SUMMARY_TOTAL_DISTANCE_LABEL_HE,
    SUMMARY_TOTAL_TIME_LABEL_HE,
    SUMMARY_MAX_ALT_ROW,
    SUMMARY_ROUTE_CODES_ROW,
    SUMMARY_TOTAL_DISTANCE_ROW,
    SUMMARY_TOTAL_TIME_ROW,
)

_NS = {
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
}
_TNS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"


def _expand_row_cells(row_el: ET.Element) -> list[str]:
    """Flatten one table:table-row to a list of cell text values (repeat expanded)."""
    out: list[str] = []
    for cell in row_el.findall("table:table-cell", _NS):
        repeated = int(cell.get(f"{{{_TNS}}}number-columns-repeated", "1") or "1")
        parts: list[str] = []
        for p in cell.findall(".//text:p", _NS):
            if p.text:
                parts.append(p.text.strip())
            for ch in p:
                if ch.tail:
                    parts.append(ch.tail.strip())
        val = " ".join(x for x in parts if x).strip()
        if not val:
            ov = cell.get("{urn:oasis:names:tc:opendocument:xmlns:office:1.0}value")
            if ov is not None:
                val = str(ov)
        for _ in range(repeated):
            out.append(val)
    return out


def read_sheet_rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as zf:
        tree = ET.parse(zf.open("content.xml"))
    root = tree.getroot()
    for table in root.findall(".//table:table", _NS):
        name = table.get(f"{{{_TNS}}}name", "")
        if name != ROUTE_SHEET_NAME:
            continue
        rows_out: list[list[str]] = []
        for row in table.findall("table:table-row", _NS):
            rows_out.append(_expand_row_cells(row))
        return rows_out
    raise ValueError(f"No sheet named {ROUTE_SHEET_NAME!r} in {path}")


def _verify_file(path: Path, expected_route_codes: str) -> None:
    rows = read_sheet_rows(path)
    r0 = rows[0]
    assert r0[0] == CRUISE_SPEED_LABEL_HE, (path.name, r0[0])

    hdr = rows[LEG_HEADER_ROW][: len(ROUTE_HEADER_ROW_HE)]
    if hdr != list(ROUTE_HEADER_ROW_HE):
        for i, (a, b) in enumerate(zip(hdr, ROUTE_HEADER_ROW_HE)):
            if a != b:
                raise AssertionError(f"{path.name} header col {i}: {a!r} != {b!r}")

    # Footer labels present on expected rows (positions from examples).
    ra = rows[SUMMARY_MAX_ALT_ROW]
    assert SUMMARY_MAX_ALT_LABEL_HE in ra, path.name

    rd = rows[SUMMARY_TOTAL_DISTANCE_ROW]
    assert SUMMARY_TOTAL_DISTANCE_LABEL_HE in rd, path.name

    rt = rows[SUMMARY_TOTAL_TIME_ROW]
    assert SUMMARY_TOTAL_TIME_LABEL_HE in rt, path.name

    rc = rows[SUMMARY_ROUTE_CODES_ROW]
    ix = rc.index(SUMMARY_ROUTE_CODES_LABEL_HE)
    codes_cell = rc[ix + 1].strip()
    assert codes_cell == expected_route_codes, (
        path.name,
        codes_cell,
        expected_route_codes,
    )


def main() -> int:
    examples = [
        (_ROOT / "LLHA-LLIB.ods", EXAMPLE_ROUTE_CODES_LLHA_LLIB),
        (_ROOT / "LLIB-LLHA.ods", EXAMPLE_ROUTE_CODES_LLIB_LLHA),
    ]
    for path, exp in examples:
        if not path.is_file():
            print(f"SKIP (missing): {path.name}")
            continue
        _verify_file(path, exp)
        print(f"OK {path.name}")

    # Header sanity: Bing note column matches constant title piece.
    assert "Bing Maps" in BING_NOTE_HEADER_EN
    print("route_ods_spec P0 checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
