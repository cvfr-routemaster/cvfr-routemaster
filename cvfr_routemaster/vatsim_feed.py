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

"""VATSIM v3 datafeed — pure-Python data layer for the live traffic
overlay (v2 feature; see ``ROADMAP-NEXT.md``).

This module deliberately has **zero Qt dependency** so it can be
unit-tested without a ``QApplication`` and reasoned about as plain
data plumbing. The Qt thread + timer that drives periodic polling
lives separately in ``cvfr_routemaster.vatsim_worker`` (lands in a
later batch); the on-chart rendering lives in
``cvfr_routemaster.traffic_overlay``.

Three layers, top-down:

1. :class:`Pilot` — the immutable record we hand on to the Qt
   layer. Just enough fields to draw a callsign-labelled
   silhouette and tooltip; everything else from the raw VATSIM
   payload is discarded.

2. :func:`parse_pilots` — defensive JSON-dict → ``list[Pilot]``
   conversion. Any single malformed entry is skipped (not
   raised) so one corrupted record from VATSIM doesn't black
   out the whole overlay. Wake category is resolved here using
   a pre-loaded :data:`WakeDB` so the parser has no I/O.

3. :func:`fetch_vatsim_data` — the HTTP layer. Sends the
   ``User-Agent`` VATSIM's Code of Conduct expects, supports
   ``If-Modified-Since`` for the polite 15-second cadence, and
   raises a :class:`VatsimFetchError` on every failure mode the
   caller might want to distinguish (network, HTTP status,
   parse, schema). Raising rather than swallowing keeps the
   data layer honest — the Qt worker layer is the right place
   to decide whether a transient network blip should hide or
   keep the previous traffic list.

Wake-category lookup
--------------------

The bundled ``cvfr_routemaster/resources/aircraft_wake.json``
maps ICAO type designators (e.g. ``"B738"``, ``"C172"``) to one
of ``"L" / "M" / "H" / "J"``. Anything not in the table — or
whose ``flight_plan`` block is missing entirely (most common
for VFR pilots without a filed plan) — resolves to
:data:`WAKE_UNKNOWN`. The renderer paints unknown traffic in a
neutral gray so it's visually obvious that we couldn't classify
the aircraft.

Why bake the lookup into the parser
-----------------------------------

We resolve wake category at parse time, not at render time, so
the resulting :class:`Pilot` is fully self-contained and the Qt
layer has nothing to look up beyond colour-coding the icon by
``pilot.wake``. This keeps the render hot path branch-free.

VATSIM data feed contract
-------------------------

URL: ``https://data.vatsim.net/v3/vatsim-data.json``.
Regenerated server-side every 15 seconds; polling more often is
wasted bandwidth and explicitly discouraged by VATSIM. The feed
is unauthenticated but VATSIM's Code of Conduct asks every
client to send a descriptive ``User-Agent`` so they can contact
the maintainer if something misbehaves — that's what
:data:`USER_AGENT` is for.

The v3 schema (December 2020) puts pilots under a top-level
``"pilots"`` array; each entry has ``cid``, ``name``,
``callsign``, ``latitude``, ``longitude``, ``altitude``,
``groundspeed``, ``heading``, ``transponder``, ``qnh_i_hg``,
``qnh_mb``, ``last_updated``, ``logon_time``, plus an optional
``flight_plan`` sub-object. Older v1/text formats are no longer
served (retired March 2021) so we don't bother with format
detection.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# --- VATSIM HTTP contract ------------------------------------------------

#: Canonical v3 datafeed URL. Hard-coded — VATSIM publishes mirror
#: addresses through ``status.vatsim.net/status.json`` for clients
#: that want failover, but a single user clicking around their
#: local airspace doesn't need that machinery; if the primary is
#: down we'll log and try again 15 s later.
VATSIM_DATA_URL = "https://data.vatsim.net/v3/vatsim-data.json"

#: HTTP User-Agent the feed sees on every request. VATSIM's Code of
#: Conduct asks clients to identify themselves with enough detail
#: that the network operators can reach the maintainer if a client
#: misbehaves (excess polling, schema misuse, etc.). The user's
#: VATSIM ID is the right contact handle because they're the
#: account that authored this client.
#:
#: Format is free-form per VATSIM guidance; the dash-separated
#: shape mirrors what other community tooling uses.
USER_AGENT = (
    "Israel CVFR Routemaster Application "
    "- Created by VATSIM User ID: 1980623"
)

#: Default request timeout (seconds). 10 s is a comfortable margin
#: for the v3 feed which is normally < 200 ms on a healthy CDN
#: edge; long enough to absorb a transient stall, short enough
#: that a hung connection doesn't keep the Qt worker thread tied
#: up for a full poll cycle.
DEFAULT_TIMEOUT_S = 10.0


# --- Wake categories -----------------------------------------------------

#: Sentinel for "we don't know what this aircraft is" — used when
#: the pilot has no flight plan filed (common for VFR), the
#: aircraft type isn't in our bundled lookup, or the type field is
#: empty / malformed. The renderer colours this category gray.
WAKE_UNKNOWN = "unknown"

#: All five categories the renderer can paint. Order is purely
#: documentary; the parser stores whatever the lookup returns.
WAKE_CATEGORIES: tuple[str, ...] = ("L", "M", "H", "J", WAKE_UNKNOWN)


# --- WakeDB --------------------------------------------------------------

#: Type alias — the in-memory representation of the bundled
#: ``aircraft_wake.json``. ``str`` (uppercase ICAO designator) →
#: one of ``WAKE_CATEGORIES`` minus ``WAKE_UNKNOWN`` (the JSON
#: never stores ``unknown`` — that's reserved for "key absent").
WakeDB = dict[str, str]


def _wake_db_path() -> Path:
    """On-disk location of the bundled wake-category dataset.

    Same path-resolution trick as :mod:`cvfr_routemaster.app_icon`:
    ``Path(__file__).parent / "resources"`` resolves to a real
    directory in both dev (``cvfr_routemaster/resources/`` in the
    checkout) and frozen builds (PyInstaller extracts the
    ``datas`` payload into ``sys._MEIPASS`` and rewrites
    ``__file__`` to point at the extracted copy).
    """
    return Path(__file__).resolve().parent / "resources" / "aircraft_wake.json"


def load_aircraft_wake_db(path: Path | None = None) -> WakeDB:
    """Read the bundled ``aircraft_wake.json`` and return a flat
    ``{type: wake}`` dict.

    Defensive contract:

    * Missing file → empty dict (every lookup falls through to
      :data:`WAKE_UNKNOWN`). Means a fresh checkout that's lost
      the resource still renders traffic — it just paints
      everything gray. Less surprising than a hard crash.
    * Malformed JSON / wrong shape → empty dict, same reasoning.
    * Unknown wake category in a JSON entry → that entry is
      dropped, but the rest of the file is honoured. A future
      schema bump that adds e.g. RECAT-EU letters won't quietly
      pollute the codomain.

    Args:
        path: Override the default bundled path. Tests use this
            to point at a synthetic JSON without monkey-patching.

    Returns:
        A flat ``dict[str, str]`` with uppercase ICAO type
        designators as keys. The codomain is restricted to
        ``{"L", "M", "H", "J"}``.
    """
    p = path if path is not None else _wake_db_path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    types = raw.get("types")
    if not isinstance(types, dict):
        return {}
    valid_codomain = {"L", "M", "H", "J"}
    out: WakeDB = {}
    for k, v in types.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if v not in valid_codomain:
            continue
        out[k.strip().upper()] = v
    return out


def wake_for_aircraft_type(type_str: str | None, db: WakeDB) -> str:
    """Resolve an ICAO type designator to a wake category.

    Returns :data:`WAKE_UNKNOWN` for any of:

    * ``type_str`` is ``None`` (no flight plan filed).
    * ``type_str`` is empty or whitespace-only.
    * ``type_str`` isn't in ``db`` (rare type or our coverage
      gap; renderer paints gray).

    The lookup is case-insensitive (uppercase the input before
    indexing) because real VATSIM payloads contain mixed-case
    type designators surprisingly often — pilots type them in
    by hand on connect.

    The first ICAO type designator can also be prefixed by a
    wake-letter / equipment suffix in some flight planning tools
    (e.g. ``"H/B738/L"``); we look at only the first slash-
    separated segment so those still resolve.
    """
    if type_str is None:
        return WAKE_UNKNOWN
    s = str(type_str).strip()
    if not s:
        return WAKE_UNKNOWN
    # FAA-format flight-plan strings (which VATSIM's
    # ``aircraft_faa`` / ``aircraft`` fields sometimes carry) wrap
    # the ICAO type designator with a wake-letter prefix and an
    # equipment suffix: ``"H/B738/L"`` means heavy + B738 +
    # equipment "L". The actual type designator is the *middle*
    # segment when the leading segment is a single character.
    #
    # Look up every slash-separated segment in turn, taking the
    # first one that hits the database. This handles all of:
    #   * ``"B738"``               → first hit
    #   * ``"H/B738/L"``           → second segment hits
    #   * ``"B738/L"``             → first segment hits (no prefix)
    #   * ``"unknown/junk"``       → no hit, return WAKE_UNKNOWN
    # without needing to predict which exact shape VATSIM hands us.
    for seg in s.split("/"):
        head = seg.strip().upper()
        if not head:
            continue
        if head in db:
            return db[head]
    return WAKE_UNKNOWN


# --- Pilot dataclass -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pilot:
    """One VATSIM pilot, distilled to the fields the on-chart
    overlay needs.

    Frozen + slotted so the Qt layer can hash/compare pilots
    safely (e.g. to diff frame-over-frame for animation) and the
    parser can't accidentally mutate a record after construction.

    Fields:
        cid: Numeric VATSIM Certificate ID. Stable across polls
            for the same logged-in pilot, so the renderer can use
            it as a key for "same plane, new position" updates.
        callsign: Free-form ASCII callsign as the pilot typed it
            (e.g. ``"ELY323"``, ``"4XCAL"``). Drawn next to the
            silhouette.
        name: Optional display name VATSIM publishes alongside
            (e.g. ``"Yaron Levi LLBG"``); rendered in the tooltip.
        lat / lon: Decimal degrees, WGS84.
        altitude_ft: Pressure altitude in feet, as VATSIM serves
            it (the feed doesn't apply a QNH correction; we don't
            either).
        groundspeed_kts: Knots over the ground.
        heading_deg: True heading in degrees [0, 360); 0 = north,
            90 = east. Used to rotate the silhouette.
        transponder: Squawk code as a 4-character string
            (e.g. ``"7000"``); kept for the tooltip.
        aircraft_type: ICAO type designator from the filed flight
            plan (e.g. ``"B738"``), or ``None`` if the pilot has
            no flight plan.
        wake: One of :data:`WAKE_CATEGORIES`. Pre-resolved at
            parse time so the renderer doesn't need a lookup.
        flight_rules: ``"I"`` (IFR), ``"V"`` (VFR), ``"Y"``
            (IFR/VFR), ``"Z"`` (VFR/IFR), or empty. Strictly
            informational for the tooltip — the icon style
            doesn't change based on flight rules.
        departure / arrival: ICAO airport codes from the filed
            plan, or empty strings. Tooltip-only.
    """

    cid: int
    callsign: str
    name: str
    lat: float
    lon: float
    altitude_ft: int
    groundspeed_kts: int
    heading_deg: int
    transponder: str
    aircraft_type: str | None
    wake: str
    flight_rules: str
    departure: str
    arrival: str


# --- Parsing -------------------------------------------------------------


class VatsimFetchError(RuntimeError):
    """Raised by :func:`fetch_vatsim_data` for any failure mode the
    caller might want to surface differently from a healthy 0-pilot
    response.

    Distinct subclasses are deliberately *not* defined here — the
    Qt worker layer treats every failure the same way (retry on
    next tick, leave the previous list visible) so a single
    exception type with a descriptive message is the simplest
    contract that fits both ends.
    """


def _safe_int(value: Any, default: int = 0) -> int:
    """Coerce a JSON number / string to int, falling back to
    ``default`` on anything funky.

    Used inside the parser because VATSIM serves altitude /
    groundspeed / heading as integers in nominal cases but I've
    seen a stray ``null`` and the occasional ``"FL230"``-shaped
    string in user-edited fields. Defensive coercion keeps one
    odd record from blacking out the whole overlay.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _safe_float(value: Any, default: float | None = None) -> float | None:
    """Same defensive coercion as :func:`_safe_int` but for floats.

    Returns ``None`` (not 0.0) for malformed lat/lon so the parser
    can drop the entry entirely — a pilot at (0, 0) is a real
    valid position off the coast of Africa and we don't want
    malformed input to teleport pilots there.
    """
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _parse_one_pilot(entry: Any, wake_db: WakeDB) -> Pilot | None:
    """Convert one entry of the v3 ``pilots`` array to a
    :class:`Pilot`, or return ``None`` when the entry is too
    malformed to be useful.

    ``None`` return cases (entry is dropped, parser continues):

    * Not a dict at all.
    * Missing ``cid`` (no stable identity).
    * Missing or non-numeric ``latitude`` / ``longitude``.
    * ``cid`` doesn't coerce to int.

    Everything else fills with sensible defaults rather than
    rejecting — a pilot with a missing heading still deserves a
    dot on the chart, just one that can't be rotated.
    """
    if not isinstance(entry, dict):
        return None
    cid_raw = entry.get("cid")
    if cid_raw is None:
        return None
    try:
        cid = int(cid_raw)
    except (TypeError, ValueError):
        return None

    lat = _safe_float(entry.get("latitude"))
    lon = _safe_float(entry.get("longitude"))
    if lat is None or lon is None:
        return None

    fp = entry.get("flight_plan")
    aircraft_type: str | None = None
    flight_rules = ""
    departure = ""
    arrival = ""
    if isinstance(fp, dict):
        ac = fp.get("aircraft_short") or fp.get("aircraft_faa") or fp.get("aircraft")
        if isinstance(ac, str) and ac.strip():
            aircraft_type = ac.strip()
        flight_rules = _safe_str(fp.get("flight_rules"))
        departure = _safe_str(fp.get("departure"))
        arrival = _safe_str(fp.get("arrival"))

    wake = wake_for_aircraft_type(aircraft_type, wake_db)

    return Pilot(
        cid=cid,
        callsign=_safe_str(entry.get("callsign")),
        name=_safe_str(entry.get("name")),
        lat=lat,
        lon=lon,
        altitude_ft=_safe_int(entry.get("altitude")),
        groundspeed_kts=_safe_int(entry.get("groundspeed")),
        heading_deg=_safe_int(entry.get("heading")) % 360,
        transponder=_safe_str(entry.get("transponder")),
        aircraft_type=aircraft_type,
        wake=wake,
        flight_rules=flight_rules,
        departure=departure,
        arrival=arrival,
    )


def parse_pilots(payload: Any, wake_db: WakeDB) -> list[Pilot]:
    """Convert a parsed VATSIM v3 datafeed dict to a list of
    :class:`Pilot` records.

    Defensive against every shape we could realistically see:

    * ``payload`` not a dict → empty list.
    * ``"pilots"`` key missing or not a list → empty list.
    * Individual entries that fail :func:`_parse_one_pilot` are
      skipped without raising; the rest of the list still
      renders.

    This function is ``O(N)`` in the number of pilots; ``N`` is
    typically 200–800 globally. The bbox filter
    (:func:`filter_to_bbox`) is the next stage and can shrink
    that to a handful for an Israel-only render.
    """
    if not isinstance(payload, dict):
        return []
    raw_pilots = payload.get("pilots")
    if not isinstance(raw_pilots, list):
        return []
    out: list[Pilot] = []
    for entry in raw_pilots:
        pilot = _parse_one_pilot(entry, wake_db)
        if pilot is not None:
            out.append(pilot)
    return out


# --- Bounding-box filter -------------------------------------------------


def filter_to_bbox(
    pilots: list[Pilot],
    *,
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    pad_deg: float = 0.0,
) -> list[Pilot]:
    """Return only pilots inside ``[min_lat - pad, max_lat + pad] ×
    [min_lon - pad, max_lon + pad]``.

    The bbox in production comes from the calibrated chart's
    geographic extents (computed by inverting the affine on the
    pixmap corners). Padding in degrees lets us include traffic
    just beyond the visible area so a fast jet doesn't pop into
    view exactly at the chart edge — 1° latitude ≈ 60 nm, so
    even ``pad_deg=0.5`` gives ~30 nm of approach room.

    No antimeridian handling — the chart this app exists for
    covers Israel (lat ~29-33, lon ~34-36) and the antimeridian
    is on the literal opposite side of the planet. If a future
    use case needs Pacific charts the caller should split the
    bbox into two and concatenate; that complexity has no place
    here.
    """
    lo_lat = min_lat - pad_deg
    hi_lat = max_lat + pad_deg
    lo_lon = min_lon - pad_deg
    hi_lon = max_lon + pad_deg
    return [
        p for p in pilots
        if lo_lat <= p.lat <= hi_lat and lo_lon <= p.lon <= hi_lon
    ]


# --- HTTP fetch ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchResult:
    """What :func:`fetch_vatsim_data` returns on a successful poll.

    Three shapes the caller needs to distinguish:

    * Fresh data → ``pilots`` populated, ``not_modified=False``,
      ``last_modified`` set if the server sent a ``Last-Modified``
      header (typical) or ``None`` if it didn't.

    * Server says "nothing new since the timestamp you sent" →
      ``pilots`` empty, ``not_modified=True``. The Qt worker
      layer keeps showing the previous list. This is the polite
      15-second-poll behaviour.

    * Pilot list is genuinely empty (fresh response, just no one
      online — never happens in practice on VATSIM but is a
      legitimate state) → ``pilots`` empty,
      ``not_modified=False``. Caller clears the overlay.

    The ``not_modified`` flag is what disambiguates an empty list
    "because nothing changed" from "because the world is empty".
    """

    pilots: list[Pilot]
    not_modified: bool
    last_modified: str | None


def fetch_vatsim_data(
    wake_db: WakeDB,
    *,
    url: str = VATSIM_DATA_URL,
    timeout: float = DEFAULT_TIMEOUT_S,
    last_modified: str | None = None,
    user_agent: str = USER_AGENT,
) -> FetchResult:
    """Fetch the v3 VATSIM datafeed and parse it into a list of
    :class:`Pilot` records.

    Sends ``If-Modified-Since`` when ``last_modified`` is provided
    so the polite 15-second poll cadence costs only a 304 round
    trip when nothing has changed server-side. The server's
    ``Last-Modified`` response header is returned in
    :class:`FetchResult` so the caller can feed it back on the
    next call.

    Args:
        wake_db: The pre-loaded wake-category lookup. Pass an
            empty dict to short-circuit lookup (every pilot ends
            up :data:`WAKE_UNKNOWN`).
        url: Override the data feed URL. Used by tests to point
            at a fixture server; production uses the default.
        timeout: Per-request timeout in seconds.
        last_modified: Last seen ``Last-Modified`` header value,
            or ``None`` on the first call. The HTTP header is an
            opaque string per RFC 7232; we don't parse or
            normalise it — VATSIM emits it, we echo it back.
        user_agent: Override the User-Agent header (tests). The
            default identifies this client per VATSIM's Code of
            Conduct.

    Raises:
        VatsimFetchError: On any of:

            * Network failure (DNS, connect, TLS, timeout).
            * HTTP status that isn't 200 or 304.
            * Response body that isn't valid JSON.
            * Response body parses but isn't a dict
              (i.e. completely unrecognisable as a v3 feed —
              an empty pilots list IS recognisable and yields
              an empty :class:`FetchResult`).

        We map each underlying error to a :class:`VatsimFetchError`
        with a short human-readable message so the Qt status-bar
        can surface a useful diagnostic without leaking
        ``urllib`` internals.
    """
    headers = {"User-Agent": user_agent}
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = getattr(resp, "status", None) or resp.getcode()
            if status == 304:
                # Nothing changed since ``last_modified``. The
                # server doesn't bother sending a body — return
                # an empty :class:`FetchResult` flagged as such
                # so the worker keeps its previous list.
                return FetchResult(
                    pilots=[], not_modified=True, last_modified=last_modified
                )
            if status != 200:
                raise VatsimFetchError(
                    f"VATSIM data feed returned HTTP {status}"
                )
            body = resp.read()
            new_last_modified = resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as exc:
        # Some HTTP servers raise on non-200 instead of returning
        # a response object — catch and remap to our exception
        # type so the caller has one branch to handle.
        if exc.code == 304:
            return FetchResult(
                pilots=[], not_modified=True, last_modified=last_modified
            )
        raise VatsimFetchError(
            f"VATSIM data feed returned HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise VatsimFetchError(
            f"VATSIM data feed unreachable: {exc.reason}"
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise VatsimFetchError(f"VATSIM data feed I/O error: {exc}") from exc

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VatsimFetchError(f"VATSIM data feed payload not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise VatsimFetchError(
            "VATSIM data feed payload root is not a JSON object."
        )

    pilots = parse_pilots(payload, wake_db)
    return FetchResult(
        pilots=pilots,
        not_modified=False,
        last_modified=new_last_modified,
    )
