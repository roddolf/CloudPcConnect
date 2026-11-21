"""WebAuthn shim and bridge, exercised through a real WebKit WebView and a fake key."""
import base64
import json

import pytest

pytest.importorskip('gi')
import gi
try:
    gi.require_version('Gtk', '4.0')
    gi.require_version('WebKit', '6.0')
except ValueError:
    pytest.skip('GTK/WebKit typelibs are unavailable', allow_module_level=True)
from gi.repository import Gio, GLib, Gtk, WebKit
if not Gtk.init_check():
    pytest.skip('GTK display is unavailable', allow_module_level=True)

from avd_feed_connect.auth import fido
from avd_feed_connect.gui import webauthn


@pytest.fixture(scope="module", autouse=True)
def application():
    # WebKit's sandboxed web process needs a registered application id.
    app = Gtk.Application(application_id="io.github.shakeelosmani.avd_feed_connect.Test",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    yield app


class FakePanel:
    def __init__(self, pin="1234", account=0):
        self.states = []
        self.pin = pin
        self.account = account
        self.prompts = []
        self.cancel = None

    def connect_cancel(self, callback):
        self.cancel = callback

    def set_state(self, state, message=None):
        self.states.append((state, message))

    def ask_pin(self, prompt, callback, error=None):
        self.prompts.append(("pin", error))
        GLib.idle_add(callback, self.pin)

    def choose_account(self, accounts, callback):
        self.prompts.append(("accounts", accounts))
        GLib.idle_add(callback, self.account)


def run_loop_until(condition, seconds=20):
    context = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + seconds * 1_000_000
    while not condition() and GLib.get_monotonic_time() < deadline:
        context.iteration(False)
        GLib.usleep(2000)
    return condition()


def make_view(base_uri, panel, monkeypatch, backend=None, library=object(), on_request=None):
    monkeypatch.setattr(fido, "load_library", lambda: library)
    if backend is not None:
        monkeypatch.setattr(fido, "get_assertion", backend)
    view = WebKit.WebView(network_session=WebKit.NetworkSession.new_ephemeral())
    bridge = webauthn.WebAuthnBridge(view, panel, library=library, on_request=on_request).attach()
    loaded = []
    view.connect("load-changed", lambda _v, event: loaded.append(event)
                 if event == WebKit.LoadEvent.FINISHED else None)
    view.load_html("<!doctype html><title>t</title>", base_uri)
    assert run_loop_until(lambda: loaded)
    return view, bridge


def call_js(view, body):
    outcome = []
    view.call_async_javascript_function(
        "try { " + body + " } catch (e) { return 'ERR ' + e.name + ': ' + e.message; }",
        -1, None, None, None, None,
        lambda v, result: outcome.append(v.call_async_javascript_function_finish(result).to_string()))
    assert run_loop_until(lambda: outcome)
    return outcome[0]


GET = """
const b = (a) => new Uint8Array(a).buffer;
const cred = await navigator.credentials.get({publicKey: {
  rpId: 'login.microsoft.com', challenge: b(Array.from({length: 32}, (_, i) => i)),
  allowCredentials: [{type: 'public-key', id: b([1, 2, 3])}], userVerification: 'required'}});
const dec = new TextDecoder();
return JSON.stringify({id: cred.id, type: cred.type, attach: cred.authenticatorAttachment,
  raw: Array.from(new Uint8Array(cred.rawId)),
  auth: Array.from(new Uint8Array(cred.response.authenticatorData)),
  sig: Array.from(new Uint8Array(cred.response.signature)),
  user: cred.response.userHandle && Array.from(new Uint8Array(cred.response.userHandle)),
  client: JSON.parse(dec.decode(cred.response.clientDataJSON)),
  isPKC: cred instanceof PublicKeyCredential,
  isAssertion: cred.response instanceof AuthenticatorAssertionResponse});
"""


def test_shim_is_only_injected_on_microsoft_sign_in_pages(monkeypatch):
    view, _ = make_view("https://login.microsoftonline.com/", FakePanel(), monkeypatch)
    assert call_js(view, "return typeof navigator.credentials + typeof PublicKeyCredential;") \
        == "objectfunction"
    other, _ = make_view("https://evil.example/", FakePanel(), monkeypatch)
    assert call_js(other, "return typeof navigator.credentials;") == "undefined"


def test_get_returns_a_browser_shaped_assertion(monkeypatch):
    seen = {}

    def backend(lib, rp_id, client_hash, allow, uv, timeout, interaction, cancel):
        seen.update(rp_id=rp_id, hash=client_hash, allow=allow, uv=uv)
        interaction.status("touch")
        assert interaction.ask_pin(None, 8) == "1234"
        return {"credential_id": b"\x01\x02\x03", "authenticator_data": b"AUTH",
                "signature": b"SIG", "user_handle": b"USER"}

    panel = FakePanel()
    view, _ = make_view("https://login.microsoftonline.com/signin", panel, monkeypatch, backend)
    result = json.loads(call_js(view, GET))
    assert result["id"] == "AQID" and result["raw"] == [1, 2, 3]
    assert result["type"] == "public-key" and result["attach"] == "cross-platform"
    assert result["auth"] == list(b"AUTH") and result["sig"] == list(b"SIG")
    assert result["user"] == list(b"USER")
    assert result["isPKC"] and result["isAssertion"]
    challenge = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
    assert result["client"] == {"type": "webauthn.get", "challenge": challenge,
                                "origin": "https://login.microsoftonline.com",
                                "crossOrigin": False}
    assert seen["rp_id"] == "login.microsoft.com" and seen["uv"] == "required"
    assert seen["allow"] == [b"\x01\x02\x03"] and len(seen["hash"]) == 32
    assert ("pin", None) in panel.prompts
    assert panel.states[-1] == ("idle", None)


def test_errors_reach_the_page_as_dom_exceptions(monkeypatch):
    def backend(*args):
        raise fido.WebAuthnError("NotAllowedError", "This security key has no passkey for this account.")

    panel = FakePanel()
    view, _ = make_view("https://login.microsoftonline.com/", panel, monkeypatch, backend)
    assert call_js(view, GET) == "ERR NotAllowedError: This security key has no passkey for this account."
    assert panel.states[-1][0] == "error"


def test_pages_cannot_use_other_relying_parties_or_hosts(monkeypatch):
    def backend(*args):
        raise AssertionError("must not reach the key")

    requests = []
    view, _ = make_view("https://login.microsoftonline.com/", FakePanel(), monkeypatch, backend,
                        on_request=lambda: requests.append(True))
    other_rp = GET.replace("login.microsoft.com", "example.com")
    assert call_js(view, other_rp).startswith("ERR SecurityError")
    assert requests == []


def test_a_real_request_reveals_the_waiting_window(monkeypatch):
    requests = []

    def backend(*args):
        raise fido.WebAuthnError(*fido._NOT_ALLOWED)

    view, _ = make_view("https://login.microsoftonline.com/", FakePanel(), monkeypatch, backend,
                        on_request=lambda: requests.append(True))
    call_js(view, GET)
    assert requests == [True]


def test_user_cancel_rejects_and_hides_the_prompt(monkeypatch):
    started = []

    def backend(lib, rp_id, client_hash, allow, uv, timeout, interaction, cancel):
        started.append(True)
        cancel.wait(10)
        raise fido.WebAuthnError(*fido._NOT_ALLOWED)

    panel = FakePanel()
    view, bridge = make_view("https://login.microsoftonline.com/", panel, monkeypatch, backend)
    outcome = []
    view.call_async_javascript_function(
        "try { " + GET + " } catch (e) { return 'ERR ' + e.name; }", -1, None, None, None, None,
        lambda v, r: outcome.append(v.call_async_javascript_function_finish(r).to_string()))
    assert run_loop_until(lambda: started)
    panel.cancel()
    assert run_loop_until(lambda: outcome)
    assert outcome == ["ERR NotAllowedError"]
    assert panel.states[-1] == ("idle", None)


def test_page_origin_requires_https_and_a_known_host():
    hosts = webauthn.allowed_hosts()
    assert webauthn.page_origin("https://login.microsoftonline.com/x?y", hosts) \
        == "https://login.microsoftonline.com"
    assert webauthn.page_origin("http://login.microsoftonline.com/", hosts) is None
    assert webauthn.page_origin("https://login.microsoftonline.com.evil.example/", hosts) is None
    assert webauthn.page_origin("https://evil.example/login.microsoftonline.com", hosts) is None
    assert webauthn.page_origin("", hosts) is None
