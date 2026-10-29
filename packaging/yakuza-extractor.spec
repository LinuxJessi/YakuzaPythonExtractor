# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — Yakuza (PS2) Python Extractor (release build).

Self-contained, one-folder bundle, the same shape as the Xenosaga I/III kits.
One ``COLLECT`` puts BOTH binaries in one directory so the GUI always finds
its engine next to itself:

    dist/Yakuza-Extractor-windows/     (built on Windows)
      Yakuza-Extractor.exe             windowed launcher — the local web GUI
      yakuza-cli.exe                   console; tool.py, the dispatcher the GUI shells out to
      _internal/                       shared runtime (numpy, Pillow, tools/ffmpeg.exe)

    dist/Yakuza-Extractor.app          (built on macOS — double-click this)
    dist/Yakuza-Extractor-macos/       (same build, bare folder form)

AV-friendliness: one-folder (one-file unpacks to %TEMP% at launch, a packer
heuristic), ``upx=False``, GUI ``console=False``, CLI ``console=True``.

PyInstaller does not cross-compile — run on the OS you want binaries for:
    python -m pip install pyinstaller numpy pillow
    python build.py --clean
"""
import sys
from pathlib import Path

HERE = Path(SPECPATH).resolve()
REPO = HERE.parent
PLATFORM = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")

ICON = HERE / "icon.ico"
ICON_ARG = str(ICON) if ICON.exists() else None

DATAS = [(str(REPO / "README.md"), "."), (str(REPO / "LICENSE"), "."),
         (str(REPO / "docs" / "FORMATS.md"), "docs"), (str(HERE / "tools" / "TOOLS.txt"), "tools")]
DATAS = [(s, d) for s, d in DATAS if Path(s).exists()]

# A portable ffmpeg dropped into packaging/tools/ (the release workflow
# downloads one per OS) lands under tools/ in the runtime dir, where
# media.find_ffmpeg() looks first — voice, music and movies then need no installs.
FFMPEG = HERE / "tools" / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
BINARIES = [(str(FFMPEG), "tools")] if FFMPEG.exists() else []

# tool.py imports the scripts by name at run time, and cli.py's convert
# imports browse/media/render lazily, so every kit module is listed here.
HIDDEN = ["cli", "browse", "render", "media", "repack", "blender_open",
          "containers", "avlz", "txbp", "ome", "texbind", "ogredir", "cvm", "iso9660", "dap_decode",
          "numpy", "PIL", "PIL.Image"]


def _analysis(entry):
    return Analysis([str(REPO / entry)], pathex=[str(REPO)], binaries=BINARIES, datas=DATAS,
                    hiddenimports=HIDDEN, hookspath=[], runtime_hooks=[],
                    excludes=["tkinter", "test", "unittest", "matplotlib", "scipy", "pandas"], noarchive=False)


gui_a = _analysis("gui.py")
cli_a = _analysis("tool.py")
gui_pyz = PYZ(gui_a.pure, gui_a.zipped_data)
cli_pyz = PYZ(cli_a.pure, cli_a.zipped_data)

gui_exe = EXE(gui_pyz, gui_a.scripts, [], exclude_binaries=True, name="Yakuza-Extractor",
              debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False,
              disable_windowed_traceback=False, icon=ICON_ARG)
cli_exe = EXE(cli_pyz, cli_a.scripts, [], exclude_binaries=True, name="yakuza-cli",
              debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True,
              disable_windowed_traceback=False, icon=ICON_ARG)

coll = COLLECT(gui_exe, gui_a.binaries, gui_a.zipfiles, gui_a.datas,
               cli_exe, cli_a.binaries, cli_a.zipfiles, cli_a.datas,
               strip=False, upx=False, name=f"Yakuza-Extractor-{PLATFORM}")

if sys.platform == "darwin":
    ICNS = HERE / "icon.icns"
    app = BUNDLE(coll, name="Yakuza-Extractor.app", icon=str(ICNS) if ICNS.exists() else None,
                 bundle_identifier="io.github.linuxjessi.yakuzaextractor",
                 info_plist={"CFBundleName": "Yakuza Extractor", "CFBundleDisplayName": "Yakuza Extractor",
                             "NSHighResolutionCapable": True})
