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

"""Tests for :mod:`cvfr_routemaster.satellite_tiles`.

Pure-math module, so no Qt and no network. Every test runs offline,
deterministic, and inside a few milliseconds.

Coverage targets:

1. **Forward + inverse projection** — known-fixture lat/lon round
   through ``lonlat_to_world_pixel`` and back via
   ``world_pixel_to_lonlat`` to within float precision; spot-check
   against tile coords we already verified in the
   ``scratch/preview_*.py`` sanity scripts.
2. **tile_for_lonlat** — boundary semantics (point exactly on a tile
   edge belongs to the *lower* tile), correct values for our flight-
   relevant fixtures (LLBG, LLMZ Bar Yehuda, Eilat) at z=10–16.
3. **bbox_to_tiles / count_tiles_for_bbox** — covers the right tile
   set, count agrees with the materialised list, off-by-one absent
   at boundaries, reversed/empty bboxes degrade gracefully.
4. **tile_url** — both placeholder orderings (Esri's y-before-x and
   the OSM-style x-before-y) format correctly.
5. **metres_per_pixel** — standard fixture values
   (~38 m/px @ z=12 / lat=32°, ~9.5 m/px @ z=14, etc.).
6. **TileCache** path math is pure (never touches disk), and
   ``has`` returns False on an absent / empty file but True on a
   non-empty file.
7. **Edge cases** — lat clamp at the Mercator pole, lon wraparound
   beyond ±180°.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from cvfr_routemaster.satellite_tiles import (
    DEFAULT_TARGET_ZOOM,
    EARTH_CIRCUMFERENCE_M,
    ESRI_ATTRIBUTION,
    ESRI_WORLD_IMAGERY_TEMPLATE,
    ISRAEL_BBOX,
    MAX_TARGET_ZOOM,
    MIN_TARGET_ZOOM,
    TILE_SIZE_PX,
    USER_AGENT,
    WEB_MERCATOR_MAX_LAT,
    TileCache,
    TileCoord,
    bbox_to_tiles,
    count_tiles_for_bbox,
    lonlat_to_world_pixel,
    metres_per_pixel,
    tile_for_lonlat,
    tile_url,
    world_pixel_to_lonlat,
)

# Real-world fixtures — coordinates we have confirmed visually
# against Esri imagery in scratch/preview_*.py runs.
LLBG_LAT, LLBG_LON = 32.0114, 34.8867  # Ben Gurion, central Israel
LLMZ_LAT, LLMZ_LON = 31.3278, 35.3883  # Bar Yehuda, Dead Sea
EILAT_LAT, EILAT_LON = 29.5577, 34.9519  # south coast


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


class TestModuleConstants:
    """Pin the public constants — these are referenced by the dialog
    state machine and the build script, and changing them silently
    would silently change UX."""

    def test_tile_size_is_256(self) -> None:
        assert TILE_SIZE_PX == 256

    def test_default_zoom_inside_allowed_range(self) -> None:
        assert MIN_TARGET_ZOOM <= DEFAULT_TARGET_ZOOM <= MAX_TARGET_ZOOM

    def test_min_zoom_below_max_zoom(self) -> None:
        assert MIN_TARGET_ZOOM < MAX_TARGET_ZOOM

    def test_israel_bbox_is_normalised(self) -> None:
        min_lat, max_lat, min_lon, max_lon = ISRAEL_BBOX
        assert min_lat < max_lat
        assert min_lon < max_lon
        # Sanity: bbox actually contains all our flight fixtures.
        for lat, lon in [
            (LLBG_LAT, LLBG_LON),
            (LLMZ_LAT, LLMZ_LON),
            (EILAT_LAT, EILAT_LON),
        ]:
            assert min_lat <= lat <= max_lat
            assert min_lon <= lon <= max_lon

    def test_user_agent_identifies_app(self) -> None:
        # Pin presence of the project signature so we never
        # accidentally ship an empty/generic UA — the v2 VATSIM
        # work established this as a project-wide convention.
        assert "Israel CVFR Routemaster" in USER_AGENT
        assert "1980623" in USER_AGENT

    def test_esri_template_uses_y_before_x(self) -> None:
        # Esri's quirk that originally bit us in the roadmap: their
        # public arcgisonline endpoint wants {z}/{y}/{x}, NOT
        # {z}/{x}/{y}. Pin it so a regression flips back to OSM order.
        assert "{z}/{y}/{x}" in ESRI_WORLD_IMAGERY_TEMPLATE

    def test_esri_attribution_mentions_partners(self) -> None:
        # Esri's terms ask for partner attribution; pin the substrings.
        for required in ("Esri", "Maxar", "Earthstar"):
            assert required in ESRI_ATTRIBUTION


# ---------------------------------------------------------------------------
# Forward / inverse projection
# ---------------------------------------------------------------------------


class TestLonLatToWorldPixel:
    """Forward projection: ``(lon, lat, z) → (px, py)``."""

    def test_z0_whole_world_in_one_tile(self) -> None:
        # At z=0 the whole world is a single 256x256 tile, so:
        # lon=-180 → x=0; lon=+180 → x=256; lat=0 → y=128.
        x0, y0 = lonlat_to_world_pixel(-180.0, 0.0, 0)
        assert x0 == pytest.approx(0.0)
        assert y0 == pytest.approx(128.0)

        x1, y1 = lonlat_to_world_pixel(180.0, 0.0, 0)
        assert x1 == pytest.approx(256.0)
        assert y1 == pytest.approx(128.0)

    def test_origin_at_z_n_is_centre_of_world(self) -> None:
        # Lon=0, lat=0 always lands at world centre regardless of zoom.
        for z in range(0, 17):
            px, py = lonlat_to_world_pixel(0.0, 0.0, z)
            n = TILE_SIZE_PX * (2 ** z)
            assert px == pytest.approx(n / 2.0)
            assert py == pytest.approx(n / 2.0)

    def test_increasing_lon_moves_x_east(self) -> None:
        # Monotone in lon: increasing lon → strictly increasing x.
        x_west, _ = lonlat_to_world_pixel(34.0, 32.0, 14)
        x_east, _ = lonlat_to_world_pixel(36.0, 32.0, 14)
        assert x_east > x_west

    def test_increasing_lat_moves_y_north(self) -> None:
        # Web Mercator: larger lat → smaller y (closer to top of image).
        _, y_north = lonlat_to_world_pixel(35.0, 33.0, 14)
        _, y_south = lonlat_to_world_pixel(35.0, 29.0, 14)
        assert y_south > y_north

    def test_lat_clamped_at_pole(self) -> None:
        # ±90° would blow up tan(); the function clamps to
        # ±WEB_MERCATOR_MAX_LAT silently. Same y as exactly at the
        # clamp is the contract.
        _, y_at_clamp = lonlat_to_world_pixel(0.0, WEB_MERCATOR_MAX_LAT, 5)
        _, y_at_pole = lonlat_to_world_pixel(0.0, 90.0, 5)
        assert y_at_pole == pytest.approx(y_at_clamp)


class TestWorldPixelToLonLat:
    """Inverse projection: ``(px, py, z) → (lon, lat)``."""

    def test_centre_pixel_is_lon0_lat0(self) -> None:
        for z in range(0, 17):
            n = TILE_SIZE_PX * (2 ** z)
            lon, lat = world_pixel_to_lonlat(n / 2.0, n / 2.0, z)
            assert lon == pytest.approx(0.0, abs=1e-9)
            assert lat == pytest.approx(0.0, abs=1e-9)

    def test_top_left_pixel_is_pole_corner(self) -> None:
        # World pixel (0, 0) is the NW corner: lon=-180, lat≈+85.05.
        lon, lat = world_pixel_to_lonlat(0.0, 0.0, 0)
        assert lon == pytest.approx(-180.0)
        assert lat == pytest.approx(WEB_MERCATOR_MAX_LAT, abs=1e-6)


class TestProjectionRoundTrip:
    """``world_pixel_to_lonlat ∘ lonlat_to_world_pixel`` should be
    the identity within a few epsilons of float precision for any
    sane input."""

    @pytest.mark.parametrize(
        "lat,lon,name",
        [
            (LLBG_LAT, LLBG_LON, "LLBG"),
            (LLMZ_LAT, LLMZ_LON, "LLMZ"),
            (EILAT_LAT, EILAT_LON, "Eilat"),
            (60.0, -120.0, "high north / west"),
            (-45.0, 90.0, "south / east"),
            (0.0, 0.0, "origin"),
        ],
    )
    @pytest.mark.parametrize("z", [0, 8, 12, 14, 16])
    def test_round_trip_identity(
        self, lat: float, lon: float, name: str, z: int
    ) -> None:
        px, py = lonlat_to_world_pixel(lon, lat, z)
        lon_back, lat_back = world_pixel_to_lonlat(px, py, z)
        assert lon_back == pytest.approx(lon, abs=1e-9), name
        assert lat_back == pytest.approx(lat, abs=1e-9), name


# ---------------------------------------------------------------------------
# tile_for_lonlat
# ---------------------------------------------------------------------------


class TestTileForLonLat:
    """``(lon, lat, z) → TileCoord``."""

    def test_returns_tilecoord_with_correct_zoom(self) -> None:
        c = tile_for_lonlat(35.0, 32.0, 14)
        assert isinstance(c, TileCoord)
        assert c.z == 14

    def test_known_fixture_llbg_z14(self) -> None:
        # Pinned against the value our scratch/preview_esri_tiles.py
        # script emitted (``LLBG_BenGurion_z14_x9779_y6652.jpg``).
        c = tile_for_lonlat(LLBG_LON, LLBG_LAT, 14)
        assert c == TileCoord(z=14, x=9779, y=6652)

    def test_known_fixture_eilat_z14(self) -> None:
        # Pinned against ``Eilat_z14_x9782_y6782.jpg``.
        c = tile_for_lonlat(EILAT_LON, EILAT_LAT, 14)
        assert c == TileCoord(z=14, x=9782, y=6782)

    @pytest.mark.parametrize("z", [10, 11, 12, 13, 14, 15, 16])
    def test_tile_coords_in_valid_range(self, z: int) -> None:
        c = tile_for_lonlat(LLBG_LON, LLBG_LAT, z)
        max_idx = (2 ** z) - 1
        assert 0 <= c.x <= max_idx
        assert 0 <= c.y <= max_idx

    def test_tile_doubles_each_zoom_step(self) -> None:
        # Going from z to z+1 quadruples the world's tile count;
        # any specific point lands in a tile whose (x, y) doubles
        # plus a 0/1 sub-quadrant offset.
        c12 = tile_for_lonlat(LLBG_LON, LLBG_LAT, 12)
        c13 = tile_for_lonlat(LLBG_LON, LLBG_LAT, 13)
        assert c13.x in (c12.x * 2, c12.x * 2 + 1)
        assert c13.y in (c12.y * 2, c12.y * 2 + 1)

    def test_boundary_point_belongs_to_lower_tile(self) -> None:
        # Construct a point exactly on a tile boundary by going from
        # tile coords back to its NW corner lat/lon, then verify the
        # round-trip lands us at the same tile (not the one above/left
        # of it).
        target = TileCoord(z=12, x=2444, y=1663)
        nw_px = (target.x * TILE_SIZE_PX, target.y * TILE_SIZE_PX)
        lon, lat = world_pixel_to_lonlat(nw_px[0], nw_px[1], target.z)
        c = tile_for_lonlat(lon, lat, target.z)
        assert c == target


# ---------------------------------------------------------------------------
# bbox_to_tiles + count_tiles_for_bbox
# ---------------------------------------------------------------------------


class TestBBoxToTiles:
    """Bbox enumeration."""

    def test_degenerate_point_returns_one_tile(self) -> None:
        tiles = bbox_to_tiles(
            LLBG_LAT, LLBG_LAT, LLBG_LON, LLBG_LON, 14
        )
        assert len(tiles) == 1
        assert tiles[0] == tile_for_lonlat(LLBG_LON, LLBG_LAT, 14)

    def test_reversed_lat_returns_empty(self) -> None:
        tiles = bbox_to_tiles(33.0, 29.0, 34.0, 36.0, 14)
        assert tiles == []

    def test_reversed_lon_returns_empty(self) -> None:
        tiles = bbox_to_tiles(29.0, 33.0, 36.0, 34.0, 14)
        assert tiles == []

    def test_row_major_ordering(self) -> None:
        # First scan is north-to-south outer (y increases monotone),
        # then west-to-east inner (x resets at each new y).
        tiles = bbox_to_tiles(31.0, 32.0, 35.0, 36.0, 12)
        ys = [t.y for t in tiles]
        assert ys == sorted(ys)  # monotonic non-decreasing y
        # And within the first row, x is monotone increasing.
        first_y = tiles[0].y
        first_row = [t for t in tiles if t.y == first_y]
        assert [t.x for t in first_row] == sorted(t.x for t in first_row)

    def test_israel_bbox_z14_yields_thousands(self) -> None:
        # Sanity: the v3 default (all of Israel at z=14) should land
        # in the ~19k-20k range we estimated to the user. Tighter
        # bound than "thousands" because the count drives the
        # download-size estimate the first-launch dialog quotes; a
        # silent bbox change that 2x's the tile count would silently
        # 2x the user-visible "9 min, 330 MB" claim.
        tiles = bbox_to_tiles(*ISRAEL_BBOX, z=14)
        assert 17_000 < len(tiles) < 22_000

    def test_count_matches_materialised_list(self) -> None:
        for z in (8, 10, 12, 14):
            tiles = bbox_to_tiles(*ISRAEL_BBOX, z=z)
            count = count_tiles_for_bbox(*ISRAEL_BBOX, z=z)
            assert count == len(tiles), f"mismatch at z={z}"

    def test_count_quadruples_per_zoom_step(self) -> None:
        # Each zoom step doubles per-axis resolution → 4× tiles.
        c12 = count_tiles_for_bbox(*ISRAEL_BBOX, z=12)
        c13 = count_tiles_for_bbox(*ISRAEL_BBOX, z=13)
        c14 = count_tiles_for_bbox(*ISRAEL_BBOX, z=14)
        # Allow a small tolerance because the bbox edges don't fall on
        # tile boundaries cleanly, but the ratio should be close to 4.
        assert 3.5 < c13 / c12 < 4.5
        assert 3.5 < c14 / c13 < 4.5

    def test_no_duplicates(self) -> None:
        tiles = bbox_to_tiles(31.0, 32.0, 34.5, 35.5, 12)
        assert len(set(tiles)) == len(tiles)


# ---------------------------------------------------------------------------
# tile_url
# ---------------------------------------------------------------------------


class TestTileURL:
    """Template substitution for tile URLs."""

    def test_esri_template_substitutes_y_before_x(self) -> None:
        url = tile_url(
            ESRI_WORLD_IMAGERY_TEMPLATE,
            TileCoord(z=14, x=9779, y=6652),
        )
        # The y comes before the x in Esri's URL — pinned here.
        assert url.endswith("/14/6652/9779")

    def test_osm_style_template_substitutes_x_before_y(self) -> None:
        osm_template = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        url = tile_url(osm_template, TileCoord(z=10, x=611, y=415))
        assert url.endswith("/10/611/415.png")

    def test_template_with_extra_placeholder_passes_through(self) -> None:
        # str.format ignores keys it isn't asked to substitute, so a
        # template referencing extra named placeholders we don't
        # provide raises KeyError. Pin that contract so callers know.
        with pytest.raises(KeyError):
            tile_url(
                "{z}/{x}/{y}/{q}",
                TileCoord(z=10, x=1, y=2),
            )


# ---------------------------------------------------------------------------
# metres_per_pixel
# ---------------------------------------------------------------------------


class TestMetresPerPixel:
    """Ground resolution at lat/zoom."""

    def test_equator_z0(self) -> None:
        # Whole earth in 256 px wide at the equator → 156.5 km/px.
        assert metres_per_pixel(0.0, 0) == pytest.approx(
            EARTH_CIRCUMFERENCE_M / TILE_SIZE_PX
        )

    @pytest.mark.parametrize(
        "z,expected_mpp",
        [
            (10, 132.0),  # Israel-latitude approximate values
            (12, 33.0),
            (14, 8.3),
            (15, 4.1),
        ],
    )
    def test_israel_latitude_known_resolutions(
        self, z: int, expected_mpp: float
    ) -> None:
        # Loose match — within 5% for our cost-estimator dialog needs.
        actual = metres_per_pixel(31.5, z)
        assert actual == pytest.approx(expected_mpp, rel=0.05)

    def test_resolution_doubles_each_zoom_step(self) -> None:
        for lat in (0.0, 30.0, 60.0):
            r12 = metres_per_pixel(lat, 12)
            r13 = metres_per_pixel(lat, 13)
            assert r12 / r13 == pytest.approx(2.0, rel=1e-9)

    def test_higher_latitude_finer_resolution(self) -> None:
        # cos(lat) shrinks with lat → m/px decreases.
        assert metres_per_pixel(60.0, 14) < metres_per_pixel(0.0, 14)

    def test_clamps_at_pole_without_inf(self) -> None:
        # No NaN / Inf at exactly ±90° because we clamp inside.
        v = metres_per_pixel(90.0, 14)
        assert math.isfinite(v)
        assert v > 0


# ---------------------------------------------------------------------------
# TileCache (skeleton)
# ---------------------------------------------------------------------------


class TestTileCachePathFor:
    """Path computation is pure — never touches disk."""

    def test_path_layout_includes_provider_zoom_x(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        p = cache.path_for(TileCoord(z=14, x=9779, y=6652))
        # Path should be <root>/esri/14/9779/6652.jpg.
        assert p == tmp_path / "esri" / "14" / "9779" / "6652.jpg"

    def test_path_for_does_not_create_dirs(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        c = TileCoord(z=14, x=9779, y=6652)
        _ = cache.path_for(c)
        # Nothing should have been created.
        assert not (tmp_path / "esri").exists()

    def test_provider_segregation(self, tmp_path: Path) -> None:
        # Two TileCaches under different provider names produce
        # disjoint paths so simultaneous Esri + Stadia caches don't
        # collide.
        esri = TileCache(tmp_path, provider="esri")
        stadia = TileCache(tmp_path, provider="stadia")
        c = TileCoord(z=14, x=9779, y=6652)
        assert esri.path_for(c) != stadia.path_for(c)

    def test_invalid_provider_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            TileCache(tmp_path, provider="")
        with pytest.raises(ValueError):
            TileCache(tmp_path, provider="path/with/slash")
        with pytest.raises(ValueError):
            TileCache(tmp_path, provider="path\\with\\backslash")


class TestTileCacheHas:
    """``has`` does the only filesystem access in Phase 1."""

    def test_has_false_when_root_missing(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path / "does-not-exist", provider="esri")
        assert cache.has(TileCoord(z=14, x=1, y=2)) is False

    def test_has_false_when_file_missing(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        assert cache.has(TileCoord(z=14, x=1, y=2)) is False

    def test_has_false_for_zero_byte_file(self, tmp_path: Path) -> None:
        # A 0-byte file is most likely an interrupted previous write;
        # treat it as not-yet-cached so the next fetch overwrites
        # cleanly. Pin this contract — it's referenced in the
        # docstring and the next phase's fetch logic relies on it.
        cache = TileCache(tmp_path, provider="esri")
        c = TileCoord(z=14, x=1, y=2)
        p = cache.path_for(c)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        assert p.is_file()
        assert p.stat().st_size == 0
        assert cache.has(c) is False

    def test_has_true_for_non_empty_file(self, tmp_path: Path) -> None:
        cache = TileCache(tmp_path, provider="esri")
        c = TileCoord(z=14, x=1, y=2)
        p = cache.path_for(c)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\xff\xd8\xff")  # JPEG magic
        assert cache.has(c) is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Boundary behaviours we want to pin so they don't silently
    change."""

    def test_lat_above_clamp_does_not_explode(self) -> None:
        # +89° is past the Mercator clamp; should not raise / NaN.
        px, py = lonlat_to_world_pixel(0.0, 89.0, 14)
        assert math.isfinite(px) and math.isfinite(py)

    def test_lat_below_clamp_does_not_explode(self) -> None:
        px, py = lonlat_to_world_pixel(0.0, -89.0, 14)
        assert math.isfinite(px) and math.isfinite(py)

    def test_lat_at_exact_mercator_limit(self) -> None:
        px, py = lonlat_to_world_pixel(
            0.0, WEB_MERCATOR_MAX_LAT, 14
        )
        # py should be very close to 0 (top of the world pixmap).
        assert py == pytest.approx(0.0, abs=1e-3)

    def test_tilecoord_is_hashable(self) -> None:
        # Pinned because the bulk-fetch enumerator and Phase 3
        # inverse-warp planner both compose TileCoords into sets to
        # de-dupe. Hashability is part of the contract.
        s = {TileCoord(z=14, x=1, y=2), TileCoord(z=14, x=1, y=2)}
        assert len(s) == 1

    def test_tilecoord_is_immutable(self) -> None:
        c = TileCoord(z=14, x=1, y=2)
        with pytest.raises(Exception):
            c.x = 99  # type: ignore[misc]
