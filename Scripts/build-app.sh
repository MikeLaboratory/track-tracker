#!/bin/zsh
# Compila Track Tracker.app in Apps/: poi puoi trascinarla in Applicazioni.
set -euo pipefail

project_dir="${0:A:h:h}"
app_dir="$project_dir/Apps/Track Tracker.app"

cd "$project_dir"

# versione: unica fonte è __version__ in track_tracker.py; la build è il numero di commit
version=$(sed -nE 's/^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$/\1/p' track_tracker.py)
[[ -n "$version" ]] || { echo "Versione non trovata in track_tracker.py"; exit 1 }
build_number=$(git rev-list --count HEAD 2>/dev/null || echo 1)

swift build -c release --product TrackTracker

# icona: disegnata una volta sola (cancella Resources/AppIcon.png per rifarla)
if [[ ! -f Resources/AppIcon.png ]]; then
    swift Scripts/make-icon.swift Resources/AppIcon.png
fi
iconset_dir="$project_dir/.build/AppIcon.iconset"
rm -rf "$iconset_dir" && mkdir -p "$iconset_dir"
for size in 16 32 128 256 512; do
    sips -z "$size" "$size" Resources/AppIcon.png --out "$iconset_dir/icon_${size}x${size}.png" >/dev/null
    sips -z $((size * 2)) $((size * 2)) Resources/AppIcon.png --out "$iconset_dir/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset_dir" -o Resources/AppIcon.icns

rm -rf "$app_dir"
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources"
cp .build/release/TrackTracker "$app_dir/Contents/MacOS/TrackTracker"
cp Resources/Info.plist "$app_dir/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $version" \
    -c "Set :CFBundleVersion $build_number" "$app_dir/Contents/Info.plist"
cp Resources/AppIcon.icns "$app_dir/Contents/Resources/AppIcon.icns"
cp track_tracker.py "$app_dir/Contents/Resources/track_tracker.py"

# firma locale ad hoc: basta per usarla sul proprio Mac (non è una notarizzazione Apple)
codesign --force --sign - "$app_dir"
touch "$app_dir"

lsregister="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
[[ -x "$lsregister" ]] && "$lsregister" -f "$app_dir"

echo "App pronta: $app_dir (versione $version, build $build_number)"
