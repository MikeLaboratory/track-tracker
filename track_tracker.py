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
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8765
SCRIPT_DIR = Path(__file__).resolve().parent
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
# --app: lanciato da TrackTracker.app (porta scelta dal sistema, niente browser né launcher)
APP_MODE = "--app" in sys.argv[1:]
ROOT = Path(ARGS[0]).expanduser().resolve() if ARGS else SCRIPT_DIR
DATA_FILE = ROOT / "_tracker_data.json"
IS_MAC = sys.platform == "darwin"
MAX_DEPTH = 6
SKIP_DIRS = {"Backup", "Samples", "Ableton Project Info", "Recorded", "Processed",
             "Imported", "Freeze", "Consolidated", "Presets"}

# chiave, etichetta, colore tag Finder (0 nessuno, 1 grigio, 2 verde, 3 viola, 4 blu, 5 giallo,
# 6 rosso, 7 arancio). Il Finder ha solo 7 colori per 8 stati: le idee, che sono le più numerose,
# hanno il tag senza colore, così il grigio va a "Rilasciata" e non si confonde con "Finita".
STATUSES = [
    ("idea", "Idea", 0),
    ("wip", "In lavorazione", 4),
    ("arrangiamento", "Arrangiamento", 3),
    ("mix", "Da mixare", 7),
    ("master", "Master", 5),
    ("bloccata", "Bloccata", 6),
    ("finita", "Finita", 2),
    ("rilasciata", "Rilasciata", 1),
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
    r"master(ed)?|finit[ao]|done|finished|released|rilasciat[ao]|idea|sketch|bozza|wip|"
    r"arrang(iamento)?|stuck)$")
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
            fst = folder.stat()
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
            # identità della cartella su disco: resta uguale se la rinomini o la sposti
            # sullo stesso disco, quindi permette di ritrovare stato e note
            "ino": f"{fst.st_dev}:{fst.st_ino}",
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


def _unique_by(pairs):
    """{chiave: id} tenendo solo le chiavi che compaiono una volta sola."""
    seen = {}
    for rid, key in pairs:
        seen.setdefault(key, []).append(rid)
    return {k: ids[0] for k, ids in seen.items() if len(ids) == 1}


def reconcile(base):
    """Riaggancia stato e note dei progetti rinominati o spostati. Va chiamata con LOCK preso.

    Un dato salvato senza più cartella ("orfano") viene spostato su una cartella nuova
    se hanno la stessa identità su disco, oppure, se la cartella è stata copiata su un
    altro disco, lo stesso file .als principale, ma solo quando l'abbinamento è univoco.
    Gli orfani senza abbinamento restano nel JSON (il disco potrebbe essere scollegato)."""
    saved = DATA["projects"]
    changed = False
    orphans = {rid: e for rid, e in saved.items() if rid not in base}
    new = {rid: b for rid, b in base.items() if rid not in saved}
    if orphans and new:
        new_by_ino = _unique_by((rid, b["ino"]) for rid, b in new.items())
        for oid, e in list(orphans.items()):
            nid = new_by_ino.get(e.get("ino"))
            if nid in new:
                saved[nid] = saved.pop(oid)
                del new[nid], orphans[oid]
                changed = True
        old_by_als = _unique_by((oid, e["als_name"]) for oid, e in orphans.items()
                                if e.get("als_name"))
        new_by_als = _unique_by((rid, b["als_name"]) for rid, b in new.items())
        for name, oid in old_by_als.items():
            if name in new_by_als:
                saved[new_by_als[name]] = saved.pop(oid)
                changed = True
    # tieni aggiornata l'impronta dei progetti che conosciamo
    for rid, e in saved.items():
        b = base.get(rid)
        if b and (e.get("ino") != b["ino"] or e.get("als_name") != b["als_name"]):
            e["ino"], e["als_name"] = b["ino"], b["als_name"]
            changed = True
    if changed:
        save_data(DATA)


def scan(fresh=False):
    """Ritorna {id: progetto}: cartelle dal disco + stati/note salvati."""
    now = time.time()
    projects = {}
    with LOCK:
        if fresh:
            _FS["at"] = 0.0
        base = scan_fs()
        reconcile(base)
        for rel, b in base.items():
            p = dict(b)
            in_released = p.pop("in_released")
            p.pop("ino")
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
            status = body["status"] or ""  # "" = torna allo stato dedotto dal nome
            if status and status not in STATUS_KEYS:
                raise ValueError("Stato non valido")
            if status:
                entry["status"] = status
            else:
                entry.pop("status", None)
        save_data(DATA)
    p = scan()[pid]
    if "status" in body and DATA["settings"].get("finder_tags"):
        try:
            apply_status_tag(p["path"], p["status"])
        except Exception as e:
            warning = f"Tag Finder non applicato: {e}"
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
        if self.path == "/api/ping":
            return self._send(200, {"app": "track-tracker", "root": str(ROOT)})
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
    """Crea 'Avvia Track Tracker.command' nella cartella dei progetti: poi basta il doppio clic.
    Il launcher apre la cartella in cui si trova, quindi funziona anche se hai lanciato
    lo script da un'altra parte passando la cartella come argomento."""
    if not IS_MAC:
        return
    launcher = ROOT / "Avvia Track Tracker.command"
    if launcher.exists():
        return
    # script nella stessa cartella: percorso relativo, così puoi spostare tutta la cartella
    script = Path(__file__).name if ROOT == SCRIPT_DIR else str(Path(__file__).resolve())
    try:
        launcher.write_text(
            '#!/bin/bash\ncd "$(dirname "$0")"\n'
            f'exec /usr/bin/env python3 "{script}" "$PWD"\n', encoding="utf-8")
        launcher.chmod(0o755)
        print(f"Creato '{launcher.name}': la prossima volta fai doppio clic su quello.")
    except OSError:
        pass


def running_root(port):
    """Se su questa porta gira già Track Tracker, ritorna la sua cartella; altrimenti None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            d = json.loads(r.read())
        return d.get("root") if d.get("app") == "track-tracker" else None
    except Exception:
        return None


def main_app():
    """Modalità app: l'app legge l'indirizzo da stdout e chiude stdin quando esce."""
    global PORT
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    PORT = server.server_address[1]
    print(f"TRACKTRACKER_URL http://127.0.0.1:{PORT}", flush=True)

    def exit_when_app_quits():
        sys.stdin.read()  # ritorna solo quando l'app chiude la pipe (anche se va in crash)
        os._exit(0)

    threading.Thread(target=exit_when_app_quits, daemon=True).start()
    server.serve_forever()


def main():
    if APP_MODE:
        return main_app()
    global PORT
    server = None
    for port in range(PORT, PORT + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            # porta occupata: se è Track Tracker sulla stessa cartella basta aprire il browser,
            # se è un altro programma (o un'altra cartella) proviamo la porta dopo
            if running_root(port) == str(ROOT):
                print("Track Tracker è già aperto, apro il browser.")
                webbrowser.open(f"http://127.0.0.1:{port}")
                return
    if server is None:
        print(f"Nessuna porta libera tra {PORT} e {PORT + 9}: chiudi qualche programma e riprova.")
        sys.exit(1)
    PORT = port
    url = f"http://127.0.0.1:{PORT}"
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
:root{--bg:#141416;--side:#111113;--raise:#1c1c20;--card:#202024;--card2:#26262b;--line:#2a2a30;--line2:#3a3a42;
--text:#ececf0;--soft:#cfcfd6;--mute:#9a9aa3;--dim:#64646d;--accent:#0a84ff;--warn:#ffb340;
--idea:#8e8e93;--wip:#0a84ff;--arrangiamento:#bf5af2;--mix:#ff9f0a;--master:#ffd60a;--bloccata:#ff453a;--finita:#30d158;--rilasciata:#64d2ff;
--shadow:0 12px 32px #0009,0 2px 8px #0006;color-scheme:dark}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:13px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased;overflow:hidden}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer;background:none;border:0;padding:0;text-align:inherit}
svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round;flex:none}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-thumb{background:#ffffff1c;border-radius:10px;border:3px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background-color:#ffffff30}
::-webkit-scrollbar-track,::-webkit-scrollbar-corner{background:transparent}
.shell{display:grid;grid-template-columns:236px minmax(0,1fr);height:100vh}

/* barra laterale */
.side{background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.brand{display:flex;align-items:center;gap:10px;padding:16px 16px 14px;font-weight:650;font-size:15px;letter-spacing:.2px}
.logo{width:28px;height:28px;border-radius:8px;background:linear-gradient(180deg,#2c2c34,#111114);display:grid;place-items:center;box-shadow:inset 0 0 0 1px #ffffff17,0 2px 6px #0007}
.logo svg{width:20px;height:20px;stroke-width:2.3}
.stackbar{display:flex;gap:2px;height:6px;margin:0 16px 6px;border-radius:3px;overflow:hidden;background:#ffffff0c}
.stackbar span{background:var(--c);min-width:3px}
.stackinfo{margin:0 16px 4px;color:var(--dim);font-size:11.5px}
.navs{flex:1;overflow:auto;padding:0 8px 12px}
.sec{margin:16px 9px 5px;color:var(--dim);font-size:10.5px;font-weight:650;letter-spacing:.8px;text-transform:uppercase}
.nav{display:flex;align-items:center;gap:9px;width:100%;padding:6px 9px;border-radius:7px;color:var(--soft)}
.nav:hover{background:#ffffff0a}
.nav.on{background:#ffffff18;color:#fff}
.nav .nt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nav .nc{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums}
.nav.on .nc{color:var(--mute)}
.nav svg{color:var(--mute)}
.nav.stale svg{color:var(--warn)}
.nav .dot{margin:0 4px}
.side-foot{border-top:1px solid var(--line);padding:9px 8px 9px 14px;display:flex;align-items:center;gap:8px;color:var(--mute);font-size:12px}
.side-foot .fn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* contenuto */
.content{display:flex;flex-direction:column;min-width:0;min-height:0;transition:margin-right .22s cubic-bezier(.2,.8,.2,1)}
.toolbar{display:flex;align-items:center;gap:10px;padding:12px 18px;border-bottom:1px solid var(--line)}
.ttl{display:flex;align-items:baseline;gap:9px;min-width:0;margin-right:auto}
.ttl h1{font-size:17px;margin:0;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ttl span{color:var(--mute);font-size:12px;white-space:nowrap}
.search{position:relative;display:flex;align-items:center}
.search svg{position:absolute;left:9px;color:var(--dim);pointer-events:none}
.search input{width:250px;background:var(--raise);border:1px solid var(--line);border-radius:8px;padding:6px 44px 6px 30px;outline:none;transition:border-color .15s,box-shadow .15s}
.search input::placeholder{color:var(--dim)}
.search kbd{position:absolute;right:8px;color:var(--dim);font:11px -apple-system,sans-serif;border:1px solid var(--line2);border-radius:4px;padding:0 4px;pointer-events:none}
.search input:focus,.field:focus,textarea:focus,.dd:focus{border-color:var(--accent);box-shadow:0 0 0 3px #0a84ff30}
.dd{background:var(--raise);border:1px solid var(--line);border-radius:8px;padding:6px 8px;outline:none}
.seg{display:inline-flex;background:var(--raise);border:1px solid var(--line);border-radius:8px;padding:2px}
.seg button{display:flex;align-items:center;gap:6px;padding:4px 10px;border-radius:6px;color:var(--mute)}
.seg button:hover{color:var(--text)}
.seg button.on{background:#ffffff1d;color:#fff}
.ib{display:inline-grid;place-items:center;width:28px;height:28px;border-radius:7px;color:var(--mute);flex:none}
.ib:hover{background:#ffffff14;color:var(--text)}
.ib.sm{width:24px;height:24px;border-radius:6px}
.ib.sm svg{width:14px;height:14px}
main{flex:1;min-height:0;overflow:auto;padding:16px 18px 18px}
main.board{overflow-y:hidden}
main.list{padding-top:0}

/* board */
.board-w{display:flex;gap:12px;height:100%;align-items:stretch}
.col{flex:1 0 256px;max-width:360px;display:flex;flex-direction:column;background:#ffffff05;border:1px solid var(--line);border-radius:12px;min-height:0;box-shadow:inset 0 2px 0 var(--c);transition:background .15s,border-color .15s}
.col.over{background:#ffffff0e;border-color:var(--c)}
.colh{display:flex;align-items:center;gap:8px;padding:11px 8px 8px 13px;font-weight:600}
.colh .ct{flex:1}
.n{color:var(--dim);font-weight:500;font-variant-numeric:tabular-nums}
.dot{width:8px;height:8px;border-radius:50%;background:var(--c);flex:none}
.cards{flex:1;overflow-y:auto;padding:2px 8px 10px;display:flex;flex-direction:column;gap:8px}
.col-empty{color:var(--dim);font-size:12px;text-align:center;border:1px dashed var(--line2);border-radius:9px;padding:20px 8px}
.col.collapsed{flex:0 0 46px;cursor:pointer}
.col.collapsed:hover{background:#ffffff0c}
.col.collapsed .colh{flex-direction:column;padding:14px 0;gap:12px}
.vlabel{writing-mode:vertical-rl;color:var(--mute);font-weight:600;white-space:nowrap}

/* card */
.card{position:relative;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 10px 10px 14px;cursor:pointer;box-shadow:0 1px 2px #0005;transition:border-color .12s,background .12s}
.card::before{content:"";position:absolute;left:0;top:10px;bottom:10px;width:3px;border-radius:0 3px 3px 0;background:var(--c)}
.card:hover{background:var(--card2);border-color:var(--line2)}
.card.is-sel{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.card.dragging{opacity:.35}
.c-top{display:flex;align-items:flex-start;gap:4px}
.cname{flex:1;font-weight:600;font-size:13.5px;line-height:1.3;word-break:break-word;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.c-act{display:flex;gap:1px;margin:-4px -5px -4px 0;opacity:0;transition:opacity .12s}
.card:hover .c-act,.card.is-sel .c-act{opacity:1}
.meta{display:flex;flex-wrap:wrap;align-items:center;gap:4px 9px;margin-top:6px;color:var(--mute);font-size:11.5px}
.pill{display:inline-flex;align-items:center;gap:4px;background:#ffffff0d;border:1px solid #ffffff10;border-radius:5px;padding:0 6px;font-size:11px;color:var(--soft);white-space:nowrap}
.pill svg{width:11px;height:11px}
.bpm{font-variant-numeric:tabular-nums;color:var(--soft)}
.stale-b{color:var(--warn);background:#ffb34017;border-color:#ffb34030}
.auto{color:var(--dim);font-style:italic}
.crel{margin-top:6px;font-size:11.5px;color:var(--rilasciata)}
.cnote{margin-top:7px;color:#b3b3bb;font-size:12px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word;white-space:pre-line}

/* lista */
.tbl{width:100%;border-collapse:separate;border-spacing:0;margin-top:16px}
.tbl th{position:sticky;top:0;z-index:1;background:var(--bg);text-align:left;font-weight:500;color:var(--mute);font-size:12px;padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap;user-select:none}
.tbl th:hover{color:var(--text)}
.tbl td{padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
.tbl tr.row{cursor:pointer}
.tbl tr.row:hover td{background:#ffffff07}
.tbl tr.row.is-sel td{background:#0a84ff1e}
.num{text-align:right;font-variant-numeric:tabular-nums}
.rname{font-weight:600}
.rsub{font-size:11.5px;color:var(--rilasciata);margin-top:1px}
.mute{color:var(--mute)}
.nowrap{white-space:nowrap}
.spill{display:inline-flex;align-items:center;gap:7px;white-space:nowrap}
.rnote{color:var(--mute);max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ra{white-space:nowrap;text-align:right;width:64px}
.ra .ib{opacity:0}
.row:hover .ra .ib,.row.is-sel .ra .ib{opacity:1}

/* pannello dettagli */
.panel{position:fixed;top:0;right:0;bottom:0;width:400px;max-width:100%;background:#18181b;border-left:1px solid var(--line);box-shadow:-24px 0 48px #0007;transform:translateX(104%);transition:transform .22s cubic-bezier(.2,.8,.2,1);display:flex;flex-direction:column;z-index:20}
.panel.open{transform:none}
@media (min-width:1400px){body.panel-open .content{margin-right:400px}}
.p-scroll{flex:1;overflow:auto;padding:18px 20px 22px}
.p-head{display:flex;align-items:flex-start;gap:10px}
.p-head>div{flex:1;min-width:0}
.p-head h2{margin:3px 0 0;font-size:20px;line-height:1.25;word-break:break-word}
.p-alias{font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.7px;font-weight:650}
.p-path{display:flex;gap:7px;align-items:flex-start;margin-top:8px;color:var(--dim);font-size:11.5px;word-break:break-all;line-height:1.4}
.p-path:hover{color:var(--soft)}
.p-path svg{width:13px;height:13px;margin-top:1px}
.facts{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:16px}
.fact{background:var(--raise);border:1px solid var(--line);border-radius:9px;padding:8px 10px;font-size:11px;color:var(--mute)}
.fact b{display:block;color:var(--text);font-size:15px;font-weight:600;margin-top:1px;font-variant-numeric:tabular-nums}
.warn{display:flex;gap:8px;align-items:center;margin-top:12px;background:#ffb34012;border:1px solid #ffb34030;color:var(--warn);border-radius:9px;padding:8px 10px;font-size:12px}
.lbl{display:flex;align-items:center;justify-content:space-between;margin:20px 0 8px;color:var(--mute);font-size:11px;font-weight:650;letter-spacing:.6px;text-transform:uppercase}
.hint{text-transform:none;letter-spacing:0;font-weight:400;color:var(--dim)}
.stgrid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.stb{display:flex;align-items:center;gap:8px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--raise);color:var(--soft)}
.stb:hover{border-color:var(--line2);color:var(--text)}
.stb .k{margin-left:auto;color:var(--dim);font-size:11px;font-variant-numeric:tabular-nums}
.stb.on{border-color:var(--c);background:#ffffff0c;color:#fff;box-shadow:inset 0 0 0 1px var(--c)}
.autonote{margin-top:8px;font-size:12px;color:var(--dim)}
textarea{display:block;width:100%;min-height:170px;background:var(--raise);border:1px solid var(--line);border-radius:9px;padding:10px 12px;resize:vertical;outline:none;line-height:1.5}
textarea::placeholder,.field::placeholder{color:var(--dim)}
.field{width:100%;background:var(--raise);border:1px solid var(--line);border-radius:8px;padding:7px 10px;outline:none}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.saved{color:var(--finita);opacity:0;transition:opacity .3s;text-transform:none;letter-spacing:0;font-weight:500}
.saved.show{opacity:1}
.p-foot{border-top:1px solid var(--line);padding:12px 16px 12px 20px;display:flex;gap:8px;align-items:center}
.btn{display:inline-flex;align-items:center;gap:7px;background:var(--raise);border:1px solid var(--line2);border-radius:8px;padding:7px 12px;font-weight:500;white-space:nowrap}
.btn:hover{background:#26262c}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{background:#2391ff}
.btn svg{width:14px;height:14px}
.p-keys{margin-left:auto;color:var(--dim);font-size:11px;text-align:right;line-height:1.45}

/* menu contestuale */
.menu{position:fixed;z-index:50;min-width:230px;background:#232328f5;-webkit-backdrop-filter:blur(24px);backdrop-filter:blur(24px);border:1px solid var(--line2);border-radius:10px;box-shadow:var(--shadow);padding:5px;display:none}
.menu button{display:flex;align-items:center;gap:9px;width:100%;padding:6px 9px;border-radius:6px}
.menu button:hover{background:var(--accent);color:#fff}
.menu button svg{width:14px;height:14px;color:var(--mute)}
.menu button:hover svg{color:#fff}
.menu .mh{padding:6px 9px 3px;color:var(--dim);font-size:11px;font-weight:650}
.menu .sep{height:1px;background:var(--line2);margin:5px 4px}
.menu .chk{margin-left:auto}
.menu .dot{margin:0 3px}

/* notifiche */
.toast{position:fixed;left:50%;bottom:22px;transform:translate(-50%,16px);opacity:0;pointer-events:none;background:#2a2a30f5;-webkit-backdrop-filter:blur(24px);backdrop-filter:blur(24px);border:1px solid var(--line2);box-shadow:var(--shadow);border-radius:10px;padding:9px 10px 9px 14px;display:flex;align-items:center;gap:12px;z-index:60;transition:opacity .2s,transform .2s;max-width:min(560px,90vw)}
.toast.show{opacity:1;transform:translate(-50%,0);pointer-events:auto}
.toast span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.toast button{color:#4da3ff;font-weight:600;padding:3px 8px;border-radius:6px;white-space:nowrap}
.toast button:hover{background:#ffffff12}

/* impostazioni */
.overlay{position:fixed;inset:0;background:#0009;display:none;align-items:center;justify-content:center;z-index:40;padding:20px}
.overlay.show{display:flex}
.modal{background:#1c1c20;border:1px solid var(--line2);border-radius:14px;width:min(500px,100%);max-height:100%;overflow:auto;padding:22px;box-shadow:var(--shadow)}
.modal h2{margin:0 0 4px;font-size:18px}
.small{font-size:12px;color:var(--dim);word-break:break-all}
.inline{display:flex;align-items:center;gap:10px;color:var(--soft)}
.check{display:flex;gap:9px;align-items:flex-start;color:var(--soft);cursor:pointer}
.check input{margin-top:3px}
.keys{display:grid;grid-template-columns:auto 1fr;gap:7px 14px;font-size:12.5px;color:var(--soft);align-items:center}
kbd{font:11.5px -apple-system,sans-serif;color:var(--text);background:#ffffff10;border:1px solid var(--line2);border-bottom-width:2px;border-radius:5px;padding:1px 6px;white-space:nowrap}
.m-foot{display:flex;justify-content:flex-end;margin-top:22px}

/* stati vuoti */
.empty{height:100%;min-height:300px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:var(--mute);text-align:center;padding:40px}
.empty .big{font-size:16px;color:var(--text);font-weight:600}
.empty .small{max-width:420px}
.empty .btn{margin-top:8px}
.spinner{width:22px;height:22px;border:2px solid #ffffff22;border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* finestre strette */
.side-btn{display:none}
@media (max-width:760px){
  .shell{grid-template-columns:minmax(0,1fr)}
  .side{position:fixed;left:0;top:0;bottom:0;width:260px;z-index:30;transform:translateX(-102%);transition:transform .2s}
  body.side-open .side{transform:none;box-shadow:var(--shadow)}
  .side-btn{display:inline-grid}
  .toolbar{flex-wrap:wrap;padding:10px 12px}
  .search{order:5;flex:1 1 100%}
  .search input{width:100%}
  main{padding:12px}
  .panel{width:100%}
}
</style></head><body>
<div class="shell">
  <aside class="side" id="side">
    <div class="brand"><span class="logo"><svg viewBox="0 0 20 20"><path d="M3.5 9v2" stroke="#8e8e93"/><path d="M6.75 6.5v7" stroke="#0a84ff"/><path d="M10 3.5v13" stroke="#bf5af2"/><path d="M13.25 6v8" stroke="#ff9f0a"/><path d="M16.5 8.5v3" stroke="#30d158"/></svg></span>Track Tracker</div>
    <div id="sideStats"></div>
    <nav class="navs" id="nav"></nav>
    <div class="side-foot"><span data-i="folder"></span><span class="fn" id="folder"></span><button class="ib" id="settingsBtn" title="Impostazioni e scorciatoie" data-i="gear"></button></div>
  </aside>
  <section class="content">
    <header class="toolbar">
      <button class="ib side-btn" id="sideBtn" title="Mostra filtri" data-i="menu"></button>
      <div class="ttl"><h1 id="title">Tutte le tracce</h1><span id="count"></span></div>
      <label class="search"><span data-i="search"></span><input id="q" placeholder="Cerca tracce e note" autocomplete="off" spellcheck="false"><kbd id="qkey">⌘F</kbd></label>
      <select class="dd" id="sort" title="Ordina"></select>
      <div class="seg"><button data-view="board" data-i="board"> Board</button><button data-view="list" data-i="list"> Lista</button></div>
      <button class="ib" id="reload" title="Rileggi la cartella" data-i="refresh"></button>
    </header>
    <main id="main"></main>
  </section>
</div>
<aside class="panel" id="panel"></aside>
<div class="menu" id="menu"></div>
<div class="overlay" id="ovs"><div class="modal" id="smodal"></div></div>
<div class="toast" id="toast"><span id="toastMsg"></span><button id="toastBtn"></button></div>

<script>
const ICON={
 search:'<svg viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.5"/><path d="m10.5 10.5 3.5 3.5"/></svg>',
 board:'<svg viewBox="0 0 16 16"><rect x="1.75" y="2.5" width="3.5" height="11" rx="1"/><rect x="6.25" y="2.5" width="3.5" height="7" rx="1"/><rect x="10.75" y="2.5" width="3.5" height="9" rx="1"/></svg>',
 list:'<svg viewBox="0 0 16 16"><path d="M5.5 4h8.5M5.5 8h8.5M5.5 12h8.5"/><path d="M2 4h.5M2 8h.5M2 12h.5"/></svg>',
 refresh:'<svg viewBox="0 0 16 16"><path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9"/><path d="M13.5 2.5v3h-3"/></svg>',
 gear:'<svg viewBox="0 0 16 16"><path d="M2 4.5h7.5M12.5 4.5H14M2 11.5h1.5M6.5 11.5H14"/><circle cx="11" cy="4.5" r="1.5"/><circle cx="5" cy="11.5" r="1.5"/></svg>',
 play:'<svg viewBox="0 0 16 16"><path d="M5 3.3v9.4l7.4-4.7z" fill="currentColor" stroke-width="1"/></svg>',
 folder:'<svg viewBox="0 0 16 16"><path d="M1.75 4.5c0-.6.4-1 1-1h3.1l1.5 1.5h5.9c.6 0 1 .4 1 1v6.25c0 .6-.4 1-1 1H2.75c-.6 0-1-.4-1-1z"/></svg>',
 more:'<svg viewBox="0 0 16 16"><circle cx="3.5" cy="8" r="1.1" fill="currentColor" stroke="none"/><circle cx="8" cy="8" r="1.1" fill="currentColor" stroke="none"/><circle cx="12.5" cy="8" r="1.1" fill="currentColor" stroke="none"/></svg>',
 close:'<svg viewBox="0 0 16 16"><path d="m4 4 8 8M12 4l-8 8"/></svg>',
 clock:'<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="M8 4.75V8l2.25 1.5"/></svg>',
 wave:'<svg viewBox="0 0 16 16"><path d="M2.5 7v2M5.25 4.5v7M8 2.5v11M10.75 5v6M13.5 7v2"/></svg>',
 user:'<svg viewBox="0 0 16 16"><circle cx="8" cy="5.5" r="2.75"/><path d="M2.75 14c.75-2.75 2.9-4.1 5.25-4.1s4.5 1.35 5.25 4.1"/></svg>',
 users:'<svg viewBox="0 0 16 16"><circle cx="6" cy="5.5" r="2.5"/><path d="M1.5 13.5c.6-2.4 2.4-3.6 4.5-3.6s3.9 1.2 4.5 3.6"/><path d="M10.5 3.2a2.4 2.4 0 0 1 0 4.6M12 10c1.3.4 2.2 1.4 2.5 3.5"/></svg>',
 info:'<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="M8 7.25v4"/><circle cx="8" cy="5" r=".6" fill="currentColor"/></svg>',
 wand:'<svg viewBox="0 0 16 16"><path d="m2.5 13.5 8-8M11.5 2v2.5M11.5 7v.5M9 4.5h-.5M14.5 4.5H14"/></svg>',
 collapse:'<svg viewBox="0 0 16 16"><path d="M10 4 6 8l4 4"/></svg>',
 menu:'<svg viewBox="0 0 16 16"><path d="M2.5 4h11M2.5 8h11M2.5 12h11"/></svg>',
};
const MAC=/Mac|iPhone|iPad/i.test(navigator.platform||navigator.userAgent);
const CMD=MAC?'⌘':'Ctrl+';
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function store(k,v){try{if(v===undefined)return localStorage.getItem('tt_'+k);localStorage.setItem('tt_'+k,v)}catch(e){return null}}
function storedSet(k){try{return new Set(JSON.parse(store(k)||'[]'))}catch(e){return new Set()}}
const SORTS={'mtime:-1':'Più recenti','mtime:1':'Meno recenti','name:1':'Nome','bpm:-1':'BPM','status:1':'Stato'};
const S={projects:[],statuses:[],settings:{stale_days:60},root:'',aliasN:0,loaded:false,error:'',
  alias:'__all',filter:'all',q:'',view:store('view')==='list'?'list':'board',sort:store('sort')||'mtime:-1',
  openId:null,menuId:null,pendingNotes:null,notesT:null,collapsed:storedSet('collapsed'),expanded:storedSet('expanded')};
// prima apertura: "Rilasciata" fa da archivio, quindi parte compressa
if(store('collapsed')===null)S.collapsed.add('rilasciata');
if(!SORTS[S.sort])S.sort='mtime:-1';
const DONE=['finita','rilasciata'];
const label=k=>k?((S.statuses.find(s=>s.key===k)||{}).label||k):'automatico';
const isStale=p=>!DONE.includes(p.status)&&p.days>=S.settings.stale_days;
const byId=id=>S.projects.find(p=>p.id===id);
const isTyping=a=>!!a&&(a.tagName==='TEXTAREA'||a.tagName==='SELECT'||(a.tagName==='INPUT'&&a.type!=='checkbox'));

function ago(d){if(d<=0)return'oggi';if(d===1)return'ieri';if(d<45)return d+' giorni fa';if(d<365)return Math.round(d/30)+' mesi fa';const y=Math.floor(d/365);return y===1?'1 anno fa':y+' anni fa'}
function fmtDate(s){const d=new Date(s+'T00:00:00');return isNaN(d)?s:d.toLocaleDateString('it-IT',{day:'numeric',month:'short',year:'numeric'})}
function releaseText(p){const parts=[];if(p.release_date)parts.push((new Date(p.release_date+'T00:00:00')>new Date()?'Esce il ':'Uscita il ')+fmtDate(p.release_date));if(p.label)parts.push(p.label);return parts.join(' · ')}

async function api(path,body){
  const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
  const d=await r.json().catch(()=>({error:'Risposta non valida'}));
  if(!r.ok)throw new Error(d.error||r.status);
  return d;
}

let toastT;
function toast(msg,action){
  $('#toastMsg').textContent=msg;const b=$('#toastBtn');
  if(action){b.textContent=action.label;b.style.display='';b.onclick=()=>{hideToast();action.fn()}}
  else{b.style.display='none';b.onclick=null}
  $('#toast').classList.add('show');clearTimeout(toastT);toastT=setTimeout(hideToast,action?6000:3000);
}
function hideToast(){$('#toast').classList.remove('show')}

async function load(fresh){
  try{
    const d=await api('/api/projects'+(fresh?'?fresh=1':''));
    Object.assign(S,{projects:d.projects,statuses:d.statuses,settings:d.settings,root:d.root,loaded:true,error:''});
  }catch(e){S.error=e.message;S.loaded=true}
  S.aliasN=new Set(S.projects.map(p=>p.alias)).size;
  if(S.alias!=='__all'&&!S.projects.some(p=>p.alias===S.alias))S.alias='__all';
  render();
  if(S.openId){byId(S.openId)?renderPanel():closePanel()}
}

// ------------------------------------------------------------ filtri e ordine
function filtered(){
  const q=S.q.trim().toLowerCase();
  return S.projects.filter(p=>(S.alias==='__all'||p.alias===S.alias)
    &&(S.filter==='all'||(S.filter==='stale'?isStale(p):p.status===S.filter))
    &&(!q||[p.name,p.folder,p.notes,p.label,p.alias].join(' ').toLowerCase().includes(q)));
}
function sortSpec(){const [k,d]=S.sort.split(':');return{k,dir:+d||1}}
function sorted(list){
  const {k,dir}=sortSpec(),order=S.statuses.map(s=>s.key);
  const val=p=>k==='status'?order.indexOf(p.status):k==='bpm'?(p.bpm||0):k==='mtime'?p.mtime:String(p[k]||'').toLowerCase();
  return [...list].sort((a,b)=>{const x=val(a),y=val(b);return((x>y)-(x<y))*dir||b.mtime-a.mtime});
}
function visibleOrder(){
  const l=sorted(filtered());
  if(S.view!=='board')return l;
  const o=S.statuses.map(s=>s.key);
  return l.sort((a,b)=>o.indexOf(a.status)-o.indexOf(b.status));
}

// ------------------------------------------------------------ disegno
function render(){renderSide();renderToolbar();renderMain()}

function renderSide(){
  const inA=S.projects.filter(p=>S.alias==='__all'||p.alias===S.alias);
  const cnt={};inA.forEach(p=>cnt[p.status]=(cnt[p.status]||0)+1);
  const done=inA.filter(p=>DONE.includes(p.status)).length;
  $('#sideStats').innerHTML=inA.length?`<div class="stackbar">${S.statuses.filter(s=>cnt[s.key]).map(s=>
    `<span style="--c:var(--${s.key});flex:${cnt[s.key]}" title="${esc(s.label)}: ${cnt[s.key]}"></span>`).join('')}</div>
    <div class="stackinfo">${done} di ${inA.length} finite o rilasciate</div>`:'';
  const nav=(attr,icon,text,n,on,cls='')=>`<button class="nav${on?' on':''} ${cls}" ${attr}>${icon}<span class="nt">${esc(text)}</span><span class="nc">${n}</span></button>`;
  let h='<div class="sec">Libreria</div>'
    +nav('data-filter="all"',ICON.wave,'Tutte le tracce',inA.length,S.filter==='all')
    +nav('data-filter="stale"',ICON.clock,'Ferme',inA.filter(isStale).length,S.filter==='stale','stale')
    +'<div class="sec">Stati</div>'
    +S.statuses.map(s=>nav(`data-filter="${s.key}"`,`<span class="dot" style="--c:var(--${s.key})"></span>`,s.label,cnt[s.key]||0,S.filter===s.key)).join('');
  if(S.aliasN>1){
    const aliases=[...new Set(S.projects.map(p=>p.alias))].sort((a,b)=>a.localeCompare(b,'it'));
    h+='<div class="sec">Alias</div>'+nav('data-alias="__all"',ICON.users,'Tutti gli alias',S.projects.length,S.alias==='__all')
      +aliases.map(a=>nav(`data-alias="${esc(a)}"`,ICON.user,a,S.projects.filter(p=>p.alias===a).length,S.alias===a)).join('');
  }
  $('#nav').innerHTML=h;
  $('#folder').textContent=S.root.split('/').filter(Boolean).pop()||S.root||'…';
  $('#folder').title=S.root;
}

function renderToolbar(){
  const t=S.filter==='all'?'Tutte le tracce':S.filter==='stale'?'Tracce ferme':label(S.filter);
  $('#title').textContent=t+(S.alias!=='__all'?' · '+S.alias:'');
  const n=filtered().length;
  $('#count').textContent=S.loaded?n+(n===1?' traccia':' tracce'):'';
  document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('on',b.dataset.view===S.view));
  $('#sort').value=SORTS[S.sort]?S.sort:'';
}

function renderMain(){
  const m=$('#main');m.className=S.view;
  if(!S.loaded){m.innerHTML='<div class="empty"><div class="spinner"></div>Carico i progetti…</div>';return}
  if(S.error){m.innerHTML=`<div class="empty"><div class="big">Non riesco a leggere i progetti</div><div class="small">${esc(S.error)}</div><button class="btn" id="retry">Riprova</button></div>`;return}
  if(!S.projects.length){m.innerHTML=`<div class="empty"><div class="big">Nessun progetto Ableton trovato</div>Cerco le cartelle che contengono un file .als dentro<div class="small">${esc(S.root)}</div></div>`;return}
  const list=sorted(filtered());
  if(!list.length){m.innerHTML=`<div class="empty"><div class="big">Nessuna traccia</div>Niente corrisponde ai filtri o alla ricerca.<button class="btn" id="resetF">Azzera filtri</button></div>`;return}
  S.view==='board'?renderBoard(list):renderList(list);
}

function renderBoard(list){
  const only=S.statuses.some(s=>s.key===S.filter);
  $('#main').innerHTML='<div class="board-w">'+S.statuses.filter(s=>!only||s.key===S.filter).map(s=>{
    const items=list.filter(p=>p.status===s.key),st=`style="--c:var(--${s.key})"`;
    const collapsed=!only&&(S.collapsed.has(s.key)||(!items.length&&!S.expanded.has(s.key)));
    if(collapsed)return `<section class="col collapsed" data-status="${s.key}" data-toggle="${s.key}" ${st} title="Espandi «${esc(s.label)}»">
      <div class="colh"><span class="dot"></span><span class="vlabel">${esc(s.label)}</span><span class="n">${items.length}</span></div></section>`;
    return `<section class="col" data-status="${s.key}" ${st}><div class="colh"><span class="dot"></span><span class="ct">${esc(s.label)}</span>
      <span class="n">${items.length}</span>${only?'':`<button class="ib sm" data-toggle="${s.key}" title="Comprimi colonna">${ICON.collapse}</button>`}</div>
      <div class="cards">${items.length?items.map(card).join(''):'<div class="col-empty">Trascina qui una traccia</div>'}</div></section>`;
  }).join('')+'</div>';
}

function card(p){
  const meta=[];
  if(S.alias==='__all'&&S.aliasN>1)meta.push(`<span class="pill">${esc(p.alias)}</span>`);
  if(p.bpm)meta.push(`<span class="bpm">${p.bpm} BPM</span>`);
  meta.push(`<span>${ago(p.days)}</span>`);
  if(p.versions>1)meta.push(`<span title="File .als nella cartella">${p.versions} versioni</span>`);
  if(p.status_source==='auto')meta.push('<span class="auto" title="Stato dedotto dal nome della cartella">auto</span>');
  const rel=releaseText(p);
  return `<article class="card${p.id===S.openId?' is-sel':''}" draggable="true" data-id="${esc(p.id)}" style="--c:var(--${p.status})">
    <div class="c-top"><div class="cname">${esc(p.name)}</div><div class="c-act">
      <button class="ib sm" data-act="play" title="Apri in Ableton">${ICON.play}</button>
      <button class="ib sm" data-act="more" title="Altre azioni">${ICON.more}</button></div></div>
    <div class="meta">${meta.join('')}</div>
    ${isStale(p)?`<div class="meta"><span class="pill stale-b">${ICON.clock}Ferma da ${p.days} giorni</span></div>`:''}
    ${rel?`<div class="crel">${esc(rel)}</div>`:''}
    ${p.notes?`<div class="cnote">${esc(p.notes)}</div>`:''}</article>`;
}

function renderList(list){
  const {k,dir}=sortSpec(),many=S.aliasN>1;
  const th=(key,l,cls='')=>`<th class="${cls}" data-sort="${key}">${l}${k===key?(dir>0?' ↑':' ↓'):''}</th>`;
  $('#main').innerHTML=`<table class="tbl"><thead><tr>${th('name','Traccia')}${many?th('alias','Alias'):''}${th('status','Stato')}${th('bpm','BPM','num')}${th('mtime','Modificata')}<th>Note</th><th></th></tr></thead><tbody>`
   +list.map(p=>{const rel=releaseText(p);return `<tr class="row${p.id===S.openId?' is-sel':''}" data-id="${esc(p.id)}" style="--c:var(--${p.status})">
    <td><div class="rname">${esc(p.name)}</div>${rel?`<div class="rsub">${esc(rel)}</div>`:''}</td>
    ${many?`<td class="mute">${esc(p.alias)}</td>`:''}
    <td><span class="spill"><span class="dot"></span>${esc(label(p.status))}${p.status_source==='auto'?'<span class="auto">auto</span>':''}</span></td>
    <td class="num">${p.bpm||'–'}</td>
    <td class="nowrap">${ago(p.days)}${isStale(p)?` <span class="pill stale-b" title="Ferma">${ICON.clock}</span>`:''}</td>
    <td class="rnote">${esc((p.notes||'').split('\n')[0])}</td>
    <td class="ra"><button class="ib sm" data-act="play" title="Apri in Ableton">${ICON.play}</button><button class="ib sm" data-act="more" title="Altre azioni">${ICON.more}</button></td></tr>`}).join('')
   +'</tbody></table>';
}

// ------------------------------------------------------------ pannello dettagli
function renderPanel(){
  const el=$('#panel'),p=byId(S.openId);if(!p)return closePanel();
  const a=document.activeElement;
  const keep=a&&el.contains(a)&&a.id?{id:a.id,v:a.value,s:a.selectionStart,e:a.selectionEnd}:null;
  const ta=$('#notes'),notes=ta&&ta.dataset.pid===p.id?ta.value:p.notes;
  const facts=[['BPM',p.bpm||'–'],['Modificata',ago(p.days)],['Versioni .als',p.versions]];
  el.innerHTML=`<div class="p-scroll">
    <div class="p-head"><div><div class="p-alias">${esc(p.alias)}</div><h2>${esc(p.name)}</h2></div>
      <button class="ib" data-close title="Chiudi (Esc)">${ICON.close}</button></div>
    <button class="p-path" data-act="finder" title="Mostra nel Finder">${ICON.folder}<span>${esc(p.path)}</span></button>
    <div class="facts">${facts.map(([k,v])=>`<div class="fact">${k}<b>${esc(v)}</b></div>`).join('')}</div>
    ${isStale(p)?`<div class="warn">${ICON.clock}Non la tocchi da ${p.days} giorni</div>`:''}
    <div class="lbl">Stato<span class="hint">tasti 1–${S.statuses.length}</span></div>
    <div class="stgrid">${S.statuses.map((s,i)=>`<button class="stb${p.status===s.key?' on':''}" data-st="${s.key}" style="--c:var(--${s.key})"><span class="dot"></span>${esc(s.label)}<span class="k">${i+1}</span></button>`).join('')}</div>
    ${p.status_source==='auto'?'<div class="autonote">Stato dedotto dal nome della cartella: scegline uno per confermarlo.</div>':''}
    <div class="lbl">Note e cose da fare<span class="saved" id="saved">Salvato ✓</span></div>
    <textarea id="notes" data-pid="${esc(p.id)}" placeholder="Rifare il drop, kick troppo forte, mandare a…">${esc(notes)}</textarea>
    <div class="row2"><div><div class="lbl">Label</div><input class="field" id="label" value="${esc(p.label)}" placeholder="es. Toolroom"></div>
      <div><div class="lbl">Data di uscita</div><input class="field" type="date" id="rdate" value="${esc(p.release_date)}"></div></div>
  </div>
  <div class="p-foot"><button class="btn primary" data-act="play">${ICON.play}Apri in Ableton</button>
    <button class="btn" data-act="finder">${ICON.folder}Finder</button>
    <div class="p-keys">${CMD}↵ apri · ↑↓ tracce<br>Esc chiudi</div></div>`;
  if(keep){const n=document.getElementById(keep.id);if(n){n.value=keep.v;n.focus();try{n.setSelectionRange(keep.s,keep.e)}catch(e){}}}
  $('#notes').addEventListener('input',e=>{S.pendingNotes={id:p.id,v:e.target.value};clearTimeout(S.notesT);S.notesT=setTimeout(flushNotes,700)});
  $('#label').addEventListener('change',e=>update(p.id,{label:e.target.value}));
  $('#rdate').addEventListener('change',e=>update(p.id,{release_date:e.target.value}));
}
function flushNotes(){clearTimeout(S.notesT);const n=S.pendingNotes;S.pendingNotes=null;if(n)update(n.id,{notes:n.v})}
function openPanel(id){
  if(S.openId!==id)flushNotes();
  S.openId=id;renderPanel();
  $('#panel').classList.add('open');document.body.classList.add('panel-open');markSel(true);
}
function closePanel(){
  flushNotes();S.openId=null;
  $('#panel').classList.remove('open');document.body.classList.remove('panel-open');markSel();
}
function markSel(scroll){
  document.querySelectorAll('#main [data-id]').forEach(e=>{
    const on=e.dataset.id===S.openId;e.classList.toggle('is-sel',on);
    if(on&&scroll)setTimeout(()=>e.scrollIntoView({block:'nearest',inline:'nearest',behavior:'smooth'}),60);
  });
}
function move(delta){
  const l=visibleOrder();if(!l.length)return;
  let i=l.findIndex(p=>p.id===S.openId);
  i=i<0?0:Math.max(0,Math.min(l.length-1,i+delta));
  openPanel(l[i].id);
}

// ------------------------------------------------------------ azioni
async function update(id,fields){
  try{
    const d=await api('/api/update',{id,...fields});
    const i=S.projects.findIndex(p=>p.id===id);if(i>=0)S.projects[i]=d.project;
    if(d.warning)toast(d.warning);
    renderSide();renderToolbar();renderMain();
    if(S.openId===id){
      if('notes' in fields){const s=$('#saved');if(s){s.classList.add('show');setTimeout(()=>s.classList.remove('show'),1300)}}
      else renderPanel();
    }
    return d.project;
  }catch(e){toast('Errore: '+e.message)}
}
async function setStatus(id,key){
  const p=byId(id);if(!p)return;
  if(key?(p.status===key&&p.status_source==='manual'):p.status_source==='auto')return;
  const prev=p.status_source==='manual'?p.status:'';
  const np=await update(id,{status:key});
  if(np&&S.openId===id)markSel(true);
  if(np)toast(key?`«${p.name}» → ${label(key)}`:`«${p.name}» torna allo stato automatico (${label(np.status)})`,
    {label:'Annulla',fn:()=>update(id,{status:prev})});
}
async function openIn(id,action){if(!id)return;try{await api('/api/open',{id,action})}catch(e){toast(e.message)}}
function toggleCol(k,isCollapsed){
  if(isCollapsed){S.collapsed.delete(k);S.expanded.add(k)}else{S.expanded.delete(k);S.collapsed.add(k)}
  store('collapsed',JSON.stringify([...S.collapsed]));store('expanded',JSON.stringify([...S.expanded]));
  renderMain();
}
function closeSide(){document.body.classList.remove('side-open')}

function openMenu(id,x,y){
  const p=byId(id);if(!p)return;S.menuId=id;const m=$('#menu');
  m.innerHTML=`<button data-m="open">${ICON.play}Apri in Ableton</button><button data-m="finder">${ICON.folder}Mostra nel Finder</button>
    <button data-m="details">${ICON.info}Dettagli e note</button><div class="sep"></div><div class="mh">Sposta in</div>`
    +S.statuses.map(s=>`<button data-m="st" data-st="${s.key}" style="--c:var(--${s.key})"><span class="dot"></span>${esc(s.label)}${p.status===s.key?'<span class="chk">✓</span>':''}</button>`).join('')
    +(p.status_source==='manual'?`<div class="sep"></div><button data-m="st" data-st="">${ICON.wand}Stato automatico dal nome</button>`:'');
  m.style.left='0px';m.style.top='0px';m.style.display='block';
  const r=m.getBoundingClientRect();
  m.style.left=Math.max(8,Math.min(x,innerWidth-r.width-8))+'px';
  m.style.top=Math.max(8,Math.min(y,innerHeight-r.height-8))+'px';
}
function closeMenu(){$('#menu').style.display='none';S.menuId=null}
function menuAction(b){
  const id=S.menuId,a=b.dataset.m;closeMenu();
  if(a==='open')openIn(id,'ableton');else if(a==='finder')openIn(id,'finder');
  else if(a==='details')openPanel(id);else if(a==='st')setStatus(id,b.dataset.st);
}

function openSettings(){
  const s=S.settings;
  $('#smodal').innerHTML=`<h2>Impostazioni</h2><div class="small">Cartella: ${esc(S.root)}</div>
    <div class="lbl">Tracce ferme</div>
    <div class="inline">Dopo <input class="field" type="number" min="1" id="sdays" value="${s.stale_days}" style="width:80px"> giorni senza modifiche</div>
    <div class="lbl">Tag del Finder</div>
    <label class="check"><input type="checkbox" id="stags" ${s.finder_tags?'checked':''} ${MAC?'':'disabled'}>Quando cambio stato, metti il tag colorato alla cartella del progetto</label>
    <button class="btn" id="syncTags" style="margin-top:12px" ${MAC?'':'disabled'}>Applica i tag a tutte le tracce</button>
    <div class="lbl">Scorciatoie</div>
    <div class="keys"><span><kbd>${CMD}F</kbd> <kbd>/</kbd></span>Cerca<span><kbd>↑</kbd> <kbd>↓</kbd></span>Scorri le tracce e apri i dettagli
      <span><kbd>1</kbd>–<kbd>${S.statuses.length}</kbd></span>Cambia stato alla traccia aperta<span><kbd>${CMD}↵</kbd></span>Apri in Ableton
      <span><kbd>Esc</kbd></span>Chiudi pannello o ricerca<span>Tasto destro</span>Menu della traccia</div>
    <div class="m-foot"><button class="btn primary" id="closeS">Fatto</button></div>`;
  $('#sdays').addEventListener('change',async e=>{try{const d=await api('/api/settings',{stale_days:+e.target.value||60});S.settings=d.settings;render()}catch(err){toast(err.message)}});
  $('#stags').addEventListener('change',async e=>{try{const d=await api('/api/settings',{finder_tags:e.target.checked});S.settings=d.settings}catch(err){toast(err.message)}});
  $('#syncTags').addEventListener('click',async()=>{try{const d=await api('/api/sync_tags',{});toast(`Tag applicati a ${d.ok} tracce`+(d.errors.length?` (${d.errors.length} errori)`:''))}catch(e){toast('Errore: '+e.message)}});
  $('#ovs').classList.add('show');
}
function closeSettings(){$('#ovs').classList.remove('show')}

// ------------------------------------------------------------ eventi
document.addEventListener('click',e=>{
  if(e.button!==0||(MAC&&e.ctrlKey))return;  // tasto destro / ctrl-clic: ci pensa il menu
  const t=e.target;let x;
  if(t.closest('#menu')){const b=t.closest('[data-m]');if(b)menuAction(b);return}
  if(S.menuId)closeMenu();
  if(t.closest('#toast'))return;
  if(x=t.closest('[data-act]')){
    const host=x.closest('[data-id]'),id=host?host.dataset.id:S.openId,a=x.dataset.act;
    if(a==='play')openIn(id,'ableton');else if(a==='finder')openIn(id,'finder');
    else if(a==='more'){const r=x.getBoundingClientRect();openMenu(id,r.left,r.bottom+4)}
    return;
  }
  if(x=t.closest('[data-view]')){S.view=x.dataset.view;store('view',S.view);render();markSel(true);return}
  if(x=t.closest('[data-filter]')){S.filter=x.dataset.filter;closeSide();render();return}
  if(x=t.closest('[data-alias]')){S.alias=x.dataset.alias;closeSide();render();return}
  if(x=t.closest('[data-toggle]')){toggleCol(x.dataset.toggle,!!x.closest('.collapsed'));return}
  if(x=t.closest('[data-sort]')){const k=x.dataset.sort,c=sortSpec();S.sort=k+':'+(c.k===k?-c.dir:(k==='mtime'||k==='bpm'?-1:1));store('sort',S.sort);renderToolbar();renderMain();return}
  if(x=t.closest('#panel [data-st]')){setStatus(S.openId,x.dataset.st);return}
  if(t.closest('[data-close]')){closePanel();return}
  if(t.closest('#reload')){load(true).then(()=>toast('Cartella riletta'));return}
  if(t.closest('#settingsBtn')){openSettings();return}
  if(t.closest('#closeS')||t.id==='ovs'){closeSettings();return}
  if(t.closest('#sideBtn')){document.body.classList.toggle('side-open');return}
  if(t.closest('#resetF')){S.filter='all';S.alias='__all';S.q='';$('#q').value='';render();return}
  if(t.closest('#retry')){load(true);return}
  if(x=t.closest('#main [data-id]')){openPanel(x.dataset.id);return}
  if(document.body.classList.contains('side-open')&&!t.closest('#side'))closeSide();
});
document.addEventListener('contextmenu',e=>{
  const x=e.target.closest('#main [data-id]');if(!x)return;
  e.preventDefault();openMenu(x.dataset.id,e.clientX,e.clientY);
});
document.addEventListener('keydown',e=>{
  const a=document.activeElement,typing=isTyping(a),mod=e.metaKey||e.ctrlKey;
  if(mod&&e.key.toLowerCase()==='f'){e.preventDefault();$('#q').focus();$('#q').select();return}
  if(mod&&e.key==='Enter'&&S.openId){e.preventDefault();openIn(S.openId,'ableton');return}
  if(e.key==='Escape'){
    if(S.menuId)return closeMenu();
    if($('#ovs').classList.contains('show'))return closeSettings();
    if(a===$('#q')&&S.q){S.q='';a.value='';renderToolbar();renderMain();return}
    if(typing)return a.blur();
    if(S.openId)return closePanel();
    return;
  }
  if(typing||mod||e.altKey||$('#ovs').classList.contains('show'))return;
  if(e.key==='/'){e.preventDefault();$('#q').focus();return}
  if(S.openId&&/^[1-9]$/.test(e.key)){const s=S.statuses[+e.key-1];if(s)setStatus(S.openId,s.key);return}
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();closeMenu();move(e.key==='ArrowDown'?1:-1)}
});
$('#q').addEventListener('input',e=>{S.q=e.target.value;renderToolbar();renderMain()});
$('#sort').addEventListener('change',e=>{if(e.target.value){S.sort=e.target.value;store('sort',S.sort);renderMain()}});

let dragId=null;
document.addEventListener('dragstart',e=>{
  const c=e.target.closest&&e.target.closest('.card');if(!c)return;
  dragId=c.dataset.id;c.classList.add('dragging');closeMenu();
  e.dataTransfer.effectAllowed='move';try{e.dataTransfer.setData('text/plain',dragId)}catch(_){}
});
document.addEventListener('dragend',()=>{dragId=null;document.querySelectorAll('.dragging,.over').forEach(x=>x.classList.remove('dragging','over'))});
document.addEventListener('dragover',e=>{
  const col=e.target.closest&&e.target.closest('.col');if(!col||!dragId)return;
  e.preventDefault();e.dataTransfer.dropEffect='move';
  document.querySelectorAll('.col.over').forEach(x=>x!==col&&x.classList.remove('over'));col.classList.add('over');
});
document.addEventListener('drop',e=>{
  const col=e.target.closest&&e.target.closest('.col');if(!col||!dragId)return;
  e.preventDefault();const id=dragId;dragId=null;setStatus(id,col.dataset.status);
});
window.addEventListener('focus',()=>{if(!isTyping(document.activeElement))load()});

document.querySelectorAll('[data-i]').forEach(e=>e.insertAdjacentHTML('afterbegin',ICON[e.dataset.i]));
$('#qkey').textContent=MAC?'⌘F':'Ctrl F';
$('#sort').innerHTML='<option value="" hidden>Personalizzato</option>'+Object.entries(SORTS).map(([v,l])=>`<option value="${v}">${l}</option>`).join('');
render();load();
</script></body></html>"""

if __name__ == "__main__":
    main()
