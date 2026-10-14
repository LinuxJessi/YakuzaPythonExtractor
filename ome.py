"""ome.py — OME meshes → Wavefront OBJ.

An OME leaf (always AVLZ-wrapped on disc) is::

  0x00  "OME\\0"
  0x04  u32 ?            0x08  u32 ODBP offset       0x0c  0
  0x10  u32 material-table rows   0x14  u32 0x30 (table offset)
  0x18  u32 format hint  0x1c  u32 ?
  0x30  rows × 32 bytes  material/GS-register table (opaque for now)

  ODBP: "ODBP", u32 data offset, u32 n, u32 total size, pad to 0x20,
        then 0x40-byte sub-mesh records at +0x80:
          u32 first block offset (relative to ODBP+0x20), u32 material-color
          offset, u32 texture slot, i32 -1/0, u32 id, 0, 0, u32 flags,
          float cx, cy, cz, radius.
        The records run until the data offset; only those whose offset
        lands on a real block header (and whose flags low word is 1) are
        sub-meshes — the rest are matrices/visibility cells. The trailing
        u16 list before the first block is a block-index draw list used by
        the visibility cells (bbox + offset + count records).

  Blocks (chained back-to-back, each 16-byte aligned)::
    u32 5, u32 vertex flags, u32 vertex offset, u32 vertex count,
    u32 index offset, u32 index count, 0, 0       (offsets rel. ODBP+0x20)
    vertices, then u16 triangle-strip indices (degenerate restarts).

  Vertex flags → layout (in this order):
    0x0002 pos  xyz (12)   0x0008 pos xyzw (16)   0x1000 bone index u32 (4)
    0x0010 normal xyz (12) 0x0040 colour RGBA8 (4) 0x0100 uv (8)
    0x0142 = 24 B static, 0x0112 = 32 B lit static, 0x1116 = 36 B,
    0x1118 = 40 B skinned.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

MAGIC = b"OME\0"


@dataclass
class Block:
    offset: int
    flags: int
    stride: int
    positions: List[Tuple[float, float, float]]
    normals: List[Tuple[float, float, float]]
    uvs: List[Tuple[float, float]]
    colors: List[Tuple[int, int, int, int]]
    bones: List[int]
    strip: List[int]
    submesh: int = -1
    tex_slot: int = -1

    def triangles(self):
        s = self.strip
        for i in range(len(s) - 2):
            a, b, c = s[i], s[i + 1], s[i + 2]
            if a == b or b == c or a == c:
                continue
            yield (a, c, b) if i & 1 else (a, b, c)


@dataclass
class Mesh:
    blocks: List[Block] = field(default_factory=list)
    tex_ids: List[int] = field(default_factory=list)
    format_hint: int = 0


def vertex_layout(flags: int):
    """[(name, struct fmt, size)] in file order for the given flags."""
    lay = []
    if flags & 0x2:
        lay.append(("pos", "<3f", 12))
    if flags & 0x8:
        lay.append(("pos4", "<4f", 16))
    if flags & 0x1000:
        lay.append(("bone", "<I", 4))
    if flags & 0x10:
        lay.append(("nrm", "<3f", 12))
    if flags & 0x40:
        lay.append(("col", "<4B", 4))
    if flags & 0x100:
        lay.append(("uv", "<2f", 8))
    return lay


def parse(d: bytes) -> Mesh:
    if d[:4] != MAGIC:
        raise ValueError("not OME")
    hdr = struct.unpack_from("<8I", d, 0)
    odbp = hdr[2]
    if d[odbp:odbp + 4] != b"ODBP":
        raise ValueError("no ODBP")
    doff, n, size = struct.unpack_from("<III", d, odbp + 4)
    base = odbp + 0x20
    mesh = Mesh(format_hint=hdr[6])
    # texture-id list sometimes sits between the records and the first block
    # (u16 list, zero-terminated); keep it opaque but record it.
    # Blocks
    pos = odbp + doff
    end = min(odbp + size, len(d))
    blocks = []
    while pos + 0x20 <= end:
        t, fl, voff, nv, ioff, ni, _z0, _z1 = struct.unpack_from("<8I", d, pos)
        if t == 5 and base + voff == pos + 0x20 and 0 < nv < 1_000_000 and ioff > voff and base + ioff + ni * 2 <= len(d):
            lay = vertex_layout(fl)
            stride = sum(s for _, _, s in lay)
            if stride * nv != ioff - voff:
                stride = (ioff - voff) // nv
            blk = Block(pos - base, fl, stride, [], [], [], [], [], [])
            vp = base + voff
            for _ in range(nv):
                o = vp
                for name, fmt, sz in lay:
                    v = struct.unpack_from(fmt, d, o)
                    o += sz
                    if name == "pos":
                        blk.positions.append(v)
                    elif name == "pos4":
                        blk.positions.append(v[:3])
                    elif name == "nrm":
                        blk.normals.append(v)
                    elif name == "uv":
                        blk.uvs.append(v)
                    elif name == "col":
                        blk.colors.append(v)
                    elif name == "bone":
                        blk.bones.append(v[0])
                vp += stride
            blk.strip = list(struct.unpack_from(f"<{ni}H", d, base + ioff))
            blocks.append(blk)
            pos = (base + ioff + ni * 2 + 0xF) & ~0xF
        else:
            pos += 0x10
    # Sub-mesh records → assign to blocks
    by_off = {b.offset: i for i, b in enumerate(blocks)}
    recs = []
    i = 0
    while odbp + 0x80 + (i + 1) * 0x40 <= odbp + doff:
        r = struct.unpack_from("<8I", d, odbp + 0x80 + i * 0x40)
        if r[0] in by_off and r[7] & 0xFFFF == 1:
            recs.append((by_off[r[0]], i, r[2]))
        i += 1
    recs.sort()
    for k, (bi, si, tex) in enumerate(recs):
        nxt = recs[k + 1][0] if k + 1 < len(recs) else len(blocks)
        for j in range(bi, nxt):
            blocks[j].submesh = si
            blocks[j].tex_slot = tex
    mesh.blocks = blocks
    return mesh


def write_obj(path: str, mesh: Mesh, name: str = "mesh", flip_v: bool = True, mtllib: Optional[str] = None) -> int:
    """Write OBJ; returns triangle count."""
    out = [f"# Yakuza OME export: {name}", f"# format hint {mesh.format_hint:#x}", ""]
    if mtllib:
        out.append(f"mtllib {mtllib}")
    vbase = 1
    tris = 0
    for bi, b in enumerate(mesh.blocks):
        out.append(f"g {name}_sm{b.submesh}_b{bi}")
        out.append(f"usemtl tex{b.tex_slot}")
        for p in b.positions:
            out.append(f"v {p[0]:.6f} {p[1]:.6f} {p[2]:.6f}")
        has_n = len(b.normals) == len(b.positions)
        has_t = len(b.uvs) == len(b.positions)
        for nrm in b.normals:
            out.append(f"vn {nrm[0]:.6f} {nrm[1]:.6f} {nrm[2]:.6f}")
        for uv in b.uvs:
            out.append(f"vt {uv[0]:.6f} {(1.0 - uv[1]) if flip_v else uv[1]:.6f}")
        for a, bb, c in b.triangles():
            def ref(i):
                i += vbase
                if has_t and has_n:
                    return f"{i}/{i}/{i}"
                if has_t:
                    return f"{i}/{i}"
                if has_n:
                    return f"{i}//{i}"
                return str(i)
            out.append(f"f {ref(a)} {ref(bb)} {ref(c)}")
            tris += 1
        vbase += len(b.positions)
    with open(path, "w") as fh:
        fh.write("\n".join(out) + "\n")
    return tris


def render_wire(path: str, mesh: Mesh, size: int = 512, axis: str = "xy") -> None:
    """Debug: orthographic wireframe PNG (no deps)."""
    import txbp
    pts = [p for b in mesh.blocks for p in b.positions]
    if not pts:
        return
    ax = {"x": 0, "y": 1, "z": 2}
    ia, ib = ax[axis[0]], ax[axis[1]]
    mn = [min(p[i] for p in pts) for i in range(3)]
    mx = [max(p[i] for p in pts) for i in range(3)]
    span = max(mx[ia] - mn[ia], mx[ib] - mn[ib], 1e-6)
    sc = (size - 20) / span
    img = bytearray(b"\x00\x00\x00\xff" * size * size)

    def put(x, y):
        if 0 <= x < size and 0 <= y < size:
            o = (y * size + x) * 4
            img[o:o + 3] = b"\xff\xff\xff"

    def line(x0, y0, x1, y1):
        n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        for k in range(n + 1):
            t = k / n
            put(int(x0 + (x1 - x0) * t), int(y0 + (y1 - y0) * t))

    def proj(p):
        return 10 + (p[ia] - mn[ia]) * sc, size - 10 - (p[ib] - mn[ib]) * sc

    for b in mesh.blocks:
        P = [proj(p) for p in b.positions]
        for a, bb, c in b.triangles():
            for (u, v) in ((a, bb), (bb, c), (c, a)):
                line(*P[u], *P[v])
    txbp.write_png(path, size, size, bytes(img))


# ---------------------------------------------------------------------------
# skeleton helpers (vertex bone bytes; the OMZ skeleton itself is not parsed)
# ---------------------------------------------------------------------------
# Body rigs: 12/17 = upper arms, 13/18 = elbows, 14/19 = forearms (wrist end
# is the lowest part of that cluster); negative x is the model's left.
# Shared hands (ENECOMMON.DAT 13/14) have their own 18-bone rig; bone 0 is
# the wrist cuff.
BODY_WRIST_BONES = {"L": 14, "R": 19}


def _verts_with_primary_bone(mesh: "Mesh", bone: int):
    out = []
    for b in mesh.blocks:
        if len(b.bones) != len(b.positions):
            continue
        for p, bb in zip(b.positions, b.bones):
            if (bb & 0xFF) == bone:
                out.append(p)
    return out


def body_wrists(mesh: "Mesh"):
    """{'L': (x,y,z), 'R': (x,y,z)} — distal end of each forearm cluster."""
    out = {}
    for side, bone in BODY_WRIST_BONES.items():
        pts = _verts_with_primary_bone(mesh, bone)
        if len(pts) < 3:
            continue
        pts.sort(key=lambda p: p[1])
        low = pts[:max(3, len(pts) // 5)]
        out[side] = tuple(sum(c) / len(low) for c in zip(*low))
    return out


def hand_cuff(mesh: "Mesh"):
    """(x,y,z) of a shared hand's wrist cuff (top of its bone-0 cluster)."""
    pts = _verts_with_primary_bone(mesh, 0)
    if len(pts) < 3:
        return None
    pts.sort(key=lambda p: -p[1])
    top = pts[:max(3, int(len(pts) * 0.3))]
    return tuple(sum(c) / len(top) for c in zip(*top))
