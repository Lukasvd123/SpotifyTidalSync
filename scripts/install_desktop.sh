#!/bin/bash
# Install SpotifySync into the desktop application menu, running from source.
# No PyInstaller build needed; use scripts/build_linux.sh for a standalone binary.
set -e

PROJECT_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
LAUNCHER="$PROJECT_ROOT/scripts/spotifysync-run.sh"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
BIN_DIR="$HOME/.local/bin"

chmod +x "$LAUNCHER"
mkdir -p "$DESKTOP_DIR" "$ICON_DIR" "$BIN_DIR"
install -m 644 "$PROJECT_ROOT/assets/logo.png" "$ICON_DIR/spotifysync.png"
install -Dm 644 "$PROJECT_ROOT/assets/logo.svg" \
    "$HOME/.local/share/icons/hicolor/scalable/apps/spotifysync.svg"

# Command-line entry point, so `spotifysync` works from a shell too.
ln -sf "$LAUNCHER" "$BIN_DIR/spotifysync"

cat > "$DESKTOP_DIR/spotifysync.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=SpotifySync
GenericName=Music Player
Comment=Play Spotify's queue through Tidal's hi-res streams
Exec=$LAUNCHER
Icon=spotifysync
Terminal=false
Categories=AudioVideo;Audio;Music;Player;
Keywords=spotify;tidal;music;hifi;sync;
StartupNotify=true
StartupWMClass=spotifysync
SingleMainWindow=true
DESKTOP

chmod 644 "$DESKTOP_DIR/spotifysync.desktop"
desktop-file-validate "$DESKTOP_DIR/spotifysync.desktop" 2>/dev/null || true
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

echo "Installed: $DESKTOP_DIR/spotifysync.desktop"
echo "Launcher : $LAUNCHER"
echo "Command  : $BIN_DIR/spotifysync"
echo "Search 'SpotifySync' in your app menu."
