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
    ("Kairo", "Signal Fire", 127, 9, 2, "mix", "Sidechain più morbido sul synth", "", ""),
    ("Kairo", "Echo Chamber ARRANG", 123, 30, 3, None, "", "", ""),
    ("Kairo", "Pulse Theory", 126, 5, 1, "wip", "", "", ""),
    ("Kairo", "Released/Blue Hour", 122, 400, 6, None, "", "Deep Current", "-380"),
]


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
        if entry:
            saved[f"{alias}/{folder}"] = entry
    data = {"projects": saved, "cache": {}, "settings": {"stale_days": 60, "finder_tags": False}}
    (root / "_tracker_data.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Progetti dimostrativi creati in {root}")


if __name__ == "__main__":
    main()
