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
    """Greedy LZSS encoder producing a stream :func:`decompress` accepts.

    Not byte-identical to SEGA's tool (unknown match policy) but valid, and
    sufficient to rebuild OGREDIR tables after editing."""
    N_, F_ = N, F
    ring = bytearray(N_)
    r = N_ - F_
    out = bytearray()
    pos = 0
    tokens: list[bytes] = []
    flagbits = 0
    nbits = 0
    # dictionary: ring content indexed by position; brute-force search
    while pos < len(data):
        best_len, best_off = 0, 0
        maxlen = min(F_, len(data) - pos)
        if maxlen >= 3:
            for off in range(N_):
                ln = 0
                while ln < maxlen and ring[(off + ln) % N_] == data[pos + ln]:
                    ln += 1
                    # matches may overlap the write cursor only if they
                    # replicate correctly; the decoder copies byte-by-byte
                    # from a ring that is being written, so emulate that:
                    if (off + ln) % N_ == (r + ln) % N_:
                        pass
                if ln > best_len:
                    best_len, best_off = ln, off
                    if ln == maxlen:
                        break
        if best_len >= 3:
            # verify by emulating the decoder's byte-at-a-time copy
            ok = True
            tmp = ring[:]
            rr = r
            for k in range(best_len):
                c = tmp[(best_off + k) % N_]
                if c != data[pos + k]:
                    ok = False
                    break
                tmp[rr] = c
                rr = (rr + 1) % N_
            if not ok:
                best_len = 0
        if best_len >= 3:
            tokens.append(bytes((best_off & 0xFF, ((best_off >> 4) & 0xF0) | (best_len - 3))))
            for k in range(best_len):
                ring[r] = data[pos + k]
                r = (r + 1) % N_
            pos += best_len
        else:
            tokens.append(bytes((data[pos],)))
            flagbits |= 1 << nbits
            ring[r] = data[pos]
            r = (r + 1) % N_
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
