"""App launcher entries for single Cloud PCs.

Entries go through the Desktop portal's DynamicLauncher interface, which
asks the user to confirm each one and writes the .desktop file to
~/.local/share/applications. The Exec line opens a cloudpcconnect:// URI
that the running app (or a fresh one) turns into a connection.

Every launcher icon carries a small PC badge in the bottom-right corner, so a
Cloud PC entry is never mistaken for a local app in the launcher.
"""
import hashlib
import math
import re
import urllib.parse

SCHEME = "cloudpcconnect"
ICON_SIZE = 256
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def connect_uri(account_id, resource_id):
    return "%s://connect/%s/%s" % (SCHEME, urllib.parse.quote(account_id, safe=""),
                                   urllib.parse.quote(resource_id, safe=""))


def parse_connect_uri(uri):
    """(account id, resource id) from a connect URI, or None."""
    parsed = urllib.parse.urlparse(uri or "")
    if parsed.scheme != SCHEME or parsed.netloc != "connect":
        return None
    parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
    if len(parts) != 2 or not all(_SAFE_ID.match(p) for p in parts):
        return None
    return parts[0], parts[1]


def desktop_file_id(app_id, account_id, resource_id):
    digest = hashlib.sha256(f"{account_id}/{resource_id}".encode()).hexdigest()[:16]
    return f"{app_id}.pc_{digest}.desktop"


def _desktop_value(text):
    return " ".join(str(text).split()).replace("\\", "\\\\")


def desktop_entry(app_id, name, account_id, resource_id, command="cloud-pc-connect"):
    """The entry text handed to the portal, which adds Icon."""
    return "\n".join((
        "[Desktop Entry]",
        "Type=Application",
        "Name=" + _desktop_value(name),
        "Comment=" + _desktop_value("Connect to %s (Cloud PC)" % name),
        "Exec=%s %s" % (command, connect_uri(account_id, resource_id)),
        "Categories=Network;RemoteAccess;",
        "Keywords=cloud;pc;remote;rdp;windows;",
        "StartupNotify=false",
        "X-CloudPcConnect-Resource=" + _desktop_value(resource_id),
        "",
    ))


def _hex_rgb(value, fallback):
    value = (value or fallback).lstrip("#")
    try:
        return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, IndexError):
        return _hex_rgb(fallback, "#000000")


def _rounded_rect(cr, x, y, w, h, r):
    r = max(0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _draw_pc(cr, x, y, size, rgb):
    """A monitor on a stand, filling a size×size box."""
    cr.set_source_rgb(*rgb)
    sw, sh = size * 0.86, size * 0.58
    sx, sy = x + (size - sw) / 2, y + size * 0.1
    line = max(2.0, size * 0.09)
    _rounded_rect(cr, sx, sy, sw, sh, size * 0.08)
    cr.set_line_width(line)
    cr.stroke()
    cr.rectangle(x + size * 0.44, sy + sh, size * 0.12, size * 0.14)
    cr.fill()
    _rounded_rect(cr, x + size * 0.26, sy + sh + size * 0.13, size * 0.48, line, line / 2)
    cr.fill()


def render_icon(name, picture=None, palette=None, size=ICON_SIZE):
    """PNG bytes: the picture (or a lettered tile) with a PC badge.

    picture is a path to any image GdkPixbuf reads. palette takes the Omarchy
    theme keys background, foreground and accent.
    """
    import cairo
    import gi
    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf

    palette = palette or {}
    bg = _hex_rgb(palette.get("background"), "#1e1e2e")
    fg = _hex_rgb(palette.get("foreground"), "#cdd6f4")
    accent = _hex_rgb(palette.get("accent"), "#89b4fa")
    radius = size * 0.18

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    pixbuf = None
    if picture:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(picture, size, size, True)
    if pixbuf is not None and max(pixbuf.get_width(), pixbuf.get_height()) < size // 2:
        # Feed icons are often 32px: sit them on a theme tile rather than
        # blowing them up edge to edge.
        _rounded_rect(cr, 0, 0, size, size, radius)
        cr.set_source_rgb(*bg)
        cr.fill_preserve()
        cr.set_source_rgba(*accent, 0.9)
        cr.set_line_width(size * 0.035)
        cr.stroke()
        target = size * 0.5
        scale = target / max(pixbuf.get_width(), pixbuf.get_height())
        cr.save()
        cr.translate(size * 0.46 - pixbuf.get_width() * scale / 2,
                     size * 0.46 - pixbuf.get_height() * scale / 2)
        cr.scale(scale, scale)
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
        cr.paint()
        cr.restore()
    elif pixbuf is not None:
        # Centre the picture in a rounded square.
        _rounded_rect(cr, 0, 0, size, size, radius)
        cr.clip()
        scale = max(size / pixbuf.get_width(), size / pixbuf.get_height())
        cr.save()
        cr.translate((size - pixbuf.get_width() * scale) / 2, (size - pixbuf.get_height() * scale) / 2)
        cr.scale(scale, scale)
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
        cr.paint()
        cr.restore()
        cr.reset_clip()
    else:
        _rounded_rect(cr, 0, 0, size, size, radius)
        cr.set_source_rgb(*bg)
        cr.fill_preserve()
        cr.set_source_rgba(*accent, 0.9)
        cr.set_line_width(size * 0.035)
        cr.stroke()
        letters = "".join(word[0] for word in re.findall(r"[A-Za-z0-9]+", name)[:2]).upper() or "PC"
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(size * (0.42 if len(letters) > 1 else 0.5))
        extents = cr.text_extents(letters)
        cr.move_to(size * 0.46 - extents.width / 2 - extents.x_bearing,
                   size * 0.46 - extents.height / 2 - extents.y_bearing)
        cr.set_source_rgb(*fg)
        cr.show_text(letters)

    # Badge: a filled square in the theme accent with a PC glyph, ringed in
    # the background colour so it reads on any picture.
    badge = size * 0.42
    bx = by = size - badge
    _rounded_rect(cr, bx, by, badge, badge, badge * 0.26)
    cr.set_source_rgb(*bg)
    cr.fill()
    inset = size * 0.025
    _rounded_rect(cr, bx + inset, by + inset, badge - 2 * inset, badge - 2 * inset, badge * 0.22)
    cr.set_source_rgb(*accent)
    cr.fill()
    glyph = badge * 0.62
    _draw_pc(cr, bx + (badge - glyph) / 2, by + (badge - glyph) / 2 + badge * 0.02, glyph, bg)

    import io
    out = io.BytesIO()
    surface.write_to_png(out)
    return out.getvalue()


def ico_to_png(data):
    """PNG bytes of the largest image in a Windows .ico, or None."""
    import struct
    if len(data) < 6 or data[:4] != b"\0\0\1\0":
        return None
    count = struct.unpack_from("<H", data, 4)[0]
    best = None
    for index in range(min(count, 64)):
        offset = 6 + 16 * index
        if offset + 16 > len(data):
            break
        width, height = data[offset] or 256, data[offset + 1] or 256
        size, start = struct.unpack_from("<II", data, offset + 8)
        if start + size <= len(data) and (best is None or width * height > best[0]):
            best = (width * height, data[start:start + size])
    if best is None:
        return None
    if best[1][:8] == b"\x89PNG\r\n\x1a\n":
        return best[1]
    # BMP-in-ICO: let GdkPixbuf decode the file (it picks its best entry).
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    loader = GdkPixbuf.PixbufLoader.new_with_type("ico")
    loader.write(data)
    loader.close()
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        return None
    ok, png = pixbuf.save_to_bufferv("png", [], [])
    return bytes(png) if ok else None


class PortalLauncher:
    """org.freedesktop.portal.DynamicLauncher, asynchronous, main-thread."""

    BUS = "org.freedesktop.portal.Desktop"
    PATH = "/org/freedesktop/portal/desktop"
    IFACE = "org.freedesktop.portal.DynamicLauncher"

    def __init__(self, app_id):
        self.app_id = app_id
        self._connection = None

    def _bus(self):
        """A private session-bus connection registered as this app. The portal
        takes the app ID of a host app from Registry.Register, which must come
        before any other portal call on the connection; GTK has already used
        the shared one for its settings portal."""
        if self._connection is None:
            from gi.repository import Gio, GLib
            address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            connection = Gio.DBusConnection.new_for_address_sync(
                address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
            try:
                connection.call_sync(
                    self.BUS, self.PATH, "org.freedesktop.host.portal.Registry", "Register",
                    GLib.Variant("(sa{sv})", (self.app_id, {})), None,
                    Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error:
                pass
            self._connection = connection
        return self._connection

    def available(self):
        from gi.repository import Gio, GLib
        try:
            reply = self._bus().call_sync(
                self.BUS, self.PATH, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (self.IFACE, "version")), None,
                Gio.DBusCallFlags.NONE, 2000, None)
            return reply.unpack()[0] >= 1
        except GLib.Error:
            return False

    def installed(self, file_id):
        from gi.repository import Gio, GLib
        try:
            self._bus().call_sync(self.BUS, self.PATH, self.IFACE, "GetDesktopEntry",
                                  GLib.Variant("(s)", (file_id,)), None,
                                  Gio.DBusCallFlags.NONE, 2000, None)
            return True
        except GLib.Error:
            return False

    def install(self, name, icon_png, file_id, entry_for_name, done):
        """Ask the portal to install; done(ok, message) runs on the main loop.

        entry_for_name(name) builds the entry with the name the user confirmed.
        """
        import secrets
        from gi.repository import Gio, GLib
        bus = self._bus()
        token = "cpc" + secrets.token_hex(8)
        sender = bus.get_unique_name()[1:].replace(".", "_")
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        subscription = [0]

        def on_response(connection, _sender, _path, _iface, _signal, params):
            connection.signal_unsubscribe(subscription[0])
            response, results = params.unpack()
            if response != 0:
                done(False, "")
                return
            chosen = results.get("name") or name
            try:
                connection.call_sync(
                    self.BUS, self.PATH, self.IFACE, "Install",
                    GLib.Variant("(sssa{sv})", (results["token"], file_id, entry_for_name(chosen), {})),
                    None, Gio.DBusCallFlags.NONE, 10000, None)
            except (GLib.Error, KeyError) as error:
                done(False, getattr(error, "message", str(error)))
                return
            done(True, chosen)

        subscription[0] = bus.signal_subscribe(
            self.BUS, "org.freedesktop.portal.Request", "Response", request_path,
            None, Gio.DBusSignalFlags.NO_MATCH_RULE, on_response)
        icon = Gio.BytesIcon.new(GLib.Bytes.new(icon_png)).serialize()
        options = {"handle_token": GLib.Variant("s", token),
                   "modal": GLib.Variant("b", True),
                   "launcher_type": GLib.Variant("u", 1),
                   "editable_name": GLib.Variant("b", True),
                   "editable_icon": GLib.Variant("b", False)}

        def prepared(connection, result):
            try:
                connection.call_finish(result)
            except GLib.Error as error:
                connection.signal_unsubscribe(subscription[0])
                done(False, error.message)

        bus.call(self.BUS, self.PATH, self.IFACE, "PrepareInstall",
                 GLib.Variant("(ssva{sv})", ("", name, icon, options)), None,
                 Gio.DBusCallFlags.NONE, -1, None, prepared)

    def uninstall(self, file_id):
        from gi.repository import Gio, GLib
        try:
            self._bus().call_sync(self.BUS, self.PATH, self.IFACE, "Uninstall",
                                  GLib.Variant("(sa{sv})", (file_id, {})), None,
                                  Gio.DBusCallFlags.NONE, 5000, None)
            return True
        except GLib.Error:
            return False
