# CVFR Route Master — roadmap

Phased plan for the app. Update this file when scope or status changes so we can reference it in design and reviews.

---

## P0 — Next session

- **Verify Save / Load Flight Plan feature end-to-end.** Added this
  session (May 13, 2026) but the full pytest suite was interrupted
  before completing on Windows, and the human has not yet exercised
  the new buttons in the running app. Next-session checklist:
  1. **Run the full test suite** — `python -m pytest -q` from the
     repo root. The two new test files (`tests/test_flight_plan.py`,
     additions to `tests/test_route_panel.py` and
     `tests/test_ui_layout.py`) passed in isolation (125/125 across
     those three files), and the rest of the suite was green just
     before this feature landed, so a quick sanity-pass is all
     that's needed; if anything fails it's almost certainly a
     downstream test that imports the parser symbols and needs a
     trivial fix.
  2. **Manual smoke test in the app** (Windows dev OR Linux WSL via
     `release-linux/run-on-wsl.sh`):
     * Build a small route (Shift+click 2–3 chart waypoints +
       maybe an intermediate).
     * Confirm the three buttons in the Route pane header row
       read **Save plan | Load plan | Clear route** with Clear
       rightmost. Tooltip on Save mentions ".cvfr".
     * Click **Save plan** → file dialog opens defaulting to
       `<project_root>/flight-plan.cvfr`; save it under a chosen
       name (e.g. `test-plan.cvfr`).
     * Click **Clear route** → confirm the route is empty.
     * Click **Load plan** → pick the file you just saved → route
       table re-populates with the same points (named fixes + any
       intermediates, in the same order).
     * Negative path: open the saved file in a text editor, append
       a garbage token (e.g. ` GARBAGE`) and save; **Load plan** the
       broken file → should pop a "malformed" warning quoting the
       offending token + position, and leave any previously-loaded
       route untouched.
     * Negative path: edit a code to one that doesn't exist (e.g.
       `ZZZZZ`) → load → should pop an "unknown code" warning
       naming `ZZZZZ` specifically (not the generic "malformed"
       message).
  3. **If everything looks good, mark this P0 done and update
     ROADMAP** — move the feature to the Resolved section with a
     short description of the file format (single-line ICAO Field
     15 string, `.cvfr` extension, strict grammar) and the parser
     contract (formatter ↔ parser are exact inverses; pinned by
     `test_round_trip_idempotent_on_re_format`).

- **Ship release v4 (Windows + Linux).** Both builds are in tree
  with all release-portability fixes (numpy build-time guard, cache
  mtime restamp, shipped `map_layout.json`) **plus the new Font
  Settings feature** (`View → Font Settings…` menu, three knobs:
  tables, route-text labels, usage hints — see *Resolved* below).
  Remaining work is operational:
  1. Linux: `tar -czf cvfr-routemaster-linux.tar.gz -C release-linux .`
     and copy to the VATSIM laptop. Extract, run
     `./check-runtime-deps.sh`, install the suggested apt
     packages, then `./cvfr-routemaster`.
  2. Windows: zip the *contents* of `release/` (not the folder
     itself) and send to your friend / copy to the laptop.

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
| Multi-anchor 6-DoF affine LSQ `(lon, lat) ↔ (u, v)` on each sheet, normalised to the pixmap (≥ `MIN_ANCHORS = 3`, default capture flow uses 4) | Done (`geo_calibration.py`) |
| Persist `.cvfr_routemaster/geo_calibration.json` | Done |
| Invalidate when PDF fingerprint or **map layout** (position/scale) no longer matches | Done |
| Toolbar: calibrate north/south, cancel, clear, calibration instructions | Done |
| Calibration flow: auto-picked, well-spread anchor waypoints per sheet (count = `_CALIBRATION_ANCHOR_TARGET`) + one **Shift+click** per anchor on its triangle centre (prompted, with a precision reticle cursor) | Done |
| Instruction dialog when calibration is missing or stale; optional follow-up if only one sheet is done | Done |
| Map: calibration click handling; arrow cursor; Ctrl+drag pan | Done (`map_graphics_view.py`) |
| `MainWindow.lonlat_on_sheet_scene` for overlays / tests | Done |
| Unit tests for geo math and persistence | Done (`tests/test_geo_calibration.py`) |

---

## P2 — Route on the map + export

| Item | Status |
|------|--------|
| Route data model + per-segment math (`Route`, great-circle distance, true/magnetic bearing, time formatting) | Done (`route.py`, `tests/test_route.py`) |
| Shift+left adds nearest waypoint to the route, Shift+right removes nearest route point; consecutive duplicates ignored | Done (`MainWindow.try_route_click`, `map_graphics_view.py`) |
| Left-pane route panel: cruise-speed input, segment table (FROM / TO / MAG BRG / dist nm / HH:MM:SS), >180 kt CVFR warning | Done (`route_panel.py`) |
| Draw route on the chart using calibration | Done (`MainWindow._redraw_route_overlay` — semi-transparent red marker-pen polyline, redrawn on route / calibration / sheet-layout changes) |
| Persist route / polyline in the project | Not done |
| Export route: **CSV** first, then **ODS** / **XLS** aligned with `route_ods_spec` | Not done |

*Note: waypoint export to CSV exists today; route-specific export is still P2.*

---

## Resolved (since last roadmap pass)

- **Click-to-track plane following + main-toolbar group restructure
  (CSV export button removed).** Two related UX changes shipped
  in one session.

  **Plane tracking.** The user wanted to pick a VATSIM pilot on
  the chart and have the viewport stay framed around them on
  every VATSIM update (~15 s cadence), with two-thirds of the
  viewport ahead of the plane along its heading and one-third
  behind — i.e. for a westbound plane the icon should sit 1/3
  from the right edge, centred vertically; for a southbound
  plane 1/3 from the top edge, centred horizontally; diagonals
  scale proportionally on both axes.

  Interaction model (after iterating with the user): a plain
  left click on a traffic plane starts tracking that callsign;
  a plain click anywhere else on the chart releases. No new
  toolbar button — the gesture is purely click-based. Visual
  selection cue is a yellow halo ring around the silhouette of
  the tracked plane, drawn behind the existing
  black/white/black border so the wake-category icon still
  reads as the primary signal.

  Implementation lives in four pieces, each independently
  testable:

  * `cvfr_routemaster/plane_tracking.py` — a pure
    `compute_tracking_view_center(plane_scene_pos, heading_deg,
    view_w_px, view_h_px, view_scale) -> QPointF` helper that
    encodes the 2/3-ahead / 1/3-behind framing math. Qt-light
    (only imports `QPointF` for the return type) so the
    `tests/test_plane_tracking_math.py` cardinal + diagonal
    suite runs without spinning a real view.
  * `cvfr_routemaster/traffic_overlay.py` — `_TrafficPlaneItem`
    gains a `_selected` flag + `set_selected(bool)` +
    `callsign` property; `paint()` adds a yellow ring pass
    when `_selected`. `TrafficOverlay` carries the tracked
    callsign at the manager level (because the per-pilot
    items get torn down + rebuilt on every snapshot) and
    re-applies the visual on every `set_pilots(...)` rebuild.
  * `cvfr_routemaster/map_graphics_view.py` — sub-threshold
    plain-click branch in `mouseReleaseEvent` now does a
    `_hit_test_traffic_callsign(scene_pt)` before falling
    through to the existing sheet-selection logic. Plane hit
    → `controller.set_tracked_callsign(hit)`, empty chart +
    active tracking → `controller.set_tracked_callsign(None)`
    (release). The hit-test runs only outside calibration so
    the calibration-hint behaviour stays intact.
  * `cvfr_routemaster/main_window.py` — new
    `_tracking_callsign` state, `set_tracked_callsign(...)` +
    `tracked_callsign()` controller methods (status-bar
    feedback on every transition, idempotent on no-ops), and
    `_recenter_on_tracked_pilot()` which `_on_vatsim_pilots_updated`
    calls after the overlay rebuild. The recenter pass has
    three terminal paths: no-op when not tracking, snap
    `view.centerOn(target)` to the math helper's result when
    the pilot is in the snapshot, clear + status message
    ("Tracking stopped: PILOT_XYZ no longer in feed.") when
    the tracked callsign vanishes. Unprojectable pilots
    (no sheet calibrated, off-chart) are a transient skip
    rather than a disconnect, so a single off-chart blip
    doesn't kick the user out of tracking.

  **Toolbar restructure + CSV removal.** The flat top toolbar
  is now three titled, rounded-border `QFrame` groups: **Program
  Settings** (Map File Settings, Map Calibration Options,
  Display Settings), **View Toggles** (Airplane mode, Hide
  Waypoint View, Hide Usage Hints, Show VATSIM traffic), and
  **Satellite View Options** (Satellite view, Download
  Satellite Imagery). The "Export waypoints to CSV…" entry
  was removed entirely per the user's request — the
  `_export_waypoints_csv` slot method itself is left in place
  (orphan but harmless; a future cleanup pass can delete it).
  "Cancel calibration" stays on the toolbar but sits OUTSIDE
  the three groups so its transient mid-calibration appearance
  doesn't visually disrupt them.

  Each group is a `QFrame` with an object-name-scoped QSS rule
  (rounded 8 px border, bold centred title). Buttons are
  `QToolButton` instances proxying the existing `QAction`
  instances via `setDefaultAction(...)`, so every existing
  tooltip, slot connection, checkable state, icon, and object
  name (`act_open_map_file_settings`, etc.) carries over
  unchanged. The previous toolbar-wide
  `setToolButtonStyle(ToolButtonTextBesideIcon)` is now a
  per-button override applied only to airplane-mode (the one
  action that carries an icon), so the other buttons no longer
  reserve a blank icon column.

  **Tests.** 165 focused tests across five files:
  `test_plane_tracking_math` (16 cardinal + diagonal +
  degenerate-input guards), `test_traffic_overlay` (112; 10
  new in `TestTrackingSelection`), `test_main_window_tracking`
  (16 covering `set_tracked_callsign`, `tracked_callsign`, and
  `_recenter_on_tracked_pilot`), `test_main_window_toolbar`
  (10 pinning the three group frames + CSV absence +
  per-button styles), `test_map_graphics_view` (11; 5 new in
  `TestTrafficClickToTrack`). `test_ui_layout.py` updated:
  `test_main_toolbar_visible_actions_are_only_ten` →
  `..._nine` to reflect the CSV removal, and the
  ``tb.actions()`` selectors replaced with ``findChild(QAction,
  ...)`` because actions wired via `setDefaultAction` no longer
  appear in the toolbar's direct action list.

- **Startup-freeze cascade fixed: skipped GUI-thread seeding,
  skipped invisible-overlay refresh, honest-first-progress
  worker emit.** User reported, after the DirectConnection fix,
  that the app still went in-and-out of "Not Responding" twice
  on startup before becoming responsive, with sat view OFF and
  z=15 download resuming. Three root causes, three surgical
  fixes.

  **Cause 1 — heavyweight GUI-thread seeding.**
  `_check_satellite_resume_on_startup` called
  `_seed_satellite_progress_per_zoom`, which loops every
  configured zoom and runs `tiles_to_fetch_for_bbox` —
  `Path.exists` per candidate tile. On the default
  `[12, 13, 14, 15]` set that's ~107 k stat calls on the GUI
  thread (z=12 ~1.3 k + z=13 ~5.2 k + z=14 ~20 k + z=15 ~80 k),
  freezing the app for 5–10 s right at app launch when the user
  is least tolerant of unresponsiveness. The pre-z=15 default
  topped out at ~26 k stat calls and the docstring claim
  ("well under a second") was accurate; lifting to z=15 broke
  that invariant without anyone noticing.

  **Fix 1.** Drop the seeding call from
  `_check_satellite_resume_on_startup` entirely. The chain's
  per-worker initial progress emit (see
  `SatelliteWorker.start_fetch`) populates each zoom's status-bar
  entry as the chain reaches it; with the coarsest-first order
  a returning user with z=12/13/14 cached sees the full
  multi-line readout populate over ~1–2 s rather than freezing
  for ~10 s. The toggle-on path
  (`_on_show_satellite_toggled`) still seeds eagerly because
  the user explicitly asked to see the imagery and is OK with a
  brief loading state.

  **Cause 2 — invisible-overlay refresh thrash.**
  `_on_satellite_finished` ended with a "final catch-up" call
  to `refresh_from_cache(None)` on both north and south
  multi-zoom overlays. The intent (catch any tiles written in
  the last debounce window before `finished`) was correct under
  the original assumptions; the implementation walks every
  tile item in every per-zoom overlay and runs the lazy-load
  loop. With sat view OFF the overlays' `_last_visible_rect`
  is `None`, which triggers the lazy-load loop's "no visible
  rect known yet, conservatively load everything" branch —
  every single tile item gets a `TileCache.get` call (which is
  a file read for cache hits, a stat for cache misses). On the
  default `[12, 13, 14, 15]` set that's
  ~(1.3 + 5.2 + 20 + 80) k = ~107 k cache reads *per sheet*,
  ~213 k per chain transition. The user's coarsest-first
  startup triggered three back-to-back transitions through
  cached z=12, z=13, z=14 — multi-second GUI freeze each, all
  for *invisible* work.

  **Fix 2.** Guard the catch-up refresh on
  `self._act_show_satellite.isChecked()`. With sat view OFF,
  skip the refresh entirely — tiles aren't visible, the work is
  pure overhead. When the user later toggles sat view ON,
  `_on_show_satellite_toggled` does a full refresh via
  `eager_load_all_cached` which catches up any tiles we
  skipped here.

  **Cause 3 — initial-progress flicker on all-cached path.**
  With seeding removed, the worker's progress emits become the
  only signal the GUI status label gets. On the all-cached fast
  path (`to_fetch` empty after enumeration), the worker emitted
  `(completed_base, total)` first — which for secondary zooms
  is `(0, total)` — and then immediately emitted `finished`,
  which the GUI handler followed up by promoting the entry to
  "done". If Qt happened to paint between the two
  GUI-thread events, the status bar briefly read
  "z=N 0 / N (0 %)" before flipping to "z=N ✓ N tiles" — a
  flicker masked while seeding pre-populated the entry.

  **Fix 3.** Restructure `SatelliteWorker.start_fetch` so the
  all-cached path emits `(total, total)` directly before
  `finished`, skipping the misleading `(0, total)`. The
  non-cached path is unchanged. New pinning test
  `test_full_cache_first_progress_is_total_not_zero` makes
  the contract explicit so a future refactor of the emit
  ordering can't silently regress it.

  **Tests.** 30/30 worker + main-window-workers tests green,
  including the existing `test_initial_progress_tick_fires`
  (which still asserts `(0, total)` first on the non-cached
  path) and the new `test_full_cache_first_progress_is_total_not_zero`
  (which pins the cached-path contract).

- **DirectConnection on `worker.finished → thread.quit` fixes
  90-second UI freeze cascade after chain-order inversion.**
  The user reported the program going in-and-out of "Not
  Responding" repeatedly on startup after the coarsest-first
  chain landed, with satellite imagery toggle OFF. Eventually
  ("finally released") z=15 download resumed normally.

  **Root cause.** Latent queue-ordering deadlock in
  `_cleanup_satellite_worker_refs`, only exposed by the new
  chain order:

  1. `worker.finished` had three connections wired with the
     default `Qt.AutoConnection`: (a) GUI-thread slot
     `_on_satellite_finished`, (b) worker's own `deleteLater`,
     (c) `thread.quit`. The QThread was created with
     `QThread(self)` where `self` is MainWindow, so the QThread
     object's *Qt thread affinity* is the GUI thread — even
     though the OS thread it manages is the worker thread. That
     means `worker.finished → thread.quit` resolves to a
     *queued* connection: the slot call is posted to the GUI
     thread's event queue.
  2. When the worker emits `finished`, two events end up on the
     GUI thread's queue in connection order: the
     `_on_satellite_finished` slot first, then the
     `thread.quit` slot.
  3. The GUI thread processes `_on_satellite_finished`, which
     calls `_cleanup_satellite_worker_refs`, which calls
     `self._satellite_thread.wait(30_000)` — blocking the GUI
     event loop. The pending `thread.quit` event is sitting
     immediately behind it in the queue and can't be
     processed.
  4. The thread's event loop never receives the quit, the wait
     times out after 30 s, the GUI unfreezes, the chain pops
     the next link.

  With the previous finest-first chain order, the first link
  (z=15) took hours of real fetching, so chain transitions
  were rare and the deadlock essentially never manifested.
  With the new coarsest-first order, a returning user with
  z=12/13/14 fully cached and only z=15 to finish would
  trigger *three* chain transitions in rapid succession (z=12,
  z=13, z=14 each enumerate-and-finish in milliseconds via
  `cache.has`), each blocking on the 30 s wait — totalling
  ~90 s of intermittent UI freezing before the actual z=15
  fetch could start. That's exactly the symptom the user
  observed.

  **Fix.** Connect `worker.finished → thread.quit` with
  explicit `Qt.ConnectionType.DirectConnection` instead of
  letting auto-connection pick queued:

  ```python
  self._satellite_worker.finished.connect(
      self._satellite_thread.quit,
      Qt.ConnectionType.DirectConnection,
  )
  ```

  `QThread.quit()` is documented thread-safe, so direct-calling
  it from the emitter (worker) thread is correct. Now when
  `finished` fires on the worker thread, quit runs *immediately*
  on that thread, the event loop exits before the queued
  `_on_satellite_finished` slot is even dequeued on the GUI
  thread, and by the time GUI reaches the
  `_cleanup_satellite_worker_refs.wait(...)` the OS thread is
  already gone — wait returns sub-millisecond.

  Also reduced that wait from 30 s to 2 s as defence-in-depth:
  with DirectConnection it should always be sub-millisecond,
  but a generous cap avoids hanging if something exotic in the
  worker holds a kernel resource on shutdown. Same fix applied
  to the on-demand fetch worker for consistency (same wiring
  pattern, same theoretical risk on its
  `_stop_satellite_demand_worker` polite-wait path).

  **Tests.** The existing 15-test
  `tests/test_main_window_workers.py` suite still passes; the
  deadlock itself isn't directly unit-testable (it's an
  emergent property of Qt's event-loop scheduling), but the
  user-visible symptom is verified by manually restarting the
  app and confirming the chain transitions through z=12 → 13
  → 14 in <1 s collectively before z=15 picks up the actual
  download. Full regression on
  `test_satellite_worker.py`/`test_satellite_demand_worker.py`/
  `test_main_window_workers.py` is 48/48 green.

- **Bulk-fetch chain inverted to coarsest-first + shutdown made
  snappy via parallel-stop-and-terminate.** Two user-reported
  papercuts on the multi-zoom-with-z=15 build, both fixed in a
  single pass with focused tests.

  **Problem 1 (chain order).** A fresh user with no satellite
  tiles cached at all would see the bulk fetcher pull the FINEST
  zoom first (z=15, ~71,600 tiles, ~36 min @ Esri's polite pacing)
  and then drop to z=14, then z=13, then z=12 (~1,300 tiles,
  ~minute). That order was a pre-multi-zoom hand-me-down from
  when the user spent most of their session on the finest zoom
  and "finest first" matched "useful first". Under the current
  `MULTIZOOM_BASE_VIEW_SCALE = 6.0` anchor the default
  fit-to-screen view sits on z=12; the user would stare at a
  gray map for ~36 minutes before any satellite imagery
  appeared at the zoom they actually use.

  **Problem 2 (shutdown hang).** Clicking the red X resulted in
  a multi-second hang plus a `QThread: Destroyed while thread ''
  is still running` warning to stdout. Root cause: the
  `closeEvent` called the three existing `_stop_*_worker`
  methods sequentially, each doing a polite 12-30 s
  `QThread.wait()` after queueing its stop signal. Polite waits
  stack additively, and any worker mid-`urllib.request.urlopen`
  (15 s default HTTP timeout) couldn't process the queued
  `stop_fetch` slot until the HTTP call returned. Worst-case
  shutdown ≈ 57 s of GUI freeze before the window vanished.

  **Fix.** Three coordinated changes in `cvfr_routemaster/main_window.py`:

  1. New pure helper `_plan_satellite_zoom_chain(levels)` returns
     the chain spec as `[(zoom, persist_state), ...]` in
     execution order. Policy: coarsest-first, with
     `persist_state=True` exclusively on the FINEST (last) link.
     For the default `[12, 13, 14, 15]` set that produces
     `[(12, False), (13, False), (14, False), (15, True)]`. Only
     one persist=True link per chain because the state file's
     resume semantics assume one canonical "in-progress" zoom.
     `_satellite_pending_secondary_zooms: list[int]` retired in
     favour of `_satellite_pending_zoom_chain: list[tuple[int, bool]]`
     so per-link persist flags survive the chain-pop in
     `_on_satellite_finished`.

  2. New helper `_stop_workers_for_shutdown(polite_timeout_ms=1500,
     force_timeout_ms=500)` replaces the three sequential
     `_stop_*_worker` calls in `closeEvent`. Three-phase
     algorithm: (a) signal every worker to stop without waiting
     (the worker-specific signal helpers `_signal_vatsim_worker_stop`,
     `_signal_satellite_bulk_worker_stop`,
     `_signal_satellite_demand_worker_stop` each set `_stopped =
     True` and queue the stop slot, then return immediately); (b)
     wait briefly on each thread; (c) `QThread.terminate()` any
     straggler still stuck in I/O. The bounded-stop logic itself
     is extracted as `_force_stop_threads(threads, *,
     polite_timeout_ms, force_timeout_ms)` so it's testable
     without standing up a full `MainWindow`. Worst-case
     shutdown drops from ~57 s to ~2 s.

  3. Why `terminate()` is OK here: on shutdown the app is going
     away regardless, and the satellite cache's tmp-file +
     atomic-rename write discipline limits worst-case damage to
     a single `.tmp` file left behind, which the next launch
     ignores (the non-tmp file either exists, in which case the
     tmp is harmlessly overwritten on next put, or doesn't, in
     which case the tile is cleanly re-fetched).

  **Tests.** New file `tests/test_main_window_workers.py` with
  15 focused tests, all passing:

  - 8 `TestPlanSatelliteZoomChain` tests pinning the policy
    invariants — coarsest-first ordering on default
    `[12, 13, 14, 15]`, legacy `[12, 13, 14]`, single-level,
    unsorted input, duplicates; persist=True appears exactly
    once; persist=True is always on the finest (max) zoom.

  - 7 `TestForceStopThreads` tests, mostly mock-based to pin
    the algorithm (wait → isRunning → conditional terminate →
    wait) and the input-order processing. One real-QThread
    natural-finish test verifies the polite-wait fast path. The
    "real-QThread + terminate" budget test was tried but
    reliably tripped `STATUS_STACK_BUFFER_OVERRUN` interpreter
    aborts on Windows during pytest cleanup (PySide6 cleanup
    racing with TerminateThread-killed Python threads); the
    user-visible behaviour is verified by clicking the red X
    instead.

  Full regression pass: 105/105 tests across
  `test_satellite_worker.py`, `test_satellite_demand_worker.py`,
  `test_satellite_overlay.py`, and the new
  `test_main_window_workers.py`.

- **Multi-zoom default raised to 4 layers (z=12 → z=15) so
  zoomed-in views get fine airport detail, and the anchor lifted
  to 6.0 to slot z=15 in above the existing z=14 boundary
  without disturbing the verified z=12/z=13/z=14 bands.** The
  user reported (a) at default fit-to-screen view scale (~0.5–0.6
  on the Israel calibration) the multi-zoom selector was eagerly
  loading z=14 -- the previous top layer -- at ~7.6× downsampling,
  paying ~16× the tile budget for an image visually
  indistinguishable from z=12, and (b) z=14's ~8 m/px ground
  resolution wasn't quite enough to see runway markings or pick
  out individual taxiway turns when circling unfamiliar airfields.

  **Fix.** Four-part change:

  1. `MULTIZOOM_BASE_VIEW_SCALE` lifted from `1.0` to `6.0`.
     Boundaries on the default `[12, 13, 14, 15]` set:

     - `view_scale > 3.0`   → z=15 active (deep airport detail)
     - `(1.5, 3.0]`         → z=14 active (first-detail layer)
     - `(0.75, 1.5]`        → z=13 active (medium-detail layer)
     - `<= 0.75`            → z=12 active (default fit-to-screen)

  2. `DEFAULT_TARGET_ZOOM` / `DEFAULT_SATELLITE_ZOOM` raised from
     `14` to `15`. Bulk fetch now pulls Israel coverage at z=15
     (~71,600 tiles, ~1.2 GB on disk, ~36 min one-time download)
     instead of z=14 (~17,900 tiles, ~305 MB, ~9 min). z=15 was
     already supported by the URL template + worker code and
     within the `MIN_TARGET_ZOOM..MAX_TARGET_ZOOM` (12..16) range
     -- only the default constant moved.

  3. `_satellite_zoom_levels()` in `main_window.py` reworked from
     the previous "top-2..top" rule to "always 12..top". The old
     rule meant a user setting `satellite_zoom = 15` got
     `[13, 14, 15]` — z=12 dropped out of the fallback stack,
     contradicting the explicit "load z=12 always" preference.
     The new rule always anchors the floor at z=12 regardless of
     the user's top setting:

     - top=14 → `[12, 13, 14]` (same as before)
     - top=15 → `[12, 13, 14, 15]` (was `[13, 14, 15]`)
     - top=16 → `[12, 13, 14, 15, 16]` (was `[14, 15, 16]`)

  4. Status-bar tooltip updated with the four new boundaries
     (view-scale 0.75, 1.5, 3.0) and explicit note that z=12 is
     always loaded as the permanent base layer.

  The boundaries `0.75`, `1.5`, and `3.0` form a clean per-octave
  ladder anchored at 6.0; the z=12/z=13/z=14 boundaries are
  inherited verbatim from the previous (3.0-anchor) revision
  that the user verified visually, and z=15 slots in at the
  view-scale-3.0 line where its tile-rendering downsampling
  ratio (~2.5×) matches the perceptual escalation z=14 used to
  have at 1.5.

  Empirical impact at default fit-to-screen view scale (~0.55 on
  Israel): satellite tile budget stays at ~90 z=12 tiles
  in-viewport (unchanged from the previous revision) -- the
  performance win of "z=12 is the default" survives the addition
  of z=15. The user only pays the z=15 cost (download + cache +
  RAM + paint) when they explicitly zoom past ×3.0 -- empirically
  ~3 nm viewport on the Israel chart, the scale at which they're
  already on a fine layer and want still more resolution.

  Implementation:

  - `cvfr_routemaster/satellite_overlay.py`: anchor constant
    moved 3.0 → 6.0; docstring rewritten with the anchor history
    (1.0 → 3.0 → 6.0) and the rationale for each step; function
    docstring updated with 4-level examples; inline algorithm
    comment updated with new 4-band boundary table.
  - `cvfr_routemaster/satellite_tiles.py`: `DEFAULT_TARGET_ZOOM`
    moved 14 → 15; docstring updated with the new tile / disk /
    time figures.
  - `cvfr_routemaster/settings_store.py`: `DEFAULT_SATELLITE_ZOOM`
    moved 14 → 15 in lockstep.
  - `cvfr_routemaster/main_window.py`: `_satellite_zoom_levels`
    reworked + docstring rewritten to explain the
    permanent-z=12-floor invariant; status-bar tooltip updated.
  - `tests/test_satellite_overlay.py`: `TestSelectZoomForViewScale`
    boundary table updated for the 4-band default;
    regression-guard tests reworked into a full band-by-band
    sweep: `test_default_fit_to_screen_scale_uses_lowest_zoom`
    (0.4–0.75 → z=12), `test_mid_low_band_uses_z13` (0.8–1.5 →
    z=13), `test_first_detail_band_uses_z14` (1.51–3.0 → z=14,
    pinning that z=14 keeps its old behaviour), and new
    `test_deep_detail_band_uses_z15` (3.01–10.0 → z=15). Tests
    are now parameterised on the *default* 4-level set so they
    track what the user actually sees.

  Existing users with a saved `satellite_target_zoom` of 14 in
  QSettings will stay on z=14 until they explicitly update the
  setting; new installs (and users who never touched the setting)
  pick up z=15 by default. There's no auto-migration path --
  intentionally, because the ~1.2 GB / ~36 min bulk fetch is a
  decision the user should make consciously.

  Visual verification: pending user check that default
  fit-to-screen view loads z=12 only (status-bar readout should
  show `z=12`), and that zoom-in past view_scale 0.75 escalates
  to `z=13`, past 1.5 to `z=14`, and past 3.0 to `z=15` at the
  boundaries.

- **Per-tile satellite transform upgraded from 6-DOF affine to 8-DOF
  projective (homography) to close the LCC-induced tile-seam gap.**
  After the LCC switch, the user reported thin white vertical lines
  scattered across the satellite overlay at z=12, z=13, and z=14 --
  most visible at z=12, present over both sea and land. Diagnosis:
  the per-tile transform was a 3-corner-exact affine fit (NW + NE +
  SE; SW held out as a residual probe). Under the legacy planar
  calibration the held-out SW residual was floating-point zero
  because the whole projection chain (Mercator tile pixel -> lon/lat
  -> planar source -> chart UV) was *globally* affine, so any
  3-corner fit reproduced the global affine exactly. Under LCC the
  composed map is non-linear in (lat, lon) and the 3-corner fit
  leaves a residual at the SW corner of every tile -- about 0.30
  chart-px at z=12 over Israel, 0.075 at z=13, 0.019 at z=14
  (quadratic decay because LCC curvature is quadratic in tile span).
  At every 4-tile X-junction the same geographic point P is the SE
  of one tile (exact in fit), the NE of another (exact), the NW of
  another (exact), and the SW of a fourth (held out → residual-off),
  so adjacent tile fits disagreed on P's scene position by the full
  residual. That disagreement opens a hairline gap along the west
  and south edges of every NE tile -- the visible thin-white-line
  artifact at z=12 / z=13 / z=14.

  **Fix.** Replace the 6-DOF affine per-tile fit with an 8-DOF
  projective transform (homography) fitted exactly through all 4
  tile corners. A homography is uniquely determined by 4 point
  correspondences, so every tile maps each of its 4 corners to its
  true calibration-projected scene position exactly. Adjacent tiles
  therefore share the 2 corners of every common edge by
  construction, and a projective transform maps the straight line
  between 2 source points to the straight line between their 2
  image points, so the shared tile edge is *one* straight line in
  scene space for both tiles -- no gap, no overlap, no anti-aliased
  seam.

  Implementation:

  - `cvfr_routemaster/satellite_overlay_math.py`:
    - New `fit_homography_4pt(src_pts, dst_pts) -> 9-tuple` (8-DOF
      projective fit via 8x8 Gaussian elimination with partial
      pivoting, pure stdlib).
    - New `homography_apply(coeffs, x, y) -> (x', y')` (projective
      application with `w' = m13*x + m23*y + m33` divide).
    - `TileTransform` redesigned to carry the 9 row-major coefficients
      (`m11, m12, m13, m21, m22, m23, m31, m32, m33`) matching Qt's
      `QTransform` field naming. `to_qtransform_components()` returns
      all 9 in row-major order for the 9-arg `QTransform` constructor.
    - `tile_to_chart_transform` now fits the 4-corner homography and
      probes the residual at the *tile center* (the natural held-out
      point under a 4-corner exact-fit transform).
    - `fit_affine_3pt` / `affine_apply` retained as standalone
      primitives -- diagnostics and tests use them, but the
      production path no longer does.
  - `cvfr_routemaster/satellite_overlay.py::_make_tile_item` now
    constructs the per-tile `QTransform` from all 9 components. For
    inputs that happen to be exactly affine (synthetic test
    calibrations) the homography solver returns `m13 = m23 = 0` and
    `m33 = 1`, so the QTransform is bit-identical to the legacy
    affine and tests that assume affine behavior keep working.

  **Empirical confirmation** via
  `scratch/diagnose_lcc_tile_seam.py`:

  | zoom | 3-corner affine X-junction gap | 4-corner homography X-junction gap | center residual (held out) |
  |-----:|-------------------------------:|-----------------------------------:|---------------------------:|
  | z=12 | 0.300 chart-px                 | 0.000000 chart-px                  | 0.075 chart-px             |
  | z=13 | 0.075 chart-px                 | 0.000000 chart-px                  | 0.019 chart-px             |
  | z=14 | 0.019 chart-px                 | 0.000000 chart-px                  | 0.005 chart-px             |

  The X-junction disagreement (the source of the visible white-line
  artifact) collapses to floating-point zero at every zoom under
  the projective fit. The held-out residual moves from a *boundary*
  location (visible artifact) to the *tile center* (invisible
  interior point, just a projection-quality measure for
  `MAX_TILE_RESIDUAL_PX`) and shrinks by ~4x in magnitude at the
  same time.

  A dedicated test `test_adjacent_tiles_share_boundary_corners_exactly`
  pins the seam-closing invariant at the X-junction so a future
  refactor that drops back to an affine fit fails loudly here. The
  `MAX_TILE_RESIDUAL_PX = 1.0` guard is unchanged in semantics --
  it now bounds the interior LCC curvature within a tile rather
  than the held-out corner residual, but the production threshold
  is the same and Israeli tiles at z=10 and finer are well under it.

  No persisted-state migration needed: the per-tile transforms are
  computed at runtime from the calibration and never serialized.

- **Lambert Conformal Conic projection replaces the planar
  `(lon * cos(mean_lat), -lat)` source plane in
  `SheetGeoCalibration`.** The legacy planar approximation was the
  load-bearing structural residual under everything we'd done up to
  v2.5: per-sheet click RMS sat at ~16 / ~8 chart-px on north / south
  (worst anchors ~16 px) and cross-sheet disagreement at the Gaza /
  Dead Sea seam corners hit ~9-11 chart-px, because the chart is
  actually drawn on LCC (per ICAO Annex 4 for VFR charts at 30-80 N)
  while the calibration was approximating LCC with a 6-DoF affine on
  raw (lat, lon). The affine has six degrees of freedom but LCC has
  conic curvature that no 2D affine can absorb; the residual scaled
  with the chart's lat/lon extent and was largest at the corners,
  manifesting as a horizontal `dx` shift that varied linearly with
  longitude across the seam.

  **Empirical confirmation.** `scratch/diagnose_lcc_projection_fit.py`
  swept LCC vs TM vs Mercator at several parallel choices against the
  user's live click anchors:

  | source plane                                | N click RMS | S click RMS | seam (no joint LSQ) |
  |---------------------------------------------|------------:|------------:|--------------------:|
  | BASELINE `(lon * cos(mean_lat), -lat)`      | 16.58 px    | 8.17 px     | 8.11 px             |
  | LCC 29 N / 33 N (ICAO defaults)             | 2.70 px     | 1.27 px     | 5.33 px             |
  | TM (Survey of Israel ITM parameters)        | 2.56 px     | 1.28 px     | 5.71 px             |
  | Mercator                                    | 14.62 px    | 11.28 px    | 18.06 px            |

  LCC and TM both fit ~85% better than the planar baseline; Mercator
  is worse than baseline (different shape entirely), eliminating it
  and the cylindrical-projection family. LCC was chosen over TM
  because ICAO Annex 4 mandates LCC for VFR charts at this latitude
  band (Survey of Israel TM/ITM is for topographic maps, not
  aeronautical charts).

  **Fix.** New `lcc_project`, `lcc_unproject`, and module-level
  constants `LCC_PHI_1_DEG=29`, `LCC_PHI_2_DEG=33`,
  `LCC_LAMBDA_0_DEG=35`, `LCC_PHI_0_DEG=31` in
  `cvfr_routemaster/geo_calibration.py`. `SheetGeoCalibration` now
  projects every anchor through LCC before fitting the 6-DoF affine;
  the affine therefore only has to absorb per-sheet print and scan
  distortions (paper stretch, scan skew, slight rotation, non-uniform
  scale between the two print runs) rather than the projection's
  curvature. `compute_joint_calibration` and `apply_joint_affine_overrides`
  follow the same pipeline. The legacy `north_lon_scale` /
  `south_lon_scale` / `apply_joint_affine_overrides(..., lon_scale)`
  parameters are kept at 1.0 for callsite backward compatibility but
  are no-ops -- LCC carries the cos-correction the planar pipeline
  needed.

  **Renderer.** `cvfr_routemaster/satellite_render.py` now probes the
  calibration's affine inverse via `cal.uv_to_lcc_xy(...)` (the new
  affine-only inverse), chains through a vectorised
  `_lcc_unproject_array` over the UV grid, and feeds the resulting
  (lon, lat) grid to the existing Web Mercator projector. The
  per-pixel cost is unchanged from the legacy pipeline because the
  affine probe is still three corner samples and the LCC unprojection
  is a single whole-grid numpy op.

  **Measured impact** on the user's live calibration:

  | metric | pre-LCC (v2.5) | post-LCC (v2.6) |
  | --- | ---: | ---: |
  | worst per-anchor click residual, north | ~16 px | ~3 px |
  | worst per-anchor click residual, south | ~9 px | ~2 px |
  | worst seam |shift| at z=14 corners | 11.6 px | 1.30 px |
  | consistency residual at shared anchors (post joint LSQ) | 6.7 px | 0.73 px |
  | per-tile held-out residual at z=12 / z=13 / z=14 | n/a | 0.30 / 0.075 / 0.019 px (at SW corner -- visible seam, see next entry) |
  | tiles dropped for projection mismatch | a few at chart corners | 0 |

  No persisted-state migration needed: only click anchors are stored
  in `geo_calibration.json`, and `SheetGeoCalibration.__post_init__`
  recomputes the affine through LCC on load. Existing saved
  calibrations pick up the LCC pipeline automatically on next launch.

- **Joint LSQ chart-sheet alignment + chart-pixmap seam partition
  for satellite tiles and waypoint markers.** Two-sheet alignment
  used to be a chain of independent steps: per-sheet affine fit
  → eyeball-based Alt+drag + Alt+wheel of the south sheet against
  the north → live satellite tiles partitioned by a UV-distance
  heuristic. The last two stages were the load-bearing
  unreliable bits — they could leave the chart-on-chart join
  tight to ~2 px while the satellite stitch still drifted by 30+
  px across the overlap, and the UV partition could disagree
  between the two sheets and either drop tiles in the seam
  strip or paint waypoint markers twice.

  **Fix.** Two cooperating changes in
  `cvfr_routemaster/geo_calibration.py`,
  `cvfr_routemaster/main_window.py`,
  `cvfr_routemaster/satellite_overlay.py`, and
  `cvfr_routemaster/waypoint_marker_overlay.py`:

  1. *Joint LSQ over both affines + the layout.* New
     `compute_joint_calibration` runs an alternating LSQ over
     the north affine (6-DoF), the south affine (6-DoF), and
     the south sheet's scale + translation. The layout
     sub-problem mixes affine-derived consistency rows
     (sat-stitch driver) with click-derived chart-on-chart
     rows so both objectives are balanced at the L2 minimum
     instead of either being optimised at the other's
     expense. New `JointCalibration` dataclass carries
     `click_residual_north_px`, `click_residual_south_px`,
     `consistency_residual_px`, and `chart_residual_px` so the
     status bar and the calibration warning logic can reason
     about each independently. `SheetGeoCalibration` gained
     `apply_joint_affine_overrides(...)` so the joint-fit
     affine can replace the per-sheet independent fit
     without rebuilding the cal object.
  2. *Chart-pixmap seam partition.* New `ChartSeamPartition`
     dataclass owns the question "does this lon/lat belong to
     north or south at the visible seam?". Both overlays
     consult the same partition (north's calibration, the
     scaled south pixmap's top edge in scene space, and a
     `self_is_north` flag), so the decisions are exclusive
     (no double-render) and symmetric (no missing-band) for
     every lat/lon. `SatelliteOverlay`,
     `MultiZoomSatelliteOverlay`, and `WaypointMarkerOverlay`
     now take `chart_seam_partition` instead of the old
     `peer_calibration` UV-distance reference.

  **Wiring.** `_finalize_auto_anchor_calibration` runs the
  joint LSQ at the end of every recalibration and applies the
  result via the new `_apply_joint_calibration` helper.
  Startup ordering in `_on_map_finished` was rewritten:
  `_reapply_overlap_alignment_from_saved_clicks_if_changed`
  applies only the layout (using local cals constructed from
  saved data), then `_reload_geo_calibration_from_disk` loads
  the persisted `SheetGeoCalibration` objects, then the new
  `_apply_joint_affine_overrides_at_startup` re-runs the
  joint LSQ on those loaded objects and pushes the affine
  overrides into them — fixing the silent regression where
  the previous ordering applied the overrides to throwaway
  locals before the in-memory cals were even constructed.

  **Removal of Alt+drag manual alignment.** The chart-sheet
  drag gesture in `map_graphics_view.py` would silently
  invalidate the joint-fit pose with no warning and no
  visible undo, so it was removed entirely. Alt+left clicks
  are still swallowed (so Qt's default rubber-band selection
  doesn't fire), but they no longer mutate state. Alt+wheel
  (and Alt+Shift+wheel for the fine 0.05%-per-notch pass)
  remain as an escape hatch for nudging the solver's result.
  Tooltip and dialog copy in `calibration_options_dialog.py`,
  `calibration_instruction_dialog.py`, and `settings_dialog.py`
  was rewritten to reflect "alignment is automatic; Alt+wheel
  is the escape hatch; Alt+drag is gone".

  **Measured impact** on the user's saved live calibration
  (smoke test via `scratch/smoke_test_joint_lsq.py`):

  | metric | independent fit | joint fit |
  | --- | --- | --- |
  | worst affine disagreement in overlap | 32.1 px | 14.5 px |
  | chart-on-chart residual | 2.7 px | 5.7 px |
  | sat-stitch consistency residual | ~32 px worst | 6.7 px |
  | iterations to converge | — | 7 |

  Trade-off accepted: chart join goes from "indistinguishable
  alignment" to "small visible step that doesn't matter for
  navigation" in exchange for satellite stitching going from
  "ragged ~30 px gap at the seam" to "near-perfect across the
  whole overlap".

- **Asymmetric one-row partition extension closes the residual
  4-px satellite gap at the chart-pixmap seam.** Even after the
  joint LSQ above brought the worst affine disagreement in the
  overlap down to ~14 px, the chart-seam partition still left a
  visible hairline at the boundary: tiles are placed by the
  owning sheet's affine, so north's *last* tile ended at
  `north_proj(boundary_lat)` and south's *first* tile started at
  `south_proj(boundary_lat)`, with those two scene-y values
  disagreeing by the residual ~4 chart-px at the
  high-disagreement east end (Dead Sea). At view scale 0.186
  that's only ~0.7 screen-px wide, but it runs continuously
  across the whole chart, so the user saw a faint horizontal
  hairline at z=12 (most prominent over the Dead Sea, where
  imagery contrast makes a thin sliver of chart-pixmap
  show-through highly visible). z=13 and z=14 had the same gap
  in chart-px, but viewing context (the user is zoomed in to a
  smaller patch, so the boundary lat usually isn't in frame
  over a contrasty feature) made them appear "perfect".

  **Fix.** `ChartSeamPartition.item_owned_by_peer` now accepts
  a keyword-only `north_extension_chart_px` argument. When > 0
  and the overlay is north's, north's territory is widened by
  that many chart-px past the seam; south's threshold is
  unchanged (the extension is deliberately asymmetric).
  `SatelliteOverlay` computes the chart-px height of one
  mercator tile-row at the seam latitude for its `target_zoom`
  in the constructor (`_compute_seam_tile_height_chart_px`)
  and passes that as the extension on every partition call.
  The result: north's overlay enumerates *one extra row* past
  the partition boundary, exactly the row whose centre's
  scene-y falls in `(seam_y, seam_y + tile_height]`. South
  already enumerates that row, so both sheets now claim the
  same tile in the seam-row — and Qt's painter order for
  items at identical z is implementation-defined, so the
  duplicate would otherwise coin-flip between north's and
  south's projection.

  **Tie-break via per-sheet z-bump.** `MultiZoomSatelliteOverlay`
  gained a `sheet_z_bump: float = 0.0` parameter that's added
  on top of the per-zoom `SATELLITE_TILE_Z + zoom * 0.01`
  layering offset. `_build_satellite_overlays` in
  `main_window.py` passes `sheet_z_bump=0.0` to north and
  `sheet_z_bump=0.005` to south, so south's tiles sit
  deterministically above north's in the seam-row overlap and
  south's *visible* territory remains identical to the
  un-extended partition. The half-step bump (0.005) stays
  strictly less than the per-zoom step (0.01), so the
  coarse-under-fine layering is preserved across both sheets
  too — e.g. south z=12 (15.125) still sits under north z=13
  (15.13), and a regression test
  (`test_sheet_z_bump_breaks_overlap_z_ties_without_inverting_zoom_order`)
  pins both invariants.

  **Visible delta.** At z=12 view the 4-px gap-sliver where
  the user previously saw chart-pixmap show-through now
  renders north's projection of the seam-row tile (~north's
  imagery, ~4-px wide in chart-px). At z=13 / z=14 view,
  south's territory is byte-for-byte unchanged (south wins in
  the overlap), and the only delta is the same 4-px sliver
  along the boundary lat where chart-pixmap previously bled
  through — now filled with satellite imagery from one of the
  rendered layers. No alignment shifts at any zoom.

  **Why "+1 row" and not "+ exact gap width":** the partition
  decision is on tile centres, so any extension that captures
  the boundary tile's centre suffices to enumerate the row.
  One tile-row height is the smallest extension guaranteed to
  capture exactly the boundary row (and only that row) across
  every column — it gives the LSQ-driver enough overlap to
  defeat the worst-case affine residual without ever expanding
  to a second row.

  **Waypoint markers are unchanged.** `WaypointMarkerOverlay`
  calls `item_owned_by_peer(lon, lat)` without the extension
  keyword, so the strict-exclusive partition still applies to
  markers — no double-rendered VRP triangles in the overlap.

  Verified against the user's saved live calibration by
  `scratch/diagnose_z12_seam_gap.py`: at z=12 over the Dead
  Sea, the formerly-orphaned seam-row tile (mercator row 1670
  at the lon=35.4 column) is now claimed by both sheets, with
  south winning the overlap via the z-bump and north's
  spill-over filling the 4-px sliver that previously showed
  chart-pixmap. Full test suite green.

  **Coverage.** New tests in `tests/test_geo_calibration.py`
  (joint LSQ recovery on synthetic affine + Lambert-like
  data, click-noise balancing, convergence, edge cases,
  override application — 9 new tests), rewritten partition
  tests in `tests/test_satellite_overlay.py` (exclusive
  ownership invariant, direction-of-threshold pin) and
  `tests/test_waypoint_marker_overlay.py` (TestPeerCalibration­
  Partition → TestChartSeamPartition). Full suite **1137
  passed, 2 skipped**.

- **Linux release: PyInstaller warn-file scanner false-positive on
  stdlib ``collections.abc``.** Building release v4 for Linux
  (May 14, 2026) the post-PyInstaller scanner rejected the binary
  with ``ERROR: PyInstaller flagged unresolved top-level imports …
  missing: collections.abc, imported by: cvfr_routemaster.route``.
  The shipped helper even suggested ``pip install collections.abc``
  — which is nonsense, ``collections.abc`` is stdlib.

  **Cause.** Two interacting facts:

  1. PyInstaller 6.20 on Python 3.13's static analyser flags
     ``'collections.abc'`` as "missing" against ~70 importers
     (stdlib ``traceback`` / ``typing`` / ``inspect`` / ``logging``,
     every Qt / PIL / numpy submodule, plus any of our own modules
     that do ``from collections.abc import …``) — even though the
     frozen binary always bundles the running Python's stdlib and
     the import succeeds at runtime. This is a known analyser
     limitation, not a real ship-time risk.

  2. ``cvfr_routemaster/route.py`` line 36 grew
     ``from collections.abc import Iterable`` recently (used as a
     quoted ``"Iterable[WaypointRecord]"`` forward annotation, only
     evaluated at type-check time thanks to ``from __future__ import
     annotations``). That made ``cvfr_routemaster.route`` the first
     app-package module to appear on the ``collections.abc``
     warning line. Prior Linux builds had only third-party importers
     on that line, so the scanner's existing ``app_package``
     filter silently dropped it.

  **Fix.** Stdlib filter in ``scripts/_pyinstaller_warnings.py``:
  any "missing" record whose module's top-level name is in
  :data:`sys.stdlib_module_names` is now skipped before the
  ``app_package`` filter. PyInstaller has zero choice but to bundle
  the running interpreter's stdlib, so a stdlib warning can never
  produce a runtime ``ImportError`` — it's always an analyser false
  positive, regardless of importer.

  **Why this is the right cut.** The filter is data-driven from the
  live interpreter (``sys.stdlib_module_names``) rather than a hard-
  coded allow-list, so future Python releases that move modules into
  / out of stdlib stay correctly handled. Real third-party top-level
  misses (e.g. the original numpy bug the scanner was written to
  catch) still flag correctly — the dual-line regression
  ``test_scan_still_flags_non_stdlib_top_level_miss_when_stdlib_filter_active``
  pins that.

  **End-to-end.** Linux ELF binary now builds cleanly: 303 MiB
  single-file binary at ``release-linux/cvfr-routemaster``, 384 MiB
  total release folder (19 files: ELF + 3 chart PDFs + seed cache +
  desktop / install-shortcut / runtime-deps / WSL launcher / README).
  Wall-clock 4:09 min on the WSL Debian build host. Windows binary
  unchanged.

  **Tests.** ``tests/test_pyinstaller_warning_scan.py`` grew three
  new tests pinning the stdlib filter:
  - ``test_scan_ignores_stdlib_module_misses_even_from_app_package``
    — the exact ``'collections.abc' … cvfr_routemaster.route
    (top-level)`` line from the failing build, must return empty.
  - ``test_scan_ignores_stdlib_dotted_submodule_via_top_level_package_check``
    — ``'importlib.resources'`` / ``'xml.etree.ElementTree'`` must
    match via top-level-package split, since
    ``sys.stdlib_module_names`` only lists package roots.
  - ``test_scan_still_flags_non_stdlib_top_level_miss_when_stdlib_filter_active``
    — mixed warn file with one stdlib false positive + one real
    numpy miss must still return numpy. Belt-and-braces against
    the filter becoming over-eager.

  Existing 22 scanner tests still pass; total ``test_pyinstaller_
  warning_scan.py`` is now 25/25 green.

  **Dev-tooling follow-on.** ``scripts/_wsl_build_linux.sh`` written
  as a permanent shim: PowerShell → wsl.exe → bash is three quoting
  layers and inline invocations from the Windows host kept getting
  mangled (PowerShell strips bare single quotes; nested double
  quotes get re-quoted). The wrapper activates the WSL build venv
  (``~/cvfr-build-venv``, one-time set up via ``python3 -m venv`` +
  ``pip install -r requirements-dev.txt``) and then delegates to
  ``scripts/build_release_for_linux.py``. Documented from the build
  script's docstring so future agents/operators find it.

- **Altitude matcher: wide-corridor rescue (phase 4) for chart layouts
  that broadcast altitude labels in the open space on the opposite
  edge of a wide airway from the user's clicked waypoint chain.**
  Test-driving LLHZ → LLMZ on May 14, 2026 the user identified that
  every coastal southbound 800 ft label was being missed: SFAIM →
  APOLN → ARENA → HTZUK → RIDNG → CLORE → TYONA all returned unknown,
  even though the chart prints a column of SB 800 ft arrows clearly
  labelling the corridor. SIRNI → NSHRM (eastbound 800 ft) had the
  same shape.

  **Cause.** The HRTZ coastal corridor is laid out wide: the waypoint
  triangles sit on the eastern edge of the airway and the chart designer
  places the SB 800 ft arrow column in the open space along the LBG
  TMA boundary 1.0–1.8 nm west of the waypoint chain. The user's
  clicked route runs along the eastern triangles, so every SB 800
  arrow lies 0.66–1.78 nm cross-track from the route line — outside
  the strict 0.65 nm real-waypoint radius. Audit of the 9 SB 800
  arrows in the corridor confirmed that for 6 of the 7 problem legs
  there is a clean on-segment, bearing-aligned, parallel-right
  candidate arrow, all sitting just past the strict radius and well
  within parallel tolerance — it's purely a "wide corridor" radius
  issue, not a chart-anomaly issue.

  **Fix.** New Phase 4 in ``match_altitudes_for_route``,
  ``cvfr_routemaster/altitude_arrows.py``. Runs after the competitive
  primary pass, stack expansion, and the shared-bend rescue. Only
  fires on legs that are still empty. For each unknown real-waypoint
  segment, looks for unclaimed parallel-right arrows within
  ``MATCH_WIDE_CORRIDOR_RADIUS_NM = 1.8`` nm cross-track,
  ``MATCH_WIDE_CORRIDOR_FWD_DIFF_DEG = 20°`` fwd-diff, on-segment
  (no endpoint overshoot). Per-arrow competition: each eligible arrow
  goes to the unknown segment it fits best (lowest cross-track,
  fwd-diff as tiebreaker), so a single chart label is never smeared
  across multiple legs.

  Gate stack, all conjunctive:
  - **Real-waypoint segment only.** Free-clicked intermediates use
    the loose primary radius already and admitting them to the rescue
    would smear corridor labels across user sub-leg chains.
  - **Segment unknown after phases 1–3.** Rescue can never overwrite
    a per-leg primary, stack, or bend match.
  - **Directional arrow only** — bidirectional arrows already get an
    axis-parallel gate in phase 1; rescuing them here would duplicate
    that logic at a riskier radius.
  - **Parallel-right side.** The chart's predominant labelling
    convention; parallel-left arrows admitted at this radius are
    very likely from a neighbouring corridor.
  - **Foot on segment** (no endpoint overshoot). Arrow must
    geographically sit alongside THIS leg, not the next one.
  - **Cross-track ≤ 1.8 nm.** Sized to cover the worst legitimate
    HRTZ on-segment label (ARENA→HTZUK at 1.78 nm) with a hair of
    headroom.
  - **Fwd-diff ≤ 20°.** Tighter than the primary 30° budget since
    we're paying for a wider cross-track allowance. Catches every
    legitimate HRTZ coastal label on the LLHZ → LLMZ ground truth,
    including SIRNI → NSHRM at 15.7°, without admitting cross-
    corridor noise that the primary already filtered.
  - **Arrow not already claimed** by any earlier phase.

  **Why this is safe across the existing regressions.** The full
  pinned ground-truth set (LLHA↔LLHZ × 2 = 26 legs, LLIB↔LLHZ × 2 =
  28 legs, LLIB → LLMZ = 22 legs, LLMZ → LLHZ = 27 legs) keeps every
  altitude. The rescue doesn't fire on those routes because either
  the legs are already matched by phases 1–3, the unknown legs are
  intermediates (filtered by the real-waypoint gate), or there's no
  eligible parallel-right arrow within 1.8 nm + 20° fwd-diff (the
  SIRNI cluster's chart-intentional unknowns).

  **End-to-end result.** All 30 legs of the user's LLHZ → LLMZ route
  match the user's hand-verified ground truth exactly — including
  every previously-wrong coastal leg (now 800), SIRNI → NSHRM
  (now 800), and the four chart-intentional SIRNI-cluster unknowns
  (still unknown). Full 561-test suite green.

  **Tests.** 10 new unit tests in ``tests/test_altitude_arrows.py``
  pin every gate: canonical happy path; bearing-tolerance boundary
  (15° in, 25° out); parallel-left rejection; beyond-1.8 nm
  rejection; past-endpoint overshoot rejection; intermediate-leg
  rejection; already-matched-leg skip; already-claimed-arrow skip;
  competition between segments for the same arrow; and a constant-
  pinning test on ``MATCH_WIDE_CORRIDOR_*``. New end-to-end
  regression ``test_forward_route_llhz_to_llmz_against_user_ground_truth``
  in ``tests/test_route_altitude_regression.py`` pins all 30 legs of
  the user's Herzliya → Masada route. The pre-existing
  ``test_matcher_intermediate_leg_uses_loose_radius_to_catch_far_arrow``
  picked up an explicit ``wide_corridor_radius_nm=0.0`` override to
  isolate the primary-vs-intermediate radius distinction it was
  written to test.

- **Altitude matcher: bidirectional arrows now carry a real body-axis
  bearing instead of a flat ``0.0`` placeholder, and the matcher gates
  them on parallel-OR-antiparallel alignment with the segment direction.**
  Test-driving LLHZ → LLMZ on May 14, 2026 the user identified that
  RIDNG→CLORE matched 1200 ft, but the chart-correct altitude for that
  SW-going leg is 800 ft. The 1200 came from a *bidirectional* arrow
  sitting between RIDNG and ROKCH (a separate east-west corridor the
  route doesn't follow). The pilot called out the underlying bug
  directly: "Bidir arrows have a bearing. In fact, they have TWO. If
  you record all bidir arrows as having no bearing, we will have more
  bugs."

  **Cause.** The PDF extractor previously dropped the body-axis bearing
  for dual-headed arrows — when ``_arrow_bearing_pdf_deg`` returned
  ``None`` (no concave tail notch), the code set ``bearing_value = 0.0``
  as a placeholder and flagged ``bidirectional=True``. The matcher's
  bidirectional branch then accepted any in-radius bidirectional arrow
  regardless of the segment direction. So the RIDNG↔ROKCH bidirectional
  1200 ft arrow at ``(32.101, 34.769)``, whose body runs roughly east-
  west to label the RIDNG-ROKCH corridor, was admitted for our SW-going
  RIDNG→CLORE leg even though its body axis is nearly perpendicular to
  our flight direction.

  **Fix.** Two coordinated changes in
  ``cvfr_routemaster/altitude_arrows.py``:

  1. ``_arrow_bidirectional_axis_bearing_pdf(items)`` — new helper that
     finds the two vertices furthest apart on the polygon path (the two
     tip apexes on a dual-headed arrow) and returns the compass bearing
     of the chord between them. Robust to rotation; works for N-S,
     E-W, and arbitrary diagonal axes. ``O(n²)`` over the ``≤ 15``
     vertices on a real arrow is trivially cheap. Returns ``None`` for
     degenerate paths; the extractor drops those arrows entirely
     rather than fall back to a meaningless placeholder.
  2. Matcher's bidirectional branch now applies an axis-parallel gate
     via the new ``_axis_diff_deg(a, b)`` helper (smallest angle
     between two undirected axes, returning ``[0°, 90°]``). The same
     ``parallel_tol_deg`` budget that gates directional arrows now
     gates bidirectional ones too — and the same
     ``past_endpoint_parallel_tol_deg`` tightening fires when the
     bidirectional arrow's foot lies past the segment endpoint. The
     ``_CLASS_BIDIRECTIONAL`` class rank stays last so any directional
     arrow still wins competition outright; the change only narrows
     which bidirectional arrows enter the candidate pool at all.

  **Cache invalidation.** Bumped ``ALTITUDE_CACHE_FORMAT_VERSION``
  ``6 → 7`` in ``cvfr_routemaster/altitude_cache.py``. Old caches
  contain ``bearing_deg = 0.0`` for every bidirectional arrow, so the
  matcher's new axis gate would over-reject them (treating every
  bidirectional arrow as having a N-S axis) until extraction reruns.
  The GUI auto-rebuilds the cache on next launch; the
  ``scripts/debug_route_altitudes.py`` diagnostic now falls back to
  live extraction + cache rewrite when it sees a schema mismatch, so
  the script is self-healing too.

  **End-to-end result.** On the LLHZ→LLMZ route:
  - Before: RIDNG→CLORE matched 1200 (wrong — bidirectional E-W arrow).
  - After: RIDNG→CLORE returns ``unknown`` (bidirectional 1200 axis
    rejects; the SB 800 arrow is still out of radius — see the
    pending "wide-corridor radius" issue below).

  **No regression on the reverse direction.** The pinned LLMZ→LLHZ
  regression test (27 legs) still passes identically — including the
  HTZUK→KNTRY and KNTRY→LLHZ bend-rescue legs (both still 1200), because
  the corridor arrow at the bend isn't bidirectional and was unaffected
  by this change. The full 550-test suite is green.

  **Tests.** ``tests/test_altitude_arrows.py`` gets 12 new unit tests:
  3 helpers covering the axis-bearing extractor against N-S, E-W, and
  diagonal dual-headed arrow polygons; 2 degenerate-input cases (empty
  path, all-coincident points); 1 algebraic test on ``_axis_diff_deg``
  collapsing parallel ↔ anti-parallel; and 6 end-to-end matcher
  scenarios pinning that bidirectional arrows are accepted along their
  axis (parallel and anti-parallel) and rejected across it (perpendicular,
  45°-off, etc.).

  **Follow-up resolved together with this fix.** The "wide-corridor
  radius" issue along the HRTZ coastal southbound chain
  (SFAIM→APOLN→ARENA→HTZUK→RIDNG→CLORE→TYONA all missing their 800 ft
  labels; SIRNI→NSHRM ditto) was tackled in the same session via the
  Phase 4 wide-corridor rescue (see entry above). After both fixes
  combined, every leg on the user's LLHZ→LLMZ ground truth matches.

- **Altitude matcher: shared-bend arrow rescue for chart corridor
  arrows that label both adjacent legs of a route turn.**
  Test-driving LLMZ → LLHZ on May 14, 2026 the user identified that
  the HTZUK→KNTRY→LLHZ legs both returned "unknown" even though the
  chart clearly shows a single 1200 corridor arrow at the bend, which
  pilots read as labeling both legs. 22 of 24 legs in the user's
  ground-truth table matched correctly; only these two were wrong.

  **Cause.** The chart designer placed a single yellow arrow at the
  corner of the route turn whose body is drawn along the *bisector*
  of the two legs' bearings, not along either leg. Concretely: the
  HTZUK→KNTRY leg bears 103.6° ESE, KNTRY→LLHZ bears 35.9° NE, and
  the corridor arrow at ``(32.148, 34.782)`` bears 71.4° — exactly
  along the (104+36)/2 = 70° bisector, only 1.65° off. The per-leg
  matcher's gates fail both legs by small margins:
  - HTZUK→KNTRY: fwd-diff 32.2° (just past the 30° parallel
    tolerance, ~0.16 nm cross-track).
  - KNTRY→LLHZ: 1.07 nm cross-track (well past the 0.65 nm
    real-waypoint radius), 35.5° fwd-diff.

  Neither gate alone yields to a small widening — a 35° parallel
  tolerance admits HTZUK→KNTRY but KNTRY→LLHZ is still way out of
  radius. The chart-convention signature is fundamentally a "this
  arrow labels TWO legs" pattern, not a per-leg geometry issue.

  **Fix.** New Phase 3 in ``match_altitudes_for_route`` —
  ``cvfr_routemaster/altitude_arrows.py``. After competitive matching
  and stacking finalize per-leg verdicts, walk consecutive segment
  pairs ``(si, si+1)`` and check for the bisector-bend signature.
  Implemented as a deliberately narrow rescue gate stack:
  - **Both legs must be real-waypoint** (no free-clicked
    intermediates). Crossing this boundary would smear corridor
    altitudes through user-clicked sub-leg chains.
  - **Both legs must currently be "unknown"** (empty result tuple).
    The rescue can never trample a per-leg primary — a real
    chart-arrow match always wins. Canonical safety: NSHRM→SIRNI
    matches 1200, so SIRNI→NTAIM can't be propagated even though it
    too returns unknown.
  - **Bend angle ≥ 30°**
    (``MATCH_BEND_RESCUE_MIN_BEND_DEG``). Below this the standard
    parallel-tolerance should have caught any single-arrow corridor.
    Above this a single arrow can only be bisector-aligned, not
    leg-parallel — exactly the signature we want.
  - **Arrow bearing within 15° of the bisector**
    (``MATCH_BEND_RESCUE_BISECTOR_TOL_DEG``). Tight gate because
    the geometric signature is precise; the HTZUK case lands at
    1.65° off.
  - **Arrow within 0.5 nm of one of the two legs' lines**
    (``MATCH_BEND_RESCUE_MAX_LEG_DIST_NM``, endpoint-clamped). Keeps
    the rescue local; an arrow drifting from both legs is in a
    different corridor.
  - **Arrow not already claimed** by another segment via competitive
    matching. The arrow's altitudes go to BOTH legs, and the arrow
    is marked as owned afterwards.

  Helper ``_bisector_bearing_deg(b1, b2)`` added using unit-vector
  averaging in compass-bearing space (``sin``/``cos`` summation +
  ``atan2``) so wraparound at 0°/360° works correctly. Antipodal
  inputs are handled deterministically (returns ``b1 + 90``) for
  robustness even though the min-bend gate makes that case
  unreachable in production.

  **Why this is safe across the existing regression routes.** Every
  pre-existing routes (LLHA↔LLHZ × 2 = 26 legs, LLIB↔LLHZ × 2 = 28
  legs, LLIB→LLMZ = 22 legs) keeps every pinned altitude. The rescue
  doesn't fire on those routes for one of three reasons: their
  unknowns sit at endpoints where there's no consecutive pair, both
  consecutive unknowns are along chain-of-intermediates legs (NAAMA→
  3153N03531E → 3150N03532E → 3147N03530E in LLIB→LLMZ; eligibility
  filtered by the real-waypoint gate), or — most importantly — the
  SIRNI-cluster case where consecutive unknowns exist but all their
  bend angles sit below 30° (SIRNI→NTAIM→IKKEA bends at 20.5°,
  NTAIM→IKKEA→MEHOL at 20.2°, IKKEA→MEHOL→SUPER at 22.0°). On the
  new LLMZ→LLHZ route the rescue fires exactly once — at the
  HTZUK→KNTRY→LLHZ bend — and matches the user's ground truth.

  **Tests.** Eight new unit tests in ``tests/test_altitude_arrows.py``
  pin the rescue end-to-end: bisector helper at canonical / wrap-
  around / antipodal / HTZUK case (4); rescue attribution on a
  synthetic HTZUK-shaped route (1); five negative-path cases pinning
  every gate (already-matched, sub-threshold bend, off-bisector
  bearing, too-far-from-both-legs, intermediate-leg-involved); and a
  constant-pinning test that any drive-by re-tuning has to come
  through. New end-to-end regression
  ``test_inverse_route_llmz_to_llhz_against_user_ground_truth`` in
  ``tests/test_route_altitude_regression.py`` pins all 27 legs of
  the user's plotted Masada→Herzliya route against the user's
  hand-verified ground truth (24 user-confirmed legs + 3 first-three
  legs from the matcher's verified-by-user output). Full suite:
  **539 passed, 2 skipped** (was 526 + 2 before this turn; 13 new
  passes — 8 bend-rescue unit + 4 bisector unit + 1 new 27-leg
  end-to-end regression).

- **Altitude matcher: on-segment matches beat past-endpoint matches
  within the same direction class.** Test-driving SORES→SHARG on
  May 14, 2026 the user saw the SORES→SHARG segment label as
  ``2300`` even though the chart's arrow ``along`` the leg (at Eyal
  Junction) is the ``3300`` one and the ``2300`` arrow at SHARG is
  positioned just *past* the segment, into the SHARG→LTRUN
  continuation. Adding the next leg made the matcher correctly re-
  assign 3300 to SORES→SHARG and 2300 to SHARG→LTRUN — the global
  competitive pass had a better home for the 2300 arrow once the
  second leg existed, so it released its grip on SORES→SHARG. The
  intermediate single-leg state was wrong by chart-reading
  convention even though it was the best score among the two arrows
  in isolation.

  **Cause.** The existing matcher already computes an *along-segment
  overshoot* (``_distance_and_overshoot_to_segment_nm`` returns
  ``0.0`` when the perpendicular foot is inside the segment, positive
  past either endpoint) and uses it for two *hard* gates (a 0.30 nm
  overshoot ceiling and a tighter 15° parallel tolerance for past-
  endpoint arrows). Both gates accept the SHARG-2300 arrow on
  SORES→SHARG — it's only ~0.1 nm past SHARG and bearing-aligned —
  so it competed on equal footing with the on-segment 3300 arrow.
  Endpoint-clamping means a past-endpoint arrow's reported distance
  is just its endpoint-to-arrow great-circle, which is often *smaller*
  than a mid-segment label's perpendicular distance. With nothing
  else in the score, the past-endpoint arrow could win the tie.

  **Fix.** Added a tiny ranking layer in
  ``cvfr_routemaster/altitude_arrows.py``: ``_fit_key`` now returns
  ``(class_rank, on_segment_tier, score)`` instead of
  ``(class_rank, score)``. The new ``on_segment_tier`` is ``0`` when
  ``overshoot_nm == 0.0`` (foot along the segment) and ``1`` when
  ``overshoot_nm > 0.0`` (foot past either endpoint). ``_ArrowSegFit``
  gained an ``overshoot_nm`` field populated at every construction
  site (both the gate-passing path in ``_evaluate_arrow_for_segment``
  and the Phase 1.5 stack-reclaim path, the latter switched from
  ``_great_circle_distance_to_segment_nm`` to
  ``_distance_and_overshoot_to_segment_nm`` so it gets the overshoot
  for free). Tuple ordering does the rest — within a direction
  class, any on-segment fit beats any past-endpoint fit; only when
  both are on-segment (or both past-endpoint) does the numeric
  ``_fit_score`` decide.

  **Why this is safe.** The tier sits *between* ``class_rank`` and
  ``score`` on purpose. A right-of-track past-endpoint arrow still
  beats a left-of-track on-segment arrow — the side-of-track signal
  is a stronger same-direction indicator than the along-vs-past
  signal, and we don't want the new tier to override it. And a
  *solitary* past-endpoint candidate still wins by default: the
  tier is a tiebreaker between candidates that both passed the
  existing gates, never a hard reject of past-endpoint arrows on
  its own. The LLHA→LLHZ middle sub-leg case (fwd-diff 0.9° on the
  genuine ``(2000, 1000)`` past-endpoint chart-label arrows, no on-
  segment alternative) continues to match correctly — re-verified
  by the existing route-altitude regression tests.

  **Tests.** Four new cases in ``tests/test_altitude_arrows.py``:
  one synthetic SORES→SHARG-style segment with both a mid-segment
  3300 (on-segment, larger cross-track) and a just-past-endpoint
  2300 (smaller endpoint-clamped distance, tightly parallel) —
  asserts 3300 wins; a solitary past-endpoint candidate test —
  asserts it still wins; plus two ``_fit_key`` unit tests pinning
  the ordering contract directly (tier wins inside a class, class
  rank wins across classes; tier threshold is strict ``> 0.0``).
  Full suite: **526 passed, 2 skipped** (was 521 + 2; the 5 new
  asserts cover the four new tests with one having three
  comparisons). No existing pinned altitude (the 22-leg LLIB↔LLMZ
  regression × 2 directions and the 13-leg LLHA↔LLHZ regression ×
  2 directions) shifted — the tier only matters when *both* on-
  segment and past-endpoint candidates pass the gates for the same
  leg, which the pinned routes never reach.

- **Route-add chart-click snap radius bumped 0.5 → 1.0 nm to absorb the
  SIRNI back-pages-table anomaly.** Test-driving the LLBG → NSHRM → SIRNI
  route on May 14, 2026 the user observed that clicking on SIRNI's chart
  triangle was producing an *intermediate* point (``3155N03449E``) rather
  than the named ``SIRNI`` waypoint; clicking a fraction of an nm to the
  east correctly snapped. Root cause is a *publication error* in the
  official CVFR back-pages waypoint table, not in our OCR or calibration:
  - The back-pages PDF lists SIRNI at ``31°55'41" N, 34°49'48" E`` ≈
    ``(31.928056, 34.830000)``.
  - The chart artist drew SIRNI's triangle at ``~(31.917, 34.817)``,
    which is where Wikipedia / GeoHack place the Netzer Sereni kibbutz
    (``31°55'21" N, 34°49'20" E`` ≈ ``(31.9225, 34.8222)``).
  - The two disagree by ~0.55 nm; the table's seconds-fields are off by
    a suspiciously round 20" in both axes — looks like a typo in the
    source document.
  - A click on the visible triangle therefore lands ~0.96 nm from the
    table-derived SIRNI position, which exceeded the previous 0.5 nm
    snap and fell through to the intermediate path.
  - Every other waypoint tested in the same chart region (LLBG, NSHRM,
    NTAIM, IKKEA, MEHOL, SUPER, LLRS, AYLON, LLHZ, PARDS, ROKCH, RIDNG,
    CLORE, TYONA) snapped correctly — calibration is innocent; SIRNI is
    the lone known outlier.

  **Fix.** Single-constant change in ``main_window.py`` (``_ROUTE_ADD_SNAP_NM``
  bumped from ``0.5`` → ``1.0`` nm). No data overrides — the deliberate
  choice is to trust the published table coordinates for navigation math
  (filed plan, MAG BRG, distance, altitude matching) and treat the chart
  triangle purely as a visual indicator. Consequence: the drawn red
  polyline lands ~0.55 nm NE of the SIRNI chart icon, accepted as below
  chart precision.

  **Why 1.0 nm is safe across the whole 198-waypoint database.** The
  click handler delegates to a new pure function
  ``cvfr_routemaster.route.find_nearest_waypoint`` (factored out of the
  ex-``MainWindow._nearest_waypoint_to``) whose closer-wins tiebreak
  resolves overlapping snap zones. At 1.0 nm, only 6 of the 19,503
  unique waypoint pairs overlap (``IKKEA↔MEHOL`` 0.65, ``MEHOL↔LLRS``
  0.67, ``IKKEA↔NTAIM`` 0.71, ``SUPER↔LLRS`` 0.81, ``ZRANA↔RANNO`` 0.82,
  ``EVLYM↔GILAM`` 0.96) — all "real" geographic neighbours; closer-wins
  handles them. Every other pair stays ≥ 1.0 nm apart so single-waypoint
  snap zones are unambiguous. ``_ROUTE_REMOVE_SNAP_NM`` left at 4.0 nm
  (intentional asymmetry — remove is forgiving).

  **Tests.** New ``tests/test_route_click_snap.py`` pins 8 cases on the
  pure helper, including the SIRNI-class ~0.96 nm snap success, the
  symmetric *failure under the old 0.5 nm radius* (regression-catches a
  future shrink), closer-wins tiebreak in both directions, and the
  far-click / empty-db boundaries. Full suite: 521 passed, 2 skipped
  (was 513).

  **Open question, not addressed here.** Whether the back-pages PDF
  table or the chart triangle is the "ground truth" for SIRNI is a
  documentation-quality question, not a code question. If we ever find
  more such anomalies and the geometry mismatch becomes operationally
  annoying, an override mechanism becomes worth revisiting — but as of
  this fix SIRNI is the lone known outlier and the snap-radius approach
  scales gracefully to similarly-sized future drifts without per-
  waypoint maintenance.

- **Default Save-plan filename from origin → destination
  (`LLIB-LLMZ.cvfr`).** The Save-plan dialog used to open with a
  hard-coded ``flight-plan.cvfr`` suggestion regardless of the
  route. Pilots refer to plans by their endpoints and the matching
  ODS paperwork export uses the same convention, so the default now
  derives from the route's first and last *named* waypoints:
  ``<origin>-<destination>.cvfr`` (e.g. ``LLIB-LLMZ.cvfr`` for the
  canonical Dead Sea route).
  - **Helper.** New ``default_save_plan_name(route)`` in
    ``cvfr_routemaster/route.py`` next to the other Route-derived
    string builders; pure data, no Qt. Walks ``Route.points()`` from
    each end and picks the first ``RoutePoint`` whose ``waypoint``
    is not ``None`` (intermediates are skipped). Sanitises each
    code through an ``[A-Za-z0-9]``-only whitelist before joining
    so a synthetic / future code with filesystem-hostile characters
    can't leak into the suggestion.
  - **Edge cases handled.** *Empty route* or *all-intermediates*
    (structurally impossible through the public mutators but
    defended against) → fall back to the historical
    ``flight-plan.cvfr``. *Single named fix* — one-point route or a
    returns-to-origin route where origin code equals destination
    code after skipping intermediates — collapses to ``<code>.cvfr``
    rather than the noisier ``LLIB-LLIB.cvfr``. Sanitised-to-empty
    endpoint → fallback (no leading/trailing-dash filenames).
  - **Call site.** ``MainWindow._on_save_plan_requested``
    (``main_window.py`` ≈ L1535) now does
    ``default_name = default_save_plan_name(self._route)`` instead
    of the hard-coded string; the QFileDialog plumbing is otherwise
    unchanged, and the user can still rename in the dialog.
  - **Tests.** Seven new cases in ``tests/test_flight_plan.py``
    pin the contract: canonical two-named-fix → ``LLIB-LLMZ.cvfr``,
    intermediates-between-endpoints → ``LLIB-LLMZ.cvfr`` (skipped),
    empty route → ``flight-plan.cvfr``, all-intermediates synthetic
    → ``flight-plan.cvfr``, single named fix → ``LLIB.cvfr``,
    returns-to-origin → ``LLIB.cvfr`` (collapsed), and two
    filesystem-hostile-char defenses. Full suite: 513 passed, 2
    skipped (was 505 + 1 spillover).

- **Past-endpoint altitude-arrow matches suppressed (two gates).**
  Wrong-direction altitude false positive caught test-driving the
  LLIB→LLMZ Dead Sea flight plan (May 13 2026). The
  ``3147N03530E → ALMOG`` westbound sub-leg (intermediate-radius,
  ~2.1 nm long, bearing 264°M ≈ 269°T) was returning ``(4000,)``
  for a leg that should have been ``unknown`` — the matcher had
  latched onto the Highway 1 "4000 / 291 / 3.9" chart arrow at
  @(31.801, 35.450) whose bearing (292°) sat within the parallel
  tolerance of the segment but whose tail anchor projects past
  ALMOG (the leg's terminal endpoint). That arrow actually labels
  the chart's published Route 1 westbound corridor *continuing
  from* ALMOG, not the user's terminating leg. The endpoint-
  clamped cross-track distance (0.728 nm) was inside the loose
  1.30 nm intermediate radius; the parallel-tolerance gate (30°)
  let it through because the arrow is a real chart arrow with a
  real direction; and competitive matching had no better claimant.
  - **Root cause.** ``_great_circle_distance_to_segment_nm`` clamps
    the projection parameter ``t`` to ``[0, 1]`` and returns the
    great-circle distance to the nearest endpoint when the foot lies
    outside the segment. That's the correct answer for "how far from
    the segment is this point?" but the wrong question for "does
    this chart arrow label this segment?". An arrow whose foot sits
    significantly past an endpoint belongs by chart convention to
    whatever route continues beyond that endpoint, not to OUR leg.
  - **Two complementary gates, both required.** A single overshoot
    threshold isn't sufficient because the user's *actual click
    position* — stored at sub-minute lat/lon precision — sits a
    fraction of a nautical mile from where the ICAO-minute display
    label suggests. That shifts the bug's overshoot from 0.42 nm
    (rounded coords) down to 0.29 nm (precise click), JUST inside
    any threshold tight enough to also accept the worst-known
    legitimate match. The fix is two gates that compound:
    1. **``MATCH_MAX_ENDPOINT_OVERSHOOT_NM = 0.30``** — kills arrows
       whose foot projects more than 0.30 nm past either endpoint
       regardless of bearing. Catches the rounded-coord shape of the
       bug outright (overshoot 0.42 nm), and is the only gate the
       legitimate LLHA→LLHZ middle sub-leg match has to clear
       (overshoot 0.26 nm).
    2. **``MATCH_PARALLEL_TOL_DEG_PAST_ENDPOINT = 15.0``** — kills
       past-endpoint arrows that are only loosely aligned with the
       segment bearing. On-segment arrows still get the wider 30°
       budget (chart-print + extraction jitter on a genuine OUR-leg
       arrow can reach the high single digits); past-endpoint arrows
       don't get that courtesy because the foot-past-endpoint
       signature is itself already a "wrong leg" indicator. Catches
       the precise-coord shape of the bug (fwd-diff 23.6°), and the
       legitimate LLHA→LLHZ match comfortably clears it (fwd-diff
       0.9°).
  - **New helper.** ``_distance_and_overshoot_to_segment_nm`` returns
    both the clamped distance AND the along-segment overshoot in
    nm (positive when ``t`` is outside ``[0, 1]``, zero when the
    foot lies on the segment). Threading exposes both threshold
    knobs through ``match_altitudes_for_route`` /
    ``match_altitudes_for_segment`` for parity with the other gates.
  - **Threshold choices.** The 22° gap between the bug's fwd-diff
    (23.6°) and the legitimate match's fwd-diff (0.9°) gives the
    parallel-tol-past-endpoint gate ample headroom in either
    direction; 15° is right in the middle. The overshoot gate's
    0.30 nm sits in a narrower band between rounded-coord bug
    overshoot (0.42 nm) and legitimate match overshoot (0.26 nm).
    A first attempt at 0.20 nm broke the LLHA→LLHZ regression;
    ≥0.40 nm re-admits the Dead Sea bug at the rounded-coord test
    path. Both thresholds are pinned by safe-band tests
    (``test_match_endpoint_overshoot_threshold_is_in_safe_band``,
    ``test_parallel_tol_past_endpoint_is_in_safe_band``) so a
    future tuning pass can't silently slip outside the windows.
  - **Bonus catch.** The same change cleaned up a second false
    positive on the same route: the user's ``3126N03523E → LLMZ``
    leg was returning ``(3500,)`` from arrows whose feet project
    ~0.47 nm past LLMZ. Now correctly ``unknown``, and locked in
    by the full-route regression below.
  - **Coverage.** 10 new tests in ``tests/test_altitude_arrows.py``
    (overshoot helper contract on 4 geometries; matcher-level
    rejection of past-TO and past-FROM arrows; acceptance of the
    ≤0.10 nm small-overshoot case; the intermediate-loose-radius
    scenario; safe-band pins on both threshold constants; the new
    conditional gate's reject-on-loose-parallel and accept-on-
    tight-parallel pair). 2 new regressions in
    ``tests/test_route_altitude_regression.py``:
    ``test_forward_route_llib_to_llmz_against_user_ground_truth``
    (the user's full 22-leg LLIB→LLMZ Jordan / Dead Sea route,
    every altitude verified by the user against the printed CVFR
    North + South charts on 2026-05-13 — locks in **both** past-
    endpoint kills, the canonical ``3147N03530E → ALMOG`` shape
    and the second-order ``3126N03523E → LLMZ`` shape, plus the
    legitimate ``NAAMA → 3153N03531E`` on-segment match that
    stresses competitive matching) and
    ``test_dead_sea_almog_leg_with_precise_click_coords`` (which
    faithfully reproduces the live-app sub-minute click precision
    that the rounded-coord test path doesn't expose). Total suite
    **505/505** (was 491/491).

- **Font Settings toolbar button (between Map Calibration
  Options and Export waypoints to CSV).** Three user-controlled
  font-size knobs, persisted via QSettings and applied
  immediately without a restart. The button lives on the main
  toolbar (not in a separate menu bar) so all four user-facing
  commands stay grouped — the reading order is "load →
  calibrate → style → export":
    1. **Tables** — applies to both the waypoint table and the
       route table via the ``QTableView { font-size: Npx; }``
       selector in the dark-theme stylesheet. The header sections
       inherit (``QHeaderView::section`` doesn't override
       ``font-size``), so headers scale with the data rows.
    2. **Route text** — applies to the three labels stacked above
       the route table (ICAO Field 15 string, Hebrew paperwork
       string, totals summary). All three were tagged with
       ``objectName="routeText"`` so a single
       ``QLabel#routeText { font-size: Npx; }`` rule hits them.
    3. **Usage hints** — applies to the three help-text labels
       (waypoint-table hint, map hint, route-panel hint). All
       three already shared ``objectName="mapHint"`` for the
       bright-white styling; the font-size is now user-controlled
       instead of the previous hard-coded 18 px.

  Implementation:

    - ``cvfr_routemaster/settings_store.py`` —
      ``FontSizes`` dataclass plus ``load_font_sizes`` /
      ``save_font_sizes`` (CSS pixels; ``FONT_SIZE_MIN_PX = 8``,
      ``FONT_SIZE_MAX_PX = 48``; defaults preserve the historic
      first-launch rendering: tables 12 px, route text 12 px,
      hint 18 px).
    - ``cvfr_routemaster/ui_theme.py`` — ``apply_dark_theme``
      now takes an optional ``font_sizes`` argument and bakes
      them into the QSS at call time. Re-callable, so the "user
      changed sizes → re-apply" path is just another call.
    - ``cvfr_routemaster/font_settings_dialog.py`` —
      ``FontSettingsDialog`` with three ``QSpinBox`` controls,
      clamps out-of-range incoming values, OK / Cancel buttons.
    - ``cvfr_routemaster/main_window.py`` — adds the toolbar
      action between ``act_open_calibration_options`` and
      ``act_export_waypoints_csv``, wires the dialog
      (``_open_font_settings``), and loads font sizes at startup.
    - ``cvfr_routemaster/__main__.py`` — loads font sizes at the
      earliest QApplication creation point so the splash uses
      the user's preference too.
    - ``tests/test_font_settings.py`` — 14 tests covering
      persistence, theme application (stylesheet contains
      expected ``font-size`` rules), and the dialog (seed,
      clamp, accept/reject, ``chosen_sizes()`` round-trip).

  Total test suite: 380 tests passing.

- **Shipped `map_layout.json` so calibration loads on first
  launch on any machine.** Second-order startup-hang bug from
  release v2: even after the mtime fingerprints were restamped
  (see below), the user re-launched and the modal calibration
  dialog **still** appeared. ``py-spy dump`` confirmed the main
  thread was still parked in
  ``_open_calibration_instruction_dialog``. The
  ``sheet-layout-debug.log`` told the story::

      on_map_finished.load_map_layout | present=0
      sheet.snapshot s_x=0 s_y=10536 s_scale=1

  but ``geo_calibration.json`` had ``south.map_layout = {x:
  -84.09, y: 9268.32, scale: 1.0}`` (the dev's custom sheet
  position from manually dragging south on the dev box).
  ``map_layout_matches((-84.09, 9268.32, 1.0), (0, 10536, 1.0))``
  returns False → south calibration rejected → modal blocks the
  event loop forever.

  The trap: the **current** sheet position on first launch is
  loaded by ``settings_store.load_map_layout()``, which read
  **QSettings** only — a per-user store living in
  ``~/.config/CVFRRouteMaster/`` (Linux) or the Windows registry.
  QSettings does **NOT** ship with the release tree, so a
  release built against a calibration captured at a non-default
  layout was broken-by-construction on any machine other than
  the dev's (or in WSL where QSettings is empty). On any machine
  receiving the release for the first time:
    - QSettings empty → app applies hard-coded default layout
      (vertical stack, south at ``(0, north_pixmap_height)``).
    - ``geo_calibration.json``'s ``map_layout`` blocks record
      the dev's layout, not the default → mismatch → calibration
      rejected → modal "please re-calibrate 8 anchor waypoints"
      prompt fires before the main window finishes compositing
      → in WSLg specifically, the prompt also renders offscreen
      as a tiny ``[WARN:COPY...]`` taskbar slice for unrelated
      Wayland reasons, so the whole app appears to hang at
      startup.

  Resolution:
  - **`settings_store.load_map_layout(project_root)` extended
    with a file-based fallback.** When QSettings doesn't have a
    saved layout (i.e. first launch on a fresh machine), the
    loader now consults
    ``<project_root>/.cvfr_routemaster/map_layout.json`` before
    falling through to the hard-coded vertical-stack default.
    QSettings still wins when populated, so the user's own
    drag-arrangements on the release machine still stick —
    this is genuinely a *first-launch* default, not an override
    the user has to keep dismissing. Loader is defensive about
    corrupt / partial / wrong-type shipped files (any failure
    mode collapses to ``None`` → caller falls through to the
    hard-coded default, never crashes startup).
  - **`scripts/_restamp_cache_fingerprints.write_shipped_map_layout()`
    — new build step.** Derives ``map_layout.json`` from
    ``geo_calibration.json``'s ``map_layout`` blocks (the dev's
    calibrated layout is by definition what loads cleanly
    against the shipped calibration). When only one sheet was
    calibrated, the uncalibrated sheet falls back to the
    in-app default placement (north at ``(0, 0, 1.0)``, south
    at ``(0, north_pixmap_height, 1.0)`` — the height comes from
    ``map_images_meta.json``'s ``north_crop.cropped_h``).
    Reports ``meta_missing`` when the latter is needed but
    absent so the build script can hard-fail rather than ship
    south overlapping north entirely at ``(0, 0)``.
  - **Wired into both build scripts** alongside the mtime
    restamp. The Windows build is a no-op in practice for the
    mtime step (Windows preserves NTFS sub-second precision
    end-to-end) but writes the shipped layout exactly the same
    way as Linux, so a Windows zip sent to a friend lands
    fully-warm-cached just like the Linux tarball.
  - **Earlier iteration was the wrong fix and got reverted.**
    A previous version of this step *normalised* the
    calibration's ``map_layout`` to the release default
    (``restamp_calibration_map_layout``) — i.e. it threw away
    the dev's intent rather than honouring it. That worked for
    the dialog-blocking symptom but produced a release where
    the maps came up in the default vertical-stack layout, not
    the dev's preferred side-by-side overlap. Replaced wholesale
    by the new ``write_shipped_map_layout`` approach; the dev's
    layout now ships verbatim.
  - **Coverage:** 13 new tests in
    ``tests/test_restamp_cache_fingerprints.py`` (now 33 total,
    overall suite 366). Each load-map-layout precedence rung
    (QSettings populated wins, shipped file used when QSettings
    empty, hard-coded default when neither present); shipped
    file generated from both-sheets-calibrated; single-sheet
    calibration (north or south alone); ``file_absent`` and
    ``no_layouts`` no-op paths; ``meta_missing`` hard-fail path
    for the uncalibrated-sheet-needs-meta case; the
    no-meta-needed-when-both-calibrated path (so a build
    pipeline that for some reason hasn't shipped meta yet still
    succeeds when both sheets are calibrated); corrupted /
    partial / wrong-type shipped file ⇒ loader returns None
    rather than crashing; QSettings-isolated end-to-end
    round-trip from build helper through ``load_map_layout``;
    backward-compat with the old no-arg signature.

  Total suite **366/366** (was 353/353).

- **Cache-fingerprint mtime drift on WSL-built Linux releases.**
  The bug-symptom: Linux release v2 launched in WSL and the user
  saw only a tiny ``[WARN:COPY...]`` window slice in the taskbar
  with no main window. ``py-spy dump`` showed the Python main
  thread parked in ``_open_calibration_instruction_dialog`` —
  a modal QDialog rendering offscreen on WSLg's Wayland
  compositor, blocking the event loop forever. Underlying cause:
  the shipped ``geo_calibration.json``'s stored PDF mtimes had
  NTFS sub-second precision (``...121.332421700``) but the
  shipped PDFs had whole-second mtimes (``...121.000000000``)
  because ``shutil.copy2`` running in WSL reads source files
  through the kernel's 9P bridge to NTFS, which **floors mtimes
  to whole seconds**. ``fingerprints_match`` rejected the cache,
  the app thought calibration was missing, it tried to prompt
  the user to re-pick anchor waypoints, and WSLg mis-rendered
  the prompt. (On real Debian — the user's VATSIM laptop — the
  prompt would have rendered correctly but still been a
  several-minute manual ritual on a release that's supposed to
  ship a fully-warm cache.)
  - **`scripts/_restamp_cache_fingerprints.py` — new module.**
    Walks all five shipped cache JSONs
    (``waypoints_cache.json``, ``geo_calibration.json``,
    ``altitude_arrows_{north,south}.json``,
    ``map_images_meta.json``) under ``<release>/.cvfr_routemaster/``
    and overwrites each ``mtime_ns`` field with the live mtime of
    the corresponding PDF under ``<release>/map-pdfs/``. The
    binding from cache→PDF is captured in ``_default_bindings()``,
    which uses parallel ``mtime_paths`` and ``pdf_names`` tuples
    so a future fifth cache or PDF rename is a one-place edit.
    Idempotent (second run is a no-op), schema-tolerant (an
    unknown path segment or missing ``mtime_ns`` is skipped, not
    crashed), and reports ``missing_pdfs`` separately from
    ``skipped`` so the build script can distinguish a build-order
    bug from "this optional cache wasn't generated yet".
  - **Wired into both build scripts.** ``scripts/build_release.py``
    and ``scripts/build_release_for_linux.py`` both call
    ``_restamp_cache_fingerprints()`` immediately after
    ``_copy_seed_cache()`` and before
    ``_write_readme()`` / ``_write_desktop_entry_template()``.
    Same step in both pipelines, by design: on Windows the
    helper is a no-op (Python preserves NTFS sub-second mtime
    precision end-to-end), but having both scripts call it
    keeps the pipelines symmetric and means a future change
    that introduces precision drift on Windows can't silently
    ship a broken cache.
  - **Available as a CLI for in-place fixup of an
    already-shipped release:**
    ``python3 -m scripts._restamp_cache_fingerprints <release-dir>``.
    Used to repair the user's current ``release-linux/`` without
    a full rebuild after the bug was diagnosed. Exit codes:
    0 on clean, 1 if a cache referenced a PDF not in
    ``map-pdfs/``, 2 if the release dir argument doesn't exist
    (build-pipeline ordering bug vs CLI argument bug — distinct
    for CI).
  - **Coverage:** ``tests/test_restamp_cache_fingerprints.py``
    (20 tests). Pins the happy path for each of the three real
    cache shapes (single-PDF flat, dual-PDF flat, dual-PDF
    nested-under-sheet) so the binding map can't silently desync
    from the actual JSON files; idempotency; the five edge cases
    (missing cache file → skipped, missing PDF → reported not
    crashed, missing ``mtime_ns`` field → skipped, non-dict
    top-level → skipped, unknown JSON path segment → skipped);
    the three FileNotFoundError raises for build-ordering bugs;
    all four CLI exit codes; build-script integration (both
    scripts must call ``_restamp_cache_fingerprints()`` after
    ``_copy_seed_cache()`` — pinned via source parsing); and
    the verbatim regression from the broken v2 build (the exact
    sub-second mtimes that triggered the calibration-dialog
    deadlock). Total suite **353/353** (was 333/333).

- **Build-time guard against missing top-level imports.**
  Three defence-in-depth fixes for the failure mode that shipped
  Linux release v2 — a binary that crashed at first launch with
  `ModuleNotFoundError: No module named 'numpy'` because the WSL
  build venv was assembled with an explicit `pip install` list
  that didn't include numpy. PyInstaller's analysis pass *had*
  correctly flagged it in `build/cvfr-routemaster-linux/warn-...txt`
  (`missing module named numpy - imported by cvfr_routemaster.map_crop (top-level)`),
  but the build scripts ignored the warning and shipped anyway. The
  Windows release was unaffected only because the Windows dev box
  happens to have numpy installed as a transitive dep of something
  else — luck, not robustness.
  - **`numpy` pinned in `requirements.txt`.** It's a top-level
    import in `cvfr_routemaster.map_crop` (vectorised white-margin
    detection on the rendered chart pixmap) — a runtime dep, not a
    build/test-only one, so it belongs in `requirements.txt`, not
    `requirements-dev.txt`. Closes the loop so a clean
    `pip install -r requirements.txt` always drags numpy in. Pinned
    by `test_numpy_pinned_in_runtime_requirements` (matches `numpy`
    at line start so the version-spec form is free to change but
    the package must be present).
  - **`numpy` added to `hiddenimports` in both spec files.**
    Defence-in-depth alongside the warn-file scanner: a future build
    venv assembled with `pip install --no-deps` (a legitimate
    reproducible-CI workflow) would have numpy installed but
    PyInstaller's auto-discovery might still skip it. Explicit
    listing ensures it gets bundled. Pinned by
    `test_numpy_in_windows_spec_hiddenimports` and
    `test_numpy_in_linux_spec_hiddenimports`.
  - **PyInstaller warn-file scanner (`scripts/_pyinstaller_warnings.py`).**
    The load-bearing fix. After every successful PyInstaller run,
    both `scripts/build_release.py` and
    `scripts/build_release_for_linux.py` parse
    `build/<spec-stem>/warn-<spec-stem>.txt`, extract every "missing
    module named X - imported by Y (qualifiers)" record, and
    `sys.exit(1)` with a verbatim `pip install <missing>` remediation
    line if any record has `Y` inside `cvfr_routemaster.*` AND
    qualifiers containing `top-level`. Two filters by design:
    - **Top-level only.** `conditional` / `optional` / `delayed`
      imports are runtime-guarded by the app's own try/except logic
      (e.g. the `try: import numpy` inside `pytesseract` is a known
      `(optional)` reference); failing the build over those would
      drown the scanner in noise.
    - **App-package importers only.** PIL flagging `defusedxml`,
      PySide6's deploy script flagging `deploy_lib`, etc. are
      PyInstaller's normal cross-platform-noise output. Treating
      those as build failures would make the scanner permanently
      red and the user learns to ignore it — exactly the failure
      mode this is meant to prevent.
    
    Parser correctly handles:
    - Quoted module names PyInstaller wraps with `'...'` for names
      containing dots (e.g. `'collections.abc'`).
    - Multi-importer lines with `(...)` qualifier groups whose own
      commas must NOT split entries — `_split_importers` uses a
      paren-depth counter, not `str.split(', ')`.
    - Exact package-prefix match: `cvfr_routemaster_other.foo`
      does NOT trigger the filter even though it shares the prefix.
    - The package's `__init__.py` (bare `cvfr_routemaster` importer,
      no dotted suffix) IS matched.
    
    When the warn file is missing entirely (interrupted build, weird
    CI environment) the scanner emits a stderr WARNING but doesn't
    fail the build — we can't flag failures we can't see. The
    Windows and Linux build scripts both run the scan BEFORE
    `_copy_exe`, so a flagged build never produces a `release/` or
    `release-linux/` folder containing a broken binary.
  - **Coverage:** new `tests/test_pyinstaller_warning_scan.py` (22
    tests) pins parser behaviour against the verbatim warn-file
    content from the broken WSL build (so a future PyInstaller
    format quirk is caught here, not by a user reporting "the
    release crashed again"), the filtering contracts (third-party
    importers ignored, non-top-level qualifiers ignored, quoted
    names handled, multi-importer commas split correctly,
    substring-vs-prefix match), the formatter (groups by module,
    sorts modules, includes a pip-install line, raises on empty
    input), both build scripts' integration (exit code 1 on flagged
    builds, success on clean builds, warn-but-continue on missing
    warn file), and the three defence-in-depth measures above.
    Total suite **333/333** (was 311/311).

- **Linux release v2: WSL-validated, plugin-bundling fixed, runtime-deps check shipped.**
  Previously-unverified Linux release pipeline got an end-to-end build
  + smoke-test pass on WSL Debian 13 (the same target environment the
  user's VATSIM laptop runs). Three substantive findings + fixes came
  out of it.
  - **Critical plugin-bundling bug found and fixed.** PyInstaller's
    stock `hook-PySide6.QtGui.py` on Linux (verified with PySide6
    6.11 + PyInstaller 6.20 on Debian 13) bundles only the Qt shared
    libraries (`PySide6/Qt/lib/*.so.6`) and silently *skips the entire
    `PySide6/Qt/plugins/` tree*. Without `Qt/plugins/platforms/libqxcb.so`
    (or any platform plugin) the binary cannot open a window — Qt
    aborts at startup with "Could not find the Qt platform plugin
    'xcb'". The originally-shipped Linux build would have failed
    silently the first time the user double-clicked it. This bug does
    not manifest on Windows because the Windows hook bundles plugins
    through a different code path (`windeployqt`-style enumeration)
    that the Linux hook lacks. **Fix:** added
    `pyside_datas, pyside_binaries, pyside_hiddenimports = collect_all('PySide6')`
    at the top of `cvfr-routemaster-linux.spec` and merged the
    results into the `Analysis` call. `collect_all` walks the entire
    PySide6 install tree and explicitly collects every plugin .so,
    every `.qm` translation, and every conf file Qt looks up at
    runtime. Ground-truth verified by `pyi-archive_viewer --list` on
    the rebuilt binary (9 platform plugins including `libqxcb.so` +
    `libqoffscreen.so`, 10 image-format plugins, 73 plugin .so files
    total) and by launching the binary with `QT_QPA_PLATFORM=minimal`
    in WSL and confirming the bundle self-extracts to
    `/tmp/_MEIxxxx/PySide6/Qt/plugins/platforms/libqxcb.so` and Qt
    initialises without error.
  - **Cost: bundle size grew from ~74 MiB (broken) to 286 MiB
    (working).** `collect_all` is a superset of what we strictly
    need — it pulls in QtWebEngine (~80 MiB), QtMultimedia + ffmpeg
    (~40 MiB), Qt3D, QtCharts, QML, etc. Selective bundling could
    halve this but is fragile across PySide6 minor versions
    (the hook's curated list of plugins-per-Qt-module shifts). User
    explicitly chose to ship the larger version: simpler, demonstrably
    works, no bikeshedding. First-launch self-extract to /tmp tmpfs
    takes ~15-20s for the 448 MiB extracted tree; subsequent launches
    are fast because tmpfs caching keeps the extracted files RAM-resident.
  - **Runtime apt-deps definitively enumerated by `ldd`.** PyInstaller
    bundles Qt's own libraries but deliberately *not* system libraries
    that vary by distro (libxcb, libxkbcommon, libgl, libfontconfig,
    libfreetype, etc.). Determined the exact set the user's box needs
    by running `ldd` on the bundled `libqxcb.so` plugin in the
    self-extracted tree and mapping each dependency back to its
    providing apt package via `apt-file`. Result: 24 Qt runtime
    packages (mostly `libxcb-*`, plus `libxkbcommon0`,
    `libxkbcommon-x11-0`, `libgl1`, `libegl1`, `libfontconfig1`,
    `libfreetype6`, `libglib2.0-0t64`, `libdbus-1-3`,
    `libx11-6`/`libx11-xcb1`) + DejaVu fonts. Most are present on any
    Debian-13-with-desktop-environment install; the notable usual gap
    is `libxcb-cursor0` — a Qt6-specific dependency that Qt5-era
    desktop installs didn't pull in. Tesseract remains the optional
    second tier (only needed for re-OCR of new chart cycles).
    Captured this set as a single Python list
    (`RUNTIME_QT_APT_PACKAGES`) in `scripts/build_release_for_linux.py`
    and reused it in both the README and the new check script —
    regression-tested with `test_runtime_lib_probes_aligned_with_qt_apt_packages`
    so they can't drift apart.
  - **`release-linux/check-runtime-deps.sh` shipped with the release.**
    POSIX sh script (not bash — works on every Debian-derivative
    without depending on /bin/bash) the user runs on the *target* box
    *before* first launch. Probes every required `lib*.so.*` via
    `ldconfig -p` (more reliable than `dpkg -s` because it survives
    Snap/Flatpak/manual-installed libs that aren't in dpkg's view),
    detects DejaVu fonts via `dpkg-query` (no soname to probe), and
    checks Tesseract via `command -v tesseract` + `tesseract --list-langs`
    so the partial-install case (binary present, `-heb` package missing
    → garbage Hebrew OCR) is caught here, not at the next OCR run.
    Exit codes are split into 3 tiers: 0 = all good, 1 = Qt missing
    (binary won't start at all), 2 = Tesseract missing (degraded only).
    The remediation is always a single copy-paste-able
    `sudo apt install --no-install-recommends ...` line listing exactly
    what's missing — no "go figure out what library this lib*.so name
    maps to" left as homework. Verified end-to-end by running it on
    a fresh WSL Debian: with all libs installed, every line prints
    `[ OK ]` and exit 0; with Tesseract uninstalled, the OCR section
    prints `[MISS]` plus the exact remediation command.
  - **README rewritten** to use the deps lists as source of truth.
    The prior version only mentioned the Tesseract apt command (only
    addressed tier 2). The rewrite walks the user through the full
    flow: extract → `./check-runtime-deps.sh` → install whatever it
    flagged → `./cvfr-routemaster`. The full Qt apt-install command
    is included up-front for users who want to install everything
    without running the check first. Troubleshooting section now
    leads with "if it exits silently, run check-runtime-deps.sh first"
    because that catches ~95% of failure modes.
  - **Build environment notes for the user.** WSL Debian 13 with
    `python3-venv` + `python3-pip` + `libpython3.13` (PyInstaller
    needs the shared lib for `--onefile` linking — a Debian gotcha
    because the stock `python3` package doesn't ship it) is sufficient
    to build. Inside a Linux-native venv (`~/cvfr-build-venv`),
    `pip install pyinstaller pyside6 pymupdf pillow pytesseract`
    completes in ~60s. Building under `/mnt/c` (NTFS) takes ~3.5
    minutes vs ~2 on a native ext4 root; acceptable for a one-shot
    build that the user runs occasionally.
  - **Coverage:** `tests/test_release_for_linux.py` grew from 10 to
    16 tests. Six new tests cover the check script
    (`test_write_check_runtime_deps_script_produces_executable_posix_sh`,
    `test_check_runtime_deps_script_probes_every_qt_runtime_lib`,
    `test_check_runtime_deps_script_probes_tesseract_languages`,
    `test_check_runtime_deps_script_uses_distinct_exit_codes_for_qt_vs_ocr`,
    `test_runtime_lib_probes_aligned_with_qt_apt_packages`) and the
    extended README (`test_write_readme_documents_complete_qt_runtime_apt_install`).
    Existing `test_write_readme_documents_apt_install_command_verbatim`
    was renamed and softened to test "command appears in some form"
    rather than "command appears as one specific literal line"
    because the rewrite splits the apt install across sections. Total
    suite **311/311** (was 305/305).
  - **Final release-linux/ contents** (16 files, 367 MiB):
    `cvfr-routemaster` (286 MiB ELF), `check-runtime-deps.sh` (5 KB,
    executable), `README.txt` (5 KB), `install-shortcut.sh` (2 KB),
    `cvfr-routemaster.desktop`, `icon.png` (28 KB), `map-pdfs/` (17 MiB
    — the 3 chart PDFs), `.cvfr_routemaster/` (~64 MiB — calibration +
    arrow caches + waypoint cache + pre-rendered map PNGs).

- **Linux release pipeline (`release-linux/cvfr-routemaster`).**
  PyInstaller `--onefile` ELF build targeting Debian 13 (Trixie) and
  derivatives, parallel to the Windows release but with a deliberately
  different distribution strategy: **system Tesseract via apt** instead
  of bundled, on the user's explicit choice. Works on any glibc ≥ the
  build host's (Debian 13 builds → Debian 13+ runtime; building on
  Trixie gives broad forward compat, narrow back-compat).
  - **Why no bundled Tesseract on Linux** (in contrast to Windows):
    Linux Tesseract is dynamically linked against ~50 shared libraries
    at fixed FHS paths (`/usr/lib/x86_64-linux-gnu/lib*.so.*`).
    Bundling reliably means `ldd`-walking the dep graph and patching
    rpath, fragile across glibc versions and silently broken when
    transitive deps load via `dlopen` at runtime. For a single-user
    Debian-13 case the right answer is a one-time
    `sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-heb`
    on the target box. Saves ~165 MiB vs the Windows bundle and is
    actually more robust because we're not also shipping system libs
    that might mismatch the user's glibc / openjpeg / leptonica
    versions.
  - **Release layout** (mirrors the Windows clean-folder design):
    ```
    release-linux/
    ├── cvfr-routemaster              ← PyInstaller --onefile ELF
    ├── icon.png                      ← 256×256 launcher icon
    ├── cvfr-routemaster.desktop      ← Desktop Entry template (placeholder paths)
    ├── install-shortcut.sh           ← one-shot installer for the menu entry
    ├── README.txt                    ← user-facing one-pager
    ├── map-pdfs/                     ← chart PDFs (same names as Windows)
    └── .cvfr_routemaster/            ← seed cache (calibration + arrows + waypoints + map PNGs)
    ```
  - **Code changes powering it.**
    - `cvfr_routemaster/back_page_ocr.py`: extracted the
      "Tesseract not found" message into
      `_tesseract_missing_message()` and made it `sys.platform`-aware.
      Windows path keeps the bundled-layout + `fetch_vendor_tesseract.py`
      hint; POSIX path emits the exact `apt install tesseract-ocr
      tesseract-ocr-eng tesseract-ocr-heb` command verbatim. Critical
      that `tesseract-ocr-heb` is named explicitly — without it
      users would install the base package and hit a confusing
      "language not available" error instead of "Tesseract not
      found" the next OCR run.
    - `cvfr-routemaster-linux.spec`: separate PyInstaller spec
      mirroring the Windows one but stripping Windows-only options
      (`console=False` is meaningless on Linux ELFs;
      `win_no_prefer_redirects` / `win_private_assemblies` are
      Windows API-version-resolution flags; no `.ico` icon
      embedding because Linux launchers reference an external PNG
      via `.desktop`). Same hidden imports + excludes as Windows
      so the two builds stay in sync on what Python deps they
      bundle.
    - `scripts/build_release_for_linux.py`: end-to-end orchestrator,
      structured identically to `build_release.py` (prereq check
      → icon regen → clean → PyInstaller → copy ELF → copy charts
      → copy seed cache → desktop entry + installer + README →
      summary). No retry loop on PyInstaller because the Defender-
      file-lock race that motivated the Windows retries doesn't
      exist on Linux. Refuses to run off-Linux with a clear "use
      WSL Debian or build on the laptop" hint, so a stray
      double-click of the script on Windows can't silently
      produce a Windows .exe with a Linux-named output file.
    - `_regenerate_icon` reuses the existing
      `_render_icon(size)` from
      `scripts/generate_release_icon.py` rather than shelling out
      to its CLI (which only emits .ico — Windows multi-resolution
      container — and refactoring that CLI to also emit PNG was
      more work than just calling the renderer directly). Same
      compass-rose + magenta-route artwork at 256×256.
    - `_copy_exe` adds `+x` for owner/group/other regardless of
      source mode, so a WSL-mounted-NTFS source tree (which
      can't store the Unix executable bit) doesn't ship a
      non-executable binary that fails with `Permission denied`
      on the user's machine.
  - **Desktop integration** is opt-in via `install-shortcut.sh`:
    the user runs it once after extracting and the script
    substitutes `${INSTALL_DIR}` placeholders in the .desktop
    file with the absolute path it's run from, copies the result
    to `~/.local/share/applications/`, copies the icon to
    `~/.local/share/icons/`, and refreshes the desktop database
    (best-effort, not fatal if `update-desktop-database` isn't
    installed). Idempotent — re-running just refreshes the entry,
    so the user can move the folder and re-run without manually
    editing anything.
  - **Build-environment options** (PyInstaller can't
    cross-compile, so the build must run on Linux):
    - **WSL Debian** — install once on the dev Windows box, then
      `cd /mnt/<drive>/<path-to-repo> && python3
      scripts/build_release_for_linux.py`. Works for a single-machine
      reproducible workflow.
    - **Direct on the Debian 13 laptop** — `git clone` the repo
      there, run the script natively. Simplest if the laptop is
      already set up for VATSIM.
  - **Coverage:** new `tests/test_release_for_linux.py` (10 tests):
    Tesseract-not-found message is platform-aware (Windows hint +
    Linux apt hint + macOS POSIX-fallback), prereq check refuses
    non-Linux hosts and passes when invariants hold, .desktop +
    installer-script generation produces well-formed files with
    matching `${INSTALL_DIR}` placeholders, README mentions every
    shipped artefact AND the exact apt-install command verbatim,
    PDFs land in the `map-pdfs/` subfolder, optional cache files
    are skipped with a friendly notice. Runs entirely on Windows
    (the test platform) by monkeypatching `sys.platform` and the
    build-script constants — no actual PyInstaller invocation.
    Total suite **305/305** (was 295/295).
  - **Not yet smoke-tested end-to-end** because the build itself
    can't run on the dev Windows box. Smoke test happens when the
    user runs `python3 scripts/build_release_for_linux.py` on their
    Debian box (or WSL); the test suite covers everything we can
    cover from Windows.

- **Release v2: clean subfolder layout + bundled Tesseract OCR.**
  Two follow-ups to the initial release build (see entry below):
  - **Tesseract bundling.** The friend's machine doesn't ship Tesseract,
    and the v1 release didn't bundle it. The cached
    `waypoints_cache.json` we ship masked the problem on a happy-path
    launch (cache hits → OCR never runs), but the moment the friend
    triggered "Re-OCR waypoints" from the menu, swapped in
    next-month's chart-cycle PDFs (cache invalidates → OCR runs), or
    extracted the zip with a tool that doesn't preserve mtime (cache
    fingerprint mismatches → OCR runs), they'd hit a hard
    `RuntimeError("Tesseract OCR not found")` modal with no recovery
    path. The fix ships a **slim subset** of `vendor/tesseract/`
    inside `release/tesseract/`: `tesseract.exe` + every `*.dll`
    runtime dep + `tessdata/{eng,heb}.traineddata`, dropping
    ~74 MiB of training-only utilities (`lstmtraining.exe`,
    `text2image.exe`, `cntraining.exe`, etc.), HTML man pages, the
    10 MiB `osd.traineddata` (orientation/script detection — unused
    by us), the Java-based GUI tools (`*.jar`), and the
    `pdf.ttf`/`eng.user-*` placeholders. Net: ~239 MiB → ~165 MiB
    for the OCR engine. Verified end-to-end with
    `release\tesseract\tesseract.exe --list-langs` reporting both
    `eng` and `heb` available, and `--version` reporting
    UB-Mannheim's `v5.4.0.20240606`.
  - **Clean release-folder layout.** v1 dumped all the chart PDFs in
    the release root next to the .exe; the friend opening the
    extracted zip saw 13 files and had to figure out which one to
    double-click. v2 reorganises into 6 top-level items —
    `cvfr-routemaster.exe`, `icon.ico`, `README.txt`, plus three
    clearly-named subfolders (`map-pdfs/`, `tesseract/`,
    `.cvfr_routemaster/`). The chart PDFs live under `map-pdfs/`
    so updating to a new chart cycle is "drop the three new PDFs
    in this folder, keep the names" — no risk of replacing
    `tesseract.exe` by accident or wondering what `icon.ico` is for.
  - **Code changes powering both.**
    - `cvfr_routemaster/tesseract_runtime.py` now searches *two*
      candidate layouts in priority order:
      `<root>/tesseract/` (v2 release layout) followed by
      `<root>/vendor/tesseract/` (dev / `fetch_vendor_tesseract.py`
      layout). Centralised in a `_TESSERACT_SUBDIRS` tuple so a
      future third layout is a one-liner. Release wins when both
      are present (an unusual case, but the explicit choice the
      build script ships should always win).
    - `cvfr_routemaster/back_page_ocr.py` switched from a hardcoded
      `application_root() / "vendor" / "tesseract" / "tessdata" / ...`
      probe to `bundled_tessdata_dir()` so the heb-fast-path check
      transparently picks up either layout — the old hardcode
      meant the release layout would silently fall through to the
      slower `--list-langs` round-trip on every OCR call.
    - `cvfr_routemaster/settings_store.load_pdf_paths()` now
      auto-discovers under both `<project_root>/map-pdfs/`
      (release) and `<project_root>/` (dev), in that priority
      order. Per-PDF discovery (vs. the previous "only run if
      *all* paths are empty"): each chart's QSettings entry is
      now independently re-resolved when stale.
    - `scripts/build_release.py` grew `_copy_slim_tesseract()`
      and reorganised PDFs into the `map-pdfs/` subfolder. The
      `_check_prerequisites()` step now also verifies
      `vendor/tesseract/tesseract.exe` and the
      `eng/heb.traineddata` files exist before kicking off a
      build, so a missing fetch is caught at build time rather
      than silently producing a release with no OCR engine.
    - `cvfr-routemaster.spec` (PyInstaller spec) **unchanged** —
      the spec doesn't bundle PDFs/Tesseract into the .exe; both
      sit alongside it as plain files copied by the build script,
      so changing the on-disk layout didn't require a spec change.
  - **README.txt** rewritten for the new layout: explains what each
    of the three subfolders is for, adds an "Updating to a new
    chart cycle" section ("replace the three PDFs in `map-pdfs/`,
    keep the names"), and adds a troubleshooting bullet for the
    rare "Tesseract OCR not found" error (means the friend
    extracted the zip without the `tesseract/` folder).
  - **Coverage:** 13 new tests in `tests/test_release_portability.py`
    (was 6, now 19 — then +1 = 20 covering the empty-file
    regression below): both Tesseract layout lookups, layout
    priority, PDF auto-discovery in `map-pdfs/`, dev-layout
    fallback, mixed-layout priority, stale-QSettings-path
    fallback, custom-QSettings-path-wins guardrail, and four
    tests pinning the slim-Tesseract copy logic
    (keeps `tesseract.exe` + DLLs; drops training tools + HTML;
    tessdata allowlist correctness; `shutil.copy2` mtime
    preservation). The `_isolate_qsettings` helper now stubs
    `settings_store._settings` rather than relying on
    `QSettings.setPath` (which doesn't reliably redirect the
    Windows registry-backed `QSettings(org, app)` constructor —
    see "QSettings test pollution" below for why this matters).
  - **Second regression caught + fixed during this work** —
    *geo-calibration cache fourth-cache miss*. The original
    path-portability sweep covered three caches (altitude
    arrows, waypoints, map images) but missed the fourth: the
    per-sheet north/south chart calibration in
    `geo_calibration.py`. Its `fingerprints_match` helper
    still compared `stored["path"]` against the resolved PDF
    path, so on the v2 release the user got pinged to
    re-calibrate north + south on first launch — the cached
    JSON had `path = <repo-root>/CVFR-NORTH-...`
    but the live PDF resolved to
    `<repo-root>/release/map-pdfs/CVFR-NORTH-...`,
    different absolute string. (Manual 8-anchor click ritual the
    friend has zero context to do correctly — easily the worst
    UX failure of the v2 release before this fix.) Same
    surgical change as the other three caches: drop the `path`
    comparison from `fingerprints_match`, keep `(mtime_ns,
    size)`. Pinned by
    `test_geo_calibration_cache_hits_after_copying_to_a_new_root`
    + a defensive size-still-invalidates regression test.
    Total suite **295/295**.

  - **Known regression caught + fixed during this work** —
    *QSettings test pollution + 0-byte fallback gap*. An earlier
    version of the QSettings isolation helper (see above) didn't
    actually isolate, so a regression test's
    `QSettings(ORG, APP).setValue("pdf_north", <pytest tmp path
    to a 0-byte fixture>)` wrote to the user's *real* Windows
    registry. The 0-byte fixture later got cleaned up by pytest
    but the registry entry persisted; on the next .exe launch
    `Path(...).is_file()` returned True (file existed, just
    empty), the new "stale path → autodiscover" fallback didn't
    trigger, and PyMuPDF surfaced
    `Cannot open empty file` from the map-load worker. Fixed in
    two places: (1) the isolation helper now monkeypatches
    `_settings` directly rather than relying on
    `QSettings.setPath`, and (2) `_qsetting_path_is_usable`
    in `settings_store.py` now requires `size > 0` in addition
    to `is_file()` — same guard now also catches truncated
    chart downloads / disk-full mid-copy on the friend's
    machine.     Pinned by
    `test_load_pdf_paths_falls_back_to_autodiscovery_when_qsetting_path_is_empty_file`.

- **Single-file Windows release build (`release/cvfr-routemaster.exe`).**
  Friend-shippable PyInstaller `--onefile` build that bundles the
  entire app + Qt + PyMuPDF + Tesseract bindings into one 156 MiB
  executable. The release folder additionally ships the three chart
  PDFs (`CVFR-NORTH/SOUTH/BACK-PAGES-OCT-2025-UPD2.pdf`, ~16 MiB) and
  a seed `.cvfr_routemaster/` directory containing the user's
  current `geo_calibration.json`, both `altitude_arrows_*.json`
  caches, the back-pages `waypoints_cache.json`, and the
  pre-rendered `map_north.png` + `map_south.png` (~67 MiB) — so the
  recipient gets a fully-warm cache, identical calibration, and
  zero-setup launch on the first double-click. Total release
  payload: **237 MiB across 13 files** (a future build flag could
  drop the rendered PNGs to shave ~67 MiB at the cost of a one-time
  ~30 s render on the friend's first launch).
  - **Path-portable cache fingerprints.** All three on-disk caches
    (`altitude_cache.py`, `waypoint_cache.py`, `map_image_cache.py`)
    used to fingerprint by `(absolute_path, mtime_ns, size)`. The
    `path` component meant the cache *always* invalidated when the
    folder was copied to a different machine — defeating the whole
    point of shipping a warm cache. The fingerprint comparison is
    now `(mtime_ns, size)` only; `path` is still stored for
    diagnostics but excluded from `_fp_match` /
    `_fingerprints_match`. `shutil.copy2` in the build script
    preserves `mtime_ns` end-to-end so the cache hits on the
    friend's machine on the very first launch.
  - **Frozen-mode project root.** `cvfr_routemaster/__main__.py`
    grew a `_project_root()` helper that returns
    `Path(__file__).resolve().parents[1]` in dev (the repo root)
    and `Path(sys.executable).resolve().parent` when
    `getattr(sys, "frozen", False)` is true. Without this,
    PyInstaller's onefile bootloader unpacks into a transient
    `_MEI…` temp dir and the app would look for PDFs +
    `.cvfr_routemaster/` *there* instead of next to the .exe.
  - **PyInstaller spec (`cvfr-routemaster.spec`).** `--onefile`
    with `console=False`, custom `icon.ico`, `upx=False` (UPX
    triggers Defender heuristics + buys ~10 MiB at the cost of
    multi-second cold-start), and explicit `hiddenimports` for
    PySide6 submodules + `fitz` + `pytesseract` that PyInstaller's
    static analyzer misses on the dynamic-import paths the app
    uses. Charts and seed cache are **deliberately not** packed
    into `datas` — they sit alongside the .exe as plain files so
    the user can swap in newer chart revs or wipe the cache
    without rebuilding.
  - **Procedurally-generated icon (`scripts/generate_release_icon.py`).**
    Pillow renders a multi-resolution ICO (16/24/32/48/64/128/256)
    of a compass rose with a route line — distinctive enough to
    spot in the taskbar and avoids shipping a binary asset under
    revision control. Regenerated automatically by the build
    script before each PyInstaller run.
  - **Build orchestrator (`scripts/build_release.py`).** End-to-end
    driver: prereq check (all 3 PDFs + `geo_calibration.json`
    must exist) → icon regen → clean `release/`+`build/`+`dist/`
    → PyInstaller → move .exe → copy charts (preserving mtime)
    → copy seed cache (preserving mtime) → write `README.txt` →
    final summary. Runs PyInstaller with **3-attempt retry +
    exponential backoff** because Windows Defender real-time
    scanning intermittently locks `build/base_library.zip`
    mid-pack and throws `PermissionError [WinError 32]` —
    transient, cleared by waiting 5–10 s and re-attempting after
    a fresh `build/`+`dist/` wipe.
  - **Friend-machine smoke test passed.** Copied `release/` to
    `%TEMP%\cvfr-friend-test-<rand>\` (a deliberately different
    absolute path), launched the .exe, confirmed it survived 12 s
    without the kind of immediate-crash exit code we'd see for
    missing DLLs, broken hiddenimports, or `_project_root()`
    pointing at the wrong place. The path-portable cache fingerprints
    + `_project_root()` switch + onefile bootloader all worked
    end-to-end.
  - **Coverage:** new `tests/test_release_portability.py` (6 tests)
    locks down the cache-portability invariant for all three
    caches, the `_project_root()` dev/frozen branch, and a negative
    case (waypoint cache *still* invalidates on size changes). The
    map-image cache test was important to get right: it must use
    `QApplication` (not the lighter `QCoreApplication` /
    `QGuiApplication`) for `QImage` because Qt allows only one
    `Q*Application` per process and a later route-panel test that
    needs `QApplication`-only methods would otherwise crash with
    `STATUS_STACK_BUFFER_OVERRUN` on Windows when handed a
    `QCoreApplication` singleton. Total suite **279/279** (was
    272/272 from Feature 3).

- **"Show ATC columns" visibility toggle above the route table.**
  A new checkbox sits in its own row directly above the table —
  *Show ATC columns (CTR / Freq / New CTR / New Freq)* — that
  collapses or restores the four ATC-handoff columns at once for
  the narrower plotting view the user wanted (vs the briefing-style
  full row that's the checked-by-default state). The toggle is a
  pure display concern via `QTableView.setColumnHidden`: the
  underlying `QStandardItem` objects, the cells' edit flags and
  delegates, and `_atc_inputs` are all untouched, so re-checking
  restores every typed value automatically — including across full
  re-renders that happen *while hidden* (cruise-speed nudge,
  calibration completion, route mutation), since `_render` rebuilds
  rows from `_atc_inputs` regardless of visibility.
  - **Layout.** Sits between the totals line and the table (its own
    `QHBoxLayout` with a trailing stretch, mirroring the
    `table_strip` left-alignment) so the checkbox is visually
    associated with what it shows/hides — not crammed into the
    title row alongside *Include intermediate points* and
    *Clear route*, which would have crowded that row.
  - **Width re-pin.** After toggling we call
    `resizeColumnsToContents()` + `_apply_table_natural_width()` —
    `QHeaderView.length()` already returns the sum of *visible*
    sections, so dropping four columns shrinks the natural-width
    pin automatically. Without the explicit re-pin the table would
    keep its old maximum width and leave a wide trailing stretch
    beside it (Qt has no built-in signal for "a column was
    hidden", so the existing scrollbar-rangeChanged hook doesn't
    cover this case).
  - **Constants.** New `_ATC_VISIBILITY_COLS = (CTR, Freq, New CTR,
    New Freq)` tuple owns the column set the toggle keys off,
    intentionally distinct from `_USER_INPUT_COLS` (same indices
    today, but the visibility toggle is a *display* concern while
    `_USER_INPUT_COLS` is an *editability* concern; splitting
    them means a future read-only ATC column or a new editable
    non-ATC column wouldn't accidentally inherit the wrong
    policy).
  - **Coverage:** `tests/test_route_panel.py` grew 7 new tests
    (43 → 50 in the file) covering checked-by-default state, all
    four columns hide on uncheck while non-ATC columns stay
    visible, round-trip restores visibility, typed values survive
    a hide → show cycle, typed values survive a re-render *while
    hidden*, the toggle is non-destructive on `_atc_inputs`
    (snapshot equality before/after), and the
    `_ATC_VISIBILITY_COLS` constant pins the column set + order.
    Total suite **272/272** (was 265/265).

- **Max-route-altitude suffix on the totals line.** The
  `Total: X nm · HH:MM:SS at K kt` line above the route table now
  ends with `· Max route alt: Y ft` (or `· Max route alt: unknown`
  when no leg has altitude data). The suffix is folded into the same
  string rather than its own label so the line still reads as one
  logical "what does this route look like at a glance" answer, and
  the order — distance, time, speed, ceiling — matches how a pilot
  briefs a leg out loud.
  - **Stacked-altitude legs** contribute their per-leg maximum to
    the route-wide max (a `1600 over 800` chart label means the
    leg climbs as high as 1600 ft), and **`unknown` legs are
    skipped** rather than counted as 0 — without that guard a
    single missed-by-matcher leg would silently mask a real
    higher ceiling on another leg.
  - **Honours Feature 1 Alt overrides.** A user-typed `5500`
    override on a leg the matcher thought was 1500 ft drives the
    route-wide max to 5500; restoring the override (cell-level or
    column-level) recomputes the max from the original computed
    values so a hand-edit the user cleared can't leave a stale
    ceiling in the totals line.
  - **`unknown` rather than suppressed.** When the matcher returned
    nothing for any leg (chart not calibrated, every leg missed,
    or no per-segment list supplied) the suffix still appears but
    reads `Max route alt: unknown` so the field's presence
    documents what was attempted even when the answer is
    unavailable. The suffix only disappears entirely when the
    totals row itself is hidden — i.e. an empty / origin-only
    route with no legs to total.
  - **Wiring.** New `_effective_altitudes_for_segment(seg, computed)`
    and `_effective_max_altitude_ft()` helpers sit alongside
    `_effective_distance_nm` / `_effective_time_seconds`. The
    altitudes helper is the single source of truth that the cell
    factory and the max-alt math both consult so the displayed
    cell value and the totals-line suffix can never disagree.
  - **Coverage:** `tests/test_route_panel.py` grew 8 new tests
    (35 → 43 in the file) covering the suffix appearing in the
    totals line, route-wide max across multiple legs, the
    stacked-altitude max-within-cell rule, override-driven max,
    skip-on-unknown, the `unknown` rendering when no leg has
    data, hidden-on-no-segments, and post-restore recompute.
    Total suite **265/265** (was 257/257).

- **MAG BRG / Alt (ft) / Dist (nm) cell overrides.** The route table's
  three computed-value columns are now user-editable: a double-click
  (or single-click on an already-selected cell — Excel/Sheets style)
  opens an editor pre-populated with the bare numeric value, and a
  successful commit replaces the displayed value with a bright-red
  asterisked form (e.g. `120°M*`, `2500*`, `12.3*`) so a hand-edit
  stands out at a glance against a screen full of computed values.
  - **Per-leg persistence.** Overrides live in `RoutePanel._cell_overrides`
    keyed `(from_label, to_label) → {col → canonical_string}`, mirroring
    the existing `_atc_inputs` design — they survive every full re-render
    (cruise-speed change, route mutation, calibration completion). The
    canonical-storage form (`"046"` for MAG BRG, `"12.0"` for Dist,
    `"1600,800"` for Alt) means typing variants like `"46"`/`"046"` or
    `"1600, 800"`/`"1600,800"` collapse to one entry instead of accumulating
    two equivalent overrides.
  - **Validation.** `_OverridableCellDelegate` runs a per-column regex
    + value-range check on commit (`_parse_override`). Garbage input is
    silently dropped so the cell keeps its previous value (mirrors the
    pre-existing `_FrequencyCellDelegate` contract); empty input routes
    through the data-changed handler's "remove the override" branch and
    repaints the cell with the computed value.
  - **Time auto-recompute on Dist override.** The Time cell on the
    affected row recomputes from the overridden distance and the
    current cruise speed, and the `Total: X nm at Y kt` line above the
    table sums *effective* distances/times so the row math always adds
    up to what the user sees in the table cells. Time itself stays
    read-only — overriding Dist is the only path through which a
    hand-edited time enters the table.
  - **Restore actions.** Right-click on an overridden cell offers
    "Restore computed `<col>`"; right-click on the column header offers
    "Restore all `<col>` values" (disabled when the column has no
    overrides — the affordance is visible even when there's nothing to
    clean up so the user doesn't see a context menu that mysteriously
    appears and disappears). Per-column restore is column-scoped, not
    row-scoped: clearing every MAG BRG override leaves Alt/Dist
    overrides on the same rows untouched.
  - **Cosmetic-strip on edit.** The delegate's `setEditorData` pulls
    the bare numeric form out of the cell's display text (drops `°M`,
    drops `*`, joins multi-line altitude stacks back to comma-
    separated for editing) so re-editing an `046°M*` cell shows
    `046` in the editor instead of forcing the user to surgically
    delete the cosmetic glyphs first.
  - **Hint copy refreshed.** The route-panel footer hint now reads as
    three short paragraphs separated by `<br><br>` (chart clicks /
    table-cell links / overrides) instead of a single wall of text,
    and the third paragraph spells out the asterisk colour, the
    Dist→Time recompute behaviour, and both restore-action paths.
  - **Deferred re-render on delegate commit.** The data-changed handler
    used to call `_render` synchronously every time an override
    committed. Inside a delegate commit that meant `removeRows` ran
    while the view was still in `EditingState` and the editor↔index
    mapping was live — the editor's subsequent focus-out re-entered
    `commitData` with an index that no longer existed and Qt logged
    `QAbstractItemView::commitData called with an editor that does
    not belong to this view`. The handler now checks `self._table.state()`:
    when in `EditingState` it queues the re-render via
    `QTimer.singleShot(0, self._rerender_for_current_route)` so Qt
    finishes the commit cycle (`closeEditor` → editor destroyed →
    editor map cleaned up) before any rows move underneath it; when
    not in `EditingState` (direct `model.setData` from tests or a
    future controller-side override-injection API) the re-render
    stays synchronous so callers can read the repainted cell straight
    after the commit. A regression test
    (`test_delegate_commit_does_not_warn_about_orphaned_editor`)
    captures Qt messages via `qInstallMessageHandler` while driving
    the full edit pipeline (`view.edit()` → `commitData` →
    `closeEditor` → `processEvents`) and fails if the warning string
    appears; the fix-revert sanity check confirmed the test catches
    the original bug.
  - **Coverage:** `tests/test_route_panel.py` grew 19 new tests
    (16 pre-existing + 19 new = 35 in the file) covering
    editability, red-+-asterisk render contracts for all three
    columns, multi-altitude stacks, Dist→Time recompute,
    Dist→totals propagation, per-leg keying, persistence across
    re-renders, per-cell + per-column restore, the `column_has_any_override`
    enable-gate for the header menu, the delegate's cosmetic-strip
    `setEditorData`, the delegate's silent-reject on malformed /
    out-of-range commits, the empty-commit clears-override path,
    `_parse_override`'s canonicalisation for all three columns, and
    the orphaned-editor warning regression. Total suite
    **257/257** (was 238/238).

- **Phantom altitude-arrow detections suppressed.** Two distinct classes
  of yellow chart symbols were being misinterpreted as altitude arrows
  by `extract_altitude_arrows`. Both are now gated out by complementary
  filters in `altitude_arrows.py`, and the `altitude_cache.py`
  `format_version` bumped 4→6 so any old cache containing phantoms
  auto-invalidates on next launch. Together the two gates dropped
  53 phantom detections from the north sheet (503 → 450) and 15 from
  the south (198 → 183) without losing a single real arrow — the
  pre-existing LLHZ↔LLHA round-trip still passes against the refreshed
  fixtures, and the new LLHZ↔LLIB round-trip passes its full 14-leg
  forward + 14-leg reverse golden-truth.
  - **Settlement-blob filter** (`_MAX_ARROW_PATH_ITEMS = 15`). Real CVFR
    altitude arrows are simple notched-tail polygons (5–7 path items);
    settlements / lakes / forests on the same chart are drawn in the
    arrow-yellow palette and routinely clear the size + aspect-ratio
    gates, but carry an order of magnitude more vector-path items
    (Umm El Fahm = 43; larger towns into the hundreds). Pre-fix, when
    such a blob's bbox happened to swallow a nearby altitude digit span
    the extractor emitted a phantom arrow whose bearing was derived
    from the blob's largest concavity. Concrete bug this resolved: the
    forward LLHZ→LLIB leg `EIRON.1→ZMGID` used to pick up a phantom
    `(3000,)` from Umm El Fahm pointing NE, even though the only real
    3000 ft arrow at that location points SW (anti-parallel to the
    segment); with the gate the leg correctly returns unknown.
  - **Holding-pattern filter** (`_FORBIDDEN_ARROW_PATH_KINDS = {'c', 'qu'}`).
    Real arrows are 100% straight-line polygons; holding-pattern
    racetracks share the arrow-yellow palette and clear every other
    gate but their semicircular ends are rendered as cubic-Bézier
    curves, so their PyMuPDF item list always contains `'c'` items.
    Empirically only 7 of 459 north-sheet candidates have any curve
    items, and every one of those 7 has the canonical `{'c': 4, 'l': 2}`
    racetrack signature at locations consistent with real chart
    holding patterns (Umm El Fahm NE of EIRON, north of Megiddo, north
    of Tel Aviv, near Eilat, etc.). Concrete bug this resolved: the
    reverse LLIB→LLHZ leg `EIRON.1→EIRON` used to pick up a phantom
    *bidirectional* `(2500,)` from the EIRON-area racetrack — even
    though there's no real 2500 ft arrow in that sub-leg at all (the
    pilot flies the whole ZMGID→EIRON corridor at 3000 ft, but only
    the first half is labelled). With the gate the leg correctly
    returns unknown.
  - **Forward + reverse LLHZ↔LLIB locked into golden-truth.** Added
    `test_forward_route_llhz_to_llib_against_user_ground_truth` (14
    legs) and `test_inverse_route_llib_to_llhz_against_user_ground_truth`
    (14 legs) to `tests/test_route_altitude_regression.py`. Header
    comments call out three structurally interesting legs verbatim:
    the ZMGID→LLMG / AFULA→LLMG lucky-coincidence matches (different
    altitude in each direction, same chart routing), the
    EIRON.1↔EIRON / EIRON.1→ZMGID phantom-rejection legs (which would
    silently regress if either gate were weakened), and the HADRA→ZYAAR
    parallel-LEFT chart anomaly (legitimate but documented).
  - **Extractor-level coverage.** `tests/test_altitude_arrows.py` grew
    four new tests: `test_max_arrow_path_items_is_in_safe_band` and
    `test_max_arrow_path_items_rejects_known_settlement_blob_size`
    pin both endpoints of the path-items gate (6-item real arrow
    passes, 43-item Umm El Fahm fails);
    `test_forbidden_arrow_path_kinds_includes_cubic_and_quadratic_beziers`
    and `test_forbidden_arrow_path_kinds_rejects_canonical_holding_pattern_signature`
    pin the curve-gate contract (rejects `'c'`/`'qu'`, never lists
    `'l'`/`'m'`/`'re'` as forbidden, accepts pure-line polygons,
    rejects the canonical `{'c': 4, 'l': 2}` racetrack signature).
  - **Coverage:** total suite **238/238** (232 prior + 1 forward LLIB
    regression + 1 reverse LLIB regression + 4 extractor-gate tests).

- **Top toolbar restructured into two task hubs.** The chrome at the top of the
  window is now intentionally tight: only **Map File Settings…**, **Map
  Calibration Options…**, and **Export waypoints to CSV…** are visible in
  normal use (plus the hidden **Cancel calibration** action which appears only
  while a calibration is in progress). Everything else moved into one of the
  two sub-dialogs:
  - **Map File Settings** (`settings_dialog.py`) keeps the three PDF paths and
    the autoload toggle, and grew a new **Load maps & waypoints now** button
    that validates + persists the paths and immediately fires `_load_all` via
    a `LOAD_NOW = 1201` return code.
  - **Map Calibration Options** (`calibration_options_dialog.py`, new module)
    hosts Re-OCR waypoints from PDF, Fit map to view, Reset map layout,
    Calibrate north / south, and Clear geo calibration. Each button is a
    distinct return code in the `1100+` range; the controller's
    `_open_calibration_options` dispatches via `QTimer.singleShot(0, ...)` so
    the dialog has fully closed before any follow-up modal stacks on top. The
    calibration *instructions* render directly on the dialog (not behind a
    button) so a curious user can read the explanation alongside the actions.
- **Hint copy and styling unified.** The three pane footer hints (route panel,
  map, waypoint table) all opt into the `QLabel#mapHint` selector now, which
  was promoted from muted `#b0b0b0` / 12 px to bright `#ffffff` / 18 px (~50%
  larger) so the instructions stop reading as disabled-style copy. Specific
  rewrites:
  - **Route panel hint** drops the misleading "empty chart space" phrasing —
    you usually click on something that just isn't a published waypoint, like
    a road junction or a coastline feature. The new wording calls that out
    explicitly.
  - **Map hint** is pared down to `Ctrl+drag pans · Ctrl+wheel zooms`. The
    Alt-based sheet adjustments and the sheet-selection click moved into the
    calibration workflow and shouldn't sit in the always-visible footer
    inviting accidental moves of an aligned chart.
  - **Waypoint table hint** moved from above the table to below it (so the
    rhythm matches the other two panes), and "same zoom" became
    "maintains current zoom level".
- **Auxiliary text bumped to bright white.** The route-panel totals row and
  the empty-state route-string placeholder were `#888` / `#888` — both now
  `#ffffff`. Functional colour assignments (green/blue links, magenta/cyan
  CTR cells, reporting-type colours, the red sim-only banner, intermediate
  cell `#9ca3af`, "unknown" altitude `#888`) are deliberately left alone per
  the user's instruction.
- **Coverage:** `tests/test_ui_layout.py` (20 new tests) pins the
  `CalibrationOptionsDialog` action codes (uniqueness, no collision with
  `QDialog.Accepted/Rejected` / `CalibrationInstructionDialog` /
  `SettingsDialog.LOAD_NOW`, click-routes-to-code), the `SettingsDialog`
  retitle and Load-Now validation, the toolbar's three-visible + one-hidden
  shape (with an explicit "no legacy actions" guard), the hint wording
  changes, and the `mapHint` QSS contract. Total suite: **232/232** (212
  prior + 20 new).

- **Waypoint table disappeared from the right of the map** — fixed via
  `self._pane_stretch = (3, 7, 3)` and rewriting `_apply_splitter_ratio` to
  derive sizes from the stored stretch tuple instead of indexing into a
  2-element list.
- **Route table column widths** — same "natural width" treatment as the
  waypoint table (`setStretchLastSection(False)`, `ResizeMode.ResizeToContents`,
  HBox with trailing stretch, `_apply_table_natural_width` pin).
- **Sub-segments via user-clicked intermediates** — `RoutePoint` model
  (waypoint *or* intermediate), `Route.append_intermediate`, ordinal labelling
  (`DAROM.1`, `DAROM.2`), `--> CODE.N` row prefix, and per-sub-leg distance /
  bearing / time. Snap radius is `_ROUTE_ADD_SNAP_NM = 0.5` so closely-spaced
  waypoints (DAROM↔GALIM ≈ 3 nm) still leave room for intermediate clicks.
- **ICAO Field 15 route string** above the table with a "include intermediate
  coords" checkbox; intermediates serialise as `DDMMN/SDDDMME/W` per ICAO.
- **Both real and intermediate route-table cells link out to the configured
  external map provider** via the unified `route_point_clicked` signal.
- **Reporting (Hebrew name) cell in the route table is a blue underlined link**
  matching the master waypoint table; clicking centres the chart on that
  waypoint via `MainWindow._on_route_reporting_name_clicked` →
  `_center_map_on_waypoint`. Shared link-colour constants live in
  `cvfr_routemaster/waypoint_styles.py`.
- **`Distance (nm)` → `Dist (nm)`** column rename.
- **Route polyline drawn on the chart** as a semi-transparent red marker-pen
  stroke (`rgba(220, 38, 38, 150)`, ~21 px cosmetic width, round caps/joins)
  via `MainWindow._redraw_route_overlay`; refreshed on route mutation,
  calibration completion, sheet move/scale (`persist_map_layout`), and map
  clear. Per-point projection prefers the in-bounds calibrated sheet
  (south wins overlap region).
- **Scrollable scene area follows Alt+drag** — `persist_map_layout` now calls
  `_refresh_scene_rect()`, so dragging sheets far apart no longer leaves the
  outer regions unreachable until the next scale tick.
- **Plain scroll-wheel no longer pans the map** — only `Ctrl+wheel` zooms the
  view and `Alt+wheel` scales the selected sheet. Plain wheel events are
  swallowed in `MapGraphicsView.wheelEvent` so an idle scroll while reading
  the side panels can no longer accidentally re-frame the chart.
- **`THIS PROGRAM IS FOR SIMULATOR USE ONLY!` warning banners** sit above and
  below the map view. Bold red text on light-red fill, surrounded by a 2-px
  red border with rounded corners — both labels share `_make_sim_only_banner`
  in `main_window.py`, so any future tweak (font size, palette, copy) is a
  single-place change.
- **Four ATC-handoff columns in the route table** (`CTR`, `Freq`, `New CTR`,
  `New Freq`, between `Type` and `MAG BRG`). Editable on double-click / F2 /
  any-key. CTR cells are free-form alphanumeric and rendered in magenta /
  cyan to give a left-to-right *now → next* visual gradient. Frequency cells
  are gated by `_FrequencyCellDelegate`: a `QRegularExpressionValidator`
  enforces `XXX.Y` / `XXX.YYY` while typing and the same regex rejects
  malformed values on commit. Typed values persist across re-renders via
  `RoutePanel._atc_inputs`, keyed by `(from_label, to_label)` so they
  survive cruise-speed changes / route mutations / altitude refreshes.
- **Route-table copy with grid formatting** — `Ctrl+C` produces both an HTML
  `<table>` payload (with column headers and the CTR magenta / New CTR cyan
  styling preserved inline) and a TSV plain-text fallback. Pasting into
  Word / Excel / Outlook lands as a real grid; pasting into a code editor
  lands as tab-separated text.
- **Window geometry + pane sizes persist across sessions.**
  `MainWindow.closeEvent` now writes `self.saveGeometry()` (window
  position, size, and maximized/fullscreen state) and
  `self._splitter.saveState()` (route / map / waypoint pane proportions)
  via `settings_store.save_window_layout`. On startup, after the splitter
  is built, `load_window_layout()` is consulted: if a payload exists,
  `restoreGeometry` and `restoreState` are applied and a
  `_window_layout_restored` flag short-circuits the deferred
  `_apply_splitter_ratio` so the user's saved pane sizes aren't
  overwritten by the (3, 7, 3) default. Off-screen safety reuses the
  existing `_ensure_window_on_screen` first-show handler — if the
  restored frame's center isn't on any current screen (monitor unplugged
  since last close) the window is clamped into the primary screen's
  available rect; the handler now also bails out while maximized /
  fullscreen so Qt's own restore semantics aren't fought. Pinned by
  `tests/test_window_layout_persistence.py`: byte-exact round-trip,
  overwrite semantics, real `QMainWindow.saveGeometry` + `QSplitter.saveState`
  acceptance through QSettings, and graceful fallback on a partially
  corrupted entry.
- **Per-segment matcher radius (strict for real waypoints, loose for
  intermediates)** — `MATCH_RADIUS_NM_INTERMEDIATE = 1.30` is applied to any
  segment with at least one free-clicked endpoint (`RoutePoint.waypoint is
  None`); real-waypoint legs keep the strict 0.65 nm radius. Free clicks
  are inherently up to ~0.7 nm off the "true" CVFR route line (one
  arc-minute = 1 nm; rounded ICAO Field 15 coords carry that much
  imprecision), so the loose radius lets an intermediate leg find its
  altitude arrow without weakening the strict precision contract for
  real-waypoint legs. **Stacked-alternate reclaim** (phase 1.5 of
  `match_altitudes_for_route`) ensures a precise leg's stacked
  alternate — an arrow within stack-radius of its primary but outside its
  own strict radius — cannot be siphoned off by an adjacent intermediate
  leg's loose catchment. Pinned by two regression tests:
  `tests/test_route_altitude_regression.py` runs the full LLHZ↔LLHA
  round-trip against user-verified ground truth (13/13 each direction)
  using snapshotted cache fixtures in `tests/fixtures/altitude_regression/`.

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

- **Hebrew UI translation (i18n).** Wrap user-visible strings in `tr()` /
  `QCoreApplication.translate(...)`, ship a Hebrew `.ts` / compiled `.qm`
  via Qt Linguist, and add a Settings toggle (or auto-detect from
  `QLocale.system()`) so VatIL pilots who prefer Hebrew can flip the UI.
  Needs RTL handling — `QApplication.setLayoutDirection(Qt.RightToLeft)`
  for window chrome, but the chart pane and route table must stay LTR
  (lat/lon / bearings / frequencies / ICAO codes are inherently
  left-to-right) so the layout direction needs to be applied per-widget,
  not globally. Reporting-name column already renders Hebrew correctly,
  so the existing font stack is a known-good baseline to extend from.
