"""Parser for PyInstaller's ``warn-<spec-stem>.txt`` file.

Used by ``scripts/build_release.py`` (Windows) and
``scripts/build_release_for_linux.py`` (Linux) to turn unresolved
top-level imports from inside our application package into a hard
build failure rather than a silently-printed warning.

Why this matters
----------------

The Linux release v2 was built in a WSL venv assembled with an
explicit ``pip install pyinstaller pyside6 pymupdf pillow pytesseract
pytest`` list that did *not* include numpy. PyInstaller's analyser
correctly flagged it::

    missing module named numpy - imported by cvfr_routemaster.map_crop (top-level)

but the build script previously just let that warning slide. The
shipped binary crashed on the user's laptop the moment Python tried
to import ``cvfr_routemaster.map_crop``::

    File "cvfr_routemaster/map_crop.py", line 19, in <module>
    ModuleNotFoundError: No module named 'numpy'
    [PYI-248:ERROR] Failed to execute script '__main__' due to unhandled exception!

The Windows build happens to ship numpy because the dev Windows
Python environment has it installed as a transitive dependency of
something else; the Linux build venv didn't, and there was no
mechanism to catch the gap.

What this module does
---------------------

It parses the warn file PyInstaller writes to
``build/<spec-stem>/warn-<spec-stem>.txt`` after every build and
returns the subset of missing-module reports that are:

  - Imported at **top-level** (not inside ``try``/``if``/a function
    body): these are the only ones guaranteed to crash at
    ``import``-time before any application code runs.
  - Imported from inside our application package (default
    ``cvfr_routemaster``): third-party libs routinely have
    optional top-level imports of things PyInstaller doesn't
    bundle (e.g. ``PIL._typing`` flagging ``numpy``), and we'd
    drown in noise if we treated those as build failures.
  - Not in the running interpreter's stdlib: PyInstaller always
    bundles the running Python's stdlib into the frozen binary, so
    a "missing module named ``collections.abc``" warning can't
    produce a real runtime ``ImportError`` — but PyInstaller's
    Python 3.13 analyser flags it as missing anyway, including
    against importers inside ``cvfr_routemaster``. Filtering by
    :data:`sys.stdlib_module_names` drops those false positives
    cleanly.

The build scripts call :func:`scan_missing_top_level_imports` after
a successful PyInstaller invocation; if it returns a non-empty list
the script prints :func:`format_missing_imports_message` and
``sys.exit(1)``s.

The warn-file format (lines wrap; trimmed for the docstring)::

    missing module named foo - imported by bar (top-level)
    missing module named 'foo.bar' - imported by baz (conditional), qux (top-level)
    missing module named numpy - imported by PIL._typing (conditional, optional), cvfr_routemaster.map_crop (top-level), pytesseract.pytesseract (optional)

Notes on the grammar:

- A module name (after ``missing module named``) may or may not be
  quoted with ``'`` (PyInstaller quotes names that contain ``.``).
- An importer entry has the shape ``NAME (QUALIFIERS)``; QUALIFIERS
  is one or more of ``top-level``, ``conditional``, ``delayed``,
  ``optional`` joined by ``, ``.
- The importer NAME may itself be quoted (rare; happens for some
  PySide6 sub-paths) or be an absolute filesystem path (rare;
  happens for PyInstaller's own runtime hooks). We strip surrounding
  quotes and otherwise treat the NAME as opaque.
- Multiple importer entries are joined by ``, `` at the *top level*
  — but the commas inside ``(...)`` qualifier groups must stay with
  their entry. ``_split_importers`` handles that with a depth counter
  rather than ``str.split(", ")``.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path


# Python stdlib module names (e.g. ``"collections"``, ``"typing"``).
# Captured once at import time; the set is stable for the running
# interpreter. Used by :func:`scan_missing_top_level_imports` to drop
# stdlib false positives — PyInstaller always bundles the running
# Python's stdlib, so a "missing module named collections.abc" warning
# can't possibly produce a runtime ImportError. PyInstaller's static
# analyser flags ``collections.abc`` against ~70 importers on Python
# 3.13 (traceback, typing, inspect, every Qt/PIL/numpy submodule, plus
# any of our own files that ``from collections.abc import X``) even
# though the import works fine at runtime; without this filter the
# build refuses to ship a perfectly good binary.
_STDLIB_MODULE_NAMES: frozenset[str] = frozenset(sys.stdlib_module_names)


def _is_stdlib(module: str) -> bool:
    """True iff ``module`` is part of the current Python's stdlib.

    Matches the top-level package name (``collections.abc`` → check
    ``collections``) so submodule references like ``'collections.abc'``
    and ``'importlib.resources'`` are also recognised. PyInstaller
    always bundles the running interpreter's stdlib into the frozen
    binary, so a stdlib name in the "missing" report is always a
    false-positive of the static analyser, never a real ship-time risk.
    """
    top = module.split(".", 1)[0]
    return top in _STDLIB_MODULE_NAMES


# Captures ``missing module named <module> - imported by <imports>``
# where <module> may be quoted (``'collections.abc'``) and <imports>
# is the remainder of the line that ``_split_importers`` then
# subdivides.
_MISSING_LINE = re.compile(
    r"^missing module named (?P<module>.+?) - imported by (?P<imports>.+)$"
)


def _strip_quotes(name: str) -> str:
    """Remove a balanced pair of surrounding ``'`` or ``"`` quotes
    from ``name``. PyInstaller wraps module names containing dots in
    single quotes (e.g. ``'collections.abc'``); we want the bare name
    for comparison against ``app_package`` prefixes.
    """
    if len(name) >= 2 and name[0] == name[-1] and name[0] in ("'", '"'):
        return name[1:-1]
    return name


def _split_importers(text: str) -> list[str]:
    """Split a PyInstaller importer list on top-level commas only.

    ``foo (top-level), bar (conditional, optional), baz (delayed)``
    splits to three entries; the comma between ``conditional`` and
    ``optional`` stays with the ``bar`` entry because it's nested
    inside that entry's ``(...)`` qualifier group.

    We track parenthesis depth with a counter rather than using
    ``str.split(", ")`` so any future PyInstaller format change that
    introduces nested parens (unlikely) still parses sensibly.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            piece = "".join(current).strip()
            if piece:
                parts.append(piece)
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


# Captures one importer entry ``<name> (<qualifiers>)``. ``.+?``
# instead of ``.+`` so the ``(``-then-qualifiers match wins greedily
# from the right (importer paths can themselves contain parens in
# principle — they don't today, but the non-greedy match is robust
# either way).
_IMPORTER_ENTRY = re.compile(r"^(?P<name>.+?)\s*\((?P<qualifiers>[^)]+)\)\s*$")


def _parse_importer(entry: str) -> tuple[str, str] | None:
    """Parse one importer entry like ``cvfr_routemaster.map_crop (top-level)``.

    Returns ``(importer_name, qualifiers_text)`` with surrounding
    quotes stripped from the name. Returns ``None`` if the entry
    doesn't match the expected shape — we'd rather skip a malformed
    record than crash the build over a PyInstaller format quirk.
    """
    m = _IMPORTER_ENTRY.match(entry)
    if m is None:
        return None
    return _strip_quotes(m.group("name").strip()), m.group("qualifiers")


@dataclass(frozen=True)
class MissingImport:
    """One PyInstaller-flagged unresolved top-level import.

    Attributes:
      module:     The module that couldn't be resolved
                  (e.g. ``"numpy"``).
      importer:   The Python module (or rarely, file path) that
                  imports it
                  (e.g. ``"cvfr_routemaster.map_crop"``).
      qualifiers: The qualifier text PyInstaller assigned to this
                  importer's reference, e.g. ``"top-level"`` or
                  ``"top-level, conditional"``.

    Frozen so callers can use these as dict keys / set members
    without worrying about accidental mutation between scan and
    report stages.
    """

    module: str
    importer: str
    qualifiers: str

    def is_top_level(self) -> bool:
        """True iff this importer references the missing module at
        top-level (i.e. directly at module import time, not inside
        a function, ``try`` block, or ``if`` branch).
        """
        return "top-level" in self.qualifiers


def scan_missing_top_level_imports(
    warn_path: Path,
    app_package: str,
) -> list[MissingImport]:
    """Find missing top-level imports from inside ``app_package``.

    Top-level imports PyInstaller couldn't resolve will crash the
    shipped binary at launch with ``ModuleNotFoundError`` before any
    application code runs — there's no ``try``/``except`` wrapping
    a top-level ``import foo`` statement, so the failure is
    guaranteed.

    Records flagged ``conditional`` / ``optional`` / ``delayed``
    (without ``top-level``) are deliberately ignored — those are
    runtime-guarded by the importing code and won't break a
    happy-path launch. (E.g. ``pytesseract.pytesseract`` does
    ``try: import numpy`` in an optional branch; we don't care.)

    Imports from outside ``app_package`` are ignored too: third-party
    libs routinely have top-level imports of optional deps that
    PyInstaller doesn't bundle by default (PIL importing ``defusedxml``,
    PySide6's deploy script importing ``deploy_lib``, etc.), and
    treating those as build failures would mean the scanner is
    permanently red regardless of what we ship.

    Stdlib references are also filtered: PyInstaller's static analyser
    on Python 3.13 flags ``'collections.abc'`` as "missing" against
    ~70 importers (stdlib ``traceback`` / ``typing`` / ``inspect``,
    every Qt/PIL/numpy submodule, plus our own ``from collections.abc
    import Iterable``), even though the frozen binary always bundles
    the running interpreter's stdlib and the import succeeds at
    runtime. We trust :data:`sys.stdlib_module_names` to enumerate
    those names; anything in there can't be a real ship-time risk.

    Args:
        warn_path: Path to PyInstaller's
            ``build/<spec-stem>/warn-<spec-stem>.txt``. Returns an
            empty list if the file doesn't exist (the build never
            ran, or it ran with ``--clean`` and the warn file
            hasn't been written yet).
        app_package: The top-level package name to filter importers
            by (e.g. ``"cvfr_routemaster"``). Matches ``app_package``
            exactly and any dotted descendant (``app_package.foo``,
            ``app_package.foo.bar``), but NOT ``app_package_other``.

    Returns:
        List of :class:`MissingImport` records. Empty list means
        no actionable misses — safe to ship.
    """
    if not warn_path.is_file():
        return []

    failures: list[MissingImport] = []
    text = warn_path.read_text(encoding="utf-8", errors="replace")
    package_prefix = app_package + "."
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.startswith("missing module named "):
            continue
        m = _MISSING_LINE.match(line)
        if m is None:
            continue
        module = _strip_quotes(m.group("module").strip())
        if _is_stdlib(module):
            continue
        for entry in _split_importers(m.group("imports")):
            parsed = _parse_importer(entry)
            if parsed is None:
                continue
            importer, qualifiers = parsed
            if not (importer == app_package or importer.startswith(package_prefix)):
                continue
            if "top-level" not in qualifiers:
                continue
            failures.append(MissingImport(
                module=module,
                importer=importer,
                qualifiers=qualifiers,
            ))
    return failures


def format_missing_imports_message(missing: list[MissingImport]) -> str:
    """Render a multi-line operator-facing diagnostic for a
    non-empty list of :class:`MissingImport` records.

    The message has three parts:

    1. **Header** explaining what was found and why it matters
       (shipped binary will crash at launch).
    2. **Per-module bullets** grouping importers under each missing
       module — collapses the ``numpy imported by map_crop AND
       altitude_arrows`` case to one fix-it line.
    3. **Remediation hint**: a concrete ``pip install ...`` command
       listing exactly the modules to install, plus the escape hatch
       (mark genuinely-optional imports in the spec's ``excludes``
       list rather than disabling the check).

    Returns the rendered string; raises ``ValueError`` if
    ``missing`` is empty (the caller shouldn't be formatting a
    success case).
    """
    if not missing:
        raise ValueError(
            "format_missing_imports_message called with an empty list; "
            "the caller should branch on emptiness before formatting"
        )

    lines: list[str] = [
        "ERROR: PyInstaller flagged unresolved top-level imports from "
        "inside the application package. The shipped binary would "
        "crash at launch with ModuleNotFoundError because PyInstaller "
        "couldn't resolve the import during analysis — almost always "
        "because the build venv is missing a pip-installable dependency.",
        "",
    ]
    # Group by module so a single missing dep imported from multiple
    # app modules collapses to one bullet with multiple importer lines.
    by_module: dict[str, set[str]] = {}
    for mi in missing:
        by_module.setdefault(mi.module, set()).add(mi.importer)
    for module in sorted(by_module):
        lines.append(f"  - missing: {module}")
        for imp in sorted(by_module[module]):
            lines.append(f"      imported by: {imp}")
    lines.append("")
    lines.append(
        "Fix: install the missing module(s) into the build venv and "
        "rebuild. For example:"
    )
    lines.append("")
    lines.append(
        "    pip install " + " ".join(sorted({mi.module for mi in missing}))
    )
    lines.append("")
    lines.append(
        "If a missing module is genuinely optional (a try/except wraps "
        "the import that PyInstaller's analysis missed), wrap the "
        "import site or add the module to the spec's ``excludes`` "
        "list. Do not bypass this check — that's how the Linux release "
        "v2 shipped a binary that crashed on launch with "
        "\"No module named 'numpy'\"."
    )
    return "\n".join(lines)
