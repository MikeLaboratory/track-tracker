#!/bin/zsh
# Pubblica una nuova versione di Track Tracker.
#
# Uso:  ./Scripts/release.sh patch | minor | major | X.Y.Z
#
# 1. aggiorna __version__ in track_tracker.py
# 2. sposta le voci "Non rilasciato" del CHANGELOG sotto la nuova versione, aggiorna i link e il badge del README
# 3. crea il commit "Versione X.Y.Z" e il tag vX.Y.Z
# Non pubblica niente su GitHub: alla fine stampa il comando per farlo.
set -euo pipefail

project_dir="${0:A:h:h}"
cd "$project_dir"

kind="${1:-}"
current=$(sed -nE 's/^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$/\1/p' track_tracker.py)
[[ -n "$current" ]] || { echo "Versione attuale non trovata in track_tracker.py"; exit 1 }
parts=("${(@s:.:)current}")
major=$parts[1] minor=$parts[2] patch=$parts[3]

if [[ "$kind" =~ '^[0-9]+\.[0-9]+\.[0-9]+$' ]]; then
    new="$kind"
else
    case "$kind" in
        major) new="$((major + 1)).0.0" ;;
        minor) new="$major.$((minor + 1)).0" ;;
        patch) new="$major.$minor.$((patch + 1))" ;;
        *)
            echo "Uso: $0 patch|minor|major|X.Y.Z   (versione attuale: $current)"
            echo "  patch → correzioni   minor → funzioni nuove   major → cambi che rompono qualcosa"
            exit 1 ;;
    esac
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Ci sono modifiche non salvate: fai prima il commit (o mettile da parte), poi rilancia."
    exit 1
fi
if git rev-parse -q --verify "refs/tags/v$new" >/dev/null; then
    echo "Il tag v$new esiste già."
    exit 1
fi

# controlla che il CHANGELOG dica cosa è cambiato, poi aggiorna i file
python3 - "$new" "$current" "$(date +%Y-%m-%d)" <<'PY'
import re, sys
new, old, today = sys.argv[1], sys.argv[2], sys.argv[3]
log = open("CHANGELOG.md", encoding="utf-8").read()
head = "## [Non rilasciato]\n"
if head not in log:
    sys.exit('Nel CHANGELOG manca la sezione "## [Non rilasciato]".')
pending = re.split(r"^## \[", log.split(head, 1)[1], maxsplit=1, flags=re.M)[0]
if not pending.strip():
    sys.exit('Scrivi nel CHANGELOG, sotto "Non rilasciato", cosa è cambiato in questa versione.')
log = log.replace(head, f"{head}\n## [{new}] — {today}\n", 1)
# link di confronto in fondo al file (formato Keep a Changelog)
m = re.search(r"^\[Non rilasciato\]: (\S+)/compare/\S+$", log, flags=re.M)
if m:
    base = m.group(1)
    log = log.replace(m.group(0),
                      f"[Non rilasciato]: {base}/compare/v{new}...HEAD\n[{new}]: {base}/compare/v{old}...v{new}", 1)
open("CHANGELOG.md", "w", encoding="utf-8").write(log)
src = open("track_tracker.py", encoding="utf-8").read()
src = re.sub(r'^__version__ = "[^"]+"$', f'__version__ = "{new}"', src, count=1, flags=re.M)
open("track_tracker.py", "w", encoding="utf-8").write(src)
# badge della versione nel README
readme = open("README.md", encoding="utf-8").read()
open("README.md", "w", encoding="utf-8").write(re.sub(r"badge/versione-[0-9.]+-", f"badge/versione-{new}-", readme))
PY

git add track_tracker.py CHANGELOG.md README.md
git commit -q -m "Versione $new"
git tag -a "v$new" -m "Versione $new"

echo "Creata la versione $new (da $current): commit e tag v$new."
echo "Per pubblicarla su GitHub:   git push origin main --follow-tags"
echo "Per aggiornare l'app:         ./Scripts/build-app.sh"
