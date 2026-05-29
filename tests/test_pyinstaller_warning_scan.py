"""Tests for the PyInstaller warn-file scanner and its integration
with the Windows + Linux release-build scripts.

The scanner (``scripts/_pyinstaller_warnings.py``) exists to turn
the failure mode from the Linux release v2 bug — shipping a binary
that crashes at launch with ``ModuleNotFoundError: No module named
'numpy'`` — into a hard build-time error. PyInstaller's analyser
correctly flagged the missing import in its warn file::

    missing module named numpy - imported by cvfr_routemaster.map_crop (top-level)

but the build scripts ignored the warning. These tests:

  1. Pin the parser's behaviour against the *actual* warn file the
     broken WSL build produced (saved as
     ``tests/fixtures/pyinstaller_warn/warn-with-missing-numpy.txt``),
     so a regression in the regex / quote handling / qualifier
     filtering can't slip past.

  2. Cover the filtering contracts explicitly: third-party importers
     are ignored, non-top-level qualifiers are ignored, quoted module
     names are handled, multiple-importer entries split correctly on
     commas-outside-parens.

  3. Pin both build scripts' integration: they exit with code 1 when
     the warn file flags anything, succeed when it's clean, and emit
     a warning (but don't fail) when the warn file is missing
     entirely.

  4. Belt-and-braces: confirm ``numpy`` is in both spec files'
     ``hiddenimports`` list. The scanner is the load-bearing fix;
     listing numpy in the spec is the additional defence-in-depth
     measure that survives a future build venv assembled with
     ``pip install --no-deps``.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# A real warn file the broken WSL build produced. Verbatim so any future
# PyInstaller format quirk (e.g. quoting a module name differently, adding
# a new qualifier word) is caught by the regression suite, not by a user
# reporting "the release crashed again". Trimmed to the lines that exercise
# every interesting code path; full file lives at
# build/cvfr-routemaster-linux/warn-cvfr-routemaster-linux.txt in the
# checkout that produced the bug.
# ---------------------------------------------------------------------------

# The line we care about most — the bug that motivated this whole module.
# Numpy is imported from three places: a third-party (PIL._typing) with
# (conditional, optional) qualifiers, our app at top-level, and another
# third-party (pytesseract) with (optional). Only the middle one should
# fail the build.
_REAL_NUMPY_LINE = (
    "missing module named numpy - imported by "
    "PIL._typing (conditional, optional), "
    "cvfr_routemaster.map_crop (top-level), "
    "pytesseract.pytesseract (optional)"
)

# A line that exercises quoted-module-name parsing. PyInstaller wraps
# names containing dots in single quotes.
_QUOTED_NAME_LINE = (
    "missing module named 'collections.abc' - imported by "
    "traceback (top-level), typing (top-level), PySide6.QtCore (top-level)"
)

# A line where every importer is qualified non-top-level. The scanner
# must skip the record entirely even though one of the importers is
# from our app package.
_ALL_NON_TOPLEVEL_LINE = (
    "missing module named some_optional_dep - imported by "
    "cvfr_routemaster.foo (conditional), "
    "cvfr_routemaster.bar (delayed, optional)"
)

# Only third-party importers — must be ignored even though all are
# top-level.
_THIRD_PARTY_ONLY_LINE = (
    "missing module named winreg - imported by "
    "importlib._bootstrap_external (conditional), platform (top-level)"
)

# A made-up second top-level miss from inside the app package, to
# verify multi-importer grouping works in the diagnostic.
_SECOND_APP_MISS_LINE = (
    "missing module named scipy - imported by "
    "cvfr_routemaster.altitude_arrows (top-level)"
)


_WARN_HEADER = (
    "\n"
    "This file lists modules PyInstaller was not able to find. This does not\n"
    "necessarily mean these modules are required for running your program.\n"
    "\n"
    "Types of import:\n"
    "* top-level: imported at the top-level - look at these first\n"
    "* conditional: imported within an if-statement\n"
    "* delayed: imported within a function\n"
    "* optional: imported within a try-except-statement\n"
    "\n"
)


def _write_warn_file(tmp_path: Path, *lines: str, header: bool = True) -> Path:
    """Helper: write a synthetic PyInstaller warn file containing
    ``lines`` and return its path.

    ``header`` controls whether to prepend the standard PyInstaller
    preamble; defaults to True so the test corpus looks like the real
    artefact. Tests that want to verify the parser is line-driven
    (not preamble-aware) can pass ``header=False``.
    """
    body = ("\n".join(lines)) + "\n"
    text = (_WARN_HEADER + body) if header else body
    out = tmp_path / "warn-cvfr-routemaster-linux.txt"
    out.write_text(text, encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Parser-level tests: scan_missing_top_level_imports
# ---------------------------------------------------------------------------


def test_scan_flags_numpy_top_level_import_from_app_module(tmp_path: Path) -> None:
    """The exact bug that motivated this whole module: numpy missing
    from the build venv, imported at top-level by
    ``cvfr_routemaster.map_crop``. The scanner must flag this even
    though the same warn-file line *also* lists numpy as an optional
    import from third-party libs (PIL, pytesseract)."""
    from scripts._pyinstaller_warnings import scan_missing_top_level_imports

    warn = _write_warn_file(tmp_path, _REAL_NUMPY_LINE)

    missing = scan_missing_top_level_imports(warn, "cvfr_routemaster")
    assert len(missing) == 1, (
        f"Expected exactly one flagged miss (numpy from map_crop), "
        f"got {len(missing)}: {missing}"
    )
    mi = missing[0]
    assert mi.module == "numpy"
    assert mi.importer == "cvfr_routemaster.map_crop"
    assert mi.is_top_level(), (
        f"Importer qualifiers must include 'top-level'; got: {mi.qualifiers!r}"
    )


def test_scan_ignores_third_party_top_level_imports(tmp_path: Path) -> None:
    """PIL / PySide6 / pytesseract routinely have top-level imports
    of optional deps that PyInstaller doesn't bundle by default. We
    don't ship those bug-free third parties; flagging their top-level
    misses would mean the scanner is permanently red and the user
    learns to ignore it — exactly the failure mode we're guarding
    against."""
    from scripts._pyinstaller_warnings import scan_missing_top_level_imports

    warn = _write_warn_file(tmp_path, _THIRD_PARTY_ONLY_LINE)

    missing = scan_missing_top_level_imports(warn, "cvfr_routemaster")
    assert missing == [], (
        f"Third-party top-level miss must not fail the build; "
        f"got: {missing}"
    )


def test_scan_ignores_non_top_level_imports_even_from_app_package(
    tmp_path: Path,
) -> None:
    """Imports that PyInstaller classified as ``conditional`` /
    ``optional`` / ``delayed`` (without ``top-level``) are
    runtime-guarded by the calling code — the app already handles
    the missing-dep case. The scanner skips them so the build can
    still ship optional features that depend on packages the user
    might not have."""
    from scripts._pyinstaller_warnings import scan_missing_top_level_imports

    warn = _write_warn_file(tmp_path, _ALL_NON_TOPLEVEL_LINE)

    missing = scan_missing_top_level_imports(warn, "cvfr_routemaster")
    assert missing == [], (
        f"Non-top-level imports must not fail the build; got: {missing}"
    )


def test_scan_handles_quoted_module_names(tmp_path: Path) -> None:
    """PyInstaller wraps module names containing dots in single
    quotes (e.g. ``'collections.abc'``). The parser must strip the
    quotes so the comparison against ``app_package`` prefix works,
    and the diagnostic message shows the bare name. This line has
    no app-package importer so it should produce an empty result
    set — but only if the quote-stripping doesn't crash."""
    from scripts._pyinstaller_warnings import scan_missing_top_level_imports

    warn = _write_warn_file(tmp_path, _QUOTED_NAME_LINE)

    missing = scan_missing_top_level_imports(warn, "cvfr_routemaster")
    assert missing == []


def test_scan_strips_quotes_in_flagged_module_name(tmp_path: Path) -> None:
    """Positive case for the quote-stripping: if a quoted module is
    imported top-level from the app package, the reported module
    name in the MissingImport record must be the bare unquoted form
    so the ``pip install ...`` remediation line is correct."""
    from scripts._pyinstaller_warnings import scan_missing_top_level_imports

    warn = _write_warn_file(
        tmp_path,
        # Synthetic: cvfr_routemaster.fake_module top-level-imports
        # a dotted-name dep that PyInstaller wrapped in quotes.
        "missing module named 'some.dotted.dep' - imported by "
        "cvfr_routemaster.fake_module (top-level)",
    )

    missing = scan_missing_top_level_imports(warn, "cvfr_routemaster")
    assert len(missing) == 1
    assert missing[0].module == "some.dotted.dep", (
        f"Module name must have quotes stripped for the pip-install "
        f"hint to be valid; got: {missing[0].module!r}"
    )


def test_scan_picks_up_multiple_top_level_misses_from_app_package(
    tmp_path: Path,
) -> None:
    """If multiple distinct deps are missing from the app package
    (e.g. someone wipes the build venv and rebuilds), the scanner
    must report all of them rather than stopping at the first.
    Important so the user can ``pip install A B C`` once instead
    of playing whack-a-mole across rebuilds."""
    from scripts._pyinstaller_warnings import scan_missing_top_level_imports

    warn = _write_warn_file(
        tmp_path,
        _REAL_NUMPY_LINE,
        _SECOND_APP_MISS_LINE,
    )

    missing = scan_missing_top_level_imports(warn, "cvfr_routemaster")
    modules = sorted({mi.module for mi in missing})
    assert modules == ["numpy", "scipy"]


def test_scan_returns_empty_list_when_warn_file_missing(tmp_path: Path) -> None:
    """If PyInstaller didn't write a warn file (e.g. the build never
    ran, or it ran with --clean and the warn-file write was
    interrupted), the scanner returns an empty list — the build
    script's caller emits a warning and continues. We can't flag
    failures we can't see."""
    from scripts._pyinstaller_warnings import scan_missing_top_level_imports

    nonexistent = tmp_path / "definitely-not-here.txt"
    assert not nonexistent.exists()

    missing = scan_missing_top_level_imports(nonexistent, "cvfr_routemaster")
    assert missing == []


def test_scan_returns_empty_list_when_no_app_top_level_misses(
    tmp_path: Path,
) -> None:
    """The happy path: warn file exists, contains the usual
    PyInstaller noise (Windows-only modules missing on Linux, etc.),
    but nothing top-level from our app package. The scanner must
    return an empty list so the build proceeds."""
    from scripts._pyinstaller_warnings import scan_missing_top_level_imports

    warn = _write_warn_file(
        tmp_path,
        # All of these are noise — none are top-level + cvfr_routemaster.
        _THIRD_PARTY_ONLY_LINE,
        _ALL_NON_TOPLEVEL_LINE,
        _QUOTED_NAME_LINE,
        # And a stock PyInstaller-noise line (windows-only modules on Linux).
        "missing module named msvcrt - imported by subprocess (optional)",
    )

    missing = scan_missing_top_level_imports(warn, "cvfr_routemaster")
    assert missing == []


def test_scan_uses_exact_package_prefix_not_substring_match(
    tmp_path: Path,
) -> None:
    """A package called ``cvfr_routemaster_other`` should NOT be
    treated as part of ``cvfr_routemaster``. Without this guard a
    future sibling package (test fixtures, vendored fork, etc.)
    would incorrectly fail the build for missing deps that aren't
    actually in our package."""
    from scripts._pyinstaller_warnings import scan_missing_top_level_imports

    warn = _write_warn_file(
        tmp_path,
        # A "sibling" package whose name shares a prefix with ours.
        "missing module named numpy - imported by "
        "cvfr_routemaster_other.foo (top-level)",
    )

    missing = scan_missing_top_level_imports(warn, "cvfr_routemaster")
    assert missing == [], (
        f"Substring-match would incorrectly flag this; got: {missing}"
    )


def test_scan_ignores_stdlib_module_misses_even_from_app_package(
    tmp_path: Path,
) -> None:
    """PyInstaller's static analyser on Python 3.13 routinely flags
    ``'collections.abc'`` as "missing" against ~70 importers — every
    stdlib module that does ``from collections.abc import …``, plus
    Qt/PIL/numpy, plus any of our own modules that do the same. But
    the frozen binary always bundles the running interpreter's stdlib
    (PyInstaller has zero choice on that), so a stdlib "missing module"
    warning can never become a runtime ``ImportError`` — it's a false
    positive of the analyser, not a real ship-time risk.

    This was the trigger that broke the Linux build the day
    ``cvfr_routemaster/route.py`` grew ``from collections.abc import
    Iterable`` for a type annotation: before that line existed, every
    importer of ``collections.abc`` on the warn-file line was a third
    party and got filtered by the ``app_package`` gate; the moment an
    app-package importer joined the list, the scanner started failing
    the build over an unfixable warning (you can't ``pip install
    collections.abc`` — it's stdlib). The stdlib filter is the right
    load-bearing fix; route.py was just the canary.

    This test pins the filter with the exact line PyInstaller produced
    on the failing build so a future refactor of the filter set can't
    re-open the gap.
    """
    from scripts._pyinstaller_warnings import scan_missing_top_level_imports

    warn = _write_warn_file(
        tmp_path,
        # The actual line, trimmed to the interesting entries:
        # stdlib importers (traceback, typing), a Qt importer, and
        # — critically — an app-package top-level importer.
        "missing module named 'collections.abc' - imported by "
        "traceback (top-level), typing (top-level), "
        "PySide6.QtCore (top-level), "
        "cvfr_routemaster.route (top-level)",
    )

    missing = scan_missing_top_level_imports(warn, "cvfr_routemaster")
    assert missing == [], (
        f"Stdlib misses must be filtered regardless of importer; got: {missing}"
    )


def test_scan_ignores_stdlib_dotted_submodule_via_top_level_package_check(
    tmp_path: Path,
) -> None:
    """``sys.stdlib_module_names`` lists top-level package names only
    (e.g. ``"collections"``, ``"importlib"``, ``"xml"``). The filter
    must match dotted submodule references like
    ``'importlib.resources'`` and ``'xml.etree.ElementTree'`` by
    splitting on the first dot — otherwise this would only catch
    bare-top-level stdlib names and miss the dotted submodules that
    PyInstaller actually wraps in quotes."""
    from scripts._pyinstaller_warnings import scan_missing_top_level_imports

    warn = _write_warn_file(
        tmp_path,
        "missing module named 'importlib.resources' - imported by "
        "cvfr_routemaster.route (top-level)",
        "missing module named 'xml.etree.ElementTree' - imported by "
        "cvfr_routemaster.altitude_arrows (top-level)",
    )

    missing = scan_missing_top_level_imports(warn, "cvfr_routemaster")
    assert missing == [], (
        f"Dotted stdlib submodules must be filtered via top-level "
        f"package name; got: {missing}"
    )


def test_scan_still_flags_non_stdlib_top_level_miss_when_stdlib_filter_active(
    tmp_path: Path,
) -> None:
    """The stdlib filter must NOT swallow real misses. Belt-and-braces
    paranoia for the load-bearing case: a warn file mixing a stdlib
    false-positive line and a real numpy miss must still flag numpy."""
    from scripts._pyinstaller_warnings import scan_missing_top_level_imports

    warn = _write_warn_file(
        tmp_path,
        # False positive — stdlib, must be filtered.
        "missing module named 'collections.abc' - imported by "
        "cvfr_routemaster.route (top-level)",
        # Real miss — third-party pip-installable, must be flagged.
        _REAL_NUMPY_LINE,
    )

    missing = scan_missing_top_level_imports(warn, "cvfr_routemaster")
    modules = sorted({mi.module for mi in missing})
    assert modules == ["numpy"], (
        f"Stdlib filter must not swallow real misses; got: {modules}"
    )


def test_scan_matches_app_package_itself_not_just_descendants(
    tmp_path: Path,
) -> None:
    """If ``cvfr_routemaster/__init__.py`` ever has a top-level
    import of a missing module, the scanner must catch it even
    though the importer is *exactly* ``cvfr_routemaster`` (no
    dotted suffix). Otherwise a future bug where someone adds
    ``import some_dep`` to ``__init__.py`` without installing
    ``some_dep`` in the build venv would silently ship a broken
    binary."""
    from scripts._pyinstaller_warnings import scan_missing_top_level_imports

    warn = _write_warn_file(
        tmp_path,
        "missing module named some_dep - imported by "
        "cvfr_routemaster (top-level)",
    )

    missing = scan_missing_top_level_imports(warn, "cvfr_routemaster")
    assert len(missing) == 1
    assert missing[0].importer == "cvfr_routemaster"


# ---------------------------------------------------------------------------
# Formatter tests
# ---------------------------------------------------------------------------


def test_format_missing_imports_message_includes_pip_install_command() -> None:
    """The diagnostic message has to give the operator a concrete
    next action. The most important line is the
    ``pip install <missing modules>`` command — without it the user
    has to figure out which packages to install from the per-module
    bullets above. Pin the exact format so future copy edits don't
    drop the actionable bit."""
    from scripts._pyinstaller_warnings import (
        MissingImport,
        format_missing_imports_message,
    )

    msg = format_missing_imports_message([
        MissingImport(
            module="numpy",
            importer="cvfr_routemaster.map_crop",
            qualifiers="top-level",
        ),
    ])

    assert "pip install numpy" in msg, (
        f"Diagnostic must include the exact pip-install command; got:\n{msg}"
    )
    # The bullet should also mention the importer so the user can
    # confirm the detection is real before installing anything.
    assert "cvfr_routemaster.map_crop" in msg


def test_format_message_groups_multiple_importers_under_one_module() -> None:
    """If numpy is imported top-level from two different app modules,
    the message should have ONE bullet per missing module with both
    importers listed underneath — not two duplicated bullets that
    push the user toward thinking there are two distinct problems."""
    from scripts._pyinstaller_warnings import (
        MissingImport,
        format_missing_imports_message,
    )

    msg = format_missing_imports_message([
        MissingImport(
            module="numpy",
            importer="cvfr_routemaster.map_crop",
            qualifiers="top-level",
        ),
        MissingImport(
            module="numpy",
            importer="cvfr_routemaster.altitude_arrows",
            qualifiers="top-level",
        ),
    ])

    # Exactly one ``- missing: numpy`` bullet across the whole message.
    assert msg.count("- missing: numpy") == 1, (
        f"Multiple importers for one module should produce one bullet; got:\n{msg}"
    )
    # Both importers must be listed though.
    assert "cvfr_routemaster.map_crop" in msg
    assert "cvfr_routemaster.altitude_arrows" in msg
    # And the pip-install line should list numpy once, not twice.
    assert msg.count("pip install numpy") == 1


def test_format_message_lists_multiple_missing_modules_sorted() -> None:
    """Two different missing modules → two bullets, and the pip
    install line lists both. Sorted for stability so the message
    doesn't change between runs based on dict iteration order."""
    from scripts._pyinstaller_warnings import (
        MissingImport,
        format_missing_imports_message,
    )

    msg = format_missing_imports_message([
        MissingImport(
            module="scipy",
            importer="cvfr_routemaster.altitude_arrows",
            qualifiers="top-level",
        ),
        MissingImport(
            module="numpy",
            importer="cvfr_routemaster.map_crop",
            qualifiers="top-level",
        ),
    ])

    assert "- missing: numpy" in msg
    assert "- missing: scipy" in msg
    # Both modules in the pip-install line, sorted (numpy < scipy).
    assert "pip install numpy scipy" in msg


def test_format_message_raises_on_empty_input() -> None:
    """The caller is expected to branch on whether
    scan_missing_top_level_imports returned anything before formatting
    a message. If they accidentally format an empty list we want to
    fail loudly rather than emit a misleading "ERROR" header with no
    bullets underneath."""
    from scripts._pyinstaller_warnings import format_missing_imports_message

    with pytest.raises(ValueError, match="empty"):
        format_missing_imports_message([])


# ---------------------------------------------------------------------------
# Build-script integration: Linux
# ---------------------------------------------------------------------------


def test_linux_scan_step_exits_nonzero_when_warn_file_flags_app_top_level(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The actual integration test: the Linux build script's
    ``_scan_pyinstaller_warnings`` must call ``sys.exit(1)`` and
    print the diagnostic to stderr when the warn file lists
    top-level misses from our app package.

    This is the exact code path that would have caught the v2 bug
    at build time."""
    from scripts import build_release_for_linux

    fake_repo = tmp_path / "repo"
    fake_build = fake_repo / "build" / build_release_for_linux.SPEC_STEM
    fake_build.mkdir(parents=True)
    warn = fake_build / f"warn-{build_release_for_linux.SPEC_STEM}.txt"
    warn.write_text(_WARN_HEADER + _REAL_NUMPY_LINE + "\n", encoding="utf-8")

    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", fake_repo)
    monkeypatch.setattr(build_release_for_linux, "BUILD_DIR", fake_repo / "build")

    with pytest.raises(SystemExit) as exc:
        build_release_for_linux._scan_pyinstaller_warnings()
    assert exc.value.code == 1

    err = capsys.readouterr().err
    assert "numpy" in err
    assert "cvfr_routemaster.map_crop" in err
    assert "pip install numpy" in err, (
        "Operator-facing remediation hint must be in stderr — "
        "without it the user knows the build failed but not how to fix it"
    )


def test_linux_scan_step_succeeds_when_warn_file_is_clean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Happy path: warn file exists, contains only PyInstaller's
    usual cross-platform / optional-dep noise, no top-level misses
    from our app. The scan must NOT exit, and should print a
    positive confirmation so the build log clearly shows the check
    ran (vs. silently passing — that would let a future bug skip
    the scan and look identical from the logs)."""
    from scripts import build_release_for_linux

    fake_repo = tmp_path / "repo"
    fake_build = fake_repo / "build" / build_release_for_linux.SPEC_STEM
    fake_build.mkdir(parents=True)
    warn = fake_build / f"warn-{build_release_for_linux.SPEC_STEM}.txt"
    warn.write_text(
        _WARN_HEADER
        + _THIRD_PARTY_ONLY_LINE + "\n"
        + _ALL_NON_TOPLEVEL_LINE + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", fake_repo)
    monkeypatch.setattr(build_release_for_linux, "BUILD_DIR", fake_repo / "build")

    # Must not raise.
    build_release_for_linux._scan_pyinstaller_warnings()

    out = capsys.readouterr().out
    assert "no unresolved top-level imports" in out


def test_linux_scan_step_warns_but_does_not_fail_when_warn_file_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If PyInstaller somehow didn't write a warn file (interrupted
    build, weird CI environment, etc.), the scanner can't verify
    anything — but it shouldn't fail the build either. Surface
    the gap as a stderr WARNING and continue; the binary may well
    be fine, we just couldn't confirm."""
    from scripts import build_release_for_linux

    fake_repo = tmp_path / "repo"
    (fake_repo / "build" / build_release_for_linux.SPEC_STEM).mkdir(parents=True)
    # No warn file written.

    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", fake_repo)
    monkeypatch.setattr(build_release_for_linux, "BUILD_DIR", fake_repo / "build")

    # Must not raise.
    build_release_for_linux._scan_pyinstaller_warnings()

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "warn file" in err.lower()


# ---------------------------------------------------------------------------
# Build-script integration: Windows
# ---------------------------------------------------------------------------


def test_windows_scan_step_exits_nonzero_when_warn_file_flags_app_top_level(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mirror of the Linux integration test, exercised through
    ``scripts/build_release.py`` (Windows). The Windows release has
    historically shipped numpy because the dev Python install
    happens to have it as a transitive dep of something else — but
    that's luck. If that luck ever runs out the Windows release
    needs the same protection as Linux."""
    from scripts import build_release

    fake_repo = tmp_path / "repo"
    fake_build = fake_repo / "build" / build_release.SPEC_STEM
    fake_build.mkdir(parents=True)
    warn = fake_build / f"warn-{build_release.SPEC_STEM}.txt"
    warn.write_text(_WARN_HEADER + _REAL_NUMPY_LINE + "\n", encoding="utf-8")

    monkeypatch.setattr(build_release, "REPO_ROOT", fake_repo)
    monkeypatch.setattr(build_release, "BUILD_DIR", fake_repo / "build")

    with pytest.raises(SystemExit) as exc:
        build_release._scan_pyinstaller_warnings()
    assert exc.value.code == 1

    err = capsys.readouterr().err
    assert "numpy" in err
    assert "pip install" in err


def test_windows_scan_step_succeeds_when_warn_file_is_clean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mirror of the Linux happy-path test for the Windows script."""
    from scripts import build_release

    fake_repo = tmp_path / "repo"
    fake_build = fake_repo / "build" / build_release.SPEC_STEM
    fake_build.mkdir(parents=True)
    warn = fake_build / f"warn-{build_release.SPEC_STEM}.txt"
    warn.write_text(_WARN_HEADER + _THIRD_PARTY_ONLY_LINE + "\n", encoding="utf-8")

    monkeypatch.setattr(build_release, "REPO_ROOT", fake_repo)
    monkeypatch.setattr(build_release, "BUILD_DIR", fake_repo / "build")

    build_release._scan_pyinstaller_warnings()

    out = capsys.readouterr().out
    assert "no unresolved top-level imports" in out


# ---------------------------------------------------------------------------
# Defence-in-depth: numpy in hiddenimports
# ---------------------------------------------------------------------------


def test_numpy_in_windows_spec_hiddenimports() -> None:
    """Belt-and-braces: numpy must be in the Windows spec's
    ``hiddenimports`` list. The warn-file scanner is the load-bearing
    fix (it catches missing deps regardless of which deps), but
    listing numpy explicitly survives a build venv that was
    deliberately assembled with ``pip install --no-deps`` — a
    legitimate workflow for reproducible CI builds — where
    PyInstaller's auto-discovery wouldn't pick it up.
    """
    spec_path = Path(__file__).resolve().parents[1] / "cvfr-routemaster.spec"
    text = spec_path.read_text(encoding="utf-8")
    assert "'numpy'" in text, (
        "numpy must appear (with single quotes) in cvfr-routemaster.spec's "
        "hiddenimports list as a defensive listing — see the comment in "
        "the spec file for the full rationale."
    )


def test_numpy_in_linux_spec_hiddenimports() -> None:
    """Mirror of the Windows-spec check for the Linux spec.

    Especially important here because the Linux release v2 *was*
    the bug — Windows has historically shipped numpy by luck (dev
    box has it transitively), but the Linux WSL build venv was
    assembled with an explicit ``pip install`` list that didn't
    include numpy, and that's exactly the failure mode this guards
    against on rebuilds."""
    spec_path = (
        Path(__file__).resolve().parents[1] / "cvfr-routemaster-linux.spec"
    )
    text = spec_path.read_text(encoding="utf-8")
    assert "'numpy'" in text, (
        "numpy must appear (with single quotes) in cvfr-routemaster-linux.spec's "
        "hiddenimports list as a defensive listing — see the comment in "
        "the spec file for the full rationale."
    )


def test_numpy_pinned_in_runtime_requirements() -> None:
    """numpy is a top-level import in ``cvfr_routemaster.map_crop``,
    making it a *runtime* dependency (not test-only / build-only).
    It must live in ``requirements.txt``, not just
    ``requirements-dev.txt`` — the latter wouldn't be installed in
    a downstream environment that does ``pip install -r requirements.txt``.

    This was the original bug: the WSL build venv was set up with
    only the packages explicitly listed (no requirements.txt at all
    at that point), and numpy wasn't on the list. Pinning it in
    requirements.txt closes the loop so a clean install always
    drags numpy in.
    """
    req_path = Path(__file__).resolve().parents[1] / "requirements.txt"
    text = req_path.read_text(encoding="utf-8")
    # Match ``numpy`` at line start, ignoring case for safety
    # (PyPI lookup is case-insensitive on the package name).
    has_numpy = any(
        line.strip().lower().startswith("numpy")
        for line in text.splitlines()
    )
    assert has_numpy, (
        f"numpy must be pinned in requirements.txt as a runtime dep "
        f"(it's a top-level import in cvfr_routemaster.map_crop); "
        f"current contents:\n{text}"
    )
