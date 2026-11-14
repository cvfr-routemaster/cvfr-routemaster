# CVFR Route Master — roadmap

Phased plan for the app. Update this file when scope or status changes so we can reference it in design and reviews. Design records for newer features (VATSIM traffic, satellite view, parked requests, cleanup debt) live in `ROADMAP-NEXT.md`.

---

## P0 — Route spreadsheet contract

| Item | Status |
|------|--------|
| `route_ods_spec.py` — sheet name, row layout, Hebrew/English headers, footer labels, route-code row | Done |
| `scripts/verify_route_ods_examples.py` — verify example ODS files against the spec | Done |
| Example ODS at repo root (e.g. `LLHA-LLIB.ods`, `LLIB-LLHA.ods`) — **living** test fixtures; expect them to change as features grow | Done |

---

## P1 — Georeferencing (charts ↔ lat/lon)

| Item | Status |
|------|--------|
| Per-sheet calibration (`geo_calibration.py`): Lambert Conformal Conic source plane + LSQ fit to anchor waypoints, joint two-sheet alignment | Done |
| Persist `.cvfr_routemaster/geo_calibration.json`; invalidate on PDF fingerprint / map-layout mismatch | Done |
| Calibration flow: auto-picked anchors + Shift+click per anchor with a precision reticle; instruction dialogs | Done |
| Unit tests for geo math and persistence | Done (`tests/test_geo_calibration.py`) |

---

## P2 — Route on the map + export

| Item | Status |
|------|--------|
| Route data model + per-segment math (`Route`, great-circle distance, true/magnetic bearing, time formatting) | Done (`route.py`) |
| Shift+left / Shift+right add / remove route points; user-clicked intermediates | Done |
| Route panel: cruise speed, segment table, ATC columns, cell overrides, ICAO Field 15 string | Done (`route_panel.py`) |
| Route polyline drawn on the chart | Done (`MainWindow._redraw_route_overlay`) |
| Persist route: Save / Load plan as a single-line ICAO Field 15 `.cvfr` file | Done (`route.parse_icao_route_string`, `MainWindow._on_save_plan_requested` / `_on_load_plan_requested`) |
| Export route: **CSV** first, then **ODS** / **XLS** aligned with `route_ods_spec` | Not done |

---

## Resolved — summary

Shipped through v4.1 (commit `f1770f6`). The detailed per-fix
narratives that used to live here (root causes, measurements,
rejected alternatives) are preserved in git:
`git show f1770f6:ROADMAP.md`, section "Resolved (since last
roadmap pass)". Search it for a feature name before re-investigating
an old bug.

**Release pipeline (Windows + Linux).**
Single-file PyInstaller Windows build with bundled Tesseract and a
clean subfolder layout; Linux `--onefile` ELF for Debian 13 built
under WSL (`_wsl_build_linux.sh`), with a shipped runtime-deps check
and Qt plugin bundling fixed. Build-time guard against missing
top-level imports (PyInstaller warn-file scanner with a stdlib
filter; `collections.abc` false positive fixed). Cache-fingerprint
mtime restamping for WSL-built releases. Shipped `map_layout.json`
and calibration so first launch works on any machine. AGPLv3
`LICENSE` + source bundle in the Windows release.

**Satellite imagery.**
Per-tile overlay (one pixmap item per Web Mercator tile) placed with
an 8-DOF projective transform per tile, closing the seam gaps the
earlier 6-DOF affine left. Four zoom layers (z=12 → z=15) fetched
coarsest-first. Chart-pixmap seam partition (with an asymmetric
one-row extension) for satellite tiles and waypoint markers.
Startup-freeze fixes (no GUI-thread cache seeding, no refresh of
invisible overlays, `DirectConnection` on `finished → quit`) and a
parallel stop-and-terminate shutdown.

**Altitude-arrow matcher.**
Phantom-arrow suppression; strict per-segment radius for real
waypoints and a looser one for intermediates; past-endpoint gates;
on-segment matches beat past-endpoint ones; shared-bend rescue;
real body-axis bearing for bidirectional arrows; wide-corridor
rescue. Pinned by the LLHZ↔LLHA round-trip regression fixtures in
`tests/fixtures/altitude_regression/`.

**Route panel.**
Intermediates with ordinal labels, ICAO Field 15 string (optional
intermediate coords), four ATC-handoff columns with a visibility
toggle, MAG BRG / Alt / Dist cell overrides, max-altitude suffix on
the totals line, grid-formatted `Ctrl+C`, Save / Load plan with an
origin→destination default filename, route-add snap radius 1.0 nm,
links from route cells to the chart and external map providers.

**UI / chrome.**
Toolbar in titled groups (Program Settings, Map Type, View Toggles,
Satellite View Options, Program Information) with Garmin-green
checked state; Display Settings dialog (font profiles incl.
airplane mode, traffic-icon and waypoint-marker sizes) plus
Ctrl+wheel resizing; unified hint styling; simulator-only warning
banners; window geometry and pane sizes persisted; click-to-track
VATSIM plane following; CVFR / LSA dual-mode charts (v4).

---

## Open issues to address next (captured 2026-05-06 evening)

1. **South sheet spontaneously moves on program startup (intermittent).**
   Reported twice in the same session:
   - First report: realigned, could not reproduce on the immediate next start.
   - Second report (with the changes from this session in place): south sheet
     came up *"all the way to the right"* on startup. User did not recalibrate
     and **closed the program in that state**.

   *Important correction:* the saved sheet layout actually lives in
   ``QSettings`` (Windows registry, ``CVFRRouteMaster/CVFR Route Master``),
   **not** in ``map-layout.json``. The only file under
   ``.cvfr_routemaster/`` that holds layout-shaped numbers is
   ``geo_calibration.json``, and that records the *lock-state* layout at the
   moment of calibration (used to invalidate calibration if the sheet moves).
   ``MainWindow.closeEvent`` does **not** persist the sheet layout — only the
   view transform/scroll. So a session that didn't Alt+drag or Alt+wheel
   should leave QSettings unchanged. That contradicts the "closed in the bad
   state, so the bad layout is now saved" theory; the next reproduction needs
   to settle whether the bad position came from QSettings (loaded from disk)
   or from something inside the running session.

   Things this session ruled out (for context, not as proof):
   - None of the new edits call `setPos` / `setScale` on the sheet items.
   - No new `persist_map_layout()` invocations were added; only
     `_redraw_route_overlay()` and `_refresh_scene_rect()` were added inside it.
     Neither of those moves items.
   - The Reporting-link click handler only pans the *view*, never the sheets.

   **Diagnostics now in place** (2026-05-07):
   - New module `cvfr_routemaster/layout_diag.py` writes a rotating debug
     log to `.cvfr_routemaster/sheet-layout-debug.log` (~256 KiB × 5 files,
     UTF-8). Every line is `ISO-timestamp | event | k=v ...`, with `None`
     rendered as `-` so a missing sheet item is unambiguous.
   - Snapshots of `(north pos/scale/pix_w/pix_h, south pos/scale/pix_w/pix_h)`
     are taken at every meaningful boundary: session start, the moment
     pixmaps become available in `_on_map_finished`, the result of
     `load_map_layout()` (with all loaded fields), immediately after sheet
     `setPos`/`setScale` is applied, after scene setup, on entry/exit of
     `_apply_saved_map_view`, before `persist_map_layout` writes to
     QSettings, around `_reset_map_layout_confirm`, around Alt+wheel scale,
     at `alt_drag.start` / `alt_drag.end`, and at `close_event.before_persist`
     immediately before window close. Per-event mouse-move deltas are
     deliberately *not* logged — they would flood the file and the start/end
     pair already tells us the net delta.

   How to use the log on next reproduction:
   1. Reproduce the bad state (don't recalibrate, don't move the sheets).
   2. Close the app cleanly — `close_event.before_persist` will record the
      sheet positions at that exact moment.
   3. Start the app again. Compare in the log:
      - `on_map_finished.load_map_layout` (what came back from QSettings).
      - `on_map_finished.after_apply` (positions after `setPos` ran).
      - The last `close_event.before_persist` from the previous session.
      A mismatch between the close-time snapshot and the next start's
      `load_map_layout` would prove the bad layout came in via something
      *other* than the explicit persist paths.
   4. Search the previous session's log for `persist_map_layout.about_to_save`
      and `alt_drag.*` — that's the complete list of sheet-layout writes.

   Still worth looking at if the log alone isn't conclusive:
   - `_invalidate_geo_if_layout_mismatch()` only rewrites
     `geo_calibration.json`; it does **not** touch QSettings. If the bad
     position is appearing in QSettings without an `alt_drag.*` /
     `sheet.alt_wheel_scale` / `reset_map_layout.*` event, the call is
     coming from somewhere not yet instrumented and the snapshot pair around
     each Qt slot is the next place to add hooks.
   - The autoload path between "PDF rendered into pixmap" and "saved layout
     applied". The new `on_map_finished.pixmaps_ready` event records pixmap
     dimensions at that boundary, so we can see immediately if the pixmap
     came back at unexpected dimensions before scaling.

   Workaround for now: realign cleanly so the persisted layout is the good
   one. The diagnostic file will tell us next time whether the bad position
   loaded from disk or was applied at runtime.

2. **First Shift+route-click feels laggy again (Windows).** Test-driving on
   May 14, 2026 the user observed *"the old 'first route click takes a long
   time' problem is back"*. The original symptom was on Linux/Debian cold
   starts (fontconfig walk on first Hebrew paint); fixed at the time by
   ``_warm_text_rendering_caches`` (``main_window.py`` ≈ L259–319) scheduled
   via ``QTimer.singleShot(0, ...)`` from the MainWindow constructor. That
   wiring is still in place and the function is unchanged. What's likely
   regressed is the *first-click rendering path*, which has grown since the
   warmup was sized for it.

   Subsequent route clicks feel instant — classic "first paint is cold,
   then everything is warm" pattern.

   Shortlist of suspects (in order of likelihood), with the work each one
   does on first click and why the existing warmup doesn't cover it:

   - **(A) Warmup primes fontconfig + HarfBuzz but probably never primed
     the glyph cache on any platform.** The warmup draws ``"דרום LLHA
     1600"`` into a ``QPixmap(1, 1)`` with ``painter.drawText(0, 0,
     sample)``. The text bbox is essentially entirely above-and-right of
     the 1×1 destination clip rect, so Qt's modern QPainter clip-rejection
     shortcut can compute the bbox (which exercises font fallback +
     shaping) and skip glyph rasterisation. The fontconfig walk that hurt
     Linux gets primed; the per-glyph rasteriser cache (the dominant cost
     on Windows DirectWrite/GDI) likely doesn't. Almost certainly always
     been a partial fix; the symptom just wasn't user-visible on Windows
     when system caches happened to be warmer.

   - **(B) Per-widget ``setStyleSheet`` calls run for the first time on
     first click.** ``RoutePanel._render`` (``route_panel.py`` ≈ L1261–
     1283) calls ``setStyleSheet`` on the Latin route-string label
     (``font-family: 'Consolas'...``) and again on the Hebrew route-
     string label (``font-weight: bold;``) every render. The app-wide
     stylesheet from ``ui_theme.apply_dark_theme`` has ~18 top-level
     selectors; the first per-widget ``setStyleSheet`` resolution against
     a complex sheet forces a cold-cache QSS recompute that the warmup
     doesn't touch at all (it's a font-cache primer, not a style-engine
     primer). These calls also fire on every subsequent render, but
     that's cheap once the style cache is warm — the cold call eats the
     visible cost.

   - **(C) Hebrew label is created hidden, first ``.show()`` happens on
     first click.** ``self._hebrew_string_label.hide()`` (``route_panel.py``
     L459) keeps the label out of the layout until a non-empty route
     exists; ``_render`` calls ``.show()`` on first click (L1283).
     That defers the first paint event for the Hebrew QLabel from
     startup (where the splash hides the cost) to first interaction —
     compounding (A) because the glyph cache fault, if any, fires while
     the user is watching. If the label were created visible with empty
     text, that cost would land at startup instead.

   **Confirmed not the cause:**
   - The user's live session predates today's snap-radius edits (session
     start at 16:01:46 per the layout debug log; my edits at 18:25-18:26
     never loaded into the running process).
   - ``_compute_altitudes_for_route`` returns ``None`` early for a single-
     point route (no segment) — the 633-arrow projection only runs from
     the *second* click onward.
   - ``find_nearest_waypoint`` is a linear scan over 198 records, ~5 µs.
   - ``_redraw_route_overlay`` on first click adds one
     ``QGraphicsEllipseItem`` (the origin dot); no polyline yet.
   - Altitude arrows are already loaded at startup
     (``altitudes.extracted`` event in ``sheet-layout-debug.log``); no
     lazy load happens on click.
   - The QTimer at 0 ms (warmup) fires before the autoload at 150 ms, so
     they don't compete for the event loop.

   **Confirmation plan when we pick this up.** A ~5-minute instrumented
   run should pin down which of (A)/(B)/(C) dominates:

   1. Wrap the body of ``RoutePanel._render`` with
      ``t0 = time.perf_counter(); ...; print(f"render={time.perf_counter()-t0:.3f}s")``.
   2. Wrap each of the three suspect blocks individually:
      - Latin label ``setText`` + ``setStyleSheet``.
      - Hebrew label ``setText`` + ``setStyleSheet`` + ``.show()``.
      - ``MainWindow._redraw_route_overlay``.
   3. Click one chart waypoint. Whichever block dwarfs the others is
      the culprit.

   **Likely fixes once confirmed.**
   - For (A): bump the warmup pixmap to a size large enough to defeat
     Qt's clip shortcut (e.g. ``QPixmap(256, 64)`` with
     ``painter.drawText(10, 40, sample)`` so the text sits well inside
     the dest rect) — primes the glyph cache for real.
   - For (B): hoist the two ``setStyleSheet`` calls in ``_render`` into
     the ``RoutePanel`` constructor so the QSS resolution happens at
     startup, not on first interaction.
   - For (C): show the Hebrew label from the start with empty text
     (set ``setMinimumHeight(0)`` if vertical jitter matters) so the
     first Hebrew paint happens at startup under the splash, not on
     first click.

   Any one of those would likely cure the symptom; doing (A)+(C)
   together would be the most robust ("warmup actually primes the
   right caches" AND "first paint happens off the user's critical
   path").

---

## Later ideas

Add rows here when brainstorming (optional epics, UX polish, performance). Keep P0–P2 above as the agreed backbone unless we explicitly reprioritize.

- **Hebrew UI translation (i18n).** *Now tracked in `ROADMAP-NEXT.md`
  → "Parked feature requests" → "Hebrew localization of the whole
  interface" (user-requested 2026-11-14); original note kept below.*
  Wrap user-visible strings in `tr()` /
  `QCoreApplication.translate(...)`, ship a Hebrew `.ts` / compiled `.qm`
  via Qt Linguist, and add a Settings toggle (or auto-detect from
  `QLocale.system()`) so VatIL pilots who prefer Hebrew can flip the UI.
  Needs RTL handling — `QApplication.setLayoutDirection(Qt.RightToLeft)`
  for window chrome, but the chart pane and route table must stay LTR
  (lat/lon / bearings / frequencies / ICAO codes are inherently
  left-to-right) so the layout direction needs to be applied per-widget,
  not globally. Reporting-name column already renders Hebrew correctly,
  so the existing font stack is a known-good baseline to extend from.
