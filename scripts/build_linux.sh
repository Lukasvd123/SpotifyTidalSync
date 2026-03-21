#!/bin/bash
set -e

# Navigate to project root (parent of scripts/)
cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"

echo "=== SpotifySync Linux Build ==="

# Detect package manager
install_deps() {
    if command -v dnf &>/dev/null; then
        echo "Detected: Fedora/RHEL (dnf)"
        sudo dnf install -y \
            python3 python3-pip python3-tkinter python3-devel \
            vlc vlc-devel \
            dbus-devel dbus-glib-devel \
            gcc
    elif command -v apt-get &>/dev/null; then
        echo "Detected: Debian/Ubuntu (apt)"
        sudo apt-get update
        sudo apt-get install -y \
            python3 python3-pip python3-tk python3-dev python3-venv \
            vlc libvlc-dev \
            libdbus-1-dev libdbus-glib-1-dev \
            gcc pkg-config
    elif command -v pacman &>/dev/null; then
        echo "Detected: Arch Linux (pacman)"
        sudo pacman -S --needed --noconfirm \
            python python-pip tk \
            vlc \
            dbus dbus-glib \
            gcc
    elif command -v zypper &>/dev/null; then
        echo "Detected: openSUSE (zypper)"
        sudo zypper install -y \
            python3 python3-pip python3-tk python3-devel \
            vlc vlc-devel \
            dbus-1-devel dbus-1-glib-devel \
            gcc
    else
        echo "ERROR: Could not detect package manager (tried dnf, apt, pacman, zypper)."
        echo "Please manually install: python3, python3-pip, python3-tkinter, vlc, dbus dev libs, gcc"
        exit 1
    fi
}

echo "Installing system dependencies..."
install_deps

# Create virtual environment if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

echo "Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Clean previous builds
rm -rf _build

# Convert logo if Pillow available
if python3 -c "from PIL import Image" 2>/dev/null; then
    python3 -c "
from PIL import Image
img = Image.open('assets/logo.png')
img.save('assets/logo.ico', format='ICO', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
"
    ICON_FLAG="--icon assets/logo.ico"
else
    ICON_FLAG=""
fi


# Build
mkdir -p _build
pyinstaller --noconsole --onefile \
    $ICON_FLAG \
    --name "SpotifySync" \
    --distpath "_build/dist" \
    --workpath "_build/build" \
    --specpath "_build" \
    spotify.py

if [ -f "_build/dist/SpotifySync" ]; then
    cp "_build/dist/SpotifySync" .

    # Install desktop shortcut so it appears in the app menu and can be double-clicked
    DESKTOP_DIR="$HOME/.local/share/applications"
    ICON_SRC="$PROJECT_ROOT/assets/logo.png"
    ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
    mkdir -p "$DESKTOP_DIR" "$ICON_DIR"
    cp "$ICON_SRC" "$ICON_DIR/spotifysync.png"

    cat > "$DESKTOP_DIR/spotifysync.desktop" <<DESKTOP
[Desktop Entry]
Name=SpotifySync
Comment=Sync Spotify playback with Tidal Hi-Res audio
Exec=$PROJECT_ROOT/SpotifySync
Icon=spotifysync
Terminal=false
Type=Application
Categories=Audio;Music;Player;
DESKTOP

    desktop-file-validate "$DESKTOP_DIR/spotifysync.desktop" 2>/dev/null || true
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

    echo ""
    echo "Build Complete!"
    echo "SpotifySync installed to app menu — search 'SpotifySync' or double-click from Activities."
    echo "You can also run directly: ./SpotifySync"
else
    echo ""
    echo "BUILD FAILED - Check errors above."
    exit 1
fi
