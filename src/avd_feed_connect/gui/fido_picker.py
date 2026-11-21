#!/usr/bin/env python3
"""Private, one-shot security-key dialogs for the bundled FreeRDP client."""

import math
import os
import re
import struct
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path


def read_toml(path):
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except (OSError, ValueError):
        return {}


def omarchy_style(home):
    current = home / ".local/state/omarchy/current/theme"
    colors = read_toml(current / "colors.toml")
    if not colors:
        return None
    shell = read_toml(current / "shell.toml")
    overrides = read_toml(home / ".config/omarchy/shell.toml")
    for section, values in overrides.items():
        if isinstance(values, dict):
            shell.setdefault(section, {}).update(values)

    def color(value, fallback):
        visited = set()
        while isinstance(value, str) and value not in visited:
            visited.add(value)
            value = value.strip().split()[0] if value.strip() else ""
            if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                return value
            rgba = re.fullmatch(r"rgba\(([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?\)", value)
            if rgba:
                return "#" + rgba.group(1)
            if "." in value:
                section, key = value.split(".", 1)
                value = shell.get(section, {}).get(key)
            else:
                value = colors.get(value)
        return fallback

    bg = color(colors.get("background"), "#101315")
    fg = color(colors.get("foreground"), "#cacccc")
    accent = color(colors.get("accent"), fg)
    menu = shell.get("menu", {})
    polkit = shell.get("polkit", {})
    controls = shell.get("controls", {})
    font = shell.get("font", {})
    try:
        size = float(font.get("base-size", 12))
        size = max(1, size) if math.isfinite(size) else 12
    except (TypeError, ValueError):
        size = 12

    family = os.environ.get("OMARCHY_MENU_FONT") or "JetBrainsMono Nerd Font"
    if not os.environ.get("OMARCHY_MENU_FONT"):
        try:
            root = ET.parse(home / ".config/fontconfig/fonts.conf").getroot()
            for match in root.findall("match"):
                tests = match.findall("test")
                if not any(t.get("name") == "family" and
                           t.findtext("string") == "monospace" for t in tests):
                    continue
                for edit in match.findall("edit"):
                    if edit.get("name") == "family" and edit.get("mode") == "prepend_first":
                        family = edit.findtext("string") or family
        except (OSError, ET.ParseError):
            pass
    family = family.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")

    def alpha(section, key, fallback):
        try:
            return min(1.0, max(0.0, float(section.get(key, fallback))))
        except (TypeError, ValueError):
            return fallback

    return {
        "background": color(polkit.get("background"), bg),
        "foreground": color(polkit.get("text"), fg),
        "accent": color(polkit.get("accent"), accent),
        "selection": color(menu.get("selected-background"), fg),
        "selection_alpha": alpha(menu, "selected-background-alpha", 0.08),
        "selected_text": color(menu.get("selected-text"), accent),
        "hover_alpha": alpha(controls, "hover-cursor-fill-alpha", 0.08),
        "focus_alpha": alpha(controls, "focus-fill-alpha", 0.08),
        "error": color(polkit.get("text-error"), color(colors.get("red"), "#e06c75")),
        "family": family,
        "size": size,
    }


def theme_css(style):
    return """
@define-color fido_bg %(background)s;
@define-color fido_fg %(foreground)s;
@define-color fido_accent %(accent)s;
@define-color fido_selection %(selection)s;
@define-color fido_selected_text %(selected_text)s;
@define-color fido_error %(error)s;
window.fido, window.fido headerbar {
  background: @fido_bg; color: @fido_fg;
  font-family: "%(family)s"; font-size: %(size)spx;
}
window.fido headerbar { box-shadow: none; border-bottom: 1px solid alpha(@fido_fg, 0.2); }
window.fido label { color: @fido_fg; }
window.fido .fido-heading { font-size: %(size)spx; font-weight: bold; color: @fido_accent; }
window.fido .fido-description { color: alpha(@fido_fg, 0.75); }
window.fido label.fido-error { color: @fido_error; }
window.fido entry.error { border-color: @fido_error; }
window.fido list, window.fido scrolledwindow { background: transparent; }
window.fido row {
  background: transparent; color: @fido_fg;
  border: 1px solid transparent; border-radius: 6px; margin: 2px 0;
}
window.fido row:hover { background: alpha(@fido_fg, %(hover_alpha)s); }
window.fido row:selected {
  background: alpha(@fido_selection, %(selection_alpha)s);
  border-color: alpha(@fido_fg, 0.25);
}
window.fido row:selected label { color: @fido_selected_text; }
window.fido row:focus-visible { outline: 2px solid @fido_accent; outline-offset: -2px; }
window.fido button, window.fido entry {
  color: @fido_fg; background: alpha(@fido_fg, 0.04);
  border: 1px solid alpha(@fido_fg, 0.4); border-radius: 6px; box-shadow: none;
}
window.fido button:hover, window.fido entry:focus {
  background: alpha(@fido_fg, %(focus_alpha)s); border-color: @fido_accent;
}
window.fido button.suggested-action { color: @fido_accent; border-color: @fido_accent; }
window.fido button:disabled { opacity: 0.45; }
""" % style


def configure_display():
    picker_display = os.environ.pop("AVD_PICKER_WAYLAND_DISPLAY", None)
    if picker_display:
        os.environ["WAYLAND_DISPLAY"] = picker_display
    if os.environ.get("WAYLAND_DISPLAY"):
        os.environ["GDK_BACKEND"] = "wayland"


def read_exact(size):
    chunks = []
    while size:
        chunk = os.read(0, size)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def read_accounts():
    count = struct.unpack("!I", read_exact(4))[0]
    if not 2 <= count <= 128:
        raise ValueError("Invalid account count")
    accounts = []
    for _ in range(count):
        length = struct.unpack("!I", read_exact(4))[0]
        if length > 520:
            raise ValueError("Account label is too long")
        accounts.append(read_exact(length).decode("utf-8"))
    return accounts


SITE_PATTERN = re.compile(r"[A-Za-z0-9.-]{1,253}")
RETRIES_PATTERN = re.compile(r"[0-9]{1,3}|wrong")


def parse_args(argv):
    """Return (pin_mode, site, retries) for: [--pin [--site S] [--retries N|wrong]].

    retries is None before any wrong PIN, -1 after a wrong PIN with an unknown
    count, else the attempts left.
    """
    if not argv:
        return False, None, None
    if argv[0] != "--pin":
        raise ValueError("Unknown mode")
    site = None
    retries = None
    rest = list(argv[1:])
    while rest:
        if len(rest) < 2:
            raise ValueError("Missing option value")
        option, value = rest[0], rest[1]
        del rest[:2]
        if option == "--site" and site is None and SITE_PATTERN.fullmatch(value):
            site = value
        elif option == "--retries" and retries is None and RETRIES_PATTERN.fullmatch(value):
            retries = -1 if value == "wrong" else int(value)
        else:
            raise ValueError("Invalid option")
    return True, site, retries


def pin_texts(site, retries):
    """Return (description, error) for the PIN prompt; error is None on the first try."""
    description = (f"Enter the PIN for your security key to sign in to {site}." if site
                   else "Enter the PIN for your security key.")
    if retries is None:
        return description, None
    if retries < 0:
        return description, "Wrong PIN. Try again."
    if retries == 1:
        return description, "Wrong PIN. 1 attempt left before the key locks."
    return description, f"Wrong PIN. {retries} attempts left."


def main():
    try:
        pin_mode, site, retries = parse_args(sys.argv[1:])
    except ValueError:
        return 1
    accounts = []
    if not pin_mode:
        try:
            accounts = read_accounts()
        except (EOFError, UnicodeError, ValueError):
            return 1

    configure_display()

    import gi

    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gio, GLib, Gtk

    app = Gtk.Application(
        application_id="io.github.roddolf.CloudPcConnect.SecurityKey",
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )
    result = None

    def show_dialog(application):
        nonlocal result
        title = "Security key PIN" if pin_mode else "Choose an account"
        window = Gtk.ApplicationWindow(application=application, title=title)
        window.add_css_class("fido")
        window.set_default_size(520, 240 if pin_mode else 400)
        window.set_resizable(True)
        provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        current_style = None

        def refresh_style():
            nonlocal current_style
            style = omarchy_style(Path.home())
            if style != current_style:
                provider.load_from_data(theme_css(style).encode() if style else b"")
                current_style = style
            return GLib.SOURCE_CONTINUE

        refresh_style()
        GLib.timeout_add(750, refresh_style)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)
        window.set_child(box)

        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class("fido-heading")
        box.append(heading)
        description_text, error_text = (pin_texts(site, retries) if pin_mode else (
            "Select the account to use with your security key.", None))
        description = Gtk.Label(label=description_text, xalign=0)
        description.set_wrap(True)
        description.add_css_class("fido-description")
        box.append(description)

        listing = None
        entry = None
        if pin_mode:
            entry = Gtk.PasswordEntry()
            entry.set_property("placeholder-text", "PIN")
            entry.set_property("activates-default", True)
            box.append(entry)
            if error_text:
                entry.add_css_class("error")
                error = Gtk.Label(label=error_text, xalign=0,
                                  accessible_role=Gtk.AccessibleRole.ALERT)
                error.set_wrap(True)
                error.add_css_class("fido-error")
                box.append(error)
        else:
            listing = Gtk.ListBox()
            listing.add_css_class("boxed-list")
            listing.set_selection_mode(Gtk.SelectionMode.SINGLE)
            for label in accounts:
                row = Gtk.ListBoxRow()
                text = Gtk.Label(label=label, xalign=0)
                text.set_wrap(True)
                text.set_selectable(False)
                text.set_margin_top(12)
                text.set_margin_bottom(12)
                text.set_margin_start(14)
                text.set_margin_end(14)
                row.set_child(text)
                listing.append(row)
            listing.unselect_all()

            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_vexpand(True)
            scroll.set_child(listing)
            box.append(scroll)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        confirm = Gtk.Button(label="Continue")
        confirm.add_css_class("suggested-action")
        confirm.set_sensitive(False)
        buttons.append(cancel)
        buttons.append(confirm)
        box.append(buttons)
        def finish(selected):
            nonlocal result
            result = selected
            window.close()

        cancel.connect("clicked", lambda _: finish(None))
        window.set_default_widget(confirm)
        if pin_mode:
            entry.connect("changed", lambda widget: confirm.set_sensitive(bool(widget.get_text())))
            confirm.connect("clicked", lambda _: finish(entry.get_text().encode("utf-8")))
        else:
            listing.connect("row-selected", lambda _, row: confirm.set_sensitive(row is not None))
            confirm.connect(
                "clicked",
                lambda _: finish(str(listing.get_selected_row().get_index()).encode("ascii"))
                if listing.get_selected_row() is not None else None,
            )
            listing.connect("row-activated", lambda _, row: finish(str(row.get_index()).encode("ascii")))
        controller = Gtk.EventControllerKey()
        controller.connect(
            "key-pressed",
            lambda _, key, _code, _state: (finish(None) or True)
            if key == Gdk.KEY_Escape else False,
        )
        window.add_controller(controller)
        window.present()
        if entry:
            entry.grab_focus()

    def activate(application):
        try:
            show_dialog(application)
        except Exception:
            application.quit()

    app.connect("activate", activate)
    status = app.run([])
    data = result or b""
    sys.stdout.buffer.write(struct.pack("!I", len(data)) + data)
    sys.stdout.buffer.flush()
    return status


if __name__ == "__main__":
    sys.exit(main())
