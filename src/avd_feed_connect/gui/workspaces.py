"""Workspace placement through Hyprland's IPC socket."""
import os

from gi.repository import GLib


def select_backend(environ=None):
    """The Hyprland backend when its command socket is reachable, else None."""
    socket_path = hyprland_socket(environ)
    return HyprlandWorkspaceBackend(socket_path) if socket_path else None


# ---- Hyprland ---------------------------------------------------------------
# Hyprland's command socket works from inside the sandbox when the manifest
# grants xdg-run/hypr. Windows are matched by the unique /t: title set at
# launch, because PIDs differ across the sandbox boundary.

def hyprland_socket(environ=None):
    environ = os.environ if environ is None else environ
    signature = environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    runtime = environ.get("XDG_RUNTIME_DIR", "")
    if not signature or not runtime or "/" in signature:
        return None
    path = os.path.join(runtime, "hypr", signature, ".socket.sock")
    return path if os.path.exists(path) else None


def hyprctl(command, socket_path=None, timeout=1.0):
    """Send one request to Hyprland's socket and return the reply text."""
    import socket
    path = socket_path or hyprland_socket()
    if not path:
        raise OSError("Hyprland socket not found")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(command.encode())
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks).decode(errors="replace")


def _lua_string(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def hypr_dispatch(lua_call, legacy, socket_path=None):
    """Run a dispatcher; Lua syntax for Hyprland 0.55+, classic syntax before."""
    reply = hyprctl("dispatch " + lua_call, socket_path)
    if reply.strip() == "ok":
        return True
    return hyprctl("dispatch " + legacy, socket_path).strip() == "ok"


def hypr_move_window(address, workspace, socket_path=None):
    return hypr_dispatch(
        "hl.dsp.window.move({ workspace = %s, follow = false, window = %s })"
        % (_lua_string(workspace), _lua_string("address:" + address)),
        "movetoworkspacesilent %s,address:%s" % (workspace, address), socket_path)


def hypr_focus_workspace(workspace, socket_path=None):
    return hypr_dispatch("hl.dsp.focus({ workspace = %s })" % _lua_string(workspace),
                         "workspace %s" % workspace, socket_path)


def hypr_windows(match, socket_path=None, window_class=None):
    """[(address, workspace name)] of client windows whose title contains match."""
    import json
    clients = json.loads(hyprctl("j/clients", socket_path) or "[]")
    return [(c["address"], (c.get("workspace") or {}).get("name", ""))
            for c in clients if match in (c.get("title") or "")
            and (window_class is None or c.get("class") == window_class)]


def valid_workspace(value):
    """Numbered (1-99) or named workspaces; never special or relative ones."""
    text = str(value or "").strip()
    if text.isdigit():
        return 1 <= int(text) <= 99
    return bool(text) and text.startswith("name:") and len(text) <= 64 \
        and all(ch.isalnum() or ch in "-_ :" for ch in text[5:])


class HyprlandPlacement:
    """Move a session's windows to a workspace as soon as they map.

    Starts at launch so the window never shows up on the current workspace,
    then switches to that workspace once the remote desktop has logged on.
    Multi-monitor sessions open several windows; all of them are moved.
    """

    POLL_MS = 150
    MAX_POLLS = 400          # one minute of waiting for the first window
    START_ON_LAUNCH = True

    def __init__(self, proc, title, workspace, follow=True, socket_path=None):
        self.proc = proc
        self.title = title
        self.workspace = str(workspace)
        self.follow = follow
        self.socket_path = socket_path
        self.moved = set()
        self.polls = 0
        self.started = False
        self.connected = False

    def start(self):
        if not self.started:
            self.started = True
            GLib.timeout_add(self.POLL_MS, self._poll)
        return False

    def on_connected(self):
        self.connected = True
        if self.follow and self.moved:
            try:
                hypr_focus_workspace(self.workspace, self.socket_path)
            except (OSError, ValueError):
                pass
        return False

    def _poll(self):
        if self.proc.poll() is not None:
            return False
        self.polls += 1
        try:
            for address, current in hypr_windows(self.title, self.socket_path):
                if address in self.moved:
                    continue
                if current != self.workspace:
                    hypr_move_window(address, self.workspace, self.socket_path)
                self.moved.add(address)
                if self.connected and self.follow:
                    hypr_focus_workspace(self.workspace, self.socket_path)
        except (OSError, ValueError, KeyError):
            pass
        # Keep watching a little after logon for late monitor windows.
        return self.polls < self.MAX_POLLS and not (self.connected and self.polls > 40 and self.moved)


class HyprlandWorkspaceBackend:
    def __init__(self, socket_path=None):
        self.socket_path = socket_path or hyprland_socket()

    def available(self):
        return self.socket_path is not None

    def create_placement(self, proc, title, workspace="", follow=True):
        return HyprlandPlacement(proc, title, workspace, follow, self.socket_path)

    def place_app_window(self, title, window_class, workspace):
        """Move this app's own window; False until the window has mapped."""
        found = hypr_windows(title, self.socket_path, window_class)
        for address, current in found:
            if current != str(workspace):
                hypr_move_window(address, workspace, self.socket_path)
        return bool(found)
