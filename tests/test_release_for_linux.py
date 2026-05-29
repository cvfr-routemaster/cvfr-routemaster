"""Tests for the Linux release pipeline.

The Linux release is built by ``scripts/build_release_for_linux.py``
running on a Linux host (PyInstaller is platform-specific — it can't
cross-compile a Linux ELF from Windows). These tests therefore cover
the *script logic* on whichever platform the test suite happens to
run on, by monkeypatching ``sys.platform`` and the build script's
constants. The actual PyInstaller invocation is not exercised here;
that's a one-shot manual smoke-test on the target Debian box.

What's covered:

- ``_tesseract_missing_message()`` returns platform-appropriate
  install hints (Windows vs Linux/POSIX).
- ``_check_prerequisites()`` refuses to run on non-Linux hosts so a
  Windows-machine "build" doesn't silently produce a Windows .exe
  with the wrong filename / spec.
- The Desktop Entry template + installer script are well-formed,
  contain the expected placeholders, and the installer script ends
  up executable.
- The friend-facing README mentions the exact ``apt install``
  command users need (no Tesseract is bundled — the README is the
  contract).
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Tesseract "not found" error message: platform-aware
# ---------------------------------------------------------------------------


def test_tesseract_missing_message_on_windows_points_at_bundled_release_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows users get the bundled-Tesseract instructions because
    the Windows release ships ``release/tesseract/tesseract.exe``
    next to the .exe — telling them to ``apt install`` would make
    no sense."""
    from cvfr_routemaster.back_page_ocr import _tesseract_missing_message

    monkeypatch.setattr(sys, "platform", "win32")
    msg = _tesseract_missing_message()

    assert "tesseract.exe" in msg
    assert "release zip" in msg or "release" in msg
    assert "fetch_vendor_tesseract.py" in msg, (
        "Windows users in dev mode without vendor/tesseract/ need the "
        "fetch script hint to populate it."
    )
    # No accidental Linux hint — that would be confusing on Windows.
    assert "apt install" not in msg


def test_tesseract_missing_message_on_linux_points_at_apt_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux users get the apt install command verbatim. The Linux
    release deliberately doesn't bundle Tesseract (see the
    build_release_for_linux.py module docstring for why), so this
    message IS the install instructions for the OCR path."""
    from cvfr_routemaster.back_page_ocr import _tesseract_missing_message

    monkeypatch.setattr(sys, "platform", "linux")
    msg = _tesseract_missing_message()

    assert "sudo apt install" in msg
    assert "tesseract-ocr" in msg
    assert "tesseract-ocr-eng" in msg
    assert "tesseract-ocr-heb" in msg, (
        "Hebrew traineddata is the whole reason we need Tesseract — "
        "the install hint must explicitly include the heb package "
        "or users will install the base tesseract-ocr and still "
        "hit 'language not available' errors."
    )
    # No accidental Windows hint.
    assert "tesseract.exe" not in msg
    assert "fetch_vendor_tesseract.py" not in msg


def test_tesseract_missing_message_on_macos_uses_posix_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS isn't a target platform for the release pipeline, but
    the dev-mode fallback should still produce a sensible message
    rather than the Windows one. Our POSIX branch covers both Linux
    and Darwin — the apt hint won't help a macOS dev directly but
    it's a clearer signpost than telling them about
    ``vendor/tesseract/tesseract.exe`` which doesn't exist on POSIX."""
    from cvfr_routemaster.back_page_ocr import _tesseract_missing_message

    monkeypatch.setattr(sys, "platform", "darwin")
    msg = _tesseract_missing_message()

    assert "tesseract.exe" not in msg
    # The POSIX branch falls through to the same apt suggestion;
    # macOS users have to translate to brew install themselves but
    # at least they aren't sent looking for a .exe.
    assert "apt install" in msg or "tesseract" in msg


# ---------------------------------------------------------------------------
# Build-script prereq check: must refuse to run off-Linux
# ---------------------------------------------------------------------------


def test_check_prerequisites_refuses_non_linux_host(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running the Linux build script on Windows would silently
    produce a Windows .exe (PyInstaller picks the host platform's
    output format regardless of which spec we pass — the spec only
    controls hidden imports / icon / data files, not the target
    OS). Bail with a clear message instead.

    Equally important: the test suite itself runs on Windows, so
    without this guard a stray test invoking ``main()`` would
    actually start a PyInstaller build."""
    from scripts import build_release_for_linux

    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SystemExit) as exc:
        build_release_for_linux._check_prerequisites()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "must run on Linux" in err
    assert "win32" in err
    assert "WSL" in err or "Debian" in err


def test_check_prerequisites_passes_on_linux_with_required_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The positive case: when running on Linux and all chart PDFs
    + calibration JSON exist in the repo root, the prereq check
    succeeds quietly. This is the path a real build takes — without
    it we'd never know if a future tightening of the prereq list
    accidentally locked out a valid environment."""
    from scripts import build_release_for_linux

    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    (fake_repo / ".cvfr_routemaster").mkdir()
    (fake_repo / ".cvfr_routemaster" / "geo_calibration.json").write_text("{}")
    for pdf in build_release_for_linux.CHART_PDFS:
        (fake_repo / pdf).write_bytes(b"%PDF-1.4 stub\n")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", fake_repo)
    monkeypatch.setattr(
        build_release_for_linux, "DEV_CACHE_DIR", fake_repo / ".cvfr_routemaster"
    )

    build_release_for_linux._check_prerequisites()
    out = capsys.readouterr().out
    assert "All prerequisites present" in out


# ---------------------------------------------------------------------------
# Desktop Entry template + installer script
# ---------------------------------------------------------------------------


def test_write_desktop_entry_template_produces_well_formed_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The template + installer script are the entire 'desktop menu
    integration' UX. Test that:
      - both files actually get written,
      - the .desktop file contains the placeholder the installer
        script substitutes (regression guard against renaming
        ``${INSTALL_DIR}`` in one file but not the other),
      - the installer script is marked executable (the user expects
        ``./install-shortcut.sh`` to just work),
      - core Desktop Entry fields are present so the launcher
        actually appears in menus and uses the right icon.
    """
    from scripts import build_release_for_linux

    # ``_write_desktop_entry_template`` prints relative-to-REPO_ROOT
    # paths in its progress output; with RELEASE_DIR moved into
    # tmp_path that ``relative_to`` would raise. Move REPO_ROOT
    # alongside it so the relative-path arithmetic stays valid.
    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_release_for_linux, "RELEASE_DIR", tmp_path / "rel")
    (tmp_path / "rel").mkdir()

    build_release_for_linux._write_desktop_entry_template()

    desktop = tmp_path / "rel" / "cvfr-routemaster.desktop"
    installer = tmp_path / "rel" / "install-shortcut.sh"

    assert desktop.is_file()
    assert installer.is_file()

    desk_text = desktop.read_text(encoding="utf-8")
    # Standard XDG Desktop Entry section header — without this the
    # entry is rejected silently by every desktop environment.
    assert "[Desktop Entry]" in desk_text
    assert "Type=Application" in desk_text
    assert "Name=CVFR Route Master" in desk_text
    assert "Exec=${INSTALL_DIR}/cvfr-routemaster" in desk_text
    assert "Icon=${INSTALL_DIR}/icon.png" in desk_text
    # Placeholder must be present in the EXACT form the installer
    # script substitutes for, otherwise a future rename would silently
    # leave an unsubstituted ``${INSTALL_DIR}`` in the deployed
    # .desktop and the launcher would point at a non-existent path.
    assert "${INSTALL_DIR}" in desk_text

    inst_text = installer.read_text(encoding="utf-8")
    assert inst_text.startswith("#!/bin/sh")
    assert "${INSTALL_DIR}" in inst_text  # the placeholder it substitutes
    assert "$HOME/.local/share/applications" in inst_text
    assert "cvfr-routemaster.desktop" in inst_text

    # +x for the installer — without it the user has to chmod first
    # which contradicts the README's "just run it" promise.
    mode = installer.stat().st_mode
    # Skip the executable-bit assertion on Windows because NTFS doesn't
    # carry POSIX permission bits and the test would always fail there;
    # the chmod call still ran (and is what matters on Linux).
    if sys.platform != "win32":
        assert mode & stat.S_IXUSR, "install-shortcut.sh must be executable for the user"


# ---------------------------------------------------------------------------
# README content — it's the contract for the apt install requirement
# ---------------------------------------------------------------------------


def test_write_readme_documents_tesseract_apt_install_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The Linux release relies on a one-time
    ``sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-heb``
    on the target box for the OCR path. The README is the single
    place the user finds out about it BEFORE first launch — the
    in-app Qt dialog only fires AFTER an OCR-triggering action.

    Pin the exact command form: missing the ``-eng`` or ``-heb``
    suffix would leave the user with a tesseract that can't read
    the back-pages PDF, which manifests as a confusing
    "language not available" error instead of "Tesseract not found".
    """
    from scripts import build_release_for_linux

    # See _write_desktop_entry_template test for why REPO_ROOT also moves.
    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_release_for_linux, "RELEASE_DIR", tmp_path / "rel")
    (tmp_path / "rel").mkdir()

    build_release_for_linux._write_readme()

    text = (tmp_path / "rel" / "README.txt").read_text(encoding="utf-8")
    # The exact command form (one line, three packages) appears in the
    # "Optional" section. Use ``in`` so reformatting whitespace later
    # doesn't break the test.
    assert "tesseract-ocr tesseract-ocr-eng tesseract-ocr-heb" in text
    assert "sudo apt install" in text
    # The README also needs to surface the install-shortcut.sh path
    # because it's optional (no install creates one automatically).
    assert "install-shortcut.sh" in text
    # And the chmod +x recovery hint for the case where transfer
    # stripped the executable bit (common with FAT32 USB sticks /
    # some zip extractors).
    assert "chmod +x cvfr-routemaster" in text


def test_write_readme_documents_complete_qt_runtime_apt_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The README's Qt runtime apt install command must enumerate
    every package the binary needs at startup. Without this list
    a Debian box that lacks (e.g.) ``libxcb-cursor0`` exits silently
    with no visible window and the user has nothing to debug from.

    The list comes from running ``ldd`` on the bundled platform
    plugin (``libqxcb.so``) and mapping each "not found" .so back
    to the providing apt package — see the comment block above
    ``RUNTIME_QT_APT_PACKAGES`` in the build script.
    """
    from scripts import build_release_for_linux

    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_release_for_linux, "RELEASE_DIR", tmp_path / "rel")
    (tmp_path / "rel").mkdir()

    build_release_for_linux._write_readme()

    text = (tmp_path / "rel" / "README.txt").read_text(encoding="utf-8")
    # Every Qt runtime package must appear in the README literally.
    # Spot-check the load-bearing ones (they're the most common cause of
    # silent-failure-at-launch) and assert the full list with a loop so
    # a future addition to RUNTIME_QT_APT_PACKAGES can't slip past.
    assert "libxcb-cursor0" in text, (
        "libxcb-cursor0 is the most common silent-failure cause on "
        "Debian 13 — README must call it out explicitly"
    )
    for pkg in build_release_for_linux.RUNTIME_QT_APT_PACKAGES:
        assert pkg in text, f"README missing required Qt runtime apt package: {pkg}"


def test_write_readme_lists_every_top_level_artefact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sanity check that the "What's in this folder" section mentions
    every file/dir the build script actually drops. Catches silent
    drift between "build script ships X" and "README claims to ship
    X" — neither is more authoritative than the other but them
    diverging is a UX bug."""
    from scripts import build_release_for_linux

    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_release_for_linux, "RELEASE_DIR", tmp_path / "rel")
    (tmp_path / "rel").mkdir()

    build_release_for_linux._write_readme()

    text = (tmp_path / "rel" / "README.txt").read_text(encoding="utf-8")
    # v3.3+ no longer ships ``map-pdfs/`` (the charts are downloaded
    # at runtime from CAAI URLs — see ``cvfr_routemaster.chart_source``).
    # The README must mention every artefact that DOES ship.
    for artefact in (
        "cvfr-routemaster",
        "icon.png",
        "cvfr-routemaster.desktop",
        "install-shortcut.sh",
        "check-runtime-deps.sh",
        ".cvfr_routemaster/",
    ):
        assert artefact in text, f"README doesn't mention shipped artefact: {artefact}"
    # The README must also explain the new first-launch download
    # flow so users aren't surprised by the progress dialog.
    assert "download" in text.lower(), (
        "README must explain the first-launch download flow"
    )
    assert "Map File Settings" in text, (
        "README must point users to Map File Settings for URL fallback"
    )


# ---------------------------------------------------------------------------
# check-runtime-deps.sh — the helper users run on the target box
# ---------------------------------------------------------------------------


def test_write_check_runtime_deps_script_produces_executable_posix_sh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The runtime-deps check script is the user's first defense
    against silent-failure-at-launch caused by missing Qt libs
    (most commonly libxcb-cursor0 on Debian 13). It must:

      - exist at ``release-linux/check-runtime-deps.sh``
      - be a POSIX sh script (not bash) so it works on every
        Debian-derivative without depending on /bin/bash being
        present
      - have the executable bit set (the README tells users to
        ``./check-runtime-deps.sh`` directly without ``sh ...``)
    """
    from scripts import build_release_for_linux

    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_release_for_linux, "RELEASE_DIR", tmp_path / "rel")
    (tmp_path / "rel").mkdir()

    build_release_for_linux._write_check_runtime_deps_script()

    out = tmp_path / "rel" / "check-runtime-deps.sh"
    assert out.is_file(), "check-runtime-deps.sh wasn't written"

    text = out.read_text(encoding="utf-8")
    # POSIX sh shebang, NOT bash — bash is not guaranteed on minimal
    # Debian installs (busybox-based containers, debootstrap setups).
    assert text.startswith("#!/bin/sh\n"), (
        f"check-runtime-deps.sh should start with '#!/bin/sh', got: "
        f"{text.splitlines()[0] if text else '(empty)'}"
    )

    # +x bit on POSIX. Skip on Windows where NTFS doesn't carry the
    # bit; the chmod() call still ran and is what matters on Linux.
    if sys.platform != "win32":
        assert out.stat().st_mode & stat.S_IXUSR, (
            "check-runtime-deps.sh must be executable for the user — "
            "the README says ``./check-runtime-deps.sh`` not ``sh ./...``"
        )


def test_check_runtime_deps_script_probes_every_qt_runtime_lib(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The check script's probe list must cover every package in
    RUNTIME_QT_APT_PACKAGES — otherwise a missing lib silently
    slips past the check and the user still hits "binary exits
    with no window" at launch. This regression-guards the case
    where someone adds a new package to RUNTIME_QT_APT_PACKAGES
    but forgets to add a probe for it.

    DejaVu fonts is the one exception (it's a font package, not a
    shared library, so it's checked via dpkg-query — but it must
    still appear in the script).
    """
    from scripts import build_release_for_linux

    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_release_for_linux, "RELEASE_DIR", tmp_path / "rel")
    (tmp_path / "rel").mkdir()

    build_release_for_linux._write_check_runtime_deps_script()
    text = (tmp_path / "rel" / "check-runtime-deps.sh").read_text(encoding="utf-8")

    # Every probed library + its providing package must appear in
    # the script. The check_lib invocation references both, so
    # presence in the script implies the probe is wired up.
    for pkg, soname in build_release_for_linux.RUNTIME_LIB_PROBES:
        assert pkg in text, f"check script missing apt package: {pkg}"
        assert soname in text, f"check script missing soname probe for: {soname}"

    # Font package is special-cased (not in RUNTIME_LIB_PROBES because
    # it's not a shared lib) but must still be checked.
    assert "fonts-dejavu-core" in text
    assert "dpkg-query" in text, (
        "Font check uses dpkg-query (no soname to probe via ldconfig)"
    )


def test_check_runtime_deps_script_probes_tesseract_languages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Tesseract is the optional / second tier dep. The check script
    must probe BOTH the binary itself and the Hebrew + English
    language packs separately, because the partial-install case
    (binary present, ``-heb`` package missing) is the most common
    Tesseract failure mode for this app — back-pages text is
    Hebrew and Tesseract returns garbage rather than an error
    when the language data isn't available."""
    from scripts import build_release_for_linux

    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_release_for_linux, "RELEASE_DIR", tmp_path / "rel")
    (tmp_path / "rel").mkdir()

    build_release_for_linux._write_check_runtime_deps_script()
    text = (tmp_path / "rel" / "check-runtime-deps.sh").read_text(encoding="utf-8")

    assert "command -v tesseract" in text, (
        "Probe for Tesseract binary itself"
    )
    assert "tesseract --list-langs" in text, (
        "Must enumerate installed languages, not just check the binary"
    )
    assert "'heb'" in text or "\"heb\"" in text or " heb " in text, (
        "Must check for the Hebrew language pack specifically"
    )
    assert "'eng'" in text or "\"eng\"" in text or " eng " in text, (
        "Must check for the English language pack as well"
    )

    # The remediation hint for the no-tesseract case must spell out
    # all three apt packages (otherwise the user installs the base
    # package and still hits the same OCR failure later).
    for pkg in build_release_for_linux.RUNTIME_OCR_APT_PACKAGES:
        assert pkg in text, f"check script missing OCR apt package hint: {pkg}"


def test_check_runtime_deps_script_uses_distinct_exit_codes_for_qt_vs_ocr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exit code 0 = all good, 1 = Qt missing (binary won't start at
    all), 2 = OCR missing (degraded, only blocks chart-cycle re-OCR).
    The distinction matters because a CI / wrapper script can decide
    whether to refuse-launch (exit 1) or warn-but-launch (exit 2).

    Pin the exit-code numbers literally so any future "let's just
    use exit 1 for everything" cleanup gets pushed back here.
    """
    from scripts import build_release_for_linux

    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_release_for_linux, "RELEASE_DIR", tmp_path / "rel")
    (tmp_path / "rel").mkdir()

    build_release_for_linux._write_check_runtime_deps_script()
    text = (tmp_path / "rel" / "check-runtime-deps.sh").read_text(encoding="utf-8")

    # The script body sets RC=1 on Qt-missing path and RC=2 on
    # OCR-only-missing path. Both literal ``RC=1`` and ``RC=2``
    # must appear (along with the standard ``exit 0`` happy path
    # and the final ``exit $RC``).
    assert "RC=1" in text, "Qt-missing case must use exit code 1"
    assert "RC=2" in text or "RC=0 ] && RC=2" in text, (
        "OCR-only-missing case must use exit code 2"
    )
    assert "exit 0" in text, "Happy-path exit must be 0"
    assert "exit $RC" in text, "Final exit must propagate RC"


def test_runtime_lib_probes_aligned_with_qt_apt_packages() -> None:
    """RUNTIME_LIB_PROBES is the source of truth for the check
    script's per-lib probes; RUNTIME_QT_APT_PACKAGES is the source
    of truth for the README's apt install command. They must agree
    on which packages are required (modulo the DejaVu font package
    which is in QT_APT_PACKAGES but not LIB_PROBES because it's not
    a shared library).
    """
    from scripts import build_release_for_linux

    probe_pkgs = {pkg for pkg, _soname in build_release_for_linux.RUNTIME_LIB_PROBES}
    apt_pkgs = set(build_release_for_linux.RUNTIME_QT_APT_PACKAGES)

    # DejaVu fonts is the only package allowed to be in apt_pkgs
    # without a corresponding lib probe (it's a font set, no soname).
    apt_minus_fonts = apt_pkgs - {"fonts-dejavu-core"}

    missing_probes = apt_minus_fonts - probe_pkgs
    extra_probes = probe_pkgs - apt_minus_fonts

    assert not missing_probes, (
        f"These apt packages are in RUNTIME_QT_APT_PACKAGES but lack a "
        f"check-script probe in RUNTIME_LIB_PROBES: {sorted(missing_probes)}"
    )
    assert not extra_probes, (
        f"These probes are in RUNTIME_LIB_PROBES but the package isn't "
        f"in RUNTIME_QT_APT_PACKAGES: {sorted(extra_probes)} — would "
        f"mean the check script reports them missing but the README "
        f"won't tell the user how to install them."
    )


# ---------------------------------------------------------------------------
# run-on-wsl.sh — dev-side launcher wrapper for the WSL "[WARN: COPY MODE]"
# Weston bug that makes the GUI window invisible on WSLg
# (microsoft/wslg #972 / #1278, microsoft/WSL #12616).
# ---------------------------------------------------------------------------


def test_write_wsl_launcher_script_produces_executable_posix_sh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The WSL launcher wrapper is a dev-side helper for running the
    Linux release inside WSL. It MUST be POSIX sh (not bash) and
    have the executable bit set, same contract as the other helper
    scripts (install-shortcut.sh / check-runtime-deps.sh). If
    either invariant breaks the user can't ``./run-on-wsl.sh``
    without typing an explicit ``sh ...`` prefix, which defeats
    the purpose of having a launcher in the first place."""
    from scripts import build_release_for_linux

    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_release_for_linux, "RELEASE_DIR", tmp_path / "rel")
    (tmp_path / "rel").mkdir()

    build_release_for_linux._write_wsl_launcher_script()

    out = tmp_path / "rel" / "run-on-wsl.sh"
    assert out.is_file(), "run-on-wsl.sh wasn't written"

    text = out.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n"), (
        f"run-on-wsl.sh should start with '#!/bin/sh', got: "
        f"{text.splitlines()[0] if text else '(empty)'}"
    )

    if sys.platform != "win32":
        assert out.stat().st_mode & stat.S_IXUSR, (
            "run-on-wsl.sh must be executable so the user can run "
            "``./run-on-wsl.sh`` directly without ``sh ./...``"
        )


def test_wsl_launcher_script_detects_wsl_via_proc_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The wrapper's whole point is to behave differently on WSL vs
    native Linux: on WSL it forces ``QT_QPA_PLATFORM=xcb`` to
    bypass Weston's broken compositor; on native Linux it leaves
    Qt's platform-plugin choice alone so the user's friend on
    bare-metal Debian / Ubuntu keeps full native Wayland with GPU
    compositing. The detection contract is ``/proc/version`` -- if
    it ever changes (e.g. someone "simplifies" the detection to a
    uname check) the script silently stops doing its job on native
    Linux or, worse, downgrades it to xcb. Pin both halves:
      * The detection probe MUST be ``/proc/version`` (the official
        Microsoft-documented detection contract).
      * Both spellings ``microsoft`` and ``WSL`` must be matched
        case-insensitively (older WSL1 vs newer WSL2 vs any future
        rebrand all show up in different cases).
    """
    from scripts import build_release_for_linux

    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_release_for_linux, "RELEASE_DIR", tmp_path / "rel")
    (tmp_path / "rel").mkdir()

    build_release_for_linux._write_wsl_launcher_script()
    text = (tmp_path / "rel" / "run-on-wsl.sh").read_text(encoding="utf-8")

    assert "/proc/version" in text, (
        "WSL detection must use /proc/version (Microsoft-documented contract)."
    )
    assert "microsoft" in text and "wsl" in text, (
        "Detection regex must cover both 'microsoft' and 'wsl' spellings."
    )
    assert "grep -qiE" in text or "grep -qEi" in text, (
        "Detection must be case-insensitive (-i) and use extended regex (-E) "
        "so the alternation ``microsoft|wsl`` works."
    )


def test_wsl_launcher_script_sets_qt_qpa_platform_xcb_inside_wsl_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The override must be ``QT_QPA_PLATFORM=xcb`` and must be
    inside the WSL-detection ``if``, NOT at the top of the script.
    A top-level export would force xcb on native Linux too,
    silently degrading the bare-metal Wayland compositor that the
    user's friend on real Debian 13 expects to use. Asserting the
    relative ordering catches an "oops, hoisted the export out of
    the conditional" regression that would otherwise look fine
    until a non-WSL user complains about poor visual performance.
    """
    from scripts import build_release_for_linux

    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_release_for_linux, "RELEASE_DIR", tmp_path / "rel")
    (tmp_path / "rel").mkdir()

    build_release_for_linux._write_wsl_launcher_script()
    text = (tmp_path / "rel" / "run-on-wsl.sh").read_text(encoding="utf-8")

    assert "QT_QPA_PLATFORM=xcb" in text, (
        "The whole point of the wrapper is QT_QPA_PLATFORM=xcb."
    )

    # The export must come AFTER the ``if ... grep ... /proc/version``
    # detection guard, not before it. Match the actual ``if`` line
    # (not the comment that mentions /proc/version a few lines above
    # it), so the assertion describes the runtime branching structure
    # rather than the script's prose.
    if_idx = text.find("if [ -r /proc/version ]")
    export_idx = text.find("export QT_QPA_PLATFORM=xcb")
    fi_idx = text.find("\nfi\n", if_idx)
    assert if_idx != -1 and export_idx != -1 and fi_idx != -1, (
        "Script must contain the ``if [ -r /proc/version ]`` guard, the "
        "``export QT_QPA_PLATFORM=xcb`` override, and a closing ``fi``."
    )
    assert if_idx < export_idx < fi_idx, (
        "``export QT_QPA_PLATFORM=xcb`` must be inside the WSL-detection "
        "``if`` block; otherwise it would force xcb on native Linux too "
        "and silently degrade native-Wayland users (the friend on "
        "bare-metal Debian)."
    )


def test_wsl_launcher_script_uses_lf_line_endings_not_crlf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The wrapper MUST be written with LF-only line endings even
    when generated from a Windows Python interpreter (which happens
    if someone regenerates the file via a one-shot ``python -c
    "..."`` from PowerShell instead of running the full Linux
    build inside WSL). If CRLF sneaks in, bash on the WSL side
    reads the shebang as ``#!/bin/sh\\r``, tries to exec an
    interpreter named literally ``/bin/sh\\r`` -- which doesn't
    exist -- and aborts the launch with::

        ./run-on-wsl.sh: cannot execute: required file not found

    No diagnostic from the user that ever describes that exact
    error should be possible after this fixture. The other
    release-linux helper scripts don't need a parallel test
    because they're only ever written from inside the WSL build
    pipeline, where Python's text-mode write produces LF natively;
    the WSL launcher is the one script that's plausibly
    regenerated from Windows.
    """
    from scripts import build_release_for_linux

    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_release_for_linux, "RELEASE_DIR", tmp_path / "rel")
    (tmp_path / "rel").mkdir()

    build_release_for_linux._write_wsl_launcher_script()
    raw = (tmp_path / "rel" / "run-on-wsl.sh").read_bytes()

    assert b"\r\n" not in raw, (
        "run-on-wsl.sh has CRLF line endings -- bash on WSL will read "
        "the shebang as '#!/bin/sh\\r' and abort with 'cannot execute: "
        "required file not found'. The writer must pass newline='\\n' "
        "to Path.write_text to keep Python's Windows text-mode "
        "translation from sneaking CRLF in."
    )
    # Sanity-check: the file is non-empty and actually contains LF
    # separators (an empty / single-line file would technically pass
    # the CRLF assertion above by vacuous truth).
    assert raw.count(b"\n") > 5, (
        "Generated script has too few line breaks -- something's off."
    )


def test_wsl_launcher_script_execs_the_bundled_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The wrapper must end by ``exec``-ing the ELF (not just calling
    it), so signals (SIGINT from Ctrl-C, SIGTERM from a window-manager
    close-button-from-taskbar) propagate to the actual app instead
    of being eaten by the sh wrapper, and so the wrapper process
    doesn't linger after the GUI exits. It must also forward ``$@``
    so any CLI args the user passes to the wrapper reach the binary.
    """
    from scripts import build_release_for_linux

    monkeypatch.setattr(build_release_for_linux, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_release_for_linux, "RELEASE_DIR", tmp_path / "rel")
    (tmp_path / "rel").mkdir()

    build_release_for_linux._write_wsl_launcher_script()
    text = (tmp_path / "rel" / "run-on-wsl.sh").read_text(encoding="utf-8")

    assert 'exec "$BIN"' in text, (
        "Wrapper must exec the binary (not just call it) so signals "
        "and exit codes propagate transparently."
    )
    assert '"$@"' in text, (
        'Wrapper must forward $@ so user-supplied CLI args reach the binary.'
    )
    # Sanity-check the script can locate its own folder portably (handles
    # the case where the user runs ./run-on-wsl.sh from a different cwd
    # via an absolute path or a symlink in /usr/local/bin).
    assert "$(dirname " in text, (
        "Wrapper must resolve the binary path relative to its own location, "
        "not the user's cwd."
    )


# ---------------------------------------------------------------------------
# Chart PDFs are NO LONGER shipped (v3.3+)
# ---------------------------------------------------------------------------


def test_build_script_no_longer_defines_copy_charts() -> None:
    """The v3.2-era ``_copy_charts`` function is gone — Israeli
    government terms of use prohibit redistribution of the CAAI
    chart PDFs. v3.3+ relies on the runtime download flow (see
    ``cvfr_routemaster.chart_source``) and the build script must
    not silently re-introduce a chart-copy step.

    Pinning the absence at module level catches a copy-paste
    revival; the matching ordering test in
    ``test_restamp_cache_fingerprints.test_build_script_calls_derived_files_after_seed_cache``
    catches a re-introduction at the call site."""
    from scripts import build_release_for_linux

    assert not hasattr(build_release_for_linux, "_copy_charts"), (
        "v3.3+ removed _copy_charts — re-adding it would ship "
        "CAAI PDFs in violation of gov.il terms of use"
    )


def test_cache_files_does_not_include_rendered_pngs() -> None:
    """v3.3+ no longer ships ``map_north.png`` / ``map_south.png``
    in the seed cache: those are rendered output of the chart
    PDFs (which are not redistributable), so the rendered raster
    inherits the same restriction. The runtime renders them on
    first chart-load against the just-downloaded PDFs (~30s)."""
    from scripts import build_release_for_linux

    forbidden = {"map_north.png", "map_south.png"}
    assert not (
        forbidden & set(build_release_for_linux.CACHE_FILES)
    ), (
        f"v3.3+ must not include rendered chart PNGs in CACHE_FILES; "
        f"found: {forbidden & set(build_release_for_linux.CACHE_FILES)}"
    )


def test_copy_seed_cache_skips_missing_optional_files_with_a_notice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Optional cache files (the rendered map PNGs especially) might
    not be present in every dev environment. Build script must skip
    them with a friendly notice rather than failing the build.

    Same contract as the Windows builder; pinned here so the Linux
    builder doesn't drift into stricter behaviour by accident.
    """
    from scripts import build_release_for_linux

    fake_dev_cache = tmp_path / "dev-cache"
    fake_dev_cache.mkdir()
    # Seed only the critical file — calibration JSON.
    (fake_dev_cache / "geo_calibration.json").write_text("{}")

    fake_release = tmp_path / "rel"
    fake_release.mkdir()

    monkeypatch.setattr(build_release_for_linux, "DEV_CACHE_DIR", fake_dev_cache)
    monkeypatch.setattr(build_release_for_linux, "RELEASE_DIR", fake_release)

    build_release_for_linux._copy_seed_cache()

    out = capsys.readouterr().out
    assert "geo_calibration.json" in out
    assert "skipped" in out
    assert "optional" in out
    # Critical file made it through.
    assert (fake_release / ".cvfr_routemaster" / "geo_calibration.json").is_file()
