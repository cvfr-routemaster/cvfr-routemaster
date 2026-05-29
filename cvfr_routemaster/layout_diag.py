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

"""
Project-local diagnostic logger for the south-sheet startup-move investigation.

Captures the *minimum* information needed to answer one question the next time
the bug reproduces: did the bad sheet position come from the saved layout on
disk, or did something inside this session mutate the sheet at runtime?

The log lives at ``<project_root>/.cvfr_routemaster/sheet-layout-debug.log``
with rotation (~256 KiB per file, 5 files kept). Each line is a structured
record of the form::

    YYYY-MM-DDTHH:MM:SS.mmm | <event> | k1=v1 k2=v2 ...

Floats are formatted to ~9 significant figures so we can spot epsilon drift.
``None`` becomes ``-`` so a missing sheet item shows up unambiguously rather
than blending into 0.0. The module is process-wide and idempotent: the first
``init(...)`` wins, subsequent calls are no-ops, and ``log(...)`` before
``init(...)`` is a silent no-op so importing this file from a unit-test
context never blows up.

Why a hand-rolled formatter instead of stdlib ``logging.Formatter``: every
event is a single dictionary of small fields, and the diagnostic question is
answered by *diffing* lines. A predictable ``k=v`` layout is much easier to
``rg`` and eyeball than free-form messages, and avoids quoting surprises that
would force escaping in field values that come from filesystem paths.
"""

from __future__ import annotations

import logging
import logging.handlers
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOGGER_NAME = "cvfr_routemaster.layout_diag"
_LOG_FILE_NAME = "sheet-layout-debug.log"
_MAX_BYTES = 256 * 1024
_BACKUP_COUNT = 5

_initialised = False


def _format_value(v: Any) -> str:
    """Format a single field value for the human-readable log line.

    Floats get ~9 significant figures (enough to expose epsilon drift on a
    pixel-scale coordinate without dumping 17-digit ulps on every line). Ints,
    bools, and ``None`` get short canonical forms; everything else falls back
    to ``str()`` with whitespace and ``=`` neutralised so a stray newline in a
    Windows path can't corrupt the field grammar.
    """
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        if not math.isfinite(v):
            return repr(v)  # 'nan' / 'inf' / '-inf'
        return f"{v:.9g}"
    if isinstance(v, int):
        return str(v)
    s = str(v)
    return s.replace("\n", "\\n").replace("\r", "\\r").replace(" ", "_")


class _CompactFormatter(logging.Formatter):
    """Render ``record.msg`` as the event name and ``record.args`` as fields."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        )
        event = str(record.msg)
        fields = record.args if isinstance(record.args, dict) else {}
        parts = [f"{k}={_format_value(v)}" for k, v in fields.items()]
        body = " ".join(parts)
        return f"{ts} | {event} | {body}".rstrip()


def _ensure_log_dir(project_root: Path) -> Path:
    d = project_root / ".cvfr_routemaster"
    d.mkdir(exist_ok=True)
    return d / _LOG_FILE_NAME


def init(project_root: Path) -> None:
    """Idempotent setup. Safe to call from ``MainWindow.__init__`` every run."""
    global _initialised
    if _initialised:
        return
    try:
        path = _ensure_log_dir(project_root)
    except OSError:
        # If the project dir is unwritable (read-only mount, permissions),
        # silently skip — diagnostics are nice-to-have and must never block
        # the app from starting.
        _initialised = True
        return

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # don't bubble into the root logger / stdout
    # Defensive: remove any handlers a previous re-import attached so a hot-
    # reload during development doesn't double every line.
    for h in list(logger.handlers):
        logger.removeHandler(h)

    try:
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        _initialised = True
        return
    handler.setFormatter(_CompactFormatter())
    logger.addHandler(handler)
    _initialised = True

    log(
        "session.start",
        pid=os.getpid(),
        py=sys.version.split()[0],
        platform=sys.platform,
        project=str(project_root),
    )


def log(event: str, /, **fields: Any) -> None:
    """Emit one event line. No-op if ``init`` was never called or failed.

    ``event`` is a short dotted name (``"sheet.setPos"``, ``"persist.layout"``)
    so log filtering with ``rg`` stays trivial: ``rg '\\| sheet\\.' debug.log``.
    """
    if not _initialised:
        return
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        return
    logger.info(event, fields)


def snapshot_sheets(label: str, north_item: Any, south_item: Any) -> None:
    """Capture both sheet positions and scales in one event.

    ``label`` is the *call site* (e.g. ``"on_map_finished.after_load"``) so the
    log reads as a chronological story even when several events fire from the
    same Qt slot.
    """

    def _state(item: Any) -> dict[str, Any]:
        if item is None:
            return {"x": None, "y": None, "scale": None, "pix_w": None, "pix_h": None}
        try:
            p = item.pos()
            x, y = float(p.x()), float(p.y())
        except (AttributeError, TypeError):
            x = y = None  # type: ignore[assignment]
        try:
            sc = float(item.scale())
        except (AttributeError, TypeError):
            sc = None  # type: ignore[assignment]
        try:
            pm = item.pixmap()
            pw, ph = int(pm.width()), int(pm.height())
        except (AttributeError, TypeError):
            pw = ph = None  # type: ignore[assignment]
        return {"x": x, "y": y, "scale": sc, "pix_w": pw, "pix_h": ph}

    n = _state(north_item)
    s = _state(south_item)
    log(
        "sheet.snapshot",
        at=label,
        n_x=n["x"],
        n_y=n["y"],
        n_scale=n["scale"],
        n_pw=n["pix_w"],
        n_ph=n["pix_h"],
        s_x=s["x"],
        s_y=s["y"],
        s_scale=s["scale"],
        s_pw=s["pix_w"],
        s_ph=s["pix_h"],
    )
