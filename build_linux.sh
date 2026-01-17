#!/bin/bash
echo "Building SpotifySync for Linux..."

# Ensure python3 and pip are used
# Depending on your distro, you might need python3-pip and libvlc-dev installed via apt/dnf/pacman

pip install -r requirements.txt

# Clean previous builds
rm -rf dist build SpotifyTidalSync.spec

# Build
# Note: Linux executables don't have extensions, so we just name it SpotifySync
pyinstaller --noconsole --onefile --add-data ".env:." --name "SpotifySync" spotify.py

echo ""
echo "Build Complete!"
echo "You can find the executable in the 'dist' folder."
echo "NOTE: You must have VLC installed (libvlc) for audio to work."
echo "Run with: ./dist/SpotifySync"