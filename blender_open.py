"""blender_open.py — open an assembled character (or any OBJ folder) in Blender.

  python3 blender_open.py MODELS_DIR E333            # enemy set: body + head + shared hands
  python3 blender_open.py MODELS_DIR MEDIA/PLAYER/PLAYER0.DAT
  python3 blender_open.py MODELS_DIR MEDIA3/ARC/DAY_KAB0000.ARC   # a whole street block

MODELS_DIR is the ``browse/models`` folder written by ``browse.py models``.
Enemy bodies have no hands of their own — the hand meshes 13/14 live in
``MEDIA3/ENEMY/ENECOMMON.DAT`` and are added automatically for E-sets. Their
skin is painted on each character's own body sheet (ENECOMMON only holds
shadow blobs), so the hands are given the body's material, and they are
snapped to the arm ends because the skeleton that places them is not
exported.
Finds Blender at /Applications/Blender.app or on PATH; textures bind via
the MTLs. Imports are deferred with ``bpy.app.timers`` because
``wm.obj_import`` has no window context during ``--python`` startup.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile


def blender_exe():
    cands = ["/Applications/Blender.app/Contents/MacOS/Blender", shutil.which("blender") or ""]
    cands += sorted(glob.glob(os.path.expandvars(r"%ProgramFiles%\Blender Foundation\Blender*\blender.exe")), reverse=True)
    cands += sorted(glob.glob("/snap/blender/current/blender")) + sorted(glob.glob("/opt/blender*/blender"), reverse=True)
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def require_blender():
    exe = blender_exe()
    if not exe:
        sys.exit("Blender not found (install it from blender.org, or put `blender` on PATH)")
    return exe


def collect(models_dir, target):
    if target.upper().startswith("E") and target[1:].isdigit():     # enemy set
        base = os.path.join(models_dir, "MEDIA3", "ENEMY", target.upper())
        files = sorted(glob.glob(os.path.join(base, f"{target.upper()}00.DAT", "*.obj")))
        files += sorted(glob.glob(os.path.join(base, f"{target.upper()}01.DAT", "*.obj")))[:1]
        common = os.path.join(models_dir, "MEDIA3", "ENEMY", "ENECOMMON.DAT")
        files += [os.path.join(common, "1-13.obj"), os.path.join(common, "1-14.obj")]
    else:
        files = sorted(glob.glob(os.path.join(models_dir, target.strip("/"), "*.obj")))
    files = [f for f in files if os.path.exists(f)]
    if not files:
        sys.exit(f"no OBJ files for {target}")
    return files


def hand_offsets(files):
    """Translate each shared hand so its wrist cuff meets the body's wrist.

    Enemy hands are rigid parts the skeleton places at runtime; on their own
    they sit at the male rest pose. ``browse.py models`` writes a .json
    sidecar per mesh with bone-derived attachment points (forearm-bone tip
    for bodies, bone-0 cuff for hands); we shift each hand by the difference.
    """
    import json
    body = [f for f in files if "ENECOMMON" not in f and f.endswith("1-0.obj")]
    if not body or not os.path.exists(body[0][:-4] + ".json"):
        return {}
    wrists = json.load(open(body[0][:-4] + ".json")).get("wrists") or {}
    offs = {}
    for f in files:
        if "ENECOMMON" not in f or not os.path.exists(f[:-4] + ".json"):
            continue
        cuff = json.load(open(f[:-4] + ".json")).get("cuff")
        if not cuff:
            continue
        side = "R" if cuff[0] > 0 else "L"
        if side in wrists:
            offs[f] = tuple(w - c for w, c in zip(wrists[side], cuff))
    return offs


def main(argv=None):
    a = (argv or sys.argv[1:])
    if len(a) != 2:
        sys.exit(__doc__)
    files = collect(os.path.abspath(a[0]), a[1])
    offs = hand_offsets(files)
    script = os.path.join(tempfile.mkdtemp(), "open.py")
    with open(script, "w") as fh:
        fh.write("import bpy\nfiles = %r\noffs = %r\n" % (files, offs) + '''
def load():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    body_mat = None
    for f in files:
        bpy.ops.wm.obj_import(filepath=f)
        objs = list(bpy.context.selected_objects)
        if body_mat is None and objs and objs[0].data.materials:
            body_mat = objs[0].data.materials[0]
        if f in offs:
            for o in objs:
                o.location = (o.location[0] + offs[f][0], o.location[1] - offs[f][2], o.location[2] + offs[f][1])
                if body_mat is not None:
                    o.data.materials.clear()
                    o.data.materials.append(body_mat)
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = 'MATERIAL'
                region = [r for r in area.regions if r.type == 'WINDOW'][0]
                with bpy.context.temp_override(window=window, area=area, region=region):
                    bpy.ops.view3d.view_all()
    return None
bpy.app.timers.register(load, first_interval=1.0)
''')
    print(f"opening {len(files)} meshes:")
    for f in files:
        print("  " + os.path.relpath(f, a[0]))
    subprocess.Popen([require_blender(), "--python", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
