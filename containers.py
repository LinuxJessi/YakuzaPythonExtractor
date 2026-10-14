"""containers.py — the recursive "count + 16-byte entry table" container.

Nearly every .DAT/.ARC/.BIN on the disc is this shape, nested several
levels deep::

  0x00  u32 count
  0x04  char tag[4] or 0     ("TLFD", "EGTS", "ARTS" seen)
  0x08  8 bytes pad
  0x10  count × { u32 offset (from container start), u32 size, u32 id, char tag[4] }

Leaves are usually AVLZ blocks wrapping an OME mesh or a TXBP texture pack,
or small raw records ("Ver 1.x" placement tables, OSOS/OVRP/RELM/OCSP, text).
"""
from __future__ import annotations

import struct
from typing import Iterator, List, Optional, Tuple

import avlz

MAX_ENTRIES = 8192


def entries(blob: bytes) -> Optional[List[Tuple[int, int, int, int]]]:
    """Entry tuples (off, size, id, tag) if ``blob`` parses as a container."""
    if len(blob) < 16:
        return None
    cnt = struct.unpack_from("<I", blob, 0)[0]
    if not (1 <= cnt <= MAX_ENTRIES) or 16 + cnt * 16 > len(blob):
        return None
    ents = [struct.unpack_from("<IIII", blob, 16 + i * 16) for i in range(cnt)]
    hdr_end = 16 + cnt * 16
    if not all(e[0] >= hdr_end and e[0] + e[1] <= len(blob) for e in ents):
        return None
    return ents


def leaves(blob: bytes, path: str = "", depth: int = 0, max_depth: int = 8) -> Iterator[Tuple[str, bytes]]:
    """Yield (path, bytes) for every non-container leaf, depth-first."""
    ents = entries(blob) if depth < max_depth else None
    if ents is None:
        if blob:
            yield path, blob
        return
    for i, (off, size, _id, _tag) in enumerate(ents):
        if size == 0:
            continue
        yield from leaves(blob[off:off + size], f"{path}/{i}", depth + 1, max_depth)


def inflate(leaf: bytes) -> bytes:
    """Transparently decompress an AVLZ leaf."""
    return avlz.unwrap(leaf) if avlz.is_avlz(leaf) else leaf
