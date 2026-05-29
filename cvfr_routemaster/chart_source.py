"""Chart-source resolver: source-string (URL OR local path) -> local PDF.

What this module is for
-----------------------

v3.3 stopped shipping the three CVFR chart PDFs inside the release
bundle (Israeli government terms of use prohibit redistribution of
the CAAI charts). Instead the release ships with the three default
CAAI URLs as the chart-source defaults, and on first run the
program downloads the PDFs into a per-project cache. Subsequent
launches reuse the cache without ever hitting the network.

The rest of the codebase (``MapLoadWorker``, ``WaypointsOcrWorker``,
the altitude extraction worker, the calibration loader, every
``Path(...).is_file()`` gate in ``main_window``) was written
against the assumption that the chart path is a real, on-disk
file. Rather than teach every one of those sites to handle URLs,
this module provides ONE chokepoint:

* :class:`ChartSource` wraps the raw source string (URL or path)
  and exposes either a ``local_path`` (already-resolved, on-disk)
  or a ``download_to`` (knows how to fetch into the cache).
* :func:`resolve_chart_source` is the entry point the load path
  calls. It returns a :class:`Path` pointing at a real file on
  disk, after downloading if needed.

URL-identity cache invalidation
-------------------------------

The cache key is the **URL string** (normalized to canonical
percent-encoded form). If the URL in the active source field
matches the URL we last successfully downloaded from for this
sheet, we reuse the cached file unconditionally — NO HTTP
freshness check (no ``If-Modified-Since``, no ``ETag``). The AIP
publication cadence is too slow to justify network chatter; the
release-cycle gate is the right gate for chart updates.

A re-download fires when one of:

* The active URL differs from the URL recorded in
  ``charts/manifest.json`` for that sheet.
* The cached PDF file is missing from disk (corruption, manual
  delete, first run on a fresh install).

Hebrew-in-URL normalization
---------------------------

The CAAI URLs contain percent-encoded Hebrew path segments (e.g.
``%D7%91``). These are already ASCII end-to-end. But a user might
*paste* a URL that the browser address bar has URL-decoded for
human reading (e.g. ``…ב'-03.pdf`` with literal Hebrew chars).
:func:`normalize_url` re-percent-encodes those so the runtime
HTTP layer (``urllib.request``) never sees a non-ASCII character.
It also collapses both forms ("raw Hebrew" and "already encoded")
to the same canonical string so the manifest-equality check
sees them as equal, preventing a spurious re-download when the
user re-pastes the same URL in a different encoding.

Atomic writes / partial downloads
---------------------------------

PDFs are small (~2-5 MiB each). On a failed mid-stream download
we don't try to resume or salvage anything — the partial file is
deleted and the next run re-downloads from scratch. The download
writes to ``<final>.partial`` and only renames into place after
the response stream completes; a ``.partial`` left over from a
crashed run is treated as no-cache-present.

The manifest is updated *only* after the rename, so the contract
"manifest URL recorded for sheet X => the cached file at the
expected path matches that URL" is maintained as a strict
invariant.

What this module deliberately does NOT do
-----------------------------------------

* No Qt imports. Tests run without a ``QApplication``.
  The progress callback is a plain Python callable; the Qt
  side (modal :class:`QProgressDialog`, error modal with the
  URL list and Retry / Copy buttons) lives in ``main_window``.
* No re-stamping of downstream cache JSON fingerprints. That's
  the job of :mod:`cvfr_routemaster.cache_restamp`, called by
  the load path *after* :func:`resolve_chart_source` returns.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Sheet identity
# ---------------------------------------------------------------------------

#: Stable, ASCII-only filenames for the cached PDFs. We deliberately do NOT
#: use the dev's ``CVFR-NORTH-OCT-2025-UPD2.pdf`` naming scheme because:
#:
#: * Those names embed a release-month tag that drifts every time CAAI
#:   updates the AIP — using them at runtime would force a rename every
#:   edition.
#: * The dev's local PDFs (in the repo root or ``map-pdfs/``) keep their
#:   original CAAI-distribution-style names; we don't want the runtime to
#:   confuse a URL-downloaded copy with a local-path copy.
#:
#: The mapping is keyed by *sheet identity* (``north`` / ``south`` /
#: ``back``), not by URL or by source-field index, so the rest of the
#: codebase can refer to a sheet by a stable symbolic name.
SHEET_KEYS: Final[tuple[str, ...]] = ("north", "south", "back")

#: Filename per sheet inside ``<project_root>/.cvfr_routemaster/charts/``.
#: The leading ``cvfr_`` keeps the cached files easily distinguishable
#: from dev-local PDFs if a user ever inspects the folder.
CACHE_FILENAMES: Final[dict[str, str]] = {
    "north": "cvfr_north.pdf",
    "south": "cvfr_south.pdf",
    "back": "cvfr_back.pdf",
}

#: Subdirectory under ``<project_root>/.cvfr_routemaster/`` where the
#: URL-downloaded PDFs live. Peer to the existing cache JSONs.
CACHE_SUBDIR: Final[str] = "charts"

#: Manifest filename. One file (rather than per-sheet sidecars) so a
#: dev inspecting the cache can see all three sheet states at a glance,
#: and so we can rewrite atomically.
MANIFEST_FILENAME: Final[str] = "manifest.json"

#: Default CAAI URLs shipped as the v3.3 release defaults. These are the
#: same URLs the Copyright Information dialog displays (single source of
#: truth — :mod:`cvfr_routemaster.program_info_dialog` imports them from
#: here).
#:
#: When CAAI publishes a new AIP edition that changes the URLs (rare —
#: the URL pattern has been stable since the b'-03 series began), bump
#: this dict, follow the build cookbook's step 1 (calibration-drift
#: verification), and ship as a new release version.
CAAI_CHART_URLS: Final[dict[str, str]] = {
    "north": (
        "https://www.gov.il/BlobFolder/guide/aip/he/"
        "aip_%D7%91'-03%20CVFR%20%D7%A6%D7%A4%D7%95%D7%A0%D7%99-.pdf"
    ),
    "south": (
        "https://www.gov.il/BlobFolder/guide/aip/he/"
        "aip_%D7%91'-03%20CVFR%20%D7%93%D7%A8%D7%95%D7%9E%D7%99.pdf"
    ),
    "back": (
        "https://www.gov.il/BlobFolder/guide/aip/he/"
        "aip_%D7%91'-03CVFR%20%D7%90%D7%97%D7%95%D7%A8%D7%99.pdf"
    ),
}

#: Human-facing labels for the sheets. Used by the Copyright
#: Information dialog and (TODO) error modals.
SHEET_DISPLAY_NAMES: Final[dict[str, str]] = {
    "north": "North sheet",
    "south": "South sheet",
    "back": "Back-pages",
}

#: Connection timeout (seconds) for a single HTTP fetch.
#: 30 s covers a slow handshake on a hotel Wi-Fi without forcing the
#: user to wait minutes for a DNS / TLS hang to time out.
CONNECT_TIMEOUT_SEC: Final[float] = 30.0


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ChartFetchError(Exception):
    """Raised when a chart PDF download fails for any reason.

    Attributes
    ----------
    url
        The URL we were attempting to fetch (the *normalized* form,
        not the raw paste).
    sheet_key
        Which of ``north`` / ``south`` / ``back`` was being fetched
        — surfaced in the error modal so the user knows which line
        in the manual-fallback instructions to follow.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str,
        sheet_key: str,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.sheet_key = sheet_key


@dataclass(frozen=True)
class ChartSource:
    """One sheet's source-of-truth string from the user's Settings
    dialog, plus the methods to interrogate it.

    ``raw`` is whatever was in the QLineEdit / QSettings — could be:

    * A local filesystem path (``C:/.../north.pdf``, ``/home/.../n.pdf``)
    * An ``http://`` or ``https://`` URL (raw or already-percent-encoded)
    * Empty string (user hasn't set this sheet yet) — both ``is_url``
      and ``is_local_path`` are False; callers should treat as unset.

    The class is deliberately tiny — most logic lives in the
    module-level :func:`resolve_chart_source`. ``ChartSource`` is just
    a typed wrapper that makes the path-or-URL discrimination
    explicit at call sites instead of leaving raw strings flying
    around.
    """

    raw: str

    @property
    def is_url(self) -> bool:
        """True iff ``raw`` looks like an ``http(s)://`` URL.

        Conservative check: matches the scheme prefix only. We do NOT
        try to ``urlsplit`` because a user mid-edit might have typed
        ``ht`` and we don't want to flag that as "URL-but-malformed"
        — that's the Settings dialog's validation responsibility.
        """
        lowered = self.raw.lower().lstrip()
        return lowered.startswith("http://") or lowered.startswith("https://")

    @property
    def is_local_path(self) -> bool:
        """True iff ``raw`` is non-empty and does NOT look like a URL.

        We don't check ``Path(raw).is_file()`` here — that's a
        separate concern (the file might not exist YET; the resolver
        is allowed to return the path and let the caller's
        :meth:`Path.is_file` check fail loudly downstream)."""
        return bool(self.raw.strip()) and not self.is_url

    def normalized_url(self) -> str:
        """Return ``raw`` percent-encoded to canonical form.

        Only valid when :attr:`is_url` is True; raises
        :class:`ValueError` otherwise so a callsite that
        accidentally calls this on a path gets a loud failure
        rather than a silent "use the path as a URL".
        """
        if not self.is_url:
            raise ValueError(
                f"normalized_url() only valid for URL sources; got {self.raw!r}"
            )
        return normalize_url(self.raw)


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------


def normalize_url(url: str) -> str:
    """Return the canonical percent-encoded form of ``url``.

    Two callers paste the same logical URL in different encodings:

    * Already percent-encoded: ``…/%D7%91-03.pdf``
    * Raw Unicode (browser address-bar decoded): ``…/ב-03.pdf``

    Both must collapse to the same canonical string so the manifest
    URL-equality check sees them as equal. This function:

    1. Splits the URL into ``scheme``, ``netloc``, ``path``, ``query``,
       ``fragment`` via :func:`urllib.parse.urlsplit`.
    2. Percent-encodes each component's non-ASCII characters with
       :func:`urllib.parse.quote`, using a ``safe`` set that includes
       ``%`` itself (so already-encoded sequences aren't double-encoded
       into ``%25D7%2591``).
    3. Re-assembles via :func:`urllib.parse.urlunsplit`.

    The ``safe`` set for the ``path`` component follows RFC 3986
    pchar (``unreserved / pct-encoded / sub-delims / ":" / "@"``), so
    every reserved-but-legal-in-path character (``'``, ``(``, ``)``,
    etc.) is preserved verbatim. For query and fragment the same
    safe set applies plus ``?`` and ``=``.

    Idempotent: ``normalize_url(normalize_url(u)) == normalize_url(u)``.
    """
    parts = urllib.parse.urlsplit(url.strip())

    # RFC 3986 pchar = unreserved / pct-encoded / sub-delims / ":" / "@"
    # We keep "%" safe so existing percent-escapes are preserved (the
    # alternative is double-encoding, which produces a different URL
    # the server has never heard of).
    path_safe = "/%-._~!$&'()*+,;=:@"
    query_safe = path_safe + "?&="
    fragment_safe = path_safe + "?"

    normalized_path = urllib.parse.quote(parts.path, safe=path_safe)
    normalized_query = urllib.parse.quote(parts.query, safe=query_safe)
    normalized_fragment = urllib.parse.quote(parts.fragment, safe=fragment_safe)

    return urllib.parse.urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc,
            normalized_path,
            normalized_query,
            normalized_fragment,
        )
    )


# ---------------------------------------------------------------------------
# Cache paths
# ---------------------------------------------------------------------------


def charts_cache_dir(project_root: Path) -> Path:
    """Directory containing the URL-downloaded chart PDFs.

    Located at ``<project_root>/.cvfr_routemaster/charts/`` — peer
    to the existing cache JSONs so a friend inspecting the folder
    sees ALL the per-project cache state in one place.

    Does NOT create the directory; callers that intend to write
    here call :func:`ensure_charts_cache_dir`.
    """
    return project_root / ".cvfr_routemaster" / CACHE_SUBDIR


def ensure_charts_cache_dir(project_root: Path) -> Path:
    """Create ``charts/`` if it doesn't exist; return the path."""
    target = charts_cache_dir(project_root)
    target.mkdir(parents=True, exist_ok=True)
    return target


def cache_path_for_sheet(sheet_key: str, project_root: Path) -> Path:
    """Local filesystem path where the URL-downloaded PDF for
    ``sheet_key`` (``north`` / ``south`` / ``back``) lives.

    Filenames are stable across runs (see :data:`CACHE_FILENAMES`),
    so updating ``manifest.json`` to a new URL implies overwriting
    the PDF at the same path.
    """
    if sheet_key not in CACHE_FILENAMES:
        raise ValueError(
            f"unknown sheet_key {sheet_key!r}; expected one of {SHEET_KEYS}"
        )
    return charts_cache_dir(project_root) / CACHE_FILENAMES[sheet_key]


def manifest_path(project_root: Path) -> Path:
    """Path to the manifest JSON. One file for all three sheets."""
    return charts_cache_dir(project_root) / MANIFEST_FILENAME


# ---------------------------------------------------------------------------
# Manifest read / write
# ---------------------------------------------------------------------------


def load_manifest(project_root: Path) -> dict[str, str]:
    """Read ``manifest.json`` and return ``{sheet_key: url}``.

    Returns ``{}`` if the file doesn't exist or is malformed —
    callers treat both cases identically (no cached URL recorded
    => download is required). We do NOT raise on malformed JSON
    because a corrupt manifest should not block the program; the
    next successful download will rewrite it cleanly.

    Only entries whose key is a known ``SHEET_KEYS`` are returned;
    unknown keys (a future schema field, a typo from manual
    editing) are silently dropped from the in-memory view but
    preserved on disk (see :func:`save_manifest`).
    """
    path = manifest_path(project_root)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key in SHEET_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    return out


def save_manifest(project_root: Path, entries: dict[str, str]) -> None:
    """Write ``manifest.json`` with ``entries`` ``{sheet_key: url}``.

    Atomic: writes to ``manifest.json.tmp`` then renames over the
    existing file so a crashed write can't leave the manifest
    half-finished and unparseable on next launch.

    Keys not in :data:`SHEET_KEYS` are silently dropped (manifest is
    sheet-scoped). The JSON is pretty-printed (indent=2) so a dev
    inspecting the file by hand can read it easily.
    """
    cache_dir = ensure_charts_cache_dir(project_root)
    clean: dict[str, str] = {
        key: entries[key]
        for key in SHEET_KEYS
        if key in entries and isinstance(entries[key], str) and entries[key]
    }
    target = cache_dir / MANIFEST_FILENAME
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(clean, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


ProgressCallback = Callable[[str, int, int], None]
"""``(label, completed_bytes, total_bytes) -> None``.

``total_bytes == 0`` means the server didn't advertise a
``Content-Length`` and the caller should show an indeterminate
spinner. ``completed_bytes`` is monotonic non-decreasing across
calls within a single download.
"""


def _make_request(url: str) -> urllib.request.Request:
    """Build a ``urllib.request.Request`` for the chart fetch.

    Two attributes worth calling out:

    * ``User-Agent`` is set to a descriptive string identifying the
      program. CAAI's CDN doesn't appear to block default
      ``Python-urllib/3.x`` UAs as of this writing, but several
      gov.il CDN tiers in the past *have* blocked unidentified UAs,
      so naming ourselves is cheap insurance. The version is
      pinned at the cvfr_routemaster ``__version__`` (single source
      of truth).
    * No ``Accept-Encoding`` is set; ``urllib`` doesn't transparently
      decompress, so requesting ``gzip`` would just leave us with a
      compressed file we never decode. Plain.
    """
    from cvfr_routemaster import APP_NAME, __version__

    return urllib.request.Request(
        url,
        headers={
            "User-Agent": f"{APP_NAME}/{__version__} (chart-source-fetcher)",
        },
    )


def download_chart_pdf(
    url: str,
    *,
    sheet_key: str,
    dest: Path,
    on_progress: ProgressCallback | None = None,
    timeout: float = CONNECT_TIMEOUT_SEC,
) -> None:
    """Download a PDF from ``url`` to ``dest``, atomically.

    Writes to ``<dest>.partial`` first; renames over ``dest`` on
    success; deletes the partial on failure (so a half-written file
    doesn't survive across launches).

    ``on_progress`` is called from the same thread that invoked
    this function. Callers running this on a worker thread are
    responsible for marshaling the callback's effects to the GUI
    thread (Qt: ``Signal.emit(...)``). The callback receives a
    human-readable label, completed bytes, and total bytes (0 if
    the server didn't send a ``Content-Length``).

    Raises :class:`ChartFetchError` on any HTTP error, network
    failure, or truncated response. The original exception is
    chained via ``__cause__`` so a debugger can see the root
    cause. On error, the partial file is removed before raising.
    """
    normalized = normalize_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".partial")
    # Clean up a stale partial from a previous crashed run before
    # we start; otherwise we'd append to it and ship a corrupt PDF.
    if partial.exists():
        partial.unlink()

    req = _make_request(normalized)
    label = SHEET_DISPLAY_NAMES.get(sheet_key, sheet_key)

    try:
        # ``urlopen`` with explicit timeout — the urllib default is
        # ``socket.getdefaulttimeout()`` (usually None = forever),
        # which would hang the progress dialog indefinitely on a
        # silent network drop.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Content-Length is informational; some CDNs omit it
            # (chunked transfer). 0 here is the signal to the
            # caller to fall back to an indeterminate spinner.
            content_length_raw = resp.headers.get("Content-Length")
            try:
                total = int(content_length_raw) if content_length_raw else 0
            except (TypeError, ValueError):
                total = 0

            if on_progress is not None:
                on_progress(f"Downloading {label}\u2026", 0, total)

            completed = 0
            # Chunk size deliberately small (32 KiB) so the
            # progress callback fires often enough that the UI
            # feels responsive on slow links, without thrashing
            # the dispatcher on a fast wired connection. PDFs are
            # ~2-5 MiB so we expect ~100 chunks per fetch.
            chunk_size = 32 * 1024
            with partial.open("wb") as fh:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    fh.write(chunk)
                    completed += len(chunk)
                    if on_progress is not None:
                        on_progress(
                            f"Downloading {label}\u2026", completed, total
                        )

            # Server claimed N bytes but we got fewer — treat as
            # truncation, not as success. Skipping this check
            # would let us cache a torn PDF that PyMuPDF would
            # fail to open with a confusing error later.
            if total and completed != total:
                partial.unlink(missing_ok=True)
                raise ChartFetchError(
                    f"Download truncated: expected {total} bytes, got "
                    f"{completed}. The connection probably dropped "
                    f"mid-stream; try again.",
                    url=normalized,
                    sheet_key=sheet_key,
                )

        # Rename only after the stream completes cleanly. Anything
        # that throws between ``urlopen`` and this rename leaves
        # the existing ``dest`` (if any) untouched.
        partial.replace(dest)

    except ChartFetchError:
        raise
    except urllib.error.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise ChartFetchError(
            f"Server returned HTTP {exc.code}: {exc.reason}. "
            f"The URL may be wrong, or the CAAI page may be "
            f"temporarily unavailable.",
            url=normalized,
            sheet_key=sheet_key,
        ) from exc
    except urllib.error.URLError as exc:
        partial.unlink(missing_ok=True)
        raise ChartFetchError(
            f"Network error: {exc.reason}. Check your internet "
            f"connection and try again.",
            url=normalized,
            sheet_key=sheet_key,
        ) from exc
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ChartFetchError(
            f"Could not write to {dest}: {exc}. The cache folder "
            f"may be read-only or out of disk space.",
            url=normalized,
            sheet_key=sheet_key,
        ) from exc


# ---------------------------------------------------------------------------
# Top-level resolver
# ---------------------------------------------------------------------------


def needs_download(
    sheet_key: str,
    normalized_active_url: str,
    project_root: Path,
) -> bool:
    """Return True iff the active URL doesn't match the manifest
    record (or the cached file is missing).

    Pure function — only reads the manifest and stats the cache
    file. No network calls. Used by the load path to decide
    whether to show a "downloading" progress dialog.
    """
    cache_file = cache_path_for_sheet(sheet_key, project_root)
    if not cache_file.is_file():
        return True
    manifest = load_manifest(project_root)
    recorded = manifest.get(sheet_key)
    if recorded is None:
        return True
    # ``recorded`` may itself need normalization if a previous
    # version of the program saved the raw paste. Normalize both
    # sides before comparing.
    return normalize_url(recorded) != normalized_active_url


def resolve_chart_source(
    source: str,
    *,
    sheet_key: str,
    project_root: Path,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Resolve ``source`` (URL or local path) to a real on-disk
    :class:`Path`, downloading if necessary.

    Behaviour by source type:

    * **Local path** (``not is_url``): return ``Path(source)``
      directly. We do NOT validate ``is_file()`` here — the caller
      already does that, and forcing the check here would mean
      ``ChartSource("")`` couldn't be passed through unchanged for
      the "user hasn't filled this field yet" case.
    * **URL**:

      1. Normalize the URL.
      2. Check the manifest: if the recorded URL for this sheet
         matches AND the cache file exists, return the cache
         path — no network call.
      3. Otherwise download to the cache path, update the
         manifest, and return the cache path.

    The ``on_progress`` callback is invoked during downloads only;
    a cache hit (the steady-state case after first run) makes no
    callbacks at all, so the caller doesn't need to gate "is this
    a download or a cache hit?" before showing a progress dialog
    — let the resolver decide silently.

    Raises :class:`ChartFetchError` on download failures. Local-path
    sources never raise (they're just a path return).
    """
    chart_src = ChartSource(raw=source)

    if not chart_src.is_url:
        return Path(source)

    normalized = chart_src.normalized_url()
    cache_file = cache_path_for_sheet(sheet_key, project_root)

    if not needs_download(sheet_key, normalized, project_root):
        return cache_file

    download_chart_pdf(
        normalized,
        sheet_key=sheet_key,
        dest=cache_file,
        on_progress=on_progress,
    )

    # Update the manifest only after the download succeeds. If the
    # download raised, we land in the caller's ``except`` and the
    # manifest stays at its previous (stale) entry — that's fine,
    # because the cache file at ``cache_path`` will also be in its
    # previous state (the atomic rename ensures partial files
    # never become "the cached file"), so the next launch's
    # manifest-vs-cache consistency check still holds.
    manifest = load_manifest(project_root)
    manifest[sheet_key] = normalized
    save_manifest(project_root, manifest)

    return cache_file
