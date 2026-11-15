# Registro delle modifiche

Qui trovi cosa cambia in ogni versione di Track Tracker. Il formato segue [Keep a Changelog](https://keepachangelog.com/it/1.1.0/) e i numeri di versione seguono il [Versionamento Semantico](https://semver.org/lang/it/):

- **correzione** (1.1.0 → 1.1.1): errori sistemati, nessuna funzione nuova;
- **minore** (1.1.0 → 1.2.0): funzioni nuove, tutto il resto funziona come prima;
- **maggiore** (1.1.0 → 2.0.0): cambi che rompono qualcosa, per esempio un nuovo formato di `_tracker_data.json`.

Mentre lavori, scrivi le modifiche sotto "Non rilasciato": `Scripts/release.sh` le sposterà sotto il numero della nuova versione.

## [Non rilasciato]

## [1.1.0] — 2026-11-15

### Aggiunto
- Barra laterale con l'avanzamento della libreria, i filtri per stato, le tracce ferme e gli alias.
- Pannello dei dettagli a lato, scorrevole con ↑ ↓ senza chiuderlo.
- Menu col tasto destro su ogni traccia e pulsante ▶ per aprirla in Ableton direttamente dalla card.
- "Annulla" dopo ogni cambio di stato, e la possibilità di tornare allo stato dedotto dal nome della cartella.
- Scorciatoie: 1–8 per lo stato, ⌘↵ per aprire in Ableton, ⌘F o / per cercare, Esc per chiudere.
- Ordinamento per data, nome, BPM o stato.
- Numero di versione visibile nelle impostazioni, in "Informazioni su Track Tracker" e con `python3 track_tracker.py --version`.

### Modificato
- Interfaccia ridisegnata: colonne più larghe e comprimibili, con le colonne vuote e "Rilasciata" compresse all'inizio.
- Nuova icona: un'onda sonora nei colori degli stati.
- La barra del titolo dell'app si fonde con il resto della finestra.

### Corretto
- Dal nome mostrato vengono tolte anche le parole `ARRANG` e `STUCK`.
- Una data di uscita vuota non mostra più la data di oggi come se fosse impostata.

### Documentazione
- Nuova pagina di presentazione con screenshot, licenza MIT e istruzioni per le versioni.

## [1.0.0] — 2026-11-13

Prima versione con l'app per Mac.

### Aggiunto
- App per Mac con finestra propria, icona, menu e scorciatoie, che sceglie e ricorda la cartella dei progetti.
- Board con stati, note, label, data di uscita, BPM letto dai file `.als` e tracce ferme.
- Stato e note seguono le cartelle rinominate o spostate.
- Tag colorati del Finder, con colori distinti per "Finita" e "Rilasciata".

### Sicurezza
- Le altre pagine aperte nel browser non possono mandare comandi a Track Tracker.

[Non rilasciato]: https://github.com/MikeLaboratory/track-tracker/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/MikeLaboratory/track-tracker/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/MikeLaboratory/track-tracker/releases/tag/v1.0.0
