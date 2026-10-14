"""txbp.py — TXBP texture packs → RGBA / PNG.

Layout (all little-endian)::

  0x00  "TXBP"
  0x04  u32 count
  0x08  u32 largest per-texture payload (CLUT + pixels) — staging-buffer size
  0x0c  pad to 0x20
  0x20  count × { 32-byte entry, then that texture's payload }:
          u32 payload size (CLUT + pixels)
          u32 width, u32 height
          u32 format   0x14 = 4-bit indexed (16-entry CLUT)
                       0x15 = 8-bit indexed (256-entry CLUT)
                       0x1d = 32-bit RGBA8888, no CLUT
          u32 CLUT format (0x1d = RGBA8888; 0xffffffff when no CLUT)
          u32 flags    0x02000000 / 0x0a000000 seen; bit 0x400 = mipmapped:
                       the texture is square (width × width) and the
                       "height" word holds the mip-level count; levels are
                       stored back-to-back after the CLUT, largest first
          u32 texture id   (what OME sub-meshes reference)
          u32 0xb26b4000 | texture id (lookup key)
  Each entry is immediately followed by its payload: CLUT (RGBA8, alpha
  0..0x80) then the pixel indices (4-bit = two pixels per byte, low nibble
  first). The next entry starts right after the payload.

8-bit CLUTs are stored in the GS "CSM1" order: inside every 32-entry group,
entries 8–15 and 16–23 are swapped.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import List

MAGIC = b"TXBP"
FMT_4BIT, FMT_8BIT, FMT_32BIT = 0x14, 0x15, 0x1d


@dataclass
class Texture:
    index: int
    width: int
    height: int
    fmt: int
    flags: int
    tex_id: int
    clut: bytes
    pixels: bytes

    def rgba(self, csm1: bool = True, scale_alpha: bool = True) -> bytes:
        w, h = self.width, self.height
        out = bytearray(w * h * 4)
        if self.fmt in (FMT_4BIT, FMT_8BIT):
            pal = [self.clut[i:i + 4] for i in range(0, len(self.clut), 4)]
            if self.fmt == FMT_8BIT and csm1 and len(pal) == 256:
                for g in range(8):
                    for j in range(8):
                        a, b = g * 32 + 8 + j, g * 32 + 16 + j
                        pal[a], pal[b] = pal[b], pal[a]
            if scale_alpha:
                pal = [bytes((p[0], p[1], p[2], min(p[3] * 2, 255))) for p in pal]
            if self.fmt == FMT_8BIT:
                for i in range(w * h):
                    out[i * 4:i * 4 + 4] = pal[self.pixels[i]]
            else:
                for i in range(w * h):
                    b = self.pixels[i >> 1]
                    out[i * 4:i * 4 + 4] = pal[(b & 0xF) if (i & 1) == 0 else (b >> 4)]
        elif self.fmt == FMT_32BIT:
            px = self.pixels
            for i in range(w * h):
                r, g, b, a = px[i * 4:i * 4 + 4]
                out[i * 4:i * 4 + 4] = bytes((r, g, b, min(a * 2, 255) if scale_alpha else a))
        else:
            raise ValueError(f"unknown TXBP format {self.fmt:#x}")
        return bytes(out)


def clut_bytes(fmt: int) -> int:
    return {FMT_4BIT: 64, FMT_8BIT: 1024}.get(fmt, 0)


def pixel_bytes(fmt: int, w: int, h: int) -> int:
    if fmt not in (FMT_4BIT, FMT_8BIT, FMT_32BIT):
        raise ValueError(f"unknown TXBP pixel format {fmt:#x}")
    return {FMT_4BIT: (w * h + 1) // 2, FMT_8BIT: w * h, FMT_32BIT: w * h * 4}[fmt]


def parse(blob: bytes) -> List[Texture]:
    if blob[:4] != MAGIC:
        raise ValueError("not TXBP")
    count = struct.unpack_from("<I", blob, 4)[0]
    texs = []
    pos = 0x20
    for i in range(count):
        size, w, h, fmt, _cfmt, flags, tex_id, _key = struct.unpack_from("<8I", blob, pos)
        pos += 0x20
        cb = clut_bytes(fmt)
        if flags & 0x400:            # mip chain, square
            levels, h = h, w
            pb = sum(pixel_bytes(fmt, w >> k, h >> k) for k in range(levels))
        else:
            pb = pixel_bytes(fmt, w, h)
        if cb + pb != size:
            raise ValueError(f"tex {i}: payload {size:#x} != clut {cb:#x} + pixels {pb:#x} ({w}x{h} fmt {fmt:#x} flags {flags:#x})")
        clut = blob[pos:pos + cb]
        pixels = blob[pos + cb:pos + cb + pixel_bytes(fmt, w, h)]
        texs.append(Texture(i, w, h, fmt, flags, tex_id, clut, pixels))
        pos += size
    return texs


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)


def write_png(path, width: int, height: int, rgba: bytes) -> None:
    raw = b"".join(b"\x00" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    png = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)
