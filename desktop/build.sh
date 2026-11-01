#!/usr/bin/env bash
# Build the one-file Windows exe (desktop/dist/tenir-capture.exe). Run on Windows (Git Bash).
#   desktop/build.sh [VERSION]   # VERSION is x.y.z for a release; "dev" otherwise
set -euo pipefail
cd "$(dirname "$0")"
version="${1:-dev}"
printf '# Stamped by the release build (.github/workflows/release.yml); "dev" for an unbuilt checkout.\n__version__ = "%s"\n' \
  "$version" > tenir_capture/_version.py
python -m pip install --quiet '.[build]'
# --hide-console hide-early: no console window when started at logon or double-clicked, but
# --pair / --list-windows still talk to the terminal they were run from.
# plyer picks its notification backend at run time, so PyInstaller can't see the import.
python -m PyInstaller --noconfirm --clean --onefile --name tenir-capture \
  --hide-console hide-early \
  --hidden-import plyer.platforms.win.notification \
  --paths . \
  tenir_capture/__main__.py
