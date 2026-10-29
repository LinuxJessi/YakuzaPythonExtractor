"""avlz.py — AVLZ decompressor (Amusement Vision LZ).

Classic Okumura LZSS: 4096-byte ring buffer (zero-initialised, write cursor
starts at 0xFEE), flag byte covers the next 8 tokens LSB-first, bit=1 is a
literal byte, bit=0 is a 2-byte match ``lo, hi`` where
``offset = lo | ((hi & 0xF0) << 4)`` and ``length = (hi & 0x0F) + 3``.

Container: ``"AVLZ" | u32 decompressed size | u32 compressed size (counted
from the magic, header included) | stream``.
"""
from __future__ import annotations

import struct

MAGIC = b"AVLZ"
N, F = 4096, 18


def decompress(src: bytes, outlen: int) -> bytes:
    ring = bytearray(N)
    r = N - F
    out = bytearray()
    p = flags = nbits = 0
    while len(out) < outlen:
        if nbits == 0:
            flags = src[p]
            p += 1
            nbits = 8
        bit = flags & 1
        flags >>= 1
        nbits -= 1
        if bit:
            c = src[p]
            p += 1
            out.append(c)
            ring[r] = c
            r = (r + 1) % N
        else:
            lo, hi = src[p], src[p + 1]
            p += 2
            off = lo | ((hi & 0xF0) << 4)
            ln = (hi & 0x0F) + 3
            for k in range(ln):
                c = ring[(off + k) % N]
                out.append(c)
                ring[r] = c
                r = (r + 1) % N
    return bytes(out[:outlen])


def unwrap(blob: bytes, off: int = 0) -> bytes:
    """Decode an AVLZ block that starts at ``blob[off]``."""
    if blob[off:off + 4] != MAGIC:
        raise ValueError(f"no AVLZ magic at {off:#x}")
    dec, comp = struct.unpack_from("<II", blob, off + 4)
    return decompress(blob[off + 12:off + comp], dec)


def is_avlz(blob: bytes) -> bool:
    return blob[:4] == MAGIC


def compress(data: bytes) -> bytes:
    """LZSS encoder producing a stream :func:`decompress` accepts.

    Greedy longest-match with a 3-byte-prefix index over the ring buffer
    (the decoder copies byte-by-byte, so overlapping matches are legal and
    we emulate that when verifying a candidate). Not byte-identical to
    SEGA's tool but valid, and within a few percent of its ratio."""
    N_, F_ = N, F
    ring = bytearray(N_)
    r = N_ - F_
    out = bytearray()
    tokens = []
    flagbits = nbits = 0
    index = {}              # 3-byte prefix -> list of ring offsets (most recent last)
    n = len(data)
    pos = 0

    def put(c):
        nonlocal r
        ring[r] = c
        r = (r + 1) % N_

    def remember(start):
        # index the prefix that now starts at ring offset `start`
        key = bytes((ring[start], ring[(start + 1) % N_], ring[(start + 2) % N_]))
        lst = index.setdefault(key, [])
        lst.append(start)
        if len(lst) > 192:
            del lst[0]

    while pos < n:
        best_len, best_off = 0, 0
        maxlen = min(F_, n - pos)
        if maxlen >= 3:
            key = data[pos:pos + 3]
            for off in reversed(index.get(key, ())):
                ln = 0
                rr = r
                # emulate decoder: source may run into bytes we are writing
                tmp = {}
                while ln < maxlen:
                    src = (off + ln) % N_
                    c = tmp.get(src, ring[src])
                    if c != data[pos + ln]:
                        break
                    tmp[(rr + ln) % N_] = c
                    ln += 1
                if ln > best_len:
                    best_len, best_off = ln, off
                    if ln == maxlen:
                        break
        if best_len >= 3:
            tokens.append(bytes((best_off & 0xFF, ((best_off >> 4) & 0xF0) | (best_len - 3))))
            for k in range(best_len):
                start = (r - 2) % N_
                put(data[pos + k])
                if pos + k >= 2:
                    remember(start)
            pos += best_len
        else:
            tokens.append(bytes((data[pos],)))
            flagbits |= 1 << nbits
            start = (r - 2) % N_
            put(data[pos])
            if pos >= 2:
                remember(start)
            pos += 1
        nbits += 1
        if nbits == 8:
            out.append(flagbits)
            out += b"".join(tokens)
            tokens, flagbits, nbits = [], 0, 0
    if nbits:
        out.append(flagbits)
        out += b"".join(tokens)
    return MAGIC + struct.pack("<II", len(data), 12 + len(out)) + bytes(out)
