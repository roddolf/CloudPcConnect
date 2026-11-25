"""Offscreen renders of the app UI on a private Broadway display.

Usage: tools/run-headless.sh tools/render.py <out-prefix> [scene ...]
tools/screenshots.sh renders the README screenshots with it.
The Omarchy loader honours AVD_OMARCHY / AVD_OMARCHY_THEME as in the app.
"""
import os
import sys
import tempfile

os.environ["AVD_DEMO"] = "1"
os.environ.setdefault("AVD_TRAY", "0")
scratch = tempfile.mkdtemp(dir=os.environ["XDG_CACHE_HOME"])

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gtk, Gsk, Graphene, GLib, Gdk  # noqa: E402

import avd_feed_gui as gui  # noqa: E402
from avd_feed_connect.gui.signin_panel import SignInPanel  # noqa: E402
from avd_feed_connect.gui.session import AccountSession  # noqa: E402
from avd_feed_connect.accounts import profile_dir  # noqa: E402

gui.config.CACHE = os.path.join(scratch, "token-cache.json")
gui.storage.SETTINGS_FILE = os.path.join(scratch, "settings.json")
gui.storage.save_settings({"_language": "en"})
gui.TokenStore._keyring_store = lambda *a: False
gui.TokenStore._keyring_load = lambda *a: None
gui.TokenStore._keyring_clear = lambda *a: None
SCALE = 1.25


def drain(n=40):
    ctx = GLib.MainContext.default()
    for _ in range(n):
        while ctx.pending():
            ctx.iteration(False)
        GLib.usleep(5000)


def bg(widget, color="#808080"):
    rgba = Gdk.RGBA()
    rgba.parse(color)
    return rgba


def render(node, w, h, path):
    renderer = Gsk.CairoRenderer()
    renderer.realize_for_display(Gdk.Display.get_default())
    tex = renderer.render_texture(node, Graphene.Rect().init(0, 0, w * SCALE, h * SCALE))
    renderer.unrealize()
    tex.save_to_png(path)
    print("wrote", path, w, h)


def painted(widget):
    snap = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(widget).snapshot(snap, max(1, widget.get_width()), max(1, widget.get_height()))
    return snap.to_node() is not None


def settle(widget):
    drain(10)
    for i in range(400):
        if widget.get_mapped() and widget.get_width() > 0 and painted(widget):
            return
        drain(2)
    print("warning: never painted", widget)


def shoot(widget, path, popovers=(), color="#808080"):
    settle(widget)
    for pop in popovers:
        settle(pop)
    drain(60)  # icons load asynchronously
    w, h = widget.get_width(), widget.get_height()
    snap = Gtk.Snapshot()
    snap.scale(SCALE, SCALE)
    snap.append_color(bg(widget, color), Graphene.Rect().init(0, 0, w, h))
    Gtk.WidgetPaintable.new(widget).snapshot(snap, w, h)
    wx, wy = widget.get_native().get_surface_transform()
    for pop in popovers:
        surface = pop.get_surface()
        px, py = pop.get_surface_transform()
        x = surface.get_position_x() + px - wx
        y = surface.get_position_y() + py - wy
        snap.save()
        snap.translate(Graphene.Point().init(x, y))
        Gtk.WidgetPaintable.new(pop).snapshot(snap, pop.get_width(), pop.get_height())
        snap.restore()
    render(snap.to_node(), w, h, path)


def make_app():
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    app = gui.AvdApp()
    app.register(None)
    app._background_refresh = lambda *a: None
    app._load_icons = lambda *a: None
    app._build_ui()
    app.win.set_default_size(880, 600)
    return app


def account(app, pid, upn, resources):
    profile = {"id": pid, "tid": "demo", "oid": pid, "upn": upn}
    s = AccountSession(profile, gui.AvdClient(), profile_dir(app.registry.root, pid))
    s.client.upn = upn
    app.accounts[s.id] = s
    return s


def workspaces(app):
    s = account(app, "a" * 32, "alex@contoso.com", None)
    account(app, "b" * 32, "alex@fabrikam.com", None)
    app.selected_id = s.id
    app._populate(gui.demo_resources(), s)
    app._set_tile_state("d1", "state-connecting", s)
    app._set_tile_state("d2", "state-connected", s)
    app._set_tile_state("d5", "state-error", s)
    drain()
    return s


def panel_window():
    win = Gtk.Window(title="Sign in")
    win.add_css_class("avd")
    win.set_default_size(640, 440)
    overlay = Gtk.Overlay()
    page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    page.add_css_class("avd-page")
    page.set_valign(Gtk.Align.FILL)
    filler = Gtk.Label(label="login.microsoftonline.com", vexpand=True, valign=Gtk.Align.START)
    filler.add_css_class("hero-note")
    filler.set_margin_top(24)
    page.append(filler)
    overlay.set_child(page)
    panel = SignInPanel()
    overlay.add_overlay(panel)
    win.set_child(overlay)
    win.present()
    return win, panel


if __name__ == "__main__":
    prefix = sys.argv[1]
    scenarios = sys.argv[2:] or ["signin", "workspaces", "panel"]
    app = make_app()
    app.win.present()
    drain()
    for name in scenarios:
        if name == "signin":
            app.stack.set_visible_child_name("signin")
            shoot(app.win, f"{prefix}-signin.png")
        elif name == "workspaces":
            s = workspaces(app)
            app.win.set_focus_visible(True)
            s.tiles["d4"].grab_focus()
            shoot(app.win, f"{prefix}-workspaces.png")
            app._toast(gui.tr("Signed in as {account}").format(account="alex@contoso.com"))
            drain()
            shoot(app.win, f"{prefix}-toast.png")
            app._hide_toast()
            app._settings_pop.popup()
            drain()
            shoot(app.win, f"{prefix}-settings-menu.png", [app._settings_pop])
            app._settings_pop.popdown()
            app._account_pop.popup()
            drain()
            shoot(app.win, f"{prefix}-account-menu.png", [app._account_pop])
            app._account_pop.popdown()
            app._on_filter_shortcut()
            app._filter.set_text("de")
            app._apply_filter()
            shoot(app.win, f"{prefix}-filter.png")
            app._close_filter()
        elif name in ("settings", "settings-default"):
            s = workspaces(app)
            app.win.set_focus_visible(True)
            res = None if name == "settings-default" else s.resources[0]
            gui.workspace_backend = lambda: gui.HyprlandWorkspaceBackend("/nonexistent")
            app._teams_plugin_available = lambda: True
            before = set(Gtk.Window.list_toplevels())
            app._open_settings(res, s)
            drain()
            dlg = next(w for w in Gtk.Window.list_toplevels() if w not in before and w.get_visible())
            shoot(dlg, f"{prefix}-{name}.png")
            dlg.destroy()
        elif name == "readme-main":
            s = account(app, "a" * 32, "alex@contoso.com", None)
            app.selected_id = s.id
            app._populate(gui.demo_resources(), s)
            app._set_tile_state("d2", "state-connected", s)
            drain()
            shoot(app.win, f"{prefix}-main.png")
            app._session_running = lambda child: child is s.tiles["d2"]
            pop = app._show_resource_menu(s.tiles["d2"])
            drain()
            shoot(app.win, f"{prefix}-actions.png", [pop])
            pop.popdown()
        elif name == "readme-filter":
            s = account(app, "a" * 32, "alex@contoso.com", None)
            app.selected_id = s.id
            app._populate(gui.demo_resources(), s)
            drain()
            app._on_filter_shortcut()
            app._filter.set_text("west")
            app._apply_filter()
            drain()
            shoot(app.win, f"{prefix}-filter.png")
        elif name == "readme-settings":
            s = account(app, "a" * 32, "alex@contoso.com", None)
            app.selected_id = s.id
            app._populate(gui.demo_resources(), s)
            s.resource_settings["d4"] = {"hypr_workspace": "3"}
            gui.workspace_backend = lambda: gui.HyprlandWorkspaceBackend("/nonexistent")
            app._teams_plugin_available = lambda: True
            before = set(Gtk.Window.list_toplevels())
            app._open_settings(next(r for r in s.resources if r["id"] == "d4"), s)
            drain()
            dlg = next(w for w in Gtk.Window.list_toplevels() if w not in before and w.get_visible())
            if os.environ.get("AVD_UX_SCENE") == "hover":
                def walk(w):
                    if isinstance(w, Gtk.Button) and w.get_ancestor(Gtk.HeaderBar):
                        print("hover", w.get_css_name(), w.get_css_classes())
                        w.set_state_flags(Gtk.StateFlags.PRELIGHT, False)
                    c = w.get_first_child()
                    while c:
                        walk(c); c = c.get_next_sibling()
                walk(dlg); drain()
            shoot(dlg, f"{prefix}-settings.png")
            dlg.destroy()
        elif name == "connecting":
            s = workspaces(app)
            app._set_connecting("d1", "Finance Desktop", True, s)
            drain()
            shoot(app.win, f"{prefix}-connecting.png")
        elif name == "empty":
            workspaces(app)
            empty = account(app, "c" * 32, "sam@northwind.com", None)
            app._select_account(empty.id)
            drain()
            app._populate([], empty)
            drain()
            shoot(app.win, f"{prefix}-empty.png")
        elif name == "panel":
            win, panel = panel_window()
            win.set_focus_visible(True)
            panel.set_state("insert")
            shoot(win.get_child(), f"{prefix}-key-insert.png")
            panel.set_state("touch")
            shoot(win.get_child(), f"{prefix}-key-touch.png")
            panel.set_state("busy", "Verifying with Microsoft…")
            shoot(win.get_child(), f"{prefix}-key-busy.png")
            panel.ask_pin("Enter the PIN for your security key.", lambda pin: None,
                          error="Wrong PIN, 7 attempts left")
            panel._entry.set_text("1234")
            shoot(win.get_child(), f"{prefix}-key-pin.png")
            panel.choose_account([{"name": "alex@contoso.com", "detail": "Alex Kim · Contoso"},
                                  {"name": "admin@contoso.com", "detail": "Contoso admin"},
                                  {"name": "", "detail": ""}], lambda i: None)
            shoot(win.get_child(), f"{prefix}-key-choose.png")
            panel.set_state("error", "The security key didn't respond. Try again or use another sign-in option.")
            shoot(win.get_child(), f"{prefix}-key-error.png")
            win.destroy()
