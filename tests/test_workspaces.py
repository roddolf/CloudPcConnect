"""Bounded placement and graceful fallback without a real Shell extension."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
pytest.importorskip('gi')
from gi.repository import GLib
from avd_feed_connect.gui import workspaces


def placement():
    return workspaces.WorkspacePlacement(SimpleNamespace(poll=lambda: None), 'AVD-' + 'a' * 32)


def test_start_only_once(monkeypatch):
    value = placement()
    monkeypatch.setattr(value, '_try_place', Mock())
    value.start()
    value.start()
    value._try_place.assert_called_once()


def test_exited_and_exhausted_never_call_bus(monkeypatch):
    bus = Mock()
    monkeypatch.setattr(workspaces.Gio, 'bus_get_sync', bus)
    value = placement()
    value.proc.poll = lambda: 1
    value.start()
    value.proc.poll = lambda: None
    value.attempts = 60
    value._try_place()
    bus.assert_not_called()


def test_absent_extension_does_not_retry(monkeypatch):
    value = placement()
    timer = Mock()
    monkeypatch.setattr(workspaces.GLib, 'timeout_add', timer)
    connection = Mock()
    connection.call_finish.side_effect = GLib.Error('No service')
    value._finished(connection, object())
    timer.assert_not_called()


def test_window_map_race_and_late_monitor_grace(monkeypatch):
    value = placement()
    timer = Mock()
    monkeypatch.setattr(workspaces.GLib, 'timeout_add', timer)
    connection = Mock()
    connection.call_finish.return_value = GLib.Variant('(b)', (False,))
    value._finished(connection, object())
    timer.assert_called_once_with(250, value._try_place)
    timer.reset_mock()
    connection.call_finish.return_value = GLib.Variant('(b)', (True,))
    value._finished(connection, object())
    timer.assert_called_once_with(250, value._try_place)
    timer.reset_mock()
    value.attempts = 60
    value._finished(connection, object())
    timer.assert_not_called()


def test_availability_requires_running_bridge(monkeypatch):
    monkeypatch.setenv('XDG_CURRENT_DESKTOP', 'GNOME')
    connection = Mock()
    monkeypatch.setattr(workspaces.Gio, 'bus_get_sync', lambda *args: connection)
    for present in (False, True):
        connection.call_sync.return_value = GLib.Variant('(b)', (present,))
        assert workspaces.available() is present
    connection.call_sync.side_effect = GLib.Error('Permission denied')
    assert workspaces.available() is False


@pytest.mark.parametrize('environ,expected', [
    ({'XDG_CURRENT_DESKTOP': 'GNOME'}, True),
    ({'XDG_CURRENT_DESKTOP': 'ubuntu:GNOME'}, True),
    ({'XDG_CURRENT_DESKTOP': 'GNOME-Classic:GNOME'}, True),
    ({'XDG_SESSION_DESKTOP': 'gnome-xorg'}, True),
    ({'DESKTOP_SESSION': 'gnome'}, True),
    ({'XDG_CURRENT_DESKTOP': 'KDE', 'XDG_SESSION_DESKTOP': 'gnome'}, False),
    ({'XDG_CURRENT_DESKTOP': 'sway', 'DESKTOP_SESSION': 'gnome'}, False),
    ({'XDG_CURRENT_DESKTOP': 'X-Cinnamon'}, False),
    ({}, False),
])
def test_desktop_detection(environ, expected):
    assert workspaces.current_desktop_is_gnome(environ) is expected
    assert (workspaces.select_backend(environ) is not None) is expected


def test_non_gnome_never_probes_shell_even_if_preference_saved(monkeypatch):
    monkeypatch.setenv('XDG_CURRENT_DESKTOP', 'KDE')
    probe = Mock()
    monkeypatch.setattr(workspaces, '_bridge_available', probe)
    assert workspaces.available() is False
    probe.assert_not_called()


class FakeHyprland:
    """A Hyprland command socket that records requests."""

    def __init__(self, tmp_path, clients, lua=True):
        import json, socket, threading
        self.requests = []
        self.clients = clients
        self.dir = tmp_path / 'hypr' / 'sig'
        self.dir.mkdir(parents=True)
        self.path = str(self.dir / '.socket.sock')
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen()

        def serve():
            while True:
                try:
                    conn, _ = self.server.accept()
                except OSError:
                    return
                with conn:
                    request = conn.recv(65536).decode()
                    self.requests.append(request)
                    if request == 'j/clients':
                        reply = json.dumps(self.clients)
                    elif request.startswith('dispatch hl.'):
                        reply = 'ok' if lua else 'error'
                    else:
                        reply = 'ok'
                    conn.sendall(reply.encode())
        threading.Thread(target=serve, daemon=True).start()

    def close(self):
        self.server.close()


def test_hyprland_backend_needs_its_socket(tmp_path):
    env = {'XDG_CURRENT_DESKTOP': 'Hyprland', 'HYPRLAND_INSTANCE_SIGNATURE': 'sig',
           'XDG_RUNTIME_DIR': str(tmp_path)}
    assert workspaces.select_backend(env) is None
    fake = FakeHyprland(tmp_path, [])
    try:
        assert isinstance(workspaces.select_backend(env), workspaces.HyprlandWorkspaceBackend)
        assert workspaces.select_backend(dict(env, HYPRLAND_INSTANCE_SIGNATURE='../x')) is None
    finally:
        fake.close()


@pytest.mark.parametrize('lua', [True, False])
def test_hyprland_placement_moves_only_matching_windows(tmp_path, lua):
    clients = [{'address': '0x1', 'title': 'CloudPC-abc juno', 'class': 'com.freerdp.client.sdl3',
                'workspace': {'name': '1'}},
               {'address': '0x2', 'title': 'Other', 'class': 'x', 'workspace': {'name': '1'}}]
    fake = FakeHyprland(tmp_path, clients, lua=lua)
    try:
        value = workspaces.HyprlandPlacement(SimpleNamespace(poll=lambda: None), 'CloudPC-abc', '4',
                                             socket_path=fake.path)
        value._poll()
        value._poll()
        value.on_connected()
        moves = [r for r in fake.requests if 'workspace' in r and r.startswith('dispatch')]
        if lua:
            assert moves[0] == 'dispatch hl.dsp.window.move({ workspace = "4", follow = false, window = "address:0x1" })'
            assert moves[-1] == 'dispatch hl.dsp.focus({ workspace = "4" })'
        else:
            assert 'dispatch movetoworkspacesilent 4,address:0x1' in moves
            assert moves[-1] == 'dispatch workspace 4'
        assert not any('0x2' in r for r in fake.requests)
        assert value.moved == {'0x1'}
    finally:
        fake.close()


@pytest.mark.parametrize('value, ok', [('3', True), ('99', True), ('0', False), ('100', False),
                                       ('special:x', False), ('+1', False), ('name:dev box', True),
                                       ('name:a"b', False), ('', False), (None, False)])
def test_valid_workspace(value, ok):
    assert workspaces.valid_workspace(value) is ok
