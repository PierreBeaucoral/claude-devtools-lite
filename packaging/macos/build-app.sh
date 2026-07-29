#!/usr/bin/env bash
# Builds "Claude DevTools.app" — a native macOS window around the dashboard.
#
#   bash packaging/macos/build-app.sh [output-dir]
#
# Requires the Xcode command line tools (swiftc). The icon step additionally
# uses Pillow if it happens to be installed; without it the app simply gets the
# default icon. Everything else is stdlib.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$REPO/..}"
APP="$OUT/Claude DevTools.app"

command -v swiftc >/dev/null 2>&1 || {
  echo "swiftc not found — install the Xcode command line tools: xcode-select --install" >&2
  exit 1
}

echo "Building native window…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O "$REPO/native/main.swift" -o "$APP/Contents/MacOS/ClaudeDevTools" \
       -framework Cocoa -framework WebKit

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Claude DevTools</string>
  <key>CFBundleDisplayName</key><string>Claude DevTools</string>
  <key>CFBundleIdentifier</key><string>com.claude-devtools-lite.app</string>
  <key>CFBundleVersion</key><string>0.5.0</string>
  <key>CFBundleShortVersionString</key><string>0.5.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>ClaudeDevTools</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
PLIST
echo "</plist>" >> "$APP/Contents/Info.plist"

# optional icon (needs Pillow); harmless to skip
if python3 -c "import PIL" 2>/dev/null; then
  echo "Rendering icon…"
  python3 "$HERE/make_icon.py" "$APP/Contents/Resources/AppIcon.icns" || \
    echo "  (icon step failed — continuing without one)"
else
  echo "Skipping icon (Pillow not installed)."
fi

codesign --force --sign - "$APP" 2>/dev/null || true
touch "$APP"

echo
echo "Built: $APP"
echo "Drag it to /Applications, or double-click it where it is."
