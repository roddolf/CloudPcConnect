#!/bin/sh
# /usr/bin/cloud-pc-connect for the native Arch package. The patched SDL3 and
# FreeRDP live in /opt/cloud-pc-connect and are found through their RUNPATH.
: "${AVD_SDL_FREERDP:=/opt/cloud-pc-connect/bin/sdl-freerdp}"
# Empty unless set: the unpackaged default (~/opt/sdl3/lib) would go on
# LD_LIBRARY_PATH and shadow the bundled SDL3.
: "${AVD_SDL_LIBS=}"
: "${AVD_WEBAUTHN_NOTIFICATIONS:=1}"
export AVD_SDL_FREERDP AVD_SDL_LIBS AVD_WEBAUTHN_NOTIFICATIONS
exec /usr/bin/python3 /opt/cloud-pc-connect/share/avd-feed-connect/avd_feed_gui.py "$@"
