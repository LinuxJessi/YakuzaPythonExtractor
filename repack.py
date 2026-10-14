"""repack.py — write modified files back into the disc image.

  python3 repack.py patch   ISO --file /MEDIA/PLAYER/PLAYER0.DAT --with new.dat [--out patched.iso]
  python3 repack.py texture ISO --file /MEDIA/PLAYER/PLAYER0.DAT --leaf /0/0 --index 0 \\
                                --png suit_edit.png [--out patched.iso]
  python3 repack.py verify  ISO

How it works
------------
Files live inside the four CVMs at ``0x1800 + lba * 2048``; the engine
finds them through ``OGREDIRn.BIN`` (§3 of docs/FORMATS.md), never through
the scrambled ISO directories. So a replacement needs two writes:

1. the new body, zero-padded, over the old extent — it must fit in the
   sectors the original occupied (same sector count or fewer);
2. the new byte size in the file's 48-byte OGREDIR record, which lives in
   an AVLZ block, so the directory table is rebuilt with ``avlz.compress``
   and must fit in OGREDIRn.BIN's original sector footprint (it always has
   so far: the rebuilt table is within a few hundred bytes of SEGA's).

``texture`` goes one level deeper: it inflates the AVLZ leaf, swaps one
TXBP entry's pixels (and CLUT, for indexed formats) with the PNG — same
size and format as the original, quantised with Pillow when the texture is
4/8-bit — recompresses the leaf, re-nests it in the container with every
offset preserved, and then runs ``patch``. Containers keep their original
slot sizes, so the new leaf must compress to no more than the old one;
textures with little detail usually do, photographic ones may not.

``--out`` copies the ISO first; without it the ISO is patched in place.
"""
from __future__ import annotations

import argparse
import os
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import avlz            # noqa: E402
import containers      # noqa: E402
import ogredir         # noqa: E402
import txbp            # noqa: E402
from iso9660 import IsoImage  # noqa: E402

SECTOR = 2048


def _root(iso):
    fl = iso.files() if callable(iso.files) else iso.files
    out = {}
    for f in (fl.values() if isinstance(fl, dict) else fl):
        if isinstance(f, str):
            f = fl[f]
        out[f.path] = f
    return out


def _volume_for(iso_path, game_path):
    """(dir entry, cvm entry, dir blob, entries, Entry) for a /MEDIAn/... path."""
    iso = IsoImage(iso_path)
    root = _root(iso)
    top = game_path.strip("/").split("/")[0].upper()
    for dirname, cvmname, tag in ogredir.VOLUMES:
        if tag == top:
            d, c = root[dirname], root[cvmname]
            blob = bytes(iso.mm[d.lba * SECTOR:d.lba * SECTOR + d.size])
            entries = ogredir.parse(blob)
            for e in entries:
                if e.path.upper() == "/" + game_path.strip("/").upper():
                    iso.close() if hasattr(iso, "close") else None
                    return d, c, blob, e
            raise SystemExit(f"{game_path} not in {dirname}")
    raise SystemExit(f"no volume for {game_path}")


def rebuild_ogredir(blob: bytes, target_path: str, new_size: int) -> bytes:
    """Rewrite one record's size and recompress every directory block.

    Output keeps the header, the 64-byte directory list and block order; only
    block bytes change, so block offsets are recomputed and patched into the
    directory list.
    """
    ndirs, _ = struct.unpack_from("<II", blob, 0x40)
    dirs = []
    for i in range(ndirs):
        e = blob[0x80 + i * 0x40:0x80 + (i + 1) * 0x40]
        dirs.append((e[:0x38].split(b"\0")[0].decode(), *struct.unpack_from("<II", e, 0x38)))
    out = bytearray(blob[:0x80 + ndirs * 0x40])
    want_dir, want_name = target_path.rsplit("/", 1)
    want_dir += "/"
    found = False
    for i, (dname, cnt, off) in enumerate(dirs):
        raw = avlz.unwrap(blob, off)
        raw = bytearray(raw)
        if dname.upper() == want_dir.upper():
            for k in range(cnt):
                r = 20 + k * 48
                name = raw[r + 18:r + 48].split(b"\0")[0].decode()
                if name.upper() == want_name.upper():
                    struct.pack_into("<I", raw, r + 4, new_size)
                    found = True
        # blocks are 0x10-aligned like the originals
        while len(out) % 0x10:
            out.append(0)
        new_off = len(out)
        struct.pack_into("<II", out, 0x80 + i * 0x40 + 0x38, cnt, new_off)
        out += avlz.compress(bytes(raw))
    if not found:
        raise SystemExit(f"{target_path} not found in OGREDIR")
    struct.pack_into("<II", out, 0x40, ndirs, new_off)
    while len(out) % 0x10:
        out.append(0)
    return bytes(out)


def cmd_patch(a):
    iso_path = a.out or a.iso
    if a.out:
        shutil.copyfile(a.iso, a.out)
    d, c, blob, e = _volume_for(iso_path, a.file)
    new = open(a.with_, "rb").read()
    old_sectors = (e.size + SECTOR - 1) // SECTOR
    if len(new) > old_sectors * SECTOR:
        raise SystemExit(f"{a.file}: new body {len(new)} B exceeds the original {old_sectors} sectors "
                         f"({old_sectors * SECTOR} B). Shrink it or pick a same-size asset.")
    dir_blob = rebuild_ogredir(blob, e.path, len(new))
    dir_sectors = (d.size + SECTOR - 1) // SECTOR
    if len(dir_blob) > dir_sectors * SECTOR:
        raise SystemExit("rebuilt OGREDIR does not fit its original sectors")
    with open(iso_path, "r+b") as fh:
        fh.seek(c.lba * SECTOR + e.cvm_offset)
        fh.write(new + b"\0" * (old_sectors * SECTOR - len(new)))
        fh.seek(d.lba * SECTOR)
        fh.write(dir_blob + b"\0" * (dir_sectors * SECTOR - len(dir_blob)))
    print(f"patched {a.file}: {e.size} -> {len(new)} bytes; OGREDIR {d.size} -> {len(dir_blob)} bytes "
          f"-> {iso_path}")


def _png_rgba(path):
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("texture import needs Pillow: pip install Pillow")
    im = Image.open(path).convert("RGBA")
    return im


def encode_texture(t: txbp.Texture, png_path: str) -> txbp.Texture:
    """New Texture with ``t``'s format/size and the PNG's pixels."""
    from PIL import Image
    im = _png_rgba(png_path)
    if im.size != (t.width, t.height):
        raise SystemExit(f"PNG is {im.size}, texture is {t.width}x{t.height}")
    if t.fmt == txbp.FMT_32BIT:
        px = bytearray()
        for r, g, b, a in im.getdata():
            px += bytes((r, g, b, (a + 1) // 2))
        return txbp.Texture(t.index, t.width, t.height, t.fmt, t.flags, t.tex_id, b"", bytes(px))
    ncol = 16 if t.fmt == txbp.FMT_4BIT else 256
    # quantise RGB, keep alpha per palette entry (mean alpha of its pixels)
    rgb = im.convert("RGB").quantize(colors=ncol, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = (rgb.getpalette() or [])[:ncol * 3]
    pal += [0] * (ncol * 3 - len(pal))
    idx = list(rgb.getdata())
    alpha = list(im.getchannel("A").getdata())
    asum = [0] * ncol
    acnt = [0] * ncol
    for i, a in zip(idx, alpha):
        asum[i] += a
        acnt[i] += 1
    clut = bytearray()
    for k in range(ncol):
        a = (asum[k] // acnt[k]) if acnt[k] else 255
        clut += bytes((pal[k * 3], pal[k * 3 + 1], pal[k * 3 + 2], (a + 1) // 2))
    if t.fmt == txbp.FMT_8BIT:          # store in CSM1 order (decoder swaps back)
        entries = [bytes(clut[i:i + 4]) for i in range(0, 1024, 4)]
        for g in range(8):
            for j in range(8):
                x, y = g * 32 + 8 + j, g * 32 + 16 + j
                entries[x], entries[y] = entries[y], entries[x]
        clut = b"".join(entries)
        pixels = bytes(idx)
    else:
        pixels = bytearray()
        for i in range(0, len(idx), 2):
            lo = idx[i]
            hi = idx[i + 1] if i + 1 < len(idx) else 0
            pixels.append(lo | (hi << 4))
        pixels = bytes(pixels)
    return txbp.Texture(t.index, t.width, t.height, t.fmt, t.flags, t.tex_id, bytes(clut), pixels)


def build_txbp(original: bytes, textures) -> bytes:
    """Re-emit a TXBP with the given Texture list (entries copied from original)."""
    count = struct.unpack_from("<I", original, 4)[0]
    out = bytearray(original[:0x20])
    pos = 0x20
    largest = 0
    for i in range(count):
        ent = bytearray(original[pos:pos + 0x20])
        size, w, h, fmt = struct.unpack_from("<4I", ent, 0)
        flags = struct.unpack_from("<I", ent, 0x14)[0]
        t = textures[i]
        if flags & 0x400:
            # mip chain: keep the original payload wholesale unless replaced
            payload = original[pos + 0x20:pos + 0x20 + size]
            if t.pixels != txbp.parse(original)[i].pixels:
                raise SystemExit("replacing mipmapped textures is not supported yet")
        else:
            payload = t.clut + t.pixels
            if len(payload) != size:
                raise SystemExit(f"entry {i}: payload {len(payload)} != {size}")
        out += ent + payload
        largest = max(largest, len(payload))
        pos += 0x20 + size
    struct.pack_into("<I", out, 8, largest)
    return bytes(out)


def replace_leaf(blob: bytes, leaf_path: str, new_leaf: bytes) -> bytes:
    """Return ``blob`` with the leaf at ``leaf_path`` (e.g. '/0/0') replaced in place."""
    segs = [int(s) for s in leaf_path.strip("/").split("/")]
    data = bytearray(blob)

    def rec(base, size, depth):
        sub = bytes(data[base:base + size])
        ents = containers.entries(sub)
        if ents is None or depth >= len(segs):
            raise SystemExit(f"{leaf_path}: not a container at depth {depth}")
        off, sz, _i, _t = ents[segs[depth]]
        if depth == len(segs) - 1:
            if len(new_leaf) > sz:
                raise SystemExit(f"new leaf is {len(new_leaf)} B but the slot holds {sz} B "
                                 "(recompressed texture is larger than the original — simplify the image)")
            data[base + off:base + off + sz] = new_leaf + b"\0" * (sz - len(new_leaf))
            return
        rec(base + off, sz, depth + 1)

    rec(0, len(blob), 0)
    return bytes(data)


def cmd_texture(a):
    iso = IsoImage(a.iso)
    _d, c, _blob, e = _volume_for(a.iso, a.file)
    body = bytes(iso.mm[c.lba * SECTOR + e.cvm_offset:c.lba * SECTOR + e.cvm_offset + e.size])
    leaf = None
    for p, l in containers.leaves(body):
        if p == a.leaf:
            leaf = l
            break
    if leaf is None:
        raise SystemExit(f"leaf {a.leaf} not found in {a.file}")
    was_avlz = avlz.is_avlz(leaf)
    pack = containers.inflate(leaf)
    texs = txbp.parse(pack)
    texs[a.index] = encode_texture(texs[a.index], a.png)
    new_pack = build_txbp(pack, texs)
    new_leaf = avlz.compress(new_pack) if was_avlz else new_pack
    print(f"leaf {a.leaf}: {len(leaf)} B -> {len(new_leaf)} B ({'AVLZ' if was_avlz else 'raw'})")
    new_body = replace_leaf(body, a.leaf, new_leaf)
    tmp = a.png + ".body.bin"
    with open(tmp, "wb") as fh:
        fh.write(new_body)
    try:
        a.with_ = tmp
        cmd_patch(a)
    finally:
        os.unlink(tmp)


def cmd_verify(a):
    iso = IsoImage(a.iso)
    root = _root(iso)
    bad = 0
    for dirname, cvmname, _tag in ogredir.VOLUMES:
        d, c = root[dirname], root[cvmname]
        blob = bytes(iso.mm[d.lba * SECTOR:d.lba * SECTOR + d.size])
        entries = ogredir.parse(blob)
        n = 0
        for e in ogredir.files(entries):
            if e.cvm_offset + e.size > c.size:
                print(f"! {e.path} extends past {cvmname}")
                bad += 1
            n += 1
        print(f"{dirname}: {n} files, all inside {cvmname}" if not bad else f"{dirname}: {bad} problems")
    print("ok" if not bad else f"{bad} problems")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    q = sp.add_parser("patch"); q.add_argument("iso"); q.add_argument("--file", required=True)
    q.add_argument("--with", dest="with_", required=True); q.add_argument("--out"); q.set_defaults(fn=cmd_patch)
    q = sp.add_parser("texture"); q.add_argument("iso"); q.add_argument("--file", required=True)
    q.add_argument("--leaf", required=True); q.add_argument("--index", type=int, default=0)
    q.add_argument("--png", required=True); q.add_argument("--out"); q.set_defaults(fn=cmd_texture)
    q = sp.add_parser("verify"); q.add_argument("iso"); q.set_defaults(fn=cmd_verify)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
