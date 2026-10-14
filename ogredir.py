"""ogredir.py — OGREDIR*.BIN directory tables.

Yakuza never walks the ISO9660 directories inside its CVMs (those sectors
are ROFS-scrambled). Instead each ``OGREn.CVM`` ships with a plaintext
``OGREDIRn.BIN`` that the engine loads at boot: a flat list of directories,
each pointing at an AVLZ-compressed block of 48-byte records that carry the
ISO extent LBA, byte size, flags and name of every entry.

Layout::

  0x00  7 bytes   build timestamp (yy mm dd hh mm ss ...) + zero pad
  0x40  u32       number of directory entries
  0x44  u32       offset of the last AVLZ block
  0x80  n × 64    {name[0x38] NUL-padded, u32 entry_count, u32 block_offset}
  ...   AVLZ blocks, each decoding to:
          u32 entry_count, u32 entry_count, u32 dir_lba, "#DirLst#",
          entry_count × 48 {u32 0, u32 size, u32 0, u32 lba,
                             u8 flags (2 = directory), u8 name_hash,
                             char name[30]}

``lba`` is the ISO9660 extent inside the CVM; the byte offset within the
CVM file is ``0x1800 + lba * 2048`` (the CVM header is 0x1800 bytes).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator, List

import avlz

CVM_HEADER = 0x1800
SECTOR = 2048

# OGREDIRn.BIN <-> OGREn.CVM <-> in-game root, as the engine registers them
VOLUMES = [
    ("OGREDIR.BIN", "OGRE.CVM", "MEDIA"),
    ("OGREDIR2.BIN", "OGRE2.CVM", "MEDIA2"),
    ("OGREDIR3.BIN", "OGRE3.CVM", "MEDIA3"),
    ("OGREDIR4.BIN", "OGRE4.CVM", "MEDIA4"),
]


@dataclass(frozen=True)
class Entry:
    path: str      # "/MEDIA/MAP/MAP_ADV.DAT"
    lba: int
    size: int
    is_dir: bool
    name_hash: int

    @property
    def cvm_offset(self) -> int:
        return CVM_HEADER + self.lba * SECTOR


def parse(blob: bytes) -> List[Entry]:
    ndirs, _last = struct.unpack_from("<II", blob, 0x40)
    out: List[Entry] = []
    for i in range(ndirs):
        e = blob[0x80 + i * 0x40:0x80 + (i + 1) * 0x40]
        dname = e[:0x38].split(b"\0")[0].decode("ascii")
        cnt, off = struct.unpack_from("<II", e, 0x38)
        blk = avlz.unwrap(blob, off)
        hc, hc2, dlba = struct.unpack_from("<III", blk, 0)
        if blk[12:20] != b"#DirLst#" or hc != cnt:
            raise ValueError(f"bad #DirLst# block for {dname}")
        for k in range(cnt):
            r = blk[20 + k * 48:20 + (k + 1) * 48]
            _z0, size, _z1, lba = struct.unpack_from("<IIII", r, 0)
            flags, nh = r[16], r[17]
            name = r[18:].split(b"\0")[0].decode("ascii")
            if name in (".", ".."):
                continue
            out.append(Entry(dname + name, lba, size, bool(flags & 2), nh))
    return out


def files(entries: List[Entry]) -> Iterator[Entry]:
    return (e for e in entries if not e.is_dir)
