#!/bin/bash
# Removes Spicetify Updater. Leaves Spicetify and Spotify exactly as they are.
set -euo pipefail

LABEL="io.github.biilow.spicetify-updater"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/Applications/Spicetify Updater.app" "$HOME/Library/Application Support/Spicetify Updater"
echo "Removed. The log is still at ~/Library/Logs/Spicetify Updater.log, and the app can be"
echo "removed from System Settings > Privacy & Security > App Management."
