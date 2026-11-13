# Track Tracker

Una board per tenere traccia dello stato dei tuoi progetti Ableton Live: idea, in lavorazione, arrangiamento, da mixare, master, bloccata, finita, rilasciata.

Funziona tutto in locale, senza account né cloud. Puoi usarla come app per Mac con la sua finestra, oppure lanciare direttamente lo script Python e aprirla nel browser.

## App per Mac

Il modo più comodo: un'app con la sua icona e la sua finestra, senza Terminale né browser.

### Requisiti

- macOS 13 Ventura o successivo
- Xcode o Command Line Tools (servono per compilare e forniscono anche Python 3)

### Installazione

```bash
git clone https://github.com/MikeLaboratory/track-tracker.git
cd track-tracker
./Scripts/build-app.sh
open "Apps/Track Tracker.app"
```

Lo script compila l'app, disegna l'icona, la firma per il tuo Mac (firma locale *ad hoc*, non una notarizzazione Apple) e la mette in `Apps/`. Da lì puoi trascinarla in **Applicazioni** o nel Dock.

Al primo avvio ti chiede la cartella dei progetti Ableton e poi se la ricorda. Per cambiarla: **Archivio → Scegli cartella progetti…** (⌘O).

| Scorciatoia | Azione |
|---|---|
| ⌘O | Scegli cartella progetti |
| ⇧⌘O | Mostra la cartella nel Finder |
| ⌘R | Rileggi la cartella |
| ⌘F | Cerca |

Dentro l'app gira lo stesso `track_tracker.py` descritto sotto: dati, stati e note sono gli stessi, che tu usi l'app o lo script. Dopo aver modificato lo script, rilancia `./Scripts/build-app.sh` per aggiornare l'app. Per controllare che l'app si avvii e carichi la board: `"Apps/Track Tracker.app/Contents/MacOS/TrackTracker" --check-launch`.

> Ogni volta che ricompili, macOS può chiederti di nuovo il permesso di accedere a cartelle come Documenti o Scrivania: è normale con la firma ad hoc.

## Avvio da Terminale (senza app)

1. Copia `track_tracker.py` dentro la cartella che contiene i tuoi progetti (es. `Ableton Projects`).
2. Apri il Terminale in quella cartella e lancia:

   ```bash
   python3 track_tracker.py
   ```

3. Si apre il browser su <http://127.0.0.1:8765>. Per chiudere premi `Ctrl+C` nel Terminale.

Su Mac, al primo avvio viene creato `Avvia Track Tracker.command` nella cartella dei progetti: le volte successive basta un doppio clic su quel file.

Puoi anche lasciare lo script altrove e passare la cartella come argomento:

```bash
python3 track_tracker.py ~/Music/"Ableton Projects"
```

Anche in questo caso il launcher viene creato nella cartella dei progetti e la riapre da solo (se poi sposti lo script, cancella il launcher: verrà ricreato al prossimo avvio).

Se Track Tracker è già aperto su quella cartella, rilanciarlo apre solo il browser. Se la porta 8765 è occupata da un altro programma (o da Track Tracker aperto su un'altra cartella), usa la prima libera tra 8766 e 8774: l'indirizzo giusto è scritto nel Terminale.

Serve Python 3.8 o successivo (su macOS è già installato con gli strumenti da riga di comando).

## Come trova i progetti

- Ogni cartella che contiene almeno un file `.als` è un progetto. Se ce ne sono diversi, conta come principale quello modificato più di recente.
- La prima sottocartella sotto la cartella principale è l'**alias** (es. `Ableton Projects/NomeArtista/Traccia Project`). I progetti direttamente nella cartella principale finiscono in "Senza alias".
- Le cartelle di servizio di Ableton (`Backup`, `Samples`, `Recorded`, `Freeze`…) vengono saltate.
- Il BPM viene letto dal file `.als` e messo in cache: il file viene riletto solo quando cambia.

## Stato automatico dal nome

Finché non scegli tu uno stato, viene dedotto dal nome della cartella (sulla card compare la scritta *auto*):

| Nel nome compare… | Stato |
|---|---|
| `RELEASED`, `RILASCIATA`, `USCITA` (o la cartella sta dentro una cartella `Released` / `Rilasciate` / `Uscite`) | Rilasciata |
| `BLOCKED`, `BLOCCATA`, `STUCK` | Bloccata |
| `MASTER` | Master |
| `DA MIX`, `MIXARE`, `TO MIX`, `MIXDOWN` | Da mixare |
| `FINITA`, `DONE`, `FINISHED`, `CONCLUSA` | Finita |
| `ARRANG…` | Arrangiamento |
| `IDEA`, `SKETCH`, `BOZZA` | Idea |
| nient'altro | In lavorazione |

Maiuscole e minuscole non contano, e funzionano anche separatori come `_` o `-` (es. `Kick_DONE`). Queste parole vengono tolte anche dal nome mostrato nella board.

## Cosa puoi fare

- **Board** con una colonna per stato: trascina le card per cambiarlo. Oppure usa la vista **Lista**, ordinabile per colonna.
- Filtra per alias, cerca nei nomi e nelle note, mostra **solo le tracce ferme** (non toccate da più di N giorni, 60 di default, modificabile in ⚙︎).
- Clicca una traccia per scrivere note e cose da fare (salvate mentre scrivi), label e data di uscita.
- **▶ Apri in Ableton** e **Mostra nel Finder** (solo Mac).
- **Tag colorati nel Finder** (solo Mac): quando cambi stato, la cartella del progetto riceve il tag corrispondente. Da ⚙︎ puoi disattivarli o applicarli a tutte le tracce in una volta. Il Finder ha 7 colori per 8 stati, quindi "Idea" è un tag senza colore:

  | Idea | In lavorazione | Arrangiamento | Da mixare | Master | Bloccata | Finita | Rilasciata |
  |---|---|---|---|---|---|---|---|
  | nessuno | blu | viola | arancio | giallo | rosso | verde | grigio |

## Dove sono i dati

Stati, note, label e impostazioni vengono salvati in `_tracker_data.json` nella cartella dei progetti. I tuoi file Ableton non vengono mai modificati: al massimo ricevono un tag del Finder.

Se **rinomini o sposti** la cartella di un progetto (anche sotto un altro alias o in `Released`), stato e note la seguono: Track Tracker la riconosce dalla sua identità su disco. Se invece la copi su un altro disco, la ritrova dal nome del file `.als` principale, ma solo se nessun altro progetto ha un `.als` con lo stesso nome (due `Untitled.als`, per esempio, non vengono riagganciati). I dati di un progetto non più trovato restano nel JSON, così ricompaiono se ricolleghi il disco.

## Sicurezza

Il server (anche quello dentro l'app) ascolta solo su `127.0.0.1`, quindi non è raggiungibile dalla rete. Accetta richieste solo dalla propria pagina: altri siti aperti nel browser non possono modificare i dati.
