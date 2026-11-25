# Upstream sync and fork decisions

This file is for whoever merges
[shakeelosmani/avd_feed_connect](https://github.com/shakeelosmani/avd_feed_connect)
into this fork. It records where the fork differs from upstream on purpose
and how past conflicts were resolved, so a merge can be done without asking.
Update it whenever a merge or a change adds a new decision.

**Last upstream commit merged:** `d64f6b9` (upstream #73, Shared folder).

## How to sync

```bash
git remote add upstream https://github.com/shakeelosmani/avd_feed_connect.git  # once
git fetch upstream main --no-tags
git log --oneline HEAD..upstream/main          # what's new
git merge upstream/main                         # one merge commit, no rebase
```

1. Resolve conflicts with the rules below. Keep the fork's side for anything
   listed under "Fork wins", and adapt upstream bug fixes and feed or protocol
   compatibility changes to the fork's code instead of reverting the fork.
2. Drop anything upstream adds for Flatpak, GNOME, KDE or its own release
   pipeline.
3. Add Spanish strings for new UI text under `src/avd_feed_connect/locales/`.
4. Run `tools/run-headless.sh -m pytest -q tests`. Delete upstream tests that
   only cover code the fork removed, and say so in the merge commit.
5. If upstream changes UI shown in the README, run `tools/screenshots.sh`.
6. Update README sections for new user-facing features, in the README's
   current style.
7. Commit as `chore: merge upstream <summary>`, list each conflict and how it
   was resolved in the body, and update **Last upstream commit merged** and
   **Merge history** here.

## Fork wins

These differ from upstream on purpose. Keep the fork's behavior in conflicts.

- **Identity:** app ID `io.github.roddolf.CloudPcConnect`, name Cloud PC
  Connect, its own icon, keyring schema (`auth/tokens.py`) and version line
  (from 0.1.0).
- **Distribution:** native Arch package only (`packaging/arch/`, installed
  to `/opt/cloud-pc-connect`, wrapper `/usr/bin/cloud-pc-connect`). The
  Flatpak manifest, Flatpak code paths and upstream's release workflow were
  removed. Releases come from `.github/workflows/release.yml` on `v*` tags;
  there are no release branches and no `gh-pages`.
- **Desktop scope:** Omarchy and Hyprland only. The GNOME workspace
  integration was removed; workspace placement goes through the Hyprland IPC
  socket (`gui/workspaces.py`) and never edits the user's Hyprland config.
- **Main window:** follows the live Omarchy theme (`gui/omarchy.py`).
  Resources are grouped into sections, Cloud PCs first, with collapsible
  headings remembered per account (`gui/resource_groups.py`). Search plus
  cascading filters, including a region filter grouped by geography; the
  feed has no region field, so the region is parsed from names. Keyboard
  navigation and a keyboard-shortcuts popover.
- **Tiles:** the layout stays fixed when a session connects. Actions depend on
  state: Connect / Connect in a window; Cancel connection; Show window,
  Reconnect, Disconnect.
- **Settings dialog:** sections built with `field()` and `help_text`. New
  upstream settings go into the matching section in that style.
- **Sign-in:** in-app WebKit sign-in with a system-browser fallback,
  cancellable, security keys offered by default, Entra errors shown instead of
  a blank window, sign-in windows kept drawing after suspend.
- **Security keys inside sessions:** `/dvc:rdpewa` is added unless the `.rdp`
  sets it (`rdp/launcher.py:webauthn_args`), never saved in Advanced flags.
  FreeRDP patch 0004 adds the account picker, PIN prompt and notifications.
  Never log PINs or credential IDs.
- **FreeRDP arguments** go through a private argument file
  (`private_argument_file`), not the command line.
- **Client shortcuts:** FreeRDP's own hotkeys and keyboard grab are off under
  Hyprland (`disable_client_hotkeys`). Hyprland owns shortcuts; the README
  documents a submap that passes them to Windows.
- **Rendering:** Vulkan without vsync on NVIDIA, because NVIDIA GL under
  XWayland stalls input on hidden workspaces. `SDL_RENDER_DRIVER` overrides.
- **Teams:** the freerdp-teams-redir add-in is built in; Teams optimization
  is on by default, with a per-resource setting.
- **Resilience:** a 401 while downloading a connection file refreshes the
  token and retries.
- **Launcher entries:** per-Cloud PC entries through the DynamicLauncher
  portal (`gui/launchers.py`) with the feed icon plus a PC badge. The app
  registers with the portal's host registry on a private D-Bus connection.
  Opening an entry shows a "Connecting to …" notification and doesn't raise
  the main window.
- **Tray:** StatusNotifierItem with per-PC actions and close/start-to-tray
  settings.
- **README** is an app README for users, not a changelog.

## Merge history

### `d64f6b9`: upstream #72 and #73

- **#72, workspace grouping:** kept the fork's sections and collapsible
  layout instead of upstream's `Gtk.Expander` groups. Took the feed's
  `workspace` field; `resource_groups.workspace()` prefers it over `tenant`,
  and the tray uses `tray_menu.workspace_name`. Removed upstream's two
  Expander tests and their `find_widgets` helper; added
  `test_feed_workspace_name_wins_over_tenant`.
- **#73, Shared folder:** kept the feature but moved it into the CONNECTION
  section with `field()` and `help_text`. Merged upstream's two option lists
  into `SHARE_LABELS = ["Automatic (off)", "Off", "Home folder", "Custom
  folder…"]` and dropped `SHARE_DEFAULT_*` and the "Use Default settings"
  string. Kept `AVD_SHARE`. Dropped the `AvdWorkspaces` D-Bus talk-name
  (GNOME integration, removed here).

## Patches

Pinned versions and the reason for each patch are in
`packaging/arch/PKGBUILD`. When bumping SDL or FreeRDP, check whether each
patch has landed upstream and drop it if so.
Keep SDL on 3.2.x: newer SDL reports a 0×0 display and full screen fails.

## Known limitations

- The AUR package isn't published yet (issue #1).
- CI has no GTK, so GUI tests only run locally.
- Some WebAuthn edge cases (cancel mid-operation, PIN-protected credential
  enumeration, timeouts) depend on FreeRDP and Windows behavior.
