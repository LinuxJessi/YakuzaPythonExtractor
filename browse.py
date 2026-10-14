"""browse.py — convert dumped assets to desktop formats.

  python3 browse.py textures DUMP_DIR OUT_DIR [--filter SUBSTR] [--jobs N]
  python3 browse.py models   DUMP_DIR OUT_DIR [--filter SUBSTR] [--jobs N]

Walks every file under DUMP_DIR, descends the container tree, inflates AVLZ
leaves, and writes each TXBP texture as PNG:
  OUT_DIR/<in-game path>/<leaf path>_<n>.png
or each OME mesh as OBJ + MTL (materials point at the sibling PNGs when the
texture id resolves inside the same file):
  OUT_DIR/<in-game path>/<leaf path>.obj
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import containers  # noqa: E402
import txbp        # noqa: E402

SKIP_EXT = {".ahx", ".adx", ".sfd"}


def _texture_job(args):
    src, rel, out_root = args
    blob = open(src, "rb").read()
    if blob[:4] == b"SGT\0":
        blob = blob[0x20:]
    n = 0
    written = []
    stack = [("", blob)] if blob[:4] == b"TXBP" else list(containers.leaves(blob))
    for path, leaf in stack:
        try:
            data = containers.inflate(leaf)
        except Exception:
            continue
        if data[:4] != b"TXBP":
            continue
        try:
            texs = txbp.parse(data)
        except Exception as e:  # unknown variant (e.g. DEBUG/ALL.TXB)
            written.append(f"!{rel}{path}: {e}")
            continue
        outdir = os.path.join(out_root, rel.strip("/"))
        os.makedirs(outdir, exist_ok=True)
        for t in texs:
            name = f"{path.strip('/').replace('/', '-') or 'tex'}_{t.index}.png"
            txbp.write_png(os.path.join(outdir, name), t.width, t.height, t.rgba())
            n += 1
    return rel, n, written


def cmd_textures(a):
    jobs = []
    for dp, _dn, fn in os.walk(a.dump):
        for name in fn:
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            full = os.path.join(dp, name)
            rel = full[len(a.dump):]
            if a.filter and a.filter.lower() not in rel.lower():
                continue
            jobs.append((full, rel, a.out))
    total = 0
    with multiprocessing.Pool(a.jobs) as pool:
        for i, (rel, n, notes) in enumerate(pool.imap_unordered(_texture_job, jobs, chunksize=4)):
            total += n
            for w in notes:
                print(w, flush=True)
            if i % 500 == 0:
                print(f"[{i}/{len(jobs)}] {total} textures so far", flush=True)
    print(f"wrote {total} textures from {len(jobs)} files")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    q = sp.add_parser("textures")
    q.add_argument("dump"); q.add_argument("out")
    q.add_argument("--filter"); q.add_argument("--jobs", type=int, default=max(2, os.cpu_count() // 2))
    q.set_defaults(fn=cmd_textures)
    _add_models(sp)
    a = p.parse_args(argv)
    a.fn(a)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
def _model_job(args):
    import ome
    import texbind
    src, rel, out_root = args
    blob = open(src, "rb").read()
    packs, meshes = texbind.collect_packs(containers.leaves(blob), containers.inflate, txbp.parse, texbind.png_name)
    if not meshes:
        return rel, 0, 0
    outdir = os.path.join(out_root, rel.strip("/"))
    os.makedirs(outdir, exist_ok=True)
    n = tris = 0
    ups = "../" * (len(rel.strip("/").split("/")) + 1)   # back to the browse/ root
    parsed = []
    for path, data in meshes:
        try:
            m = ome.parse(data)
        except Exception as e:
            print(f"!{rel}{path}: {e}", flush=True)
            continue
        if m.blocks:
            parsed.append((path, m))
    binding = texbind.bind_file(packs, {path: {b.tex_slot for b in m.blocks} for path, m in parsed})
    for path, m in parsed:
        stem = path.strip("/").replace("/", "-") or "mesh"
        slots = sorted({b.tex_slot for b in m.blocks})
        bound = binding.get(path, {})
        with open(os.path.join(outdir, stem + ".mtl"), "w") as fh:
            for s in slots:
                fh.write(f"newmtl tex{s}\nKd 0.8 0.8 0.8\n")
                if s in bound:
                    fh.write(f"map_Kd {ups}textures/{rel.strip('/')}/{bound[s]}\n")
                fh.write("\n")
        tris += ome.write_obj(os.path.join(outdir, stem + ".obj"), m, stem, mtllib=stem + ".mtl")
        side = {"wrists": ome.body_wrists(m), "cuff": ome.hand_cuff(m),
                "bones_used": sorted({int(b & 0xFF) for blk in m.blocks for b in blk.bones})}
        if side["wrists"] or side["cuff"]:
            import json
            with open(os.path.join(outdir, stem + ".json"), "w") as fh:
                json.dump(side, fh)
        n += 1
    return rel, n, tris


def cmd_models(a):
    jobs = []
    for dp, _dn, fn in os.walk(a.dump):
        for name in fn:
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            full = os.path.join(dp, name)
            rel = full[len(a.dump):]
            if a.filter and a.filter.lower() not in rel.lower():
                continue
            jobs.append((full, rel, a.out))
    total = tris = 0
    with multiprocessing.Pool(a.jobs) as pool:
        for i, (rel, n, t) in enumerate(pool.imap_unordered(_model_job, jobs, chunksize=4)):
            total += n
            tris += t
            if i % 500 == 0:
                print(f"[{i}/{len(jobs)}] {total} meshes / {tris} triangles so far", flush=True)
    print(f"wrote {total} meshes ({tris} triangles) from {len(jobs)} files")


def _add_models(sp):
    q = sp.add_parser("models")
    q.add_argument("dump"); q.add_argument("out")
    q.add_argument("--filter"); q.add_argument("--jobs", type=int, default=max(2, os.cpu_count() // 2))
    q.set_defaults(fn=cmd_models)


if __name__ == "__main__":
    main()
