"""render.py — textured software previews of OME meshes (numpy + Pillow).

  python3 render.py DUMP_DIR TEXTURE_DIR OUT_DIR [--filter SUBSTR] [--jobs N] [--size 512]

For every file that holds meshes writes
  OUT_DIR/<in-game path>/<leaf>.png        one image per mesh
  OUT_DIR/<in-game path>.png               all meshes of the file composited
Orthographic 3/4 view, Lambert shading, nearest-filtered textures with
alpha test; untextured sub-meshes are drawn grey.
"""
from __future__ import annotations

import argparse
import math
import multiprocessing
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import containers  # noqa: E402
import ome         # noqa: E402
import txbp        # noqa: E402
import texbind     # noqa: E402

SKIP_EXT = {".ahx", ".adx", ".sfd"}
BG = (40, 40, 48)


def _view_matrix(az_deg=215.0, el_deg=22.0):
    az, el = math.radians(az_deg), math.radians(el_deg)
    ry = np.array([[math.cos(az), 0, math.sin(az)], [0, 1, 0], [-math.sin(az), 0, math.cos(az)]])
    rx = np.array([[1, 0, 0], [0, math.cos(el), -math.sin(el)], [0, math.sin(el), math.cos(el)]])
    return rx @ ry


def gather(blocks):
    """Concatenate blocks → (verts Nx3, uvs Nx2|None, tris Mx3, tex_slot per tri)."""
    V, T, F, S = [], [], [], []
    base = 0
    for b in blocks:
        n = len(b.positions)
        if n == 0:
            continue
        V.append(np.asarray(b.positions, dtype=np.float32))
        T.append(np.asarray(b.uvs, dtype=np.float32) if len(b.uvs) == n else np.zeros((n, 2), np.float32))
        tris = np.asarray(list(b.triangles()), dtype=np.int64).reshape(-1, 3)
        F.append(tris + base)
        S.append(np.full(len(tris), b.tex_slot if len(b.uvs) == n else -2, np.int64))
        base += n
    if not V:
        return None
    return np.concatenate(V), np.concatenate(T), np.concatenate(F), np.concatenate(S)


def rasterize(verts, uvs, tris, slots, textures, size=512, az=215.0, el=22.0):
    R = _view_matrix(az, el)
    p = verts @ R.T
    lo, hi = p.min(0), p.max(0)
    span = max(hi[0] - lo[0], hi[1] - lo[1], 1e-6)
    sc = (size - 16) / span
    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    sx = (p[:, 0] - cx) * sc + size / 2
    sy = size / 2 - (p[:, 1] - cy) * sc
    sz = p[:, 2]
    img = np.zeros((size, size, 3), np.float32)
    img[:] = np.array(BG, np.float32) / 255
    zbuf = np.full((size, size), -np.inf, np.float32)
    light = np.array([0.3, 0.6, 0.74])
    light /= np.linalg.norm(light)
    # face normals in view space
    a, b, c = p[tris[:, 0]], p[tris[:, 1]], p[tris[:, 2]]
    fn = np.cross(b - a, c - a)
    nl = np.linalg.norm(fn, axis=1)
    keep = nl > 1e-12
    fn = fn[keep] / nl[keep][:, None]
    tris, slots = tris[keep], slots[keep]
    shade = 0.35 + 0.65 * np.abs(fn @ light)
    # draw far-to-near not needed with z-buffer; loop per triangle
    for i in range(len(tris)):
        t = tris[i]
        x0, x1 = sx[t], sy[t]
        xmin, xmax = int(max(np.floor(x0.min()), 0)), int(min(np.ceil(x0.max()), size - 1))
        ymin, ymax = int(max(np.floor(x1.min()), 0)), int(min(np.ceil(x1.max()), size - 1))
        if xmin > xmax or ymin > ymax:
            continue
        xs = np.arange(xmin, xmax + 1)
        ys = np.arange(ymin, ymax + 1)
        px, py = np.meshgrid(xs + 0.5, ys + 0.5)
        d = (x0[1] - x0[0]) * (x1[2] - x1[0]) - (x0[2] - x0[0]) * (x1[1] - x1[0])
        if abs(d) < 1e-9:
            continue
        w0 = ((x0[1] - px) * (x1[2] - py) - (x0[2] - px) * (x1[1] - py)) / d
        w1 = ((x0[2] - px) * (x1[0] - py) - (x0[0] - px) * (x1[2] - py)) / d
        w2 = 1 - w0 - w1
        m = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not m.any():
            continue
        z = w0 * sz[t[0]] + w1 * sz[t[1]] + w2 * sz[t[2]]
        zb = zbuf[ymin:ymax + 1, xmin:xmax + 1]
        m &= z > zb
        if not m.any():
            continue
        tex = textures.get(int(slots[i]))
        if tex is not None:
            uv = uvs[t]
            u = w0 * uv[0, 0] + w1 * uv[1, 0] + w2 * uv[2, 0]
            v = w0 * uv[0, 1] + w1 * uv[1, 1] + w2 * uv[2, 1]
            th, tw = tex.shape[:2]
            ui = (np.floor(u * tw).astype(np.int64)) % tw
            vi = (np.floor(v * th).astype(np.int64)) % th
            texel = tex[vi, ui]
            m &= texel[..., 3] > 0.25
            if not m.any():
                continue
            col = texel[..., :3] * shade[i]
        else:
            col = np.full(m.shape + (3,), 0.72 * shade[i], np.float32)
        zb[m] = z[m]
        img[ymin:ymax + 1, xmin:xmax + 1][m] = col[m]
    return (np.clip(img, 0, 1) * 255).astype(np.uint8)


def load_textures(tex_dir, rel, bound, cache):
    out = {}
    for tid, name in bound.items():
        if name not in cache:
            p = os.path.join(tex_dir, rel.strip("/"), name)
            cache[name] = np.asarray(Image.open(p).convert("RGBA"), np.float32) / 255 if os.path.exists(p) else None
        if cache[name] is not None:
            out[tid] = cache[name]
    return out


def _job(args):
    src, rel, tex_dir, out_root, size = args
    blob = open(src, "rb").read()
    packs, raw = texbind.collect_packs(containers.leaves(blob), containers.inflate, txbp.parse, texbind.png_name)
    meshes = []
    for path, data in raw:
        try:
            m = ome.parse(data)
            if m.blocks:
                meshes.append((path, m))
        except Exception:
            pass
    if not meshes:
        return rel, 0
    outdir = os.path.join(out_root, rel.strip("/"))
    os.makedirs(outdir, exist_ok=True)
    cache = {}
    n = 0
    comp_V, comp_U, comp_F, comp_S = [], [], [], []
    comp_tex = {}
    vbase = 0
    binding = texbind.bind_file(packs, {path: {b.tex_slot for b in m.blocks} for path, m in meshes})
    for path, m in meshes:
        g = gather(m.blocks)
        if g is None:
            continue
        V, U, F, Sl = g
        bound = binding.get(path, {})
        textures = load_textures(tex_dir, rel, bound, cache)
        img = rasterize(V, U, F, Sl, textures, size=size)
        stem = path.strip("/").replace("/", "-") or "mesh"
        Image.fromarray(img).save(os.path.join(outdir, stem + ".png"))
        n += 1
        # composite: remap slots to keys unique per (mesh, slot)
        remap = np.vectorize(lambda s: (n << 20) | (int(s) & 0xFFFFF))(Sl)
        for s, t in textures.items():
            comp_tex[(n << 20) | (s & 0xFFFFF)] = t
        comp_V.append(V); comp_U.append(U); comp_F.append(F + vbase); comp_S.append(remap)
        vbase += len(V)
    if len(meshes) > 1 and comp_V:
        img = rasterize(np.concatenate(comp_V), np.concatenate(comp_U), np.concatenate(comp_F),
                        np.concatenate(comp_S), comp_tex, size=size * 2 if size <= 512 else size)
        Image.fromarray(img).save(outdir + ".png")
    return rel, n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump"); ap.add_argument("textures"); ap.add_argument("out")
    ap.add_argument("--filter"); ap.add_argument("--jobs", type=int, default=max(2, os.cpu_count() // 2))
    ap.add_argument("--size", type=int, default=512)
    a = ap.parse_args(argv)
    jobs = []
    for dp, _dn, fn in os.walk(a.dump):
        for name in fn:
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            full = os.path.join(dp, name)
            rel = full[len(a.dump):]
            if a.filter and a.filter.lower() not in rel.lower():
                continue
            jobs.append((full, rel, a.textures, a.out, a.size))
    total = 0
    with multiprocessing.Pool(a.jobs) as pool:
        for i, (rel, n) in enumerate(pool.imap_unordered(_job, jobs, chunksize=2)):
            total += n
            if i % 200 == 0:
                print(f"[{i}/{len(jobs)}] {total} previews", flush=True)
    print(f"wrote {total} mesh previews from {len(jobs)} files")


if __name__ == "__main__":
    main()
