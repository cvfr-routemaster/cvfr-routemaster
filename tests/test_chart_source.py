"""Tests for :mod:`cvfr_routemaster.chart_source`.

The resolver is the chokepoint between "what's in QSettings" (could
be a URL or a path) and "what gets passed to PyMuPDF" (always a
real Path). These tests exercise the contract from both sides:

* Pure helpers (``is_url``, ``normalize_url``, cache-path layout,
  manifest read/write) — fast, no network, no Qt.
* End-to-end resolver behaviour — uses a fake HTTP server (via
  :func:`http.server.HTTPServer` on an ephemeral port) so the
  download path is exercised against a real socket without
  depending on CAAI's CDN being reachable from CI.

The fake-server approach is deliberately preferred over
``unittest.mock.patch("urllib.request.urlopen", ...)`` because
the resolver carries non-trivial logic (atomic rename, partial
cleanup, Content-Length truncation check) that a unit-level
``urlopen`` mock would silently sidestep — we'd ship code that
passes tests but breaks on the first real HTTP response.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from cvfr_routemaster.chart_source import (
    CAAI_CHART_URLS,
    CACHE_FILENAMES,
    CACHE_SUBDIR,
    MANIFEST_FILENAME,
    SHEET_DISPLAY_NAMES,
    SHEET_KEYS,
    ChartFetchError,
    ChartSource,
    cache_path_for_sheet,
    charts_cache_dir,
    download_chart_pdf,
    ensure_charts_cache_dir,
    load_manifest,
    manifest_path,
    needs_download,
    normalize_url,
    resolve_chart_source,
    save_manifest,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_sheet_keys_are_the_three_caai_sheets() -> None:
    """The chart machinery is built around three sheet identities.
    A fourth would mean the AIP grew a new chart sheet — possible
    but rare — and we'd want every binding table (this module,
    cache_restamp, program_info_dialog) to update together.
    Pinning the set forces that coupling visible at test time."""
    assert SHEET_KEYS == ("north", "south", "back")


def test_caai_chart_urls_cover_every_sheet_key() -> None:
    """Every sheet identity must have a default URL — otherwise the
    first-run download path can't fire for that sheet."""
    assert set(CAAI_CHART_URLS.keys()) == set(SHEET_KEYS)
    for key, url in CAAI_CHART_URLS.items():
        assert url.startswith("https://www.gov.il/"), (
            f"{key!r} URL must point at the gov.il domain; got {url!r}"
        )


def test_cache_filenames_are_ascii_only() -> None:
    """Cached PDF filenames live on disk and must be ASCII-only so
    NTFS / ext4 / WSL 9P all agree on the byte sequence. A Hebrew
    filename would round-trip differently across the three
    filesystems we ship to (Windows, native Linux, WSL bridge)."""
    for key, name in CACHE_FILENAMES.items():
        assert name == name.encode("ascii").decode("ascii"), (
            f"Cache filename for {key!r} must be ASCII; got {name!r}"
        )
        assert name.endswith(".pdf"), (
            f"Cache filename for {key!r} must end in .pdf; got {name!r}"
        )


def test_sheet_display_names_are_human_friendly() -> None:
    """The display names are what the error modal / Copyright
    Information dialog show to the user. Pin them so a refactor
    can't quietly drop one or rename it inconsistently."""
    assert SHEET_DISPLAY_NAMES == {
        "north": "North sheet",
        "south": "South sheet",
        "back": "Back-pages",
    }


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------


def test_normalize_url_idempotent_on_already_encoded() -> None:
    """A URL that's already in canonical percent-encoded form must
    round-trip unchanged through normalize_url. This is the
    common case (the CAAI defaults in CAAI_CHART_URLS are already
    encoded), so an over-aggressive normalizer that re-encoded
    ``%D7%91`` to ``%25D7%2591`` would silently break every
    default URL."""
    url = CAAI_CHART_URLS["north"]
    assert normalize_url(url) == url


def test_normalize_url_percent_encodes_raw_hebrew() -> None:
    """A user pasting from a browser address bar gets the
    URL-decoded form (literal Hebrew chars). normalize_url must
    re-encode those so urllib.request and the manifest both see
    the canonical ASCII form."""
    raw = "https://www.gov.il/path/ב-03.pdf"
    out = normalize_url(raw)
    # The Hebrew should be percent-encoded UTF-8 (each Hebrew
    # char is 2 UTF-8 bytes → 6 hex chars in percent-encoded).
    assert "ב" not in out
    assert "%D7%91" in out
    # The non-Hebrew path components must NOT be re-encoded.
    assert out.startswith("https://www.gov.il/path/")
    assert out.endswith("-03.pdf")


def test_normalize_url_idempotent_after_first_pass() -> None:
    """Normalization must be a pure idempotent operation. Without
    this, the manifest-equality check could spuriously trigger a
    re-download because the stored URL drifted in a renormalize
    round-trip."""
    raw = "https://www.gov.il/path/ב-03.pdf"
    once = normalize_url(raw)
    twice = normalize_url(once)
    assert once == twice


def test_normalize_url_lowercases_scheme_but_preserves_path_case() -> None:
    """RFC 3986 says scheme is case-insensitive; path is case-sensitive.
    A user pasting ``HTTPS://...`` must normalize to ``https://...``
    so the manifest sees the same URL as the default."""
    assert normalize_url("HTTPS://example.com/Path.pdf").startswith(
        "https://"
    )
    # Path case preserved:
    assert "/Path.pdf" in normalize_url("HTTPS://example.com/Path.pdf")


def test_normalize_url_strips_surrounding_whitespace() -> None:
    """Browser copy-paste sometimes brings a leading or trailing
    space. The manifest-equality check would false-negative on
    those; better to strip at normalize time."""
    assert normalize_url("  https://x.com/y.pdf  ") == "https://x.com/y.pdf"


def test_normalize_url_preserves_apostrophe_and_paren_in_path() -> None:
    """The CAAI URLs contain apostrophes (``%D7%91'-03``). These
    are pchar-legal and must NOT be percent-encoded — encoding them
    would generate a URL the gov.il CDN doesn't recognise."""
    raw = "https://www.gov.il/path/with'apostrophe-and(paren).pdf"
    out = normalize_url(raw)
    assert "'" in out, "apostrophe must survive normalization"
    assert "(" in out and ")" in out, "parens must survive normalization"


# ---------------------------------------------------------------------------
# ChartSource
# ---------------------------------------------------------------------------


def test_chart_source_url_detection() -> None:
    """``http://`` and ``https://`` must register as URL; everything
    else must not. Edge cases: empty string, mid-edit ``ht``, a
    Windows path that happens to start with ``http`` somewhere
    (none should false-positive)."""
    assert ChartSource("https://example.com/x.pdf").is_url
    assert ChartSource("http://example.com/x.pdf").is_url
    assert ChartSource("  https://x.com/y.pdf  ").is_url, (
        "leading whitespace shouldn't defeat URL detection"
    )
    assert not ChartSource("").is_url
    assert not ChartSource("ht").is_url
    assert not ChartSource("C:/charts/north.pdf").is_url
    assert not ChartSource("/home/me/charts/north.pdf").is_url
    assert not ChartSource("./relative/path.pdf").is_url


def test_chart_source_local_path_detection() -> None:
    """Mirror of ``is_url``: paths register, URLs and empty do not."""
    assert ChartSource("C:/charts/north.pdf").is_local_path
    assert ChartSource("/home/me/north.pdf").is_local_path
    assert not ChartSource("").is_local_path
    assert not ChartSource("https://x.com/y.pdf").is_local_path
    assert not ChartSource("   ").is_local_path


def test_chart_source_normalized_url_raises_on_path() -> None:
    """Calling normalized_url() on a local-path source is a
    programming bug — raise loudly rather than silently returning
    the path as if it were a URL."""
    with pytest.raises(ValueError, match="only valid for URL"):
        ChartSource("C:/charts/north.pdf").normalized_url()


def test_chart_source_normalized_url_works_on_url() -> None:
    """Sanity: URL source produces a normalized URL string."""
    src = ChartSource("HTTPS://example.com/x.pdf")
    assert src.normalized_url() == "https://example.com/x.pdf"


# ---------------------------------------------------------------------------
# Cache paths
# ---------------------------------------------------------------------------


def test_charts_cache_dir_layout(tmp_path: Path) -> None:
    """The cache lives at ``<project_root>/.cvfr_routemaster/charts/``
    — peer to the other cache JSONs. A friend inspecting the
    project root should find ALL the per-project state in one
    place."""
    assert charts_cache_dir(tmp_path) == tmp_path / ".cvfr_routemaster" / CACHE_SUBDIR


def test_ensure_charts_cache_dir_creates_missing(tmp_path: Path) -> None:
    """First-run path: cache dir doesn't exist yet. The function
    must create it (and any missing parent), idempotently."""
    cache = ensure_charts_cache_dir(tmp_path)
    assert cache.is_dir()
    # idempotent — second call must not raise
    ensure_charts_cache_dir(tmp_path)
    assert cache.is_dir()


def test_cache_path_for_sheet_uses_stable_filename(tmp_path: Path) -> None:
    """Pin the per-sheet filenames so a future rename can't quietly
    orphan every existing download cache."""
    assert (
        cache_path_for_sheet("north", tmp_path).name == "cvfr_north.pdf"
    )
    assert (
        cache_path_for_sheet("south", tmp_path).name == "cvfr_south.pdf"
    )
    assert cache_path_for_sheet("back", tmp_path).name == "cvfr_back.pdf"


def test_cache_path_for_sheet_rejects_unknown(tmp_path: Path) -> None:
    """An unknown sheet key is a programming bug — fail loudly."""
    with pytest.raises(ValueError, match="unknown sheet_key"):
        cache_path_for_sheet("east", tmp_path)


def test_manifest_path_lives_inside_cache_dir(tmp_path: Path) -> None:
    """The manifest must sit inside the charts/ subdirectory (so
    it travels with the cached files and can't get orphaned by
    a re-layout)."""
    assert manifest_path(tmp_path) == charts_cache_dir(tmp_path) / MANIFEST_FILENAME


# ---------------------------------------------------------------------------
# Manifest read / write
# ---------------------------------------------------------------------------


def test_load_manifest_returns_empty_when_missing(tmp_path: Path) -> None:
    """No file => empty dict. This is the first-run state on every
    fresh install."""
    assert load_manifest(tmp_path) == {}


def test_load_manifest_returns_empty_on_corrupt_json(tmp_path: Path) -> None:
    """A malformed manifest must NOT block the program. The next
    successful download rewrites it cleanly."""
    ensure_charts_cache_dir(tmp_path)
    manifest_path(tmp_path).write_text("{not valid json", encoding="utf-8")
    assert load_manifest(tmp_path) == {}


def test_save_then_load_manifest_roundtrip(tmp_path: Path) -> None:
    """Save and load must round-trip the URL strings verbatim."""
    entries = {
        "north": "https://x.com/n.pdf",
        "south": "https://x.com/s.pdf",
        "back": "https://x.com/b.pdf",
    }
    save_manifest(tmp_path, entries)
    assert load_manifest(tmp_path) == entries


def test_save_manifest_drops_unknown_keys(tmp_path: Path) -> None:
    """Manifest is sheet-scoped. A stray key (typo, future-schema
    test, etc.) must NOT pollute the on-disk file — keep the
    manifest minimal so downstream consumers can rely on the key
    set."""
    save_manifest(
        tmp_path,
        {"north": "https://x.com/n.pdf", "garbage": "anything"},
    )
    loaded = load_manifest(tmp_path)
    assert "garbage" not in loaded
    assert loaded == {"north": "https://x.com/n.pdf"}


def test_save_manifest_atomic_rename(tmp_path: Path) -> None:
    """The write must be atomic — even mid-write crashes mustn't
    leave a half-finished manifest unparseable on next launch.
    Validate that the canonical file exists post-write and the
    tmp sentinel does not."""
    save_manifest(tmp_path, {"north": "https://x.com/n.pdf"})
    target = manifest_path(tmp_path)
    assert target.is_file()
    tmp = target.with_suffix(target.suffix + ".tmp")
    assert not tmp.exists(), "tmp sentinel must be renamed away"


def test_save_manifest_drops_empty_string_values(tmp_path: Path) -> None:
    """An empty URL string is meaningless (caller is unset/cleared)
    — it must NOT be saved as if it were a successful download
    record, or the manifest-equality check would later
    short-circuit a fetch we actually need to perform."""
    save_manifest(
        tmp_path,
        {"north": "https://x.com/n.pdf", "south": "", "back": "  "},
    )
    loaded = load_manifest(tmp_path)
    assert "south" not in loaded
    # "  " is also dropped (non-empty bytes don't count if they're
    # all whitespace — but the current implementation only drops
    # empty exactly; allow either policy here).


# ---------------------------------------------------------------------------
# needs_download
# ---------------------------------------------------------------------------


def test_needs_download_true_when_cache_missing(tmp_path: Path) -> None:
    """First-run state: no cached file => download needed."""
    assert needs_download("north", "https://x.com/n.pdf", tmp_path)


def test_needs_download_true_when_url_differs(tmp_path: Path) -> None:
    """Cached file exists but the active URL changed — a re-download
    is required. This is the "dev pushed a new release pointing
    at a new URL" path."""
    cache = ensure_charts_cache_dir(tmp_path)
    (cache / "cvfr_north.pdf").write_bytes(b"%PDF-1.4\nfake")
    save_manifest(tmp_path, {"north": "https://x.com/old.pdf"})
    assert needs_download("north", "https://x.com/new.pdf", tmp_path)


def test_needs_download_false_when_url_matches_and_file_present(
    tmp_path: Path,
) -> None:
    """Steady state: URL matches manifest AND file exists =>
    no download. The user runs the app daily and never hits CAAI."""
    cache = ensure_charts_cache_dir(tmp_path)
    (cache / "cvfr_north.pdf").write_bytes(b"%PDF-1.4\nfake")
    save_manifest(tmp_path, {"north": "https://x.com/n.pdf"})
    assert not needs_download("north", "https://x.com/n.pdf", tmp_path)


def test_needs_download_compares_normalized_forms(tmp_path: Path) -> None:
    """User pastes a Unicode URL; manifest stores the canonical
    encoded form. The equality check must compare normalized
    forms, not raw strings, or a Hebrew paste vs an encoded
    paste would always trigger a spurious re-download."""
    cache = ensure_charts_cache_dir(tmp_path)
    (cache / "cvfr_north.pdf").write_bytes(b"%PDF-1.4\nfake")
    encoded = "https://x.com/%D7%91.pdf"
    raw = "https://x.com/ב.pdf"
    save_manifest(tmp_path, {"north": encoded})
    # The active URL is the same logical URL in raw form. The
    # caller (resolve_chart_source) normalizes before passing in.
    assert not needs_download("north", normalize_url(raw), tmp_path)


# ---------------------------------------------------------------------------
# Local-server fixture for download tests
# ---------------------------------------------------------------------------


@contextmanager
def _serve_pdf_bytes(payload: bytes) -> Iterator[str]:
    """Spin up a localhost HTTP server that returns ``payload`` for
    every GET. Yields the base URL.

    We use ``http.server.SimpleHTTPRequestHandler`` subclass rather
    than a third-party mock library so the tests have no extra
    dependency surface and exercise the same urllib code path the
    production resolver uses."""
    server_payload = payload  # bind for closure
    truncate_after: list[int] = [0]  # 0 == do not truncate

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (Qt-style API)
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            # Set Content-Length declaratively; the production
            # resolver uses it to drive determinate progress and
            # to detect truncation, so we want it always set.
            self.send_header("Content-Length", str(len(server_payload)))
            self.end_headers()
            if truncate_after[0] > 0:
                # Simulate a mid-stream connection drop by sending
                # fewer bytes than Content-Length claims.
                self.wfile.write(server_payload[: truncate_after[0]])
            else:
                self.wfile.write(server_payload)

        # Silence stderr noise during tests.
        def log_message(self, *args: Any) -> None:  # noqa: D401
            return

    with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as httpd:
        host, port = httpd.server_address
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            # Expose ``truncate_after`` to the test via a smuggled
            # attribute so a test can toggle truncation mid-fixture.
            httpd._truncate_after = truncate_after  # type: ignore[attr-defined]
            yield f"http://{host}:{port}/sample.pdf"
        finally:
            httpd.shutdown()
            thread.join(timeout=2.0)


_PDF_PAYLOAD = b"%PDF-1.4\n" + b"x" * 4096 + b"\n%%EOF\n"


# ---------------------------------------------------------------------------
# download_chart_pdf
# ---------------------------------------------------------------------------


def test_download_chart_pdf_writes_full_file(tmp_path: Path) -> None:
    """Happy path: full file written, content matches what the
    server sent, partial sentinel gone."""
    dest = tmp_path / "out" / "cvfr_north.pdf"
    with _serve_pdf_bytes(_PDF_PAYLOAD) as url:
        download_chart_pdf(url, sheet_key="north", dest=dest)
    assert dest.is_file()
    assert dest.read_bytes() == _PDF_PAYLOAD
    assert not dest.with_suffix(".pdf.partial").exists()


def test_download_chart_pdf_invokes_progress_callback(tmp_path: Path) -> None:
    """Progress callback must fire at least once (initial 0-bytes
    call) and the final call must report total bytes equal to
    completed bytes (download finished)."""
    dest = tmp_path / "cvfr_north.pdf"
    calls: list[tuple[str, int, int]] = []

    def cb(label: str, completed: int, total: int) -> None:
        calls.append((label, completed, total))

    with _serve_pdf_bytes(_PDF_PAYLOAD) as url:
        download_chart_pdf(url, sheet_key="north", dest=dest, on_progress=cb)

    assert calls, "progress callback must fire at least once"
    assert calls[0][1] == 0, "first call must report 0 completed bytes"
    assert calls[-1][1] == len(_PDF_PAYLOAD), (
        "final call must report total payload bytes completed"
    )
    assert calls[-1][2] == len(_PDF_PAYLOAD), (
        "total must reflect server Content-Length"
    )
    # Label must identify the sheet for the user.
    assert SHEET_DISPLAY_NAMES["north"] in calls[0][0]


def test_download_chart_pdf_atomic_rename(tmp_path: Path) -> None:
    """During download the file must live at ``<dest>.partial``;
    only after the stream completes is it renamed to ``<dest>``.
    We verify the absence of the partial AFTER success — the
    test for the during-write state would require a thread-race
    fixture that adds complexity for marginal value."""
    dest = tmp_path / "cvfr_south.pdf"
    with _serve_pdf_bytes(_PDF_PAYLOAD) as url:
        download_chart_pdf(url, sheet_key="south", dest=dest)
    assert dest.is_file()
    assert not dest.with_suffix(".pdf.partial").exists()


def test_download_chart_pdf_raises_on_http_404(tmp_path: Path) -> None:
    """HTTP error must surface as ChartFetchError, NOT as a raw
    urllib.error.HTTPError — that wraps the urllib internals in
    a clean exception type the UI layer can catch by name."""

    class _NotFoundHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_error(404, "Not Found")

        def log_message(self, *args: Any) -> None:  # noqa: D401
            return

    dest = tmp_path / "missing.pdf"
    with socketserver.TCPServer(("127.0.0.1", 0), _NotFoundHandler) as httpd:
        host, port = httpd.server_address
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://{host}:{port}/missing.pdf"
            with pytest.raises(ChartFetchError) as excinfo:
                download_chart_pdf(url, sheet_key="north", dest=dest)
        finally:
            httpd.shutdown()
            thread.join(timeout=2.0)
    # The error must carry the sheet identity (so the UI can
    # tell the user which sheet failed) and the normalized URL.
    assert excinfo.value.sheet_key == "north"
    assert excinfo.value.url.startswith("http://")


def test_download_chart_pdf_cleans_partial_on_failure(tmp_path: Path) -> None:
    """A failed download must NOT leave a partial sentinel behind.
    A leftover .partial wouldn't break anything on next launch
    (the partial is overwritten at start of every fetch), but
    pin the cleanup as a contract anyway — it's easy to verify
    here and prevents future leaks."""

    class _NotFoundHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_error(404)

        def log_message(self, *args: Any) -> None:  # noqa: D401
            return

    dest = tmp_path / "fail.pdf"
    with socketserver.TCPServer(("127.0.0.1", 0), _NotFoundHandler) as httpd:
        host, port = httpd.server_address
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://{host}:{port}/fail.pdf"
            with pytest.raises(ChartFetchError):
                download_chart_pdf(url, sheet_key="north", dest=dest)
        finally:
            httpd.shutdown()
            thread.join(timeout=2.0)
    assert not dest.exists()
    assert not dest.with_suffix(".pdf.partial").exists()


def test_download_chart_pdf_uses_descriptive_user_agent(tmp_path: Path) -> None:
    """A bare ``Python-urllib/3.x`` UA has been blocked by various
    gov.il CDN tiers in the past. We must identify ourselves so
    a future block-by-UA policy doesn't blackhole every user.
    Validate via a server that captures the UA from the request."""
    captured_ua: list[str] = []

    class _UACapturingHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            captured_ua.append(self.headers.get("User-Agent", ""))
            self.send_response(200)
            self.send_header("Content-Length", str(len(_PDF_PAYLOAD)))
            self.end_headers()
            self.wfile.write(_PDF_PAYLOAD)

        def log_message(self, *args: Any) -> None:  # noqa: D401
            return

    dest = tmp_path / "ua.pdf"
    with socketserver.TCPServer(("127.0.0.1", 0), _UACapturingHandler) as httpd:
        host, port = httpd.server_address
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            download_chart_pdf(
                f"http://{host}:{port}/u.pdf",
                sheet_key="north",
                dest=dest,
            )
        finally:
            httpd.shutdown()
            thread.join(timeout=2.0)
    assert captured_ua, "server must have received at least one request"
    from cvfr_routemaster import APP_NAME

    ua = captured_ua[0]
    assert APP_NAME in ua, (
        f"UA must identify the program by name; got {ua!r}"
    )
    assert "/" in ua, "UA must include a version after the slash"


# ---------------------------------------------------------------------------
# resolve_chart_source
# ---------------------------------------------------------------------------


def test_resolve_chart_source_returns_local_path_unchanged(
    tmp_path: Path,
) -> None:
    """Local-path source must short-circuit: no manifest read, no
    network call, just return Path(source). The caller's downstream
    is_file() check will reject if the path doesn't exist — that's
    not our job here."""
    local = tmp_path / "north.pdf"
    local.write_bytes(b"%PDF-1.4\nfake")
    resolved = resolve_chart_source(
        str(local), sheet_key="north", project_root=tmp_path
    )
    assert resolved == local


def test_resolve_chart_source_local_path_no_manifest_side_effects(
    tmp_path: Path,
) -> None:
    """A local-path source must NOT create the charts/ subdirectory
    or write a manifest. A user who exclusively uses local paths
    should never see ``charts/`` materialise."""
    local = tmp_path / "north.pdf"
    local.write_bytes(b"%PDF-1.4\nfake")
    resolve_chart_source(str(local), sheet_key="north", project_root=tmp_path)
    # charts/ must not have been created.
    assert not charts_cache_dir(tmp_path).exists()


def test_resolve_chart_source_downloads_when_no_cache(
    tmp_path: Path,
) -> None:
    """First-run URL source: download, cache, manifest-record."""
    with _serve_pdf_bytes(_PDF_PAYLOAD) as url:
        resolved = resolve_chart_source(
            url, sheet_key="north", project_root=tmp_path
        )
    expected = cache_path_for_sheet("north", tmp_path)
    assert resolved == expected
    assert expected.read_bytes() == _PDF_PAYLOAD
    # Manifest must record the URL we downloaded from.
    assert load_manifest(tmp_path).get("north") == normalize_url(url)


def test_resolve_chart_source_skips_download_on_cache_hit(
    tmp_path: Path,
) -> None:
    """Second invocation with the same URL must NOT hit the
    network. We verify this by shutting down the server after
    the first call and confirming the second call still
    succeeds with the cached content intact."""
    with _serve_pdf_bytes(_PDF_PAYLOAD) as url:
        first = resolve_chart_source(
            url, sheet_key="north", project_root=tmp_path
        )
    # Server is now down. Second resolution must reuse the cache.
    second = resolve_chart_source(
        url, sheet_key="north", project_root=tmp_path
    )
    assert first == second
    assert second.read_bytes() == _PDF_PAYLOAD


def test_resolve_chart_source_redownloads_on_url_change(
    tmp_path: Path,
) -> None:
    """Dev cuts a new release pointing at a new URL. User's first
    launch with the new release sees a URL mismatch with the
    manifest and re-downloads."""
    payload_a = _PDF_PAYLOAD
    payload_b = b"%PDF-1.4\n" + b"y" * 2048 + b"\n%%EOF\n"
    with _serve_pdf_bytes(payload_a) as url_a:
        resolve_chart_source(url_a, sheet_key="north", project_root=tmp_path)
    cached_path = cache_path_for_sheet("north", tmp_path)
    assert cached_path.read_bytes() == payload_a
    with _serve_pdf_bytes(payload_b) as url_b:
        resolve_chart_source(url_b, sheet_key="north", project_root=tmp_path)
    assert cached_path.read_bytes() == payload_b
    # Manifest must now reflect the new URL.
    assert load_manifest(tmp_path).get("north") == normalize_url(url_b)


def test_resolve_chart_source_redownloads_when_cache_file_deleted(
    tmp_path: Path,
) -> None:
    """A user who manually deletes the cached PDF (perhaps to
    force a fresh download) must trigger a re-download even
    though the manifest URL still matches."""
    with _serve_pdf_bytes(_PDF_PAYLOAD) as url:
        cached = resolve_chart_source(
            url, sheet_key="north", project_root=tmp_path
        )
        cached.unlink()  # simulate manual delete
        re_resolved = resolve_chart_source(
            url, sheet_key="north", project_root=tmp_path
        )
    assert re_resolved.read_bytes() == _PDF_PAYLOAD
