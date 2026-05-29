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

"""VATSIM v3 datafeed parser + HTTP layer contract tests.

Three layers under test, each with its own block of tests below:

1. **Wake-category lookup** (``load_aircraft_wake_db`` /
   ``wake_for_aircraft_type``) — bundled JSON loads cleanly,
   returns one of the documented wake codes for known types,
   ``WAKE_UNKNOWN`` for unknown / null / FAA-prefixed types,
   and absorbs malformed / missing JSON without crashing.

2. **Pilot parsing** (``parse_pilots`` / ``_parse_one_pilot``) —
   round-trips a captured v3 fixture into ``Pilot`` records,
   drops malformed entries individually, defensively coerces
   numeric fields, and pre-resolves the wake category at parse
   time. Plus the bbox-filter geometry.

3. **HTTP fetch** (``fetch_vatsim_data``) — sends the right
   headers, honours ``If-Modified-Since`` round trips
   (200 / 304 / non-200 / parse error / unreachable), and surfaces
   each failure mode as a :class:`VatsimFetchError` with a
   distinguishable message. We mock ``urllib.request.urlopen``
   throughout — **no live VATSIM traffic in CI** under any
   circumstances.

The captured fixture at ``tests/fixtures/vatsim_data_sample.json``
deliberately exercises one parser branch per pilot entry; every
inline ``_comment`` field over there annotates which branch the
entry covers.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from cvfr_routemaster.vatsim_feed import (
    DEFAULT_TIMEOUT_S,
    USER_AGENT,
    VATSIM_DATA_URL,
    WAKE_CATEGORIES,
    WAKE_UNKNOWN,
    FetchResult,
    Pilot,
    VatsimFetchError,
    fetch_vatsim_data,
    filter_to_bbox,
    load_aircraft_wake_db,
    parse_pilots,
    wake_for_aircraft_type,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "vatsim_data_sample.json"


@pytest.fixture(scope="module")
def fixture_payload() -> dict:
    """Parsed captured VATSIM v3 fixture. Module-scoped because
    every test reads the same blob and the JSON parse is cheap
    but not free.
    """
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def wake_db() -> dict[str, str]:
    """Loaded wake-category dataset from the bundled
    ``aircraft_wake.json``. Module-scoped — the database is
    immutable after load and shared across the parsing tests.
    """
    return load_aircraft_wake_db()


# ---- wake DB load + lookup ---------------------------------------------


def test_wake_db_loads_with_nonzero_size():
    """The bundled ``aircraft_wake.json`` must contain at least one
    entry — an empty database means the resource is missing,
    malformed, or has no recognised wake codes, all of which
    silently degrade every pilot to gray. Catching that at test
    time is much better than at the friend's first launch.
    """
    db = load_aircraft_wake_db()
    assert len(db) > 0


def test_wake_db_codomain_only_contains_documented_categories():
    """Every value in the bundled database must be one of the four
    real wake categories (L/M/H/J). ``WAKE_UNKNOWN`` is the
    runtime fallback and must never appear in the JSON itself —
    otherwise the parser couldn't distinguish "explicitly unknown"
    from "I have no entry for this type".
    """
    db = load_aircraft_wake_db()
    assert set(db.values()).issubset({"L", "M", "H", "J"})


def test_wake_db_keys_are_uppercase():
    """The lookup uppercases inputs at read time, so the database
    must be stored uppercase too — otherwise round-trip lookups
    silently miss for any lowercase entry that snuck in via a
    hand-edit.
    """
    db = load_aircraft_wake_db()
    for key in db:
        assert key == key.upper(), f"non-uppercase key in wake DB: {key!r}"


def test_wake_db_handles_missing_file(tmp_path):
    """Missing bundled file → empty dict, every lookup degrades to
    ``unknown``. This is the "fresh checkout, lost the resource"
    failure mode — should not crash.
    """
    db = load_aircraft_wake_db(tmp_path / "does_not_exist.json")
    assert db == {}


def test_wake_db_handles_malformed_json(tmp_path):
    """Truncated / hand-edited JSON → empty dict, same reasoning
    as the missing-file case.
    """
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    assert load_aircraft_wake_db(bad) == {}


def test_wake_db_handles_non_dict_root(tmp_path):
    """JSON whose root is a list (or any non-dict) → empty dict.
    Catches a refactor that accidentally removes the ``types``
    nesting.
    """
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"C172": "L"}]))
    assert load_aircraft_wake_db(bad) == {}


def test_wake_db_drops_unknown_codomain_entries(tmp_path):
    """A future schema bump that adds e.g. RECAT-EU letters
    (``"A"`` through ``"F"``) must not pollute the lookup —
    unknown codomain entries are dropped, the rest are kept.
    """
    db_path = tmp_path / "synthetic.json"
    db_path.write_text(json.dumps({
        "version": 1,
        "types": {
            "C172": "L",
            "FOO1": "X",
            "BAR2": "M",
        },
    }))
    db = load_aircraft_wake_db(db_path)
    assert db == {"C172": "L", "BAR2": "M"}


@pytest.mark.parametrize(
    "type_str,expected",
    [
        ("C172", "L"),
        ("B738", "M"),
        ("A388", "J"),
        ("B748", "H"),
        ("c172", "L"),
        ("  c172  ", "L"),
    ],
)
def test_wake_lookup_known_types(type_str, expected, wake_db):
    """The lookup is case-insensitive and whitespace-trimming so
    real VATSIM payloads (which arrive in arbitrary casing)
    always resolve.
    """
    assert wake_for_aircraft_type(type_str, wake_db) == expected


@pytest.mark.parametrize("type_str", [None, "", "   ", "ZZZZ", "NOTREAL"])
def test_wake_lookup_unknown_types(type_str, wake_db):
    """Missing flight plan, empty type, or types not in our
    coverage must all resolve to :data:`WAKE_UNKNOWN` — the
    renderer paints these gray.
    """
    assert wake_for_aircraft_type(type_str, wake_db) == WAKE_UNKNOWN


def test_wake_lookup_faa_format_string(wake_db):
    """FAA flight-plan format like ``H/B748/L`` puts the ICAO type
    in the middle segment. The lookup must scan all segments and
    return the first hit — important because VATSIM's
    ``aircraft_faa`` and ``aircraft`` fields can carry this
    shape.
    """
    assert wake_for_aircraft_type("H/B748/L", wake_db) == "H"
    # Note: ``"H"`` happens to also match a literal helicopter
    # designator if one exists in the DB. As it stands the wake
    # DB has no single-letter keys, so the FAA prefix never
    # collides with a real type. If a future DB adds one, this
    # test will catch the regression.


def test_wake_lookup_faa_format_with_unknown_type(wake_db):
    """If none of the slash-separated segments hit the database,
    the lookup returns :data:`WAKE_UNKNOWN`. Catches a slip-up
    where the parser might naively take the first segment
    regardless of whether it's a known type.
    """
    assert wake_for_aircraft_type("H/ZZZZ/L", wake_db) == WAKE_UNKNOWN


# ---- pilot parsing -----------------------------------------------------


def test_parse_pilots_from_fixture_returns_expected_count(
    fixture_payload, wake_db
):
    """Eight pilots in the fixture, but one is malformed (missing
    latitude) and must be dropped. Seven survivors.
    """
    pilots = parse_pilots(fixture_payload, wake_db)
    assert len(pilots) == 7


def test_parse_pilots_skips_malformed_entries(fixture_payload, wake_db):
    """The 'BAD01' entry has no latitude — parser must drop it
    silently rather than raise. Verifying by CID so a future
    fixture reorder doesn't break the test.
    """
    pilots = parse_pilots(fixture_payload, wake_db)
    cids = {p.cid for p in pilots}
    assert 1500006 not in cids


def test_parse_pilots_resolves_wake_at_parse_time(fixture_payload, wake_db):
    """Each parsed Pilot must carry its wake category, so the
    renderer never needs to re-do the lookup. Spot-check the
    representative cases the fixture exercises.
    """
    pilots = parse_pilots(fixture_payload, wake_db)
    by_callsign = {p.callsign: p for p in pilots}
    assert by_callsign["ELY323"].wake == "M"  # B738
    assert by_callsign["4XBEN"].wake == "L"  # C172
    assert by_callsign["CLX5N"].wake in ("H", WAKE_UNKNOWN)  # H/B748/L
    # The ZZZZ pilot has a flight plan but unknown type → unknown.
    assert by_callsign["TST01"].wake == WAKE_UNKNOWN
    # The VFR pilot with no flight plan → unknown.
    assert by_callsign["4XCAL"].wake == WAKE_UNKNOWN


def test_parse_pilots_preserves_aircraft_type(fixture_payload, wake_db):
    """Whichever of ``aircraft_short`` / ``aircraft_faa`` /
    ``aircraft`` had a value gets stored verbatim on the Pilot.
    For the no-flight-plan case it stays ``None``.
    """
    pilots = parse_pilots(fixture_payload, wake_db)
    by_callsign = {p.callsign: p for p in pilots}
    assert by_callsign["ELY323"].aircraft_type == "B738"
    assert by_callsign["4XBEN"].aircraft_type == "C172"
    # CLX5N has aircraft_short=null; parser falls through to
    # aircraft_faa which is "H/B748/L".
    assert by_callsign["CLX5N"].aircraft_type == "H/B748/L"
    assert by_callsign["4XCAL"].aircraft_type is None


def test_parse_pilots_preserves_position_and_kinematics(
    fixture_payload, wake_db
):
    """Lat / lon / altitude / groundspeed / heading round-trip
    through the parser unchanged for healthy entries.
    """
    pilots = parse_pilots(fixture_payload, wake_db)
    ely = next(p for p in pilots if p.callsign == "ELY323")
    assert ely.lat == 32.0
    assert ely.lon == 34.9
    assert ely.altitude_ft == 28000
    assert ely.groundspeed_kts == 420
    assert ely.heading_deg == 87


def test_parse_pilots_normalises_heading_modulo_360(
    fixture_payload, wake_db
):
    """STR07 has heading=365 (sometimes seen for aircraft on the
    ground); parser stores 365 % 360 = 5. Catches a refactor
    that drops the modulo.
    """
    pilots = parse_pilots(fixture_payload, wake_db)
    str07 = next(p for p in pilots if p.callsign == "STR07")
    assert str07.heading_deg == 5


def test_parse_pilots_defensively_coerces_string_altitude(
    fixture_payload, wake_db
):
    """STR07 has a string-shaped altitude ('FL230'); parser falls
    back to 0 rather than crashing. Catches a refactor that
    drops the ``_safe_int`` defensive layer.
    """
    pilots = parse_pilots(fixture_payload, wake_db)
    str07 = next(p for p in pilots if p.callsign == "STR07")
    assert str07.altitude_ft == 0


def test_parse_pilots_handles_non_dict_payload(wake_db):
    """parse_pilots must gracefully accept non-dict input
    (returns empty list rather than raising). Same defensive
    contract as the wake DB loader.
    """
    assert parse_pilots(None, wake_db) == []
    assert parse_pilots([], wake_db) == []
    assert parse_pilots("not a payload", wake_db) == []


def test_parse_pilots_handles_missing_pilots_key(wake_db):
    """A v3 payload with no ``pilots`` key (e.g. the network
    is empty in some hypothetical future) returns empty list,
    not a KeyError.
    """
    assert parse_pilots({"general": {"version": 3}}, wake_db) == []


def test_parse_pilots_handles_pilots_not_a_list(wake_db):
    """If the ``pilots`` field appears but isn't a list (schema
    drift), parser returns empty list rather than crashing.
    """
    assert parse_pilots({"pilots": "oops"}, wake_db) == []
    assert parse_pilots({"pilots": {"DAL1": {}}}, wake_db) == []


def test_parse_pilots_resilient_to_individual_bad_entries(wake_db):
    """A single malformed entry inside an otherwise-healthy list
    must not cascade — the rest of the pilots still parse.
    """
    payload = {
        "pilots": [
            {"cid": 1, "latitude": 30.0, "longitude": 35.0, "callsign": "OK"},
            {"cid": "garbage", "latitude": 30.0, "longitude": 35.0},
            None,
            "not a dict",
            {"cid": 2, "latitude": 31.0, "longitude": 35.5, "callsign": "ALSO_OK"},
        ],
    }
    pilots = parse_pilots(payload, wake_db)
    assert {p.callsign for p in pilots} == {"OK", "ALSO_OK"}


# ---- bbox filter -------------------------------------------------------


def _mk_pilot(lat: float, lon: float, callsign: str = "X") -> Pilot:
    """Test helper — minimal Pilot with only the fields the bbox
    filter cares about. Default values are obviously distinct
    from anything realistic so a regression that mistakenly uses
    them is easy to spot.
    """
    return Pilot(
        cid=0,
        callsign=callsign,
        name="",
        lat=lat,
        lon=lon,
        altitude_ft=0,
        groundspeed_kts=0,
        heading_deg=0,
        transponder="",
        aircraft_type=None,
        wake=WAKE_UNKNOWN,
        flight_rules="",
        departure="",
        arrival="",
    )


def test_filter_to_bbox_keeps_inside_points():
    """Israel-ish bbox; pilots inside survive the filter."""
    pilots = [
        _mk_pilot(31.5, 35.0, "INSIDE"),
        _mk_pilot(40.0, -50.0, "ATLANTIC"),
    ]
    out = filter_to_bbox(
        pilots, min_lat=29.0, max_lat=33.5, min_lon=34.0, max_lon=36.0
    )
    assert [p.callsign for p in out] == ["INSIDE"]


def test_filter_to_bbox_inclusive_at_bounds():
    """Pilots exactly on the bbox edges are inside (closed
    interval). Catches a refactor that switches to a strict
    inequality and silently drops boundary cases.
    """
    pilots = [
        _mk_pilot(29.0, 34.0, "SW_CORNER"),
        _mk_pilot(33.5, 36.0, "NE_CORNER"),
    ]
    out = filter_to_bbox(
        pilots, min_lat=29.0, max_lat=33.5, min_lon=34.0, max_lon=36.0
    )
    assert len(out) == 2


def test_filter_to_bbox_pad_widens_bounds_in_all_directions():
    """``pad_deg`` extends the bbox uniformly on every side. A
    pilot 0.5° outside on each axis is kept when pad=1.0,
    excluded when pad=0.0.
    """
    pilots = [_mk_pilot(34.0, 36.5, "NE_OUTSIDE")]
    inside_pad = filter_to_bbox(
        pilots,
        min_lat=29.0, max_lat=33.5, min_lon=34.0, max_lon=36.0, pad_deg=1.0,
    )
    inside_no_pad = filter_to_bbox(
        pilots,
        min_lat=29.0, max_lat=33.5, min_lon=34.0, max_lon=36.0, pad_deg=0.0,
    )
    assert len(inside_pad) == 1
    assert inside_no_pad == []


def test_filter_to_bbox_drops_atlantic_pilot_from_fixture(
    fixture_payload, wake_db
):
    """End-to-end on the fixture: parse pilots, filter to Israel,
    confirm the trans-Atlantic DAL100 is excluded and the
    Israel-area ones are kept.
    """
    parsed = parse_pilots(fixture_payload, wake_db)
    filtered = filter_to_bbox(
        parsed,
        min_lat=29.0,
        max_lat=33.5,
        min_lon=34.0,
        max_lon=36.0,
    )
    callsigns = {p.callsign for p in filtered}
    assert "DAL100" not in callsigns
    assert "ELY323" in callsigns
    assert "4XCAL" in callsigns


# ---- HTTP fetch (mocked) -----------------------------------------------


class _MockResponse:
    """Stand-in for the file-like object ``urllib.request.urlopen``
    returns. Implements just enough of the API (``read``,
    ``status``, ``headers``, context-manager protocol) to satisfy
    :func:`fetch_vatsim_data`.
    """

    def __init__(
        self,
        body: bytes = b"",
        status: int = 200,
        last_modified: str | None = None,
    ) -> None:
        self._body = body
        self.status = status
        self.headers = {}
        if last_modified is not None:
            self.headers["Last-Modified"] = last_modified

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> _MockResponse:
        return self

    def __exit__(self, *exc_info) -> None:
        return None


def _mock_urlopen_returning(resp: _MockResponse, captured_requests: list):
    """Build a side_effect callable for ``urlopen`` that captures
    the outgoing :class:`urllib.request.Request` (so we can
    assert headers) and returns the supplied mock response.
    """

    def _side_effect(req, timeout=None):
        captured_requests.append((req, timeout))
        return resp

    return _side_effect


def test_fetch_sends_user_agent_header(wake_db):
    """The User-Agent header must match :data:`USER_AGENT` so
    VATSIM can identify the client per their Code of Conduct.
    Catches a refactor that drops or rewrites the header.
    """
    captured: list = []
    body = json.dumps({"pilots": []}).encode("utf-8")
    resp = _MockResponse(body=body, status=200)
    with patch(
        "urllib.request.urlopen",
        side_effect=_mock_urlopen_returning(resp, captured),
    ):
        fetch_vatsim_data(wake_db)
    assert len(captured) == 1
    req, _ = captured[0]
    assert req.headers["User-agent"] == USER_AGENT


def test_fetch_uses_default_timeout(wake_db):
    """If the caller doesn't override, the request times out at
    :data:`DEFAULT_TIMEOUT_S`. Important so a hung connection
    can't tie up the Qt worker thread for longer than the poll
    interval.
    """
    captured: list = []
    body = json.dumps({"pilots": []}).encode("utf-8")
    resp = _MockResponse(body=body, status=200)
    with patch(
        "urllib.request.urlopen",
        side_effect=_mock_urlopen_returning(resp, captured),
    ):
        fetch_vatsim_data(wake_db)
    _, timeout = captured[0]
    assert timeout == DEFAULT_TIMEOUT_S


def test_fetch_includes_if_modified_since_when_provided(wake_db):
    """When the caller passes a previously seen ``Last-Modified``,
    the next request includes ``If-Modified-Since: <that value>``
    so VATSIM can answer with a cheap 304.
    """
    captured: list = []
    body = json.dumps({"pilots": []}).encode("utf-8")
    resp = _MockResponse(body=body, status=200)
    with patch(
        "urllib.request.urlopen",
        side_effect=_mock_urlopen_returning(resp, captured),
    ):
        fetch_vatsim_data(wake_db, last_modified="Sun, 17 May 2026 12:00:00 GMT")
    req, _ = captured[0]
    assert req.headers.get("If-modified-since") == "Sun, 17 May 2026 12:00:00 GMT"


def test_fetch_omits_if_modified_since_on_first_call(wake_db):
    """First call (no previous Last-Modified): the conditional
    header must NOT be sent. Catches a refactor that always sends
    an empty string and confuses the server.
    """
    captured: list = []
    body = json.dumps({"pilots": []}).encode("utf-8")
    resp = _MockResponse(body=body, status=200)
    with patch(
        "urllib.request.urlopen",
        side_effect=_mock_urlopen_returning(resp, captured),
    ):
        fetch_vatsim_data(wake_db)
    req, _ = captured[0]
    assert "If-modified-since" not in req.headers


def test_fetch_returns_pilots_on_200(wake_db, fixture_payload):
    """Happy path: 200 OK with a valid v3 body returns parsed
    pilots and a fresh ``not_modified=False`` flag.
    """
    body = json.dumps(fixture_payload).encode("utf-8")
    resp = _MockResponse(
        body=body,
        status=200,
        last_modified="Sun, 17 May 2026 12:30:00 GMT",
    )
    with patch("urllib.request.urlopen", return_value=resp):
        result = fetch_vatsim_data(wake_db)
    assert isinstance(result, FetchResult)
    assert result.not_modified is False
    assert result.last_modified == "Sun, 17 May 2026 12:30:00 GMT"
    assert len(result.pilots) > 0


def test_fetch_returns_not_modified_on_304(wake_db):
    """304 Not Modified: empty pilots list, ``not_modified=True``,
    last_modified echoed back so the next call re-sends it.
    """
    resp = _MockResponse(body=b"", status=304)
    with patch("urllib.request.urlopen", return_value=resp):
        result = fetch_vatsim_data(
            wake_db, last_modified="Sun, 17 May 2026 12:00:00 GMT"
        )
    assert result.not_modified is True
    assert result.pilots == []
    assert result.last_modified == "Sun, 17 May 2026 12:00:00 GMT"


def test_fetch_handles_httperror_304(wake_db):
    """Some HTTP servers (and some urllib mocks) raise
    :class:`urllib.error.HTTPError` on 304 instead of returning a
    response. Both paths must converge on the same
    ``not_modified=True`` :class:`FetchResult`.
    """
    err = urllib.error.HTTPError(
        url=VATSIM_DATA_URL,
        code=304,
        msg="Not Modified",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b""),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        result = fetch_vatsim_data(
            wake_db, last_modified="Sun, 17 May 2026 12:00:00 GMT"
        )
    assert result.not_modified is True
    assert result.pilots == []


def test_fetch_raises_on_non_200_status(wake_db):
    """500 / 503 / etc. → :class:`VatsimFetchError` with the
    status code in the message. Caller (Qt worker layer) gets
    one branch to handle.
    """
    resp = _MockResponse(body=b"<html>oops</html>", status=503)
    with patch("urllib.request.urlopen", return_value=resp):
        with pytest.raises(VatsimFetchError, match="503"):
            fetch_vatsim_data(wake_db)


def test_fetch_raises_on_httperror_non_304(wake_db):
    """HTTPError path equivalent of the previous test — covers
    servers that raise instead of return.
    """
    err = urllib.error.HTTPError(
        url=VATSIM_DATA_URL,
        code=500,
        msg="Internal Server Error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b""),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(VatsimFetchError, match="500"):
            fetch_vatsim_data(wake_db)


def test_fetch_raises_on_unreachable(wake_db):
    """DNS / connect / TLS failures arrive as
    :class:`urllib.error.URLError`. Mapped to
    :class:`VatsimFetchError` with the underlying reason in the
    message so the Qt status bar can show a useful diagnostic.
    """
    err = urllib.error.URLError("Name or service not known")
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(VatsimFetchError, match="unreachable"):
            fetch_vatsim_data(wake_db)


def test_fetch_raises_on_timeout(wake_db):
    """Socket timeouts arrive as :class:`TimeoutError` (Python 3.10+)
    — mapped to :class:`VatsimFetchError`.
    """
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(VatsimFetchError, match="I/O error"):
            fetch_vatsim_data(wake_db)


def test_fetch_raises_on_invalid_json(wake_db):
    """Body that isn't valid JSON → :class:`VatsimFetchError`. We
    don't try to recover (a corrupted feed isn't actionable from
    the client side); the worker layer's "retry on next tick"
    cadence will pick up a healthy body next time.
    """
    resp = _MockResponse(body=b"<html>not json</html>", status=200)
    with patch("urllib.request.urlopen", return_value=resp):
        with pytest.raises(VatsimFetchError, match="not valid JSON"):
            fetch_vatsim_data(wake_db)


def test_fetch_raises_on_json_root_not_a_dict(wake_db):
    """Body parses as JSON but the root is a list (or any non-dict)
    — that's a v3 schema violation, not a transient blip. Surface
    as a fetch error so the worker logs and retries; the data
    layer doesn't try to guess what the server meant.
    """
    resp = _MockResponse(body=b"[1, 2, 3]", status=200)
    with patch("urllib.request.urlopen", return_value=resp):
        with pytest.raises(VatsimFetchError, match="root is not a JSON object"):
            fetch_vatsim_data(wake_db)


def test_fetch_propagates_url_override(wake_db):
    """``url`` kwarg lets tests / future-staging point the fetcher
    at a different endpoint without monkey-patching module
    state. Verifies the kwarg actually drives the outgoing URL
    rather than being silently ignored.
    """
    captured: list = []
    body = json.dumps({"pilots": []}).encode("utf-8")
    resp = _MockResponse(body=body, status=200)
    with patch(
        "urllib.request.urlopen",
        side_effect=_mock_urlopen_returning(resp, captured),
    ):
        fetch_vatsim_data(wake_db, url="https://example.invalid/feed.json")
    req, _ = captured[0]
    assert req.full_url == "https://example.invalid/feed.json"


# ---- module-level constants --------------------------------------------


def test_vatsim_url_is_v3_endpoint():
    """The default URL must point at the documented v3 endpoint.
    Catches a typo or accidental rollback to the retired v1 URL.
    """
    assert VATSIM_DATA_URL == "https://data.vatsim.net/v3/vatsim-data.json"


def test_user_agent_carries_vatsim_id():
    """VATSIM's Code of Conduct asks for an identifying
    User-Agent. Catches an accidental change that drops the
    contact handle (the VATSIM user ID).
    """
    assert "VATSIM User ID:" in USER_AGENT
    assert "Israel CVFR Routemaster" in USER_AGENT


def test_wake_categories_includes_all_documented():
    """The five-element tuple is a load-bearing contract: the
    renderer iterates it to pre-cache silhouette pixmaps in each
    color. A typo in the constant would silently cause a missing
    color at render time.
    """
    assert set(WAKE_CATEGORIES) == {"L", "M", "H", "J", WAKE_UNKNOWN}
