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
from typing import Callable, Sequence

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

#: Tesseract whitelist for the 5-letter code column under the full-OCR
#: strategy (LSA): codes are always uppercase Latin, so constraining the
#: alphabet keeps the recognizer from emitting digits ("0" for "O",
#: "1" for "I") that would fail :data:`_CODE_TOKEN`.
_CODE_OCR_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

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


# --- tolerant DMS recovery for the FULL_OCR strategy (LSA) --------------
#
# The LSA coordinate cells are OCR'd (no Unicode text layer), and Tesseract
# routinely mangles the DMS *separators* even when the digits are right:
# the minute apostrophe ``'`` comes out as a second degree sign (``35° 11°
# 12"``), the seconds ``"`` lands where the apostrophe should (``35° 14"
# 05"``), a separator drops out entirely (``29° 54 03"``), or a digit is
# read as a letter (``35° O7'14"`` → ``O`` for ``0``; ``34° S157"`` → ``S``
# for ``5``). The strict :func:`coords.parse_lat_dms` / ``parse_lon_dms``
# regexes — correct for CVFR's clean vector text — reject all of these, so
# ~35 real reporting points per LSA sheet were being silently dropped.
#
# Recovery exploits the fixed structure: every Israel reporting point is
# ``DD MM SS`` with two zero-padded digits per field and a N/E hemisphere
# (the only hemisphere in Israel). So we letter-correct the obvious
# digit confusions, take the first six digits as ``DDMMSS``, and *gate the
# result against the Israel coordinate envelope* — a mis-segmented stream
# is rejected (row dropped) rather than placed at a wrong location, which
# matters for a flight-planning tool. CVFR keeps the strict parser.
_OCR_DIGIT_FIXUPS = str.maketrans(
    {
        "O": "0",
        "o": "0",
        "Q": "0",
        "l": "1",
        "I": "1",
        "i": "1",
        "|": "1",
        "S": "5",
        "s": "5",
        "B": "8",
    }
)
# Generous Israel envelope (decimal degrees). Southernmost point is Eilat
# (~29.55°N), northernmost ~33.3°N; west ~34.4°E, east ~35.8°E. Margins
# keep every real point while still catching a gross mis-parse.
_IL_LAT_LO, _IL_LAT_HI = 29.0, 33.8
_IL_LON_LO, _IL_LON_HI = 34.0, 36.2


def _ocr_dms_components(text: str) -> tuple[int, int, int] | None:
    """Recover ``(deg, min, sec)`` from a mangled OCR DMS cell, or ``None``.

    Letter→digit fixes are applied first, then the digit *groups* (runs of
    digits split by the OCR's separator glyphs) are mapped to fields:

    * Degrees are always two digits in Israel, so the leading two digits of
      the first group are the degrees — this also absorbs a separator that
      was misread as a digit and glued to the degrees (``317 20 14`` →
      ``31``/``20``/``14``).
    * The remaining digits are minutes then seconds. Minutes may be a
      single digit (``32° 2'29"`` → ``2``/``29``), and a dropped separator
      can merge them (``34° 5157"`` → ``51``/``57``); both are handled by
      reading two seconds digits off the end.

    ``min``/``sec`` must be < 60; anything else (or too few digits) returns
    ``None`` so the caller drops the row rather than guess.
    """
    groups = re.findall(r"\d+", text.translate(_OCR_DIGIT_FIXUPS))
    if not groups:
        return None
    head = groups[0]
    rest_digits = "".join(groups[1:])
    if not rest_digits:
        # Everything merged into one run: DDMMSS (+ optional trailing).
        if len(head) < 6:
            return None
        deg, minute, sec = int(head[0:2]), int(head[2:4]), int(head[4:6])
    else:
        if len(head) < 2:
            return None
        deg = int(head[:2])
        if len(rest_digits) == 3:
            # single-digit minute + two-digit second
            minute, sec = int(rest_digits[0]), int(rest_digits[1:3])
        elif len(rest_digits) >= 4:
            minute, sec = int(rest_digits[0:2]), int(rest_digits[2:4])
        else:
            return None
    if minute >= 60 or sec >= 60:
        return None
    return deg, minute, sec


def _ocr_dms_recover(
    text: str, *, hemi: str, lo: float, hi: float
) -> tuple[float | None, str]:
    """Tolerant DMS → ``(decimal_or_None, clean_display)`` for FULL_OCR.

    ``hemi`` is the fixed hemisphere for the column (``"N"`` latitude /
    ``"E"`` longitude — Israel is always north/east). The decimal is
    accepted only inside ``[lo, hi]``; outside that envelope (or on an
    unrecoverable cell) the decimal is ``None`` and the row is dropped.
    The display string is rebuilt cleanly from the recovered components so
    the waypoint table never shows the mangled separators.
    """
    comp = _ocr_dms_components(text)
    if comp is None:
        return None, _tidy_dms_display(text)
    deg, minute, sec = comp
    val = deg + minute / 60.0 + sec / 3600.0
    display = f"{deg}\u00b0 {minute:02d}' {sec:02d}\" {hemi}"
    if not (lo <= val <= hi):
        return None, display
    return val, display


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


def _clean_ocr_name_light(name: str) -> str:
    """Light cleanup for per-cell FULL_OCR names (LSA).

    The per-cell OCR path (:func:`_name_type_per_cell`) returns the
    Hebrew name verbatim, so the heavy, recovery-oriented
    :func:`_polish_waypoint_name_he` — tuned for the *noisy* CVFR
    union-strip OCR — does more harm than good here. Its place-name
    stem matcher, for instance, finds ``הר`` *inside* ``מהר`` and
    truncates ``כרם מהר"ל`` down to ``הר ל``. This does only safe
    normalization: strip bidi marks and cell-border rules, drop any
    stray leading non-Hebrew lead-in, trim trailing footnote /
    punctuation marks (the ``*`` after ``דור``), and fix word-final
    Hebrew letter forms — without ever dropping or rewriting a real
    placename token.
    """
    s = _strip_bidi_marks(_normalize_ocr_line(name))
    s = re.sub(r"[|\[\]<>]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    m = re.search(r"[\u0590-\u05FF\uFB1D-\uFB4F]", s)
    if m:
        s = s[m.start() :]
    s = re.sub(r"[\s*'\u05f3\u05f4\".,:;|/\\\-]+$", "", s)
    s = _reattach_detached_leading_letter(s)
    s = _normalize_terminal_hebrew_finals(s)
    return re.sub(r"\s+", " ", s).strip()


def _reattach_detached_leading_letter(s: str) -> str:
    """Repair a detached word-final letter that OCR moved to the front.

    The LSA per-cell OCR occasionally splits the final letter off the last
    word of an RTL name and emits it as a lone leading token — e.g.
    ``"אור עקיבא"`` (Or Akiva) comes out as ``"א אור עקיב"`` (the trailing
    ``א`` detached and re-ordered to the start). A real Hebrew placename
    never begins with a standalone single-letter word, so when the string is
    a lone leading Hebrew letter followed only by Hebrew words we reattach
    that letter to the end of the final token, restoring the name. Anything
    with Latin/digits in the tail, or more than one leading letter, is left
    untouched (too ambiguous to "fix" safely)."""
    m = re.match(
        r"^([\u0590-\u05FF\uFB1D-\uFB4F])\s+([\u0590-\u05FF\uFB1D-\uFB4F\s]+)$",
        s,
    )
    if not m:
        return s
    lead, rest = m.group(1), m.group(2).strip()
    parts = rest.split()
    if not parts:
        return s
    parts[-1] = parts[-1] + lead
    return " ".join(parts)


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


def _cell_text_ocr(
    page: fitz.Page,
    bbox: tuple,
    *,
    lang: str,
    psm: int = 7,
    zoom: float = _META_OCR_ZOOM,
    whitelist: str | None = None,
    pad_pt: float = _COORD_PAD_PT,
) -> str:
    """OCR a single table cell.

    Used by the FULL_OCR extraction strategy (LSA), whose embedded
    fonts carry no Unicode mapping — ``page.get_text`` returns mojibake
    there, so every column (code, lat, lon, name, reporting-type) must
    be recovered by rasterising the cell and running Tesseract.

    ``psm=7`` (treat the image as a single text line) suits the
    one-line cells. ``whitelist`` constrains the alphabet (used for the
    Latin-only code column).
    """
    clip = _clip_rect_padded(page, bbox, pad_pt)
    if clip.is_empty:
        return ""
    img = _meta_region_pixmap(page, clip, zoom)
    cfg = f"--oem 3 --psm {psm}"
    if whitelist:
        cfg += f" -c tessedit_char_whitelist={whitelist}"
    raw = pytesseract.image_to_string(img, lang=lang, config=cfg)
    return _strip_bidi_marks(_normalize_ocr_line(raw)).strip()


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


def _name_type_per_cell(
    page: fitz.Page, meta_bboxes: list[tuple]
) -> tuple[str, str]:
    """Hebrew name + reporting-type for the FULL_OCR strategy (LSA).

    Unlike :func:`_name_type_for_meta_columns` (used by the CVFR
    vector-hybrid path), this OCRs **each meta cell on its own** rather
    than rasterising the whole meta-column strip as one image. The
    union-strip approach drags in the inter-cell border rule and the
    inter-column gap, which Tesseract reads as spurious tokens — a
    leading ``|`` from the border plus short junk runs (``רז רז``,
    stray digits) that corrupt the Hebrew name or, when the name is
    short, erase it entirely. LSA's table has cleanly detected,
    well-separated cells, so OCR each one alone (where the name comes
    out verbatim) and classify by content: the cell that reads as a
    reporting-type marker (חובה / דרישה / ARP) is the type; whatever
    is left forms the name. Cells are joined right-to-left (descending
    ``x0``) so a multi-word Hebrew name keeps its reading order.
    """
    name_parts: list[tuple[float, str]] = []
    reporting = ""
    for bb in meta_bboxes:
        txt = _cell_text_ocr(page, bb, lang="heb+eng", psm=6)
        if not txt:
            # A few name cells (e.g. ENGDI "עין גדי", LLEY "עין יהב")
            # sit close enough to the cell's detected edge that the
            # tight default clip drops the glyphs and Tesseract finds
            # nothing. A single wider-pad, single-line retry recovers
            # them verbatim. Only the genuinely-empty tight pass pays
            # this cost, so the ~176 cells that already read cleanly
            # are untouched.
            txt = _cell_text_ocr(
                page, bb, lang="heb+eng", psm=7, pad_pt=4.0, zoom=7.0
            )
        if not txt:
            continue
        x0 = float(fitz.Rect(bb).x0)
        m = _TYPE_MARK.search(txt)
        if m and not reporting:
            reporting = m.group(1)
            leftover = (txt[: m.start()] + txt[m.end() :]).strip()
            if leftover:
                name_parts.append((x0, leftover))
            continue
        if not reporting and re.search(r"\bARP\b", txt, flags=re.IGNORECASE):
            reporting = "ARP"
            leftover = re.sub(
                r"\bARP\b", " ", txt, flags=re.IGNORECASE
            ).strip()
            if leftover:
                name_parts.append((x0, leftover))
            continue
        name_parts.append((x0, txt))
    name_parts.sort(key=lambda p: p[0], reverse=True)
    name = " ".join(t for _, t in name_parts).strip()
    return name, reporting


def _parse_table_row(
    page: fitz.Page, bboxes: list[tuple], *, full_ocr: bool = False
) -> WaypointRecord | None:
    if len(bboxes) < 5:
        return None

    # Code + DMS come from PDF vector text in the VECTOR_HYBRID strategy
    # (CVFR; the PDF has a usable Unicode text layer) but must be OCR'd
    # in the FULL_OCR strategy (LSA; embedded fonts lack a Unicode map).
    if full_ocr:
        # Codes are single uppercase Latin tokens; OCR sometimes appends a
        # stray hemisphere-ish letter as a separate "word" (e.g. ``HAON N``
        # for ``HAONN``). Collapsing internal whitespace recovers the token
        # without affecting clean reads.
        code_s = _cell_text_ocr(
            page, bboxes[0], lang="eng", whitelist=_CODE_OCR_WHITELIST
        ).replace(" ", "")
    else:
        code_s = _cell_text_vector(page, bboxes[0], pad_pt=_COORD_PAD_PT)
    if "Letter" in code_s or ("Code" in code_s and len(code_s) > 8):
        return None
    if not _CODE_TOKEN.match(code_s.strip()):
        return None
    code = code_s.strip().upper()

    if full_ocr:
        # Tolerant recovery: OCR mangles DMS separators (see
        # ``_ocr_dms_recover``); the strict parser dropped ~35 real points
        # per sheet. Hemisphere is fixed N/E for Israel and the result is
        # gated to the Israel envelope.
        lat, lat_s = _ocr_dms_recover(
            _cell_text_ocr(page, bboxes[1], lang="eng"),
            hemi="N",
            lo=_IL_LAT_LO,
            hi=_IL_LAT_HI,
        )
        lon, lon_s = _ocr_dms_recover(
            _cell_text_ocr(page, bboxes[2], lang="eng"),
            hemi="E",
            lo=_IL_LON_LO,
            hi=_IL_LON_HI,
        )
    else:
        lat_s = _tidy_dms_display(
            _cell_text_vector(page, bboxes[1], pad_pt=_COORD_PAD_PT)
        )
        lon_s = _tidy_dms_display(
            _cell_text_vector(page, bboxes[2], pad_pt=_COORD_PAD_PT)
        )
        lat = parse_lat_dms(lat_s)
        lon = parse_lon_dms(lon_s)
    if lat is None or lon is None:
        return None

    if full_ocr:
        name_he, reporting = _name_type_per_cell(page, list(bboxes[3:]))
        name_he = _clean_ocr_name_light(name_he)
    else:
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


def extract_waypoints_ocr(
    path: Path | str,
    *,
    full_ocr: bool = False,
    pages: Sequence[int] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[WaypointRecord]:
    """Extract reporting-point records from a chart PDF's table(s).

    Two strategies, selected by ``full_ocr``:

    * ``full_ocr=False`` (default; CVFR back-pages) — PDF table
      detection + **vector text** for code/lat/lon + localized OCR for
      the Hebrew name/reporting-type columns.
    * ``full_ocr=True`` (LSA) — every column is OCR'd, because the
      LSA PDFs' embedded fonts have no Unicode mapping and vector text
      comes out as mojibake.

    ``pages`` restricts table scanning to the given 0-based page
    indices (LSA's reporting points live only on page index 1, and the
    LSA map page is vector-dense enough that ``find_tables`` on it is
    very slow). ``None`` scans every page (CVFR back-pages, where the
    table may span several pages).

    ``progress`` is an optional ``(done, total)`` callback invoked once
    per candidate table row. Under the FULL_OCR strategy each row costs
    several Tesseract subprocess spawns, so the whole scan can run for
    minutes; the callback lets the GUI show a determinate bar instead
    of an indeterminate spinner that looks like a hang. ``total`` is the
    number of candidate rows (known after table detection, before any
    OCR); ``done`` counts rows processed so far. Called from whatever
    thread invoked this function — a Qt caller must marshal it to the
    GUI thread via a signal.
    """
    ensure_tesseract_hebrew()
    configure_bundled_tesseract()

    path = Path(path)
    doc = fitz.open(path)
    # Dedup within a sheet on (code, lat, lon) — not code alone — so two
    # genuinely different points stamped with the same code (e.g. נבטים and
    # נגב, both ``LLNV`` near Nevatim AFB) both survive while an accidental
    # repeat of the identical row collapses. Cross-sheet merge dedup uses
    # the same key (see ``waypoints._dedup_by_code``).
    seen: set[tuple[str, int, int]] = set()
    rows: list[WaypointRecord] = []
    try:
        if pages is None:
            page_indices: list[int] = list(range(doc.page_count))
        else:
            page_indices = [p for p in pages if 0 <= p < doc.page_count]

        # Pass 1 — table detection only (cheap relative to OCR). Collect
        # the candidate data rows up front so we know the row total
        # before spending any OCR time, which is what lets the progress
        # callback report a determinate "row X of N".
        worklist: list[tuple[fitz.Page, list]] = []
        for pi in page_indices:
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
                    worklist.append((page, list(bboxes)))

        total = len(worklist)
        if progress is not None:
            progress(0, total)

        # Pass 2 — the expensive per-row extraction (OCR under FULL_OCR).
        for done, (page, bboxes) in enumerate(worklist, start=1):
            rec = _parse_table_row(page, bboxes, full_ocr=full_ocr)
            if progress is not None:
                progress(done, total)
            if rec is None:
                continue
            key = (rec.code, round(rec.lat * 1e5), round(rec.lon * 1e5))
            if key in seen:
                continue
            seen.add(key)
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
