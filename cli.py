"""Yakuza (PS2, SLUS-21348) extractor CLI.

  python3 cli.py list     ISO                 # every file inside the four CVMs
  python3 cli.py extract  ISO --out DIR       # dump CVM contents, in-game tree
  python3 cli.py census   ISO                 # extension / magic census
"""
from __future__ import annotations

import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cvm            # noqa: E402
import ogredir        # noqa: E402
from iso9660 import IsoImage  # noqa: E402


def _root_files(iso):
    fl = iso.files() if callable(iso.files) else iso.files
    out = {}
    for f in (fl.values() if isinstance(fl, dict) else fl):
        if isinstance(f, str):
            f = fl[f]
        out[f.path] = f
    return out


def load_volumes(iso_path):
    iso = IsoImage(iso_path)
    root = _root_files(iso)
    vols = []
    for dirname, cvmname, _tag in ogredir.VOLUMES:
        d, c = root[dirname], root[cvmname]
        blob = bytes(iso.mm[d.lba * 2048:d.lba * 2048 + d.size])
        entries = ogredir.parse(blob)
        base = c.lba * 2048
        info = cvm.probe(bytes(iso.mm[base:base + 0x1800 + 17 * 2048]))
        vols.append((cvmname, base, entries, info))
    return iso, vols


def cmd_list(a):
    iso, vols = load_volumes(a.iso)
    n = tot = 0
    for cvmname, base, entries, info in vols:
        print(f"# {cvmname}: {info['builder']} scrambled={info['scrambled']} size={info['size']}")
        for e in ogredir.files(entries):
            print(f"{e.size:10d}  {e.lba:8d}  {cvmname}:{e.path}")
            n += 1
            tot += e.size
    print(f"# {n} files, {tot} bytes", file=sys.stderr)


def cmd_extract(a):
    iso, vols = load_volumes(a.iso)
    n = 0
    for cvmname, base, entries, info in vols:
        for e in ogredir.files(entries):
            if a.filter and a.filter.lower() not in e.path.lower():
                continue
            dst = os.path.join(a.out, e.path.lstrip("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            off = base + e.cvm_offset
            with open(dst, "wb") as fh:
                fh.write(iso.mm[off:off + e.size])
            n += 1
            if n % 1000 == 0:
                print(n, e.path, flush=True)
    print(f"extracted {n} files to {a.out}")


def cmd_census(a):
    iso, vols = load_volumes(a.iso)
    c = collections.Counter()
    ex = {}
    for cvmname, base, entries, info in vols:
        for e in ogredir.files(entries):
            ext = os.path.splitext(e.path)[1].lower()
            off = base + e.cvm_offset
            magic = bytes(iso.mm[off:off + 4])
            key = magic.decode() if all(32 <= b < 127 for b in magic) else magic.hex()
            c[(ext, key)] += 1
            ex.setdefault((ext, key), e.path)
    for (ext, key), cnt in sorted(c.items(), key=lambda kv: -kv[1]):
        print(f"{ext:10s} {key:10s} {cnt:6d}  {ex[(ext, key)]}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    q = sp.add_parser("list"); q.add_argument("iso"); q.set_defaults(fn=cmd_list)
    q = sp.add_parser("extract"); q.add_argument("iso"); q.add_argument("--out", required=True)
    q.add_argument("--filter", help="only paths containing this substring"); q.set_defaults(fn=cmd_extract)
    q = sp.add_parser("census"); q.add_argument("iso"); q.set_defaults(fn=cmd_census)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
