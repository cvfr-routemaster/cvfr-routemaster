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

"""Unit tests for :class:`WaypointMarkerOverlay`."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
)

if QCoreApplication.instance() is None:
    _APP = QApplication.instance() or QApplication(sys.argv[:1])

import pytest  # noqa: E402

from cvfr_routemaster.geo_calibration import (  # noqa: E402
    CalibrationPoint,
    SheetGeoCalibration,
)
from cvfr_routemaster.satellite_overlay import (  # noqa: E402
    ChartSeamPartition,
)
from cvfr_routemaster.waypoint_marker_overlay import (  # noqa: E402
    AIRPORT_BORDER_INNER,
    AIRPORT_BORDER_MID,
    AIRPORT_BORDER_OUTER,
    AIRPORT_MARKER_Z,
    DEFAULT_TRIANGLE_SIDE_PX,
    LABEL_BACKGROUND,
    LABEL_TEXT_COLOR,
    MANDATORY_FILL,
    TRIANGLE_BORDER_INNER,
    TRIANGLE_BORDER_MID,
    TRIANGLE_BORDER_OUTER,
    WAYPOINT_MARKER_Z,
    WaypointMarkerOverlay,
    _classify_reporting_type,
    _is_airport,
    _label_text_for,
    _triangle_polygon,
    _WaypointMarkerItem,
)
from cvfr_routemaster.waypoint_styles import (  # noqa: E402
    HE_MANDATORY,
    HE_ON_DEMAND,
)
from cvfr_routemaster.waypoint_types import WaypointRecord  # noqa: E402


def _make_calibration() -> SheetGeoCalibration:
    """Build a minimal 4-point calibration roughly covering Israel.

    The four corners of a unit square in (u, v) ↔ (0..1, 0..1) map
    to a recognizable bbox:

    * (u=0, v=0)  → (lon=34.0, lat=33.5) (north-west)
    * (u=1, v=0)  → (lon=36.0, lat=33.5) (north-east)
    * (u=0, v=1)  → (lon=34.0, lat=29.5) (south-west)
    * (u=1, v=1)  → (lon=36.0, lat=29.5) (south-east)

    The exact LSQ fit doesn't matter beyond "the affine round-trip
    stays sane"; tests only depend on a waypoint at, say,
    ``lon=35, lat=32`` projecting somewhere inside the unit square.
    """
    pts = [
        CalibrationPoint(code="NW", u=0.0, v=0.0, lat=33.5, lon=34.0),
        CalibrationPoint(code="NE", u=1.0, v=0.0, lat=33.5, lon=36.0),
        CalibrationPoint(code="SW", u=0.0, v=1.0, lat=29.5, lon=34.0),
        CalibrationPoint(code="SE", u=1.0, v=1.0, lat=29.5, lon=36.0),
    ]
    return SheetGeoCalibration(pdf_fp={}, points=pts)


def _make_north_calibration() -> SheetGeoCalibration:
    """North sheet covering lat 31.0..33.5 — overlaps the south sheet
    between lat 31.0 and 31.5. Paired with :func:`_make_south_calibration`
    to exercise the UV-distance peer partition in
    :class:`WaypointMarkerOverlay`."""
    pts = [
        CalibrationPoint(code="NW", u=0.0, v=0.0, lat=33.5, lon=34.0),
        CalibrationPoint(code="NE", u=1.0, v=0.0, lat=33.5, lon=36.0),
        CalibrationPoint(code="SW", u=0.0, v=1.0, lat=31.0, lon=34.0),
        CalibrationPoint(code="SE", u=1.0, v=1.0, lat=31.0, lon=36.0),
    ]
    return SheetGeoCalibration(pdf_fp={}, points=pts)


def _make_south_calibration() -> SheetGeoCalibration:
    """South sheet covering lat 29.5..31.5 — overlaps the north sheet
    between lat 31.0 and 31.5. UV centre lat is 30.5, so a waypoint
    sitting inside the overlap (e.g. lat 31.25) lands closer to centre
    in this sheet's UV space than in the north sheet's, which is what
    the partition test asserts."""
    pts = [
        CalibrationPoint(code="NW", u=0.0, v=0.0, lat=31.5, lon=34.0),
        CalibrationPoint(code="NE", u=1.0, v=0.0, lat=31.5, lon=36.0),
        CalibrationPoint(code="SW", u=0.0, v=1.0, lat=29.5, lon=34.0),
        CalibrationPoint(code="SE", u=1.0, v=1.0, lat=29.5, lon=36.0),
    ]
    return SheetGeoCalibration(pdf_fp={}, points=pts)


def _wp(
    *,
    code: str = "X",
    name_he: str = "",
    reporting_type: str = HE_MANDATORY,
    lat: float = 32.0,
    lon: float = 35.0,
    index: int = 0,
) -> WaypointRecord:
    """Convenience factory; sets the same lat/lon for everything
    that doesn't pass an override (well inside the calibration's
    bbox)."""
    return WaypointRecord(
        index=index,
        code=code,
        name_he=name_he,
        reporting_type=reporting_type,
        lat=lat,
        lon=lon,
        lat_dms="",
        lon_dms="",
    )


@pytest.fixture
def chart_setup() -> tuple[
    QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
]:
    """Fresh scene + chart pixmap item; size is 1000 × 800 px."""
    scene = QGraphicsScene()
    size = (1000, 800)
    pix = QPixmap(*size)
    pix.fill()
    item = QGraphicsPixmapItem(pix)
    scene.addItem(item)
    return scene, item, size


# ---------------------------------------------------------------------------
# Triangle polygon
# ---------------------------------------------------------------------------


class TestTrianglePolygon:
    def test_three_vertices(self) -> None:
        poly = _triangle_polygon(100.0, 100.0, 20.0)
        assert len(poly) == 3

    def test_apex_above_base(self) -> None:
        """Apex points UP (matches the printed chart) — in Qt's
        y-down scene coords that means the apex has a smaller y
        than the base vertices."""
        poly = _triangle_polygon(0.0, 0.0, 20.0)
        ys = [poly[i].y() for i in range(3)]
        sorted_ys = sorted(ys)
        assert sorted_ys[1] == sorted_ys[2]  # base vertices share y
        assert sorted_ys[0] < sorted_ys[1]   # apex above base

    def test_apex_below_base_when_apex_down(self) -> None:
        """Apex points DOWN for airport markers — in Qt's y-down
        scene coords that means the apex has a *larger* y than
        the base vertices (the base is at the top of the marker)."""
        poly = _triangle_polygon(0.0, 0.0, 20.0, apex_down=True)
        ys = [poly[i].y() for i in range(3)]
        sorted_ys = sorted(ys)
        assert sorted_ys[0] == sorted_ys[1]  # base vertices share y
        assert sorted_ys[1] < sorted_ys[2]   # apex below base

    def test_apex_down_is_horizontal_mirror_of_apex_up(self) -> None:
        """Flipping ``apex_down`` should mirror the y coordinates
        around the centroid; x coordinates are unchanged. Pinning
        this guarantees the centroid stays at ``(cx, cy)`` in
        either orientation, which the overlay relies on when
        stacking two markers at the same chart-pixel position
        (e.g. Massada / LLMZ → airport + mandatory)."""
        up = _triangle_polygon(0.0, 0.0, 20.0)
        down = _triangle_polygon(0.0, 0.0, 20.0, apex_down=True)
        up_pts = sorted([(up[i].x(), up[i].y()) for i in range(3)])
        down_pts = sorted([(down[i].x(), -down[i].y()) for i in range(3)])
        # Same x-sorted ordering plus flipped y should match.
        assert up_pts == pytest.approx(down_pts)

    def test_centre_is_geometric_centroid(self) -> None:
        """The centre we pass in should be the centroid of the
        three vertices (so the marker rendering position is
        unambiguous)."""
        poly = _triangle_polygon(50.0, 60.0, 20.0)
        cx = sum(poly[i].x() for i in range(3)) / 3.0
        cy = sum(poly[i].y() for i in range(3)) / 3.0
        assert abs(cx - 50.0) < 1e-6
        assert abs(cy - 60.0) < 1e-6

    def test_centre_is_centroid_when_apex_down_too(self) -> None:
        """Same invariant as the apex-up centroid test, but for the
        inverted triangle. Two stacked markers sharing the same
        chart position MUST have the same centroid; otherwise the
        Massada double-marker would visibly offset."""
        poly = _triangle_polygon(50.0, 60.0, 20.0, apex_down=True)
        cx = sum(poly[i].x() for i in range(3)) / 3.0
        cy = sum(poly[i].y() for i in range(3)) / 3.0
        assert abs(cx - 50.0) < 1e-6
        assert abs(cy - 60.0) < 1e-6


# ---------------------------------------------------------------------------
# Reporting-type classification helpers
# ---------------------------------------------------------------------------


class TestClassifyReportingType:
    def test_mandatory(self) -> None:
        assert _classify_reporting_type(HE_MANDATORY) == "mandatory"
        # Surrounding whitespace tolerated.
        assert _classify_reporting_type(f"  {HE_MANDATORY}  ") == "mandatory"

    def test_on_demand(self) -> None:
        assert _classify_reporting_type(HE_ON_DEMAND) == "on_demand"

    def test_arp_case_insensitive(self) -> None:
        for v in ("ARP", "arp", "Arp", " arp "):
            assert _classify_reporting_type(v) == "arp"

    def test_unknown_falls_back(self) -> None:
        assert _classify_reporting_type("") == "unknown"
        assert _classify_reporting_type("???") == "unknown"
        assert _classify_reporting_type("OBSERVATION") == "unknown"


class TestIsAirport:
    """``_is_airport`` decides whether a waypoint gets the blue
    inverted-triangle marker. Two rules OR-ed together: the OCR's
    explicit ``ARP`` classification, or the Israeli ICAO airport
    code pattern ``^LL[A-Z]{2}$`` (which catches Massada / LLMZ,
    the canonical "classified as mandatory but really an
    airfield" case).
    """

    def test_arp_reporting_type_qualifies(self) -> None:
        # All three ARP spellings the OCR emits.
        for rt in ("ARP", "arp", " Arp ", "  ARP "):
            assert _is_airport(_wp(code="LLBG", reporting_type=rt))

    def test_icao_israeli_airport_code_qualifies(self) -> None:
        """LL[A-Z]{2} catches Massada (LLMZ) which is OCR-classified
        as a mandatory reporting point because the chart lists it
        primarily as a transit waypoint. The user expects an airport
        marker there too."""
        massada = _wp(
            code="LLMZ", name_he="מצדה",
            reporting_type=HE_MANDATORY, lat=31.33, lon=35.39,
        )
        assert _is_airport(massada)

    def test_non_ll_code_with_non_arp_reporting_type_does_not_qualify(
        self,
    ) -> None:
        # Plain reporting points that aren't ICAO airports.
        assert not _is_airport(_wp(code="ABC", reporting_type=HE_MANDATORY))
        assert not _is_airport(_wp(code="VRP1", reporting_type=HE_ON_DEMAND))
        assert not _is_airport(_wp(code="", reporting_type=HE_MANDATORY))

    def test_arp_with_non_ll_code_still_qualifies(self) -> None:
        # KKDEM and GVULT are real ARP-classified airstrips with
        # non-ICAO codes; both must qualify.
        assert _is_airport(_wp(code="KKDEM", reporting_type="ARP"))
        assert _is_airport(_wp(code="GVULT", reporting_type="ARP"))

    def test_long_ll_prefixed_code_does_not_qualify(self) -> None:
        """``LL[A-Z]{2}`` is *exactly* 4 characters; a longer
        ``LLDEMO``-style code must NOT qualify, because the LL
        prefix outside the strict ICAO pattern isn't a reliable
        airport signal."""
        assert not _is_airport(_wp(code="LLDEMO", reporting_type=HE_MANDATORY))
        assert not _is_airport(_wp(code="LL", reporting_type=HE_MANDATORY))
        assert not _is_airport(_wp(code="LLB", reporting_type=HE_MANDATORY))

    def test_lower_case_icao_code_qualifies(self) -> None:
        # Defensive — OCR could in principle emit lower-case.
        assert _is_airport(_wp(code="llmz", reporting_type=HE_MANDATORY))


class TestLabelText:
    def test_prefers_hebrew_name(self) -> None:
        assert _label_text_for(_wp(code="A", name_he="אלף")) == "אלף"

    def test_falls_back_to_code(self) -> None:
        assert _label_text_for(_wp(code="A", name_he="")) == "A"
        assert _label_text_for(_wp(code="B", name_he="   ")) == "B"

    def test_blank_when_both_blank(self) -> None:
        assert _label_text_for(_wp(code="", name_he="")) == ""


# ---------------------------------------------------------------------------
# Overlay construction
# ---------------------------------------------------------------------------


class TestOverlayBuild:
    def test_creates_marker_per_in_sheet_waypoint(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(code="A", name_he="אלף",
                reporting_type=HE_MANDATORY, lat=32.0, lon=35.0,
                index=0),
            _wp(code="B", name_he="בית",
                reporting_type=HE_ON_DEMAND, lat=31.5, lon=34.5,
                index=1),
            # Outside the calibration's bbox → projects outside
            # the chart-pixel rect and gets skipped.
            _wp(code="Z", name_he="זי",
                reporting_type=HE_MANDATORY, lat=10.0, lon=10.0,
                index=2),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        assert ov.marker_count() == 2
        ov.teardown()

    def test_arp_waypoint_gets_airport_marker(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """ARP-classified records (canonically the user-visible
        airports like LLBG, LLHZ, ...) get one marker each — the
        blue inverted-triangle airport marker. They USED to be
        skipped because airport symbology was baked into the
        printed chart; under satellite mode that pixmap is
        covered, so they need their own marker. A pure ARP-only
        record produces exactly one marker (the airport).
        """
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(code="LLHZ", name_he="הרצליה",
                reporting_type="ARP", lat=32.0, lon=34.83),
            _wp(code="A", name_he="אלף",
                reporting_type=HE_MANDATORY, lat=32.0, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        # One airport marker for LLHZ + one mandatory marker for
        # "A" = 2 total.
        assert ov.marker_count() == 2
        kinds = sorted(it.kind for it in ov.marker_items())
        assert kinds == ["airport", "mandatory"]
        ov.teardown()

    def test_dual_purpose_airfield_gets_two_stacked_markers(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """Massada (LLMZ) is both an airfield AND a mandatory
        reporting point (it's used as a transit waypoint). It
        appears in the OCR with ``reporting_type=חובה`` but the
        Israeli ICAO code pattern qualifies it for the airport
        marker too. The overlay must emit BOTH markers — airport
        blue triangle underneath, mandatory yellow triangle on
        top — both stacked at the same chart-pixel position.
        """
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(code="LLMZ", name_he="מצדה",
                reporting_type=HE_MANDATORY, lat=31.33, lon=35.39),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        assert ov.marker_count() == 2
        # One of each kind, no duplicates.
        kinds = sorted(it.kind for it in ov.marker_items())
        assert kinds == ["airport", "mandatory"]
        # The two markers must share the same setPos/chart position
        # so they visually stack rather than offset. We check via
        # scene position: after _apply_chart_transform, both items
        # share the same pos() (the chart-pixmap item's
        # sceneTransform is identity at construction).
        positions = {it.pos().toTuple() for it in ov.marker_items()}
        assert len(positions) == 1
        ov.teardown()

    def test_airport_marker_sits_below_reporting_marker_in_z(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """For the Massada double-marker case the airport marker
        must paint UNDER the mandatory marker — i.e. the airport
        z-value must be strictly less than the mandatory z-value.
        Pins the stacking order the user described ("[reporting
        point] on top of the airport marker")."""
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(code="LLMZ", name_he="מצדה",
                reporting_type=HE_MANDATORY, lat=31.33, lon=35.39),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        zs = {it.kind: it.zValue() for it in ov.marker_items()}
        assert zs["airport"] < zs["mandatory"]
        # And the absolute z-values match the module constants so
        # a future tweak to the marker-z layout shows up loudly.
        assert zs["airport"] == AIRPORT_MARKER_Z
        assert zs["mandatory"] == WAYPOINT_MARKER_Z
        ov.teardown()

    def test_zero_size_pixmap_yields_no_markers(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        scene, chart_item, _size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(code="A", reporting_type=HE_MANDATORY,
                lat=32.0, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=(0, 0),
            waypoints=waypoints,
        )
        assert ov.marker_count() == 0
        ov.teardown()

    def test_items_added_to_chart_scene(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        # Markers live as top-level scene items (not children of
        # the chart pixmap) so they can paint above either chart
        # sheet — same rationale as satellite tiles, see
        # :class:`cvfr_routemaster.satellite_overlay.SatelliteOverlay`'s
        # class docstring.
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(reporting_type=HE_MANDATORY,
                lat=32.0, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        for it in ov.marker_items():
            assert it.parentItem() is None
            assert it.scene() is scene
        ov.teardown()

    def test_marker_z_value(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """Marker z sits above satellite tile z and the chart's
        bare pixmap, below traffic / route overlays."""
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(reporting_type=HE_MANDATORY,
                lat=32.0, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        marker = ov.marker_items()[0]
        assert marker.zValue() == WAYPOINT_MARKER_Z
        ov.teardown()


# ---------------------------------------------------------------------------
# Fixed-screen-size behaviour
# ---------------------------------------------------------------------------


class TestFixedScreenSize:
    def test_marker_has_ignore_transformations_flag(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """The whole point of the refactor — without this flag the
        triangles would shrink to nothing at full zoom-out, which
        is exactly what the user complained about."""
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(reporting_type=HE_MANDATORY, lat=32.0, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        marker = ov.marker_items()[0]
        flags = marker.flags()
        assert (
            flags
            & QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
        )
        ov.teardown()

    def test_position_uses_chart_pixel_coords(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """The marker's setPos coords are in *chart-pixel* units
        (parent is the chart pixmap). The local geometry inside
        the item is in screen pixels via the ignore-transforms
        flag, but the anchor point still tracks the chart."""
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        # Pick a known waypoint roughly at the chart centre.
        wp = _wp(reporting_type=HE_MANDATORY,
                 lat=31.5, lon=35.0)
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=[wp],
        )
        marker = ov.marker_items()[0]
        u, v = cal.lonlat_to_uv(35.0, 31.5)
        expected_x = u * size[0]
        expected_y = v * size[1]
        # Position is in parent (chart-pixel) coords. Verify it
        # lands where the calibration projection said.
        assert abs(marker.pos().x() - expected_x) < 1e-3
        assert abs(marker.pos().y() - expected_y) < 1e-3
        ov.teardown()


# ---------------------------------------------------------------------------
# Triangle styling per reporting type
# ---------------------------------------------------------------------------


class TestMarkerKindClassification:
    def test_mandatory_kind_set(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(reporting_type=HE_MANDATORY,
                lat=32.0, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        marker = ov.marker_items()[0]
        assert isinstance(marker, _WaypointMarkerItem)
        # Kind drives the paint() fill behaviour: "mandatory"
        # paints a solid black triangle, others don't.
        assert marker.kind == "mandatory"
        ov.teardown()

    def test_on_demand_kind_set(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(reporting_type=HE_ON_DEMAND,
                lat=32.0, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        marker = ov.marker_items()[0]
        assert marker.kind == "on_demand"
        ov.teardown()

    def test_unknown_reporting_type_classified_as_unknown(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """Defensive: weird OCR output should fall back to the
        less-emphatic outline style rather than the mandatory
        filled style — same paint behaviour as on-demand."""
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(reporting_type="???", lat=32.0, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        marker = ov.marker_items()[0]
        assert marker.kind == "unknown"
        ov.teardown()

    def test_airport_kind_set_for_arp_record(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """An ARP-classified record produces exactly one marker
        with ``kind == "airport"`` — the blue inverted triangle
        with label-above geometry. This is the path for the
        majority of airports (LLBG, LLHZ, LLHA, ...) which have
        no reporting-point classification."""
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(code="LLHZ", name_he="הרצליה",
                reporting_type="ARP", lat=32.18, lon=34.83),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        markers = ov.marker_items()
        assert len(markers) == 1
        assert markers[0].kind == "airport"
        ov.teardown()


class TestAirportMarkerGeometry:
    """The airport-marker variant inverts the triangle (apex-down)
    and flips the label to sit *above* the triangle. These tests
    pin the geometric invariants so a future refactor that
    accidentally drops the apex-direction or label-position
    handling shows up here rather than as a misplaced label in
    production."""

    def test_label_sits_above_triangle_centroid(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """The label backing rect for an airport marker must sit
        ABOVE the centroid (negative y in local coords) — opposite
        the existing reporting-point convention where the label
        sits below."""
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(code="LLHZ", name_he="הרצליה",
                reporting_type="ARP", lat=32.18, lon=34.83),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        marker = ov.marker_items()[0]
        # The label backing rect should be entirely above the
        # centroid (y < 0 in local-coord space).
        bg = marker.label_bg_rect
        assert bg.bottom() < 0.0, (
            f"airport-marker label backing rect {bg=} should sit "
            f"above the centroid (y < 0); got bottom={bg.bottom()}"
        )

    def test_reporting_point_label_still_sits_below_triangle_centroid(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """Regression guard: the existing reporting-point label
        convention (below the triangle) must NOT have changed when
        we added the airport variant."""
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(code="X", name_he="אלף",
                reporting_type=HE_MANDATORY, lat=32.0, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        marker = ov.marker_items()[0]
        bg = marker.label_bg_rect
        assert bg.top() > 0.0, (
            f"reporting-point label backing rect {bg=} should sit "
            f"below the centroid (y > 0); got top={bg.top()}"
        )


# ---------------------------------------------------------------------------
# Border-colour constants — yellow-black-yellow visibility aid
# ---------------------------------------------------------------------------


class TestBorderColours:
    def test_outer_and_inner_are_black(self) -> None:
        """Outer and inner rings are black so the marker has high
        contrast against the chart-yellow fill (mandatory
        reporting points) and against any imagery on the outside.
        Black is uniformly high-contrast against sand, sea,
        forest, and urban backgrounds — the property we need most
        from the outermost ring."""
        for c in (TRIANGLE_BORDER_OUTER, TRIANGLE_BORDER_INNER):
            assert c.alpha() == 255
            assert c.red() == 0
            assert c.green() == 0
            assert c.blue() == 0

    def test_middle_is_yellow(self) -> None:
        """The middle ring is yellow — sandwiched between two
        blacks gives the marker its iconic halo and matches the
        chart's printed yellow-on-black triangle convention so
        chart mode and satellite mode read as the same symbol."""
        # Hue check: red and green roughly equal, blue near zero
        # — i.e. the saturated-yellow region of the gamut.
        c = TRIANGLE_BORDER_MID
        assert c.alpha() == 255
        assert c.red() >= 200
        assert c.green() >= 150
        assert c.blue() <= 60

    def test_airport_outer_and_inner_are_black(self) -> None:
        """Same outer/inner black convention as reporting-point
        markers — black is the load-bearing high-contrast layer
        against any background."""
        for c in (AIRPORT_BORDER_OUTER, AIRPORT_BORDER_INNER):
            assert c.alpha() == 255
            assert c.red() == 0
            assert c.green() == 0
            assert c.blue() == 0

    def test_airport_middle_is_blue(self) -> None:
        """The airport-marker middle ring is BLUE (not yellow). The
        halo colour is the load-bearing differentiator between
        airport and reporting-point markers — the shape is
        otherwise identical (just inverted)."""
        c = AIRPORT_BORDER_MID
        assert c.alpha() == 255
        # Saturated blue: blue channel dominant, red/green low-ish.
        assert c.blue() >= 200
        assert c.red() <= 120
        assert c.green() <= 180
        # And blue must clearly beat red so it doesn't read as
        # purple under typical display gamut. Anchor against a
        # margin so a small tweak to the exact RGB stays safe.
        assert c.blue() > c.red() + 80


# ---------------------------------------------------------------------------
# Label content
# ---------------------------------------------------------------------------


class TestLabelContent:
    def test_label_uses_hebrew_name(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """User explicitly requested Hebrew names; verify the
        label string is the ``name_he`` field rather than the
        Latin code."""
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        hebrew_name = "תל אביב"
        waypoints = [
            _wp(code="LLSD", name_he=hebrew_name,
                reporting_type=HE_MANDATORY,
                lat=32.0, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        marker = ov.marker_items()[0]
        assert marker.label_text == hebrew_name
        ov.teardown()

    def test_label_falls_back_to_code(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(code="ALPHA", name_he="",
                reporting_type=HE_MANDATORY,
                lat=32.0, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        marker = ov.marker_items()[0]
        assert marker.label_text == "ALPHA"
        ov.teardown()


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


class TestVisibility:
    def test_starts_hidden(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(reporting_type=HE_MANDATORY,
                lat=32.0, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        assert not ov.is_visible()
        for it in ov.marker_items():
            assert not it.isVisible()
        ov.teardown()

    def test_set_visible_propagates_to_all_items(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(code="A", reporting_type=HE_MANDATORY,
                lat=32.0, lon=35.0),
            _wp(code="B", reporting_type=HE_ON_DEMAND,
                lat=31.5, lon=34.5),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        ov.set_visible(True)
        assert ov.is_visible()
        for it in ov.marker_items():
            assert it.isVisible()
        ov.set_visible(False)
        assert not ov.is_visible()
        for it in ov.marker_items():
            assert not it.isVisible()
        ov.teardown()


# ---------------------------------------------------------------------------
# Click semantics
# ---------------------------------------------------------------------------


class TestClickPassthrough:
    def test_marker_does_not_accept_mouse_buttons(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """Markers must let mouse events fall through to the
        underlying chart item / view, otherwise shift-add /
        shift-remove routing breaks when the user clicks on a
        marker."""
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(code="A", reporting_type=HE_MANDATORY,
                lat=32.0, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        for it in ov.marker_items():
            assert it.acceptedMouseButtons() == Qt.MouseButton.NoButton
        ov.teardown()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestTeardown:
    def test_teardown_removes_all_items(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(code="A", reporting_type=HE_MANDATORY,
                lat=32.0, lon=35.0),
            _wp(code="B", reporting_type=HE_ON_DEMAND,
                lat=31.5, lon=34.5),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        all_items = ov.marker_items()
        assert len(all_items) > 0
        ov.teardown()
        assert ov.marker_count() == 0
        for item in all_items:
            assert item.scene() is None
            assert item.parentItem() is None

    def test_teardown_is_idempotent(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=[],
        )
        ov.teardown()
        ov.teardown()
        assert ov.marker_count() == 0


# ---------------------------------------------------------------------------
# Default constants
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ChartSeamPartition (overlap-strip dedup driven by chart pixmap seam)
# ---------------------------------------------------------------------------


class TestChartSeamPartition:
    """The chart-seam partition replaced the older UV-distance heuristic.
    A waypoint inside the overlap is owned by whichever sheet's territory
    contains its scene-Y under *north's* calibration: items above the seam
    belong to north; items at or below belong to south. The two overlays
    therefore reach symmetric, exclusive decisions for every lat/lon so the
    rendered marker count is exactly one in the overlap and the sheet-local
    behaviour is unchanged outside the overlap."""

    def test_no_partition_passes_every_projectable_waypoint(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """Backwards-compatible: ``chart_seam_partition=None`` is the default
        and the constructor must behave exactly as it did before the
        partition was added."""
        scene, chart_item, size = chart_setup
        cal = _make_calibration()
        waypoints = [
            _wp(code="A", reporting_type=HE_MANDATORY, lat=31.25, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=cal,
            pixmap_size=size,
            waypoints=waypoints,
        )
        assert ov.marker_count() == 1
        ov.teardown()

    def test_overlap_waypoint_below_seam_dropped_by_north_overlay(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """North-sheet overlay: a waypoint that projects below the seam
        under north's calibration belongs to south, so the north overlay
        must skip it. Seam is placed at scene_y == 0 (top of north's
        pixmap) so every projectable waypoint falls below it under
        north's calibration."""
        scene, chart_item, size = chart_setup
        north_cal = _make_north_calibration()
        H_n = float(size[1])
        partition = ChartSeamPartition(
            north_calibration=north_cal,
            north_pixmap_height=H_n,
            chart_seam_scene_y=0.0,
            self_is_north=True,
        )
        waypoints = [
            _wp(code="OL", reporting_type=HE_MANDATORY, lat=31.25, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=north_cal,
            pixmap_size=size,
            waypoints=waypoints,
            chart_seam_partition=partition,
        )
        assert ov.marker_count() == 0
        ov.teardown()

    def test_overlap_waypoint_below_seam_kept_by_south_overlay(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """South-sheet overlay: same waypoint that the north overlay
        dropped (because it projects below the seam) is exactly what
        the south overlay must keep."""
        scene, chart_item, size = chart_setup
        north_cal = _make_north_calibration()
        south_cal = _make_south_calibration()
        H_n = float(size[1])
        partition = ChartSeamPartition(
            north_calibration=north_cal,
            north_pixmap_height=H_n,
            chart_seam_scene_y=0.0,
            self_is_north=False,
        )
        waypoints = [
            _wp(code="OL", reporting_type=HE_MANDATORY, lat=31.25, lon=35.0),
        ]
        ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=south_cal,
            pixmap_size=size,
            waypoints=waypoints,
            chart_seam_partition=partition,
        )
        assert ov.marker_count() == 1
        ov.teardown()

    def test_two_overlays_together_render_overlap_waypoint_exactly_once(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """The user-facing invariant: paint both per-sheet overlays for the
        same waypoint and the sum of their marker counts is exactly one. No
        doubling, no dropping. The chart-seam partition is exclusive (unlike
        the older UV-distance heuristic which could double-fire on ties), so
        this holds at every lat/lon."""
        scene, chart_item, size = chart_setup
        north_cal = _make_north_calibration()
        south_cal = _make_south_calibration()
        H_n = float(size[1])
        # Seam at the south-sheet's natural overlap edge under north's
        # calibration — lat 31.0 is north's bottom edge.
        _u_seam, v_seam = north_cal.lonlat_to_uv(35.0, 31.0)
        seam_y = v_seam * H_n
        north_partition = ChartSeamPartition(
            north_calibration=north_cal,
            north_pixmap_height=H_n,
            chart_seam_scene_y=seam_y,
            self_is_north=True,
        )
        south_partition = ChartSeamPartition(
            north_calibration=north_cal,
            north_pixmap_height=H_n,
            chart_seam_scene_y=seam_y,
            self_is_north=False,
        )
        wp = _wp(
            code="OL", reporting_type=HE_MANDATORY, lat=31.25, lon=35.0
        )
        north_ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=north_cal,
            pixmap_size=size,
            waypoints=[wp],
            chart_seam_partition=north_partition,
        )
        south_ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=south_cal,
            pixmap_size=size,
            waypoints=[wp],
            chart_seam_partition=south_partition,
        )
        assert north_ov.marker_count() + south_ov.marker_count() == 1
        north_ov.teardown()
        south_ov.teardown()

    def test_sheet_local_waypoint_unaffected_by_partition(
        self,
        chart_setup: tuple[
            QGraphicsScene, QGraphicsPixmapItem, tuple[int, int]
        ],
    ) -> None:
        """Waypoint at lat 32.5 sits well inside the north sheet (above
        any reasonable seam under north's calibration), so the north
        overlay must still render it regardless of partition state."""
        scene, chart_item, size = chart_setup
        north_cal = _make_north_calibration()
        H_n = float(size[1])
        # Seam at the bottom of north's chart — only true overlap-strip
        # waypoints (lat near 31.0) would fall below it.
        partition = ChartSeamPartition(
            north_calibration=north_cal,
            north_pixmap_height=H_n,
            chart_seam_scene_y=H_n,
            self_is_north=True,
        )
        wp = _wp(
            code="N", reporting_type=HE_MANDATORY, lat=32.5, lon=35.0
        )
        north_ov = WaypointMarkerOverlay(
            chart_item=chart_item,
            calibration=north_cal,
            pixmap_size=size,
            waypoints=[wp],
            chart_seam_partition=partition,
        )
        assert north_ov.marker_count() == 1
        north_ov.teardown()


# ---------------------------------------------------------------------------
# Default constants
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_triangle_side_is_24(self) -> None:
        """Default bumped from 16 → 24 px (50 % larger) after
        user feedback that the original triangle was too small to
        read at typical satellite-view zoom levels."""
        assert DEFAULT_TRIANGLE_SIDE_PX == 24.0

    def test_z_value_above_satellite_tile_z(self) -> None:
        from cvfr_routemaster.satellite_overlay import SATELLITE_TILE_Z

        assert WAYPOINT_MARKER_Z > SATELLITE_TILE_Z

    def test_label_background_alpha_is_translucent(self) -> None:
        """Pilots glancing for landmarks shouldn't lose all
        visual context behind every label — the label backing
        rect must be only partially opaque."""
        assert LABEL_BACKGROUND.alpha() < 255

    def test_label_text_is_white(self) -> None:
        """Direct check that we ship the user's requested white
        fill (and not, say, the prior black-border-no-fill that
        rendered as illegible)."""
        assert LABEL_TEXT_COLOR.red() == 255
        assert LABEL_TEXT_COLOR.green() == 255
        assert LABEL_TEXT_COLOR.blue() == 255

    def test_mandatory_fill_is_chart_yellow(self) -> None:
        """Fill of "full" (mandatory) triangles is the printed
        chart's yellow rather than the prior solid black.
        Yellow pops against satellite imagery where black would
        blend with shadow / urban dark areas; the black inner
        border ring below preserves the high-contrast outline so
        the marker remains findable against its own bright fill.
        """
        # Saturated yellow gamut (red+green high, blue low) is
        # what the chart uses for these triangles' interior. We
        # check via the same gamut bands as the border-yellow
        # test so a future palette tweak picks up exactly one
        # set of bounds.
        assert MANDATORY_FILL.alpha() == 255
        assert MANDATORY_FILL.red() >= 200
        assert MANDATORY_FILL.green() >= 150
        assert MANDATORY_FILL.blue() <= 60
