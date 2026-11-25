# AGENTS.md

Cloud PC Connect is an Omarchy (Arch Linux + Hyprland) client for Windows 365
Cloud PCs, Dev Boxes and Azure Virtual Desktop. It is a GTK 4 / PyGObject app
that signs in with Entra ID, reads the AVD feed and connects with a patched
FreeRDP SDL3 client. It is a fork of
[shakeelosmani/avd_feed_connect](https://github.com/shakeelosmani/avd_feed_connect).

Read [docs/UPSTREAM.md](docs/UPSTREAM.md) before merging upstream changes or
changing behavior that differs from upstream.

## Layout

| Path | What |
|---|---|
| `src/avd_feed_gui.py` | GTK app (`AvdApp`): window, tiles, settings, tray, launch |
| `src/avdfeed.py` | CLI |
| `src/avd_feed_connect/auth/` | Entra sign-in, token store (keyring), FIDO |
| `src/avd_feed_connect/feed/` | Feed discovery and download |
| `src/avd_feed_connect/rdp/` | `.rdp` handling and `sdl-freerdp` arguments |
| `src/avd_feed_connect/gui/` | GUI helpers: Omarchy theme, workspaces, launchers, tray, storage, translations |
| `src/avd_feed_connect/locales/` | Spanish translations; add every new UI string |
| `patches/` | SDL3 and FreeRDP patches, explained in `packaging/arch/PKGBUILD` |
| `packaging/arch/` | The only build: `PKGBUILD`, `.SRCINFO`, `/usr/bin` wrapper |
| `tools/` | Headless test runner, screenshot renderer, demo themes |
| `screenshots/` | README images, rendered from demo data |

The package installs the app and its private SDL3, FreeRDP and Teams add-in
into `/opt/cloud-pc-connect`. `/usr/bin/cloud-pc-connect` sets
`AVD_SDL_FREERDP` to the bundled client. App data lives in
`~/.local/share/avd-feed-connect`, refresh tokens in the login keyring.

## Build and test

```bash
tools/run-headless.sh -m pytest -q tests          # needs python-pytest
cd packaging/arch && makepkg -si                  # full package build
tools/screenshots.sh                              # regenerate README screenshots
```

`tools/run-headless.sh` runs Python on a private GTK Broadway display with
throwaway XDG directories, so tests never touch the real screen, settings or
keyring. GUI tests need `webkitgtk-6.0`, which the package depends on. CI
(`.github/workflows/ci.yml`) runs the tests without GTK, so GUI tests skip
there; run them locally.

To try a checkout against the installed package's FreeRDP:

```bash
AVD_SDL_FREERDP=/opt/cloud-pc-connect/bin/sdl-freerdp AVD_SDL_LIBS= python3 src/avd_feed_gui.py
```

`AVD_DEMO=1` shows mock Contoso data.

## Rules

- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `build:`, `ci:`, `test:`, `refactor:`,
  `chore:`), with an optional scope such as `fix(launcher):`. Group related
  work into a few scoped commits. Don't commit or push after every change;
  commit and push when the user asks.
- **Screenshots** use the demo data and offscreen rendering
  (`tools/screenshots.sh`). Never capture the real desktop, accounts or
  Cloud PCs.
- **Secrets:** never print or log tokens, auth codes, PINs, credential IDs or
  `.rdp` contents.
- **Live sessions:** ask before anything that disconnects or restarts an
  active RDP session or the running app.
- **Scope:** Omarchy and Hyprland only. Don't add GNOME, KDE or Flatpak
  integrations.
- **README** describes the current app for users: features, install, usage,
  settings, troubleshooting. Keep history and decisions out of it; they go in
  `docs/UPSTREAM.md` and commit messages. Only document install methods that
  exist.
- Write tests for behavior you change, in the existing test files.

## Releases

1. Set the version in `pyproject.toml`, `src/avd_feed_connect/__init__.py`,
   `APP_VERSION` in `src/avd_feed_gui.py`, `pkgver` in
   `packaging/arch/PKGBUILD` (reset `pkgrel=1`) and the README install URL.
2. Add a `<release>` to `io.github.roddolf.CloudPcConnect.metainfo.xml`.
3. Regenerate `packaging/arch/.SRCINFO` with `makepkg --printsrcinfo`.
4. Commit, then push a `vX.Y.Z` tag. `.github/workflows/release.yml` builds
   the package in an Arch container and attaches it to the GitHub release.
   It fails if the tag doesn't match `pkgver`.

The package isn't on the AUR yet; see issue #1.
