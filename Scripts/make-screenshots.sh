#!/bin/zsh
# Rigenera gli screenshot del README in docs/ usando progetti dimostrativi inventati.
# Uso: ./Scripts/make-screenshots.sh
set -euo pipefail

project_dir="${0:A:h:h}"
cd "$project_dir"
mkdir -p docs/screenshots .build

# percorso fisso e leggibile: compare nel pannello dei dettagli
work="/tmp/Track Tracker Demo"
rm -rf "$work"
demo="$work/Ableton Projects"
python3 Scripts/make-demo.py "$demo"

swiftc -O Scripts/snapshot.swift -o .build/snapshot 2> >(grep -v "search path" >&2)

# server in modalità app: si chiude da solo quando finisce il "sleep" che gli tiene aperta la stdin
sleep 300 | python3 -u track_tracker.py "$demo" --app > "$work/url" &
for _ in {1..50}; do grep -q TRACKTRACKER_URL "$work/url" 2>/dev/null && break; sleep 0.1; done
url=$(sed -n 's/^TRACKTRACKER_URL //p' "$work/url")
[[ -n "$url" ]] || { echo "Il server non è partito"; exit 1 }
cleanup() { pkill -f "track_tracker.py $demo --app" 2>/dev/null || true; rm -rf "$work" }
trap cleanup EXIT

shot() { .build/snapshot "$url" "docs/screenshots/$1" 1600 960 "$2" }

shot board.png "S.view='board'; render();"
shot details.png "S.view='board'; render(); openPanel('Kairo/Moonlight (Kairo Edit)');"
shot list.png "S.view='list'; render();
  const row = document.querySelector('[data-id=\"Kairo/Signal Fire\"]').getBoundingClientRect();
  openMenu('Kairo/Signal Fire', row.left + 300, row.top + row.height / 2);"
shot stale.png "S.view='board'; S.filter='stale'; render();"
shot promo.png "setMode('promo');"
shot compose.png "S.view='board'; render(); openCompose({pid:'Kairo/Northern Lines',fixedTrack:true});
  const c = document.getElementById('cContact'); c.value = 'nr'; c.dispatchEvent(new Event('change'));"
shot contacts.png "setMode('contacts'); openContact('dc');"
shot sends.png "S.view='board'; render(); openPanel('Kairo/Northern Lines','sends');"
shot promo-djs.png "S.view='board'; render(); openCompose({pid:'Nova Echo/Daylight (Nova Echo Edit)',fixedTrack:true,kind:'promo'});
  for (const id of ['ds','kb','nr']) { const cb = document.querySelector('[data-rc=\"' + id + '\"]'); cb.checked = true; cb.dispatchEvent(new Event('change')); }
  const l = document.getElementById('cLink'); l.value = 'https://soundcloud.com/nova-echo/daylight-edit/s-anteprima'; l.dispatchEvent(new Event('input'));"

# 2000 px di larghezza bastano e il README si carica più in fretta
for f in docs/screenshots/*.png; do sips -Z 2000 "$f" >/dev/null; done
sips -z 256 256 Resources/AppIcon.png --out docs/icon.png >/dev/null
echo "Screenshot aggiornati in docs/screenshots/ e icona in docs/icon.png"
