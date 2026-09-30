"""Freeze Ember for this OS with PyInstaller: dist/Ember/ holding Ember.exe
(Windows) or Ember (Linux), plus its _internal/ folder. Ship the folder.

    python -m pip install pyinstaller pywebview      # Linux: pywebview[gtk]
    python packaging/pyinstaller/build.py

The executable is the window (native/window.py), the server (--server) and
the Claude Code tees (devtools_hooks.py …) in one; the release workflow runs
this on Windows and Linux. macOS uses packaging/macos/build-app.sh instead.
"""
import os
import sys
from pathlib import Path

import PyInstaller.__main__

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "build" / "pyinstaller"


def main():
    data = [("index.html", "."), ("addons.json", "."), ("vendor", "vendor"),
            ("launchers/linux/claude-devtools.svg", ".")]      # --install's icon
    args = [str(REPO / "native" / "window.py"), "--name", "Ember", "--noconfirm",
            "--distpath", str(REPO / "dist"), "--workpath", str(BUILD),
            "--specpath", str(BUILD),
            # window.py puts these on sys.path at run time; tell the analyser
            "--paths", str(REPO), "--paths", str(REPO / "tools"),
            "--hidden-import", "server", "--hidden-import", "devtools_hooks",
            "--hidden-import", "winconpty"]
    for src, dest in data:
        args += ["--add-data", f"{REPO / src}{os.pathsep}{dest}"]
    if sys.platform == "win32":
        # no console window; window.py rebuilds stdio for hooks and the server
        args += ["--windowed", "--icon", str(REPO / "launchers" / "windows" / "claude-devtools.ico")]
    PyInstaller.__main__.run(args)


if __name__ == "__main__":
    main()
