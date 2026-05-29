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

"""HTTP fetch + bulk-download bookkeeping for the v3 satellite-
imagery feature.

This module is the bridge between the pure projection math /
on-disk cache plumbing in :mod:`cvfr_routemaster.satellite_tiles`
and the Qt worker thread in :mod:`cvfr_routemaster.satellite_worker`
that drives the user-facing bulk download.

Three layers, top-down:

1. :class:`FetchOutcome` — a discriminated-union dataclass holding
   the outcome of a single tile fetch attempt. Five disjoint
   shapes (``fetched`` / ``missing`` / ``rate_limited`` /
   ``network_error`` / ``http_error``) so the caller can pattern-
   match on ``kind`` rather than inspect a grab-bag of fields.

2. :func:`fetch_tile` — single-tile HTTP GET. Pure plumbing: no
   cache awareness, no retry, no Qt; this is the bottom of the
   stack so it composes cleanly into both the synchronous
   ``fetch_and_cache_tile`` wrapper and a future async/parallel
   fetcher if we ever need it.

3. :class:`DownloadState` + :func:`read_download_state` /
   :func:`write_download_state` — persistent JSON next to the
   cache that records what bbox/zoom/decision the last bulk fetch
   was working on. Read on app start to drive the
   resume-vs-restart-vs-prompt decision tree (Phase 5). Written
   atomically; safe to interrupt at any point.

Plus :func:`tiles_to_fetch_for_bbox` — the bulk-fetch enumerator
that lists every tile in a bbox at a zoom and subtracts the ones
already on disk.

All HTTP error paths are mapped onto :class:`FetchOutcome`
explicitly. The worker layer (Phase 4) decides retry policy:
``rate_limited`` and ``network_error`` are transient (retry with
backoff), ``missing`` is permanent (skip this tile, paint gray
in the renderer's footprint), ``http_error`` is generally
permanent + worth surfacing (probably a config bug).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cvfr_routemaster.satellite_tiles import (
    ESRI_WORLD_IMAGERY_TEMPLATE,
    USER_AGENT,
    TileCache,
    TileCoord,
    bbox_to_tiles,
    tile_for_lonlat,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default per-tile HTTP timeout in seconds. 15 s is forgiving:
#: Esri's CDN normally answers in <300 ms but a transient stall
#: shouldn't kill a 17k-tile bulk fetch. The bulk-fetch worker
#: caps total wall time via cancellation, not per-tile timeout.
DEFAULT_TIMEOUT_S: float = 15.0

#: ``Retry-After`` we assume on a 429 if the server didn't send
#: an explicit value (it usually does, but defensively pick a
#: sensible floor). Consistent with our 15 s VATSIM polling
#: cadence — at this lower bound a backoff doesn't accidentally
#: hammer the provider faster than they're asking us to slow.
DEFAULT_RATE_LIMIT_BACKOFF_S: float = 30.0

#: Tile size sanity floor in bytes. JPEGs from Esri are reliably
#: ≥1 KB even for uniform-blue ocean tiles (the smallest we saw
#: in scratch/preview was 5 KB). Anything smaller is almost
#: certainly a truncated response or a server-side error page
#: served with a 200 status; we treat it as ``missing`` rather
#: than caching a corrupt tile.
MIN_TILE_BYTES: int = 256

#: Filename for the persistent download-state JSON. Lives at
#: ``<cache.provider_root()>/<DOWNLOAD_STATE_FILENAME>`` so it
#: travels with the per-provider cache; switching providers
#: doesn't conflate state.
#:
#: Leading underscore is intentional — visually flags this as a
#: "house-keeping" file rather than a tile (which would have a
#: ``.jpg`` extension and live further down the tree anyway).
DOWNLOAD_STATE_FILENAME: str = "_download_state.json"


# ---------------------------------------------------------------------------
# FetchOutcome — discriminated union
# ---------------------------------------------------------------------------

#: All :attr:`FetchOutcome.kind` values, exposed for callers that
#: want to ``match`` exhaustively (ruff / pylint won't catch a new
#: kind landing here, but at least the constant is in one place).
FETCH_OUTCOME_KINDS: tuple[str, ...] = (
    "fetched",
    "missing",
    "rate_limited",
    "network_error",
    "http_error",
)


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """The outcome of a single tile fetch attempt.

    Five disjoint shapes, discriminated on :attr:`kind`:

    * ``fetched``: HTTP 200 with non-empty body. ``content`` set
      to the response bytes; everything else default. **The only
      shape where the caller should call** :meth:`TileCache.put`.

    * ``missing``: server doesn't have a tile at this address
      (HTTP 404 / 410 / similar permanent-not-found). ``reason``
      gives a short human-readable label for logging. The
      renderer paints gray in this tile's footprint and we don't
      bother retrying.

    * ``rate_limited``: HTTP 429. ``retry_after_seconds`` is
      either the value of the server's ``Retry-After`` header or
      :data:`DEFAULT_RATE_LIMIT_BACKOFF_S` if they didn't send
      one. Worker layer applies this as exponential-ish backoff.

    * ``network_error``: connection refused, DNS failure,
      timeout, SSL issue, or any 5xx HTTP status (which we treat
      as transient regardless of the specific code — Esri's
      service can flap briefly during their internal failovers).
      ``reason`` carries the underlying error message. Worker
      layer retries with backoff.

    * ``http_error``: any other unexpected HTTP status (e.g. 401,
      403). Almost certainly a config bug — wrong URL template,
      auth misconfiguration, etc. ``status`` and ``reason`` set;
      worker layer surfaces once and stops retrying that tile.

    Use the classmethod constructors below rather than calling
    ``FetchOutcome(...)`` directly so the discriminator/payload
    invariants stay enforced in one place.
    """

    kind: str
    content: bytes | None = None
    status: int | None = None
    retry_after_seconds: float | None = None
    reason: str | None = None

    @classmethod
    def fetched(cls, content: bytes) -> "FetchOutcome":
        """Successful 200 with usable bytes."""
        if not content:
            raise ValueError(
                "FetchOutcome.fetched requires non-empty content; "
                "an empty response is a 'missing' or 'http_error' "
                "outcome, not a successful fetch"
            )
        return cls(kind="fetched", content=content, status=200)

    @classmethod
    def missing(cls, reason: str, status: int | None = None) -> "FetchOutcome":
        """Permanent not-found (404, 410, or post-decode body of 0
        bytes). Renderer fills gray; no retry."""
        return cls(kind="missing", reason=reason, status=status)

    @classmethod
    def rate_limited(
        cls, retry_after_seconds: float | None = None
    ) -> "FetchOutcome":
        """HTTP 429. Worker backs off and retries."""
        return cls(
            kind="rate_limited",
            status=429,
            retry_after_seconds=(
                retry_after_seconds
                if retry_after_seconds is not None
                and retry_after_seconds > 0
                else DEFAULT_RATE_LIMIT_BACKOFF_S
            ),
        )

    @classmethod
    def network_error(
        cls, reason: str, status: int | None = None
    ) -> "FetchOutcome":
        """Transient: timeout, DNS, refused, 5xx. Worker retries."""
        return cls(kind="network_error", reason=reason, status=status)

    @classmethod
    def http_error(cls, status: int, reason: str) -> "FetchOutcome":
        """Unexpected HTTP status (4xx other than 404/410/429).
        Worker surfaces once and skips this tile."""
        return cls(kind="http_error", status=status, reason=reason)


# ---------------------------------------------------------------------------
# Single-tile HTTP fetch
# ---------------------------------------------------------------------------


def _parse_retry_after(value: str | None) -> float | None:
    """Convert an HTTP ``Retry-After`` header value to seconds.

    The header can be either an integer-seconds count or an
    HTTP-date. We only handle the integer form because that's what
    Esri (and ~every CDN) emits in practice; HTTP-date form would
    need ``email.utils.parsedate_to_datetime`` for correctness and
    we have no evidence we'd ever see it from our supported
    providers. Returns ``None`` for any unparseable input —
    :meth:`FetchOutcome.rate_limited` falls back to
    :data:`DEFAULT_RATE_LIMIT_BACKOFF_S`.
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


def fetch_tile(
    coord: TileCoord,
    *,
    template: str = ESRI_WORLD_IMAGERY_TEMPLATE,
    timeout: float = DEFAULT_TIMEOUT_S,
    user_agent: str = USER_AGENT,
    opener: urllib.request.OpenerDirector | None = None,
) -> FetchOutcome:
    """Fetch one tile via HTTP. Returns a :class:`FetchOutcome`;
    never raises (every error path maps onto a non-``fetched``
    outcome).

    The function is pure HTTP — it does not consult or update any
    cache. Composability win: tests can mock it without touching
    disk, the worker can wrap it in a thread pool without
    coordinating two side-effects, and a future "verify cache"
    pass could call it to compare bytes against disk.

    Args:
        coord: The tile to fetch.
        template: URL template with ``{z}``, ``{x}``, ``{y}``
            placeholders. Default is :data:`ESRI_WORLD_IMAGERY_TEMPLATE`;
            override per call so the same fetcher can serve future
            providers with different URL ordering.
        timeout: Per-request socket timeout (seconds). Applies to
            both connect and read. Defaults to
            :data:`DEFAULT_TIMEOUT_S`.
        user_agent: ``User-Agent`` header value. Defaults to the
            project string :data:`USER_AGENT`. Tests can override
            to assert it's actually being sent.
        opener: Optional pre-built ``OpenerDirector`` (e.g. one
            with retry middleware or test mocks). When ``None``,
            uses ``urllib.request.urlopen`` directly. Public so
            tests can inject without monkey-patching the module.

    Returns:
        :class:`FetchOutcome`. Inspect ``.kind`` to dispatch.
    """
    from cvfr_routemaster.satellite_tiles import tile_url  # local to avoid cycle on reload  # noqa: PLC0415

    url = tile_url(template, coord)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})

    try:
        if opener is not None:
            resp = opener.open(req, timeout=timeout)
        else:
            resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        # 4xx / 5xx with a response body. Branch on status here
        # rather than letting the caller dig through the exception
        # — every other outcome path returns a flat dataclass.
        status = int(e.code)
        if status in (404, 410):
            return FetchOutcome.missing(
                reason=f"HTTP {status} {e.reason or ''}".strip(),
                status=status,
            )
        if status == 429:
            retry_after = _parse_retry_after(
                e.headers.get("Retry-After") if e.headers else None
            )
            return FetchOutcome.rate_limited(
                retry_after_seconds=retry_after
            )
        if 500 <= status < 600:
            # Treat 5xx as a transient blip — Esri (and most CDNs)
            # do recover within seconds.
            return FetchOutcome.network_error(
                reason=f"HTTP {status} {e.reason or ''}".strip(),
                status=status,
            )
        return FetchOutcome.http_error(
            status=status, reason=str(e.reason or "unknown HTTP error")
        )
    except urllib.error.URLError as e:
        # DNS failure, connection refused, SSL trust issue, etc.
        # All transient enough to retry; user-actionable ones
        # (e.g. captive portal) the worker surfaces after enough
        # consecutive failures.
        return FetchOutcome.network_error(reason=f"URLError: {e.reason!r}")
    except TimeoutError:
        return FetchOutcome.network_error(reason="timeout")
    except OSError as e:
        # Catch-all for low-level socket gremlins; matches the
        # vatsim_feed style.
        return FetchOutcome.network_error(reason=f"OSError: {e!r}")

    # Body read happens outside the urlopen try so a partial-body
    # ConnectionResetError surfaces as its own network_error rather
    # than masquerading as an HTTP error.
    try:
        with resp:
            content = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return FetchOutcome.network_error(reason=f"read failed: {e!r}")

    if len(content) < MIN_TILE_BYTES:
        # 200 with a tiny body almost always means a custom error
        # page or a truncated response. Esri specifically returns
        # a 200+JSON-error for some misconfigurations; treat it
        # as missing rather than caching the bad bytes.
        return FetchOutcome.missing(
            reason=(
                f"response body too small ({len(content)} bytes, "
                f"min={MIN_TILE_BYTES})"
            ),
            status=200,
        )

    return FetchOutcome.fetched(content)


def fetch_and_cache_tile(
    coord: TileCoord,
    cache: TileCache,
    *,
    template: str = ESRI_WORLD_IMAGERY_TEMPLATE,
    timeout: float = DEFAULT_TIMEOUT_S,
    user_agent: str = USER_AGENT,
    opener: urllib.request.OpenerDirector | None = None,
) -> FetchOutcome:
    """Fetch one tile and, on success, write it to ``cache``.

    Convenience composition over :func:`fetch_tile` +
    :meth:`TileCache.put` — the only shape where ``put`` is called
    is the ``fetched`` outcome (every other outcome leaves the
    cache untouched).

    No cache hit-check here on purpose: the bulk-fetch enumerator
    (:func:`tiles_to_fetch_for_bbox`) already filters cached tiles
    out of the work list before the worker calls us. Adding a
    redundant ``cache.has`` here would cost a stat per tile we're
    already committed to fetching.

    Returns the underlying :class:`FetchOutcome` so the caller can
    log / count by kind. ``cache.put`` failures (out of disk,
    permission denied) propagate as :class:`OSError` rather than
    a fake outcome — they're rare and fatal-to-the-fetch-batch,
    so the worker should surface them distinctly.
    """
    outcome = fetch_tile(
        coord,
        template=template,
        timeout=timeout,
        user_agent=user_agent,
        opener=opener,
    )
    if outcome.kind == "fetched" and outcome.content is not None:
        cache.put(coord, outcome.content)
    return outcome


# ---------------------------------------------------------------------------
# Bulk-fetch enumeration
# ---------------------------------------------------------------------------


def tiles_to_fetch_for_bbox(
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    z: int,
    cache: TileCache,
) -> list[TileCoord]:
    """Enumerate every tile in the bbox at zoom ``z`` that is not
    already cached.

    Used by the Phase 5 dialog to compute the size estimate
    ("we'd download N more tiles totalling ~M MB") and by the
    Phase 4 worker to drive the bulk fetch loop.

    The returned list preserves :func:`bbox_to_tiles`' row-major
    ordering (north-to-south outer, west-to-east inner) minus the
    tiles already on disk. That preserved ordering matters: the
    worker emits progress as the loop advances, and the user sees
    "downloading north Israel… now central… now south" rather
    than a pseudo-random permutation.

    Args:
        min_lat, max_lat, min_lon, max_lon: Bbox in degrees,
            same convention as :func:`bbox_to_tiles`.
        z: Zoom level.
        cache: The destination cache. Tiles already present (per
            :meth:`TileCache.has`) are excluded.

    Returns:
        Sub-list of ``bbox_to_tiles(...)``, with cached tiles
        removed. Empty list when the cache is fully populated.
    """
    return [
        coord
        for coord in bbox_to_tiles(min_lat, max_lat, min_lon, max_lon, z)
        if not cache.has(coord)
    ]


def count_cached_tiles_in_bbox(
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    z: int,
    cache: TileCache,
) -> int:
    """Fast count of cached tiles inside ``bbox`` at zoom ``z``.

    Drop-in alternative to ``len(tiles_to_fetch_for_bbox(...))`` for
    the "how many do we still have on disk?" question — but
    100-400x faster on dense caches because it walks directories
    in bulk via :func:`os.scandir` rather than calling
    :meth:`TileCache.has` (one ``stat`` syscall) per candidate
    tile.

    Why this exists
    ---------------

    The GUI's startup-time per-zoom progress seed used to drive
    the status-bar label by calling ``tiles_to_fetch_for_bbox``
    for each configured zoom and subtracting from the bbox total.
    At the four-zoom default that's ~107k stat syscalls on the
    GUI thread, which on Windows NTFS freezes the app for 5-30 s
    on a cold filesystem cache — exactly the "Not Responding"
    window the user sees on launch when satellite view is on
    from a prior session. :func:`os.scandir` batches each
    directory's entries into one syscall returning ~400 entries,
    cutting the syscall count from O(tiles) to O(x-dirs).

    Algorithm
    ---------

    1. Translate the lat/lon bbox to a tile ``(x, y)`` range at
       this zoom (cheap arithmetic, same as
       :func:`count_tiles_for_bbox`).
    2. Open ``<cache>/<provider>/<z>/`` once via :func:`os.scandir`.
    3. For each x-subdirectory whose integer name falls in
       ``[nw.x, se.x]``, open it once via :func:`os.scandir`
       and count entries whose integer ``.jpg`` filename falls
       in ``[nw.y, se.y]``.

    No per-file ``stat`` call. The cache's atomic-rename write
    discipline (``put`` writes ``<name>.tmp`` then ``os.replace``)
    guarantees any file *without* the ``.tmp`` suffix is a
    completed write — same invariant :meth:`TileCache.is_empty`
    relies on. A zero-byte tile here would be a true caller bug
    (``put`` raises ``ValueError`` on empty content), not a
    half-written-file artefact.

    Args:
        min_lat, max_lat, min_lon, max_lon: Bbox in degrees, same
            convention as :func:`bbox_to_tiles`.
        z: Zoom level to scan.
        cache: The cache whose provider subtree to walk.

    Returns:
        Number of cached tile files whose ``(x, y)`` slippy
        coordinates fall inside the bbox at zoom ``z``. ``0`` if
        the cache's per-zoom directory doesn't exist yet, or if
        the bbox is degenerate.
    """
    if max_lat < min_lat or max_lon < min_lon:
        return 0
    nw = tile_for_lonlat(min_lon, max_lat, z)
    se = tile_for_lonlat(max_lon, min_lat, z)
    z_root = cache.provider_root() / str(z)
    extension = cache.TILE_EXTENSION
    extension_len = len(extension)
    count = 0
    try:
        x_scan = os.scandir(z_root)
    except (FileNotFoundError, NotADirectoryError):
        return 0
    except OSError:
        return 0
    try:
        with x_scan:
            for x_entry in x_scan:
                if not x_entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    x = int(x_entry.name)
                except ValueError:
                    continue
                if x < nw.x or x > se.x:
                    continue
                try:
                    y_scan = os.scandir(x_entry.path)
                except OSError:
                    continue
                with y_scan:
                    for y_entry in y_scan:
                        name = y_entry.name
                        if not name.endswith(extension):
                            continue
                        try:
                            y = int(name[:-extension_len])
                        except ValueError:
                            continue
                        if y < nw.y or y > se.y:
                            continue
                        count += 1
    except OSError:
        # Transient I/O during the walk — return what we counted
        # so far rather than raise. The seed-from-cache path
        # treats a low count the same as "incomplete download",
        # which is the safe degradation.
        return count
    return count


# ---------------------------------------------------------------------------
# Persistent download-state JSON
# ---------------------------------------------------------------------------

#: Schema version of the JSON we serialise. Bump on any breaking
#: structural change. ``read_download_state`` returns ``None`` for
#: any unrecognised version, which gracefully causes a fresh
#: prompt (worse case: a paid-for partial download is forgotten
#: and the user sees the first-launch prompt again).
DOWNLOAD_STATE_VERSION: int = 1


@dataclass(frozen=True, slots=True)
class DownloadState:
    """Persistent record of an in-progress (or completed) bulk
    fetch.

    Read on app start to drive the dialog state machine in Phase 5:

    * ``user_decision == "accepted"`` AND
      ``completed_tiles < total_tiles`` → resume-or-restart
      prompt.
    * ``user_decision == "accepted"`` AND
      ``completed_tiles == total_tiles`` → silent, cache is
      ready.
    * ``user_decision == "declined"`` → silent, respect choice;
      first satellite-toggle later kicks the prompt with extra
      "this will be slow" wording.
    * File absent / unparseable / old version → first-launch
      prompt.

    All fields are JSON-friendly primitives; serialisation is a
    plain :func:`json.dumps` of :func:`dataclasses.asdict`.

    Attributes:
        version: Schema version. Always
            :data:`DOWNLOAD_STATE_VERSION` for in-process records;
            on disk it might be older (which case
            :func:`read_download_state` rejects).
        provider: Which tile provider this state belongs to. The
            file lives under that provider's cache subdir so the
            field is technically redundant — kept for diagnostics
            and so a future "merge two caches" tool has all the
            info it needs in one record.
        zoom: Zoom level the bulk fetch targets / targeted.
        bbox: ``(min_lat, max_lat, min_lon, max_lon)`` the fetch
            covers. Stored so a later code-side change to
            :data:`ISRAEL_BBOX` invalidates an earlier-bbox
            partial — the user gets re-prompted with the new
            scope's size.
        total_tiles: Tile count in the bbox at this zoom (i.e.
            ``count_tiles_for_bbox(*bbox, zoom)``).
        completed_tiles: Tiles successfully cached so far. Bounded
            ``[0, total_tiles]``; the on-disk value reflects the
            most recent ``write_download_state`` call from the
            worker, which the worker batches every ~50 tiles to
            avoid hammering the disk on every successful fetch.
        user_decision: ``"accepted"`` (download permitted),
            ``"declined"`` (skip; ask again later only on toggle),
            or ``"in_progress"`` (the worker is currently
            running; same as ``accepted`` for resume-decision
            purposes but lets the dialog distinguish "you said
            yes earlier, app closed mid-download" from "you said
            yes earlier, download already finished").
        started_at: ISO-8601 timestamp of the user's accept
            decision (or first-fetch trigger if there's no
            explicit decision recorded). Cosmetic in the
            "resumed download started 14 minutes ago" sense; not
            used in any logic.
        completed_at: ISO-8601 timestamp of fetch completion, or
            ``None`` while in progress.
    """

    version: int = DOWNLOAD_STATE_VERSION
    provider: str = "esri"
    zoom: int = 14
    bbox: tuple[float, float, float, float] = (29.3, 33.4, 34.0, 36.0)
    total_tiles: int = 0
    completed_tiles: int = 0
    user_decision: str = "in_progress"
    started_at: str = field(default_factory=lambda: _now_iso())
    completed_at: str | None = None

    def is_complete(self) -> bool:
        """``True`` iff every tile is cached.

        Defined here rather than at the call site so we keep the
        invariant ``completed_tiles ≤ total_tiles`` honest in one
        place (a bug that wrote 99% via batched ``write_download_state``
        but actually finished should still report complete on the
        next start).
        """
        return self.total_tiles > 0 and self.completed_tiles >= self.total_tiles


def _now_iso() -> str:
    """UTC ISO-8601 timestamp, second-precision, ``Z`` suffix.

    Used as the default for ``DownloadState.started_at``. We
    don't use ``datetime.now(tz=timezone.utc).isoformat()`` raw
    because it emits ``+00:00`` rather than ``Z`` — both are
    valid ISO-8601 but ``Z`` is what every other JSON timestamp
    we ship uses.
    """
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _state_path(cache: TileCache) -> Path:
    """Where ``read``/``write_download_state`` look on disk."""
    return cache.provider_root() / DOWNLOAD_STATE_FILENAME


def read_download_state(cache: TileCache) -> DownloadState | None:
    """Load the persistent download state.

    Returns ``None`` for any "no usable state" condition:

    * File absent (most common: first launch, never downloaded).
    * Cache root absent (definitely first launch).
    * JSON parse error (corrupted file from a crash mid-write —
      should be rare with the atomic-rename writer below).
    * Version mismatch (rare; happens when the on-disk file was
      written by an older release of the app, and we've bumped
      :data:`DOWNLOAD_STATE_VERSION`).

    The "return None on doubt" shape is deliberate: every doubt
    case maps onto "show the first-launch prompt", which is the
    safe default.
    """
    p = _state_path(cache)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("version") != DOWNLOAD_STATE_VERSION:
        return None
    try:
        bbox_raw = raw.get("bbox")
        if (
            not isinstance(bbox_raw, (list, tuple))
            or len(bbox_raw) != 4
            or not all(isinstance(v, (int, float)) for v in bbox_raw)
        ):
            return None
        # JSON deserialises the tuple as a list — re-tuple-ify for
        # the dataclass invariant.
        bbox: tuple[float, float, float, float] = (
            float(bbox_raw[0]),
            float(bbox_raw[1]),
            float(bbox_raw[2]),
            float(bbox_raw[3]),
        )
        return DownloadState(
            version=int(raw["version"]),
            provider=str(raw.get("provider", "esri")),
            zoom=int(raw.get("zoom", 14)),
            bbox=bbox,
            total_tiles=int(raw.get("total_tiles", 0)),
            completed_tiles=int(raw.get("completed_tiles", 0)),
            user_decision=str(raw.get("user_decision", "in_progress")),
            started_at=str(raw.get("started_at", _now_iso())),
            completed_at=(
                str(raw["completed_at"])
                if raw.get("completed_at") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def write_download_state(state: DownloadState, cache: TileCache) -> None:
    """Persist ``state`` next to the cache, atomically.

    Same atomic-rename trick as :meth:`TileCache.put` — write to
    ``<state>.tmp`` then ``os.replace`` — so a kill mid-write
    leaves either the old state intact or the new one fully
    written, never garbage. The Phase 4 worker calls this after
    every batch of N tiles (probably N=50; a finer granularity
    just costs disk I/O for the same crash-resume guarantee).

    Creates the parent dir if needed (the cache directory might
    not exist yet on the very first state write).
    """
    p = _state_path(cache)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(state), ensure_ascii=False, indent=2) + "\n"
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, p)
