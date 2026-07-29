#!/usr/bin/env bash
# Installs Claude DevTools into your Linux desktop menu (per-user, no sudo).
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"

chmod +x "$HERE/claude-devtools.sh"
mkdir -p "$APPS" "$ICONS"
cp "$HERE/claude-devtools.svg" "$ICONS/claude-devtools.svg"

cat > "$APPS/claude-devtools.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Claude DevTools
Comment=Inspect Claude Code sessions, tokens, and outputs
Exec=$HERE/claude-devtools.sh
Icon=claude-devtools
Terminal=false
Categories=Development;Utility;
StartupNotify=true
EOF

chmod +x "$APPS/claude-devtools.desktop"
command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "$APPS" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
  gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "Installed. Look for 'Claude DevTools' in your application menu."
echo "Repo: $REPO"
echo "Or run directly: $HERE/claude-devtools.sh"
