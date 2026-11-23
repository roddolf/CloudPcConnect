"""Hyprland workspace placement against a fake IPC socket."""
from types import SimpleNamespace
import pytest
pytest.importorskip('gi')
from avd_feed_connect.gui import workspaces


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
    assert workspaces.select_backend({'XDG_RUNTIME_DIR': str(tmp_path)}) is None
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
