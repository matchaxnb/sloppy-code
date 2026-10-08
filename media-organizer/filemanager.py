#!/usr/bin/env python3
"""reflink-filemanager — a tiny two-pane web UI for arranging MediaLibrary.

Why: dragging files over SMB performs a real copy (data duplicated). This runs
ON the container, so a "copy" is `cp --reflink=always` — a metadata-only clone
with shared blocks on the pool.

Safety model:
  * reads   : anywhere under the browse root, read-only
  * writes  : ONLY inside the media root (the library)
  * copy    : cp --reflink=always; never a full copy, never overwrites
  * move    : reflink + verify (same inode size) + unlink source; only files
              that are already inside MEDIA_ROOT may be moved
  * sources outside MediaLibrary are NEVER modified

Roots come from the environment (see config.py); override with --browse-root /
--media-root. Nothing here is host-specific.

Run:  .venv/bin/python filemanager.py --port 8099
"""
from __future__ import annotations
import argparse, html, json, os, posixpath, subprocess, sys, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config as C

# Resolved from the environment (see config.py): the read-only browse root
# defaults to the media mount, the writable root to its MediaLibrary.
BROWSE_ROOT = None
MEDIA_ROOT = None
MAX_ENTRIES = 3000


def _default_roots():
    return C.media_root(), C.media_library()


def _safe_join(base: str, rel: str) -> str:
    """Join base + user path, refusing any escape above base."""
    rel = (rel or "").strip().lstrip("/")
    p = os.path.realpath(os.path.join(base, rel))
    base_r = os.path.realpath(base)
    if p != base_r and not p.startswith(base_r + os.sep):
        raise PermissionError("path escapes root")
    return p


def _inside(path: str, root: str) -> bool:
    pr, rr = os.path.realpath(path), os.path.realpath(root)
    return pr == rr or pr.startswith(rr + os.sep)


def listdir(path: str) -> dict:
    if not os.path.isdir(path):
        raise NotADirectoryError(path)
    entries = []
    with os.scandir(path) as it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append({
                "name": e.name,
                "dir": e.is_dir(follow_symlinks=False),
                "size": st.st_size if e.is_file(follow_symlinks=False) else 0,
                "mtime": int(st.st_mtime),
            })
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    truncated = len(entries) > MAX_ENTRIES
    return {"path": path, "entries": entries[:MAX_ENTRIES], "truncated": truncated}


def _rel_to(path: str, root: str) -> str:
    return posixpath.relpath(path, root)


def copy_reflink(srcs, dest_dir: str) -> list[dict]:
    """Reflink each source into dest_dir. Never overwrites; never full-copies."""
    if not _inside(dest_dir, MEDIA_ROOT):
        raise PermissionError(f"destination outside MediaLibrary: {dest_dir}")
    os.makedirs(dest_dir, exist_ok=True)
    out = []
    for src in srcs:
        src = os.path.realpath(src)
        if not os.path.exists(src):
            out.append({"src": src, "status": "missing"}); continue
        if _inside(src, dest_dir):
            out.append({"src": src, "status": "already here"}); continue
        name = os.path.basename(src.rstrip("/"))
        dst = os.path.join(dest_dir, name)
        if os.path.exists(dst):
            stem, ext = os.path.splitext(name)
            dst = os.path.join(dest_dir, f"{stem}__copy{ext}")
        try:
            # Serialized with any running apply: the gate takes a cross-process
            # file lock, applies the EAGAIN sync-retry, and never full-copies.
            from orchestrator import clone_serialized
            clone_serialized(src, dst, recursive=os.path.isdir(src))
            out.append({"src": src, "dst": dst, "status": "reflinked"})
        except Exception as e:  # noqa: BLE001
            out.append({"src": src, "status": str(e)})
    return out


def move_reflink(srcs, dest_dir: str) -> list[dict]:
    """Reflink then delete source — ONLY for sources already in MediaLibrary.
    Equivalent to a rename with a shared-block copy as the durable step."""
    res = copy_reflink(srcs, dest_dir)
    for r in res:
        if r.get("status") != "reflinked":
            continue
        src, dst = r["src"], r["dst"]
        if not _inside(src, MEDIA_ROOT):
            r["status"] = "refused delete: source outside MediaLibrary"
            continue
        try:
            if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
                os.remove(src)
                r["status"] = "moved"
            else:
                r["status"] = "copied (size mismatch, source kept)"
        except OSError as e:
            r["status"] = f"copied (delete failed: {e})"
    return res


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "reflink-fm/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, s, code=200, ctype="text/html; charset=utf-8"):
        body = s.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._text(PAGE)
            if u.path == "/api/list":
                side = q.get("side", ["browse"])[0]
                base = MEDIA_ROOT if side == "media" else BROWSE_ROOT
                rel = q.get("path", [""])[0]
                p = _safe_join(base, rel)
                d = listdir(p)
                d["side"] = side
                d["rel"] = _rel_to(p, base)
                d["writable"] = _inside(p, MEDIA_ROOT)
                return self._json(d)
            if u.path == "/api/roots":
                return self._json({"browse": BROWSE_ROOT, "media": MEDIA_ROOT})
            return self._json({"error": "not found"}, 404)
        except (PermissionError, NotADirectoryError, FileNotFoundError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            b = self._body()
            if u.path == "/api/mkdir":
                d = _safe_join(MEDIA_ROOT, b.get("path", ""))
                if not _inside(d, MEDIA_ROOT):
                    raise PermissionError("mkdir outside MediaLibrary")
                os.makedirs(d, exist_ok=True)
                return self._json({"ok": True, "path": d})
            if u.path in ("/api/copy", "/api/move"):
                dest = b.get("dest", "")
                dest_dir = _safe_join(MEDIA_ROOT, dest)
                fn = move_reflink if u.path == "/api/move" else copy_reflink
                res = fn(b.get("srcs", []), dest_dir)
                return self._json({"results": res})
            return self._json({"error": "not found"}, 404)
        except (PermissionError, NotADirectoryError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>MediaLibrary reflink arranger</title>
<style>
:root{--bg:#111;--fg:#eee;--muted:#888;--card:#1b1b1b;--b:1px solid #333;--ok:#3c9;--err:#e55;}
*{box-sizing:border-box}body{margin:0;font:14px/1.4 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:8px 12px;border-bottom:var(--b);display:flex;gap:12px;align-items:center}
header b{color:var(--ok)} code{color:var(--muted)}
.wrap{display:grid;grid-template-columns:1fr 1fr;height:calc(100vh - 46px)}
.pane{overflow:auto;border-right:var(--b);padding:6px}
.pane h3{margin:6px;color:var(--muted);font-weight:600;text-transform:uppercase;font-size:11px;letter-spacing:.08em}
.row{display:flex;gap:8px;align-items:center;padding:3px 6px;border-radius:5px;cursor:pointer;user-select:none}
.row:hover{background:var(--card)}.row.sel{background:#243} .row.drop{outline:2px dashed var(--ok)}
.name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dir .name{color:#8cf}.size{color:var(--muted);font-size:11px}
.crumb{padding:4px 6px;color:var(--muted);font-size:12px;display:flex;gap:6px;flex-wrap:wrap}
.crumb a{color:#8cf;cursor:pointer;text-decoration:none}
#log{position:fixed;bottom:0;left:0;right:0;max-height:30%;overflow:auto;background:#000c;border-top:var(--b);
     padding:6px 10px;font:12px/1.4 ui-monospace,monospace;display:none}
#log div.ok{color:var(--ok)}#log div.err{color:var(--err)}
button{background:#222;color:var(--fg);border:var(--b);border-radius:5px;padding:4px 8px;cursor:pointer}
</style></head><body>
<header>
  <b>reflink arranger</b>
  <span><code id="broot"></code> → <code id="mroot"></code></span>
  <button id="clear">clear selection</button>
  <button id="showlog">log</button>
</header>
<div class="wrap">
  <div class="pane" id="left"><h3>source (read-only)</h3><div class="crumb" id="lc"></div><div id="ll"></div></div>
  <div class="pane" id="right"><h3>MediaLibrary (drop here)</h3><div class="crumb" id="rc"></div><div id="rl"></div></div>
</div>
<div id="log"></div>
<script>
const S={browse:{base:"",rel:""},media:{base:"",rel:""},sel:new Set()};
const $=s=>document.querySelector(s);
function fmt(n){if(!n)return"";const u=["B","K","M","G","T"];let i=0;while(n>=1024&&i<u.length-1){n/=1024;i++}return n.toFixed(n<10&&i?1:0)+u[i]}
function log(m,cls){const l=$("#log");l.style.display="block";const d=document.createElement("div");d.className=cls||"";d.textContent=m;l.appendChild(d);l.scrollTop=l.scrollHeight}
async function api(p,body){const r=await fetch(p,body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:undefined);const j=await r.json();if(j.error)throw new Error(j.error);return j}
function crumbs(side,rel){const el=side=="browse"?$("#lc"):$("#rc");const parts=rel?rel.split("/"):[];let acc="";let h=`<a data-side="${side}" data-rel="">/</a>`;for(const p of parts){acc=acc?acc+"/"+p:p;h+=` <a data-side="${side}" data-rel="${acc}">${p}</a> /`}el.innerHTML=h;el.querySelectorAll("a").forEach(a=>a.onclick=()=>load(a.dataset.side,a.dataset.rel))}
async function load(side,rel){
  const d=await api(`/api/list?side=${side}&path=${encodeURIComponent(rel||"")}`);
  S[side]={base:side=="media"?d.path:d.path,rel:d.rel};
  crumbs(side,d.rel);
  const list=side=="browse"?$("#ll"):$("#rl");list.innerHTML="";
  if(d.rel!==""){const up=d.rel.split("/").slice(0,-1).join("/");list.appendChild(mkrow({name:"..",dir:true},side,up,true))}
  for(const e of d.entries)list.appendChild(mkrow(e,side,posixjoin(d.rel,e.name)));
  if(d.truncated)log("listing truncated at "+3000+" entries","err");
}
function posixjoin(a,b){return a?a+"/"+b:b}
function mkrow(e,side,rel,isUp){
  const el=document.createElement("div");el.className="row"+(e.dir?" dir":"");
  const p=e.dir?rel:rel; // path relative to side base
  el.innerHTML=`<span class="name">${e.dir?"📁":"🎬"} ${e.name}</span><span class="size">${fmt(e.size)}</span>`;
  el.draggable=!isUp&&!e.dir; // drag files (and dirs) out
  if(e.dir&&!isUp)el.draggable=true;
  el.ondragstart=ev=>{const abs=side=="browse"?joinAbs(S.browse.base,p):joinAbs(S.media.base,p);
    ev.dataTransfer.setData("text/plain",JSON.stringify([abs]));ev.dataTransfer.effectAllowed="copyMove"};
  if(e.dir){el.onclick=()=>load(side,rel)}
  if(side=="browse"&&!e.dir){ // click to select for batch
    el.onclick=()=>{const abs=joinAbs(S.browse.base,p);if(S.sel.has(abs)){S.sel.delete(abs);el.classList.remove("sel")}else{S.sel.add(abs);el.classList.add("sel")}};
  }
  if(side=="media"&&!isUp){ // droppable target folder
    el.ondragover=ev=>{ev.preventDefault();el.classList.add("drop")};
    el.ondragleave=()=>el.classList.remove("drop");
    el.ondrop=async ev=>{ev.preventDefault();el.classList.remove("drop");
      const srcs=JSON.parse(ev.dataTransfer.getData("text/plain")||"[]");
      const dest=rel; // relative to MEDIA base
      try{const r=await api("/api/copy",{srcs,dest});r.results.forEach(x=>log((x.status=="reflinked"?"✓ ":"✗ ")+x.src+" → "+(x.dst||x.status),x.status=="reflinked"?"ok":"err"))}
      catch(e){log("error: "+e.message,"err")}
      load("media",S.media.rel)};
  }
  return el;
}
function joinAbs(base,rel){return rel?base+"/"+rel:base}
$("#clear").onclick=()=>{S.sel.clear();document.querySelectorAll("#ll .sel").forEach(e=>e.classList.remove("sel"))};
$("#showlog").onclick=()=>{const l=$("#log");l.style.display=l.style.display=="block"?"none":"block"};
(async()=>{const r=await api("/api/roots");$("#broot").textContent=r.browse;$("#mroot").textContent=r.media;await load("browse","");await load("media","")})();
</script></body></html>"""


def main(argv=None):
    global BROWSE_ROOT, MEDIA_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("MEDIA_FM_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MEDIA_FM_PORT", "8099")))
    ap.add_argument("--browse-root", default=None,
                    help="read-only tree to browse (default: $MEDIA_ROOT)")
    ap.add_argument("--media-root", default=None,
                    help="writable library root (default: $MEDIA_LIBRARY)")
    args = ap.parse_args(argv)
    BROWSE_ROOT = args.browse_root or C.media_root()
    MEDIA_ROOT = args.media_root or C.media_library()
    os.makedirs(MEDIA_ROOT, exist_ok=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"reflink arranger on http://{args.host}:{args.port}")
    print(f"  browse (ro): {BROWSE_ROOT}")
    print(f"  media  (rw): {MEDIA_ROOT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
