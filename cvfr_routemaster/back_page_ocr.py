"""
CVFR back-pages: hybrid extraction.

- PyMuPDF finds table rows/cells and reads **vector text** for ICAO code and DMS (reliable).
- **Hebrew name + reporting type** come from a tight pixmap of the meta columns only,
  via Tesseract (``heb+eng``), with PDF text as fallback when OCR returns nothing.

Full-page OCR is not used (it merged unrelated rows into unusable blobs).

Uses ``vendor/tesseract`` when present (see :mod:`cvfr_routemaster.tesseract_runtime`).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Sequence

import fitz
from PIL import Image
import pytesseract

from cvfr_routemaster.coords import parse_lat_dms, parse_lon_dms
from cvfr_routemaster.tesseract_runtime import (
    bundled_tessdata_dir,
    configure_bundled_tesseract,
)
from cvfr_routemaster.waypoint_types import WaypointRecord

_DRIV = "\u05d3\u05e8\u05d9\u05e9\u05d4"
_CHOVA = "\u05d7\u05d5\u05d1\u05d4"
_TYPE_MARK = re.compile(rf"({_DRIV}|{_CHOVA})")
_CODE_TOKEN = re.compile(r"^[A-Z]{4,5}$")

_META_PAD_PT = 5.5
_COORD_PAD_PT = 0.85
_META_OCR_ZOOM = 5.0
_OCR_CONFIG = r"--oem 3 --psm 6"

# Word-final forms (PDF/OCR often emits medial letters at token end).
_MEDIAL_TO_FINAL_END = {
    "\u05de": "\u05dd",
    "\u05e0": "\u05df",
    "\u05e4": "\u05e3",
    "\u05e6": "\u05e5",
}

# Hebrew letters (incl. presentation forms) for phrase extraction.
_HE = r"[\u0590-\u05FF\uFB1D-\uFB4F]"
_HE_WORD = _HE + r"+"
# Place-name stems that often survive OCR with junk prefixes (longest / most specific first).
_NAME_STEM_PATTERNS: tuple[str, ...] = (
    r"מחלף\s+" + _HE_WORD + r"(?:\s+" + _HE_WORD + r"){0,5}",
    r"צומת\s+" + _HE_WORD + r"(?:\s+" + _HE_WORD + r"){0,5}",
    r"מצפה\s+" + _HE_WORD + r"(?:\s+" + _HE_WORD + r"){0,4}",
    r"קרית\s+" + _HE_WORD + r"(?:\s+" + _HE_WORD + r"){0,3}",
    r"נווה\s+" + _HE_WORD + r"(?:\s+" + _HE_WORD + r"){0,4}",
    r"כפר\s+" + _HE_WORD + r"(?:\s+" + _HE_WORD + r"){0,5}",
    r"עינות\s+" + _HE_WORD + r"(?:\s+" + _HE_WORD + r"){0,3}",
    r"שדה\s+" + _HE_WORD + r"(?:\s+" + _HE_WORD + r"){0,3}",
    r"עין\s+" + _HE_WORD + r"(?:\s+" + _HE_WORD + r"){0,4}",
    r"בית\s+" + _HE_WORD + r"(?:\s+" + _HE_WORD + r"){0,5}",
    r"ים\s+" + _HE_WORD + r"(?:\s+" + _HE_WORD + r"){0,4}",
    r"הר\s+" + _HE_WORD + r"(?:\s+" + _HE_WORD + r"){0,3}",
    r"נמל\s+" + _HE_WORD + r"(?:\s+" + _HE_WORD + r"){0,3}",
)
_KEY_FIRST_WORD = frozenset(
    {
        "מחלף",
        "צומת",
        "מצפה",
        "קרית",
        "נווה",
        "כפר",
        "עינות",
        "שדה",
        "עין",
        "בית",
        "ים",
        "הר",
        "נמל",
        "תל",
        "באר",
        "מעלה",
        "שער",
        "גשר",
    }
)

# Tokens that appear as OCR/table junk on CVFR back-pages but not as real placename words.
# Stripped only as whole tokens (exact match). Keep small — extend when new OCR bugs repeat.
_JUNK_OCR_TOKENS = frozenset(
    {
        "היד",  # misread prefix (e.g. היידן → junk)
        "הוחו",  # misread שדה / noise
        "חר",  # stray syllable before דיו… / צופר
        "דיו",  # fragment of mis-split garbage
        "רודו",  # Latin rhythm / OCR garbage before ארגמן
        "רה",  # stray fragment (not הר)
        "רין",  # misread קרית… / רע noise before ציון
        "ין",  # broken יין / syllable (mid-name garbage)
    }
)


def _strip_junk_ocr_tokens(words: list[str]) -> list[str]:
    """Remove known OCR-noise tokens; if nothing left, keep original words."""
    if not words:
        return words
    out = [w for w in words if w not in _JUNK_OCR_TOKENS]
    return out if out else list(words)


def _is_repeated_letter_hebrew_token(tok: str) -> bool:
    """OCR often emits דד, ווו, etc. — not meaningful words."""
    if len(tok) < 2:
        return False
    if not re.fullmatch(r"[\u0590-\u05FF\uFB1D-\uFB4F]+", tok):
        return False
    return len(set(tok)) == 1


def _filter_hebrew_noise_tokens(words: list[str]) -> list[str]:
    """Drop repeated-letter chunks, lone letters in long phrases, leading junk bigrams."""
    if not words:
        return words
    words = [w for w in words if not _is_repeated_letter_hebrew_token(w)]

    if len(words) >= 3:
        words = [w for w in words if len(w) > 1]

    elif len(words) == 2:
        a, b = words[0], words[1]
        if _is_repeated_letter_hebrew_token(a):
            words = [b]
        elif len(a) == 1 and len(b) >= 2:
            words = [b]
        elif _is_repeated_letter_hebrew_token(b) and len(a) >= 2:
            words = [a]

    return _strip_junk_ocr_tokens(words)


def _apply_hebrew_token_noise_filter(s: str) -> str:
    words = re.findall(_HE_WORD, s)
    if not words:
        return s.strip()
    filtered = _filter_hebrew_noise_tokens(words)
    return " ".join(filtered).strip() if filtered else s.strip()


def _resolve_tesseract_executable() -> str:
    configure_bundled_tesseract()
    cmd = getattr(pytesseract.pytesseract, "tesseract_cmd", None)
    if cmd:
        p = Path(str(cmd))
        if p.is_file():
            return str(p.resolve())
    w = shutil.which("tesseract")
    if w:
        return w
    raise RuntimeError(_tesseract_missing_message())


def _tesseract_missing_message() -> str:
    """Build a platform-aware "Tesseract not found" instruction.

    The Windows and Linux releases use opposite distribution
    strategies (Windows bundles a slim Tesseract under
    ``<app>/tesseract/``; the Linux release expects system Tesseract
    via ``apt``), so a one-size-fits-all message would either misdirect
    the friend on Windows ("install Tesseract system-wide" — they'd
    have to find a Windows installer) or the user on Linux ("place a
    Tesseract install at <app>/tesseract/tesseract.exe" — wrong path
    separator and wrong package format on Debian).

    Detection is via ``sys.platform`` rather than ``getattr(sys,
    "frozen", False)`` because the right hint depends on the *target
    platform* (where the user can install Tesseract), not on whether
    the app is frozen — a dev-mode launch on Linux still wants the
    apt hint.
    """
    if sys.platform == "win32":
        return (
            "Tesseract OCR not found. Place a Tesseract install at "
            "<app>/tesseract/tesseract.exe (the layout the release zip uses) "
            "or <repo>/vendor/tesseract/tesseract.exe (dev layout — run "
            "`py scripts/fetch_vendor_tesseract.py` to populate it), or "
            "install Tesseract system-wide and add it to PATH."
        )
    # POSIX (Linux release path + macOS dev fallback).
    return (
        "Tesseract OCR not found. The Linux release expects a "
        "system-installed Tesseract; install it once with:\n\n"
        "    sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-heb\n\n"
        "(adjust the package manager if you're on a non-Debian distro). "
        "Alternatively, place a Tesseract install at "
        "<app>/tesseract/tesseract or <repo>/vendor/tesseract/tesseract."
    )


def ensure_tesseract_hebrew() -> None:
    exe = _resolve_tesseract_executable()
    # Resolve through bundled_tessdata_dir() so we transparently pick
    # up the Hebrew data from either the clean release layout
    # (<root>/tesseract/tessdata) or the dev layout
    # (<root>/vendor/tesseract/tessdata) — the previous hardcoded
    # ``vendor/tesseract`` path silently skipped the
    # heb-bundled fast path on release installs even when heb data was
    # present, forcing a slower ``--list-langs`` round-trip on every
    # OCR call.
    tess_dir = bundled_tessdata_dir()
    heb_file = tess_dir / "heb.traineddata" if tess_dir is not None else None
    if heb_file is not None and heb_file.is_file():
        try:
            subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15, check=True)
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"Tesseract binary failed: {exe}") from exc
        return

    try:
        proc = subprocess.run(
            [exe, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Could not run `tesseract --list-langs`.") from exc
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lines = {x.strip() for x in text.splitlines() if x.strip()}
    if "heb" not in lines and not any(x.startswith("heb") for x in lines):
        raise RuntimeError(
            "Tesseract Hebrew data (heb) missing. Run `py scripts/fetch_vendor_tesseract.py` "
            "or install Hebrew traineddata."
        )


def _normalize_ocr_line(s: str) -> str:
    t = unicodedata.normalize("NFKC", s)
    trans = str.maketrans(
        {
            "\u201d": '"',
            "\u201c": '"',
            "\u2033": '"',
            "\u2019": "'",
            "\u2032": "'",
            "\u00ba": "\u00b0",
            "\u2070": "0",
            "\xb0": "\u00b0",
        }
    )
    t = t.translate(trans)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _strip_bidi_marks(s: str) -> str:
    return re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", s)


def _clean_cell(text: object) -> str:
    s = str(text).replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    return s


def _tidy_dms_display(s: str) -> str:
    """Normalize spacing in DMS strings for stable CSV display (still parses with coords)."""
    t = _clean_cell(s)
    if not t:
        return t
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"\s*°\s*", "° ", t)
    t = re.sub(r"\s*'\s*", "'", t)
    t = re.sub(r'\s*"\s*', '" ', t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*N\s*$", " N", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*E\s*$", " E", t, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip()


def _normalize_terminal_hebrew_finals(name: str) -> str:
    """Medial → final at end of Hebrew-only tokens."""

    def fix_token(tok: str) -> str:
        if len(tok) < 2:
            return tok
        if not re.fullmatch(r"[\u0590-\u05FF\uFB1D-\uFB4F]+", tok):
            return tok
        last = tok[-1]
        rep = _MEDIAL_TO_FINAL_END.get(last)
        return tok[:-1] + rep if rep else tok

    parts = re.split(r"(\s+)", name)
    return "".join(fix_token(p) if p.strip() and not p.isspace() else p for p in parts)


def _name_needs_hebrew_recovery(s: str) -> bool:
    """True when likely OCR junk remains (Latin, digits, strong punctuation, or long runs of vav)."""
    if re.search(r"[A-Za-z]", s):
        return True
    if re.search(r"\d", s):
        return True
    if re.search(r"[(){}\[\]<>/\\]", s):
        return True
    if ".." in s or "--" in s or "—" in s:
        return True
    if re.search(r"\sו{3,}\s", s) or re.search(r"^ו{3,}\s", s) or re.search(r"\sו{3,}$", s):
        return True
    return False


def _extract_longest_stem_phrase(s: str) -> str | None:
    """If a known place-name stem appears, prefer the longest matching phrase."""
    best: str | None = None
    best_len = 0
    for pat in _NAME_STEM_PATTERNS:
        for m in re.finditer(pat, s):
            ph = m.group(0).strip()
            if len(ph) > best_len:
                best_len = len(ph)
                best = ph
    return best


def _hebrew_suffix_recovery(s: str) -> str | None:
    """When junk prefixes remain, keep a trailing run of Hebrew words (starts with keyword or long token)."""
    words = re.findall(_HE_WORD, s)
    if not words:
        return None
    for n in range(min(5, len(words)), 0, -1):
        chunk = words[-n:]
        first = chunk[0]
        if first in _KEY_FIRST_WORD or len(first) >= 4:
            return " ".join(chunk)
    last = words[-1]
    if len(last) >= 4:
        return last
    return None


def _polish_waypoint_name_he(name: str) -> str:
    """Strip OCR/table junk; keep Hebrew placenames readable for CSV."""
    raw_in = _strip_bidi_marks(_normalize_ocr_line(name))
    had_junk = _name_needs_hebrew_recovery(raw_in)
    s = raw_in
    s = re.sub(r'["\u201c\u201d\u2033\u05f4]+', " ", s)
    s = re.sub(r"[|\[\]<>]{1,}", " ", s)
    s = re.sub(r"[=]{2,}", " ", s)
    s = re.sub(r"[.:]{2,}", " ", s)
    s = re.sub(r"[–—]{1,}", " ", s)
    s = re.sub(r"\s-\s-\s", " ", s)
    s = re.sub(r"[(){}\[\]/\\]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    m = re.search(r"[\u0590-\u05FF\uFB1D-\uFB4F]", s)
    if m:
        s = s[m.start() :]
    s = re.sub(r"^[\s\-_=.|/\\:]+", "", s)
    s = re.sub(r"[\s\-_=.|/\\:]+$", "", s)
    s = re.sub(r"\s*[a-zA-Z]{1,12}\s*", " ", s)
    s = re.sub(r"\s+\d+\s*", " ", s)
    s = re.sub(r"^[a-zA-Z]{1,4}\s+", "", s)
    s = re.sub(r"\s+[a-zA-Z]{1,3}\s*$", "", s)
    s = re.sub(r"^[0-9\-.|]+\s+", "", s)
    s = re.sub(r"^ן\s+", "", s)
    s = re.sub(r"\s+", " ", s).strip()

    stem = _extract_longest_stem_phrase(s)
    stem_used = False
    if stem and stem != s.strip():
        if _name_needs_hebrew_recovery(s) or len(s) - len(stem) >= 4:
            s = stem
            stem_used = True

    words = re.findall(_HE_WORD, s)
    long_garbage = len(words) >= 5
    if not stem_used and (_name_needs_hebrew_recovery(s) or long_garbage):
        tail = _hebrew_suffix_recovery(s)
        if tail and tail != s.strip():
            s = tail

    s = re.sub(r"(?:^|\s)ו{2,}(?:\s|$)", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = _apply_hebrew_token_noise_filter(s)

    s = _normalize_terminal_hebrew_finals(s)
    s = re.sub(r"\u05f3+\s*$", "", s)
    s = re.sub(r"\s+", " ", s).strip()

    # Collapse stray punctuation between Hebrew tokens (OCR ".-" fragments).
    s = re.sub(r"(?:\.|-|–|—|\u05BE)+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = _apply_hebrew_token_noise_filter(s)

    words = re.findall(_HE_WORD, s)
    if (
        had_junk
        and len(words) == 2
        and len(words[0]) <= 3
        and len(words[1]) >= 4
        and words[0] not in _KEY_FIRST_WORD
    ):
        s = words[1]

    return s


def _clip_rect_padded(page: fitz.Page, bbox: tuple, pad_pt: float) -> fitz.Rect:
    try:
        r = fitz.Rect(bbox)
    except (ValueError, TypeError):
        return fitz.Rect(0, 0, 0, 0)
    if pad_pt > 0:
        r = fitz.Rect(r.x0 - pad_pt, r.y0 - pad_pt, r.x1 + pad_pt, r.y1 + pad_pt)
    pr = page.rect
    return fitz.Rect(
        max(r.x0, pr.x0),
        max(r.y0, pr.y0),
        min(r.x1, pr.x1),
        min(r.y1, pr.y1),
    )


def _rect_union(bboxes: Sequence[tuple]) -> fitz.Rect:
    if not bboxes:
        return fitz.Rect(0, 0, 0, 0)
    r = fitz.Rect(bboxes[0])
    for b in bboxes[1:]:
        r |= fitz.Rect(b)
    return r


def _cell_text_vector(page: fitz.Page, bbox: tuple, *, pad_pt: float) -> str:
    r = _clip_rect_padded(page, bbox, pad_pt)
    if r.is_empty:
        return ""
    return _clean_cell(page.get_text("text", clip=r))


def _split_name_and_reporting(blob: str) -> tuple[str, str]:
    blob = _strip_bidi_marks(_normalize_ocr_line(blob))
    blob = re.sub(r"\s+", " ", blob).strip()
    if not blob:
        return "", ""

    if blob.startswith("ARP") or blob == "ARP":
        rest = blob[3:].strip()
        rest = re.sub(r"^\s*,\s*", "", rest)
        return rest, "ARP"

    m = _TYPE_MARK.search(blob)
    if m:
        rep = m.group(1)
        name = (blob[: m.start()] + blob[m.end() :]).strip()
        name = re.sub(r"\s+", " ", name)
        return name, rep

    if re.search(r"\bARP\b", blob):
        name = re.sub(r"\bARP\b", " ", blob)
        name = re.sub(r"\s+", " ", name).strip()
        return name, "ARP"

    return blob, ""


def _meta_region_pixmap(page: fitz.Page, clip: fitz.Rect, zoom: float) -> Image.Image:
    if clip.is_empty:
        return Image.new("RGB", (1, 1), (255, 255, 255))
    m = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=m, clip=clip, alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def _ocr_meta_image(img: Image.Image) -> str:
    raw = pytesseract.image_to_string(img, lang="heb+eng", config=_OCR_CONFIG)
    return _strip_bidi_marks(_normalize_ocr_line(raw))


def _name_type_for_meta_columns(page: fitz.Page, meta_bboxes: list[tuple]) -> tuple[str, str]:
    """Hebrew name/type: OCR the meta-column strip only; PDF text if OCR is empty."""
    if not meta_bboxes:
        return "", ""
    ur = _rect_union(meta_bboxes)
    clip = _clip_rect_padded(page, tuple(ur), _META_PAD_PT)
    if clip.is_empty:
        return "", ""

    vec = _clean_cell(page.get_text("text", clip=clip))
    img = _meta_region_pixmap(page, clip, _META_OCR_ZOOM)
    ocr_txt = _ocr_meta_image(img)

    if ocr_txt.strip():
        return _split_name_and_reporting(ocr_txt)
    return _split_name_and_reporting(vec)


def _parse_table_row(page: fitz.Page, bboxes: list[tuple]) -> WaypointRecord | None:
    if len(bboxes) < 5:
        return None

    code_s = _cell_text_vector(page, bboxes[0], pad_pt=_COORD_PAD_PT)
    if "Letter" in code_s or ("Code" in code_s and len(code_s) > 8):
        return None
    if not _CODE_TOKEN.match(code_s.strip()):
        return None
    code = code_s.strip().upper()

    lat_s = _tidy_dms_display(_cell_text_vector(page, bboxes[1], pad_pt=_COORD_PAD_PT))
    lon_s = _tidy_dms_display(_cell_text_vector(page, bboxes[2], pad_pt=_COORD_PAD_PT))
    lat = parse_lat_dms(lat_s)
    lon = parse_lon_dms(lon_s)
    if lat is None or lon is None:
        return None

    name_he, reporting = _name_type_for_meta_columns(page, list(bboxes[3:]))
    name_he = _polish_waypoint_name_he(name_he)
    reporting = reporting.strip()

    return WaypointRecord(
        index=-1,
        code=code,
        name_he=name_he,
        reporting_type=reporting,
        lat=lat,
        lon=lon,
        lat_dms=lat_s,
        lon_dms=lon_s,
    )


def extract_waypoints_ocr(path: Path | str) -> list[WaypointRecord]:
    """
    Extract waypoints using PDF tables + vector coords + localized OCR for Hebrew columns.
    """
    ensure_tesseract_hebrew()
    configure_bundled_tesseract()

    path = Path(path)
    doc = fitz.open(path)
    seen: set[str] = set()
    rows: list[WaypointRecord] = []
    try:
        for pi in range(doc.page_count):
            page = doc[pi]
            try:
                ft = page.find_tables()
            except (RuntimeError, ValueError, AttributeError, OSError):
                continue
            if ft is None:
                continue
            tables = getattr(ft, "tables", None) or []
            for tab in tables:
                try:
                    trows = tab.rows
                except (RuntimeError, ValueError, AttributeError):
                    continue
                for ri, trow in enumerate(trows):
                    if ri == 0:
                        continue
                    try:
                        bboxes = trow.cells
                    except (RuntimeError, ValueError, AttributeError):
                        continue
                    if len(bboxes) < 5:
                        continue
                    rec = _parse_table_row(page, list(bboxes))
                    if rec is None:
                        continue
                    if rec.code in seen:
                        continue
                    seen.add(rec.code)
                    rows.append(rec)
    finally:
        doc.close()

    out: list[WaypointRecord] = []
    for i, r in enumerate(rows):
        out.append(
            WaypointRecord(
                index=i + 1,
                code=r.code,
                name_he=r.name_he,
                reporting_type=r.reporting_type,
                lat=r.lat,
                lon=r.lon,
                lat_dms=r.lat_dms,
                lon_dms=r.lon_dms,
            )
        )
    return out
