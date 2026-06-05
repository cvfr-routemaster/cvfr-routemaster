"""Ad-hoc release-folder leak scanner.

Walks every file under ``release/`` and reports any of:

CRITICAL (real leaks):
  * The dev-machine absolute path ``C:\\flying\\cvfr-routemaster``
    (and slash variants) — the dev's repo location.
  * The Windows username ``lfaerman``.
  * Any other ``C:\\Users\\<name>`` path that points at a real
    user profile (matches anything that isn't ``Public`` or
    ``Default``).
  * The historic dev-clone location ``cvfr-routemaster-v3p3``
    the user mentioned earlier (sanity-check that no test
    fixture pinned that string).
  * A repo-root path the dev might have on disk (any ``C:\\flying\\*``
    variant).

SUSPICIOUS (probable key material):
  * PEM headers (``-----BEGIN ...``).
  * AWS Access Key IDs (``AKIA[0-9A-Z]{16}``).
  * GitHub PATs (``ghp_``, ``gho_``, ``ghs_``, ``ghu_``,
    ``github_pat_``).
  * Slack tokens (``xox[abprs]-``).
  * OpenAI-style ``sk-`` keys that are at least 40 chars total.
  * JWTs (``eyJ`` followed by ≥30 chars of urlsafe-base64).

Scanning strategy:
  * Text files (``.txt``, ``.json``, ``.md``, ``.py``,
    ``.cfg``, ``.ini``, ``.toml``, ``.spec``, ``.html``):
    read as utf-8 with replacement and search line-by-line.
  * The source-bundle zip is extracted in memory and each
    entry treated as text.
  * Binaries (``.exe``, ``.dll``, ``.so``, ``.pyd``,
    ``.traineddata``): printable-ASCII string extraction (min
    length 6), then search.

Output: a CRITICAL section that fails the report if anything
matches, plus an INFO section listing every SUSPICIOUS hit
with file + offset so the dev can eyeball it. The script
returns exit code 1 if anything CRITICAL was found.

This is a one-shot diagnostic, not a regression test — kept in
``scripts/`` so it doesn't get bundled into the source zip.
"""
from __future__ import annotations

import io
import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = REPO_ROOT / "release"

# Dev-machine identifiers we treat as critical leaks. We search
# case-insensitively because Windows path comparisons are
# case-insensitive, and a leaked path that differs only in case
# is still a leaked path.
CRITICAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "DEV_REPO_PATH",
        re.compile(r"[cC]:[/\\]flying[/\\]cvfr-routemaster"),
    ),
    (
        "DEV_REPO_PATH_HISTORIC",
        re.compile(r"cvfr-routemaster-v3p3"),
    ),
    (
        "DEV_FLYING_FOLDER",
        re.compile(r"[cC]:[/\\]flying(?:[/\\]|\b)"),
    ),
    (
        "WINDOWS_USERNAME",
        re.compile(r"\blfaerman\b", re.IGNORECASE),
    ),
    # Any C:\Users\<name> path that names a real user profile.
    # We explicitly allow Public / Default / DefaultAppPool since
    # those are system-default identifiers, not a leak.
    (
        "USER_PROFILE_PATH",
        re.compile(
            r"[cC]:[/\\]Users[/\\](?!Public\b|Default\b|All Users\b|"
            r"DefaultAppPool\b)([A-Za-z0-9._-]+)"
        ),
    ),
]

SUSPICIOUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PEM_HEADER", re.compile(r"-----BEGIN [A-Z ]+-----")),
    ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "GITHUB_TOKEN",
        re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{20,}\b"),
    ),
    (
        "GITHUB_PAT",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    ("SLACK_TOKEN", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("OPENAI_LIKE_KEY", re.compile(r"\bsk-[A-Za-z0-9]{40,}\b")),
    (
        "JWT",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b"
        ),
    ),
]

# Extensions we treat as text. Anything else falls through to
# the binary strings extractor.
TEXT_EXTS = {
    ".txt",
    ".json",
    ".md",
    ".py",
    ".cfg",
    ".ini",
    ".toml",
    ".spec",
    ".html",
    ".csv",
    ".rst",
    ".log",
}

# Extensions we'll strings-scan as binaries. Everything else
# (images, icons, font files, traineddata) is opened and
# strings-scanned too — strings extraction is cheap on
# everything up to a few hundred MiB.
BINARY_EXTS = {
    ".exe",
    ".dll",
    ".so",
    ".pyd",
    ".traineddata",
    ".ico",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".jar",
    ".bin",
}

PRINTABLE_ASCII = re.compile(rb"[\x20-\x7e]{6,}")


def _iter_strings(data: bytes) -> list[str]:
    """Pull printable ASCII strings (>=6 chars) out of a blob."""
    return [m.group(0).decode("ascii", errors="replace") for m in PRINTABLE_ASCII.finditer(data)]


def _scan_text(label: str, text: str, findings: dict, source: str) -> None:
    """Run CRITICAL + SUSPICIOUS patterns over ``text`` and
    record matches under ``findings``.

    ``label`` identifies the on-disk file. ``source`` is a free-
    form note (``text``, ``zip-entry``, ``exe-strings``) the
    report prints so the reader knows how the match was found
    (a hit in the .exe's strings is qualitatively different
    from a hit in a JSON we wrote ourselves)."""
    for name, pat in CRITICAL_PATTERNS:
        for m in pat.finditer(text):
            findings["critical"].append(
                (label, source, name, _context(text, m.start(), m.end()))
            )
    for name, pat in SUSPICIOUS_PATTERNS:
        for m in pat.finditer(text):
            findings["suspicious"].append(
                (label, source, name, _context(text, m.start(), m.end()))
            )


def _context(text: str, start: int, end: int, radius: int = 60) -> str:
    """Return ~radius chars around the match for the report."""
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    snippet = text[lo:hi]
    snippet = snippet.replace("\r", " ").replace("\n", " ")
    snippet = re.sub(r"\s+", " ", snippet).strip()
    return snippet


def scan_file(path: Path, findings: dict) -> None:
    rel = path.relative_to(RELEASE_DIR)
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"[skip] {rel}: {exc}", file=sys.stderr)
            return
        _scan_text(str(rel), text, findings, "text")
    elif ext == ".zip":
        _scan_zip(path, findings)
    else:
        try:
            data = path.read_bytes()
        except OSError as exc:
            print(f"[skip] {rel}: {exc}", file=sys.stderr)
            return
        strings_blob = "\n".join(_iter_strings(data))
        _scan_text(str(rel), strings_blob, findings, "strings")


def _scan_zip(path: Path, findings: dict) -> None:
    rel = path.relative_to(RELEASE_DIR)
    with zipfile.ZipFile(path) as zf:
        for entry in zf.namelist():
            if entry.endswith("/"):
                continue
            with zf.open(entry) as fp:
                blob = fp.read()
            entry_ext = Path(entry).suffix.lower()
            if entry_ext in TEXT_EXTS or entry_ext == "":
                text = blob.decode("utf-8", errors="replace")
            else:
                text = "\n".join(_iter_strings(blob))
            _scan_text(f"{rel}::{entry}", text, findings, "zip-entry")


def main() -> int:
    if not RELEASE_DIR.is_dir():
        print(f"ERROR: {RELEASE_DIR} does not exist", file=sys.stderr)
        return 2

    findings: dict[str, list] = {"critical": [], "suspicious": []}

    file_count = 0
    total_bytes = 0
    for path in sorted(RELEASE_DIR.rglob("*")):
        if not path.is_file():
            continue
        file_count += 1
        total_bytes += path.stat().st_size
        scan_file(path, findings)

    print("=" * 72)
    print(
        f"Scanned {file_count} files, "
        f"{total_bytes / 1024 / 1024:.1f} MiB total"
    )
    print("=" * 72)

    print("\n--- CRITICAL: local paths / usernames -----------------------------------")
    if not findings["critical"]:
        print("  (none)")
    else:
        # Group by (label, kind) for readability, count occurrences.
        grouped: dict[tuple[str, str, str], list[str]] = {}
        for label, source, name, snippet in findings["critical"]:
            grouped.setdefault((label, source, name), []).append(snippet)
        for (label, source, name), snips in sorted(grouped.items()):
            print(f"\n  [{name}] {label}  ({source}, {len(snips)} hit(s))")
            for snip in snips[:5]:
                print(f"    ... {snip} ...")
            if len(snips) > 5:
                print(f"    ({len(snips) - 5} more)")

    print("\n--- SUSPICIOUS: secret-shaped tokens ------------------------------------")
    if not findings["suspicious"]:
        print("  (none)")
    else:
        grouped2: dict[tuple[str, str, str], list[str]] = {}
        for label, source, name, snippet in findings["suspicious"]:
            grouped2.setdefault((label, source, name), []).append(snippet)
        for (label, source, name), snips in sorted(grouped2.items()):
            print(f"\n  [{name}] {label}  ({source}, {len(snips)} hit(s))")
            for snip in snips[:5]:
                print(f"    ... {snip} ...")
            if len(snips) > 5:
                print(f"    ({len(snips) - 5} more)")

    print()
    return 1 if findings["critical"] else 0


if __name__ == "__main__":
    sys.exit(main())
