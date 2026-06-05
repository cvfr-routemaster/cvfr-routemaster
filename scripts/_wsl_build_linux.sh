#!/usr/bin/env bash
# Build the Linux release inside the prepared WSL Debian venv.
#
# Why this wrapper exists
# -----------------------
# The dev box is Windows with Cursor running in PowerShell; the build
# target is Linux ELF, so the build runs in WSL Debian. Going
# PowerShell → wsl.exe → bash means three quoting layers, and any
# attempt to do this inline mangles the inner quotes (PowerShell
# strips bare single quotes; nested double quotes in WSL's bash -c
# get re-quoted by the host shell). Wrapping the build in a script
# the bash invocation can find on disk sidesteps all of that.
#
# The script also activates the WSL build venv -- a permanent virtual
# environment at $HOME/cvfr-build-venv assembled once with the
# project's pip dependencies (see requirements-dev.txt). Debian 13
# enforces PEP 668 on the system Python, so a venv is required;
# rebuilding the venv from scratch is wasteful when its contents
# don't change between Linux builds.
#
# Usage (from PowerShell on the Windows host):
#
#     wsl -d Debian -- bash /mnt/<drive>/<path-to-repo>/scripts/_wsl_build_linux.sh
#
# Or from a WSL bash session:
#
#     bash scripts/_wsl_build_linux.sh
#
# Either invocation does the same thing: activate the venv, then
# delegate to scripts/build_release_for_linux.py for the actual
# pipeline.

set -euo pipefail

# Resolve the repo root from this script's own location so the wrapper
# works regardless of where the checkout lives. Handles both invocation
# styles documented above (absolute path via wsl.exe, relative path
# from a bash session).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="${HOME}/cvfr-build-venv"

if [[ ! -d "$VENV" ]]; then
    echo "[!] WSL build venv missing at $VENV" >&2
    echo "    Create it once with:" >&2
    echo "      python3 -m venv \"$VENV\"" >&2
    echo "      \"$VENV/bin/pip\" install -r $REPO/requirements-dev.txt" >&2
    exit 1
fi

cd "$REPO"
source "$VENV/bin/activate"

echo "=== Linux build starting at $(date -Is) ==="
echo "python:  $(python --version)"
echo "venv:    $VENV"
echo "repo:    $REPO"
echo

exec python scripts/build_release_for_linux.py
