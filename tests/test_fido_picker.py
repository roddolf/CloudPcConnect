import importlib.util
import os
import struct
from pathlib import Path
from unittest.mock import patch

import pytest


path = Path(__file__).parents[1] / "src/avd_feed_connect/gui/fido_picker.py"
spec = importlib.util.spec_from_file_location("fido_picker", path)
picker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(picker)


def decode(payload):
    reader, writer = os.pipe()
    try:
        os.write(writer, payload)
        os.close(writer)
        writer = None
        real_read = os.read
        with patch.object(picker.os, "read", side_effect=lambda _fd, size: real_read(reader, size)):
            return picker.read_accounts()
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)


def test_accounts_are_decoded_in_order():
    labels = ["First account", "User - user@example.test"]
    payload = struct.pack("!I", len(labels))
    for label in labels:
        data = label.encode()
        payload += struct.pack("!I", len(data)) + data
    assert decode(payload) == labels


@pytest.mark.parametrize(
    "payload,error",
    [
        (struct.pack("!I", 1), ValueError),
        (struct.pack("!I", 129), ValueError),
        (struct.pack("!II", 2, 521), ValueError),
        (struct.pack("!II", 2, 4) + b"ab", EOFError),
        (struct.pack("!II", 2, 1) + b"\xff", UnicodeError),
    ],
)
def test_invalid_payloads_fail_closed(payload, error):
    with pytest.raises(error):
        decode(payload)


def test_omarchy_style_tracks_theme_and_user_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("OMARCHY_MENU_FONT", raising=False)
    theme = tmp_path / ".local/state/omarchy/current/theme"
    theme.mkdir(parents=True)
    (theme / "colors.toml").write_text(
        'background = "#101112"\nforeground = "#eeeeee"\naccent = "#123456"\n'
    )
    (theme / "shell.toml").write_text(
        '[hyprland]\nactive-border = "#abc123"\n'
        '[polkit]\nbackground = "#202122"\naccent = "hyprland.active-border"\n'
        '[font]\nbase-size = 13\n'
    )
    user = tmp_path / ".config/omarchy"
    user.mkdir(parents=True)
    (user / "shell.toml").write_text('[font]\nbase-size = 16\n')
    font_dir = tmp_path / ".config/fontconfig"
    font_dir.mkdir()
    (font_dir / "fonts.conf").write_text(
        '<fontconfig><match><test name="family"><string>monospace</string></test>'
        '<edit name="family" mode="prepend_first"><string>Example Mono</string></edit>'
        '</match></fontconfig>'
    )

    style = picker.omarchy_style(tmp_path)
    assert style["background"] == "#202122"
    assert style["accent"] == "#abc123"
    assert style["family"] == "Example Mono"
    assert style["size"] == 16
    assert "@define-color fido_bg #202122;" in picker.theme_css(style)

    (theme / "shell.toml").write_text('[polkit]\nbackground = "#345678"\n')
    assert picker.omarchy_style(tmp_path)["background"] == "#345678"


def test_missing_omarchy_theme_uses_native_defaults(tmp_path):
    assert picker.omarchy_style(tmp_path) is None


def test_picker_restores_wayland_without_changing_freerdp_backend(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("GDK_BACKEND", "x11")
    monkeypatch.setenv("AVD_PICKER_WAYLAND_DISPLAY", "wayland-test")
    picker.configure_display()
    assert os.environ["WAYLAND_DISPLAY"] == "wayland-test"
    assert os.environ["GDK_BACKEND"] == "wayland"
    assert "AVD_PICKER_WAYLAND_DISPLAY" not in os.environ


@pytest.mark.parametrize(
    "argv,expected",
    [
        ([], (False, None, None)),
        (["--pin"], (True, None, None)),
        (["--pin", "--site", "login.microsoft.com"], (True, "login.microsoft.com", None)),
        (["--pin", "--site", "example.test", "--retries", "7"], (True, "example.test", 7)),
        (["--pin", "--retries", "wrong"], (True, None, -1)),
    ],
)
def test_pin_arguments_are_parsed(argv, expected):
    assert picker.parse_args(argv) == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["--site", "example.test"],
        ["--pin", "--site"],
        ["--pin", "--site", "bad site"],
        ["--pin", "--site", "a" * 254],
        ["--pin", "--retries", "1000"],
        ["--pin", "--retries", "-1"],
        ["--pin", "--site", "a.test", "--site", "b.test"],
        ["--pin", "--other", "x"],
    ],
)
def test_invalid_pin_arguments_fail_closed(argv):
    with pytest.raises(ValueError):
        picker.parse_args(argv)


def test_pin_prompt_names_site_and_attempts_left():
    assert picker.pin_texts(None, None) == ("Enter the PIN for your security key.", None)
    description, error = picker.pin_texts("login.microsoft.com", None)
    assert "login.microsoft.com" in description and error is None
    assert picker.pin_texts(None, 7)[1] == "Wrong PIN. 7 attempts left."
    assert "1 attempt left" in picker.pin_texts(None, 1)[1]
    assert picker.pin_texts(None, -1)[1] == "Wrong PIN. Try again."


def test_error_colour_follows_polkit_theme(tmp_path):
    theme = tmp_path / ".local/state/omarchy/current/theme"
    theme.mkdir(parents=True)
    (theme / "colors.toml").write_text('background = "#101112"\nred = "#aa0000"\n')
    assert picker.omarchy_style(tmp_path)["error"] == "#aa0000"
    (theme / "shell.toml").write_text('[polkit]\ntext-error = "#bf616a"\n')
    style = picker.omarchy_style(tmp_path)
    assert style["error"] == "#bf616a"
    assert "@define-color fido_error #bf616a;" in picker.theme_css(style)
