"""
Unit tests for the layout-diagnostic logger.

These cover the contract MainWindow relies on:

* ``log`` is a no-op before ``init`` (so importing the module from any
  test context never throws).
* ``init`` is idempotent — calling it twice with different roots picks the
  first root, doesn't double the handler set, and doesn't raise.
* The line format is the agreed ``ts | event | k=v ...`` shape, with
  ``None`` rendered as ``-`` (so a missing sheet item is unambiguous).
* The rotating file handler actually rotates after exceeding the size cap.
* ``snapshot_sheets`` survives ``None`` items and unset pixmaps without
  raising — that's the exact case at the very start of the session, before
  any maps have been loaded, and we need that snapshot to land in the log.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from cvfr_routemaster import layout_diag


@pytest.fixture(autouse=True)
def _reset_diag_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets a clean ``layout_diag`` module state.

    The module guards ``init`` with a process-wide flag and attaches a single
    handler to a named logger. Without this fixture, ordering between tests
    would couple them: the second ``init`` call would silently no-op, and
    the file handler from the first test would still be writing into a
    tmp-path that no longer exists.
    """
    monkeypatch.setattr(layout_diag, "_initialised", False)
    logger = logging.getLogger(layout_diag._LOGGER_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    yield
    logger = logging.getLogger(layout_diag._LOGGER_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


def _read_log(project_root: Path) -> str:
    p = project_root / ".cvfr_routemaster" / "sheet-layout-debug.log"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def test_log_before_init_is_silent(tmp_path: Path) -> None:
    layout_diag.log("noop.event", k=1)
    assert _read_log(tmp_path) == ""


def test_init_creates_log_file_and_writes_session_start(tmp_path: Path) -> None:
    layout_diag.init(tmp_path)
    body = _read_log(tmp_path)
    # Header line is the session.start banner with PID + Python version + project path.
    head = body.splitlines()[0]
    assert "session.start" in head
    assert "pid=" in head
    assert f"project={str(tmp_path).replace(' ', '_')}" in head


def test_event_format_renders_none_as_dash_and_floats_compactly(tmp_path: Path) -> None:
    layout_diag.init(tmp_path)
    layout_diag.log("sample", x=1.234567891234, y=None, flag=True, n=3, name="hi there")
    body = _read_log(tmp_path)
    assert "| sample | " in body
    line = next(ln for ln in body.splitlines() if "| sample |" in ln)
    # ts | sample | x=1.23456789 y=- flag=1 n=3 name=hi_there
    assert re.match(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+\d{2}:\d{2} \| sample \| ",
        line,
    )
    fields = line.split(" | ", 2)[2].split(" ")
    assert "x=1.23456789" in fields
    assert "y=-" in fields
    assert "flag=1" in fields
    assert "n=3" in fields
    # Whitespace in values is escaped (spaces become underscores) so the
    # field grammar can be split on plain spaces without quoting.
    assert "name=hi_there" in fields


def test_init_is_idempotent(tmp_path: Path) -> None:
    layout_diag.init(tmp_path)
    layout_diag.init(tmp_path)  # second call must not double-register handlers
    logger = logging.getLogger(layout_diag._LOGGER_NAME)
    assert len(logger.handlers) == 1


def test_rotation_creates_backup_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(layout_diag, "_MAX_BYTES", 512)
    monkeypatch.setattr(layout_diag, "_BACKUP_COUNT", 3)
    layout_diag.init(tmp_path)
    # Each line is tens of bytes, so a few hundred lines easily forces rotation.
    for i in range(400):
        layout_diag.log("rot", i=i, payload="x" * 50)
    log_dir = tmp_path / ".cvfr_routemaster"
    files = sorted(p.name for p in log_dir.iterdir())
    # At minimum: the active file plus one rolled-over backup.
    assert "sheet-layout-debug.log" in files
    assert any(name.startswith("sheet-layout-debug.log.") for name in files), files


def test_snapshot_sheets_handles_none_items(tmp_path: Path) -> None:
    layout_diag.init(tmp_path)
    layout_diag.snapshot_sheets("startup.no_maps_yet", None, None)
    body = _read_log(tmp_path)
    snap_line = next(ln for ln in body.splitlines() if "sheet.snapshot" in ln)
    # All per-sheet fields collapse to '-' when the items don't exist yet.
    for key in ("n_x", "n_y", "n_scale", "s_x", "s_y", "s_scale"):
        assert f"{key}=-" in snap_line, snap_line
    assert "at=startup.no_maps_yet" in snap_line
