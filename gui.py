"""gui.py — local web GUI for the Yakuza (PS2) Python Extractor.

Run me directly:
    python3 gui.py            (or double-click launch.command / launch.bat / launch.sh)

Stdlib only. Starts an HTTP server on a free localhost port, opens your
browser, and shells out to the kit's command-line scripts for the real
work; their output streams back live via Server-Sent Events. Same shape as
the Xenosaga I/III extractors' GUI: one collapsible card per command, file
pickers, a "show command" dry run, an environment panel, a job list with a
stop button, and a live log.

Everything the GUI does is a command line you could type yourself, so the
two can never disagree; press "Show command" on any card to see it.
"""
from __future__ import annotations

import http.server
import json
import os
import queue
import shlex
import signal
import socket
import socketserver
import string
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
PY = sys.executable or "python3"
KIT_SCRIPTS = ("cli.py", "browse.py", "render.py", "media.py", "repack.py", "blender_open.py")
BROWSE_KINDS = ("textures", "models", "previews", "audio", "sfx", "movies")

# Frozen bundle (build.py / PyInstaller): the GUI exe sits next to yakuza-cli,
# the console dispatcher (tool.py) that runs every script by name.
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else ROOT
CLI_EXE = APP_DIR / ("yakuza-cli.exe" if os.name == "nt" else "yakuza-cli")

sys.path.insert(0, str(ROOT))
try:
    from cli import __version__ as VERSION
except Exception:  # pragma: no cover - the kit is incomplete; main() reports it
    VERSION = "?"


def _fatal(title: str, body: str) -> None:
    msg = f"{title}\n\n{body}"
    print(f"[gui] FATAL: {msg}", file=sys.stderr)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, body, f"Yakuza Extractor — {title}", 0x10)
        except Exception:
            pass
    elif sys.platform == "darwin":
        # A double-clicked .command has no console worth reading — show a dialog too.
        try:
            esc = body.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(["osascript", "-e",
                            f'display dialog "{esc}" with title "Yakuza Extractor — {title}" '
                            'buttons {"OK"} default button 1 with icon stop'],
                           timeout=120, check=False)
        except Exception:
            pass
    sys.exit(2)


# ---------------------------------------------------------------------------
# Jobs: one subprocess each, output fanned out to SSE subscribers
# ---------------------------------------------------------------------------
class Job:
    def __init__(self, name: str, cmd: list[str], reveal: str | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.name = name
        self.cmd = cmd
        self.reveal = reveal
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.exit_code: int | None = None
        self.stopped = False
        self.lines: list[str] = []
        self.subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        popen_kw = {}
        if os.name == "nt":
            # new group so Stop can taskkill the tree; no console window flashing
            # up for every job when the GUI itself is a windowed exe
            popen_kw["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                         | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        else:
            popen_kw["start_new_session"] = True   # so Stop can take the worker pool down too
        self._proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, bufsize=1, errors="replace",
                                      env={**os.environ, "PYTHONUNBUFFERED": "1"}, **popen_kw)
        self.lines.append("$ " + " ".join(shlex.quote(c) for c in cmd))
        threading.Thread(target=self._pump, daemon=True).start()

    # -- streaming -------------------------------------------------------
    def _broadcast(self, line):
        with self._lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(line)
            except queue.Full:
                pass

    def _pump(self):
        for line in self._proc.stdout:
            line = line.rstrip("\r\n")
            self.lines.append(line)
            self._broadcast(line)
        rc = self._proc.wait()
        self.exit_code = rc
        self.ended_at = time.time()
        tail = "[stopped]" if self.stopped else f"[exit {rc}]  ({self.elapsed():.0f} s)"
        self.lines.append(tail)
        self._broadcast(tail)
        self._broadcast(None)

    def subscribe(self):
        q = queue.Queue(maxsize=200000)
        with self._lock:
            self.subscribers.append(q)
            for line in self.lines:
                try:
                    q.put_nowait(line)
                except queue.Full:
                    break
            if self.ended_at is not None:
                q.put_nowait(None)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    # -- control ---------------------------------------------------------
    def stop(self):
        if self.exit_code is not None:
            return False
        self.stopped = True
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self._proc.pid)], capture_output=True)
            else:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                self._proc.terminate()
            except OSError:
                pass
        return True

    def elapsed(self):
        return (self.ended_at or time.time()) - self.started_at

    def status(self):
        if self.exit_code is None:
            return "running"
        if self.stopped:
            return "stopped"
        return "ok" if self.exit_code == 0 else "err"

    def to_summary(self):
        return {"id": self.id, "name": self.name, "status": self.status(), "exit_code": self.exit_code,
                "started": self.started_at, "elapsed": round(self.elapsed(), 1), "reveal": self.reveal,
                "cmd": " ".join(shlex.quote(c) for c in self.cmd[1:]), "lines": len(self.lines)}


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Command builders — form fields → argv (+ the folder to reveal afterwards)
# ---------------------------------------------------------------------------
def _s(form, key, default=""):
    v = form.get(key, [default])
    v = v[0] if isinstance(v, list) else v
    return (v or default).strip()


def _bool(form, key):
    return _s(form, key).lower() in ("1", "true", "yes", "on")


def _need(form, key, label):
    v = _s(form, key)
    if not v:
        raise ValueError(f"{label} is required")
    return v


def _file(form, key, label):
    v = Path(_need(form, key, label)).expanduser()
    if not v.is_file():
        raise ValueError(f"{label}: {v} is not a file")
    return str(v)


def _dir(form, key, label, must_exist=True):
    v = Path(_need(form, key, label)).expanduser()
    if must_exist and not v.is_dir():
        raise ValueError(f"{label}: {v} is not a folder")
    return str(v)


def _int(form, key, label, default=None):
    v = _s(form, key)
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        raise ValueError(f"{label} must be a whole number, not {v!r}")


def _script(name, *args):
    if FROZEN:
        return [str(CLI_EXE), name[:-3] if name.endswith(".py") else name, *args]
    return [PY, str(ROOT / name), *args]


def layout(work: str) -> dict[str, str]:
    """The fixed folder layout under one work folder (what the README documents)."""
    w = Path(work)
    d = {"work": str(w), "dump": str(w / "dump"), "browse": str(w / "browse")}
    for k in BROWSE_KINDS:
        d[k] = str(w / "browse" / k)
    return d


def _work(form, need_dump=True):
    work = _dir(form, "work", "work folder", must_exist=False)
    lay = layout(work)
    if need_dump and not Path(lay["dump"]).is_dir():
        raise ValueError(f"no dump folder at {lay['dump']} — run Extract first")
    return lay


def _common(form):
    args = []
    f = _s(form, "filter")
    if f:
        args += ["--filter", f]
    j = _int(form, "jobs", "parallel jobs")
    if j:
        args += ["--jobs", str(j)]
    return args


def b_probe(f):
    return _script("cli.py", "probe", _file(f, "iso", "ISO")), None


def b_list(f):
    cmd = _script("cli.py", "list", _file(f, "iso", "ISO"))
    vol = _s(f, "volume")
    if vol:
        cmd += ["--volume", vol]
    flt = _s(f, "filter")
    if flt:
        cmd += ["--filter", flt]
    return cmd, None


def b_census(f):
    return _script("cli.py", "census", _file(f, "iso", "ISO")), None


def b_extract(f):
    lay = _work(f, need_dump=False)
    cmd = _script("cli.py", "extract", _file(f, "iso", "ISO"), "--out", lay["dump"])
    flt = _s(f, "filter")
    if flt:
        cmd += ["--filter", flt]
    if _bool(f, "skip_existing"):
        cmd.append("--skip-existing")
    return cmd, lay["dump"]


def b_verify_dump(f):
    lay = _work(f)
    cmd = _script("cli.py", "verify-dump", lay["dump"])
    iso = _s(f, "iso")
    if iso:
        cmd += ["--iso", _file(f, "iso", "ISO")]
    return cmd, None


def b_convert(f):
    lay = _work(f)
    kinds = [k for k in BROWSE_KINDS if _bool(f, k)]
    if not kinds:
        raise ValueError("tick at least one kind to convert")
    cmd = _script("cli.py", "convert", "--dump", lay["dump"], "--out", lay["browse"], "--kinds", ",".join(kinds),
                  *_common(f))
    size = _int(f, "size", "preview size")
    if size:
        cmd += ["--size", str(size)]
    return cmd, lay["browse"]


def b_textures(f):
    lay = _work(f)
    return _script("browse.py", "textures", lay["dump"], lay["textures"], *_common(f)), lay["textures"]


def b_models(f):
    lay = _work(f)
    return _script("browse.py", "models", lay["dump"], lay["models"], *_common(f)), lay["models"]


def b_previews(f):
    lay = _work(f)
    if not Path(lay["textures"]).is_dir():
        raise ValueError(f"no textures at {lay['textures']} — convert textures first")
    cmd = _script("render.py", lay["dump"], lay["textures"], lay["previews"], *_common(f))
    size = _int(f, "size", "preview size")
    if size:
        cmd += ["--size", str(size)]
    return cmd, lay["previews"]


def b_blender(f):
    lay = _work(f, need_dump=False)
    if not Path(lay["models"]).is_dir():
        raise ValueError(f"no models at {lay['models']} — convert models first")
    target = _s(f, "target") or _s(f, "preset")
    if not target:
        raise ValueError("pick a preset or type a character set / path")
    return _script("blender_open.py", lay["models"], target), None


def _media(kind):
    def build(f):
        lay = _work(f)
        return _script("media.py", kind, lay["dump"], lay[kind], *_common(f)), lay[kind]
    return build


def _out_iso(f, src):
    out = _s(f, "out")
    if not out:
        return []
    if os.path.abspath(out) == os.path.abspath(src):
        raise ValueError("output ISO is the same file as the source — leave it blank to patch in place")
    return ["--out", out]


def b_texture(f):
    iso = _file(f, "iso", "ISO")
    cmd = _script("repack.py", "texture", iso, "--file", _need(f, "file", "in-game path"),
                  "--leaf", _need(f, "leaf", "leaf path"), "--index", str(_int(f, "index", "texture index", 0)),
                  "--png", _file(f, "png", "PNG"))
    if _bool(f, "fit"):
        cmd.append("--fit")
    if _bool(f, "allow_grow"):
        cmd.append("--allow-grow")
    cmd += _out_iso(f, iso)
    return cmd, None


def b_patch(f):
    iso = _file(f, "iso", "ISO")
    cmd = _script("repack.py", "patch", iso, "--file", _need(f, "file", "in-game path"),
                  "--with", _file(f, "with", "replacement file"))
    cmd += _out_iso(f, iso)
    return cmd, None


def b_verify_iso(f):
    return _script("repack.py", "verify", _file(f, "iso", "ISO")), None


BUILDERS = {
    "probe": b_probe, "list": b_list, "census": b_census, "extract": b_extract, "verify-dump": b_verify_dump,
    "convert": b_convert, "textures": b_textures, "models": b_models, "previews": b_previews,
    "audio": _media("audio"), "sfx": _media("sfx"), "movies": _media("movies"), "blender": b_blender,
    "texture": b_texture, "patch": b_patch, "verify-iso": b_verify_iso,
}


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------
def _walk_for_isos(root: Path, max_depth: int, want: int, out: list[Path]) -> None:
    if len(out) >= want or max_depth < 0:
        return
    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_file(), p.name.lower()))
    except (OSError, PermissionError):
        return
    for child in entries:
        if len(out) >= want:
            return
        try:
            if child.is_file():
                if child.suffix.lower() == ".iso" and child.stat().st_size > 100 * 1024 * 1024:
                    out.append(child)
            elif child.is_dir() and not child.name.startswith("."):
                _walk_for_isos(child, max_depth - 1, want, out)
        except (OSError, PermissionError):
            continue


def detect_isos() -> list[str]:
    found: list[Path] = []
    for root, depth in ((APP_DIR.parent, 2), (APP_DIR, 1), (Path.home() / "Downloads", 2),
                        (Path.home() / "Desktop", 3), (Path.home() / "Documents", 3)):
        if root.is_dir():
            _walk_for_isos(root, depth, want=12, out=found)
    seen, out = set(), []
    for p in found:
        if str(p) not in seen:
            seen.add(str(p))
            out.append(str(p))
    pref = [p for p in out if "yakuza" in Path(p).name.lower() or "slus-21348" in p.lower()]
    return pref + [p for p in out if p not in pref]


def default_out_dir(iso: str) -> str:
    sibling = APP_DIR.parent / "Yakuza (USA)"
    if sibling.is_dir():
        return str(sibling)
    if iso:
        p = Path(iso)
        return str(p.parent / (p.stem if "yakuza" in p.stem.lower() else "yakuza-out"))
    # no ISO found: never default into the bundle itself
    return str((Path.home() if FROZEN else ROOT) / ("Yakuza (USA)" if FROZEN else "out"))


def _blender_path():
    try:
        import blender_open
        return blender_open.blender_exe() or ""
    except Exception:
        return ""


def _ffmpeg_path():
    try:
        import media
        return media.find_ffmpeg() or ""
    except Exception:
        import shutil
        return shutil.which("ffmpeg") or ""


def _dap_path():
    try:
        import dap_decode
        return getattr(dap_decode, "__file__", "") or "bundled"
    except ImportError:
        cand = ROOT.parent / "Xenosaga3PythonExtractor" / "dap_decode.py"
        return str(cand) if cand.exists() else ""


def kit_present() -> bool:
    return CLI_EXE.exists() if FROZEN else all((ROOT / s).exists() for s in KIT_SCRIPTS)


def detect_deps() -> dict:
    deps = {"python": sys.version.split()[0], "version": VERSION, "frozen": FROZEN,
            "cli": str(CLI_EXE if FROZEN else ROOT / "cli.py"),
            "ffmpeg": _ffmpeg_path(), "blender": _blender_path(), "dap_decode": _dap_path(),
            "scripts": kit_present()}
    for mod, key in (("numpy", "numpy"), ("PIL", "pillow")):
        try:
            m = __import__(mod)
            deps[key] = getattr(m, "__version__", "yes")
        except ImportError:
            deps[key] = ""
    return deps


_PROBE_CACHE: dict[tuple, dict] = {}


def probe_iso(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.is_file():
        return {"error": f"{p} is not a file"}
    st = p.stat()
    key = (str(p), st.st_size, st.st_mtime)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    try:
        sys.path.insert(0, str(ROOT))
        import cli
        res = cli.probe(str(p))
    except Exception as exc:
        res = {"error": f"{type(exc).__name__}: {exc}", "path": str(p)}
    _PROBE_CACHE[key] = res
    return res


def reveal(path: str) -> str | None:
    p = Path(path).expanduser()
    if not p.exists():
        return f"{p} does not exist (yet)"
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        elif os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except OSError as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------------------
# Filesystem browser
# ---------------------------------------------------------------------------
def list_roots() -> list[str]:
    roots: list[str] = []
    if os.name == "nt":
        for d in string.ascii_uppercase:
            if Path(f"{d}:\\").exists():
                roots.append(f"{d}:\\")
    else:
        roots += ["/", str(Path.home())]
        for parent in ("/Volumes", "/mnt", "/media"):
            try:
                pp = Path(parent)
                if pp.is_dir():
                    roots += [str(c) for c in sorted(pp.iterdir()) if c.is_dir()]
            except (OSError, PermissionError):
                continue
    return list(dict.fromkeys(roots))


def list_dir(path_str: str, ext_filter: str | None = None) -> dict:
    p = Path(path_str).expanduser() if path_str else Path.home()
    if not p.exists():
        p = Path.home()
    if not p.is_dir():
        p = p.parent
    p = p.resolve()
    exts = {e.strip().lower().lstrip(".") for e in (ext_filter or "").split(",") if e.strip()}
    entries, err = [], None
    try:
        children = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except (OSError, PermissionError) as exc:
        children, err = [], str(exc)
    for c in children:
        try:
            if c.name.startswith(".") or (c.is_symlink() and not c.exists()):
                continue
            if c.is_dir():
                entries.append({"name": c.name, "type": "dir", "path": str(c)})
            else:
                if exts and c.suffix.lower().lstrip(".") not in exts:
                    continue
                entries.append({"name": c.name, "type": "file", "path": str(c), "size": c.stat().st_size})
        except (OSError, PermissionError):
            continue
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None,
            "entries": entries[:2000], "roots": list_roots(), "error": err}


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>YAKUZA · Extractor</title>
<style>
:root {
  --bg-deep:   #070608;
  --bg-mid:    #110c0f;
  --bg-card:   #19121a;
  --bg-input:  #0b080a;
  --border:        rgba(232, 52, 58, 0.18);
  --border-strong: rgba(232, 52, 58, 0.45);
  --fg:     #efe6e3;
  --muted:  #9a8a8c;
  --accent: #e8343a;
  --accent-warm: #e9c46a;
  --accent-glow: rgba(232, 52, 58, 0.35);
  --ok:   #62e39a;
  --warn: #ffc15d;
  --err:  #ff6e7f;
  --serif: "Hiragino Mincho ProN", "Yu Mincho", "Noto Serif JP", "Cormorant Garamond", Georgia, serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0; color: var(--fg);
  background:
    radial-gradient(ellipse 60% 45% at 85% 0%, rgba(232, 52, 58, 0.10) 0%, transparent 70%),
    radial-gradient(ellipse 40% 30% at 10% 100%, rgba(233, 196, 106, 0.05) 0%, transparent 70%),
    linear-gradient(180deg, var(--bg-deep) 0%, #030204 100%);
  font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 14px; line-height: 1.5; overflow: hidden;
}
body::before {
  content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 1000;
  background: repeating-linear-gradient(180deg, transparent 0, transparent 2px,
    rgba(255, 255, 255, 0.012) 2px, rgba(255, 255, 255, 0.012) 3px);
  mix-blend-mode: lighten;
}
header {
  padding: 16px 28px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 22px; position: relative;
  background: linear-gradient(180deg, rgba(17, 12, 15, 0.95) 0%, rgba(17, 12, 15, 0.6) 100%);
}
header::after {
  content: ''; position: absolute; left: 0; right: 0; bottom: -1px; height: 1px;
  background: linear-gradient(90deg, var(--accent) 0%, transparent 60%); opacity: 0.7;
}
header .kanji {
  font-family: var(--serif); font-size: 44px; line-height: 1; color: var(--accent);
  text-shadow: 0 0 18px var(--accent-glow), 0 0 2px var(--accent); font-weight: 600;
}
header .title { display: flex; flex-direction: column; gap: 3px; line-height: 1; }
header h1 {
  margin: 0; font-family: var(--serif); font-size: 26px; font-weight: 500;
  letter-spacing: 0.16em; text-transform: uppercase;
}
header h1 .sep { color: var(--accent); margin: 0 0.45em; font-weight: 300; }
header h1 .extractor { font-family: inherit; font-size: 12px; letter-spacing: 0.4em; color: var(--muted); }
header .tagline { color: var(--muted); font-family: var(--serif); font-size: 13.5px; letter-spacing: 0.06em; }
header .tagline b { color: var(--accent-warm); font-weight: 500; }
header .actions { margin-left: auto; display: flex; gap: 8px; }

main { display: grid; grid-template-columns: 1fr 500px; height: calc(100vh - 78px); }
#cards { padding: 18px 28px 80px; overflow-y: auto; }
#cards::-webkit-scrollbar { width: 10px; }
#cards::-webkit-scrollbar-thumb { background: rgba(232, 52, 58, 0.22); border-radius: 5px; }
.group {
  font-family: var(--serif); color: var(--accent-warm); font-size: 12px; letter-spacing: 0.3em;
  text-transform: uppercase; margin: 22px 0 10px; display: flex; align-items: center; gap: 12px;
}
.group:first-child { margin-top: 4px; }
.group::after { content: ''; flex: 1; height: 1px; background: linear-gradient(90deg, var(--border-strong), transparent); }

#right {
  border-left: 1px solid var(--border); display: flex; flex-direction: column; min-height: 0;
  background: linear-gradient(180deg, rgba(25, 18, 26, 0.85) 0%, rgba(11, 8, 10, 0.95) 100%);
}
#right h2 {
  font-family: var(--serif); font-size: 13px; font-weight: 500; margin: 12px 16px 6px;
  color: var(--accent); text-transform: uppercase; letter-spacing: 0.24em; position: relative; padding-left: 14px;
}
#right h2::before {
  content: ''; position: absolute; left: 0; top: 50%; width: 7px; height: 1px;
  background: var(--accent); transform: translateY(-50%); box-shadow: 0 0 6px var(--accent-glow);
}
#status-box { padding: 0 16px 10px; }
#status-box .row {
  display: flex; justify-content: space-between; gap: 10px; padding: 4px 0; font-size: 13px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}
#status-box .row:last-child { border-bottom: 0; }
#status-box .row span:first-child { color: var(--muted); font-size: 11.5px; letter-spacing: 0.05em; text-transform: uppercase; white-space: nowrap; }
#status-box .row .v {
  font-family: var(--mono); font-size: 12px; color: var(--muted); max-width: 66%;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
#status-box .row .v.ok { color: var(--ok); }
#status-box .row .v.bad { color: var(--err); }
#status-box .row .v.warn { color: var(--warn); }

#jobs { padding: 0 10px 8px; max-height: 190px; overflow-y: auto; }
#jobs .empty { color: var(--muted); font-size: 12px; padding: 2px 6px 6px; font-style: italic; }
.job {
  display: grid; grid-template-columns: 1fr auto auto auto; gap: 8px; align-items: center;
  padding: 5px 6px; border-radius: 3px; cursor: pointer; font-size: 12.5px;
}
.job:hover { background: rgba(232, 52, 58, 0.07); }
.job.current { background: rgba(232, 52, 58, 0.12); outline: 1px solid var(--border-strong); }
.job .name { font-family: var(--serif); font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.job .t { color: var(--muted); font-family: var(--mono); font-size: 11px; }
.job button { padding: 3px 8px; font-size: 10px; }

#log {
  flex: 1; margin: 0; padding: 12px 16px; background: #050305; color: #d9c9c6;
  font-family: var(--mono); font-size: 12px; line-height: 1.5; overflow-y: auto; white-space: pre-wrap;
  border-top: 1px solid var(--border); min-height: 0;
}
#log::-webkit-scrollbar { width: 8px; }
#log::-webkit-scrollbar-thumb { background: rgba(232, 52, 58, 0.25); border-radius: 4px; }
#log .err { color: var(--err); }

.card {
  background: linear-gradient(180deg, rgba(25, 18, 26, 0.75) 0%, rgba(17, 12, 15, 0.75) 100%);
  border: 1px solid var(--border); border-radius: 5px; padding: 14px 18px; margin-bottom: 10px;
  transition: border-color 0.18s, box-shadow 0.18s; position: relative;
}
.card::before {
  content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 2px; border-radius: 5px 0 0 5px;
  background: transparent; transition: background 0.18s, box-shadow 0.18s;
}
.card.run { border-color: var(--accent); box-shadow: 0 0 22px rgba(232, 52, 58, 0.14); }
.card.run::before { background: var(--accent); box-shadow: 0 0 8px var(--accent-glow); }
.card.ok  { border-color: rgba(98, 227, 154, 0.4); }
.card.ok::before { background: var(--ok); }
.card.err, .card.stopped { border-color: rgba(255, 110, 127, 0.5); }
.card.err::before { background: var(--err); }
.card.stopped::before { background: var(--warn); }
.card-head { display: flex; align-items: baseline; gap: 12px; cursor: pointer; user-select: none; }
.card-head h3 { margin: 0; font-family: var(--serif); font-size: 18px; font-weight: 500; letter-spacing: 0.03em; }
.card-head .step { font-family: var(--serif); color: var(--accent-warm); font-size: 15px; min-width: 28px; }
.card-head .badge {
  font-size: 10px; padding: 3px 9px; border-radius: 2px; margin-left: auto;
  background: rgba(154, 138, 140, 0.12); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.14em; font-weight: 600;
}
.badge.run { background: rgba(232, 52, 58, 0.2); color: var(--accent); }
.badge.ok  { background: rgba(98, 227, 154, 0.18); color: var(--ok); }
.badge.err { background: rgba(255, 110, 127, 0.18); color: var(--err); }
.badge.stopped { background: rgba(255, 193, 93, 0.18); color: var(--warn); }
.card-head .chev { color: var(--muted); transition: transform 0.2s; margin-left: 8px; font-size: 12px; }
.card.open .chev { transform: rotate(180deg); }
.card-body { display: none; margin-top: 12px; }
.card.open .card-body { display: block; }
.card-body p.help { margin: 0 0 12px; color: var(--muted); font-size: 13px; line-height: 1.55; }
.card-body p.help code { color: var(--accent-warm); font-size: 12px; }
.card-body p.help em { color: var(--fg); font-style: normal; }
.card .row { display: grid; grid-template-columns: 140px 1fr; gap: 12px; margin-bottom: 8px; align-items: center; }
.card .row label { color: var(--muted); font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.07em; }
.card .row .with-pick { display: grid; grid-template-columns: 1fr auto; gap: 6px; }
.card .row .check-wrap { display: flex; align-items: center; gap: 10px; }
.card .row .check-wrap span { color: var(--muted); font-size: 12px; }
.card .checks { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px 14px; margin-bottom: 8px; }
.card .checks label { display: flex; gap: 8px; align-items: center; font-size: 13px; color: var(--fg); cursor: pointer; }
.card .checks label small { color: var(--muted); }
.card input[type=text], .card select {
  width: 100%; background: var(--bg-input); color: var(--fg); border: 1px solid var(--border);
  border-radius: 3px; padding: 7px 10px; font: inherit; font-family: var(--mono); font-size: 12.5px;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.card input[type=text]:focus, .card select:focus {
  outline: none; border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent-glow);
}
.card select option { background: var(--bg-mid); }
.card input[type=checkbox] { accent-color: var(--accent); width: 15px; height: 15px; }
.card .actions { margin-top: 12px; display: flex; gap: 8px; align-items: center; }
.card .actions .note { color: var(--muted); font-size: 12px; margin-left: 6px; }

button {
  background: linear-gradient(180deg, #ef4a4f 0%, #b8232a 100%); color: #fff; border: 0;
  padding: 8px 16px; border-radius: 3px; font: inherit; font-weight: 600; font-size: 12px;
  letter-spacing: 0.1em; text-transform: uppercase; cursor: pointer;
  transition: filter 0.15s, transform 0.05s; box-shadow: 0 0 12px rgba(232, 52, 58, 0.25);
}
button:hover { filter: brightness(1.12); }
button:active { transform: translateY(1px); }
button.ghost { background: transparent; color: var(--fg); border: 1px solid var(--border); font-weight: 500; box-shadow: none; }
button.ghost:hover { background: rgba(232, 52, 58, 0.08); border-color: var(--accent); }
button.gold { background: linear-gradient(180deg, #f1d58a 0%, #c9a24a 100%); color: #2b1d05; box-shadow: 0 0 12px rgba(233, 196, 106, 0.25); }
button.pick {
  padding: 6px 12px; font-size: 11px; background: rgba(232, 52, 58, 0.12); color: #f3a0a3;
  border: 1px solid var(--border); box-shadow: none; font-weight: 500;
}
button.pick:hover { background: rgba(232, 52, 58, 0.22); border-color: var(--accent); }
button:disabled { opacity: 0.4; cursor: not-allowed; }
code, kbd { background: rgba(233, 196, 106, 0.08); color: var(--accent-warm); border-radius: 2px; padding: 1px 5px; font-family: var(--mono); font-size: 12px; }

/* ---------- file picker modal ---------- */
.modal[hidden] { display: none; }
.modal { position: fixed; inset: 0; z-index: 100; display: flex; align-items: center; justify-content: center; }
.modal-backdrop { position: absolute; inset: 0; background: rgba(3, 2, 4, 0.82); backdrop-filter: blur(4px); animation: fadeIn 0.15s ease-out; }
.modal-window {
  position: relative; z-index: 1; background: linear-gradient(180deg, var(--bg-card) 0%, var(--bg-mid) 100%);
  border: 1px solid var(--border-strong);
  box-shadow: 0 0 0 1px var(--border), 0 0 80px rgba(232, 52, 58, 0.18), 0 20px 60px rgba(0,0,0,0.5);
  border-radius: 8px; width: 760px; max-width: 92vw; height: 580px; max-height: 88vh;
  display: flex; flex-direction: column; animation: slideIn 0.18s ease-out; overflow: hidden;
}
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes slideIn { from { opacity: 0; transform: translateY(-8px) scale(0.985); } to { opacity: 1; transform: none; } }
.modal-header { padding: 14px 22px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px; }
.modal-header h2 { margin: 0; font-family: var(--serif); font-size: 18px; font-weight: 500; letter-spacing: 0.05em; }
.modal-header .x { margin-left: auto; background: transparent; border: 0; color: var(--muted); font-size: 22px; cursor: pointer; padding: 0 6px; box-shadow: none; text-transform: none; }
.modal-header .x:hover { color: var(--err); background: transparent; }
.fs-toolbar { padding: 10px 18px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 8px; background: rgba(3, 2, 4, 0.3); }
.fs-crumb { flex: 1; overflow-x: auto; white-space: nowrap; font-family: var(--mono); font-size: 12px; }
.fs-crumb a { color: var(--accent-warm); cursor: pointer; padding: 2px 4px; border-radius: 2px; }
.fs-crumb a:hover { background: rgba(232, 52, 58, 0.12); }
.fs-crumb .sep { color: var(--muted); margin: 0 2px; opacity: 0.6; }
.fs-list { flex: 1; overflow-y: auto; padding: 4px 6px; }
.fs-entry { display: flex; align-items: center; gap: 12px; padding: 7px 12px; cursor: pointer; border-radius: 3px; font-size: 13px; }
.fs-entry:hover { background: rgba(232, 52, 58, 0.08); }
.fs-entry.selected { background: rgba(232, 52, 58, 0.18); outline: 1px solid var(--accent); }
.fs-entry .icon { width: 18px; text-align: center; opacity: 0.85; }
.fs-entry .name { flex: 1; }
.fs-entry .size { margin-left: auto; color: var(--muted); font-size: 11px; font-family: var(--mono); }
.fs-empty { padding: 30px; text-align: center; color: var(--muted); font-size: 13px; }
.fs-roots, .fs-quick { padding: 8px 18px; display: flex; flex-wrap: wrap; gap: 6px; font-size: 11px; align-items: center; }
.fs-roots { border-top: 1px solid var(--border); }
.fs-quick { border-bottom: 1px solid var(--border); background: rgba(233, 196, 106, 0.03); }
.fs-roots span, .fs-quick span { color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; margin-right: 4px; }
.fs-roots a, .fs-quick a {
  color: var(--fg); padding: 3px 9px; border-radius: 2px; border: 1px solid var(--border); cursor: pointer;
  font-family: var(--mono); font-size: 11px; white-space: nowrap; max-width: 320px; overflow: hidden; text-overflow: ellipsis;
}
.fs-roots a:hover, .fs-quick a:hover, .fs-quick a.selected { background: rgba(232, 52, 58, 0.14); border-color: var(--accent); color: var(--accent-warm); }
.modal-footer { padding: 12px 18px; border-top: 1px solid var(--border); display: flex; align-items: center; gap: 10px; background: rgba(3, 2, 4, 0.3); }
.modal-footer .sel-path { flex: 1; min-width: 0; color: var(--muted); font-size: 12px; font-family: var(--mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.modal-footer .sel-path.has { color: var(--fg); }
.modal-error { padding: 8px 18px; color: var(--err); font-size: 12px; background: rgba(255, 110, 127, 0.06); border-bottom: 1px solid rgba(255, 110, 127, 0.2); }

@media (max-width: 1100px) {
  main { grid-template-columns: 1fr; grid-template-rows: 1fr 40vh; }
  #right { border-left: 0; border-top: 1px solid var(--border); }
}
</style>
</head>
<body>
<header>
  <div class="kanji">龍</div>
  <div class="title">
    <h1><span>Yakuza</span><span class="sep">·</span><span class="extractor">EXTRACTOR</span></h1>
    <div class="tagline">龍が如く · Kamurocho disc unpacker · <b>SLUS-21348</b> · <span id="ver"></span></div>
  </div>
  <div class="actions">
    <button class="ghost" id="btn-open-all">Expand all</button>
    <button class="ghost" id="btn-refresh">Refresh status</button>
  </div>
</header>
<main>
  <section id="cards"></section>
  <aside id="right">
    <h2>Environment</h2>
    <div id="status-box"></div>
    <h2>Jobs</h2>
    <div id="jobs"><div class="empty">nothing run yet</div></div>
    <h2>Output</h2>
    <pre id="log">Ready. Pick a card on the left; every card shows the exact command it runs.</pre>
  </aside>
</main>

<div class="modal" id="fs-modal" hidden>
  <div class="modal-backdrop" data-close></div>
  <div class="modal-window">
    <div class="modal-header"><h2 id="fs-title">Select…</h2><button type="button" class="x" data-close>×</button></div>
    <div class="fs-toolbar"><button type="button" class="ghost" id="fs-up">↑ Up</button><div class="fs-crumb" id="fs-crumb"></div></div>
    <div class="modal-error" id="fs-error" hidden></div>
    <div class="fs-quick" id="fs-quick" hidden></div>
    <div class="fs-list" id="fs-list"></div>
    <div class="fs-roots" id="fs-roots"></div>
    <div class="modal-footer">
      <div class="sel-path" id="fs-sel">No selection</div>
      <button type="button" class="ghost" data-close>Cancel</button>
      <button type="button" id="fs-select" disabled>Select</button>
    </div>
  </div>
</div>

<script>
const log = document.getElementById('log');
const cardEls = {};
let currentES = null, currentJob = null, detected = {}, jobsTimer = null;

function esc(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]); }
function fmtSize(b) { if (b < 1024) return b + ' B'; const u = ['KB','MB','GB','TB']; let v = b / 1024, i = 0; while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; } return v.toFixed(v >= 10 ? 0 : 1) + ' ' + u[i]; }
function fmtDur(s) { s = Math.round(s); return s < 60 ? s + 's' : s < 3600 ? Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') : Math.floor(s / 3600) + 'h' + String(Math.floor(s % 3600 / 60)).padStart(2, '0'); }
function appendLog(line, cls) {
  if (cls) { const sp = document.createElement('span'); sp.className = cls; sp.textContent = line + '\n'; log.appendChild(sp); }
  else log.appendChild(document.createTextNode(line + '\n'));
  log.scrollTop = log.scrollHeight;
}
function setStatus(name, status) {
  const el = cardEls[name]; if (!el) return;
  el.classList.remove('run', 'ok', 'err', 'stopped'); if (status) el.classList.add(status);
  const b = el.querySelector('.badge'); b.classList.remove('run', 'ok', 'err', 'stopped');
  b.textContent = status || 'idle'; if (status) b.classList.add(status);
}

// ------------------------------------------------------------------ jobs
function viewJob(id) {
  if (currentES) currentES.close();
  currentJob = id; log.textContent = '';
  const es = new EventSource('/stream/' + id); currentES = es;
  es.onmessage = e => {
    if (e.data === '__eof__') { es.close(); refreshJobs(); return; }
    appendLog(e.data, /^(!|\[error\]|Traceback|\w*Error:)/.test(e.data) ? 'err' : '');
  };
  es.onerror = () => es.close();
  refreshJobs();
}
async function stopJob(id) { await fetch('/stop/' + id, {method: 'POST'}); refreshJobs(); }
async function refreshJobs() {
  const jobs = await (await fetch('/jobs')).json();
  const box = document.getElementById('jobs');
  if (!jobs.length) { box.innerHTML = '<div class="empty">nothing run yet</div>'; return; }
  box.innerHTML = jobs.map(j => `<div class="job${j.id === currentJob ? ' current' : ''}" data-id="${j.id}" title="${esc(j.cmd)}">
      <span class="name">${esc(j.title || j.name)}</span>
      <span class="badge ${j.status}">${j.status}</span>
      <span class="t">${fmtDur(j.elapsed)}</span>
      ${j.status === 'running' ? `<button class="ghost stop" data-id="${j.id}">Stop</button>` :
        (j.reveal ? `<button class="ghost reveal" data-path="${esc(j.reveal)}">Open</button>` : '<span></span>')}
    </div>`).join('');
  box.querySelectorAll('.job').forEach(el => el.addEventListener('click', () => viewJob(el.dataset.id)));
  box.querySelectorAll('.stop').forEach(b => b.addEventListener('click', e => { e.stopPropagation(); stopJob(b.dataset.id); }));
  box.querySelectorAll('.reveal').forEach(b => b.addEventListener('click', e => { e.stopPropagation(); openFolder(b.dataset.path); }));
  jobs.forEach(j => setStatus(j.name, j.status === 'running' ? 'run' : j.status));
  const anyRunning = jobs.some(j => j.status === 'running');
  clearTimeout(jobsTimer); if (anyRunning) jobsTimer = setTimeout(refreshJobs, 2000);
}
async function openFolder(path) {
  const r = await (await fetch('/open', {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: new URLSearchParams({path})})).json();
  if (r.error) appendLog('[open] ' + r.error, 'err');
}

// ------------------------------------------------------------------ runner
async function runCommand(name, body) {
  setStatus(name, 'run');
  const res = await fetch('/run/' + name, {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body});
  const j = await res.json();
  if (!res.ok || j.error) { setStatus(name, 'err'); log.textContent = ''; appendLog('[error] ' + (j.error || res.statusText), 'err'); return; }
  viewJob(j.job_id);
}
async function previewCommand(name, body) {
  const res = await fetch('/preview/' + name, {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body});
  const j = await res.json();
  if (currentES) currentES.close(); currentJob = null; log.textContent = '';
  if (j.error) { appendLog('[error] ' + j.error, 'err'); return; }
  appendLog('# Would run (from ' + detected.root + '):');
  appendLog(j.cmd.map(c => /\s/.test(c) ? '"' + c + '"' : c).join(' '));
  if (j.reveal) appendLog('# Output folder: ' + j.reveal);
  refreshJobs();
}

// ------------------------------------------------------------------ picker
const fsModal = document.getElementById('fs-modal'), fsTitle = document.getElementById('fs-title'),
      fsCrumb = document.getElementById('fs-crumb'), fsList = document.getElementById('fs-list'),
      fsRoots = document.getElementById('fs-roots'), fsError = document.getElementById('fs-error'),
      fsQuick = document.getElementById('fs-quick'), fsSel = document.getElementById('fs-sel'),
      fsSelectBtn = document.getElementById('fs-select'), fsUp = document.getElementById('fs-up');
const fsState = {mode: 'file', filter: null, current: null, parent: null, selected: null, onChoose: null};

function renderQuick() {
  const isos = fsState.filter === 'iso' ? (detected.isos || []) : [];
  if (!isos.length) { fsQuick.hidden = true; return; }
  fsQuick.hidden = false;
  fsQuick.innerHTML = '<span>Detected:</span>' + isos.map(p => `<a class="qp" data-path="${esc(p)}" title="${esc(p)}">${esc(p.split(/[\\/]/).pop())}</a>`).join('');
  fsQuick.querySelectorAll('.qp').forEach(a => {
    a.addEventListener('click', () => { fsQuick.querySelectorAll('.qp').forEach(x => x.classList.remove('selected')); a.classList.add('selected');
      fsState.selected = a.dataset.path; fsSel.classList.add('has'); fsSel.textContent = a.dataset.path; fsSelectBtn.disabled = false; });
    a.addEventListener('dblclick', () => { fsState.selected = a.dataset.path; chooseAndClose(); });
  });
}
function openPicker({mode, filter, startPath, title, onChoose}) {
  Object.assign(fsState, {mode, filter: filter || null, current: null, parent: null, selected: null, onChoose});
  fsTitle.textContent = title || (mode === 'dir' ? 'Select a folder' : 'Select a file');
  fsSel.classList.remove('has'); fsSel.textContent = 'No selection'; fsSelectBtn.disabled = mode === 'file';
  fsError.hidden = true; renderQuick(); fsModal.hidden = false; loadDir(startPath || '');
}
function closePicker() { fsModal.hidden = true; fsState.onChoose = null; }
async function loadDir(path) {
  fsError.hidden = true;
  try {
    const qs = new URLSearchParams(); if (path) qs.set('path', path); if (fsState.filter) qs.set('filter', fsState.filter);
    const data = await (await fetch('/browse-fs?' + qs)).json();
    if (data.error) { fsError.hidden = false; fsError.textContent = data.error; }
    fsState.current = data.path; fsState.parent = data.parent; fsState.selected = null;
    if (fsState.mode === 'dir') { fsState.selected = data.path; fsSel.classList.add('has'); fsSel.textContent = data.path; fsSelectBtn.disabled = false; }
    else { fsSel.classList.remove('has'); fsSel.textContent = 'No selection'; fsSelectBtn.disabled = true; }
    fsUp.disabled = !data.parent; renderCrumb(data.path); renderList(data.entries); renderRoots(data.roots);
  } catch (err) { fsError.hidden = false; fsError.textContent = 'Error loading directory: ' + err.message; }
}
function renderCrumb(full) {
  const win = full.indexOf('\\') >= 0 && full.indexOf('/') < 0;
  let parts = win ? full.split('\\').filter(Boolean) : ['/'].concat(full.split('/').filter(Boolean));
  if (win && parts.length && /^[A-Z]:$/i.test(parts[0])) parts[0] += '\\';
  let acc = '';
  fsCrumb.innerHTML = parts.map((p, i) => {
    if (!win) acc = i === 0 ? '/' : (acc.endsWith('/') ? acc : acc + '/') + p; else acc = i === 0 ? p : acc + (acc.endsWith('\\') ? '' : '\\') + p;
    const a = `<a data-path="${esc(acc)}">${esc(p)}</a>`; return i === parts.length - 1 ? a : a + '<span class="sep">/</span>';
  }).join('');
  fsCrumb.querySelectorAll('a').forEach(a => a.addEventListener('click', () => loadDir(a.dataset.path)));
}
function renderList(entries) {
  if (!entries.length) { fsList.innerHTML = `<div class="fs-empty">(empty${fsState.filter ? ' — no .' + fsState.filter + ' files here' : ''})</div>`; return; }
  fsList.innerHTML = entries.map(e => `<div class="fs-entry" data-type="${e.type}" data-path="${esc(e.path)}">
      <span class="icon">${e.type === 'dir' ? '📁' : '📄'}</span><span class="name">${esc(e.name)}</span>${e.type === 'file' ? `<span class="size">${fmtSize(e.size)}</span>` : ''}</div>`).join('');
  fsList.querySelectorAll('.fs-entry').forEach(el => {
    el.addEventListener('click', () => {
      if (el.dataset.type === 'dir') loadDir(el.dataset.path);
      else if (fsState.mode === 'file') { fsList.querySelectorAll('.selected').forEach(x => x.classList.remove('selected')); el.classList.add('selected');
        fsState.selected = el.dataset.path; fsSel.classList.add('has'); fsSel.textContent = el.dataset.path; fsSelectBtn.disabled = false; }
    });
    el.addEventListener('dblclick', () => { if (el.dataset.type === 'dir') loadDir(el.dataset.path); else if (fsState.mode === 'file') { fsState.selected = el.dataset.path; chooseAndClose(); } });
  });
}
function renderRoots(roots) {
  fsRoots.innerHTML = '<span>Roots:</span>' + roots.map(r => `<a data-path="${esc(r)}">${esc(r)}</a>`).join('');
  fsRoots.querySelectorAll('a').forEach(a => a.addEventListener('click', () => loadDir(a.dataset.path)));
}
function chooseAndClose() { if (!fsState.selected) return; const cb = fsState.onChoose; closePicker(); if (cb) cb(fsState.selected); }
fsUp.addEventListener('click', () => { if (fsState.parent) loadDir(fsState.parent); });
fsSelectBtn.addEventListener('click', chooseAndClose);
document.querySelectorAll('[data-close]').forEach(el => el.addEventListener('click', closePicker));
document.addEventListener('keydown', e => { if (!fsModal.hidden && e.key === 'Escape') closePicker(); });

// ------------------------------------------------------------------ cards
function fieldHtml(f) {
  if (f.type === 'select') {
    const opts = (f.options || []).map(([v, l]) => `<option value="${esc(v)}"${v === (f.value || '') ? ' selected' : ''}>${esc(l)}</option>`).join('');
    return `<div class="row"><label>${f.label}</label><select name="${f.name}">${opts}</select></div>`;
  }
  if (f.type === 'checkbox')
    return `<div class="row"><label>${f.label}</label><div class="check-wrap"><input type="checkbox" name="${f.name}"${f.value ? ' checked' : ''}><span>${f.hint || ''}</span></div></div>`;
  if (f.type === 'checks')
    return `<div class="row"><label>${f.label}</label><div class="checks">` + f.items.map(([n, l, hint, on]) =>
      `<label><input type="checkbox" name="${n}"${on ? ' checked' : ''}> ${l}${hint ? ` <small>${hint}</small>` : ''}</label>`).join('') + '</div></div>';
  const input = `<input type="text" name="${f.name}" placeholder="${esc(f.placeholder || '')}" value="${esc(f.value || '')}">`;
  if (f.pick)
    return `<div class="row"><label>${f.label}</label><div class="with-pick">${input}<button type="button" class="pick" data-pick="${f.pick.mode}" data-pick-filter="${f.pick.filter || ''}" data-pick-for="${f.name}">${f.pick.mode === 'dir' ? 'Browse folder' : 'Browse file'}</button></div></div>`;
  return `<div class="row"><label>${f.label}</label>${input}</div>`;
}
function makeCard(step, name, title, help, fields, buttonText, opts) {
  opts = opts || {};
  const div = document.createElement('div'); div.className = 'card'; div.dataset.name = name;
  div.innerHTML = `<div class="card-head"><span class="step">${step}</span><h3>${title}</h3><span class="badge">idle</span><span class="chev">▼</span></div>
    <div class="card-body">${help ? `<p class="help">${help}</p>` : ''}<form data-cmd="${name}">${fields.map(fieldHtml).join('')}
      <div class="actions"><button type="submit"${opts.gold ? ' class="gold"' : ''}>${buttonText}</button><button type="button" class="ghost dryrun">Show command</button>
      ${opts.reveal ? '<button type="button" class="ghost reveal" hidden>Open output folder</button>' : ''}${opts.note ? `<span class="note">${opts.note}</span>` : ''}</div></form></div>`;
  div.querySelector('.card-head').addEventListener('click', () => div.classList.toggle('open'));
  div.querySelectorAll('button.pick').forEach(btn => btn.addEventListener('click', e => {
    e.stopPropagation(); const inputEl = btn.closest('.row').querySelector(`input[name="${btn.dataset.pickFor}"]`);
    openPicker({mode: btn.dataset.pick, filter: btn.dataset.pickFilter || null, startPath: inputEl.value || undefined,
      title: btn.dataset.pick === 'dir' ? `Select folder for "${btn.dataset.pickFor}"` : `Select ${btn.dataset.pickFilter ? '.' + btn.dataset.pickFilter : 'file'} for "${btn.dataset.pickFor}"`,
      onChoose: p => { inputEl.value = p; }});
  }));
  const bodyOf = form => { const fd = new FormData(form); form.querySelectorAll('input[type=checkbox]').forEach(cb => fd.set(cb.name, cb.checked ? 'on' : '')); return new URLSearchParams(fd).toString(); };
  div.querySelector('form').addEventListener('submit', e => { e.preventDefault(); runCommand(name, bodyOf(e.target)); });
  div.querySelector('.dryrun').addEventListener('click', e => { e.stopPropagation(); previewCommand(name, bodyOf(e.target.closest('form'))); });
  const rv = div.querySelector('.reveal');
  if (rv) rv.addEventListener('click', e => { e.stopPropagation(); fetch('/preview/' + name, {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: bodyOf(rv.closest('form'))}).then(r => r.json()).then(j => { if (j.reveal) openFolder(j.reveal); else if (j.error) appendLog('[error] ' + j.error, 'err'); }); });
  cardEls[name] = div; return div;
}
function group(label) { const d = document.createElement('div'); d.className = 'group'; d.textContent = label; return d; }

// ------------------------------------------------------------------ status
function depRow(label, v, okText) {
  const ok = !!v; return [label, ok ? (okText || v) : 'not found', ok ? 'ok' : 'bad'];
}
async function loadStatus() {
  const d = await (await fetch('/detect')).json(); detected = d;
  document.getElementById('ver').textContent = 'v' + d.deps.version;
  const box = document.getElementById('status-box');
  const rows = [
    ['Python', d.deps.python + (d.deps.frozen ? ' (bundled)' : ''), 'ok'],
    [d.deps.frozen ? 'Engine' : 'Kit scripts', d.deps.scripts ? d.deps.cli : 'MISSING: ' + d.deps.cli, d.deps.scripts ? 'ok' : 'bad'],
    depRow('ffmpeg (voice, music, movies)', d.deps.ffmpeg),
    depRow('numpy (previews, PNG import)', d.deps.numpy),
    depRow('Pillow (previews, PNG import)', d.deps.pillow),
    depRow('Blender', d.deps.blender),
    depRow('dap_decode.py (sound effects)', d.deps.dap_decode),
    ['Disc', d.default_iso ? 'checking…' : 'no ISO found — use Browse file', d.default_iso ? 'warn' : 'bad'],
  ];
  box.innerHTML = rows.map(([k, v, cls]) => `<div class="row"><span>${k}</span><span class="v ${cls}" title="${esc(String(v))}">${esc(String(v))}</span></div>`).join('');
  if (d.default_iso) probeDisc(d.default_iso);
  return d;
}
async function probeDisc(iso) {
  const p = await (await fetch('/probe?iso=' + encodeURIComponent(iso))).json();
  const v = document.querySelector('#status-box .row:last-child .v');
  if (p.error) { v.className = 'v bad'; v.textContent = p.error; return; }
  const txt = `${p.serial || '?'} · ${p.files.toLocaleString()} files · ${fmtSize(p.bytes)}`;
  v.className = 'v ' + (p.yakuza ? 'ok' : 'warn'); v.textContent = (p.yakuza ? '' : 'not SLUS-21348: ') + txt; v.title = iso;
}

// ------------------------------------------------------------------ boot
(async () => {
  const d = await loadStatus();
  const iso = d.default_iso || '', work = d.default_out || '';
  const modIso = iso ? iso.replace(/\.iso$/i, '') + ' (MOD).iso' : '';
  const isoF = {name: 'iso', label: 'ISO', value: iso, pick: {mode: 'file', filter: 'iso'}};
  const workF = {name: 'work', label: 'Work folder', value: work, pick: {mode: 'dir'}};
  const filterF = {name: 'filter', label: 'Filter', placeholder: 'only in-game paths containing this, e.g. MEDIA3/ENEMY/E333 (optional)'};
  const jobsF = {name: 'jobs', label: 'Parallel jobs', placeholder: 'default: half your cores'};
  const cards = document.getElementById('cards');

  cards.appendChild(group('Disc'));
  cards.appendChild(makeCard('1', 'probe', 'Check the disc',
    'Reads <code>SYSTEM.CNF</code> and the four <code>OGREDIR*.BIN</code> indexes and reports the serial, the volumes and the file count — a one-second sanity check that this is the <em>Yakuza (USA)</em> image before you spend an hour on it.',
    [isoF], 'Check'));
  cards.appendChild(makeCard('2', 'list', 'List the disc',
    'Every file inside the CVM volumes with size, LBA and in-game path (<code>MEDIA/</code>, <code>MEDIA2/</code> …). 15,612 lines for the whole disc; pick a volume or type a filter to keep it readable.',
    [isoF, {name: 'volume', label: 'Volume', type: 'select', options: [['', 'All four'], ['MEDIA', 'MEDIA  (4,933: characters, stages, UI, SE)'], ['MEDIA2', 'MEDIA2  (7,175: voice, music, movies)'], ['MEDIA3', 'MEDIA3  (3,491: enemies, street blocks)'], ['MEDIA4', 'MEDIA4  (13: minigame overlays)']]}, filterF],
    'List'));
  cards.appendChild(makeCard('3', 'extract', 'Extract the files',
    'Dumps the files to <code>WORK/dump/</code> in the in-game tree and writes <code>manifest.csv</code> next to them (volume, path, LBA, size, ISO offset). ~4.3 GB and a few minutes for the full disc; a filter extracts just a slice. The CVM password is never needed: the engine\'s own index locates every file.',
    [isoF, workF, filterF, {name: 'skip_existing', label: 'Resume', type: 'checkbox', hint: 'skip files already present with the right size'}],
    'Extract', {reveal: true}));
  cards.appendChild(makeCard('4', 'verify-dump', 'Verify the dump',
    'Re-checks every row of <code>manifest.csv</code>: file present, byte size right, and — with the ISO given — the first 4 KB of each file compared with the disc. Zero problems means the dump is clean.',
    [workF, {name: 'iso', label: 'ISO (optional)', value: iso, pick: {mode: 'file', filter: 'iso'}}], 'Verify'));
  cards.appendChild(makeCard('·', 'census', 'Census',
    'Extension × magic-bytes table straight from the disc: what kinds of files are there and how many of each.',
    [isoF], 'Census'));

  cards.appendChild(group('Convert'));
  cards.appendChild(makeCard('5', 'convert', 'Convert everything',
    'The whole browse pipeline in one go, into <code>WORK/browse/</code>: textures → PNG (83k), models → OBJ+MTL (37k meshes), rendered previews, voice+music → WAV, sound effects → WAV, movies → MP4. Kinds whose tool is missing (see <em>Environment</em>) are skipped, not fatal. Hours for the full disc; a filter limits it to one character or street block.',
    [workF, {name: 'kinds', label: 'Kinds', type: 'checks', items: [['textures', 'Textures', 'PNG', true], ['models', 'Models', 'OBJ + MTL', true], ['previews', 'Previews', 'numpy + Pillow', true], ['audio', 'Voice + music', 'ffmpeg', true], ['sfx', 'Sound effects', 'dap_decode', true], ['movies', 'Movies', 'ffmpeg', true]]},
     filterF, jobsF, {name: 'size', label: 'Preview size', placeholder: '512'}],
    'Convert', {reveal: true, gold: true}));
  cards.appendChild(makeCard('6', 'textures', 'Textures → PNG',
    'Walks every container, inflates the AVLZ leaves and writes each <code>TXBP</code> texture as PNG under <code>WORK/browse/textures/&lt;in-game path&gt;/&lt;leaf&gt;_&lt;n&gt;.png</code>. Pure Python; ~83k images for the whole disc.',
    [workF, filterF, jobsF], 'Convert textures', {reveal: true}));
  cards.appendChild(makeCard('7', 'models', 'Models → OBJ',
    'Every <code>OME</code> mesh as OBJ with UVs and normals, one group per sub-mesh, plus an MTL pointing at the sibling PNGs (run textures first for the materials to resolve). Writes the <code>.json</code> attachment sidecars Blender assembly uses.',
    [workF, filterF, jobsF], 'Convert models', {reveal: true}));
  cards.appendChild(makeCard('8', 'previews', 'Rendered previews',
    'A textured, lit PNG of every mesh and a composite per file, so you can flip through 37k meshes in an image viewer. Needs <code>numpy</code> + <code>Pillow</code> and the textures folder.',
    [workF, filterF, jobsF, {name: 'size', label: 'Image size (px)', placeholder: '512'}], 'Render previews', {reveal: true}));
  cards.appendChild(makeCard('9', 'audio', 'Voice + music → WAV',
    '4,813 <code>AHX</code> voice clips (named by speaker and chapter) and 132 <code>ADX</code> music tracks through ffmpeg. The 80% of clips with the AHX type-0x11 header ffmpeg refuses are relabelled on the way in.',
    [workF, {name: 'filter', label: 'Filter', placeholder: 'e.g. KIRYU_17, HARUKA, BGM (optional)'}, jobsF], 'Convert audio', {reveal: true}));
  cards.appendChild(makeCard('10', 'sfx', 'Sound effects → WAV',
    '<code>ps2_DTPK</code> banks (<code>MEDIA/SE</code>, <code>MEDIA/SESET</code> and container leaves) → one WAV per cue, with the Xenosaga III kit\'s pure-Python SPU-ADPCM decoder (found automatically next door, or copy <code>dap_decode.py</code> in).',
    [workF, {name: 'filter', label: 'Filter', placeholder: 'e.g. MEDIA/SE, AMB (optional)'}, jobsF], 'Convert sound effects', {reveal: true}));
  cards.appendChild(makeCard('11', 'movies', 'Movies → MP4',
    '41 Sofdec (<code>.SFD</code>) cutscenes → H.264/AAC MP4 via ffmpeg. The slowest step; three encodes at a time.',
    [workF, {name: 'filter', label: 'Filter', placeholder: 'e.g. OP, ED, M01 (optional)'}, jobsF], 'Convert movies', {reveal: true}));

  cards.appendChild(group('Look'));
  cards.appendChild(makeCard('12', 'blender', 'Open a character in Blender',
    'Characters are split across files (body, head, shared hands); this assembles one from <code>WORK/browse/models</code>, binds the textures, snaps the hands to the wrists and opens Blender. Story characters live in the enemy sets: <code>E…00</code> body, <code>E…01</code> head. Any folder works too (a whole street block, say).',
    [workF, {name: 'preset', label: 'Preset', type: 'select', options: [['', '— pick one, or type below —'], ['E333', 'E333 · Kiryu, shirtless, dragon tattoo'], ['MEDIA/PLAYER/PLAYER0.DAT', 'PLAYER0 · Kiryu, grey suit (adventure)'], ['MEDIA/PLAYER/BATTLE.DAT', 'BATTLE · Kiryu, battle outfit'], ['E230', 'E230 · Majima, grey suit'], ['E231', 'E231 · Majima, suit and tie'], ['E044', 'E044'], ['MEDIA3/ARC/DAY_KAB0000.ARC', 'DAY_KAB0000 · a Kamurocho street block, daytime']]},
     {name: 'target', label: 'Or set / path', placeholder: 'E123, MEDIA/PLAYER/BATTLE.DAT, MEDIA3/ARC/NIGHT_KAB0000.ARC …'}],
    'Open in Blender'));

  cards.appendChild(group('Mod'));
  cards.appendChild(makeCard('13', 'texture', 'Import a texture',
    'Puts a PNG into one <code>TXBP</code> slot: quantised to the slot\'s palette (16 / 256 colours; RGBA32 takes the pixels as-is), re-encoded, recompressed with the engine\'s LZSS, nested back with every offset preserved, and the disc index updated. The PNG must be the slot\'s size, and must compress to no more than the original — <em>Fit</em> shrinks the palette until it does. The path, leaf and index are the ones in the texture\'s PNG name: <code>MEDIA/PLAYER/PLAYER0.DAT/0-0_0.png</code> = leaf <code>/0/0</code>, index 0. Leave the output blank to patch the ISO in place.',
    [isoF, {name: 'file', label: 'In-game path', value: '/MEDIA/PLAYER/PLAYER0.DAT'}, {name: 'leaf', label: 'Leaf path', value: '/0/0'},
     {name: 'index', label: 'Texture index', value: '0'}, {name: 'png', label: 'PNG', pick: {mode: 'file', filter: 'png'}},
     {name: 'out', label: 'Output ISO', value: modIso, placeholder: 'blank = patch the ISO above in place'},
     {name: 'fit', label: 'Fit', type: 'checkbox', value: true, hint: 'reduce the palette until the texture fits its slot'},
     {name: 'allow_grow', label: 'Allow growth', type: 'checkbox', hint: 'experimental: rebuild the containers to grow the slot'}],
    'Import texture'));
  cards.appendChild(makeCard('14', 'patch', 'Replace a file',
    'Writes any edited file over its original extent (same sector count or fewer) and fixes its size in the <code>OGREDIR</code> index. Works for anything on the disc: data tables, models, sound banks. Leave the output blank to patch in place.',
    [isoF, {name: 'file', label: 'In-game path', placeholder: '/MEDIA/MAP/MAP_ADV.DAT'}, {name: 'with', label: 'Replacement file', pick: {mode: 'file'}},
     {name: 'out', label: 'Output ISO', value: modIso, placeholder: 'blank = patch the ISO above in place'}],
    'Replace file'));
  cards.appendChild(makeCard('15', 'verify-iso', 'Verify an ISO',
    'Checks that every <code>OGREDIR</code> entry still points inside its CVM — run it on a modded image before booting it.',
    [{name: 'iso', label: 'ISO', value: modIso || iso, pick: {mode: 'file', filter: 'iso'}}], 'Verify'));

  cards.querySelector('[data-name="extract"]').classList.add('open');
  refreshJobs();
})();
document.getElementById('btn-refresh').addEventListener('click', loadStatus);
document.getElementById('btn-open-all').addEventListener('click', e => {
  const all = [...document.querySelectorAll('.card')], open = all.some(c => !c.classList.contains('open'));
  all.forEach(c => c.classList.toggle('open', open)); e.target.textContent = open ? 'Collapse all' : 'Expand all';
});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "YakuzaGUI/2.0"

    def log_message(self, fmt, *args):  # noqa: N802
        return

    def _send(self, status, body, ctype="text/plain"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload), "application/json")

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        params = parse_qs(url.query, keep_blank_values=True)
        if url.path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        if url.path == "/detect":
            isos = detect_isos()
            default_iso = isos[0] if isos else ""
            return self._json(200, {"root": str(ROOT), "isos": isos, "default_iso": default_iso,
                                    "default_out": default_out_dir(default_iso), "deps": detect_deps()})
        if url.path == "/probe":
            return self._json(200, probe_iso(params.get("iso", [""])[0]))
        if url.path == "/browse-fs":
            return self._json(200, list_dir(params.get("path", [""])[0], params.get("filter", [""])[0] or None))
        if url.path == "/jobs":
            with JOBS_LOCK:
                jobs = list(JOBS.values())
            return self._json(200, [j.to_summary() for j in reversed(jobs)][:40])
        if url.path.startswith("/job/"):
            with JOBS_LOCK:
                job = JOBS.get(url.path[5:])
            if not job:
                return self._json(404, {"error": "no such job"})
            return self._json(200, job.to_summary())
        if url.path.startswith("/stream/"):
            return self._stream(url.path[8:])
        return self._send(404, "not found")

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace") if length else "", keep_blank_values=True)
        if url.path.startswith("/preview/"):
            return self._preview(unquote(url.path[9:]), form)
        if url.path.startswith("/run/"):
            return self._run(unquote(url.path[5:]), form)
        if url.path.startswith("/stop/"):
            with JOBS_LOCK:
                job = JOBS.get(url.path[6:])
            if not job:
                return self._json(404, {"error": "no such job"})
            return self._json(200, {"stopped": job.stop()})
        if url.path == "/open":
            err = reveal(_s(form, "path"))
            return self._json(200 if not err else 400, {"error": err} if err else {"ok": True})
        return self._send(404, "not found")

    def _build(self, name, form):
        builder = BUILDERS.get(name)
        if not builder:
            raise ValueError(f"unknown command {name}")
        res = builder(form)
        return res if isinstance(res, tuple) else (res, None)

    def _preview(self, name, form):
        try:
            cmd, rv = self._build(name, form)
        except Exception as exc:
            return self._json(400, {"error": str(exc)})
        return self._json(200, {"cmd": cmd, "reveal": rv})

    def _run(self, name, form):
        try:
            cmd, rv = self._build(name, form)
        except Exception as exc:
            return self._json(400, {"error": str(exc)})
        try:
            job = Job(name, cmd, rv)
        except OSError as exc:
            shown = " ".join(shlex.quote(c) for c in cmd)
            return self._json(500, {"error": f"could not start:\n  $ {shown}\n  {type(exc).__name__}: {exc}"})
        with JOBS_LOCK:
            JOBS[job.id] = job
        return self._json(200, {"job_id": job.id, "reveal": rv})

    def _stream(self, job_id):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            return self._send(404, "no such job")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = job.subscribe()
        try:
            while True:
                try:
                    line = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if line is None:
                    self.wfile.write(b"data: __eof__\n\n")
                    self.wfile.flush()
                    return
                for chunk in line.replace("\r", "").split("\n"):
                    self.wfile.write(b"data: " + chunk.encode("utf-8") + b"\n")
                self.wfile.write(b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            job.unsubscribe(q)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    if FROZEN and not CLI_EXE.exists():
        _fatal("Engine missing", f"Expected {CLI_EXE.name} next to {Path(sys.executable).name}.\n\n"
               "Unzip the whole release folder before launching (running the exe straight from the "
               "zip preview will fail).")
    missing = [] if FROZEN else [s for s in KIT_SCRIPTS if not (ROOT / s).exists()]
    if missing:
        _fatal("Kit scripts missing", f"Expected next to gui.py in {ROOT}:\n  " + "\n  ".join(missing)
               + "\n\nRe-extract the kit so all its .py files sit together.")
    port = int(os.environ.get("PORT") or _free_port())
    url = f"http://localhost:{port}/"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        _fatal("Could not start local server", f"Failed to bind to 127.0.0.1:{port} — {exc}.")
    print(f"[gui] Yakuza (PS2) Extractor {VERSION}" + (" (bundled build)" if FROZEN else ""))
    print(f"[gui] Open: {url}")
    print("[gui] Press Ctrl+C to stop.")
    if "--no-browser" not in sys.argv:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[gui] stopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:
        import traceback
        _fatal("Unexpected error during startup", f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}")
