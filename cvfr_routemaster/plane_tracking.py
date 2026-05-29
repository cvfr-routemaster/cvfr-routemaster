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

"""Pure geometry for the "follow plane" viewport tracker.

When the user clicks a VATSIM plane on the chart, MainWindow stores
its callsign in ``_tracking_callsign`` and re-centres the viewport
on every fresh VATSIM snapshot (every ~15 s) so the plane stays
framed with two-thirds of the viewport *ahead* of it along its
heading and one-third *behind*.

Why "two-thirds ahead"? The user is reading the chart to plan the
next leg of the flight, not to inspect terrain they have already
crossed. Putting more pixels in the heading direction maximises
the useful look-ahead at any given zoom level. The fractions match
the explicit cardinal-case spec the user gave:

- Flying due west: plane sits 1/3 from the right edge,
  centred vertically.
- Flying due south: plane sits 1/3 from the top edge,
  centred horizontally.

The same rule generalises to arbitrary headings via the per-axis
offset formula derived below. We keep the math here, in a Qt-free
pure module, so it is trivially unit-testable without spinning up
``QApplication`` for every assertion.

Heading convention (matches VATSIM ``Pilot.heading_deg`` and
standard aviation): degrees clockwise from true north, so 0 is
north, 90 is east, 180 is south, 270 is west.

Coordinate convention (matches Qt's ``QGraphicsView``): the
viewport's x axis points right, y points *down*. The chart is
north-up, so a heading of 0 (north) maps to a forward unit vector
of ``(0, -1)``.

Derivation
----------

Let ``W`` and ``H`` be the viewport width and height in device
pixels. The viewport centre is at ``(W/2, H/2)``. Decompose the
heading into screen-axis unit components:

    forward_x = sin(heading_rad)
    forward_y = -cos(heading_rad)   # Qt y-down

For each cardinal case the user's spec pins the plane's *viewport*
position exactly:

    heading 0   (N):  (W/2,         2H/3)   -> 1/3 from bottom
    heading 90  (E):  (W/3,         H/2)    -> 1/3 from left
    heading 180 (S):  (W/2,         H/3)    -> 1/3 from top
    heading 270 (W):  (2W/3,        H/2)    -> 1/3 from right

The closed-form that satisfies all four (and is smooth in
between):

    plane_viewport_x = W/2 - forward_x * W/6
    plane_viewport_y = H/2 - forward_y * H/6

For ``W=H`` this puts the plane 1/6 of a viewport edge behind the
centre along the heading direction (so 2/3 ahead, 1/3 behind). For
a non-square viewport each axis scales independently, which keeps
the plane safely inside the viewport for any aspect ratio rather
than letting a steep diagonal heading push it off the wider edge.

To put the plane at that viewport position we have to centre the
view on a scene point *offset* from the plane's scene position by
the inverse of the desired viewport offset (scaled by the view's
device-pixels-per-scene-unit factor ``view_scale``):

    centre_scene = plane_scene + (forward_x * W / 6,
                                  forward_y * H / 6) / view_scale

That single expression is what ``compute_tracking_view_center``
returns, ready to feed directly into
``QGraphicsView.centerOn(...)``.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF


def compute_tracking_view_center(
    plane_scene_pos: QPointF,
    heading_deg: float,
    view_w_px: int,
    view_h_px: int,
    view_scale: float,
) -> QPointF:
    """Scene point to pass to ``QGraphicsView.centerOn``.

    Centring on the returned point puts the plane in the viewport
    such that two-thirds of the visible area lies ahead of it along
    its heading and one-third behind. See module docstring for the
    derivation.

    Parameters
    ----------
    plane_scene_pos:
        Where the plane is rendered, in scene coordinates (the
        same coords ``QGraphicsView.centerOn`` accepts).
    heading_deg:
        Magnetic-like aviation heading in degrees, clockwise from
        north, in ``[0, 360)`` (values outside that range are
        wrapped via the trig identities). 0 -> north, 90 -> east,
        180 -> south, 270 -> west.
    view_w_px, view_h_px:
        Viewport width and height, in device pixels (i.e.
        ``QGraphicsView.viewport().width()`` /
        ``.height()``). Must be non-negative; a zero-size
        viewport short-circuits to the plane's own scene position
        (no offset) since there is no meaningful framing to do.
    view_scale:
        Device pixels per scene unit (the view transform's m11
        component when no rotation/skew is in play, which is the
        case for this app). Used to convert the viewport-pixel
        offset into a scene-unit offset. A non-positive value is
        treated as 1.0 to keep the helper total: callers that
        haven't finished building the transform yet shouldn't
        crash this math.

    Returns
    -------
    QPointF
        The scene coordinate to pass to ``centerOn``. For axis-
        aligned headings the plane lands exactly 1/3 of a viewport
        extent from the appropriate edge.
    """
    if view_w_px <= 0 or view_h_px <= 0:
        return QPointF(plane_scene_pos)

    if view_scale <= 0:
        view_scale = 1.0

    heading_rad = math.radians(heading_deg)
    forward_x = math.sin(heading_rad)
    forward_y = -math.cos(heading_rad)

    offset_scene_x = forward_x * view_w_px / 6.0 / view_scale
    offset_scene_y = forward_y * view_h_px / 6.0 / view_scale

    return QPointF(
        plane_scene_pos.x() + offset_scene_x,
        plane_scene_pos.y() + offset_scene_y,
    )
