"""cvm.py — CRI ROFS ``.CVM`` container access.

A CVM is an ISO9660 image prefixed by a 0x1800-byte CRI header
(``"CVMH"`` + big-endian size + ``"ROFSROFSBLD Ver.1.52"``). Yakuza's
volumes have their directory sectors "scrambled" (ROFS password protection;
the well-known password is ``qi2o@9a!``) but every *file body* is stored
plain, so with the OGREDIR table we never need the ISO directory at all.
"""
from __future__ import annotations

import struct

HEADER = 0x1800
SECTOR = 2048


def probe(blob: bytes) -> dict:
    if blob[:4] != b"CVMH":
        raise ValueError("not a CVM")
    size = struct.unpack_from(">I", blob, 0x20)[0]
    build = blob[0x34:0x60].split(b"\0")[0].decode("ascii", "replace")
    pvd = blob[HEADER + 16 * SECTOR:HEADER + 16 * SECTOR + 6]
    return {"size": size, "builder": build,
            "scrambled": pvd != b"\x01CD001"}
