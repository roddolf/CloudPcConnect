"""Exercise account flows with real GTK widgets, without network or credentials."""
import base64
import json
import os

import pytest

pytest.importorskip('gi')
import gi
try:
    gi.require_version('Gtk', '4.0')
    gi.require_version('WebKit', '6.0')
except ValueError:
    pytest.skip('GTK/WebKit typelibs are unavailable', allow_module_level=True)
from gi.repository import Gtk, GLib
if not Gtk.init_check():
    pytest.skip('GTK display is unavailable', allow_module_level=True)

import avd_feed_gui as gui
from avd_feed_connect.accounts import profile_dir
from avd_feed_connect.gui.session import AccountSession


def drain():
    context = GLib.MainContext.default()
    for _ in range(200):
        if not context.pending():
            break
        context.iteration(False)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('AVD_DEMO', '1')
    monkeypatch.setattr(gui.config, 'CACHE', str(tmp_path / 'token-cache.json'))
    monkeypatch.setattr(gui.storage, 'SETTINGS_FILE', str(tmp_path / 'settings.json'))
    # Keep general UI assertions independent of the host's language.
    gui.storage.save_settings({'_language': 'en'})
    monkeypatch.setattr(gui.TokenStore, '_keyring_store', lambda *a: False)
    monkeypatch.setattr(gui.TokenStore, '_keyring_load', lambda *a: None)
    monkeypatch.setattr(gui.TokenStore, '_keyring_clear', lambda *a: None)
    import uuid
    monkeypatch.setattr(gui, "APP_ID", "io.github.avdfeed.test" + uuid.uuid4().hex)
    value = gui.AvdApp()
    value.register(None)
    value._build_ui()
    monkeypatch.setattr(value, '_background_refresh', lambda *a: None)
    monkeypatch.setattr(value, '_load_icons', lambda *a: None)
    monkeypatch.setattr(value, '_error', lambda message: pytest.fail(message))
    yield value
    if value._signin_dlg:
        value._signin_dlg.destroy()
    for account in value.accounts.values():
        if account.refresh_source:
            GLib.source_remove(account.refresh_source)
    value.win.destroy()
    drain()


def add_saved(app, tenant, email):
    profile = app.registry.upsert({'tid': tenant, 'oid': 'user', 'upn': email})
    owner = AccountSession(profile, app.registry.client(profile['id']), profile_dir(app.registry.root, profile['id']))
    owner.resources = [{'id': 'same', 'title': email, 'tenant': tenant, 'type': 'Desktop'}]
    app.accounts[owner.id] = owner
    return owner


def response(tenant, email):
    claims = {'tid': tenant, 'oid': 'user', 'preferred_username': email}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    return {'id_token': 'x.' + encoded + '.x', 'access_token': 'access', 'refresh_token': 'secret'}


def labels(widget):
    result = []
    child = widget.get_first_child()
    while child:
        if isinstance(child, Gtk.Button):
            result.append(child.get_label())
        result.extend(labels(child))
        child = child.get_next_sibling()
    return result


def test_account_popover_contains_only_accounts_and_auth_actions(app):
    a = add_saved(app, 'a', 'a@example.com')
    app._select_account(a.id)
    drain()
    assert labels(app._account_pop.get_child())[-3:] == ['Add account', 'Open a downloaded .rdp…', 'Sign out']
    text = app._account_rows.get_first_child().get_first_child().get_child()
    assert text.get_first_child().get_text() == '✓ example.com'
    assert text.get_last_child().get_text() == 'a@example.com'


def test_switch_and_late_results_preserve_owner_and_process(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    child = app._child_for('same', a)
    marker = object()
    child._proc = marker
    app._select_account(b.id)
    app._populate(a.resources, a)
    app._set_status('late account a status', a)
    drain()
    assert app.current is b
    assert app._tiles[0]._res['title'] == 'b@example.com'
    assert app._child_for('same', a)._proc is marker
    app._select_account(a.id)
    assert app._tiles[0] is child


def test_cancel_add_keeps_previous_selection(app, monkeypatch):
    a = add_saved(app, 'a', 'a@example.com')
    app._select_account(a.id)
    monkeypatch.setattr(app, '_interactive_signin', lambda owner, **_: None)
    app._add_account()
    pending = app._adding
    assert pending is not a
    app._discard_add(pending)
    assert app.current is a
    assert len(app.registry.profiles) == 1
    assert not os.path.exists(pending.directory)


def test_duplicate_add_reuses_profile(app, monkeypatch):
    a = add_saved(app, 'a', 'a@example.com')
    app._select_account(a.id)
    monkeypatch.setattr(app, '_interactive_signin', lambda owner, **_: None)
    app._add_account()
    app._finish_signin(app._adding, response('a', 'a@example.com'))
    assert app.current is a
    assert len(app.registry.profiles) == 1
    assert app._adding is None
    assert a.client.load_token_record()['refresh_token'] == 'secret'


def test_sign_out_removes_only_selected_account(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    a.client.save_refresh_token('a')
    b.client.save_refresh_token('b')
    app._select_account(a.id)
    app._sign_out()
    drain()
    assert a.removed
    assert a.client.load_token_record() is None
    assert b.client.load_token_record()['refresh_token'] == 'b'
    assert app.current is b
    assert app._tiles[0]._account is b


def test_resource_preferences_are_account_local(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._settings['_default'] = {'scale': '125'}
    a.resource_settings['same'] = {'scale': '150'}
    assert app._eff('scale', a.resources[0], a) == '150'
    assert app._eff('scale', b.resources[0], b) == '125'


def test_teams_optimization_defaults_to_video_with_overrides(app, monkeypatch):
    a = add_saved(app, 'a', 'a@example.com')
    res = a.resources[0]
    monkeypatch.setattr(app, '_teams_plugin_available', lambda: True)
    assert app._eff_teams(res, a) == 'video'
    app._settings['_default'] = {'teams': 'audio'}
    assert app._eff_teams(res, a) == 'audio'
    a.resource_settings[res['id']] = {'teams': 'off'}
    assert app._eff_teams(res, a) == 'off'
    monkeypatch.setattr(app, '_teams_plugin_available', lambda: False)
    a.resource_settings[res['id']] = {'teams': 'video'}
    assert app._eff_teams(res, a) == 'off'


def test_tray_icon_dir_prefers_installed_tray_dir(app, tmp_path, monkeypatch):
    share = tmp_path / 'share' / 'avd-feed-connect'
    (share / 'tray').mkdir(parents=True)
    monkeypatch.setattr(gui, '__file__', str(share / 'avd_feed_gui.py'))
    assert app._tray_icon_dir() == ''
    (share / 'tray' / (gui.APP_ID + '-symbolic')).write_text('<svg/>')
    assert app._tray_icon_dir() == str(share / 'tray')


def test_teams_plugin_is_found_under_the_client_prefix(app, tmp_path, monkeypatch):
    monkeypatch.setattr(gui.config, 'SDL', str(tmp_path / 'bin' / 'sdl-freerdp'))
    assert not app._teams_plugin_available()
    plugin = tmp_path / 'lib' / 'freerdp3' / 'libteams-redir-client.so'
    plugin.parent.mkdir(parents=True)
    plugin.write_bytes(b'')
    assert app._teams_plugin_available()


def test_tray_connects_unselected_saved_account_without_switching(app, monkeypatch):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    activated = []
    monkeypatch.setattr(app, '_on_tile_activated', lambda grid, child, windowed=False: activated.append(child._account.id))
    app._on_tray_action(('connect', b.id, 'same'))
    assert activated == [b.id]
    assert app.current is a


def test_settings_popover_keeps_general_options_separate(app):
    options = labels(app._settings_pop.get_child())
    assert 'Default settings…' in options
    assert 'Sign out' not in options
    assert 'Add account' not in options


def test_sign_out_does_not_break_reader_when_log_directory_disappears(app):
    from types import SimpleNamespace
    a = add_saved(app, 'a', 'a@example.com')
    app._select_account(a.id)
    child = app._child_for('same', a)
    child._launching = True
    app._sign_out()
    master, slave = os.pipe()
    os.close(slave)
    app._watch_session(child, a.resources[0], SimpleNamespace(poll=lambda: 0), master,
                       os.path.join(a.directory, 'feed', 'removed.log'), a)
    drain()
    assert child._launching is False
    with pytest.raises(OSError):
        os.read(master, 1)


def test_account_list_separator_is_hidden_until_accounts_exist(app):
    separator = app._account_rows.get_next_sibling()
    assert isinstance(separator, Gtk.Separator)
    assert not separator.get_visible()
    a = add_saved(app, 'a', 'a@example.com')
    app._select_account(a.id)
    assert separator.get_visible()
    app._sign_out()
    assert not separator.get_visible()


def test_popover_actions_follow_selected_theme(app):
    button = app._account_rows.get_next_sibling().get_next_sibling()
    app._set_theme('dark')
    drain()
    dark = button.get_style_context().get_color()
    assert dark.red > 0.8 and dark.green > 0.8
    app._set_theme('light')
    drain()
    light = button.get_style_context().get_color()
    assert light.red < 0.3 and light.green < 0.3


def test_settings_menu_has_no_trailing_separator(app):
    assert isinstance(app._settings_pop.get_child().get_last_child(), Gtk.Button)


@pytest.mark.parametrize('per_resource', [False, True])
def test_connection_settings_follow_theme(app, per_resource):
    owner = add_saved(app, 'a', 'a@example.com')
    app._select_account(owner.id)
    app._set_theme('dark')
    app._open_settings(owner.resources[0] if per_resource else None, owner)
    dialog = next(w for w in Gtk.Window.list_toplevels()
                  if w.get_transient_for() is app.win and w.has_css_class('avd-settings'))
    try:
        label = next(w for w in descendants(dialog)
                     if isinstance(w, Gtk.Label) and w.get_text() == 'Display scale')
        drain()
        dark = label.get_style_context().get_color()
        assert dark.red > 0.8 and dark.green > 0.8
        app._set_theme('light')
        drain()
        light = label.get_style_context().get_color()
        assert light.red < 0.3 and light.green < 0.3
    finally:
        dialog.destroy()


def test_account_name_editor_saves_and_restores_detected_name(app):
    owner = add_saved(app, 'a', 'a@example.com')
    app._select_account(owner.id)
    for value in ('Custom company', ''):
        app._edit_account_name(owner)
        dialog = next(w for w in Gtk.Window.list_toplevels()
                      if w.get_title() == 'Edit account name' and w.get_transient_for() is app.win)
        entry = dialog.get_child().get_first_child().get_next_sibling()
        entry.set_text(value)
        dialog.get_child().get_last_child().get_last_child().emit('clicked')
        assert owner.display_name == (value or 'example.com')
        text = app._account_rows.get_first_child().get_first_child().get_child()
        assert text.get_first_child().get_text() == '✓ ' + (value or 'example.com')
        assert text.get_last_child().get_text() == 'a@example.com'


def test_background_workspace_name_does_not_replace_account_domain(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    b.resources = [dict(b.resources[0], tenant='Updated company')]
    app._populate(b.resources, b)
    text = app._account_rows.get_last_child().get_first_child().get_child()
    assert text.get_first_child().get_text() == 'example.com'
    assert app.current is a


def test_header_displays_alias_and_keeps_email_in_selector(app):
    owner = add_saved(app, 'a', 'a@example.com')
    owner.profile['alias'] = 'Tessitura Network'
    app._select_account(owner.id)
    assert app._acct_who.get_text() == 'Tessitura Network'
    text = app._account_rows.get_first_child().get_first_child().get_child()
    assert text.get_last_child().get_text() == 'a@example.com'


@pytest.mark.parametrize('menu_name', ['_account_pop', '_settings_pop'])
def test_menu_closes_when_parent_window_loses_activation(app, menu_name, monkeypatch):
    app.win.present()
    drain()
    popover = getattr(app, menu_name)
    # Disable GTK's outside-click dismissal to exercise the independent
    # activation cleanup needed when a touch interaction misses dismissal.
    popover.set_autohide(False)
    popover.popup()
    drain()
    assert popover.get_visible()
    monkeypatch.setattr(app.win, 'is_active', lambda: False)
    app.win.notify('is-active')
    drain()
    assert not popover.get_visible()


@pytest.mark.parametrize('menu_name', ['_account_pop', '_settings_pop'])
def test_menu_stays_open_while_parent_window_is_active(app, menu_name, monkeypatch):
    app.win.present()
    drain()
    popover = getattr(app, menu_name)
    popover.set_autohide(False)
    popover.popup()
    drain()
    monkeypatch.setattr(app.win, 'is_active', lambda: True)
    app.win.notify('is-active')
    drain()
    assert popover.get_visible()


@pytest.mark.parametrize('menu_name', ['_account_pop', '_settings_pop'])
def test_menu_does_not_take_an_autohide_input_grab(app, menu_name):
    assert not getattr(app, menu_name).get_autohide()


@pytest.mark.parametrize('menu_name', ['_account_pop', '_settings_pop'])
def test_touch_on_main_content_dismisses_open_menu(app, menu_name):
    app.win.present()
    drain()
    popover = getattr(app, menu_name)
    popover.set_autohide(False)
    popover.popup()
    drain()
    assert popover.get_visible()
    app._on_menu_outside_pressed(None, 1, 20, app.win.get_height() - 20)
    drain()
    assert not popover.get_visible()


def test_repeated_gui_connections_preserve_each_log(app, tmp_path, monkeypatch):
    from pathlib import Path
    a = add_saved(app, 'a', 'a@example.com')
    app._select_account(a.id)
    resource = a.resources[0]
    child = app._child_for(resource['id'], a)
    output = Path(a.client.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    legacy = output / 'session_previous.log'
    legacy.write_bytes(b'previous session\n')
    rdp = tmp_path / 'desktop.rdp'
    client = tmp_path / 'fake-freerdp'
    client.write_text('#!/usr/bin/python3\n'
                      'import pathlib, sys\n'
                      'print(pathlib.Path(sys.argv[1]).read_text(), end="", flush=True)\n')
    client.chmod(0o755)
    monkeypatch.setattr(gui.config, 'SDL', str(client))
    monkeypatch.setattr(gui, 'disable_client_hotkeys', lambda: None)
    monkeypatch.setattr(app, '_ensure_token', lambda session: True)
    monkeypatch.setattr(a.client, 'download_rdp', lambda token, res: str(rdp))

    rdp.write_text('first connection\n')
    app._launch(child, resource, False, a)
    first = {p: p.read_bytes() for p in output.glob('session_*.log')}
    rdp.write_text('second connection\n')
    app._launch(child, resource, False, a)
    assert len(list(output.glob('session_*.log'))) == 3
    assert all(p.read_bytes() == content for p, content in first.items())
    assert legacy.read_bytes() == b'previous session\n'
    logs = [p.read_bytes() for p in output.glob('session_*.log') if p != legacy]
    assert any(b'first connection' in content for content in logs)
    assert any(b'second connection' in content for content in logs)
    drain()


def test_freerdp_keeps_x11_while_passing_wayland_to_security_dialog(app, tmp_path, monkeypatch):
    owner = add_saved(app, 'display-test', 'test@example.test')
    app._select_account(owner.id)
    resource = owner.resources[0]
    child = app._child_for(resource['id'], owner)
    rdp = tmp_path / 'desktop.rdp'
    rdp.write_text('full address:s:host\n')
    result = tmp_path / 'environment'
    client = tmp_path / 'fake-freerdp'
    client.write_text(
        '#!/usr/bin/python3\n'
        'import os\n'
        'from pathlib import Path\n'
        f'Path({str(result)!r}).write_text("|".join((os.getenv("SDL_VIDEODRIVER", ""), '
        'os.getenv("GDK_BACKEND", ""), os.getenv("WAYLAND_DISPLAY", ""), '
        'os.getenv("AVD_PICKER_WAYLAND_DISPLAY", ""))))\n'
    )
    client.chmod(0o755)
    monkeypatch.setenv('WAYLAND_DISPLAY', 'wayland-test')
    monkeypatch.setattr(gui.config, 'SDL', str(client))
    monkeypatch.setattr(gui, 'disable_client_hotkeys', lambda: None)
    monkeypatch.setattr(app, '_ensure_token', lambda session: True)
    monkeypatch.setattr(owner.client, 'download_rdp', lambda token, res: str(rdp))

    app._launch(child, resource, False, owner)

    assert result.read_text() == 'x11|x11||wayland-test'
    drain()


@pytest.mark.parametrize(
    ('failures', 'width', 'prompt', 'expected_launches', 'expected_errors'),
    [(2, 0, False, 3, 0), (3, 0, False, 3, 1),
     (3, 100, False, 1, 0), (3, 0, True, 1, 0)],
)
def test_zero_sized_monitor_retries_are_bounded(
        app, tmp_path, monkeypatch, failures, width, prompt, expected_launches, expected_errors):
    from avd_feed_connect.rdp.attempt import ConnectionAttempt

    owner = add_saved(app, 'a', 'a@example.com')
    app._select_account(owner.id)
    resource = owner.resources[0]
    child = app._child_for(resource['id'], owner)
    rdp = tmp_path / 'desktop.rdp'
    rdp.write_text('full address:s:host\n')
    counter = tmp_path / 'launch-count'
    client = tmp_path / 'fake-freerdp'
    client.write_text(
        '#!/usr/bin/python3\n'
        'from pathlib import Path\n'
        f'counter = Path({str(counter)!r})\n'
        'n = int(counter.read_text()) + 1 if counter.exists() else 1\n'
        'counter.write_text(str(n))\n'
        f'if n <= {failures}:\n'
        f' if {prompt}:\n'
        '  print("Browse to: https://example.invalid/")\n'
        f' print("Monitor configuration virtual desktop width must be 200 <= {width} <= 32766")\n'
        ' print("ERRCONNECT_PRE_CONNECT_FAILED")\n'
        'else:\n'
        ' print("Logon Info")\n')
    client.chmod(0o755)
    monkeypatch.setattr(gui.config, 'SDL', str(client))
    monkeypatch.setattr(gui, 'disable_client_hotkeys', lambda: None)
    monkeypatch.setattr(app, '_ensure_token', lambda session: True)
    monkeypatch.setattr(owner.client, 'download_rdp', lambda token, res: str(rdp))
    errors = []
    monkeypatch.setattr(app, '_error', errors.append)
    attempt = ConnectionAttempt()
    child._attempt = attempt
    attempt.is_current = lambda: child._attempt is attempt
    monkeypatch.setattr(attempt.cancelled, 'wait', lambda timeout: False)

    app._launch(child, resource, False, owner, attempt)

    assert int(counter.read_text()) == expected_launches
    assert len(errors) == expected_errors
    if errors:
        assert 'usable display' in str(errors[0])


def test_zero_sized_monitor_retry_respects_cancellation(app, tmp_path, monkeypatch):
    from avd_feed_connect.rdp.attempt import ConnectionAttempt

    owner = add_saved(app, 'a', 'a@example.com')
    app._select_account(owner.id)
    resource = owner.resources[0]
    child = app._child_for(resource['id'], owner)
    rdp = tmp_path / 'desktop.rdp'
    rdp.write_text('full address:s:host\n')
    client = tmp_path / 'fake-freerdp'
    client.write_text(
        '#!/usr/bin/python3\n'
        'print("Monitor configuration virtual desktop width must be 200 <= 0 <= 32766")\n'
        'print("ERRCONNECT_PRE_CONNECT_FAILED")\n')
    client.chmod(0o755)
    monkeypatch.setattr(gui.config, 'SDL', str(client))
    monkeypatch.setattr(gui, 'disable_client_hotkeys', lambda: None)
    monkeypatch.setattr(app, '_ensure_token', lambda session: True)
    launches = []
    monkeypatch.setattr(owner.client, 'download_rdp',
                        lambda token, res: (launches.append(res), str(rdp))[1])
    attempt = ConnectionAttempt()
    child._attempt = attempt
    attempt.is_current = lambda: child._attempt is attempt
    def cancel_during_retry(timeout):
        app._cancel_connection(child, resource, owner, attempt)
        return True
    monkeypatch.setattr(attempt.cancelled, 'wait', cancel_during_retry)

    app._launch(child, resource, False, owner, attempt)

    assert len(launches) == 1
    assert attempt.cancelled.is_set()


def test_workspace_placement_only_on_first_logon(app):
    from types import SimpleNamespace
    from unittest.mock import Mock
    placement = Mock()
    placement.on_connected.return_value = False
    child = SimpleNamespace(_workspace_placement=placement)
    owner = add_saved(app, 'demo', 'alex@example.test')
    resource = owner.resources[0]
    state = {'connected': False}
    app._on_freerdp_line(child, resource, -1, 'Connection failed', state, owner)
    drain()
    placement.on_connected.assert_not_called()
    for _ in range(2):
        app._on_freerdp_line(child, resource, -1, 'Logon Info', state, owner)
    drain()
    placement.on_connected.assert_called_once()


def test_fido_touch_notification_is_shown_and_cleared(app, monkeypatch):
    from types import SimpleNamespace

    owner = add_saved(app, 'demo', 'alex@example.test')
    resource = owner.resources[0]
    child = SimpleNamespace()
    state = {'connected': True}
    events = []
    monkeypatch.setattr(app, 'send_notification', lambda ident, notice: events.append(('show', ident)))
    monkeypatch.setattr(app, 'withdraw_notification', lambda ident: events.append(('clear', ident)))
    icons = []
    monkeypatch.setattr(gui.Gio.Notification, 'set_icon', lambda notice, icon: icons.append(icon.get_names()[0]))
    app._on_freerdp_line(child, resource, -1, 'AVD_FIDO_NOTIFICATION 123 touch', state, owner)
    app._on_freerdp_line(child, resource, -1, 'AVD_FIDO_NOTIFICATION 123 clear', state, owner)
    drain()
    assert events == [('show', 'fido-123'), ('clear', 'fido-123')]
    assert icons == ['dialog-password-symbolic']
    assert not state['fido_notifications']


def test_fido_error_notification_explains_the_failure(app, monkeypatch):
    from types import SimpleNamespace

    owner = add_saved(app, 'demo', 'alex@example.test')
    resource = owner.resources[0]
    state = {'connected': True}
    shown = []
    monkeypatch.setattr(app, 'send_notification',
                        lambda ident, notice: shown.append((ident, notice)))
    bodies = []
    real_set_body = gui.Gio.Notification.set_body
    monkeypatch.setattr(gui.Gio.Notification, 'set_body',
                        lambda notice, body: (bodies.append(body), real_set_body(notice, body)))
    icons = []
    monkeypatch.setattr(gui.Gio.Notification, 'set_icon',
                        lambda notice, icon: icons.append(icon.get_names()[0]))
    for line in ('AVD_FIDO_NOTIFICATION 9 error 46 login.microsoft.com',
                 'AVD_FIDO_NOTIFICATION 10 error 52 -',
                 'AVD_FIDO_NOTIFICATION 11 error -9 -',
                 'AVD_FIDO_NOTIFICATION 12 error 46 bad/site'):
        app._on_freerdp_line(SimpleNamespace(), resource, -1, line, state, owner)
    drain()
    assert [ident for ident, _ in shown] == ['fido-error'] * 3
    assert 'login.microsoft.com' in bodies[0] and 'passkey' in bodies[0]
    assert 'reinsert' in bodies[1]
    assert bodies[2] == 'The security key request failed.'
    assert icons == ['dialog-warning-symbolic'] * 3
    assert not state.get('fido_notifications')


@pytest.mark.parametrize('browser_signin', [False, True])
def test_connection_signin_respects_browser_preference(app, monkeypatch, browser_signin):
    from types import SimpleNamespace

    monkeypatch.setattr(gui.config, 'CLOUD', 'commercial')
    monkeypatch.setattr(app, '_use_browser_signin', lambda: browser_signin)
    monkeypatch.setattr(app, '_scheme_connect', lambda session: False)
    scheduled = []
    monkeypatch.setattr(gui.GLib, 'idle_add', lambda *args: scheduled.append(args))
    owner = add_saved(app, 'demo', 'alex@example.test')
    resource = owner.resources[0]
    child = SimpleNamespace()
    app._on_freerdp_line(
        child, resource, -1,
        'Browse to: https://login.microsoftonline.com/common/oauth2/authorize',
        {'connected': False}, owner,
    )
    expected = app._resolve_aad_browser if browser_signin else app._resolve_aad
    assert len(scheduled) == 1
    assert scheduled[0][0] == expected


def find_widget(widget, kind, label=None):
    child = widget.get_first_child()
    while child:
        if isinstance(child, kind) and (label is None or child.get_label() == label):
            return child
        found = find_widget(child, kind, label)
        if found:
            return found
        child = child.get_next_sibling()
    return None


def press_escape(window):
    controllers = window.observe_controllers()
    for index in range(controllers.get_n_items()):
        controller = controllers.get_item(index)
        if isinstance(controller, Gtk.ShortcutController):
            for position in range(controller.get_n_items()):
                shortcut = controller.get_item(position)
                if shortcut.get_trigger().to_string() == 'Escape':
                    return shortcut.get_action().activate(
                        Gtk.ShortcutActionFlags.EXCLUSIVE, window, None)
    pytest.fail('no Escape shortcut')


@pytest.fixture
def browser_flow(app, monkeypatch):
    for name in ('AVD_SCHEME_SIGNIN', 'AVD_BROWSER_SIGNIN', 'AVD_DEVICE_CODE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gui.config, 'CLOUD', 'commercial')
    monkeypatch.setattr(gui.Gio.AppInfo, 'launch_default_for_uri', lambda *a: None)
    return app


def test_mistaken_browser_signin_can_be_cancelled_without_changing_preference(browser_flow):
    app = browser_flow
    app._start_browser_signin()
    dialog = app._signin_dlg
    assert dialog is not None and app._adding is not None
    assert find_widget(dialog, Gtk.Button, 'Use in-app sign-in')
    find_widget(dialog, Gtk.Button, 'Cancel').emit('clicked')
    drain()
    assert app._signin_dlg is None and app._adding is None
    assert not app._settings.get('_browser_signin')
    assert not gui.storage.load_settings().get('_browser_signin')


def test_browser_signin_hands_the_same_account_back_to_in_app(browser_flow, monkeypatch):
    app = browser_flow
    app._start_browser_signin()
    pending = app._adding
    calls = []
    monkeypatch.setattr(app, '_interactive_signin',
                        lambda session, **options: calls.append((session, options)))
    find_widget(app._signin_dlg, Gtk.Button, 'Use in-app sign-in').emit('clicked')
    drain()
    assert calls == [(pending, {'browser': False})]
    assert app._adding is pending
    assert not app._settings.get('_browser_signin')


def test_browser_shortcut_is_remembered_once_signin_succeeds(browser_flow, monkeypatch):
    app = browser_flow
    monkeypatch.setattr(app, '_browser_exchange', lambda *a: None)
    app._start_browser_signin()
    entry = find_widget(app._signin_dlg, Gtk.Entry)
    entry.set_text('code-from-browser')
    entry.emit('activate')
    drain()
    assert app._signin_dlg is None
    assert gui.storage.load_settings().get('_browser_signin') is True


def test_preferred_browser_signin_offers_only_cancel(browser_flow):
    app = browser_flow
    app._settings['_browser_signin'] = True
    app._interactive_signin()
    assert find_widget(app._signin_dlg, Gtk.Button, 'Cancel')
    assert not find_widget(app._signin_dlg, Gtk.Button, 'Use in-app sign-in')


def connection_prompt(app, monkeypatch, offer_in_app):
    import threading
    from types import SimpleNamespace

    monkeypatch.setattr(app, '_scheme_connect', lambda session: False)
    cancelled = threading.Event()
    attempt = SimpleNamespace(cancelled=cancelled, cancel=cancelled.set, close_auth=None,
                              active=lambda: not cancelled.is_set())
    owner = add_saved(app, 'demo', 'alex@example.test')
    resource = owner.resources[0]
    read_fd, master = os.pipe()
    os.set_blocking(read_fd, False)
    url = 'https://login.microsoftonline.com/common/oauth2/authorize'
    app._resolve_aad_browser(master, url, resource, owner, attempt, offer_in_app)
    title = 'Sign in — ' + resource['title']
    window = next(w for w in Gtk.Window.list_toplevels() if w.get_title() == title)
    return window, attempt, (master, url, resource, owner), read_fd


def test_connection_browser_prompt_cancels_on_escape(browser_flow, monkeypatch):
    window, attempt, (master, *_), read_fd = connection_prompt(browser_flow, monkeypatch, False)
    assert not find_widget(window, Gtk.Button, 'Use in-app sign-in')
    assert press_escape(window)
    drain()
    assert attempt.cancelled.is_set() and attempt.close_auth is None
    with pytest.raises(BlockingIOError):
        os.read(read_fd, 1)
    os.close(read_fd); os.close(master)


def test_connection_browser_prompt_returns_to_in_app_signin(browser_flow, monkeypatch):
    app = browser_flow
    window, attempt, args, read_fd = connection_prompt(app, monkeypatch, True)
    scheduled = []
    monkeypatch.setattr(gui.GLib, 'idle_add', lambda *a: scheduled.append(a))
    find_widget(window, Gtk.Button, 'Use in-app sign-in').emit('clicked')
    assert scheduled == [(app._resolve_aad, *args, attempt)]
    assert not attempt.cancelled.is_set() and attempt.close_auth is None
    with pytest.raises(BlockingIOError):
        os.read(read_fd, 1)
    os.close(read_fd); os.close(args[0])


def test_scheme_capture_follows_the_browser_preference(app, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.delenv('AVD_SCHEME_SIGNIN', raising=False)
    monkeypatch.setattr(gui.config, 'CLOUD', 'commercial')
    feed = SimpleNamespace(local=False)
    monkeypatch.setattr(app, '_use_browser_signin', lambda: False)
    assert not app._scheme_connect(feed)
    monkeypatch.setattr(app, '_use_browser_signin', lambda: True)
    assert app._scheme_connect(feed)
    assert not app._scheme_connect(SimpleNamespace(local=True))


def test_gateway_bearer_is_handed_to_every_feed_account():
    from types import SimpleNamespace

    inject = gui.AvdApp._inject_bearer
    assert inject(SimpleNamespace(token='t', local=False))
    assert not inject(SimpleNamespace(token='', local=False))
    assert not inject(SimpleNamespace(token='t', local=True))


def test_connection_scheme_redirect_matches_uri_and_state(app, monkeypatch):
    delivered = []
    monkeypatch.setattr(gui.GLib, 'idle_add', lambda callback, uri: callback(uri))
    monkeypatch.setattr(app, '_toast', lambda *args, **kwargs: None)
    older = {'state': 'first', 'finish': delivered.append, 'attempt': None}
    newer = {'state': 'second', 'finish': delivered.append, 'attempt': None}
    app._conn_scheme_waiters = [older, newer]
    uri = gui.config.BROKER_REDIRECT
    assert not app._deliver_scheme_conn('ms-appx-web://attacker/path?code=x&state=first')
    assert not app._deliver_scheme_conn(uri + '?code=x&state=wrong')
    assert not app._deliver_scheme_conn(uri + '?code=x&state=first&state=second')
    assert app._deliver_scheme_conn(uri + '?code=x&state=first')
    assert delivered == [uri + '?code=x&state=first']
    assert app._conn_scheme_waiters == [newer]
    assert not app._deliver_scheme_conn(uri + '?code=x&state=first')


def test_unified_mode_preserves_selection_and_duplicate_resource_sessions(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    child = app._child_for('same', a)
    marker = object()
    child._proc = marker
    app._set_view_mode('all')
    drain()
    assert app.current is a
    assert {tile._account.id for tile in app._tiles} == {a.id, b.id}
    assert app._child_for('same', a)._proc is marker
    assert app._child_for('same', b) is not child
    app._set_view_mode('account')
    assert app._tiles == [child]
    assert gui.storage.load_settings()['_account_view'] == 'account'


def test_view_toggle_requires_two_live_accounts(app):
    a = add_saved(app, 'a', 'a@example.com')
    app._select_account(a.id)
    assert not app._view_modes.get_visible()
    b = add_saved(app, 'b', 'b@example.com')
    app._rebuild_account_menu()
    assert app._view_modes.get_visible()
    b.removed = True
    app._rebuild_account_menu()
    assert not app._view_modes.get_visible()


def test_unified_empty_and_failed_account_keep_other_resources(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    b.resources = []
    app._select_account(a.id)
    app._set_view_mode('all')
    app._show('signin', b)
    drain()
    assert app.stack.get_visible_child_name() == 'workspaces'
    assert app._tiles[0]._account is a
    assert b.client.upn in widget_text(app._resource_box)
    assert 'Sign in' in labels(app._resource_box)


def widget_text(widget):
    text = widget.get_text() if isinstance(widget, Gtk.Label) else ''
    child = widget.get_first_child()
    while child:
        text += widget_text(child)
        child = child.get_next_sibling()
    return text


def test_sign_out_clamps_unified_mode_and_keeps_remaining_account(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    app._set_view_mode('all')
    app._sign_out()
    drain()
    assert app.view_mode == 'account'
    assert app.current is b
    assert not app._view_modes.get_visible()
    assert gui.storage.load_settings()['_account_view'] == 'account'


def test_unified_alias_workspace_groups_and_refresh_are_independent(app, monkeypatch):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    a.profile['alias'] = 'Fictional North'
    a.resources += [dict(a.resources[0], id='second', tenant='Second workspace', type='RemoteApp')]
    app._select_account(a.id)
    app._set_view_mode('all')
    app._set_status('Sign in again to refresh', b)
    drain()
    text = widget_text(app._resource_box)
    assert 'Fictional North' in text
    assert 'Second workspace' in text
    assert 'b@example.com' in text
    assert 'Sign in again to refresh' in text
    assert len(app._tiles) == 3
    calls = []
    monkeypatch.setattr(app, '_interactive_signin', lambda owner: calls.append(owner.id))
    row = app._account_sections[b.id].get_first_child().get_next_sibling()
    # Activate the actual affected-account button, not the selected account.
    row.get_last_child().emit('clicked')
    assert calls == [b.id]


@pytest.mark.parametrize('count', [0, 1, 2])
def test_restart_restores_mode_and_selection_or_clamps_to_single_account(app, count):
    owners = [add_saved(app, str(i), f'person{i}@example.com') for i in range(count)]
    if owners:
        app.registry.select(owners[-1].id)
    gui.storage.save_settings({'_account_view': 'all'})
    restarted = gui.AvdApp()
    restarted.set_application_id(gui.APP_ID + "restart")
    restarted.register(None)
    try:
        restarted._build_ui()
        assert restarted.view_mode == ('all' if count > 1 else 'account')
        assert restarted.selected_id == (owners[-1].id if owners else None)
        assert restarted._view_modes.get_visible() == (count > 1)
    finally:
        restarted.win.destroy()


def test_unified_connection_status_does_not_offer_reauth_or_reload_icons(app, monkeypatch):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    app._set_view_mode('all')
    drain()
    icons = []
    monkeypatch.setattr(app, '_load_icons', lambda *args: icons.append(args))
    app._set_status('Connected to Fictional desktop', a)
    drain()
    label, button = app._account_status_widgets[a.id]
    assert label.get_text() == 'Connected to Fictional desktop'
    assert not button.get_visible()
    assert not icons
    app._set_status('Sign in again to refresh', b, reauth=True)
    drain()
    assert app._account_status_widgets[b.id][1].get_visible()
    assert not icons


def test_unified_launch_and_pending_callback_use_resource_owner(app, monkeypatch):
    from types import SimpleNamespace
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    app._set_view_mode('all')
    launched = []
    monkeypatch.setattr(gui.threading, 'Thread', lambda target, args, daemon:
                        SimpleNamespace(start=lambda: target(*args)))
    monkeypatch.setattr(app, '_launch', lambda child, resource, windowed, owner, attempt=None:
                        launched.append((child._account.id, owner.id, resource['id'], windowed)))
    app._on_tile_activated(app.grid, app._child_for('same', b), windowed=True)
    assert launched == [(b.id, b.id, 'same', True)]
    assert app.current is a
    resumed = []
    monkeypatch.setattr(app, '_on_tile_activated', lambda grid, child, windowed:
                        resumed.append((child._account.id, windowed)))
    b.pending_launch = ('same', True)
    app._populate(b.resources, b)
    assert resumed == [(b.id, True)]
    assert b.pending_launch is None


def test_unified_headings_and_mode_buttons_follow_theme(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    app._set_view_mode('all')
    app.win.present()
    drain()
    heading = app._account_sections[a.id].get_first_child()
    for theme, dark in [('dark', True), ('light', False)]:
        app._set_theme(theme)
        drain()
        color = heading.get_style_context().get_color()
        assert (color.red > .8) if dark else (color.red < .3)
        assert app._view_buttons['all'].get_active()
        assert not app._view_buttons['account'].get_active()


def test_renaming_other_unified_account_keeps_selected_header_identity(app, monkeypatch):
    a = add_saved(app, 'a', 'alice.north@example.com')
    b = add_saved(app, 'b', 'ben.south@example.com')
    app._select_account(a.id)
    app._set_view_mode('all')
    assert app._avatar_lbl.get_text() == 'AN'
    updates = []
    update = app._update_account
    monkeypatch.setattr(app, '_update_account', lambda who, *rest: (updates.append(who), update(who, *rest)))
    app._edit_account_name(b)
    dialog = next(w for w in Gtk.Window.list_toplevels()
                  if w.get_title() == 'Edit account name' and w.get_transient_for() is app.win)
    entry = dialog.get_child().get_first_child().get_next_sibling()
    entry.set_text('Fictional South')
    dialog.get_child().get_last_child().get_last_child().emit('clicked')
    assert b.display_name == 'Fictional South'
    assert updates and all(who == a.client.upn for who in updates)
    assert app._avatar_lbl.get_text() == 'AN'
    assert app._acct_who.get_text() == a.display_name


def test_unified_refresh_only_rebuilds_changed_owner_and_fetches_its_icons(app, monkeypatch):
    from types import SimpleNamespace
    owners = [add_saved(app, str(i), f'person{i}@example.com') for i in range(3)]
    monkeypatch.setattr(gui.threading, 'Thread', lambda target, args, daemon:
                        SimpleNamespace(start=lambda: target(*args)))
    app._select_account(owners[0].id)
    app._set_view_mode('all')
    icons = []
    monkeypatch.setattr(app, '_load_icons', lambda resources, owner, tiles: icons.append(owner.id))
    untouched_parent = app._child_for('same', owners[1]).get_parent()
    changed = [dict(owners[0].resources[0], title='Updated resource')]
    app._populate(changed, owners[0])
    assert app._child_for('same', owners[1]).get_parent() is untouched_parent
    assert icons == [owners[0].id]
    icons.clear()
    section = app._account_sections[owners[0].id]
    app._set_status('Sign in again to refresh', owners[0], reauth=True)
    drain()
    app._populate(changed, owners[0])
    assert not app._account_status_widgets[owners[0].id][1].get_visible()
    assert app._account_sections[owners[0].id] is section
    assert app._resource_box.get_first_child() is section
    assert section.get_next_sibling() is app._account_sections[owners[1].id]
    assert not icons
    app._set_view_mode('account')
    app._set_view_mode('all')
    assert not icons


def test_icon_download_reaches_owner_tile_created_while_download_is_pending(app, monkeypatch):
    import threading
    from gi.repository import Gdk
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    b.resources[0]['icon32'] = 'https://icons.example/fictional.png'
    b.token = 'fictional-token'
    started, finish = threading.Event(), threading.Event()
    png = Gdk.MemoryTexture.new(1, 1, Gdk.MemoryFormat.R8G8B8A8,
                               GLib.Bytes.new(bytes([255, 0, 0, 255])), 4).save_to_png_bytes().get_data()

    def download(url, token):
        assert token == b.token
        started.set()
        assert finish.wait(3)
        return png

    monkeypatch.setattr(gui.storage, 'bearer_bytes', download)
    worker = threading.Thread(target=gui.AvdApp._load_icons, args=(app, b.resources, b, []))
    worker.start()
    try:
        assert started.wait(3)
        assert not b.tiles
        app._set_view_mode('all')
        child = app._child_for('same', b)
        assert child._img.get_storage_type() == Gtk.ImageType.ICON_NAME
    finally:
        finish.set()
        worker.join(3)
    assert not worker.is_alive()
    drain()
    assert child._img.get_storage_type() == Gtk.ImageType.PAINTABLE
    assert app._child_for('same', a)._img.get_storage_type() == Gtk.ImageType.ICON_NAME


def test_unchanged_feed_retries_missing_icon_after_transient_failure(app, monkeypatch):
    from types import SimpleNamespace
    from gi.repository import Gdk
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    b.resources[0]['icon32'] = 'https://icons.example/fictional.png'
    b.resources.append(dict(b.resources[0], id='cached', icon32='https://icons.example/cached.png'))
    b.token = 'fictional-b-token'
    png = Gdk.MemoryTexture.new(1, 1, Gdk.MemoryFormat.R8G8B8A8,
                               GLib.Bytes.new(bytes([255, 0, 0, 255])), 4).save_to_png_bytes().get_data()
    calls = []

    def download(url, token):
        calls.append((url, token))
        if len(calls) == 1:
            raise OSError('Temporary fictional network failure')
        return png

    monkeypatch.setattr(gui.storage, 'bearer_bytes', download)
    monkeypatch.setattr(app, '_load_icons', gui.AvdApp._load_icons.__get__(app))
    monkeypatch.setattr(gui.threading, 'Thread', lambda target, args, daemon:
                        SimpleNamespace(start=lambda: target(*args)))
    app._populate(b.resources, b)
    initial = [(b.resources[0]['icon32'], b.token), (b.resources[1]['icon32'], b.token)]
    assert calls == initial
    assert not os.path.exists(gui.storage.icon_path('same', b.directory))
    app._populate(b.resources, b)
    assert calls == initial + [(b.resources[0]['icon32'], b.token)]
    assert os.path.exists(gui.storage.icon_path('same', b.directory))
    app._populate(b.resources, b)
    app._populate(a.resources, a)
    assert calls == initial + [(b.resources[0]['icon32'], b.token)]


@pytest.mark.parametrize('empty', [False, True])
def test_unchanged_account_refresh_restores_page_and_clears_stale_status(app, monkeypatch, empty):
    owner = add_saved(app, 'a', 'alice@example.com')
    if empty:
        owner.resources = []
    app._select_account(owner.id)
    app._populate(owner.resources, owner)
    section = app._account_sections[owner.id]
    icons = []
    monkeypatch.setattr(app, '_load_icons', lambda *args: icons.append(args))
    app._set_status('Could not refresh — showing saved resources', owner, reauth=True)
    app._show('busy', owner)
    drain()
    assert app.stack.get_visible_child_name() == 'busy'
    app._populate(owner.resources, owner)
    assert app.stack.get_visible_child_name() == 'workspaces'
    assert app.status.get_text() == 'Signed in as alice@example.com'
    assert not owner.needs_signin
    assert app._account_sections[owner.id] is section
    assert not icons


def test_language_selection_applies_live_and_preserves_sessions(app):
    from avd_feed_connect import i18n
    def descendants(widget):
        yield widget
        child = widget.get_first_child()
        while child:
            yield from descendants(child)
            child = child.get_next_sibling()
    owner = add_saved(app, 'fictional', 'alex@example.test')
    app._select_account(owner.id)
    owner.resources = [{'id': 'desk', 'title': 'Save', 'tenant': 'Northstar', 'type': 'Desktop'}]
    app._populate(owner.resources, owner)
    tile = owner.tiles['desk']
    from types import SimpleNamespace
    process = SimpleNamespace(poll=lambda: None)
    tile._proc = process
    tray = SimpleNamespace()
    tray.set_menu = lambda menu: setattr(tray, "menu", menu)
    tray.set_tooltip = lambda tooltip: setattr(tray, "tooltip", tooltip)
    app._tray = tray
    app._sessions[(owner.id, "desk")] = "connected"
    app._set_status(i18n._('Connected to {resource}').format(resource='Save'), owner)
    app._open_settings(None)
    dialog = next(w for w in Gtk.Window.get_toplevels()
                  if w.get_title() == 'Default connection settings')
    dropdown = next(w for w in descendants(dialog) if isinstance(w, Gtk.DropDown)
                    and w.get_model().get_string(0) == 'System default')
    connection_dropdown = next(w for w in descendants(dialog) if isinstance(w, Gtk.DropDown)
                               and w is not dropdown)
    connection_dropdown.set_selected(3)
    unsaved_flags = next(w for w in descendants(dialog) if isinstance(w, Gtk.Entry)
                         and w.get_placeholder_text().startswith("advanced:"))
    unsaved_flags.set_text("/gfx /network:auto")
    app._edit_account_name(owner)
    alias_dialog = next(w for w in Gtk.Window.get_toplevels() if w.get_title() == "Edit account name")
    alias_entry = next(w for w in descendants(alias_dialog) if isinstance(w, Gtk.Entry))
    alias_entry.set_text("Unsaved company name")
    for selection, language, expected in [(2, 'es-419', 'Conectado a Save'),
                                           (3, 'es-ES', 'Conectado a Save'),
                                           (4, 'es-AR', 'Conectado a Save'),
                                           (1, 'en', 'Connected to Save')]:
        dropdown.set_selected(selection)
        drain()
        assert gui.storage.load_settings()['_language'] == language
        assert app.status.get_text() == expected
        assert owner.tiles['desk'] is tile and tile._proc is process
        assert app.current is owner
        assert tray.menu[-1]['label'] == ('Quit' if language == 'en' else 'Salir')
        assert str(tray.tooltip) == ('1 session connected' if language == 'en' else '1 sesión conectada')
        assert app._sessions[(owner.id, 'desk')] == 'connected'
        assert connection_dropdown.get_selected() == 3
        assert unsaved_flags.get_text() == '/gfx /network:auto'
        assert alias_entry.get_text() == 'Unsaved company name'
        assert alias_dialog.get_title() == ('Edit account name' if language == 'en' else 'Editar nombre de la cuenta')
        assert any(isinstance(w, Gtk.Label) and w.get_text() == 'Save'
                   for w in descendants(tile))  # resource title must never be translated
        assert dialog.get_title() == ('Default connection settings' if language == 'en'
                                      else 'Configuración de conexión predeterminada')
        assert any(isinstance(w, Gtk.Button) and w.get_label() ==
                   ('Save' if language == 'en' else 'Guardar') for w in descendants(dialog))
    alias_dialog.destroy()
    dialog.destroy()
    tile._proc = None
    app._tray = None


def test_tile_state_labels_are_derived_from_state_and_translate_live(app):
    owner = add_saved(app, 'demo', 'alex@example.test')
    app._select_account(owner.id)
    tile = owner.tiles['same']
    app._set_tile_state('same', 'state-connecting', owner)
    drain()
    assert tile._pill.get_visible() and tile._pill.get_text() == 'CONNECTING'
    app._set_language('es-AR')
    assert tile._pill.get_text() == 'CONECTANDO'
    app._set_tile_state('same', 'state-connected', owner)
    drain()
    assert tile._pill.get_text() == 'CONECTADO'
    assert tile._tile.has_css_class('tile-connected')
    assert app._sessions[(owner.id, 'same')] == 'connected'
    app._set_tile_state('same', 'state-ended', owner)
    drain()
    assert not tile._pill.get_visible()
    assert not tile._tile.has_css_class('tile-connected')
    assert (owner.id, 'same') not in app._sessions
    app._set_language('en')


def test_localization_registrations_follow_native_lifetime_without_language_refresh():
    import gc
    from avd_feed_connect.gui import localization
    from avd_feed_connect.i18n import _
    gc.collect()
    registries = (localization._widgets, localization._models, localization._dropdowns)
    baseline = tuple(map(len, registries))
    parent = Gtk.Box()
    label = localization.create(Gtk.Label, label=_('Save'))
    dropdown = localization.create(Gtk.DropDown,
                                  model=localization.strings([_('Cancel'), _('Save')]))
    parent.append(label)
    parent.append(dropdown)
    del label, dropdown
    gc.collect()
    assert tuple(map(len, registries)) == tuple(count + 1 for count in baseline)
    # Native ownership, rather than the Python wrapper, retains each binding.
    assert parent.get_first_child()._translation_bindings['label'].source == 'Save'
    while child := parent.get_first_child():
        parent.remove(child)
    del child, parent
    gc.collect()
    # No configure/refresh call is needed to remove dead registrations.
    assert tuple(map(len, registries)) == baseline


def descendants(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from descendants(child)
        child = child.get_next_sibling()


def test_authentication_setting_roundtrip_and_inheritance(app):
    owner = add_saved(app, 'a', 'test@example.test')
    res = owner.resources[0]
    app._settings['_default'] = {'sec': 'aad'}
    assert app._eff('sec', res, owner) == 'aad'
    app._open_settings(res, owner)
    drain()
    dialog = next(w for w in Gtk.Window.list_toplevels() if w.get_title().startswith('Settings —'))
    dropdown = next(w for w in descendants(dialog) if isinstance(w, Gtk.DropDown)
                    and w.get_model().get_string(0) == 'Automatic (use feed or remembered mode)')
    dropdown.set_selected(2)
    next(w for w in descendants(dialog) if isinstance(w, Gtk.Button) and w.get_label() == 'Save').emit('clicked')
    assert owner.resource_settings[res['id']]['sec'] == 'nla'


def test_authentication_remembered_only_after_logon_and_account_scoped(app):
    from types import SimpleNamespace
    from avd_feed_connect.rdp.security import SecurityDecision
    a,b = add_saved(app, 'a', 'a@example.test'), add_saved(app, 'b', 'b@example.test')
    res = a.resources[0]
    child = SimpleNamespace(_security_decision=SecurityDecision('aad','connection setting',[],'fingerprint'))
    state = {'connected':False}
    app._on_freerdp_line(child,res,-1,'ERRCONNECT_LOGON_FAILURE',state,a)
    drain()
    assert res['id'] not in a.auth_modes
    app._on_freerdp_line(child,res,-1,'Logon Info',state,a)
    drain()
    assert a.auth_modes[res['id']] == {'mode':'aad','fingerprint':'fingerprint'}
    assert res['id'] not in b.auth_modes


@pytest.mark.parametrize('override', ['aad', 'nla'])
def test_gui_launch_passes_only_one_selected_security_mode(app, tmp_path, monkeypatch, override):
    from pathlib import Path
    owner = add_saved(app, 'auth-test', 'test@example.test')
    app._select_account(owner.id)
    res = owner.resources[0]; child = app._child_for(res['id'], owner)
    Path(owner.client.output_dir).mkdir(parents=True, exist_ok=True)
    path = tmp_path / 'desktop.rdp'; path.write_text('enablerdsaadauth:i:0\n')
    fake = tmp_path / 'client'
    fake.write_text('#!/usr/bin/python3\nimport sys\nprint(" ".join(sys.argv[1:]), flush=True)\n')
    fake.chmod(0o755)
    monkeypatch.setattr(gui.config, 'SDL', str(fake))
    monkeypatch.setattr(app, '_ensure_token', lambda session: True)
    monkeypatch.setattr(owner.client, 'download_rdp', lambda *args: str(path))
    monkeypatch.setenv('AVD_SEC', override)
    owner.resource_settings[res['id']] = {'extra_args': '/sec:nla /gfx'}
    app._launch(child,res,False,owner)
    logs=list(Path(owner.client.output_dir).glob('session_*.log'))
    assert len(logs)==1
    text=logs[0].read_text()
    assert f'[AVD auth] mode={override}; reason=AVD_SEC override' in text
    assert text.count('/sec:')==1
    assert f'/sec:{override}' in text


def test_cancel_pending_connection_allows_retry_without_touching_connected_session(app):
    from avd_feed_connect.rdp.attempt import ConnectionAttempt
    owner = add_saved(app, 'cancel', 'test@example.test')
    app._select_account(owner.id)
    res = owner.resources[0]; child = app._child_for(res['id'], owner)
    attempt = ConnectionAttempt();child._attempt=attempt;child._launching=True
    attempt.is_current = lambda: child._attempt is attempt
    app._set_connecting(res['id'],res['title'],True,owner)
    other = add_saved(app, 'connected', 'other@example.test')
    app._set_tile_state(other.resources[0]['id'],'state-connected',other)
    drain()
    app._cancel_connection(child,res,owner,attempt)
    drain()
    assert attempt.cancelled.is_set()
    assert not child._launching
    assert not app._connect_overlay.get_visible()
    assert app._sessions[(other.id,other.resources[0]['id'])]=='connected'
    assert child._proc is None


def test_cancelled_attempt_cannot_restore_overlay_or_connected_state(app):
    from avd_feed_connect.rdp.attempt import ConnectionAttempt
    owner=add_saved(app,'cancel','test@example.test');app._select_account(owner.id)
    res=owner.resources[0];child=app._child_for(res['id'],owner)
    old=ConnectionAttempt();child._attempt=old;old.is_current=lambda: child._attempt is old
    app._set_connecting(res['id'],res['title'],True,owner,attempt=old)
    old.cancel()
    new=ConnectionAttempt();child._attempt=new;new.is_current=lambda: child._attempt is new
    app._set_tile_state(res['id'],'state-connecting',owner,attempt=new)
    app._set_tile_state(res['id'],'state-connected',owner,attempt=old)
    drain()
    assert app._sessions[(owner.id,res['id'])]=='connecting'
    assert (owner.id,res['id']) not in app._connecting


def test_cancelled_launch_never_downloads_or_starts_client(app, monkeypatch):
    from avd_feed_connect.rdp.attempt import ConnectionAttempt
    owner=add_saved(app,'cancel','test@example.test');app._select_account(owner.id)
    res=owner.resources[0];child=app._child_for(res['id'],owner)
    attempt=ConnectionAttempt();attempt.cancel()
    monkeypatch.setattr(app,'_ensure_token',lambda *_: pytest.fail('cancelled launch must not authenticate'))
    app._launch(child,res,False,owner,attempt)


def test_rdp_download_401_refreshes_token_and_feed_url(app, monkeypatch):
    from types import SimpleNamespace
    from avd_feed_connect.rdp.resources import RdpDownloadError

    owner = add_saved(app, 'reauth', 'test@example.test')
    resource = dict(owner.resources[0], rdp_url='https://example.microsoft.com/old')
    owner.resources = [resource]
    owner.token = 'expired'
    owner.deadline = 1
    child = SimpleNamespace(_res=resource)
    attempt = SimpleNamespace(active=lambda: True, is_current=lambda: True)
    calls = []

    def download(token, res):
        calls.append((token, res['rdp_url']))
        if len(calls) < 3:
            raise RdpDownloadError(401, '')
        return '/tmp/fresh.rdp'

    fresh = dict(resource, rdp_url='https://example.microsoft.com/current')
    monkeypatch.setattr(owner.client, 'download_rdp', download)
    monkeypatch.setattr(owner.client, 'enumerate_feed', lambda token: [fresh])

    def refresh(session):
        session.token = 'fresh-token'
        return True

    monkeypatch.setattr(app, '_ensure_token', refresh)
    path = app._download_resource_rdp(child, resource, owner, False, attempt)

    assert path == '/tmp/fresh.rdp'
    assert calls == [
        ('expired', 'https://example.microsoft.com/old'),
        ('fresh-token', 'https://example.microsoft.com/old'),
        ('fresh-token', 'https://example.microsoft.com/current'),
    ]
    assert child._res['rdp_url'] == 'https://example.microsoft.com/current'


def test_closing_connection_login_cancels_immediately(app, monkeypatch):
    from avd_feed_connect.rdp.attempt import ConnectionAttempt
    owner=add_saved(app,'close-auth','test@example.test');app._select_account(owner.id)
    res=owner.resources[0];child=app._child_for(res['id'],owner)
    attempt=ConnectionAttempt();child._attempt=attempt;child._launching=True
    attempt.is_current=lambda: child._attempt is attempt
    # Exercise real GTK close-request without any navigation or remote service.
    monkeypatch.setattr(gui.WebKit.WebView, 'load_uri', lambda *_: None)
    app._set_connecting(res['id'],res['title'],True,owner,attempt=attempt)
    app._resolve_aad(-1,'https://login.microsoftonline.com/common',res,owner,attempt)
    window=next(w for w in Gtk.Window.list_toplevels() if w.get_title() == 'Sign in — '+res['title'])
    window.emit('close-request')
    drain()
    assert attempt.cancelled.is_set()
    assert attempt.close_auth is None
    assert not child._launching
    assert not app._connect_overlay.get_visible()


def test_overlay_close_button_cancels_pending_attempt(app):
    from avd_feed_connect.rdp.attempt import ConnectionAttempt
    owner=add_saved(app,'button','test@example.test');app._select_account(owner.id)
    res=owner.resources[0];child=app._child_for(res['id'],owner)
    attempt=ConnectionAttempt();child._attempt=attempt;child._launching=True
    attempt.is_current=lambda: child._attempt is attempt
    app._set_connecting(res['id'],res['title'],True,owner,attempt=attempt)
    drain()
    assert app._connect_overlay.get_can_target()
    button=next(w for w in descendants(app._connect_overlay) if isinstance(w,Gtk.Button))
    button.emit('clicked');drain()
    assert attempt.cancelled.is_set()
    assert not app._connect_overlay.get_visible()


def test_tile_actions_follow_session_state_and_reconnect_relaunches(app, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from gi.repository import Gdk
    owner = add_saved(app, 'demo', 'alex@example.test')
    app._select_account(owner.id)
    drain()
    tile = owner.tiles['same']

    def menu_labels():
        pop = app._show_resource_menu(tile)
        result = labels(pop.get_child())
        pop.popdown()
        drain()
        return result

    assert menu_labels() == ['Connect', 'Connect in a window', 'Settings…']
    assert app._on_tile_key(None, Gdk.KEY_F10, 0, Gdk.ModifierType.SHIFT_MASK, tile)
    assert tile._menu.get_visible()
    tile._menu.popdown()
    drain()
    assert not tile._actions.get_visible()

    proc = SimpleNamespace(pid=4242, poll=lambda: None)
    tile._proc = proc
    app._set_tile_state('same', 'state-connected', owner)
    drain()
    assert tile._actions.get_visible()
    assert menu_labels() == ['Show window', 'Reconnect', 'Disconnect', 'Settings…']

    signals, launches = [], []
    monkeypatch.setattr(gui.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(gui.GLib, 'timeout_add_seconds', lambda *a: 0)
    monkeypatch.setattr(app, '_on_tile_activated',
                        lambda grid, child, windowed=False: launches.append((child, windowed)))
    tile._windowed = True
    buttons = []
    button = tile._actions.get_first_child()
    while button:
        buttons.append(button)
        button = button.get_next_sibling()
    buttons[1].emit('clicked')            # Reconnect
    assert signals == [(4242, gui.signal.SIGTERM)]

    read_end, write_end = os.pipe()
    os.close(write_end)                   # FreeRDP exited: the pty reaches EOF
    tile._launching = True
    app._watch_session(tile, tile._res, proc, read_end, str(tmp_path / 'session.log'), owner)
    drain()
    assert launches == [(tile, True)]
    assert not tile._reconnect

    buttons[2].emit('clicked')            # Disconnect ends without relaunching
    read_end, write_end = os.pipe()
    os.close(write_end)
    app._watch_session(tile, tile._res, proc, read_end, str(tmp_path / 'second.log'), owner)
    drain()
    assert launches == [(tile, True)]
    assert signals[-1] == (4242, gui.signal.SIGTERM)
    tile._proc = None


def test_failed_tile_state_and_slash_filter(app):
    owner = add_saved(app, 'demo', 'alex@example.test')
    owner.resources.append({'id': 'other', 'title': 'Build box', 'tenant': 'demo', 'type': 'Desktop'})
    app._select_account(owner.id)
    drain()
    tile = owner.tiles['same']
    app._set_tile_state('same', 'state-error', owner)
    drain()
    assert tile._pill.get_visible() and tile._pill.get_text() == 'FAILED'
    assert tile._tile.has_css_class('tile-error') and (owner.id, 'same') not in app._sessions
    app._set_tile_state('same', 'state-connecting', owner)
    drain()
    assert not tile._tile.has_css_class('tile-error') and not tile._pill.has_css_class('pill-error')
    assert app._on_filter_shortcut() and app._filter.get_visible()
    app._filter.set_text('BUILD')
    app._apply_filter()
    assert owner.tiles['other'].get_child_visible() and not tile.get_child_visible()
    assert app._count_lbl.get_text() == '1 of 2'
    app._close_filter()
    assert app._filter.get_text() == '' and tile.get_child_visible()


def test_collapsed_sections_persist_and_search_temporarily_expands(app):
    owner = add_saved(app, 'demo', 'alex@example.test')
    owner.resources = [
        {'id': 'pc', 'title': 'Dev Box', 'tenant': 'devcenter-one / Project', 'type': 'Desktop'},
        {'id': 'word', 'title': 'Word', 'tenant': 'Office', 'type': 'RemoteApp'},
        {'id': 'excel', 'title': 'Excel', 'tenant': 'Office', 'type': 'RemoteApp'},
    ]
    app._select_account(owner.id)
    apps = app._kind_boxes[-1]
    workspace = apps._groups[0]
    workspace._toggle.set_active(False)
    apps._toggle.set_active(False)
    assert not apps._toggle._content.get_visible()
    assert not workspace._toggle._content.get_visible()
    app._render_resources()
    apps = app._kind_boxes[-1]
    workspace = apps._groups[0]
    assert not apps._toggle.get_active() and not workspace._toggle.get_active()
    app._filter.set_text('Word')
    app._apply_filter()
    assert apps._toggle.get_active() and workspace._toggle.get_active()
    assert owner.tiles['word'].get_child_visible() and not owner.tiles['excel'].get_child_visible()
    app._filter.set_text('')
    app._apply_filter()
    assert not apps._toggle.get_active() and not workspace._toggle.get_active()
    assert app._settings['_collapsed_sections'][owner.id]['workspace:Office']


def test_arrow_navigation_crosses_sections_and_skips_collapsed(app, monkeypatch):
    owner = add_saved(app, 'demo', 'alex@example.test')
    activated = []
    monkeypatch.setattr(app, '_on_tile_activated', lambda _grid, child: activated.append(child))
    owner.resources = [
        {'id': 'pc', 'title': 'Dev Box', 'tenant': 'devcenter-one / Project', 'type': 'Desktop'},
        {'id': 'word', 'title': 'Word', 'tenant': 'Office', 'type': 'RemoteApp'},
        {'id': 'excel', 'title': 'Excel', 'tenant': 'Office', 'type': 'RemoteApp'},
    ]
    app._select_account(owner.id)
    app.win.present()
    drain()
    pc, word, excel = (owner.tiles[key] for key in ('pc', 'word', 'excel'))
    pc.grab_focus()
    assert app._on_tile_key(None, gui.Gdk.KEY_Down, 0, 0, pc)
    assert app.win.get_focus() is word
    assert app._on_tile_key(None, gui.Gdk.KEY_Right, 0, 0, word)
    assert app.win.get_focus() is excel
    excel.activate()
    assert activated == [excel]
    app._kind_boxes[-1]._toggle.set_active(False)
    drain()
    assert not app._on_tile_key(None, gui.Gdk.KEY_Down, 0, 0, pc)
    assert app._on_shortcuts_shortcut()
    assert app._shortcuts_button.get_popover().get_visible()


def test_signin_header_has_themed_title_and_browser_action(app):
    window = Gtk.Window(title='Sign in — Dev Box')
    browser = app._signin_header(window)
    called = []
    browser.connect('clicked', lambda *_: called.append(True))
    browser.emit('clicked')
    assert window.has_css_class('avd-signin')
    assert window.get_titlebar().has_css_class('avd-header')
    assert window.get_titlebar().get_title_widget().get_label() == 'Sign in — Dev Box'
    assert called == [True]
    window.destroy()


def test_omarchy_theme_change_restyles_live(app, monkeypatch):
    tokens = {'dark': False, 'background': '#eff1f5', 'foreground': '#4c4f69', 'accent': '#1e66f5',
              'border': '#1e66f5', 'menu_border': '#1e66f5', 'selected_text': '#1e66f5',
              'error': '#d20f39', 'ok': '#40a02b', 'warn': '#df8e1d', 'normal_fill': 0.04,
              'normal_border': 0.4, 'hover_fill': 0.08, 'hover_border': 0.25, 'selected_fill': 0.18,
              'pressed_fill': 0.22, 'family': 'Test Mono', 'size': 12.0, 'radius': 0}
    monkeypatch.setattr(gui.omarchy, 'load', lambda: tokens)
    app._on_omarchy_changed()
    assert app._omarchy is tokens and not app._is_dark()
    assert 'Test Mono' in app._css_provider.to_string()


def _settings_dialog(app, owner):
    app._open_settings(owner.resources[0], owner)
    drain()
    dialog = next(w for w in Gtk.Window.list_toplevels() if w.get_title().startswith('Settings —'))
    scale = next(w for w in descendants(dialog) if isinstance(w, Gtk.DropDown)
                 and w.get_model().get_string(0) == 'Automatic (match display)')
    return dialog, scale


def test_settings_dialog_escape_cancels_without_saving(app):
    owner = add_saved(app, 'a', 'a@example.com')
    app._select_account(owner.id)
    dialog, scale = _settings_dialog(app, owner)
    scale.set_selected(2)
    keys = [c for c in dialog.observe_controllers() if isinstance(c, Gtk.EventControllerKey)]
    from gi.repository import Gdk
    assert any(c.emit('key-pressed', Gdk.KEY_Escape, 9, 0) for c in keys)
    drain()
    assert dialog not in Gtk.Window.list_toplevels()
    assert owner.resources[0]['id'] not in owner.resource_settings
    assert not app._open_light_dialogs()


def test_settings_dialog_closes_on_click_outside_and_swallows_the_click(app, monkeypatch):
    owner = add_saved(app, 'a', 'a@example.com')
    app._select_account(owner.id)
    dialog, scale = _settings_dialog(app, owner)
    scale.set_selected(2)
    launched = []
    monkeypatch.setattr(app, '_on_tile_activated', lambda *a: launched.append(a))
    presses = [c for c in app.win.observe_controllers() if isinstance(c, Gtk.GestureClick)
               and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE]
    for press in presses:
        press.emit('pressed', 1, 40, 200)
    drain()
    assert dialog not in Gtk.Window.list_toplevels()
    assert owner.resources[0]['id'] not in owner.resource_settings
    assert not launched
    # With no dialog open, presses on the main window are left alone.
    for press in presses:
        press.emit('pressed', 1, 40, 200)
    assert not app._open_light_dialogs()


def test_settings_dialog_enter_in_text_field_saves(app):
    owner = add_saved(app, 'a', 'a@example.com')
    app._select_account(owner.id)
    dialog, scale = _settings_dialog(app, owner)
    # Initial focus is the first setting, so Enter never lands on Cancel.
    assert dialog.get_focus() is scale or scale.is_ancestor(dialog.get_focus()) or dialog.get_focus().is_ancestor(scale)
    flags = next(w for w in descendants(dialog) if isinstance(w, Gtk.Entry)
                 and w.get_placeholder_text().startswith('advanced:'))
    flags.grab_focus()
    flags.set_text('/bpp:16')
    assert flags.get_activates_default() and dialog.get_default_widget().get_label() == 'Save'
    assert flags.get_delegate().activate_action('default.activate', None)
    import time
    deadline = time.monotonic() + 2  # GtkButton.activate emits clicked after a short flash
    while dialog in Gtk.Window.list_toplevels() and time.monotonic() < deadline:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.01)
    assert dialog not in Gtk.Window.list_toplevels()
    assert owner.resource_settings[owner.resources[0]['id']]['extra_args'] == '/bpp:16'


def test_redirect_error_reads_entra_error_without_code():
    uri = ("https://login.microsoftonline.com/common/oauth2/nativeclient?error=access_denied"
           "&error_description=AADSTS53003%3a+Access+has+been+blocked.%0d%0aTrace+ID%3a+x")
    assert gui._redirect_error(uri) == ("access_denied", "AADSTS53003: Access has been blocked.")
    assert gui._redirect_error(
        "https://login.microsoftonline.com/common/oauth2/nativeclient?error=login_required") \
        == ("login_required", "login_required")
