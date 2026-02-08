#!/bin/bash

# Navigate to project root (parent of scripts/)
cd "$(dirname "$0")/.."
echo "Building SpotifySync for Linux..."

# Ensure python3 and pip are used
# Depending on your distro, you might need python3-pip and libvlc-dev installed via apt/dnf/pacman

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
pyinstaller --noconsole --onefile --add-data ".env:." \
    $ICON_FLAG \
    --name "SpotifySync" \
    --distpath "_build/dist" \
    --workpath "_build/build" \
    --specpath "_build" \
    spotify.py

if [ -f "_build/dist/SpotifySync" ]; then
    cp "_build/dist/SpotifySync" .
    echo ""
    echo "Build Complete!"
    echo "SpotifySync is ready to use in the project root."
    echo "NOTE: You must have VLC installed (libvlc) for audio to work."
    echo "Run with: ./SpotifySync"
else
    echo ""
    echo "BUILD FAILED - Check errors above."
fi
