#!/usr/bin/env python3
"""
Track Tracker - gestione degli stati dei progetti Ableton.
Tutto in locale: nessun cloud, nessuna libreria da installare.

Uso: metti questo file dentro la cartella "Ableton Projects" e lancialo con
    python3 track_tracker.py
Si apre il browser su http://127.0.0.1:8765. Per chiudere: Ctrl+C nel Terminale.

I dati (stati, note, label...) sono salvati in "_tracker_data.json"
nella stessa cartella.
"""
import gzip
import json
import os
import plistlib
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8765
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else SCRIPT_DIR
DATA_FILE = ROOT / "_tracker_data.json"
IS_MAC = sys.platform == "darwin"
MAX_DEPTH = 6
SKIP_DIRS = {"Backup", "Samples", "Ableton Project Info", "Recorded", "Processed",
             "Imported", "Freeze", "Consolidated", "Presets"}

# chiave, etichetta, colore tag Finder (1 grigio, 2 verde, 3 viola, 4 blu, 5 giallo, 6 rosso, 7 arancio)
STATUSES = [
    ("idea", "Idea", 1),
    ("wip", "In lavorazione", 4),
    ("arrangiamento", "Arrangiamento", 3),
    ("mix", "Da mixare", 7),
    ("master", "Master", 5),
    ("bloccata", "Bloccata", 6),
    ("finita", "Finita", 2),
    ("rilasciata", "Rilasciata", 2),
]
STATUS_KEYS = {s[0] for s in STATUSES}
STATUS_LABELS = {s[1] for s in STATUSES}

# parole nel nome della cartella -> stato iniziale (se non l'hai ancora impostato tu).
# Niente \b: "_" conta come lettera per le regex, quindi "Track_DONE" non verrebbe riconosciuto.
_B, _E = r"(?<![A-Z0-9])", r"(?![A-Z0-9])"
NAME_RULES = [
    (rf"RELEASED|RILASCIAT|USCIT[AOE]{_E}", "rilasciata"),
    (r"BLOCK|BLOCCAT|STUCK", "bloccata"),
    (r"MASTER", "master"),
    (r"DA[ _\-]?MIX|MIXARE|TO[ _\-]?MIX|MIXDOWN", "mix"),
    (rf"FINIT[AO]|CONCLUS|{_B}DONE{_E}|FINISHED", "finita"),
    (r"ARRANG", "arrangiamento"),
    (rf"{_B}IDEA{_E}|SKETCH|BOZZA", "idea"),
]
STRIP_END = re.compile(
    r"(?i)[\s_\-–]+(project|da[ _]?mixare|to[ _]?mix|mixdown|blocked|bloccat[ao]|"
    r"master(ed)?|finit[ao]|done|finished|released|rilasciat[ao]|idea|sketch|bozza|wip)$")
RELEASE_DIR = re.compile(r"(?i)released|rilasciat[ea]|uscit[ea]")

LOCK = threading.Lock()


# ---------------------------------------------------------------- dati

def load_data():
    try:
        d = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d.setdefault("projects", {})
    d.setdefault("cache", {})
    d.setdefault("settings", {})
    d["settings"].setdefault("stale_days", 60)
    d["settings"].setdefault("finder_tags", IS_MAC)
    return d


def save_data(d):
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, DATA_FILE)


DATA = load_data()


# ---------------------------------------------------------------- scansione

def guess_status(name):
    up = name.upper()
    for pat, key in NAME_RULES:
        if re.search(pat, up):
            return key
    return "wip"


def clean_name(name):
    prev = None
    while prev != name:
        prev = name
        name = STRIP_END.sub("", name).strip()
    return name or prev


def read_bpm(als_path):
    try:
        with gzip.open(als_path, "rb") as f:
            data = f.read()
    except OSError:
        try:
            data = Path(als_path).read_bytes()
        except OSError:
            return None
    m = re.search(rb"<Tempo>.*?<Manual Value=\"([0-9.]+)\"", data, re.S)
    if not m:
        return None
    bpm = round(float(m.group(1)), 2)
    return int(bpm) if bpm == int(bpm) else bpm


def find_project_dirs():
    found = []

    def walk(d, depth):
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        als = [e for e in entries
               if e.is_file() and e.name.lower().endswith(".als") and not e.name.startswith("._")]
        if als and Path(d) != ROOT:
            found.append((Path(d), als))
            return
        if depth >= MAX_DEPTH:
            return
        for e in entries:
            if (e.is_dir(follow_symlinks=False) and not e.name.startswith(".")
                    and e.name not in SKIP_DIRS):
                walk(e.path, depth + 1)

    walk(ROOT, 0)
    return found


SCAN_TTL = 3.0  # secondi: entro questo tempo si riusa l'ultima lettura del disco
_FS = {"at": 0.0, "base": {}}


def scan_fs():
    """Legge le cartelle dal disco, al massimo una volta ogni SCAN_TTL secondi.
    Va chiamata con LOCK preso. Usa la cache per non rileggere i BPM a ogni volta."""
    if time.time() - _FS["at"] < SCAN_TTL:
        return _FS["base"]
    base = {}
    cache_changed = False
    for folder, als_entries in find_project_dirs():
        rel = folder.relative_to(ROOT).as_posix()
        parts = rel.split("/")
        try:
            stats = [(e, e.stat()) for e in als_entries]
        except OSError:  # file spostato o cancellato durante la scansione
            continue
        main, st = max(stats, key=lambda x: x[1].st_mtime)

        ckey = f"{rel}/{main.name}"
        c = DATA["cache"].get(ckey)
        if not c or c.get("mtime") != st.st_mtime or c.get("size") != st.st_size:
            c = {"mtime": st.st_mtime, "size": st.st_size, "bpm": read_bpm(main.path)}
            DATA["cache"][ckey] = c
            cache_changed = True

        base[rel] = {
            "id": rel,
            "name": clean_name(folder.name).replace("_", " "),
            "folder": folder.name,
            "alias": parts[0] if len(parts) > 1 else "Senza alias",
            "in_released": any(RELEASE_DIR.fullmatch(p) for p in parts[:-1]),
            "path": str(folder),
            "als": main.path,
            "als_name": main.name,
            "versions": len(als_entries),
            "bpm": c.get("bpm"),
            "mtime": max(s.st_mtime for _, s in stats),
        }
    if cache_changed:
        valid = {f"{b['id']}/{b['als_name']}" for b in base.values()}
        DATA["cache"] = {k: v for k, v in DATA["cache"].items() if k in valid}
        save_data(DATA)
    _FS.update(at=time.time(), base=base)
    return base


def scan(fresh=False):
    """Ritorna {id: progetto}: cartelle dal disco + stati/note salvati."""
    now = time.time()
    projects = {}
    with LOCK:
        if fresh:
            _FS["at"] = 0.0
        for rel, b in scan_fs().items():
            p = dict(b)
            in_released = p.pop("in_released")
            saved = DATA["projects"].get(rel, {})
            if saved.get("status") in STATUS_KEYS:
                status, source = saved["status"], "manual"
            else:
                status = "rilasciata" if in_released else guess_status(p["folder"])
                source = "auto"
            p.update(
                days=int((now - p["mtime"]) // 86400),
                status=status,
                status_source=source,
                notes=saved.get("notes", ""),
                label=saved.get("label", ""),
                release_date=saved.get("release_date", ""),
            )
            projects[rel] = p
    return projects


# ---------------------------------------------------------------- tag Finder

TAG_ATTR = "com.apple.metadata:_kMDItemUserTags"


def get_tags(path):
    r = subprocess.run(["xattr", "-px", TAG_ATTR, path], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    try:
        return list(plistlib.loads(bytes.fromhex("".join(r.stdout.split()))))
    except Exception:
        return []


def apply_status_tag(path, status):
    if not IS_MAC:
        return
    label, color = next((s[1], s[2]) for s in STATUSES if s[0] == status)
    tags = [t for t in get_tags(path) if t.split("\n")[0] not in STATUS_LABELS]
    tags.append(f"{label}\n{color}")
    raw = plistlib.dumps(tags, fmt=plistlib.FMT_BINARY)
    subprocess.run(["xattr", "-wx", TAG_ATTR, raw.hex(), path], check=True,
                   capture_output=True)


# ---------------------------------------------------------------- API

def payload(fresh=False):
    projects = scan(fresh)
    return {
        "root": str(ROOT),
        "is_mac": IS_MAC,
        "statuses": [{"key": k, "label": l} for k, l, _ in STATUSES],
        "settings": DATA["settings"],
        "projects": sorted(projects.values(), key=lambda p: -p["mtime"]),
    }


def update_project(body):
    pid = body.get("id")
    projects = scan()
    if pid not in projects:
        raise ValueError("Progetto non trovato")
    warning = None
    with LOCK:
        entry = DATA["projects"].setdefault(pid, {})
        for field in ("notes", "label", "release_date"):
            if field in body:
                entry[field] = str(body[field])[:20000]
        if "status" in body:
            if body["status"] not in STATUS_KEYS:
                raise ValueError("Stato non valido")
            entry["status"] = body["status"]
        save_data(DATA)
    if "status" in body and DATA["settings"].get("finder_tags"):
        try:
            apply_status_tag(projects[pid]["path"], body["status"])
        except Exception as e:
            warning = f"Tag Finder non applicato: {e}"
    p = scan()[pid]
    return {"project": p, "warning": warning}


def open_project(body):
    p = scan().get(body.get("id"))
    if not p:
        raise ValueError("Progetto non trovato")
    if not IS_MAC:
        raise ValueError("Funziona solo su Mac")
    if body.get("action") == "finder":
        subprocess.run(["open", "-R", p["path"]], check=True)
    else:
        subprocess.run(["open", p["als"]], check=True)
    return {"ok": True}


def update_settings(body):
    with LOCK:
        s = DATA["settings"]
        if "stale_days" in body:
            s["stale_days"] = max(1, min(3650, int(body["stale_days"])))
        if "finder_tags" in body:
            s["finder_tags"] = bool(body["finder_tags"]) and IS_MAC
        save_data(DATA)
    return {"settings": DATA["settings"]}


def sync_tags(_body):
    if not IS_MAC:
        raise ValueError("Funziona solo su Mac")
    ok, errors = 0, []
    for p in scan().values():
        try:
            apply_status_tag(p["path"], p["status"])
            ok += 1
        except Exception as e:
            errors.append(f"{p['name']}: {e}")
    return {"ok": ok, "errors": errors}


ROUTES = {
    "/api/update": update_project,
    "/api/open": open_project,
    "/api/settings": update_settings,
    "/api/sync_tags": sync_tags,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _local_host(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._local_host():
            return self._send(403, {"error": "forbidden"})
        if self.path in ("/", "/index.html"):
            return self._send(200, HTML, "text/html; charset=utf-8")
        if self.path in ("/api/projects", "/api/projects?fresh=1"):
            return self._send(200, payload(fresh=self.path.endswith("fresh=1")))
        self._send(404, {"error": "not found"})

    def _same_origin(self):
        """Blocca le POST mandate da altri siti aperti nel browser (CSRF).
        Il browser mette sempre Origin sulle POST cross-site; il Content-Type JSON
        obbliga in più a un preflight CORS, che questo server non autorizza."""
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return False
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return ctype == "application/json"

    def do_POST(self):
        if not self._local_host() or not self._same_origin():
            return self._send(403, {"error": "forbidden"})
        fn = ROUTES.get(self.path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, fn(body))
        except Exception as e:
            self._send(400, {"error": str(e)})


def make_launcher():
    """Crea 'Avvia Track Tracker.command' accanto allo script: poi basta il doppio clic."""
    if not IS_MAC:
        return
    launcher = SCRIPT_DIR / "Avvia Track Tracker.command"
    if launcher.exists():
        return
    try:
        launcher.write_text(
            '#!/bin/bash\ncd "$(dirname "$0")"\n'
            f'exec /usr/bin/env python3 "{Path(__file__).name}"\n', encoding="utf-8")
        launcher.chmod(0o755)
        print(f"Creato '{launcher.name}': la prossima volta fai doppio clic su quello.")
    except OSError:
        pass


def main():
    url = f"http://127.0.0.1:{PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print("Track Tracker è già aperto, apro il browser.")
        webbrowser.open(url)
        return
    make_launcher()
    print(f"Track Tracker attivo su {url}")
    print(f"Cartella: {ROOT}")
    print("Per chiudere premi Ctrl+C.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nChiuso.")


# ---------------------------------------------------------------- interfaccia

HTML = r"""<!DOCTYPE html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Track Tracker</title>
<style>
:root{--bg:#121214;--panel:#1c1c1f;--panel2:#26262a;--line:#333338;--text:#ececf0;--mute:#8d8d96;
--idea:#8e8e93;--wip:#0a84ff;--arrangiamento:#bf5af2;--mix:#ff9f0a;--master:#ffd60a;--bloccata:#ff453a;--finita:#30d158;--rilasciata:#64d2ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text",Helvetica,Arial,sans-serif}
header{position:sticky;top:0;z-index:5;background:rgba(18,18,20,.92);backdrop-filter:blur(10px);border-bottom:1px solid var(--line);padding:14px 20px}
.top{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:18px;margin:0 8px 0 0;letter-spacing:.3px}
h1 span{color:var(--mute);font-weight:400;font-size:13px;margin-left:6px}
input,select,textarea,button{font:inherit;color:inherit}
.search{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:7px 10px;width:220px}
.btn{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:7px 12px;cursor:pointer}
.btn:hover{border-color:#55555c}
.btn.on{background:#e9e9ee;color:#111;border-color:#e9e9ee}
.btn.primary{background:var(--wip);border-color:var(--wip);color:#fff}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg .btn{border:0;border-radius:0}
.spacer{flex:1}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}
.chip{border:1px solid var(--line);border-radius:999px;padding:4px 11px;cursor:pointer;color:var(--mute);background:transparent}
.chip.on{color:var(--text);border-color:#77777f;background:var(--panel2)}
.chip b{font-weight:600;margin-left:4px;color:var(--text)}
main{padding:18px 20px 40px}
.board{display:flex;gap:12px;overflow-x:auto;align-items:flex-start;padding-bottom:10px}
.col{flex:1 1 0;min-width:165px;background:var(--panel);border-radius:12px;border-top:3px solid var(--c);min-height:120px}
.col.over{outline:2px dashed var(--c);outline-offset:-4px}
.colh{display:flex;justify-content:space-between;padding:10px 12px;font-weight:600}
.colh span{color:var(--mute);font-weight:400}
.cards{padding:0 8px 8px;display:flex;flex-direction:column;gap:8px;min-height:60px}
.card{background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--c);border-radius:9px;padding:9px 10px;cursor:pointer}
.card:hover{border-color:#55555c;border-left-color:var(--c)}
.card.dragging{opacity:.4}
.cname{font-weight:600;word-break:break-word}
.meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px;color:var(--mute);font-size:12px;align-items:center}
.tag{background:#0003;border:1px solid var(--line);border-radius:5px;padding:0 6px}
.stale{color:#ffb340;border-color:#ffb34055}
.note{margin-top:6px;color:#b9b9c2;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.auto{color:var(--mute);font-size:11px;font-style:italic}
table{width:100%;border-collapse:collapse;background:var(--panel);border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line)}
th{color:var(--mute);font-weight:500;cursor:pointer;user-select:none;white-space:nowrap}
tr.row{cursor:pointer}tr.row:hover td{background:var(--panel2)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--c);margin-right:7px}
.empty{color:var(--mute);text-align:center;padding:60px 0}
.overlay{position:fixed;inset:0;background:#000a;display:none;align-items:flex-start;justify-content:center;z-index:20;overflow:auto;padding:50px 16px}
.overlay.show{display:flex}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:14px;width:min(620px,100%);padding:22px}
.modal h2{margin:0 0 4px;font-size:19px;word-break:break-word}
.path{color:var(--mute);font-size:12px;word-break:break-all}
.lbl{color:var(--mute);font-size:12px;margin:16px 0 6px}
.stbtns{display:flex;flex-wrap:wrap;gap:6px}
.stbtn{border:1px solid var(--line);background:transparent;border-radius:8px;padding:6px 10px;cursor:pointer}
.stbtn.on{background:var(--c);border-color:var(--c);color:#111;font-weight:600}
textarea{width:100%;min-height:150px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px;resize:vertical}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.field{width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.actions{display:flex;gap:8px;margin-top:18px;flex-wrap:wrap;align-items:center}
.saved{color:var(--finita);font-size:12px;opacity:0;transition:opacity .3s}
.saved.show{opacity:1}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#333;padding:10px 16px;border-radius:10px;display:none;z-index:30;max-width:90%}
.facts{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.fact{background:var(--panel2);border-radius:8px;padding:6px 10px;font-size:12px}
.fact b{display:block;font-size:15px}
</style></head><body>
<header>
  <div class="top">
    <h1>Track Tracker<span id="count"></span></h1>
    <input class="search" id="q" placeholder="Cerca traccia o note…">
    <div class="seg"><button class="btn" data-view="board">Board</button><button class="btn" data-view="list">Lista</button></div>
    <button class="chip" id="staleBtn">⏳ Solo ferme</button>
    <div class="spacer"></div>
    <button class="btn" id="reload">⟳ Aggiorna</button>
    <button class="btn" id="settingsBtn">⚙︎</button>
  </div>
  <div class="chips" id="aliases"></div>
</header>
<main id="main"></main>

<div class="overlay" id="ov"><div class="modal" id="modal"></div></div>
<div class="overlay" id="ovs"><div class="modal" id="smodal"></div></div>
<div class="toast" id="toast"></div>

<script>
const S={projects:[],statuses:[],settings:{},alias:'__all',q:'',staleOnly:false,
  view:store('view')||'board',sort:{k:'mtime',dir:-1},openId:null};
function store(k,v){try{if(v===undefined)return localStorage.getItem('tt_'+k);localStorage.setItem('tt_'+k,v)}catch(e){return null}}
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const label=k=>(S.statuses.find(s=>s.key===k)||{}).label||k;
const DONE=['finita','rilasciata'];
const isStale=p=>!DONE.includes(p.status)&&p.days>=S.settings.stale_days;

async function api(path,body){
  const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
  const d=await r.json().catch(()=>({error:'Risposta non valida'}));
  if(!r.ok)throw new Error(d.error||r.status);
  return d;
}
function toast(msg){const t=$('#toast');t.textContent=msg;t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',3500)}

async function load(fresh){
  try{const d=await api('/api/projects'+(fresh?'?fresh=1':''));Object.assign(S,{projects:d.projects,statuses:d.statuses,settings:d.settings,root:d.root,isMac:d.is_mac});render()}
  catch(e){$('#main').innerHTML='<div class="empty">Errore: '+esc(e.message)+'</div>'}
}
function ago(d){if(d<=0)return'oggi';if(d===1)return'ieri';if(d<45)return d+' g fa';if(d<365)return Math.round(d/30)+' mesi fa';const y=Math.floor(d/365);return y+(y===1?' anno fa':' anni fa')}

function filtered(){
  const q=S.q.toLowerCase();
  return S.projects.filter(p=>(S.alias==='__all'||p.alias===S.alias)&&(!S.staleOnly||isStale(p))&&
    (!q||(p.name+' '+p.folder+' '+p.notes+' '+p.label).toLowerCase().includes(q)));
}

function render(){
  document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('on',b.dataset.view===S.view));
  $('#staleBtn').classList.toggle('on',S.staleOnly);
  const staleN=S.projects.filter(isStale).length;
  $('#staleBtn').innerHTML='⏳ Solo ferme <b>'+staleN+'</b>';
  const aliases=[...new Set(S.projects.map(p=>p.alias))].sort();
  $('#aliases').innerHTML=[['__all','Tutti',S.projects.length],...aliases.map(a=>[a,a,S.projects.filter(p=>p.alias===a).length])]
    .map(([k,l,n])=>`<button class="chip ${S.alias===k?'on':''}" data-alias="${esc(k)}">${esc(l)}<b>${n}</b></button>`).join('');
  const list=filtered();
  $('#count').textContent=list.length+' tracce';
  if(!S.projects.length){$('#main').innerHTML='<div class="empty">Nessun progetto Ableton trovato in<br>'+esc(S.root)+'</div>';return}
  S.view==='board'?renderBoard(list):renderList(list);
}

function card(p){
  return `<div class="card" draggable="true" data-id="${esc(p.id)}" style="--c:var(--${p.status})">
    <div class="cname">${esc(p.name)}</div>
    <div class="meta">${S.alias==='__all'?`<span class="tag">${esc(p.alias)}</span>`:''}
      ${p.bpm?`<span>${p.bpm} BPM</span>`:''}<span>· ${ago(p.days)}</span>
      ${isStale(p)?`<span class="tag stale">⏳ ferma da ${p.days} g</span>`:''}
      ${p.status_source==='auto'?'<span class="auto" title="Stato letto dal nome della cartella">auto</span>':''}</div>
    ${p.notes?`<div class="note">📝 ${esc(p.notes.split('\n')[0])}</div>`:''}</div>`;
}
function renderBoard(list){
  $('#main').innerHTML='<div class="board">'+S.statuses.map(s=>{
    const items=list.filter(p=>p.status===s.key);
    return `<div class="col" data-status="${s.key}" style="--c:var(--${s.key})">
      <div class="colh">${esc(s.label)}<span>${items.length}</span></div>
      <div class="cards">${items.map(card).join('')}</div></div>`}).join('')+'</div>';
}
function renderList(list){
  const k=S.sort.k,dir=S.sort.dir,order=S.statuses.map(s=>s.key);
  const val=p=>k==='status'?order.indexOf(p.status):k==='bpm'?(p.bpm||0):k==='mtime'?p.mtime:String(p[k]).toLowerCase();
  list=[...list].sort((a,b)=>(val(a)>val(b)?1:val(a)<val(b)?-1:0)*dir);
  const th=(key,l)=>`<th data-sort="${key}">${l}${S.sort.k===key?(S.sort.dir>0?' ▲':' ▼'):''}</th>`;
  $('#main').innerHTML=list.length?`<table><tr>${th('name','Traccia')}${th('alias','Alias')}${th('status','Stato')}${th('bpm','BPM')}${th('mtime','Modificata')}<th>Note</th></tr>`+
    list.map(p=>`<tr class="row" data-id="${esc(p.id)}" style="--c:var(--${p.status})"><td><b>${esc(p.name)}</b></td><td>${esc(p.alias)}</td>
    <td><span class="dot"></span>${esc(label(p.status))}</td><td>${p.bpm||'–'}</td>
    <td>${ago(p.days)}${isStale(p)?' <span class="tag stale">⏳</span>':''}</td>
    <td style="color:var(--mute);max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.notes.split('\n')[0])}</td></tr>`).join('')+'</table>'
    :'<div class="empty">Nessuna traccia con questi filtri</div>';
}

async function update(id,fields){
  try{
    const d=await api('/api/update',{id,...fields});
    const i=S.projects.findIndex(p=>p.id===id);if(i>=0)S.projects[i]=d.project;
    if(d.warning)toast(d.warning);
    // le note si salvano mentre scrivi: non ridisegnare il modale o la textarea perde il focus
    render();if(S.openId===id&&!('notes' in fields))renderModal(true);
    const sv=$('#saved');if(sv){sv.classList.add('show');setTimeout(()=>sv.classList.remove('show'),1200)}
  }catch(e){toast('Errore: '+e.message)}
}

function openModal(id){S.openId=id;renderModal();$('#ov').classList.add('show')}
function closeModal(){S.openId=null;$('#ov').classList.remove('show')}
function renderModal(keepText){
  const p=S.projects.find(x=>x.id===S.openId);if(!p)return closeModal();
  const m=$('#modal');
  const notes=keepText&&$('#notes')?$('#notes').value:p.notes;
  m.innerHTML=`<h2>${esc(p.name)}</h2><div class="path">${esc(p.path)}</div>
    <div class="facts"><div class="fact">Alias<b>${esc(p.alias)}</b></div><div class="fact">BPM<b>${p.bpm||'–'}</b></div>
    <div class="fact">Ultima modifica<b>${ago(p.days)}</b></div><div class="fact">Versioni .als<b>${p.versions}</b></div></div>
    ${isStale(p)?`<div class="facts"><span class="tag stale">⏳ Non la tocchi da ${p.days} giorni</span></div>`:''}
    <div class="lbl">Stato ${p.status_source==='auto'?'<span class="auto">(letto dal nome cartella, clicca per confermare)</span>':''}</div>
    <div class="stbtns">${S.statuses.map(s=>`<button class="stbtn ${p.status===s.key?'on':''}" data-st="${s.key}" style="--c:var(--${s.key})">${esc(s.label)}</button>`).join('')}</div>
    <div class="lbl">Note / cose da fare</div>
    <textarea id="notes" placeholder="es. rifare il drop, kick troppo forte, mandare a …">${esc(notes)}</textarea>
    <div class="row2"><div><div class="lbl">Label</div><input class="field" id="label" value="${esc(p.label)}" placeholder="es. Toolroom"></div>
    <div><div class="lbl">Data di uscita</div><input class="field" type="date" id="rdate" value="${esc(p.release_date)}"></div></div>
    <div class="actions"><button class="btn primary" data-open="ableton">▶ Apri in Ableton</button>
    <button class="btn" data-open="finder">Mostra nel Finder</button><span class="saved" id="saved">Salvato ✓</span>
    <div class="spacer"></div><button class="btn" id="closeM">Chiudi</button></div>`;
  let t;
  $('#notes').addEventListener('input',e=>{clearTimeout(t);t=setTimeout(()=>update(p.id,{notes:e.target.value}),700)});
  $('#label').addEventListener('change',e=>update(p.id,{label:e.target.value}));
  $('#rdate').addEventListener('change',e=>update(p.id,{release_date:e.target.value}));
}

function openSettings(){
  const s=S.settings;
  $('#smodal').innerHTML=`<h2>Impostazioni</h2><div class="path">Cartella: ${esc(S.root)}</div>
    <div class="lbl">Considera "ferma" una traccia non toccata da (giorni)</div>
    <input class="field" type="number" min="1" id="sdays" value="${s.stale_days}" style="width:120px">
    <div class="lbl">Tag colorati nel Finder</div>
    <label><input type="checkbox" id="stags" ${s.finder_tags?'checked':''} ${S.isMac?'':'disabled'}> Quando cambio stato, colora anche la cartella nel Finder</label>
    <div class="actions"><button class="btn" id="syncTags" ${S.isMac?'':'disabled'}>Applica i tag a tutte le tracce ora</button>
    <div class="spacer"></div><button class="btn" id="closeS">Chiudi</button></div>`;
  $('#sdays').addEventListener('change',async e=>{const d=await api('/api/settings',{stale_days:+e.target.value||60});S.settings=d.settings;render()});
  $('#stags').addEventListener('change',async e=>{const d=await api('/api/settings',{finder_tags:e.target.checked});S.settings=d.settings});
  $('#syncTags').addEventListener('click',async()=>{try{const d=await api('/api/sync_tags',{});toast(`Tag applicati a ${d.ok} tracce`+(d.errors.length?` (${d.errors.length} errori)`:''))}catch(e){toast('Errore: '+e.message)}});
  $('#ovs').classList.add('show');
}

document.addEventListener('click',async e=>{
  const t=e.target;
  if(t.closest('[data-view]')){S.view=t.closest('[data-view]').dataset.view;store('view',S.view);render();return}
  if(t.closest('[data-alias]')){S.alias=t.closest('[data-alias]').dataset.alias;render();return}
  if(t.closest('#staleBtn')){S.staleOnly=!S.staleOnly;render();return}
  if(t.closest('#reload')){load(true);toast('Cartella riletta');return}
  if(t.closest('#settingsBtn')){openSettings();return}
  if(t.closest('[data-sort]')){const k=t.closest('[data-sort]').dataset.sort;S.sort=S.sort.k===k?{k,dir:-S.sort.dir}:{k,dir:k==='mtime'||k==='bpm'?-1:1};render();return}
  if(t.closest('.card,.row')){openModal(t.closest('.card,.row').dataset.id);return}
  if(t.closest('[data-st]')){update(S.openId,{status:t.closest('[data-st]').dataset.st});return}
  if(t.closest('[data-open]')){try{await api('/api/open',{id:S.openId,action:t.closest('[data-open]').dataset.open})}catch(err){toast(err.message)}return}
  if(t.closest('#closeM')||t.id==='ov'){closeModal();return}
  if(t.closest('#closeS')||t.id==='ovs'){$('#ovs').classList.remove('show');return}
});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();$('#ovs').classList.remove('show')}});
$('#q').addEventListener('input',e=>{S.q=e.target.value;render()});

let dragId=null;
document.addEventListener('dragstart',e=>{const c=e.target.closest&&e.target.closest('.card');if(!c)return;dragId=c.dataset.id;c.classList.add('dragging');e.dataTransfer.effectAllowed='move'});
document.addEventListener('dragend',e=>{document.querySelectorAll('.dragging,.over').forEach(x=>x.classList.remove('dragging','over'))});
document.addEventListener('dragover',e=>{const col=e.target.closest&&e.target.closest('.col');if(!col||!dragId)return;e.preventDefault();
  document.querySelectorAll('.col.over').forEach(x=>x!==col&&x.classList.remove('over'));col.classList.add('over')});
document.addEventListener('drop',e=>{const col=e.target.closest&&e.target.closest('.col');if(!col||!dragId)return;e.preventDefault();
  const p=S.projects.find(x=>x.id===dragId);if(p&&p.status!==col.dataset.status)update(dragId,{status:col.dataset.status});dragId=null});
window.addEventListener('focus',()=>{if(!S.openId)load()});
load();
</script></body></html>"""

if __name__ == "__main__":
    main()
