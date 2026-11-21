"""In-app sign-in / security-key card (no network or auth logic).

A themed card for the WebAuthn bridge to drive while the sign-in page waits
for a security key. Styling lives in theme.py (.avd-keyprompt*). Every method
must be called on the GTK main thread.
"""
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk, Pango  # noqa: E402

from ..i18n import _ as tr  # noqa: E402
from .localization import bind  # noqa: E402

_ICONS = {
    "insert": "media-removable-symbolic",
    "touch": "auth-fingerprint-symbolic",
    "busy": "channel-secure-symbolic",
    "error": "dialog-warning-symbolic",
    "pin": "dialog-password-symbolic",
    "choose": "system-users-symbolic",
}
_DIGITS = {getattr(Gdk, f"KEY_{n}"): n for n in range(1, 10)}
_DIGITS.update({getattr(Gdk, f"KEY_KP_{n}"): n for n in range(1, 10)})


class SignInPanel(Gtk.Box):
    """States: idle (hidden), insert, touch, busy, error; plus ask_pin() and
    choose_account() prompts whose callbacks run exactly once.

    Cancel/Esc first resolves a pending prompt with None, then calls the
    connect_cancel() callbacks; if none of them changed the state, the card
    hides itself. Showing another state also resolves a pending prompt with
    None.
    """

    STATES = ("idle", "insert", "touch", "busy", "error")

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("avd-keyprompt")
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.END)
        self.set_margin_bottom(24)
        self.set_size_request(380, -1)
        self.set_focusable(True)
        self._mode = "idle"
        self._generation = 0
        self._pending = None
        self._cancel_callbacks = []

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._glyph = Gtk.Image(pixel_size=20, valign=Gtk.Align.CENTER)
        self._glyph.add_css_class("avd-keyprompt-glyph")
        self._title = Gtk.Label(xalign=0, wrap=True, hexpand=True, max_width_chars=40)
        self._title.add_css_class("avd-keyprompt-title")
        self._spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        for widget in (self._glyph, self._title, self._spinner):
            head.append(widget)
        self.append(head)

        self._text = self._label("avd-keyprompt-text")
        self._error = self._label("avd-keyprompt-error")

        self._entry = Gtk.PasswordEntry(show_peek_icon=False)
        self._entry.add_css_class("avd-keyprompt-pin")
        self._entry.connect("activate", lambda *_: self._submit_pin())
        self._entry.connect("changed", lambda *_: self._sync_continue())
        self.append(self._entry)

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._list.connect("row-activated", lambda _list, row: self._pick(row.get_index()))
        self._list.connect("row-selected", lambda *_: self._sync_continue())
        self._scroll = Gtk.ScrolledWindow(child=self._list, propagate_natural_height=True,
                                          max_content_height=260,
                                          hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.append(self._scroll)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_margin_top(4)
        self._hint = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self._hint.add_css_class("avd-keyprompt-hint")
        self._cancel = Gtk.Button()
        bind(self._cancel, "label", tr("Cancel"))
        self._cancel.connect("clicked", lambda *_: self._on_cancel())
        self._continue = Gtk.Button()
        bind(self._continue, "label", tr("Continue"))
        self._continue.add_css_class("suggested-action")
        self._continue.connect("clicked", lambda *_: self._confirm())
        for widget in (self._hint, self._cancel, self._continue):
            actions.append(widget)
        self.append(actions)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("map", lambda *_: self._focus())
        self.set_visible(False)

    # ---- public API ---------------------------------------------------------
    def set_state(self, state, message=None):
        if state not in self.STATES:
            raise ValueError(f"unknown sign-in panel state: {state!r}")
        self._settle(None)
        self._render(state, message)

    def ask_pin(self, prompt, cb, *, error=None):
        self._settle(None)
        self._render("pin", prompt, error=error)
        self._pending = cb

    def choose_account(self, accounts, cb):
        self._settle(None)
        accounts = list(accounts or [])
        if not accounts:
            cb(None)
            return
        self._render("choose", None)
        for index, account in enumerate(accounts):
            self._list.append(self._account_row(index, account))
        self._list.select_row(self._list.get_row_at_index(0))
        self._pending = cb
        self._sync_continue()
        self._focus()

    def connect_cancel(self, cb):
        self._cancel_callbacks.append(cb)

    @property
    def state(self):
        return self._mode

    # ---- rendering ----------------------------------------------------------
    def _label(self, css):
        label = Gtk.Label(xalign=0, wrap=True, max_width_chars=48)
        label.add_css_class(css)
        self.append(label)
        return label

    def _render(self, mode, message, error=None):
        self._generation += 1
        self._mode = mode
        self._entry.set_text("")
        while (row := self._list.get_row_at_index(0)) is not None:
            self._list.remove(row)
        for css in ("error", "touch", "pin-error"):
            self.remove_css_class(css)
        if mode == "idle":
            self._spinner.stop()
            self.set_visible(False)
            return
        titles = {
            "insert": tr("Insert your security key"),
            "touch": tr("Touch your security key"),
            "busy": tr("Signing in…"),
            "error": tr("Sign-in failed"),
            "pin": tr("Security key PIN"),
            "choose": tr("Choose an account"),
        }
        bind(self._title, "label", titles[mode])
        self._glyph.set_from_icon_name(_ICONS[mode])
        if mode in ("error", "touch"):
            self.add_css_class(mode)
        if mode == "choose":
            message = tr("Your security key has more than one account.")
        error = message if mode == "error" else error
        message = None if mode == "error" else message
        bind(self._text, "label", message or "")
        self._text.set_visible(bool(message))
        bind(self._error, "label", error or "")
        self._error.set_visible(bool(error))
        if mode == "pin" and error:
            self.add_css_class("pin-error")
        spinning = mode in ("insert", "busy")
        self._spinner.set_visible(spinning)
        (self._spinner.start if spinning else self._spinner.stop)()
        self._entry.set_visible(mode == "pin")
        # PasswordEntry has no placeholder setter for bind(); prompts are short-lived.
        self._entry.set_property("placeholder-text", tr("PIN").render())
        self._scroll.set_visible(mode == "choose")
        self._continue.set_visible(mode in ("pin", "choose"))
        hints = {"pin": tr("Enter to continue · Esc to cancel"),
                 "choose": tr("1–9 or Enter to choose · Esc to cancel")}
        bind(self._hint, "label", hints.get(mode, tr("Esc to cancel")))
        self._sync_continue()
        self.set_visible(True)
        self._focus()

    def _account_row(self, index, account):
        row = Gtk.ListBoxRow()
        row.add_css_class("avd-keyprompt-account")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        key = Gtk.Label(label=str(index + 1) if index < 9 else "", valign=Gtk.Align.CENTER)
        key.add_css_class("avd-keyprompt-key")
        key.set_visible(index < 9)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        name = (account.get("name") or "").strip() if isinstance(account, dict) else ""
        detail = (account.get("detail") or "").strip() if isinstance(account, dict) else ""
        title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        bind(title, "label", name or tr("Account {number}").format(number=index + 1))
        title.add_css_class("avd-keyprompt-account-name")
        text.append(title)
        if detail:
            sub = Gtk.Label(label=detail, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            sub.add_css_class("avd-keyprompt-account-detail")
            text.append(sub)
        chevron = Gtk.Label(label="›", valign=Gtk.Align.CENTER)
        chevron.add_css_class("avd-keyprompt-chevron")
        for widget in (key, text, chevron):
            box.append(widget)
        row.set_child(box)
        return row

    def _focus(self):
        if not self.get_mapped():
            return
        if self._mode == "pin":
            self._entry.grab_focus()
        elif self._mode == "choose" and self._list.get_selected_row() is not None:
            self._list.get_selected_row().grab_focus()
        elif self._mode != "idle" and not self._has_focus_within():
            self.grab_focus()

    def _has_focus_within(self):
        root = self.get_root()
        focus = root.get_focus() if root is not None else None
        while focus is not None:
            if focus is self:
                return True
            focus = focus.get_parent()
        return False

    def _sync_continue(self):
        if self._mode == "pin":
            self._continue.set_sensitive(bool(self._entry.get_text()))
        elif self._mode == "choose":
            self._continue.set_sensitive(self._list.get_selected_row() is not None)

    # ---- results ------------------------------------------------------------
    def _settle(self, value):
        callback, self._pending = self._pending, None
        if self._mode == "pin":
            self._entry.set_text("")
        if callback is not None:
            callback(value)

    def _submit_pin(self):
        if self._mode != "pin" or self._pending is None:
            return
        pin = self._entry.get_text()
        if not pin:
            return
        callback, self._pending = self._pending, None
        self._render("busy", None)
        callback(pin)

    def _pick(self, index):
        if self._mode != "choose" or self._pending is None:
            return
        if self._list.get_row_at_index(index) is None:
            return
        callback, self._pending = self._pending, None
        self._render("busy", None)
        callback(index)

    def _confirm(self):
        if self._mode == "pin":
            self._submit_pin()
        elif self._mode == "choose" and self._list.get_selected_row() is not None:
            self._pick(self._list.get_selected_row().get_index())

    def _on_cancel(self):
        if self._mode == "idle":
            return
        generation = self._generation
        self._settle(None)
        for callback in list(self._cancel_callbacks):
            callback()
        if self._generation == generation:
            self._render("idle", None)

    def _on_key(self, _controller, keyval, _code, _state):
        if self._mode == "idle":
            return False
        if keyval == Gdk.KEY_Escape:
            self._on_cancel()
            return True
        if self._mode == "choose" and keyval in _DIGITS:
            self._pick(_DIGITS[keyval] - 1)
            return True
        return False
