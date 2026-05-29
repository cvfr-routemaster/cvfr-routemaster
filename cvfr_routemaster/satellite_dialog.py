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

"""User-facing prompts for the v3 satellite-imagery feature.

Two dialog flows live here:

  1. :func:`show_first_download_notice` — informational popup shown
     once per install before the bulk satellite-imagery download
     starts. Tells the user the download will happen, quotes the
     size, and notes that the download will resume across
     interruptions. There is **no accept/decline** branching;
     the program needs the tiles to function, downloads them
     unconditionally, and uses this notice solely to make sure
     the user is not surprised by the disk + network activity.
  2. :func:`show_completion_toast` — non-blocking "satellite
     imagery ready" notice shown once when the bulk fetch
     finishes a session in which tiles were actually fetched.

History
-------

Pre-v3.3 builds had a three-dialog consent flow
(``prompt_first_launch`` / ``confirm_decline_warning`` /
``prompt_resume``) that asked the user to accept or decline the
download and offered resume / restart / skip choices on
interrupted sessions. That flow was deleted because:

* The download isn't actually optional in practice — without
  the imagery the satellite-view toggle just shows a gray field,
  which has no meaningful use case in a flight-planning app.
* Resume is silent now (see
  :meth:`cvfr_routemaster.main_window.MainWindow._check_satellite_resume_on_startup`)
  so there's nothing to prompt about — the worker just keeps
  walking the missing-tile list it discovers via
  :func:`satellite_fetch.tiles_to_fetch_for_bbox`.

Tests can replace :func:`show_first_download_notice` with a stub
to drive the state machine without spawning a modal.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget

#: Approximate per-tile size for cost-estimate text. Real Esri
#: tiles vary 8–25 KB depending on imagery contents (ocean tiles
#: are tiny, dense urban tiles are big); 18 KB is a fair middle.
APPROX_TILE_SIZE_BYTES: int = 18 * 1024

#: Approximate per-tile fetch time for the time-estimate text.
#: Conservative — assumes ~30 ms median round-trip on a typical
#: residential connection plus the JPEG payload. Tunes the
#: "estimated N minutes" wording so the user isn't surprised.
APPROX_TILE_FETCH_S: float = 0.10


def _format_size_mb(tile_count: int) -> str:
    """Pretty-print the bulk-fetch download size in MB."""
    bytes_total = tile_count * APPROX_TILE_SIZE_BYTES
    mb = bytes_total / (1024 * 1024)
    if mb < 100.0:
        return f"~{mb:.0f} MB"
    return f"~{mb / 1024:.1f} GB" if mb >= 1024.0 else f"~{int(mb)} MB"


def _format_duration(tile_count: int) -> str:
    """Pretty-print the bulk-fetch wall time as "Nm" or "Nh Nm"."""
    seconds_total = tile_count * APPROX_TILE_FETCH_S
    minutes, seconds = divmod(int(seconds_total), 60)
    if minutes < 1:
        return f"{seconds}s"
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def show_first_download_notice(
    parent: QWidget | None,
    *,
    tile_count: int,
    zoom_levels: list[int],
) -> None:
    """One-time informational notice surfaced before the bulk
    satellite-imagery download starts on a fresh install.

    The dialog is informational, not consensual: there's a single
    OK button. The caller is expected to start the bulk worker
    immediately on return and to persist
    :func:`settings_store.save_satellite_notice_shown(True)` so
    the notice doesn't reappear on subsequent launches.

    Body content:

    * Tile count and approximate disk size — derived from
      :data:`APPROX_TILE_SIZE_BYTES`, same per-tile estimate the
      pre-v3.3 consent prompt used.
    * Zoom-level range — so a user inspecting the imagery (or
      our cache directory) can correlate the numbers they see
      with the slippy-map zoom convention.
    * Resume-on-interrupt promise — the bulk worker writes its
      progress to ``_download_state.json`` for the primary zoom
      and walks the filesystem for the secondaries, so closing
      the app mid-download loses at most a few in-flight tiles.
    * Esri attribution — required by Esri's tile-service terms
      whenever cached imagery is surfaced. The Legal & Copyright
      dialog carries the same attribution, but a user dismissing
      the notice without later visiting that dialog has still
      seen the attribution at least once.
    """
    if zoom_levels:
        sorted_zooms = sorted(int(z) for z in zoom_levels)
        if len(sorted_zooms) == 1:
            zoom_range_html = f"zoom level <b>z={sorted_zooms[0]}</b>"
        else:
            zoom_range_html = (
                f"zoom levels <b>z={sorted_zooms[0]}–z={sorted_zooms[-1]}</b>"
            )
    else:
        zoom_range_html = "the configured zoom levels"

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("Satellite imagery download")
    box.setText("<b>Satellite imagery download</b>")
    box.setInformativeText(
        f"<p>This program will download approximately "
        f"<b>{tile_count:,} satellite tiles</b> "
        f"(<b>{_format_size_mb(tile_count)}</b>) in the background, "
        f"covering Israel at {zoom_range_html} for offline use.</p>"
        "<p>The download runs while you use the program. You can "
        "close the app at any time — the download will resume "
        "automatically the next time you launch it, picking up "
        "where it left off.</p>"
        "<p>No further confirmation is needed; the download will "
        "start as soon as you dismiss this notice.</p>"
        "<p><i>Imagery courtesy of Esri, Maxar, Earthstar "
        "Geographics, USDA FSA, USGS, AeroGRID, IGN, and the GIS "
        "User Community.</i></p>"
    )
    # Single OK button. ``setStandardButtons`` is explicit (rather
    # than relying on the QMessageBox.Information default) so the
    # X-close / Esc paths both come back through the same button.
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.setDefaultButton(QMessageBox.StandardButton.Ok)
    box.exec()


def show_completion_toast(
    parent: QWidget | None,
    *,
    total_tiles: int,
) -> None:
    """Non-blocking completion notice. Shown once when the bulk
    fetch finishes a session it actually fetched tiles in (i.e. not
    on every "fully cached, nothing to do" no-op).

    Uses an information-level message box rather than the status bar
    because the bulk download is a meaningful long-running task and
    users notice when it's done — burying the "you can now use
    satellite view freely" message in a 3 s status-bar transient
    would underplay the moment.
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("Satellite imagery ready")
    box.setText(
        f"<b>Downloaded {total_tiles:,} tiles. "
        "Satellite view is ready.</b>"
    )
    box.setInformativeText(
        "<p>You can now toggle Satellite view from the toolbar at any "
        "time without download delay.</p>"
    )
    box.exec()


__all__ = [
    "APPROX_TILE_FETCH_S",
    "APPROX_TILE_SIZE_BYTES",
    "show_completion_toast",
    "show_first_download_notice",
]
