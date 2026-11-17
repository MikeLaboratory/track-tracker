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

# Versione dell'app (MAJOR.MINOR.PATCH): è l'unico posto dove si scrive.
# La legge anche Scripts/build-app.sh per l'app Mac; per cambiarla usa Scripts/release.sh.
__version__ = "1.1.0"

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
# tipo di traccia: un edit (o bootleg) di solito si pubblica da sé, non lo firma una label
TRACK_KINDS = {"original", "edit", "remix"}
KIND_RULES = [(re.compile(r"(?i)edit|bootleg|flip"), "edit"), (re.compile(r"(?i)remix|rmx"), "remix")]
RELEASE_KINDS = {"", "label", "self"}  # non ancora / con una label / pubblicata da me
PLATFORMS = {"soundcloud", "bandcamp", "youtube", "altro"}

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


# ---------------------------------------------------------------- dati promo (invii e contatti)
# In un file a parte perché contiene dati personali di altre persone (email, profili).

PROMO_FILE = ROOT / "_tracker_promo.json"
CONTACT_TYPES = {"label", "ar", "dj", "promoter", "altro"}
CHANNELS = {"email", "instagram", "soundcloud", "website", "altro"}
# attesa → in attesa di risposta; "da ricontattare" non è uno stato salvato:
# è un invio in attesa con la data di follow-up già passata.
SEND_STATUSES = {"attesa", "interessati", "supportata", "firmata", "rifiutata", "nessuna"}
# demo: proposta a una label o a un A&R; promo: anteprima a un DJ perché la suoni
SEND_KINDS = {"demo", "promo"}
DEFAULT_TEMPLATES = {
    "email_subject": "Demo: {artista} – {titolo}",
    "email_body": "Ciao {nome},\n\nsono {artista}: ti mando {titolo}, una traccia a {bpm} BPM che secondo me "
                  "starebbe bene nel vostro catalogo.\n\nAscolto privato: {link}\n\nGrazie dell'ascolto!\n{artista}",
    "dm_body": "Ciao {nome}! Sono {artista}, ti va di ascoltare la mia nuova traccia {titolo}? {link}",
    "followup_body": "Ciao {nome}, ti riscrivo per sapere se hai avuto modo di ascoltare {titolo}: {link}\nGrazie!",
    "promo_subject": "Promo: {artista} – {titolo}",
    "promo_body": "Ciao {nome}! Ti mando in anteprima {titolo}{uscita}. Se ti piace e la suoni fammelo sapere, mi fa un sacco piacere!\n\n{link}\n\n{artista}",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_promo():
    try:
        p = json.loads(PROMO_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        p = {}
    p.setdefault("contacts", {})
    p.setdefault("sends", {})
    s = p.setdefault("settings", {})
    s.setdefault("followup_days", 14)
    t = s.setdefault("templates", {})
    for k, v in DEFAULT_TEMPLATES.items():
        t.setdefault(k, v)
    return p


def save_promo(p):
    tmp = PROMO_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, PROMO_FILE)


PROMO = load_promo()


# ---------------------------------------------------------------- scansione

def guess_kind(name):
    for pat, kind in KIND_RULES:
        if pat.search(name):
            return kind
    return "original"


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
    moved = {}  # vecchio id → nuovo id, per spostare anche gli invii
    orphans = {rid: e for rid, e in saved.items() if rid not in base}
    new = {rid: b for rid, b in base.items() if rid not in saved}
    if orphans and new:
        new_by_ino = _unique_by((rid, b["ino"]) for rid, b in new.items())
        for oid, e in list(orphans.items()):
            nid = new_by_ino.get(e.get("ino"))
            if nid in new:
                saved[nid] = saved.pop(oid)
                moved[oid] = nid
                del new[nid], orphans[oid]
                changed = True
        old_by_als = _unique_by((oid, e["als_name"]) for oid, e in orphans.items()
                                if e.get("als_name"))
        new_by_als = _unique_by((rid, b["als_name"]) for rid, b in new.items())
        for name, oid in old_by_als.items():
            if name in new_by_als:
                saved[new_by_als[name]] = saved.pop(oid)
                moved[oid] = new_by_als[name]
                changed = True
    if moved:
        for s in PROMO["sends"].values():
            if s.get("project") in moved:
                s["project"] = moved[s["project"]]
        save_promo(PROMO)
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
                link=saved.get("link", ""),
                kind=saved.get("kind") if saved.get("kind") in TRACK_KINDS else guess_kind(p["folder"]),
                kind_source="manual" if saved.get("kind") in TRACK_KINDS else "auto",
                # chi non ha mai scelto ma ha una label: uscita con label
                release_kind=saved.get("release_kind", "label" if saved.get("label") else ""),
                release_platform=saved.get("release_platform", "soundcloud"),
                release_link=saved.get("release_link", ""),
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
        "version": __version__,
        "is_mac": IS_MAC,
        "statuses": [{"key": k, "label": l} for k, l, _ in STATUSES],
        "settings": DATA["settings"],
        "projects": sorted(projects.values(), key=lambda p: -p["mtime"]),
        "promo": promo_payload(),
    }


def update_project(body):
    pid = body.get("id")
    projects = scan()
    if pid not in projects:
        raise ValueError("Progetto non trovato")
    # prima si controlla tutto, poi si cambia: un valore sbagliato non lascia modifiche a metà
    checks = (("kind", TRACK_KINDS | {""}, "Tipo di traccia non valido"),
              ("release_kind", RELEASE_KINDS, "Tipo di uscita non valido"),
              ("release_platform", PLATFORMS, "Piattaforma non valida"),
              ("status", STATUS_KEYS | {""}, "Stato non valido"))
    for field, allowed, message in checks:
        if field in body and (body[field] or "") not in allowed:
            raise ValueError(message)
    warning = None
    with LOCK:
        entry = DATA["projects"].setdefault(pid, {})
        for field in ("notes", "label", "release_date", "release_link"):
            if field in body:
                entry[field] = str(body[field])[:20000]
        for field in ("release_kind", "release_platform"):
            if field in body:
                entry[field] = body[field]
        # "" = torna al valore dedotto dal nome della cartella
        for field in ("kind", "status"):
            if field in body:
                if body[field]:
                    entry[field] = body[field]
                else:
                    entry.pop(field, None)
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


# ---------------------------------------------------------------- API promo

def _text(value, limit):
    return str(value or "").strip()[:limit]


def _date(value, field):
    value = _text(value, 10)
    if value and not DATE_RE.match(value):
        raise ValueError(f"Data non valida ({field})")
    return value


def _today():
    return time.strftime("%Y-%m-%d")


def promo_payload():
    with LOCK:
        return {
            "contacts": sorted(PROMO["contacts"].values(), key=lambda c: c["name"].lower()),
            "sends": sorted(PROMO["sends"].values(), key=lambda s: (s["date"], s["created"]), reverse=True),
            "settings": PROMO["settings"],
            "today": _today(),
        }


CONTACT_TEXT = {"name": 200, "org": 200, "email": 300, "instagram": 100, "soundcloud": 300,
                "website": 500, "notes": 20000}


def save_contact(body):
    """Crea (senza id) o aggiorna un contatto: label, A&R, DJ, promoter…"""
    upd = {f: _text(body[f], n) for f, n in CONTACT_TEXT.items() if f in body}
    if "name" in upd and not upd["name"]:
        raise ValueError("Il contatto deve avere un nome")
    if "instagram" in upd:
        upd["instagram"] = upd["instagram"].lstrip("@")
    if "type" in body:
        if body["type"] not in CONTACT_TYPES:
            raise ValueError("Tipo di contatto non valido")
        upd["type"] = body["type"]
    if "genres" in body:
        genres = body["genres"].split(",") if isinstance(body["genres"], str) else list(body["genres"])
        upd["genres"] = [str(g).strip()[:40] for g in genres if str(g).strip()][:12]
    with LOCK:
        cid = body.get("id")
        if cid:
            contact = PROMO["contacts"].get(cid)
            if not contact:
                raise ValueError("Contatto non trovato")
        else:
            if not upd.get("name"):
                raise ValueError("Scrivi il nome del contatto")
            cid = os.urandom(5).hex()
            contact = {"id": cid, "name": "", "type": "label", "org": "", "email": "", "instagram": "",
                       "soundcloud": "", "website": "", "genres": [], "notes": "", "created": time.time()}
            PROMO["contacts"][cid] = contact
        contact.update(upd)
        save_promo(PROMO)
        return {"contact": contact}


def delete_contact(body):
    with LOCK:
        cid = body.get("id")
        if cid not in PROMO["contacts"]:
            raise ValueError("Contatto non trovato")
        n = sum(1 for s in PROMO["sends"].values() if s["contact"] == cid)
        if n:
            raise ValueError(f"Ha {n} {'invio registrato' if n == 1 else 'invii registrati'}: "
                             "non lo elimino, così non perdi lo storico")
        del PROMO["contacts"][cid]
        save_promo(PROMO)
    return {"ok": True}


def save_send(body):
    """Registra (senza id) o aggiorna un invio di una traccia a un contatto.
    Quando un invio diventa "firmata", la traccia passa a Master e prende la label."""
    projects = scan()
    upd = {}
    if "kind" in body:
        if body["kind"] not in SEND_KINDS:
            raise ValueError("Tipo di invio non valido")
        upd["kind"] = body["kind"]
    if "channel" in body:
        if body["channel"] not in CHANNELS:
            raise ValueError("Canale non valido")
        upd["channel"] = body["channel"]
    if "status" in body:
        if body["status"] not in SEND_STATUSES:
            raise ValueError("Stato dell'invio non valido")
        upd["status"] = body["status"]
    for f in ("date", "followup", "reply_date", "exclusive_until"):
        if f in body:
            upd[f] = _date(body[f], f)
    for f, n in (("link", 1000), ("reply_note", 5000), ("notes", 20000)):
        if f in body:
            upd[f] = _text(body[f], n)
    signed = False
    with LOCK:
        if "contact" in body and body["contact"] not in PROMO["contacts"]:
            raise ValueError("Scegli a chi l'hai mandata")
        if "contact" in body:
            upd["contact"] = body["contact"]
        sid = body.get("id")
        if sid:
            send = PROMO["sends"].get(sid)
            if not send:
                raise ValueError("Invio non trovato")
            old_status = send["status"]
        else:
            pid = body.get("project")
            if pid not in projects:
                raise ValueError("Traccia non trovata")
            if "contact" not in upd:
                raise ValueError("Scegli a chi l'hai mandata")
            send = {"id": os.urandom(5).hex(), "project": pid, "title": projects[pid]["name"],
                    "contact": "", "kind": "demo", "channel": "email", "link": "", "date": _today(), "followup": "",
                    "status": "attesa", "reply_date": "", "reply_note": "", "notes": "",
                    "exclusive_until": "", "followups": [], "created": time.time()}
            old_status = None
        send.update(upd)
        send["date"] = send["date"] or _today()
        if body.get("add_followup"):  # l'hai ricontattato oggi
            send["followups"] = send["followups"] + [_today()]
        if send["status"] != "attesa" and old_status in (None, "attesa") and not send["reply_date"]:
            send["reply_date"] = _today()
        PROMO["sends"][send["id"]] = send
        # la voce del progetto serve anche a ritrovare gli invii se rinomini la cartella
        entry = DATA["projects"].setdefault(send["project"], {})
        if send["link"]:
            entry["link"] = send["link"]
        if send["status"] == "firmata" and old_status != "firmata":
            signed = True
            current = projects.get(send["project"])
            if current and current["status"] not in ("master", "rilasciata"):
                entry["status"] = "master"
            contact = PROMO["contacts"][send["contact"]]
            if contact["type"] == "label" and not entry.get("label"):
                entry["label"] = contact["name"]
        save_promo(PROMO)
        save_data(DATA)
    project = scan().get(send["project"])
    warning = None
    if signed and project and DATA["settings"].get("finder_tags"):
        try:
            apply_status_tag(project["path"], project["status"])
        except Exception as e:
            warning = f"Tag Finder non applicato: {e}"
    return {"send": send, "project": project, "signed": signed, "warning": warning}


def delete_send(body):
    with LOCK:
        if body.get("id") not in PROMO["sends"]:
            raise ValueError("Invio non trovato")
        del PROMO["sends"][body["id"]]
        save_promo(PROMO)
    return {"ok": True}


def update_promo_settings(body):
    with LOCK:
        s = PROMO["settings"]
        if "followup_days" in body:
            s["followup_days"] = max(1, min(365, int(body["followup_days"])))
        for key, value in dict(body.get("templates") or {}).items():
            if key in DEFAULT_TEMPLATES:
                s["templates"][key] = _text(value, 5000) or DEFAULT_TEMPLATES[key]
        save_promo(PROMO)
        return {"settings": s}


def open_link(body):
    """Apre una mail già scritta (mailto:) o una pagina web (https://) con l'app predefinita."""
    url = _text(body.get("url"), 8000)
    if not re.match(r"(?i)^(mailto:|https://)", url):
        raise ValueError("Link non supportato")
    if not IS_MAC:
        raise ValueError("Funziona solo su Mac")
    subprocess.run(["open", url], check=True)
    return {"ok": True}


ROUTES = {
    "/api/update": update_project,
    "/api/open": open_project,
    "/api/settings": update_settings,
    "/api/sync_tags": sync_tags,
    "/api/contact": save_contact,
    "/api/contact_delete": delete_contact,
    "/api/send": save_send,
    "/api/send_delete": delete_send,
    "/api/promo_settings": update_promo_settings,
    "/api/open_link": open_link,
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
            return self._send(200, {"app": "track-tracker", "version": __version__, "root": str(ROOT)})
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
    if "--version" in sys.argv[1:]:
        return print(f"Track Tracker {__version__}")
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
body.panel-open .search input{width:190px}
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

/* promo: invii e contatti */
body.mode-promo .tracks-only,body.mode-contacts .tracks-only{display:none}
body.mode-tracks .promo-only{display:none}
.nav.due .nc{color:var(--warn)}
.chan{display:inline-flex;align-items:center;gap:5px;color:var(--soft)}
.chan svg{width:13px;height:13px}
.st{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;font-weight:600;color:var(--c);background:#ffffff0a;border:1px solid #ffffff12;border-radius:999px;padding:1px 9px 1px 7px;white-space:nowrap}
.st::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--c)}
.who{display:flex;align-items:center;gap:8px;margin-top:6px;min-width:0}
.who b{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.who small{color:var(--mute);white-space:nowrap}
.avatar{width:22px;height:22px;border-radius:6px;display:grid;place-items:center;font-size:10px;font-weight:700;color:#fff;background:var(--a);flex:none}
.scard .cname{font-size:13px}
.excl{color:#d9b3f5}
.pill.sends-b{color:#6cb4ff}
.pill.sends-b.has-due{color:var(--warn);background:#ffb34017;border-color:#ffb34030}
.pill.never{color:var(--dim)}
.ptabs{display:flex;gap:4px;margin-top:16px;border-bottom:1px solid var(--line)}
.ptabs button{padding:7px 10px;color:var(--mute);border-bottom:2px solid transparent;margin-bottom:-1px;font-weight:500}
.ptabs button:hover{color:var(--text)}
.ptabs button.on{color:var(--text);border-bottom-color:var(--accent)}
.ptabs .n{margin-left:5px}
.note-box{display:flex;gap:9px;align-items:flex-start;margin-top:14px;background:#bf5af214;border:1px solid #bf5af238;color:#d9b3f5;border-radius:9px;padding:9px 11px;font-size:12px;line-height:1.45}
.note-box svg,.warnbox svg{margin-top:1px}
.sends{margin-top:12px}
.send{position:relative;padding:11px 8px 11px 22px;border-left:2px solid var(--line);margin-left:6px;cursor:pointer;border-radius:0 8px 8px 0}
.send:hover{background:#ffffff06}
.send::before{content:"";position:absolute;left:-6px;top:15px;width:10px;height:10px;border-radius:50%;background:var(--c);box-shadow:0 0 0 3px #18181b}
.send-top{display:flex;align-items:center;gap:8px}
.send-top b{flex:1;min-width:0;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.send-meta{display:flex;flex-wrap:wrap;gap:3px 12px;color:var(--mute);font-size:11.5px;margin-top:4px}
.send-note{margin-top:5px;color:#b3b3bb;font-size:12px;white-space:pre-line;word-break:break-word}
.send-act{display:flex;gap:6px;margin-top:8px}
.due{color:var(--warn)}
.btn.sm{padding:4px 9px;font-size:12px;border-radius:7px}
.btn.danger{color:#ff6961}
.btn:disabled{opacity:.45;cursor:default}
.modal.wide{width:min(640px,100%)}
.f2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.f3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
.seg2{display:flex;flex-wrap:wrap;gap:6px}
.seg2 button{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--raise);color:var(--soft)}
.seg2 button:hover{border-color:var(--line2)}
.seg2 button.on{border-color:var(--accent);color:#fff;box-shadow:inset 0 0 0 1px var(--accent)}
.seg2 svg{width:14px;height:14px}
textarea.msg{min-height:150px;font-size:12.5px}
textarea.short{min-height:64px}
.warnbox{display:flex;gap:9px;align-items:flex-start;margin-top:14px;background:#ffb34012;border:1px solid #ffb34030;color:var(--warn);border-radius:9px;padding:9px 11px;font-size:12px;line-height:1.45}
.m-foot2{display:flex;gap:8px;align-items:center;margin-top:20px;flex-wrap:wrap}
.m-foot2 .grow{flex:1}
.m-hint{margin-top:14px;font-size:11.5px;color:var(--dim)}
.genre{display:inline-block;font-size:11px;color:var(--soft);background:#ffffff0d;border:1px solid #ffffff10;border-radius:5px;padding:0 6px;margin:1px 3px 1px 0}
.icons{display:inline-flex;gap:7px;color:var(--mute)}
.icons svg{width:14px;height:14px}
.rate small{color:var(--dim)}
.cf{display:grid;grid-template-columns:1fr 1fr;gap:8px 10px}
.cf label,.tpl{display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--mute)}
.tpl{margin-top:10px}
.newc{margin-top:10px;padding:12px;border:1px dashed var(--line2);border-radius:9px}
select.field{height:34px}
.pill.kind{color:#e0b3ff;background:#bf5af214;border-color:#bf5af230}
.pill.promo-p{color:#7fd8ff;background:#64d2ff14;border-color:#64d2ff30}
body:not(.mode-promo) .promo-board-only{display:none}
.rlist{max-height:240px;overflow:auto;border:1px solid var(--line);border-radius:9px;background:var(--raise)}
.rrow{display:flex;align-items:center;gap:9px;padding:7px 10px;border-bottom:1px solid var(--line);cursor:pointer}
.rrow:last-child{border-bottom:0}
.rrow:hover{background:#ffffff06}
.rrow input{accent-color:var(--accent);margin:0}
.rname2{flex:1;min-width:0;display:flex;flex-direction:column}
.rname2 b{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rname2 small{color:var(--mute);font-size:11px}
.via{display:inline-flex;gap:5px;align-items:center;font-size:11.5px;color:var(--mute);white-space:nowrap}
.via svg{width:13px;height:13px}
.relrow{display:flex;gap:6px}
.picker{display:flex;align-items:center;gap:9px;background:var(--raise);border:1px solid var(--line);border-radius:8px;padding:6px 10px}

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
    <div class="brand" id="brand"><span class="logo"><svg viewBox="0 0 20 20"><path d="M3.5 9v2" stroke="#8e8e93"/><path d="M6.75 6.5v7" stroke="#0a84ff"/><path d="M10 3.5v13" stroke="#bf5af2"/><path d="M13.25 6v8" stroke="#ff9f0a"/><path d="M16.5 8.5v3" stroke="#30d158"/></svg></span>Track Tracker</div>
    <div id="sideStats"></div>
    <nav class="navs" id="nav"></nav>
    <div class="side-foot"><span data-i="folder"></span><span class="fn" id="folder"></span><button class="ib" id="settingsBtn" title="Impostazioni e scorciatoie" data-i="gear"></button></div>
  </aside>
  <section class="content">
    <header class="toolbar">
      <button class="ib side-btn" id="sideBtn" title="Mostra filtri" data-i="menu"></button>
      <div class="ttl"><h1 id="title">Tutte le tracce</h1><span id="count"></span></div>
      <label class="search"><span data-i="search"></span><input id="q" placeholder="Cerca tracce e note" autocomplete="off" spellcheck="false"><kbd id="qkey">⌘F</kbd></label>
      <select class="dd tracks-only" id="sort" title="Ordina"></select>
      <div class="seg tracks-only"><button data-view="board" data-i="board"> Board</button><button data-view="list" data-i="list"> Lista</button></div>
      <div class="seg promo-board-only" id="pkSeg"><button data-pk="all">Tutti</button><button data-pk="demo">Demo</button><button data-pk="promo">Promo</button></div>
      <button class="btn primary promo-only" id="tbAction"></button>
      <button class="ib" id="reload" title="Rileggi la cartella" data-i="refresh"></button>
    </header>
    <main id="main"></main>
  </section>
</div>
<aside class="panel" id="panel"></aside>
<div class="menu" id="menu"></div>
<div class="overlay" id="ovs"><div class="modal wide" id="smodal"></div></div>
<div class="overlay" id="ovSend"><div class="modal wide" id="sendModal"></div></div>
<div class="overlay" id="ovC"><div class="modal" id="cModal"></div></div>
<div class="overlay" id="ovQ"><div class="modal" id="qModal"></div></div>
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
 send:'<svg viewBox="0 0 16 16"><path d="M2 8.5 14 3l-3.5 11-2.6-4.4z"/><path d="m7.9 9.6 2.6-2.6"/></svg>',
 mail:'<svg viewBox="0 0 16 16"><rect x="1.75" y="3.25" width="12.5" height="9.5" rx="1.5"/><path d="m2.25 4 5.75 4.5L13.75 4"/></svg>',
 ig:'<svg viewBox="0 0 16 16"><rect x="2" y="2" width="12" height="12" rx="3.5"/><circle cx="8" cy="8" r="2.8"/><circle cx="11.4" cy="4.6" r=".6" fill="currentColor"/></svg>',
 cloud:'<svg viewBox="0 0 16 16"><path d="M4.5 12.5h7.25a2.75 2.75 0 0 0 .3-5.48 4 4 0 0 0-7.7-.8A2.9 2.9 0 0 0 4.5 12.5z"/></svg>',
 globe:'<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c1.8 1.7 2.6 3.7 2.6 6s-.8 4.3-2.6 6c-1.8-1.7-2.6-3.7-2.6-6S6.2 3.7 8 2z"/></svg>',
 book:'<svg viewBox="0 0 16 16"><rect x="2.5" y="2" width="11" height="12" rx="2"/><circle cx="8" cy="6.75" r="1.9"/><path d="M5 11.5c.6-1.4 1.7-2.1 3-2.1s2.4.7 3 2.1"/></svg>',
 plus:'<svg viewBox="0 0 16 16"><path d="M8 3v10M3 8h10"/></svg>',
 warn:'<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="M8 4.5v4.25"/><circle cx="8" cy="11" r=".6" fill="currentColor"/></svg>',
 edit:'<svg viewBox="0 0 16 16"><path d="M10.5 2.5l3 3L6 13H3v-3z"/></svg>',
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
  openId:null,menuId:null,pendingNotes:null,notesT:null,collapsed:storedSet('collapsed'),expanded:storedSet('expanded'),
  // promo: mode = tracks | promo | contacts
  mode:'tracks',promoFilter:'all',promoKind:'all',ptab:'details',openContact:null,compose:null,
  promo:{contacts:[],sends:[],settings:{followup_days:14,templates:{}},today:''}};
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
const KINDS={original:'Originale',edit:'Edit',remix:'Remix'};
const PLAT={soundcloud:'SoundCloud',bandcamp:'Bandcamp',youtube:'YouTube',altro:'online'};
function releaseText(p){
  const parts=[];
  if(p.release_date)parts.push((new Date(p.release_date+'T00:00:00')>new Date()?'Esce il ':'Uscita il ')+fmtDate(p.release_date));
  if(p.release_kind==='self')parts.push((parts.length?'':'Pubblicata ')+'da me su '+(PLAT[p.release_platform]||'online'));
  else if(p.label)parts.push(p.label);
  return parts.join(' · ');
}

// ------------------------------------------------------------ promo: dati e testi
const CH={email:{l:'Email',i:'mail'},instagram:{l:'DM Instagram',i:'ig'},soundcloud:{l:'SoundCloud',i:'cloud'},website:{l:'Sito demo',i:'globe'},altro:{l:'Altro',i:'send'}};
const SST={attesa:{l:'In attesa',c:'#0a84ff'},interessati:{l:'Interessati',c:'#bf5af2'},supportata:{l:'Supportata',c:'#64d2ff'},
  firmata:{l:'Firmata',c:'#30d158'},rifiutata:{l:'Rifiutata',c:'#ff453a'},nessuna:{l:'Nessuna risposta',c:'#8e8e93'}};
const DUE_C='#ffb340';
const CTYPES={label:'Label',ar:'A&R',dj:'DJ',promoter:'Promoter',altro:'Altro'};
const PCOLS=[['due','Da ricontattare',DUE_C],['attesa','In attesa','#0a84ff'],['interessati','Interessati o supporto','#bf5af2'],
  ['firmata','Firmate','#30d158'],['chiuse','Rifiutate o senza risposta','#ff453a']];
const ANSWERED=['interessati','supportata','firmata','rifiutata'];
const contactById=id=>S.promo.contacts.find(c=>c.id===id);
const sendsOf=pid=>S.promo.sends.filter(s=>s.project===pid);
const isDue=s=>s.status==='attesa'&&!!s.followup&&s.followup<=S.promo.today;
const sendCol=s=>isDue(s)?'due':s.status==='attesa'?'attesa':(s.status==='interessati'||s.status==='supportata')?'interessati':s.status==='firmata'?'firmata':'chiuse';
const sendColor=s=>isDue(s)?DUE_C:(SST[s.status]||SST.attesa).c;
const sendLabel=s=>isDue(s)?'Da ricontattare':s.kind==='promo'&&s.status==='attesa'?'Inviata':(SST[s.status]||SST.attesa).l;
const AV=['#0e7490','#b45309','#5e5ce6','#7c3aed','#be185d','#c2410c','#15803d','#475569','#0369a1','#a21caf'];
function avatar(c){
  const n=(c&&c.name)||'?';let h=0;for(const ch of n)h=(h*31+ch.charCodeAt(0))>>>0;
  const ini=n.split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0]).join('').toUpperCase()||'?';
  return `<span class="avatar" style="--a:${AV[h%AV.length]}">${esc(ini)}</span>`;
}
const pad=n=>String(n).padStart(2,'0');
const isoLocal=d=>`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
function addDays(s,n){const d=new Date(s+'T00:00:00');d.setDate(d.getDate()+n);return isoLocal(d)}
function dayDiff(a,b){return Math.round((new Date(b+'T00:00:00')-new Date(a+'T00:00:00'))/864e5)}
const agoDate=s=>s?ago(dayDiff(s,S.promo.today)):'';
function shortDate(s){const d=new Date(s+'T00:00:00');return isNaN(d)?s:d.toLocaleDateString('it-IT',{day:'numeric',month:'short'})}
// "il 30 nov" ma "l'11 nov", "l'1", "l'8"; "al 30" ma "all'11"
const vowelDay=s=>[1,8,11].includes(new Date(s+'T00:00:00').getDate());
const ilDate=s=>(vowelDay(s)?"l'":'il ')+shortDate(s);
const alDate=s=>(vowelDay(s)?"all'":'al ')+shortDate(s);
function inDays(s){const n=dayDiff(S.promo.today,s);return n<=0?'oggi':n===1?'domani':'tra '+n+' giorni'}
function dueText(s){const n=dayDiff(s.followup,S.promo.today);return n<=0?'Follow-up oggi':'Follow-up scaduto da '+n+(n===1?' giorno':' giorni')}
const contactSub=c=>[CTYPES[c.type]||'',c.org].filter(Boolean).join(' · ');
const chAddr=(c,ch)=>!c?'':ch==='email'?c.email:ch==='instagram'?c.instagram:ch==='soundcloud'?c.soundcloud:ch==='website'?c.website:'';
// invii ancora aperti con un'esclusiva valida (escluso un contatto, se serve)
const exclusivesFor=(pid,except)=>sendsOf(pid).filter(s=>s.exclusive_until&&s.exclusive_until>=S.promo.today
  &&(s.status==='attesa'||s.status==='interessati')&&s.contact!==except);
const fill=(t,ctx)=>String(t||'').replace(/\{(\w+)\}/g,(m,k)=>k in ctx?String(ctx[k]):m);
function copyText(t){
  // prima la copia "classica" (funziona dentro il clic), poi l'API moderna
  let ok=false;const ta=document.createElement('textarea');ta.value=t;ta.setAttribute('readonly','');
  ta.style.cssText='position:fixed;top:-1000px;opacity:0';document.body.appendChild(ta);ta.select();
  try{ok=document.execCommand('copy')}catch(e){}ta.remove();
  if(ok)return Promise.resolve(true);
  return navigator.clipboard?navigator.clipboard.writeText(t).then(()=>true,()=>false):Promise.resolve(false);
}
async function openLink(url){
  try{await api('/api/open_link',{url})}
  catch(e){if(/solo su Mac/.test(e.message))window.open(url,'_blank');else throw e}
}

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
    Object.assign(S,{projects:d.projects,statuses:d.statuses,settings:d.settings,root:d.root,version:d.version,promo:d.promo,loaded:true,error:''});
    $('#brand').title='Track Tracker '+d.version;
  }catch(e){S.error=e.message;S.loaded=true}
  S.aliasN=new Set(S.projects.map(p=>p.alias)).size;
  if(S.alias!=='__all'&&!S.projects.some(p=>p.alias===S.alias))S.alias='__all';
  render();
  if(S.openId||S.openContact){(S.openId?byId(S.openId):contactById(S.openContact))?renderPanel():closePanel()}
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
function render(){
  document.body.classList.remove('mode-tracks','mode-promo','mode-contacts');
  document.body.classList.add('mode-'+S.mode);
  renderSide();renderToolbar();renderMain();
}
function setMode(mode,pf){
  if(S.mode!==mode)closePanel();
  S.mode=mode;S.promoFilter=pf||'all';render();
}

function renderSide(){
  const inA=S.projects.filter(p=>S.alias==='__all'||p.alias===S.alias);
  const cnt={};inA.forEach(p=>cnt[p.status]=(cnt[p.status]||0)+1);
  const done=inA.filter(p=>DONE.includes(p.status)).length;
  $('#sideStats').innerHTML=inA.length?`<div class="stackbar">${S.statuses.filter(s=>cnt[s.key]).map(s=>
    `<span style="--c:var(--${s.key});flex:${cnt[s.key]}" title="${esc(s.label)}: ${cnt[s.key]}"></span>`).join('')}</div>
    <div class="stackinfo">${done} di ${inA.length} finite o rilasciate</div>`:'';
  const nav=(attr,icon,text,n,on,cls='')=>`<button class="nav${on?' on':''} ${cls}" ${attr}>${icon}<span class="nt">${esc(text)}</span><span class="nc">${n}</span></button>`;
  const tr=S.mode==='tracks',sends=S.promo.sends,due=sends.filter(isDue).length;
  let h='<div class="sec">Libreria</div>'
    +nav('data-filter="all"',ICON.wave,'Tutte le tracce',inA.length,tr&&S.filter==='all')
    +nav('data-filter="stale"',ICON.clock,'Ferme',inA.filter(isStale).length,tr&&S.filter==='stale','stale')
    +'<div class="sec">Promo</div>'
    +nav('data-mode="promo" data-pf="all"',ICON.send,'Invii',sends.length,S.mode==='promo'&&S.promoFilter==='all')
    +nav('data-mode="promo" data-pf="due"',ICON.refresh,'Da ricontattare',due,S.mode==='promo'&&S.promoFilter==='due',due?'stale due':'')
    +nav('data-mode="contacts"',ICON.book,'Contatti',S.promo.contacts.length,S.mode==='contacts')
    +'<div class="sec">Stati</div>'
    +S.statuses.map(s=>nav(`data-filter="${s.key}"`,`<span class="dot" style="--c:var(--${s.key})"></span>`,s.label,cnt[s.key]||0,tr&&S.filter===s.key)).join('');
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
  const al=S.alias!=='__all'?' · '+S.alias:'',tb=$('#tbAction'),q=$('#q');
  if(S.mode==='promo'){
    const l=promoSends(),tr=new Set(l.map(s=>s.project)).size;
    $('#title').textContent=(S.promoFilter==='due'?'Da ricontattare':'Invii')+al;
    $('#count').textContent=`${l.length} ${l.length===1?'invio':'invii'} · ${tr} ${tr===1?'traccia':'tracce'}`;
    tb.innerHTML=ICON.send+'Nuovo invio';tb.dataset.action='send';q.placeholder='Cerca tracce, label, persone';
    document.querySelectorAll('[data-pk]').forEach(b=>b.classList.toggle('on',b.dataset.pk===S.promoKind));
    return;
  }
  if(S.mode==='contacts'){
    const n=contactList().length;
    $('#title').textContent='Contatti';$('#count').textContent=n+(n===1?' contatto':' contatti');
    tb.innerHTML=ICON.plus+'Nuovo contatto';tb.dataset.action='contact';q.placeholder='Cerca contatti e generi';
    return;
  }
  q.placeholder='Cerca tracce e note';
  const t=S.filter==='all'?'Tutte le tracce':S.filter==='stale'?'Tracce ferme':label(S.filter);
  $('#title').textContent=t+al;
  const n=filtered().length;
  $('#count').textContent=S.loaded?n+(n===1?' traccia':' tracce'):'';
  document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('on',b.dataset.view===S.view));
  $('#sort').value=SORTS[S.sort]?S.sort:'';
}

function renderMain(){
  const m=$('#main');m.className=S.mode==='promo'?'board':S.mode==='contacts'?'list':S.view;
  if(!S.loaded){m.innerHTML='<div class="empty"><div class="spinner"></div>Carico i progetti…</div>';return}
  if(S.error){m.innerHTML=`<div class="empty"><div class="big">Non riesco a leggere i progetti</div><div class="small">${esc(S.error)}</div><button class="btn" id="retry">Riprova</button></div>`;return}
  if(S.mode==='promo')return renderPromo();
  if(S.mode==='contacts')return renderContacts();
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
  if(p.kind!=='original')meta.push(`<span class="pill kind">${KINDS[p.kind]}</span>`);
  if(p.bpm)meta.push(`<span class="bpm">${p.bpm} BPM</span>`);
  meta.push(`<span>${ago(p.days)}</span>`);
  if(p.versions>1)meta.push(`<span title="File .als nella cartella">${p.versions} versioni</span>`);
  if(p.status_source==='auto')meta.push('<span class="auto" title="Stato dedotto dal nome della cartella">auto</span>');
  const rel=releaseText(p),ss=sendsOf(p.id),dn=ss.filter(isDue).length;
  const promo=ss.length?`<div class="meta"><span class="pill sends-b${dn?' has-due':''}">${ICON.send}${ss.length} ${ss.length===1?'invio':'invii'}${dn?` · ${dn} da ricontattare`:''}</span></div>`
    :p.status==='finita'?'<div class="meta"><span class="pill never">Mai inviata</span></div>':'';
  return `<article class="card${p.id===S.openId?' is-sel':''}" draggable="true" data-id="${esc(p.id)}" style="--c:var(--${p.status})">
    <div class="c-top"><div class="cname">${esc(p.name)}</div><div class="c-act">
      <button class="ib sm" data-act="play" title="Apri in Ableton">${ICON.play}</button>
      <button class="ib sm" data-act="more" title="Altre azioni">${ICON.more}</button></div></div>
    <div class="meta">${meta.join('')}</div>
    ${isStale(p)?`<div class="meta"><span class="pill stale-b">${ICON.clock}Ferma da ${p.days} giorni</span></div>`:''}
    ${promo}
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
  const el=$('#panel'),a=document.activeElement;
  const keep=a&&el.contains(a)&&a.id?{id:a.id,v:a.value,s:a.selectionStart,e:a.selectionEnd}:null;
  if(S.openContact){const c=contactById(S.openContact);if(!c)return closePanel();renderContactPanel(el,c)}
  else{const p=byId(S.openId);if(!p)return closePanel();renderTrackPanel(el,p)}
  if(keep){const n=document.getElementById(keep.id);if(n){n.value=keep.v;n.focus();try{n.setSelectionRange(keep.s,keep.e)}catch(e){}}}
}

function renderTrackPanel(el,p){
  const ta=$('#notes'),notes=ta&&ta.dataset.pid===p.id?ta.value:p.notes;
  const ss=sendsOf(p.id),sendsTab=S.ptab==='sends';
  const facts=[['BPM',p.bpm||'–'],['Modificata',ago(p.days)],['Versioni .als',p.versions]];
  const head=`<div class="p-head"><div><div class="p-alias">${esc(p.alias)}</div><h2>${esc(p.name)}</h2></div>
      <button class="ib" data-close title="Chiudi (Esc)">${ICON.close}</button></div>
    <button class="p-path" data-act="finder" title="Mostra nel Finder">${ICON.folder}<span>${esc(p.path)}</span></button>
    <div class="facts">${facts.map(([k,v])=>`<div class="fact">${k}<b>${esc(v)}</b></div>`).join('')}</div>
    ${isStale(p)?`<div class="warn">${ICON.clock}Non la tocchi da ${p.days} giorni</div>`:''}
    <div class="ptabs"><button data-ptab="details" class="${sendsTab?'':'on'}">Dettagli</button>
      <button data-ptab="sends" class="${sendsTab?'on':''}">Invii<span class="n">${ss.length}</span></button></div>`;
  if(sendsTab){
    el.innerHTML=`<div class="p-scroll">${head}${trackSendsHtml(p,ss)}</div>
    <div class="p-foot"><button class="btn primary" data-new-send="${esc(p.id)}">${ICON.send}Nuovo invio</button>
      <button class="btn" data-new-promo="${esc(p.id)}">${ICON.wave}Promo ai DJ</button>
      <div class="p-keys">↑↓ tracce<br>Esc chiudi</div></div>`;
    return;
  }
  el.innerHTML=`<div class="p-scroll">${head}
    <div class="lbl">Stato<span class="hint">tasti 1–${S.statuses.length}</span></div>
    <div class="stgrid">${S.statuses.map((s,i)=>`<button class="stb${p.status===s.key?' on':''}" data-st="${s.key}" style="--c:var(--${s.key})"><span class="dot"></span>${esc(s.label)}<span class="k">${i+1}</span></button>`).join('')}</div>
    ${p.status_source==='auto'?'<div class="autonote">Stato dedotto dal nome della cartella: scegline uno per confermarlo.</div>':''}
    <div class="lbl">Tipo di traccia${p.kind_source==='auto'?'<span class="hint">dedotto dal nome</span>':''}</div>
    <div class="seg2">${Object.entries(KINDS).map(([k,l])=>`<button data-kind="${k}" class="${p.kind===k?'on':''}">${l}</button>`).join('')}</div>
    <div class="lbl">Uscita</div>
    <div class="seg2">${[['','Non ancora'],['label','Con una label'],['self','Pubblicata da me']].map(([k,l])=>`<button data-rk="${k}" class="${p.release_kind===k?'on':''}">${l}</button>`).join('')}</div>
    ${p.release_kind==='label'?`<div class="row2"><div><div class="lbl">Label</div><input class="field" id="label" value="${esc(p.label)}" placeholder="Nome della label"></div>
      <div><div class="lbl">Data di uscita</div><input class="field" id="rdate" type="${p.release_date?'date':'text'}" placeholder="Nessuna data" value="${esc(p.release_date)}"></div></div>`
    :p.release_kind==='self'?`<div class="row2"><div><div class="lbl">Dove</div><select class="field" id="rplat">${Object.entries(PLAT).map(([k,l])=>`<option value="${k}"${p.release_platform===k?' selected':''}>${k==='altro'?'Altro':l}</option>`).join('')}</select></div>
      <div><div class="lbl">Pubblicata il</div><input class="field" id="rdate" type="${p.release_date?'date':'text'}" placeholder="Nessuna data" value="${esc(p.release_date)}"></div></div>
      <div class="lbl">Link alla traccia pubblicata</div>
      <div class="relrow"><input class="field" id="rlink" value="${esc(p.release_link)}" placeholder="https://soundcloud.com/…"><button class="btn" data-open-rlink title="Apri il link">${ICON.globe}Apri</button></div>
      ${p.status!=='rilasciata'?`<button class="btn primary" data-publish style="margin-top:10px">${ICON.send}Segna come pubblicata${p.release_date?'':' oggi'}</button>`:''}`
    :`<div class="autonote">${p.kind==='edit'?'Gli edit di solito si pubblicano da sé (per esempio su SoundCloud) o si mandano come promo ai DJ.':'Quando esce, scegli se con una label o pubblicata da te.'}</div>`}
    <div class="lbl">Note e cose da fare<span class="saved" id="saved">Salvato ✓</span></div>
    <textarea id="notes" data-pid="${esc(p.id)}" placeholder="Rifare il drop, kick troppo forte, mandare a…">${esc(notes)}</textarea>
  </div>
  <div class="p-foot"><button class="btn primary" data-act="play">${ICON.play}Apri in Ableton</button>
    <button class="btn" data-act="finder">${ICON.folder}Finder</button>
    <div class="p-keys">${CMD}↵ apri · ↑↓ tracce<br>Esc chiudi</div></div>`;
  $('#notes').addEventListener('input',e=>{S.pendingNotes={id:p.id,v:e.target.value};clearTimeout(S.notesT);S.notesT=setTimeout(flushNotes,700)});
  const lb=$('#label');if(lb)lb.addEventListener('change',e=>update(p.id,{label:e.target.value}));
  const rp=$('#rplat');if(rp)rp.addEventListener('change',e=>update(p.id,{release_platform:e.target.value}));
  const rl=$('#rlink');if(rl)rl.addEventListener('change',e=>update(p.id,{release_link:e.target.value.trim()}));
  // data vuota: campo di testo "Nessuna data" (WebKit mostrerebbe la data di oggi come se fosse impostata)
  const rd=$('#rdate');if(!rd)return;
  rd.addEventListener('focus',()=>{if(rd.type!=='date'){rd.type='date';try{rd.showPicker()}catch(e){}}});
  rd.addEventListener('blur',()=>{if(!rd.value)rd.type='text'});
  rd.addEventListener('change',e=>update(p.id,{release_date:e.target.value}));
}

function trackSendsHtml(p,ss){
  const ex=exclusivesFor(p.id).map(s=>`<div class="note-box">${ICON.info}<div>In esclusiva a <b>${esc((contactById(s.contact)||{}).name||'?')}</b>
    fino ${alDate(s.exclusive_until)}: se la mandi ad altri prima di quella data te lo ricordo.</div></div>`).join('');
  if(!ss.length)return ex+`<div class="empty" style="min-height:200px"><div class="big">Non l'hai ancora mandata a nessuno</div>
    Quando la mandi a una label, a un A&amp;R o a un DJ, registralo qui: tieni lo storico e ti ricordo i follow-up.</div>`;
  return ex+'<div class="sends">'+ss.map(s=>sendRow(s,'contact')).join('')+'</div>';
}

// una riga dello storico: nel pannello di una traccia mostra il contatto, in quello di un contatto la traccia
function sendRow(s,show){
  const c=contactById(s.contact)||{name:'Contatto eliminato',type:'altro'},ch=CH[s.channel]||CH.altro,p=byId(s.project);
  const addr=chAddr(c,s.channel),col=sendColor(s);
  const meta=[`${s.kind==='promo'?'<span class="pill promo-p">Promo</span>':''}<span class="chan">${ICON[ch.i]}${esc(ch.l)}${show==='contact'&&addr?' · '+esc(addr):''}</span>`,
    `<span>${shortDate(s.date)}${s.reply_date?' → risposta '+shortDate(s.reply_date):''}</span>`];
  if(isDue(s))meta.push(`<span class="due">${esc(dueText(s).toLowerCase())}</span>`);
  else if(s.status==='attesa'&&s.followup)meta.push(`<span>follow-up ${inDays(s.followup)}</span>`);
  if(s.followups&&s.followups.length)meta.push(`<span>${s.followups.length===1?'1 follow-up fatto':s.followups.length+' follow-up fatti'}</span>`);
  const note=[s.reply_note?'«'+s.reply_note+'»':'',s.notes].filter(Boolean).join('\n');
  const act=s.status==='attesa'?`<div class="send-act"><button class="btn sm" data-sact="followup" data-sid="${s.id}">${ICON.refresh}Ricontatta</button>
    <button class="btn sm" data-sact="edit" data-sid="${s.id}">Segna risposta</button></div>`:'';
  return `<div class="send" data-sedit="${s.id}" style="--c:${col}" title="Modifica l'invio">
    <div class="send-top"><b>${esc(show==='contact'?c.name:(p?p.name:s.title))}</b><span class="st" style="--c:${col}">${esc(sendLabel(s))}</span></div>
    <div class="send-meta">${meta.join('')}</div>${note?`<div class="send-note">${esc(note)}</div>`:''}${act}</div>`;
}

function renderContactPanel(el,c){
  const ss=S.promo.sends.filter(s=>s.contact===c.id),ans=ss.filter(s=>ANSWERED.includes(s.status));
  const times=ans.filter(s=>s.reply_date).map(s=>dayDiff(s.date,s.reply_date)).filter(n=>n>=0);
  const facts=[['Invii',ss.length],['Risposte',ss.length?Math.round(ans.length/ss.length*100)+'%':'–'],
    ['Tempo medio',times.length?Math.round(times.reduce((a,b)=>a+b,0)/times.length)+' giorni':'–']];
  const f=(k,l,ph)=>`<label>${l}<input class="field" id="cf-${k}" data-cf="${k}" value="${esc(k==='genres'?(c.genres||[]).join(', '):c[k])}" placeholder="${esc(ph)}"></label>`;
  el.innerHTML=`<div class="p-scroll">
    <div class="p-head"><div><div class="p-alias">${esc(contactSub(c)||'Contatto')}</div><h2>${esc(c.name)}</h2></div>
      <button class="ib" data-close title="Chiudi (Esc)">${ICON.close}</button></div>
    <div class="facts">${facts.map(([k,v])=>`<div class="fact">${k}<b>${esc(v)}</b></div>`).join('')}</div>
    <div class="lbl">Dati<span class="saved" id="saved">Salvato ✓</span></div>
    <div class="cf">${f('name','Nome','')}
      <label>Tipo<select class="field" id="cf-type" data-cf="type">${Object.entries(CTYPES).map(([k,l])=>`<option value="${k}"${c.type===k?' selected':''}>${l}</option>`).join('')}</select></label>
      ${f('org','Label o agenzia','per un A&R o un promoter')}${f('genres','Generi','Deep House, Melodic')}
      ${f('email','Email','demo@label.com')}${f('instagram','Instagram','nomeutente')}
      ${f('soundcloud','SoundCloud','https://soundcloud.com/…')}${f('website','Pagina per le demo','https://…')}</div>
    <div class="lbl">Note</div>
    <textarea id="cf-notes" data-cf="notes" class="short" placeholder="Vogliono solo link privati, rispondono in 3 settimane…">${esc(c.notes)}</textarea>
    <div class="lbl">Storico</div>
    ${ss.length?'<div class="sends">'+ss.map(s=>sendRow(s,'track')).join('')+'</div>':'<div class="autonote">Ancora nessun invio a questo contatto.</div>'}
  </div>
  <div class="p-foot"><button class="btn primary" data-new-send-c="${c.id}">${ICON.send}Nuovo invio</button>
    <span class="grow" style="flex:1"></span>
    <button class="btn danger" data-del-contact="${c.id}" ${ss.length?'disabled title="Ha invii registrati: lo tengo per non perdere lo storico"':''}>Elimina contatto</button></div>`;
  el.querySelectorAll('[data-cf]').forEach(i=>i.addEventListener('change',()=>saveContactField(c.id,i.dataset.cf,i.value)));
}
async function saveContactField(id,k,v){
  try{
    const d=await api('/api/contact',{id,[k]:v});
    const i=S.promo.contacts.findIndex(c=>c.id===id);if(i>=0)S.promo.contacts[i]=d.contact;
    renderSide();renderToolbar();renderMain();
    if(['name','type','org'].includes(k))renderPanel();
    else{const s=$('#saved');if(s){s.classList.add('show');setTimeout(()=>s.classList.remove('show'),1300)}}
  }catch(e){toast('Errore: '+e.message);renderPanel()}
}
function flushNotes(){clearTimeout(S.notesT);const n=S.pendingNotes;S.pendingNotes=null;if(n)update(n.id,{notes:n.v})}
function openPanel(id,tab){
  if(S.openId!==id){flushNotes();S.ptab=tab||'details'}else if(tab)S.ptab=tab;
  S.openContact=null;S.openId=id;renderPanel();
  $('#panel').classList.add('open');document.body.classList.add('panel-open');markSel(true);
}
function openContact(cid){
  flushNotes();S.openId=null;S.openContact=cid;renderPanel();
  $('#panel').classList.add('open');document.body.classList.add('panel-open');markSel(true);
}
function closePanel(){
  flushNotes();S.openId=null;S.openContact=null;
  $('#panel').classList.remove('open');document.body.classList.remove('panel-open');markSel();
}
function markSel(scroll){
  document.querySelectorAll('#main [data-id],#main [data-cid]').forEach(e=>{
    const on=e.dataset.id?e.dataset.id===S.openId:e.dataset.cid===S.openContact;e.classList.toggle('is-sel',on);
    if(on&&scroll)setTimeout(()=>e.scrollIntoView({block:'nearest',inline:'nearest',behavior:'smooth'}),60);
  });
}
function move(delta){
  const l=visibleOrder();if(!l.length)return;
  let i=l.findIndex(p=>p.id===S.openId);
  i=i<0?0:Math.max(0,Math.min(l.length-1,i+delta));
  openPanel(l[i].id,S.ptab);
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
    <button data-m="details">${ICON.info}Dettagli e note</button><button data-m="send">${ICON.send}Nuovo invio</button><button data-m="promo">${ICON.wave}Promo ai DJ</button>
    <div class="sep"></div><div class="mh">Sposta in</div>`
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
  else if(a==='send')openCompose({pid:id,fixedTrack:true});
  else if(a==='promo')openCompose({pid:id,fixedTrack:true,kind:'promo'});
}

function openSettings(){
  const s=S.settings;
  $('#smodal').innerHTML=`<h2>Impostazioni</h2><div class="small">Track Tracker ${esc(S.version)} · Cartella: ${esc(S.root)}</div>
    <div class="lbl">Tracce ferme</div>
    <div class="inline">Dopo <input class="field" type="number" min="1" id="sdays" value="${s.stale_days}" style="width:80px"> giorni senza modifiche</div>
    <div class="lbl">Tag del Finder</div>
    <label class="check"><input type="checkbox" id="stags" ${s.finder_tags?'checked':''} ${MAC?'':'disabled'}>Quando cambio stato, metti il tag colorato alla cartella del progetto</label>
    <button class="btn" id="syncTags" style="margin-top:12px" ${MAC?'':'disabled'}>Applica i tag a tutte le tracce</button>
    <div class="lbl">Promo</div>
    <div class="inline">Ricontatta dopo <input class="field" type="number" min="1" id="pdays" value="${S.promo.settings.followup_days}" style="width:80px"> giorni senza risposta</div>
    <div class="lbl">Modelli dei messaggi<span class="hint">{nome} {artista} {titolo} {bpm} {link}</span></div>
    ${[['email_subject','Oggetto della mail',0],['email_body','Mail per label e pagine demo',1],['dm_body','Messaggio breve (DM, SoundCloud)',1],['followup_body','Follow-up',1],['promo_subject','Oggetto della promo ai DJ',0],['promo_body','Promo ai DJ ({uscita} = «, esce il…»)',1]]
      .map(([k,l,big])=>`<label class="tpl">${l}${big?`<textarea class="msg short" data-tpl="${k}">${esc(S.promo.settings.templates[k]||'')}</textarea>`
        :`<input class="field" data-tpl="${k}" value="${esc(S.promo.settings.templates[k]||'')}">`}</label>`).join('')}
    <div class="lbl">Scorciatoie</div>
    <div class="keys"><span><kbd>${CMD}F</kbd> <kbd>/</kbd></span>Cerca<span><kbd>↑</kbd> <kbd>↓</kbd></span>Scorri le tracce e apri i dettagli
      <span><kbd>1</kbd>–<kbd>${S.statuses.length}</kbd></span>Cambia stato alla traccia aperta<span><kbd>${CMD}↵</kbd></span>Apri in Ableton
      <span><kbd>Esc</kbd></span>Chiudi pannello o ricerca<span>Tasto destro</span>Menu della traccia</div>
    <div class="m-foot"><button class="btn primary" id="closeS">Fatto</button></div>`;
  $('#sdays').addEventListener('change',async e=>{try{const d=await api('/api/settings',{stale_days:+e.target.value||60});S.settings=d.settings;render()}catch(err){toast(err.message)}});
  $('#stags').addEventListener('change',async e=>{try{const d=await api('/api/settings',{finder_tags:e.target.checked});S.settings=d.settings}catch(err){toast(err.message)}});
  $('#pdays').addEventListener('change',async e=>{try{const d=await api('/api/promo_settings',{followup_days:+e.target.value||14});S.promo.settings=d.settings}catch(err){toast(err.message)}});
  document.querySelectorAll('[data-tpl]').forEach(i=>i.addEventListener('change',async()=>{
    try{const d=await api('/api/promo_settings',{templates:{[i.dataset.tpl]:i.value}});S.promo.settings=d.settings;i.value=d.settings.templates[i.dataset.tpl];toast('Modello salvato')}
    catch(err){toast(err.message)}}));
  $('#syncTags').addEventListener('click',async()=>{try{const d=await api('/api/sync_tags',{});toast(`Tag applicati a ${d.ok} tracce`+(d.errors.length?` (${d.errors.length} errori)`:''))}catch(e){toast('Errore: '+e.message)}});
  $('#ovs').classList.add('show');
}
function closeSettings(){$('#ovs').classList.remove('show')}

// ------------------------------------------------------------ promo: board degli invii e rubrica
function promoSends(){
  const q=S.q.trim().toLowerCase();
  return S.promo.sends.filter(s=>{
    if(S.promoFilter==='due'&&!isDue(s))return false;
    if(S.promoKind!=='all'&&(s.kind||'demo')!==S.promoKind)return false;
    const p=byId(s.project),c=contactById(s.contact)||{};
    if(S.alias!=='__all'&&p&&p.alias!==S.alias)return false;
    return !q||[s.title,p&&p.name,c.name,c.org,s.notes,s.reply_note].join(' ').toLowerCase().includes(q);
  });
}
function renderPromo(){
  const m=$('#main');
  if(!S.promo.sends.length){m.innerHTML=`<div class="empty"><div class="big">Nessun invio registrato</div>
    <div class="small" style="max-width:460px">Quando mandi una traccia a una label, a un A&amp;R o a un DJ, registralo qui:
    tieni lo storico, vedi chi deve ancora rispondere e ti ricordo quando ricontattare.</div>
    <button class="btn primary" data-new-send="">${ICON.send}Nuovo invio</button></div>`;return}
  const list=promoSends();
  if(!list.length){m.innerHTML=`<div class="empty"><div class="big">${S.promoFilter==='due'?'Niente da ricontattare':'Nessun invio'}</div>
    ${S.promoFilter==='due'?'Tutti i follow-up sono in regola.':'Niente corrisponde alla ricerca.'}</div>`;return}
  const cols=S.promoFilter==='due'?PCOLS.slice(0,1):PCOLS;
  m.innerHTML='<div class="board-w">'+cols.map(([k,l,c])=>{
    const items=list.filter(s=>sendCol(s)===k);
    return `<section class="col" style="--c:${c}"><div class="colh"><span class="dot"></span><span class="ct">${l}</span><span class="n">${items.length}</span></div>
      <div class="cards">${items.length?items.map(sendCard).join(''):'<div class="col-empty">Nessun invio</div>'}</div></section>`;
  }).join('')+'</div>';
}
function sendCard(s){
  const c=contactById(s.contact)||{name:'Contatto eliminato',type:'altro'},p=byId(s.project),ch=CH[s.channel]||CH.altro;
  const when=s.status==='attesa'?'inviata '+agoDate(s.date):s.reply_date?'risposta '+agoDate(s.reply_date):agoDate(s.date);
  const sub=c.type!=='label'?contactSub(c):'';
  const ex=s.exclusive_until&&s.exclusive_until>=S.promo.today&&(s.status==='attesa'||s.status==='interessati');
  const rel=s.status==='firmata'&&p?releaseText(p):'';
  return `<article class="card scard" data-send="${s.id}" style="--c:${sendColor(s)}">
    <div class="c-top"><div class="cname">${esc(p?p.name:s.title)}</div><div class="c-act">
      <button class="ib sm" data-sact="edit" data-sid="${s.id}" title="Modifica o segna la risposta">${ICON.edit}</button></div></div>
    <div class="who">${avatar(c)}<b>${esc(c.name)}</b>${sub?`<small>· ${esc(sub)}</small>`:''}</div>
    <div class="meta">${s.kind==='promo'?'<span class="pill promo-p">Promo</span>':''}<span class="chan">${ICON[ch.i]}${esc(ch.l)}</span><span>${esc(when)}</span>
      ${s.followups&&s.followups.length?`<span>${s.followups.length} follow-up</span>`:''}</div>
    ${isDue(s)?`<div class="meta"><span class="pill stale-b">${ICON.clock}${esc(dueText(s))}</span></div>`
      :s.status==='attesa'&&s.followup?`<div class="meta"><span>follow-up ${inDays(s.followup)}</span></div>`:''}
    ${s.status==='rifiutata'||s.status==='nessuna'?`<div class="meta"><span class="st" style="--c:${sendColor(s)}">${esc(sendLabel(s))}</span></div>`:''}
    ${ex?`<div class="crel excl">In esclusiva fino ${alDate(s.exclusive_until)}</div>`:''}
    ${rel?`<div class="crel">${esc(rel)}</div>`:''}
    ${s.reply_note?`<div class="cnote">«${esc(s.reply_note)}»</div>`:''}</article>`;
}

function contactList(){
  const q=S.q.trim().toLowerCase();
  return S.promo.contacts.filter(c=>!q||[c.name,c.org,c.email,c.instagram,(c.genres||[]).join(' '),c.notes,CTYPES[c.type]]
    .join(' ').toLowerCase().includes(q));
}
function renderContacts(){
  const m=$('#main');
  if(!S.promo.contacts.length){m.innerHTML=`<div class="empty"><div class="big">La rubrica è vuota</div>
    <div class="small" style="max-width:460px">Aggiungi le label, gli A&amp;R e i DJ a cui mandi la tua musica:
    ritrovi canali, note e lo storico di tutto quello che gli hai mandato.</div>
    <button class="btn primary" data-new-contact>${ICON.plus}Nuovo contatto</button></div>`;return}
  const list=contactList();
  if(!list.length){m.innerHTML='<div class="empty"><div class="big">Nessun contatto</div>Niente corrisponde alla ricerca.</div>';return}
  m.innerHTML=`<table class="tbl"><thead><tr><th>Nome</th><th>Tipo</th><th>Canali</th><th>Generi</th><th class="num">Invii</th>
    <th class="num">Risposte</th><th>Ultimo invio</th></tr></thead><tbody>`+list.map(c=>{
    const ss=S.promo.sends.filter(s=>s.contact===c.id),ans=ss.filter(s=>ANSWERED.includes(s.status)).length;
    const last=ss.reduce((a,s)=>s.date>a?s.date:a,'');
    const icons=[c.email&&ICON.mail,c.instagram&&ICON.ig,c.soundcloud&&ICON.cloud,c.website&&ICON.globe].filter(Boolean).join('');
    return `<tr class="row${c.id===S.openContact?' is-sel':''}" data-cid="${c.id}">
      <td><div class="who" style="margin:0">${avatar(c)}<b>${esc(c.name)}</b></div></td><td class="mute">${esc(contactSub(c))}</td>
      <td><span class="icons">${icons}</span></td><td>${(c.genres||[]).map(g=>`<span class="genre">${esc(g)}</span>`).join('')}</td>
      <td class="num">${ss.length}</td><td class="num rate">${ss.length?`${ans} <small>· ${Math.round(ans/ss.length*100)}%</small>`:'–'}</td>
      <td class="nowrap">${last?agoDate(last):'–'}</td></tr>`;
  }).join('')+'</tbody></table>';
}

// ------------------------------------------------------------ promo: nuovo invio, ricontatta, modifica
const CH_ORDER=['email','instagram','soundcloud','website','altro'];
const ADDR={email:['Email','demo@label.com'],instagram:['Utente Instagram','nomeutente'],
  soundcloud:['Profilo SoundCloud','https://soundcloud.com/…'],website:['Pagina per le demo','https://…']};
const OPEN_LABEL={email:'Apri in Mail',instagram:'Copia, apri Instagram',soundcloud:'Copia, apri SoundCloud',website:'Copia, apri il sito',altro:'Copia il messaggio'};
const defaultChannel=c=>!c?'email':c.email?'email':c.instagram?'instagram':c.website?'website':c.soundcloud?'soundcloud':'email';

function openCompose(o){
  const T=S.promo.today,N=S.promo.settings.followup_days||14;
  const C=S.compose={mode:o.mode||'new',sid:o.sid||null,dirty:false,fixedTrack:!!o.fixedTrack,addrFor:null,recips:new Set(),step:''};
  if(C.sid){
    const s=S.promo.sends.find(x=>x.id===C.sid);if(!s)return;
    Object.assign(C,{kind:s.kind||'demo',pid:s.project,cid:s.contact,channel:s.channel,link:s.link,date:s.date,followup:s.followup,
      excl:s.exclusive_until,status:s.status,reply_date:s.reply_date,reply_note:s.reply_note,notes:s.notes,title:s.title,fixedTrack:true});
    if(C.mode==='followup')C.followup=addDays(T,N);
  }else{
    const p=byId(o.pid),oc=contactById(o.cid);
    // link: quello salvato per la traccia, altrimenti quello dell'ultimo invio
    const lastLink=o.pid?(sendsOf(o.pid).find(s=>s.link)||{}).link||'':'';
    // promo ai DJ se lo chiedi, se scegli un DJ o un promoter, o se la traccia è un edit
    const kind=o.kind||(oc?(oc.type==='dj'||oc.type==='promoter'?'promo':'demo'):(p&&p.kind==='edit'?'promo':'demo'));
    Object.assign(C,{kind,pid:o.pid||'',cid:o.cid||(S.promo.contacts.length?'':'__new'),link:(p&&p.link)||lastLink,date:T,
      followup:kind==='promo'?'':addDays(T,N),excl:'',status:'attesa',ncName:'',ncType:'label'});
    if(o.cid)C.recips.add(o.cid);
    C.channel=defaultChannel(oc);
  }
  renderCompose();$('#ovSend').classList.add('show');
}
function syncCompose(){
  const C=S.compose;if(!C)return;
  const map={cLink:'link',cAddr:'addr',cDate:'date',cFollow:'followup',cExcl:'excl',cSubj:'subj',cMsg:'msg',cStatus:'status',
    cReplyDate:'reply_date',cReply:'reply_note',cNotes:'notes',ncName:'ncName',ncType:'ncType'};
  for(const [id,k] of Object.entries(map)){const e=document.getElementById(id);if(e)C[k]=e.value}
}
function composeCtx(nome){
  const C=S.compose,p=byId(C.pid);
  return {nome:nome||'',artista:p&&p.alias!=='Senza alias'?p.alias:'',titolo:p?p.name:(C.title||''),bpm:p&&p.bpm?p.bpm:'',
    link:C.link||'',label:p?p.label:'',uscita:p&&p.release_date?', esce '+ilDate(p.release_date):''};
}
// senza nome (promo a più DJ) "Ciao !" diventa "Ciao!"
const tidy=t=>t.replace(/[ \t]+([!,.?])/g,'$1').replace(/[ \t]{2,}/g,' ').replace(/\n{3,}/g,'\n\n');
function composeText(nomeOverride){
  const C=S.compose,T=S.promo.settings.templates,promo=C.kind==='promo';
  let nome;
  if(nomeOverride!==undefined)nome=nomeOverride;
  else if(promo&&C.mode==='new'){const r=[...C.recips];nome=r.length===1?(contactById(r[0])||{}).name:''}
  else nome=(C.cid==='__new'?{name:C.ncName}:contactById(C.cid)||{}).name;
  const ctx=composeCtx(nome),subj=fill(promo?T.promo_subject:T.email_subject,ctx);
  const key=C.mode==='followup'?'followup_body':promo?'promo_body':(C.channel==='email'||C.channel==='website')?'email_body':'dm_body';
  return {subj:C.mode==='followup'?'Re: '+subj:subj,msg:tidy(fill(T[key],ctx))};
}
function regenMsg(){
  const C=S.compose;if(!C||C.dirty||C.mode==='edit')return;
  syncCompose();const t=composeText();C.subj=t.subj;C.msg=t.msg;
  const m=$('#cMsg'),sj=$('#cSubj');if(m)m.value=C.msg;if(sj)sj.value=C.subj;
}
function dateField(id,v,ph){return `<input class="field" id="${id}" data-date type="${v?'date':'text'}" placeholder="${ph}" value="${esc(v||'')}">`}
function bindDates(root){
  // data vuota: campo di testo (WebKit mostrerebbe la data di oggi come se fosse impostata)
  root.querySelectorAll('[data-date]').forEach(i=>{
    i.addEventListener('focus',()=>{if(i.type!=='date'){i.type='date';try{i.showPicker()}catch(e){}}});
    i.addEventListener('blur',()=>{if(!i.value)i.type='text'});
  });
}
function trackOptions(sel){
  const rank=p=>p.status==='finita'?0:p.status==='master'?1:p.status==='rilasciata'?3:2;
  return [...S.projects].sort((a,b)=>rank(a)-rank(b)||b.mtime-a.mtime).map(p=>`<option value="${esc(p.id)}"${p.id===sel?' selected':''}>
    ${esc(p.name)} · ${esc(label(p.status))}${p.kind!=='original'?' · '+esc(KINDS[p.kind]):''}${S.aliasN>1?' · '+esc(p.alias):''}</option>`).join('');
}
function composeWarnings(){
  const C=S.compose;if(C.mode!=='new'||!C.pid||C.kind==='promo')return'';
  const p=byId(C.pid),c=C.cid&&C.cid!=='__new'?contactById(C.cid):null;
  const w=[],same=c?sendsOf(C.pid).filter(s=>s.contact===c.id):[];
  if(same.length)w.push(`L'hai già mandata a <b>${esc(c.name)}</b> ${ilDate(same[0].date)} (${esc(sendLabel(same[0]).toLowerCase())}).`);
  exclusivesFor(C.pid,C.cid).forEach(s=>w.push(`È in esclusiva a <b>${esc((contactById(s.contact)||{}).name||'?')}</b> fino ${alDate(s.exclusive_until)}.`));
  if(p&&p.kind==='edit'&&(!c||c.type==='label'||c.type==='ar'))
    w.push("È un <b>edit</b>: le label di solito non lo firmano senza la licenza dell'originale. Puoi pubblicarlo da te o mandarlo come promo ai DJ.");
  return w.length?`<div class="warnbox">${ICON.warn}<div>${w.join('<br>')}</div></div>`:'';
}
// promo ai DJ: con che canale raggiungo ogni contatto
const viaOf=c=>c.email?'email':c.instagram?'instagram':c.soundcloud?'soundcloud':'';
function recipRows(){
  const C=S.compose,order=c=>c.type==='dj'?0:c.type==='promoter'?1:2;
  return [...S.promo.contacts].sort((a,b)=>order(a)-order(b)||a.name.localeCompare(b.name,'it')).map(c=>{
    const v=viaOf(c),sent=C.pid&&sendsOf(C.pid).some(s=>s.contact===c.id);
    return `<label class="rrow"><input type="checkbox" data-rc="${c.id}"${C.recips.has(c.id)?' checked':''}>${avatar(c)}
      <span class="rname2"><b>${esc(c.name)}</b><small>${esc(contactSub(c))}${sent?' · già inviata':''}</small></span>
      <span class="via${v?'':' due'}">${v?ICON[CH[v].i]+esc(CH[v].l):'nessun canale'}</span></label>`;
  }).join('');
}
function renderDmStep(){
  const C=S.compose;
  $('#sendModal').innerHTML=`<h2>Ora su Instagram e SoundCloud</h2>
    <div class="small">Questi contatti non hanno l'email: per ognuno copio il messaggio e apro la chat, tu incolli e invii.</div>
    <div class="rlist" style="margin-top:14px">${C.dmList.map(id=>{
      const c=contactById(id)||{name:'?'},v=c.instagram?'instagram':'soundcloud',done=C.dmDone.has(id);
      return `<div class="rrow">${avatar(c)}<span class="rname2"><b>${esc(c.name)}</b><small>${esc(CH[v].l)}</small></span>
        <button class="btn sm${done?'':' primary'}" data-dm="${id}">${done?'✓ Aperta':ICON[CH[v].i]+'Copia e apri'}</button></div>`;
    }).join('')}</div>
    <div class="m-foot2"><span class="grow"></span><button class="btn primary" data-closecompose>Fatto</button></div>`;
}
function renderCompose(){
  const C=S.compose;
  if(C.step==='dm')return renderDmStep();
  const prevScroll=($('#sendModal .rlist')||{}).scrollTop||0;
  const p=byId(C.pid),c=C.cid&&C.cid!=='__new'?contactById(C.cid):null,edit=C.mode==='edit',fu=C.mode==='followup';
  const promoNew=C.kind==='promo'&&C.mode==='new';
  const key=(C.cid||'')+'|'+C.channel;
  if(C.addrFor!==key){C.addr=chAddr(c,C.channel);C.addrFor=key}
  if(!C.dirty&&!edit){const t=composeText();C.subj=t.subj;C.msg=t.msg}
  const title=edit?(C.kind==='promo'?'Promo':'Invio'):fu?'Ricontatta':'Nuovo invio';
  const sub=p?`${esc(p.name)} · ${esc(p.alias)}${p.bpm?' · '+p.bpm+' BPM':''}${p.kind!=='original'?' · '+esc(KINDS[p.kind]):''}`:esc(C.title||'');
  const types=sel=>Object.entries(CTYPES).map(([k,l])=>`<option value="${k}"${sel===k?' selected':''}>${l}</option>`).join('');
  const head=`<h2>${title}${p||C.title?' · '+esc(p?p.name:C.title):''}</h2><div class="small">${sub}</div>
    ${C.mode==='new'?`<div class="seg2" style="margin-top:14px"><button data-ck="demo" class="${C.kind==='demo'?'on':''}">${ICON.send}Demo a una label</button>
      <button data-ck="promo" class="${C.kind==='promo'?'on':''}">${ICON.wave}Promo ai DJ</button></div>`:''}
    ${C.mode==='new'&&!C.fixedTrack?`<div class="lbl">Traccia</div><select class="field" id="cTrack"><option value=""${C.pid?'':' selected'} disabled>Scegli la traccia…</option>${trackOptions(C.pid)}</select>`:''}`;
  let body,foot;
  if(promoNew){
    const sel=[...C.recips].map(contactById).filter(Boolean),em=sel.filter(x=>x.email),dm=sel.filter(x=>!x.email);
    body=`<div class="lbl">A chi<span class="hint">${sel.length?sel.length+(sel.length===1?' selezionato':' selezionati'):'scegli uno o più contatti'}</span></div>
      ${S.promo.contacts.length?`<div class="rlist">${recipRows()}</div>`:'<div class="autonote">La rubrica è vuota: aggiungi i DJ in Promo → Contatti.</div>'}
      <div class="lbl">Link alla traccia</div><input class="field" id="cLink" value="${esc(C.link||'')}" placeholder="https://soundcloud.com/…/s-privato">
      <div class="f2"><div><div class="lbl">Inviata il</div>${dateField('cDate',C.date,'Oggi')}</div>
        <div><div class="lbl">Ricontatta il</div>${dateField('cFollow',C.followup,'Nessun promemoria')}</div></div>
      <div class="lbl">Messaggio<span class="hint">modello «Promo» in ⚙︎ · puoi modificarlo</span></div>
      ${em.length?`<input class="field" id="cSubj" value="${esc(C.subj||'')}" style="margin-bottom:8px">`:''}
      <textarea class="msg" id="cMsg">${esc(C.msg||'')}</textarea>
      ${em.length>1?`<div class="m-hint">Una sola mail con ${em.length} destinatari in Ccn: nessuno vede gli indirizzi degli altri.</div>`:''}
      ${dm.length?`<div class="m-hint">${dm.length===1?'1 contatto non ha':dm.length+' contatti non hanno'} l'email: dopo ti aiuto a scrivergli su Instagram o SoundCloud.</div>`:''}`;
    foot=`<button class="btn" data-csub="log">Registra soltanto</button>
      <button class="btn primary" data-csub="open">${em.length?`${ICON.mail}Apri Mail${em.length>1?' (Ccn)':''} e registra`:`${ICON.send}Registra e continua`}</button>`;
  }else{
    const contactSel=fu?`<div class="picker">${avatar(c)}<b>${esc(c?c.name:'?')}</b><span class="mute">${esc(c?contactSub(c):'')}</span></div>`
      :`<select class="field" id="cContact">${!C.cid?'<option value="" selected disabled>Scegli a chi…</option>':''}
        ${S.promo.contacts.map(x=>`<option value="${x.id}"${x.id===C.cid?' selected':''}>${esc(x.name)}${x.type!=='label'?' · '+esc(CTYPES[x.type]):''}</option>`).join('')}
        ${edit?'':`<option value="__new"${C.cid==='__new'?' selected':''}>＋ Nuovo contatto…</option>`}</select>`;
    const addr=ADDR[C.channel];
    body=`<div class="lbl">A chi</div>${contactSel}
      ${C.cid==='__new'?`<div class="cf newc"><label>Nome<input class="field" id="ncName" value="${esc(C.ncName||'')}" placeholder="es. Nightshift Records"></label>
        <label>Tipo<select class="field" id="ncType">${types(C.ncType||'label')}</select></label></div>`:''}
      <div class="lbl">Canale</div>
      <div class="seg2">${CH_ORDER.map(k=>`<button data-ch="${k}" class="${C.channel===k?'on':''}">${ICON[CH[k].i]}${esc(CH[k].l)}</button>`).join('')}</div>
      <div class="f2">${addr?`<div><div class="lbl">${addr[0]}</div><input class="field" id="cAddr" value="${esc(C.addr||'')}" placeholder="${esc(addr[1])}"></div>`:''}
        <div${addr?'':' style="grid-column:1/-1"'}><div class="lbl">Link alla traccia</div><input class="field" id="cLink" value="${esc(C.link||'')}" placeholder="https://soundcloud.com/…/s-privato"></div></div>
      ${fu?`<div class="f2"><div><div class="lbl">Ricontatta di nuovo il</div>${dateField('cFollow',C.followup,'Nessun promemoria')}</div><div></div></div>`
        :`<div class="f3"><div><div class="lbl">Inviata il</div>${dateField('cDate',C.date,'Oggi')}</div>
        <div><div class="lbl">Ricontatta il</div>${dateField('cFollow',C.followup,'Nessun promemoria')}</div>
        <div><div class="lbl">Esclusiva fino al</div>${dateField('cExcl',C.excl,'Nessuna esclusiva')}</div></div>`}
      ${edit?`<div class="f2"><div><div class="lbl">Stato</div><select class="field" id="cStatus">${Object.entries(SST).map(([k,v])=>`<option value="${k}"${C.status===k?' selected':''}>${C.kind==='promo'&&k==='attesa'?'Inviata':v.l}</option>`).join('')}</select></div>
        <div><div class="lbl">Risposta arrivata il</div>${dateField('cReplyDate',C.reply_date,'Nessuna risposta')}</div></div>
        <div class="lbl">Cosa hanno risposto</div><textarea class="short" id="cReply" placeholder="${C.kind==='promo'?'«La suono sabato al club»':'«Ci piace, mandaci gli stem»'}">${esc(C.reply_note||'')}</textarea>
        <div class="lbl">Note</div><textarea class="short" id="cNotes" placeholder="Versione mandata, accordi, promemoria…">${esc(C.notes||'')}</textarea>`
      :`<div class="lbl">Messaggio<span class="hint">dai modelli in ⚙︎ · puoi modificarlo</span></div>
        ${C.channel==='email'?`<input class="field" id="cSubj" value="${esc(C.subj||'')}" style="margin-bottom:8px">`:''}
        <textarea class="msg" id="cMsg">${esc(C.msg||'')}</textarea>`}`;
    foot=edit?`<button class="btn primary" data-csub="save">Salva</button>`
      :`<button class="btn" data-csub="log">Registra soltanto</button>
        <button class="btn primary" data-csub="open">${ICON[CH[C.channel].i]}${OPEN_LABEL[C.channel]} e registra</button>`;
  }
  $('#sendModal').innerHTML=`${head}${body}${composeWarnings()}
    ${edit?'':`<div class="m-hint">L'app prepara il messaggio e registra l'invio: l'ultimo clic su «Invia» resta tuo.</div>`}
    <div class="m-foot2">${edit?`<button class="btn danger" data-csub="delete">Elimina invio</button>`:''}<span class="grow"></span>
      <button class="btn" data-closecompose>Annulla</button>${foot}</div>`;
  const root=$('#sendModal');bindDates(root);
  const list=root.querySelector('.rlist');if(list)list.scrollTop=prevScroll;
  const on=(id,ev,fn)=>{const e=document.getElementById(id);if(e)e.addEventListener(ev,fn)};
  on('cTrack','change',e=>{syncCompose();C.pid=e.target.value;const np=byId(C.pid);
    if(!C.link)C.link=(np&&np.link)||(sendsOf(C.pid).find(s=>s.link)||{}).link||'';renderCompose()});
  on('cContact','change',e=>{syncCompose();C.cid=e.target.value;C.channel=defaultChannel(contactById(C.cid));renderCompose()});
  on('ncName','input',regenMsg);on('cLink','input',regenMsg);
  on('cMsg','input',()=>{C.dirty=true});on('cSubj','input',()=>{C.dirty=true});
  root.querySelectorAll('[data-rc]').forEach(cb=>cb.addEventListener('change',()=>{
    syncCompose();cb.checked?C.recips.add(cb.dataset.rc):C.recips.delete(cb.dataset.rc);renderCompose()}));
}
function setComposeKind(kind){
  const C=S.compose;if(!C||C.kind===kind)return;
  syncCompose();
  if(kind==='promo'){if(C.cid&&C.cid!=='__new')C.recips.add(C.cid);if(C.followup===addDays(S.promo.today,S.promo.settings.followup_days||14))C.followup=''}
  else if(!C.followup)C.followup=addDays(S.promo.today,S.promo.settings.followup_days||14);
  C.kind=kind;C.dirty=false;renderCompose();
}
function closeOv(id){$('#'+id).classList.remove('show');if(id==='ovSend')S.compose=null}

async function saveAddr(cid){
  const C=S.compose,c=contactById(cid),f=ADDR[C.channel]?C.channel:null;
  const v=(C.addr||'').trim();
  if(!c||!f||!v||v.replace(/^@/,'')===(c[f]||''))return;
  const d=await api('/api/contact',{id:cid,[f]:v});Object.assign(c,d.contact);
}
async function submitPromo(how){
  const C=S.compose;
  if(!C.pid)throw new Error('Scegli la traccia');
  const sel=[...C.recips].map(contactById).filter(Boolean);
  if(!sel.length)throw new Error('Scegli almeno un contatto');
  for(const c of sel)await api('/api/send',{project:C.pid,contact:c.id,kind:'promo',channel:viaOf(c)||'altro',link:C.link,date:C.date,followup:C.followup});
  const em=sel.filter(c=>c.email),dm=sel.filter(c=>!c.email&&(c.instagram||/^https:\/\//i.test(c.soundcloud||'')));
  let msg=`Promo registrata per ${sel.length} ${sel.length===1?'contatto':'contatti'}`;
  if(how==='open'&&em.length){
    // una sola mail, destinatari in Ccn: nessuno vede gli indirizzi degli altri
    const bcc=em.map(c=>c.email).join(',');
    await openLink(`mailto:?bcc=${encodeURIComponent(bcc).replace(/%40/g,'@').replace(/%2C/g,',')}&subject=${encodeURIComponent(C.subj||'')}&body=${encodeURIComponent(C.msg||'')}`);
    msg+=' · mail aperta';
  }
  await load();
  if(how==='open'&&dm.length){C.step='dm';C.dmList=dm.map(c=>c.id);C.dmDone=new Set();renderCompose();toast(msg);return}
  closeOv('ovSend');toast(msg);
}
async function dmOne(id){
  const C=S.compose,c=contactById(id);if(!C||!c)return;
  const copying=copyText(C.dirty?C.msg:composeText(c.name).msg);  // subito, dentro il clic
  const url=c.instagram?'https://ig.me/m/'+encodeURIComponent(c.instagram):/^https:\/\//i.test(c.soundcloud||'')?c.soundcloud:'';
  try{
    const ok=await copying;if(url)await openLink(url);
    C.dmDone.add(id);renderCompose();toast(ok?`Messaggio per ${c.name} copiato: incollalo e invia`:'Copia il messaggio a mano');
  }catch(e){toast(e.message)}
}
async function composeSubmit(how){
  syncCompose();const C=S.compose;if(!C)return;
  // la copia va fatta subito, dentro il clic: dopo, il browser potrebbe non permetterla
  const copying=how==='open'&&C.kind!=='promo'&&C.channel!=='email'?copyText(C.msg||''):null;
  try{
    if(C.kind==='promo'&&C.mode==='new')return await submitPromo(how);
    if(C.mode==='edit'){
      if(how==='delete'){
        if(!await ask('Eliminare questo invio dallo storico?','Elimina'))return;
        await api('/api/send_delete',{id:C.sid});closeOv('ovSend');await load();toast('Invio eliminato');return;
      }
      await saveAddr(C.cid);
      const d=await api('/api/send',{id:C.sid,contact:C.cid,channel:C.channel,link:C.link,date:C.date,followup:C.followup,
        exclusive_until:C.excl,status:C.status,reply_date:C.reply_date,reply_note:C.reply_note,notes:C.notes});
      closeOv('ovSend');await load();
      toast(d.signed?`Firmata! «${d.send.title}» passa a Master${d.warning?' · '+d.warning:''}`:'Invio aggiornato');return;
    }
    if(!C.pid)throw new Error('Scegli la traccia');
    let cid=C.cid;
    if(!cid)throw new Error('Scegli a chi la mandi');
    if(cid==='__new'){
      if(!(C.ncName||'').trim())throw new Error('Scrivi il nome del nuovo contatto');
      const d=await api('/api/contact',{name:C.ncName,type:C.ncType||'label'});
      cid=C.cid=d.contact.id;S.promo.contacts.push(d.contact);C.addrFor=cid+'|'+C.channel;
    }
    await saveAddr(cid);
    if(C.mode==='followup')await api('/api/send',{id:C.sid,add_followup:true,followup:C.followup,channel:C.channel,link:C.link});
    else await api('/api/send',{project:C.pid,contact:cid,kind:C.kind,channel:C.channel,link:C.link,date:C.date,followup:C.followup,exclusive_until:C.excl});
    let msg=C.mode==='followup'?'Follow-up registrato':'Invio registrato';
    if(how==='open'){
      const c=contactById(cid),addr=chAddr(c,C.channel);
      if(C.channel==='email'){
        if(addr){await openLink(`mailto:${encodeURIComponent(addr).replace(/%40/g,'@')}?subject=${encodeURIComponent(C.subj||'')}&body=${encodeURIComponent(C.msg||'')}`);msg+=' · mail aperta'}
        else msg+=' · manca l\'email del contatto: mail non aperta';
      }else{
        const copied=await copying;
        const url=C.channel==='instagram'?(addr?'https://ig.me/m/'+encodeURIComponent(addr):'https://www.instagram.com/direct/inbox/')
          :/^https:\/\//i.test(addr||'')?addr:'';
        if(url)await openLink(url);
        msg+=copied?' · messaggio copiato, incollalo e invia':' · copia il messaggio a mano';
      }
    }
    closeOv('ovSend');await load();toast(msg);
  }catch(e){toast(e.message)}
}

function openNewContact(){
  $('#cModal').innerHTML=`<h2>Nuovo contatto</h2><div class="small">Una label, un A&amp;R, un DJ o un promoter a cui mandi la musica.</div>
    <div class="cf" style="margin-top:14px"><label>Nome<input class="field" id="nc2Name" placeholder="es. Nightshift Records"></label>
      <label>Tipo<select class="field" id="nc2Type">${Object.entries(CTYPES).map(([k,l])=>`<option value="${k}">${l}</option>`).join('')}</select></label>
      <label>Email<input class="field" id="nc2Email" placeholder="demo@label.com"></label>
      <label>Instagram<input class="field" id="nc2Ig" placeholder="nomeutente"></label></div>
    <div class="m-foot2"><span class="grow"></span><button class="btn" data-closec>Annulla</button><button class="btn primary" data-createc>Crea contatto</button></div>`;
  $('#ovC').classList.add('show');setTimeout(()=>$('#nc2Name').focus(),30);
}
async function createContact(){
  try{
    const d=await api('/api/contact',{name:$('#nc2Name').value,type:$('#nc2Type').value,email:$('#nc2Email').value,instagram:$('#nc2Ig').value});
    closeOv('ovC');if(S.mode!=='contacts')setMode('contacts');await load();openContact(d.contact.id);toast('Contatto creato');
  }catch(e){toast(e.message)}
}
async function deleteContact(id){
  const c=contactById(id);if(!c)return;
  if(!await ask(`Eliminare «${c.name}» dalla rubrica?`,'Elimina'))return;
  try{await api('/api/contact_delete',{id});closePanel();await load();toast('Contatto eliminato')}catch(e){toast(e.message)}
}
// conferma fatta in casa: nell'app per Mac le finestre confirm() del browser non compaiono
function ask(text,ok){
  return new Promise(res=>{
    $('#qModal').innerHTML=`<h2 style="font-size:16px">${esc(text)}</h2><div class="small">Non si può annullare.</div>
      <div class="m-foot2"><span class="grow"></span><button class="btn" id="qNo">Annulla</button><button class="btn primary" id="qYes" style="background:#ff453a;border-color:#ff453a">${esc(ok)}</button></div>`;
    const done=v=>{$('#ovQ').classList.remove('show');res(v)};
    $('#qNo').onclick=()=>done(false);$('#qYes').onclick=()=>done(true);
    $('#ovQ').classList.add('show');
  });
}

// ------------------------------------------------------------ eventi
document.addEventListener('click',e=>{
  if(e.button!==0||(MAC&&e.ctrlKey))return;  // tasto destro / ctrl-clic: ci pensa il menu
  const t=e.target;let x;
  if(t.closest('#menu')){const b=t.closest('[data-m]');if(b)menuAction(b);return}
  if(S.menuId)closeMenu();
  if(t.closest('#toast'))return;
  // promo
  if(x=t.closest('[data-mode]')){setMode(x.dataset.mode,x.dataset.pf);closeSide();return}
  if(x=t.closest('[data-ptab]')){flushNotes();S.ptab=x.dataset.ptab;renderPanel();return}
  if(x=t.closest('[data-sact]')){openCompose({sid:x.dataset.sid,mode:x.dataset.sact==='followup'?'followup':'edit'});return}
  if(x=t.closest('[data-sedit]')){openCompose({sid:x.dataset.sedit,mode:'edit'});return}
  if(x=t.closest('[data-new-send]')){openCompose({pid:x.dataset.newSend,fixedTrack:!!x.dataset.newSend});return}
  if(x=t.closest('[data-new-promo]')){openCompose({pid:x.dataset.newPromo,fixedTrack:true,kind:'promo'});return}
  if(x=t.closest('#sendModal [data-ck]')){setComposeKind(x.dataset.ck);return}
  if(x=t.closest('[data-dm]')){dmOne(x.dataset.dm);return}
  if(x=t.closest('[data-pk]')){S.promoKind=x.dataset.pk;renderToolbar();renderMain();return}
  if(x=t.closest('#panel [data-kind]')){update(S.openId,{kind:x.dataset.kind});return}
  if(x=t.closest('#panel [data-rk]')){update(S.openId,{release_kind:x.dataset.rk});return}
  if(t.closest('[data-open-rlink]')){const v=($('#rlink')||{}).value||'';if(/^https:\/\//i.test(v.trim()))openLink(v.trim()).catch(e=>toast(e.message));else toast('Scrivi un link che inizi con https://');return}
  if(t.closest('[data-publish]')){const p=byId(S.openId);if(p)update(p.id,{status:'rilasciata',release_date:p.release_date||S.promo.today}).then(r=>{if(r)toast(`«${p.name}» è uscita: spostata in Rilasciata`)});return}
  if(x=t.closest('[data-new-send-c]')){openCompose({cid:x.dataset.newSendC});return}
  if(t.closest('[data-new-contact]')){openNewContact();return}
  if(x=t.closest('#tbAction')){x.dataset.action==='contact'?openNewContact():openCompose(S.promoKind==='promo'?{kind:'promo'}:{});return}
  if(x=t.closest('#sendModal [data-ch]')){syncCompose();S.compose.channel=x.dataset.ch;renderCompose();return}
  if(x=t.closest('[data-csub]')){composeSubmit(x.dataset.csub);return}
  if(t.closest('[data-closecompose]')||t.id==='ovSend'){closeOv('ovSend');return}
  if(t.closest('[data-closec]')||t.id==='ovC'){closeOv('ovC');return}
  if(t.closest('[data-createc]')){createContact();return}
  if(x=t.closest('[data-del-contact]')){deleteContact(x.dataset.delContact);return}
  if(x=t.closest('#main [data-send]')){
    const s=S.promo.sends.find(y=>y.id===x.dataset.send);
    if(s&&byId(s.project))openPanel(s.project,'sends');else if(s)openCompose({sid:s.id,mode:'edit'});
    return;
  }
  if(x=t.closest('#main [data-cid]')){openContact(x.dataset.cid);return}
  if(x=t.closest('[data-act]')){
    const host=x.closest('[data-id]'),id=host?host.dataset.id:S.openId,a=x.dataset.act;
    if(a==='play')openIn(id,'ableton');else if(a==='finder')openIn(id,'finder');
    else if(a==='more'){const r=x.getBoundingClientRect();openMenu(id,r.left,r.bottom+4)}
    return;
  }
  if(x=t.closest('[data-view]')){S.view=x.dataset.view;store('view',S.view);render();markSel(true);return}
  if(x=t.closest('[data-filter]')){S.filter=x.dataset.filter;closeSide();setMode('tracks');return}
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
  const ov=[...document.querySelectorAll('.overlay.show')].pop();  // la finestra aperta più in alto
  if(mod&&e.key==='Enter'&&ov&&ov.id==='ovSend'){e.preventDefault();composeSubmit(S.compose&&S.compose.mode==='edit'?'save':'open');return}
  if(mod&&e.key==='Enter'&&S.openId&&!ov){e.preventDefault();openIn(S.openId,'ableton');return}
  if(e.key==='Escape'){
    if(S.menuId)return closeMenu();
    if(ov){if(ov.id==='ovs')closeSettings();else if(ov.id==='ovQ')$('#qNo').click();else closeOv(ov.id);return}
    if(a===$('#q')&&S.q){S.q='';a.value='';renderToolbar();renderMain();return}
    if(typing)return a.blur();
    if(S.openId)return closePanel();
    return;
  }
  if(typing||mod||e.altKey||ov)return;
  if(e.key==='/'){e.preventDefault();$('#q').focus();return}
  if(S.openId&&/^[1-9]$/.test(e.key)){const s=S.statuses[+e.key-1];if(s)setStatus(S.openId,s.key);return}
  if(S.mode==='tracks'&&(e.key==='ArrowDown'||e.key==='ArrowUp')){e.preventDefault();closeMenu();move(e.key==='ArrowDown'?1:-1)}
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
