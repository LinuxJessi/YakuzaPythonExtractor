"""tool.py — one entry point for every kit script (what the bundled ``yakuza-cli`` runs).

    yakuza-cli cli     probe "Yakuza (USA).iso"
    yakuza-cli browse  textures dump out/textures
    yakuza-cli render  dump out/textures out/previews
    yakuza-cli media   audio dump out/audio
    yakuza-cli repack  verify modded.iso
    yakuza-cli blender_open out/models E333

From a source checkout ``python3 tool.py cli probe …`` is the same as
``python3 cli.py probe …``; the dispatcher exists so the frozen bundle
needs a single console executable next to the GUI. Every script's
``main(argv)`` is called in-process; the worker pools the converters spawn
re-enter this file, which is why ``freeze_support()`` runs first.
"""
from __future__ import annotations

import importlib
import multiprocessing
import os
import sys

MODULES = ("cli", "browse", "render", "media", "repack", "blender_open")


def main(argv=None):
    multiprocessing.freeze_support()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--version", "-V"):
        import cli
        print(f"Yakuza (PS2) Python Extractor {cli.__version__}")
        return 0
    if not argv or argv[0] not in MODULES:
        print(__doc__.strip(), file=sys.stderr)
        print(f"\nscripts: {', '.join(MODULES)}", file=sys.stderr)
        return 2
    name, rest = argv[0], argv[1:]
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    mod = importlib.import_module(name)
    sys.argv = [name + ".py", *rest]      # argparse shows the script's own name
    return mod.main(rest)


if __name__ == "__main__":
    sys.exit(main())
