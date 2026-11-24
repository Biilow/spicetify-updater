#!/bin/bash
# Installs Spicetify Updater: builds the small launcher app, copies updater.py,
# writes the LaunchAgent and starts it. Safe to re-run after editing updater.py.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="io.github.biilow.spicetify-updater"
APP="$HOME/Applications/Spicetify Updater.app"
SUPPORT="$HOME/Library/Application Support/Spicetify Updater"
LOG="$HOME/Library/Logs/Spicetify Updater.log"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$SUPPORT" "$HOME/Applications" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
install -m 0644 "$HERE/updater.py" "$SUPPORT/updater.py"

# Rebuilding the launcher changes its signature, and macOS then drops the
# App Management grant, so only rebuild when its source actually changed.
src_hash=$(cat "$HERE/launcher/main.swift" "$HERE/launcher/Info.plist" | shasum -a 256 | cut -d' ' -f1)
if [[ ! -x "$APP/Contents/MacOS/SpicetifyUpdater" || "$(cat "$SUPPORT/launcher.sha256" 2>/dev/null)" != "$src_hash" ]]; then
  echo "Building Spicetify Updater.app"
  build=$(mktemp -d)
  mkdir -p "$build/Spicetify Updater.app/Contents/MacOS"
  cp "$HERE/launcher/Info.plist" "$build/Spicetify Updater.app/Contents/Info.plist"
  swiftc -O -o "$build/Spicetify Updater.app/Contents/MacOS/SpicetifyUpdater" "$HERE/launcher/main.swift"
  codesign --force --sign - --identifier "$LABEL" "$build/Spicetify Updater.app"
  rm -rf "$APP"
  mv "$build/Spicetify Updater.app" "$APP"
  rmdir "$build"
  echo "$src_hash" > "$SUPPORT/launcher.sha256"
fi

# Triggers: WatchPaths fires when Spotify replaces its Info.plist, which is
# what an update does (Spicetify never touches that file). RunAtLoad covers
# updates that landed while you were logged out. The daily run only matters
# while waiting for Spicetify to support a new Spotify: GitHub can't notify
# a Mac, so that is the one check on a timer. Otherwise it exits without
# touching the network.
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>$APP/Contents/MacOS/SpicetifyUpdater</string>
		<string>run</string>
	</array>
	<key>WatchPaths</key>
	<array>
		<string>/Applications/Spotify.app/Contents/Info.plist</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>StartCalendarInterval</key>
	<dict>
		<key>Hour</key>
		<integer>12</integer>
		<key>Minute</key>
		<integer>0</integer>
	</dict>
	<key>ThrottleInterval</key>
	<integer>30</integer>
	<key>StandardOutPath</key>
	<string>$LOG</string>
	<key>StandardErrorPath</key>
	<string>$LOG</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. Log: $LOG"
