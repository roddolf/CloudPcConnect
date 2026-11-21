"""Omarchy theme tokens, the generated stylesheet, live reload and the sign-in card."""
import shutil
import time

import pytest

from avd_feed_connect.gui import omarchy, theme

COLORS = """
accent = "#81a1c1"
background = "#2e3440"
foreground = "#d8dee9"
color1 = "#bf616a"
color2 = "#a3be8c"
color3 = "#ebcb8b"
"""
SHELL = """
[menu]
border = "accent"
selected-text = "menu.border"
[popups]
border = "rgba(88c0d0ee) rgba(81a1c1ee) 45deg"
[controls]
hover-cursor-fill-alpha = 0.12
[font]
base-size = 14
"""
FONTS = """<?xml version="1.0"?>
<fontconfig>
  <match target="pattern">
    <test qual="any" name="family"><string>monospace</string></test>
    <edit name="family" mode="prepend_first"><string>Test Mono</string></edit>
  </match>
</fontconfig>
"""


def make_home(tmp_path, colors=COLORS, shell=SHELL, user_shell=None, hyprland=None):
    theme_dir = tmp_path / ".local/state/omarchy/current/theme"
    theme_dir.mkdir(parents=True)
    (theme_dir / "colors.toml").write_text(colors)
    (theme_dir / "shell.toml").write_text(shell)
    if hyprland is not None:
        (theme_dir / "hyprland.lua").write_text(hyprland)
    if user_shell is not None:
        (tmp_path / ".config/omarchy").mkdir(parents=True)
        (tmp_path / ".config/omarchy/shell.toml").write_text(user_shell)
    (tmp_path / ".config/fontconfig").mkdir(parents=True)
    (tmp_path / ".config/fontconfig/fonts.conf").write_text(FONTS)
    return tmp_path


def test_tokens_resolve_names_references_gradients_and_font(tmp_path):
    tokens = omarchy.load(make_home(tmp_path), environ={})
    assert tokens["dark"] is True
    assert (tokens["background"], tokens["foreground"]) == ("#2e3440", "#d8dee9")
    assert tokens["menu_border"] == tokens["selected_text"] == "#81a1c1"
    assert tokens["border"] == "#88c0d0"
    assert (tokens["error"], tokens["ok"], tokens["warn"]) == ("#bf616a", "#a3be8c", "#ebcb8b")
    assert tokens["hover_fill"] == 0.12 and tokens["normal_border"] == 0.4
    assert (tokens["family"], tokens["size"], tokens["radius"]) == ("Test Mono", 14.0, 0)


def test_user_shell_overrides_single_keys_and_references_follow(tmp_path):
    home = make_home(tmp_path, user_shell='[menu]\nborder = "#ff0000"\n')
    tokens = omarchy.load(home, environ={})
    assert tokens["menu_border"] == tokens["selected_text"] == "#ff0000"
    assert tokens["border"] == "#88c0d0"


def test_mode_rounding_and_overrides(tmp_path):
    light = 'mode = "light"\nbackground = "#eff1f5"\nforeground = "#4c4f69"\ncolor4 = "#1e66f5"\n'
    lua = "-- rounding = 3\nhl.config({ decoration = {\n  rounding = 20,\n  rounding_power = 3,\n} })\n"
    tokens = omarchy.load(make_home(tmp_path, colors=light, shell="", hyprland=lua), environ={})
    assert tokens["dark"] is False and tokens["accent"] == "#1e66f5"
    assert tokens["radius"] == omarchy.MAX_RADIUS
    other = tmp_path / "other"
    other.mkdir()
    (other / "colors.toml").write_text(COLORS)
    assert omarchy.load(tmp_path, environ={"AVD_OMARCHY_THEME": str(other)})["dark"] is True


def test_missing_or_disabled_theme_falls_back(tmp_path):
    assert omarchy.load(tmp_path, environ={}) is None
    home = make_home(tmp_path / "home")
    assert omarchy.load(home, environ={"AVD_OMARCHY": "0"}) is None
    assert theme.stylesheet(True, None).startswith(theme.PALETTE_DARK)
    assert theme.stylesheet(False, None).startswith(theme.PALETTE_LIGHT)


def test_stylesheet_uses_theme_palette_only_for_matching_mode(tmp_path):
    tokens = omarchy.load(make_home(tmp_path), environ={})
    css = theme.stylesheet(True, tokens)
    assert "@define-color avd_accent #81a1c1;" in css
    assert "@define-color avd_bg #2e3440;" in css
    # The darker background reads better on the pale accent than the foreground.
    assert "@define-color avd_on_accent #2e3440;" in css
    assert 'font-family:"Test Mono", monospace;' in css
    assert "border-radius:0px" in css and "border-radius:6px" not in css
    assert "gradient" not in css and "box-shadow:0" not in css
    assert theme.stylesheet(False, tokens).startswith(theme.PALETTE_LIGHT)
    assert "font-family" not in theme.stylesheet(True, None)


def test_theme_watcher_debounces_bursts_and_stop_cancels():
    timers, removed, calls = {}, [], []

    def timeout_add(_delay, callback):
        timers[len(timers) + 1] = callback
        return len(timers)

    watcher = omarchy.ThemeWatcher(lambda: calls.append(1), timeout_add=timeout_add,
                                   source_remove=removed.append)
    for _ in range(3):
        watcher.poke()
    assert removed == [1, 2]
    assert timers[3]() is False
    assert calls == [1]
    watcher.poke()
    watcher.stop()
    assert removed == [1, 2, 4] and calls == [1]


def test_theme_watcher_notices_theme_directory_swap(tmp_path):
    pytest.importorskip("gi")
    from gi.repository import GLib
    home = make_home(tmp_path)
    current = home / ".local/state/omarchy/current"
    reloads = []
    watcher = omarchy.ThemeWatcher(lambda: reloads.append(omarchy.load(home, environ={})),
                                   home=home, environ={}).start()
    try:
        # What `omarchy theme set` does: build next-theme, then swap it in.
        staged = current / "next-theme"
        shutil.copytree(current / "theme", staged)
        (staged / "colors.toml").write_text(COLORS.replace("#81a1c1", "#ff8800"))
        shutil.rmtree(current / "theme")
        staged.rename(current / "theme")
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 5
        while not reloads and time.monotonic() < deadline:
            context.iteration(False)
            time.sleep(0.01)
        assert reloads and reloads[-1]["accent"] == "#ff8800"
    finally:
        watcher.stop()


@pytest.fixture
def panel():
    pytest.importorskip("gi")
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("GTK display is unavailable")
    from avd_feed_connect.gui.signin_panel import SignInPanel
    window = Gtk.Window()
    value = SignInPanel()
    window.set_child(value)
    yield value
    window.destroy()


def test_signin_panel_states_and_cancel(panel):
    cancelled = []
    panel.connect_cancel(lambda: cancelled.append(panel.state))
    panel.set_state("touch")
    assert panel.get_visible() and panel.has_css_class("touch")
    panel.set_state("error", "No key found")
    assert panel.has_css_class("error") and not panel.has_css_class("touch")
    panel._cancel.emit("clicked")
    assert cancelled == ["error"] and not panel.get_visible() and panel.state == "idle"
    with pytest.raises(ValueError):
        panel.set_state("bogus")


def test_signin_panel_pin_callback_runs_once_and_forgets_pin(panel):
    results = []
    panel.ask_pin("Enter the PIN", results.append, error="Wrong PIN, 7 attempts left")
    assert panel.has_css_class("pin-error") and panel._entry.get_visible()
    panel._entry.set_text("123456")
    panel._entry.emit("activate")
    panel.set_state("busy")
    assert results == ["123456"] and panel._entry.get_text() == ""
    panel.ask_pin("Enter the PIN", results.append)
    panel.set_state("touch")
    panel.ask_pin("Enter the PIN", results.append)
    panel._on_key(None, _gdk().KEY_Escape, 0, 0)
    assert results == ["123456", None, None] and not panel.get_visible()


def test_signin_panel_choose_account_by_number_and_enter(panel):
    picked, cancelled = [], []
    panel.connect_cancel(lambda: cancelled.append(True))
    accounts = [{"name": "alex@example.test", "detail": "Contoso"}, {"name": "", "detail": ""}]
    panel.choose_account(accounts, picked.append)
    assert "Account 2" in label_texts(panel._list.get_row_at_index(1))
    panel._on_key(None, _gdk().KEY_2, 0, 0)
    panel.choose_account(accounts, picked.append)
    panel._list.get_row_at_index(0).activate()
    panel.choose_account(accounts, picked.append)
    panel._on_key(None, _gdk().KEY_Escape, 0, 0)
    assert picked == [1, 0, None] and cancelled == [True]


def label_texts(widget):
    texts = [widget.get_label()] if hasattr(widget, "get_label") and widget.get_label() else []
    child = widget.get_first_child()
    while child:
        texts += label_texts(child)
        child = child.get_next_sibling()
    return texts


def _gdk():
    from gi.repository import Gdk
    return Gdk
