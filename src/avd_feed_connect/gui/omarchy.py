"""Read the live Omarchy theme (colors, shell tokens, font, corner radius).

Mirrors the parsing in fido_picker.py (which must stay a standalone script)
and adds what the main window needs. Importing this module does not load GTK;
only ThemeWatcher touches Gio/GLib. Set AVD_OMARCHY=0 to ignore Omarchy, or
AVD_OMARCHY_THEME=<dir> to read another theme directory.
"""

import math
import os
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

CURRENT = Path(".local/state/omarchy/current")
USER_SHELL = Path(".config/omarchy/shell.toml")
FONTS_CONF = Path(".config/fontconfig/fonts.conf")
MAX_RADIUS = 8
_HEX = re.compile(r"#([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?")
_RGBA = re.compile(r"rgba?\(([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?\)")
_ROLES = {"urgent": "red"}


def read_toml(path):
    try:
        with Path(path).open("rb") as source:
            return tomllib.load(source)
    except (OSError, ValueError):
        return {}


def _number(value, fallback, low, high):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(high, max(low, value)) if math.isfinite(value) else fallback


def monospace_family(path):
    """The family Omarchy's font picker put in front of `monospace`."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    for match in root.findall("match"):
        if not any(t.get("name") == "family" and t.findtext("string") == "monospace"
                   for t in match.findall("test")):
            continue
        for edit in match.findall("edit"):
            family = (edit.findtext("string") or "").strip()
            if edit.get("name") == "family" and family:
                return family
    return None


def corner_radius(theme_dir):
    """Hyprland decoration:rounding from the theme's hyprland.lua (default 0)."""
    try:
        text = Path(theme_dir, "hyprland.lua").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    for line in text.splitlines():
        found = re.match(r"\s*rounding\s*=\s*(\d+)", line.split("--", 1)[0])
        if found:
            return int(_number(found.group(1), 0, 0, MAX_RADIUS))
    return 0


def _luminance(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def mix(a, b, amount):
    """Blend hex colour a towards b (0 → a, 1 → b)."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join("%02x" % round(x + (y - x) * amount) for x, y in zip(ca, cb))


def load(home=None, theme_dir=None, environ=None):
    """Return the theme tokens, or None when no Omarchy theme is readable."""
    env = os.environ if environ is None else environ
    if env.get("AVD_OMARCHY", "").strip().lower() in ("0", "off", "false", "no"):
        return None
    home = Path(home or Path.home())
    theme_dir = Path(theme_dir or env.get("AVD_OMARCHY_THEME") or home / CURRENT / "theme")
    colors = read_toml(theme_dir / "colors.toml")
    if not colors:
        return None
    shell = read_toml(theme_dir / "shell.toml")
    for section, values in read_toml(home / USER_SHELL).items():
        if isinstance(values, dict):
            target = shell.get(section)
            shell[section] = {**target, **values} if isinstance(target, dict) else dict(values)
    for index, name in ((0, "background"), (7, "foreground"), (4, "accent"), (1, "red"),
                        (2, "green"), (3, "yellow"), (8, "muted")):
        colors.setdefault(name, colors.get(f"color{index}"))

    def color(value, fallback):
        visited = set()
        while isinstance(value, str) and value.strip() and value not in visited:
            visited.add(value)
            token = next((part for part in value.split() if not part.endswith("deg")), "")
            for pattern in (_HEX, _RGBA):
                found = pattern.fullmatch(token)
                if found:
                    return "#" + found.group(1).lower()
            if "." in token:
                section, key = token.split(".", 1)
                table = shell.get(section)
                value = table.get(key) if isinstance(table, dict) else None
            else:
                value = colors.get(_ROLES.get(token, token))
        return fallback

    bg = color(colors.get("background"), None)
    fg = color(colors.get("foreground"), None)
    if not bg or not fg:
        return None
    accent = color(colors.get("accent"), fg)
    mode = str(colors.get("mode", "")).strip().lower()
    dark = mode == "dark" if mode in ("dark", "light") else _luminance(bg) < 0.5

    def table(name):
        value = shell.get(name)
        return value if isinstance(value, dict) else {}

    controls, menu, polkit, popups = table("controls"), table("menu"), table("polkit"), table("popups")

    def alpha(section, key, fallback):
        return _number(section.get(key, fallback), fallback, 0.0, 1.0)

    family = env.get("OMARCHY_MENU_FONT") or monospace_family(home / FONTS_CONF) or "monospace"
    return {
        "dark": dark,
        "background": color(polkit.get("background"), bg),
        "foreground": color(polkit.get("text"), fg),
        "accent": accent,
        "border": color(popups.get("border"), accent),
        "menu_border": color(menu.get("border"), fg),
        "selected_text": color(menu.get("selected-text"), accent),
        "error": color(polkit.get("text-error"), color(colors.get("red"), "#e06c75")),
        "ok": color(colors.get("green"), accent),
        "warn": color(colors.get("yellow"), accent),
        "normal_fill": alpha(controls, "normal-fill-alpha", 0.04),
        "normal_border": alpha(controls, "normal-border-alpha", 0.4),
        "hover_fill": alpha(controls, "hover-cursor-fill-alpha", 0.08),
        "hover_border": alpha(controls, "hover-cursor-border-alpha", 0.25),
        "selected_fill": alpha(controls, "selected-fill-alpha", 0.18),
        "pressed_fill": alpha(controls, "pressed-fill-alpha", 0.22),
        "family": family.replace("\\", "").replace('"', "").replace("\n", "").strip() or "monospace",
        "size": round(_number(table("font").get("base-size", 12), 12, 8, 32), 1),
        "radius": corner_radius(theme_dir),
    }


def watched_paths(home=None, environ=None):
    """Directories/files whose changes can alter load()'s result."""
    env = os.environ if environ is None else environ
    home = Path(home or Path.home())
    custom = env.get("AVD_OMARCHY_THEME")
    theme = Path(custom) if custom else home / CURRENT / "theme"
    return [theme.parent, theme, home / USER_SHELL, home / FONTS_CONF]


class ThemeWatcher:
    """Call `callback()` once a burst of theme file changes has settled.

    `omarchy theme set` deletes and renames the whole theme directory, so the
    watcher re-arms its monitors after every burst. The scheduler hooks exist
    for tests; by default they are GLib's.
    """

    DELAY_MS = 250

    def __init__(self, callback, home=None, environ=None, timeout_add=None, source_remove=None):
        self._callback = callback
        self._home = home
        self._environ = environ
        self._timeout_add = timeout_add
        self._source_remove = source_remove
        self._monitors = []
        self._pending = 0

    def start(self):
        from gi.repository import Gio, GLib
        self._timeout_add = self._timeout_add or GLib.timeout_add
        self._source_remove = self._source_remove or GLib.source_remove
        self.stop_monitors()
        for path in watched_paths(self._home, self._environ):
            if not path.exists():
                continue
            target = Gio.File.new_for_path(str(path))
            try:
                monitor = (target.monitor_directory if path.is_dir() else target.monitor_file)(
                    Gio.FileMonitorFlags.WATCH_MOVES, None)
            except Exception:
                continue
            monitor.connect("changed", lambda *_: self.poke())
            self._monitors.append(monitor)
        return self

    def poke(self):
        if self._pending:
            self._source_remove(self._pending)
        self._pending = self._timeout_add(self.DELAY_MS, self._fire)

    def _fire(self):
        self._pending = 0
        if self._monitors:
            self.start()
        self._callback()
        return False

    def stop_monitors(self):
        for monitor in self._monitors:
            monitor.cancel()
        self._monitors = []

    def stop(self):
        self.stop_monitors()
        if self._pending:
            self._source_remove(self._pending)
            self._pending = 0
