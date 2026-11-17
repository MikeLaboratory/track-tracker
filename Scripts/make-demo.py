#!/usr/bin/env python3
"""Crea una cartella di progetti Ableton finti per gli screenshot del README.

Uso: python3 Scripts/make-demo.py <cartella>

Nomi, alias, label e note sono inventati. I file .als contengono solo il tempo,
quanto basta perché Track Tracker legga il BPM.
"""
import datetime as dt
import gzip
import json
import os
import sys
from pathlib import Path

# (alias, cartella, BPM, giorni dall'ultima modifica, versioni .als, stato, note, label, uscita)
TRACKS = [
    ("Nova Echo", "Midnight Run Project", 124, 2, 3, "mix",
     "Kick e basso si pestano sotto i 60 Hz\nAbbassare il pad nel break\nMandare a Luca per un ascolto", "", ""),
    ("Nova Echo", "Glass Hearts", 122, 1, 2, "wip", "Cercare un vocal per il drop", "", ""),
    ("Nova Echo", "Solar Drift_IDEA", 118, 20, 1, None, "", "", ""),
    ("Nova Echo", "Low Tide", 120, 6, 2, "arrangiamento", "Allungare l'intro a 32 battute", "", ""),
    ("Nova Echo", "Paper Planes MASTER", 126, 3, 4, "master", "Ultimo controllo sui medi", "Nightshift Records", "+20"),
    ("Nova Echo", "Released/Afterglow", 124, 190, 5, None, "", "Deep Current", "-160"),
    ("Nova Echo", "Static Bloom", 128, 95, 2, "bloccata", "Il groove non convince: ripartire dalle percussioni", "", ""),
    ("Kairo", "Northern Lines", 125, 12, 3, "finita", "Pronta da mandare alle label", "", ""),
    ("Kairo", "Concrete Garden", 130, 75, 1, "wip", "", "", ""),
    ("Kairo", "Velvet Room_IDEA", 121, 4, 1, None, "Accordi dal vecchio sketch al piano", "", ""),
    ("Kairo", "Signal Fire", 127, 9, 2, "finita", "Sidechain più morbido sul synth", "", ""),
    ("Kairo", "Echo Chamber ARRANG", 123, 30, 3, None, "", "", ""),
    ("Kairo", "Pulse Theory", 126, 5, 1, "finita", "", "", ""),
    ("Kairo", "Released/Blue Hour", 122, 400, 6, None, "", "Deep Current", "-380"),
    ("Kairo", "Moonlight (Kairo Edit)", 124, 12, 2, "rilasciata", "Free download per i follower", "", "-10"),
    ("Nova Echo", "Daylight (Nova Echo Edit)", 122, 2, 1, "finita", "", "", ""),
]
# uscite pubblicate da me (per esempio gli edit su SoundCloud)
SELF_RELEASES = {
    "Kairo/Moonlight (Kairo Edit)": {"release_kind": "self", "release_platform": "soundcloud",
                                     "release_link": "https://soundcloud.com/kairo-example/moonlight-edit"},
}

# rubrica e invii, tutti inventati: (id, nome, tipo, org, email, instagram, soundcloud, sito, generi, note)
CONTACTS = [
    ("dc", "Deep Current", "label", "", "demos@deepcurrent.example", "", "", "https://deepcurrent.example/demo",
     ["Deep House", "Melodic"], "Vogliono solo link privati, niente allegati. Esclusiva di 3 settimane."),
    ("nr", "Nightshift Records", "label", "", "demo@nightshift.example", "", "", "", ["Tech House"], ""),
    ("lo", "Low Orbit Music", "label", "", "demo@loworbit.example", "loworbitmusic", "", "", ["Progressive"], ""),
    ("br", "Blue Room Recordings", "label", "", "", "", "", "https://blueroom.example/demos", ["Minimal", "Deep Tech"],
     "Rispondono sempre, anche quando è un no."),
    ("pl", "Parallel Lines", "label", "", "hello@parallellines.example", "", "", "", ["Melodic Techno"], ""),
    ("mr", "Marta R.", "ar", "Nightshift Records", "", "marta.r.music", "", "", ["Tech House"], ""),
    ("ds", "DJ Solace", "dj", "", "", "djsolace", "https://soundcloud.com/djsolace-example", "", ["House"], ""),
    ("kb", "Kai B.", "dj", "", "", "", "https://soundcloud.com/kaib-example", "", ["Afro House"], ""),
]
# (traccia, contatto, canale, giorni fa, ricontatta tra N giorni, stato, risposta N giorni fa, risposta, esclusiva tra N giorni, follow-up fatti)
# gli invii ai DJ (ds, kb) sono promo, gli altri demo
SENDS = [
    ("Kairo/Northern Lines", "lo", "email", 18, -4, "attesa", None, "", None, 0),
    ("Kairo/Northern Lines", "dc", "website", 6, 8, "attesa", None, "", 13, 0),
    ("Kairo/Northern Lines", "br", "website", 28, None, "rifiutata", 14, "Non è nel nostro suono, ma mandaci le prossime", None, 0),
    ("Kairo/Northern Lines", "kb", "soundcloud", 33, None, "supportata", 32, "Bomba, la suono sabato", None, 0),
    ("Nova Echo/Paper Planes MASTER", "nr", "email", 40, None, "firmata", 15, "La prendiamo! Ti mando il contratto", None, 1),
    ("Nova Echo/Paper Planes MASTER", "ds", "instagram", 15, 0, "attesa", None, "", None, 0),
    ("Nova Echo/Paper Planes MASTER", "br", "website", 60, None, "nessuna", None, "", None, 2),
    ("Kairo/Signal Fire", "mr", "instagram", 3, 11, "attesa", None, "", None, 0),
    ("Kairo/Signal Fire", "kb", "soundcloud", 1, 13, "attesa", None, "", None, 0),
    ("Kairo/Signal Fire", "pl", "email", 9, 5, "attesa", None, "", None, 0),
    ("Nova Echo/Released/Afterglow", "dc", "email", 21, None, "interessati", 2, "Ci piace molto, puoi mandarci gli stem?", None, 0),
    ("Kairo/Moonlight (Kairo Edit)", "ds", "instagram", 11, None, "supportata", 9, "L'ho suonata ieri, il pubblico è impazzito", None, 0),
    ("Kairo/Moonlight (Kairo Edit)", "kb", "soundcloud", 11, None, "attesa", None, "", None, 0),
]
PROMO_CONTACTS = {"ds", "kb"}


def make_promo(root, today):
    day = lambda n: (today + dt.timedelta(days=n)).isoformat()
    contacts = {}
    for cid, name, kind, org, email, ig, sc, web, genres, notes in CONTACTS:
        contacts[cid] = {"id": cid, "name": name, "type": kind, "org": org, "email": email, "instagram": ig,
                         "soundcloud": sc, "website": web, "genres": genres, "notes": notes, "created": 0}
    titles = {f"{a}/{f}": f.split("/")[-1] for a, f, *_ in TRACKS}
    sends = {}
    for i, (pid, cid, ch, ago, fu, status, reply, note, excl, fups) in enumerate(SENDS):
        sid = f"s{i:02d}"
        sends[sid] = {"id": sid, "project": pid, "title": titles[pid], "contact": cid, "channel": ch,
                      "kind": "promo" if cid in PROMO_CONTACTS else "demo",
                      "link": f"https://soundcloud.com/demo-example/{sid}/s-privato", "date": day(-ago),
                      "followup": day(fu) if fu is not None else "", "status": status,
                      "reply_date": day(-reply) if reply is not None else "", "reply_note": note, "notes": "",
                      "exclusive_until": day(excl) if excl is not None else "",
                      "followups": [day(-ago + 14 * (k + 1)) for k in range(fups)], "created": i}
    promo = {"contacts": contacts, "sends": sends, "settings": {"followup_days": 14}}
    (root / "_tracker_promo.json").write_text(json.dumps(promo, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    if len(sys.argv) != 2:
        sys.exit("Uso: python3 Scripts/make-demo.py <cartella>")
    root = Path(sys.argv[1])
    root.mkdir(parents=True, exist_ok=True)
    today = dt.date.today()
    saved = {}
    for alias, folder, bpm, age, versions, status, notes, label, release in TRACKS:
        project = root / alias / folder
        project.mkdir(parents=True, exist_ok=True)
        name = folder.split("/")[-1]
        stamp = (dt.datetime.now() - dt.timedelta(days=age)).timestamp()
        for v in range(versions):
            als = project / (f"{name}.als" if v == 0 else f"{name} v{v + 1}.als")
            with gzip.open(als, "wb") as f:
                f.write(f'<Ableton><Tempo><Manual Value="{bpm}"/></Tempo></Ableton>'.encode())
            # la versione principale è la più recente
            t = stamp - (versions - v) * 3600 if v else stamp
            os.utime(als, (t, t))
        entry = {}
        if status:
            entry["status"] = status
        if notes:
            entry["notes"] = notes
        if label:
            entry["label"] = label
        if release:
            entry["release_date"] = (today + dt.timedelta(days=int(release))).isoformat()
        entry.update(SELF_RELEASES.get(f"{alias}/{folder}", {}))
        if entry:
            saved[f"{alias}/{folder}"] = entry
    data = {"projects": saved, "cache": {}, "settings": {"stale_days": 60, "finder_tags": False}}
    (root / "_tracker_data.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    make_promo(root, today)
    print(f"Progetti dimostrativi creati in {root}")


if __name__ == "__main__":
    main()
