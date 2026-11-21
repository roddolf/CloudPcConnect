"""WebAuthn (security-key) support for the embedded sign-in WebViews.

WebKitGTK ships no WebAuthn, so ``navigator.credentials`` does not exist and
Microsoft's sign-in page never offers the security-key option. This module
injects a small shim that implements ``navigator.credentials.get()`` by
forwarding the request to this process, which answers it through libfido2
(see :mod:`avd_feed_connect.auth.fido`) and shows the PIN / account / touch
prompts in the sign-in window itself.

Only the Microsoft sign-in hosts get the shim and only they are answered; the
origin put into ``clientDataJSON`` comes from the WebView's own URL, never from
the page's message.
"""

import base64
import hashlib
import json
import threading
from urllib.parse import urlparse

import gi
gi.require_version("WebKit", "6.0")
gi.require_version("JavaScriptCore", "6.0")
from gi.repository import GLib, JavaScriptCore as JSC, WebKit

from .. import config
from ..auth import fido
from ..auth.fido import WebAuthnError
from ..i18n import _ as tr

HANDLER = "avdWebAuthn"
MICROSOFT_LOGIN_HOSTS = frozenset({
    "login.microsoftonline.com", "login.microsoft.com", "login.windows.net",
    "login.microsoftonline.us", "login.partner.microsoftonline.cn",
})

SHIM_JS = r"""
(() => {
  'use strict';
  const channel = window.webkit && window.webkit.messageHandlers &&
      window.webkit.messageHandlers.avdWebAuthn;
  if (!channel || navigator.credentials || window.PublicKeyCredential) return;

  const bytes = (value) => {
    if (value instanceof ArrayBuffer) return new Uint8Array(value);
    if (ArrayBuffer.isView(value))
      return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    throw new TypeError('Expected a BufferSource');
  };
  const encode = (value) => {
    let text = '';
    for (const byte of bytes(value)) text += String.fromCharCode(byte);
    return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  };
  const decode = (text) => {
    const padded = text.replace(/-/g, '+').replace(/_/g, '/') +
        '==='.slice((text.length + 3) % 4);
    const binary = atob(padded);
    const out = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
    return out.buffer;
  };
  const fail = (name, message) => new DOMException(message, name);

  class AuthenticatorResponse {}
  class AuthenticatorAssertionResponse extends AuthenticatorResponse {}
  class PublicKeyCredential {
    getClientExtensionResults() { return {}; }
    toJSON() {
      const r = this.response;
      return {
        id: this.id, rawId: this.id, type: this.type,
        authenticatorAttachment: this.authenticatorAttachment,
        clientExtensionResults: {},
        response: {
          clientDataJSON: encode(r.clientDataJSON),
          authenticatorData: encode(r.authenticatorData),
          signature: encode(r.signature),
          userHandle: r.userHandle ? encode(r.userHandle) : null,
        },
      };
    }
  }
  const no = async () => false;
  PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable = no;
  PublicKeyCredential.isConditionalMediationAvailable = no;
  PublicKeyCredential.getClientCapabilities = async () => ({});

  let sequence = 0;
  const get = async (options) => {
    const key = options && options.publicKey;
    if (!key) throw fail('NotSupportedError', 'Only public-key credentials are supported.');
    const signal = options.signal;
    if (signal && signal.aborted) throw fail('AbortError', 'The operation was aborted.');
    if (options.mediation === 'conditional')
      throw fail('NotSupportedError', 'Conditional mediation is not supported.');
    const requestId = ++sequence;
    const onAbort = () => channel.postMessage({op: 'cancel', requestId});
    if (signal) signal.addEventListener('abort', onAbort, {once: true});
    let result;
    try {
      result = JSON.parse(await channel.postMessage({
        op: 'get', requestId,
        rpId: key.rpId || location.hostname,
        challenge: encode(key.challenge),
        allowCredentials: (key.allowCredentials || []).map((c) => encode(c.id)),
        userVerification: key.userVerification || 'preferred',
        timeout: key.timeout || 0,
      }));
    } finally {
      if (signal) signal.removeEventListener('abort', onAbort);
    }
    if (signal && signal.aborted) throw fail('AbortError', 'The operation was aborted.');
    if (result.error) throw fail(result.error.name, result.error.message);
    const ok = result.ok;
    const response = Object.create(AuthenticatorAssertionResponse.prototype);
    Object.defineProperties(response, {
      clientDataJSON: {value: decode(ok.clientDataJSON), enumerable: true},
      authenticatorData: {value: decode(ok.authenticatorData), enumerable: true},
      signature: {value: decode(ok.signature), enumerable: true},
      userHandle: {value: ok.userHandle ? decode(ok.userHandle) : null, enumerable: true},
    });
    const credential = Object.create(PublicKeyCredential.prototype);
    Object.defineProperties(credential, {
      id: {value: ok.credentialId, enumerable: true},
      rawId: {value: decode(ok.credentialId), enumerable: true},
      type: {value: 'public-key', enumerable: true},
      authenticatorAttachment: {value: 'cross-platform', enumerable: true},
      response: {value: response, enumerable: true},
    });
    return credential;
  };
  const container = {
    get,
    create: async () => { throw fail('NotSupportedError', 'Creating passkeys is not supported here.'); },
    store: async () => { throw fail('NotSupportedError', 'Not supported.'); },
    preventSilentAccess: async () => {},
  };
  Object.defineProperty(Navigator.prototype, 'credentials', {get: () => container, configurable: true});
  Object.defineProperty(window, 'PublicKeyCredential', {value: PublicKeyCredential, configurable: true, writable: true});
  Object.defineProperty(window, 'AuthenticatorResponse', {value: AuthenticatorResponse, configurable: true, writable: true});
  Object.defineProperty(window, 'AuthenticatorAssertionResponse', {value: AuthenticatorAssertionResponse, configurable: true, writable: true});
})();
"""


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text):
    if not isinstance(text, str):
        raise ValueError("not text")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def allowed_hosts():
    hosts = set(MICROSOFT_LOGIN_HOSTS)
    login_host = urlparse(config.LOGIN).hostname
    if login_host:
        hosts.add(login_host)
    return hosts


def page_origin(uri, hosts):
    """The https origin of ``uri`` if it is an allowed sign-in page, else None."""
    parts = urlparse(uri or "")
    if parts.scheme != "https" or parts.hostname not in hosts:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return f"https://{parts.hostname}" + (f":{port}" if port not in (None, 443) else "")


def client_data(challenge, origin):
    """The clientDataJSON a browser would produce for this get() call."""
    return json.dumps({"type": "webauthn.get", "challenge": challenge,
                       "origin": origin, "crossOrigin": False},
                      separators=(",", ":")).encode()


class _Interaction(fido.Interaction):
    """Marshals the worker thread's questions onto the GTK main loop."""

    def __init__(self, panel, cancel):
        self.panel = panel
        self.cancel = cancel
        self.lock = threading.Lock()   # one PIN/account question at a time

    def _ask(self, show):
        done = threading.Event()
        answer = []

        def reply(value):
            if not answer:
                answer.append(value)
            done.set()

        def present():
            if self.cancel.is_set():
                reply(None)
            else:
                show(reply)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(present)
        while not done.wait(0.2):
            if self.cancel.is_set():
                return None
        return answer[0]

    def status(self, state):
        def apply():
            if not self.cancel.is_set():
                self.panel.set_state(state)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(apply)

    def ask_pin(self, error, retries):
        prompt = tr("Enter your security key PIN")
        with self.lock:
            return self._ask(lambda reply: self.panel.ask_pin(prompt, reply, error=error))

    def choose(self, accounts):
        with self.lock:
            return self._ask(lambda reply: self.panel.choose_account(accounts, reply))


class WebAuthnBridge:
    """Answers navigator.credentials.get() for one WebView."""

    def __init__(self, webview, panel, library=None, hosts=None, on_request=None):
        self.webview = webview
        self.panel = panel
        self.on_request = on_request
        self.hosts = frozenset(hosts) if hosts else frozenset(allowed_hosts())
        self._library = library
        self._library_loaded = library is not None
        self._active = None

    def attach(self):
        manager = self.webview.get_user_content_manager()
        manager.add_script(WebKit.UserScript.new(
            SHIM_JS, WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.START,
            [f"https://{host}/*" for host in sorted(self.hosts)], None))
        manager.register_script_message_handler_with_reply(HANDLER, None)
        manager.connect(f"script-message-with-reply-received::{HANDLER}", self._on_message)
        self.webview.connect("load-changed", self._on_load_changed)
        self.panel.connect_cancel(self.cancel)
        return self

    def cancel(self, *_):
        active, self._active = self._active, None
        if active is not None:
            active["cancel"].set()
        self.panel.set_state("idle")

    def _on_load_changed(self, _view, event):
        if event == WebKit.LoadEvent.STARTED and self._active is not None:
            self.cancel()

    @property
    def library(self):
        if not self._library_loaded:
            self._library = fido.load_library()
            self._library_loaded = True
        return self._library

    def _reply(self, reply, context, payload):
        reply.return_value(JSC.Value.new_string(context, json.dumps(payload)))

    def _error(self, reply, context, name, message):
        self._reply(reply, context, {"error": {"name": name, "message": message}})

    def _on_message(self, _manager, value, reply):
        context = value.get_context()
        try:
            message = json.loads(value.to_json(0))
            operation = message["op"]
        except (ValueError, KeyError, TypeError):
            self._error(reply, context, "NotSupportedError", "Malformed request.")
            return True
        if operation == "cancel":
            if self._active is not None and self._active["id"] == message.get("requestId"):
                self.cancel()
            self._reply(reply, context, {"ok": {}})
            return True
        if operation != "get":
            self._error(reply, context, "NotSupportedError", "Unsupported operation.")
            return True
        try:
            self._start(message, reply, context)
        except WebAuthnError as error:
            self._error(reply, context, error.name, error.message)
        return True

    def _start(self, message, reply, context):
        origin = page_origin(self.webview.get_uri(), self.hosts)
        rp_id = message.get("rpId")
        if origin is None or rp_id not in self.hosts:
            raise WebAuthnError("SecurityError", "This page may not use a security key.")
        try:
            challenge = message["challenge"]
            if len(b64url_decode(challenge)) < 16:
                raise ValueError("challenge too short")
            allow = [b64url_decode(item) for item in message.get("allowCredentials", [])]
            timeout = float(message.get("timeout") or 0) / 1000 or 120
        except (ValueError, KeyError, TypeError) as error:
            raise WebAuthnError("TypeError", "Invalid WebAuthn options.") from error
        preference = message.get("userVerification")
        if preference not in ("required", "preferred", "discouraged"):
            preference = "preferred"
        library = self.library
        if library is None:
            raise WebAuthnError("NotSupportedError", "Security keys are not available.")
        self.cancel()
        if self.on_request is not None:
            self.on_request()
        data = client_data(challenge, origin)
        active = self._active = {"id": message.get("requestId"), "cancel": threading.Event()}
        interaction = _Interaction(self.panel, active["cancel"])

        def work():
            try:
                found = fido.get_assertion(
                    library, rp_id, hashlib.sha256(data).digest(), allow, preference,
                    timeout, interaction, active["cancel"])
                payload = {"ok": {
                    "credentialId": b64url(found["credential_id"]),
                    "authenticatorData": b64url(found["authenticator_data"]),
                    "signature": b64url(found["signature"]),
                    "userHandle": b64url(found["user_handle"]) if found["user_handle"] else None,
                    "clientDataJSON": b64url(data)}}
            except WebAuthnError as error:
                payload = {"error": {"name": error.name, "message": error.message}}
            except Exception:
                payload = {"error": {"name": "NotAllowedError",
                                     "message": "The security key request failed."}}
            GLib.idle_add(finish, payload)

        def finish(payload):
            current = self._active is active
            if current:
                self._active = None
            self._reply(reply, context, payload)
            if current:
                error = payload.get("error")
                if error and error["message"] != fido.NOT_ALLOWED_MESSAGE:
                    self.panel.set_state("error", error["message"])
                    GLib.timeout_add_seconds(8, hide_error)
                else:
                    self.panel.set_state("idle")
            return GLib.SOURCE_REMOVE

        def hide_error():
            if self._active is None:
                self.panel.set_state("idle")
            return GLib.SOURCE_REMOVE

        threading.Thread(target=work, daemon=True).start()
