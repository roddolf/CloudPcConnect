"""Security-key (FIDO2/CTAP) assertions for the in-app sign-in.

WebKitGTK has no WebAuthn, so the sign-in window injects a small shim that
forwards ``navigator.credentials.get()`` to :mod:`avd_feed_connect.gui.webauthn`,
which uses this module to talk to the key through the same libfido2 that the
bundled FreeRDP client uses. This module is toolkit-free: it only needs ctypes
and the user-interaction callbacks in :class:`Interaction`.
"""

import ctypes
import threading
import time

OPT_TRUE = 2   # FIDO_OPT_TRUE

ERR_OPERATION_DENIED = 0x27
ERR_KEEPALIVE_CANCEL = 0x2D
ERR_NO_CREDENTIALS = 0x2E
ERR_USER_ACTION_TIMEOUT = 0x2F
ERR_NOT_ALLOWED = 0x30
ERR_PIN_INVALID = 0x31
ERR_PIN_BLOCKED = 0x32
ERR_PIN_AUTH_INVALID = 0x33
ERR_PIN_AUTH_BLOCKED = 0x34
ERR_PIN_NOT_SET = 0x35
ERR_PIN_REQUIRED = 0x36
ERR_ACTION_TIMEOUT = 0x3A
ERR_UV_BLOCKED = 0x3C
ERR_UV_INVALID = 0x3F

_CANCELLED = {ERR_KEEPALIVE_CANCEL, ERR_OPERATION_DENIED, ERR_USER_ACTION_TIMEOUT,
              ERR_ACTION_TIMEOUT}
_WRONG_PIN = {ERR_PIN_INVALID, ERR_PIN_AUTH_INVALID}


class WebAuthnError(Exception):
    """A failure the page should see as a DOMException of the given name."""

    def __init__(self, name, message):
        super().__init__(message)
        self.name = name
        self.message = message


class FidoError(Exception):
    def __init__(self, code):
        super().__init__(f"libfido2 error {code}")
        self.code = code


class Interaction:
    """What the assertion needs from the user; called from a worker thread."""

    def status(self, state):                  # 'insert' | 'touch'
        pass

    def ask_pin(self, error, retries):        # -> str, or None when cancelled
        return None

    def choose(self, accounts):               # -> index, or None when cancelled
        return 0 if accounts else None


class FidoLibrary:
    def __init__(self, cdll):
        c = self.c = cdll
        p, i, sz, cp = ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_char_p
        signatures = {
            "fido_init": (None, [i]),
            "fido_dev_info_new": (p, [sz]),
            "fido_dev_info_manifest": (i, [p, sz, ctypes.POINTER(sz)]),
            "fido_dev_info_ptr": (p, [p, sz]),
            "fido_dev_info_path": (cp, [p]),
            "fido_dev_info_free": (None, [ctypes.POINTER(p), sz]),
            "fido_dev_new": (p, []),
            "fido_dev_open": (i, [p, cp]),
            "fido_dev_close": (i, [p]),
            "fido_dev_free": (None, [ctypes.POINTER(p)]),
            "fido_dev_cancel": (i, [p]),
            "fido_dev_has_pin": (ctypes.c_bool, [p]),
            "fido_dev_has_uv": (ctypes.c_bool, [p]),
            "fido_dev_get_retry_count": (i, [p, ctypes.POINTER(i)]),
            "fido_dev_get_assert": (i, [p, p, cp]),
            "fido_assert_new": (p, []),
            "fido_assert_free": (None, [ctypes.POINTER(p)]),
            "fido_assert_set_rp": (i, [p, cp]),
            "fido_assert_set_clientdata_hash": (i, [p, cp, sz]),
            "fido_assert_allow_cred": (i, [p, cp, sz]),
            "fido_assert_set_up": (i, [p, i]),
            "fido_assert_set_uv": (i, [p, i]),
            "fido_assert_count": (sz, [p]),
            "fido_assert_id_ptr": (p, [p, sz]),
            "fido_assert_id_len": (sz, [p, sz]),
            "fido_assert_authdata_raw_ptr": (p, [p, sz]),
            "fido_assert_authdata_raw_len": (sz, [p, sz]),
            "fido_assert_sig_ptr": (p, [p, sz]),
            "fido_assert_sig_len": (sz, [p, sz]),
            "fido_assert_user_id_ptr": (p, [p, sz]),
            "fido_assert_user_id_len": (sz, [p, sz]),
            "fido_assert_user_name": (cp, [p, sz]),
            "fido_assert_user_display_name": (cp, [p, sz]),
        }
        for name, (restype, argtypes) in signatures.items():
            function = getattr(c, name)
            function.restype = restype
            function.argtypes = argtypes
        c.fido_init(0)

    def devices(self):
        capacity = 32
        found = self.c.fido_dev_info_new(capacity)
        if not found:
            return []
        try:
            count = ctypes.c_size_t(0)
            if self.c.fido_dev_info_manifest(found, capacity, ctypes.byref(count)) != 0:
                return []
            paths = []
            for index in range(count.value):
                path = self.c.fido_dev_info_path(self.c.fido_dev_info_ptr(found, index))
                if path:
                    paths.append(path.decode(errors="replace"))
            return paths
        finally:
            self.c.fido_dev_info_free(ctypes.byref(ctypes.c_void_p(found)), capacity)


def load_library():
    for name in ("libfido2.so.1", "libfido2.so"):
        try:
            return FidoLibrary(ctypes.CDLL(name))
        except (OSError, AttributeError):
            continue
    return None


def _blob(pointer, length):
    return ctypes.string_at(pointer, length) if pointer and length else b""


class Device:
    def __init__(self, library, path):
        self.lib = library
        self.path = path
        self.handle = library.c.fido_dev_new()
        self.is_open = False
        self._lock = threading.Lock()

    def open(self):
        code = self.lib.c.fido_dev_open(self.handle, self.path.encode())
        if code != 0:
            raise FidoError(code)
        self.is_open = True

    def close(self):
        with self._lock:
            if self.handle:
                if self.is_open:
                    self.lib.c.fido_dev_close(self.handle)
                    self.is_open = False
                self.lib.c.fido_dev_free(ctypes.byref(ctypes.c_void_p(self.handle)))
                self.handle = None

    def cancel(self):
        with self._lock:
            if self.handle and self.is_open:
                self.lib.c.fido_dev_cancel(self.handle)

    def has_pin(self):
        return bool(self.lib.c.fido_dev_has_pin(self.handle))

    def has_uv(self):
        return bool(self.lib.c.fido_dev_has_uv(self.handle))

    def retries(self):
        count = ctypes.c_int(0)
        if self.lib.c.fido_dev_get_retry_count(self.handle, ctypes.byref(count)) == 0:
            return count.value
        return None

    def get_assertion(self, rp_id, client_hash, allow, user_verification, pin):
        c = self.lib.c
        request = c.fido_assert_new()
        if not request:
            raise FidoError(-9)
        secret = ctypes.create_string_buffer(pin.encode()) if pin else None
        try:
            for code in (
                    c.fido_assert_set_rp(request, rp_id.encode()),
                    c.fido_assert_set_clientdata_hash(request, client_hash, len(client_hash)),
                    c.fido_assert_set_up(request, OPT_TRUE),
                    c.fido_assert_set_uv(request, OPT_TRUE) if user_verification else 0,
                    *(c.fido_assert_allow_cred(request, credential, len(credential))
                      for credential in allow)):
                if code != 0:
                    raise FidoError(code)
            code = c.fido_dev_get_assert(self.handle, request, secret)
            if code != 0:
                raise FidoError(code)
            assertions = []
            for index in range(c.fido_assert_count(request)):
                name = c.fido_assert_user_name(request, index)
                display = c.fido_assert_user_display_name(request, index)
                assertions.append({
                    "credential_id": _blob(c.fido_assert_id_ptr(request, index),
                                           c.fido_assert_id_len(request, index)),
                    "authenticator_data": _blob(
                        c.fido_assert_authdata_raw_ptr(request, index),
                        c.fido_assert_authdata_raw_len(request, index)),
                    "signature": _blob(c.fido_assert_sig_ptr(request, index),
                                       c.fido_assert_sig_len(request, index)),
                    "user_handle": _blob(c.fido_assert_user_id_ptr(request, index),
                                         c.fido_assert_user_id_len(request, index)),
                    "name": name.decode(errors="replace") if name else "",
                    "display_name": display.decode(errors="replace") if display else "",
                })
            return assertions
        finally:
            if secret is not None:
                ctypes.memset(secret, 0, len(secret))
            c.fido_assert_free(ctypes.byref(ctypes.c_void_p(request)))


NOT_ALLOWED_MESSAGE = "The operation either timed out or was not allowed."
_NOT_ALLOWED = ("NotAllowedError", NOT_ALLOWED_MESSAGE)


def _user_verification(preference, device):
    """Return (use_pin, use_builtin_uv) for the page's userVerification."""
    if preference == "discouraged":
        return False, False
    if device.has_pin():
        return True, False
    if device.has_uv():
        return False, True
    if preference == "required":
        raise WebAuthnError(
            "NotAllowedError",
            "This security key has no PIN or fingerprint set up, which this "
            "sign-in requires. Set a PIN on the key first.")
    return False, False


def _assert_on_device(lib, path, request, interaction, finished):
    device = Device(lib, path)
    try:
        # A key that was just plugged in can take a moment to become accessible.
        for attempt in range(12):
            try:
                device.open()
                break
            except FidoError as error:
                if attempt == 11 or finished.wait(0.25):
                    raise WebAuthnError(
                        "NotAllowedError",
                        "Could not open the security key (permission denied?).") from error
        request["devices"].append(device)
        use_pin, use_uv = _user_verification(request["user_verification"], device)
        pin = None
        error_text = None
        while True:
            if finished.is_set():
                raise WebAuthnError(*_NOT_ALLOWED)
            if use_pin and pin is None:
                pin = interaction.ask_pin(error_text, device.retries())
                if pin is None:
                    raise WebAuthnError(*_NOT_ALLOWED)
            interaction.status("touch")
            try:
                return device.get_assertion(
                    request["rp_id"], request["client_hash"], request["allow"],
                    use_pin or use_uv, pin)
            except FidoError as error:
                if error.code in _WRONG_PIN and use_pin:
                    retries = device.retries()
                    error_text = ("Wrong PIN." if retries is None else
                                  f"Wrong PIN. {retries} attempt{'s' * (retries != 1)} left.")
                    pin = None
                    continue
                if error.code == ERR_PIN_AUTH_BLOCKED:
                    raise WebAuthnError(
                        "NotAllowedError",
                        "Too many wrong PINs. Unplug the key, plug it back in "
                        "and try again.") from error
                if error.code == ERR_PIN_BLOCKED:
                    raise WebAuthnError(
                        "NotAllowedError",
                        "The security key's PIN is blocked. It has to be reset.") from error
                if error.code == ERR_NO_CREDENTIALS:
                    raise WebAuthnError(
                        "NotAllowedError",
                        "This security key has no passkey for this account.") from error
                if error.code == ERR_PIN_REQUIRED and not use_pin:
                    use_pin = True
                    continue
                if error.code in _CANCELLED or finished.is_set():
                    raise WebAuthnError(*_NOT_ALLOWED) from error
                raise WebAuthnError(
                    "NotAllowedError", "The security key could not complete the request.") from error
    finally:
        device.close()


def get_assertion(lib, rp_id, client_hash, allow, user_verification, timeout,
                  interaction, cancel, poll=0.4):
    """Blocking WebAuthn getAssertion on the first key that answers.

    Waits for a key to be plugged in, asks for the PIN and for an account
    through ``interaction``, and returns the chosen assertion. ``cancel`` is a
    threading.Event; set it (from any thread) to abort. Raises WebAuthnError.
    """
    deadline = time.monotonic() + min(max(timeout, 10), 300)
    request = {"rp_id": rp_id, "client_hash": client_hash, "allow": list(allow),
               "user_verification": user_verification, "devices": []}
    finished = threading.Event()
    announced = False
    while not cancel.is_set():
        if time.monotonic() > deadline:
            raise WebAuthnError(*_NOT_ALLOWED)
        paths = lib.devices()
        if paths:
            break
        if not announced:
            interaction.status("insert")
            announced = True
        cancel.wait(poll)
    else:
        raise WebAuthnError(*_NOT_ALLOWED)

    lock = threading.Lock()
    outcome = {"assertions": None, "errors": []}
    remaining = [len(paths)]

    def run(path):
        try:
            assertions = _assert_on_device(lib, path, request, interaction, finished)
            with lock:
                if outcome["assertions"] is None:
                    outcome["assertions"] = assertions
        except WebAuthnError as error:
            with lock:
                outcome["errors"].append(error)
        except Exception as error:
            with lock:
                outcome["errors"].append(
                    WebAuthnError("NotAllowedError", f"{type(error).__name__}"))
        finally:
            with lock:
                remaining[0] -= 1
                if remaining[0] == 0 or outcome["assertions"] is not None:
                    finished.set()
                    for device in list(request["devices"]):
                        device.cancel()

    threads = [threading.Thread(target=run, args=(path,), daemon=True) for path in paths]
    for thread in threads:
        thread.start()

    def watchdog():
        while not finished.is_set():
            if cancel.is_set() or time.monotonic() > deadline:
                finished.set()
                for device in list(request["devices"]):
                    device.cancel()
                return
            finished.wait(0.2)
    threading.Thread(target=watchdog, daemon=True).start()
    for thread in threads:
        thread.join()
    finished.set()

    assertions = outcome["assertions"]
    if not assertions:
        for error in outcome["errors"]:
            if error.message != _NOT_ALLOWED[1]:
                raise error
        raise WebAuthnError(*_NOT_ALLOWED)
    index = 0
    if len(assertions) > 1:
        accounts = [{"name": item["display_name"] or item["name"],
                     "detail": item["name"] if item["display_name"] else ""}
                    for item in assertions]
        index = interaction.choose(accounts)
        if index is None or not 0 <= index < len(assertions):
            raise WebAuthnError(*_NOT_ALLOWED)
    chosen = dict(assertions[index])
    if not chosen["credential_id"] and len(request["allow"]) == 1:
        chosen["credential_id"] = request["allow"][0]
    if not chosen["credential_id"]:
        raise WebAuthnError("NotAllowedError", "The security key returned no credential.")
    return chosen
