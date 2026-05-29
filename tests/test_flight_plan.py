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

"""Tests for the Save / Load Flight Plan parser in ``cvfr_routemaster.route``.

The Save / Load feature persists a route as a single ICAO Field 15 line — the
same string the panel already shows above the table — and parses it back into
a list of :class:`ParsedPlanCode` / :class:`ParsedPlanCoord` tokens on load.
The grammar is intentionally narrow per the feature spec: only 4- or 5-letter
uppercase alphabetic codes and the 11-char ICAO ``DDMM[NS]DDDMM[EW]`` coord
tokens, separated by single ASCII spaces. Anything else must be rejected
with a clear, position-tagged error.

These tests pin two contracts:

1. **Parser correctness** — every shape from the formatter must round-trip
   through the parser unchanged, every legal hand-edited variant must parse,
   and every illegal variant must raise :class:`FlightPlanParseError` with
   enough context (``position`` + ``token``) for the UI to point the user at
   the exact byte to fix.

2. **Round-trip with the formatter** — :func:`to_icao_route_string` (called
   with ``include_intermediates=True``) and :func:`parse_icao_route_string`
   form an exact inverse pair: format → parse → structure-equivalent token
   list. This is what makes "save then load" idempotent across machines.

No Qt / UI here. Pure-data tests; fast.
"""

from __future__ import annotations

import math

import pytest

from cvfr_routemaster.route import (
    FlightPlanParseError,
    ParsedPlanCode,
    ParsedPlanCoord,
    Route,
    RoutePoint,
    default_save_plan_name,
    format_icao_coord,
    parse_icao_coord_token,
    parse_icao_route_string,
    to_icao_route_string,
)
from cvfr_routemaster.waypoint_types import WaypointRecord


def _wp(code: str, lat: float, lon: float) -> WaypointRecord:
    return WaypointRecord(
        index=0,
        code=code,
        name_he="",
        reporting_type="MR",
        lat=lat,
        lon=lon,
        lat_dms="",
        lon_dms="",
    )


# ---------------------------------------------------------------------------
# Positive cases: every legal token shape parses to the expected structure
# ---------------------------------------------------------------------------


def test_single_four_letter_airport_code_parses_as_ParsedPlanCode() -> None:
    """The minimal valid plan: one ICAO 4-letter airport code (e.g. ``LLBG``,
    Tel Aviv). Used by the simplest "fly from A to B" plans where source =
    destination (the pilot is using the save file as a placeholder)."""
    tokens = parse_icao_route_string("LLBG")
    assert tokens == [ParsedPlanCode(code="LLBG")]


def test_single_five_letter_waypoint_code_parses_as_ParsedPlanCode() -> None:
    """5-letter codes are the CVFR fix style (e.g. ``DAROM``, ``GALIM``).
    Parser must accept them on the same code path as 4-letter codes — the
    grammar deliberately doesn't try to distinguish "airport" vs "waypoint"
    at parse time; resolution lives in MainWindow which looks both up in
    the same WaypointRecord table."""
    tokens = parse_icao_route_string("DAROM")
    assert tokens == [ParsedPlanCode(code="DAROM")]


def test_single_coord_token_parses_to_ParsedPlanCoord_with_correct_floats() -> None:
    """``3133N03433E`` → lat 31°33'N, lon 34°33'E → 31.55°, 34.55°. Round
    trip with the formatter is the strict contract; this pins the absolute
    numeric value too in case someone "simplifies" the parser by reusing a
    different lat/lon convention."""
    tokens = parse_icao_route_string("3133N03433E")
    assert len(tokens) == 1
    tok = tokens[0]
    assert isinstance(tok, ParsedPlanCoord)
    assert math.isclose(tok.lat, 31.0 + 33.0 / 60.0, rel_tol=1e-12)
    assert math.isclose(tok.lon, 34.0 + 33.0 / 60.0, rel_tol=1e-12)
    assert tok.text == "3133N03433E"


def test_southern_hemisphere_coord_yields_negative_latitude() -> None:
    """N/S flips lat sign. Even though Israeli CVFR plans never go south,
    the parser is general-ICAO and must round-trip correctly so it stays a
    well-behaved building block if reused elsewhere."""
    tok = parse_icao_route_string("0530S03433E")[0]
    assert isinstance(tok, ParsedPlanCoord)
    assert math.isclose(tok.lat, -(5.0 + 30.0 / 60.0), rel_tol=1e-12)
    assert math.isclose(tok.lon, 34.0 + 33.0 / 60.0, rel_tol=1e-12)


def test_western_hemisphere_coord_yields_negative_longitude() -> None:
    """E/W flips lon sign. Same general-ICAO completeness rationale as the
    N/S test above."""
    tok = parse_icao_route_string("3133N12000W")[0]
    assert isinstance(tok, ParsedPlanCoord)
    assert math.isclose(tok.lat, 31.0 + 33.0 / 60.0, rel_tol=1e-12)
    assert math.isclose(tok.lon, -120.0, rel_tol=1e-12)


def test_mixed_route_string_parses_in_source_order() -> None:
    """Typical end-to-end plan: airport + waypoint + intermediate coord +
    airport. Order matters (the route is a sequence, not a set), so the
    test pins the exact list."""
    tokens = parse_icao_route_string("LLBG DAROM 3133N03433E LLHA")
    assert tokens == [
        ParsedPlanCode(code="LLBG"),
        ParsedPlanCode(code="DAROM"),
        ParsedPlanCoord(
            lat=31.0 + 33.0 / 60.0, lon=34.0 + 33.0 / 60.0, text="3133N03433E"
        ),
        ParsedPlanCode(code="LLHA"),
    ]


def test_trailing_newline_does_not_break_parsing() -> None:
    """Files saved with ``write_text + "\\n"`` (which is what the
    controller does) end in a single LF. The parser must strip outer
    whitespace before applying its strict single-space-between-tokens
    rule; otherwise the trailing LF would split into a 5th empty 'token'
    and the whole grammar would reject any saved file."""
    tokens = parse_icao_route_string("LLBG DAROM\n")
    assert tokens == [
        ParsedPlanCode(code="LLBG"),
        ParsedPlanCode(code="DAROM"),
    ]


def test_leading_and_trailing_whitespace_are_stripped() -> None:
    """Defensive: a hand-pasted plan with stray surrounding spaces should
    parse cleanly. The strict single-space rule applies *between* tokens,
    not to the file's outer whitespace envelope."""
    tokens = parse_icao_route_string("   LLBG LLHA  \n")
    assert tokens == [ParsedPlanCode(code="LLBG"), ParsedPlanCode(code="LLHA")]


# ---------------------------------------------------------------------------
# Negative cases: every illegal variant must raise with useful context
# ---------------------------------------------------------------------------


def test_empty_string_raises_with_explanatory_message() -> None:
    """An empty plan file is not a meaningful artefact — there's no route
    to load. The error message should say so explicitly so the user
    doesn't think the parser failed silently on otherwise-valid content."""
    with pytest.raises(FlightPlanParseError) as exc:
        parse_icao_route_string("")
    assert "empty" in str(exc.value).lower()


def test_whitespace_only_string_raises_with_same_empty_message() -> None:
    """A file containing only whitespace (e.g. a stray editor save of a
    deleted plan) should error out the same way — the parser strips outer
    whitespace and then sees nothing, which is indistinguishable from an
    empty file."""
    with pytest.raises(FlightPlanParseError) as exc:
        parse_icao_route_string("   \n\n  ")
    assert "empty" in str(exc.value).lower()


def test_three_letter_code_is_rejected_as_too_short() -> None:
    """The grammar is exactly {4, 5}-letter codes. ``LLB`` (3 letters) is
    not an ICAO airport code (those are 4) and not a CVFR fix (those are 5)
    — pinning the rejection here catches a "relax to 3+" regression that
    would silently accept non-ICAO codes from older paperwork."""
    with pytest.raises(FlightPlanParseError) as exc:
        parse_icao_route_string("LLB")
    assert exc.value.token == "LLB"
    assert exc.value.position == 1


def test_six_letter_code_is_rejected_as_too_long() -> None:
    """Symmetric to the 3-letter case. Real ICAO has no 6-letter fix code in
    the route-string grammar; rejecting protects against typos like
    ``DAROMM`` slipping through and being silently treated as unknown."""
    with pytest.raises(FlightPlanParseError) as exc:
        parse_icao_route_string("DAROMM")
    assert exc.value.token == "DAROMM"
    assert exc.value.position == 1


def test_lowercase_code_is_rejected() -> None:
    """Strict-uppercase is part of the contract: the formatter only ever
    emits uppercase, so a lowercase token can't be the output of any
    legitimate save. Rejecting it forces the user to fix the file (or
    re-save from the app) rather than silently accepting near-miss data."""
    with pytest.raises(FlightPlanParseError) as exc:
        parse_icao_route_string("llbg")
    assert exc.value.token == "llbg"


def test_mixed_case_code_is_rejected() -> None:
    """Companion to the lowercase test — ``Llbg`` is a typo, not a code."""
    with pytest.raises(FlightPlanParseError):
        parse_icao_route_string("Llbg")


def test_code_with_digit_is_rejected() -> None:
    """4/5 *letters* — no digits. ``LL1G`` is not a code; if a future spec
    needs alphanumeric fixes, that's a deliberate widening, not a silent
    accident."""
    with pytest.raises(FlightPlanParseError) as exc:
        parse_icao_route_string("LL1G")
    assert exc.value.token == "LL1G"


def test_double_space_between_tokens_is_rejected() -> None:
    """The grammar specifies *single* spaces between tokens. Allowing
    extra spaces would make the format ambiguous to a strict reader
    (e.g. an external script) and weaken the round-trip contract with
    the formatter which always emits exactly one space."""
    with pytest.raises(FlightPlanParseError) as exc:
        parse_icao_route_string("LLBG  LLHA")
    # The empty "token" between the two spaces should be the offender.
    assert exc.value.token == ""
    assert exc.value.position == 2


def test_tab_separator_is_rejected() -> None:
    """Only ASCII space (0x20) is permitted as the separator. A tab
    sneaking in from a text editor that auto-replaces spaces is a real
    user mistake; rejecting it loudly is the right behaviour."""
    with pytest.raises(FlightPlanParseError):
        parse_icao_route_string("LLBG\tLLHA")


def test_coord_minutes_60_is_rejected() -> None:
    """``3160N`` is malformed even though it matches the regex shape —
    minutes max at 59. The formatter's 60-carry logic guarantees we
    never *emit* this, but a hand-edited file might; the parser catches
    it with a specific minutes-range message."""
    with pytest.raises(FlightPlanParseError) as exc:
        parse_icao_route_string("3160N03433E")
    assert "60" in str(exc.value) or "minutes" in str(exc.value).lower()


def test_coord_latitude_above_90_is_rejected() -> None:
    """``9101N`` is north of the pole — physically impossible. Stronger
    range check than the regex provides; pinned so a "loosen the range
    check, regex is enough" simplification can't sneak in."""
    with pytest.raises(FlightPlanParseError):
        parse_icao_route_string("9101N03433E")


def test_coord_longitude_above_180_is_rejected() -> None:
    """Same idea for the longitude axis (180° is the antimeridian,
    anything above wraps and is ambiguous)."""
    with pytest.raises(FlightPlanParseError):
        parse_icao_route_string("3133N18101E")


def test_garbage_token_in_middle_carries_correct_position() -> None:
    """Position counting is 1-based and counts from the start of the plan
    so the UI's "token #N" hint matches what a pilot would count by eye.
    Pin position=2 specifically for a mid-route bad token — off-by-one
    here would be a quietly annoying user-facing bug."""
    with pytest.raises(FlightPlanParseError) as exc:
        parse_icao_route_string("LLBG GARBAGE_TOKEN LLHA")
    assert exc.value.position == 2
    assert exc.value.token == "GARBAGE_TOKEN"


def test_garbage_token_at_end_carries_correct_position() -> None:
    """Position counting at the *end* of the plan — same off-by-one risk
    as the middle case, but exercised separately to catch a regression
    that only mishandles the final-token branch."""
    with pytest.raises(FlightPlanParseError) as exc:
        parse_icao_route_string("LLBG LLHA garbage")
    assert exc.value.position == 3
    assert exc.value.token == "garbage"


# ---------------------------------------------------------------------------
# parse_icao_coord_token: standalone unit (the parser's narrow inner pass)
# ---------------------------------------------------------------------------


def test_parse_icao_coord_token_round_trips_with_format_icao_coord() -> None:
    """The two functions are exact inverses for coordinates whose minutes
    are integer (which is everything the formatter ever emits, by
    construction). Cross-checked at a handful of representative chart
    locations spanning the Israeli operating area + a southern-hemisphere
    sanity sample so a sign-handling bug couldn't slip through."""
    samples = [
        (31.0 + 33.0 / 60.0, 34.0 + 33.0 / 60.0),  # central Israel
        (33.0 + 0.0 / 60.0, 35.0 + 0.0 / 60.0),  # whole-degree corner
        (32.0 + 59.0 / 60.0, 35.0 + 59.0 / 60.0),  # near-minute-rollover
        (-5.0 + -30.0 / 60.0, 0.0 + 0.0 / 60.0),  # equatorial S, prime meridian
    ]
    for lat, lon in samples:
        token = format_icao_coord(lat, lon)
        parsed_lat, parsed_lon = parse_icao_coord_token(token)
        assert math.isclose(parsed_lat, lat, abs_tol=1e-9), (
            f"lat round-trip failed for {token!r}: {parsed_lat} vs {lat}"
        )
        assert math.isclose(parsed_lon, lon, abs_tol=1e-9), (
            f"lon round-trip failed for {token!r}: {parsed_lon} vs {lon}"
        )


def test_parse_icao_coord_token_rejects_short_input() -> None:
    """The standalone helper raises plain ``ValueError`` (not a
    FlightPlanParseError) because it has no position context. Callers in
    the route module wrap it with the structured exception; tests can
    therefore exercise the inner behaviour without faking position state."""
    with pytest.raises(ValueError):
        parse_icao_coord_token("3133N0343E")  # missing one longitude digit


def test_parse_icao_coord_token_rejects_extra_characters() -> None:
    """Anchored regex: trailing garbage means the whole token is wrong."""
    with pytest.raises(ValueError):
        parse_icao_coord_token("3133N03433Ex")


# ---------------------------------------------------------------------------
# Round-trip: formatter ↔ parser preserves a full Route's structure
# ---------------------------------------------------------------------------


def test_round_trip_preserves_real_waypoint_codes() -> None:
    """Build a route of named fixes, format → parse → expect the original
    code sequence back. This is the load contract: any plan saved from
    the app must re-parse into a token list that resolves to the same
    route on the same waypoint database."""
    route = Route()
    route.append_waypoint(_wp("LLBG", 32.0, 34.88))
    route.append_waypoint(_wp("DAROM", 31.55, 34.55))
    route.append_waypoint(_wp("LLHA", 31.72, 35.0))

    plan = to_icao_route_string(route, include_intermediates=True)
    tokens = parse_icao_route_string(plan)

    assert [type(t).__name__ for t in tokens] == [
        "ParsedPlanCode",
        "ParsedPlanCode",
        "ParsedPlanCode",
    ]
    assert [getattr(t, "code", None) for t in tokens] == ["LLBG", "DAROM", "LLHA"]


def test_round_trip_preserves_intermediate_coordinates() -> None:
    """Intermediates have no code — they round-trip as the same 11-char
    coord token (after the formatter's whole-minute rounding). The
    parser yields a ParsedPlanCoord whose ``(lat, lon)`` agrees with the
    formatter's rounded representation to within sub-minute precision."""
    route = Route()
    route.append_waypoint(_wp("LLBG", 32.0, 34.88))
    route.append_intermediate(31.55, 34.55)
    route.append_waypoint(_wp("LLHA", 31.72, 35.0))

    plan = to_icao_route_string(route, include_intermediates=True)
    tokens = parse_icao_route_string(plan)

    assert len(tokens) == 3
    assert isinstance(tokens[0], ParsedPlanCode) and tokens[0].code == "LLBG"
    assert isinstance(tokens[1], ParsedPlanCoord)
    assert isinstance(tokens[2], ParsedPlanCode) and tokens[2].code == "LLHA"
    # The formatter rounds to whole minutes; the parser yields the same
    # rounded coords on the inverse. Tolerance accommodates the round.
    assert math.isclose(tokens[1].lat, 31.55, abs_tol=1.0 / 60.0)
    assert math.isclose(tokens[1].lon, 34.55, abs_tol=1.0 / 60.0)


def test_round_trip_idempotent_on_re_format() -> None:
    """``format(parse(format(route)))`` must equal ``format(route)`` —
    pinning idempotency on the *string* form catches a subtle drift where
    e.g. the parser's minute rounding doesn't match the formatter's
    rounding rule and a saved-then-loaded route serialises slightly
    differently the second time. This is the property that lets a user
    diff-compare saved plans across sessions."""
    route = Route()
    route.append_waypoint(_wp("LLBG", 32.0, 34.88))
    route.append_intermediate(31.553, 34.547)
    route.append_waypoint(_wp("LLHA", 31.72, 35.0))

    first_plan = to_icao_route_string(route, include_intermediates=True)
    # Rebuild a route from the parsed tokens — simulates what the load
    # handler does, minus the waypoint-database lookup (we substitute
    # the original WaypointRecord since the parser only carries codes).
    tokens = parse_icao_route_string(first_plan)
    rebuilt = Route()
    code_to_wp = {
        "LLBG": _wp("LLBG", 32.0, 34.88),
        "LLHA": _wp("LLHA", 31.72, 35.0),
    }
    for tok in tokens:
        if isinstance(tok, ParsedPlanCode):
            rebuilt.append_waypoint(code_to_wp[tok.code])
        else:
            rebuilt.append_intermediate(tok.lat, tok.lon)
    second_plan = to_icao_route_string(rebuilt, include_intermediates=True)

    assert first_plan == second_plan


# ---------------------------------------------------------------------------
# default_save_plan_name: dialog default filename derived from route endpoints
# ---------------------------------------------------------------------------
#
# The Save-plan dialog (``MainWindow._on_save_plan_requested``) used to open
# with a hard-coded ``flight-plan.cvfr`` suggestion. Pilots refer to their
# plans by origin → destination (``LLIB-LLMZ``), and the matching ODS
# paperwork export uses the same convention, so the default name now derives
# from the route's first and last *named* waypoints. The helper lives in
# ``cvfr_routemaster.route`` (alongside the other Route-derived string
# builders) so it's exercisable without spinning up Qt.
#
# Three contract-pinning cases per the roadmap, plus one defensive sanitiser
# case (current Israeli codes are all alnum but a malformed code mustn't
# escape into a filename):
#
#   1. canonical two-named-fix route          → ``LLIB-LLMZ.cvfr``
#   2. all-intermediates / empty route        → ``flight-plan.cvfr``
#   3. single-named-fix route                 → ``LLIB.cvfr``
#   4. returns-to-origin route                → ``LLIB.cvfr`` (collapsed)
#   5. intermediates between named endpoints  → ``LLIB-LLMZ.cvfr`` (skipped)
#   6. filesystem-hostile chars in a code     → fallback (defensive)


def test_default_save_plan_name_canonical_two_named_fixes() -> None:
    """LLIB → LLMZ direct: the canonical Dead Sea reverse-leg shape from
    the May 13 test-drive. Default filename must be ``LLIB-LLMZ.cvfr``."""
    route = Route()
    route.append_waypoint(_wp("LLIB", 32.0, 35.0))
    route.append_waypoint(_wp("LLMZ", 31.1, 35.4))
    assert default_save_plan_name(route) == "LLIB-LLMZ.cvfr"


def test_default_save_plan_name_skips_intermediates_between_named_fixes() -> None:
    """Free-clicked points between the origin and destination must be
    skipped — the user thinks of the file as "LLIB to LLMZ", not as
    "LLIB to 3145N03528E". Walking from each end and picking the first
    named fix is what implements this contract."""
    route = Route()
    route.append_waypoint(_wp("LLIB", 32.0, 35.0))
    route.append_intermediate(31.75, 35.47)
    route.append_intermediate(31.5, 35.45)
    route.append_waypoint(_wp("LLMZ", 31.1, 35.4))
    assert default_save_plan_name(route) == "LLIB-LLMZ.cvfr"


def test_default_save_plan_name_empty_route_falls_back() -> None:
    """An empty route has no endpoints to derive a name from. The dialog
    is already disabled in this state via the Save button's enablement,
    but the helper must still be total — fall back to ``flight-plan.cvfr``
    to match historical behaviour and avoid the caller having to None-check."""
    route = Route()
    assert default_save_plan_name(route) == "flight-plan.cvfr"


def test_default_save_plan_name_all_intermediates_falls_back() -> None:
    """All-intermediates routes are structurally impossible through the
    public mutators (``append_intermediate`` refuses an empty leading
    route), but the helper must still degrade gracefully if a synthesised
    route ever reaches it — coord-token filenames like
    ``3145N03528E-3126N03523E.cvfr`` read poorly to humans, so we'd rather
    fall back to ``flight-plan.cvfr``. Bypass the mutator guard by
    constructing the point list directly."""
    route = Route()
    route._points = [  # noqa: SLF001 — intentional bypass to exercise the all-intermediates branch
        RoutePoint(lat=31.75, lon=35.47, waypoint=None),
        RoutePoint(lat=31.5, lon=35.45, waypoint=None),
    ]
    assert default_save_plan_name(route) == "flight-plan.cvfr"


def test_default_save_plan_name_single_named_fix_uses_bare_code() -> None:
    """A one-point route (only an origin chosen so far) has no destination.
    The helper collapses the would-be ``LLIB-LLIB.cvfr`` into the cleaner
    ``LLIB.cvfr`` rather than repeat the same code on both sides of the
    dash."""
    route = Route()
    route.append_waypoint(_wp("LLIB", 32.0, 35.0))
    assert default_save_plan_name(route) == "LLIB.cvfr"


def test_default_save_plan_name_returns_to_origin_collapses() -> None:
    """A round-trip route (LLIB → … → LLIB) has origin == destination
    after skipping intermediates. Same collapse as the single-named-fix
    case — pilots reading the filename only need to see the one code."""
    route = Route()
    route.append_waypoint(_wp("LLIB", 32.0, 35.0))
    route.append_intermediate(31.5, 35.45)
    route.append_waypoint(_wp("LLIB", 32.0, 35.0))
    assert default_save_plan_name(route) == "LLIB.cvfr"


def test_default_save_plan_name_strips_filesystem_hostile_chars() -> None:
    """Defensive: Israeli waypoint codes are all ASCII alnum so this is
    theoretical, but a synthetic code containing a path separator must
    not leak into the default filename. Sanitiser strips everything
    outside ``[A-Za-z0-9]`` before joining."""
    route = Route()
    route.append_waypoint(_wp("LL/IB", 32.0, 35.0))
    route.append_waypoint(_wp("LL\\MZ", 31.1, 35.4))
    assert default_save_plan_name(route) == "LLIB-LLMZ.cvfr"


def test_default_save_plan_name_degrades_when_sanitised_endpoint_empty() -> None:
    """If a code is *entirely* outside the alnum whitelist, stripping it
    leaves the empty string — joining ``"" + "-" + "LLMZ"`` would yield
    a leading-dash filename that's confusing on listings. Fall back to
    the generic name instead."""
    route = Route()
    route.append_waypoint(_wp("///", 32.0, 35.0))
    route.append_waypoint(_wp("LLMZ", 31.1, 35.4))
    assert default_save_plan_name(route) == "flight-plan.cvfr"
