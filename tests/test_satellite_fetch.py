"""Tests for :mod:`cvfr_routemaster.satellite_fetch`.

No real network — every HTTP call is mocked through
``urllib.request.urlopen``. No Qt either; the fetch + state code is
plain stdlib.

Coverage targets:

1. **fetch_tile** — every :class:`FetchOutcome` shape produced from
   the matching server response (200 / 200-too-small / 404 / 410 /
   429 / 500 / 503 / arbitrary 4xx / URLError / TimeoutError /
   OSError). User-Agent is actually sent. URL substitution uses the
   provided template.
2. **fetch_and_cache_tile** — only writes to the cache on
   ``fetched``; leaves cache untouched on every other outcome.
3. **tiles_to_fetch_for_bbox** — preserves bbox enumeration order,
   subtracts cached tiles, returns empty list when fully cached.
4. **DownloadState** dataclass + read/write — round-trip identity,
   atomic write semantics, version mismatch returns ``None``,
   corrupt JSON returns ``None``, ``is_complete`` invariant.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cvfr_routemaster.satellite_fetch import (
    DEFAULT_RATE_LIMIT_BACKOFF_S,
    DOWNLOAD_STATE_FILENAME,
    DOWNLOAD_STATE_VERSION,
    FETCH_OUTCOME_KINDS,
    MIN_TILE_BYTES,
    DownloadState,
    FetchOutcome,
    fetch_and_cache_tile,
    fetch_tile,
    read_download_state,
    count_cached_tiles_in_bbox,
    tiles_to_fetch_for_bbox,
    write_download_state,
)
from cvfr_routemaster.satellite_tiles import (
    ESRI_WORLD_IMAGERY_TEMPLATE,
    USER_AGENT,
    TileCache,
    TileCoord,
    bbox_to_tiles,
)
import urllib.error


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


JPEG_MAGIC = b"\xff\xd8\xff\xe0\x00\x10JFIF"


def _ok_response(content: bytes):
    """Build a fake ``urlopen`` return that supports the context-
    manager interface and ``.read()`` like the real one."""
    resp = MagicMock()
    resp.read.return_value = content
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *_: False
    return resp


def _http_error(
    status: int,
    reason: str = "",
    headers: dict[str, str] | None = None,
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://example/test",
        code=status,
        msg=reason,
        hdrs=headers or {},  # type: ignore[arg-type]
        fp=io.BytesIO(b""),
    )


def _make_jpeg_bytes(size: int) -> bytes:
    """Synthesise a 'JPEG' of the requested size by padding the
    magic bytes — fetch_tile only checks length, not internal
    structure."""
    if size <= len(JPEG_MAGIC):
        return JPEG_MAGIC[:size]
    return JPEG_MAGIC + b"\x00" * (size - len(JPEG_MAGIC))


# ---------------------------------------------------------------------------
# FetchOutcome constructors
# ---------------------------------------------------------------------------


class TestFetchOutcomeConstructors:
    """The classmethod constructors enforce invariants we rely on."""

    def test_fetched_kinds_pinned(self) -> None:
        # Pin the discriminator set so callers can ``match`` on it.
        assert "fetched" in FETCH_OUTCOME_KINDS
        assert "missing" in FETCH_OUTCOME_KINDS
        assert "rate_limited" in FETCH_OUTCOME_KINDS
        assert "network_error" in FETCH_OUTCOME_KINDS
        assert "http_error" in FETCH_OUTCOME_KINDS

    def test_fetched_carries_content(self) -> None:
        out = FetchOutcome.fetched(b"some-bytes")
        assert out.kind == "fetched"
        assert out.content == b"some-bytes"
        assert out.status == 200

    def test_fetched_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            FetchOutcome.fetched(b"")

    def test_missing_carries_reason(self) -> None:
        out = FetchOutcome.missing(reason="no imagery", status=404)
        assert out.kind == "missing"
        assert out.content is None
        assert out.reason == "no imagery"
        assert out.status == 404

    def test_rate_limited_default_backoff(self) -> None:
        out = FetchOutcome.rate_limited()
        assert out.kind == "rate_limited"
        assert out.retry_after_seconds == DEFAULT_RATE_LIMIT_BACKOFF_S

    def test_rate_limited_uses_provided_backoff(self) -> None:
        out = FetchOutcome.rate_limited(retry_after_seconds=12.5)
        assert out.retry_after_seconds == 12.5

    def test_rate_limited_zero_falls_back_to_default(self) -> None:
        # 0 is a degenerate value (no actual backoff) and we'd
        # rather fall through to the safe default than spin.
        out = FetchOutcome.rate_limited(retry_after_seconds=0.0)
        assert out.retry_after_seconds == DEFAULT_RATE_LIMIT_BACKOFF_S

    def test_network_error_carries_reason(self) -> None:
        out = FetchOutcome.network_error(reason="DNS hit the floor")
        assert out.kind == "network_error"
        assert "DNS" in (out.reason or "")

    def test_http_error_carries_status(self) -> None:
        out = FetchOutcome.http_error(status=403, reason="forbidden")
        assert out.kind == "http_error"
        assert out.status == 403


# ---------------------------------------------------------------------------
# fetch_tile
# ---------------------------------------------------------------------------


class TestFetchTile:
    """Single-tile HTTP behaviour, every outcome path."""

    coord = TileCoord(z=14, x=9779, y=6652)

    def test_fetched_path_returns_content(self) -> None:
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen"
        ) as mock_urlopen:
            mock_urlopen.return_value = _ok_response(_make_jpeg_bytes(2000))
            out = fetch_tile(self.coord)
        assert out.kind == "fetched"
        assert out.content is not None
        assert len(out.content) == 2000

    def test_user_agent_actually_sent(self) -> None:
        captured: dict[str, str] = {}

        def fake_urlopen(req, timeout):
            captured["ua"] = req.get_header("User-agent")
            captured["url"] = req.get_full_url()
            return _ok_response(_make_jpeg_bytes(2000))

        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            fetch_tile(self.coord)

        assert captured["ua"] == USER_AGENT
        # Esri URL substitution: y comes BEFORE x.
        assert captured["url"].endswith("/14/6652/9779")

    def test_custom_template_substitutes(self) -> None:
        captured: dict[str, str] = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.get_full_url()
            return _ok_response(_make_jpeg_bytes(2000))

        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            fetch_tile(
                self.coord,
                template="https://example/{z}/{x}/{y}.png",
            )
        assert captured["url"] == "https://example/14/9779/6652.png"

    def test_404_is_missing(self) -> None:
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=_http_error(404, "Not Found"),
        ):
            out = fetch_tile(self.coord)
        assert out.kind == "missing"
        assert out.status == 404

    def test_410_is_missing(self) -> None:
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=_http_error(410, "Gone"),
        ):
            out = fetch_tile(self.coord)
        assert out.kind == "missing"
        assert out.status == 410

    def test_429_with_retry_after(self) -> None:
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=_http_error(
                429, "Too Many", headers={"Retry-After": "42"}
            ),
        ):
            out = fetch_tile(self.coord)
        assert out.kind == "rate_limited"
        assert out.retry_after_seconds == 42.0

    def test_429_without_retry_after_uses_default(self) -> None:
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=_http_error(429, "Too Many"),
        ):
            out = fetch_tile(self.coord)
        assert out.kind == "rate_limited"
        assert out.retry_after_seconds == DEFAULT_RATE_LIMIT_BACKOFF_S

    def test_500_is_network_error(self) -> None:
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=_http_error(500, "Server Error"),
        ):
            out = fetch_tile(self.coord)
        assert out.kind == "network_error"
        assert out.status == 500

    def test_503_is_network_error(self) -> None:
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=_http_error(503, "Unavailable"),
        ):
            out = fetch_tile(self.coord)
        assert out.kind == "network_error"
        assert out.status == 503

    def test_403_is_http_error(self) -> None:
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=_http_error(403, "Forbidden"),
        ):
            out = fetch_tile(self.coord)
        assert out.kind == "http_error"
        assert out.status == 403

    def test_url_error_is_network_error(self) -> None:
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=urllib.error.URLError("DNS lookup failed"),
        ):
            out = fetch_tile(self.coord)
        assert out.kind == "network_error"
        assert "DNS" in (out.reason or "")

    def test_timeout_is_network_error(self) -> None:
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=TimeoutError("read timeout"),
        ):
            out = fetch_tile(self.coord)
        assert out.kind == "network_error"
        assert out.reason == "timeout"

    def test_oserror_is_network_error(self) -> None:
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=OSError("connection reset"),
        ):
            out = fetch_tile(self.coord)
        assert out.kind == "network_error"

    def test_short_body_treated_as_missing(self) -> None:
        # 200 response but 0-50 bytes → almost certainly an error
        # page; not a real tile. Covers Esri's "200 + JSON-error
        # body" case.
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen"
        ) as mock_urlopen:
            mock_urlopen.return_value = _ok_response(
                _make_jpeg_bytes(MIN_TILE_BYTES - 1)
            )
            out = fetch_tile(self.coord)
        assert out.kind == "missing"
        assert out.status == 200

    def test_read_failure_is_network_error(self) -> None:
        # Connection drops mid-body-read.
        resp = MagicMock()
        resp.read.side_effect = ConnectionResetError("boom")
        resp.__enter__ = lambda self: self
        resp.__exit__ = lambda self, *_: False
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            return_value=resp,
        ):
            out = fetch_tile(self.coord)
        assert out.kind == "network_error"
        assert "read failed" in (out.reason or "")


# ---------------------------------------------------------------------------
# fetch_and_cache_tile
# ---------------------------------------------------------------------------


class TestFetchAndCacheTile:
    """Composition: fetch + cache.put on success only."""

    coord = TileCoord(z=14, x=1234, y=5678)

    def test_fetched_writes_to_cache(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        body = _make_jpeg_bytes(1024)
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen"
        ) as mock_urlopen:
            mock_urlopen.return_value = _ok_response(body)
            out = fetch_and_cache_tile(self.coord, cache)
        assert out.kind == "fetched"
        assert cache.has(self.coord)
        assert cache.get(self.coord) == body

    def test_missing_does_not_write(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=_http_error(404, "Not Found"),
        ):
            out = fetch_and_cache_tile(self.coord, cache)
        assert out.kind == "missing"
        assert not cache.has(self.coord)

    def test_rate_limited_does_not_write(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=_http_error(429, "Too Many"),
        ):
            fetch_and_cache_tile(self.coord, cache)
        assert not cache.has(self.coord)

    def test_network_error_does_not_write(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        with patch(
            "cvfr_routemaster.satellite_fetch.urllib.request.urlopen",
            side_effect=urllib.error.URLError("oops"),
        ):
            fetch_and_cache_tile(self.coord, cache)
        assert not cache.has(self.coord)


# ---------------------------------------------------------------------------
# tiles_to_fetch_for_bbox
# ---------------------------------------------------------------------------


class TestTilesToFetchForBbox:
    """Bulk-fetch enumerator filters out already-cached tiles."""

    def test_empty_cache_returns_full_bbox(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        full = bbox_to_tiles(31.0, 32.0, 35.0, 35.5, 12)
        todo = tiles_to_fetch_for_bbox(31.0, 32.0, 35.0, 35.5, 12, cache)
        assert todo == full

    def test_fully_cached_returns_empty(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        for c in bbox_to_tiles(31.0, 31.5, 35.0, 35.2, 12):
            cache.put(c, _make_jpeg_bytes(1024))
        todo = tiles_to_fetch_for_bbox(31.0, 31.5, 35.0, 35.2, 12, cache)
        assert todo == []

    def test_partially_cached(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        full = bbox_to_tiles(31.0, 31.5, 35.0, 35.2, 12)
        # Cache the first half.
        cached = full[: len(full) // 2]
        for c in cached:
            cache.put(c, _make_jpeg_bytes(1024))
        todo = tiles_to_fetch_for_bbox(31.0, 31.5, 35.0, 35.2, 12, cache)
        assert set(todo) == set(full) - set(cached)

    def test_preserves_row_major_order(self, tmp_path: Path) -> None:
        # Even with arbitrary cached tiles missing, surviving order
        # should still be row-major (y monotone non-decreasing).
        cache = TileCache(tmp_path, provider="esri")
        full = bbox_to_tiles(31.0, 32.0, 35.0, 36.0, 12)
        # Cache every 3rd tile so the remainder is interleaved.
        for c in full[::3]:
            cache.put(c, _make_jpeg_bytes(1024))
        todo = tiles_to_fetch_for_bbox(31.0, 32.0, 35.0, 36.0, 12, cache)
        ys = [t.y for t in todo]
        assert ys == sorted(ys)


# ---------------------------------------------------------------------------
# count_cached_tiles_in_bbox
# ---------------------------------------------------------------------------


class TestCountCachedTilesInBbox:
    """Fast scandir-based per-zoom cache count.

    Performance contract: this is the helper the GUI's startup-time
    progress seed uses to populate the status-bar label without
    blocking. The previous implementation called
    :meth:`TileCache.has` (one ``stat`` syscall) per candidate
    tile, which on the four-zoom default freezes the GUI thread
    for 5-30 s on Windows NTFS — the "Not Responding" window the
    user reported on launch with satellite view on from a prior
    session. The fast helper walks each x-subdirectory once via
    :func:`os.scandir`, cutting syscalls from O(tiles) to
    O(x-dirs) (a 10-50x speedup measured on a live cache, see
    the docstring's perf note).

    Correctness contract: must agree with
    ``count_tiles_for_bbox - len(tiles_to_fetch_for_bbox)`` for
    every bbox / zoom combination — the seed formula is
    ``completed = cached_in_bbox`` and ``done = (cached == total)``,
    so a single-tile discrepancy would mis-render the status bar.
    """

    def test_empty_cache_returns_zero(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        n = count_cached_tiles_in_bbox(
            min_lat=31.0,
            max_lat=32.0,
            min_lon=35.0,
            max_lon=35.5,
            z=12,
            cache=cache,
        )
        assert n == 0

    def test_fully_cached_matches_bbox_total(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        full = bbox_to_tiles(31.0, 31.5, 35.0, 35.2, 12)
        for c in full:
            cache.put(c, _make_jpeg_bytes(1024))
        n = count_cached_tiles_in_bbox(
            min_lat=31.0,
            max_lat=31.5,
            min_lon=35.0,
            max_lon=35.2,
            z=12,
            cache=cache,
        )
        assert n == len(full)

    def test_partially_cached_count(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        full = bbox_to_tiles(31.0, 31.5, 35.0, 35.2, 12)
        cached = full[: len(full) // 2]
        for c in cached:
            cache.put(c, _make_jpeg_bytes(1024))
        n = count_cached_tiles_in_bbox(
            min_lat=31.0,
            max_lat=31.5,
            min_lon=35.0,
            max_lon=35.2,
            z=12,
            cache=cache,
        )
        assert n == len(cached)

    def test_ignores_out_of_bbox_tiles(self, tmp_path: Path) -> None:
        """Tiles cached at zoom ``z`` but whose ``(x, y)`` falls
        outside the queried bbox must not be counted. Common
        case: user shrinks the chart bbox between releases; the
        old wider-bbox tiles linger but shouldn't inflate the
        new bbox's progress count.
        """
        cache = TileCache(tmp_path, provider="esri")
        in_bbox = bbox_to_tiles(31.0, 31.3, 35.0, 35.2, 12)
        out_of_bbox = bbox_to_tiles(33.5, 33.7, 35.5, 35.7, 12)
        for c in in_bbox + out_of_bbox:
            cache.put(c, _make_jpeg_bytes(1024))
        n = count_cached_tiles_in_bbox(
            min_lat=31.0,
            max_lat=31.3,
            min_lon=35.0,
            max_lon=35.2,
            z=12,
            cache=cache,
        )
        assert n == len(in_bbox)

    def test_ignores_other_zoom_levels(self, tmp_path: Path) -> None:
        """A tile cached at z=11 whose ``(x, y)`` coincidentally
        falls in the z=12 bbox range must not be counted — the
        helper is per-zoom and the on-disk layout segregates
        zooms into separate subtrees.
        """
        cache = TileCache(tmp_path, provider="esri")
        z12_tiles = bbox_to_tiles(31.0, 31.3, 35.0, 35.2, 12)
        z11_tiles = bbox_to_tiles(31.0, 31.3, 35.0, 35.2, 11)
        for c in z11_tiles:
            cache.put(c, _make_jpeg_bytes(1024))
        # Only z=11 cached; query z=12 → must be 0.
        n = count_cached_tiles_in_bbox(
            min_lat=31.0,
            max_lat=31.3,
            min_lon=35.0,
            max_lon=35.2,
            z=12,
            cache=cache,
        )
        assert n == 0
        # Sanity: cache the z=12 ones too → must equal that bbox total.
        for c in z12_tiles:
            cache.put(c, _make_jpeg_bytes(1024))
        n2 = count_cached_tiles_in_bbox(
            min_lat=31.0,
            max_lat=31.3,
            min_lon=35.0,
            max_lon=35.2,
            z=12,
            cache=cache,
        )
        assert n2 == len(z12_tiles)

    def test_degenerate_bbox_returns_zero(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        full = bbox_to_tiles(31.0, 31.5, 35.0, 35.2, 12)
        for c in full:
            cache.put(c, _make_jpeg_bytes(1024))
        # Swapped lat order: degenerate, must return 0 without raising.
        n = count_cached_tiles_in_bbox(
            min_lat=32.0,
            max_lat=31.0,
            min_lon=35.0,
            max_lon=35.5,
            z=12,
            cache=cache,
        )
        assert n == 0

    def test_missing_provider_dir_returns_zero(
        self, tmp_path: Path
    ) -> None:
        """The provider subtree may not exist yet on a fresh install.
        Returning 0 (rather than raising) lets the seed treat a
        never-downloaded zoom as "0/total" without a try-except
        around every per-zoom call.
        """
        cache = TileCache(tmp_path, provider="esri")
        # No ``put`` ever, so the provider subtree doesn't exist.
        assert not cache.provider_root().exists()
        n = count_cached_tiles_in_bbox(
            min_lat=31.0,
            max_lat=31.5,
            min_lon=35.0,
            max_lon=35.2,
            z=12,
            cache=cache,
        )
        assert n == 0

    def test_agrees_with_slow_path_on_partial_cache(
        self, tmp_path: Path
    ) -> None:
        """The whole point of the fast helper is to return the
        same count as
        ``count_tiles_for_bbox - len(tiles_to_fetch_for_bbox)``
        in every scenario — the seed reads the difference as
        ``completed`` and a single-tile drift would flip the
        ``done`` flag and either over-report or under-report
        progress.
        """
        from cvfr_routemaster.satellite_tiles import (
            count_tiles_for_bbox,
        )

        cache = TileCache(tmp_path, provider="esri")
        full = bbox_to_tiles(31.0, 31.5, 35.0, 35.2, 12)
        # Cache every other tile, in a deliberately non-contiguous
        # pattern to exercise mixed-membership per-x-subdirectory.
        for c in full[::2]:
            cache.put(c, _make_jpeg_bytes(1024))
        total = count_tiles_for_bbox(
            min_lat=31.0,
            max_lat=31.5,
            min_lon=35.0,
            max_lon=35.2,
            z=12,
        )
        slow_missing = tiles_to_fetch_for_bbox(
            min_lat=31.0,
            max_lat=31.5,
            min_lon=35.0,
            max_lon=35.2,
            z=12,
            cache=cache,
        )
        fast_cached = count_cached_tiles_in_bbox(
            min_lat=31.0,
            max_lat=31.5,
            min_lon=35.0,
            max_lon=35.2,
            z=12,
            cache=cache,
        )
        assert fast_cached == total - len(slow_missing)


# ---------------------------------------------------------------------------
# TileCache I/O extensions
# ---------------------------------------------------------------------------


class TestTileCacheGetPut:
    """The new I/O methods added to TileCache for Phase 2."""

    def test_put_then_get_round_trip(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        c = TileCoord(z=14, x=1, y=2)
        body = _make_jpeg_bytes(2048)
        cache.put(c, body)
        assert cache.has(c)
        assert cache.get(c) == body

    def test_get_returns_none_on_miss(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        assert cache.get(TileCoord(z=14, x=1, y=2)) is None

    def test_put_creates_parent_dirs(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        c = TileCoord(z=14, x=99, y=99)
        # Cache root doesn't exist yet.
        assert not (tmp_path / "esri").exists()
        cache.put(c, _make_jpeg_bytes(1024))
        # mkdir(parents=True) should have created everything.
        assert cache.path_for(c).exists()

    def test_put_atomic_via_tmp_rename(self, tmp_path: Path) -> None:
        # After a successful put, no .tmp file should remain.
        cache = TileCache(tmp_path, provider="esri")
        c = TileCoord(z=14, x=1, y=2)
        cache.put(c, _make_jpeg_bytes(1024))
        # No leftover .tmp.
        for p in (tmp_path / "esri").rglob("*.tmp"):
            pytest.fail(f"leaked tmp file: {p}")

    def test_put_rejects_empty_content(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        with pytest.raises(ValueError):
            cache.put(TileCoord(z=14, x=1, y=2), b"")

    def test_put_overwrites_existing(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        c = TileCoord(z=14, x=1, y=2)
        cache.put(c, _make_jpeg_bytes(1024))
        cache.put(c, _make_jpeg_bytes(2048))
        got = cache.get(c)
        assert got is not None
        assert len(got) == 2048


class TestTileCacheSizeAndEvict:
    """size_bytes + evict_lru."""

    def test_size_zero_for_missing_root(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path / "absent", provider="esri")
        assert cache.size_bytes() == 0

    def test_size_sums_all_tiles(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        cache.put(TileCoord(z=14, x=1, y=1), _make_jpeg_bytes(1000))
        cache.put(TileCoord(z=14, x=1, y=2), _make_jpeg_bytes(2000))
        cache.put(TileCoord(z=15, x=1, y=1), _make_jpeg_bytes(3000))
        assert cache.size_bytes() == 6000

    def test_size_ignores_tmp_and_state_files(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        cache.put(TileCoord(z=14, x=1, y=1), _make_jpeg_bytes(1000))
        # Drop a .tmp file (simulating an interrupted write) and a
        # state JSON. Neither should count.
        provider_dir = cache.provider_root()
        (provider_dir / "interrupted.jpg.tmp").write_bytes(b"x" * 5000)
        (provider_dir / DOWNLOAD_STATE_FILENAME).write_text("{}")
        assert cache.size_bytes() == 1000


class TestTileCacheIsEmpty:
    """``is_empty`` is the cheap yes/no probe that replaces
    ``size_bytes() == 0`` in the startup-time toggle-on path —
    short-circuits on the first observed tile instead of walking
    the whole subtree summing sizes. At ~107k tiles the old check
    can stall the GUI thread for many seconds; the new one is
    bounded by a single ``scandir`` traversal that exits as soon
    as the first ``.jpg`` is observed.
    """

    def test_missing_root_is_empty(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path / "absent", provider="esri")
        assert cache.is_empty() is True

    def test_empty_provider_dir_is_empty(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        # Force creation of the provider root without any tile.
        cache.provider_root().mkdir(parents=True, exist_ok=True)
        assert cache.is_empty() is True

    def test_single_tile_is_not_empty(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        cache.put(TileCoord(z=14, x=1, y=1), _make_jpeg_bytes(1024))
        assert cache.is_empty() is False

    def test_only_state_json_still_empty(self, tmp_path: Path) -> None:
        """A bare state JSON or stray ``.tmp`` in the provider root
        must not flip ``is_empty`` to False — the contract is
        "are there any *tile* files", not "is there any file".
        Returning False here would prevent the first-launch
        consent dialog from ever firing on a user who declined
        once (state JSON gets written) and later wiped the cache
        manually.
        """
        cache = TileCache(tmp_path, provider="esri")
        provider_dir = cache.provider_root()
        provider_dir.mkdir(parents=True, exist_ok=True)
        (provider_dir / DOWNLOAD_STATE_FILENAME).write_text("{}")
        (provider_dir / "stray.tmp").write_bytes(b"x")
        assert cache.is_empty() is True

    def test_evict_no_op_when_under_target(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        cache.put(TileCoord(z=14, x=1, y=1), _make_jpeg_bytes(1000))
        evicted = cache.evict_lru(target_bytes=10_000)
        assert evicted == 0
        assert cache.has(TileCoord(z=14, x=1, y=1))

    def test_evict_drops_oldest(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        coords = [TileCoord(z=14, x=1, y=i) for i in range(5)]
        # Stagger mtimes by writing then bumping them manually.
        import os  # noqa: PLC0415
        import time  # noqa: PLC0415

        for i, c in enumerate(coords):
            cache.put(c, _make_jpeg_bytes(1000))
            mtime = time.time() - (10 - i)
            os.utime(cache.path_for(c), (mtime, mtime))

        # Total = 5000; ask to fit in 2500. Should drop oldest 3.
        evicted = cache.evict_lru(target_bytes=2500)
        assert evicted == 3
        # Newest two should survive.
        assert cache.has(coords[3])
        assert cache.has(coords[4])
        # Oldest three are gone.
        assert not cache.has(coords[0])
        assert not cache.has(coords[1])
        assert not cache.has(coords[2])

    def test_evict_target_zero_drops_everything(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        for i in range(3):
            cache.put(TileCoord(z=14, x=1, y=i), _make_jpeg_bytes(1000))
        cache.evict_lru(target_bytes=0)
        for i in range(3):
            assert not cache.has(TileCoord(z=14, x=1, y=i))

    def test_evict_negative_target_raises(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        with pytest.raises(ValueError):
            cache.evict_lru(target_bytes=-1)


# ---------------------------------------------------------------------------
# DownloadState + read/write
# ---------------------------------------------------------------------------


class TestDownloadStateRoundTrip:
    """Persist + restore preserves all fields."""

    def test_round_trip_identity(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        original = DownloadState(
            zoom=14,
            bbox=(29.3, 33.4, 34.0, 36.0),
            total_tiles=19324,
            completed_tiles=4321,
            user_decision="in_progress",
            started_at="2026-05-18T08:30:00Z",
            completed_at=None,
        )
        write_download_state(original, cache)
        loaded = read_download_state(cache)
        assert loaded == original

    def test_completed_state_round_trip(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        original = DownloadState(
            zoom=14,
            bbox=(29.3, 33.4, 34.0, 36.0),
            total_tiles=100,
            completed_tiles=100,
            user_decision="accepted",
            started_at="2026-05-18T08:00:00Z",
            completed_at="2026-05-18T08:09:00Z",
        )
        write_download_state(original, cache)
        loaded = read_download_state(cache)
        assert loaded == original
        assert loaded is not None and loaded.is_complete()

    def test_state_lives_at_documented_path(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        write_download_state(DownloadState(), cache)
        expected = tmp_path / "esri" / DOWNLOAD_STATE_FILENAME
        assert expected.is_file()


class TestDownloadStateReadEdgeCases:
    """Every "doubt" path returns ``None`` so the dialog falls
    through to first-launch prompt."""

    def test_absent_returns_none(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        assert read_download_state(cache) is None

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        cache.provider_root().mkdir(parents=True)
        (cache.provider_root() / DOWNLOAD_STATE_FILENAME).write_text(
            "{ not valid json"
        )
        assert read_download_state(cache) is None

    def test_wrong_version_returns_none(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        cache.provider_root().mkdir(parents=True)
        (cache.provider_root() / DOWNLOAD_STATE_FILENAME).write_text(
            json.dumps({"version": 999, "zoom": 14})
        )
        assert read_download_state(cache) is None

    def test_missing_bbox_returns_none(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        cache.provider_root().mkdir(parents=True)
        (cache.provider_root() / DOWNLOAD_STATE_FILENAME).write_text(
            json.dumps(
                {
                    "version": DOWNLOAD_STATE_VERSION,
                    "zoom": 14,
                    "total_tiles": 0,
                    "completed_tiles": 0,
                }
            )
        )
        assert read_download_state(cache) is None

    def test_atomic_write_no_tmp_left(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        write_download_state(DownloadState(), cache)
        for p in cache.provider_root().rglob("*.tmp"):
            pytest.fail(f"leaked tmp: {p}")


class TestDownloadStateInvariants:
    """Behaviour the dialog state machine relies on."""

    def test_is_complete_false_when_zero_total(self) -> None:
        s = DownloadState(total_tiles=0, completed_tiles=0)
        assert not s.is_complete()

    def test_is_complete_true_when_equal(self) -> None:
        s = DownloadState(total_tiles=100, completed_tiles=100)
        assert s.is_complete()

    def test_is_complete_true_when_overshoot(self) -> None:
        # Defensive: if a bug ever wrote completed > total we still
        # call it complete (no infinite resume).
        s = DownloadState(total_tiles=100, completed_tiles=101)
        assert s.is_complete()
