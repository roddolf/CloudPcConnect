"""GTK4 CSS theme for the app.

GTK CSS is not web CSS (no var() before GTK 4.16, no ::before), so the
palette is a block of @define-color lines and the sizes are substituted into
the stylesheet in Python. On Omarchy the palette, font, base size and corner
radius come from the live theme (see omarchy.py); elsewhere the built-in light
and dark palettes are used with the same flat structure.
"""
from string import Template

from . import omarchy

PALETTE_LIGHT = """
@define-color avd_bg #F4F6F9;
@define-color avd_surface #FFFFFF;
@define-color avd_surface2 #F1F4F8;
@define-color avd_border #E3E8EF;
@define-color avd_border_strong #D3DAE3;
@define-color avd_ink #1A2230;
@define-color avd_muted #66707E;
@define-color avd_faint #98A2B3;
@define-color avd_accent #0E7CF4;
@define-color avd_accent_ink #0B63C4;
@define-color avd_ok #2FB86B;
@define-color avd_warn #B87E00;
@define-color avd_err #D1343F;
@define-color avd_focus #0E7CF4;
@define-color avd_menu_border #D3DAE3;
@define-color avd_on_accent #FFFFFF;
"""
PALETTE_DARK = """
@define-color avd_bg #161A21;
@define-color avd_surface #212630;
@define-color avd_surface2 #2A303B;
@define-color avd_border #353D49;
@define-color avd_border_strong #48525F;
@define-color avd_ink #F2F5FA;
@define-color avd_muted #AEB8C6;
@define-color avd_faint #7B8593;
@define-color avd_accent #57A0FF;
@define-color avd_accent_ink #8ABAFF;
@define-color avd_ok #4FD08D;
@define-color avd_warn #F2B838;
@define-color avd_err #F0707A;
@define-color avd_focus #57A0FF;
@define-color avd_menu_border #48525F;
@define-color avd_on_accent #FFFFFF;
"""

# Sizes used without an Omarchy theme (system font, gentle rounding).
FALLBACK = {"family": None, "size": 13, "radius": 6, "normal_fill": 0.04,
            "normal_border": 0.4, "hover_fill": 0.08, "hover_border": 0.25,
            "selected_fill": 0.18, "pressed_fill": 0.22}


def omarchy_palette(tokens):
    """@define-color block for an Omarchy theme: surfaces are the theme
    background with foreground washes, as Omarchy's own shell draws them."""
    bg, fg, accent = tokens["background"], tokens["foreground"], tokens["accent"]
    on_accent = max((bg, fg), key=lambda c: abs(omarchy._luminance(c) - omarchy._luminance(accent)))
    colors = {
        "bg": bg, "surface": bg, "surface2": omarchy.mix(bg, fg, tokens["hover_fill"]),
        "border": omarchy.mix(bg, fg, 0.14),
        "border_strong": omarchy.mix(bg, fg, tokens["normal_border"]),
        "ink": fg, "muted": omarchy.mix(bg, fg, 0.68), "faint": omarchy.mix(bg, fg, 0.45),
        "accent": accent, "accent_ink": tokens["selected_text"],
        "ok": tokens["ok"], "warn": tokens["warn"], "err": tokens["error"],
        "focus": tokens["border"], "menu_border": tokens["menu_border"], "on_accent": on_accent,
    }
    return "\n" + "".join(f"@define-color avd_{k} {v};\n" for k, v in colors.items())


CSS_TEMPLATE = Template("""
window.avd, window.avd-settings, window.avd-signin, popover.avd-menu, .avd-keyprompt { $font font-size:${fs_body}px; }
window.avd, .avd-page { background:@avd_bg; color:@avd_ink; }
window.avd-signin { background:@avd_bg; color:@avd_ink; }
headerbar.avd-header button.signin-browser { color:@avd_muted; background:none;
  border:1px solid transparent; border-radius:${r}px; box-shadow:none; padding:4px 10px; }
headerbar.avd-header button.signin-browser:hover { color:@avd_ink;
  background:alpha(@avd_ink,$hover); border-color:alpha(@avd_ink,$hover_border); }

headerbar.avd-header { background:@avd_bg; color:@avd_ink; border-bottom:1px solid @avd_border;
  box-shadow:none; min-height:44px; padding:4px 8px; }
headerbar.avd-header windowcontrols button,
headerbar.avd-header .titlebutton { color:@avd_muted; background:none; box-shadow:none;
  border-radius:${r}px; min-width:24px; min-height:24px; }
headerbar.avd-header windowcontrols button:hover,
headerbar.avd-header .titlebutton:hover { color:@avd_ink; background:alpha(@avd_ink,$hover); }
.mark { min-width:24px; min-height:24px; border-radius:${r}px; color:@avd_accent;
  background:none; border:2px solid @avd_focus; }
.brand-title { font-weight:bold; font-size:${fs_sub}px; color:@avd_ink; }
.brand-sub { font-size:${fs_caption}px; color:@avd_faint; }

button.iconbtn, menubutton.iconbtn > button { border-radius:${r}px; color:@avd_muted;
  background:none; border:1px solid transparent; min-width:30px; min-height:30px;
  box-shadow:none; padding:0; transition:background 120ms ease-out, border-color 120ms ease-out; }
button.iconbtn:hover, menubutton.iconbtn > button:hover { color:@avd_ink;
  background:alpha(@avd_ink,$hover); border-color:alpha(@avd_ink,$hover_border); }
menubutton.iconbtn > button:checked { color:@avd_ink; background:alpha(@avd_ink,$selected); }
/* the class sits on the menubutton; style its inner button */
menubutton.acct-btn { background:none; box-shadow:none; }
menubutton.acct-btn > button { border-radius:${r}px; border:1px solid alpha(@avd_ink,$border);
  background:alpha(@avd_ink,$fill); color:@avd_ink; padding:2px 8px 2px 3px; box-shadow:none;
  min-height:28px; transition:background 120ms ease-out, border-color 120ms ease-out; }
menubutton.acct-btn > button:hover { background:alpha(@avd_ink,$hover);
  border-color:alpha(@avd_ink,$hover_border); }
menubutton.acct-btn > button:checked { background:alpha(@avd_ink,$selected); }
.avatar { min-width:22px; min-height:22px; border-radius:${r}px; background:alpha(@avd_accent,0.18);
  color:@avd_accent; font-weight:bold; font-size:${fs_caption}px; }
.acct-who { font-weight:bold; font-size:${fs_body}px; color:@avd_ink; }
.caret { color:@avd_faint; -gtk-icon-size:12px; }

window.avd button:focus-visible, popover.avd-menu button:focus-visible,
popover.avd-menu checkbutton:focus-visible, window.avd-settings button:focus-visible,
.avd-keyprompt button:focus-visible { outline:1px solid @avd_focus; outline-offset:-1px; }

.view-modes button, box.linked > button { background:alpha(@avd_ink,$fill); color:@avd_muted;
  border:1px solid alpha(@avd_ink,$border); border-radius:${r}px; box-shadow:none;
  min-height:26px; font-size:${fs_small}px; }
box.linked > button { border-radius:0; }
box.linked > button:first-child { border-top-left-radius:${r}px; border-bottom-left-radius:${r}px; }
box.linked > button:last-child { border-top-right-radius:${r}px; border-bottom-right-radius:${r}px; }
box.linked > button:not(:first-child) { border-left-width:0; }
.view-modes button:hover, box.linked > button:hover { background:alpha(@avd_ink,$hover); color:@avd_ink; }
.view-modes button:checked, box.linked > button:checked { background:alpha(@avd_ink,$selected);
  color:@avd_accent_ink; font-weight:bold; }

.section-h { font-weight:bold; font-size:${fs_caption}px; letter-spacing:1px; color:@avd_muted; }
.section-count { font-size:${fs_caption}px; color:@avd_faint; }
button.section-toggle { background:none; border:1px solid transparent; box-shadow:none;
  border-radius:${r}px; color:@avd_muted; padding:0; }
button.section-toggle:hover { background:alpha(@avd_ink,$hover); color:@avd_ink; }
button.section-toggle image { color:@avd_faint; -gtk-icon-size:12px; }
.subsection-h { font-size:${fs_small}px; color:@avd_muted; }
separator.subsection-rule { background:@avd_border; min-height:1px; }
.count { font-size:${fs_small}px; color:@avd_faint; }
entry.filter { min-height:24px; padding:0 8px; font-size:${fs_small}px; color:@avd_ink;
  background:alpha(@avd_ink,$fill); border:1px solid alpha(@avd_ink,$border); border-radius:${r}px;
  box-shadow:none; outline:none; caret-color:@avd_accent; }
entry.filter:focus-within { border-color:@avd_focus; }
entry.filter image { color:@avd_faint; }
entry.filter > text > placeholder { color:@avd_faint; }
dropdown.facet > button { min-height:24px; padding:0 8px; font-size:${fs_small}px; color:@avd_muted;
  background:alpha(@avd_ink,$fill); border:1px solid alpha(@avd_ink,$border); border-radius:${r}px;
  box-shadow:none; }
dropdown.facet > button:hover { background:alpha(@avd_ink,$hover); color:@avd_ink; }
dropdown.facet > button:checked { background:alpha(@avd_ink,$selected); color:@avd_accent_ink; }

flowbox, flowboxchild { background:none; padding:0; border:none; box-shadow:none; outline:none; }
flowboxchild:selected, flowboxchild:focus, flowboxchild:active { background:none; }
flowboxchild:focus-visible { outline:none; }
.tile { background:alpha(@avd_ink,$fill); border:1px solid @avd_border; border-radius:${r}px;
  padding:14px; transition:background 120ms ease-out, border-color 120ms ease-out,
  box-shadow 120ms ease-out; }
.tile:hover { background:alpha(@avd_ink,$hover); border-color:alpha(@avd_ink,$hover_border); }
flowboxchild:active .tile { background:alpha(@avd_ink,$pressed); }
.tile.tile-connected { border-color:alpha(@avd_ok,0.7); }
.tile.tile-error { border-color:alpha(@avd_err,0.8); }
flowboxchild:focus-visible .tile { border-color:@avd_focus; box-shadow:inset 0 0 0 1px @avd_focus; }

.ic { min-width:40px; min-height:40px; border-radius:${r}px; background:alpha(@avd_accent,0.12); }
.ic.desktop image { color:@avd_accent; }
.ic.app { background:alpha(@avd_warn,0.12); }
.ic.app image { color:@avd_warn; }

.tname { font-weight:bold; font-size:${fs_sub}px; color:@avd_ink; }
.chiplabel { font-size:${fs_caption}px; font-weight:bold; letter-spacing:0.5px; color:@avd_muted; }
.chipdot { min-width:6px; min-height:6px; border-radius:${r_dot}px; background:@avd_accent; }
.chip-app .chipdot { background:@avd_warn; }
.tile-sub { font-size:${fs_small}px; color:@avd_muted; }
.tile-actions { margin-top:-4px; }
button.tile-action { min-width:26px; min-height:26px; padding:0; border-radius:${r}px;
  background:none; border:1px solid @avd_border; color:@avd_muted; box-shadow:none; }
button.tile-action:hover { color:@avd_ink; border-color:alpha(@avd_ink,$hover_border);
  background:alpha(@avd_ink,$hover); }
button.tile-action:focus-visible { border-color:@avd_focus; }

.status-pill { font-size:${fs_caption}px; font-weight:bold; letter-spacing:0.5px; padding:1px 6px;
  border-radius:${r}px; color:@avd_ok; background:alpha(@avd_ok,0.12);
  border:1px solid alpha(@avd_ok,0.45); }
.status-pill.pill-connecting { color:@avd_warn; background:alpha(@avd_warn,0.12);
  border-color:alpha(@avd_warn,0.45); }
.status-pill.pill-ended { color:@avd_muted; background:none; border-color:@avd_border; }
.status-pill.pill-error { color:@avd_err; background:alpha(@avd_err,0.12);
  border-color:alpha(@avd_err,0.45); }

.foot { background:@avd_bg; border-top:1px solid @avd_border; padding:5px 14px; }
.status { color:@avd_muted; font-size:${fs_small}px; }
.ver { color:@avd_faint; font-size:${fs_caption}px; }
.livedot { min-width:6px; min-height:6px; border-radius:${r_dot}px; background:@avd_ok; }
.error { color:@avd_err; }

.empty-glyph { color:@avd_faint; }
.empty-title { font-weight:bold; font-size:${fs_title}px; color:@avd_ink; }

.connect-card, .toast, .avd-keyprompt { background:@avd_bg; color:@avd_ink;
  border:2px solid @avd_focus; border-radius:${r}px; box-shadow:none; }
.connect-card { padding:14px 28px 20px 28px; }
.connect-card spinner, .avd-keyprompt spinner { color:@avd_accent; }
.connect-label { font-weight:bold; font-size:${fs_title}px; color:@avd_ink; }
.connect-sub { font-size:${fs_small}px; color:@avd_muted; }

.toast { padding:4px 4px 4px 14px; }
.toast-label { font-size:${fs_body}px; color:@avd_ink; }

.bigmark { min-width:56px; min-height:56px; border-radius:${r}px; color:@avd_accent;
  background:none; border:2px solid @avd_focus; }
.hero-title { font-weight:bold; font-size:${fs_display}px; color:@avd_ink; }
.hero-sub { font-size:${fs_body}px; color:@avd_muted; }
.hero-note { font-size:${fs_caption}px; color:@avd_faint; }
.avd-page button.flat, .connect-card button.flat, .toast button.flat {
  color:@avd_muted; background:none; border:1px solid transparent; border-radius:${r}px;
  box-shadow:none; min-height:26px; padding:2px 10px; }
.avd-page button.flat:hover, .connect-card button.flat:hover, .toast button.flat:hover {
  color:@avd_accent_ink; background:alpha(@avd_ink,$hover); border-color:alpha(@avd_ink,$hover_border); }
button.msbtn, .avd-keyprompt button.suggested-action { background:@avd_accent; color:@avd_on_accent;
  font-weight:bold; border-radius:${r}px; border:1px solid @avd_accent; box-shadow:none;
  transition:background 120ms ease-out; }
button.msbtn { font-size:${fs_sub}px; padding:8px 20px; }
button.msbtn:hover, .avd-keyprompt button.suggested-action:hover {
  background:mix(@avd_accent,@avd_ink,0.18); }
button.msbtn:active, .avd-keyprompt button.suggested-action:active {
  background:mix(@avd_accent,@avd_bg,0.25); }
button.msbtn:focus-visible { outline:1px solid @avd_ink; outline-offset:2px; }
.avd-keyprompt button.suggested-action:disabled { background:alpha(@avd_accent,0.3);
  border-color:transparent; color:alpha(@avd_on_accent,0.7); }

window.avd-settings { background:@avd_bg; color:@avd_ink; }
window.avd-settings headerbar { background:@avd_bg; color:@avd_ink; box-shadow:none;
  border-bottom:1px solid @avd_border; }
window.avd-settings label { color:inherit; }
window.avd-settings button, window.avd-settings entry {
  background:alpha(@avd_ink,$fill); color:@avd_ink; border:1px solid alpha(@avd_ink,$border);
  border-radius:${r}px; box-shadow:none; }
window.avd-settings button:hover { background:alpha(@avd_ink,$hover); }
window.avd-settings entry:focus-within { border-color:@avd_focus; outline:none; }
window.avd-settings button.suggested-action { background:@avd_accent; color:@avd_on_accent;
  border-color:@avd_accent; }
window.avd-settings entry > text > placeholder { color:@avd_faint; }
window.avd-settings popover > contents { background:@avd_bg; color:@avd_ink;
  border:2px solid @avd_menu_border; border-radius:${r}px; box-shadow:none; }
window.avd-settings listview { background:@avd_bg; color:@avd_ink; }
window.avd-settings listview row:hover { background:alpha(@avd_ink,$hover); }
window.avd-settings listview row:selected { background:alpha(@avd_ink,$selected); color:@avd_accent_ink; }
.settings-title { font-weight:bold; font-size:${fs_heading}px; color:@avd_ink; }
.settings-sub { font-size:${fs_small}px; color:@avd_muted; }
.field-label { font-weight:bold; font-size:${fs_caption}px; letter-spacing:0.6px; color:@avd_muted; }
window.avd-settings .settings-foot { border-top:1px solid @avd_border; padding:12px 22px; }
window.avd-settings .settings-subpanel { border-left:1px solid @avd_border; padding-left:12px; }
window.avd-settings scrolledwindow, window.avd-settings viewport { background:none; }
window.avd-settings dropdown > button, window.avd-settings entry { min-height:28px; padding-top:0;
  padding-bottom:0; }
window.avd-settings .settings-foot button { min-height:28px; padding:0 16px; }
window.avd-settings headerbar.avd-header windowcontrols button { border:1px solid transparent;
  background:none; min-width:24px; min-height:24px; padding:0; }
window.avd-settings headerbar.avd-header windowcontrols button:hover { background:alpha(@avd_ink,$hover);
  border-color:alpha(@avd_ink,$hover_border); }

/* Menus mirror Omarchy's menu card: theme background, 2px border, no shadow. */
popover.avd-menu { background:none; box-shadow:none; }
popover.avd-menu > contents { background:@avd_bg; color:@avd_ink; padding:4px;
  border:2px solid @avd_menu_border; border-radius:${r}px; box-shadow:none; }
popover.avd-menu > arrow { background:@avd_bg; border:2px solid @avd_menu_border; }
/* menus are parented to the headerbar; drop its 6px button margins */
popover.avd-menu button { margin-top:0; margin-bottom:0; }
popover.avd-menu button.flat { color:@avd_ink; background:none; box-shadow:none;
  border:1px solid transparent; border-radius:${r}px; min-height:28px; padding:2px 4px; }
popover.avd-menu button.flat:hover, popover.avd-menu button.flat:focus-visible {
  background:alpha(@avd_ink,$hover); border-color:alpha(@avd_ink,$hover_border);
  color:@avd_accent_ink; outline:none; }
popover.avd-menu button.flat:hover .account-email { color:@avd_muted; }
popover.avd-menu button.flat:disabled { color:@avd_faint; }
popover.avd-menu button.row-edit { color:@avd_faint; min-width:28px; padding:0; }
popover.avd-menu button.row-edit:hover, popover.avd-menu button.row-edit:focus-visible { color:@avd_accent_ink; }
popover.avd-menu checkbutton { color:@avd_ink; padding:2px 4px; }
popover.avd-menu checkbutton:disabled { color:@avd_faint; }
popover.avd-menu checkbutton check { min-width:14px; min-height:14px; border-radius:${r_dot}px;
  background:alpha(@avd_ink,$fill); border:1px solid alpha(@avd_ink,$border); box-shadow:none; }
popover.avd-menu checkbutton check:checked { background:@avd_accent; border-color:@avd_accent;
  color:@avd_on_accent; }
popover.avd-menu separator { background:@avd_border; min-height:1px; margin:4px 0; }
.account-name { font-weight:bold; }
button.current-account .account-name { color:@avd_accent_ink; }
.account-email { font-size:${fs_caption}px; color:@avd_muted; }
.menu-head { font-weight:bold; font-size:${fs_caption}px; letter-spacing:1px; color:@avd_muted; }

/* In-app sign-in / security-key card (signin_panel.py), after Omarchy's polkit prompt. */
.avd-keyprompt { padding:18px; }
.avd-keyprompt.error { border-color:@avd_err; }
.avd-keyprompt-glyph { color:@avd_accent; }
.avd-keyprompt.error .avd-keyprompt-glyph { color:@avd_err; }
.avd-keyprompt.touch .avd-keyprompt-glyph { animation:avd-pulse 1400ms ease-in-out infinite; }
@keyframes avd-pulse { 0% { opacity:1; } 50% { opacity:0.3; } 100% { opacity:1; } }
.avd-keyprompt-title { font-weight:bold; font-size:${fs_title}px; color:@avd_ink; }
.avd-keyprompt-text { color:@avd_muted; }
.avd-keyprompt-error { font-size:${fs_small}px; color:@avd_err; }
.avd-keyprompt-hint { font-size:${fs_caption}px; color:@avd_faint; }
.avd-keyprompt entry.avd-keyprompt-pin { min-height:34px; padding:0 10px; color:@avd_ink;
  background:alpha(@avd_ink,$fill); border:1px solid alpha(@avd_ink,$border);
  border-radius:${r}px; box-shadow:none; outline:none; caret-color:@avd_accent; }
.avd-keyprompt entry.avd-keyprompt-pin:focus-within { border-color:@avd_focus; }
.avd-keyprompt.pin-error entry.avd-keyprompt-pin { border-color:@avd_err; }
.avd-keyprompt list { background:none; }
.avd-keyprompt row.avd-keyprompt-account { background:none; color:@avd_ink;
  border:1px solid transparent; border-radius:${r}px; padding:6px 8px; outline:none; }
.avd-keyprompt row.avd-keyprompt-account:hover { background:alpha(@avd_ink,$hover); }
.avd-keyprompt row.avd-keyprompt-account:selected { background:alpha(@avd_ink,$hover);
  border-color:alpha(@avd_ink,$hover_border); }
.avd-keyprompt row.avd-keyprompt-account:selected .avd-keyprompt-account-name { color:@avd_accent_ink; }
.avd-keyprompt row.avd-keyprompt-account:focus-visible { border-color:@avd_focus; }
.avd-keyprompt-key { font-size:${fs_caption}px; font-weight:bold; color:@avd_muted; min-width:16px;
  border:1px solid @avd_border; border-radius:${r_dot}px; padding:0 3px; }
.avd-keyprompt-account-name { font-weight:bold; }
.avd-keyprompt-account-detail { font-size:${fs_small}px; color:@avd_muted; }
.avd-keyprompt-chevron { color:alpha(@avd_ink,0.36); }
.avd-keyprompt button { background:alpha(@avd_ink,$fill); color:@avd_ink;
  border:1px solid alpha(@avd_ink,$border); border-radius:${r}px; box-shadow:none;
  min-height:28px; padding:0 14px; }
.avd-keyprompt button:hover { background:alpha(@avd_ink,$hover); border-color:alpha(@avd_ink,$hover_border); }
""")


def _px(value):
    return ("%.1f" % value).rstrip("0").rstrip(".")


def build_css(tokens=None):
    """The stylesheet body (no palette) for Omarchy tokens or the fallback."""
    t = dict(FALLBACK, **(tokens or {}))
    base = float(t["size"])
    radius = int(t["radius"])
    family = t.get("family")
    return CSS_TEMPLATE.substitute(
        font='font-family:"%s", monospace;' % family if family else "",
        r=radius, r_dot=min(radius, 3),
        fs_caption=_px(base * 10 / 12), fs_small=_px(base * 11 / 12), fs_body=_px(base),
        fs_sub=_px(base * 13 / 12), fs_title=_px(base * 14 / 12),
        fs_heading=_px(base * 16 / 12), fs_display=_px(base * 2),
        fill=t["normal_fill"], border=t["normal_border"], hover=t["hover_fill"],
        hover_border=t["hover_border"], selected=t["selected_fill"], pressed=t["pressed_fill"])


def stylesheet(dark, tokens=None):
    """Full CSS: the Omarchy palette when its mode matches the requested
    appearance, otherwise the built-in palette for that mode."""
    if tokens and tokens.get("dark") == dark:
        palette = omarchy_palette(tokens)
    else:
        palette = PALETTE_DARK if dark else PALETTE_LIGHT
    return palette + build_css(tokens)


CSS_BASE = build_css()
