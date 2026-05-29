"""Israel CVFR route planning assistant.

Single source of truth for the app's version string and the
window-title formatter that derives from it. Every place that
displays the running version to the user (the main window's title
bar, progress-dialog titles, the splash screen, and the
Copyright Information dialog) goes through this module so a
version bump is one edit, not five.

The build cookbook (.cursor/rules/build-releases.mdc, gitignored)
documents the version-bump checklist; in short, ``__version__``
here, the window title at runtime, and the Copyright Information
dialog must all read the same string before the release goes out.
"""

__version__ = "3.3.0"

APP_NAME = "CVFR Route Master"


def display_version() -> str:
    """Return the user-facing version string, with the trailing
    ``.0`` of a major.minor.0 trimmed off so a clean release reads
    as ``3.3`` rather than ``3.3.0`` in the title bar.

    Patch releases keep the third segment (``3.3.1`` stays
    ``3.3.1``), so a hotfix is visually distinct from its parent
    release without forcing the dev to remember a second display
    format.

    Examples:
        ``"3.3.0"`` -> ``"3.3"``
        ``"3.3.1"`` -> ``"3.3.1"``
        ``"3.0.0"`` -> ``"3"``  (also trims a redundant minor zero)
        ``"4.0.5"`` -> ``"4.0.5"``
    """
    parts = __version__.split(".")
    while len(parts) > 1 and parts[-1] == "0":
        parts.pop()
    return ".".join(parts)


def app_title(prefix: str = "") -> str:
    """Build a window-title string consistent across every window
    the app opens.

    The pattern is ``<APP_NAME> (v<display_version>)`` for top-level
    windows and ``<prefix> \u2014 <APP_NAME> (v<display_version>)``
    when a window has a contextual prefix (e.g. ``Loading``,
    ``Waypoints``). The em-dash separator matches the pattern the
    progress-dialog code has used since v1 — keeping it stable
    means existing window-manager rules and screenshots stay valid.

    Args:
        prefix: Optional contextual prefix joined with an em-dash
            before the app name. ``""`` (default) yields the bare
            ``CVFR Route Master (v3.3)`` form used by the main
            window and the splash screen.

    Returns:
        The full title string with version embedded; the version
        suffix is non-optional because the build cookbook's step 0
        (see ``.cursor/rules/build-releases.mdc``) treats a missing
        version in the title as a release-blocking issue.
    """
    suffix = f"{APP_NAME} (v{display_version()})"
    if prefix:
        return f"{prefix} \u2014 {suffix}"
    return suffix
