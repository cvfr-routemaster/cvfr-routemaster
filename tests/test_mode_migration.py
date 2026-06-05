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

"""Tests for the one-time v3.3 → v4 per-project state migration."""

from __future__ import annotations

from pathlib import Path

import pytest

PySide6 = pytest.importorskip("PySide6")
from PySide6.QtCore import QSettings  # noqa: E402

from cvfr_routemaster import mode_migration, settings_store  # noqa: E402
from cvfr_routemaster.settings_store import (  # noqa: E402
    SETTINGS_INI_FILENAME,
    _settings,
)


@pytest.fixture
def fake_ini_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the settings INI to a tmp dir (mirrors the fixture in
    test_settings_ini_backend)."""
    monkeypatch.setattr(settings_store, "_settings_root", lambda: tmp_path)
    return tmp_path


def _flat(project_root: Path) -> Path:
    d = project_root / ".cvfr_routemaster"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_fresh_install_runs_once_and_sets_default_mode(
    fake_ini_root: Path, tmp_path: Path
) -> None:
    project_root = tmp_path
    assert not mode_migration.already_migrated(project_root)

    ran = mode_migration.migrate_v33_to_v4(project_root)
    assert ran is True
    assert mode_migration.already_migrated(project_root)
    assert settings_store.load_current_map_mode() == "cvfr"

    # Second call is a no-op.
    assert mode_migration.migrate_v33_to_v4(project_root) is False


def test_flat_caches_copied_into_cvfr_namespace(
    fake_ini_root: Path, tmp_path: Path
) -> None:
    project_root = tmp_path
    flat = _flat(project_root)
    (flat / "geo_calibration.json").write_text("{}", encoding="utf-8")
    (flat / "map_north.png").write_bytes(b"PNGDATA")
    (flat / "waypoints_cache.json").write_text("{}", encoding="utf-8")
    charts = flat / "charts"
    charts.mkdir()
    (charts / "cvfr_north.pdf").write_bytes(b"%PDF-1.4")

    mode_migration.migrate_v33_to_v4(project_root)

    cvfr = flat / "cvfr"
    assert (cvfr / "geo_calibration.json").is_file()
    assert (cvfr / "map_north.png").read_bytes() == b"PNGDATA"
    assert (cvfr / "waypoints_cache.json").is_file()
    assert (cvfr / "charts" / "cvfr_north.pdf").read_bytes() == b"%PDF-1.4"

    # Non-destructive: originals remain as a backup.
    assert (flat / "geo_calibration.json").is_file()
    assert (flat / "charts" / "cvfr_north.pdf").is_file()


def test_flat_qsettings_keys_copied_into_mode_group(
    fake_ini_root: Path, tmp_path: Path
) -> None:
    project_root = tmp_path
    s = _settings()
    s.setValue("pdf_north", "https://example/n.pdf")
    s.setValue("pdf_south", "https://example/s.pdf")
    s.setValue("map_layout_saved", True)
    s.setValue("map_north_x", 12.5)
    s.sync()

    mode_migration.migrate_v33_to_v4(project_root)

    after = _settings()
    assert after.value("modes/cvfr/pdf_north", "", str) == "https://example/n.pdf"
    assert after.value("modes/cvfr/pdf_south", "", str) == "https://example/s.pdf"
    assert after.value("modes/cvfr/map_layout_saved", False, bool) is True
    assert float(after.value("modes/cvfr/map_north_x", 0.0)) == 12.5

    # Originals preserved.
    assert after.value("pdf_north", "", str) == "https://example/n.pdf"


def test_load_pdf_paths_reads_migrated_cvfr_group(
    fake_ini_root: Path, tmp_path: Path
) -> None:
    """End-to-end: after migration, reading sources for the cvfr mode
    returns the user's previously-saved flat values."""
    s = _settings()
    s.setValue("pdf_north", "N-url")
    s.setValue("pdf_south", "S-url")
    s.setValue("pdf_back", "B-url")
    s.sync()

    mode_migration.migrate_v33_to_v4(tmp_path)

    north, south, back = settings_store.load_pdf_paths(None, "cvfr")
    assert (north, south, back) == ("N-url", "S-url", "B-url")


def test_does_not_overwrite_existing_namespaced_state(
    fake_ini_root: Path, tmp_path: Path
) -> None:
    project_root = tmp_path
    flat = _flat(project_root)
    (flat / "geo_calibration.json").write_text("OLD", encoding="utf-8")
    cvfr = flat / "cvfr"
    cvfr.mkdir()
    (cvfr / "geo_calibration.json").write_text("NEW", encoding="utf-8")

    s = _settings()
    s.setValue("pdf_north", "old-flat")
    s.setValue("modes/cvfr/pdf_north", "already-namespaced")
    s.sync()

    mode_migration.migrate_v33_to_v4(project_root)

    # Existing namespaced destinations win; flat values don't clobber.
    assert (cvfr / "geo_calibration.json").read_text(encoding="utf-8") == "NEW"
    assert _settings().value("modes/cvfr/pdf_north", "", str) == "already-namespaced"


def test_existing_current_mode_not_overwritten(
    fake_ini_root: Path, tmp_path: Path
) -> None:
    s = _settings()
    s.setValue(settings_store.CURRENT_MAP_MODE_KEY, "lsa")
    s.sync()

    mode_migration.migrate_v33_to_v4(tmp_path)

    assert settings_store.load_current_map_mode() == "lsa"
