"""Yakuza (PS2, SLUS-21348) extractor CLI.

  python3 cli.py probe    ISO                       # is this a Yakuza disc? serial, volumes, file count
  python3 cli.py list     ISO [--volume MEDIA3]     # every file inside the four CVMs
  python3 cli.py extract  ISO --out DIR [--filter S] # dump CVM contents, in-game tree + manifest.csv
  python3 cli.py verify-dump DIR [--iso ISO]        # re-check a dump against its manifest
  python3 cli.py census   ISO                       # extension / magic census
  python3 cli.py convert  --dump DIR --out DIR [--kinds textures,models,previews,audio,sfx,movies]
                                                    # the whole browse pipeline in one go
"""
from __future__ import annotations

import argparse
import collections
import csv
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cvm            # noqa: E402
import ogredir        # noqa: E402
from iso9660 import IsoImage  # noqa: E402

__version__ = "1.1.0"

MANIFEST = "manifest.csv"
MANIFEST_FIELDS = ("volume", "path", "lba", "size", "iso_offset")
CONVERT_KINDS = ("textures", "models", "previews", "audio", "sfx", "movies")


def _root_files(iso):
    fl = iso.files() if callable(iso.files) else iso.files
    out = {}
    for f in (fl.values() if isinstance(fl, dict) else fl):
        if isinstance(f, str):
            f = fl[f]
        out[f.path] = f
    return out


def load_volumes(iso_path, only=None):
    """[(cvm name, cvm byte base, entries, probe info)] for the four volumes."""
    iso = IsoImage(iso_path)
    root = _root_files(iso)
    vols = []
    for dirname, cvmname, tag in ogredir.VOLUMES:
        if only and tag.upper() != only.upper():
            continue
        if dirname not in root or cvmname not in root:
            raise SystemExit(f"{iso_path}: {dirname} / {cvmname} missing — not a Yakuza (SLUS-21348) disc?")
        d, c = root[dirname], root[cvmname]
        blob = bytes(iso.mm[d.lba * 2048:d.lba * 2048 + d.size])
        entries = ogredir.parse(blob)
        base = c.lba * 2048
        info = cvm.probe(bytes(iso.mm[base:base + 0x1800 + 17 * 2048]))
        vols.append((cvmname, base, entries, info))
    return iso, vols


def probe(iso_path):
    """Cheap sanity check used by the GUI: serial, volumes, file count, total bytes."""
    iso = IsoImage(iso_path)
    root = _root_files(iso)
    serial = ""
    cnf = root.get("SYSTEM.CNF")
    if cnf is not None:
        text = bytes(iso.mm[cnf.lba * 2048:cnf.lba * 2048 + cnf.size]).decode("ascii", "replace")
        for line in text.splitlines():
            if line.startswith("BOOT2"):
                serial = line.split("\\")[-1].split(";")[0].strip()
    volumes = []
    files = total = 0
    for dirname, cvmname, tag in ogredir.VOLUMES:
        if dirname not in root or cvmname not in root:
            volumes.append({"tag": tag, "present": False})
            continue
        d = root[dirname]
        entries = ogredir.parse(bytes(iso.mm[d.lba * 2048:d.lba * 2048 + d.size]))
        n = sz = 0
        for e in ogredir.files(entries):
            n += 1
            sz += e.size
        files += n
        total += sz
        volumes.append({"tag": tag, "present": True, "files": n, "bytes": sz, "cvm_bytes": root[cvmname].size})
    ok = serial.upper().replace("_", "").replace(".", "") == "SLUS21348" and all(v["present"] for v in volumes)
    return {"path": iso_path, "serial": serial, "size": os.path.getsize(iso_path),
            "volumes": volumes, "files": files, "bytes": total, "yakuza": ok,
            "main_bin": "MAIN.BIN" in root}


def cmd_probe(a):
    p = probe(a.iso)
    print(f"{p['path']}")
    print(f"  serial   {p['serial'] or '?'}   {'Yakuza (USA) — ok' if p['yakuza'] else 'NOT recognised as Yakuza (USA)'}")
    print(f"  size     {p['size']:,} bytes")
    for v in p["volumes"]:
        if v["present"]:
            print(f"  {v['tag']:7s} {v['files']:6d} files  {v['bytes']:13,} bytes")
        else:
            print(f"  {v['tag']:7s} MISSING")
    print(f"  total    {p['files']} files, {p['bytes']:,} bytes")


def cmd_list(a):
    iso, vols = load_volumes(a.iso, a.volume)
    n = tot = 0
    for cvmname, base, entries, info in vols:
        print(f"# {cvmname}: {info['builder']} scrambled={info['scrambled']} size={info['size']}")
        for e in ogredir.files(entries):
            if a.filter and a.filter.lower() not in e.path.lower():
                continue
            print(f"{e.size:10d}  {e.lba:8d}  {cvmname}:{e.path}")
            n += 1
            tot += e.size
    print(f"# {n} files, {tot} bytes", file=sys.stderr)


def cmd_extract(a):
    iso, vols = load_volumes(a.iso)
    os.makedirs(a.out, exist_ok=True)
    n = skipped = 0
    total = sum(1 for _c, _b, entries, _i in vols for _e in ogredir.files(entries))
    t0 = time.time()
    with open(os.path.join(a.out, MANIFEST), "w", newline="") as mf:
        w = csv.writer(mf)
        w.writerow(MANIFEST_FIELDS)
        for cvmname, base, entries, info in vols:
            tag = cvmname.split(".")[0]
            for e in ogredir.files(entries):
                if a.filter and a.filter.lower() not in e.path.lower():
                    continue
                dst = os.path.join(a.out, e.path.lstrip("/"))
                off = base + e.cvm_offset
                w.writerow((tag, e.path, e.lba, e.size, off))
                if a.skip_existing and os.path.exists(dst) and os.path.getsize(dst) == e.size:
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as fh:
                    fh.write(iso.mm[off:off + e.size])
                n += 1
                if n % 1000 == 0:
                    print(f"[{n + skipped}/{total}] {e.path}", flush=True)
    dt = time.time() - t0
    print(f"extracted {n} files to {a.out}" + (f" ({skipped} already present)" if skipped else "")
          + f" in {dt:.0f} s; manifest at {os.path.join(a.out, MANIFEST)}")


def cmd_verify_dump(a):
    path = os.path.join(a.dump, MANIFEST)
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found — extract with this version of the kit first")
    iso = IsoImage(a.iso) if a.iso else None
    rows = list(csv.DictReader(open(path, newline="")))
    missing = wrong = bad = 0
    for i, r in enumerate(rows):
        dst = os.path.join(a.dump, r["path"].lstrip("/"))
        size = int(r["size"])
        if not os.path.exists(dst):
            missing += 1
            if missing <= 20:
                print(f"! missing  {r['path']}")
            continue
        if os.path.getsize(dst) != size:
            wrong += 1
            if wrong <= 20:
                print(f"! size     {r['path']}: {os.path.getsize(dst)} != {size}")
            continue
        if iso is not None and size:
            off = int(r["iso_offset"])
            head = bytes(iso.mm[off:off + min(size, 4096)])
            with open(dst, "rb") as fh:
                if fh.read(len(head)) != head:
                    bad += 1
                    if bad <= 20:
                        print(f"! content  {r['path']}")
        if i % 2000 == 0 and i:
            print(f"[{i}/{len(rows)}]", flush=True)
    problems = missing + wrong + bad
    print(f"{len(rows)} manifest rows: {missing} missing, {wrong} wrong size, {bad} content mismatches"
          + ("" if iso is not None else " (content not compared: pass --iso)"))
    print("ok" if not problems else f"{problems} problems")
    if problems:
        sys.exit(1)


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


# ---------------------------------------------------------------------------
# convert — the whole browse pipeline, one command
# ---------------------------------------------------------------------------
def _have(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def _dap_available():
    try:
        import dap_decode  # noqa: F401
        return True
    except ImportError:
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.exists(os.path.join(os.path.dirname(here), "Xenosaga3PythonExtractor", "dap_decode.py"))


def cmd_convert(a):
    kinds = [k.strip() for k in a.kinds.split(",") if k.strip()]
    unknown = [k for k in kinds if k not in CONVERT_KINDS]
    if unknown:
        raise SystemExit(f"unknown kind(s) {unknown}; choose from {', '.join(CONVERT_KINDS)}")
    if not os.path.isdir(a.dump):
        raise SystemExit(f"{a.dump} is not a directory — run extract first")
    import browse
    import media
    tex_dir = os.path.join(a.out, "textures")
    common = (["--filter", a.filter] if a.filter else []) + (["--jobs", str(a.jobs)] if a.jobs else [])
    results = []
    t_all = time.time()
    for kind in kinds:
        print(f"\n=== {kind} ===", flush=True)
        t0 = time.time()
        status = "ok"
        try:
            if kind == "textures":
                browse.main(["textures", a.dump, tex_dir, *common])
            elif kind == "models":
                browse.main(["models", a.dump, os.path.join(a.out, "models"), *common])
            elif kind == "previews":
                if not (_have("numpy") and _have("PIL")):
                    print("skipped: previews need numpy + Pillow (pip install numpy pillow)")
                    status = "skipped"
                elif not os.path.isdir(tex_dir):
                    print(f"skipped: {tex_dir} missing — convert textures first")
                    status = "skipped"
                else:
                    import render
                    render.main([a.dump, tex_dir, os.path.join(a.out, "previews"), *common,
                                 *(["--size", str(a.size)] if a.size else [])])
            elif kind in ("audio", "movies"):
                if not media.find_ffmpeg():
                    print("skipped: ffmpeg not found (install it, or drop it in tools/ next to the app)")
                    status = "skipped"
                else:
                    media.main([kind, a.dump, os.path.join(a.out, kind), *common])
            elif kind == "sfx":
                if not _dap_available():
                    print("skipped: dap_decode.py not found next to cli.py")
                    status = "skipped"
                else:
                    media.main(["sfx", a.dump, os.path.join(a.out, "sfx"), *common])
        except SystemExit as exc:
            status = f"failed ({exc})" if exc.code not in (0, None) else "ok"
        except Exception as exc:  # keep going with the next kind
            status = f"failed ({type(exc).__name__}: {exc})"
            print(f"! {kind}: {status}", flush=True)
        results.append((kind, status, time.time() - t0))
    print(f"\n=== summary ({time.time() - t_all:.0f} s) ===")
    for kind, status, dt in results:
        print(f"  {kind:9s} {status:30s} {dt:7.0f} s   {os.path.join(a.out, kind)}")
    if any(s.startswith("failed") for _k, s, _t in results):
        sys.exit(1)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"Yakuza (PS2) Python Extractor {__version__}")
    sp = p.add_subparsers(dest="cmd", required=True)
    q = sp.add_parser("probe"); q.add_argument("iso"); q.set_defaults(fn=cmd_probe)
    q = sp.add_parser("list"); q.add_argument("iso")
    q.add_argument("--volume", help="only this volume (MEDIA, MEDIA2, MEDIA3, MEDIA4)")
    q.add_argument("--filter", help="only paths containing this substring"); q.set_defaults(fn=cmd_list)
    q = sp.add_parser("extract"); q.add_argument("iso"); q.add_argument("--out", required=True)
    q.add_argument("--filter", help="only paths containing this substring")
    q.add_argument("--skip-existing", action="store_true", help="leave files that already have the right size")
    q.set_defaults(fn=cmd_extract)
    q = sp.add_parser("verify-dump"); q.add_argument("dump")
    q.add_argument("--iso", help="also compare the first 4 KB of every file with the disc")
    q.set_defaults(fn=cmd_verify_dump)
    q = sp.add_parser("census"); q.add_argument("iso"); q.set_defaults(fn=cmd_census)
    q = sp.add_parser("convert"); q.add_argument("--dump", required=True); q.add_argument("--out", required=True)
    q.add_argument("--kinds", default=",".join(CONVERT_KINDS), help="comma list of " + ", ".join(CONVERT_KINDS))
    q.add_argument("--filter"); q.add_argument("--jobs", type=int); q.add_argument("--size", type=int)
    q.set_defaults(fn=cmd_convert)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
