# Registro delle modifiche

Qui trovi cosa cambia in ogni versione di Track Tracker. Il formato segue [Keep a Changelog](https://keepachangelog.com/it/1.1.0/) e i numeri di versione seguono il [Versionamento Semantico](https://semver.org/lang/it/):

- **correzione** (1.1.0 → 1.1.1): errori sistemati, nessuna funzione nuova;
- **minore** (1.1.0 → 1.2.0): funzioni nuove, tutto il resto funziona come prima;
- **maggiore** (1.1.0 → 2.0.0): cambi che rompono qualcosa, per esempio un nuovo formato di `_tracker_data.json`.

Mentre lavori, scrivi le modifiche sotto "Non rilasciato": `Scripts/release.sh` le sposterà sotto il numero della nuova versione.

## [Non rilasciato]

## [1.3.0] — 2026-11-17

### Aggiunto
- Rubrica: colonne ordinabili con un clic (nome, tipo, generi, invii, risposte, ultimo invio); l'ordine scelto viene ricordato.
- Rubrica: filtri per tipo (con i conteggi), genere, canale e "Mai contattati"; un clic su un genere nella tabella filtra per quel genere.

## [1.2.1] — 2026-11-17

### Corretto
- Nei contatti si può incollare il link del profilo Instagram (o «@nome»): l'app tiene il solo nome utente, così «Copia e apri Instagram» apre la chat giusta. I contatti già salvati vengono sistemati all'apertura.
- Gli indirizzi SoundCloud e delle pagine demo scritti senza «https://» si aprono lo stesso.

## [1.2.0] — 2026-11-17

### Aggiunto
- Sezione **Promo** per gestire l'invio delle tracce a label, A&R e DJ:
  - board degli invii con le colonne Da ricontattare, In attesa, Interessati o supporto, Firmate, Rifiutate o senza risposta;
  - scheda "Invii" nel pannello di ogni traccia, con tutto lo storico, e sulla card "N invii · M da ricontattare" o "Mai inviata";
  - finestra "Nuovo invio" con messaggio compilato dai modelli: apre Mail con la mail pronta, oppure copia il testo e apre la chat di Instagram, il profilo SoundCloud o la pagina demo;
  - promemoria di follow-up (dopo 14 giorni, modificabile) e pulsante "Ricontatta" con il messaggio di sollecito;
  - avvisi se la traccia è in esclusiva a un'altra label o se l'hai già mandata a quel contatto;
  - quando un invio diventa "Firmata", la traccia passa a Master e prende il nome della label.
- Rubrica dei contatti con canali, generi, note, percentuale e tempo medio di risposta, e storico degli invii.
- Modelli dei messaggi e giorni del follow-up modificabili nelle impostazioni.
- **Promo ai DJ**: un invio a più DJ insieme, con un'unica mail in Ccn per chi ha l'email e un pulsante "Copia e apri" per chi è su Instagram o SoundCloud; l'esito è "Supportata" o "Nessuna risposta" e la board si filtra per Demo o Promo.
- **Tipo di traccia** (Originale, Edit, Remix), dedotto dal nome della cartella e modificabile.
- **Uscita pubblicata da me** (SoundCloud, Bandcamp, YouTube…) con data e link, oltre all'uscita con una label, e pulsante "Segna come pubblicata".
- Avviso quando provi a mandare un edit come demo a una label.

### Modificato
- Contatti e invii stanno in un file a parte, `_tracker_promo.json`, nella cartella dei progetti, e seguono le tracce anche se rinomini o sposti le cartelle.

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

[Non rilasciato]: https://github.com/MikeLaboratory/track-tracker/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/MikeLaboratory/track-tracker/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/MikeLaboratory/track-tracker/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/MikeLaboratory/track-tracker/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/MikeLaboratory/track-tracker/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/MikeLaboratory/track-tracker/releases/tag/v1.0.0
