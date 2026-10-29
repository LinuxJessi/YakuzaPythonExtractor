"""media.py — audio and video conversion via ffmpeg.

  python3 media.py audio  DUMP_DIR OUT_DIR [--filter S] [--jobs N]   # MEDIA2/SOUND *.AHX (voice) + MEDIA2/BGM *.ADX (music) → WAV
  python3 media.py movies DUMP_DIR OUT_DIR [--filter S] [--jobs N]   # MEDIA2/MOVIE *.SFD → H.264/AAC MP4
  python3 media.py sfx    DUMP_DIR OUT_DIR [--filter S] [--jobs N]   # ps2_DTPK sound-effect banks → per-cue WAV

``--filter`` keeps only in-game paths containing the substring (KIRYU_17, BGM, MOVIE/OP …).

Yakuza's streams are stock CRI formats (ADX, AHX, Sofdec), all of which
ffmpeg decodes natively. The only wrinkle: ~80% of the voice clips are AHX
"type 0x11" (byte 4), which ffmpeg's demuxer rejects although the MPEG-2
Layer II payload is the same as type 0x10 — we relabel the byte on the fly.
All voice is 48 kHz mono (the ADX header's rate field is right).
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import shutil
import subprocess
import sys


_FFMPEG_WIN = (r"%ProgramFiles%\ffmpeg\bin\ffmpeg.exe", r"%LocalAppData%\Microsoft\WinGet\Links\ffmpeg.exe",
               r"C:\ffmpeg\bin\ffmpeg.exe", r"%ChocolateyInstall%\bin\ffmpeg.exe")
_FFMPEG_POSIX = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg", "/opt/local/bin/ffmpeg")


def find_ffmpeg():
    """The ffmpeg to use: bundled tools/ (frozen builds), then PATH, then the usual installs."""
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        for tools in (os.path.join(exe_dir, "tools"), os.path.join(exe_dir, "_internal", "tools"),
                      os.path.join(os.path.dirname(exe_dir), "Frameworks", "tools")):
            cand = os.path.join(tools, name)
            if os.path.isfile(cand):
                return cand
    hit = shutil.which("ffmpeg")
    if hit:
        return hit
    for cand in (_FFMPEG_WIN if os.name == "nt" else _FFMPEG_POSIX):
        cand = os.path.expandvars(cand)
        if os.path.isfile(cand):
            return cand
    return None


def _ffmpeg():
    p = find_ffmpeg()
    if not p:
        sys.exit("ffmpeg not found (install it, put it on PATH, or drop it in tools/ next to the app)")
    return p


def _audio_job(args):
    src, dst = args
    if os.path.exists(dst) and os.path.getsize(dst) > 44:
        return src, True, ""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    head = open(src, "rb").read(8)
    if head[:2] == b"\x80\x00" and head[4] == 0x11:
        # AHX "type 0x11": identical MPEG-2 Layer II payload to type 0x10, which
        # ffmpeg's ADX demuxer accepts — so relabel the type byte and feed via stdin.
        data = bytearray(open(src, "rb").read())
        data[4] = 0x10
        r = subprocess.run([_ffmpeg(), "-v", "error", "-y", "-f", "adx", "-i", "-", dst],
                           input=bytes(data), capture_output=True)
        return src, r.returncode == 0, r.stderr.decode(errors="replace").strip()[:200]
    r = subprocess.run([_ffmpeg(), "-v", "error", "-y", "-i", src, dst], capture_output=True, text=True)
    return src, r.returncode == 0, r.stderr.strip()[:200]


def _movie_job(args):
    src, dst = args
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    r = subprocess.run([_ffmpeg(), "-v", "error", "-y", "-i", src, "-c:v", "libx264", "-crf", "18",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", dst],
                       capture_output=True, text=True)
    return src, r.returncode == 0, r.stderr.strip()[:200]


def _collect(dump, out, exts, newext, flt=None):
    jobs = []
    for dp, _dn, fn in os.walk(dump):
        for name in fn:
            if os.path.splitext(name)[1].lower() in exts:
                src = os.path.join(dp, name)
                rel = src[len(dump):].strip("/")
                if flt and flt.lower() not in rel.lower():
                    continue
                jobs.append((src, os.path.join(out, os.path.splitext(rel)[0] + newext)))
    return jobs


def _run(jobs, fn, nproc, label):
    ok = 0
    if not jobs:
        print(f"{label}: nothing to convert")
        return
    with multiprocessing.Pool(nproc) as pool:
        for i, (src, good, err) in enumerate(pool.imap_unordered(fn, jobs, chunksize=4)):
            ok += good
            if not good:
                print(f"!{src}: {err}", flush=True)
            if i % 500 == 0:
                print(f"[{i}/{len(jobs)}] {label}", flush=True)
    print(f"{label}: {ok}/{len(jobs)} converted")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for c in ("audio", "movies", "sfx"):
        q = sp.add_parser(c); q.add_argument("dump"); q.add_argument("out")
        q.add_argument("--filter", help="only in-game paths containing this substring")
        q.add_argument("--jobs", type=int, default=max(2, os.cpu_count() // 2))
    a = ap.parse_args(argv)
    if a.cmd == "sfx":
        cmd_sfx(a.dump, a.out, a.jobs, a.filter)
        return
    _ffmpeg()
    if a.cmd == "audio":
        _run(_collect(a.dump, a.out, {".ahx", ".adx"}, ".wav", a.filter), _audio_job, a.jobs, "audio")
    else:
        _run(_collect(a.dump, a.out, {".sfd"}, ".mp4", a.filter), _movie_job, min(a.jobs, 3), "movies")


# ---------------------------------------------------------------------------
# sfx — ps2_DTPK sound banks (MEDIA/SE, MEDIA/SESET and container leaves)
# ---------------------------------------------------------------------------
def _dap_module():
    """The DTPK decoder: vendored dap_decode.py (also importable frozen), else the Xenosaga III kit's copy."""
    try:
        import dap_decode
        return dap_decode
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "dap_decode.py"),
                 os.path.join(os.path.dirname(here), "Xenosaga3PythonExtractor", "dap_decode.py")):
        if os.path.exists(cand):
            import importlib.util
            spec = importlib.util.spec_from_file_location("dap_decode", cand)
            if "dap_decode" in sys.modules:
                return sys.modules["dap_decode"]
            mod = importlib.util.module_from_spec(spec)
            sys.modules["dap_decode"] = mod
            spec.loader.exec_module(mod)
            return mod
    sys.exit("dap_decode.py not found next to media.py")


def _sfx_job(args):
    src, rel, out = args
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import containers
    from pathlib import Path
    dap = _dap_module()
    blob = open(src, "rb").read()
    banks = [("", blob)] if blob[:8] == b"ps2_DTPK" else \
        [(p, l) for p, l in containers.leaves(blob) if l[:8] == b"ps2_DTPK"]
    n = 0
    for path, data in banks:
        stem = os.path.splitext(os.path.basename(rel))[0] + (path.strip("/").replace("/", "-") and "_" + path.strip("/").replace("/", "-") or "")
        try:
            # Yakuza banks start at the segment itself; the XS3 decoder expects a 0x40 preamble
            n += dap.decode_to_wavs(bytes(0x40) + data, Path(out) / os.path.dirname(rel).strip("/"), stem)
        except Exception as e:
            print(f"!{rel}{path}: {e}", flush=True)
    return src, True, str(n)


def cmd_sfx(dump, out, nproc, flt=None):
    jobs = []
    for dp, _dn, fn in os.walk(dump):
        for name in fn:
            if os.path.splitext(name)[1].lower() in (".ahx", ".adx", ".sfd"):
                continue
            src = os.path.join(dp, name)
            rel = src[len(dump):]
            if flt and flt.lower() not in rel.lower():
                continue
            jobs.append((src, rel, out))
    if not jobs:
        print("sfx: nothing to convert")
        return
    total = 0
    with multiprocessing.Pool(nproc) as pool:
        for i, (src, _ok, n) in enumerate(pool.imap_unordered(_sfx_job, jobs, chunksize=8)):
            total += int(n)
            if i % 2000 == 0:
                print(f"[{i}/{len(jobs)}] {total} cues", flush=True)
    print(f"sfx: {total} cues written")


if __name__ == "__main__":
    main()
