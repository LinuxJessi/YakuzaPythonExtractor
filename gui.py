"""gui.py — local web GUI for the Yakuza (PS2) Python Extractor.

Run me directly:
    python3 gui.py

Stdlib only. Starts an HTTP server on a free localhost port, opens your
browser, and shells out to the kit's command-line scripts for the real
work; their output streams back live via Server-Sent Events. Same shape as
the Xenosaga I/III extractors' GUI: one card per command, file pickers,
a live log.
"""
from __future__ import annotations

import http.server
import json
import os
import queue
import shlex
import socket
import socketserver
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


# ---------------------------------------------------------------------------
# Jobs: one subprocess each, output fanned out to SSE subscribers
# ---------------------------------------------------------------------------
class Job:
    def __init__(self, cmd):
        self.id = uuid.uuid4().hex[:12]
        self.cmd = cmd
        self.started_at = time.time()
        self.ended_at = None
        self.exit_code = None
        self.lines = []
        self.subscribers = []
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        self.lines.append("$ " + " ".join(shlex.quote(c) for c in cmd))
        threading.Thread(target=self._pump, daemon=True).start()

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
        self.lines.append(f"[exit {rc}]")
        self._broadcast(f"[exit {rc}]")
        self._broadcast(None)

    def subscribe(self):
        q = queue.Queue(maxsize=50000)
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


JOBS = {}
JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Command builders — form fields → argv
# ---------------------------------------------------------------------------
def _s(form, key, default=""):
    v = form.get(key, [default])
    return (v[0] if v else default).strip()


def _need(form, key, label):
    v = _s(form, key)
    if not v:
        raise ValueError(f"{label} is required")
    return v


def _script(name, *args):
    return [PY, str(ROOT / name), *args]


def _jobs(form):
    j = _s(form, "jobs")
    return ["--jobs", j] if j else []


def _filter(form):
    f = _s(form, "filter")
    return ["--filter", f] if f else []


def b_list(f):
    return _script("cli.py", "list", _need(f, "iso", "ISO"))


def b_census(f):
    return _script("cli.py", "census", _need(f, "iso", "ISO"))


def b_extract(f):
    return _script("cli.py", "extract", _need(f, "iso", "ISO"), "--out", _need(f, "dump", "dump folder"), *_filter(f))


def b_textures(f):
    return _script("browse.py", "textures", _need(f, "dump", "dump folder"), _need(f, "out", "output folder"),
                   *_filter(f), *_jobs(f))


def b_models(f):
    return _script("browse.py", "models", _need(f, "dump", "dump folder"), _need(f, "out", "output folder"),
                   *_filter(f), *_jobs(f))


def b_previews(f):
    return _script("render.py", _need(f, "dump", "dump folder"), _need(f, "textures", "textures folder"),
                   _need(f, "out", "output folder"), *_filter(f), *_jobs(f))


def b_audio(f):
    return _script("media.py", "audio", _need(f, "dump", "dump folder"), _need(f, "out", "output folder"), *_jobs(f))


def b_movies(f):
    return _script("media.py", "movies", _need(f, "dump", "dump folder"), _need(f, "out", "output folder"), *_jobs(f))


def b_sfx(f):
    return _script("media.py", "sfx", _need(f, "dump", "dump folder"), _need(f, "out", "output folder"), *_jobs(f))


def b_blender(f):
    return _script("blender_open.py", _need(f, "models", "models folder"), _need(f, "target", "character / path"))


def b_patch(f):
    cmd = _script("repack.py", "patch", _need(f, "iso", "ISO"), "--file", _need(f, "file", "in-game path"),
                  "--with", _need(f, "with", "replacement file"))
    if _s(f, "out"):
        cmd += ["--out", _s(f, "out")]
    return cmd


def b_texture(f):
    cmd = _script("repack.py", "texture", _need(f, "iso", "ISO"), "--file", _need(f, "file", "in-game path"),
                  "--leaf", _need(f, "leaf", "leaf path"), "--index", _s(f, "index", "0") or "0",
                  "--png", _need(f, "png", "PNG"))
    if _s(f, "out"):
        cmd += ["--out", _s(f, "out")]
    return cmd


def b_verify(f):
    return _script("repack.py", "verify", _need(f, "iso", "ISO"))


BUILDERS = {
    "list": b_list, "census": b_census, "extract": b_extract,
    "textures": b_textures, "models": b_models, "previews": b_previews,
    "audio": b_audio, "movies": b_movies, "sfx": b_sfx,
    "blender": b_blender, "patch": b_patch, "texture": b_texture, "verify": b_verify,
}


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------
def detect_isos():
    found = []
    for base in (ROOT, ROOT.parent, Path.home() / "Desktop", Path.home() / "Downloads"):
        try:
            for p in sorted(base.iterdir()):
                if p.suffix.lower() == ".iso" and p.stat().st_size > 1 << 30:
                    found.append(str(p))
        except OSError:
            pass
    pref = [p for p in found if "yakuza" in p.lower()]
    return pref + [p for p in found if p not in pref]


def detect_deps():
    import shutil
    deps = {"ffmpeg": bool(shutil.which("ffmpeg")),
            "blender": os.path.exists("/Applications/Blender.app") or bool(shutil.which("blender"))}
    for mod in ("numpy", "PIL"):
        try:
            __import__(mod)
            deps[mod] = True
        except ImportError:
            deps[mod] = False
    return deps


def list_dir(path_str):
    p = Path(path_str).expanduser() if path_str else Path.home()
    if not p.is_dir():
        p = p.parent if p.parent.is_dir() else Path.home()
    entries = []
    try:
        for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if c.name.startswith("."):
                continue
            entries.append({"name": c.name, "path": str(c), "dir": c.is_dir()})
    except OSError:
        pass
    return {"path": str(p), "parent": str(p.parent), "entries": entries[:400]}


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Yakuza (PS2) Extractor</title>
<style>
:root{--bg:#141418;--card:#1e1e26;--ink:#e8e8ee;--mut:#9a9aa8;--acc:#d4342b;--ok:#3fb950;--err:#f85149;--line:#2c2c38}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline}
header h1{margin:0;font-size:20px}header span{color:var(--mut)}
main{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:16px 24px}
section.cards{display:flex;flex-direction:column;gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card h2{margin:0 0 4px;font-size:15px}.card p{margin:0 0 10px;color:var(--mut);font-size:13px}
.row{display:flex;gap:8px;margin:6px 0;align-items:center}.row label{width:110px;color:var(--mut);font-size:12px}
.row input,.row select{flex:1;background:#101016;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
.row button.pick{padding:5px 8px}
button{background:#2a2a36;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:7px 12px;font:inherit;cursor:pointer}
button.run{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
.status{font-size:12px;color:var(--mut);margin-left:8px}
#log{background:#0b0b0f;border:1px solid var(--line);border-radius:10px;padding:12px;height:calc(100vh - 140px);overflow:auto;font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;position:sticky;top:16px}
.ok{color:var(--ok)}.err{color:var(--err)}
#deps{font-size:12px;color:var(--mut);margin-left:auto}#deps b{color:var(--ink)}
details summary{cursor:pointer;color:var(--mut);font-size:12px;margin:4px 0}
#picker{position:fixed;inset:0;background:#0008;display:none;align-items:center;justify-content:center}
#picker .box{background:var(--card);border:1px solid var(--line);border-radius:10px;width:620px;max-height:80vh;display:flex;flex-direction:column}
#picker .hd{padding:10px 14px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
#picker .hd input{flex:1}#picker .ls{overflow:auto;padding:6px 0}
#picker .ls div{padding:5px 14px;cursor:pointer}#picker .ls div:hover{background:#2a2a36}
#picker .ft{padding:10px 14px;border-top:1px solid var(--line);display:flex;gap:8px;justify-content:flex-end}
</style></head><body>
<header><h1>Yakuza (PS2) Extractor</h1><span>SLUS-21348 · local GUI</span><span id="deps"></span></header>
<main><section class="cards" id="cards"></section><div id="log">Ready.</div></main>
<div id="picker"><div class="box"><div class="hd"><button id="pk-up">↑</button><input id="pk-path"><button id="pk-go">Go</button></div>
<div class="ls" id="pk-list"></div><div class="ft"><button id="pk-cancel">Cancel</button><button class="run" id="pk-choose">Choose this folder</button></div></div></div>
<script>
const CARDS = [
 {id:'list', title:'1 · List disc', desc:'Every file inside the four CVM volumes (name, size, LBA).', fields:[['iso','ISO','file']]},
 {id:'extract', title:'2 · Extract', desc:'Dump all 15,612 files to a folder in the in-game tree (MEDIA/, MEDIA2/ …). ~4.3 GB.', fields:[['iso','ISO','file'],['dump','dump folder','dir'],['filter','filter (optional)','text']]},
 {id:'census', title:'Census', desc:'Extension × magic-bytes table, straight from the disc.', fields:[['iso','ISO','file']]},
 {id:'textures', title:'3 · Textures → PNG', desc:'Walk the containers, inflate AVLZ, write every TXBP texture as PNG (83k).', fields:[['dump','dump folder','dir'],['out','output folder','dir'],['filter','filter (optional)','text'],['jobs','parallel jobs','text']]},
 {id:'models', title:'4 · Models → OBJ', desc:'Every OME mesh as OBJ + MTL bound to the sibling PNGs (37k). Writes the .json attachment sidecars.', fields:[['dump','dump folder','dir'],['out','output folder','dir'],['filter','filter (optional)','text'],['jobs','parallel jobs','text']]},
 {id:'previews', title:'5 · Rendered previews', desc:'Textured, lit preview PNG per mesh and per file (needs numpy + Pillow).', fields:[['dump','dump folder','dir'],['textures','textures folder','dir'],['out','output folder','dir'],['filter','filter (optional)','text'],['jobs','parallel jobs','text']]},
 {id:'blender', title:'Open in Blender', desc:'Assemble a character (E333 = shirtless Kiryu, E230 = Majima, MEDIA/PLAYER/PLAYER0.DAT …) or any folder and open it in Blender.', fields:[['models','models folder','dir'],['target','set / path','text']]},
 {id:'audio', title:'6 · Voice + music → WAV', desc:'4,813 AHX voice clips and 132 ADX tracks via ffmpeg.', fields:[['dump','dump folder','dir'],['out','output folder','dir'],['jobs','parallel jobs','text']]},
 {id:'sfx', title:'7 · Sound effects → WAV', desc:'ps2_DTPK banks → per-cue WAV (needs the Xenosaga III kit\'s dap_decode.py next door or copied in).', fields:[['dump','dump folder','dir'],['out','output folder','dir'],['jobs','parallel jobs','text']]},
 {id:'movies', title:'8 · Movies → MP4', desc:'41 Sofdec cutscenes → H.264/AAC via ffmpeg.', fields:[['dump','dump folder','dir'],['out','output folder','dir'],['jobs','parallel jobs','text']]},
 {id:'texture', title:'9 · Import a texture (mod)', desc:'Replace one TXBP texture with a PNG of the same size and write it back into the ISO (or a copy).', fields:[['iso','ISO','file'],['file','in-game path','text','/MEDIA/PLAYER/PLAYER0.DAT'],['leaf','leaf path','text','/0/0'],['index','texture index','text','0'],['png','PNG','file'],['out','output ISO (optional)','text']]},
 {id:'patch', title:'10 · Replace a file (mod)', desc:'Write any edited file back over its original extent (same sector count or fewer) and fix the OGREDIR index.', fields:[['iso','ISO','file'],['file','in-game path','text'],['with','replacement file','file'],['out','output ISO (optional)','text']]},
 {id:'verify', title:'Verify ISO', desc:'Check every OGREDIR entry still points inside its CVM.', fields:[['iso','ISO','file']]},
];
let detect={};
function h(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function build(){
  const el=document.getElementById('cards');
  el.innerHTML=CARDS.map(c=>`<div class="card" id="card-${c.id}"><h2>${h(c.title)}</h2><p>${h(c.desc)}</p>`+
    c.fields.map(([k,l,t,d])=>`<div class="row"><label>${h(l)}</label><input name="${k}" value="${h(defaultFor(k,d||''))}">${t!=='text'?`<button class="pick" data-kind="${t}" data-for="${c.id}:${k}">…</button>`:''}</div>`).join('')+
    `<div class="row"><button class="run" data-run="${c.id}">Run</button><span class="status" id="st-${c.id}"></span></div></div>`).join('');
  el.querySelectorAll('button[data-run]').forEach(b=>b.onclick=()=>run(b.dataset.run));
  el.querySelectorAll('button.pick').forEach(b=>b.onclick=()=>pick(b.dataset.for,b.dataset.kind));
}
function defaultFor(k,d){
  if(d) return d;
  const base=detect.default_out||'';
  return {iso:detect.default_iso||'',dump:base?base+'/dump':'',out:'',textures:base?base+'/browse/textures':'',models:base?base+'/browse/models':''}[k]||'';
}
function formOf(id){const o={};document.querySelectorAll(`#card-${id} input`).forEach(i=>o[i.name]=i.value);
  if(!o.out&&['textures','models','previews','audio','sfx','movies'].includes(id)&&detect.default_out){o.out=detect.default_out+'/browse/'+{textures:'textures',models:'models',previews:'previews',audio:'audio',sfx:'sfx',movies:'movies'}[id];}
  return o;}
async function run(id){
  const st=document.getElementById('st-'+id); st.textContent='starting…';
  const r=await fetch('/run/'+id,{method:'POST',body:new URLSearchParams(formOf(id))});
  const j=await r.json(); if(j.error){st.innerHTML='<span class="err">'+h(j.error)+'</span>';return;}
  st.textContent='running'; const log=document.getElementById('log'); log.textContent='';
  const es=new EventSource('/stream/'+j.job_id);
  es.onmessage=e=>{if(e.data==='__eof__'){es.close();fetch('/job/'+j.job_id).then(r=>r.json()).then(s=>{st.innerHTML=s.status==='ok'?'<span class="ok">done</span>':'<span class="err">exit '+s.exit_code+'</span>';});return;}
    log.textContent+=e.data+'\n'; log.scrollTop=log.scrollHeight;};
}
let pkTarget=null;
async function pick(target,kind){pkTarget={target,kind}; const [id,k]=target.split(':'); const cur=document.querySelector(`#card-${id} input[name=${k}]`).value; await pkLoad(cur||''); document.getElementById('picker').style.display='flex';}
async function pkLoad(p){const r=await fetch('/browse-fs?path='+encodeURIComponent(p)); const j=await r.json();
  document.getElementById('pk-path').value=j.path; document.getElementById('pk-up').onclick=()=>pkLoad(j.parent);
  document.getElementById('pk-list').innerHTML=j.entries.map(e=>`<div data-p="${h(e.path)}" data-d="${e.dir?1:0}">${e.dir?'📁':'📄'} ${h(e.name)}</div>`).join('');
  document.getElementById('pk-list').querySelectorAll('div').forEach(d=>d.onclick=()=>{if(d.dataset.d==='1')pkLoad(d.dataset.p);else pkSet(d.dataset.p);});}
function pkSet(p){const [id,k]=pkTarget.target.split(':'); document.querySelector(`#card-${id} input[name=${k}]`).value=p; document.getElementById('picker').style.display='none';}
document.getElementById('pk-go').onclick=()=>pkLoad(document.getElementById('pk-path').value);
document.getElementById('pk-cancel').onclick=()=>document.getElementById('picker').style.display='none';
document.getElementById('pk-choose').onclick=()=>pkSet(document.getElementById('pk-path').value);
fetch('/detect').then(r=>r.json()).then(d=>{detect=d; build();
  document.getElementById('deps').innerHTML=Object.entries(d.deps).map(([k,v])=>`${k}: <b class="${v?'ok':'err'}">${v?'yes':'no'}</b>`).join(' · ');});
</script></body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "YakuzaGUI/1.0"

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
        if url.path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        if url.path == "/detect":
            isos = detect_isos()
            default_out = str(ROOT.parent / "Yakuza (USA)") if (ROOT.parent / "Yakuza (USA)").is_dir() else str(ROOT / "out")
            return self._json(200, {"isos": isos, "default_iso": isos[0] if isos else "",
                                    "default_out": default_out, "deps": detect_deps()})
        if url.path == "/browse-fs":
            params = parse_qs(url.query, keep_blank_values=True)
            return self._json(200, list_dir(params.get("path", [""])[0]))
        if url.path.startswith("/job/"):
            with JOBS_LOCK:
                job = JOBS.get(url.path[5:])
            if not job:
                return self._json(404, {"error": "no such job"})
            return self._json(200, {"id": job.id, "status": "running" if job.exit_code is None else
                                    ("ok" if job.exit_code == 0 else "err"), "exit_code": job.exit_code})
        if url.path.startswith("/stream/"):
            return self._stream(url.path[8:])
        return self._send(404, "not found")

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace") if length else "", keep_blank_values=True)
        if url.path.startswith("/run/"):
            name = unquote(url.path[5:])
            builder = BUILDERS.get(name)
            if not builder:
                return self._json(400, {"error": f"unknown command {name}"})
            try:
                cmd = builder(form)
                job = Job(cmd)
            except (ValueError, OSError) as exc:
                return self._json(400, {"error": str(exc)})
            with JOBS_LOCK:
                JOBS[job.id] = job
            return self._json(200, {"job_id": job.id})
        return self._send(404, "not found")

    def _stream(self, job_id):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            return self._send(404, "no such job")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
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
    port = int(os.environ.get("PORT") or _free_port())
    url = f"http://localhost:{port}/"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[gui] Yakuza (PS2) Extractor — open {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[gui] stopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
