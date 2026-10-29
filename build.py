#!/usr/bin/env python3
"""build.py — package the extractor into a self-contained one-folder bundle.

Drives ``packaging/yakuza-extractor.spec`` to produce, in ``dist/``:

    dist/Yakuza-Extractor-windows/         (built on Windows)
      Yakuza-Extractor.exe                 the GUI — double-click this
      yakuza-cli.exe                       the engine the GUI drives (tool.py)
      _internal/                           shared runtime, numpy, Pillow, tools/ffmpeg.exe

    dist/Yakuza-Extractor.app              (built on macOS — double-click this)
    dist/Yakuza-Extractor-macos/           (same build, bare folder form)

Both binaries live in ONE folder, so the GUI always finds its engine beside
it. Ship the platform folder (zip it), or on macOS the .app.

Usage:
    python build.py            # build the bundle for THIS platform
    python build.py --clean    # wipe build/ and this platform's dist output first

PyInstaller does not cross-compile: run this under a Windows Python for the
.exe files, under macOS for the .app. The GitHub Actions workflow in
.github/workflows/release.yml does both on every version tag.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "packaging" / "yakuza-extractor.spec"
DIST = ROOT / "dist"

PLATFORM = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
BUNDLE = DIST / f"Yakuza-Extractor-{PLATFORM}"
APP = DIST / "Yakuza-Extractor.app"


def main() -> int:
    try:
        from PyInstaller.__main__ import run as pyinstaller
    except ImportError:
        sys.exit("PyInstaller is not installed in this Python.\n"
                 "  python -m pip install pyinstaller numpy pillow\n"
                 "then re-run:  python build.py")
    if not SPEC.exists():
        sys.exit(f"spec not found: {SPEC}")

    argv = ["--noconfirm", str(SPEC)]
    if "--clean" in sys.argv:
        argv.insert(0, "--clean")
        shutil.rmtree(BUNDLE, ignore_errors=True)
        if sys.platform == "darwin":
            shutil.rmtree(APP, ignore_errors=True)

    print(f"=== pyinstaller {' '.join(argv)} ===", flush=True)
    pyinstaller(argv)

    ext = ".exe" if sys.platform == "win32" else ""
    gui = BUNDLE / f"Yakuza-Extractor{ext}"
    cli = BUNDLE / f"yakuza-cli{ext}"
    ok = gui.exists() and cli.exists()
    print("\n" + "-" * 60)
    print(f"bundle : {BUNDLE}")
    print(f"  GUI  : {gui.name}  {'OK' if gui.exists() else 'MISSING'}")
    print(f"  CLI  : {cli.name}  {'OK' if cli.exists() else 'MISSING'}")
    if sys.platform == "darwin":
        app_ok = (APP / "Contents" / "MacOS" / "Yakuza-Extractor").exists()
        print(f"  app  : {APP.name}  {'OK' if app_ok else 'MISSING'}")
        ok = ok and app_ok
    if ok:
        what = APP.name if sys.platform == "darwin" else f"{BUNDLE.name}/ (zip it)"
        print(f"\nDone. Ship {what}; double-click {APP.name if sys.platform == 'darwin' else gui.name} to launch.")
    else:
        print("\nBuild incomplete — see the PyInstaller output above.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
