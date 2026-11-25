#!/bin/bash
# Runs python3 against a private, headless GTK Broadway display with throwaway
# XDG directories, so tests and renders never touch the real screen, settings
# or keyring. Usage: tools/run-headless.sh <python args...>
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"
xdg=$(mktemp -d)
trap 'kill $bw 2>/dev/null; rm -rf "$xdg" "$sock" "$disp"' EXIT
export PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}"
export XDG_DATA_HOME="$xdg/data" XDG_CONFIG_HOME="$xdg/config" \
  XDG_CACHE_HOME="$xdg/cache" XDG_STATE_HOME="$xdg/state"
mkdir -p "$XDG_CACHE_HOME"
unset WAYLAND_DISPLAY DISPLAY
n=$((40 + RANDOM % 50))
sock="$XDG_RUNTIME_DIR/avd-headless-$n.sock"
disp="$XDG_RUNTIME_DIR/broadway$((n + 1)).socket"
gtk4-broadwayd -u "$sock" ":$n" >"$xdg/broadway.log" 2>&1 &
bw=$!
for _ in $(seq 50); do [ -S "$disp" ] && break; sleep 0.1; done
GDK_BACKEND=broadway BROADWAY_DISPLAY=":$n" python3 -u "$@"
