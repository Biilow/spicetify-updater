# Spicetify Updater

Puts Spicetify back after Spotify updates itself on macOS, checks it actually
works, and puts Spotify back to normal if it doesn't.

Spotify updates wipe Spicetify, so after every update your theme and
extensions vanish until you run `spicetify apply` again. This does that for
you, in the background, without ever leaving Spotify broken.

macOS only. No telemetry: the only network request is to GitHub's public
releases page for Spicetify.

## Requirements

- macOS 13 or later, with Spotify installed in `/Applications`.
- Spicetify installed the usual way, in `~/.spicetify`
  ([spicetify.app](https://spicetify.app/docs/getting-started)), and applied
  once by hand.
- Apple's Command Line Tools, for `swiftc` and `python3`. If you don't have
  them, run `xcode-select --install`.

## What happens when Spotify updates

1. A LaunchAgent watches `/Applications/Spotify.app/Contents/Info.plist`. An
   update replaces that file, and Spicetify never touches it. It waits 15 s so
   Spotify's own installer can finish.
2. If Spotify has been open for more than 3 minutes, you might be listening. It
   sends a notification and waits for you to quit Spotify. The wait is a kernel
   exit event, not a polling loop. If Spotify only just opened (the usual case
   right after an update), it quits Spotify straight away.
3. Installs a newer Spicetify from its GitHub release if one exists, checking
   the release's SHA-256. It doesn't use `spicetify upgrade`, because that
   re-applies and restarts Spotify on its own.
4. Reads the "Spotify for macOS: a -> b" tested range from the release notes.
   An out-of-range Spotify is still tried, but the result is double-checked.
5. Runs `spicetify backup apply -n`.
6. **Health check.** Opens Spotify hidden with a one-off debug port on
   127.0.0.1 and asks the page whether Spicetify loaded, hooked into Spotify, and
   the UI rendered (`<main>` plus the now-playing bar). This catches the
   black-window failure that `spicetify apply` reports as a success. Spotify is
   then quit so the port doesn't stay open.
7. Pass: reopens Spotify if it was open before and sends a notification.
   Fail: `spicetify restore`, checks Spotify is stock again, reopens it, sends a
   notification, and waits until a newer Spicetify comes out before trying
   again.

## Install

```bash
git clone https://github.com/Biilow/spicetify-updater.git
cd spicetify-updater
./install.sh
```

Then turn on **Spicetify Updater** in System Settings > Privacy & Security >
App Management. macOS blocks background jobs from writing inside Spotify.app,
which is why a small app (`~/Applications/Spicetify Updater.app`) runs the
script instead of python directly. The app is ad-hoc signed, so rebuilding it
makes macOS forget the grant. `install.sh` only rebuilds it when
`launcher/` changes. Editing `updater.py` and re-running `install.sh` keeps the
grant.

Like granting Terminal App Management, anything that can edit `updater.py` or
`~/.spicetify/spicetify` (both owned by your user) runs with this permission.

## Commands

```bash
P="$HOME/Library/Application Support/Spicetify Updater/updater.py"
/usr/bin/python3 -I "$P" status      # JSON: versions, app files, last result
/usr/bin/python3 -I "$P" pause       # stop automatic re-applying
/usr/bin/python3 -I "$P" resume
launchctl kickstart "gui/$(id -u)/io.github.biilow.spicetify-updater"   # run the agent now
```

`apply-now` re-applies immediately, quitting and reopening Spotify. Run it
through the agent's app, or from a terminal that has App Management itself.

## Triggers

- **Info.plist changes**: a Spotify update.
- **Login**: catches updates that landed while you were logged out.
- **Daily at 12:00**: only does anything while waiting for Spicetify to
  support a new Spotify. GitHub can't notify a Mac, so this is the one check on
  a timer. Otherwise it exits without touching the network.

If Spotify is stock at a version that had Spicetify before, it assumes you ran
`spicetify restore` on purpose and leaves it alone.

## Files

| Path | What |
|---|---|
| `~/Applications/Spicetify Updater.app` | launcher, holds the App Management grant |
| `~/Library/Application Support/Spicetify Updater/` | installed `updater.py`, `state.json`, `paused` flag |
| `~/Library/LaunchAgents/io.github.biilow.spicetify-updater.plist` | the agent |
| `~/Library/Logs/Spicetify Updater.log` | log, trimmed at 2 MB |

Notifications go through `osascript`, so they show Script Editor's icon.
Native ones would need a paid Apple developer certificate, because
Notification Center won't register ad-hoc-signed apps.

`./uninstall.sh` removes everything above except the log and leaves Spotify
and Spicetify as they are.

## License

MIT
