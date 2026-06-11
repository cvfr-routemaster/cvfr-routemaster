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

"""Unit tests for :mod:`cvfr_routemaster`'s version/title helpers.

The version string is the single source of truth that the window
title, splash screen, progress dialogs, and the Copyright
Information dialog all read from. If these helpers regress, every
window in the app starts lying about which version the user is
running — exactly the failure mode the build cookbook's "step 0"
checklist is designed to catch. These tests pin the formatting
contract so a future ``__version__`` bump can't silently break
the display string format.
"""

from __future__ import annotations

import re

import pytest

from cvfr_routemaster import APP_NAME, __version__, app_title, display_version


def test_version_string_is_well_formed_semver_like() -> None:
    """``__version__`` is expected to follow ``MAJOR.MINOR[.PATCH]``
    so ``display_version()``'s trailing-zero trim has a stable
    input. Validate the shape so an accidental ``"v3.3"`` or
    ``"3.3-dev"`` (which would break PyPI-style packaging tooling
    and confuse the trim logic) fails here, not when the user
    opens the title bar."""
    assert re.fullmatch(r"\d+\.\d+(?:\.\d+)?", __version__), (
        f"__version__={__version__!r} does not match MAJOR.MINOR[.PATCH]"
    )


def test_app_name_is_unchanged_literal() -> None:
    """``APP_NAME`` is the QSettings identity (see
    ``settings_store.APP``) and also the visible product name.
    Changing it would orphan every existing user's preferences
    AND mismatch the marketing/window-title brand string.
    Pin the literal so the breakage is visible at test time."""
    assert APP_NAME == "CVFR Route Master"


# ---------------------------------------------------------------------------
# display_version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4.0.0", "4.0"),
        ("3.3.0", "3.3"),
        ("3.3.1", "3.3.1"),
        ("3.0.0", "3.0"),
        ("4.0.5", "4.0.5"),
        ("1.2", "1.2"),
        ("10.0.0", "10.0"),
        ("0.1.0", "0.1"),
    ],
)
def test_display_version_trims_trailing_zero_segments(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    """``display_version()`` drops a redundant trailing ``.0`` *patch*
    segment (so a clean release like ``4.0.0`` reads as ``v4.0``) but
    always keeps the ``MAJOR.MINOR`` pair (so ``4.0.0`` is ``4.0``, not
    ``4``) and every non-zero segment intact (``3.3.1`` stays
    ``v3.3.1``). Parametrized so a refactor that breaks one branch
    can't pass under a luckier input."""
    import cvfr_routemaster as pkg

    monkeypatch.setattr(pkg, "__version__", raw)
    assert pkg.display_version() == expected


def test_display_version_for_current_version_is_four_one() -> None:
    """Concrete sanity check on the actual shipped version. v4.1
    is what the build cookbook says we're shipping; if a future
    bump moves the version again, update this test, the cookbook
    entry, and the Copyright Information dialog together."""
    assert __version__ == "4.1.0"
    assert display_version() == "4.1"


# ---------------------------------------------------------------------------
# app_title
# ---------------------------------------------------------------------------


def test_app_title_without_prefix_uses_brand_then_version_suffix() -> None:
    """The bare-window title format is ``<APP_NAME> (v<version>)``
    with the version suffix non-optional — this is what the build
    cookbook step 0 validates against on every release."""
    assert app_title() == "CVFR Route Master (v4.1)"


def test_app_title_with_prefix_uses_em_dash_separator() -> None:
    """Progress dialogs prepend a context word (``Loading``,
    ``Waypoints``) and join with an em-dash. Pin the separator so
    a code-review LGTM that swaps it for a hyphen or colon doesn't
    silently land a UX regression — window-manager taskbar
    grouping rules and screenshots both key on the exact pattern."""
    assert app_title("Loading") == "Loading \u2014 CVFR Route Master (v4.1)"
    assert app_title("Waypoints") == "Waypoints \u2014 CVFR Route Master (v4.1)"


def test_app_title_empty_string_prefix_is_treated_as_no_prefix() -> None:
    """``app_title("")`` (rather than ``app_title()``) is the kind
    of call a refactor might emit if a caller's prefix variable
    is uninitialised. It should NOT render as ``" — CVFR Route
    Master (v4.0)"`` with a stray leading em-dash; treat empty
    string like the missing-arg case."""
    assert app_title("") == app_title()


def test_app_title_tracks_version_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The title must update automatically when ``__version__``
    moves. This is the contract that makes the build cookbook's
    "bump __version__ in one place" promise true — no other site
    should need editing for the title bar to reflect the new
    version."""
    import cvfr_routemaster as pkg

    monkeypatch.setattr(pkg, "__version__", "4.0.0")
    assert pkg.app_title() == "CVFR Route Master (v4.0)"
    assert pkg.app_title("Loading") == "Loading \u2014 CVFR Route Master (v4.0)"

    monkeypatch.setattr(pkg, "__version__", "3.3.1")
    assert pkg.app_title() == "CVFR Route Master (v3.3.1)"


def test_app_title_includes_app_name_literal() -> None:
    """Defence in depth — the title must contain ``APP_NAME``
    exactly. Any refactor that swaps the display string (e.g.
    abbreviating to ``CVFR-RM``) must update ``APP_NAME`` first,
    not change ``app_title()``'s formatting in isolation."""
    assert APP_NAME in app_title()
    assert APP_NAME in app_title("Anything")
