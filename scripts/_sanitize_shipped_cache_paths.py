"""Strip dev-machine absolute paths from shipped cache JSONs.

Why
---

The five seed cache JSONs that ride the release bundle
(``waypoints_cache.json``, ``geo_calibration.json``,
``altitude_arrows_{north,south}.json``, ``map_images_meta.json``)
each record a ``"path"`` field next to the PDF fingerprint they
were derived from. On the dev box the field looks like::

    "path": "C:\\flying\\cvfr-routemaster\\CVFR-NORTH-OCT-2025-UPD2.pdf"

If we ship the JSONs verbatim that absolute path travels with the
release — leaking the dev's filesystem layout (drive letter,
top-level folder name) into every shipped bundle. Friends who
unpack the release on their own machines see the dev's repo root
in plain text inside ``.cvfr_routemaster/``.

The fix
-------

Walk every cache JSON, find each ``"path"`` field whose value is a
filesystem path string, and replace it with the bare PDF filename.
Friends still see ``"path": "CVFR-NORTH-OCT-2025-UPD2.pdf"`` — enough
to answer "which PDF was this cache built from?" without giving up
the dev's layout.

Why this is safe
----------------

The shipped caches do **not** use the ``path`` field for anything
load-bearing. The cache-fingerprint check in
:func:`cvfr_routemaster.altitude_cache.fingerprints_match` (and the
mirror checks in ``geo_calibration``, ``waypoints_cache``,
``map_images_meta``) compares the stored ``(mtime_ns, size)`` pair
against the live PDF, never the ``path`` string. The string is
preserved only for human diagnostics ("which PDF was this cache
built from?"). The path-portability tests in
``tests/test_release_portability.py`` and
``tests/test_altitude_cache.py`` pin that contract.

Schema awareness
----------------

The five shipped caches have three structural shapes for their PDF
fingerprint blocks. The sanitiser handles all three by walking dicts
recursively and rewriting any ``"path"`` key whose neighbours form a
PDF fingerprint block (``mtime_ns`` + ``size``):

* **Single-PDF flat** — ``waypoints_cache.back_pdf``,
  ``altitude_arrows_{north,south}.pdf``.
* **Dual-PDF flat** — ``map_images_meta.{north_pdf, south_pdf}``.
* **Dual-PDF nested** — ``geo_calibration.{north, south}.pdf``.

Schema-blind walking (rather than enumerating the three shapes) means
a future cache format that adds a fourth shape gets sanitised
automatically.

Call sites
----------

Both release pipelines invoke :func:`sanitize_shipped_cache_paths`
after ``_copy_seed_cache`` and after the mtime-restamp step. The
restamp helper writes the same files back to disk, so the
sanitisation must run *after* it to avoid being clobbered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

RELEASE_CACHE_SUBDIR = ".cvfr_routemaster"


@dataclass
class SanitizeReport:
    """Outcome of one sanitisation pass over a release tree.

    Mirrors the report shape used by ``_restamp_cache_fingerprints``
    so the build scripts can use both helpers with the same idiom.
    """

    updates: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    """Per-file list of ``(json-path, before, after)`` triples for
    every ``path`` field that was rewritten. Empty list means the
    file had no leaks. Key is the cache JSON's filename relative
    to ``.cvfr_routemaster/``."""

    skipped: list[str] = field(default_factory=list)
    """Filenames the helper couldn't process (malformed JSON,
    missing file, unreadable). Build scripts print these as warnings
    but don't fail — a missing cache file is a pre-existing concern
    that the rest of the pipeline already validates."""

    def total_fields_updated(self) -> int:
        """Number of leaks scrubbed across every cache file. Used
        by the build summary so a "0 leaks fixed" emission means
        either the dev cache was already clean or every cache is
        new enough to never have stored an absolute path."""
        return sum(len(updates) for updates in self.updates.values())


def _looks_like_pdf_fingerprint_block(node: dict) -> bool:
    """Return True if ``node`` looks like a PDF fingerprint block.

    A fingerprint block is a dict with both ``path`` and ``mtime_ns``
    keys (``size`` is also expected in practice but we only require
    the two whose presence together is the strongest signal). Used
    to scope the path-rewrite to actual fingerprint blocks and not
    accidentally any unrelated ``"path"`` key a future schema might
    add elsewhere in the tree.
    """
    return "path" in node and "mtime_ns" in node and isinstance(node["path"], str)


def _basename_of(path_str: str) -> str:
    """Return just the filename component of ``path_str``.

    ``os.path.basename`` is platform-aware — on POSIX it splits only
    on ``/`` even if the input is a Windows path. The shipped JSONs
    use ``\\``-separated paths because the dev box is Windows, so
    we need to handle both separators regardless of which OS the
    sanitiser is running on. ``rsplit`` on the union of both
    separators does the right thing on every host.
    """
    for sep in ("\\", "/"):
        if sep in path_str:
            path_str = path_str.rsplit(sep, 1)[-1]
    return path_str


def _walk_and_rewrite(
    node: object,
    json_path: str,
    rewrites: list[tuple[str, str, str]],
) -> None:
    """Recursively walk ``node``; rewrite ``path`` fields in any
    fingerprint block we find.

    Mutates ``node`` in place and appends a ``(json-path, before,
    after)`` tuple to ``rewrites`` for each rewrite. ``json_path``
    is a dotted breadcrumb (``north.pdf``, ``back_pdf``, ``pdf``)
    used so the build's report can tell the user *which* leak was
    scrubbed inside a multi-PDF cache file.
    """
    if isinstance(node, dict):
        if _looks_like_pdf_fingerprint_block(node):
            before = node["path"]
            after = _basename_of(before)
            if before != after:
                node["path"] = after
                rewrites.append((f"{json_path}.path", before, after))
        for key, value in node.items():
            sub_path = f"{json_path}.{key}" if json_path else key
            _walk_and_rewrite(value, sub_path, rewrites)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk_and_rewrite(item, f"{json_path}[{index}]", rewrites)


def sanitize_shipped_cache_paths(release_root: Path) -> SanitizeReport:
    """Strip absolute paths from every cache JSON under
    ``release_root / .cvfr_routemaster``.

    Args:
        release_root: The release bundle root (the folder that
            contains the executable, ``map-pdfs/``, and
            ``.cvfr_routemaster/``). Matches the contract used by
            :func:`scripts._restamp_cache_fingerprints.restamp_cache_fingerprints`.

    Returns:
        A :class:`SanitizeReport` describing what got rewritten and
        what got skipped. Callers (build scripts) inspect
        ``updates`` for the human summary line and ``skipped`` for
        warnings.

    Idempotent: re-running on a tree that's already been sanitised
    is a no-op (the basename of a basename is itself).
    """
    cache_dir = release_root / RELEASE_CACHE_SUBDIR
    report = SanitizeReport()
    if not cache_dir.is_dir():
        return report
    # ``rglob`` (not ``glob``) so the v4 per-mode namespaces —
    # ``.cvfr_routemaster/cvfr/*.json`` and
    # ``.cvfr_routemaster/lsa/*.json`` — are scrubbed too, not just
    # the legacy flat top-level JSONs. The report key is the path
    # relative to the cache dir (POSIX separators) so a flat file
    # keys as its bare basename (back-compat with pre-v4 callers
    # and tests) while a namespaced file keys as ``cvfr/<name>``,
    # avoiding a collision between the same filename in two modes.
    for cache_file in sorted(cache_dir.rglob("*.json")):
        name = cache_file.relative_to(cache_dir).as_posix()
        try:
            raw = cache_file.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            report.skipped.append(name)
            continue
        rewrites: list[tuple[str, str, str]] = []
        _walk_and_rewrite(data, "", rewrites)
        if rewrites:
            # ``sort_keys=False`` preserves the source ordering so
            # diffs against the dev cache stay minimal — useful when
            # debugging "what does the shipped file look like?".
            # ``indent=2`` matches the formatting the dev app writes
            # so the on-disk shape is stable across re-runs.
            cache_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            report.updates[name] = rewrites
    return report
