#!/usr/bin/env python3
"""Windows-App-style desktop client for Azure Virtual Desktop on Linux.

A GTK4 + WebKitGTK 6.0 front-end over the feed-discovery logic in the
avd_feed_connect package (see its client.AvdClient facade):
  * in-app interactive sign-in (embedded WebKit view — the same auth-code+PKCE
    flow the native client uses, so Conditional Access lets it through; it
    catches the …/oauth2/nativeclient?code=… redirect automatically, no paste).
    The modern WebKit engine renders federated org IdP pages (ADFS/Okta/Ping/…)
    that the old WebKit2GTK 4.1 used to freeze on (issue #1).
  * a tiled workspace grid with the real per-resource icons from the feed
  * double-click / Enter a tile to connect via the bundled sdl-freerdp
  * persistent session (cached workspaces + icons) + silent token refresh +
    sign out. When the tenant's Conditional Access sign-in frequency rejects
    the refresh (AADSTS70043, "every time" = 5 min), the saved grid stays,
    the status says why, and connecting re-authenticates (account picker,
    prompt=select_account — the flow that works everywhere, including tenants
    with Seamless SSO) and then connects on its own.

The connection itself is still sdl-freerdp with /gateway:type:arm /sec:aad, so
camera/mic/gfx behave exactly as before.

A tray icon (gui.tray, StatusNotifierItem over plain D-Bus — GTK4 has no tray
API) lists every resource by workspace with Connect / Connect (windowed) /
Focus window / Disconnect. Opt-in (Settings menu), while a tray host is present:
closing the window hides it to the tray, and the app can start hidden there.
AVD_TRAY=0 turns the tray off.

Runtime: PyGObject with Gtk 4.0 and WebKit 6.0.
"""

import json
import os
import re as _re
import sys
import threading
import time
import urllib.request
import urllib.parse
import hashlib
import hmac
import base64
import secrets
import shutil
import signal
import subprocess
import shlex
import pty
import select
import termios
import uuid
# stdlib HTTP server for the loopback redirect of browser sign-in. Imported by
# name, NOT as `import http.server`: `http` in this module is our own
# avd_feed_connect.http (see below), and `import http.server` would shadow it.
from http.server import BaseHTTPRequestHandler, HTTPServer

# Software-composite WebKit (our sign-in webview) instead of the DMABUF/GBM
# GPU path. That path fails to allocate a GBM buffer on some GPUs/drivers
# (notably under XWayland, and after repeated webviews
# exhaust buffers), leaving the sign-in window BLANK. Must be set before WebKit
# initializes its renderer. setdefault so a user can still force the GPU path.
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

APP_ID = "io.github.roddolf.CloudPcConnect"

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GLib  # noqa: E402
# The X11 window class comes from the program name, which is otherwise
# "python3". Use the app id so docks match the .desktop file and window
# managers can target the window with rules. Must run before GTK initializes.
GLib.set_prgname(APP_ID)
from gi.repository import Gtk, Gdk, GdkPixbuf, Gio, GObject, Pango  # noqa: E402
from gi.repository import WebKit  # noqa: E402

# The feed/auth package is installed as a sibling directory next to this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from avd_feed_connect import config, display, http  # noqa: E402
from avd_feed_connect.auth.oauth import _b64url  # noqa: E402
from avd_feed_connect.client import AvdClient  # noqa: E402
from avd_feed_connect.gui.workspaces import select_backend as workspace_backend  # noqa: E402
from avd_feed_connect.gui.workspaces import HyprlandWorkspaceBackend, valid_workspace  # noqa: E402
from avd_feed_connect.gui import resource_groups  # noqa: E402
from avd_feed_connect.gui import launchers, omarchy as omarchy_theme  # noqa: E402
from avd_feed_connect.rdp.security import resolve_security  # noqa: E402
from avd_feed_connect.rdp.resources import local_resource, RdpDownloadError  # noqa: E402
from avd_feed_connect.rdp.attempt import ConnectionAttempt  # noqa: E402
from avd_feed_connect.rdp.launcher import (  # noqa: E402
    build_display_args, disable_client_hotkeys, drive_redirect_args,
    private_argument_file, resolve_share_mode, session_log_path,
    webauthn_args)
from avd_feed_connect.rdp.resources import (  # noqa: E402
    rdp_is_remoteapp, set_rdp_desktop_mode)
from avd_feed_connect.i18n import _ as tr, ngettext, configure, LANGUAGES  # noqa: E402
from avd_feed_connect.gui.localization import bind, create, strings  # noqa: E402
from avd_feed_connect.gui.theme import stylesheet  # noqa: E402
from avd_feed_connect.gui import omarchy  # noqa: E402
from avd_feed_connect.gui.demo import demo_resources  # noqa: E402
from avd_feed_connect.gui import storage, tray_menu, x11focus  # noqa: E402
from avd_feed_connect.gui.tray import TrayIcon  # noqa: E402

from avd_feed_connect.accounts import (  # noqa: E402
    ProfileRegistry, profile_dir, claims_from_token, LOCAL_PROFILE_ID)
from avd_feed_connect.auth.tokens import TokenStore  # noqa: E402
from avd_feed_connect.gui.session import AccountSession  # noqa: E402

APP_NAME = "Cloud PC Connect"
HYPR_WORKSPACES = list(range(1, 11))
APP_VERSION = "0.1.0"
# LOCAL_PROFILE_ID (the synthetic account for user-opened .rdp files, issue #17)
# is imported from avd_feed_connect.accounts, its single source of truth.


def _url_path(uri):
    """Scheme, host and path of a URL, for logs: the query can hold codes."""
    parts = urllib.parse.urlsplit(uri or "")
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _redirect_error(uri):
    """(error, first line of error_description) from an Entra error redirect."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(uri or "").query)
    error = (query.get("error") or ["unknown_error"])[0]
    detail = (query.get("error_description") or [""])[0]
    return error, detail.strip().splitlines()[0].strip() if detail.strip() else error


def is_broker_redirect(uri):
    try:
        parsed = urllib.parse.urlsplit(uri)
        expected = urllib.parse.urlsplit(config.BROKER_REDIRECT)
    except ValueError:
        return False
    return (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path) == (
        expected.scheme.lower(), expected.netloc.lower(), expected.path)


def fido_error_message(code, site):
    """Explain a failed in-session security-key request (libfido2 error code)."""
    site = site if site and site != "-" else None
    if code == 0x2E:
        if site:
            return tr("This security key has no passkey for {site}. Use the key you "
                      "registered, or sign in another way.").format(site=site)
        return tr("This security key has no passkey for this sign-in. Use the key you "
                  "registered, or sign in another way.")
    if code == 0x31:
        return tr("Wrong security key PIN.")
    if code == 0x32:
        return tr("Your security key PIN is blocked. Reset the key to use it again.")
    if code == 0x34:
        return tr("Too many wrong PINs. Remove and reinsert your security key, then try again.")
    if code in (0x35, 0x36):
        return tr("Set a PIN on your security key first.")
    if code in (0x3C, 0x3F):
        return tr("Your security key could not verify you.")
    if code in (0x2F, 0x3A):
        return tr("Your security key was not touched in time.")
    if code == 0x19:
        return tr("This security key is already registered.")
    if code in (0x27, 0x30):
        return tr("Your security key denied the request.")
    return tr("The security key request failed.")


def sign_in_actions(win, on_cancel, on_in_app=None):
    """Bottom-right Cancel (plus an optional way back to in-app sign-in) for a
    header-less sign-in window, which otherwise has no visible close control.
    Escape cancels too."""
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    row.set_halign(Gtk.Align.END)
    row.set_margin_top(4)
    if on_in_app is not None:
        in_app = create(Gtk.Button, label=tr("Use in-app sign-in"))
        in_app.add_css_class("flat")
        in_app.connect("clicked", lambda *_: on_in_app())
        row.append(in_app)
    cancel = create(Gtk.Button, label=tr("Cancel"))
    cancel.connect("clicked", lambda *_: on_cancel())
    row.append(cancel)
    keys = Gtk.ShortcutController()
    keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape"),
                                       Gtk.CallbackAction.new(lambda *_: (on_cancel(), True)[1])))
    win.add_controller(keys)
    return row


class _LoopbackHandler(BaseHTTPRequestHandler):
    """Catches the one OAuth redirect from the system browser. The browser hits
    http://127.0.0.1:<port>/?code=… after sign-in; we hand the query back to the
    app and show a 'you can close this tab' page. Only localhost reaches it (the
    socket is bound to 127.0.0.1), and it fires its callback exactly once."""

    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" not in qs and "error" not in qs:
            self.send_response(204)       # ignore favicon / stray probes
            self.end_headers()
            return
        body = self.server._done_html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass
        self.server._on_code(qs)

    def log_message(self, *a):
        pass



class AvdApp(Gtk.Application):
    def __init__(self):
        # Demo/screenshot runs skip D-Bus single-instance registration entirely
        # (NON_UNIQUE), so they always run standalone and never hand off to (or
        # collide with) the installed app.
        if os.environ.get("AVD_DEMO"):
            super().__init__(application_id=APP_ID,
                             flags=Gio.ApplicationFlags.NON_UNIQUE)
        else:
            # HANDLES_OPEN so the OS can hand this running instance the
            # ms-appx-web://…?code=… sign-in redirect (see do_open); GApplication
            # routes that second invocation to the primary instance over D-Bus.
            super().__init__(application_id=APP_ID,
                             flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.registry = ProfileRegistry()
        if not os.environ.get("AVD_DEMO"):
            self.registry.migrate_legacy(TokenStore())
        self.accounts = {}
        for profile in self.registry.profiles:
            # A single malformed profile (e.g. a bad id) must not stop the app
            # from starting with the rest of the accounts.
            try:
                self.accounts[profile['id']] = AccountSession(
                    profile, self.registry.client(profile['id']),
                    profile_dir(self.registry.root, profile['id']))
            except (ValueError, KeyError, OSError):
                continue
        self.selected_id = self.registry.selected_id
        self._adding = None
        self.win = None
        self.stack = None
        self.grid = None
        self.status = None
        self._account_sections = {}
        self._account_status_widgets = {}
        self._kind_boxes = []       # one per Cloud PCs / desktops / apps section
        self._tiles = []            # GTK4 FlowBox has no get_children(); track ours
        self._signout_item = None
        self._scale = 1             # client display scale factor (1 or 2 = HiDPI)
        self._n_monitors = 1        # how many monitors the compositor reports
        self._settings = storage.load_settings()   # application-wide defaults and appearance
        if storage.tidy_extra_args(self._settings):
            storage.save_settings(self._settings)
        self.view_mode = "all" if self._settings.get("_account_view") == "all" else "account"
        configure(self._settings.get("_language", "system"))
        self._signin_dlg = None      # the open sign-in window, if any
        self._pending_scheme = None  # (state, cb) awaiting an ms-appx-web redirect
        self._conn_scheme_waiters = []  # connection-time AAD prompts awaiting a scheme redirect
        self._toast_box = None        # transient bottom-centre notice (see _toast)
        self._toast_label = None
        self._toast_source = 0        # timeout that auto-hides the toast
        self._connecting = set()      # (account id, resource id) currently connecting
        self._tray = None             # TrayIcon, unless AVD_TRAY=0 or no session bus
        self._sessions = {}           # (account id, resource id) → connection state
        self._tray_toggles = []       # settings-menu tray options (need a tray host)
        self._start_hidden_source = 0  # timeout giving up on "start in the tray"

    def _set_language(self, preference):
        self._changing_language = True
        try:
            configure(preference)
            self._rebuild_account_menu()
            self._tray_refresh()
        finally:
            self._changing_language = False

    # ---- app lifecycle ----------------------------------------------------
    def do_open(self, files, n_files, hint):
        """Handle a launch that carries a URI. The only URIs we register for are
        the ms-appx-web:// sign-in redirect (x-scheme-handler/ms-appx-web): the
        browser is handed the final ?code=… URL by Entra, the OS routes it to
        this already-running instance, and we deliver it to the waiting browser
        sign-in. Anything else just surfaces the app."""
        delivered = False
        for f in files:
            try:
                uri = f.get_uri()
            except Exception:
                uri = None
            target = launchers.parse_connect_uri(uri)
            if target:
                self._connect_from_launcher(*target)
                delivered = True
            elif uri and is_broker_redirect(uri):
                delivered = (self._deliver_scheme_code(uri)
                             or self._deliver_scheme_conn(uri) or delivered)
        if not delivered:
            self.do_activate()

    def _connect_from_launcher(self, account_id, resource_id):
        """Open a Cloud PC from its app launcher entry."""
        self.do_activate()
        session = self.accounts.get(account_id)
        if session is None or session.removed:
            self._error(tr("This launcher belongs to an account that is no longer signed in."))
            return
        if session is not self.current and self.view_mode != "all":
            self._select_account(account_id)
        child = session.tiles.get(resource_id)
        if child is not None:
            self._on_tile_activated(None, child)
        else:
            session.pending_launch = (resource_id, False)

    def _scheme_connect(self, session):
        """Whether this session's connection-time AAD should auto-capture via the
        ms-appx-web scheme handler (no paste) instead of the native-client paste.
        Default on for the sovereign clouds, where the broker redirect is the one
        registered redirect (loopback/nativeclient are not) and a Gov tester has
        confirmed the full no-paste connect. Commercial signs in inside the app
        (native-client redirect, captured by the embedded WebView) unless the
        system-browser preference or AVD_SCHEME_SIGNIN selects the browser. A
        local .rdp keeps its own flow (it may carry a baked-in gateway token and
        has no feed token to inject)."""
        if session.local:
            return False
        force = os.environ.get("AVD_SCHEME_SIGNIN", "").strip().lower() \
            in ("1", "true", "yes", "on")
        return force or config.CLOUD != "commercial" or self._use_browser_signin()

    @staticmethod
    def _inject_bearer(session):
        """Whether the gateway bearer token we already hold is handed to FreeRDP,
        which skips its own gateway 'Browse to:' prompt. Any feed account has
        one; a local .rdp keeps its own (possibly baked-in) gateway token."""
        return bool(session.token) and not session.local

    def _deliver_scheme_conn(self, uri):
        """Route a caught ms-appx-web connection redirect to the most recent live
        connection-time AAD prompt. Its ``finish`` writes the URL to FreeRDP's
        pty, exactly as a manual paste would, so no code races FreeRDP's reader."""
        if not is_broker_redirect(uri):
            return False
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)
        except Exception:
            return False
        states = qs.get("state", [])
        if len(states) != 1 or ("code" not in qs and "error" not in qs):
            return False
        for waiter in reversed(self._conn_scheme_waiters[:]):
            attempt = waiter.get("attempt")
            if attempt is not None and not attempt.active():
                self._conn_scheme_waiters.remove(waiter)
                continue
            if not hmac.compare_digest(states[0].encode(), waiter["state"].encode()):
                continue
            self._conn_scheme_waiters.remove(waiter)
            if self.win:
                self.win.present()
            self._toast(tr("Signed in. You can close the browser tab that's "
                           "still loading."))
            GLib.idle_add(waiter["finish"], uri)
            return True
        return False

    def _deliver_scheme_code(self, uri):
        """Route a caught ms-appx-web://…?code=… back to the pending browser
        sign-in. Returns True if a waiting sign-in accepted it. The state must
        match the one we sent, so a stray or foreign redirect is ignored."""
        pending = self._pending_scheme
        if not pending or not is_broker_redirect(uri):
            return False
        want_state, cb = pending
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)
        except Exception:
            return False
        if "code" not in qs and "error" not in qs:
            return False
        if qs.get("state", [None])[0] != want_state:
            return False
        self._pending_scheme = None
        # The OS routed this redirect to us while the browser was in front, and
        # the browser tab is now stuck spinning (a custom scheme has no landing
        # page). Pull our window back to the front and, once the feed is up,
        # float a notice so the dead tab reads as "done" rather than "broken".
        if self.win:
            self.win.present()
        if "code" in qs:
            self._toast(tr("Signed in. You can close the browser tab that's "
                           "still loading."))
        GLib.idle_add(cb, qs)
        return True

    def do_activate(self):
        if self.win:
            self.win.present()
            return
        self._build_ui()
        # "Start in the tray" only applies to a saved session (a first run must
        # show sign-in) and only once a tray host accepts the icon; otherwise
        # the window is shown after a short wait, as it would have been.
        start_hidden = bool(self._settings.get("_start_in_tray")
                            and not os.environ.get("AVD_DEMO")
                            and bool(self.current and self.current.resources))
        if not start_hidden:
            self.win.present()
            self._place_main_window()
        self._detect_displays()
        self._start_tray()
        if start_hidden:
            if self._tray:
                self._start_hidden_source = GLib.timeout_add_seconds(
                    3, self._start_hidden_timeout)
            else:
                self.win.present()
        # Dev/screenshot mode: show a fixed, anonymous sample feed (no network,
        # no sign-in). Used for documentation screenshots so they carry no real
        # account or org details. Never triggered in normal use.
        if os.environ.get("AVD_DEMO"):
            profile = {'id': uuid.uuid4().hex, 'tid': 'demo', 'oid': 'demo', 'upn': 'alex@contoso.com'}
            demo_session = AccountSession(profile, AvdClient(), profile_dir(self.registry.root, profile['id']))
            demo_session.client.upn = profile['upn']
            self.accounts[profile['id']] = demo_session
            self.selected_id = profile['id']
            demo = demo_resources()
            self._populate(demo)
            self._set_status(tr("Signed in as {account}").format(account="alex@contoso.com"))
            self._set_tile_state("d2", "state-connected")
            return
        # If we have previously discovered workspaces, show them immediately and
        # refresh the token + feed silently in the background (Windows-App-style
        # persistent session). Only a first run with no cache shows sign-in.
        if self.current:
            self._select_account(self.selected_id)
        else:
            self._show("signin")

    def _place_main_window(self):
        """Move the main window to its configured Hyprland workspace once mapped."""
        workspace = self._settings.get("_window_workspace", "")
        backend = workspace_backend()
        if not valid_workspace(workspace) or not isinstance(backend, HyprlandWorkspaceBackend):
            return
        tries = [0]
        def place():
            tries[0] += 1
            try:
                if backend.place_app_window(APP_NAME, APP_ID, workspace):
                    return GLib.SOURCE_REMOVE
            except (OSError, ValueError):
                return GLib.SOURCE_REMOVE
            return tries[0] < 20
        GLib.timeout_add(100, place)

    @property
    def current(self):
        return self.accounts.get(self.selected_id)

    def _visible(self, session):
        return not session.removed and (self.view_mode == "all" or session is self.current)

    def _select_account(self, profile_id):
        session = self.accounts.get(profile_id)
        if not session or session.removed:
            return
        self.selected_id = profile_id
        if not os.environ.get("AVD_DEMO"):
            self.registry.select(profile_id)
        self._update_account(session.client.upn, session)
        self._rebuild_account_menu()
        if self.view_mode == "all":
            self._render_resources()
            for owner in self.accounts.values():
                if not owner.removed:
                    threading.Thread(target=self._background_refresh, args=(owner,), daemon=True).start()
            return
        self._clear_tiles()
        if session.resources or session.local:
            self._populate(session.resources, session)
        else:
            self._show("busy", session)
        self._set_connecting("", "", False, session)
        threading.Thread(target=self._background_refresh, args=(session,), daemon=True).start()

    def _rebuild_account_menu(self):
        if not hasattr(self, '_account_rows'):
            return
        multiple = sum(not owner.removed for owner in self.accounts.values()) > 1
        self._view_modes.set_visible(multiple)
        if not multiple and self.view_mode == "all":
            self.view_mode = "account"
            self._settings["_account_view"] = "account"
            storage.save_settings(self._settings)
            if hasattr(self, "_resource_box"):
                self._render_resources()
        for mode, button in self._view_buttons.items():
            button.set_active(mode == self.view_mode)
        row = self._account_rows.get_first_child()
        while row:
            self._account_rows.remove(row)
            row = self._account_rows.get_first_child()
        for session in self.accounts.values():
            if session.removed:
                continue
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            button = create(Gtk.Button, )
            button.add_css_class("flat")
            if session.id == self.selected_id:
                button.add_css_class("current-account")
            button.set_hexpand(True)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            name = session.display_name
            title = create(Gtk.Label, label=("✓ " if session.id == self.selected_id else "") + name, xalign=0)
            title.set_ellipsize(3)
            title.set_max_width_chars(36)
            title.add_css_class("account-name")
            email = create(Gtk.Label, label=session.client.upn, xalign=0)
            email.set_ellipsize(3)
            email.set_max_width_chars(36)
            email.add_css_class("account-email")
            text.append(title)
            text.append(email)
            button.set_child(text)
            button.connect("clicked", lambda _, sid=session.id: (
                self._account_pop.popdown(), self._select_account(sid)))
            edit = Gtk.Button.new_from_icon_name("document-edit-symbolic")
            edit.add_css_class("flat"); edit.add_css_class("row-edit")
            bind(edit, 'tooltip_text', tr("Edit account name"))
            edit.connect("clicked", lambda _, owner=session: (
                self._account_pop.popdown(), self._edit_account_name(owner)))
            row.append(button)
            row.append(edit)
            self._account_rows.append(row)
        self._account_separator.set_visible(self._account_rows.get_first_child() is not None)
        self._signout_item.set_sensitive(self.current is not None)

    def _edit_account_name(self, session):
        if session.removed:
            return
        dialog = create(Gtk.Window, title=tr("Edit account name"), transient_for=self.win)
        dialog.add_css_class("avd-settings")
        dialog.set_default_size(380, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for margin in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{margin}")(16)
        entry = Gtk.Entry()
        bind(entry, 'text', session.profile.get('alias', ''))
        bind(entry, 'placeholder_text', session.display_name)
        box.append(create(Gtk.Label, label=session.client.upn, xalign=0))
        box.append(entry)
        box.append(create(Gtk.Label, label=tr("Leave empty to use the email domain."),
                             xalign=0, wrap=True))
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = create(Gtk.Button, label=tr("Cancel"))
        cancel.connect("clicked", lambda *_: dialog.destroy())
        save = create(Gtk.Button, label=tr("Save"))
        save.add_css_class("suggested-action")

        def apply(*_):
            if not session.removed:
                try:
                    self.registry.rename(session.id, entry.get_text())
                except OSError as error:
                    self._error(tr("Could not save account name: {error}").format(error=str(error)))
                    return
                self._rebuild_account_menu()
                if session is self.current:
                    self._update_account(session.client.upn)
                self._tray_refresh()
                if self.view_mode == "all":
                    self._render_resources()
            dialog.destroy()

        save.connect("clicked", apply)
        entry.connect("activate", apply)
        buttons.append(cancel)
        buttons.append(save)
        box.append(buttons)
        dialog.set_child(box)
        dialog.set_default_widget(save)
        self._light_dismiss(dialog)
        dialog.present()

    def _add_account(self, browser=None, remember=False):
        if self._adding:
            return
        profile = {'id': uuid.uuid4().hex, 'tid': '', 'oid': '', 'upn': ''}
        session = AccountSession(profile, self.registry.client(profile['id']),
                                 profile_dir(self.registry.root, profile['id']))
        session.client.upn = ""
        self._adding = session
        self._interactive_signin(session, browser=browser, remember=remember)

    def _discard_add(self, session):
        if session is self._adding:
            session.close()
            session.web_session = None
            shutil.rmtree(session.directory, ignore_errors=True)
            self._adding = None

    def _detect_displays(self):
        """Read the client's real display layout from the compositor so the
        remote session matches it automatically (like the Windows App does):
        number of monitors → single vs multi-monitor, and the HiDPI scale."""
        try:
            mons = Gdk.Display.get_default().get_monitors()
            self._n_monitors = max(1, mons.get_n_items())
        except Exception:
            mons, self._n_monitors = None, 1
        try:
            if self.win.get_mapped() or not mons:
                self._scale = self.win.get_scale_factor() or 1
            else:   # started hidden in the tray: no window yet, ask the monitor
                self._scale = mons.get_item(0).get_scale_factor() or 1
        except Exception:
            self._scale = 1

    def _background_refresh(self, session):
        if session.local:
            return            # no feed to refresh; opened .rdp tiles are shown as-is
        if not self._do_refresh(True, session):
            if session.resources:
                self._set_status(self._refresh_failed_status(session), session, reauth=True)
            else:
                self._show("signin", session)
            return
        self._load_feed(session)

    def _is_dark(self):
        ov = os.environ.get("AVD_THEME", "").strip().lower()
        if ov in ("dark", "light"):
            return ov == "dark"
        pref = (self._settings.get("_theme") or "system")   # in-app choice
        if pref == "light":
            return False
        if pref == "dark":
            return True
        tokens = getattr(self, "_omarchy", None)
        if tokens:
            return tokens["dark"]
        s = Gtk.Settings.get_default()
        try:
            if s.get_property("gtk-application-prefer-dark-theme"):
                return True
        except Exception:
            pass
        try:
            return "dark" in (s.get_property("gtk-theme-name") or "").lower()
        except Exception:
            return False

    def _apply_theme(self, *_):
        tokens = getattr(self, "_omarchy", None)
        self._css_provider.load_from_string(stylesheet(self._is_dark(), tokens))

    def _on_omarchy_changed(self):
        """Live Omarchy theme/font switch: re-read the tokens, restyle in place."""
        tokens = omarchy.load()
        if tokens != self._omarchy:
            self._omarchy = tokens
            self._apply_theme()

    def _set_theme(self, key):
        """Persist the appearance choice (system/light/dark) and apply it live."""
        self._settings["_theme"] = key
        storage.save_settings(self._settings)
        self._apply_theme()

    def _build_ui(self):
        self._css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._omarchy = omarchy.load()
        self._theme_watch = (omarchy.ThemeWatcher(self._on_omarchy_changed).start()
                             if self._omarchy else None)
        self._apply_theme()
        st = Gtk.Settings.get_default()
        for prop in ("notify::gtk-application-prefer-dark-theme", "notify::gtk-theme-name"):
            st.connect(prop, self._apply_theme)

        self.win = create(Gtk.ApplicationWindow, application=self, title=APP_NAME)
        self.win.add_css_class("avd")
        self.win.set_default_size(820, 600)
        self.win.connect("close-request", self._on_close_request)
        self.win.connect("notify::is-active", self._on_window_active_changed)
        outside = Gtk.GestureClick()
        outside.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        outside.connect("pressed", self._on_menu_outside_pressed)
        self.win.add_controller(outside)
        keys = Gtk.ShortcutController()
        for trigger, handler in (("<Control>r", self._on_refresh_shortcut),
                                 ("F5", self._on_refresh_shortcut),
                                 ("slash", self._on_filter_shortcut),
                                 ("<Control>f", self._on_filter_shortcut),
                                 ("question", self._on_shortcuts_shortcut)):
            keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trigger),
                                               Gtk.CallbackAction.new(handler)))
        self.win.add_controller(keys)

        # ---- header bar: brand (left) · refresh + account (right) -----------
        hb = Gtk.HeaderBar()
        hb.add_css_class("avd-header")
        hb.set_title_widget(create(Gtk.Label, ))     # blank the centered window title
        self.win.set_titlebar(hb)

        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        # The mark IS the image (a Gtk.Image always draws its icon centered in
        # its allocation), sized/tinted by the .mark CSS — no box wrapper to
        # mis-align or expand.
        mark = Gtk.Image.new_from_icon_name("computer-symbolic")
        mark.set_pixel_size(14); mark.add_css_class("mark"); mark.set_valign(Gtk.Align.CENTER)
        tbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tbox.set_valign(Gtk.Align.CENTER)      # center the title block next to the logo
        t1 = create(Gtk.Label, label="Cloud PC Connect", xalign=0); t1.add_css_class("brand-title")
        t2 = create(Gtk.Label, label="Azure Virtual Desktop · Windows 365", xalign=0)
        t2.add_css_class("brand-sub")
        tbox.append(t1); tbox.append(t2)
        brand.append(mark); brand.append(tbox)
        brand.set_valign(Gtk.Align.CENTER)
        hb.pack_start(brand)

        shortcuts = Gtk.MenuButton(icon_name="help-about-symbolic")
        shortcuts.add_css_class("iconbtn")
        bind(shortcuts, 'tooltip_text', tr("Keyboard shortcuts"))
        shortcut_pop = Gtk.Popover()
        shortcut_pop.add_css_class("avd-menu")
        shortcut_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                               margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        for label in (tr("Arrow keys: move between resources"),
                      tr("Enter: connect"), tr("Menu or Shift+F10: resource actions"),
                      tr("/ or Ctrl+F: search"),
                      tr("Ctrl+R or F5: refresh"), tr("Escape: clear search")):
            shortcut_box.append(create(Gtk.Label, label=label, xalign=0))
        shortcut_pop.set_child(shortcut_box)
        shortcuts.set_popover(shortcut_pop)
        self._shortcuts_button = shortcuts
        hb.pack_end(shortcuts)

        # account menu button (avatar + who + caret), opens the saved account list
        menu_btn = Gtk.MenuButton(); menu_btn.add_css_class("acct-btn")
        acctbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        # The avatar IS the label (a Gtk.Label centers its own text), sized/
        # tinted by the .avatar CSS — no box wrapper.
        self._avatar_lbl = create(Gtk.Label, label="?"); self._avatar_lbl.add_css_class("avatar")
        self._avatar_lbl.set_valign(Gtk.Align.CENTER)
        self._acct_who = create(Gtk.Label, label=tr("Sign in")); self._acct_who.add_css_class("acct-who")
        self._acct_who.set_ellipsize(3); self._acct_who.set_max_width_chars(24)
        caret = Gtk.Image.new_from_icon_name("pan-down-symbolic"); caret.add_css_class("caret")
        acctbox.append(self._avatar_lbl); acctbox.append(self._acct_who); acctbox.append(caret)
        menu_btn.set_child(acctbox)

        # Avoid an input grab that can deactivate the parent on touch input.
        # Outside presses and parent deactivation dismiss these menus explicitly.
        pop = Gtk.Popover(autohide=False)
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for m in ("top", "bottom", "start", "end"):
            getattr(pbox, f"set_margin_{m}")(6)

        # Appearance: System / Light / Dark (remembered), applied live.
        appr = create(Gtk.Label, label=tr("APPEARANCE"), xalign=0); appr.add_css_class("menu-head")
        appr.set_margin_start(4); appr.set_margin_top(2)
        pbox.append(appr)
        seg = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, homogeneous=True)
        seg.add_css_class("linked"); seg.set_margin_bottom(4)
        cur_theme = self._settings.get("_theme") or "system"
        first = None
        for key, label in (("system", tr("System")), ("light", tr("Light")), ("dark", tr("Dark"))):
            b = create(Gtk.ToggleButton, label=label)
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.set_active(key == cur_theme)
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self._set_theme(k))
            seg.append(b)
        pbox.append(seg)
        # Language lives in the Default settings dialog: a GtkDropDown opens its
        # own popup, which fights this non-autohide popover's outside-click
        # dismisser (the menu just closed on click). A real window has no such
        # conflict and a dropdown scales to many languages. This row opens it.
        language_btn = create(Gtk.Button, label=tr("Language…")); language_btn.add_css_class("flat")
        language_btn.connect("clicked",
                             lambda *_: (self._settings_pop.popdown(), self._open_settings(None)))
        pbox.append(language_btn)
        pbox.append(Gtk.Separator())

        # System tray options (off by default, so closing still quits unless
        # asked). Insensitive until a tray host shows our icon.
        trayh = create(Gtk.Label, label=tr("SYSTEM TRAY"), xalign=0); trayh.add_css_class("menu-head")
        trayh.set_margin_start(4); trayh.set_margin_top(2)
        pbox.append(trayh)
        for key, label in (("_close_to_tray", tr("Keep running in the tray when closed")),
                           ("_start_in_tray", tr("Start in the tray"))):
            cb = create(Gtk.CheckButton, label=label)
            cb.set_active(bool(self._settings.get(key)))
            cb.set_sensitive(False)
            bind(cb, 'tooltip_text', tr("Needs a system tray on your desktop"))
            cb.connect("toggled", lambda btn, k=key: self._set_tray_option(k, btn.get_active()))
            pbox.append(cb)
            self._tray_toggles.append(cb)
        pbox.append(Gtk.Separator())

        setbtn = create(Gtk.Button, label=tr("Default settings…")); setbtn.add_css_class("flat")
        setbtn.connect("clicked", lambda *_: (settings_pop.popdown(), self._open_settings(None)))
        pbox.append(setbtn)
        settings_pop = pop
        settings_pop.add_css_class("avd-menu")
        self._settings_pop = settings_pop
        settings_btn = Gtk.MenuButton(icon_name="emblem-system-symbolic")
        settings_btn.add_css_class("iconbtn")
        bind(settings_btn, 'tooltip_text', tr("Settings"))
        settings_pop.set_child(pbox)
        settings_btn.set_popover(settings_pop)
        settings_pop.set_has_arrow(False)
        for item in (language_btn, setbtn):
            item.get_child().set_xalign(0)
        # Avoid an input grab that can deactivate the parent on touch input.
        # Outside presses and parent deactivation dismiss these menus explicitly.
        pop = Gtk.Popover(autohide=False)
        self._account_pop = pop
        pop.add_css_class("avd-menu")
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for margin in ("top", "bottom", "start", "end"):
            getattr(pbox, f"set_margin_{margin}")(6)
        self._view_modes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._view_modes.add_css_class("view-modes")
        previous = None
        self._view_buttons = {}
        for mode, label in (("account", tr("By account")), ("all", tr("All accounts"))):
            button = create(Gtk.ToggleButton, label=label, hexpand=True)
            if previous:
                button.set_group(previous)
            button.set_active(self.view_mode == mode)
            button.connect("toggled", lambda control, value=mode:
                           self._set_view_mode(value) if control.get_active() else None)
            self._view_buttons[mode] = button
            self._view_modes.append(button)
            previous = button
        pbox.append(self._view_modes)
        self._account_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        pbox.append(self._account_rows)
        self._account_separator = Gtk.Separator()
        pbox.append(self._account_separator)
        add = create(Gtk.Button, label=tr("Add account"))
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: (pop.popdown(), self._add_account()))
        pbox.append(add)
        openrdp = create(Gtk.Button, label=tr("Open a downloaded .rdp…"))
        openrdp.add_css_class("flat")
        openrdp.connect("clicked", lambda *_: (pop.popdown(), self._open_local_rdp()))
        pbox.append(openrdp)
        pbox.append(Gtk.Separator())
        signout = create(Gtk.Button, label=tr("Sign out")); signout.add_css_class("flat")
        signout.set_sensitive(False)
        signout.connect("clicked", lambda *_: (pop.popdown(), self._sign_out()))
        self._signout_item = signout
        pbox.append(signout)
        pop.set_child(pbox)
        pop.set_has_arrow(False)
        for item in (add, openrdp, signout):
            item.get_child().set_xalign(0)
        menu_btn.set_popover(pop)

        self.refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_btn.add_css_class("iconbtn")
        bind(self.refresh_btn, 'tooltip_text', tr("Refresh workspaces"))
        self.refresh_btn.connect("clicked", lambda *_: self._reload_feed())

        hb.pack_end(menu_btn)
        hb.pack_end(settings_btn)
        self._rebuild_account_menu()
        hb.pack_end(self.refresh_btn)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_css_class("avd-page")
        self.win.set_child(self.stack)

        # ---- sign-in hero ---------------------------------------------------
        signin = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        signin.add_css_class("avd-page")
        signin.set_valign(Gtk.Align.CENTER); signin.set_halign(Gtk.Align.CENTER)
        bm = Gtk.Image.new_from_icon_name("computer-symbolic"); bm.set_pixel_size(26)
        bm.add_css_class("bigmark"); bm.set_margin_bottom(18); bm.set_halign(Gtk.Align.CENTER)
        h1 = create(Gtk.Label, label=tr("Sign in to your workspaces")); h1.add_css_class("hero-title")
        h2 = create(Gtk.Label, label=tr("Connect your Microsoft work account to see the desktops "
                             "and apps you're entitled to."))
        h2.add_css_class("hero-sub"); h2.set_wrap(True); h2.set_justify(Gtk.Justification.CENTER)
        h2.set_max_width_chars(38); h2.set_margin_top(4)
        btn = create(Gtk.Button, label=tr("Sign in with Microsoft")); btn.add_css_class("msbtn")
        btn.set_halign(Gtk.Align.CENTER); btn.set_margin_top(20)
        btn.connect("clicked", lambda *_: self._interactive_signin())
        browser_btn = create(Gtk.Button, label=tr("Sign in with a browser instead"))
        browser_btn.add_css_class("flat"); browser_btn.set_halign(Gtk.Align.CENTER)
        browser_btn.set_margin_top(6)
        browser_btn.connect("clicked", lambda *_: self._start_browser_signin())
        browser_sub = create(Gtk.Label, label=tr("For smart card or certificate sign-in"))
        browser_sub.add_css_class("hero-note"); browser_sub.set_margin_top(2)
        openrdp_link = create(Gtk.Button, label=tr("Open a downloaded .rdp instead"))
        openrdp_link.add_css_class("flat"); openrdp_link.set_halign(Gtk.Align.CENTER)
        openrdp_link.set_margin_top(12)
        openrdp_link.connect("clicked", lambda *_: self._open_local_rdp())
        openrdp_sub = create(Gtk.Label, label=tr("If your organization blocks sign-in on Linux"))
        openrdp_sub.add_css_class("hero-note"); openrdp_sub.set_margin_top(2)
        note = create(Gtk.Label, label=tr("Unofficial client · not affiliated with or endorsed by Microsoft"))
        note.add_css_class("hero-note"); note.set_margin_top(24)
        for w in (bm, h1, h2, btn, browser_btn, browser_sub,
                  openrdp_link, openrdp_sub, note):
            signin.append(w)
        self.stack.add_named(signin, "signin")

        # ---- workspaces page ------------------------------------------------
        wp = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wp.add_css_class("avd-page")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        for m, v in (("top", 14), ("start", 22), ("end", 22), ("bottom", 2)):
            getattr(head, f"set_margin_{m}")(v)
        head.set_spacing(8)
        # Search ("/" focuses it, Esc clears it) plus one drop-down per facet.
        # Drop-down choices persist in settings.json under "_filters".
        self._filter = create(Gtk.SearchEntry, placeholder_text=tr("Search resources…"))
        self._filter.add_css_class("filter")
        self._filter.set_hexpand(True)
        self._filter.set_valign(Gtk.Align.CENTER)
        self._filter.connect("search-changed", lambda *_: self._apply_filter())
        self._filter.connect("stop-search", lambda *_: self._close_filter())
        self._filter.connect("activate", lambda *_: self._focus_first_tile())
        head.append(self._filter)
        self._facets = {}
        saved_filters = self._settings.get("_filters", {})
        for facet, all_label in (("kind", "All types"), ("region", "All regions"),
                                 ("workspace", "All workspaces"), ("app", "All apps")):
            dropdown = Gtk.DropDown(model=Gtk.StringList.new([tr(all_label)]))
            dropdown.add_css_class("facet")
            dropdown.set_valign(Gtk.Align.CENTER)
            dropdown.set_factory(self._facet_factory(14))
            dropdown.set_list_factory(self._facet_factory(0))
            dropdown._all_label = all_label
            dropdown._values = [resource_groups.ALL]
            dropdown._wanted = saved_filters.get(facet, resource_groups.ALL)
            dropdown.connect("notify::selected", self._on_facet_changed)
            bind(dropdown, 'tooltip_text', tr({"kind": "Resource type", "region": "Region",
                                               "workspace": "Workspace", "app": "App"}[facet]))
            self._facets[facet] = dropdown
            head.append(dropdown)
        self._count_lbl = create(Gtk.Label, label="", xalign=1); self._count_lbl.add_css_class("count")
        self._count_lbl.set_valign(Gtk.Align.CENTER)
        head.append(self._count_lbl)
        wp.append(head)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_vexpand(True)
        self.grid = Gtk.FlowBox(valign=Gtk.Align.START, max_children_per_line=5,
                                min_children_per_line=2, row_spacing=14,
                                column_spacing=14, homogeneous=True,
                                selection_mode=Gtk.SelectionMode.NONE)
        self.grid.set_margin_top(8); self.grid.set_margin_bottom(18)
        self.grid.set_margin_start(22); self.grid.set_margin_end(22)
        self.grid.connect("child-activated", self._on_tile_activated)
        self.grid.set_filter_func(self._filter_tile)
        self._resource_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.START)
        self._resource_box.append(self.grid)
        sw.set_child(self._resource_box)

        # Connecting overlay: centered spinner card shown while a session is
        # establishing (silent AAD + RDP handshake), until the desktop logs on.
        overlay = Gtk.Overlay()
        overlay.set_vexpand(True)
        overlay.set_child(sw)
        cbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        cbox.set_halign(Gtk.Align.CENTER); cbox.set_valign(Gtk.Align.CENTER)
        cbox.add_css_class("connect-card")
        self._connect_spinner = Gtk.Spinner(); self._connect_spinner.set_size_request(42, 42)
        self._connect_label = create(Gtk.Label, label=tr("Connecting…")); self._connect_label.add_css_class("connect-label")
        csub = create(Gtk.Label, label=tr("Securing your session")); csub.add_css_class("connect-sub")
        cbox.append(self._connect_spinner); cbox.append(self._connect_label); cbox.append(csub)
        cbox.set_visible(False)
        cancel_connect = Gtk.Button.new_from_icon_name("window-close-symbolic")
        bind(cancel_connect, 'tooltip_text', tr("Cancel pending connections"))
        cancel_connect.set_halign(Gtk.Align.END)
        cancel_connect.add_css_class("flat")
        cancel_connect.connect("clicked", lambda *_: self._cancel_visible_connections())
        cbox.prepend(cancel_connect)
        cbox.set_can_target(True)
        self._connect_overlay = cbox
        overlay.add_overlay(cbox)

        # Transient toast (bottom-centre): a self-dismissing notice that floats
        # over the feed. Used to tell a browser-scheme sign-in that it landed —
        # the ms-appx-web redirect leaves the browser tab spinning with no
        # completion page (the final hop is a custom scheme, not an HTTP URL), so
        # the app has to be the one that says "you're in, close the tab."
        toast = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toast.add_css_class("toast")
        toast.set_halign(Gtk.Align.CENTER); toast.set_valign(Gtk.Align.END)
        toast.set_margin_bottom(18)
        self._toast_label = create(Gtk.Label, label="", xalign=0, wrap=True)
        self._toast_label.add_css_class("toast-label")
        # Wrapped labels in a horizontal box shrink to their narrowest fit;
        # expanding keeps short notices on one line.
        self._toast_label.set_max_width_chars(60); self._toast_label.set_hexpand(True)
        toast_close = Gtk.Button.new_from_icon_name("window-close-symbolic")
        toast_close.add_css_class("flat")
        toast_close.connect("clicked", lambda *_: self._hide_toast())
        toast.append(self._toast_label); toast.append(toast_close)
        toast.set_visible(False)
        self._toast_box = toast
        overlay.add_overlay(toast)
        wp.append(overlay)

        # footer: live dot · status · version
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        foot.add_css_class("foot")
        self._livedot = Gtk.Box(); self._livedot.add_css_class("livedot")
        self._livedot.set_valign(Gtk.Align.CENTER)
        self.status = create(Gtk.Label, label="", xalign=0); self.status.add_css_class("status")
        self.status.set_hexpand(True)
        ver = create(Gtk.Label, label="v" + APP_VERSION, xalign=1); ver.add_css_class("ver")
        foot.append(self._livedot); foot.append(self.status); foot.append(ver)
        wp.append(foot)
        self.stack.add_named(wp, "workspaces")

        # busy page
        busy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        busy.add_css_class("avd-page")
        busy.set_valign(Gtk.Align.CENTER)
        sp = Gtk.Spinner(); sp.start()
        busy.append(sp)
        busy.append(create(Gtk.Label, label=tr("Loading…")))
        self.stack.add_named(busy, "busy")
        self.stack.set_visible_child_name("busy")

    # ---- helpers (main thread) --------------------------------------------
    def _dismiss_menus(self):
        for name in ("_account_pop", "_settings_pop"):
            popover = getattr(self, name, None)
            if popover is not None:
                popover.popdown()

    def _on_refresh_shortcut(self, *_):
        if self.stack.get_visible_child_name() != "workspaces":
            return False
        self._reload_feed()
        return True

    def _on_filter_shortcut(self, *_):
        if self.stack.get_visible_child_name() != "workspaces" or not self._tiles:
            return False
        self._filter.grab_focus()
        return True

    def _on_shortcuts_shortcut(self, *_):
        self._shortcuts_button.popup()
        return True

    def _collapsed(self, account_id, key):
        return bool(self._settings.get("_collapsed_sections", {}).get(account_id, {}).get(key))

    def _set_expanded(self, control, expanded):
        control._content.set_visible(expanded)
        control._arrow.set_from_icon_name("pan-down-symbolic" if expanded else "pan-end-symbolic")
        control.set_active(expanded)

    def _collapse_button(self, caption, account_id, key, content):
        button = Gtk.ToggleButton()
        button.add_css_class("section-toggle")
        button.set_child(caption)
        button._content = content
        button._arrow = caption.get_first_child()
        button._collapse_id = (account_id, key)
        self._set_expanded(button, not self._collapsed(account_id, key)
                           or getattr(self, "_filtering_active", False))

        def toggled(control):
            expanded = control.get_active()
            control._content.set_visible(expanded)
            control._arrow.set_from_icon_name("pan-down-symbolic" if expanded else "pan-end-symbolic")
            saved = self._settings.setdefault("_collapsed_sections", {}).setdefault(account_id, {})
            if expanded:
                saved.pop(key, None)
            else:
                saved[key] = True
            storage.save_settings(self._settings)

        button._collapse_handler = button.connect("toggled", toggled)
        return button

    def _set_filter_expansion(self, button, expanded):
        button.handler_block(button._collapse_handler)
        try:
            self._set_expanded(button, expanded)
        finally:
            button.handler_unblock(button._collapse_handler)

    @staticmethod
    def _facet_factory(max_chars):
        """Labels for a facet drop-down; the button ellipsizes, the list does not."""
        factory = Gtk.SignalListItemFactory()
        def setup(_factory, item):
            label = Gtk.Label(xalign=0)
            if max_chars:
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.set_max_width_chars(max_chars)
            item.set_child(label)
        factory.connect("setup", setup)
        factory.connect("bind", lambda _f, item: item.get_child().set_text(item.get_item().get_string()))
        return factory

    def _filter_values(self):
        facets = getattr(self, "_facets", {})
        return {facet: dd._values[dd.get_selected()] if dd.get_selected() < len(dd._values)
                else resource_groups.ALL for facet, dd in facets.items()}

    def _filter_tile(self, child):
        entry = getattr(self, "_filter", None)
        values = self._filter_values()
        return resource_groups.matches(
            child._res, entry.get_text() if entry else "",
            values.get("kind", ""), values.get("region", ""),
            values.get("workspace", ""), values.get("app", ""))

    def _refresh_filter_options(self):
        """Offer only values present in the shown feed; keep each saved choice."""
        resources = [child._res for child in self._tiles]
        facets = getattr(self, "_facets", {})
        everything = resource_groups.options(resources)
        # Each list follows the other choices; a choice is kept even when the
        # others rule it out, so it can still be cleared.
        available = resource_groups.options(
            resources, {facet: dd._wanted for facet, dd in facets.items()})
        self._updating_facets = True
        try:
            for facet, dropdown in facets.items():
                wanted = dropdown._wanted
                values = list(available[facet])
                if wanted and wanted not in values and wanted in everything[facet]:
                    values.append(wanted)
                if facet == "kind":
                    labels = [tr(resource_groups.KIND_LABELS[v]) for v in values]
                elif facet == "region":
                    labels = [tr(v) for v in values]
                elif facet == "workspace":
                    labels = [resource_groups.workspace_label(v) for v in values]
                else:
                    labels = list(values)
                if dropdown._values != [resource_groups.ALL] + values:
                    dropdown._values = [resource_groups.ALL] + values
                    dropdown.set_model(Gtk.StringList.new([tr(dropdown._all_label)] + labels))
                dropdown.set_selected(dropdown._values.index(wanted) if wanted in dropdown._values else 0)
                # A facet with one value in the whole feed cannot narrow anything.
                dropdown.set_visible(len(everything[facet]) > 1)
        finally:
            self._updating_facets = False

    def _on_facet_changed(self, dropdown, *_):
        if getattr(self, "_updating_facets", False):
            return
        dropdown._wanted = dropdown._values[dropdown.get_selected()] \
            if dropdown.get_selected() < len(dropdown._values) else resource_groups.ALL
        self._settings["_filters"] = {facet: dd._wanted for facet, dd in self._facets.items() if dd._wanted}
        storage.save_settings(self._settings)
        self._refresh_filter_options()
        self._apply_filter()

    def _apply_filter(self):
        filtering = bool(self._filter.get_text().strip() or any(self._filter_values().values()))
        changed = filtering != getattr(self, "_filtering_active", False)
        self._filtering_active = filtering
        if changed:
            for kind_box in getattr(self, "_kind_boxes", []):
                for button in [kind_box._toggle] + [group._toggle for group in kind_box._groups
                                                   if group._toggle is not None]:
                    account_id, key = button._collapse_id
                    self._set_filter_expansion(button, filtering or not self._collapsed(account_id, key))
        for grid in {t.get_parent() for t in self._tiles if t.get_parent() is not None}:
            grid.invalidate_filter()
        shown = 0
        for kind_box in getattr(self, "_kind_boxes", []):
            any_group = False
            in_kind = 0
            for group in kind_box._groups:
                visible = sum(1 for child in group._tiles if self._filter_tile(child))
                group.set_visible(visible > 0)
                any_group = any_group or visible > 0
                shown += visible
                in_kind += visible
                if getattr(group, "_count", None) is not None:
                    group._count.set_text(str(visible))
            kind_box.set_visible(any_group)
            if getattr(kind_box, "_count", None) is not None:
                kind_box._count.set_text(str(in_kind))
        total = len(self._tiles)
        text = (ngettext("{count} resource", "{count} resources", total).format(count=total)
                if shown == total else tr("{shown} of {total}").format(shown=shown, total=total))
        if getattr(self, "_count_lbl", None) is not None:
            bind(self._count_lbl, 'text', text)

    def _close_filter(self):
        self._filter.set_text("")
        self._apply_filter()
        self._focus_first_tile()

    def _focus_first_tile(self):
        for child in self._tiles:
            if child.get_child_visible() and child.get_mapped():
                child.grab_focus()
                return

    def _on_tile_key(self, _controller, key, _keycode, state, child):
        if key == Gdk.KEY_Menu or (key == Gdk.KEY_F10 and state & Gdk.ModifierType.SHIFT_MASK):
            self._show_resource_menu(child)
            return True
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK |
                    Gdk.ModifierType.SUPER_MASK):
            return False
        direction = {Gdk.KEY_Left: (-1, 0), Gdk.KEY_Right: (1, 0),
                     Gdk.KEY_Up: (0, -1), Gdk.KEY_Down: (0, 1)}.get(key)
        if direction is None:
            return False
        ok, rect = child.compute_bounds(self._resource_box)
        if not ok:
            return False
        x, y = rect.get_x() + rect.get_width() / 2, rect.get_y() + rect.get_height() / 2
        candidates = []
        for other in self._tiles:
            if other is child or not other.get_mapped() or not other.get_child_visible():
                continue
            valid, bounds = other.compute_bounds(self._resource_box)
            if not valid:
                continue
            dx = bounds.get_x() + bounds.get_width() / 2 - x
            dy = bounds.get_y() + bounds.get_height() / 2 - y
            if direction[0] and dx * direction[0] > 1 and abs(dy) < rect.get_height() / 2:
                candidates.append(((abs(dx), abs(dy)), other))
            elif direction[1] and dy * direction[1] > rect.get_height() / 2:
                candidates.append(((abs(dy), abs(dx)), other))
        if not candidates:
            return False
        min(candidates, key=lambda item: item[0])[1].grab_focus()
        return True

    def _empty_state(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      halign=Gtk.Align.CENTER, margin_top=56)
        box.add_css_class("empty")
        glyph = Gtk.Image.new_from_icon_name("computer-symbolic")
        glyph.set_pixel_size(28); glyph.add_css_class("empty-glyph"); glyph.set_margin_bottom(6)
        title = create(Gtk.Label, label=tr("No workspaces yet")); title.add_css_class("empty-title")
        hint = create(Gtk.Label, label=tr("Press Ctrl+R to refresh")); hint.add_css_class("hero-note")
        for widget in (glyph, title, hint):
            box.append(widget)
        return box

    def _on_menu_outside_pressed(self, _gesture, _count, x, y):
        target = self.win.pick(x, y, Gtk.PickFlags.DEFAULT)
        while target is not None:
            # Let menu items and the menu buttons handle their own interaction.
            if isinstance(target, (Gtk.Popover, Gtk.MenuButton)):
                return
            target = target.get_parent()
        self._dismiss_menus()

    def _on_window_active_changed(self, window, _property):
        """Dismiss menus when their non-grabbing parent loses activation."""
        if not window.is_active():
            self._dismiss_menus()

    def _set_status(self, text, session=None, reauth=False, attempt=None, allow_cancelled=False):
        def apply():
            if attempt is not None and (not attempt.is_current()
                    or (attempt.cancelled.is_set() and not allow_cancelled)):
                return False
            if session is not None:
                session.view_status = text
                session.needs_signin = reauth
                if self.view_mode == "all" and not session.removed:
                    widgets = self._account_status_widgets.get(session.id)
                    if widgets:
                        bind(widgets[0], 'text', text)
                        widgets[0].get_parent().set_visible(bool(text) or reauth)
                        widgets[1].set_visible(reauth)
                    return False
            if self.status and (session is None or self._visible(session)):
                bind(self.status, 'text', text)
            return False
        GLib.idle_add(apply)

    def _toast(self, text, timeout=10):
        """Show a self-dismissing notice over the feed. Safe from any thread."""
        def apply():
            if not self._toast_box:
                return False
            bind(self._toast_label, 'text', text)
            self._toast_box.set_visible(True)
            if self._toast_source:
                GLib.source_remove(self._toast_source)
            self._toast_source = GLib.timeout_add_seconds(timeout, self._hide_toast)
            return False
        GLib.idle_add(apply)

    def _hide_toast(self):
        self._toast_source = 0
        if self._toast_box:
            self._toast_box.set_visible(False)
        return False

    def _update_account(self, who, session=None):
        """Fill the header account chip: email + a 2-letter avatar."""
        session = session if session is not None else self.current
        if session is not None and session.local:
            name = session.display_name
            bind(self._acct_who, 'text', name)
            bind(self._avatar_lbl, 'text', (name[:1] or "L").upper())
            return
        if not who or "@" not in who:
            bind(self._acct_who, 'text', tr("Sign in"))
            bind(self._avatar_lbl, 'text', "?")
            return
        bind(self._acct_who, 'text', self.current.display_name if self.current else who)
        user = who.split("@", 1)[0]
        parts = _re.split(r"[.\-_]+", user)
        initials = (parts[0][:1] + (parts[1][:1] if len(parts) > 1 else user[1:2]))
        bind(self._avatar_lbl, 'text', initials.upper() or "?")

    def _set_connecting(self, res_id, title, on, session=None, attempt=None, allow_cancelled=False):
        """Show/hide the centered 'Connecting…' spinner overlay. Safe to call
        from any thread. The overlay stays up while any session is connecting."""
        session = session or self.current
        if session is None:
            return
        key = (session.id, res_id)
        def apply():
            if attempt is not None and (not attempt.is_current()
                    or (attempt.cancelled.is_set() and not allow_cancelled)):
                return False
            if on and not session.removed:
                self._connecting.add(key)
            else:
                self._connecting.discard(key)
            visible = [key for key in self._connecting if self.view_mode == "all" or key[0] == self.selected_id]
            if visible:
                n = len(visible)
                bind(self._connect_label, 'text',
                    tr("Connecting to {resource}…").format(resource=title) if n == 1 else tr("Connecting… ({count})").format(count=n))
                self._connect_spinner.start()
                self._connect_overlay.set_visible(True)
            else:
                self._connect_overlay.set_visible(False)
                self._connect_spinner.stop()
            return False
        GLib.idle_add(apply)

    def _show(self, name, session=None):
        def apply():
            if session is not None and not self._visible(session):
                return False
            if self.view_mode == "all" and session is not None:
                if name == "signin":
                    session.view_status = "Sign in again to refresh"
                    session.needs_signin = True
                self._render_resources(session)
                return False
            self.stack.set_visible_child_name(name)
            # "Sign out" only makes sense once we're signed in (workspaces view)
            if self._signout_item is not None:
                self._signout_item.set_sensitive(self.current is not None)
        GLib.idle_add(apply)

    # ---- auth / token lifecycle ------------------------------------------
    def _schedule_refresh(self, session):
        if session.removed:
            return False
        if session.refresh_source:
            GLib.source_remove(session.refresh_source)
        secs = max(60, int(session.deadline - time.monotonic()) - 300)
        session.refresh_source = GLib.timeout_add_seconds(secs, self._auto_refresh, session)
        return False

    def _auto_refresh(self, session):
        session.refresh_source = 0
        threading.Thread(target=self._do_refresh, args=(True, session), daemon=True).start()
        return False

    def _do_refresh(self, silent, session):
        if session.refresh():
            GLib.idle_add(self._save_refreshed_profile, session)
            GLib.idle_add(self._schedule_refresh, session)
            return True
        if not silent:
            self._show("signin", session)
        return False

    def _save_refreshed_profile(self, session):
        if not session.removed:
            self.registry.save()
            self._rebuild_account_menu()
        return False

    def _ensure_token(self, session):
        if session.local:
            return not session.removed   # local .rdp files need no feed token
        with session.refresh_lock:
            if session.removed:
                return False
            if session.token and time.monotonic() < session.deadline - 120:
                return True
        return self._do_refresh(True, session)

    def _download_resource_rdp(self, child, res, session, windowed, attempt):
        try:
            return session.client.download_rdp(session.token, res)
        except RdpDownloadError as error:
            if error.status != 401 or session.local:
                raise

        # A token can be revoked before its advertised expiry. Force one silent
        # refresh, then retry. If the feed's ResourceFile URL is stale too, fetch
        # the feed once and retry with its current URL.
        with session.refresh_lock:
            session.token = None
            session.deadline = 0.0
        if not self._ensure_token(session):
            if attempt.is_current():
                child._launching = False
            self._set_connecting(res["id"], res["title"], False, session, attempt=attempt)
            self._set_tile_state(res["id"], None, session, attempt=attempt)
            self._set_status(tr("Sign in to connect"), session, attempt=attempt)
            session.pending_launch = (res["id"], windowed)
            GLib.idle_add(lambda: self._interactive_signin(session) if attempt.active() else False)
            return None

        try:
            return session.client.download_rdp(session.token, res)
        except RdpDownloadError as error:
            if error.status != 401:
                raise

        refreshed = session.client.enumerate_feed(session.token)
        if not session.save_resources(refreshed):
            raise RdpDownloadError(401, tr("The account was signed out during the download."))
        current = next((item for item in refreshed if item.get("id") == res["id"]), None)
        if current is None or not current.get("rdp_url"):
            raise RdpDownloadError(401, tr("Refresh the workspace list and try again."))
        child._res = current
        return session.client.download_rdp(session.token, current)

    def _refresh_failed_status(self, session):
        err = session.client.last_refresh_error
        if "AADSTS70043" in err or "sign-in frequency" in err:
            return (tr("Showing saved workspaces · your organization requires signing "
                    "in again (Conditional Access sign-in frequency)"))
        return tr("Showing saved workspaces · sign in again to refresh")

    def _web_session(self, session):
        """Shared, PERSISTENT WebKit network session used by every Microsoft
        sign-in webview — the launcher's sign-in dialog and the connection-time
        AAD resolver. It only ever visits Entra login domains, so persisting its
        cookies (a) makes a re-auth after a token refresh is refused a
        click-through (SSO) instead of full password+MFA, and (b) lets the
        connection-time gateway token be fetched silently from the same SSO
        session. Stored in our app data dir (user-private); the refresh token
        itself lives in the keyring, not here."""
        if session.web_session is None:
            d = os.path.join(session.directory, "webview")
            os.makedirs(d, mode=0o700, exist_ok=True)
            session.web_session = WebKit.NetworkSession.new(
                d, os.path.join(d, "cache"))
            try:
                session.web_session.get_cookie_manager().set_persistent_storage(
                    os.path.join(d, "cookies.sqlite"),
                    WebKit.CookiePersistentStorage.SQLITE)
            except Exception:
                pass
        return session.web_session

    def _signin_surface(self, wv, window, on_request=None):
        """Let a sign-in WebView use a FIDO2 security key (WebKitGTK has no
        WebAuthn): inject the shim, answer it from the key, and show the
        insert/touch/PIN/account prompt over the page. ``on_request`` runs when
        the page asks for the key (to reveal a window that was waiting for
        silent SSO). Returns the widget to put in the window."""
        from avd_feed_connect.gui.signin_panel import SignInPanel
        from avd_feed_connect.gui.webauthn import WebAuthnBridge
        # Sign-in pages need no GPU, and a GPU-composited web process can come
        # back from suspend drawing nothing (a blank sign-in window).
        try:
            wv.get_settings().set_hardware_acceleration_policy(
                WebKit.HardwareAccelerationPolicy.NEVER)
        except (AttributeError, TypeError):
            pass
        # A crashed or killed web process leaves the page blank; load it again.
        wv.connect("web-process-terminated",
                   lambda view, _reason: GLib.idle_add(lambda: view.reload() and False))
        panel = SignInPanel()
        bridge = WebAuthnBridge(wv, panel, on_request=on_request).attach()
        # GTK 4 does not emit "destroy" from gtk_window_destroy() while Python
        # still references the window; "unrealize" does fire there, so a closed
        # (or handed-off) sign-in never keeps the security key busy.
        window.connect("unrealize", lambda *_: bridge.cancel())
        overlay = Gtk.Overlay()
        overlay.set_child(wv)
        overlay.add_overlay(panel)
        return overlay

    def _signin_header(self, window, title=None):
        window.add_css_class("avd-signin")
        header = Gtk.HeaderBar()
        header.add_css_class("avd-header")
        heading = create(Gtk.Label, label=title if title is not None else window.get_title())
        heading.add_css_class("brand-title")
        heading.set_ellipsize(Pango.EllipsizeMode.END)
        header.set_title_widget(heading)
        browser = create(Gtk.Button, label=tr("Use system browser"))
        browser.add_css_class("flat")
        browser.add_css_class("signin-browser")
        header.pack_end(browser)
        window.set_titlebar(header)
        return browser

    def _use_browser_signin(self):
        """True when sign-in should go through the system browser (auth-code +
        PKCE) instead of the embedded WebView. Needed for tenants that enforce
        certificate-based auth (smart card / CAC / PIV): WebKitGTK has no PKCS#11
        bridge, so it can't present a client certificate and sign-in fails before
        the card is ever read (issue #17) — the real browser can. FIDO2 security
        keys work in the embedded WebView through the WebAuthn bridge, so this is
        off unless the user opts in. AVD_BROWSER_SIGNIN forces it on/off;
        otherwise it's the saved toggle."""
        env = os.environ.get("AVD_BROWSER_SIGNIN", "").strip().lower()
        if env in ("1", "true", "yes", "on"):
            return True
        if env in ("0", "false", "no", "off"):
            return False
        return bool(self._settings.get("_browser_signin", False))

    def _interactive_signin(self, session=None, browser=None, remember=False):
        """``browser`` None follows the sign-in preference; True/False picks the
        system browser / in-app sign-in for this one sign-in. ``remember`` saves
        the browser preference once that browser sign-in succeeds."""
        # Device-code is kept as a hidden fallback (AVD_DEVICE_CODE=1) for the
        # rare tenant that allows it; many block it via Conditional Access, so
        # the browser auth-code flow is the default "sign in with a browser".
        if os.environ.get("AVD_DEVICE_CODE", "").strip().lower() in ("1", "true", "yes", "on"):
            self._devicecode_signin(session)
            return
        if browser or (browser is None and self._use_browser_signin()):
            self._browser_signin(session, offer_in_app=browser is True, remember=remember)
            return
        session = session or self.current
        if session is None:
            self._add_account(browser=browser)
            return
        if session.removed:
            return
        # Never reuse or resume a previous sign-in window: tear any existing one
        # down and start fresh, so closing and reopening is always a clean slate
        # (the user's rule: close must close, reopen must resume from clean).
        if self._signin_dlg is not None:
            old, self._signin_dlg = self._signin_dlg, None
            old.destroy()
        verifier = _b64url(secrets.token_bytes(64))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = secrets.token_urlsafe(16)
        # Always show the account picker (prompt=select_account). It is the
        # known-good flow: it bypasses Azure AD Seamless SSO — which needs a
        # Windows Kerberos ticket and, on Linux, hangs the webview forever on
        # "Trying to sign you in" — and it does NOT force the full interactive
        # re-login that makes tenants inject a security-info registration
        # interrupt ("Keep your account secure"). Trying to save the one picker
        # click (login_hint without a prompt, or prompt=login) reintroduced
        # both of those bugs, so we don't.
        params = {
            "client_id": config.CLIENT_ID, "response_type": "code",
            "redirect_uri": config.REDIRECT, "scope": config.SCOPE,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "state": state, "prompt": "select_account",
        }
        if session.client.upn:
            params["login_hint"] = session.client.upn   # preselect the known account in the picker
        url = config.LOGIN + "/authorize?" + urllib.parse.urlencode(params)

        # With the main window hidden in the tray (e.g. a tray-initiated
        # connect), show the sign-in on its own instead of surfacing the app:
        # a standalone top-level, since a modal dialog needs a visible parent.
        parent = self.win if self.win and self.win.get_visible() else None
        dlg = create(Gtk.Window, title=tr("Sign in"), transient_for=parent, modal=parent is not None)
        dlg.set_default_size(760, 680)
        # Shared PERSISTENT session (SSO cookies kept on disk) so a re-auth is a
        # click-through and the connection-time token can be fetched silently.
        # The half-finished-flow / ghost-window problems that made us try an
        # ephemeral session are fixed independently (popup blocking + explicit
        # close-request + always destroying/rebuilding the dialog + DMABUF off),
        # so persistence is safe and is what actually reduces sign-in prompts.
        wv = WebKit.WebView(network_session=self._web_session(session))
        self._apply_ua(wv)   # account sign-in → Default UA (issue #28)
        dlg.set_child(self._signin_surface(wv, dlg))
        self._signin_dlg = dlg
        got_code = [False]
        handoff = [False]

        browser_btn = self._signin_header(dlg, tr("Sign in"))

        def use_browser(*_):
            # Hand this same account over to the browser flow; it must not be
            # discarded the way a closed sign-in is.
            handoff[0] = True
            close()
            self._browser_signin(session, offer_in_app=True)
        browser_btn.connect("clicked", use_browser)

        # Block popups. A page (e.g. the personal-account "approve on your phone"
        # step) can call window.open(); WebKit would spawn a separate, orphan
        # WebView/window with no controls — the "ghost window" that can't be
        # closed. Returning None here declines the popup instead.
        wv.connect("create", lambda *_a: None)

        closed = [False]

        def on_closed(*_):
            # Clicking the window's × always closes it and fully resets state,
            # so the next sign-in is a clean start (no resumed half-finished flow).
            if closed[0]:
                return
            closed[0] = True
            if self._signin_dlg is dlg:
                self._signin_dlg = None
            if not got_code[0] and not handoff[0]:
                session.pending_launch = None
                self._discard_add(session)

        def close(*_):
            # Reset explicitly: GTK 4 does not emit "destroy" when the window is
            # destroyed while Python still holds it.
            on_closed()
            dlg.destroy()
            return True
        dlg.connect("destroy", on_closed)
        # Default close-request already destroys the window; make it explicit and
        # unconditional so nothing can swallow the close.
        dlg.connect("close-request", close)

        def on_decide(view, decision, dtype):
            if dtype != WebKit.PolicyDecisionType.NAVIGATION_ACTION:
                return False
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri.startswith(config.REDIRECT) and "code=" in uri:
                decision.ignore()
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)
                got_code[0] = True
                dlg.destroy()
                if qs.get("state", [state])[0] != state:
                    session.pending_launch = None
                    self._discard_add(session)
                    self._error(tr("Sign-in state mismatch, please retry."))
                    return True
                code = qs.get("code", [""])[0]
                self._show("busy", session)
                threading.Thread(target=self._exchange, args=(code, verifier, session),
                                 daemon=True).start()
                return True
            return False

        wv.connect("decide-policy", on_decide)
        wv.load_uri(url)
        dlg.present()

    def _exchange(self, code, verifier, session):
        try:
            st, tok = http.post(config.LOGIN + "/token", {
                "grant_type": "authorization_code", "client_id": config.CLIENT_ID,
                "code": code, "redirect_uri": config.REDIRECT, "scope": config.SCOPE,
                "code_verifier": verifier})
            if st != 200:
                raise ValueError(tok.get("error_description", tok.get("error", tr("Sign-in failed"))))
            claims_from_token(tok)
        except Exception as error:
            GLib.idle_add(self._signin_failed, session, str(error))
            return
        GLib.idle_add(self._finish_signin, session, tok)

    def _signin_failed(self, session, message):
        session.pending_launch = None
        self._discard_add(session)
        self._error(tr("Sign-in failed: {error}").format(error=message))
        if self._visible(session):
            self._show("workspaces" if session.resources else "signin", session)
        return False

    def _finish_signin(self, session, tok):
        if session.removed:
            return False
        try:
            claims = claims_from_token(tok)
            if session is self._adding:
                existing = next((s for s in self.accounts.values() if not s.removed and
                                 (s.profile['tid'], s.profile['oid']) == (claims['tid'], claims['oid'])), None)
                target = existing or session
                if not target.apply_token(tok):
                    raise ValueError(tr("Please use the selected work account."))
                profile = self.registry.upsert(claims, target.id)
                target.profile = profile
                if existing:
                    self._discard_add(session)
                else:
                    self._adding = None
                    self.accounts[target.id] = target
                session = target
            else:
                if not session.apply_token(tok):
                    raise ValueError(tr("Please sign in with the selected work account."))
                self.registry.save()
            self._schedule_refresh(session)
            self._select_account(session.id)
        except (ValueError, OSError) as error:
            self._signin_failed(session, str(error))
        return False

    def _start_browser_signin(self):
        """Hero shortcut: sign in through the system browser. The preference
        becomes sticky only once that sign-in succeeds — a tenant that needs it
        (certificate-based auth) needs it for every re-auth, and the toggle is
        reversible in Default settings — so a cancelled mis-click changes nothing."""
        self._interactive_signin(browser=True, remember=True)

    def _browser_signin(self, session=None, offer_in_app=False, remember=False):
        """System-browser auth-code + PKCE sign-in (issue #17). Opens the real
        browser so a smart card / certificate can be used, and captures the code
        on a loopback redirect. If the loopback can't bind or isn't a registered
        redirect for the client, the user falls back to pasting the final URL —
        the OOB nativeclient flow, exactly like FreeRDP's 'Browse to:'.
        ``offer_in_app`` adds a way back to in-app sign-in; ``remember`` saves
        the browser preference once a sign-in code arrives."""
        session = session or self.current
        if session is None:
            self._add_account(browser=True if offer_in_app else None, remember=remember)
            return
        if session.removed:
            return
        if self._signin_dlg is not None:
            old, self._signin_dlg = self._signin_dlg, None
            old.destroy()

        verifier = _b64url(secrets.token_bytes(64))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = secrets.token_urlsafe(16)

        def authorize_url(redirect_uri):
            params = {
                "client_id": config.CLIENT_ID, "response_type": "code",
                "redirect_uri": redirect_uri, "scope": config.SCOPE,
                "code_challenge": challenge, "code_challenge_method": "S256",
                "state": state, "prompt": "select_account",
            }
            if session.client.upn:
                params["login_hint"] = session.client.upn
            return config.LOGIN + "/authorize?" + urllib.parse.urlencode(params)

        parent = self.win if self.win and self.win.get_visible() else None
        dlg = create(Gtk.Window, title=tr("Sign in"),
                     transient_for=parent, modal=parent is not None)
        dlg.set_default_size(480, -1)
        self._signin_dlg = dlg
        done = [False]
        handoff = [False]
        server_ref = [None]
        redirect_used = [None]   # which redirect_uri the current authorize used

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(22)

        heading = create(Gtk.Label, label=tr("Sign in with your browser"), xalign=0)
        heading.add_css_class("hero-title")
        intro = create(Gtk.Label, xalign=0, wrap=True,
                       label=tr("Your browser will open so you can sign in with your "
                                "smart card or certificate."))
        intro.add_css_class("hero-sub")

        open_btn = create(Gtk.Button, label=tr("Open browser"))
        open_btn.add_css_class("suggested-action")
        copy_btn = create(Gtk.Button, label=tr("Copy sign-in link"))
        copy_btn.add_css_class("flat")
        btnrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btnrow.set_halign(Gtk.Align.START)
        btnrow.append(open_btn); btnrow.append(copy_btn)
        copy_note = create(Gtk.Label, xalign=0, wrap=True,
            label=tr("If your browser didn't open — or your certificate is in a "
                     "different browser — copy the link and open it there."))
        copy_note.add_css_class("hero-sub")

        statusrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        spinner = Gtk.Spinner(); spinner.start()
        status = create(Gtk.Label, label=tr("Waiting for you to finish signing in…"),
                        xalign=0, wrap=True)
        status.add_css_class("hero-sub")
        statusrow.append(spinner); statusrow.append(status)

        paste_link = create(Gtk.Button, label=tr("Paste the address instead"))
        paste_link.add_css_class("flat"); paste_link.set_halign(Gtk.Align.START)
        paste_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        paste_note = create(Gtk.Label, xalign=0, wrap=True,
            label=tr("After you sign in, copy the final address from your browser and paste it here:"))
        paste_note.add_css_class("hero-sub")
        paste_entry = Gtk.Entry(); paste_entry.set_hexpand(True)
        bind(paste_entry, 'placeholder_text', tr("Paste the sign-in address"))
        paste_go = create(Gtk.Button, label=tr("Sign in"))
        paste_go.add_css_class("suggested-action"); paste_go.set_halign(Gtk.Align.START)
        paste_box.append(paste_note); paste_box.append(paste_entry); paste_box.append(paste_go)
        paste_box.set_visible(False)

        def use_in_app():
            # Same account, in-app sign-in; it must not be discarded the way a
            # cancelled sign-in is.
            handoff[0] = True
            close()
            self._interactive_signin(session, browser=False)

        actions = sign_in_actions(dlg, lambda: close(), use_in_app if offer_in_app else None)
        for w in (heading, intro, btnrow, copy_note, statusrow, paste_link, paste_box, actions):
            box.append(w)
        dlg.set_child(box)

        done_html = (
            "<!doctype html><meta charset=utf-8><title>%s</title>"
            "<body style='font-family:system-ui,sans-serif;text-align:center;"
            "margin-top:4em;color:#222'><h2>%s</h2></body>"
            % (GLib.markup_escape_text(tr("Signed in")),
               GLib.markup_escape_text(
                   tr("Signed in. You can close this tab and return to the app.")))
        )

        def shutdown_server():
            srv, server_ref[0] = server_ref[0], None
            if srv is not None:
                threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()),
                                 daemon=True).start()

        def complete(redirect_uri, qs):
            if (self._browser_complete(session, dlg, verifier, redirect_uri,
                                       qs, state, done, shutdown_server)
                    and remember):
                self._settings["_browser_signin"] = True
                storage.save_settings(self._settings)

        def on_code_main(qs):
            if not done[0] and self._signin_dlg is dlg:
                complete(redirect_used[0], qs)
            return False

        def start_loopback():
            try:
                srv = HTTPServer(("127.0.0.1", 0), _LoopbackHandler)
            except Exception:
                return None
            srv._done_html = done_html
            fired = {"v": False}
            def guard(qs):
                if fired["v"]:
                    return
                fired["v"] = True
                GLib.idle_add(on_code_main, qs)
            srv._on_code = guard
            threading.Thread(target=srv.serve_forever,
                             kwargs={"poll_interval": 0.3}, daemon=True).start()
            return srv

        def launch(url):
            try:
                Gio.AppInfo.launch_default_for_uri(url, None)
            except Exception:
                pass

        def begin_paste(*_):
            shutdown_server()
            self._pending_scheme = None
            redirect_used[0] = config.REDIRECT
            copy_btn.set_label(tr("Copy sign-in link"))   # link changed; allow re-copy
            spinner.stop()
            statusrow.set_visible(False)
            paste_link.set_visible(False)
            paste_box.set_visible(True)
            launch(authorize_url(config.REDIRECT))

        def begin_loopback():
            srv = start_loopback()
            server_ref[0] = srv
            if srv is None:
                begin_paste()
                return
            redirect_used[0] = "http://localhost:%d/" % srv.server_address[1]
            launch(authorize_url(redirect_used[0]))

        def begin_scheme():
            # Auto-capture without a local web server: authorize with the
            # ms-appx-web broker redirect (registered on all clouds, including
            # sovereign where loopback is not), and let the OS x-scheme-handler
            # hand the ?code=… URL back to this running app (do_open). The paste
            # link stays as a manual fallback if the handler is not registered.
            shutdown_server()
            redirect_used[0] = config.BROKER_REDIRECT
            self._pending_scheme = (state, on_code_main)
            launch(authorize_url(config.BROKER_REDIRECT))

        open_btn.connect("clicked", lambda *_: launch(authorize_url(
            redirect_used[0] or config.REDIRECT)))

        def do_copy(*_):
            try:
                copy_btn.get_clipboard().set(
                    authorize_url(redirect_used[0] or config.REDIRECT))
                copy_btn.set_label(tr("Copied"))
            except Exception:
                pass
        copy_btn.connect("clicked", do_copy)
        paste_link.connect("clicked", begin_paste)

        def do_paste(*_):
            frag = paste_entry.get_text().strip()
            if "://" in frag:
                p = urllib.parse.urlparse(frag)
                frag = p.query or p.fragment
            frag = frag.lstrip("?#")
            qs = urllib.parse.parse_qs(frag)
            if "code" not in qs and "error" not in qs and frag:
                qs = {"code": [frag.split("&", 1)[0]], "state": [state]}
            complete(config.REDIRECT, qs)
        paste_go.connect("clicked", do_paste)
        paste_entry.connect("activate", do_paste)

        closed = [False]

        def on_closed(*_):
            if closed[0]:
                return
            closed[0] = True
            if self._signin_dlg is dlg:
                self._signin_dlg = None
            if self._pending_scheme and self._pending_scheme[0] == state:
                self._pending_scheme = None
            shutdown_server()
            if not done[0] and not handoff[0]:
                session.pending_launch = None
                self._discard_add(session)

        def close(*_):
            # Reset explicitly: GTK 4 does not emit "destroy" when the window is
            # destroyed while Python still holds it.
            on_closed()
            dlg.destroy()
            return True
        dlg.connect("destroy", on_closed)
        dlg.connect("close-request", close)

        # Loopback auto-capture (http://localhost:<port>) only works where that
        # redirect is registered for the public MS Remote Desktop client. On
        # sovereign clouds it is NOT — every Gov tester hits AADSTS50011
        # "redirect URI 'http://localhost:…' does not match" after a successful
        # CAC sign-in. There the ms-appx-web broker redirect IS registered and is
        # auto-captured by the OS scheme handler (begin_scheme), so Gov gets the
        # same no-paste flow; the paste link stays as a fallback. Commercial keeps
        # the loopback flow, which needs no external-protocol prompt.
        force_scheme = os.environ.get("AVD_SCHEME_SIGNIN", "").strip().lower() \
            in ("1", "true", "yes", "on")
        if config.CLOUD == "commercial" and not force_scheme:
            begin_loopback()
        else:
            begin_scheme()
        dlg.present()

    def _browser_complete(self, session, dlg, verifier, redirect_uri, qs,
                           state, done, shutdown_server):
        """Finish a browser sign-in; True once a code is being exchanged."""
        if done[0]:
            return False
        if qs.get("error"):
            message = (qs.get("error_description") or qs.get("error"))[0]
        elif qs.get("state", [state])[0] != state:
            message = tr("Sign-in state mismatch, please retry.")
        elif not qs.get("code", [""])[0]:
            return False    # nothing usable yet (e.g. a stray probe) — keep waiting
        else:
            message = None
        done[0] = True
        self._pending_scheme = None
        shutdown_server()
        if self._signin_dlg is dlg:
            self._signin_dlg = None
        dlg.destroy()
        if message is not None:
            self._signin_failed(session, message)
            return False
        self._show("busy", session)
        threading.Thread(target=self._browser_exchange,
                         args=(qs["code"][0], verifier, redirect_uri, session),
                         daemon=True).start()
        return True

    def _browser_exchange(self, code, verifier, redirect_uri, session):
        try:
            st, tok = http.post(config.LOGIN + "/token", {
                "grant_type": "authorization_code", "client_id": config.CLIENT_ID,
                "code": code, "redirect_uri": redirect_uri, "scope": config.SCOPE,
                "code_verifier": verifier})
            if st != 200:
                raise ValueError(tok.get("error_description",
                                         tok.get("error", tr("Sign-in failed"))))
            claims_from_token(tok)
        except Exception as error:
            GLib.idle_add(self._signin_failed, session, str(error))
            return
        GLib.idle_add(self._finish_signin, session, tok)

    def _devicecode_signin(self, session=None):
        """Device-code sign-in: the app shows a short URL + code, the user
        authenticates in their own system browser (which CAN use a smart card /
        certificate), and we poll for the token. The browser-side companion to
        the embedded-WebView flow in _interactive_signin (issue #17)."""
        session = session or self.current
        if session is None:
            self._add_account()
            return
        if session.removed:
            return
        # Same single-surface contract as the WebView flow: tear any existing
        # sign-in window down so closing and reopening is always a clean slate.
        if self._signin_dlg is not None:
            old, self._signin_dlg = self._signin_dlg, None
            old.destroy()

        parent = self.win if self.win and self.win.get_visible() else None
        dlg = create(Gtk.Window, title=tr("Sign in"),
                     transient_for=parent, modal=parent is not None)
        dlg.set_default_size(460, -1)
        self._signin_dlg = dlg
        cancelled = threading.Event()
        done = [False]

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(22)

        heading = create(Gtk.Label, label=tr("Sign in with your browser"), xalign=0)
        heading.add_css_class("hero-title")
        intro = create(Gtk.Label, xalign=0, wrap=True,
                       label=tr("Open the page below and enter this code to sign in. "
                                "Use the browser where your smart card or certificate works."))
        intro.add_css_class("hero-sub")

        url_lbl = Gtk.Label(xalign=0, selectable=True, wrap=True)
        url_lbl.add_css_class("status")
        code_lbl = Gtk.Label(xalign=0, selectable=True)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        open_btn = create(Gtk.Button, label=tr("Open browser"))
        open_btn.add_css_class("suggested-action")
        open_btn.set_sensitive(False)
        copy_btn = create(Gtk.Button, label=tr("Copy code"))
        copy_btn.set_sensitive(False)
        btns.append(open_btn)
        btns.append(copy_btn)

        statusrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        spinner = Gtk.Spinner()
        spinner.start()
        status = create(Gtk.Label, label=tr("Requesting a sign-in code…"),
                        xalign=0, wrap=True)
        status.add_css_class("hero-sub")
        statusrow.append(spinner)
        statusrow.append(status)

        for w in (heading, intro, url_lbl, code_lbl, btns, statusrow):
            box.append(w)
        dlg.set_child(box)

        state = {"url": "", "code": ""}

        def do_open(*_):
            if state["url"]:
                try:
                    Gio.AppInfo.launch_default_for_uri(state["url"], None)
                except Exception:
                    pass
        open_btn.connect("clicked", do_open)

        def do_copy(*_):
            if state["code"]:
                try:
                    copy_btn.get_clipboard().set(state["code"])
                    copy_btn.set_label(tr("Copied"))
                except Exception:
                    pass
        copy_btn.connect("clicked", do_copy)

        def on_closed(*_):
            if self._signin_dlg is dlg:
                self._signin_dlg = None
            cancelled.set()
            if not done[0]:
                session.pending_launch = None
                self._discard_add(session)
        dlg.connect("destroy", on_closed)
        dlg.connect("close-request", lambda w: (w.destroy(), True)[1])

        ui = {"url_lbl": url_lbl, "code_lbl": code_lbl, "open_btn": open_btn,
              "copy_btn": copy_btn, "status": status, "state": state}
        threading.Thread(target=self._devicecode_worker,
                         args=(session, dlg, cancelled, done, ui),
                         daemon=True).start()
        dlg.present()

    def _devicecode_worker(self, session, dlg, cancelled, done, ui):
        st, dc = http.post(config.LOGIN + "/devicecode",
                           {"client_id": config.CLIENT_ID, "scope": config.SCOPE})
        if st != 200:
            GLib.idle_add(self._devicecode_error, session, dlg, done,
                          dc.get("error_description") or dc.get("error")
                          or tr("Couldn't start sign-in. Please try again."))
            return
        GLib.idle_add(self._devicecode_present, dlg, cancelled, done, ui, dc)
        interval = dc.get("interval", 5)
        deadline = time.monotonic() + dc.get("expires_in", 900)
        while not cancelled.is_set():
            slept = 0.0
            while slept < interval and not cancelled.is_set():
                time.sleep(0.5)
                slept += 0.5
            if cancelled.is_set():
                return
            if time.monotonic() > deadline:
                GLib.idle_add(self._devicecode_error, session, dlg, done,
                              tr("The sign-in code expired. Please try again."))
                return
            st, tok = http.post(config.LOGIN + "/token", {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": config.CLIENT_ID, "device_code": dc["device_code"]})
            if st == 200:
                try:
                    claims_from_token(tok)
                except Exception as error:
                    GLib.idle_add(self._devicecode_error, session, dlg, done, str(error))
                    return
                done[0] = True
                GLib.idle_add(self._devicecode_success, session, dlg, tok)
                return
            err = tok.get("error", "")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            GLib.idle_add(self._devicecode_error, session, dlg, done,
                          tok.get("error_description") or err
                          or tr("Sign-in failed"))
            return

    def _devicecode_present(self, dlg, cancelled, done, ui, dc):
        if cancelled.is_set() or done[0]:
            return False
        ui["state"]["url"] = (dc.get("verification_uri")
                              or dc.get("verification_url") or "")
        ui["state"]["code"] = dc.get("user_code", "")
        ui["url_lbl"].set_text(ui["state"]["url"])
        ui["code_lbl"].set_markup(
            '<span size="xx-large" weight="bold">%s</span>'
            % GLib.markup_escape_text(ui["state"]["code"]))
        ui["open_btn"].set_sensitive(bool(ui["state"]["url"]))
        ui["copy_btn"].set_sensitive(bool(ui["state"]["code"]))
        ui["status"].set_text(tr("Waiting for you to finish signing in…"))
        return False

    def _devicecode_success(self, session, dlg, tok):
        if self._signin_dlg is dlg:
            self._signin_dlg = None
        dlg.destroy()
        self._show("busy", session)
        self._finish_signin(session, tok)
        return False

    def _devicecode_error(self, session, dlg, done, message):
        done[0] = True
        if self._signin_dlg is dlg:
            self._signin_dlg = None
        dlg.destroy()
        self._signin_failed(session, message)
        return False

    def _sign_out(self):
        session = self.current
        if session is None:
            return
        session.close()
        if session.refresh_source:
            GLib.source_remove(session.refresh_source)
            session.refresh_source = 0
        self.registry.remove(session.id)
        if session.web_session is not None:
            session.web_session.get_website_data_manager().clear(
                WebKit.WebsiteDataTypes.ALL, 0, None, None, None)
            session.web_session = None
        shutil.rmtree(session.directory, ignore_errors=True)
        self.selected_id = self.registry.selected_id
        self._clear_tiles()
        self._rebuild_account_menu()
        if self.current:
            self._select_account(self.selected_id)
        else:
            self._update_account("")
            self._show("signin")
            self._set_connecting("", "", False, session)

    def _reload_feed(self):
        if self.view_mode == "all":
            for owner in self.accounts.values():
                if not owner.removed:
                    threading.Thread(target=self._background_refresh, args=(owner,), daemon=True).start()
            return
        session = self.current
        if not session or not session.token:
            self._interactive_signin(session)
            return
        self._load_feed_bg(session)

    def _load_feed_bg(self, session):
        self._show("busy", session)
        threading.Thread(target=self._load_feed, args=(session,), daemon=True).start()

    def _load_feed(self, session):
        if not self._ensure_token(session):
            self._set_status(self._refresh_failed_status(session), session, reauth=True)
            if not session.resources:
                self._show("signin", session)
            return
        try:
            resources = session.client.enumerate_feed(session.token)
        except Exception as error:
            session.pending_launch = None
            if session.resources:
                self._show("workspaces", session)
                self._set_status(tr("Couldn't refresh workspaces — showing saved list"), session)
            else:
                self._show("signin", session)
                if self.view_mode != "all" and self._visible(session):
                    self._error(tr("Feed error: {error}").format(error=str(error)))
            return
        if session.save_resources(resources):
            GLib.idle_add(self._populate, resources, session)

    def _set_view_mode(self, mode):
        if mode not in ("account", "all") or self.view_mode == mode:
            return
        self.view_mode = mode
        self._settings["_account_view"] = mode
        storage.save_settings(self._settings)
        self._render_resources()
        self._set_connecting("", "", False)
        if mode == "all":
            self._reload_feed()

    def _clear_tiles(self):
        for child in self._tiles:
            parent = child.get_parent()
            if parent:
                parent.remove(child)
        self._tiles = []
        child = self._resource_box.get_first_child()
        while child:
            self._resource_box.remove(child)
            child = self._resource_box.get_first_child()
        self._account_sections = {}
        self._kind_boxes = []
        self._tray_refresh()

    def _render_resources(self, session=None):
        partial = self.view_mode == "all" and session is not None and session.id in self._account_sections
        if partial:
            section = self._account_sections.pop(session.id)
            previous = section.get_prev_sibling()
            for child in self._tiles:
                if child._account is session:
                    child.get_parent().remove(child)
            self._tiles = [child for child in self._tiles if child._account is not session]
            self._kind_boxes = [box for box in self._kind_boxes if not box.is_ancestor(section)]
            self._resource_box.remove(section)
        else:
            self._clear_tiles()
            self._account_status_widgets = {}
        owners = ([owner for owner in self.accounts.values() if not owner.removed]
                  if self.view_mode == "all" else ([self.current] if self.current else []))
        for owner in ([session] if partial else owners):
            section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            if partial:
                self._resource_box.insert_child_after(section, previous)
            else:
                self._resource_box.append(section)
            self._account_sections[owner.id] = section
            owner._rendered_resources = [dict(resource) for resource in owner.resources]
            if self.view_mode == "all":
                heading = Gtk.Label(label=owner.display_name + " · " + owner.client.upn, xalign=0,
                                    margin_start=22, margin_end=22, wrap=True)
                heading.add_css_class("tname")
                section.append(heading)
                message = getattr(owner, "view_status", "")
                row = Gtk.Box(spacing=12, margin_start=22, margin_end=22)
                status_label = Gtk.Label(label=message or ("No resources available" if not owner.resources else ""), xalign=0, wrap=True)
                status_label.add_css_class("status")
                row.append(status_label)
                signin = Gtk.Button(label="Sign in")
                signin.add_css_class("flat")
                signin.connect("clicked", lambda *_, session=owner: self._interactive_signin(session))
                signin.set_visible(getattr(owner, "needs_signin", False))
                row.append(signin)
                self._account_status_widgets[owner.id] = (status_label, signin)
                row.set_visible(bool(message) or not owner.resources)
                section.append(row)
            # Cloud PCs first, then pooled desktops, then apps by workspace.
            # Vertical rhythm comes from box spacing only (never margins on the
            # first/last child), so hiding sections or groups while filtering
            # keeps every remaining gap identical.
            kinds = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28,
                            margin_top=10, margin_bottom=18)
            section.append(kinds)
            for kind, groups in resource_groups.sections(owner.resources):
                kind_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                kind_box._groups = []
                caption = Gtk.Box(spacing=8, margin_start=22, margin_end=22)
                caption.append(Gtk.Image.new_from_icon_name("pan-down-symbolic"))
                heading = create(Gtk.Label, label=tr(resource_groups.KIND_LABELS[kind]).upper(), xalign=0)
                heading.add_css_class("section-h")
                caption.append(heading)
                kind_box._count = Gtk.Label(xalign=0)
                kind_box._count.add_css_class("section-count")
                caption.append(kind_box._count)
                group_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
                kind_box._toggle = self._collapse_button(caption, owner.id, "section:" + kind, group_box)
                kind_box.append(kind_box._toggle)
                kind_box.append(group_box)
                kinds.append(kind_box)
                self._kind_boxes.append(kind_box)
                for workspace, resources in groups:
                    group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                    group._count = None
                    group._toggle = None
                    grid = Gtk.FlowBox(valign=Gtk.Align.START, max_children_per_line=5,
                                       min_children_per_line=2, row_spacing=14, column_spacing=14,
                                       homogeneous=True, selection_mode=Gtk.SelectionMode.NONE,
                                       margin_start=22, margin_end=22)
                    if workspace:
                        # Workspace subgroup: a lighter, mixed-case heading with a
                        # hairline so it reads as subordinate to the section caption.
                        sub = Gtk.Box(spacing=8, margin_start=22, margin_end=22)
                        sub.append(Gtk.Image.new_from_icon_name("pan-down-symbolic"))
                        label = Gtk.Label(label=workspace, xalign=0, wrap=True)
                        label.add_css_class("subsection-h")
                        sub.append(label)
                        group._count = Gtk.Label(xalign=0)
                        group._count.add_css_class("section-count")
                        sub.append(group._count)
                        rule = Gtk.Separator(hexpand=True, valign=Gtk.Align.CENTER)
                        rule.add_css_class("subsection-rule")
                        sub.append(rule)
                        group._toggle = self._collapse_button(sub, owner.id, "workspace:" + workspace, grid)
                        group.append(group._toggle)
                    grid.connect("child-activated", self._on_tile_activated)
                    grid.set_filter_func(self._filter_tile)
                    group.append(grid)
                    group_box.append(group)
                    self.grid = grid
                    group._tiles = []
                    kind_box._groups.append(group)
                    for res in resources:
                        child = owner.tiles.get(res["id"])
                        if child is None:
                            child = self._make_tile(res, owner)
                            owner.tiles[res["id"]] = child
                        child._res = res
                        grid.append(child)
                        self._tiles.append(child)
                        group._tiles.append(child)
            kinds.set_visible(bool(owner.resources))
            if self.view_mode != "all" and not owner.resources:
                section.append(self._empty_state())
        self._refresh_filter_options()
        self._apply_filter()
        bind(self.status, 'text',
             tr("All accounts") if self.view_mode == "all"
             else self.current.display_name if self.current and self.current.local
             else tr("Signed in as {upn}").format(upn=self.current.client.upn) if self.current
             else "")
        self._update_account(self.current.client.upn if self.current else "", self.current)
        self._rebuild_account_menu()
        self._tray_refresh()
        self.stack.set_visible_child_name("workspaces" if owners else "signin")
        self._signout_item.set_sensitive(self.current is not None)

    def _populate(self, resources, session=None):
        session = session or self.current
        if session is None or session.removed:
            return False
        changed = (session.id not in self._account_sections or
                   getattr(session, "_rendered_resources", None) != resources)
        session.resources = resources
        session.view_status = ""
        session.needs_signin = False
        if self._visible(session):
            if changed:
                self._render_resources(session)
            else:
                widgets = self._account_status_widgets.get(session.id)
                if widgets:
                    widgets[0].set_text("" if resources else "No resources available")
                    widgets[1].set_visible(False)
                    widgets[0].get_parent().set_visible(not resources)
                self.stack.set_visible_child_name("workspaces")
                self.status.set_text("All accounts" if self.view_mode == "all" else
                                     session.display_name if session.local else
                                     f"Signed in as {session.client.upn}")
        else:
            self._rebuild_account_menu()
            self._tray_refresh()
        icon_resources = resources
        if getattr(session, "_icon_resources", None) == resources:
            icon_resources = [res for res in resources if
                              (res.get("icon32") and
                               not os.path.isfile(storage.icon_path(res["id"], session.directory)))
                              or (res.get("icon_raw") and
                                  not os.path.isfile(storage.large_icon_path(res["id"], session.directory)))]
        if icon_resources:
            session._icon_resources = [dict(resource) for resource in resources]
            tiles = [session.tiles[res["id"]] for res in icon_resources if res["id"] in session.tiles]
            threading.Thread(target=self._load_icons, args=(icon_resources, session, tiles), daemon=True).start()
        pending, session.pending_launch = session.pending_launch, None
        if pending:
            child = self._child_for(pending[0], session)
            if child:
                self._on_tile_activated(self.grid, child, windowed=pending[1])
        return False

    def _make_tile(self, res, session):
        is_desktop = res["type"] == "Desktop"
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
        tile.add_css_class("tile")

        # top row: tinted icon plate (left) + status pill (top-right)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        ic = Gtk.Box(); ic.add_css_class("ic")
        ic.add_css_class("desktop" if is_desktop else "app")
        ic.set_halign(Gtk.Align.START); ic.set_valign(Gtk.Align.CENTER)
        ic.set_size_request(40, 40)          # fixed square, so the glyph centers
        img = Gtk.Image.new_from_icon_name(
            "computer-symbolic" if is_desktop else "view-grid-symbolic")
        img.set_pixel_size(22)
        img.set_hexpand(True); img.set_vexpand(True)
        img.set_halign(Gtk.Align.CENTER); img.set_valign(Gtk.Align.CENTER)
        ic.append(img)
        pill = create(Gtk.Label, label="")
        pill.add_css_class("status-pill")
        pill.set_valign(Gtk.Align.START)
        pill.set_visible(False)
        spacer = Gtk.Box(); spacer.set_hexpand(True)
        top.append(ic); top.append(spacer); top.append(pill)

        # meta: title + type chip
        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        title = create(Gtk.Label, label=res["title"], xalign=0); title.add_css_class("tname")
        title.set_wrap(True); title.set_max_width_chars(18); title.set_lines(2)
        title.set_ellipsize(3)
        chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        if not is_desktop:
            chip.add_css_class("chip-app")
        chip.set_halign(Gtk.Align.START)
        dot = Gtk.Box(); dot.add_css_class("chipdot"); dot.set_valign(Gtk.Align.CENTER)
        clabel = create(Gtk.Label, label=tr("DESKTOP") if is_desktop else tr("REMOTE APP"))
        clabel.add_css_class("chiplabel")
        chip.append(dot); chip.append(clabel)
        chip.set_valign(Gtk.Align.CENTER)

        # Session actions sit on the chip row, which keeps a fixed height so
        # the tile layout doesn't shift when a session connects.
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        actions.add_css_class("tile-actions")
        actions.set_halign(Gtk.Align.END); actions.set_hexpand(True)
        actions.set_valign(Gtk.Align.CENTER)
        actions.set_visible(False)
        chiprow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        chiprow.add_css_class("tile-chiprow")
        chiprow.append(chip); chiprow.append(actions)
        meta.append(title); meta.append(chiprow)

        tile.append(top); tile.append(meta)

        child = Gtk.FlowBoxChild()
        child.set_child(tile)
        for icon, tip, callback in (
                ("focus-windows-symbolic", tr("Show window"), lambda: self._focus_session(child)),
                ("view-refresh-symbolic", tr("Reconnect"),
                 lambda: self._disconnect_session(child, reconnect=True)),
                ("system-log-out-symbolic", tr("Disconnect"), lambda: self._disconnect_session(child))):
            button = Gtk.Button.new_from_icon_name(icon)
            button.add_css_class("tile-action")
            button.set_focus_on_click(False)
            button.set_tooltip_text(tip)
            button.update_property([Gtk.AccessibleProperty.LABEL], [GObject.Value(str, tip)])
            button.connect("clicked", lambda _b, cb=callback: cb())
            actions.append(button)
        child._actions = actions
        child._account = session
        child._res = res
        child._img = img
        try:
            img.set_from_paintable(Gdk.Texture.new_from_filename(storage.icon_path(res["id"], session.directory)))
        except GLib.Error:
            pass
        child._tile = tile
        child._pill = pill
        child._proc = None
        child._launching = False
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_tile_key, child)
        child.add_controller(keys)
        bind(child, 'tooltip_text',
            tr("{resource} — {workspace}\nDouble-click to connect · right-click for actions").format(
                resource=res["title"], workspace=tray_menu.workspace_name(res)))
        rclick = Gtk.GestureClick()
        rclick.set_button(3)
        rclick.connect("pressed", lambda g, n, x, y: self._show_resource_menu(child, x, y))
        child.add_controller(rclick)
        return child

    def _child_for(self, res_id, session=None):
        session = session or self.current
        return session.tiles.get(res_id) if session else None

    def _set_tile_state(self, res_id, css, session=None, attempt=None, allow_cancelled=False):
        session = session or self.current
        if session is None:
            return
        key = (session.id, res_id)
        # Render session state as a corner pill: amber "Connecting", green
        # "Connected", red "Failed", hidden when idle.
        # (css: state-connecting/connected/error/ended)
        def apply():
            if attempt is not None and (not attempt.is_current()
                    or (attempt.cancelled.is_set() and not allow_cancelled)):
                return False
            if css in ("state-connecting", "state-connected"):
                self._sessions[key] = css[len("state-"):]
            else:
                self._sessions.pop(key, None)
            self._tray_refresh()
            ch = self._child_for(res_id, session)
            if not ch:
                return
            pill, tile = ch._pill, ch._tile
            if getattr(ch, "_actions", None) is not None:
                ch._actions.set_visible(css == "state-connected")
            for name in ("pill-connecting", "pill-error"):
                pill.remove_css_class(name)
            for name in ("tile-connected", "tile-error"):
                tile.remove_css_class(name)
            if css == "state-error":
                bind(pill, 'text', tr("FAILED"))
                pill.add_css_class("pill-error")
                pill.set_visible(True)
                tile.add_css_class("tile-error")
            elif css == "state-connected":
                bind(pill, 'text', tr("CONNECTED"))
                pill.set_visible(True)
                tile.add_css_class("tile-connected")
            elif css == "state-connecting":
                bind(pill, 'text', tr("CONNECTING"))
                pill.add_css_class("pill-connecting")
                pill.set_visible(True)
            else:
                pill.set_visible(False)
        GLib.idle_add(apply)

    def _load_large_icon(self, res, session, token):
        """Cache the feed's IconRaw as PNG for app launcher entries."""
        url = res.get("icon_raw")
        path = storage.large_icon_path(res["id"], session.directory)
        if not url or not token or os.path.isfile(path):
            return
        try:
            png = launchers.ico_to_png(storage.bearer_bytes(url, token))
        except Exception:
            png = None
        if not png:
            return
        with session.refresh_lock:
            if session.removed:
                return
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            with open(path, "wb") as f:
                f.write(png)

    def _load_icons(self, resources, session, tiles):
        children = {c._res["id"]: c for c in tiles}
        token = session.token
        for res in resources:
            if session.removed:
                return
            self._load_large_icon(res, session, token)
            url = res.get("icon32")
            if not url:
                continue
            path = storage.icon_path(res["id"], session.directory)
            data = None
            if token:
                try:
                    data = storage.bearer_bytes(url, token)
                    with session.refresh_lock:
                        if session.removed:
                            return
                        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
                        with open(path, "wb") as f:
                            f.write(data)
                except Exception:
                    data = None
            if data is None:            # no token, or the fetch failed → cache
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                except OSError:
                    continue
            try:
                # Load the PNG straight into a GdkTexture (the Image scales it to
                # its pixel size); avoids the deprecated new_for_pixbuf path.
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            except Exception:
                continue
            def apply(resource_id=res["id"], texture=tex):
                if session.removed:
                    return False
                child = session.tiles.get(resource_id) or children.get(resource_id)
                if child is not None and child._account is session:
                    child._img.set_from_paintable(texture)
                return False
            GLib.idle_add(apply)

    # ---- launch + live connection state -----------------------------------
    def _on_tile_activated(self, flowbox, child, windowed=False):
        session = child._account
        if session.removed:
            return
        res = child._res
        # Guard synchronously on the main thread: the second click of a
        # double-click arrives before the launch thread has set child._proc, so
        # a flag set here (not the proc handle) is what prevents a second launch.
        if getattr(child, "_launching", False) or (child._proc and child._proc.poll() is None):
            self._set_status(tr("{resource} is already open").format(resource=res['title']), session)
            return
        attempt = ConnectionAttempt()
        child._attempt = attempt
        attempt.is_current = lambda: getattr(child, "_attempt", None) is attempt
        child._launching = True
        child._windowed = windowed
        child._reconnect = False
        self._set_tile_state(res["id"], "state-connecting", session, attempt=attempt)
        self._set_status(tr("Connecting to {resource}…").format(resource=res['title']), session, attempt=attempt)
        self._set_connecting(res["id"], res["title"], True, session, attempt=attempt)
        threading.Thread(target=self._launch, args=(child, res, windowed, session, attempt),
                         daemon=True).start()

    def _cancel_visible_connections(self):
        for account_id, resource_id in tuple(self._connecting):
            if self.view_mode != "all" and account_id != self.selected_id:
                continue
            session = self.accounts.get(account_id)
            child = self._child_for(resource_id, session) if session else None
            if child:
                self._cancel_connection(child, child._res, session, getattr(child, "_attempt", None))

    def _cancel_connection(self, child, res, session, attempt):
        if attempt is None or not attempt.is_current():
            return
        attempt.cancel()
        close_auth, attempt.close_auth = attempt.close_auth, None
        if close_auth:
            close_auth()
        child._launching = False
        child._proc = None
        if session.pending_launch and session.pending_launch[0] == res["id"]:
            session.pending_launch = None
        # Guard the deferred cleanup by identity, even though this attempt is cancelled.
        def clear():
            if attempt.is_current():
                self._set_connecting(res["id"], res["title"], False, session, attempt=attempt, allow_cancelled=True)
                self._set_tile_state(res["id"], None, session, attempt=attempt, allow_cancelled=True)
                self._set_status(tr("Connection cancelled"), session, attempt=attempt, allow_cancelled=True)
            return GLib.SOURCE_REMOVE
        clear()

    def _launch(self, child, res, windowed, session, attempt=None, monitor_retries=0):
        if attempt is None:
            attempt = ConnectionAttempt()
            child._attempt = attempt
            attempt.is_current = lambda: getattr(child, "_attempt", None) is attempt
        if not attempt.active():
            return
        token_ready = self._ensure_token(session)
        if not attempt.active():
            return
        if not token_ready:
            # Session lapsed and can't refresh silently — re-auth on demand
            # (like the Windows App when you click a workspace after a while),
            # keeping the workspace list intact.
            if attempt.is_current():
                child._launching = False
            self._set_connecting(res["id"], res["title"], False, session, attempt=attempt)
            self._set_tile_state(res["id"], None, session, attempt=attempt)
            self._set_status(tr("Sign in to connect"), session, attempt=attempt)
            session.pending_launch = (res["id"], windowed)   # connect automatically afterwards
            GLib.idle_add(lambda: self._interactive_signin(session) if attempt.active() else False)
            return
        try:
            with session.refresh_lock:
                if session.removed or not attempt.active():
                    if attempt.is_current():
                        child._launching = False
                    return
                path = (self._prepare_local_rdp(res) if session.local
                        else self._download_resource_rdp(child, res, session, windowed, attempt))
                if not attempt.active():
                    return
                if path is None:
                    return
        except (SystemExit, Exception) as e:  # never let a write/HTTP error hang the tile
            if not attempt.active():
                return
            if attempt.is_current():
                child._launching = False
            self._set_connecting(res["id"], res["title"], False, session, attempt=attempt)
            self._error(str(e))
            self._set_tile_state(res["id"], "state-ended", session, attempt=attempt)
            return
        try:
            security = resolve_security(path, self._eff("sec", res, session),
                                        self._eff_extra(res, session),
                                        session.auth_modes.get(res["id"]))
        except (OSError, ValueError) as error:
            child._launching = False
            self._set_connecting(res["id"], res["title"], False, session)
            self._set_tile_state(res["id"], "state-ended", session)
            self._error(tr("Could not prepare authentication: {error}").format(error=error))
            return
        child._security_decision = security
        nla = security.mode == "nla"
        env = dict(os.environ)
        # sdl-freerdp (SDL3) and FreeRDP's own AAD webview are unstable on native
        # Wayland (#2: "Error 71 dispatching to Wayland display"). Force X11 /
        # XWayland for the child, which is stable — and drop WAYLAND_DISPLAY so
        # nothing in the subprocess re-selects Wayland.
        env["GDK_BACKEND"] = "x11"
        env["SDL_VIDEODRIVER"] = "x11"
        wayland_display = env.pop("WAYLAND_DISPLAY", None)
        if wayland_display:
            env["AVD_PICKER_WAYLAND_DISPLAY"] = wayland_display
        # FreeRDP no longer has its own webview (we resolve AAD in-process), but
        # keep this set for the child in case any bundled component initializes
        # WebKitGTK — the DMABUF/GBM path blanks on some GPUs. Harmless otherwise.
        env["WEBKIT_DISABLE_DMABUF_RENDERER"] = "1"
        # NVIDIA's GLX driver on XWayland can block forever in an explicit-sync
        # wait while the window is on a hidden workspace, freezing the session's
        # keyboard and mouse. SDL's Vulkan renderer without vsync doesn't stall.
        if os.path.exists("/proc/driver/nvidia/version"):
            env.setdefault("SDL_RENDER_DRIVER", "vulkan")
            env.setdefault("SDL_RENDER_VSYNC", "0")
        # Reduce tearing in the SDL3 client (vsync + double buffer). SDL hints,
        # ignored where unsupported, so always safe.
        env.setdefault("SDL_RENDER_VSYNC", "1")
        env.setdefault("SDL_VIDEO_DOUBLE_BUFFER", "1")
        if config.SDL_LIBS and os.path.isdir(config.SDL_LIBS):
            env["LD_LIBRARY_PATH"] = config.SDL_LIBS + (
                os.pathsep + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
        # Teams call optimization: the bundled freerdp-teams-redir add-in runs
        # Teams call media on the local mic, speakers and camera instead of the
        # session host. On (audio and video) unless the settings turn it down.
        teams_mode = self._eff_teams(res, session)
        teams_redir = teams_mode != "off"
        if teams_redir:
            # Never write transcripts: they hold TURN creds / ICE passwords.
            env["TEAMS_REDIR_TRANSCRIPTS"] = "off"
            if teams_mode == "video":
                # Hold a closed video window a little longer to smooth Teams'
                # frequent open/close churn. VAAPI stays off (the Intel HW
                # encoder crashes the client).
                env.setdefault("TEAMS_REDIR_VIDEO_KEEP_MS", "8000")
            else:
                env["TEAMS_REDIR_VIDEO"] = "off"
        safe = _re.sub(r"[^A-Za-z0-9]+", "_", res["title"])[:40]
        logpath = session_log_path(
            session.client.output_dir,
            f"session_{safe}_{hashlib.sha256(res['id'].encode()).hexdigest()[:16]}")
        scheme_conn = self._scheme_connect(session)
        gw = config.gateway_arg()
        inject_bearer = self._inject_bearer(session)
        if inject_bearer:
            # Hand FreeRDP the AVD access token we already hold (same audience as
            # the feed scope) so it skips the ARM gateway's own "Browse to:"
            # prompt — arm.c only prompts when GatewayHttpExtAuthBearer is unset.
            # The complete argument list is passed through a private descriptor.
            gw = gw + ",bearer:" + session.token
        argv = [config.SDL, path, f"/gateway:{gw}",
                "/sec:nla" if nla else "/sec:aad"]
        # Sovereign clouds point FreeRDP's connection-time AAD at their own Entra
        # via /azure; scheme_conn additionally swaps the redirect to ms-appx-web
        # so the prompt auto-captures (commercial default stays empty — the
        # seamless SSO webview — unless scheme_conn forces the browser path).
        az = config.azure_arg(scheme=scheme_conn)
        if az:
            argv.append(f"/azure:{az}")
        if session.client.upn:
            argv.append(f"/u:{session.client.upn}")
        # RemoteApp (RAIL) tiles: the bundled SDL client can't render RAIL windows,
        # so a RemoteApp can optionally be opened as the host's full DESKTOP instead
        # (per-resource "Open as full desktop" setting, or AVD_REMOTEAPP=desktop).
        # Keep RemoteApplicationProgram/loadBalanceInfo — AVD's ARM resolver needs
        # them — and only flip remoteapplicationmode off, then connect as a normal
        # desktop below. Off by default: RemoteApp tiles otherwise behave as before.
        force_desktop = os.environ.get("AVD_REMOTEAPP", "").strip().lower() == "desktop" \
            or self._eff("remoteapp", res, session) == "desktop"
        if force_desktop and rdp_is_remoteapp(path):
            set_rdp_desktop_mode(path)
        # Remote scale follows the client's display scale (HiDPI → 200%, standard/
        # ultrawide → 100%); AVD_SCALE overrides. Multi-monitor and any other flag
        # are opt-in via AVD_EXTRA_ARGS (e.g. "/multimon /gfx"), until a settings UI.
        # --- display config: env var > per-resource/default setting > auto ---
        extra = shlex.join(security.options)
        if windowed:
            # A resizable window instead of /f: build_display_args drops the
            # fullscreen/smart-sizing/multimon flags and the feed's screen mode.
            extra = (extra + " /dynamic-resolution").strip()
        # AVD NLA host pools reject FreeRDP's default "AzureAD" domain — both
        # reporters on issue #3 connect only with the domain cleared. Send an
        # empty domain so the user just types their password (a rare pool that
        # needs "AzureAD" can override with /d: in Advanced flags).
        if nla and "/d:" not in extra:
            argv.append("/d:")
        scale = os.environ.get("AVD_SCALE") or self._eff("scale", res, session) \
            or str(100 * max(1, self._scale))
        # Pin the host cert on first use by default; only turn the check off if
        # the user opted out (e.g. a pooled host pool whose certs rotate). Env
        # override: AVD_CERT=ignore / verify.
        cert_mode = os.environ.get("AVD_CERT", "").strip().lower() \
            or self._eff("cert", res, session)
        cert_flag = "/cert:ignore" if cert_mode == "ignore" else "/cert:tofu"
        disable_client_hotkeys()
        argv += ["/sound:sys:pulse", "/microphone", cert_flag,
                 f"/scale-desktop:{scale}", "/log-level:info",
                 # bandwidth/quality + resilience + keepalive:
                 "+compression", "+fonts",
                 "+auto-reconnect", "/auto-reconnect-max-retries:10",
                 # inject fake input so the Azure gateway doesn't idle-drop the
                 # session (which would otherwise force a reconnect + token re-mint)
                 "/prevent-session-lock:120"]
        if teams_redir:
            argv.append("/dvc:teams-redir")
        argv += webauthn_args(path, security.options)
        mm = os.environ.get("AVD_MULTIMON", "").strip().lower()
        forced_multimon = mm in ("1", "on", "true", "yes")
        # Read the live display layout now, at connect time — not the monitor count
        # cached when the UI started, which goes stale if displays are plugged in or
        # rearranged while the app runs. display.monitor_layout() uses its own X
        # connection, so it is safe from this worker thread (GDK/_detect_displays are
        # not). Falls back to the cached count only when the layout can't be read.
        layout = display.monitor_layout()
        n_monitors = len(layout) if layout else self._n_monitors
        if forced_multimon:
            want_multimon = True
        elif mm in ("0", "off", "false", "no"):
            want_multimon = False
        else:
            setting = self._eff("multimon", res, session)      # "on"/"off"/None
            want_multimon = (setting == "on") if setting else (n_monitors > 1)
        # The bundled FreeRDP anchors the monitor layout at the top-left monitor, so
        # a multi-monitor session works for any primary — except a staggered layout
        # where no monitor sits at the top-left corner, which can't be anchored
        # without a negative origin the AVD host stalls on. In that rare case connect
        # on the primary only and say why, rather than hang (see display.py).
        # AVD_MULTIMON=1 forces the real multi-monitor path regardless.
        if want_multimon and not forced_multimon:
            if display.multimon_will_wedge_now():
                want_multimon = False
                self._toast(tr("Connected on your primary display only. Multi-monitor "
                               "needs one of your displays at the top-left corner of the "
                               "layout; line up your displays' top edges in your system's "
                               "Display settings to use all monitors."), timeout=14)
        try:
            argv += build_display_args(path, extra, want_multimon)
        except (OSError, ValueError) as error:
            if not attempt.active():
                return
            if attempt.is_current():
                child._launching = False
            self._set_connecting(res["id"], res["title"], False, session, attempt=attempt)
            self._set_tile_state(res["id"], "state-ended", session, attempt=attempt)
            self._error(tr("Could not prepare display settings: {error}").format(error=error))
            return
        # Local folder → remote session (appears under \\tsclient\…). Env
        # AVD_SHARE wins; otherwise Default / per-resource Shared folder setting.
        share_setting = self._eff("share", res, session)
        share_path = self._eff_share_path(res, session)
        share_mode, share_path = resolve_share_mode(share_setting, share_path)
        argv += drive_redirect_args(path, share_mode, share_path, extra=extra)
        backend = workspace_backend()
        workspace_title = None
        hypr_workspace = None
        if isinstance(backend, HyprlandWorkspaceBackend):
            # Hyprland has no xwayland-keyboard-grab, so FreeRDP's X keyboard grab
            # (taken on pointer enter) only goes stale across workspace switches
            # and leaves the session ignoring input. The cloudpc submap already
            # passes shortcuts through to Windows.
            if "grab-keyboard" not in extra:
                argv.append("-grab-keyboard")
            wanted = self._eff("hypr_workspace", res, session)
            if res.get("type") == "Desktop" and valid_workspace(wanted):
                hypr_workspace = wanted
                workspace_title = "CloudPC-" + uuid.uuid4().hex[:12] + " " + res.get("title", "")
                argv.append("/t:" + workspace_title)
        # Launch under a PTY. With FreeRDP built WITHOUT its own AAD webview,
        # /sec:aad prints "Browse to: <url>" and reads the redirect URL back
        # from stdin — but only when it thinks it's attached to a terminal, so
        # we give it a real pty. We service that prompt ourselves in a hidden
        # webview that shares the launcher's SSO cookies, making the
        # connection-time token silent. A new process group lets us tear down
        # the whole FreeRDP tree cleanly.
        with session.refresh_lock:
            if session.removed or not attempt.active():
                if attempt.is_current():
                    child._launching = False
                self._set_connecting(res["id"], res["title"], False, session, attempt=attempt)
                return
            master, slave = pty.openpty()
            args_fd = None
            try:
                attributes = termios.tcgetattr(slave)
                attributes[3] &= ~termios.ECHO
                termios.tcsetattr(slave, termios.TCSANOW, attributes)
                command = argv
                extra_fds = ()
                if inject_bearer:
                    command, args_fd = private_argument_file(argv)
                    extra_fds = (args_fd,)
                proc = subprocess.Popen(command, env=env, stdin=slave, stdout=slave,
                                        stderr=slave, start_new_session=True,
                                        close_fds=True, pass_fds=extra_fds)
                attempt.attach(proc)
            except Exception as error:
                os.close(master)
                if not attempt.active():
                    return
                if attempt.is_current():
                    child._launching = False
                self._set_connecting(res["id"], res["title"], False, session, attempt=attempt)
                self._set_tile_state(res["id"], None, session, attempt=attempt)
                self._error(tr("Could not launch FreeRDP: {error}").format(error=error))
                return
            finally:
                os.close(slave)
                if args_fd is not None:
                    os.close(args_fd)
        def publish():
            if hypr_workspace:
                child._workspace_placement = backend.create_placement(
                    proc, workspace_title.split(" ", 1)[0], hypr_workspace,
                    follow=self._settings.get("_follow_workspace", True))
                GLib.idle_add(child._workspace_placement.start)
            else:
                child._workspace_placement = None
            child._proc = proc
            child._ptym = master
        if not attempt.publish(publish):
            attempt.cancel()
            os.close(master)
            return
        GLib.idle_add(self._tray_refresh)    # Focus/Disconnect now apply
        self._set_status(tr("Launched {resource}").format(resource=res['title']), session, attempt=attempt)
        retry = self._watch_session(
            child, res, proc, master, logpath, session, attempt,
            retry_monitor=monitor_retries < 2)
        if retry:
            self._set_status(tr("Display not ready; retrying {resource}…").format(
                resource=res['title']), session, attempt=attempt)
            if not attempt.cancelled.wait(1) and attempt.active() and not session.removed:
                self._launch(child, res, windowed, session, attempt, monitor_retries + 1)

    _ANSI = _re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")

    def _watch_session(self, child, res, proc, master, logpath, session, attempt=None,
                       retry_monitor=False):
        """Read sdl-freerdp's PTY output: flip the tile to 'Connected' on logon,
        service each 'Browse to:' AAD prompt silently, and tidy up on exit."""
        attempt = attempt or getattr(child, "_attempt", None)
        state = {"connected": False, "attempt": attempt}
        buf = b""
        try:
            # The local ".rdp files" account never runs a feed download, so its
            # output_dir (…/feed) may not exist yet; create it so the session
            # log isn't silently dropped to /dev/null (issue #17).
            os.makedirs(os.path.dirname(logpath), mode=0o700, exist_ok=True)
            log = open(logpath, "xb")
        except OSError:
            log = open(os.devnull, "wb")
        decision = getattr(child, "_security_decision", None)
        if decision:
            log.write(f"[AVD auth] mode={decision.mode}; reason={decision.reason}\n".encode())
            log.flush()
        try:
            while True:
                try:
                    r, _, _ = select.select([master], [], [], 1.0)
                except (OSError, ValueError):
                    break
                if r:
                    try:
                        data = os.read(master, 4096)
                    except OSError:
                        break
                    if not data:            # EOF: child closed the pty (exited)
                        break
                    log.write(data)
                    log.flush()
                    buf += data
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        line = self._ANSI.sub(b"", raw).decode("utf-8", "replace").strip()
                        if line:
                            self._on_freerdp_line(child, res, master, line, state, session)
                elif proc.poll() is not None:
                    break
        finally:
            try:
                log.close()
            except Exception:
                pass
            try:
                os.close(master)
            except OSError:
                pass
        for notification_id in state.get("fido_notifications", set()):
            GLib.idle_add(self.withdraw_notification, notification_id)
        if attempt is not None and attempt.close_auth:
            def close_pending_auth():
                if attempt.close_auth:
                    attempt.close_auth()
                return GLib.SOURCE_REMOVE
            GLib.idle_add(close_pending_auth)
        if attempt is not None and not attempt.active():
            return
        monitor_failure = (state.get("monitor_zero") and state.get("preconnect_failed")
                           and not state["connected"] and not state.get("auth_prompt"))
        if monitor_failure:
            if retry_monitor and attempt is not None:
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    if attempt.active() and not session.removed:
                        return True
            if attempt is not None and (not attempt.active() or session.removed):
                return
            self._error(tr("Could not detect a usable display for {resource}. "
                           "Check your display and try again.").format(resource=res['title']))
        child._launching = False
        self._set_connecting(res["id"], res["title"], False, session, attempt=attempt)  # ensure overlay clears
        self._set_tile_state(res["id"], "state-ended", session, attempt=attempt)
        self._set_status(tr("{resource} session ended").format(resource=res['title']), session, attempt=attempt)
        GLib.timeout_add_seconds(
            6, lambda: (self._set_tile_state(res["id"], None, session, attempt=attempt), False)[1])
        if getattr(child, "_reconnect", False):
            child._reconnect = False
            def reconnect():
                if not session.removed and (attempt is None or attempt.is_current()):
                    self._on_tile_activated(self.grid, child,
                                            windowed=getattr(child, "_windowed", False))
                return GLib.SOURCE_REMOVE
            GLib.idle_add(reconnect)

    def _on_freerdp_line(self, child, res, master, line, state, session):
        """Handle one line of FreeRDP output (called from the reader thread)."""
        attempt = state.get("attempt", getattr(child, "_attempt", None))
        if attempt is not None and not attempt.active():
            return
        notification = _re.fullmatch(
            r"AVD_FIDO_NOTIFICATION ([0-9]{1,20}) "
            r"(touch|clear|error (-?[0-9]{1,4}) ([A-Za-z0-9.-]{1,253}))", line)
        if notification and notification.group(3) is not None:
            code, site = int(notification.group(3)), notification.group(4)
            def show_error():
                if not session.removed and (attempt is None or attempt.active()):
                    notice = Gio.Notification.new(tr("Security key"))
                    notice.set_body(fido_error_message(code, site))
                    notice.set_icon(Gio.ThemedIcon.new("dialog-warning-symbolic"))
                    self.send_notification("fido-error", notice)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(show_error)
            return
        if notification:
            notification_id = "fido-" + notification.group(1)
            pending = state.setdefault("fido_notifications", set())
            if notification.group(2) == "touch":
                pending.add(notification_id)
                def show_touch():
                    if not session.removed and (attempt is None or attempt.active()):
                        notice = Gio.Notification.new(tr("Security key"))
                        notice.set_body(tr("Touch your security key to continue."))
                        # A theme icon the notification daemon always has.
                        notice.set_icon(Gio.ThemedIcon.new("dialog-password-symbolic"))
                        self.send_notification(notification_id, notice)
                    return GLib.SOURCE_REMOVE
                GLib.idle_add(show_touch)
            else:
                pending.discard(notification_id)
                GLib.idle_add(self.withdraw_notification, notification_id)
            return
        if "Monitor configuration virtual desktop " in line and " <= 0 <= " in line:
            state["monitor_zero"] = True
        if "ERRCONNECT_PRE_CONNECT_FAILED" in line:
            state["preconnect_failed"] = True
        if not state["connected"] and "Logon Info" in line:
            state["connected"] = True
            decision = getattr(child, "_security_decision", None)
            if decision:
                def remember():
                    try:
                        session.remember_security(res["id"], decision.mode, decision.fingerprint)
                    except OSError as error:
                        print(f"Could not remember authentication mode: {type(error).__name__}", file=sys.stderr)
                    return GLib.SOURCE_REMOVE
                GLib.idle_add(remember)
            placement = getattr(child, "_workspace_placement", None)
            if placement:
                GLib.idle_add(placement.on_connected)
            self._set_tile_state(res["id"], "state-connected", session, attempt=attempt)
            self._set_status(tr("Connected to {resource}").format(resource=res['title']), session, attempt=attempt)
            self._set_connecting(res["id"], res["title"], False, session, attempt=attempt)  # desktop is up
        if line.startswith("Browse to: "):
            state["auth_prompt"] = True
            url = line[len("Browse to: "):].strip()
            if config.is_aad_login_url(url):
                # Resolve on the main thread (WebKit must run there). A local
                # .rdp is for tenants whose CA needs a smart card / CAC, which
                # the embedded WebView can't present — so send those through the
                # system browser instead (issue #17). Sovereign clouds (Gov) are
                # the same case: their accounts are CAC-backed, and the embedded
                # WebView has no smart-card reader, so the feed route's
                # connection-time prompt failed cert validation there too. Route
                # those through the system browser as well (issue #61).
                # Respect the browser sign-in preference for commercial feeds too:
                # the embedded WebView cannot authenticate with every security key.
                use_browser = (session.local or config.CLOUD != "commercial"
                               or self._scheme_connect(session)
                               or self._use_browser_signin())
                resolver = self._resolve_aad_browser if use_browser else self._resolve_aad
                GLib.idle_add(resolver, master, url, res, session, attempt)
            else:
                # Never load an arbitrary URL in the SSO-cookie WebView; a blank
                # line makes FreeRDP abort this auth instead of blocking on stdin.
                print(f"refusing non-Entra AAD URL: {url[:80]}", file=sys.stderr)
                try:
                    os.write(master, b"\n")
                except OSError:
                    pass

    def _resolve_aad(self, master, url, res, session, attempt=None):
        """Resolve one connection's AAD prompt; closing it cancels its attempt."""
        if session.removed or (attempt is not None and not attempt.active()):
            # FreeRDP is blocked on stdin waiting for the redirect URI. Signing
            # out (session.removed) does not itself cancel the attempt, so cancel
            # it here to kill the pending FreeRDP process group rather than leak
            # it; if there is no live attempt, unblock FreeRDP so it aborts.
            if attempt is not None and not attempt.cancelled.is_set():
                attempt.cancel()
            else:
                try:
                    os.write(master, b"\n")
                except OSError:
                    pass
            return False
        wv = WebKit.WebView(network_session=self._web_session(session))
        self._apply_ua(wv)
        win = create(Gtk.Window, title=tr("Sign in — {resource}").format(resource=res['title']), transient_for=self.win)
        # Wide enough for Entra's centred-card layout; narrower gets its mobile one.
        win.set_default_size(760, 680)
        win.set_child(self._signin_surface(wv, win, on_request=lambda: reveal()))
        holder = {"done": False, "shown": False, "timer": 0}

        browser_btn = self._signin_header(win, tr("Sign in — {resource}").format(resource=res["title"]))

        def finish(redirect_uri=None, browser=False):
            if holder["done"]:
                return
            holder["done"] = True
            if holder["timer"]:
                GLib.source_remove(holder["timer"])
                holder["timer"] = 0
            wv.stop_loading()
            if attempt is not None and attempt.close_auth is close_auth:
                attempt.close_auth = None
            if browser:
                # FreeRDP stays blocked on its prompt; the browser flow answers it.
                GLib.idle_add(self._resolve_aad_browser, master, url, res, session, attempt, True)
            elif redirect_uri and not session.removed and (attempt is None or attempt.active()):
                try:
                    os.write(master, (redirect_uri + "\n").encode())
                except OSError:
                    pass
            elif attempt is not None and not attempt.cancelled.is_set():
                child = self._child_for(res["id"], session)
                if child:
                    self._cancel_connection(child, res, session, attempt)
                else:
                    attempt.cancel()
            win.destroy()

        browser_btn.connect("clicked", lambda *_: finish(browser=True))

        def close_auth(*_):
            finish()
            return True

        def on_decide(view, decision, dtype):
            if holder["done"]:
                decision.ignore()
                return True
            if dtype == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
                uri = decision.get_navigation_action().get_request().get_uri()
                print(f"aad sign-in: {_url_path(uri)}", file=sys.stderr)
                if "nativeclient" in uri and "code=" in uri:
                    decision.ignore()
                    finish(uri)
                    return True
                if "/oauth2/nativeclient" in uri:
                    # Entra answered with an error instead of a code. Loading
                    # the redirect would leave an empty page; say what failed.
                    decision.ignore()
                    signin_error(uri)
                    return True
            return False

        def signin_error(uri):
            error, message = _redirect_error(uri)
            print(f"aad sign-in failed: {error} {' '.join(_re.findall(r'AADSTS[0-9]+', message))}",
                  file=sys.stderr)
            if error in ("interaction_required", "login_required", "consent_required") \
                    and not holder.get("prompted"):
                # Silent SSO was refused; ask Entra for the interactive page.
                holder["prompted"] = True
                sep = "&" if "?" in url else "?"
                wv.load_uri(url + sep + "prompt=select_account")
                reveal()
                return
            finish()
            self._set_status(tr("Sign-in to {resource} failed: {error}").format(
                resource=res["title"], error=message), session)

        def on_load_failed(_view, _event, failing_uri, error):
            print(f"aad sign-in load failed: {_url_path(failing_uri)} ({error.message})", file=sys.stderr)
            return False

        wv.connect("create", lambda *_a: None)
        wv.connect("decide-policy", on_decide)
        wv.connect("load-failed", on_load_failed)
        win.connect("close-request", close_auth)
        win.connect("destroy", close_auth)
        if attempt is not None:
            attempt.close_auth = close_auth
        # No login_hint here: without a prompt it sends Entra into Seamless SSO
        # (Kerberos), which fails in the WebView with "Something went wrong".
        wv.load_uri(url)

        def reveal():
            if holder["done"] or holder["shown"]:
                return
            holder["shown"] = True
            self._set_status(tr("Signing in to {resource}…").format(resource=res['title']), session, attempt=attempt)
            win.present()

        def show_if_pending():
            holder["timer"] = 0
            if session.removed or (attempt is not None and not attempt.active()):
                finish()
            else:
                reveal()
            return False
        holder["timer"] = GLib.timeout_add(3000, show_if_pending)
        return False

    # ---- local .rdp files (issue #17) -------------------------------------
    def _local_session(self):
        """Return the synthetic 'Local .rdp files' account, creating + persisting
        it on first use. It has no feed and no token; it only holds .rdp files
        the user opened themselves, for tenants whose Conditional Access blocks
        feed sign-in from Linux but still lets a downloaded .rdp connect."""
        session = self.accounts.get(LOCAL_PROFILE_ID)
        if session is not None:
            return session
        profile = {'id': LOCAL_PROFILE_ID, 'local': True,
                   'tid': '', 'oid': '', 'upn': '', 'alias': tr("Local .rdp files")}
        if not any(p.get('id') == LOCAL_PROFILE_ID for p in self.registry.profiles):
            self.registry.profiles.append(profile)
            self.registry.save()
        session = AccountSession(profile, self.registry.client(LOCAL_PROFILE_ID),
                                 profile_dir(self.registry.root, LOCAL_PROFILE_ID))
        self.accounts[LOCAL_PROFILE_ID] = session
        return session

    def _open_local_rdp(self):
        """Pick a .rdp/.rdpw downloaded from the AVD web client and add it as a
        tile under the local account."""
        dialog = Gtk.FileDialog()
        dialog.set_title(tr("Open a downloaded .rdp"))
        filt = Gtk.FileFilter()
        filt.set_name(tr("Remote Desktop files"))
        for pattern in ("*.rdp", "*.rdpw", "*.RDP", "*.RDPW"):
            filt.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filt)
        dialog.set_filters(filters)
        dialog.set_default_filter(filt)
        parent = self.win if self.win and self.win.get_visible() else None

        def done(dlg, result):
            try:
                gfile = dlg.open_finish(result)
            except GLib.Error:
                return                       # cancelled or dismissed — nothing to do
            finally:
                self._open_dialog = None     # release the kept reference
            try:
                path = gfile.get_path() if gfile else None
                if path:
                    self._import_rdp(path)
            except Exception as error:       # never fail silently in the async cb
                self._error(tr("Could not open that .rdp file: {error}").format(error=error))
        # Keep a reference while the chooser is open: a Gtk.FileDialog held only
        # in a local would be garbage-collected before its async callback fires,
        # so picking a file silently did nothing (issue #17).
        self._open_dialog = dialog
        dialog.open(parent, None, done)

    def _import_rdp(self, src_path):
        """Copy a chosen .rdp into the local account's store and show its tile."""
        session = self._local_session()
        try:
            res = local_resource(src_path)
            store = os.path.join(session.directory, "files")
            os.makedirs(store, mode=0o700, exist_ok=True)
            stored = os.path.join(store, res["id"] + ".rdp")
            shutil.copyfile(src_path, stored)
        except OSError as error:
            self._error(tr("Could not open that .rdp file: {error}").format(error=error))
            return
        res["_local_path"] = stored
        # Default new tiles to a smart-card-enabled connection — this feature is
        # for CAC/PIV users; they can edit Advanced flags per tile afterwards.
        settings = session.resource_settings.setdefault(res["id"], {})
        settings.setdefault("extra_args", "/smartcard")
        session.save_preferences()
        resources = [r for r in session.resources if r.get("id") != res["id"]]
        resources.append(res)
        session.save_resources(resources)
        self._select_account(LOCAL_PROFILE_ID)
        self._set_status(tr("Opened {resource} — double-click to connect").format(
            resource=res["title"]), session)

    def _prepare_local_rdp(self, res):
        """Copy a stored local .rdp to a fresh working file so the connect-time
        display rewrites never mutate the user's saved original. Returns the
        path, or raises OSError if the stored file is missing."""
        src = res.get("_local_path")
        if not src or not os.path.exists(src):
            raise OSError(tr("The .rdp file could not be found. Open it again."))
        work = os.path.join(os.path.dirname(config.CACHE), "local-rdp", "work")
        os.makedirs(work, mode=0o700, exist_ok=True)
        dst = os.path.join(work, res["id"] + ".rdp")
        shutil.copyfile(src, dst)
        return dst

    def _resolve_aad_browser(self, master, url, res, session, attempt=None, offer_in_app=False):
        """Service a connection-time 'Browse to:' prompt through the SYSTEM
        browser instead of the embedded WebView, so a smart card / CAC
        certificate can be presented (the WebView can't — issue #17). FreeRDP
        stays blocked on stdin while the user signs in and pastes the final
        address back, so there is no race against FreeRDP's own redirect
        handler. The pasted redirect URL is written straight to the pty.

        When scheme-capture is on, FreeRDP's redirect is the ms-appx-web broker
        URL, so the browser hands the code back to the app (do_open ->
        _deliver_scheme_conn -> this prompt's ``finish``) with no paste; the
        paste field stays visible as a fallback. ``offer_in_app`` (set when the
        user came here from the in-app prompt) offers the way back to it."""
        if session.removed or (attempt is not None and not attempt.active()):
            if attempt is not None and not attempt.cancelled.is_set():
                attempt.cancel()
            else:
                try:
                    os.write(master, b"\n")
                except OSError:
                    pass
            return False
        scheme = self._scheme_connect(session)
        waiter_box = [None]
        done = [False]

        def launch(target):
            try:
                Gio.AppInfo.launch_default_for_uri(target, None)
            except Exception:
                pass

        parent = self.win if self.win and self.win.get_visible() else None
        win = create(Gtk.Window,
                     title=tr("Sign in — {resource}").format(resource=res['title']),
                     transient_for=parent, modal=parent is not None)
        win.set_default_size(480, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for margin in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{margin}")(22)
        heading = create(Gtk.Label, label=tr("Finish signing in"), xalign=0)
        heading.add_css_class("hero-title")
        intro = create(Gtk.Label, xalign=0, wrap=True,
            label=tr("Your browser will open so you can sign in with your smart "
                     "card or certificate. When it asks to open the app, allow "
                     "it and the connection continues on its own — or paste the "
                     "final address below. Take your time, the connection waits.")
                  if scheme else
                  tr("Your browser will open so you can sign in with your smart "
                     "card or certificate. Then copy the final address from the "
                     "browser's address bar and paste it below. Take your time — "
                     "the connection waits for you."))
        intro.add_css_class("hero-sub")
        open_btn = create(Gtk.Button, label=tr("Open browser"))
        open_btn.add_css_class("suggested-action")
        copy_btn = create(Gtk.Button, label=tr("Copy sign-in link"))
        copy_btn.add_css_class("flat")
        btnrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btnrow.set_halign(Gtk.Align.START)
        btnrow.append(open_btn); btnrow.append(copy_btn)
        paste_entry = Gtk.Entry(); paste_entry.set_hexpand(True)
        bind(paste_entry, 'placeholder_text', tr("Paste the sign-in address"))
        paste_go = create(Gtk.Button, label=tr("Sign in"))
        paste_go.add_css_class("suggested-action"); paste_go.set_halign(Gtk.Align.START)
        # The in-app WebView only captures FreeRDP's native-client redirect, so
        # there is no way back when the prompt uses the broker (scheme) redirect.
        actions = sign_in_actions(win, lambda: finish(),
                                  (lambda: finish(in_app=True))
                                  if offer_in_app and not scheme else None)
        for widget in (heading, intro, btnrow, paste_entry, paste_go, actions):
            box.append(widget)
        win.set_child(box)

        def finish(redirect_uri=None, in_app=False):
            if done[0]:
                return
            done[0] = True
            if waiter_box[0] is not None:
                try:
                    self._conn_scheme_waiters.remove(waiter_box[0])
                except ValueError:
                    pass
            if attempt is not None and attempt.close_auth is close_auth:
                attempt.close_auth = None
            if in_app:
                # FreeRDP stays blocked on its prompt; the in-app sign-in answers it.
                GLib.idle_add(self._resolve_aad, master, url, res, session, attempt)
            elif redirect_uri and not session.removed and (attempt is None or attempt.active()):
                try:
                    os.write(master, (redirect_uri + "\n").encode())
                except OSError:
                    pass
            elif attempt is not None and not attempt.cancelled.is_set():
                child = self._child_for(res["id"], session)
                if child:
                    self._cancel_connection(child, res, session, attempt)
                else:
                    attempt.cancel()
            win.destroy()

        def close_auth(*_):
            finish()
            return True

        def do_copy(*_):
            try:
                copy_btn.get_clipboard().set(url)
                copy_btn.set_label(tr("Copied"))
            except Exception:
                pass

        def do_paste(*_):
            frag = paste_entry.get_text().strip()
            if frag:
                finish(frag)

        open_btn.connect("clicked", lambda *_: launch(config.with_login_hint(url, session.client.upn)))
        copy_btn.connect("clicked", do_copy)
        paste_go.connect("clicked", do_paste)
        paste_entry.connect("activate", do_paste)
        win.connect("close-request", close_auth)
        if attempt is not None:
            attempt.close_auth = close_auth
        if scheme:
            # Let do_open deliver the caught ms-appx-web redirect to this prompt.
            states = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("state", [])
            if len(states) != 1 or not states[0]:
                self._error(tr("Could not verify the connection sign-in state."))
                finish()
                return False
            waiter_box[0] = {"finish": finish, "attempt": attempt, "state": states[0]}
            self._conn_scheme_waiters.append(waiter_box[0])
        launch(config.with_login_hint(url, session.client.upn))  # open immediately, like the manual flow
        win.present()
        return False

    # ---- tray icon ---------------------------------------------------------
    def _start_tray(self):
        if os.environ.get("AVD_TRAY", "").strip().lower() in ("0", "off", "false", "no"):
            return
        try:
            conn = self.get_dbus_connection() or Gio.bus_get_sync(Gio.BusType.SESSION)
        except GLib.Error:
            return
        # Monochrome "-symbolic" icon: tray hosts recolor it to match the bar.
        self._tray = TrayIcon(conn, APP_ID, APP_NAME, APP_ID + "-symbolic",
                              self._tray_icon_dir(),
                              on_activate=self._show_window,
                              on_action=self._on_tray_action,
                              on_availability=self._on_tray_availability)
        self._tray.start()
        self._tray_refresh()

    def _tray_icon_dir(self):
        """A directory holding the tray icon file itself, so the tray host
        doesn't depend on its icon theme. The installed tray/ dir has the icon
        both as <name>.svg and as <name>, since Quickshell loads
        <dir>/<IconName> without adding an extension."""
        # Installed: tray/ sits next to this script.
        here = os.path.dirname(os.path.abspath(__file__))
        tray = os.path.join(here, "tray")
        if os.path.exists(os.path.join(tray, APP_ID + "-symbolic")):
            return tray
        # Unpackaged run: the icon sits at the top of the source tree.
        root = os.path.dirname(here)
        return root if os.path.exists(os.path.join(root, APP_ID + "-symbolic.svg")) else ""

    def _tray_refresh(self):
        if not self._tray:
            return False
        accounts, states = [], {}
        for session in self.accounts.values():
            resources = list(session.resources)
            known = {r['id'] for r in resources}
            # Keep established sessions accessible if a feed stops listing them.
            for rid, child in session.tiles.items():
                running = bool(child._proc and child._proc.poll() is None)
                state = self._sessions.get((session.id, rid)) or ("connecting" if child._launching else "idle")
                states[(session.id, rid)] = {'running': running, 'state': state}
                if running and rid not in known:
                    resources.append(child._res)
            accounts.append({'id': session.id, 'upn': session.display_name,
                             'resources': resources, 'removed': session.removed})
        self._tray.set_menu(tray_menu.build_accounts_menu(accounts, states))
        n = sum(1 for state in states.values() if state['running'])
        self._tray.set_tooltip(ngettext("{count} session connected", "{count} sessions connected", n).format(count=n) if n else "")
        return False

    def _show_window(self):
        if self.win:
            self.win.set_visible(True)
            self.win.present()

    def _set_tray_option(self, key, on):
        """Persist a tray checkbox (close-to-tray / start-in-tray)."""
        if on:
            self._settings[key] = True
        else:
            self._settings.pop(key, None)
        storage.save_settings(self._settings)

    def _teams_plugin_available(self):
        """True when the FreeRDP in use carries the freerdp-teams-redir add-in
        (libteams-redir-client.so in its lib/freerdp3). The add-in is not
        bundled: it exists only when AVD_SDL_FREERDP points at a build that has
        it, so the Teams-optimization setting stays inert on the stock client."""
        try:
            prefix = os.path.dirname(os.path.dirname(config.SDL))
            return os.path.isfile(os.path.join(
                prefix, "lib", "freerdp3", "libteams-redir-client.so"))
        except OSError:
            return False

    def _on_close_request(self, win):
        # Opted in and a tray can bring it back: closing only hides the window
        # (sessions and the token refresh keep running). Otherwise close quits.
        if self._settings.get("_close_to_tray") and self._tray and self._tray.available:
            win.set_visible(False)
            return True
        return False

    def _start_hidden_timeout(self):
        # Started in the tray, but no tray host took the icon: show the window.
        self._start_hidden_source = 0
        self._show_window()
        return False

    def _on_tray_availability(self, available):
        for cb in self._tray_toggles:
            cb.set_sensitive(available)
            bind(cb, 'tooltip_text', None if available else tr("Needs a system tray on your desktop"))
        if available and self._start_hidden_source:
            # Started in the tray and the icon is up: stay hidden.
            GLib.source_remove(self._start_hidden_source)
            self._start_hidden_source = 0
        # The tray host went away (e.g. the bar restarted or was closed): never
        # leave the window hidden with no way back.
        if not available and self.win and not self.win.get_visible():
            self._show_window()

    def _on_tray_action(self, action):
        kind = action[0]
        session = self.accounts.get(action[1]) if len(action) > 2 else self.current
        rid = action[2] if len(action) > 2 else None
        if kind == "show":
            self._show_window()
        elif kind == "quit":
            self.quit()
        elif kind == "refresh":
            self._reload_feed()
        elif kind in ("connect", "connect-windowed"):
            if session is None or session.removed:
                return
            ch = self._child_for(rid, session)
            if ch is None:
                resource = next((r for r in session.resources if r['id'] == rid), None)
                if resource is not None:
                    ch = self._make_tile(resource, session)
                    session.tiles[rid] = ch
            if ch:
                self._on_tile_activated(self.grid, ch, windowed=kind == "connect-windowed")
        elif kind in ("focus", "disconnect", "reconnect"):
            ch = self._child_for(rid, session)
            if ch is None:
                return
            if kind == "focus":
                self._focus_session(ch)
            else:
                self._disconnect_session(ch, reconnect=kind == "reconnect")

    @staticmethod
    def _session_running(child):
        return bool(child._proc and child._proc.poll() is None)

    def _focus_session(self, child):
        if not self._session_running(child):
            return
        if not x11focus.focus_pid(child._proc.pid):
            self._set_status(tr("Couldn't bring {resource} to the front").format(
                resource=child._res["title"]), child._account)

    def _disconnect_session(self, child, reconnect=False):
        """End a running session; with ``reconnect`` start it again once it exits."""
        proc = child._proc
        if not self._session_running(child):
            return
        child._reconnect = reconnect
        try:
            os.killpg(proc.pid, signal.SIGTERM)   # own session (start_new_session)
        except (ProcessLookupError, PermissionError):
            pass
        title = child._res["title"]
        self._set_status((tr("Reconnecting to {resource}…") if reconnect
                          else tr("Disconnecting {resource}…")).format(resource=title),
                         child._account)
        def force():
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            return GLib.SOURCE_REMOVE
        GLib.timeout_add_seconds(10, force)

    def _resource_actions(self, child):
        """(label, callback) pairs for a tile's actions menu; None is a separator."""
        session = child._account
        res = child._res
        if self._session_running(child):
            actions = [(tr("Show window"), lambda: self._focus_session(child))]
            if not session.removed:
                actions.append((tr("Reconnect"), lambda: self._disconnect_session(child, reconnect=True)))
            actions.append((tr("Disconnect"), lambda: self._disconnect_session(child)))
        elif child._launching:
            actions = [(tr("Cancel connection"), lambda: self._cancel_connection(
                child, res, session, getattr(child, "_attempt", None)))]
        else:
            actions = [(tr("Connect"), lambda: self._on_tile_activated(self.grid, child)),
                       (tr("Connect in a window"),
                        lambda: self._on_tile_activated(self.grid, child, windowed=True))]
        return actions + [None, (tr("Settings…"), lambda: self._open_settings(res, session))]

    def _show_resource_menu(self, child, x=None, y=None):
        pop = Gtk.Popover()
        pop.add_css_class("avd-menu")
        pop.set_has_arrow(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        for entry in self._resource_actions(child):
            if entry is None:
                box.append(Gtk.Separator())
                continue
            label, callback = entry
            button = create(Gtk.Button, label=label)
            button.add_css_class("flat")
            button.get_child().set_xalign(0)
            button.connect("clicked", lambda _b, cb=callback: (pop.popdown(), cb()))
            box.append(button)
        pop.set_child(box)
        pop.set_parent(child)
        if x is not None:
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            pop.set_pointing_to(rect)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        child._menu = pop
        pop.popup()
        return pop

    # ---- per-resource settings -------------------------------------------
    def _eff(self, key, res, session=None):
        """Resolve a setting: per-resource value → global default → None(=auto)."""
        session = session or self.current
        rv = (session.resource_settings if session else {}).get(res["id"], {}).get(key, "auto")
        if rv and rv != "auto":
            return rv
        dv = self._settings.get("_default", {}).get(key, "auto")
        if dv and dv != "auto":
            return dv
        return None

    def _eff_ua(self):
        """Resolve the sign-in webview User-Agent; "auto" is Chrome on Linux,
        the identity Entra offers security keys to. It is a single account-wide
        value (the Default setting): every
        sign-in webview shares one persistent SSO cookie session, so they must
        all present the same identity or Entra refuses the silent token. A
        per-resource UA would only ever work when it equalled this, so there
        isn't one — this covers the account sign-in and every connection."""
        dv = self._settings.get("_default", {}).get("user_agent", "auto")
        return dv if dv and dv != "auto" else storage.UA_LINUX_CHROME

    def _apply_ua(self, wv):
        """Spoof the sign-in webview's User-Agent when configured (issue #28)."""
        ua = self._eff_ua()
        if ua:
            try:
                wv.get_settings().set_user_agent(ua)
            except Exception:
                pass

    def _eff_extra(self, res, session):
        parts = [self._settings.get("_default", {}).get("extra_args", ""),
                 session.resource_settings.get(res["id"], {}).get("extra_args", ""),
                 os.environ.get("AVD_EXTRA_ARGS", "")]
        return " ".join(p.strip() for p in parts if p and p.strip()).strip()

    def _eff_teams(self, res, session):
        """Teams call optimization for this resource: "video", "audio" or "off".
        Automatic means audio and video, and needs the add-in in the client."""
        if not self._teams_plugin_available():
            return "off"
        return self._eff("teams", res, session) or "video"

    def _eff_share_path(self, res, session=None):
        """Custom shared-folder path: per-resource → Default → \"\"."""
        session = session or self.current
        rv = (session.resource_settings if session else {}).get(res["id"], {}).get("share_path", "")
        if rv:
            return rv
        return self._settings.get("_default", {}).get("share_path", "") or ""

    SETTINGS_CONTROL_WIDTH = 260

    def _form_row(self, label, widget, width=None):
        """Label left, control right; a fixed control width keeps one straight column."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        lbl = create(Gtk.Label, label=label, xalign=0, wrap=True)
        lbl.set_hexpand(True)
        widget.set_halign(Gtk.Align.END)
        widget.set_valign(Gtk.Align.CENTER)
        if width:
            widget.set_hexpand(False)
            widget.set_size_request(width, -1)
            if isinstance(widget, Gtk.DropDown):
                # Ellipsize the closed button so long choices cannot widen the column;
                # the open list still shows every choice in full.
                widget.set_factory(self._facet_factory(1))
                widget.set_list_factory(self._facet_factory(0))
        row.append(lbl)
        row.append(widget)
        return row

    def _dialog_max_height(self, reserved=200):
        """Tallest scrollable body that keeps a dialog on the main window's monitor."""
        display = Gdk.Display.get_default()
        monitor = None
        surface = self.win.get_surface() if self.win is not None else None
        if display is not None and surface is not None:
            monitor = display.get_monitor_at_surface(surface)
        if monitor is None and display is not None and display.get_monitors().get_n_items():
            monitor = display.get_monitors().get_item(0)
        height = monitor.get_geometry().height if monitor is not None else 900
        return max(240, min(760, height - reserved))

    def _light_dismiss(self, dialog, cancel=None):
        """Esc, or a click on the main window, cancels a small app dialog.

        The dialog is deliberately not GTK-modal: a modal toplevel makes Hyprland
        (modal_parent_blocking) and GTK's grab swallow clicks on the parent, so
        "click outside to close" could never fire. Modality is emulated instead:
        while a dialog is open, presses on the main window cancel it and keys
        typed there are swallowed (Esc cancels, anything else refocuses it).
        """
        cancel = cancel or dialog.destroy
        dialog._cancel = cancel
        keys = Gtk.EventControllerKey()
        def on_key(_controller, keyval, _keycode, state):
            if keyval == Gdk.KEY_Escape and not state & Gtk.accelerator_get_default_mod_mask():
                cancel()
                return True
            return False
        keys.connect("key-pressed", on_key)
        dialog.add_controller(keys)
        self._light_dialogs = [d for d in self._open_light_dialogs() if d is not dialog] + [dialog]
        if self.win is not None and not getattr(self.win, "_dialog_guard", False):
            self.win._dialog_guard = True
            press = Gtk.GestureClick(button=0)
            press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            press.connect("pressed", self._on_parent_pressed_with_dialog)
            self.win.add_controller(press)
            parent_keys = Gtk.EventControllerKey()
            parent_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            parent_keys.connect("key-pressed", self._on_parent_key_with_dialog)
            self.win.add_controller(parent_keys)

    def _open_light_dialogs(self):
        # GTK4 emits "destroy" only at finalization, so prune closed dialogs here.
        alive = Gtk.Window.list_toplevels()
        self._light_dialogs = [d for d in getattr(self, "_light_dialogs", []) if d in alive]
        return self._light_dialogs

    def _on_parent_pressed_with_dialog(self, gesture, *_args):
        dialogs = list(self._open_light_dialogs())
        if not dialogs:
            return
        # Claim the press so the click only closes the dialog (no tile launch).
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        for dialog in reversed(dialogs):
            dialog._cancel()

    def _on_parent_key_with_dialog(self, _controller, keyval, _keycode, _state):
        dialogs = self._open_light_dialogs()
        if not dialogs:
            return False
        if keyval == Gdk.KEY_Escape:
            dialogs[-1]._cancel()
        else:
            dialogs[-1].present()
        return True

    def _open_settings(self, res, session=None):
        session = session or self.current
        settings = session.resource_settings if res and session else self._settings
        key = res["id"] if res else "_default"
        cur = settings.get(key, {})
        title = tr("Settings — {resource}").format(resource=res['title']) if res else tr("Default connection settings")
        auto_note = (tr("Automatic uses your Default settings, then the display.")
                     if res else tr("Automatic matches your display and monitors."))

        dlg = create(Gtk.Window, title=title, transient_for=self.win)
        dlg.add_css_class("avd-settings")
        dlg.set_default_size(580, -1)

        # Header: which resource is being configured, like the main window's brand block.
        header = Gtk.HeaderBar()
        header.add_css_class("avd-header")
        header.set_title_widget(Gtk.Box())
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, margin_start=14)
        if res:
            head_title = Gtk.Label(label=res["title"], xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                   max_width_chars=40)
            kind = resource_groups.kind(res)
            kind_label = tr({resource_groups.PC: "Cloud PC", resource_groups.DESKTOP: "Virtual desktop",
                             resource_groups.APP: "App"}[kind])
            workspace = resource_groups.workspace(res)
            head_sub = create(Gtk.Label, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=48,
                              label=tr("{kind} · {workspace}").format(kind=kind_label, workspace=workspace)
                              if workspace else kind_label)
        else:
            head_title = create(Gtk.Label, label=tr("Default connection settings"), xalign=0)
            head_sub = create(Gtk.Label, label=tr("Used by every resource without its own settings"),
                              xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=48)
        head_title.add_css_class("brand-title")
        head_sub.add_css_class("brand-sub")
        heading.append(head_title)
        heading.append(head_sub)
        header.pack_start(heading)
        dlg.set_titlebar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        box.set_margin_top(18); box.set_margin_bottom(22)
        box.set_margin_start(22); box.set_margin_end(22)
        width = self.SETTINGS_CONTROL_WIDTH
        sections = []

        def section(caption):
            """A captioned group, styled like the main window's section captions."""
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            label = create(Gtk.Label, label=caption, xalign=0)
            label.add_css_class("section-h")
            body.append(label)
            box.append(body)
            sections.append(body)
            return body

        def field(parent, label, widget, note=None):
            """A form row plus its optional help text, kept together."""
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            row = self._form_row(label, widget, width)
            group.append(row)
            if note is not None:
                note.add_css_class("tile-sub")
                group.append(note)
            parent.append(group)
            return row

        def help_text(text):
            return create(Gtk.Label, label=text, xalign=0, wrap=True)

        general = section(tr("GENERAL")) if res is None else None
        display_section = section(tr("DISPLAY"))
        connection = section(tr("CONNECTION"))
        placement = section(tr("PLACEMENT"))
        signin_section = section(tr("SIGN-IN")) if res is None else None
        advanced = section(tr("ADVANCED"))

        # Language is an app-wide preference, shown only in the Default settings
        # dialog (res is None). It applies immediately and is persisted on change
        # rather than waiting for Save, matching how it behaved in the menu.
        if res is None:
            language_keys = [key for key, label in LANGUAGES]
            language_dd = create(Gtk.DropDown, model=strings([tr(label) for key, label in LANGUAGES]))
            current_language = self._settings.get("_language", "system")
            language_dd.set_selected(language_keys.index(current_language)
                                     if current_language in language_keys else 0)
            field(general, tr("Language"), language_dd, help_text(tr("Language changes apply immediately.")))

            def change_language(dropdown, *_args):
                if getattr(self, "_changing_language", False):
                    return
                self._settings["_language"] = language_keys[dropdown.get_selected()]
                storage.save_settings(self._settings)
                self._set_language(self._settings["_language"])
            language_dd.connect("notify::selected", change_language)

        scale_dd = create(Gtk.DropDown, model=strings([tr(label) for label in storage.SCALE_LABELS]))
        sv = cur.get("scale", "auto")
        scale_dd.set_selected(storage.SCALE_VALUES.index(sv) if sv in storage.SCALE_VALUES else 0)
        field(display_section, tr("Display scale"), scale_dd)

        mm_dd = create(Gtk.DropDown, model=strings([tr(label) for label in storage.MULTIMON_LABELS]))
        mv = cur.get("multimon", "auto")
        mm_dd.set_selected(storage.MULTIMON_VALUES.index(mv) if mv in storage.MULTIMON_VALUES else 0)
        field(display_section, tr("Monitors"), mm_dd)

        cert_dd = create(Gtk.DropDown, model=strings([tr(label) for label in storage.CERT_LABELS]))
        cv = cur.get("cert", "auto")
        cert_dd.set_selected(storage.CERT_VALUES.index(cv) if cv in storage.CERT_VALUES else 0)
        field(connection, tr("Server certificate"), cert_dd)

        sec_dd = create(Gtk.DropDown, model=strings([tr(label) for label in storage.SEC_LABELS]))
        sec_value = cur.get("sec", "auto")
        sec_dd.set_selected(storage.SEC_VALUES.index(sec_value) if sec_value in storage.SEC_VALUES else 0)
        field(connection, tr("Session authentication"), sec_dd, help_text(
            tr("Automatic respects the feed and remembers successful connections. If your feed does not advertise SSO, select Entra to test it. This does not bypass MFA or your organization's sign-in policy.")))

        # RemoteApp (published single app) tiles can optionally open as the host's
        # full desktop — the fallback for when the app's own window doesn't appear
        # (the bundled client's RAIL rendering is limited). Only meaningful for a
        # RemoteApp resource, so the control is shown only there; desktops never
        # see it. It is a per-resource choice, off by default.
        remoteapp_dd = None
        if res is not None and res.get("type") == "RemoteApp":
            remoteapp_dd = create(Gtk.DropDown,
                model=strings([tr(label) for label in storage.REMOTEAPP_LABELS]))
            rav = cur.get("remoteapp", "auto")
            remoteapp_dd.set_selected(storage.REMOTEAPP_VALUES.index(rav)
                                      if rav in storage.REMOTEAPP_VALUES else 0)
            field(connection, tr("Open RemoteApp as"), remoteapp_dd, help_text(
                tr("Full desktop opens the whole remote PC so you can launch the app yourself — use this if the app window never appears.")))


        teams_dd = None
        if self._teams_plugin_available():
            teams_dd = create(Gtk.DropDown,
                model=strings([tr(label) for label in storage.TEAMS_LABELS]))
            tv = cur.get("teams", "auto")
            teams_dd.set_selected(storage.TEAMS_VALUES.index(tv)
                                  if tv in storage.TEAMS_VALUES else 0)
            field(connection, tr("Teams calls"), teams_dd, help_text(
                tr("Runs Teams call media on this computer. Restart Teams in the session once after the first connection. Video opens in its own window and screen sharing is not supported.")))

        hyprland = isinstance(workspace_backend(), HyprlandWorkspaceBackend)

        # Hyprland: a fixed workspace per Cloud PC, and where this window opens.
        hypr_dd = None
        hypr_labels = [tr("Current workspace")] + [str(n) for n in HYPR_WORKSPACES]
        if hyprland and res is not None and res.get("type") == "Desktop":
            hypr_dd = create(Gtk.DropDown, model=strings(hypr_labels))
            hv_ws = str(cur.get("hypr_workspace", "auto"))
            hypr_dd.set_selected(HYPR_WORKSPACES.index(int(hv_ws)) + 1
                                 if hv_ws.isdigit() and int(hv_ws) in HYPR_WORKSPACES else 0)
            field(placement, tr("Open on workspace"), hypr_dd)
        if hyprland and res is None:
            window_dd = create(Gtk.DropDown, model=strings([tr("Any workspace")] + [str(n) for n in HYPR_WORKSPACES]))
            window_ws = str(self._settings.get("_window_workspace", ""))
            window_dd.set_selected(HYPR_WORKSPACES.index(int(window_ws)) + 1
                                   if window_ws.isdigit() and int(window_ws) in HYPR_WORKSPACES else 0)
            def change_window_workspace(dropdown, *_):
                index = dropdown.get_selected()
                if index:
                    self._settings["_window_workspace"] = str(HYPR_WORKSPACES[index - 1])
                else:
                    self._settings.pop("_window_workspace", None)
                storage.save_settings(self._settings)
                self._place_main_window()
            window_dd.connect("notify::selected", change_window_workspace)
            field(placement, tr("Open this window on workspace"), window_dd)
            follow_check = create(Gtk.CheckButton, label=tr("Switch to a Cloud PC's workspace when it connects"))
            follow_check.set_active(bool(self._settings.get("_follow_workspace", True)))
            follow_check.connect("toggled", lambda c, *_: (
                self._settings.__setitem__("_follow_workspace", c.get_active()),
                storage.save_settings(self._settings)))
            follow = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            follow.append(follow_check)
            hypr_note = help_text(tr("Pick a workspace for each Cloud PC in its own settings (right-click its tile, then Settings…)."))
            hypr_note.add_css_class("tile-sub")
            follow.append(hypr_note)
            placement.append(follow)

        # Shared local folder redirected into the remote session (Desktop and
        # RemoteApp). Off by default; Home uses FreeRDP +home-drive, Custom a
        # /drive:share,<path>. Host-pool policy can still block redirection.
        share_values = storage.SHARE_VALUES
        share_labels = storage.SHARE_LABELS
        share_dd = create(Gtk.DropDown, model=strings([tr(label) for label in share_labels]))
        share_value = cur.get("share", "auto")
        share_dd.set_selected(share_values.index(share_value)
                              if share_value in share_values else 0)
        share_path_entry = Gtk.Entry()
        bind(share_path_entry, 'text', cur.get("share_path", ""))
        bind(share_path_entry, 'placeholder_text', tr("path to a local folder"))
        share_path_entry.set_hexpand(True)
        share_path_entry.set_width_chars(1)
        share_browse = create(Gtk.Button, label=tr("Choose…"))
        share_path_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        share_path_row.append(share_path_entry)
        share_path_row.append(share_browse)
        field(connection, tr("Shared folder"), share_dd, help_text(
            tr("Shows up in the remote session as a network drive under This PC (\\\\tsclient). Your organization's host pool can still block folder redirection.")))
        share_path_form = field(connection, tr("Folder path"), share_path_row)
        custom_index = share_values.index("custom") if "custom" in share_values else -1

        def update_share_path(*_):
            share_path_form.get_parent().set_visible(share_dd.get_selected() == custom_index)
        share_dd.connect("notify::selected", update_share_path)
        update_share_path()

        def choose_share_folder(*_):
            dialog = Gtk.FileDialog(title=tr("Choose folder to share"))
            parent = self.win if self.win and self.win.get_visible() else None

            def on_selected(dlg, result):
                try:
                    folder = dlg.select_folder_finish(result)
                except GLib.Error:
                    return                       # cancelled or dismissed
                finally:
                    self._share_dialog = None    # release the kept reference
                if folder:
                    bind(share_path_entry, 'text', folder.get_path() or "")
            # Keep a reference while the chooser is open, or the Gtk.FileDialog is
            # garbage-collected before its async callback fires (issue #17).
            self._share_dialog = dialog
            dialog.select_folder(parent, None, on_selected)
        share_browse.connect("clicked", choose_share_folder)

        # Sign-in browser identity (User-Agent). Lets a tenant that blocks Linux
        # at sign-in via Conditional Access be satisfied by posing as Edge/Chrome
        # on Windows (issue #28). Stored value is the literal UA string; "Custom…"
        # reveals an entry. It is account-wide (Default only, not per resource):
        # every sign-in webview shares one SSO session, so a per-resource identity
        # that differed from the account's would just fail to connect.
        ua_dd = None
        if res is None:
            uav = cur.get("user_agent", "auto")
            ua_dd = create(Gtk.DropDown, model=strings([tr(label) for label in storage.UA_LABELS]))
            ua_entry = Gtk.Entry(activates_default=True)
            bind(ua_entry, 'placeholder_text', tr("paste a User-Agent string"))
            if uav in storage.UA_PRESETS:
                ua_dd.set_selected(storage.UA_PRESETS.index(uav))
            else:
                ua_dd.set_selected(len(storage.UA_LABELS) - 1)   # "Custom…"
                bind(ua_entry, 'text', uav)
            field(signin_section, tr("Sign-in browser"), ua_dd)
            ua_entry_row = field(signin_section, tr("Custom identity"), ua_entry).get_parent()
            ua_entry_row.set_visible(ua_dd.get_selected() == len(storage.UA_LABELS) - 1)
            ua_dd.connect(
                "notify::selected",
                lambda dd, *_: ua_entry_row.set_visible(
                    dd.get_selected() == len(storage.UA_LABELS) - 1))

            # Browser (device-code) sign-in. App-wide, applied immediately like
            # Language — needed for certificate-based auth the WebView can't do
            # (issue #17). The embedded sign-in window has no PKCS#11 bridge, so
            # a smart card / CAC / PIV can't be presented there.
            dc_check = create(Gtk.CheckButton, label=tr("Use the system browser to sign in"))
            dc_check.set_active(bool(self._settings.get("_browser_signin", False)))
            dc_check.connect("toggled", lambda c, *_: (
                self._settings.__setitem__("_browser_signin", c.get_active()),
                storage.save_settings(self._settings)))
            browser_signin = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            browser_signin.append(dc_check)
            dc_note = help_text(tr("Required for certificate-based (CBA) sign-in — the built-in "
                                   "sign-in window can't present a smart card. The app shows a code "
                                   "to enter in your browser."))
            dc_note.add_css_class("tile-sub")
            browser_signin.append(dc_note)
            signin_section.append(browser_signin)

        if res is not None and res.get("type") == "Desktop" and session is not None \
                and not session.local:
            placement.append(self._launcher_controls(res, session, cur))
        placement.set_visible(placement.get_first_child().get_next_sibling() is not None)

        # Enter in a text field saves (the default action) rather than dropping the edit.
        extra_entry = Gtk.Entry(activates_default=True)
        bind(extra_entry, 'text', cur.get("extra_args", ""))
        bind(extra_entry, 'placeholder_text', tr("advanced: e.g. /gfx /network:auto"))
        field(advanced, tr("Advanced flags"), extra_entry)

        # Hide captions whose rows are all unavailable here (e.g. no workspace support).
        for body in sections:
            child = body.get_first_child().get_next_sibling()
            shown = False
            while child is not None:
                shown = shown or child.get_visible()
                child = child.get_next_sibling()
            body.set_visible(shown)

        note = create(Gtk.Label, label=tr("{automatic} Applies to your next connection.").format(automatic=auto_note),
                         xalign=0, wrap=True, hexpand=True, valign=Gtk.Align.CENTER)
        note.add_css_class("tile-sub")

        btnbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btnbox.add_css_class("settings-foot")
        btnbox.append(note)
        cancel = create(Gtk.Button, label=tr("Cancel"))
        cancel.connect("clicked", lambda *_: dlg.destroy())
        save = create(Gtk.Button, label=tr("Save"))
        save.add_css_class("suggested-action")

        def do_save(*_):
            entry = {
                "scale": storage.SCALE_VALUES[scale_dd.get_selected()],
                "multimon": storage.MULTIMON_VALUES[mm_dd.get_selected()],
                "cert": storage.CERT_VALUES[cert_dd.get_selected()],
                "sec": storage.SEC_VALUES[sec_dd.get_selected()],
                "extra_args": storage.drop_builtin_flags(extra_entry.get_text().strip()),
                "share": share_values[share_dd.get_selected()],
                "share_path": share_path_entry.get_text().strip(),
            }
            if ua_dd is not None:   # Default settings only; account-wide (issue #28)
                ua_sel = ua_dd.get_selected()
                if ua_sel == len(storage.UA_LABELS) - 1:        # "Custom…"
                    entry["user_agent"] = ua_entry.get_text().strip() or "auto"
                else:
                    entry["user_agent"] = storage.UA_PRESETS[ua_sel]
            if teams_dd is not None:
                entry["teams"] = storage.TEAMS_VALUES[teams_dd.get_selected()]
            elif "teams" in cur:
                entry["teams"] = cur["teams"]
            if remoteapp_dd is not None:   # RemoteApp resources only
                entry["remoteapp"] = storage.REMOTEAPP_VALUES[remoteapp_dd.get_selected()]
            if hypr_dd is not None:
                index = hypr_dd.get_selected()
                entry["hypr_workspace"] = str(HYPR_WORKSPACES[index - 1]) if index else "auto"
            elif "hypr_workspace" in cur:
                entry["hypr_workspace"] = cur["hypr_workspace"]
            live = settings.get(key, {})   # the launcher controls save as they go
            for kept in ("launcher_icon", "launcher_id"):
                if kept in live:
                    entry[kept] = live[kept]
            # drop an all-default entry so the file stays tidy
            if entry["scale"] == "auto" and entry["multimon"] == "auto" \
                    and entry["cert"] == "auto" and entry["sec"] == "auto" \
                    and entry.get("user_agent", "auto") == "auto" \
                    and entry.get("remoteapp", "auto") == "auto" \
                    and entry.get("teams", "auto") == "auto" \
                    and entry.get("hypr_workspace", "auto") == "auto" \
                    and not any(k in entry for k in ("launcher_icon", "launcher_id")) \
                    and entry["share"] == "auto" \
                    and not entry["share_path"] \
                    and not entry["extra_args"]:
                settings.pop(key, None)
            else:
                settings[key] = entry
            if res:
                session.save_preferences()
            else:
                storage.save_settings(self._settings)
            dlg.destroy()
            self._set_status(tr("Settings saved — applies to your next connection"))
        save.connect("clicked", do_save)
        cancel.set_valign(Gtk.Align.CENTER)
        save.set_valign(Gtk.Align.CENTER)
        btnbox.append(cancel)
        btnbox.append(save)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                     propagate_natural_height=True,
                                     max_content_height=self._dialog_max_height())
        scroller.set_child(box)
        scroller.set_vexpand(True)
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        layout.append(scroller)
        layout.append(btnbox)
        dlg.set_child(layout)
        dlg.set_default_widget(save)
        # Start on the first setting, not a footer button, so Enter saves.
        dlg.set_focus(language_dd if res is None else scale_dd)
        self._light_dismiss(dlg)
        dlg.present()

    # ---- app launcher entries ---------------------------------------------
    def _launcher_icon_path(self, session, res, cur=None):
        cur = cur if cur is not None else session.resource_settings.get(res["id"], {})
        name = cur.get("launcher_icon")
        if not name or os.path.basename(name) != name:
            return None
        path = os.path.join(session.directory, "launcher-icons", name)
        return path if os.path.isfile(path) else None

    def _launcher_png(self, session, res, cur=None):
        picture = self._launcher_icon_path(session, res, cur) \
            or storage.feed_picture(res["id"], session.directory)
        try:
            palette = omarchy_theme.load() or {}
        except Exception:
            palette = {}
        try:
            return launchers.render_icon(res["title"], picture, palette)
        except Exception:
            return launchers.render_icon(res["title"], None, palette)

    def _launcher_controls(self, res, session, cur):
        """Add/remove an app launcher entry and pick its picture."""
        portal = launchers.PortalLauncher(APP_ID)
        file_id = launchers.desktop_file_id(APP_ID, session.id, res["id"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        preview = Gtk.Image(pixel_size=48)
        status = create(Gtk.Label, label="", xalign=0, wrap=True)
        status.add_css_class("tile-sub")
        toggle = create(Gtk.Button, label="")
        choose = create(Gtk.Button, label=tr("Choose picture…"))
        reset = create(Gtk.Button, label=tr("Default picture"))
        row = Gtk.Box(spacing=8)
        row.append(preview)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                         valign=Gtk.Align.CENTER)
        title = create(Gtk.Label, label=tr("App launcher"), xalign=0)
        labels.append(title); labels.append(status)
        row.append(labels)
        row.append(toggle)
        box.append(row)
        pictures = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        pictures.append(reset); pictures.append(choose)
        box.append(pictures)
        available = portal.available()

        def settings_entry():
            return session.resource_settings.setdefault(res["id"], dict(cur))

        def refresh():
            png = self._launcher_png(session, res, session.resource_settings.get(res["id"], cur))
            preview.set_from_paintable(Gdk.Texture.new_from_bytes(GLib.Bytes.new(png)))
            installed = available and portal.installed(file_id)
            bind(toggle, 'label', tr("Remove") if installed else tr("Add…"))
            toggle.set_sensitive(available)
            reset.set_sensitive(self._launcher_icon_path(session, res) is not None)
            bind(status, 'text', tr("Shown in the app launcher") if installed
                 else tr("Open this Cloud PC from the app launcher") if available
                 else tr("The desktop portal does not offer app launcher entries"))
            return installed

        def install():
            png = self._launcher_png(session, res)
            def done(ok, message):
                if ok:
                    entry = settings_entry()
                    entry["launcher_id"] = file_id
                    session.save_preferences()
                    self._set_status(tr("Added {resource} to the app launcher").format(resource=message))
                elif message:
                    self._error(tr("Could not add the launcher entry: {error}").format(error=message))
                refresh()
            portal.install(res["title"], png, file_id,
                           lambda name: launchers.desktop_entry(APP_ID, name, session.id, res["id"]), done)

        def on_toggle(*_):
            if portal.installed(file_id):
                portal.uninstall(file_id)
                session.resource_settings.get(res["id"], {}).pop("launcher_id", None)
                session.save_preferences()
                refresh()
            else:
                install()

        def set_picture(path):
            directory = os.path.join(session.directory, "launcher-icons")
            entry = settings_entry()
            old = self._launcher_icon_path(session, res)
            if path is None:
                entry.pop("launcher_icon", None)
            else:
                os.makedirs(directory, mode=0o700, exist_ok=True)
                name = hashlib.sha256(res["id"].encode()).hexdigest()[:16] + ".png"
                # Store the badge-free picture as PNG, at most launcher size.
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, launchers.ICON_SIZE,
                                                                 launchers.ICON_SIZE, True)
                pixbuf.savev(os.path.join(directory, name), "png", [], [])
                entry["launcher_icon"] = name
            if path is None and old:
                try:
                    os.remove(old)
                except OSError:
                    pass
            session.save_preferences()
            if refresh():
                install()   # the portal confirms the new picture

        def on_choose(*_):
            dialog = Gtk.FileDialog(title=tr("Choose a picture"))
            image_filter = Gtk.FileFilter()
            image_filter.set_name(tr("Images"))
            image_filter.add_pixbuf_formats()
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(image_filter)
            dialog.set_filters(filters)
            def chosen(dlg, result):
                try:
                    file = dlg.open_finish(result)
                except GLib.Error:
                    return
                try:
                    set_picture(file.get_path())
                except GLib.Error as error:
                    self._error(tr("Could not read that picture: {error}").format(error=error.message))
            dialog.open(box.get_root(), None, chosen)

        toggle.connect("clicked", on_toggle)
        choose.connect("clicked", on_choose)
        reset.connect("clicked", lambda *_: set_picture(None))
        refresh()
        return box

    def _error(self, msg):
        def show():
            self._show_window()
            d = Gtk.AlertDialog()
            d.set_modal(True)
            bind(d, 'message', msg)
            d.show(self.win)
        GLib.idle_add(show)


if __name__ == "__main__":
    app = AvdApp()
    # Pass argv so a launch carrying the ms-appx-web://…?code=… sign-in redirect
    # reaches GApplication (HANDLES_OPEN → do_open); a no-URI launch still just
    # activates. The wrapper forwards "$@", so sys.argv holds the URI here.
    sys.exit(app.run(sys.argv))
