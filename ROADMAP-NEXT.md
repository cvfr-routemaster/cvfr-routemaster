# ROADMAP-NEXT — v2/v3/v4 features

## Known gap (2026-05-29): Linux release lacks the v3.3 AGPL artifacts

The Windows v3.3 build now copies `LICENSE` into `release/` next
to the .exe (AGPL §4) and includes `LICENSE` in
`release/source/cvfr-routemaster-source.zip`'s top-level entries
(AGPL §6(a)). The Linux build script
(`scripts/build_release_for_linux.py`) was NOT updated to match —
it ships neither `release-linux/LICENSE` nor any source bundle,
and its README has no "where to get source" pointer.

This is **knowingly accepted** because the Linux build is for
personal (developer) use only and is not being distributed. If
the Linux build ever becomes a public release, mirror the
Windows changes:

- Add `_copy_license()` to `build_release_for_linux.py` and wire
  it into `main()` (copies `LICENSE` from repo root into
  `release-linux/` next to the binary).
- Either (a) add a `_bundle_source_zip()` step to mirror the
  Windows §6(a) approach, or (b) add a "Source code is available
  at https://github.com/cvfr-routemaster/cvfr-routemaster" line
  to `release-linux/README.txt` to satisfy AGPLv3 §6(d).
- Add Linux-mirror tests in `tests/test_build_script_no_pdfs.py`
  next to the Windows `test_windows_*` tests for the same
  contracts.

The tracked test
`test_windows_source_bundle_top_files_includes_license_and_requirements`
already pins the Windows contract; the Linux mirror is the only
piece outstanding.

---

This file captures the design investigation for the features
queued after the current roadmap. v1 work continues to be tracked
in `ROADMAP.md`; this file is the design record for what comes
next, in the order we plan to implement it:

1. **v2 — VATSIM traffic on the map (live)**
2. **v3 — Satellite imagery view of the chart area**
3. **v4 — Keep screen awake while flying** (small, isolated)

Implementation begins with the VATSIM traffic feature.

---

## Status snapshot — 2026-05-17 (resume here)

### Feature 1 (VATSIM traffic, v2): live and visible on the chart.

User confirmed visual review with a real flight at LLER on
2026-05-17. Stepping away mid-session; pick up here next time.

**Done — landed and tested (746 passed, 2 skipped):**

- **Display-settings rename** (kept filename
  `font_settings_dialog.py`, expanded scope; QSettings keys
  unchanged so users don't lose their saved sizes).
- **Traffic icon size knob** — persisted via QSettings under
  `traffic_icon_size_px`, default **36 px** (1.5× the original
  24 px after first-flight visual review showed labels were too
  small at 24 px and the silhouette crowded). Adjustable from
  the dialog and via Ctrl+scroll over a plane.
- **Aircraft wake DB** bundled at
  `cvfr_routemaster/resources/aircraft_wake.json`; bundled in
  both `cvfr-routemaster.spec` AND
  `cvfr-routemaster-linux.spec`. `wake_for_aircraft_type`
  walks every slash-segment of the FAA-style equip code so
  `H/B738/L` correctly resolves to **M** via the `B738` segment.
- **`vatsim_feed.py`** — pure-Python v3 datafeed parser with
  `If-Modified-Since` caching, `User-Agent`
  `"Israel CVFR Routemaster Application - Created by VATSIM
  User ID: 1980623"`, `Pilot` dataclass, `filter_to_bbox`. Full
  network-mocked test coverage.
- **`vatsim_worker.py`** — `QObject` poller on a `QThread`,
  15 s `QTimer`, race-safe stop, signals
  `pilots_updated`/`fetch_failed`/`poll_skipped`. Israel bbox
  defaults: lat 29–34, lon 33.5–36.5.
- **Toolbar toggle "Show VATSIM traffic"** — persisted; restored
  on startup via a `QTimer.singleShot(200ms, …)` to fire after
  signal connection. Worker started/stopped on toggle and torn
  down in `closeEvent`.
- **`traffic_overlay.py`** — silhouette = wake-colored shape
  with 3-pixel **black/white/black** layered border (cosmetic
  pens, round caps/joins). Two-line right-side composed label
  (white fill + 2-pixel black halo on each line):
  - line 1: `<callsign>/<icao_type>` (e.g. `ELY323/B738`).
    Falls back to bare `<callsign>` when no aircraft type
    was filed — the unknown-wake gray colour already conveys
    "no plan filed".
  - line 2: `<altitude>/<speed>kt` (e.g. `FL280/420kt`,
    `2500ft/85kt`, `GRND/0kt`, `GRND/12kt`). The altitude
    component uses `FLnnn` for IFR/Y/Z, `<n>ft` for VFR/empty
    plans, or `GRND` when the pilot is below the wake-class
    groundspeed threshold (see `_is_on_ground`).
  - **Why two lines, not three**: the earlier three-line layout
    (callsign / altitude / speed on separate rows) ran ≈70 px
    tall at default 36 px icon / 16 pt bold, taller than the
    silhouette and prone to overlap at busy ramps like LLBG.
    Composing identity into line 1 and motion-state into line 2
    drops the block to ≈45 px while keeping every piece of
    info on screen.
- **Ground detection.** Per-wake thresholds, strict `<`:
  - L = 50 kt, M = 100 kt, H = 120 kt, J = 140 kt, unknown = 50 kt
  - Source of truth: `_GROUND_SPEED_THRESHOLDS_KT` +
    `_is_on_ground` in `traffic_overlay.py`.
- **Map-hint legend** — `_map_hint` is now a `RichText`
  `QLabel`. Generates a colored-square wake-category legend
  inline when **(traffic toggle is ON)** AND **(hints are not
  hidden)**. Method: `_update_map_hint_text`.

**Not yet done in v2 (parked, low-priority unless asked):**

- Per-frame **interpolation between 15 s polls** using each
  pilot's `groundspeed` + `heading` (the "v2 polish" item from
  the original plan). Worth revisiting only if the 15 s step
  feels choppy in real use.
- **Item-identity reuse keyed on `cid`** — current rebuild is
  full-clear-and-redraw. Hasn't been profiled against
  rebuild-stutter; only optimize if the user reports flicker.
- **Standalone VATSIM attribution label** in the corner of the
  map view (we currently attribute via the legend's "VATSIM" in
  context; consider adding a dedicated `Traffic from VATSIM
  (network data)` label per the original plan if VATSIM ever
  push-back).
- **Per-profile icon size** (normal vs. airplane). Today there's
  one global value. Split only if asked.
- **Three consecutive failures → status-bar message.** Currently
  every failure shows once; the original plan called for a
  "non-modal status bar message after 3 in a row" debounce.
  Trivial to add later, hasn't bitten us in testing.

**Layout caveat (improved 2026-05-17 by going to 2 lines):**

The composed two-line label runs ~45 px tall at default 36 px
icon / 16 pt bold — comparable to the silhouette and a
meaningful improvement over the earlier three-line block
(~70 px). LLBG ramp overlap is much less severe now. If it
ever still bites in dense areas, options to consider:
label-collision avoidance, smaller font for line 2, or
hiding line 2 entirely while a plane is on the ground.

### Feature 2 (Satellite view, v3): not started.

Design plan below stands; still **the next task after v2 polish**
or whenever the user calls for it.

### Where to look first when resuming

- `cvfr_routemaster/traffic_overlay.py` — silhouette + label
  rendering, ground detection, altitude/speed formatters.
- `cvfr_routemaster/vatsim_worker.py` — polling lifecycle.
- `cvfr_routemaster/main_window.py` — toolbar action wiring,
  worker start/stop, map hint legend (`_update_map_hint_text`).
- `tests/test_traffic_overlay.py` (92 tests),
  `tests/test_vatsim_worker.py` (18 tests),
  `tests/test_ui_layout.py` (legend tests).
- Run the full suite with: `python -m pytest -q` (~3 m 30 s on
  this machine, 746 pass / 2 skip).

---

## TL;DR

**Both features are feasible.** Feature 1 (VATSIM traffic) is medium-effort
and has a very clean drop-in spot in the existing architecture. Feature 2
(satellite view) is meaningfully harder, mostly because of tile-provider
terms, a coordinate-system mismatch with the Lambert-projected chart, and
offline behaviour — but there's a single design decision that collapses
most of the complexity: **render satellite tiles into a chart-shaped
pixmap by sampling them through the existing affine calibration, so every
overlay (route, traffic, altitude arrows) keeps working unchanged.**

---

## What's already in our favor

A few invariants in `cvfr_routemaster/` make both features dramatically
easier:

- `geo_calibration.lonlat_to_scene(pixmap_item, cal, lon, lat) -> QPointF`
  already gives us the exact pixel coordinate for any lat/lon on either
  sheet. `_project_route_point_to_scene` (`main_window.py:1749`) already
  implements the "prefer the in-bounds sheet, fall back to the calibrated
  one" logic — that's exactly what we need for traffic too.
- `_redraw_route_overlay` (`main_window.py:1787`) is a textbook pattern to
  copy: rebuild a `QGraphicsItem` from scratch on every change, give it
  `ItemIgnoresTransformations` so its size is in screen pixels, set
  `setAcceptedMouseButtons(NoButton)` so it doesn't eat clicks, set a high
  `setZValue` so it lays over the chart.
- The settings store (`settings_store.py`) already has the three-rung load
  path (QSettings → shipped JSON → hard-coded defaults), the
  airplane-vs-normal profile split, and the persistence hooks for
  window/splitter/font sizes. Adding one more knob is a copy-paste
  exercise.
- `font_wheel_resize.py` already implements "Ctrl+wheel over a recognized
  widget category resizes that category, persists, re-applies." For the
  icon-size scroll resize, this file *is* the template.
- We already use `QThread` workers (`_map_thread`, `_alt_thread`,
  `_wp_ocr_thread`) so the "fetch on a worker, hand the result to the
  main thread via signal" pattern is in our bones.
- No HTTP client is currently bundled — but stdlib `urllib.request` is
  enough for both VATSIM and tile fetches. Zero new third-party deps
  required, and PyInstaller doesn't care.

The thing that will bite us most is **PyInstaller `--onefile` + new
dependencies** — see "release pitfalls" near the bottom.

---

# Feature 1 — Real-time VATSIM traffic on the map (v2)

## Feasibility: clearly feasible, ~1.5–2 days of focused work.

### Where the data comes from

The single source of truth for VATSIM positions is the v3 JSON datafeed:

- **URL:** `https://data.vatsim.net/v3/vatsim-data.json`
- **No auth required**, public, served from a CDN (status.vatsim.net
  publishes mirror URLs in `status.json` if a primary fails — worth
  honoring).
- **Refresh cadence:** the file regenerates every **15 seconds**
  server-side. Polling more often than that is wasted. Their published
  guidance is "poll once every 15 s and respect the `Last-Modified`
  header." We'd send `If-Modified-Since` and accept 304 as a no-op.
- **VATSIM Code of Conduct** wants any client to send a descriptive
  `User-Agent` header so they can contact us if a client misbehaves.
  Use something like `cvfr-routemaster/<version> (contact-info)`.

The `pilots` array entries contain (confirmed via the published schema
and live samples):

| Field | Use |
|---|---|
| `cid` | unique VATSIM ID (stable identity for tracking same plane between polls) |
| `callsign` | label text (the whole reason for the feature) |
| `name` | optional tooltip |
| `latitude`, `longitude` | position (decimal degrees, WGS84) |
| `altitude` | feet |
| `groundspeed`, `heading` | for icon rotation + tooltip |
| `transponder`, `qnh_i_hg`, `qnh_mb` | tooltip extras |
| `flight_plan.aircraft_short` / `aircraft_faa` | ICAO type designator → wake category lookup |
| `flight_plan.departure`, `arrival`, `route` | tooltip |
| `last_updated`, `logon_time` | freshness |

**Wake category is not a field in the feed.** It's derived from
`flight_plan.aircraft_short` (e.g. `B738` → Medium, `A388` → Super,
`C172` → Light). Two viable sources for the lookup:

1. **ICAO Doc 8643** (authoritative). They publish a JSON dataset, but
   it's behind a free registration. Annoying for redistribution.
2. **`atoff/OpenAircraftType` on GitHub** (open license, ~hundreds of
   designators). This is what most third-party VATSIM tooling uses. We'd
   ship a stripped-down `aircraft_wake.json` (≈30 KB) with just
   `{"B738": "M", "C172": "L", ...}` derived from this dataset and
   refreshed at build time. Falls back to "M" (medium) for unknown
   types.

The "vatsim radar?" question — vatsim-radar.com itself doesn't publish
a clean traffic API beyond an airlines list endpoint. They consume the
same `data.vatsim.net` feed. **Use VATSIM directly**; vatsim-radar adds
nothing for our purposes.

### Architecture proposal

Two new modules and a small surgical extension of `main_window.py`:

**`cvfr_routemaster/vatsim_feed.py`** (new) — pure-data layer:

- `fetch_vatsim_data(timeout: float) -> list[Pilot]` —
  `urllib.request` GET with the User-Agent + `If-Modified-Since`
  plumbing, JSON parse, raise on network/parse errors.
- `@dataclass(frozen=True) class Pilot` —
  `cid, callsign, lat, lon, altitude_ft, heading_deg, groundspeed_kts,
  aircraft_type: str | None, wake: str ("L"/"M"/"H"/"J"/"unknown"), …`.
- A function `filter_to_bbox(pilots, min_lat, max_lat, min_lon, max_lon,
  pad_deg) -> list[Pilot]` — we never plot more than a handful at any
  time over Israel anyway, but the filter belongs in the data layer for
  testability.
- One `aircraft_wake.json` resource bundled under
  `cvfr_routemaster/resources/` (PyInstaller `datas`).
- 100% unit-testable with a fixture JSON blob; no Qt dependency.

**`cvfr_routemaster/vatsim_worker.py`** (new) — Qt thread + timer:

- `class VatsimPoller(QObject)` with signals
  `pilots_updated(list[Pilot])`, `failed(str)`. Owns a `QTimer` set to
  15 000 ms, calls `fetch_vatsim_data` on a `QThread`, emits the result
  on the GUI thread.
- Start/stop API: enabled only when the toolbar toggle is on AND the
  chart is loaded AND at least one sheet is calibrated (no calibration
  → no projection → no point in fetching). This is also how we honor
  "don't waste bandwidth when the user isn't looking".
- Robustness: any fetch error logs once, leaves the previous list
  alone, retries on the next tick. Three consecutive failures show a
  non-modal status bar message.

**`cvfr_routemaster/traffic_overlay.py`** (new) — rendering:

- `class TrafficOverlay` mirrors `_redraw_route_overlay`'s shape:
  builds one `QGraphicsItem` per pilot, parented to a single group item
  so a clear/redraw is one `removeItem` call.
- Each plane is drawn as a small chevron/silhouette painted into a
  `QGraphicsItemGroup` that contains:
  - a colored, rotated triangle (color encodes wake category — blue/L,
    green/M, orange/H, purple/J)
  - a `QGraphicsSimpleTextItem` for the callsign placed offset to the
    lower-right
- The whole group has `ItemIgnoresTransformations` set, so when the
  user zooms the chart, the planes stay constant pixel size — exactly
  the route-origin-marker pattern at lines 1820–1837.
- Position: set via `setPos(lonlat_to_scene(...))`. On every
  `pilots_updated` signal, we rebuild from scratch (cheap — typically
  < 50 planes in Israeli airspace at peak). De-flicker by reusing items
  keyed on `cid` if the rebuild proves visibly stuttery; profile first.

**Wiring inside `MainWindow`:**

- A new toolbar toggle "Show VATSIM traffic" alongside Hide Waypoint
  View / Hide Usage Hints.
- A `_traffic_overlay: TrafficOverlay | None` and a
  `_vatsim_poller: VatsimPoller | None`, lifetime tied to "chart is
  loaded AND toggle is on".
- Connect `vatsim_poller.pilots_updated` →
  `traffic_overlay.set_pilots`.
- When calibration completes (already a single chokepoint at the end of
  `_finalize_auto_anchor_calibration`), redraw existing pilots with
  the new projection.

### The icon-size knob

Two ways to set it (settings dialog + Ctrl+scroll over a plane),
persisted alongside font sizes / pane positions.

**Rename the dialog scope.**

- `font_settings_dialog.py` keeps its filename (less churn for any
  test that targets it), but the dialog title becomes "Display
  settings" and the toolbar action label "Font Settings…" →
  "Display Settings…". QSettings key prefix unchanged so we don't
  invalidate users' saved font sizes.
- Add a fourth row to each profile group: "Traffic icon size: __ px".
  Keep the same min/max bounds machinery.
- `settings_store.FontSizes` stays untouched; a separate
  `traffic_icon_size_px` lives as its own QSettings key — fonts are
  a category; traffic icon size is a single scalar that doesn't share
  airplane-mode logic with fonts. The dialog renders both, but they
  persist independently. (One global value to start; can split into
  per-profile later if needed.)

**Ctrl+scroll on a plane.**

- Reuse `font_wheel_resize.py`'s pattern: install on `QApplication`,
  intercept `QEvent.Wheel + Ctrl`, walk the widget tree from
  `QApplication.widgetAt(QCursor.pos())`. Currently it routes by
  ancestor `QTableView` / `QLabel#routeText` / `QLabel#mapHint`.
- Extend `_font_category` to recognize "the cursor is over a
  `QGraphicsView` AND `itemAt(view_local_pos)` is a `TrafficPlaneItem`"
  → return a fourth category `"traffic"`. Adjust by 1 px per detent
  like the others, save via the new key, re-apply by signaling the
  overlay to rebuild with the new size.
- Ctrl+scroll *while pointed at* any plane is enough; no click-to-arm
  gesture (the existing font-resize doesn't require one either).
- Pointing-elsewhere-on-the-map Ctrl+scroll keeps doing nothing
  (current `_font_category` returns `None` for the map and the filter
  consumes the event silently — that's the correct continuation; the
  user already won't be surprised).

### Practical concerns and decisions

- **Update cadence on the screen.** 15 s is choppy for fast traffic.
  We can interpolate between polls using each pilot's `groundspeed` +
  `heading` and a per-frame `QTimer` (say 1 Hz). Clear "v2 polish" —
  easy to add later without changing the data layer.
- **Icon style.** Three or four wake-category-tinted shapes is plenty;
  don't try to draw distinct silhouettes per type. A single
  isoceles-triangle silhouette rotated to `heading_deg` reads as
  "airplane" universally. We can render to an offscreen `QPixmap` once
  per (color, size) tuple and cache.
- **Filtering.** Israel-only is geographically tiny (lat 29–33,
  lon 34–36). Padding 1–2 degrees still leaves us with maybe 5–50
  planes globally relevant, so the bbox filter could even live in the
  worker; we don't need spatial indexing.
- **VATSIM ToS.** The feed is public and free. We must (a) present a
  User-Agent identifying the app, (b) cap polling to ≥15 s, (c) not
  redistribute the data, (d) attribute "VATSIM" somewhere in the UI
  when traffic is visible. A small `Traffic from VATSIM (network data)`
  label in the corner of the map view is the cheap way to satisfy (d).
- **Tests.** Unit-test `vatsim_feed.py` with a captured JSON fixture
  (the file is ~5 MB raw at peak, but we trim to a minimal fixture).
  Don't hit the network in tests — use `responses`-style mocking via
  `unittest.mock.patch('urllib.request.urlopen', …)`. The poller and
  overlay get their own tests that don't touch the network.
- **Headless / no-internet.** If the network is down or VATSIM is
  offline (it happens — there's a recurring `status.vatsim.net` post
  about it), the feature must degrade silently: empty traffic list,
  status bar warning the first time, retry on the timer. It must never
  block the main UI or crash on a torn DNS.

### Risks (known unknowns)

1. **Schema drift.** VATSIM bumped from text → JSON v1 → JSON v3 over
   a decade; expect another bump someday. Mitigation: parse
   defensively, log unknown fields once, key on `general.version` for
   future-proofing. We've absorbed a schema bump before with a worse
   loader (the OCR cache versioning) so the muscle memory is here.
2. **`flight_plan` is `null`** for VFR pilots without a filed plan —
   meaning no aircraft type and therefore no wake category. Use
   "unknown" (a fifth color, e.g. gray) and don't crash.
3. **Wake dataset license.** OpenAircraftType is MIT — fine to bundle
   the lookup. ICAO's official 8643 has a non-redistribution clause —
   don't ship that one even though it's authoritative. Document the
   choice in the file's docstring.

---

# Feature 2 — Satellite view checkbox (v3)

## Feasibility: clearly feasible, ~3–5 days, more design decisions.

### Where the imagery comes from — provider survey

| Provider | Endpoint | Cost | Attribution | Suitable? |
|---|---|---|---|---|
| **Esri World_Imagery** | `https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}` | Free for non-commercial | "Esri, Maxar, Earthstar Geographics, GIS User Community" | ✅ best fit. Israel coverage is good. No API key for the public service. Note `{z}/{y}/{x}` order, not `{z}/{x}/{y}`. |
| **Stadia Maps Alidade Satellite** | `https://tiles.stadiamaps.com/tiles/alidade_satellite/{z}/{x}/{y}.jpg` | Free tier with attribution; needs API key for production | CNES/Airbus DS, PlanetObserver, Stadia | ✅ good quality, slightly higher zoom (20). Requires registering for a free key. |
| **OpenStreetMap default tiles** | `tile.openstreetmap.org` | Free, fragile | OSMF | ❌ **not satellite** — they're rendered map tiles. Don't confuse the two. |
| **Mapbox Satellite** | API key required | Free tier; commercial | Mapbox | Possible but adds a key-management story we don't want. |
| **Bing/Google/Apple imagery** | Tile URLs exist but ToS prohibits direct tile use outside their SDKs. | — | — | ❌ off limits. |

**Recommendation: Esri World_Imagery as default**, with the URL
template configurable via QSettings for power users / future Stadia
switch. Esri's public service explicitly states "Esri imagery does not
legally require attribution" but courtesy attribution costs us nothing.
Their AUP allows desktop-app usage; high-volume users are asked to
register but a single user clicking around their local airspace is
well within informal limits.

### The hard part: projection mismatch

Our charts are **Israeli CVFR Lambert Conformal Conic** (the standard
ICAO 1:500k projection), captured into a pixmap and tied to lat/lon
by a 6-DoF affine. Web tile providers serve **Web Mercator
(EPSG:3857)**. The two projections disagree:

- At Israel's latitude (~31.5°N), Web Mercator stretches north–south
  by a factor of ~`1/cos(31.5°)` ≈ 1.17.
- Lambert and Web Mercator coincide *only* at one latitude band each.
  Over the ~4° latitude span of a CVFR sheet, the two systems
  disagree by visibly more than a single pixel.

This means we **cannot** just slap a Web Mercator tile mosaic into the
same pixmap rectangle and expect it to align with our route polylines,
traffic, altitude arrows, or anything else.

There are three escape hatches; only one is the right answer.

**Option A — naive bbox stitch** (what most quick-and-dirty satellite
overlays do). Compute the chart's lat/lon bbox, fetch enough Web
Mercator tiles to cover it, blit them into a pixmap, replace the
chart. Result: route lines drift visibly across the screen as you
scroll. **Reject.** Breaks every existing overlay.

**Option B — replace the projection model entirely while satellite is
on.** Track which view mode is active; switch the affine pipeline to a
Web-Mercator-aware projection (lon/lat → EPSG:3857 → tile pixel) for
plotting overlays, while the satellite pixmap is laid out at
Web-Mercator pixel coordinates 1:1. Result: correct everywhere, but
it's a second projection codebase running in parallel with the
calibration system, with state synchronization between them.
**Possible but expensive.** Adds 800+ lines and a second test surface.

**Option C — render satellite tiles into a *chart-shaped* pixmap by
sampling them through the existing affine.** This is the move. For
each pixel `(x, y)` in the chart pixmap rectangle:

1. Convert `(x, y)` → `(u, v)` (normalize by pixmap size).
2. `cal.uv_to_lonlat(u, v)` → `(lon, lat)`.
3. `(lon, lat)` → Web Mercator pixel at the fetched tile zoom.
4. Sample the tile mosaic at that Mercator pixel, write into the
   chart pixmap at `(x, y)`.

**Pros:**

- Every overlay stays unchanged. Routes, traffic, altitude arrows,
  calibration anchors — all of them stay pixel-correct because the
  pixmap they're drawn over has the *same* lat/lon-to-pixel
  relationship as the chart.
- Sheet-selection logic, the saved layout, the Alt+wheel sheet-scale
  escape hatch, scroll-bar pan, and view-zoom — all of them keep
  working with no changes. (The old Alt+drag manual-move gesture was
  removed when the joint LSQ layout solver took over alignment.)
- The Lambert-vs-Mercator disagreement is absorbed by the per-pixel
  resampling: the satellite is stretched/skewed to fit the chart, not
  the other way around.

**Cons:**

- One inverse warp per pixel. A 4000×3000 pixmap is 12 M samples —
  bilinear-sampling that in pure Python is slow. NumPy fixes it:
  vectorize the inverse to give a `(2, H, W)` float array of source
  coordinates, then use `numpy.take` /
  `scipy.ndimage.map_coordinates` for the bilinear sample. Without
  scipy we can do nearest-neighbor with `numpy.take` (acceptable
  visual quality at the chart's effective resolution) in a few
  hundred ms. NumPy is already a runtime dependency.
- Tile fetch + warp must be backgrounded — not on the GUI thread.
  Same `QThread` pattern we already use for OCR.
- The first time the user hits the toggle, we have to download
  O(50–200) tiles. ~5–15 MB. Show a progress label.

**Recommendation: Option C.** Keep our projection assumptions; treat
the satellite pixmap as just another render of the same chart
geometry. Anything else creates a parallel coordinate system we'll
regret.

### Architecture proposal

**`cvfr_routemaster/satellite_tiles.py`** (new):

- Pure functions: `lonlat_to_mercator_xy(lon, lat) -> (x, y)`,
  `tile_for_lonlat(lon, lat, z) -> (tx, ty)`,
  `tile_url(template, z, x, y) -> str`.
- A `TileCache` class wrapping the on-disk cache directory
  (`<project_root>/.cvfr_routemaster/tile_cache/<provider>/<z>/<y>/<x>.jpg`).
  Append-only, LRU-trimmed at e.g. 200 MB.
- `fetch_tile(template, z, x, y, cache) -> bytes` —
  `urllib.request` with the same User-Agent etiquette as VATSIM.

**`cvfr_routemaster/satellite_render.py`** (new):

- `def render_sheet_as_satellite(pixmap_item, calibration, target_zoom, tile_cache) -> QPixmap`
- Computes which Web Mercator tiles cover the bbox of the chart's
  lat/lon corners, fetches them concurrently, builds a NumPy mosaic,
  then walks every output pixel via the inverse warp described in
  Option C, samples the mosaic, returns a `QPixmap`.
- `target_zoom` is chosen so 1 satellite pixel ≈ 1 chart pixel at
  the chart's nominal scale — for an Israel-sized CVFR sheet at
  4000×3000 px, that's about z=11 or 12 (~150 m/pixel).

**`cvfr_routemaster/satellite_worker.py`** (new):

- `QObject` with signals `progress(int)`,
  `finished(QPixmap, sheet_id)`, `failed(str)`. Renders both sheets
  in parallel `QThread`s.

**Wiring inside `MainWindow`:**

- A new checkbox/action *over the map area*. Cleanest place: add it
  as a small `QCheckBox` overlay anchored to the top-right of
  `self._view`'s viewport, parented to the viewport directly (same
  trick the calibration overlay uses — see `_calibration_overlay`
  at `main_window.py:430` and the `viewport()` parenting at
  `main_window.py:2001`).
- Two parallel pixmap items per sheet: `_north_item` (chart) and a
  new `_north_satellite_item`. Toggle by flipping `setVisible`
  instead of swapping items, so the route overlay's `Z` ordering
  doesn't have to change. This also makes "fade between" achievable
  later if desired.
- The first time the toggle is enabled, kick off the satellite
  worker; while it runs, show a "Rendering satellite imagery…"
  overlay and keep the chart visible. When the rendered pixmap
  arrives, install it and flip visibility.
- Persist the toggle state across sessions in QSettings
  (`map_view_mode = "chart" | "satellite"`).

### Practical concerns and decisions

- **Caching is not optional.** First render fetches tiles; every
  subsequent toggle reads from disk. The cache should be invalidated
  only on (a) provider change, (b) user-triggered "refresh imagery"
  command, (c) corrupt-tile detection. Do NOT invalidate when the
  chart's calibration changes — the warp is recomputed from the
  cache, no re-download needed.
- **Offline behaviour.** If a tile is missing from cache and there's
  no internet, fill that tile with a neutral gray and proceed. The
  user gets a partial satellite mosaic instead of a crash.
- **Attribution.** Bottom-corner label "Imagery: Esri, Maxar,
  Earthstar Geographics, GIS User Community" while satellite mode is
  on. Hide it in chart mode. ~10 lines.
- **PDF chart still wins for in-flight use.** Make this clear in the
  README — satellite is for situational awareness/planning, the CVFR
  chart is what you actually fly off. Consider auto-disabling
  satellite mode in airplane mode (or just leaving it as-is and
  trusting the user; either is defensible).
- **Tests.** Unit-test the projection math and the inverse-warp loop
  with a synthetic 256×256 calibrated pixmap, a fake "tile" that's
  just a coordinate gradient, and assert the resampled output
  reproduces the original lat/lon → uv map. The fetch layer gets a
  network mock. The Qt worker gets a simple "feed it a fake renderer,
  assert signals fire in order" test. **Do not** hit live tile servers
  in CI.
- **Performance budget.** Acceptable: ≤ 5 s per sheet on first render
  (4000×3000 px, ~100 tiles, NumPy warp). Aim for ≤ 1 s on
  cached-tile re-renders. If we get slower than that, the warp loop
  is the suspect — vectorize harder, or use scipy.
- **Coordinate-system gotcha.** Esri uses `{z}/{y}/{x}` order in some
  endpoints and `{z}/{x}/{y}` in others. Their **public arcgisonline
  endpoint** wants `{z}/{y}/{x}`. Get this wrong and the tiles load
  but are scrambled. Sanity-check with a known fixture in tests.

### Risks (known unknowns)

1. **Provider blocks high-volume usage.** Single-pilot use should be
   fine; if Esri ever rate-limits us, having the provider URL
   configurable in settings means switching to Stadia is a config
   change, not a code change.
2. **Network library bloat.** `urllib.request` is fine for VATSIM
   (one small file every 15 s). For tile fetch we want some
   concurrency. `concurrent.futures.ThreadPoolExecutor` over
   `urllib.request` is enough; do NOT pull in `requests` or
   `aiohttp` just for this — every new dep multiplies the
   PyInstaller surface.
3. **NumPy version drift.** Already pinned to ≥1.24 in
   `requirements.txt`; we're fine. We'd add NumPy operations that
   already exist in `map_crop.py`.
4. **Tile cache size.** A populated tile cache for both Israeli
   sheets at z=12 is ~50–100 MB. **Don't** ship it inside the
   PyInstaller `--onefile` bundle — keep it in
   `<project_root>/.cvfr_routemaster/tile_cache/`, downloaded on
   first use. The build script (per
   `.cursor/rules/build-releases.mdc`) deliberately keeps caches
   outside the .exe so they survive across launches.

---

# Feature 3 — Keep screen awake while flying (v4)

## Feasibility: trivially feasible, ~half a day of focused work.

The smallest feature in this file. Both target OSes expose a
clean, no-extra-deps way to inhibit the idle screen-lock and
display-sleep while our app is running; no admin rights
needed on either, no background polling required (both APIs
are "set once, forget until release"). PyInstaller is
unaffected — Windows uses ctypes against an OS DLL, Linux
uses Qt's bundled D-Bus.

### Why we want it

The whole point of the chart window is to be looked at during
a flight. A laptop with the default 5-minute idle-lock will
blank, lock, and demand re-auth right when the user is busy
hand-flying a CVFR transition near LLBG. Annoying at best;
genuinely unsafe at worst if the user fumbles for the
keyboard. Suppressing the idle lock for the duration of the
program run is the right answer.

### Per-OS mechanism

**Windows** — `kernel32.SetThreadExecutionState` via
`ctypes.windll.kernel32`:

```
SetThreadExecutionState(
    ES_CONTINUOUS         # 0x80000000 — sticks until cleared
  | ES_DISPLAY_REQUIRED   # 0x00000002 — keeps display awake
  | ES_SYSTEM_REQUIRED    # 0x00000001 — keeps system awake
)
```

Reset on shutdown with `ES_CONTINUOUS` alone (no requirement
flags) → back to default behaviour.

`ES_CONTINUOUS` is critical — without it the call is a
one-shot "reset the idle timer to now", which would force us
to re-pump the call on a QTimer every minute. With it, the
inhibit persists for the lifetime of the calling thread.
Process exit releases automatically.

**Linux** — D-Bus `org.freedesktop.ScreenSaver.Inhibit`,
called via `PySide6.QtDBus.QDBusInterface` (already a Qt
component; zero new pip deps):

```
cookie = bus.call("org.freedesktop.ScreenSaver",
                  "/org/freedesktop/ScreenSaver",
                  "org.freedesktop.ScreenSaver",
                  "Inhibit", "<app-name>", "<reason>")
# ... later ...
bus.call(..., "UnInhibit", cookie)
```

Honored by every modern desktop (GNOME, KDE, XFCE, Cinnamon,
MATE) on both X11 and Wayland — it's a session-bus call, not
a display-server call, so the X11/Wayland split doesn't
matter. The `cookie` pattern composes correctly with other
apps' inhibitors (each app holds its own cookie; releases are
independent).

Fallback chain in case a stripped-down DE doesn't implement
`org.freedesktop.ScreenSaver`:

1. `org.freedesktop.ScreenSaver.Inhibit` — covers ~99% of
   desktops
2. `org.gnome.SessionManager.Inhibit` — older GNOME,
   slightly different signature (takes a flags bitfield)
3. `org.freedesktop.login1.Manager.Inhibit("idle", ..., "block")`
   — systemd-logind, returns a Unix file descriptor; closing
   the fd releases the inhibitor. Most powerful, also covers
   tiling WMs without a session manager
4. If all three fail: log once, no-op (graceful degradation)

In practice, only #1 is needed; #2 and #3 are paranoia we
can land lazily if a user reports a problem.

**macOS** (out of scope but trivial to add later): either
spawn `caffeinate -dim` as a subprocess for the lifetime of
our process, or call `IOPMAssertionCreateWithName` via
PyObjC.

### Architecture

`cvfr_routemaster/screen_keepalive.py` (new, ~100 lines):

```
class ScreenKeepalive:
    def activate(self) -> None: ...
    def deactivate(self) -> None: ...
    def is_active(self) -> bool: ...
```

Platform fork inside the class via `sys.platform`:
- `"win32"` → ctypes call
- `"linux"` → QtDBus Inhibit/UnInhibit, fallback chain
- otherwise → no-op (graceful, with a one-shot status-bar
  message so the user knows their platform isn't supported)

Activate is idempotent (calling twice is safe). Deactivate
when not active is safe (no-op). Both contracts are pinned
in tests.

**Wiring into `MainWindow`:**

- Construct in `__init__` after the existing
  display-settings load.
- A new toolbar action **"Keep screen awake"** alongside
  "Show VATSIM traffic" — checkable, persisted via QSettings
  under `keep_screen_awake` (default **ON** — this is a
  flight tool, the whole point is the user is reading the
  screen during flight).
- Toggle slot calls `activate()` / `deactivate()` and
  persists.
- `closeEvent` calls `deactivate()` for clean OS-level
  release. Crash safety is handled for free (see caveats),
  but a clean release is still the right hygiene.

### Caveats to call out in the README

1. **Corporate Group Policy / mandatory lock can override
   our inhibit.** Windows 10/11 Pro and Enterprise machines
   with admin-enforced "lock after N minutes" via GPO will
   lock anyway — we can't fight admin-level policy from a
   user-mode process. Same on Linux with screen-locker
   daemons that ignore `org.freedesktop.ScreenSaver` (rare).
   Document this so a user under corporate GPO doesn't think
   the feature is broken.
2. **We only inhibit *idle-triggered* lock.** Manual lock
   (Win+L, `loginctl lock-session`, `xdg-screensaver lock`)
   still works. That's exactly the right scope — the user
   should always be able to lock the machine on demand.
3. **Crash safety is automatic on both OSes.** Windows
   execution state is per-thread, released when the process
   exits. Linux D-Bus inhibit is tied to the bus connection,
   dropped automatically when our process disconnects. We
   never leave the OS in a "permanently can't lock" state
   even on a hard crash.
4. **Default-ON is a small security trade-off.** A user who
   walks away from the computer mid-route-plan won't get an
   idle lock. Mitigation: the toggle is in plain view in the
   toolbar; user can disable for non-flight sessions. The
   persisted state means they only have to set their
   preference once.

### Tests

- Platform-mocked unit tests:
  - `win32` path: mock `ctypes.windll.kernel32` and assert
    `SetThreadExecutionState` is called with the right flag
    composition on `activate()` and the bare `ES_CONTINUOUS`
    on `deactivate()`.
  - `linux` path: mock `QDBusInterface` and assert the
    Inhibit/UnInhibit call sequence and cookie round-trip.
    Also test the fallback chain (mock #1 to fail, assert
    #2 is tried).
- Idempotency tests: `activate()` twice → still one inhibit
  held. `deactivate()` when not active → no-op, no error.
- Toolbar wiring tests: toggle ON calls `activate`, toggle
  OFF calls `deactivate`, state persists across simulated
  app restarts.
- **No live OS-level tests** in CI — they'd require a real
  desktop session, and the unit-level mocking covers the
  contract.

### Risks (known unknowns)

1. **Qt D-Bus availability in our PyInstaller bundle.**
   `PySide6.QtDBus` is part of the standard PySide6 wheel
   on Linux. Worth a one-line sanity check during the
   build that the module imports cleanly from the bundled
   `.exe` (`cvfr-routemaster-linux.spec` may need
   `hiddenimports=["PySide6.QtDBus"]` if PyInstaller's
   scanner doesn't pick it up — common gotcha).
2. **D-Bus session bus not available** in headless / SSH
   contexts. Our app is GUI-only, so a session bus should
   always be present, but the fallback chain handles a
   missing bus gracefully (catch `QDBusError`, no-op).
3. **Two app instances both inhibiting** — perfectly safe.
   Each instance holds its own cookie; releases are
   independent. No coordination needed.

### Implementation sequencing (small enough to land in one go)

Two PRs make sense for cleanliness:

1. **PR 1**: `screen_keepalive.py` + tests, no GUI wiring.
   Pure unit tests, exercises the platform fork, easy to
   review.
2. **PR 2**: toolbar action + persistence + `MainWindow`
   wiring + README caveat. About 30 lines of `main_window.py`
   diff plus a small `tests/test_ui_layout.py` extension to
   assert the new toolbar action is wired up.

---

## Open questions to pin down before coding

If the directions above hold, these are the knobs to lock down first.
None of them changes the architecture — they're settings on the same
plan.

1. **Traffic icon size — one global value, or one per profile (normal
   vs. airplane)?** Default: global; can split later.
2. **Wake category color palette.** L=blue, M=green, H=orange,
   J/Super=purple is the working proposal. Easy to bikeshed; pick
   whatever colorblind-safe scheme reads at a glance.
3. **Satellite tile provider for v1: Esri or Stadia?** Default: Esri
   (no key, public, well-tested) with the template configurable in
   QSettings.
4. **Satellite mode behaviour while in airplane mode** — silently
   disable, or honor the user's last choice? Default: honor.
5. **Should the "Show VATSIM traffic" toggle persist across launches**
   like font sizes/window layout, or default off every launch?
   Default: persist.
6. **Renaming `font_settings_dialog.py` → `display_settings_dialog.py`**
   is a multi-file find-replace including object names used by tests.
   Mild preference: keep filename, expand scope. Less churn for any
   test that targets the dialog.
7. **Screen-keepalive default — ON or OFF?** Proposal: ON.
   This is a flight-following tool; the user can disable it
   in route-planning sessions if they want the machine to
   lock at idle.
8. **Screen-keepalive: only display, or also system sleep?**
   Proposal: include system sleep
   (`ES_SYSTEM_REQUIRED` on Windows, `idle:sleep` on
   systemd-logind). A flying laptop on battery saver will
   otherwise suspend mid-flight if the user isn't touching
   the keyboard.
9. **Screen-keepalive UI placement: toolbar or Display
   Settings dialog?** Proposal: toolbar — it's a thing the
   user might want to flip per-session, not a one-shot
   preference.

---

## Release pitfalls worth knowing now

Per `.cursor/rules/build-releases.mdc`:

- **PyInstaller warn-file scanner.** Both new modules will import
  stdlib `urllib.request`, `json`, `concurrent.futures` — already in
  `sys.stdlib_module_names`, so no scanner action. NumPy is already
  in `hiddenimports`. We don't need spec changes for either feature.
- **The bundled `aircraft_wake.json`** must be added under
  `cvfr_routemaster/resources/` and listed in `cvfr-routemaster.spec`
  `datas=[…]` AND the Linux spec (`cvfr-routemaster-linux.spec`).
  Forgetting the Linux spec is the most common build-tree-drift bug —
  worth a bullet on whatever PR description ends up landing this.
- **Cache mtime restamping.**
  `.cursor/rules/build-releases.mdc` mentions
  `_restamp_cache_fingerprints.py`. The satellite tile cache should
  NOT be restamped — it's user-data, not a fingerprinted seed cache.
  The aircraft wake JSON is a frozen build artifact and doesn't need
  restamping either (it's not a fingerprinted cache file in our
  sense).
- **Qt D-Bus on Linux (v4).** `PySide6.QtDBus` is part of the
  standard PySide6 wheel, but PyInstaller's static scanner
  occasionally misses dynamic Qt module loads. If a built Linux
  binary fails to import `QtDBus` at runtime, add
  `"PySide6.QtDBus"` to `hiddenimports` in
  `cvfr-routemaster-linux.spec`. The Windows spec doesn't need
  this — the Windows code path uses `ctypes` against
  `kernel32.dll`, which is always present and not a Python
  dependency at all.

---

## Implementation sequencing

1. **Feature 1 first** (lower risk, fewer decisions, fully testable
   offline once we have a fixture JSON).
2. Inside Feature 1, do the dialog scope rename + icon-size
   persistence as a self-contained prereq before wiring the actual
   traffic — that way we can land "Display Settings dialog with an
   unused icon-size knob" as one PR and "actual planes on the map"
   as the next.
3. **Feature 2 second**; mostly self-contained but has a one-time
   numpy-warp performance pass that's worth doing carefully.
4. **Feature 3 (v4 — screen keepalive) is the natural pause-point
   between v2 and v3** if we want a quick win between two larger
   pieces of work. ~Half a day, two small PRs (module + tests, then
   GUI wiring). Doesn't depend on anything else in this file and
   doesn't block anything else either, so it can also slot in
   whenever the user asks.

---

# Cleanup tech-debt — picked up 2026-05-27 (defer to next session)

Captured during the pre-git cleanup pass on 2026-05-27. The
cleanup itself landed (20 dead files removed, `.gitignore`
extended, all `C:\flying\cvfr-routemaster` path leaks scrubbed
from tracked sources/fixtures/docs, regression at 1268 passed),
and both Windows + Linux releases were built clean for the
evening's flight. The items below were deliberately deferred so
the release could ship; they're queued for the next session in
priority order.

## 1. Build-system rework (highest priority — promised next session)

**Today's state.** Both releases are still driven by:

- `.cursor/rules/build-releases.mdc` — the human/LLM-readable
  cookbook (gitignored, stays local).
- `scripts/build_release.py` (Windows) and
  `scripts/build_release_for_linux.py` (Linux) — the actual
  end-to-end pipelines. Self-contained Python; the cookbook is
  narration on top.
- `scripts/_wsl_build_linux.sh` — three-quoting-layer dodge that
  invokes the Linux script under WSL Debian's bash with the
  build venv activated.

**Why this is tech debt.** Building requires either reading the
cookbook (an LLM in the loop) or knowing the script names + flags
by heart. The user wants an actual build system: a single
declarative entrypoint per target, with named recipes, that
doesn't need narration to drive.

**What to build.** Python's equivalent of a Makefile — i.e.
either:

- **`invoke`** (pyinvoke.org) — `tasks.py` with `@task` decorators,
  invoked as `invoke build-windows` / `invoke build-linux`. Pure
  Python, well-suited to a project that's already Python-only.
  Tasks compose; first-class support for "depends on" + "runs
  in".
- **GNU Make + recipes calling the existing scripts** — works
  natively on Linux/WSL but needs a fallback on Windows
  PowerShell. Adds a non-Python dependency the dev box
  already has via WSL but feels off-tree for a Python project.
- **`hatch run` / `tox -e`** — environment-aware recipes inside a
  `pyproject.toml`. Pulls in `hatch` as a build dep but
  consolidates env config in one file.

**Recommendation.** `invoke`. Reasons: zero new ecosystem (pip
install + tasks.py); per-recipe Python so the `_wsl_build_linux.sh`
quoting dodge becomes a one-liner inside a `@task`; idempotent
re-runs are trivial because the existing scripts already are.

**Recipe set to ship:**

```
invoke regression       # py -m pytest -q
invoke build-windows    # scripts/build_release.py
invoke build-linux      # wraps wsl + scripts/build_release_for_linux.py
invoke release          # regression -> build-windows -> build-linux
invoke clean            # rm -rf build dist release release-linux
```

**Non-goals for this session of work.** Don't try to replace the
existing build scripts; just put a thin task layer in front so
they can be invoked without consulting the cookbook. The cookbook
itself can stay gitignored as a local cheatsheet.

## 2. `test_ui_layout.py` test-pollution hang — RESOLVED (v3.3 candidate)

**Was:** Two tests
(`test_airplane_mode_toggle_hides_map_and_waypoint_panes` and
`test_hide_usage_hints_survives_airplane_mode_round_trip`)
appeared to hang the pytest worker indefinitely when the full
file ran, but passed individually in ~2 s.

**Root cause (confirmed by direct measurement).** The
`main_window` fixture's teardown
(`w.close(); w.deleteLater(); qapp.processEvents()`) called
`processEvents()` exactly once. That is not enough to drain
Qt's deferred-delete queue for a tree of the `MainWindow`'s
depth — a one-off probe at `tests/_leak_probe.py` (since
removed) measured `len(QApplication.topLevelWidgets())`
growing linearly as `[2, 4, 6, 8, 10]` across five
construct/teardown cycles. Each cycle leaked the `MainWindow`
itself plus one `QFrame` that's the popup view of a
`QComboBox` inside the window (popups are always top-level on
Qt because they need their own window). By the time the
airplane-mode tests ran, the module-scoped `QApplication` was
hosting ~30 zombie `MainWindow` trees and the
`setStyle("Fusion") + setStyleSheet(...)` polish pass each
toggle fires off was walking thousands of widgets, hence the
multi-minute "hang".

**Fix.** `tests/test_ui_layout.py`'s `main_window` fixture
teardown was changed to:

```python
w.close()
w.deleteLater()
for _ in range(3):
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
```

The explicit `sendPostedEvents(..., DeferredDelete)` drains
the deferred-delete queue without waiting for the next idle
iteration; three rounds is enough for the `MainWindow`
hierarchy's depth (re-measured at `[0, 0, 0, 0, 0]` post-fix).
The same probe also showed that this drain pattern is safe
ONLY because the fixture doesn't call `w.show()` — drained
shown widgets produce an `ACCESS_VIOLATION` (0xC0000005) on
Windows, retained as a comment in the fixture for future
contributors.

A small production-side guard was added in the same pass:
`RoutePanel._redistribute_airplane_column_widths` now
catches `RuntimeError` from the early
`self._table.horizontalHeader()` / `self._table.viewport()`
access path. The same `QTimer.singleShot(0, redistribute)`
scheduling race that exposed the teardown order also exists
in production if a user closes the window between an
airplane-mode toggle and the next layout pass; the guard is
narrow (early-return on dead-wrapper access) and benefits
both worlds.

**Net effect.** Full regression (`python -m pytest -q`) now
runs end-to-end in ~8 min with `1467 passed, 2 skipped, 0
failed`. The `-k "not test_airplane_mode_toggle_hides_map and
not test_hide_usage_hints_survives_airplane_mode_round_trip"`
filter is no longer required and has been removed from the
build-releases cookbook.

## 3. Stale module-docstring sweep (medium priority — polish)

Currently the codebase has narrative pointers like
`(v2 feature — see ROADMAP-NEXT.md)` and `(Phase 3 — see
ROADMAP-NEXT.md)` scattered through:

- `cvfr_routemaster/main_window.py` (3 sites)
- `cvfr_routemaster/traffic_overlay.py` (top-of-file)
- `cvfr_routemaster/settings_store.py` (3 sites)
- `cvfr_routemaster/font_settings_dialog.py` (1 site)
- `cvfr_routemaster/vatsim_feed.py` (top-of-file)

These pointers were correct at the time they were written and
still point to a real file in the tree, but the "Phase N" /
"v3" labels are stale (everything those phases gated is now
landed). Pure copy-editing pass. Lowest risk; lowest value
relative to (1) and (2).

**Suggested rewrite.** Replace `(v2 feature — see ROADMAP-NEXT.md)`
with the actually-relevant cross-reference (e.g. "see
`vatsim_feed.py` for the datafeed parser"). Drop "Phase N"
entirely — it was useful while we were doing waterfall planning;
now everything is shipped and the labels are noise.

## 4. Unused imports + dead public helpers (low priority — polish)

The pre-cleanup audit identified roughly:

- ~13 unused imports across the production modules (Ruff would
  surface these in one pass with `ruff check --select F401`).
- ~8 dead public helpers — symbols exported via `__all__` or
  `from module import name` patterns but no longer called
  anywhere in the tree.

Defer until after (1) lands — the build-system rework will
naturally surface any helpers that the build scripts no longer
reach, and we can audit while the build refactor is fresh.

**Test gate.** Anything removed needs `rg "<symbol>"` to show
zero remaining callers in tracked code (scratch/ doesn't count;
it's gitignored).

## 5. Long-form ROADMAP.md trimming (low priority — polish)

`ROADMAP.md` is ~2800 lines and accreted history-as-narrative.
After git-init lands, the resolved sections become git log
entries by definition, and we can collapse the "Resolved"
chronicle into a tight summary at the head with the deeper
notes folded under a `<details>` block (or just deleted, since
`git log` is the canonical history).

Lowest priority of all — `ROADMAP.md` is for the dev, not
shipped to users, and reading is optional.
