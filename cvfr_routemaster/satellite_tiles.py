"""Web Mercator tile math + tile URL/cache plumbing — pure-Python
data layer for the satellite-imagery feature.

This module deliberately has **zero Qt dependency** so it can be
unit-tested without a ``QApplication`` and reasoned about as plain
geometry. The HTTP fetch layer (the network call itself) lives
separately in :mod:`cvfr_routemaster.satellite_fetch`; the Qt
overlay that actually paints decoded tiles onto the chart scene
lives in :mod:`cvfr_routemaster.satellite_overlay`; the Qt thread
that drives bulk fetch + emits progress is
:mod:`cvfr_routemaster.satellite_worker`.

Three sub-layers, top-down:

1. :class:`TileCoord` — the immutable address of a single tile in the
   web-tile slippy-map scheme. Just ``(z, x, y)``; no provider info,
   no payload, no I/O. Hashable so it composes with sets / cache
   keys.
2. **Pure projection math** — :func:`lonlat_to_world_pixel` and its
   inverse :func:`world_pixel_to_lonlat`, plus :func:`tile_for_lonlat`,
   :func:`bbox_to_tiles`, :func:`metres_per_pixel`. The full
   chart-shaped-warp renderer in Phase 3 builds on top of these to
   turn each chart pixel into a sample of the satellite mosaic.
3. :class:`TileCache` — owns the on-disk cache: per-tile path
   computation, atomic put/get, total-size accounting, LRU eviction.
   The class deliberately holds **no in-memory state** beyond root
   path + provider name so two ``TileCache`` instances pointing at
   the same dir (e.g. one in the worker thread, one in the renderer)
   never disagree on what's cached. Filesystem mtime is the source
   of truth for both presence and recency.

Web Mercator vs. Lambert
------------------------

Our chart PDFs are Israeli CVFR Lambert Conformal Conic (the
standard ICAO 1:500k projection) and we calibrate them with an
affine in :mod:`cvfr_routemaster.geo_calibration`. Web tile
providers serve **Web Mercator** (EPSG:3857). The two projections
disagree by ≈17% N–S at Israel's latitude and visibly more than a
single chart-pixel over a 4° span.

We do **not** try to align the projections. Instead the renderer
(Phase 3) walks every chart pixel ``(x, y)``, runs it through the
existing affine to ``(lon, lat)``, then through this module's
:func:`lonlat_to_world_pixel` to look up the satellite mosaic
sample. Every other overlay (route, traffic, altitude arrows,
calibration anchors) keeps working unchanged because the satellite
pixmap shares the chart's lat/lon-to-pixel relationship after the
warp.

Slippy-map convention
---------------------

Standard "slippy map" tile scheme (the de-facto standard since
OpenStreetMap shipped it in 2007):

* Zoom level ``z`` slices the world into ``2**z × 2**z`` tiles.
* Each tile is :data:`TILE_SIZE_PX` × :data:`TILE_SIZE_PX` pixels
  (always 256 px square; no provider in our supported set deviates).
* Tile ``x`` increases eastward (lon), tile ``y`` increases southward
  (Web Mercator y, which is *not* lat). Tile (0, 0) is the NW corner
  of the world; tile (2**z - 1, 2**z - 1) is the SE corner.
* The Web Mercator projection clamps latitudes to ±85.0511° (the
  natural limit where the Mercator stretch goes to infinity); we
  reflect that as :data:`WEB_MERCATOR_MAX_LAT`.

URL ordering quirk
------------------

The two providers worth supporting differ on URL placeholder order:

* **Esri public arcgisonline** (``services.arcgisonline.com``) wants
  ``{z}/{y}/{x}`` — note the y *before* the x. Get this wrong and
  the tiles load but are scrambled, which is *not* obvious at first
  glance because every tile is still individually well-formed.
* **OSM-style providers** (Stadia, Mapbox, etc.) want ``{z}/{x}/{y}``.

Rather than encode this as a flag, we let the URL template be a
plain Python ``str.format``-able string with ``{z}``/``{x}``/``{y}``
placeholders; the calling code is responsible for picking the right
template. :data:`ESRI_WORLD_IMAGERY_TEMPLATE` is shipped pre-baked
with the y-before-x order.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Edge length of a single Web Mercator tile, in pixels. Every
#: provider in our supported set uses 256 — Esri, Stadia, Mapbox,
#: OSM, all of them. We deliberately don't expose this as a knob;
#: changing it would invalidate every other formula in the module.
TILE_SIZE_PX: int = 256

#: The latitude (in degrees) at which Web Mercator's natural
#: stretching factor ``sec(lat)`` becomes infinite. Tiles do not
#: exist beyond this latitude in the slippy-map scheme; clients
#: clamp lat to ``±WEB_MERCATOR_MAX_LAT`` before any pixel math.
#: Value comes from ``arctan(sinh(π))·180/π``.
WEB_MERCATOR_MAX_LAT: float = 85.0511287798066

#: Earth's circumference at the equator, in metres. Used for the
#: ground-resolution formula :func:`metres_per_pixel`. Web Mercator
#: assumes a perfect sphere of this circumference; the ~0.3% error
#: vs. WGS84 ellipsoid is well below a tile pixel at any zoom level
#: we care about.
EARTH_CIRCUMFERENCE_M: float = 40_075_016.686

#: Esri World_Imagery public-service tile URL template. Notes:
#:
#: * The placeholders are ``{z}``, ``{y}``, ``{x}`` in that order —
#:   y *before* x, which is the Esri-specific convention (see the
#:   "URL ordering quirk" section in the module docstring).
#: * No API key required for the public arcgisonline endpoint.
#: * Esri's master license prohibits **redistribution** of cached
#:   tiles, so we never bundle a populated cache in the release;
#:   each user's installation downloads to its own
#:   ``.cvfr_routemaster/tile_cache/`` on first use.
ESRI_WORLD_IMAGERY_TEMPLATE: str = (
    "https://services.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)

#: Attribution string that the satellite-mode UI renders bottom-right
#: of the viewport whenever Esri imagery is on screen. Esri's public
#: arcgisonline service explicitly says imagery doesn't legally
#: require attribution but courtesy attribution costs us nothing
#: and pre-empts any future tightening of their terms.
ESRI_ATTRIBUTION: str = (
    "Imagery © Esri, Maxar, Earthstar Geographics, GIS User Community"
)

#: HTTP ``User-Agent`` we send on every tile fetch. Same string the
#: VATSIM worker uses — Esri's terms (and basic etiquette) ask for
#: a contactable identifier on every request; reusing the established
#: project string is the obvious choice. The "VATSIM" reference in
#: the string is fine: Esri only cares that the request is
#: identifiable, not which network the application talks to.
USER_AGENT: str = (
    "Israel CVFR Routemaster Application - Created by VATSIM "
    "User ID: 1980623"
)

#: Default ``target_zoom`` for the bulk fetch. z=15 is the
#: empirically-chosen sweet spot: ~4 m/px native resolution (sharp
#: enough to make out runway markings and individual taxiway turns
#: -- meaningful for close-to-airport situational awareness),
#: ~71,600 tiles for a full Israel coverage, ~1.2 GB on disk,
#: ~36 min one-time download. The previous default of z=14 saved
#: ~1 GB and ~27 min but cost the user fine-grained airport detail.
#: Lower zooms lose the small-airfield case; higher zooms (z=16)
#: pass a 4 GB cache footprint. User-overridable in Display Settings
#: within the :data:`MIN_TARGET_ZOOM`–:data:`MAX_TARGET_ZOOM` range.
DEFAULT_TARGET_ZOOM: int = 15

#: Lower bound on the user-configurable target zoom. Below z=12 the
#: imagery is too coarse to add anything over the chart we already
#: render; we'd rather refuse the setting than ship a satellite mode
#: that's pointless to enable.
MIN_TARGET_ZOOM: int = 12

#: Upper bound on the user-configurable target zoom. z=16 is ~2 m/px
#: ground resolution and ≈4.6 GB of tiles for full Israel — past the
#: 1.5 GB default cache cap, so the user has to also raise the cap
#: in Display Settings before the cache won't thrash.
MAX_TARGET_ZOOM: int = 16

#: Israel-encompassing bounding box used as the default fetch scope.
#: Sized to cover both calibrated CVFR sheets (the chart anchors in
#: ``geo_calibration.json`` put the North sheet at lat 32.3°–33.15°/
#: lon 34.84°–35.64° and the South sheet at lat 29.56°–30.99°/
#: lon 34.47°–35.13°; we add roughly 0.2° buffer in every direction
#: to catch the chart edges plus a Mediterranean-coastal cushion).
#: Order is ``(min_lat, max_lat, min_lon, max_lon)`` — degrees,
#: WGS84.
#:
#: At z=14 this yields ≈19,300 tiles → ≈330 MB on disk and ~9 min
#: one-time download — the figures the first-launch dialog quotes.
#: Tightening further would mean computing the exact lat/lon hull
#: of each sheet's calibration at fetch time, which adds a coupling
#: to the calibration loader for diminishing returns (≤10% saving).
ISRAEL_BBOX: tuple[float, float, float, float] = (29.3, 33.4, 34.0, 36.0)

# ---------------------------------------------------------------------------
# Data class: TileCoord
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TileCoord:
    """The address of a single Web Mercator tile in slippy-map terms.

    Immutable / hashable so it composes naturally with ``set`` (the
    bulk-fetch enumerator hands us a set of TileCoords; the cache
    layer answers "do you have this one yet?" against the same set).

    Attributes:
        z: Zoom level (``0`` → whole world, ``2**z`` tiles per axis at
           level z). Slippy-map convention: ``0 ≤ z ≤ ~22`` in
           practice.
        x: Tile column. ``0`` at lon = -180°, ``2**z - 1`` just before
           lon = +180°. Increases eastward.
        y: Tile row. ``0`` at the top of Web Mercator (lat ≈ +85°),
           ``2**z - 1`` at the bottom (lat ≈ -85°). Increases
           **southward** — note the sign flip vs. latitude.
    """

    z: int
    x: int
    y: int


# ---------------------------------------------------------------------------
# Pure projection math
# ---------------------------------------------------------------------------


def _clamp_lat(lat: float) -> float:
    """Clamp ``lat`` to ``±WEB_MERCATOR_MAX_LAT``.

    Used as a defensive guard before any ``tan(lat)`` evaluation;
    callers that pass ``±90°`` would otherwise hit infinity. Internal
    helper, not exported — public functions document the clamp
    behaviour as part of their contract.
    """
    if lat > WEB_MERCATOR_MAX_LAT:
        return WEB_MERCATOR_MAX_LAT
    if lat < -WEB_MERCATOR_MAX_LAT:
        return -WEB_MERCATOR_MAX_LAT
    return lat


def lonlat_to_world_pixel(
    lon: float, lat: float, z: int
) -> tuple[float, float]:
    """Convert ``(lon, lat)`` (degrees, WGS84) to Web Mercator
    *world-pixel* coordinates at zoom ``z``.

    World-pixel space spans ``[0, TILE_SIZE_PX * 2**z)`` in both
    axes. Returned coordinates are floats (continuous), so callers
    can sample with sub-tile precision — exactly what Phase 3's
    inverse warp needs when sampling the satellite mosaic at every
    chart pixel.

    The relation to tile coords is just the integer floor:
    ``tile_x = int(world_x // TILE_SIZE_PX)``,
    ``tile_y = int(world_y // TILE_SIZE_PX)``.

    Latitude is clamped to ``±WEB_MERCATOR_MAX_LAT`` because Web
    Mercator's ``tan(lat)`` term is undefined at the poles. Out-of-
    range longitudes are *not* clamped — they wrap naturally through
    the modulo of the world-pixel formula, so e.g. ``lon = +200°``
    aliases to ``lon = -160°`` in pixel space (no exception, no
    surprise; same behaviour every slippy-map library implements).

    Args:
        lon: Longitude in degrees, WGS84. Positive east.
        lat: Latitude in degrees, WGS84. Positive north. Clamped to
            ``±WEB_MERCATOR_MAX_LAT`` before any math.
        z: Zoom level. ``0`` is whole-world-in-256-px, each step
           doubles per-axis resolution.

    Returns:
        ``(world_pixel_x, world_pixel_y)`` as floats. Range is
        ``[0, TILE_SIZE_PX * 2**z]`` in both axes (inclusive of the
        upper bound only at the antimeridian / extreme lat).
    """
    lat = _clamp_lat(lat)
    n = TILE_SIZE_PX * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def world_pixel_to_lonlat(
    px: float, py: float, z: int
) -> tuple[float, float]:
    """Inverse of :func:`lonlat_to_world_pixel`.

    Convert a Web Mercator world-pixel coordinate at zoom ``z`` back
    to ``(lon, lat)`` in degrees. Used by the Phase 3 renderer to
    map satellite-mosaic samples back to chart-pixel positions
    during the inverse warp; also handy for sanity tests
    (round-tripping any lat/lon through both functions should
    identity to within float precision).

    No range checks: callers that pass world-pixel coords beyond
    ``[0, 256·2**z]`` get a meaningfully wrapped lon and a
    saturating lat. This matches the forward function's tolerance
    of out-of-range longitudes; it's caller's job to keep inputs
    sensible.

    Args:
        px: World-pixel x. Conventionally in
            ``[0, TILE_SIZE_PX * 2**z]``.
        py: World-pixel y. Conventionally in
            ``[0, TILE_SIZE_PX * 2**z]``.
        z: Zoom level.

    Returns:
        ``(lon, lat)`` in degrees.
    """
    n = TILE_SIZE_PX * (2 ** z)
    lon = px / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * py / n)))
    lat = math.degrees(lat_rad)
    return lon, lat


def tile_for_lonlat(lon: float, lat: float, z: int) -> TileCoord:
    """Return the integer :class:`TileCoord` whose footprint contains
    ``(lon, lat)`` at zoom ``z``.

    Convenience wrapper over :func:`lonlat_to_world_pixel` for the
    common "which tile do I need to fetch for this point?" question.
    Boundary behaviour: a point exactly on a tile edge belongs to
    the *lower* tile — i.e. ``int(world_pixel // 256)`` floors,
    consistent with every other slippy-map library.

    Args:
        lon: Longitude in degrees.
        lat: Latitude in degrees (clamped to
            ``±WEB_MERCATOR_MAX_LAT``).
        z: Zoom level.

    Returns:
        :class:`TileCoord` with ``0 ≤ x, y < 2**z``.
    """
    px, py = lonlat_to_world_pixel(lon, lat, z)
    return TileCoord(z=z, x=int(px // TILE_SIZE_PX), y=int(py // TILE_SIZE_PX))


def bbox_to_tiles(
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    z: int,
) -> list[TileCoord]:
    """Return every :class:`TileCoord` whose footprint intersects the
    given lat/lon bounding box at zoom ``z``.

    Used by the Phase 2 bulk-fetch enumerator to compute "which tiles
    cover all of Israel?" given :data:`ISRAEL_BBOX`. The returned
    list is in row-major order (outer loop ``y``, inner loop ``x``)
    so that progress through the list maps to a vaguely top-down
    spatial sweep — handy for "downloading north Israel… now central…
    now south" perception in a status-bar progress message.

    Both edges of the bbox are inclusive: a bbox of
    ``(31.0, 31.0, 35.0, 35.0)`` (degenerate point) returns the
    single tile containing that point, not an empty list. Callers
    that want *exclusive* upper bounds should subtract one tile
    from each axis themselves.

    Args:
        min_lat: Southern edge of the bbox in degrees.
        max_lat: Northern edge of the bbox in degrees. Must be
            ``≥ min_lat``; reversed bounds yield an empty list.
        min_lon: Western edge of the bbox in degrees.
        max_lon: Eastern edge of the bbox in degrees. Must be
            ``≥ min_lon``.
        z: Zoom level.

    Returns:
        List of :class:`TileCoord`. Ordering is row-major
        (north-to-south outer, west-to-east inner). Length is
        ``≤ 2**z * 2**z`` (full-world bbox would return everything).
    """
    if max_lat < min_lat or max_lon < min_lon:
        return []
    nw_tile = tile_for_lonlat(min_lon, max_lat, z)
    se_tile = tile_for_lonlat(max_lon, min_lat, z)
    tiles: list[TileCoord] = []
    for ty in range(nw_tile.y, se_tile.y + 1):
        for tx in range(nw_tile.x, se_tile.x + 1):
            tiles.append(TileCoord(z=z, x=tx, y=ty))
    return tiles


def count_tiles_for_bbox(
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    z: int,
) -> int:
    """Count tiles for a bbox without materialising the full list.

    Used by the first-launch dialog to compute the size estimate it
    quotes the user (``Downloading imagery for all of Israel at
    zoom 14 will take ~9 min and use ~305 MB``) without paying the
    per-tile allocation cost — at z=16 over Israel that would be
    ~286k :class:`TileCoord` instances.

    Args:
        min_lat, max_lat, min_lon, max_lon: same as
            :func:`bbox_to_tiles`.
        z: Zoom level.

    Returns:
        Non-negative tile count.
    """
    if max_lat < min_lat or max_lon < min_lon:
        return 0
    nw_tile = tile_for_lonlat(min_lon, max_lat, z)
    se_tile = tile_for_lonlat(max_lon, min_lat, z)
    return (se_tile.x - nw_tile.x + 1) * (se_tile.y - nw_tile.y + 1)


def metres_per_pixel(lat: float, z: int) -> float:
    """Approximate ground resolution at the given lat/zoom, in metres.

    Standard Web Mercator formula:
    ``earth_circumference · cos(lat) / (TILE_SIZE_PX · 2**z)``.
    Used for the cost-estimator dialog ("native resolution at z=14
    in Israel is ~8 m/px") and for picking sensible
    :data:`DEFAULT_TARGET_ZOOM` based on chart-pixel scale.

    Args:
        lat: Latitude in degrees (clamped). Resolution depends on
            latitude because Mercator stretches with ``sec(lat)``.
        z: Zoom level.

    Returns:
        Metres per Web Mercator pixel at that lat/zoom.
    """
    return (
        EARTH_CIRCUMFERENCE_M
        * math.cos(math.radians(_clamp_lat(lat)))
        / (TILE_SIZE_PX * (2 ** z))
    )


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------


def tile_url(template: str, coord: TileCoord) -> str:
    """Substitute a :class:`TileCoord` into a URL template.

    The template is a plain Python ``str.format``-able string with
    ``{z}``, ``{x}``, ``{y}`` placeholders in any order. We do *not*
    encode provider-specific URL ordering as a flag; pick the
    correct template (e.g. :data:`ESRI_WORLD_IMAGERY_TEMPLATE` for
    Esri's y-before-x convention) and pass it in. This keeps
    provider switches a one-line config change rather than a code
    branch.

    Args:
        template: URL template string with ``{z}/{x}/{y}``
            placeholders. Other placeholders pass through (anything
            that ``str.format`` accepts).
        coord: The tile to address.

    Returns:
        Fully-resolved URL.

    Raises:
        KeyError: If the template references a placeholder we don't
            provide (e.g. ``{q}`` for Bing's quadkey scheme — not
            supported here, that's a different addressing model
            entirely).
    """
    return template.format(z=coord.z, x=coord.x, y=coord.y)


# ---------------------------------------------------------------------------
# TileCache
# ---------------------------------------------------------------------------


class TileCache:
    """On-disk LRU tile cache.

    Layout
    ------

    A cache is a directory tree rooted at
    ``<root>/<provider>/<z>/<x>/<y>.<ext>``. The provider segment
    isolates simultaneous caches across providers — switching from
    Esri to Stadia doesn't invalidate the Esri tiles, you just
    accumulate two roots. Per-tile filename uses the slippy-map
    ``y`` value (not Esri's URL order); the cache speaks the
    addressing scheme of the data, not the wire format.

    File extension is currently fixed to ``.jpg`` because every
    supported provider serves JPEG. If we ever add a provider that
    serves PNG (Stadia's "alidade" can; Esri can't), the extension
    will become provider-keyed.

    State
    -----

    The cache holds **no in-memory state** beyond ``root`` and
    ``provider``. Filesystem mtime is the source of truth for both
    presence (``has``) and recency (LRU eviction). This means two
    instances pointing at the same root — e.g. one in the worker
    thread that fills the cache, one in the renderer thread that
    samples it — never disagree on contents.

    Atomic writes
    -------------

    :meth:`put` writes via a ``.tmp`` sibling + ``os.replace`` so
    a crashed/killed process can never leave a half-written tile
    that ``has`` would mistakenly return ``True`` for. Combined
    with the "zero-byte files don't count as cached" rule in
    :meth:`has`, this gives crash-safe partial-download resume:
    interrupt at any moment, restart, and the worst case is one
    re-fetch.

    Attributes:
        root: Filesystem path containing all providers' caches.
            Conventionally ``<project_root>/.cvfr_routemaster/
            tile_cache``. Auto-created lazily on first
            :meth:`put`; computing paths or calling ``has`` works
            on a non-existent root.
        provider: Short provider id used as the second path
            segment. Default ``"esri"``; anything containing a path
            separator is rejected (we don't need provider
            templating, we need a directory name).
    """

    #: File extension for every cached tile. JPEG today; provider-
    #: keyed when/if we add a PNG provider.
    TILE_EXTENSION: str = ".jpg"

    #: Suffix appended to in-flight :meth:`put` writes before the
    #: atomic rename. Picked to be unambiguous and unlikely to
    #: clash with anything a future provider might serve.
    TMP_SUFFIX: str = ".tmp"

    def __init__(self, root: Path, provider: str = "esri") -> None:
        if "/" in provider or "\\" in provider or not provider:
            raise ValueError(
                f"provider must be a non-empty path segment, got {provider!r}"
            )
        self.root = Path(root)
        self.provider = provider

    # ----- Path math --------------------------------------------------

    def path_for(self, coord: TileCoord) -> Path:
        """Compute the on-disk path for a given tile.

        Pure: never touches the disk, never creates parent dirs.
        :meth:`put` handles parent-directory creation when actually
        writing.

        The returned path is always under ``self.root /
        self.provider / str(coord.z) / str(coord.x) /``; the
        per-zoom and per-column subdirs prevent any single directory
        from holding tens of thousands of files (which becomes
        slow on Windows NTFS once a directory passes ~50k entries).

        Args:
            coord: The tile to address.

        Returns:
            Absolute path (or relative to cwd, depending on whether
            ``self.root`` is absolute) where this tile's bytes live
            once cached.
        """
        return (
            self.root
            / self.provider
            / str(coord.z)
            / str(coord.x)
            / f"{coord.y}{self.TILE_EXTENSION}"
        )

    def provider_root(self) -> Path:
        """Path to the ``<root>/<provider>/`` directory.

        Used by :meth:`size_bytes` and :meth:`evict_lru` for the
        directory walk. Public so the bulk-fetch state-file writer
        in :mod:`cvfr_routemaster.satellite_fetch` can drop its
        sidecar JSON next to the tiles without re-deriving the
        path layout.
        """
        return self.root / self.provider

    # ----- Presence ---------------------------------------------------

    def has(self, coord: TileCoord) -> bool:
        """Cheap "is this tile already on disk?" probe.

        Reads from the filesystem (one ``stat`` syscall). Used by
        the bulk-fetch enumerator's "subtract tiles already on
        disk" pass and by the renderer's per-pixel sampling.

        We require a non-zero file size to consider the tile
        cached: a zero-byte file probably means an interrupted
        previous write, and ``has -> True`` for an empty file
        would lead the renderer to draw 0×0 of imagery in that
        tile's footprint. Returning ``False`` for empty files lets
        the next fetch overwrite cleanly without explicit cleanup.

        Args:
            coord: The tile to check.

        Returns:
            ``True`` iff the corresponding file exists and is
            non-empty.
        """
        p = self.path_for(coord)
        try:
            return p.is_file() and p.stat().st_size > 0
        except OSError:
            return False

    # ----- I/O --------------------------------------------------------

    def get(self, coord: TileCoord) -> bytes | None:
        """Read a cached tile's bytes from disk.

        Returns ``None`` for any "not really cached" condition —
        absent file, zero-byte file, or unreadable file (permission
        flap, transient I/O error). Callers treat ``None`` as a
        cache miss and hand the request off to the network fetcher.

        We don't *touch* the file's mtime on read here. The LRU
        eviction policy (:meth:`evict_lru`) drops oldest-by-mtime,
        and "oldest" intentionally means "least-recently-written"
        rather than "least-recently-read" — this keeps the cache
        biased toward recently-fetched tiles even if a long render
        re-samples old corners of the cache repeatedly. If we ever
        want true LRU-by-access we'd need to ``os.utime`` here
        explicitly; today's behaviour is FIFO-on-write, which is
        good enough.

        Args:
            coord: The tile to read.

        Returns:
            The tile's bytes, or ``None`` on miss / read error.
        """
        if not self.has(coord):
            return None
        try:
            return self.path_for(coord).read_bytes()
        except OSError:
            return None

    def put(self, coord: TileCoord, content: bytes) -> None:
        """Write ``content`` as the cached bytes for ``coord``,
        atomically.

        Algorithm:

        1. Compute the final path via :meth:`path_for`.
        2. ``mkdir(parents=True, exist_ok=True)`` on its parent
           dir — both the per-zoom and per-x subdirs.
        3. Write to ``<path>.tmp`` (overwrites any stale tmp from a
           previous crash).
        4. ``os.replace(<path>.tmp, <path>)`` — atomic on every
           supported OS as long as both paths live on the same
           filesystem (they always do here, both are under our own
           cache dir).

        Empty ``content`` is rejected with :class:`ValueError`
        because a zero-byte tile would be indistinguishable from
        an interrupted write and :meth:`has` would never return
        ``True`` for it. If a provider returns 200 with an empty
        body, the caller (:mod:`cvfr_routemaster.satellite_fetch`)
        is expected to translate that into a "missing tile" result
        without ever calling :meth:`put`.

        Args:
            coord: Where to put it.
            content: The bytes to write. Must be non-empty.

        Raises:
            ValueError: If ``content`` is empty.
            OSError: For unrecoverable filesystem errors (out of
                disk, permission denied, etc.). Caller decides
                whether to retry or surface to the user.
        """
        if not content:
            raise ValueError(
                "TileCache.put refuses empty content (would create a "
                "zero-byte file indistinguishable from an interrupted "
                "write)"
            )
        final_path = self.path_for(coord)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = final_path.with_suffix(
            final_path.suffix + self.TMP_SUFFIX
        )
        # write_bytes is open(..., 'wb').write — overwrites cleanly,
        # no need to unlink first.
        tmp_path.write_bytes(content)
        os.replace(tmp_path, final_path)

    # ----- Size accounting --------------------------------------------

    def is_empty(self) -> bool:
        """Cheap "does the cache hold any tile?" probe.

        Short-circuits as soon as the first tile file is observed
        — avoids the O(N) ``stat`` walk that :meth:`size_bytes`
        performs. Designed for the startup-time toggle-on path
        where the only question is "should we show the first-launch
        consent dialog because there's nothing here yet?", not
        "exactly how many bytes are on disk".

        With ~107k tiles on disk (the four-zoom default after a
        completed bulk fetch), :meth:`size_bytes` can take 5-30 s
        on Windows NTFS — :meth:`is_empty` returns essentially
        instantly because the first :func:`os.scandir` call's
        first entry is enough.

        Returns:
            ``True`` iff the provider subtree either doesn't exist
            yet or contains no tile file. Auxiliary files (state
            JSON, ``.tmp`` writes in-flight, stray dotfiles) are
            ignored.
        """
        provider_dir = self.provider_root()
        if not provider_dir.is_dir():
            return True
        try:
            with os.scandir(provider_dir) as z_iter:
                for z_entry in z_iter:
                    if not z_entry.is_dir(follow_symlinks=False):
                        continue
                    with os.scandir(z_entry.path) as x_iter:
                        for x_entry in x_iter:
                            if not x_entry.is_dir(follow_symlinks=False):
                                continue
                            with os.scandir(x_entry.path) as y_iter:
                                for y_entry in y_iter:
                                    if y_entry.name.endswith(
                                        self.TILE_EXTENSION
                                    ):
                                        return False
        except OSError:
            # Permission flap or transient I/O — treat as empty
            # rather than misclassify, since the worst case for
            # "false empty" is re-showing the consent dialog (the
            # user can dismiss it), whereas misclassifying a
            # genuinely empty cache as non-empty would silently
            # skip the dialog.
            return True
        return True

    def size_bytes(self) -> int:
        """Total disk usage of the cache, in bytes.

        Walks the entire ``<root>/<provider>/`` subtree and sums
        ``stat().st_size`` for every regular file with our tile
        extension. Skips ``.tmp`` files (they're transient and
        their size is unstable). Skips other files entirely
        (e.g. the future ``_download_state.json``); only
        ``*.jpg`` count.

        Returns 0 if the cache directory doesn't exist yet.

        At our biggest expected zoom (z=15, ~77k tiles), this walk
        is ~few hundred ms on a warm cache. Don't call from a hot
        path; the worker layer should call once at startup and
        once after each batch of evictions. Callers that only
        need a yes/no answer should use :meth:`is_empty` instead
        — it short-circuits on the first tile file rather than
        summing the whole tree.

        Returns:
            Total bytes of cached tiles.
        """
        provider_dir = self.provider_root()
        if not provider_dir.is_dir():
            return 0
        total = 0
        # rglob is recursive; we don't need our own walk.
        for p in provider_dir.rglob(f"*{self.TILE_EXTENSION}"):
            try:
                total += p.stat().st_size
            except OSError:
                continue
        return total

    def evict_lru(self, target_bytes: int) -> int:
        """Delete oldest-by-mtime tiles until the cache fits in
        ``target_bytes`` total.

        "Oldest" is least-recently-*written* (see :meth:`get` for
        why we don't bump mtime on read). For our use case — bulk
        fetch then steady-state — write-mtime is the right LRU
        proxy: the only times tiles are written are during the
        initial bulk fetch and during a user-triggered "Refresh
        Imagery", both of which produce a single coherent sweep.

        Strategy:

        1. If ``target_bytes < 0``, raise — almost certainly a
           bug in the caller's accounting.
        2. Walk the provider dir collecting ``(mtime, size, path)``
           tuples for every ``*.jpg`` file.
        3. Sort ascending by mtime.
        4. Delete tiles in order until accumulated remaining size
           ≤ ``target_bytes``.

        Returns the count of evicted tiles. Caller can use this
        for status-bar messaging if they want; we don't surface
        it from inside the worker today.

        Cleanup also drops empty per-x and per-z subdirs after
        eviction — slightly tedious but keeps the directory tree
        from accumulating thousands of empty leaf dirs over time.

        Args:
            target_bytes: Maximum allowed cache size in bytes.

        Returns:
            Number of tiles deleted.

        Raises:
            ValueError: If ``target_bytes`` is negative.
        """
        if target_bytes < 0:
            raise ValueError(
                f"target_bytes must be non-negative, got {target_bytes}"
            )
        provider_dir = self.provider_root()
        if not provider_dir.is_dir():
            return 0

        # Collect (mtime, size, path) for every cached tile.
        records: list[tuple[float, int, Path]] = []
        for p in provider_dir.rglob(f"*{self.TILE_EXTENSION}"):
            try:
                st = p.stat()
            except OSError:
                continue
            records.append((st.st_mtime, st.st_size, p))

        total = sum(size for _, size, _ in records)
        if total <= target_bytes:
            return 0

        records.sort(key=lambda r: r[0])  # oldest first
        evicted = 0
        remaining = total
        for _, size, p in records:
            if remaining <= target_bytes:
                break
            try:
                p.unlink()
                remaining -= size
                evicted += 1
            except OSError:
                # Skip what we can't delete; don't bail out — a
                # locked file is rare on Windows but possible.
                continue

        # Best-effort cleanup of now-empty subdirs. We don't
        # rmtree the whole provider dir even if it's empty —
        # subsequent puts will recreate the structure, and an
        # empty provider dir is harmless.
        for sub in sorted(
            provider_dir.rglob("*"), key=lambda p: -len(p.parts)
        ):
            if sub.is_dir():
                try:
                    sub.rmdir()
                except OSError:
                    # Non-empty or in-use; just leave it.
                    pass

        return evicted
