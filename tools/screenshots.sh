#!/bin/bash
# Regenerates the README screenshots in screenshots/ from the demo data
# (AVD_DEMO=1), so they never show a real account. One process per scene,
# because Broadway asserts on long sessions.
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
render() {  # <theme> <scene>
  AVD_OMARCHY_THEME="$repo/tools/themes/$1" \
    timeout 200 "$repo/tools/run-headless.sh" "$repo/tools/render.py" "$out/$1" "$2" \
    2>&1 | grep '^wrote' || true
}
render tokyo-night readme-main
render tokyo-night readme-settings
render catppuccin-latte readme-main
render catppuccin-latte readme-filter
cp "$out/tokyo-night-main.png" "$repo/screenshots/main-dark.png"
cp "$out/tokyo-night-settings.png" "$repo/screenshots/settings-dark.png"
cp "$out/catppuccin-latte-actions.png" "$repo/screenshots/actions-light.png"
cp "$out/catppuccin-latte-filter.png" "$repo/screenshots/filter-light.png"
