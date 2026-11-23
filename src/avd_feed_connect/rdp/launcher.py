"""Launch a downloaded .rdp with the bundled sdl-freerdp.

The launch used by the command line is a simple, blocking full-screen session
(using the same AAD/NLA selection as the GUI). The GUI uses sdl-freerdp over
a PTY, so it can resolve the connection-time AAD prompt silently — see
:mod:`avd_feed_connect.gui.connect`; this module is the CLI path.
"""

import json
import os
import shlex
import subprocess
import uuid
from datetime import datetime, timezone

from .. import config
from .security import resolve_security
from .resources import rdp_property, set_rdp_dynamic_resolution, set_rdp_multimon


def webauthn_args(path, options=()):
    """Redirect FIDO2 security keys (MS-RDPEWA) unless the .rdp turns it off.

    Feed .rdp files omit ``redirectwebauthn``, so without this FreeRDP never
    opens the channel and the remote session cannot see the key."""
    if any(option.lower().startswith("/dvc:rdpewa") for option in options):
        return []
    if rdp_property(path, "redirectwebauthn") == "0":
        return []
    return ["/dvc:rdpewa"]


def private_argument_file(argv):
    """Keep FreeRDP arguments, including gateway tokens, out of the process list."""
    if any("\n" in arg or "\r" in arg for arg in argv[1:]):
        raise ValueError("FreeRDP arguments cannot contain newlines")
    fd = os.memfd_create("freerdp-arguments", os.MFD_CLOEXEC)
    try:
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(("\n".join(argv[1:]) + "\n").encode())
        os.lseek(fd, 0, os.SEEK_SET)
        return [argv[0], f"/args-from:fd:{fd}"], fd
    except Exception:
        os.close(fd)
        raise


def session_log_path(output_dir, prefix):
    """Name one connection log without reusing earlier sessions' paths."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return os.path.join(output_dir, f"{prefix}_{stamp}_{uuid.uuid4().hex}.log")


def build_display_args(path, extra, want_multimon):
    """Resolve GUI display flags and conflicting settings in the feed file."""
    options = shlex.split(extra)
    dynamic = False
    for option in options:
        if option in ("/dynamic-resolution", "+dynamic-resolution"):
            dynamic = True
        elif option == "-dynamic-resolution":
            dynamic = False
        elif option in ("/multimon", "+multimon", "/multimon:on", "/multimon:force"):
            want_multimon = True
        elif option in ("-multimon", "/multimon:off"):
            want_multimon = False

    if dynamic:
        set_rdp_dynamic_resolution(path)
        want_multimon = False
        options = [option for option in options
                   if option.lstrip("/+-").partition(":")[0]
                   not in ("smart-sizing", "f", "multimon")]

    set_rdp_multimon(path, want_multimon)
    return ([] if dynamic else ["/f"]) + [
        "/multimon" if want_multimon else "-multimon"] + options


# sdl-freerdp has its own shortcuts (Right Shift + Enter toggles fullscreen,
# + D disconnects, ...) that swallow keys meant for the remote PC. Hyprland and
# the app cover those actions, so KMOD_NONE turns every one of them off. The
# setting only exists in FreeRDP's user config file, not on the command line.
HOTKEY_KEYS = ("SDL_Fullscreen", "SDL_Resizeable", "SDL_Grab", "SDL_Disconnect", "SDL_Minimize")


def sdl_config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(config.HOME, ".config")
    return os.path.join(base, "freerdp", "sdl-freerdp.json")


def disable_client_hotkeys(path=None):
    """Turn off sdl-freerdp's shortcuts; keep its other preferences."""
    path = path or sdl_config_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key in HOTKEY_KEYS:
        data.pop(key, None)
    data["SDL_KeyModMask"] = ["KMOD_NONE"]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    except OSError:
        return False
    return True


def build_argv(sdl, path, upn):
    """Assemble the sdl-freerdp command line for a full-screen CLI connection."""
    decision = resolve_security(path)
    argv = [sdl, path, f"/gateway:{config.gateway_arg()}", f"/sec:{decision.mode}"]
    if decision.mode == "nla":
        argv.append("/d:")
    if config.azure_arg():  # sovereign cloud: point AAD at its Entra via /azure
        argv.append(f"/azure:{config.azure_arg()}")
    if upn:
        argv.append(f"/u:{upn}")
    # Pin the host cert on first use by default; AVD_CERT=ignore turns the check
    # off for host pools whose session-host certs rotate.
    cert_flag = "/cert:ignore" if os.environ.get("AVD_CERT", "").strip().lower() \
        == "ignore" else "/cert:tofu"
    argv += ["/sound:sys:pulse", "/microphone", cert_flag,
             "/f", "/scale-desktop:200", "-multimon", "/log-level:info"]
    argv += webauthn_args(path, decision.options)
    return argv


class RdpLauncher:
    """Runs sdl-freerdp for a .rdp file (command-line, blocking)."""

    def __init__(self, sdl=None, sdl_libs=None):
        self.sdl = sdl or config.SDL
        self.sdl_libs = config.SDL_LIBS if sdl_libs is None else sdl_libs

    def launch(self, path, upn=""):
        env = dict(os.environ)
        # Only inject SDL_LIBS when it actually exists (unpackaged builds); inside
        # the Flatpak the loader finds the bundled libs via rpath.
        if self.sdl_libs and os.path.isdir(self.sdl_libs):
            env["LD_LIBRARY_PATH"] = self.sdl_libs + (
                os.pathsep + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
        os.makedirs(config.OUT, exist_ok=True)
        log = session_log_path(config.OUT, "feed")
        argv = build_argv(self.sdl, path, upn)
        print("launching:", " ".join(argv))
        print("log:", log)
        with open(log, "x") as lf:
            return subprocess.call(argv, env=env, stdout=lf, stderr=subprocess.STDOUT)
